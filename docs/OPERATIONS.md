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

Simulátor kreslí i stránku SLUŽBY: ve scénáři s fiktivními stavy a značkou `SIM`, v režimu `--live` bez kolektorů služeb: NEXUS, NAS a BACKUP jsou `UNKNOWN` na SLUŽBY i ve STATUS a stránka NEXUS ukazuje `?`; AnimeDB je `N/A`. Snímky ukládej mimo repozitář (např. do `/tmp`).

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

## Před přechodem na hardware

1. Ověřit originální konfiguraci, reálné řízení bočních ventilátorů, GPIO6 polaritu a chování při bootu.
2. Připravit zálohu dotčených konfigurací a testovaný rollback. Fyzický dohled a teplotní kontrola jsou povinné.
3. Předem stanovit kritéria úspěchu a co dělat při selhání přepnutí. Při nejistotě Pironman nevypínat.
4. Tlačítko: ověřit výchozí logind akce a evdev grab; neimplementovat konfliktní dlouhé držení.
5. InfluxDB nevypínat, dokud nejsou doložené všechny závislosti a zálohy.

## Provozní backup politika (neimplementováno)

- Oddělit živou SQLite DB a její zálohovatelnou konzistentní kopii.
- Použít Online Backup API, ověřit `PRAGMA integrity_check` na kopii a test obnovení.
- Zajistit, že stávající Restic nezachytí živou DB nekonzistentně, a že při nedostupném NAS selže bezpečně.
- Nepřidávat další samostatný timer zálohování bez potřeby; do existujícího skriptu sahat pouze po schválení a s rollbackem.
- `restore_tested=false` do skutečného úspěšného testu.

## Po každé budoucí změně

Zaznamenej změněné soubory, výsledky testů, Git stav, rollback, případnou aktualizaci manifestu, operací a registru `PROJECTS.md`. Výsledky simulace jsou označené jako simulace; nikdy je nevydávat za funkci reálného HW.
