"""Testy denního plánování a diagnostiky snímků historie (M3.4b-1).

Vše s injektovanými hodinami a neměnnými snímky stavu: žádné skutečné čekání,
žádná SQLite, žádný adresář snímků a žádné vlákno. Plán se počítá z UTC
a zadaného časového pásma, takže přechody letního a zimního času i skoky
systémového času jde ověřit přesně.
"""

import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from types import MappingProxyType
from zoneinfo import ZoneInfo

from nexusstat import history_link
from nexusstat.core import State
from nexusstat.history_worker import RUNNING, MaintenanceStatus, SnapshotStatus, WorkerStatus
from nexusstat.snapshot_plan import DailyPlan, SnapshotPlanner

PRAGUE = ZoneInfo("Europe/Prague")
UTC = timezone.utc
BASE = 1_800_003_600
HOUR = 3600
SPRING = date(2026, 3, 29)  # 02:00 místního času neexistuje
AUTUMN = date(2026, 10, 25)  # 02:00 místního času existuje dvakrát


def moment(year, month, day, hour=0, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def ready(**options):
    """Stav snímků po prohledání adresáře (bez něj se nic neplánuje)."""
    options.setdefault("scanned", True)
    return SnapshotStatus(**options)


def status(snap=None, *, time_base="synced", confirmed_seq=0):
    """WorkerStatus s daným stavem snímku; ostatní údaje tyto testy nezajímají."""
    maintenance = MaintenanceStatus(snapshot=snap if snap is not None else ready())
    return WorkerStatus(RUNNING, 1, 1, 100.0, BASE, 0, 0, 0, 0, 0, 0, 0, 0, 0, time_base,
                        MappingProxyType({}), MappingProxyType({}), None, None, 0, None,
                        maintenance=maintenance, confirmed_seq=confirmed_seq)


class DailyPlanTest(unittest.TestCase):
    """Denní plán v zadaném pásmu (DailyPlan)."""

    def test_time_and_zone_must_be_valid(self):
        for hour in (24, -1, True, "2", 2.5, None):
            with self.subTest(hour=hour):
                with self.assertRaises(ValueError):
                    DailyPlan(hour, 0, PRAGUE)
        for minute in (60, -1, True, "15", 0.5, None):
            with self.subTest(minute=minute):
                with self.assertRaises(ValueError):
                    DailyPlan(2, minute, PRAGUE)
        with self.assertRaises(ValueError):
            DailyPlan(2, 0, None)  # pásmo se nikdy nedovozuje z prostředí procesu

    def test_planned_instant_is_the_local_hour(self):
        plan = DailyPlan(2, 0, PRAGUE)
        summer = plan.scheduled_for(date(2026, 7, 1))
        winter = plan.scheduled_for(date(2026, 1, 15))
        self.assertEqual(summer, moment(2026, 7, 1, 0))  # 02:00 CEST = 00:00 UTC
        self.assertEqual(winter, moment(2026, 1, 15, 1))  # 02:00 CET = 01:00 UTC
        for instant in (summer, winter):
            self.assertEqual(instant.astimezone(PRAGUE).hour, 2)

    def test_due_at_uses_the_local_day(self):
        plan = DailyPlan(2, 0, PRAGUE)
        # 23:30 UTC je v Praze už další den, takže plán patří tomu dalšímu dni.
        self.assertEqual(plan.due_at(moment(2026, 7, 1, 23, 30)), moment(2026, 7, 2, 0))
        self.assertEqual(plan.local_date(moment(2026, 7, 1, 23, 30)), date(2026, 7, 2))

    def test_next_after_skips_to_the_following_day(self):
        plan = DailyPlan(2, 0, PRAGUE)
        today = plan.scheduled_for(date(2026, 7, 1))
        self.assertEqual(plan.next_after(today - timedelta(minutes=1)), today)
        self.assertEqual(plan.next_after(today), plan.scheduled_for(date(2026, 7, 2)))
        self.assertEqual(plan.next_after(today + timedelta(hours=5)), plan.scheduled_for(date(2026, 7, 2)))

    def test_spring_transition_uses_the_first_existing_instant(self):
        # V den jarního posunu 02:00 místního času neexistuje: použije se 03:00,
        # takže se den nikdy nevynechá ani nevznikne čas mimo pásmo.
        plan = DailyPlan(2, 0, PRAGUE)
        instant = plan.scheduled_for(SPRING)
        self.assertEqual(instant, moment(2026, 3, 29, 1))
        local = instant.astimezone(PRAGUE)
        self.assertEqual((local.hour, local.minute), (3, 0))
        self.assertEqual(local.date(), SPRING)

    def test_autumn_transition_uses_the_first_occurrence(self):
        # 02:00 nastane dvakrát (CEST a CET); plán míří na první výskyt a druhý
        # se už nespustí, protože den je tím vyřízený.
        plan = DailyPlan(2, 0, PRAGUE)
        instant = plan.scheduled_for(AUTUMN)
        self.assertEqual(instant, moment(2026, 10, 25, 0))
        self.assertEqual(instant.astimezone(PRAGUE).utcoffset(), timedelta(hours=2))
        second = moment(2026, 10, 25, 1)  # druhý výskyt 02:00, už v CET
        self.assertEqual(second.astimezone(PRAGUE).hour, 2)
        self.assertEqual(plan.due_at(second), instant)  # pořád týž plánovaný okamžik

    def test_next_after_is_always_in_the_future(self):
        plan = DailyPlan(2, 0, PRAGUE)
        start = moment(2026, 3, 1)
        for step in range(0, 250 * 24):  # po hodině přes jarní i podzimní přechod
            now = start + timedelta(hours=step)
            following = plan.next_after(now)
            self.assertGreater(following, now)
            self.assertLessEqual(following - now, timedelta(hours=25))

    def test_minutes_are_part_of_the_plan(self):
        plan = DailyPlan(3, 15, PRAGUE)
        instant = plan.scheduled_for(date(2026, 7, 1))
        self.assertEqual(instant, moment(2026, 7, 1, 1, 15))  # 03:15 CEST = 01:15 UTC
        local = instant.astimezone(PRAGUE)
        self.assertEqual((local.hour, local.minute), (3, 15))
        self.assertEqual(plan.next_after(instant), plan.scheduled_for(date(2026, 7, 2)))
        self.assertEqual(plan.next_after(instant - timedelta(seconds=1)), instant)

    def test_midnight_and_last_minute_plans(self):
        midnight = DailyPlan(0, 0, PRAGUE)
        latest = DailyPlan(23, 59, PRAGUE)
        start = midnight.scheduled_for(date(2026, 7, 1))
        self.assertEqual(start.astimezone(PRAGUE).strftime("%H:%M"), "00:00")
        self.assertEqual(midnight.next_after(start), midnight.scheduled_for(date(2026, 7, 2)))
        last = latest.scheduled_for(date(2026, 7, 1))
        self.assertEqual(last.astimezone(PRAGUE).strftime("%H:%M"), "23:59")
        self.assertEqual(latest.next_after(last) - last, timedelta(days=1))

    def test_spring_transition_with_minutes(self):
        # 02:30 v den jarního posunu neexistuje: použije se 03:30 místního času.
        plan = DailyPlan(2, 30, PRAGUE)
        instant = plan.scheduled_for(SPRING)
        self.assertEqual(instant, moment(2026, 3, 29, 1, 30))
        local = instant.astimezone(PRAGUE)
        self.assertEqual((local.hour, local.minute, local.date()), (3, 30, SPRING))

    def test_autumn_transition_with_minutes(self):
        # 02:30 nastane dvakrát; plán míří na první výskyt (ještě v CEST).
        plan = DailyPlan(2, 30, PRAGUE)
        instant = plan.scheduled_for(AUTUMN)
        self.assertEqual(instant, moment(2026, 10, 25, 0, 30))
        self.assertEqual(instant.astimezone(PRAGUE).utcoffset(), timedelta(hours=2))
        second = moment(2026, 10, 25, 1, 30)  # druhý výskyt 02:30, už v CET
        self.assertEqual(second.astimezone(PRAGUE).strftime("%H:%M"), "02:30")
        self.assertEqual(plan.due_at(second), instant)

    def test_next_after_with_minutes_is_always_in_the_future(self):
        plan = DailyPlan(2, 30, PRAGUE)
        start = moment(2026, 3, 1)
        for step in range(0, 250 * 24):
            now = start + timedelta(hours=step)
            following = plan.next_after(now)
            self.assertGreater(following, now)
            self.assertLessEqual(following - now, timedelta(hours=25))
            self.assertEqual(following.astimezone(PRAGUE).minute, 30)

    def test_utc_zone_is_allowed(self):
        plan = DailyPlan(0, 0, ZoneInfo("UTC"))
        self.assertEqual(plan.scheduled_for(date(2026, 7, 1)), moment(2026, 7, 1, 0))


class PlannerTest(unittest.TestCase):
    """Rozhodování o denním snímku (SnapshotPlanner)."""

    def setUp(self):
        self.plan = DailyPlan(2, 0, PRAGUE)
        self.planner = SnapshotPlanner(self.plan)
        self.scheduled = self.plan.scheduled_for(date(2026, 7, 1))  # 00:00 UTC

    def test_nothing_before_the_planned_time(self):
        self.assertFalse(self.planner.due(self.scheduled - timedelta(seconds=1), ready()))
        self.assertEqual(self.planner.next_run(self.scheduled - timedelta(hours=1), ready()), self.scheduled)

    def test_first_snapshot_is_due_at_the_planned_time(self):
        self.assertTrue(self.planner.due(self.scheduled, ready()))

    def test_missed_time_is_caught_up_the_same_day(self):
        # Proces se spustil (nebo se hodiny srovnaly) po plánovaném čase.
        late = self.scheduled + timedelta(hours=5)
        self.assertTrue(self.planner.due(late, ready()))
        self.assertEqual(self.planner.next_run(late, ready()), self.scheduled)

    def test_second_request_the_same_day_is_refused(self):
        now = self.scheduled + timedelta(minutes=1)
        self.assertTrue(self.planner.due(now, ready()))
        self.planner.note_requested(now)
        self.assertFalse(self.planner.due(now + timedelta(hours=10), ready()))
        self.assertEqual(self.planner.requests, 1)
        self.assertEqual(self.planner.next_run(now, ready()), self.plan.scheduled_for(date(2026, 7, 2)))

    def test_next_run_with_minutes(self):
        plan = DailyPlan(3, 15, PRAGUE)
        planner = SnapshotPlanner(plan)
        today = plan.scheduled_for(date(2026, 7, 1))
        tomorrow = plan.scheduled_for(date(2026, 7, 2))
        before = today - timedelta(minutes=20)
        self.assertEqual(planner.next_run(before, ready()), today)  # dnešní termín
        self.assertEqual(planner.next_run(today + timedelta(hours=2), ready()), today)  # zmeškaný
        done = ready(utc=int(today.timestamp()) + 30)
        self.assertEqual(planner.next_run(today + timedelta(hours=2), done), tomorrow)
        planner.note_requested(today)
        self.assertEqual(planner.next_run(today + timedelta(hours=3), ready()), tomorrow)

    def test_successful_snapshot_closes_the_plan_after_restart(self):
        # Nový proces (plánovač bez paměti) nesmí snímek zopakovat.
        done = ready(utc=int(self.scheduled.timestamp()) + 60, generation="20260701T000100Z")
        self.assertFalse(SnapshotPlanner(self.plan).due(self.scheduled + timedelta(hours=3), done))

    def test_older_snapshot_does_not_close_todays_plan(self):
        yesterday = ready(utc=int(self.scheduled.timestamp()) - 12 * HOUR)
        self.assertTrue(SnapshotPlanner(self.plan).due(self.scheduled, yesterday))

    def test_manual_snapshot_today_closes_the_plan(self):
        manual = ready(utc=int(self.scheduled.timestamp()) + 3 * HOUR)
        self.assertFalse(self.planner.due(self.scheduled + timedelta(hours=4), manual))

    def test_running_or_pending_snapshot_blocks_a_new_request(self):
        for snap in (ready(requested=True), ready(running=True, started=10.0)):
            with self.subTest(snap=snap):
                self.assertFalse(self.planner.due(self.scheduled, snap))

    def test_unscanned_state_blocks_the_request(self):
        # Dokud zapisovač nezjistil existující generace, neví se, co už je hotové.
        self.assertFalse(self.planner.due(self.scheduled, SnapshotStatus()))

    def test_failed_attempt_is_not_repeated_in_this_process(self):
        now = self.scheduled
        self.planner.note_requested(now)
        failed = ready(failures=1, last_error="io: disk")
        self.assertFalse(self.planner.due(now + timedelta(hours=2), failed))
        # Po restartu procesu se smí zkusit znovu (nejvýš jednou za běh).
        self.assertTrue(SnapshotPlanner(self.plan).due(now + timedelta(hours=2), failed))

    def test_missed_days_are_never_caught_up_in_bulk(self):
        # Proces byl týden vypnutý: vznikne jediný snímek pro dnešní plán.
        old = ready(utc=int(self.scheduled.timestamp()) - 7 * 24 * HOUR)
        now = self.scheduled + timedelta(hours=6)
        requests = 0
        snap = old
        for step in range(12):  # dvanáct měření po hodině
            when = now + timedelta(hours=step)
            if self.planner.due(when, snap):
                requests += 1
                self.planner.note_requested(when)
                snap = replace(snap, utc=int(when.timestamp()))
        self.assertEqual(requests, 1)

    def test_one_snapshot_per_day_across_both_transitions(self):
        plan = DailyPlan(2, 0, PRAGUE)
        planner = SnapshotPlanner(plan)
        snap = ready()
        start, days = moment(2026, 3, 1), []
        steps = 250 * 24
        for step in range(steps):
            now = start + timedelta(hours=step)
            if planner.due(now, snap):
                planner.note_requested(now)
                snap = replace(snap, utc=int(now.timestamp()))
                days.append(plan.local_date(now))
        end = start + timedelta(hours=steps - 1)
        expected = [day for day in (date(2026, 3, 1) + timedelta(days=step) for step in range(260))
                    if start <= plan.scheduled_for(day) <= end]
        self.assertEqual(days, expected)  # každý den právě jednou, nic dvakrát
        self.assertEqual(len(days), len(set(days)))

    def test_plan_is_keyed_by_the_planned_instant_not_the_completion_day(self):
        """Kopie přes půlnoc nezaměňuje den plánovaného úkolu s dnem dokončení.

        Rozhoduje výhradně porovnání okamžiků v UTC: plán dne je splněný
        snímkem, jehož čas je **od plánovaného okamžiku dál**. Místní datum
        dokončení (ani zveřejnění) do rozhodnutí nevstupuje, takže snímek
        zahájený ve 23:59:30 patří svému dni a plán dalšího dne nevynechá.
        """
        plan = DailyPlan(23, 0, PRAGUE)
        first = plan.scheduled_for(date(2026, 7, 1))  # D1 23:00 místně = 21:00 UTC
        planner = SnapshotPlanner(plan)
        start = first + timedelta(minutes=59, seconds=30)  # dohánění v 23:59:30 místně
        self.assertTrue(planner.due(start, ready()))
        planner.note_requested(start)
        done = ready(utc=int(start.timestamp()), generation="20260701T215930Z")
        # Ještě téhož místního dne je plán splněný.
        self.assertFalse(SnapshotPlanner(plan).due(start + timedelta(seconds=10), done))
        # Zveřejnění i restart procesu až po místní půlnoci: žádný druhý snímek.
        after_midnight = first + timedelta(hours=1, minutes=10)  # D2 00:10 místně
        self.assertEqual(plan.local_date(after_midnight), date(2026, 7, 2))
        fresh = SnapshotPlanner(plan)
        self.assertFalse(fresh.due(after_midnight, done))
        second = plan.scheduled_for(date(2026, 7, 2))
        self.assertEqual(fresh.next_run(after_midnight, done), second)
        # Další plánovaná záloha se nevynechá.
        self.assertTrue(fresh.due(second, done))

    def test_clock_step_backwards_does_not_duplicate_the_day(self):
        now = self.scheduled + timedelta(hours=2)
        self.planner.note_requested(now)
        snap = ready(utc=int(now.timestamp()))
        back = now - timedelta(hours=1)  # systémový čas se posunul zpět
        self.assertFalse(self.planner.due(back, snap))

    def test_clock_step_forward_runs_the_new_day_once(self):
        now = self.scheduled
        self.planner.note_requested(now)
        snap = ready(utc=int(now.timestamp()))
        jumped = now + timedelta(days=2)  # čas skočil dopředu o dva dny
        self.assertTrue(self.planner.due(jumped, snap))
        self.planner.note_requested(jumped)
        self.assertFalse(self.planner.due(jumped + timedelta(hours=1), snap))


class SnapshotDiagnosticsTest(unittest.TestCase):
    """Kontrola history.snapshot ze snímku stavu (docs/HISTORY.md 9.3)."""

    def state(self, snap=None, *, enabled=True, now=None, time_base="synced", confirmed_seq=0, **options):
        worker = None if snap is False else status(snap, time_base=time_base, confirmed_seq=confirmed_seq)
        return history_link.snapshot_state(worker, enabled=enabled, now_utc=now, **options)

    def aged(self, seconds, **options):
        """Stav s posledním snímkem daného stáří."""
        return ready(utc=BASE - seconds, generation="20270101T000000Z", **options), BASE

    def test_disabled_snapshots_are_not_a_fault(self):
        state, text = self.state(enabled=False)
        self.assertIs(state, State.NA)
        self.assertEqual(text, "vypnuto")

    def test_unusable_directories_are_an_error(self):
        state, text = self.state(enabled=True, prepare_error="adresář /var/snapshot neexistuje")
        self.assertIs(state, State.ERROR)
        self.assertIn("neexistuje", text)

    def test_missing_worker_status_is_unknown(self):
        state, text = self.state(snap=False)
        self.assertIs(state, State.UNKNOWN)
        self.assertIn("není k dispozici", text)

    def test_before_the_scan_the_age_is_unknown(self):
        state, text = self.state(SnapshotStatus(), now=BASE)
        self.assertIs(state, State.UNKNOWN)
        self.assertIn("nezjistil", text)

    def test_no_valid_generation_is_an_error(self):
        state, text = self.state(ready(), now=BASE)
        self.assertIs(state, State.ERROR)
        self.assertIn("žádná platná generace", text)

    def test_unreadable_directory_is_an_error(self):
        state, text = self.state(ready(scan_error="path: Permission denied"), now=BASE)
        self.assertIs(state, State.ERROR)
        self.assertIn("nelze přečíst", text)

    def test_fresh_snapshot_is_info(self):
        snap, now = self.aged(2 * HOUR)
        state, text = self.state(snap, now=now)
        self.assertIs(state, State.INFO)
        self.assertIn("2.0 h", text)
        self.assertIn("20270101T000000Z", text)

    def test_boundaries_26_and_50_hours(self):
        cases = {26 * HOUR: State.INFO, 26 * HOUR + 1: State.WARNING,
                 50 * HOUR: State.WARNING, 50 * HOUR + 1: State.ERROR}
        for age, expected in cases.items():
            with self.subTest(age=age):
                snap, now = self.aged(age)
                state, _ = self.state(snap, now=now)
                self.assertIs(state, expected)

    def test_failed_attempt_with_usable_snapshot_is_warning(self):
        snap, now = self.aged(3 * HOUR, failures=1, last_error="io: disk I/O error")
        state, text = self.state(snap, now=now)
        self.assertIs(state, State.WARNING)
        self.assertIn("poslední pokus o snímek selhal", text)
        self.assertIn("3.0 h", text)

    def test_failed_attempt_with_stale_snapshot_is_error(self):
        snap, now = self.aged(60 * HOUR, failures=2, last_error="verify: poškozeno")
        state, text = self.state(snap, now=now)
        self.assertIs(state, State.ERROR)
        self.assertIn("60.0 h", text)

    def test_unknown_time_never_hides_a_confirmed_failure(self):
        snap, now = self.aged(3 * HOUR, failures=1, last_error="io: disk I/O error")
        state, text = self.state(snap, now=now, time_base="unsynced")
        self.assertIs(state, State.WARNING)  # ne UNKNOWN: závada je doložená
        self.assertIn("stáří snímku nelze určit", text)

    def test_unsynced_clock_without_failure_is_unknown(self):
        snap, now = self.aged(3 * HOUR)
        state, text = self.state(snap, now=now, time_base="unsynced")
        self.assertIs(state, State.UNKNOWN)
        self.assertIn("čas není doložený", text)

    def test_snapshot_in_the_future_is_reported(self):
        snap = ready(utc=BASE + 2 * HOUR, generation="20270101T000000Z")
        state, text = self.state(snap, now=BASE)
        self.assertIs(state, State.WARNING)
        self.assertIn("budoucnosti", text)

    def test_damaged_generation_stays_visible_even_with_a_fresh_snapshot(self):
        snap, now = self.aged(HOUR, invalid=1, invalid_name="20270102T020000Z")
        state, text = self.state(snap, now=now)
        self.assertIs(state, State.WARNING)  # aktuální snímek, ale poškozená generace v adresáři
        self.assertIn("neověřitelné generace: 1", text)
        self.assertIn("20270102T020000Z", text)

    def test_unconfirmed_event_is_only_a_note(self):
        # Zveřejněná generace nezávisí na zápisu události: stav zůstává INFO.
        snap, now = self.aged(HOUR, event_seq=42)
        state, text = self.state(snap, now=now, confirmed_seq=41)
        self.assertIs(state, State.INFO)
        self.assertIn("není potvrzená", text)
        state, text = self.state(snap, now=now, confirmed_seq=42)
        self.assertIs(state, State.INFO)
        self.assertNotIn("není potvrzená", text)

    def test_failed_planning_is_a_warning(self):
        snap, now = self.aged(HOUR)
        state, text = self.state(snap, now=now, plan_failures=2)
        self.assertIs(state, State.WARNING)
        self.assertIn("plánování snímku selhalo (2×)", text)

    def test_next_run_is_shown_when_known(self):
        snap, now = self.aged(HOUR)
        state, text = self.state(snap, now=now, next_run=moment(2027, 1, 2, 1))
        self.assertIs(state, State.INFO)
        self.assertIn("příští plán 2027-01-02 01:00 UTC", text)

    def test_result_keeps_the_check_id_and_label(self):
        snap, now = self.aged(HOUR)
        observed = datetime.fromtimestamp(now, UTC)
        result = history_link.snapshot_result(status(snap), observed, 100.0, enabled=True, now_utc=now)
        self.assertEqual(result.id, "history.snapshot")
        self.assertTrue(result.summary.startswith("HIST SNAPSHOT: "))
        self.assertIs(result.state, State.INFO)
        self.assertFalse(result.simulated)
        self.assertEqual(result.details, {"snapshots": "on"})


class FakeWorker:
    """Náhradní zapisovač: vrací zadaný stav a počítá požadavky na snímek."""

    def __init__(self, snap=None):
        self.snap = snap if snap is not None else ready()
        self.time_base = "synced"
        self.confirmed_seq = 0
        self.requests = 0
        self.accept = True
        self.status_error = None
        self.started = None

    def start(self, time_synced=None):
        self.started = time_synced

    def status(self):
        if self.status_error is not None:
            raise self.status_error
        return status(self.snap, time_base=self.time_base, confirmed_seq=self.confirmed_seq)

    def request_snapshot(self):
        self.requests += 1
        return self.accept

    def stop(self, timeout=None):
        raise AssertionError("tyto testy zapisovač nezastavují")


class FakeSync:
    """Zdroj stavu hodin: vrací, co test nastaví (True, False, None)."""

    def __init__(self, state=True):
        self.state = state
        self.source = "test"

    def synced(self):
        return self.state


class LinkPlanTest(unittest.TestCase):
    """Napojení plánu na měřicí smyčku (HistoryLink.plan_snapshot)."""

    def setUp(self):
        self.plan = DailyPlan(2, 0, PRAGUE)
        self.scheduled = self.plan.scheduled_for(date(2026, 7, 1))
        self.lines = []

    def make(self, worker=None, *, enabled=True, plan=True, synced=True, error=None):
        worker = worker if worker is not None else FakeWorker()
        link = history_link.HistoryLink(
            worker, FakeSync(synced), output=self.lines.append, validity_seconds=30,
            snapshot_enabled=enabled, plan=self.plan if plan else None, snapshot_error=error)
        link.start()  # stav hodin se zjistí stejně jako v provozu
        return link, worker

    def test_request_happens_once_at_the_planned_time(self):
        link, worker = self.make()
        self.assertTrue(link.plan_snapshot(100.0, self.scheduled))
        self.assertEqual(worker.requests, 1)
        self.assertFalse(link.plan_snapshot(110.0, self.scheduled + timedelta(hours=1)))
        self.assertEqual(worker.requests, 1)
        self.assertTrue(any("vyžádán denní snímek" in line for line in self.lines))

    def test_nothing_before_the_planned_time(self):
        link, worker = self.make()
        self.assertFalse(link.plan_snapshot(100.0, self.scheduled - timedelta(minutes=1)))
        self.assertEqual(worker.requests, 0)

    def test_without_plan_nothing_is_requested(self):
        link, worker = self.make(plan=False)
        self.assertFalse(link.plan_snapshot(100.0, self.scheduled))
        self.assertEqual(worker.requests, 0)
        self.assertIsNone(link.planner)

    def test_unsynced_or_unknown_clock_never_requests(self):
        for state in (False, None):
            with self.subTest(state=state):
                link, worker = self.make(synced=state)
                self.assertFalse(link.plan_snapshot(100.0, self.scheduled))
                self.assertEqual(worker.requests, 0)

    def test_stopped_link_starts_no_snapshot(self):
        link, worker = self.make()
        link._stopped = True  # po stop(): nový snímek se nezačíná
        self.assertFalse(link.plan_snapshot(100.0, self.scheduled))
        self.assertEqual(worker.requests, 0)

    def test_refused_request_keeps_the_plan_open(self):
        # request_snapshot() odmítne při ukončování; plán se nepovažuje za vyřízený.
        link, worker = self.make()
        worker.accept = False
        self.assertFalse(link.plan_snapshot(100.0, self.scheduled))
        self.assertIsNone(link.planner.attempted)
        worker.accept = True
        self.assertTrue(link.plan_snapshot(110.0, self.scheduled + timedelta(minutes=1)))

    def test_planning_failure_is_counted_and_measuring_continues(self):
        # S aktuálním snímkem rozhoduje o stavu právě selhané plánování.
        worker = FakeWorker(ready(utc=int(self.scheduled.timestamp()) - HOUR,
                                  generation="20260630T230000Z"))
        link, worker = self.make(worker)
        worker.status_error = RuntimeError("stav nelze přečíst")
        self.assertFalse(link.plan_snapshot(100.0, self.scheduled))  # bez výjimky do smyčky
        self.assertFalse(link.plan_snapshot(110.0, self.scheduled))
        self.assertEqual(link.plan_failures, 2)
        self.assertEqual(sum("plánování snímku selhalo" in line for line in self.lines), 1)
        worker.status_error = None
        result = link.snapshot_check(120.0, self.scheduled)
        self.assertIs(result.state, State.WARNING)
        self.assertIn("plánování snímku selhalo", result.summary)

    def test_check_reports_the_next_run(self):
        worker = FakeWorker(ready(utc=int(self.scheduled.timestamp()), generation="20260701T000000Z"))
        link, _ = self.make(worker)
        result = link.snapshot_check(100.0, self.scheduled + timedelta(hours=3))
        self.assertIs(result.state, State.INFO)
        self.assertIn("příští plán 2026-07-02 00:00 UTC", result.summary)

    def test_disabled_snapshots_report_na_without_reading_the_worker(self):
        link, worker = self.make(enabled=False, plan=False)
        result = link.snapshot_check(100.0, self.scheduled)
        self.assertIs(result.state, State.NA)
        self.assertIn("vypnuto", result.summary)
        self.assertEqual(result.details, {"snapshots": "off"})

    def test_unusable_directories_are_reported_from_the_start(self):
        link, _ = self.make(plan=False, error="adresář /var/snapshot neexistuje")
        result = link.snapshot_check(100.0, self.scheduled)
        self.assertIs(result.state, State.ERROR)
        self.assertIn("neexistuje", result.summary)

    def test_check_failure_is_unknown_not_a_silent_ok(self):
        link, worker = self.make()
        worker.status_error = RuntimeError("stav nelze přečíst")
        result = link.snapshot_check(100.0, self.scheduled)
        self.assertIs(result.state, State.UNKNOWN)
        self.assertIn("nelze přečíst", result.summary)

    def test_stuck_snapshot_blocks_requests_while_the_age_grows(self):
        # Zaseknuté I/O: snímek běží a nedokončí se. Nový se nezačíná a stáří
        # posledního úspěšného snímku roste, dokud nedosáhne WARNING a ERROR.
        started = int(self.scheduled.timestamp())
        worker = FakeWorker(ready(utc=started - 3 * HOUR, generation="20260630T210000Z",
                                  running=True, started=50.0))
        link, _ = self.make(worker)
        self.assertFalse(link.plan_snapshot(100.0, self.scheduled))
        self.assertEqual(worker.requests, 0)
        states = []
        for hours in (3, 30, 60):
            when = datetime.fromtimestamp(started - 3 * HOUR + hours * HOUR, UTC)
            states.append(link.snapshot_check(100.0, when).state)
        self.assertEqual(states, [State.INFO, State.WARNING, State.ERROR])
