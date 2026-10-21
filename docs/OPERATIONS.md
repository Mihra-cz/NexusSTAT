# NexusSTAT – provozní postup (zatím návrh)

## Skutečný stav

Na Nexusu zatím neběží žádný kód, service ani port. Není co instalovat, spouštět či zastavovat. Příkazy v sekcích o Nexusu níže nejsou příkazem k nasazení.

## Lokální vývoj (simulace, read-only kolektory a pravidelné měření)

Pouze na vývojovém počítači. Kód nepřistupuje k GPIO, I²C, SPI, tlačítku ani NAS a nic neposílá do sítě. Systémový kolektor jen čte `/proc/stat`, `/proc/meminfo`, `/sys/class/thermal` a `os.statvfs("/")`. Síťový kolektor (v0.6) jen čte `/sys/class/net` a `/proc/net/route` a lokální IPv4 zjistí dotazem na jádro (`ioctl SIOCGIFADDR` na nepřipojeném socketu: žádný paket, žádný port, žádné připojení). Nic nezapisují a nespouštějí žádné příkazy.

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

`nexusstat.run` měří systém a síť v popředí terminálu každých `[collector] interval_seconds` (výchozí 10 s) a vypisuje jeden řádek na měření. Ukončení Ctrl+C (kód 0); neplatná nebo chybějící konfigurace skončí kódem 2 bez měření. Neběží jako služba, nezapisuje soubory, neotevírá port a SIGTERM zvlášť neobsluhuje (proces skončí výchozím způsobem, nic se neukládá). Interval i prahy jde ověřit vlastní kopií konfigurace mimo Git, např. `config/config.toml` (ignorováno v `.gitignore`).

Alarmové prahy (`[alarms.cpu_temp]`, `[alarms.disk_root]`) jsou ve výchozím stavu vypnuté a nemají výchozí hodnoty. Zapnutí vyžaduje `enabled = true` a všechny tři hodnoty `warning`, `error`, `hysteresis`; neplatná kombinace ukončí program kódem 2. Konkrétní hodnoty pro Nexus zatím nejsou schválené, pro lokální zkoušku používej jen zjevně ilustrační prahy. Alarm pouze změní stav a ve výpisu označí hodnotu (`[WARNING]`, `[ERROR]`); nic nevypíná, neřídí ventilátory ani neposílá zprávy. Změna prahů se projeví až po novém spuštění (stav potvrzení začne znovu).

Síťové rozhraní (v0.6): bez `[network] interface` se vybírá automaticky (fyzické s výchozí trasou IPv4 a nejnižší metrikou → fyzické s linkou UP → jakékoli fyzické, při shodě abecedně; Docker, VPN, loopback ani jiná virtuální rozhraní ne). Pro lokální zkoušku jiného rozhraní stačí `python3 -m nexusstat.network_monitor <rozhraní>` nebo vlastní `config/config.toml` mimo Git. Neexistující rozhraní je `UNKNOWN` („rozhraní nenalezeno“), neplatný název ukončí program kódem 2. První měření rychlosti RX/TX je vždy `UNKNOWN`. Výpis obsahuje lokální IPv4 notebooku: do dokumentace, issue ani veřejného repozitáře ji nekopíruj (příklady používají dokumentační rozsah 192.0.2.0/24).

SQLite historii (`src/nexusstat/history.py`, základ M3.1a a synchronní zápis M3.1b) zatím používají jen testy: databáze vznikají výhradně v `tempfile.TemporaryDirectory`. Modul nemá výchozí cestu, nevytváří adresáře, nepřepisuje soubory a produkční roli odmítá; vedle databáze nechává jen soubor zámku `<jméno>.lock`. Po pádu uprostřed vytváření může zůstat dočasný soubor `.<jméno>.*.creating` (s `-journal`); nemaže se automaticky a smí se smazat ručně, když žádné vytváření neběží. Druhé jméno po pádu těsně po zveřejnění odstraní příští `open_writer` sám. Zapisovač (`HistoryRecorder`) po nečistém ukončení při dalším startu běh uzavře jako nečistý a zapíše neznámou mezeru; nic se nedoplňuje. Žádná aplikace ani služba ho nespouští.

Benchmark návrhu SQLite (M2, `tools/sqlite_history_benchmark.py`) není součástí aplikace: syntetické databáze zapisuje jen do nového dočasného adresáře (`TMPDIR`, jinak `/tmp`, nebo `--dir`) a na konci ho smaže; plný běh potřebuje asi 1 GB volného místa a trvá řádově minuty, `--quick` kolem minuty. Nepoužívá síť, kód aplikace ani cesty Nexusu.

```bash
python3 tools/sqlite_history_benchmark.py --quick
```

Simulátor kreslí i stránku SERVICES: ve scénáři s fiktivními stavy a značkou `SIM`, v režimu `--live` bez kolektorů služeb: NEXUS, NAS a BACKUP jsou `UNKNOWN` na SERVICES (`UNKN`) i ve STATUS (`NAS NO DATA`) a stránka NEXUS ukazuje `?`; AnimeDB je `N/A`. Snímky ukládej mimo repozitář (např. do `/tmp`).

### Živý simulátor OLED (v0.5)

Jen na vývojovém notebooku s grafickým prostředím. Potřebuje Pillow a tkinter. Na vývojovém notebooku je od 2026-10-19 nainstalovaný systémový balíček `python3-tk` 3.12.3 se závislostmi `libtk8.6`, `tk8.6-blt2.5` a `blt` (Tk 8.6.14); na jiném počítači `sudo apt install python3-tk`, systémová instalace jen po schválení. `PIL.ImageTk` (`python3-pil.imagetk`) potřeba není: obraz se do okna předává jako PNG.

```bash
PYTHONPATH=src python3 -m nexusstat.preview
```

```bash
PYTHONPATH=src python3 -m nexusstat.preview --scenario many --scale 6 --config config/config.example.toml
```

- Ovládání: „Stisk“, mezerník nebo Enter = krátký stisk fyzického tlačítka (skutečná logika `OledController`); nabídka scénářů; rychlost 1×/5×/10× (jen simulace); Esc, křížek nebo Ctrl+C v terminálu okno čistě zavřou (žádná vlákna ani procesy na pozadí).
- Displej je anglicky (texty z `oled_text.py`), ovládání okna česky.
- Pod displejem je režim (SLEEP / CAROUSEL – stránka / STATUS – stránka x/y) a zdroj dat: „SIMULACE: fiktivní data“ nebo „LIVE: měření tohoto NOTEBOOKU, ne Nexusu“.
- Scénáře: 1 Vše v pořádku, 2 WARNING (teplota), 3 ERROR a CRITICAL, 4 UNKNOWN (neplatná, chybějící a po 20/40 s zastaralá data), 5 Více problémů (2 stránky STATUS), 6 Nenapojené služby (NEXUS, NAS, BACKUP `UNKNOWN`, AnimeDB `N/A`), 7–11 NETWORK (v0.6): linka DOWN, bez IPv4, nízký provoz, vysoký provoz, neplatná a po 20 s zastaralá data. Změna scénáře začne data, paměť alarmů i okno kolotoče znovu; rozsvícený displej zůstane rozsvícený od stránky NEXUS.
- LIVE: `SystemMonitor`, `NetworkMonitor` (rozhraní z `[network]`) a `AlarmEvaluator` (prahy z `--config`) v intervalu `[collector]`, vždy 1×, bez značky `SIM`. Měření běží v okně (typicky jednotky ms); zaseknuté čtení `/` by okno zablokovalo stejně jako `nexusstat.run`. První vzorek CPU je `UNKNOWN`.
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

Pro budoucí kolektory (M4, M7) a jen po schválení read-only ověřit zdroje dat na RPi 5, např. typy a stav `/sys/class/thermal/cooling_device*` (`type`, `cur_state`, `max_state`), dostupnost otáček v `/sys/class/hwmon/hwmon*/` (`name`, `fan*_input`) a rozhraní v `/sys/class/net/*/operstate` (název ethernetu na RPi 5 s Debianem 13, např. `eth0` nebo `end0`, a zda automatický výběr zvolí správné rozhraní). Nic se nezapisuje; GPIO se ani nečte bez samostatného schválení.

## Před přechodem na hardware

1. Ověřit originální konfiguraci, reálné řízení bočních ventilátorů, GPIO6 polaritu a chování při bootu.
2. Připravit zálohu dotčených konfigurací a testovaný rollback. Fyzický dohled a teplotní kontrola jsou povinné.
3. Předem stanovit kritéria úspěchu a co dělat při selhání přepnutí. Při nejistotě Pironman nevypínat.
4. Tlačítko: ověřit výchozí logind akce a evdev grab; neimplementovat konfliktní dlouhé držení.
5. InfluxDB nevypínat, dokud nejsou doložené všechny závislosti a zálohy. NexusSTAT ji nepotřebuje; před vypnutím read-only zjistit, kdo ji používá (klienti, dashboardy, skripty), a zálohovat data.
6. Pironman nahrazovat po funkcích (OLED, RGB, tlačítko, ventilátory), u každé s vlastním kritériem úspěchu. Pironman při přechodu jen zastavit a zakázat, neodinstalovat; návrat = obnovit zálohované konfigurace a znovu povolit a spustit `pironman5.service`. Postup návratu vyzkoušet předem.

## SQLite: provoz a zálohování (návrh, neimplementováno)

SQLite zatím neexistuje. Návrh M2 je v `HISTORY.md` (schválený 2026-10-20 kromě R11, R13, R15 (odloženo do M7); neimplementováno, nasazení neschváleno); postup se ověří v M3 na notebooku nad dočasnou databází a na Nexusu až v M7 se schválením. Nic z toho dnes na serveru neexistuje.

- Umístění (návrh M2): živá databáze `/srv/nexus/appdata/nexusstat/history/history.sqlite3` s `-wal`, `-shm`, zámkem `history.lock` a rozpracovaným snímkem, konzistentní snímek `/srv/nexus/appdata/nexusstat/snapshot/history-snapshot.sqlite3` se záznamem `history-snapshot.json` (čas, SHA-256, počty řádků). Lokální SSD, nikdy NAS/CIFS: zamykání souborů přes síť není spolehlivé a výpadek NAS by zastavil zápis. Zapisovač adresář sám nevytváří.
- Režim WAL (`synchronous=FULL`, dávka 60 s): čtení webem neblokuje zápis. Soubory `-wal` a `-shm` patří k databázi. Viz [WAL](https://www.sqlite.org/wal.html).
- Jediný zapisovač (`HISTORY.md`, sekce 5.8): zapisuje a checkpoint dělá jen NexusSTAT. Za jeho běhu živou databázi nikdy neotvírat jiným nástrojem pro zápis (`sqlite3` jen s `-readonly`, raději snímek); převzetí souboru (`adopt`) a obnova jen se zastaveným NexusSTAT. Důvodem je mimo jiné chyba SQLite „WAL-reset“, jejíž oprava je na notebooku (`libsqlite3-0 3.45.1-1ubuntu2.8`) i v Debianu 13 (`3.46.1-7+deb13u1`) neprokázaná (UNKNOWN). Podmínky pro M3 a pro provoz: `HISTORY.md`, sekce 12.3 a 12.4.
- **Samotné kopírování aktivního souboru SQLite nemusí být konzistentní záloha**: během kopírování může probíhat zápis a poslední potvrzené změny mohou být jen ve `-wal`. Restic proto živou databázi přímo nezálohuje: vyloučit celý `history/`, zahrnout `snapshot/`.
- Konzistentní kopie přes SQLite Backup API ([Backup API](https://www.sqlite.org/backup.html), v Pythonu `sqlite3.Connection.backup`): jednou denně asi 30 minut před během existujícího Resticu ji vytvoří sám NexusSTAT do dočasného souboru v `history/` (mimo zálohu), převede na samostatný soubor bez WAL (`journal_mode=DELETE`), ověří `PRAGMA integrity_check` a atomicky přejmenuje do `snapshot/`, pak zapíše záznam (`HISTORY.md`, sekce 9.2). Neúspěšný snímek nic nepřepíše. Kontrola `history.snapshot` hlásí chybějící nebo starý snímek (`WARNING` nad 26 h, `ERROR` nad 50 h), protože Restic sám by úspěch hlásil i bez aktuální historie (9.3). Skript ani timer Resticu se nemění, jen jeho seznam cest (po schválení, se zálohou původní konfigurace).
- Nepřidávat další samostatný timer zálohování. Při nedostupném NAS selže jen Restic; snímek ani živá databáze se nemění.
- Test obnovy (`HISTORY.md`, sekce 9.5): obnovit `snapshot/` z Resticu do nového dočasného adresáře, porovnat SHA-256 se záznamem `history-snapshot.json`, `PRAGMA integrity_check` a `foreign_key_check`, otevřít jen pro čtení a porovnat `meta.snapshot_utc` a počty řádků se záznamem, spustit čtecí dotazy. `restore_tested=false` do skutečného úspěšného testu na Nexusu.
- Obnova po havárii (`HISTORY.md`, sekce 9.6): zastavit NexusSTAT, celý `history/` přesunout stranou včetně `-wal` a `-shm` (nesmí zůstat vedle obnoveného souboru), zkopírovat ověřený snímek, nastavit vlastníka a práva, na náhradním stroji soubor výslovně převzít (otisk stroje), spustit. NexusSTAT obnovený snímek rozpozná a zapíše začátek ztraceného úseku.
- Růst: retenční mazání po hodinových dávkách; hlídat velikost souboru a volné místo (návrh: zápis se zastaví pod 1 GiB nebo 5 % volného místa). Při chybě zápisu nebo plném disku se zastaví jen ukládání historie (vlastní stav `history.storage` a událost), monitoring, alarmy a OLED běží dál.

## Po každé budoucí změně

Zaznamenej změněné soubory, výsledky testů, Git stav, rollback, případnou aktualizaci manifestu, operací a registru `PROJECTS.md`. Výsledky simulace jsou označené jako simulace; nikdy je nevydávat za funkci reálného HW.
