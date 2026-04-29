# scripts/

Operator scripts. Not part of the installed `tapo-cli` package.

## `smoke.py` — Phase 0 hardware-test harness

Per SRD §16.0, no Phase 1 verb ships until this script passes against every camera the operator owns. It probes seven mechanisms per camera and writes a structured report plus raw fixtures.

### One-time setup

1. **Provision a camera account** in the Tapo app for every camera you'll test:
   `Settings > Advanced settings > Camera account > Create account`. Use a username and password 6-32 characters each. Without this, RTSP streaming and ONVIF auth will both fail. **This is the most common Phase 0 failure cause — do it first.**

2. **Enable third-party compatibility** on firmware revisions where it's gated:
   `Settings > Tapo Lab > Third-Party Compatibility`. Some firmware exposes ONVIF only when this is on.

3. **Write the smoke config** at `~/.config/tapo-cli/smoke-cameras.json`:

   ```json
   [
     {
       "alias": "front-door",
       "ip": "192.168.1.42",
       "model": "C320WS",
       "username": "tapouser",
       "password": "tapopass",
       "onvif_port": 2020
     }
   ]
   ```

   `onvif_port` defaults to `2020`. Some firmware uses `8000` — try that if ONVIF probes fail with a connection-refused error.

4. **Set permissions** so the file isn't world-readable:

   ```bash
   chmod 0600 ~/.config/tapo-cli/smoke-cameras.json
   ```

5. **Verify ffmpeg is on `PATH`** (`ffmpeg -version`). The RTSP-frame mechanism shells out to it.

6. **Sync deps**:

   ```bash
   uv sync --all-extras --dev
   ```

### Run

```bash
uv run python scripts/smoke.py
```

Or with explicit paths:

```bash
uv run python scripts/smoke.py \
  --cameras ./my-cameras.json \
  --fixtures-dir ./tests/fixtures
```

JSON mode (one object per camera on stdout):

```bash
uv run python scripts/smoke.py --json
```

### What success looks like

Exit 0. Every camera shows `gate: PASS` in the text report. At least ONE of the snapshot mechanisms — `pytapo_native_snapshot`, `onvif_GetSnapshotUri`, `ffmpeg_rtsp_frame` — must show `[PASS]` per camera.

`tests/fixtures/raw/<alias>-onvif.jpg` and/or `tests/fixtures/raw/<alias>-ffmpeg.jpg` will exist. Open one — that's a real frame from your camera.

`tests/fixtures/smoke-report-<UTC-iso8601>.json` contains per-mechanism timing + status for the run. Keep it for the SRD §3.3.1 capability matrix update.

### What "skipped" means

`pytapo_native_snapshot` is marked `skipped` on current pytapo (no first-class single-frame API at the pinned SHA). This is expected. The snapshot gate is satisfied by the ONVIF tier or the ffmpeg-RTSP tier. If `pytapo_native_snapshot` ever shows `pass` in a future pytapo SHA, the SRD's three-mechanism fallback chain (FR-11) gets stronger.

### When a tier fails

The smoke gate only requires ≥1 snapshot mechanism per camera. If a tier fails:

- **`pytapo_getBasicInfo` fails** → camera-account credentials are wrong, the camera is unreachable, OR pytapo can't speak this firmware's variant (legacy POST-cookie / KLAP / encrypted login — see SRD §6.8). Verify the camera account in the Tapo app, then retry.

- **`pytapo_getStreamURL` fails** but `pytapo_getBasicInfo` passes → likely a model-specific pytapo gap. Document in §3.3.1.

- **All ONVIF probes fail with `connect failed`** → `onvif_port` is wrong or ONVIF is disabled on this firmware. Try `8000`. If still failing, the model goes in the §3.3.1 "ONVIF: no" cell and the snapshot gate must rest on `ffmpeg_rtsp_frame`.

- **`ffmpeg_rtsp_frame` fails with `ffmpeg not on PATH`** → install ffmpeg (`brew install ffmpeg` or distro equivalent).

- **All snapshot mechanisms fail for one camera** → exit 1. That model is **not** Phase 1-eligible. Update the SRD §3.3.1 matrix to move it to the "untested" row, OR investigate further before unblocking Phase 1.

### What to do after a successful run

1. Manually scrub `tests/fixtures/raw/*-getprofiles.xml` and `*-getsnapshoturi.xml` per the checklist in `tests/fixtures/README.md`.
2. Promote scrubbed copies to `tests/fixtures/scrubbed/` (Phase 1 will wire mocks against them).
3. Open a PR updating SRD §3.3.1 with any per-model deltas observed (ONVIF port differences, snapshot tier results, model-row additions).
4. Phase 1 starts.

### Constraints

- **No real network in unit tests.** `tests/test_smoke_dryrun.py` mocks pytapo, onvif-zeep-async, and `subprocess.run`. The harness itself is the only thing that touches the network — and only when run by the operator.
- **No credentials in stdout/stderr.** RTSP and snapshot URLs are masked (`scheme://***:***@host`) before printing or logging. Camera-account passwords are never echoed.
