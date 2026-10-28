# NexusSTAT – provozní postup (zatím návrh)

## Skutečný stav

Na Nexusu zatím neběží žádný kód, service ani port. Není co instalovat, spouštět či zastavovat. Příkazy v sekcích o Nexusu níže nejsou příkazem k nasazení.

## Lokální vývoj (simulace, read-only kolektory a pravidelné měření)

Pouze na vývojovém počítači. Kód nepřistupuje k GPIO, I²C, SPI, tlačítku ani NAS a nic neposílá do sítě. Systémový kolektor jen čte `/proc/stat`, `/proc/meminfo`, `/sys/class/thermal` a `os.statvfs("/")`. Síťový kolektor (v0.6) jen čte `/sys/class/net` a `/proc/net/route` a lokální IPv4 zjistí dotazem na jádro (`ioctl SIOCGIFADDR` na nepřipojeném socketu: žádný paket, žádný port, žádné připojení). Nic nezapisují a nespouštějí žádné příkazy. Výjimkami jsou volitelný healthcheck AnimeDB (SERVICES-1, níže): jen se zapnutým `[services.animedb]` pošle jeden HTTP `GET` na nastavenou adresu `/health`, a volitelná kontrola NAS (SERVICES-2, níže): jen se zapnutým `[services.nas]` jeden pokus o spojení TCP na port SMB, který se hned zavře. Obě jsou ve výchozím stavu vypnuté a nikam se nepřipojují.

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m nexusstat.simulate /tmp/nexusstat-sim
PYTHONPATH=src python3 -m nexusstat.simulate /tmp/nexusstat-live --live
PYTHONPATH=src python3 -m nexusstat.system_monitor
PYTHONPATH=src python3 -m nexusstat.network_monitor
PYTHONPATH=src python3 -m nexusstat.network_monitor eth0
PYTHONPATH=src python3 -m nexusstat.run
PYTHONPATH=src python3 -m nexusstat.run --config config/config.example.toml
```

`nexusstat.run` měří systém a síť v popředí terminálu každých `[collector] interval_seconds` (výchozí 10 s) a vypisuje jeden řádek na měření. Ukončení Ctrl+C **nebo SIGTERM** (kód 0, od M3.3b-1); neplatná nebo chybějící konfigurace skončí kódem 2 bez měření. Neběží jako služba, neotevírá port a bez sekce `[history]` nezapisuje žádné soubory. Interval i prahy jde ověřit vlastní kopií konfigurace mimo Git, např. `config/config.toml` (ignorováno v `.gitignore`).

Alarmové prahy (`[alarms.cpu_temp]`, `[alarms.disk_root]`) jsou ve výchozím stavu vypnuté a nemají výchozí hodnoty. Zapnutí vyžaduje `enabled = true` a všechny tři hodnoty `warning`, `error`, `hysteresis`; neplatná kombinace ukončí program kódem 2. Konkrétní hodnoty pro Nexus zatím nejsou schválené, pro lokální zkoušku používej jen zjevně ilustrační prahy. Alarm pouze změní stav a ve výpisu označí hodnotu (`[WARNING]`, `[ERROR]`); nic nevypíná, neřídí ventilátory ani neposílá zprávy. Změna prahů se projeví až po novém spuštění (stav potvrzení začne znovu).

Ventilátor CPU (M4-1, jen čtení): `python3 -m nexusstat.fan_monitor` vypíše skutečný stav tohoto stroje. Kolektor čte jen `/sys/class/hwmon/hwmon*` (`name`, `fanN_input`, `fanN_label`, `pwmN`) a `/sys/class/thermal/cooling_device*` (`type`, `cur_state`, `max_state`); **nikdy nic nezapisuje do sysfs, neřídí PWM ani ventilátor** — chlazení dál řídí Linux. Řádek `CPU FAN` na stránce SYSTEM znamená:

- `ON 3200` — **doložené otáčení** z tachometru; číslo za stavem jsou naměřené otáčky,
- `OFF` — **doložené stání** (naměřené 0 RPM); `OFF` při nízké teplotě není závada,
- `REQ ON` / `REQ OFF` — stav je znám **jen z požadavku řízení** (hwmon `pwmN`, nebo chladicí zařízení kernelu typu `pwm-fan`), ne z otáček; otáčky se z PWM nikdy neodhadují a žádné procento se nezobrazuje,
- `UNKN` — zdroj existuje, ale data jsou nečitelná, nevěrohodná, prošlá, nebo je zdrojů víc a žádný není prokazatelně hlavní,
- `N/A` — na tomto stroji **není prokazatelný zdroj** (není co sledovat, tedy ani závada). Tak to vypadá i na vývojovém notebooku, který žádný ventilátor v hwmon nemá; co se našlo a nepřiřadilo, uvádí popis výsledku.

Zařízení se vybírá podle identity (`name` ovladače, například `pwmfan`, nebo `fanN_label` od zařízení), nikdy podle čísla `hwmonX` ani podle jména souboru. Zdroje Raspberry Pi 5 je nutné read-only ověřit na Nexusu (M7); pravidlo o závadě při rozporu „požadavek > 0, otáčky 0“ zatím neexistuje a kolektor žádnou závadu nevyhodnocuje.

Síťové rozhraní (v0.6): bez `[network] interface` se vybírá automaticky (fyzické s výchozí trasou IPv4 a nejnižší metrikou → fyzické s linkou UP → jakékoli fyzické, při shodě abecedně; Docker, VPN, loopback ani jiná virtuální rozhraní ne). Pro lokální zkoušku jiného rozhraní stačí `python3 -m nexusstat.network_monitor <rozhraní>` nebo vlastní `config/config.toml` mimo Git. Neexistující rozhraní je `UNKNOWN` („rozhraní nenalezeno“), neplatný název ukončí program kódem 2. První měření rychlosti RX/TX je vždy `UNKNOWN`. Výpis obsahuje lokální IPv4 notebooku: do dokumentace, issue ani veřejného repozitáře ji nekopíruj (příklady používají dokumentační rozsah 192.0.2.0/24).

SQLite historii (`src/nexusstat/history.py`, základ M3.1a, synchronní zápis M3.1b, agregace M3.2a a retence M3.2b, a vlákno zapisovače `history_worker.py` M3.3a) používají mimo volitelnou zkoušku na notebooku (níže) jen testy: databáze v testech vznikají výhradně v `tempfile.TemporaryDirectory`. Modul nemá výchozí cestu, nevytváří adresáře, nepřepisuje soubory a produkční roli odmítá; vedle databáze nechává jen soubor zámku `<jméno>.lock`. Po pádu uprostřed vytváření může zůstat dočasný soubor `.<jméno>.*.creating` (s `-journal`); nemaže se automaticky a smí se smazat ručně, když žádné vytváření neběží. Druhé jméno po pádu těsně po zveřejnění odstraní příští `open_writer` sám. Zapisovač (`HistoryRecorder`) po nečistém ukončení při dalším startu běh uzavře jako nečistý a zapíše neznámou mezeru; nic se nedoplňuje. Agregace se po pádu dokončí znovu od potvrzeného `done_until`; při konfliktu (okno už existuje, např. po ručním zásahu) se zastaví s událostí `history.aggregation_conflict` a nic nepřepíše, řešení je na správci. Retence (M3.2b) maže nevratně; automaticky ji od M3.3b-2 spouští jen vlákno zapisovače se zapnutou historií na notebooku (podmínky níže), na Nexusu neběží a produkční databáze neexistuje. Smaže jen data starší než schválená doba a pod potvrzenou agregací, a jen se stabilním synchronizovaným časem; začátek zachované historie je v `meta` (`retained_since.<tabulka>`; mezery v měření po něm zůstávají mezerami). Soubor se po retenci nezmenšuje (volné stránky se znovu použijí); `VACUUM` se nespouští. Žádná aplikace ani služba ho nespouští.

Vlákno zapisovače (M3.3a) spouští od M3.3b-1 i `nexusstat.run` se zapnutou sekcí `[history]` (viz níže); jinak jen testy. Pro provoz platí: při chybě zápisu drží data jen v paměti (omezené fronty, nejvýš hodina) a opakuje zápis s odstupem; nikdy nevytvoří novou databázi. Po ztrátě práva zápisu je potřeba vrátit práva souboru databáze **i** jeho `-wal` a `-shm` (SQLite je mohlo mezitím vytvořit jen pro čtení). Poškozený nebo cizí soubor zapisovač zastaví (`FAILED`) a soubor nemění. Ukončení čeká nejvýš 10 s; zápis zaseknutý v systémovém I/O ukončit nejde, zámek pak uvolní až konec procesu a nepotvrzená data se ztratí (příští start je označí neznámou mezerou).

### Healthcheck AnimeDB (SERVICES-1, volitelný, výchozí stav vypnuto)

Bez sekce `[services.animedb]` (nebo s `enabled = false`) se kolektor nevytvoří a nikam se nepřipojuje; AnimeDB je na SERVICES `N/A`. Zapnutí jen ve vlastní kopii konfigurace mimo Git (`config/config.toml`), protože obsahuje skutečnou adresu:

```toml
[services.animedb]
enabled = true
url = "http://192.0.2.10:8080/health"   # skutečnou IP a port jen v lokální konfiguraci
timeout_seconds = 2
```

- Adresa jen `http://<IP adresa>[:port]/…/health` (název hostitele ne: DNS nemá časový limit), bez přihlašovacích údajů, query a fragmentu; neplatná adresa ukončí program kódem 2.
- Při každém měření (výchozí po 10 s) jediný `GET` bez proxy z prostředí a bez následování přesměrování; spojení, odeslání i příjem mají dohromady nejvýš `timeout_seconds` (výchozí 2 s, nejvýš 5 s). Jednovláknovou smyčku i okno náhledu zdrží nejvýš o tuto dobu; ostatní kolektory měří normálně.
- Výpadek (odmítnuto, timeout, přerušeno, nedostupná adresa, HTTP 5xx) se ukáže jako `ERROR` až po 3 po sobě jdoucích kontrolách; první dvě jsou `UNKNOWN` (`ANIMEDB FAILING`) bez blikání, takže restart AnimeDB kratší než asi 20 s alarm nevyvolá. Výpadek odezní po 2 úspěšných kontrolách (mezi nimi `ANIMEDB RECOVER`). Neplatná nebo nečekaná odpověď je `UNKNOWN` (`ANIMEDB INVALID`). Význam stavů je v `docs/REQUIREMENTS.md` (Healthcheck AnimeDB).
- Výsledek `service.animedb` jde do řádku `nexusstat.run` (např. `AnimeDB: /health odpovídá, status ok`, `AnimeDB: spojení odmítnuto (výpadek zatím nepotvrzen, 1/3)`), se zapnutou historií do SQLite jako změna stavu (bez číselné řady), v živém náhledu LIVE na SERVICES, do STATUS, na symbol stránky NEXUS a do simulovaných LED. Adresa se do výpisu ani historie nezapisuje.
- Nepotřebuje Docker, jeho socket ani skupinu `docker`; nic se nespouští ani neinstaluje.
- Návrat: `enabled = false` nebo odstranění sekce a nové spuštění; nic jiného se nemění.

Ověřeno jen testy nad syntetickými odpověďmi HTTP (`tests/test_animedb_monitor.py`); proti běžící AnimeDB kolektor zatím nebyl spuštěn.

### Kontrola dostupnosti NAS (SERVICES-2, volitelná, výchozí stav vypnuto)

Bez sekce `[services.nas]` (nebo s `enabled = false`) se kolektor nevytvoří a nikam se nepřipojuje; NAS je na SERVICES `N/A` (do SERVICES-2 `UNKN`). Zapnutí jen ve vlastní kopii konfigurace mimo Git (`config/config.toml`), protože obsahuje skutečnou adresu:

```toml
[services.nas]
enabled = true
host = "192.0.2.20"   # skutečnou IP NAS jen v lokální konfiguraci
port = 445
timeout_seconds = 2
```

- `host` jen číselná IPv4 nebo IPv6 adresa (název hostitele ne: DNS nemá časový limit; bez zóny `%rozhraní`, ne `0.0.0.0` ani `::`), `port` celé číslo 1–65535, `timeout_seconds` v rozsahu (0, 5] s; neplatná hodnota ukončí program kódem 2.
- Při každém měření (výchozí po 10 s) **jediný pokus o spojení TCP**, který se po navázání hned zavře. Žádné přihlášení SMB, procházení sdílených složek, čtení souborů, SNMP, DSM API ani SSH; síťové mounty NAS a `autofs` se neotevírají, takže kontrola automatické připojení nespustí. Pokus trvá nejvýš `timeout_seconds` (výchozí 2 s); jednovláknovou smyčku zdrží nejvýš o tuto dobu, se zapnutou AnimeDB se limity sčítají (timeouty proto drž výrazně pod `[collector] interval_seconds`). Čas a platnost výsledků se počítají od začátku měření; měření delší než interval se provádějí hned po sobě (zmeškaná se nedohánějí) a měření delší než platnost výsledků (3 × interval) výpadek nikdy nepotvrdí, NAS i AnimeDB pak zůstanou `UNKNOWN`. Ctrl+C a SIGTERM přeruší i čekající spojení.
- `OK` znamená jen „služba SMB přijímá spojení“: **ne** dostupnost všech sdílených složek, čitelnost dat, možnost zapisovat zálohy ani zdraví hardwaru NAS.
- Výpadek (odmítnuto, timeout, nedostupná síť nebo adresa) se ukáže jako `ERROR` až po 3 po sobě jdoucích kontrolách (`NAS OFFLINE`); první dvě jsou `UNKNOWN` (`NAS FAILING`) bez blikání. Výpadek odezní po 2 úspěšných kontrolách (mezi nimi `NAS RECOVER`). Chyba na této straně (např. nedostatek deskriptorů) je `UNKNOWN` (`NAS NO DATA`), nikdy výpadek ani OK. Význam stavů je v `docs/REQUIREMENTS.md` (Kontrola dostupnosti NAS).
- Výsledek `service.nas` jde do řádku `nexusstat.run` (např. `NAS: služba SMB přijímá spojení (port 445)`, `NAS: spojení odmítnuto (výpadek zatím nepotvrzen, 1/3)`), se zapnutou historií do SQLite jako změna stavu (bez číselné řady), v živém náhledu LIVE na SERVICES, do STATUS, na symbol stránky NEXUS a do simulovaných LED (kód 2). Adresa se do výpisu ani historie nezapisuje.
- NAS může v logu vidět krátké spojení z Nexusu při každém měření; s přihlášením to nesouvisí.
- Návrat: `enabled = false` nebo odstranění sekce a nové spuštění; nic jiného se nemění.

Ověřeno jen testy nad podvrženým spojením (`tests/test_nas_monitor.py`); proti skutečnému NAS kontrola zatím nebyla spuštěna. Dostupnost sdílených složek SMB a ověřování záloh Restic zatím **nejsou implementované**.

### Zkouška historie na notebooku (M3.3b-1, volitelná, výchozí stav vypnuto)

Bez sekce `[history]` je chování shodné s v0.6: nic se neukládá. Zapnutí je výslovné rozhodnutí a vyžaduje už existující databázi v roli `development`, kterou vytvoří správce mimo repozitář (`.gitignore` chrání `*.sqlite3`, cesty Nexusu se nepoužívají):

```bash
mkdir -p ~/.local/state/nexusstat
PYTHONPATH=src python3 -c "from pathlib import Path; from nexusstat import history; history.create_database(Path.home() / '.local/state/nexusstat/history.sqlite3', 'development', history.local_host_id())"
PYTHONPATH=src python3 -m nexusstat.run --config config/config.toml
```

`config/config.toml` (mimo Git) obsahuje jen `[history] enabled = true` a absolutní `path` k tomuto souboru; vzor je v `config/config.example.toml`. Co platí za běhu:

- Výpis má navíc pole `HISTORY: …` se stavem kontroly `history.storage`; jeho stav se počítá do celkového stavu řádku. `N/A` = vypnuto, `UNKNOWN` = zapisovač ještě neotevřel databázi, `INFO` = zapisuje, `WARNING` = přechodná chyba, čekání na synchronizaci hodin, neznámý stav hodin, zkrácený text nebo zápis nepotvrzený déle než 180 s (typicky zaseknuté I/O), `ERROR` = ztráta dat, plná fronta nebo zastavený zapisovač. `CRITICAL` historie nikdy nehlásí.
- Ukončení (Ctrl+C i SIGTERM) dopíše fronty s limitem 10 s a vypíše, co je potvrzené a co ne. „Čisté ukončení zapsáno“ se objeví jen po potvrzeném zápisu; jinak výpis uvede počty nezapsaných a nejistých položek a stav zapisovače. Zápis zaseknutý v systémovém I/O přerušit nelze: pak zámek drží až do konce procesu a chybí poslední dávka (příští start ji označí neznámou mezerou).
- Databáze se nikdy nevytváří ani nepřepisuje. Chybějící soubor, zámek jiného procesu nebo odmítnuté úložiště dají `WARNING` a opakování s odstupem; cizí nebo poškozený soubor `ERROR` a zastavení zápisu bez jakékoli změny souboru. Měření, prahy a výpis běží ve všech těchto případech dál.
- Úložiště se ověřuje při každém otevření a cesta se nejdřív rozvine (symlinky, `..`), takže rozhoduje skutečné místo souboru, ne zápis cesty. Odmítne se síťové úložiště (CIFS, NFS, 9p, sshfs… i bind mount z nich), `autofs` a také stav, kdy úložiště ověřit nelze (nečitelná tabulka `/proc/self/mounts`, cesta mimo všechny body připojení). Pro zkoušku proto volte běžný lokální adresář v domovském adresáři, ne připojený NAS ani automaticky připojovaný adresář.
- Vedle databáze vzniká zámek `history.lock` a soubory `-wal`/`-shm`. Smazat se smí jen při zastaveném procesu, a `-wal` nikdy odděleně (obsahuje potvrzené transakce).
- Agregace uzavřených 5min a hodinových oken a retence běží automaticky ve vlákně zapisovače (M3.3b-2): agregace po každé potvrzené dávce (jinak nejvýš jednou za minutu), retence nejvýš jednou za hodinu. Zápis měření má vždy přednost; přechodné selhání údržby měření nezastaví a vypíše se v poli `HISTORY`.
- **Retence maže nevratně, ale jen s doloženými podmínkami:** po startu procesu se prvních 24 h nemaže nic. Doba souvislé synchronizace se dokládá **jen z průběžných pozorování** (rozsah mezi prvním a posledním potvrzením nepřerušené řady), takže zastavená měřicí smyčka retenci zablokuje; řadu přeruší ztráta synchronizace, neznámý stav, skok času, návrat z uspání i mezera v měření delší než `max(3 × interval, 300 s)`, a stejně tak nezapsaná ztráta důležité události (mohl to být doklad skoku hodin), maže se jen pod potvrzenou hranicí agregace a jen podle schválených dob uchování; nejvýš 24 transakcí za běh, každá nejvýš hodinu historie jedné tabulky. Důvod blokace (`clock_unstable`, `time_unsynced`, `clock_jump_recent`, …) je vidět v poli `HISTORY` a prvních 24 h to **není závada**. `VACUUM` se nespouští, soubor se po mazání nezmenší.
- Snímky vznikají jen se zapnutou sekcí `[history.snapshot]` (M3.4b-1, výchozí vypnuto, viz SQLite: provoz a zálohování); Restic se nespouští.

Stav synchronizace hodin se zjišťuje jen čtením a rozhoduje o něm jediný zdroj: `adjtimex` (dotaz `modes = 0`, rozhoduje kernelová mez chyby hodin pod 16 s, stejné kritérium jako `timedatectl` `NTPSynchronized`). Nic se nenastavuje a žádná služba NTP se nespouští. Soubor `/run/systemd/timesync/synchronized` se vypisuje jen jako doplňující informace („první synchronizace v tomto bootu doložena/nedoložena“): dokládá dřívější synchronizaci, po pozdější ztrátě nemizí, a proto o aktuálním stavu hodin nerozhoduje. Bez použitelné odpovědi jádra je stav neznámý a nikdy se nebere jako synchronizovaný: data pak čekají ve frontě a po první doložené synchronizaci dostanou odvozený čas (`derived`). Ztrátu synchronizace jádro přizná až po narůstu meze chyby zpět na 16 s (řádově hodiny), ne hned. Pro retenci se navíc počítá jen průběžně potvrzovaná synchronizace: po delší pauze v měření (a po uspání notebooku) začíná 24hodinová lhůta znovu. Mez `max(3 × interval, 300 s)` je **tolerance mezery mezi dotazy na zdroj**, ne nepřetržitý důkaz, že NTP celou dobu drželo; samostatná frekvence ověřování pro provoz je otevřený bod M7.

Benchmark návrhu SQLite (M2, `tools/sqlite_history_benchmark.py`) není součástí aplikace: syntetické databáze zapisuje jen do nového dočasného adresáře (`TMPDIR`, jinak `/tmp`, nebo `--dir`) a na konci ho smaže; plný běh potřebuje asi 1 GB volného místa a trvá řádově minuty, `--quick` kolem minuty. Nepoužívá síť, kód aplikace ani cesty Nexusu.

```bash
python3 tools/sqlite_history_benchmark.py --quick
```

Simulátor kreslí i stránku SERVICES: ve scénáři s fiktivními stavy a značkou `SIM`, v režimu `--live` bez kolektorů služeb: NEXUS, NAS a BACKUP jsou `UNKNOWN` na SERVICES (`UNKN`) i ve STATUS (`NAS NO DATA`) a stránka NEXUS ukazuje `?`; AnimeDB je `N/A`, se zapnutým `[services.animedb]` (a `--config`) výsledek jednoho healthchecku (bez potvrzování, které potřebuje víc kontrol). Snímky ukládej mimo repozitář (např. do `/tmp`).

### Živý simulátor OLED a RGB LED (v0.5, RGB-1)

Jen na vývojovém notebooku s grafickým prostředím. Potřebuje Pillow a tkinter. Na vývojovém notebooku je od 2026-10-19 nainstalovaný systémový balíček `python3-tk` 3.12.3 se závislostmi `libtk8.6`, `tk8.6-blt2.5` a `blt` (Tk 8.6.14); na jiném počítači `sudo apt install python3-tk`, systémová instalace jen po schválení. `PIL.ImageTk` (`python3-pil.imagetk`) potřeba není: obraz se do okna předává jako PNG.

```bash
PYTHONPATH=src python3 -m nexusstat.preview
```

```bash
PYTHONPATH=src python3 -m nexusstat.preview --scenario many --scale 6 --config config/config.example.toml
```

```bash
PYTHONPATH=src python3 -m nexusstat.preview --scenario rgb_acknowledged
```

- Ovládání: „Stisk“, mezerník nebo Enter = krátký stisk fyzického tlačítka (skutečná logika `OledController`); nabídka scénářů; rychlost 1×/5×/10× (jen simulace); Esc, křížek nebo Ctrl+C v terminálu okno čistě zavřou (žádná vlákna ani procesy na pozadí).
- RGB (RGB-1): pod displejem jsou čtyři simulované LED (vždy stejná barva, počet bliknutí = kategorie) a pod nimi popis signálu, jasu a potvrzených závad. „Potvrdit WARNING/ERROR“ potvrdí všechny aktivní `WARNING`/`ERROR`, „Zrušit potvrzení“ je vrátí (jen pro zkoušení) a posuvník „Jas LED %“ nastaví 5–100 % (výchozí 20 %); nic se neukládá. Jednorázové přehrání potvrzené závady je vidět po probuzení OLED, např. `--scenario rgb_acknowledged` a pak Stisk; přepnutí scénáře při rozsvíceném OLED probuzení není. Při 5×/10× LED neblikají a ukazují barvu vzoru trvale. Tlumený jas je na monitoru zobrazený s gama korekcí; skutečné LED se ověří až na hardwaru.
- Displej je anglicky (texty z `oled_text.py`), ovládání okna česky.
- Pod displejem je režim (SLEEP / CAROUSEL – stránka / STATUS – stránka x/y) a zdroj dat: „SIMULACE: fiktivní data“ nebo „LIVE: měření tohoto NOTEBOOKU, ne Nexusu“.
- Scénáře: 1 Vše v pořádku, 2 WARNING (teplota), 3 ERROR a CRITICAL, 4 UNKNOWN (neplatná, chybějící a po 20/40 s zastaralá data), 5 Více problémů (2 stránky STATUS), 6 Nenapojené služby (NEXUS, NAS, BACKUP `UNKNOWN`, AnimeDB `N/A`), 7–11 NETWORK (v0.6): linka DOWN, bez IPv4, nízký provoz, vysoký provoz, neplatná a po 20 s zastaralá data, 12–17 RGB (NAS WARNING, AnimeDB ERROR, zálohy ERROR, několik závad, CRITICAL a WARNING, potvrzená závada). Změna scénáře začne data, paměť alarmů, potvrzení LED i okno kolotoče znovu; rozsvícený displej zůstane rozsvícený od stránky NEXUS.
- LIVE: `SystemMonitor`, `NetworkMonitor` (rozhraní z `[network]`) a `AlarmEvaluator` (prahy z `--config`) v intervalu `[collector]`, vždy 1×, bez značky `SIM`. Měření běží v okně (typicky jednotky ms); zaseknuté čtení `/` by okno zablokovalo stejně jako `nexusstat.run`. První vzorek CPU je `UNKNOWN`. Se zapnutým `[services.animedb]` se přidá healthcheck AnimeDB (okno zdrží nejvýš `timeout_seconds`) a popis pod displejem to uvádí; jinak „AnimeDB N/A (sledování vypnuto)“.
- Bez tkinter skončí příkaz kódem 2 se zprávou, bez grafického displeje kódem 1.

Úspěšné testy dokládají logiku automatu, validaci, výpočty kolektorů nad vzorovými vstupy (síťové stavy výhradně ve fixturách, nezávisle na síti počítače), plánování smyčky a vyhodnocení prahů se simulovaným časem a hodnotami a vykreslení do obrázku. Nedokládají vhodnost konkrétních prahů pro Nexus. Nedokládají funkci displeje, tlačítka ani správnou volbu teplotního senzoru na Raspberry Pi 5; to vyžaduje samostatné schválené read-only ověření na cílovém stroji. Ruční běh `nexusstat.run` dokládá jen funkci na notebooku.

### Návratový postup lokálního vývoje

Projekt má lokální Git repozitář (větev `main`, `origin` = veřejný GitHub). Návratovým bodem lokálních změn je poslední commit (`git status`, `git diff`, `git restore <soubor>`); před prvním commitem žádný návratový bod neexistuje. Commit i push jen po schválení. GitHub není ověřenou zálohou: necommitované soubory a vše vyloučené v `.gitignore` (lokální konfigurace, DB, logy) na něm nejsou. Na Nexusu není co vracet, protože nic nebylo nasazeno.

## Bezpečné předběžné kontroly (read-only)

Na cílovém Nexusu před nasazením znovu ověř:

- `systemctl show pironman5.service -p ActiveState -p SubState -p ExecStart`
- `systemctl show <timer-zálohy>.timer -p ActiveState -p TimersCalendar` pro denní zálohu i týdenní údržbu Resticu
- `findmnt -M <mountpoint-NAS> -o TARGET,FSTYPE -n` pro každý připojený NAS

Skutečné názvy jednotek a cesty mountů jsou v soukromé dokumentaci Nexusu, ne v tomto repozitáři.

Kontrola mountu nikdy není náhradou za ověřený read-only test dostupnosti NAS s timeoutem. Nespouštěj restic backup, retention ani test zápisu bez schválení.

Pro budoucí kolektory (M4, M7) a jen po schválení read-only ověřit zdroje dat na RPi 5, např. typy a stav `/sys/class/thermal/cooling_device*` (`type`, `cur_state`, `max_state`), dostupnost otáček v `/sys/class/hwmon/hwmon*/` (`name`, `fan*_input`) a rozhraní v `/sys/class/net/*/operstate` (název ethernetu na RPi 5 s Debianem 13, např. `eth0` nebo `end0`, a zda automatický výběr zvolí správné rozhraní). Nic se nezapisuje; GPIO se ani nečte bez samostatného schválení. Pro healthcheck AnimeDB (SERVICES-1) až po nasazení AnimeDB ručně jen `curl --noproxy '*' --max-time 2 http://<adresa>/health` (očekávané HTTP 200 a `{"status":"ok"}`), bez zásahu do AnimeDB.

## Před přechodem na hardware

1. Ověřit originální konfiguraci, reálné řízení bočních ventilátorů, GPIO6 polaritu a chování při bootu.
2. Připravit zálohu dotčených konfigurací a testovaný rollback. Fyzický dohled a teplotní kontrola jsou povinné.
3. Předem stanovit kritéria úspěchu a co dělat při selhání přepnutí. Při nejistotě Pironman nevypínat.
4. Tlačítko: ověřit výchozí logind akce a evdev grab; neimplementovat konfliktní dlouhé držení.
5. InfluxDB nevypínat, dokud nejsou doložené všechny závislosti a zálohy. NexusSTAT ji nepotřebuje; před vypnutím read-only zjistit, kdo ji používá (klienti, dashboardy, skripty), a zálohovat data.
6. Pironman nahrazovat po funkcích (OLED, RGB, tlačítko, ventilátory), u každé s vlastním kritériem úspěchu. Pironman při přechodu jen zastavit a zakázat, neodinstalovat; návrat = obnovit zálohované konfigurace a znovu povolit a spustit `pironman5.service`. Postup návratu vyzkoušet předem.
7. RGB (návrh): read-only zjistit, jak Pironman LED ovládá (zařízení, zapojení, jeho konfigurace), a konfiguraci zálohovat. Vypnout jen RGB Pironmanu jeho vlastní volbou; kvůli RGB nikdy nezastavovat `pironman5.service`, protože řídí boční ventilátory. LED smí ovládat jen jeden program, teprve potom zapnout RGB NexusSTAT. Pironman ovládá RGB až do samostatně schváleného převzetí. Návrh kritéria úspěchu: všechny stavy z tabulky v sekci RGB LED fyzicky ověřené a LED zhasnuté po startu i po korektním zastavení NexusSTAT; po pádu procesu mohou WS2812 držet poslední barvu (provozní omezení). Návrat: vypnout RGB NexusSTAT a obnovit konfiguraci RGB Pironmanu.

## RGB LED a potvrzení závad (schválený návrh 2026-10-23, neimplementováno)

Na Nexusu nic z toho neběží: LED ovládá Pironman a NexusSTAT nemá ovladač RGB, web ani trvale uložená potvrzení. Na notebooku lze chování vyzkoušet v živém náhledu (RGB-1, viz Živý simulátor OLED a RGB LED). Požadavky a rozhodnutí jsou v `REQUIREMENTS.md`, datové vazby v `ARCHITECTURE.md`. Budoucí význam LED pro správce:

| LED | Význam |
| --- | --- |
| zhasnuté, OLED spí | žádná **nepotvrzená** závada; neznamená OK (může trvat potvrzená závada nebo chybět data), stav ukáže OLED po stisku nebo web |
| tlumeně bílé, OLED probuzený | zdravý ověřený stav: všechny skutečně monitorované kontroly mají platný stav `OK` nebo `INFO` |
| zhasnuté, OLED probuzený | trvá potvrzená závada (po jednom přehrání jejího kódu), nebo skutečně monitorovaná kontrola nemá platná data (OLED ukazuje `?`) |
| oranžové skupiny po N bliknutích (0,4 s / 0,4 s, pauza 2 s) | nepotvrzená `WARNING` v kategorii N |
| červené skupiny po N bliknutích (0,4 s / 0,4 s, pauza 2 s) | nepotvrzená `ERROR` v kategorii N |
| nepřetržité červené 0,25 s / 0,25 s | `CRITICAL`; nejde umlčet potvrzením ani jasem |
| jakýkoli svit po pádu NexusSTAT | nic nedokládá: WS2812 drží poslední barvu (provozní omezení M7) |

Kategorie: 1 Nexus a systém, 2 NAS a jeho dostupnost, 3 AnimeDB, 4 zálohy a Restic, 5 Docker a ostatní skutečně monitorované služby, sem i každá nezařazená skutečná závada. Čísla jsou kódy, ne čísla LED; všechny čtyři LED blikají společně.

- Potvrzení „Beru na vědomí / pracuji na nápravě“ jen eviduje, že o závadě víš: nic neopravuje, nemění diagnostický stav, OLED ani historii měření a přetrvá restart. Zanikne až po doloženém odeznění (nejméně tři po sobě jdoucí platná měření bez závady, nebo delší řada podle alarmového pravidla; `UNKNOWN` se nepočítá). Nová epizoda nebo zhoršení na `ERROR` chce nové potvrzení; `CRITICAL` bliká dál.
- Probuzení OLED kódy potvrzených závad jednou přehraje, pokud nebliká `CRITICAL` ani nepotvrzená závada; pak LED zhasnou. Další stisky mají dosavadní význam.
- Jediný jas 5–100 % (výchozí 20 %), bílá asi na polovině. Jas a potvrzení budou v samostatném malém souboru v `appdata/nexusstat/` (atomický zápis, soukromá oprávnění, zahrnutý do záloh); nastavovat je smí jen přihlášený správce v LAN.

## SQLite: provoz a zálohování (implementováno lokálně, na Nexusu nenasazeno)

Na Nexusu databáze NexusSTAT neexistuje. Návrh M2 je v `HISTORY.md` (schválený 2026-10-20 kromě R11, R13, R15 (odloženo do M7); nasazení neschváleno); lokálně je implementovaný v M3 a ověřený na notebooku nad dočasnými a vývojovými databázemi, na Nexusu se ověří až v M7 se schválením. Nic z toho dnes na serveru neexistuje.

- Umístění (návrh M2, uspořádání snímků podle M3.4a): živá databáze `/srv/nexus/appdata/nexusstat/history/history.sqlite3` s `-wal`, `-shm` a zámkem `history.lock`; rozpracované generace snímků v `snapshot-work/` (Restic nezálohuje) a hotové **neměnné generace** v `snapshot/<generace>/` (jméno = čas snímku), každá s `history-snapshot.sqlite3` a `history-snapshot.json` (identita generace, čas, SHA-256, velikost, počty řádků, schéma). Lokální SSD, nikdy NAS/CIFS: zamykání souborů přes síť není spolehlivé a výpadek NAS by zastavil zápis. Všechny adresáře vytváří správce, aplikace nikdy.
- Režim WAL (`synchronous=FULL`, dávka 60 s): čtení webem neblokuje zápis. Soubory `-wal` a `-shm` patří k databázi. Viz [WAL](https://www.sqlite.org/wal.html).
- Jediný zapisovač (`HISTORY.md`, sekce 5.8): zapisuje a checkpoint dělá jen NexusSTAT. Za jeho běhu živou databázi nikdy neotvírat jiným nástrojem pro zápis (`sqlite3` jen s `-readonly`, raději snímek); převzetí souboru (`adopt`) a obnova jen se zastaveným NexusSTAT. Důvodem je mimo jiné chyba SQLite „WAL-reset“, jejíž oprava je na notebooku (`libsqlite3-0 3.45.1-1ubuntu2.8`) i v Debianu 13 (`3.46.1-7+deb13u1`) neprokázaná (UNKNOWN). Podmínky pro M3 a pro provoz: `HISTORY.md`, sekce 12.3 a 12.4.
- **Samotné kopírování aktivního souboru SQLite nemusí být konzistentní záloha**: během kopírování může probíhat zápis a poslední potvrzené změny mohou být jen ve `-wal`. Restic proto živou databázi přímo nezálohuje: vyloučit celý `history/`, zahrnout `snapshot/`.
- Konzistentní kopie přes SQLite Backup API ([Backup API](https://www.sqlite.org/backup.html), v Pythonu `sqlite3.Connection.backup`): vytvoří ji sám NexusSTAT ve vlákně zapisovače do rozpracované generace v `snapshot-work/` (mimo zálohu), převede na samostatný soubor bez WAL (`journal_mode=DELETE`), **úplně ověří** (integrita, cizí klíče, identita, schéma, počty řádků, SHA-256), zapíše JSON záznam a celou generaci zveřejní **jediným přejmenováním adresáře** do `snapshot/`, které nenahradí žádný existující cíl (`renameat2(RENAME_NOREPLACE)`; bez této záruky úložiště se nezveřejní nic) (`HISTORY.md` 9.2). Neúspěšný snímek nic nepřepíše a starší generace zůstávají; nedokončená dvojice se nikdy nezveřejní. Denní plánování i stav `history.snapshot` jsou od M3.4b-1 hotové lokálně: sekce `[history.snapshot]` (výchozí vypnuto) zadává oba adresáře, denní čas `"HH:MM"` a **výslovné** časové pásmo, plán vyhodnocuje měřicí smyčka (nejvýš jeden pokus na den, zmeškaný jen dnešní, přechody DST podle pravidel pásma) a snímek lze dál vyžádat i ručně. Adresáře se nikdy nevytvářejí a ověří se před prvním měřením; jejich nepoužitelnost historii nezastaví. Kontrola `history.snapshot` hlásí chybějící nebo starý snímek (`WARNING` nad 26 h, `ERROR` nad 50 h), protože Restic sám by úspěch hlásil i bez aktuální historie (9.3). Skript ani timer Resticu se nemění, jen jeho seznam cest (po schválení, se zálohou původní konfigurace).
- Nepřidávat další samostatný timer zálohování. Při nedostupném NAS selže jen Restic; snímek ani živá databáze se nemění.
- Test obnovy (`HISTORY.md`, sekce 9.5): obnovit `snapshot/` z Resticu do nového dočasného adresáře, vybrat generaci a ověřit ji. Na notebooku to dělá `snapshot.check_restore(<adresář generace>)` jen pro čtení: záznam a SHA-256, `integrity_check`, `foreign_key_check`, identita a schéma, `meta.snapshot_utc`, počty řádků proti záznamu a reprezentativní čtecí dotazy; živou databázi neotevírá a nic neobnovuje. `restore_tested=false` do skutečného úspěšného testu na Nexusu.
- Obnova po havárii (`HISTORY.md`, sekce 9.6): zastavit NexusSTAT, celý `history/` přesunout stranou včetně `-wal` a `-shm` (nesmí zůstat vedle obnoveného souboru), zkopírovat ověřený snímek, nastavit vlastníka a práva, na náhradním stroji soubor výslovně převzít (otisk stroje), spustit. NexusSTAT obnovený snímek rozpozná a zapíše začátek ztraceného úseku.
- Generace snímků se **automaticky nemažou** (mazání je M3.4b-3 a vyžaduje samostatné schválení): místo drží retence Resticu a zásah správce. Stav generací a dry-run plán retence vypíše `python3 -m nexusstat.history generations <adresář>`; nic nemaže a vždy hlásí, co ho blokuje (`HISTORY.md` 9.7).
- Stav souboru databáze (identita, role, schéma, integrita, značka obnovy, potřeba převzetí) zjistí `python3 -m nexusstat.history info <soubor>`. Příkaz **databázi nemění ani nečekpointuje** a uložený otisk stroje netiskne, jen jeho nevratný otisk.
- Příkaz `info` **jen čte**: běžným spojením jen pro čtení (zamyká, pozná změnu souboru a čekající rollback), nikdy ne přes `immutable=1` — absence `-wal` totiž nedokazuje, že databáze není živá, a `immutable=1` ignoruje horký `-journal`, takže by nedokončený stav vypadal jako v pořádku. U databáze **ve WAL** smí SQLite při čtení vytvořit prázdný `-wal` a `-shm` (index), obsah databáze se nemění a checkpoint neproběhne; u samostatného souboru v režimu `delete` nevznikne nic. Přepínač `--immutable` je **tvrzení správce**, že soubor se nemůže měnit (např. generace snímku na read-only médiu), a vedle existujícího `-wal` se odmítne. `--no-integrity` vynechá kontroly integrity a výpis to výslovně uvede — o poškození pak netvrdí nic.
- **Horký rollback žurnál** (`<soubor>-journal`): `info` ho nedokončí (to by byl zápis) a hlásí, že čeká nedokončené zotavení. Řešení je nechat soubor otevřít zapisovači, který zotavení provede, nebo žurnál i databázi odložit stranou — nikdy nemazat jen žurnál.
- **Bezpečný ruční postup převzetí obnovené databáze na jiném stroji** (offline, `HISTORY.md` 9.6):
  1. Zastavit NexusSTAT (žádný živý zapisovač nad vstupem).
  2. Vstupní soubor nechat **samostatný**: vedle něj nesmí být `-wal`, `-shm` ani `-journal`. Pokud tam jsou, celý adresář se odkládá stranou (nemazat jednotlivé soubory): obsah cizího WAL by se mohl přehrát do obnovené historie.
  3. `python3 -m nexusstat.history info <soubor>` — ověřit roli, schéma, integritu a že soubor vyžaduje ruční převzetí.
  4. `python3 -m nexusstat.history adopt <soubor> --output <nový soubor>` — vstup zůstane beze změny, výsledek vznikne jako nový soubor bez přepsání cíle a hned se nezávisle ověří. Odmítne produkční roli, zamčenou databázi, pomocné soubory, nedůvěryhodný čas i vstup, který se během kopírování změnil.
  5. Nový soubor nasadit jako `history/history.sqlite3` (vlastník, práva) a spustit NexusSTAT: obnovu zaeviduje první start (`history.restored` s původem a `history.unknown_gap` s hranicemi neznámého úseku).
- **Hranice ochrany při převzetí:** zámek NexusSTAT zachytí jen jeho vlastní zapisovače. Cizí program pracující přímo se SQLite ho nerespektuje; pozná se jen podle pomocných souborů (otevřená databáze ve WAL) nebo podle toho, že se vstup během kopírování změnil (kontroluje se SHA-256 před i po). **Výhradní přístup to nedokazuje** a kontrola SHA-256 není záruka konzistentní kopie živé databáze, proto je `adopt` operace nad prokazatelně odstaveným souborem. Vstup se nemění nikdy, takže nejhorším následkem souběhu je odmítnuté převzetí. Generace jsou neměnné — nikdy se nepřepisují ani nepřesouvají; při obnově se kopíruje jejich obsah.
- Růst: retenční mazání po hodinových dávkách; hlídat velikost souboru a volné místo (návrh: zápis se zastaví pod 1 GiB nebo 5 % volného místa). Při chybě zápisu nebo plném disku se zastaví jen ukládání historie (vlastní stav `history.storage` a událost), monitoring, alarmy a OLED běží dál.

## Po každé budoucí změně

Zaznamenej změněné soubory, výsledky testů, Git stav, rollback, případnou aktualizaci manifestu, operací a registru `PROJECTS.md`. Výsledky simulace jsou označené jako simulace; nikdy je nevydávat za funkci reálného HW.
