# Changelog NexusSTAT

## 2026-10-19 – OLED: čtvrtá stránka SLUŽBY (lokálně, navazuje na rozpracovanou v0.4)

- Kolotoč OLED: NEXUS → SYSTEM → DISK → SLUŽBY (interně `SERVICES`) a znovu NEXUS. Interval stránky (výchozí 10 s), okno 300 s, nečinnost STATUS 30 s i ovládání STATUS beze změny.
- Nový `src/nexusstat/services.py`: katalog služeb (NEXUS, ANIMEDB, NAS, BACKUP) s id pro budoucí kolektory a známá skutečnost „AnimeDB není nasazena“ (`N/A`). Žádný sběr dat Dockeru, NAS, Resticu ani AnimeDB.
- `oled.py`: `service_rows()` a společné texty stavů `STATE_TEXT`. Služba bez výsledku nebo s prošlým výsledkem je `UNKN`, nikdy `OK`; `N/A` zůstává `N/A`.
- Sjednocení UNKNOWN (kontrola před commitem): služba bez kolektoru byla `UNKN` jen na stránce SLUŽBY, zatímco stránka NEXUS neměla `?` a STATUS hlásil `NO ACTIVE ISSUES`. Nově `services.complete_service_results()` doplní sledované služby bez výsledku jako `UNKNOWN` („<název>: bez dat“) a nenasazenou AnimeDB jako `N/A`; všechny OLED výstupy tak vycházejí ze stejných výsledků. Původní `deployment_facts()` nahrazeno. Evaluator alarmů beze změny.
- `fit_text` zkracuje podle skutečně vykreslených znaků (`textbbox`): dříve mohl inkoust přesáhnout pravý okraj o 1 px (např. „chybí disk.smart: v“). `render.py`: stránka SLUŽBY (název vlevo, stav vpravo, značka `SIM` u fiktivních dat); STATUS používá tytéž texty stavů.
- Simulátor: fiktivní služby NEXUS, ANIMEDB, NAS `OK`, BACKUP `WARN`. Fiktivní problémy, které by jim odporovaly (NAS CRITICAL, záloha ERROR, AnimeDB N/A), nahrazeny jinými fiktivními položkami; scénář projde čtyři stránky a dál ukazuje dvě stránky STATUS. `--live`: služby `UNKN`, AnimeDB `N/A`.
- Testy: pořadí a střídání čtyř stránek, návrat na NEXUS, timeouty, stisk na stránce SLUŽBY, simulované, neznámé, prošlé a `N/A` stavy služeb, rozvržení 128 × 64 pro všechny kombinace názvů a stavů, scénář simulátoru. Dva dosavadní testy s pevným třístránkovým cyklem upraveny na čtyři stránky (časování se nezměnilo). Test rozvržení odhalil, že stav zarovnaný vpravo podle šířky pro posun zasahoval o zlomek pixelu za okraj; zarovnání je nově podle skutečného inkoustu.
- Ověření: celá sada s Pillow i bez Pillow; snímky scénáře a `--live` uložené do `/tmp` a vizuálně zkontrolované. Bez hardware, nasazení, commitu a pushe.

## 2026-10-19 – v0.4: číselná měření a první alarmové prahy (lokálně)

- Datový model: nový `Measurement(value, unit)` a volitelné `CheckResult.measurement`. `UNKNOWN` a `N/A` hodnotu mít nesmějí, expirace ji smaže. UTC čas, monotónní platnost a `simulated` beze změny.
- `system_monitor.py`: každá kontrola vrací nezaokrouhlené číslo s jednotkou (`%`, `°C`) a stejný text jako dřív. Chyba měření = `measurement` `None` a `UNKNOWN`.
- Nový `alarms.py` (`AlarmEvaluator`): prahy WARNING/ERROR pro teplotu CPU a obsazení `/`, hodnota rovná prahu je nad prahem, hystereze pro návrat, vznik po 3 a odeznění po 2 po sobě jdoucích měřeních (konfigurovatelné). `UNKNOWN` nebo mezera v datech přeruší řadu, závadu ale nevyřeší; `N/A` beze změny; simulovaná a skutečná data mají oddělenou paměť. Vyhodnocuje se jen číselná hodnota, nikdy text. Žádné automatické akce.
- Konfigurace: `[alarms.cpu_temp]` a `[alarms.disk_root]` (`enabled`, `warning`, `error`, `hysteresis`, `confirm_count`, `clear_count`). Výchozí stav vypnuto a bez prahových hodnot; zapnutí vyžaduje všechny tři prahy. Validace pořadí prahů, hystereze, rozsahů veličin a dosažitelnosti odeznění.
- `run.py`: vyhodnocení prahů po každém měření i po selhání; úvodní řádek popisuje prahy, hodnota v závadě má ve výpisu `[WARNING]`/`[ERROR]`. `core.py` beze změny chování mimo nové pole; `oled.py`, `render.py` a `simulate.py` beze změny.
- Testy: nový `tests/test_alarms.py` (hranice prahů, hystereze, opakovaná měření, výkyvy, UNKNOWN, N/A, expirace, mezera v datech, simulovaná data, OLED a vykreslení, integrace se skutečným kolektorem nad vzorovými soubory); rozšířené `test_core.py`, `test_config.py`, `test_system_monitor.py` a `test_run.py`.
- Ověření: celá sada s Pillow i bez Pillow; kontrolní mutace evaluatoru (ostrá/neostrá hranice, bez hystereze, UNKNOWN jako vyřešení, bez přerušení řady, okamžité potvrzení či odeznění, sdílená paměť simulace, chybějící hodnota jako INFO, prošlá hodnota) testy odhalí; ruční běh na notebooku s výchozí konfigurací (bez alarmů) a se zjevně ilustračním prahem (WARNING po třetím měření).
- Revize před commitem (opraveno, s regresními testy, které proti předchozí verzi selžou):
  - Opakované vyhodnocení téhož vzorku zvyšovalo počítadlo potvrzení (stejný vzorek 3× = WARNING) a opakované staré `UNKNOWN` přerušilo novější řadu. Nově je identita vzorku (`id`, `simulated`, `expires_monotonic`) a stejný nebo starší vzorek se nezapočítá.
  - Hranice hystereze u desetinných prahů byla posunutá binární aritmetikou (65.2 − 1.1 = 64.10000000000001), takže hodnota přesně na hranici závadu ukončila; validace zase přijala hysterezi rovnou odstupu prahů (70.1/75.2/5.1). Hranice se nově zaokrouhlují na 6 desetinných míst.
  - Obří celé číslo v TOML prahu shodilo `nexusstat.run` tracebackem (`OverflowError`, kód 1); nově `ConfigError` a kód 2. `CheckResult` odmítá NaN/nekonečnou platnost (jinak by nikdy nevypršel), `SystemMonitor` odmítá NaN/nekonečnou platnost a `Measurement(10**400)` dává `ValueError`. Společná kontrola `core.is_finite_number()`.
  - Neopodstatněné (ověřeno testy, beze změny kódu): NaN/nekonečno/bool v prazích a hysterezi se odmítaly; `UNKNOWN` během rozpracovaného potvrzování řadu přeruší; potvrzený alarm přes `UNKNOWN` trvá; vypnuté pravidlo nikdy nevytvoří WARNING/ERROR; přechod ERROR → WARNING a celé hranice fungovaly; simulovaná data zůstávají označená.
- Hodnoty prahů pro Nexus nejsou schválené ani nastavené. Ověřeno jen na notebooku. Bez nasazení, commitu a pushe.

## 2026-10-19 – v0.3: pravidelný běh diagnostiky (lokálně)

- Nový `src/nexusstat/run.py`: jedna hlavní smyčka `run_loop()`, která každých `interval_seconds` změří CPU, teplotu CPU, RAM a obsazenost `/` a vypíše jeden řádek do terminálu. Spuštění `PYTHONPATH=src python3 -m nexusstat.run [--config SOUBOR]`, ukončení Ctrl+C.
- Plánování v monotónním čase, čas měření v UTC. Termíny se počítají od plánovaného času, takže neujíždějí. Zpoždění o celý interval nebo víc spustí jediné měření a interval začne znovu; zmeškaná měření se nedohánějí. Mezi měřeními proces spí, bez aktivního čekání.
- Výjimka kolektoru: všechny kontroly `UNKNOWN` („měření selhalo“), předchozí hodnoty se nepodrží, smyčka běží dál. Ctrl+C se za selhání nepovažuje.
- Konfigurace: nová sekce `[collector]` s `interval_seconds = 10` (celé sekundy 1–3600, stejně přísná validace jako `[oled]`). `load_config()` vrací `Config(oled, collector)` a validuje vždy celý soubor; nahrazuje `load_oled_config()`. Platnost výsledků `validity_seconds` = 3 × interval.
- `system_monitor.py`: odstraněna pevná platnost `RESULT_VALIDITY_SECONDS = 30`; `SystemMonitor(validity_seconds, ...)` ji dostává od volajícího. Simulátor (`--live`) a ukázka `system_monitor` používají platnost z konfigurace. Funkce simulátoru beze změny.
- Testy: `tests/test_run.py` se simulovanými hodinami, `sleep` a kolektorem (plánování, drift, zpoždění a restart intervalu, expirace, výpadek kolektoru, Ctrl+C, výpis, kompatibilita s Core a OLED, `main` a konfigurace); rozšířené `test_config.py` a `test_system_monitor.py`. Žádný test skutečně nečeká.
- `CLAUDE.md` bod 3 popisuje aktuální pracovní postup místo splněného prvního úkolu. `REQUIREMENTS.md`: platnost 3 × interval a sekce Pravidelné měření.
- Ověření: celá sada `unittest` s Pillow i bez Pillow; ruční běh `nexusstat.run` na notebooku ukončený signálem SIGINT (kód 0, bez tracebacku, spotřeba CPU ~0,2 s za 7 s běhu); neplatná konfigurace kód 2; kontrolní mutace smyčky (drift, chybějící restart, podržení starých hodnot, aktivní čekání, Ctrl+C jako výpadek) testy odhalí. Simulátor ověřen bez generování PNG.
- Ověřeno jen na notebooku (x86_64, Linux Mint), ne na Raspberry Pi 5. Bez nasazení, služby, commitu a pushe.

## 2026-10-19 – Git a příprava veřejného repozitáře

- Lokálně `git init -b main` a `origin` = `https://github.com/Mihra-cz/NexusSTAT.git` (veřejný, v okamžiku propojení prázdný).
- Licence MIT (`LICENSE`, Copyright 2026 Mihra-cz); v manifestu `license` a veřejný vlastník `Mihra-cz` místo jména SSH uživatele.
- `.gitignore` pro veřejný repozitář: snímky simulátoru jen podle vzorů jmen ze `simulate.py` (ostatní PNG lze verzovat), `venv/`, lokální varianty `config/*.toml` (verzují se jen `*.example.toml`), `.claude/settings.local.json`, klíče (`*.pem`, `*.key`, `secrets/`), SQLite journal, záložní soubory editoru a ochrana proti omylem zkopírovanému `PROJECT-STANDARD.md`, `PROJECTS.md` a `NEXUS-RECOVERY.md`.
- Zobecněny údaje o skutečné instalaci Nexusu: názvy zálohovacích jednotek, cesty NAS mountů, číslo vstupního zařízení, výsledky inventury (`ARCHITECTURE.md`, `OPERATIONS.md`, `REQUIREMENTS.md`, `manifest.toml`). Konkrétní hodnoty jsou v soukromé dokumentaci mimo repozitář.
- Manifest (`version_control`, `repository`), README a návratový postup v `OPERATIONS.md` sladěny se stavem Gitu.
- Proč: první commit do veřejného repozitáře bez soukromých údajů Nexusu.
- Ověření: kontrola souborů pro commit na IP adresy, údaje NAS, účty a tajemství; `git check-ignore` nad vzorovými cestami a skutečnými snímky simulátoru; parsování TOML; sada `unittest`. Implementace beze změny.
- Nasazení na Nexus: žádné. Tento stav je obsahem prvního commitu a pushe do veřejného repozitáře, schválených správcem.

## 2026-10-19 – v0.2: závěrečná kontrola milníku

- **Opraveno:** poškozený nesmyslně velký údaj (např. 400místná teplota nebo `MemTotal`) vyvolal `OverflowError` a shodil celé `collect()`, tedy všechny kontroly. Nově je to `UNKNOWN` („neplatná data“) jen pro dotčenou kontrolu.
- **Opraveno:** `SystemMonitor(proc_dir=Path("/proc"))` četl skutečný systém, ale označil výsledky `simulated=True`. Cesty se nově porovnávají jako `Path`.
- Komentáře a dokumentace sladěny s potvrzenými rozhodnutími (INFO bez prahů, `NO ACTIVE ISSUES`, první vzorek CPU `UNKNOWN`, dočasná platnost 30 s, rozsah teploty jen jako kontrola věrohodnosti).
- Manifest: `status = "development"` (dříve `planned`), nová sekce `[runtime]` (Python, závislosti), v `[development]` datum ověření, stav verzování a lokální zálohy; odkazy na `REQUIREMENTS.md` a `SECURITY.md`. `README.md` a `CLAUDE.md` sladěny se stavem.
- Proč: uzavření milníku v0.2 a sladění se `PROJECT-STANDARD.md` v1.0 (soukromá kopie na notebooku; šablona manifestu z Nexusu nebyla k dispozici, názvy nových polí nejsou ověřené proti šabloně).
- Ověření: celá sada `unittest` s Pillow i bez Pillow, parsování TOML, regresní testy obou oprav selhávají na původním kódu.
- Nasazení: žádné. Bez commitu a pushe.

## 2026-10-19 – v0.2: read-only systémový kolektor (lokálně)

- Nový `src/nexusstat/system_monitor.py`: vytížení CPU ze dvou vzorků `/proc/stat`, RAM z `/proc/meminfo` (`MemAvailable`), teplota CPU ze zóny `/sys/class/thermal` vybrané podle typu, obsazenost `/` přes `os.statvfs`. Bez psutil, jen standardní knihovna.
- Platná hodnota má stav `INFO`, nedostupná `UNKNOWN`; bez alarmových prahů. Chyba jedné kontroly neovlivní ostatní.
- Stránky SYSTEM a DISK čtou výsledky kolektoru místo pevných ukázkových čísel; nedostupná hodnota jako text `N/A`.
- STATUS bez problémů: `NO ACTIVE ISSUES` místo `ALL CHECKS OK` (hodnoty `INFO` nejsou vyhodnocené).
- Simulátor: fiktivní systémové hodnoty ve scénáři (`simulated=True`) a režim `--live` se skutečným měřením tohoto počítače. Ukázkový výpis: `python3 -m nexusstat.system_monitor`.
- Testy: `tests/test_system_monitor.py` nad připravenými vzorovými vstupy.
- Ověřeno jen na notebooku (x86_64, Linux Mint), ne na Raspberry Pi 5. Bez nasazení, commitu a pushe. `PROJECT-STANDARD.md` (ani soukromá kopie) nebyl k dispozici.

## 2026-10-19 – Core v0.1: opravy okrajových stavů

- `N/A` už nevyprší na `UNKNOWN`; prošlé výsledky ostatních stavů jsou `UNKNOWN` s popisem „zastaralá data“.
- Stránka NEXUS: nový symbol `?` pro UNKNOWN bez potvrzené závady, žádná data nebo jen N/A; trojúhelník má přednost. Dříve vypadal tento stav stejně jako „vše OK“.
- STATUS: `NO MONITOR DATA` i pro případ, kdy existují jen výsledky `N/A` (dříve „Bez závad“); jinak `ALL CHECKS OK`.
- Datový model: `observed_utc` (UTC) a `expires_monotonic` místo nejednoznačných `observed_at`/`expires_at` v monotónním čase; povinný příznak `simulated` a značka `SIM` na displeji.
- Konfigurace: `home_title`, `clock_24h`, `show_warning_triangle` a sekce `[web]`/`[storage]` přesunuty do komentářů jako budoucí možnosti; neimplementované klíče v `[oled]` i nenačítané sekce jsou chyba.
- Odstraněny prázdné `src/.gitkeep` a `tests/.gitkeep`.
- Omezení: `PROJECT-STANDARD.md` nebyl na vývojovém počítači k dispozici, soulad s ním není ověřen.
- Technický stav: pouze simulace na vývojovém počítači. Bez hardware, nasazení, commitu a pushe.

## 2026-10-19 – první implementační krok: offline simulace Core a OLED

- Přidán balíček `src/nexusstat/`: stavový model a řazení problémů (`core.py`), validovaná konfigurace `[oled]` (`config.py`), stavový automat SLEEP/CAROUSEL/STATUS (`oled.py`), vykreslení 128 × 64 (`render.py`) a skriptovaný scénář do PNG (`simulate.py`).
- Přidány testy `unittest` se simulovaným časem (`tests/`); testy vykreslení se bez Pillow přeskočí.
- `config.example.toml`: doplněny komentáře k rozsahům a k zatím nepoužitým klíčům.
- Technický stav: pouze simulace na vývojovém počítači. Žádný hardware, kolektory, web, SQLite, nasazení, commit ani push.

## 2026-10-19 – návrhový startovní balíček

- Připraven manifest projektu v režimu `planned` a dokumentace podle `PROJECT-STANDARD.md` v1.0.
- Popsáno schválené OLED UX, rozsah monitoringu, síťové hranice a plán bezpečného nahrazení Pironmanu.
- Technický stav: bez implementace, bez testů zařízení, bez nasazení, bez commitu/pushe.
- Ověření: struktura dokumentů a parsování TOML (provádí se při sestavení balíčku).
