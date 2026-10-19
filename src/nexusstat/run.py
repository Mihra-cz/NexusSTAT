"""Pravidelné read-only měření systému s výpisem do terminálu.

Použití:
  PYTHONPATH=src python3 -m nexusstat.run [--config SOUBOR]

Každých `interval_seconds` (sekce [collector], výchozí 10 s) změří vytížení CPU,
teplotu CPU, RAM a obsazenost / a vypíše jeden řádek. Ukončení: Ctrl+C.
Nic nezapisuje, neotevírá port a nepřistupuje k displeji, NAS ani jinému hardwaru.
"""

import argparse
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import Config, ConfigError, load_config
from .core import CheckResult, State, overall_state
from .system_monitor import LABELS, SystemMonitor


def utc_now():
    return datetime.now(timezone.utc)


def print_now(line):
    # Vypsat hned, i když je výstup přesměrovaný do souboru nebo do `tee`.
    print(line, flush=True)


def failed_results(monitor, now, observed_utc, validity_seconds):
    """Výsledky pro měření, které celé selhalo: všechny kontroly UNKNOWN.

    Předchozí hodnoty se nepodrží, i když by ještě byly platné: víme, že
    aktuální měření neproběhlo, a staré INFO/OK by vypadalo jako čerstvé.
    Stejně se kolektor chová při selhání jedné kontroly.
    """
    return [
        CheckResult(
            check_id, State.UNKNOWN, f"{label}: měření selhalo", observed_utc,
            now + validity_seconds, simulated=monitor.simulated,
        )
        for check_id, label in LABELS.items()
    ]


def format_line(results, now, observed_utc, text):
    """Řádek výpisu: čas měření (UTC), značka SIM, celkový stav a text."""
    # Simulovaná data se nesmí ani ve výpisu plést se skutečným měřením.
    sim = "SIM " if any(result.simulated for result in results) else ""
    state = overall_state(results, now).value
    return f"{observed_utc:%Y-%m-%d %H:%M:%S} UTC  {sim}{state:<8} {text}"


def run_loop(monitor, config, clock=time.monotonic, utc_clock=utc_now, sleep=time.sleep, output=print_now):
    """Hlavní smyčka měření. Běží do Ctrl+C a vrací poslední výsledky.

    `config` je CollectorConfig. `clock` jsou monotónní hodiny pro plánování
    a platnost výsledků, `utc_clock` dává jen čas měření. Testy dodají
    simulované hodiny i `sleep`, takže nic skutečně nečekají.
    """
    interval = config.interval_seconds
    output(
        f"NexusSTAT: měření každých {interval} s, platnost výsledků {config.validity_seconds} s. "
        "Stav INFO = změřeno, bez alarmových prahů. Ukončení: Ctrl+C."
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
                text = " | ".join(result.summary for result in results)
            except Exception as error:
                # Ctrl+C (KeyboardInterrupt) není Exception a smyčku ukončí níže.
                results = failed_results(monitor, now, observed_utc, config.validity_seconds)
                text = f"měření selhalo: {type(error).__name__}: {error}"
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
    parser = argparse.ArgumentParser(description="Pravidelné read-only měření systému NexusSTAT")
    parser.add_argument("--config", type=Path, help="TOML se sekcemi [collector] a [oled]")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config) if args.config else Config()
    except (ConfigError, OSError) as error:
        # Neplatná konfigurace = žádné měření, ne tiché použití výchozích hodnot.
        parser.error(str(error))
    collector = config.collector
    run_loop(SystemMonitor(validity_seconds=collector.validity_seconds), collector)


if __name__ == "__main__":
    main()
