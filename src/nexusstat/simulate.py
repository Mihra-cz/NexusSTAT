"""Offline simulace OLED: obrazovky uložené jako PNG snímky.

Použití:
  PYTHONPATH=src python3 -m nexusstat.simulate VÝSTUPNÍ_SLOŽKA [--config SOUBOR]
  PYTHONPATH=src python3 -m nexusstat.simulate VÝSTUPNÍ_SLOŽKA --live

Bez --live: skriptovaný scénář stisků s fiktivními daty, simulovaný čas, nic se nečeká.
S --live: jeden snímek každé obrazovky ze skutečného read-only měření tohoto
počítače (system_monitor, fan_monitor, network_monitor a se zapnutým
[services.animedb] i healthcheck AnimeDB, se zapnutým [services.nas] i kontrola
dostupnosti NAS, se zapnutým [services.backup] i read-only stav zálohovací
úlohy ze systemd). Ani jeden režim nepřistupuje k hardwaru displeje; do sítě
jdou jen zapnuté kontroly AnimeDB a NAS.
"""

import argparse
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Config, load_config
from .core import CheckResult, Measurement, State, active_problems
from .fan_monitor import CPU_FAN_ID, DEVICE, FAN_ON, FAN_STATE, RPM, SOURCE, SOURCE_RPM
from .network_monitor import (
    ADDRESS,
    BYTES_PER_SECOND,
    INTERFACE,
    IPV4_ID,
    LINK,
    LINK_ID,
    LINK_UP,
    RX_RATE_ID,
    TX_RATE_ID,
)
from .oled import CAROUSEL_PAGES, Mode, OledController
from .run import live_monitor
from .services import ANIMEDB_ID, BACKUP_ID, NAS_ID, NEXUS_SERVICES_ID, complete_service_results
from .system_monitor import CELSIUS, CPU_TEMP_ID, CPU_USAGE_ID, DISK_ROOT_ID, MEMORY_ID, PERCENT

# Snímky se ukládají zvětšené, aby byly 128 × 64 pixely na monitoru čitelné.
PNG_SCALE = 4


def sample_results(observed_utc):
    """Fiktivní výsledky kontrol: od t=5 s 6 aktivních problémů, tj. 2 stránky STATUS.

    Všechny jsou označené simulated=True, takže displej ukazuje značku SIM.
    Služby odpovídají ukázce stránky SERVICES (NEXUS, ANIMEDB a NAS OK, BACKUP WARN);
    ostatní fiktivní problémy jim proto neodporují. Hodnoty mají číselnou
    hodnotu jako skutečný kolektor, protože displej ukazuje ji, ne popis.
    """
    valid = 10_000  # monotónní sekundy, dost dlouho na celý scénář

    def sim(check_id, state, summary, expires=valid, value=None, details=None):
        return CheckResult(check_id, state, summary, observed_utc, expires, simulated=True, measurement=value,
                           details=details or {})

    # Fiktivní síť: adresa z dokumentačního rozsahu 192.0.2.0/24 (RFC 5737).
    eth0 = {INTERFACE: "eth0"}

    return [
        sim("system.load", State.OK, "Zátěž v normě"),
        sim("disk.root_readonly", State.CRITICAL, "Disk / jen pro čtení"),
        sim("system.time_sync", State.ERROR, "Čas nesynchronizován"),
        sim("disk.nvme_temp", State.WARNING, "NVMe 71 °C", value=Measurement(71, CELSIUS)),
        sim("docker.engine", State.UNKNOWN, "Docker: stav neznámý"),
        # N/A s krátkou platností: neexpiruje, zůstane N/A po celý scénář.
        sim("public.status", State.NA, "Veřejný status vypnut", expires=1),
        # Výsledek s prošlou platností: v čase scénáře se z OK stane UNKNOWN.
        sim("disk.smart", State.OK, "SMART bez chyb", expires=5),
        # Fiktivní hodnoty pro stránky SYSTEM a DISK (stejná id jako kolektor).
        sim(CPU_USAGE_ID, State.INFO, "CPU 23 %", value=Measurement(23, PERCENT)),
        sim(CPU_TEMP_ID, State.INFO, "Teplota CPU 48 °C", value=Measurement(48, CELSIUS)),
        sim(MEMORY_ID, State.INFO, "RAM 24 % z 7,9 GiB", value=Measurement(24, PERCENT)),
        # Ventilátor CPU: doložené otáčky (čtvrtý řádek SYSTEM, zobrazí se "ON 3200").
        sim(CPU_FAN_ID, State.INFO, "Ventilátor CPU ON (3200 RPM)", value=Measurement(3200, RPM),
            details={FAN_STATE: FAN_ON, SOURCE: SOURCE_RPM, DEVICE: "pwmfan"}),
        sim(DISK_ROOT_ID, State.INFO, "Disk / 31 % z 117 GiB", value=Measurement(31, PERCENT)),
        # Fiktivní hodnoty pro stránku NETWORK (stejná id a details jako kolektor).
        sim(LINK_ID, State.INFO, "Síť eth0 UP", details={**eth0, LINK: LINK_UP}),
        sim(RX_RATE_ID, State.INFO, "Síť RX 1,2 MB/s", value=Measurement(1_234_000, BYTES_PER_SECOND), details=eth0),
        sim(TX_RATE_ID, State.INFO, "Síť TX 85,3 kB/s", value=Measurement(85_300, BYTES_PER_SECOND), details=eth0),
        sim(IPV4_ID, State.INFO, "IPv4 192.0.2.10", details={**eth0, ADDRESS: "192.0.2.10"}),
        # Fiktivní stavy služeb pro stránku SERVICES (skutečné kolektory zatím nejsou).
        sim(NEXUS_SERVICES_ID, State.OK, "Služby Nexusu běží"),
        sim(ANIMEDB_ID, State.OK, "AnimeDB odpovídá"),
        sim(NAS_ID, State.OK, "NAS dostupný"),
        sim(BACKUP_ID, State.WARNING, "Záloha starší 26 h"),
    ]


# (čas v s, akce): "press" = krátký stisk, "wait" = jen uplynutí času
SCENARIO = [
    (0, "wait"),
    (1, "press"),  # probuzení na NEXUS
    (11, "wait"),  # SYSTEM
    (21, "wait"),  # DISK
    (31, "wait"),  # NETWORK
    (41, "wait"),  # SERVICES
    (51, "wait"),  # kolotoč dokola: znovu NEXUS
    (55, "press"),  # STATUS 1/2
    (60, "press"),  # STATUS 2/2
    (65, "press"),  # z poslední stránky zpět na NEXUS, nové okno
    (70, "press"),  # STATUS 1/2
    (100, "wait"),  # 30 s bez stisku → zpět do kolotoče
    (400, "wait"),  # 300 s od návratu → spánek
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
    """Snímek každé obrazovky ze skutečného měření. Čas je skutečný monotónní.

    `config` je celá konfigurace: [oled] pro automat, [collector] pro platnost
    výsledků, [network] pro sledované rozhraní, [services] pro AnimeDB, NAS a zálohy.
    """
    monitor = live_monitor(config)
    monitor.collect(time.monotonic(), datetime.now(timezone.utc))  # první vzorek CPU a síťových čítačů
    time.sleep(1)
    now = time.monotonic()
    observed_utc = datetime.now(timezone.utc)
    # Kolektor NEXUS zatím není: je UNKNOWN (bez dat) na všech obrazovkách.
    # AnimeDB, NAS a BACKUP jsou N/A, ledaže je zapnutá jejich kontrola.
    results = complete_service_results(
        monitor.collect(now, observed_utc), observed_utc, now + config.collector.validity_seconds,
        monitored=monitor.labels,
    )
    for result in results:
        print(f"{result.id:<17} {result.state.value:<8} {result.summary}")

    render_screen = load_renderer(output_dir)
    if render_screen is None:
        return
    wall_time = datetime.now()
    # Každou stránku kolotoče vykreslíme ve stejném okamžiku `now`, aby žádný
    # snímek neukazoval výsledky po vypršení platnosti.
    for page in CAROUSEL_PAGES:
        controller = OledController(config.oled, pages=(page,))
        controller.press(now, 0)
        save_png(render_screen(controller, now, results, wall_time), output_dir / f"live_{page}.png")
    controller = OledController(config.oled)
    controller.press(now, 0)
    controller.press(now, len(active_problems(results, now)))  # druhý stisk otevře STATUS
    save_png(render_screen(controller, now, results, wall_time), output_dir / "live_STATUS.png")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Offline simulace OLED NexusSTAT")
    parser.add_argument("output_dir", type=Path, help="složka pro PNG snímky")
    parser.add_argument("--config", type=Path, help="TOML se sekcemi [oled], [collector] a [network]")
    parser.add_argument("--live", action="store_true", help="skutečné read-only měření tohoto počítače")
    args = parser.parse_args(argv)
    config = load_config(args.config) if args.config else Config()
    if args.live:
        run_live(args.output_dir, config)
    else:
        run(args.output_dir, config.oled)


if __name__ == "__main__":
    main()
