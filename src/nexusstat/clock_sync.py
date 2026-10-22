"""Read-only zjištění synchronizace systémových hodin a identifikátoru bootu.

Jen čte: `adjtimex` s `modes = 0` (dotaz, nic nenastavuje) a soubory v /run
a /proc. Nemění systémový čas, nespouští ani nekonfiguruje žádnou službu NTP
a nespouští podproces.

Tři stavy a jejich význam (docs/HISTORY.md 4.3):

* True  – synchronizace je zdrojem prokazatelně doložená,
* False – zdroj prokazatelně říká, že hodiny synchronizované nejsou,
* None  – stav se nepodařilo bezpečně zjistit (chybějící zdroj, chyba dotazu,
  nevěrohodná odpověď).

Neznámý stav se nikdy nevydává za synchronizovaný: zapisovač historie pak drží
data ve frontě s monotónním časem, dokud synchronizaci někdo nedoloží.
Správný kalendářní rok důkaz synchronizace není a nikde se k němu nepřihlíží;
dolní mez `history.MIN_TRUSTED_UTC` je samostatná, nezávislá ochrana zápisu.

O aktuálním stavu hodin rozhoduje jediný zdroj: dotaz jádra (`adjtimex`).
Soubor systemd-timesyncd dokládá jen *dřívější* synchronizaci v tomto bootu
(po pozdější ztrátě nemizí), takže o aktuálním stavu nevypovídá v žádném směru
a slouží pouze jako doplňující diagnostická informace do výpisu.
"""

import ctypes
import ctypes.util
import functools
import os
import re
from pathlib import Path

# Kernelová mez chyby hodin v mikrosekundách. Jádro ji po startu i po `ntp_clear()`
# drží na NTP_PHASE_LIMIT = 16 s a sníží ji jen služba, která hodiny ukázňuje
# (NTP); synchronizovaný stroj má mez řádově milisekundy a po ztrátě
# synchronizace narůstá zpátky o toleranci 500 ppm za sekundu. Stejné kritérium
# používá systemd pro NTPSynchronized (`timedatectl`), takže se výsledek
# s `timedatectl` shoduje.
MAX_ERROR_LIMIT_US = 16_000_000

# Návratový kód adjtimex: TIME_OK (0) až TIME_BAD (5). Jiná hodnota znamená, že
# odpovědi nerozumíme (jiná architektura, jiné rozložení struktury).
MAX_TIME_RESULT = 5

# Příznak STA_UNSYNC sám o sobě použít nejde: systemd-timesyncd ho nechává
# nastavený i na správně synchronizovaném stroji (ověřeno na notebooku:
# status 0x2041, mez chyby 0,02 s, `timedatectl` NTPSynchronized=yes).
# Nese se jen do popisu stavu, nikdy nerozhoduje.
STA_UNSYNC = 0x0040

# systemd-timesyncd: soubor vznikne po první úspěšné synchronizaci v tomto bootu
# (používá ho i systemd-time-wait-sync) a po pozdější ztrátě synchronizace
# nemizí. Adresář existuje, dokud služba běží.
TIMESYNCD_MARKER = "/run/systemd/timesync/synchronized"
TIMESYNCD_RUNTIME = "/run/systemd/timesync"

# boot_id odlišuje restart procesu od restartu systému (docs/HISTORY.md 4.2).
# Je to údaj jedinečný pro tento stroj a boot: patří jen do lokální databáze
# historie, nikdy do veřejné dokumentace ani do výpisu.
BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"
BOOT_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


class Timex(ctypes.Structure):
    """Linuxová `struct timex` pro dotaz adjtimex (jen čtení).

    Pole odpovídají hlavičce jádra; `__kernel_long_t` je c_long na 32 i 64 bitech.
    Chybné rozložení na neznámé architektuře odhalí kontrola věrohodnosti
    v `read_timex()`, takže se z nesmyslné odpovědi nikdy neudělá True.
    """

    _fields_ = [
        ("modes", ctypes.c_int),
        ("offset", ctypes.c_long),
        ("freq", ctypes.c_long),
        ("maxerror", ctypes.c_long),
        ("esterror", ctypes.c_long),
        ("status", ctypes.c_int),
        ("constant", ctypes.c_long),
        ("precision", ctypes.c_long),
        ("tolerance", ctypes.c_long),
        ("time_sec", ctypes.c_long),
        ("time_usec", ctypes.c_long),
        ("tick", ctypes.c_long),
        ("ppsfreq", ctypes.c_long),
        ("jitter", ctypes.c_long),
        ("shift", ctypes.c_int),
        ("stabil", ctypes.c_long),
        ("jitcnt", ctypes.c_long),
        ("calcnt", ctypes.c_long),
        ("errcnt", ctypes.c_long),
        ("stbcnt", ctypes.c_long),
        ("tai", ctypes.c_int),
        ("padding", ctypes.c_int * 11),
    ]


@functools.lru_cache(maxsize=1)
def _adjtimex():
    """Funkce adjtimex z libc, nebo None, když ji na tomto systému použít nejde."""
    try:
        library = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
        function = library.adjtimex
    except (OSError, AttributeError):
        return None
    function.argtypes = [ctypes.POINTER(Timex)]
    function.restype = ctypes.c_int
    return function


def read_timex(adjtimex=None):
    """Dotaz adjtimex: vrací (návratový kód, status, mez chyby v µs).

    `modes = 0` je čistý dotaz: jádro nic nenastaví. OSError znamená, že
    odpověď není k dispozici nebo jí nelze věřit (nedostupná libc, chyba
    volání, hodnoty mimo rozsah daný jádrem).
    """
    call = adjtimex if adjtimex is not None else _adjtimex()
    if call is None:
        raise OSError("adjtimex není v této libc dostupný")
    timex = Timex()
    result = call(ctypes.byref(timex))
    if result < 0:
        raise OSError(ctypes.get_errno(), "adjtimex selhal")
    # Jádro drží mez chyby v rozsahu 0 až NTP_PHASE_LIMIT a vrací TIME_OK až
    # TIME_BAD. Cokoli jiného je nevěrohodná odpověď (neznámé rozložení struktury).
    if not 0 <= result <= MAX_TIME_RESULT or not 0 <= timex.maxerror <= MAX_ERROR_LIMIT_US:
        raise OSError(f"nevěrohodná odpověď adjtimex (kód {result}, mez chyby {timex.maxerror})")
    return result, timex.status, timex.maxerror


def kernel_sync(read=None):
    """Synchronizace podle jádra: True, False, nebo None při chybě dotazu.

    Rozhoduje mez chyby hodin: na hranici 16 s (a výš) jádro žádnou korekci
    nedostalo, pod ní hodiny někdo ukázňuje. Ztrátu synchronizace to pozná až
    po narůstu meze zpět na 16 s, tedy řádově za hodiny, ne okamžitě.
    """
    try:
        _, _, maxerror = (read or read_timex)()
    except OSError:
        return None
    return maxerror < MAX_ERROR_LIMIT_US


def timesyncd_first_sync(marker=None, runtime=None):
    """Doklad **dřívější** první synchronizace od systemd-timesyncd.

    True = v tomto bootu se hodiny aspoň jednou srovnaly, False = služba běží
    a zatím nesynchronizovala, None = nezjištěno (služba neběží nebo soubor
    nelze přečíst).

    O **aktuálním** stavu hodin to nevypovídá ani v jednom směru: soubor po
    ztrátě synchronizace nemizí a jeho chybějící podoba zase nic neříká
    o hodinách, které srovnal někdo jiný. Proto se nepoužívá jako zdroj
    rozhodnutí (`SOURCES`) a je jen doplňující diagnostickou informací.
    """
    marker = TIMESYNCD_MARKER if marker is None else marker
    runtime = TIMESYNCD_RUNTIME if runtime is None else runtime
    try:
        if os.path.exists(marker):
            return True
        return False if os.path.isdir(runtime) else None
    except OSError:
        return None


# Jediný zdroj aktuálního stavu hodin: dotaz jádra. Soubor timesyncd tu
# záměrně není (dokládá jen dřívější synchronizaci, viz timesyncd_first_sync),
# takže bez použitelné odpovědi jádra zůstává stav neznámý a data čekají.
# Chrony se nepoužívá: na notebooku není nainstalované, takže jeho čtení nelze
# ověřit (docs/HISTORY.md 4.3, gate 12.4).
SOURCES = (("adjtimex", kernel_sync),)

UNKNOWN_SOURCE = "neznámý zdroj"


class ClockSync:
    """Stav synchronizace hodin z read-only zdrojů, postupně podle důvěryhodnosti.

    `synced()` vrací True, False nebo None a zapamatuje si, který zdroj
    rozhodl (`source`). Výjimka zdroje se bere jako neznámý stav: chyba dotazu
    nesmí zastavit měření ani se vydávat za synchronizovaný čas.
    """

    def __init__(self, sources=SOURCES):
        self.sources = tuple(sources)
        self.source = UNKNOWN_SOURCE

    def synced(self):
        for name, probe in self.sources:  # pořadí = důvěryhodnost; první jistý výsledek platí
            try:
                state = probe()
            except Exception:
                state = None  # chyba zdroje = neznámý stav, nikdy True
            if state is not None:
                self.source = name
                return state
        self.source = UNKNOWN_SOURCE
        return None


def first_sync_note():
    """Krátká doplňující informace o markeru timesyncd pro výpis, nebo prázdný text.

    Nikdy netvrdí nic o aktuálním stavu hodin; pomáhá jen rozpoznat, proč
    historie čeká na čas (hodiny se v tomto bootu ještě nikdy nesrovnaly).
    """
    state = timesyncd_first_sync()
    if state is None:
        return ""
    return ("systemd-timesyncd: první synchronizace v tomto bootu doložena" if state
            else "systemd-timesyncd: první synchronizace v tomto bootu zatím nedoložena")


def read_boot_id(path=None):
    """boot_id Linuxu (jen čtení); None při chybějícím, nečitelném nebo neplatném údaji.

    Neplatný údaj se nedoplňuje ani nenahrazuje: `None` znamená, že restart
    systému od restartu procesu odlišit nejde.
    """
    try:
        value = Path(BOOT_ID_PATH if path is None else path).read_text(encoding="ascii").strip()
    except (OSError, ValueError):
        return None
    return value if BOOT_ID.fullmatch(value) else None
