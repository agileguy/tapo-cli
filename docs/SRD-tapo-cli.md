# Software Requirements Document: tapo-cli

**Document ID:** SRD-TAPO-CLI-001
**Version:** 1.2.0
**Date:** 2026-04-29
**Status:** Draft v1.2 — Phase 4 scope defined
**Author:** Dan Elliott
**Source:** Derived from user requirements + verified pytapo 3.4.13 (PyPI, released 2026-04-14), onvif-zeep-async 4.0.4 (PyPI, released 2025-08-20), WSDiscovery 2.1.2 (PyPI, released 2025-01-24), and HomeAssistant-Tapo-Control feature inventory (GitHub) as of 2026-04-28. v1.1.0 incorporates Architect / Engineer / Researcher review findings; v1.2.0 incorporates a Phase 0-3 implementation audit and Phase 4 scope decisions — see §17 Revision History.

---

## Table of Contents

1. [Overview](#1-overview)
2. [Goals and Non-Goals](#2-goals-and-non-goals)
3. [Background and Prior Art](#3-background-and-prior-art)
4. [Architecture Decision: Wrap vs Reimplement](#4-architecture-decision-wrap-vs-reimplement)
5. [Functional Requirements](#5-functional-requirements)
6. [Authentication and Credentials](#6-authentication-and-credentials)
7. [Non-Functional Requirements](#7-non-functional-requirements)
8. [CLI Surface](#8-cli-surface)
9. [Configuration File](#9-configuration-file)
10. [Data Model](#10-data-model)
11. [Error Model and Exit Codes](#11-error-model-and-exit-codes)
12. [Testing Strategy](#12-testing-strategy)
13. [Distribution and Install](#13-distribution-and-install)
14. [Out of Scope](#14-out-of-scope)
15. [Resolved Decisions](#15-resolved-decisions)
16. [Phase Plan](#16-phase-plan)
17. [Revision History](#17-revision-history)

---

## 1. Overview

`tapo-cli` is a deterministic, scriptable command-line tool for discovering, querying, and controlling TP-Link Tapo cameras and doorbells on the local LAN. It is the sister tool to `kasa-cli` — same product philosophy, different device family. It is not a video player, not a NVR, not a HomeKit bridge, not a cloud daemon, not an MQTT broker, not a rules engine, and not a GUI. It is a single binary that takes a verb, a target, and flags, performs one operation against one camera (or a group), prints a result on stdout — typically structured JSON or, for streams, a `rtsp://` URL — and exits with a meaningful status code. Its job is to be the leaf node in a shell pipeline, ffmpeg upstream, or cron job — nothing more.

The CLI deliberately separates **control** (which uses pytapo and the TP-Link cloud-account credential) from **media** (which uses the camera's RTSP server and a separate "camera account" credential). Both seams are visible to the user, and both credentials are stored in chmod-0600 files local to the host.

---

## 2. Goals and Non-Goals

### 2.1 Goals

- **Discover** Tapo cameras and doorbells on the local network using ONVIF WS-Discovery, with subnet-scan fallback for cameras whose ONVIF stack is disabled or quirky
- **Query** device state (model, firmware, MAC, alias, on/off, motion-detection state, privacy-mode state, LED state)
- **Control** non-media surfaces: PTZ (pan/tilt/zoom on PTZ-capable models), preset positions, motion detection, alarm/siren, status LED, privacy mode (lens cover), night-vision mode, on-screen-display overlay, audio volume, mic and speaker mute
- **Emit RTSP stream URLs** for downstream consumption (`ffmpeg`, `mpv`, `ffplay`, NVR software). The CLI does NOT decode video itself
- **Pull a snapshot still image** to a local file via a three-mechanism fallback chain (pytapo → ONVIF `GetSnapshotUri` → ffmpeg single-frame from RTSP)
- **Optionally record** to a local file via the `record` verb that spawns ffmpeg as a foreground child process (the recording lives and dies with the CLI invocation; non-tty callers must specify `--duration` or `--max-bytes` per FR-13a)
- **Be scriptable**: deterministic exit codes, JSON/JSONL output, no interactive prompts in non-tty mode
- **Group devices logically** via local config (alias-to-IP map and group-to-alias-list map)
- **Run batch operations** across multiple devices in parallel
- **Cache pytapo session state** between invocations to avoid per-command re-auth latency

### 2.2 Non-Goals

- **No GUI.** This is a CLI. Visual dashboards belong elsewhere.
- **No video display in the terminal.** `stream` prints a URL. Decoding is the consumer's job.
- **No motion-event video clip downloads in v1.** Vendor-specific binary protocols; pytapo has experimental support but it's brittle. Deferred.
- **No two-way real-time audio.** Would require WebRTC or SIP. Out of scope at all phases of this SRD.
- **No NVR functionality.** Long-running multi-day recording, retention policies, motion-triggered DVR — wrong tool. Use Frigate, Shinobi, or Synology Surveillance Station downstream of `tapo-cli stream`.
- **No scheduling daemon.** Cron, systemd timers, and launchd handle scheduling. The CLI exposes a verb; the scheduler invokes it.
- **No cloud relay.** Local LAN only. No port forwarding. No TP-Link cloud control plane (cloud-account credentials are used solely for on-LAN session derivation).
- **No automation rules engine.** "If motion detected, turn on lights" is Home Assistant or Node-RED territory.
- **No Matter or Thread support.** Tapo cameras don't speak either.
- **No Kasa-line support.** Kasa plugs/bulbs/strips/switches are `kasa-cli`'s lane. This CLI does not encroach.
- **No battery-doorbell third-party integration.** Tapo D210 and D235 in battery mode cannot accept a camera account (verified against HA-Tapo-Control discussions #739, #794). Wired/always-on operation only for `tapo-cli`.
- **No firmware updates.** Use the Tapo app.
- **No Wi-Fi provisioning.** Use the Tapo app.

---

## 3. Background and Prior Art

### 3.1 pytapo

The de-facto open-source library for Tapo camera control is **pytapo** (https://github.com/JurajNyiri/pytapo, https://pypi.org/project/pytapo/), latest stable **3.4.13** released **2026-04-14**, MIT-licensed, declared minimum Python **3.13** in package metadata (works on 3.11+ in practice; the 3.13 minimum is conservative). pytapo is a thin wrapper over the camera's HTTPS control plane. It supports:

- **Camera-account login** (preferred; created in the Tapo app under Settings > Advanced settings > Camera account)
- **Cloud-account fallback login** ("admin" username + TP-Link cloud password)
- **Stream URL retrieval** (`getStreamURL()`)
- **PTZ control** (move, calibrate, save/delete preset)
- **Privacy mode** toggle
- **Motion detection** enable/disable, sensitivity, recent-events query
- **Reboot**
- **Recordings** download from on-camera SD card (requires ffmpeg subprocess for the stream conversion step)
- **LED, alarm/siren, night vision, OSD, audio volume** — surface coverage varies by model

pytapo's README explicitly does **not** enumerate supported models. The community-maintained Home Assistant integration (`HomeAssistant-Tapo-Control`, see §3.3) is the de-facto compatibility matrix.

**Upstream cadence note.** During the Home Assistant 2025.11.0 release window, the community HA-Tapo-Control integration was pinned to pytapo **3.3.51** and that exact pinned version became incompatible with shipping firmware — operators on HA 2025.11 + pytapo 3.3.51 saw auth break with no advance notice (HA-Tapo-Control issue #1099). The lesson is not "newer pytapo bad" — it is that single-maintainer libraries with firmware-coupled APIs require pinning to a known-good git SHA in this CLI, not a floating `>=3.4.13` constraint.

### 3.2 onvif-zeep-async

The maintained ONVIF client for Python is **onvif-zeep-async** (https://pypi.org/project/onvif-zeep-async/), latest stable **4.0.4** released **2025-08-20**, MIT-licensed, Python 3.10+. Async-native, built on `zeep[async]>=4.1.0` and `httpx`. The Home Assistant ecosystem standardized on this library over the abandoned `onvif-zeep` (last release 2018-08-20 — do not use for new work).

`tapo-cli` uses `onvif-zeep-async` for two purposes:
1. **WS-Discovery** of cameras on the LAN
2. **Snapshot fallback** via `GetSnapshotUri` when pytapo's snapshot path fails (model- or firmware-dependent)

Tapo cameras advertise ONVIF Profile S (live streaming + snapshot) on most current models, but support is per-firmware and the user must enable "Tapo Lab > Third-Party Compatibility" in the app on some firmware revisions. ONVIF support on doorbells is sparse and the CLI SHALL NOT depend on it for doorbell operations.

### 3.3 HomeAssistant-Tapo-Control

The community-maintained Home Assistant integration `HomeAssistant-Tapo-Control` (https://github.com/JurajNyiri/HomeAssistant-Tapo-Control) is the gold-standard feature reference for Tapo cameras. It is the wrong tool for shell scripting (requires a running HA instance), but its README enumerates supported models and capabilities, which `tapo-cli` adopts as the v1 device matrix:

| Family | Models verified by HA-Tapo-Control README (as of 2026-04-28) |
|--------|--------------------------------------------------------------|
| Cameras (TC) | TC55, TC60, TC70, TC82, TC85 |
| Cameras (C, indoor) | C100, C110, C120, C125, C200, C201, C210, C211, C216, C220, C225, C236 |
| Cameras (C, outdoor) | C310, C320WS, C410, C420, C420S2, C500, C510W, C520WS, C530WS, C710, C720 |
| Doorbells | D100C, D210, D230, D235 |
| Cameras (current TP-Link catalog, **untested in v1**) | C402, C403, C460, C465, C560WS, C610, C615F, C645D, C660, C675D, TC53, TCW90 |

**Disclaimer.** The verified rows above are derived from the HA-Tapo-Control README as of 2026-04-28 — this is **NOT** equivalent to TP-Link's current product catalog. The "untested" row enumerates models advertised on TP-Link's current cloud-camera product page that have no third-party-integration data yet; their compatibility status is **UNVERIFIED in v1**. Legacy entries (C100/C200) are kept because they're widely deployed in the field even if absent from current sales pages.

This combined list is the **v1 supported set** for warning purposes. Models not on it MAY work but are unsupported; the CLI SHALL NOT block unknown models, but `tapo-cli info` SHALL flag them with a `supported: false` field in JSON output and a stderr warning. Models on the "untested" row SHALL emit a `supported: untested` value with a stronger stderr warning recommending Phase 0 smoke-test (§16.0).

### 3.3.1 Capability matrix (per-feature × per-model)

The following matrix is the v1 contract for what `tapo-cli info` reports under `features` and what Phase 2 verbs (FR-31 privacy, FR-33 audio volume, FR-36 tts, FR-37 osd, FR-28 alarm trigger, FR-32 night-vision modes, FR-12b dual-lens stream, FR-14..17 ptz) check before issuing a request. A blank cell means **unsupported on that model** — verbs targeting it SHALL exit code 5 with a hint pointing at this matrix.

| Model | tts | osd | alarm-trigger | night-vision modes | audio-volume | dual-lens-stream | ptz-mode |
|-------|-----|-----|---------------|---------------------|--------------|-------------------|----------|
| C100 | — | yes | — | auto/on/off | — | — | none |
| C110 | — | yes | — | auto/on/off | — | — | none |
| C200 | — | yes | — | auto/on/off | — | — | step |
| C210 | — | yes | — | auto/on/off | yes | — | step |
| C220 | — | yes | — | auto/on/off/ir-only | yes | — | step |
| C225 | — | yes | — | auto/on/off/ir-only | yes | yes (wide+telephoto) | continuous |
| C320WS | — | yes | yes | auto/on/off/ir-only | yes | — | none |
| C420 / C420S2 | — | yes | yes | auto/on/off/ir-only | yes | — | none |
| C520WS | yes | yes | yes | auto/on/off/ir-only | yes | — | step |
| C530WS | yes | yes | yes | auto/on/off/ir-only | yes | — | step |
| C710 / C720 | — | yes | yes | auto/on/off | yes | — | none |
| TC55 / TC60 / TC70 / TC82 / TC85 | — | yes | — | auto/on/off | — | — | none |
| D100C | — | yes | — | auto/on/off | yes | — | none |
| D210 (wired) | — | yes | — | auto/on/off | yes | — | none |
| D230 | — | yes | — | auto/on/off | yes | — | none |
| D235 (wired) | — | yes | — | auto/on/off | yes | — | none |
| **untested-row models** | unknown | unknown | unknown | unknown | unknown | unknown | unknown |

`ptz-mode` values: `none` (no motors), `step` (discrete steps in device units), `continuous` (degrees-addressable). FR-14..17 use this to decide whether `--step` is interpreted as degrees or device-step-units (see §5.7).

The matrix is intentionally a v1 floor — it captures what HA-Tapo-Control's README and pytapo's per-model branches confirm. The "untested-row" entries SHALL fall through to a Phase 0 smoke-test result before being added to a real row.

### 3.4 WSDiscovery

For the WS-Discovery path, `tapo-cli` uses the **WSDiscovery** Python library (https://pypi.org/project/WSDiscovery/), latest stable **2.1.2** released **2025-01-24**, Python 3.9-3.13 compatible. The author flags incompleteness ("not 100% complete and correct").

**Multicast-drop is the dominant failure mode.** WS-Discovery transports over multicast UDP (group 239.255.255.250, port 3702). Most consumer mesh routers — Eero, Google Nest Wifi, TP-Link Deco, Asus AiMesh — drop or fail to forward multicast across mesh nodes by default. Wi-Fi client-isolation (often enabled on guest networks and IoT-segregated SSIDs) silently swallows it on a single AP too. Even on flat enterprise networks, multi-NIC hosts (Wi-Fi + Tailscale + Docker bridges) frequently send the probe out the wrong interface. **The empirical result is that WS-Discovery fails on more home networks than it succeeds on.**

For this reason `tapo-cli` does **NOT** treat WS-Discovery as the primary path. WS-Discovery and a subnet TCP/443 probe-scan are co-equal primaries, both run in parallel by default, deduplicated by MAC then IP. See §5.1 for the discovery contract and the `--no-scan` / `--ws-discovery-only` opt-out flags.

### 3.5 Other prior art (informative)

- **dickydoouk/tp-link-tapo-connect** — TypeScript/Node implementation of the Tapo control protocol. Reference for protocol details; not a runtime dependency.
- **peterstamps/TAPO-camera-ONVIF-RTSP-and-AI-Object-Recognition** — community ONVIF + RTSP demo against C225. Reference for ONVIF event subscription patterns (deferred to post-v1).
- **digitaltrails/onvifeye** — ONVIF Python client targeting C225/C125. Reference for the onvif-zeep-async usage pattern.

### 3.6 Why a thin custom CLI is justified

pytapo and onvif-zeep-async are libraries, not CLIs. The shell-friendly affordances `tapo-cli` adds — alias/group resolution, chmod-0600 dual-credential file handling, deterministic exit codes, JSON/JSONL output across all verbs, parallelized batch operations with structured failure reporting, the three-mechanism snapshot fallback, the `stream`-emits-URL convention — are not present in any existing tool. The wrapper does not duplicate protocol work; it adds a config-and-output layer over maintained libraries and an optional ffmpeg subprocess seam.

---

## 4. Architecture Decision: Wrap vs Reimplement

### 4.1 Decision

**Hybrid wrap.** Specifically:

- **Wrap pytapo 3.4.13** for the camera control plane (PTZ, presets, motion, alarm, LED, privacy, audio, night-vision, OSD, reboot, info)
- **Wrap onvif-zeep-async 4.0.4** for ONVIF WS-Discovery and snapshot-fallback
- **Wrap WSDiscovery 2.1.2** for the WS-Discovery transport (used by onvif-zeep-async or directly)
- **Subprocess ffmpeg** for the snapshot-of-last-resort and the `record` verb's recording path

Do not reimplement the Tapo HTTPS control protocol, the ONVIF SOAP envelope, or video codec handling.

### 4.2 Rationale

| Factor | Wrap pytapo + ONVIF + ffmpeg | Reimplement protocol(s) |
|--------|-------------------------------|--------------------------|
| Tapo HTTPS control plane | Free | ~800 lines + per-model variance |
| Tapo session/token derivation | Free, maintained | ~400 lines + ongoing churn |
| Camera-account vs cloud-account auth dance | Free | Re-derive every Tapo app revision |
| ONVIF SOAP envelopes | Free (via onvif-zeep-async) | Months of WSDL work |
| WS-Discovery multicast | Free (via WSDiscovery) | ~300 lines + Windows/macOS quirks |
| RTSP container + H.264 frame extraction | Free (via ffmpeg) | Years |
| Device family coverage | 30+ models, community-tested | Ship for 5, hope for the rest |
| Firmware churn response time | Upstream patches | We patch every regression |
| v1 ship time | Days-to-weeks | Months |
| Long-term maintenance burden | Track minor version bumps + ffmpeg version | Own a multi-protocol stack forever |

### 4.3 Why three libraries, not one

A naive read of "wrap pytapo" suggests a single-library wrapper. Reality:

- **pytapo's discovery is weak.** It expects you to already have the camera's IP. Real LAN discovery requires ONVIF or scanning.
- **pytapo's snapshot path is model-dependent.** Some models return a working JPEG via the control plane; others return a stream-token-only response that requires further work. ONVIF `GetSnapshotUri` is the broader-coverage fallback.
- **Recording requires ffmpeg regardless.** pytapo's own SD-card download example shells out to ffmpeg for the conversion step. There is no "pure pytapo" recording path.

Three libraries each doing what they're best at is the honest architecture. Reject single-stack purity.

### 4.4 Implementation language

**Recommendation: Python 3.11+ with `uv` for dependency and tool management.** Reasoning:

- All three primary libraries (pytapo, onvif-zeep-async, WSDiscovery) are Python; using them from Python is idiomatic and avoids a process-boundary tax on every command
- Matches `kasa-cli`'s stack — Dan can carry one mental model across both tools
- `uv tool install tapo-cli` gives single-command global install with isolated venv
- pytapo's package metadata claims Python 3.13 minimum; the CLI MAY relax this to 3.11 in `pyproject.toml` if pytapo runs cleanly on 3.11 in CI (verify in Phase 1). If pytapo genuinely requires 3.13 in 3.5+ releases, `tapo-cli` SHALL adopt the same minimum.

### 4.5 Considered alternative: reimplement in TypeScript

A Bun TypeScript wrapper that shells out to `pytapo` was considered for stack consistency with other Dan tools. Rejected because:

- Each invocation pays both Python startup AND a process-spawn round-trip — roughly 2x the latency floor
- Three libraries, not one, would each need shell-out plumbing
- ffmpeg is process-spawn either way; adding a Python wrapper layer multiplies the seam count
- Two languages to maintain for a single tool

If stack-uniformity becomes a requirement later, the same CLI surface can be re-fronted in Bun-TS shelling out to a Python `tapo-cli-rpc` daemon. That is a future concern.

### 4.6 Considered alternative: pure ONVIF (no pytapo)

A pure-ONVIF implementation (no pytapo dependency) was considered for vendor-neutrality. Rejected because:

- ONVIF coverage on Tapo is per-firmware and is gated behind a Tapo-app "Tapo Lab > Third-Party Compatibility" toggle on some models. The three-mechanism snapshot fallback (FR-11a) exists because pytapo's coverage of stills is the most-divergent surface across models — not because of a specific cited "varies" claim. (Operators wanting protocol-level evidence can consult TP-Link's third-party-compatibility FAQ entries and the community thread at community.tp-link.com regarding RTSP/ONVIF inconsistencies; specific URLs are intentionally omitted because they move and the citation would go stale.)
- Doorbells have minimal ONVIF support
- Tapo-specific features (alarm/siren, OSD, named night-vision modes, motion-event history, presets-by-name, audio TTS) are NOT exposed via ONVIF — they're proprietary Tapo control-plane verbs
- Going ONVIF-only would cut feature surface to the Profile-S subset (snapshot, RTSP URL, basic PTZ) and leave the rest unimplementable

ONVIF is in the stack as a **discovery primitive** and a **snapshot fallback**, not as the primary control surface.

---

## 5. Functional Requirements

Each FR is atomic and independently testable.

### 5.1 Discovery

`tapo-cli discover` runs **two co-equal primary paths in parallel** by default — WS-Discovery multicast and a subnet TCP/443 probe-scan — because consumer mesh routers and client-isolation silently drop multicast (see §3.4). Results from both paths are merged and deduplicated by MAC, falling back to IP when MAC is unavailable.

- **FR-1:** `tapo-cli discover` SHALL invoke **both** primary paths concurrently: (a) ONVIF WS-Discovery multicast probe on UDP `239.255.255.250:3702`, and (b) a TCP/443 HTTPS probe-scan across the local subnet (resolved from the default route or `--target-network <CIDR>`).
- **FR-1a:** Results from both paths SHALL be deduplicated by MAC (preferred key) then by IP (fallback). When the same device responds on both paths, the WS-Discovery record's ONVIF metadata SHALL be preserved and merged with the probe-scan record's reachability evidence.
- **FR-1b:** `--ws-discovery-only` SHALL run only the multicast path (legacy behavior; useful when scanning is forbidden by network policy).
- **FR-1c:** `--no-scan` is a synonym for `--ws-discovery-only`.
- **FR-1d:** `--scan-only` SHALL run only the TCP/443 probe-scan path (useful when multicast is known-broken on the host's network).
- **FR-2:** v1.0.0's `--scan` flag is **deprecated** but accepted for one release; using it SHALL emit a single deprecation warning on stderr and behave as the (now-default) co-primary scan path.
- **FR-3:** Discovery SHALL complete within `--timeout` seconds (default **30s**, sized for the probe-scan; WS-Discovery's internal probe deadline is hardcoded to 5s of that budget). Aggregation SHALL emit results as soon as both paths return or the budget elapses, whichever is sooner.
- **FR-4:** Discovery output SHALL include device IP, MAC, model (from ONVIF `GetDeviceInformation` or pytapo `getBasicInfo` when ONVIF is unavailable), hardware version, firmware version, the source path (`onvif` | `scan` | `both`), and a `supported` value (`true` | `false` | `untested`) per the §3.3 matrix.
- **FR-5:** Discovery SHALL be invokable with `--target-network <CIDR>` to constrain both paths to a specific subnet.
- **FR-5a:** Discovery completing within timeout with **zero responding devices** SHALL exit 0 with empty output (`[]` in `--json`/`--jsonl` modes; empty stdout in text mode) and emit a single INFO log line to stderr stating "timeout reached, 0 devices found." Exit code 3 (network error) SHALL be reserved for cases where both transports fail at the OS level (e.g., multicast bind error AND scan socket error).
- **FR-5b:** On multi-NIC hosts (Wi-Fi + Tailscale + Docker bridges), discovery MAY bind to the wrong interface. The CLI SHALL document this in `--help` for `discover`. If `--target-network <CIDR>` is supplied **and no local interface has an address inside that CIDR**, discovery SHALL exit code 6 (config error) with a message naming the available interface CIDRs and a hint to choose one.
- **FR-5c:** Multicast-drop is the **expected** failure mode on most home networks (see §3.4). The CLI SHALL not warn when WS-Discovery returns zero results so long as the scan path returned ≥1.

### 5.2 Listing

- **FR-6:** `tapo-cli list` SHALL print every alias defined in the local config file with its resolved IP/MAC. By default, list does **not** issue a per-device probe — output reflects config-resolved data only.
- **FR-6a:** `tapo-cli list --probe` SHALL additionally probe each device for liveness within `--timeout` and include an `online: bool` field.
- **FR-6b:** List output in `--json`/`--jsonl` mode SHALL be a JSON array of list-view objects: `{alias, ip, mac, model, online: bool|null}` where `online` is `null` if `--probe` was not specified.
- **FR-7:** `tapo-cli list --groups` SHALL print every group defined in config with its member alias list.
- **FR-8:** `tapo-cli list --online-only` SHALL imply `--probe` and filter the output to devices that responded.

### 5.3 Info

- **FR-9:** `tapo-cli info <target>` SHALL issue live calls against the device (pytapo `getBasicInfo` + capability probes) and print full state including model, firmware, MAC, alias, motion-detection state, privacy-mode state, LED state, current night-vision mode, ptz-capable, audio-capable, alarm-capable, and on-board features.
- **FR-10:** Info output in `--json` mode SHALL be a single JSON object matching the full Camera record per §10.1, with stable key names across firmware versions.
- **FR-10a:** `tapo-cli info <target>` against a model NOT on the v1 verified list SHALL still execute, but SHALL include `"supported": false` and emit a single WARN log line to stderr.

### 5.4 Snapshot

- **FR-11:** `tapo-cli snapshot <target> --output <path>` SHALL write a JPEG still image to `<path>`.
- **FR-11a:** Snapshot SHALL try mechanisms in this order, advancing on failure: (1) pytapo native snapshot path, (2) ONVIF `GetSnapshotUri` via onvif-zeep-async, (3) ffmpeg single-frame capture from the RTSP stream URL (`ffmpeg -y -i rtsp://... -frames:v 1 <path>`).
- **FR-11a.1: Tier-advance condition.** A mechanism is considered FAILED — and the next tier is attempted — on any of: (a) the per-mechanism budget (FR-11a.3) elapses without a complete response, (b) a non-200 HTTP response or a non-JPEG payload (verified by magic-byte sniffing the first 3 bytes for `FF D8 FF`), or (c) any unhandled exception OTHER THAN auth-rejection.
- **FR-11a.2: Auth short-circuit.** Auth-rejection at any tier — HTTP 401 from the snapshot endpoint, pytapo `_AUTH_FAILED` / `AuthError`, or RTSP 401 — SHALL NOT advance to the next tier. The snapshot verb SHALL exit code 2 immediately with a structured error naming the credential that was rejected. Rationale: if pytapo can't auth, ONVIF and RTSP are also using the wrong credential — burning their budgets re-failing is wasteful and noisy.
- **FR-11a.3: Budget split.** `--timeout <seconds>` is the **TOTAL wall-clock budget** across all three mechanisms (default 5s, raise it for slow networks). The default split is 40% pytapo, 30% ONVIF, 30% ffmpeg. Override per-mechanism via `--snapshot-budget pytapo=N,onvif=N,ffmpeg=N` where each `N` is seconds. The sum SHALL NOT exceed `--timeout`; if it does, the CLI SHALL exit 64 (usage error).
- **FR-11a.4: ffmpeg-missing is config not device.** If snapshot reaches tier 3 and `ffmpeg` is not on `PATH`, the CLI SHALL exit code **6 (config error)** — not code 1 (device) — with a structured error naming the missing dependency and a hint to install ffmpeg or run with `--snapshot-budget ffmpeg=0` to disable the tier.
- **FR-11b:** The mechanism that succeeded SHALL be reported in `--json` output as `{"mechanism": "pytapo"|"onvif"|"ffmpeg"}` and in `-v` mode as a stderr log line. This is observability, not contract — the CLI SHALL NOT promise a specific mechanism per model.
- **FR-11c:** All three mechanisms failing without auth-rejection SHALL exit code 1 (device error) with a structured error listing each attempt's failure reason and elapsed time.
- **FR-11d:** `--output -` SHALL write the JPEG bytes to stdout. In this mode, `--json` and `--jsonl` SHALL exit code 64 as mutually-exclusive. `--quiet` IS permitted alongside `--output -` because the JPEG bytes ARE the stdout payload — the no-stdout invariant of `--quiet` does not apply when stdout is the binary payload, but stderr-side suppression still applies.

### 5.5 Stream

- **FR-12:** `tapo-cli stream <target>` SHALL print a single RTSP URL on stdout in the form `rtsp://<camera-account-user>:<camera-account-pass>@<ip>:554/<stream-path>` where `<stream-path>` is resolved per FR-12b.
- **FR-12a:** `--quality sd` SHALL request the sub-stream (lower resolution); `--quality hd` is the default. The legacy `--protocol stream2` flag is **deprecated** but accepted for one release as a synonym for `--quality sd`.
- **FR-12b: ONVIF GetProfiles-driven resolver.** The stream-path resolution SHALL prefer the ONVIF `GetProfiles` response when available — match `--lens` and `--quality` to a profile by name (e.g., `mainStream` / `subStream` / `mainStream2` / `subStream2`) or by encoder configuration. If ONVIF is unavailable or returns no usable profile, the CLI SHALL fall back to the **defaults** in the truth table below. Both behaviors are documented and the ONVIF-resolved path SHALL be reported in `--json` output as `"resolver": "onvif"|"defaults"`.

  **Lens × quality default truth table** (used when ONVIF GetProfiles is unavailable):

  | lens | quality | stream path |
  |------|---------|-------------|
  | wide | hd | `/stream1` |
  | wide | sd | `/stream2` |
  | telephoto | hd | `/stream6` |
  | telephoto | sd | `/stream7` |

  These defaults are documented and overridable, not contractual. Operators with non-Tapo-firmware cameras or experimental builds SHALL use `--profile <name>` (FR-12b.1) to bypass the table entirely.
- **FR-12b.1: Profile override.** `--profile <name>` SHALL force a specific ONVIF profile by name and bypass both the truth table and the lens/quality flags.
- **FR-12b.2: Profile listing.** `--list-profiles` SHALL emit the ONVIF `GetProfiles` response as a JSON array of `{name, lens?, quality?, encoder, resolution}` records to stdout and exit 0. If ONVIF is unavailable on the target, it SHALL exit code 5.
- **FR-12c:** `--protocol hls` is **deferred to a future phase**. Tapo cameras do not natively serve HLS; constructing it requires an ffmpeg transcoder. v1 SHALL exit code 5 with a hint to use an external transcoder.
- **FR-12d:** `stream` SHALL NOT decode, transcode, or display video. It is a URL emitter.
- **FR-12e:** The camera-account credential used in the URL SHALL be resolved per §6.4. If no camera account is configured for the target, `stream` SHALL exit code 2 with a hint to set one in the Tapo app (Settings > Advanced settings > Camera account) and add `camera_account_file` for the device in config.
- **FR-12f: Credential-leak hardening.** `--credentials-via-env` SHALL emit the URL with credentials redacted to `<user>:<pass>` placeholders on stdout AND export `RTSP_USER` / `RTSP_PASS` environment variables for an exec'd child process. This avoids the credential ever appearing in shell history or process-list snapshots.
- **FR-12g: Exec shorthand.** `--exec <argv...>` SHALL replace `tapo-cli` with the named child process (via `execvp`), passing the constructed RTSP URL as the value of an `RTSP_URL` env var (or substituting it into a literal `{}` placeholder anywhere in `<argv>`). Combined with `--credentials-via-env` (FR-12f), the credential is exported to the child but never written to a shell-visible buffer. Example: `tapo-cli stream office-cam --credentials-via-env --exec ffmpeg -i '{}' -c copy out.mp4`.

### 5.6 Record (Subprocess)

- **FR-13:** `tapo-cli record <target> --output <path>` SHALL spawn `ffmpeg` as a foreground child process, configured to record the RTSP stream to `<path>` in MP4 container with `-c copy` (no transcoding by default). The verb name `record` already announces intent to write a file; the previous `--with-recording` opt-in flag has been removed in v1.1.
- **FR-13a: Footgun guard.** In **non-tty mode**, `record` SHALL require ONE of `--duration <seconds>` or `--max-bytes <N>` — open-ended recording without a cap from a script is a footgun and SHALL exit code 64 with a hint. In **tty mode** without either cap, the CLI SHALL prompt on stderr for confirmation ("Record indefinitely until Ctrl-C? [y/N] ") and abort on no/empty.
- **FR-13b:** The recording lives and dies with the CLI invocation. SIGINT (Ctrl-C) or SIGTERM on the `tapo-cli` process SHALL: (1) forward the signal to the ffmpeg child, (2) wait up to 5 seconds for ffmpeg to flush and finalize the MP4, (3) exit with the matching signal code (130 / 143).
- **FR-13c:** ffmpeg not on `PATH` SHALL exit code 6 (config error) with a hint to install ffmpeg.
- **FR-13d:** `--duration <seconds>` SHALL pass `-t <seconds>` to ffmpeg for fixed-length recordings.
- **FR-13d.1:** `--max-bytes <N>` SHALL pass `-fs <N>` to ffmpeg for size-capped recordings.
- **FR-13e:** `--quality`, `--lens`, `--profile`, and `--list-profiles` SHALL apply the same way as in `stream` (FR-12a, FR-12b, FR-12b.1, FR-12b.2).
- **FR-13f:** v1 SHALL NOT support segmented recording, motion-triggered recording, or NVR-style retention. `record` is one-shot. Use Frigate or Shinobi for those use cases.
- **FR-13g: Performance targets.** Start-to-first-frame SHALL be < **3000 ms p95** under §7.1 network assumptions. SIGINT-to-finalized-MP4 SHALL be < **2000 ms** for recordings under 1 GB (the 5s grace at FR-13b is the upper budget; 2s is the typical-case target).

### 5.7 PTZ

PTZ unit semantics are deterministic and depend on the target model's `ptz-mode` field in the §3.3.1 capability matrix.

- **FR-14:** `tapo-cli ptz <target> pan left|right [--step <N>]` SHALL move the camera horizontally. `--step <N>` SHALL be passed to pytapo as an **integer**. Interpretation:
  - On models with `ptz-mode: continuous` (the §3.3.1 matrix lists C225 as the v1 example), `<N>` is interpreted as **DEGREES**.
  - On models with `ptz-mode: step`, `<N>` is interpreted as **device-step-units** (camera-internal increments — typically a few degrees per unit, but not contractual).
  - Default `--step` value: 10 in either unit. Models with neither mode (`ptz-mode: none`) exit code 5 per FR-17a.
- **FR-15:** `tapo-cli ptz <target> tilt up|down [--step <N>]` SHALL move the camera vertically. Same `--step` semantics as FR-14.
- **FR-16:** `tapo-cli ptz <target> zoom in|out [--step <N>]` SHALL zoom the camera. **Zoom `--step` is ALWAYS device-step-units regardless of `ptz-mode`** — there is no documented degree-mapping for zoom. Models without zoom SHALL exit code 5.
- **FR-17:** `tapo-cli ptz <target> stop` SHALL halt any in-progress motion immediately.
- **FR-17a:** PTZ on non-PTZ-capable models (`ptz-mode: none` in the §3.3.1 matrix) SHALL exit code 5 with the message "model does not support PTZ" and the resolved model name.
- **FR-17b:** PTZ verbs SHALL be idempotent at the request level — issuing `pan left` twice in succession SHALL move twice (PTZ is a verb, not a state). This is the opposite of `on`/`off` idempotence.
- **FR-17c:** `--json` output for PTZ verbs SHALL include `{"step": N, "step_unit": "degrees"|"device-step-units"}` so callers can record what was actually issued without re-deriving from the model.

### 5.8 Presets

- **FR-18:** `tapo-cli preset <target> list` SHALL print all saved presets on the device in `{id, name}` form.
- **FR-19:** `tapo-cli preset <target> goto <name>` SHALL move the camera to the named preset. Unknown names SHALL exit code 4.
- **FR-20:** `tapo-cli preset <target> save <name>` SHALL save the current camera position as a preset under `<name>`. Existing names SHALL be overwritten with a warn log.
- **FR-21:** `tapo-cli preset <target> delete <name>` SHALL delete the named preset. Unknown names SHALL exit code 4.

### 5.9 Motion Detection

- **FR-22:** `tapo-cli motion <target> enable` SHALL enable on-camera motion detection.
- **FR-23:** `tapo-cli motion <target> disable` SHALL disable on-camera motion detection.
- **FR-24:** `tapo-cli motion <target> status` SHALL print enabled/disabled state plus configured sensitivity.
- **FR-25:** `tapo-cli motion <target> history [--since <RFC3339>] [--limit N]` SHALL emit recent motion events as JSON/JSONL. Default limit 50; default `--since` is 24h ago.
- **FR-25a:** Motion event payloads SHALL include `{ts, event_type, region, has_clip}`:
  - `ts` SHALL be **RFC 3339 UTC with the literal `Z` suffix** (e.g., `2026-04-28T08:14:02Z`). No local-time output. No fractional seconds unless the device reports them, in which case 3-digit milliseconds are preserved (e.g., `2026-04-28T08:14:02.412Z`).
  - `event_type` is one of `motion`, `person`, `vehicle`, `doorbell-press`, `unknown`.
  - `region` is the device-specific region label, often `full`.
  - `has_clip` reflects whether on-camera SD-card recording exists; downloading the clip is **out of scope for v1** (see §14).
- **FR-25b: `--since` parsing.** `--since` SHALL accept any RFC 3339 timestamp. If no offset is supplied (e.g., `2026-04-28T00:00:00`), the CLI SHALL assume UTC and emit a single INFO log line on stderr noting the assumption. Bare ISO 8601 dates without time (`2026-04-28`) SHALL be interpreted as `2026-04-28T00:00:00Z`.
- **FR-25c: Sort order.** Results SHALL be **sorted ascending by `ts`** before emission. This is contract — callers can rely on it for delta-poll loops.
- **FR-25d: Future `--since`.** A `--since` timestamp in the future (after the device's current clock) SHALL exit code 0 with empty output (`[]` in `--json`/`--jsonl` mode). It is not an error to ask "what happened after now" — the answer is "nothing yet."

### 5.10 Alarm

- **FR-26:** `tapo-cli alarm <target> enable` SHALL enable the camera's siren/alarm response.
- **FR-27:** `tapo-cli alarm <target> disable` SHALL disable it.
- **FR-28:** `tapo-cli alarm <target> trigger [--duration <seconds>]` SHALL manually fire the siren. Duration defaults to a model-specific value (typically 10s); models with no manual-trigger support SHALL exit code 5.
- **FR-29:** `tapo-cli alarm <target> status` SHALL print enabled/disabled and current-firing state.

### 5.11 LED, Privacy, Night Vision

- **FR-30:** `tapo-cli led <target> on|off|status` SHALL control the front status LED.
- **FR-31:** `tapo-cli privacy <target> enable|disable|status` SHALL control privacy mode (lens cover or feed disable). Models without privacy mode SHALL exit code 5.
- **FR-32:** `tapo-cli night-vision <target> auto|on|off|ir-only` SHALL set night-vision mode. Models that don't support a given mode SHALL exit code 5 with the supported set listed.

### 5.12 Audio

- **FR-33:** `tapo-cli audio <target> volume <0-100>` SHALL set speaker volume. Models without a speaker SHALL exit code 5.
- **FR-34:** `tapo-cli audio <target> mic mute|unmute` SHALL control microphone capture.
- **FR-35:** `tapo-cli audio <target> speaker mute|unmute` SHALL control speaker output.
- **FR-36:** `tapo-cli audio <target> tts --text "<message>"` SHALL play a text-to-speech message through the camera speaker on models that support it (e.g., C520WS). Unsupported models SHALL exit code 5.

### 5.13 OSD (On-Screen Display)

- **FR-37:** `tapo-cli osd <target> set --text "<s>" [--position bl|br|tl|tr] [--show-time true|false]` SHALL configure the on-screen display overlay. Position defaults to `bl` (bottom-left).
- **FR-37a: Length measurement.** The 32-character limit is measured in **Unicode codepoints**, not bytes. Inputs above 32 codepoints SHALL exit 64 (usage error). Codepoints below 32 SHALL be passed to the device as-is.
- **FR-37b: Codepoint compatibility.** Some camera firmware silently rejects non-ASCII codepoints on the device side. When the device returns an error response on a codepoint it cannot render, the CLI SHALL exit code 1 (device error) — not 64 — with a structured error indicating the device rejected the payload. This distinguishes "you sent too much" (CLI's fault, 64) from "device firmware can't render that glyph" (device's fault, 1).

### 5.14 Reboot and Set

- **FR-38:** `tapo-cli reboot <target>` SHALL reboot the camera. In tty mode it SHALL prompt for confirmation **on stderr** (so stdout JSON/text contracts are preserved when piped); in non-tty mode it SHALL exit 64 unless `--yes` is supplied. `--quiet` SHALL imply `--yes` (a quiet caller has signalled intent to proceed without prompts; the alternative — silently failing on missing `--yes` — is worse than a clear contract).
- **FR-39:** `tapo-cli set <target> --image-flip true|false` SHALL set image flip on supported models.
- **FR-39a:** `tapo-cli set <target> --timezone <IANA>` SHALL set the camera's timezone (e.g., `America/Toronto`).
- **FR-39b:** Other camera config knobs (HDR, noise cancelling, auto-track, recording-to-SD enable) are **deferred to v0.4+**. v1 ships only `--image-flip` and `--timezone`. v1.2.0 audit confirms these knobs have no current operator pressure and remain out-of-scope for Phase 4.
- **FR-39c: Phase 4a retro-fix.** v0.2.0 (Phase 2) shipped without the `set` verb despite FR-39 / FR-39a being on the Phase 2 acceptance list. v1.2.0 audit catalogued the gap; the retro-fix lands in **Phase 4a** alongside the fan-out generalization (§16.4). FR-39 / FR-39a / FR-39b prose stays exactly as written — Phase 4a is implementing them, not redefining them.

### 5.15 Groups

- **FR-40:** Groups SHALL be defined locally in the CLI config file's `[groups]` table, NOT on the devices themselves.
- **FR-41:** A group target (`@group-name` or `--group group-name`) SHALL resolve to its member aliases at command execution time.
- **FR-42:** Group operations SHALL execute device commands in parallel up to a configurable concurrency limit (default 5; per-command override via `--concurrency N`). Lower default than kasa-cli (10) because camera control ops are heavier.
- **FR-43:** Group operations SHALL report per-device success/failure individually; a single device failure SHALL NOT abort the group operation.
- **FR-43a:** Group exit code SHALL be:
  - **0** if every sub-operation succeeded
  - **7** (partial failure) if at least one sub-operation succeeded AND at least one failed
  - When **all** sub-operations failed, the exit code SHALL be the failure code of the sub-operation whose target appears first in the resolved alias list. The "resolved alias list" is the alias-config-file ordering of the group's members — **NOT** the execution-completion order. Rationale: deterministic exit codes matter more than reflecting which sub-op happened to lose its race; callers can pattern-match the first-target's error from JSONL output for diagnosis.
- **FR-43b:** v1 SHALL NOT support `groups add` / `groups remove` sub-verbs. `tapo-cli groups list` is the only group sub-verb in v1; mutations are by hand-editing the config.
- **FR-43c:** `record` and `stream` SHALL refuse group targets and exit 64. Recording or URL-emitting against multiple devices simultaneously is a footgun; require the user to spell out per-device invocations.
- **FR-43d: Fan-out generalization (Phase 4a).** Every state-control verb that takes a `<target>` argument SHALL honor `@group-name` syntax by expanding it to the group's resolved alias list and running per-target with bounded concurrency (FR-42), emitting one JSONL record per member in resolved-alias-list order, with FR-43a exit-code semantics. The set of verbs in scope for FR-43d is: `info`, `privacy`, `led`, `night-vision`, `motion enable|disable|status`, `motion history`, `alarm enable|disable|trigger|status`, `audio volume|mic|speaker|tts`, `osd set`, `preset list|goto|save|delete`, `reboot`, `set --image-flip`, `set --timezone`. The FR-43c carve-outs (`stream`, `record`) remain — they SHALL continue to exit 64 on `@group` syntax. v0.3.0 partially honored this contract — `ptz` fans out, every other in-scope verb stripped the leading `@` and treated the group as a single alias. v1.2.0 closes that gap.
- **FR-43e: `reboot @group` confirmation.** `reboot` against `@group-name` SHALL apply the FR-38 confirmation rules at the **group level**, not per device — one prompt for the entire group. In non-tty mode, `--yes` (or `--quiet` per FR-38) is required as before; the prompt text in tty mode SHALL name the group and enumerate the resolved member aliases on stderr before reading y/N. Confirmation applies once; the per-device sub-operations proceed without further prompts.
- **FR-43f: Mixed-feature `@group` fan-out.** When a group contains members whose models do not all support the requested verb (e.g., `audio tts @all-cams` with members lacking TTS), the unsupported members SHALL emit a per-target exit-5 result via the existing fan-out envelope (FR-43d) and the overall exit code SHALL be derived per FR-43a. The group operation SHALL NOT abort on the first unsupported member.

### 5.16 Batch

- **FR-44:** `tapo-cli batch --file <path>` SHALL read newline-delimited commands from a file and execute them, emitting one JSONL result per line on stdout.
- **FR-44a: JSONL per-line shape.** Each emitted line SHALL conform to:

  ```json
  {
    "command": "<verb-and-flags-string>",
    "target": "<resolved-alias-or-ip>",
    "status": "ok" | "error",
    "exit_code": <int>,
    "result": <verb's normal JSON payload, present iff status == "ok">,
    "error": {                            // present iff status == "error"
      "code": "<error-enum-from-§11.2>",
      "message": "<human-readable>",
      "hint": "<optional actionable hint>"
    }
  }
  ```

  `result` is the verb's normal `--json` payload on success (e.g., a Stream record for `stream`, a Camera record for `info`). `error` matches §11.2's structured error shape **minus** the `exit_code` wrapping (the per-line `exit_code` field replaces it). Tooling can `jq -c '. | select(.status=="error")'` deterministically.
- **FR-45:** `tapo-cli batch --stdin` SHALL accept the same format from stdin for shell-pipe composability.
- **FR-45a:** Batch exit code semantics SHALL match FR-43a (0 / 7 / first-failure-code).
- **FR-45b:** Empty-input batch SHALL exit 0 with no stdout output (`[]` in `--json` mode). Blank lines SHALL be skipped silently. Lines beginning with `#` SHALL be treated as comments.
- **FR-45c:** On SIGINT or SIGTERM during batch execution, the CLI SHALL: (1) cease dispatching new sub-operations, (2) wait up to 2 seconds for in-flight sub-operations to complete and have their results emitted, (3) emit a final JSONL summary line `{"event":"interrupted","completed":N,"pending":M}` to stdout, (4) flush the pytapo session cache for any successfully-authenticated device, (5) exit with code **130** (SIGINT) or **143** (SIGTERM).

### 5.17 Output Formats

- **FR-46:** Default output SHALL be human-readable text on a tty, JSONL otherwise. Specifically: `auto` mode SHALL emit JSONL whenever `isatty(stdout) == false`, **including file redirects** (e.g., `tapo-cli list > out.txt` writes JSONL, not text — redirected output is a machine consumer by definition). Pipes (`| cat`) and command substitutions (`$(...)`) are also JSONL.
- **FR-47:** `--json` SHALL force pretty JSON output regardless of tty detection.
- **FR-48:** `--jsonl` SHALL force one-JSON-per-line output regardless of tty detection.
- **FR-49:** `--quiet` SHALL suppress all stdout output; only the exit code communicates result.
- **FR-49a:** In `--json` and `--jsonl` modes, on **any** non-zero exit, stdout SHALL be valid parseable JSON or empty. The CLI SHALL never emit malformed JSON. For batch and group operations with mixed results, stdout JSONL SHALL contain one result object per attempted operation including those that failed (each with its own `error` field per §11.2). Stderr SHALL emit the structured summary error per §11.2 once.

### 5.18 Error Handling

- **FR-50:** Network errors SHALL exit with code 3 and emit a structured error object to stderr.
- **FR-51:** Authentication failures (cloud-account or camera-account) SHALL exit with code 2 and a credential-source hint that names which credential failed.
- **FR-52:** Unknown alias or unreachable IP SHALL exit with code 4.
- **FR-53:** Verbose mode (`-v`, `-vv`) SHALL emit progressively detailed JSON-structured logs to stderr; stdout SHALL remain clean.

### 5.19 Configuration Resolution

- **FR-54:** Config file resolution order: (1) `--config <path>` flag if present, (2) `TAPO_CLI_CONFIG` env var if set and non-empty, (3) `~/.config/tapo-cli/config.toml` if it exists.
- **FR-54a:** If `--config` or `TAPO_CLI_CONFIG` is set and the referenced file does not exist or cannot be read, the CLI SHALL exit code 6 (config error). Silent fallback is forbidden — explicit selection means strict.
- **FR-54b:** If only the default location is consulted and it does not exist, the CLI SHALL operate with built-in defaults and emit a single INFO log line on stderr ("no config file found, using defaults").
- **FR-54c:** `tapo-cli config show` SHALL print the effective resolved config (after all overrides) in TOML format. `tapo-cli config validate [<path>]` SHALL load and validate a config file and exit 0 / 6.
- **FR-54d: `config show` redaction.** Passwords in `config show` output SHALL be redacted to the literal string `***`. This applies to **both** the cloud-account credential (resolved from `[credentials] file_path` or `TAPO_PASSWORD`) and per-device camera-account credentials (resolved from `[devices.<alias>] camera_account_file`). The redaction is non-optional — there is no `--show-secrets` flag in v1.

### 5.20 Auth migration

- **FR-55: `auth migrate`.** `tapo-cli auth migrate` SHALL rewrite older versioned credential files in place to the current `version` schema. The verb SHALL refuse to run if any target file is not chmod 0600 (exit 2) and SHALL preserve the original file at `<path>.v<old>.bak`. This mirrors `kasa-cli`'s migration verb and exists for forward-compat as the credential schema evolves.

### 5.21 Fan-out execution contract (Phase 4a)

The `_fanout` helper module already exists from Phase 3 (`src/tapo_cli/verbs/_fanout.py`) and is invoked by `ptz`. Phase 4a generalizes its use across the verbs enumerated in FR-43d. This subsection codifies the per-verb integration points so the migration is mechanical, not negotiated.

- **FR-56: Per-verb fan-out integration obligation.** Each verb in FR-43d's enumeration SHALL detect a group target (leading `@` whose stripped value matches a configured group name in `[groups]`) and dispatch through `_fanout.run_fanout` instead of `_target.resolve`. Detection logic SHALL match `_fanout.is_group_target(target, cfg)` exactly — no verb-local re-implementation. The per-target coroutine passed to `run_fanout` SHALL be the verb's existing single-target work-function adapted to return `(exit_code, record_dict)` on success and raise `TapoCliError` subclasses on failure. Verbs SHALL NOT add per-verb concurrency overrides — `--concurrency` and `[defaults] concurrency` remain the only knobs.
- **FR-56a: Output mode parity.** Per-target JSONL records emitted by `run_fanout` SHALL conform to the existing FR-44a / B10 envelope: `{target, status, exit_code, result?, error?}`. `result` is the verb's normal `--json` payload on success; `error` is the §11.2 envelope minus the wrapping `exit_code`. Verbs SHALL NOT emit any other shape on group targets. Single-target invocations remain unaffected — they emit the verb's normal JSON shape.
- **FR-56b: Verbose log parity.** Per-target stderr `-v` / `-vv` log lines SHALL include the resolved alias as a `target` field so operators can correlate fan-out concurrent log lines back to specific cameras. The fan-out helper itself SHALL NOT log per-target progress lines on its own — that would double-log when the inner per-target coroutine is already verbose.

### 5.22 Events (Phase 4b)

Push-based event subscription — distinct from FR-25's pull-based `motion history`. Both surfaces emit the same `event_type` enum; they differ only in transport (ONVIF `PullPointSubscription` vs the periodic pytapo control-plane query) and lifecycle (`events --follow` is long-running; `motion history` is one-shot).

- **FR-57: `events <target>`.** `tapo-cli events <target>` SHALL subscribe to the camera's ONVIF Profile-S `PullPointSubscription` endpoint via onvif-zeep-async, pull pending events with `PullMessages`, project each into the §10.6 `Event` record, and emit the result on stdout in the operator's selected output mode. The default mode is JSONL on a non-tty (FR-46). Without `--follow`, the verb SHALL pull once with the default `Timeout=PT5S` and `MessageLimit=100`, emit any returned events, unsubscribe cleanly, and exit 0. Models without ONVIF Profile-S support SHALL exit code 5 with a hint pointing at §3.3.1 and the Tapo-app "Tapo Lab > Third-Party Compatibility" toggle.
- **FR-58: `--follow`.** `tapo-cli events <target> --follow` SHALL loop on `PullMessages` until SIGINT or SIGTERM. The pull cadence SHALL be `Timeout=PT30S` with `MessageLimit=100` per call; emitted events SHALL be flushed to stdout immediately on each pull return (line-buffered when stdout is a tty, otherwise unbuffered). On SIGINT or SIGTERM, the verb SHALL: (1) attempt a clean `Unsubscribe` with a hard 2-second budget, (2) emit a final summary line `{"event":"interrupted","subscription_age_s":<float>}` to stdout, (3) exit 130 (SIGINT) or 143 (SIGTERM).
- **FR-59: `--types <list>`.** Optional comma-separated event-type filter (`motion`, `person`, `vehicle`, `doorbell-press`, `unknown`). Events outside the filter SHALL be dropped silently. Default is no filter (all event types pass through).
- **FR-60: `--reconnect-after <seconds>`.** Optional cap (default 0 = disabled). When set and the subscription has been alive for ≥ N seconds, the verb SHALL `Unsubscribe`, sleep ≤200ms, and create a fresh subscription. Useful for cameras with broker-side TTL on PullPoint subscriptions. Re-subscriptions SHALL NOT be reflected in the JSONL stream (operator-visible behavior is a continuous event flow).
- **FR-61: Auto-reconnect on transport error.** If `PullMessages` raises a transport-layer error (HTTP 5xx, connection reset, SOAP fault other than "subscription terminated"), the verb SHALL retry with capped exponential backoff: 1s → 2s → 4s → 8s → 16s → 32s → 32s. Five consecutive failures SHALL exit code 3 (network) with a structured error naming the last failure. A retry that succeeds resets the counter to zero. Retries are silent at default verbosity; `-v` SHALL log each retry attempt as a single INFO line.
- **FR-62: Event envelope shape (§10.6).** Each emitted event SHALL conform to the §10.6 `Event` data model: `{ts, target, event_type, region?, has_clip, source: "onvif"}`. The `source` field is `"onvif"` to distinguish push-emitted events from `motion history`'s `"pytapo"` source. `ts` is RFC 3339 UTC `Z` per §7.2 — derived from the ONVIF `UtcTime` field on the message envelope. `has_clip` SHALL be derived heuristically (true iff the device's most recent SD-card recording timestamp is within ±5s of `ts`); `false` is the safe default if SD-card metadata is unobtainable.

### 5.23 Motion-clip download (Phase 4c, experimental)

- **FR-63: `motion download-clip <target>`.** `tapo-cli motion download-clip <target> --event-id <id> --output <path> --experimental-clips` SHALL fetch the on-camera SD-card video clip associated with a `motion history` event whose `has_clip: true`, write the MP4 to `<path>`, and exit 0. The `--experimental-clips` flag is REQUIRED — without it, the verb SHALL exit 64 with a hint pointing at this section's experimental status. The flag's purpose is to prevent operators from depending on this surface in long-lived scripts; every script that uses it must explicitly opt in to firmware-fragility risk.
- **FR-63a: `--event-id` resolution.** The `<id>` value SHALL be the device-side event identifier surfaced by `motion history` JSONL (a new `event_id` field added to §10.3 `MotionEvent` for Phase 4c — pytapo `getEvents()` already returns it). `motion history` output SHALL include `event_id` whenever the device exposes it. Unknown `event_id` SHALL exit 4 (not_found).
- **FR-64: Backing mechanism.** The clip-download path SHALL be the pytapo `experiments/DownloadRecordings.py` flow (`getRecordings()` → segment list → ffmpeg-assisted concat to MP4). The wrapper SHALL hide the ffmpeg conversion behind the verb — operators see one input (event id) and one output (MP4 file). `ffmpeg` not on PATH SHALL exit 6 (config), parity with `record` (FR-13c).
- **FR-64a: Models without SD-card.** Devices without an inserted SD card or with an event whose `has_clip: false` SHALL exit 4 (not_found) with a structured hint distinguishing the two cases.
- **FR-65: Output schema.** Output SHALL be `{target, event_id, output_path, bytes, duration_s, mechanism: "pytapo-experiments"}`. The `mechanism` field is a deliberate flag — observability lets operators detect when the pytapo upstream changes the experiments-folder API and regression-tests fail in a known shape.

---

## 6. Authentication and Credentials

### 6.1 The dual-credential reality

Tapo cameras require **two distinct credentials** for full functionality:

1. **Camera account ("third-party account")** — a per-device username and password (6-32 chars each) configured in the Tapo app under Settings > Advanced settings > Camera account. This is the **PRIMARY** credential for `tapo-cli` in v1.1+: it authenticates RTSP streaming, ONVIF connections, AND is pytapo's documented preferred path for the control plane (PTZ, motion, alarm, LED, etc.) on current Tapo firmware.
2. **TP-Link cloud account** — the email and password the user uses to log into the Tapo mobile app. This is a **FALLBACK** credential, kept for legacy firmware that does not honor camera-account login on the control plane (older C200/C210 / TC55-era builds). When pytapo's camera-account login fails with `_AUTH_FAILED` and a cloud account is configured, the CLI MAY retry with the cloud account, emitting a deprecation warning (FR-CRED-8.1).

Rationale for the inversion vs v1.0.0: pytapo itself has settled on camera-account-first for current firmware. The cloud-account path encourages users to put their Tapo-app password into local files unnecessarily — a per-device credential confined to one camera is a smaller blast radius. v1.0.0 ordered them backwards; v1.1 corrects that.

These are not interchangeable end-to-end. Streaming (RTSP) is camera-account-only. Control-plane is camera-account-preferred, cloud-account-fallback.

### 6.2 Credential sources (in resolution order, control plane)

For each device, the CLI SHALL resolve in this order until a credential is obtained or all sources are exhausted:

1. **Per-device camera account file** — `[devices.<alias>] camera_account_file` (PRIMARY).
2. **Per-device cloud-account override** — `[devices.<alias>] credential_file` (legacy-firmware fallback for this device).
3. **Default cloud-account credentials file** — `~/.config/kasa-cli/credentials` (shared with `kasa-cli` — same JSON v1 format, same TP-Link cloud account; tapo-cli reads but never writes this file). A tapo-only override `~/.config/tapo-cli/credentials` is honored if it exists and takes precedence over the shared kasa file (FR-CRED-3.1).
4. **Environment variables** — `TAPO_USERNAME` and `TAPO_PASSWORD` (cloud account).
5. **No credentials configured** — the CLI exits code 2 with a hint pointing at the Tapo-app camera-account menu and `[devices.<alias>] camera_account_file` config.

Notes:
- The `--credential-source <env|file|none>` flag (§6.7) overrides this resolution order, scoping which sources are even consulted.
- **Partial environment-variable fall-through** — if exactly ONE of `TAPO_USERNAME` / `TAPO_PASSWORD` is set and the other is empty, the env-var source SHALL be treated as "not set" and the resolver SHALL fall through to the next source. Verbose mode (`-v`) SHALL log this partial set as a single WARN line on stderr — half-set env vars are almost always a misconfiguration, but they should not block other sources.
- For RTSP-using verbs (`stream`, `record`), the resolver SHALL stop at step 1 — only the camera-account file is used. If step 1 yields no result, those verbs exit code 2 with the camera-account hint per FR-CRED-7.

### 6.3 Credentials file format (cloud account)

- **FR-CRED-1:** The default credentials file SHALL be JSON with a top-level `version` integer (currently `1`) plus the keys appropriate for that version. v1 keys: `username` (TP-Link cloud email), `password`. Unknown additional keys SHALL cause a config-validation error and exit code 6. Missing `version` SHALL be treated as v1 with a single deprecation warning on stderr.
- **FR-CRED-2:** The CLI SHALL refuse to load a credentials file whose mode is more permissive than 0600 and SHALL exit with code 2 and an actionable error showing the current mode.
- **FR-CRED-3:** A missing credentials file SHALL fall through to the next source (env vars) without warning. Verbose mode (`-v`) SHALL log the fall-through with the path that was tried.
- **FR-CRED-3.1: Shared cloud-account file with kasa-cli.** The default cloud-account credentials file path SHALL be `~/.config/kasa-cli/credentials`. Rationale: TP-Link cloud accounts are issued per user, not per device family, so the same email + password authenticates both Kasa and Tapo control planes. The CLI SHALL prefer a tapo-only override at `~/.config/tapo-cli/credentials` when present (e.g., for users who maintain a separate Tapo-only TP-Link account); when both files exist, the tapo-only file wins and the shared file is not consulted. `tapo-cli` SHALL NEVER write to `~/.config/kasa-cli/credentials` — that file is owned by `kasa-cli` and read-only from this CLI's perspective. The shared file SHALL be subject to the same chmod-0600 enforcement as any other credentials file (FR-CRED-2). `tapo-cli auth migrate` (FR-55) SHALL only act on `~/.config/tapo-cli/credentials`, never on the shared file.

### 6.4 Camera-account file format (per-device)

- **FR-CRED-4:** Camera accounts SHALL live in **separate** files, one per device, referenced from `[devices.<alias>] camera_account_file = "<path>"` in the config.
- **FR-CRED-5:** The camera-account file SHALL be JSON: `{"version": 1, "username": "<6-32 chars>", "password": "<6-32 chars>"}`, chmod 0600.
- **FR-CRED-6:** The CLI SHALL never embed cleartext camera-account credentials into stderr logs. RTSP URLs printed by `stream` and constructed for `record` ARE the only place credentials appear in output, and `stream` SHALL emit them to stdout (not stderr).
- **FR-CRED-7:** Verbs that require the camera account (`stream`, `record`) SHALL exit code 2 with an actionable hint if no `camera_account_file` is configured for the target. The hint SHALL name the Tapo-app menu path (Settings > Advanced settings > Camera account).
- **FR-CRED-8:** Control-plane verbs (PTZ, motion, alarm, LED, privacy, night-vision, audio, OSD, info, reboot) SHALL attempt the **camera-account credential first** (per §6.2 step 1). On `_AUTH_FAILED` from pytapo's camera-account path, the CLI SHALL fall back to the cloud-account credential if one is configured (per §6.2 steps 2-4) and retry once.
- **FR-CRED-8.1: Deprecation warning on cloud fallback.** When the cloud-account fallback fires (i.e., the camera-account login failed and the cloud-account login succeeded), the CLI SHALL emit a single WARN line on stderr per device per CLI invocation: `"WARN: cloud-account fallback used for <alias>; camera-account login is the supported path on current firmware. See §6.1."`

### 6.5 pytapo session caching

- **FR-CRED-9:** Successful pytapo control-plane auth SHALL persist the session state to `~/.config/tapo-cli/.tokens/<device-mac>.json` with chmod 0600. The `.tokens/` directory SHALL be created with chmod 0700 on first use. The cached payload is treated as an **opaque pytapo state blob** — `tapo-cli` does not parse its internals — and SHALL include a top-level `pytapo_version` field naming the pytapo library version that produced it. On read, if `pytapo_version` mismatches the currently-installed pytapo, the cache entry SHALL be invalidated and a fresh handshake performed (one INFO line on stderr).
- **FR-CRED-10:** Subsequent commands against the same device SHALL deserialize the cached state into pytapo before issuing requests. If pytapo signals session expiration, the CLI SHALL re-auth and update the cache.
- **FR-CRED-11:** A 401-equivalent or pytapo `_AUTH_FAILED` response during a command SHALL invalidate the cached state and trigger a single retry with a fresh handshake. Two consecutive auth failures SHALL exit code 2. The cache SHALL also be invalidated when `--credential-source` (§6.7) selects a different source than the one that wrote the cache (the source name is recorded in the cache blob).
- **FR-CRED-12:** `tapo-cli auth flush` SHALL delete all cached state files. `tapo-cli auth flush --target <alias>` SHALL delete only that device's state file.
- **FR-CRED-13:** Concurrent `tapo-cli` invocations targeting the same device SHALL serialize on a per-device advisory lock (`flock` on the token state file). Cache writes SHALL use atomic file replacement (`write tmpfile + fsync + rename`). Lock acquisition SHALL time out after `--timeout` seconds (default 5); a timeout SHALL exit code **3** (network/contention) — not 2 (auth) — with a structured error naming the holding PID if the OS exposes it (Linux: read `/proc/locks`; macOS: best-effort via `lsof`, omit the field if unobtainable).

### 6.6 `auth status`

- **FR-CRED-14:** `tapo-cli auth status` SHALL emit, per cached state file: device alias (resolved from config; `<unmapped>` if no alias matches the MAC), MAC, cache file path, mtime, file size in bytes, expires_at (RFC 3339 string or `null` if unknown), cloud-account configured (bool), camera-account configured (bool, per-device). `--json` mode emits a JSON array. The CLI SHALL NOT issue liveness probes against cached devices in `auth status` — that is `list --probe`'s job.

### 6.7 `--credential-source` flag

- **FR-CRED-15: Source override.** The `--credential-source <env|file|none>` flag SHALL constrain which credential sources from §6.2 are consulted for the duration of the invocation:
  - `env` — only `TAPO_USERNAME` / `TAPO_PASSWORD` (skip per-device camera-account files, skip default credentials file). Useful in CI / containerized contexts.
  - `file` — only file-based sources (per-device camera-account file, per-device cloud-account override, default credentials file). Skip env vars. Useful when env vars are present but stale.
  - `none` — skip all sources; commands requiring credentials SHALL exit code 2. Useful for verifying that a cached pytapo session works without re-auth (paired with `auth status`).
- The flag overrides the default §6.2 resolution order; it does not relax the chmod-0600 enforcement (FR-CRED-2) or the partial-env-fallthrough rule (§6.2 note) — those are integrity invariants, not source choices.
- When unspecified, the default §6.2 resolution order applies.

### 6.8 pytapo authentication variants (informative)

pytapo handles three authentication mechanisms across the Tapo firmware fleet:

1. **Legacy POST + cookie** — older firmware (pre-2023, common on C100/C200/TC55-era builds). Returns a session cookie; pytapo persists it in the state blob.
2. **KLAP-style** — TP-Link's KLAP handshake protocol introduced in mid-2023 firmware on C2xx and Cxx0 series. Uses a per-session derived key; pytapo persists handshake state.
3. **Encrypted / SSE login** — current (2024+) firmware on C225 and other newly-released models, using server-sent-events for an authenticated push channel and a different handshake.

`tapo-cli` does **NOT** branch on these variants — the cache schema (FR-CRED-9) is an opaque pytapo state blob, and library-version-keyed invalidation handles forward-compat. This subsection documents what the underlying library is doing so that future contributors understand why cache schemas appear to change without our intervention.

### 6.9 `config show` redaction (informative)

Per FR-54d, passwords in `config show` output are redacted to `***`. There is no mechanism in v1 to display the cleartext credential — to inspect a credential file, read it directly with `cat` (the file is chmod 0600 and visible only to its owner). This keeps screenshots and pasted-output from leaking secrets.

---

## 7. Non-Functional Requirements

### 7.1 Performance

Targets assume a wired LAN or 5GHz Wi-Fi with <50ms RTT to the camera. Mesh Wi-Fi or distant access points are not contractual.

| Metric | Target |
|--------|--------|
| Discovery (WS-Discovery) | < 5 seconds with default timeout |
| Discovery (`--scan` /24 subnet) | < 30 seconds with default timeout |
| Single-camera control command (cached pytapo session) | < 800ms p95 |
| Single-camera control command (cold pytapo handshake) | < 3000ms p95 |
| `snapshot` via pytapo native | < 2000ms p95 |
| `snapshot` via ONVIF fallback | < 3500ms p95 |
| `snapshot` via ffmpeg-from-RTSP fallback | < 5000ms p95 |
| `stream` URL emission (no I/O) | < 200ms |
| Cold CLI startup (no command, just `--help`) | < 250ms |

### 7.2 Determinism

- Commands SHALL be idempotent where physically possible (privacy enable/enable/enable is the same as one). PTZ verbs are explicitly NOT idempotent (FR-17b).
- Identical input SHALL produce identical output structure (JSON key set is stable).
- No interactive prompts when stdin/stdout are not ttys (except `reboot` which requires `--yes` in non-tty mode per FR-38; `--quiet` implies `--yes` for `reboot`).
- **All emitted timestamps SHALL be RFC 3339 in UTC with the literal `Z` suffix.** No local time, no offsets other than `Z`. This applies to `motion history`, `auth status`, `Camera.last_seen`, JSONL `result.*` payloads, and any future timestamp field.
- **Multi-record output SHALL be sorted deterministically.** Default sort is by `target` ascending in the resolved-config order (the order aliases appear in the config file's `[devices.*]` blocks), with ties broken by event timestamp ascending. For motion history the only sort is timestamp ascending (FR-25c) since it is single-target.
- **Numeric fields SHALL be JSON numbers**, not strings — durations, counts, byte sizes, exit codes, step values, etc. The only stringly-typed numeric is when a device returns a non-finite or non-numeric token; in that case the field SHALL be `null` and an INFO line on stderr SHALL note the device's raw response.

### 7.3 Observability

- `-v` enables INFO-level structured logs to stderr
- `-vv` enables DEBUG-level logs including raw protocol envelopes (with credentials redacted)
- All log lines in verbose mode are single-line JSON
- Optional file logging: when `[logging] file = "<path>"` is set in config, JSON log lines SHALL be tee'd there (append, line-buffered)
- File logging SHALL NOT rotate in v1

### 7.4 Portability

- Supported platforms: macOS 13+ (Apple Silicon and Intel), Linux x86_64 and arm64
- Python 3.11+ required (subject to pytapo's actual minimum at install time — see §4.4)
- ffmpeg required on `PATH` for snapshot tier-3 fallback and the `record` verb
- No Windows support in v1

### 7.5 Network model

- All `tapo-cli` operations SHALL be fully local-network. The CLI SHALL NOT make outbound connections to TP-Link servers under any code path.
- DNS unreachable SHALL NOT block any operation.
- A camera unreachable on the LAN SHALL exit code 3 (network error) — not code 2 (auth) — even if the failure mode is a TLS handshake reset.

---

## 8. CLI Surface

### 8.1 Verb summary

| Verb | Purpose |
|------|---------|
| `discover` | WS-Discovery + optional subnet-scan fallback |
| `list` | Print configured aliases and groups |
| `info` | Show full state of one camera |
| `snapshot` | Pull a JPEG still to a file (three-mechanism fallback) |
| `stream` | Print an RTSP URL on stdout |
| `record` | Spawn ffmpeg subprocess for one-shot RTSP recording (requires `--duration` or `--max-bytes` in non-tty mode; FR-13a) |
| `ptz` | Pan/tilt/zoom and stop sub-verbs |
| `preset` | List / goto / save / delete saved positions |
| `motion` | Motion-detection enable / disable / status / history; `motion download-clip` (Phase 4c, behind `--experimental-clips`) |
| `events` | Push-based ONVIF event subscription; `events <target> [--follow] [--types ...]` (Phase 4b) |
| `alarm` | Siren enable / disable / trigger / status |
| `led` | Status LED on / off / status |
| `privacy` | Privacy mode enable / disable / status |
| `night-vision` | auto / on / off / ir-only |
| `audio` | volume / mic / speaker / tts |
| `osd` | On-screen-display configuration |
| `set` | Image flip, timezone (FR-39 / FR-39a; Phase 4a retro-fix per FR-39c — slipped from Phase 2) |
| `reboot` | Reboot camera (interactive confirm or `--yes`) |
| `groups` | List local group definitions |
| `batch` | Execute commands from file or stdin |
| `config` | `show` (effective config) and `validate` |
| `auth` | Session-cache management (`flush`, `status`) |

### 8.2 Target syntax

A target is one of:
- An **alias** defined in config (e.g., `front-door`)
- An **IP address** (e.g., `192.168.1.42`)
- A **MAC address** (e.g., `AA:BB:CC:DD:EE:FF`)
- A **group name** prefixed with `@` (e.g., `@perimeter-cams`)
- The literal `all` to target every alias in config

Group targets are forbidden for `stream` and `record` per FR-43c.

### 8.3 Common flags

| Flag | Meaning |
|------|---------|
| `--json` | Pretty JSON output |
| `--jsonl` | Newline-delimited JSON output |
| `--quiet` | Suppress stdout (does not apply to `snapshot --output -` where JPEG bytes ARE the stdout payload — see FR-11d) |
| `--timeout <seconds>` | Per-operation timeout, default 5 (per-verb defaults override; see §7.1). For `snapshot` this is the **total** budget across all three mechanisms (FR-11a.3). |
| `--config <path>` | Use a non-default config file |
| `--credential-source <env\|file\|none>` | Constrain credential sources for this invocation (FR-CRED-15). Overrides §6.2 resolution order. |
| `--concurrency N` | Override `[defaults] concurrency` for this invocation only |
| `--probe` | On `list`, additionally probe each device for liveness |
| `--online-only` | On `list`, imply `--probe` and filter to devices that responded (FR-8) |
| `--target-network <CIDR>` | Constrain `discover` to a specific subnet (exits 6 if no local interface matches; FR-5b) |
| `-v`, `-vv` | Verbose / very verbose stderr logging |
| `--yes` | Bypass interactive confirmation (currently only `reboot`; `--quiet` implies `--yes`) |

### 8.4 Worked examples

```text
# Discover everything ONVIF-capable on the LAN
$ tapo-cli discover
front-door     192.168.1.42   AA:BB:CC:DD:EE:01  D230     true
backyard       192.168.1.51   AA:BB:CC:DD:EE:02  C320WS   true
office-cam     192.168.1.78   AA:BB:CC:DD:EE:03  C225     true

# JSON form
$ tapo-cli discover --json
[
  { "ip": "192.168.1.42", "mac": "...", "model": "D230", "supported": true, "firmware": "..." },
  ...
]

# Pull a still from a configured alias
$ tapo-cli snapshot front-door --output /tmp/door.jpg
$ tapo-cli snapshot front-door --output /tmp/door.jpg --json
{"target":"front-door","output":"/tmp/door.jpg","mechanism":"pytapo","bytes":48201}

# Get the RTSP URL — pipe to ffmpeg or mpv
$ tapo-cli stream office-cam
rtsp://camuser:campass@192.168.1.78:554/stream1

$ tapo-cli stream office-cam --lens telephoto
rtsp://camuser:campass@192.168.1.78:554/stream6

$ tapo-cli stream office-cam | xargs mpv

# One-shot 30s recording via ffmpeg subprocess
$ tapo-cli record office-cam --duration 30 --output /tmp/cam.mp4

# Stream URL with credentials redacted, exec'd into ffmpeg
$ tapo-cli stream office-cam --credentials-via-env --exec ffmpeg -i '{}' -c copy /tmp/cam.mp4

# Move the camera and save a preset
$ tapo-cli ptz office-cam pan left --step 15
$ tapo-cli ptz office-cam tilt up --step 5
$ tapo-cli preset office-cam save desk-view

# Recall a preset
$ tapo-cli preset office-cam goto desk-view

# Motion detection
$ tapo-cli motion office-cam enable
$ tapo-cli motion office-cam history --since '2026-04-28T00:00:00Z' --jsonl
{"ts":"2026-04-28T08:14:02Z","event_type":"motion","region":"full","has_clip":true}
{"ts":"2026-04-28T09:31:55Z","event_type":"motion","region":"full","has_clip":true}

# Trigger the siren for 5 seconds
$ tapo-cli alarm office-cam trigger --duration 5

# Privacy mode on (lens cover engages on supported models)
$ tapo-cli privacy office-cam enable

# Set night vision to ir-only
$ tapo-cli night-vision office-cam ir-only

# OSD overlay
$ tapo-cli osd office-cam set --text "FRONT DOOR" --position bl --show-time true

# Audio TTS through the speaker (C520WS)
$ tapo-cli audio backyard tts --text "The package has arrived"

# Inspect cached pytapo sessions
$ tapo-cli auth status --json
[
  {"alias":"office-cam","mac":"AA:BB:CC:DD:EE:03","cache_path":"/Users/dan/.config/tapo-cli/.tokens/AABBCCDDEE03.json","mtime":"2026-04-28T18:02:14Z","expires_at":"2026-05-28T18:02:14Z","bytes_size":412,"pytapo_version":"3.4.13","cloud_account":true,"camera_account":true},
  {"alias":"front-door","mac":"AA:BB:CC:DD:EE:01","cache_path":"/Users/dan/.config/tapo-cli/.tokens/AABBCCDDEE01.json","mtime":"2026-04-28T17:42:11Z","expires_at":null,"bytes_size":412,"pytapo_version":"3.4.13","cloud_account":true,"camera_account":true}
]

# Flush all sessions
$ tapo-cli auth flush

# Show effective config
$ tapo-cli config show

# Lint a candidate config
$ tapo-cli config validate /tmp/new-config.toml

# Run a list of commands from a file
$ cat night.batch
privacy front-door enable
privacy office-cam enable
night-vision backyard ir-only
$ tapo-cli batch --file night.batch --jsonl

# Phase 4b: subscribe to push events (long-running)
$ tapo-cli events front-door --follow --types motion,doorbell-press --jsonl
{"ts":"2026-04-29T14:02:11Z","target":"front-door","event_type":"doorbell-press","has_clip":true,"region":"full","source":"onvif"}
{"ts":"2026-04-29T14:02:18Z","target":"front-door","event_type":"motion","has_clip":true,"region":"full","source":"onvif"}
^C
{"event":"interrupted","subscription_age_s":47.3}

# Phase 4c (experimental): download a clip referenced by motion history
$ tapo-cli motion history front-door --since 1h --jsonl | jq -r 'select(.has_clip).event_id' | head -1
ev_2026042914021100
$ tapo-cli motion download-clip front-door --event-id ev_2026042914021100 --output /tmp/door.mp4 --experimental-clips
{"target":"front-door","event_id":"ev_2026042914021100","output_path":"/tmp/door.mp4","bytes":4823104,"duration_s":12.3,"mechanism":"pytapo-experiments"}

# Phase 4a: fan-out across a group with mixed-feature handling (FR-43d, FR-43f)
$ tapo-cli privacy @perimeter-cams enable --jsonl
{"target":"front-door","status":"ok","exit_code":0,"result":{"target":"front-door","privacy_enabled":true}}
{"target":"backyard","status":"ok","exit_code":0,"result":{"target":"backyard","privacy_enabled":true}}
```

---

## 9. Configuration File

### 9.1 Location and format

Default path: `~/.config/tapo-cli/config.toml` (override via `--config` or `TAPO_CLI_CONFIG`).

Format: TOML.

### 9.2 Schema

| Section | Field | Type | Default | Purpose |
|---------|-------|------|---------|---------|
| `[defaults]` | `timeout_seconds` | int | 5 | Per-operation timeout |
| `[defaults]` | `concurrency` | int | 5 | Max parallel device ops in groups/batch |
| `[defaults]` | `output_format` | string | `auto` | `auto`/`text`/`json`/`jsonl` |
| `[credentials]` | `file_path` | string | `~/.config/kasa-cli/credentials` | Default cloud-account credentials (shared with kasa-cli; FR-CRED-3.1). Set to `~/.config/tapo-cli/credentials` to use a tapo-only file. |
| `[ffmpeg]` | `path` | string | `ffmpeg` (resolved on `PATH`) | Override ffmpeg binary path |
| `[logging]` | `file` | string | — | Optional file path for JSON log tee |
| `[devices.<alias>]` | `ip` | string | — | Static IP (skips discovery) |
| `[devices.<alias>]` | `mac` | string | — | MAC for stable identification |
| `[devices.<alias>]` | `model` | string | — | Optional; verifies against probe |
| `[devices.<alias>]` | `credential_file` | string | — | Per-device cloud-account override |
| `[devices.<alias>]` | `camera_account_file` | string | — | Per-device camera-account file (REQUIRED for `stream`/`record`) |
| `[groups]` | `<name>` | string[] | — | Array of alias names |

### 9.3 Complete example

```toml
# ~/.config/tapo-cli/config.toml

[defaults]
timeout_seconds = 5
concurrency = 5
output_format = "auto"

[credentials]
# Default is ~/.config/kasa-cli/credentials (shared with kasa-cli, same TP-Link
# cloud account, same JSON v1 format). Override here only if you maintain a
# separate Tapo-only cloud account; tapo-cli will then ignore the shared file.
file_path = "~/.config/kasa-cli/credentials"

[ffmpeg]
# Optional. Default is whatever is on PATH.
# path = "/opt/homebrew/bin/ffmpeg"

[logging]
# Optional.
# file = "~/.local/state/tapo-cli/log"

[devices.front-door]
ip = "192.168.1.42"
mac = "AA:BB:CC:DD:EE:01"
model = "D230"
camera_account_file = "~/.config/tapo-cli/camera-accounts/front-door.json"

[devices.backyard]
ip = "192.168.1.51"
mac = "AA:BB:CC:DD:EE:02"
model = "C320WS"
camera_account_file = "~/.config/tapo-cli/camera-accounts/backyard.json"

[devices.office-cam]
ip = "192.168.1.78"
mac = "AA:BB:CC:DD:EE:03"
model = "C225"
camera_account_file = "~/.config/tapo-cli/camera-accounts/office-cam.json"

[groups]
perimeter-cams = ["front-door", "backyard"]
all-cams       = ["front-door", "backyard", "office-cam"]
```

### 9.4 Config validation

`tapo-cli config validate` SHALL parse the file, resolve every alias-to-device reference, resolve every group-to-alias reference, verify referenced credential and camera-account files exist with chmod 0600, and exit 0 only if all checks pass.

---

## 10. Data Model

### 10.1 Camera

```text
Camera {
  alias              : string         # human-friendly name (config-resolved or device-stored)
  ip                 : string         # IPv4 dotted quad
  mac                : string         # uppercase colon-separated
  model              : string         # e.g., "C225", "D230"
  hardware_version   : string
  firmware_version   : string
  supported          : bool           # true if model is on the v1 verified list (§3.3)
  features           : string[]       # subset of ["ptz", "zoom", "audio", "tts", "alarm", "led", "privacy", "ir", "dual-lens", "doorbell"]
  motion_enabled     : bool
  privacy_enabled    : bool
  led_state          : "on" | "off"
  night_vision_mode  : "auto" | "on" | "off" | "ir-only" | "unknown"
  has_camera_account : bool           # config-derived (camera_account_file is set and readable)
  last_seen          : RFC3339 string  # UTC, 'Z' suffix
}
```

### 10.2 Stream

```text
Stream {
  target    : string         # alias
  protocol  : "rtsp"
  url       : string         # rtsp://user:pass@ip:port/streamN
  lens      : "wide" | "telephoto"
  quality   : "hd" | "sd"    # stream1/6 = hd, stream2/7 = sd
}
```

### 10.3 MotionEvent

```text
MotionEvent {
  ts          : RFC3339 string  # UTC, 'Z' suffix (FR-25a, §7.2)
  alias       : string
  event_type  : "motion" | "person" | "vehicle" | "doorbell-press" | "unknown"
  region      : string?         # device-specific region label, often "full"
  has_clip    : bool             # whether on-camera SD-card clip exists
  duration_s  : number?          # event duration if reported by device
  event_id    : string?          # device-side event id; present when surfaced by pytapo getEvents (Phase 4c, FR-63a)
}
```

### 10.4 Preset

```text
Preset {
  id     : int                # device-assigned
  name   : string
  pan    : number?            # degrees, if reported
  tilt   : number?            # degrees, if reported
  zoom   : number?            # device units, if reported
}
```

### 10.5 SessionMetadata (cache)

```text
SessionMetadata {
  alias              : string?         # null if MAC has no alias mapping
  mac                : string
  cache_path         : string          # absolute path to the .tokens/<mac>.json file
  mtime              : RFC3339 string  # cache file modification time, UTC 'Z'
  expires_at         : RFC3339 string? # null if the underlying pytapo state doesn't expose an expiry
  bytes_size         : int             # cache file size in bytes (rendered as "bytes_size" in JSON; v1.2.0 audit reconciled prose with shipped code)
  pytapo_version     : string          # pytapo version that wrote the cache (FR-CRED-9)
  cloud_account      : bool
  camera_account     : bool
}
```

### 10.6 Event (Phase 4b)

```text
Event {
  ts          : RFC3339 string  # UTC 'Z'; derived from ONVIF UtcTime on the message envelope (FR-62)
  target      : string          # alias resolved from the verb invocation
  event_type  : "motion" | "person" | "vehicle" | "doorbell-press" | "unknown"
  has_clip    : bool            # heuristic: true iff SD-card recording exists within ±5s of ts (FR-62)
  region      : string?         # device-specific region label, often "full"; null if not surfaced via ONVIF
  source      : "onvif"         # constant; distinguishes push (events) from pull (motion history, source="pytapo")
}
```

The `Event` shape is **identical** to the `MotionEvent` shape (§10.3) modulo the `source` field — the same `event_type` enum, the same `ts` semantics, the same `has_clip` semantics. Operators MAY merge JSONL streams from `motion history` and `events --follow` and dedupe on `(target, ts, event_type)` if they want a single unified event log. v1.2.0 commits to keeping the two shapes in lockstep — any future addition to the §10.3 enum SHALL also extend §10.6 and vice versa.

---

## 11. Error Model and Exit Codes

### 11.1 Exit code table

| Code | Meaning | When |
|------|---------|------|
| 0 | Success | Operation completed; for batch/group, **every** sub-op succeeded |
| 1 | Device error | Camera returned an error response (non-auth, non-network); all three snapshot mechanisms failed without auth-rejection (FR-11c); device firmware rejects an OSD codepoint (FR-37b) |
| 2 | Authentication error | Cloud-account or camera-account auth failed; missing credentials when no other source is configured; credentials file chmod-mode too permissive (FR-CRED-2); no `camera_account_file` for an RTSP-using verb (FR-CRED-7); auth-rejection at any snapshot tier (FR-11a.2) |
| 3 | Network error | Timeout, connection refused, no route, multicast bind failure, camera unreachable on LAN; concurrent-lock acquisition timeout (FR-CRED-13) |
| 4 | Device not found | Alias unknown in config, IP unreachable, MAC not on LAN, unknown preset name |
| 5 | Unsupported feature | Verb/flag combo not supported by target model or firmware (e.g., PTZ on a non-PTZ camera per the §3.3.1 matrix, `tts` on a model without speaker, HLS protocol, ONVIF GetProfiles on a model without ONVIF) |
| 6 | Config error | Config file missing when `--config`/`TAPO_CLI_CONFIG` was set; invalid TOML; unresolvable references; unknown keys; ffmpeg not on PATH **including the snapshot tier-3 fallback case** (FR-11a.4); `--target-network <CIDR>` with no matching local interface (FR-5b) |
| 7 | Partial batch/group failure | ≥1 sub-op succeeded AND ≥1 sub-op failed |
| 64 | Usage error | Invalid CLI invocation: missing required arg, mutually-exclusive flags, group target on `stream`/`record`, `record` in non-tty without `--duration`/`--max-bytes` (FR-13a), `osd` text >32 codepoints (FR-37a), `reboot` non-tty without `--yes`, `--snapshot-budget` sum > `--timeout` (FR-11a.3), `--output -` with `--json`/`--jsonl` (FR-11d) |
| 130 | SIGINT | Ctrl-C during execution; partial JSONL stream emitted with trailing `{"event":"interrupted",...}` line; ffmpeg child (if any) gets forwarded SIGINT |
| 143 | SIGTERM | Same partial-result + interrupted-line behavior as 130 |

**Disambiguation notes (v1.1):**

- **Missing credentials** (no source configured at all) → exit **2** (auth, not config). Rationale: the user has not configured how to authenticate; this is an auth-domain error even though a config field is involved.
- **Credentials file chmod violation** (file exists but mode is more permissive than 0600) → exit **2** (auth). It is a credential-source integrity failure, not a config-syntax failure.
- **Snapshot reaches tier 3 and ffmpeg is missing on `PATH`** → exit **6** (config, not device). The CLI cannot complete a request because a documented dependency is absent — name it explicitly. Distinct from "device returned an error" which is exit 1.
- **`--target-network <CIDR>` with no matching local interface** → exit **6** (config). Naming the available interface CIDRs in the error message is mandatory (FR-5b).

### 11.2 Structured error object (stderr)

```json
{
  "error": "auth_failed",
  "exit_code": 2,
  "target": "front-door",
  "credential": "camera_account",
  "message": "RTSP auth rejected; check camera_account_file",
  "hint": "Create a camera account in the Tapo app: Settings > Advanced settings > Camera account, then update camera_account_file in config"
}
```

The `error` enum is closed and stable. Tooling MAY pattern-match on it. The `credential` field appears only on auth errors and names which credential failed (`cloud_account` | `camera_account`).

---

## 12. Testing Strategy

### 12.1 Unit tests

- Mock pytapo implementations covering: outdoor non-PTZ + siren (C320WS), outdoor TC-series (TC85), indoor non-PTZ (C100), indoor PTZ (C200), dual-lens with telephoto path (C225 in BOTH wide-only AND telephoto modes), and a wired doorbell (D230). For parity with kasa-cli's per-family mocks, each fixture set SHALL exercise its full §3.3.1 capability row.
- Mock onvif-zeep-async responses for WS-Discovery and `GetSnapshotUri`
- Mock ffmpeg subprocess invocation (use `subprocess.run` patching) for snapshot-fallback and recording paths
- Config parser tests with valid configs, invalid TOML, dangling alias refs, dangling group refs, missing `camera_account_file`, missing `version` in credentials file
- Output formatter tests asserting JSON key stability across mock cameras, including: list-view subset (FR-6b), full Camera record (FR-10), MotionEvent records, Preset records, Stream records, structured error (§11.2)
- Exit-code matrix tests: every exit code 0/1/2/3/4/5/6/7/64/130/143 SHALL be reachable by at least one test
- Snapshot fallback test: mock pytapo failure → mock ONVIF success → assert `mechanism: "onvif"` in output
- Snapshot fallback test: mock pytapo failure → mock ONVIF failure → mock ffmpeg success → assert `mechanism: "ffmpeg"`
- Snapshot fallback test: all three fail → assert exit code 1 with structured error listing each
- Concurrency lock test (FR-CRED-13): two concurrent `tapo-cli` invocations against the same camera serialize on flock
- Credential resolver tests covering all three sources for cloud account AND the per-device camera-account file path
- Signal handling test: SIGINT during a 10-element batch SHALL produce ≤10 result lines plus the `{"event":"interrupted",...}` line and exit 130
- Signal handling test: SIGINT during `record` SHALL forward SIGINT to ffmpeg child and wait up to 5s for finalization
- Group target rejected by `stream`/`record` (FR-43c) SHALL exit 64

### 12.2 Integration tests

Gated on environment variable `TAPO_TEST_DEVICE_IP`. When unset, integration tests are skipped (CI default). When set, the test suite runs against a real camera on the operator's LAN. CI never sets this variable.

A second variable `TAPO_TEST_DOORBELL_IP` enables doorbell-specific integration tests for users who own one. A third variable `TAPO_TEST_PTZ_DEVICE_IP` enables PTZ-specific tests.

### 12.3 Fixture corpus

Capture real device responses (with MACs, IPs, and credentials redacted) in `tests/fixtures/` to reproduce protocol-level edge cases without requiring hardware. ONVIF SOAP envelopes captured against a real C225 should suffice for most tests.

---

## 13. Distribution and Install

### 13.1 Recommended

```text
uv tool install git+ssh://git@github.com/agileguy/tapo-cli
```

Rationale: this is a personal tool. Installing directly from the git repo via `uv tool` keeps the install pattern Dan already uses for `kasa-cli`, `ghost-cli`, `pypi-cli`, `resend-cli`, etc. (isolated venv, entry point on PATH, no system-Python conflicts). Updates are `uv tool upgrade tapo-cli`.

### 13.2 Alternatives considered

- **Publish to PyPI** — rejected for v1. Personal scope.
- `pipx install git+...` — works identically but Dan's stack guidance prefers `uv`.
- `brew install tapo-cli` — would require maintaining a Homebrew tap; not warranted for a single-user tool.
- Single-file binary via PyInstaller — increases binary size dramatically and complicates ffmpeg subprocess handling; not recommended.

### 13.3 Versioning

Tag releases as `vX.Y.Z` in git. `uv tool install git+ssh://...@vX.Y.Z` pins to a tag. The `pyproject.toml` SHALL pin `pytapo` to a known-good git SHA (not a floating `>=` constraint) due to the HA 2025.11 / pytapo 3.3.51 incident; rolling forward requires an explicit `tapo-cli` release. Example pyproject syntax: `pytapo @ git+https://github.com/JurajNyiri/pytapo@<40-char-sha>`.

---

## 14. Out of Scope

The following are **explicitly excluded** from v1:

- **Live video display in the terminal.** Use `mpv`, `ffplay`, or `vlc` downstream of `tapo-cli stream`.
- **Motion-event video clip downloads (default-OFF).** Vendor-specific binary protocol; pytapo has experimental support but it's brittle. v1.2.0 reclassifies this from "low priority not impossible" to **opt-in via Phase 4c** behind a mandatory `--experimental-clips` flag (FR-63..65). Without the flag, `motion download-clip` exits 64 with a hint pointing at the experimental status. Default-OFF is deliberate: the protocol breaks on firmware changes more than once per year.
- **Two-way real-time audio.** Would require WebRTC or SIP. Out of scope at all phases of this SRD. `audio tts` is one-shot send only.
- **Battery-doorbell third-party integration.** Tapo D210 and D235 in battery mode cannot accept a camera account (verified against HA-Tapo-Control discussions #739 and #794). Wired/always-on operation only.
- ~~**Doorbell event-stream subscription.**~~ Reclassified in v1.2.0: a long-running ONVIF `PullPointSubscription` push verb (`tapo-cli events <target> --follow`) is **committed for Phase 4b** (FR-57..62). The cron + `motion history --since` idiom remains supported for callers who don't want a long-running process. Doorbell-press events surface in BOTH paths — the same `event_type: "doorbell-press"` token (FR-25a) appears in `motion history` JSONL and in `events --follow` JSONL.
- **Kasa plug/bulb/strip/switch support.** That's `kasa-cli`'s lane.
- **Matter and Thread devices.** Tapo cameras don't speak either.
- **Cloud relay control.** Local LAN only.
- **NVR / DVR functionality.** Long-running multi-day recording, retention policies, motion-triggered DVR — wrong tool. Use Frigate, Shinobi, Synology Surveillance Station, etc., downstream of `tapo-cli stream`.
- **HLS protocol output.** Tapo cameras don't natively serve HLS. `--protocol hls` SHALL exit code 5 in v1; users wanting HLS should run an ffmpeg transcoder.
- **Automation rules engine.** "If motion at front-door, turn on porch-light" belongs in Home Assistant or a cron job that pipes `motion history` into `kasa-cli`.
- **GUI dashboard.** This is a CLI.
- **Firmware updates.** Use the Tapo app.
- **Wi-Fi provisioning.** Use the Tapo app.
- **Multi-network discovery via mDNS reflectors.** Single-LAN broadcast/scan only.
- **Group config mutation.** v1 `groups` sub-verb is `list` only. Add/remove are deferred.
- **Comment-preserving TOML round-trip on config writes.** v1 does not write user config files.
- **Camera-account creation from the CLI.** **Deferred — no documented endpoint exists; pytapo does not expose it.** The Tapo app creates camera accounts via an undocumented HTTPS verb; reverse-engineering it would put the CLI in the firmware-coupled-API position this whole project was built to avoid. v1.2.0 audit (2026-04-29) re-confirmed this: a focused web search across pytapo, HA-Tapo-Control, and tapo-rest turned up no public API for creating a camera account programmatically — the Tapo-app menu (Settings > Advanced settings > Camera account) remains the only reliable path. The flow is rare enough (once per device, ever) that the manual step is acceptable. A future SRD may revisit if TP-Link publishes an official endpoint or pytapo's mainline adopts a stable wrapper.
- **Continuous segmented recording.** The `record` verb is one-shot. No `--segment 1h` flag.
- **PTZ patrol / auto-track scheduling.** Per-device feature, not a CLI feature.
- **Image-quality tuning beyond `--image-flip`.** HDR, noise cancelling, sharpness, exposure — not in v1's `set` verb. Phase 2+ candidates if requested.

---

## 15. Resolved Decisions

The architectural decisions surfaced during research and design are recorded here for traceability.

| # | Decision area | Outcome |
|---|---------------|---------|
| 1 | **Library strategy** | Hybrid wrap. pytapo for control, onvif-zeep-async for discovery + snapshot fallback, ffmpeg subprocess for snapshot-of-last-resort and recording. Reject single-stack reimplementation. |
| 2 | **Implementation language** | Python 3.11+ with `uv`. Matches `kasa-cli`. pytapo claims 3.13 minimum; verify in Phase 1 CI and adjust `pyproject.toml` minimum if needed. |
| 3 | **Stream verb behavior** | Emit RTSP URL on stdout. Do not decode, transcode, or display video. Recording is a separate verb (`record`) with mandatory `--duration`/`--max-bytes` cap in non-tty mode (FR-13a). |
| 4 | **Snapshot mechanism** | Three-mechanism fallback chain: pytapo → ONVIF `GetSnapshotUri` → ffmpeg single-frame from RTSP. Emit `mechanism` in `--json` output for observability. |
| 5 | **Discovery primary** | ONVIF WS-Discovery via `WSDiscovery` library. Subnet HTTPS-probe scan as `--scan` fallback. Manual config always wins. |
| 6 | **Credential model** | Dual-credential. Cloud account default is `~/.config/kasa-cli/credentials` — shared with `kasa-cli`, same TP-Link cloud login, same v1 JSON format, read-only from this CLI (FR-CRED-3.1). Tapo-only override at `~/.config/tapo-cli/credentials` if a separate Tapo cloud account is desired. Camera account is per-device-mandatory in `[devices.<alias>] camera_account_file`. All credential files chmod 0600. |
| 7 | **Battery-doorbell third-party support** | Out of scope. Wired/always-on doorbells only. D210/D235 in battery mode cannot accept a camera account. |
| 8 | **Doorbell / motion event subscription** | v0.3.0 surfaces doorbell-press through `motion history --event-type doorbell-press` (poll-based). Long-running push subscription via ONVIF Profile-S `PullPointSubscription` is **committed for Phase 4b** as the `events --follow` verb (FR-57..62). |
| 9 | **Recording boundary** | One-shot only. ffmpeg as foreground child process. Lives and dies with the CLI invocation. SIGINT/SIGTERM forward to ffmpeg with 5s grace for MP4 finalization. |
| 10 | **Stream/record group rejection** | `stream` and `record` SHALL refuse group targets and exit 64. Per-device invocation only. |
| 11 | **HLS support** | Deferred. v1 `--protocol hls` exits 5. Tapo cameras don't natively serve HLS; transcoding belongs in an external ffmpeg invocation. |
| 12 | **Motion-clip download** | v0.3.0 emits `has_clip: true` in `motion history` but does not act on it. v1.2.0 commits to a **Phase 4c experimental** `motion download-clip` verb (FR-63..65) gated behind `--experimental-clips`. Backed by pytapo's `experiments/DownloadRecordings.py` (upstream-experimental); brittle by design. Operators opt in. |
| 13 | **Two-way realtime audio** | Out of scope at all phases. `audio tts` is one-shot send only. |
| 14 | **pytapo version pinning** | Pin to a git SHA in `pyproject.toml`, not `>=3.4.13`. HA 2025.11 incident proves single-maintainer libs need explicit pinning. Roll forward requires a `tapo-cli` release. |
| 15 | **Distribution** | `uv tool install git+ssh://git@github.com/agileguy/tapo-cli`. Not published to PyPI. |
| 16 | **Concurrency default** | 5 parallel ops (lower than kasa-cli's 10) because camera control is heavier. Override per-command with `--concurrency N`. |
| 17 | **Token cache location** | `~/.config/tapo-cli/.tokens/` (co-located with config). Mirrors `kasa-cli` Decision 4. |
| 18 | **Windows support** | Out of scope. macOS 13+ and Linux x86_64/arm64 only. WSL is the answer for Windows users. |
| 19 | **Camera-account creation** | Out of scope. v1 consumes camera accounts; Tapo-app operation creates them. No `tapo-cli auth setup-camera-account` verb. v1.2.0 audit (2026-04-29) re-confirmed: no documented endpoint, no pytapo coverage; reverse-engineering would re-introduce the firmware-fragility this CLI was designed to avoid. |
| 20 | **OSD text length** | Capped at 32 characters. Exits 64 if exceeded. (Conservative — actual device limit varies; 32 is the cross-model floor.) |

---

## 16. Phase Plan

### 16.0 Phase 0 — Hardware Smoke-Test Gate (3 days)

**Deliverable:** Empirical proof that the chosen pytapo SHA actually works against every camera the operator owns, plus a captured fixture corpus to feed §12.3.

This phase ships **no** CLI code — only a smoke-test script and a pytapo SHA pin. Phases 1-3 do not start until Phase 0 passes. This de-risks every subsequent phase: if the chosen pytapo can't auth or can't snapshot on a real device, every later phase is built on sand.

Tasks:
- Pin pytapo to a specific git SHA in a pre-MVP `pyproject.toml` skeleton.
- Write a smoke-test script (`scripts/smoke.py`) that, given a list of `(ip, camera-account-user, camera-account-pass)` tuples, exercises: pytapo `getBasicInfo`, pytapo `getStreamURL`, pytapo native snapshot (capture bytes), ONVIF `GetDeviceInformation`, ONVIF `GetProfiles`, ONVIF `GetSnapshotUri`, RTSP single-frame via ffmpeg.
- Run `scripts/smoke.py` against **every camera the operator owns** — Dan's deployment baseline plus any borrowed test units. Capture pass/fail per mechanism per device.
- Record protocol-level fixtures (`tests/fixtures/<model>-<firmware>.json` and `.xml` for ONVIF SOAP envelopes) for replay in Phase 1+ unit tests.
- Document any model that fails a tier — that model's row in the §3.3.1 capability matrix is updated, OR the model is moved to the "untested" row pending firmware-specific investigation.

**Exit criteria for Phase 0** (none of Phases 1-3 starts before all of these are met):

- [ ] Every owned camera passes pytapo `getBasicInfo` with the pinned SHA.
- [ ] At least one snapshot mechanism succeeds per camera.
- [ ] Captured ONVIF GetProfiles + GetSnapshotUri responses for ≥3 distinct models.
- [ ] Smoke-test script committed to the repo.
- [ ] §3.3.1 matrix updated with any deltas observed against §3.3 expected behavior.

If a camera fails Phase 0, the question is "investigate and update the matrix" not "ship anyway and let it fail in production."

### 16.1 Phase 1 — MVP (2-3 weeks)

**Deliverable:** Discover, list, info, snapshot, stream, basic state control (privacy, LED, night-vision, motion enable/disable/status), and reboot for the v1 verified-model set.

- Project skeleton, `uv` packaging, entry point
- Config loader (TOML), `config show`, `config validate` (FR-54, FR-54a/b/c)
- pytapo wrapper layer with alias-to-device resolution
- onvif-zeep-async wrapper for WS-Discovery
- WSDiscovery wrapper for the multicast transport
- ffmpeg subprocess seam for snapshot-fallback (record verb deferred to Phase 3)
- Verbs: `discover`, `list` (with `--probe`), `info`, `snapshot` (three-mechanism fallback), `stream` (URL emission), `privacy`, `led`, `night-vision`, `motion enable`, `motion disable`, `motion status`, `reboot`
- Cloud-account credential source: env var and file with versioned-format support (FR-CRED-1..3); default file is the shared `~/.config/kasa-cli/credentials` per FR-CRED-3.1 (read-only from tapo-cli)
- Per-device camera-account file resolution (FR-CRED-4..7)
- pytapo session caching to `~/.config/tapo-cli/.tokens/` (FR-CRED-9..12)
- Per-device session-cache locking (FR-CRED-13)
- Output: text (default), `--json`, structured-error contract (FR-49a, §11.2)
- Exit codes 0, 1, 2, 3, 4, 5, 6, 64, 130
- Discovery zero-result handling (FR-5a) and multi-NIC `--target-network` (FR-5, FR-5b)
- Unit tests with mock pytapo, mock ONVIF, mock ffmpeg subprocess

### 16.2 Phase 2 — PTZ, Presets, Alarm, Audio, OSD (shipped 2026-04-29 in v0.2.0)

**Deliverable:** Full camera control surface beyond Phase 1's state toggles.

- Verbs: `ptz` (pan/tilt/zoom/stop with `--step`), `preset` (list/goto/save/delete)
- Verbs: `alarm` (enable/disable/trigger/status)
- Verbs: `audio` (volume, mic mute/unmute, speaker mute/unmute, tts on supported models)
- Verb: `osd set` with text/position/show-time
- ~~Verb: `set --image-flip` and `set --timezone`~~ — **slipped from Phase 2; retro-fix in Phase 4a per v1.2.0 audit (§16.4, §17 v1.2.0).** FR-39/39a/39b prose stays as written.
- `auth status`, `auth flush` sub-verbs (FR-CRED-12, FR-CRED-14)
- Optional file logging via `[logging] file` (§7.3)
- Exit code 5 paths exercised (PTZ on non-PTZ, tts on no-speaker, etc.)

### 16.3 Phase 3 — Recording, Motion History, Groups, Batch, SIGTERM (2 weeks)

**Deliverable:** ffmpeg-subprocess recording, motion history retrieval, group/batch fan-out.

- Verb: `record` (FR-13..13g) with mandatory `--duration`/`--max-bytes` cap in non-tty mode and SIGINT/SIGTERM forwarding to ffmpeg
- Verb: `motion history --since --limit` (FR-25, FR-25a)
- `groups list` sub-verb (mutations remain manual TOML edits — FR-43b)
- `@group` and `--group` target syntax resolution
- Parallel execution with concurrency cap (FR-42) + per-command `--concurrency`
- `batch` verb reading from `--file` and `--stdin`; comments and blank lines (FR-45b)
- `--jsonl` output format finalized; mixed-result JSON-validity contract (FR-49a)
- Exit code 7 for mixed-result batch/group failures (FR-43a, FR-45a)
- SIGINT/SIGTERM handling with `{"event":"interrupted",...}` summary line (FR-45c)
- `stream`/`record` group-target rejection (FR-43c) verified
- Per-device-result reporting in JSON

### 16.4 Phase 4 — Fan-out, push events, experimental clips (3-4 weeks total)

**Deliverable:** Three sub-phases that close real Phase 0-3 gaps surfaced by the v1.2.0 audit and add the highest-value of the deferred-from-v1 candidates.

Phase 4 is sub-phased on the same a/b/c/d cadence as Phase 1 — each sub-phase ships as one PR against `main` with its own reviewer pass. 4a is a retro-fix of two Phase 0-3 misses; 4b is the largest functional addition; 4c is opt-in experimental.

#### 16.4.1 Phase 4a — Fan-out generalization + `set` retro-fix (1 PR, ~1 week)

**Why:** v0.3.0 introduced `_fanout.run_fanout` for `ptz` only; the other state-control verbs strip the leading `@` and treat `@group-name` as a single alias — silently violating FR-41 / FR-43. Separately, the `set` verb (FR-39 / FR-39a) was on the Phase 2 acceptance list but slipped — `tapo-cli set` exits 64 today.

**Scope (FR-43d, FR-43e, FR-43f, FR-56, FR-56a, FR-56b, FR-39c):**

- Migrate every verb in FR-43d's enumeration to dispatch through `_fanout.run_fanout` when `is_group_target(target, cfg)` returns true. The 1-2-line shim per verb is the same shape as `ptz_cmd.py` already uses.
- Implement `set --image-flip <true|false>` and `set --timezone <IANA>` per FR-39 / FR-39a. The verb body is a thin pytapo wrapper: `setLensFlip` and `setTimezone` accept the model's accepted enum values (verify against the Phase 0 fixture for C200 — others read-only-tested).
- Add `set` to `cli.py`'s `add_command` registry alongside the existing verbs.
- `reboot @group-name` SHALL apply group-level confirmation (FR-43e) — one prompt naming the resolved member list, not per-camera.
- Update `--help` text on every fan-out-enabled verb to mention `@group` syntax.

**Test fixtures (`tests/`):**

- `tests/test_fanout_generalization.py` — for each verb in FR-43d, two tests: `<verb>_fans_out_via_resolved_alias_list` and `<verb>_emits_b10_per_target_envelope`. The mock corpus already has the §3.3.1 capability matrix; add a multi-camera `[groups]` fixture.
- `tests/test_set_cmd.py` — image-flip true/false, timezone happy-path, timezone with bad-IANA exit code 64, model-without-image-flip exit code 5.
- `tests/test_reboot_group_confirm.py` — tty-mode group prompt, non-tty without `--yes` exit 64, non-tty with `--yes` proceeds.

**Hardware acceptance against live Tapo C200:**

- [ ] `tapo-cli privacy @one-cam-group enable` → exit 0, JSONL line emitted, camera lens cover engages
- [ ] `tapo-cli set @one-cam-group --image-flip true` → exit 0, fan-out envelope per device
- [ ] `tapo-cli set front-door --timezone America/Toronto` → exit 0, `tapo-cli info front-door --json` reflects new tz
- [ ] `tapo-cli reboot @perimeter-cams --yes` → one stderr confirmation line, two cameras reboot

**Risks:**

- **Mixed-feature groups regress UX.** Mitigation: FR-43f explicitly handles per-target exit-5 within the fan-out envelope.
- **`reboot @group` confirmation pattern unfamiliar.** Mitigation: FR-43e prose; integration test pins the prompt copy.
- **`set` verb on a doorbell.** Mitigation: §3.3.1 capability gate; tts-style exit-5 hint.

#### 16.4.2 Phase 4b — `events --follow` push subscription (1 PR, ~2 weeks)

**Why:** Doorbells and front-door cameras want push, not poll. The `motion history` cron-poll idiom adds 30-90s of latency per event and quadruples camera control-plane load on high-cadence cameras. ONVIF Profile-S `PullPointSubscription` is the right primitive; community exemplars (`peterstamps/TAPO-camera-ONVIF-RTSP-and-AI-Object-Recognition`, `pablo-zarate/Tapo-C200-event-listener`, `digitaltrails/onvifeye`) confirm it works on C200, C225, and C125.

**Scope (FR-57..62, §10.6):**

- New verb module `src/tapo_cli/verbs/events_cmd.py`. Wired into `cli.py`.
- Backed by onvif-zeep-async — reuse the existing `media._resolve_onvif_wsdl_dir()` helper from Phase 1c.
- Profile-S `CreatePullPointSubscription` → loop `PullMessages` → project to §10.6 `Event` → emit JSONL.
- `--follow` long-running mode with FR-58 cadence and FR-61 backoff.
- `--types` filter (FR-59), `--reconnect-after` cap (FR-60).
- SIGINT/SIGTERM handling: clean `Unsubscribe` within 2s, summary line, exit 130/143 (parity with `batch` FR-45c, `record` FR-13b).
- `has_clip` heuristic: query pytapo `getRecordings()` lazily on first event of a `--follow` session, cache the most-recent-recording timestamp, refresh every 60s.
- `--profile <name>` flag NOT included — events use a different ONVIF surface than streams; no profile selection needed.

**Test fixtures (`tests/`):**

- `tests/test_events_cmd.py` — 12+ tests:
  - mock onvif `CreatePullPointSubscription` returns subscription manager
  - mock `PullMessages` returns 0/1/N events
  - `--types` filter drops unmatched events
  - `--follow` SIGINT triggers `Unsubscribe` within 2s
  - 5xx on PullMessages triggers backoff, 6th failure → exit 3
  - `--reconnect-after 60` triggers re-subscribe at the boundary
  - non-ONVIF model → exit 5 with capability hint
  - `event_type` classification covers all five enum values
  - JSON envelope has `source: "onvif"`
- `tests/fixtures/onvif-pullpoint-*.xml` — captured ONVIF SOAP envelopes for `CreatePullPointSubscription` and `PullMessages` against a C200.

**Hardware acceptance against live Tapo C200:**

- [ ] `tapo-cli events front-door` (no `--follow`) → exit 0 within 5s, zero or more JSONL events emitted
- [ ] `tapo-cli events front-door --follow &` then physically trigger motion → at least one motion event line on stdout within 5s of the trigger
- [ ] `kill -TERM` the running follower → final `{"event":"interrupted",...}` line, exit 143, ONVIF Unsubscribe verified via tcpdump
- [ ] Camera-side reboot mid-follow → backoff retries, subscription re-created on the 1s/2s/4s ladder, no events lost from the post-reboot window
- [ ] `tapo-cli events @perimeter-cams --follow` → REJECTED with exit 64 (events `--follow` is per-device by design — group fan-out is meaningless for a long-running verb that pins to one subscription per stdout)

**Risks:**

- **ONVIF Profile-S not enabled on the camera.** Mitigation: FR-57 exit-5 path with hint pointing at the Tapo-app "Tapo Lab > Third-Party Compatibility" toggle. Phase 0 smoke runs `GetProfiles` so we already know per-camera support before Phase 4b ships.
- **PullPoint TTL kicks in mid-session.** Mitigation: FR-60 `--reconnect-after` plus FR-61 transport-error retry handle both clean termination and broker-side eviction.
- **onvif-zeep-async 4.0.4 PullPointSubscription quirks.** Risk: the library's PullPoint support is documented but lightly tested upstream (the maintainer's own examples favor short-poll discovery). Mitigation: the Phase 4b PR SHALL include a captured tcpdump of a successful CreatePullPointSubscription + PullMessages + Unsubscribe sequence in `tests/fixtures/onvif/` so future regressions can be replayed.
- **Group target on `--follow` is meaningless.** Mitigation: explicit exit-64 carve-out (matching `stream` / `record` FR-43c posture). Single-pull mode (no `--follow`) MAY accept `@group` and fan out — left as a Phase 4b stretch goal.

#### 16.4.3 Phase 4c — Motion-clip download (experimental) (1 PR, ~1 week)

**Why:** Operators with `motion history` JSONL pipelines that filter on `has_clip: true` cannot act on the result today. The clip download primitive completes the loop. The risk profile is fundamentally different from Phase 4a/4b — pytapo's clip-download path is upstream-experimental (`experiments/DownloadRecordings.py`, not mainline `pytapo/__init__.py`), and the underlying camera HTTPS endpoints have been observed to break across firmware revisions. v1.2.0 commits to shipping it behind a mandatory `--experimental-clips` flag and an honest upstream-fragility warning, not as a default-on feature.

**Scope (FR-63..65, §10.3 `event_id` addition):**

- New verb sub-command `motion download-clip` (extends the existing `motion` Click custom-command dispatcher in `motion_cmd.py`).
- `--event-id <id>` resolves against the same `getEvents()` payload `motion history` uses — Phase 4c adds `event_id` to the §10.3 MotionEvent shape (already in pytapo's response, just not currently surfaced).
- `--experimental-clips` is REQUIRED. Without it, exit 64 with hint pointing at this section. Hint copy MUST include the words "experimental" and "may break across firmware".
- Backing implementation lifted from pytapo's `experiments/DownloadRecordings.py`, isolated behind `tapo_cli.media.download_clip()`. The wrapper hides the segment-list-then-ffmpeg-concat dance; operators see one input (event id) and one output (MP4 file).
- ffmpeg required on PATH (FR-64); missing → exit 6 (parity with `record` FR-13c).
- Devices without SD card or with `has_clip: false` → exit 4 with structured hint.

**Test fixtures (`tests/`):**

- `tests/test_motion_download_clip.py` — 8+ tests:
  - missing `--experimental-clips` flag → exit 64
  - unknown `event_id` → exit 4
  - `has_clip: false` event → exit 4 with explicit hint
  - happy path: pytapo segments → ffmpeg concat → bytes-on-disk match expected
  - ffmpeg-missing → exit 6
  - SD-card-missing → exit 4 with distinguishing hint
  - JSON output schema matches FR-65
  - SIGINT during ffmpeg concat → ffmpeg killed, partial file removed, exit 130
- `tests/test_motion_history_event_id.py` — verify `event_id` field surfaces when the device returns it; back-compat-test that older fixtures without `event_id` still parse.

**Hardware acceptance against live Tapo C200 (with SD card):**

- [ ] `motion enable front-door` then physically trigger motion → next `motion history --since 1m --jsonl` shows one event with `has_clip: true` and a non-null `event_id`
- [ ] `tapo-cli motion download-clip front-door --event-id <id> --output /tmp/clip.mp4 --experimental-clips` → exit 0, `/tmp/clip.mp4` plays in mpv, duration ≥ 1s
- [ ] Same command without `--experimental-clips` → exit 64
- [ ] `motion download-clip front-door --event-id ev_NONEXISTENT --output /tmp/x.mp4 --experimental-clips` → exit 4
- [ ] Same command on a no-SD-card camera → exit 4 with the SD-card-missing hint

**Risks:**

- **pytapo `experiments/` API breaks on firmware update.** Mitigation: `mechanism: "pytapo-experiments"` in output (FR-65) lets operators detect upstream churn from CI signals; `--experimental-clips` flag is the operator's opt-in to this risk.
- **Tapo SD-card recording format changes.** Mitigation: ffmpeg-on-the-fly concat is the most robust path; if ffmpeg can read the segments today it can read them tomorrow even if the segment-list endpoint changes shape (we'd patch the wrapper).
- **Clip file sizes large (~100MB+).** No special handling — `--output` to disk; operators can pipe to S3 themselves with a wrapper script.
- **Operators forget `--experimental-clips` and pile up exit-64 cron-mail.** Mitigation: deliberate. The friction is the feature.

#### 16.4.4 Phase 4 risk register (cross-cutting)

| Risk | Affects | Mitigation |
|------|---------|------------|
| **Phase 0 fixture corpus stale by Phase 4 start.** New ONVIF endpoints (`PullMessages`, `Unsubscribe`) and the pytapo experiments path were not exercised in Phase 0. | 4b, 4c | Phase 4 begins with a 1-day mini-Phase-0 — re-run `scripts/smoke.py` against the C200 with new probes for `CreatePullPointSubscription` and `getRecordings()`. Capture fixtures before any production code is written. |
| **pytapo SHA pin churn.** Library's mainline has moved since the v0.0.2 SHA pin (`de5ca37`); some Phase 4 features may want a newer SHA. | All | Phase 4a takes the explicit hit: pin to a newer SHA at Phase 4a-start, confirm regressions via the existing Phase 1-3 test suite (369 tests as of v0.3.0), then ship. Phase 4b/4c reuse the new pin. |
| **Group fan-out on long-running verbs.** A `@group --follow` is meaningless for `events`. | 4b | FR-57/58 prose plus carve-out test mirroring FR-43c posture. |
| **`--experimental-clips` adoption signals success too loudly.** If operators love it and depend on it, the flag's friction was wasted. | 4c | If Phase 4c clip-download becomes load-bearing, v1.3 promotes it to default-on with a sub-flag for the legacy guard. The flag is a tactic, not a strategy. |
| **Test suite size doubles.** Phase 3 added 44 tests (369 total); Phase 4 likely adds 30-40 more. | All | `pytest --maxfail=1` and the existing per-verb test file convention scale fine; CI budget is unchanged. |
| **Doorbell-press in `events` divergent from `motion history`.** ONVIF event-type taxonomy doesn't map 1:1 to pytapo's. | 4b | §10.6 + §10.3 enums kept in lockstep (FR-62 commitment); `_classify_event_type` (already in motion_cmd.py) extended with an ONVIF-side variant. |

#### 16.4.5 Phase 4 out-of-scope (explicitly NOT shipping)

- **Camera-account auto-creation verb.** v1.2.0 audit re-confirmed no documented endpoint exists; pytapo does not expose it. Reverse-engineering an undocumented Tapo-app HTTPS verb is exactly the firmware-fragility this CLI was designed to avoid. Stays in §14 Out-of-Scope.
- **HDR / noise-cancelling / auto-track set-flags.** No current operator pressure (per Dan, 2026-04-29). Stays in §14 / §15 row 19. v0.4 candidate, not Phase 4.
- **Atomic-rename refactor (drop flock).** Already settled in v1.1.0 §17 reviewer block (Architect #14). flock + atomic-rename both work; the simplification was rejected as observability regression. Not reopening.
- **HLS transcoding shim.** Stays in §14. `--protocol hls` exits 5; transcoding belongs in an external ffmpeg invocation.
- **Battery-doorbell third-party support.** Stays in §14 + §15 row 7. TP-Link upstream has not opened the API.

---

## 17. Revision History

### v1.2.0 — 2026-04-29 — Phase 4 scope defined + Phase 0-3 audit

Phase 3 shipped (v0.3.0, feature-complete) on 2026-04-29. v1.2.0 incorporates two concurrent passes on the SRD: a Phase 0-3 implementation accuracy audit (PASS A) and a definition of Phase 4 from "Reserved (no commitment)" placeholder into a concrete sub-phased plan (PASS B).

**PASS A — Phase 0-3 implementation audit:**

- **A-1 (MEDIUM, fixed): SessionMetadata field name drift.** §10.5 prose said `bytes`; §8.4 `auth status --json` example showed `"bytes":412`; shipped code (`types.SessionMetadata.bytes_size`, `auth_cmd._row_to_dict`) emits `bytes_size`. JSON contract drift would have broken `jq` patterns. **Fix:** §10.5 + §8.4 example reconciled to `bytes_size` (path of least breakage; v0.1.0 onwards has shipped this name).
- **A-2 (LOW, fixed): `auth migrate` FR identifier double-naming.** §5.20 (line 457) defined the verb as **FR-55**; §6.3 line 494 and §17 v1.1.1 prose called it **FR-CRED-15a**. Two names, one verb. **Fix:** FR-55 is now the canonical name; the two stray FR-CRED-15a references corrected.
- **A-3 (HIGH, retro-fix scheduled): `set` verb missing.** §8.1 verb table and §16.2 Phase 2 acceptance both list `set --image-flip` and `set --timezone` (FR-39 / FR-39a). The shipped v0.2.0 / v0.3.0 binaries do not register a `set` verb; `tapo-cli set` exits 64 ("No such command 'set'."). The Phase 2 PR (#6) missed it; the v0.2.0 CHANGELOG entry doesn't mention it. **Fix:** §16.2 annotated to call out the slip; §8.1 row updated; new FR-39c codifies the Phase 4a retro-fix; §16.4.1 lists it as a 4a deliverable.
- **A-4 (HIGH, Phase 4a focus): Fan-out generalization gap.** v0.3.0's `_fanout.run_fanout` (FR-39..43) is wired into `ptz` only. The verbs `info`, `privacy`, `led`, `night-vision`, `motion enable|disable|status|history`, `alarm`, `audio`, `osd`, `preset`, `reboot`, `set` (when shipped) all `lstrip("@")` and treat `@group-name` as a single alias. The Phase 3 engineer acknowledged the gap in code comments (`info_cmd.py:79-83`) but Phase 3 closed without the migration. **Fix:** new FR-43d enumerates the verbs in scope; FR-43e adds group-level confirmation for `reboot @group`; FR-43f handles mixed-feature groups; FR-56 / FR-56a / FR-56b codify the per-verb integration contract; §16.4.1 ships the migration as one PR.
- **A-5 (EXPECTED, fixed): §16.4 placeholder.** "Reserved (no commitment)" replaced with the concrete Phase 4 sub-phased plan (4a/4b/4c).
- **A-6 (INFO, closed): doorbell-press in `motion history --event-type` already implemented.** The brief flagged this as a Phase 3 README gap; in fact `motion_cmd.py:140` includes `doorbell-press` in the `--event-type` Choice, and `_classify_event_type` (line 426) routes doorbell ring events to the right token. No work needed; the brief item was itself stale.
- **A-7 (EXPECTED, fixed): SRD version + status string bumped to v1.2.0.**
- **A-8/A-9/A-10 (CLEAN):** §11.1 exit-code table, §10.1 Camera dataclass, §3.3.1 capability matrix all match the shipped code with zero drift.

**Drift severity totals:** 2 HIGH (A-3 set verb, A-4 fan-out), 1 MEDIUM (A-1 bytes_size), 1 LOW (A-2 FR-55 vs FR-CRED-15a), 4 EXPECTED/INFO/CLEAN.

**PASS B — Phase 4 scope decisions:**

Seven candidate items were on the deferred-from-v1 list. Decisions:

| # | Item | Decision | Sub-phase |
|---|------|----------|-----------|
| 1 | Fan-out generalization (FR-43d, FR-56) | **IN** | 4a |
| 2 | ONVIF `events --follow` push subscription (FR-57..62) | **IN** | 4b |
| 3 | Motion-clip download (FR-63..65, behind `--experimental-clips`) | **IN** | 4c |
| 4 | Camera-account auto-creation | **OUT** (defer-again) | n/a |
| 5 | Doorbell-press in `motion history --event-type` | **N/A** (already shipped) | n/a |
| 6 | HDR / noise-cancelling / auto-track set-flags | **OUT** (no operator pressure) | v0.4+ |
| 7 | Atomic-rename refactor (drop flock) | **OUT** (settled in v1.1.0 reviewer block) | n/a |
| **bonus** | `set` verb retro-fix (A-3) | **IN** | 4a |

**New FRs added in v1.2.0:** FR-39c (set retro-fix), FR-43d/e/f (fan-out generalization, group reboot, mixed-feature handling), FR-56/56a/56b (per-verb fan-out integration), FR-57..62 (events `--follow`), FR-63..65 (motion clip download experimental). Total **15 new atomic FRs**.

**New data models:** §10.6 `Event` (Phase 4b push events). §10.3 MotionEvent extended with optional `event_id` field for Phase 4c reference. §10.6 + §10.3 enums kept in lockstep per FR-62 commitment.

**Phase 4 effort estimate:** 3-4 weeks total across three sub-phases — 4a (~1 week), 4b (~2 weeks), 4c (~1 week). Each sub-phase is one PR. Each ships behind a hardware-acceptance gate against the live Tapo C200 (Dan's test rig).

**Camera-account auto-creation** was specifically re-evaluated against current upstream evidence (WebSearch across pytapo, HA-Tapo-Control, tapo-rest, TP-Link community forums). No documented endpoint exists; pytapo does not expose it. The Tapo-app menu remains the only reliable creation path. v1.2.0 keeps the verb out of scope and notes the evidence in §14 / §15 row 19.

**Atomic-rename refactor** (drop flock, keep atomic-rename only) was re-confirmed as **NOT REOPENED**. The v1.1.0 reviewer block (§17) settled it; the simplification trades observability for purity, which is the wrong direction. The current flock + atomic-rename + `pytapo_version` invalidation triple is the correct posture and stays.

No FRs renumbered. New FRs occupy the next-available slots (FR-39c, FR-43d-f, FR-56-65). v1.2.0 is a minor version bump because Phase 4 is a real phase now; no breaking changes to the v0.3.0 contract.

---

### v1.1.1 — 2026-04-28 — Cloud credentials shared with kasa-cli

Operator request: tapo-cli SHALL reuse the existing kasa-cli cloud-account credentials by default rather than requiring a duplicate `~/.config/tapo-cli/credentials` file. TP-Link cloud accounts are user-scoped, not device-family-scoped — same email and password authenticate both Kasa plug/bulb control planes and Tapo camera control planes.

- **§6.2 step 3.** Default cloud-account credentials file path changed from `~/.config/tapo-cli/credentials` to `~/.config/kasa-cli/credentials`. Format unchanged (JSON v1: `{"version":1,"username":"...","password":"..."}`).
- **§6.3 + new FR-CRED-3.1.** Codifies the shared-file model: tapo-cli reads but NEVER writes the kasa-cli credentials file; a tapo-only override at `~/.config/tapo-cli/credentials` is honored when present and takes precedence; chmod 0600 enforcement (FR-CRED-2) still applies; `auth migrate` (FR-CRED-15a) only ever rewrites the tapo-only file, never the shared file.
- **§9.2 / §9.3 (config schema).** Default `[credentials] file_path` updated; example block annotated to explain the shared-file rationale.
- **§15 row 6 (Resolved Decisions).** Credential model row updated to name the shared-file default and the override path.
- **§16.1 (Phase 1 acceptance).** Cross-references FR-CRED-3.1 alongside FR-CRED-1..3.

No FR renumbering. New atomic clause: FR-CRED-3.1.

---

### v1.1.0 — 2026-04-28 — Reviewer feedback applied

Three independent reviews (Architect, Engineer, Researcher) hit v1.0.0. The findings are catalogued by ID; each bullet names the section(s) touched.

**BLOCKING fixes (B-series):**

- **B1 — §3.4 + §5.1.** Subnet TCP/443 probe-scan promoted to **co-equal primary** alongside WS-Discovery. Both run in parallel by default; dedupe by MAC then IP. New flags: `--no-scan` / `--ws-discovery-only` / `--scan-only`. Multicast-drop documented as the dominant failure mode on consumer mesh / client-isolation networks.
- **B2 — §6.1 + §6.2 + FR-CRED-8/8.1.** Credential ordering inverted. **Camera account is now the PRIMARY** control-plane credential; cloud-account is a fallback for legacy firmware only. Cloud-fallback emits a deprecation warning per device per invocation.
- **B3 — §16.0.** New **Phase 0: Hardware Smoke-Test Gate** (3 days). Pin pytapo SHA, smoke-test against every owned camera, capture protocol fixtures. Phases 1-3 do not start until Phase 0 passes.
- **B4 — FR-5/5b.** `--target-network <CIDR>` with no matching local interface SHALL exit code **6** (config error) naming available interfaces.
- **B5 — FR-11..11d.** Snapshot fallback chain made deterministic: tier-advance condition explicit (timeout / non-200 / non-JPEG / non-auth-exception); auth-rejection at any tier short-circuits to exit 2 immediately; `--timeout` is a TOTAL wall-clock budget with default 40/30/30 split overridable via `--snapshot-budget`; tier-3 ffmpeg-missing → exit 6 (config), not 1 (device).
- **B6 — FR-12..12g.** Stream verb hardcoded `/stream1..7` paths replaced with **ONVIF `GetProfiles`-driven resolver**. New flags: `--profile <name>`, `--list-profiles`. Default lens × quality 2×2 truth table documented as the fallback when ONVIF is unavailable. Legacy `--protocol stream2` deprecated for one release.
- **B7 — FR-14..17c.** PTZ unit semantics specified: `--step` is interpreted as **degrees** on `ptz-mode: continuous` models, **device-step-units** on `ptz-mode: step` models. Zoom `--step` is always device-step-units. Steps passed to pytapo as integers. JSON output includes `step_unit`.
- **B8 — FR-25..25d.** Motion history determinism: `ts` is RFC 3339 UTC `Z`; `--since` accepts any RFC 3339 (assumes UTC if no offset, accepts bare dates); results sorted ascending by `ts`; future `--since` exits 0 with empty array.
- **B9 — FR-43a.** Group/batch all-failure exit code is now the failure code of the sub-op whose target is **first in the resolved alias list** (alias-config-file ordering), not first to fail by completion order. Deterministic.
- **B10 — FR-44a.** Batch JSONL per-line shape specified: `{command, target, status, exit_code, result?, error?}`. `result` is the verb's normal `--json` payload on success; `error` matches §11.2 minus the `exit_code` wrapping.
- **B11 — §7.2.** Determinism mandate: all timestamps RFC 3339 UTC `Z`; multi-record output sorted by target ascending in config order with secondary timestamp tiebreak; numeric fields are JSON numbers, never strings.
- **B12 — §6.7 + §8.3.** `--credential-source <env|file|none>` flag added with explicit precedence-override semantics, documented in both the auth chapter and the common-flags table.

**SHOULD-FIX (S-series):**

- **S1 — §6.8 + FR-CRED-9.** pytapo auth variants (legacy POST cookie / KLAP / SSE) acknowledged as informative. Cache schema is an opaque pytapo state blob with a `pytapo_version` field for invalidation on library upgrade.
- **S2 — FR-12f / FR-12g.** Credential-leak hardening: `--credentials-via-env` redacts URL on stdout and exports `RTSP_USER`/`RTSP_PASS`. New `--exec` shorthand passes RTSP URL via env to a child process via `execvp`.
- **S3 — FR-13/13a/13d.1/13g.** `--with-recording` mandate **dropped**. Footgun guard: `record` in non-tty mode requires `--duration` OR `--max-bytes`; tty mode prompts for confirmation. New `--max-bytes`. Perf targets: start-to-first-frame < 3000 ms p95; SIGINT-finalize < 2000 ms typical (5s upper).
- **S4 — §3.3.1.** Per-feature × per-model capability matrix added — tts, osd, alarm-trigger, night-vision modes, audio-volume, dual-lens-stream, ptz-mode. Phase 2 verbs cite this matrix; exit code 5 mappings reference it explicitly.
- **S5 — §3.1.** HA 2025.11 incident reworded as a **pytapo 3.3.51 incompatibility incident** (HA pinned to 3.3.51 and that exact pinned version became incompatible). Issue #1099 cited. Drops the misleading "<3.3.51" framing.
- **S6 — §3.3.** Disclaimer added clarifying the verified list is from HA-Tapo-Control README (2026-04-28) and is NOT TP-Link's current catalog. Added an "untested in v1" row enumerating current-catalog models (C402/C403/C460/C465/C560WS/C610/C615F/C645D/C660/C675D/TC53/TCW90). Legacy entries kept.
- **S7 — §4.6 + §5.4.** ONVIF "varies by firmware" claim rephrased to factual rationale ("pytapo coverage of stills is the most-divergent surface") rather than uncited generalization. Specific URLs intentionally not embedded because they move.
- **S8 — §10.5 + FR-CRED-14.** SessionMetadata reconciled with `auth status` example. Added `expires_at` (RFC 3339 string or null), `cache_path` field, and `pytapo_version` per FR-CRED-9.
- **S9 — FR-CRED-13.** Per-device flock acquisition timeout = `--timeout` seconds (default 5); timeout exits code **3** (network/contention) with PID of holder if obtainable from `/proc/locks` (Linux) or `lsof` (macOS).
- **S10 — §11.1 disambiguation block.** Missing-credentials → exit 2 (auth); credentials-file chmod violation → exit 2; snapshot tier-3 ffmpeg-missing → exit 6 (config); `--target-network` with no matching interface → exit 6.
- **S11 — §6.2 partial env fall-through note.** If exactly one of `TAPO_USERNAME`/`TAPO_PASSWORD` is set, env-var source is treated as not-set and resolver falls through. `-v` mode logs the partial-set as WARN.
- **S12 — FR-46.** `auto` output mode emits JSONL whenever stdout is not a tty, **including file redirects** — not only pipes.
- **S13 — FR-38.** `--quiet` implies `--yes` for `reboot`. tty-mode prompt rendered to stderr.
- **S14 — FR-37/37a/37b.** OSD `--text` length measured in Unicode codepoints (32 limit). Device-side rejection of unsupported codepoints → exit 1 (device error), distinct from exit 64 (CLI usage error for >32 codepoints).
- **S15 — FR-11d.** `snapshot --output -` incompatible with `--json`/`--jsonl` (exit 64). `--quiet` IS permitted with `--output -` because the JPEG bytes ARE the stdout payload.
- **S16 — kasa-cli parity gaps.** New `auth migrate` sub-verb (FR-55). New `--online-only` flag for `list` (FR-8 + §8.3 table). FR-54d adds `config show` redaction (`***` for both cloud-account and camera-account passwords). §12.1 mock-device fixtures expanded: TC85 outdoor, C100 indoor non-PTZ, C225 telephoto path (was wide-only), parity with kasa's per-family mocks.

### Deferred to backlog (intentional, with rationale)

- **Architect #10 — ONVIF event subscription.** Defer to Phase 4 candidate.
- **Architect #11 — motion-clip download experimental.** Kept in §14 Out-of-Scope; rephrased to "low priority not impossible" rather than "possibly never." A future SRD may revisit if the underlying protocol stabilizes.
- **Architect #14 — drop flock for atomic-rename only.** Kept as-is; flock is cheap insurance against rare same-machine concurrent invocations and the atomic-rename alone would be a regression in observability.
- **Architect #15 — camera-account creation verb.** Kept in §14 Out-of-Scope; rephrased to "deferred — protocol exists, low priority." Rare-enough operation that the manual Tapo-app step is acceptable for v1.
- **Engineer #19 — chmod on weird filesystems (NFS, FAT).** Deferred. Will surface as bug reports if it matters.
- **Engineer #25 — full SHA-pin syntax block.** Single one-line example added in §13.3; full pyproject `[tool.uv.sources]` block deferred until Phase 0 closes and the SHA is known.

### v1.0.0 — 2026-04-28 — Initial draft

Initial draft. 16 sections, 60+ FRs, kasa-cli structural parity. See git history for content baseline.

---

**End of document.**
