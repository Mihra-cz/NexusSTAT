"""Testy šetrného plánování kontrol služeb (SCHED-1) a živého měření (SCHED-2) se simulovaným časem.

Žádné skutečné čekání, síť ani systemd: spojení AnimeDB a NAS i výstup
systemctl jsou podvržené a počítají skutečná volání, čas je číslo. Kolektory
služeb se berou přímo z run.live_monitor (stejné intervaly, platnosti
a obal Scheduled jako v provozu); vymění se jen spojení, systemctl a hodiny.
"""

import errno
import math
import subprocess
import tempfile
import unittest
from collections import namedtuple
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from nexusstat import history, rgb, snapshot_plan
from nexusstat.alarms import AlarmEvaluator
from nexusstat.animedb_monitor import AnimeDbMonitor
from nexusstat.backup_monitor import (FALLBACK_SECONDS, PENDING_SECONDS, RECHECK_SECONDS, SETTLE_SECONDS,
                                      BackupMonitor, next_elapse)
from nexusstat.config import (ANIMEDB_INTERVAL_DEFAULT, NAS_INTERVAL_DEFAULT, AlarmRule, AlarmsConfig,
                              AnimeDbConfig, BackupConfig, CollectorConfig, Config, ConfigError, NasConfig,
                              ServicesConfig, load_config)
from nexusstat.core import CheckResult, Measurement, State, effective
from nexusstat.fan_monitor import CPU_FAN_ID, RPM
from nexusstat.network_monitor import (BYTES_PER_SECOND, INTERFACE, IPV4_ID, LINK_ID, RX_RATE_ID, TX_RATE_ID,
                                       NetworkMonitor)
from nexusstat.nas_monitor import NasMonitor
from nexusstat.run import COLLECTOR_FAILURE, Collectors, Scheduled, live_monitor, result_text, run_loop
from nexusstat.services import ANIMEDB_ID, BACKUP_ID, CONFIRM_RETRY_SECONDS, NAS_ID, OutageConfirmation
from nexusstat.system_monitor import (CELSIUS, CPU_TEMP_ID, CPU_USAGE_ID, DISK_ROOT_ID, LABELS as SYSTEM_LABELS,
                                      MEMORY_ID, PERCENT, SystemMonitor)

URL = "http://192.0.2.10:8080/health"  # dokumentační rozsah RFC 5737
HOST = "192.0.2.20"
UNIT = "example-backup.service"
TIMER = "example-backup.timer"
EXAMPLE_CONFIG = Path(__file__).resolve().parent.parent / "config" / "config.example.toml"

START = 500_000.0  # monotónní s při spuštění monitoringu (systém běží asi 5,8 dne)
START_UTC = datetime(2026, 10, 29, 10, 0, tzinfo=timezone.utc)
HOUR = 3600
DAY = 24 * HOUR
TICK = 10  # výchozí [collector] interval_seconds
GRACE = 3 * TICK  # obvyklá platnost výsledků (3 takty)

HEALTHY = None  # výsledek pokusu: spojení navázáno (AnimeDB odpoví HTTP 200 a status ok)
REFUSED = ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused")
NO_DESCRIPTORS = OSError(errno.EMFILE, "Too many open files")  # chyba na této straně, ne výpadek
HTTP_OK = b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 15\r\n\r\n{"status":"ok"}'
HTTP_404 = b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n"
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def utc_at(monotonic):
    return START_UTC + timedelta(seconds=monotonic - START)


def mono_at(moment):
    return START + (moment - START_UTC).total_seconds()


class FakeClock:
    def __init__(self, now=START):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeSystem:
    """Systémová měření bez /proc: každý takt nový výsledek se stálou platností."""

    simulated = False
    labels = SYSTEM_LABELS

    def __init__(self, validity_seconds=GRACE):
        self.validity_seconds = validity_seconds

    def collect(self, now, observed_utc):
        return [CheckResult(check_id, State.INFO, label, observed_utc, now + self.validity_seconds, simulated=False)
                for check_id, label in SYSTEM_LABELS.items()]


class FakeSocket:
    def __init__(self, data=b""):
        self.data = data

    def settimeout(self, value):
        pass

    def sendall(self, data):
        pass

    def recv_into(self, buffer):
        size = min(len(buffer), len(self.data))
        buffer[:size] = self.data[:size]
        self.data = self.data[size:]
        return size

    def close(self):
        pass


class Endpoint:
    """Podvržené socket.create_connection pro AnimeDB i NAS; nic se nepřipojuje.

    `outcome` je výsledek každého pokusu (HEALTHY nebo výjimka), funkce
    monotónního času, která ho vrátí, nebo seznam opakovaný podle pořadí
    pokusu. `calls` jsou časy skutečných pokusů.
    """

    def __init__(self, clock, outcome=HEALTHY, response=HTTP_OK):
        self.clock = clock
        self.outcome = outcome
        self.response = response
        self.calls = []

    def __call__(self, address, timeout=None):
        self.calls.append(self.clock.now)
        if isinstance(self.outcome, list):
            outcome = self.outcome[(len(self.calls) - 1) % len(self.outcome)]
        else:
            outcome = self.outcome(self.clock.now) if callable(self.outcome) else self.outcome
        if outcome is not None:
            raise outcome
        return FakeSocket(self.response)


def us(seconds):
    return round(seconds * 1_000_000)


def systemd_time(moment):
    """Plán timeru ve tvaru systemd 255 s TZ=UTC, např. "Fri 2026-10-30 02:00:00 UTC"."""
    return f"{DAYS[moment.weekday()]} {moment:%Y-%m-%d %H:%M:%S} UTC"


def service_block(start, end, active="inactive", sub="dead", result="success", code=1, status=0):
    return (f"Result={result}\nExecMainStartTimestampMonotonic={us(start)}\n"
            f"ExecMainExitTimestampMonotonic={us(end)}\nExecMainCode={code}\nExecMainStatus={status}\n"
            f"Id={UNIT}\nNames={UNIT}\nLoadState=loaded\nActiveState={active}\nSubState={sub}")


def timer_block(elapse=None, active="active", raw=None):
    plan = raw if raw is not None else ("" if elapse is None else systemd_time(elapse))
    return (f"NextElapseUSecRealtime={plan}\nResult=success\nId={TIMER}\nNames={TIMER}\nLoadState=loaded\n"
            f"ActiveState={active}\nSubState=waiting")


def completed(args, *blocks):
    return subprocess.CompletedProcess(args, 0, ("\n\n".join(blocks) + "\n").encode("ascii"), b"")


class DailyBackup:
    """Podvržený systemctl: denní záloha v `hour`:00 UTC trvající `duration` s; počítá dotazy.

    Odpovídá podle simulovaného času jako systemd 255: během běhu `activating`
    s novým začátkem a bez konce, po něm doložený úspěch; timer ukazuje
    příští plánovaný start (po spuštění úlohy už ten zítřejší). `duration`
    může být i funkce data běhu (různě dlouhé běhy).
    """

    def __init__(self, clock, hour=2, duration=600, timer=True):
        self.clock = clock
        self.hour = hour
        self.duration = duration
        self.timer = timer
        self.calls = []

    def __call__(self, args, **kwargs):
        now = self.clock.now
        self.calls.append(now)
        last = utc_at(now).replace(hour=self.hour, minute=0, second=0, microsecond=0)
        if mono_at(last) > now:
            last -= timedelta(days=1)
        start = mono_at(last)
        duration = self.duration(last.date()) if callable(self.duration) else self.duration
        if now < start + duration:
            service = service_block(start, 0, active="activating", sub="start", code=0)
        else:
            service = service_block(start, start + duration)
        if not self.timer:
            return completed(args, service)
        return completed(args, service, timer_block(last + timedelta(days=1)))


class StaticSystemctl:
    """Podvržený systemctl s pevným výstupem (záloha, která už neběží); počítá dotazy."""

    def __init__(self, clock, *blocks):
        self.clock = clock
        self.blocks = blocks
        self.calls = []

    def __call__(self, args, **kwargs):
        self.calls.append(self.clock.now)
        return completed(args, *self.blocks)


class Flaky:
    """Obal podvrženého systemctl: kdy `failing(now)`, dotaz selže výjimkou nebo nenulovým kódem."""

    def __init__(self, inner, failing, error):
        self.inner, self.failing, self.error = inner, failing, error
        self.calls = []

    def __call__(self, args, **kwargs):
        now = self.inner.clock.now
        self.calls.append(now)
        if not self.failing(now):
            return self.inner(args, **kwargs)
        if isinstance(self.error, int):
            return subprocess.CompletedProcess(args, self.error, b"", b"Failed to connect to bus")
        raise self.error


def timeout_error():
    return subprocess.TimeoutExpired(["systemctl"], 2)


def services_config(animedb=True, nas=True, backup=True, timer=TIMER, collector=None,
                    animedb_interval=ANIMEDB_INTERVAL_DEFAULT, nas_interval=NAS_INTERVAL_DEFAULT):
    return Config(collector=collector or CollectorConfig(), services=ServicesConfig(
        AnimeDbConfig(animedb, URL, interval_seconds=animedb_interval),
        NasConfig(nas, HOST, interval_seconds=nas_interval),
        BackupConfig(backup, UNIT, timer)))


def wired(config, clock, animedb=None, nas=None, systemctl=None):
    """Kolektory přesně z run.live_monitor, jen s podvrženým spojením, systemctl a hodinami.

    Systém a síť nahradí FakeSystem, aby test nečetl /proc tohoto počítače.
    Vrací (Collectors, {id služby: Scheduled}).
    """
    live = live_monitor(config)  # jen vytvoření: nic se nepřipojuje ani nespouští
    services = {}
    for scheduled in live.monitors:
        inner = scheduled.monitor
        if not isinstance(inner, (AnimeDbMonitor, NasMonitor, BackupMonitor)):
            continue  # systém, ventilátor a síť (SCHED-2) nahradí FakeSystem
        if isinstance(inner, AnimeDbMonitor):
            inner.connect, inner.clock = animedb, clock
        elif isinstance(inner, NasMonitor):
            inner.connect = nas
        else:
            inner.run, inner.clock = systemctl, clock
        services[next(iter(inner.labels))] = scheduled
    return Collectors(FakeSystem(config.collector.validity_seconds), *services.values()), services


class FakeLink:
    """Napojení historie bez SQLite: zaznamená předané výsledky a plánuje snímky skutečným SnapshotPlanner."""

    def __init__(self, daily_time=(3, 0)):
        self.recorded = []
        self.planner = snapshot_plan.SnapshotPlanner(snapshot_plan.DailyPlan(*daily_time, ZoneInfo("UTC")))
        # Dnešní snímek už existuje, jinak by se zmeškaný dnešní termín hned dohnal.
        self.snapshot = SimpleNamespace(requested=False, running=False, scanned=True,
                                        utc=int(START_UTC.timestamp()) - HOUR)
        self.requested = []

    def record(self, results, measured_monotonic):
        self.recorded.append((list(results), measured_monotonic))
        return True

    def plan_snapshot(self, now, observed_utc):
        if not self.planner.due(observed_utc, self.snapshot):
            return False
        self.planner.note_requested(observed_utc)
        self.requested.append(observed_utc)
        self.snapshot.utc = int(observed_utc.timestamp())  # snímek hned hotový
        return True

    def check(self, now, observed_utc):
        return CheckResult("history.storage", State.INFO, "HISTORY: test", observed_utc, now + GRACE, simulated=False)

    def snapshot_check(self, now, observed_utc):
        return CheckResult("history.snapshot", State.INFO, "HIST SNAPSHOT: test", observed_utc, now + GRACE,
                           simulated=False)


def run_for(monitor, clock, seconds, history_link=None, pause=None, tick_seconds=None, alarms=None,
            config=None):
    """Smyčka run_loop po simulovanou dobu; vrací (poslední výsledky, řádky, výsledky po taktech).

    `pause` = (od, délka): proces je jednou pozastavený (hodiny poskočí).
    """
    end = clock.now + seconds
    lines, ticks, paused = [], [], []

    def sleep(delay):
        clock.advance(delay)
        if pause is not None and clock.now >= pause[0] and not paused:
            paused.append(True)
            clock.advance(pause[1])
        if clock.now >= end:
            raise KeyboardInterrupt

    class Recording:
        """Obal kolektoru: výsledky každého taktu pro kontrolu mezi termíny."""

        labels, simulated, validity_seconds = monitor.labels, monitor.simulated, monitor.validity_seconds

        def collect(self, now, observed_utc):
            results = monitor.collect(now, observed_utc)
            ticks.append((now, {result.id: result for result in results}))
            return results

        def window_results(self, now, observed_utc, validity_seconds):
            return monitor.window_results(now, observed_utc, validity_seconds)

    results = run_loop(Recording(), config or CollectorConfig(), AlarmEvaluator(alarms or AlarmsConfig()), clock=clock,
                       utc_clock=lambda: utc_at(clock.now), sleep=sleep, output=lines.append,
                       history_link=history_link, tick_seconds=tick_seconds)
    return results, lines, ticks


def by_id(results):
    return {result.id: result for result in results}


class DailyCountTest(unittest.TestCase):
    """Skutečný počet kontrol za simulovaných 24 h (dřív 8640 u každé služby)."""

    def test_calls_per_service_in_24_hours(self):
        clock = FakeClock()
        animedb, nas, systemctl = Endpoint(clock), Endpoint(clock), DailyBackup(clock)
        monitor, services = wired(services_config(), clock, animedb, nas, systemctl)
        results, lines, ticks = run_for(monitor, clock, DAY)
        self.assertEqual(len(ticks), DAY // TICK)  # takt smyčky beze změny: 8640 měření systému
        self.assertEqual(animedb.calls, [START + index * 15 * 60 for index in range(96)])
        self.assertEqual(nas.calls, [START + index * 30 * 60 for index in range(48)])
        # Zálohy: po spuštění a po předpokládaném konci zítřejšího běhu (02:00 + 10 min + 5 min rezerva).
        self.assertEqual(systemctl.calls, [START, mono_at(datetime(2026, 10, 30, 2, 15, tzinfo=timezone.utc))])
        self.assertEqual({name: scheduled.checks for name, scheduled in services.items()},
                         {ANIMEDB_ID: 96, NAS_ID: 48, BACKUP_ID: 2})
        self.assertEqual({by_id(results)[name].state for name in services}, {State.OK})

    def test_calls_in_24_hours_with_permanent_failures(self):
        clock = FakeClock()
        animedb, nas = Endpoint(clock, REFUSED), Endpoint(clock, REFUSED)
        systemctl = Flaky(DailyBackup(clock), lambda now: True, timeout_error())
        monitor, services = wired(services_config(), clock, animedb, nas, systemctl)
        results, _, _ = run_for(monitor, clock, DAY)
        # Výpadek: 3 kontroly do potvrzení, pak jen pomalý interval; systemd: dotaz + jediný opravný.
        self.assertEqual([call - START for call in animedb.calls], [0, 30, 60] + [960 + 900 * k for k in range(95)])
        self.assertEqual([call - START for call in nas.calls], [0, 30, 60] + [1860 + 1800 * k for k in range(47)])
        self.assertEqual([call - START for call in systemctl.calls], [0, HOUR])
        self.assertEqual((len(animedb.calls), len(nas.calls), len(systemctl.calls)), (98, 50, 2))
        self.assertEqual({name: by_id(results)[name].state for name in services},
                         {ANIMEDB_ID: State.ERROR, NAS_ID: State.ERROR, BACKUP_ID: State.UNKNOWN})

    def test_calls_in_24_hours_with_transient_failures(self):
        # AnimeDB se restartuje 45 s, NAS má 2h výpadek, jeden dotaz na systemd vyprší.
        clock = FakeClock()
        animedb = Endpoint(clock, lambda now: REFUSED if START + 3 * HOUR <= now < START + 3 * HOUR + 45 else HEALTHY)
        nas = Endpoint(clock, lambda now: REFUSED if START + HOUR <= now < START + 3 * HOUR else HEALTHY)
        after_run = mono_at(datetime(2026, 10, 30, 2, 15, tzinfo=timezone.utc))
        systemctl = Flaky(DailyBackup(clock), lambda now: now == after_run, timeout_error())
        monitor, services = wired(services_config(), clock, animedb, nas, systemctl)
        results, _, ticks = run_for(monitor, clock, DAY)
        self.assertEqual(len(animedb.calls), 96 + 2)  # restart: 2 rychlé pokusy, ERROR nevznikne
        self.assertNotIn(State.ERROR, {results[ANIMEDB_ID].state for _, results in ticks})
        self.assertEqual([call - START for call in nas.calls][2:10],
                         [3600, 3630, 3660, 5460, 7260, 9060, 10860, 10890])  # potvrzení výpadku i obnovy
        self.assertEqual(len(nas.calls), 51)
        self.assertEqual(systemctl.calls, [START, after_run, after_run + HOUR])  # jediný opravný dotaz
        self.assertEqual({by_id(results)[name].state for name in services}, {State.OK})

    def test_backup_still_running_is_checked_every_15_minutes(self):
        clock = FakeClock()
        systemctl = DailyBackup(clock, duration=25 * 60)
        monitor, _ = wired(services_config(animedb=False, nas=False), clock, systemctl=systemctl)
        run_for(monitor, clock, DAY)
        due = mono_at(datetime(2026, 10, 30, 2, 30, tzinfo=timezone.utc))  # 02:00 + 25 min + 5 min
        self.assertEqual(systemctl.calls, [START, due])  # v plánovaném termínu už doběhla
        # Včera 10 min, dnes 40 min: v termínu 02:15 ještě běží, pak po 15 min až do konce (02:40).
        clock = FakeClock()
        systemctl = DailyBackup(clock, duration=lambda day: 40 * 60 if day.day == 30 else 10 * 60)
        monitor, _ = wired(services_config(animedb=False, nas=False), clock, systemctl=systemctl)
        results, _, _ = run_for(monitor, clock, DAY)
        self.assertEqual(systemctl.calls, [START] + [mono_at(datetime(2026, 10, 30, 2, minute, tzinfo=timezone.utc))
                                                     for minute in (15, 30, 45)])
        self.assertIs(by_id(results)[BACKUP_ID].state, State.OK)

    def test_running_job_is_rechecked_after_15_minutes_not_every_tick(self):
        clock = FakeClock(mono_at(datetime(2026, 10, 30, 2, 5, tzinfo=timezone.utc)))
        systemctl = DailyBackup(clock, duration=50 * 60)
        monitor, _ = wired(services_config(animedb=False, nas=False), clock, systemctl=systemctl)
        results, _, _ = run_for(monitor, clock, HOUR)
        self.assertEqual([call - systemctl.calls[0] for call in systemctl.calls], [0, 900, 1800, 2700])
        self.assertIs(by_id(results)[BACKUP_ID].state, State.OK)  # po doběhnutí doložený úspěch

    def test_no_timer_means_at_most_one_query_a_day(self):
        clock = FakeClock()
        systemctl = DailyBackup(clock, timer=False)
        monitor, _ = wired(services_config(animedb=False, nas=False, timer=None), clock, systemctl=systemctl)
        run_for(monitor, clock, 3 * DAY)
        self.assertEqual(systemctl.calls, [START, START + DAY, START + 2 * DAY])

    def test_configured_intervals(self):
        clock = FakeClock()
        animedb, nas = Endpoint(clock), Endpoint(clock)
        monitor, _ = wired(services_config(backup=False, animedb_interval=300, nas_interval=3600), clock, animedb, nas)
        run_for(monitor, clock, DAY)
        self.assertEqual((len(animedb.calls), len(nas.calls)), (288, 24))


class OutageScheduleTest(unittest.TestCase):
    """Rychlé potvrzení výpadku nejvýš dvěma pokusy po 30 s a pak zase pomalý interval."""

    def nas(self, outcome, seconds=3 * HOUR, clock=None, **kwargs):
        clock = clock or FakeClock()
        nas = Endpoint(clock, outcome)
        monitor, services = wired(services_config(animedb=False, backup=False), clock, nas=nas)
        results, lines, ticks = run_for(monitor, clock, seconds, **kwargs)
        return nas, ticks, lines, services[NAS_ID]

    def states(self, ticks, check_id=NAS_ID):
        """Stav po každé skutečné kontrole: [(čas od startu, stav), ...]."""
        seen, changes = None, []
        for now, results in ticks:
            result = results[check_id]
            if result is not seen:
                changes.append((now - START, result.state))
                seen = result
        return changes

    def test_confirmed_outage_returns_to_slow_interval(self):
        nas, ticks, lines, _ = self.nas(REFUSED)
        self.assertEqual([call - START for call in nas.calls], [0, 30, 60, 1860, 3660, 5460, 7260, 9060])
        self.assertEqual(self.states(ticks)[:3], [(0, State.UNKNOWN), (30, State.UNKNOWN), (60, State.ERROR)])
        self.assertIn("NAS: spojení odmítnuto (výpadek zatím nepotvrzen, 1/3)", lines[1])
        self.assertIn("NAS: spojení odmítnuto [ERROR]", lines[7])  # třetí kontrola v čase 60 s
        # Mezi kontrolami zůstává potvrzený ERROR, nikdy OK.
        self.assertEqual({results[NAS_ID].state for now, results in ticks if now >= START + 60}, {State.ERROR})

    def test_recovery_is_confirmed_by_one_fast_retry(self):
        nas, ticks, lines, _ = self.nas(lambda now: REFUSED if now < START + 100 else HEALTHY)
        self.assertEqual([call - START for call in nas.calls][:7], [0, 30, 60, 1860, 1890, 3690, 5490])
        self.assertEqual(self.states(ticks)[2:5], [(60, State.ERROR), (1860, State.ERROR), (1890, State.OK)])
        # První úspěch nic nevyřeší; řádek vznikne, protože se změnil důvod (details).
        [recovering] = [line for line in lines if "odeznívá" in line]
        self.assertIn("(výpadek odeznívá, 1/2) [ERROR]", recovering)
        self.assertTrue(recovering.startswith(f"{utc_at(START + 1860):%Y-%m-%d %H:%M:%S} UTC"))

    def test_failed_or_unknown_recovery_retry_returns_to_slow_interval(self):
        for name, outcome, state in (("neúspěch", REFUSED, State.ERROR), ("UNKNOWN", NO_DESCRIPTORS, State.UNKNOWN)):
            with self.subTest(retry=name):
                nas, ticks, _, _ = self.nas(lambda now: REFUSED if now < START + 100 else
                                            outcome if now == START + 1890 else HEALTHY)
                self.assertEqual([call - START for call in nas.calls][:8], [0, 30, 60, 1860, 1890, 3690, 3720, 5520])
                # Potvrzená závada se bez dvou úspěchů po sobě nevyřeší; pak zase jeden rychlý pokus.
                self.assertEqual(self.states(ticks)[3:7], [(1860, State.ERROR), (1890, state), (3690, State.ERROR),
                                                           (3720, State.OK)])

    def test_flapping_recovery_never_polls_fast_endlessly(self):
        # Po potvrzeném výpadku každá pomalá kontrola uspěje a rychlý pokus selže:
        # nejvýš 2 kontroly na interval, nikdy OK a nikdy rychlé opakování za sebou.
        def flapping(now):
            return HEALTHY if now >= START + 1860 and (now - START - 1860) % 1830 == 0 else REFUSED

        nas, ticks, _, _ = self.nas(flapping, seconds=DAY)
        expected = [0, 30, 60] + [1860 + 1830 * k + offset for k in range(47) for offset in (0, 30)]
        self.assertEqual([call - START for call in nas.calls], expected)
        self.assertNotIn(State.OK, {results[NAS_ID].state for now, results in ticks if now >= START + 60})

    def test_success_ends_the_fast_series(self):
        nas, ticks, _, _ = self.nas(lambda now: REFUSED if now == START else HEALTHY)
        self.assertEqual([call - START for call in nas.calls][:3], [0, 30, 1830])
        self.assertEqual(self.states(ticks)[:2], [(0, State.UNKNOWN), (30, State.OK)])

    def test_fast_series_never_restarts_endlessly(self):
        # Pomalá kontrola i první rychlý pokus selžou, druhý uspěje: řada se ukončí a další
        # začne až s pomalou kontrolou o 30 min později, nikdy hned.
        nas, ticks, _, _ = self.nas([REFUSED, REFUSED, HEALTHY], seconds=DAY)
        period = 60 + 30 * 60
        self.assertEqual([call - START for call in nas.calls],
                         [period * series + offset for series in range(47) for offset in (0, 30, 60)])
        self.assertNotIn(State.ERROR, {results[NAS_ID].state for _, results in ticks})  # nikdy 3 po sobě

    def test_unexpected_or_invalid_answer_is_not_evidence_of_outage(self):
        nas, ticks, _, _ = self.nas(NO_DESCRIPTORS)  # chyba na této straně
        self.assertEqual([call - START for call in nas.calls], [0, 1800, 3600, 5400, 7200, 9000])
        self.assertEqual({results[NAS_ID].state for _, results in ticks}, {State.UNKNOWN})
        clock = FakeClock()
        animedb = Endpoint(clock, response=HTTP_404)  # neplatná odpověď
        monitor, _ = wired(services_config(nas=False, backup=False), clock, animedb)
        _, _, ticks = run_for(monitor, clock, HOUR)
        self.assertEqual([call - START for call in animedb.calls], [0, 900, 1800, 2700])
        self.assertEqual({results[ANIMEDB_ID].state for _, results in ticks}, {State.UNKNOWN})

    def test_unexpected_exception_is_unknown_until_the_next_slow_check(self):
        nas, ticks, _, _ = self.nas(RuntimeError("chyba kolektoru"), seconds=HOUR)
        self.assertEqual([call - START for call in nas.calls], [0, 1800])  # žádné opakování každý takt
        first = ticks[0][1][NAS_ID]
        self.assertIs(first.state, State.UNKNOWN)
        self.assertEqual(first.details[COLLECTOR_FAILURE], "RuntimeError")
        self.assertEqual(first.expires_monotonic, START + 1800 + GRACE)  # do další kontroly, ne déle

    def test_outage_confirmation_sees_each_real_check_exactly_once(self):
        clock = FakeClock()
        nas = Endpoint(clock, REFUSED)
        monitor, services = wired(services_config(animedb=False, backup=False), clock, nas=nas)
        outage = services[NAS_ID].monitor.outage
        confirmed = []
        original = outage.confirm
        outage.confirm = lambda outcome, now: confirmed.append(now) or original(outcome, now)
        run_for(monitor, clock, 2 * HOUR)
        self.assertEqual(confirmed, nas.calls)

    def test_paused_process_does_not_catch_up(self):
        nas, ticks, _, _ = self.nas(HEALTHY, seconds=6 * HOUR, pause=(START + 600, 3 * HOUR))
        resumed = 600 + 3 * HOUR  # první takt po pozastavení; 5 zmeškaných kontrol se nedohání
        self.assertEqual([call - START for call in nas.calls], [0] + [resumed + 1800 * step for step in range(6)])

    def test_monitoring_restart_checks_immediately_without_old_memory(self):
        nas, ticks, _, _ = self.nas(REFUSED, seconds=HOUR)
        self.assertIs(ticks[-1][1][NAS_ID].state, State.ERROR)
        # Nový proces: nic si nepamatuje, kontroluje hned a výpadek potvrzuje znovu.
        clock = FakeClock(START + 2 * HOUR)
        restarted, ticks, _, _ = self.nas(REFUSED, seconds=HOUR, clock=clock)
        self.assertEqual([call - clock.now + HOUR for call in restarted.calls][:3], [0, 30, 60])
        self.assertEqual(self.states(ticks)[:3], [(2 * HOUR, State.UNKNOWN), (2 * HOUR + 30, State.UNKNOWN),
                                                   (2 * HOUR + 60, State.ERROR)])

    def test_outage_confirmation_next_delay_rules(self):
        outage = OutageConfirmation(validity_seconds=1830)
        self.assertEqual(outage.next_delay(1800), 1800)
        outage.confirm("down", 0)
        self.assertEqual(outage.next_delay(1800), CONFIRM_RETRY_SECONDS)
        outage.confirm("down", 30)
        self.assertEqual(outage.next_delay(1800), CONFIRM_RETRY_SECONDS)
        outage.confirm("down", 60)  # potvrzeno
        self.assertEqual(outage.next_delay(1800), 1800)
        outage.confirm("ok", 1860)  # první úspěch po potvrzeném výpadku: jeden rychlý pokus
        self.assertEqual(outage.next_delay(1800), CONFIRM_RETRY_SECONDS)
        outage.confirm("down", 1890)  # pokus selhal: zpět k pomalému intervalu
        self.assertEqual(outage.next_delay(1800), 1800)
        outage.confirm("ok", 3690)
        self.assertEqual(outage.next_delay(1800), CONFIRM_RETRY_SECONDS)
        outage.confirm(None, 3720)  # neznámý výsledek: také pomalu, závada trvá
        self.assertEqual((outage.next_delay(1800), outage.down), (1800, True))
        outage.confirm("ok", 5520)
        outage.confirm("ok", 5550)  # druhý úspěch: obnova potvrzená, pomalu
        self.assertEqual((outage.next_delay(1800), outage.down), (1800, False))


class CacheAndValidityTest(unittest.TestCase):
    """Mezi kontrolami tentýž výsledek: stejný čas měření, stejná platnost, nic se neprodlužuje."""

    def test_cached_result_is_the_same_measurement(self):
        clock = FakeClock()
        monitor, _ = wired(services_config(backup=False), clock, Endpoint(clock), Endpoint(clock))
        _, _, ticks = run_for(monitor, clock, 15 * 60 + TICK)
        first = ticks[0][1][ANIMEDB_ID]
        for now, results in ticks[:90]:
            self.assertIs(results[ANIMEDB_ID], first)  # tentýž objekt, žádná kopie
        self.assertEqual((first.observed_utc, first.expires_monotonic), (START_UTC, START + 900 + GRACE))
        self.assertIsNot(ticks[90][1][ANIMEDB_ID], first)  # nová kontrola v 15. minutě
        self.assertEqual(ticks[90][1][ANIMEDB_ID].observed_utc, START_UTC + timedelta(minutes=15))

    def test_terminal_line_admits_the_age_of_an_earlier_check(self):
        clock = FakeClock()
        monitor, _ = wired(services_config(nas=False, timer=None), clock, Endpoint(clock),
                           systemctl=DailyBackup(clock, timer=False))
        _, lines, ticks = run_for(monitor, clock, 2 * HOUR)
        self.assertIn("AnimeDB: /health odpovídá, status ok |", lines[1])  # právě ověřeno: bez poznámky
        # Souhrnný řádek po 30 min (SCHED-2): AnimeDB právě zkontrolovaná, záloha z dotazu po
        # spuštění; popis nese stáří z okamžiku dotazu a poznámka přizná stáří dotazu.
        [summary] = [line for line in lines if line.startswith(f"{utc_at(START + 1800):%Y-%m-%d %H:%M:%S} UTC")]
        self.assertIn("AnimeDB: /health odpovídá, status ok |", summary)
        self.assertIn("Záloha: úloha naposledy skončila úspěšně před 7.8 h (ověřeno před 30 min)", summary)
        animedb = ticks[0][1][ANIMEDB_ID]
        for seconds, note in ((59, ""), (60, " (ověřeno před 1 min)"), (5 * 60 + 30, " (ověřeno před 5 min)"),
                              (90 * 60, " (ověřeno před 1.5 h)")):
            with self.subTest(seconds=seconds):
                self.assertEqual(result_text(animedb, animedb.observed_utc + timedelta(seconds=seconds)),
                                 animedb.summary + note)

    def test_rgb_acknowledgement_needs_three_real_clean_checks(self):
        clock = FakeClock()
        nas = Endpoint(clock, lambda now: REFUSED if now < START + 100 else HEALTHY)
        monitor, _ = wired(services_config(animedb=False, backup=False), clock, nas=nas)
        _, _, ticks = run_for(monitor, clock, 3 * HOUR)
        acks = rgb.Acknowledgements(AlarmsConfig())
        cleared = None
        for now, results in ticks:
            current = list(results.values())
            if now == START + 70:
                self.assertEqual(acks.acknowledge(current, now), [NAS_ID])  # potvrzený výpadek
            acks.update(current, now)
            if cleared is None and now > START + 70 and NAS_ID not in acks.records:
                cleared = now - START
        # OK od 1890 s (rychlý pokus po prvním úspěchu); potvrzení zanikne až třetím
        # skutečným měřením bez závady, ne třetím taktem.
        self.assertEqual(cleared, 1890 + 2 * 1800)

    def test_result_expires_if_the_next_check_never_comes(self):
        clock = FakeClock()
        monitor, services = wired(services_config(animedb=False, backup=False), clock, nas=Endpoint(clock))
        [result] = [r for r in monitor.collect(START, START_UTC) if r.id == NAS_ID]
        self.assertIs(effective(result, START + 1800 + GRACE - 0.001).state, State.OK)
        self.assertIs(effective(result, START + 1800 + GRACE).state, State.UNKNOWN)  # žádné falešné OK

    def test_alarm_evaluator_counts_each_real_measurement_once(self):
        # Obecný princip identity: měření s prahem kontrolované jen jednou za minutu se při
        # každém taktu předává znovu, ale potvrzení (3 měření) počítá jen skutečná měření.
        class Hot:
            labels = {CPU_TEMP_ID: "Teplota CPU"}
            simulated = False
            validity_seconds = GRACE

            def __init__(self):
                self.calls = []

            def collect(self, now, observed_utc):
                self.calls.append(now)
                return [CheckResult(CPU_TEMP_ID, State.INFO, "Teplota CPU 90 °C", observed_utc, now + 60 + GRACE,
                                    simulated=False, measurement=Measurement(90.0, CELSIUS))]

            def next_check(self, now):
                return now + 60

        rule = AlarmRule(enabled=True, warning=70, error=80, hysteresis=2)
        # (termín, takt smyčky, doba, třetí skutečné měření): služba ze SCHED-1 a teplota po 5 s v taktu 1 s.
        for interval, tick, seconds, third in ((None, None, 3 * 60, 120), (5, 1, 15, 10)):
            with self.subTest(interval=interval, tick=tick):
                hot = Hot()
                clock = FakeClock()
                _, _, ticks = run_for(Collectors(Scheduled(hot, GRACE, interval_seconds=interval)), clock, seconds,
                                      tick_seconds=tick)
                evaluated = []
                evaluator = AlarmEvaluator(AlarmsConfig(cpu_temp=rule))
                for now, results in ticks:
                    [result] = evaluator.evaluate([results[CPU_TEMP_ID]], now)
                    evaluated.append((now - START, result.state))
                self.assertEqual(len(hot.calls), 3)
                first_error = next(offset for offset, state in evaluated if state is State.ERROR)
                self.assertEqual(first_error, third)  # třetí skutečné měření, ne třetí takt


class Counting:
    """Náhrada živého kolektoru v Scheduled (SCHED-2): stejná id a platnost, počítá skutečná měření.

    Hodnoty jsou ve správných jednotkách katalogu historie; `window_ids` vede
    okno zápisu jako skutečné kolektory (vytížení CPU, RX/TX).
    """

    simulated = False
    MEASUREMENTS = {CPU_USAGE_ID: (5.0, PERCENT), CPU_TEMP_ID: (48.0, CELSIUS), MEMORY_ID: (24.0, PERCENT),
                    DISK_ROOT_ID: (31.0, PERCENT), CPU_FAN_ID: (3200.0, RPM), RX_RATE_ID: (1200.0, BYTES_PER_SECOND),
                    TX_RATE_ID: (300.0, BYTES_PER_SECOND)}
    DETAILS = {CPU_FAN_ID: {"fan": "ON"}, RX_RATE_ID: {INTERFACE: "eth0"}, TX_RATE_ID: {INTERFACE: "eth0"},
               LINK_ID: {INTERFACE: "eth0", "link": "UP"}, IPV4_ID: {INTERFACE: "eth0", "ipv4": "192.0.2.10"}}

    def __init__(self, original, window_ids=()):
        self.labels, self.validity_seconds = original.labels, original.validity_seconds
        self.window_ids = set(window_ids)
        self.calls = 0

    def result(self, check_id, observed_utc, expires):
        value = self.MEASUREMENTS.get(check_id)
        return CheckResult(check_id, State.INFO, self.labels.get(check_id, check_id), observed_utc, expires,
                           simulated=False, measurement=None if value is None else Measurement(*value),
                           details=self.DETAILS.get(check_id, {}))

    def collect(self, now, observed_utc):
        self.calls += 1
        return [self.result(check_id, observed_utc, now + self.validity_seconds) for check_id in self.labels]

    def window_results(self, now, observed_utc, validity_seconds):
        return self.window_ids, [self.result(check_id, observed_utc, now + validity_seconds)
                                 for check_id in sorted(self.window_ids)]


def live_counted(config, clock, animedb, nas, systemctl):
    """Celé zapojení run.live_monitor (SCHED-2), živé kolektory nahrazené počítadly, služby podvržené."""
    live = live_monitor(config)
    counters = {}
    for scheduled in live.monitors:
        inner = scheduled.monitor
        if isinstance(inner, AnimeDbMonitor):
            inner.connect, inner.clock = animedb, clock
        elif isinstance(inner, NasMonitor):
            inner.connect = nas
        elif isinstance(inner, BackupMonitor):
            inner.run, inner.clock = systemctl, clock
        else:
            windows = {CPU_USAGE_ID, RX_RATE_ID, TX_RATE_ID} & set(inner.labels)
            scheduled.monitor = Counting(inner, windows)
            counters[tuple(inner.labels)] = scheduled.monitor
    return live, counters


class LiveMeasurementTest(unittest.TestCase):
    """SCHED-2: takt 1 s, kolektory v intervalech 1/5/10/60/1800 s, historie po 10 s."""

    def test_real_measurements_in_24_hours(self):
        clock = FakeClock()
        animedb, nas, systemctl = Endpoint(clock), Endpoint(clock), DailyBackup(clock)
        monitor, counters = live_counted(services_config(), clock, animedb, nas, systemctl)
        link, lines = FakeLink(), []

        def sleep(delay):
            clock.advance(delay)
            if clock.now >= START + DAY:
                raise KeyboardInterrupt

        run_loop(monitor, CollectorConfig(), AlarmEvaluator(AlarmsConfig()), clock=clock,
                 utc_clock=lambda: utc_at(clock.now), sleep=sleep, output=lines.append, history_link=link,
                 tick_seconds=1)
        self.assertEqual({labels: counter.calls for labels, counter in counters.items()}, {
            (CPU_USAGE_ID, CPU_TEMP_ID, MEMORY_ID): 17_280,  # po 5 s
            (DISK_ROOT_ID,): 48,  # po 1800 s
            (CPU_FAN_ID,): 8_640,  # po 10 s
            (LINK_ID, RX_RATE_ID, TX_RATE_ID, IPV4_ID): 86_400,  # čítače po 1 s (rozhraní a IPv4 uvnitř po 60 s)
        })
        self.assertEqual((len(animedb.calls), len(nas.calls), len(systemctl.calls)), (96, 48, 2))  # SCHED-1
        self.assertEqual(len(link.recorded), 8_640)  # zápis do historie po 10 s jako dřív
        self.assertEqual(link.requested, [datetime(2026, 10, 30, 3, 0, tzinfo=timezone.utc)])  # denní snímek
        printed = [line for line in lines if " UTC  " in line]
        self.assertEqual(len(printed), 48)  # beze změny jen souhrn po 30 min, ne 86 400 řádků
        self.assertIn("počet měření: 86400", lines[-1])

    def test_history_gets_windows_and_keeps_its_time_base(self):
        clock = FakeClock()
        monitor, _ = live_counted(services_config(), clock, Endpoint(clock), Endpoint(clock), DailyBackup(clock))
        link = FakeLink()
        run_for(monitor, clock, 2 * HOUR, history_link=link, tick_seconds=1)
        self.assertEqual(len(link.recorded), 720)
        for results, measured in link.recorded[:3]:
            # Okna (CPU, RX, TX) jdou první se stejnou platností jako dřív každé měření.
            self.assertEqual([r.id for r in results[:3]], [CPU_USAGE_ID, RX_RATE_ID, TX_RATE_ID])
            self.assertEqual({r.expires_monotonic - measured for r in results[:3]}, {GRACE})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            base = int(START_UTC.timestamp())
            history.create_database(path, "test", "0123456789abcdef", now_utc=base - 3600)
            database = history.open_writer(path, "test", "0123456789abcdef")
            try:
                recorder = history.HistoryRecorder(database)
                recorder.start_run("test", 10, utc_at(START - 60))
                for start in range(0, len(link.recorded), 6):  # dávky po 60 s jako vlákno zapisovače
                    batch = recorder.begin_batch()
                    for results, _ in link.recorded[start:start + 6]:
                        recorder.add_results(batch, results)
                    recorder.write(batch)
                count = database.connection.execute
                jumps = count("SELECT count(*) FROM event WHERE kind = 'clock.jump'").fetchone()[0]
                samples = dict(count("SELECT m.check_id, count(*) FROM sample s JOIN metric m ON m.id = s.metric_id "
                                     "GROUP BY m.check_id").fetchall())
            finally:
                database.close()
        self.assertEqual(jumps, 0)  # různé intervaly a platnosti nevytvoří falešný skok hodin
        self.assertEqual(dict(recorder.rejected), {})  # žádná odmítnutá data
        # Každý zápis jedno okno a čerstvé měření; disk po 1800 s jen jednou za své měření.
        self.assertEqual(samples, {CPU_USAGE_ID: 720, RX_RATE_ID: 720, TX_RATE_ID: 720, CPU_TEMP_ID: 720,
                                   MEMORY_ID: 720, CPU_FAN_ID: 720, DISK_ROOT_ID: 4})


Statvfs = namedtuple("Statvfs", "f_frsize f_blocks f_bfree f_bavail")


class LiveFixture:
    """Skutečné SystemMonitor a NetworkMonitor (SCHED-2) nad soubory, jejichž hodnoty jsou funkcí času.

    Před každým taktem zapíše /proc/stat, /proc/meminfo, teplotu a čítače sítě
    pro simulovaný čas, takže přesnou hodnotu za libovolný úsek jde spočítat:
    jiffy CPU busy(t) a total(t), bajty rx(t). Nic se nečte ze skutečného systému.
    """

    def __init__(self, directory, clock, temperature=None, stalls=None):
        base = Path(directory)
        self.proc, self.thermal, self.net = base / "proc", base / "thermal", base / "net"
        (self.thermal / "thermal_zone0").mkdir(parents=True)
        (self.thermal / "thermal_zone0" / "type").write_text("x86_pkg_temp\n", encoding="ascii")
        (self.net / "eth0" / "statistics").mkdir(parents=True)
        (self.net / "eth0" / "device").mkdir()
        for name, value in (("operstate", "up"), ("carrier", "1"), ("ifindex", "2")):
            (self.net / "eth0" / name).write_text(f"{value}\n", encoding="ascii")
        self.route = base / "route"
        self.route.write_text("Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
                              "eth0\t00000000\t010200C0\t0003\t0\t0\t100\t00000000\t0\t0\t0\n", encoding="ascii")
        self.proc.mkdir()
        self.clock = clock
        self.temperature = temperature or (lambda t: 50 + (int(t) // 5) % 7 - 3)  # kolísá o ±3 °C
        self.stalls = stalls or {}  # sekunda -> zdržení měření (pomalá služba)
        statvfs = lambda path: Statvfs(4096, 50_000_000, 40_000_000, 37_500_000)
        self.monitor = Collectors(
            Scheduled(SystemMonitor(15, proc_dir=self.proc, thermal_dir=self.thermal, statvfs=statvfs,
                                    checks=(CPU_USAGE_ID, CPU_TEMP_ID, MEMORY_ID)), GRACE, interval_seconds=5),
            Scheduled(SystemMonitor(5400, proc_dir=self.proc, thermal_dir=self.thermal, statvfs=statvfs,
                                    checks=(DISK_ROOT_ID,)), GRACE, interval_seconds=1800),
            Scheduled(NetworkMonitor(3, net_dir=self.net, route_path=self.route, ipv4_lookup=lambda name: "192.0.2.10",
                                     details_interval=60, gap_seconds=30), GRACE, interval_seconds=1),
        )
        self.labels, self.simulated, self.validity_seconds = (self.monitor.labels, self.monitor.simulated,
                                                              self.monitor.validity_seconds)

    # Takty jsou celé sekundy; čítače jen rostou (pokles by byl reset čítače).
    @staticmethod
    def busy(t):  # vytížení po 5 s postupně 10, 20, … 80 % a znovu
        n = math.floor(t)
        blocks, rest = divmod(n, 5)
        level = blocks % 8
        return 10 * n + 10 * (5 * (28 * (blocks // 8) + level * (level - 1) // 2) + rest * level)

    @staticmethod
    def total(t):
        return 100 * math.floor(t)  # USER_HZ 100

    @staticmethod
    def rx(t):  # každou sekundu jiná rychlost: 1000 až 2500 B/s
        n = math.floor(t)
        return 1000 * n + 500 * (6 * (n // 4) + (n % 4) * (n % 4 - 1) // 2)

    def write(self, t):
        busy, total = round(self.busy(t)), round(self.total(t))
        (self.proc / "stat").write_text(f"cpu  {busy} 0 0 {total - busy} 0 0 0 0 0 0\n", encoding="ascii")
        available = 6_000_000 - 50_000 * (int(t) // 5 % 9)  # využití RAM se průběžně mění
        (self.proc / "meminfo").write_text(f"MemTotal: 8000000 kB\nMemFree: 1000000 kB\nMemAvailable: {available} kB\n",
                                           encoding="ascii")
        (self.thermal / "thermal_zone0" / "temp").write_text(f"{round(self.temperature(t) * 1000)}\n", encoding="ascii")
        statistics = self.net / "eth0" / "statistics"
        (statistics / "rx_bytes").write_text(f"{self.rx(t)}\n", encoding="ascii")
        (statistics / "tx_bytes").write_text(f"{200 * round(t)}\n", encoding="ascii")

    def collect(self, now, observed_utc):
        self.write(now - START)
        results = self.monitor.collect(now, observed_utc)
        self.clock.advance(self.stalls.get(now - START, 0))
        return results

    def window_results(self, now, observed_utc, validity_seconds):
        return self.monitor.window_results(now, observed_utc, validity_seconds)


class LiveFixtureTest(unittest.TestCase):
    """SCHED-2 se skutečnými kolektory: přesná okna historie a tichý výpis při kolísajících hodnotách."""

    def test_history_windows_cover_exactly_the_record_interval(self):
        # (interval historie, zdržení smyčky): povolené násobky 5 s i po zdržení 12 s přesně;
        # 7 a 11 s konfigurace odmítne, ale ani obejitá validace nedá hodnotu za jiný úsek.
        for interval, stalls in ((10, {}), (10, {40: 12}), (15, {40: 12}), (7, {}), (11, {})):
            with self.subTest(interval=interval, stalls=stalls), tempfile.TemporaryDirectory() as directory:
                clock = FakeClock()
                live, link = LiveFixture(directory, clock, stalls=stalls), FakeLink()
                run_for(live, clock, 300, history_link=link, tick_seconds=1,
                        config=CollectorConfig(interval_seconds=interval))
                previous, cpu_states = None, set()
                for results, measured in link.recorded:
                    windows = {r.id: r for r in results[:3]}
                    if previous is not None:
                        a, b = previous - START, measured - START
                        rx = windows[RX_RATE_ID]  # síť po 1 s: vždy přesně skutečný úsek [a, b]
                        self.assertEqual((rx.state, rx.measurement.value),
                                         (State.INFO, (LiveFixture.rx(b) - LiveFixture.rx(a)) / (b - a)))
                        cpu = windows.get(CPU_USAGE_ID)
                        if cpu is not None:
                            cpu_states.add(cpu.state)
                            if cpu.state is State.INFO:  # jen za přesně pokrytý úsek, nikdy za jiný
                                expected = 100 * (LiveFixture.busy(b) - LiveFixture.busy(a)) / (
                                    LiveFixture.total(b) - LiveFixture.total(a))
                                self.assertAlmostEqual(cpu.measurement.value, expected, places=9)
                    for window in results[:3]:  # čas a platnost zápisu: časová základna historie
                        self.assertEqual((window.observed_utc, window.expires_monotonic),
                                         (utc_at(measured), measured + 3 * interval))
                    previous = measured
                if interval % 5 == 0:
                    self.assertEqual(cpu_states, {State.INFO})  # i po zdržení se mřížky znovu sejdou
                else:
                    self.assertIn(State.UNKNOWN, cpu_states)  # nepokryté okno přiznané, ne posunutá hodnota

    def test_fluctuating_values_print_only_state_changes(self):
        rule = AlarmsConfig(cpu_temp=AlarmRule(enabled=True, warning=70, error=80, hysteresis=2))
        with tempfile.TemporaryDirectory() as directory:
            clock = FakeClock()
            live = LiveFixture(directory, clock, temperature=lambda t: (75 if t >= 3600 else 50) + int(t) // 5 % 3)
            _, lines, ticks = run_for(live, clock, 2 * HOUR, tick_seconds=1, alarms=rule)
        printed = [line for line in lines if " UTC  " in line]
        # První měření, první rychlost sítě, první vytížení CPU, souhrny po 30 min a nová závada.
        self.assertEqual([line[11:19] for line in printed],
                         ["10:00:00", "10:00:01", "10:00:05", "10:30:05", "11:00:05", "11:00:10", "11:30:10"])
        self.assertIn("[WARNING]", printed[5])
        # Hodnoty se přitom měnily: OLED a web je mají aktuální z výsledků každého taktu.
        for check_id, at_least in ((CPU_USAGE_ID, 5), (MEMORY_ID, 5), (CPU_TEMP_ID, 3), (RX_RATE_ID, 3)):
            with self.subTest(check_id=check_id):
                self.assertGreaterEqual(len({results[check_id].summary for _, results in ticks}), at_least)
        self.assertEqual(len(ticks), 2 * HOUR)  # takt 1 s, ale výpis jen 7 řádků


class BackupBetweenQueriesTest(unittest.TestCase):
    """Přechod přes 26/50 h z doloženého úspěchu bez dalšího dotazu na systemd."""

    def test_age_crossing_without_query(self):
        clock = FakeClock()
        success_end = START - 20 * HOUR
        systemctl = StaticSystemctl(clock, service_block(success_end - 600, success_end))
        monitor, _ = wired(services_config(animedb=False, nas=False, timer=None), clock, systemctl=systemctl)
        _, lines, ticks = run_for(monitor, clock, 2 * DAY)
        self.assertEqual(systemctl.calls, [START, START + DAY])  # bez timeru nejvýš jednou denně
        states = []
        for now, results in ticks:
            result = results[BACKUP_ID]
            if not states or result is not states[-1][1]:
                states.append((now - START, result))
        self.assertEqual([(offset, result.state) for offset, result in states],
                         [(0, State.OK), (6 * HOUR + TICK, State.WARNING), (DAY, State.WARNING),
                          (30 * HOUR + TICK, State.ERROR)])
        crossing = states[1][1]
        # Čas přepočtu, ale původní platnost (do dalšího dotazu); popis přizná stáří dotazu.
        self.assertEqual(crossing.observed_utc, utc_at(START + 6 * HOUR + TICK))
        self.assertEqual(crossing.expires_monotonic, states[0][1].expires_monotonic)
        self.assertIn("(nad 26 h) (přepočet bez dotazu, údaje systemd z dotazu před 6.0 h)", crossing.summary)
        self.assertEqual(dict(crossing.details), {"backup": "overdue"})
        self.assertNotIn("přepočet", states[2][1].summary)  # nový dotaz: čerstvé údaje
        self.assertIs(states[3][1].state, State.ERROR)
        self.assertIn("(nad 50 h) (přepočet bez dotazu", states[3][1].summary)

    def test_unreadable_query_is_not_recomputed(self):
        clock = FakeClock()
        systemctl = StaticSystemctl(clock, "nesmysl")
        monitor, _ = wired(services_config(animedb=False, nas=False, timer=None), clock, systemctl=systemctl)
        _, _, ticks = run_for(monitor, clock, DAY)
        self.assertEqual(systemctl.calls, [START])
        self.assertEqual({results[BACKUP_ID].state for _, results in ticks}, {State.UNKNOWN})

    def test_one_retry_after_transient_query_failure(self):
        clock = FakeClock()
        systemctl = Flaky(DailyBackup(clock), lambda now: now == START, timeout_error())
        monitor, _ = wired(services_config(animedb=False, nas=False), clock, systemctl=systemctl)
        _, _, ticks = run_for(monitor, clock, DAY)
        due = mono_at(datetime(2026, 10, 30, 2, 15, tzinfo=timezone.utc))
        self.assertEqual(systemctl.calls, [START, START + HOUR, due])  # opravný dotaz, pak běžný plán
        failed = ticks[0][1][BACKUP_ID]
        self.assertIs(failed.state, State.UNKNOWN)  # žádný dřívější úspěch
        self.assertIn("systemd neodpověděl do 2 s; opravný dotaz za 60 min", failed.summary)
        self.assertEqual(failed.expires_monotonic, START + HOUR + GRACE)
        self.assertIs(ticks[360][1][BACKUP_ID].state, State.OK)
        self.assertEqual(ticks[360][1][BACKUP_ID].observed_utc, utc_at(START + HOUR))  # nové ověření

    def test_failed_retry_returns_to_the_normal_plan(self):
        clock = FakeClock()
        systemctl = Flaky(DailyBackup(clock), lambda now: True, 1)  # systemctl skončí kódem 1
        monitor, _ = wired(services_config(animedb=False, nas=False), clock, systemctl=systemctl)
        _, _, ticks = run_for(monitor, clock, 2 * DAY)
        self.assertEqual(systemctl.calls, [START, START + HOUR, START + HOUR + DAY, START + 2 * HOUR + DAY])
        self.assertEqual({results[BACKUP_ID].state for _, results in ticks}, {State.UNKNOWN})

    def test_previous_success_is_never_reused_after_a_failed_query(self):
        clock = FakeClock()
        after_run = mono_at(datetime(2026, 10, 30, 2, 15, tzinfo=timezone.utc))
        systemctl = Flaky(DailyBackup(clock), lambda now: now == after_run, timeout_error())
        monitor, _ = wired(services_config(animedb=False, nas=False), clock, systemctl=systemctl)
        _, _, ticks = run_for(monitor, clock, DAY)
        states = {now - START: results[BACKUP_ID] for now, results in ticks}
        offset = after_run - START
        self.assertIs(states[offset - TICK].state, State.OK)
        # Selhaný dotaz je UNKNOWN až do opravného pokusu: dřívější OK ani jeho přepočet se nepoužije.
        self.assertEqual({states[offset + step].state for step in range(0, HOUR, TICK)}, {State.UNKNOWN})
        self.assertIs(states[offset + HOUR].state, State.OK)

    def test_only_transient_query_failures_get_a_retry(self):
        cases = {
            "timeout": (timeout_error(), True),
            "nenulový kód": (1, True),
            "nedostatek prostředků": (BlockingIOError(errno.EAGAIN, "Resource temporarily unavailable"), True),
            "chybějící systemctl": (FileNotFoundError(errno.ENOENT, "No such file"), False),
            "nespustitelný systemctl": (PermissionError(errno.EACCES, "Permission denied"), False),
        }
        for name, (error, retried) in cases.items():
            with self.subTest(case=name):
                clock = FakeClock()
                systemctl = Flaky(DailyBackup(clock), lambda now: now == START, error)
                monitor, _ = wired(services_config(animedb=False, nas=False), clock, systemctl=systemctl)
                run_for(monitor, clock, 2 * HOUR)
                self.assertEqual(systemctl.calls, [START, START + HOUR] if retried else [START])
        future = service_block(START + 60, START + 120)  # rozporné údaje: konec běhu v budoucnosti
        for name, blocks in (("neplatný výstup", ("nesmysl",)), ("rozporné časy", (future,)),
                             ("chybějící služba", (service_block(0, 0, code=0).replace("=loaded", "=not-found"),))):
            with self.subTest(case=name):
                clock = FakeClock()
                systemctl = StaticSystemctl(clock, *blocks)
                monitor, _ = wired(services_config(animedb=False, nas=False, timer=None), clock, systemctl=systemctl)
                run_for(monitor, clock, DAY)
                self.assertEqual(systemctl.calls, [START])  # žádný opravný pokus, nejvýš jednou denně

    def test_history_sees_one_change_per_crossing(self):
        clock = FakeClock()
        success_end = START - 20 * HOUR
        systemctl = StaticSystemctl(clock, service_block(success_end - 600, success_end))
        monitor, _ = wired(services_config(animedb=False, nas=False, timer=None), clock, systemctl=systemctl)
        link = FakeLink()
        run_for(monitor, clock, 2 * DAY, history_link=link)
        tracker = history.ChangeTracker()
        changes = []
        for results, measured in link.recorded:
            [result] = [r for r in results if r.id == BACKUP_ID]
            change = tracker.observe(result, int(result.observed_utc.timestamp()), "synced")
            if change is not None:
                changes.append((change.from_state, change.to_state, change.ts - int(START_UTC.timestamp())))
        self.assertEqual(changes, [(None, "OK", 0), ("OK", "WARNING", 6 * HOUR + TICK),
                                   ("WARNING", "ERROR", 30 * HOUR + TICK)])


class HistoryAndSnapshotTest(unittest.TestCase):
    """Historie nedostane stará měření jako nová a denní plán snímků běží dál."""

    def test_history_gets_each_real_check_once_and_snapshots_stay_daily(self):
        clock = FakeClock()
        animedb, nas = Endpoint(clock), Endpoint(clock, lambda now: REFUSED if START + HOUR <= now < START + 2 * HOUR
                                                    else HEALTHY)
        monitor, _ = wired(services_config(), clock, animedb, nas, DailyBackup(clock))
        link = FakeLink(daily_time=(3, 0))
        run_for(monitor, clock, 2 * DAY, history_link=link)
        self.assertEqual(len(link.recorded), 2 * DAY // TICK)  # předání po každém taktu jako dosud
        identities = {}
        for results, measured in link.recorded:
            for result in results:
                if result.id in (ANIMEDB_ID, NAS_ID):
                    identities.setdefault(result.id, set()).add((result.observed_utc, result.expires_monotonic))
        self.assertEqual({key: len(value) for key, value in identities.items()},
                         {ANIMEDB_ID: len(animedb.calls), NAS_ID: len(nas.calls)})
        # Denní snímek: jednou denně ve 03:00 UTC, takt smyčky beze změny.
        self.assertEqual(link.requested, [datetime(2026, 10, 30, 3, 0, tzinfo=timezone.utc),
                                          datetime(2026, 10, 31, 3, 0, tzinfo=timezone.utc)])

    def test_recorder_writes_cached_result_only_once(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            base = 1_800_003_600
            history.create_database(path, "test", "0123456789abcdef", now_utc=base - 3600)
            database = history.open_writer(path, "test", "0123456789abcdef")
            try:
                recorder = history.HistoryRecorder(database)
                recorder.start_run("test", TICK, datetime.fromtimestamp(base - 60, timezone.utc))
                cached = CheckResult(NAS_ID, State.OK, "NAS: ok", datetime.fromtimestamp(base, timezone.utc),
                                     1000.0 + 1800 + GRACE, simulated=False, details={"health": "ok"})
                for index in range(3):
                    moment = datetime.fromtimestamp(base + index * TICK, timezone.utc)
                    cpu = CheckResult(CPU_TEMP_ID, State.INFO, "Teplota CPU 50 °C", moment, 1000.0 + index * TICK + GRACE,
                                      simulated=False, measurement=Measurement(50.0, CELSIUS))
                    batch = recorder.begin_batch()
                    recorder.add_results(batch, [cpu, cached])
                    recorder.write(batch)
                self.assertEqual(recorder.duplicates, 2)  # totéž měření NAS ve 2. a 3. taktu
                rows = database.connection.execute(
                    "SELECT count(*) FROM check_change WHERE check_id = ?", (NAS_ID,)).fetchone()[0]
                self.assertEqual(rows, 1)
            finally:
                database.close()


class ConfigurationTest(unittest.TestCase):
    def test_defaults_and_services_stay_disabled(self):
        self.assertEqual((ANIMEDB_INTERVAL_DEFAULT, NAS_INTERVAL_DEFAULT), (900, 1800))
        config = Config()
        self.assertEqual((config.services.animedb.enabled, config.services.nas.enabled,
                          config.services.backup.enabled), (False, False, False))
        self.assertEqual((config.services.animedb.interval_seconds, config.services.nas.interval_seconds), (900, 1800))
        example = load_config(EXAMPLE_CONFIG)
        self.assertEqual((example.services.animedb.enabled, example.services.nas.enabled,
                          example.services.backup.enabled), (False, False, False))
        self.assertEqual((example.services.animedb.interval_seconds, example.services.nas.interval_seconds),
                         (900, 1800))
        self.assertEqual(live_monitor(example).labels.keys() & {ANIMEDB_ID, NAS_ID, BACKUP_ID}, set())

    def test_intervals_from_file_and_invalid_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(f'[services.animedb]\nurl = "{URL}"\ninterval_seconds = 600\n'
                            f'[services.nas]\nhost = "{HOST}"\ninterval_seconds = 3600\n', encoding="utf-8")
            services = load_config(path).services
            self.assertEqual((services.animedb.interval_seconds, services.nas.interval_seconds), (600, 3600))
            for value in ("59", "3601", "0", "-1", "true", "900.0", '"900"'):
                for section, key in (("animedb", f'url = "{URL}"'), ("nas", f'host = "{HOST}"')):
                    with self.subTest(section=section, value=value):
                        path.write_text(f"[services.{section}]\n{key}\ninterval_seconds = {value}\n",
                                        encoding="utf-8")
                        with self.assertRaisesRegex(ConfigError, rf"\[services.{section}\] interval_seconds"):
                            load_config(path)
            path.write_text("[services.backup]\ninterval_seconds = 900\n", encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "neznámé klíče"):  # zálohy se řídí plánem, ne intervalem
                load_config(path)

    def test_monitor_validates_interval(self):
        with self.assertRaises(ValueError):
            AnimeDbMonitor(930, URL, 2, connect=Endpoint(FakeClock()), interval_seconds=30)
        with self.assertRaises(ValueError):
            NasMonitor(1830, HOST, 445, 2, connect=Endpoint(FakeClock()), interval_seconds=True)

    def test_live_monitor_validity_follows_interval(self):
        config = services_config(collector=CollectorConfig(interval_seconds=5), animedb_interval=600, nas_interval=1200)
        monitor = live_monitor(config)  # jen vytvoření
        animedb, nas, backup = monitor.monitors[4:]
        self.assertEqual((animedb.monitor.validity_seconds, nas.monitor.validity_seconds,
                          backup.monitor.validity_seconds), (615, 1215, 15))
        self.assertEqual({scheduled.validity_seconds for scheduled in (animedb, nas, backup)}, {15})
        self.assertEqual(monitor.validity_seconds, 15)


class BackupPlanTest(unittest.TestCase):
    """Termín dalšího dotazu podle údajů systemd (BackupMonitor.plan)."""

    def plan(self, *blocks, utc=START_UTC, timer=TIMER):
        clock = FakeClock()
        systemctl = StaticSystemctl(clock, *blocks)
        monitor = BackupMonitor(GRACE, UNIT, timer, 2, run=systemctl, clock=clock)
        [result] = monitor.collect(START, utc)
        self.assertEqual(result.expires_monotonic, monitor.next_check(START) + GRACE)
        return monitor.next_check(START) - START

    def test_next_run_from_the_timer(self):
        done = service_block(START - 4000, START - 3400)  # poslední běh trval 600 s
        self.assertEqual(self.plan(done, timer_block(START_UTC + timedelta(hours=16))),
                         16 * HOUR + 600 + SETTLE_SECONDS)
        # Týdenní timer: nepředpokládá se denní plán, ale dotaz nejvýš po dni.
        self.assertEqual(self.plan(done, timer_block(START_UTC + timedelta(days=7))), FALLBACK_SECONDS)

    def test_timer_already_due_but_job_not_started(self):
        done = service_block(START - 4000, START - 3400)
        self.assertEqual(self.plan(done, timer_block(START_UTC - timedelta(minutes=5))), RECHECK_SECONDS)
        self.assertEqual(self.plan(done, timer_block(START_UTC - timedelta(seconds=PENDING_SECONDS))),
                         RECHECK_SECONDS)
        self.assertEqual(self.plan(done, timer_block(START_UTC - timedelta(seconds=PENDING_SECONDS + 1))),
                         FALLBACK_SECONDS)

    def test_unknown_plan_falls_back_to_once_a_day(self):
        done = service_block(START - 4000, START - 3400)
        cases = {
            "bez timeru": (done,),
            "neaktivní timer": (done, timer_block(active="inactive")),
            "jen monotónní plán": (done, timer_block(raw="")),
            "jiný tvar": (done, timer_block(raw="Fri 2026-10-30 03:00:00 CET")),
            "nesmysl": (done, timer_block(raw="x")),
            "nečitelný výstup": ("nesmysl",),
        }
        for name, blocks in cases.items():
            with self.subTest(case=name):
                timer = None if name in ("bez timeru", "nečitelný výstup") else TIMER
                self.assertEqual(self.plan(*blocks, timer=timer), FALLBACK_SECONDS)

    def test_missing_service_falls_back(self):
        missing = service_block(0, 0, code=0).replace("LoadState=loaded", "LoadState=not-found")
        self.assertEqual(self.plan(missing, timer_block(START_UTC + timedelta(hours=1))), FALLBACK_SECONDS)

    def test_running_job(self):
        running = service_block(START - 60, 0, active="activating", sub="start", code=0)
        self.assertEqual(self.plan(running, timer_block(START_UTC + timedelta(days=1))), RECHECK_SECONDS)

    def test_systemd_time_format(self):
        self.assertEqual(next_elapse({"NextElapseUSecRealtime": "Thu 2026-10-29 23:00:00 UTC"}),
                         datetime(2026, 10, 29, 23, 0, tzinfo=timezone.utc).timestamp())
        for value in ("", "n/a", "Thu 2026-10-29 23:00:00 CET", "2026-10-29 23:00:00 UTC", "Thu 2026-13-29 23:00:00 UTC"):
            with self.subTest(value=value):
                self.assertIsNone(next_elapse({"NextElapseUSecRealtime": value}))
        self.assertIsNone(next_elapse({}))


if __name__ == "__main__":
    unittest.main()
