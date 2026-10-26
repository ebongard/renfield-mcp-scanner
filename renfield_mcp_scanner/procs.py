"""Kindprozesse einsammeln — EINE Stelle für alle asyncio-Prozesse.

🛑 Warum ein eigenes Modul: im Paket standen drei Muster für dieselbe Frage
nebeneinander — `sane.py` tötete das Kind, `pdf.py` die ganze Prozessgruppe,
und `usb.py` gar nichts. Das dritte war kein Entwurf, sondern ein Versehen:
`scanner_node` brach bei Zeitablauf nur das `await` ab und liess `ioreg`
laufen. Aufgerufen wird es unter anderem auf den FEHLERPFADEN der
Wächterschleife (`button.py`), die zyklisch wiederkehren — ein hängendes
`ioreg` hätte sich dort je Runde angesammelt, und zwar genau dann, wenn der
USB-Stapel klemmt.

`pdf.py` bleibt bewusst eigen: `ocrmypdf` ist ein Elternteil mit bis zu sieben
tesseract-Nachkommen, dort muss die GRUPPE fallen. Alle übrigen Programme im
Paket (`scanimage`, `ioreg`, `img2pdf`, `pdftotext`) sind Einzelprozesse, für
die das Töten des Kindes genügt.
"""

from __future__ import annotations

import asyncio
import contextlib


def reap(proc: asyncio.subprocess.Process) -> None:
    """Tötet einen noch laufenden Kindprozess SYNCHRON.

    🛑 Kein `await`. Im Abbruchfall ist die Aufgabe bereits am Abbrechen, und
    ein `await` könnte sofort wieder mit `CancelledError` herausfahren — der
    Prozess blieb dann trotz Aufräumens stehen. `kill()` ist synchron und ist
    der entscheidende Teil: es gibt das Betriebsmittel frei. Das Einsammeln
    des Zombies übernimmt die Kinderaufsicht der Ereignisschleife.
    """
    if proc.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
