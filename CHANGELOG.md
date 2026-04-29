# Changelog

All notable changes to `tapo-cli`.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.1] — 2026-04-29

Phase 4a per SRD §16.4.1 — fan-out generalization (FR-43d, FR-56/56a/56b), `set` verb retro-fix (FR-39c, slipped from Phase 2), and group-level `reboot` confirmation (FR-43e/f).

### Added

- `tapo-cli set <target> [--image-flip on|off] [--timezone <IANA>]` (FR-39, FR-39a, FR-39c) — Phase 2 acceptance item that slipped past v0.2.0; v1.2.0 audit (A-3) catalogued the gap and Phase 4a ships the retro-fix. `--image-flip` drives pytapo `setImageFlipVertical(bool)` (the on-device 180° image rotation, NOT fisheye correction). `--timezone` drives `setTimezone(timezone, zoneID, timingMode="ntp")`. Pytapo at the pinned SHA (`de5ca37`) does not expose an IANA→zone_id lookup; the wrapper passes the IANA value as both fields, which current C-series firmware accepts. Bare `set <target>` exits 64 — at least one flag is required. Other knobs (HDR, noise cancelling, auto-track, recording-to-SD enable) remain deferred to v0.4+ per FR-39b. Output: `{target, changes: {<flag>: <value>}}`.
- Group fan-out (FR-43d, FR-56/56a/56b) generalized across every state-control verb listed in FR-43d's enumeration: `info`, `privacy`, `led`, `night-vision`, `motion {enable|disable|status|history}`, `alarm {enable|disable|trigger|status}`, `audio {volume|mic|speaker|tts}`, `osd {set|clear|status}`, `preset {list|goto|save|delete}`, `reboot`, `snapshot`, and the new `set` verb. Each verb's existing single-target work-function is now wrapped by an `is_group_target` check that dispatches through `_fanout.run_fanout` with the standard B10 envelope (`{target, status, exit_code, result?, error?}`). Per-target results emit one JSONL line in resolved-alias-list order (B9 deterministic). Exit codes follow FR-43a: `0` if all pass, `7` if mixed, the first member's exit code if all fail.
- `snapshot @group --output <path>` requires a `{target}` placeholder in the path (e.g. `--output /tmp/snap-{target}.jpg`) — silently clobbering N JPEGs into one file is the worst possible behavior. Without the placeholder → exit 64 with an actionable hint. `snapshot @group --output -` continues to exit 64 (binary stdout × N cameras = mess).
- `reboot @group` group-level confirmation (FR-43e, FR-43f). One stderr prompt enumerating the resolved member aliases — NOT one prompt per camera. `--yes` and `--quiet` short-circuit the prompt. Non-tty without `--yes` exits 64. Per-camera fan-out then proceeds with no further prompts.
- Test suite: new tests cover fan-out integration across every migrated verb (`tests/test_fanout_integration.py`), the `set` verb's flag combinations (`tests/test_set_cmd.py`), and the `reboot @group` confirmation matrix (`tests/test_reboot_group_confirm.py`). Existing per-verb test files gain `@group` regression cases.

### Changed

- `stream` and `record` continue to reject `@group` targets with exit 64 (FR-43c carve-out unchanged) — recording or URL-emitting against multiple cameras simultaneously is a footgun.
- `cli.py` registers the new `set` verb. Top-level `--help` now lists it.
- Bumped version `0.3.0` → `0.3.1` in both `pyproject.toml` and `src/tapo_cli/__init__.py`.

### Notes

- pytapo at pin `de5ca37` does NOT expose any device-rename API (`setName` / `changeName` / `setAlias` / `setDeviceName` are all absent). A `set --name` flag was outside SRD §16.4.1 scope and is not included; rename a device by editing the local `[devices.<alias>]` block in `~/.config/tapo-cli/config.toml`.

## [0.3.0] — 2026-04-29

Phase 3 — `record`, `motion history`, `groups list`, and `batch` verbs land on top of every prior phase. This is the v1 feature-complete release: every in-scope SRD requirement now ships.

### Added

- `tapo-cli record <target> --output PATH` (FR-13..13g, S3) — one-shot RTSP-to-MP4 recording via an `ffmpeg` foreground child. Footgun guard (FR-13a): non-tty mode requires `--duration <seconds>` or `--max-bytes <N>`; tty mode without a cap prompts on stderr ("Record indefinitely until Ctrl-C? [y/N]") and aborts on `N` / empty. `--duration` → `ffmpeg -t`, `--max-bytes` → `ffmpeg -fs`. SIGINT/SIGTERM forwards to ffmpeg with 2 s grace for MP4-atom finalization (FR-13b/g, S3 perf budget); SIGKILL after 2 s. ffmpeg-missing-on-PATH exits 6 (FR-13c). Group-target `@group` → exit 64 parity with `stream` (FR-43c). Camera-account-only path (FR-CRED-7); missing → exit 2. Output JSON: `{target, output_path, duration_seconds, bytes, exit_reason}` where `exit_reason` is one of `complete | sigint | sigterm | max-bytes | max-duration | ffmpeg-error`.
- `tapo-cli motion history <target> [--since <RFC3339>] [--limit N] [--event-type ...]` (FR-25..25d, B8) — emits per-event JSONL by default with `{target, ts, event_type, region, has_clip}`. `ts` is RFC 3339 UTC `Z` (FR-25a). `--since` accepts RFC 3339 with offset, RFC 3339 without offset (assumes UTC + INFO log on stderr per FR-25b), bare `YYYY-MM-DD` dates (treated as `T00:00:00Z`), and relative shorthand (`1h`, `30m`, `7d`, `60s`). Default window is the last 24 h. Results are sorted ascending by `ts` (FR-25c). Future `--since` exits 0 with empty output (FR-25d). `--event-type` filter accepts `motion | person | vehicle | doorbell-press | unknown`. `--limit` (default 50) truncates AFTER the sort. Backed by pytapo's `getEvents()` with the camera-clock-corrected epoch timestamps.
- `tapo-cli groups list` (FR-39..43, FR-43b) — read-only enumeration of every group defined in `[groups]` with member aliases and resolved IPs. Output is one record per group; `{name, members: [{alias, ip}, ...]}`. Empty / missing `[groups]` table → exit 0 with empty array. Per FR-43b, group mutations remain by hand-editing the config in v1 — same posture as `kasa-cli`.
- `tapo-cli batch [--stdin | --file PATH]` (FR-44..45c, B10) — reads newline-delimited sub-commands and executes them sequentially, emitting one JSONL line per result on stdout. Per-line shape conforms to B10: `{command, target, status, exit_code, result?, error?}`. `result` is the verb's normal `--json` payload on success; `error` is the structured §11.2 envelope minus the wrapping `exit_code` on failure. Comments (`#`) and blank lines are skipped (FR-45b). Empty input → exit 0 silently. Exit codes per FR-43a / FR-45a: `0` if all pass, `7` (partial-failure) if mixed, the first sub-op's exit code if all fail (B9 deterministic ordering — input-file order, NOT completion order). `--stdin` and `--file` are mutually exclusive (exit 64). Each sub-call is dispatched in-process through the top-level Click group with `--json` injected, so `result` parses cleanly.
- Group-target `@group` fan-out groundwork — every camera-control verb correctly accepts `@alias` as a single-alias target (the leading `@` is stripped). The verbs that explicitly reject groups (`stream` / `record` per FR-43c) continue to do so.
- Test suite: 44 new tests (was 325 in Phase 2 → 369 in Phase 3). New: `tests/test_record_cmd.py` (10 tests covering footgun guard, ffmpeg argv shape, SIGINT/SIGTERM forwarding, group rejection, missing-ffmpeg, missing-camera-account), `tests/test_motion_history_cmd.py` (12 tests covering `--since` parsing variants, future-`--since` empty-array path, sort determinism, event-type classification, limit truncation), `tests/test_groups_cmd.py` (5 tests covering text / json / jsonl / quiet modes plus empty `[groups]`), `tests/test_batch_cmd.py` (12 tests covering input shapes, B10 per-line schema, FR-43a / B9 exit-code semantics), `tests/test_signals.py` (6 tests pinning runner SIGINT→130 / SIGTERM→143 mapping). Mock-only — no real network. The `test_motion_history_exits_5` block from Phase 1d is replaced with a "history runs" assertion since Phase 3 implements it.

### Changed

- `motion` is now a custom Click `Command` instead of a leaf command — the parser dispatches between the legacy flat positional form (`motion <target> enable|disable|status|history`) and the new sub-verb form (`motion history <target> [--since ... --limit ... --event-type ...]`) based on the first argv token. Both forms are honored.
- `cli.py` registers three new verbs: `record_cmd`, `groups_cmd`, `batch_cmd`. `motion_cmd` stays a leaf at the top level.
- Bumped version `0.2.0` → `0.3.0` in both `pyproject.toml` and `src/tapo_cli/__init__.py`. v0.3.0 is the v1 feature-complete release.

## [0.1.2] — 2026-04-29

Phase 1c — `snapshot` and `stream` verbs land on top of the Phase 1a/1b foundation.

### Added

- `tapo-cli snapshot <target> --output <path>` (FR-11..11d, B5) — three-mechanism fallback chain: pytapo native → ONVIF `GetSnapshotUri` → ffmpeg single-frame from RTSP. Tier-advance condition (FR-11a.1) honored: per-tier budget timeout, non-200 / non-JPEG (`FF D8 FF` magic-byte sniff), and any non-auth exception each advance to the next tier. Auth-rejection (HTTP 401, pytapo `_AUTH_FAILED`, RTSP 401) at any tier short-circuits the chain to exit 2 immediately (FR-11a.2). Total `--timeout` is split 40% pytapo / 30% ONVIF / 30% ffmpeg by default; override via `--snapshot-budget pytapo=N,onvif=N,ffmpeg=N`. Sum-exceeds-`--timeout` exits 64 (FR-11a.3). Tier-3 ffmpeg-missing-on-PATH exits 6 (config error, FR-11a.4) — not 1 (device error). `--output -` writes JPEG bytes on stdout, incompatible with `--json` / `--jsonl` (exit 64, FR-11d). `--quiet` is permitted with `--output -` (S15 carve-out — JPEG bytes ARE the stdout payload). JSON output reports `mechanism`, `bytes`, `width`, `height`, `elapsed_ms`, `target` per FR-11b.
- `tapo-cli stream <target>` (FR-12..12g, B6, S2) — emits an `rtsp://user:pass@ip:554/streamN` URL on stdout (Unix philosophy; user pipes to ffmpeg). Stream-path resolution honors lens-by-quality truth table per B6 (`(wide,hd)→stream1`, `(wide,sd)→stream2`, `(telephoto,hd)→stream6`, `(telephoto,sd)→stream7`), overridable via `--protocol streamN` or `--profile <name>`. `--list-profiles` (FR-12b.2) emits the ONVIF `GetProfiles` response as a JSON array; ONVIF unavailable → exit 5. Camera-account-only path (FR-CRED-7): no `camera_account_file` for target → exit 2 with Tapo-app-menu hint. Group-target rejection (FR-49 / FR-43c) → exit 64.
- Credential-leakage hardening for `stream` (FR-12f, FR-12g, S2): `--credentials-via-env` redacts the URL printed on stdout (`rtsp://<user>:<pass>@host:port/path`) and exports `RTSP_USER` / `RTSP_PASS` / `RTSP_URL` to an exec'd child. `--exec <argv...>` replaces this process via `execvp` and substitutes the URL into `{}` placeholders or appends as the last arg; combined with `--credentials-via-env` the child sees only the redacted URL on argv with full creds via env — credentials never appear in shell history or process-list snapshots.
- Reusable media helpers in `src/tapo_cli/media.py`: `build_rtsp_url` (with `urllib.parse.quote(safe='')` percent-encoding for special-char passwords), `mask_url_credentials` (logs/stderr), `redact_userinfo` (`<user>:<pass>` placeholders for hardened mode), `resolve_onvif_wsdl_dir` (the `site-packages/onvif/wsdl/` lookup that works around onvif-zeep-async 4.0.4's shallow `_WSDL_PATH` default — Phase 0 BUG 2 lifted out of `scripts/smoke.py`).
- Pure-Python JPEG-dimension parser in `snapshot_cmd._jpeg_dimensions` — walks SOFn markers and reads height/width without a Pillow dependency.
- Test suite: 43 new tests (was 178 in Phase 1b → 221 in Phase 1c). New: `tests/test_snapshot_cmd.py` (23 tests covering budget parsing, JPEG validation, tier-advance, auth short-circuit, ffmpeg-missing → exit 6, --output - mutex, JSON schema, group rejection), `tests/test_stream_cmd.py` (20 tests covering URL construction, special-char password quoting, lens/quality matrix, ONVIF profile fetch, --list-profiles, --credentials-via-env redaction, --exec child substitution with and without creds-via-env, FR-CRED-7 missing-account, group rejection). Mock-only — no real network.
- `cli.py` Click context now exposes `json_flag` / `jsonl_flag` / `quiet_flag` so verbs that need to distinguish "explicit `--json`/`--jsonl`" from "auto-JSONL on a pipe" can do so without re-parsing argv. The stream verb requires this distinction because its FR-12 contract is a bare `rtsp://...` line on stdout regardless of tty state.

### Changed

- Bumped version `0.1.1` → `0.1.2` in both `pyproject.toml` and `src/tapo_cli/__init__.py`.
- `tests/test_cli.py` — `test_top_level_help_lists_phase_1b_verbs` renamed to `test_top_level_help_lists_phase_1c_verbs`. `snapshot` and `stream` moved from the embargoed list to the required list. `record`, `ptz`, `preset`, `motion`, `alarm`, `led`, `privacy`, `night-vision`, `audio`, `osd`, `reboot`, `batch` remain embargoed (Phase 1d / Phase 2+).

## [0.1.0] — 2026-04-28

Phase 1a (Foundation) — first release with executable CLI surface. No camera verbs yet; those land in Phase 1b/1c/1d.

### Added

- `tapo_cli` package: `__init__`, `__main__`, `cli`, `errors`, `types`, `config`, `credentials`, `auth_cache`, `output`, `wrapper`, `verbs/auth_cmd`, `verbs/config_cmd`.
- `tapo-cli --help` / `--version` / global flags: `--json`, `--jsonl`, `--quiet`, `--timeout`, `--config`, `--concurrency`, `--credential-source`, `-v`/`-vv`.
- `tapo-cli auth status` (FR-CRED-14) — emits per-cache rows with alias, MAC, cache_path, mtime (RFC 3339 UTC), bytes_size, expires_at, pytapo_version, cloud_account, camera_account.
- `tapo-cli auth flush [--target ALIAS|MAC]` (FR-CRED-12) — removes all or one cached pytapo session state file.
- `tapo-cli auth migrate` (FR-CRED-15a) — version-stamps the tapo-only credentials file at `~/.config/tapo-cli/credentials`. Refuses to touch `~/.config/kasa-cli/credentials` per FR-CRED-3.1.
- `tapo-cli config show` (FR-54a) — canonical TOML render of the resolved config.
- `tapo-cli config validate [<path>]` (FR-54c) — schema lint, exit 0 / 6.
- TOML config loader (SRD §9): `[defaults]`, `[credentials]`, `[ffmpeg]`, `[logging]`, `[devices.<alias>]`, `[groups]`. Strict mode for `--config` and `TAPO_CLI_CONFIG`.
- Credential resolver (SRD §6.1-6.7) covering camera-account-first chain, cloud-account fallback, partial-env-fall-through with one-shot WARN, FR-CRED-3.1 kasa-cli sharing with tapo-only override priority, chmod 0600 enforcement, symlink refusal, length validation for camera accounts.
- pytapo session cache (SRD §6.5, FR-CRED-9..13): atomic-rename writes (`tmpfile + fsync + rename`), per-MAC `flock` with `--timeout` (lock timeout exits 3), `pytapo_version` invalidation, credential-source mismatch invalidation, opaque state blob, RFC 3339 expires_at metadata, holder-PID best-effort on Linux.
- Output formatter (SRD §5.17, §7.2): auto-mode JSONL on non-tty (FR-46), `--quiet` carve-out for binary stdout (S15), RFC 3339 UTC timestamps, deterministic multi-record sort (target in config order, ties by event timestamp).
- Structured error envelope (SRD §11.2): `{error, exit_code, message, target?, hint?, mechanism?, credential?, details?}` with closed-enum error names.
- Exit-code constants for all 0/1/2/3/4/5/6/7/64/130/143 paths (SRD §11.1).
- Test suite: 134 tests (was 28 in Phase 0). New: `test_errors.py`, `test_config.py`, `test_credentials.py`, `test_auth_cache.py`, `test_output.py`, `test_cli.py`. Mock-only — no real network.

### Fixed

- `scripts/smoke.py` asyncio event-loop conflict: pytapo at the pinned SHA is synchronous but its internal `AsyncHandler` invokes `loop.run_until_complete()` on a fresh loop, which collided with the harness's outer `asyncio.run` (`RuntimeError: Cannot run the event loop while another loop is running`). The three pytapo probes now run via `asyncio.to_thread` so pytapo's loop is isolated to a worker thread.
- `scripts/smoke.py` onvif-zeep-async WSDL discovery under Python 3.14: the upstream `_WSDL_PATH` default in `onvif/client.py` resolves to `site-packages/wsdl` — one directory too shallow, since the bundle actually lives at `site-packages/onvif/wsdl/`. Smoke now resolves the correct path at runtime via `Path(onvif.__file__).parent / "wsdl"` and passes it explicitly via `ONVIFCamera(..., wsdl_dir=...)`. Also fixed the device-info call to use the documented `await create_devicemgmt_service()` accessor.
- `scripts/smoke.py` RTSP URL construction: tier 7 (ffmpeg) no longer depends on `pytapo.getStreamURL()` output, which on the pinned SHA returns a bare peer `host:port` rather than a full URL. Tier 7 now builds its own RTSP URL via `build_rtsp_url()` from camera config, percent-encoding usernames and passwords with `urllib.parse.quote(safe='')` so reserved characters (`@`, `:`, `/`, `!`, `?`, `#`, `&`) don't corrupt the URL. Tier 2 (`pytapo_getStreamURL`) reports pytapo's return informationally only. Added `-rtsp_transport tcp` and `-update 1` to the ffmpeg invocation for ffmpeg 8.x compatibility and deterministic single-frame capture.

### Added

- Initial repository scaffold: `pyproject.toml`, `README.md`, `.gitignore`, CI workflow stub.
- SRD v1.1.1 (`docs/SRD-tapo-cli.md`) — 1197 lines, 142 atomic FRs, three-reviewer pass applied, cloud credentials shared with `kasa-cli`.
- `scripts/smoke.py` per SRD §16.0 — Phase 0 hardware-test harness. Probes seven mechanisms per camera (pytapo `getBasicInfo`, pytapo `getStreamURL`, pytapo native-snapshot tier, ONVIF `GetDeviceInformation` / `GetProfiles` / `GetSnapshotUri`, ffmpeg single-frame-from-RTSP). Writes raw fixtures to `tests/fixtures/raw/` and a structured run report. Credentials in URLs are masked before any output.
- Pinned pytapo to git SHA `de5ca3787c710151185bc72a35945d4091727c1e` (latest `main` as of 2026-04-28).
- Added `onvif-zeep-async>=4.0.4,<5`, `WSDiscovery>=2.1.2,<3`, `httpx>=0.27` deps.
- `tests/test_smoke_dryrun.py` — credential-redaction, partial-failure isolation, JSON-mode validity, exit-6 config-error coverage. Mocks pytapo + onvif + subprocess; never touches the network.
- `tests/fixtures/README.md` and `scripts/README.md` — operator docs for fixture scrubbing and smoke-run procedure.

### Notes

- **No source code shipped yet.** Phase 0 (hardware smoke-test gate, SRD §16.0) must pass before Phase 1 verbs (`discover`, `info`, `snapshot`, `stream`, etc.) ship.

## [0.0.2] — 2026-04-28

Phase 0 deliverables (smoke-test harness + dependency pin). No CLI verbs shipped yet — waiting on operator hardware-run results.

## [0.0.1] — 2026-04-28

Project bootstrap. Repository created, SRD frozen at v1.1.1, Phase 0 work begins.
