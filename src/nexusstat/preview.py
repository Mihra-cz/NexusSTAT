"""Živý grafický simulátor OLED 128 × 64 a čtyř RGB LED pro vývojový počítač (tkinter).

Spuštění:
  PYTHONPATH=src python3 -m nexusstat.preview [--scenario NÁZEV] [--scale 4|6] [--config SOUBOR]

Obraz kreslí výhradně render.render_screen(); simulátor jen řídí čas,
tlačítko, scénáře a zvětšení. LED jsou samostatná plocha pod displejem
a jejich barvu určuje rgb.py, do bitmapy OLED se nekreslí. Nepřistupuje
k hardwaru displeje, LED, SPI ani GPIO a do sítě posílá jen zapnuté kontroly
AnimeDB a NAS v LIVE (síť jinak jen čte jako network_monitor); se zapnutou
kontrolou záloh se v LIVE jen čte stav systemd. Scénáře s NAS používají
podvržené spojení, nikam se nepřipojují. Stavová logika (PreviewSession) nepotřebuje
tkinter ani grafický displej, takže ji testy ověřují bez X serveru; tkinter
se načte až při otevření okna.
"""

import argparse
import base64
import importlib.util
import io
import signal
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import rgb
from .alarms import AlarmEvaluator
from .config import HEALTH_TIMEOUT_DEFAULT, LIVE_TICK_SECONDS, NAS_PORT_DEFAULT, Config, ConfigError, load_config
from .core import CheckResult, Measurement, State, active_problems
from .mounts import FSTYPE, KIND, LOCAL, MODE, RO_BY, ROOT_MODE_ID
from .nas_monitor import NasMonitor
from .network_monitor import (
    ADDRESS,
    BYTES_PER_SECOND,
    INTERFACE,
    IPV4_ID,
    LINK,
    LINK_DOWN,
    LINK_ID,
    LINK_UP,
    RX_RATE_ID,
    TX_RATE_ID,
    format_rate,
)
from .oled import Mode, OledController, status_page_count
from .root_device import identify_root_device
from .run import failed_results, live_monitor
from .services import ANIMEDB_ID, BACKUP_ID, CAUSE, DOWN, HEALTH, NAS_ID, NEXUS_SERVICES_ID, complete_service_results
from .simulate import SIM_ROOT_DEVICE
from .system_monitor import CELSIUS, CPU_TEMP_ID, CPU_USAGE_ID, DISK_ROOT_ID, MEMORY_ID, PERCENT

# Zrychlení simulovaného času. LIVE běží vždy 1×, protože měří skutečný čas.
SPEEDS = (1, 5, 10)
# Celočíselné zvětšení, aby každý pixel OLED zůstal ostrým čtvercem.
SCALES = (4, 6)
# Obnova okna. Stránce kolotoče by stačilo 0,1 s, ale fáze blikání CRITICAL
# trvá jen 0,25 s, takže po 0,1 s by svit kolísal o celou obnovu.
TICK_MS = 50

LIVE = "live"

# Zhasnutá LED je na obrazovce tmavá s obrysem, aby bylo vidět, kde je.
LED_OFF_FILL = "#141414"
LED_OUTLINE = "#505050"
LED_COUNT = 4


# --- Fiktivní scénáře -------------------------------------------------------
# Každý scénář dostane funkci sim(id, stav, popis, hodnota=None, stale_after=None,
# details=None) a vrátí seznam výsledků. Běžná položka je čerstvé měření
# v okamžiku obnovy (obnovuje se po intervalu kolektoru jako skutečná data).
# `stale_after=N` znamená: změřeno jen při spuštění scénáře a dál nepřichází,
# po N s tedy vyprší. Hodnoty mají číselnou hodnotu a údaje details jako
# skutečné kolektory: displej (oled_text) ukazuje je, ne český popis.

# Fiktivní rozhraní a adresa z dokumentačního rozsahu 192.0.2.0/24 (RFC 5737),
# aby se simulace nikdy nepletla se skutečnou sítí.
SIM_INTERFACE = "eth0"
SIM_ADDRESS = "192.0.2.10"
SIM_NAS_ADDRESS = "192.0.2.20"  # fiktivní NAS ze stejného rozsahu; nikdy se na ni nepřipojuje
# Fiktivní nepodporované (vrstvené) zařízení ROOT: řádek ROOT DEV ukáže UNKN,
# stav, STATUS ani LED se tím nemění (je to jen údaj pro displej).
SIM_UNKNOWN_DEVICE = (State.UNKNOWN, "vrstvené nebo virtuální blokové zařízení", {CAUSE: "stacked"})


def pct(value):
    return Measurement(value, PERCENT)


def deg(value):
    return Measurement(value, CELSIUS)


def rate(bytes_per_second):
    return Measurement(bytes_per_second, BYTES_PER_SECOND)


def network(sim, rx, tx, link=LINK_UP, address=SIM_ADDRESS, stale_after=None):
    """Čtyři fiktivní výsledky stránky NETWORK; address=None = rozhraní bez IPv4."""
    on_interface = {INTERFACE: SIM_INTERFACE}
    with_address = {ADDRESS: address} if address else {}
    return [
        sim(LINK_ID, State.INFO, f"Síť {SIM_INTERFACE} {link}", details={**on_interface, LINK: link}),
        sim(RX_RATE_ID, State.INFO, f"Síť RX {format_rate(rx)}", rate(rx), stale_after, details=on_interface),
        sim(TX_RATE_ID, State.INFO, f"Síť TX {format_rate(tx)}", rate(tx), stale_after, details=on_interface),
        sim(IPV4_ID, State.INFO, f"IPv4 {address or 'bez adresy'}", stale_after=stale_after,
            details={**on_interface, **with_address}),
    ]


def root_mode(sim, read_only=False):
    """Fiktivní režim / se stejnými details jako kolektor (M5-S2): rw INFO, doložené ro ERROR."""
    if read_only:
        return sim(ROOT_MODE_ID, State.ERROR, "Režim / jen pro čtení (ext4, lokální; ro v superbloku)",
                   details={MODE: "ro", RO_BY: "superblock", FSTYPE: "ext4", KIND: LOCAL})
    return sim(ROOT_MODE_ID, State.INFO, "Režim / rw (ext4, lokální)",
               details={MODE: "rw", FSTYPE: "ext4", KIND: LOCAL})


def system_and_services(sim):
    # Režim / na konci, aby indexy systémových položek a služeb zůstaly stejné.
    return [
        sim(CPU_USAGE_ID, State.INFO, "CPU 12 %", pct(12)),
        sim(CPU_TEMP_ID, State.INFO, "Teplota CPU 46 °C", deg(46)),
        sim(MEMORY_ID, State.INFO, "RAM 31 % z 7,9 GiB", pct(31)),
        sim(DISK_ROOT_ID, State.INFO, "Disk / 42 % z 117 GiB", pct(42)),
        sim(NEXUS_SERVICES_ID, State.OK, "Služby Nexusu běží"),
        sim(NAS_ID, State.OK, "NAS dostupný"),
        sim(BACKUP_ID, State.OK, "Záloha v pořádku"),
        sim(ANIMEDB_ID, State.NA, "AnimeDB není nasazena"),
        root_mode(sim),
    ]


def scenario_ok(sim):
    # Síť na konci, aby indexy systémových položek a služeb zůstaly stejné.
    return system_and_services(sim) + network(sim, rx=1_234_000, tx=85_300)


def scenario_warning(sim):
    results = scenario_ok(sim)
    results[1] = sim(CPU_TEMP_ID, State.WARNING, "Teplota CPU 74 °C", deg(74))
    return results


def scenario_error(sim):
    results = scenario_ok(sim)
    results[1] = sim(CPU_TEMP_ID, State.ERROR, "Teplota CPU 86 °C", deg(86))
    results[5] = sim(NAS_ID, State.CRITICAL, "NAS nedostupný")
    return results


def scenario_unknown(sim):
    return [
        sim(CPU_USAGE_ID, State.UNKNOWN, "CPU: neplatná data"),
        sim(CPU_TEMP_ID, State.INFO, "Teplota CPU 47 °C", deg(47)),
        # RAM chybí úplně: stránka SYSTEM ukáže "RAM UNKN", do STATUS se nepočítá.
        sim(DISK_ROOT_ID, State.INFO, "Disk / 42 % z 117 GiB", pct(42), stale_after=20),
        sim(NEXUS_SERVICES_ID, State.OK, "Služby Nexusu běží"),
        sim(NAS_ID, State.UNKNOWN, "NAS: neodpovídá"),
        sim(BACKUP_ID, State.OK, "Záloha v pořádku", stale_after=40),
        sim(ANIMEDB_ID, State.NA, "AnimeDB není nasazena"),
    ]


def scenario_many(sim):
    # Šest problémů = dvě stránky STATUS (4 řádky na stránku).
    return [
        sim(CPU_USAGE_ID, State.WARNING, "CPU 97 %", pct(97)),
        sim(CPU_TEMP_ID, State.ERROR, "Teplota CPU 88 °C", deg(88)),
        sim(MEMORY_ID, State.INFO, "RAM 61 % z 7,9 GiB", pct(61)),
        sim(DISK_ROOT_ID, State.WARNING, "Disk / 91 % z 117 GiB", pct(91)),
        sim(NEXUS_SERVICES_ID, State.UNKNOWN, "NEXUS: bez odpovědi"),
        sim(NAS_ID, State.CRITICAL, "NAS nedostupný"),
        sim(BACKUP_ID, State.ERROR, "Záloha starší 3 dny"),
        sim(ANIMEDB_ID, State.NA, "AnimeDB není nasazena"),
        root_mode(sim),
    ] + network(sim, rx=4_820_000, tx=310_000)


def scenario_unconnected(sim):
    # Fiktivní systém a síť, služby bez výsledku: doplnění je ukáže jako dnes
    # ve skutečnosti s výchozí konfigurací (NEXUS UNKNOWN; AnimeDB, NAS a BACKUP
    # se sledováním vypnutým N/A).
    return system_and_services(sim)[:4] + [root_mode(sim)] + network(sim, rx=1_234_000, tx=85_300)


# Síťové scénáře: systém a služby jako ve scénáři 1, mění se jen NETWORK.
def scenario_net_down(sim):
    # Linka DOWN: platné měření (INFO), adresa žádná, čítače stojí.
    return system_and_services(sim) + network(sim, rx=0, tx=0, link=LINK_DOWN, address=None)


def scenario_net_no_ipv4(sim):
    # Linka UP, ale IPv4 chybí (např. nefunkční DHCP): NONE, ne UNKN.
    return system_and_services(sim) + network(sim, rx=412, tx=0, address=None)


def scenario_net_low(sim):
    return system_and_services(sim) + network(sim, rx=312, tx=96)


def scenario_net_high(sim):
    return system_and_services(sim) + network(sim, rx=112_400_000, tx=48_700_000)


def scenario_net_invalid(sim):
    # RX neplatné (reset čítače, UNKNOWN), TX a IPv4 změřené jen při spuštění
    # scénáře: po 20 s vyprší na UNKN (ve STATUS STALE). Linka platí dál.
    on_interface = {INTERFACE: SIM_INTERFACE}
    fresh = network(sim, rx=0, tx=85_300, stale_after=20)
    return system_and_services(sim) + [
        fresh[0],
        sim(RX_RATE_ID, State.UNKNOWN, "Síť RX: reset čítače", details=on_interface),
        fresh[2],
        fresh[3],
    ]


# Scénáře pro RGB: systém, síť a služby jako ve scénáři 1, závady jen tam,
# kde je uvádí název. Počet bliknutí určuje rgb.category_of(), ne scénář.
def scenario_rgb_nas_warning(sim):
    results = scenario_ok(sim)
    # service.nas sleduje dostupnost, proto i varování mluví o dostupnosti.
    results[5] = sim(NAS_ID, State.WARNING, "NAS: krátkodobé výpadky dostupnosti")
    return results


def scenario_rgb_animedb_error(sim):
    # Jen v simulaci je AnimeDB nasazená a sledovaná; na Nexusu je N/A.
    results = scenario_ok(sim)
    results[7] = sim(ANIMEDB_ID, State.ERROR, "AnimeDB neodpovídá")
    return results


def scenario_rgb_backup_error(sim):
    results = scenario_ok(sim)
    results[6] = sim(BACKUP_ID, State.ERROR, "Záloha starší 3 dny")
    return results


def scenario_rgb_multi(sim):
    # Dvě ERROR a dvě WARNING bez CRITICAL: automaticky bliká jen červeně 3 a 4
    # (ERROR má přednost); po potvrzení přehraje probuzení OLED i oranžové 1 a 2.
    results = scenario_ok(sim)
    results[1] = sim(CPU_TEMP_ID, State.WARNING, "Teplota CPU 74 °C", deg(74))
    results[5] = sim(NAS_ID, State.WARNING, "NAS: krátkodobé výpadky dostupnosti")
    results[6] = sim(BACKUP_ID, State.ERROR, "Záloha starší 3 dny")
    results[7] = sim(ANIMEDB_ID, State.ERROR, "AnimeDB neodpovídá")
    return results


def scenario_rgb_critical(sim):
    # CRITICAL má přednost i před nepotvrzenou WARNING.
    results = scenario_ok(sim)
    results[1] = sim(CPU_TEMP_ID, State.WARNING, "Teplota CPU 74 °C", deg(74))
    results[5] = sim(NAS_ID, State.CRITICAL, "NAS nedostupný")
    return results


# Scénáře kontroly NAS (SERVICES-2). V průběhu výpadku dává výsledek NAS
# skutečný NasMonitor s podvrženým spojením (ScriptedConnection): jeden pokus
# na každou obnovu dat, takže potvrzení výpadku i odeznění probíhá stejně jako
# v provozu. Po posledním pokusu se opakuje poslední (NAS dál dostupný).
# Při intervalu 10 s: 0 s OK, 10 s a 20 s UNKNOWN 1/3 a 2/3 (bez blikání),
# od 30 s ERROR (NAS OFFLINE, 2 bliknutí), 90 s ERROR odeznívá 1/2 (NAS RECOVER),
# od 100 s OK.
NAS_OUTAGE = (None, ConnectionRefusedError, TimeoutError) + (ConnectionRefusedError,) * 6 + (None, None)


class ScriptedConnection:
    """Náhrada socket.create_connection pro scénáře NAS: připravený průběh, žádná síť.

    Pokus je None (spojení přijato) nebo třída výjimky, kterou by vyvolalo
    skutečné spojení; každý pokus dostane novou výjimku.
    """

    def __init__(self, attempts):
        self.attempts = attempts
        self.count = 0

    def __call__(self, address, timeout=None):
        attempt = self.attempts[min(self.count, len(self.attempts) - 1)]
        self.count += 1
        if attempt is not None:
            raise attempt("simulace")
        return self  # „navázané spojení“, které NasMonitor hned zavře

    def close(self):
        pass


def scenario_root_readonly(sim):
    # Doložené ro (M5-S2): ERROR jako každá jiná závada; STORAGE ukáže ROOT MODE RO,
    # STATUS ROOT READ-ONLY, NEXUS trojúhelník a LED červeně 1 bliknutí (systém).
    results = scenario_ok(sim)
    results[8] = root_mode(sim, read_only=True)
    return results


def scenario_nas_animedb(sim):
    # Potvrzený výpadek NAS i AnimeDB zároveň (stejné popisy a details jako
    # u kolektorů): červeně 2 a 3 bliknutí, ve STATUS ANIMEDB DOWN a NAS OFFLINE.
    refused = {HEALTH: DOWN, CAUSE: "refused"}
    results = scenario_ok(sim)
    results[5] = sim(NAS_ID, State.ERROR, "NAS: spojení odmítnuto", details=refused)
    results[7] = sim(ANIMEDB_ID, State.ERROR, "AnimeDB: spojení odmítnuto", details=refused)
    return results


# Pořadí odpovídá nabídce v okně.
SCENARIOS = {
    "ok": ("1 Vše v pořádku", scenario_ok),
    "warning": ("2 WARNING: teplota", scenario_warning),
    "error": ("3 ERROR a CRITICAL", scenario_error),
    "unknown": ("4 UNKNOWN: stará a chybějící data", scenario_unknown),
    "many": ("5 Více problémů (2 stránky STATUS)", scenario_many),
    "unconnected": ("6 Nenapojené služby (dnešní stav)", scenario_unconnected),
    "net_down": ("7 NETWORK: linka DOWN", scenario_net_down),
    "net_no_ipv4": ("8 NETWORK: bez IPv4", scenario_net_no_ipv4),
    "net_low": ("9 NETWORK: nízký provoz", scenario_net_low),
    "net_high": ("10 NETWORK: vysoký provoz", scenario_net_high),
    "net_invalid": ("11 NETWORK: neplatná a zastaralá data", scenario_net_invalid),
    "rgb_nas_warning": ("12 RGB: NAS WARNING (2 bliknutí)", scenario_rgb_nas_warning),
    "rgb_animedb_error": ("13 RGB: AnimeDB ERROR (3 bliknutí)", scenario_rgb_animedb_error),
    "rgb_backup_error": ("14 RGB: zálohy ERROR (4 bliknutí)", scenario_rgb_backup_error),
    "rgb_multi": ("15 RGB: několik závad (ERROR 3, 4; WARNING 1, 2)", scenario_rgb_multi),
    "rgb_critical": ("16 RGB: CRITICAL a WARNING", scenario_rgb_critical),
    "rgb_acknowledged": ("17 RGB: potvrzená závada (zálohy ERROR)", scenario_rgb_backup_error),
    "nas_outage": ("18 NAS: průběh výpadku a obnovení (1/3, 2/3, ERROR, odeznění)", scenario_ok),
    "nas_animedb": ("19 NAS a AnimeDB: současný výpadek (2 a 3 bliknutí)", scenario_nas_animedb),
    "root_readonly": ("20 STORAGE: ROOT jen pro čtení (ERROR, 1 bliknutí)", scenario_root_readonly),
    LIVE: ("LIVE: měření tohoto notebooku", None),
}
# Fiktivní zařízení ROOT pro řádek ROOT DEV; scénář UNKNOWN ukazuje nepodporované.
SCENARIO_DEVICES = {"unknown": SIM_UNKNOWN_DEVICE}
# Scénáře, ve kterých je závada potvrzená hned od spuštění.
PREACKNOWLEDGED = {"rgb_acknowledged"}
# Scénáře, ve kterých výsledek NAS dává NasMonitor podle připraveného průběhu pokusů.
NAS_ATTEMPTS = {"nas_outage": NAS_OUTAGE}


class SessionClock:
    """Společná časová základna simulátoru: controller, platnost výsledků i vykreslení.

    Simulovaný monotónní čas běží `speed`-krát rychleji než skutečný. Při změně
    rychlosti se základ přepočítá, aby čas neskočil ani se nevracel.
    """

    def __init__(self, real_clock=time.monotonic, speed=1):
        self.real_clock = real_clock
        self.speed = speed
        self._real_base = real_clock()
        self._base = 0.0

    def now(self):
        return self._base + (self.real_clock() - self._real_base) * self.speed

    def set_speed(self, speed):
        # bool je podtřída int a True == 1 by jinak prošlo jako rychlost 1×.
        if isinstance(speed, bool) or speed not in SPEEDS:
            raise ValueError(f"rychlost musí být jedna z {SPEEDS}, ne {speed!r}")
        self._base, self._real_base = self.now(), self.real_clock()
        self.speed = speed


class PreviewSession:
    """Stav simulátoru bez grafického okna: scénář, data, controller a čas."""

    def __init__(self, config=None, scenario="ok", clock=None, utc_clock=None, wall_clock=datetime.now,
                 monitor_factory=live_monitor, device_factory=identify_root_device):
        self.config = config or Config()
        self.clock = clock or SessionClock()
        self.utc_clock = utc_clock or (lambda: datetime.now(timezone.utc))
        self.wall_clock = wall_clock
        self.monitor_factory = monitor_factory
        self.device_factory = device_factory
        self.validity = self.config.collector.validity_seconds
        self.interval = self.config.collector.interval_seconds
        # Převod simulovaného času na UTC a místní čas pro výsledky a hodiny na displeji.
        self._time_start = self.clock.now()
        self._utc_start = self.utc_clock()
        self._wall_start = self.wall_clock()
        # Jas je místní nastavení okna: přežije změnu scénáře, nikam se nezapisuje.
        self.brightness = rgb.BRIGHTNESS_DEFAULT
        self.controller = None
        self.set_scenario(scenario)

    @property
    def is_live(self):
        return self.scenario == LIVE

    def set_scenario(self, name):
        """Přepne scénář. Data, paměť alarmů, potvrzení i okno kolotoče začnou znovu.

        Rozsvícený displej zůstane rozsvícený, ale od stránky NEXUS s novým
        oknem; zhasnutý zůstane zhasnutý. Nic se nemíchá s předchozím scénářem,
        ani potvrzení závady: patří jen ke scénáři, ve kterém vzniklo.
        """
        if name not in SCENARIOS:
            raise ValueError(f"neznámý scénář {name!r}")
        awake = self.controller is not None and self.controller.mode is not Mode.SLEEP
        self.scenario = name
        if self.is_live:
            # Skutečné měření běží ve skutečném čase.
            self.clock.set_speed(1)
            # Skutečné read-only kolektory podle konfigurace: systém, síť ([network])
            # a jen při zapnutém [services.animedb] i healthcheck AnimeDB.
            self.monitor = self.monitor_factory(self.config)
            self.evaluator = AlarmEvaluator(self.config.alarms)
            # Zařízení ROOT jen jednou při vytvoření LIVE (M5-S3), ne při obnově ani kreslení.
            self.root_device = self._identify_root_device()
        else:
            self.monitor = self.evaluator = None
            self.root_device = SCENARIO_DEVICES.get(name, SIM_ROOT_DEVICE)
        # Průběh výpadku NAS začíná se scénářem znovu, jako potvrzení a alarmy.
        attempts = NAS_ATTEMPTS.get(name)
        self.nas = None if attempts is None else NasMonitor(
            self.validity, SIM_NAS_ADDRESS, NAS_PORT_DEFAULT, HEALTH_TIMEOUT_DEFAULT,
            connect=ScriptedConnection(attempts))
        now = self.clock.now()
        self.started_at = now
        self.controller = OledController(self.config.oled)
        self.leds = rgb.RgbLeds(self.config.alarms, self.brightness)
        if awake:
            self.controller.press(now, 0)
        self._refresh(now)
        if name in PREACKNOWLEDGED:
            self.leds.acks.acknowledge(self.monitored, now)

    def _identify_root_device(self):
        # Doplňující údaj pro displej: chyba identifikace nesmí zastavit náhled (ROOT DEV UNKN).
        try:
            return self.device_factory()
        except Exception:
            return None

    def set_speed(self, speed):
        if self.is_live:
            raise ValueError("LIVE režim běží jen ve skutečném čase (1×)")
        self.clock.set_speed(speed)

    def utc_at(self, now):
        return self._utc_start + timedelta(seconds=now - self._time_start)

    def wall_time(self, now):
        if self.is_live:
            return self.wall_clock()
        return self._wall_start + timedelta(seconds=now - self._time_start)

    def _scenario_results(self, now):
        def sim(check_id, state, summary, value=None, stale_after=None, details=None):
            at, valid = (self.started_at, stale_after) if stale_after else (now, self.validity)
            return CheckResult(check_id, state, summary, self.utc_at(at), at + valid, simulated=True,
                               measurement=value, details=details or {})

        return SCENARIOS[self.scenario][1](sim)

    def _refresh(self, now):
        """Nová data jako po jednom měření kolektoru; další obnova za interval kolektoru."""
        if self.is_live:
            observed_utc = self.utc_clock()
            try:
                measured = self.monitor.collect(now, observed_utc)
            except Exception:
                # Stejně jako run.py: celé selhání = UNKNOWN, ne staré hodnoty.
                measured = failed_results(self.monitor, now, observed_utc, self.validity)
            measured = self.evaluator.evaluate(measured, now)
        else:
            observed_utc = self.utc_at(now)
            measured = self._scenario_results(now)
            if self.nas is not None:
                # Místo pevného výsledku NAS další pokus z připraveného průběhu.
                measured = [result for result in measured if result.id != NAS_ID]
                measured += self.nas.collect(now, observed_utc)
        # Skutečně monitorované kontroly pro LED: služby, které doplnění níže
        # přidá jen kvůli chybějícímu kolektoru, bílou blokovat nemají.
        self.monitored = measured
        # Služba se zapnutým kolektorem (LIVE, např. AnimeDB) bez výsledku je UNKNOWN, ne N/A.
        enabled = self.monitor.labels if self.monitor is not None else ()
        self.results = complete_service_results(measured, observed_utc, now + self.validity, monitored=enabled)
        # LIVE obnovuje v taktu živého měření (SCHED-2); kolektory samy měří jen ve
        # svých termínech. Fiktivní scénáře dál po intervalu kolektoru.
        self.next_refresh = now + (LIVE_TICK_SECONDS if self.is_live else self.interval)

    def _refresh_if_due(self, now):
        if now >= self.next_refresh:
            self._refresh(now)

    def press(self):
        """Krátký stisk tlačítka: skutečná logika OledController."""
        now = self.clock.now()
        self._refresh_if_due(now)
        # Probouzecí stisk se pozná až po uplatnění časových limitů: stisk po
        # uplynutí okna kolotoče také probouzí (press() je uplatní stejně).
        self.controller.update(now)
        asleep = self.controller.mode is Mode.SLEEP
        self.controller.press(now, len(active_problems(self.results, now)))
        if asleep and self.controller.mode is not Mode.SLEEP:
            self.leds.wake(self.monitored, now)

    def led_frame(self):
        """Barva čtyř LED v aktuálním simulovaném čase; obraz OLED se tím nemění."""
        now = self.clock.now()
        self._refresh_if_due(now)
        self.controller.update(now)
        # Při zrychlení by blikání bylo rychlejší, než okno vykreslí (a rychlejší
        # než 3 Hz): barva vzoru se ukáže trvale a popis říká, co by blikalo.
        steady = self.clock.speed > 1
        return self.leds.frame(self.monitored, self.controller.mode is not Mode.SLEEP, now, steady)

    def acknowledge(self):
        """„Beru na vědomí“: potvrdí aktivní WARNING a ERROR. CheckResult se nemění."""
        now = self.clock.now()
        self._refresh_if_due(now)
        return self.leds.acks.acknowledge(self.monitored, now)

    def clear_acknowledgements(self):
        """Zruší potvrzení (jen pro zkoušení v simulátoru)."""
        self.leds.acks.clear()

    def set_brightness(self, percent):
        self.leds.set_brightness(percent)
        self.brightness = percent

    def tick(self):
        """Volá se pravidelně z okna: obnoví data, uplatní časové limity a vrátí obraz 128 × 64."""
        now = self.clock.now()
        self._refresh_if_due(now)
        self.controller.update(now)
        # Pillow až tady: stavová logika a její testy bez něj fungují.
        from .render import render_screen
        return render_screen(self.controller, now, self.results, self.wall_time(now), self.root_device)

    def describe(self):
        """Režim displeje pro popisek mimo plochu OLED."""
        now = self.clock.now()
        mode = self.controller.mode
        elapsed = f"{now - self.started_at:.0f} s od začátku scénáře"
        if mode is Mode.CAROUSEL:
            return f"CAROUSEL – {self.controller.current_page(now)} ({elapsed})"
        if mode is Mode.STATUS:
            total = status_page_count(len(active_problems(self.results, now)))
            return f"STATUS – stránka {min(self.controller.status_page + 1, total)}/{total} ({elapsed})"
        return f"SLEEP – displej zhasnutý ({elapsed})"

    def source_text(self):
        if self.is_live:
            animedb = self.config.services.animedb
            source = ("AnimeDB z HTTP healthchecku /health" if animedb.enabled
                      else "AnimeDB N/A (sledování vypnuto)")
            nas = ("NAS ze spojení TCP na port SMB" if self.config.services.nas.enabled
                   else "NAS N/A (sledování vypnuto)")
            backup = ("BACKUP z výsledku úlohy v systemd" if self.config.services.backup.enabled
                      else "BACKUP N/A (sledování vypnuto)")
            return ("LIVE: skutečná read-only měření (systém a síť) tohoto NOTEBOOKU, ne Nexusu. "
                    f"Služby bez kolektoru UNKNOWN, {source}, {nas}, {backup}. "
                    "Zařízení ROOT zjištěno jednou při spuštění LIVE. Čas 1×.")
        text = f"SIMULACE: fiktivní data (značka SIM), čas {self.clock.speed}×."
        if self.nas is not None:
            # OLED ukazuje u 1. i 2. neúspěšného pokusu stejné NAS FAILING;
            # pořadí pokusu a důvod jsou vidět jen v českém popisu výsledku.
            [nas] = [result for result in self.monitored if result.id == NAS_ID]
            text += f"\nPoslední kontrola NAS ({self.nas.connect.count}.): {nas.summary}"
        return text

    def rgb_text(self):
        """Popis pod LED: co právě ukazují, jaký je jas a co je potvrzené."""
        signal = self.leds.signal
        text = f"RGB, jas {self.brightness} %: {rgb.describe(signal) if signal else 'zhasnuto'}"
        if self.clock.speed > 1 and signal is not None and signal.kind in rgb.BLINKING:
            text += " (při zrychlení barva trvale, bez blikání)"
        acknowledged = ", ".join(f"{check_id} {record.state.value}"
                                 for check_id, record in sorted(self.leds.acks.records.items()))
        return f"{text}\nPotvrzeno: {acknowledged or 'nic'}"


def led_fill(frame):
    """Barva LED na obrazovce jako #rrggbb.

    Tlumený jas je s gama korekcí (2,2): lineárních 20 % by na monitoru vypadalo
    skoro černě, kdežto skutečnou LED oko vnímá jasněji. Skutečný vzhled LED
    se ověří až na hardwaru.
    """
    if not frame.lit:
        return LED_OFF_FILL
    gain = frame.level ** (1 / 2.2)
    red, green, blue = (round(channel * gain) for channel in frame.color)
    return f"#{red:02x}{green:02x}{blue:02x}"


def scale_frame(image, scale):
    """Celočíselné zvětšení metodou NEAREST: pixely zůstanou ostré, bez vyhlazení."""
    if isinstance(scale, bool) or not isinstance(scale, int) or scale < 1:
        raise ValueError(f"zvětšení musí být celé kladné číslo, ne {scale!r}")
    from PIL import Image
    return image.resize((image.width * scale, image.height * scale), resample=Image.NEAREST)


def png_data(image):
    """Obraz jako base64 PNG pro tkinter.PhotoImage (nepotřebuje PIL.ImageTk)."""
    buffer = io.BytesIO()
    image.convert("L").save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue())


def run_window(session, scale):
    """Otevře okno a běží do jeho zavření. Žádná vlákna ani procesy na pozadí."""
    import tkinter as tk  # jen pro okno; stavová logika ho nepotřebuje

    root = tk.Tk()
    root.title("NexusSTAT – simulátor OLED 128 × 64 a RGB LED (notebook)")
    root.configure(padx=10, pady=10)

    # Plocha OLED: jen obraz z render_screen(), nic dalšího se do ní nekreslí.
    display = tk.Label(root, bg="black", borderwidth=4, relief="sunken")
    display.pack()

    mode_text = tk.StringVar()
    source_text = tk.StringVar()
    tk.Label(root, textvariable=mode_text, font=("TkDefaultFont", 11, "bold")).pack(anchor="w", pady=(8, 0))
    tk.Label(root, textvariable=source_text, wraplength=128 * scale, justify="left").pack(anchor="w")

    # Čtyři LED pod displejem jako samostatná plocha: do bitmapy OLED se nekreslí
    # a svítí vždy všechny stejně (počet bliknutí je kód kategorie, ne číslo LED).
    led_size = led_gap = 6 * scale
    margin = 4
    leds = tk.Canvas(root, width=LED_COUNT * led_size + (LED_COUNT - 1) * led_gap + 2 * margin,
                     height=led_size + 2 * margin, highlightthickness=0)
    leds.pack(after=display, pady=(8, 0))
    led_items = []
    for index in range(LED_COUNT):
        left = margin + index * (led_size + led_gap)
        led_items.append(leds.create_oval(left, margin, left + led_size, margin + led_size,
                                          fill=LED_OFF_FILL, outline=LED_OUTLINE, width=2))
    rgb_text = tk.StringVar()
    tk.Label(root, textvariable=rgb_text, wraplength=128 * scale, justify="left").pack(after=leds, anchor="w")

    controls = tk.Frame(root)
    controls.pack(fill="x", pady=(8, 0))
    # takefocus=0: klávesy jdou oknu, ne ovládacímu prvku, takže mezerník
    # nestiskne zároveň tlačítko s fokusem a OLED tlačítko dvakrát.
    tk.Button(controls, text="Stisk (mezerník / Enter)", takefocus=0, command=lambda: press()).pack(side="left")

    scenario_names = {label: key for key, (label, _) in SCENARIOS.items()}
    scenario_var = tk.StringVar(value=SCENARIOS[session.scenario][0])
    menu = tk.OptionMenu(controls, scenario_var, *scenario_names, command=lambda label: change_scenario(label))
    menu.configure(takefocus=0)
    menu.pack(side="left", padx=8)

    speed_var = tk.IntVar(value=session.clock.speed)
    speed_buttons = []
    for speed in SPEEDS:
        button = tk.Radiobutton(controls, text=f"{speed}×", value=speed, variable=speed_var, takefocus=0,
                                command=lambda: session.set_speed(speed_var.get()))
        button.pack(side="left")
        speed_buttons.append(button)

    # Potvrzení a jas jen v paměti okna: nic se neukládá ani nezapisuje do konfigurace.
    rgb_controls = tk.Frame(root)
    rgb_controls.pack(fill="x", pady=(4, 0))
    tk.Button(rgb_controls, text="Potvrdit WARNING/ERROR", takefocus=0,
              command=lambda: acknowledge()).pack(side="left")
    tk.Button(rgb_controls, text="Zrušit potvrzení", takefocus=0,
              command=lambda: clear_acknowledgements()).pack(side="left", padx=8)
    tk.Label(rgb_controls, text="Jas LED %").pack(side="left")
    brightness_var = tk.IntVar(value=session.brightness)
    tk.Scale(rgb_controls, from_=rgb.BRIGHTNESS_MIN, to=rgb.BRIGHTNESS_MAX, orient="horizontal",
             variable=brightness_var, takefocus=0, command=lambda value: set_brightness(value)).pack(side="left")

    state = {"after_id": None, "frame": None, "photo": None, "led": None}

    def update_controls():
        for button in speed_buttons:
            button.configure(state="disabled" if session.is_live else "normal")
        speed_var.set(session.clock.speed)

    def press():
        session.press()
        refresh(schedule=False)

    def change_scenario(label):
        session.set_scenario(scenario_names[label])
        update_controls()
        refresh(schedule=False)

    def acknowledge():
        session.acknowledge()
        refresh(schedule=False)

    def clear_acknowledgements():
        session.clear_acknowledgements()
        refresh(schedule=False)

    def set_brightness(value):
        # Scale předává text, např. "37".
        session.set_brightness(int(float(value)))
        refresh(schedule=False)

    def refresh(schedule=True):
        try:
            frame = session.tick()
            # Nový obrázek jen při změně, aby okno zbytečně nevytěžovalo procesor.
            if frame.tobytes() != state["frame"]:
                state["frame"] = frame.tobytes()
                state["photo"] = tk.PhotoImage(data=png_data(scale_frame(frame, scale)))
                display.configure(image=state["photo"])
            fill = led_fill(session.led_frame())
            if fill != state["led"]:
                state["led"] = fill
                for item in led_items:
                    leds.itemconfigure(item, fill=fill)
            mode_text.set(session.describe())
            source_text.set(session.source_text())
            rgb_text.set(session.rgb_text())
        except Exception as error:  # chyba se ukáže v okně, simulátor běží dál
            mode_text.set(f"Chyba: {type(error).__name__}: {error}")
        if schedule:
            state["after_id"] = root.after(TICK_MS, refresh)

    def close(*_):
        # Zrušit naplánovanou obnovu, jinak by po zavření okna volala zničené prvky.
        if state["after_id"] is not None:
            root.after_cancel(state["after_id"])
            state["after_id"] = None
        root.destroy()

    root.bind("<space>", lambda event: press())
    root.bind("<Return>", lambda event: press())
    root.bind("<Escape>", close)
    root.protocol("WM_DELETE_WINDOW", close)
    # Ctrl+C v terminálu zavře okno stejně čistě jako křížek.
    previous_handler = signal.signal(signal.SIGINT, lambda signum, frame: close())
    try:
        update_controls()
        root.focus_set()
        refresh()
        root.mainloop()
    finally:
        signal.signal(signal.SIGINT, previous_handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Živý simulátor OLED NexusSTAT (tkinter, jen notebook)")
    parser.add_argument("--scenario", choices=list(SCENARIOS), default="ok", help="výchozí scénář")
    parser.add_argument("--scale", type=int, choices=SCALES, default=4, help="zvětšení pixelů OLED")
    parser.add_argument("--config", type=Path,
                        help="TOML se sekcemi [oled], [collector], [alarms], [network] a [services]")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config) if args.config else Config()
    except (ConfigError, OSError) as error:
        parser.error(str(error))
    missing = [name for name in ("tkinter", "PIL") if importlib.util.find_spec(name) is None]
    if missing:
        parser.exit(2, f"Simulátor potřebuje tkinter a Pillow, chybí: {', '.join(missing)}. "
                       "Na Linux Mint: sudo apt install python3-tk (instalace jen po schválení).\n")
    session = PreviewSession(config, scenario=args.scenario)
    try:
        run_window(session, args.scale)
    except Exception as error:
        # Typicky tkinter.TclError bez grafického displeje (prázdné DISPLAY).
        parser.exit(1, f"Okno nelze otevřít: {type(error).__name__}: {error}\n")


if __name__ == "__main__":
    main()
