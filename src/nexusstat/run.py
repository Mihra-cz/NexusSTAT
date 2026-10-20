"""Pravidelné read-only měření systému s výpisem do terminálu.

Použití:
  PYTHONPATH=src python3 -m nexusstat.run [--config SOUBOR]

Každých `interval_seconds` (sekce [collector], výchozí 10 s) změří vytížení CPU,
teplotu CPU, RAM, obsazenost / a síťové rozhraní ([network]: linka, RX/TX, IPv4),
vyhodnotí zapnuté prahy ([alarms], výchozí vypnuto) a vypíše jeden řádek.
Ukončení: Ctrl+C.
Nic nezapisuje, neotevírá port, nic neposílá do sítě a nepřistupuje k displeji,
NAS ani jinému hardwaru.
Alarm se jen vypíše; žádná automatická akce (vypnutí, ventilátory) neexistuje.
"""

import argparse
import time
from datetime import datetime, timezone
from pathlib import Path

from .alarms import AlarmEvaluator
from .config import Config, ConfigError, load_config
from .core import ALERT_STATES, CheckResult, State, overall_state
from .network_monitor import NetworkMonitor
from .system_monitor import SystemMonitor


def utc_now():
    return datetime.now(timezone.utc)


def print_now(line):
    # Vypsat hned, i když je výstup přesměrovaný do souboru nebo do `tee`.
    print(line, flush=True)


def failed_results(monitor, now, observed_utc, validity_seconds, reason="měření selhalo"):
    """Výsledky pro měření, které celé selhalo: všechny kontroly kolektoru UNKNOWN.

    Kontroly určuje `monitor.labels` (id → popisek). Předchozí hodnoty se
    nepodrží, i když by ještě byly platné: víme, že aktuální měření
    neproběhlo, a staré INFO/OK by vypadalo jako čerstvé. Stejně se kolektor
    chová při selhání jedné kontroly.
    """
    return [
        CheckResult(
            check_id, State.UNKNOWN, f"{label}: {reason}", observed_utc,
            now + validity_seconds, simulated=monitor.simulated,
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
                                          f"měření selhalo ({type(error).__name__})")
        return results


def live_monitor(config):
    """Skutečné read-only kolektory tohoto počítače: systém a síť podle konfigurace."""
    validity = config.collector.validity_seconds
    return Collectors(
        SystemMonitor(validity_seconds=validity),
        NetworkMonitor(validity_seconds=validity, interface=config.network.interface),
    )


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


def run_loop(monitor, config, evaluator, clock=time.monotonic, utc_clock=utc_now, sleep=time.sleep,
             output=print_now):
    """Hlavní smyčka měření. Běží do Ctrl+C a vrací poslední vyhodnocené výsledky.

    `config` je CollectorConfig, `evaluator` AlarmEvaluator s pravidly [alarms].
    `clock` jsou monotónní hodiny pro plánování a platnost výsledků, `utc_clock`
    dává jen čas měření. Testy dodají simulované hodiny i `sleep`, takže nic
    skutečně nečekají.
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
                results = failed_results(monitor, now, observed_utc, config.validity_seconds)
                text = f"měření selhalo: {type(error).__name__}: {error}"
            # Prahy se vyhodnotí i po selhání: UNKNOWN přeruší řadu měření pro
            # potvrzení závady, ale závadu nevyřeší.
            results = evaluator.evaluate(results, now)
            if text is None:
                text = " | ".join(result_text(result) for result in results)
            measurements += 1
            output(format_line(results, now, observed_utc, text))

            # Další termín od plánovaného času, ne od konce měření: doba měření
            # ani nepřesnost uspání se nesčítají a plán postupně neujíždí.
            next_due += interval
    except KeyboardInterrupt:
        # Nový řádek, aby hláška nenavazovala na "^C" vypsané terminálem.
        output(f"\nUkončeno (Ctrl+C), počet měření: {measurements}.")
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description="Pravidelné read-only měření systému a sítě NexusSTAT")
    parser.add_argument("--config", type=Path, help="TOML se sekcemi [collector], [network], [alarms] a [oled]")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config) if args.config else Config()
    except (ConfigError, OSError) as error:
        # Neplatná konfigurace = žádné měření, ne tiché použití výchozích hodnot.
        parser.error(str(error))
    run_loop(live_monitor(config), config.collector, AlarmEvaluator(config.alarms))


if __name__ == "__main__":
    main()
