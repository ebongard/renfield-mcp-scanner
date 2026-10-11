"""Durable staging for scans that have not (yet) reached an instance.

This directory is the reason an unrouted scan never has to touch a database:
it holds pages and the assembled PDF on the ROUTER's disk until a destination
is settled, so nothing transits an instance that may turn out to be the wrong
one.

It is also the crash/sleep survival story. The operator host is a workstation:
it sleeps, it reboots, the backend may be down when a scan finishes. A staged
scan survives all three and is retried or resolved later. Pages are only ever
discarded after an acknowledged terminal outcome.

Contents are private documents. The directory is created 0700 and lives outside
any repository.
"""

from __future__ import annotations

import json
import logging
import shutil
import uuid
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger("renfield-mcp-scanner.staging")

_META = "stage.json"

#: Gründe, die auf eine ENTSCHEIDUNG warten — ein vorgesehener Zustand. Alles
#: andere ist ein FEHLER-Stapel: ein abgebrochener Scan, eine abgelehnte oder
#: unmögliche Zustellung. 🛑 Der Unterschied ist nicht kosmetisch. Am
#: 2026-10-11 lag ein Stapel mit `reason: scanner_fault` sieben Tage im
#: Zwischenlager — zwei digitalisierte Seiten eines Briefes, die nie in einer
#: Instanz ankamen —, und gemeldet wurde er nur als Zahl in der Startzeile
#: („2 target(s), 1 pending, 0 purged"). Ein `unrouted`-Stapel wartet auf einen
#: Menschen; ein Fehler-Stapel wartet darauf, dass jemand überhaupt von ihm
#: erfährt.
_AWAITING_DECISION = frozenset({"unrouted"})


class Staging:
    def __init__(self, root: Path):
        self.root = Path(root).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)
        self.root.chmod(0o700)

    def new_stage(self) -> Path:
        # The random suffix is not cosmetic: a timestamp alone (even to the
        # millisecond) can collide for two scans started in the same instant,
        # and a collision means two documents sharing one directory and
        # overwriting each other's pages.
        stage = self.root / f"scan-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}"
        (stage / "pages").mkdir(parents=True, exist_ok=True)
        stage.chmod(0o700)
        return stage

    def keep(self, stage: Path, *, reason: str, detail: str = "",
             target: str | None = None) -> None:
        """Retain a stage with the reason it is still here.

        ``target`` is load-bearing for recovery: without it a retry cannot know
        where the scan was destined, and KEEP degrades into "kept forever".
        A scan kept because it was UNROUTED has no target by definition — that
        one needs a human decision, not a retry.
        """
        (stage / _META).write_text(json.dumps(
            {"reason": reason, "detail": detail, "target": target,
             "at": datetime.now().isoformat()},
            indent=2))
        logger.info("stage %s kept (%s)", stage.name, reason)

    def discard(self, stage: Path) -> None:
        """Remove a stage. Only ever after an acknowledged terminal outcome."""
        shutil.rmtree(stage, ignore_errors=True)
        logger.info("stage %s discarded", stage.name)

    def pending(self) -> list[dict]:
        """Jeder wartende Stapel — mit Grund, ALTER und der Angabe, ob er aus
        einem Fehler stammt.

        Grund und Zeitpunkt standen hier schon immer (`reason`, `at`); das
        Alter musste der Aufrufer selbst rechnen, und dass `reason:
        scanner_fault` etwas anderes ist als `reason: unrouted`, stand nirgends.
        Beides ist jetzt im Eintrag — sonst bleibt ein abgebrochener Scan in
        jeder Anzeige ein Posten unter anderen.
        """
        out = []
        for stage in sorted(self.root.glob("scan-*")):
            meta_file = stage / _META
            meta = {}
            if meta_file.exists():
                try:
                    meta = json.loads(meta_file.read_text())
                except ValueError:
                    meta = {"reason": "unreadable_metadata"}
            entry = {
                "stage_id": stage.name,
                "pages": len(list((stage / "pages").glob("p*.png"))),
                "has_pdf": any(stage.glob("*.pdf")),
                **meta,
            }
            # 🛑 NACH `**meta`, nicht davor: eine fremde oder von Hand
            # bearbeitete `stage.json` darf das abgeleitete Urteil nicht
            # überschreiben. (`pages` und `has_pdf` stehen aus demselben Grund
            # schon seit immer unter diesem Vorbehalt — dort ist es ein
            # bekannter, kleinerer Mangel.)
            entry["age_seconds"] = self._age_seconds(stage, entry.get("at"))
            entry["fault"] = entry.get("reason") not in _AWAITING_DECISION
            out.append(entry)
        return out

    def _age_seconds(self, stage: Path, at: object) -> float:
        """Wie lange der Stapel schon liegt.

        `at` ist die Zeit, zu der er zuletzt zurückgestellt wurde, und damit
        die richtige Auskunft. Fehlt oder bröselt sie — ein Stapel, dessen Scan
        gerade läuft, hat noch keine —, zählt die Änderungszeit des
        Verzeichnisses; dieselbe Grösse, an der auch `purge_expired` hängt.
        """
        try:
            seit = datetime.fromisoformat(str(at))
        except (TypeError, ValueError):
            seit = datetime.fromtimestamp(stage.stat().st_mtime)
        return round(max(0.0, (datetime.now() - seit).total_seconds()), 1)

    def warn_stale(self, warn_after_days: float) -> list[dict]:
        """Jeden zu lange liegenden Stapel als WARNUNG melden, mit Alter und Grund.

        🛑 Warum eine Warnung je Stapel und nicht eine Zahl: die Startzeile
        („… 2 target(s), 1 pending, 0 purged") stand seit dem 2026-10-04 bei
        jedem Start im Protokoll, während zwei gescannte Seiten eines Briefes
        unbemerkt im Zwischenlager lagen. Eine Zahl in einer INFO-Zeile fordert
        keine Entscheidung an; sie wird gelesen wie ein Betriebszustand.

        Gemeldet wird je Stapel genau einmal je Start — das ist kein Takt und
        kein Wecker, sondern die Gelegenheit, bei der der Zustand ohnehin
        gelesen wird.
        """
        if warn_after_days <= 0:
            return []
        grenze = warn_after_days * 86400
        alt = [e for e in self.pending() if e["age_seconds"] >= grenze]
        for e in alt:
            art = "FEHLER" if e["fault"] else "wartet auf eine Entscheidung"
            detail = str(e.get("detail") or "")[:200]
            logger.warning(
                "Stapel %s liegt seit %.1f Tag(en) im Zwischenlager: %s — %s%s, "
                "%d Seite(n). Mit route_scan zuordnen oder mit "
                "retry_pending_scans erneut zustellen; sonst verfällt er nach "
                "SCANNER_STAGING_RETENTION_DAYS.",
                e["stage_id"], e["age_seconds"] / 86400, art,
                e.get("reason") or "ohne Grund",
                f" ({detail})" if detail else "", e.get("pages", 0),
            )
        return alt

    def purge_expired(self, retention_days: int) -> int:
        """Retention sweep. Private documents do not sit here forever.

        Der Kehraus unterscheidet NICHT nach Grund: ein Fehler-Stapel verfällt
        wie ein `unrouted`, und zwar ab der letzten Änderung des Verzeichnisses
        — also ab dem letzten `keep`, nicht ab dem Scan. Ein Test hält das
        fest, weil die Frage bei #6 ausdrücklich gestellt wurde.

        🛑 Gerufen wird er beim START des Dienstes. Ein Dienst, der monatelang
        durchläuft, kehrt also nicht aus. Auf dem Bedienrechner ist das in der
        Praxis kein Zeitraum (er schläft, startet neu, wird aktualisiert) —
        aber es ist eine Annahme, nicht eine Zusage.
        """
        if retention_days <= 0:
            return 0
        cutoff = datetime.now() - timedelta(days=retention_days)
        removed = 0
        for stage in self.root.glob("scan-*"):
            if datetime.fromtimestamp(stage.stat().st_mtime) < cutoff:
                shutil.rmtree(stage, ignore_errors=True)
                removed += 1
        if removed:
            logger.info("purged %d stage(s) older than %dd", removed, retention_days)
        return removed
