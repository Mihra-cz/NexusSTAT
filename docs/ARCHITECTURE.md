# Architektura NexusSTAT – návrh

## Zásada

Diagnostika běží odděleně od hardwarových ochranných funkcí. Selhání NAS nebo webu nesmí zastavit kontrolu systému; selhání diagnostiky nesmí ovlivnit chlazení. Selhání ukládání historie (SQLite) nesmí zastavit monitoring, alarmy ani OLED.

Cílem je nahradit aplikační software Pironman 5, ne ho trvale doplňovat (viz Náhrada Pironmanu 5). Implementováno je zatím jen to, co popisuje sekce Implementováno lokálně; vše ostatní v tomto dokumentu je návrh.

## Plánované komponenty (cílový stav, návrh)

```text
read-only kolektory          Core                            výstupy
  system, CPU FAN,      ->   aktuální měření v paměti   ->   OLED renderer
  network, storage,          (platnost 3 × interval)         RGB renderer
  NAS, backup, služby        alarmy (AlarmEvaluator)         LAN web (autentizovaný, čte i historii)
                                   |                         PUBLIC snapshot (allowlist, bez IP)
                                   v
                             zapisovač historie   ->   SQLite na lokálním SSD
                             (dávky, oddělené          (měření, agregace, alarmy, události)
                              selhání)                       |
                                                             v
                                                  kopie přes Backup API -> existující Restic

Linux kernel/firmware -> hlavní CPU ventilátor (NexusSTAT jen čte jeho stav)
Linux GPIO controller  -> boční ventilátory, až po samostatně ověřeném přechodu
InfluxDB Pironmanu     -> NexusSTAT ji nepoužívá; vypnutí až po ověření dalších uživatelů
```

## Výchozí stav cílové platformy (zobecněno)

Podrobnosti skutečné instalace (názvy služeb, cesty mountů, výsledky inventury) jsou v soukromé dokumentaci Nexusu mimo tento repozitář. Před nasazením je vždy znovu ověřit na cílovém stroji.

- Raspberry Pi 5, Debian 13, ARM64.
- Referenční verze Pironman 5 1.3.20 (pm_auto 2.0.6), služba `pironman5.service`. V jejím procesu běží moduly ventilátorů, OLED a RGB (`FanAddon`, `OLEDAddon`, `WS2812Addon`), které má NexusSTAT postupně nahradit.
- Boční ventilátory řídí Pironman přes GPIO6 v režimu Always On; kernelový `gpio-hog` je jen návrh, není aktivovaný.
- Hostitel zálohuje Resticem na NAS a NAS je připojený síťovými mounty; metadata mountu samy nezaručují online dostupnost NAS.
- AnimeDB zatím na Nexusu neběží.

## Datový kontrakt (návrh)

- Každá kontrola: `id`, `state`, `summary`, `observed_utc`, `expires_monotonic`, `simulated`, volitelně `measurement` (číselná hodnota + jednotka) a od v0.6 `details` (krátké textové údaje ASCII, např. název rozhraní a IPv4); později `source`.
- `summary` je jen text k zobrazení. Rozhodování (prahy, alarmy) používá výhradně `measurement`.
- Agregovaný výsledek musí odlišovat `UNKNOWN` od `OK`. Historie alarmů a událostí zaznamenává přechody, ne kopie každého měření; historie měření ukládá vzorky časových řad s retencí (viz Historie a SQLite).
- Dvě časové základny, nikdy se neporovnávají mezi sebou: monotónní čas (`time.monotonic()`) pro intervaly, timeouty a platnost v běžícím procesu; skutečný UTC čas (`observed_utc`) pro budoucí perzistentní historii a časové značky událostí. Monotónní čas nepřežije restart, proto se nesmí ukládat do historie. Při budoucím exportu do JSON je nutné platnost převést (např. na zbývající sekundy nebo UTC čas vypršení v okamžiku exportu).
- Aktuální JSON publikovat lokálně atomicky, pouze s odpovídajícími oprávněními a časem platnosti.

## Náhrada Pironmanu 5 (cílová architektura, návrh)

NexusSTAT má převzít aplikační funkce Pironmanu po jednotlivých funkcích. Pironman zůstává v provozu, dokud není převzetí každé potřebné funkce ověřené na Nexusu.

| Funkce Pironmanu | Cíl v NexusSTAT | Stav dnes | Podmínka převzetí |
| --- | --- | --- | --- |
| Systémový monitoring | `system_monitor`, smyčka měření, alarmy | read-only kolektor a smyčka na notebooku | ověření na RPi 5 (M7) |
| OLED (`OLEDAddon`) | `oled`, `oled_text`, `render` + ovladač SSD1306 (Adafruit) | jen simulace | HW test displeje; Pironman ho v té době nesmí ovládat |
| RGB (`WS2812Addon`) | rozhodovací logika RGB oddělená od ovladače (Adafruit NeoPixel SPI), chování schválené 2026-10-23 | jen schválený návrh | jediný vlastník LED: RGB Pironmanu vypnuté jeho konfigurací bez zastavení služby, HW test, rollback |
| Tlačítko | obsluha události Linux input | nic | ověření `systemd-logind` a `evdev` grabu |
| Ventilátory (`FanAddon`) | hlavní CPU ventilátor: Linux řídí, NexusSTAT čte; boční: nezávislé řízení (návrh gpio-hog) + monitoring | hlavní CPU ventilátor read-only jen lokálně (M4-1), boční nic | fyzicky ověřené nezávislé řízení bočních ventilátorů s rollbackem |
| Historie (InfluxDB) | SQLite na lokálním SSD | implementováno lokálně (M3), nenasazeno | ověřená záloha a obnova na Nexusu (M7) |
| Webový dashboard | lokální LAN web NexusSTAT | nic | autentizace, schválení firewallu (M6) |

- Návrat: Pironman se při převzetí jen zastaví a zakáže, neodinstaluje; jeho konfigurace i dotčené systémové soubory se předem zálohují a postup návratu se vyzkouší.
- InfluxDB: NexusSTAT ji nečte ani nezapisuje. Před vypnutím se read-only ověří, kdo ji používá (klienti, dashboardy, skripty); vypnutí jen po schválení a se zálohou dat.

## Historie a SQLite (implementováno lokálně v M3, nenasazeno)

Podrobný návrh M2 (tabulky a DDL, čas a identita, zápis, agregace, retence, odolnost, záloha a obnova, benchmark, plán M3, rozhodnutí ke schválení) je v `HISTORY.md`. Lokálně je implementovaný v M3 (sekce Implementováno lokálně), na Nexusu nic; body níže jsou shrnutí.

- Jediný zapisovač historie ve vlastním vlákně procesu NexusSTAT dostává výsledky ze smyčky měření po `AlarmEvaluator`u. Zápis běží v dávkách (nejstarší nezapsané měření nejvýš 60 s, změny stavu a události do 10 s), každá dávka jednou transakcí s ochranou proti opakování (`run.last_batch`). Selhání zápisu smyčku ani výstupy nezastaví: omezené fronty v paměti (návrh 1 h měření a 2 000 změn), při jejich zaplnění se zahodí nejstarší položky a ztráta se zaznamená; výpadek zápisu je vlastní stav `history.storage`, ne závada měřené veličiny. Ztráta „nejvýš 60 s“ není garance, platí jen za podmínek v `HISTORY.md`, sekce 5.5. Zapisuje a checkpoint dělá jediné spojení zapisovače, všechna ostatní jsou jen pro čtení; tím návrh vylučuje podmínky chyby SQLite „WAL-reset“, jejíž oprava je v použitých verzích neprokázaná (`HISTORY.md`, sekce 5.8, gate 12.4).
- Čas v databázi je UTC (`observed_utc`, celé Unixové sekundy); monotónní čas se nikdy neukládá. Skutečná nula = řádek s hodnotou 0, `UNKNOWN` = řádek s `NULL`, chybějící vzorek = žádný řádek (mezera), `N/A` = jen stav v tabulce změn. Simulovaná data do provozní databáze nepatří (odmítnutí v zapisovači, role a otisk stroje v souboru).
- Historie měření: surové vzorky po 10 s, z nich průběžně 5min a hodinové agregace (počet vzorků, počet platných, minimum, maximum, součet pro průměr), aby byly vidět mezery. Historie alarmů a události: jen přechody (vznik, změna závažnosti, odeznění, ztráta dat, budoucí potvrzení či ztišení; start, ukončení, výpadek kolektoru, změna konfigurace, skok času, mezera v historii).
- Retence: 30 dní surových vzorků je nejkratší doba uchování; vzorek se smaže, až když je starší a zároveň agregovaný, po hodinových dávkách s limitem jednoho dne dat za běh. Agregace nikdy nepřepisuje hotové okno. Smazaná data soubor nezmenší, SQLite uvolněné stránky znovu použije; `VACUUM` jen výjimečně, protože potřebuje volné místo zhruba velikosti databáze. Před zápisem se hlídá volné místo; `SQLITE_FULL` nebo chyba I/O zastaví jen zápis historie.
- Režim WAL umožní čtení webem během zápisu. Záloha jen přes Backup API do neměnných generací snímků, které zahrne existující Restic (`HISTORY.md`, sekce 9, provoz v `OPERATIONS.md`).
- Schéma v1: `meta`, `schema_migration`, `run`, `metric`, `sample`, `agg_5m`, `agg_1h`, `aggregation_state`, `check_change`, `event`; verze v `PRAGMA user_version`, identita souboru v `application_id`.

### Měření velikosti a zápisů (benchmark M2)

Reprodukovatelný benchmark `tools/sqlite_history_benchmark.py` nad dočasnými syntetickými databázemi (notebook x86_64, SATA SSD, SQLite 3.45.1; ne Raspberry Pi 5), 20 řad po 10 s. Úplné výsledky a metodika jsou v `HISTORY.md`, sekce 10; nahrazují jednorázový orientační pokus z 2026-10-19 (10 veličin).

| | Výsledek |
| --- | --- |
| surová data 30 dní | 102–105 MB WITHOUT ROWID, 202 MB s ROWID a indexem |
| zápis surových dat, dávka 60 s | 35 MB/den s klíčem (`ts`, `metric_id`), 266 MB/den s klíčem (`metric_id`, `ts`) |
| agregace za rok | 1 min 410 MB, 5 min 81 MB, hodinové 7 MB |
| doporučená databáze celkem | asi 200 MB po roce, 270 MB po 10 letech; na disku se snímkem asi 0,6–0,8 GB |
| po pravidelné retenci (týden cyklu zápis → agregace → retence → snímek) | velikost stálá 194–195 MB; logický zápis asi 310 MB/den, z toho denní snímek 195 MB |

Závěry: klíč začínající časem a dávkování po 60 s drží zápisy surových dat na desítkách MB denně; 5min agregace jsou pětkrát menší než minutové. I s denním snímkem je to asi 110 GB/rok, malé proti výdrži běžných NVMe ve stovkách TBW; skutečný disk Nexusu se ověří read-only (SMART) před nasazením.

## Budoucí stránky OLED (návrh; NETWORK a CPU FAN hotové lokálně, STORAGE zbývá)

Požadavky jsou v `REQUIREMENTS.md` (Budoucí kolotoč OLED). Technické poznámky pro M4–M5:

- Kolotoč NEXUS → SYSTEM → STORAGE → NETWORK → SERVICES; časování se počítá z počtu stránek, takže pátá stránka okno 300 s neprodlouží (ověřeno v0.6 s pořadím NEXUS → SYSTEM → DISK → NETWORK → SERVICES). STORAGE místo DISK je další fáze; CPU FAN je od M4-1 hotový lokálně.
- Rozvržení: čtyři řádky jako dnes SERVICES (12 px), název 6x13 vlevo, hodnota 8x13B vpravo (nejvýš asi 9 tučných znaků); na řádek se vejde 21 znaků 6x13. Pravidlo v0.5 „velká písmena ASCII“ by pro jednotky typu `kB/s` nebo `Mb/s` potřebovalo výslovnou výjimku (otevřené).
- **CPU FAN** (M4-1 implementováno lokálně, `fan_monitor.py`): požadavek řízení je hwmon `pwmN` prokázaného zařízení, jinak stupeň chlazení kernelu (`/sys/class/thermal/cooling_device*` typu `pwm-fan`, `cur_state`/`max_state`); skutečné otáčky z hwmon (`fan*_input`), pokud je tachometr zapojen a ovladač ho vystavuje. Na displeji je především jednoduchý stav: `ON 3200` (doložené otáčky s doplňujícím číslem), `OFF` (doložené stání), `REQ ON`/`REQ OFF` (stav jen z požadavku řízení, nikdy tvrzení o otáčení), `UNKN` (nečitelné, nejednoznačné nebo prošlé) a `N/A` (prokazatelný zdroj neexistuje). Závada až při potvrzeném rozporu (požadavek > 0, otáčky 0) zůstává **neimplementovaná**: kolektor stavy nevyhodnocuje a prahy jsou samostatné rozhodnutí. Žádný zápis do `/sys`. **Ověřeno jen na notebooku nad syntetickými adresáři** (notebook žádný ventilátor v hwmon nemá, takže skutečná data dávají `N/A`); hodnoty `pwmfan`, `fan1_input` a `cooling_device` typu `pwm-fan` je nutné read-only ověřit na Raspberry Pi 5 (M7).
- **STORAGE**: `statvfs` na síťovém mountu se může zaseknout, proto každá kontrola úložiště poběží s timeoutem mimo hlavní smyčku (dnešní smyčka je jednovláknová a bez timeoutu). Dostupnost mountu z `/proc/self/mountinfo`; skutečná dostupnost NAS je samostatná kontrola s timeoutem; velikost složek má vlastní dlouhý interval a timeout a na NAS se často nespouští.
- **NETWORK** (v0.6 implementováno lokálně, popis v sekci Implementováno lokálně; zbývá adresa webu a případné prahy): RX/TX z `/sys/class/net/<rozhraní>/statistics/{rx,tx}_bytes` jako rozdíl za monotónní čas (první vzorek a reset čítače `UNKNOWN`, stejně jako CPU); stav linky z `operstate`; procentní vytížení jen při známé rychlosti linky (`speed`, u Wi-Fi často neznámá). Hlavní rozhraní z konfigurace nebo výchozí trasy (`/proc/net/route`); virtuální rozhraní (Docker `docker0`, `br-*`, `veth*`, VPN) se do hlavního zobrazení automaticky nezahrnují. IPv4 (až 15 znaků) se vejde jen písmem 6x13, IPv6 se na řádek nevejde. Adresa webu jen po úspěšné kontrole běžícího webu, nikdy podle plánovaného portu.
- Veřejný status dostane jen allowlist souhrnných stavů; údaje stránky NETWORK do něj nepatří.

## RGB signalizace a potvrzení závad (schválený návrh 2026-10-23, neimplementováno)

Požadavky a rozhodnutí správce jsou v `REQUIREMENTS.md` (RGB LED a potvrzování závad). Nic z toho není implementováno; sekce popisuje budoucí datové vazby. Nový alarmový systém nevzniká: závažnost dál určují kolektory, `AlarmEvaluator` a `effective()` a `core.py`, `alarms.py` ani `oled.py` se kvůli RGB nemění. Rozhodovací logiku lze po schválení implementovat a testovat lokálně dřív než ovladač, který patří do M7.

### Pět oddělených údajů

| Údaj | Odkud | Kdo ho mění | Dnes |
| --- | --- | --- | --- |
| skutečný stav služby | kolektor → `CheckResult` (`measurement`, `details`) | jen kolektor | implementováno lokálně |
| závažnost | `CheckResult.state` po `AlarmEvaluator`u, expirace přes `effective()` | jen diagnostika | implementováno lokálně |
| potvrzení uživatelem | záznam v souboru uživatelského stavu | oprávněný uživatel přes web; zaniká doloženým odezněním | návrh |
| výsledná RGB signalizace | čistá funkce z ostatních údajů a jasu | nikdo, jen se počítá | návrh |
| stav OLED | `OledController.mode` (SLEEP = spí, CAROUSEL a STATUS = probuzený) | tlačítko a časovače | implementováno lokálně |

Potvrzení nikdy nemění závažnost ani `CheckResult` a signalizace nikdy nemění potvrzení ani stav OLED.

### Tok dat

```text
kolektory -> CheckResult -> AlarmEvaluator --> complete_service_results() --> OLED
                                  |
                                  |  jen skutečně monitorované výsledky (bez doplněných služeb bez kolektoru)
                                  v
jas a potvrzení (soubor stavu) ---> rgb_signal(výsledky, potvrzení, OLED, přehrání, now)
OledController.mode po update(now) ->      |  Signal: OFF | WHITE | CODES(sekvence) | CRITICAL
                                           v
                                    přehrávač: frame_at(signal, t) -> barva a jas v čase t
                                           |  spí do příští změny, nic nerozhoduje
                                           v
                                    ovladač WS2812 (Adafruit NeoPixel SPI): 4 × stejná barva
```

- **Skutečně monitorované kontroly** jsou výsledky, které dodal kolektor, včetně `UNKNOWN` po jeho selhání a výsledků, které mezitím prošly. Služby, které `complete_service_results()` doplní do seznamu pro OLED jen proto, že pro ně kolektor ještě neexistuje, a výsledky `N/A` do rozhodnutí o bílé nevstupují; OLED je dál ukazuje (`UNKN`, `?`). `rgb_signal()` proto dostává výsledky před doplněním katalogem a nový příznak v `CheckResult` není potřeba.
- **Rozhodovací logika** (budoucí `rgb.py` bez importu hardwaru): `category_of(id)` podle tabulky kategorií, `rgb_signal()` a `frame_at()`. Čisté funkce testované `unittest`em se simulovaným časem jako `oled.py`.
- **Ovladač** (budoucí tenká vrstva nad Adafruit NeoPixel SPI): jen zapíše čtyřem LED stejnou barvu s jasem, nic nerozhoduje a importuje se až při zapnutém RGB, takže testy i notebook běží bez hardwaru. Při inicializaci a korektním vypnutí odešle zhasnutí všech LED.
- Blikání potřebuje změny po stovkách ms, kdežto měřicí smyčka tiká po 10 s. Přehrávač proto poběží v malém vlastním vlákně, které dostává jen neměnný `Signal` a spí do dalšího přechodu svit/tma; selhání zápisu na LED nezastaví měření, alarmy, OLED ani historii. Přesné uspořádání se navrhne s obsluhou tlačítka a ovladačem OLED (M7).

### Pravidla `rgb_signal()` (v tomto pořadí)

1. Vstupem jsou skutečně monitorované výsledky po `effective(result, now)`, na hardwaru jen `simulated=False`. Blikají jen `WARNING`, `ERROR` a `CRITICAL`.
2. Aktivní `CRITICAL` → `CRITICAL`; potvrzení, přehrání ani stav OLED na tom nic nemění.
3. Nepotvrzená `ERROR` → `CODES` červeně pro kategorie nepotvrzených `ERROR`; jinak nepotvrzená `WARNING` → `CODES` oranžově pro kategorie nepotvrzených `WARNING`. Platí i při probuzeném OLED.
4. Běžící přehrání po probouzecím stisku → jeden průchod `CODES` pro potvrzené aktivní závady: kategorie `ERROR` červeně, pak `WARNING` oranžově, každé vzestupně. Přehrání začne jen tehdy, když v okamžiku probuzení existuje potvrzená aktivní `WARNING`/`ERROR` a nebliká nic s vyšší prioritou; po průchodu skončí.
5. Probuzený OLED, aspoň jeden skutečně monitorovaný výsledek a všechny `OK` nebo `INFO` (žádná aktivní závada, potvrzená ani nepotvrzená, a žádné `UNKNOWN`) → `WHITE` na polovině nastaveného jasu.
6. Jinak `OFF`: spící OLED, probuzený s potvrzenou trvající závadou po přehrání, nebo probuzený se skutečně monitorovanou kontrolou bez platných dat (OLED ukazuje `?`). Po startu procesu tak LED zůstanou zhasnuté až do prvního úplného měření (první vzorek CPU je `UNKNOWN`).

- Probouzecí stisk se pozná bez změny automatu: po `update(now)` je `mode` SLEEP a po `press(now, …)` CAROUSEL. Porovnání musí jít až po `update(now)`, protože `press()` nejdřív sám uplatní vypršení okna a stisk po uplynutí 300 s je také probouzecí. Význam stisků, `update()` i časování 10 s / 300 s / 30 s zůstávají.
- Změna signálu (nová závada, potvrzení, uspání) se projeví až na hranici skupiny, aby přerušená skupina neukázala špatný počet bliknutí; `CRITICAL` přebíjí okamžitě.
- Časování: bliknutí kódu 0,4 s svit a 0,4 s tma, po každé skupině pauza 2 s; `CRITICAL` 0,25 s svit a 0,25 s tma (2 Hz) bez pauzy. Kód je zřetelně pomalejší než `CRITICAL` a nic nebliká rychleji než 3× za sekundu.
- Barvy: červená a oranžová s nastaveným jasem 5–100 % (výchozí 20 %), bílá asi na polovině nastaveného jasu.
- Kontroly bez potvrzování opakovaným měřením (např. `history.storage`) se signalizují, jakmile stav nahlásí; RGB kvůli tomu nepřidává vlastní zpoždění ani nová pravidla.

### Datová vazba potvrzení

- Záznam: `check_id`, potvrzená závažnost (`WARNING` nebo `ERROR`; u `CRITICAL` jen značka „přečteno“ bez vlivu na RGB), čas UTC a uživatel. Existence záznamu znamená, že epizoda závady ještě nebyla doloženě ukončená; zhoršení se pozná porovnáním aktuální závažnosti s potvrzenou. Jen pro skutečná data; simulátor si potvrzení drží jen v paměti.
- Platí, když je efektivní stav kontroly `WARNING` nebo `ERROR` a není závažnější než potvrzená úroveň.
- **Zánik:** po `N` po sobě jdoucích platných měřeních `OK`/`INFO`, kde `N = max(3, confirm_count)` u kontrol s alarmovým pravidlem a `N = 3` u ostatních. Počítají se nová měření (identita vzorku jako v `AlarmEvaluator`u podle `expires_monotonic`), ne opakovaná vyhodnocení při překreslení; `UNKNOWN`, `N/A`, prošlý výsledek i závada řadu vynulují.
- **Restart:** počítadlo řady je jen v paměti a po restartu začíná od nuly. `AlarmEvaluator` po restartu hlásí trvající závadu jako `INFO`, dokud ji `confirm_count` měřeními znovu nepotvrdí; protože `N ≥ confirm_count`, uložené potvrzení během tohoto počátečního potvrzování nezanikne. Odeznění mimo běh procesu doložit nelze: závada, která po restartu trvá se stejnou nebo nižší závažností, se bere jako pokračující epizoda.
- Vlastníkem záznamů je hlavní proces NexusSTAT. Web pošle požadavek (`check_id` a závažnost, kterou uživatel viděl), proces ho ověří proti aktuálnímu stavu, uloží ho a auditní událost zapíše frontou zapisovače historie. Web sám nezapisuje do databáze ani do souboru stavu (jediný zapisovač, `HISTORY.md` 5.8).
- **Uložení:** samostatný malý soubor v `appdata/nexusstat/` pro jas, potvrzení aktivních závad a údaje k rozpoznání nové epizody nebo zhoršení, mimo SQLite historii, protože historie je volitelná a její výpadek nesmí ovlivnit výstupy. Při implementaci atomický zápis celého souboru (dočasný soubor, `fsync`, přejmenování), soukromá oprávnění, zachování po restartu a zahrnutí do záloh; úprava seznamu cest Resticu jen po schválení. Schéma SQLite v1 se nemění a nový databázový subsystém nevzniká.

### Převzetí RGB od Pironmanu (M7)

- Pironman zůstává v provozu a má RGB pod svou kontrolou až do samostatně schváleného bezpečného převzetí. Ovladač RGB je ve výchozím stavu vypnutý a zapne se až po ověřeném vypnutí RGB Pironmanu jeho vlastní konfigurací, bez zastavení `pironman5.service`, který dál řídí boční ventilátory. Druhého zapisovatele na SPI NexusSTAT spolehlivě nepozná, proto rozhoduje postup, ne detekce.
- Před převzetím read-only ověřit, jak Pironman LED ovládá (zařízení, zapojení, konfigurace). Bez schválení žádná změna `config.txt`, overlayů, oprávnění ani služeb.
- **Provozní omezení:** ovladač zhasne LED při inicializaci a korektním vypnutí, ale WS2812 po pádu procesu drží poslední barvu a pouhé ukončení aplikace to neodstraní. Svit LED po pádu NexusSTAT proto nic nedokládá; hardwarový watchdog ani další elektronika se nenavrhují.
- Návrat: vypnout RGB v NexusSTAT a vrátit konfiguraci RGB Pironmanu ze zálohy.

## Implementováno lokálně (fáze 1: simulace, read-only kolektory, pravidelné měření a první prahy)

Stav k 2026-10-23 (M4-1): jen lokální kód a testy na vývojovém počítači (x86_64, Linux Mint). Systémový, síťový a ventilátorový kolektor čtou skutečná data notebooku (ventilátor dává `N/A`, notebook žádný v hwmon nemá), `alarms.py` je vyhodnocuje podle zapnutých prahů (výchozí vypnuto) a smyčka `run.py` je pravidelně vypisuje do terminálu; OLED je pouze obrazová simulace a se smyčkou zatím není propojen. Nic z toho nebylo spuštěno na Raspberry Pi ani proti skutečnému hardwaru displeje. Historie (`history.py` schéma M3.1a, synchronní zápis M3.1b, agregace M3.2a, retence M3.2b; vlákno `history_worker.py` M3.3a) se od M3.3b-1 dá na notebooku volitelně napojit na smyčku (`history_link.py`, sekce `[history]`, výchozí stav vypnuto, jen role `development` na výslovně zadané cestě); bez té sekce se nic neukládá. Automatická agregace a retence (M3.3b-2) a snímky (M3.4) jsou hotové lokálně, STORAGE zůstává plánovaná; RGB a potvrzování závad jsou jen schválený návrh.

```text
run.run_loop  (monotónní plán po interval_seconds, výpis do terminálu)
      |
      v
run.Collectors (live_monitor)   selhání jednoho kolektoru = UNKNOWN jen jeho kontrol
  |-- system_monitor.SystemMonitor    (read-only: /proc/stat, /proc/meminfo,
  |                                     /sys/class/thermal, os.statvfs("/"))
  |-- network_monitor.NetworkMonitor  (read-only: /sys/class/net, /proc/net/route,
  |                                     ioctl SIOCGIFADDR bez odesílání)
  |-- fan_monitor.FanMonitor          (read-only: /sys/class/hwmon,
  |                                     /sys/class/thermal/cooling_device*, M4-1)
  nebo fiktivní CheckResult (testy, simulátor)
                |  CheckResult: INFO + measurement/details, nebo UNKNOWN bez hodnoty
                v
alarms.AlarmEvaluator          (prahy [alarms]; mění jen state)
                |
                v
CheckResult  ->  core.active_problems / core.overall_state
                                   |
stisk / update(now)     ->  oled.OledController (SLEEP / CAROUSEL / STATUS)
                                   |
                            render.render_screen  ->  1bit obrázek 128 × 64 (Pillow)
                            (texty: oled_text, anglicky podle id a hodnoty)
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
- `core.py` (v0.6): nové volitelné pole `CheckResult.details`, slovník krátkých textů pro údaje, které nejsou číslo (název rozhraní `interface`, stav linky `link`, adresa `ipv4`). Jen tisknutelné ASCII bez prázdných hodnot, po vytvoření neměnné (`MappingProxyType`). Displej je čte místo `summary`, prahy je nečtou. Expirace (`effective()`) je smaže spolu s `measurement`, takže prošlý výsledek neukáže staré rozhraní ani adresu; `N/A` je ponechá. `UNKNOWN` je mít smí (víme, které rozhraní měříme, jen ne hodnotu).
- `fan_monitor.py` (M4-1, read-only): `FanMonitor` vrací při každém `collect()` **jeden** výsledek `system.cpu_fan` s `details` `fan` = `ON`/`OFF`, `source` = `rpm`/`request` a `device` = jméno zařízení. S tachometrem nese `measurement` v `RPM` (doplňující číslo), bez tachometru nemá žádné číslo a stav je jen z požadavku řízení (`source = request`). Zdroj se hledá při každém měření znovu podle identity zařízení (`/sys/class/hwmon/hwmon*/name`, `fanN_label`), nikdy podle čísla `hwmonX`; požadavek z `pwmN`, jinak z `/sys/class/thermal/cooling_device*` typu `pwm-fan`, a používá se jen na rozhodnutí „žádá řízení ventilátor?“ (žádné procento se dál nepředává). Čisté funkce (`find_cpu_fan`, `other_fans`, `parse_rpm`, `parse_pwm_percent`, `parse_cooling_percent`, `find_fan_cooling_device`) jsou testované nad syntetickými adresáři. Nic nezapisuje a ventilátor neřídí: chlazení dál řídí Linux.
- `network_monitor.py` (v0.6): `NetworkMonitor` vrací při každém `collect()` čtyři výsledky: `network.link` (`details` `interface`, `link` = `UP`/`DOWN`), `network.rx_rate` a `network.tx_rate` (`measurement` v `B/s`), `network.ipv4` (`details` `ipv4`; bez klíče = rozhraní platně nemá IPv4). Všechny nesou `interface`. Čisté funkce (`parse_default_routes`, `choose_interface`, `read_link_state`, `counter_rate`) jsou testované nad vzorovými soubory.
  - Výběr rozhraní při každém měření: `[network] interface` přesně (i virtuální, např. most), jinak fyzické rozhraní (má odkaz `/sys/class/net/<název>/device`) s aktivní výchozí trasou IPv4 z `/proc/net/route` s nejnižší metrikou, pak fyzické s linkou UP, pak jakékoli fyzické; při shodě abecedně. Loopback, Docker (`docker0`, `br-*`, `veth*`), VPN (`tun*`, `wg*`) a soubor `bonding_masters` odkaz `device` nemají. Název mimo `[A-Za-z0-9_.-]{1,15}` se přeskočí. Výchozí trasa přes VPN v hlavní tabulce se ignoruje (VPN není fyzická); provoz VPN a kontejnerů se do fyzického rozhraní započítá jen tak, jak skutečně prošel kabelem nebo Wi-Fi, nic se nesčítá.
  - Rychlost: rozdíl čítačů `rx_bytes`/`tx_bytes` ÷ rozdíl monotónního `now` mezi dvěma `collect()` (skutečně uplynulý čas, ne nominální interval). Identita vzorku je (název, `ifindex`): jiný název nebo znovu vytvořené rozhraní = „změna rozhraní“. `UNKNOWN` i pro první vzorek, pokles čítače („reset čítače“, také přetečení 32bitového čítače), nulový interval, mezeru delší než platnost výsledků („mezera v datech“) a nečitelný čítač; každý směr zvlášť. Chybějící rozhraní nebo žádné fyzické rozhraní vynuluje předchozí vzorek.
  - Linka z `operstate` (`up` → UP; `down`, `lowerlayerdown`, `notpresent`, `dormant` → DOWN; `unknown` → `carrier`; `testing` a cokoli jiného `UNKNOWN`). IPv4 přes `ioctl(SIOCGIFADDR)` na nepřipojeném socketu `AF_INET`/`SOCK_DGRAM`: socket se nikam neváže, nic neposílá a hned se zavře; `EADDRNOTAVAIL` = bez adresy, jiná chyba (např. `ENODEV`) `UNKNOWN`, odpověď se ověří jako tečková IPv4. Zamítnutá varianta: `/proc/net/fib_trie` neumí přiřadit adresu rozhraní, když jsou dvě rozhraní (např. eth0 a wlan0 na RPi) ve stejné podsíti. Odchozí „připojení“ kvůli zjištění adresy se nepoužívá.
  - `simulated=False` jen se skutečným `/sys/class/net`, `/proc/net/route` a skutečným dotazem na IPv4. Omezení: jen primární IPv4, bez IPv6; most nebo VLAN s adresou na virtuálním rozhraní automatický výběr nenajde (fyzický port ukáže `NONE`), řeší se nastavením `interface`.
- `run.py`: jediná hlavní smyčka `run_loop()` v jednom vlákně. Od v0.6 dostává `Collectors(SystemMonitor, NetworkMonitor)` z `live_monitor(config)`: výsledky obou kolektorů v jednom řádku, výjimka jednoho kolektoru udělá `UNKNOWN` („měření selhalo (Typ)“) jen z jeho kontrol (`failed_results` podle `monitor.labels`), Ctrl+C prochází. Hodiny, `sleep`, kolektor i výstup jsou parametry, takže testy běží se simulovaným časem. Od M3.3b-2 nese selhání kolektoru strukturovaný údaj `details["failure"]` (jméno výjimky), takže se v historii pozná od platného měření bez hodnoty; stav zůstává `UNKNOWN` a výpis i prahy se nemění. Od M3.3b-1 má dva volitelné parametry: `history_link` (napojení historie, `history_link.py`) a `stopper` (`Stopper`: obsluha SIGINT a SIGTERM jen v hlavním vlákně, vyvolá `KeyboardInterrupt`, takže ukončení jde stejnou cestou jako dosud Ctrl+C; během dopisování front další signál smyčku nepřeruší). Bez obou je chování shodné s v0.6.
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
- `oled.py`: čistá stavová logika bez kreslení. Kolotoč má od v0.6 pět stránek `NEXUS`, `SYSTEM`, `DISK`, `NETWORK`, `SERVICES` (názvy jsou zároveň nadpisy na displeji); časování (10 s na stránku, okno 300 s, nečinnost STATUS 30 s) se počítá z počtu stránek, takže pátá stránka okno neprodlužuje (300 s = přesně 6 cyklů) a po ní následuje znovu NEXUS. Texty stránek tu nejsou (od v0.5 v `oled_text.py`). OLED výstupy dostávají výsledky doplněné `services.complete_service_results()`, takže sledovaná služba bez dat je `UNKNOWN` stejně na SERVICES, v symbolu `?` na NEXUS i ve STATUS (společné vyhodnocení přes `effective()`); potvrzené WARNING/ERROR mají přednost. Čas dostává jako parametr `now` (v provozu `time.monotonic()`, v testech libovolná čísla). Stránka kolotoče se počítá z času od začátku okna, takže automatické přepínání okno neprodlužuje. Při návratu ze STATUS po nečinnosti začíná nové okno okamžikem vypršení, nikoli až pozdním voláním `update()`. Stisk nejdřív uplatní časové limity, takže stisk po uplynutí okna displej probudí a neotevře STATUS. `home_symbol()` určuje symbol stránky NEXUS: trojúhelník (`WARNING/ERROR/CRITICAL`) má přednost před `?` (`UNKNOWN`, žádná data nebo jen `N/A`).
- `oled_text.py` (v0.5): prezentační vrstva, krátké anglické texty displeje. Vstupem je jen `id`, stav a `measurement`; text `summary` (česky, pro terminál) se nečte, takže jeho jazyk ani formát displej neovlivní. Výsledky jen čte, diagnostiku, prahy ani id nemění. Není to lokalizační framework: dva slovníky a pár funkcí.
  - `CHECK_NAMES`: id → název (`system.cpu_usage` CPU, `system.cpu_temp` CPU TEMP, `system.memory` RAM, `disk.root` ROOT, služby z `services.SERVICE_LABELS`, plánované kontroly ze simulace, např. `system.time_sync` TIME SYNC, `disk.root_readonly` ROOT MODE). Názvy jsou různé, aby dva řádky STATUS nevypadaly stejně. Neznámé id: `CHECK` a kód `check_code()` = první 4 znaky SHA-256 id velkými písmeny (např. `docker.container_restarts` → `CHECK 0D51`). Kód je stejný v každém běhu i na každém stroji (ne `hash()`), takže jde dohledat: `PYTHONPATH=src python3 -c "from nexusstat.oled_text import check_code; print(check_code('<id>'))"`. Samotné id se na displej nepřepisuje (může být česky nebo dlouhé). Při shodě kódů dvou neznámých id (1 : 65 536) se id přidá do `CHECK_NAMES`.
  - `FAULT_TEXT`: hláška potvrzené závady bez číselné hodnoty, jen u id s jednoznačným významem: `service.nas` = dostupnost (`NAS OFFLINE`), `service.backup` = stáří poslední zálohy (`BACKUP OVERDUE`), `TIME UNSYNCED`, `ROOT READ-ONLY`. Jiný druh závady (např. zaplnění NAS) má dostat vlastní id, ne jiný text. Ostatní závady bez hodnoty ukážou jen název; závažnost je ve sloupci stavu.
  - `measurement_text()`: hodnota zaokrouhlená `:.0f` stejně jako `summary` kolektoru, bez mezery před jednotkou (`48°C`, `24%`); z jednotky jen ASCII a `°`.
  - `network_rows()` (NETWORK, v0.6): `(název, hodnota)` čtyř řádků: rozhraní velkými písmeny (bez platného výsledku linky `LINK`) a `UP`/`DOWN`, `RX` a `TX` přes `measurement_text()`, `IPV4` s adresou z `details`, `NONE` u platného výsledku bez adresy, jinak `UNKN`. `rate_text()`: jednotky `B/s`, `KB/s`, `MB/s` desítkově (1 KB = 1000 B), nejvýš 3 platné číslice, jednotka se volí až po zaokrouhlení (999,6 B/s → `1.0 KB/s`), nad 999 MB/s zůstává MB/s. `measurement_text()` převádí `B/s` touto funkcí. Názvy pro STATUS: `LINK`, `NET RX`, `NET TX`, `IPV4` (nejvýš 6 znaků, aby se vešlo `NET RX NO DATA`).
  - `page_rows()` (SYSTEM, DISK): `(název, hodnota)`; bez platné hodnoty text stavu, tedy chybějící, prošlá nebo neměřitelná hodnota `UNKN`, nikdy staré číslo ani `N/A`. `service_rows()` (SERVICES, přesunuto z `oled.py` beze změny logiky): `(název, OK/INFO/WARN/ERR/CRIT/UNKN/N/A)`, prošlý výsledek `UNKN`, chybějící služba `UNKN`.
  - `status_texts()` (STATUS): popisy od nejúplnějšího po samotný název. Hodnota (`CPU TEMP 88°C`), u `UNKNOWN` `NO DATA` nebo `STALE` (prošlá data se poznají z `expires_monotonic`, který `effective()` nemění, ne z textu), u závady bez hodnoty `FAULT_TEXT` nebo jen název. `STALE` se nepoužívá pro starou zálohu (ta je `BACKUP OVERDUE`), aby se nepletla neznámá data se závadou.
- `render.py`: jen kreslí podle stavu automatu. Písmo je přiložené bitmapové X11 „Fixed“ (`src/nexusstat/fonts`, volné dílo, původ a SHA-256 v `fonts/README.md`): `8x13B` pro nápis NEXUS, nadpisy stránek, hodiny (2×), hodnoty a stavy CRIT/WARN/OK…, `6x13` pro názvy, popisy ve STATUS, datum a značku `SIM`. Znaky jsou navržené pixel po pixelu, takže na 1 bitu zůstávají ostré. Displej od v0.5 používá jen ASCII velkými písmeny, `°` a `…`; všechny tyto znaky v obou písmech existují a navzájem se liší (test). Užší tučná 6x13B a 7x13B nejsou použitá, protože v nich splývá „M“ a „W“ s „H“. Písmo se načte z přiloženého `.pcf.gz` do paměti (FreeType by gzip při každém skoku rozbaloval znovu a Pillow by při chybějícím souboru potichu dohledal stejné jméno v systému); chybějící soubor je chyba, ne náhradní vzhled. Všechny rozměry vycházejí ze skutečně vykreslených pixelů (`ink_box`: text se vykreslí a změří, metriky písma obsahují i prázdné řádky buňky), souřadnice jsou celá čísla.
  - NEXUS: nápis, hodiny `HH:MM` (8x13B zvětšené 2× metodou NEAREST, 20 px) a datum jsou každý vodorovně vystředěné na celou šířku displeje (`(128 − šířka) // 2`, rozdíl okrajů nejvýš 1 px); symbol vpravo nahoře ani `SIM` vystředění neovlivní. Svisle pásma inkoustu 2–11, 21–40 a 50–59 px s mezerami 9 px. `SIM` je vpravo v řádku data, nejdelší datum (12 znaků) končí nejméně 4 sloupce před ní.
  - Záhlaví SYSTEM, DISK, SERVICES a STATUS: nadpis 8x13B vlevo, `SIM` vpravo (u STATUS vlevo od čítače s 6 volnými sloupci), čára na y = 13. SYSTEM a DISK: až 3 řádky po 15 px, název 6x13 vlevo, hodnota 8x13B vpravo (hodnoty pod sebou zarovnané jako stavy na SERVICES; hodnota nejvýš 72 px, název se zkrátí až před ní s 4 volnými sloupci). SERVICES: 4 řádky po 12 px, název vlevo, stav tučně vpravo. STATUS: 4 řádky po 12 px, stav 8x13B, popis 6x13 od x = 36, nejvýš 15 znaků; renderer (`first_fitting`) vybere první popis z `status_texts()`, který se vejde celý, a výpustku „…“ (bez mezery před ní) použije až u příliš dlouhého názvu. Text zarovnaný vpravo končí ve sloupci 126.
  - NETWORK (v0.6): 4 řádky po 12 px jako SERVICES, název 6x13 vlevo, hodnota vpravo s odstupem aspoň 4 sloupců. Linka a RX/TX tučně 8x13B (nejvýš 72 px), IPv4 obyčejným 6x13 (nejvýš 96 px), protože 15 znaků adresy tučně (120 px) by se vedle názvu nevešlo. Vejde se 15znakový název rozhraní vedle `DOWN`/`UNKN` i `255.255.255.255` bez výpustky (test).
  - Bez sledovaných dat (žádné výsledky nebo jen `N/A`) STATUS zobrazí `NO MONITOR DATA`, při platných datech bez závad `NO ACTIVE ISSUES` (ne „vše OK“, protože hodnoty `INFO` nejsou vyhodnocené prahy); zpráva je vystředěná. Obrazovky se simulovanými výsledky nesou značku `SIM`; skutečná měření ji nemají.
- `history.py` (M3.1a, jen infrastruktura schématu, nenapojeno): `create_database()` vytvoří databázi schématu v1 (`SCHEMA_SQL` = DDL z `HISTORY.md`) jen na výslovné cestě v existujícím adresáři a jen s rolí `test` nebo `development`; nikdy nepřepíše soubor ani zbylé `-wal`/`-shm`/`-journal`, staví v dočasném souboru a zveřejní ho `os.link`, při chybě maže jen vlastní dočasné soubory. `open_writer()` ověří hlavičku bez SQLite, vezme zámek `flock` (`<jméno>.lock`, druhý zapisovač i z jiného procesu dostane chybu), otevře `mode=rw` (soubor nevytvoří), ověří `application_id`, `user_version`, otisk schématu a `meta` (role, stroj) a nastaví spojení podle `HISTORY.md` 5.7. `open_reader()` otevře `mode=ro` s `query_only` a `NO_CKPT_ON_CLOSE`. Zapisovač ověří identitu nejdřív na spojení jen pro čtení (odmítnutí nic nezapíše ani checkpointem), vytvoření synchronizuje soubor před a adresář po zveřejnění a `open_writer` odstraní druhé jméno souboru po pádu mezi zveřejněním a úklidem. Chyby jsou `HistoryError` s kódem důvodu.
- `history.py` (M3.1b, synchronní zapisovač, nenapojeno): `HistoryRecorder` nad `open_writer()` zahájí běh (uzavře nečisté běhy, horní mez, `process.start`), skládá dávky (`begin_batch`, `add_results`, `add_event`) a zapíše každou jednou transakcí s `batch-guard` (`run.last_batch`); vzorky jen z katalogu `SERIES` (RX/TX podle rozhraní, `UNKNOWN` = `NULL`) přes pravidla přijetí `SampleGate`, každou změnu stavu a `details` přes `ChangeTracker` (R4), konflikt nikdy nepřepíše uloženou hodnotu, simulovaná data a nedůvěryhodný čas se nezapíšou a započtou (`history.rejected`). Kvalitu času dodává volající; vlákno, fronty a volání z `run.py` jsou M3.3 (`HISTORY.md` 11.1).
- `history.py` (M3.2a, agregace, nenapojeno): `HistoryRecorder.aggregate(now_utc, ts_quality, queue)` na stejném zapisujícím spojení uzavře okna podle `HISTORY.md` 6.1 (rezerva 120 s, stav fronty `QueueState` a otevřená dávka, synchronizovaný čas, nepředběhnout zapsaná data) a v jedné transakci zpracuje nejvýš hodinu 5min oken (`progress-5m`, `aggregate-5m`) a jednu hodinu `agg_1h` (`progress-1h`, `rollup-1h`). Hotové okno se nepřepíše: konflikt zapíše `history.aggregation_conflict` a agregaci zastaví. `SampleGate` převezme hranici až po potvrzení. Volání po dávce zařídí vlákno M3.3 (`HISTORY.md` 11.2).
- `history.py` (M3.2b, retence, nenapojeno): `retention_plan()` (dry-run, i nad spojením jen pro čtení) a `HistoryRecorder.retain(now_utc, clock)` na stejném zapisujícím spojení. Hranice podle `HISTORY.md` 7 (`sample` 30 dní a pod `done_until '5m'`, `agg_5m` 365 dní a pod `done_until '1h'`, `agg_1h` 10 let, `check_change` a `event` 730 dní), mazání `<`; každá transakce nejvýš hodinu jedné tabulky a znovu ověří čas (`ClockState` od M3.3, stabilita ≥ 24 h, žádný `clock.jump` za 24 h), invarianty agregací a `rowcount`; nejvýš 24 transakcí za běh, tabulky se střídají; trvalá hranice `retained_since.*` v `meta` (začátek zachované historie, ne záruka souvislých dat); po retenci epizody s neznámým začátkem `start_ts = NULL` a dotaz `retention-unknown`. Žádný `VACUUM` (`HISTORY.md` 11.3).
- `history_worker.py` (M3.3a, vlákno zapisovače; napojení M3.3b-1): `HistoryWorker` vlastní jediné vlákno, které otevře (`open_writer`, `flock`), používá a zavře spojení SQLite (`check_same_thread` zůstává zapnuté); hlavní vlákno jen předává (`submit`, `submit_event`), čte `status()` a volá `flush`/`stop` a nikdy nečeká na I/O. Změny stavu (R4) se počítají už při předání. Omezené fronty: hodina měření (nejvýš 72 000 hodnot), hodina trvalého kmitání 20 kontrol a 500 událostí po nejvýš 16 KiB; přetečení zahodí nejstarší a ztrátu zapíše další dávka (`history.gap`, `history.changes_dropped`, `history.events_dropped`). Dávka po 60 s, změny a události hned; přechodné chyby (`BUSY`, `FULL`, `IOERR`, ztráta práva, zámek) s odstupem a znovuotevřením ve stejném běhu, nevratné (poškození, cizí soubor, `IntegrityError`) zastaví zápis bez nové databáze. Před synchronizací hodin data čekají a dostanou odvozený čas (`derived`). `stop()` s limitem 10 s nepředstírá ukončení zaseknutého I/O (`HISTORY.md` 11.4). Od M3.3b-2 vlákno samo spouští i údržbu: agregaci po potvrzené dávce (jinak nejvýš jednou za minutu) a retenci nejvýš jednou za hodinu, obojí až po kontrole, že nečeká zápis a není nepotvrzená ani nejistá dávka. Stav fronty pro uzavírání oken se hlásí jako prázdný jen tehdy, když to lze doložit, a retence dostane doloženou dobu souvislé synchronizace hodin — jen z průběžných pozorování (po startu nulová, takže se prvních 24 h nemaže; mezera v měření, skok času i nezapsaná ztráta události ji vynulují). Přechodné selhání údržby zápis měření nezastaví (`HISTORY.md` 11.6). Od M3.3b-1 má každý předaný výsledek strop textu `MAX_RESULT_BYTES` = 512 B UTF-8 (id, `summary` a `details`): delší text se zkrátí (objekt řady v `details` má přednost), identita kontroly, stav ani naměřená hodnota se nezahodí a zkrácení se započítá a zapíše jako `history.result_truncated` (`WARNING`, není to ztráta měření). Tím má paměť front pevnou mez v bajtech, ne jen v počtu položek.
- `history_link.py` (M3.3b-1, napojení na smyčku): `open_history(config)` podle sekce `[history]` postaví `HistoryWorker` (role vždy `development`, otisk stroje z `/etc/machine-id`, `boot_id` read-only z `/proc`) a vrátí `HistoryLink`; vypnutá historie dá `None` a smyčka se chová jako v0.6. `record()` předá smyčkou vyhodnocené výsledky se skutečným monotónním časem měření (nikdy nevyhodí výjimku do smyčky), `check()` vrátí kontrolu `history.storage` ze snímku `WorkerStatus` (včetně stavu údržby: konflikt agregace `ERROR`, selhání údržby, zastavená retence, velikost WAL a zpoždění agregace `WARNING`, blokovaná retence jen jako text) (`N/A` vypnuto, `UNKNOWN` zapisovač ještě neotevřel databázi, `INFO` zapisuje, `WARNING` přechodná chyba / čekání na hodiny / neznámý stav hodin / zkrácený text, `ERROR` ztráta dat, plná fronta nebo zastavený zapisovač; `CRITICAL` nikdy), `stop()` dopíše fronty a vypíše jen doložené skutečnosti. Kontrola `history.storage` se do historie **nepředává** (jinak by zápis sám vytvářel další zápisy) a její `details` nesou jen ustálené názvy stavů. Úložiště se ověřuje při každém otevření (a znovu po otevření) podle `/proc/self/mounts` nad cestou rozvinutou přes `realpath`: odmítne se síťový souborový systém (NAS, CIFS, NFS, 9p, sshfs i bind mount z nich), `autofs` i stav, kdy úložiště ověřit nelze. Databáze se nikdy nevytváří ani nepřepisuje. Zaseknutý zápis (visící I/O stav zapisovače nezmění) se pozná podle dávky nepotvrzené déle než 180 s, kdy data čekají.
- `history_cli.py` (M3.4b-2, příkazy správce): `python3 -m nexusstat.history info SOUBOR` (jen čte: identita, role, schéma, nevratný otisk stroje, značka obnovy, evidence převzetí, integrita, poslední běh, počty, závěr o otevření a o potřebě převzetí), `adopt SOUBOR --output NOVÝ` (ruční převzetí obnovené databáze: vstup beze změny, SHA-256 před i po kopii, výsledek jako nový soubor bez přepsání cíle, nezávislé ověření) a `generations ADRESÁŘ` (read-only dry-run retence generací). `info` čte běžným spojením jen pro čtení (zamyká a pozná čekající rollback); `immutable=1` jen na výslovný přepínač `--immutable`, nikdy podle absence `-wal`. Logika a bezpečnostní podmínky jsou v `history.py` (`database_info`, `adopt_database`) a ve `snapshot.py` (`retention_candidates`); CLI jen zpracuje argumenty a vypíše výsledek (`HISTORY.md` 11.9).
- `snapshot_plan.py` (M3.4b-1, denní plán): `DailyPlan` spočítá plánovaný okamžik v UTC z místního času `"HH:MM"` a **výslovného** časového pásma (`zoneinfo`; přechody času podle pravidel pásma), `SnapshotPlanner` rozhodne, zda o snímek požádat — nejvýš jeden pokus na plánovaný okamžik, dohání se jen dnešní zmeškaný čas a existující novější snímek plán vyřizuje i po restartu. Čistá časová logika bez I/O a bez vlákna; plán vyhodnocuje měřicí smyčka svým běžným tikem (`HistoryLink.plan_snapshot`), takže nevzniká žádné aktivní dotazování (`HISTORY.md` 11.8).
- `snapshot.py` (M3.4a, generace snímků): `create()` postaví snímek přes `sqlite3.Connection.backup()` ze spojení vlákna zapisovače do rozpracované generace mimo zálohovaný adresář, převede ho na samostatný soubor (`journal_mode=DELETE`, `meta.snapshot_utc` jen v kopii), úplně ověří (integrita, cizí klíče, identita, schéma, počty řádků, velikost, SHA-256), zapíše JSON záznam, porovná ho se souborem a celou **neměnnou generaci** zveřejní **jediným přejmenováním adresáře** (`publish()` přes `renameat2(RENAME_NOREPLACE)`, takže nenahradí žádný existující cíl — ani prázdný adresář — a bez této záruky úložiště se nezveřejní nic); dvě samostatná přejmenování souborů atomické zveřejnění dvojice nejsou. `latest_valid()` vybere poslední prokazatelně platnou generaci, `check_restore()` je test obnovitelnosti jen pro čtení a `cleanup_work()` maže jen vlastní rozpracované zbytky. Žádný nový zapisovatel ani vlákno; kopii lze při ukončení přerušit na hranici stránek (`HISTORY.md` 11.7).
- `storage.py` (M3.4a, kontroly cest): rozvinutí symlinků, typ souborového systému z `/proc/self/mounts`, odmítnutí síťového úložiště, `autofs` i neověřitelného stavu, existující skutečný adresář, shodný souborový systém a `fsync` souboru i adresáře. Používá ho zapisovač i snímky.
- `clock_sync.py` (M3.3b-1, read-only stav hodin): `ClockSync.synced()` zkouší zdroje v pořadí důvěryhodnosti a vrací `True` (doloženo), `False` (prokazatelně nesynchronizováno) nebo `None` (nezjištěno). `kernel_sync()` používá `adjtimex` s `modes = 0` (čistý dotaz) a rozhoduje podle kernelové meze chyby hodin pod 16 s (`NTP_PHASE_LIMIT`) — stejné kritérium jako `NTPSynchronized` v systemd; samotný příznak `STA_UNSYNC` použít nejde, systemd-timesyncd ho nechává nastavený i při správné synchronizaci. Jiný zdroj aktuálního stavu se nepoužívá: `timesyncd_first_sync()` čte `/run/systemd/timesync/synchronized`, ale ten dokládá jen *dřívější* synchronizaci v tomto bootu (po ztrátě nemizí), takže o aktuálním stavu nerozhoduje a slouží jen jako doplňující informace do výpisu (`first_sync_note()`). Chyba, nevěrohodná odpověď i chybějící zdroj dají vždy `None`, nikdy `True`; správný kalendářní rok se za důkaz nepovažuje. `read_boot_id()` čte `/proc/sys/kernel/random/boot_id` (neplatný údaj = `None`); boot_id patří jen do lokální databáze, ne do dokumentace.
- `services.py`: katalog služeb pro stránku SERVICES (id `service.nexus`, `service.animedb`, `service.nas`, `service.backup` a krátké názvy). Nesbírá žádná data. `complete_service_results()` doplní služby z katalogu, pro které nepřišel výsledek: nenasazená AnimeDB `N/A` („AnimeDB není nasazena“), ostatní `UNKNOWN` („<název>: bez dat“), vše `simulated=False` a bez číselné hodnoty; výsledky, které přišly (i simulované), nemění. Rozdíl: `UNKNOWN` = sledované, ale aktuální stav neznáme (problém ve STATUS, symbol `?`); `N/A` = záměrně vypnuté nebo nenasazené (žádný alarm, nevyprší). NEXUS zatím nemá definovaný healthcheck, proto je `UNKNOWN`, nikdy `OK`. Terminálová smyčka `run.py` služby zatím nevypisuje (jen systémová měření a prahy). Budoucí kolektory (služby Nexusu, AnimeDB, NAS, Restic) budou vracet výsledky se stejnými id.
- `simulate.py`: fiktivní data scénáře odpovídají ukázce SERVICES (NEXUS, ANIMEDB, NAS `OK`, BACKUP `WARN`); systémové hodnoty a teplota NVMe mají `measurement` jako skutečný kolektor; dřívější fiktivní problémy NAS (CRITICAL), zálohy (ERROR) a N/A AnimeDB byly nahrazeny jinými fiktivními položkami, aby si obrazovky neodporovaly. Scénář projde všechny čtyři stránky, vrátí se na NEXUS a dál ukazuje dvě stránky STATUS. `--live` doplní k měření služby přes `complete_service_results()`.
- `preview.py` (v0.5): živý simulátor OLED v okně tkinter, jen pro notebook. Obraz je výhradně `render_screen()` (žádná druhá grafická implementace), zvětšený celočíselně metodou NEAREST a předaný do `tkinter.PhotoImage` jako PNG (bez `PIL.ImageTk`).
  - `PreviewSession` je stavová logika bez tkinter: scénář, výsledky, `OledController` a čas. `SessionClock` je jediná časová základna pro controller, platnost výsledků i vykreslení; zrychlení 1×/5×/10× se při změně přepočítá bez skoku. Hodiny na stránce NEXUS i UTC čas fiktivních výsledků se z ní odvozují.
  - Fiktivní systémové hodnoty mají `measurement` jako skutečný kolektor, protože displej ukazuje číslo, ne popis. Popisek režimu v okně používá názvy stránek z displeje (SERVICES).
  - Fiktivní scénáře se obnovují v intervalu kolektoru jako skutečná měření (`simulated=True`, platnost 3 × interval), takže při dlouhém běhu nevyprší; položky `stale_after` jsou změřené jen při startu scénáře a ukazují expiraci. Všechny výsledky projdou `complete_service_results()`.
  - Síťové scénáře (v0.6): 7 linka DOWN (bez IPv4, 0 B/s), 8 bez IPv4 (linka UP), 9 nízký provoz (B/s), 10 vysoký provoz (MB/s), 11 neplatná a zastaralá data (RX `UNKNOWN` „reset čítače“, TX a IPv4 změřené jen při startu a po 20 s `UNKN`/STALE). Fiktivní rozhraní `eth0`, adresa `192.0.2.10` (RFC 5737). Ostatní scénáře mají platnou fiktivní síť, scénář 4 síť záměrně nemá (stránka celá `UNKN`).
  - LIVE: `live_monitor(config)` (`SystemMonitor` + `NetworkMonitor` s `[network]`) + `AlarmEvaluator` v intervalu `[collector]`, rychlost vynucená 1×, čas měření ze skutečného UTC; selhání kolektoru = `UNKNOWN` jako v `run.py`.
  - Okno: obnova přes `after()` po 100 ms, nový obrázek jen při změně, klávesy mezerník/Enter, ovládací prvky bez fokusu (mezerník nestiskne nic dalšího), zavření zruší naplánovanou obnovu; Ctrl+C zavře okno stejně. tkinter se importuje až při otevření okna, takže testy běží bez X serveru; zapojení okna ověřuje test s falešným modulem tkinter.
- `config.py`: sekce `[oled]`, `[collector]` a od v0.6 `[network]` z TOML (`tomllib`) přes `load_config()`; validuje se vždy celý soubor, i sekce, kterou volající nepotřebuje. Časy jsou celé sekundy 1–3600, `page_seconds ≤ carousel_seconds`, neznámé klíče, jiné sekce a jiný rozměr než 128 × 64 jsou chyba. `[collector] interval_seconds` (výchozí 10 s); platnost výsledků `validity_seconds` = 3 × interval se samostatně nenastavuje, aby nemohla být kratší než interval. `[alarms.cpu_temp]` a `[alarms.disk_root]` (`AlarmRule`): `enabled` (výchozí `false`), `warning`, `error`, `hysteresis` bez výchozích hodnot (při zapnutí povinné, zadávají se všechny tři najednou), `confirm_count` 3 a `clear_count` 2 (celá čísla 1–60). Validace: konečná čísla, `warning < error`, `0 ≤ hysteresis < error − warning`, prahy v rozsahu teploty (0, 125] °C a disku (0, 100] %, `warning − hysteresis` nad dolní mezí (jinak by závada nikdy neodezněla). `[network] interface` (v0.6): bez klíče automatický výběr, jinak název 1–15 znaků `[A-Za-z0-9_.-]` (ne `.` a `..`); existence se ověřuje až při měření. Neimplementované volby (`home_title`, `clock_24h`, `[alarms] show_warning_triangle`, `[web]`, `[storage]`) jsou v ukázkové konfiguraci jen jako komentář a při zadání vyvolají chybu.
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
- Fáze 5: až po doloženém funkčním nahrazení zvážit ukončení původní služby a InfluxDB (u InfluxDB nejdřív read-only ověřit, zda ji nepoužívá jiná aplikace).
- Fáze 2–5 odpovídají milníku M7 níže; žádná z nich zatím není schválená.

## Zálohování

- Projektové zdrojáky budou v plánovaném `/srv/nexus/projects/nexusstat` a jejich pokrytí Resticem musí být reálně ověřené.
- Provozní SQLite (návrh M2, implementováno lokálně v M3, nenasazeno): lokální SSD, živá databáze v `/srv/nexus/appdata/nexusstat/history/`, snímky přes SQLite Backup API jako **neměnné generace** v `/srv/nexus/appdata/nexusstat/snapshot/<generace>/` (rozpracované v `snapshot-work/`), nikdy NAS/CIFS. Restic zahrne jen `snapshot/`, živou databázi s `-wal` a `-shm` vyloučí. Kontrola integrity snímku a test obnovitelnosti jsou implementované lokálně (M3.4); test obnovy na Nexusu a zápis do manifestu až s nasazením (`HISTORY.md`, sekce 9, postup v `OPERATIONS.md`).
- Nová aplikace NESMÍ sama měnit nebo spouštět Restic job bez schválení.

## Plán milníků (návrh k posouzení)

Pořadí a obsah jsou návrh správci. Milníky nejsou hotové a čísla verzí se jim přidělí až při schválení, nenavazují automaticky na v0.5.

| Milník | Obsah | Výstup | Mimo rozsah |
| --- | --- | --- | --- |
| M1 Uzavření v0.5 | posouzení anglického OLED a otevřených rozhodnutí v0.5, commit a push po schválení | uzavřená v0.5 v repozitáři | nové funkce |
| M2 Návrh SQLite | schéma (měření, agregace, alarmy, události, verze schématu), retence a agregace, odhad velikosti a zápisů pro skutečné veličiny, dávkování, WAL, záloha přes Backup API, napojení na Restic, test obnovy, chování při chybě a plném disku. Návrh v `HISTORY.md` s benchmarkem (`tools/`), **schválený 2026-10-20 kromě R11, R13, R15 (odloženo do M7)** | schválený návrh v dokumentaci | kód aplikace |
| M3 Lokální SQLite | zapisovač historie (`sqlite3`), zápis měření, přechodů alarmů a událostí, retence a agregace, izolace selhání, snímky; testy nad dočasnou databází se simulovaným časem, zkouška zálohy a obnovy na notebooku. Kroky M3.1a–M3.4b v `HISTORY.md`, sekce 11; **M3.1a (základ databáze) a M3.1b (synchronní zápis) hotovy lokálně 2026-10-20, M3.2a (agregace), M3.2b (retence) a M3.3a (vlákno zapisovače) 2026-10-21, M3.3b-1 (volitelné napojení na smyčku, stav hodin, SIGTERM, `history.storage`), M3.3b-2 (automatická agregace a retence z vlákna) a M3.4a (neměnné generace snímků, test obnovitelnosti) 2026-10-22, M3.4b-1 (sekce `[history.snapshot]`, denní plánování z měřicí smyčky, kontrola `history.snapshot`, auditní události, nalezení poslední platné generace při startu) a M3.4b-2 (rozpoznání obnovené databáze, příkazy `info`, `adopt` a `generations`, návrh retence generací) 2026-10-23; zbývá M3.4b-3 (mazání generací, jen po samostatném schválení)** | kód a testy na notebooku | Nexus, Restic, produkční data |
| M4 Kolektory CPU FAN a NETWORK | read-only čtení nad vzorovými soubory a LIVE na notebooku; read-only ověření zdrojů ventilátoru na RPi 5 jen po schválení. NETWORK hotovo lokálně ve v0.6, **M4-1 (kolektor CPU FAN a čtvrtý řádek SYSTEM) hotovo lokálně 2026-10-23** | kolektory s testy | řízení ventilátorů |
| M5 STORAGE a OLED | rozhodnutí STORAGE vs. ROOT pod SYSTEM, kontroly úložišť s timeoutem, simulátor a vizuální kontrola (SYSTEM se 4 řádky hotový lokálně v M4-1). Pětistránkový kolotoč se stránkou NETWORK hotov lokálně ve v0.6 (zatím s DISK místo STORAGE) | nové stránky v simulátoru | hardware displeje |
| M6 Web s historií | lokální web čte historii, autentizace; potvrzování závad a jas LED (schválený návrh 2026-10-23); port až po schválení firewallu; veřejný status odděleně | web na notebooku | veřejné nasazení |
| M7 RPi 5 a náhrada Pironmanu | testovací nasazení podle standardu Nexus, souběh s Pironmanem, převzetí funkcí po jedné (OLED, RGB, tlačítko), boční ventilátory až po ověřeném nezávislém řízení s fyzickým dohledem, rollback; InfluxDB až po ověření dalších uživatelů | ověřená náhrada | vše bez schválení |
