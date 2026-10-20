# NexusSTAT

**Stav:** `development`, nenasazeno (`not-deployed`). Existuje jen lokální vývoj na notebooku: offline simulace OLED, read-only systémový a síťový kolektor, jejich pravidelné měření v terminálu konfigurovatelné alarmové prahy (ve výchozím stavu vypnuté) a základ schématu SQLite historie bez ukládání měření (M3.1a), vše s testy.\
**Cíl:** samostatná lokální diagnostika Raspberry Pi 5, která postupně **nahradí aplikační software SunFounder Pironman 5** (monitoring, OLED, RGB, tlačítko, monitoring ventilátorů, historie, lokální web), ne aby běžela trvale vedle něj. Hardwarové ochranné funkce Raspberry Pi zůstávají, hlavní CPU ventilátor dál řídí Linux a Pironman se nevypne dřív, než je ověřené předání jeho funkcí včetně nezávislého řízení bočních ventilátorů.

## Co bude sledovat

Systém a NVMe, ventilátory, síť, NAS, Restic, systemd služby, Docker; později AnimeDB (zatím není na Nexusu). Hlavní databází pro historii měření, alarmů a událostí bude **SQLite** na lokálním SSD; NexusSTAT nepotřebuje InfluxDB Pironmanu (návrh M2 v `docs/HISTORY.md` schválený 2026-10-20 kromě R11, R13, R15 (odloženo do M7); zatím neimplementováno).

Výstupy: 128 × 64 OLED, čtyři RGB WS2812, lokální LAN web s administrací (plánovaný port 8765), později přísně omezený veřejný status pro `mh42.cz`. Cloud nesmí být nutný.

## Směr projektu (návrh, nic z toho není hotové)

- OLED cílově: NEXUS → SYSTEM (CPU, CPU TEMP, RAM, CPU FAN) → STORAGE → NETWORK → SERVICES, STATUS dál tlačítkem. Dnešní v0.6 má NEXUS, SYSTEM, DISK, NETWORK, SERVICES; STORAGE (místo DISK) a CPU FAN jsou další fáze.
- Historie: aktuální měření v paměti po 10 s; v SQLite časové řady, změny stavů a textových údajů (alarmy, `UNKNOWN`, `N/A`, linka, IPv4) a systémové události. Návrh M2 (`docs/HISTORY.md`, schváleno 2026-10-20 kromě R11, R13, R15): nejméně 30 dní po 10 s, 1 rok 5min agregace (alternativa 1 min), hodinové 10 let; zápis v dávkách po 60 s ve vlastním vlákně, aby výpadek databáze nezastavil měření. Záloha jen konzistentním snímkem přes SQLite Backup API, který zahrne existující Restic.
- Milníky k posouzení: M1 uzavření v0.5 → M2 návrh SQLite (`docs/HISTORY.md`, schválen 2026-10-20 kromě R11, R13, R15) → M3 lokální SQLite s testy → M4 kolektory CPU FAN a NETWORK → M5 STORAGE a nové stránky OLED → M6 lokální web s historií → M7 testovací nasazení na RPi 5 a postupná náhrada Pironmanu. Podrobnosti v `docs/ARCHITECTURE.md` (Plán milníků).

## Implementováno lokálně: simulace, kolektory, pravidelné měření a první prahy (v0.6)

Čistý Python + `Pillow` pro **offline simulátor OLED**, stavový automat `SLEEP/CAROUSEL/STATUS`, simulované alarmy, read-only systémový a síťový kolektor s číselnými hodnotami, jejich pravidelná smyčka, vyhodnocení prahů teploty CPU a disku `/` a testy. Žádná komunikace s fyzickým hardwarem displeje, NAS, Resticem či InfluxDB a žádné odchozí síťové požadavky.

Hotovo pouze na vývojovém počítači (ověřeno na Linux Mint, ne na Raspberry Pi):

- `src/nexusstat/core.py`: stavy, výsledky kontrol s časem platnosti, číselnou hodnotou s jednotkou a krátkými textovými údaji `details` (např. rozhraní, IPv4), řazení aktivních problémů,
- `src/nexusstat/config.py`: načtení a validace sekcí `[oled]`, `[collector]`, `[network]` a `[alarms]`,
- `src/nexusstat/oled.py`: stavový automat OLED (bez kreslení a bez hardware); kolotoč NEXUS → SYSTEM → DISK → NETWORK → SERVICES (v0.6),
- `src/nexusstat/oled_text.py`: krátké anglické texty displeje (např. `CPU TEMP 88°C`, `NAS OFFLINE`, `NAS NO DATA`) jen z id kontroly, stavu a číselné hodnoty, nikdy z českého `summary`; neznámé id se zobrazí jako `CHECK` s dohledatelným kódem (např. `CHECK 0D51`) (v0.5),
- `src/nexusstat/render.py`: 1bitové obrazovky 128 × 64 (Pillow) přiloženým bitmapovým písmem X11 „Fixed“ (`src/nexusstat/fonts`, volné dílo); domovská stránka s vystředěným nápisem, hodinami a datem; všechny texty na displeji anglicky bez diakritiky,
- `src/nexusstat/services.py`: katalog služeb pro stránku SERVICES (NEXUS, ANIMEDB, NAS, BACKUP), bez sběru dat; služba bez výsledku se doplní jako `UNKNOWN`, nenasazená AnimeDB jako `N/A`,
- `src/nexusstat/system_monitor.py`: read-only vytížení CPU, RAM, teplota CPU a obsazenost `/` (jen `/proc`, `/sys/class/thermal`, `os.statvfs`) jako číslo s jednotkou i jako text,
- `src/nexusstat/network_monitor.py` (v0.6): read-only jedno síťové rozhraní: stav linky `UP`/`DOWN`/`UNKNOWN`, rychlost RX/TX z čítačů jádra za skutečně uplynulý monotónní čas, lokální IPv4. Čte `/sys/class/net` a `/proc/net/route`, IPv4 dotazem na jádro (`ioctl SIOCGIFADDR` přes nepřipojený socket, nic se neodesílá ani neotevírá). Rozhraní z `[network] interface`, jinak deterministický automatický výběr fyzického rozhraní (Docker, VPN, loopback a jiná virtuální se nevybírají ani nesčítají),
- `src/nexusstat/alarms.py`: vyhodnocení prahů WARNING/ERROR s hysterezí a potvrzením opakovaným měřením (3 měření pro vznik, 2 pro odeznění); jen stav, žádné automatické akce,
- `src/nexusstat/run.py`: pravidelné měření systému a sítě (výchozí interval 10 s, platnost výsledků 3 × interval) s jedním řádkem výpisu do terminálu na měření, ukončení Ctrl+C; selhání jednoho kolektoru nezneplatní druhý,
- `src/nexusstat/history.py` (M3.1a, jen infrastruktura): vytvoření, bezpečné otevření a ověření SQLite databáze schématu v1 z `docs/HISTORY.md` jen na výslovně zadané cestě a jen v roli `test` nebo `development` (bez vytváření adresáře a přepisu souborů), zámek jediného zapisovače, otevření jen pro čtení. Aplikace do ní zatím nic neukládá a `run.py` ji nepoužívá,
- `src/nexusstat/simulate.py`: skriptovaný scénář (fiktivní data, značka `SIM`) nebo `--live` snímek ze skutečného měření, uložené jako PNG. Ve skutečném režimu jsou služby bez kolektoru `UNKNOWN` na všech obrazovkách (SERVICES, symbol `?` na NEXUS, STATUS) a AnimeDB `N/A`, nikdy `OK`.
- `src/nexusstat/preview.py`: živý grafický simulátor OLED v okně (tkinter): skutečný `render_screen()`, tlačítko, 11 fiktivních scénářů (z toho 5 síťových), zrychlení času a volitelně LIVE měření notebooku včetně sítě (v0.5, v0.6).

Jazyk: displej (i jeho simulovaný obraz) je anglicky (`CPU TEMP 48°C`, `ROOT 42%`, `UNKN NAS NO DATA`); terminálový výpis, `summary`, dokumentace a ovládání okna simulátoru zůstávají česky. Jedinou výjimkou z velkých písmen jsou jednotky rychlosti `B/s`, `KB/s`, `MB/s` (desítkově, 1 KB = 1000 B).

Stránka NETWORK (v0.6, jen lokálně, nenasazeno): název rozhraní a linka (`ETH0 UP`, `ETH0 DOWN`), `RX` a `TX` (`1.2 MB/s`, `85 KB/s`, `312 B/s`) a `IPV4` se samotnou lokální adresou. Rozhraní bez IPv4 je `NONE`, chybějící, neplatný nebo prošlý údaj `UNKN`, první vzorek rychlosti `UNKN`. Adresa webu se neukazuje, dokud NexusSTAT žádný web nemá; plánovaný port 8765 se nikde nezobrazuje. Linka `DOWN` je platné měření (`INFO`), ne alarm: prahy pro síť zatím neexistují.

Stavy: `UNKNOWN` = sledovaná věc, jejíž aktuální stav neznáme (chybí nebo prošla data); není to potvrzená závada ani „vše v pořádku“. `N/A` = záměrně vypnutá nebo nenasazená integrace (např. AnimeDB); nevytváří alarm. Potvrzené `WARNING`/`ERROR` vznikají jen ze zapnutých prahů s hysterezí a opakovaným potvrzením.

Požadavky: Python ≥ 3.11 (`tomllib`), Pillow jen pro vykreslení (písmo je přiložené, systémová písma se nepoužívají), tkinter (`python3-tk`) jen pro okno živého simulátoru. Core, automat a jejich testy fungují bez Pillow i bez tkinter.

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m nexusstat.simulate /tmp/nexusstat-sim
PYTHONPATH=src python3 -m nexusstat.simulate /tmp/nexusstat-live --live
PYTHONPATH=src python3 -m nexusstat.system_monitor
PYTHONPATH=src python3 -m nexusstat.network_monitor
PYTHONPATH=src python3 -m nexusstat.network_monitor eth0
PYTHONPATH=src python3 -m nexusstat.run
PYTHONPATH=src python3 -m nexusstat.run --config config/config.example.toml
PYTHONPATH=src python3 -m nexusstat.preview
```

Živý simulátor OLED (`nexusstat.preview`, volby `--scenario`, `--scale 4|6`, `--config`): okno ukazuje skutečnou bitmapu 128 × 64 zvětšenou bez vyhlazení. Ovládání: tlačítko „Stisk“, mezerník nebo Enter = krátký stisk; nabídka scénářů (1 Vše v pořádku, 2 WARNING, 3 ERROR a CRITICAL, 4 UNKNOWN, 5 Více problémů, 6 Nenapojené služby, 7–11 NETWORK: linka DOWN, bez IPv4, nízký provoz, vysoký provoz, neplatná a zastaralá data, LIVE); rychlost 1×/5×/10× jen v simulaci; Esc, křížek nebo Ctrl+C okno zavře. Fiktivní scénáře nesou značku `SIM`; LIVE měří **tento notebook, ne Nexus** (systém i síť), běží ve skutečném čase a služby bez kolektoru ukazuje `UNKNOWN`, AnimeDB `N/A`. Na vývojovém notebooku je nainstalovaný `python3-tk` 3.12.3 (Tk 8.6.14, X11) a okno bylo skutečně spuštěné a ověřené (kolotoč, STATUS, timeouty 30 s a 300 s, zrychlení, scénáře, LIVE, zavření); podrobnosti v `docs/CHANGELOG.md`.

Ilustrační ukázka formátu výpisu `nexusstat.run`. Hodnoty jsou vymyšlené (adresa z dokumentačního rozsahu RFC 5737); nejde o skutečné měření Nexusu ani vývojového počítače. První vzorek CPU i síťové rychlosti je vždy `UNKNOWN`, stav `INFO` znamená změřeno bez zapnutého prahu:

```text
NexusSTAT: měření každých 10 s, platnost výsledků 30 s. Prahy: Teplota CPU vypnuto; Disk / vypnuto (bez prahu je platné měření INFO). Ukončení: Ctrl+C.
2026-10-19 12:00:00 UTC  UNKNOWN  CPU: první vzorek | Teplota CPU 48 °C | RAM 24 % z 7,9 GiB | Disk / 31 % z 117 GiB | Síť eth0 UP | Síť RX: první vzorek | Síť TX: první vzorek | IPv4 192.0.2.10
2026-10-19 12:00:10 UTC  INFO     CPU 23 % | Teplota CPU 48 °C | RAM 24 % z 7,9 GiB | Disk / 31 % z 117 GiB | Síť eth0 UP | Síť RX 1,2 MB/s | Síť TX 85,3 kB/s | IPv4 192.0.2.10
2026-10-19 12:00:20 UTC  INFO     CPU 19 % | Teplota CPU 47 °C | RAM 24 % z 7,9 GiB | Disk / 31 % z 117 GiB | Síť eth0 UP | Síť RX 312 B/s | Síť TX 96 B/s | IPv4 192.0.2.10

Ukončeno (Ctrl+C), počet měření: 3.
```

Ilustrační řádek se zapnutým prahem teploty (vymyšlené prahy i hodnoty, ne schválené provozní nastavení). Závadná hodnota je označená, stav se potvrzuje až třetím měřením nad prahem:

```text
2026-10-19 12:00:40 UTC  WARNING  CPU 61 % | Teplota CPU 72 °C [WARNING] | RAM 24 % z 7,9 GiB | Disk / 31 % z 117 GiB | Síť eth0 UP | Síť RX 1,1 MB/s | Síť TX 80,2 kB/s | IPv4 192.0.2.10
```

## Co zatím není hotové

- Schválené hodnoty prahů (vyhodnocování je ve výchozím stavu vypnuté), prahy pro CPU a RAM, kolektory NAS, Resticu, Dockeru, AnimeDB a služeb (stránka SERVICES zatím jen zobrazuje, co by dodaly), hardware drivery (OLED, RGB, tlačítko; živý simulátor je jen okno na notebooku), web a SQLite. Nic nebylo testováno na Raspberry Pi ani na hardwaru displeje.
- Velikost RAM a disku na OLED (`RAM 24% / 7.9G`): kolektor ji dává jen v českém textu `summary`, který displej nečte; zobrazení by vyžadovalo strukturovanou hodnotu kapacity v kolektoru (samostatný krok).
- Ukládání historie (zápis měření a změn, agregace, retence, snímky a záloha); existuje jen základ schématu `history.py` (M3.1a) nad dočasnými testovacími databázemi, návrh M2 v `docs/HISTORY.md` a benchmark nad syntetickými daty; žádná skutečná databáze historie, kolektory CPU FAN a dalších úložišť, stránka STORAGE (místo DISK) a čtyřřádkový SYSTEM. Vše jen návrh. Síťový kolektor a stránka NETWORK jsou od v0.6 implementované, ale jen lokálně na notebooku (nenasazeno, neověřeno na Raspberry Pi).
- Síť: žádné alarmové prahy (linka `DOWN` zůstává `INFO`), jen jedno rozhraní a jen jeho primární IPv4 (bez IPv6), procentní vytížení linky se nepočítá; adresa webového rozhraní se ukáže až s běžícím webem.
- Smyčka `nexusstat.run` jen vypisuje do terminálu: nekreslí OLED, nezapisuje JSON, historii ani logy a neběží jako služba. Je jednovláknová a kolektor nemá timeout, takže zaseknuté měření zastaví i výpis.
- Neexistuje provozní služba či nasazení na `nexus`.
- Není schválen přechod GPIO6 ani vypnutí Pironmanu/InfluxDB.
- Nebyla ověřena záloha ani obnova budoucí SQLite databáze NexusSTAT.
- Port 8765 je zatím jen záměr. Veřejný GitHub repozitář slouží pouze ke správě zdrojového kódu; neznamená nasazení ani zálohu.

## Orientace

- `manifest.toml`: plánovaný a ověřený stav projektu.
- `docs/REQUIREMENTS.md`: schválené a otevřené požadavky.
- `docs/ARCHITECTURE.md`: hranice procesu, hardware, síť a data.
- `docs/HISTORY.md`: návrh SQLite historie (M2): schéma, zápis, retence, záloha a obnova, benchmark; neimplementováno.
- `tools/sqlite_history_benchmark.py`: benchmark návrhu, jen dočasné syntetické databáze; není součástí aplikace.
- `docs/OPERATIONS.md`: budoucí provozní postup a bezpečnostní brány.
- `docs/CHANGELOG.md`: historie změn návrhu/implementace.
- `CLAUDE.md`: konkrétní pracovní pravidla pro Claude Code.
- `config/config.example.toml`: pouze bezpečné výchozí hodnoty.

## Licence

MIT, viz `LICENSE`. Volitelná závislost Pillow má permisivní licenci HPND, plánované knihovny Adafruit SSD1306 a NeoPixel SPI mají MIT. Kód SunFounder (GPL-2.0) se nepřebírá ani neimportuje.

## Nadřazený standard

Na produkčním Nexusu platí po schváleném zavedení `/srv/nexus/docs/PROJECT-STANDARD.md` (verze návrhu 1.0 z 2026-10-19). Není-li dostupný, agent si vyžádá platnou kopii. Ve veřejném repozitáři úmyslně není kompletní interní standard obsahující údaje o domácí síti.

**Bez výslovného souhlasu nic neinstalovat, nenasazovat, nepřepisovat, necommitovat ani nepushovat.**
