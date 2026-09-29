# ace_gen1_tunnel.py - Gen-1 (ACE Pro) tag reads through the community
# firmware's RC522 tunnel. Plain helper module, imported lazily by ace.py -
# deliberately NOT a Klipper extra: no [ace_gen1_tunnel] config section
# exists, so an old config cannot hit the unknown-section halt, and a
# printer whose install predates this file gets one clear log line and the
# ordinary status-only path instead of a dead Klipper.
#
# The tunnel (Godless50/ACE-PRO-v1.-NFC-UID, docs/GEN1_TAG_TUNNEL.md in
# this repo). The community firmware (CV1.3.87x) hooks the ACE's
# `filament_recognition` handler and dispatches host-issued operations from
# its packed `index` parameter:
#
#   packed = 0x80000000 | reader<<24 | op<<16 | (a1 & 0x3F)<<8 | (a2 & 0xFF)
#
# and it MUST go on the wire in the SIGNED 32-bit form (packed - 2**32):
# the firmware parses parameters with a signed strtol, so the unsigned
# value is clamped and the operation is silently lost. Replies arrive in
# `result.code` - and a `result.code` only exists when the stub ran, which
# is also the support probe.
#
# Ops: 0 read reg (a1) | 1 write reg (a1 = reg, a2 = value)
#      2 FIFO write (a2, a1 = index) | 3 PCD command (a1 = TX bytes,
#      a2 = command, 0x0C = TRANSCEIVE) | 4 FIFO read | 5 RX bits
#      6 REQA+anticollision+SELECT (0 = card ACTIVE) | 7 acquire (hold the
#      reader, returns the saved recognition state) | 8 release (a2 = the
#      saved state). Ops 7/8 wrap a whole read session: the hold is what
#      keeps the stock recognition task from resetting the chip between
#      the commands, and the release is what leaves the unit in its normal
#      state.
#
# Gen 1 has ONE reader/antenna channel per slot (owner-confirmed):
# reader = slot (0 -> spool 1, 1 -> spool 3, 2 -> spool 2, 3 -> spool 4).
# The tag must face the coil: there is no host-side motor control on a
# Gen-1 that could rotate a spool to look for it (feed/unwind are refused
# by the stock firmware), so a read is opportunistic by construction.
#
# Read sequence (tunnel-verified host order, inside ONE acquire/release):
#   acquire -> SELECT -> TXMODE |= 0x80 -> RXMODE |= 0x80 ->
#   BitFraming = 0 -> FIFO writes -> TRANSCEIVE -> RX bits -> FIFO reads
#   -> release.
# A full 16-byte reply is rx_bits == 0x80 (RxCRCEn strips the tag CRC); a
# page-0 read is pages 0..3, from which the 7-byte UID is taken with both
# ISO14443-3 BCC check bytes verified.
#
# Failure policy: every failure is "no tunnel" / "no tag" - never an error
# the user sees, never a blocking call outside the greenlet that runs it.
# Tunnel ops are ONLY ever sent when the firmware string matches the known
# tunnel builds (CV1.3.87x) AND a probe op answers through the stub; a
# stock (or older community) unit sees no tunnel traffic at all.

import json
import logging

# --- host contract (docs/GEN1_TAG_TUNNEL.md, REPORT-RC522-TUNNEL-EN.md) ----

TUNNEL_MAGIC = 0x80000000
# Firmware string prefix of the known tunnel builds. Stock ACE Pro reports
# V1.3.863, the UID-only community build CV1.3.863 - neither matches, so
# neither is ever sent a tunnel op.
GEN1_TUNNEL_FW_PREFIX = 'CV1.3.87'
# The verified tunnel reference (REPORT-RC522-TUNNEL-EN.md): CV1.3.871.
GEN1_TUNNEL_FW_REFERENCE = 'CV1.3.871'

# Ops.
OP_READ_REG = 0
OP_WRITE_REG = 1
OP_FIFO_WRITE = 2
OP_PCD_XCV = 3
OP_FIFO_READ = 4
OP_RX_BITS = 5
OP_SELECT = 6
OP_ACQUIRE = 7
OP_RELEASE = 8

# RC522 registers this client touches (the full map is in the tunnel docs).
REG_BITFRAMING = 0x0D
REG_TXMODE = 0x12
REG_RXMODE = 0x13
REG_VERSION = 0x37
PCD_TRANSCEIVE = 0x0C
VERSION_REG_VALUE = 0xA1     # MFRC522-family VersionReg answer

# Replies.
SELECT_CARD_ACTIVE = 0       # op 6 status when a card is in ACTIVE state
FULL_FRAME_BITS = 0x80       # op 5 for a full 16-byte reply
RELEASE_OK = 0               # op 8 result

# NTAG READ(0x30) of `page` returns 16 bytes = pages page..page+3.
NTAG_READ = 0x30
NTAG_CC_MAGIC = 0xE1         # capability container, page 3 byte 0
# The user pages an OpenSpool NDEF record lives in. 4..39 covers a NTAG213
# (0x12 * 8 = 144 user bytes) and the NDEF area of a NTAG216 alike; a
# bigger tag is truncated at the same boundary multiACE's V2 reader uses.
OPENSPOOL_FIRST_PAGE = 4
OPENSPOOL_LAST_PAGE = 39
# Anycubic slot-record magic (first two bytes of page 4 on their tags,
# mirrored here only for classification - this client never writes).
ANYCUBIC_MAGIC = b'\x7b\x00'

DEFAULT_TIMEOUT = 3.0        # seconds per tunnel command
POLL_INTERVAL = 0.005        # reply poll granularity (greenlet-yield safe)


def pack_index(op, a1=0, a2=0, reader=0):
    """The unsigned packed tunnel request (see the module docstring)."""
    return (TUNNEL_MAGIC | ((int(reader) & 0x3) << 24)
            | ((int(op) & 0xFF) << 16)
            | ((int(a1) & 0x3F) << 8) | (int(a2) & 0xFF))


def as_signed32(value):
    """The wire form the firmware actually parses (signed strtol)."""
    value &= 0xFFFFFFFF
    return value - 0x100000000 if value & TUNNEL_MAGIC else value


def parse_code(response):
    """The op's byte result from a JSON-RPC reply, or None.

    STRICTLY `result.code`: a stock reply carries `code` at the TOP level
    (`{"result":{},"code":0,"msg":"success"}`) and must never be mistaken
    for a tunnel answer - only the stub's own writer puts the byte inside
    `result`. Masked to 8 bits (the stub formats a byte)."""
    if not isinstance(response, dict):
        return None
    result = response.get('result')
    if not isinstance(result, dict) or 'code' not in result:
        return None
    try:
        return int(result['code']) & 0xFF
    except (TypeError, ValueError):
        return None


def firmware_supports_tunnel(firmware):
    """True when the runtime firmware string is a known tunnel build.

    This is only the cheap pre-gate that keeps tunnel traffic away from
    stock/older units; the authoritative gate is a probe op that actually
    answers through the stub (Gen1TagTunnel.tunnel_available)."""
    txt = (firmware or '').strip().upper()
    return txt.startswith(GEN1_TUNNEL_FW_PREFIX)


def uid_from_page0(data):
    """The 7-byte NTAG UID from a page-0 read (16 bytes), hex, or ''.

    Both ISO14443-3 BCC check bytes are verified: they are the tag's own
    checksum over exactly these bytes, so a mismatch is a corrupted read
    (field edge), never a UID. Same rule the V2 reader applies."""
    try:
        if len(data) < 9:
            return ''
        bcc0 = 0x88 ^ data[0] ^ data[1] ^ data[2]
        bcc1 = data[4] ^ data[5] ^ data[6] ^ data[7]
        if data[3] != bcc0 or data[8] != bcc1:
            return ''
        uid = bytes(data[0:3] + data[4:8])
        if not any(uid):
            return ''
        return uid.hex().upper()
    except (TypeError, IndexError):
        return ''


def classify_page0(data):
    """Coarse format label for a page-0 read: 'anycubic', 'ntag' or
    'unknown' (a card answered SELECT but the page read is not an
    NTAG/Anycubic layout - a MIFARE chip, or a corrupted read)."""
    try:
        if len(data) >= 2 and bytes(data[0:2]) == ANYCUBIC_MAGIC:
            return 'anycubic'
        if len(data) >= 16 and data[12] == NTAG_CC_MAGIC:
            return 'ntag'
    except (TypeError, IndexError):
        pass
    return 'unknown'


def decode_openspool(data):
    """Parse an OpenSpool NDEF tag (openspool.io) into an identity dict, or
    None. Mirrors ace_rc522.AceTagReader._openspool_decode (same byte
    format, verified against a real tag there): NDEF-message TLV (0x03) ->
    MIME record 'application/json' -> JSON with type / color_hex / brand.
    Robust: skips NULL and lock/memory TLVs, tolerates the 3-byte length
    form."""
    try:
        i, n = 0, len(data)
        while i < n:
            t = data[i]
            if t == 0x00:            # NULL padding
                i += 1
                continue
            if t == 0x03:            # NDEF message TLV
                break
            if t == 0xFE:            # terminator, no NDEF
                return None
            if t in (0x01, 0x02):    # lock / memory-control TLV
                i += 2 + data[i + 1]
                continue
            return None
        else:
            return None
        ln = data[i + 1]
        if ln == 0xFF:
            ln = (data[i + 2] << 8) | data[i + 3]
            p = i + 4
        else:
            p = i + 2
        msg = data[p:p + ln]
        hdr = msg[0]
        tl = msg[1]
        if hdr & 0x10:               # SR: 1-byte payload length
            pl = msg[2]
            off = 3
        else:
            pl = int.from_bytes(msg[3:7], 'big')
            off = 6
        typ = msg[off:off + tl]
        off += tl
        payload = msg[off:off + pl]
        if typ != b'application/json':
            return None
        j = json.loads(payload.decode('utf-8', 'replace'))
        if str(j.get('protocol', '')).lower() != 'openspool':
            return None
        return {
            'material': (j.get('type') or '').strip(),
            'color': (j.get('color_hex') or '').lstrip('#').upper()[:6],
            'brand': (j.get('brand') or '').strip(),
        }
    except (IndexError, ValueError, TypeError, UnicodeError):
        return None


class Gen1TagTunnel:
    """Drive one ACE Pro's RC522 tunnel through `filament_recognition`.

    Runs in a greenlet (reactor.pause is used for the reply poll);
    `send_request_to` is multiACE's ordinary V1 request path, so the
    heartbeat and other traffic interleave on the same serial port with no
    extra locking. One instance per ACE is cached by ace.py; the support
    probe result is cached per (idx, firmware string)."""

    METHOD = 'filament_recognition'

    def __init__(self, ace, idx=0, timeout=DEFAULT_TIMEOUT):
        self.ace = ace
        self.idx = int(idx)
        self.timeout = float(timeout)
        # (firmware string, supported bool) from the last probe - re-probed
        # when the firmware string changes (a re-flash in the same session).
        self._support = None
        self._no_tunnel_said = set()
        self._release_fail_said = False

    # -- plumbing ----------------------------------------------------------

    def _pause(self, seconds):
        self.ace.reactor.pause(seconds)

    def _req(self, packed, timeout=None):
        """One tunnel request: send + poll the reply box. Returns the raw
        JSON-RPC reply dict or None (timeout / no reply / send failed).
        The callback is invoked with KEYWORD arguments by the dispatcher
        (callback(self=<ace>, response=<ret>)); **kw tolerates both
        calling styles."""
        box = {}

        def _cb(*args, **kw):
            box['r'] = kw.get('response', args[-1] if args else None)

        try:
            self.ace.send_request_to(
                self.idx,
                {'method': self.METHOD,
                 'params': {'index': as_signed32(packed)}}, _cb)
        except Exception as e:
            logging.info('[multiACE] gen1 tag tunnel: send failed on ACE %d: '
                         '%s', self.idx, e)
            return None
        deadline = (self.ace.reactor.monotonic()
                    + (self.timeout if timeout is None else float(timeout)))
        while 'r' not in box and self.ace.reactor.monotonic() < deadline:
            self._pause(POLL_INTERVAL)
        return box.get('r')

    def _op(self, op, a1=0, a2=0, reader=0, timeout=None):
        """One packed op, result byte or None."""
        return parse_code(self._req(pack_index(op, a1, a2, reader),
                                    timeout=timeout))

    # -- support -----------------------------------------------------------

    def _fw_string(self):
        try:
            return (self.ace._ace_models.get(self.idx) or ('', ''))[1]
        except Exception:
            return ''

    def support_state(self):
        """(firmware, supported) from the last probe, or None - status
        surfacing only, never triggers traffic."""
        return self._support

    def _note_no_tunnel(self, fw, probed=False):
        key = (fw, bool(probed))
        if key in self._no_tunnel_said:
            return
        self._no_tunnel_said.add(key)
        if not firmware_supports_tunnel(fw):
            logging.info(
                '[multiACE] gen1 tag tunnel: ACE %d firmware %r has no '
                'RC522 tunnel (needs the community tunnel build %s) - the '
                'unit keeps its normal status-only tag path',
                self.idx, fw or '?', GEN1_TUNNEL_FW_PREFIX + 'x')
        else:
            logging.info(
                '[multiACE] gen1 tag tunnel: ACE %d firmware %r matches the '
                'tunnel builds but a probe op did not answer through the '
                'stub - no tunnel traffic from here',
                self.idx, fw or '?')

    def tunnel_available(self, timeout=None):
        """True only after a probe op answered through the stub.

        Gate 1 (cheap): the firmware string must match the known tunnel
        builds. Gate 2 (authoritative): op 0 read VersionReg 0x37 must come
        back with a `result.code` - a stock reply has no `result.code`, so
        a stock (or older community) unit can never pass. The result is
        cached per firmware string; a probe is one command."""
        fw = self._fw_string()
        if not firmware_supports_tunnel(fw):
            self._note_no_tunnel(fw)
            self._support = (fw, False)
            return False
        if self._support is not None and self._support[0] == fw:
            return bool(self._support[1])
        v = self._op(OP_READ_REG, REG_VERSION, reader=0, timeout=timeout)
        ok = v is not None
        self._support = (fw, ok)
        if not ok:
            self._note_no_tunnel(fw, probed=True)
        elif v != VERSION_REG_VALUE:
            # The stub answered (a code exists) but reader 0 does not read
            # 0xA1 - a wiring/chip problem on that channel, not a missing
            # tunnel. Say it once; the read will simply find no card.
            logging.info(
                '[multiACE] gen1 tag tunnel: ACE %d VersionReg on reader 0 '
                'reads 0x%02X (expected 0x%02X) - tunnel present, check '
                'that channel if reads fail', self.idx, v, VERSION_REG_VALUE)
        return ok

    # -- reader session ----------------------------------------------------

    def acquire(self):
        """op 7: hold the reader. Returns the saved recognition state byte,
        or None (no tunnel / lost reply - the caller must not proceed)."""
        return self._op(OP_ACQUIRE)

    def release(self, state):
        """op 8: restore the state acquire() returned. Mandatory: without
        it the unit stays paused and reports status=busy. Logs once when a
        release fails (the unit may need a power cycle - klippy.log must
        say why)."""
        ok = self._op(OP_RELEASE, a2=int(state) & 0xFF) == RELEASE_OK
        if not ok and not self._release_fail_said:
            self._release_fail_said = True
            logging.warning(
                '[multiACE] gen1 tag tunnel: ACE %d release (op 8, state '
                '%d) did not confirm - the stock recognition state was not '
                'restored; a power-cycle of the ACE clears it',
                self.idx, int(state) & 0xFF)
        return ok

    def select(self, reader):
        """op 6: bring a card to ACTIVE. True when a card is in the field
        (status 0) - works for ANY ISO14443A tag, unlike the firmware's own
        Anycubic-only identification."""
        return self._op(OP_SELECT, reader=reader) == SELECT_CARD_ACTIVE

    def read_page(self, page, reader=0, count=16, timeout=None):
        """One NTAG READ(0x30) of `page`; 16 data bytes or None.

        The exact tunnel host order: TXMODE |= 0x80 (the op-1 write to
        0x12 also runs the firmware's full reader bring-up), RXMODE |=
        0x80 (RxCRCEn verifies and strips the tag's 2 CRC bytes), then
        BitFraming = 0, the bare 30 <page> frame pushed byte by byte, the
        TRANSCEIVE, RX bits, FIFO reads. Any lost op returns None - a
        partial FIFO is not a page."""
        v = self._op(OP_READ_REG, REG_TXMODE, reader=reader, timeout=timeout)
        if v is None:
            return None
        self._op(OP_WRITE_REG, REG_TXMODE, v | 0x80, reader=reader,
                 timeout=timeout)
        v = self._op(OP_READ_REG, REG_RXMODE, reader=reader, timeout=timeout)
        if v is None:
            return None
        self._op(OP_WRITE_REG, REG_RXMODE, v | 0x80, reader=reader,
                 timeout=timeout)
        self._op(OP_WRITE_REG, REG_BITFRAMING, 0x00, reader=reader,
                 timeout=timeout)
        for i, b in enumerate((NTAG_READ, int(page) & 0xFF)):
            self._op(OP_FIFO_WRITE, i, b, reader=reader, timeout=timeout)
        self._op(OP_PCD_XCV, 2, PCD_TRANSCEIVE, reader=reader, timeout=timeout)
        bits = self._op(OP_RX_BITS, reader=reader, timeout=timeout)
        # A real page reply is 16 bytes = 0x80 rx bits; anything else is a
        # partial reply or NO reply (the FIFO then still holds the previous
        # transceive - never hand that to the decoder).
        if bits != FULL_FRAME_BITS:
            return None
        out = bytearray()
        for i in range(count):
            b = self._op(OP_FIFO_READ, i, reader=reader, timeout=timeout)
            if b is None:
                return None
            out.append(b)
        return bytes(out)

    def read_openspool(self, reader):
        """Read the NTAG user pages and decode an OpenSpool NDEF record.
        Returns the identity dict or None. One 16-byte READ per 4 pages;
        stops at the first failed chunk (the record, when present, sits at
        the start of the user area)."""
        data = bytearray()
        page = OPENSPOOL_FIRST_PAGE
        while page <= OPENSPOOL_LAST_PAGE:
            chunk = self.read_page(page, reader=reader)
            if chunk is None:
                break
            data += chunk
            page += 4
        return decode_openspool(bytes(data)) if data else None

    def read_slot(self, slot, page=0, userdata=True):
        """The full operator/auto read: acquire -> SELECT on the slot's
        antenna -> READ -> release. Returns a dict or None:

            {'slot', 'reader', 'page', 'data' (bytes), 'uid' (bare upper
             hex or ''), 'format' ('openspool' | 'anycubic' | 'ntag' |
             'unknown'), 'openspool' (dict or None), 'saved'}

        `userdata` also reads the OpenSpool user pages when page 0 shows a
        plain NTAG capability container. None = no card / no reply / no
        tunnel; the release runs on every path."""
        slot = int(slot) & 0x3
        page = int(page) & 0xFF
        saved = self.acquire()
        if saved is None:
            return None
        try:
            if not self.select(slot):
                return None
            data = self.read_page(page, reader=slot)
            if data is None:
                return None
            uid = uid_from_page0(data) if page == 0 else ''
            fmt = classify_page0(data)
            out = {'slot': slot, 'reader': slot, 'page': page,
                   'data': data, 'uid': uid, 'format': fmt,
                   'openspool': None, 'saved': saved}
            if (userdata and uid and page == 0
                    and len(data) >= 16 and data[12] == NTAG_CC_MAGIC):
                op = self.read_openspool(slot)
                if op:
                    out['openspool'] = op
                    out['format'] = 'openspool'
            return out
        finally:
            self.release(saved)

    # -- helpers for callers ----------------------------------------------

    @staticmethod
    def hex_dump(data):
        """'04 22 52 FC ...' for console/status lines."""
        try:
            return ' '.join('%02X' % b for b in bytes(data))
        except (TypeError, ValueError):
            return ''
