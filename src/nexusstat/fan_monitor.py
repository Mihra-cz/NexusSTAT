"""Read-only kolektor hlavního ventilátoru CPU (M4-1, docs/REQUIREMENTS.md 85).

Jen čte `/sys/class/hwmon` a `/sys/class/thermal`. Nic nezapisuje, nespouští
žádný příkaz a ventilátor nijak neřídí: chlazení dál řídí Linux.

Kolektor vrací **jednu** kontrolu `system.cpu_fan` s jednoduchým stavem:

* `ON` / `OFF` z **naměřených otáček** (RPM > 0, respektive RPM = 0). Otáčky
  jdou do `measurement` jako doplňující číslo pro displej i historii.
* `ON` / `OFF` z **požadavku řízení**, když tachometr není. Takový stav je
  v `details` označený (`source = request`) a displej ho ukáže jako `REQ ON`
  nebo `REQ OFF`, takže se nevydává za potvrzené otáčení. Procenta ani stupně
  PWM se nikam nepředávají: rozhoduje jen to, zda řízení ventilátor žádá.

Chybějící tachometr se nikdy nenahrazuje odhadnutými otáčkami.

Zařízení se vybírá podle **identity** (`name` ovladače, `fanN_label` od
zařízení), nikdy podle čísla `hwmonX` ani podle jména souboru: čísla se mezi
starty mění a `fan1_input` sám o sobě neříká, který ventilátor měří. Když
prokazatelný zdroj neexistuje, je stav `N/A` (není co sledovat, žádná závada);
když zdroj existuje, ale je nečitelný nebo nejednoznačný, je `UNKNOWN`.

Prahy ani závady kolektor nevyhodnocuje (stejně jako system_monitor): `OFF`
při nízké teplotě není závada a pravidlo pro rozpor „požadavek > 0, otáčky 0“
bude samostatné schválené rozhodnutí (docs/ARCHITECTURE.md 95).

Ukázkové čtení skutečného systému:  PYTHONPATH=src python3 -m nexusstat.fan_monitor
"""

import time
from collections import namedtuple
from datetime import datetime, timezone
from pathlib import Path

from .config import CollectorConfig
from .core import CheckResult, Measurement, State, is_finite_number
from .system_monitor import PERCENT, Unavailable, read_text

CPU_FAN_ID = "system.cpu_fan"

RPM = "RPM"

LABELS = {CPU_FAN_ID: "Ventilátor CPU"}

DEFAULT_HWMON_DIR = "/sys/class/hwmon"
DEFAULT_THERMAL_DIR = "/sys/class/thermal"

# Ovladače, u kterých je prokázané, že měří **hlavní** ventilátor CPU/SoC.
# `pwmfan` je jméno hwmon zařízení ovladače pwm-fan (Raspberry Pi 5 a další SoC
# s ventilátorem řízeným kernelem). Rozšiřovat se smí jen o ovladač, u kterého
# je to doložené na skutečném hardwaru, ne podle dohadu.
CPU_FAN_HWMON_NAMES = ("pwmfan",)

# Popisek kanálu od zařízení (`fanN_label`), kterým se ventilátor sám hlásí
# jako ventilátor CPU. Jde o údaj zařízení, ne o jméno souboru.
CPU_FAN_LABELS = ("cpu fan", "cpu_fan", "cpufan")

# Typ chladicího zařízení kernelu, které je požadavkem na PWM ventilátor
# (`/sys/class/thermal/cooling_device*/type`). Typy jako `Processor`,
# `intel_powerclamp` nebo `PCIe_Port_Link_Speed` ventilátor neřídí.
COOLING_FAN_TYPES = ("pwm-fan",)

# Kontrola věrohodnosti: ne hardwarový limit ani práh, jen odhalení vadného
# čtení. Záporné otáčky nebo desetitisíce RPM nad rozsah běžných ventilátorů
# jsou neplatná data, nikdy skutečná hodnota.
MAX_PLAUSIBLE_RPM = 30_000
PWM_MAX = 255  # hwmon pwmN je 0–255 (bez jednotky), přepočítává se na procenta

# Údaje v `details`: jen kategorické hodnoty, které se mění při skutečném
# přechodu, takže historie zapíše jeden řádek na přechod, ne na každé měření.
FAN_STATE = "fan"
FAN_ON = "ON"
FAN_OFF = "OFF"
SOURCE = "source"  # odkud stav je
SOURCE_RPM = "rpm"  # naměřené otáčky: doložené otáčení nebo stání
SOURCE_REQUEST = "request"  # jen požadavek řízení: otáčení není doložené
DEVICE = "device"  # jméno hwmon zařízení (nebo typ chladicího zařízení)

# Nalezený zdroj otáček: adresář hwmon zařízení, jeho `name`, číslo kanálu
# a čím je doložené, že patří ventilátoru CPU.
FanSource = namedtuple("FanSource", "path name channel evidence")
RequestSource = namedtuple("RequestSource", "path kind device")


class Ambiguous(Unavailable):
    """Zdrojů je víc a žádný není prokazatelně ten hlavní: stav je UNKNOWN."""


def _channels(device, pattern):
    """Čísla kanálů zařízení podle vzoru (fanN_input, pwmN), vzestupně."""
    numbers = []
    for entry in device.glob(pattern.replace("N", "*")):
        number = entry.name.removeprefix(pattern.split("N")[0])
        number = number.removesuffix(pattern.split("N")[1]) if pattern.split("N")[1] else number
        if number.isdigit():
            numbers.append(int(number))
    return sorted(numbers)


def _label(device, channel):
    try:
        return read_text(device / f"fan{channel}_label").strip().lower()
    except (OSError, ValueError):
        return None


def find_cpu_fan(hwmon_dir):
    """Prokazatelný zdroj otáček ventilátoru CPU, nebo None.

    Projde všechna zařízení `hwmon*` (číslo se mezi starty mění, proto se na ně
    nikdy nespoléhá) a přijme jen ty, u kterých identitu dokládá `name`
    ovladače, nebo `fanN_label` od zařízení. Víc přijatých zdrojů je
    nejednoznačnost (`Ambiguous`), ne volba podle pořadí.
    """
    found = []
    for device in sorted(Path(hwmon_dir).glob("hwmon*")):
        if not device.is_dir():
            continue
        try:
            name = read_text(device / "name").strip()
        except (OSError, ValueError):
            continue  # nečitelné zařízení nesmí zastavit hledání ostatních
        channels = _channels(device, "fanN_input")
        if name in CPU_FAN_HWMON_NAMES:
            # Ovladač hlavního ventilátoru. Bez tachometru zůstane kanál None:
            # otáčky nejsou, ale zařízení je prokázané (požadavek z něj jde číst).
            found.append(FanSource(device, name, channels[0] if channels else None, "hwmon-name"))
            continue
        for channel in channels:
            if _label(device, channel) in CPU_FAN_LABELS:
                found.append(FanSource(device, name, channel, "fan-label"))
    if not found:
        return None
    if len(found) > 1:
        detail = ", ".join(f"{source.name}/fan{source.channel}" for source in found)
        raise Ambiguous(f"víc zdrojů otáček ({detail}), žádný prokazatelně hlavní")
    return found[0]


def other_fans(hwmon_dir):
    """Jména zařízení s ventilátorem, která se nepodařilo přiřadit CPU (pro popis)."""
    names = []
    for device in sorted(Path(hwmon_dir).glob("hwmon*")):
        try:
            name = read_text(device / "name").strip()
        except (OSError, ValueError):
            continue
        if name in CPU_FAN_HWMON_NAMES:
            continue
        for channel in _channels(device, "fanN_input"):
            if _label(device, channel) not in CPU_FAN_LABELS:
                names.append(f"{name}/fan{channel}")
    return names


def parse_rpm(text):
    """Otáčky z `fanN_input` (celé RPM)."""
    rpm = int(text.strip())
    if not 0 <= rpm <= MAX_PLAUSIBLE_RPM:
        raise ValueError(f"nevěrohodné otáčky {rpm} RPM")
    return rpm


def parse_pwm_percent(text):
    """Požadavek z hwmon `pwmN` (0–255) na procenta."""
    raw = int(text.strip())
    if not 0 <= raw <= PWM_MAX:
        raise ValueError(f"nevěrohodné PWM {raw}")
    return 100 * raw / PWM_MAX


def parse_cooling_percent(current_text, maximum_text):
    """Požadavek z chladicího zařízení kernelu (`cur_state` z `max_state`) na procenta."""
    current, maximum = int(current_text.strip()), int(maximum_text.strip())
    if maximum <= 0 or not 0 <= current <= maximum:
        raise ValueError(f"nevěrohodný stupeň chlazení {current}/{maximum}")
    return 100 * current / maximum


def find_fan_cooling_device(thermal_dir):
    """Chladicí zařízení kernelu typu pwm-fan, nebo None; víc jich je nejednoznačnost."""
    found = []
    for device in sorted(Path(thermal_dir).glob("cooling_device*")):
        try:
            kind = read_text(device / "type").strip()
        except (OSError, ValueError):
            continue
        if kind in COOLING_FAN_TYPES:
            found.append(device)
    if not found:
        return None
    if len(found) > 1:
        raise Ambiguous(f"víc chladicích zařízení typu {COOLING_FAN_TYPES[0]} ({len(found)})")
    return found[0]


class FanMonitor:
    """Read-only kolektor otáček ventilátoru CPU a požadavku regulace.

    Výsledky mají stejný model jako ostatní kolektory: `INFO` s číselnou
    hodnotou u změřeného údaje, `UNKNOWN` u nečitelných nebo nejednoznačných
    dat a `N/A`, když prokazatelný zdroj na tomto stroji vůbec není (není co
    sledovat, tedy ani závada). Zdroje se hledají při každém měření znovu,
    takže změna identifikace po restartu se pozná.
    """

    labels = LABELS  # id výsledků, které collect() vrací (pro run.failed_results)

    def __init__(self, validity_seconds, hwmon_dir=DEFAULT_HWMON_DIR, thermal_dir=DEFAULT_THERMAL_DIR):
        if not is_finite_number(validity_seconds) or validity_seconds <= 0:
            raise ValueError(f"validity_seconds musí být kladný počet sekund, ne {validity_seconds!r}")
        self.validity_seconds = validity_seconds
        self.hwmon_dir = Path(hwmon_dir)
        self.thermal_dir = Path(thermal_dir)
        # Skutečné měření jen ze skutečných cest; vzorové soubory v testech jsou
        # fiktivní data, tedy simulated=True (stejné pravidlo jako SystemMonitor).
        self.simulated = not (
            self.hwmon_dir == Path(DEFAULT_HWMON_DIR) and self.thermal_dir == Path(DEFAULT_THERMAL_DIR)
        )

    def collect(self, now, observed_utc):
        """Jeden výsledek se stavem ventilátoru. `now` je monotónní čas, `observed_utc` čas UTC."""
        return [self._fan(now, observed_utc)]

    def _result(self, state, summary, now, observed_utc, measurement=None, details=None):
        return CheckResult(CPU_FAN_ID, state, summary, observed_utc, now + self.validity_seconds,
                           simulated=self.simulated, measurement=measurement, details=details or {})

    def _fan(self, now, observed_utc):
        """Stav ventilátoru: `ON`/`OFF` z otáček, jinak z požadavku řízení.

        `UNKNOWN` jen u nečitelných, nevěrohodných nebo nejednoznačných dat,
        `N/A`, když na stroji není prokazatelný zdroj (není co sledovat, tedy
        ani závada). Žádná závada se nevyhodnocuje.
        """
        label = LABELS[CPU_FAN_ID]
        try:
            source = find_cpu_fan(self.hwmon_dir)
        except Ambiguous as reason:
            return self._result(State.UNKNOWN, f"{label}: {reason}", now, observed_utc)
        if source is not None and source.channel is not None:
            try:
                rpm = parse_rpm(read_text(source.path / f"fan{source.channel}_input"))
            except OSError:
                return self._result(State.UNKNOWN, f"{label}: nedostupné", now, observed_utc,
                                    details={DEVICE: source.name})
            except (ValueError, ArithmeticError):
                return self._result(State.UNKNOWN, f"{label}: neplatná data", now, observed_utc,
                                    details={DEVICE: source.name})
            state = FAN_ON if rpm > 0 else FAN_OFF
            return self._result(State.INFO, f"{label} {state} ({rpm} RPM)", now, observed_utc,
                                measurement=Measurement(rpm, RPM),
                                details={FAN_STATE: state, SOURCE: SOURCE_RPM, DEVICE: source.name})
        # Bez tachometru rozhoduje jen to, zda řízení ventilátor žádá. Otáčky
        # se neodhadují, takže výsledek nemá žádné `measurement`.
        try:
            request = self._request(source)
        except Ambiguous as reason:
            return self._result(State.UNKNOWN, f"{label}: {reason}", now, observed_utc)
        except OSError:
            return self._result(State.UNKNOWN, f"{label}: nedostupné", now, observed_utc)
        except (ValueError, ArithmeticError):
            return self._result(State.UNKNOWN, f"{label}: neplatná data", now, observed_utc)
        if request is None:
            others = other_fans(self.hwmon_dir)
            seen = f" (nepřiřazené ventilátory: {', '.join(others)})" if others else ""
            return self._result(State.NA, f"{label}: prokazatelný zdroj stavu nenalezen{seen}",
                                now, observed_utc)
        state, device = request
        return self._result(State.INFO, f"{label} {state} podle požadavku řízení (otáčky neměřené)",
                            now, observed_utc, details={FAN_STATE: state, SOURCE: SOURCE_REQUEST,
                                                        DEVICE: device})

    def _request(self, source):
        """(`ON`/`OFF`, zařízení) z požadavku řízení, nebo None, když zdroj není.

        Hodnota se používá jen na rozhodnutí „žádá řízení ventilátor?“; žádné
        procento ani stupeň PWM se dál nepředává.
        """
        if source is not None:
            channels = _channels(source.path, "pwmN")
            if channels:
                percent = parse_pwm_percent(read_text(source.path / f"pwm{channels[0]}"))
                return (FAN_ON if percent > 0 else FAN_OFF), source.name
        device = find_fan_cooling_device(self.thermal_dir)
        if device is None:
            return None
        percent = parse_cooling_percent(read_text(device / "cur_state"), read_text(device / "max_state"))
        return (FAN_ON if percent > 0 else FAN_OFF), COOLING_FAN_TYPES[0]


def main():
    """Výpis skutečného stavu ventilátoru tohoto stroje (jen čtení)."""
    monitor = FanMonitor(validity_seconds=CollectorConfig().validity_seconds)
    for result in monitor.collect(time.monotonic(), datetime.now(timezone.utc)):
        print(f"{result.id:<26} {result.state.value:<8} {result.summary}  (simulated={result.simulated})")


if __name__ == "__main__":
    main()
