#!/usr/bin/env python3
"""avrcp-player.py: tell the JVC radio what Music Assistant is playing.

The radio plays our Bluetooth audio (A2DP) but only shows play state and track
info, and only sends its play/pause/next buttons somewhere, when the source also
offers an AVRCP media player. This registers one with BlueZ (an MPRIS-style
player, the same thing BlueZ's mpris-proxy does for desktop players):

  Home Assistant media_player (Music Assistant's player for the radio)
      → polled over the REST API → PlaybackStatus / Metadata / Position → radio
  radio buttons → BlueZ → Play/Pause/Next/Previous here → HA media_player services

Settings come from /etc/jvc2mqtt/jvc2mqtt.conf (HA_API_URL, RADIO_HA_ENTITY, BT_ADAPTER,
RADIO_IDLE_STATUS, RADIO_IDLE_TEXT);
the Home Assistant long-lived token from /etc/jvc2mqtt/ha-token.
Run by jvc2mqtt-radio-avrcp.service.
"""
import datetime
import json
import re
import sys
import threading
import urllib.error
import urllib.request

import dbus
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib

CONF = "/etc/jvc2mqtt/jvc2mqtt.conf"
TOKEN_FILE = "/etc/jvc2mqtt/ha-token"
PLAYER_PATH = "/org/jvc2mqtt/radio/player"
MPRIS = "org.mpris.MediaPlayer2"
PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"
PROPS_IFACE = "org.freedesktop.DBus.Properties"
POLL_SECONDS = 2
# Button presses are sent from worker threads so the D-Bus main loop never waits
# on Home Assistant (it used to block up to 5 s per press when Music Assistant
# hung, and presses piled up). At most this many calls may be in flight; more
# presses while HA is stuck are dropped instead of queued and replayed later.
MAX_CALLS_IN_FLIGHT = 2
CALL_TIMEOUT = 10   # seconds a worker waits for HA's reply (nothing else waits on it)


def log(msg):
    print(msg, flush=True)


def read_conf():
    conf = {}
    try:
        with open(CONF) as f:
            for line in f:
                m = re.match(r'\s*([A-Z_][A-Z0-9_]*)="(.*)"\s*$', line)
                if m:
                    conf[m.group(1)] = re.sub(r'\\(.)', r'\1', m.group(2))
    except OSError:
        pass
    return conf


def ha_base_url(conf):
    """HA_API_URL, else Home Assistant's usual local address."""
    return (conf.get("HA_API_URL") or "http://homeassistant.local:8123").rstrip("/")


class HomeAssistant:
    def __init__(self, base, token, entity):
        self.base, self.token, self.entity = base, token, entity

        self._slots = threading.BoundedSemaphore(MAX_CALLS_IN_FLIGHT)

    def _req(self, method, path, body=None, timeout=5):
        req = urllib.request.Request(
            self.base + path, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"null")

    def state(self):
        return self._req("GET", f"/api/states/{self.entity}")

    def call_async(self, service, done=None):
        """Fire a media_player service call from a worker thread and return at once.
        Returns False (and sends nothing) if too many calls are still waiting on HA.
        `done(error_or_None)` runs on the GLib main loop afterwards."""
        if not self._slots.acquire(blocking=False):
            return False

        def work():
            err = None
            try:
                self._req("POST", f"/api/services/media_player/{service}",
                          {"entity_id": self.entity}, timeout=CALL_TIMEOUT)
            except (urllib.error.URLError, OSError, ValueError) as e:
                err = e
            finally:
                self._slots.release()
            if done:
                GLib.idle_add(done, err)

        threading.Thread(target=work, name=f"ha-{service}", daemon=True).start()
        return True


TAGGED = re.compile(r'(?:^|\s)text="([^"]*)"')


def clean_track(title, artist):
    """Radio streams (iHeart and others) put a raw tag line in the title, e.g.
    'Def Leppard - text="Photograph" song_spot="M" MediaBaseId="0" ...'.
    Return (title, artist) with just the song and its artist."""
    if not title:
        return title, artist
    m = TAGGED.search(title)
    if not m:
        return title, artist
    song = m.group(1).strip()
    prefix = title[:m.start()].strip().rstrip("-").strip()
    return song or prefix or title, prefix or artist


def to_status(state):
    return {"playing": "Playing", "paused": "Paused"}.get(state, "Stopped")


IDLE_STATUSES = {"stopped": "Stopped", "paused": "Paused", "playing": "Playing"}


def position_us(attrs, playing):
    pos = attrs.get("media_position")
    if pos is None:
        return 0
    pos = float(pos)
    if playing and attrs.get("media_position_updated_at"):
        try:
            t = datetime.datetime.fromisoformat(attrs["media_position_updated_at"])
            pos += (datetime.datetime.now(datetime.timezone.utc) - t).total_seconds()
        except ValueError:
            pass
    return int(max(pos, 0) * 1_000_000)


class Player(dbus.service.Object):
    def __init__(self, bus, ha, idle_status="Stopped", idle_text=""):
        super().__init__(bus, PLAYER_PATH)
        self.ha = ha
        # What the radio sees while HA's player is stopped/idle/off: the radio shows
        # STOP for "Stopped", so RADIO_IDLE_STATUS / RADIO_IDLE_TEXT can show text instead.
        self.idle_status, self.idle_text = idle_status, idle_text
        self.ha_playing = False   # HA's real state, for the play/pause button
        self.props = {
            "PlaybackStatus": dbus.String("Stopped"),
            "LoopStatus": dbus.String("None"),
            "Rate": dbus.Double(1.0),
            "Shuffle": dbus.Boolean(False),
            "Metadata": dbus.Dictionary({}, signature="sv"),
            "Position": dbus.Int64(0),
            "MinimumRate": dbus.Double(1.0),
            "MaximumRate": dbus.Double(1.0),
            "CanGoNext": dbus.Boolean(True),
            "CanGoPrevious": dbus.Boolean(True),
            "CanPlay": dbus.Boolean(True),
            "CanPause": dbus.Boolean(True),
            "CanSeek": dbus.Boolean(False),
            "CanControl": dbus.Boolean(True),
        }
        self.track = None
        self.error = None
        self.polling = False

    def _error(self, msg):   # log each problem once, not every poll
        if msg != self.error:
            log(msg)
            self.error = msg

    # ── radio buttons (BlueZ calls these) ───────────────────────────────────
    def _do(self, service):
        """Hand the press to HA without waiting; BlueZ gets its D-Bus reply at once."""
        def done(err):
            if err is not None:
                log(f"Home Assistant call {service}: {err}")
            self.refresh()

        if self.ha.call_async(service, done):
            log(f"radio button → {service}")
        else:
            log(f"radio button → {service} dropped (Home Assistant still busy with earlier presses)")
        GLib.timeout_add(500, self.refresh)

    @dbus.service.method(PLAYER_IFACE)
    def Play(self):
        self._do("media_play")

    @dbus.service.method(PLAYER_IFACE)
    def Pause(self):
        # While idle is shown as "Playing" the radio sends Pause for its play button
        self._do("media_pause" if self.ha_playing else "media_play")

    @dbus.service.method(PLAYER_IFACE)
    def PlayPause(self):
        self._do("media_play_pause")

    @dbus.service.method(PLAYER_IFACE)
    def Stop(self):
        self._do("media_stop")

    @dbus.service.method(PLAYER_IFACE)
    def Next(self):
        self._do("media_next_track")

    @dbus.service.method(PLAYER_IFACE)
    def Previous(self):
        self._do("media_previous_track")

    @dbus.service.method(PLAYER_IFACE, in_signature="x")
    def Seek(self, offset):
        pass

    # ── properties ──────────────────────────────────────────────────────────
    @dbus.service.method(PROPS_IFACE, in_signature="ss", out_signature="v")
    def Get(self, iface, name):
        if iface == MPRIS:
            return {"Identity": "Music Assistant", "CanQuit": False, "CanRaise": False}.get(name, "")
        return self.props[name]

    @dbus.service.method(PROPS_IFACE, in_signature="s", out_signature="a{sv}")
    def GetAll(self, iface):
        if iface == MPRIS:
            return {"Identity": "Music Assistant", "CanQuit": False, "CanRaise": False,
                    "HasTrackList": False, "SupportedUriSchemes": dbus.Array([], signature="s"),
                    "SupportedMimeTypes": dbus.Array([], signature="s")}
        return self.props

    @dbus.service.method(PROPS_IFACE, in_signature="ssv")
    def Set(self, iface, name, value):
        pass  # the radio can't change shuffle/loop on Music Assistant from here

    @dbus.service.signal(PROPS_IFACE, signature="sa{sv}as")
    def PropertiesChanged(self, iface, changed, invalidated):
        pass

    # ── Home Assistant → radio ──────────────────────────────────────────────
    def refresh(self):
        """Poll HA's state in a worker thread; the result is applied on the main loop.
        Only one poll runs at a time, so a slow HA can't stack them up either."""
        if self.polling:
            return False
        self.polling = True

        def work():
            try:
                res = ("ok", self.ha.state())
            except urllib.error.HTTPError as e:
                res = ("err", f"Home Assistant: HTTP {e.code} for {self.ha.entity}"
                       + (" (check the token)" if e.code == 401 else ""))
            except (urllib.error.URLError, OSError, ValueError) as e:
                res = ("err", f"Home Assistant unreachable: {e}")
            GLib.idle_add(self._apply, res)

        threading.Thread(target=work, name="ha-poll", daemon=True).start()
        return False

    def _apply(self, res):
        self.polling = False
        kind, st = res
        if kind == "err":
            self._error(st)
            return False
        if self.error:
            log("Home Assistant OK again")
            self.error = None
        a = st.get("attributes", {}) if isinstance(st, dict) else {}
        status = to_status(st.get("state") if isinstance(st, dict) else None)
        self.ha_playing = status == "Playing"
        title, artist = clean_track(a.get("media_title"), a.get("media_artist"))
        if status == "Stopped":
            status = self.idle_status
            if self.idle_text:
                title, artist, a = self.idle_text, None, {}
        a = dict(a, media_title=title, media_artist=artist)
        meta = {}
        if title:
            meta["xesam:title"] = dbus.String(title)
        if artist:
            meta["xesam:artist"] = dbus.Array([artist], signature="s")
        if a.get("media_album_name"):
            meta["xesam:album"] = dbus.String(a["media_album_name"])
        if a.get("media_duration"):
            meta["mpris:length"] = dbus.Int64(int(float(a["media_duration"]) * 1_000_000))
        meta["mpris:trackid"] = dbus.ObjectPath(f"{PLAYER_PATH}/track/{abs(hash(a.get('media_title', ''))) % 10**9}")
        new = {
            "PlaybackStatus": dbus.String(status),
            "Metadata": dbus.Dictionary(meta, signature="sv"),
            "Position": dbus.Int64(position_us(a, self.ha_playing)),
        }
        changed = {k: v for k, v in new.items()
                   if k != "Position" and self.props.get(k) != v}
        if changed:   # BlueZ restarts its position clock from this
            changed["Position"] = new["Position"]
        self.props.update(new)
        if changed:
            self.PropertiesChanged(PLAYER_IFACE, changed, dbus.Array([], signature="s"))
            track = (a.get("media_artist"), a.get("media_title"))
            if "PlaybackStatus" in changed or track != self.track:
                log(f"{status}: {a.get('media_artist') or '-'} – {a.get('media_title') or '-'}")
                self.track = track
        return False


def main():
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    conf = read_conf()
    base = ha_base_url(conf)
    entity = conf.get("RADIO_HA_ENTITY") or (
        "media_player." + re.sub(r"[^a-z0-9]+", "_", conf.get("RADIO_AUDIO_NAME", "Car-Radio").lower()).strip("_"))
    try:
        token = open(TOKEN_FILE).read().strip()
    except OSError:
        token = ""
    if not base or not token:
        sys.exit(f"Need a Home Assistant token in {TOKEN_FILE}")
    adapter = conf.get("BT_ADAPTER") or "hci0"
    log(f"Following {entity} on {base} (Bluetooth adapter {adapter})")

    bus = dbus.SystemBus()
    idle = (conf.get("RADIO_IDLE_STATUS") or "stopped").strip().lower()
    if idle not in IDLE_STATUSES:
        log(f"RADIO_IDLE_STATUS={idle!r} not understood; using stopped")
        idle = "stopped"
    idle_text = (conf.get("RADIO_IDLE_TEXT") or "").strip()
    log(f"While stopped the radio is told: {IDLE_STATUSES[idle]}" + (f", \"{idle_text}\"" if idle_text else ""))
    player = Player(bus, HomeAssistant(base, token, entity), IDLE_STATUSES[idle], idle_text)
    registered = {"ok": False, "timer": None, "path": None, "missing": False}

    def find_adapter():
        """D-Bus path of BT_ADAPTER (a name or the adapter's address), or None."""
        manager = dbus.Interface(bus.get_object("org.bluez", "/"), "org.freedesktop.DBus.ObjectManager")
        for path, interfaces in manager.GetManagedObjects().items():
            props = interfaces.get("org.bluez.Adapter1")
            if props is not None and "org.bluez.Media1" in interfaces and (
                    str(path) == f"/org/bluez/{adapter}"
                    or str(props.get("Address", "")).upper() == adapter.upper()):
                return str(path)
        return None

    def register():
        try:
            path = find_adapter()
            if not path:
                if not registered["missing"]:
                    log(f"Bluetooth adapter {adapter} not found; waiting for it")
                    registered["missing"] = True
                return True
            registered["missing"], registered["path"] = False, path
            media = dbus.Interface(bus.get_object("org.bluez", path), "org.bluez.Media1")
            media.RegisterPlayer(dbus.ObjectPath(PLAYER_PATH), player.props)
            registered["ok"] = True
            log("Registered with BlueZ as the radio's media player")
        except dbus.DBusException as e:
            if "AlreadyExists" in e.get_dbus_name():
                registered["ok"] = True
            else:
                log(f"BlueZ RegisterPlayer failed ({e.get_dbus_name()}); retrying")
        if registered["ok"]:
            registered["timer"] = None
        return not registered["ok"]   # keep the retry timer going until it works

    def stop_retrying():
        if registered["timer"]:
            GLib.source_remove(registered["timer"])
            registered["timer"] = None

    def register_soon(seconds=2):
        registered["ok"] = False
        stop_retrying()   # never two retry timers
        registered["timer"] = GLib.timeout_add_seconds(seconds, register)

    def bluez_owner_changed(new_owner):
        # bluetoothd restarted: our registration is gone, do it again
        registered["ok"] = False
        stop_retrying()
        if new_owner:
            register_soon()

    def interfaces_added(path, interfaces):
        # the adapter came back (replugged, driver reloaded): BlueZ forgot our registration
        # any adapter may be ours under a new number; register() checks which one it is
        if not registered["ok"] and "org.bluez.Media1" in interfaces:
            register_soon()

    def interfaces_removed(path, interfaces):
        if str(path) == registered["path"] and "org.bluez.Media1" in interfaces:
            log(f"Bluetooth adapter {adapter} went away; will register again when it's back")
            registered["ok"], registered["path"] = False, None
            stop_retrying()

    for signal_name, handler in (("InterfacesAdded", interfaces_added),
                                 ("InterfacesRemoved", interfaces_removed)):
        bus.add_signal_receiver(handler, signal_name=signal_name, bus_name="org.bluez",
                                dbus_interface="org.freedesktop.DBus.ObjectManager")
    bus.watch_name_owner("org.bluez", bluez_owner_changed)

    def tick():
        player.refresh()
        return True

    register_soon(5)
    GLib.timeout_add_seconds(POLL_SECONDS, tick)
    tick()
    GLib.MainLoop().run()


if __name__ == "__main__":
    main()
