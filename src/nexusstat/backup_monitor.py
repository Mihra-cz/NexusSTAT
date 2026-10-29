"""Read-only kontrola výsledku existující zálohovací úlohy přes systemd (SERVICES-3).

Při každé skutečné kontrole jediný dotaz `systemctl show` na výslovně nastavenou službu
zálohy (`[services.backup] unit`) a volitelně její timer, s pevnými argumenty,
bez shellu, s limitem `timeout_seconds` a jako běžný uživatel (bez sudo,
polkit ani skupin). Nic se nespouští ani nemění: ani zálohovací úloha, ani
Restic, jeho repozitář, přihlašovací údaje, NAS nebo journal. Při vypnutém
sledování (výchozí stav) kolektor vůbec nevznikne (run.live_monitor).

Od SCHED-1 se systemd neptá periodicky (run.Scheduled): hned po spuštění, pak
po předpokládaném konci příštího běhu podle plánu timeru, při běžící úloze po
15 min a bez spolehlivého plánu nejvýš jednou denně (`plan`); přechodně
selhaný dotaz dostane jediný opravný pokus za 60 min. Mezi dotazy se
jen přepočítá stáří posledního doloženého úspěchu, takže přechod přes 26/50 h
se ukáže včas bez dalšího dotazu (`between`).

Výsledkem je jediná kontrola `service.backup`. Kontroluje **úspěch úlohy
systemd, ne obsah repozitáře Resticu**: OK znamená, že úloha naposledy doloženě
skončila sama s kódem 0 a není to déle než 26 h. Že tím vznikl úplný snapshot,
platí jen tehdy, když skript úlohy předává všechny chyby Resticu (ověří se
read-only na Nexusu). `history.snapshot` je samostatná kontrola.

Údaje systemd o běhu nejsou trvalá evidence: zmizí restartem systému a také
tehdy, když systemd neaktivní jednotku uvolní z paměti. Proto:

* `Result=success` sám nestačí: má ho i jednotka, která nikdy neběžela.
  Úspěch je jen doložený dokončený běh (čas konce, `ExecMainCode` = proces
  skončil sám, `ExecMainStatus` 0),
* chybějící nebo nulové časy nejsou úspěch, ale UNKNOWN; kolektor pak zahodí
  i úspěch, který si dřív zapamatoval,
* zapamatovaný úspěch žije jen v paměti tohoto procesu (nikdy přes restart
  NexusSTATu ani systému) a použije se jen vedle toho, co systemd právě
  dokládá: běžící úlohy a nového neúspěšného pokusu.

Stáří se počítá z monotónních časů systemd (CLOCK_MONOTONIC, stejné hodiny
jako time.monotonic), takže ho nerozhodí posun systémového času.
"""

import subprocess
import time
from datetime import datetime, timezone

from .config import check_health_timeout, check_unit_name
from .core import SEVERITY_ORDER, CheckResult, State, is_finite_number
from .services import BACKUP_ID

LABELS = {BACKUP_ID: "Záloha"}

# Pevná cesta: systemctl se nehledá v PATH. Debian 13 má sloučené /usr.
SYSTEMCTL = "/usr/bin/systemctl"
# Vlastnosti jednotek. Pořadí ve výstupu systemd neodpovídá pořadí dotazu
# a každý blok obsahuje jen vlastnosti, které jednotka má: služba bez plánu
# timeru, timer jen obecné a plán (ověřeno se systemd 255).
SERVICE_PROPERTIES = ("Id", "Names", "LoadState", "ActiveState", "SubState", "Result", "ExecMainCode",
                      "ExecMainStatus", "ExecMainStartTimestampMonotonic", "ExecMainExitTimestampMonotonic")
TIMER_PROPERTIES = ("Id", "Names", "LoadState", "ActiveState")
# Plán timeru slouží jen k plánování dotazů (SCHED-1), proto není povinný:
# bez něj je plán neznámý a stav zálohy se nemění.
PLAN_PROPERTY = "NextElapseUSecRealtime"
PROPERTIES = SERVICE_PROPERTIES + (PLAN_PROPERTY,)
# TZ=UTC: plán timeru se vypíše jako "Thu 2026-10-29 23:00:00 UTC" bez závislosti
# na pásmu systému (monotónní časy v µs se tím nemění).
ENV = {"LC_ALL": "C", "TZ": "UTC"}
MAX_OUTPUT_BYTES = 8192  # běžný výstup dvou jednotek má asi 650 B

# Plánování dotazů (SCHED-1): ne periodicky, ale podle údajů systemd.
RECHECK_SECONDS = 15 * 60  # úloha běží, nebo plánovaný start právě nastal
SETTLE_SECONDS = 5 * 60  # rezerva po předpokládaném konci (zpoždění spuštění, AccuracySec)
PENDING_SECONDS = 3600  # jak dlouho po plánovaném startu ještě čekat, než se úloha ukáže
FALLBACK_SECONDS = 24 * 3600  # bez spolehlivého plánu nejvýš jeden dotaz denně; delší odstup nikdy
QUERY_RETRY_SECONDS = 3600  # jediný opravný dotaz po přechodném selhání samotného dotazu

# Stáří posledního doloženého úspěchu, hranice včetně jako u history.snapshot:
# přesně 26 h je ještě OK, přesně 50 h ještě WARNING (denní záloha s rezervou).
WARNING_AGE_SECONDS = 26 * 3600
ERROR_AGE_SECONDS = 50 * 3600

# ExecMainCode podle waitid(): 1 = proces skončil sám (CLD_EXITED), 0 = žádný
# proces neskončil, 2 a 3 = ukončen signálem. Hodnota je číslo, ne text.
CLD_EXITED = 1
EXIT_CODES = (1, 2, 3)  # kódy dokončeného hlavního procesu; jiné u dokončeného běhu nedávají smysl
# Result, když ExecCondition= úlohu přeskočila: služba neběžela, ale podle
# systemd neselhala (systemd.service(5), systemd.exec(5) $SERVICE_RESULT).
SKIPPED_RESULT = "exec-condition"

# Rozhodující důvod stavu; zároveň hodnota `backup` v details (jen tyto pevné
# texty, takže historie zapíše řádek jen při skutečné změně).
BACKUP = "backup"
SUCCEEDED = "ok"
OVERDUE = "overdue"  # poslední doložený úspěch je starší než 26 h
FAILED = "failed"  # poslední pokus selhal
RUNNING = "running"  # úloha právě běží
SKIPPED = "skipped"  # poslední spuštění přeskočila ExecCondition=
NO_RUN = "no_run"  # systemd neuvádí žádný dokončený běh
NO_JOB = "no_job"  # nastavená služba neexistuje, je maskovaná nebo nejde načíst
NO_TIMER = "no_timer"  # nastavený timer neexistuje, je maskovaný nebo není aktivní

# LoadState nastavené jednotky, které dokládají, že úloha pod tímto jménem neběží.
MISSING = {"not-found": "neexistuje", "masked": "je maskovaná", "bad-setting": "má chybné nastavení",
           "error": "nejde načíst"}
RUNNING_STATES = {"activating", "deactivating", "reloading", "refreshing"}


class QueryError(ValueError):
    """Výstup systemctl nejde věrohodně přečíst."""


class QueryFailed(QueryError):
    """Dotaz sám neproběhl (systemctl skončil nenulovým kódem, např. nedostupná D-Bus).

    Na rozdíl od neplatného nebo rozporného výstupu nic nedokládá o jednotce,
    takže smí dostat jeden opravný pokus (SCHED-1).
    """


def parse_blocks(text, count):
    """Bloky vlastností (slovníky) z výstupu `systemctl show` pro `count` jednotek."""
    blocks = text.strip("\n").split("\n\n") if text.strip() else []
    if len(blocks) != count:
        raise QueryError(f"neočekávaný počet bloků ({len(blocks)} místo {count})")
    parsed = []
    for block in blocks:
        props = {}
        for line in block.split("\n"):
            key, separator, value = line.partition("=")
            if not separator or not key or key in props:
                raise QueryError("neočekávaný řádek výstupu")
            props[key] = value
        parsed.append(props)
    return parsed


def required(props, names):
    missing = [name for name in names if name not in props]
    if missing:
        raise QueryError(f"neúplné údaje (chybí {', '.join(missing)})")


def number(props, name):
    """Nezáporné celé číslo z vlastnosti; jinak QueryError."""
    value = props[name]
    if not (value.isascii() and value.isdigit()):
        raise QueryError(f"neplatná hodnota {name}")
    return int(value)


def age_state(age):
    if age > ERROR_AGE_SECONDS:
        return State.ERROR
    if age > WARNING_AGE_SECONDS:
        return State.WARNING
    return State.OK


def job_running(active, start, end):
    """Úloha právě běží, i ve fázi ExecStartPre (systemd pak ukazuje ještě časy předchozího běhu)."""
    return active in RUNNING_STATES or (active == "active" and start > 0 and end == 0)


def next_elapse(props):
    """Plánovaný příští start timeru v sekundách UTC, nebo None (plán neznámý).

    Systemd ho s TZ=UTC vypíše jako "Thu 2026-10-29 23:00:00 UTC" (ověřeno se
    systemd 255). Prázdná hodnota (neaktivní timer, timer jen s monotónním
    plánem OnBootSec/OnUnitActiveSec) ani jiný tvar se nevyvozují. Den v týdnu
    se nečte, aby rozbor nezávisel na národním nastavení procesu.
    """
    parts = props.get(PLAN_PROPERTY, "").split(" ")
    if len(parts) != 4 or parts[3] != "UTC":
        return None
    try:
        moment = datetime.strptime(f"{parts[1]} {parts[2]}", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return moment.replace(tzinfo=timezone.utc).timestamp()


def worse(first, second):
    """Závažnější ze dvou stavů podle core.SEVERITY_ORDER (doložená závada před UNKNOWN)."""
    return min(first, second, key=SEVERITY_ORDER.index)


def hours(seconds):
    return f"{seconds / 3600:.1f} h"


class BackupMonitor:
    """Výsledek a stáří poslední zálohovací úlohy ze systemd.

    `run` je subprocess.run a `clock` time.monotonic; testy dodají syntetický
    výstup systemctl a výsledky jsou pak fiktivní data (simulated=True), stejně
    jako u ostatních kolektorů se vzorovými soubory.
    Výsledek platí do dalšího plánovaného dotazu (`next_check`) a ještě
    `validity_seconds` (stejná rezerva jako u ostatních kolektorů).
    """

    labels = LABELS  # id výsledků, které collect() vrací (pro run.failed_results)

    def __init__(self, validity_seconds, unit, timer, timeout_seconds, run=subprocess.run, clock=time.monotonic):
        if not is_finite_number(validity_seconds) or validity_seconds <= 0:
            raise ValueError(f"validity_seconds musí být kladný počet sekund, ne {validity_seconds!r}")
        check_unit_name("unit", unit, "service")  # ConfigError je ValueError
        if timer is not None:
            check_unit_name("timer", timer, "timer")
        check_health_timeout(timeout_seconds)
        self.validity_seconds = validity_seconds
        self.unit = unit
        self.timer = timer
        self.timeout_seconds = timeout_seconds
        self.run = run
        self.clock = clock
        self.simulated = run is not subprocess.run
        # Konec posledního doloženého úspěšného běhu (monotónní s), jen z tohoto
        # procesu. Nepřežije restart a zahodí se, jakmile ho systemd přestane dokládat.
        self.last_success = None
        # Plánování (SCHED-1): termín dalšího dotazu, čas posledního dotazu
        # a jeho věrohodně přečtené bloky (None = dotaz nic nedoložil).
        self.due = None
        self.queried = None
        self.blocks = None
        # Přechodné selhání samotného dotazu a zda je další dotaz už opravný pokus.
        self.query_failed = False
        self.retrying = False

    def units(self):
        return [self.unit] + ([self.timer] if self.timer else [])

    def command(self):
        """Pevné argumenty dotazu; názvy jednotek až za "--", nikdy shell."""
        return [SYSTEMCTL, "show", "--no-pager", "--no-ask-password", "--property=" + ",".join(PROPERTIES),
                "--", *self.units()]

    def collect(self, now, observed_utc):
        """Jeden výsledek `service.backup` z dotazu. `now` je monotónní čas měření, `observed_utc` čas UTC.

        Selhal-li přechodně samotný dotaz (`query_failed`), přijde jediný opravný
        pokus za QUERY_RETRY_SECONDS; selže-li i ten, platí běžný plán (bez
        přečtených údajů nejvýš jednou denně). Výsledek je mezitím UNKNOWN,
        nikdy dřívější úspěch.
        """
        retried, self.retrying = self.retrying, False
        # Náhradní termín předem: ani neočekávaná výjimka nevede k častějšímu dotazování.
        self.blocks, self.queried, self.due = None, now, now + FALLBACK_SECONDS
        state, text, details = self.evaluate()
        if self.query_failed and not retried:
            self.retrying = True
            self.due = now + QUERY_RETRY_SECONDS
            text = f"{text}; opravný dotaz za {QUERY_RETRY_SECONDS // 60} min"
        else:
            self.due = now + self.plan(observed_utc.timestamp())
        return [CheckResult(BACKUP_ID, state, f"{LABELS[BACKUP_ID]}: {text}", observed_utc,
                            self.due + self.validity_seconds, simulated=self.simulated, details=details)]

    def next_check(self, now):
        """Monotónní čas dalšího dotazu (run.Scheduled); určil ho poslední dotaz."""
        return self.due

    def plan(self, utc_now):
        """Za kolik sekund se systemd zeptat znovu, podle bloků posledního dotazu.

        Běžící úloha za RECHECK_SECONDS. Jinak po předpokládaném konci příštího
        běhu podle timeru: plánovaný start + délka posledního běhu + rezerva.
        Když plánovaný start už nastal a úloha se ještě neukázala, za
        RECHECK_SECONDS, ale jen PENDING_SECONDS od plánu. Bez spolehlivého
        plánu (nečitelný dotaz, chybějící služba, bez timeru, neaktivní nebo
        jen monotónní timer) za FALLBACK_SECONDS a nikdy později. Nepředpokládá
        se denní plán: týdenní timer dá týdenní termín, omezený na jeden den.
        `utc_now` jen převede plán timeru na odstup; stav se z něj nikdy neurčuje.
        """
        if self.blocks is None or self.blocks[0]["LoadState"] != "loaded":
            return FALLBACK_SECONDS
        service = self.blocks[0]
        start = number(service, "ExecMainStartTimestampMonotonic") / 1e6
        end = number(service, "ExecMainExitTimestampMonotonic") / 1e6
        if job_running(service["ActiveState"], start, end):
            return RECHECK_SECONDS
        elapse = None if self.timer is None else next_elapse(self.blocks[1])
        if elapse is None:
            return FALLBACK_SECONDS
        wait = elapse - utc_now
        if wait > 0:
            duration = max(0.0, end - start) if start and end else 0.0
            return min(wait + duration + SETTLE_SECONDS, FALLBACK_SECONDS)
        if -wait <= PENDING_SECONDS:
            return RECHECK_SECONDS
        return FALLBACK_SECONDS

    def between(self, results, now, observed_utc):
        """Výsledek mezi dotazy (run.Scheduled): přechod stáří přes 26/50 h bez dotazu na systemd.

        Přepočítají se jen bloky posledního věrohodně přečteného dotazu k času
        `now`: stáří posledního doloženého úspěchu roste i bez dotazu, nic
        jiného se tím nezjistí. Nový výsledek vznikne jen při změně stavu nebo
        důvodu; má čas tohoto přepočtu, původní platnost (nic se neprodlužuje)
        a popis přizná, z jak starého dotazu vychází. Jinak se vrací poslední
        výsledek beze změny.
        """
        if self.blocks is None:
            return results
        [last] = results
        try:
            state, text, details = self.assess(self.blocks, now)
        except QueryError:
            return results
        if state is last.state and details == dict(last.details):
            return results
        note = f" (přepočet bez dotazu, údaje systemd z dotazu před {hours(now - self.queried)})"
        return [CheckResult(BACKUP_ID, state, f"{LABELS[BACKUP_ID]}: {text}{note}", observed_utc,
                            last.expires_monotonic, simulated=self.simulated, details=details)]

    def query(self):
        """Bloky vlastností nastavených jednotek v pořadí dotazu, nebo výjimka."""
        completed = self.run(self.command(), stdin=subprocess.DEVNULL, capture_output=True,
                             timeout=self.timeout_seconds, env=ENV, check=False)
        if completed.returncode != 0:
            raise QueryFailed(f"systemctl skončil kódem {completed.returncode}")
        if len(completed.stdout) > MAX_OUTPUT_BYTES:
            raise QueryError("neočekávaně dlouhý výstup")
        try:
            text = completed.stdout.decode("ascii")
        except UnicodeDecodeError:
            raise QueryError("výstup není ASCII") from None
        blocks = parse_blocks(text, len(self.units()))
        for name, props, names in zip(self.units(), blocks, (SERVICE_PROPERTIES, TIMER_PROPERTIES)):
            required(props, names)
            # Blok patří k dotazované jednotce i u aliasu (Id je pak hlavní název).
            if name not in props["Names"].split():
                raise QueryError("výstup neodpovídá dotazu")
        return blocks

    def evaluate(self):
        """(stav, popis, details) z jediného dotazu na systemd; věrohodné bloky si uloží.

        `query_failed` rozliší přechodné selhání samotného dotazu (timeout,
        nenulový kód systemctl, nedostatek prostředků) od neplatných nebo
        rozporných údajů a od skutečného stavu jednotky.
        """
        self.query_failed = False
        try:
            blocks = self.query()
        except subprocess.TimeoutExpired:
            self.query_failed = True
            return State.UNKNOWN, f"systemd neodpověděl do {self.timeout_seconds:g} s", {}
        except QueryError as error:
            # Neplatný, cizí nebo neúplný výstup přechodné selhání není.
            self.query_failed = isinstance(error, QueryFailed)
            return State.UNKNOWN, f"stav úlohy nelze věrohodně přečíst: {error}", {}
        except OSError as error:
            # Např. nedostatek prostředků na této straně. Chybějící nebo nespustitelný
            # systemctl se opravným pokusem nezmění, proto ten se neopakuje.
            self.query_failed = not isinstance(error, (FileNotFoundError, PermissionError))
            return State.UNKNOWN, f"stav úlohy nelze přečíst ({type(error).__name__})", {}
        checked = self.clock()  # po dotazu: všechny časy systemd musí být starší
        try:
            result = self.assess(blocks, checked)
        except QueryError as error:
            return State.UNKNOWN, f"stav úlohy nelze věrohodně přečíst: {error}", {}
        self.blocks = blocks
        return result

    def assess(self, blocks, checked):
        """(stav, popis, details) z přečtených bloků k monotónnímu času `checked`, bez dotazu."""
        service = self.service_state(blocks[0], checked)
        if self.timer is None:
            return service
        timer = self.timer_state(blocks[1])
        first, second = service, timer
        if SEVERITY_ORDER.index(timer[0]) < SEVERITY_ORDER.index(service[0]):
            # Závažnější stav timeru rozhoduje; aktivní timer stav služby nikdy nezlepší.
            first, second = timer, service
        return first[0], f"{first[1]}; {second[1]}", first[2]

    def service_state(self, props, checked):
        """(stav, popis, details) zálohovací služby; aktualizuje zapamatovaný úspěch."""
        load = props["LoadState"]
        if load in MISSING:
            self.last_success = None
            return State.ERROR, f"zálohovací úloha {MISSING[load]} ({load})", {BACKUP: NO_JOB}
        if load != "loaded":
            raise QueryError(f"neznámý stav načtení jednotky {load!r}")
        active = props["ActiveState"]
        start = number(props, "ExecMainStartTimestampMonotonic") / 1e6
        end = number(props, "ExecMainExitTimestampMonotonic") / 1e6
        code, status = number(props, "ExecMainCode"), number(props, "ExecMainStatus")
        if start > checked or end > checked:
            # Čas z budoucnosti: jiný boot nebo jiné hodiny, nic se z něj nevyvozuje.
            raise QueryError("časy systemd nejsou věrohodné")
        if job_running(active, start, end):
            # Před spuštěním hlavního procesu (ExecStartPre) ukazuje systemd ještě
            # časy předchozího běhu; zapamatovaný úspěch nesmí být novější než ony.
            return self.running(end or start, checked)
        if active == "failed":
            return self.failure(props, end, checked)
        if active not in ("inactive", "active"):
            raise QueryError(f"neznámý stav jednotky {active!r}")
        if not start and not end:
            # Nikdy neběžela od startu systému, nebo ji systemd uvolnil z paměti a
            # s ní i údaje o běhu: úspěch už není doložitelný (ani zapamatovaný).
            forgotten = self.last_success is not None
            self.last_success = None
            note = "; dříve doložený úspěch se proto nepoužije" if forgotten else ""
            return (State.UNKNOWN, "systemd neuvádí žádný dokončený běh (od startu systému neběžela, "
                    f"nebo byla jednotka uvolněna z paměti){note}", {BACKUP: NO_RUN})
        if not start or not end or end < start:
            # Konec bez začátku nebo před ním: údaje o běhu nejsou úplné ani věrohodné.
            raise QueryError("neúplné údaje o posledním běhu")
        if code not in EXIT_CODES:
            # Dokončený běh s vynulovaným kódem ukončení: údaje chybí, selhání to nedokládá.
            raise QueryError(f"nevěrohodný kód ukončení ExecMainCode={code}")
        if props["Result"] == SKIPPED_RESULT:
            # ExecMain* tu popisují ještě předchozí běh a jeho Result se přepsal:
            # použije se jen dříve doložený úspěch, přeskočení novým úspěchem není.
            return self.skipped(end, checked)
        if props["Result"] == "success" and code == CLD_EXITED and status == 0:
            self.last_success = end
            age = checked - end
            state = age_state(age)
            limit = {State.OK: "", State.WARNING: f" (nad {WARNING_AGE_SECONDS // 3600} h)",
                     State.ERROR: f" (nad {ERROR_AGE_SECONDS // 3600} h)"}[state]
            return (state, f"úloha naposledy skončila úspěšně před {hours(age)}{limit}",
                    {BACKUP: SUCCEEDED if state is State.OK else OVERDUE})
        # Dokončený běh bez úspěchu (např. nenulový kód po `reset-failed`).
        return self.failure(props, end, checked, status)

    def previous_success(self, latest):
        """Zapamatovaný úspěch, pokud není novější než poslední čas, který systemd dokládá."""
        if self.last_success is not None and latest and self.last_success > latest:
            self.last_success = None  # nesouhlasí s pořadím běhů: nepoužije se
        return self.last_success

    def running(self, latest, checked):
        success = self.previous_success(latest)
        if success is None:
            return State.UNKNOWN, "úloha právě běží; předchozí úspěch není doložený", {BACKUP: RUNNING}
        age = checked - success
        state = age_state(age)
        return (state, f"úloha právě běží; poslední doložený úspěch před {hours(age)}",
                {BACKUP: RUNNING if state is State.OK else OVERDUE})

    def skipped(self, latest, checked):
        success = self.previous_success(latest)
        if success is None:
            return (State.UNKNOWN, "poslední spuštění přeskočila ExecCondition; úspěch není doložený",
                    {BACKUP: SKIPPED})
        age = checked - success
        state = age_state(age)
        return (state, f"poslední spuštění přeskočila ExecCondition; poslední doložený úspěch před {hours(age)}",
                {BACKUP: SKIPPED if state is State.OK else OVERDUE})

    def failure(self, props, end, checked, status=None):
        # U ActiveState=failed (např. selhání už v ExecStartPre) mohou ExecMain*
        # popisovat ještě předchozí běh, proto se kód hlavního procesu uvede jen
        # u dokončeného běhu, ke kterému patří.
        detail = "" if status is None else f", ExecMainStatus={status}"
        reason = f"poslední pokus selhal (Result={props['Result']}{detail})"
        success = self.previous_success(end)
        if success is None:
            return State.ERROR, f"{reason}; úspěch od startu systému není doložený", {BACKUP: FAILED}
        age = checked - success
        # Nový neúspěch při ještě použitelné záloze je WARNING, se stářím se zhoršuje.
        state = worse(State.WARNING, age_state(age))
        return state, f"{reason}; poslední doložený úspěch před {hours(age)}", {BACKUP: FAILED}

    def timer_state(self, props):
        """(stav, popis, details) plánu zálohy. Aktivní timer stav nikdy nezlepší."""
        load = props["LoadState"]
        if load in MISSING:
            return State.ERROR, f"timer zálohy {MISSING[load]} ({load})", {BACKUP: NO_TIMER}
        if load != "loaded":
            raise QueryError(f"neznámý stav načtení timeru {load!r}")
        active = props["ActiveState"]
        if active == "active":
            return State.OK, "plán zálohy je aktivní", {}
        if active in ("inactive", "failed"):
            return State.WARNING, f"timer zálohy není aktivní ({active})", {BACKUP: NO_TIMER}
        raise QueryError(f"neznámý stav timeru {active!r}")
