"""Živý grafický simulátor OLED 128 × 64 pro vývojový počítač (tkinter).

Spuštění:
  PYTHONPATH=src python3 -m nexusstat.preview [--scenario NÁZEV] [--scale 4|6] [--config SOUBOR]

Obraz kreslí výhradně render.render_screen(); simulátor jen řídí čas,
tlačítko, scénáře a zvětšení. Nepřistupuje k hardwaru displeje, GPIO ani síti.
Stavová logika (PreviewSession) nepotřebuje tkinter ani grafický displej,
takže ji testy ověřují bez X serveru; tkinter se načte až při otevření okna.
"""

import argparse
import base64
import importlib.util
import io
import signal
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .alarms import AlarmEvaluator
from .config import Config, ConfigError, load_config
from .core import CheckResult, Measurement, State, active_problems
from .oled import Mode, OledController, status_page_count
from .run import failed_results
from .services import ANIMEDB_ID, BACKUP_ID, NAS_ID, NEXUS_SERVICES_ID, complete_service_results
from .system_monitor import CELSIUS, CPU_TEMP_ID, CPU_USAGE_ID, DISK_ROOT_ID, MEMORY_ID, PERCENT, SystemMonitor

# Zrychlení simulovaného času. LIVE běží vždy 1×, protože měří skutečný čas.
SPEEDS = (1, 5, 10)
# Celočíselné zvětšení, aby každý pixel OLED zůstal ostrým čtvercem.
SCALES = (4, 6)
# Obnova okna. Při 10× trvá stránka kolotoče 1 s, takže 0,1 s stačí.
TICK_MS = 100

LIVE = "live"


# --- Fiktivní scénáře -------------------------------------------------------
# Každý scénář dostane funkci sim(id, stav, popis, hodnota=None, stale_after=None)
# a vrátí seznam výsledků. Běžná položka je čerstvé měření v okamžiku obnovy
# (obnovuje se po intervalu kolektoru jako skutečná data). `stale_after=N`
# znamená: změřeno jen při spuštění scénáře a dál nepřichází, po N s tedy vyprší.
# Systémové hodnoty mají číselnou hodnotu jako skutečný kolektor: displej
# (oled_text) ukazuje ji, ne český popis.

def pct(value):
    return Measurement(value, PERCENT)


def deg(value):
    return Measurement(value, CELSIUS)


def scenario_ok(sim):
    return [
        sim(CPU_USAGE_ID, State.INFO, "CPU 12 %", pct(12)),
        sim(CPU_TEMP_ID, State.INFO, "Teplota CPU 46 °C", deg(46)),
        sim(MEMORY_ID, State.INFO, "RAM 31 % z 7,9 GiB", pct(31)),
        sim(DISK_ROOT_ID, State.INFO, "Disk / 42 % z 117 GiB", pct(42)),
        sim(NEXUS_SERVICES_ID, State.OK, "Služby Nexusu běží"),
        sim(NAS_ID, State.OK, "NAS dostupný"),
        sim(BACKUP_ID, State.OK, "Záloha v pořádku"),
        sim(ANIMEDB_ID, State.NA, "AnimeDB není nasazena"),
    ]


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
    ]


def scenario_unconnected(sim):
    # Fiktivní systém, služby bez výsledku: doplnění je ukáže jako dnes ve
    # skutečnosti (NEXUS, NAS, BACKUP UNKNOWN, AnimeDB N/A).
    return scenario_ok(sim)[:4]


# Pořadí odpovídá nabídce v okně.
SCENARIOS = {
    "ok": ("1 Vše v pořádku", scenario_ok),
    "warning": ("2 WARNING: teplota", scenario_warning),
    "error": ("3 ERROR a CRITICAL", scenario_error),
    "unknown": ("4 UNKNOWN: stará a chybějící data", scenario_unknown),
    "many": ("5 Více problémů (2 stránky STATUS)", scenario_many),
    "unconnected": ("6 Nenapojené služby (dnešní stav)", scenario_unconnected),
    LIVE: ("LIVE: měření tohoto notebooku", None),
}


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
                 monitor_factory=SystemMonitor):
        self.config = config or Config()
        self.clock = clock or SessionClock()
        self.utc_clock = utc_clock or (lambda: datetime.now(timezone.utc))
        self.wall_clock = wall_clock
        self.monitor_factory = monitor_factory
        self.validity = self.config.collector.validity_seconds
        self.interval = self.config.collector.interval_seconds
        # Převod simulovaného času na UTC a místní čas pro výsledky a hodiny na displeji.
        self._time_start = self.clock.now()
        self._utc_start = self.utc_clock()
        self._wall_start = self.wall_clock()
        self.controller = None
        self.set_scenario(scenario)

    @property
    def is_live(self):
        return self.scenario == LIVE

    def set_scenario(self, name):
        """Přepne scénář. Data, paměť alarmů i okno kolotoče začnou znovu.

        Rozsvícený displej zůstane rozsvícený, ale od stránky NEXUS s novým
        oknem; zhasnutý zůstane zhasnutý. Nic se nemíchá s předchozím scénářem.
        """
        if name not in SCENARIOS:
            raise ValueError(f"neznámý scénář {name!r}")
        awake = self.controller is not None and self.controller.mode is not Mode.SLEEP
        self.scenario = name
        if self.is_live:
            # Skutečné měření běží ve skutečném čase.
            self.clock.set_speed(1)
            self.monitor = self.monitor_factory(validity_seconds=self.validity)
            self.evaluator = AlarmEvaluator(self.config.alarms)
        else:
            self.monitor = self.evaluator = None
        now = self.clock.now()
        self.started_at = now
        self.controller = OledController(self.config.oled)
        if awake:
            self.controller.press(now, 0)
        self._refresh(now)

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
        def sim(check_id, state, summary, value=None, stale_after=None):
            at, valid = (self.started_at, stale_after) if stale_after else (now, self.validity)
            return CheckResult(check_id, state, summary, self.utc_at(at), at + valid, simulated=True,
                               measurement=value)

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
        self.results = complete_service_results(measured, observed_utc, now + self.validity)
        self.next_refresh = now + self.interval

    def _refresh_if_due(self, now):
        if now >= self.next_refresh:
            self._refresh(now)

    def press(self):
        """Krátký stisk tlačítka: skutečná logika OledController."""
        now = self.clock.now()
        self._refresh_if_due(now)
        self.controller.press(now, len(active_problems(self.results, now)))

    def tick(self):
        """Volá se pravidelně z okna: obnoví data, uplatní časové limity a vrátí obraz 128 × 64."""
        now = self.clock.now()
        self._refresh_if_due(now)
        self.controller.update(now)
        # Pillow až tady: stavová logika a její testy bez něj fungují.
        from .render import render_screen
        return render_screen(self.controller, now, self.results, self.wall_time(now))

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
            return ("LIVE: skutečná read-only měření tohoto NOTEBOOKU, ne Nexusu. "
                    "Služby bez kolektoru UNKNOWN, AnimeDB N/A. Čas 1×.")
        return f"SIMULACE: fiktivní data (značka SIM), čas {self.clock.speed}×."


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
    root.title("NexusSTAT – simulátor OLED 128 × 64 (notebook)")
    root.configure(padx=10, pady=10)

    # Plocha OLED: jen obraz z render_screen(), nic dalšího se do ní nekreslí.
    display = tk.Label(root, bg="black", borderwidth=4, relief="sunken")
    display.pack()

    mode_text = tk.StringVar()
    source_text = tk.StringVar()
    tk.Label(root, textvariable=mode_text, font=("TkDefaultFont", 11, "bold")).pack(anchor="w", pady=(8, 0))
    tk.Label(root, textvariable=source_text, wraplength=128 * scale, justify="left").pack(anchor="w")

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

    state = {"after_id": None, "frame": None, "photo": None}

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

    def refresh(schedule=True):
        try:
            frame = session.tick()
            # Nový obrázek jen při změně, aby okno zbytečně nevytěžovalo procesor.
            if frame.tobytes() != state["frame"]:
                state["frame"] = frame.tobytes()
                state["photo"] = tk.PhotoImage(data=png_data(scale_frame(frame, scale)))
                display.configure(image=state["photo"])
            mode_text.set(session.describe())
            source_text.set(session.source_text())
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
    parser.add_argument("--config", type=Path, help="TOML se sekcemi [oled], [collector] a [alarms]")
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
