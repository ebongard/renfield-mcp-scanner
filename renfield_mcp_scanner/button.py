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


from . import sane, usb

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

#: Die längste Lücke zwischen zwei VOLLENDETEN Durchläufen, in der nichts
#: kaputt ist. 🛑 ABGELEITET, Summand für Summand — eine geratene Zahl driftet,
#: und die erste Fassung hat genau hier zwei Posten übersehen (Review
#: 2026-10-11):
#:
#:   `RESTART_BACKOFF_MAX`   die Aufsicht wartet nach einem Absturz, und in
#:                           dieser Spanne dreht sich die Schleife wirklich nicht
#: + `sane.PROBE_TIMEOUT`    die Gerätesuche darf so lange hängen …
#: + `sane.PROBE_TIMEOUT`    … und der Sensorabruf danach ebenso
#: + `ERROR_BACKOFF`         die Wartezeit am Ende des Durchlaufs (der längste
#:                           der drei Takte)
#:
#: Die beiden SANE-Grenzen sind der Grund: ein hängendes `scanimage` zieht die
#: Lücke über die Wartezeit der Aufsicht hinaus, ohne dass der Wächter steht.
#: `IDLE_INTERVAL` und `ARMED_INTERVAL` liegen unter `ERROR_BACKOFF` und sind
#: damit mitgedeckt.
LIVENESS_WORST_GAP = (RESTART_BACKOFF_MAX + 2 * sane.PROBE_TIMEOUT + ERROR_BACKOFF)
#: Sicherheitsfaktor auf diese Lücke. Zwei, damit EIN unvorhergesehener
#: zusätzlicher Halt noch keinen Fehlalarm ergibt: ein ausgestöpselter oder
#: schlafender Scanner ist der NORMALFALL auf einem Arbeitsplatzrechner, und
#: ein Fehlalarm darauf macht die Alarmierung unbrauchbar. Der Preis ist die
#: Erkennungsdauer — gegen einen Ausfall, der sieben Tage unbemerkt blieb, ist
#: sie belanglos.
LIVENESS_FACTOR = 2
#: Ab dieser Lücke gilt die Zeitmarke als veraltet und der Wächter als nicht am
#: Leben.
LIVENESS_STALE_AFTER = LIVENESS_FACTOR * LIVENESS_WORST_GAP

#: Ab so vielen Neustarts in Folge ist der Wächter nicht „am Leben", sondern in
#: einem ABSTURZKREIS. Zwei genügen, weil die Aufsicht die Folge selbst schon
#: nach einem gesunden Lauf von mehr als `RESTART_BACKOFF_MAX` zurücksetzt: ein
#: Zähler von zwei heisst also „erneut abgestürzt, bevor er einmal so lange
#: durchgehalten hat". Ein einzelner Absturz nach Monaten Betrieb ist kein
#: Befund — er steht als Rückverfolgung im Protokoll und die Schleife läuft.
CRASHLOOP_RESTARTS = 2

#: Zustände, die `WatcherLiveness.report` meldet. `DEAD` und `CRASHLOOP` sind
#: die Befunde; `OFF` und `STARTING` sind normale Zustände und dürfen keinen
#: Alarm auslösen.
WATCHER_OFF = "off"
WATCHER_STARTING = "starting"
WATCHER_ALIVE = "alive"
WATCHER_DEAD = "dead"
WATCHER_CRASHLOOP = "crashloop"

#: Fehlercode je BEFUND-Zustand — EINE Abbildung, nicht zwei.
#:
#: 🛑 `watcher_health` leitete den Code anfangs selbst aus dem Zustand ab und
#: schaltete damit ein ZWEITES Mal über diese Menge. Ein neuer Zustand mit
#: `finding=True` wäre dort stillschweigend als `watcher_dead` beschriftet
#: worden — dieselbe Drift wie am 2026-10-04 in `scanner_jobs.py`, wo eine
#: zweite Verzweigung über dieselbe Statusmenge zwei Zustände vergaß. Wer hier
#: einen Befund-Zustand hinzufügt, MUSS ihn hier eintragen; fehlt er, fällt das
#: in `watcher_health` sofort auf, statt still falsch zu beschriften.
WATCHER_FINDING_CODES = {
    WATCHER_DEAD: "watcher_dead",
    WATCHER_CRASHLOOP: "watcher_crashloop",
}


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
        self._last_restart: float | None = None
        self._device_readable: bool | None = None
        self._device_detail = ""

    # --- vom Wächter selbst gesetzt ---------------------------------------------

    def loop_started(self) -> None:
        self._loop_started = self._clock()
        self._loop_started_at = self._wallclock()

    def tick(self) -> None:
        """Ein Schleifendurchlauf ist VOLLENDET — die Quelle der Lebendigkeit.

        🛑 Vollendet, nicht begonnen. Am Anfang gesetzt hiess die Marke nur
        „die Schleife wurde betreten", und genau das tut ein Absturzkreis
        ständig; das Feld meldete dann `alive`, während der Knopf tot war.
        """
        self._last_tick = self._clock()

    def note_device(self, readable: bool, detail: str = "") -> None:
        self._device_readable = readable
        self._device_detail = detail

    def note_restart(self, streak: int) -> None:
        self._restarts_total += 1
        self._restart_streak = streak
        # 🛑 Der ZEITPUNKT muss mit. Die Folge wird von der Aufsicht nur im
        # Absturzzweig zurückgesetzt — ein Dienst, der zweimal beim Start
        # abstürzt und danach monatelang läuft, trägt die Zwei für immer. Ohne
        # Zeitpunkt läse er für immer als Absturzkreis.
        self._last_restart = self._clock()

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
        if self._in_crashloop():
            # 🛑 VOR der Zeitmarke geprüft. Ein Absturzkreis betritt die
            # Schleife ständig neu und kann dabei einzelne Durchläufe
            # vollenden — die Marke allein läse ihn dann als `alive`, und
            # genau das war der Befund vom 2026-10-11.
            return self._shape(WATCHER_CRASHLOOP, True,
                               None if self._last_tick is None
                               else self._clock() - self._last_tick,
                               detail=(
                f"Wächter stürzt wiederholt ab: {self._restart_streak} Neustarts "
                f"in Folge, der letzte vor {self._clock() - self._last_restart:.0f} s. "
                f"Der blaue Knopf wirkt höchstens zeitweise. Ursache steht als "
                f"Rückverfolgung im Protokoll."))
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

    def _in_crashloop(self) -> bool:
        """Stürzt der Wächter gerade WIEDERHOLT ab?

        Zwei Bedingungen, und beide sind nötig: die Folge (sie wird von der
        Aufsicht nach einem gesunden Lauf zurückgesetzt, ist also ein Mass für
        die aktuelle Unruhe) UND die Frische des letzten Neustarts. Ohne die
        zweite trägt ein Dienst, der zweimal beim Start abstürzte und danach
        monatelang lief, den Befund für immer.
        """
        if self._last_restart is None or self._restart_streak < CRASHLOOP_RESTARTS:
            return False
        return self._clock() - self._last_restart <= LIVENESS_STALE_AFTER

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


async def _knoten(scanner_node: Callable[[], Awaitable[usb.UsbNode | None]]) -> str:
    """Auskunft über den USB-Knoten, nie werfend.

    Eine Diagnosezeile darf den Wächter nicht mitnehmen — sonst wäre die
    Beobachtung selbst die nächste Ausfallursache.
    """
    try:
        node = await scanner_node()
    except Exception as exc:  # noqa: BLE001 — Diagnose, nie tragend
        return f"USB-Knoten: keine Auskunft ({exc})"
    return str(node) if node is not None else "USB-Knoten: keine Auskunft"


async def watch_button(
    config: Any,
    start_job: Callable[..., Awaitable[dict]],
    *,
    read_sensors: Callable[[str], Awaitable[sane.Sensors]] = sane.read_sensors,
    find_device: Callable[[str], Awaitable[str]] = sane.find_device,
    # Freie Diagnose am USB-Knoten: liest die Registratur des Kernels, fasst
    # das Gerät nicht an. Einspritzbar, damit Tests kein `ioreg` aufrufen.
    scanner_node: Callable[[], Awaitable[usb.UsbNode | None]] = usb.scanner_node,
    # „Hält ein Scan gerade den Einzug?" — eingespritzt, damit der Wächter das
    # Gerät nicht abfragt, während der Scan es hält. Vorgabe: nein.
    device_held: Callable[[], bool] = sane.device_held,
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
    # 🛑 ZWEITER Zähler, bewusst nicht `errors`. `errors` ist monoton und trägt
    # die Protokolldrosselung (eine Meldung je Minute) und die Erholungsmeldung
    # („wieder lesbar nach N Fehlversuch(en)"); würde man ihn für den Riegel
    # zurücksetzen, käme die Warnung alle 21 s und die Zahl in der
    # Erholungsmeldung wäre falsch. Dieser hier zählt NUR bis zur nächsten
    # Suche und wird dabei zurückgesetzt.
    since_rediscover = 0
    leben = liveness if liveness is not None else LIVENESS
    leben.loop_started()

    while max_iterations is None or iterations < max_iterations:
        iterations += 1
        if device_held():
            # 🛑 Der Scan hält den Einzug — ein Sensorabruf daneben MUSS
            # scheitern. Am 2026-10-21 gemessen: 4 Fehlschläge über 31 s, und
            # nach dreien löste der Fehlerpfad eine 6-Sekunden-Vollsuche aus,
            # mitten im laufenden Scan. Last auf genau dem Anschluss, um den es
            # geht — und `watcher_health` las den Zusammenstoß als „verklemmt".
            #
            # 🛑 Die Marke wird TROTZDEM gesetzt: ein übersprungener Takt ist
            # ein VOLLENDETER Durchlauf. Ohne sie liefe ein langer Scan (bis
            # 1800 s) über `LIVENESS_STALE_AFTER` (370 s) und die Aufsicht
            # erklärte den Knopf für tot — derselbe Fehlalarm, den das
            # Nicht-Sperren von `scan_batch` gerade vermeiden soll.
            leben.tick()
            await sleep(IDLE_INTERVAL)
            continue
        try:
            if device is None:
                device = await find_device(config.device)
                logger.info("Knopfwächter: Gerät %s (%s)", device,
                            await _knoten(scanner_node))
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
                # 🛑 Der Knoten hängt an der GEDROSSELTEN Zeile, nicht am Takt:
                # ein `ioreg` je Minute, nicht eines je fünf Sekunden. Er
                # trennt in der Auswertung „Scanner aus" von „verklemmt" —
                # beides ergab bis 2026-10-12 dasselbe Bild, und genau deshalb
                # sind am 2026-10-11 15,3 h Ausfall unbemerkt verstrichen.
                logger.warning("Knopfwächter: Sensor nicht lesbar (%s) — "
                               "%d. Versuch seit dem letzten Erfolg [%s]",
                               exc, errors, await _knoten(scanner_node))
            # 🛑 Das Geraet NICHT bei jedem Fehler neu suchen. Die Suche
            # (`scanimage -f`) ist der langsame Pfad — sie durchkaemmt alle
            # SANE-Backends und belegt dabei den USB-Anschluss. Die erste
            # Fassung setzte hier `device = None` und erzwang damit alle fuenf
            # Sekunden genau diese Suche; eine haengende Suche hielt das Geraet,
            # die naechste fand es deshalb nicht, und der Schaden vervielfachte
            # sich im Takt. Am 2026-09-25 hat das den Scanner festgesetzt, bis
            # er vom Strom genommen wurde.
            # Erst nach mehreren Fehlschlaegen in Folge neu suchen.
            #
            # 🛑 Und danach ZURUECKSETZEN. Bis 2026-10-12 stand hier
            # `errors >= REDISCOVER_AFTER`, und `errors` wird ausschliesslich im
            # Erfolgszweig genullt — die Bedingung wurde also einmal wahr und
            # blieb es fuer immer: ab dem dritten Fehlschlag lief die
            # Vollaufzaehlung in JEDEM Takt. Die Reparatur vom 2026-09-25 hatte
            # damit nur den Beginn um drei Takte verschoben, nicht den
            # Dauerzustand beseitigt. Gemessen am 2026-10-12: 4956 Versuche in
            # 15,3 h = 11,11 s je Versuch, und Suche (6,17 s) + ERROR_BACKOFF
            # (5,0 s) = 11,17 s — die Suche steckte in jedem einzelnen Versuch.
            # Mit dem Vorsatz des Kommentars (eine Suche je drei Fehlschlaege)
            # faellt die Belegung des USB-Anschlusses von 55,2 % auf 29,1 %.
            since_rediscover += 1
            if since_rediscover >= REDISCOVER_AFTER:
                device = None
                since_rediscover = 0
            pressed_before = False
            # Ein VOLLENDETER Durchlauf — auch dieser hier. Begründung zur
            # Stelle der Marke s. unten am Ende des Erfolgszweigs.
            leben.tick()
            await sleep(ERROR_BACKOFF)
            continue
        leben.note_device(True)
        if errors:
            logger.info("Knopfwächter: Sensor wieder lesbar nach %d Fehlversuch(en)",
                        errors)
            errors = 0
            since_rediscover = 0

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

        # 🛑 Die Zeitmarke steht am ENDE des Durchlaufs, nicht an seinem
        # Anfang. Am Anfang bedeutete sie nur „die Schleife wurde BETRETEN",
        # und das ist in einem Absturzkreis dauernd der Fall: die Aufsicht
        # startet neu, der Wächter betritt die Schleife, die Marke ist frisch —
        # und das Feld meldete `alive`, während der Knopf faktisch tot war. Im
        # Review vom 2026-10-11 nachgemessen. Am Ende bedeutet sie „ein
        # Durchlauf ist VOLLENDET": Sensoren versucht, Flanke ausgewertet,
        # Takt gewählt. Der Fehlerzweig oben zählt dazu — er ist ein
        # vollständiger Durchlauf, nur ohne Gerät (`note_device`).
        leben.tick()
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
    device_held: Callable[[], bool] = sane.device_held,
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
            await run(config, start_job, device_held=device_held)
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
