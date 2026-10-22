# NexusSTAT – návrh SQLite historie (M2)

**Stav: návrh schválený 2026-10-20 kromě R11, R13, R15 (odloženo do M7). Nic z toho není implementováno ani schválené k nasazení.** Žádná databáze NexusSTAT neexistuje, aplikace nic nezapisuje a SQL níže není aktivní migrace. Bloky SQL jsou označené komentářem `<!-- sql:… -->`, aby je benchmark `tools/sqlite_history_benchmark.py` mohl spustit nad dočasnou syntetickou databází a ověřit, že návrh je platné SQL se slíbeným chováním. Stav jednotlivých rozhodnutí je v sekci 12. Opravy z architektonického review a bezpečnostní revize 2026-10-20 shrnuje sekce 13.

Vychází z požadavků v `REQUIREMENTS.md` (Historie a databáze SQLite) a ze skutečného datového modelu v0.6 (`core.CheckResult`, kolektory `system_monitor` a `network_monitor`, `alarms.AlarmEvaluator`, `services`).

## 1. Co dnes existuje a co se bude ukládat

`CheckResult` má `id`, `state`, `summary` (česky, jen k zobrazení), `observed_utc` (UTC), `expires_monotonic` (monotónní čas běžícího procesu), `simulated`, volitelně `measurement` (`value`, `unit`) a `details` (krátké texty ASCII). Smyčka `run_loop` změří všechny kontroly jednou za interval (výchozí 10 s) se společným `observed_utc` a výsledky projdou `AlarmEvaluator`em.

| Kontrola (`id`) | Údaj | Kam v historii |
| --- | --- | --- |
| `system.cpu_usage` | % | číselná řada (`sample`) |
| `system.cpu_temp` | °C | číselná řada; alarmy → `check_change` |
| `system.memory` | % | číselná řada |
| `disk.root` | % | číselná řada; alarmy → `check_change` |
| `network.rx_rate`, `network.tx_rate` | B/s, rozhraní v `details` | číselná řada s objektem = rozhraní |
| `network.link` | `UP`/`DOWN`, rozhraní (`details`) | změny → `check_change` |
| `network.ipv4` | adresa (`details`) | změny → `check_change` (jen lokálně, nikdy veřejně) |
| `service.nexus`, `service.nas`, `service.backup`, `service.animedb` | jen stav (`UNKNOWN`, `N/A`, budoucí `OK`…) | změny → `check_change` |
| budoucí CPU FAN | otáčky RPM, stupeň chlazení `cur_state`; `ON`/`OFF` | číselné řady + změny stavu |
| budoucí STORAGE | % obsazení, volné bajty po mountech; dostupnost mountu | číselné řady (objekt = mount) + změny stavu |
| budoucí NAS | dostupnost, případně odezva v ms | změny stavu + číselná řada |
| budoucí Restic | stáří poslední zálohy v s, výsledek | číselná řada + změny stavu |
| nové `history.storage`, `history.snapshot` (M3) | stav zapisovače historie, stáří posledního snímku | jen paměť, STATUS a události; ne vlastní řada |

Pro odhady velikosti se počítá s **20 číselnými řadami po 10 s** (6 dnešních + 14 budoucích). Je to konzervativní předpoklad: kontroly úložišť, NAS a Resticu budou nejspíš měřit méně často.

## 2. Zásady

- Historie zaznamenává, co proces skutečně naměřil a vyhodnotil. Nic se nedopočítává, neinterpoluje, nedoplňuje ani nepřepisuje.
- Čas v databázi je skutečný UTC jako celé Unixové sekundy. Monotónní čas (`expires_monotonic`, `now` smyčky) se nikdy neukládá: po restartu nemá význam. Uvnitř běžícího procesu slouží jen k dopočtu času měření před synchronizací hodin (4.3).
- Čtyři různé stavy bez běžné hodnoty se nikdy nezaměňují:

| Situace | Číselná řada `sample` | `check_change` |
| --- | --- | --- |
| skutečná nula (např. RX 0 B/s) | řádek s `value = 0.0` | — |
| `UNKNOWN` (měření proběhlo, hodnota neznámá: první vzorek, chybí senzor, neplatná data, selhání kolektoru) | řádek s `value IS NULL` | změna na `UNKNOWN` s důvodem v `note` |
| chybějící vzorek (proces neběžel, kolektor se zasekl, měření se zahodilo) | **žádný řádek** (mezera) | — ; příčina v `event` (`process.*`, `collector.stalled`, `history.gap`, `history.rejected`) |
| `N/A` (záměrně vypnutá nebo nenasazená integrace) | žádné řádky | změna na `N/A` |

- Agregace počítají jen platné hodnoty: `count(value)`, `min`, `max` a `sum` v SQL hodnoty `NULL` přeskakují, chybějící řádky v nich nejsou. Mezera ani `UNKNOWN` se tak nikdy nezapočítají jako nula.
- Každá ztráta nebo odmítnutí dat se zaznamená (5.4); konflikt se nikdy tiše nepřepíše ani nezamaskuje.
- Simulovaná data do historie nepatří (4.6).
- Selhání historie není selhání monitoringu (5.6).
- Jen `sqlite3` ze standardní knihovny, bez ORM a bez další služby.

## 3. Schéma v1

### 3.1 Přehled tabulek

| Tabulka | Obsah | Klíč | Retence (návrh) |
| --- | --- | --- | --- |
| `meta` | role souboru (`production`/`development`/`test`), otisk stroje, čas vytvoření; ve snímku navíc čas snímku | `key` | trvale |
| `schema_migration` | provedené migrace | `version` | trvale |
| `run` | jedno spuštění procesu: start, ukončení, čisté/nečisté, interval, verze, `boot_id`, poslední zapsaný čas, poslední zapsaná dávka | `id` | trvale (jednotky řádků denně) |
| `metric` | definice číselné řady: `check_id` + objekt (`subject`, např. rozhraní) + jednotka | `id`, unikátní (`check_id`, `subject`) | trvale |
| `sample` | surové vzorky po 10 s | (`ts`, `metric_id`), WITHOUT ROWID | nejméně 30 dní (7) |
| `agg_5m` | 5min agregace: počet vzorků, počet platných, min, max, součet | (`metric_id`, `bucket`), WITHOUT ROWID | nejméně 1 rok |
| `agg_1h` | hodinové agregace z `agg_5m` | (`metric_id`, `bucket`), WITHOUT ROWID | 10 let (prakticky trvale) |
| `aggregation_state` | do kdy jsou agregace hotové | `level` | trvale |
| `check_change` | každá změna stavu nebo provozně významného údaje kontroly (alarmy, `UNKNOWN`, `N/A`, linka, IPv4, služby) s kvalitou času, nic se neslučuje (7.1) | `id`, index `ts` | 2 roky |
| `event` | systémové události (start, ukončení, výpadek kolektoru, mezera, odmítnutá data, skok času, záloha, migrace) s kvalitou času | `id`, index `ts` | 2 roky |

Varianta s minutovými agregacemi (`agg_1m`) by se lišila jen názvem tabulky, kontrolou `bucket % 60 = 0` a dotazy; srovnání je v sekci 10. Denní agregace jako tabulka navržena není: den je 24 hodinových řádků a web ho spočítá při čtení.

### 3.2 DDL (návrh, neaktivní)

Identita souboru je v hlavičce databáze: `application_id` odliší soubor NexusSTAT od jiné SQLite databáze, `user_version` je verze schématu. Tabulky jsou `STRICT` (SQLite ≥ 3.37), takže text se do číselného sloupce nedostane. Debian 13 dodává SQLite 3.46, notebook 3.45.

<!-- sql:ddl -->
```sql
-- Identita souboru a verze schématu (hlavička databáze).
PRAGMA application_id = 1314411348;  -- 0x4E585354 = 'NXST'
PRAGMA user_version = 1;

-- Vlastnosti souboru: 'role' (production | development | test), 'host_id', 'created_utc';
-- jen ve snímku navíc 'snapshot_utc' (9.2).
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
) STRICT, WITHOUT ROWID;

-- Provedené migrace; nemaže se.
CREATE TABLE schema_migration (
    version     INTEGER PRIMARY KEY,
    applied_utc INTEGER NOT NULL,
    description TEXT NOT NULL
) STRICT;

-- Jedno spuštění procesu NexusSTAT.
CREATE TABLE run (
    id             INTEGER PRIMARY KEY,
    started_utc    INTEGER NOT NULL,
    stopped_utc    INTEGER,            -- NULL jen u právě běžícího procesu
    clean_stop     INTEGER CHECK (clean_stop IN (0, 1)),  -- 0 = nečisté ukončení zjištěné při dalším startu
    last_write_utc INTEGER,            -- nejnovější zapsaný vzorek běhu (horní mez pro další běh)
    last_batch     INTEGER NOT NULL DEFAULT 0 CHECK (last_batch >= 0),  -- poslední zapsaná dávka (5.2)
    app_version    TEXT NOT NULL,
    interval_s     INTEGER NOT NULL CHECK (interval_s BETWEEN 1 AND 3600),
    boot_id        TEXT                -- /proc/sys/kernel/random/boot_id: restart procesu vs. systému
) STRICT;

-- Definice číselné řady: kontrola + konkrétní objekt (rozhraní, mount, ventilátor).
CREATE TABLE metric (
    id          INTEGER PRIMARY KEY,
    check_id    TEXT NOT NULL,              -- CheckResult.id, např. 'network.rx_rate'
    subject     TEXT NOT NULL DEFAULT '',   -- '' nebo např. 'eth0', '/'
    unit        TEXT NOT NULL CHECK (unit <> ''),  -- Measurement.unit: '%', '°C', 'B/s', 'RPM'
    created_utc INTEGER NOT NULL,
    UNIQUE (check_id, subject)
) STRICT;

-- Surové vzorky. Řádek = měření proběhlo; value NULL = výsledek UNKNOWN.
-- Chybějící řádek = měření neproběhlo nebo se nezapsalo (mezera).
-- Klíč začíná časem: nové řádky přibývají na konec, retence maže od začátku.
CREATE TABLE sample (
    metric_id INTEGER NOT NULL REFERENCES metric (id),
    ts        INTEGER NOT NULL,         -- čas měření, celé Unixové sekundy UTC (4.3)
    value     REAL,
    PRIMARY KEY (ts, metric_id)
) STRICT, WITHOUT ROWID;

-- 5min agregace ze sample. Řádek jen pro okno s aspoň jedním řádkem sample.
CREATE TABLE agg_5m (
    metric_id INTEGER NOT NULL REFERENCES metric (id),
    bucket    INTEGER NOT NULL CHECK (bucket % 300 = 0),   -- začátek okna, UTC
    n_samples INTEGER NOT NULL CHECK (n_samples > 0),      -- řádky sample v okně včetně UNKNOWN
    n_valid   INTEGER NOT NULL CHECK (n_valid BETWEEN 0 AND n_samples),  -- s hodnotou
    min_value REAL,
    max_value REAL,
    sum_value REAL,                                         -- průměr = sum_value / n_valid
    CHECK ((n_valid = 0) = (sum_value IS NULL)),
    CHECK ((sum_value IS NULL) = (min_value IS NULL) AND (sum_value IS NULL) = (max_value IS NULL)),
    PRIMARY KEY (metric_id, bucket)
) STRICT, WITHOUT ROWID;

-- Hodinové agregace z agg_5m (stejný význam sloupců).
CREATE TABLE agg_1h (
    metric_id INTEGER NOT NULL REFERENCES metric (id),
    bucket    INTEGER NOT NULL CHECK (bucket % 3600 = 0),
    n_samples INTEGER NOT NULL CHECK (n_samples > 0),
    n_valid   INTEGER NOT NULL CHECK (n_valid BETWEEN 0 AND n_samples),
    min_value REAL,
    max_value REAL,
    sum_value REAL,
    CHECK ((n_valid = 0) = (sum_value IS NULL)),
    CHECK ((sum_value IS NULL) = (min_value IS NULL) AND (sum_value IS NULL) = (max_value IS NULL)),
    PRIMARY KEY (metric_id, bucket)
) STRICT, WITHOUT ROWID;

-- Agregace jsou hotové pro všechna okna, která končí nejpozději v done_until.
CREATE TABLE aggregation_state (
    level      TEXT PRIMARY KEY CHECK (level IN ('5m', '1h')),
    done_until INTEGER NOT NULL
) STRICT, WITHOUT ROWID;

-- Každá skutečná změna diagnostického stavu nebo provozně významného údaje kontroly (7.1):
-- alarmy, UNKNOWN, N/A, linka UP/DOWN, IPv4, služby. Nic se neslučuje.
CREATE TABLE check_change (
    id         INTEGER PRIMARY KEY,    -- pořadí vložení = pořadí přechodů i ve stejné sekundě
    ts         INTEGER NOT NULL,
    ts_quality TEXT NOT NULL CHECK (ts_quality IN ('synced', 'derived', 'unsynced')),  -- 4.3, bez výchozí hodnoty
    run_id     INTEGER NOT NULL REFERENCES run (id),
    check_id   TEXT NOT NULL,
    from_state TEXT CHECK (from_state IN ('OK', 'INFO', 'WARNING', 'ERROR', 'CRITICAL', 'UNKNOWN', 'N/A')),
                                       -- NULL = výchozí řádek kontroly v tomto běhu (není přechod)
    to_state   TEXT NOT NULL CHECK (to_state IN ('OK', 'INFO', 'WARNING', 'ERROR', 'CRITICAL', 'UNKNOWN', 'N/A')),
                                       -- from_state = to_state: změnil se jen provozně významný údaj v details
    value      REAL,                   -- naměřená hodnota v okamžiku změny, je-li
    unit       TEXT,
    details    TEXT CHECK (details IS NULL OR json_valid(details)),  -- CheckResult.details jako JSON
    note       TEXT                    -- summary jen pro člověka, nikdy pro logiku
) STRICT;
CREATE INDEX check_change_ts ON check_change (ts);

-- Systémové události.
CREATE TABLE event (
    id         INTEGER PRIMARY KEY,
    ts         INTEGER NOT NULL,
    ts_quality TEXT NOT NULL CHECK (ts_quality IN ('synced', 'derived', 'unsynced')),
    run_id     INTEGER REFERENCES run (id),   -- NULL = mimo běh procesu (např. ruční obnova)
    kind       TEXT NOT NULL,                 -- 'process.start', 'history.gap', ...
    severity   TEXT NOT NULL CHECK (severity IN ('INFO', 'WARNING', 'ERROR', 'CRITICAL')),
    check_id   TEXT,
    message    TEXT NOT NULL,                 -- česky pro člověka
    data       TEXT CHECK (data IS NULL OR json_valid(data))
) STRICT;
CREATE INDEX event_ts ON event (ts);
```

Implementace (M3.1a): `nexusstat.history.SCHEMA_SQL` je doslovná kopie tohoto bloku; shodu hlídá `tests/test_history.py` a aplikace dokumentaci nečte. Při vytvoření souboru zapíše kód (ne DDL) do `meta` řádky `role`, `host_id` (prvních 16 znaků SHA-256 z `/etc/machine-id`, ne samotné id) a `created_utc`, do `schema_migration` verzi 1 a do `aggregation_state` začátek aktuální hodiny pro obě úrovně. Celé vytvoření je jedna transakce v dočasném souboru vedle cíle (`synchronous=FULL`). Zveřejní se jen samostatně použitelný soubor: po zavření spojení nesmí zůstat `-wal` ani `-shm` a soubor sám musí projít ověřením a `integrity_check` (otevření `immutable=1`). Pak `fsync` souboru, zveřejnění pevným odkazem (`os.link`, který existující soubor nikdy nepřepíše) a `fsync` adresáře, aby nové jméno přežilo výpadek napájení. Soubor vznikne s právy 0600. Pád uprostřed vytváření nezveřejní nic a nechá jen dočasný soubor `.<jméno>.*.creating` (případně s `-journal`); ten se automaticky nemaže, protože může patřit souběžnému vytváření, a správce ho smí smazat, když žádné vytváření neběží. Pád mezi zveřejněním a úklidem nechá druhé jméno téhož souboru; `open_writer` ho po získání zámku smaže, jen pokud je prokazatelně tentýž inode bez vlastních pomocných souborů, jinak otevření odmítne.

### 3.3 Indexy

- `sample`: jen primární klíč WITHOUT ROWID (`ts`, `metric_id`), žádný další index. Zápis každé dávky přidává řádky na konec stromu (jedna až dvě stránky místo jedné stránky za každou řadu), retence maže souvislý začátek. Graf jedné řady přečte časový rozsah všech řad a vybere svou; pro rozsahy do 48 h to stačí (sekce 10). Varianta s klíčem (`metric_id`, `ts`) čte jednu řadu asi 3× rychleji, ale zapisuje asi 8× víc (rozhodnutí R5).
- `agg_5m`, `agg_1h`: primární klíč (`metric_id`, `bucket`), protože se čtou dlouhé rozsahy jedné řady (týdny až roky) a zapisují se jen jednou za 5 min, resp. hodinu.
- Dotazy přes všechny řady (agregace, retence) mají podmínku `metric_id IN (SELECT id FROM metric)`: při klíči (`ts`, `metric_id`) nevadí a při alternativním klíči (`metric_id`, `ts`) díky ní dotaz použije klíč místo procházení celé tabulky.
- ROWID tabulka s unikátním indexem (`metric_id`, `ts`) je zamítnutá: index sám zabere 86 MB z 202 MB a zdvojnásobí velikost (sekce 10).
- `metric`: unikátní (`check_id`, `subject`) brání dvěma definicím stejné řady.
- `check_change`, `event`: ROWID (pořadí vložení zachová pořadí i ve stejné sekundě) a index `ts` pro časové výběry a retenci. Tabulky jsou malé, další indexy nejsou potřeba.

### 3.4 Kontrakt typů a hodnot

- `ts`, `bucket`, `*_utc`: celé Unixové sekundy UTC (dolů na celou sekundu). Člověk je přečte přes `datetime(ts, 'unixepoch')`.
- `value`: `Measurement.value` beze změny (nezaokrouhlené, jako ho vyhodnocuje `AlarmEvaluator`); `NULL` = `UNKNOWN`. SQLite by NaN uložil jako `NULL`; `Measurement` NaN ani nekonečno nepřipouští, takže se `UNKNOWN` s chybným číslem nesplete.
- Jednotka patří řadě (`metric.unit`), ne vzorku. Výsledek s jinou jednotkou než definice se do řady nezapíše (událost `history.unit_mismatch`), stejně jako ho `AlarmEvaluator` odmítne pro prahy.
- `details` v `check_change` je JSON se seřazenými klíči (porovnání změny je deterministické). Smí obsahovat jen provozně významné údaje, protože každá jejich změna je přechod (7.1); rychle se měnící nepodstatné údaje do `details` nepatří (čísla patří do `sample`).
- `ts_quality` je povinný a nemá výchozí hodnotu: zapisovač ho musí vždy uvést (4.3).

## 4. Čas a identita měření

### 4.1 Identita vzorku a pravidla přijetí

Identita vzorku je dvojice (čas `ts`, řada `metric_id`), primární klíč tabulky `sample`. Jedno měření smyčky má jedno `observed_utc` pro všechny kontroly. Řada = (`check_id`, `subject`) z pevného katalogu v kódu (jako `alarms.RULED_CHECKS`): kontrola, jednotka a odkud vzít objekt (u sítě `details["interface"]`). Neznámé nebo simulované id nikdy nevytvoří novou řadu. Výsledek bez objektu (síť bez rozhraní) nedostane řádek `sample`, jen změnu stavu v `check_change`.

Zapisovač prochází frontu v pořadí měření a o každém vzorku rozhodne podle prvního platného pravidla:

| Podmínka | Výsledek | Záznam (5.4) |
| --- | --- | --- |
| čas není důvěryhodný (před první synchronizací, 4.3) | čeká ve frontě, po synchronizaci dostane odvozený čas | `clock.synced` s rozsahem |
| totéž měření podruhé (stejná řada, `ts` i monotónní čas měření) | nezapíše se, nic se neztratilo | jen počitadlo duplicit, není to ztráta |
| `ts` ≤ horní mez z předchozích běhů (4.2) | nezapíše se | `history.rejected`, důvod `before_high_water` |
| `ts` = poslední přijatý `ts` řady, ale jiné měření (jiný monotónní čas) | nezapíše se, platí první hodnota | `same_second` (ztráta jednoho měření) |
| `ts` < poslední přijatý `ts` řady | nezapíše se | `clock_backward` |
| `ts` < `done_until` úrovně `5m` (okno už agregované) | nezapíše se | `late` |
| jinak | zapíše se | — |

Tři případy, které se nesmějí plést:

| Případ | Co se stalo | Výsledek |
| --- | --- | --- |
| duplicita | totéž měření dorazilo znovu (opakovaná dávka po nejistém commitu, dvojí odeslání) | nic se neztratilo ani nezměnilo; dávku odmítne `batch-guard`, jednotlivý vzorek pozná pravidlo přijetí nebo `rowcount` + `sample-conflict` se stejnou hodnotou; jen počitadlo |
| kolize v sekundě (`same_second`) | dvě různá měření jedné řady připadla na stejnou celou sekundu | první platí, druhé se nezapíše a započítá jako ztráta; hodnoty se neporovnávají, obě byla platná měření |
| konflikt | databáze už má pro (`ts`, řada) jinou hodnotu, ačkoli ji zapisovač nečekal (chyba programu, času nebo obnovy) | uložená hodnota zůstane, událost `history.sample_conflict` (`ERROR`) s oběma hodnotami |

- **Dvě různá měření jedné řady v jedné sekundě** nastanou jen výjimečně: `run_loop` drží původní plán i po zpoždění menším než interval, takže po měření zpožděném o 9–10 s přijde další za méně než sekundu. Nic se neposouvá (posun by zkreslil čas měření); milisekundy by kolizi odstranily za cenu většího klíče (R6).
- **Tiché přepsání vylučují tři vrstvy:** pravidla přijetí v zapisovači, primární klíč s `ON CONFLICT DO NOTHING` (nikdy `UPDATE` ani `REPLACE`) a kontrola `rowcount` s klasifikací dotazem `sample-conflict`.
- **Opakování dávky:** každá transakce dávky začíná kontrolou `run.last_batch` (dotaz `batch-guard`). Dávka, která už je zapsaná (opakování po nejistém výsledku commitu), se celá odmítne, takže se nezdvojí ani `check_change` a `event`, které přirozený klíč nemají.

### 4.2 Restart procesu

- Při startu se běhy bez `stopped_utc` uzavřou jako nečisté (`close-unclean-runs`) a zapíše se `process.unclean_stop`; stejné `boot_id` = spadl jen proces, jiné = restart systému. Výjimka: obnovený snímek (9.6).
- Nový řádek `run` a událost `process.start` (verze, interval, verze schématu, `boot_id`). Čisté ukončení (SIGTERM, Ctrl+C) dopíše frontu, nastaví `stopped_utc` a `clean_stop = 1`.
- Horní mez = `max(last_write_utc)` ze všech běhů (`high-water`). Nový běh přijme jen vzorky novější, takže po pádu nemůže přepsat ani zdvojit starší data.
- `AlarmEvaluator` po restartu začíná znovu (stávající chování, `alarms.py` se nemění). Potvrzený alarm z historie se neobnovuje, aby ho nepotvrzovala stará data; v `check_change` je proto první záznam běhu s `from_state = NULL` jako viditelný předěl.
- Dva současně zapisující procesy brání zámek souboru (`fcntl.flock` na `history.lock`) a jediná systemd služba (M7).

<!-- sql:close-unclean-runs -->
```sql
UPDATE run SET stopped_utc = coalesce(last_write_utc, started_utc), clean_stop = 0
WHERE stopped_utc IS NULL;
```

<!-- sql:high-water -->
```sql
SELECT max(last_write_utc) FROM run;
```

### 4.3 Kvalita časové značky a synchronizace hodin

Raspberry Pi bez baterie RTC startuje s časem posledního vypnutí a NTP ho až později posune dopředu. Chybný čas by přitom prošel horní mezí (je novější než poslední zápis) a měření by se uložila doprostřed doby, kdy server neběžel. Proto rozlišení tří úrovní:

| `ts_quality` | Význam | Kde se smí objevit |
| --- | --- | --- |
| `synced` (skutečně synchronizovaný) | měřeno po první synchronizaci v tomto bootu a bez zjištěného skoku od ní; dočasnou ztrátu synchronizace ohraničují události `clock.sync_lost` a `clock.sync_restored` | `sample`, `check_change`, `event` |
| `derived` (dodatečně odvozený) | měřeno před první synchronizací; čas dopočtený po synchronizaci z monotónních hodin: `ts = monotonic_měření + (UTC − monotonic)` v okamžiku synchronizace. Chyba je pod rozlišením 1 s: odchylka krystalu 100 ppm dá za hodinu fronty 0,36 s | `sample` (bez sloupce, rozsah v události `clock.synced`), `check_change`, `event` |
| `unsynced` (nelze spolehlivě určit) | synchronizace v tomto bootu nenastala dřív, než bylo nutné data zapsat nebo zahodit (ukončení, přetečení fronty); čas je systémový a může být chybný | jen `check_change` a `event`, **nikdy `sample`**; jejich `data` neobsahují žádný rozsah UTC, jen počet a monotónní rozsah |

- Tabulka `sample` sloupec kvality nemá (1 B na řádek navíc pro vzácný případ). Odvozené vzorky označuje událost `clock.synced` s jejich rozsahem a počtem.
- Stav implementace (M3.3a, 11.4): vlákno zapisovače drží měření i změny před první synchronizací ve frontě s monotónním časem měření a po první synchronizované dávce jim dopočte `derived` čas (posun `UTC − monotonic` z prvního synchronizovaného měření) a zapíše `clock.synced`. Ztráty před synchronizací nesou počet a monotónní rozsah (s `boot_id`) a dostanou odvozený rozsah UTC; při ukončení bez synchronizace se vzorky nezapíšou (`history.gap`, důvod `time_unsynced`, jen monotónní rozsah) a změny se zapíšou s `unsynced`. Skutečný zdroj stavu synchronizace dodává M3.3b-1 (`clock_sync.py`, 11.5); stav nese každé předání (`time_synced`) a neznámý stav se nikdy nepovažuje za synchronizovaný.
- Před první synchronizací v bootu čekají měření ve frontě s monotónním časem. Přetečení fronty (déle než 1 h bez synchronizace) zahodí nejstarší; záznam o ztrátě drží jen počet a monotónní rozsah. Po synchronizaci dostane rozsah ztráty odvozený čas stejně jako čekající vzorky (`history.gap`, `derived`). Při ukončení bez synchronizace se vzorky nezapíšou (`history.rejected`, důvod `time_unsynced`) a změny, události i záznam o ztrátě se zapíšou s `unsynced` a bez rozsahu UTC. **Vymyšlená časová značka tak nevznikne nikdy.**
- Restart procesu před synchronizací frontu ztratí; v historii zůstane neznámá mezera (5.4).
- Ztráta synchronizace po předchozí synchronizaci je jen událost (`clock.sync_lost`, `clock.sync_restored`); hodiny běží dál s naposledy nastavenou korekcí, data se zapisují. Skok o víc než 2 s (posun `UTC − monotonic` mezi měřeními; běžné doladění NTP je řádově milisekundy za sekundu) je `clock.jump` se směrem a velikostí.
- Pevná dolní mez v kódu (např. 2026-01-01): čas před ní se nikdy nepovažuje za synchronizovaný.
- **Evidence stability pro retenci (M3.3b-2, 11.6):** vlákno zapisovače dokládá jen rozsah mezi **prvním a posledním potvrzením jedné nepřerušené řady pozorování**, nikdy uplynulý monotónní čas. Řada se přeruší po startu procesu (po restartu se nic nepředpokládá), po ztrátě i po neznámém stavu synchronizace, po zjištěném skoku času a **po mezeře v pozorování delší než `sync_fresh_seconds`**; `ClockState` je navíc neznámý, dokud poslední potvrzení není čerstvé. Zastavená nebo mrtvá měřicí smyčka proto retenci zablokuje, i když monotónní čas běží dál.
  - **Čerstvost pozorování** `sync_fresh_seconds` = `max(3 × interval měření, 300 s)`. Trojnásobek intervalu je stejné pravidlo, jakým projekt označuje data za zastaralá (platnost výsledku, `config.VALIDITY_INTERVALS`); dolní mez 300 s je jedno agregační okno (`WINDOW_SECONDS`), aby jediné zdržené měření nezahodilo celodenní doklad. Při výchozím intervalu 10 s je to 300 s, tedy nejvýš 0,35 % z 24 h bez pozorování na jednu mezeru; při nejdelším povoleném intervalu 3600 s jsou to 3 h.
    **Co `sync_fresh_seconds` je a co není:** je to **tolerance mezery mezi dvěma pozorováními**, ne nepřetržitý důkaz stavu NTP. Dokládá se jen to, že se zdroj v těchto odstupech ptal a odpovídal „synchronizováno“; co se dělo mezi dotazy, se netvrdí. Mezi pozorováními proto zůstává nepokrytá doba (nejvýš jedna tolerance na mezeru), kterou pokrývá jen nezávislá detekce skoku času (`UTC − monotonic`) a dolní mez `MIN_TRUSTED_UTC`. Vlastní frekvenci ověřování synchronizace (nezávislou na intervalu měření) projekt zatím nemá: je to **otevřený bod M7** (12.4), ne věc tohoto kroku.
  - Skok se pozná ze změny posunu `UTC − monotonic` mezi měřeními nad `CLOCK_JUMP_SECONDS` (2 s, stejný práh jako u události `clock.jump`). Tím se řeší i **návrat z uspání**: monotónní čas při uspání stojí, UTC jde dál, takže se rozdíl projeví jako skok a řada se přeruší.
  - Dokud není **nezapsaná** ztráta událostí doložená v databázi (`events_dropped`, `event_too_large`), je `ClockState` neznámý: ztracená událost mohla být doklad `clock.jump`. Po zapsání ztráty je mezera v historii viditelná a skok v tomto běhu hlídá i vlastní detekce posunu času.
  - Falešně zjištěný skok nebo mezera retenci jen odloží o dalších 24 h, nikdy nerozšíří, co se smí smazat. Bez doložené stability retence nemaže nic.
- Zdroj stavu synchronizace a přítomnost baterie RTC **zůstávají na Nexusu neověřené (UNKNOWN)** do schválené read-only kontroly (gate 12.4); zdroj je vyměnitelná funkce, takže testy ho simulují. Na notebooku je ověřený (M3.3b-1, 11.5): `adjtimex` s `modes = 0` (čistý dotaz) a rozhodnutí podle kernelové meze chyby hodin pod 16 s (`NTP_PHASE_LIMIT`) — stejné kritérium jako `NTPSynchronized` v systemd, takže se výsledek s `timedatectl` shoduje. Samotný příznak `STA_UNSYNC` použitelný není: systemd-timesyncd ho nechává nastavený i při správně synchronizovaných hodinách (změřeno: status 0x2041, mez chyby 0,02 s, `NTPSynchronized=yes`). **Jiný zdroj aktuálního stavu se nepoužívá.** Soubor timesyncd `/run/systemd/timesync/synchronized` dokládá podle dokumentace systemd jen *dřívější* úspěšnou synchronizaci v tomto bootu a po pozdější ztrátě nemizí; o aktuálním stavu hodin nevypovídá ani v jednom směru (jeho chybějící podoba zase nic neříká o hodinách, které srovnal někdo jiný). Čte se proto jen jako doplňující diagnostická informace do výpisu a o kvalitě času nerozhoduje: bez použitelné odpovědi jádra zůstává stav neznámý (`None`) a data čekají ve frontě. Chrony na notebooku nainstalované není, takže jeho čtení nelze ověřit, a neověřené čtení se nepíše. **Omezení:** jádro přizná ztrátu synchronizace až po narůstu meze chyby zpět na 16 s (tolerance 500 ppm, tedy řádově hodiny), ne hned; okamžitou ztrátu doloží jen zdroj, který ji sám hlásí. `CLOCK_MONOTONIC` neběží při uspání systému; server se neuspává.

### 4.4 Skok času, výpadek a opožděná data

- **Skok zpět:** vzorky, které nejsou novější než poslední přijatý vzorek své řady, se nezapíšou (4.1, `clock_backward`); nic se nepřepisuje ani neprokládá. Pokud byl chybný naopak čas před skokem, zůstanou dřívější vzorky s posunutým časem; zapisovač je dodatečně nemění, jen událost `clock.jump` říká, od kdy a o kolik.
- **Skok dopředu:** obyčejná mezera.
- **Výpadek kolektoru:** chybějící měření = žádné řádky. Vlákno zapisovače má hlídání: bez nového měření déle než platnost výsledků (3 × interval) zapíše `collector.stalled`, po obnovení `collector.resumed` s délkou mezery. Výjimka kolektoru dál dává `UNKNOWN` (řádky `NULL`). **Od M3.3b-2** předává `run.Collectors` strukturovaný údaj o selhání v `details["failure"]` (jméno výjimky), takže selhání sběru jde v `check_change` odlišit od platného měření, které jen nemá hodnotu; z textu `summary` se nic neodvozuje. Stav zůstává `UNKNOWN`, takže prahy, výpis ani displej se nemění. Samotná událost `collector.failed` (a hlídání `collector.stalled`/`resumed`) zatím není implementovaná.
- **Opožděná data:** `ts` je čas měření, ne zápisu, takže fronta ani zpožděný zápis čas nezkreslí. Okno se agreguje až po uzavření (6.1); vzorek do už agregovaného okna se odmítne jako `late` (6.4).

### 4.5 Simulované scénáře

Benchmark (`--only selfcheck`) ověřuje SQL přímo z této dokumentace, malé modely pravidel 4.1–4.3 a 7.1 (třídy `TimeRules` a `ChangeRules`, nejsou to kód aplikace) a předpoklad jediného zapisovače (5.8):

| Scénář | Navržené chování | Ověřeno |
| --- | --- | --- |
| dvě měření jedné řady v jedné sekundě | první platí, druhé se nezapíše, `same_second` | model |
| totéž měření odeslané znovu | duplicita: nic se nezapíše, nic se neztratí, není to `same_second` | model |
| duplicitní a konfliktní vzorek v databázi | `rowcount` 0, `sample-conflict` rozliší duplicitu a konflikt, uložená hodnota zůstane | SQL |
| opakování stejné dávky, dávka mimo pořadí | `batch-guard` celou dávku odmítne, nic se nezdvojí | SQL |
| restart | předchozí běh uzavřen jako nečistý časem posledního zápisu, horní mez, vzorek v její sekundě odmítnut | SQL + model |
| posun času zpět o 1 h | `clock.jump`, 360 měření `clock_backward`, zápis pokračuje až za posledním vzorkem, nic nepřepsáno | model |
| start bez synchronizace, pak synchronizace | nic se nezapíše s chybným časem; čekající měření dostanou odvozený čas (`derived`) | model |
| ztráta synchronizace a obnova s krokem +5 s | `clock.sync_lost`, `clock.sync_restored`, `clock.jump`, mezera 5 s, nic se nedoplňuje | model |
| ukončení bez synchronizace | vzorky `time_unsynced` (započteno), události s `unsynced` | model + SQL |
| přetečení fronty bez synchronizace | ztráta jen s počtem a monotónním rozsahem, kvalita `unsynced`, žádný čas UTC | model |
| přetečení fronty, pak synchronizace | rozsah ztráty i čekající vzorky dostanou odvozený čas | model |
| změny stavu a údajů (R4) | každý přechod vlastním řádkem, opakované měření nic; scénáře a počty v 7.1 | model + SQL nad DDL (`selfcheck_r4`) |
| spojení jen pro čtení | nezapíše, checkpoint neprovede ani při zavření jako poslední; checkpoint udělá až zapisovač | SQLite této verze |

### 4.6 Simulovaná data mimo produkční historii

1. Zapisovač odmítne každý výsledek se `simulated=True` (nikdy neuloží, započítá, jednou zapíše `history.simulated_rejected`). Tabulky sloupec `simulated` nemají: co se uloží, je skutečné měření.
2. Simulátor (`simulate`, `preview` včetně LIVE) zapisovač nikdy nevytváří; zapisuje jen `run` se zapnutou sekcí `[history]`.
3. `meta.role` a `meta.host_id`: produkční zapisovač na Nexusu otevře jen soubor s rolí `production` a otiskem stejného stroje. Databáze z notebooku (i se skutečným měřením notebooku) nebo z testů se do produkční historie nedostane ani zkopírováním. Výjimkou je výslovné převzetí obnoveného snímku správcem na náhradním hardwaru (9.6).
4. Testy pracují jen s dočasnými soubory a rolí `test`.

## 5. Zápis, fronta a ztráta dat

### 5.1 Architektura

```text
run_loop (hlavní vlákno, beze změny chování měření)
  └─ po AlarmEvaluator: HistoryLink.record(results, now)   [M3.3b-1, history_link.py]
       → HistoryWorker.submit(results, monotónní čas měření, time_synced)
       jen porovnání se stavem v paměti, strop textu výsledku a přidání do
       omezených front; žádné SQLite, žádné čekání na zapisovač

HistoryWriter (vlastní vlákno, vlastní spojení sqlite3, jediný zapisovatel)
  ├─ dávka (5.2): BEGIN IMMEDIATE; batch-guard; vzorky; check_change; event; COMMIT
  ├─ po zapsané dávce: agregace uzavřených oken (6), na celou hodinu retence (7)
  ├─ jednou denně: snímek přes Backup API (9.2)
  └─ stav zapisovače (bez SQLite) → kontroly history.storage a history.snapshot
```

### 5.2 Fronta a dávka

- `submit()` v hlavním vlákně: (a) porovná stav a `details` každé kontroly s posledním odeslaným a změny vloží do fronty změn; (b) číselné hodnoty měření vloží do fronty vzorků; (c) probudí zapisovač. Vše jen v paměti, jeho cena je úměrná počtu výsledků (desítky).
- **Fronta vzorků:** nejvýš 1 h měření, tedy `ceil(3600 / interval_s)` měření. Při 20 řadách po 10 s je to nejvýš **360 měření = 7 200 hodnot**, odhadem pod 1 MB paměti. Kapacita se řídí časem, ne počtem: při nejkratším povoleném intervalu 1 s by to bylo 3 600 měření a 72 000 hodnot (asi 7 MB). Při zaplnění se zahodí nejstarší měření a zaznamená ztráta (5.4).
- **Fronta změn a událostí:** nejvýš 2 000 položek (změny stavu a vlastní události zapisovače). Změny se počítají už při `submit()`, takže zahození vzorků neposune čas ani neztratí přechod alarmu. Při zaplnění se zahodí nejstarší a započítá (`history.changes_dropped`). Trvale kmitající kontrola přidá až 360 položek za hodinu (přechod při každém měření, 7.1); hodinový výpadek zápisu tak fronta vydrží nejvýš s pěti takovými kontrolami najednou, při víc se nejstarší změny zahodí se záznamem ztráty (5.4).
- **Dávka:** zapisovač se probouzí při každém `submit()` a nejméně každých 10 s. Transakci zahájí, když (a) nejstarší nezapsané měření je staré 60 s (monotónní čas), (b) čeká změna stavu nebo událost, nebo (c) proces končí. **Interval dávkování 60 s je tedy nejvyšší stáří nezapsaného měření v bezchybném provozu**, ne pevný takt.
- Pořadí v transakci: `BEGIN IMMEDIATE` (zámek zápisu hned na začátku), `batch-guard` (číslo dávky = `last_batch + 1`), vzorky (4.1), `check_change`, `event`, evidence běhu (`run-last-write`), `COMMIT`. Teprve po úspěšném commitu se položky z front odeberou.
- **Implementace (M3.3a, 11.4), změřené kapacity:** fronta měření = `ceil(3600 / interval_s)` měření; skutečně změřeno 3,6 MB za hodinu při 20 výsledcích po 10 s (odhad „pod 1 MB“ výše byl nízký), při 1 s asi 36 MB. Změna stavu zabere asi 470 B. Původních **2 000 změn** vydrží hodinový výpadek zápisu jen při 5 trvale kmitajících kontrolách (10 kontrol = 3 600, 20 = 7 200 změn za hodinu po 10 s). **Schváleno 2026-10-21:** fronta změn = hodina trvalého kmitání 20 kontrol, `ceil(3600 / interval_s) × 20` (7 200 změn ≈ 3,4 MB po 10 s, 72 000 ≈ 33 MB po 1 s); ověřeno 1, 5, 10 a 20 kmitajícími kontrolami bez ztráty, 21 kontrol ztrácí. Důležité události mají vlastní frontu (500), aby je záplava změn nevytlačila. **Pevný paměťový strop:** fronta měření drží nejvýš 72 000 hodnot (výsledků) bez ohledu na počet kontrol; s víc než 20 výsledky na měření pokryje proto kratší dobu než hodinu (zahazuje nejstarší se záznamem ztráty). Změřeno v nejhorším případě (interval 1 s, 20 výsledků, všechny kontroly kmitají, obě fronty plné): **55,7 MB**; při výchozích 10 s asi 5,6 MB. Povolený interval je celé číslo 1–3600 s (ověřuje konfigurace, `HistoryWorker`, `start_run` i `CHECK` v `run`), takže fronta změn má nejvýš 72 000 položek. Fronta událostí drží 500 událostí po nejvýš `MAX_EVENT_BYTES` = 16 KiB textu a údajů (≈ 8 MB); větší událost `submit_event()` nepřijme a započte jako ztrátu `event_too_large` (`history.events_dropped`), nikdy nevyhodí chybu do sledování. Horní mez všech tří front je tak asi 64 MB. Odhad počítá s krátkými texty výsledků (`summary`, `details` podle 3.4). **Od M3.3b-1 je délka vynucená:** jeden výsledek smí mít nejvýš `MAX_RESULT_BYTES` = 512 B UTF-8 textu (id, `summary` a údaje `details` včetně režie JSON). Delší text `submit()` zkrátí ještě před vložením do fronty: zůstanou údaje `details`, které se do limitu vejdou (klíč objektu řady má přednost, jinak by vzorek nedostal řadu), zbytek limitu dostane `summary` se značkou zkrácení. Id kontroly, stav, naměřená hodnota ani oba časy se nikdy nezahodí, takže z historie je dál vidět, co se měřilo a jak to skončilo; výsledek se neodmítá ani nezahazuje. Počet zkrácených výsledků a kontroly, kterých se to týká, zapíše další dávka jako `history.result_truncated` (`WARNING` s `limit_bytes` a rozpisem po kontrolách) a drží je i `WorkerStatus.losses`; ztráta měření to není. Fronty tím mají pevnou mez v bajtech, ne jen v počtu položek: nejhorší případ zůstává v měřeném rozsahu (72 000 hodnot × ≈ 0,7 kB ≈ 50 MB). Bezeztrátový provoz při neomezeném výpadku databáze slíbit nejde: po zaplnění se zahazují nejstarší položky se záznamem ztráty.
- `run.last_write_utc` (horní mez, 4.2) posune jen vzorek, který dávka skutečně vložila (`rowcount` 1). Proto se zapisuje až po vzorcích: vzorek odmítnutý databází (konflikt, jiná jednotka uložené řady) ani duplicita horní mez neposunou (oprava při review M3.1b; dřív `batch-guard` zapisoval nejnovější čas dávky ještě před zápisem).

<!-- sql:batch-guard -->
```sql
-- První příkaz transakce dávky. 0 změněných řádků = dávka už je zapsaná (opakování
-- po nejistém commitu) nebo porušené pořadí: ROLLBACK, z dávky se nic nezapíše.
UPDATE run SET last_batch = :batch
WHERE id = :run_id AND last_batch = :batch - 1;
```

<!-- sql:run-last-write -->
```sql
-- Poslední příkaz dávky před COMMIT: :max_ts = nejnovější vzorek, který dávka skutečně
-- vložila (rowcount 1). Duplicita, konflikt ani odmítnutý vzorek horní mez neposunou;
-- NULL = beze změny.
UPDATE run SET last_write_utc = coalesce(max(last_write_utc, :max_ts), last_write_utc, :max_ts)
WHERE id = :run_id;
```

<!-- sql:insert-sample -->
```sql
INSERT INTO sample (metric_id, ts, value) VALUES (:metric_id, :ts, :value)
ON CONFLICT DO NOTHING;
```

<!-- sql:sample-conflict -->
```sql
-- Pro vzorek, který INSERT nevložil: same_value 1 = duplicita, 0 = konflikt (ohlásit).
SELECT value IS :value AS same_value, value AS stored_value
FROM sample
WHERE ts = :ts AND metric_id = :metric_id;
```

### 5.3 Chyby zápisu

V každém případě: když je spojení v transakci, zapisovač ji zruší (`ROLLBACK`; SQLite některé chyby, např. `SQLITE_FULL`, ruší sám) a data zůstávají ve frontách.

| Chyba | Reakce | Další pokus |
| --- | --- | --- |
| `SQLITE_BUSY` (zámek drží jiný zapisovatel, ruční `sqlite3`, checkpoint jiného procesu) | po `busy_timeout` 5 s transakce zrušena | při dalším probuzení (≤ 10 s); po 5 min neúspěchů `history.storage` `WARNING` |
| málo místa (návrh: volné < 1 GiB nebo < 5 %, kontrola před každou dávkou) | transakce se nezahájí | odstup 60 s, zdvojnásobovaný až na 10 min |
| `SQLITE_FULL` | transakce zrušena | jako málo místa |
| `SQLITE_IOERR`, `SQLITE_READONLY`, `SQLITE_CANTOPEN` | spojení se zavře | znovu otevřít s odstupem 60 s až 10 min |
| chyba při `COMMIT` (nejisté, zda proběhl) | spojení se zavře a znovu otevře, přečte `run.last_batch`: ≥ číslo dávky = zapsáno, položky se odeberou; jinak zůstávají | ihned po zjištění |
| `SQLITE_CORRUPT`, `SQLITE_NOTADB`, `quick_check` ≠ `ok` | historie zastavena, soubor se nemění | jen po zásahu správce (9.6) |
| neočekávaná `IntegrityError` (chyba programu) | zapisovač přejde do `FAILED` a dál nezapisuje; data zůstanou ve frontách, monitoring běží (rozhodnuto 2026-10-21; automatické rozdělení dávky odloženo) | jen po zásahu správce |

- Chyba zápisu nikdy nezahodí data okamžitě: zahazuje jen přetečení fronty (5.4).
- **Implementace (M3.3a, 11.4):** druh chyby určuje jméno chyby SQLite (`HistoryError.sqlite_error`): `SQLITE_BUSY`/`LOCKED` opakování po 5 s až 60 s; `SQLITE_FULL` po 60 s až 10 min; `SQLITE_IOERR`, `SQLITE_READONLY`, `SQLITE_CANTOPEN`, `SQLITE_PERM` a nečekané chyby zavřou spojení a znovu ho otevřou po 60 s až 10 min (stejný běh, `HistoryRecorder.rebind`), stejně uzamčená nebo chybějící databáze při otevření (nikdy se nevytváří). Nejistý `COMMIT` vyřeší opakování zapečetěné dávky (`batch-guard`). `SQLITE_CORRUPT`, `SQLITE_NOTADB`, `quick_check` ≠ `ok`, cizí soubor a **nečekaná `IntegrityError`** zapisovač zastaví (`FAILED`), data zůstanou ve frontách a monitoring běží; obnova jen zásahem správce. Automatické rozdělení dávky po `IntegrityError` je odložené (zapečetěnou dávku nelze měnit, 13.8). Po ztrátě práva zápisu je potřeba vrátit práva i `-wal` a `-shm`: SQLite je mezitím může vytvořit s právy hlavního souboru jen pro čtení.
- Zaseknuté I/O (visící `fsync`) blokuje jen vlákno zapisovače; fronty se plní a pak zahazují nejstarší.

### 5.4 Záznam skutečné ztráty dat

| Ztráta | Kde se počítá | Událost (po prvním úspěšném zápisu) |
| --- | --- | --- |
| přetečení fronty vzorků | rozsah `ts` a počet měření a hodnot | `history.gap` (`WARNING`, při delší než 1 h `ERROR`) |
| odmítnuté vzorky (`before_high_water`, `same_second`, `clock_backward`, `late`, `time_unsynced`, `unit_mismatch`, simulovaná data) | počty podle důvodu a rozsah | `history.rejected` (souhrn nejvýš jednou za hodinu) |
| přetečení fronty změn | počet a rozsah | `history.changes_dropped` (`ERROR`) |
| konflikt vzorku | řada, čas, obě hodnoty | `history.sample_conflict` (`ERROR`) |

- Počitadla jsou v paměti od okamžiku ztráty a hned se promítnou do `history.storage` (`ERROR` = data se zahazují). Do databáze se dostanou s prvním úspěšným zápisem. Při ukončení se vypíšou i do journalu.
- Stav implementace: `history.rejected`, `history.sample_conflict` a `history.unknown_gap` zapisuje zapisovač (M3.1b); `history.gap`, `history.changes_dropped` a `history.events_dropped` vlákno zapisovače (M3.3a) z počitadel v paměti, jedna událost za druh a dávku (záznam ztráty sám frontu neplní). Stav `history.storage` (kontrola ze snímku `WorkerStatus`) a výpis zbývajících dat při ukončení jsou hotové v M3.3b-1 (11.5).

**Druhy mezer v historii:**

| Mezera | Jak se pozná | Co je doloženo |
| --- | --- | --- |
| proces neběžel (čisté ukončení) | `run.stopped_utc` s `clean_stop = 1`, další `process.start` | celá doba bez měření, příčina známá |
| ztráta, kterou proces zapsal | `history.gap`, `history.rejected`, `history.changes_dropped` | přesný rozsah (UTC, případně jen monotónní u `unsynced`) a počty |
| **neznámá mezera** | při startu `history.unknown_gap` (`WARNING`) od `last_write_utc` předchozího běhu do startu nového | jen hranice; příčina a počet ztracených měření neznámé |

Po restartu během výpadku SQLite (nebo po pádu, výpadku napájení) lze doložit jen: poslední úspěšně zapsaný vzorek (`run.last_write_utc`), že předchozí běh se neukončil zapsaným čistým ukončením (`clean_stop = 0`; to nedokazuje pád, i čisté ukončení se při výpadku SQLite nemuselo zapsat) a čas nového startu. Doložit nelze, jak dlouho předchozí proces ještě měřil, kolik měření ztratil ani proč; pomůže jen journal. Mezera se proto označí jako neznámá, nikdy se nedoplňuje ani nevydává za dobu, kdy proces neběžel.

### 5.5 Co lze a co nelze zaručit o ztrátě dat

Zaručit lze jen:

1. Potvrzená dávka (úspěšný `COMMIT`) přežije pád procesu; se `synchronous=FULL` i výpadek napájení, pokud úložiště dodržuje `fsync` (nemá volatilní cache, která potvrzuje zápis dřív).
2. V bezchybném provozu se zapisovač pokusí potvrdit každé měření nejpozději 60 s po změření, plus doba commitu.
3. Zapsaná hodnota se nikdy nepřepíše a každá ztráta, o které proces ví, se zapíše, pokud proces stihne ještě aspoň jednu úspěšnou dávku.
4. Ztráta, kterou proces zapsat nestihne, zůstane v historii viditelná jako neznámá mezera (5.4).

Zaručit nelze: horní mez ztráty při pádu (běžně ≤ 60 s, ale po předchozím selhání zápisu celé fronty, až 1 h), zachování dat při poruše úložiště nebo poškození souboru, ani to, že úložiště `fsync` skutečně dodržuje. **„Nejvýš 60 s“ je popis běžného provozu, ne garance.**

| Situace | Ztracená data |
| --- | --- |
| čisté ukončení (SIGTERM, Ctrl+C), zápis funguje | žádná: fronta se dopíše (čekání nejvýš 10 s) |
| čisté ukončení, zápis visí nebo selhává | po 10 s se proces ukončí; obsah front, záznam jen do journalu |
| pád procesu, zápis fungoval | nezapsaná měření, běžně ≤ 60 s + doba commitu; změny stavu ≤ 1 cyklus (10 s) |
| pád procesu během výpadku zápisu | celé fronty: až 1 h měření |
| výpadek napájení, `synchronous=FULL` | jako pád procesu, pokud úložiště dodržuje `fsync` |
| výpadek napájení, `synchronous=NORMAL` | navíc potvrzené transakce od poslední synchronizace WAL (ta probíhá při checkpointu); hranice není pevná |

SIGTERM i SIGINT obsluhuje `run.py` od M3.3b-1 (`Stopper`, jen v hlavním vlákně): oba vedou na stejné řízené ukončení s dopsáním front (limit 10 s) a výpisem toho, co je potvrzené a co ne (11.5).

**Ukončení vlákna zapisovače (M3.3a):** `stop(timeout=10)` přestane přijímat data (pozdější předání se jen započtou), zkusí poslední zápis nejvýš dvakrát bez čekání na odstup, zapíše čisté ukončení (`finish_run`) a zavře databázi ve vlákně zapisovače. Zaručeno: data jsou buď potvrzená commitem, nebo jsou v `StopReport` započtená jako nezapsaná; čisté ukončení se hlásí jen po potvrzeném `finish_run`. **Nezaručeno:** Python neumí přerušit vlákno zaseknuté v systémovém I/O (visící `fsync`). Pak `stop()` po limitu vrátí `completed = False`, vlákno (démonické) dál drží zámek databáze a uvolní ho až konec procesu; nepotvrzená dávka se ztratí a příští start ji označí neznámou mezerou.

### 5.6 Proč výpadek databáze nezastaví měření ani alarmy

- Hlavní vlákno (měření, `AlarmEvaluator`, budoucí OLED) nikdy nevolá `sqlite3` a nikdy nečeká na zapisovač: `submit()` jen přidá položky do omezených front a nastaví příznak probuzení.
- Fronty jsou omezené, takže ani dlouhý výpadek nevyčerpá paměť; místo zablokování se zahazují nejstarší vzorky se záznamem.
- Python `sqlite3` během dotazu i `fsync` uvolňuje GIL, takže visící zápis nezdrží ostatní vlákna.
- Aktuální měření, alarmy a OLED čtou jen výsledky v paměti, nikdy databázi.
- Ukončení procesu čeká na zapisovač nejvýš 10 s.
- Selhání historie mění jen kontroly `history.storage` a `history.snapshot`, nikdy stav měřených veličin.

### 5.7 Nastavení spojení

<!-- sql:connection -->
```sql
PRAGMA journal_mode = WAL;            -- trvale v souboru; čtení webem neblokuje zápis
PRAGMA synchronous = FULL;            -- commit je po návratu na disku; při 1 commitu za minutu zanedbatelné
PRAGMA foreign_keys = ON;             -- platí pro spojení, nastavuje se při každém otevření
PRAGMA busy_timeout = 5000;           -- ms čekání na zámek místo okamžité chyby
PRAGMA journal_size_limit = 8388608;  -- WAL po checkpointu zkrátit na 8 MiB
PRAGMA secure_delete = FAST;          -- sestavení Ubuntu má výchozí ON: retence by přepisovala nulami navíc
```

- Checkpoint: automatický po 1000 stránkách (asi 4 MB WAL, výchozí `wal_autocheckpoint`), režim PASSIVE ve vlákně zapisovače. Čtenáři (web) drží jen krátké transakce: dlouhá čtecí transakce brání checkpointu a WAL roste (změřeno v 10.6).
- `auto_vacuum` zůstává vypnuté: smazané stránky jdou do seznamu volných a SQLite je znovu použije. Po roce provozu soubor přestane růst (10.6). `VACUUM` jen výjimečně a ručně (potřebuje volné místo zhruba velikosti databáze).
- `PRAGMA optimize` při zavření spojení zapisovače (doporučení SQLite), `PRAGMA quick_check` při startu ve vlákně zapisovače, plná `integrity_check` a `foreign_key_check` na denním snímku (9.2) a při testu obnovy, ne na živé databázi; nové porušení cizích klíčů brání při zápisu `foreign_keys = ON`. Identitu souboru ověří zapisovač nejdřív na spojení jen pro čtení, takže odmítnuté otevření hlavní soubor nikdy nezmění (ani checkpointem při zavření).

### 5.8 Riziko WAL-reset a jediný zapisovač

Dokumentace SQLite ([WAL, sekce 11](https://www.sqlite.org/wal.html)) popisuje vzácnou chybu souběhu („WAL-reset bug“), která může poškodit databázi:

- je pravděpodobně ve verzích 3.7.0 až 3.51.2; opravena ve 3.51.3 (2026-03-13), zpětné opravy pro 3.44.6 a 3.50.7 (odkazy na check-iny v dokumentaci);
- nastane jen ve WAL, když jsou na stejný soubor otevřená dvě nebo víc spojení (vlákna nebo procesy) a dvě z nich ve stejném okamžiku zapisují nebo dělají checkpoint;
- je vzácná, ale s vážnými následky; vývojáři doporučují opravenou verzi a nepovažují ji za naléhavou.

**Zjištěný stav (read-only, 2026-10-20):**

| Prostředí | SQLite v Pythonu | Balíček | Oprava |
| --- | --- | --- | --- |
| vývojový notebook (Linux Mint 22.3 na Ubuntu 24.04, Python 3.12.3) | 3.45.1, `sqlite_source_id` končí `alt1` (zdroj upravený distribucí) | `libsqlite3-0 3.45.1-1ubuntu2.8` | **UNKNOWN**: changelog balíčku uvádí jen opravy CVE ve FTS5, session, sqlar a výrazech, nic o WAL ani o check-inech opravy. Doložit ji nelze, pro řízení rizika se bere jako neopravená. |
| cílový Debian 13 (veřejný tracker Debianu) | na Nexusu neověřeno | `sqlite3 3.46.1-7+deb13u1` (stable), `+deb13u2` v proposed-updates | **UNKNOWN**: changelogy obou verzí opravu neuvádějí; verze skutečně nainstalovaná na Nexusu neověřena |

**Shrnutí: na notebooku UNKNOWN, na Nexusu neověřeno** (skutečnou verzi zjistí až schválená read-only kontrola, 12.4).

Oprava se považuje za doloženou jen podle jedné z podmínek, nikdy jen proto, že balíček dostal aktualizaci:

- (a) `sqlite3.sqlite_version` v Pythonu služby je aspoň 3.51.3;
- (c) zdrojový balíček distribuce prokazatelně obsahuje upstream opravu: záplata dohledaná v `debian/patches` a porovnaná s check-inem [863c171b](https://sqlite.org/src/info/863c171b76cd36e0) nebo [c7facf7a](https://sqlite.org/src/info/c7facf7ac58d2cda). Verze 3.44.6 a 3.50.7 se posuzují stejně: přítomnost opravy se musí dohledat.

**Návrh vylučuje podmínky chyby nezávisle na verzi:**

1. Na živou databázi zapisuje a checkpoint provádí jediné spojení, spojení vlákna zapisovače. Automatický checkpoint běží uvnitř jeho commitu, takže se s vlastním zápisem nikdy nepřekryje; jiný checkpoint nevolá nikdo.
2. Všechna ostatní spojení jsou jen pro čtení: `file:…?mode=ro`, `PRAGMA query_only` a od Pythonu 3.12 navíc `SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE`. Nevolají `wal_checkpoint`, `optimize`, `VACUUM` ani zápis. Kontroly `history.storage` a `history.snapshot` SQLite vůbec nepoužívají. Takové spojení smí vedle databáze ve WAL vytvořit prázdné `-wal` a `-shm`, proto se cizí soubor odmítá podle hlavičky dřív, než ho SQLite otevře (M3.1a).
3. Druhému procesu NexusSTAT brání `flock` na `history.lock`; zapisovač po chybě nejdřív zavře staré spojení, pak otevře nové (nikdy dvě zapisující spojení najednou).
4. Snímek čte přes Backup API spojením zapisovače; kontroly snímku a testy obnovy pracují s kopiemi, ne se živým souborem. Restic čte jen soubory ve `snapshot/` a se živou databází se nespojuje.
5. Budoucí web (M6) čte jen podle bodu 2. Potvrzení nebo ztišení alarmu do databáze nezapisuje sám, předá ho procesu NexusSTAT, který ho zapíše frontou zapisovače.
6. Správce za běhu NexusSTAT neotvírá živou databázi pro zápis (`sqlite3` jen s volbou `-readonly`, raději snímek); `adopt` a obnova jen se zastaveným NexusSTAT.

Na notebooku benchmark (`--only selfcheck`, „Jeden zapisovač“) ověřuje předpoklad bodů 1 a 2 na dané verzi SQLite: spojení `mode=ro` nezapíše a checkpoint neprovede (pokus skončí chybou), ani když se zavírá jako poslední; checkpoint udělá až zapisovač. Chybu samotnou to netestuje a o opravě nic neříká. Zbývá riziko porušení pravidel (ruční nástroj, chyba programu) a jiné, dosud neznámé chyby; poškození by odhalil `quick_check` při startu a `integrity_check` snímku, starší generace snímku drží Restic. Podmínky pro M3 a pro provoz jsou v 12.3 a 12.4.

## 6. Agregace

### 6.1 Uzavřené a úplné okno

- Okno `[b, b + 300)` je **uzavřené**, když platí všechno: (1) `b + 300 ≤ now_utc − 120 s`, (2) ve frontě vzorků není žádné nezapsané měření s časem před `b + 300` (ani čekající na synchronizaci), (3) čas je synchronizovaný. Agreguje se jen uzavřené okno; rezerva 120 s je pro budoucí kolektory s vlastním vláknem a časovým limitem.
- **(4) Okno nepředběhne zapsaná data** (doplněno v M3.2a, schváleno 2026-10-21 jako konzervativní ochrana proti skokům hodin): uzavřou se jen okna před oknem nejnovějšího zapsaného vzorku (`max(run.last_write_utc)`). V běžném provozu nic neomezuje (nejnovější vzorek je v aktuálním okně, které podmínka 1 stejně drží otevřené). Brání tomu, aby po skoku hodin dopředu bez nových vzorků (čas pořád hlášený jako `synced`) agregace uzavřela budoucí okna a pozdější platná měření se odmítala jako `late`; doloženo testem. Absence nových měření tedy nikdy neznamená, že lze uzavírat: bez dat se nic nového neuzavře.
- **Poslední interval zůstává nedokončený:** okno s nejnovějším vzorkem (a všechna pozdější) se neagreguje, dokud se nezapíše novější vzorek. Po zastavení měření, čistém ukončení i pádu proto poslední okno zůstane jen v `sample`; uzavře se s prvním vzorkem dalšího měření, i po restartu (mez je z `run.last_write_utc` všech běhů). Prázdná okna řádek nemají, takže pozdější uzavření nic neztratí, a retence maže jen před `done_until` (7), takže tento vzorek nesmaže. Trvale blokovaný stav nevznikne: každý nově zapsaný vzorek mez posune.
- Stav implementace (M3.2a, 11.2): podmínku 2 předává volající jako `QueueState` (fronta je až v M3.3; bez výslovného stavu se neuzavře nic) a zapisovač k ní přidá vzorky své otevřené dávky. Podmínku 3 vyjadřuje `ts_quality = "synced"` (zdroj synchronizace je dál vstup API, 4.3).
- **Úplnost** je počet vzorků proti očekávanému počtu (`300 / interval_s`, při 10 s 30). Měří se a ukládá (`n_samples`, `n_valid`), ale na ni se nečeká: chybějící vzorek už nepřijde. Neúplné okno je platná agregace s menším pokrytím; mezery vysvětlují události (`process.*`, `history.gap`, `history.rejected`).
- Hodina se agreguje, když je hotová celá hodina 5min oken (`done_until` úrovně `5m` ≥ konec hodiny).
- Zachovává se `n_samples` (měření proběhlo, i `UNKNOWN`), `n_valid` (s hodnotou), `min`, `max`, `sum`; průměr = `sum_value / n_valid`. Okno bez řádků nemá agregaci (mezera zůstane mezerou), okno jen s `UNKNOWN` má řádek s `n_valid = 0` a prázdnými hodnotami.
- Součet místo průměru: hodinová agregace z 5min je přesná (`sum(sum)`, `sum(n_valid)`, `min(min)`, `max(max)`), bez vážení zaokrouhlených průměrů.
- Rychlost RX/TX je průměr rychlostí za platné vzorky; když část vzorků chybí, je to průměr za změřenou dobu, ne odhad přenesených bajtů.

### 6.2 Transakce agregace a opakování po pádu

- Jedna transakce zpracuje nejvýš hodinu uzavřených oken: `BEGIN IMMEDIATE`, `progress-5m` (posun `done_until` jen z očekávané hodnoty), `aggregate-5m`, `COMMIT`. Hodinová úroveň stejně s `progress-1h` a `rollup-1h`. Implementace (M3.2a): jedno volání `aggregate()` provede nejvýš jednu transakci 5min oken (do nejbližší celé hodiny) a jednu hodinovou, takže dohánění po výpadku je postupné (hodina oken za volání) a nic neblokuje dlouho; změřeno na notebooku: medián 9 ms, nejvýš 25 ms za volání při 20 řadách po 10 s.
- **Pád před commitem** nezanechá nic: `done_until` zůstane, další běh zpracuje stejný interval celý znovu ze stejných surových dat a výsledek je stejný.
- **Hotový interval se znovu nezpracuje:** `progress-5m` vrátí 0 změněných řádků a transakce se zruší.
- **Existující okno se nikdy nepřepíše:** `INSERT` do agregací nemá `ON CONFLICT`. Kdyby okno za `done_until` už existovalo (chyba programu nebo ruční zásah), transakce selže, vznikne `history.aggregation_conflict` (`ERROR`), agregace se zastaví a `history.storage` hlásí `ERROR`. Implementace (M3.2a): událost nese úroveň, interval a počet, první a poslední existující okno; `aggregate()` vyvolá `HistoryError("conflict")` a do konce běhu agreguje už jen tuto chybu (zápis měření běží dál); nový běh to zkusí znovu a konflikt doloží znovu. Stav `history.storage` je až M3.3. Dřívější návrh s `ON CONFLICT … DO UPDATE` by při opakování nad intervalem, z něhož retence už smazala část surových dat, přepsal správnou agregaci neúplnou.
- **Neúspěšná agregace neztratí surová data:** retence maže jen vzorky před `done_until` (7).

### 6.3 Žádné dvojí započítání

Každý vzorek patří do právě jednoho okna (`ts - ts % 300`), každé okno se vloží jednou (posun `done_until` + primární klíč) a každá hodina se sečte z 5min oken jednou. Dlouhodobý cyklus v benchmarku to po týdnu provozu ověřuje: součet `n_samples` = počet surových vzorků a každé okno se shoduje s přepočtem ze surových dat (10.6).

### 6.4 Pozdě doručená měření

Měření s časem v už agregovaném okně se nezapíše (`late`, 5.4), aby se hotová agregace nikdy nelišila od surových dat. Implementace (M3.2a): `SampleGate` převezme novou hranici až po potvrzené agregaci (při neúspěchu zůstane, po restartu se načte z databáze) a `write()` navíc v transakci dávky porovná vzorky s potvrzeným `done_until`, takže do hotového okna se nevloží ani vzorek, který obešel pravidla přijetí. Je to skutečná ztráta, proto se počítá a zaznamenává. Při navržených podmínkách uzavření (6.1) nastane jen u kolektoru zpožděného o víc než 120 s nebo po skoku času; dnešní kolektory jsou v hlavní smyčce a zpozdit se takto nemohou. Zamítnutá alternativa: znovu otevřít hotové okno (složitější a mění už zálohovaná data).

<!-- sql:progress-5m -->
```sql
-- První příkaz transakce agregace; 0 změněných řádků = interval už je hotový: ROLLBACK.
UPDATE aggregation_state SET done_until = :end
WHERE level = '5m' AND done_until = :start;
```

<!-- sql:aggregate-5m -->
```sql
-- :start a :end jsou násobky 300; okna končící nejpozději v :end. Bez ON CONFLICT:
-- existující okno je rozpor, transakce selže a nic se nepřepíše.
INSERT INTO agg_5m (metric_id, bucket, n_samples, n_valid, min_value, max_value, sum_value)
SELECT metric_id, ts - ts % 300, count(*), count(value), min(value), max(value), sum(value)
FROM sample
WHERE metric_id IN (SELECT id FROM metric) AND ts >= :start AND ts < :end
GROUP BY metric_id, ts - ts % 300;
```

Pozor na `sum()` místo `total()`: `total()` by z oken jen s `NULL` udělal 0,0.

<!-- sql:progress-1h -->
```sql
UPDATE aggregation_state SET done_until = :end
WHERE level = '1h' AND done_until = :start
  AND :end <= (SELECT done_until FROM aggregation_state WHERE level = '5m');
```

<!-- sql:rollup-1h -->
```sql
-- :start a :end jsou násobky 3600.
INSERT INTO agg_1h (metric_id, bucket, n_samples, n_valid, min_value, max_value, sum_value)
SELECT metric_id, bucket - bucket % 3600, sum(n_samples), sum(n_valid),
       min(min_value), max(max_value), sum(sum_value)
FROM agg_5m
WHERE metric_id IN (SELECT id FROM metric) AND bucket >= :start AND bucket < :end
GROUP BY metric_id, bucket - bucket % 3600;
```

Čtení pro web podle délky zobrazeného rozsahu: do 48 h `sample` (je-li v retenci, jinak `agg_5m`), do 60 dní `agg_5m`, déle `agg_1h`. Graf o šířce stovek pixelů víc bodů nepotřebuje.

<!-- sql:read-agg -->
```sql
SELECT bucket, n_samples, n_valid, min_value, max_value, sum_value / n_valid AS avg_value
FROM agg_5m
WHERE metric_id = :metric_id AND bucket >= :start AND bucket < :end
ORDER BY bucket;
```

Mezery v surových datech (rozestup větší než dva intervaly):

<!-- sql:gaps -->
```sql
SELECT previous_ts AS gap_after, ts AS gap_until
FROM (SELECT ts, lag(ts) OVER (ORDER BY ts) AS previous_ts
      FROM sample
      WHERE metric_id = :metric_id AND ts >= :start AND ts < :end)
WHERE ts - previous_ts > :max_step;
```

## 7. Retence a mazání

**30 dní je nejkratší doba uchování surových vzorků, ne přesná.** Vzorek se smaže, až když je zároveň starší než 30 dní a jeho 5min okno je agregované. Dřívější formulace „retence maže jen agregovaná data“ tomu neodporuje, ale mohla se číst jako „maže hned po agregaci“; přesná hranice je minimum obou podmínek.

| Data | Návrh | Hranice mazání |
| --- | --- | --- |
| `sample` | nejméně 30 dní | `min(now − 30 d, done_until '5m')` |
| `agg_5m` | nejméně 365 dní | `min(now − 365 d` zaokrouhleno dolů na hodinu`, done_until '1h')` |
| `agg_1h` | 10 let | `now − 10 let` |
| `check_change`, `event` | 2 roky (R4) | `now − 730 d` |
| `run`, `metric`, `meta`, `schema_migration` | trvale | — |

- Běží na celou hodinu ve vlákně zapisovače. V jedné transakci nejvýš hodina dat (u `sample` 7 200 řádků pro 20 řad): `oldest-sample` najde nejstarší vzorek a transakce smaže do `min(hranice, začátek jeho hodiny + 1 h)`. Nejvýš 24 transakcí za běh, tedy nejvýš den dat za hodinu: dohnání po delším výpadku je postupné a transakce i WAL zůstávají malé.
- Retence běží jen při synchronizovaném čase a po `clock.jump` za synchronizovaného provozu se na 24 h pozastaví; limit jednoho dne za běh omezí škodu chybného času dopředu.
- Když agregace stojí, surová data se nemažou a rostou. `history.storage` hlásí `WARNING`, jakmile existuje surový vzorek starší než 31 dní; dál chrání limit volného místa (5.3).
- Smazané stránky SQLite znovu použije; velikost po pravidelné retenci zůstává stejná (10.6).

**Implementace (M3.2b, lokálně, 11.3):**

- **Hranice přesně podle tabulky výše, mazání vždy `<`:** řádek přesně na hranici zůstává. `agg_5m` i `agg_1h` mažou jen celé hodiny (hranice dolů na hodinu), takže se nikdy nesmaže hodina, jejíž část je mladší než doba uchování. „10 let“ = 10 kalendářních let v UTC (29. února → 28. února), tedy aspoň celých 10 let. Doby uchování jsou konstanty kódu; žádný parametr API je nezkrátí.
- **Autoritou je `done_until`, ne existence řádků:** vzorek se smaže jen pod `done_until '5m'`, 5min okno jen pod `done_until '1h'`. Navíc transakce těsně před mazáním ověří invarianty `retention-check-sample` a `retention-check-5m` (každé mazané okno vzorků má 5min agregaci s aspoň tolika vzorky, každá mazaná hodina oken hodinovou agregaci); při rozporu se tabulka v běhu zastaví (`aggregation_mismatch`) a nesmaže nic. `done_until '1h'` > `done_until '5m'` je porušená evidence (`aggregation_state_invalid`). Retence `aggregation_state` nikdy nemění.
- **Krok:** jedna transakce smaže nejvýš hodinu historie jedné tabulky (od hodiny nejstaršího řádku); běh má nejvýš 24 mazacích transakcí a tabulky se v něm střídají, takže dluh jedné tabulky nezablokuje ostatní. Dohánění po výpadku je postupné.
- **Pořadí v transakci:** `BEGIN IMMEDIATE` → kontrola času → `done_until` a hranice → invariant → počet kandidátů → `DELETE` → `rowcount` se musí rovnat počtu (jinak `ROLLBACK`, žádné falešné potvrzení) → trvalá hranice `retained_since.<tabulka>` v `meta` → `COMMIT`. Chyba SQLite i jiná výjimka vrátí celou transakci. Po chybě `COMMIT` se stav ověří (pod hranicí nic nezbylo a hranice je zapsaná), ne slepě opakuje.
- **`retained_since.<tabulka>`** (klíč `meta`, schváleno 2026-10-21, schéma beze změny) je začátek zachované historie: horní mez posledního mazacího kroku, mění se jen v jeho transakci (při `ROLLBACK` ne) a nikdy neklesá. Řádky s časem před ní retence smazala, od ní nesmazala nic; žádný řádek tabulky pod ní nezůstane. **Neříká, že data od ní jsou souvislá:** chybějící měření, neběžící proces nebo odmítnuté vzorky zůstávají legitimními mezerami (dotaz `gaps`). Hranice může ležet pod skutečnou retenční hranicí, když pod tou už žádné řádky nebyly. Každá tabulka má vlastní hranici.
- **Plán bez zápisu:** `retention_plan()` počítá hranice, kandidáty, omezení agregací, invariant dalšího kroku a důvod blokace v jedné čtecí transakci ukončené `ROLLBACK`, i nad spojením jen pro čtení. Provedení plánu nevěří: plán je jen horní mez a vše se ověří znovu.
- **Čas (`ClockState`, kontrakt s M3.3):** maže se jen při `synced` a doložené stabilitě hodin `stable_s` ≥ 24 h (monotónní doba souvislé synchronizace bez skoku; `None` = neznámo) a s časem po 2026-01-01. V transakci se navíc ověří databáze: žádná `clock.jump` za posledních 24 h (i z předchozích běhů a i „z budoucnosti“) a žádná zapsaná data novější než `now`. Jinak se nesmaže nic (`time_unsynced`, `clock_unstable`, `clock_jump_recent`, `clock_behind_data`). Ruční `ClockState` v testech je simulace, ne důkaz synchronizace; skutečný zdroj dodá M3.3.
- **Objem přechodů:** hodinový krok stačí i pro kmitání. Změřeno na notebooku: hodina se 7 200 přechody (20 kontrol po 10 s) se smaže za 12 ms, teoretické maximum 72 000 přechodů za hodinu (20 kontrol po 1 s) za nejvýš 157 ms s WAL do 4,8 MB; omezení počtu řádků proto není potřeba (ověřit na Raspberry Pi, M7).
- **Vzorky, WAL a velikost** (notebook, 20 řad po 10 s, 32 dní): transakce s hodinou (7 200 řádků) medián 23 ms, nejvýš 53 ms; WAL nejvýš 4,2 MB; soubor se nezmenší (87 MB), volné stránky (5,1 MB po 2 dnech) další zápis znovu použije, soubor neroste. `VACUUM` ani nové indexy nejsou potřeba.

<!-- sql:oldest-sample -->
```sql
SELECT ts FROM sample ORDER BY ts LIMIT 1;
```

<!-- sql:retention-sample -->
```sql
DELETE FROM sample
WHERE metric_id IN (SELECT id FROM metric) AND ts < :cutoff;
```

<!-- sql:oldest-5m -->
```sql
SELECT min((SELECT min(bucket) FROM agg_5m WHERE agg_5m.metric_id = metric.id)) FROM metric;
```

<!-- sql:retention-5m -->
```sql
DELETE FROM agg_5m
WHERE metric_id IN (SELECT id FROM metric) AND bucket < :cutoff;
```

Doplněno v M3.2b (stejný tvar jako bloky výše):

<!-- sql:oldest-1h -->
```sql
SELECT min((SELECT min(bucket) FROM agg_1h WHERE agg_1h.metric_id = metric.id)) FROM metric;
```

<!-- sql:retention-1h -->
```sql
DELETE FROM agg_1h
WHERE metric_id IN (SELECT id FROM metric) AND bucket < :cutoff;
```

<!-- sql:oldest-change -->
```sql
SELECT ts FROM check_change ORDER BY ts LIMIT 1;
```

<!-- sql:retention-change -->
```sql
DELETE FROM check_change
WHERE ts < :cutoff;
```

<!-- sql:oldest-event -->
```sql
SELECT ts FROM event ORDER BY ts LIMIT 1;
```

<!-- sql:retention-event -->
```sql
DELETE FROM event
WHERE ts < :cutoff;
```

Invarianty, které provedení ověří ve své transakci těsně před mazáním (nespoléhá jen na `done_until`):

<!-- sql:retention-check-sample -->
```sql
-- Invariant před mazáním vzorků: okna se vzorky před :cutoff bez 5min agregace, která
-- zahrnuje aspoň tolik vzorků. Výsledek 0 = vzorky jsou v agregacích, smí se mazat.
SELECT count(*) FROM (
    SELECT metric_id, ts - ts % 300 AS bucket, count(*) AS n
    FROM sample
    WHERE metric_id IN (SELECT id FROM metric) AND ts < :cutoff
    GROUP BY metric_id, ts - ts % 300
) raw
LEFT JOIN agg_5m a ON a.metric_id = raw.metric_id AND a.bucket = raw.bucket
WHERE a.n_samples IS NULL OR a.n_samples < raw.n;
```

<!-- sql:retention-check-5m -->
```sql
-- Invariant před mazáním 5min agregací: hodiny s okny před :cutoff bez hodinové agregace,
-- která zahrnuje aspoň tolik vzorků. Výsledek 0 = okna jsou v agg_1h, smí se mazat.
SELECT count(*) FROM (
    SELECT metric_id, bucket - bucket % 3600 AS hour, sum(n_samples) AS n
    FROM agg_5m
    WHERE metric_id IN (SELECT id FROM metric) AND bucket < :cutoff
    GROUP BY metric_id, bucket - bucket % 3600
) five
LEFT JOIN agg_1h h ON h.metric_id = five.metric_id AND h.bucket = five.hour
WHERE h.n_samples IS NULL OR h.n_samples < five.n;
```

### 7.1 Změny stavu a provozních údajů (R4, schváleno 2026-10-20)

Do `check_change` se zapíše **každá skutečná změna** a nic se neslučuje:

- Řádek vzniká, když se u kontroly změní diagnostický stav (`to_state`) nebo provozně významný údaj v `details` (linka `UP`/`DOWN`, rozhraní, IPv4, budoucí dostupnost služby). Opakované měření beze změny stavu i údajů nevytvoří nic.
- Vlastní řádek s přesným časem má tedy každý přechod `INFO` → `WARNING`/`ERROR`/`CRITICAL`, každé zlepšení na nižší stav, každé úplné odeznění závady, přechod do `UNKNOWN` i návrat z něj a každá změna linky, i když je diagnostická klasifikace obou výsledků `INFO` (pak `from_state = to_state` a změna je v `details`).
- První výsledek kontroly v běhu je výchozí řádek s `from_state = NULL`; není to přechod, jen stav po startu.
- Pořadí přechodů ve stejné sekundě drží `id` (vložení v pořadí měření), takže se dva různé přechody v jedné sekundě uloží oba. Délka epizody má rozlišení 1 s; epizoda kratší než sekunda má délku 0.
- `details` smí obsahovat jen provozně významné údaje (3.4). Nepodstatné rychle se měnící údaje do nich ani do porovnání změn nepatří, a proto žádné slučování není potřeba.
- Duplicitám a přepisu brání dávka (4.1, 5.2): `batch-guard` odmítne opakovanou dávku po nejistém commitu, neúspěšná transakce nezanechá nic a opakovaný zápis po ní uloží každý přechod právě jednou.
- **Po retenci (M3.2b, princip schválen 2026-10-21):** začátek epizody, kterou retence (nebo začátek rozsahu `:start`) usekla, se nedomýšlí: `state-episodes` i `details-episodes` ji vrátí s `start_ts = NULL`. Poznají to podle prvního zachovaného řádku běhu bez výchozího řádku (`from_state` není `NULL`). U stavu je `from_state` stav až do první zachované změny (`end_ts`; `NULL` = změna nenastala, stav trvá); u provozních údajů je známá jen hodnota na prvním zachovaném řádku a dál, předchozí hodnota ne. Kde stav ani údaje rekonstruovat nejde, přizná to dotaz `retention-unknown` (úsek od hranice do prvního zachovaného řádku běhu, `state_before` = známý stav, nebo `NULL`). Začátek zachované historie říká `retained_since.check_change` v `meta` (ne souvislost dat); počty `transition-rate` a `transitions-window` před ní nejsou úplné.
- Z historie se dopočítá vznik, konec a délka každé epizody stavu (`state-episodes`) nebo údaje (`details-episodes`), počet a četnost přechodů po oknech (`transition-rate`) a počet přechodů za posledních N sekund v okamžiku každého přechodu (`transitions-window`, vstup budoucího detektoru kmitání, 7.2). Epizoda, kterou přeruší konec běhu procesu, má konec `NULL` (neznámý); epizoda trvající už při startu běhu je označená.
- Omezení notifikací (RGB, web, zprávy) bude samostatné pravidlo nad aktuálním stavem a historii nikdy neomezuje.
- Velikost: retence 2 roky beze změny. Trvalé kmitání (přechod při každém měření po 10 s) zabírá podle měření v 10.7 asi 0,85–0,97 MB denně na kontrolu, za 2 roky 0,62–0,71 GB; platí pro předpoklady uvedené v 10.7. `history.storage` hlásí `WARNING`, když jedna kontrola zapíše víc než 1 000 řádků za 24 h; ukládání se tím neomezuje. Při trvalém kmitání se navíc každá dávka zapisuje do 10 s (změny se zapisují v dalším cyklu, 5.2), takže zápis surových vzorků stoupne z 35 na asi 88 MB/den (10.2).

Regresní scénáře (benchmark `selfcheck_r4`, zápis dávkami s `batch-guard` do DDL a dotazy z této sekce):

| Scénář | Výsledek |
| --- | --- |
| 30 min, 360 měření po 5 s, stav INFO ↔ CRITICAL každých 10 s | 180 přechodů + výchozí řádek = 181 řádků, 180 opakovaných měření beze změny nic; 90 přesných časů vzniku a 90 časů odeznění CRITICAL, `state-episodes` vrátí 90 epizod po 10 s; `transition-rate` 60 + 60 + 60 za 10 min; 6. přechod v okně 10 min v čase +55 s (`transitions-window`) |
| WARNING → ERROR → CRITICAL → WARNING → INFO | 5 přechodů, každý vlastním řádkem včetně úplného odeznění; epizoda CRITICAL 10 s, dvě epizody WARNING po 10 s |
| INFO → UNKNOWN → (UNKNOWN) → INFO | přechod do UNKNOWN i obnovení měření, opakované UNKNOWN nic, epizoda 20 s |
| dva přechody v jedné sekundě (OK → CRITICAL → OK) | oba uložené se stejným `ts`, pořadí podle `id`, délka epizody 0 s |
| linka UP/DOWN se stavem INFO (41 měření, 20 změn) | 21 řádků s `from_state = to_state = INFO`; 10 epizod DOWN po 10 s (`details-episodes`); `transition-rate` 20 přechodů, 0 změn stavu |
| selhání transakce, opakovaný zápis, opakování zapsané dávky | selhaná transakce nic nezanechá a neposune `last_batch`; opakovaný zápis uloží vše jednou; opakovaná dávka odmítnuta; celkem 214 řádků bez duplikátu |
| události `flapping.start` a `flapping.end` v `event` | počty přechodů beze změny |

<!-- sql:insert-change -->
```sql
INSERT INTO check_change (ts, ts_quality, run_id, check_id, from_state, to_state, value, unit, details, note)
VALUES (:ts, :ts_quality, :run_id, :check_id, :from_state, :to_state, :value, :unit, :details, :note);
```

<!-- sql:state-episodes -->
```sql
-- Epizody stavu :state jedné kontroly: začátek, konec a délka v sekundách.
-- end_ts NULL = epizoda trvá, nebo ji přerušil konec běhu procesu či rozsahu (konec neznámý).
-- start_at_run_begin 1 = stav trval už při startu běhu (skutečný začátek neznámý).
-- start_ts NULL = stav trval už před prvním zachovaným řádkem běhu (bez výchozího řádku): jeho
-- začátek smazala retence nebo leží před :start (M3.2b). Začátek se nedomýšlí; end_ts je první
-- zachovaná změna stavu, nebo NULL, když žádná nenastala (stav trvá, konec neznámý).
WITH state_rows AS (
    SELECT id, ts, run_id, from_state, to_state
    FROM check_change
    WHERE check_id = :check_id AND from_state IS NOT to_state AND ts >= :start AND ts < :end
), ordered AS (
    SELECT ts, from_state, to_state, lead(ts) OVER w AS next_ts, lead(from_state) OVER w AS next_from,
           lag(to_state) OVER w AS previous_to, row_number() OVER w AS state_position
    FROM state_rows
    WINDOW w AS (PARTITION BY run_id ORDER BY ts, id)
), first_rows AS (
    SELECT run_id, ts, from_state, row_number() OVER (PARTITION BY run_id ORDER BY ts, id) AS position
    FROM check_change
    WHERE check_id = :check_id AND ts >= :start AND ts < :end
)
SELECT start_ts, end_ts, duration_s, start_at_run_begin FROM (
    -- Konec jen tehdy, když další změna začíná tímto stavem; jinak chybí změny (M3.3a): konec neznámý.
    SELECT ts AS start_ts, CASE WHEN next_from IS to_state THEN next_ts END AS end_ts,
           CASE WHEN next_from IS to_state THEN next_ts - ts END AS duration_s,
           from_state IS NULL AS start_at_run_begin, ts AS sort_ts
    FROM ordered
    WHERE to_state = :state
    UNION ALL
    SELECT NULL, (SELECT min(ts) FROM state_rows WHERE state_rows.run_id = first_rows.run_id), NULL, 0, ts
    FROM first_rows
    WHERE position = 1 AND from_state = :state
    UNION ALL
    -- Nespojitost uvnitř běhu: stav před řádkem začal v chybějících změnách (začátek neznámý).
    SELECT NULL, ts, NULL, 0, ts
    FROM ordered
    WHERE state_position > 1 AND previous_to IS NOT from_state AND from_state = :state
)
ORDER BY sort_ts, start_ts IS NOT NULL;
```

<!-- sql:details-episodes -->
```sql
-- Epizody hodnoty údaje :key = :value (např. link = DOWN) jedné kontroly; sloupce jako state-episodes.
-- start_ts NULL = hodnota platila už na prvním zachovaném řádku běhu bez výchozího řádku:
-- její začátek smazala retence nebo leží před :start (M3.2b). Začátek se nedomýšlí.
WITH marked AS (
    SELECT id, ts, run_id, from_state, json_extract(details, '$.' || :key) AS current,
           lag(json_extract(details, '$.' || :key)) OVER (PARTITION BY run_id ORDER BY ts, id) AS previous,
           row_number() OVER (PARTITION BY run_id ORDER BY ts, id) AS position
    FROM check_change
    WHERE check_id = :check_id AND ts >= :start AND ts < :end
), ordered AS (
    SELECT ts, from_state, current, position = 1 AND from_state IS NOT NULL AS cut,
           lead(ts) OVER (PARTITION BY run_id ORDER BY ts, id) AS next_ts
    FROM marked
    WHERE from_state IS NULL OR position = 1 OR current IS NOT previous
)
SELECT CASE WHEN cut THEN NULL ELSE ts END AS start_ts, next_ts AS end_ts,
       CASE WHEN cut THEN NULL ELSE next_ts - ts END AS duration_s, from_state IS NULL AS start_at_run_begin
FROM ordered
WHERE current = :value
ORDER BY ts;
```

**Zahozené změny (M3.3a):** při přetečení fronty změn zapíše vlákno zapisovače `history.changes_dropped` s počty a hranicemi i po kontrolách (`data.checks`). `state-episodes` nespojitost, kterou řádky ukazují (další změna nezačíná stavem, kterým epizoda skončila), přizná koncem `NULL` a epizodou s neznámým začátkem. Nespojitost, kterou řádky neukážou (zahozený celý cyklus, např. CRITICAL → INFO → CRITICAL, nebo změny údajů), přizná jen dotaz `changes-dropped`. Aktuální stav kontroly (poslední zapsaná změna) zůstává pravdivý.

**Kontrakt čtení epizod (M3.3a):** návaznost `from_state` sama nedokazuje úplnou historii. Čtenář (web, export, výpis) proto vyhodnocuje `state-episodes` i `details-episodes` vždy spolu se zahozenými změnami: pro každou epizodu spustí `episode-dropped-changes` (`:start_ts` a `:end_ts` z epizody, `NULL` = neomezeno). Vrátí-li dotaz řádek, nepřerušenost epizody není doložená. Pak je konec i délka neznámá (zobrazí se jako neúplná historie s úsekem ztráty) a chybějící přechody ani jejich časy se nedomýšlejí. Úsek ztráty bez rozsahu UTC (ztráta před synchronizací hodin, kterou proces už nestihl odvodit) zasahuje do každé epizody kontroly, dokud ho retence nesmaže.

<!-- sql:changes-dropped -->
```sql
-- Úseky, v nichž zapisovač zahodil změny kontroly (přetečení fronty): historie tam není úplná,
-- i když navazující řádky vypadají spojitě. dropped_from a dropped_to jsou UTC (u ztráty před
-- synchronizací odvozené); NULL = jen monotónní rozsah v data (mono_from, mono_to, boot_id).
SELECT event.ts, json_extract(item.value, '$.count') AS dropped,
       json_extract(item.value, '$.from') AS dropped_from, json_extract(item.value, '$.to') AS dropped_to
FROM event, json_each(event.data, '$.checks') AS item
WHERE event.kind = 'history.changes_dropped' AND item.key = :check_id
ORDER BY event.id;
```

<!-- sql:episode-dropped-changes -->
```sql
-- Zahozené změny kontroly, které zasahují do epizody [:start_ts, :end_ts] ze state-episodes
-- nebo details-episodes (NULL = začátek či konec neznámý, tedy neomezeno). Řádek = nepřerušenost
-- epizody není doložená, konec a délka jsou neznámé. Úsek bez rozsahu UTC zasahuje vždy.
SELECT event.ts, json_extract(item.value, '$.count') AS dropped,
       json_extract(item.value, '$.from') AS dropped_from, json_extract(item.value, '$.to') AS dropped_to
FROM event, json_each(event.data, '$.checks') AS item
WHERE event.kind = 'history.changes_dropped' AND item.key = :check_id
  AND (json_extract(item.value, '$.from') IS NULL
       OR ((:start_ts IS NULL OR json_extract(item.value, '$.to') >= :start_ts)
           AND (:end_ts IS NULL OR json_extract(item.value, '$.from') <= :end_ts)))
ORDER BY event.id;
```

Po retenci (M3.2b) přizná neúplnou historii samostatný dotaz. Pro běhy, které trvaly přes hranici `:retained` (hodnota `retained_since.check_change` z `meta`), vrátí úsek od hranice do prvního zachovaného řádku kontroly v běhu (`unknown_until`; `NULL` = žádný řádek, úsek trvá do konce běhu nebo dosud). `state_before` je stav na úseku, je-li znám (`from_state` prvního zachovaného řádku: mezi hranicí a ním žádná změna nenastala); `NULL` = stav neznámý. Provozní údaje (`details`) na úseku známé nejsou nikdy, protože řádek nenese jejich předchozí hodnotu. Běh, jehož první zachovaný řádek je výchozí (kontrola začala hlásit až po hranici), úsek nemá.

<!-- sql:retention-unknown -->
```sql
-- Úseky běhů s neúplnou historií kontroly po retenci (viz text výše).
WITH spanning AS (
    SELECT id, stopped_utc FROM run
    WHERE started_utc < :retained AND (stopped_utc IS NULL OR stopped_utc > :retained) AND started_utc < :end
), first_rows AS (
    SELECT run_id, ts, from_state, row_number() OVER (PARTITION BY run_id ORDER BY ts, id) AS position
    FROM check_change
    WHERE check_id = :check_id AND ts >= :retained AND run_id IN (SELECT id FROM spanning)
)
SELECT spanning.id AS run_id, :retained AS unknown_from,
       coalesce(first_rows.ts, CASE WHEN spanning.stopped_utc < :end THEN spanning.stopped_utc END) AS unknown_until,
       first_rows.from_state AS state_before
FROM spanning
LEFT JOIN first_rows ON first_rows.run_id = spanning.id AND first_rows.position = 1
WHERE first_rows.run_id IS NULL OR first_rows.from_state IS NOT NULL
ORDER BY spanning.id;
```

<!-- sql:transition-rate -->
```sql
-- Přechody kontroly po oknech :bucket_s: všechny (stav i provozně významné údaje) a jen změny stavu.
-- Výchozí řádek běhu (from_state NULL) přechod není; události FLAPPING jsou v event, ne zde.
SELECT ts - ts % :bucket_s AS bucket, count(*) AS transitions, sum(from_state IS NOT to_state) AS state_changes
FROM check_change
WHERE check_id = :check_id AND from_state IS NOT NULL AND ts >= :start AND ts < :end
GROUP BY ts - ts % :bucket_s
ORDER BY bucket;
```

<!-- sql:transitions-window -->
```sql
-- Počet přechodů kontroly za posledních :window_s sekund v okamžiku každého přechodu (vstup detektoru, 7.2).
SELECT ts, count(*) OVER (ORDER BY ts RANGE BETWEEN :window_s - 1 PRECEDING AND CURRENT ROW) AS recent
FROM check_change
WHERE check_id = :check_id AND from_state IS NOT NULL AND ts >= :start AND ts < :end
ORDER BY ts, id;
```

### 7.2 Kmitání (FLAPPING): budoucí detektor (schválený požadavek, neimplementováno)

Kmitání se má do budoucna vyhodnocovat jako možný příznak začínající závady, ne jen omezovat. M2 zajišťuje jen datový kontrakt; detektor se navrhne a schválí později (nejdřív s webem, M6).

- **Vstup:** jen řádky `check_change` s `from_state` různým od `NULL`, tedy změny stavu i provozně významných údajů (počítá se i linka `UP`/`DOWN` se stavem `INFO`). Úplná historie (7.1) dovolí dopočítat počet přechodů, četnost i délky epizod kdykoli zpětně; web je zobrazí dotazy `transition-rate`, `state-episodes` a `details-episodes`.
- **Výstup:** události `flapping.start` a `flapping.end` v tabulce `event` (s `check_id`, počtem přechodů v okně a použitými prahy) a samostatný příznak v paměti. Detektor nikdy nemění `state` kontroly ani řádky `check_change`; `CRITICAL` zůstává `CRITICAL` i během kmitání a příznak FLAPPING se zobrazí vedle stavu.
- **Bez zpětné vazby:** výstupy detektoru se nikdy nepočítají jako přechody. Události jsou v `event`, ne v `check_change`; kdyby se kmitání v budoucnu zobrazovalo i jako samostatná položka, detektor její řádky do vstupu nezahrne.
- **Prahy pro každou kontrolu zvlášť:** například cyklování ventilátoru (`fan.*`) může být normální provoz s vyšším prahem nebo bez detekce.
- **Příklad k budoucímu schválení, ne schválená hodnota:** okno 10 minut, vznik při 6 významných přechodech v okně, ukončení po 20 minutách bez přechodu. Benchmark ověřuje jen to, že takové počty jdou z historie spočítat (`transitions-window`, 7.1).
- **Notifikace** o kmitání lze omezovat samostatně, nikdy na úkor úplnosti historie.

## 8. Odolnost

| Situace | Zapisovač historie | Měření, alarmy, OLED |
| --- | --- | --- |
| adresář databáze neexistuje | historie nedostupná, adresář ani soubor se nevytváří (žádné `mkdir -p` do případně nepřipojeného místa); nový pokus za 5 min | beze změny |
| soubor není NexusSTAT (`application_id`), má novější `user_version`, jinou roli nebo jiný stroj | historie vypnutá do zásahu správce, soubor se nemění | beze změny |
| chyby zápisu | podle 5.3, data zůstávají ve frontách | beze změny |
| poškozený soubor | historie zastavena; obnova ze snímku (9.6) | beze změny |
| zaseknuté I/O | blokuje jen vlákno zapisovače | beze změny |
| pád procesu | ztráta podle 5.5, při startu `process.unclean_stop` | restart systemd (M7) |
| snímek selže nebo zastará | starý snímek zůstává, `history.snapshot` `WARNING`/`ERROR` (9.3) | beze změny |

- `history.storage` (STATUS `HISTORY`): `INFO` zapisuje; `WARNING` zápis selhává a fronty drží data, stojí retence, je WAL větší než 64 MB (dlouhá čtecí transakce, 10.6), nebo jedna kontrola zapsala víc než 1 000 řádků `check_change` za 24 h (trvalé kmitání, 7.1); `ERROR` data se zahazují, agregace narazila na rozpor nebo je historie zastavená; `N/A` historie vypnutá v konfiguraci.
- **Implementace (M3.3b-1, 11.5):** stav se počítá výhradně ze snímku `WorkerStatus` (bez SQLite) v tomto pořadí: `N/A` vypnutá historie; `ERROR` selhané předání výsledků, `FAILED`, doložená ztráta dat, plná fronta nebo neběžící vlákno; `WARNING` `DEGRADED` (s druhem chyby a časem dalšího pokusu); `UNKNOWN` zapisovač ještě neotevřel databázi; `WARNING` data čekající na synchronizaci hodin, neznámý stav synchronizace, ztracená synchronizace nebo zkrácený text výsledku; `WARNING` také zápis, který se nepotvrdil déle než 180 s, i když data čekají (zaseknuté I/O stav zapisovače nezmění: zůstane `RUNNING` a žádná chyba nevznikne, proto se pozná až podle chybějícího potvrzení); `INFO` jen běžící zápis (bez potvrzené dávky to říká výslovně). `CRITICAL` historie nepoužívá: ztráta historie není havárie Nexusu. Z toho, že monitoring jako celek pokračuje, se stav historie neodvozuje nikdy. **Zpětná vazba:** kontrola `history.storage` se do historie nepředává (její stav se mění podle historie, takže by zápis sám vytvářel další zápisy) a její `details` nesou jen ustálené názvy stavů, ne počty z front; i kdyby ji někdo předal, R4 z opakovaného stejného stavu další řádek nevytvoří. **Doplněno v M3.3b-2 (11.6):** `ERROR` navíc při konfliktu agregace (hotové okno se nikdy nepřepíše, 6.2); `WARNING` při selhání údržby (s počtem pokusů a druhem chyby), při zastavené retenci (`aggregation_mismatch`), při WAL nad 64 MiB a při zpoždění hranice hotových 5min oken nad hodinu za aktuálním časem (normálně jsou to minuty: okno 5 min + rezerva 120 s + dávka 60 s). Blokovaná retence **není závada**: prvních 24 h běhu se nemaže, dokud není doložená stabilita hodin, a stejně tak agregace čekající na frontu; obojí se jen vypíše v souhrnu údržby. Popis `INFO` i `WARNING` nese krátký souhrn (`údržba: agregace N+M oken, retence …`), `details` zůstávají jen ustálené názvy stavů. Zatím neimplementované části stavu: trvalé kmitání (7.2) a stav snímku `history.snapshot` (M3.4).
- Události, které nejde zapsat (plný disk), zůstanou ve frontě změn a zapíšou se po obnovení se svým původním časem.
- Testy M3 nasimulují plný disk přes `PRAGMA max_page_count`, jen pro čtení přes `mode=ro`, poškozený soubor zápisem do kopie a zamčení druhým spojením; nic z toho nevyžaduje skutečně plný disk ani skutečné čekání.

## 9. Zálohování a obnova (plán, na serveru nic neexistuje)

### 9.1 Soubory

```text
/srv/nexus/appdata/nexusstat/
  history/                      Restic tento adresář NEZÁLOHUJE
    history.sqlite3             živá databáze
    history.sqlite3-wal         patří k databázi (potvrzené změny před checkpointem)
    history.sqlite3-shm         index WAL ve sdílené paměti
    history.lock                zámek jediného zapisovače
    snapshot.tmp, snapshot.json.tmp   rozpracovaný snímek (jen během tvorby)
  snapshot/                     Restic zálohuje jen tento adresář
    history-snapshot.sqlite3    poslední ověřený snímek, samostatný soubor bez -wal
    history-snapshot.json       záznam o snímku: čas, SHA-256, velikost, počty řádků, verze schématu
```

Samotné kopírování `history.sqlite3` (ani spolu s `-wal` a `-shm`) není konzistentní záloha: během kopírování může běžet zápis nebo checkpoint a poslední potvrzené změny mohou být jen ve `-wal`. Rozpracovaný snímek vzniká v `history/`, aby ho Restic nikdy nezálohoval napůl zapsaný (v předchozí verzi návrhu vznikal v `snapshot/`).

### 9.2 Snímek přes Backup API

Jednou denně v nastavitelném čase asi 30 minut před během existujícího Resticu (čas jeho timeru je v soukromé dokumentaci Nexusu). Provádí ho vlákno zapisovače; data mezitím čekají ve frontách. Snímek vzniká jen při synchronizovaném čase (jinak se odloží a vznikne událost), takže `created_utc` v záznamu je vždy synchronizovaný čas:

1. Smazat zbytky přerušeného pokusu (`history/snapshot.tmp*`). Zkontrolovat volné místo (aspoň velikost databáze + rezerva).
2. Zdroj Backup API: spojení zapisovače na živou `history/history.sqlite3`. Cíl: nové spojení na nový soubor `history/snapshot.tmp`. `source.backup(target)` celé najednou (`pages=-1`) přečte ve WAL jednu konzistentní verzi zdroje; cíl je jiný soubor, takže na živou databázi nepřibude druhý zapisovatel (5.8).
3. Na kopii: `PRAGMA journal_mode = DELETE` (kopie by jinak zdědila WAL), zápis `meta.snapshot_utc`, počty řádků tabulek.
4. Kontrola samostatným spojením jen pro čtení: `PRAGMA integrity_check` = `ok`, `foreign_key_check` bez řádků, `application_id`, `user_version`. Při neúspěchu se kopie smaže a nic se nezveřejní.
5. `fsync` kopie, `os.replace()` do `snapshot/history-snapshot.sqlite3`, pak stejně atomicky `history-snapshot.json` (SHA-256 zveřejněného souboru), `fsync` adresáře. Záznam až po snímku: při pádu mezi nimi záznam ukazuje starší čas a kontrola aktuálnosti chybuje na bezpečnou stranu.
6. Událost `history.snapshot` (velikost, trvání, výsledek kontroly).

Alternativa `VACUUM INTO` vytvoří menší, zhuštěnou kopii, ale přepisuje celý soubor jinak uspořádaný a více vytěžuje CPU; požadavek výslovně uvádí Backup API. Před každou migrací schématu vznikne stejným postupem `snapshot/pre-migration-v<N>.sqlite3` (návrat = obnova z něj).

### 9.3 Aktuálnost a selhání snímku

Kontrola `history.snapshot` (STATUS `HIST SNAPSHOT`) se počítá v hlavním vlákně jen z údajů v paměti: čas posledního úspěšného snímku (zapisovač ho při startu načte z `history-snapshot.json` a po každém snímku aktualizuje) a výsledek posledního pokusu. Hlavní vlákno soubor nečte, takže ho visící disk nezablokuje (5.6), a stáří roste i tehdy, když se pokus o snímek vůbec nespustí (zaseknutý zapisovač):

| Stav | Podmínka |
| --- | --- |
| `INFO` | poslední úspěšný snímek nejvýš 26 h starý |
| `WARNING` | 26–50 h, nebo poslední pokus selhal a předchozí snímek je ještě mladší než 50 h |
| `ERROR` | starší než 50 h, snímek nebo záznam chybí, nečitelný záznam |
| `N/A` | historie vypnutá v konfiguraci |

- Selhání snímku nikdy nesmaže ani nepřepíše předchozí snímek; `history.snapshot` ho ohlásí hned a s rostoucím stářím se stav zhoršuje.
- **Zakázaná situace** (živá databáze vyloučena, snímek chybí, záloha hlásí úspěch bez upozornění) se pokrývá třemi vrstvami: (1) `history.snapshot` hlásí chybějící nebo starý snímek v STATUS a na webu nezávisle na Resticu; (2) budoucí kolektor `service.backup` porovná čas posledního úspěšného běhu Resticu (read-only `systemctl show`) s `created_utc` v záznamu a záloha s historií starší než 26 h je `WARNING` „záloha bez aktuální historie“; (3) záznam je v záloze spolu se snímkem, takže test obnovy stáří pozná.
- Hranice návrhu: Restic sám bude dál hlásit úspěch, protože jeho úloha se nemění. Upozornění dává NexusSTAT. Volitelná kontrola aktuálnosti přímo v úloze Resticu (jen varování, záloha ostatních dat nesmí selhat) by znamenala změnu jeho skriptu a je odložená (R13).
- Pokud neběží NexusSTAT, neběží ani kontrola; to je ale vidět samo (OLED, web a měření nefungují).
- Logickou chybu, kterou `integrity_check` nepozná, by nový snímek převzal; starší generace drží Restic podle své retence.

Potvrzení požadavků R12:

| Požadavek | Jak je splněn | Ověřeno |
| --- | --- | --- |
| nedokončený snímek se nikdy nezveřejní | rozpracovaný soubor jen v `history/` (mimo Restic), do `snapshot/` až atomickým `os.replace()` po kontrolách | benchmark: v `snapshot/` jen hotové soubory |
| poškozený snímek nenahradí poslední platný | `integrity_check`, `foreign_key_check`, `application_id` a `user_version` před zveřejněním; při chybě se kopie smaže | benchmark: poškozená kopie nezveřejněna, předchozí snímek i záznam beze změny |
| známe stáří posledního úspěšného snímku | `created_utc` v záznamu (vždy synchronizovaný čas), stav v paměti | model stavů 26/50 h |
| výpadek tvorby je diagnostická událost | `history.snapshot` (`ERROR`) při neúspěšném pokusu; `WARNING`/`ERROR` podle stáří i bez pokusu | model stavů; událost v M3.4 |
| úspěch Resticu s prošlým snímkem se nezamění za aktuální zálohu | stáří hlásí NexusSTAT, budoucí `service.backup` porovná čas běhu Resticu se záznamem, záznam je v záloze pro test obnovy | návrh; Restic se nemění |

### 9.4 Zahrnutí do existujícího Resticu (vyžaduje schválení, M7)

1. Read-only zjistit, které cesty dnešní úloha Resticu zahrnuje a vylučuje (soukromá dokumentace Nexusu, žádná změna).
2. Pokud zahrnuje celé `/srv/nexus/appdata/`: přidat vyloučení `/srv/nexus/appdata/nexusstat/history/`. Jinak přidat `/srv/nexus/appdata/nexusstat/snapshot/`. Změnu dělá správce se zálohou původní konfigurace; návrat = obnovit původní soubor.
3. Po dalším běhu read-only ověřit (`restic ls latest` pro adresář nexusstat), že snímek i záznam v záloze jsou, živé soubory `history.sqlite3*` a `snapshot.tmp` ne.
4. Při nedostupném NAS selže jen Restic; snímek ani živá databáze se nemění a zapisovač nic nečeká.

### 9.5 Test obnovy (samostatný, nikdy přes živou databázi)

1. Obnovit `snapshot/` z Resticu do nového dočasného adresáře (`restic restore … --target <dočasný adresář>`).
2. SHA-256 obnoveného souboru = záznam `history-snapshot.json` ze stejné zálohy.
3. Otevřít jen pro čtení (`file:…?mode=ro`): `integrity_check`, `foreign_key_check`, `application_id`, `user_version`, `journal_mode` = `delete`, `meta.snapshot_utc` = čas v záznamu, počty řádků všech tabulek = záznam. (Dřívější verze porovnávala s událostí `history.snapshot`, která ale ve snímku samotném není.)
4. Spustit čtecí dotazy webu (graf, agregace, mezery) a ověřit, že čtení nevytvořilo `-wal` ani `-shm`.
5. Výsledek zapsat do `manifest.toml` (`restore_tested`, `last_restore_test`). Do prvního úspěšného testu na Nexusu platí `restore_tested = false`.

Na notebooku ověřuje kroky 2–4 benchmark nad syntetickou databází (místo `restic restore` kopie snímku, 10.6).

### 9.6 Obnova po havárii (postup pro správce)

1. Zastavit NexusSTAT (obnova mění databázi; měření se tím zastaví).
2. Celý adresář `history/` přesunout stranou (nic nemazat). **Starý `-wal` a `-shm` nesmí zůstat vedle obnoveného souboru**, jinak by se mohl použít WAL jiné databáze.
3. Ověřit snímek jako v 9.5 a zkopírovat ho jako `history/history.sqlite3`, nastavit vlastníka a práva (adresář 0750, soubory 0640, uživatel služby).
4. Na náhradním hardwaru (jiné `/etc/machine-id`) zapisovač soubor odmítne (4.6); správce převezme soubor výslovným příkazem (M3.4, např. `python3 -m nexusstat.history adopt <soubor>`), který přepíše `meta.host_id` a zapíše událost `history.adopted`.
5. Spustit NexusSTAT. Zapisovač podle `meta.snapshot_utc` pozná obnovený snímek: zapíše `history.restored` (čas snímku = začátek ztraceného úseku), běh otevřený ve snímku uzavře jako nečistý s touto poznámkou místo běžného `process.unclean_stop` a `snapshot_utc` z `meta` odstraní. Horní mez je `max(last_write_utc)` ze snímku, doba mezi snímkem a obnovou zůstane mezerou.

## 10. Benchmark

### 10.1 Metodika

`python3 tools/sqlite_history_benchmark.py` (první plný běh 5 min 12 s, `--quick` asi 1 min). Jen standardní knihovna, nový dočasný adresář, který se na konci smaže; nic z aplikace, žádná síť ani cesty Nexusu.

- Prostředí 2026-10-20: vývojový notebook x86_64 (Intel i7-8550U, SATA SSD, ext4), SQLite 3.45.1, Python 3.12.3. **Není to Raspberry Pi 5 ani jeho NVMe**: absolutní časy na RPi 5 budou jiné (odhadem 2–3× delší), poměry variant by měly platit.
- Data: 20 řad po 10 s (sekce 1), deterministický generátor (stejná data pro všechny varianty), denní výpadek 5 min bez řádků, 0,3 % `UNKNOWN`, první vzorek CPU a sítě po výpadku `UNKNOWN`, 5 % nulových rychlostí, celočíselné hodnoty tam, kde je dává hardware (otáčky, stupeň chlazení, bajty).
- Pořadí vkládání jako v provozu (po časech, uvnitř všechny řady), dávky po 60 s. Naplnění 30 dní (5 166 000 řádků) s `synchronous=OFF` jen kvůli délce běhu; na velikost to vliv nemá. Objem zápisů, latence commitu a retence s nastavením z 5.7 (`FULL`, `secure_delete=FAST`).
- Objem zápisů = bajty předané do `write()` (`wchar` z `/proc/self/io`): WAL, checkpoint do databáze a snímky. Dlouhodobý cyklus uvádí i `write_bytes` (odhad jádra pro úložiště, včetně mapované `-shm`). Obojí je logický objem; souborový systém a řadič SSD přidají vlastní režii.
- Čtení: medián z 5 opakování, teplá cache (data v paměti OS); studené čtení po restartu bude pomalejší.
- Ověření: SQL bloky z této dokumentace a model pravidel 4.1–4.3 se spouštějí na začátku (4.5); dlouhodobý cyklus používá stejné bloky.
- Revize po prvním běhu měnily jen malou tabulku `check_change` (finální R4 z ní odstranila sloupce souhrnu) a pravidla nad ní. Velikosti, zápisy a časy v 10.2–10.6 se tím nemění (v cyklu má `check_change` desítky řádků denně), proto se plný benchmark neopakoval; `--only selfcheck`, nová část `--only changes` (10.7) a `--quick` ano.
- Opakovatelnost: druhý plný běh při review (2026-10-20, 5 min 26 s) dal shodné velikosti, objemy zápisů i změny snímku (data jsou deterministická); časy byly o 10–50 % kratší podle stavu notebooku, poměry variant zůstaly. Tabulky 10.2 a 10.3 jsou z prvního běhu. Řádek „agregace 1 h“ měří od review vložení vrácené `ROLLBACK`em, protože návrh existující okno nepřepisuje; ve druhém běhu A 7,2 / B 3,6 / C 4,5 ms.

### 10.2 Surové vzorky: tři uspořádání (30 dní, 20 řad)

| | A: ROWID + unikátní index (`metric_id`, `ts`) | B: WITHOUT ROWID (`metric_id`, `ts`) | **C: WITHOUT ROWID (`ts`, `metric_id`)** |
| --- | --- | --- | --- |
| velikost | 202,3 MB (tabulka 115,9 + index 86,4) | 101,5 MB | 104,6 MB |
| bajtů na řádek | 39,2 | 19,6 | 20,2 |
| plnění (řádků/s) | 137 740 | 125 848 | 203 061 |
| čtení 1 řady 1 h / 24 h / 48 h | 0,5 / 16,8 / 36,9 ms | 0,5 / 8,9 / 16,6 ms | 0,9 / 25,1 / 51,2 ms |
| čtení 1 řady 7 d / 30 d | 136 / 530 ms | 65 / 319 ms | 188 / 865 ms |
| čtení všech 20 řad za 1 h | 12,8 ms | 10,2 ms | 10,2 ms |
| agregace 1 h všech řad do 5min oken | 10,7 ms | 7,6 ms | 7,4 ms |
| zápis, dávka 10 s | 956 MB/den | 930 MB/den | 88 MB/den |
| **zápis, dávka 60 s** | 270 MB/den | 266 MB/den | **35 MB/den** |
| zápis, dávka 300 s | 108 MB/den | 103 MB/den | 14 MB/den |
| commit, dávka 60 s, `FULL` (medián / p95) | 3,3 / 10,2 ms | 3,0 / 8,4 ms | 2,5 / 5,0 ms |
| commit, dávka 60 s, `NORMAL` (medián / p95) | 0,7 / 2,2 ms | 0,6 / 2,4 ms | 0,3 / 0,7 ms |
| retence 1 dne, `secure_delete=ON` / `FAST` | 21,1 / 15,9 MB | 13,5 / 9,6 MB | 7,6 / 3,6 MB |
| `quick_check` / `integrity_check` | 1,2 / 6,4 s | 1,0 / 1,4 s | 1,1 / 1,4 s |
| snímek Backup API / `integrity_check` snímku | 1,5 / 6,1 s | 0,5 / 1,3 s | 0,6 / 1,3 s |
| změněné stránky snímku za den | 6,2 % | 5,2 % | 6,1 % |

Plány dotazů: B a C všechny dotazy přes primární klíč (C graf řady jako rozsah `ts`), A přes index a dohledání řádku v tabulce. Objem zápisu `NORMAL` a `FULL` je stejný; `FULL` přidá jen čekání na `fsync` (ve vlákně zapisovače).

**Závěr:** varianta C. Při stejné velikosti jako B zapisuje 7,6× méně (nové řádky jdou na konec stromu, B mění při každém commitu stránku každé řady), má nejkratší commit a nejlevnější retenci; její objem zápisu navíc neroste s počtem řad. Pomalejší čtení jedné řady (48 h za 51 ms na notebooku) je v rozsahu, pro který web surová data používá; delší rozsahy čte z agregací. A je zamítnutá: index zdvojnásobí velikost i dobu `integrity_check` a nic nepřidá.

### 10.3 Agregace: 1 min vs. 5 min (365 dní, 20 řad)

| | 1 min | **5 min** | 1 h |
| --- | --- | --- | --- |
| řádků za rok | 10 512 000 | 2 102 400 | 175 200 |
| velikost za rok | 410,5 MB | 81,0 MB | 7,3 MB (10 let ≈ 73 MB) |
| čtení 1 řady 7 d | 18,3 ms (10 080 bodů) | 3,6 ms (2 016 bodů) | 0,2 ms |
| čtení 1 řady 30 d | 86,2 ms | 16,6 ms | 1,0 ms |
| čtení 1 řady 365 d | 1 084 ms | 226 ms | 17,8 ms |
| zápis za den | 178 MB (commit po 60 s), 65 MB (po 300 s) | 36 MB (po 300 s) | 3,2 MB |

**Závěr:** 5 min. Detail po 10 s pokrývá 30 dní surových dat; pro starší týdny 5 min (2 016 bodů na týden) stačí na jakýkoli graf. 1 min by zvětšila databázi i denní snímek asi 5× (410 MB místo 81 MB jen agregací) a čtení delších rozsahů rovněž 5×.

### 10.4 Odhad provozu (20 řad, doporučená varianta)

| | Po 1 roce | Po 10 letech |
| --- | --- | --- |
| `sample` 30 dní (C) | 105 MB | 105 MB |
| `agg_5m` 1 rok | 81 MB | 81 MB |
| `agg_1h` | 7 MB | 73 MB |
| `check_change`, `event` 2 roky | < 10 MB běžně (desítky změn denně); trvalé kmitání jedné kontroly viz 10.7 (hlásí `history.storage`) | totéž |
| **databáze celkem** | **≈ 200 MB** (změřeno 194,4 MB, 10.6) | **≈ 270 MB** |
| místo na disku (databáze + WAL ≤ 8 MiB + snímek + dočasný snímek při tvorbě) | ≈ 0,6 GB | ≈ 0,8 GB |

S 1min agregacemi by databáze měla asi 530 MB a místo na disku asi 1,6 GB. Trvalé kmitání = přechod při každém měření (8 640 přechodů denně při 10 s); každý zůstává samostatným řádkem (7.1), změřená velikost je v 10.7.

Denní logický zápis změřený v dlouhodobém cyklu (10.6) je asi 308 MB, z toho snímek 195 MB, tedy asi 112 GB za rok. Snímek se mezi dny změní asi o 5–6 % stránek (u databáze 200 MB asi 12 MB). Restic deduplikuje po blocích proměnné délky, takže přírůstek v repozitáři bude větší než změněné stránky, ale řádově v desítkách MB denně, ne celá databáze. Ověří se při prvních bězích Resticu (M7). Skutečný disk Nexusu a jeho dosavadní zápisy se ověří read-only (SMART) před nasazením.

### 10.5 Pokrytí benchmarku

| Oblast | Pokrytí |
| --- | --- |
| dlouhodobý cyklus zápis → agregace → mazání | 10.6: plná databáze po roce a týden provozu se všemi operacemi |
| současné čtení historie a zápis | 10.6: čtenář s vlastním spojením jen pro čtení po celý jeden den |
| WAL checkpoint | 10.6: největší WAL za den, dlouhá čtecí transakce a stav po jejím uvolnění |
| velikost databáze po pravidelné retenci | 10.6: velikost a volné stránky po každém dni |
| `secure_delete`, `synchronous=FULL` | 10.2 (`ON` proti `FAST` u retence, `FULL` proti `NORMAL` u commitu); cyklus 10.6 s doporučeným `FULL` a `FAST` |
| celkový logický objem zápisů | 10.6: `wchar` a `write_bytes` celého procesu za den včetně checkpointu, agregací, retence a snímku |
| změny stavu při trvalém kmitání | 10.7: velikost a zápis `check_change` při přechodu každých 10 s |

### 10.6 Dlouhodobý cyklus (doplněno při review)

Plná databáze jako po roce provozu (30 dní surových dat, 365 dní 5min a hodinových agregací, naplněno navrženými dotazy), pak 7 simulovaných dní: každou minutu dávka (`batch-guard`, vzorky, každých 30 min změna v `check_change`), po ní agregace uzavřených oken, na celou hodinu retence po dávkách, denně v 02:30 snímek s kontrolou a zveřejněním. Vše s `synchronous=FULL`, `secure_delete=FAST` a SQL z této dokumentace; 6. den navíc čtenář ve vlastním vlákně, po 7. dni dlouhá čtecí transakce.

| | Výsledek (7 dní) |
| --- | --- |
| velikost souboru | 194,4 MB na začátku, 194,6–194,9 MB po každém dni (přibývají jen hodinové agregace a změny); volných stránek 36–37, smazané stránky se hned znovu použijí |
| WAL | nejvýš 4,2–4,8 MB za den (automatický checkpoint po 1000 stránkách) |
| transakce za den | 1 436 dávek, 288 agregací 5 min, 24 hodinových, 24 + 24 retenčních |
| commit dávky (`FULL`) | medián 1,6 ms, p95 2,1–3,8 ms |
| logický zápis za den | `wchar` 308 MB, z toho snímek 195 MB a provoz (vzorky, agregace, retence, checkpointy) asi 113 MB; `write_bytes` 317 MB |
| snímek | Backup API 0,8 s, kontrola 2,0 s, `fsync`, SHA-256 a zveřejnění 0,6 s |
| souběžné čtení (6. den) | čtenář 264 dotazů (24 h surových dat, 7 d agregací, 1 h všech řad), p95 42,9 ms; commit dávky p95 4,4 ms |
| dlouhá čtecí transakce 6 h | WAL až 19,0 MB (checkpoint ho nemohl vrátit na začátek); 30 min po uvolnění 8,4 MB (oříznutí na `journal_size_limit`) |
| invarianty po cyklu | 172 300 5min oken = přepočet ze surových dat, 175 200 hodin = součet 5min oken, součet `n_samples` = počet surových vzorků, nejstarší vzorek na hranici retence, `integrity_check` a `foreign_key_check` v pořádku |
| selhání snímku | poškozená kopie (přepsaná kořenová stránka `sample`) se nezveřejnila, předchozí snímek i záznam beze změny, v `snapshot/` jen hotové soubory |
| zkouška obnovy (9.5 bez Resticu) | SHA-256 = záznam, `integrity_check`, `foreign_key_check`, identita a verze, `journal_mode` = `delete`, `meta.snapshot_utc` a počty řádků = záznam, čtecí dotazy vrací data, čtení nevytvořilo `-wal` ani `-shm` |

Závěry:

- Velikost se po pravidelné retenci nemění; `auto_vacuum` ani `VACUUM` nejsou potřeba.
- Provoz zapisuje asi 113 MB denně, víc než součet samostatných měření v 10.2 a 10.3 (asi 80 MB): každá dávka mění i řádek `run` a každá agregace `aggregation_state`, obojí jde do WAL i do checkpointu. S denním snímkem asi 0,3 GB/den (112 GB/rok), z poloviny snímek; řádově méně, než unese běžné NVMe.
- Čtenář s krátkými transakcemi zápis prakticky nezdrží. Dlouhá čtecí transakce WAL zvětšuje (19 MB za 6 h), proto pravidlo krátkých transakcí pro web (5.7) a hlídání velikosti WAL v `history.storage` (`WARNING` nad 64 MB, R16).

### 10.7 Změny stavu při trvalém kmitání (R4)

`--only changes` (2026-10-20): jedna kontrola s přechodem při každém měření po 10 s po dobu 24 h (8 640 přechodů), každý přechod ve vlastní dávce s `batch-guard` a `synchronous=FULL`, DDL a `insert-change` z této dokumentace. **Výsledek platí jen pro tyto předpoklady:** jedna trvale kmitající kontrola, krátké texty `note` (asi 20 znaků), u linky `details` s rozhraním a stavem, notebook se SQLite 3.45.1, stránka 4 KiB.

| Scénář | Bajtů na řádek (tabulka + index `ts`) | Za den | Za 2 roky (retence) | Logický zápis za den | Commit (`FULL`) |
| --- | --- | --- | --- | --- | --- |
| stav INFO ↔ CRITICAL s hodnotou | 99 | 0,85 MB | 0,62 GB | 113 MB | 1,3 ms |
| linka UP/DOWN se stavem INFO | 112 | 0,97 MB | 0,71 GB | 113 MB | 1,3 ms |

- Běžný provoz má desítky změn denně (< 10 MB za 2 roky). Každá trvale kmitající kontrola přidá zhruba 0,6–0,7 GB za 2 roky a stejně zvětší denní snímek: databáze s jednou takovou kontrolou by po 2 letech měla asi 0,9 GB místo 0,2 GB a na disku se snímky asi 2,7 GB.
- Při trvalém kmitání se dávka zapisuje každých 10 s: surové vzorky 88 místo 35 MB/den (10.2) a změny 113 MB/den (měřeno samostatně; ve společné transakci se vzorky se část zápisu sdílí). Horní odhad logického zápisu je proto asi 0,47 GB/den včetně snímku místo 0,31 GB, stále hluboko pod výdrží běžného NVMe.
- Proto `history.storage` hlásí `WARNING` nad 1 000 řádků jedné kontroly za 24 h a příčinu kmitání má odstranit správce; ukládání přechodů se neomezuje.

### 10.8 Co benchmark nedokládá

- Chování a časy na Raspberry Pi 5, jeho NVMe ani skutečné opotřebení SSD (jen logický objem zápisů).
- Studenou cache po restartu, výpadek napájení a skutečné chyby disku (ty testy M3 simulují, 8).
- Skutečná data Nexusu: generátor je syntetický; skutečný počet řad a interval budoucích kolektorů (úložiště, NAS, Restic) bude nejspíš menší než předpoklad 20 řad po 10 s.
- Kód zapisovače: měří se SQLite s navrženými dotazy a zjednodušený cyklus bez front a vlákna, ne budoucí `history.py`.

## 11. Plán M3 (malé kroky, každý s testy a schválením)

Vše jen na notebooku, nad dočasnými databázemi v testech; žádná cesta Nexusu, žádný Restic. Testy `unittest` se simulovaným časem (UTC i monotónním) a bez skutečného čekání; vlákno zapisovače je tenký obal kolem funkcí, které se testují synchronně.

| Krok | Obsah | Soubory | Testy (výběr) |
| --- | --- | --- | --- |
| M3.1a Základ databáze (**implementováno lokálně 2026-10-20**) | `history.py`: vytvoření nové databáze `test`/`development` na výslovné cestě (bez vytváření adresáře, bez přepsání souboru a jeho `-wal`/`-shm`/`-journal`, stavba v dočasném souboru a zveřejnění `os.link`), otevření pro zápis (kontrola hlavičky bez SQLite, zámek `flock` na `<jméno>.lock`, `mode=rw`, ověření `application_id`, `user_version`, otisku schématu, role a stroje, nastavení spojení podle 5.7, `quick_check`), otevření jen pro čtení (`mode=ro`, `query_only`, `NO_CKPT_ON_CLOSE`), kontrola verze SQLite ≥ 3.38 | `src/nexusstat/history.py`, `tests/test_history.py` | 51 testů: shoda `SCHEMA_SQL` a nastavení s dokumentací, pády v odděleném procesu (uprostřed inicializace, po zveřejnění, nečisté ukončení zapisovače s daty ve WAL, novější schéma jen ve WAL), pořadí `fsync`, samostatná použitelnost souboru, schéma vytvořené souboru = schéma z DDL dokumentace, cizí, prázdný, novější, starší a upravený soubor, role, stroj, chybějící adresář, přepis, zbylé `-wal`, nepovedené vytvoření, druhý zapisovač v témže i jiném procesu, čtenář bez zápisu a checkpointu, poškozená kopie; 14 kontrolních mutací odhaleno |
| M3.1b Zápis (**implementováno lokálně 2026-10-20**, 11.1) | katalog řad, pravidla přijetí (4.1) včetně rozlišení duplicity, kolize a konfliktu, kvalita času (4.3) jako vstup API, dávka s `batch-guard`, změny stavu a provozních údajů podle R4 (7.1), horní mez, uzavření nečistých běhů s `history.unknown_gap`, odmítnutí simulovaných dat, záznam ztrát (5.4) | `src/nexusstat/history.py`, `tests/test_history.py` | 49 nových testů (100 v modulu): nula vs. `UNKNOWN` vs. chybějící měření vs. `N/A`; simulovaná data; neznámá řada a jednotka i jednotka uložené řady; neplatné číslo; RX/TX podle rozhraní; každý přechod včetně `UNKNOWN` a linky při `INFO`; kmitání INFO ↔ CRITICAL (scénář A, 181 řádků, 90 epizod přes `state-episodes`) shodné s `ChangeRules`; dvě změny v jedné sekundě; opakovaná dávka, nejistý commit, vynechané číslo, konflikt, rollback uprostřed; restart, nečisté ukončení, horní mez, posun času zpět, kvalita času; pravidla přijetí shodná s `TimeRules` rozhodnutí po rozhodnutí; horní mez po restartu pro platnou, odmítnutou, smíšenou a opakovanou dávku, konflikt, `same_second`, jednotku a nedůvěryhodný čas; 49 kontrolních mutací odhaleno |
| M3.2a Agregace (**implementováno lokálně 2026-10-21**, 11.2) | uzavření okna (6.1) s kontraktem fronty `QueueState`, agregace s `progress-*` bez přepisu, konflikt s dokladem a zastavením, pozdní vzorky a posun `SampleGate` | `src/nexusstat/history.py`, `tests/test_history.py` | 35 nových testů (135 v modulu): jedno okno, sousední okna, hodina z 12 oken, nula, `UNKNOWN`, okno jen s `UNKNOWN`, prázdné a neúplné okno, hranice na sekundě, rezerva 120 s i 119 s, neznámá fronta, čekající vzorek, otevřená dávka, nesynchronizovaný čas, posun hodin zpět i dopředu, opakování, konflikt 5 min i 1 h, pád před `COMMIT` v odděleném procesu, selhání uprostřed vkládání, přerušení, restart, `SampleGate`, pozdní vzorek; shoda s přímým výpočtem i se smyčkou zapisovače benchmarku M2; podmínka 4 při posledním vzorku uprostřed i na hranici okna, zastaveném měření, obnově po hodině i po dnech, restartu během výpadku, skoku hodin dopředu a návratu, frontě a hodině bez nových dat; doklad konfliktu i jeho selhání; kontrolní mutace v 13.6 |
| M3.2b Retence (**implementováno lokálně 2026-10-21**, 11.3) | plán bez zápisu a provedení po hodinových transakcích (nejvýš 24 za běh) s hranicí podle potvrzené agregace a invarianty, kontrakt času `ClockState` a pauza po `clock.jump`, trvalá hranice `retained_since.*`, epizody s neznámým začátkem po retenci | `src/nexusstat/history.py`, `tests/test_history.py`, `docs/HISTORY.md` (7, 7.1) | 47 nových testů (182 v modulu): hranice tabulek přesně na sekundě, přestupný rok, prázdná a dlouhá databáze, neagregované a otevřené okno, opožděná a selhaná agregace, řádky za `done_until`, 5min okna před hodinovou agregací, invarianty, idempotence, M3.2a a `SampleGate` po retenci, limit hodiny a 24 transakcí, střídání tabulek, rychlé přechody, čas (neznámý, nestabilní, skok, data z budoucnosti, opakovaná kontrola v transakci), 90 epizod CRITICAL, UNKNOWN a linka přes hranici, pád v odděleném procesu, výjimka uprostřed DELETE, Ctrl+C, `SQLITE_BUSY`, plný disk, nejistý commit, neúplné DELETE, restart, souběžný čtenář; finální kontrola: `retained_since` po každém kroku (bez přeskočení, nikdy neklesá, vlastní u každé tabulky, mezery zůstávají), epizody skutečným SQL (začátek před hranicí s koncem i bez dalšího přechodu, začátek přesně na hranici, stejná sekunda, UNKNOWN, linka, shoda s úplnou historií), 10 let a 29. února, data z budoucnosti ve všech tabulkách, dry-run nad souborem otevřeným jen pro čtení |
| M3.3a Vlákno zapisovače (**implementováno lokálně 2026-10-21**, 11.4) | samostatné vlákno s jediným spojením, omezené fronty měření, změn a událostí, dávky po 60 s a okamžitý zápis změn a událostí, opakování s odstupem podle druhu chyby, znovuotevření, nevratné chyby, snímek stavu, fronta před synchronizací a `derived`, záznamy ztrát, ukončení s limitem | `src/nexusstat/history_worker.py`, `src/nexusstat/history.py` (`sqlite_error`, `add_change`, `track_changes`, `rebind`), `tests/test_history_worker.py` | 49 testů: jediný zapisovač, spojení ve vlákně i jeho zavření, pořadí s bariérami (předání během `COMMIT`, opakování zapečetěné dávky, nejistý commit bez dvojího započtení), shoda `add_change` se zapisovačem M3.1b, nespojitost po zahozených změnách včetně ztraceného celého cyklu a kontraktu `episode-dropped-changes`, pevný paměťový strop, mezní intervaly a strop velikosti události, neblokující předání, pořadí a hranice dávky (59 a 60 s), všechny přechody a kmitání, přetečení všech tří front a kapacita pro 1–21 kmitajících kontrol, `SQLITE_BUSY` (skutečný zámek), skutečný `SQLITE_FULL`, `SQLITE_IOERR`, nejistý commit, skutečná ztráta práva zápisu, `IntegrityError`, poškození, chybějící soubor, zaseknuté I/O, nesynchronizovaný a neznámý čas, ztráta synchronizace, čisté a neúspěšné ukončení, restart a pád procesu |
| M3.3b-1 Napojení na měření (**implementováno lokálně 2026-10-22**, 11.5) | nová sekce `[history]` (výchozí `enabled = false`, cesta bez výchozí hodnoty, jen role `development`, odmítnutí síťového úložiště), volání `HistoryWorker` ze smyčky `run.py` přes `history_link.py` se skutečným monotónním časem měření, read-only zdroj synchronizace hodin a `boot_id` (`clock_sync.py`), obsluha SIGINT i SIGTERM přes `stop()` s limitem 10 s a výpisem nezapsaných dat, stav `history.storage` z `WorkerStatus`, strop velikosti textu jednoho výsledku | `src/nexusstat/history_link.py`, `src/nexusstat/clock_sync.py`, `config.py`, `run.py`, `history_worker.py`, `config/config.example.toml`, `tests/test_history_link.py`, `tests/test_clock_sync.py`, doplnění `tests/test_config.py`, `tests/test_run.py`, `tests/test_history_worker.py` | 176 nových testů (904 celkem): výchozí stav vypnuto a běh shodný s v0.6, zapnutá historie, výsledky obou kolektorů ze vzorových souborů, nula / `UNKNOWN` / chybějící měření, přechody a R4, síť UP/DOWN, simulovaná data, první vzorek CPU a rychlosti `UNKNOWN`, víc intervalů a hranice dávky, restart procesu s horní mezí, zámek jiného zapisovače, chybějící, cizí a síťová cesta bez jakékoli změny souboru, zaplněné fronty, neznámá i chybějící synchronizace, odvozený čas, ztráta a obnova synchronizace, skok času, SIGINT a SIGTERM (skutečné signály), nečistý `StopReport`, neomezeně velký vstupní text, validace konfigurace; 24 kontrolních mutací odhaleno; krátký skutečný běh na notebooku |
| M3.3b-2 Agregace a retence z vlákna (**implementováno lokálně 2026-10-22**, 11.6) | volání `aggregate()` po zapsané dávce se stavem fronty `QueueState` z vlákna, retence nejvýš jednou za hodinu s `ClockState` z vlastní evidence stability hodin (≥ 24 h), stav údržby v `history.storage` (konflikt, selhání, zastavená retence, velikost WAL, zpoždění agregace), strukturovaný údaj o selhání kolektoru v `run.Collectors`, názvy `HISTORY` a `HIST SNAPSHOT` v `oled_text.py` | `history_worker.py`, `history_link.py`, `run.py`, `oled_text.py`, testy | 87 nových testů (987 celkem): kontrakt fronty (prázdná, čekající, nesynchronizovaná, zapečetěná dávka, měření bez výsledků), agregace po potvrzené dávce, 5min i hodinová okna, dohánění tří dnů po hodinách, nová data během dohánění, konflikt (zastavení a diagnostika), selhání údržby bez zastavení zápisu, nevratná chyba, ukončení během agregace i retence, evidence stability (start se i bez synchronizace, první synchronizace, ztráta, obnova, neznámý stav, krátký výpadek, restart procesu, skok dopředu i dozadu, práh 2 s, hranice 24 h), retence po 24 h i těsně před, retence pod hranicí agregace, hodinový odstup, velikost WAL (i přes symlink, chybějící i nečitelný), pravdivost diagnostiky, vypnutá historie bez údržby; po review navíc průběžnost doložené stability (pravidelná potvrzení, jen první potvrzení, výpadek 6 h i 25 h, nečerstvý doklad, uspání), evidence `clock.jump` (zápis, dohledatelnost, přechodná chyba, blokace retence, nezapsaná ztráta události) a hladovění zápisu (měření každou sekundu při dohánění tří dnů, kmitání, nová událost, ukončení) |
| M3.4 Snímek a obnova | snímek (9.2) jen při synchronizovaném čase, se záznamem a stavem `history.snapshot` z paměti (9.3), zkouška obnovy na notebooku (9.5), rozpoznání obnoveného snímku (9.6), read-only výpis stavu (`python3 -m nexusstat.history info <soubor>`) a převzetí souboru na náhradním stroji (`adopt`) | `history.py`, testy, `OPERATIONS.md` | snímek během zápisu je konzistentní a bez `-wal`; poškozený snímek se nezveřejní; stáří snímku; obnovený soubor projde kontrolami a čtecími dotazy |

- Kroky M3.1, M3.2 a M3.3a nemění chování aplikace (nové moduly bez napojení). M3.3b-1 mění `run.py` a `config.py`: historie je volitelná, ve výchozím stavu vypnutá (bez sekce `[history]` zůstává chování dnešní v0.6) a zapíše jen do výslovně zadané databáze role `development`. Vše je hotové jen lokálně na notebooku; produkční role se nevytváří ani neotevírá a na Nexusu se nic nemění.
- Ruční zkouška na notebooku jen s cestou mimo repozitář v lokálním `config/config.toml` (ignorováno Gitem), např. v `~/.local/state/`; DB soubory chrání i `.gitignore` (`*.sqlite3`, `*.db`).
- Test veřejných souborů (`PublicDataTest`) od review prochází i `tools/`.
- Mimo M3: Nexus, Restic, systemd, produkční data, web (M6), nasazení (M7).

### 11.1 Zapisovač M3.1b (synchronní, bez vlákna a front)

Rozhraní pro budoucí jediný zapisovač z M3.3; schéma v1 se nezměnilo.

| Volání | Co dělá |
| --- | --- |
| `HistoryRecorder(open_writer(...))` | jen nad databází otevřenou pro zápis (zámek, role, stroj z M3.1a); čtecí spojení odmítne |
| `start_run(app_version, interval_s, now_utc, boot_id=None, ts_quality="synced")` | jedna transakce: uzavře nečisté běhy (`close-unclean-runs`), přečte horní mez (`high-water`) a `done_until` úrovně `5m`, vloží `run`, zapíše `process.unclean_stop` a `history.unknown_gap` (od `last_write_utc`; bez vzorku od `started_utc` nečistého běhu, jen měl-li jeho `process.start` důvěryhodný čas, jinak bez začátku; do startu, při `unsynced` bez konce) a `process.start`; vrací `run_id` |
| `begin_batch()` | dávka s číslem `last_batch + 1`; další jen po zapsání předchozí, nic se tedy nezahodí |
| `add_results(batch, results, ts_quality)` | výsledky jednoho měření: změny podle R4 (`ChangeTracker`) a vzorky podle katalogu a pravidel přijetí (`SampleGate`, 4.1) |
| `add_event(batch, Event(...))` | vlastní událost (např. budoucí `flapping.start`); kvalita času povinná |
| `write(batch)` → `BatchResult` | `BEGIN IMMEDIATE`, `batch-guard` (číslo dávky, `last_batch`), vzorky (`insert-sample`, `rowcount`, `sample-conflict`), `check_change`, `event`, `run-last-write` (horní mez jen z vložených vzorků), `COMMIT`; při chybě SQLite `ROLLBACK` a `HistoryError("write")`, při jiné výjimce (např. Ctrl+C) `ROLLBACK` a výjimka dál. Opakování zapsané dávky vrátí `already_written`, dávka mimo pořadí, dávka mimo `begin_batch()` a zapečetěná dávka změněná po odeslání `HistoryError("batch")` |
| `finish_run(now_utc, ts_quality)` | čisté ukončení (`stopped_utc`, `clean_stop = 1`, zbývající souhrn ztrát, `process.stop`); opakované volání nic nezdvojí |
| `run_info()`, `high_water()` | evidence běhu a horní mez i přes spojení jen pro čtení |

- **Katalog** (`SERIES`): `system.cpu_usage` a `system.memory` a `disk.root` (%), `system.cpu_temp` (°C), `network.rx_rate` a `network.tx_rate` (B/s, objekt = `details["interface"]`). Řada (`metric`) vznikne až s prvním přijatým vzorkem, a to v transakci dávky.
- **Hodnoty:** 0 je 0, `UNKNOWN` je `NULL`, chybějící výsledek nic, `N/A` jen změnu stavu. Mimo pravidla 4.1 se vzorek nezapíše a započítá také jako `unknown_series` (číslo kontroly mimo katalog), `no_subject` (síť bez rozhraní), `missing_value`, `invalid_value` (NaN, nekonečno), `unit_mismatch` (jiná jednotka než katalog nebo než uložená řada) a `simulated`.
- **Horní mez** (`run.last_write_utc`) = nejnovější vzorek, který běh skutečně vložil. Vzorek odmítnutý pravidly přijetí, časem nebo jednotkou se do dávky vůbec nedostane; vzorek odmítnutý databází (konflikt, jiná jednotka uložené řady) a duplicita mez neposunou; opakovaná dávka nic nemění. Ověřeno i restartem po každém z případů (`HighWaterTest`).
- **Kvalita času** je vstup volajícího (zdroj synchronizace Raspberry Pi se tu nezjišťuje): `synced` a `derived` dovolí vzorky, `unsynced` ne. Čas před 2026-01-01 je vždy `unsynced`, i u startu, ukončení a událostí; `Event` s takovým časem a jinou kvalitou se odmítne. Změny a události se zapíšou vždy se svou kvalitou. Zapisovač žádný čas neodhaduje ani nedopočítává. `clock.jump` se pozná ze změny `observed_utc − expires_monotonic` o víc než 2 s.
- **Čas běhu:** tabulka `run` kvalitu času nemá (schéma v1); kvalitu startu a ukončení nese `process.start` a `process.stop`. `close-unclean-runs` dá běhu bez vzorku `stopped_utc = started_utc` i při nedůvěryhodném startu; zapisovač takový čas jako hranici mezery nepoužije.
- **Souhrn ztrát** `history.rejected` (`WARNING`): `data = {"total": n, "reasons": {důvod: {"count", "from", "to"}}}`; rozsah UTC jen z důvěryhodného času, u `time_unsynced` a `simulated` jen počet. První souhrn jde s nejbližší dávkou, další nejvýš jednou za hodinu monotónního času (simulace ho neposouvá), zbytek při `finish_run`. Souhrn jede se zapečetěnou dávkou: první pokus o zápis dávku zapečetí včetně obsahu (`add_results`, `add_event` i přímá změna seznamů se pak odmítnou), takže se po chybě zapíše jejím opakováním právě jednou. Totéž měření odeslané znovu (stejný `CheckResult`) je duplicita, ztráta se nezapočte podruhé. Po nejistém commitu se počitadla potvrzeného pokusu (konflikty, duplicity, jednotka uložené řady) promítnou při opakování právě jednou.
- **Omezení:** znovuotevření spojení po nejistém commitu řeší vlákno M3.3a (11.4); po neočekávané `IntegrityError` zapisovač přejde do `FAILED` a čeká na zásah správce, automatické rozdělení dávky je odložené. Detektor kmitání neexistuje (7.2).

**Otevřené požadavky M3.3 z 4.3:**

1. Fronta měření před první synchronizací s monotónním časem měření a dopočtem `derived` času po synchronizaci (`ts = monotonic_měření + (UTC − monotonic)`). **Hotovo v M3.3a** (vlákno zapisovače, 11.4).
2. Monotónní rozsah ztracených měření bez důvěryhodného času (přetečení fronty, ukončení bez synchronizace) v JSON `event.data` (`mono_from`, `mono_to`, `boot_id`), ze skutečného monotónního času měření z fronty. **Hotovo v M3.3a** pro záznamy ztrát vlákna (`history.gap`); souhrn `history.rejected` zapisovače (M3.1b) nese dál jen počty. `boot_id` dodá M3.3b.
3. `history.gap` při přetečení fronty (rozsah v UTC, po synchronizaci `derived`), `clock.synced` s rozsahem odvozených vzorků, `clock.sync_lost` a `clock.sync_restored`. **Hotovo v M3.3a.**
4. Zdroj stavu synchronizace na Raspberry Pi zůstává neověřený (gate 12.4, M7); napojení zdroje je M3.3b.

### 11.2 Agregace M3.2a (synchronní, součást zapisovače)

Agregace je metoda jediného zapisovače `HistoryRecorder`: používá jeho zapisující spojení, běh (`run_id` pro události), `SampleGate` a otevřenou dávku. Druhé zapisující spojení ani vlákno nevzniká; volat ji bude vlákno M3.3 po zapsané dávce.

| Volání | Co dělá |
| --- | --- |
| `QueueState.empty()`, `QueueState.waiting_since(ts)`, `QueueState.unknown()` | výslovný stav fronty M3.3 (podmínka 2): prázdná; nejstarší čekající měření má čas `ts` (jen důvěryhodný UTC); neznámý stav nebo měření čekající na synchronizaci. Parametr je povinný, `None` ani nic jiného se za prázdnou frontu nepovažuje |
| `aggregate(now_utc, ts_quality, queue)` → `AggregationResult` | hranice uzavření = nejmenší z (1) `now − 120 s`, (2) okno nejstaršího čekajícího vzorku ve frontě a v otevřené dávce, (4) okno nejnovějšího zapsaného vzorku; (3) bez `synced` času nic. Pak nejvýš jedna transakce `progress-5m` + `aggregate-5m` (do konce hodiny) a jedna `progress-1h` + `rollup-1h` (celá hodina s hotovými 5min okny). Výsledek: potvrzené `done_until` obou úrovní, počty vložených oken, `blocked` (`time_unsynced`, `queue_unknown`) a `more` (zbývá práce na další volání) |

- **Atomicita:** chyba SQLite i jiná výjimka (Ctrl+C) uvnitř transakce ji celou vrátí, `done_until` ani `SampleGate` se neposunou; pád procesu před `COMMIT` nezanechá nic (test v odděleném procesu). Opakování nad hotovým intervalem odmítne `progress-*` (0 řádků) a nic nezapíše.
- **Konflikt** (okno za `done_until` už existuje): `ROLLBACK`, událost `history.aggregation_conflict` (`ERROR`), `HistoryError("conflict")` a zastavení agregace do konce běhu (6.2). Jiná `IntegrityError` bez existujícího okna je obecná chyba zápisu.
- **Hodnoty:** `n_samples` včetně `UNKNOWN`, `n_valid`, `min`, `max`, `sum`; průměr = `sum_value / n_valid`; okno jen s `UNKNOWN` má `n_valid = 0` a prázdné hodnoty, prázdné okno řádek nemá; hodina je přesný součet oken (ne průměr průměrů).
- **Ekvivalentní mutace:** odstranění kontroly „hodina až po všech 5min oknech“ v kódu chování nezmění, protože totéž vynucuje SQL `progress-1h` (`:end <= done_until '5m'`); druhou vrstvu dokládá samostatný test.
- **Dohánění a `more`:** `more` hlásí jen práci, kterou jde udělat hned se stejným vstupem (hranici neomezenou frontou, otevřenou dávkou ani daty); každé volání s `more` posune `done_until`. Volající (M3.3) smí volat agregaci znovu, dokud `more` platí, a nezacyklí se: po výpadku měření se 3 dny dohoní 73 voláními po nejvýš hodině oken, bez nových dat `more` neplatí. Hotová 5min okna bez hodinového souhrnu se dosčítají i bez nových dat.
- **Doklad konfliktu:** výjimka i zastavení uvádějí, zda byl doklad `history.aggregation_conflict` potvrzeně zapsán („doklad … zapsán“), nebo ne („… se nepodařilo potvrdit“, i při nejistém commitu). Nepodaří-li se konflikt ověřit (chyba dotazu na existující okna), je to obecná chyba zápisu (`HistoryError("write")`) a agregace se nezastaví.
- **Co musí `QueueState` v M3.3 vědět:** čas UTC nejstaršího měření, které ještě neprošlo `add_results` (čeká ve frontě nebo na opakování zápisu); zda nějaké měření čeká na synchronizaci hodin (pak `unknown()`); měření ve stavu sběru (kolektor ještě běží) pokrývá rezerva 120 s jen tehdy, když časový limit kolektoru je kratší, jinak ho musí fronta hlásit od začátku cyklu. Vzorky otevřené dávky zapisovač započítá sám (i zapečetěné po nejistém commitu a i přidané mimo `add_results`).
- **Neimplementováno:** retence a mazání (M3.2b), vlákno, fronta, `history.storage`, napojení na `run.py` a sekce `[history]` (M3.3), produkční nasazení (M7).

### 11.3 Retence M3.2b (synchronní, součást zapisovače)

| Volání | Co dělá |
| --- | --- |
| `ClockState.synced_for(stable_s)`, `ClockState.unknown()` | kontrakt času s M3.3: synchronizovaný čas s doloženou dobou stability, nebo neznámý stav; povinný parametr, `None` se nepřijímá |
| `retention_plan(connection, now_utc, clock)`, `recorder.retention_plan(now_utc, clock)` → `RetentionPlan` | dry-run: pro každou tabulku časová hranice, hranice z agregace, výsledná hranice, `limited_by_aggregation`, nejstarší řádek, počet kandidátů, invariant dalšího kroku; `blocked` (čas) a `done_until` obou úrovní. Nic nezapíše |
| `recorder.retain(now_utc, clock, plan=None, max_transactions=24)` → `RetentionResult` | jeden běh údržby: mazací transakce po hodině, tabulky se střídají; výsledek nese ověřené počty smazaných řádků, `stopped` (tabulka a důvod), `retained_since`, počet kroků ověřených po nejistém commitu, `blocked` a `more` (zbývá práce) |
| `retained_since(connection)` | trvalé hranice retence z `meta`: začátek zachované historie každé tabulky (ne záruka souvislých dat) |
| SQL `retention-unknown` (7.1) | úseky běhů s neúplnou historií kontroly po retenci a stav na nich, je-li znám |

- Pořadí tabulek v běhu: `sample`, `agg_5m`, `agg_1h`, `check_change`, `event`; `run`, `metric`, `meta` a `schema_migration` se nemažou nikdy.
- **Neimplementováno:** volání retence na celou hodinu z vlákna, skutečný zdroj `ClockState` a stav `history.storage` (M3.3), `VACUUM` (není potřeba), produkční nasazení (M7).

### 11.4 Vlákno zapisovače M3.3a (`history_worker.py`, nenapojeno)

| Volání (hlavní vlákno, bez SQLite) | Co dělá |
| --- | --- |
| `HistoryWorker(path, role, host_id, app_version=…, interval_s=…, boot_id=None, monotonic=…, utc_now=…, opener=…)` | připraví fronty; databázi ještě neotevírá. Hodiny a otevírání jsou vyměnitelné (testy) |
| `start(time_synced=None)` | spustí jediné vlákno zapisovače; to otevře databázi (`open_writer`, zámek `flock`), zahájí běh a databázi později samo zavře |
| `submit(results, measured_monotonic, time_synced)` | neblokující předání jednoho měření: n-tice zmrazených `CheckResult`, monotónní čas měření a stav synchronizace (`True`, `False`, `None` = neznámý). Změny stavu (R4) se spočítají hned, simulace změny nevytváří |
| `submit_event(event)` | důležitá událost do vlastní fronty; probudí zapisovač hned. Událost větší než 16 KiB (`MAX_EVENT_BYTES`) nepřijme a započte jako ztrátu `event_too_large` |
| `status()` → `WorkerStatus` | snímek pro budoucí `history.storage`: `STARTING`/`RUNNING`/`DEGRADED`/`STOPPED`/`FAILED`, potvrzené dávky a čas posledního commitu, čekající měření, hodnoty, změny a události, nepotvrzená dávka, data čekající na synchronizaci, časová základna (`unsynced`, `synced`, `sync_lost`), ztráty od startu a ještě nezapsané, poslední chyba a její druh, počet neúspěchů a čas do dalšího pokusu |
| `flush(timeout)` | okamžitý zápis všeho zapisovatelného; `True` jen po potvrzení |
| `stop(timeout=10)` → `StopReport` | čisté ukončení (5.5): `completed` (vlákno skončilo v limitu), `clean_stop` (potvrzený `finish_run`, jen když vlákno skončilo), jistě nezapsané položky (fronty a dávka vrácená před `COMMIT`) a `in_doubt` (dávka, jejíž `COMMIT` probíhá nebo nemá známý výsledek); potvrzené položky jsou v `status.confirmed_*` |

- **Vlastnictví:** spojení SQLite vzniká, používá se a zavírá jen ve vlákně zapisovače (`check_same_thread` zůstává zapnuté, jiné vlákno ho použít nemůže). `HistoryRecorder` i zapečetěnou dávku má jen to vlákno; hlavní vlákno sdílí jen fronty a stav pod zámkem, který se nikdy nedrží během I/O.
- **Dávka:** nová vznikne jen bez nepotvrzené předchozí; neúspěšná zůstane zapečetěná a opakuje se beze změny. Zápis spustí nejstarší zapisovatelné měření staré 60 s (monotónní čas), čekající změna, událost nebo zapisovatelný záznam ztráty, `flush` a ukončení. Čekání je `Condition.wait` s limitem nejvýš 10 s, bez aktivního dotazování.
- **Pořadí:** novější data nikdy nepředběhnou nepotvrzenou dávku (nová vznikne až po potvrzení předchozí, zapečetěná se opakuje beze změny); předání během `COMMIT` jdou do další dávky. Změny přidané přes `add_change()` jsou řádek po řádku shodné se změnami, které by spočítal zapisovač M3.1b (test).
- **Přetečení:** zahodí se nejstarší položka; počty a dostupné hranice (UTC jen z důvěryhodného času, jinak monotónní) zapíše další úspěšná dávka jako `history.gap` (`WARNING`, nad hodinu `ERROR`), `history.changes_dropped` nebo `history.events_dropped` (`ERROR`). Ztráta změn ponechá nejnovější změny: poslední zapsaný stav je skutečný a první zachovaná změna nese předchozí stav (`from_state`), takže CRITICAL se nikdy nezobrazí jako falešné OK. `history.changes_dropped` nese i počty a hranice po kontrolách (`data.checks`, dotaz `changes-dropped`); `state-episodes` nespojitost přizná (konec a začátek `NULL`) místo vymyšlené délky závady a zahozený celý cyklus přizná kontrakt čtení epizod s dotazem `episode-dropped-changes` (7.1).
- **Zapojeno v M3.3b-1 (11.5):** `run.py`, sekce `[history]`, SIGINT a SIGTERM, skutečný zdroj synchronizace a `boot_id`, kontrola `history.storage`. **Zapojeno v M3.3b-2 (11.6):** agregace a retence z vlákna, stav údržby v diagnostice.
- **Známá omezení:** paměťový strop fronty měření počítá výsledky, ne bajty, ale text jednoho výsledku je od M3.3b-1 omezený na 512 B UTF-8 (5.2), takže mez v bajtech je daná i tak. Čtenář epizod musí dodržet kontrakt 7.1 (`episode-dropped-changes`); webový čtenář zatím neexistuje.

### 11.5 Napojení na měření M3.3b-1 (`history_link.py`, `clock_sync.py`)

| Volání | Co dělá |
| --- | --- |
| `open_history(config, app_version=…)` | podle sekce `[history]` postaví `HistoryWorker` (role vždy `development`, otisk stroje z `/etc/machine-id`, `boot_id` read-only z `/proc`) a vrátí `HistoryLink`; vypnutá historie vrací `None`. Databázi neotevírá ani nevytváří |
| `HistoryLink.start()` | zjistí stav synchronizace hodin a spustí vlákno zapisovače; druhé spuštění je chyba |
| `HistoryLink.record(results, measured_monotonic)` | předá smyčkou vyhodnocené výsledky a stav synchronizace zjištěný při tomto měření; výjimku do smyčky nikdy nepustí (započítá ji a jednou vypíše) |
| `HistoryLink.check(now, observed_utc)` | kontrola `history.storage` ze snímku `WorkerStatus` (bez SQLite); do historie se nepředává |
| `HistoryLink.stop()` | řízené ukončení s limitem 10 s a výpisem doložených skutečností; druhé volání nic nedělá a nic netvrdí |
| `ClockSync.synced()` | `True` (doloženo), `False` (prokazatelně nesynchronizováno), `None` (nezjištěno). Jediný zdroj aktuálního stavu je `adjtimex` (4.3) |
| `timesyncd_first_sync()`, `first_sync_note()` | doklad **dřívější** první synchronizace v tomto bootu a krátký text do výpisu; o aktuálním stavu hodin nerozhodují a mezi zdroji (`SOURCES`) nejsou |
| `read_boot_id()` | `boot_id` z `/proc/sys/kernel/random/boot_id`; neplatný nebo nečitelný údaj dá `None` a restart systému pak od restartu procesu odlišit nejde |

- **Konfigurace** (`[history]`, vzor v `config/config.example.toml`): `enabled` (výchozí `false`) a `path`. Žádná výchozí cesta, žádná volba role (vždy `development`; produkční roli `history.py` v tomto kroku odmítá), žádné vytváření databáze ani adresáře. `path` musí být absolutní cesta k souboru s příponou `.sqlite3` (stejné pravidlo jako `.gitignore`); `enabled = true` bez cesty, neznámý klíč i cizí typ hodnoty jsou chyba a program skončí kódem 2 ještě před prvním měřením. Bez sekce `[history]` je běh funkčně shodný s v0.6 a nevznikne ani spojení SQLite.
- **Ověření úložiště** (`check_local_path`, při každém otevření, protože připojení se za běhu mění): rozhoduje typ souborového systému nejdelšího bodu připojení, který je předponou cesty **po rozvinutí symlinků** (`os.path.realpath`, takže symlink v poslední části, symlink v nadřazeném adresáři i `..` v cestě ukazují na skutečné místo); při stejně dlouhém bodu platí pozdější záznam, protože pozdější připojení překryje starší. Odmítne se: síťové úložiště (`cifs`, `smb*`, `nfs*`, `9p`, `ceph`, `sshfs`, `davfs`, `lustre`, … včetně `fuse.*` variant a bind mountů, které mají v tabulce vlastní řádek s typem původního systému), `autofs` (cíl se připojí až při prvním přístupu a bývá síťový) a **stav, kdy úložiště ověřit nelze** (nečitelná nebo neúplná tabulka připojení, cesta mimo všechny body připojení). Zápis se tedy neprovede ani při nejistotě. Lokální systém připojený dovnitř síťového (`/srv/nas/lokalni`) vyhrává delším bodem připojení a projde. **Zbytkové riziko:** mezi ověřením a otevřením může někdo úložiště vyměnit; okno zužuje druhá kontrola po otevření (při změně se spojení i zámek hned uvolní), odstranit ho úplně by znamenalo izolaci na úrovni jádra (Landlock), která na cílovém Nexusu není a není požadavkem tohoto řešení. Zbývající možnost záměny mezi oběma kontrolami je **provozní riziko k posouzení v M7** (spolu s volbou cesty a práv na Nexusu), ne nedořešená chyba tohoto kroku. **Volba `/proc/self/mounts` místo `/proc/self/mountinfo`:** o rozhodnutí rozhoduje typ souborového systému, který oba soubory uvádějí shodně (u bind mountu typ původního systému) a oba používají stejné osmičkové kódování bodu připojení; `mountinfo` navíc nese jen podstrom zdroje, ID připojení a propagaci, což by žádný výsledek nezměnilo. Kdyby bylo někdy potřeba rozlišit, z jakého podstromu bind mount pochází, je přechod na `mountinfo` změnou jen v parsování.
- **Datový tok:** smyčka měří ve stejném intervalu jako dosud, prahy vyhodnocuje `AlarmEvaluator` beze změny a do historie jde až **finální vyhodnocený** výsledek se skutečným monotónním časem měření (`now` z téže smyčky, ze kterého se počítá i platnost). Zápis dělá vlákno zapisovače, takže v měřicí smyčce není žádný blokující přístup k SQLite. Kontrola `history.storage` se připojí k výsledkům až po předání, takže se do historie nikdy nedostane (8).
- **Simulovaná data:** `simulated=True` odmítá zapisovač sám (4.6) a `run.py` historii staví jen nad skutečnými kolektory (`live_monitor`); simulátor ani náhled zapisovač nevytvářejí.
- **Ukončení:** `Stopper` nastaví obsluhu SIGINT a SIGTERM jen v hlavním vlákně (jinde Python signály doručit neumí) a vyvolá `KeyboardInterrupt`, takže ukončení jde stejnou cestou jako dosud Ctrl+C; výpis signál pojmenuje (`Ukončeno (SIGTERM)`). Během dopisování front další signál smyčku nepřeruší, aby poslední zápis neskončil v polovině (zaseknutý proces jde dál ukončit SIGKILL). `stop()` se volá nejvýš jednou, i při výjimce ve smyčce (`finally`). **Čisté ukončení se hlásí jen při potvrzeném `finish_run`, skončeném vlákně, prázdných frontách a bez nejisté dávky**; jinak výpis uvede počty jistě nezapsaných položek, počet položek s nejistým výsledkem commitu, stav zapisovače, ztráty od startu a ztráty, které v databázi nejsou. Vlákno zaseknuté v systémovém I/O se nepřerušuje a netvrdí se, že to jde: výpis to přizná a zámek drží až do konce procesu (5.5).
- **Velikost výsledku:** strop 512 B UTF-8 na výsledek (5.2); zkrácení je `WARNING`, ne ztráta měření, a zapečetěnou dávku nemění, protože se provádí už při předání. Důležitý diagnostický stav se zkrácením nemůže zmizet: `CRITICAL` s obřím textem se zapíše jako `CRITICAL` i s číselnou hodnotou a zkrácení má vlastní doklad. **Zbytkové omezení:** fronta měření omezuje počet hodnot (72 000), ale jedno měření se nikdy nezahodí celé, takže jeho strop je dán počtem kontrol × 512 B (dnešních 11 kontrol = nejvýš ≈ 5,6 kB na měření).
- **Ověřeno na notebooku (2026-10-22):** čtyři krátké běhy s intervalem 2 s do výslovně vytvořené databáze role `development` mimo repozitář (`~/.local/state/`; poslední běh jako kontrola po zpřísnění ověření úložiště v review): 21 měření, 126 vzorků v 6 řadách (`system.cpu_usage`, `system.cpu_temp`, `system.memory`, `disk.root`, `network.rx_rate` a `network.tx_rate` s objektem rozhraní), po jedné hodnotě `NULL` na první vzorek vytížení CPU a obou rychlostí v každém běhu, 44 řádků `check_change`, události jen `process.start` a `process.stop`, žádná ztráta ani `history.unknown_gap`, `aggregation_state` beze změny, čisté ukončení po SIGTERM s kódem 0.
- **Neimplementováno:** snímky a Restic (M3.4), detektor kmitání (7.2), web (M6), nasazení (M7). Automatickou agregaci a retenci doplnila M3.3b-2 (11.6).

### 11.6 Automatická údržba M3.3b-2 (agregace a retence z vlákna)

| Volání / stav | Co dělá |
| --- | --- |
| `_maintenance_due(now)` | rozhodne, zda je na řadě `aggregate`, `retain`, nebo nic. Jen s otevřenou databází, bez nepotvrzené dávky, bez nejistého commitu a nikdy při ukončování; zápis čekajících měření a změn se kontroluje dřív a má vždy přednost |
| `_queue_state()` | `QueueState` pro uzavírání oken a kvalita času „teď“: `empty()` jen při synchronizovaném čase, prázdné frontě a žádné vybrané či zapečetěné dávce; `unknown()` při čekajícím nesynchronizovaném měření (dostane až odvozený čas), při měření bez výsledků a při nedůvěryhodném čase |
| `_clock_state()` | `ClockState` pro retenci z vlastní evidence stability (4.3): `unknown()` bez doloženého souvislého úseku, bez čerstvého posledního potvrzení nebo s nezapsanou ztrátou událostí, jinak `synced_for(doložený rozsah pozorování)` |
| `_aggregate()` | jeden pokus o `HistoryRecorder.aggregate()`; `more` pokračuje hned, ale až po dalším kole smyčky (zápis a stop mají přednost) |
| `_retain()` | jeden běh `HistoryRecorder.retain()` (nejvýš 24 mazacích transakcí po hodině historie jedné tabulky) |
| `WorkerStatus.maintenance` | neměnný snímek: čas a hranice poslední agregace, vložená okna, blokace, doklad konfliktu, čas a výsledek retence, smazané řádky, doba stability hodin, selhání údržby a velikost `-wal` |

- **Plánování (monotónní čas):** agregace hned po potvrzené dávce (5.1), jinak nejvýš jednou za `AGGREGATION_INTERVAL_SECONDS` (60 s); retence nejvýš jednou za `RETENTION_INTERVAL_SECONDS` (1 h). Otevření databáze samo nespustí nic: první agregace přijde až po první dávce nebo po intervalu, první retence nejdřív za hodinu, takže start nepřidá žádnou transakci. Dohánění pokračuje bez čekání jen tehdy, když se potvrzená hranice skutečně posunula; bez posunu se čeká na interval, takže se nemůže zacyklit (hodina bez měření je také hotová práce, i když nevloží žádné okno).
- **Přednost zápisu:** `_next_action()` se ptá nejdřív na zápis, pak na údržbu. Jedno volání agregace uzavře nejvýš hodinu 5min oken a jednu hodinu, retence nejvýš 24 transakcí; mezi voláními se smyčka vrací k zápisu i k požadavku na ukončení. Měření v hlavním vlákně se nedotýká ničeho z toho a SQLite nevolá.
- **Selhání údržby nezastaví sledování:** přechodná chyba (zámek, plný disk, I/O) se jen započítá, ohlásí a odloží podle druhu (`BACKOFF`); stav zapisovače zůstane `RUNNING` a čekající měření se zapisují dál. Nevratná chyba (poškozená databáze, porušená integrita) zastaví zapisovač stejně jako při zápisu. Konflikt agregace (6.2) zapisovač nezastaví: zapíše doklad, agregace se do konce běhu nespouští a `history.storage` hlásí `ERROR`.
- **Bezpečnost retence:** maže jen `HistoryRecorder.retain()` se svými vlastními kontrolami v každé transakci (čas, `done_until`, invarianty, `rowcount`, 7). Vlákno k nim přidává jen doloženou stabilitu hodin: po startu procesu je nulová, takže **prvních 24 h běhu se nemaže nic**, a ztráta synchronizace, neznámý stav, skok času i mezera v pozorování ji vynulují (4.3). Konflikt agregace cestu k mazání neotevře: hranice `done_until` se přestane posouvat, takže retence nemůže smazat nic nového. Hranice agregace se neobchází: bez postupující agregace retence surové vzorky nemaže. `VACUUM` se nespouští.
- **Diagnostika:** hlavní vlákno čte jen snímek v paměti (`MaintenanceStatus`), nikdy SQLite; stavy a jejich pořadí jsou v 8. Velikost `-wal` se zjišťuje `stat` souboru ve vlákně zapisovače po zápisu a před údržbou, nad **rozvinutou** cestou (SQLite drží `-wal` u skutečného souboru, ne u symlinku); chybějící soubor i chyba čtení dávají „nezjištěno“ (`None`), nikdy nulu, a samy o sobě nic nehlásí. Velikost nad 64 MiB je podezření na dlouhou čtecí transakci (10.6). Žádný `VACUUM` ani vynucený checkpoint (regresní test nad zdrojovým kódem).
- **Neimplementováno:** snímky a Restic (M3.4), události `collector.failed`, `collector.stalled` a `collector.resumed` (strukturovaný údaj o selhání už existuje, 4.4), detektor kmitání (7.2), web (M6), nasazení (M7).

## 12. Rozhodnutí ke schválení

### 12.1 Už schválené požadavky (nejsou předmětem rozhodnutí)

Z `REQUIREMENTS.md` a zadání M2: SQLite jako hlavní databáze historie a nezávislost na InfluxDB; `sqlite3` ze standardní knihovny bez ORM a další služby; lokální SSD v `/srv/nexus/appdata/nexusstat/`, nikdy NAS/CIFS; přechody místo opakování stavu; mezera místo nuly; rozlišení nuly, `UNKNOWN`, chybějícího vzorku a `N/A`; simulovaná data mimo provozní historii; skutečné UTC a monotónní čas mimo trvalou historii; ochrana proti duplicitám, restartu, skoku času, výpadku a opoždění kolektoru a opakovanému zpracování; agregace s počtem platných vzorků, minimem, maximem a průměrem bez nul za chybějící vzorky; výpadek SQLite nezastaví měření, alarmy ani OLED; konzistentní kopie přes Backup API do existujícího Resticu s ověřenou obnovou, živý soubor se nekopíruje. Režim WAL je v požadavcích jen „zvážit“, proto je součástí R7.

### 12.2 Návrhy M2 a stav jejich schválení

**Stav schválení (správce, 2026-10-20):** schválena R1, R2, R3, R4, R5, R6, R7, R8, R9, R10, R12, R14, R16, R17 a R18 pro dokumentační návrh a lokální vývoj M3. R4 schválena ve finální podobě (každá změna stavu i provozně významného údaje samostatně, 7.1) po ověření regresních scénářů. R11, R13 a R15 jsou odložené do M7, protože vyžadují ověření skutečného Nexusu. Schválení architektury neznamená povolení produkčního nasazení, zásahu do Resticu ani přijetí rizika neopravené SQLite (12.4). Sloupec „Doporučení“ je doporučení předložené ke schválení.

Doporučení: **SCHVÁLIT** = lze přijmout tak, jak je; **UPRAVIT** = před schválením změnit podle sloupce rizik; **ODLOŽIT** = rozhodnout až s údaji, které na notebooku nejsou. „Revize“ označuje body změněné v architektonickém review, „revize 2“ v bezpečnostní revizi (obojí 2026-10-20).

| # | Rozhodnutí | Výchozí hodnota | Základ | Technické zdůvodnění | Rizika a alternativy | Doporučení |
| --- | --- | --- | --- | --- | --- | --- |
| R1 | retence surových vzorků | nejméně 30 dní; maže se jen agregované | výchozí návrh v požadavcích, ne kontrakt | detail po 10 s za měsíc zpět za 105 MB (10.2) | při stojící agregaci rostou (hlídá `history.storage`, limit místa); alternativa 14 dní = polovina místa i snímku | SCHVÁLIT |
| R2 | interval roční agregace | 5 min, nejméně 365 dní | požadavky: „1 nebo 5 min“ | 81 proti 410 MB, čtení 5× rychlejší, snímek menší (10.3) | starší než 30 dní jen po 5 min (min/max špičky zachovají); alternativa 1 min | SCHVÁLIT |
| R3 | dlouhodobé agregace | hodinové 10 let, bez denní tabulky | požadavky: „hodinové nebo denní“ | 7 MB za rok, den = 24 řádků při čtení | 10 let je odhad potřeby; alternativa trvale nebo denní tabulka navíc | SCHVÁLIT |
| R4 | změny stavu, retence `check_change` a `event` | každá skutečná změna diagnostického stavu nebo provozně významného údaje vlastním řádkem s přesným časem, nic se neslučuje; opakované měření beze změny nic; retence 2 roky; `WARNING` nad 1 000 řádků kontroly za 24 h; detektor kmitání později a odděleně (finální, 7.1 a 7.2) | požadavky: samostatná politika; pravidlo zadáno správcem 2026-10-20 | úplná historie přechodů: epizody, délky i četnost dopočitatelné; nejjednodušší pravidlo bez souhrnů | trvalé kmitání zvětší historii (10.7) a zrychlí dávky na 10 s; nic se kvůli tomu neomezuje | SCHVÁLIT |
| R5 | uspořádání tabulky `sample` | WITHOUT ROWID, klíč (`ts`, `metric_id`) | nový návrh | 7,6× méně zápisů než (`metric_id`, `ts`), stejná velikost, nejlevnější retence (10.2) | čtení jedné řady asi 3× pomalejší a roste s počtem řad; alternativa B | SCHVÁLIT |
| R6 | čas a kolize v sekundě | celé Unixové sekundy UTC, hranice hodin a dnů v UTC; duplicita (totéž měření) neškodí, kolize v sekundě se nezapíše a započítá jako ztráta, konflikt s jinou uloženou hodnotou = `ERROR`, nikdy přepis (revize, revize 2) | UTC je požadavek, celé sekundy návrh | čitelné `datetime(ts, 'unixepoch')`, menší klíč; kolize jen po zpoždění 9–10 s | vzácná ztráta měření < 1 s po předchozím; alternativa milisekundy (+2 B/řádek, asi +10 % surových dat) | SCHVÁLIT |
| R7 | WAL, dávka, fronty, trvanlivost, jediný zapisovač | WAL, `synchronous=FULL`, dávka = nejvyšší stáří 60 s, změny ≤ 1 cyklus, fronta vzorků 1 h (360 měření, 7 200 hodnot), fronta změn 2 000, ochrana `last_batch`; jediné spojení pro zápis a checkpoint, ostatní `mode=ro` (revize, revize 2) | WAL „zvážit“ v požadavcích, zbytek návrh | 35 MB/den surových dat, 1 440 `fsync` denně; ztráta běžně ≤ 60 s, bez garance (5.5); jediný zapisovač vylučuje podmínky chyby WAL-reset (5.8) | oprava WAL-reset v SQLite notebooku i Debianu 13 UNKNOWN (gate 12.4); dávka 300 s (14 MB/den), 10 s (88 MB/den); `NORMAL` bez pevné hranice ztráty | SCHVÁLIT |
| R8 | čas před synchronizací | měření čekají s monotónním časem a po synchronizaci dostanou odvozený čas (`derived`); čas, který nelze určit (`unsynced`): vzorky se neukládají, ztráta jen s monotónním rozsahem, nikdy vymyšlený čas UTC; `ts_quality` u změn a událostí; ztráta synchronizace jen událost (revize, revize 2) | nový návrh | chybný čas po startu RPi by prošel horní mezí a uložil data doprostřed výpadku (4.5) | zdroj stavu synchronizace a RTC na Nexusu neověřené (M7); jednodušší alternativa: zahazovat do synchronizace (ztráta boot → NTP); zapisovat vždy = chybné časy | SCHVÁLIT (zdroj stavu ověřit v M7, gate 12.4) |
| R9 | alarm po restartu | `AlarmEvaluator` začíná znovu, stav z historie se neobnovuje | stávající schválené chování v0.4 | žádné potvrzení starými daty, `alarms.py` beze změny | trvající závada je po restartu až 3 měření (20–30 s) `INFO`; alternativa obnovovat stav | SCHVÁLIT |
| R10 | IPv4 a názvy rozhraní v historii | ukládat jen změny, jen lokálně, nikdy do veřejného statusu | zákaz IP ve veřejném statusu je požadavek; uložení návrh | diagnostika změn DHCP a rozhraní | interní adresy v databázi i v šifrované záloze Resticu na NAS; alternativa IPv4 neukládat | SCHVÁLIT |
| R11 | cesty a názvy | `appdata/nexusstat/history/history.sqlite3`, `appdata/nexusstat/snapshot/history-snapshot.sqlite3` + `.json` | `appdata` ze standardu Nexusu, podadresáře návrh | jednoduché pravidlo Resticu: vyloučit `history/`, zahrnout `snapshot/` | platný `PROJECT-STANDARD.md` na notebooku není k dispozici; M3 cesty nepotřebuje | ODLOŽIT (ověřit proti standardu před M7) |
| R12 | snímek | NexusSTAT sám jednou denně asi 30 min před Resticem, jen při synchronizovaném čase; rozpracovaný v `history/`, zveřejnění atomické se záznamem; kontrola `history.snapshot` 26/50 h z údajů v paměti (revize, revize 2) | Backup API a Restic jsou požadavek, mechanismus návrh | nemění úlohu Resticu, Restic nikdy nevidí rozpracovaný soubor, stáří je vidět (9.3) | Restic sám hlásí úspěch i se starým snímkem (upozorňuje NexusSTAT); asi 200–270 MB zápisu denně; alternativa volání z úlohy Resticu | SCHVÁLIT (čas spuštění nastavit v M7) |
| R13 | změna Resticu | vyloučit `history/`, zahrnout `snapshot/`, jen seznam cest se zálohou konfigurace; volitelná kontrola aktuálnosti v úloze jen jako varování | požadavek „živou DB vyloučit, kopii zahrnout“; konkrétní změna návrh | nejmenší zásah do zálohování | konfigurace Resticu je v soukromé dokumentaci; zásah jen se schválením a rollbackem | ODLOŽIT (M7, po read-only ověření úlohy) |
| R14 | test obnovy | po nasazení v M7, po každé změně schématu, nejméně jednou za čtvrtletí; postup 9.5 | ověřená obnova je požadavek, četnost návrh | snímek bez vyzkoušené obnovy není ověřená záloha | ruční práce; později automatický test do dočasného adresáře | SCHVÁLIT |
| R15 | limit volného místa | zastavit zápis pod 1 GiB nebo 5 % | nový návrh | ochrana systémového disku | velikost a obsazenost disku Nexusu neznámá; 5 % může být na velkém disku zbytečně mnoho | ODLOŽIT (hodnoty po zjištění disku; v M3 jen nastavitelné) |
| R16 | stav historie | kontroly `history.storage` (`HISTORY`, včetně `WARNING` při WAL nad 64 MB) a `history.snapshot` (`HIST SNAPSHOT`) se stavy podle 8 a 9.3 (revize) | nový návrh | výpadek historie je vlastní stav, ne závada veličiny; nutné pro 9.3 | dvě položky navíc ve STATUS; texty OLED se schválí s M3.3; alternativa jen událost a web (nesplní 9.3) | SCHVÁLIT |
| R17 | benchmark v repozitáři | ponechat `tools/sqlite_history_benchmark.py`; test veřejných souborů prochází `tools/` | nový návrh | reprodukovatelnost a ověření SQL z dokumentace | další kód mimo aplikaci k údržbě; alternativa odstranit, výsledky jen v dokumentaci | SCHVÁLIT |
| R18 | `STRICT` tabulky | ano | nový návrh | typová kontrola v databázi (text do čísla se nedostane) | nástroje se SQLite < 3.37 schéma nepřečtou (Debian 13 má 3.46); alternativa bez `STRICT` s kontrolami `typeof` | SCHVÁLIT |

Hodnoty R1–R3 jsou od 2026-10-20 schválené pro lokální vývoj M3; R4 platí až po schválení opravy 7.1.

### 12.3 Podmínka zahájení M3 (ověřitelná)

M3 smí začít, když platí vše (splněno 2026-10-20 pro M3.1a: M2 schváleno a commitnuto, SQLite 3.45.1 se stavem WAL-reset UNKNOWN, `selfcheck` prošel, jen dočasné databáze testů):

1. M2 je schválené: každé z R1–R18 je schválené, nebo výslovně odložené (R11, R13 a R15 M3 nepoužije), a dokumentace M2 je commitnutá.
2. Na notebooku je zapsaná verze SQLite, kterou používá Python (`python3 -c "import sqlite3; print(sqlite3.sqlite_version)"`), a stav opravy WAL-reset podle 5.8; dnes 3.45.1, **UNKNOWN**.
3. `python3 tools/sqlite_history_benchmark.py --only selfcheck` na notebooku projde (SQL, modely pravidel, jeden zapisovač).
4. M3 pracuje jen s dočasnými a vývojovými databázemi bez hodnoty (testy, ruční zkouška mimo repozitář). Stav UNKNOWN je pro M3 přijatelný: návrh podmínky chyby vylučuje (5.8) a o taková data nejde.

### 12.4 Gate před provozním nasazením (M7, samostatné schválení)

Než NexusSTAT začne na Nexusu zapisovat produkční historii, musí platit vše:

1. Schválená read-only kontrola Nexusu zapíše: verzi SQLite v Pythonu služby, verzi a changelog balíčku `libsqlite3-0`, zdroj stavu synchronizace času a přítomnost baterie RTC (R8), velikost a obsazenost disku (R15), cesty podle platného `PROJECT-STANDARD.md` (R11) a úlohu Resticu (R13).
2. Oprava WAL-reset doložená podle 5.8 (a) nebo (c). Jinak buď SQLite s opravou ze schváleného zdroje (samostatné rozhodnutí správce; projekt nic nekompiluje ani nenahrazuje), nebo **výslovné přijetí rizika správcem** se stavem UNKNOWN, zapsané v `manifest.toml`.
3. `python3 tools/sqlite_history_benchmark.py --only selfcheck` projde na Raspberry Pi v dočasném adresáři (stejné chování spojení `mode=ro` na tamní verzi SQLite).
4. Za zkušebního běhu read-only ověřeno, že `history.sqlite3` má otevřený jen proces NexusSTAT a případný web jen pro čtení (např. výpis `/proc/<pid>/fd`).
5. Test obnovy podle 9.5 ze skutečného běhu Resticu, `restore_tested = true` v manifestu.
6. **Rozhodnutí o frekvenci ověřování synchronizace času** pro provoz: dnes se stav hodin zjišťuje jen při každém měření a `sync_fresh_seconds` (4.3) je tolerance mezery mezi pozorováními, ne nepřetržitý důkaz stavu NTP. Pro produkci se posoudí, zda zavést samostatný (častější nebo nezávislý) dotaz na zdroj synchronizace a s jakou periodou; architektura se kvůli tomu v M3 nemění a při nejistotě se retence jen odloží.
7. Ostatní podmínky M7 (převzetí funkcí Pironmanu po jedné, rollback) beze změny.

Schválení architektury M2 (2026-10-20) žádnou z těchto podmínek nesplňuje ani nenahrazuje.

## 13. Revize

### 13.1 Architektonické review 2026-10-20

Opravené chyby a rozpory v předchozí verzi návrhu:

1. **Agregace přepisovala:** `ON CONFLICT … DO UPDATE` by při opakování intervalu, z něhož retence smazala surová data, přepsal správnou agregaci neúplnou a konflikt by zamaskoval. Nyní `INSERT` bez `ON CONFLICT` a posun `done_until` s kontrolou výchozí hodnoty (6.2).
2. **Opakování dávky zdvojilo změny a události:** `ON CONFLICT DO NOTHING` chránil jen vzorky. Nyní `run.last_batch` a `batch-guard` (4.1, 5.2); `DO NOTHING` u vzorků zůstává jako druhá ochrana s rozlišením duplicity a konfliktu.
3. **Rozpracovaný snímek v zálohovaném adresáři:** dočasný soubor vznikal v `snapshot/`. Nyní v `history/` (9.1, 9.2).
4. **Záloha bez upozornění na chybějící historii:** chyběla kontrola aktuálnosti snímku. Nyní záznam `history-snapshot.json`, kontrola `history.snapshot` a porovnání v budoucím `service.backup` (9.3).
5. **Test obnovy porovnával s událostí, která ve snímku není.** Nyní se záznamem snímku a `meta.snapshot_utc` (9.5).
6. **Obnova po havárii:** obnovený snímek by se ohlásil jako běžné nečisté ukončení a na náhradním stroji by ho zapisovač odmítl. Nyní rozpoznání snímku a výslovné převzetí (9.6).
7. **Formulace retence:** 30 dní je nejkratší doba uchování, mazání je po dávkách s limitem a pozastavuje se po skoku času (7).
8. **Ztráta „≤ 60 s“ byla podaná jako garance:** nyní přesné podmínky a případy (5.5) a záznam skutečné ztráty (5.4).
9. **Měření před synchronizací času by se zahodila** a čas ve frontě nebyl popsaný: kvalita časové značky (4.3).
10. **Odhad `check_change` „< 10 MB“** neplatil při kmitání stavu (10.4, R4).
11. **Benchmark nepokrýval** dlouhodobý cyklus, souběžné čtení, dlouhou čtecí transakci, celkový zápis ani obnovu; doplněno (10.5, 10.6). Test veřejných souborů nezahrnoval `tools/`; doplněno.

### 13.2 Bezpečnostní revize 2026-10-20

1. **WAL-reset:** návrh neřešil známou chybu SQLite. Doplněn zjištěný stav (notebook i Debian 13 UNKNOWN), ověřitelné podmínky opravy, pravidlo jediného spojení pro zápis a checkpoint, zákaz zápisu z webu, kontrola v benchmarku a podmínky 12.3 a 12.4 (5.8).
2. **R4:** předchozí návrh (nejvýš jedna změna za minutu) mohl mezi zapsanými změnami ztratit `CRITICAL`. Nahrazen souhrny se zachovaným nejhorším stavem, počtem a hranicemi a se samostatným zápisem každého zhoršení nad dosavadní maximum souhrnu (sloupce `kind`, `until_ts`, `max_state`, `transitions`). Report to zobecnil na „každé zhoršení“, což neplatilo (13.3); souhrny pak finální R4 odstranila (13.4).
3. **Identita a fronta:** rozlišeny duplicita, kolize v sekundě a konflikt (4.1); konkrétní kapacita fronty (5.2); neznámá mezera po restartu během výpadku SQLite (5.4); přesné záruky místo „nejvýš 60 s“ (5.5).
4. **Čas:** kategorie času přesněji; přetečení fronty před synchronizací nevytvoří vymyšlený čas, ztráta nese jen monotónní rozsah (4.3).
5. **Snímek:** vzniká jen při synchronizovaném čase, stav `history.snapshot` se počítá z paměti (hlavní vlákno nečte disk), přehled potvrzení R12 (9.3).

### 13.3 Finální kontrola R4 2026-10-20

1. **Pravidlo z revize 2 neumělo doložit každý vstup do `CRITICAL`:** samostatně zapisovalo jen zhoršení nad dosavadní maximum souhrnu. Při střídání INFO → CRITICAL → INFO (180 změn) mělo vlastní řádek jen 4 z 90 vstupů do `CRITICAL`; zbylých 86 bylo jen v souhrnech s `max_state = CRITICAL` a se společným počtem 173 změn obou směrů. Tvrzení „každé zhoršení samostatně“ a „žádný `CRITICAL` ztracen“ byla zavádějící.
2. Navržená oprava se slučováním zlepšení nebyla přijata; správce zvolil úplnou historii všech změn (13.4).
3. **Struktura dokumentu:** sekce 7.1 byla vložená mezi text a SQL bloky retence; přesunuta za ně.

### 13.4 Finální R4 a požadavek na sledování kmitání 2026-10-20

1. **R4:** každá skutečná změna diagnostického stavu nebo provozně významného údaje (včetně linky `UP`/`DOWN` při stavu `INFO`) má vlastní řádek s přesným časem; nic se neslučuje. Ze schématu zmizely sloupce souhrnu (`kind`, `until_ts`, `max_state`, `transitions`) a dotaz `critical-entries`; přibyly dotazy `insert-change`, `state-episodes`, `details-episodes`, `transition-rate` a `transitions-window`.
2. **Kmitání:** schválený požadavek na budoucí detektor oddělený od diagnostického stavu, s prahy pro každou kontrolu a výstupy jen v `event` (7.2); v M2 jen datový kontrakt a jeho testy.
3. **Testy:** regresní scénáře R4 nad DDL (7.1), měření velikosti `check_change` při trvalém kmitání (10.7).

### 13.5 Cílené review M3.1b 2026-10-20

1. **Horní mez:** `batch-guard` zapisoval `last_write_utc` z nejnovějšího vzorku dávky ještě před zápisem. Vzorek, který databáze odmítla (jiná jednotka uložené řady, konflikt), tak posunul horní mez a po restartu by se platná měření do té doby odmítla jako `before_high_water`. Nyní `batch-guard` jen kontroluje číslo dávky a `run-last-write` před `COMMIT` zapíše jen čas skutečně vložených vzorků (5.2); schéma beze změny.
2. **Dávka a souhrny:** ručně vytvořená dávka se stejným číslem mohla předběhnout otevřenou dávku, která by se pak tiše zahodila jako „už zapsaná“; zapečetěná dávka změněná přímo v seznamech by se po nejistém commitu tiše zahodila; totéž měření odeslané znovu započetlo ztrátu dvakrát; počitadla potvrzeného pokusu se po nejistém commitu ztratila. Vše opraveno (11.1).
3. **Čas:** `history.unknown_gap` po běhu bez vzorku brala jako začátek mezery `started_utc` i při nedůvěryhodném startu a označila ji kvalitou nového startu; start, ukončení a události s časem před 2026-01-01 mohly nést `synced`. Opraveno. Monotónní rozsah ztrát před synchronizací je vědomě odložený otevřený požadavek M3.3 (11.1), doplnitelný bez změny schématu.
4. **Atomicita:** výjimka mimo SQLite (např. Ctrl+C) uprostřed transakce nechala transakci otevřenou se zámkem zápisu; nyní `ROLLBACK` při jakékoli výjimce.

### 13.6 Finální kontrola M3.2a 2026-10-21

1. **Podmínka 4** ověřena deterministickými testy (poslední vzorek uprostřed i na hranici okna, zastavené měření, obnova po hodině a po 3 dnech, restart během výpadku, skok hodin dopředu s daty i bez nich a návrat, prázdná, neznámá a čekající fronta, hodina bez nových dat). Nekonečná smyčka přes `more` ani trvalé zablokování nevzniká; poslední interval zůstává nedokončený do dalšího vzorku (6.1). Pravidlo ponecháno beze změny.
2. **Obsluha konfliktu:** selhání zápisu dokladu `history.aggregation_conflict` propustilo z `aggregate()` holou výjimku SQLite místo `HistoryError("conflict")` a selhání dotazu na existující okna holou výjimku místo `HistoryError("write")`. Data ohrožená nebyla (rollback, `done_until` i `SampleGate` beze změny, nic přepsáno). Opraveno; zpráva rozlišuje potvrzený a nepotvrzený doklad.
3. **Testy a mutace:** 13 nových testů (135 v modulu); kontrolní mutace agregace 26 z 27 odhaleno (zbylá je ekvivalentní, 11.2), mutace M3.1b 49 z 49.

### 13.7 Implementace M3.2b 2026-10-21 (ke schválení)

1. **Úprava schválených dotazů 7.1:** `details-episodes` by po retenci ohlásila u prvního zachovaného řádku běhu začátek epizody, který neznáme (domyšlený čas); `state-episodes` useknutou epizodu tiše vynechala. Obě nyní vracejí epizodu s neznámým začátkem jako `start_ts = NULL`; pro úplné rozsahy se výsledky (včetně benchmarku R4) nemění. Testy s původním zněním dotazů selžou. Při finální kontrole doplněno: `state-episodes` bere stav před prvním zachovaným řádkem z řádku libovolného typu (dřív se ztratila trvající epizoda, když po hranici zůstaly jen změny údajů) a nový dotaz `retention-unknown` přizná úseky, kde stav nebo údaje rekonstruovat nejde.
2. **Nový klíč `meta`:** `retained_since.<tabulka>` (trvalá hranice retence, nikdy neklesá); schéma v1 beze změny, ověření souboru dovoluje další klíče.
3. **Výklad hranic:** `agg_1h` dolů na celou hodinu a 10 kalendářních let (konzervativně, aby se nesmazala hodina zčásti mladší než 10 let).
4. **Čas:** `ClockState` s doloženou dobou stability ≥ 24 h a kontrolou uložených `clock.jump`; po startu procesu, který stabilitu doložit neumí, se 24 h nemaže (prodlouží jen uchování).
5. **Nalezeno při vývoji:** dotazy `oldest-*` ve tvaru `ORDER BY … LIMIT 1` nevracejí nad prázdnou tabulkou žádný řádek (opraveno čtení); pauza po skoku končila až sekundu po 24 h (opraveno na přesně 24 h); `more` hlásil práci i po přesném dokončení (opraveno).

### 13.8 Implementace M3.3a 2026-10-21 (ke schválení)

1. **Kapacita fronty změn (schváleno):** hodina trvalého kmitání 20 kontrol (`ceil(3600 / interval_s) × 20`, 7 200 po 10 s, ≈ 3,4 MB); fronta měření navíc s pevným stropem 72 000 hodnot (nejhorší případ obou front 55,7 MB).
2. **Nečekaná `IntegrityError` (schváleno):** `FAILED` a zásah správce; automatické rozdělení dávky odloženo.
3. **Úpravy `HistoryRecorder` bez změny datového modelu:** `HistoryError.sqlite_error` (rozlišení chyb), `add_results(..., track_changes=False)` a `add_change()` (změny se počítají při předání, 5.2), `rebind()` (znovuotevření téhož souboru ve stejném běhu).
4. **Záznam ztráty před synchronizací** čeká na synchronizaci (odvozený rozsah UTC), při ukončení bez ní se zapíše jen s monotónním rozsahem (4.3).
5. **Finální revize:** `state-episodes` při zahozených prostředních změnách natahovala epizodu až k další zachované změně (vymyšlená délka závady, doloženo testem na původním znění); nyní konec `NULL` a epizoda s neznámým začátkem, dotaz `changes-dropped` a `data.checks` přiznají i nespojitost, kterou řádky neukážou. `StopReport` rozlišuje jistě nezapsaná a nejistá data (`in_doubt`), stav nese počty potvrzených položek.
6. **Kontrola před uzavřením:** zahozený celý cyklus CRITICAL → INFO → CRITICAL nechá řádky navenek spojité; samotná `state-episodes` by epizodu CRITICAL vykázala jako nepřerušenou. Doplněn kontrakt čtení epizod s dotazem `episode-dropped-changes` (7.1) a test na přesný scénář. Fronta událostí má nově i strop velikosti jedné události (`MAX_EVENT_BYTES` 16 KiB, ztráta `event_too_large`), takže je paměť všech front omezená pro každý povolený interval 1–3600 s.

### 13.9 Implementace M3.3b-1 2026-10-22 (ke schválení)

- Historie je volitelná a ve výchozím stavu vypnutá; bez sekce `[history]` se chování nijak neliší od v0.6 (ověřeno testy smyčky i prázdnou databází po běhu bez napojení).
- Databáze se nikdy nevytváří ani nepřepisuje: ověřeno pro chybějící soubor (nevznikne ani `-wal`), cizí soubor (bajt po bajtu beze změny, zapisovač `FAILED`), zámek jiného zapisovače a cestu na síťovém úložišti (žádný řádek `run`).
- Zdroj synchronizace hodin je read-only a nikdy nelže: chyba dotazu, nedostupná libc i nevěrohodná odpověď dají `None`, ne `True`. Rozhoduje kernelová mez chyby hodin (shodné kritérium jako `timedatectl`), protože `STA_UNSYNC` nechává systemd-timesyncd trvale nastavený. Ztrátu synchronizace jádro přizná až po narůstu meze na 16 s (řádově hodiny) — uvedeno jako omezení v 4.3, nezastírá se.
- Zpětná vazba diagnostiky: kontrola `history.storage` se do historie nepředává a její `details` jsou ustálené, takže sama nevytváří nové změny k zápisu (regresní test s `ChangeTracker` i test smyčky).
- Strop textu výsledku (512 B UTF-8) vznikl z požadavku na omezenou paměť front; místo odmítnutí výsledku se zkracuje text, takže se nikdy neztratí identita kontroly, stav ani naměřená hodnota. Rozsáhlá změna datového modelu nebyla potřeba: `summary` a `details` mají dnes desítky bajtů a schéma v1 se nemění.
- Kontrolní mutace (24): vypnutý strop velikosti, zahození celého výsledku místo zkrácení, ztráta přednosti objektu řady, `True` při chybě dotazu na hodiny, ignorovaná mez chyby, chybějící timesyncd brané jako nesynchronizováno, výjimka zdroje času puštěná do smyčky, `FAILED` hlášené jako `INFO`, ignorovaná ztráta dat i plná fronta, `OK` u vypnuté historie, nejistý commit vydávaný za čisté ukončení, zamlčené zaseknuté vlákno, povolené síťové úložiště, chyba předání zastavující měření, opakované `stop()`, kontrola historie předaná do historie, druhý signál přerušující dopisování front, obsluha signálů mimo hlavní vlákno, vynechané ukončení historie po pádu smyčky, výchozí zapnutá historie, zapnutí bez cesty, relativní cesta a neznámý klíč v `[history]`. Všech 24 testy odhalí.
- Nezměněno: schéma v1, datový model, kolektory, OLED, alarmy, simulátor, Pironman. Žádná agregace ani retence se nespouští; na Nexusu se nic nemění.

### 13.10 Finální review M3.3b-1 2026-10-22 (nálezy a opravy)

Review se soustředilo na čas, ochranu úložiště, konzistenci živého měření a ukončení. Čtyři nálezy (reprodukované na předchozím znění regresními testy):

1. **Symlink obešel ochranu úložiště** (vážné): `filesystem_type()` porovnávala cestu jen textově, takže symlink v poslední části nebo v nadřazeném adresáři (a stejně tak `..` v cestě) ukazující do síťového připojení prošel jako lokální a historie by skončila na NAS. Opraveno rozvinutím cesty přes `os.path.realpath()` před hledáním bodu připojení; lokální symlink zůstává povolený.
2. **Neověřitelné úložiště se propouštělo**: nečitelná nebo neúplná tabulka připojení a cesta mimo všechny body připojení dřív znamenaly „neznámo, tedy povolit“. Nově se v takovém případě zápis odmítne (`HistoryError("path")`, stav `WARNING` a opakování s odstupem), v souladu s cílem „při neověřitelné identitě bezpečně odmítnout zápis“.
3. **`autofs` se bral jako lokální**: cíl se připojí až při prvním přístupu a bývá NFS nebo CIFS, takže se dopředu ověřit nedá; nově se odmítá stejně jako síťové úložiště.
4. **Záložní marker timesyncd rozhodoval o aktuálním stavu hodin** (bezpečnostní nález, doloženo čtyřmi kombinacemi): bez použitelné odpovědi `adjtimex` vracela existence souboru `synchronized` `True` (a jeho chybějící podoba při běžící službě `False`), takže dřívější synchronizace se vydávala za aktuální a historie mohla dostat čas, který nikdo nedokládá. Opraveno vyřazením markeru ze zdrojů rozhodnutí: o aktuálním stavu rozhoduje jen dotaz jádra, bez něj je stav `None` a data čekají ve frontě. Marker zůstal jako doplňující historická diagnostická informace ve výpisu (`timesyncd_first_sync()`, `first_sync_note()`). Platná odpověď jádra měla přednost už dřív a nezměnila se.
5. **Zaseknutý zápis nebyl v diagnostice vidět**: visící I/O nezmění stav zapisovače (zůstane `RUNNING`, žádná chyba nevznikne), takže `history.storage` hlásilo `INFO`, dokud se nezaplnila fronta. Nově je `WARNING`, když data čekají a poslední dávka se nepotvrdila déle než 180 s (3 × dávkovací interval).

Doplněno bez změny chování: doplňující informace o markeru v úvodním výpisu (nikdy netvrdí aktuální synchronizaci), druhá kontrola úložiště po otevření databáze (zužuje okno záměny, při změně uvolní spojení i zámek), pozdní vazba výchozích zdrojů v `clock_sync` (celý řetězec zdrojů jde ověřit v testech) a řízené ukončení i při signálu mimo měřicí smyčku (dřív by SIGTERM během spouštění historie skončil tracebackem).

Ověřeno jako správné bez zásahu: rozložení struktury `timex` odpovídá jádru na této platformě (čas z jádra se shoduje se skutečným časem procesu, zlomek sekundy respektuje `STA_NANO`); mez chyby nad 16 s je nevěrohodná odpověď, a proto `None`, ne `True`; vzorky patří ke svému měřicímu cyklu; `UNKNOWN` se ukládá jako `NULL` a skutečná nula jako nula; dvě rozhraní dávají dvě řady; opakované stejné měření je duplicita, ne ztráta; vypnutá historie neotevře ani spojení SQLite; jedno `stop()` na životní cyklus i při opakovaném signálu nebo vnoření.

Zůstává do M7 (Raspberry Pi): rozložení `struct timex` a dostupnost `adjtimex` na ARM64, chování bez RTC na Nexusu (bez použitelné odpovědi jádra se historie nezapíše, dokud se čas nedoloží), ověření cesty a práv úložiště v produkčním prostředí, záměna připojení mezi oběma kontrolami jako provozní riziko a Landlock (na cílovém systému není, takže se s ním nepočítá).

### 13.11 Implementace M3.3b-2 2026-10-22 (ke schválení)

- **Opravená chyba předčasného upozornění** (doloženo regresním testem na předchozím znění): `HistoryLink._stalled()` hlásilo zaseknutý zápis okamžitě při prvním měření, protože `last_commit_monotonic` je po startu `None`. Nově rozhoduje věk konkrétní nepotvrzené zapisovatelné práce (`WorkerStatus.unconfirmed_since`): nejstarší měření ve frontě, nebo začátek prvního pokusu o zápis dávky, který opakování ani znovuotevření spojení nevynuluje. Běžné čekání na minutovou dávku proto závada není, zaseknutý **první** COMMIT se naopak pozná, i když jsou fronty prázdné (data jsou v dávce), a čekání na synchronizaci hodin se hlásí zvlášť.
- **Oprava nalezená při napojení údržby:** společné ošetření chyb zápisu by při selhání agregace nebo retence přepnulo zapisovač do `DEGRADED` a odložilo zápis čekajících měření. Údržba má proto vlastní reakci (`_maintenance_failed`): přechodná chyba zápis nezastaví, nevratná ano. Bez toho by neúspěšná agregace zdržela sledování.
- Agregace nepředbíhá nepotvrzenou dávku ani nejistý commit a frontu hlásí jako prázdnou jen tehdy, když to lze doložit. Zapečetěná dávka po chybě (čeká na odstup) údržbu blokuje, i když je vlákno volné.
- Kontrolní mutace (17): 16 testy odhaleno. Neodhalená je **ekvivalentní**: kontrola `commit_in_doubt` je druhá vrstva k podmínce „žádná nepotvrzená dávka“, protože zapisovač obojí vyřeší jedním potvrzením; zůstává jako pojistka.
- Schéma v1 beze změny (`history.py` se nezměnil vůbec), jediné zapisovací vlákno, jediné zapisující spojení, výchozí `[history] enabled = false`, role `development`, ochrana úložiště, duplicity a nejistý commit z M3.3b-1 beze změny. Zbývající možnost záměny připojení mezi oběma kontrolami zůstává provozním rizikem k posouzení v M7; Landlock se nepoužívá (na cílovém Nexusu není).

### 13.12 Bezpečnostní review údržby 2026-10-22 (nálezy a opravy)

Review se soustředilo na průběžnost doložené stability času, evidenci `clock.jump`, hladovění zápisu a diagnostiku WAL. Jeden potvrzený nález (reprodukovaný na předchozím znění dvěma regresními testy):

1. **Doložená 24h stabilita vycházela z monotónního rozdílu, ne z pozorování** (vážné, doloženo): po jednom potvrzení při startu stačilo nechat běžet čas a `ClockState` hlásil `synced_for(86400)`, i když **nepřišlo žádné další měření** — zastavená nebo mrtvá měřicí smyčka tak mohla povolit nevratné mazání. Stejně tak dvě pozorování s 25hodinovou mezerou hlásila 49 h stability. Opraveno: dokládá se jen rozsah mezi prvním a posledním potvrzením jedné nepřerušené řady, mezera delší než `sync_fresh_seconds` řadu přeruší a nečerstvé poslední potvrzení dává neznámý stav (4.3, kontrakt a zdůvodnění čerstvosti tam).
2. **Doplněno k dokladu skoku hodin:** dokud není ztráta událostí zapsaná v databázi, je `ClockState` neznámý (ztracená událost mohla být `clock.jump`). Blokace trvá jen do zapsání dokladu ztráty — trvalá blokace po jedné ztracené události by retenci zastavila navždy a nechala soubor růst.
3. **Oprava velikosti WAL:** čte se nad rozvinutou cestou, protože při konfiguraci se symlinkem leží `-wal` u skutečného souboru; dřív by se hlásilo „nezjištěno“.

Ověřeno jako správné bez zásahu: zápis měření má přednost i při dohánění tří dnů s měřením každou sekundu a kmitáním dvou kontrol (žádná ztráta z přetečení, všech 60 měření potvrzeno); nová událost během dohánění se zapíše; `more` bez posunu hranice nevytvoří smyčku; zapečetěná dávka čekající na odstup i nejistý commit údržbu blokují; ukončení během dohánění, agregace i retence je čisté; surové vzorky nelze smazat bez dokončené 5min agregace a `agg_5m` bez hodinové; retence nemaže při neznámém `ClockState`, respektuje `clock.jump` v databázi za posledních 24 h a po restartu začíná ochranná lhůta znovu; zpoždění agregace zůstává `WARNING` (podezření), konflikt je `ERROR`, nevratná chyba není `RUNNING`; chybějící `-wal` není chyba a chyba čtení není nula; žádný `VACUUM`, `TRUNCATE` ani vynucený checkpoint. Schéma v1 a `history.py` zůstaly beze změny.

## 14. Zamítnuté varianty

- **Široká tabulka** (řádek = měření, sloupec = veličina): menší pro pevnou sadu, ale každá nová veličina nebo nový mount mění schéma a řídké řady (NAS, Restic) by nesly prázdné sloupce. Orientační pokus z 2026-10-19 ji měl o polovinu menší; při 20 řadách s různým intervalem a objekty to nevyváží.
- **Jedna tabulka pro čísla, texty i stavy:** míchá hodnotu po 10 s se změnami jednou za týden a hodnoty `NULL` by ztratily jednoznačný význam `UNKNOWN`.
- **Celá čísla v pevném měřítku** (např. tisíciny °C): o několik bajtů menší řádek, ale převody v každém dotazu a riziko chyby měřítka; nevyplatí se.
- **`ON CONFLICT … DO UPDATE` v agregacích:** tiché přepsání (13.1, bod 1).
- **Omezovat nebo slučovat přechody v historii** (všechny dřívější varianty R4: jedna změna za minutu, souhrny se zhoršeními, souhrny zlepšení): ztrácely nebo jen přibližně dokládaly přechody, časy vzniku a odeznění a délky epizod (13.2–13.4).
- **Zapisovat výsledky detektoru kmitání do `check_change` nebo měnit `state` kontroly:** detektor by počítal vlastní výstupy jako přechody a mohl by zakrýt `CRITICAL` (7.2).
- **Druhé zapisující nebo checkpointující spojení** (např. web zapisující potvrzení alarmu přímo, samostatný proces pro checkpoint): přesně podmínka chyby WAL-reset (5.8).
- **Posunout čas kolidujícího vzorku o sekundu:** zkreslil by čas měření.
- **Znovu otevírat agregovaná okna pro pozdní vzorky:** mění už zálohovaná data a komplikuje idempotenci.
- **Sloupec kvality času v `sample`:** 1 B na řádek pro vzácný případ; rozsah odvozených vzorků nese událost `clock.synced`.
- **InfluxDB, ORM, další služba:** v rozporu s požadavkem jednoduchosti a nezávislosti na Pironmanu.
- **Kopírování živého souboru, `-wal` a `-shm` Resticem:** není konzistentní záloha.
- **Ukládání monotónního času nebo `expires_monotonic`:** po restartu nemá význam; prošlost je pojem čtení v běžícím procesu, ne historie.
