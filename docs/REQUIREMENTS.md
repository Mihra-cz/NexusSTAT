# NexusSTAT – požadavky v0.9

**Dokument je specifikací návrhu, nikoliv popisem hotového softwaru.** Požadavky vycházejí z dosavadního rozhodnutí správce projektu.

## Účel

Lokální diagnostika a stavová signalizace Nexusu (systém, NVMe, NAS, Restic, Docker, služby, později AnimeDB). **Cílem je nahradit aplikační software Pironman 5 vlastním NexusSTAT**, ne ho trvale provozovat vedle Pironmanu; hardwarové ochranné funkce a chlazení přitom nesmí utrpět (viz Náhrada Pironmanu 5). Žádná povinná cloudová závislost.

## Náhrada Pironmanu 5 (schválený cíl, zatím neimplementováno)

- NexusSTAT postupně převezme aplikační funkce Pironman 5: systémový monitoring, obrazovky OLED, signalizaci RGB, obsluhu dostupných funkcí fyzického tlačítka, monitoring ventilátorů, historii naměřených hodnot a lokální webové rozhraní.
- Dnes existuje jen lokální vývoj na notebooku (offline simulace OLED, read-only kolektory systému, sítě a CPU FAN, lokální SQLite historie). Na Nexusu nic neběží a Pironman dál plní všechny své funkce.
- Hardwarové ochranné mechanismy Raspberry Pi (firmware, teplotní omezování výkonu, nouzové vypnutí při přehřátí) se neodstraňují ani neobcházejí.
- Hlavní CPU ventilátor dál bezpečně řídí Linux (kernel/firmware); NexusSTAT ho jen čte.
- Oba boční ventilátory Pironmanu musí mít před vypnutím původního softwaru bezpečně ověřené nezávislé řízení (fyzická kontrola, teplotní dohled, rollback).
- Pironman se nevypne, dokud není ověřené předání všech potřebných funkcí. Převzetí probíhá po jednotlivých funkcích, ne najednou.
- Návrat k původnímu softwaru musí zůstat možný: při přechodu se Pironman nejdřív jen zastaví a zakáže, neodinstaluje se, a jeho konfigurace se předem zálohuje.

## Historie a databáze SQLite (schválený směr; implementováno lokálně v M3, nenasazeno)

- Hlavní databází NexusSTAT pro historii měření, alarmů a událostí bude **SQLite**. NexusSTAT nesmí vyžadovat InfluxDB Pironmanu ani na ní stavět.
- Po úspěšném nahrazení Pironmanu nebude jeho ukládání historie potřeba. Vypnutí příslušné instance InfluxDB je cíl, ale až po ověření, že ji nepoužívá žádná jiná aplikace, a po schválení.
- Druhy dat:
  1. **Aktuální měření**: rychlé hodnoty pro OLED, alarmy a web, v paměti procesu s časem platnosti; základní interval měření 10 s. Nečtou se z databáze a výpadek databáze je neovlivní.
  2. **Historie měření**: časové řady vytížení CPU, teploty CPU, RAM, stavu a případně otáček CPU ventilátoru, obsazenosti disků, síťového provozu RX/TX a dalších systémových měření.
  3. **Historie alarmů**: vznik alarmu, změna závažnosti, odeznění, ztráta měření (`UNKNOWN`), budoucí potvrzení nebo ztišení uživatelem.
  4. **Systémové události**: start a ukončení NexusSTAT, výpadek kolektoru, změna konfigurace, významné změny sledovaných služeb.
- Alarmy a události se ukládají jako přechody; nezměněný stav se každých 10 s znovu neukládá. Chybějící měření se ukládá jako mezera, nikdy jako nula. Simulovaná data se do provozní historie nezapisují.
- Zadání M2 (2026-10-20): historie rozlišuje skutečnou číselnou nulu, `UNKNOWN`, chybějící vzorek a `N/A`; textové a stavové údaje nemusí být v tabulce číselných měření. Časové značky jsou skutečné UTC, monotónní čas slouží jen běžícímu procesu a do trvalé historie nepatří. Návrh se chrání proti duplicitním vzorkům, restartu procesu, skoku systémového času, výpadku nebo opoždění kolektoru a opakovanému zpracování uloženého intervalu. Agregace zachovávají počet platných vzorků, minimum, maximum a průměr a chybějící vzorky nepočítají jako nuly. Výpadek SQLite nezastaví měření, alarmovou logiku ani obsluhu OLED.
- Retence, **výchozí návrh, ne implementovaný kontrakt**: 30 dní původní měření po 10 s, 1 rok agregace po 1 nebo 5 minutách, dlouhodobě hodinové nebo denní agregace; důležité alarmy a systémové události mají samostatnou retenční politiku. Schváleno 2026-10-20 (R1–R4): nejméně 30 dní surových dat, 1 rok 5min agregací, hodinové 10 let, změny stavů a události 2 roky. Velikost databáze, počet veličin, frekvence zápisů a nároky na SSD jsou změřené v návrhu M2 (`HISTORY.md`, sekce 10); ověření na Raspberry Pi 5 zbývá.
- Změny stavu (R4, schváleno 2026-10-20): v historii se zachová každá skutečná změna diagnostického stavu i provozně významného údaje (zhoršení, zlepšení, úplné odeznění, přechod do `UNKNOWN` a návrat, změna linky `UP`/`DOWN` i při klasifikaci `INFO`) s přesným časem; opakované měření beze změny nevytváří událost; slučovat lze jen nepodstatné údaje, které nemění stav ani provozně významnou informaci; žádné tiché přepsání ani duplicita po nejistém commitu; omezení notifikací historii neovlivní.
- Kmitání (schválený požadavek 2026-10-20, detektor zatím neimplementován): z historie musí jít dopočítat počet přechodů, četnost a délky epizod; budoucí detektor FLAPPING pracuje odděleně od diagnostického stavu a nepřepisuje ho (`CRITICAL` zůstává `CRITICAL`), má prahy pro každou kontrolu zvlášť, počítá i provozní změny typu `UP`/`DOWN`, jeho vlastní události FLAPPING START a END se nepočítají jako přechody a web má zobrazit četnost a vývoj kmitání. Okno 10 min, vznik při 6 přechodech a konec po 20 min stability jsou jen příklad k budoucímu schválení.
- **Návrh naplnění (M2, schválený 2026-10-20 kromě R11, R13, R15 (odloženo do M7); implementováno lokálně v M3, nasazení neschváleno):** `HISTORY.md` – schéma a DDL, čas a identita měření, zápis po dávkách, agregace, retence, odolnost, záloha a obnova, benchmark, plán M3 a seznam rozhodnutí ke schválení.
- Jednoduchost: modul `sqlite3` ze standardní knihovny, bez ORM, databázového frameworku a externí databázové služby.
- Umístění: lokální SSD, adresář `/srv/nexus/appdata/nexusstat/` podle standardu Nexus; název souboru se určí s návrhem schématu. SQLite se nikdy neprovozuje přímo na NAS/CIFS.
- Zvážit režim WAL pro současné čtení (web) a zápis.
- Zálohování: konzistentní kopie živé databáze přes SQLite Backup API, napojená na existující Restic, s ověřenou obnovou. **Samotné kopírování aktivního souboru SQLite (ani spolu se soubory `-wal` a `-shm`) nemusí být konzistentní záloha.**
- Retenční pravidla a omezení růstu databáze; ošetření chyb zápisu a nedostatku místa.
- Chyba ukládání historie není chyba monitoringu: aktuální měření, alarmy, OLED a web běží dál a výpadek historie se hlásí jako vlastní stav, ne jako závada měřené veličiny.
- Dokumentace SQLite: [WAL](https://www.sqlite.org/wal.html), [Backup API](https://www.sqlite.org/backup.html).

## Stavové kategorie

- `OK`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`, `UNKNOWN`.
- `N/A` znamená záměrně neaktivní nebo nepoužívanou integraci (např. AnimeDB před nasazením), není alarm. N/A **nevyprší** uplynutím času; přechod z N/A na aktivní sledování musí být explicitní (nový výsledek), nikoli důsledek timeoutu.
- `UNKNOWN` nesmí být vydáváno za `OK`; čas platnosti každé kontroly je explicitní.
- Po vypršení platnosti je stav `UNKNOWN` a popis uvádí zastaralá data. Dřívější WARNING/ERROR/CRITICAL se bez platných dat nepovažuje za potvrzenou trvající závadu (ani za vyřešenou).
- Žádné výsledky nebo pouze `N/A` znamenají „žádná data ze sledování“, nikoli „vše v pořádku“.
- Simulovaná data musí být v datech i na výstupu odlišitelná od skutečného měření.
- Potvrzení/ztišení nemění skutečný stav chyby.
- Pořadí STATUS: CRITICAL, ERROR, WARNING, UNKNOWN. Při stejném stupni stabilní deterministické řazení.

## OLED 128 × 64 (schválený směr UX)

1. Displej nejprve spí. První **krátký stisk** zobrazí první stránku s nápisem `NEXUS`, hodinami (24h) a datem.
2. Režim **CAROUSEL** automaticky mění stránky v pořadí **NEXUS → SYSTEM → DISK → NETWORK → SERVICES** (od v0.6, dříve bez NETWORK) a pak znovu NEXUS, každých **10 s**; celkově je aktivní **300 s** a následně OLED usne. Výchozí hodnoty jsou konfigurovatelné, později změnitelné přes LAN web.
3. Každý další krátký stisk během kolotoče otevře **STATUS**, což je přehled konkrétních závad pod sebou.
4. **STATUS bez závad nebo s jednou stránkou:** další krátký stisk vrací do CAROUSEL.
5. **STATUS s více stránkami:** další stisky listují po stránkách; ze **závěrečné stránky** další krátký stisk vrací do CAROUSEL (bez cyklení uvnitř STATUS).
6. Ve STATUS se po **30 s bez stisku** automaticky vrátíme do CAROUSEL; každý platný stisk resetuje jeho 30s časovač.
7. Každý návrat do CAROUSEL zahájí nové 300s okno od první stránky NEXUS. Automatické přepnutí stránky dobu neprodlužuje.
8. Aktivní WARNING/ERROR/CRITICAL signalizuje na první stránce malý obrysový trojúhelník s vykřičníkem; potvrzení alarmu jej neschová, dokud závada trvá. Pouze UNKNOWN bez potvrzené závady (včetně prošlých výsledků, žádných dat nebo jen N/A) signalizuje symbol `?`. Trojúhelník má přednost před `?`. Vše platně OK/INFO/N/A: bez symbolu.
9. OLED v běžném stavu není nuceně rozsvěcen pouze kvůli upozornění. Ovládání a uspání musí zůstat funkční i při chybách.
10. Vše nejprve testovat jako monochromatický bitmapový simulátor 128 × 64 bez hardware.
11. STATUS bez sledovaných dat zobrazí `NO MONITOR DATA`, bez aktivních problémů při platných datech `NO ACTIVE ISSUES`. Obrazovky se simulovanými daty nesou značku `SIM`.
12. Stránky SYSTEM a DISK ukazují hodnoty ze systémového kolektoru: anglický název vlevo, hodnota s jednotkou vpravo (`CPU 23%`, `CPU TEMP 48°C`, `RAM 24%`, `ROOT 42%`). Chybějící, prošlá nebo neměřitelná hodnota se zobrazí jako `UNKN` (diagnostický stav `UNKNOWN`), nikdy jako stará hodnota ani jako `N/A`, které je vyhrazené záměrně vypnuté nebo nenasazené integraci (změna v0.5, dříve text `N/A`). Bez zapnutých prahů (`[alarms]`, výchozí vypnuto) se z hodnot neodvozuje WARNING ani ERROR.
13. Stránka **SERVICES** má řádky NEXUS, ANIMEDB, NAS a BACKUP: název vlevo, stav vpravo (`OK`, `INFO`, `WARN`, `ERR`, `CRIT`, `UNKN`, `N/A`). Hodnoty dodávají kolektory; dnes jen volitelný healthcheck AnimeDB (SERVICES-1, viz Healthcheck AnimeDB), ostatní služby kolektor nemají. Ve skutečném režimu se nevykonaná kontrola nikdy neukáže jako `OK`: sledovaná služba bez výsledku nebo s prošlým výsledkem je `UNKNOWN` konzistentně na stránce SERVICES (`UNKN`), v symbolu `?` na stránce NEXUS i ve STATUS. `UNKNOWN` není potvrzená závada; potvrzené WARNING/ERROR mají přednost (trojúhelník, pořadí ve STATUS). AnimeDB bez zapnutého sledování je `N/A`: není alarm ani neznámý stav; se zapnutým healthcheckem nikdy `N/A`. Položka NEXUS netvrdí `OK`, dokud nebude mít jednoznačně definovaný healthcheck s kolektorem. Fiktivní stavy simulátoru nesou značku `SIM`. Konkrétní závady dál ukazuje samostatný STATUS se stávajícím ovládáním.
14. Typografie (v0.5): jednotné ostré bitmapové písmo přiložené v projektu (nezávislé na písmech systému, s doloženou licencí), 1 bit na pixel bez vyhlazování. Stránka NEXUS má nápis, hodiny a datum vodorovně na středu podle skutečně vykreslených pixelů (tolerance 1 px), hodiny jsou dominantní; symbol vpravo nahoře ani značka `SIM` vystředění neovlivní a nepřekrývají se s textem.
15. Jazyk OLED (v0.5, doplněno v0.6): všechny texty na displeji (i v jeho simulovaném obrazu) jsou anglicky, velkými písmeny ASCII bez diakritiky; ze symbolů jen `%`, `°C`, `/`, `:`, `.`, `_`, `-` a `…` u zkrácení. Jedinou výjimkou s malým písmenem jsou výslovně zadané jednotky rychlosti `B/s`, `KB/s` a `MB/s` (v0.6). Nadpisy NEXUS, SYSTEM, DISK, NETWORK, SERVICES, STATUS; názvy CPU, CPU TEMP, RAM, ROOT, LINK, NET RX, NET TX, IPV4, NEXUS, ANIMEDB, NAS, BACKUP; stavy OK, INFO, WARN, ERR, CRIT, UNKN, N/A. Texty sestavuje samostatná prezentační vrstva výhradně z `id`, stavu a `measurement`, nikdy z textu `summary`; diagnostika, prahy ani id se kvůli zobrazení nemění. Česky zůstávají `summary`, terminál, dokumentace a ovládání okna simulátoru.
16. STATUS (v0.5): stav vlevo (CRIT/ERR/WARN/UNKN), za mezerou krátký popis nejvýš na 15 znaků bez výpustky, např. `ERR  CPU TEMP 88°C`, `CRIT NAS OFFLINE`, `UNKN NAS NO DATA`. Potvrzený alarm ukazuje skutečně naměřenou hodnotu. `NO DATA` = platná hodnota chybí (nezměřeno, selhání, bez kolektoru), `STALE` = výsledek prošel. Hláška závady bez hodnoty jen u kontrol s jednoznačným významem id (`NAS OFFLINE` = prokázaná nedostupnost, `BACKUP OVERDUE` = stará záloha) nebo s důvodem od healthchecku AnimeDB (`ANIMEDB DOWN`, `ANIMEDB RECOVER`, u `UNKNOWN` `ANIMEDB FAILING` a `ANIMEDB INVALID`); jinak jen název. Když se celý popis nevejde, zůstane název kontroly; výpustka až u názvu delšího než řádek. Neznámé id: obecný název `CHECK` a deterministický dohledatelný kód (první 4 znaky SHA-256 id, např. `CHECK 0D51`), samotné id se nezobrazuje; závažnost zůstává ve sloupci stavu.

17. Stránka **NETWORK** (v0.6, implementováno lokálně, nenasazeno): čtyři řádky, název vlevo, hodnota vpravo. (1) název sledovaného rozhraní velkými písmeny a stav linky `UP`/`DOWN`/`UNKN` (bez známého rozhraní obecné `LINK`); (2) `RX` a (3) `TX` jako rychlost v automaticky vhodné jednotce `B/s`, `KB/s` nebo `MB/s` (desítkově, 1 KB = 1000 B, nejvýš 3 platné číslice: `312 B/s`, `9.9 KB/s`, `85 KB/s`, `1.2 MB/s`, `112 MB/s`); (4) `IPV4` se samotnou lokální adresou. Rozhraní platně bez IPv4 ukáže `NONE`, chybějící, neplatný, prošlý nebo ještě nespočítaný údaj (první vzorek) `UNKN`; nikdy stará hodnota, nikdy `OK`. Adresa ani port webového rozhraní se nezobrazují, dokud NexusSTAT nemá běžící web. Platná síťová měření (i linka `DOWN` a chybějící IPv4) jsou `INFO`, ne alarm; `UNKNOWN` se objeví ve STATUS (`UNKN NET RX NO DATA`, `UNKN LINK STALE`) a v symbolu `?`. Fiktivní hodnoty nesou `SIM` a adresy jen z dokumentačních rozsahů (RFC 5737).

### Budoucí kolotoč OLED (schválený návrh; NETWORK a CPU FAN hotové lokálně, STORAGE zbývá)

Body 1–17 popisují implementovanou v0.6 s pěti stránkami NEXUS, SYSTEM, DISK, NETWORK, SERVICES. **CPU FAN na stránce SYSTEM je od M4-1 implementovaný lokálně** (čtvrtý řádek, read-only kolektor `fan_monitor.py`); STORAGE místo DISK je dál plánovaná další fáze. Cílové uspořádání:

1. **NEXUS** beze změny (varianta B).
2. **SYSTEM**: CPU, CPU TEMP, RAM, CPU FAN; čtyři řádky čitelné na 128 × 64.
3. **STORAGE**: rozšíření dnešní stránky DISK.
4. **NETWORK**: nová stránka.
5. **SERVICES**: NEXUS, ANIMEDB, NAS, BACKUP beze změny.

- STATUS zůstává samostatný režim přístupný tlačítkem; řazení, stránkování a odlišení WARNING, ERROR, CRITICAL a UNKNOWN se nemění. Časování (10 s na stránku, okno 300 s) a pravidla v0.5 (anglicky, žádné falešné OK, `UNKN` vs `N/A`) platí i pro nové stránky.
- **CPU FAN** (jen read-only monitoring, žádné řízení): nejméně stav `ON`/`OFF`/`UNKN`, pokud ho lze spolehlivě získat; přednost mají skutečně měřené otáčky (RPM), pokud je hardware poskytuje. Odlišit požadavek řídicího systému od prokázaného fyzického otáčení. `OFF` při nízké teplotě sám o sobě není závada.
- **Implementace M4-1 (lokálně, `fan_monitor.py`):** **jedna** kontrola `system.cpu_fan` s jednoduchým stavem. `ON`/`OFF` z **naměřených otáček** (RPM > 0, respektive 0); otáčky jdou do `measurement` jako doplňující číslo a displej ukáže `ON 3200`, respektive `OFF`. Bez tachometru rozhoduje **jen to, zda řízení ventilátor žádá** (hwmon `pwmN`, jinak chladicí zařízení kernelu typu `pwm-fan`): stav je `REQ ON`, nebo `REQ OFF`, bez čísla a bez procent — požadavek se nikdy nevydává za potvrzené otáčení a otáčky se neodhadují. Zařízení se vybírá podle identity (`name` ovladače, `fanN_label` od zařízení), nikdy podle čísla `hwmonX` ani jména souboru; víc prokázaných zdrojů je `UNKN` (nejednoznačné), žádný prokazatelný zdroj `N/A` (není co sledovat, tedy ani závada). Žádná další diagnostická veličina a žádná závada: pravidlo pro rozpor „požadavek > 0, otáčky 0“ zůstává samostatným schváleným rozhodnutím.
- **STORAGE**: možné položky lokální systémový disk/root, NVMe/SSD, další lokální souborové systémy a relevantní NAS mounty. Rozlišuje se obsazenost souborového systému, volné místo, dostupnost mountu, skutečná dostupnost NAS a velikost konkrétní složky; velikost složky není obsazenost souborového systému. Žádné časté rekurzivní procházení NAS: zjišťování velikosti adresářů má vlastní bezpečný interval a timeout. Otevřené: pokud se nebude sledovat více úložišť, ROOT se přesune pod SYSTEM a stránka STORAGE odpadne.
- **NETWORK** (v0.6 implementováno lokálně, viz bod 17 a Síťový kolektor; zbývá adresa webu): vybrané rozhraní, stav linky, aktuální RX a TX, lokální IP adresa, případně adresa webového rozhraní NexusSTAT. RX/TX z rozdílu čítačů v čase; přenosová rychlost v B/s se nezaměňuje s procentním vytížením linky. Počítá se s více rozhraními včetně Dockeru a VPN, virtuální rozhraní se ale automaticky nezahrnují do hlavního zobrazení. Adresa/port webu se ukáže jen tehdy, když web skutečně běží a lze určit použitelnou přístupovou adresu; plánovaný port 8765 se nikdy nevydává za běžící web. IP adresy ani jiné interní údaje z této obrazovky nesmí automaticky proniknout do budoucího veřejného statusu.
- **SERVICES**: žádné falešné OK; nenapojená služba `UNKNOWN`, záměrně nenasazená nebo vypnutá integrace `N/A`. Rozšíření katalogu služeb až podle skutečných potřeb. AnimeDB má od SERVICES-1 volitelný healthcheck (sekce Healthcheck AnimeDB).

## Systémový kolektor (potvrzeno pro v0.2)

- Platná naměřená hodnota bez stanoveného alarmového prahu má stav `INFO`.
- STATUS bez aktivních závad zobrazuje `NO ACTIVE ISSUES`.
- První vzorek CPU bez dostatku údajů pro výpočet má stav `UNKNOWN`.
- Platnost výsledků je trojnásobek intervalu pravidelného měření (výchozí 3 × 10 s = 30 s), viz níže.
- Rozsah teploty (0, 125] °C je prozatím jen kontrola věrohodnosti údaje, ne univerzální hardwarový limit ani alarmový práh.
- Chyba jedné kontroly nesmí ovlivnit ostatní kontroly.

## Pravidelné měření (potvrzeno pro v0.3)

- Jedna přehledná smyčka pravidelně měří CPU, teplotu CPU, RAM a obsazenost `/`; výsledky stručně vypisuje do terminálu, bez vlastního logování.
- Interval `[collector] interval_seconds`, výchozí 10 s. Platnost výsledků je trojnásobek intervalu a samostatně se nenastavuje.
- Plánování v monotónním čase, čas měření v UTC. Plánované termíny postupně neujíždějí; při zpoždění se nespouští několik doháněcích měření najednou.
- Selhání měření nesmí vést k falešnému stavu OK. Žádné aktivní čekání vytěžující procesor. Čisté ukončení pomocí Ctrl+C.
- Zatím jen terminál: bez OLED hardware, služby a webu; SQLite historie je od M3.3b-1 volitelná (výchozí vypnuto).

## Číselné hodnoty a první alarmové prahy (potvrzeno pro v0.4)

- Měřená veličina nese číselnou hodnotu a jednotku (`measurement`), ne jen text. Alarmy se nikdy nevyhodnocují parsováním textu `summary`. Formát textu pro OLED a terminál zůstává.
- Chyba měření: hodnota nedostupná a stav `UNKNOWN`. Zachovány UTC čas, monotónní expirace a příznak `simulated`; po expiraci hodnota zmizí.
- Prahy zatím jen pro teplotu CPU (°C) a obsazení kořenového disku `/` (%). Pro každou veličinu: zapnutí/vypnutí, práh WARNING, práh ERROR (vyšší než WARNING), hystereze pro návrat do nižšího stavu, počet měření pro potvrzení vzniku (výchozí 3) a odeznění (výchozí 2).
- Výchozí konfigurace žádné prahy nezapíná a kód nemá výchozí prahové hodnoty. Bez zapnutého pravidla zůstává platné měření `INFO`. Neplatné kombinace konfigurace se odmítnou.
- Hodnota rovná prahu je nad prahem. Krátké jednorázové výkyvy alarm nevytvářejí ani neruší. `UNKNOWN` závadu nevyřeší; `N/A` se nemění. Simulovaná data se nesmějí prezentovat jako skutečný alarm Nexusu.
- Závažné stavy zatím nemají zvláštní rychlou cestu ani automatickou akci. Žádné automatické vypínání serveru ani regulace ventilátorů.
- Konkrétní hodnoty prahů pro Nexus zatím nejsou schválené.

## Síťový kolektor (v0.6, implementováno lokálně, nenasazeno)

- Read-only jediné síťové rozhraní: stav linky `UP`/`DOWN`/`UNKNOWN`, rychlost RX a TX, lokální IPv4. Kolektor prahy nevyhodnocuje: platný údaj `INFO`, nezjistitelný `UNKNOWN`; prahy pro síť zatím neexistují.
- Potvrzeno správcem (v0.6): linka `DOWN` je naměřený stav `INFO`, ne automatický alarm. Linka `UP` je jen provozní stav rozhraní (`operstate`), ne potvrzená dostupnost routeru, internetu ani jiné služby. Kontrola očekávané dostupnosti konkrétního rozhraní bude samostatná budoucí funkce. Jednotky RX/TX `B/s`, `KB/s`, `MB/s` desítkově; IPv4 přes lokální read-only `ioctl`; bez adresy `NONE`, při chybě zjišťování `UNKNOWN`; adresa webu se nezobrazuje, dokud web skutečně neběží.
- Linka podle provozního, ne administrativního stavu: administrativně vypnuté rozhraní (IFF_UP = 0) jádro hlásí jako `operstate` `down`, zapnuté bez nosné také; obojí je `DOWN`. Příznak IFF_UP nikdy neznamená `UP`.
- Zdroje: `/sys/class/net/<rozhraní>/` (`operstate`, `carrier`, `ifindex`, `statistics/rx_bytes`, `tx_bytes`), `/proc/net/route` (výchozí trasa). IPv4 dotazem na jádro `ioctl SIOCGIFADDR` přes nepřipojený socket (nic se neodesílá, neváže ani neotevírá). Žádné odchozí síťové požadavky ani spouštění příkazů.
- Rychlost z rozdílu kernelových čítačů a skutečně uplynulého monotónního času mezi dvěma měřeními (ne z nominálního intervalu). `UNKNOWN`: první vzorek, změna rozhraní (i stejný název s jiným `ifindex`), snížení čítače (reset nebo přetečení), nulový interval, mezera delší než platnost výsledků, chybějící nebo nečitelný čítač. Druhý směr a ostatní kontroly tím nejsou dotčeny.
- Výběr rozhraní: `[network] interface` (přesně, i virtuální), jinak automaticky a deterministicky: fyzické rozhraní s aktivní výchozí trasou IPv4 (nejnižší metrika) → fyzické s linkou UP → jakékoli fyzické; při shodě abecedně. Fyzické = má odkaz `device`; loopback, Docker, VPN a jiná virtuální rozhraní se automaticky nevybírají. Provoz různých rozhraní se nesčítá. Nečitelná tabulka tras: výběr podle linky. Žádné fyzické rozhraní nebo chybějící nastavené rozhraní: vše `UNKNOWN`.
- Linka: `operstate` `up` → UP; `down`, `lowerlayerdown`, `notpresent`, `dormant` → DOWN; `unknown` → podle `carrier`; jinak `UNKNOWN`.
- IPv4: primární adresa rozhraní; rozhraní bez adresy je platné „bez adresy“ (`NONE`), chyba dotazu nebo nesmyslná adresa `UNKNOWN`. IPv6 se nesleduje. IP adresy z této stránky nepatří do budoucího veřejného statusu.
- Pravidelná smyčka `run.py`, simulátor i `simulate --live` měří systém a síť společně; selhání jednoho kolektoru udělá `UNKNOWN` jen z jeho kontrol.

## Healthcheck AnimeDB (SERVICES-1, implementováno lokálně, nenasazeno)

NexusSTAT zůstává jednoduchý stavový panel: pro AnimeDB je důležitá dostupnost samotné aplikace, ne diagnostika kontejneru. Kontroluje se jen HTTP endpoint `/health`, který při dřívějším nasazení AnimeDB odpovídal HTTP 200 a JSON `{"status":"ok"}`. Nevzniká vlastní alarmový systém ani nový stav RGB.

- **Rozhodnutí správce (2026-10-24):** žádný přístup k `/var/run/docker.sock` ani členství ve skupině `docker`; dříve navržený (necommitnutý) kolektor přes `docker container inspect` byl nahrazen HTTP healthcheckem. Jediná kontrola zůstává `service.animedb`, žádný druhý paralelní monitor.
- Konfigurace `[services.animedb]`: `enabled` (výchozí `false`), `url` (bez výchozí hodnoty, povinná při zapnutí) a `timeout_seconds` (výchozí 2 s, rozsah (0, 5] s). Adresa jen `http://<IP adresa>[:port]/…/health`: IP kvůli tomu, že překlad názvu přes DNS nemá časový limit; bez přihlašovacích údajů, query a fragmentu. Skutečná adresa patří jen do lokální konfigurace mimo Git, ukázka používá dokumentační rozsah 192.0.2.0/24.
- **Vypnuté sledování** (výchozí): kolektor nevznikne a nikam se nepřipojuje; AnimeDB je `N/A` (nevyprší, není alarm).
- **Zapnuté sledování:** při každém měření jediný `GET` jen standardní knihovnou (socket a rozbor odpovědi `http.client`), bez proxy z prostředí a bez následování přesměrování. Spojení, odeslání i příjem mají **jeden celkový limit** `timeout_seconds` (každé čtení dostane jen zbývající čas), takže ani pomalu odpovídající server neblokuje měřicí smyčku déle. Tělo odpovědi nejvýš 4 KiB.
- Potvrzování výpadku je stejné jako u alarmových prahů (`confirm_count` 3, `clear_count` 2 z výchozího `AlarmRule`): výpadek je `ERROR` až po 3 po sobě jdoucích kontrolách s výpadkem; odezní po 2 po sobě jdoucích úspěšných kontrolách. Neznámý výsledek ani mezera v měření výpadek nepotvrdí ani nevyřeší, jen přeruší řadu. Při intervalu 10 s tak restart aplikace kratší než asi 20 s nevyvolá `ERROR` ani blikání.

| Situace | Stav | SERVICES | STATUS |
| --- | --- | --- | --- |
| sledování vypnuto | `N/A` | `N/A` | – |
| HTTP 200 a `{"status": "ok"}` | `OK` | `OK` | – |
| výpadek (spojení odmítnuto nebo přerušeno, timeout, nedostupná adresa, HTTP 5xx), 1. a 2. kontrola | `UNKNOWN` | `UNKN` | `ANIMEDB FAILING` |
| výpadek potvrzený 3. po sobě jdoucí kontrolou a dál | `ERROR` | `ERR` | `ANIMEDB DOWN` |
| po potvrzeném výpadku první úspěšná kontrola | `ERROR` | `ERR` | `ANIMEDB RECOVER` |
| jiná nebo neplatná odpověď (neplatný JSON, chybějící nebo jiný `status`, HTTP 3xx, 4xx a jiné 2xx než 200, příliš dlouhá odpověď) | `UNKNOWN` | `UNKN` | `ANIMEDB INVALID` |
| kontrolu nejde provést na této straně (např. nedostatek deskriptorů) | `UNKNOWN` | `UNKN` | `ANIMEDB NO DATA` |
| výsledek prošel (3 × interval) | `UNKNOWN` | `UNKN` | `ANIMEDB STALE` |

- Výpadek služby (`ERROR`, `DOWN`) se tak odlišuje od neznámého výsledku kontroly (`UNKNOWN`, `INVALID` nebo `NO DATA`). HTTP 4xx se nepočítá jako výpadek: dokládá spíš chybnou adresu nebo požadavek než nedostupnou aplikaci.
- RGB beze změny pravidel: potvrzený výpadek bliká červeně kódem kategorie 3; nepotvrzený výpadek i neznámý výsledek jsou `UNKNOWN`, neblikají a bílou vyloučí.
- Terminálový výpis `nexusstat.run`, volitelná historie SQLite (jen změny stavu, bez číselné řady), STATUS, stránka SERVICES, symbol na NEXUS i LED vycházejí ze stejného výsledku `service.animedb`. Popis výsledku ani historie adresu neobsahují. Ostatní služby (NEXUS, NAS, BACKUP) zůstávají bez kolektoru `UNKNOWN`.
- Ověřeno jen syntetickými odpověďmi HTTP v testech, ne proti běžící AnimeDB.

## Živý simulátor OLED (potvrzeno pro v0.5)

- Okno tkinter na notebooku; obraz výhradně z existujícího `render_screen()`, skutečná bitmapa 128 × 64 zvětšená celočíselně bez vyhlazení, obnova přes `after()` bez aktivního čekání, ve SLEEP černý displej, bezpečné zavření bez procesů na pozadí.
- Ovládání mimo plochu OLED: tlačítko „Stisk“ a klávesy mezerník/Enter, přepínač scénáře, režim SLEEP/CAROUSEL/STATUS, zrychlení 1×/5×/10× jen v simulaci. Logika je skutečný `OledController`.
- Jedna společná simulovaná časová základna pro controller, platnost výsledků i vykreslení.
- Scénáře s fiktivními daty (`simulated=True`, značka `SIM`): vše v pořádku, WARNING, ERROR a CRITICAL, UNKNOWN/zastaralá/chybějící data, více problémů (≥ 2 stránky STATUS), nenapojené služby. Změna scénáře nesmí míchat alarmy ani časy.
- Volitelný LIVE režim: read-only měření notebooku přes `SystemMonitor`, `NetworkMonitor` (v0.6) a `AlarmEvaluator`, bez `SIM`, ve skutečném čase, jasně označený jako notebook, ne Nexus; nenapojené služby `UNKNOWN`, AnimeDB `N/A`, se zapnutým `[services.animedb]` výsledek healthchecku (SERVICES-1).
- Síťové scénáře (v0.6, `SIM`): linka DOWN, bez IPv4, nízký provoz, vysoký provoz, neplatná a zastaralá data; všechny scénáře používají skutečný `render_screen()`.
- Čtyři simulované RGB LED (RGB-1): samostatná plocha pod displejem, do bitmapy OLED se nekreslí; barvu určuje `rgb.py` podle sekce RGB LED a potvrzování závad. Ovládání „Potvrdit WARNING/ERROR“ (potvrdí všechny aktivní), „Zrušit potvrzení“ a jas 5–100 % (výchozí 20 %), vše jen v paměti okna bez zápisu konfigurace. Potvrzení patří ke scénáři a změnou scénáře zaniká. Scénáře RGB: NAS WARNING, AnimeDB ERROR, zálohy ERROR, několik závad, CRITICAL a WARNING, potvrzená závada.
- Při zrychlení 5×/10× by blikání bylo rychlejší, než okno vykreslí (a rychlejší než 3 Hz): LED ukazují barvu vzoru trvale a popis to uvádí; logika včetně délky přehrání běží dál v simulovaném čase.
- Testy bez grafického displeje; stávající testy bez tkinter i Pillow.

## Tlačítko a kritické funkce

- Fyzický `pwr_button` je událost Linux input zařízení, nikoliv libovolný GPIO pin. Číslo zařízení `/dev/input/eventN` se může lišit a není trvalý kontrakt.
- Návrh krátkého/dlouhého stisku pro řízené vypnutí je **neuzavřený do HW testů**. Dlouhé 10s gesto reboot se **neimplementuje**, může kolidovat s hardwarovým vypnutím.
- Ovládání tlačítka při přechodu ověřit proti `systemd-logind` a přítomnosti exkluzivního `evdev` grabu.

## RGB LED a potvrzování závad (schválený návrh 2026-10-23; lokálně jen rozhodovací logika a simulace v náhledu)

Schválené chování čtyř RGB LED WS2812 ve vazbě na OLED a na budoucí potvrzování závad v lokálním webu. **Implementováno je jen lokálně na notebooku (RGB-1):** rozhodovací logika `rgb.py` a čtyři simulované LED v živém náhledu s potvrzením v paměti okna. Neexistuje ovladač RGB, web ani trvale uložená potvrzení a na Nexusu LED dál ovládá Pironman. NexusSTAT je jednoduchá náhrada softwaru Pironman 5 a stavový panel domácího serveru, ne další alarmový systém: signalizace vychází jen z existujících výsledků `CheckResult` po `AlarmEvaluator`u a kvůli RGB nevznikají žádné nové alarmy. Oddělení rozhodovací logiky od ovladače popisuje `ARCHITECTURE.md` (RGB signalizace a potvrzení závad); rozhodnutí správce jsou shrnutá na konci sekce.

### Normální stav

- OLED spí a všechny čtyři LED jsou zhasnuté.
- Krátký stisk tlačítka probudí OLED a LED se rozsvítí tlumeně bíle; když OLED usne, bílá zhasne. Chování tlačítka, přechody CAROUSEL/STATUS a časování automatu (10 s, 300 s, 30 s) se nemění a žádné nové gesto nevzniká.
- **Bílá značí zdravý ověřený stav, ne jen nepřítomnost alarmu.** Svítí jen při probuzeném OLED, když existuje aspoň jeden výsledek skutečně monitorované kontroly a všechny skutečně monitorované kontroly mají platný stav `OK` nebo `INFO`.
- **`UNKNOWN` a bílá:** `UNKNOWN` samo o sobě není `WARNING` ani `ERROR` a nikdy nespouští blikání. Dosud neimplementovaný nebo záměrně nesledovaný zdroj (služba z katalogu bez kolektoru, `N/A`) bílé při jinak zdravém systému nebrání. Skutečně monitorovaná kontrola s nedostupnými nebo prošlými daty se nesmí vydávat za OK: OLED ukazuje `?` a bílá se nepoužije, dokud není stav znám (LED zůstanou zhasnuté).
- Potvrzená, ale stále aktivní závada se bílou nikdy nezobrazí jako úplné OK.
- OLED se kvůli nové závadě samo neprobouzí (bod 9 sekce OLED platí dál).

### Aktivní závady

| Stav | Signál | Přednost |
| --- | --- | --- |
| `CRITICAL` | červená, 0,25 s svit / 0,25 s tma (2 Hz) nepřetržitě, bez kódu kategorie | před všemi ostatními vzory; potvrzení ho nikdy nevypne |
| `ERROR` | červené skupiny bliknutí 0,4 s svit / 0,4 s tma, počet = kód kategorie, po každé skupině pauza 2 s | před `WARNING` |
| `WARNING` | oranžové skupiny se stejným časováním jako `ERROR` | jen bez nepotvrzené `ERROR` |

- Všechny čtyři LED blikají vždy společně: počet bliknutí je kód kategorie, ne číslo fyzické LED.
- Automaticky se signalizuje jen nejvyšší závažnost mezi nepotvrzenými závadami; `CRITICAL` vždy. Více kategorií stejné závažnosti se přehraje postupně vzestupně podle kódu, každá s pauzou po skupině, a pak znovu od začátku. Kategorie s více závadami se přehraje jednou.
- Stabilní kódy kategorií:

| Bliknutí | Kategorie | Kontroly (`id`) |
| --- | --- | --- |
| 1 | Nexus a systém | `service.nexus`, `system.*`, `disk.*`, `network.*`, `history.storage` |
| 2 | NAS a jeho dostupnost | `service.nas` a budoucí kontroly NAS |
| 3 | AnimeDB | `service.animedb` a budoucí kontroly AnimeDB |
| 4 | zálohy, Restic | `service.backup`, `history.snapshot` a budoucí kontroly Resticu |
| 5 | Docker a ostatní skutečně monitorované služby | budoucí kontroly Dockeru a služeb; sem spadne i **každé nezařazené id** skutečné závady |

- Blikají jen `WARNING`, `ERROR` a `CRITICAL`. Pouhé `INFO`, `UNKNOWN`, `N/A` a běžné změny provozních údajů (např. linka `DOWN`, která je `INFO`) neblikají. Prošlý výsledek se stejně jako na OLED vyhodnotí jako `UNKNOWN`, takže dřívější závada bez platných dat nebliká.
- Simulovaná data (`simulated=True`) skutečné LED nikdy nerozsvítí: LED nemají značku `SIM` a simulace se nesmí prezentovat jako skutečný alarm Nexusu.

### Potvrzení závady

- Lokální web nabídne u každé aktivní závady tlačítko **„Beru na vědomí / pracuji na nápravě“**. Potvrzení není oprava.
- Po potvrzení `WARNING` nebo `ERROR` závada zůstává aktivní a její diagnostický stav se nemění; vypne se jen její automatické blikání. OLED ji dál ukazuje beze změny (STATUS, pořadí, trojúhelník na stránce NEXUS).
- Potvrzení platí pro danou kontrolu až do potvrzené závažnosti včetně, dokud trvá epizoda závady: zmírnění `ERROR` → `WARNING` nové potvrzení nevyžaduje, zhoršení `WARNING` → `ERROR` ano. Nová epizoda závady vyžaduje nové potvrzení.
- **Zánik potvrzení:** jen po doloženém odeznění závady, tedy po nejméně třech po sobě jdoucích platných měřeních bez závady (`OK` nebo `INFO`); vyžaduje-li alarmové pravidlo delší řadu (`confirm_count` nad 3), platí ta delší. `UNKNOWN`, `N/A` ani prošlá data se jako odstranění problému nepočítají.
- **Restart:** potvrzení `WARNING`/`ERROR` přežívá restart NexusSTAT. `AlarmEvaluator` po restartu začíná znovu (`HISTORY.md` 4.2), takže se trvající závada prvních měření jeví jako `INFO`; během tohoto počátečního potvrzování výsledků se uložené potvrzení nesmí předčasně zrušit.
- Potvrzení nese závažnost, kterou uživatel viděl; zhoršila-li se mezitím, odmítne se a uživatel rozhodne znovu.
- `CRITICAL` lze evidovat jako přečtený, ale je signalizovaný i po potvrzení. Přečtení neumlčí ani `ERROR` či `WARNING`, na které se `CRITICAL` později zmírní; ty vyžadují běžné potvrzení.
- Volitelné „potvrdit všechny `WARNING`/`ERROR`“ potvrdí jen závady aktivní v tu chvíli, ne budoucí; `CRITICAL` se tím neumlčí.
- Potvrzení a jeho zánik jsou událost historie alarmů (viz Historie a databáze SQLite); web je předá procesu NexusSTAT, který je zapíše frontou zapisovače (`HISTORY.md` 5.8, bod 5). Signalizace ani potvrzení na zapnuté historii nezávisí.

### Tlačítko, OLED a přehrání kódů

Priorita vzorů od nejvyšší: `CRITICAL` → automatická signalizace nepotvrzených závad → jednorázové přehrání potvrzených závad → bílá → zhasnuto.

- Aktivní `CRITICAL`: nepřetržité červené blikání; ostatní vzory mají nižší prioritu.
- Aktivní nepotvrzená závada: automatická signalizace podle její závažnosti pokračuje i při probuzeném OLED.
- Jen potvrzené aktivní `WARNING`/`ERROR`: při probuzení OLED se jejich světelné kódy jednou přehrají (nejdřív `ERROR` červeně, pak `WARNING` oranžově, kategorie vzestupně). Po dokončení přehrání LED opět zhasnou a do uspání OLED se bílá nepoužije.
- Ostatní stisky (otevření STATUS, listování, návrat do CAROUSEL) a časování OLED mají dosavadní význam a nic nepřehrávají.

### Jas a nastavení

- Jediný uživatelský parametr jasu 5–100 %, výchozí 20 %. Platí pro červenou i oranžovou, tedy i pro `CRITICAL`; dolní mez 5 % brání umlčení `CRITICAL` jasem.
- Bílá při probuzeném OLED svítí tlumeněji, přibližně na polovině nastaveného jasu; druhý posuvník není.
- Jas i potvrzení přetrvají restart NexusSTAT.
- Nastavení jasu a potvrzování jsou dostupné jen oprávněnému uživateli v LAN (autentizace, ochrana CSRF, viz Web / přístup); veřejný status je nenabízí ani nezobrazuje.

### Ukládání uživatelského stavu (schválený návrh, bez kódu)

- Jas, potvrzení aktivních závad a údaje potřebné k rozpoznání nové epizody nebo zhoršení budou v samostatném malém souboru v `appdata` NexusSTAT, ne v SQLite historii. Schéma SQLite v1 se nerozšiřuje a nový databázový subsystém nevzniká.
- Při budoucí implementaci: atomický zápis, soukromá oprávnění, zachování po restartu a zahrnutí do záloh.

### Bezpečnost hardwaru a převzetí od Pironmanu

- Pironman zůstává v provozu a má RGB pod svou kontrolou až do samostatně schváleného bezpečného převzetí. LED smí ovládat vždy jen jeden program; NexusSTAT je neotevře, dokud je ovládá Pironman (`WS2812Addon`). Souběžný zápis dvou programů nejde spolehlivě odhalit, proto rozhoduje ověřený postup, ne automatická detekce.
- Převzetí RGB nesmí zastavit Pironman jako celek, dokud nejsou ověřené boční ventilátory: RGB Pironmanu se vypne jen jeho vlastní konfigurací, jinak až při celkovém přechodu (M7). Konkrétní volbu Pironmanu i zapojení LED (předpoklad SPI pro Adafruit NeoPixel SPI) je nutné nejdřív ověřit read-only.
- Budoucí ovladač RGB bude ve výchozím stavu vypnutý a při inicializaci i korektním vypnutí odešle zhasnutí všech LED. Selhání LED nesmí zastavit měření, alarmy, OLED ani historii. Návrat: vypnout RGB v NexusSTAT a vrátit původní nastavení RGB Pironmanu.
- **Provozní omezení (M7):** WS2812 mohou po pádu procesu držet poslední barvu. Pouhé ukončení aplikace to neodstraní, takže svit LED po pádu NexusSTAT nic nedokládá (zamrzlá bílá neznamená OK). Hardwarový watchdog ani další elektronika se nenavrhují.

### Rozhodnutí správce (2026-10-23)

1. `UNKNOWN` nikdy nebliká; neimplementovaný nebo záměrně nesledovaný zdroj bílé nebrání, skutečně monitorovaná kontrola bez platných dat bílou vyloučí. Bílá = zdravý ověřený stav.
2. Potvrzení `WARNING`/`ERROR` přežívá restart a zaniká až po nejméně třech platných měřeních bez závady, případně po delší řadě podle alarmového pravidla; `UNKNOWN` se nepočítá. Nová epizoda a zhoršení vyžadují nové potvrzení, `CRITICAL` zůstává signalizovaný.
3. Probuzení OLED jednou přehraje kódy potvrzených aktivních závad a LED pak zhasnou; nepotvrzené závady a `CRITICAL` mají přednost; tlačítko a časování OLED beze změny.
4. Kód 0,4 s svit / 0,4 s tma s pauzou 2 s po skupině, `CRITICAL` 0,25 s / 0,25 s nepřetržitě; jediný jas 5–100 % (výchozí 20 %), bílá asi na polovině.
5. Stabilní kategorie 1–5, nezařazené id skutečné závady do 5; `INFO`, `UNKNOWN`, `N/A` a provozní údaje neblikají; žádné nové alarmy kvůli RGB.
6. Samostatný malý soubor v `appdata` pro jas a potvrzení (atomický zápis, soukromá oprávnění, po restartu, v zálohách); SQLite v1 beze změny.
7. Zhasnutí při inicializaci a korektním vypnutí ovladače; držení barvy WS2812 po pádu je provozní omezení M7; Pironman ovládá RGB do schváleného převzetí.

## Ventilátory

- Hlavní PWM ventilátor CPU ponechat stávající linuxové regulaci.
- Oba boční ventilátory nyní ovládá Pironman přes GPIO6 v režimu Always On (`gpio_fan_mode=0`); GPIO6 bylo při read-only kontrole `HIGH`. Kernelový gpio-hog je **návrh**, není nasazen ani fyzicky otestován.
- Nezastavovat Pironman, dokud není schválené a ověřené předání řízení ventilátorů a tlačítka.
- Monitoring ventilátorů: CPU FAN read-only je od M4-1 implementovaný lokálně (`fan_monitor.py`, čtvrtý řádek stránky SYSTEM), na Raspberry Pi 5 neověřený. U bočních ventilátorů se nejdřív ověří, zda vůbec poskytují otáčky; bez tachometru lze jejich stav nanejvýš odvodit z řídicího signálu, a i jeho čtení (GPIO) vyžaduje samostatné schválení.

## Web / přístup

- Lokální webový dashboard dostupný z LAN na konfigurovatelném portu (navržen `8765`), po schválení firewallu a autentizace.
- Funkce LAN: přehledné stavové karty, filtrování událostí, potvrzení závad a jas LED (viz RGB LED a potvrzování závad; `CRITICAL` nelze umlčet), konfigurace (včetně délky kolotoče 300 s, intervalu 10 s, nečinnosti STATUS 30 s).
- Na veřejném webu `mh42.cz` pouze sanitizovaný read-only celkový stav a případně stručný výpis závad. **Nikdy** celé logy, interní IP, administraci ani API pro zápis.
- Oddělit veřejnou projekci dat od administračního procesu už v návrhu; žádný automatický port forwarding nebo cloud.
- Budoucí lokální web zobrazí historii ze SQLite jen pro čtení; veřejný status z ní dostane jen výslovně povolené souhrnné údaje (allowlist), nikdy IP adresy, rozhraní ani cesty.

## Jednoduchost a způsob vývoje

- Claude primární vývojář; Python + standardní knihovna, další balíčky jen s důvodem. Pro obrazový simulátor Pillow; pro budoucí OLED Adafruit SSD1306, pro RGB Adafruit NeoPixel SPI.
- Nevyužívat převzatý zdrojový kód SunFounder. Testy nejprve s fiktivními daty, produkční testy až po samostatném souhlasu.
- Počáteční milník: core + simulátor OLED + testy timeoutů, více stránek STATUS, upozornění, obnovy stavu, bez jakéhokoli přístupu k zařízení.
