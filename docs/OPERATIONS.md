# NexusSTAT – provozní postup (zatím návrh)

## Skutečný stav

Na Nexusu zatím neběží žádný kód, service ani port. Není co instalovat, spouštět či zastavovat. Příkazy v sekcích o Nexusu níže nejsou příkazem k nasazení.

## Lokální vývoj (simulace, read-only kolektor a pravidelné měření)

Pouze na vývojovém počítači. Kód nepřistupuje k GPIO, I²C, SPI, tlačítku, síti ani NAS. Systémový kolektor jen čte `/proc/stat`, `/proc/meminfo`, `/sys/class/thermal` a `os.statvfs("/")`; nic nezapisuje a nespouští žádné příkazy.

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m nexusstat.simulate /tmp/nexusstat-sim
PYTHONPATH=src python3 -m nexusstat.simulate /tmp/nexusstat-live --live
PYTHONPATH=src python3 -m nexusstat.system_monitor
PYTHONPATH=src python3 -m nexusstat.run
PYTHONPATH=src python3 -m nexusstat.run --config config/config.example.toml
```

`nexusstat.run` měří v popředí terminálu každých `[collector] interval_seconds` (výchozí 10 s) a vypisuje jeden řádek na měření. Ukončení Ctrl+C (kód 0); neplatná nebo chybějící konfigurace skončí kódem 2 bez měření. Neběží jako služba, nezapisuje soubory, neotevírá port a SIGTERM zvlášť neobsluhuje (proces skončí výchozím způsobem, nic se neukládá). Interval i prahy jde ověřit vlastní kopií konfigurace mimo Git, např. `config/config.toml` (ignorováno v `.gitignore`).

Alarmové prahy (`[alarms.cpu_temp]`, `[alarms.disk_root]`) jsou ve výchozím stavu vypnuté a nemají výchozí hodnoty. Zapnutí vyžaduje `enabled = true` a všechny tři hodnoty `warning`, `error`, `hysteresis`; neplatná kombinace ukončí program kódem 2. Konkrétní hodnoty pro Nexus zatím nejsou schválené, pro lokální zkoušku používej jen zjevně ilustrační prahy. Alarm pouze změní stav a ve výpisu označí hodnotu (`[WARNING]`, `[ERROR]`); nic nevypíná, neřídí ventilátory ani neposílá zprávy. Změna prahů se projeví až po novém spuštění (stav potvrzení začne znovu).

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
- Scénáře: 1 Vše v pořádku, 2 WARNING (teplota), 3 ERROR a CRITICAL, 4 UNKNOWN (neplatná, chybějící a po 20/40 s zastaralá data), 5 Více problémů (2 stránky STATUS), 6 Nenapojené služby (NEXUS, NAS, BACKUP `UNKNOWN`, AnimeDB `N/A`). Změna scénáře začne data, paměť alarmů i okno kolotoče znovu; rozsvícený displej zůstane rozsvícený od stránky NEXUS.
- LIVE: `SystemMonitor` a `AlarmEvaluator` (prahy z `--config`) v intervalu `[collector]`, vždy 1×, bez značky `SIM`. Měření běží v okně (typicky jednotky ms); zaseknuté čtení `/` by okno zablokovalo stejně jako `nexusstat.run`. První vzorek CPU je `UNKNOWN`.
- Bez tkinter skončí příkaz kódem 2 se zprávou, bez grafického displeje kódem 1.

Úspěšné testy dokládají logiku automatu, validaci, výpočty kolektoru nad vzorovými vstupy, plánování smyčky a vyhodnocení prahů se simulovaným časem a hodnotami a vykreslení do obrázku. Nedokládají vhodnost konkrétních prahů pro Nexus. Nedokládají funkci displeje, tlačítka ani správnou volbu teplotního senzoru na Raspberry Pi 5; to vyžaduje samostatné schválené read-only ověření na cílovém stroji. Ruční běh `nexusstat.run` dokládá jen funkci na notebooku.

### Návratový postup lokálního vývoje

Projekt má lokální Git repozitář (větev `main`, `origin` = veřejný GitHub). Návratovým bodem lokálních změn je poslední commit (`git status`, `git diff`, `git restore <soubor>`); před prvním commitem žádný návratový bod neexistuje. Commit i push jen po schválení. GitHub není ověřenou zálohou: necommitované soubory a vše vyloučené v `.gitignore` (lokální konfigurace, DB, logy) na něm nejsou. Na Nexusu není co vracet, protože nic nebylo nasazeno.

## Bezpečné předběžné kontroly (read-only)

Na cílovém Nexusu před nasazením znovu ověř:

- `systemctl show pironman5.service -p ActiveState -p SubState -p ExecStart`
- `systemctl show <timer-zálohy>.timer -p ActiveState -p TimersCalendar` pro denní zálohu i týdenní údržbu Resticu
- `findmnt -M <mountpoint-NAS> -o TARGET,FSTYPE -n` pro každý připojený NAS

Skutečné názvy jednotek a cesty mountů jsou v soukromé dokumentaci Nexusu, ne v tomto repozitáři.

Kontrola mountu nikdy není náhradou za ověřený read-only test dostupnosti NAS s timeoutem. Nespouštěj restic backup, retention ani test zápisu bez schválení.

Pro budoucí kolektory (M4, M7) a jen po schválení read-only ověřit zdroje dat na RPi 5, např. typy a stav `/sys/class/thermal/cooling_device*` (`type`, `cur_state`, `max_state`), dostupnost otáček v `/sys/class/hwmon/hwmon*/` (`name`, `fan*_input`) a rozhraní v `/sys/class/net/*/operstate`. Nic se nezapisuje; GPIO se ani nečte bez samostatného schválení.

## Před přechodem na hardware

1. Ověřit originální konfiguraci, reálné řízení bočních ventilátorů, GPIO6 polaritu a chování při bootu.
2. Připravit zálohu dotčených konfigurací a testovaný rollback. Fyzický dohled a teplotní kontrola jsou povinné.
3. Předem stanovit kritéria úspěchu a co dělat při selhání přepnutí. Při nejistotě Pironman nevypínat.
4. Tlačítko: ověřit výchozí logind akce a evdev grab; neimplementovat konfliktní dlouhé držení.
5. InfluxDB nevypínat, dokud nejsou doložené všechny závislosti a zálohy. NexusSTAT ji nepotřebuje; před vypnutím read-only zjistit, kdo ji používá (klienti, dashboardy, skripty), a zálohovat data.
6. Pironman nahrazovat po funkcích (OLED, RGB, tlačítko, ventilátory), u každé s vlastním kritériem úspěchu. Pironman při přechodu jen zastavit a zakázat, neodinstalovat; návrat = obnovit zálohované konfigurace a znovu povolit a spustit `pironman5.service`. Postup návratu vyzkoušet předem.

## SQLite: provoz a zálohování (návrh, neimplementováno)

SQLite zatím neexistuje; tento postup se ověří v M3 na notebooku a na Nexusu až v M7 se schválením.

- Umístění: lokální SSD v `/srv/nexus/appdata/nexusstat/` (název souboru určí návrh schématu v M2). SQLite nikdy přímo na NAS/CIFS: zamykání souborů přes síť není spolehlivé a výpadek NAS by zastavil zápis.
- Režim WAL zvážit kvůli současnému čtení webem a zápisu. Vedle databáze pak existují soubory `-wal` a `-shm`, které k ní patří. Viz [WAL](https://www.sqlite.org/wal.html).
- **Samotné kopírování aktivního souboru SQLite nemusí být konzistentní záloha**: během kopírování může probíhat zápis a poslední potvrzené změny mohou být jen ve `-wal`. Restic proto živou databázi přímo nezálohuje.
- Konzistentní kopie přes SQLite Backup API ([Backup API](https://www.sqlite.org/backup.html), v Pythonu `sqlite3.Connection.backup`) do samostatného souboru na lokálním disku, na kopii `PRAGMA integrity_check`. Tento soubor zahrne existující Restic; živou databázi a její `-wal`/`-shm` z Resticu vyloučit. Kdy kopie vzniká (těsně před spuštěním Resticu, nebo ve vlastním intervalu), rozhodne M2.
- Nepřidávat další samostatný timer zálohování bez potřeby; do existujícího zálohovacího skriptu nebo jednotky sahat jen po schválení a s rollbackem. Při nedostupném NAS musí záloha selhat bezpečně a živá databáze zůstat nedotčená.
- Test obnovy: obnovit kopii z Resticu do dočasného adresáře, `PRAGMA integrity_check`, otevřít ji NexusSTAT v režimu jen pro čtení a porovnat počty záznamů a poslední časy. `restore_tested=false` do skutečného úspěšného testu.
- Růst: retenční mazání po dávkách; hlídat velikost souboru a volné místo. Při chybě zápisu nebo plném disku se zastaví jen ukládání historie (vlastní stav a událost), monitoring, alarmy a OLED běží dál.

## Po každé budoucí změně

Zaznamenej změněné soubory, výsledky testů, Git stav, rollback, případnou aktualizaci manifestu, operací a registru `PROJECTS.md`. Výsledky simulace jsou označené jako simulace; nikdy je nevydávat za funkci reálného HW.
