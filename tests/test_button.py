"""Der Knopfwächter: Flanke, Doppeltakt, und Überleben ohne Gerät.

Diese vier Eigenschaften sind die, an denen ein Poll-Wächter scheitert, und
keine davon fällt im Betrieb sofort auf:

* Zustand statt Flanke -> ein gehaltener Knopf startet Scan um Scan.
* Ein Takt für alles -> entweder das Gerät wird festgesetzt (gemessen: bei
  100-ms-Takt nach einer halben Stunde), oder der ~200-ms-Impuls geht verloren.
* Ein Sensorfehler, der durchschlägt -> der ausgestöpselte Scanner reisst den
  ganzen MCP-Dienst mit.
* `busy` als Fehler behandelt -> Protokolllärm bei einem völlig normalen Fall.
"""
from __future__ import annotations

import pytest

from renfield_mcp_scanner import button
from renfield_mcp_scanner.sane import Sensors


class _Config:
    device = "fujitsu:test"


def _sensors(*, pressed: bool = False, paper: bool = False) -> Sensors:
    return Sensors(page_loaded=paper, scan_button=pressed)


class _Fake:
    """Spielt eine Folge von Sensorzuständen ab und schreibt Aufrufe mit."""

    def __init__(self, states):
        self.states = list(states)
        self.sleeps: list[float] = []
        self.jobs: list[dict] = []
        self.reads = 0
        self.result: dict = {"job_id": "j1"}
        self.raise_on: set[int] = set()

    async def find_device(self, _hint):
        return "fujitsu:test"

    async def read_sensors(self, _device):
        i = self.reads
        self.reads += 1
        if i in self.raise_on:
            raise RuntimeError("Gerät weg")
        return self.states[min(i, len(self.states) - 1)]

    async def sleep(self, seconds):
        self.sleeps.append(seconds)

    async def start_job(self, **kw):
        self.jobs.append(kw)
        return self.result

    async def run(self, iterations):
        await button.watch_button(
            _Config(), self.start_job,
            read_sensors=self.read_sensors, find_device=self.find_device,
            sleep=self.sleep, max_iterations=iterations,
        )


class TestEdgeNotLevel:
    async def test_a_held_button_starts_exactly_one_scan(self):
        # Der Sensor meldet den Momentanzustand. Wird der Knopf gehalten, sieht
        # der Wächter viele `yes` hintereinander — das ist EIN Druck.
        f = _Fake([_sensors(pressed=True, paper=True)] * 6)
        await f.run(6)
        assert len(f.jobs) == 1

    async def test_two_separate_presses_start_two_scans(self):
        f = _Fake([
            _sensors(pressed=True, paper=True),
            _sensors(pressed=False, paper=True),
            _sensors(pressed=True, paper=True),
        ])
        await f.run(3)
        assert len(f.jobs) == 2

    async def test_no_press_starts_nothing(self):
        f = _Fake([_sensors(paper=True)] * 5)
        await f.run(5)
        assert f.jobs == []

    async def test_the_job_runs_under_its_own_caller(self):
        # Damit ein Knopf-Scan in Protokollen unterscheidbar ist UND per
        # SCANNER_CALLER_TARGET_BUTTON fest zugeordnet werden kann.
        f = _Fake([_sensors(pressed=True, paper=True)])
        await f.run(1)
        assert f.jobs[0]["caller"] == button.BUTTON_CALLER
        # KEIN Ziel: ein Knopfdruck trägt keine Absicht. Bei mehreren Zielen
        # landet der Stapel bewusst `unrouted` statt in einer Vermutung.
        assert f.jobs[0]["target"] == ""


class TestTwoRates:
    async def test_without_paper_it_polls_slowly(self):
        f = _Fake([_sensors(paper=False)] * 3)
        await f.run(3)
        assert f.sleeps == [button.IDLE_INTERVAL] * 3

    async def test_with_paper_it_polls_fast(self):
        f = _Fake([_sensors(paper=True)] * 3)
        await f.run(3)
        assert f.sleeps == [button.ARMED_INTERVAL] * 3

    async def test_the_armed_rate_stays_between_both_measured_bounds(self):
        # Der ~200-ms-Impuls muss erfasst werden — aber der Takt darf das Gerät
        # auch nicht festsetzen. Beide Grenzen sind gemessen (Modulkopf): bei
        # 100 ms belegte der Wächter den Scanner zu 42 % und wedgte ihn nach
        # einer halben Stunde. Deshalb eine UNTERE Schranke, nicht nur eine
        # obere — "zur Sicherheit schneller" ist hier der Schaden.
        assert button.ARMED_INTERVAL <= 0.25   # faengt den ~200-ms-Impuls
        assert button.ARMED_INTERVAL >= 0.15   # und setzt das Geraet nicht fest
        assert button.IDLE_INTERVAL >= button.ARMED_INTERVAL * 4


class TestSurvival:
    async def test_a_sensor_error_does_not_escape(self):
        # Der ausgestöpselte oder schlafende Scanner ist ein normaler Zustand.
        f = _Fake([_sensors(paper=True)])
        f.raise_on = {0, 1}
        await f.run(3)   # wirft nicht
        assert f.sleeps[:2] == [button.ERROR_BACKOFF] * 2

    async def test_it_recovers_and_still_sees_a_press(self):
        f = _Fake([_sensors(paper=True), _sensors(pressed=True, paper=True)])
        f.raise_on = {0}
        await f.run(3)
        assert len(f.jobs) == 1

    async def test_a_press_during_an_error_is_not_replayed_afterwards(self):
        # Nach einem Fehler ist der vorherige Zustand unbekannt. Er darf NICHT
        # als "war gedrückt" weiterwirken, sonst fehlt die Flanke beim nächsten
        # echten Druck — oder es entsteht eine erfundene.
        f = _Fake([_sensors(pressed=True, paper=True)])
        f.raise_on = {1}
        await f.run(3)
        assert len(f.jobs) == 2   # vor dem Fehler einer, danach die neue Flanke

    async def test_a_failing_start_does_not_kill_the_watcher(self):
        async def boom(**_kw):
            raise RuntimeError("Auftrag abgelehnt")

        f = _Fake([_sensors(pressed=True, paper=True), _sensors(paper=True)])
        await button.watch_button(
            _Config(), boom, read_sensors=f.read_sensors,
            find_device=f.find_device, sleep=f.sleep, max_iterations=2,
        )
        assert len(f.sleeps) == 2   # lief weiter

    async def test_busy_is_not_treated_as_a_failure(self):
        f = _Fake([_sensors(pressed=True, paper=True)])
        f.result = {"ok": False, "busy": True, "message": "läuft schon"}
        await f.run(1)
        assert len(f.jobs) == 1     # versucht, und ruhig hingenommen


class TestDieAufsichtHaeltDenWaechterAmLeben:
    """🛑 Der Ausfall vom 2026-10-11: eine unvorhergesehene Ausnahme beendete
    die Wächter-Aufgabe, und weil `server.py` sie in einer starken Referenz
    hielt, ohne sie abzuwarten, unterdrückte das Pythons eingebaute Warnung.

    Gemessen: nach dem Papierstau vom 04.10. schrieb der Wächter SIEBEN TAGE
    keine Zeile. Sein Fehlerpfad drosselt auf eine Warnung je Minute — ein
    dauerhaft fehlendes Gerät hätte Tausende erzeugt. Es waren null.
    """

    async def test_ein_absturz_startet_die_schleife_neu(self):
        laeufe = 0

        async def crashing(_config, _start_job):
            nonlocal laeufe
            laeufe += 1
            raise RuntimeError("unvorhergesehen")

        schlaf: list[float] = []

        async def sleep(s):
            schlaf.append(s)

        await button.supervise_button(
            _Config(), None, watcher=crashing, sleep=sleep, max_restarts=3,
        )
        assert laeufe == 3, "die Schleife muss nach einem Absturz neu anlaufen"
        assert schlaf == [button.RESTART_BACKOFF, button.RESTART_BACKOFF * 2], (
            f"die Wartezeit muss wachsen, war {schlaf}"
        )

    async def test_die_wartezeit_ist_gedeckelt(self):
        """Ohne Deckel würde ein langer Absturzkreis beliebig träge; mit Deckel
        bleibt es bei einer Zeile je Minute — derselbe Takt wie im
        Sensorfehlerpfad."""
        async def crashing(_config, _start_job):
            raise RuntimeError("dauerhaft")

        schlaf: list[float] = []

        async def sleep(s):
            schlaf.append(s)

        await button.supervise_button(
            _Config(), None, watcher=crashing, sleep=sleep, max_restarts=40,
        )
        assert max(schlaf) == button.RESTART_BACKOFF_MAX
        assert all(s <= button.RESTART_BACKOFF_MAX for s in schlaf)

    async def test_ein_gesunder_waechter_wird_NICHT_neu_gestartet(self):
        """🛑 Negativkontrolle, und die wichtigere Hälfte.

        Startete die Aufsicht auch ohne Absturz neu, liefe der teure Sensortakt
        unkontrolliert mehrfach — und der Test oben wäre trotzdem grün.
        """
        laeufe = 0

        async def healthy(_config, _start_job):
            nonlocal laeufe
            laeufe += 1
            return None          # wie `watch_button` mit max_iterations

        schlaf: list[float] = []

        async def sleep(s):
            schlaf.append(s)

        await button.supervise_button(
            _Config(), None, watcher=healthy, sleep=sleep, max_restarts=5,
        )
        assert laeufe == 1, "ohne Absturz darf nicht neu gestartet werden"
        assert schlaf == [], "und es darf dafür nicht gewartet werden"

    async def test_abbruch_laeuft_durch(self):
        """`asyncio.CancelledError` ist das geordnete Ende beim Herunterfahren.
        Fängt die Aufsicht sie, hängt der Dienst beim Beenden."""
        import asyncio as _asyncio

        async def cancelled(_config, _start_job):
            raise _asyncio.CancelledError()

        with pytest.raises(_asyncio.CancelledError):
            await button.supervise_button(
                _Config(), None, watcher=cancelled, max_restarts=1,
            )

    async def test_nach_einem_gesunden_lauf_beginnt_die_staffelung_neu(self, monkeypatch):
        """Die Wartezeit soll die AKTUELLE Unruhe abbilden.

        Ohne Rücksetzung erbt ein Dienst, der monatelang läuft und dreimal
        abstürzt, dauerhaft den Deckel von 60 s — und die Zählung in der Meldung
        wirft Monate zusammen. Hier läuft der Wächter zwischen den Abstürzen
        länger als der Deckel, also muss die Staffelung wieder bei 5 s anfangen.
        """
        # 🛑 Der gesunde Lauf muss MITTENDRIN liegen, nicht am Anfang. Meine
        # erste Fassung prüfte den ERSTEN Absturz — dort steht der Zähler
        # ohnehin auf 0, die Rücksetzung war also wirkungslos und der Test
        # blieb auch ohne sie grün. Die Negativkontrolle hat das aufgedeckt.
        #
        # Folge der Laufdauern: kurz, kurz, LANG, kurz, … Die Uhr wird zweimal
        # je Lauf abgefragt (Start und Absturz) und wiederholt die letzte Dauer.
        uhr = _Uhr([1.0, 1.0, button.RESTART_BACKOFF_MAX + 1, 1.0])
        monkeypatch.setattr(button.time, "monotonic", uhr)

        async def crashing(_config, _start_job):
            raise RuntimeError("weg")

        schlaf: list[float] = []

        async def sleep(s):
            schlaf.append(s)

        await button.supervise_button(
            _Config(), None, watcher=crashing, sleep=sleep, max_restarts=4,
        )
        # kurz   -> 1. Neustart            ->  5 s
        # kurz   -> 2. Neustart            -> 10 s
        # LANG   -> Zähler zurück, 1.      ->  5 s   <- der Beweis
        # kurz   -> 2.                     -> 10 s
        # kurz   -> 3.                     -> 15 s
        assert schlaf == [5.0, 10.0, 5.0, 10.0, 15.0], (
            f"nach einem gesunden Lauf muss die Staffelung neu beginnen, war {schlaf}"
        )

    async def test_ein_absturzkreis_erbt_die_staffelung_WEITER(self, monkeypatch):
        """🛑 Negativkontrolle zur Rücksetzung: stürzt der Wächter SOFORT wieder
        ab, darf der Zähler NICHT zurückgesetzt werden — sonst hämmert ein
        Absturzkreis dauerhaft im 5-Sekunden-Takt und flutet das Protokoll.
        """
        monkeypatch.setattr(button.time, "monotonic", lambda: 0.0)

        async def crashing(_config, _start_job):
            raise RuntimeError("dauerhaft")

        schlaf: list[float] = []

        async def sleep(s):
            schlaf.append(s)

        await button.supervise_button(
            _Config(), None, watcher=crashing, sleep=sleep, max_restarts=4,
        )
        assert schlaf == [5.0, 10.0, 15.0], f"Staffelung muss wachsen, war {schlaf}"


class _Uhr:
    """Monotone Uhr mit vorgegebenen Laufdauern, je Lauf zwei Abfragen."""

    def __init__(self, laufdauern: list[float]):
        self.laufdauern = list(laufdauern)
        self.aufrufe = 0

    def __call__(self) -> float:
        lauf = self.aufrufe // 2
        start_abfrage = self.aufrufe % 2 == 0
        self.aufrufe += 1
        if start_abfrage:
            return 0.0
        return self.laufdauern[min(lauf, len(self.laufdauern) - 1)]
