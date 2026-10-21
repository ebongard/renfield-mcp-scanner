"""SANE wrapper: device discovery, live sensors, and batch scanning.

Everything here is driven through the `scanimage` CLI rather than a SANE
binding, because the CLI is what is actually installed on an operator host and
its behaviour is what the measurements in pipeline.py were taken against.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import shutil
import weakref
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("renfield-mcp-scanner.sane")

# A finished stack ENDS with this: it is the normal terminator, not a fault.
_TERMINATOR = "out of documents"
# Emitted on EVERY run when an option is quantised to the device's step size
# ("rounded value of page-height from 297 to 296.994"). Treating any
# "scanimage:" line as an error would fault every single scan.
_BENIGN = re.compile(r"out of documents|rounded value")


#: Zeitgrenze der beiden BILLIGEN Abfragen: Gerätesuche (`scanimage -f`) und
#: Sensorgruppe (`scanimage -A`). Als benannte Konstante, weil der Knopfwächter
#: sie in seine Lebendigkeitsgrenze EINRECHNET (`button.LIVENESS_WORST_GAP`):
#: eine hängende Abfrage zieht die Lücke zwischen zwei Durchläufen um bis zu
#: diese Spanne, ohne dass etwas kaputt ist. Stünde die Zahl an beiden Stellen
#: einzeln, liefe sie beim nächsten Umbau auseinander.
PROBE_TIMEOUT = 60.0

#: 🛑 Die Sensoren, von denen das VERHALTEN des Wächters abhängt. Fehlt einer,
#: ist die Sensorgruppe für den blauen Knopf unbrauchbar — und zwar STILL:
#:
#:   * `scan` fehlt        → `yes("scan")` liest `raw.get("scan","no")` → der
#:                           Knopfdruck ist für immer unsichtbar.
#:   * `page-loaded` fehlt → der Takt bleibt bei `IDLE_INTERVAL` (1,0 s) statt
#:                           `ARMED_INTERVAL` (0,2 s), und der ~200-ms-Impuls
#:                           des Knopfes geht verloren.
#:
#: Bis 2026-10-21 warf `read_sensors` nur bei VOLLSTÄNDIG leerem `raw`. Eine
#: Teilmenge — etwa nach einer `scanimage`-Fassung mit leicht geändertem
#: Optionsformat, auf die die Regex nur noch teilweise passt — ergab `raw`
#: nicht leer, keinen Wurf, `ready=True`, `device_readable=True` und einen
#: toten Knopf ohne eine einzige Protokollzeile. Genau der Ausfall, der neun
#: Tage gekostet hat, nur durch eine engere Tür. Im Review zu #9 gefunden.
#:
#: Die übrigen Felder (`cover-open`, `error-code`, `power-save`,
#: `double-feed`) tragen den Knopf nicht und dürfen fehlen; ihre Vorgaben
#: verfälschen höchstens eine Anzeige, nicht das Verhalten.
REQUIRED_SENSORS = ("scan", "page-loaded")

#: Wie viele Scans halten das Gerät gerade? EIN Zähler, DIE eine Quelle für
#: „der Einzug ist belegt" — gelesen vom Knopfwächter (er überspringt dann den
#: Sensorabruf) und von `watcher_health` (es nennt den Zusammenstoß dann nicht
#: „verklemmt").
#:
#: 🛑 Warum HIER und nicht am Auftrag. Die erste Fassung fragte
#: `JobManager._active`, also „läuft ein Auftrag". Das umfasst auch Zusammenbau,
#: OCR und Klassifizierung — und `img2pdf`/`ocrmypdf` laufen über
#: `asyncio.to_thread` OHNE Zeitgrenze und unabbrechbar (`pdf.py`). Eine
#: hängende OCR hätte den Auftrag für immer „laufend" gehalten, die Probe
#: dauerhaft verstummt und die Zeitmarke grün — der Neun-Tage-Ausfall durch
#: eine neue Tür. Im Review zu #10 gefunden.
#:
#: Das Gerät wird NUR während `scan_batch` gehalten, und das ist durch sein
#: eigenes `timeout=1800` begrenzt: unbegrenztes Schweigen ist damit
#: strukturell unmöglich, ohne eine gewählte Frist. Nach dem Scan ist der
#: Einzug frei, und der Wächter darf wieder lesen — der Knopf ist während der
#: OCR also bedienbar statt unnötig taub.
#:
#: Ein Zähler, kein Schalter: ein Schalter, den zwei Pfade setzen und einer
#: zurücknimmt, bleibt irgendwann hängen.
_DEVICE_HELD = 0


def device_held() -> bool:
    """Hält ein laufender Scan gerade den Einzug?"""
    return _DEVICE_HELD > 0


class ScannerError(RuntimeError):
    """A scanner fault. Carries the device's own message."""


class ScannerBusyError(ScannerError):
    """The device is already scanning."""


class DeviceContendedError(ScannerError):
    """Eine andere Probe hielt das Gerät länger als die Wartegrenze.

    🛑 Eigene Klasse, damit ein ZUSAMMENSTOSS in der Auswertung nicht wie ein
    Verklemmen aussieht. Fünf Aufrufer lösen `find_device` unabhängig aus
    (`scan.py:35`, `button.py:339`, `tools.py:31`, `tools.py:61`,
    `tools.py:135`); bis 2026-10-12 ohne jede Serialisierung. Zwei davon laufen
    regelmässig gegeneinander, und das erklärt den Befund „`scanner_status`
    scheitert, während der Wächter im selben Prozess erfolgreich liest": nicht
    zwei Sichten auf einen Zustand, sondern zwei Öffner, von denen einer
    verliert.
    """


#: Serialisiert die BILLIGEN Geräteproben gegeneinander.
#:
#: 🛑 NUR die Proben — `scan_batch` läuft ausdrücklich OHNE dieses Schloss.
#: Gegengerechnet am 2026-10-12: ein Scan darf 1800 s dauern, die
#: Lebendigkeitsgrenze des Wächters liegt bei `LIVENESS_STALE_AFTER` = 370 s
#: (2 × (60 + 2×60 + 5)). Würde das Schloss über den Scan gehalten, käme der
#: Wächter 1800 s nicht durch seinen Durchlauf und die Aufsicht erklärte den
#: blauen Knopf bei JEDEM langen Scan für tot — ein Fehlalarm auf den
#: Normalfall, also genau das, was `LIVENESS_FACTOR` verhindern soll. Die
#: Probe-gegen-Scan-Serialisierung braucht deshalb eine Änderung AM WÄCHTER
#: (nicht anstehen, sondern den Takt überspringen) und gehört nicht in diese
#: Stufe.
#: 🛑 Je EREIGNISSCHLEIFE, nicht je Modul. `asyncio.Lock()` bindet sich beim
#: ersten Gebrauch an die laufende Schleife und wirft danach in jeder anderen
#: („is bound to a different event loop"). Ein Schloss auf Modulebene lief im
#: Dienst durch — er hat genau eine Schleife — und riss im zweiten
#: asynchronen Test. Ein Riegel, der nur in der Produktion hält und im Test
#: wirft, ist kein Riegel: er ist unprüfbar. Schwache Schlüssel, damit
#: beendete Schleifen nicht hängenbleiben.
_PROBE_LOCKS: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, asyncio.Lock
] = weakref.WeakKeyDictionary()


def _probe_lock() -> asyncio.Lock:
    """Das Schloss der laufenden Schleife.

    🛑 **Reichweite: EIN Prozess, EINE Ereignisschleife.** Das ist kein
    Versehen, aber auch keine Nebensache — ein Schloss je Schleife ist der
    Fehler des modulweiten Schlosses, nur umgedreht: es serialisiert
    innerhalb einer Schleife und schweigt über alles darüber hinaus. Liefe je
    ein Aufrufer in einem eigenen Thread mit eigener Schleife, bekäme jeder
    sein eigenes Schloss und die Serialisierung wäre **still** weg — ohne
    Ausnahme, ohne Zeile. Heute laufen alle fünf Aufrufer in derselben
    Schleife desselben Dienstes; wer das ändert, muss hierher zurück.

    🛑 **Den sechsten Aufrufer erreicht der Riegel grundsätzlich nicht:** ein
    `scanimage` von Hand, aus einer Shell, aus Chromes Geräteaufzählung. Das
    ist eine GRENZE, keine Lücke — sie steht hier, damit sie später niemand
    für eine Zusage hält.
    """
    loop = asyncio.get_running_loop()
    lock = _PROBE_LOCKS.get(loop)
    if lock is None:
        lock = _PROBE_LOCKS[loop] = asyncio.Lock()
    return lock


@dataclass
class ScanResult:
    pages: list[Path]
    log: str
    faulted: bool = False
    fault_detail: str = ""


@dataclass
class Sensors:
    """Live hardware state. Honest — read from the device, never inferred."""

    page_loaded: bool = False
    cover_open: bool = False
    power_save: bool = False
    double_feed: bool = False
    scan_button: bool = False
    error_code: int = 0
    raw: dict[str, str] = field(default_factory=dict)

    @property
    def ready(self) -> bool:
        """🛑 Leere Sensoren sind NICHT „bereit".

        Bis 2026-10-12 verwarf `read_sensors` den Exit-Status, und ein
        fehlgeschlagener Abruf ergab `raw={}` → alle Sensoren `False` →
        `error_code=0` → **`ready is True`**. Ein verklemmtes Gerät meldete
        damit „bereit, kein Papier", ohne eine einzige Protokollzeile. Das
        war nicht nur ein stiller Fehlschlag, es machte die Ursache des
        Verklemmens UNBEOBACHTBAR: `device` blieb gesetzt, `errors` blieb 0,
        und die Gerätesuche lief nie wieder — ein Ausfall im Dauerbetrieb
        wurde erst beim nächsten Neustart laut, der dann wie die Ursache
        aussah. `read_sensors` wirft jetzt; dieser Riegel hier deckt jede
        andere Herkunft eines `Sensors`-Satzes ab.
        """
        if not self.raw:
            return False
        return not self.cover_open and self.error_code == 0


async def _run(*args: str, timeout: float = 600.0) -> tuple[int, str]:
    """Run scanimage, returning (exit status, combined output)."""
    if not shutil.which("scanimage"):
        raise ScannerError("scanimage not found — install sane-backends")
    proc = await asyncio.create_subprocess_exec(
        "scanimage", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        _reap(proc)
        raise ScannerError(f"scanimage timed out after {timeout}s") from None
    except BaseException:
        # 🛑 JEDE andere Beendigung raeumt ebenfalls auf — vor allem
        # `CancelledError`. Bis 2026-10-12 toetete nur die Zeitablauf-Bahn den
        # Prozess; wird die wartende Aufgabe abgebrochen (Dienstende, ein
        # aeusseres Zeitlimit, ein abgebrochener Auftrag), kehrte `_run` zurueck
        # und liess ein `scanimage` zurueck, das das USB-Geraet weiter hielt.
        # Der naechste Aufruf fand dann "no Fujitsu scanner found" — ein
        # Fehlerbild, das wie ein Geraetedefekt aussieht und von uns stammt.
        _reap(proc)
        raise
    return proc.returncode or 0, out.decode("utf-8", "replace")


def _reap(proc: asyncio.subprocess.Process) -> None:
    """Toetet einen noch laufenden `scanimage` SYNCHRON.

    🛑 Kein `await`. Im Abbruchfall ist die Aufgabe bereits am Abbrechen, und
    ein `await` koennte sofort wieder mit `CancelledError` herausfahren — der
    Prozess blieb dann trotz Aufraeumens stehen. `kill()` ist synchron und ist
    der entscheidende Teil: es gibt das USB-Geraet frei. Das Einsammeln des
    Zombies uebernimmt die Kinderaufsicht der Ereignisschleife.
    """
    if proc.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        proc.kill()


async def _probe(*args: str, timeout: float) -> tuple[int, str]:
    """`_run` fuer die billigen Proben — serialisiert, mit Wartegrenze.

    Die Wartegrenze ist `timeout`: laenger zu warten als die Probe selbst
    dauern darf, bringt nichts und verlaengerte nur die Luecke, die der
    Waechter in seine Lebendigkeitsgrenze einrechnet.
    """
    lock = _probe_lock()
    try:
        await asyncio.wait_for(lock.acquire(), timeout=timeout)
    except asyncio.TimeoutError:
        raise DeviceContendedError(
            f"eine andere Geraeteprobe hielt das Geraet laenger als {timeout:.0f}s"
        ) from None
    try:
        return await _run(*args, timeout=timeout)
    finally:
        lock.release()


def _last_line(out: str) -> str:
    """Letzte nichtleere Zeile — die Meldung des Geraets, nicht der ganze Strom."""
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    return lines[-1] if lines else "(keine Ausgabe)"


async def find_device(preferred: str = "") -> str:
    """Resolve the scanner device name. Empty preference -> first Fujitsu."""
    if preferred:
        return preferred
    _, out = await _probe("-f", "%d%n", timeout=PROBE_TIMEOUT)
    for line in out.splitlines():
        if "fujitsu" in line.lower():
            return line.strip()
    raise ScannerError("no Fujitsu scanner found (is the vendor software holding it?)")


async def read_sensors(device: str) -> Sensors:
    """Read the live sensor group. Cheap, and the basis of scanner_status."""
    status, out = await _probe("-d", device, "-A", timeout=PROBE_TIMEOUT)
    # 🛑 ZWEI verschiedene Fehlschlaege, getrennt gemeldet. Deckte einer den
    # anderen mit ab, waere hinterher nicht erkennbar, welcher gefeuert hat.
    if status != 0:
        raise ScannerError(
            f"Sensorabruf fehlgeschlagen (scanimage endete mit {status}): "
            f"{_last_line(out)}"
        )
    raw: dict[str, str] = {}
    for m in re.finditer(r"^\s+--([a-z0-9-]+)\[?=?.*?\[(\w+)\]\s*\[hardware\]", out, re.M):
        raw[m.group(1)] = m.group(2)
    if not raw:
        # Status 0, aber keine einzige Hardware-Option: das Geraet hat
        # geantwortet, die Antwort passt aber nicht zum Muster (anderes
        # Backend, andere scanimage-Fassung, abgeschnittene Ausgabe).
        raise ScannerError(
            "Sensorabruf lieferte keine einzige Hardware-Option — "
            f"Ausgabe passt nicht zum Muster: {_last_line(out)}"
        )
    fehlend = [k for k in REQUIRED_SENSORS if k not in raw]
    if fehlend:
        # Eine TEILMENGE ist gefaehrlicher als nichts: ohne diesen Riegel
        # melden die fehlenden Felder ihre Vorgabe ("no") als Messwert.
        raise ScannerError(
            f"Sensorabruf ohne die Felder, die der Knopf braucht: "
            f"{', '.join(fehlend)} (gelesen: {', '.join(sorted(raw)) or 'keine'})"
        )
    yes = lambda k: raw.get(k, "no") == "yes"  # noqa: E731
    code = raw.get("error-code", "0")
    return Sensors(
        page_loaded=yes("page-loaded"), cover_open=yes("cover-open"),
        power_save=yes("power-save"), double_feed=yes("double-feed"),
        scan_button=yes("scan"),
        error_code=int(code) if code.isdigit() else 0,
        raw=raw,
    )


async def scan_batch(
    device: str, out_dir: Path, *, source: str, mode: str, resolution: int,
    page_height_mm: float, deskew: bool = False, crop: bool = False,
    skip_blank: float = 2.0, timeout: float = 1800.0,
) -> ScanResult:
    """Scan the whole feeder into out_dir as p0001.png...

    Two measured decisions are baked in as defaults:

    * ``page_height_mm`` is A4 (297), NOT SANE's US Letter default (279.364),
      which silently truncates 17.7mm off the bottom of every A4 page.
    * ``deskew`` is OFF. Scanner-side --swdeskew ESTIMATES the angle from page
      content and gets it badly wrong on a sparse back side (measured ~34
      degrees of rotation it invented). A crooked feed skews BOTH sides of a
      sheet, so a one-sided tilt is always this bug, never the paper.

    A fault mid-stack is REPORTED, never swallowed. scanimage signals a jam,
    double feed, opened cover or USB error through its exit status and still
    leaves the pages it managed to scan on disk; checking only "did any page
    come out?" files a silently truncated document, which for an archive is
    worse than a loud failure because it is found only when the page is needed.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    global _DEVICE_HELD
    _DEVICE_HELD += 1
    try:
        status, log = await _run(
            "-d", device,
            "--source", source, "--mode", mode, "--resolution", str(resolution),
            "--page-height", str(page_height_mm),
            "--swdeskew=yes" if deskew else "--swdeskew=no",
            "--swcrop=yes" if crop else "--swcrop=no",
            "--swskip", str(skip_blank),
            "--format=png", f"--batch={out_dir}/p%04d.png",
            timeout=timeout,
        )
    finally:
        _DEVICE_HELD -= 1
    pages = sorted(out_dir.glob("p*.png"))
    detail = "\n".join(
        ln for ln in log.splitlines()
        if ln.startswith("scanimage:") and not _BENIGN.search(ln)
    )
    if detail:
        logger.error("scanner faulted mid-stack after %d page(s): %s", len(pages), detail)
        return ScanResult(pages=pages, log=log, faulted=True, fault_detail=detail)
    if status != 0 and _TERMINATOR not in log and not pages:
        return ScanResult(pages=pages, log=log, faulted=True,
                          fault_detail=f"scanimage exited {status}")
    return ScanResult(pages=pages, log=log)
