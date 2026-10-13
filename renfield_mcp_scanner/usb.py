"""Diagnose am USB-Knoten — ohne das Gerät anzufassen.

`ioreg` liest die Registratur des Kernels. Es öffnet das Gerät NICHT, belegt
den Anschluss nicht und kann es nicht verklemmen. Das ist der Grund, warum
diese Auskunft hier stehen darf, während jede `scanimage`-Probe im
Geräte-Budget steckt.

🛑 Wozu: bis 2026-10-12 war „der Scanner ist aus" von „der Scanner ist
verklemmt" nicht zu unterscheiden — beides ergab `ok:false`, und deshalb sind
am 2026-10-11 15,3 h Ausfall unbemerkt verstrichen. Zwei Messungen am
2026-10-12 geben das Unterscheidungsmerkmal:

* Beim Stromlosmachen wechselt die `sessionID` (`68501126921529` →
  `68618798644518`), während der SANE-Name `:10551` unverändert bleibt. Ein
  Wechsel heisst also: jemand hat das Gerät angefasst.
* Im VERKLEMMTEN Zustand ist der Knoten weiterhin vorhanden. Fehlt er, ist der
  Scanner aus oder abgesteckt — auf einem Arbeitsplatzrechner der Normalfall.

Daraus folgt die Grenze dieser Auskunft: sie ist ein **Abbruch- und
Diagnosesignal, kein Aufwachsignal.** „Knoten da" beweist nicht „Gerät
benutzbar".
"""

from __future__ import annotations

import asyncio
import logging
import plistlib
import shutil
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("renfield-mcp-scanner.usb")

#: Fujitsu. Der Produktname ist lesbar (`ScanSnap S1500`), die Seriennummer
#: NICHT — der S1500 beantwortet die Abfrage seines String-Deskriptors nicht
#: (Chrome meldet `serial=""`, `sane-find-scanner` `Pipe error`). Deshalb wird
#: über Hersteller und Produktname gesucht, nie über die Seriennummer.
_VENDOR_ID = 1221
_PRODUCT_ID = 4514
_PRODUCT_HINT = "scansnap"

_IOREG_TIMEOUT = 5.0


@dataclass(frozen=True)
class UsbNode:
    """Was die Registratur über den Scanner-Knoten sagt."""

    present: bool
    session_id: str | None = None
    #: Zweites Merkmal. Ein Verbund- oder Hub-Knoten kann mehr als einen
    #: Treffer liefern; die `locationID` sagt, an welchem Anschluss er hängt,
    #: und trennt in der Auswertung einen Anschlusswechsel von einem
    #: Aus- und Einschalten am selben Anschluss.
    location_id: str | None = None

    def __str__(self) -> str:
        if not self.present:
            return "USB-Knoten FEHLT (Scanner aus oder abgesteckt)"
        return (f"USB-Knoten vorhanden, sessionID={self.session_id or 'unbekannt'}"
                f", locationID={self.location_id or 'unbekannt'}")


async def scanner_node() -> UsbNode | None:
    """Zustand des Scanner-Knotens, oder `None` wenn nicht feststellbar.

    `None` heisst „keine Auskunft" (kein `ioreg`, kein macOS, Zeitablauf,
    unlesbare Ausgabe) und ist ausdrücklich NICHT `present=False`: eine
    fehlende Auskunft darf nicht als „Scanner aus" gelesen werden. Wirft nie —
    eine Diagnose, die den Aufrufer mitnimmt, ist keine.
    """
    if not shutil.which("ioreg"):
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            # 🛑 `-a` (XML-plist) ist nicht Bequemlichkeit, sondern Korrektheit.
            # Die erste Fassung las die TEXTausgabe und trennte die Geräte an
            # `^\+-o`. Verschachtelte Knoten sind aber eingerückt und fallen
            # damit in den Block ihres Elternteils: der Treffer „ScanSnap" lag
            # im Block eines LG-Monitors, und die zurückgegebene `sessionID`
            # war dessen — eine plausible Zahl vom falschen Gerät. Der plist
            # gibt einen Baum, in dem jeder Knoten seine eigenen Merkmale hält.
            "ioreg", "-a", "-r", "-c", "IOUSBHostDevice", "-l", "-w", "0",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=_IOREG_TIMEOUT)
        nodes = plistlib.loads(out)
    except (TimeoutError, OSError, plistlib.InvalidFileException,
            ValueError, EOFError) as exc:
        logger.debug("ioreg nicht auswertbar: %s", exc)
        return None
    if not isinstance(nodes, list):
        return None
    return _pick(nodes)


def _pick(nodes: list[dict[str, Any]]) -> UsbNode:
    """Erster Scanner-Knoten MIT `sessionID`.

    Der Scanner erscheint zweimal: als Gerät (trägt die `sessionID`) und als
    seine Schnittstelle darunter (trägt sie nicht). Ohne die Bedingung „hat
    eine `sessionID`" entscheidet die Reihenfolge des Baums darüber, ob die
    Auskunft brauchbar ist.
    """
    treffer = False
    for node in _walk(nodes):
        name = str(node.get("USB Product Name")
                   or node.get("IORegistryEntryName") or "")
        # Hersteller UND Produkt, oder ersatzweise der Produktname. Der
        # Name allein genügt nicht: er steht auch an der Schnittstelle
        # unter dem Gerät. Die Kennungen allein genügen auch nicht: ein
        # Gerät, das seine Deskriptoren schlecht beantwortet, kann sie
        # schuldig bleiben.
        passt = (node.get("idVendor") == _VENDOR_ID
                 and node.get("idProduct") == _PRODUCT_ID)
        if not passt and _PRODUCT_HINT not in name.lower():
            continue
        treffer = True
        sid = node.get("sessionID")
        if sid is not None:
            loc = node.get("locationID")
            return UsbNode(present=True, session_id=str(sid),
                           location_id=str(loc) if loc is not None else None)
    # Gefunden, aber ohne Kennung: vorhanden ist vorhanden.
    return UsbNode(present=True) if treffer else UsbNode(present=False)


def _walk(nodes: list[dict[str, Any]]):
    for node in nodes:
        if not isinstance(node, dict):
            continue
        yield node
        kinder = node.get("IORegistryEntryChildren")
        if isinstance(kinder, list):
            yield from _walk(kinder)
