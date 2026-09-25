"""Der blaue Knopf am Gerät startet einen Scan.

WARUM GEPOLLT WIRD
------------------
Ein Hardwareknopf sendet kein Ereignis. SANE bietet dafür nichts an, worauf man
horchen könnte — der Zustand ist nur abfragbar. Das ist die eine Stelle, an der
Polling nicht Bequemlichkeit ist, sondern die einzige verfügbare Bauform.

GEMESSEN AM 2026-09-25 (ScanSnap S1500, fujitsu-Backend) — die Zahlen stehen
hier, damit sie niemand später wegoptimiert:

* **Ein Sensorabruf kostet 10 ms.** `scanimage -d <gerät> -A`, dreimal gemessen,
  jeweils 0,01 s. 🛑 NICHT zu verwechseln mit `scanimage --help`: das braucht
  **7,4 s**, weil es sämtliche SANE-Backends durchsucht. Wer die Gerätekennung
  wegfallen lässt, macht aus 10 ms mehr als sieben Sekunden.
* **Der Knopf VERRIEGELT NICHT und meldet auch kein HALTEN.** Zwei Messreihen,
  100-ms- und 200-ms-Takt: jeder Druck ergab genau EINE Stichprobe mit
  `scan=yes` — auch wenn zwei Sekunden lang gehalten wurde. Eine einzelne
  Abfrage Sekunden nach einem Druck meldete `no`. Der Sensor gibt einen kurzen
  IMPULS je Druck; Halten hilft nicht, und ein gespeichertes Ereignis gibt es
  nicht.
* **Der Impuls dauert rund 200–250 ms.** Bei 200-ms-Takt wurden 5 von 5 Drücke
  erfasst — das ginge bei einem kürzeren Impuls nicht.

🛑 **200 ms ist auch die Obergrenze fuer die GERAETElast, nicht nur fuer die CPU.**
Die erste Fassung fragte zehnmal je Sekunde ab und belegte den Scanner damit zu
42 % der Zeit. Am 2026-09-25 hat ihn das nach rund einer halben Stunde
festgesetzt: SANE meldete "no Fujitsu scanner found", und erst Aus- und
Einschalten half. Die teure Ressource ist hier das GERAET, nicht der Prozessor —
das hatte ich beim ersten Messen uebersehen.

DESHALB DIE ZWEI TAKTE
----------------------
Zehn Abfragen je Sekunde kosten rund 10 % eines Kerns — dauerhaft, für einen
Knopf, der vielleicht dreimal am Tag gedrückt wird. `page-loaded` ist der
Türsteher: ohne Papier im Einzug KANN kein Scan beginnen, dort genügt eine
Abfrage je Sekunde (~1 %). Liegt Papier ein, wird hochgeschaltet — der teure
Takt läuft nur in den Sekunden, in denen er etwas bringt.

WOHIN SO EIN SCAN GEHT
----------------------
Nirgendwohin, und das ist Absicht. Das Ziel wird sonst aus dem `caller`
abgeleitet; ein Knopfdruck hat keinen. Bei mehreren konfigurierten Zielen landet
der Stapel deshalb `unrouted` und wartet auf eine Entscheidung, statt in eine
Vermutung gefilt zu werden. Trennblätter entscheiden es im Vorbeigehen — sie
sind die einzige Routing-Schicht, die einen unbeaufsichtigten Scan übersteht.
Wer den Knopf fest zuordnen will, setzt `SCANNER_CALLER_TARGET_BUTTON`.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable


from . import sane

logger = logging.getLogger("renfield-mcp-scanner.button")

#: `caller`, unter dem ein knopfgestarteter Auftrag läuft. Eigener Name, damit
#: er in Protokollen von einem Agenten-Scan unterscheidbar ist UND per
#: `SCANNER_CALLER_TARGET_BUTTON` fest zugeordnet werden kann.
BUTTON_CALLER = "button"

IDLE_INTERVAL = 1.0    # ohne Papier: ~1 % eines Kerns
ARMED_INTERVAL = 0.2   # mit Papier: fängt den ~200-ms-Impuls, s. Modulkopf
ERROR_BACKOFF = 5.0    # Gerät weg/schläft: nicht im Leerlauf hämmern
REDISCOVER_AFTER = 3   # so viele Fehlschläge in Folge, bevor neu gesucht wird


async def watch_button(
    config: Any,
    start_job: Callable[..., Awaitable[dict]],
    *,
    read_sensors: Callable[[str], Awaitable[sane.Sensors]] = sane.read_sensors,
    find_device: Callable[[str], Awaitable[str]] = sane.find_device,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    max_iterations: int | None = None,
) -> None:
    """Warte auf den Knopf und starte einen Scan. Läuft bis zum Abbruch.

    ``max_iterations`` gibt es allein für Tests — ohne Abbruchbedingung wäre
    eine Endlosschleife nicht prüfbar.
    """
    device: str | None = None
    pressed_before = False
    scanning = False
    iterations = 0
    errors = 0

    while max_iterations is None or iterations < max_iterations:
        iterations += 1
        try:
            if device is None:
                device = await find_device(config.device)
                logger.info("Knopfwächter: Gerät %s", device)
            sensors = await read_sensors(device)
        except Exception as exc:
            # Ein ausgestöpselter oder schlafender Scanner ist ein normaler
            # Zustand, kein Ausfall des Dienstes. Gerät neu suchen und ruhig
            # weiterlaufen — der Wächter darf den MCP-Server nie mitreissen.
            #
            # 🛑 SICHTBAR, nicht `debug`. Die erste Fassung protokollierte hier
            # auf debug, der Dienst läuft auf info — ein Wächter, der jede
            # Sekunde scheitert, wäre damit STUMM gewesen und der Knopf hätte
            # einfach nie funktioniert, ohne eine einzige Zeile. Genau der
            # Fehlermodus, gegen den dieser Dienst sonst überall gebaut ist.
            # Gedrosselt, damit ein dauerhaft fehlendes Gerät das Protokoll
            # nicht flutet: die erste Meldung sofort, danach eine je Minute.
            errors += 1
            if errors == 1 or errors % int(60 / ERROR_BACKOFF) == 0:
                logger.warning("Knopfwächter: Sensor nicht lesbar (%s) — "
                               "%d. Versuch seit dem letzten Erfolg", exc, errors)
            # 🛑 Das Geraet NICHT bei jedem Fehler neu suchen. Die Suche
            # (`scanimage -f`) ist der langsame Pfad — sie durchkaemmt alle
            # SANE-Backends und belegt dabei den USB-Anschluss. Die erste
            # Fassung setzte hier `device = None` und erzwang damit alle fuenf
            # Sekunden genau diese Suche; eine haengende Suche hielt das Geraet,
            # die naechste fand es deshalb nicht, und der Schaden vervielfachte
            # sich im Takt. Am 2026-09-25 hat das den Scanner festgesetzt, bis
            # er vom Strom genommen wurde.
            # Erst nach mehreren Fehlschlaegen in Folge neu suchen.
            if errors >= REDISCOVER_AFTER:
                device = None
            pressed_before = False
            await sleep(ERROR_BACKOFF)
            continue
        if errors:
            logger.info("Knopfwächter: Sensor wieder lesbar nach %d Fehlversuch(en)",
                        errors)
            errors = 0

        pressed = sensors.scan_button
        # FLANKE, nicht Zustand: ein gehaltener Knopf darf nicht zwei Scans
        # auslösen. Fällt der Sensor zwischendurch nicht auf `no` zurück,
        # bleibt es bei einem Auftrag.
        if pressed and not pressed_before and not scanning:
            logger.info("Knopf gedrückt — starte Scan (Papier: %s)",
                        "ja" if sensors.page_loaded else "nein")
            scanning = True
            try:
                result = await start_job(caller=BUTTON_CALLER, target="", title="")
            except Exception as exc:
                logger.warning("Knopf-Scan konnte nicht starten: %s", exc)
                result = {}
            finally:
                scanning = False
            if result.get("busy"):
                # Erwarteter Fall, kein Fehler: es gibt genau einen Einzug.
                logger.info("Knopf gedrückt, aber ein Scan läuft bereits — ignoriert")
            elif result.get("job_id"):
                logger.info("Knopf-Scan gestartet: %s", result["job_id"])
        pressed_before = pressed

        # Der teure Takt NUR mit Papier im Einzug. Ohne Papier kann der Knopf
        # ohnehin nichts auslösen, was ankommt.
        await sleep(ARMED_INTERVAL if sensors.page_loaded else IDLE_INTERVAL)
