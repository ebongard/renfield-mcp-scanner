"""Die Verdrahtung des Knopfwächters im Server — sie WAR der Ausfall.

🛑 Am 2026-10-11 im Review aufgefallen: `tests/` berührte `server.py` an dieser
Stelle mit keiner Zeile. Genau dort lag der Fehler, der den blauen Knopf sieben
Tage lahmgelegt hat — die Task wurde als nackter `create_task` gestartet, in
einer starken Referenz gehalten und nie abgewartet, wodurch Pythons „Task
exception was never retrieved" nie feuern konnte.

Ein Test auf die Schleife allein hätte das nie gefunden. Diese Datei prüft
darum die NAHT, nicht den Wächter.
"""
from __future__ import annotations

import asyncio
import inspect
import logging

import pytest

from renfield_mcp_scanner import server



async def _fertige_aufgabe(coro) -> asyncio.Task:
    """Eine Task, die schon durch ist — nur so feuert der Rückruf."""
    task = asyncio.ensure_future(coro)
    try:
        await task
    except BaseException:
        pass
    return task


class TestDerAbschlussrueckruf:

    async def test_eine_ausnahme_wird_als_fehler_protokolliert(self, caplog):
        """Der Fall, auf den alles ankommt: ohne diese Zeile ist das Ende der
        Aufgabe unsichtbar, weil die starke Referenz Pythons eigene Warnung
        unterdrückt."""
        async def boom():
            raise RuntimeError("unvorhergesehen")

        task = await _fertige_aufgabe(boom())
        with caplog.at_level(logging.DEBUG):
            server._button_task_done(task)

        fehler = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert fehler, "eine Ausnahme in der Aufgabe MUSS protokolliert werden"
        assert "unvorhergesehen" in caplog.text
        assert fehler[0].exc_info is not None, (
            "ohne Rückverfolgung wäre die Ursache so unauffindbar wie vorher"
        )

    async def test_ein_abbruch_ist_KEIN_befund(self, caplog):
        """🛑 Negativkontrolle. Beim Herunterfahren bricht `_serve` die Aufgabe
        selbst ab. Würde das protokolliert, stünde bei jedem geordneten Stop
        eine Fehlermeldung — und echte Befunde gingen darin unter."""
        async def endlos():
            await asyncio.sleep(3600)

        task = asyncio.ensure_future(endlos())
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        with caplog.at_level(logging.DEBUG):
            server._button_task_done(task)
        assert caplog.records == [], (
            f"ein Abbruch darf nichts protokollieren, es kam: {caplog.text}"
        )

    async def test_ein_stilles_ende_wird_gewarnt(self, caplog):
        """Im Betrieb unmöglich — aber falls ein Umbau je einen Rückweg ohne
        Ausnahme einbaut, soll er auffallen statt den Knopf still abzuschalten."""
        async def einfach_fertig():
            return None

        task = await _fertige_aufgabe(einfach_fertig())
        with caplog.at_level(logging.DEBUG):
            server._button_task_done(task)

        warnungen = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warnungen, "ein Ende ohne Ausnahme muss auffallen"


class TestDieNahtImServer:
    """Strukturprüfung. `_serve` ist eine lange Funktion mit einem
    uvicorn-Server darin und in einem Test nicht ausführbar — die Naht ist aber
    genau die Stelle, an der der Fehler lag. Also wird sie am Quelltext
    festgenagelt, statt ungeprüft zu bleiben."""

    def test_der_server_startet_die_AUFSICHT_nicht_den_waechter_direkt(self):
        quelle = inspect.getsource(server._serve)
        assert "supervise_button(" in quelle, (
            "`_serve` muss die Aufsicht starten; `watch_button` direkt zu "
            "starten war der Ausfall vom 2026-10-04"
        )
        assert "watch_button(" not in quelle, (
            "`watch_button` darf nicht mehr direkt gestartet werden"
        )

    def test_die_aufgabe_bekommt_einen_abschlussrueckruf(self):
        quelle = inspect.getsource(server._serve)
        assert "add_done_callback(_button_task_done)" in quelle, (
            "ohne Rückruf unterdrückt die starke Referenz Pythons "
            "'Task exception was never retrieved' — genau so starb der "
            "Knopf stumm"
        )

    def test_die_aufgabe_wird_beim_herunterfahren_abgebrochen(self):
        quelle = inspect.getsource(server._serve)
        assert "button_task.cancel()" in quelle, (
            "ohne Abbruch hängt das Herunterfahren an der Endlosschleife"
        )
