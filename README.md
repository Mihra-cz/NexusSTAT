# NexusSTAT

**Stav:** `development`, nenasazeno (`not-deployed`). Existuje jen lokální vývoj na notebooku: offline simulace OLED, read-only systémový, síťový a od M4-1 i ventilátorový (CPU FAN) kolektor, jejich pravidelné měření v terminálu, konfigurovatelné alarmové prahy (ve výchozím stavu vypnuté) a lokálně implementovaná SQLite historie (M3: zápis, agregace, retence, vlákno zapisovače, volitelné napojení na měřicí smyčku, neměnné generace snímků, rozpoznání obnovy a příkazy správce), vše s testy. RGB signalizace a potvrzování závad jsou zatím jen schválený návrh.\
**Cíl:** samostatná lokální diagnostika Raspberry Pi 5, která postupně **nahradí aplikační software SunFounder Pironman 5** (monitoring, OLED, RGB, tlačítko, monitoring ventilátorů, historie, lokální web), ne aby běžela trvale vedle něj. Hardwarové ochranné funkce Raspberry Pi zůstávají, hlavní CPU ventilátor dál řídí Linux a Pironman se nevypne dřív, než je ověřené předání jeho funkcí včetně nezávislého řízení bočních ventilátorů.

## Co bude sledovat

Systém a NVMe, ventilátory, síť, NAS, Restic, systemd služby, Docker; později AnimeDB (zatím není na Nexusu). Hlavní databází pro historii měření, alarmů a událostí bude **SQLite** na lokálním SSD; NexusSTAT nepotřebuje InfluxDB Pironmanu (návrh M2 v `docs/HISTORY.md` schválený 2026-10-20 kromě R11, R13, R15 (odloženo do M7); implementováno lokálně v M3, na Nexusu nenasazeno).

Výstupy: 128 × 64 OLED, čtyři RGB WS2812, lokální LAN web s administrací (plánovaný port 8765), později přísně omezený veřejný status pro `mh42.cz`. Cloud nesmí být nutný.

## Směr projektu (cílový stav; na Nexusu nic nenasazeno)

- OLED cílově: NEXUS → SYSTEM (CPU, CPU TEMP, RAM, CPU FAN) → STORAGE → NETWORK → SERVICES, STATUS dál tlačítkem. Lokálně je hotový kolotoč NEXUS, SYSTEM (od M4-1 se čtyřmi řádky včetně CPU FAN), DISK, NETWORK, SERVICES; STORAGE místo DISK je další fáze.
- Historie: aktuální měření v paměti po 10 s; v SQLite časové řady, změny stavů a textových údajů (alarmy, `UNKNOWN`, `N/A`, linka, IPv4) a systémové události. Návrh M2 (`docs/HISTORY.md`, schváleno 2026-10-20 kromě R11, R13, R15), lokálně implementovaný v M3: nejméně 30 dní po 10 s, 1 rok 5min agregace (alternativa 1 min), hodinové 10 let; zápis v dávkách po 60 s ve vlastním vlákně, aby výpadek databáze nezastavil měření. Záloha jen konzistentními snímky přes SQLite Backup API (lokálně hotové), které zahrne existující Restic (zatím neschváleno).
- RGB a potvrzování závad (schválený návrh 2026-10-23, `docs/REQUIREMENTS.md`): LED normálně zhasnuté, při probuzeném OLED tlumeně bílé jen ve zdravém ověřeném stavu; `WARNING` oranžově a `ERROR` červeně s počtem bliknutí podle kategorie, `CRITICAL` rychle červeně a nejde umlčet, `UNKNOWN` nikdy nebliká. Webové potvrzení vypne jen automatické blikání `WARNING`/`ERROR`, závadu neopraví a OLED ji dál ukazuje; jas a potvrzení budou v samostatném malém souboru v `appdata`. Neimplementováno, LED na Nexusu dál ovládá Pironman.
- Milníky k posouzení: M1 uzavření v0.5 → M2 návrh SQLite (`docs/HISTORY.md`, schválen 2026-10-20 kromě R11, R13, R15) → M3 lokální SQLite s testy (hotovo lokálně) → M4 kolektory CPU FAN a NETWORK (NETWORK ve v0.6 a CPU FAN v M4-1 hotové lokálně) → M5 STORAGE a nové stránky OLED → M6 lokální web s historií → M7 testovací nasazení na RPi 5 a postupná náhrada Pironmanu. Podrobnosti v `docs/ARCHITECTURE.md` (Plán milníků).

## Implementováno lokálně: simulace, kolektory, pravidelné měření a první prahy (v0.6)

Čistý Python + `Pillow` pro **offline simulátor OLED**, stavový automat `SLEEP/CAROUSEL/STATUS`, simulované alarmy, read-only systémový a síťový kolektor s číselnými hodnotami, jejich pravidelná smyčka, vyhodnocení prahů teploty CPU a disku `/` a testy. Žádná komunikace s fyzickým hardwarem displeje, NAS, Resticem či InfluxDB a žádné odchozí síťové požadavky.

Hotovo pouze na vývojovém počítači (ověřeno na Linux Mint, ne na Raspberry Pi):

- `src/nexusstat/core.py`: stavy, výsledky kontrol s časem platnosti, číselnou hodnotou s jednotkou a krátkými textovými údaji `details` (např. rozhraní, IPv4), řazení aktivních problémů,
- `src/nexusstat/config.py`: načtení a validace sekcí `[oled]`, `[collector]`, `[network]`, `[alarms]` a `[history]` (ukládání historie, výchozí stav vypnuto a bez výchozí cesty),
- `src/nexusstat/oled.py`: stavový automat OLED (bez kreslení a bez hardware); kolotoč NEXUS → SYSTEM → DISK → NETWORK → SERVICES (v0.6),
- `src/nexusstat/oled_text.py`: krátké anglické texty displeje (např. `CPU TEMP 88°C`, `NAS OFFLINE`, `NAS NO DATA`) jen z id kontroly, stavu a číselné hodnoty, nikdy z českého `summary`; neznámé id se zobrazí jako `CHECK` s dohledatelným kódem (např. `CHECK 0D51`) (v0.5),
- `src/nexusstat/render.py`: 1bitové obrazovky 128 × 64 (Pillow) přiloženým bitmapovým písmem X11 „Fixed“ (`src/nexusstat/fonts`, volné dílo); domovská stránka s vystředěným nápisem, hodinami a datem; všechny texty na displeji anglicky bez diakritiky,
- `src/nexusstat/services.py`: katalog služeb pro stránku SERVICES (NEXUS, ANIMEDB, NAS, BACKUP), bez sběru dat; služba bez výsledku se doplní jako `UNKNOWN`, nenasazená AnimeDB jako `N/A`,
- `src/nexusstat/system_monitor.py`: read-only vytížení CPU, RAM, teplota CPU a obsazenost `/` (jen `/proc`, `/sys/class/thermal`, `os.statvfs`) jako číslo s jednotkou i jako text,
- `src/nexusstat/fan_monitor.py` (M4-1): read-only **stav** hlavního ventilátoru CPU jako jediná kontrola. `ON`/`OFF` z naměřených otáček (otáčky se přidají jako doplňující číslo, `ON 3200`), bez tachometru `REQ ON`/`REQ OFF` jen z požadavku řízení (hwmon `pwmN`, nebo chladicí zařízení kernelu typu `pwm-fan`) — nikdy tvrzení o otáčení a nikdy odhadnuté otáčky. Zařízení se hledá podle identity (`/sys/class/hwmon/hwmon*/name`, `fanN_label`), nikdy podle čísla `hwmonX`. Nic nezapisuje a ventilátor neřídí — chlazení dál řídí Linux,
- `src/nexusstat/network_monitor.py` (v0.6): read-only jedno síťové rozhraní: stav linky `UP`/`DOWN`/`UNKNOWN`, rychlost RX/TX z čítačů jádra za skutečně uplynulý monotónní čas, lokální IPv4. Čte `/sys/class/net` a `/proc/net/route`, IPv4 dotazem na jádro (`ioctl SIOCGIFADDR` přes nepřipojený socket, nic se neodesílá ani neotevírá). Rozhraní z `[network] interface`, jinak deterministický automatický výběr fyzického rozhraní (Docker, VPN, loopback a jiná virtuální se nevybírají ani nesčítají),
- `src/nexusstat/alarms.py`: vyhodnocení prahů WARNING/ERROR s hysterezí a potvrzením opakovaným měřením (3 měření pro vznik, 2 pro odeznění); jen stav, žádné automatické akce,
- `src/nexusstat/run.py`: pravidelné měření systému a sítě (výchozí interval 10 s, platnost výsledků 3 × interval) s jedním řádkem výpisu do terminálu na měření, ukončení Ctrl+C nebo SIGTERM (řízené, s dopsáním historie); selhání jednoho kolektoru nezneplatní druhý,
- `src/nexusstat/history.py` (M3.1a, jen infrastruktura): vytvoření, bezpečné otevření a ověření SQLite databáze schématu v1 z `docs/HISTORY.md` jen na výslovně zadané cestě a jen v roli `test` nebo `development` (bez vytváření adresáře a přepisu souborů), zámek jediného zapisovače, otevření jen pro čtení. Od M3.1b synchronní zapisovač `HistoryRecorder` (běh, transakční dávky s ochranou `run.last_batch`, vzorky jen z pevného katalogu, každá změna stavu a provozních údajů, události, horní mez po restartu, odmítnutí simulovaných dat a nedůvěryhodného času). Od M3.2a agregace uzavřených oken do `agg_5m` a `agg_1h` (`aggregate()` se stavem fronty `QueueState`, bez přepisu hotových oken). Od M3.2b retence s plánem bez zápisu (`retention_plan()`) a provedením po hodinových transakcích (`retain()`), jen pod potvrzenou agregací a jen se stabilním synchronizovaným časem (`ClockState`); běží jen v testech nad dočasnými databázemi. Aplikace do ní zatím nic neukládá a `run.py` ji nepoužívá,
- `src/nexusstat/history_worker.py` (M3.3a): jediné vlákno zapisovače historie s vlastním spojením SQLite, omezenými frontami měření, změn a událostí (přetečení se zahazuje nejstarší se záznamem ztráty), dávkami po 60 s, opakováním po přechodných chybách a stavovým snímkem `status()`; `submit()` hlavní vlákno nikdy nezdrží a od M3.3b-1 drží text jednoho výsledku do 512 B UTF-8 (delší zkrátí, stav, hodnotu ani identitu kontroly nezahodí). Od M3.3b-2 vlákno samo spouští údržbu: agregaci uzavřených 5min a hodinových oken po potvrzené dávce a retenci nejvýš jednou za hodinu, a to jen s doloženou stabilitou hodin (po startu procesu se prvních 24 h nemaže nic); zápis měření má vždy přednost a selhání údržby ho nezastaví,
- `src/nexusstat/history_link.py` (M3.3b-1): volitelné napojení historie na smyčku podle sekce `[history]`. Výchozí stav je vypnuto a cesta nemá výchozí hodnotu; zapnutá historie zapisuje do **už existující** databáze v roli `development` na výslovně zadané cestě (aplikace databázi nikdy nevytváří ani nepřepisuje; cestu na síťovém úložišti, pod `autofs` i cestu, jejíž úložiště nelze ověřit, odmítne, a to po rozvinutí symlinků). Do historie jdou finální vyhodnocené výsledky se skutečným monotónním časem měření, zápis běží v jiném vlákně a výpadek historie nezastaví měření ani alarmy; stav hlásí kontrola `history.storage` (`N/A`, `UNKNOWN`, `INFO`, `WARNING`, `ERROR`) včetně stavu automatické údržby (konflikt agregace, zastavená retence, velikost WAL, zpoždění agregace, důvod blokace retence),
- `src/nexusstat/history_cli.py` (M3.4b-2): příkazy správce `python3 -m nexusstat.history info | adopt | generations`. `info` jen čte stav databáze (identita, role, schéma, nevratný otisk stroje, značka obnovy, integrita, poslední běh, počty, zda je soubor vhodný k otevření na tomto stroji a zda vyžaduje ruční převzetí; `--no-integrity` vynechá kontroly integrity a výpis to přizná, `--immutable` je výslovné tvrzení o neměnné kopii — nikdy se nedovozuje z absence `-wal`), `adopt` ručně převezme obnovenou databázi na jiném stroji (vstup beze změny, výsledek jako nový soubor bez přepsání cíle, nezávislé ověření) a `generations` vypíše generace snímků s dry-run plánem retence. Nic nemaže a nic se neobnovuje automaticky,
- `src/nexusstat/snapshot_plan.py` (M3.4b-1): denní plán snímků z místního času `"HH:MM"` a výslovného časového pásma (přechody letního a zimního času podle pravidel pásma) a rozhodnutí, kdy o snímek požádat — nejvýš jeden pokus na plánovaný den, zmeškaný se dohání jen dnešní a existující novější snímek plán vyřizuje i po restartu. Plán vyhodnocuje měřicí smyčka svým tikem, takže nevzniká žádné aktivní dotazování ani další vlákno; stav hlásí kontrola `history.snapshot` (`HIST SNAPSHOT`) jen z údajů v paměti,
- `src/nexusstat/snapshot.py` a `src/nexusstat/storage.py` (M3.4a): neměnné generace konzistentních snímků SQLite (Backup API ze spojení vlákna zapisovače, úplná kontrola kopie, atomické zveřejnění celé generace jediným přejmenováním adresáře, které nenahradí žádný existující cíl, výběr poslední platné generace, test obnovitelnosti jen pro čtení) a společné kontroly cest a úložiště (symlinky, síťové systémy, shodný souborový systém, `fsync`). Snímek vzniká jen při doloženě synchronizovaném čase a nikdy nepřepíše starší generaci,
- `src/nexusstat/clock_sync.py` (M3.3b-1): read-only zjištění synchronizace systémových hodin (jediný zdroj je dotaz `adjtimex`) a `boot_id`. Vrací True, False, nebo None (nezjištěno); neznámý stav se nikdy nebere jako synchronizovaný a správný kalendářní rok není důkaz. Soubor systemd-timesyncd dokládá jen dřívější synchronizaci v tomto bootu, takže je pouze doplňující informací do výpisu a o stavu hodin nerozhoduje. Nic nenastavuje, žádná služba NTP se nespouští,
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
PYTHONPATH=src python3 -m nexusstat.run --config config/config.toml   # volitelná historie, postup v docs/OPERATIONS.md
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

- Schválené hodnoty prahů (vyhodnocování je ve výchozím stavu vypnuté), prahy pro CPU a RAM, kolektory NAS, Resticu, Dockeru, AnimeDB a služeb (stránka SERVICES zatím jen zobrazuje, co by dodaly), hardware drivery (OLED, RGB, tlačítko; živý simulátor je jen okno na notebooku), RGB signalizace a potvrzování závad (jen schválený návrh), web a nasazení SQLite historie na Nexusu (lokálně implementovaná v M3). Nic nebylo testováno na Raspberry Pi ani na hardwaru displeje.
- Velikost RAM a disku na OLED (`RAM 24% / 7.9G`): kolektor ji dává jen v českém textu `summary`, který displej nečte; zobrazení by vyžadovalo strukturovanou hodnotu kapacity v kolektoru (samostatný krok).
- Provozní záloha historie přes Restic a test obnovy na Nexusu. **Snímky** už od M3.4a existují lokálně: `snapshot.py` staví konzistentní kopii přes SQLite Backup API jako **neměnnou generaci** (databáze + JSON záznam v jednom adresáři), zveřejní ji jediným přejmenováním adresáře, vybírá poslední prokazatelně platnou generaci a umí test obnovitelnosti jen pro čtení; staré generace se zatím nemažou automaticky. Od M3.4b-1 se snímek dělá i podle denního plánu (`[history.snapshot]`, výchozí vypnuto, výslovné adresáře, denní čas `"HH:MM"` a časové pásmo) a jeho stáří hlásí kontrola `history.snapshot` (`INFO` do 26 h, `WARNING` do 50 h, `ERROR` nad ni; `N/A` při vypnutých snímcích). Na notebooku lze historii od M3.3b-1 volitelně zapnout (`[history]`, výchozí stav vypnuto) do vlastní databáze v roli `development` mimo repozitář; na Nexusu žádná databáze neexistuje a nasazení schválené není. Návrh M2 je v `docs/HISTORY.md`, benchmark nad syntetickými daty v `tools/`. Chybí dál kolektory dalších úložišť a stránka STORAGE (místo DISK), obojí jen návrh; CPU FAN a čtyřřádkový SYSTEM jsou od M4-1 hotové lokálně. Síťový kolektor a stránka NETWORK jsou od v0.6 implementované, ale jen lokálně na notebooku (nenasazeno, neověřeno na Raspberry Pi).
- Síť: žádné alarmové prahy (linka `DOWN` zůstává `INFO`), jen jedno rozhraní a jen jeho primární IPv4 (bez IPv6), procentní vytížení linky se nepočítá; adresa webového rozhraní se ukáže až s běžícím webem.
- Smyčka `nexusstat.run` vypisuje do terminálu a volitelně ukládá historii: nekreslí OLED, nezapisuje JSON ani logy a neběží jako služba. Měření je jednovláknové a kolektor nemá timeout, takže zaseknuté měření zastaví i výpis (zápis historie běží ve vlastním vlákně a smyčku nezdrží).
- Neexistuje provozní služba či nasazení na `nexus`.
- Není schválen přechod GPIO6 ani vypnutí Pironmanu/InfluxDB.
- Na Nexusu nebyla ověřena záloha ani obnova SQLite databáze NexusSTAT (lokálně jen test obnovitelnosti snímků nad dočasnými databázemi).
- Port 8765 je zatím jen záměr. Veřejný GitHub repozitář slouží pouze ke správě zdrojového kódu; neznamená nasazení ani zálohu.

## Orientace

- `manifest.toml`: plánovaný a ověřený stav projektu.
- `docs/REQUIREMENTS.md`: schválené a otevřené požadavky.
- `docs/ARCHITECTURE.md`: hranice procesu, hardware, síť a data.
- `docs/HISTORY.md`: návrh SQLite historie (M2): schéma, zápis, retence, záloha a obnova, benchmark; implementováno lokálně v M3, nenasazeno.
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
