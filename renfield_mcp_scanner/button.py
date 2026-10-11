"""Der blaue Knopf am Gerät startet einen Scan.

WARUM GEPOLLT WIRD
------------------
Ein Hardwareknopf sendet kein Ereignis. SANE bietet dafür nichts an, worauf man
horchen könnte — der Zustand ist nur abfragbar. Das ist die eine Stelle, an der
Polling nicht Bequemlichkeit ist, sondern die einzige verfügbare Bauform.

GEMESSEN AM 2026-09-25 (ScanSnap S1500, fujitsu-Backend) — die Zahlen stehen
hier, damit sie niemand später wegoptimiert:

* **Ein Sensorabruf kostet 10 ms.** `scanimage -d <gerät> -A`, dreimal gemessen,
  jeweils 0,01 s. 🛑 NICHT zu verwechseln mit `scanimage --help`: das braucht
  **7,4 s**, weil es sämtliche SANE-Backends durchsucht. Wer die Gerätekennung
  wegfallen lässt, macht aus 10 ms mehr als sieben Sekunden.
* **Der Knopf VERRIEGELT NICHT und meldet auch kein HALTEN.** Zwei Messreihen,
  100-ms- und 200-ms-Takt: jeder Druck ergab genau EINE Stichprobe mit
  `scan=yes` — auch wenn zwei Sekunden lang gehalten wurde. Eine einzelne
  Abfrage Sekunden nach einem Druck meldete `no`. Der Sensor gibt einen kurzen
  IMPULS je Druck; Halten hilft nicht, und ein gespeichertes Ereignis gibt es
  nicht.
* **Der Impuls dauert rund 200–250 ms.** Bei 200-ms-Takt wurden 5 von 5 Drücke
  erfasst — das ginge bei einem kürzeren Impuls nicht.

🛑 **200 ms ist auch die Obergrenze fuer die GERAETElast, nicht nur fuer die CPU.**
Die erste Fassung fragte zehnmal je Sekunde ab und belegte den Scanner damit zu
42 % der Zeit. Am 2026-09-25 hat ihn das nach rund einer halben Stunde
festgesetzt: SANE meldete "no Fujitsu scanner found", und erst Aus- und
Einschalten half. Die teure Ressource ist hier das GERAET, nicht der Prozessor —
das hatte ich beim ersten Messen uebersehen.

DESHALB DIE ZWEI TAKTE
----------------------
Zehn Abfragen je Sekunde kosten rund 10 % eines Kerns — dauerhaft, für einen
Knopf, der vielleicht dreimal am Tag gedrückt wird. `page-loaded` ist der
Türsteher: ohne Papier im Einzug KANN kein Scan beginnen, dort genügt eine
Abfrage je Sekunde (~1 %). Liegt Papier ein, wird hochgeschaltet — der teure
Takt läuft nur in den Sekunden, in denen er etwas bringt.

WOHIN SO EIN SCAN GEHT
----------------------
Nirgendwohin, und das ist Absicht. Das Ziel wird sonst aus dem `caller`
abgeleitet; ein Knopfdruck hat keinen. Bei mehreren konfigurierten Zielen landet
der Stapel deshalb `unrouted` und wartet auf eine Entscheidung, statt in eine
Vermutung gefilt zu werden. Trennblätter entscheiden es im Vorbeigehen — sie
sind die einzige Routing-Schicht, die einen unbeaufsichtigten Scan übersteht.
Wer den Knopf fest zuordnen will, setzt `SCANNER_CALLER_TARGET_BUTTON`.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any, Awaitable, Callable


from . import sane

logger = logging.getLogger("renfield-mcp-scanner.button")

#: `caller`, unter dem ein knopfgestarteter Auftrag läuft. Eigener Name, damit
#: er in Protokollen von einem Agenten-Scan unterscheidbar ist UND per
#: `SCANNER_CALLER_TARGET_BUTTON` fest zugeordnet werden kann.
BUTTON_CALLER = "button"

IDLE_INTERVAL = 1.0    # ohne Papier: ~1 % eines Kerns
ARMED_INTERVAL = 0.2   # mit Papier: fängt den ~200-ms-Impuls, s. Modulkopf
ERROR_BACKOFF = 5.0    # Gerät weg/schläft: nicht im Leerlauf hämmern
REDISCOVER_AFTER = 3   # so viele Fehlschläge in Folge, bevor neu gesucht wird
#: Wartezeit nach einem Absturz der Schleife. 🛑 ABGELEITET, nicht gewählt:
#: ein neu gestarteter `watch_button` beginnt mit `device = None` und ruft
#: deshalb sofort `find_device`. `config.device` ist produktiv LEER
#: (`SCANNER_DEVICE` setzt niemand), also läuft dort `scanimage -f` — der
#: langsame Pfad, der alle SANE-Backends durchkämmt und dabei den USB-Anschluss
#: belegt. Gemessen am 2026-10-11: 4,3 s je Suche.
#:
#: `watch_button` bremst sich dafür selbst: im Sensorfehlerpfad sucht es erst
#: nach `REDISCOVER_AFTER` Fehlschlägen neu, also höchstens alle
#: `REDISCOVER_AFTER * ERROR_BACKOFF` Sekunden. Diese Grenze stammt aus der
#: Messung vom 2026-09-25, die den Scanner FESTGESETZT hat (s. Modulkopf).
#: Eine Aufsicht, die schneller neu startet, umgeht genau diese Bremse — aus
#: „Knopf tot" würde „Gerät fest". Darum bindet die Wartezeit an dieselbe
#: Grenze und wird nie kleiner.
RESTART_BACKOFF = REDISCOVER_AFTER * ERROR_BACKOFF
RESTART_BACKOFF_MAX = 60.0  # Deckel: im Dauerfehler eine MELDUNG je Minute —
#: dieselbe Frequenz wie der Sensorfehlerpfad, aber nicht dasselbe Volumen:
#: dort eine `warning`-Zeile, hier eine vollständige Rückverfolgung.

#: Faktor auf die längste vorgesehene Lücke zwischen zwei Durchläufen. Drei,
#: damit EINE lange Lücke noch keinen Befund ergibt: eine Gerätesuche und ein
#: Sensorabruf tragen je bis zu 60 s Zeitgrenze (`sane._run`), und ein
#: schlafender oder ausgestöpselter Scanner ist der NORMALFALL auf einem
#: Arbeitsplatzrechner.
LIVENESS_FACTOR = 3
#: Ab dieser Lücke gilt die Zeitmarke als veraltet und der Wächter als nicht am
#: Leben. 🛑 ABGELEITET, nicht gewählt: `RESTART_BACKOFF_MAX` ist die längste
#: vorgesehene Lücke überhaupt — in ihr läuft die Schleife tatsächlich nicht,
#: weil die Aufsicht zwischen zwei Neustarts wartet. Alles Kürzere
#: (`IDLE_INTERVAL`, `ARMED_INTERVAL`, `ERROR_BACKOFF`, `RESTART_BACKOFF`)
#: liegt darunter und ist damit mitgedeckt.
LIVENESS_STALE_AFTER = LIVENESS_FACTOR * RESTART_BACKOFF_MAX

#: Zustände, die `WatcherLiveness.report` meldet. `DEAD` ist der EINZIGE
#: Befund — die anderen drei sind normale Zustände und dürfen keinen Alarm
#: auslösen.
WATCHER_OFF = "off"
WATCHER_STARTING = "starting"
WATCHER_ALIVE = "alive"
WATCHER_DEAD = "dead"


class WatcherLiveness:
    """Lebt der Knopfwächter? Die Antwort hängt an einer ZEITMARKE.

    🛑 DIE FALLE, UM DIE ES HIER GEHT (der Ausfall vom 2026-10-04, Issue #3)
    ======================================================================
    Die Task des Wächters EXISTIERTE sieben Tage lang, während der blaue Knopf
    ohne Wirkung war: `server.py` hielt sie in einer starken Referenz, und die
    Schleife darin war tot. Jede Auskunft, die aus dem Vorhandensein der Task
    oder aus `task.done()` abgeleitet wäre, hätte in diesen sieben Tagen
    „läuft" gemeldet. Darum wird hier NICHTS abgeleitet: die Schleife setzt bei
    jedem Durchlauf selbst eine Zeitmarke, und nur deren Alter entscheidet.

    Die Marke wird AM ANFANG jedes Durchlaufs gesetzt, auch im
    Sensorfehlerpfad — die Aussage ist „die Schleife dreht sich", nicht „das
    Gerät antwortet". Beides getrennt zu halten ist der Sinn der Sache: ein
    ausgestöpselter oder schlafender Scanner ist ein NORMALER Zustand (der
    Dienst läuft auf einem Arbeitsplatzrechner), ein stehender Wächter nicht.

    Die Uhr ist monoton, nicht die Wanduhr: ein Sprung durch NTP oder
    Zeitumstellung würde den Befund sonst erfinden. Schläft der Rechner,
    erholt sich die Auskunft nach dem Aufwachen binnen eines Durchlaufs
    (`IDLE_INTERVAL`) — eine Probe unmittelbar nach dem Aufwachen ist also der
    einzige Fall, in dem sie kurz veraltet sein kann.

    Die Wanduhr wird allein für die ANZEIGE geführt („seit wann"), nie für den
    Vergleich.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic,
                 wallclock: Callable[[], datetime] = datetime.now):
        self._clock = clock
        self._wallclock = wallclock
        # Gegen DIESE Marke zählt „noch nie getickt". Ohne sie läse eine
        # Verdrahtung, die den Wächter gar nicht erst startet, für immer als
        # „startet gerade" — und das ist genau die Klasse Fehler, gegen die
        # dieses Feld gebaut ist.
        self._process_started = clock()
        self._loop_started: float | None = None
        self._loop_started_at: datetime | None = None
        self._last_tick: float | None = None
        self._restarts_total = 0
        self._restart_streak = 0
        self._device_readable: bool | None = None
        self._device_detail = ""

    # --- vom Wächter selbst gesetzt ---------------------------------------------

    def loop_started(self) -> None:
        self._loop_started = self._clock()
        self._loop_started_at = self._wallclock()

    def tick(self) -> None:
        """Ein Schleifendurchlauf beginnt. Die einzige Quelle der Lebendigkeit."""
        self._last_tick = self._clock()

    def note_device(self, readable: bool, detail: str = "") -> None:
        self._device_readable = readable
        self._device_detail = detail

    def note_restart(self, streak: int) -> None:
        self._restarts_total += 1
        self._restart_streak = streak

    # --- Auskunft ---------------------------------------------------------------

    def report(self, *, enabled: bool) -> dict:
        """Der Wächterbefund für `scanner_status`.

        `finding` ist die Hälfte, auf die eine funktionale Probe schaut: sie ist
        NUR bei `dead` gesetzt. „Wächter lebt, Gerät nicht erreichbar" ist kein
        Befund, und `SCANNER_BUTTON_WATCH=false` ist erst recht keiner.
        """
        if not enabled:
            return self._shape(WATCHER_OFF, False, None, detail=(
                "absichtlich aus (SCANNER_BUTTON_WATCH) — der blaue Knopf "
                "startet keinen Scan, und das ist so gewollt"))
        if self._last_tick is None:
            # Noch kein Durchlauf. Die erste Gerätesuche dauert messbar (am
            # 2026-10-11: 4,3 s), also ist das kurz nach dem Start normal.
            age = self._clock() - self._process_started
            if age <= LIVENESS_STALE_AFTER:
                return self._shape(WATCHER_STARTING, False, age, detail=(
                    "Wächter startet — noch kein Schleifendurchlauf"))
            return self._shape(WATCHER_DEAD, True, age, detail=(
                f"Wächter hat seit dem Start des Dienstes ({age:.0f} s) keinen "
                f"einzigen Durchlauf gemeldet — er läuft nicht, der blaue Knopf "
                f"wirkt nicht"))
        age = self._clock() - self._last_tick
        if age > LIVENESS_STALE_AFTER:
            return self._shape(WATCHER_DEAD, True, age, detail=(
                f"Wächter lebt NICHT: letzte Zeitmarke vor {age:.0f} s, Grenze "
                f"{LIVENESS_STALE_AFTER:.0f} s. Der blaue Knopf wirkt nicht."))
        if self._device_readable is False:
            return self._shape(WATCHER_ALIVE, False, age, detail=(
                f"Wächter lebt; Gerät nicht erreichbar — schlafend oder "
                f"ausgestöpselt, ein normaler Zustand, kein Befund"
                f"{f': {self._device_detail}' if self._device_detail else ''}"))
        return self._shape(WATCHER_ALIVE, False, age, detail="Wächter lebt")

    def _shape(self, state: str, finding: bool, age: float | None, *,
               detail: str) -> dict:
        return {
            "state": state,
            "finding": finding,
            "last_tick_age_seconds": None if age is None else round(age, 1),
            # Mitgeliefert, damit eine Probe die Grenze nicht nachbauen muss —
            # eine zweite Zahl an einer zweiten Stelle läuft auseinander.
            "stale_after_seconds": LIVENESS_STALE_AFTER,
            "since": (self._loop_started_at.isoformat(timespec="seconds")
                      if self._loop_started_at else None),
            "restarts_total": self._restarts_total,
            # Der Zähler aus der Absturzmeldung („%d. Neustart"): er beginnt
            # nach einem gesunden Lauf neu, deshalb steht die Lebenssumme
            # daneben.
            "restart_streak": self._restart_streak,
            "device_readable": self._device_readable,
            "detail": detail,
        }


#: Ein Prozess, ein Scanner, ein Wächter — deshalb ein Modul-Exemplar. Sowohl
#: `watch_button` als auch `supervise_button` greifen VORGABEWEISE darauf zu;
#: Tests hängen ihr eigenes ein. 🛑 Wer eines der beiden mit einem eigenen
#: Exemplar aufruft und das andere nicht, bekommt zwei halbe Auskünfte.
LIVENESS = WatcherLiveness()


async def watch_button(
    config: Any,
    start_job: Callable[..., Awaitable[dict]],
    *,
    read_sensors: Callable[[str], Awaitable[sane.Sensors]] = sane.read_sensors,
    find_device: Callable[[str], Awaitable[str]] = sane.find_device,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    liveness: WatcherLiveness | None = None,
    max_iterations: int | None = None,
) -> None:
    """Warte auf den Knopf und starte einen Scan. Läuft bis zum Abbruch.

    🛑 NICHT DIREKT ALS TASK ABSETZEN — dafür gibt es `supervise_button`.
    Dieser Satz steht hier, weil der vorige („Läuft bis zum Abbruch.") die
    Fehlverdrahtung getragen hat: er klingt wie eine Zusage, dass die Schleife
    nicht endet. Sie fängt aber nur die VORGESEHENEN Fehler; eine
    unvorhergesehene Ausnahme beendet sie, und als nackte Task bleibt das
    unsichtbar. Am 2026-10-04 hat das den blauen Knopf sieben Tage lahmgelegt.

    🛑 Ein NEUSTART dieser Funktion ist nicht billig: sie beginnt mit
    ``device = None`` und löst damit eine vollständige SANE-Suche aus, solange
    ``config.device`` leer ist (produktiv ist es das). Wer sie neu startet,
    muss die Grenze aus `RESTART_BACKOFF` einhalten — s. dort.

    ``max_iterations`` gibt es allein für Tests — ohne Abbruchbedingung wäre
    eine Endlosschleife nicht prüfbar.
    """
    device: str | None = None
    pressed_before = False
    scanning = False
    iterations = 0
    errors = 0
    leben = liveness if liveness is not None else LIVENESS
    leben.loop_started()

    while max_iterations is None or iterations < max_iterations:
        iterations += 1
        # 🛑 Die Zeitmarke steht HIER, am Anfang jedes Durchlaufs, und nicht im
        # Erfolgszweig: die Aussage ist „die Schleife dreht sich". Stünde sie
        # nach dem Sensorabruf, läse ein ausgestöpselter Scanner — der
        # Normalfall auf einem Arbeitsplatzrechner — als toter Wächter.
        leben.tick()
        try:
            if device is None:
                device = await find_device(config.device)
                logger.info("Knopfwächter: Gerät %s", device)
            sensors = await read_sensors(device)
        except Exception as exc:
            # Ein ausgestöpselter oder schlafender Scanner ist ein normaler
            # Zustand, kein Ausfall des Dienstes. Gerät neu suchen und ruhig
            # weiterlaufen — der Wächter darf den MCP-Server nie mitreissen.
            #
            # 🛑 SICHTBAR, nicht `debug`. Die erste Fassung protokollierte hier
            # auf debug, der Dienst läuft auf info — ein Wächter, der jede
            # Sekunde scheitert, wäre damit STUMM gewesen und der Knopf hätte
            # einfach nie funktioniert, ohne eine einzige Zeile. Genau der
            # Fehlermodus, gegen den dieser Dienst sonst überall gebaut ist.
            # Gedrosselt, damit ein dauerhaft fehlendes Gerät das Protokoll
            # nicht flutet: die erste Meldung sofort, danach eine je Minute.
            errors += 1
            leben.note_device(False, str(exc))
            if errors == 1 or errors % int(60 / ERROR_BACKOFF) == 0:
                logger.warning("Knopfwächter: Sensor nicht lesbar (%s) — "
                               "%d. Versuch seit dem letzten Erfolg", exc, errors)
            # 🛑 Das Geraet NICHT bei jedem Fehler neu suchen. Die Suche
            # (`scanimage -f`) ist der langsame Pfad — sie durchkaemmt alle
            # SANE-Backends und belegt dabei den USB-Anschluss. Die erste
            # Fassung setzte hier `device = None` und erzwang damit alle fuenf
            # Sekunden genau diese Suche; eine haengende Suche hielt das Geraet,
            # die naechste fand es deshalb nicht, und der Schaden vervielfachte
            # sich im Takt. Am 2026-09-25 hat das den Scanner festgesetzt, bis
            # er vom Strom genommen wurde.
            # Erst nach mehreren Fehlschlaegen in Folge neu suchen.
            if errors >= REDISCOVER_AFTER:
                device = None
            pressed_before = False
            await sleep(ERROR_BACKOFF)
            continue
        leben.note_device(True)
        if errors:
            logger.info("Knopfwächter: Sensor wieder lesbar nach %d Fehlversuch(en)",
                        errors)
            errors = 0

        pressed = sensors.scan_button
        # FLANKE, nicht Zustand: ein gehaltener Knopf darf nicht zwei Scans
        # auslösen. Fällt der Sensor zwischendurch nicht auf `no` zurück,
        # bleibt es bei einem Auftrag.
        if pressed and not pressed_before and not scanning:
            logger.info("Knopf gedrückt — starte Scan (Papier: %s)",
                        "ja" if sensors.page_loaded else "nein")
            scanning = True
            try:
                result = await start_job(caller=BUTTON_CALLER, target="", title="")
            except Exception as exc:
                logger.warning("Knopf-Scan konnte nicht starten: %s", exc)
                result = {}
            finally:
                scanning = False
            if result.get("busy"):
                # Erwarteter Fall, kein Fehler: es gibt genau einen Einzug.
                logger.info("Knopf gedrückt, aber ein Scan läuft bereits — ignoriert")
            elif result.get("job_id"):
                logger.info("Knopf-Scan gestartet: %s", result["job_id"])
        pressed_before = pressed

        # Der teure Takt NUR mit Papier im Einzug. Ohne Papier kann der Knopf
        # ohnehin nichts auslösen, was ankommt.
        await sleep(ARMED_INTERVAL if sensors.page_loaded else IDLE_INTERVAL)


async def supervise_button(
    config: Any,
    start_job: Callable[..., Awaitable[dict]],
    *,
    watcher: Callable[..., Awaitable[None]] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
    liveness: WatcherLiveness | None = None,
    max_restarts: int | None = None,
) -> None:
    """Hält den Knopfwächter am Leben — und macht sein Ende HÖRBAR.

    🛑 DER AUSFALL, GEGEN DEN DAS GESCHRIEBEN IST (2026-10-11)
    ===========================================================
    `watch_button` fängt alles, was in seiner Schleife vorgesehen ist: ein
    fehlendes Gerät, einen Sensorfehler, einen fehlgeschlagenen Scan-Start.
    Eine UNVORHERGESEHENE Ausnahme beendete die Aufgabe trotzdem — und zwar
    lautlos, weil `server.py` sie in einer starken Referenz hielt, ohne sie
    abzuwarten und ohne Abschluss-Rückruf: Pythons Warnung „Task exception was
    never retrieved" feuert erst beim Aufräumen und kam deshalb NIE.

    Gemessen am Haushalt: nach dem Papierstau vom 2026-10-04 15:24 hat der
    Wächter **sieben Tage** keine Zeile mehr geschrieben. Sein Fehlerpfad
    protokolliert gedrosselt eine Warnung je Minute — ein dauerhaft fehlendes
    Gerät hätte also Tausende Zeilen erzeugt. Es waren null. Der blaue Knopf
    war eine Woche tot, und nichts hat es gesagt.

    Darum zwei Dinge, die vorher beide fehlten:

    * Ein Absturz der Schleife wird mit Rückverfolgung protokolliert und die
      Schleife NEU GESTARTET. Ein Knopf, der nach einem Papierstau dauerhaft
      tot bleibt, ist schlimmer als einer, der es nochmal versucht.
    * Die Wartezeit wächst, beginnt bei `RESTART_BACKOFF` und ist auf
      `RESTART_BACKOFF_MAX` gedeckelt. Im Dauerfehler bleibt es bei einer
      MELDUNG je Minute — dieselbe Frequenz wie im Sensorfehlerpfad, aber nicht
      dasselbe Volumen: dort steht eine `warning`-Zeile, hier eine vollständige
      Rückverfolgung. Das ist der Preis dafür, die Ursache überhaupt zu
      erfahren, und er ist bewusst bezahlt.

    `asyncio.CancelledError` wird NICHT behandelt: sie ist das geordnete Ende
    beim Herunterfahren und muss durchlaufen. Sie ist in Python ≥ 3.8 keine
    `Exception`, fällt also ohnehin nicht in den Fang unten — das steht hier,
    damit es beim nächsten Umbau nicht versehentlich verlorengeht.

    Die Neustarts werden an `WatcherLiveness` gemeldet, damit `scanner_status`
    sie nennen kann — die Aufsicht ist die einzige Stelle, die sie kennt. Die
    Lebendigkeit selbst meldet sie NICHT: die kommt aus der Schleife, sonst
    wäre sie wieder aus dem Vorhandensein einer Task abgeleitet.

    `watcher`, `clock` und `max_restarts` gibt es allein für Tests: ohne
    Abbruchbedingung wäre eine Aufsicht über eine Endlosschleife nicht prüfbar.
    Die Uhr ist EINGEHÄNGT und wird nicht global verbogen — ein Patch auf
    `time.monotonic` gilt prozessweit, und die Ereignisschleife fragt sie
    selbst ab; ein aufrufzahl-gekoppelter Doppelgänger wäre dann phasenver-
    schoben, ohne dass es auffällt.
    """
    run = watcher if watcher is not None else watch_button
    leben = liveness if liveness is not None else LIVENESS
    restarts = 0

    while True:
        begonnen = clock()
        try:
            await run(config, start_job)
        except Exception:
            # Die Wartezeit soll die AKTUELLE Unruhe abbilden, nicht die
            # Lebensgeschichte des Dienstes. Hat der Wächter vor dem Absturz
            # länger durchgehalten als der Deckel, war er dazwischen gesund —
            # dann beginnt die Staffelung neu. Ohne das erbt ein Dienst, der
            # monatelang läuft und dreimal abstürzt, dauerhaft die 60 Sekunden,
            # und die Zählung in der Meldung („12. Neustart") führt in die Irre,
            # weil sie Monate zusammenwirft.
            if clock() - begonnen > RESTART_BACKOFF_MAX:
                restarts = 0
            restarts += 1
            leben.note_restart(restarts)
            # `exception` statt `error`: ohne Rückverfolgung wäre die Ursache
            # genau so unauffindbar wie vorher, nur eine Zeile lauter.
            logger.exception(
                "Knopfwächter abgestürzt — %d. Neustart, der blaue Knopf war "
                "bis dahin ohne Wirkung", restarts,
            )
            if max_restarts is not None and restarts >= max_restarts:
                return
            await sleep(min(RESTART_BACKOFF * restarts, RESTART_BACKOFF_MAX))
            continue

        # Rückkehr OHNE Ausnahme. Im Betrieb läuft `watch_button` bis zum
        # Abbruch, kommt also nie hierher; mit `max_iterations` (Tests) schon.
        # Falls ein Umbau je einen stillen Rückweg einbaut, soll er auffallen.
        logger.warning(
            "Knopfwächter ist von selbst beendet — der blaue Knopf wirkt nicht "
            "mehr, bis der Dienst neu startet",
        )
        return
