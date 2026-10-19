# NexusSTAT

**Stav:** `development`, nenasazeno (`not-deployed`). Existuje jen lokální vývoj na notebooku: offline simulace OLED, read-only systémový kolektor, jeho pravidelné měření v terminálu a konfigurovatelné alarmové prahy (ve výchozím stavu vypnuté), vše s testy.\
**Cíl:** samostatná lokální diagnostika Raspberry Pi 5 a plánovaná postupná náhrada aplikace SunFounder Pironman 5, nikoli odstranění hardwarových ochranných funkcí.

## Co bude sledovat

Systém a NVMe, NAS, Restic, systemd služby, Docker; později AnimeDB (zatím není na Nexusu). Události a potvrzení alarmů se plánují v malé SQLite databázi. Časové řady nebudeme bez důvodu znovu archivovat.

Výstupy: 128 × 64 OLED, čtyři RGB WS2812, lokální LAN web s administrací (plánovaný port 8765), později přísně omezený veřejný status pro `mh42.cz`. Cloud nesmí být nutný.

## Implementováno lokálně: simulace, kolektor, pravidelné měření a první prahy (v0.4)

Čistý Python + `Pillow` pro **offline simulátor OLED**, stavový automat `SLEEP/CAROUSEL/STATUS`, simulované alarmy, read-only systémový kolektor s číselnými hodnotami, jeho pravidelná smyčka, vyhodnocení prahů teploty CPU a disku `/` a testy. Žádná komunikace s fyzickým hardwarem displeje, NAS, Resticem či InfluxDB.

Hotovo pouze na vývojovém počítači (ověřeno na Linux Mint, ne na Raspberry Pi):

- `src/nexusstat/core.py`: stavy, výsledky kontrol s časem platnosti a číselnou hodnotou s jednotkou, řazení aktivních problémů,
- `src/nexusstat/config.py`: načtení a validace sekcí `[oled]`, `[collector]` a `[alarms]`,
- `src/nexusstat/oled.py`: stavový automat OLED (bez kreslení a bez hardware); kolotoč NEXUS → SYSTEM → DISK → SLUŽBY,
- `src/nexusstat/render.py`: 1bitové obrazovky 128 × 64 (Pillow),
- `src/nexusstat/services.py`: katalog služeb pro stránku SLUŽBY (NEXUS, ANIMEDB, NAS, BACKUP), bez sběru dat; služba bez výsledku se doplní jako `UNKNOWN`, nenasazená AnimeDB jako `N/A`,
- `src/nexusstat/system_monitor.py`: read-only vytížení CPU, RAM, teplota CPU a obsazenost `/` (jen `/proc`, `/sys/class/thermal`, `os.statvfs`) jako číslo s jednotkou i jako text,
- `src/nexusstat/alarms.py`: vyhodnocení prahů WARNING/ERROR s hysterezí a potvrzením opakovaným měřením (3 měření pro vznik, 2 pro odeznění); jen stav, žádné automatické akce,
- `src/nexusstat/run.py`: pravidelné měření (výchozí interval 10 s, platnost výsledků 3 × interval) s jedním řádkem výpisu do terminálu na měření, ukončení Ctrl+C,
- `src/nexusstat/simulate.py`: skriptovaný scénář (fiktivní data, značka `SIM`) nebo `--live` snímek ze skutečného měření, uložené jako PNG. Ve skutečném režimu jsou služby bez kolektoru `UNKNOWN` na všech obrazovkách (SLUŽBY, symbol `?` na NEXUS, STATUS) a AnimeDB `N/A`, nikdy `OK`.

Stavy: `UNKNOWN` = sledovaná věc, jejíž aktuální stav neznáme (chybí nebo prošla data); není to potvrzená závada ani „vše v pořádku“. `N/A` = záměrně vypnutá nebo nenasazená integrace (např. AnimeDB); nevytváří alarm. Potvrzené `WARNING`/`ERROR` vznikají jen ze zapnutých prahů s hysterezí a opakovaným potvrzením.

Požadavky: Python ≥ 3.11 (`tomllib`), Pillow jen pro vykreslení. Core, automat a jejich testy fungují bez Pillow.

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m nexusstat.simulate /tmp/nexusstat-sim
PYTHONPATH=src python3 -m nexusstat.simulate /tmp/nexusstat-live --live
PYTHONPATH=src python3 -m nexusstat.system_monitor
PYTHONPATH=src python3 -m nexusstat.run
PYTHONPATH=src python3 -m nexusstat.run --config config/config.example.toml
```

Ilustrační ukázka formátu výpisu `nexusstat.run`. Hodnoty jsou vymyšlené; nejde o skutečné měření Nexusu ani vývojového počítače. První vzorek CPU je vždy `UNKNOWN`, stav `INFO` znamená změřeno bez zapnutého prahu:

```text
NexusSTAT: měření každých 10 s, platnost výsledků 30 s. Prahy: Teplota CPU vypnuto; Disk / vypnuto (bez prahu je platné měření INFO). Ukončení: Ctrl+C.
2026-10-19 12:00:00 UTC  UNKNOWN  CPU: první vzorek | Teplota CPU 48 °C | RAM 24 % z 7,9 GiB | Disk / 31 % z 117 GiB
2026-10-19 12:00:10 UTC  INFO     CPU 23 % | Teplota CPU 48 °C | RAM 24 % z 7,9 GiB | Disk / 31 % z 117 GiB
2026-10-19 12:00:20 UTC  INFO     CPU 19 % | Teplota CPU 47 °C | RAM 24 % z 7,9 GiB | Disk / 31 % z 117 GiB

Ukončeno (Ctrl+C), počet měření: 3.
```

Ilustrační řádek se zapnutým prahem teploty (vymyšlené prahy i hodnoty, ne schválené provozní nastavení). Závadná hodnota je označená, stav se potvrzuje až třetím měřením nad prahem:

```text
2026-10-19 12:00:40 UTC  WARNING  CPU 61 % | Teplota CPU 72 °C [WARNING] | RAM 24 % z 7,9 GiB | Disk / 31 % z 117 GiB
```

## Co zatím není hotové

- Schválené hodnoty prahů (vyhodnocování je ve výchozím stavu vypnuté), prahy pro CPU a RAM, kolektory NAS, Resticu, Dockeru, AnimeDB a služeb (stránka SLUŽBY zatím jen zobrazuje, co by dodaly), hardware drivery (OLED, RGB, tlačítko), web a SQLite. Nic nebylo testováno na Raspberry Pi ani na hardwaru displeje.
- Smyčka `nexusstat.run` jen vypisuje do terminálu: nekreslí OLED, nezapisuje JSON, historii ani logy a neběží jako služba. Je jednovláknová a kolektor nemá timeout, takže zaseknuté měření zastaví i výpis.
- Neexistuje provozní služba či nasazení na `nexus`.
- Není schválen přechod GPIO6 ani vypnutí Pironmanu/InfluxDB.
- Nebyla ověřena záloha ani obnova budoucí SQLite databáze NexusSTAT.
- Port 8765 je zatím jen záměr. Veřejný GitHub repozitář slouží pouze ke správě zdrojového kódu; neznamená nasazení ani zálohu.

## Orientace

- `manifest.toml`: plánovaný a ověřený stav projektu.
- `docs/REQUIREMENTS.md`: schválené a otevřené požadavky.
- `docs/ARCHITECTURE.md`: hranice procesu, hardware, síť a data.
- `docs/OPERATIONS.md`: budoucí provozní postup a bezpečnostní brány.
- `docs/CHANGELOG.md`: historie změn návrhu/implementace.
- `CLAUDE.md`: konkrétní pracovní pravidla pro Claude Code.
- `config/config.example.toml`: pouze bezpečné výchozí hodnoty.

## Licence

MIT, viz `LICENSE`. Volitelná závislost Pillow má permisivní licenci HPND, plánované knihovny Adafruit SSD1306 a NeoPixel SPI mají MIT. Kód SunFounder (GPL-2.0) se nepřebírá ani neimportuje.

## Nadřazený standard

Na produkčním Nexusu platí po schváleném zavedení `/srv/nexus/docs/PROJECT-STANDARD.md` (verze návrhu 1.0 z 2026-10-19). Není-li dostupný, agent si vyžádá platnou kopii. Ve veřejném repozitáři úmyslně není kompletní interní standard obsahující údaje o domácí síti.

**Bez výslovného souhlasu nic neinstalovat, nenasazovat, nepřepisovat, necommitovat ani nepushovat.**
