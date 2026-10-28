"""Pravidelné read-only měření systému s výpisem do terminálu.

Použití:
  PYTHONPATH=src python3 -m nexusstat.run [--config SOUBOR]

Každých `interval_seconds` (sekce [collector], výchozí 10 s) změří vytížení CPU,
teplotu CPU, RAM, obsazenost / a síťové rozhraní ([network]: linka, RX/TX, IPv4),
vyhodnotí zapnuté prahy ([alarms], výchozí vypnuto) a vypíše jeden řádek.
Se zapnutým [services.animedb] (výchozí vypnuto) navíc jedním požadavkem GET
na nastavenou adresu /health zkontroluje dostupnost AnimeDB, se zapnutým
[services.nas] (výchozí vypnuto) jedním pokusem o spojení TCP na port SMB
dostupnost NAS (bez přihlášení, bez sdílených složek a mountů), se zapnutým
[services.backup] (výchozí vypnuto) jedním read-only dotazem `systemctl show`
výsledek existující zálohovací úlohy (bez spouštění zálohy a bez Resticu).
Ukončení: Ctrl+C nebo SIGTERM.
Bez sekce [history] (výchozí stav) nic nezapisuje, neotevírá port, nepřistupuje
k displeji, mountům NAS ani jinému hardwaru a do sítě posílá jen zapnuté kontroly AnimeDB a NAS. Se zapnutou historií
([history] enabled = true s výslovnou cestou) předává vyhodnocené výsledky
vláknu zapisovače SQLite; samo měření zůstává read-only a chyba historie ho
nezastaví (history_link.py).
Alarm se jen vypíše; žádná automatická akce (vypnutí, ventilátory) neexistuje.
"""

import argparse
import signal
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import __version__, history
from .alarms import AlarmEvaluator
from .animedb_monitor import AnimeDbMonitor
from .backup_monitor import BackupMonitor
from .config import HISTORY_ROLE, Config, ConfigError, load_config
from .core import ALERT_STATES, CheckResult, State, overall_state
from .fan_monitor import FanMonitor
from .history_link import open_history
from .nas_monitor import NasMonitor
from .network_monitor import NetworkMonitor
from .system_monitor import SystemMonitor


# Klíč v details, kterým se selhání sběru odlišuje od platného měření bez hodnoty
# (docs/HISTORY.md 4.4). Hodnotou je jméno výjimky; stav zůstává UNKNOWN, takže
# prahy, výpis ani displej se nemění a nic nevypadá jako OK.
COLLECTOR_FAILURE = "failure"


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


def live_monitor(config):
    """Skutečné read-only kolektory: systém, ventilátor CPU, síť a volitelně AnimeDB, NAS a zálohy podle konfigurace."""
    validity = config.collector.validity_seconds
    monitors = [
        SystemMonitor(validity_seconds=validity),
        # Ventilátor CPU se jen čte; chlazení dál řídí Linux (M4-1).
        FanMonitor(validity_seconds=validity),
        NetworkMonitor(validity_seconds=validity, interface=config.network.interface),
    ]
    animedb = config.services.animedb
    if animedb.enabled:
        # Healthcheck se volá jen při výslovně zapnutém sledování (SERVICES-1);
        # jinak kolektor vůbec nevznikne, nic se nepřipojuje a AnimeDB zůstává N/A.
        monitors.append(AnimeDbMonitor(validity_seconds=validity, url=animedb.url,
                                       timeout_seconds=animedb.timeout_seconds))
    nas = config.services.nas
    if nas.enabled:
        # Pokus o spojení TCP jen při výslovně zapnutém sledování (SERVICES-2);
        # jinak kolektor vůbec nevznikne, nic se nepřipojuje a NAS zůstává N/A.
        monitors.append(NasMonitor(validity_seconds=validity, host=nas.host, port=nas.port,
                                   timeout_seconds=nas.timeout_seconds))
    backup = config.services.backup
    if backup.enabled:
        # Dotaz na systemd jen při výslovně zapnutém sledování (SERVICES-3); jinak
        # kolektor vůbec nevznikne, nic se nespouští a BACKUP zůstává N/A.
        monitors.append(BackupMonitor(validity_seconds=validity, unit=backup.unit, timer=backup.timer,
                                      timeout_seconds=backup.timeout_seconds))
    return Collectors(*monitors)


def result_text(result):
    # U závady je ve výpisu vidět, které hodnoty se týká; ostatní text beze změny.
    if result.state in ALERT_STATES:
        return f"{result.summary} [{result.state.value}]"
    return result.summary


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


def run_loop(monitor, config, evaluator, clock=time.monotonic, utc_clock=utc_now, sleep=time.sleep,
             output=print_now, history_link=None, stopper=None):
    """Hlavní smyčka měření. Běží do Ctrl+C nebo SIGTERM a vrací poslední výsledky.

    `config` je CollectorConfig, `evaluator` AlarmEvaluator s pravidly [alarms].
    `clock` jsou monotónní hodiny pro plánování a platnost výsledků, `utc_clock`
    dává jen čas měření. Testy dodají simulované hodiny i `sleep`, takže nic
    skutečně nečekají.
    `history_link` (volitelně) dostane finální vyhodnocené výsledky a doplní do
    nich vlastní kontrolu `history.storage`; bez něj se smyčka chová jako dosud.
    """
    interval = config.interval_seconds
    output(
        f"NexusSTAT: měření každých {interval} s, platnost výsledků {config.validity_seconds} s. "
        f"Prahy: {evaluator.describe()} (bez prahu je platné měření INFO). Ukončení: Ctrl+C."
    )
    results = []
    measurements = 0
    next_due = clock()  # první měření hned po spuštění
    try:
        while True:
            now = clock()
            if now < next_due:
                # Jediné čekání ve smyčce: proces spí až do plánovaného času,
                # žádné aktivní čekání. Po probuzení se čas ověří znovu.
                sleep(next_due - now)
                continue

            late = now - next_due
            if late >= interval:
                # Měření je o celý interval (nebo víc) pozdě: proces byl pozastavený
                # nebo zaseknutý. Zmeškaná měření se nedohánějí, proběhne jen
                # toto jedno a interval se počítá znovu od něj.
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
            # potvrzení závady, ale závadu nevyřeší.
            results = evaluator.evaluate(results, now)
            if history_link is not None:
                # Do historie jdou finální vyhodnocené výsledky se skutečným
                # monotónním časem měření; zápis probíhá v jiném vlákně, takže
                # se tu na nic nečeká. Vlastní kontroly historie se do historie
                # nepředávají: jejich stav se mění podle historie, takže by zápis
                # sám vytvářel další zápisy (zpětná vazba).
                history_link.record(results, now)
                # Denní snímek se jen vyžádá (staví ho vlákno zapisovače a
                # čekající měření mají přednost); smyčka na nic nečeká.
                history_link.plan_snapshot(now, observed_utc)
                results = [*results, history_link.check(now, observed_utc),
                           history_link.snapshot_check(now, observed_utc)]
            if text is None:
                text = " | ".join(result_text(result) for result in results)
            measurements += 1
            output(format_line(results, now, observed_utc, text))

            # Další termín od plánovaného času, ne od konce měření: doba měření
            # ani nepřesnost uspání se nesčítají a plán postupně neujíždí.
            next_due += interval
    except KeyboardInterrupt:
        # Nový řádek, aby hláška nenavazovala na "^C" vypsané terminálem.
        label = "Ctrl+C" if stopper is None else stopper.label
        output(f"\nUkončeno ({label}), počet měření: {measurements}.")
    return results


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
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config) if args.config else Config()
    except (ConfigError, OSError) as error:
        # Neplatná konfigurace = žádné měření, ne tiché použití výchozích hodnot.
        parser.error(str(error))
    stopper = Stopper()
    link = None
    try:
        stopper.install()
        link = start_history(config)
        run_loop(live_monitor(config), config.collector, AlarmEvaluator(config.alarms),
                 history_link=link, stopper=stopper)
    except KeyboardInterrupt:
        # Signál mimo smyčku (např. během spouštění historie): ukončení je
        # řízené i tady, jen bez řádku o počtu měření.
        print_now(f"\nUkončeno ({stopper.label}) před prvním měřením.")
    finally:
        # Obsluha signálů se vrátí i tehdy, když historie selže už při spuštění.
        # Další signál už ukončení nepřeruší: historie dopisuje fronty s limitem.
        stopper.stopping = True
        if link is not None:
            link.stop()
        stopper.restore()


if __name__ == "__main__":
    main()
