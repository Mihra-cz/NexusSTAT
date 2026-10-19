"""Read-only kolektor základních údajů Linuxu: vytížení CPU, RAM, teplota CPU a disk /.

Jen čte /proc, /sys/class/thermal a os.statvfs("/"). Nic nezapisuje, nespouští
příkazy a neprochází /mnt, NAS ani jiné souborové systémy.

Kolektor prahy nevyhodnocuje: platná hodnota má stav INFO (změřeno) a číselnou
hodnotu s jednotkou (`measurement`). WARNING/ERROR určuje až alarms.AlarmEvaluator.
Nedostupná nebo neplatná hodnota má stav UNKNOWN a žádnou číselnou hodnotu.

Ukázkové čtení skutečného systému:  PYTHONPATH=src python3 -m nexusstat.system_monitor
"""

import os
import time
from collections import namedtuple
from datetime import datetime, timezone
from pathlib import Path

from .config import CollectorConfig
from .core import CheckResult, Measurement, State, is_finite_number

CPU_USAGE_ID = "system.cpu_usage"
CPU_TEMP_ID = "system.cpu_temp"
MEMORY_ID = "system.memory"
DISK_ROOT_ID = "disk.root"

# Jednotky číselných hodnot. Prahy v konfiguraci jsou ve stejných jednotkách.
PERCENT = "%"
CELSIUS = "°C"

# Krátké popisky pro displej a popisy výsledků.
LABELS = {
    CPU_USAGE_ID: "CPU",
    CPU_TEMP_ID: "Teplota CPU",
    MEMORY_ID: "RAM",
    DISK_ROOT_ID: "Disk /",
}

# Typy teplotních zón, které měří CPU, v pořadí přednosti. Číslo zóny se mezi
# stroji liší (na notebooku může být thermal_zone0 i Wi-Fi), proto rozhoduje typ.
# cpu-thermal: Raspberry Pi, x86_pkg_temp: Intel. Zóny Wi-Fi, ACPI (acpitz)
# nebo baterie se ignorují, protože neměří CPU.
CPU_THERMAL_TYPES = ("cpu-thermal", "cpu_thermal", "soc_thermal", "x86_pkg_temp")

# Prozatímní kontrola věrohodnosti údaje v °C: (MIN, MAX]. Není to hardwarový
# limit ani alarmový práh, jen odhalení zjevně vadného čtení. Hodnota 0 a nižší
# bývá zástupná hodnota nepřipraveného senzoru (běžící CPU není pod bodem mrazu).
# Hodnota mimo rozsah je UNKNOWN, nikdy se nevydává za skutečnou teplotu.
MIN_PLAUSIBLE_CELSIUS = 0
MAX_PLAUSIBLE_CELSIUS = 125

DEFAULT_PROC_DIR = "/proc"
DEFAULT_THERMAL_DIR = "/sys/class/thermal"

CpuSample = namedtuple("CpuSample", "idle total")


class Unavailable(Exception):
    """Hodnotu nelze změřit ze známého důvodu; text se použije v popisu UNKNOWN."""


def read_text(path):
    # /proc a /sys jsou ASCII. Poškozený obsah vyvolá UnicodeDecodeError,
    # což je podtřída ValueError, a skončí jako "neplatná data".
    return Path(path).read_text(encoding="ascii")


def format_size(size_bytes):
    """Velikost pro řádek displeje: méně číslic u velkých hodnot, aby se řádek vešel do 128 px."""
    gib = size_bytes / 2**30
    if gib >= 1000:
        text = f"{gib / 1024:.1f} TiB"
    elif gib >= 100:
        text = f"{gib:.0f} GiB"
    else:
        text = f"{gib:.1f} GiB"
    return text.replace(".", ",")


def parse_cpu_sample(stat_text):
    """Souhrnné čítače CPU z /proc/stat (řádek "cpu")."""
    for line in stat_text.splitlines():
        fields = line.split()
        if fields and fields[0] == "cpu":
            # user nice system idle iowait irq softirq steal. Sloupce guest
            # a guest_nice jsou už započtené v user/nice, proto se nesčítají.
            counters = [int(value) for value in fields[1:9]]
            if len(counters) < 4 or min(counters) < 0:
                raise ValueError("neúplný řádek cpu")
            # iowait je čekání CPU bez práce, proto patří k nečinnosti.
            idle = counters[3] + (counters[4] if len(counters) > 4 else 0)
            return CpuSample(idle=idle, total=sum(counters))
    raise ValueError("v /proc/stat chybí řádek cpu")


def cpu_usage_percent(previous, current):
    """Vytížení CPU v % mezi dvěma vzorky, nebo None, pokud ho nejde spočítat."""
    total_delta = current.total - previous.total
    idle_delta = current.idle - previous.idle
    # Nulový rozdíl = dvě čtení ve stejném tiku (dělení nulou). Rozdíl mimo
    # rozsah = čítače šly zpět (iowait se na některých jádrech může snížit,
    # hotplug CPU). Takový interval nedává smysluplné procento.
    if total_delta <= 0 or not 0 <= idle_delta <= total_delta:
        return None
    return 100 * (total_delta - idle_delta) / total_delta


def parse_meminfo(meminfo_text):
    """(MemTotal, MemAvailable) v KiB z /proc/meminfo."""
    values = {}
    for line in meminfo_text.splitlines():
        fields = line.split()
        if fields and fields[0] in ("MemTotal:", "MemAvailable:"):
            if len(fields) != 3 or fields[2] != "kB":
                raise ValueError(f"neočekávaný formát: {line!r}")
            values[fields[0]] = int(fields[1])
    total = values.get("MemTotal:")
    available = values.get("MemAvailable:")
    # Bez MemAvailable nepočítáme z MemFree: to by vydávalo cache za obsazenou paměť.
    if total is None or available is None:
        raise ValueError("chybí MemTotal nebo MemAvailable")
    if total <= 0 or not 0 <= available <= total:
        raise ValueError("nesmyslné hodnoty paměti")
    return total, available


def disk_usage(stat):
    """(obsazenost v %, velikost v bajtech) ze statvfs, stejným výpočtem jako `df`.

    Bloky rezervované pro roota se nepočítají jako volné, protože je běžné
    procesy nemohou využít.
    """
    if stat.f_frsize <= 0 or stat.f_blocks <= 0:
        raise ValueError("nesmyslná velikost souborového systému")
    if not 0 <= stat.f_bavail <= stat.f_bfree <= stat.f_blocks:
        raise ValueError("nesmyslné počty volných bloků")
    used = stat.f_blocks - stat.f_bfree
    usable = used + stat.f_bavail
    if usable <= 0:
        raise ValueError("žádné využitelné bloky")
    return 100 * used / usable, stat.f_blocks * stat.f_frsize


def find_cpu_thermal_zone(thermal_dir):
    """Adresář zóny s teplotou CPU vybraný podle typu, nebo None."""
    candidates = []
    for zone in Path(thermal_dir).glob("thermal_zone*"):
        number = zone.name.removeprefix("thermal_zone")
        if not number.isdigit():
            continue
        try:
            zone_type = read_text(zone / "type").strip()
        except (OSError, ValueError):
            continue  # nečitelná zóna nesmí zastavit hledání ostatních
        if zone_type in CPU_THERMAL_TYPES:
            candidates.append((CPU_THERMAL_TYPES.index(zone_type), int(number), zone))
    # Přednost má typ, při shodě nejnižší číslo zóny, aby byla volba stabilní.
    return min(candidates)[2] if candidates else None


def parse_temperature(temp_text):
    """Teplota v °C z hodnoty v tisícinách stupně (formát /sys/class/thermal)."""
    celsius = int(temp_text.strip()) / 1000
    if not MIN_PLAUSIBLE_CELSIUS < celsius <= MAX_PLAUSIBLE_CELSIUS:
        raise ValueError(f"nevěrohodná teplota {celsius} °C")
    return celsius


class SystemMonitor:
    """Čte systémové údaje a vrací je jako CheckResult.

    Pamatuje si jen předchozí vzorek CPU, protože vytížení vyžaduje dva vzorky.
    """

    def __init__(self, validity_seconds, proc_dir=DEFAULT_PROC_DIR, thermal_dir=DEFAULT_THERMAL_DIR,
                 statvfs=os.statvfs):
        # Platnost výsledků dodává volající z konfigurace (CollectorConfig, trojnásobek
        # intervalu měření). Kolektor nemá vlastní výchozí hodnotu, aby se nemohla
        # rozejít s intervalem spouštění. Když měření přestanou přicházet, hodnoty
        # zestárnou na UNKNOWN místo tichého zamrznutí.
        if not is_finite_number(validity_seconds) or validity_seconds <= 0:
            raise ValueError(f"validity_seconds musí být kladný počet sekund, ne {validity_seconds!r}")
        self.validity_seconds = validity_seconds
        self.proc_dir = Path(proc_dir)
        self.thermal_dir = Path(thermal_dir)
        self.statvfs = statvfs
        # Skutečné měření jen ze skutečného systému. Jakýkoli náhradní zdroj
        # (vzorové soubory v testech) jsou fiktivní data, tedy simulated=True.
        # Porovnávají se Path, aby "/proc", "/proc/" i Path("/proc") znamenaly totéž.
        self.simulated = not (
            self.proc_dir == Path(DEFAULT_PROC_DIR)
            and self.thermal_dir == Path(DEFAULT_THERMAL_DIR)
            and statvfs is os.statvfs
        )
        self.previous_cpu = None

    def collect(self, now, observed_utc):
        """Změří všechny kontroly. `now` je monotónní čas, `observed_utc` skutečný čas UTC."""
        return [
            self._check(CPU_USAGE_ID, self._cpu_usage, now, observed_utc),
            self._check(CPU_TEMP_ID, self._cpu_temperature, now, observed_utc),
            self._check(MEMORY_ID, self._memory, now, observed_utc),
            self._check(DISK_ROOT_ID, self._disk_root, now, observed_utc),
        ]

    def _check(self, check_id, measure, now, observed_utc):
        # Každá kontrola ošetřuje své chyby sama, aby selhání jedné neshodilo ostatní.
        # Při chybě zůstane measurement None: hodnota je nedostupná, stav UNKNOWN.
        label = LABELS[check_id]
        measurement = None
        try:
            measurement, text = measure()
            state, summary = State.INFO, f"{label} {text}"
        except Unavailable as reason:
            state, summary = State.UNKNOWN, f"{label}: {reason}"
        except OSError:
            state, summary = State.UNKNOWN, f"{label}: nedostupné"
        except (ValueError, ArithmeticError):
            # ArithmeticError: nesmyslně velké číslo projde int(), ale dělení na
            # float přeteče (OverflowError). Je to vadný údaj, ne pád celého měření.
            state, summary = State.UNKNOWN, f"{label}: neplatná data"
        expires = now + self.validity_seconds
        return CheckResult(
            check_id, state, summary, observed_utc, expires, simulated=self.simulated, measurement=measurement
        )

    # Každé měření vrací (Measurement, text pro displej). Text se zaokrouhluje,
    # hodnota pro prahy ne.
    def _cpu_usage(self):
        current = parse_cpu_sample(read_text(self.proc_dir / "stat"))
        previous, self.previous_cpu = self.previous_cpu, current
        if previous is None:
            raise Unavailable("první vzorek")
        percent = cpu_usage_percent(previous, current)
        if percent is None:
            raise Unavailable("neplatný interval")
        return Measurement(percent, PERCENT), f"{percent:.0f} %"

    def _cpu_temperature(self):
        zone = find_cpu_thermal_zone(self.thermal_dir)
        if zone is None:
            raise Unavailable("senzor nenalezen")
        celsius = parse_temperature(read_text(zone / "temp"))
        return Measurement(celsius, CELSIUS), f"{celsius:.0f} °C"

    def _memory(self):
        total_kib, available_kib = parse_meminfo(read_text(self.proc_dir / "meminfo"))
        used_percent = 100 * (total_kib - available_kib) / total_kib
        return Measurement(used_percent, PERCENT), f"{used_percent:.0f} % z {format_size(total_kib * 1024)}"

    def _disk_root(self):
        # Výhradně kořenový souborový systém, nikdy /mnt ani síťové mounty.
        percent, size_bytes = disk_usage(self.statvfs("/"))
        return Measurement(percent, PERCENT), f"{percent:.0f} % z {format_size(size_bytes)}"


def main():
    """Dva vzorky skutečného systému s odstupem 1 s a výpis výsledků."""
    monitor = SystemMonitor(validity_seconds=CollectorConfig().validity_seconds)
    monitor.collect(time.monotonic(), datetime.now(timezone.utc))  # první vzorek CPU
    time.sleep(1)
    for result in monitor.collect(time.monotonic(), datetime.now(timezone.utc)):
        print(f"{result.id:<17} {result.state.value:<8} {result.summary}  (simulated={result.simulated})")


if __name__ == "__main__":
    main()
