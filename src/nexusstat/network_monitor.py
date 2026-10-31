"""Read-only kolektor síťového rozhraní Linuxu: stav linky, přenos RX/TX a IPv4.

Čte /sys/class/net (stav linky a čítače bajtů jádra) a /proc/net/route
(výchozí trasa). Lokální IPv4 adresu zjistí dotazem na jádro (ioctl
SIOCGIFADDR): k tomu stačí nepřipojený socket, který se na nic neváže,
nic neodesílá a hned se zavře. Žádné odchozí síťové požadavky, žádné
spouštění příkazů, nic se nezapisuje.

Sleduje jediné rozhraní a provoz různých rozhraní nesčítá: provoz kontejnerů
nebo VPN prochází i fyzickým rozhraním, takže by se započítal dvakrát.

Automatický výběr (bez [network] interface), v tomto pořadí:
1. fyzické rozhraní s aktivní výchozí trasou IPv4, nejnižší metrika,
2. fyzické rozhraní s linkou UP,
3. jakékoli fyzické rozhraní (displej pak ukáže DOWN);
při shodě rozhoduje abecední pořadí názvu. Fyzické rozhraní má odkaz na
hardware /sys/class/net/<název>/device. Loopback, Docker (docker0, br-*,
veth*), VPN (tun*, wg*) ani jiná virtuální rozhraní ho nemají, takže se
automaticky nevyberou. Výběr se opakuje při každém měření, v živém měření
(SCHED-2) jednou za minutu a hned po zmizení nebo novém vzniku rozhraní.

Kolektor prahy nevyhodnocuje: platný údaj má stav INFO (i linka DOWN nebo
rozhraní bez IPv4), nezjistitelný UNKNOWN.

Ukázkové čtení skutečného systému:  PYTHONPATH=src python3 -m nexusstat.network_monitor [ROZHRANÍ]
"""

import argparse
import errno
import fcntl
import ipaddress
import socket
import struct
import time
from collections import namedtuple
from datetime import datetime, timezone
from pathlib import Path

from .config import VALIDITY_INTERVALS, CollectorConfig, is_valid_interface_name
from .core import CheckResult, Measurement, State, is_finite_number
from .system_monitor import Unavailable, read_text

LINK_ID = "network.link"
RX_RATE_ID = "network.rx_rate"
TX_RATE_ID = "network.tx_rate"
IPV4_ID = "network.ipv4"

# Rychlost přenosu v bajtech za sekundu. Není to procento vytížení linky:
# rychlost linky (u Wi-Fi často neznámá) se nečte.
BYTES_PER_SECOND = "B/s"

# Popisky výsledků pro terminál, ve stejném pořadí, v jakém je vrací collect().
LABELS = {
    LINK_ID: "Síť",
    RX_RATE_ID: "Síť RX",
    TX_RATE_ID: "Síť TX",
    IPV4_ID: "IPv4",
}

# Klíče v CheckResult.details.
INTERFACE = "interface"  # název sledovaného rozhraní (všechny čtyři výsledky)
LINK = "link"  # UP nebo DOWN (jen network.link ve stavu INFO)
ADDRESS = "ipv4"  # adresa (jen network.ipv4 ve stavu INFO; bez klíče = rozhraní IPv4 nemá)

LINK_UP = "UP"
LINK_DOWN = "DOWN"

# Hodnoty operstate (RFC 2863), u kterých linka nepřenáší data. "dormant"
# = Wi-Fi nepřihlášená k síti. "unknown" hlásí i funkční rozhraní, jejichž
# ovladač stav neuvádí; o těch rozhodne carrier.
DOWN_OPERSTATES = {"down", "lowerlayerdown", "notpresent", "dormant"}

RTF_UP = 0x0001  # linux/route.h: trasa je aktivní

SIOCGIFADDR = 0x8915  # linux/sockios.h: adresa rozhraní
# struct ifreq: název 16 B, za ním sockaddr_in (rodina 2 B, port 2 B, adresa 4 B).
IFREQ_ADDRESS = slice(20, 24)

DEFAULT_NET_DIR = "/sys/class/net"
DEFAULT_ROUTE_PATH = "/proc/net/route"

# Jeden vzorek čítačů. `counters` = {id výsledku: hodnota čítače nebo výjimka
# při čtení}. ifindex se změní, když jádro rozhraní vytvoří znovu (např. po
# odpojení USB adaptéru), i se stejným názvem; čítače pak začínají od nuly.
Sample = namedtuple("Sample", "interface ifindex time counters")

COUNTER_FILES = {RX_RATE_ID: "rx_bytes", TX_RATE_ID: "tx_bytes"}


def interface_ipv4(name):
    """Primární IPv4 adresa rozhraní podle jádra, nebo None, když žádnou nemá.

    ioctl SIOCGIFADDR potřebuje libovolný socket rodiny AF_INET. Socket se
    jen vytvoří a zavře: neváže se na port, nepřipojuje se a nic neodesílá.
    Zmizelé rozhraní vyvolá OSError (ENODEV).
    """
    request = struct.pack("256s", name.encode("ascii"))
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        try:
            reply = fcntl.ioctl(sock.fileno(), SIOCGIFADDR, request)
        except OSError as error:
            if error.errno == errno.EADDRNOTAVAIL:
                return None  # rozhraní existuje, ale IPv4 nemá
            raise
    return socket.inet_ntoa(reply[IFREQ_ADDRESS])


def parse_default_routes(route_text):
    """{rozhraní: nejnižší metrika} aktivních výchozích tras IPv4 z /proc/net/route.

    Výchozí trasa má cíl i masku 0.0.0.0. Čísla jsou v souboru šestnáctkově,
    metrika desítkově.
    """
    lines = route_text.splitlines()
    if not lines or lines[0].split()[:1] != ["Iface"]:
        raise ValueError("neočekávaný formát /proc/net/route")
    metrics = {}
    for line in lines[1:]:
        fields = line.split()
        if not fields:
            continue
        if len(fields) < 8:
            raise ValueError(f"neúplný řádek trasy: {line!r}")
        name, destination, flags, metric, mask = fields[0], fields[1], fields[3], fields[6], fields[7]
        if int(destination, 16) == 0 and int(mask, 16) == 0 and int(flags, 16) & RTF_UP:
            metrics[name] = min(int(metric), metrics.get(name, int(metric)))
    return metrics


def choose_interface(physical, default_metrics, links_up):
    """Deterministický automatický výběr rozhraní; pořadí pravidel viz popis modulu.

    `physical`: názvy fyzických rozhraní, `default_metrics`: {název: metrika
    výchozí trasy}, `links_up`: názvy s linkou UP. Vrací název nebo None.
    """
    routed = [name for name in physical if name in default_metrics]
    if routed:
        return min(routed, key=lambda name: (default_metrics[name], name))
    up = [name for name in physical if name in links_up]
    return min(up or physical, default=None)


def read_number(path):
    """Nezáporné celé číslo ze souboru /sys (čítač, ifindex). Jen číslice, nic jiného."""
    text = read_text(path).strip()
    if not text.isdigit():
        raise ValueError(f"neplatné číslo {text!r}")
    return int(text)


def read_link_state(interface_dir):
    """UP nebo DOWN podle operstate, případně carrier. Nejasný stav je Unavailable."""
    operstate = read_text(interface_dir / "operstate").strip()
    if operstate == "up":
        return LINK_UP
    if operstate in DOWN_OPERSTATES:
        return LINK_DOWN
    if operstate == "unknown":
        # Provozní stav, ne administrativní (příznak IFF_UP): administrativně
        # vypnuté rozhraní jádro hlásí vždy jako operstate "down", takže sem
        # nedojde. Selže-li tu čtení carrier (EINVAL, rozhraní vypnuté mezi
        # oběma čteními), je to OSError, tedy UNKNOWN.
        carrier = read_text(interface_dir / "carrier").strip()
        if carrier in ("0", "1"):
            return LINK_UP if carrier == "1" else LINK_DOWN
        raise ValueError(f"neplatný carrier {carrier!r}")
    if operstate == "testing":
        raise Unavailable("linka v testovacím režimu")
    raise ValueError(f"neznámý operstate {operstate!r}")


def counter_rate(previous, current, elapsed):
    """Rychlost v B/s z rozdílu dvou hodnot čítače za `elapsed` sekund."""
    if current < previous:
        # Čítač se snížil: reset (znovu načtený ovladač) nebo přetečení
        # 32bitového čítače. Rozdíl by nedával smysl, raději UNKNOWN.
        raise Unavailable("reset čítače")
    return (current - previous) / elapsed


def format_rate(bytes_per_second):
    """Rychlost pro terminál (česky): B/s, kB/s nebo MB/s, desetinná čárka."""
    if bytes_per_second < 1000:
        text = f"{bytes_per_second:.0f} B/s"
    elif bytes_per_second < 1_000_000:
        text = f"{bytes_per_second / 1000:.1f} kB/s"
    else:
        text = f"{bytes_per_second / 1_000_000:.1f} MB/s"
    return text.replace(".", ",")


class FirstSample(Unavailable):
    """První vzorek čítačů: rychlost zatím nejde spočítat, okno zápisu tím nekončí."""


# Výběr rozhraní a jeho linka a IPv4 z posledního obnovení (SCHED-2). `reason` je
# důvod, proč čítače nejde číst (rozhraní nevybráno nebo nenalezeno), jinak None.
Details = namedtuple("Details", "interface ifindex reason link ipv4")


class NetworkMonitor:
    """Čte jedno síťové rozhraní a vrací čtyři CheckResult: linka, RX, TX a IPv4.

    Pamatuje si jen předchozí vzorek čítačů, protože rychlost vyžaduje dva.
    Bez `details_interval` vybírá rozhraní a čte linku a IPv4 při každém
    měření. S ním (SCHED-2, živé měření po 1 s) se čítače čtou při každém
    měření, ale výběr rozhraní, linka a IPv4 jen jednou za `details_interval`
    a hned, když rozhraní zmizí nebo vznikne znovu (jiný ifindex); mezi tím se
    vrací tytéž výsledky s jejich vlastní platností (3 × `details_interval`).
    `gap_seconds` je nejdelší mezera mezi dvěma vzorky, ze které se rychlost
    ještě počítá (výchozí `validity_seconds`).

    Pro historii sčítá přenesené bajty a změřenou dobu všech platných kroků od
    posledního zápisu (`window_results`).
    """

    labels = LABELS

    def __init__(self, validity_seconds, interface=None, net_dir=DEFAULT_NET_DIR, route_path=DEFAULT_ROUTE_PATH,
                 ipv4_lookup=interface_ipv4, details_interval=None, gap_seconds=None):
        # Platnost dodává volající z [collector] stejně jako u SystemMonitor.
        if not is_finite_number(validity_seconds) or validity_seconds <= 0:
            raise ValueError(f"validity_seconds musí být kladný počet sekund, ne {validity_seconds!r}")
        if interface is not None and not is_valid_interface_name(interface):
            raise ValueError(f"neplatný název rozhraní {interface!r}")
        for name, value in (("details_interval", details_interval), ("gap_seconds", gap_seconds)):
            if value is not None and (not is_finite_number(value) or value <= 0):
                raise ValueError(f"{name} musí být kladný počet sekund, ne {value!r}")
        self.validity_seconds = validity_seconds
        self.interface = interface  # None = automatický výběr
        self.net_dir = Path(net_dir)
        self.route_path = Path(route_path)
        self.ipv4_lookup = ipv4_lookup
        self.details_interval = details_interval
        self.details_validity = (validity_seconds if details_interval is None
                                 else VALIDITY_INTERVALS * details_interval)
        self.gap_seconds = validity_seconds if gap_seconds is None else gap_seconds
        # Skutečné měření jen ze skutečného systému; vzorové soubory nebo
        # náhradní zjišťování adresy (testy) jsou fiktivní data.
        self.simulated = not (
            self.net_dir == Path(DEFAULT_NET_DIR)
            and self.route_path == Path(DEFAULT_ROUTE_PATH)
            and ipv4_lookup is interface_ipv4
        )
        self.previous = None
        self.details = None
        self.details_due = None
        # Okno zápisu historie (SCHED-2): [bajty, sekundy, začátek prvního kroku,
        # konec posledního] platných kroků, důvod přerušení a čas minulého zápisu.
        self.window = {check_id: [0, 0.0, None, None] for check_id in COUNTER_FILES}
        self.window_broken = dict.fromkeys(COUNTER_FILES)
        self.window_mark = None

    def collect(self, now, observed_utc):
        """Změří vybrané rozhraní. `now` je monotónní čas, `observed_utc` skutečný čas UTC.

        Rychlost se počítá z rozdílu čítačů a z rozdílu `now` mezi dvěma
        měřeními, tedy ze skutečně uplynulého času, ne z nominálního intervalu.
        """
        if self.details is None or self.details_interval is None or now >= self.details_due:
            self._refresh(now, observed_utc)
        details = self.details
        if details.reason is None:
            sample = self._sample(details.interface, now)
            if self.details_interval is not None and sample.ifindex != details.ifindex:
                # Rozhraní zmizelo nebo vzniklo znovu: výběr, linka a IPv4 hned,
                # ne až v dalším termínu (obnova si nový ifindex zapamatuje).
                self._refresh(now, observed_utc)
                details = self.details
        if details.reason is not None:
            # Bez rozhraní nejsou čítače: další nalezené rozhraní začne prvním vzorkem.
            self.previous = None
            prefix = "" if details.interface is None else f" {details.interface}"
            extra = {} if details.interface is None else {INTERFACE: details.interface}
            rates = []
            for check_id in COUNTER_FILES:
                self._break(check_id, details.reason)
                rates.append(self._result(check_id, State.UNKNOWN, f"{LABELS[check_id]}{prefix}: {details.reason}",
                                          now, observed_utc, details=extra))
            return [details.link, *rates, details.ipv4]
        previous, self.previous = self.previous, sample
        interface = details.interface
        return [
            details.link,
            self._check(RX_RATE_ID, interface, lambda: self._rate(RX_RATE_ID, previous, sample), now, observed_utc),
            self._check(TX_RATE_ID, interface, lambda: self._rate(TX_RATE_ID, previous, sample), now, observed_utc),
            details.ipv4,
        ]

    def window_results(self, now, observed_utc, validity_seconds):
        """Rychlost RX/TX za okno od posledního zápisu do historie (SCHED-2); okno začne znovu.

        Vrací (id nahrazených živých výsledků, výsledky). Rychlost = přenesené
        bajty / změřená doba všech platných kroků, tedy stejné měření jako dřív
        jeden 10s vzorek (a zároveň skutečný objem dat). Čas a platnost jsou
        okamžik zápisu (`now`, `observed_utc`) + `validity_seconds`; kroky musí
        přesně pokrývat úsek od minulého zápisu do `now`. Reset čítače, změna
        rozhraní, mezera, neplatná data nebo nepokrytá část okna dají UNKNOWN,
        nikdy falešnou rychlost. Okno bez kroků vrací nic (mezera), jen první
        okno je UNKNOWN „první vzorek“.
        """
        mark, self.window_mark = self.window_mark, now
        interface = None if self.details is None else self.details.interface
        details = {} if interface is None else {INTERFACE: interface}
        expires = now + validity_seconds
        results = []
        for check_id, (transferred, seconds, start, end) in self.window.items():
            broken, label = self.window_broken[check_id], LABELS[check_id]
            self.window[check_id], self.window_broken[check_id] = [0, 0.0, None, None], None
            if broken is not None:
                state, summary, measurement = State.UNKNOWN, f"{label}: {broken} v okně zápisu", None
            elif seconds > 0 and mark is not None and (start, end) != (mark, now):
                state, summary, measurement = State.UNKNOWN, f"{label}: měření nepokrývá celé okno zápisu", None
            elif seconds > 0:
                rate = transferred / seconds
                state, summary, measurement = State.INFO, f"{label} {format_rate(rate)}", Measurement(
                    rate, BYTES_PER_SECOND)
            elif mark is None:
                state, summary, measurement = State.UNKNOWN, f"{label}: první vzorek", None
            else:
                continue
            results.append(CheckResult(check_id, state, summary, observed_utc, expires, simulated=self.simulated,
                                       measurement=measurement, details=details))
        return set(COUNTER_FILES), results

    def _break(self, check_id, reason):
        """Okno zápisu obsahuje neplatný krok: celé okno bude UNKNOWN (první důvod zůstává)."""
        self.window_broken[check_id] = self.window_broken[check_id] or reason

    def _refresh(self, now, observed_utc):
        """Výběr rozhraní, linka a IPv4; výsledky platí `details_validity`."""
        if self.details_interval is not None:
            self.details_due = now + self.details_interval
        try:
            interface = self._select_interface()
        except Unavailable as reason:
            link, ipv4 = (self._result(check_id, State.UNKNOWN, f"{LABELS[check_id]}: {reason}", now, observed_utc,
                                       validity=self.details_validity) for check_id in (LINK_ID, IPV4_ID))
            self.details = Details(None, None, str(reason), link, ipv4)
            return
        if not (self.net_dir / interface).is_dir():
            # Nastavené rozhraní neexistuje (nebo zmizelo mezi výběrem a čtením).
            link, ipv4 = (self._result(check_id, State.UNKNOWN, f"{LABELS[check_id]} {interface}: rozhraní nenalezeno",
                                       now, observed_utc, details={INTERFACE: interface},
                                       validity=self.details_validity) for check_id in (LINK_ID, IPV4_ID))
            self.details = Details(interface, None, "rozhraní nenalezeno", link, ipv4)
            return
        link = self._check(LINK_ID, interface, lambda: self._link(interface), now, observed_utc,
                           validity=self.details_validity)
        ipv4 = self._check(IPV4_ID, interface, lambda: self._ipv4(interface), now, observed_utc,
                           validity=self.details_validity)
        self.details = Details(interface, self._ifindex(interface), None, link, ipv4)

    def _result(self, check_id, state, summary, now, observed_utc, measurement=None, details=None, validity=None):
        validity = self.validity_seconds if validity is None else validity
        return CheckResult(check_id, state, summary, observed_utc, now + validity,
                           simulated=self.simulated, measurement=measurement, details=details or {})

    def _check(self, check_id, interface, measure, now, observed_utc, validity=None):
        # Každá kontrola ošetřuje své chyby sama, aby selhání jedné neshodilo ostatní.
        # Název rozhraní zůstává v details i u UNKNOWN: víme, co měříme, jen ne
        # hodnotu. Ve výpisu ho nese řádek linky, ostatní by ho jen opakovaly.
        label = LABELS[check_id]
        if check_id == LINK_ID:
            label = f"{label} {interface}"
        measurement, details = None, {INTERFACE: interface}
        try:
            measurement, text, extra = measure()
            details.update(extra)
            state, summary = State.INFO, f"{label} {text}"
        except Unavailable as reason:
            state, summary = State.UNKNOWN, f"{label}: {reason}"
        except OSError:
            state, summary = State.UNKNOWN, f"{label}: nedostupné"
        except (ValueError, ArithmeticError):
            # ArithmeticError: obří čítač projde int(), ale dělení na float přeteče.
            state, summary = State.UNKNOWN, f"{label}: neplatná data"
        return self._result(check_id, state, summary, now, observed_utc, measurement, details, validity)

    def _select_interface(self):
        if self.interface is not None:
            return self.interface
        try:
            names = sorted(entry.name for entry in self.net_dir.iterdir())
        except OSError:
            raise Unavailable("seznam rozhraní nedostupný") from None
        # Soubory jako bonding_masters nejsou rozhraní a odkaz device nemají.
        physical = [name for name in names
                    if is_valid_interface_name(name) and (self.net_dir / name / "device").exists()]
        try:
            default_metrics = parse_default_routes(read_text(self.route_path))
        except (OSError, ValueError):
            # Tabulka tras je jen nápověda pro pořadí. Bez ní se volí podle linky;
            # zobrazený název rozhraní pak říká, čí hodnoty to jsou.
            default_metrics = {}
        links_up = {name for name in physical if self._link_or_none(name) == LINK_UP}
        chosen = choose_interface(physical, default_metrics, links_up)
        if chosen is None:
            raise Unavailable("žádné fyzické rozhraní")
        return chosen

    def _link_or_none(self, interface):
        try:
            return read_link_state(self.net_dir / interface)
        except (Unavailable, OSError, ValueError):
            return None

    def _ifindex(self, interface):
        try:
            return read_number(self.net_dir / interface / "ifindex")
        except (OSError, ValueError):
            return None

    def _sample(self, interface, now):
        statistics = self.net_dir / interface / "statistics"
        counters = {}
        for check_id, file_name in COUNTER_FILES.items():
            try:
                counters[check_id] = read_number(statistics / file_name)
            except (OSError, ValueError) as error:
                counters[check_id] = error  # ohlásí se u příslušné kontroly
        return Sample(interface, self._ifindex(interface), now, counters)

    # Každé měření vrací (Measurement nebo None, text pro výpis, další údaje details).
    def _link(self, interface):
        state = read_link_state(self.net_dir / interface)
        return None, state, {LINK: state}

    def _rate(self, check_id, previous, current):
        try:
            transferred, elapsed = self._transfer(check_id, previous, current)
        except FirstSample:
            raise  # okno tím nezačíná ani nekončí
        except Unavailable as reason:
            self._break(check_id, str(reason))
            raise
        except OSError:
            self._break(check_id, "nedostupné")
            raise
        except (ValueError, ArithmeticError):
            self._break(check_id, "neplatná data")
            raise
        window = self.window[check_id]
        window[0] += transferred
        window[1] += elapsed
        window[2] = previous.time if window[2] is None else window[2]
        window[3] = current.time
        rate = transferred / elapsed
        return Measurement(rate, BYTES_PER_SECOND), format_rate(rate), {}

    def _transfer(self, check_id, previous, current):
        """(přenesené bajty, uplynulé sekundy) mezi dvěma vzorky, nebo výjimka."""
        value = current.counters[check_id]
        if isinstance(value, Exception):
            raise value
        if previous is None:
            raise FirstSample("první vzorek")
        if (previous.interface, previous.ifindex) != (current.interface, current.ifindex):
            raise Unavailable("změna rozhraní")
        old = previous.counters[check_id]
        if isinstance(old, Exception):
            raise Unavailable("chybí předchozí vzorek")
        elapsed = current.time - previous.time
        if elapsed <= 0:
            raise Unavailable("neplatný interval")
        if elapsed > self.gap_seconds:
            # Po dlouhé mezeře (zaseknuté měření, uspání) by vyšel průměr
            # za dávno uplynulou dobu, ne aktuální rychlost.
            raise Unavailable("mezera v datech")
        counter_rate(old, value, elapsed)  # reset čítače je Unavailable
        return value - old, elapsed

    def _ipv4(self, interface):
        address = self.ipv4_lookup(interface)
        if address is None:
            return None, "bez adresy", {}
        # Na displej smí jen skutečná tečková adresa IPv4, nic jiného.
        address = str(ipaddress.IPv4Address(address))
        return None, address, {ADDRESS: address}


def main(argv=None):
    """Dva vzorky skutečného systému s odstupem 1 s a výpis výsledků."""
    parser = argparse.ArgumentParser(description="Read-only měření síťového rozhraní (NexusSTAT)")
    parser.add_argument("interface", nargs="?", help="sledované rozhraní; bez něj automatický výběr")
    args = parser.parse_args(argv)
    if args.interface is not None and not is_valid_interface_name(args.interface):
        parser.error(f"neplatný název rozhraní {args.interface!r}")
    monitor = NetworkMonitor(validity_seconds=CollectorConfig().validity_seconds, interface=args.interface)
    monitor.collect(time.monotonic(), datetime.now(timezone.utc))  # první vzorek čítačů
    time.sleep(1)
    for result in monitor.collect(time.monotonic(), datetime.now(timezone.utc)):
        details = ", ".join(f"{key}={value}" for key, value in result.details.items())
        print(f"{result.id:<16} {result.state.value:<8} {result.summary}  ({details}; simulated={result.simulated})")


if __name__ == "__main__":
    main()
