"""Pravidelné read-only měření systému s výpisem do terminálu.

Použití:
  PYTHONPATH=src python3 -m nexusstat.run [--config SOUBOR]

Měří vytížení CPU, teplotu CPU, RAM, obsazenost /, režim a typ souborového
systému / (jen z /proc/self/mountinfo; ro je závada ERROR) a síťové rozhraní
([network]: linka, RX/TX, IPv4) a vyhodnotí zapnuté prahy ([alarms], výchozí
vypnuto).
Se zapnutým [services.animedb] (výchozí vypnuto) navíc jedním požadavkem GET
na nastavenou adresu /health zkontroluje dostupnost AnimeDB, se zapnutým
[services.nas] (výchozí vypnuto) jedním pokusem o spojení TCP na port SMB
dostupnost NAS (bez přihlášení, bez sdílených složek a mountů), se zapnutým
[services.backup] (výchozí vypnuto) jedním read-only dotazem `systemctl show`
výsledek existující zálohovací úlohy (bez spouštění zálohy a bez Resticu).
Od SCHED-2 tiká smyčka po 1 s, ale každý kolektor skutečně měří jen ve svém
termínu (Scheduled): síť RX/TX po 1 s, CPU, teplota a RAM po 5 s, ventilátor
po 10 s, rozhraní a IPv4 a režim / po 60 s, obsazenost / po 1800 s; služby AnimeDB po 15 min,
NAS po 30 min, zálohy podle plánu timeru (SCHED-1). Mezi termíny se ukazuje
poslední výsledek se svou původní platností. Historie se zapisuje a její
kontroly běží jen jednou za [collector] interval_seconds (výchozí 10 s) a řádek
výpisu vzniká jen při změně stavu, jinak nejvýš jednou za 30 min.
Ukončení: Ctrl+C nebo SIGTERM.
S přepínačem --web (M6-1) ukazuje poslední výsledky i lokální web jen pro čtení
na http://127.0.0.1:8765/ (web.py): jen z tohoto počítače, nic neměří. S --web-tls-cert
a --web-tls-key běží web přes HTTPS a s --web-admin-password nabídne na /admin
chráněné potvrzení závad a jas LED (M6-4B, admin.py); bez HTTPS administrace není.
Se zapnutou sekcí [heartbeat] (M6-5, výchozí vypnuto) posílá jednou za 300 s
podepsaný odchozí HTTPS heartbeat jen s celkovým stavem a časem (heartbeat.py).
Bez sekce [history] (výchozí stav) nic nezapisuje, neotevírá port, nepřistupuje
k displeji, mountům NAS ani jinému hardwaru a do sítě posílá jen zapnuté kontroly AnimeDB a NAS. Se zapnutou historií
([history] enabled = true s výslovnou cestou) předává vyhodnocené výsledky
vláknu zapisovače SQLite; samo měření zůstává read-only a chyba historie ho
nezastaví (history_link.py).
Alarm se jen vypíše; žádná automatická akce (vypnutí, ventilátory) neexistuje.
"""

import argparse
import math
import signal
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import __version__, history
from .admin import Admin, AdminError, load_password_file
from .alarms import AlarmEvaluator
from .animedb_monitor import AnimeDbMonitor
from .backup_monitor import BackupMonitor
from .config import (CPU_INTERVAL_SECONDS, DISK_INTERVAL_SECONDS, FAN_INTERVAL_SECONDS, HISTORY_ROLE,
                     LIVE_TICK_SECONDS, NETWORK_DETAILS_INTERVAL_SECONDS, NETWORK_RATE_INTERVAL_SECONDS,
                     ROOT_MODE_INTERVAL_SECONDS, VALIDITY_INTERVALS, Config, ConfigError, load_config)
from .core import ALERT_STATES, CheckResult, State, effective, overall_state
from .fan_monitor import FanMonitor
from .history_charts import HistoryCharts
from .heartbeat import Heartbeat, HeartbeatError, client_context, load_secret
from .history_link import open_history
from .mounts import RootMountMonitor
from .nas_monitor import NasMonitor
from .network_monitor import NetworkMonitor
from .system_monitor import CPU_TEMP_ID, CPU_USAGE_ID, DISK_ROOT_ID, MEMORY_ID, SystemMonitor
from .user_state import UserState
from .web import DEFAULT_PORT, HISTORY_FAILED, LOOPBACK, check_port, start_web, stop_web, web_access
from .web_tls import tls_context


# Klíč v details, kterým se selhání sběru odlišuje od platného měření bez hodnoty
# (docs/HISTORY.md 4.4). Hodnotou je jméno výjimky; stav zůstává UNKNOWN, takže
# prahy, výpis ani displej se nemění a nic nevypadá jako OK.
COLLECTOR_FAILURE = "failure"

# Nejdelší odstup řádků výpisu, když se nic významného nemění (SCHED-2): výpis
# nevzniká při každém taktu, ale změny se vypíší hned.
SUMMARY_SECONDS = 30 * 60


def failure_name(error):
    """Jméno výjimky jako krátký údaj ASCII pro details (nikdy nevyhodí chybu)."""
    name = "".join(character for character in type(error).__name__
                   if character.isascii() and character.isprintable() and character != " ")
    return name or "Exception"


def utc_now():
    return datetime.now(timezone.utc)


def print_now(line):
    # Vypsat hned, i když je výstup přesměrovaný do souboru nebo do `tee`.
    print(line, flush=True)


def failed_results(monitor, now, observed_utc, validity_seconds, reason="měření selhalo", failure=None):
    """Výsledky pro měření, které celé selhalo: všechny kontroly kolektoru UNKNOWN.

    Kontroly určuje `monitor.labels` (id → popisek). Předchozí hodnoty se
    nepodrží, i když by ještě byly platné: víme, že aktuální měření
    neproběhlo, a staré INFO/OK by vypadalo jako čerstvé. Stejně se kolektor
    chová při selhání jedné kontroly.
    `failure` je strukturovaný údaj o selhání sběru (jméno výjimky) v
    `details[COLLECTOR_FAILURE]`: v historii tak jde selhání kolektoru odlišit
    od platného měření, které jen nemá hodnotu. Stav zůstává UNKNOWN.
    """
    details = {} if failure is None else {COLLECTOR_FAILURE: failure}
    return [
        CheckResult(
            check_id, State.UNKNOWN, f"{label}: {reason}", observed_utc,
            now + validity_seconds, simulated=monitor.simulated, details=details,
        )
        for check_id, label in monitor.labels.items()
    ]


class Collectors:
    """Několik read-only kolektorů měřených společně, navenek jako jeden.

    Výjimka jednoho kolektoru nahradí UNKNOWN jen jeho vlastní kontroly;
    ostatní kolektory změří normálně. Chyba sítě tak nezneplatní měření CPU
    (a naopak), stejně jako chyba jedné kontroly uvnitř kolektoru.
    """

    def __init__(self, *monitors):
        if not monitors:
            raise ValueError("Collectors potřebuje aspoň jeden kolektor")
        validities = {monitor.validity_seconds for monitor in monitors}
        if len(validities) != 1:
            raise ValueError("kolektory musí mít stejnou platnost výsledků")
        self.monitors = monitors
        self.validity_seconds = validities.pop()
        self.simulated = any(monitor.simulated for monitor in monitors)
        self.labels = {check_id: label for monitor in monitors for check_id, label in monitor.labels.items()}

    def collect(self, now, observed_utc):
        results = []
        for monitor in self.monitors:
            try:
                results += monitor.collect(now, observed_utc)
            except Exception as error:
                # Ctrl+C (KeyboardInterrupt) není Exception a projde až do smyčky.
                results += failed_results(monitor, now, observed_utc, self.validity_seconds,
                                          f"měření selhalo ({type(error).__name__})",
                                          failure=failure_name(error))
        return results

    def window_results(self, now, observed_utc, validity_seconds):
        """Výsledky za okno zápisu historie od kolektorů, které ho vedou (SCHED-2).

        Vrací (id nahrazených živých výsledků, výsledky). Selhání okna jednoho
        kolektoru jeho id ponechá nahrazená bez výsledku: historie dostane
        mezeru, nikdy krátký živý vzorek místo měření za celé okno.
        """
        ids, results = set(), []
        for monitor in self.monitors:
            inner = getattr(monitor, "monitor", monitor)  # kolektor uvnitř Scheduled
            window = getattr(inner, "window_results", None)
            if window is None:
                continue
            try:
                replaced, measured = window(now, observed_utc, validity_seconds)
            except Exception:
                replaced, measured = set(monitor.labels), []
            ids |= replaced
            results += measured
        return ids, results


class Scheduled:
    """Kolektor služby, který skutečně měří jen ve svém termínu (SCHED-1).

    Smyčka volá collect() každý takt, ale kontrola služby proběhne jen při
    dosažení termínu, který kolektor určí po každé skutečné kontrole
    (`monitor.next_check(now)`). Mezi termíny se vrací poslední výsledky beze
    změny, tedy se stejným časem měření i platností: nic se neprodlužuje
    a AlarmEvaluator, RGB i historie je podle expires_monotonic poznají jako
    totéž měření. Kolektor může mezi termíny výsledek jen přepočítat bez
    nového měření (`monitor.between`, u záloh stáří posledního úspěchu).
    Zmeškaný termín (pozastavený proces) se nedohání: proběhne jedna kontrola
    a další termín se počítá od ní. Neočekávaná výjimka kolektoru je UNKNOWN
    do dalšího termínu, nikdy důvod k častějšímu měření.
    """

    def __init__(self, monitor, validity_seconds, interval_seconds=None):
        self.monitor = monitor
        self.labels = monitor.labels
        self.simulated = monitor.simulated
        # Rezerva za termínem další kontroly pro výsledky po výjimce; zároveň
        # platnost, jakou mají ostatní kolektory (Collectors ji vyžaduje shodnou).
        self.validity_seconds = validity_seconds
        # Pevný interval (SCHED-2, systém, ventilátor, síť); bez něj termín
        # určuje kolektor (`next_check`, služby ze SCHED-1).
        self.interval_seconds = interval_seconds
        self.origin = None  # čas prvního měření: počátek mřížky pevného intervalu
        self.results = None
        self.due = None
        self.checks = 0  # počet skutečných kontrol od spuštění

    def collect(self, now, observed_utc):
        if self.results is not None and now < self.due:
            between = getattr(self.monitor, "between", None)
            if between is not None:
                self.results = between(self.results, now, observed_utc)
            return self.results
        self.checks += 1
        error = None
        try:
            results = self.monitor.collect(now, observed_utc)
        except Exception as caught:
            # Ctrl+C (KeyboardInterrupt) není Exception a projde až do smyčky.
            error = caught
        self.due = self._next_due(now)
        if error is not None:
            results = failed_results(self.monitor, now, observed_utc, self.due - now + self.validity_seconds,
                                     f"měření selhalo ({type(error).__name__})", failure=failure_name(error))
        self.results = results
        return results

    def _next_due(self, now):
        if self.interval_seconds is None:
            return self.monitor.next_check(now)
        if self.origin is None:
            self.origin = now
        return grid_after(self.origin, self.interval_seconds, now)


def grid_after(origin, interval, now):
    """Nejbližší termín po `now` na mřížce `origin + k × interval` (SCHED-2).

    Kolektory s pevným intervalem i zápis historie začínají prvním taktem, takže
    mají stejný počátek: CPU po 5 s a zápis po 10 s padnou do stejného taktu
    i po zdržení smyčky (timeout služby, pozastavení). Zmeškané termíny se
    nedohánějí, další je až po `now`.
    """
    return origin + (math.floor((now - origin) / interval) + 1) * interval


def live_monitor(config):
    """Skutečné read-only kolektory: systém, režim /, ventilátor CPU, síť a volitelně AnimeDB, NAS a zálohy.

    Všechny kolektory běží přes Scheduled a skutečně měří jen ve svém termínu:
    systém, režim /, ventilátor a síť v pevných intervalech (SCHED-2, M5-S2,
    takt smyčky LIVE_TICK_SECONDS), služby podle svého plánu (SCHED-1).
    Platnost výsledku systému, režimu /, ventilátoru a sítě je 3 × jejich interval.
    """
    validity = config.collector.validity_seconds

    def every(monitor, interval):
        return Scheduled(monitor, validity, interval_seconds=interval)

    fast = (CPU_USAGE_ID, CPU_TEMP_ID, MEMORY_ID)
    monitors = [
        every(SystemMonitor(VALIDITY_INTERVALS * CPU_INTERVAL_SECONDS, checks=fast), CPU_INTERVAL_SECONDS),
        every(SystemMonitor(VALIDITY_INTERVALS * DISK_INTERVAL_SECONDS, checks=(DISK_ROOT_ID,)),
              DISK_INTERVAL_SECONDS),
        # Režim a typ souborového systému / jen z tabulky připojení (M5-S2), aby
        # přepnutí na ro bylo vidět do minuty; statvfs("/") dělá jen kontrola výše.
        every(RootMountMonitor(VALIDITY_INTERVALS * ROOT_MODE_INTERVAL_SECONDS), ROOT_MODE_INTERVAL_SECONDS),
        # Ventilátor CPU se jen čte; chlazení dál řídí Linux (M4-1).
        every(FanMonitor(VALIDITY_INTERVALS * FAN_INTERVAL_SECONDS), FAN_INTERVAL_SECONDS),
        # Čítače po 1 s, výběr rozhraní, linka a IPv4 po 60 s. Rychlost se počítá
        # i po zdržení smyčky (např. timeout služby) až do obvyklé platnosti.
        every(NetworkMonitor(VALIDITY_INTERVALS * NETWORK_RATE_INTERVAL_SECONDS, interface=config.network.interface,
                             details_interval=NETWORK_DETAILS_INTERVAL_SECONDS, gap_seconds=validity),
              NETWORK_RATE_INTERVAL_SECONDS),
    ]
    animedb = config.services.animedb
    if animedb.enabled:
        # Healthcheck se volá jen při výslovně zapnutém sledování (SERVICES-1);
        # jinak kolektor vůbec nevznikne, nic se nepřipojuje a AnimeDB zůstává N/A.
        # Výsledek platí do další plánované kontroly a ještě obvyklou platnost.
        monitors.append(Scheduled(AnimeDbMonitor(
            validity_seconds=animedb.interval_seconds + validity, url=animedb.url,
            timeout_seconds=animedb.timeout_seconds, interval_seconds=animedb.interval_seconds), validity))
    nas = config.services.nas
    if nas.enabled:
        # Pokus o spojení TCP jen při výslovně zapnutém sledování (SERVICES-2);
        # jinak kolektor vůbec nevznikne, nic se nepřipojuje a NAS zůstává N/A.
        monitors.append(Scheduled(NasMonitor(
            validity_seconds=nas.interval_seconds + validity, host=nas.host, port=nas.port,
            timeout_seconds=nas.timeout_seconds, interval_seconds=nas.interval_seconds), validity))
    backup = config.services.backup
    if backup.enabled:
        # Dotaz na systemd jen při výslovně zapnutém sledování (SERVICES-3); jinak
        # kolektor vůbec nevznikne, nic se nespouští a BACKUP zůstává N/A.
        # Platnost výsledku určuje plán dalšího dotazu (BackupMonitor.plan).
        monitors.append(Scheduled(BackupMonitor(validity_seconds=validity, unit=backup.unit, timer=backup.timer,
                                                timeout_seconds=backup.timeout_seconds), validity))
    return Collectors(*monitors)


def result_text(result, observed_utc=None):
    # U závady je ve výpisu vidět, které hodnoty se týká; ostatní text beze změny.
    text = f"{result.summary} [{result.state.value}]" if result.state in ALERT_STATES else result.summary
    # Výsledek služby z dřívější kontroly (SCHED-1) nesmí vedle času řádku
    # vypadat jako právě ověřený: řádek přizná jeho stáří.
    age = 0 if observed_utc is None else (observed_utc - result.observed_utc).total_seconds()
    if age >= 3600:
        return f"{text} (ověřeno před {age / 3600:.1f} h)"
    if age >= 60:
        return f"{text} (ověřeno před {age // 60:.0f} min)"
    return text


def format_line(results, now, observed_utc, text):
    """Řádek výpisu: čas měření (UTC), značka SIM, celkový stav a text."""
    # Simulovaná data se nesmí ani ve výpisu plést se skutečným měřením.
    sim = "SIM " if any(result.simulated for result in results) else ""
    state = overall_state(results, now).value
    return f"{observed_utc:%Y-%m-%d %H:%M:%S} UTC  {sim}{state:<8} {text}"


class Stopper:
    """Řízené ukončení na SIGINT (Ctrl+C) i SIGTERM (systemd, `kill`).

    Obsluha se nastaví jen v hlavním vlákně: Python signály v jiném vlákně
    doručit neumí. Obsluha vyvolá KeyboardInterrupt, takže se smyčka ukončí
    stejnou cestou jako dosud u Ctrl+C. Během dopisování front (`stopping`)
    už další signál smyčku nepřerušuje, aby poslední zápis historie neskončil
    v polovině; zaseknutý proces jde dál ukončit SIGKILL.
    """

    def __init__(self):
        self.signal = None
        self.stopping = False
        self._previous = {}

    @property
    def label(self):
        return "SIGTERM" if self.signal == signal.SIGTERM else "Ctrl+C"

    def install(self):
        """Nastaví obsluhu v hlavním vlákně; jinde vrátí False a nic nemění."""
        if threading.current_thread() is not threading.main_thread():
            return False
        for number in (signal.SIGINT, signal.SIGTERM):
            self._previous[number] = signal.getsignal(number)
            signal.signal(number, self)
        return True

    def restore(self):
        """Vrátí původní obsluhu signálů (po skončení smyčky)."""
        for number, handler in self._previous.items():
            signal.signal(number, handler)
        self._previous.clear()

    def __call__(self, number, frame):
        self.signal = number
        if self.stopping:
            return
        raise KeyboardInterrupt


def significant(results, now):
    """Provozně významný obsah výsledků pro výpis: stav a údaje `details` každé kontroly.

    Čísla ani text se neporovnávají (stejně jako history.ChangeTracker), takže
    běžné kolísání hodnot výpis nevyvolá; vypršelý výsledek (UNKNOWN) ano.
    """
    current = (effective(result, now) for result in results)
    return {result.id: (result.state, tuple(sorted(result.details.items()))) for result in current}


def history_results(monitor, results, now, observed_utc, validity_seconds):
    """Výsledky pro zápis do historie (SCHED-2).

    Veličiny měřené za interval (vytížení CPU, rychlost RX/TX) dostane historie
    za celé okno od minulého zápisu (`window_results` kolektoru), ne jako
    poslední krátké živé měření; ostatní výsledky beze změny. Okna jdou první:
    mají čas zápisu a platnost `validity_seconds`, takže časová základna
    historie (první výsledek měření) je stejná jako dřív.
    """
    window = getattr(monitor, "window_results", None)
    if window is None:
        return results
    replaced, windows = window(now, observed_utc, validity_seconds)
    return [*windows, *(result for result in results if result.id not in replaced)]


def run_loop(monitor, config, evaluator, clock=time.monotonic, utc_clock=utc_now, sleep=time.sleep,
             output=print_now, history_link=None, stopper=None, tick_seconds=None, board=None, user_state=None,
             heartbeat=None):
    """Hlavní smyčka měření. Běží do Ctrl+C nebo SIGTERM a vrací poslední výsledky.

    `config` je CollectorConfig, `evaluator` AlarmEvaluator s pravidly [alarms].
    `clock` jsou monotónní hodiny pro plánování a platnost výsledků, `utc_clock`
    dává jen čas měření. Testy dodají simulované hodiny i `sleep`, takže nic
    skutečně nečekají.
    `tick_seconds` je takt smyčky (SCHED-2, v provozu LIVE_TICK_SECONDS):
    kolektor se volá každý takt a sám měří jen ve svých termínech. Bez něj je
    takt `config.interval_seconds` jako dřív.
    `history_link` (volitelně) dostane finální vyhodnocené výsledky jen jednou
    za `config.interval_seconds` (zápis, plán snímku, kontroly `history.storage`
    a `history.snapshot`); bez něj se smyčka chová jako dosud.
    Výpis: řádek při prvním měření, při každé provozně významné změně (stav
    nebo `details` některé kontroly, `significant`) a jinak nejvýš jednou za
    SUMMARY_SECONDS, nikdy při každém taktu.
    `board` (web.StateBoard, M6-1) dostane po každém taktu tytéž vyhodnocené
    výsledky jako výpis; web je jen čte, sám nic neměří.
    `user_state` (user_state.UserState, M6-4A) je jediný vlastník potvrzení
    a jasu: v každém taktu vyřídí čekající požadavky, započte měření pro
    zánik potvrzení a soubor zapíše jen při skutečné změně.
    `heartbeat` (heartbeat.Heartbeat, M6-5) v každém taktu jen porovná čas; v termínu
    složí veřejný stav z týchž výsledků a předá ho vláknu přenosu, nic nečeká.
    """
    interval = config.interval_seconds
    tick = interval if tick_seconds is None else tick_seconds
    output(
        f"NexusSTAT: takt {tick} s, historie a její kontroly každých {interval} s, platnost jejich výsledků "
        f"{config.validity_seconds} s. Prahy: {evaluator.describe()} (bez prahu je platné měření INFO). "
        f"Výpis při změně stavu, jinak nejvýš jednou za {SUMMARY_SECONDS // 60} min. Ukončení: Ctrl+C."
    )
    results, history_checks = [], []
    measurements = 0
    next_due = clock()  # první měření hned po spuštění
    record_origin = next_record = next_due
    printed, next_summary = None, None
    try:
        while True:
            now = clock()
            if now < next_due:
                # Jediné čekání ve smyčce: proces spí až do plánovaného času,
                # žádné aktivní čekání. Po probuzení se čas ověří znovu.
                sleep(next_due - now)
                continue

            late = now - next_due
            if late >= tick:
                # Takt je o celý takt (nebo víc) pozdě: proces byl pozastavený,
                # zaseknutý nebo ho zdržela kontrola služby. Zmeškané takty se
                # nedohánějí, takt se počítá znovu od tohoto měření. Hláška jen
                # při zpoždění o celý interval historie, ne při každém krátkém.
                if late >= interval:
                    output(f"Zpoždění {late:.1f} s: zmeškaná měření se nedohánějí, interval začíná znovu.")
                next_due = now

            observed_utc = utc_clock()
            try:
                results = monitor.collect(now, observed_utc)
                text = None
            except Exception as error:
                # Ctrl+C (KeyboardInterrupt) není Exception a smyčku ukončí níže.
                results = failed_results(monitor, now, observed_utc, config.validity_seconds,
                                         failure=failure_name(error))
                text = f"měření selhalo: {type(error).__name__}: {error}"
            # Prahy se vyhodnotí i po selhání: UNKNOWN přeruší řadu měření pro
            # potvrzení závady, ale závadu nevyřeší. Výsledek z dřívějšího
            # měření (Scheduled) se podle identity nezapočte znovu.
            results = evaluator.evaluate(results, now)
            if now >= next_record:
                if history_link is not None:
                    # Do historie jdou finální vyhodnocené výsledky se skutečným
                    # monotónním časem zápisu; zápis probíhá v jiném vlákně, takže
                    # se tu na nic nečeká. Vlastní kontroly historie se do historie
                    # nepředávají: jejich stav se mění podle historie, takže by zápis
                    # sám vytvářel další zápisy (zpětná vazba).
                    history_link.record(history_results(monitor, results, now, observed_utc,
                                                        config.validity_seconds), now)
                    # Denní snímek se jen vyžádá (staví ho vlákno zapisovače a
                    # čekající měření mají přednost); smyčka na nic nečeká.
                    history_link.plan_snapshot(now, observed_utc)
                    history_checks = [history_link.check(now, observed_utc),
                                      history_link.snapshot_check(now, observed_utc)]
                # Stejná mřížka jako kolektory (Scheduled, grid_after): zápis po
                # 10 s padne do taktu s měřením CPU po 5 s. Zmeškané se nedohánějí.
                next_record = grid_after(record_origin, interval, now)
            results = [*results, *history_checks]
            if user_state is not None:
                # Potvrzení a jas mění jen tato smyčka; diagnostický stav výsledků se nemění.
                user_state.tick(results, now, observed_utc)
            if heartbeat is not None:
                heartbeat.tick(results, now, observed_utc)
            if board is not None:
                board.publish(results, now, observed_utc)
            measurements += 1
            current = significant(results, now)
            if text is not None or current != printed or now >= next_summary:
                if text is None:
                    text = " | ".join(result_text(result, observed_utc) for result in results)
                output(format_line(results, now, observed_utc, text))
                printed, next_summary = current, now + SUMMARY_SECONDS

            # Další termín od plánovaného času, ne od konce měření: doba měření
            # ani nepřesnost uspání se nesčítají a plán postupně neujíždí.
            next_due += tick
    except KeyboardInterrupt:
        # Nový řádek, aby hláška nenavazovala na "^C" vypsané terminálem.
        label = "Ctrl+C" if stopper is None else stopper.label
        output(f"\nUkončeno ({label}), počet měření: {measurements}.")
    return results


def live_note():
    """Jedna věta o intervalech živého měření pro výpis při startu (SCHED-2)."""
    return (f"Živé měření: takt {LIVE_TICK_SECONDS} s; CPU, teplota a RAM po {CPU_INTERVAL_SECONDS} s, "
            f"ventilátor po {FAN_INTERVAL_SECONDS} s, síť RX/TX po {NETWORK_RATE_INTERVAL_SECONDS} s, "
            f"rozhraní a IPv4 po {NETWORK_DETAILS_INTERVAL_SECONDS} s, režim / po {ROOT_MODE_INTERVAL_SECONDS} s, "
            f"obsazenost / po {DISK_INTERVAL_SECONDS} s; služby podle vlastního plánu.")


def snapshot_note(config, link):
    """Jedna věta o stavu denních snímků pro výpis při startu."""
    settings = config.history.snapshot
    if not settings.enabled:
        return "vypnuté ([history.snapshot] enabled = false)."
    if link.snapshot_error is not None:
        return f"zapnuté, ale nepoužitelné: {link.snapshot_error}; snímek nevznikne."
    return (f"každý den v {settings.daily_time} ({settings.timezone}) do {settings.published_dir}; "
            f"rozpracované v {settings.work_dir}. Staré generace se nemažou.")


def start_history(config, output=print_now):
    """Připraví a spustí historii podle [history]; vypnutá nebo nedostupná dá None.

    Nedostupná historie (např. nečitelné /etc/machine-id) měření nezastaví:
    vypíše se důvod a měří se dál bez ukládání.
    """
    try:
        link = open_history(config, app_version=__version__, output=output)
    except (history.HistoryError, OSError, ValueError) as error:
        output(f"Historie: nedostupná ({error}); měření pokračuje bez historie.")
        return None
    if link is None:
        return None
    link.start()
    note = link.sync_note
    output(f"Historie: {link.path} (role {HISTORY_ROLE}), zdroj synchronizace hodin: {link.sync_source}. "
           "Databáze se nikdy nevytváří ani nepřepisuje."
           + (f" ({note})" if note else ""))
    output(f"Historie: snímky {snapshot_note(config, link)}")
    return link


def main(argv=None):
    parser = argparse.ArgumentParser(description="Pravidelné read-only měření systému a sítě NexusSTAT")
    parser.add_argument("--config", type=Path,
                        help="TOML se sekcemi [collector], [network], [alarms], [oled], [history] a [services]")
    parser.add_argument("--web", action="store_true",
                        help=f"lokální web jen pro čtení (výchozí jen http://{LOOPBACK}:PORT/ z tohoto počítače)")
    parser.add_argument("--web-port", type=int, default=DEFAULT_PORT, metavar="PORT",
                        help=f"port lokálního webu (výchozí {DEFAULT_PORT})")
    parser.add_argument("--web-bind", default=LOOPBACK, metavar="IPv4",
                        help=f"adresa webu: {LOOPBACK} (výchozí) nebo konkrétní soukromá adresa LAN; s --web-allow")
    parser.add_argument("--web-allow", action="append", default=[], metavar="ROZSAH",
                        help="schválený rozsah klientů v LAN, např. 192.0.2.0/24 (lze opakovat)")
    parser.add_argument("--web-host", action="append", default=[], metavar="NÁZEV",
                        help="další povolený název počítače v hlavičce Host (lze opakovat)")
    parser.add_argument("--web-tls-cert", type=Path, metavar="SOUBOR",
                        help="certifikát HTTPS (PEM, existující soubor správce); s --web-tls-key")
    parser.add_argument("--web-tls-key", type=Path, metavar="SOUBOR",
                        help="soukromý klíč HTTPS (PEM bez hesla, jen pro vlastníka 0600)")
    parser.add_argument("--web-admin-password", type=Path, metavar="SOUBOR",
                        help="otisk administračního hesla (python3 -m nexusstat.admin SOUBOR); jen s HTTPS")
    args = parser.parse_args(argv)
    tls = password_hash = None
    try:
        config = load_config(args.config) if args.config else Config()
        check_port(args.web_port)
        access = web_access(args.web_bind, args.web_allow, args.web_host)
        if not args.web and (not access.loopback or access.host_names or args.web_tls_cert or args.web_tls_key
                             or args.web_admin_password):
            raise ValueError("--web-bind, --web-allow, --web-host, --web-tls-* a --web-admin-password platí "
                             "jen s --web")
        if (args.web_tls_cert is None) != (args.web_tls_key is None):
            raise ValueError("--web-tls-cert a --web-tls-key se zadávají spolu")
        if args.web_admin_password is not None and args.web_tls_cert is None:
            # Administrace nikdy přes nešifrované HTTP, ani na tomto počítači.
            raise ValueError("--web-admin-password vyžaduje HTTPS: --web-tls-cert a --web-tls-key")
        if args.web_tls_cert is not None:
            tls = tls_context(args.web_tls_cert, args.web_tls_key)
        if args.web_admin_password is not None:
            password_hash = load_password_file(args.web_admin_password)
        if config.heartbeat.enabled:
            # Tajemství a CA se ověří před měřením: chybná konfigurace = žádný start, ne tichý vypnutý heartbeat.
            heartbeat_secret = load_secret(config.heartbeat.secret_file)
            heartbeat_context = client_context(config.heartbeat.ca_file)
    except (AdminError, ConfigError, HeartbeatError, OSError, ValueError) as error:
        # Neplatná konfigurace = žádné měření, ne tiché použití výchozích hodnot.
        parser.error(str(error))
    monitor = live_monitor(config)  # jen vytvoření kolektorů, nic se neměří
    board = server = None
    if args.web:
        # Web jen čte výsledky této smyčky; obsazený port = chyba před měřením.
        try:
            board, server = start_web(monitor.labels, config.collector.validity_seconds, args.web_port,
                                      access=access, tls=tls)
        except OSError as error:
            parser.error(f"web nelze spustit na {access.bind}:{args.web_port}: {error}")
        clients = ("jen z tohoto počítače" if access.loopback else
                   f"bez přihlášení z {', '.join(map(str, access.networks))} a z tohoto počítače")
        scheme = "https" if tls is not None else "http"
        admin_note = ("; administrace na /admin jen s heslem (potvrzení závad a jas LED)" if password_hash
                      else "; administrace vypnutá")
        print_now(f"Web: {scheme}://{access.bind}:{server.server_address[1]}/ jen pro čtení, {clients}; "
                  f"ukazuje výsledky této smyčky, sám nic neměří{admin_note}.")
    stopper = Stopper()
    link = user_state = heartbeat = None
    try:
        stopper.install()
        link = start_history(config)
        # Potvrzení a jas (M6-4A) mají jediného vlastníka v této smyčce; události jdou do historie, je-li zapnutá.
        user_state = UserState(config.alarms, config.user_state.path, output=print_now,
                               on_event=None if link is None else link.record_event)
        print_now(f"Uživatelský stav: {user_state.load()}")
        if server is not None and password_hash is not None:
            # Administrace jen předává požadavky vlastníkovi stavu (request()); sama nic nemění.
            # Chybné přihlášení a blokace jdou jako auditní událost frontou zapisovače historie.
            server.admin = Admin(password_hash, user_state, board, output=print_now,
                                 on_event=None if link is None else link.record_event)
        if server is not None:
            # Grafy (M6-2) čtou historii jen pro čtení a jen na požádání stránky /history.
            if link is not None:
                server.history = HistoryCharts(link.path, HISTORY_ROLE, config.collector.interval_seconds)
            elif config.history.enabled:
                server.history = HISTORY_FAILED
        if config.heartbeat.enabled:
            heartbeat = Heartbeat(config.heartbeat.url, heartbeat_secret, heartbeat_context, monitor.labels,
                                  config.collector.validity_seconds, output=print_now)
            print_now(f"Heartbeat: https://{heartbeat.host}:{heartbeat.port}{heartbeat.path}, jen celkový stav a čas, "
                      "první za 60 s, pak po 300 s bez opakování; podpis HMAC-SHA256.")
        print_now(live_note())
        run_loop(monitor, config.collector, AlarmEvaluator(config.alarms),
                 history_link=link, stopper=stopper, tick_seconds=LIVE_TICK_SECONDS, board=board,
                 user_state=user_state, heartbeat=heartbeat)
    except KeyboardInterrupt:
        # Signál mimo smyčku (např. během spouštění historie): ukončení je
        # řízené i tady, jen bez řádku o počtu měření.
        print_now(f"\nUkončeno ({stopper.label}) před prvním měřením.")
    finally:
        # Obsluha signálů se vrátí i tehdy, když historie selže už při spuštění.
        # Další signál už ukončení nepřeruší: historie dopisuje fronty s limitem.
        stopper.stopping = True
        if server is not None:
            stop_web(server)
        if heartbeat is not None:
            heartbeat.close()
        if user_state is not None:
            user_state.close()
        if link is not None:
            link.stop()
        stopper.restore()


if __name__ == "__main__":
    main()
