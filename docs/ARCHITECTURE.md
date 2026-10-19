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

- Každá kontrola: `id`, `state`, `summary`, `observed_utc`, `expires_monotonic`, `simulated` a volitelně `measurement` (číselná hodnota + jednotka); později `source` a volitelně omezená diagnostická metadata.
- `summary` je jen text k zobrazení. Rozhodování (prahy, alarmy) používá výhradně `measurement`.
- Agregovaný výsledek musí odlišovat `UNKNOWN` od `OK`; do historie se zaznamenávají přechody, ne kopie každého měření.
- Dvě časové základny, nikdy se neporovnávají mezi sebou: monotónní čas (`time.monotonic()`) pro intervaly, timeouty a platnost v běžícím procesu; skutečný UTC čas (`observed_utc`) pro budoucí perzistentní historii a časové značky událostí. Monotónní čas nepřežije restart, proto se nesmí ukládat do historie. Při budoucím exportu do JSON je nutné platnost převést (např. na zbývající sekundy nebo UTC čas vypršení v okamžiku exportu).
- Aktuální JSON publikovat lokálně atomicky, pouze s odpovídajícími oprávněními a časem platnosti.

## Implementováno lokálně (fáze 1: simulace, read-only kolektor, pravidelné měření a první prahy)

Stav k 2026-10-19 (v0.4): jen lokální kód a testy na vývojovém počítači (x86_64, Linux Mint). Kolektor čte skutečná data notebooku, `alarms.py` je vyhodnocuje podle zapnutých prahů (výchozí vypnuto) a smyčka `run.py` je pravidelně vypisuje do terminálu; OLED je pouze obrazová simulace a se smyčkou zatím není propojen. Nic z toho nebylo spuštěno na Raspberry Pi ani proti skutečnému hardwaru displeje.

```text
run.run_loop  (monotónní plán po interval_seconds, výpis do terminálu)
      |
      v
system_monitor.SystemMonitor   (read-only: /proc/stat, /proc/meminfo,
  nebo fiktivní CheckResult      /sys/class/thermal, os.statvfs("/"))
                |  CheckResult: INFO + measurement, nebo UNKNOWN bez hodnoty
                v
alarms.AlarmEvaluator          (prahy [alarms]; mění jen state)
                |
                v
CheckResult  ->  core.active_problems / core.overall_state
                                   |
stisk / update(now)     ->  oled.OledController (SLEEP / CAROUSEL / STATUS)
                                   |
                            render.render_screen  ->  1bit obrázek 128 × 64 (Pillow)
```

- `core.py`: `CheckResult` má `id`, `state`, `summary`, `observed_utc` (UTC `datetime`), `expires_monotonic` (sekundy monotónních hodin) a povinný příznak `simulated`. Typy se kontrolují při vytvoření, takže záměnu časových základen odhalí hned. Pole `source` zatím chybí: existuje jediný kolektor a jeho výsledky jednoznačně určuje `id`. Výsledek s `now >= expires_monotonic` se vyhodnotí jako `UNKNOWN` s popisem „<id>: zastaralá data“, ať byl původně jakýkoli. Výjimkou je `N/A`, které nevyprší. `Measurement(value, unit)` je konečné číslo (ne bool, NaN ani nekonečno) s neprázdnou jednotkou; `UNKNOWN` a `N/A` hodnotu mít nesmějí a expirace ji smaže, takže prošlé číslo nemůže dál vypadat platně. Problémy jsou `CRITICAL`, `ERROR`, `WARNING` a `UNKNOWN`; `OK`, `INFO` a `N/A` problémem nejsou. Stejná závažnost se řadí podle `id`. `overall_state()` vrací nejzávažnější stav; bez dat nebo jen s `N/A` vrací `UNKNOWN`.
- `system_monitor.py`: jedna třída `SystemMonitor` a malé čisté funkce pro parsování (testovatelné na připravených textech). Výsledky `system.cpu_usage`, `system.cpu_temp`, `system.memory`, `disk.root`. Platnost od měření dodává volající z `[collector]` (3 × interval, výchozí 30 s); kolektor vlastní výchozí hodnotu nemá, aby se nemohla rozejít s intervalem spouštění.
  - CPU: ze dvou vzorků `/proc/stat`; první vzorek je `UNKNOWN` („první vzorek“). Nečinnost = `idle + iowait`, `guest` se nesčítá dvakrát, nulový nebo záporný rozdíl čítačů je `UNKNOWN`.
  - RAM: `(MemTotal − MemAvailable) / MemTotal`; chybějící, nulové nebo nesmyslné hodnoty jsou `UNKNOWN`.
  - Teplota: zóna vybraná podle typu (`cpu-thermal`, `cpu_thermal`, `soc_thermal`, `x86_pkg_temp`), ne automaticky `thermal_zone0`. Bez vhodného senzoru `UNKNOWN`, nikdy 0 °C. Hodnoty mimo (0, 125] °C jsou neplatné; jde jen o prozatímní kontrolu věrohodnosti, ne hardwarový limit ani alarmový práh.
  - Disk: výhradně `os.statvfs("/")`, výpočet jako `df` (rezervované bloky nejsou volné). `/mnt`, NAS ani jiné mounty se neprocházejí.
  - Každá kontrola má vlastní ošetření chyb (`OSError` → „nedostupné“, `ValueError`/`ArithmeticError` → „neplatná data“), selhání jedné neshodí ostatní.
  - Vytížení CPU je průměr za dobu od předchozího volání `collect()`; jeho vypovídací hodnotu tedy určuje interval volání.
  - Kolektor prahy nevyhodnocuje: platná hodnota má stav `INFO` a `measurement` (nezaokrouhlené číslo: CPU %, teplota °C, RAM % obsazení, disk % obsazení jako `df`). Text `summary` je stejný jako dřív a zaokrouhlený na celé jednotky. Při chybě je `measurement` `None` a stav `UNKNOWN`.
  - `simulated=False` jen při čtení skutečného `/proc`, `/sys/class/thermal` a `os.statvfs`; jakýkoli náhradní zdroj (testy) dává `simulated=True`.
- `run.py`: jediná hlavní smyčka `run_loop()` v jednom vlákně. Hodiny, `sleep`, kolektor i výstup jsou parametry, takže testy běží se simulovaným časem.
  - Plánuje v monotónním čase (`time.monotonic()`); `observed_utc` bere z UTC hodin v okamžiku měření. Skok systémového času plán neposune.
  - První měření hned po startu. Další termín = předchozí *plánovaný* termín + interval, ne konec měření, takže doba měření ani nepřesnost uspání se nesčítají a plán postupně neujíždí.
  - Je-li měření o celý interval nebo víc pozdě (pozastavený či zaseknutý proces), proběhne jediné měření, vypíše se „Zpoždění … s“ a interval se počítá znovu od něj. Zmeškané termíny se nedohánějí. Při menším zpoždění zůstává původní plán, takže další měření může přijít dřív než za celý interval.
  - Mezi měřeními proces spí (`time.sleep`) až do plánovaného termínu; žádné aktivní čekání.
  - Výjimka z `collect()` (kromě Ctrl+C) nahradí výsledky všech čtyř kontrol stavem `UNKNOWN` („měření selhalo“) s platností od tohoto měření. Předchozí, ještě platné hodnoty se nepodrží, aby nevypadaly jako čerstvé (stejně jako při selhání jedné kontroly v kolektoru). Smyčka běží dál.
  - Platnost 3 × interval: běžné zpoždění ani jeden vynechaný termín nezpůsobí `UNKNOWN` a hodnoty mezi měřeními nebliknou na `N/A`. Když měření přestanou přicházet, nejpozději po třech intervalech zestárnou.
  - Ctrl+C (`KeyboardInterrupt`) ve spánku i během měření ukončí smyčku bez tracebacku s kódem 0. Neplatná konfigurace ukončí program s kódem 2 ještě před prvním měřením.
  - Po každém měření (i po selhání, aby `UNKNOWN` přerušilo řadu) projdou výsledky `AlarmEvaluator`em.
  - Výpis: úvodní řádek s popisem prahů, pak jeden řádek na měření (UTC čas, `SIM` u simulovaných dat, celkový stav, popisy kontrol; hodnota ve stavu WARNING/ERROR má za sebou `[WARNING]`/`[ERROR]`) přes `print`; vlastní logování ani soubory nejsou.
  - Omezení: `CLOCK_MONOTONIC` v Linuxu neběží během uspání systému, takže výsledky změřené před uspáním notebooku mohou po probuzení ještě až do dalšího plánovaného měření (nejvýše jeden interval) vypadat platně (Raspberry Pi jako server se neuspává). Kolektor nemá timeout: zaseknuté čtení zablokuje celou jednovláknovou smyčku.
- `alarms.py`: `AlarmEvaluator` vyhodnocuje zapnutá pravidla pro `system.cpu_temp` (°C) a `disk.root` (%). Pracuje jen s `measurement`, nikdy s textem, a mění jen `state`; popis, hodnota, časy i `simulated` zůstávají z kolektoru.
  - Hodnota rovná prahu je nad prahem (`>=`). Úroveň měření: `ERROR` od `error`, `WARNING` od `warning`, jinak `INFO` (normální stav zůstává `INFO`, ne `OK`).
  - Hystereze drží potvrzenou úroveň: `ERROR` trvá, dokud hodnota neklesne pod `error − hysteresis`, `WARNING` (i z `ERROR`) pod `warning − hysteresis`. Hodnota rovná `práh − hysteresis` závadu ještě drží. Tyto hranice (`AlarmRule.warning_hold`, `error_hold`) se zaokrouhlují na 6 desetinných míst, aby desetinné prahy platily přesně jako v konfiguraci (65.2 − 1.1 = 64.1, ne 64.10000000000001); stejnou hranici používá i validace.
  - Zhoršení vyžaduje `confirm_count` (výchozí 3) po sobě jdoucích měření alespoň na nové úrovni, zlepšení `clear_count` (výchozí 2) měření nejvýš na nové úrovni. Měření nad `error` se počítá i pro `WARNING`, takže při postupném růstu přijde `WARNING` dřív než `ERROR`; skok rovnou nad `error` potvrdí po třech měřeních přímo `ERROR` (bez zvláštní rychlé cesty a bez automatické akce). Jednorázové výkyvy nahoru ani dolů stav nemění.
  - `UNKNOWN` (selhání měření, chybějící hodnota nebo jiná jednotka u zapnutého pravidla) přeruší řadu po sobě jdoucích měření, ale potvrzenou závadu nevyřeší: po návratu dat platí dál, dokud ji neukončí `clear_count` platných měření. Stejně se počítá mezera, kdy předchozí výsledek mezi měřeními prošel. `N/A` a výsledky bez zapnutého pravidla se nemění.
  - Simulovaná a skutečná data mají oddělenou paměť, takže simulace nemůže potvrdit ani ukončit skutečný alarm. Simulovaný alarm nese `simulated=True` a na displeji i ve výpisu značku `SIM`.
  - Identita vzorku: (`id`, `simulated`, `expires_monotonic`). Platnost = monotónní čas měření + pevná platnost z `[collector]`, takže každé měření jedné veličiny má v běžícím procesu jinou, rostoucí hodnotu. Vzorek, který není novější než poslední vyhodnocený (i `UNKNOWN`), se nezapočítá znovu a dostane aktuální potvrzený stav: opakované vyhodnocení, např. při obnově displeje, je idempotentní a starší vzorek nic nezmění. `now` v `evaluate()` je čas vzniku výsledků a slouží jen k poznání mezery v datech.
  - Zaokrouhlení pro zobrazení: o stavu rozhoduje nezaokrouhlená `measurement.value`. OLED i terminál ukazují text `summary` zaokrouhlený na celé °C/% (Python `:.0f`, polovina k sudé: 69,5 → 70, 70,5 → 70, 64,5 → 64). Zobrazená hodnota se od vyhodnocené liší nejvýš o 0,5 jednotky. Text „70 °C“ proto při prahu 70 může znamenat `INFO` (69,5–69,99) i hodnotu nad prahem (70–70,5) a „65 °C“ při hranici odeznění 65 může být pod ní (nad 64,5 až 64,99). Rozhodující je zobrazený stav (`[WARNING]`, STATUS, trojúhelník), ne zaokrouhlené číslo.
  - Změna pravidel = nový evaluator: potvrzené i rozpracované stavy začnou znovu.
- `oled.py`: čistá stavová logika bez kreslení. Kolotoč má čtyři stránky `NEXUS`, `SYSTEM`, `DISK`, `SERVICES` (na displeji SLUŽBY); časování (10 s na stránku, okno 300 s, nečinnost STATUS 30 s) se počítá z počtu stránek, takže čtvrtá stránka okno neprodlužuje a po ní následuje znovu NEXUS. `service_rows()` sestaví řádky SLUŽBY z výsledků podle id z `services.py`: text stavu `OK/INFO/WARN/ERR/CRIT/UNKN/N/A`; prošlý výsledek je `UNKN`, nikdy `OK`. OLED výstupy dostávají výsledky doplněné `services.complete_service_results()`, takže sledovaná služba bez dat je `UNKNOWN` stejně na SLUŽBY, v symbolu `?` na NEXUS i ve STATUS (společné vyhodnocení přes `effective()`); potvrzené WARNING/ERROR mají přednost. Kdyby doplnění chybělo, `service_rows()` chybějící službu i tak ukáže jako `UNKN`. Čas dostává jako parametr `now` (v provozu `time.monotonic()`, v testech libovolná čísla). Stránka kolotoče se počítá z času od začátku okna, takže automatické přepínání okno neprodlužuje. Při návratu ze STATUS po nečinnosti začíná nové okno okamžikem vypršení, nikoli až pozdním voláním `update()`. Stisk nejdřív uplatní časové limity, takže stisk po uplynutí okna displej probudí a neotevře STATUS. `home_symbol()` určuje symbol stránky NEXUS: trojúhelník (`WARNING/ERROR/CRITICAL`) má přednost před `?` (`UNKNOWN`, žádná data nebo jen `N/A`). `page_lines()` sestaví řádky stránek SYSTEM a DISK z výsledků podle id; chybějící, prošlá nebo neměřitelná hodnota se ukáže jako text „<popisek> N/A“, diagnostický stav přitom zůstává `UNKNOWN` (nejde o `State.NA`).
- `render.py`: jen kreslí podle stavu automatu. STATUS má 4 řádky na stránku. SLUŽBY: nadpis 12 px tučně, čára na y = 15, 4 řádky písmem 11 px od y = 14 po 12 px (inkoust 17–62 px), název vlevo, stav zarovnaný vpravo podle skutečného inkoustu (sloupec 127 zůstává volný); rozvržení ověřuje test pro všechny kombinace názvů a stavů. Zkracování textu (`fit_text`) i zarovnání vpravo měří skutečně vykreslené znaky (`textbbox`), ne šířku pro posun, která bývá o zlomek pixelu menší. Bez sledovaných dat (žádné výsledky nebo jen `N/A`) zobrazí `NO MONITOR DATA`, při platných datech bez závad `NO ACTIVE ISSUES` (ne „vše OK“, protože hodnoty `INFO` nejsou vyhodnocené prahy). Obrazovky se simulovanými výsledky nesou značku `SIM`; skutečná měření ji nemají.
- `services.py`: katalog služeb pro stránku SLUŽBY (id `service.nexus`, `service.animedb`, `service.nas`, `service.backup` a krátké názvy). Nesbírá žádná data. `complete_service_results()` doplní služby z katalogu, pro které nepřišel výsledek: nenasazená AnimeDB `N/A` („AnimeDB není nasazena“), ostatní `UNKNOWN` („<název>: bez dat“), vše `simulated=False` a bez číselné hodnoty; výsledky, které přišly (i simulované), nemění. Rozdíl: `UNKNOWN` = sledované, ale aktuální stav neznáme (problém ve STATUS, symbol `?`); `N/A` = záměrně vypnuté nebo nenasazené (žádný alarm, nevyprší). NEXUS zatím nemá definovaný healthcheck, proto je `UNKNOWN`, nikdy `OK`. Terminálová smyčka `run.py` služby zatím nevypisuje (jen systémová měření a prahy). Budoucí kolektory (služby Nexusu, AnimeDB, NAS, Restic) budou vracet výsledky se stejnými id.
- `simulate.py`: fiktivní data scénáře odpovídají ukázce SLUŽBY (NEXUS, ANIMEDB, NAS `OK`, BACKUP `WARN`); dřívější fiktivní problémy NAS (CRITICAL), zálohy (ERROR) a N/A AnimeDB byly nahrazeny jinými fiktivními položkami, aby si obrazovky neodporovaly. Scénář projde všechny čtyři stránky, vrátí se na NEXUS a dál ukazuje dvě stránky STATUS. `--live` doplní k měření služby přes `complete_service_results()`.
- `config.py`: sekce `[oled]` a `[collector]` z TOML (`tomllib`) přes `load_config()`; validuje se vždy celý soubor, i sekce, kterou volající nepotřebuje. Časy jsou celé sekundy 1–3600, `page_seconds ≤ carousel_seconds`, neznámé klíče, jiné sekce a jiný rozměr než 128 × 64 jsou chyba. `[collector] interval_seconds` (výchozí 10 s); platnost výsledků `validity_seconds` = 3 × interval se samostatně nenastavuje, aby nemohla být kratší než interval. `[alarms.cpu_temp]` a `[alarms.disk_root]` (`AlarmRule`): `enabled` (výchozí `false`), `warning`, `error`, `hysteresis` bez výchozích hodnot (při zapnutí povinné, zadávají se všechny tři najednou), `confirm_count` 3 a `clear_count` 2 (celá čísla 1–60). Validace: konečná čísla, `warning < error`, `0 ≤ hysteresis < error − warning`, prahy v rozsahu teploty (0, 125] °C a disku (0, 100] %, `warning − hysteresis` nad dolní mezí (jinak by závada nikdy neodezněla). Neimplementované volby (`home_title`, `clock_24h`, `[alarms] show_warning_triangle`, `[web]`, `[storage]`) jsou v ukázkové konfiguraci jen jako komentář a při zadání vyvolají chybu.
- Hodiny a datum na stránce NEXUS používají místní čas předaný zvlášť (`wall_time`); monotónní čas slouží jen časovačům a platnosti.

### Příklady přechodů stavů

Ilustrační prahy teploty (`warning = 70`, `error = 80`, `hysteresis = 5`, výchozí počty 3 a 2), měření po 10 s. Nejde o schválené hodnoty; stejné posloupnosti ověřuje `tests/test_alarms.py`.

```text
°C     65   72   72   72   85   85   85   60   60
stav   INFO INFO INFO WARN WARN WARN ERR  ERR  INFO

°C     65   71   72   73   79   81   82   83   77   74   72   66   64   63
stav   INFO INFO INFO WARN WARN WARN WARN ERR  ERR  ERR  WARN WARN WARN INFO
```

V druhé řadě drží 77 °C stav ERROR díky hysterezi (nad 75), 74 a 72 jej po dvou měřeních sníží na WARNING, 66 drží WARNING (nad 65) a 64 a 63 závadu ukončí.

### Budoucí nastavení prahů přes web (návrh)

Prahy jsou data (`AlarmsConfig`/`AlarmRule`), ne kód. Budoucí LAN web jen připraví nové hodnoty, nechá je ověřit stejnou validací (`alarms_config_from_dict`), uloží je (konfigurační soubor nebo budoucí DB) a běžící proces si vytvoří nový `AlarmEvaluator`. Diagnostická logika v `alarms.py` se tím nemění. Otevřené: zda při změně pravidla zachovat paměť nezměněných veličin, auditní záznam změn, oprávnění a ochrana CSRF.

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
