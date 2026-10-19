# NexusSTAT – požadavky v0.8

**Dokument je specifikací návrhu, nikoliv popisem hotového softwaru.** Požadavky vycházejí z dosavadního rozhodnutí správce projektu.

## Účel

Lokální diagnostika a stavová signalizace Nexusu (systém, NVMe, NAS, Restic, Docker, služby, později AnimeDB). Postupná náhrada nadbytečných aplikačních funkcí Pironman 5 bez ohrožení hardwarového chlazení. Žádná povinná cloudová závislost.

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
2. Režim **CAROUSEL** automaticky mění stránky každých **10 s**; celkově je aktivní **300 s** a následně OLED usne. Výchozí hodnoty jsou konfigurovatelné, později změnitelné přes LAN web.
3. Každý další krátký stisk během kolotoče otevře **STATUS**, což je přehled konkrétních závad pod sebou.
4. **STATUS bez závad nebo s jednou stránkou:** další krátký stisk vrací do CAROUSEL.
5. **STATUS s více stránkami:** další stisky listují po stránkách; ze **závěrečné stránky** další krátký stisk vrací do CAROUSEL (bez cyklení uvnitř STATUS).
6. Ve STATUS se po **30 s bez stisku** automaticky vrátíme do CAROUSEL; každý platný stisk resetuje jeho 30s časovač.
7. Každý návrat do CAROUSEL zahájí nové 300s okno od první stránky NEXUS. Automatické přepnutí stránky dobu neprodlužuje.
8. Aktivní WARNING/ERROR/CRITICAL signalizuje na první stránce malý obrysový trojúhelník s vykřičníkem; potvrzení alarmu jej neschová, dokud závada trvá. Pouze UNKNOWN bez potvrzené závady (včetně prošlých výsledků, žádných dat nebo jen N/A) signalizuje symbol `?`. Trojúhelník má přednost před `?`. Vše platně OK/INFO/N/A: bez symbolu.
9. OLED v běžném stavu není nuceně rozsvěcen pouze kvůli upozornění. Ovládání a uspání musí zůstat funkční i při chybách.
10. Vše nejprve testovat jako monochromatický bitmapový simulátor 128 × 64 bez hardware.
11. STATUS bez sledovaných dat zobrazí `NO MONITOR DATA`, bez aktivních problémů při platných datech `NO ACTIVE ISSUES`. Obrazovky se simulovanými daty nesou značku `SIM`.
12. Stránky SYSTEM a DISK ukazují hodnoty ze systémového kolektoru. Nedostupná hodnota se zobrazí jako text `N/A`, její diagnostický stav je `UNKNOWN`. Bez schválených prahů se z hodnot neodvozuje WARNING ani ERROR.

## Systémový kolektor (potvrzeno pro v0.2)

- Platná naměřená hodnota bez stanoveného alarmového prahu má stav `INFO`.
- STATUS bez aktivních závad zobrazuje `NO ACTIVE ISSUES`.
- První vzorek CPU bez dostatku údajů pro výpočet má stav `UNKNOWN`.
- Platnost výsledků 30 s je dočasná; později se naváže na interval pravidelného spouštění kolektoru.
- Rozsah teploty (0, 125] °C je prozatím jen kontrola věrohodnosti údaje, ne univerzální hardwarový limit ani alarmový práh.
- Chyba jedné kontroly nesmí ovlivnit ostatní kontroly.

## Tlačítko a kritické funkce

- Fyzický `pwr_button` je událost Linux input zařízení, nikoliv libovolný GPIO pin. Číslo zařízení `/dev/input/eventN` se může lišit a není trvalý kontrakt.
- Návrh krátkého/dlouhého stisku pro řízené vypnutí je **neuzavřený do HW testů**. Dlouhé 10s gesto reboot se **neimplementuje**, může kolidovat s hardwarovým vypnutím.
- Ovládání tlačítka při přechodu ověřit proti `systemd-logind` a přítomnosti exkluzivního `evdev` grabu.

## RGB / ventilátory

- 4 RGB WS2812: normálně vypnuté, chybové sekvence až po schváleném HW přechodu. Kód signálu nemá zakrývat důležitější alarm.
- Hlavní PWM ventilátor CPU ponechat stávající linuxové regulaci.
- Oba boční ventilátory nyní ovládá Pironman přes GPIO6 v režimu Always On (`gpio_fan_mode=0`); GPIO6 bylo při read-only kontrole `HIGH`. Kernelový gpio-hog je **návrh**, není nasazen ani fyzicky otestován.
- Nezastavovat Pironman, dokud není schválené a ověřené předání řízení ventilátorů a tlačítka.

## Web / přístup

- Lokální webový dashboard dostupný z LAN na konfigurovatelném portu (navržen `8765`), po schválení firewallu a autentizace.
- Funkce LAN: přehledné stavové karty, filtrování událostí, potvrzení/ztišení alarmů, konfigurace (včetně délky kolotoče 300 s, intervalu 10 s, nečinnosti STATUS 30 s).
- Na veřejném webu `mh42.cz` pouze sanitizovaný read-only celkový stav a případně stručný výpis závad. **Nikdy** celé logy, interní IP, administraci ani API pro zápis.
- Oddělit veřejnou projekci dat od administračního procesu už v návrhu; žádný automatický port forwarding nebo cloud.

## Jednoduchost a způsob vývoje

- Claude primární vývojář; Python + standardní knihovna, další balíčky jen s důvodem. Pro obrazový simulátor Pillow; pro budoucí OLED Adafruit SSD1306, pro RGB Adafruit NeoPixel SPI.
- Nevyužívat převzatý zdrojový kód SunFounder. Testy nejprve s fiktivními daty, produkční testy až po samostatném souhlasu.
- Počáteční milník: core + simulátor OLED + testy timeoutů, více stránek STATUS, upozornění, obnovy stavu, bez jakéhokoli přístupu k zařízení.
