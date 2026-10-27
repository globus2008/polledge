# PollEdge

Analýza volebních sázek. PollEdge spočítá z předvolebních průzkumů pravděpodobnosti výsledků, porovná je s kurzy sázkových kanceláří a hledá rozdíly (tzv. value bety).

> **Aplikace jen analyzuje a doporučuje. Nikdy sama nesází** a nepřihlašuje se k žádné sázkové kanceláři. Doporučené vklady slouží pro virtuální deník (paper trading).

Fáze 1 obsahuje:

- sběr průzkumů z Wikipedie,
- model s Monte Carlo simulací,
- ruční zadávání kurzů,
- signály, Kellyho kritérium a detekci arbitráže,
- deník a vyhodnocení,
- kalibraci modelu na minulých volbách.

Scrapery sázkovek a notifikace přijdou později. Připravená jsou pro ně rozhraní `OddsSource` a `Notifier`.

## Instalace

Je potřeba Python 3.11 nebo novější.

```powershell
python -m venv .venv
.venv\Scripts\activate          # Linux/macOS: source .venv/bin/activate
pip install -e ".[dev]"
```

Po instalaci je k dispozici příkaz `polledge`. Spouštěj ho z kořene projektu, kde leží složka `config/`.

**Před prvním stažením** doplň kontakt do `wikipedia.user_agent` v `config/settings.yaml`. Pravidla Wikipedie vyžadují, aby šlo robota identifikovat.

## Rychlý start

```powershell
polledge update-polls                                   # stáhne průzkumy aktuálních voleb
polledge model -e fr_pres_2027                          # pravděpodobnosti z modelu
polledge model -e fr_pres_2027 -m share_over:LE_PEN:30  # navíc vlastní trh
polledge import-odds data/odds/fr_pres_2027_sample.csv  # načte kurzy a vypíše marže
polledge signals -e fr_pres_2027                        # porovnání, signály, arbitráže
polledge signals -e fr_pres_2027 --record               # signály uloží jako virtuální sázky
polledge journal -e fr_pres_2027                        # obsah deníku
polledge settle -e fr_pres_2027 -r vysledek.yaml        # po volbách: vyhodnocení
```

Pozor: `data/odds/fr_pres_2027_sample.csv` obsahuje **smyšlené ukázkové kurzy**. Před skutečnou analýzou je nahraď kurzy, které sám opíšeš z nabídky kanceláří.

Globální volby: `-c/--config cesta/settings.yaml` (jiná konfigurace) a `-v/--verbose` (podrobné logování). Log se ukládá do `logs/polledge.log`.

### Příkazy

| Příkaz | Co dělá |
|---|---|
| `update-polls [-e ID] [--history] [--force]` | Stáhne stránky přes MediaWiki API, naparsuje tabulky a uloží je do SQLite s deduplikací. Stránky se cachují v `data/cache/` (výchozí platnost 6 h). `--force` cache obejde, `--history` stáhne i historické volby. |
| `model -e ID [-n RUNS] [--as-of YYYY-MM-DD] [-m TRH] [--save]` | Spočítá vážený průměr a Monte Carlo simulaci a vypíše tabulky. `--save` uloží pravděpodobnosti do deníku pro pozdější Brierovo skóre. |
| `import-odds SOUBOR [-e ID]` | Načte kurzy z CSV nebo YAML, uloží je do DB a vypíše marži každé kanceláře. |
| `signals -e ID [--record] [--only-signals]` | Pro každý výsledek najde nejlepší kurz, spočítá edge a vklad podle Kellyho a ukáže arbitráže. |
| `journal -e ID` | Vypíše virtuální sázky. |
| `settle -e ID [-r vysledek.yaml]` | Zapíše skutečný výsledek a vyhodnotí sázky: P/L, ROI a Brierovo skóre modelu a trhu. |
| `calibrate [-e ID ...] [--horizons 90,60,30,14,7,1]` | Nakalibruje parametry modelu na minulých volbách. |
| `backtest -e ID [--odds SOUBOR]` | Ukáže přesnost modelu na minulých volbách. S `--odds` přehraje i virtuální sázení. |

## Konfigurace

### `config/settings.yaml`

Obsahuje globální nastavení: cesty, Wikipedii (User-Agent, cache, timeouty), parametry modelu (`model:`) a signálů (`signals:`). Každý parametr je v souboru okomentovaný. Žádný parametr modelu není zadaný natvrdo v kódu.

Pokud existuje `config/calibrated.yaml` (vytváří ho `polledge calibrate`), jeho hodnoty mají přednost před `model:` v `settings.yaml`. K původním hodnotám se vrátíš tak, že soubor smažeš.

### Přidání nových voleb

Vytvoř soubor `config/elections/<id>.yaml`. Historické volby s výsledkem patří do `config/elections/history/`.

```yaml
id: cz_parl_2029
name: "Česko – sněmovní volby 2029"
type: single_round            # nebo two_round (prezidentské se 2. kolem)
election_date: 2029-10-06
wiki_page: "Opinion polling for the 2029 Czech parliamentary election"
default_sample_size: 1000     # když tabulka velikost vzorku neuvádí

candidates:                   # ID -> jméno; aliasy podle hlaviček sloupců
  ANO: {name: "ANO"}
  SPOLU: {name: "Spolu", aliases: ["SPOLU"]}
  # nenamapované sloupce (kromě ignorovaných) se sečtou do "Ostatní"

first_round:
  tables:                     # výběr tabulek podle nadpisů sekcí nad nimi
    - heading: ["Opinion polls"]          # všechny řetězce musí být v cestě nadpisů
      exclude_heading: ["Seat projections"]
    # - index: 3                          # nebo pořadí wikitable na stránce
  columns:                    # názvy sloupců (shoda začátku textu, bez diakritiky)
    pollster: "Polling firm"
    fieldwork: "Fieldwork date"
    sample_size: "Sample size"
    ignore: ["Lead", "Abs", "Turnout"]
  scenario:                   # u hypotetických sestav kandidátů
    require: []               # řádek musí obsahovat všechny tyto kandidáty
    exclude: []               # ...a žádného z těchto
  skip_rows: ["election", "result", "exit poll"]   # řádky s výsledkem a exit polly

runoff:                       # jen pro two_round: H2H tabulky, jedna dvojice na tabulku
  tables:
    - heading: ["Second round"]

model:                        # volitelné přepsání parametrů jen pro tyto volby
  half_life_days: 21

result:                       # jen u historických voleb (pro kalibraci a backtest)
  first_round: {ANO: 34.5, SPOLU: 23.4}
  runoff: {}
```

Postup:

1. Spusť `polledge update-polls -e <id> -v`. Log ukáže, které tabulky parser našel, kolik řádků načetl a kolik přeskočil.
2. Když se struktura stránky změní (chybí sloupec, tabulka se nenajde), příkaz skončí srozumitelnou chybou, v níž vypíše nalezené sloupce. Podle nich uprav YAML.
3. Jméno kandidáta se porovnává s hlavičkou sloupce bez stranické zkratky („Le Pen RN“ → „Le Pen“, „Mélenchon (LFI)“ → „Mélenchon“). Příjmení se jako alias přidává automaticky. Jiný zápis doplň do `aliases`.

### Formát kurzů

CSV (oddělovač `,` nebo `;`, řádky s `#` se ignorují):

```csv
election_id,bookmaker,market,outcome,odds,timestamp
fr_pres_2027,Tipsport,winner,LE_PEN,1.45,2026-10-27 10:00
fr_pres_2027,Fortuna,qualify_runoff:PHILIPPE,yes,2.05,2026-10-27 09:30
```

YAML lze psát po jednotlivých záznamech, nebo seskupeně:

```yaml
- election_id: fr_pres_2027
  bookmaker: Tipsport
  timestamp: 2026-10-27 10:00
  markets:
    winner: {LE_PEN: 1.45, PHILIPPE: 4.2}
    "qualify_runoff:PHILIPPE": {"yes": 1.8, "no": 1.92}
```

Typy trhů (`market`) a jejich výsledky (`outcome`):

| Trh | Výsledky | Význam |
|---|---|---|
| `winner` | ID kandidáta | celkový vítěz voleb (u dvoukolových voleb vítěz 2. kola) |
| `first_round_winner` | ID kandidáta | nejvíc hlasů v 1. kole |
| `finalists` | `A\|B` (ID seřazená abecedně) | dvojice ve 2. kole |
| `qualify_runoff:ID` | `yes` / `no` | postup do 2. kola |
| `threshold:ID:5` | `yes` / `no` | strana získá alespoň 5 % |
| `share_over:ID:30` | `over` / `under` | podíl nad, nebo pod 30 % |

Kurzy jsou desetinné. Platí vždy nejnovější kurz dané kanceláře pro daný výsledek.

## Jak model funguje

1. **Vážený průměr průzkumů.** Váha průzkumu je `0.5^(stáří/poločas) · √n`. Průzkumy starší než `max_poll_age_days` se ignorují. Jeden průzkum s více scénáři (řádky) se zprůměruje do jednoho záznamu. Podíly se přepočítají na 100 % včetně „Ostatní“.
2. **House effects.** Agentura s alespoň `house_min_polls` průzkumy dostane korekci o svou váženou průměrnou odchylku od celkového průměru. Korekci lze zmírnit shrinkage faktorem `k/(k+m)`. Průměr se pak spočítá znovu.
3. **Nejistota.** Pro každou stranu se počítá σ² = σ_sys² + (b·√dní do voleb)² + p(1−p)/n_eff. n_eff je efektivní vzorek váženého průměru. Systematická chyba σ_sys (výchozí 2,5 p.b.) se u stran pod `small_party_ref_pct` zmenšuje úměrně √(podíl/10 %). Bez toho by strana s 0,5 % měla nesmyslnou nejistotu ±2,5 p.b.
4. **Monte Carlo, logistic-normal.** K logaritmům podílů se přičte normální šum a výsledek se převede zpět přes softmax. Každá simulace tak dává přesně 100 % a žádný podíl není záporný. Rozptyl v log prostoru se iterativně doladí, aby skutečná směrodatná odchylka odpovídala σ z bodu 3.
   - *Proč ne Dirichletovo rozdělení:* má jediný parametr koncentrace, takže nejistota každé strany je pevně svázaná s jejím podílem. Nelze nastavit „2,5 p.b. na stranu“ a malé strany by měly nejistotu příliš úzkou.
5. **Dvoukolové volby.** V každé simulaci postupují dva nejsilnější kandidáti („Ostatní“ nikdy nepostupují). Výsledek 2. kola vychází z váženého průměru H2H průzkumů dané dvojice se směrodatnou odchylkou `sigma_runoff_pp` (plus horizont a výběrová chyba). Když pro dvojici H2H průzkumy chybí, použije se poměr podílů z 1. kola s vyšší nejistotou (`sigma_runoff_fallback_pp`) a model vypíše varování. Kdo má v 1. kole nadpoloviční většinu, vyhrává rovnou.
6. **Pravděpodobnosti trhů** jsou podíly simulací, v nichž daný výsledek nastal.

### Kurzy, signály, Kelly

- Implikovaná pravděpodobnost = 1/kurz. Marže = Σ(1/kurz) − 1. Pravděpodobnost bez marže = (1/kurz)/Σ, počítá se v rámci jednoho trhu jedné kanceláře.
- Trh, kde kancelář nevypsala všechny výsledky, se označí jako neúplný a marže se u něj neodstraňuje. Binární trh potřebuje obě strany. Vícevýsledkový trh musí mít Σ(1/kurz) ≥ 1 a chybějící výsledky smí mít podle modelu nejvýš 2 %.
- **Edge** = p_model × nejlepší kurz − 1. **Signál** vznikne, když edge > `edge_threshold` (výchozí 5 %).
- **Arbitráž:** součet 1/nejlepší kurz přes všechny výsledky trhu (napříč kancelářemi) je menší než 1.
- **Vklad:** f* = (p·b − (1−p))/b, kde b = kurz − 1. Vklad = max(0, f*) × `kelly_fraction` (výchozí 1/4) × `bankroll`, s volitelným stropem `max_stake_pct`.

### Deník a vyhodnocení

`signals --record` uloží každý signál jako virtuální sázku. Stejnou otevřenou sázku za stejný kurz neukládá dvakrát. Zároveň uloží snapshot pravděpodobností modelu i trhu (bez marže).

`settle` zapíše výsledek a vyhodnotí sázky. Vypíše zisk/ztrátu a ROI a porovná Brierovo skóre modelu a trhu na stejných řádcích. Soubor s výsledkem:

```yaml
first_round: {LE_PEN: 31.2, PHILIPPE: 19.5, MELENCHON: 18.0, GLUCKSMANN: 11.0}
runoff: {LE_PEN: 48.6, PHILIPPE: 51.4}
```

## Kalibrace na historických datech

Model má jen několik parametrů: poločas, house effects, σ_sys, horizontovou σ a σ 2. kola. Proto se netrénuje v ML smyslu, ale **kalibruje** na minulých volbách, u kterých známe průzkumy i výsledek:

```powershell
polledge update-polls --history   # stáhne historické volby z config/elections/history/
polledge calibrate                 # grid search + leave-one-election-out, zapíše calibrated.yaml
polledge backtest -e fr_pres_2022  # přesnost modelu 90…1 den před volbami
```

- **Bez úniku budoucích informací:** pro každý horizont (90, 60, 30, 14, 7 a 1 den před volbami) model vidí jen průzkumy ukončené před tímto dnem.
- **Metrika:** CRPS simulovaných podílů v p.b. (správné skórovací pravidlo, odměňuje přesnost i dobře nastavenou nejistotu). Výpis ukazuje i pokrytí 80% intervalů, Brierovo skóre a log-loss vítěze.
- **Leave-one-election-out:** parametry se naladí bez dané volby a otestují se na ní. Výsledek se porovná s parametry ze `settings.yaml`.
- **σ 2. kola** se odhaduje z chyb H2H průzkumů u skutečných finalistů. Je to velmi malý vzorek, proto se hodnota omezuje na rozsah 2–8 p.b.
- Součástí projektu jsou historické volby FR 2017, FR 2022, DE 2021 a DE 2025. Další přidáš jako YAML s polem `result`.

**Historické kurzy.** České kanceláře historii kurzů nezveřejňují a veřejných archivů politických trhů je málo. Pokud nějaké kurzy máš (vlastní záznamy, export z Betfair Historical Data a podobně), ulož je ve stejném CSV formátu s časem. Příkaz `polledge backtest -e ID --odds soubor.csv` pak přehraje virtuální sázení: každý den s kurzy použije jen průzkumy a kurzy známé v tu chvíli a spočítá ROI a Brierovo skóre modelu a trhu. Bez kurzů se kalibruje jen model, a to je pro value bety podstatné: edge má smysl jen tehdy, když je model dobře kalibrovaný.

## Omezení modelu

- **Průzkumy se mohou mýlit všechny stejným směrem.** Model to pokrývá jen přes σ_sys a horizontovou σ. Nemodeluje korelované chyby mezi stranami (např. podcenění celého bloku), změny v kampani ani odstoupení kandidátů.
- **House effects** se měří vůči průměru, který se v čase mění. Agentura, která publikovala hlavně v období výkyvu, může dostat zkreslenou korekci. Agentury s málo průzkumy se nekorigují.
- **Scénáře:** u francouzských voleb záleží na tom, který scénář (sestavu kandidátů) vybereš v YAML. Když se kandidáti změní, je potřeba konfiguraci upravit.
- **2. kolo** je simulované nezávisle na 1. kole. Bez H2H průzkumů je odhad hrubý.
- **Kalibrace** stojí na malém počtu voleb z různých zemí a systémů. Historické tabulky na Wikipedii se navíc zpětně upravují. Kalibrované hodnoty ber jako řád nejistoty, ne jako přesné číslo. Pokud optimum vyjde na okraji mřížky, příkaz `calibrate` na to upozorní.
- **Kurzy** se zadávají ručně, takže na jejich aktuálnosti a správném přiřazení k trhu záleží.
- Sázky na politické události mohou mít u kanceláří zvláštní pravidla vypořádání (oficiální výsledek, termíny, neplatnost při odstoupení). Model je nezohledňuje.

## Struktura projektu

```
config/settings.yaml            globální nastavení
config/elections/*.yaml         aktuální volby
config/elections/history/*.yaml historické volby s výsledkem (kalibrace)
src/polledge/polls/             MediaWiki klient s cache, parser tabulek, čištění, DB
src/polledge/model/             vážený průměr, house effects, simulace, trhy
src/polledge/odds/              OddsSource (rozhraní), CSV/YAML zdroje, marže
src/polledge/signals/           edge, arbitráž, Kelly
src/polledge/journal/           deník, vyhodnocení, Brierovo skóre
src/polledge/calibration/       backtest, metriky, grid search
src/polledge/notify/            rozhraní Notifier (pro fázi 2)
tests/                          pytest (HTML fixture z Wikipedie v tests/fixtures)
```

### Rozšíření o scraper kanceláře (fáze 2)

```python
from polledge.odds.base import OddsQuote, OddsSource

class TipsportSource(OddsSource):
    name = "tipsport"

    def fetch(self, election_id=None) -> list[OddsQuote]:
        ...  # stáhni nabídku a převeď ji na OddsQuote s ID trhů podle tabulky výše
```

Zbytek aplikace (ukládání, marže, signály, deník) funguje beze změny.

## Testy

```powershell
pytest
```

Testy pokrývají parsování dat a procent, odstranění marže, edge a Kellyho kritérium, arbitráž, součet simulovaných podílů na 100 %, parser nad skutečnou HTML fixture, chování při výpadku sítě, deník a vyhodnocení a kalibraci (včetně kontroly, že model nevidí budoucí průzkumy).

Data z Wikipedie jsou pod licencí CC BY-SA.
