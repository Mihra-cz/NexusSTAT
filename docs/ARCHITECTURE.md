# Architektura NexusSTAT – návrh

## Zásada

Diagnostika běží odděleně od hardwarových ochranných funkcí. Selhání NAS nebo webu nesmí zastavit kontrolu systému; selhání diagnostiky nesmí ovlivnit chlazení.

## Plánované komponenty

```text
read-only collectors  ->  Core / stavový automat  ->  aktuální stav (lokální JSON)
       |                         |                         |
  system / backup          historie událostí             +-> OLED renderer
  NAS / SSD / Docker         (SQLite, ne grafy)           +-> RGB renderer
                                                           +-> LAN web (autentizovaný)
                                                           +-> PUBLIC snapshot (whitelist)

Linux kernel/firmware -> hlavní CPU ventilátor
Linux GPIO controller  -> boční ventilátory, až po samostatně ověřeném přechodu
```

## Výchozí stav cílové platformy (zobecněno)

Podrobnosti skutečné instalace (názvy služeb, cesty mountů, výsledky inventury) jsou v soukromé dokumentaci Nexusu mimo tento repozitář. Před nasazením je vždy znovu ověřit na cílovém stroji.

- Raspberry Pi 5, Debian 13, ARM64.
- Referenční verze Pironman 5 1.3.20 (pm_auto 2.0.6), služba `pironman5.service`. V jejím procesu běží moduly ventilátorů, OLED a RGB (`FanAddon`, `OLEDAddon`, `WS2812Addon`), které má NexusSTAT postupně nahradit.
- Boční ventilátory řídí Pironman přes GPIO6 v režimu Always On; kernelový `gpio-hog` je jen návrh, není aktivovaný.
- Hostitel zálohuje Resticem na NAS a NAS je připojený síťovými mounty; metadata mountu samy nezaručují online dostupnost NAS.
- AnimeDB zatím na Nexusu neběží.

## Datový kontrakt (návrh)

- Každá kontrola: `id`, `state`, `summary`, `observed_utc`, `expires_monotonic`, `simulated`; později `source` a volitelně omezená diagnostická metadata.
- Agregovaný výsledek musí odlišovat `UNKNOWN` od `OK`; do historie se zaznamenávají přechody, ne kopie každého měření.
- Dvě časové základny, nikdy se neporovnávají mezi sebou: monotónní čas (`time.monotonic()`) pro intervaly, timeouty a platnost v běžícím procesu; skutečný UTC čas (`observed_utc`) pro budoucí perzistentní historii a časové značky událostí. Monotónní čas nepřežije restart, proto se nesmí ukládat do historie. Při budoucím exportu do JSON je nutné platnost převést (např. na zbývající sekundy nebo UTC čas vypršení v okamžiku exportu).
- Aktuální JSON publikovat lokálně atomicky, pouze s odpovídajícími oprávněními a časem platnosti.

## Implementováno lokálně (fáze 1: simulace + read-only systémový kolektor)

Stav k 2026-10-19: jen lokální kód a testy na vývojovém počítači (x86_64, Linux Mint). Kolektor čte skutečná data notebooku; OLED je pouze obrazová simulace. Nic z toho nebylo spuštěno na Raspberry Pi ani proti skutečnému hardwaru displeje.

```text
system_monitor.SystemMonitor   (read-only: /proc/stat, /proc/meminfo,
  nebo fiktivní CheckResult      /sys/class/thermal, os.statvfs("/"))
                |
                v
CheckResult  ->  core.active_problems / core.overall_state
                                   |
stisk / update(now)     ->  oled.OledController (SLEEP / CAROUSEL / STATUS)
                                   |
                            render.render_screen  ->  1bit obrázek 128 × 64 (Pillow)
```

- `core.py`: `CheckResult` má `id`, `state`, `summary`, `observed_utc` (UTC `datetime`), `expires_monotonic` (sekundy monotónních hodin) a povinný příznak `simulated`. Typy se kontrolují při vytvoření, takže záměnu časových základen odhalí hned. Pole `source` zatím chybí: existuje jediný kolektor a jeho výsledky jednoznačně určuje `id`. Výsledek s `now >= expires_monotonic` se vyhodnotí jako `UNKNOWN` s popisem „<id>: zastaralá data“, ať byl původně jakýkoli. Výjimkou je `N/A`, které nevyprší. Problémy jsou `CRITICAL`, `ERROR`, `WARNING` a `UNKNOWN`; `OK`, `INFO` a `N/A` problémem nejsou. Stejná závažnost se řadí podle `id`. `overall_state()` vrací nejzávažnější stav; bez dat nebo jen s `N/A` vrací `UNKNOWN`.
- `system_monitor.py`: jedna třída `SystemMonitor` a malé čisté funkce pro parsování (testovatelné na připravených textech). Výsledky `system.cpu_usage`, `system.cpu_temp`, `system.memory`, `disk.root`, platnost 30 s od měření (dočasná hodnota, později odvozená z intervalu spouštění).
  - CPU: ze dvou vzorků `/proc/stat`; první vzorek je `UNKNOWN` („první vzorek“). Nečinnost = `idle + iowait`, `guest` se nesčítá dvakrát, nulový nebo záporný rozdíl čítačů je `UNKNOWN`.
  - RAM: `(MemTotal − MemAvailable) / MemTotal`; chybějící, nulové nebo nesmyslné hodnoty jsou `UNKNOWN`.
  - Teplota: zóna vybraná podle typu (`cpu-thermal`, `cpu_thermal`, `soc_thermal`, `x86_pkg_temp`), ne automaticky `thermal_zone0`. Bez vhodného senzoru `UNKNOWN`, nikdy 0 °C. Hodnoty mimo (0, 125] °C jsou neplatné; jde jen o prozatímní kontrolu věrohodnosti, ne hardwarový limit ani alarmový práh.
  - Disk: výhradně `os.statvfs("/")`, výpočet jako `df` (rezervované bloky nejsou volné). `/mnt`, NAS ani jiné mounty se neprocházejí.
  - Každá kontrola má vlastní ošetření chyb (`OSError` → „nedostupné“, `ValueError`/`ArithmeticError` → „neplatná data“), selhání jedné neshodí ostatní.
  - Vytížení CPU je průměr za dobu od předchozího volání `collect()`; jeho vypovídací hodnotu tedy určuje interval volání.
  - Bez alarmových prahů: platná hodnota má stav `INFO` (změřeno, nevyhodnoceno), nikdy WARNING/ERROR. Prahy čekají na konfigurační kontrakt.
  - `simulated=False` jen při čtení skutečného `/proc`, `/sys/class/thermal` a `os.statvfs`; jakýkoli náhradní zdroj (testy) dává `simulated=True`.
- `oled.py`: čistá stavová logika bez kreslení. Čas dostává jako parametr `now` (v provozu `time.monotonic()`, v testech libovolná čísla). Stránka kolotoče se počítá z času od začátku okna, takže automatické přepínání okno neprodlužuje. Při návratu ze STATUS po nečinnosti začíná nové okno okamžikem vypršení, nikoli až pozdním voláním `update()`. Stisk nejdřív uplatní časové limity, takže stisk po uplynutí okna displej probudí a neotevře STATUS. `home_symbol()` určuje symbol stránky NEXUS: trojúhelník (`WARNING/ERROR/CRITICAL`) má přednost před `?` (`UNKNOWN`, žádná data nebo jen `N/A`). `page_lines()` sestaví řádky stránek SYSTEM a DISK z výsledků podle id; chybějící, prošlá nebo neměřitelná hodnota se ukáže jako text „<popisek> N/A“, diagnostický stav přitom zůstává `UNKNOWN` (nejde o `State.NA`).
- `render.py`: jen kreslí podle stavu automatu. STATUS má 4 řádky na stránku. Bez sledovaných dat (žádné výsledky nebo jen `N/A`) zobrazí `NO MONITOR DATA`, při platných datech bez závad `NO ACTIVE ISSUES` (ne „vše OK“, protože hodnoty `INFO` nejsou vyhodnocené prahy). Obrazovky se simulovanými výsledky nesou značku `SIM`; skutečná měření ji nemají.
- `config.py`: sekce `[oled]` z TOML (`tomllib`), časy jsou celé sekundy 1–3600, `page_seconds ≤ carousel_seconds`, neznámé klíče, jiné sekce než `[oled]` a jiný rozměr než 128 × 64 jsou chyba. Neimplementované volby (`home_title`, `clock_24h`, `[alarms] show_warning_triangle`, `[web]`, `[storage]`) jsou v ukázkové konfiguraci jen jako komentář a při zadání vyvolají chybu.
- Hodiny a datum na stránce NEXUS používají místní čas předaný zvlášť (`wall_time`); monotónní čas slouží jen časovačům a platnosti.

## Síť a důvěra

- Fáze offline: žádný poslouchající port.
- Budoucí LAN UI: port 8765 zatím jen návrh; autentizace, CSRF ochrana pro změny, role, omezení UFW po kontrole pravidel.
- Veřejný stav: samostatný read-only minimalizovaný kontrakt bez interních adres, cest, logů a API mutací. Preferovat separátní proces/port a explicitní allowlist; veřejné routování až se schválením.
- Žádný přístup k Docker socketu bez samostatného oprávnění a bezpečnostní analýzy.

## Přechod hardware (dosud neautorizovaný)

- Fáze 1: čistá simulace, žádné hardware knihovny neotevírat na Nexusu.
- Fáze 2: laboratorní čtecí testy/ověření pinů, obnovy, časovačů a oprávnění.
- Fáze 3: konkrétní testovací plán pro chlazení s rollbackem a fyzickým dohledem.
- Fáze 4: po souhlasu adaptovat OLED, RGB a tlačítko, prověřit všechny interakce.
- Fáze 5: až po doloženém funkčním nahrazení zvážit ukončení původní služby a InfluxDB.

## Zálohování

- Projektové zdrojáky budou v plánovaném `/srv/nexus/projects/nexusstat` a jejich pokrytí Resticem musí být reálně ověřené.
- Umístění provozní SQLite, staging zálohy a vyloučení živé DB jsou otevřené. Před ostrým provozem navrhnout SQLite Online Backup API, kontrolu integrity, test obnovy a zdokumentovat vše v manifestu.
- Nová aplikace NESMÍ sama měnit nebo spouštět Restic job bez schválení.
