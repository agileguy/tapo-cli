# tapo-cli — Verb Reference

Complete reference for every verb shipped in v0.4.1. Each entry covers the synopsis, every flag, two or three real example invocations with real output, and the exit codes the verb can return.

The authoritative spec is [`docs/SRD-tapo-cli.md`](SRD-tapo-cli.md). FR/B-anchors in this doc point at SRD subsections — that's where you go when behaviour disagrees.

For configuration semantics see [`CONFIGURATION.md`](CONFIGURATION.md). For the credentials model see [`CREDENTIALS.md`](CREDENTIALS.md). For why things are built the way they are see [`ARCHITECTURE.md`](ARCHITECTURE.md).

## Table of contents

1. [Conventions](#conventions)
2. [Top-level flags](#top-level-flags)
3. [Discovery & inventory](#discovery--inventory) — `discover`, `list`, `info`
4. [Media](#media) — `snapshot`, `stream`, `record`
5. [Camera control](#camera-control) — `privacy`, `led`, `night-vision`, `ptz`, `preset`, `alarm`, `audio`, `osd`, `set`, `reboot`
6. [Motion & events](#motion--events) — `motion`, `events`
7. [Lifecycle & administration](#lifecycle--administration) — `auth`, `config`, `groups`, `batch`

---

## Conventions

**Targets.** A `<target>` is one of:

- An alias from `[devices.<alias>]` in your config (e.g. `office`).
- A bare IPv4 address (e.g. `192.168.86.65`).
- A MAC address (e.g. `10:5A:95:4C:44:C7`).
- A group name prefixed with `@` (e.g. `@perimeter-cams`).

Group fan-out (FR-43d) is supported on every state-control verb except `stream` and `record`, which exit 64 on `@group` targets (FR-43c). `events` likewise rejects `@group` (per-device, single subscription).

**Flag placement.** Top-level flags (`--json`, `--jsonl`, `--quiet`, `--timeout`, `--config`, `--concurrency`, `--credential-source`, `-v`, `-vv`) come BEFORE the verb. Per-verb flags come after. Click is strict about this:

```bash
tapo-cli --json info office          # right
tapo-cli info office --json          # wrong — exits 64
```

**Output mode.** Default is human-readable text on a tty, JSONL on anything else (pipes, redirects, command substitutions). `--json` forces pretty JSON; `--jsonl` forces line-delimited JSON; `--quiet` suppresses stdout entirely (FR-46..49a).

**Exit codes** (SRD §11.1) — see [`ARCHITECTURE.md`](ARCHITECTURE.md#exit-code-model) for the full table. Quick map: `0` ok, `1` device, `2` auth, `3` network, `4` not-found, `5` unsupported, `6` config, `7` partial-failure, `64` usage, `130` SIGINT, `143` SIGTERM.

---

## Top-level flags

```text
--json                            Pretty JSON output (mutually exclusive with --jsonl).
--jsonl                           Newline-delimited JSON.
--quiet                           Suppress stdout. Exit code is the only signal.
--timeout FLOAT                   Per-operation timeout in seconds. Default 5.
--config FILE                     Override config path. Also via TAPO_CLI_CONFIG.
--concurrency INTEGER             Override [defaults] concurrency for this run.
--credential-source [env|file|none]
                                  Constrain credential sources (FR-CRED-15).
-v, -vv                           Stderr JSON-line logs at INFO / DEBUG.
--version                         Print version and exit.
--help                            Show top-level help.
```

`--credential-source` semantics (SRD §6.7):

- `env` — only `TAPO_USERNAME` / `TAPO_PASSWORD` are consulted.
- `file` — only file-based sources (per-device camera-account file, per-device cloud-account override, default credentials file).
- `none` — skip every source. Useful for verifying that a cached pytapo session works without re-auth.

---

## Discovery & inventory

### `discover`

```text
Usage: tapo-cli discover [OPTIONS]
```

Run ONVIF WS-Discovery and a subnet TCP/443 scan in parallel, dedupe by MAC then IP, emit one row per camera (FR-1..5c).

| Flag | Type | Default | Meaning |
|---|---|---|---|
| `--no-scan` / `--ws-discovery-only` | flag | off | Multicast only. FR-1b/1c. |
| `--scan-only` | flag | off | TCP/443 scan only; skip multicast. FR-1d. |
| `--target-network <CIDR>` | string | — | Restrict scan to an explicit CIDR. FR-5/5b. |
| `--probe` | flag | off | Also fetch model + firmware via pytapo `getBasicInfo` (slow). |

Top-level `--timeout` is the total wall-clock budget for both transports. Default 5s; raise it on slow networks.

```bash
$ tapo-cli --json discover --no-scan --timeout 6
INFO timeout reached, 0 devices found (timeout=6s)
[]

$ tapo-cli discover --target-network 192.168.86.0/24
192.168.86.65    10:5A:95:4C:44:C7   C200    true    scan

$ tapo-cli --jsonl discover --probe
{"ip":"192.168.86.65","mac":"10:5A:95:4C:44:C7","model":"C200","firmware_version":"1.3.5 Build 260228 Rel.36932n","supported":true,"source":"both"}
```

A zero-result discovery is success (exit 0, empty output) per FR-5a — no cameras responding is a valid answer to "what's on the LAN."

**Exit codes.** `0` always (zero results included), `3` if both transports fail at the OS level, `6` if `--target-network` matches no local interface (FR-5b).

---

### `list`

```text
Usage: tapo-cli list [OPTIONS]
```

Print every alias from your config (FR-6..8). Does NOT touch the network unless you ask.

| Flag | Type | Default | Meaning |
|---|---|---|---|
| `--probe` | flag | off | Probe each device on TCP/443 and add `online: bool`. |
| `--online-only` | flag | off | Implies `--probe`; hide entries that don't respond. FR-8. |

```bash
$ tapo-cli list
{"alias":"office","ip":"192.168.86.65","mac":"10:5A:95:4C:44:C7","model":"C200","online":null}

$ tapo-cli --json list
[
  {
    "alias": "office",
    "ip": "192.168.86.65",
    "mac": "10:5A:95:4C:44:C7",
    "model": "C200",
    "online": null
  }
]

$ tapo-cli --jsonl list --probe
{"alias":"office","ip":"192.168.86.65","mac":"10:5A:95:4C:44:C7","model":"C200","online":true}
```

`online` is `null` when `--probe` was not passed (config-resolved data only). With `--probe` it's a real boolean.

**Exit codes.** `0` on success, `6` if config is malformed.

---

### `info <target>`

```text
Usage: tapo-cli info [OPTIONS] TARGET
```

Issue a live `pytapo getBasicInfo` plus capability probes against the device and emit the full §10.1 Camera record (FR-9..10a).

```bash
$ tapo-cli --json info office
{
  "alias": "office",
  "features": ["ir", "led", "privacy", "ptz"],
  "firmware_version": "1.3.5 Build 260228 Rel.36932n",
  "hardware_version": "5.0",
  "has_camera_account": true,
  "ip": "192.168.86.65",
  "last_seen": "2026-04-30T00:27:07Z",
  "led_state": "off",
  "mac": "10:5A:95:4C:44:C7",
  "model": "C200",
  "motion_enabled": false,
  "night_vision_mode": "unknown",
  "privacy_enabled": false,
  "supported": true
}

$ tapo-cli --jsonl info @perimeter-cams
{"target":"front-door","status":"ok","exit_code":0,"result":{...}}
{"target":"backyard","status":"ok","exit_code":0,"result":{...}}
```

Models not on the v1 verified list are still queried, but `supported: false` is emitted and a single WARN log line lands on stderr (FR-10a).

**Exit codes.** `0`, `1` on device error, `2` on auth, `3` on network, `4` on unknown alias, `7` on mixed-result group fan-out.

---

## Media

### `snapshot`

```text
Usage: tapo-cli snapshot [OPTIONS] TARGET
```

Pull one JPEG via the three-mechanism fallback chain (FR-11..11d): pytapo native → ONVIF `GetSnapshotUri` → ffmpeg single-frame from RTSP.

| Flag | Type | Default | Meaning |
|---|---|---|---|
| `--output PATH` | string | required | Destination JPEG path. `-` writes binary bytes to stdout. |
| `--snapshot-budget <spec>` | string | — | Override per-tier seconds: `pytapo=N,onvif=N,ffmpeg=N`. Sum SHALL NOT exceed `--timeout` (FR-11a.3). |
| `--onvif-port INTEGER` | int | 2020 | ONVIF service port (Tapo C-series default). |

`--timeout` is the TOTAL wall-clock budget across all three tiers; default split is 40% pytapo / 30% ONVIF / 30% ffmpeg. Auth rejection at any tier short-circuits to exit 2 (FR-11a.2) — no point burning the next tier's budget on a credential the camera already rejected.

```bash
$ tapo-cli --json snapshot office --output /tmp/snap.jpg
{"target":"office","output":"/tmp/snap.jpg","mechanism":"pytapo","bytes":48201}

$ tapo-cli snapshot office --output - > /tmp/snap.jpg     # binary on stdout

$ tapo-cli --json snapshot office --output /tmp/snap.jpg --timeout 10 --snapshot-budget pytapo=5,onvif=3,ffmpeg=2
{"target":"office","output":"/tmp/snap.jpg","mechanism":"onvif","bytes":52311}

# All three tiers timed out — structured failure per attempt.
$ tapo-cli --json snapshot office --output /tmp/snap.jpg --timeout 5
{"error":"device_error","exit_code":1,"message":"all snapshot mechanisms failed for 'office'","target":"office","details":{"attempts":[{"mechanism":"pytapo","status":"fail","elapsed_ms":2001.28,"detail":"timeout after 2.00s"},{"mechanism":"onvif","status":"fail","elapsed_ms":1641.7,"detail":"timeout after 1.50s"},{"mechanism":"ffmpeg","status":"fail","elapsed_ms":1508.55,"detail":"timeout after 1.50s"}]}}
```

`@group` snapshot fan-out is supported but `--output` MUST contain a `{target}` placeholder (e.g. `--output /tmp/snap-{target}.jpg`) — silently clobbering N JPEGs into one file is a footgun. Without the placeholder, exits 64.

**Exit codes.** `0`, `1` (all three tiers failed without auth-rejection — FR-11c), `2` (auth at any tier — FR-11a.2), `3` (network), `5` (unsupported snapshot endpoint), `6` (ffmpeg missing on PATH at tier 3 — FR-11a.4; or `--snapshot-budget` sum exceeds `--timeout`), `64` (`--output -` with `--json`/`--jsonl` — FR-11d).

SRD anchor: FR-11..11d, B5.

---

### `stream`

```text
Usage: tapo-cli stream [OPTIONS] TARGET [EXEC_ARGV]...
```

Emit one RTSP URL on stdout (FR-12..12g). Does not decode video — pipe to `mpv`, `ffmpeg`, `ffplay`, or whatever you actually want to render or mux.

| Flag | Type | Default | Meaning |
|---|---|---|---|
| `--lens [wide\|telephoto]` | choice | `wide` | Lens selector for dual-lens cameras (C225). Single-lens cameras ignore this. |
| `--quality [hd\|sd]` | choice | `hd` | HD = main stream, SD = sub-stream. |
| `--protocol [stream1\|stream2\|stream6\|stream7]` | choice | — | Override the lens/quality truth table with an explicit stream path. |
| `--profile NAME` | string | — | Force a specific ONVIF profile by name (FR-12b.1). |
| `--list-profiles` | flag | off | Emit the ONVIF `GetProfiles` response as JSON and exit (FR-12b.2). |
| `--credentials-via-env` | flag | off | Redact creds in the printed URL; export `RTSP_USER`/`RTSP_PASS` for an exec'd child (FR-12f). |
| `--exec` | flag | off | Replace `tapo-cli` with a child process via `execvp`; `{}` placeholder substituted with the URL (FR-12g). |
| `--onvif-port INTEGER` | int | 2020 | ONVIF service port. |

Stream-path resolution prefers ONVIF `GetProfiles` when available (FR-12b); when not, falls back to the lens × quality truth table. `--json` reports `"resolver": "onvif"|"defaults"` so you know which path produced the URL.

```bash
$ tapo-cli stream office
rtsp://<user>:<pass>@192.168.86.65:554/stream1

$ tapo-cli --json stream office
{
  "lens": "wide",
  "protocol": "rtsp",
  "quality": "hd",
  "resolver": "defaults",
  "target": "office",
  "url": "rtsp://<user>:<pass>@192.168.86.65:554/stream1"
}

$ tapo-cli stream office --quality sd
rtsp://<user>:<pass>@192.168.86.65:554/stream2

# C225 telephoto path
$ tapo-cli stream backyard --lens telephoto
rtsp://<user>:<pass>@192.168.86.51:554/stream6

# Pipe straight into ffmpeg (creds embedded in URL — visible in process list)
$ tapo-cli stream office | xargs -I{} ffmpeg -i {} -c copy /tmp/cam.mp4

# Better: --credentials-via-env --exec keeps the password out of argv
$ tapo-cli stream office --credentials-via-env --exec ffmpeg -i '{}' -c copy /tmp/cam.mp4
```

**Group rejection.** `stream @indoor` exits 64 with a hint: multiple cameras can't share one URL.

**Exit codes.** `0`, `2` (no `camera_account_file` configured — FR-12e), `3` (network), `4` (unknown alias), `5` (ONVIF unavailable for `--list-profiles`, or `--protocol hls` requested — FR-12c), `64` (group target — FR-43c).

SRD anchor: FR-12..12g, B6, S2.

---

### `record`

```text
Usage: tapo-cli record [OPTIONS] TARGET
```

Spawn `ffmpeg` as a foreground subprocess to record the RTSP stream to a local MP4 (FR-13..13g). One-shot. Lives and dies with the CLI invocation.

| Flag | Type | Default | Meaning |
|---|---|---|---|
| `--output FILE` | path | required | Destination MP4. Parent directory must exist. |
| `--duration INTEGER` | int | — | Fixed-length recording in whole seconds (ffmpeg `-t`). |
| `--max-bytes INTEGER` | int | — | Size-capped recording (ffmpeg `-fs`). |
| `--lens [wide\|telephoto]` | choice | `wide` | Same as `stream`. |
| `--quality [hd\|sd]` | choice | `hd` | Same as `stream`. |
| `--protocol [stream1\|stream2\|stream6\|stream7]` | choice | — | Same as `stream`. |

**Footgun guard (FR-13a).** In non-tty mode, you MUST supply `--duration` OR `--max-bytes` — open-ended recording from a script is how disks fill up. In tty mode without either, you get a confirmation prompt on stderr.

```bash
$ tapo-cli record office --output /tmp/cam.mp4 --duration 30
# 30 seconds of MP4 at /tmp/cam.mp4

$ tapo-cli record office --output /tmp/cam.mp4 --max-bytes 1073741824
# Recording capped at 1 GiB

# In a script with no terminal:
$ tapo-cli record office --output /tmp/cam.mp4 < /dev/null
{"error":"usage_error","exit_code":64,"message":"record requires --duration or --max-bytes when stdin is not a tty",...}
```

**Signal handling (FR-13b).** SIGINT/SIGTERM forwards to the ffmpeg child; the CLI waits up to 5s for ffmpeg to flush and finalize the MP4 before exiting 130/143. The 2s typical-case target (FR-13g) is for recordings under 1 GB.

**Group rejection.** `record @group` exits 64 (FR-43c).

**Exit codes.** `0`, `2` (no `camera_account_file`), `3` (network), `4` (unknown alias), `6` (ffmpeg missing on PATH — FR-13c), `64` (group target; missing `--duration`/`--max-bytes` in non-tty), `130`/`143` (signal).

SRD anchor: FR-13..13g, S3.

---

## Camera control

Every verb in this section honors `@group` fan-out (FR-43d) — the per-target B10 envelope `{target, status, exit_code, result?, error?}` is emitted as JSONL in resolved-alias-list order, and the overall exit code follows FR-43a (0 / 7 / first-failure-code).

### `privacy`

```text
Usage: tapo-cli privacy [OPTIONS] TARGET {enable|disable|status}
```

Engage / disengage / report the privacy mode (lens cover on supported models, feed disable otherwise) — FR-31.

```bash
$ tapo-cli --json privacy office status
{"privacy_enabled":false,"target":"office"}

$ tapo-cli privacy office enable

$ tapo-cli --jsonl privacy @indoor disable
{"target":"office","status":"ok","exit_code":0,"result":{"privacy_enabled":false,"target":"office"}}
```

**Exit codes.** `0`, `1`, `2`, `3`, `5` (model has no privacy feature).

---

### `led`

```text
Usage: tapo-cli led [OPTIONS] TARGET {on|off|status}
```

Front status LED (FR-30).

```bash
$ tapo-cli --json led office status
{"led_enabled":true,"target":"office"}

$ tapo-cli led office off
```

**Exit codes.** `0`, `1`, `2`, `3`, `5`.

---

### `night-vision`

```text
Usage: tapo-cli night-vision [OPTIONS] TARGET {auto|on|off|ir-only|status}
```

Set or report night-vision mode (FR-32). `ir-only` is supported on a subset of cameras per the §3.3.1 capability matrix — unsupported modes exit 5 with the supported set listed in the hint.

```bash
$ tapo-cli --json night-vision office status
{"night_vision_mode":"auto","target":"office"}

$ tapo-cli night-vision office ir-only
```

**Exit codes.** `0`, `1`, `2`, `3`, `5`.

---

### `ptz <target> {pan|tilt|zoom|move|stop}`

```text
Usage: tapo-cli ptz [OPTIONS] TARGET COMMAND [ARGS]...
```

Pan / tilt / zoom motors (FR-14..17c). Sub-verbs:

```text
ptz <target> pan {left|right}  [--step N]      # FR-14
ptz <target> tilt {up|down}    [--step N]      # FR-15
ptz <target> zoom {in|out}     [--step N]      # FR-16 — always device-step-units
ptz <target> move [--pan N] [--tilt N] [--zoom N]   # combined offset
ptz <target> stop                              # FR-17 — halts in-progress motion
```

`--step` semantics depend on the camera's `ptz-mode` (SRD §3.3.1):

- `continuous` (C225) → `--step` is interpreted as **degrees**.
- `step` (C200, C210, C220, C520WS, C530WS) → `--step` is **device-step-units**.
- `none` → exit 5 with "model does not support PTZ".

Zoom `--step` is always device-step-units regardless of `ptz-mode` — there is no documented degree mapping for zoom (FR-16).

```bash
$ tapo-cli --json ptz office pan left --step 10
{"action":"pan","direction":"left","elapsed_ms":989,"step":10,"step_unit":"device-step-units","target":"office"}

$ tapo-cli ptz office tilt up --step 5
$ tapo-cli ptz office zoom in --step 2
$ tapo-cli ptz office move --pan -5 --tilt 5
$ tapo-cli ptz office stop
```

**Idempotence.** Issuing `pan left` twice moves twice — PTZ is a verb, not a state (FR-17b). This is the opposite of `on`/`off` style verbs.

**Exit codes.** `0`, `1`, `2`, `3`, `5` (PTZ on non-PTZ model).

---

### `preset <target> {list|goto|save|delete}`

```text
Usage: tapo-cli preset [OPTIONS] TARGET COMMAND [ARGS]...
```

Saved-position registry on the camera (FR-18..21).

```text
preset <target> list                    # FR-18
preset <target> goto <name>             # FR-19; unknown name → exit 4
preset <target> save <name>             # FR-20; existing name overwritten with WARN log
preset <target> delete <name>           # FR-21; unknown name → exit 4
```

```bash
$ tapo-cli --json preset office list
[]

$ tapo-cli preset office save desk-view
$ tapo-cli preset office goto desk-view
$ tapo-cli preset office delete desk-view
```

**Exit codes.** `0`, `1`, `2`, `3`, `4` (unknown preset name on `goto`/`delete`).

---

### `alarm`

```text
Usage: tapo-cli alarm [OPTIONS] TARGET {enable|disable|trigger|status}
```

Siren control on alarm-equipped models — C320WS, C420, C520WS, C530WS, C710, C720 per the §3.3.1 matrix. `enable`/`disable` toggle the camera's siren response to motion or other triggers; `trigger` manually fires it (FR-26..29).

```bash
$ tapo-cli --json alarm office status
{"action":"status","alarm_enabled":false,"light_enabled":true,"sound_enabled":true,"target":"office"}

$ tapo-cli alarm backyard trigger --duration 5     # 5-second siren burst
$ tapo-cli alarm backyard disable
```

`trigger` defaults to a model-specific duration (typically 10s) when `--duration` is omitted. Models without manual-trigger support exit 5 (FR-28).

**Exit codes.** `0`, `1`, `2`, `3`, `5` (no siren / no manual trigger).

---

### `audio <target> {volume|mic|speaker|tts}`

```text
Usage: tapo-cli audio [OPTIONS] TARGET COMMAND [ARGS]...
```

Camera audio (FR-33..36).

```text
audio <target> volume <0-100>                       # FR-33
audio <target> mic     {mute|unmute|status}         # FR-34
audio <target> speaker {mute|unmute|status}         # FR-35
audio <target> tts <text>                           # FR-36 — most models exit 5
```

```bash
$ tapo-cli audio office volume 60
$ tapo-cli --json audio office mic status
$ tapo-cli audio office mic mute
$ tapo-cli audio backyard tts "The package has arrived"   # C520WS / C530WS only
```

TTS is rare — current matrix supports it on C520WS and C530WS. Unsupported models exit 5.

**Exit codes.** `0`, `1`, `2`, `3`, `5`.

---

### `osd <target> {set|clear|status}`

```text
Usage: tapo-cli osd [OPTIONS] TARGET COMMAND [ARGS]...
```

On-screen-display overlay (FR-37..37b).

```text
osd <target> set [--text "<s>"] [--position tl|tr|bl|br] [--show-time/--hide-time]
osd <target> clear
osd <target> status
```

`--text` is capped at 32 Unicode codepoints (FR-37a) — over that, exit 64 (CLI's fault). Codepoints the camera firmware can't render exit 1 (device's fault — FR-37b).

```bash
$ tapo-cli --json osd office status
{"action":"status","label":null,"label_on":false,"target":"office","timestamp_on":false}

$ tapo-cli osd office set --text "FRONT DOOR" --position bl --show-time
$ tapo-cli osd office clear
```

**Exit codes.** `0`, `1` (device rejected codepoint), `2`, `3`, `5`, `64` (>32 codepoints).

---

### `set`

```text
Usage: tapo-cli set [OPTIONS] TARGET
```

Apply one or more device-config changes (FR-39, FR-39a). Phase 4a retro-fix — `set` was on the Phase 2 acceptance list but slipped past v0.2.0; v0.3.1 ships it.

| Flag | Type | Meaning |
|---|---|---|
| `--image-flip [on\|off]` | choice | Toggle vertical image flip (FR-39). pytapo `setImageFlipVertical`. |
| `--timezone <IANA>` | string | Set the camera's timezone (FR-39a). e.g. `America/Toronto`. |

At least one of `--image-flip` or `--timezone` is required. Bare `set <target>` exits 64. Other knobs (HDR, noise cancelling, auto-track, recording-to-SD) remain deferred per FR-39b.

```bash
$ tapo-cli --json set office --image-flip off
{
  "changes": {
    "image_flip": false
  },
  "target": "office"
}

$ tapo-cli set office --timezone America/Toronto
$ tapo-cli set office --image-flip on --timezone America/Vancouver

$ tapo-cli set office
{"error":"usage_error","exit_code":64,"message":"set requires at least one of --image-flip or --timezone","target":"office","hint":"Pass --image-flip on|off, --timezone <IANA>, or both. Other knobs (HDR, noise cancelling, etc.) are deferred per FR-39b."}
```

**Exit codes.** `0`, `1`, `2`, `3`, `5` (model lacks the setting), `64` (no flag passed).

SRD anchor: FR-39..39c.

---

### `reboot`

```text
Usage: tapo-cli reboot [OPTIONS] TARGET
```

Reboot the camera (FR-38). In tty mode, prompts for confirmation **on stderr** (so stdout JSON contracts survive piping). In non-tty mode, requires `--yes`. `--quiet` implies `--yes`.

| Flag | Meaning |
|---|---|
| `-y`, `--yes` | Skip the interactive prompt. |

```bash
$ tapo-cli reboot office --yes

# Without --yes in a non-tty:
$ echo | tapo-cli reboot office
{"error":"usage_error","exit_code":64,"message":"reboot requires --yes when stdin/stderr is not a tty","hint":"Pass --yes (or --quiet, which implies --yes) to confirm."}

# Group reboot — FR-43e: ONE prompt naming the resolved member list.
$ tapo-cli reboot @perimeter-cams --yes
```

**Exit codes.** `0`, `1`, `2`, `3`, `4`, `7` (mixed group result), `64` (non-tty without `--yes`).

---

## Motion & events

### `motion <target> {enable|disable|status|history|download-clip}`

```text
Usage: tapo-cli motion [OPTIONS]
Forms:
  motion <target> enable | disable | status | history
  motion history <target> [--since RFC3339] [--limit N] [--event-type ...]
  motion download-clip <target> <event-id> --output PATH --experimental-clips
```

Motion-detection control + event history + (experimental) clip download. SRD §5.9, §5.23, FR-22..25d, FR-63..65.

#### `motion <target> enable | disable | status`

```bash
$ tapo-cli motion office enable
$ tapo-cli motion office disable

$ tapo-cli --json motion office status
{"motion_enabled":true,"sensitivity":"medium","target":"office"}
```

#### `motion history <target>`

| Flag | Type | Default | Meaning |
|---|---|---|---|
| `--since <RFC3339>` | string | 24h ago | Lower time bound. Bare ISO date → `T00:00:00Z`. |
| `--limit INTEGER` | int | 50 | Max events emitted (after sort). |
| `--event-type` | choice | — | Filter to `motion`/`person`/`vehicle`/`doorbell-press`/`unknown`. |

Results are RFC 3339 UTC `Z` (FR-25a) and **sorted ascending by `ts`** (FR-25c) — callers can rely on the order for delta-poll loops. Future `--since` exits 0 with empty output (FR-25d).

```bash
$ tapo-cli --jsonl motion office history --limit 5 --since '2026-04-29T00:00:00Z'
{"ts":"2026-04-29T08:14:02Z","alias":"office","event_type":"motion","region":"full","has_clip":true,"event_id":"1745836042-1745836047"}
{"ts":"2026-04-29T09:31:55Z","alias":"office","event_type":"motion","region":"full","has_clip":true,"event_id":"1745840515-1745840520"}

$ tapo-cli motion office history --since 2026-04-30 --event-type doorbell-press
```

#### `motion download-clip <target> <event-id> --output PATH --experimental-clips`

Phase 4c, gated behind a mandatory `--experimental-clips` flag (FR-63..65). The clip-download path uses pytapo's `experiments/DownloadRecordings.py` flow which has been observed to break across firmware revisions — the flag is the operator's opt-in to that risk.

```bash
$ tapo-cli motion office history --since 1h --jsonl | jq -r 'select(.has_clip).event_id' | head -1
1745836042-1745836047

$ tapo-cli --json motion download-clip office 1745836042-1745836047 --output /tmp/clip.mp4 --experimental-clips
{"target":"office","event_id":"1745836042-1745836047","output_path":"/tmp/clip.mp4","bytes":4823104,"duration_s":12.3,"mechanism":"pytapo-experiments"}

# Without the opt-in flag:
$ tapo-cli motion download-clip office 1745836042-1745836047 --output /tmp/clip.mp4
{"error":"usage_error","exit_code":64,...}
```

The `mechanism: "pytapo-experiments"` field in output is deliberate observability — when pytapo upstream changes the experiments-folder API, regression tests fail in a known shape (FR-65).

**Exit codes (`history`).** `0`, `1`, `2`, `3`, `4`.
**Exit codes (`download-clip`).** `0`, `1`, `2`, `3`, `4` (unknown event id, no SD card, or `has_clip: false`), `5`, `6` (ffmpeg missing), `64` (missing `--experimental-clips`).

SRD anchor: FR-22..25d, FR-63..65, B8.

---

### `events`

```text
Usage: tapo-cli events [OPTIONS] TARGET
```

Push-based event subscription via ONVIF Profile-S `PullPointSubscription` (FR-57..62, §10.6). Distinct from `motion history` — same `event_type` enum, different transport.

| Flag | Type | Default | Meaning |
|---|---|---|---|
| `--follow` | flag | off | Long-running PullPoint loop until SIGINT/SIGTERM. |
| `--types <list>` | string | — | CSV filter: `motion,person,vehicle,doorbell-press,unknown`. |
| `--reconnect-after <seconds>` | int | 0 | Recreate the subscription after N seconds of liveness (broker-TTL workaround). 0 = never. |
| `--limit INTEGER` | int | — | Cap emissions (one-shot mode only; ignored under `--follow`). |
| `--onvif-port INTEGER` | int | 2020 | ONVIF service port. |

**One-shot mode** (no `--follow`): pulls once with `Timeout=PT5S` `MessageLimit=100`, emits any returned events, `Unsubscribe`s cleanly, exits 0.

**Follow mode** (`--follow`): loops on `PullMessages` with `Timeout=PT30S` `MessageLimit=100`. SIGINT/SIGTERM triggers `Unsubscribe` within a 2-second hard budget, emits a final `{"event":"interrupted","subscription_age_s":...}` summary line, and exits 130/143.

**Auto-reconnect on transport error** (FR-61): capped exponential backoff `1s → 2s → 4s → 8s → 16s → 32s → 32s`. Five consecutive failures exit 3.

```bash
$ tapo-cli --jsonl events office --limit 5

$ tapo-cli --jsonl events office --follow --types motion,doorbell-press
{"ts":"2026-04-29T14:02:11Z","target":"office","event_type":"doorbell-press","has_clip":false,"region":null,"source":"onvif"}
{"ts":"2026-04-29T14:02:18Z","target":"office","event_type":"motion","has_clip":false,"region":null,"source":"onvif"}
^C
{"event":"interrupted","subscription_age_s":47.3}

$ tapo-cli events office --follow --reconnect-after 1800
```

`source: "onvif"` distinguishes push events from `motion history`'s `source: "pytapo"`. `has_clip` is currently always false on emitted ONVIF events; the SD-card ±5s heuristic lands when Phase 4c integrates with `pytapo.getRecordings()`.

**Group rejection.** `events @group` exits 64 by design (one subscription per stdout).

**Exit codes.** `0`, `2`, `3` (5 consecutive transport failures), `4`, `5` (camera lacks ONVIF Profile-S — enable Tapo Lab > Third-Party Compatibility), `64` (group target).

SRD anchor: FR-57..62, §10.6.

---

## Lifecycle & administration

### `auth {status|flush|migrate}`

```text
Usage: tapo-cli auth [OPTIONS] COMMAND [ARGS]...
```

pytapo session-cache management (FR-CRED-12, FR-CRED-14, FR-55).

#### `auth status`

Print one row per cached pytapo session-state file. SRD §6.6 / FR-CRED-14.

```bash
$ tapo-cli --json auth status
[
  {
    "alias": "office",
    "mac": "10:5A:95:4C:44:C7",
    "cache_path": "/Users/dan/.config/tapo-cli/.tokens/105A954C44C7.json",
    "mtime": "2026-04-30T00:27:07Z",
    "expires_at": null,
    "bytes_size": 412,
    "pytapo_version": "3.4.13",
    "cloud_account": true,
    "camera_account": true
  }
]
```

`auth status` does NOT issue liveness probes against cached devices — that's `list --probe`'s job (FR-CRED-14).

#### `auth flush`

Delete cached session files.

| Flag | Meaning |
|---|---|
| `--target <alias\|MAC>` | Delete only that device's cache. Default: flush all. |

```bash
$ tapo-cli auth flush                     # flush everything
$ tapo-cli auth flush --target office     # flush one device
```

#### `auth migrate`

Rewrite older versioned credential files at the **tapo-only** path (FR-55, FR-CRED-3.1). Acts ONLY on `~/.config/tapo-cli/credentials` — never on the shared `~/.config/kasa-cli/credentials` (kasa-cli owns that file).

Refuses to run if any target file is not chmod 0600 (exit 2). Preserves the original at `<path>.v<old>.bak`.

```bash
$ tapo-cli auth migrate
```

**Exit codes (all auth sub-verbs).** `0`, `2` (chmod violation, missing version), `6` (parse error).

---

### `config {show|validate}`

```text
Usage: tapo-cli config [OPTIONS] COMMAND [ARGS]...
```

#### `config show`

Print the resolved effective config as TOML, **passwords redacted to `***`** (FR-54c, FR-54d). There is no `--show-secrets` flag in v1 — to inspect a credential file, `cat` it directly.

```bash
$ tapo-cli config show
[defaults]
timeout_seconds = 5
concurrency = 5
output_format = "auto"

[credentials]
file_path = "~/.config/kasa-cli/credentials"

[ffmpeg]
path = "ffmpeg"

[logging]
# file = "~/.local/state/tapo-cli/log"

[devices.office]
ip = "192.168.86.65"
mac = "10:5A:95:4C:44:C7"
model = "C200"
camera_account_file = "~/.config/tapo-cli/cam-office.json"

[groups]
indoor = ["office"]
```

#### `config validate`

```text
Usage: tapo-cli config validate [OPTIONS] [PATH]
```

Parse the file, resolve every alias-to-device reference, resolve every group-to-alias reference, verify referenced credential and camera-account files exist with chmod 0600, exit 0 on success or 6 on failure. Default PATH is the resolved config file (per `--config` / `TAPO_CLI_CONFIG` / `~/.config/tapo-cli/config.toml`).

```bash
$ tapo-cli config validate
$ echo $?
0

$ tapo-cli config validate /nonexistent
{"error":"config_error","exit_code":6,"message":"config file not found: /nonexistent","details":{"path":"/nonexistent"}}
```

**Exit codes.** `0`, `6`.

SRD anchor: FR-54..54d, S16.

---

### `groups list`

```text
Usage: tapo-cli groups [OPTIONS] COMMAND [ARGS]...
Commands:
  list  List every defined group with its member aliases (FR-39..43).
```

Read-only — group mutations are by hand-editing the TOML config (FR-43b). `groups add` / `groups remove` are explicitly out of scope for v1 (comment-preserving TOML round-trip is a non-trivial side quest).

```bash
$ tapo-cli groups list
indoor: office (192.168.86.65)

$ tapo-cli --json groups list
[
  {
    "members": [
      {
        "alias": "office",
        "ip": "192.168.86.65"
      }
    ],
    "name": "indoor"
  }
]
```

**Exit codes.** `0`, `6`.

---

### `batch`

```text
Usage: tapo-cli batch [OPTIONS]
```

Read newline-delimited sub-commands from a file or stdin and emit one JSONL result per line on stdout (FR-44..45c). Empty input exits 0; blank lines are skipped; lines starting with `#` are comments.

| Flag | Meaning |
|---|---|
| `--file <path>` | Read sub-commands from a file. |
| `--stdin` | Read from stdin. |

Per-line shape (FR-44a / B10):

```json
{
  "command": "<verb-and-flags>",
  "target": "<resolved-alias-or-ip>",
  "status": "ok" | "error",
  "exit_code": <int>,
  "result": <verb's normal JSON payload, present iff status == "ok">,
  "error": { "code": "...", "message": "...", "hint": "..." }   // iff status == "error"
}
```

Exit code follows FR-43a: `0` if every sub-op succeeded, `7` if mixed, the first sub-op's failure code if all failed.

```bash
$ cat /tmp/night.batch
# Front porch lockdown sequence
privacy office enable
night-vision office ir-only
motion office enable

$ tapo-cli --jsonl batch --file /tmp/night.batch
{"command":"privacy office enable","target":"office","status":"ok","exit_code":0,"result":{"privacy_enabled":true,"target":"office"}}
{"command":"night-vision office ir-only","target":"office","status":"ok","exit_code":0,"result":{"night_vision_mode":"ir-only","target":"office"}}
{"command":"motion office enable","target":"office","status":"ok","exit_code":0,"result":{"motion_enabled":true,"target":"office"}}

$ echo "led office off" | tapo-cli batch --stdin
```

**SIGINT / SIGTERM during batch** (FR-45c): cease dispatching, wait up to 2s for in-flight ops to complete, emit a final summary line `{"event":"interrupted","completed":N,"pending":M}`, exit 130/143.

**Exit codes.** `0`, `7`, the first sub-op's failure code if all failed, `64` (mutually exclusive flags), `130`/`143`.

SRD anchor: FR-44..45c, B10.

---

## Cross-cutting reference

- **Config schema:** [`CONFIGURATION.md`](CONFIGURATION.md)
- **Credentials model:** [`CREDENTIALS.md`](CREDENTIALS.md)
- **Architecture (snapshot fallback chain, fan-out, exit codes):** [`ARCHITECTURE.md`](ARCHITECTURE.md)
- **Authoritative spec:** [`SRD-tapo-cli.md`](SRD-tapo-cli.md)
