"""Staging is the reason an unrouted scan never touches an instance."""
import inspect
import json
import logging
import os
from datetime import datetime, timedelta

from renfield_mcp_scanner.staging import Staging


def test_root_is_private(tmp_path):
    s = Staging(tmp_path / "st")
    assert oct(s.root.stat().st_mode)[-3:] == "700"


def test_keep_records_the_reason(tmp_path):
    s = Staging(tmp_path / "st")
    stage = s.new_stage()
    s.keep(stage, reason="unrouted", detail="two targets configured")
    pending = s.pending()
    assert len(pending) == 1
    assert pending[0]["reason"] == "unrouted"
    assert pending[0]["stage_id"] == stage.name


def test_discard_removes(tmp_path):
    s = Staging(tmp_path / "st")
    stage = s.new_stage()
    s.discard(stage)
    assert not stage.exists() and s.pending() == []


def test_pending_counts_pages(tmp_path):
    s = Staging(tmp_path / "st")
    stage = s.new_stage()
    for i in range(3):
        (stage / "pages" / f"p{i:04d}.png").write_bytes(b"x")
    s.keep(stage, reason="scanner_fault")
    assert s.pending()[0]["pages"] == 3


def test_unreadable_metadata_still_lists(tmp_path):
    s = Staging(tmp_path / "st")
    stage = s.new_stage()
    (stage / "stage.json").write_text("{not json")
    assert s.pending()[0]["reason"] == "unreadable_metadata"


def test_purge_respects_retention(tmp_path):
    s = Staging(tmp_path / "st")
    old, new = s.new_stage(), s.new_stage()
    old_ts = (datetime.now() - timedelta(days=40)).timestamp()
    os.utime(old, (old_ts, old_ts))
    assert s.purge_expired(30) == 1
    assert new.exists() and not old.exists()


def test_purge_disabled_by_zero(tmp_path):
    s = Staging(tmp_path / "st")
    s.new_stage()
    assert s.purge_expired(0) == 0 and len(s.pending()) == 1


def test_stage_ids_never_collide(tmp_path):
    # A timestamp alone collides for scans started in the same instant, and a
    # collision means two documents sharing a directory.
    s = Staging(tmp_path / "st")
    ids = {s.new_stage().name for _ in range(500)}
    assert len(ids) == 500


# --- Fehler-Stapel sind ein EREIGNIS, kein Zählwert (#6) --------------------------


def _fehler_stapel(s: Staging, *, tage_alt: float = 7.0, pages: int = 2):
    """Der Stapel vom 2026-10-04, nachgebaut: Papierstau nach zwei Seiten."""
    stage = s.new_stage()
    for i in range(pages):
        (stage / "pages" / f"p{i:04d}.png").write_bytes(b"x")
    s.keep(stage, reason="scanner_fault",
           detail="scanimage: sane_start: Document feeder jammed", target=None)
    meta = json.loads((stage / "stage.json").read_text())
    meta["at"] = (datetime.now() - timedelta(days=tage_alt)).isoformat()
    (stage / "stage.json").write_text(json.dumps(meta))
    return stage


class TestEinFehlerStapelIstSichtbar:
    """🛑 Der Befund vom 2026-10-11: `scan-20261004-152358-5e530ac2` lag SIEBEN
    TAGE im Zwischenlager — `reason: scanner_fault`, `target: None`, zwei
    Seiten, 20 MB — und wurde allein als Zahl in der Startzeile gemeldet
    („2 target(s), 1 pending, 0 purged"). Zwei digitalisierte Seiten eines
    Briefes, die nie in einer Instanz ankamen.
    """

    def test_grund_und_zeitpunkt_standen_schon_IMMER_im_eintrag(self, tmp_path):
        """Nachgemessen, bevor etwas geändert wurde: `list_pending_scans` hat
        Fehler-Stapel nie verschwiegen. Gefehlt haben das ABGELEITETE Alter und
        die Unterscheidung vom vorgesehenen `unrouted` — nicht die Zeile."""
        s = Staging(tmp_path / "st")
        _fehler_stapel(s)
        eintrag = s.pending()[0]
        assert eintrag["reason"] == "scanner_fault"
        assert "feeder jammed" in eintrag["detail"]
        assert eintrag["at"] and eintrag["pages"] == 2

    def test_der_eintrag_nennt_jetzt_alter_UND_fehlerkennung(self, tmp_path):
        s = Staging(tmp_path / "st")
        _fehler_stapel(s, tage_alt=7.0)
        eintrag = s.pending()[0]
        assert eintrag["fault"] is True
        assert 6.9 * 86400 < eintrag["age_seconds"] < 7.1 * 86400

    def test_ein_unrouted_stapel_ist_KEIN_fehler(self, tmp_path):
        """🛑 Negativkontrolle. Ohne sie wäre `fault` immer wahr und die
        Unterscheidung wertlos: ein `unrouted`-Stapel ist ein VORGESEHENER
        Zustand, der auf einen Menschen wartet."""
        s = Staging(tmp_path / "st")
        s.keep(s.new_stage(), reason="unrouted", detail="two targets configured")
        eintrag = s.pending()[0]
        assert eintrag["fault"] is False
        assert eintrag["reason"] == "unrouted"

    def test_eine_fremde_stage_json_kann_das_urteil_nicht_umschreiben(self, tmp_path):
        """Die abgeleiteten Felder stehen NACH `**meta`. Stünden sie davor,
        liesse sich ein Fehler-Stapel durch eine von Hand bearbeitete Datei
        unsichtbar machen."""
        s = Staging(tmp_path / "st")
        stage = _fehler_stapel(s)
        meta = json.loads((stage / "stage.json").read_text())
        meta.update(fault=False, age_seconds=0.0)
        (stage / "stage.json").write_text(json.dumps(meta))
        eintrag = s.pending()[0]
        assert eintrag["fault"] is True
        assert eintrag["age_seconds"] > 86400

    def test_ohne_zeitstempel_zaehlt_die_aenderungszeit(self, tmp_path):
        """Ein Stapel, dessen Scan gerade läuft, hat noch keine `stage.json` —
        er darf deswegen nicht ohne Alter dastehen."""
        s = Staging(tmp_path / "st")
        stage = s.new_stage()
        alt = (datetime.now() - timedelta(days=3)).timestamp()
        os.utime(stage, (alt, alt))
        eintrag = s.pending()[0]
        assert 2.9 * 86400 < eintrag["age_seconds"] < 3.1 * 86400
        assert eintrag["fault"] is True, (
            "ohne Grund ist ein Stapel kein vorgesehener Wartezustand")


class TestEinAlterStapelFordertEineENTSCHEIDUNGAn:
    """Eine Zahl in einer INFO-Zeile fordert nichts an — sie liest sich wie ein
    Betriebszustand. Genau so stand „1 pending" seit dem 2026-10-04 bei jedem
    Start im Protokoll."""

    def test_ein_alter_stapel_wird_als_warnung_gemeldet(self, tmp_path, caplog):
        s = Staging(tmp_path / "st")
        stage = _fehler_stapel(s, tage_alt=7.0)
        with caplog.at_level(logging.DEBUG):
            alt = s.warn_stale(1.0)

        assert [e["stage_id"] for e in alt] == [stage.name]
        warnungen = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warnungen, "ein alter Stapel MUSS eine Warnung erzeugen"
        text = warnungen[0].getMessage()
        assert stage.name in text
        assert "scanner_fault" in text, "der Grund gehört in die Meldung"
        assert "7.0 Tag" in text, f"das Alter gehört in die Meldung: {text}"

    def test_ein_frischer_stapel_wird_NICHT_gemeldet(self, tmp_path, caplog):
        """🛑 Negativkontrolle, und die wichtigere Hälfte: ein Scan, der gerade
        auf eine Zuordnung wartet, ist der Normalfall. Würde er gewarnt, wäre
        die Warnung binnen einer Woche Hintergrundrauschen — und der eine
        Fehler-Stapel wieder unsichtbar."""
        s = Staging(tmp_path / "st")
        s.keep(s.new_stage(), reason="unrouted")
        with caplog.at_level(logging.DEBUG):
            assert s.warn_stale(1.0) == []
        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []

    def test_null_schaltet_die_meldung_ab(self, tmp_path, caplog):
        s = Staging(tmp_path / "st")
        _fehler_stapel(s, tage_alt=99.0)
        with caplog.at_level(logging.DEBUG):
            assert s.warn_stale(0) == []
        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []

    def test_der_dienst_meldet_das_beim_START(self, tmp_path):
        """Strukturprüfung auf die NAHT. `_serve` ist mit einem uvicorn-Server
        darin nicht ausführbar — und die Naht war bei #3 schon einmal die
        Stelle, an der ein ungeprüfter Aufruf fehlte."""
        from renfield_mcp_scanner import server

        quelle = inspect.getsource(server._serve)
        assert "warn_stale(" in quelle, (
            "ohne diesen Aufruf bleibt die Warnung ungenutzt und der alte "
            "Stapel so unsichtbar wie am 2026-10-04"
        )


class TestDieAufbewahrungGreiftAuchFuerFehlerStapel:
    """Die ausdrückliche Frage aus #6: gescannte Seiten sind Papierinhalt und
    sollen nicht unbegrenzt auf einer Platte liegen."""

    def test_ein_alter_fehler_stapel_verfaellt(self, tmp_path):
        s = Staging(tmp_path / "st")
        stage = _fehler_stapel(s, tage_alt=40.0)
        alt = (datetime.now() - timedelta(days=40)).timestamp()
        os.utime(stage, (alt, alt))
        assert s.purge_expired(30) == 1
        assert not stage.exists()

    def test_ein_frischer_fehler_stapel_verfaellt_NICHT(self, tmp_path):
        """🛑 Negativkontrolle: ohne sie wäre der Test oben auch mit einem
        Kehraus grün, der ALLES löscht — und genau das wäre der Verlust, den
        das Zwischenlager verhindern soll."""
        s = Staging(tmp_path / "st")
        stage = _fehler_stapel(s, tage_alt=7.0)
        assert s.purge_expired(30) == 0
        assert stage.exists() and len(list((stage / "pages").glob("*.png"))) == 2
