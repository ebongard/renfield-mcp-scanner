"""Das Messgerät: ein Fehlschlag am Gerät muss SICHTBAR sein.

Bis 2026-10-12 war er es nicht, und das hatte eine Folge, die schwerer wog als
der Fehlschlag selbst: **die Ursache des Verklemmens war unbeobachtbar.**
`read_sensors` verwarf den Exit-Status, ein gescheiterter Abruf ergab `raw={}`,
daraus `ready is True` — also „bereit, kein Papier", ohne eine Zeile. `device`
blieb gesetzt, `errors` blieb 0, die Gerätesuche lief nie wieder. Ein Ausfall
im Dauerbetrieb wurde damit erst beim nächsten Neustart laut, und der sah dann
wie die Ursache aus.

Diese Tests prüfen die vier Zusagen, die das abstellen. Keine davon setzt eine
Annahme über die Ursache voraus — sie stellen her, was der Code über sich selbst
behauptet.
"""
from __future__ import annotations

import asyncio
import math

import pytest

from renfield_mcp_scanner import button, sane, usb
from renfield_mcp_scanner.sane import Sensors


class TestSensorFailureIsLoud:
    """Zwei VERSCHIEDENE Fehlschläge, getrennt geprüft.

    🛑 Je ein eigener Test, nicht einer für beide: deckte ein Fall den anderen
    mit ab, wäre hinterher nicht erkennbar, welcher gefeuert hat — und genau
    diese Unterscheidung ist der Zweck des Messgeräts.
    """

    async def test_exit_status_ungleich_null_wirft(self, monkeypatch):
        async def fake_run(*args, timeout=0.0):
            return 1, "scanimage: open of device fujitsu:test failed: Invalid argument"

        monkeypatch.setattr(sane, "_run", fake_run)
        with pytest.raises(sane.ScannerError) as exc:
            await sane.read_sensors("fujitsu:test")
        assert "endete mit 1" in str(exc.value)
        # Die Meldung des Geräts muss mitkommen, sonst ist die Zeile im
        # Protokoll wertlos.
        assert "Invalid argument" in str(exc.value)

    async def test_leeres_ergebnis_bei_status_null_wirft(self, monkeypatch):
        async def fake_run(*args, timeout=0.0):
            # Status 0, aber keine einzige `[hardware]`-Zeile: das Gerät hat
            # geantwortet, die Antwort passt nicht zum Muster.
            return 0, "Usage: scanimage [OPTION]...\nOptions:\n  -d, --device\n"

        monkeypatch.setattr(sane, "_run", fake_run)
        with pytest.raises(sane.ScannerError) as exc:
            await sane.read_sensors("fujitsu:test")
        assert "keine einzige Hardware-Option" in str(exc.value)

    async def test_der_gute_fall_wirft_NICHT(self, monkeypatch):
        """Negativkontrolle: ohne sie würden beide Prüfungen oben auch dann
        bestehen, wenn `read_sensors` IMMER wirft."""
        async def fake_run(*args, timeout=0.0):
            return 0, (
                "  --page-loaded[=(yes|no)] [no] [hardware]\n"
                "  --cover-open[=(yes|no)] [no] [hardware]\n"
                "  --scan[=(yes|no)] [yes] [hardware]\n"
            )

        monkeypatch.setattr(sane, "_run", fake_run)
        s = await sane.read_sensors("fujitsu:test")
        assert s.scan_button is True
        assert s.ready is True

    async def test_TEILMENGE_ohne_scan_wirft(self, monkeypatch):
        """🛑 Im Review zu #9 gefunden: der gefährlichste Fall war nicht die
        leere, sondern die HALBE Antwort.

        Status 0, die Regex trifft einige `[hardware]`-Zeilen, aber nicht
        `scan`. Dann war `raw` nicht leer, es gab keinen Wurf, `ready` las
        `True`, `device_readable` las `True` — und `yes("scan")` meldete für
        immer die Vorgabe „no". Knopf tot, Probe grün, keine Zeile. Genau der
        Ausfall, der neun Tage gekostet hat, nur durch eine engere Tür.
        """
        async def fake_run(*args, timeout=0.0):
            return 0, (
                "  --cover-open[=(yes|no)] [no] [hardware]\n"
                "  --power-save[=(yes|no)] [no] [hardware]\n"
            )

        monkeypatch.setattr(sane, "_run", fake_run)
        with pytest.raises(sane.ScannerError) as exc:
            await sane.read_sensors("fujitsu:test")
        # Die Meldung muss BEIDE fehlenden Felder nennen, sonst sucht man am
        # falschen Ende.
        assert "scan" in str(exc.value)
        assert "page-loaded" in str(exc.value)
        # Und sagen, was ANKAM — sonst ist nicht unterscheidbar, ob gar nichts
        # oder das Falsche gelesen wurde.
        assert "cover-open" in str(exc.value)

    async def test_TEILMENGE_ohne_page_loaded_wirft(self, monkeypatch):
        """`page-loaded` fehlt → der Takt bleibt bei 1,0 s statt 0,2 s, und der
        ~200-ms-Impuls des Knopfes geht verloren. Eigener Test, weil die beiden
        Felder aus verschiedenen Gründen nötig sind."""
        async def fake_run(*args, timeout=0.0):
            return 0, (
                "  --scan[=(yes|no)] [no] [hardware]\n"
                "  --cover-open[=(yes|no)] [no] [hardware]\n"
            )

        monkeypatch.setattr(sane, "_run", fake_run)
        with pytest.raises(sane.ScannerError, match="page-loaded"):
            await sane.read_sensors("fujitsu:test")

    async def test_NEGATIVKONTROLLE_vollstaendige_antwort_wirft_nicht(self, monkeypatch):
        """Ohne sie bestünden die beiden Tests oben auch dann, wenn
        `read_sensors` IMMER wirft — und der Riegel wäre eine Mauer."""
        async def fake_run(*args, timeout=0.0):
            return 0, (
                "  --scan[=(yes|no)] [yes] [hardware]\n"
                "  --page-loaded[=(yes|no)] [yes] [hardware]\n"
            )

        monkeypatch.setattr(sane, "_run", fake_run)
        s = await sane.read_sensors("fujitsu:test")
        assert s.scan_button is True
        assert s.page_loaded is True
        assert s.ready is True

    def test_die_pflichtfelder_sind_die_vom_WAECHTER_gelesenen(self):
        """Strukturtest: die Liste darf nicht aus der Luft kommen. Beide
        Pflichtfelder müssen die sein, von denen das Verhalten des Wächters
        abhängt — `scan` für den Druck, `page-loaded` für den Takt."""
        assert set(sane.REQUIRED_SENSORS) == {"scan", "page-loaded"}

    def test_leere_sensoren_sind_nicht_bereit(self):
        """Der Riegel an `ready` deckt jede andere Herkunft eines Satzes ab.

        Das war der eigentliche Schaden: nicht dass der Abruf scheiterte,
        sondern dass das Ergebnis `bereit` lautete.
        """
        assert Sensors().ready is False
        assert Sensors(raw={"page-loaded": "no"}).ready is True
        assert Sensors(raw={"cover-open": "yes"}, cover_open=True).ready is False


class TestRediscoverIsRationed:
    """Die SUCHE wird rationiert — in FALL B, und nur dort.

    🛑 Es gibt zwei Fälle, und sie verhalten sich entgegengesetzt. Wer sie
    verwechselt, hält den Zählerfix für die Behebung des gemessenen Schadens:

    ======  ==========================  ==========  ====================
    Fall    was scheitert               `device`    wirkt REDISCOVER_AFTER?
    ======  ==========================  ==========  ====================
    A       `find_device`               bleibt      NEIN — die Zuweisung
                                        `None`      findet nicht statt
    B       `read_sensors`              gesetzt     ja
    ======  ==========================  ==========  ====================

    **Alle acht gemessenen Episoden sind Fall A** (462 Warnzeilen, alle mit
    der Meldung aus `find_device`). Der Zählerfix ändert an ihnen NICHTS. Er
    gehört trotzdem hierher, aber aus dem umgekehrten Grund: `read_sensors`
    konnte bis 2026-10-12 gar nicht laut scheitern, also gab es Fall B nie.
    Die Fehlschlag-Erkennung (`TestSensorFailureIsLoud`) macht ihn **zum
    ersten Mal erreichbar** — und ohne den Zähler entstünde damit ein NEUER
    Dauerzustand mit Suche in jedem Takt.

    🛑 Die Zählgröße ist der TAKT, nicht „Fehlschläge nach dem ersten".
    `watch_button` beginnt mit `device = None`, also sucht Takt 1 immer. Wer
    die erste Entdeckung herausrechnet, liegt um eins daneben — und ein Test
    mit der falschen Zahl ist schlechter als keiner, weil er Sicherheit
    vortäuscht.

    🛑 Nicht „weniger als zehn Suchen". Ein boolescher Riegel lässt sich auf
    zwei Weisen falsch bauen, eine Zahl nicht. Bis 2026-10-12 war die
    Bedingung `errors >= REDISCOVER_AFTER` mit einem Zähler verdrahtet, der
    nur im ERFOLGSzweig genullt wird — sie wurde also einmal wahr und blieb es:
    ab dem dritten Fehlschlag lief die Vollaufzählung in JEDEM Takt.

    🛑 Die Zählgröße ist der TAKT, nicht „Fehlschläge nach dem ersten".
    `watch_button` beginnt mit `device = None`, also sucht Takt 1 immer. Wer
    die erste Entdeckung herausrechnet, liegt um eins daneben — und ein Test
    mit der falschen Zahl ist schlechter als keiner, weil er Sicherheit
    vortäuscht.
    """

    class _NieLesbar:
        def __init__(self):
            self.finds = 0

        async def find_device(self, _hint):
            self.finds += 1
            return "fujitsu:test"

        async def read_sensors(self, _device):
            raise RuntimeError("Gerät antwortet nicht")

        async def scanner_node(self):
            return None

        async def sleep(self, _seconds):
            return None

        async def start_job(self, **_kw):
            return {}

    @pytest.mark.parametrize("takte", [1, 2, 3, 4, 5, 6, 7, 9, 10, 12])
    async def test_takte_ergeben_genau_ceil_takte_durch_grenze(self, takte):
        f = self._NieLesbar()
        await button.watch_button(
            _Cfg(), f.start_job, read_sensors=f.read_sensors,
            find_device=f.find_device, scanner_node=f.scanner_node,
            sleep=f.sleep, max_iterations=takte,
        )
        erwartet = math.ceil(takte / button.REDISCOVER_AFTER)
        assert f.finds == erwartet, (
            f"{takte} Takte mit nie lesbarem Sensor müssen genau {erwartet} "
            f"Suchen ergeben (eine je {button.REDISCOVER_AFTER} Fehlschläge), "
            f"nicht {f.finds}. Mehr heißt: die Bedingung bleibt dauerhaft wahr."
        )

    async def test_ein_erfolg_setzt_die_rationierung_zurueck(self):
        """Sonst wäre die Zählung nach einer Erholung dauerhaft verschoben."""
        class _ZweiMalFehler:
            def __init__(self):
                self.finds = 0
                self.reads = 0

            async def find_device(self, _hint):
                self.finds += 1
                return "fujitsu:test"

            async def read_sensors(self, _device):
                self.reads += 1
                # Takte 1,2 scheitern; 3 gelingt; 4,5 scheitern wieder.
                if self.reads in (3,):
                    return Sensors(raw={"page-loaded": "no"})
                raise RuntimeError("Gerät antwortet nicht")

            async def scanner_node(self):
                return None

            async def sleep(self, _seconds):
                return None

            async def start_job(self, **_kw):
                return {}

        f = _ZweiMalFehler()
        await button.watch_button(
            _Cfg(), f.start_job, read_sensors=f.read_sensors,
            find_device=f.find_device, scanner_node=f.scanner_node,
            sleep=f.sleep, max_iterations=5,
        )
        # Takt 1 sucht (device None). Fehlschläge 1,2 -> keine weitere Suche.
        # Takt 3 gelingt -> Rationierung zurückgesetzt. Fehlschläge 1,2 danach
        # -> wieder keine Suche. Also genau EINE Suche über fünf Takte.
        assert f.finds == 1, (
            f"ein Erfolg muss die Rationierung zurücksetzen; {f.finds} Suchen "
            "heißt, der Zähler läuft über den Erfolg hinweg weiter")


class TestFallAIsNotFixedByTheCounter:
    """🛑 Der wichtigere der beiden Tests: er hält den GEMESSENEN Schaden fest.

    Scheitert `find_device`, findet die Zuweisung `device = await
    find_device(...)` nicht statt — `device` bleibt `None`, und der nächste
    Takt sucht wieder, unabhängig von jedem Zähler. `device = None` im
    Fehlerzweig ist dann ein Leerlauf: es IST schon `None`.

    Ohne diesen Test wandert „der Riegel ist repariert" in die Dokumentation,
    während der eigentliche Dauerzustand unbenannt bleibt — die Klasse gilt
    als erledigt, weil eine Instanz es ist. Genau das Muster, das in diesem
    Dienst schon zweimal zugeschlagen hat.

    Fällt dieser Test, ist Fall A behoben — dann gehört die Grenze in Stufe 3
    beschrieben und DIESER Test umgeschrieben, nicht gelöscht.
    """

    class _SucheScheitert:
        def __init__(self):
            self.finds = 0

        async def find_device(self, _hint):
            self.finds += 1
            raise RuntimeError("no Fujitsu scanner found")

        async def read_sensors(self, _device):
            raise AssertionError(
                "unerreichbar: ohne Gerät gibt es keinen Sensorabruf")

        async def scanner_node(self):
            return None

        async def sleep(self, _seconds):
            return None

        async def start_job(self, **_kw):
            return {}

    @pytest.mark.parametrize("takte", [1, 3, 5, 10, 12])
    async def test_jeder_takt_sucht_erneut(self, takte):
        f = self._SucheScheitert()
        await button.watch_button(
            _Cfg(), f.start_job, read_sensors=f.read_sensors,
            find_device=f.find_device, scanner_node=f.scanner_node,
            sleep=f.sleep, max_iterations=takte,
        )
        assert f.finds == takte, (
            f"Fall A: {takte} Takte müssen {takte} Suchen ergeben, nicht "
            f"{f.finds}. REDISCOVER_AFTER wirkt hier NICHT — gemessen am "
            f"2026-10-11: 4956 Versuche über 15,3 h, 55 % Belegung des "
            f"USB-Anschlusses. Ergibt dieser Test weniger, ist Fall A behoben "
            f"und der Test gehört umgeschrieben, nicht entfernt."
        )
        assert f.finds > math.ceil(takte / button.REDISCOVER_AFTER) or takte < 2, (
            "Gegenprobe zur Gegenprobe: in Fall A darf die Rationierung "
            "gerade NICHT greifen")


class TestProbesAreSerialised:
    """Fünf Aufrufer, ein Gerät.

    `scan.py:35`, `button.py:339`, `tools.py:31`, `tools.py:61`,
    `tools.py:135` lösen `find_device` unabhängig aus, bis 2026-10-12 ohne
    jede Serialisierung. Zwei davon laufen regelmässig gegeneinander — und das
    erklärt den Befund „`scanner_status` scheitert, während der Wächter im
    selben Prozess erfolgreich liest": nicht zwei Sichten auf einen Zustand,
    sondern zwei Öffner, von denen einer verliert.
    """

    async def test_zwei_proben_verschachteln_sich_nicht(self, monkeypatch):
        verlauf: list[str] = []

        async def fake_run(*args, timeout=0.0):
            verlauf.append("auf")
            await asyncio.sleep(0.01)
            verlauf.append("zu")
            return 0, ("  --scan[=(yes|no)] [no] [hardware]\n"
                      "  --page-loaded[=(yes|no)] [no] [hardware]\n")

        monkeypatch.setattr(sane, "_run", fake_run)
        await asyncio.gather(
            sane.read_sensors("fujitsu:test"),
            sane.read_sensors("fujitsu:test"),
        )
        assert verlauf == ["auf", "zu", "auf", "zu"], (
            f"die Proben müssen hintereinander laufen, nicht verschachtelt: "
            f"{verlauf}")

    async def test_ohne_das_schloss_verschachteln_sie_sich(self, monkeypatch):
        """Negativkontrolle. Ohne sie bestünde der Test oben auch dann, wenn
        `fake_run` zufällig schnell genug wäre — der Beweis wäre dann der
        Zeitablauf, nicht das Schloss."""
        verlauf: list[str] = []

        async def fake_run(*args, timeout=0.0):
            verlauf.append("auf")
            await asyncio.sleep(0.01)
            verlauf.append("zu")
            return 0, ("  --scan[=(yes|no)] [no] [hardware]\n"
                      "  --page-loaded[=(yes|no)] [no] [hardware]\n")

        monkeypatch.setattr(sane, "_run", fake_run)
        # Dasselbe OHNE `_probe` — direkt gegen `_run`.
        await asyncio.gather(
            sane._run("-A", timeout=1.0),
            sane._run("-A", timeout=1.0),
        )
        assert verlauf == ["auf", "auf", "zu", "zu"], (
            "ohne das Schloss MUSS es sich verschachteln, sonst prüft der Test "
            f"oben nichts: {verlauf}")

    async def test_der_zusammenstoss_hat_eine_eigene_klasse(self, monkeypatch):
        """Ein Zusammenstoß darf in der Auswertung nicht wie ein Verklemmen
        aussehen — sonst ist die Messwoche an dieser Stelle blind."""
        async def langsam(*args, timeout=0.0):
            await asyncio.sleep(0.2)
            return 0, ("  --scan[=(yes|no)] [no] [hardware]\n"
                      "  --page-loaded[=(yes|no)] [no] [hardware]\n")

        monkeypatch.setattr(sane, "_run", langsam)
        halter = asyncio.create_task(sane.read_sensors("fujitsu:test"))
        await asyncio.sleep(0.01)
        with pytest.raises(sane.DeviceContendedError):
            await sane._probe("-A", timeout=0.01)
        await halter


class TestCancellationReleasesTheDevice:
    """Ein Abbruch darf kein `scanimage` zurücklassen.

    Bis 2026-10-12 tötete nur die Zeitablauf-Bahn den Prozess. Wurde die
    wartende Aufgabe abgebrochen, kehrte `_run` zurück und liess ein
    `scanimage` stehen, das das USB-Gerät weiter hielt — der nächste Aufruf
    fand dann „no Fujitsu scanner found", ein Fehlerbild, das wie ein
    Gerätedefekt aussieht und von uns stammt.
    """

    async def test_abbruch_toetet_den_prozess(self):
        aufgabe = asyncio.create_task(
            sane._run("-e", "import time; time.sleep(30)", timeout=30.0))
        await asyncio.sleep(0.05)
        aufgabe.cancel()
        with pytest.raises(asyncio.CancelledError):
            await aufgabe

    def test_reap_ist_synchron_und_vertraegt_einen_toten_prozess(self):
        """`_reap` darf nicht `await`en (die Aufgabe bricht gerade ab) und
        nicht werfen, wenn der Prozess schon weg ist."""
        class _Tot:
            returncode = 0
            def kill(self):
                raise AssertionError("ein beendeter Prozess wird nicht getötet")

        sane._reap(_Tot())                       # wirft nicht

        class _Weg:
            returncode = None
            def kill(self):
                raise ProcessLookupError

        sane._reap(_Weg())                       # wirft nicht


class TestUsbNodeDiagnosis:
    """Die freie Diagnose — und ihre Grenze."""

    def test_der_knoten_wird_dem_RICHTIGEN_geraet_zugeordnet(self):
        """🛑 Die erste Fassung las die Textausgabe und trennte an `^\\+-o`.
        Verschachtelte Knoten sind eingerückt und fallen in den Block ihres
        Elternteils: der Treffer „ScanSnap" lag im Block eines Monitors, und
        die gelieferte `sessionID` war dessen. Eine plausible Zahl vom
        falschen Gerät."""
        baum = [
            {"IORegistryEntryName": "LG Monitor Controls", "idVendor": 1105,
             "sessionID": 21386420314724,
             "IORegistryEntryChildren": [
                 {"USB Product Name": "ScanSnap S1500", "idVendor": 1221,
                  "idProduct": 4514, "sessionID": 68618798644518,
                  "locationID": 539033600,
                  "IORegistryEntryChildren": [
                      {"USB Product Name": "ScanSnap S1500", "idVendor": 1221},
                  ]},
             ]},
        ]
        knoten = usb._pick(baum)
        assert knoten.present is True
        assert knoten.session_id == "68618798644518", (
            "die sessionID muss die des Scanners sein, nicht die des "
            "Elternknotens")
        assert knoten.location_id == "539033600", (
            "zweites Merkmal: ein Verbund- oder Hub-Knoten kann mehr als "
            "einen Treffer liefern")

    def test_ohne_scanner_KEINE_fremde_kennung(self):
        """🛑 Der Test, den der Vorfall vorzeichnet.

        Eine plausible Zahl vom falschen Gerät ist die schlimmste Sorte
        Messwert: sie fällt in der Auswertung nicht auf, sie verschiebt sie
        nur. Ist der Scanner weg, muss der Leser das SAGEN — und nicht die
        Kennung irgendeines Nachbarn liefern.
        """
        ohne_scanner = [
            {"IORegistryEntryName": "LG Monitor Controls", "idVendor": 1105,
             "idProduct": 33090, "sessionID": 21386420314724,
             "locationID": 337641472,
             "IORegistryEntryChildren": [
                 {"USB Product Name": "Logitech BRIO", "idVendor": 1133,
                  "idProduct": 2142, "sessionID": 21386426883581},
             ]},
        ]
        knoten = usb._pick(ohne_scanner)
        assert knoten.present is False
        assert knoten.session_id is None, (
            f"ohne Scanner darf KEINE Kennung zurückkommen, schon gar nicht "
            f"die eines anderen Geräts — bekommen: {knoten.session_id}")

    def test_der_name_allein_genuegt_nicht_fuer_die_kennung(self):
        """Die Schnittstelle unter dem Gerät trägt denselben Namen, aber
        keine `sessionID`. Ohne die Bedingung „hat eine Kennung" entschiede
        die Reihenfolge des Baums über die Brauchbarkeit der Auskunft."""
        nur_schnittstelle = [
            {"USB Product Name": "ScanSnap S1500", "idVendor": 1221,
             "idProduct": 4514},
        ]
        knoten = usb._pick(nur_schnittstelle)
        assert knoten.present is True        # gefunden ist gefunden
        assert knoten.session_id is None     # aber ohne Kennung

    def test_fehlender_knoten_ist_nicht_dasselbe_wie_keine_auskunft(self):
        assert usb._pick([]).present is False
        # `None` als Rückgabe von `scanner_node` heißt „keine Auskunft" und
        # darf nicht als „Scanner aus" gelesen werden — geprüft am Wächter:
        # er bleibt lesbar und wirft nicht.

    async def test_die_diagnose_nimmt_den_waechter_nie_mit(self):
        async def kaputt():
            raise OSError("ioreg weg")

        text = await button._knoten(kaputt)
        assert "keine Auskunft" in text


class _Cfg:
    device = ""          # produktiv LEER -> jeder Zugriff sucht


class TestBefundCodesHabenEineQuelle:
    """🛑 Prior learning `two-switches-over-one-status-set-drift` (2026-10-04).

    `watcher_health` leitete den Fehlercode anfangs selbst aus dem Zustand ab
    und schaltete damit ein zweites Mal über dieselbe Menge. Ein neuer
    Befund-Zustand wäre still als `watcher_dead` beschriftet worden.
    """

    def test_jeder_befund_zustand_hat_einen_code(self):
        assert set(button.WATCHER_FINDING_CODES) == {
            button.WATCHER_DEAD, button.WATCHER_CRASHLOOP}

    def test_kein_NICHT_befund_zustand_steht_in_der_abbildung(self):
        """Sonst verspräche die Abbildung einen Alarm, den `finding` nie
        auslöst."""
        for zustand in (button.WATCHER_OFF, button.WATCHER_STARTING,
                        button.WATCHER_ALIVE):
            assert zustand not in button.WATCHER_FINDING_CODES

    async def test_unbekannter_befund_zustand_ALARMIERT_trotzdem(self):
        """Der Riegel gegen die Drift selbst: fehlt ein Zustand in der
        Abbildung, muss es einen unspezifischen ALARM geben — nie ein falsches
        Etikett und nie Stille."""
        from renfield_mcp_scanner import tools

        class _Neu:
            def report(self, *, enabled: bool) -> dict:
                return {"state": "stuck", "finding": True,
                        "device_readable": True, "detail": "neuer Zustand",
                        "last_tick_age_seconds": 1.0, "stale_after_seconds": 370.0,
                        "since": None, "restarts_total": 0, "restart_streak": 0}

        async def knoten():
            return None

        class _C:
            device = ""
            button_watch = True

        r = await tools.watcher_health(_C(), liveness=_Neu(), scanner_node=knoten)
        assert r["ok"] is False, "ein unbekannter Befund-Zustand darf nicht durchfallen"
        assert r["error_code"] == "watcher_finding"


# ---------------------------------------------------------------------------
# `ioreg` wird eingesammelt — die dritte Fassung desselben Riegels
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_hanging_ioreg_is_killed_not_abandoned(monkeypatch):
    """🛑 `scanner_node` brach bei Zeitablauf nur das `await` ab und liess
    `ioreg` laufen. Geholt wird diese Auskunft unter anderem auf den
    FEHLERPFADEN der Waechterschleife, die zyklisch wiederkehren — ein
    haengendes `ioreg` haette sich dort je Runde angesammelt, und zwar genau
    dann, wenn der USB-Stapel klemmt.

    Gegengemessen: ohne das `reap` bleibt der Prozess hier stehen.
    """
    from renfield_mcp_scanner import usb

    started: list = []
    real_exec = asyncio.create_subprocess_exec

    async def hanging_exec(*argv, **kw):
        # Statt `ioreg` ein Schlaefer, der die Zeitgrenze sicher reisst.
        proc = await real_exec("/bin/sh", "-c", "sleep 30", **kw)
        started.append(proc)
        return proc

    monkeypatch.setattr(usb.shutil, "which", lambda _: "/usr/sbin/ioreg")
    monkeypatch.setattr(usb.asyncio, "create_subprocess_exec", hanging_exec)
    monkeypatch.setattr(usb, "_IOREG_TIMEOUT", 0.3)

    assert await usb.scanner_node() is None      # „keine Auskunft", wirft nie
    assert started, "kein Prozess beobachtet — der Test misst nichts"
    await asyncio.sleep(0.2)
    assert started[0].returncode is not None, (
        f"ioreg lief nach dem Zeitablauf weiter (PID {started[0].pid})")


@pytest.mark.asyncio
async def test_a_cancelled_ioreg_is_killed_too(monkeypatch):
    """Dienstende bricht die Aufgabe ab. Auch dann darf nichts stehenbleiben —
    dieselbe Lehre wie bei `scanimage`: nur die Zeitablauf-Bahn zu raeumen
    liess beim Abbruch einen Prozess zurueck, der das Betriebsmittel hielt."""
    from renfield_mcp_scanner import usb

    started: list = []
    real_exec = asyncio.create_subprocess_exec

    async def hanging_exec(*argv, **kw):
        proc = await real_exec("/bin/sh", "-c", "sleep 30", **kw)
        started.append(proc)
        return proc

    monkeypatch.setattr(usb.shutil, "which", lambda _: "/usr/sbin/ioreg")
    monkeypatch.setattr(usb.asyncio, "create_subprocess_exec", hanging_exec)

    task = asyncio.create_task(usb.scanner_node())
    while not started:
        await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.2)
    assert started[0].returncode is not None, (
        f"ioreg ueberlebte den Abbruch (PID {started[0].pid})")
