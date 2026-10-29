"""Testy kontroly výsledku zálohovací úlohy přes systemd (SERVICES-3) nad syntetickým výstupem.

Žádný test nespouští systemctl ani žádnou službu: subprocess.run nahrazuje
FakeSystemctl s připraveným výstupem a čas je simulovaný (monotónní sekundy
jako čísla), nic se nečeká. Tvar výstupu (pořadí vlastností, čísla v µs,
ExecMainCode jako číslo, blok timeru jen s obecnými vlastnostmi) odpovídá
skutečnému `systemctl show` ze systemd 255 zachycenému na vývojovém notebooku;
názvy jednotek jsou nahrazené obecnými example-backup.*.
"""

import ast
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from nexusstat import backup_monitor, rgb
from nexusstat.alarms import AlarmEvaluator
from nexusstat.backup_monitor import (BACKUP, FAILED, NO_JOB, NO_RUN, NO_TIMER, OVERDUE, RUNNING, SKIPPED,
                                      SUCCEEDED, SYSTEMCTL, BackupMonitor)
from nexusstat.config import (AlarmsConfig, BackupConfig, CollectorConfig, Config, ConfigError, ServicesConfig,
                              load_config, services_config_from_dict)
from nexusstat.core import CheckResult, State, active_problems, effective
from nexusstat.oled import ALERT_SYMBOL, UNKNOWN_SYMBOL, home_symbol
from nexusstat.oled_text import service_rows, status_texts
from nexusstat.preview import LIVE, PreviewSession, SessionClock
from nexusstat.run import COLLECTOR_FAILURE, Collectors, Scheduled, live_monitor, run_loop
from nexusstat.services import BACKUP_ID, complete_service_results
from nexusstat.system_monitor import LABELS as SYSTEM_LABELS

OBSERVED = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
UNIT = "example-backup.service"
TIMER = "example-backup.timer"
TIMEOUT = 2
VALIDITY = 30
HOUR = 3600
START = 9_000.0  # monotónní s: začátek posledního běhu
END = 10_000.0  # monotónní s: jeho konec
EXAMPLE_CONFIG = Path(__file__).resolve().parent.parent / "config" / "config.example.toml"

# Skutečný výstup `systemctl show` (systemd 255) pro úspěšnou oneshot úlohu s timerem,
# jen s přejmenovanými jednotkami: Result první, Id a Names až uprostřed bloku.
REAL_OUTPUT = (b"Result=success\nExecMainStartTimestampMonotonic=15451570\nExecMainExitTimestampMonotonic=15598222\n"
               b"ExecMainCode=1\nExecMainStatus=0\nId=example-backup.service\nNames=example-backup.service\n"
               b"LoadState=loaded\nActiveState=inactive\nSubState=dead\n\n"
               b"Result=success\nId=example-backup.timer\nNames=example-backup.timer\nLoadState=loaded\n"
               b"ActiveState=active\nSubState=waiting\n")


def us(seconds):
    return round(seconds * 1_000_000)


def service(result="success", start=START, end=END, code=1, status=0, active="inactive", sub="dead",
            load="loaded", unit=UNIT, names=None):
    """Blok služby ve stejném pořadí a tvaru jako skutečný systemd (časy v µs, 0 = žádný údaj)."""
    return (f"Result={result}\nExecMainStartTimestampMonotonic={us(start)}\n"
            f"ExecMainExitTimestampMonotonic={us(end)}\nExecMainCode={code}\nExecMainStatus={status}\n"
            f"Id={unit}\nNames={names or unit}\nLoadState={load}\nActiveState={active}\nSubState={sub}")


# Stavy, které systemd skutečně ukázal na notebooku (hodnoty přenesené, jen jména obecná).
NEVER_RUN = service(start=0, end=0, code=0, status=0)  # i maskovaná a neexistující mají Result=success
SUCCESS = service()
FAILURE = service(result="exit-code", code=1, status=1, active="failed", sub="failed")
RUNNING_NOW = service(start=END + HOUR, end=0, code=0, status=0, active="activating", sub="start")


def timer(active="active", sub="waiting", load="loaded", unit=TIMER):
    return f"Result=success\nId={unit}\nNames={unit}\nLoadState={load}\nActiveState={active}\nSubState={sub}"


def output(*blocks):
    return ("\n\n".join(blocks) + "\n").encode("ascii")


class FakeClock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class FakeSystemctl:
    """Náhrada subprocess.run: vrací připravený výstup, nic nespouští."""

    def __init__(self, *blocks, returncode=0, error=None, stdout=None):
        self.stdout = stdout if stdout is not None else output(*blocks)
        self.returncode = returncode
        self.error = error
        self.calls = []

    def show(self, *blocks):
        self.stdout = output(*blocks)

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        if self.error is not None:
            raise self.error
        return subprocess.CompletedProcess(args, self.returncode, self.stdout, b"")


class FakeSystem:
    """Skutečně vypadající systémová měření bez čtení /proc (pro skládání kolektorů)."""

    simulated = False
    labels = SYSTEM_LABELS

    def __init__(self, validity_seconds=VALIDITY):
        self.validity_seconds = validity_seconds

    def collect(self, now, observed_utc):
        return [CheckResult(check_id, State.INFO, label, observed_utc, now + self.validity_seconds, simulated=False)
                for check_id, label in SYSTEM_LABELS.items()]


def monitor_for(systemctl, at=END + 3 * HOUR, timer_unit=None):
    clock = FakeClock(at)
    return BackupMonitor(VALIDITY, UNIT, timer_unit, TIMEOUT, run=systemctl, clock=clock), clock


def check(systemctl, at=END + 3 * HOUR, timer_unit=None):
    monitor, clock = monitor_for(systemctl, at, timer_unit)
    [result] = monitor.collect(clock.now, OBSERVED)
    return result


def outcome(result):
    return result.state, dict(result.details).get(BACKUP)


class CommandTest(unittest.TestCase):
    """Jediný read-only dotaz systemctl s pevnými argumenty, bez shellu a bez spouštění úlohy."""

    def test_single_read_only_query_with_fixed_arguments(self):
        systemctl = FakeSystemctl(SUCCESS, timer())
        check(systemctl, timer_unit=TIMER)
        [(args, kwargs)] = systemctl.calls
        self.assertEqual(args, [SYSTEMCTL, "show", "--no-pager", "--no-ask-password",
                                "--property=Id,Names,LoadState,ActiveState,SubState,Result,ExecMainCode,"
                                "ExecMainStatus,ExecMainStartTimestampMonotonic,ExecMainExitTimestampMonotonic,"
                                "NextElapseUSecRealtime",
                                "--", UNIT, TIMER])
        self.assertEqual(SYSTEMCTL, "/usr/bin/systemctl")  # nehledá se v PATH
        # TZ=UTC jen pro jednoznačný tvar plánu timeru (SCHED-1); prostředí je jinak prázdné.
        self.assertEqual(kwargs, {"stdin": subprocess.DEVNULL, "capture_output": True, "timeout": TIMEOUT,
                                  "env": {"LC_ALL": "C", "TZ": "UTC"}, "check": False})
        self.assertNotIn("shell", kwargs)
        # Žádný příkaz, který by jednotku spustil, zastavil nebo změnil.
        for word in ("start", "stop", "restart", "reset-failed", "enable", "disable", "kill", "sudo"):
            self.assertNotIn(word, args)

    def test_without_timer_only_the_service_is_queried(self):
        systemctl = FakeSystemctl(SUCCESS)
        check(systemctl)
        self.assertEqual(systemctl.calls[0][0][-2:], ["--", UNIT])

    def test_invalid_constructor_arguments(self):
        for arguments in ((0, UNIT, None, TIMEOUT), (VALIDITY, "example-backup", None, TIMEOUT),
                          (VALIDITY, UNIT, "example-backup.service", TIMEOUT), (VALIDITY, UNIT, None, 6),
                          (VALIDITY, "-x.service", None, TIMEOUT)):
            with self.subTest(arguments=arguments):
                with self.assertRaises(ValueError):
                    BackupMonitor(*arguments, run=FakeSystemctl(SUCCESS))

    def test_real_runner_is_real_data_and_fake_is_simulated(self):
        self.assertFalse(BackupMonitor(VALIDITY, UNIT, None, TIMEOUT).simulated)  # jen vytvoření, nic se nespouští
        self.assertTrue(check(FakeSystemctl(SUCCESS)).simulated)

    def test_no_file_access_and_no_extra_modules(self):
        # Kolektor nečte žádný soubor (repozitář, heslo, journal ani mount), jen výstup systemctl.
        forbidden = AssertionError("přístup k souborovému systému při kontrole záloh")
        with mock.patch("builtins.open", side_effect=forbidden), mock.patch("os.open", side_effect=forbidden), \
                mock.patch("os.stat", side_effect=forbidden), mock.patch("os.scandir", side_effect=forbidden):
            self.assertIs(check(FakeSystemctl(SUCCESS)).state, State.OK)
        self.assertFalse({"os", "pathlib", "glob", "shutil", "socket"} & set(vars(backup_monitor)))
        # Žádný řetězec v kódu (mimo docstringy) nevolá journal, Restic ani sudo a nic se nespouští shellem.
        tree = ast.parse(Path(backup_monitor.__file__).read_text(encoding="utf-8"))
        docstrings = {id(node.body[0].value) for node in ast.walk(tree)
                      if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)) and ast.get_docstring(node)}
        literals = [node.value for node in ast.walk(tree)
                    if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings]
        for word in ("journalctl", "restic", "sudo", "/bin/sh", "bash"):
            self.assertFalse([text for text in literals if word in text], word)
        self.assertFalse([node for node in ast.walk(tree) if isinstance(node, ast.keyword) and node.arg == "shell"])


class RealFormatTest(unittest.TestCase):
    """Výstupy ve skutečném tvaru systemd 255 (ověřeno na notebooku, jména obecná)."""

    def test_real_successful_job_with_active_timer(self):
        monitor, clock = monitor_for(FakeSystemctl(stdout=REAL_OUTPUT), at=15.598222 + 3 * HOUR, timer_unit=TIMER)
        [result] = monitor.collect(clock.now, OBSERVED)
        self.assertEqual(outcome(result), (State.OK, SUCCEEDED))
        self.assertEqual(result.summary, "Záloha: úloha naposledy skončila úspěšně před 3.0 h; plán zálohy je aktivní")
        self.assertEqual(monitor.last_success, 15.598222)  # µs → s

    def test_real_states_seen_on_the_notebook(self):
        cases = (
            ("selhaná oneshot", FAILURE, (State.ERROR, FAILED)),
            ("maskovaná", service(start=0, end=0, code=0, load="masked"), (State.ERROR, NO_JOB)),
            ("neexistující", service(start=0, end=0, code=0, load="not-found"), (State.ERROR, NO_JOB)),
            ("dlouho běžící služba", service(start=15.446524, end=0, code=0, active="active", sub="running"),
             (State.UNKNOWN, RUNNING)),
            ("nikdy nespuštěná", NEVER_RUN, (State.UNKNOWN, NO_RUN)),
        )
        for name, block, expected in cases:
            with self.subTest(case=name):
                self.assertEqual(outcome(check(FakeSystemctl(block))), expected)

    def test_alias_is_recognised_by_names(self):
        # Alias: Id je hlavní název, dotazované jméno je v Names (ověřeno na notebooku).
        block = service(unit="real-backup.service", names=f"real-backup.service {UNIT}")
        self.assertIs(check(FakeSystemctl(block)).state, State.OK)


class StateTest(unittest.TestCase):
    """Stavy podle zadání SERVICES-3; OK jen z doloženého dokončeného úspěšného běhu."""

    def test_never_run_unit_with_result_success_is_unknown(self):
        result = check(FakeSystemctl(NEVER_RUN))
        self.assertEqual(outcome(result), (State.UNKNOWN, NO_RUN))
        self.assertIn("neuvádí žádný dokončený běh", result.summary)

    def test_completed_successful_run_is_ok(self):
        result = check(FakeSystemctl(SUCCESS))
        self.assertEqual(outcome(result), (State.OK, SUCCEEDED))
        self.assertEqual(result.summary, "Záloha: úloha naposledy skončila úspěšně před 3.0 h")
        # Bez timeru není plán: výsledek platí do náhradního dotazu za den a ještě VALIDITY (SCHED-1).
        self.assertEqual((result.id, result.observed_utc, result.expires_monotonic),
                         (BACKUP_ID, OBSERVED, END + 3 * HOUR + 24 * HOUR + VALIDITY))

    def test_failed_last_attempt_without_known_success_is_error(self):
        for block in (FAILURE, service(result="signal", code=2, status=15, active="failed", sub="failed"),
                      service(result="timeout", code=2, status=9, active="failed", sub="failed"),
                      service(result="resources", start=0, end=0, code=0, active="failed", sub="failed")):
            with self.subTest(block=block.split("\n")[0]):
                result = check(FakeSystemctl(block))
                self.assertEqual(outcome(result), (State.ERROR, FAILED))
                self.assertIn("úspěch od startu systému není doložený", result.summary)

    def test_completed_run_with_nonzero_status_is_failure_even_with_result_success(self):
        # Např. po `systemctl reset-failed` (Result=success, ActiveState=inactive, ExecMainStatus=1)
        # nebo se SuccessExitStatus=3 (neúplný snapshot Resticu): úspěch není doložený.
        for code, status in ((1, 1), (1, 3), (2, 15), (3, 6)):
            with self.subTest(code=code, status=status):
                self.assertEqual(outcome(check(FakeSystemctl(service(code=code, status=status)))),
                                 (State.ERROR, FAILED))

    def test_running_job_uses_last_documented_success(self):
        systemctl = FakeSystemctl(RUNNING_NOW)
        self.assertEqual(outcome(check(systemctl)), (State.UNKNOWN, RUNNING))  # bez paměti
        monitor, clock = monitor_for(FakeSystemctl(SUCCESS), at=END + HOUR)
        monitor.collect(clock.now, OBSERVED)
        monitor.run.show(RUNNING_NOW)
        clock.now = END + 2 * HOUR
        [result] = monitor.collect(clock.now, OBSERVED)
        self.assertEqual(outcome(result), (State.OK, RUNNING))
        self.assertEqual(result.summary, "Záloha: úloha právě běží; poslední doložený úspěch před 2.0 h")
        # Ve fázi ExecStartPre ukazuje systemd ještě časy předchozího běhu: paměť platí dál.
        monitor.run.show(service(active="activating", sub="start-pre"))
        self.assertEqual(outcome(monitor.collect(clock.now, OBSERVED)[0]), (State.OK, RUNNING))
        # Běží-li úloha dlouho, stáří posledního úspěchu dál roste.
        monitor.run.show(RUNNING_NOW)
        clock.now = END + 27 * HOUR
        self.assertEqual(outcome(monitor.collect(clock.now, OBSERVED)[0]), (State.WARNING, OVERDUE))

    def test_success_followed_by_failure(self):
        for success_age, expected in ((3, State.WARNING), (49, State.WARNING), (51, State.ERROR)):
            with self.subTest(hours=success_age):
                monitor, clock = monitor_for(FakeSystemctl(SUCCESS), at=END + HOUR)
                self.assertIs(monitor.collect(clock.now, OBSERVED)[0].state, State.OK)
                failed_at = END + (success_age - 1) * HOUR
                monitor.run.show(service(result="exit-code", start=failed_at - 60, end=failed_at, status=1,
                                         active="failed", sub="failed"))
                clock.now = END + success_age * HOUR
                result = monitor.collect(clock.now, OBSERVED)[0]
                self.assertEqual(outcome(result), (expected, FAILED))
                self.assertIn(f"poslední doložený úspěch před {success_age:.1f} h", result.summary)

    def test_loss_of_systemd_data_when_unit_is_unloaded(self):
        # Systemd uvolní neaktivní jednotku z paměti: časy se vynulují a Result je zase success.
        monitor, clock = monitor_for(FakeSystemctl(SUCCESS), at=END + HOUR)
        self.assertIs(monitor.collect(clock.now, OBSERVED)[0].state, State.OK)
        monitor.run.show(NEVER_RUN)
        result = monitor.collect(clock.now, OBSERVED)[0]
        self.assertEqual(outcome(result), (State.UNKNOWN, NO_RUN))
        self.assertIn("dříve doložený úspěch se proto nepoužije", result.summary)
        self.assertIsNone(monitor.last_success)
        # Zapomenutý úspěch už neudělá z nového neúspěchu WARNING ani z běhu OK.
        monitor.run.show(FAILURE)
        self.assertEqual(outcome(monitor.collect(clock.now, OBSERVED)[0]), (State.ERROR, FAILED))
        monitor.run.show(RUNNING_NOW)
        self.assertEqual(outcome(monitor.collect(clock.now, OBSERVED)[0]), (State.UNKNOWN, RUNNING))

    def test_nexusstat_restart_uses_only_currently_verifiable_data(self):
        # Nový proces nemá paměť: rozhoduje jen to, co systemd právě dokládá.
        self.assertEqual(outcome(check(FakeSystemctl(SUCCESS))), (State.OK, SUCCEEDED))
        self.assertEqual(outcome(check(FakeSystemctl(FAILURE))), (State.ERROR, FAILED))
        self.assertEqual(outcome(check(FakeSystemctl(RUNNING_NOW))), (State.UNKNOWN, RUNNING))
        self.assertEqual(outcome(check(FakeSystemctl(NEVER_RUN))), (State.UNKNOWN, NO_RUN))

    def test_system_restart_with_zeroed_or_foreign_times(self):
        # Po startu systému jsou monotónní údaje vynulované; paměť předchozího procesu neexistuje.
        self.assertEqual(outcome(check(FakeSystemctl(NEVER_RUN), at=120.0)), (State.UNKNOWN, NO_RUN))
        # Čas novější než hodiny tohoto procesu (jiný boot nebo jiné hodiny) se nepoužije.
        for block in (service(start=500.0, end=600.0), service(start=50.0, end=600.0),
                      service(start=500.0, end=0, active="activating", sub="start")):
            with self.subTest(block=block.split("\n")[1:3]):
                result = check(FakeSystemctl(block), at=120.0)
                self.assertEqual(outcome(result), (State.UNKNOWN, None))
                self.assertIn("časy systemd nejsou věrohodné", result.summary)

    def test_age_boundaries_are_inclusive(self):
        cases = ((26 * HOUR - 1, State.OK), (26 * HOUR, State.OK), (26 * HOUR + 0.001, State.WARNING),
                 (50 * HOUR, State.WARNING), (50 * HOUR + 0.001, State.ERROR), (400 * HOUR, State.ERROR))
        for age, expected in cases:
            with self.subTest(age=age):
                result = check(FakeSystemctl(SUCCESS), at=END + age)
                self.assertIs(result.state, expected)
                self.assertEqual(dict(result.details)[BACKUP], SUCCEEDED if expected is State.OK else OVERDUE)
        self.assertIn("(nad 26 h)", check(FakeSystemctl(SUCCESS), at=END + 30 * HOUR).summary)
        self.assertIn("(nad 50 h)", check(FakeSystemctl(SUCCESS), at=END + 60 * HOUR).summary)

    def test_missing_masked_or_broken_unit_is_error(self):
        for load, text in (("not-found", "neexistuje"), ("masked", "je maskovaná"),
                           ("bad-setting", "má chybné nastavení"), ("error", "nejde načíst")):
            with self.subTest(load=load):
                result = check(FakeSystemctl(service(start=0, end=0, code=0, load=load)))
                self.assertEqual(outcome(result), (State.ERROR, NO_JOB))
                self.assertIn(f"zálohovací úloha {text} ({load})", result.summary)

    def test_inactive_timer_is_never_ok(self):
        cases = (
            (SUCCESS, timer(active="inactive", sub="dead"), (State.WARNING, NO_TIMER)),
            (SUCCESS, timer(active="failed", sub="failed"), (State.WARNING, NO_TIMER)),
            (SUCCESS, timer(load="not-found", active="inactive", sub="dead"), (State.ERROR, NO_TIMER)),
            (SUCCESS, timer(load="masked", active="inactive", sub="dead"), (State.ERROR, NO_TIMER)),
            # Aktivní timer stav služby nikdy nezlepší.
            (NEVER_RUN, timer(), (State.UNKNOWN, NO_RUN)),
            (FAILURE, timer(), (State.ERROR, FAILED)),
            # Při závažnější službě rozhoduje služba, timer se jen připíše.
            (FAILURE, timer(active="inactive", sub="dead"), (State.ERROR, FAILED)),
            (NEVER_RUN, timer(active="inactive", sub="dead"), (State.WARNING, NO_TIMER)),
        )
        for block, timer_block, expected in cases:
            with self.subTest(service=block.split("\n")[0], timer=timer_block.split("\n")[3:5]):
                result = check(FakeSystemctl(block, timer_block), timer_unit=TIMER)
                self.assertEqual(outcome(result), expected)
        result = check(FakeSystemctl(SUCCESS, timer(active="inactive", sub="dead")), timer_unit=TIMER)
        self.assertEqual(result.summary, "Záloha: timer zálohy není aktivní (inactive); "
                                         "úloha naposledy skončila úspěšně před 3.0 h")

    def test_invalid_or_incomplete_output_is_unknown(self):
        without_code = "\n".join(line for line in SUCCESS.split("\n") if not line.startswith("ExecMainCode="))
        cases = (
            ("chybí vlastnost", output(without_code)),
            ("nečíselná hodnota", output(SUCCESS.replace("ExecMainStatus=0", "ExecMainStatus=x"))),
            ("záporná hodnota", output(SUCCESS.replace("ExecMainCode=1", "ExecMainCode=-1"))),
            ("mezera v čísle", output(SUCCESS.replace("ExecMainStatus=0", "ExecMainStatus=0 "))),
            ("zdvojená vlastnost", output(SUCCESS + "\nResult=success")),
            ("řádek bez =", output(SUCCESS + "\nSmeti")),
            ("prázdný výstup", b""),
            ("chybí blok timeru", output(SUCCESS)),
            ("blok navíc", output(SUCCESS, timer(), timer())),
            ("cizí jednotka", output(service(unit="other.service"), timer())),
            ("neznámý LoadState", output(service(load="stub"), timer())),
            ("neznámý ActiveState", output(service(active="maintenance"), timer())),
            ("neznámý stav timeru", output(SUCCESS, timer(active="activating"))),
            ("neúplný běh", output(service(end=0), timer())),
            ("konec před začátkem", output(service(start=END, end=START), timer())),
            ("ne ASCII", output(SUCCESS, timer()).replace(b"dead", "mrtvá".encode("utf-8"))),
            ("příliš dlouhý", output(SUCCESS, timer()) + b"X" * 9000),
        )
        for name, stdout in cases:
            with self.subTest(case=name):
                result = check(FakeSystemctl(stdout=stdout), timer_unit=TIMER)
                self.assertEqual(outcome(result), (State.UNKNOWN, None))
                self.assertIn("nelze věrohodně přečíst", result.summary)

    def test_timeout_error_and_nonzero_exit_are_unknown(self):
        cases = (
            (FakeSystemctl(error=subprocess.TimeoutExpired(SYSTEMCTL, TIMEOUT)), "systemd neodpověděl do 2 s"),
            (FakeSystemctl(SUCCESS, returncode=1), "systemctl skončil kódem 1"),
            (FakeSystemctl(error=FileNotFoundError(2, "No such file")), "(FileNotFoundError)"),
            (FakeSystemctl(error=PermissionError(13, "Permission denied")), "(PermissionError)"),
            (FakeSystemctl(error=OSError(24, "Too many open files")), "(OSError)"),
        )
        for systemctl, text in cases:
            with self.subTest(text=text):
                result = check(systemctl)
                self.assertEqual(outcome(result), (State.UNKNOWN, None))
                self.assertIn(text, result.summary)

    def test_unexpected_exception_stays_with_backup(self):
        collectors = Collectors(FakeSystem(), monitor_for(FakeSystemctl(error=RuntimeError("chyba")))[0])
        results = {result.id: result for result in collectors.collect(END, OBSERVED)}
        self.assertIs(results[BACKUP_ID].state, State.UNKNOWN)
        self.assertEqual(results[BACKUP_ID].details[COLLECTOR_FAILURE], "RuntimeError")
        self.assertEqual({results[check_id].state for check_id in SYSTEM_LABELS}, {State.INFO})

    def test_no_false_ok(self):
        # OK smí vzniknout jen z načtené jednotky, dokončeného běhu (konec ≥ začátek > 0),
        # Result=success, ExecMainCode=1, ExecMainStatus=0, neaktivní nebo exited a stáří ≤ 26 h.
        seen_ok = 0
        for result_value in ("success", "exit-code", "signal", "exec-condition"):
            for code in (0, 1, 2, 3, 4):
                for status in (0, 1):
                    for start, end in ((0, 0), (START, 0), (0, END), (START, END), (END, START)):
                        for active, sub in (("inactive", "dead"), ("failed", "failed"), ("active", "exited"),
                                            ("activating", "start")):
                            for load in ("loaded", "not-found", "masked"):
                                block = service(result_value, start, end, code, status, active, sub, load)
                                result = check(FakeSystemctl(block))
                                valid = (load == "loaded" and result_value == "success" and code == 1
                                         and status == 0 and 0 < start <= end and active in ("inactive", "active"))
                                with self.subTest(block=block):
                                    self.assertEqual(result.state is State.OK, valid)
                                seen_ok += valid
        self.assertEqual(seen_ok, 2)  # jen úspěšný dokončený běh, neaktivní i s RemainAfterExit


class LastSuccessTest(unittest.TestCase):
    """Poslední doložený úspěch a další pokus (review SERVICES-3), skutečné vlastnosti systemd.

    Podle systemd 255 (org.freedesktop.systemd1(5)) popisují ExecMain* poslední běh
    hlavního procesu nebo právě běžící proces a Result výsledek posledního běhu
    služby. Selhání před ExecStart proto nechá v ExecMain* předchozí úspěch.
    """

    def observed_success(self, end=END):
        monitor, clock = monitor_for(FakeSystemctl(service(start=end - 1000, end=end)), at=end + HOUR)
        self.assertEqual(outcome(monitor.collect(clock.now, OBSERVED)[0]), (State.OK, SUCCEEDED))
        return monitor, clock

    def step(self, monitor, clock, block, at):
        monitor.run.show(block)
        clock.now = at
        return monitor.collect(clock.now, OBSERVED)[0]

    def test_next_run_start_including_exec_start_pre_keeps_last_success(self):
        monitor, clock = self.observed_success()
        for block in (service(active="activating", sub="start-pre"),  # ještě časy předchozího běhu
                      service(start=END + 2 * HOUR, end=0, code=0, status=0, active="activating", sub="start")):
            with self.subTest(sub=block.split("SubState=")[1]):
                result = self.step(monitor, clock, block, END + 3 * HOUR)
                self.assertEqual(outcome(result), (State.OK, RUNNING))
                self.assertIn("poslední doložený úspěch před 3.0 h", result.summary)
                self.assertEqual(monitor.last_success, END)

    def test_new_successful_run_updates_the_time_of_success(self):
        monitor, clock = self.observed_success()
        self.step(monitor, clock, RUNNING_NOW, END + HOUR + 60)
        new_end = END + 25 * HOUR
        result = self.step(monitor, clock, service(start=new_end - 1000, end=new_end), END + 28 * HOUR)
        self.assertEqual(monitor.last_success, new_end)
        self.assertEqual(outcome(result), (State.OK, SUCCEEDED))  # 3 h od nového úspěchu, ne 28 h
        self.assertIn("před 3.0 h", result.summary)

    def test_new_failed_run_depends_on_age_of_last_success(self):
        for age, expected in ((5, State.WARNING), (50, State.WARNING), (51, State.ERROR)):
            with self.subTest(hours=age):
                monitor, clock = self.observed_success()
                result = self.step(monitor, clock, service(result="exit-code", start=END + HOUR, end=END + HOUR + 60,
                                                           status=1, active="failed", sub="failed"), END + age * HOUR)
                self.assertEqual(outcome(result), (expected, FAILED))

    def test_failure_before_exec_start_is_never_ok(self):
        # Spuštění selhalo už v ExecStartPre (nebo nešlo vytvořit proces): ActiveState=failed,
        # ExecMain* dál ukazují předchozí úspěšný běh (kód 1, status 0).
        for result_value in ("exit-code", "timeout", "resources", "start-limit-hit"):
            with self.subTest(result=result_value):
                pre = service(result=result_value, active="failed", sub="failed")
                self.assertEqual(outcome(check(FakeSystemctl(pre))), (State.ERROR, FAILED))  # bez paměti
                monitor, clock = self.observed_success()
                result = self.step(monitor, clock, pre, END + 2 * HOUR)
                self.assertEqual(outcome(result), (State.WARNING, FAILED))
                # Kód předchozího hlavního procesu se k tomuto selhání nepřipisuje.
                self.assertNotIn("ExecMainStatus", result.summary)
                self.assertEqual(monitor.last_success, END)

    def test_skipped_run_is_never_a_new_success(self):
        # Condition*=: jednotka se vůbec nespustí, systemd nechá Result i ExecMain* beze změny.
        # Úspěch je dál ten předchozí a jeho stáří roste.
        monitor, clock = self.observed_success()
        self.assertEqual(outcome(self.step(monitor, clock, SUCCESS, END + 27 * HOUR)), (State.WARNING, OVERDUE))
        self.assertEqual(monitor.last_success, END)
        # ExecCondition=: Result=exec-condition, služba neběžela a podle systemd neselhala.
        skip = service(result="exec-condition")
        result = check(FakeSystemctl(skip))
        self.assertEqual(outcome(result), (State.UNKNOWN, SKIPPED))  # bez paměti nic nedoloženo
        self.assertNotIn("selhal", result.summary)
        monitor, clock = self.observed_success()
        result = self.step(monitor, clock, skip, END + 2 * HOUR)
        self.assertEqual(outcome(result), (State.OK, SKIPPED))
        self.assertIn("poslední doložený úspěch před 2.0 h", result.summary)
        self.assertEqual(outcome(self.step(monitor, clock, skip, END + 27 * HOUR)), (State.WARNING, OVERDUE))
        self.assertEqual(monitor.last_success, END)  # přeskočení čas úspěchu neposune

    def test_reset_failed_shows_only_the_real_age_of_the_previous_run(self):
        # Ruční `systemctl reset-failed` vrátí Result=success a ExecMain* jsou z předchozího
        # úspěšného běhu: OK jen podle jeho skutečného stáří, bez obnovení.
        self.assertEqual(outcome(check(FakeSystemctl(SUCCESS), at=END + 27 * HOUR)), (State.WARNING, OVERDUE))


class EvidenceLossTest(unittest.TestCase):
    """Ztráta údajů systemd: nikdy falešné OK a nikdy automaticky „selhání“ (review SERVICES-3)."""

    def assert_lost(self, result, reason=None):
        self.assertEqual(outcome(result), (State.UNKNOWN, reason))
        self.assertNotIn("selhal", result.summary)

    def test_result_success_without_valid_times(self):
        self.assert_lost(check(FakeSystemctl(NEVER_RUN)), NO_RUN)
        for start, end in ((0, END), (START, 0), (END, START)):
            with self.subTest(start=start, end=end):
                self.assert_lost(check(FakeSystemctl(service(start=start, end=end))))

    def test_zeroed_exec_main_code_and_status(self):
        # Kód ukončení chybí, i když časy zůstaly: údaje jsou neúplné, selhání nedokládají.
        for code in (0, 4, 9):
            with self.subTest(code=code):
                self.assert_lost(check(FakeSystemctl(service(code=code, status=0))))

    def test_unload_restart_and_old_memory(self):
        monitor, clock = monitor_for(FakeSystemctl(SUCCESS), at=END + HOUR)
        monitor.collect(clock.now, OBSERVED)
        monitor.run.show(NEVER_RUN)  # uvolnění jednotky z paměti systemd
        self.assert_lost(monitor.collect(clock.now, OBSERVED)[0], NO_RUN)
        self.assertIsNone(monitor.last_success)
        # Restart NexusSTATu i systému: nový proces bez paměti, jen aktuálně ověřitelné údaje.
        self.assert_lost(check(FakeSystemctl(NEVER_RUN), at=60.0), NO_RUN)
        self.assert_lost(check(FakeSystemctl(RUNNING_NOW)), RUNNING)
        # Starší úspěch jen v paměti se nahradí novějším doloženým, nikdy nepřebije chybějící data.
        monitor, clock = monitor_for(FakeSystemctl(service(start=100.0, end=200.0)), at=300.0)
        monitor.collect(clock.now, OBSERVED)
        monitor.run.show(service(start=END - 1000, end=END))
        clock.now = END + HOUR
        monitor.collect(clock.now, OBSERVED)
        self.assertEqual(monitor.last_success, END)
        monitor.run.show(service(start=0, end=END))  # nekonzistentní data po ztrátě
        self.assert_lost(monitor.collect(clock.now, OBSERVED)[0])

    def test_invalid_output_and_missing_or_masked_unit_are_not_failures(self):
        self.assert_lost(check(FakeSystemctl(stdout=b"Result=success\n")))
        self.assert_lost(check(FakeSystemctl(SUCCESS, returncode=1)))
        for load in ("not-found", "masked"):
            with self.subTest(load=load):
                result = check(FakeSystemctl(service(start=0, end=0, code=0, load=load)))
                self.assertEqual(outcome(result), (State.ERROR, NO_JOB))  # doložený stav jednotky, ne Restic
                self.assertNotIn("selhal", result.summary)


def screens(result, now=None):
    """(SERVICES, první text STATUS, symbol NEXUS, signál RGB) pro výsledek záloh vedle zdravého systému."""
    now = result.expires_monotonic - VALIDITY if now is None else now
    monitored = FakeSystem().collect(now, OBSERVED) + [result]
    results = complete_service_results(monitored, OBSERVED, now + VALIDITY, monitored={BACKUP_ID})
    problems = {problem.id: problem for problem in active_problems(results, now)}
    status = status_texts(problems[BACKUP_ID], now)[0] if BACKUP_ID in problems else None
    signal = rgb.rgb_signal(monitored, rgb.Acknowledgements(), True, False, now)
    return dict(service_rows(results, now))["BACKUP"], status, home_symbol(results, now), signal


def blink_groups(signal):
    """Skupiny bliknutí jednoho průchodu ze skutečných snímků frame_at: [(barva, počet), ...]."""
    groups, lit, dark = [], False, 0
    for index in range(round(rgb.pass_length(signal.steps) / 0.1)):
        frame = rgb.frame_at(signal, 0.05 + index * 0.1, rgb.BRIGHTNESS_DEFAULT)
        if frame.lit and not lit:
            if not groups or dark >= 10:
                groups.append([frame.color, 0])
            groups[-1][1] += 1
        dark = 0 if frame.lit else dark + 1
        lit = frame.lit
    return [tuple(group) for group in groups]


class DisplayAndRgbTest(unittest.TestCase):
    def test_every_state_on_services_status_nexus_and_rgb(self):
        cases = (
            ("čerstvá záloha", check(FakeSystemctl(SUCCESS)), ("OK", None), rgb.LIGHT),
            ("stará záloha", check(FakeSystemctl(SUCCESS), at=END + 30 * HOUR), ("WARN", "BACKUP OVERDUE"),
             rgb.CODES),
            ("velmi stará záloha", check(FakeSystemctl(SUCCESS), at=END + 60 * HOUR), ("ERR", "BACKUP OVERDUE"),
             rgb.CODES),
            ("selhání", check(FakeSystemctl(FAILURE)), ("ERR", "BACKUP FAILED"), rgb.CODES),
            ("chybí úloha", check(FakeSystemctl(service(load="not-found", start=0, end=0, code=0))),
             ("ERR", "BACKUP NO JOB"), rgb.CODES),
            ("neaktivní timer", check(FakeSystemctl(SUCCESS, timer(active="inactive", sub="dead")), timer_unit=TIMER),
             ("WARN", "BACKUP NO TIMER"), rgb.CODES),
            ("běží bez paměti", check(FakeSystemctl(RUNNING_NOW)), ("UNKN", "BACKUP RUNNING"), rgb.OFF),
            ("přeskočeno bez paměti", check(FakeSystemctl(service(result="exec-condition"))),
             ("UNKN", "BACKUP SKIPPED"), rgb.OFF),
            ("bez doloženého běhu", check(FakeSystemctl(NEVER_RUN)), ("UNKN", "BACKUP NO DATA"), rgb.OFF),
            ("nečitelné", check(FakeSystemctl(SUCCESS, returncode=1)), ("UNKN", "BACKUP NO DATA"), rgb.OFF),
        )
        for name, result, (row, status), kind in cases:
            with self.subTest(case=name):
                shown_row, shown_status, symbol, signal = screens(result)
                self.assertEqual((shown_row, shown_status), (row, status))
                self.assertEqual(signal.kind, kind)
                if shown_status is not None:
                    self.assertLessEqual(len(shown_status), 15)
                if kind == rgb.CODES:
                    # Žádný nový RGB režim: kategorie 4 (zálohy), oranžově WARNING, červeně ERROR.
                    color = rgb.ORANGE if result.state is State.WARNING else rgb.RED
                    self.assertEqual(blink_groups(signal), [(color, 4)])
                    self.assertEqual(symbol, ALERT_SYMBOL)
                else:
                    self.assertEqual(symbol, UNKNOWN_SYMBOL)  # NEXUS nemá kolektor

    def test_expired_result_is_stale(self):
        result = check(FakeSystemctl(SUCCESS))
        row, status, _, signal = screens(result, now=result.expires_monotonic)
        self.assertEqual((row, status, signal.kind), ("UNKN", "BACKUP STALE", rgb.OFF))
        self.assertEqual(dict(effective(result, result.expires_monotonic).details), {})

    def test_disabled_is_na_and_enabled_without_result_is_unknown(self):
        added = {r.id: r for r in complete_service_results([], OBSERVED, END + VALIDITY)}
        self.assertEqual((added[BACKUP_ID].state, added[BACKUP_ID].summary), (State.NA, "Záloha: sledování vypnuto"))
        self.assertIs(effective(added[BACKUP_ID], 10 ** 9).state, State.NA)
        added = {r.id: r for r in complete_service_results([], OBSERVED, END + VALIDITY, monitored={BACKUP_ID})}
        self.assertIs(added[BACKUP_ID].state, State.UNKNOWN)
        result = check(FakeSystemctl(FAILURE))
        for monitored in ((), {BACKUP_ID}):
            completed = complete_service_results([result], OBSERVED, END + VALIDITY, monitored=monitored)
            self.assertEqual([r for r in completed if r.id == BACKUP_ID], [result])

    def test_alarm_evaluator_leaves_backup_state_unchanged(self):
        evaluator = AlarmEvaluator(AlarmsConfig())
        for block in (SUCCESS, FAILURE, NEVER_RUN, RUNNING_NOW):
            result = check(FakeSystemctl(block))
            self.assertEqual(evaluator.evaluate([result], END), [result])


class ConfigurationTest(unittest.TestCase):
    """[services.backup]: výchozí vypnuto, názvy jednotek jen výslovně a jen platné názvy systemd."""

    def test_defaults_are_off_without_units(self):
        self.assertEqual(Config().services.backup, BackupConfig(enabled=False, unit=None, timer=None, timeout_seconds=2))
        self.assertEqual(services_config_from_dict({}).backup, BackupConfig())

    def test_example_config_keeps_backup_off_with_generic_names_only(self):
        backup = load_config(EXAMPLE_CONFIG).services.backup
        self.assertEqual((backup.enabled, backup.unit, backup.timer, backup.timeout_seconds), (False, None, None, 2))
        text = EXAMPLE_CONFIG.read_text(encoding="utf-8")
        section = text[text.index("[services.backup]"):]
        self.assertIn('# unit = "example-backup.service"', section)
        self.assertIn('# timer = "example-backup.timer"', section)

    def test_valid_names(self):
        for unit, timer_unit in ((UNIT, TIMER), ("restic@daily.service", None), ("a:b_c.d-e.service", "x.timer")):
            with self.subTest(unit=unit):
                config = services_config_from_dict({"backup": {"enabled": True, "unit": unit, "timer": timer_unit,
                                                               "timeout_seconds": 1.5}} if timer_unit else
                                                   {"backup": {"enabled": True, "unit": unit, "timeout_seconds": 1.5}})
                self.assertEqual(config.backup, BackupConfig(True, unit, timer_unit, 1.5))

    def test_invalid_values(self):
        sections = (
            {"backup": {"enabled": True}},  # zapnuto bez služby
            {"backup": {"enabled": True, "timer": TIMER}},
            {"backup": {"enabled": "true", "unit": UNIT}},
            {"backup": {"unit": "example-backup"}},  # bez přípony
            {"backup": {"unit": TIMER}},  # timer místo služby
            {"backup": {"timer": UNIT}},  # služba místo timeru
            {"backup": {"unit": "-x.service"}},  # vypadalo by jako volba systemctl
            {"backup": {"unit": "a b.service"}},
            {"backup": {"unit": "a;reboot.service"}},
            {"backup": {"unit": "$(id).service"}},
            {"backup": {"unit": "/etc/systemd/system/x.service"}},
            {"backup": {"unit": "a\\x2db.service"}},
            {"backup": {"unit": "foo@.service"}},  # šablona, ne konkrétní úloha
            {"backup": {"unit": "x" * 250 + ".service"}},
            {"backup": {"unit": 5}},
            {"backup": {"timeout_seconds": 0}},
            {"backup": {"timeout_seconds": 5.5}},
            {"backup": {"timeout_seconds": True}},
            {"backup": {"unit": UNIT, "repository": "/srv/backup"}},  # žádný repozitář Resticu
            {"backup": {"unit": UNIT, "password_file": "/root/.restic"}},  # žádné přihlašovací údaje
            {"backup": {"unit": UNIT, "command": "restic snapshots"}},  # žádný vlastní příkaz
            {"backup": "on"},
        )
        for section in sections:
            with self.subTest(section=section):
                with self.assertRaises(ConfigError):
                    services_config_from_dict(section)

    def test_section_from_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(f'[services.backup]\nenabled = true\nunit = "{UNIT}"\ntimer = "{TIMER}"\n', encoding="utf-8")
            self.assertEqual(load_config(path).services.backup, BackupConfig(True, UNIT, TIMER, 2))
            path.write_text("[services.backup]\nenabled = true\n", encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, r"\[services.backup\] enabled = true vyžaduje unit"):
                load_config(path)

    def test_disabled_monitoring_never_runs_systemctl(self):
        forbidden = AssertionError("spuštění příkazu při vypnutém sledování záloh")
        disabled = (Config(), Config(services=ServicesConfig(backup=BackupConfig(False, UNIT, TIMER))),
                    load_config(EXAMPLE_CONFIG))
        with mock.patch("nexusstat.run.BackupMonitor", side_effect=forbidden) as monitor_class, \
                mock.patch("subprocess.run", side_effect=forbidden) as run_mock, \
                mock.patch("subprocess.Popen", side_effect=forbidden) as popen_mock:
            for config in disabled:
                monitor = live_monitor(config)
                self.assertNotIn(BACKUP_ID, monitor.labels)
                completed = complete_service_results([], OBSERVED, END + VALIDITY, monitored=monitor.labels)
                self.assertIs({r.id: r for r in completed}[BACKUP_ID].state, State.NA)
        monitor_class.assert_not_called()
        run_mock.assert_not_called()
        popen_mock.assert_not_called()

    def test_enabled_monitoring_adds_one_backup_collector_without_running_anything(self):
        config = Config(collector=CollectorConfig(interval_seconds=5),
                        services=ServicesConfig(backup=BackupConfig(True, UNIT, TIMER, 1.5)))
        with mock.patch("subprocess.Popen", side_effect=AssertionError("nic se nespouští")):
            monitor = live_monitor(config)  # jen vytvoření
        scheduled = monitor.monitors[-1]
        self.assertIsInstance(scheduled, Scheduled)  # dotaz jen v plánovaném termínu (SCHED-1)
        backup = scheduled.monitor
        self.assertIsInstance(backup, BackupMonitor)
        self.assertEqual((backup.unit, backup.timer, backup.timeout_seconds, backup.validity_seconds),
                         (UNIT, TIMER, 1.5, 15))
        self.assertEqual(scheduled.validity_seconds, 15)
        self.assertIs(backup.run, subprocess.run)
        self.assertEqual(sum(isinstance(getattr(m, "monitor", m), BackupMonitor) for m in monitor.monitors), 1)
        self.assertIn(BACKUP_ID, monitor.labels)
        self.assertFalse(monitor.simulated)

    def test_terminal_lines(self):
        lines = []
        clock = FakeClock(END + 3 * HOUR)

        def sleep(seconds):
            if len(lines) > 2:
                raise KeyboardInterrupt
            clock.now += seconds

        systemctl = FakeSystemctl(SUCCESS)
        backup = BackupMonitor(VALIDITY, UNIT, None, TIMEOUT, run=systemctl, clock=clock)
        results = run_loop(Collectors(FakeSystem(), backup), CollectorConfig(), AlarmEvaluator(AlarmsConfig()),
                           clock=clock, utc_clock=lambda: OBSERVED, sleep=sleep, output=lines.append)
        self.assertIn("Záloha: úloha naposledy skončila úspěšně před 3.0 h", lines[1])
        self.assertNotIn(UNIT, lines[1])  # název jednotky se do výpisu ani historie nezapisuje
        self.assertIs({r.id: r for r in results}[BACKUP_ID].state, State.OK)
        lines.clear()
        clock.now = END + 30 * HOUR
        run_loop(Collectors(FakeSystem(), backup), CollectorConfig(), AlarmEvaluator(AlarmsConfig()), clock=clock,
                 utc_clock=lambda: OBSERVED, sleep=sleep, output=lines.append)
        self.assertIn("(nad 26 h) [WARNING]", lines[1])


class PreviewTest(unittest.TestCase):
    """Živý náhled LIVE: BACKUP N/A bez sledování, se sledováním výsledek úlohy, bez spouštění."""

    def session(self, systemctl, enabled):
        config = Config(services=ServicesConfig(backup=BackupConfig(enabled, UNIT, None)))
        clock = FakeClock(END + 3 * HOUR)

        def factory(config):
            validity = config.collector.validity_seconds
            monitors = [FakeSystem(validity)]
            backup = config.services.backup
            if backup.enabled:
                monitors.append(BackupMonitor(validity, backup.unit, backup.timer, backup.timeout_seconds,
                                              run=systemctl, clock=clock))
            return Collectors(*monitors)

        return PreviewSession(config, scenario=LIVE, clock=SessionClock(real_clock=lambda: 5000.0),
                              utc_clock=lambda: OBSERVED, wall_clock=lambda: datetime(2026, 10, 28, 13, 0),
                              monitor_factory=factory)

    def test_enabled_and_disabled(self):
        session = self.session(FakeSystemctl(FAILURE), enabled=True)
        now = session.clock.now()
        self.assertEqual(dict(service_rows(session.results, now))["BACKUP"], "ERR")
        self.assertIn("BACKUP FAILED", [status_texts(p, now)[0] for p in active_problems(session.results, now)])
        self.assertIn("BACKUP z výsledku úlohy v systemd", session.source_text())
        systemctl = FakeSystemctl(SUCCESS)
        session = self.session(systemctl, enabled=False)
        self.assertEqual(dict(service_rows(session.results, session.clock.now()))["BACKUP"], "N/A")
        self.assertEqual(systemctl.calls, [])
        self.assertIn("BACKUP N/A (sledování vypnuto)", session.source_text())
        session.press()
        self.assertEqual(session.led_frame().color, rgb.WHITE)  # N/A bílou neblokuje


if __name__ == "__main__":
    unittest.main()
