#!/usr/bin/env python3
"""jvc2mqtt – the JVC KD-SX38MBT as Home Assistant entities over MQTT.

Holds the radio's JVC Remote link (RFCOMM, PROTOCOL.md): answers the hello, ACKs
every frame, sets the radio's clock on connect and hourly, reconnects when the
link drops, and maps MQTT Discovery entities to the radio's settings:

  volume (Vol+/Vol− steps), fader, balance, EQ preset, 13 EQ bands, loudness,
  bass boost, bass extend, source; plus status sensors (source, tuner, RDS, track).

The radio takes one remote at a time. The "Remote link" switch lets go of it so
the JVC Remote phone app can connect; turn it back on to take it again.

Settings: /etc/jvc2mqtt/jvc2mqtt.conf. MQTT password: /etc/jvc2mqtt/mqtt-password
(mode 600, never in the config). Run by jvc2mqtt.service.
"""
import json
import os
import queue
import re
import select
import signal
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import jvcproto as P  # noqa: E402

import paho.mqtt.client as mqtt  # noqa: E402

CONF = "/etc/jvc2mqtt/jvc2mqtt.conf"
MQTT_PASSWORD_FILE = "/etc/jvc2mqtt/mqtt-password"
STATE_FILE = "/var/lib/jvc2mqtt/state.json"
CLOCK_EVERY = 3600          # re-set the radio's clock (drift, DST)
EQ_SAVE_DELAY = 1.0         # band edits are gathered, then saved together (one 3d 05)
VOLUME_STEP_WAIT = 1.0      # wait for the radio's 12 04 after each Vol± key


def log(msg):
    print(msg, flush=True)


def read_conf(path=CONF):
    conf = {}
    try:
        with open(path) as f:
            for line in f:
                m = re.match(r'\s*([A-Z_][A-Z0-9_]*)="(.*)"\s*$', line)
                if m:
                    conf[m.group(1)] = re.sub(r'\\(.)', r'\1', m.group(2))
    except OSError:
        pass
    return conf


def mqtt_host(conf):
    """MQTT_HOST, else the host in HA_API_URL (the Mosquitto app runs on HA), else homeassistant.local."""
    if conf.get("MQTT_HOST"):
        return conf["MQTT_HOST"]
    m = re.match(r"^[a-z]+://\[?([^/:\]]+)", conf.get("HA_API_URL") or "")
    return m.group(1) if m else "homeassistant.local"


def read_secret(path):
    try:
        with open(path) as f:
            return f.read().strip() or None
    except OSError:
        return None


# ═══ the radio link ═══════════════════════════════════════════════════════════

class LinkDown(Exception):
    pass


class RadioLink:
    """One open RFCOMM link: framing, ACKs, the hello, request/reply."""

    def __init__(self, sock, on_frame):
        self.s, self.on_frame = sock, on_frame
        self.deframer, self.seq = P.Deframer(), 0
        self.acked = set()
        self.replies = {}           # (grp, cmd) → (count, payload)
        self.hello = False

    def close(self):
        try:
            self.s.close()
        except OSError:
            pass

    def send(self, grp, cmd, payload=b""):
        seq = self.seq
        self.seq = (self.seq + 1) & 0xFF
        self.acked.discard(seq)
        try:
            self.s.sendall(P.build_frame(P.PHONE, P.RADIO, grp, cmd, seq, payload))
        except OSError as e:
            raise LinkDown(f"send failed: {e}") from e
        return seq

    def pump(self, seconds):
        end = time.monotonic() + seconds
        while True:
            left = end - time.monotonic()
            if left <= 0:
                return
            r, _, _ = select.select([self.s], [], [], min(left, 0.5))
            if not r:
                continue
            try:
                data = self.s.recv(4096)
            except OSError as e:
                raise LinkDown(f"link dropped ({e.strerror or e})") from e
            if not data:
                raise LinkDown("link closed by the radio")
            for f in self.deframer.feed(data):
                self._handle(f)

    def _handle(self, f):
        if f.ack:
            self.acked.add(f.seq)
            return
        try:   # ACK it, keeping the radio's src/dst order, like the app
            self.s.sendall(P.build_frame(f.src, f.dst, f.grp, f.cmd, f.seq, b"\x00", ack=True))
        except OSError as e:
            raise LinkDown(f"send failed: {e}") from e
        if (f.grp, f.cmd) == (0x3B, 0x02):
            self.hello = True
            self.send(0x5B, 0x02, bytes([0, 0, 1, 1]))
            return
        n = self.replies.get((f.grp, f.cmd), (0, b""))[0]
        self.replies[(f.grp, f.cmd)] = (n + 1, f.payload)
        self.on_frame(f)

    def request(self, grp, cmd, payload=b"", reply=None, timeout=2.0):
        """Send; wait for the ACK and, if `reply` is given, that reply frame.
        Returns the reply payload, True for an ACK-only request, or None."""
        before = self.replies.get(reply, (0, b""))[0] if reply else 0
        seq = self.send(grp, cmd, payload)
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            got_ack = seq in self.acked
            if reply:
                n, pl = self.replies.get(reply, (0, b""))
                if n > before:
                    return pl
            elif got_ack:
                return True
            self.pump(0.05)
        return None


def connect(mac, channel, adapter_addr=None, timeout=8.0):
    s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
    s.settimeout(timeout)
    try:
        if adapter_addr:
            s.bind((adapter_addr, 0))
        s.connect((mac, channel))
    except OSError:
        s.close()
        raise
    s.settimeout(None)
    return s


# ═══ the bridge ═══════════════════════════════════════════════════════════════

class Bridge:
    def __init__(self, conf):
        self.conf = conf
        self.mac = conf.get("JVC_MAC", "")
        if not re.fullmatch(r"([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", self.mac):
            sys.exit(f"JVC_MAC is not set in {CONF}")
        self.channel = int(conf.get("JVC_CHANNEL") or 16)
        adapter = conf.get("BT_ADAPTER") or "hci0"
        self.adapter_addr = read_secret(f"/sys/class/bluetooth/{adapter}/address")
        self.reconnect = max(5, int(conf.get("RADIO_RECONNECT_SECONDS") or 15))
        self.volume_limit = int(conf.get("VOLUME_LIMIT") or 25)
        self.prefix = (conf.get("MQTT_PREFIX") or "jvc2mqtt").strip("/")
        self.disc = (conf.get("HA_DISCOVERY_PREFIX") or "homeassistant").strip("/")
        self.obj = re.sub(r"[^a-z0-9_]", "_", (conf.get("RADIO_ENTITY_PREFIX") or "car_radio").lower())
        self.base = f"{self.prefix}/{self.obj}"
        self.uid = "jvc2mqtt_" + self.mac.replace(":", "").lower()

        self.cmds = queue.Queue()
        self.state = {}               # key → last published value
        self.volume, self.volume_n = None, 0
        self.fb = None                # (fader, balance)
        self.eq = None                # jvcproto.EqBands
        self.eq_dirty_at = None
        self.link = None
        self.link_enabled = self._load_state().get("link", True)
        self.stop = threading.Event()
        self.rds = ""

        self.entities = self._entities()
        self.mqtt = self._mqtt_client()

    # ── persistent bits ──────────────────────────────────────────────────────
    def _load_state(self):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save_state(self):
        try:
            os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
            with open(STATE_FILE, "w") as f:
                json.dump({"link": self.link_enabled}, f)
        except OSError as e:
            log(f"can't save {STATE_FILE}: {e}")

    # ── entities ─────────────────────────────────────────────────────────────
    def _entities(self):
        """key → (component, discovery config). Entity IDs are <component>.<obj>_<key>."""
        e = {}

        def add(key, comp, name, settable=True, radio=True, **cfg):
            cfg.update(name=name)
            cfg["_settable"], cfg["_radio"] = settable, radio
            e[key] = (comp, cfg)

        add("volume", "number", "Volume", min=0, max=self.volume_limit, step=1, mode="slider",
            icon="mdi:volume-high")
        add("fader", "number", "Fader", min=-15, max=15, step=1, mode="slider", icon="mdi:car-seat",
            entity_category="config")
        add("balance", "number", "Balance", min=-15, max=15, step=1, mode="slider",
            icon="mdi:speaker-multiple", entity_category="config")
        add("eq_preset", "select", "EQ preset", options=list(P.EQ_PRESETS), icon="mdi:tune-vertical")
        for i, band in enumerate(P.EQ_BANDS):
            add(f"eq_band_{i + 1:02d}", "number", f"EQ {band}", min=-9, max=9, step=1, mode="slider",
                unit_of_measurement="dB", icon="mdi:equalizer", entity_category="config")
        add("loudness", "select", "Loudness", options=list(P.LOUDNESS), icon="mdi:volume-plus",
            entity_category="config")
        add("bass_boost", "select", "Bass boost", options=list(P.BASS_BOOST), icon="mdi:speaker",
            entity_category="config")
        add("bass_extend", "switch", "Bass extend", icon="mdi:speaker-wireless", entity_category="config")
        add("source", "select", "Source", options=list(P.SOURCES.values()), icon="mdi:import")
        add("tuner", "sensor", "Tuner frequency", settable=False, icon="mdi:radio")
        add("rds", "sensor", "RDS text", settable=False, icon="mdi:card-text-outline")
        add("title", "sensor", "Track title", settable=False, icon="mdi:music")
        add("artist", "sensor", "Track artist", settable=False, icon="mdi:account-music")
        add("remote_link", "switch", "Remote link", radio=False, icon="mdi:bluetooth-connect",
            entity_category="config")
        add("connected", "binary_sensor", "Connected", settable=False, radio=False,
            device_class="connectivity", entity_category="diagnostic")
        return e

    def entity_id(self, key):
        return f"{self.entities[key][0]}.{self.obj}_{key}"

    def discovery(self):
        dev = {"identifiers": [self.uid], "name": "Car Radio", "manufacturer": "JVC",
               "model": "KD-SX38MBT", "connections": [["bluetooth", self.mac.lower()]]}
        for key, (comp, cfg) in self.entities.items():
            c = {k: v for k, v in cfg.items() if not k.startswith("_")}
            c.update(unique_id=f"{self.uid}_{key}", default_entity_id=self.entity_id(key),
                     state_topic=f"{self.base}/{key}", device=dev,
                     availability_mode="all",
                     availability=[{"topic": f"{self.base}/status"}]
                     + ([{"topic": f"{self.base}/radio"}] if cfg["_radio"] else []))
            if cfg["_settable"]:
                c["command_topic"] = f"{self.base}/{key}/set"
            if comp == "switch":
                c.update(payload_on="ON", payload_off="OFF")
            if comp == "binary_sensor":
                c.update(payload_on="ON", payload_off="OFF")
            self.mqtt.publish(f"{self.disc}/{comp}/{self.uid}/{key}/config", json.dumps(c), retain=True)

    # ── MQTT ─────────────────────────────────────────────────────────────────
    def _mqtt_client(self):
        cid = f"jvc2mqtt-{socket.gethostname()}"
        try:   # paho 2.x
            c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=cid)
        except AttributeError:   # paho 1.x
            c = mqtt.Client(client_id=cid)
        user = self.conf.get("MQTT_USER") or None
        if user:
            c.username_pw_set(user, read_secret(MQTT_PASSWORD_FILE))
        c.will_set(f"{self.base}/status", "offline", retain=True)
        c.on_connect = self._on_connect
        c.on_connect_fail = lambda *_: log("MQTT broker not reachable; retrying")
        c.on_message = self._on_message
        return c

    def _on_connect(self, client, userdata, flags, rc, *_):
        if (getattr(rc, "is_failure", None) is True) or (isinstance(rc, int) and rc != 0):
            log(f"MQTT connect refused: {rc}")
            return
        log("MQTT connected")
        client.subscribe(f"{self.base}/+/set")
        client.subscribe(f"{self.disc}/status")      # HA restarted → send discovery again
        self.discovery()
        client.publish(f"{self.base}/status", "online", retain=True)
        self.publish("radio", "online" if self.link else "offline", raw=True)
        for k, v in list(self.state.items()):
            client.publish(f"{self.base}/{k}", v, retain=True)
        self.publish("remote_link", "ON" if self.link_enabled else "OFF")
        self.publish("connected", "ON" if self.link else "OFF")

    def _on_message(self, client, userdata, msg):
        if msg.topic == f"{self.disc}/status":
            if msg.payload == b"online":
                self.discovery()
            return
        key = msg.topic[len(self.base) + 1:-len("/set")]
        if key in self.entities:
            self.cmds.put((key, msg.payload.decode("utf-8", "replace").strip()))

    def publish(self, key, value, raw=False):
        v = "" if value is None else str(value)
        if not raw:
            self.state[key] = v
        self.mqtt.publish(f"{self.base}/{key}", v, retain=True)

    # ── radio → HA ───────────────────────────────────────────────────────────
    def on_frame(self, f):
        k, p = (f.grp, f.cmd), f.payload
        if k == (0x12, 0x04) and p:
            self.volume, self.volume_n = p[0], self.volume_n + 1
            self.publish("volume", p[0])
        elif k == (0x12, 0x02):
            self._eq_name(P.ascii_field(p))
        elif k == (0x12, 0x01) and len(p) >= 3:
            self.publish("source", P.SOURCES.get((p[1], p[2]), f"{p[1]:02x} {p[2]:02x}"))
            if len(p) > 4:
                self._eq_name(P.ascii_field(p, 4))
        elif k == (0x8C, 0x01) and len(p) == 4:
            fr = int.from_bytes(p[2:4], "big")
            self.publish("tuner", f"FM {fr / 100:.1f} MHz" if p[0] == 2 else f"AM {fr} kHz" if p[0] == 1 else "")
        elif k == (0x5C, 0x20) and len(p) > 1:
            self.publish("rds", P.ascii_field(p, 1))
        elif k == (0x5B, 0x80):
            self.publish("title", P.track_text(p))
        elif k == (0x5B, 0x81):
            self.publish("artist", P.track_text(p))
        elif f.grp == 0x5D:
            self._setting_reply(f.cmd, p)

    def _eq_name(self, name):
        for n in P.EQ_PRESETS:
            if n.replace(" ", "") == name.replace(" ", ""):
                self.publish("eq_preset", n)
                return

    def _setting_reply(self, cmd, p):
        """Replies to the 'get' half of each setting (the 'set' replies are just [01])."""
        if cmd == 0x0E and p and p[0] < len(P.EQ_PRESETS):
            self.publish("eq_preset", P.EQ_PRESETS[p[0]])
        elif cmd == 0x0B:
            fb = P.fader_balance_from(p)
            if fb:
                self.fb = fb
                self.publish("fader", fb[0])
                self.publish("balance", fb[1])
            else:
                log(f"fader/balance reply not understood: [{p.hex(' ')}]")
        elif cmd == 0x04:
            try:
                self.eq = P.EqBands(p)
            except ValueError as e:
                log(f"EQ bands: {e} [{p.hex(' ')}]")
                return
            if self.eq_dirty_at is None:
                for i in range(13):
                    self.publish(f"eq_band_{i + 1:02d}", self.eq.db(i))
        elif cmd == 0x4C and p and p[0] < len(P.LOUDNESS):
            self.publish("loudness", P.LOUDNESS[p[0]])
        elif cmd == 0x49 and p and p[0] < len(P.BASS_BOOST):
            self.publish("bass_boost", P.BASS_BOOST[p[0]])
        elif cmd == 0x40 and p:
            self.publish("bass_extend", "ON" if p[0] else "OFF")

    def read_settings(self):
        for cmd in (0x0E, 0x0B, 0x04, 0x4C, 0x49, 0x40):
            if self.link.request(0x3D, cmd, reply=(0x5D, cmd)) is None:
                log(f"no reply to 3d {cmd:02x}")

    # ── HA → radio ───────────────────────────────────────────────────────────
    def _set(self, cmd, payload, get):
        ok = self.link.request(0x3D, cmd, payload, reply=(0x5D, cmd))
        if ok is None or (ok and ok[0] != 1):
            log(f"radio refused 3d {cmd:02x} [{bytes(payload).hex(' ')}]: {ok!r}")
        self.link.request(0x3D, get, reply=(0x5D, get))

    def handle(self, key, val):
        if key == "remote_link":
            self.link_enabled = val.upper() == "ON"
            self._save_state()
            self.publish("remote_link", "ON" if self.link_enabled else "OFF")
            if not self.link_enabled and self.link:
                log("remote link released (for the JVC Remote app)")
                raise LinkDown("released")
            return
        if not self.link:
            return   # entities are unavailable while the radio is away
        try:
            if key == "volume":
                self.set_volume(int(float(val)))
            elif key in ("fader", "balance"):
                fader, balance = self.fb or (0, 0)
                v = max(-15, min(15, int(float(val))))
                fader, balance = (v, balance) if key == "fader" else (fader, v)
                self._set(0x0C, P.fader_balance_payload(fader, balance), 0x0B)
            elif key == "eq_preset" and val in P.EQ_PRESETS:
                self._set(0x0F, bytes([P.EQ_PRESETS.index(val)]), 0x0E)
            elif key.startswith("eq_band_"):
                if self.eq is None:
                    log("EQ bands not read from the radio yet; ignoring")
                    return
                i = int(key[-2:]) - 1
                self.eq.set_db(i, max(-9, min(9, int(float(val)))))
                self.publish(key, self.eq.db(i))
                self.eq_dirty_at = time.monotonic()
            elif key == "loudness" and val in P.LOUDNESS:
                self._set(0x4D, bytes([P.LOUDNESS.index(val)]), 0x4C)
            elif key == "bass_boost" and val in P.BASS_BOOST:
                self._set(0x4A, bytes([P.BASS_BOOST.index(val)]), 0x49)
            elif key == "bass_extend":
                self._set(0x41, bytes([1 if val.upper() == "ON" else 0]), 0x40)
            elif key == "source":
                for sid, name in P.SOURCES.items():
                    if name == val:
                        self.link.request(0x11, 0x01, bytes(sid))
        except ValueError:
            log(f"bad value for {key}: {val!r}")

    def save_eq(self):
        self.eq_dirty_at = None
        log("saving EQ bands")
        ok = self.link.request(0x3D, 0x05, self.eq.payload(), reply=(0x5D, 0x05))
        if ok is None or (ok and ok[0] != 1):
            log(f"radio refused the EQ bands: {ok!r}")
        self.link.request(0x3D, 0x04, reply=(0x5D, 0x04))
        self.link.request(0x3D, 0x0E, reply=(0x5D, 0x0E))

    def _key_and_wait(self, key):
        n = self.volume_n
        self.link.request(0x11, 0x05, bytes([key]))
        end = time.monotonic() + VOLUME_STEP_WAIT
        while self.volume_n == n and time.monotonic() < end:
            self.link.pump(0.05)
        return self.volume_n != n

    def set_volume(self, target):
        """The radio has only Vol+/Vol−: step there, checking each 12 04 report."""
        target = max(0, min(self.volume_limit, target))
        if self.volume is None and not self._key_and_wait(P.KEY_VOL_DOWN):   # learn where it is
            log("radio didn't report its volume")
            return
        for _ in range(60):
            if self.volume == target:
                return
            if not self._key_and_wait(P.KEY_VOL_UP if self.volume < target else P.KEY_VOL_DOWN):
                log("radio stopped reporting volume; giving up")
                self.publish("volume", self.volume)
                return

    # ── session / main loop ──────────────────────────────────────────────────
    def set_clock(self):
        t = time.localtime()
        dow = (t.tm_wday + 1) % 7   # Python Mon=0 → radio Sun=0
        self.link.request(0x3B, 0x35, bytes([dow, t.tm_hour, t.tm_min, t.tm_sec]), reply=(0x5B, 0x35))
        self.link.request(0x3B, 0x37, bytes([t.tm_year // 100, t.tm_year % 100, t.tm_mon, t.tm_mday]),
                          reply=(0x5B, 0x37))

    def session(self, sock):
        self.link = RadioLink(sock, self.on_frame)
        try:
            end = time.monotonic() + 5
            while not self.link.hello and time.monotonic() < end:
                self.link.pump(0.2)
            for cmd in (0x01, 0x34, 0x36):
                self.link.request(0x3B, cmd, reply=(0x5B, cmd))
            self.set_clock()
            self.link.request(0x3B, 0x12, reply=(0x5B, 0x12))
            self.link.request(0x3B, 0x30, reply=(0x5B, 0x30))
            self.link.pump(3)      # rest of the source list; commands sent during it are ignored
            self.read_settings()
            self.publish("radio", "online", raw=True)
            self.publish("connected", "ON")
            log("radio connected")
            next_clock = time.monotonic() + CLOCK_EVERY
            while not self.stop.is_set():
                try:
                    key, val = self.cmds.get_nowait()
                    self.handle(key, val)
                    continue
                except queue.Empty:
                    pass
                if self.eq_dirty_at and time.monotonic() - self.eq_dirty_at > EQ_SAVE_DELAY:
                    self.save_eq()
                if time.monotonic() > next_clock:
                    self.set_clock()
                    next_clock = time.monotonic() + CLOCK_EVERY
                self.link.pump(0.1)
        finally:
            self.link.close()
            self.link = None
            self.volume, self.eq_dirty_at = None, None
            self.publish("radio", "offline", raw=True)
            self.publish("connected", "OFF")

    def run(self):
        host = mqtt_host(self.conf)
        port = int(self.conf.get("MQTT_PORT") or 1883)
        self.mqtt.connect_async(host, port, keepalive=30)
        self.mqtt.loop_start()
        log(f"jvc2mqtt: radio {self.mac} ch {self.channel}, MQTT {host}:{port}, topics {self.base}/…")
        last_err = None
        while not self.stop.is_set():
            if not self.link_enabled:
                self._idle(1)
                continue
            try:
                sock = connect(self.mac, self.channel, self.adapter_addr)
            except OSError as e:
                err = e.strerror or str(e)
                if err != last_err:
                    log(f"radio not reachable ({err}); retrying every {self.reconnect} s")
                    last_err = err
                self._idle(self.reconnect)
                continue
            last_err = None
            try:
                self.session(sock)
            except LinkDown as e:
                log(f"radio: {e}")
            self._idle(2)
        self.mqtt.publish(f"{self.base}/status", "offline", retain=True)
        self.mqtt.loop_stop()

    def _idle(self, seconds):
        """Wait, but still take commands (only remote_link matters while offline)."""
        end = time.monotonic() + seconds
        while not self.stop.is_set() and time.monotonic() < end:
            try:
                key, val = self.cmds.get(timeout=0.5)
            except queue.Empty:
                continue
            if key == "remote_link":
                try:
                    self.handle(key, val)
                except LinkDown:
                    pass
                if self.link_enabled:
                    return


def main():
    b = Bridge(read_conf())
    signal.signal(signal.SIGTERM, lambda *_: b.stop.set())
    try:
        b.run()
    except KeyboardInterrupt:
        b.stop.set()


if __name__ == "__main__":
    main()
