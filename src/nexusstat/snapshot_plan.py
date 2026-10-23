"""Denní plánování snímků historie (M3.4b-1, docs/HISTORY.md 9.2).

Čistá časová logika bez I/O, bez vlákna a bez aktivního dotazování: o snímek
žádá měřicí smyčka při svém běžném tiku (`history_link.plan_snapshot`), takže
nic nového nečeká ani se nebudí. Plán se vyhodnocuje jen z doloženě
synchronizovaného času; bez něj se nerozhoduje vůbec (vymyšlené UTC by dalo
vymyšlené jméno generace).

Čas zálohovací úlohy Nexusu tu není a nebude: hodina se zadává v konfiguraci
(`[history.snapshot] daily_time`, `timezone`) a provozní odstup od Resticu
(přibližně 30 minut před ním) se ověří až při nasazení (M7).
"""

from datetime import datetime, timedelta, timezone

MAX_NEXT_DAYS = 400  # ochrana proti nekonečnému hledání v exotickém pásmu


class DailyPlan:
    """Jeden snímek denně v zadaný místní čas (hodina a minuta) daného pásma.

    Přechody letního a zimního času se řeší výhradně pravidly pásma:

    * V den jarního posunu zadaný čas existovat nemusí (02:00 i 02:30
      v Evropě/Praze). Použije se první existující okamžik po něm (03:00,
      respektive 03:30 místního času), takže se den nikdy nevynechá.
    * V den podzimního posunu existuje čas dvakrát. Použije se **první**
      výskyt; druhý se už nespustí, protože plán dne je tím vyřízený.
    """

    __slots__ = ("hour", "minute", "zone")

    def __init__(self, hour, minute, zone):
        for name, value, limit in (("hodina", hour, 23), ("minuta", minute, 59)):
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= limit:
                raise ValueError(f"{name} plánu musí být celé číslo 0–{limit}, ne {value!r}")
        if zone is None:
            raise ValueError("plán snímků potřebuje výslovné časové pásmo")
        self.hour, self.minute, self.zone = hour, minute, zone

    def __repr__(self):
        return f"DailyPlan({self.hour:02d}:{self.minute:02d} {self.zone})"

    def local_date(self, moment):
        """Místní datum, do kterého okamžik (UTC) patří."""
        return moment.astimezone(self.zone).date()

    def scheduled_for(self, day):
        """Plánovaný okamžik (UTC) pro místní datum `day`."""
        local = datetime(day.year, day.month, day.day, self.hour, self.minute, tzinfo=self.zone)
        return local.astimezone(timezone.utc)

    def due_at(self, moment):
        """Plánovaný okamžik dne, do kterého `moment` (UTC) místně patří."""
        return self.scheduled_for(self.local_date(moment))

    def next_after(self, moment):
        """První plánovaný okamžik po `moment` (UTC).

        Hledá se po dnech, aby posun pásma (přechod času, změna pravidel) nikdy
        nedal čas v minulosti ani se nezacyklil.
        """
        day = self.local_date(moment)
        for step in range(MAX_NEXT_DAYS):
            candidate = self.scheduled_for(day + timedelta(days=step))
            if candidate > moment:
                return candidate
        raise ValueError(f"v pásmu {self.zone} nelze najít další plánovaný čas po {moment}")


class SnapshotPlanner:
    """Rozhoduje, kdy požádat vlákno zapisovače o denní snímek.

    Pravidla (docs/HISTORY.md 9.2):

    * Na jeden plánovaný okamžik nejvýš **jeden automatický pokus** v tomto
      procesu. Plán je vyřízený, jakmile pokus proběhl, ať uspěl, nebo ne:
      opakování po selhání by při trvalé závadě vyrábělo generace v řadě.
    * Dohání se jen **dnešní** zmeškaný okamžik. Starší dny se nedohánějí:
      snímek zachycuje dnešní stav databáze, zpětně ho vytvořit nelze.
    * Úspěšný snímek novější než plánovaný okamžik plán vyřizuje i **po
      restartu** procesu, protože zapisovač čte čas poslední prokazatelně
      platné generace z jejího ověřeného záznamu. Platí to i pro snímek
      vyžádaný ručně: druhý snímek téhož dne by nic nepřinesl.
    * Selhaný pokus se po restartu procesu smí zkusit znovu (nejvýš jednou za
      běh), protože restart bývá právě tím zásahem, který závadu odstranil.
    """

    __slots__ = ("plan", "attempted", "requests")

    def __init__(self, plan):
        self.plan = plan
        self.attempted = None  # plánovaný okamžik (UTC) posledního pokusu v tomto procesu
        self.requests = 0

    def due(self, now_utc, snapshot_status):
        """True, když se teď má požádat o snímek pro dnešní plánovaný okamžik."""
        scheduled = self.plan.due_at(now_utc)
        if now_utc < scheduled:
            return False  # dnešní čas ještě nenastal
        if self.attempted is not None and self.attempted >= scheduled:
            return False  # tento plán už měl svůj pokus
        if snapshot_status.requested or snapshot_status.running:
            return False  # jeden snímek už čeká ve frontě údržby nebo se právě staví
        if not snapshot_status.scanned:
            return False  # zapisovač ještě nezjistil, jaké generace existují
        if snapshot_status.utc is not None and snapshot_status.utc >= int(scheduled.timestamp()):
            return False  # pro tento plán snímek už existuje (platí i po restartu)
        return True

    def note_requested(self, now_utc):
        """Zaznamená, že pokus pro dnešní plán proběhl (úspěch se pozná jinde)."""
        self.attempted = self.plan.due_at(now_utc)
        self.requests += 1

    def next_run(self, now_utc, snapshot_status=None):
        """Čas příštího plánovaného snímku (UTC); zmeškaný dnešní termín vrací sám sebe."""
        scheduled = self.plan.due_at(now_utc)
        if now_utc >= scheduled and (self.attempted is None or self.attempted < scheduled):
            if snapshot_status is None or snapshot_status.utc is None \
                    or snapshot_status.utc < int(scheduled.timestamp()):
                return scheduled
        return self.plan.next_after(now_utc)
