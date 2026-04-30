# tapo-cli

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

Deterministic, scriptable command-line tool for discovering, querying, and controlling TP-Link Tapo cameras and wired doorbells on the local LAN. Sister project to [`kasa-cli`](https://github.com/agileguy/kasa-cli) — same product philosophy, different protocol surface.

**Status:** v0.4.1 — feature-complete. SRD frozen at v1.2.0. Every Phase 0 / 1 / 2 / 3 / 4a / 4b / 4c verb ships; verified against a live Tapo C200 at every phase boundary. See [`CHANGELOG.md`](CHANGELOG.md).

---

## What it is

A single CLI binary that takes a verb, a target, and flags, performs one operation against one or more Tapo devices over the local network, prints a result on stdout, and exits with a meaningful status code. The leaf node in a shell pipeline or cron job — nothing more.

```bash
tapo-cli discover                                # find cameras on the LAN
tapo-cli info @front-door                        # device state
tapo-cli snapshot @front-door --output snap.jpg  # still image
tapo-cli stream @front-door                      # emit RTSP URL
tapo-cli privacy @front-door enable              # privacy mode
tapo-cli ptz @front-door pan left --step 15      # pan/tilt/zoom
tapo-cli motion history @front-door --since 1h   # motion events
tapo-cli events @front-door --follow             # ONVIF push events
```

## What it is not

- Not a media player — `stream` emits a URL; pipe to `mpv`/`ffmpeg` yourself.
- Not an NVR — `record` is a one-shot foreground subprocess with `--duration` cap.
- Not a rules engine — Home Assistant or Node-RED handle automation.
- Not a cloud daemon — local LAN only, no port forwarding, no TP-Link cloud relay.
- Not Kasa — see [`kasa-cli`](https://github.com/agileguy/kasa-cli) for plugs, bulbs, strips, switches.

## Highlights

- **Deterministic exit codes** (per SRD §11.1) — `0` ok, `1` device, `2` auth, `3` network, `4` not-found, `5` unsupported, `6` config, `7` partial, `64` usage, `130`/`143` SIGINT/SIGTERM.
- **Local LAN only** — never contacts TP-Link cloud servers. Cloud-account credentials are used solely for on-LAN session derivation against the device.
- **Three-mechanism snapshot fallback** — pytapo native → ONVIF `GetSnapshotUri` → ffmpeg single-frame from RTSP. Auth-rejection short-circuits; ffmpeg-missing exits 6.
- **Output formats** — text on tty, JSONL on pipes/redirects, `--json` / `--jsonl` / `--quiet` overrides.
- **Group fan-out** — `tapo-cli privacy @perimeter-cams enable` runs in parallel across configured group members. Per-target B10 envelope per FR-43d.
- **Batch mode** — `tapo-cli batch --file ops.txt` for cron-friendly sequenced operations with graceful Ctrl-C drain.
- **Session caching** — pytapo session state persists per-MAC under `~/.config/tapo-cli/.tokens/`, atomic-write + flock + library-version-keyed invalidation.
- **Push events** — `tapo-cli events <target> --follow` subscribes to ONVIF Profile-S `PullPointSubscription`, emits JSONL until SIGINT.

## Install

```bash
uv tool install git+ssh://git@github.com/agileguy/tapo-cli@v0.4.1
```

Updates: `uv tool upgrade tapo-cli`.

Requires Python 3.11+. Tested on macOS 13+ and Linux x86_64/arm64. Windows is not supported (use WSL).

ffmpeg required on `PATH` for `record` and the snapshot tier-3 fallback.

## Quick start

```bash
# Discover everything on the LAN — both ONVIF multicast and TCP/443 scan, in parallel.
tapo-cli discover

# Drop a config at ~/.config/tapo-cli/config.toml. Minimal example:
cat > ~/.config/tapo-cli/config.toml <<'EOF'
[devices.office]
ip = "192.168.86.65"
mac = "10:5A:95:4C:44:C7"
model = "C200"
camera_account_file = "~/.config/tapo-cli/cam-office.json"

[groups]
indoor = ["office"]
EOF

# Camera account file (chmod 0600). Created in the Tapo app:
#   Settings > Advanced settings > Camera account
cat > ~/.config/tapo-cli/cam-office.json <<'EOF'
{"version": 1, "username": "<6-32 chars>", "password": "<6-32 chars>"}
EOF
chmod 0600 ~/.config/tapo-cli/cam-office.json

# Validate the config
tapo-cli config validate

# First verb against a real camera
tapo-cli info office --json
```

## Verbs at a glance

Grouped by category. One-line description per verb; flags and exit codes live in [`docs/USAGE.md`](docs/USAGE.md).

### Read-only / discovery

| Verb | What it does |
|---|---|
| `discover` | LAN-scan for Tapo cameras (WS-Discovery + TCP/443 in parallel). `tapo-cli discover` |
| `list` | Enumerate configured aliases. `tapo-cli list --probe` |
| `info <target>` | Full device record over pytapo `getBasicInfo` + capability probes. `tapo-cli info office` |
| `groups list` | Read-only group enumeration. `tapo-cli groups list` |

### Camera control (`@group` fan-out per FR-43d)

| Verb | What it does |
|---|---|
| `privacy <target> enable\|disable\|status` | Privacy lens-cover. `tapo-cli privacy office enable` |
| `led <target> on\|off\|status` | Front status LED. `tapo-cli led office off` |
| `night-vision <target> auto\|on\|off\|ir-only\|status` | Night vision mode. `tapo-cli night-vision office ir-only` |
| `motion <target> enable\|disable\|status` | Motion detection toggle + sensitivity. `tapo-cli motion office enable` |
| `motion history <target> [--since ... --limit ... --event-type ...]` | RFC 3339 UTC motion-event timeline. `tapo-cli motion office history --limit 10` |
| `motion download-clip <target> <event-id>` | Phase 4c experimental — gated behind `--experimental-clips`. |
| `ptz <target> pan\|tilt\|zoom\|move\|stop` | PTZ motors. `tapo-cli ptz office pan left --step 10` |
| `preset <target> list\|goto\|save\|delete` | Saved-position registry. `tapo-cli preset office goto desk-view` |
| `alarm <target> enable\|disable\|trigger\|status` | Siren control. `tapo-cli alarm backyard trigger --duration 5` |
| `audio <target> volume\|mic\|speaker\|tts` | Speaker volume / mic-mute / TTS. `tapo-cli audio office volume 60` |
| `osd <target> set\|clear\|status` | On-screen-display overlay. `tapo-cli osd office set --text "FRONT" --show-time` |
| `set <target> [--image-flip] [--timezone]` | Image flip, timezone (FR-39 retro-fix in 4a). `tapo-cli set office --timezone America/Toronto` |

### Media (NEVER fan out — per-device only)

| Verb | What it does |
|---|---|
| `snapshot <target> --output PATH` | Three-mechanism JPEG capture. `tapo-cli snapshot office --output /tmp/snap.jpg` |
| `stream <target>` | Emit `rtsp://...` URL on stdout. `tapo-cli stream office \| xargs mpv` |
| `record <target> --output PATH --duration N` | One-shot ffmpeg recording with mandatory cap in non-tty mode. |

### Push events (per-device, no `@group --follow`)

| Verb | What it does |
|---|---|
| `events <target> [--follow] [--types ...]` | ONVIF `PullPointSubscription`; long-running JSONL until SIGINT. |

### Lifecycle

| Verb | What it does |
|---|---|
| `reboot <target>` | Reboot the camera. tty prompt or `--yes` non-tty. Group-level confirm for `@group` (FR-43e). |

### Auth & config

| Verb | What it does |
|---|---|
| `auth status` | One row per cached pytapo session (mtime, expires_at, source). |
| `auth flush [--target ...]` | Delete cached pytapo session state. |
| `auth migrate` | Rewrite older versioned credential files in place (tapo-only path). |
| `config show` | Print resolved effective config as TOML; passwords redacted to `***`. |
| `config validate [PATH]` | Lint a config file. Exit 0 / 6. |

### Batch

| Verb | What it does |
|---|---|
| `batch --file PATH` / `--stdin` | Newline-delimited sub-command runner with FR-44a JSONL output. |

Full reference (every flag, every default, two-or-three real examples per verb): [`docs/USAGE.md`](docs/USAGE.md).

## Credentials

Tapo cameras need two credentials: a per-device **camera account** (created in the Tapo app under Settings → Advanced settings → Camera account) and the **TP-Link cloud account** you use to log into the mobile app.

The killer ergonomic: **tapo-cli reads cloud credentials from kasa-cli by default.** Same TP-Link account authenticates both Kasa plugs and Tapo cameras — no point storing the password twice. The default cloud-credentials path is `~/.config/kasa-cli/credentials` (FR-CRED-3.1, read-only from tapo-cli). A tapo-only override at `~/.config/tapo-cli/credentials` wins when present.

For RTSP-using verbs (`stream`, `record`), only the per-device camera account is consulted — RTSP is camera-account-only by protocol.

Full credential model, file formats, session caching, and troubleshooting: [`docs/CREDENTIALS.md`](docs/CREDENTIALS.md).

## Documentation

| Doc | Purpose |
|---|---|
| [`docs/USAGE.md`](docs/USAGE.md) | Every verb, every flag, real examples. |
| [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md) | TOML schema, resolution order, worked example. |
| [`docs/CREDENTIALS.md`](docs/CREDENTIALS.md) | Dual-credential model, kasa-cli sharing, session cache, troubleshooting. |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Why the snapshot fallback, the fan-out, the exit codes — and why three libraries. |
| [`docs/CONTRIBUTING.md`](docs/CONTRIBUTING.md) | Dev setup, test policy, pytapo SHA pin policy, phase workflow. |
| [`docs/SRD-tapo-cli.md`](docs/SRD-tapo-cli.md) | Authoritative software requirements document (v1.2.0). |
| [`CHANGELOG.md`](CHANGELOG.md) | Per-version delta. |

The SRD is the system of record. README is a courtesy summary. If they disagree, the SRD wins.

## Phase plan

| Phase | Scope | Status |
|---|---|---|
| 0 | Hardware smoke-test gate (pytapo SHA pin + per-camera fixture capture) | Shipped |
| 1 | MVP — discover, list, info, snapshot, stream, basic state, reboot | Shipped (v0.1.0) |
| 2 | PTZ, presets, alarm, audio, OSD | Shipped (v0.2.0) |
| 3 | record, motion history, groups, batch, signal handling | Shipped (v0.3.0) |
| 4a | Fan-out generalization + `set` retro-fix | Shipped (v0.3.1) |
| 4b | `events --follow` ONVIF push subscription | Shipped (v0.4.0) |
| 4c | Motion-clip download (experimental) | Shipped (v0.4.1) |

## Supported devices

The SRD §3.3 verified-list is the v1 contract. Cameras (TC55/60/70/82/85, C100/110/120/125/200/201/210/211/216/220/225/236, C310/320WS/410/420/420S2/500/510W/520WS/530WS/710/720) and wired doorbells (D100C, D210, D230, D235) are exercised. Models on the "untested in v1" row (C402/403/460/465/560WS/610/615F/645D/660/675D/TC53/TCW90) are unverified — they MAY work but emit a `supported: untested` field with a stronger WARN log. Battery-mode doorbells (D210/D235 in battery mode) cannot accept a camera account and are explicitly out of scope.

Per-feature × per-model capability matrix: SRD §3.3.1.

## Stack

- Python 3.11+, `uv` for packaging
- [`pytapo`](https://github.com/JurajNyiri/pytapo) — pinned by git SHA, not floating constraint (HA 2025.11 incident lesson)
- [`onvif-zeep-async`](https://pypi.org/project/onvif-zeep-async/) 4.0.4 — ONVIF SOAP client
- [`WSDiscovery`](https://pypi.org/project/WSDiscovery/) 2.1.2 — multicast transport
- ffmpeg subprocess for snapshot tier-3 + `record` + motion-clip concat
- [Click](https://palletsprojects.com/projects/click/) for the CLI surface
- `tomllib` (stdlib) for config; `pytest` + `mypy` + `ruff` for quality

## License

MIT — see [LICENSE](LICENSE).

## Repository

- Source: <https://github.com/agileguy/tapo-cli>
- Issues: <https://github.com/agileguy/tapo-cli/issues>
