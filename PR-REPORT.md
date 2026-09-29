# Gen-1 flasher PR — full report

**Branch:** `feat/gen1-flasher-v1` in `Godless50/multiACE` (fork of `decay71/multiACE`)
**Scope:** implement the maintainer's request in decay71/multiACE#137 —
a **V1 (ACE Pro) branch in the firmware tab**, `ACE_FW_RELEASE` /
`ACE_FW_RESUME` **opened for V1 instead of new commands**, a **tested-images
list with a source line for the OpenCubic image**, rollback by re-flashing
stock, GPL-3.0, and a `docs/` note for review.

No push, no PR: the controller reviews and pushes.

## Deliverables (as requested)

1. **Gen-1 transport + flasher** — `multiace/web/backend/ace1_flash.py`
   (new): `FF AA | len(u16 LE) | payload | crc16(MCRF4XX) | FE`, JSON-RPC
   (`get_info`, `get_status`, `iap_version`,
   `iap_upgrade{size,crc,version}`, `iap_upgrade_finish`) and
   `0x55 | addr(u32 LE) | n(u8) | data[n]` chunks from `0x08024000`,
   64 B, pace-limited. Completely separate from `ace2_ota.py`'s protobuf
   cmd 2/3/4 transport; no shared code, one shared idea (byte-exact
   tested-images gate).
2. **Wired into the same firmware tab** — `main.py` routes
   `/api/acefw/flash` by the **protocol Klipper reports** (`v1` → ace1_flash,
   `v2` → ace2_ota untouched); `/api/acefw/versions` returns a second
   list `gen1_versions`; the frontend (`app.js`, `index.html`) lists both
   generations, switches the allowlist on selection and hides the
   Gen-2-only fields (`.swu` password, ACE2-Open patch, force). Flow:
   port release → flash with progress → `iap_upgrade_finish` → wait for
   the unit to answer → `ACE_FW_RESUME`. Rollback = pick the stock entry
   and flash it through the same card.
3. **`ACE_FW_RELEASE` / `ACE_FW_RESUME` opened for V1** — `ace.py`: the
   release gate now accepts both protocols (new `_is_v1` helper); the
   command itself is unchanged for V2, and no new gcode command was added.
   `ACE_FW_RESUME` was already generation-agnostic.
4. **Tested-images list (Gen 1)** — `ace1_flash.KNOWN_FIRMWARE`, keyed by
   image id (a Gen-1 version string does not identify an image: stock and
   the OpenCubic CFW both report 1.3.863). Entries:
   - `1.3.863-opencubic` — source line: *OpenCubic ACE 1 Pro CFW v1.1.1
     (2026-08-17) release asset `ACE_V1.3.863_20260716.bin`, md5
     9f7b9a678a96caf98d6a08842d3ff971* — 113720 B, CRC `0xC110`.
   - `1.3.863-stock` — clean stock `ACE_V1.3.863_20250518.bin` (rollback
     target), md5 `dcd04589dcadd5b4feab66d33e772531` — 105652 B,
     CRC `0xDEFB`.
   No licence claim about third-party images anywhere: only the source
   line, the hashes and "multiACE ships none of these".
5. **Tests** — the repo has **no test layout** (no `tests/`, no test job
   in CI; only a release-tarball workflow), so as agreed the branch adds
   `multiace/tools/gen1_flasher_selfcheck.py` instead: protocol check
   vectors plus a **full simulated flash** over an in-process fake
   transport (no hardware, no pyserial), and an optional
   `--image/--entry` mode that verifies a real image against the shipped
   entry.
6. **Docs** — `multiace/docs/GEN1_FLASH.md`: what the PR changes, why the
   transport is a separate branch, the flow, the trap list, the tested
   list and how to extend it, plus the bench open items.

## Verification performed (evidence)

| check | result |
|---|---|
| `python3 -m py_compile` on `ace1_flash.py`, `main.py`, `ace.py`, self-check | OK |
| `tools/gen1_flasher_selfcheck.py` (17 checks: CRC `0x6F91`, frame layout, chunk layout, gate accept/reject, simulated flash order/addresses/reboot, refused image never reaches the wire) | all pass |
| self-check `--image` against both real assets (reporter's OpenCubic asset + stock) | byte-exact match to the entries (md5/size/CRC) |
| route-level test (FastAPI imported in a venv, endpoints called directly with mocked Klipper state) | `gen1_versions` served; V1 route → `gen1=True`, patch flag cleared, version preserved; V2 route `gen1=False` unchanged; unknown target → 400 before port release; V1 dry run needs no version; gen1 `_acefw_run` calls only `ace1_flash` and resumes the port |
| `node --check` on `app.js`; JSON validity for `en/de/zh`; Vue template compile of the changed card with `@vue/compiler-dom` | OK (the changelog card compiles; the rest of the template has a pre-existing unquoted-attribute that the runtime build tolerates — untouched) |
| `git diff` review | `README.md` left untouched (its worktree has a pre-existing CRLF/EOL artifact); no other unrelated changes |

Not verified (needs hardware): the wire protocol against a live ACE Pro,
the Klipper-side gate change at runtime, and the `get_status` liveness
probe — see open items.

## Design notes a reviewer may question

- **Routing by protocol, not by a client flag.** The web sends the same
  payload for both generations; `acefw_flash` reads the protocol from the
  Klipper state (`_parse_state` exposes `protocol`) and picks the engine.
  A stray `patch_to_open` on a V1 target is ignored and the client's own
  version pick is restored before the Gen-1 flasher sees it (this was a
  real bug found by the route test and fixed).
- **No same-version skip on Gen 1.** `force` is accepted for signature
  parity and unused: Gen-1 version strings do not identify an image, so
  skipping on "already reports 1.3.863" could skip a CFW flash over stock.
  Every confirmed Gen-1 flash writes; the UI hides the Force checkbox.
- **Port-open retries.** The bench flasher retries the open (the port can
  still be closing after the release), so `Gen1Transport` retries
  20 × 0.5 s instead of failing a flash on a transient `EBUSY`.
- **Keepalive.** The chunk stream is the keepalive during the write;
  `prime()` and the post-commit poll keep traffic on the link. No long
  sleeps were introduced.
- **Dry run parity.** V1 dry runs test the port + current version with the
  image optional, exactly like the V2 dry run.
- **i18n.** New strings added to `en.json` and `de.json`; `zh.json` has no
  `ui.config.acefw_*` block upstream at all, so it keeps falling back to
  English (the backend merges en over every catalog).

## Open items / concerns for the maintainer

1. **Hardware test required.** The entries carry `"tested": ""` on purpose:
   the branch adds the images with their source lines, but nothing has
   flashed them through this transport on multiACE hardware yet. Suggested
   bench order: (a) dry run a V1 (port open/version read), (b) flash the
   `1.3.863-stock` entry, (c) flash `1.3.863-opencubic`, (d) rollback to
   stock again, and only then fill the `tested` notes.
2. **Announced version.** Every entry announces `1.3.863` (the base the
   images are built on; the reporter's own flasher hard-codes exactly
   that). Whether the bootloader validates the announced version against
   the running one is untested — the reporter's unit was already on
   1.3.863, so the 856 → 863 transition was never exercised. The
   maintainer's two units are the right rig for that; if it turns out the
   announce must match the running version, it becomes a per-entry field,
   not new code.
3. **`get_status` as the application probe.** For Gen 1 the app/bootloader
   split is not researched the way the ACE 2's is; `get_status` answering
   is reported as `app_alive: true`, no answer as unknown. A unit that
   answers `get_info` but not `get_status` shows the "mute" warning — the
   wording is inherited from the V2 path and may deserve a Gen-1 phrase
   after a hardware datapoint.
4. **Klipper-side change is compile-checked, not runtime-tested.**
   `ace.py` cannot be imported off-printer. The change is small (the V1
   refusal replaced by a "protocol detected?" gate + `_is_v1`), but the
   first on-printer run of `ACE_FW_RELEASE ACE=<v1>` is still the proof.
5. **`README.md` feature bullet** still says "ACE 2 firmware updates" only.
   I left the root README untouched (its worktree carries a pre-existing
   EOL artifact in this clone); the maintainer may want to extend that
   bullet in his own tree.
6. **Repo policy on third-party images.** The PR adds md5/CRC/size of two
   third-party binaries but ships none and links none — consistent with
   `ace2_ota`'s existing policy. If the maintainer prefers to also carry
   the OpenCubic release URL in the `source` line, that is a one-line
   change in `KNOWN_FIRMWARE`.

## Files changed

```
multiace/web/backend/ace1_flash.py        new  Gen-1 IAP transport + gate
multiace/web/backend/main.py              route by protocol, gen1_versions
multiace/klipper/extras/ace.py            ACE_FW_RELEASE gate -> V1 + V2
multiace/web/frontend/app.js              candidate/allowlist logic
multiace/web/frontend/index.html          card: V1 fields, labels
multiace/i18n/en.json, de.json            new strings
multiace/docs/GEN1_FLASH.md               new  review note + traps
multiace/tools/gen1_flasher_selfcheck.py  new  test-in-a-script
```
