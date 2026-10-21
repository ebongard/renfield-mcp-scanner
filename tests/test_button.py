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

from renfield_mcp_scanner import button, sane
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
        self.finds = 0
        self.node_calls = 0
        self.result: dict = {"job_id": "j1"}
        self.raise_on: set[int] = set()

    async def find_device(self, _hint):
        self.finds += 1
        return "fujitsu:test"

    async def scanner_node(self):
        # 🛑 Eingespritzt, damit kein Test `ioreg` aufruft. Ein Test, der die
        # Umgebung befragt, prüft nicht den Code, sondern den Rechner.
        self.node_calls += 1
        return None

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
            read_sensors=self.read_sensors, find_device=self.find_device, scanner_node=self.scanner_node,
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
            find_device=f.find_device, scanner_node=f.scanner_node, sleep=f.sleep, max_iterations=2,
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
        """🛑 Negativkontrolle. Eine saubere Rückkehr darf keinen Neustart
        auslösen.

        Der Pfad ist im Betrieb unerreichbar (`watch_button` kehrt ohne
        `max_iterations` nie zurück) — geprüft wird er trotzdem, weil ein
        künftiger Umbau ihn einbauen könnte und der Knopf dann still
        abgeschaltet wäre.
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

    async def test_es_laeuft_NIE_mehr_als_eine_schleife(self):
        """🛑 Die Gefahr, die der Docstring oben vorher nur BEHAUPTET hat.

        Im Review aufgefallen: „liefe der teure Sensortakt mehrfach" war gar
        nicht geprüft — und mit sequenziellem `await` strukturell auch nicht
        möglich. Also wird jetzt gemessen statt behauptet: der Attrappen-Wächter
        zählt die Gleichzeitigkeit mit und gibt dabei an die Ereignisschleife
        zurück, damit eine zweite Schleife überhaupt laufen KÖNNTE.

        Das fängt den Umbau, der den Neustart als eigene Task absetzt — dann
        liefen zwei Sensortakte parallel, und laut Modulkopf setzt genau das
        den Scanner fest.
        """
        import asyncio as _asyncio

        gleichzeitig = 0
        hoechststand = 0

        async def crashing(_config, _start_job):
            nonlocal gleichzeitig, hoechststand
            gleichzeitig += 1
            hoechststand = max(hoechststand, gleichzeitig)
            await _asyncio.sleep(0)      # Rückgabe an die Ereignisschleife
            gleichzeitig -= 1
            raise RuntimeError("weg")

        async def sleep(_s):
            await _asyncio.sleep(0)      # auch hier zurückgeben

        await button.supervise_button(
            _Config(), None, watcher=crashing, sleep=sleep, clock=lambda: 0.0,
            max_restarts=5,
        )
        assert hoechststand == 1, (
            f"es darf nie mehr als eine Wächter-Schleife laufen, gemessen: "
            f"{hoechststand}"
        )

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

    async def test_nach_einem_gesunden_lauf_beginnt_die_staffelung_neu(self):
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

        async def crashing(_config, _start_job):
            raise RuntimeError("weg")

        schlaf: list[float] = []

        async def sleep(s):
            schlaf.append(s)

        await button.supervise_button(
            _Config(), None, watcher=crashing, sleep=sleep, clock=uhr,
            max_restarts=4,
        )
        B = button.RESTART_BACKOFF
        # kurz   -> 1. Neustart        -> 1*B
        # kurz   -> 2. Neustart        -> 2*B
        # LANG   -> Zähler zurück, 1.  -> 1*B   <- der Beweis
        # kurz   -> 2.                 -> 2*B
        # kurz   -> 3.                 -> 3*B
        assert schlaf == [B, 2 * B, B, 2 * B, 3 * B], (
            f"nach einem gesunden Lauf muss die Staffelung neu beginnen, war {schlaf}"
        )

    async def test_ein_absturzkreis_erbt_die_staffelung_WEITER(self):
        """🛑 Negativkontrolle zur Rücksetzung: stürzt der Wächter SOFORT wieder
        ab, darf der Zähler NICHT zurückgesetzt werden — sonst hämmert ein
        Absturzkreis dauerhaft im 5-Sekunden-Takt und flutet das Protokoll.
        """
        async def crashing(_config, _start_job):
            raise RuntimeError("dauerhaft")

        schlaf: list[float] = []

        async def sleep(s):
            schlaf.append(s)

        await button.supervise_button(
            _Config(), None, watcher=crashing, sleep=sleep, clock=lambda: 0.0,
            max_restarts=4,
        )
        B = button.RESTART_BACKOFF
        assert schlaf == [B, 2 * B, 3 * B], f"Staffelung muss wachsen, war {schlaf}"


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


class TestDieAufsichtUmgehtDieSelbstbremseNICHT:
    """🛑 Der Befund aus dem Review vom 2026-10-11, und er wog schwerer als der
    Fehler, gegen den die Aufsicht gebaut ist.

    Ein neu gestarteter `watch_button` beginnt mit `device = None` und ruft
    deshalb sofort `find_device`. Produktiv ist `config.device` LEER
    (`SCANNER_DEVICE` setzt niemand), also läuft dort `scanimage -f` — die
    vollständige SANE-Suche, die den USB-Anschluss belegt. Gemessen am
    2026-10-11 am laufenden Dienst: 4,3 s zwischen „Knopfwächter aktiv" und
    „Gerät …".

    `watch_button` bremst sich dafür selbst: neu gesucht wird erst nach
    `REDISCOVER_AFTER` Fehlschlägen, also höchstens alle
    `REDISCOVER_AFTER * ERROR_BACKOFF` Sekunden — eine Grenze aus der Messung,
    die den Scanner am 2026-09-25 FESTGESETZT hat. Eine Aufsicht, die
    schneller neu startet, umgeht genau diese Bremse: aus „Knopf tot" würde
    „Gerät fest, dafür lautes Protokoll".
    """

    async def test_die_wartezeit_unterbietet_die_suchbremse_nie(self):
        async def crashing(_config, _start_job):
            raise RuntimeError("dauerhaft")

        schlaf: list[float] = []

        async def sleep(s):
            schlaf.append(s)

        await button.supervise_button(
            _Config(), None, watcher=crashing, sleep=sleep, clock=lambda: 0.0,
            max_restarts=6,
        )
        bremse = button.REDISCOVER_AFTER * button.ERROR_BACKOFF
        assert min(schlaf) >= bremse, (
            f"kein Neustart darf schneller kommen als die Suchbremse von "
            f"{bremse} s — gemessen wurde {min(schlaf)} s"
        )

    async def test_die_konstante_ist_an_die_bremse_GEBUNDEN_nicht_geraten(self):
        """Eine gleich grosse Zahl wäre Zufall und driftet beim nächsten Umbau
        auseinander. Die Wartezeit muss AUS der Bremse abgeleitet sein."""
        assert button.RESTART_BACKOFF == button.REDISCOVER_AFTER * button.ERROR_BACKOFF


class _Uhr2:
    """Steuerbare monotone Uhr: `jetzt` wird im Test von Hand gestellt."""

    def __init__(self, jetzt: float = 0.0):
        self.jetzt = jetzt

    def __call__(self) -> float:
        return self.jetzt


class _MitzaehlendeLebendigkeit(button.WatcherLiveness):
    """Zählt die Zeitmarken mit — „bei JEDEM Durchlauf" ist sonst nicht prüfbar."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.marken = 0

    def tick(self) -> None:
        self.marken += 1
        super().tick()


class TestDerWaechterbefundHaengtAnEinerZEITMARKE:
    """🛑 Die Falle aus #3, und sie ist der ganze Grund für dieses Feld.

    Die Task des Wächters EXISTIERTE sieben Tage lang, während der blaue Knopf
    ohne Wirkung war — `server.py` hielt sie in einer starken Referenz, und die
    Schleife darin war tot. Eine Auskunft, die aus dem Vorhandensein der Task
    oder aus `task.done()` abgeleitet wäre, hätte in diesen sieben Tagen „läuft"
    gemeldet. Geprüft wird darum ausschliesslich das ALTER einer Marke, die die
    Schleife selbst setzt.
    """

    async def test_eine_vorhandene_aber_TOTE_schleife_liest_als_tot(self):
        """Der gemessene Fall: ein Durchlauf, dann nie wieder. Keine Task wird
        dafür befragt — die Marke veraltet, und das genügt."""
        uhr = _Uhr2()
        leben = button.WatcherLiveness(clock=uhr)
        leben.loop_started()
        leben.tick()
        leben.note_device(True)

        uhr.jetzt = button.LIVENESS_STALE_AFTER + 1
        befund = leben.report(enabled=True)

        assert befund["state"] == button.WATCHER_DEAD
        assert befund["finding"] is True, "ein stehender Wächter IST ein Befund"
        assert befund["last_tick_age_seconds"] == button.LIVENESS_STALE_AFTER + 1

    async def test_ein_lebender_waechter_liest_als_lebend(self):
        """🛑 Gegenprobe: knapp INNERHALB der Grenze darf kein Befund stehen,
        sonst meldet die Probe bei jedem langsamen Durchlauf einen Ausfall."""
        uhr = _Uhr2()
        leben = button.WatcherLiveness(clock=uhr)
        leben.loop_started()
        leben.tick()
        leben.note_device(True)

        uhr.jetzt = button.LIVENESS_STALE_AFTER - 1
        befund = leben.report(enabled=True)

        assert befund["state"] == button.WATCHER_ALIVE
        assert befund["finding"] is False
        assert befund["since"], "das Feld fuer „seit wann“ muss gesetzt sein"

    async def test_ein_nicht_erreichbares_geraet_ist_KEIN_befund(self):
        """🛑 DIE wichtigste Gegenprobe. Ein ausgestöpselter oder schlafender
        Scanner ist der NORMALFALL auf einem Arbeitsplatzrechner. Hing die
        Marke am erfolgreichen Sensorabruf, läse genau dieser Normalfall als
        toter Wächter — und die Probe würde alarmieren, sobald der Rechner
        schläft.
        """
        leben = _MitzaehlendeLebendigkeit()
        f = _Fake([_sensors(paper=False)])
        f.raise_on = {0, 1, 2, 3, 4}          # das Gerät antwortet NIE
        await button.watch_button(
            _Config(), f.start_job, read_sensors=f.read_sensors,
            find_device=f.find_device, scanner_node=f.scanner_node, sleep=f.sleep, liveness=leben,
            max_iterations=5,
        )
        befund = leben.report(enabled=True)

        assert leben.marken == 5, (
            f"die Marke muss in JEDEM Durchlauf gesetzt werden, auch im "
            f"Sensorfehlerpfad — gezählt: {leben.marken}")
        assert befund["state"] == button.WATCHER_ALIVE
        assert befund["finding"] is False, (
            "ein nicht erreichbares Gerät darf keinen Alarm auslösen")
        assert befund["device_readable"] is False, (
            "unterscheidbar bleiben muss es trotzdem")
        assert befund["state"] != button.WATCHER_CRASHLOOP, (
            "und es ist erst recht kein Absturzkreis — die Schleife vollendet "
            "jeden Durchlauf, sie findet nur kein Gerät")

    async def test_ein_lesbares_geraet_wird_als_lesbar_gemeldet(self):
        """Gegenprobe zur Gegenprobe: ohne sie wäre `device_readable` auch
        dann `False`, wenn alles in Ordnung ist, und der Unterschied wertlos."""
        leben = _MitzaehlendeLebendigkeit()
        f = _Fake([_sensors(paper=False)] * 3)
        await button.watch_button(
            _Config(), f.start_job, read_sensors=f.read_sensors,
            find_device=f.find_device, scanner_node=f.scanner_node, sleep=f.sleep, liveness=leben,
            max_iterations=3,
        )
        befund = leben.report(enabled=True)
        assert leben.marken == 3
        assert befund["device_readable"] is True
        assert befund["state"] == button.WATCHER_ALIVE

    async def test_abgeschaltet_meldet_absichtlich_AUS_und_nicht_tot(self):
        """`SCANNER_BUTTON_WATCH=false` ist eine Entscheidung, kein Ausfall —
        etwa auf einem Host ohne angeschlossenes Gerät."""
        uhr = _Uhr2()
        leben = button.WatcherLiveness(clock=uhr)
        uhr.jetzt = 10 * button.LIVENESS_STALE_AFTER   # nie getickt, lange her

        befund = leben.report(enabled=False)

        assert befund["state"] == button.WATCHER_OFF
        assert befund["state"] != button.WATCHER_DEAD
        assert befund["finding"] is False
        assert "SCANNER_BUTTON_WATCH" in befund["detail"]

    async def test_nie_getickt_liest_erst_startend_und_dann_tot(self):
        """Zwei Fälle in einem: die erste Gerätesuche dauert messbar (4,3 s am
        2026-10-11), also ist „noch keine Marke" kurz nach dem Start normal.
        🛑 Bleibt sie dauerhaft aus — eine Verdrahtung, die den Wächter gar
        nicht erst startet —, muss daraus ein Befund werden statt für immer
        „startet gerade".
        """
        uhr = _Uhr2()
        leben = button.WatcherLiveness(clock=uhr)

        uhr.jetzt = 5.0
        assert leben.report(enabled=True)["state"] == button.WATCHER_STARTING
        assert leben.report(enabled=True)["finding"] is False

        uhr.jetzt = button.LIVENESS_STALE_AFTER + 1
        spaet = leben.report(enabled=True)
        assert spaet["state"] == button.WATCHER_DEAD
        assert spaet["finding"] is True

    async def test_die_neustarts_der_aufsicht_stehen_im_befund(self):
        """Die Aufsicht ist die einzige Stelle, die sie kennt. Die Lebenssumme
        steht neben dem Zähler aus der Meldung, weil der nach einem gesunden
        Lauf neu beginnt und die Summe sonst untertreibt."""
        leben = button.WatcherLiveness()

        async def crashing(_config, _start_job):
            raise RuntimeError("dauerhaft")

        async def sleep(_s):
            pass

        await button.supervise_button(
            _Config(), None, watcher=crashing, sleep=sleep, clock=lambda: 0.0,
            liveness=leben, max_restarts=3,
        )
        befund = leben.report(enabled=True)
        assert befund["restarts_total"] == 3
        assert befund["restart_streak"] == 3

    def test_die_grenze_ist_aus_der_laengsten_LUECKE_abgeleitet(self):
        """🛑 Eine geratene Zahl driftet beim nächsten Umbau.

        Review-Befund L2 vom 2026-10-11: die erste Fassung leitete allein aus
        der Wartezeit der Aufsicht ab und liess die beiden SANE-Zeitgrenzen
        aus. Ein hängendes `scanimage` zieht die Lücke aber über diese
        Wartezeit hinaus, OHNE dass der Wächter steht — und genau dann hätte
        das Feld einen Ausfall erfunden.
        """
        assert (button.LIVENESS_STALE_AFTER
                == button.LIVENESS_FACTOR * button.LIVENESS_WORST_GAP)
        assert button.LIVENESS_WORST_GAP == (
            button.RESTART_BACKOFF_MAX + 2 * sane.PROBE_TIMEOUT
            + button.ERROR_BACKOFF), "die Summanden müssen die Lücke ERGEBEN"
        for takt in (button.IDLE_INTERVAL, button.ARMED_INTERVAL,
                     button.ERROR_BACKOFF, button.RESTART_BACKOFF,
                     button.RESTART_BACKOFF_MAX):
            assert button.LIVENESS_STALE_AFTER > takt, (
                f"die Grenze muss den Takt {takt} s vertragen, sonst ist jeder "
                f"normale Zustand ein Fehlalarm")

    def test_eine_haengende_SANE_abfrage_erfindet_keinen_ausfall(self):
        """Der schlimmste vorgesehene Fall in einer Zahl: ein Neustart der
        Aufsicht, danach eine hängende Gerätesuche UND ein hängender
        Sensorabruf, beide bis an ihre Zeitgrenze. Dabei ist nichts kaputt —
        also darf die Grenze darüber liegen."""
        schlimmste_luecke = (button.RESTART_BACKOFF_MAX + 2 * sane.PROBE_TIMEOUT
                             + button.ERROR_BACKOFF)
        assert button.LIVENESS_STALE_AFTER > schlimmste_luecke, (
            f"{button.LIVENESS_STALE_AFTER} s Grenze gegen {schlimmste_luecke} s "
            f"Lücke — eine hängende Abfrage würde als toter Wächter gelesen")

    def test_die_SANE_grenze_steht_nur_EINMAL_als_zahl(self):
        """Die Ableitung ist nur so gut wie ihre Quelle: lesen die Abfragen
        ihre Zeitgrenze nicht aus derselben Konstante, läuft die Rechnung beim
        nächsten Umbau still auseinander."""
        import inspect

        for funktion in (sane.find_device, sane.read_sensors):
            quelle = inspect.getsource(funktion)
            assert "timeout=PROBE_TIMEOUT" in quelle, (
                f"{funktion.__name__} muss die benannte Konstante benutzen")

    def test_die_marke_kommt_aus_der_SCHLEIFE_nicht_aus_einer_task(self):
        """Strukturprüfung auf die Invariante aus #3: setzt die Schleife die
        Marke nicht selbst, ist jede Auskunft wieder eine Vermutung über eine
        Task — und die war sieben Tage lang falsch."""
        import inspect

        schleife = inspect.getsource(button.watch_button)
        assert ".tick()" in schleife
        bericht = inspect.getsource(button.WatcherLiveness.report)
        for wort in ("task", "done()", "cancelled"):
            assert wort not in bericht, (
                f"der Befund darf nichts über eine Task wissen ({wort!r})")

    def test_beide_greifen_vorgabeweise_auf_DASSELBE_exemplar(self):
        """Ein Prozess, ein Wächter. Griffen Schleife und Aufsicht auf
        verschiedene Exemplare, stünden im Befund zwei halbe Auskünfte."""
        import inspect

        for funktion in (button.watch_button, button.supervise_button):
            quelle = inspect.getsource(funktion)
            assert "else LIVENESS" in quelle, (
                f"{funktion.__name__} muss auf das Modul-Exemplar zurückfallen")


class TestEinAbsturzkreisIstKeinLEBEN:
    """🛑 Der Befund der gegnerischen Durchsicht (2026-10-11), und er traf
    genau den Fall, für den das Lebenszeichen existiert: ein dauerhaft
    abstürzender Wächter las als `alive`.

    Ursache: die Marke stand am ANFANG des Durchlaufs und bedeutete damit nur
    „die Schleife wurde betreten". Ein Absturzkreis betritt sie ständig — die
    Aufsicht startet neu, der Wächter betritt, die Marke ist frisch. Der blaue
    Knopf wirkte dabei höchstens zeitweise.

    Zwei Änderungen, und beide werden hier einzeln überführt: die Marke
    bedeutet jetzt „Durchlauf VOLLENDET", und eine frische Neustartfolge ist
    selbst ein Befund.
    """

    async def test_ein_abgebrochener_durchlauf_setzt_KEINE_marke(self):
        """Der Abbruch an der einzigen unbewachten Stelle, die #3 benannt hat:
        `start_job` liefert etwas, das kein Wörterbuch ist, und `result.get`
        wirft NACH dem Scan-Start. Mit der Marke am Anfang war sie trotzdem
        frisch — der Durchlauf war es nicht."""
        leben = _MitzaehlendeLebendigkeit()
        f = _Fake([_sensors(pressed=True, paper=True)])

        async def start_job(**_kw):
            return None          # kein Wörterbuch -> AttributeError bei .get

        with pytest.raises(AttributeError):
            await button.watch_button(
                _Config(), start_job, read_sensors=f.read_sensors,
                find_device=f.find_device, scanner_node=f.scanner_node, sleep=f.sleep, liveness=leben,
                max_iterations=1,
            )

        assert leben.marken == 0, (
            "ein Durchlauf, der mitten drin abbricht, ist nicht vollendet")
        assert leben.report(enabled=True)["state"] != button.WATCHER_ALIVE

    async def test_ein_absturzkreis_MIT_frischen_marken_ist_ein_befund(self):
        """Der härtere Fall: der Kreis vollendet einzelne Durchläufe, die Marke
        ist also frisch. Trotzdem wirkt der Knopf höchstens zeitweise — die
        Marke allein kann das nicht ausdrücken."""
        leben = button.WatcherLiveness()

        async def crashing(_config, _start_job):
            leben.tick()                 # ein vollendeter Durchlauf …
            raise RuntimeError("weg")    # … und dann der Absturz

        async def sleep(_s):
            pass

        await button.supervise_button(
            _Config(), None, watcher=crashing, sleep=sleep, clock=lambda: 0.0,
            liveness=leben, max_restarts=3,
        )
        befund = leben.report(enabled=True)

        assert befund["last_tick_age_seconds"] is not None, (
            "die Vorbedingung des Tests: die Marke IST frisch")
        assert befund["state"] == button.WATCHER_CRASHLOOP
        assert befund["finding"] is True, (
            "ein dauerhaft abstürzender Wächter ist ein Befund, kein Leben")
        assert befund["restart_streak"] >= button.CRASHLOOP_RESTARTS

    async def test_EIN_neustart_nach_einem_gesunden_lauf_ist_KEIN_befund(self):
        """🛑 Negativkontrolle. Die Aufsicht setzt die Folge nach einem
        gesunden Lauf zurück; ein einzelner Absturz nach Monaten Betrieb steht
        als Rückverfolgung im Protokoll, und die Schleife läuft. Würde das
        alarmieren, wäre die Alarmierung binnen Wochen Rauschen."""
        leben = button.WatcherLiveness()
        leben.note_restart(1)
        leben.tick()
        leben.note_device(True)

        befund = leben.report(enabled=True)
        assert befund["state"] == button.WATCHER_ALIVE
        assert befund["finding"] is False

    async def test_eine_ALTE_neustartfolge_ist_kein_befund(self):
        """🛑 Negativkontrolle zur Frische. Die Folge wird von der Aufsicht nur
        im Absturzzweig zurückgesetzt — ein Dienst, der zweimal beim Start
        abstürzte und danach monatelang läuft, trägt die Zwei für immer. Ohne
        den Zeitpunkt des letzten Neustarts läse er für immer als Absturzkreis.
        """
        uhr = _Uhr2()
        leben = button.WatcherLiveness(clock=uhr)
        leben.note_restart(3)                 # die Unruhe von damals
        uhr.jetzt = button.LIVENESS_STALE_AFTER + 1
        leben.tick()                          # … und seither läuft er
        leben.note_device(True)

        befund = leben.report(enabled=True)
        assert befund["state"] == button.WATCHER_ALIVE
        assert befund["finding"] is False
        assert befund["restarts_total"] == 1, "die Historie bleibt sichtbar"
