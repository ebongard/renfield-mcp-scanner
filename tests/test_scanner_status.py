"""`scanner_status` berichtet die HARDWARE — und ob der Knopfwächter lebt.

🛑 Warum die zweite Hälfte dazugehört (Issue #5): nach #3 ist der Tod des
Wächters LAUT (eine ERROR-Zeile mit Rückverfolgung), aber er MELDET sich nicht.
Eine Protokollzeile wartet, bis jemand nachsieht; beim Ausfall vom 2026-10-04
hat sieben Tage niemand nachgesehen, und genau das war der Schaden. Eine
funktionale Probe, die dieses Werkzeug aufruft, findet es selbst.

🛑 Und die Falle, in der jede naive Fassung landet: ein schlafender oder
ausgestöpselter Scanner ist der NORMALFALL — der Dienst läuft auf einem
Arbeitsplatzrechner. Daran darf der Wächterbefund nicht hängen, und er darf vor
allem nicht mit der Fehlerantwort verlorengehen.
"""
from __future__ import annotations

import pytest

from renfield_mcp_scanner import button, sane, tools
from renfield_mcp_scanner.config import Config, ScanTarget


def _cfg(tmp_path, *, button_watch: bool = True) -> Config:
    return Config(
        targets=[ScanTarget(id="household", label="Haushalt", base_url="https://hh:8443",
                            token_env="TOK_HH", scan_profile_id="p")],
        staging_dir=tmp_path / "st",
        button_watch=button_watch,
    )


def _lebender_waechter() -> button.WatcherLiveness:
    leben = button.WatcherLiveness()
    leben.loop_started()
    leben.tick()
    leben.note_device(True)
    return leben


def _toter_waechter() -> button.WatcherLiveness:
    """Ein Wächter, dessen Marke veraltet ist — der Fall vom 2026-10-04."""
    uhr = type("_Uhr", (), {"jetzt": 0.0, "__call__": lambda self: self.jetzt})()
    leben = button.WatcherLiveness(clock=uhr)
    leben.loop_started()
    leben.tick()
    uhr.jetzt = button.LIVENESS_STALE_AFTER + 1
    return leben


@pytest.fixture
def geraet_da(monkeypatch):
    async def find_device(_hint):
        return "fujitsu:test"

    async def read_sensors(_device):
        return sane.Sensors(page_loaded=True)

    monkeypatch.setattr(sane, "find_device", find_device)
    monkeypatch.setattr(sane, "read_sensors", read_sensors)


@pytest.fixture
def geraet_weg(monkeypatch):
    """Der Normalfall auf einem Arbeitsplatzrechner: der Rechner schläft, oder
    der Scanner hängt nicht am USB-Bus."""
    async def find_device(_hint):
        raise sane.ScannerError("no Fujitsu scanner found")

    monkeypatch.setattr(sane, "find_device", find_device)


class TestDerWaechterbefundIstTeilDerAuskunft:

    async def test_ein_lebender_waechter_steht_in_der_antwort(self, tmp_path, geraet_da):
        antwort = await tools.scanner_status(_cfg(tmp_path), liveness=_lebender_waechter())

        assert antwort["ok"] is True
        assert antwort["button_watcher"]["state"] == button.WATCHER_ALIVE
        assert antwort["button_watcher"]["finding"] is False

    async def test_ein_toter_waechter_ist_ein_befund(self, tmp_path, geraet_da):
        antwort = await tools.scanner_status(_cfg(tmp_path), liveness=_toter_waechter())

        assert antwort["button_watcher"]["state"] == button.WATCHER_DEAD
        assert antwort["button_watcher"]["finding"] is True

    async def test_die_hardwarefelder_bleiben_unberuehrt(self, tmp_path, geraet_da):
        """Gegenprobe: das neue Feld darf die bisherige Auskunft nicht
        verdrängen — eine Probe und ein Agent lesen beide weiter."""
        antwort = await tools.scanner_status(_cfg(tmp_path), liveness=_lebender_waechter())

        for feld in ("device", "ready", "paper_loaded", "cover_open",
                     "power_save", "double_feed", "error_code"):
            assert feld in antwort, f"{feld} fehlt"


class TestEinSchlafenderScannerDarfDenBefundNichtVERDECKEN:
    """🛑 Der Kern von #5. Ohne diese beiden Tests ist die Probe genau dann
    blind, wenn es darauf ankommt: `scanner_status` kehrt bei fehlendem Gerät
    FRÜH zurück, und der Wächterbefund wäre dann gar nicht in der Antwort.
    """

    async def test_ohne_geraet_steht_der_befund_TROTZDEM_in_der_antwort(
            self, tmp_path, geraet_weg):
        antwort = await tools.scanner_status(_cfg(tmp_path), liveness=_toter_waechter())

        assert antwort["ok"] is False, "das Gerät fehlt, das bleibt eine Fehlerantwort"
        assert "button_watcher" in antwort, (
            "der Wächterbefund MUSS die Fehlerantwort mitreiten — sonst sieht "
            "eine Probe den toten Wächter nie, solange der Rechner schläft")
        assert antwort["button_watcher"]["finding"] is True

    async def test_ohne_geraet_aber_mit_lebendem_waechter_ist_kein_befund(
            self, tmp_path, geraet_weg):
        """Die Gegenrichtung, und sie ist die häufigere: „Dienst erreichbar,
        Gerät nicht" darf keinen Alarm erzeugen."""
        antwort = await tools.scanner_status(_cfg(tmp_path), liveness=_lebender_waechter())

        assert antwort["ok"] is False
        assert antwort["button_watcher"]["state"] == button.WATCHER_ALIVE
        assert antwort["button_watcher"]["finding"] is False


class TestAbgeschaltetIstKeinAusfall:

    async def test_button_watch_aus_meldet_absichtlich_aus(self, tmp_path, geraet_da):
        """`SCANNER_BUTTON_WATCH=false` ist eine Entscheidung. Der Wächter
        tickt dann nie — eine Auskunft, die allein auf das Alter der Marke
        schaut, würde ihn für tot erklären und dauerhaft alarmieren."""
        antwort = await tools.scanner_status(
            _cfg(tmp_path, button_watch=False), liveness=_toter_waechter())

        assert antwort["button_watcher"]["state"] == button.WATCHER_OFF
        assert antwort["button_watcher"]["finding"] is False
