"""Die funktionale Probe: schlägt an, wenn der Knopf tot ist — und sonst nie.

🛑 Warum es dieses Werkzeug überhaupt gibt. Der Scanner hatte bis 2026-10-21
KEINE funktionale Probe, mit dieser Begründung in `mcp_servers.yaml`:

    "this server reads `down` whenever that machine is asleep or logged out —
     expected, not a fault"

Für einen schlafenden Mac ist das richtig. Die Folge war aber, dass auch ein
LAUFENDER Dienst mit totem Knopf als „erwartet" durchging: am 2026-10-21 war
der blaue Knopf neun Tage tot, bei lebender Aufsicht, grüner Lebendigkeit und
`device_readable: true`. Niemand hat es gemeldet, weil niemand hingesehen hat.

Die Probe muss darum GENAU die Unterscheidung treffen, die vorher fehlte, und
dabei zwei Fehlalarmquellen vermeiden:

* ein ausgeschalteter Scanner (der Normalfall am Arbeitsplatz),
* eine FEHLENDE USB-Auskunft (kein macOS, Zeitablauf) — eine nicht
  vorhandene Messung darf nicht zu einem erfundenen Fehler werden.

Dass der schlafende Mac selbst keinen Alarm erzeugt, löst das Rahmenwerk: es
überspringt Proben für getrennte Server (`.claude/rules/mcp-health.md`).
"""
from __future__ import annotations

import time

import pytest

from renfield_mcp_scanner import button, tools, usb


class _Cfg:
    device = ""
    button_watch = True


class _Aus(_Cfg):
    button_watch = False


def _leben(state: str, *, finding: bool, device_readable: bool | None):
    """Eine Lebendigkeit, die einen festen Befund meldet."""
    class _L:
        def report(self, *, enabled: bool) -> dict:
            return {
                "state": state, "finding": finding,
                "device_readable": device_readable,
                "detail": f"Zustand {state}",
                "last_tick_age_seconds": 1.0, "stale_after_seconds": 370.0,
                "since": None, "restarts_total": 0, "restart_streak": 0,
            }
    return _L()


def _knoten(node):
    async def f():
        return node
    return f


DA = usb.UsbNode(present=True, session_id="68618798644518",
                 location_id="539033600")
WEG = usb.UsbNode(present=False)


class TestEinBefundSchlaegtAn:
    async def test_toter_waechter(self):
        r = await tools.watcher_health(
            _Cfg(), liveness=_leben(button.WATCHER_DEAD, finding=True,
                                    device_readable=True),
            scanner_node=_knoten(DA))
        assert r["ok"] is False
        assert r["error_code"] == "watcher_dead"

    async def test_absturzkreis_hat_seinen_EIGENEN_code(self):
        """Sonst wäre in der Meldung nicht erkennbar, welcher Fall vorlag —
        und die beiden brauchen verschiedene Handgriffe."""
        r = await tools.watcher_health(
            _Cfg(), liveness=_leben(button.WATCHER_CRASHLOOP, finding=True,
                                    device_readable=None),
            scanner_node=_knoten(DA))
        assert r["ok"] is False
        assert r["error_code"] == "watcher_crashloop"

    async def test_verklemmt_knoten_da_aber_sane_blind(self):
        """Der Fall vom 2026-10-21: neun Tage am Bus angemeldet, dieselbe
        sessionID, und für SANE nicht ansprechbar."""
        r = await tools.watcher_health(
            _Cfg(), liveness=_leben(button.WATCHER_ALIVE, finding=False,
                                    device_readable=False),
            scanner_node=_knoten(DA))
        assert r["ok"] is False
        assert r["error_code"] == "device_wedged"
        assert r["usb_node"]["present"] is True
        assert r["usb_node"]["session_id"] == "68618798644518"
        # Der Rückweg gehört in die Meldung — er ist nicht erratbar.
        assert "einschalten" in r["error"]


class TestKeinFehlalarm:
    """🛑 Der wichtigere Teil. Eine Probe, die am Normalfall anschlägt, wird
    abgeschaltet — und dann ist man schlechter dran als ohne sie."""

    async def test_scanner_AUSGESCHALTET_ist_kein_befund(self):
        r = await tools.watcher_health(
            _Cfg(), liveness=_leben(button.WATCHER_ALIVE, finding=False,
                                    device_readable=False),
            scanner_node=_knoten(WEG))
        assert r["ok"] is True, (
            "ein ausgeschalteter Scanner ist der Normalfall auf einem "
            "Arbeitsplatzrechner und darf keinen Alarm erzeugen")

    async def test_FEHLENDE_usb_auskunft_ist_kein_befund(self):
        """Kein `ioreg`, Zeitablauf, unlesbare Ausgabe → `None`. Daraus einen
        Fehler zu machen hieße, eine nicht vorhandene Messung zu erfinden."""
        r = await tools.watcher_health(
            _Cfg(), liveness=_leben(button.WATCHER_ALIVE, finding=False,
                                    device_readable=False),
            scanner_node=_knoten(None))
        assert r["ok"] is True
        assert r["usb_node"] is None

    async def test_wächter_absichtlich_aus(self):
        r = await tools.watcher_health(
            _Aus(), liveness=_leben(button.WATCHER_OFF, finding=False,
                                    device_readable=None),
            scanner_node=_knoten(DA))
        assert r["ok"] is True

    async def test_waechter_startet_noch(self):
        r = await tools.watcher_health(
            _Cfg(), liveness=_leben(button.WATCHER_STARTING, finding=False,
                                    device_readable=None),
            scanner_node=_knoten(DA))
        assert r["ok"] is True

    async def test_alles_gesund(self):
        r = await tools.watcher_health(
            _Cfg(), liveness=_leben(button.WATCHER_ALIVE, finding=False,
                                    device_readable=True),
            scanner_node=_knoten(DA))
        assert r["ok"] is True

    async def test_noch_kein_sensorabruf(self):
        """`device_readable is None` heißt „noch nichts gemessen", nicht
        „nicht lesbar"."""
        r = await tools.watcher_health(
            _Cfg(), liveness=_leben(button.WATCHER_ALIVE, finding=False,
                                    device_readable=None),
            scanner_node=_knoten(DA))
        assert r["ok"] is True

    async def test_ioreg_wirft_nimmt_die_probe_nicht_mit(self):
        async def kaputt():
            raise OSError("ioreg weg")
        r = await tools.watcher_health(
            _Cfg(), liveness=_leben(button.WATCHER_ALIVE, finding=False,
                                    device_readable=False),
            scanner_node=kaputt)
        assert r["ok"] is True
        assert r["usb_node"] is None


class TestDasWerkzeugFasstDasGeraetNICHTAn:
    """🛑 Die Probe läuft im Takt. Fasste sie das Gerät an, wäre sie selbst
    eine Last auf genau dem Anschluss, dessen Gesundheit sie messen soll — und
    nach dem 2026-10-21 ist der billige Sensorabruf der Hauptverdächtige."""

    async def test_kein_sane_aufruf(self, monkeypatch):
        async def verboten(*a, **k):
            raise AssertionError("watcher_health darf SANE nicht aufrufen")

        from renfield_mcp_scanner import sane
        monkeypatch.setattr(sane, "_run", verboten)
        monkeypatch.setattr(sane, "_probe", verboten)
        monkeypatch.setattr(sane, "find_device", verboten)
        monkeypatch.setattr(sane, "read_sensors", verboten)

        r = await tools.watcher_health(
            _Cfg(), liveness=_leben(button.WATCHER_ALIVE, finding=False,
                                    device_readable=True),
            scanner_node=_knoten(DA))
        assert r["ok"] is True

    async def test_scanner_status_fasst_es_SEHR_WOHL_an(self, monkeypatch):
        """Gegenprobe: ohne sie bewiese der Test oben nur, dass die Attrappen
        greifen, nicht dass die beiden Werkzeuge sich unterscheiden."""
        from renfield_mcp_scanner import sane

        async def verboten(*a, **k):
            raise AssertionError("ERWARTET: scanner_status ruft SANE auf")

        monkeypatch.setattr(sane, "find_device", verboten)
        with pytest.raises(AssertionError, match="ERWARTET"):
            await tools.scanner_status(
                _Cfg(), liveness=_leben(button.WATCHER_ALIVE, finding=False,
                                        device_readable=True))


# ---------------------------------------------------------------------------
# Verklemmung OHNE Wächter — sonst wäre sie unsichtbar
# ---------------------------------------------------------------------------

async def test_a_wedge_is_still_found_with_the_watcher_off():
    """🛑 Der Riegel gegen den Neun-Tage-Ausfall durch eine NEUE Tür.

    `device_readable` setzt nur der Wächter. Ist er aus — seit 2026-10-26 die
    Vorgabe — bleibt es `None`, und der alte `device_wedged`-Zweig kann nie
    feuern. Ohne Ersatz meldete die einzige Funktionssonde ab dann dauerhaft
    `ok`, auch im verklemmten Zustand: vom 22. bis 25.10. war das Gerät
    praktisch durchgehend verklemmt, 640–669 Fehlzeilen am Tag.
    """
    r = await tools.watcher_health(
        _Aus(),
        liveness=_leben(button.WATCHER_OFF, finding=False, device_readable=None),
        scanner_node=_knoten(DA),
        device_held=lambda: False,
        letzter_zugriff=lambda: {
            "ok": False, "detail": "no Fujitsu scanner found", "at": time.time() - 600},
    )
    assert r["ok"] is False
    assert r["error_code"] == "device_wedged_last_attempt"
    assert "no Fujitsu scanner found" in r["error"]
    # Die Meldung muss sagen, dass es der letzte VERSUCH war, keine Messung:
    # eine Vergangenheitsform darf keine Gegenwart behaupten.
    assert "letzte VERSUCH" in r["error"] or "letzter VERSUCH" in r["error"]


async def test_no_node_means_no_wedge_even_after_a_failed_access():
    """Fehlt der Knoten, ist der Scanner aus oder abgesteckt — auf einem
    Arbeitsplatzrechner der Normalfall, nie ein Befund."""
    r = await tools.watcher_health(
        _Aus(),
        liveness=_leben(button.WATCHER_OFF, finding=False, device_readable=None),
        scanner_node=_knoten(WEG),
        device_held=lambda: False,
        letzter_zugriff=lambda: {"ok": False, "detail": "weg", "at": time.time()},
    )
    assert r["ok"] is True


async def test_a_successful_last_access_is_no_finding():
    r = await tools.watcher_health(
        _Aus(),
        liveness=_leben(button.WATCHER_OFF, finding=False, device_readable=None),
        scanner_node=_knoten(DA),
        device_held=lambda: False,
        letzter_zugriff=lambda: {"ok": True, "detail": "", "at": time.time()},
    )
    assert r["ok"] is True


async def test_without_any_access_since_start_there_is_no_verdict():
    """Keine Auskunft darf nicht als Fehler gelesen werden — dieselbe Regel
    wie bei einem `ioreg`, das nichts sagt."""
    r = await tools.watcher_health(
        _Aus(),
        liveness=_leben(button.WATCHER_OFF, finding=False, device_readable=None),
        scanner_node=_knoten(DA),
        device_held=lambda: False,
        letzter_zugriff=lambda: None,
    )
    assert r["ok"] is True
    assert r["last_device_open"] is None
