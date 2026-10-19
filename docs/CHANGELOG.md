# Changelog NexusSTAT

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
