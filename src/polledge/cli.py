"""Command-line interface (Czech output)."""

from __future__ import annotations

import functools
import logging
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Optional

import numpy as np
import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from polledge.config import (
    ElectionConfig,
    ElectionResult,
    Settings,
    discover_elections,
    get_election,
    load_settings,
    model_params_for,
    read_yaml,
)
from polledge.db import PaperBetRow, make_session_factory
from polledge.errors import PollEdgeError
from polledge.logging_setup import setup_logging
from polledge.model.markets import parse_market
from polledge.model.simulate import SimulationResult
from polledge.odds.file_source import source_for_path
from polledge.odds.repository import load_quotes, store_quotes
from polledge.polls.repository import load_polls
from polledge.polls.wiki_client import WikiClient

for _stream in (sys.stdout, sys.stderr):
    # Windows consoles/pipes may default to a legacy code page; never crash on output.
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass

app = typer.Typer(
    help="PollEdge – porovnání modelu z předvolebních průzkumů s kurzy sázkových kanceláří. "
    "Aplikace pouze analyzuje a doporučuje, nikdy sama nesází.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()
log = logging.getLogger("polledge")


@dataclass
class AppState:
    settings: Settings
    sessions: object

    def session(self):
        return self.sessions()


_state: AppState | None = None


def state() -> AppState:
    assert _state is not None
    return _state


@app.callback()
def main(
    config: Annotated[Path, typer.Option("--config", "-c", help="Cesta k settings.yaml")] = Path("config/settings.yaml"),
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Podrobné logování")] = False,
) -> None:
    global _state
    try:
        settings = load_settings(config)
    except PollEdgeError as exc:
        console.print(f"[red]Chyba:[/red] {exc}")
        raise typer.Exit(1) from None
    setup_logging(settings.resolve(settings.log_dir), verbose)
    _state = AppState(settings, make_session_factory(settings.resolve(settings.database)))


def handle_errors(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except PollEdgeError as exc:
            console.print(f"[red]Chyba:[/red] {exc}")
            raise typer.Exit(1) from None

    return wrapper


# ---------- formatting helpers ----------

def pct(p: float | None, digits: int = 1) -> str:
    if p is None:
        return "–"
    return f"{p * 100:.{digits}f} %".replace(".", ",")


def num(x: float | None, digits: int = 2) -> str:
    return "–" if x is None else f"{x:.{digits}f}".replace(".", ",")


def money(x: float) -> str:
    return f"{x:,.0f}".replace(",", " ") + f" {state().settings.signals.currency}"


def signed_pct(p: float | None) -> str:
    if p is None:
        return "–"
    color = "green" if p > 0 else "red"
    return f"[{color}]{p * 100:+.1f} %[/{color}]".replace(".", ",")


def outcome_label(election: ElectionConfig, outcome: str) -> str:
    if "|" in outcome:
        return " vs. ".join(election.candidate_name(c) for c in outcome.split("|"))
    labels = {"yes": "ano", "no": "ne", "over": "nad", "under": "pod"}
    return labels.get(outcome) or election.candidate_name(outcome)


def market_label(election: ElectionConfig, market: str) -> str:
    names = {cid: election.candidate_name(cid) for cid in election.candidates}
    return parse_market(market).describe(names)


# ---------- commands ----------

@app.command("update-polls")
@handle_errors
def update_polls_cmd(
    election: Annotated[Optional[list[str]], typer.Option("--election", "-e", help="ID voleb (lze opakovat)")] = None,
    history: Annotated[bool, typer.Option("--history", help="Stáhnout i historické volby pro kalibraci")] = False,
    force: Annotated[bool, typer.Option("--force", help="Ignorovat cache a stáhnout znovu")] = False,
) -> None:
    """Stáhne průzkumy z Wikipedie pro nakonfigurované volby a uloží je do databáze."""
    from polledge.services import update_polls

    s = state().settings
    configured = discover_elections(s, include_history=True)
    current = discover_elections(s, include_history=False)
    ids = election or (sorted(configured) if history else sorted(current))
    if not ids:
        raise PollEdgeError("Nejsou nakonfigurované žádné volby (config/elections/*.yaml)")
    client = WikiClient(s.wikipedia, s.resolve(s.cache_dir))
    failed = 0
    try:
        for eid in ids:
            cfg = get_election(s, eid)
            try:
                with state().session() as session:
                    parsed, inserted, updated = update_polls(session, client, cfg, force=force)
            except PollEdgeError as exc:
                failed += 1
                console.print(f"[red]✗ {cfg.name}:[/red] {exc}")
                continue
            console.print(
                f"[green]✓[/green] {cfg.name}: načteno {parsed} průzkumů, nových {inserted}, aktualizovaných {updated}"
            )
    finally:
        client.close()
    if failed:
        raise typer.Exit(1)


@app.command("model")
@handle_errors
def model_cmd(
    election: Annotated[str, typer.Option("--election", "-e", help="ID voleb")],
    runs: Annotated[Optional[int], typer.Option("--runs", "-n", help="Počet simulací")] = None,
    as_of: Annotated[Optional[datetime], typer.Option("--as-of", help="Datum, ke kterému model počítat", formats=["%Y-%m-%d"])] = None,
    market: Annotated[Optional[list[str]], typer.Option("--market", "-m", help="Další trh, např. share_over:LE_PEN:30")] = None,
    save: Annotated[bool, typer.Option("--save", help="Uložit pravděpodobnosti do deníku (pro Brierovo skóre)")] = False,
) -> None:
    """Spočítá a vypíše pravděpodobnosti z modelu."""
    from polledge.journal.paper import record_snapshot
    from polledge.services import model_markets, params_payload, probabilities_for, run_model_from_db

    s = state().settings
    cfg = get_election(s, election)
    with state().session() as session:
        sim, params = run_model_from_db(session, s, cfg, as_of.date() if as_of else None, runs)
        specs = model_markets(sim, cfg, market)
        probs = probabilities_for(sim, specs)
        print_model(cfg, sim, probs)
        if save:
            n = record_snapshot(session, cfg.id, probs, params_payload(params, sim.average.as_of))
            console.print(f"[green]Uloženo {n} pravděpodobností do deníku.[/green]")


def print_model(cfg: ElectionConfig, sim: SimulationResult, probs: dict[str, dict[str, float]]) -> None:
    avg = sim.average
    days = (cfg.election_date - avg.as_of).days
    console.print(Panel.fit(
        f"[bold]{cfg.name}[/bold]\nStav k {avg.as_of:%d. %m. %Y} · do voleb {days} dní · "
        f"průzkumů v průměru: {avg.n_polls} · efektivní vzorek: {avg.n_eff:,.0f} · simulací: {sim.n_sims:,}".replace(",", " ")
    ))
    two_round = cfg.type == "two_round"
    winner = probs.get("winner", {})
    t = Table(title="Odhad 1. kola" if two_round else "Odhad výsledku", show_lines=False)
    t.add_column("Kandidát / strana")
    t.add_column("Průměr", justify="right")
    t.add_column("80% interval", justify="right")
    t.add_column("± (p.b.)", justify="right")
    if two_round:
        t.add_column("Postup do 2. kola", justify="right")
    t.add_column("Vítězství", justify="right")
    if not two_round:
        t.add_column("≥ 5 %", justify="right")
    lo, hi = np.quantile(sim.shares, [0.1, 0.9], axis=0)
    sd = sim.shares.std(axis=0) * 100
    order = np.argsort(-avg.mean)
    for j in order:
        cid = sim.candidates[j]
        row = [cfg.candidate_name(cid), pct(avg.mean[j]), f"{pct(lo[j])} – {pct(hi[j])}", num(sd[j], 1)]
        if two_round:
            row.append(pct(probs.get(f"qualify_runoff:{cid}", {}).get("yes")) if cid != "OTHER" else "")
        row.append(pct(winner.get(cid)) if cid != "OTHER" else "")
        if not two_round:
            row.append(pct(probs.get(f"threshold:{cid}:5", {}).get("yes")) if cid != "OTHER" else "")
        t.add_row(*row)
    console.print(t)

    if two_round and sim.finalists is not None:
        from polledge.model.markets import MarketSpec, market_probabilities

        pairs = market_probabilities(sim, MarketSpec("finalists"))
        pt = Table(title="Nejpravděpodobnější dvojice ve 2. kole")
        pt.add_column("Dvojice")
        pt.add_column("Pravděpodobnost", justify="right")
        pt.add_column("Zdroj odhadu 2. kola")
        for pair, p in sorted(pairs.items(), key=lambda kv: -kv[1])[:6]:
            source = {"polls": "H2H průzkumy", "fallback": "[yellow]odhad z 1. kola (chybí H2H)[/yellow]"}
            pt.add_row(outcome_label(cfg, pair), pct(p), source.get(sim.runoff_sources.get(pair, ""), ""))
        console.print(pt)

    extra = [m for m in probs if m.split(":")[0] in ("share_over", "threshold") and not m.endswith(":5")]
    for m in extra:
        console.print(f"{market_label(cfg, m)}: " + ", ".join(f"{outcome_label(cfg, o)} {pct(p)}" for o, p in probs[m].items()))

    if avg.house_effects:
        ht = Table(title="House effects (odchylka agentury od průměru, p.b.)")
        ht.add_column("Agentura")
        top = [sim.candidates[j] for j in order[:5] if sim.candidates[j] != "OTHER"]
        for cid in top:
            ht.add_column(cfg.candidate_name(cid), justify="right")
        for pollster, eff in sorted(avg.house_effects.items()):
            ht.add_row(pollster, *[f"{eff.get(c, 0.0):+.1f}".replace(".", ",") for c in top])
        console.print(ht)


@app.command("import-odds")
@handle_errors
def import_odds_cmd(
    path: Annotated[Path, typer.Argument(help="Soubor s kurzy (.csv nebo .yaml)")],
    election: Annotated[Optional[str], typer.Option("--election", "-e", help="Importovat jen tyto volby")] = None,
) -> None:
    """Načte ručně zadané kurzy ze souboru, vypíše marže a uloží kurzy do databáze."""
    from polledge.odds.margin import latest_books

    source = source_for_path(path)
    quotes = source.fetch(election)
    if source.errors:
        console.print(f"[yellow]Přeskočeno {len(source.errors)} chybných záznamů:[/yellow]")
        for err in source.errors:
            console.print(f"  [yellow]•[/yellow] {err}")
    known = discover_elections(state().settings, include_history=True)
    unknown = sorted({q.election_id for q in quotes} - set(known))
    if unknown:
        console.print(f"[yellow]Upozornění: kurzy pro nenakonfigurované volby: {', '.join(unknown)}[/yellow]")
    with state().session() as session:
        inserted, updated = store_quotes(session, quotes, source.name)
    console.print(f"[green]✓[/green] Načteno {len(quotes)} kurzů, nových {inserted}, aktualizovaných {updated}.")

    t = Table(title="Marže sázkových kanceláří (poslední kurzy)")
    for col in ("Volby", "Kancelář", "Trh", "Výsledků", "Součet 1/kurz", "Marže"):
        t.add_column(col, justify="right" if col in ("Výsledků", "Součet 1/kurz", "Marže") else "left")
    for b in latest_books(quotes):
        cfg = known.get(b.election_id)
        label = market_label(cfg, b.market) if cfg else b.market
        note = "" if b.complete else " [yellow](neúplný trh)[/yellow]"
        t.add_row(b.election_id, b.bookmaker, label + note, str(len(b.odds)), num(1 + b.margin, 3), pct(b.margin))
    console.print(t)


@app.command("signals")
@handle_errors
def signals_cmd(
    election: Annotated[str, typer.Option("--election", "-e", help="ID voleb")],
    record: Annotated[bool, typer.Option("--record", help="Uložit signály jako virtuální sázky do deníku")] = False,
    runs: Annotated[Optional[int], typer.Option("--runs", "-n", help="Počet simulací")] = None,
    only_signals: Annotated[bool, typer.Option("--only-signals", help="Vypsat jen řádky se signálem")] = False,
) -> None:
    """Porovná model s nejlepšími kurzy a vypíše value signály, arbitráže a doporučené vklady."""
    from polledge.journal.paper import record_signals, record_snapshot
    from polledge.services import build_comparisons, market_fair_probs, params_payload, run_model_from_db

    s = state().settings
    cfg = get_election(s, election)
    with state().session() as session:
        quotes = load_quotes(session, cfg.id)
        if not quotes:
            raise PollEdgeError(f"Pro volby {cfg.id} nejsou v databázi žádné kurzy. Použij `import-odds`.")
        sim, params = run_model_from_db(session, s, cfg, None, runs)
        comparisons = build_comparisons(sim, quotes, s)

        sig = s.signals
        console.print(Panel.fit(
            f"[bold]{cfg.name}[/bold] – model k {sim.average.as_of:%d. %m. %Y}\n"
            f"Práh edge {pct(sig.edge_threshold)} · Kelly {num(sig.kelly_fraction, 2)} · "
            f"bankroll {money(sig.bankroll)}"
        ))
        for comp in comparisons:
            t = Table(title=market_label(cfg, comp.market), title_justify="left")
            for col in ("Výsledek", "Model", "Trh (bez marže)", "Nejlepší kurz", "Kancelář", "Edge", "Vklad", ""):
                t.add_column(col, justify="left" if col in ("Výsledek", "Kancelář", "") else "right")
            rows = sorted(comp.outcomes, key=lambda o: -o.p_model)
            for o in rows:
                if o.best_odds is None or (only_signals and not o.is_signal):
                    continue
                t.add_row(
                    outcome_label(cfg, o.outcome), pct(o.p_model), pct(o.p_market), num(o.best_odds),
                    o.best_bookmaker or "", signed_pct(o.edge), money(o.stake) if o.stake > 0 else "–",
                    "[bold green]SIGNÁL[/bold green]" if o.is_signal else "",
                )
            if only_signals and not comp.signals:
                continue
            console.print(t)
            margins = ", ".join(f"{bm} {pct(m)}" for bm, m in comp.margins.items())
            console.print(f"  Marže: {margins}")
            if comp.arbitrage_sum is not None:
                if comp.is_arbitrage:
                    console.print(f"  [bold magenta]ARBITRÁŽ:[/bold magenta] součet 1/nejlepší kurz = {num(comp.arbitrage_sum, 4)} < 1")
                else:
                    console.print(f"  Součet 1/nejlepší kurz: {num(comp.arbitrage_sum, 4)} (bez arbitráže)")
            console.print()

        n_signals = sum(len(c.signals) for c in comparisons)
        n_arb = sum(c.is_arbitrage for c in comparisons)
        console.print(f"[bold]Signálů: {n_signals}, arbitráží: {n_arb}[/bold]")
        if record:
            created = record_signals(session, cfg.id, comparisons)
            probs = {c.market: {o.outcome: o.p_model for o in c.outcomes} for c in comparisons}
            record_snapshot(session, cfg.id, probs, params_payload(params, sim.average.as_of), market_fair_probs(comparisons))
            console.print(f"[green]Do deníku uloženo {created} nových virtuálních sázek.[/green]")
        console.print("[dim]Pouze analýza – aplikace nepodává žádné sázky.[/dim]")


@app.command("journal")
@handle_errors
def journal_cmd(election: Annotated[str, typer.Option("--election", "-e", help="ID voleb")]) -> None:
    """Vypíše virtuální sázky z deníku."""
    from sqlalchemy import select

    cfg = get_election(state().settings, election)
    with state().session() as session:
        bets = session.scalars(select(PaperBetRow).where(PaperBetRow.election_id == cfg.id).order_by(PaperBetRow.created_at)).all()
    if not bets:
        console.print("Deník je prázdný.")
        return
    t = Table(title=f"Deník – {cfg.name}")
    for col in ("Čas", "Trh", "Výsledek", "Kancelář", "Kurz", "Model", "Edge", "Vklad", "Stav", "Zisk"):
        t.add_column(col)
    status = {"open": "otevřená", "won": "[green]výhra[/green]", "lost": "[red]prohra[/red]", "void": "neplatná"}
    for b in bets:
        t.add_row(f"{b.created_at:%d.%m.%Y %H:%M}", market_label(cfg, b.market), outcome_label(cfg, b.outcome),
                  b.bookmaker, num(b.odds), pct(b.p_model), signed_pct(b.edge), money(b.stake),
                  status.get(b.status, b.status), money(b.profit) if b.profit is not None else "–")
    console.print(t)


@app.command("settle")
@handle_errors
def settle_cmd(
    election: Annotated[str, typer.Option("--election", "-e", help="ID voleb")],
    result: Annotated[Optional[Path], typer.Option("--result", "-r", help="YAML s výsledkem (first_round, runoff)")] = None,
) -> None:
    """Zadá skutečný výsledek voleb a vyhodnotí deník: zisk/ztráta, ROI, Brierovo skóre."""
    from pydantic import ValidationError

    from polledge.journal.paper import settle

    cfg = get_election(state().settings, election)
    if result is not None:
        try:
            res = ElectionResult(**read_yaml(result))
        except ValidationError as exc:
            raise PollEdgeError(f"Neplatný soubor s výsledkem {result}: {exc}") from exc
    elif cfg.result is not None:
        res = cfg.result
    else:
        raise PollEdgeError("Zadej výsledek přes --result soubor.yaml (klíče first_round a případně runoff)")
    unknown = (set(res.first_round) | set(res.runoff)) - set(cfg.candidates) - {"OTHER"}
    if unknown:
        raise PollEdgeError(f"Výsledek obsahuje neznámé kandidáty: {', '.join(sorted(unknown))}")

    with state().session() as session:
        report = settle(session, cfg.id, res)
    t = Table(title=f"Vyhodnocení – {cfg.name}", show_header=False)
    t.add_row("Sázek celkem", str(report.n_bets))
    t.add_row("Výhry / prohry / neplatné", f"{report.n_won} / {report.n_lost} / {report.n_void}")
    t.add_row("Vsazeno", money(report.staked))
    t.add_row("Zisk / ztráta", money(report.profit))
    t.add_row("ROI", pct(report.roi))
    t.add_row("Brierovo skóre modelu (všechny snapshoty)", num(report.brier_model, 4) + f" (n={report.n_scored})")
    t.add_row("Brierovo skóre modelu (řádky s kurzem)", num(report.brier_model_same_rows, 4))
    t.add_row("Brierovo skóre trhu (bez marže)", num(report.brier_market, 4) + f" (n={report.n_scored_market})")
    console.print(t)
    if report.brier_market is not None and report.brier_model_same_rows is not None:
        better = "model" if report.brier_model_same_rows < report.brier_market else "trh"
        console.print(f"Lépe kalibrovaný byl: [bold]{better}[/bold] (nižší Brierovo skóre je lepší).")


@app.command("calibrate")
@handle_errors
def calibrate_cmd(
    election: Annotated[Optional[list[str]], typer.Option("--election", "-e", help="Historické volby (výchozí: všechny s výsledkem)")] = None,
    horizons: Annotated[str, typer.Option("--horizons", help="Dny před volbami, oddělené čárkou")] = "90,60,30,14,7,1",
    sims: Annotated[int, typer.Option("--sims", help="Simulací na jeden bod mřížky")] = 2000,
    write: Annotated[bool, typer.Option("--write/--no-write", help="Uložit nejlepší parametry do calibrated.yaml")] = True,
) -> None:
    """Nakalibruje parametry modelu na minulých volbách (grid search, leave-one-election-out)."""
    from polledge.calibration.tune import grid_edges, tune, write_calibrated

    s = state().settings
    hz = _parse_horizons(horizons)
    data = _history_data(election)
    console.print(f"Kalibruji na {len(data)} volbách ({', '.join(c.id for c, _ in data)}), horizonty {list(hz)} dní…")
    base = s.uncalibrated_model or s.model
    with console.status("Probíhá grid search…"):
        res = tune(data, base, hz, n_sims=sims)

    t = Table(title="Nejlepší kombinace parametrů (průměrné CRPS podílů, p.b.; nižší = lepší)")
    for col in ("Poločas (dny)", "House effects (min. průzkumů)", "σ systematická", "σ horizont / √den", "CRPS"):
        t.add_column(col, justify="right")
    for combo, score in res.table[:8]:
        t.add_row(str(combo["half_life_days"]), "vyp." if combo["house"] is None else str(combo["house"]),
                  num(combo["sigma_sys_pp"], 1), num(combo["sigma_horizon_pp_per_sqrt_day"], 2), num(score, 3))
    console.print(t)

    lt = Table(title="Leave-one-election-out (parametry naladěné bez dané volby vs. settings.yaml)")
    for col in ("Volby", "CRPS naladěné", "CRPS výchozí"):
        lt.add_column(col, justify="right" if col != "Volby" else "left")
    for eid, row in res.loeo.items():
        lt.add_row(eid, num(row["tuned"], 3), num(row["default"], 3))
    console.print(lt)
    console.print(f"Pokrytí 80% intervalů s nejlepšími parametry: {pct(res.coverage80)} (ideál ~80 %)")
    if res.sigma_runoff_pp is not None:
        console.print(f"Odhad chyby H2H průzkumů 2. kola: {num(res.sigma_runoff_pp, 1)} p.b. (z velmi malého vzorku)")
    console.print(f"Výchozí parametry: CRPS {num(res.default_score, 3)} → naladěné: {num(res.best_score, 3)}")
    edges = grid_edges(res.best)
    if edges:
        console.print(f"[yellow]Optimum leží na okraji mřížky u: {', '.join(edges)} – zvaž rozšíření DEFAULT_GRID.[/yellow]")

    if write:
        path = s.resolve(s.calibrated_file)
        written = write_calibrated(path, res, base, [c.id for c, _ in data])
        console.print(f"[green]Parametry uloženy do {path}:[/green] {written}")
        console.print("[dim]Smaž soubor, pokud se chceš vrátit k hodnotám ze settings.yaml.[/dim]")


@app.command("backtest")
@handle_errors
def backtest_cmd(
    election: Annotated[str, typer.Option("--election", "-e", help="Historické volby s výsledkem")],
    odds: Annotated[Optional[Path], typer.Option("--odds", help="Historické kurzy (CSV/YAML) pro simulaci sázení")] = None,
    horizons: Annotated[str, typer.Option("--horizons", help="Dny před volbami, oddělené čárkou")] = "90,60,30,14,7,1",
) -> None:
    """Vyhodnotí model na minulých volbách; s --odds přehraje i virtuální sázení."""
    from polledge.calibration.backtest import backtest_with_odds, evaluate_election

    s = state().settings
    (cfg, polls), = _history_data([election])
    params = model_params_for(s, cfg)
    scores = evaluate_election(cfg, polls, params, _parse_horizons(horizons))
    t = Table(title=f"Backtest – {cfg.name}")
    for col in ("Dní před", "Průzkumů", "MAE průměru (p.b.)", "CRPS (p.b.)", "Pokrytí 80%", "P(skut. vítěz)", "Brier vítěz", "Chyba H2H (p.b.)"):
        t.add_column(col, justify="right")
    for sc in scores:
        t.add_row(str(sc.horizon), str(sc.n_polls), num(sc.mae_pp), num(sc.crps_pp), pct(sc.coverage80, 0),
                  pct(sc.p_actual_winner), num(sc.winner_brier, 3),
                  f"{sc.runoff_error_pp:+.1f}".replace(".", ",") if sc.runoff_error_pp is not None else "–")
    console.print(t)

    if odds is not None:
        quotes = [q for q in source_for_path(odds).fetch(cfg.id)]
        bt = backtest_with_odds(cfg, polls, quotes, s, params)
        r = Table(title="Simulované sázení na historických kurzech", show_header=False)
        r.add_row("Dnů s kurzy", str(bt.n_snapshots))
        r.add_row("Sázek", str(bt.n_bets))
        r.add_row("Vsazeno", money(bt.staked))
        r.add_row("Zisk / ztráta", money(bt.profit))
        r.add_row("ROI", pct(bt.roi))
        r.add_row("Brier model / trh", f"{num(bt.brier_model, 4)} / {num(bt.brier_market, 4)} (n={bt.n_scored})")
        console.print(r)


def _parse_horizons(text: str) -> tuple[int, ...]:
    try:
        values = tuple(int(x) for x in text.split(",") if x.strip())
    except ValueError:
        raise PollEdgeError(f"Neplatné horizonty '{text}', očekávám např. 60,30,7") from None
    if not values or min(values) < 1:
        raise PollEdgeError("Horizonty musí být kladná celá čísla")
    return values


def _history_data(ids: list[str] | None) -> list[tuple[ElectionConfig, list]]:
    s = state().settings
    all_cfg = discover_elections(s, include_history=True)
    if ids:
        cfgs = [get_election(s, i) for i in ids]
    else:
        cfgs = [c for c in all_cfg.values() if c.result is not None and c.result.first_round]
    if not cfgs:
        raise PollEdgeError("Nejsou žádné historické volby s výsledkem (config/elections/history/*.yaml)")
    data = []
    with state().session() as session:
        for cfg in cfgs:
            if cfg.result is None:
                raise PollEdgeError(f"Volby {cfg.id} nemají v konfiguraci výsledek")
            polls = load_polls(session, cfg.id)
            if not polls:
                raise PollEdgeError(f"Pro volby {cfg.id} nejsou průzkumy v databázi. Spusť `polledge update-polls -e {cfg.id}`.")
            data.append((cfg, polls))
    return data


if __name__ == "__main__":
    app()
