"""Příkazy správce nad databází historie (M3.4b-2, docs/HISTORY.md 9.6, 9.7).

    PYTHONPATH=src python3 -m nexusstat.history info SOUBOR
    PYTHONPATH=src python3 -m nexusstat.history adopt SOUBOR --output NOVÝ
    PYTHONPATH=src python3 -m nexusstat.history generations ADRESÁŘ

`info` a `generations` jen čtou: nic nevytvářejí, nemění ani nečekpointují.
`adopt` je jediný příkaz, který zapisuje, a vždy jen do **nového** souboru bez
přepsání cíle; nikdy se nespouští automaticky a nikdy nepřebírá databázi, kterou
právě zapisuje živý proces. Mazání snímkových generací žádný příkaz neumí.

Návratový kód: 0 v pořádku, 1 nález nebo odmítnutí, 2 chyba v argumentech.
"""

import argparse
from datetime import datetime, timezone

from . import history, snapshot

MISSING = "—"


def moment(value):
    """Čas UTC pro člověka; None nebo nesmysl jako pomlčka."""
    if value is None:
        return MISSING
    try:
        return f"{datetime.fromtimestamp(int(value), timezone.utc):%Y-%m-%d %H:%M:%S} UTC"
    except (TypeError, ValueError, OverflowError, OSError):
        return f"{value!r} (neplatný čas)"


def _yes_no(value):
    return MISSING if value is None else ("ano" if value else "ne")


def _mapping(values):
    return ", ".join(f"{key} = {value}" for key, value in sorted(values.items())) or MISSING


def info_lines(info):
    """Výpis příkazu `info`: co se o souboru dá zjistit jen čtením."""
    lines = [f"Soubor: {info.path}",
             f"  existuje: {_yes_no(info.exists)}"
             + (f", velikost {info.bytes} B" if info.bytes is not None else "")]
    if not info.exists and info.bytes is None:
        lines.append(f"  zjištění: {'; '.join(info.problems) or MISSING}")
        return lines
    lines += [
        f"  role: {info.role or MISSING}   schéma: {info.schema_version if info.schema_version is not None else MISSING}"
        f" (odpovídá v{history.SCHEMA_VERSION}: {_yes_no(info.schema_matches)})",
        # Samotné host_id se nikdy netiskne, jen jeho nevratný otisk.
        f"  stroj: otisk {info.fingerprint or MISSING}, tento stroj {info.local_fingerprint or MISSING}"
        f", shoda: {_yes_no(info.same_machine)}",
        f"  vytvořeno: {moment(info.created_utc)}",
        f"  značka snímku (neevidovaná obnova): {moment(info.snapshot_utc)}",
        f"  evidence obnovy: {_mapping(info.restored)}",
        f"  evidence převzetí: {_mapping(info.adopted)}",
        f"  pomocné soubory: {', '.join(info.companions) or 'žádné'}",
        f"  journal_mode: {info.journal_mode or MISSING}   integrity_check: {info.integrity or 'neprověřeno'}"
        f"   cizí klíče: {MISSING if info.foreign_keys is None else info.foreign_keys}",
    ]
    if info.last_run:
        run = info.last_run
        lines.append(f"  poslední běh: {run['id']}, start {moment(run['started_utc'])}, "
                     f"konec {moment(run['stopped_utc'])}, čistý: {_yes_no(run['clean_stop'])}, "
                     f"poslední zápis {moment(run['last_write_utc'])}, dávek {run['last_batch']}, "
                     f"verze {run['app_version']}")
    else:
        lines.append(f"  poslední běh: {MISSING}")
    if info.counts:
        counted = ", ".join(f"{table} {info.counts[table]}" for table in
                            ("sample", "agg_5m", "agg_1h", "check_change", "event", "metric", "run")
                            if table in info.counts)
        lines.append(f"  počty: {counted}")
    # Neprověřená integrita se nikdy nevydává za ověřenou ani v závěru.
    caveat = "" if info.integrity_checked else " (integrita neprověřena)"
    lines += [f"  vhodná k otevření na tomto stroji: {_yes_no(info.openable)}{caveat}",
              f"  vyžaduje ruční převzetí (adopt): {_yes_no(info.needs_adopt)}"]
    if info.problems:
        lines.append("  zjištění:")
        lines += [f"    - {problem}" for problem in info.problems]
    return lines


def adopt_lines(report):
    """Výpis příkazu `adopt`: co se změnilo a čím je to doložené."""
    return [
        f"Převzato: {report.source} → {report.target}",
        f"  role: {report.role} (produkční role se nepřebírá)",
        f"  stroj: otisk {report.from_fingerprint} → {report.to_fingerprint}",
        f"  čas převzetí: {moment(report.adopted_utc)}",
        f"  původ ze snímku: {moment(report.snapshot_utc)}",
        f"  událostí history.adopted v nové databázi: {report.events}",
        f"  nezávislé ověření nové databáze: {_yes_no(report.verified)}",
        "  vstupní soubor zůstal beze změny; cíl se nikdy nepřepisuje.",
        "  Obnovu ze snímku zaeviduje až první start zapisovače (history.restored).",
    ]


def generations_lines(plan):
    """Výpis příkazu `generations`: stav generací a dry-run plán retence."""
    lines = [f"Generace snímků: {len(plan.generations)}",
             f"  návrh retence: nejvýš {plan.keep} generací, nic mladšího než "
             f"{plan.min_keep_seconds // 3600} h"]
    for state in plan.generations:
        mark = "platná" if state.valid else "NEOVĚŘITELNÁ"
        reason = plan.protected.get(state.name)
        lines.append(f"  {state.name}: {mark}, čas {moment(state.snapshot_utc)}, "
                     f"{state.bytes if state.bytes is not None else MISSING} B"
                     + (f", chráněná ({reason})" if reason else ", kandidát")
                     + (f" — {state.problem}" if state.problem else ""))
    lines += [f"  poslední platná generace: {plan.newest_valid or MISSING}",
              f"  kandidáti k pozdějšímu smazání: {', '.join(plan.candidates) or 'žádní'}",
              f"  skutečně ke smazání nyní: {', '.join(plan.deletable) or 'nic'}"]
    lines += [f"  blokuje: {blocker}" for blocker in plan.blockers]
    return lines


def _parser():
    parser = argparse.ArgumentParser(
        prog="python3 -m nexusstat.history",
        description="Read-only diagnostika databáze historie a ruční převzetí obnovené databáze.")
    commands = parser.add_subparsers(dest="command", required=True)
    read = commands.add_parser("info", help="jen přečte stav databáze (nic nemění)")
    read.add_argument("path", help="soubor databáze historie")
    read.add_argument("--no-integrity", action="store_true",
                      help="vynechá integrity_check a foreign_key_check (rychlejší u velkého souboru); "
                           "výpis to pak výslovně uvede a o poškození netvrdí nic")
    read.add_argument("--immutable", action="store_true",
                      help="tvrzení, že soubor se nemůže měnit (např. generace snímku na read-only "
                           "médiu): SQLite pak nezamyká, změnu nepozná a čekající rollback ignoruje. "
                           "Nikdy se nedovozuje z absence -wal")
    take = commands.add_parser("adopt", help="ručně převezme obnovenou databázi na tomto stroji")
    take.add_argument("path", help="obnovená databáze (zůstane beze změny)")
    take.add_argument("--output", required=True, help="nový soubor; existující cíl se nikdy nepřepíše")
    listing = commands.add_parser("generations", help="jen přečte generace snímků a navrhne retenci")
    listing.add_argument("path", help="adresář zveřejněných generací")
    listing.add_argument("--keep", type=int, default=snapshot.KEEP_GENERATIONS,
                         help=f"kolik nejnovějších generací se drží (návrh: {snapshot.KEEP_GENERATIONS})")
    listing.add_argument("--min-hours", type=int, default=snapshot.MIN_KEEP_SECONDS // 3600,
                         help="minimální doba uchování v hodinách")
    return parser


def main(argv=None, output=print, now_utc=None):
    """Spustí příkaz; vrací návratový kód procesu (nikdy nevyhodí HistoryError)."""
    args = _parser().parse_args(argv)
    try:
        if args.command == "info":
            info = history.database_info(args.path, integrity=not args.no_integrity,
                                         immutable=args.immutable)
            for line in info_lines(info):
                output(line)
            return 0 if info.openable else 1
        if args.command == "adopt":
            report = history.adopt_database(args.path, args.output, now_utc=now_utc)
            for line in adopt_lines(report):
                output(line)
            return 0 if report.verified else 1
        plan = snapshot.retention_candidates(args.path, keep=max(0, args.keep),
                                             min_keep_seconds=max(0, args.min_hours) * 3600,
                                             now_utc=int(datetime.now(timezone.utc).timestamp())
                                             if now_utc is None else now_utc)
        for line in generations_lines(plan):
            output(line)
        return 1 if any(blocker.startswith("path:") for blocker in plan.blockers) else 0
    except (history.HistoryError, snapshot.SnapshotError) as error:
        output(f"Odmítnuto ({error.reason}): {error}")
        return 1
    except OSError as error:
        output(f"Odmítnuto (io): {error}")
        return 1
