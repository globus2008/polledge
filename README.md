# PollEdge

Analýza volebních sázek. PollEdge spočítá z předvolebních průzkumů pravděpodobnosti výsledků, porovná je s kurzy sázkových kanceláří a hledá rozdíly (tzv. value bety).

> **Aplikace jen analyzuje a doporučuje. Nikdy sama nesází** a nepřihlašuje se k žádné sázkové kanceláři. Doporučené vklady slouží pro virtuální deník (paper trading).

Co aplikace umí:

- sběr průzkumů z Wikipedie,
- model s Monte Carlo simulací,
- kurzy zadané ručně (CSV/YAML) nebo **stažené z veřejných stránek kanceláří**,
- signály, Kellyho kritérium a detekci arbitráže,
- deník a vyhodnocení,
- kalibraci modelu na minulých volbách,
- **pravidelné hlídání** (`watch`) s notifikacemi do konzole nebo e-mailem,
- **webové rozhraní** (Streamlit) lokálně nebo na internetu, chráněné heslem.

Všechna data (průzkumy, kurzy, deník, cache stažených stránek, nakalibrované parametry) jsou v databázi: lokálně v SQLite, na internetu v PostgreSQL. V souborech zůstává jen konfigurace (`config/*.yaml`), kterou verzuje git.

## Webová aplikace

### Na vlastním počítači

Po instalaci (níže) stačí dvojklik na **`PollEdge.bat`**. Aplikace se otevře v prohlížeči na `http://localhost:8501` a je dostupná jen z tohoto počítače. S lokální databází SQLite nepotřebuje heslo.

V aplikaci jsou stránky:

- **Model**: odhad výsledku, postup do 2. kola, vítěz, vývoj průzkumů v grafu, house effects, pravděpodobnost libovolného trhu,
- **Signály**: porovnání s kurzy, value signály, arbitráže, zápis do deníku, stažení kurzů z kanceláří,
- **Kurzy**: ruční zadání formulářem, import CSV/YAML, přehled marží, mazání chybných kurzů,
- **Deník**: virtuální sázky a vyhodnocení po zadání výsledku voleb,
- **Kalibrace a backtest**.

### Na internetu (zdarma): Neon + Streamlit Community Cloud + GitHub Actions

1. **Databáze:** na [neon.tech](https://neon.tech) založ projekt (free tier) a zkopíruj *connection string* (`postgresql://…?sslmode=require`).
2. **Přenos dat** z lokální databáze do Neonu (jednou):
   ```powershell
   polledge migrate-db --target "postgresql://…"
   ```
3. **Aplikace:** na [share.streamlit.io](https://share.streamlit.io) se přihlas přes GitHub a vytvoř aplikaci z repozitáře `globus2008/polledge`, větev `main`, soubor `streamlit_app.py`. V *Advanced settings → Secrets* vlož:
   ```toml
   APP_PASSWORD = "silné-heslo"
   POLLEDGE_DATABASE_URL = "postgresql://…"
   ```
   Bez `APP_PASSWORD` aplikace s hostovanou databází odmítne běžet. Ve Streamlit Cloud lze navíc v *Sharing* nastavit, že aplikaci uvidí jen pozvané účty.
4. **Hlídání každou hodinu:** v repozitáři na GitHubu *Settings → Secrets and variables → Actions* přidej secret `POLLEDGE_DATABASE_URL` (stejný řetězec). Workflow `.github/workflows/watch.yml` pak každou hodinu stáhne průzkumy a kurzy, přepočítá model a nové signály zapíše do deníku. Ručně ho spustíš v záložce *Actions → watch → Run workflow*. Pro e-mailové notifikace vyplň `notify.email` v `settings.yaml` a přidej secret `POLLEDGE_SMTP_PASSWORD`.

5. **Kurzy z kanceláří, které blokují cloud (Betano), stahuje NAS doma.** Viz další oddíl. Ve Streamlit Cloud přidej do Secrets `POLLEDGE_SKIP_SOURCES = "betano"`, aby se o Betano zbytečně nepokoušel. GitHub Actions to má nastavené ve workflow.

### Hlídání z domácí sítě: Synology NAS (Container Manager)

Betano (a možná i další kanceláře) odmítá přístup ze serverů v cloudu, ale z domácí sítě funguje. NAS jen sám navazuje odchozí spojení (Wikipedie, kanceláře, databáze), takže **veřejná IP adresa ani otevírání portů nejsou potřeba**.

1. V DSM otevři **Container Manager** (Centrum balíčků; vyžaduje model s podporou Container Manageru).
2. **Projekt → Vytvořit**, zvol libovolnou složku (např. `/docker/polledge`) a jako zdroj *Vytvořit docker-compose.yml*. Vlož obsah souboru [`docker-compose.yml`](docker-compose.yml).
3. Ve stejné složce vytvoř soubor `.env` s jedním řádkem (stejná hodnota jako ve Streamlit Cloud):
   ```
   POLLEDGE_DATABASE_URL=postgresql://…
   ```
4. Projekt spusť. Kontejner `polledge-watch` každou hodinu stáhne průzkumy, kurzy z Betana a Polymarketu, přepočítá model a nové signály zapíše do deníku. Výpis jednotlivých běhů je v Container Manageru v *Kontejner → polledge-watch → Protokol*.
5. **Aktualizace:** obraz `ghcr.io/globus2008/polledge:latest` se sestavuje automaticky po každé změně kódu. V Container Manageru u projektu zvol *Akce → Sestavit* (stáhne nejnovější obraz a kontejner restartuje).

Poznámky:
- GitHub u veřejných repozitářů po 60 dnech bez commitu plánované workflow vypne; znovu je zapneš v záložce *Actions*.
- Kanceláře mohou blokovat přístup z datacenter (GitHub Actions, Streamlit Cloud) jinak než z domácí sítě. Případnou chybu `watch` zapíše do logu běhu.
- Databázi lze kdykoli zvolit i pro CLI: `$env:POLLEDGE_DATABASE_URL = "postgresql://…"`.

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
polledge fetch-odds                                     # stáhne kurzy z nakonfigurovaných kanceláří
polledge signals -e fr_pres_2027                        # porovnání, signály, arbitráže
polledge signals -e fr_pres_2027 --record               # signály uloží jako virtuální sázky
polledge journal -e fr_pres_2027                        # obsah deníku
polledge settle -e fr_pres_2027 -r vysledek.yaml        # po volbách: vyhodnocení
polledge watch                                          # průzkumy + kurzy + signály + notifikace v jednom běhu
```

Pozor: `data/odds/fr_pres_2027_sample.csv` obsahuje **smyšlené ukázkové kurzy**. Před skutečnou analýzou je nahraď kurzy, které sám opíšeš z nabídky kanceláří.

Globální volby: `-c/--config cesta/settings.yaml` (jiná konfigurace) a `-v/--verbose` (podrobné logování). Log se ukládá do `logs/polledge.log`.

### Příkazy

| Příkaz | Co dělá |
|---|---|
| `update-polls [-e ID] [--history] [--force]` | Stáhne stránky přes MediaWiki API, naparsuje tabulky a uloží je do databáze s deduplikací. Stránky se cachují v databázi (výchozí platnost 6 h). `--force` cache obejde, `--history` stáhne i historické volby. |
| `model -e ID [-n RUNS] [--as-of YYYY-MM-DD] [-m TRH] [--save]` | Spočítá vážený průměr a Monte Carlo simulaci a vypíše tabulky. `--save` uloží pravděpodobnosti do deníku pro pozdější Brierovo skóre. |
| `import-odds SOUBOR [-e ID]` | Načte kurzy z CSV nebo YAML, uloží je do DB a vypíše marži každé kanceláře. |
| `fetch-odds [-e ID] [-b KANCELÁŘ] [--force]` | Stáhne kurzy z veřejných stránek kanceláří podle sekce `bookmakers` ve volbách a uloží je s časem stažení. |
| `watch [-e ID] [--no-polls]` | Jeden běh hlídání: aktualizuje průzkumy a kurzy, přepočítá model, nové signály uloží do deníku a pošle notifikaci. |
| `signals -e ID [--record] [--only-signals]` | Pro každý výsledek najde nejlepší kurz, spočítá edge a vklad podle Kellyho a ukáže arbitráže. |
| `journal -e ID` | Vypíše virtuální sázky. |
| `settle -e ID [-r vysledek.yaml]` | Zapíše skutečný výsledek a vyhodnotí sázky: P/L, ROI a Brierovo skóre modelu a trhu. |
| `calibrate [-e ID ...] [--horizons 90,60,30,14,7,1]` | Nakalibruje parametry modelu na minulých volbách. |
| `calibrate-reset` | Smaže nakalibrované parametry, model znovu použije `settings.yaml`. |
| `migrate-db --target URL [--replace]` | Zkopíruje všechna data do jiné databáze (např. z lokální SQLite do PostgreSQL). |
| `backtest -e ID [--odds SOUBOR]` | Ukáže přesnost modelu na minulých volbách. S `--odds` přehraje i virtuální sázení. |

## Konfigurace

### `config/settings.yaml`

Obsahuje globální nastavení: cesty, Wikipedii (User-Agent, cache, timeouty), parametry modelu (`model:`) a signálů (`signals:`). Každý parametr je v souboru okomentovaný. Žádný parametr modelu není zadaný natvrdo v kódu.

Nakalibrované parametry (`polledge calibrate` nebo stránka *Kalibrace* ve webu) se ukládají do databáze a mají přednost před `model:` v `settings.yaml`. K původním hodnotám se vrátíš příkazem `polledge calibrate-reset` nebo tlačítkem ve webu.

Databáze: bez dalšího nastavení se používá lokální SQLite `data/polledge.db`. Proměnná prostředí `POLLEDGE_DATABASE_URL` (nebo `database_url` v `settings.yaml`) přepne na jinou databázi, např. PostgreSQL. Přihlašovací údaje patří do proměnné prostředí, ne do souboru v gitu.

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
  row_total: [85, 110]        # povolený součet řádku v %; širší jen pro ankety s více odpověďmi

runoff:                       # jen pro two_round: H2H tabulky, jedna dvojice na tabulku
  tables:
    - heading: ["Second round"]

model:                        # volitelné přepsání parametrů jen pro tyto volby
  half_life_days: 21

bookmakers:                   # volitelné: odkud stahovat kurzy (fetch-odds, watch)
  betano:
    pages: ["https://www.betano.cz/..."]   # veřejná stránka události nebo ligy
    markets: {"Vítěz": winner}             # název trhu u kanceláře -> náš typ trhu
    selections: {"A. Babiš": BABIS}        # jen když se jméno neshoduje se jménem/aliasem kandidáta

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

Typy trhů: `winner`, `first_round_winner`, `finalists`, `qualify_runoff:<ID>` (ano/ne), `threshold:<ID>:<%>` (ano/ne), `share_over:<ID>:<%>` (over/under), `outcome:<ID>` (ano/ne, „vyhraje X?“), `seats_over:<ID>:<mandáty>` (over/under, jen s referenčním trhem). Při vkládání textu se hranice mandátů píše jako `+47.5` (více) a `-47.5` (méně), jak je uvádí Fortuna.

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

### Stahování kurzů z kanceláří

`polledge fetch-odds` projde volby se sekcí `bookmakers`, stáhne uvedené veřejné stránky a kurzy uloží s časem stažení. Každé stažení tak přidává do databáze historii kurzů, kterou lze později použít v backtestu.

Pravidla stahování (`scraping` v `settings.yaml`):

- jen veřejné stránky nabídky, nikdy účet, přihlášení ani podávání sázek,
- respektuje `robots.txt` včetně `Crawl-delay`, mezi dotazy na jeden server drží rozestup (výchozí 5 s),
- stránky cachuje (výchozí 30 min); kurzy z cache mají čas původního stažení, takže nevznikají duplicity,
- když web automatický přístup odmítne (HTTP 403), aplikace to oznámí a ochranu **neobchází**.

Stav podpory kanceláří (říjen 2026):

| Kancelář | Stav |
|---|---|
| **Betano** | podporováno: nabídka je jako JSON přímo ve veřejné stránce; umí i samo hledat nové trhy |
| **Polymarket** | podporováno jako **reference trhu**: veřejné API predikčního trhu |
| Tipsport | nepodporováno: web vrací automatickým klientům HTTP 403 |
| Chance | nepodporováno: web vrací automatickým klientům HTTP 403 |
| Fortuna | nepodporováno: nabídku načítá JavaScript z interního API |

U nepodporovaných kanceláří je nejrychlejší **vložení ze stránky** (Kurzy → *Vložit ze stránky*):

1. Na webu kanceláře otevři trh a zkopíruj celou stránku (Ctrl+A, Ctrl+C).
2. V PollEdge zvol kancelář a typ trhu a text vlož.
3. Aplikace sama najde dvojice jméno–kurz a přiřadí je ke kandidátům, i při jiném pořadí slov („Le Pen Marine“). V náhledu jde cokoli opravit a jedním tlačítkem se kurzy uloží.

Další možnosti jsou ruční formulář a `import-odds` ze souboru. Všechny zdroje se v porovnání kombinují a vybírá se nejlepší kurz.

Ověřeno v říjnu 2026: Tipsport, Chance a Mr Bit vracejí automatickým klientům HTTP 403. Fortuna, Synot Tip, Allwyn, BetX, Forbet, Merkur X-Tip a Kingsbet načítají nabídku JavaScriptem z interních rozhraní. Automaticky a v souladu s jejich pravidly proto jde stahovat jen Betano.

**Automatické hledání (Betano).** Místo pevné adresy stránky lze u kanceláře uvést klíčová slova, třeba `discover: ["Francie", "prezident"]`. Hodinové hlídání pak projde stránku Politika a stahuje kurzy ze všech soutěží, jejichž název obsahuje všechna slova (velikost písmen ani diakritika nehrají roli). Dokud kancelář trh nevypíše, nic se nestahuje a web u zdroje ukazuje „zatím nevypisuje“.

**Reference trhu (Polymarket).** Ceny z predikčního trhu bez marže jsou dobrým odhadem „názoru trhu“. Zobrazují se ve sloupci *Reference* a počítají se do pravděpodobnosti trhu. Nikdy ale nejsou nejlepším kurzem a nevznikají z nich signály, vklady ani arbitráže, protože na Polymarketu se z ČR nesází legálně. Které zdroje jsou referenční, určuje `signals.reference_bookmakers` v `settings.yaml`. U Polymarketu se v `pages` uvádí *slug* události (část adresy za `polymarket.com/event/`) a v `markets` jeho převod na typ trhu.

**Časy** se v databázi ukládají v UTC, protože ji sdílejí počítače v různých časových pásmech (domácí PC, GitHub Actions, Streamlit Cloud). Ve webu, ve výpisech i při ručním zadání kurzů se pracuje v pražském čase.

Nový scraper je třída `OddsSource` zaregistrovaná v `src/polledge/odds/scrapers/__init__.py` (viz Betano v `betano.py`). Ke stahování používá `PoliteFetcher` a k převodu názvů na naše ID `NameMapper`.

**Volby bez modelu z průzkumů (`probability_source: market`).** U některých trhů nejde rozumně modelovat výsledek z průzkumů. Typicky jde o americké prezidentské volby (primárky a sbor volitelů v jednom trhu) nebo o většinu v Kongresu (rozhodují křesla). Takové volby nemají `wiki_page` ani `first_round` a roli modelu přebírá referenční trh (`reference_market: polymarket`). Signál pak znamená, že česká kancelář platí víc, než odpovídá ceně na Polymarketu. Sází se pořád jen u kanceláře. U outsiderů s pravděpodobností kolem 1–5 % jsou takové signály vratké, protože ceny na Polymarketu tam kolísají. Ceny z Polymarketu se berou přímo jako pravděpodobnosti (burza nemá marži) a nepřepočítávají se na 100 %.

Nakonfigurované americké volby:
- `us_pres_2028`: prezidentské volby 2028, Betano vs. Polymarket.
- `us_house_2026` a `us_senate_2026`: většina ve Sněmovně a v Senátu 2026 a počty mandátů. Z Polymarketu se stahují i pásma mandátů (`seats:REP`, `seats:REP:low`) a z nich se počítají sázky více/méně mandátů (`seats_over:<strana>:<hranice>`). Uvnitř pásma (např. 205–209) se pravděpodobnost dělí rovnoměrně, a kde rozhodne jemnější sada pásem, použije se ta. Demokratické mandáty ve Sněmovně jsou 435 − republikánské (`seat_complements`). Kurzy kanceláří (Fortuna) se zadávají ručně.
- `us_congress_2026`: rozdělení obou komor (čtyři kombinace) podle Polymarketu *Balance of Power*. Kanceláře je vypisují jako ano/ne sázky na jednotlivou kombinaci (`outcome:<ID>`), pravděpodobnost se bere z trhu vítěze.

**Volby s živými kurzy:** **FR 2027** má automaticky ceny z Polymarketu (reference) a Betano se pro ni hlídá podle klíčových slov. Signály se objeví, jakmile ji Betano vypíše nebo zadáte kurzy české kanceláře ručně. Betano i Fortuna nabízejí **české prezidentské volby 2028** (`cz_pres_2028`), které se stahují z Betana. Pozor, průzkumy k nim jsou slabé:

- je jich jen pár,
- STEM/MARK se ptá „koho byste mohl volit“ s více odpověďmi a řádky dávají v součtu kolem 150 %; model je přepočítá na 100 %, takže jde jen o hrubý odhad relativní podpory,
- do voleb zbývá přes rok.

**Signály pro CZ 2028 proto neber jako spolehlivé.**

Když kancelář nevypisuje kandidáta, kterému model dává víc než 2 %, považuje se její trh za neúplný. Pravděpodobnost trhu bez marže se pak nepočítá, edge a signály ano. Kandidáti, které kancelář vypisuje, ale model pro ně nemá průzkumy, se zobrazí jako „není v modelu“ a nikdy z nich nevznikne signál.

### Hlídání a notifikace

`polledge watch` udělá v jednom běhu: aktualizaci průzkumů (z cache, pokud je čerstvá) → stažení kurzů → model → porovnání. Nové signály uloží do deníku a pošle notifikaci. Stejnou sázku za stejný kurz hlásí jen jednou.

Kanály nastavíš v `notify` v `settings.yaml`:

```yaml
notify:
  channels: [console, email]
  email:
    smtp_host: smtp.seznam.cz
    smtp_port: 587
    use_tls: true
    username: muj@ucet.cz
    password_env: POLLEDGE_SMTP_PASSWORD   # heslo jen v proměnné prostředí
    sender: muj@ucet.cz
    recipients: [muj@ucet.cz]
```

Pravidelné spouštění ve Windows (Plánovač úloh), např. každou hodinu:

```powershell
schtasks /Create /SC HOURLY /TN PollEdge /TR "C:\scr\PollEdge\.venv\Scripts\polledge.exe --config C:\scr\PollEdge\config\settings.yaml watch"
```

Na Linuxu/macOS stačí cron: `0 * * * * cd /cesta/PollEdge && .venv/bin/polledge watch`.

### Jazyk výstupů

Zdrojový kód je celý anglicky. České texty jsou v katalogu `src/polledge/locales/cs.yaml` (klíč je anglický text z kódu). Jazyk výstupů nastavíš v `settings.yaml` (`language: cs` nebo `en`). Nápovědu `--help` řídí proměnná prostředí `POLLEDGE_LANG`, protože se sestavuje dřív, než se nastavení načte. Test `tests/test_i18n.py` hlídá, že katalog obsahuje všechny texty z kódu se stejnými zástupnými symboly.

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
- **Spojení modelu s trhem** (`model_weight`, výchozí 0,5): kde existuje referenční trh (Polymarket), počítá se *výsledná pravděpodobnost* jako vážený průměr modelu a trhu v logitech: logit(p) = w·logit(p_model) + (1 − w)·logit(p_trh). Model z průzkumů nezná všechno (kandidatury, soudy, aliance), trh s penězi ano, a naopak trh bývá pomalý vůči novým průzkumům. Spojení obou je kalibrovanější než každý zvlášť a brání signálům, které stojí jen na tom, že se model s trhem rozchází. Příklad: model 64 %, Polymarket 43 % → výsledná ≈ 54 %; při kurzu 1,90 už to signál není. `model_weight: 1` vrátí původní chování (jen model). Bez referenčního trhu se použije model. Web i CLI ukazují model, trh i výslednou pravděpodobnost; edge, Kelly, deník i AI posudek pracují s výslednou.
- **Edge** = p_výsledná × nejlepší kurz − 1. **Signál** vznikne, když edge > `edge_threshold` (výchozí 5 %).
- **Arbitráž:** součet 1/nejlepší kurz přes všechny výsledky trhu (napříč kancelářemi) je menší než 1.
- **Vklad:** f* = (p·b − (1−p))/b (p = výsledná pravděpodobnost), kde b = kurz − 1. Vklad = max(0, f*) × `kelly_fraction` (výchozí 1/4) × `bankroll`, s volitelným stropem `max_stake_pct`.
- **Minimální vklad** (`min_stake`, výchozí 50 Kč): signál vznikne, jen když doporučený vklad dosáhne tohoto minima. Kelly dává u vysokých kurzů malé vklady, takže pravidlo zároveň odfiltruje vratké „edge“ u outsiderů. Například kurz 25 s edge +6 % vychází na vklad kolem 7 Kč z 10 000. Takové řádky se zobrazí s poznámkou „vklad pod minimem“.

### AI posudek signálu

Na stránce **Signály** má každý signál tlačítko **„Získat AI posudek“**. AI dostane všechno, co aplikace o signálu ví:
- pravděpodobnost z modelu a z trhu, kurzy a marže,
- scénáře kandidátů, riziko kandidatury a korekci 2. kola,
- průměry průzkumů a známá omezení modelu.

Pak vyhledá aktuální zprávy na webu a vrátí:
- **verdikt**: vsadit / snížit vklad / nesázet,
- **vlastní odhad pravděpodobnosti**,
- **násobek Kellyho vkladu** (0–1) se zdůvodněním, riziky a zdroji.

Vklad upravený násobkem jde jedním tlačítkem zapsat do deníku a posudek se k sázce uloží. Posudek nikdy nedoporučí víc, než kolik spočítal Kelly.

- **Poskytovatel** jde přepnout přímo ve webu: OpenAI (`gpt-5.5`) nebo Claude (`claude-opus-5-5`). Výchozí poskytovatele a modely nastavíš v `settings.yaml` → `ai`.
- **Klíče** se vkládají do Secrets ve Streamlit Cloud (lokálně do `.streamlit/secrets.toml`): `OPENAI_API_KEY` a/nebo `ANTHROPIC_API_KEY`. Nastavit stačí jeden.
- **Cena:** jeden posudek stojí řádově jednotky korun (model a několik vyhledávání) a trvá až minutu či dvě.
- Posudek je **názor, ne záruka**. Pomáhá zachytit to, co model z průzkumů nevidí: zprávy, kandidatury a pravidla vypořádání.

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
polledge calibrate                 # grid search + leave-one-election-out, uloží parametry do databáze
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
src/polledge/odds/scrapers/     stahování kurzů: PoliteFetcher, mapování názvů, Betano
src/polledge/signals/           edge, arbitráž, Kelly
src/polledge/journal/           deník, vyhodnocení, Brierovo skóre
src/polledge/calibration/       backtest, metriky, grid search
src/polledge/notify/            notifikace (konzole, e-mail)
src/polledge/locales/cs.yaml    české texty výstupů
src/polledge/web/               webová aplikace (Streamlit)
streamlit_app.py, PollEdge.bat  spuštění webu (cloud / lokálně)
.github/workflows/              testy a hodinové hlídání (GitHub Actions)
tests/                          pytest (HTML fixture z Wikipedie v tests/fixtures)
```

## Testy

```powershell
pytest
```

Testy pokrývají:

- parsování dat a procent, parser nad skutečnou HTML fixture z Wikipedie,
- odstranění marže, edge a Kellyho kritérium, arbitráž,
- součet simulovaných podílů na 100 %,
- chování při výpadku sítě,
- deník a vyhodnocení,
- kalibraci, včetně kontroly, že model nevidí budoucí průzkumy,
- scraper Betano nad uloženou stránkou a pravidla stahování (`robots.txt`, Crawl-delay, cache, žádné obcházení 403),
- notifikace,
- úplnost českého katalogu.

Testy nepotřebují síť.

Data z Wikipedie jsou pod licencí CC BY-SA.
