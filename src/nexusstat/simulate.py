"""Offline simulace OLED: obrazovky uložené jako PNG snímky.

Použití:
  PYTHONPATH=src python3 -m nexusstat.simulate VÝSTUPNÍ_SLOŽKA [--config SOUBOR]
  PYTHONPATH=src python3 -m nexusstat.simulate VÝSTUPNÍ_SLOŽKA --live

Bez --live: skriptovaný scénář stisků s fiktivními daty, simulovaný čas, nic se nečeká.
S --live: jeden snímek každé obrazovky ze skutečného read-only měření tohoto
počítače (system_monitor). Ani jeden režim nepřistupuje k hardwaru displeje.
"""

import argparse
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import OledConfig, load_oled_config
from .core import CheckResult, State, active_problems
from .oled import CAROUSEL_PAGES, Mode, OledController
from .system_monitor import CPU_TEMP_ID, CPU_USAGE_ID, DISK_ROOT_ID, MEMORY_ID, SystemMonitor

# Snímky se ukládají zvětšené, aby byly 128 × 64 pixely na monitoru čitelné.
PNG_SCALE = 4


def sample_results(observed_utc):
    """Fiktivní výsledky kontrol: od t=5 s 5 aktivních problémů, tj. 2 stránky STATUS.

    Všechny jsou označené simulated=True, takže displej ukazuje značku SIM.
    """
    valid = 10_000  # monotónní sekundy, dost dlouho na celý scénář

    def sim(check_id, state, summary, expires=valid):
        return CheckResult(check_id, state, summary, observed_utc, expires, simulated=True)

    return [
        sim("system.load", State.OK, "Zátěž v normě"),
        sim("backup.restic", State.ERROR, "Záloha starší 26 h"),
        sim("nas.anime", State.CRITICAL, "NAS anime nedostupný"),
        sim("disk.nvme_temp", State.WARNING, "NVMe 71 °C"),
        sim("docker.engine", State.UNKNOWN, "Docker: stav neznámý"),
        # N/A s krátkou platností: neexpiruje, zůstane N/A po celý scénář.
        sim("animedb", State.NA, "AnimeDB není nasazena", expires=1),
        # Výsledek s prošlou platností: v čase scénáře se z OK stane UNKNOWN.
        sim("disk.smart", State.OK, "SMART bez chyb", expires=5),
        # Fiktivní hodnoty pro stránky SYSTEM a DISK (stejná id jako kolektor).
        sim(CPU_USAGE_ID, State.INFO, "CPU 23 %"),
        sim(CPU_TEMP_ID, State.INFO, "Teplota CPU 48 °C"),
        sim(MEMORY_ID, State.INFO, "RAM 24 % z 7,9 GiB"),
        sim(DISK_ROOT_ID, State.INFO, "Disk / 31 % z 117 GiB"),
    ]


# (čas v s, akce): "press" = krátký stisk, "wait" = jen uplynutí času
SCENARIO = [
    (0, "wait"),
    (1, "press"),  # probuzení na NEXUS
    (11, "wait"),  # další stránka kolotoče
    (21, "wait"),
    (25, "press"),  # STATUS 1/2
    (30, "press"),  # STATUS 2/2
    (35, "press"),  # z poslední stránky zpět na NEXUS, nové okno
    (40, "press"),  # STATUS 1/2
    (70, "wait"),  # 30 s bez stisku → zpět do kolotoče
    (370, "wait"),  # 300 s od návratu → spánek
]


def describe(controller, now):
    if controller.mode is Mode.CAROUSEL:
        return f"CAROUSEL/{controller.current_page(now)}"
    if controller.mode is Mode.STATUS:
        return f"STATUS/{controller.status_page + 1}"
    return controller.mode.value


def load_renderer(output_dir):
    try:
        from .render import render_screen
    except ImportError:
        print("Pillow není dostupné; nic se nevykreslí.", file=sys.stderr)
        return None
    output_dir.mkdir(parents=True, exist_ok=True)
    return render_screen


def save_png(image, path):
    image.resize((image.width * PNG_SCALE, image.height * PNG_SCALE), resample=0).save(path)


def run(output_dir, config):
    render_screen = load_renderer(output_dir)

    controller = OledController(config)
    results = sample_results(observed_utc=datetime.now(timezone.utc))
    # Místní čas jen pro hodiny na stránce NEXUS. Automat a platnost výsledků
    # používají výhradně simulovaný monotónní čas `now`.
    start_wall_time = datetime.now().replace(microsecond=0)

    for step, (now, action) in enumerate(SCENARIO):
        if action == "press":
            controller.press(now, len(active_problems(results, now)))
        controller.update(now)
        state = describe(controller, now)
        print(f"t={now:>4} s  {action:<5}  {state}")
        if render_screen is not None:
            image = render_screen(controller, now, results, start_wall_time + timedelta(seconds=now))
            save_png(image, output_dir / f"{step:02d}_t{now:03d}_{state.replace('/', '_')}.png")


def run_live(output_dir, config):
    """Snímek každé obrazovky ze skutečného měření. Čas je skutečný monotónní."""
    monitor = SystemMonitor()
    monitor.collect(time.monotonic(), datetime.now(timezone.utc))  # první vzorek CPU
    time.sleep(1)
    now = time.monotonic()
    results = monitor.collect(now, datetime.now(timezone.utc))
    for result in results:
        print(f"{result.id:<17} {result.state.value:<8} {result.summary}")

    render_screen = load_renderer(output_dir)
    if render_screen is None:
        return
    wall_time = datetime.now()
    # Každou stránku kolotoče vykreslíme ve stejném okamžiku `now`, aby žádný
    # snímek neukazoval výsledky po vypršení platnosti.
    for page in CAROUSEL_PAGES:
        controller = OledController(config, pages=(page,))
        controller.press(now, 0)
        save_png(render_screen(controller, now, results, wall_time), output_dir / f"live_{page}.png")
    controller = OledController(config)
    controller.press(now, 0)
    controller.press(now, len(active_problems(results, now)))  # druhý stisk otevře STATUS
    save_png(render_screen(controller, now, results, wall_time), output_dir / "live_STATUS.png")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Offline simulace OLED NexusSTAT")
    parser.add_argument("output_dir", type=Path, help="složka pro PNG snímky")
    parser.add_argument("--config", type=Path, help="TOML s sekcí [oled]")
    parser.add_argument("--live", action="store_true", help="skutečné read-only měření tohoto počítače")
    args = parser.parse_args(argv)
    config = load_oled_config(args.config) if args.config else OledConfig()
    if args.live:
        run_live(args.output_dir, config)
    else:
        run(args.output_dir, config)


if __name__ == "__main__":
    main()
