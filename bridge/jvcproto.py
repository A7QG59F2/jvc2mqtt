"""jvcproto – frames of the JVC Remote protocol (see PROTOCOL.md).

Shared by the jvc2mqtt bridge and its tests. Pure functions, no I/O.
"""

PHONE, RADIO = 0xA4, 0x50

# Source IDs (11 01 / 12 01)
SOURCES = {(0x02, 0x00): "AUX", (0x03, 0x01): "AM", (0x03, 0x02): "FM", (0x06, 0x14): "BT audio"}

EQ_PRESETS = ["FLAT", "DVRN 3", "DVRN 2", "DVRN 1", "HARDROCK", "HIP HOP", "JAZZ", "POP",
              "R&B", "USER", "CLASSICAL"]
# 13 EQ bands, 00–0c (PROTOCOL.md lists up to 6.3k; the last two are the usual 13-band steps)
EQ_BANDS = ["62.5 Hz", "100 Hz", "160 Hz", "250 Hz", "400 Hz", "630 Hz", "1 kHz", "1.6 kHz",
            "2.5 kHz", "4 kHz", "6.3 kHz", "10 kHz", "16 kHz"]
LOUDNESS = ["Off", "Level 1", "Level 2"]
BASS_BOOST = ["Off", "Level 1", "Level 2", "Level 3", "Level 4", "Level 5"]

KEY_MUTE_PLAY, KEY_SOUND, KEY_RIGHT, KEY_LEFT, KEY_UP, KEY_DOWN, KEY_VOL_DOWN, KEY_VOL_UP = range(1, 9)

CENTER = 0x0F   # fader / balance center; front and right are lower numbers


def crc16_arc(data):
    c = 0
    for b in data:
        c ^= b
        for _ in range(8):
            c = (c >> 1) ^ 0xA001 if c & 1 else c >> 1
    return c


def build_frame(src, dst, grp, cmd, seq, payload=b"", ack=False):
    body = bytes([0x0B, 0x01, src, dst, grp, cmd, seq & 0xFF, 1 if ack else 0, 0x01, 0x00,
                  len(payload)]) + bytes(payload)
    body += crc16_arc(body).to_bytes(2, "big")
    out = bytearray([0x7E])
    for b in body:
        out += bytes([0x7D, b ^ 0x20]) if b in (0x7D, 0x7E) else bytes([b])
    out.append(0x7C)
    return bytes(out)


class Frame:
    def __init__(self, body):
        self.src, self.dst, self.grp, self.cmd = body[2], body[3], body[4], body[5]
        self.seq, self.ack = body[6], bool(body[7])
        self.payload = bytes(body[11:11 + body[10]])
        self.crc_ok = crc16_arc(body[:-2]) == int.from_bytes(body[-2:], "big")

    def __repr__(self):
        return (f"Frame({self.src:02x}>{self.dst:02x} {self.grp:02x} {self.cmd:02x} seq={self.seq:02x}"
                f"{' ACK' if self.ack else ''} [{self.payload.hex(' ')}])")


class Deframer:
    """Feed raw RFCOMM bytes, get whole frames back (unescaped, CRC-checked)."""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data):
        self.buf += data
        out = []
        while True:
            start = self.buf.find(0x7E)
            if start < 0:
                self.buf.clear()
                break
            end = self.buf.find(0x7C, start + 1)
            if end < 0:
                del self.buf[:start]
                break
            raw = bytes(self.buf[start + 1:end])
            del self.buf[:end + 1]
            body, i = bytearray(), 0
            while i < len(raw):
                if raw[i] == 0x7D and i + 1 < len(raw):
                    body.append(raw[i + 1] ^ 0x20)
                    i += 2
                else:
                    body.append(raw[i])
                    i += 1
            if len(body) >= 13 and len(body) >= 13 + body[10]:
                f = Frame(bytes(body[:13 + body[10]]))
                if f.crc_ok:
                    out.append(f)
        return out


# ── value conversions (HA side ↔ radio bytes) ───────────────────────────────

def fader_balance_from(payload):
    """3d 0b reply / 3d 0c payload [00 f 01 b] → (fader, balance), + = front / right."""
    if len(payload) >= 4 and payload[0] == 0 and payload[2] == 1:
        return CENTER - payload[1], CENTER - payload[3]
    return None


def fader_balance_payload(fader, balance):
    return bytes([0x00, CENTER - int(fader), 0x01, CENTER - int(balance)])


class EqBands:
    """3d 04 reply / 3d 05 payload. PROTOCOL.md: [Q index] [band value] × 13, value = 9 − dB.
    Read as 13 (Q, value) pairs (26 bytes); a 14-byte reply is read as one Q + 13 values.
    Written back in the same layout it was read in."""

    def __init__(self, payload):
        p = bytes(payload)
        if len(p) == 26:
            self.layout, self.q = "pairs", [p[i] for i in range(0, 26, 2)]
            self.raw = [p[i] for i in range(1, 26, 2)]
        elif len(p) == 14:
            self.layout, self.q, self.raw = "single_q", [p[0]] * 13, list(p[1:])
        else:
            raise ValueError(f"unexpected EQ band payload length {len(p)}")

    def db(self, i):
        return 9 - self.raw[i]

    def set_db(self, i, db):
        self.raw[i] = 9 - int(db)

    def payload(self):
        if self.layout == "pairs":
            return bytes(b for i in range(13) for b in (self.q[i], self.raw[i]))
        return bytes([self.q[0]] + self.raw)


def ascii_field(payload, start=0):
    return bytes(payload[start:]).split(b"\0")[0].decode("ascii", "replace").strip()


def track_text(payload):
    """5b 80 / 81 / 91: 18-byte header, then text."""
    if len(payload) <= 18:
        return ""
    return bytes(payload[18:]).split(b"\0")[0].decode("utf-8", "replace").strip()
