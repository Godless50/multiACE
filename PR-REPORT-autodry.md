# External-humidity PR — full report

**Branch:** `feat/ace-set-humidity` in `Godless50/multiACE` (fork of
`decay71/multiACE`), based on the fork's default branch `origin/main`
(`ef63ee1`), **independent** of `feat/gen1-flasher-v1`.
**Scope:** implement the maintainer's three conditions from
decay71/multiACE#137 for an **external humidity source** on auto-dry:
own per-unit store with a TTL, a gcode push command, and an expiry stop
that holds for **V1 (ACE Pro) and V2 (ACE 2) alike**, with `get_status`
showing that the reading is external and how old it is.

No push, no PR: the controller reviews and pushes.

## Deliverables (mapped to the three conditions)

1. **Own store, never `_info_per_ace`** — `multiace/klipper/extras/ace.py`:
   `self._external_rh = {idx: {'rh', 'temp', 'ts', 'ttl'}}` (monotonic ts,
   memory-only) plus `self._external_rh_cycle = set()` (which of OUR cycles
   were started from a pushed reading; persisted as `ace__auto_dry_external`
   so a Klipper restart cannot lose it). `_ace_humidity()` now prefers a
   *fresh* external reading and falls back to the internal value
   (`_ace_internal_humidity()`) when it is stale/absent; `_ace_humidity_source()`
   reports `external`/`internal`/`None`. The 1 Hz heartbeat path is untouched.
2. **`ACE_SET_HUMIDITY ACE=<n> RH=<float> [TEMP=<float>] [TTL=<seconds>]`** —
   registered next to `ACE_SET_AUTO_DRY`, validates RH 0..100, TEMP 0..100 °C
   (plausible sensor range, informational), TTL > 0 (clamped to 60..7200 s,
   default 900 s, clamp named in the response), refuses an ACE index that does
   not exist, logs one line per accepted push, answers with a clear message.
   Idempotent and cheap: no reader traffic, no device command, no
   `_info_per_ace` write, works while the unit is reconnecting. Help text and
   `msg.ace_humidity_set` added to `en.json`/`de.json`.
3. **Expiry stop, same for both generations** — enforcement lives in the
   existing `_auto_dry_tick` (no new thread/timer): a cycle in
   `_external_rh_cycle` whose reading is no longer fresh is stopped through
   the project's own `_auto_dry_stop` path, with one reason line
   (`external humidity reading expired (age …s, TTL …s)` / `… gone (none
   since restart)`). The tick's hard "non-V2 → follower only" gate is gone:
   an ACE Pro with a pushed reading regulates itself (and can drive
   followers); the three `ACE_SET_AUTO_DRY` gates were relaxed for units with
   a reading (rh_* on a Pro with a reading, enable without a master when a
   reading exists, `MASTER` may point at a Pro that has readings). Followers
   started by a master are never marked external, and a master stopped by
   expiry still hands its followers their own add-time.

`get_status` (per ACE, additive): `external_humidity`,
`external_humidity_temp`, `external_humidity_age`, `external_humidity_ttl`,
`external_humidity_fresh`, `external_humidity_cycle`, `humidity_source`,
`humidity_effective`. `humidity` keeps its old meaning (the device's own
sensor). The web backend passes the new keys through.

## Files changed

| File | Change |
|---|---|
| `multiace/klipper/extras/ace.py` | feature (store, command, tick, status, config gates); `_gcode_num` helper shared with `ACE_SET_AUTO_DRY` |
| `multiace/i18n/en.json`, `de.json` | `msg.ace_humidity_set` (zh has no auto-dry keys upstream → English fallback) |
| `multiace/web/backend/main.py` | field-by-field status passthrough of the new keys |
| `multiace/tools/ace_set_humidity_selfcheck.py` | new test-in-a-script, 66 checks |
| `multiace/docs/ACE_SET_HUMIDITY.md` | new guide: usage, REST call, TTL semantics, open decisions |
| `PR-REPORT-autodry.md` | this report |

## Verification performed (evidence)

| check | result |
|---|---|
| `python3 -m py_compile` on `ace.py`, `main.py`, the self-check | OK |
| `python3 multiace/tools/ace_set_humidity_selfcheck.py` (imports the **real** `ace.py`) | **66/66 pass** |
| self-check coverage: parsing/validation/clamping (12 negative cases, NaN, clamp up/down, default), `_info_per_ace` byte-identical after pushes, fresh-over-internal, stale fallback + true age, V2 expiry stop, V1 expiry stop, refresh-extends, restart-stop, internal-only cycle not stopped, hand-started cycle untouched, expiry with auto-dry disabled, master→follower add-time on expiry, self-master single start, follower not self-starting, relaxed/existing config gates, `get_status` payload and `auto_dry_masters` | all pass |
| `json.load` on `en.json`/`de.json`; message renders (`ACE 0 external humidity: 42.5%rH, temp 23.5 C, valid for 1800s`) | OK |
| `git diff` review | `README.md` left untouched (pre-existing CRLF/EOL artifact in the worktree, not staged); no other unrelated changes |

**Not verified (needs hardware):** a real V1 + V2 session — push a reading,
watch the cycle start, kill the feeder, confirm the stop within ~1 minute;
also the ACE Pro's first real dry cycle driven by external readings.

## Design notes a reviewer may question

- **Expiry granularity.** The stop is checked once per `AUTO_DRY_INTERVAL`
  (60 s), so a cycle can overrun its TTL by up to one tick. No new timer was
  added (per the "hook into the existing loop" condition).
- **Internal fallback is re-evaluated.** After an external reading expires on
  an ACE 2, the expiry stop fires and the *same* tick may start a new cycle
  from the internal sensor if it is above `rh_start` — marked `internal`.
  That is the pre-existing ACE 2 rule doing its job; a Pro (no sensor) stays
  off until the feeder pushes again. If the maintainer wants a suppression
  window after an external expiry, that is a two-line addition — flagged.
- **Restart safety.** The reading store is deliberately not persisted (a
  monotonic timestamp that outlived a restart cannot be trusted); the origin
  set is, so a restored cycle started from a reading is stopped on the first
  tick instead of drying to the device backstop. The feeder can restart it.
- **Roles are explicit.** A follower configured with `MASTER=<other>` ignores
  its own pushed reading while it follows (the master's cycle owns it), and a
  self-regulating Pro may use `MASTER=-1` or `MASTER=<itself>` — the latter is
  the convention our local patch used, so existing configs keep working.
  `_auto_dry_followers()` now excludes the master itself (a self-mastered unit
  must not be started twice).
- **`_gcode_num` shared helper.** `ACE_SET_AUTO_DRY`'s hand-rolled numeric
  parsing moved into one method used by both commands; behaviour is identical
  except that NaN/inf are now rejected (they previously passed the min/max
  comparisons and would poison control).
- **Follower handover on every stop path.** Extracted
  `_auto_dry_followers_done()` so a master stopped by expiry gives its
  followers their `add_time` exactly like a below-`rh_end` stop (this was a
  real bug caught by the self-check).
- **API version not bumped.** `get_status` changes are additive only.

## Open items / concerns for the maintainer

1. **Default TTL / clamp range** — implemented as 900 s default,
   60..7200 s clamp. Our feeder sends 1800 s from its config. The 60 s floor
   is tied to `AUTO_DRY_INTERVAL`; the ceiling is a policy choice.
2. **UI wording/placement** — the status keys are there, but no frontend
   change was made (the maintainer asked for the status, not the UI). Decide
   whether the humidity badge should show the *effective* (external) value,
   and the label for `humidity_source` / reading age.
3. **`ACE_SET_AUTO_DRY` role semantics** — a Pro with a reading now gets the
   threshold message instead of the follower line, and `auto_dry_masters`
   offers other units with readings. Whether the UI should offer "no master"
   explicitly is a UI decision.
4. **ACE 2 vs a stale external reading** — per condition 3 the external-driven
   cycle stops on expiry even though the internal sensor is still valid, then
   the internal rule may immediately start a new (internal) cycle. If that is
   not the wanted behaviour, say so and we add a suppression window.
5. **Hardware test** — as above; the branch has never driven a real dryer.
6. **Translations** — `zh.json` has no auto-dry block upstream; the new
   message falls back to English there.
