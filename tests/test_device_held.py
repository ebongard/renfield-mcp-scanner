"""Ein laufender Scan ist kein Defekt — und darf nicht wie einer aussehen.

🛑 Am 2026-10-21 beim ersten echten Knopfdruck nach dem Umbau gemessen:

    14:14:49  Knopf gedrückt — starte Scan (Papier: ja)
    14:14:54  Sensor nicht lesbar (scanimage endete mit 1: open of device failed)
    14:15:25  Gerät gefunden, Sensor wieder lesbar nach 4 Fehlversuch(en)

Der Scan hält den Einzug, also MUSS ein Sensorabruf daneben scheitern. Das
hatte zwei Folgen, beide falsch:

* `watcher_health` sah `device_readable=False` bei vorhandenem USB-Knoten und
  hätte `device_wedged` gemeldet — ein Fehlalarm auf den normalsten Zustand
  überhaupt. Eine Probe, die am Normalfall anschlägt, wird abgeschaltet.
* Nach drei Fehlschlägen löste der Fehlerpfad eine 6-Sekunden-Vollsuche aus,
  MITTEN im laufenden Scan — Last auf genau dem Anschluss, um den es geht.

Beide hängen an einer Frage, und die hat jetzt EINE Quelle:
`JobManager.scan_in_flight`.
"""
from __future__ import annotations

import pytest

from renfield_mcp_scanner import button, tools, usb
from renfield_mcp_scanner.sane import Sensors


class _Cfg:
    device = ""
    button_watch = True


DA = usb.UsbNode(present=True, session_id="87474184595276",
                 location_id="539033600")


class _Waehrend:
    """Spielt einen laufenden Scan: das Gerät ist belegt."""

    def __init__(self, takte_mit_scan: int):
        self.takte_mit_scan = takte_mit_scan
        self.takt = 0
        self.finds = 0
        self.reads = 0
        self.sleeps: list[float] = []

    def scan_laeuft(self) -> bool:
        self.takt += 1
        return self.takt <= self.takte_mit_scan

    async def find_device(self, _hint):
        self.finds += 1
        return "fujitsu:test"

    async def read_sensors(self, _device):
        self.reads += 1
        return Sensors(raw={"scan": "no", "page-loaded": "no"})

    async def scanner_node(self):
        return DA

    async def sleep(self, seconds):
        self.sleeps.append(seconds)

    async def start_job(self, **_kw):
        return {}


class TestDerWaechterFasstDasGeraetWaehrendEinesScansNICHTAn:
    async def test_kein_sensorabruf_und_keine_suche_waehrend_des_scans(self):
        f = _Waehrend(takte_mit_scan=5)
        await button.watch_button(
            _Cfg(), f.start_job, read_sensors=f.read_sensors,
            find_device=f.find_device, scanner_node=f.scanner_node,
            device_held=f.scan_laeuft, sleep=f.sleep, max_iterations=5,
        )
        assert f.reads == 0, (
            f"während eines Scans darf KEIN Sensorabruf laufen — {f.reads} gezählt")
        assert f.finds == 0, (
            f"und erst recht keine Vollsuche — {f.finds} gezählt. Am 2026-10-21 "
            "löste der Fehlerpfad genau hier eine 6-s-Suche mitten im Scan aus")

    async def test_die_marke_wird_TROTZDEM_gesetzt(self):
        """🛑 Die Zusage, die ein langer Scan sonst bricht.

        Ein Scan darf 1800 s dauern, `LIVENESS_STALE_AFTER` liegt bei 370 s.
        Würde der übersprungene Takt die Marke nicht setzen, erklärte die
        Aufsicht den Knopf bei JEDEM langen Scan für tot — exakt der
        Fehlalarm, den das Nicht-Sperren von `scan_batch` vermeiden soll.
        """
        class _Zaehlend(button.WatcherLiveness):
            def __init__(self):
                super().__init__()
                self.marken = 0

            def tick(self) -> None:
                self.marken += 1
                super().tick()

        leben = _Zaehlend()
        f = _Waehrend(takte_mit_scan=4)
        await button.watch_button(
            _Cfg(), f.start_job, read_sensors=f.read_sensors,
            find_device=f.find_device, scanner_node=f.scanner_node,
            device_held=f.scan_laeuft, sleep=f.sleep, liveness=leben,
            max_iterations=4,
        )
        assert leben.marken == 4, (
            f"jeder übersprungene Takt ist ein VOLLENDETER Durchlauf; "
            f"{leben.marken} von 4 Marken gesetzt")
        assert leben.report(enabled=True)["state"] == button.WATCHER_ALIVE

    async def test_nach_dem_scan_laeuft_es_normal_weiter(self):
        """Negativkontrolle: ohne sie bestünden die Tests oben auch dann, wenn
        der Wächter das Gerät NIE mehr abfragte."""
        f = _Waehrend(takte_mit_scan=2)
        await button.watch_button(
            _Cfg(), f.start_job, read_sensors=f.read_sensors,
            find_device=f.find_device, scanner_node=f.scanner_node,
            device_held=f.scan_laeuft, sleep=f.sleep, max_iterations=5,
        )
        assert f.finds == 1, "nach dem Scan muss das Gerät einmal gesucht werden"
        assert f.reads == 3, f"und die restlichen Takte normal lesen — {f.reads}"


class TestDieProbeNenntEinenLaufendenScanNichtVerklemmt:
    async def test_laufender_scan_ist_ok(self):
        async def knoten():
            return DA

        class _L:
            def report(self, *, enabled: bool) -> dict:
                return {"state": button.WATCHER_ALIVE, "finding": False,
                        "device_readable": False, "detail": "Gerät belegt",
                        "last_tick_age_seconds": 1.0, "stale_after_seconds": 370.0,
                        "since": None, "restarts_total": 0, "restart_streak": 0}

        r = await tools.watcher_health(
            _Cfg(), liveness=_L(), scanner_node=knoten,
            device_held=lambda: True)
        assert r["ok"] is True, (
            "ein laufender Scan ist der Normalfall, kein Befund")
        assert r["device_held"] is True

    async def test_OHNE_laufenden_scan_ist_derselbe_zustand_verklemmt(self):
        """🛑 Die Gegenprobe. Ohne sie prüfte der Test oben nur, dass
        `scan_in_flight` irgendetwas tut — nicht, dass der Riegel genau DIESEN
        Fall abdeckt und sonst nichts."""
        async def knoten():
            return DA

        class _L:
            def report(self, *, enabled: bool) -> dict:
                return {"state": button.WATCHER_ALIVE, "finding": False,
                        "device_readable": False, "detail": "Gerät belegt",
                        "last_tick_age_seconds": 1.0, "stale_after_seconds": 370.0,
                        "since": None, "restarts_total": 0, "restart_streak": 0}

        r = await tools.watcher_health(
            _Cfg(), liveness=_L(), scanner_node=knoten,
            device_held=lambda: False)
        assert r["ok"] is False
        assert r["error_code"] == "device_wedged"


class TestEineQuelleUndSieIstBEGRENZT:
    """🛑 Im Review zu #10 umgestellt, und das ist der Kern.

    Die erste Fassung fragte `JobManager._active`, also „läuft ein Auftrag".
    Das umfasst Zusammenbau, OCR und Klassifizierung — und `img2pdf`/
    `ocrmypdf` laufen über `asyncio.to_thread` OHNE Zeitgrenze und
    unabbrechbar. Eine hängende OCR hätte die Probe DAUERHAFT verstummt und
    die Zeitmarke grün gehalten: der Neun-Tage-Ausfall durch eine neue Tür.

    Jetzt zählt `sane` die Scans, die das Gerät wirklich halten, und das ist
    genau `scan_batch` — begrenzt durch sein eigenes `timeout=1800`.
    Unbegrenztes Schweigen ist damit strukturell unmöglich, ohne eine
    gewählte Frist.
    """

    def test_die_quelle_liegt_dort_wo_das_geraet_geoeffnet_wird(self):
        from renfield_mcp_scanner import sane

        assert callable(sane.device_held)
        assert sane.device_held() is False, "im Ruhezustand hält nichts das Gerät"

    async def test_der_zaehler_wird_auch_bei_einer_AUSNAHME_geraeumt(self, tmp_path,
                                                                     monkeypatch):
        """Ein Schalter, den nur der Erfolgspfad zurücknimmt, bleibt hängen —
        und ein hängender Zähler ist genau das unbegrenzte Schweigen."""
        from renfield_mcp_scanner import sane

        async def boom(*a, **k):
            raise sane.ScannerError("Gerät weg")

        monkeypatch.setattr(sane, "_run", boom)
        with pytest.raises(sane.ScannerError):
            await sane.scan_batch(
                "fujitsu:test", tmp_path, source="ADF Duplex", mode="Color",
                resolution=300, page_height_mm=297.0)
        assert sane.device_held() is False, (
            "nach einer Ausnahme darf der Zähler nicht stehen bleiben — sonst "
            "schweigt die Probe für immer")

    async def test_waehrend_scan_batch_IST_das_geraet_gehalten(self, tmp_path,
                                                               monkeypatch):
        """Gegenprobe: ohne sie bewiesen die Tests oben nur, dass der Zähler
        nie hochgeht."""
        from renfield_mcp_scanner import sane

        gesehen = []

        async def mitschauen(*a, **k):
            gesehen.append(sane.device_held())
            return 0, "out of documents"

        monkeypatch.setattr(sane, "_run", mitschauen)
        await sane.scan_batch(
            "fujitsu:test", tmp_path, source="ADF Duplex", mode="Color",
            resolution=300, page_height_mm=297.0)
        assert gesehen == [True], "während des Geräteaufrufs muss es gehalten sein"
        assert sane.device_held() is False, "und danach frei"


class TestEinGEHALTENERKnopfStartetKeinenZweitenScan:
    """🛑 Im Review zu #10 gefunden.

    Der Überspring-Zweig setzte `pressed_before = False`. Der echte Ablauf:

        Takt 1  Knopf gedrückt gesehen  -> Flanke, Scan startet
        Takt 2  Gerät gehalten          -> übersprungen
        Takt 3  Gerät gehalten          -> übersprungen
        Takt 4  Gerät frei, Knopf NOCH gedrückt

    Mit `pressed_before = False` im Überspring-Zweig läse Takt 4
    `pressed=True` bei `pressed_before=False`, also eine Flanke, die es nie
    gab — und startete einen ZWEITEN Scan mit leerem Einzug.

    Nicht geprüft wird der Fall, dass der Wächter den ersten Druck NIE gesehen
    hat (Dienststart mitten in einem Scan). Dort ist ein Scan beim ersten
    lesbaren Takt richtig: der Wächter kann es nicht wissen, und ihm ein
    Gedächtnis über den Scan hinweg zu geben hieße, einen Druck während des
    Scans nachträglich wirken zu lassen — bei einem Stapel fast immer
    unerwünscht, es gibt nur einen Einzug.
    """

    async def test_der_SCHON_GESEHENE_druck_feuert_nicht_erneut(self):
        from renfield_mcp_scanner.sane import Sensors

        class _Gehalten:
            """Knopf durchgehend gedrückt. Das Gerät ist in Takt 2 und 3
            gehalten — als hielte der Scan es, den Takt 1 ausgelöst hat."""

            def __init__(self):
                self.takt = 0
                self.jobs = 0

            def haelt(self) -> bool:
                self.takt += 1
                return self.takt in (2, 3)

            async def find_device(self, _hint):
                return "fujitsu:test"

            async def read_sensors(self, _device):
                return Sensors(raw={"scan": "yes", "page-loaded": "yes"},
                               scan_button=True, page_loaded=True)

            async def scanner_node(self):
                return None

            async def sleep(self, _s):
                return None

            async def start_job(self, **_kw):
                self.jobs += 1
                return {"job_id": f"j{self.jobs}"}

        f = _Gehalten()
        await button.watch_button(
            _Cfg(), f.start_job, read_sensors=f.read_sensors,
            find_device=f.find_device, scanner_node=f.scanner_node,
            device_held=f.haelt, sleep=f.sleep, max_iterations=5,
        )
        assert f.jobs == 1, (
            f"ein durchgehend gehaltener Knopf ist EIN Druck; {f.jobs} Scans "
            "gestartet. Mit `pressed_before = False` im Überspring-Zweig wären "
            "es zwei — der zweite mit leerem Einzug")
