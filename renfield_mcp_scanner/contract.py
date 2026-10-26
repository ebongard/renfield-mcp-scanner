"""The cross-repo ingest seam. Mirrors the sibling ingest MCPs verbatim.

This module is the CONTRACT with the Renfield backend's folder-ingest route.
Changing anything here requires a matching change on the backend side AND a
version bump on both — the backend pins these in
``tests/backend/test_folder_ingest_route.py``.

The load-bearing rule is `move_action_for`: an outcome we do not recognise
resolves to KEEP, never to a destructive action. Contract skew must never lose
a scan.
"""

from enum import Enum

SCANNER_INGEST_CONTRACT_VERSION = "1"
CONTRACT_HEADER = "X-Folder-Ingest-Contract"

INGEST_PATH = "/api/folder-ingest/document"
#: Der Gesundheitsendpunkt der Gegenseite. 🛑 Von DIESER Seite ruft ihn nichts:
#: `TargetPusher.health()` gab es seit Phase 1 und hatte nie einen Aufrufer, sie
#: ist 2026-10-26 entfallen. Die Konstante bleibt, weil sie den VERTRAG
#: beschreibt und der Endpunkt wirklich existiert
#: (`api/routes/folder_ingest.py`, Prefix `/api/folder-ingest`) — wer die
#: Erreichbarkeit der Ziele proben will, findet den Weg hier. In
#: `watcher_health` gehoert sie NICHT: diese Sonde liest ausdruecklich nur
#: Kostenloses und faesst weder Geraet noch Netz an.
HEALTH_PATH = "/api/folder-ingest/health"

# Scan-job completion event: the scanner tells the instance that REQUESTED a scan
# how it ended. A separate seam from ingest (the document may have gone to a
# different instance than the one that asked), with its own version.
SCANNER_JOB_EVENT_CONTRACT_VERSION = "1"
JOB_EVENT_CONTRACT_HEADER = "X-Scanner-Job-Contract"
JOB_EVENT_PATH = "/api/scanner/job-event"

#: Das Merkmal, an dem eine 404 DER ANWENDUNG erkennbar ist. Renfield antwortet
#: bei abgeschaltetem Ordner-Ingest mit
#: ``{"detail": {"reason": "folder_ingest_disabled", "message": "…"}}``.
#:
#: 🛑 Eine 404 OHNE dieses Merkmal ist WIEDERHOLBAR, nicht endgültig. Am
#: 2026-10-11 während eines Backend-Rollouts gemessen: auf zwei richtig
#: wiederholte 503 folgte eine 404 von Traefik, und die verwarf die
#: Fertigmeldung endgültig. Dass sie nicht von der Anwendung kam, ist belegt —
#: `FOLDER_INGEST_ENABLED` stand auf `true`, und im Backend-Protokoll steht zu
#: dieser Sekunde keine `job-event`-Zeile, nur Startmeldungen. Die Zahl allein
#: trägt die Unterscheidung also nicht; das Merkmal tut es.
#:
#: 🛑 NICHT ZU VERWECHSELN mit ``reason: "feature_disabled"``. Dieselbe Tatsache
#: hat auf der Renfield-Seite einen zweiten Namen (`api/routes/folder_ingest.py`
#: und `api/routes/email_ingest.py`), und das bleibt mit Absicht so:
#:
#: * ``feature_disabled`` ist GENERISCH — zwei verschiedene Routen für zwei
#:   verschiedene Funktionen antworten damit — und es reist mit **503**. Es ist
#:   eine Erläuterung an einer WIEDERHOLBAREN Antwort, kein Endgültigkeitszeichen.
#: * Dieses Merkmal hier reist mit **404** und trägt die ENDGÜLTIGKEIT: es ist
#:   die einzige Zeichenkette im System, auf die ein Absender eine Fertigmeldung
#:   verwirft.
#:
#: Die beiden gleichzuziehen würde genau die Verwechslung bauen, gegen die
#: #1391 geschrieben ist: ein generischer Grund, den heute zwei fremde Routen
#: für „nicht jetzt" benutzen, dürfte nie „für immer" bedeuten. Käme eine
#: dritte Route auf die Idee, ihn an eine 404 zu hängen, verschwände dort still
#: eine Fertigmeldung. Ein Test hält darum fest, dass ``feature_disabled`` als
#: Merkmal NICHT zählt.
#:
#: Dieses Merkmal ist von der Versionsregel oben AUSGENOMMEN, und zwar bewusst:
#: es steht nur im FEHLERRUMPF und ist in beide Richtungen verträglich. Ein
#: Renfield ohne das Merkmal lässt den Absender wiederholen (siehe
#: `JobEventNotifier.send_once`), ein Scanner ohne diese Prüfung verwirft wie
#: bisher. Ein Versionssprung würde dagegen auf JEDER Meldung eine
#: Abweichungswarnung auf beiden Seiten erzeugen, solange nur eine Seite
#: ausgerollt ist — Lärm ohne Erkenntnis.
INGEST_DISABLED_REASON = "folder_ingest_disabled"

#: Merkmal einer 409, die ENDGÜLTIG ist: die Gegenseite wird für diesen
#: Auftrag nie einen Empfänger haben.
#:
#: 🛑 Eine blanke 409 „unknown job" ist mit Absicht WIEDERHOLBAR — ein Scan,
#: der sofort scheitert, kann melden, bevor die Gegenseite den Anforderer
#: notiert hat. Für einen KNOPF-Scan gilt das nie: das Backend bindet die
#: Fertigmeldung an die `session_id` des anfordernden Chats, und ein
#: Knopfdruck hat keine Sitzung. Am 2026-10-21 wiederholte der Dienst deshalb
#: gegen eine Ablehnung, die sich nie ändern konnte.
#:
#: Dasselbe Muster wie bei der 404: endgültig ist eine Ablehnung nur MIT
#: Merkmal. Ohne Merkmal bleibt sie wiederholbar — die Unterscheidung gehört
#: der Anwendung, nicht dem Statuscode.
NO_RECIPIENT_REASON = "no_recipient_for_caller"


class IngestStatus(str, Enum):
    """The backend's 4-state reply. All four arrive as HTTP 200."""

    INGESTED = "ingested"
    DUPLICATE = "duplicate"
    RETRY = "retry"
    FAILED = "failed"


class StageAction(str, Enum):
    """What the router does with a staged scan after a push attempt.

    DISCARD only ever follows a terminal, acknowledged outcome. Everything
    ambiguous resolves to KEEP so the pages survive for another attempt — a
    scanned document cannot be re-created without the paper going back through
    the feeder, so losing one costs the operator a physical re-scan.
    """

    DISCARD = "discard"
    KEEP = "keep"
    FAIL = "fail"


_ACTION_BY_STATUS = {
    IngestStatus.INGESTED: StageAction.DISCARD,
    IngestStatus.DUPLICATE: StageAction.DISCARD,
    IngestStatus.RETRY: StageAction.KEEP,
    IngestStatus.FAILED: StageAction.FAIL,
}


def action_for_status(status: str) -> StageAction:
    """Map a backend status to a staging action. Unknown status -> KEEP.

    Deliberately lenient: a backend that grows a fifth status must not cause an
    older scanner build to delete pages it does not understand.
    """
    try:
        return _ACTION_BY_STATUS[IngestStatus(status)]
    except (ValueError, KeyError):
        return StageAction.KEEP
