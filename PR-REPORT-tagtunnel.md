# Gen-1 tunnel-backed tag read — full report

**Branch:** `feat/gen1-tag-tunnel` in the fork clone
(`/tmp/opencode/multiace-fork`, fork of `decay71/multiACE`), created from
the fork's `origin/main` (`ef63ee1`). **Independent** of the two open PR
branches (`feat/gen1-flasher-v1` #144, `feat/ace-set-humidity` #145) — both
were left untouched.

**Commits (no push, no PR — the controller does that):**

| commit | contents |
|---|---|
| `0187193` | `klipper/ace`: `ace_gen1_tunnel.py` + the ace.py wiring (`ACE_TAG_READ` Gen-1 branch, `ACE_SET_TAG_TUNNEL`, the identification fallback, own store, status surface) + `ace.cfg` template |
| `410c77d` | `tools + docs`: `tools/gen1_tag_tunnel_selfcheck.py` (102 checks) + `docs/GEN1_TAG_TUNNEL.md` |
| (this commit) | `PR-REPORT-tagtunnel.md` |

**Scope:** let multiACE identify third-party spools on the ACE Pro (Gen 1)
through the community firmware's RC522 tunnel, per the plan step
`filament_identify`. The tunnel itself is already live-verified; this
branch is the host side.

**What the tunnel contract says (integrated against):**
`REPORT-RC522-TUNNEL-EN.md` (Godless50/ACE-PRO-v1.-NFC-UID) — carrier
`filament_recognition`, packed index
`0x80000000 | reader<<24 | op<<16 | ((a1 & 0x3F) << 8) | (a2 & 0xFF)` sent
as the **signed 32-bit** value (firmware `strtol`), replies in
`result.code`, ops 0..8, `reader = slot` (0→spool1 … 3→spool4), acquire
(op 7) / release (op 8) around a session, host order
`TXMODE|=0x80 → RXMODE|=0x80 → BitFraming=0 → FIFO writes → TRANSCEIVE →
RX bits → FIFO reads`. The image reports `CV1.3.871`; stock is
`V1.3.863`, the earlier UID-only community build `CV1.3.863`.

## Deliverables, mapped to the task

1. **Tunnel client** — `multiace/klipper/extras/ace_gen1_tunnel.py`, a
   plain helper module (lazy import; deliberately not a Klipper extra, so
   no config section can halt an old config and a deploy-skew install gets
   one log line, never a dead Klipper):
   * **safe detection** — two gates: the runtime firmware string must be
     `CV1.3.87x`, and a probe op (op 0 read `VersionReg 0x37`, reader 0)
     must come back with a `result.code`. `parse_code` reads STRICTLY
     `result.code`; a stock reply (`{"result":{},"code":0,...}`) has none,
     so it can never pass. Stock and `CV1.3.863` send **zero** tunnel ops;
     a matching-but-silent firmware sends exactly one probe, logged once,
     then no more traffic for that firmware;
   * **read** — `read_slot()`: `acquire → SELECT(reader=slot) → READ page`
     (exact host order above) `→ release`; page 0 yields the 7-byte UID
     with both ISO14443-3 BCC bytes verified; an NTAG capability container
     triggers a bounded OpenSpool user-page read (pages 4..39); returns the
     raw bytes + UID + format (or `None`);
   * **normal state** — the release runs under `finally` on every path; a
     failed release is logged once (klippy.log) with the power-cycle note;
   * per-call timeout (default 3 s), reply poll 5 ms in a greenlet
     (`reactor.pause`, the same pattern `ace_rc522.py` uses).
2. **Gcode command** — `ACE_TAG_READ ACE=<n> SLOT=<0..3> [PAGE=<n>]` on a
   Gen 1 is the operator probe: prints the raw 16 bytes and the UID, plus
   the OpenSpool identity when decoded. It moves nothing (no rotation on a
   Gen 1) and is allowed during a print. The ACE 2 path is byte-for-byte
   unchanged (the command branches on protocol; the V2-only refusals still
   fire on a V2, covered by a check). The result also feeds the existing
   `tag_op` status contract.
3. **Identification fallback** — `_gen1_tunnel_status_tick()` runs from the
   heartbeat next to `_v1_tag_bind_from_status()`: on a Gen-1 with the
   feature enabled, an occupied slot whose tag the firmware did not
   identify (`rfid != 2` / empty SKU) **or** whose SKU matches no table
   entry gets **one opportunistic read per occupancy** (one session per
   unit at a time; subsequent slots on following heartbeats). The result
   is stored in multiACE's own per-unit dict (`_gen1_tunnel_reads`) —
   **never** `_info_per_ace` — and its card UID is offered to the SHARED
   `_spool_bind_by_tag(..., unbind=False)`: it binds when a spool already
   carries that UID in `sku`/`card_uids` and **never** releases a vendor
   binding. `get_status` surfaces the read: the slot's `uid`/`tag_format`
   are filled only when the device delivered none (device value always
   wins), plus an additive per-ACE `tag_tunnel` block
   (`enabled`/`available`/`reads`). The existing web plumbing already
   reads slot `uid`/`tag_format`, so a third-party UID reaches the picker
   with no backend change.
4. **Safe defaults** — `gen1_tag_tunnel` config flag, default **false**
   (`#gen1_tag_tunnel: false` template in `ace.cfg`), live setter
   `ACE_SET_TAG_TUNNEL ENABLE=0|1 [PERSIST=0|1]` (write-through, like the
   other toggles). With the flag off the only tunnel traffic is a command
   the user typed. `filament_recognition` was added to the send-stamp
   exclusion list in `send_request_to` (a page read is ~25 commands; it is
   a sensor read and must not flicker `busy`/`wait_ace_ready`). Note the
   fallback is inert on a unit without the tunnel — one `klippy.log` line,
   no console noise.
5. **Self-check** — `multiace/tools/gen1_tag_tunnel_selfcheck.py`,
   `python3 multiace/tools/gen1_tag_tunnel_selfcheck.py` →
   **all checks passed (102 `[ok]`, exit 0)**. It imports the real
   `ace.py` + `ace_gen1_tunnel.py` and drives them on fakes (no Klipper,
   no hardware) and proves:
   * (a) packing/signed conversion pinned against the tunnel notes' own
     vectors (`0x80003700 → -2147469568`, `0x80070000 → -2147024896`,
     reader-2 SELECT `0x82060000 → -2113536000`, …) and the exact op
     sequence of a read (acquire → SELECT → TXMODE/RXMODE/BitFraming →
     FIFO writes → TRANSCEIVE → RX bits → FIFO reads → release);
   * (b) reply parsing: only `result.code`, masked to 8 bits; a stock
     top-level `code` / `InvalidCommand` / non-numeric code → no tunnel;
   * (c) degradation: stock `V1.3.863` and UID-only `CV1.3.863` send ZERO
     commands; matching firmware + stock-style reply = exactly one probe,
     cached; a dead link times out bounded with exactly the sent ops;
   * (d) both genuine captures yield their bytes and UIDs (see below),
     a flipped UID byte fails the BCC check, and an OpenSpool NDEF decode
     produces material/colour/brand;
   * (e) the wiring: flag gating, one attempt per occupancy, own store,
     `_info_per_ace` never written, device value beats the tunnel copy,
     `get_status` surfacing, `ACE_SET_TAG_TUNNEL` persistence semantics,
     the Gen-1 command refusals, and the unchanged V2 refusal.
6. **Docs** — `multiace/docs/GEN1_TAG_TUNNEL.md`: what it does, the
   command, the flag, the limits (community firmware required; tag must
   face the coil; one reader per slot + the report's open pair-ambiguity
   item; read cost; mandatory release) and the maintainer decisions.

## The live captures (self-check vectors)

```
page 0: 04 22 52 FC 51 C8 2A 81 32 48 00 00 E1 10 6D 00   UID 04225251C82A81 (NTAG216)
page 0: 53 42 70 E9 D1 B5 00 01 65 48 00 00 E1 10 12 00   UID 534270D1B50001 (NTAG213)
```

**UID form (deliberate, flagging it):** the tunnel report's UID column
prints raw page bytes 0..6 — e.g. `04 22 52 FC 51 C8 2A`, which includes
BCC0 (`FC`) and omits UID6. The ISO14443-3 UID (page0[0:3] + page1[0:3],
BCC-verified) is `04225251C82A81`; that is what a phone reads, what
`ace_rc522._rc_read_uid` produces on V2, and what the bench unit's own
`card_uids` carry (`534270D1B50001` appears verbatim in the spools'
`sku` fields in the captured `ace_state.json`). The branch uses the ISO
UID so the existing `card_uids`/Spoolman binding works unchanged.

## Files changed

| File | Change |
|---|---|
| `multiace/klipper/extras/ace_gen1_tunnel.py` | **new** — tunnel client (packing, support gates, acquire/SELECT/read/release, BCC UID, OpenSpool decode) |
| `multiace/klipper/extras/ace.py` | client cache; `gen1_tag_tunnel` config + `ACE_SET_TAG_TUNNEL`; heartbeat fallback tick; own store + shared bind (unbind=False); `get_status` slot fill + `tag_tunnel` block; `ACE_TAG_READ` Gen-1 branch + report + tag_op result; `_drop_device_tag_reads` cleanup; send-stamp exclusion for `filament_recognition` |
| `multiace/config/extended/ace.cfg` | `#gen1_tag_tunnel: false` template + why |
| `multiace/tools/gen1_tag_tunnel_selfcheck.py` | **new** — 102 checks, no hardware |
| `multiace/docs/GEN1_TAG_TUNNEL.md` | **new** — review note + open decisions |
| `PR-REPORT-tagtunnel.md` | this report |

No changes to `multiace/web/` (the slot `uid`/`tag_format` and `tag_op`
plumbing already carries the surface), no i18n changes (plain strings, like
the other tag commands), no README noise (the clone is carrying a
pre-existing CRLF-only README modification from the other lane — **not
committed here**).

## What the controller should bench (HW session)

1. `ACE_EXT_RAW METHOD=get_info FORCE=1 ACE=0` → `"firmware":"CV1.3.871"`.
2. `ACE_TAG_READ ACE=0 SLOT=0` → expect the raw page bytes + UID; compare
   against the live captures above (slot→spool map: 0→1, 1→3, 2→2, 3→4).
3. `ACE_TAG_READ ... PAGE=4` for the NDEF dump on an OpenSpool tag.
4. Enable: `ACE_SET_TAG_TUNNEL ENABLE=1`; insert a third-party spool; watch
   `klippy.log` for `[spool] gen1 tunnel read ...`; check
   `printer/objects/query?ace` for the slot `uid`/`tag_format` and the
   `tag_tunnel` block; with a spool carrying that UID in Spoolman
   `card_uids`, expect the shared bind to fire (and the web picker to show
   the UID).
5. Regression: on a stock/UID-only unit, `ACE_SET_TAG_TUNNEL ENABLE=1`
   must produce one log line and no device traffic;
   `ACE_TAG_READ` must answer "no tag tunnel".
6. The release discipline: after a read, `get_status` must stay `ready`
   (never `busy`), and repeated `ACE_TAG_READ` must not leave the reader
   paused.

## Concerns / open points

* **Pair ambiguity on Gen 1** (from the tunnel report's open item 8:
  readers 0/2 and 1/3 answered with the same UID in one run). The branch
  does not add neighbour-attribution logic: a read on slot N is trusted as
  slot N's tag. If two slots ever answer with the same UID, the existing
  duplicate-binding guard refuses the second bind rather than moving the
  binding. A slot-pair disambiguation needs spool movement, which a Gen 1
  does not expose to the host — worth confirming on the bench which of the
  two explanations (shared RF path vs identical tags) is true.
* **One attempt per insert** is deliberate: the tag only answers facing
  the coil and the host cannot rotate a Gen-1 spool. A retry ladder or a
  read-on-rotation hook is a possible follow-up (doc open point 4).
* **Auto-bind by UID** (not just report) is the feature's value; it is
  guarded by `unbind=False` and the existing table guards. If the
  maintainer prefers report-only, it is one call site to delete
  (`_gen1_tunnel_store`).
* **Web UI**: the picker's Read button is hidden for non-`open_fw` units,
  so the command is console-first for now; `tag_op` and the slot fields
  are filled so enabling the button later needs no engine change.
* **Firmware acceptance window**: the version pre-gate accepts
  `CV1.3.87x`; the probe is authoritative for anything that passes it. A
  future tunnel build with a changed op contract needs the constant
  revisited (reference `CV1.3.871`).
* **No hardware from this lane** — all device interactions above are
  exercised only against fakes; the live verification is the controller's
  bench session.
