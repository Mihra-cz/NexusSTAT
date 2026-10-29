"""Testy šetrného plánování kontrol služeb (SCHED-1) se simulovaným časem.

Žádné skutečné čekání, síť ani systemd: spojení AnimeDB a NAS i výstup
systemctl jsou podvržené a počítají skutečná volání, čas je číslo. Kolektory
služeb se berou přímo z run.live_monitor (stejné intervaly, platnosti
a obal Scheduled jako v provozu); vymění se jen spojení, systemctl a hodiny.
"""

import errno
import subprocess
import tempfile
import unittest
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
from nexusstat.nas_monitor import NasMonitor
from nexusstat.run import COLLECTOR_FAILURE, Collectors, Scheduled, live_monitor, run_loop
from nexusstat.services import ANIMEDB_ID, BACKUP_ID, CONFIRM_RETRY_SECONDS, NAS_ID, OutageConfirmation
from nexusstat.system_monitor import CELSIUS, CPU_TEMP_ID, LABELS as SYSTEM_LABELS

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
    for scheduled in live.monitors[3:]:
        inner = scheduled.monitor
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


def run_for(monitor, clock, seconds, history_link=None, pause=None):
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

    results = run_loop(Recording(), CollectorConfig(), AlarmEvaluator(AlarmsConfig()), clock=clock,
                       utc_clock=lambda: utc_at(clock.now), sleep=sleep, output=lines.append,
                       history_link=history_link)
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
        self.assertIn("(výpadek odeznívá, 1/2) [ERROR]", lines[1 + 186])  # první úspěch nic nevyřeší

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
        _, lines, _ = run_for(monitor, clock, 2 * HOUR)
        self.assertIn("AnimeDB: /health odpovídá, status ok |", lines[1])  # právě ověřeno: bez poznámky
        self.assertNotIn("ověřeno před", lines[6])  # 50 s: méně než minuta
        self.assertIn("AnimeDB: /health odpovídá, status ok (ověřeno před 5 min)", lines[1 + 30])
        self.assertIn("AnimeDB: /health odpovídá, status ok |", lines[1 + 90])  # nová kontrola v 15. minutě
        # Popis nese stáří z okamžiku dotazu; poznámka přizná, že dotaz je 1,5 h starý.
        self.assertIn("Záloha: úloha naposledy skončila úspěšně před 7.8 h (ověřeno před 1.5 h)", lines[1 + 540])

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
        hot = Hot()
        clock = FakeClock()
        _, _, ticks = run_for(Collectors(Scheduled(hot, GRACE)), clock, 3 * 60)
        evaluated = []
        evaluator = AlarmEvaluator(AlarmsConfig(cpu_temp=rule))
        for now, results in ticks:
            [result] = evaluator.evaluate([results[CPU_TEMP_ID]], now)
            evaluated.append((now - START, result.state))
        self.assertEqual(len(hot.calls), 3)
        first_error = next(offset for offset, state in evaluated if state is State.ERROR)
        self.assertEqual(first_error, 120)  # třetí skutečné měření, ne třetí takt


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
        animedb, nas, backup = monitor.monitors[3:]
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
