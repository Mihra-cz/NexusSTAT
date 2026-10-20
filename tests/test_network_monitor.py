"""Testy read-only síťového kolektoru nad připravenými vzorovými soubory.

Nečtou skutečný /sys/class/net ani /proc/net/route a nevolají ioctl: každý
stav sítě (rozhraní, linka, čítače, trasy, adresy) je ve fixturách, takže
výsledek nezávisí na síti počítače, na kterém testy běží. Čas je simulovaný.
Adresy jsou z dokumentačních rozsahů RFC 5737 (192.0.2.0/24, 198.51.100.0/24).
"""

import errno
import ipaddress
import re
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from nexusstat.config import CollectorConfig, OledConfig
from nexusstat.core import CheckResult, State, active_problems, effective, overall_state
from nexusstat.network_monitor import (
    ADDRESS,
    BYTES_PER_SECOND,
    INTERFACE,
    IPV4_ID,
    LABELS,
    LINK,
    LINK_ID,
    RX_RATE_ID,
    TX_RATE_ID,
    NetworkMonitor,
    choose_interface,
    counter_rate,
    format_rate,
    parse_default_routes,
    read_link_state,
)
from nexusstat.oled import UNKNOWN_SYMBOL, OledController, home_symbol
from nexusstat.oled_text import network_rows
from nexusstat.preview import LIVE, SCENARIOS, PreviewSession
from nexusstat.simulate import sample_results
from nexusstat.system_monitor import Unavailable

try:
    from PIL import Image

    from nexusstat import render
    from nexusstat.render import render_screen
except ImportError:  # Pillow není nainstalované
    render_screen = None

REPO = Path(__file__).resolve().parent.parent

OBSERVED = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
WALL_TIME = datetime(2026, 10, 20, 14, 0)
VALIDITY_SECONDS = CollectorConfig().validity_seconds  # 30 s

ROUTE_HEADER = "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"


def route_line(interface, destination="00000000", flags="0003", metric=100, mask="00000000"):
    return f"{interface}\t{destination}\t010200C0\t{flags}\t0\t0\t{metric}\t{mask}\t0\t0\t0\n"


class NetFixture:
    """Dočasná kopie /sys/class/net a /proc/net/route a náhradní zjišťování IPv4."""

    def __init__(self, base):
        self.net = Path(base) / "net"
        self.route = Path(base) / "route"
        self.net.mkdir()
        self.addresses = {}  # rozhraní -> adresa, None = bez IPv4, výjimka = selhání
        self.lookups = []
        self.set_routes()

    def add(self, name, physical=True, operstate="up", carrier="1", rx=0, tx=0, ifindex=2):
        directory = self.net / name
        (directory / "statistics").mkdir(parents=True)
        if physical:
            (directory / "device").mkdir()  # v /sys je to odkaz na hardware
        self.write(name, operstate=operstate, carrier=carrier, rx=rx, tx=tx, ifindex=ifindex)

    def write(self, name, **values):
        directory = self.net / name
        files = {"operstate": "operstate", "carrier": "carrier", "ifindex": "ifindex",
                 "rx": "statistics/rx_bytes", "tx": "statistics/tx_bytes"}
        for key, value in values.items():
            path = directory / files[key]
            if value is None:
                path.unlink(missing_ok=True)  # soubor chybí nebo nejde přečíst
            else:
                path.write_text(f"{value}\n", encoding="ascii")

    def set_routes(self, *lines):
        self.route.write_text(ROUTE_HEADER + "".join(lines), encoding="ascii")

    def ipv4(self, name):
        self.lookups.append(name)
        address = self.addresses.get(name)
        if isinstance(address, Exception):
            raise address
        return address

    def monitor(self, interface=None):
        return NetworkMonitor(VALIDITY_SECONDS, interface=interface, net_dir=self.net, route_path=self.route,
                              ipv4_lookup=self.ipv4)


class FixtureCase(unittest.TestCase):
    """Notebook se vším, co se na skutečném stroji vyskytuje: Ethernet bez kabelu,
    Wi-Fi s výchozí trasou, modem, loopback, Docker, VPN a soubor bonding_masters."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.fx = NetFixture(tmp.name)
        self.fx.add("enp0s31f6", operstate="down", carrier="0", ifindex=2)
        self.fx.add("wlp2s0", rx=1_000_000, tx=200_000, ifindex=3)
        self.fx.add("wwan0", operstate="down", carrier=None, ifindex=4)
        self.fx.add("lo", physical=False, operstate="unknown", rx=5_000, tx=5_000, ifindex=1)
        self.fx.add("docker0", physical=False, rx=9_000_000, tx=9_000_000, ifindex=5)
        self.fx.add("veth1a2b3c", physical=False, ifindex=6)
        self.fx.add("wg0", physical=False, operstate="unknown", ifindex=7)
        (self.fx.net / "bonding_masters").write_text("\n", encoding="ascii")
        # Trasa podsítě Dockeru: dokumentační 198.51.100.0/24 (RFC 5737), ne skutečná podsíť.
        self.fx.set_routes(route_line("wlp2s0", metric=600),
                           route_line("docker0", destination="006433C6", flags="0001", metric=0, mask="00FFFFFF"))
        self.fx.addresses = {"wlp2s0": "192.0.2.23", "enp0s31f6": None, "docker0": "198.51.100.1"}

    def by_id(self, results):
        return {result.id: result for result in results}


class ParseRoutesTest(unittest.TestCase):
    def test_default_routes_with_lowest_metric(self):
        text = ROUTE_HEADER + route_line("eth0", metric=202) + route_line("wlan0", metric=303) \
            + route_line("eth0", metric=100) \
            + route_line("eth0", destination="000200C0", flags="0001", metric=0, mask="00FFFFFF")
        self.assertEqual(parse_default_routes(text), {"eth0": 100, "wlan0": 303})

    def test_inactive_and_non_default_routes_are_ignored(self):
        text = ROUTE_HEADER + route_line("eth0", flags="0002") \
            + route_line("tun0", destination="00000000", mask="00000080")  # 0.0.0.0/1 (VPN), ne výchozí
        self.assertEqual(parse_default_routes(text), {})

    def test_empty_table(self):
        self.assertEqual(parse_default_routes(ROUTE_HEADER), {})

    def test_invalid_table(self):
        for text in ("", "nesmysl\n", ROUTE_HEADER + "eth0\t00000000\n", ROUTE_HEADER + route_line("eth0", metric="x"),
                     ROUTE_HEADER + route_line("eth0", flags="zz")):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    parse_default_routes(text)


class ChooseInterfaceTest(unittest.TestCase):
    def test_default_route_wins_over_link_and_name(self):
        self.assertEqual(choose_interface(["eth0", "wlan0"], {"wlan0": 600}, {"eth0", "wlan0"}), "wlan0")

    def test_lowest_metric_then_name(self):
        self.assertEqual(choose_interface(["eth0", "wlan0"], {"eth0": 100, "wlan0": 600}, set()), "eth0")
        self.assertEqual(choose_interface(["eth1", "eth0"], {"eth0": 100, "eth1": 100}, set()), "eth0")

    def test_without_route_first_up_by_name(self):
        self.assertEqual(choose_interface(["eth0", "wlan1", "wlan0"], {}, {"wlan1", "wlan0"}), "wlan0")

    def test_without_route_and_link_first_by_name(self):
        self.assertEqual(choose_interface(["wlan0", "end0"], {}, set()), "end0")

    def test_route_on_other_interface_is_ignored(self):
        # Výchozí trasa přes virtuální rozhraní (VPN) mezi fyzickými není.
        self.assertEqual(choose_interface(["eth0"], {"wg0": 50}, {"eth0"}), "eth0")

    def test_nothing_to_choose(self):
        self.assertIsNone(choose_interface([], {"wg0": 50}, {"wg0"}))

    def test_order_of_input_does_not_matter(self):
        names = ["wlan0", "eth1", "eth0", "end0"]
        expected = choose_interface(names, {"eth1": 5, "eth0": 5}, {"wlan0"})
        for rotation in range(len(names)):
            with self.subTest(rotation=rotation):
                self.assertEqual(choose_interface(names[rotation:] + names[:rotation], {"eth1": 5, "eth0": 5},
                                                  {"wlan0"}), expected)
        self.assertEqual(expected, "eth0")


class LinkStateTest(FixtureCase):
    def state(self, operstate, carrier="1"):
        self.fx.write("wlp2s0", operstate=operstate, carrier=carrier)
        return read_link_state(self.fx.net / "wlp2s0")

    def test_operstate_mapping(self):
        self.assertEqual(self.state("up"), "UP")
        for operstate in ("down", "lowerlayerdown", "notpresent", "dormant"):
            with self.subTest(operstate=operstate):
                self.assertEqual(self.state(operstate), "DOWN")

    def test_unknown_operstate_uses_carrier(self):
        self.assertEqual(self.state("unknown", carrier="1"), "UP")
        self.assertEqual(self.state("unknown", carrier="0"), "DOWN")
        with self.assertRaises(OSError):
            self.state("unknown", carrier=None)  # vypnuté rozhraní: carrier nejde přečíst
        with self.assertRaises(ValueError):
            self.state("unknown", carrier="2")

    def test_administrative_and_operational_state_are_not_confused(self):
        # Skutečné chování jádra (ověřeno read-only na notebooku): administrativně
        # vypnuté rozhraní (IFF_UP = 0) má operstate "down" a carrier nejde přečíst;
        # zapnuté bez kabelu (IFF_UP = 1) má operstate "down" a carrier 0.
        # Příznak IFF_UP nikdy neznamená linku UP; flags se vůbec nečtou.
        cases = {
            "admin down": ("down", None, "0x1082"),
            "admin up, bez kabelu": ("down", "0", "0x1003"),
            "admin up, nižší vrstva dole": ("lowerlayerdown", "0", "0x1003"),
            "admin up, Wi-Fi nepřihlášená": ("dormant", "1", "0x1003"),
        }
        for name, (operstate, carrier, flags) in cases.items():
            (self.fx.net / "wlp2s0" / "flags").write_text(flags + "\n", encoding="ascii")
            with self.subTest(name):
                self.assertEqual(self.state(operstate, carrier), "DOWN")
                link = self.fx.monitor("wlp2s0").collect(100, OBSERVED)[0]
                self.assertEqual((link.state, link.details[LINK]), (State.INFO, "DOWN"))

    def test_testing_and_garbage(self):
        with self.assertRaises(Unavailable):
            self.state("testing")
        with self.assertRaises(ValueError):
            self.state("UPP")


class RateMathTest(unittest.TestCase):
    def test_rate_from_counter_delta_and_elapsed_time(self):
        self.assertEqual(counter_rate(1_000, 13_000, 10), 1_200)
        self.assertEqual(counter_rate(1_000, 13_000, 12.5), 960)  # skutečně uplynulý čas, ne interval
        self.assertEqual(counter_rate(5, 5, 10), 0)

    def test_decreased_counter_is_reset(self):
        with self.assertRaises(Unavailable):
            counter_rate(2**32 - 10, 100, 10)  # přetečení 32bitového čítače vypadá jako reset

    def test_terminal_format(self):
        self.assertEqual([format_rate(v) for v in (0, 999, 1_500, 85_300, 1_234_000)],
                         ["0 B/s", "999 B/s", "1,5 kB/s", "85,3 kB/s", "1,2 MB/s"])



class SelectionTest(FixtureCase):
    def interface(self, monitor=None):
        results = (monitor or self.fx.monitor()).collect(100, OBSERVED)
        return {result.details.get(INTERFACE) for result in results}

    def test_auto_selects_physical_with_default_route(self):
        self.assertEqual(self.interface(), {"wlp2s0"})

    def test_virtual_interfaces_are_never_auto_selected(self):
        # Docker má nejvíc provozu, loopback a VPN jsou UP, nic z toho se nevybere.
        self.fx.set_routes(route_line("wg0", metric=10), route_line("docker0", metric=0))
        self.fx.write("wlp2s0", operstate="down")
        self.assertEqual(self.interface(), {"enp0s31f6"})  # bez trasy a linky abecedně první fyzické

    def test_without_route_prefers_link_up(self):
        self.fx.set_routes()
        self.assertEqual(self.interface(), {"wlp2s0"})

    def test_unreadable_route_table_falls_back_to_link(self):
        self.fx.route.unlink()
        self.assertEqual(self.interface(), {"wlp2s0"})
        self.fx.route.write_text("garbage\n", encoding="ascii")
        self.assertEqual(self.interface(), {"wlp2s0"})

    def test_lower_metric_wins_when_both_have_route(self):
        self.fx.write("enp0s31f6", operstate="up", carrier="1")
        self.fx.set_routes(route_line("wlp2s0", metric=600), route_line("enp0s31f6", metric=100))
        self.assertEqual(self.interface(), {"enp0s31f6"})

    def test_configured_interface_is_used_even_if_virtual(self):
        self.assertEqual(self.interface(self.fx.monitor("docker0")), {"docker0"})
        self.assertEqual(self.interface(self.fx.monitor("enp0s31f6")), {"enp0s31f6"})

    def test_configured_interface_never_falls_back(self):
        # Regrese pro revizi: nastavené rozhraní při výpadku linky ani při zmizení
        # nikdy nevystřídá jiné, i když jiné má výchozí trasu a linku UP.
        monitor = self.fx.monitor("enp0s31f6")  # Ethernet bez kabelu, Wi-Fi je UP s výchozí trasou
        results = monitor.collect(100, OBSERVED)
        self.assertEqual({r.details[INTERFACE] for r in results}, {"enp0s31f6"})
        self.assertEqual(results[0].details[LINK], "DOWN")
        self.assertEqual(self.fx.lookups, ["enp0s31f6"])
        (self.fx.net / "enp0s31f6").rename(self.fx.net.parent / "odpojeno")
        results = monitor.collect(110, OBSERVED)
        self.assertEqual({r.state for r in results}, {State.UNKNOWN})
        self.assertEqual({r.details[INTERFACE] for r in results}, {"enp0s31f6"})
        self.assertEqual(self.fx.lookups, ["enp0s31f6"])  # adresa jiného rozhraní se nezjišťuje
        self.assertEqual(network_rows(results, 110)[0], ("ENP0S31F6", "UNKN"))

    def test_interface_with_unsafe_name_is_skipped(self):
        self.fx.set_routes(route_line("eth weird", metric=1))
        (self.fx.net / "eth weird" / "device").mkdir(parents=True)
        self.assertEqual(self.interface(), {"wlp2s0"})

    def test_no_physical_interface(self):
        for name in ("enp0s31f6", "wlp2s0", "wwan0"):
            (self.fx.net / name / "device").rmdir()
        results = self.fx.monitor().collect(100, OBSERVED)
        self.assertEqual([r.id for r in results], list(LABELS))
        self.assertEqual({r.state for r in results}, {State.UNKNOWN})
        self.assertEqual(results[0].summary, "Síť: žádné fyzické rozhraní")
        self.assertEqual({len(r.details) for r in results}, {0})
        self.assertEqual(self.fx.lookups, [])  # bez rozhraní se adresa nezjišťuje

    def test_missing_net_directory(self):
        monitor = NetworkMonitor(VALIDITY_SECONDS, net_dir=self.fx.net / "neexistuje", route_path=self.fx.route,
                                 ipv4_lookup=self.fx.ipv4)
        results = monitor.collect(100, OBSERVED)
        self.assertEqual({r.state for r in results}, {State.UNKNOWN})
        self.assertIn("nedostupný", results[0].summary)

    def test_configured_interface_missing(self):
        results = self.fx.monitor("eth1").collect(100, OBSERVED)
        self.assertEqual({r.state for r in results}, {State.UNKNOWN})
        self.assertEqual({r.details[INTERFACE] for r in results}, {"eth1"})
        self.assertEqual(results[0].summary, "Síť eth1: rozhraní nenalezeno")
        self.assertEqual(network_rows(results, 100),
                         [("ETH1", "UNKN"), ("RX", "UNKN"), ("TX", "UNKN"), ("IPV4", "UNKN")])


class CollectTest(FixtureCase):
    def test_four_results_with_contract(self):
        results = self.fx.monitor().collect(100, OBSERVED)
        self.assertEqual([r.id for r in results], [LINK_ID, RX_RATE_ID, TX_RATE_ID, IPV4_ID])
        for result in results:
            with self.subTest(check=result.id):
                self.assertIsInstance(result, CheckResult)
                self.assertEqual(result.observed_utc, OBSERVED)
                self.assertEqual(result.expires_monotonic, 100 + VALIDITY_SECONDS)
                self.assertTrue(result.simulated)  # vzorové soubory nejsou skutečné měření
                self.assertEqual(result.details[INTERFACE], "wlp2s0")
        link, rx, tx, ipv4 = results
        self.assertEqual((link.state, link.details[LINK], link.summary), (State.INFO, "UP", "Síť wlp2s0 UP"))
        self.assertEqual((ipv4.state, ipv4.details[ADDRESS], ipv4.summary), (State.INFO, "192.0.2.23", "IPv4 192.0.2.23"))
        self.assertEqual(self.fx.lookups, ["wlp2s0"])

    def test_first_sample_has_no_rate(self):
        by_id = self.by_id(self.fx.monitor().collect(100, OBSERVED))
        for check_id in (RX_RATE_ID, TX_RATE_ID):
            with self.subTest(check=check_id):
                self.assertIs(by_id[check_id].state, State.UNKNOWN)
                self.assertIsNone(by_id[check_id].measurement)
                self.assertIn("první vzorek", by_id[check_id].summary)

    def test_rate_from_two_samples_and_real_elapsed_time(self):
        monitor = self.fx.monitor()
        monitor.collect(100, OBSERVED)
        self.fx.write("wlp2s0", rx=1_000_000 + 125_000, tx=200_000 + 2_500)
        by_id = self.by_id(monitor.collect(112.5, OBSERVED))  # o 2,5 s později než nominálních 10 s
        self.assertEqual(by_id[RX_RATE_ID].measurement.value, 10_000)  # 125 000 B / 12,5 s
        self.assertEqual(by_id[RX_RATE_ID].measurement.unit, BYTES_PER_SECOND)
        self.assertEqual(by_id[TX_RATE_ID].measurement.value, 200)
        self.assertEqual(by_id[RX_RATE_ID].summary, "Síť RX 10,0 kB/s")
        self.assertEqual(network_rows(list(by_id.values()), 112.5)[1:3], [("RX", "10 KB/s"), ("TX", "200 B/s")])

    def test_traffic_is_not_summed_over_interfaces(self):
        monitor = self.fx.monitor()
        monitor.collect(100, OBSERVED)
        self.fx.write("docker0", rx=99_000_000, tx=99_000_000)  # provoz kontejnerů se nepřičte
        self.fx.write("wlp2s0", rx=1_010_000, tx=200_000)
        by_id = self.by_id(monitor.collect(110, OBSERVED))
        self.assertEqual((by_id[RX_RATE_ID].measurement.value, by_id[TX_RATE_ID].measurement.value), (1_000, 0))

    def test_counter_reset_is_unknown_then_recovers(self):
        monitor = self.fx.monitor()
        monitor.collect(100, OBSERVED)
        self.fx.write("wlp2s0", rx=500, tx=300_000)  # RX se snížil, TX roste
        by_id = self.by_id(monitor.collect(110, OBSERVED))
        self.assertIs(by_id[RX_RATE_ID].state, State.UNKNOWN)
        self.assertIn("reset čítače", by_id[RX_RATE_ID].summary)
        self.assertEqual(by_id[TX_RATE_ID].measurement.value, 10_000)  # druhý směr platí dál
        self.fx.write("wlp2s0", rx=10_500)
        by_id = self.by_id(monitor.collect(120, OBSERVED))
        self.assertEqual(by_id[RX_RATE_ID].measurement.value, 1_000)  # od nového základu

    def test_interface_change_restarts_rate(self):
        monitor = self.fx.monitor()
        monitor.collect(100, OBSERVED)
        self.fx.write("enp0s31f6", operstate="up", rx=5_000_000, tx=5_000_000)
        self.fx.set_routes(route_line("enp0s31f6", metric=100), route_line("wlp2s0", metric=600))
        by_id = self.by_id(monitor.collect(110, OBSERVED))
        self.assertEqual(by_id[LINK_ID].details[INTERFACE], "enp0s31f6")
        for check_id in (RX_RATE_ID, TX_RATE_ID):
            self.assertIn("změna rozhraní", by_id[check_id].summary)
        self.fx.write("enp0s31f6", rx=5_010_000)
        by_id = self.by_id(monitor.collect(120, OBSERVED))
        self.assertEqual(by_id[RX_RATE_ID].measurement.value, 1_000)

    def test_recreated_interface_with_same_name_restarts_rate(self):
        # Stejný název, jiný ifindex (např. znovu připojený USB adaptér): čítače
        # začaly od nuly a nižší rozdíl by vypadal platně.
        monitor = self.fx.monitor()
        monitor.collect(100, OBSERVED)
        self.fx.write("wlp2s0", ifindex=9, rx=1_500_000, tx=250_000)
        by_id = self.by_id(monitor.collect(110, OBSERVED))
        self.assertIs(by_id[RX_RATE_ID].state, State.UNKNOWN)
        self.assertIn("změna rozhraní", by_id[RX_RATE_ID].summary)

    def test_interface_disappears_and_returns(self):
        monitor = self.fx.monitor("wlp2s0")
        monitor.collect(100, OBSERVED)
        moved = self.fx.net / "wlp2s0"
        moved.rename(self.fx.net.parent / "odpojeno")
        by_id = self.by_id(monitor.collect(110, OBSERVED))
        self.assertEqual({r.state for r in by_id.values()}, {State.UNKNOWN})
        (self.fx.net.parent / "odpojeno").rename(moved)
        by_id = self.by_id(monitor.collect(120, OBSERVED))
        self.assertIn("první vzorek", by_id[RX_RATE_ID].summary)  # žádný rozdíl přes výpadek

    def test_invalid_and_long_intervals(self):
        monitor = self.fx.monitor()
        monitor.collect(100, OBSERVED)
        by_id = self.by_id(monitor.collect(100, OBSERVED))  # stejný okamžik
        self.assertIn("neplatný interval", by_id[RX_RATE_ID].summary)
        by_id = self.by_id(monitor.collect(100 + VALIDITY_SECONDS + 1, OBSERVED))  # zaseknuté měření, uspání
        self.assertIn("mezera v datech", by_id[RX_RATE_ID].summary)
        self.fx.write("wlp2s0", rx=1_300_000)
        by_id = self.by_id(monitor.collect(131 + VALIDITY_SECONDS, OBSERVED))  # právě 30 s: ještě platí
        self.assertEqual(by_id[RX_RATE_ID].measurement.value, 10_000)

    def test_link_down_is_valid_measurement_not_fault(self):
        self.fx.write("wlp2s0", operstate="down", carrier="0")
        self.fx.addresses["wlp2s0"] = None
        self.fx.set_routes()
        results = self.fx.monitor("wlp2s0").collect(100, OBSERVED)
        link, _, _, ipv4 = results
        self.assertEqual((link.state, link.details[LINK]), (State.INFO, "DOWN"))
        self.assertEqual((ipv4.state, ipv4.summary, ADDRESS in ipv4.details), (State.INFO, "IPv4 bez adresy", False))
        self.assertEqual(network_rows(results, 100)[0], ("WLP2S0", "DOWN"))
        self.assertEqual(network_rows(results, 100)[3], ("IPV4", "NONE"))

    def test_link_unknown_variants(self):
        cases = {"testing": "linka v testovacím režimu", "garbage": "neplatná data"}
        for operstate, reason in cases.items():
            self.fx.write("wlp2s0", operstate=operstate)
            link = self.fx.monitor("wlp2s0").collect(100, OBSERVED)[0]
            with self.subTest(operstate=operstate):
                self.assertIs(link.state, State.UNKNOWN)
                self.assertEqual(link.summary, f"Síť wlp2s0: {reason}")
                self.assertNotIn(LINK, link.details)
        self.fx.write("wlp2s0", operstate=None)
        self.assertEqual(self.fx.monitor("wlp2s0").collect(100, OBSERVED)[0].summary, "Síť wlp2s0: nedostupné")

    def test_modem_without_carrier_is_unknown_link(self):
        self.fx.write("wwan0", operstate="unknown")  # carrier chybí
        link = self.fx.monitor("wwan0").collect(100, OBSERVED)[0]
        self.assertEqual((link.state, link.summary), (State.UNKNOWN, "Síť wwan0: nedostupné"))

    def test_ipv4_failures(self):
        cases = {
            OSError(errno.ENODEV, "No such device"): "IPv4: nedostupné",
            "999.1.2.3": "IPv4: neplatná data",
            "fe80::1": "IPv4: neplatná data",
        }
        for answer, summary in cases.items():
            self.fx.addresses["wlp2s0"] = answer
            by_id = self.by_id(self.fx.monitor().collect(100, OBSERVED))
            with self.subTest(answer=answer):
                self.assertEqual((by_id[IPV4_ID].state, by_id[IPV4_ID].summary), (State.UNKNOWN, summary))
                self.assertNotIn(ADDRESS, by_id[IPV4_ID].details)
                self.assertIs(by_id[LINK_ID].state, State.INFO)  # ostatní kontroly platí dál

    def test_lost_or_unknown_address_is_never_carried_over(self):
        # Adresa je jen z aktuálního dotazu: ztracená IPv4 je NONE, chyba UNKN,
        # nikdy adresa z předchozího měření.
        monitor = self.fx.monitor()
        self.assertEqual(monitor.collect(100, OBSERVED)[3].details[ADDRESS], "192.0.2.23")
        self.fx.addresses["wlp2s0"] = None
        results = monitor.collect(110, OBSERVED)
        self.assertNotIn(ADDRESS, results[3].details)
        self.assertEqual(network_rows(results, 110)[3], ("IPV4", "NONE"))
        self.fx.addresses["wlp2s0"] = OSError(errno.ENODEV, "No such device")
        results = monitor.collect(120, OBSERVED)
        self.assertNotIn(ADDRESS, results[3].details)
        self.assertEqual(network_rows(results, 120)[3], ("IPV4", "UNKN"))

    def test_missing_or_corrupted_counters(self):
        cases = {None: "nedostupné", "abc": "neplatná data", "-5": "neplatná data", "1" + "0" * 400: "neplatná data"}
        for value, reason in cases.items():
            monitor = self.fx.monitor()
            self.fx.write("wlp2s0", rx=1_000_000)
            monitor.collect(100, OBSERVED)
            self.fx.write("wlp2s0", rx=value)
            by_id = self.by_id(monitor.collect(110, OBSERVED))
            with self.subTest(value=str(value)[:10]):
                self.assertEqual(by_id[RX_RATE_ID].summary, f"Síť RX: {reason}")
                self.assertIs(by_id[TX_RATE_ID].state, State.INFO)
                self.assertIs(by_id[LINK_ID].state, State.INFO)

    def test_huge_counter_jump_is_invalid_not_crash(self):
        monitor = self.fx.monitor()
        self.fx.write("wlp2s0", rx=0)
        monitor.collect(100, OBSERVED)
        self.fx.write("wlp2s0", rx="9" * 400)
        rx = self.by_id(monitor.collect(110, OBSERVED))[RX_RATE_ID]
        self.assertEqual((rx.state, rx.summary), (State.UNKNOWN, "Síť RX: neplatná data"))

    def test_counter_unreadable_in_previous_sample(self):
        monitor = self.fx.monitor()
        self.fx.write("wlp2s0", tx=None)
        monitor.collect(100, OBSERVED)
        self.fx.write("wlp2s0", tx=210_000)
        by_id = self.by_id(monitor.collect(110, OBSERVED))
        self.assertIn("chybí předchozí vzorek", by_id[TX_RATE_ID].summary)
        self.assertIs(by_id[RX_RATE_ID].state, State.INFO)


class ValidityTest(FixtureCase):
    def test_results_expire_to_unknown_without_old_values(self):
        monitor = self.fx.monitor()
        monitor.collect(100, OBSERVED)
        self.fx.write("wlp2s0", rx=1_010_000)
        results = monitor.collect(110, OBSERVED)
        self.assertEqual(network_rows(results, 139.9)[0], ("WLP2S0", "UP"))
        expired = [effective(r, 140) for r in results]
        self.assertEqual({r.state for r in expired}, {State.UNKNOWN})
        self.assertTrue(all(r.measurement is None and not r.details for r in expired))
        self.assertEqual(network_rows(results, 140),
                         [("LINK", "UNKN"), ("RX", "UNKN"), ("TX", "UNKN"), ("IPV4", "UNKN")])
        self.assertEqual(home_symbol(results, 140), UNKNOWN_SYMBOL)

    def test_validity_is_required_and_finite(self):
        for validity in (0, -1, float("nan"), float("inf"), True, "30"):
            with self.subTest(validity=validity):
                with self.assertRaises(ValueError):
                    NetworkMonitor(validity)

    def test_invalid_configured_name(self):
        for name in ("", "eth/0", "a" * 16, ".."):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    NetworkMonitor(VALIDITY_SECONDS, interface=name)


class SimulatedFlagTest(unittest.TestCase):
    def test_real_sources_are_not_simulated(self):
        # Jen vytvoření: konstruktor nic nečte ani nevolá ioctl.
        self.assertFalse(NetworkMonitor(VALIDITY_SECONDS).simulated)
        self.assertFalse(NetworkMonitor(VALIDITY_SECONDS, net_dir="/sys/class/net/", route_path=Path("/proc/net/route"))
                         .simulated)

    def test_any_replaced_source_is_simulated(self):
        replaced = ({"net_dir": "/tmp/net"}, {"route_path": "/tmp/route"}, {"ipv4_lookup": lambda name: None})
        for kwargs in replaced:
            with self.subTest(kwargs=list(kwargs)):
                self.assertTrue(NetworkMonitor(VALIDITY_SECONDS, **kwargs).simulated)


class CoreAndOledIntegrationTest(FixtureCase):
    def test_valid_network_gives_no_problem_and_no_symbol(self):
        monitor = self.fx.monitor()
        monitor.collect(100, OBSERVED)
        results = monitor.collect(110, OBSERVED)
        self.assertEqual(active_problems(results, 110), [])
        self.assertIs(overall_state(results, 110), State.INFO)
        self.assertIsNone(home_symbol(results, 110))

    def test_first_sample_shows_question_mark_and_status_rows(self):
        results = self.fx.monitor().collect(100, OBSERVED)
        self.assertEqual(home_symbol(results, 100), UNKNOWN_SYMBOL)
        self.assertEqual([p.id for p in active_problems(results, 100)], [RX_RATE_ID, TX_RATE_ID])

    @unittest.skipIf(render_screen is None, "Pillow není dostupné")
    def test_down_is_visible_on_oled(self):
        # Regrese pro revizi: DOWN z kolektoru se na stránce NETWORK opravdu nakreslí
        # (pixelově stejně jako text "DOWN" vpravo v řádku linky) a liší se od UP.
        self.fx.write("wlp2s0", operstate="down", carrier="0")
        self.fx.addresses["wlp2s0"] = None
        results = self.fx.monitor("wlp2s0").collect(100, OBSERVED)
        controller = OledController(OledConfig())
        controller.press(70, problem_count=0)
        controller.update(100)
        self.assertEqual(controller.current_page(100), "NETWORK")
        image = render_screen(controller, 100, results, WALL_TIME)
        expected = Image.new("1", (128, 64))
        render.draw_network_page(expected, [("WLP2S0", "DOWN"), ("RX", "UNKN"), ("TX", "UNKN"), ("IPV4", "NONE")], True)
        self.assertEqual(image.tobytes(), expected.tobytes())
        up = Image.new("1", (128, 64))
        render.draw_network_page(up, [("WLP2S0", "UP"), ("RX", "UNKN"), ("TX", "UNKN"), ("IPV4", "NONE")], True)
        row = (0, render.SERVICES_ROW_TOP, 128, render.SERVICES_ROW_TOP + 13)
        self.assertNotEqual(image.crop(row).tobytes(), up.crop(row).tobytes())

    @unittest.skipIf(render_screen is None, "Pillow není dostupné")
    def test_render_network_page_with_sim_mark(self):
        monitor = self.fx.monitor()
        monitor.collect(100, OBSERVED)
        results = monitor.collect(110, OBSERVED)
        controller = OledController(OledConfig())
        controller.press(80, problem_count=0)
        controller.update(110)
        self.assertEqual(controller.current_page(110), "NETWORK")
        image = render_screen(controller, 110, results, WALL_TIME)
        self.assertEqual((image.size, image.mode), ((128, 64), "1"))
        self.assertIsNotNone(image.crop((100, 0, 128, 13)).getbbox())  # SIM: vzorová data



# Fiktivní adresy jen z dokumentačních rozsahů RFC 5737; navíc smí zůstat obecné
# speciální adresy, které nepatří žádnému stroji ani síti.
DOCUMENTATION_NETWORKS = [ipaddress.ip_network(net) for net in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")]
GENERIC_ADDRESSES = {"0.0.0.0", "127.0.0.1", "255.255.255.255"}
PUBLIC_FILES = ["README.md", "manifest.toml", "config", "docs", "src", "tests", "tools"]


def is_documentation(address):
    return any(ipaddress.ip_address(address) in network for network in DOCUMENTATION_NETWORKS)


class PublicDataTest(unittest.TestCase):
    """Regrese pro revizi: do veřejného repozitáře, fixtur ani simulace se nesmí
    dostat skutečná IP adresa (ani podsíť z notebooku, např. Dockeru)."""

    def files(self):
        for name in PUBLIC_FILES:
            path = REPO / name
            for file in sorted(path.rglob("*") if path.is_dir() else [path]):
                if file.is_file() and file.suffix in (".py", ".md", ".toml"):
                    yield file

    def test_dotted_addresses_are_documentation_or_generic(self):
        for file in self.files():
            text = file.read_text(encoding="utf-8")
            for address in re.findall(r"(?<![\d.])\d{1,3}(?:\.\d{1,3}){3}(?![\d.])", text):
                if any(int(part) > 255 for part in address.split(".")):
                    continue  # záměrně neplatná adresa v testu (např. 999.1.2.3)
                with self.subTest(file=str(file.relative_to(REPO)), address=address):
                    self.assertTrue(address in GENERIC_ADDRESSES or is_documentation(address))

    def test_route_fixtures_use_documentation_addresses(self):
        # /proc/net/route má adresy šestnáctkově (little-endian): tečkové hledání je nenajde.
        text = (REPO / "tests" / "test_network_monitor.py").read_text(encoding="utf-8")
        for value in set(re.findall(r'"([0-9A-F]{8})"', text)):
            address = ipaddress.IPv4Address(int(value, 16).to_bytes(4, "little"))
            is_netmask = bin((int(address) ^ 0xFFFFFFFF) + 1).count("1") == 1  # souvislá maska
            with self.subTest(value=value, address=str(address)):
                self.assertTrue(is_netmask or is_documentation(address), address)

    def test_simulated_addresses_are_documentation_range(self):
        addresses = [r.details[ADDRESS] for r in sample_results(OBSERVED) if ADDRESS in r.details]
        for name in SCENARIOS:
            if name != LIVE:
                session = PreviewSession(scenario=name)
                addresses += [r.details[ADDRESS] for r in session.results if ADDRESS in r.details]
        self.assertTrue(addresses)
        for address in addresses:
            with self.subTest(address=address):
                self.assertIn(ipaddress.ip_address(address), ipaddress.ip_network("192.0.2.0/24"))


if __name__ == "__main__":
    unittest.main()
