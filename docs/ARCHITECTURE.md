# tapo-cli — Architecture

The why. SRD anchor: §4 (wrap vs reimplement), §11 (error model), §3.4 (discovery), §5.4/5.21 (snapshot fallback / fan-out).

## High-level shape

```
                                +---- pytapo (HTTPS control plane)
                                |       PTZ, motion, alarm, LED, privacy,
   shell --> click CLI -->  TapoConnection ----+   audio, OSD, info, reboot, set
                                |
                                +---- onvif-zeep-async (ONVIF SOAP)
                                |       WS-Discovery + GetSnapshotUri +
                                |       GetProfiles + PullPointSubscription (events)
                                |
                                +---- WSDiscovery (multicast transport)
                                |
                                +---- ffmpeg (subprocess)
                                        snapshot tier-3 fallback + record verb
                                        + motion download-clip concat
```

Three libraries plus an ffmpeg subprocess. pytapo for the Tapo control plane, onvif-zeep-async for ONVIF surfaces, WSDiscovery for the multicast transport, ffmpeg for video work. No reimplementation of the Tapo HTTPS protocol, the ONVIF SOAP envelope, or video codec handling — those are libraries' jobs (§4.1, §4.3).

## Why three libraries, not one

A naive "wrap pytapo" would be a single-library wrapper. It doesn't survive contact with reality (§4.3):

- **pytapo's discovery is weak.** It expects you to already have the camera's IP. Real LAN discovery requires ONVIF or scanning.
- **pytapo's snapshot path is model-dependent.** Some models return a working JPEG via the control plane; others return a stream-token-only response that requires further work. ONVIF `GetSnapshotUri` is the broader-coverage fallback.
- **Recording requires ffmpeg regardless.** pytapo's own SD-card download example shells out to ffmpeg for the conversion step. There is no "pure pytapo" recording path.

Three libraries each doing what they're best at is the honest architecture.

## Code layout

```
src/tapo_cli/
├── __init__.py
├── __main__.py
├── cli.py                  # Click top-level group; verb registration
├── runner.py               # async runner + TapoCliError → exit-code mapping
├── output.py               # text/json/jsonl auto-detection; emit() / emit_stream()
├── errors.py               # exit codes + StructuredError + exception hierarchy
├── config.py               # TOML loader + schema validation
├── credentials.py          # cloud-account + camera-account file resolver
├── auth_cache.py           # ~/.config/tapo-cli/.tokens/<mac>.json (FR-CRED-9..13)
├── discovery.py            # WSDiscovery + TCP/443 scan; merge by MAC then IP
├── media.py                # snapshot fallback chain + ONVIF profile resolver
├── wrapper.py              # TapoConnection — pytapo wrapper layer
├── device_info.py          # MODEL_FEATURES capability matrix; supported() check
├── types.py                # dataclasses: Camera, Stream, MotionEvent, Preset, ...
└── verbs/
    ├── _fanout.py          # @group → per-alias fan-out helper (FR-43d, FR-56)
    ├── _target.py          # alias / IP / MAC resolver
    ├── _capability.py      # per-verb capability gate (model lookup → exit 5)
    ├── alarm_cmd.py
    ├── audio_cmd.py
    ├── auth_cmd.py
    ├── batch_cmd.py
    ├── config_cmd.py
    ├── discover_cmd.py
    ├── events_cmd.py
    ├── info_cmd.py
    ├── led_cmd.py
    ├── list_cmd.py
    ├── motion_cmd.py       # enable/disable/status/history + download-clip
    ├── night_vision_cmd.py
    ├── osd_cmd.py
    ├── preset_cmd.py
    ├── privacy_cmd.py
    ├── ptz_cmd.py
    ├── reboot_cmd.py
    ├── record_cmd.py
    ├── set_cmd.py          # FR-39 retro-fix (Phase 4a)
    ├── snapshot_cmd.py
    └── stream_cmd.py
```

## The three-mechanism snapshot fallback chain

The headline architectural call. SRD anchor: FR-11..11d, B5, §15 row 4.

`tapo-cli snapshot <target> --output <path>` tries three mechanisms in order, advancing on failure:

1. **pytapo native snapshot** — pytapo's own snapshot endpoint (model-dependent return shape).
2. **ONVIF `GetSnapshotUri`** via onvif-zeep-async — broader-coverage SOAP path.
3. **ffmpeg single-frame from RTSP** — `ffmpeg -y -i rtsp://... -frames:v 1 <path>`.

A mechanism is FAILED — and the next tier is attempted — on any of (FR-11a.1):

- The per-mechanism budget elapses without a complete response.
- A non-200 HTTP response or a non-JPEG payload (verified by sniffing magic bytes `FF D8 FF`).
- Any unhandled exception **other than** auth-rejection.

**Auth-rejection short-circuits.** HTTP 401 from any tier, pytapo `_AUTH_FAILED`, or RTSP 401 does NOT advance to the next tier — it exits 2 immediately (FR-11a.2). The reasoning: if the credential is wrong, ONVIF and RTSP are using the same wrong credential. Burning their budgets re-failing is wasteful and noisy.

**Budget split.** `--timeout` is the TOTAL wall-clock budget. Default split is 40% pytapo / 30% ONVIF / 30% ffmpeg, overridable via `--snapshot-budget pytapo=N,onvif=N,ffmpeg=N`. Sum SHALL NOT exceed `--timeout`; if it does, exit 64 (FR-11a.3).

**ffmpeg-missing is config, not device.** If the chain reaches tier 3 and `ffmpeg` is not on `PATH`, exit **6** (config error) — not 1 (device error) — naming the missing dependency in the structured error (FR-11a.4).

Real failure shape:

```json
{"error":"device_error","exit_code":1,"message":"all snapshot mechanisms failed for 'office'","target":"office","details":{"attempts":[{"mechanism":"pytapo","status":"fail","elapsed_ms":2001.28,"detail":"timeout after 2.00s"},{"mechanism":"onvif","status":"fail","elapsed_ms":1641.7,"detail":"timeout after 1.50s"},{"mechanism":"ffmpeg","status":"fail","elapsed_ms":1508.55,"detail":"timeout after 1.50s"}]}}
```

The succeeded mechanism is reported in `--json` output as `{"mechanism": "pytapo"|"onvif"|"ffmpeg"}` (FR-11b). This is observability, not contract — the CLI does NOT promise a specific mechanism per model.

## ONVIF Profile-S subscription model (events)

`tapo-cli events <target> [--follow]` (Phase 4b, FR-57..62) subscribes to the camera's ONVIF Profile-S `PullPointSubscription` endpoint and emits events as JSONL.

```
events_service.CreatePullPointSubscription
       │
       ▼
pullpoint_service.PullMessages   (one-shot or loop)
       │
       ▼
NotificationMessage → _classify_event_type → §10.6 Event record
       │
       ▼
JSONL on stdout
       │
       ▼
subscription.Unsubscribe          (clean termination, 2s budget)
```

**Topic projection** (FR-62):

- `tns1:RuleEngine/CellMotionDetector/Motion`, `tns1:VideoSource/MotionAlarm` → `motion`
- `tns1:RuleEngine/MyRuleDetector/HumanDetect`, `.../PeopleDetect` → `person`
- `tns1:Device/Trigger/DigitalInput` → `doorbell-press`
- Tamper events → `unknown` (the §10.6 enum has no `tamper` token)

**Cadence.** One-shot uses `Timeout=PT5S` `MessageLimit=100`. `--follow` uses `Timeout=PT30S` `MessageLimit=100` per call.

**Auto-reconnect** (FR-61). Capped exponential backoff on transport errors: `1s → 2s → 4s → 8s → 16s → 32s → 32s`. Five consecutive failures exit 3. A successful pull resets the counter to zero.

**Why drive `CreatePullPointSubscription` directly instead of using `ONVIFCamera.create_pullpoint_manager()`?** The manager helper auto-renews on a timer; that conflicts with the explicit `--reconnect-after` lifecycle and makes the FR-58/60/61 budgets non-deterministic. The lower-level path makes the test mocks straightforward.

**Group rejection.** `events @group --follow` exits 64 — one subscription per stdout. FR-43c-style carve-out matching `stream` and `record`.

## The fan-out helper

`src/tapo_cli/verbs/_fanout.py` is the single mechanism every state-control verb uses to dispatch on `@group` targets (FR-43d, FR-56).

```python
def is_group_target(target: str, cfg: Config) -> bool:
    """True if target (with optional leading @) names a config group."""
    resolved = target.lstrip("@") or target
    return resolved in cfg.groups

async def run_fanout(*, members, per_target, concurrency, mode) -> int:
    # Bounded-concurrency dispatch. One JSONL line per member in resolved-alias
    # order (B9 deterministic — config-file ordering, NOT completion order).
    # Each line is the B10 envelope: {target, status, exit_code, result?, error?}
    # FR-43a exit-code semantics: 0 / 7 / first-failure-code.
```

Every verb's per-target work-function is the same shape: `(alias) -> (rc, record_dict)` on success, raises `TapoCliError` on failure. The fan-out helper wraps results in the standard B10 envelope so operators' `jq` patterns are consistent across batch and group fan-out output.

**Concurrency** is `--concurrency` if set, else `[defaults] concurrency`, else `5`. Lower than kasa-cli's `10` because camera control ops are heavier (FR-42, Resolved Decision #16).

**Verbs that fan out** (FR-43d enumeration): `info`, `privacy`, `led`, `night-vision`, `motion {enable|disable|status|history}`, `alarm {enable|disable|trigger|status}`, `audio {volume|mic|speaker|tts}`, `osd {set|clear|status}`, `preset {list|goto|save|delete}`, `reboot`, `set`, `snapshot` (Phase 4a).

**Verbs that REFUSE `@group`**: `stream`, `record` (FR-43c — multiple cameras can't share one URL or recording target), `events --follow` (FR-58 carve-out — single subscription per stdout).

**`reboot @group` confirmation** (FR-43e). One stderr prompt enumerating the resolved member list, NOT one prompt per camera. `--yes` / `--quiet` short-circuit; the per-camera fan-out then proceeds with no further prompts.

**Mixed-feature groups** (FR-43f). When a group contains members whose models do not all support the requested verb (e.g. `audio tts @all-cams` with members lacking TTS), unsupported members emit a per-target exit-5 result via the standard envelope. The overall exit code follows FR-43a (mixed → 7).

## Capability matrix

`device_info.MODEL_FEATURES` is the v1 contract for what `tapo-cli info` reports under `features` and what each verb checks before issuing a request (SRD §3.3.1, §10.1).

```python
MODEL_FEATURES: dict[str, frozenset[str]] = {
    "C100":   frozenset({"led", "privacy", "ir"}),
    "C200":   frozenset({"led", "privacy", "ir", "ptz"}),
    "C210":   frozenset({"led", "privacy", "ir", "audio", "ptz"}),
    "C225":   frozenset({"led", "privacy", "ir", "audio", "ptz", "zoom", "dual-lens"}),
    "C320WS": frozenset({"led", "privacy", "ir", "audio", "alarm"}),
    "C520WS": frozenset({"led", "privacy", "ir", "audio", "alarm", "ptz"}),
    "C530WS": frozenset({"led", "privacy", "ir", "audio", "alarm", "ptz", "tts"}),
    # ... full table in src/tapo_cli/device_info.py
}
```

A blank cell means **unsupported on that model** — verbs targeting it exit 5 with a hint pointing at the `--help` text and §3.3.1. The `_capability.py` module is where verbs do the lookup-and-gate; if the verb's required feature isn't in the model's feature set, it raises `UnsupportedFeatureError` before issuing any device call.

Models not on the verified list still execute (`info` etc. work fine), but the `Camera` record carries `supported: false` and a single WARN log line lands on stderr (FR-10a). Models on the "untested in v1" row (C402/C403/C460/C465/C560WS/...) carry `supported: untested` with a stronger WARN recommending Phase 0 smoke-test (§3.3, §16.0).

## Exit code model

SRD §11.1. The single source of truth is `src/tapo_cli/errors.py`.

| Code | Meaning | When |
|---|---|---|
| 0 | Success | Operation completed; for batch/group, **every** sub-op succeeded. |
| 1 | Device error | Camera returned an error response (non-auth, non-network); all three snapshot mechanisms failed without auth-rejection (FR-11c); device firmware rejects an OSD codepoint (FR-37b). |
| 2 | Authentication error | Cloud-account or camera-account auth failed; missing credentials when no other source is configured; credentials file chmod-mode too permissive (FR-CRED-2); no `camera_account_file` for an RTSP-using verb (FR-CRED-7); auth-rejection at any snapshot tier (FR-11a.2). |
| 3 | Network error | Timeout, connection refused, no route, multicast bind failure, camera unreachable on LAN; concurrent-lock acquisition timeout (FR-CRED-13). |
| 4 | Device not found | Alias unknown in config, IP unreachable, MAC not on LAN, unknown preset name. |
| 5 | Unsupported feature | Verb/flag combo not supported by target model or firmware (e.g. PTZ on a non-PTZ camera, `tts` on a model without speaker, `--protocol hls`, ONVIF `GetProfiles` on a model without ONVIF). |
| 6 | Config error | Config file missing when `--config`/`TAPO_CLI_CONFIG` was set; invalid TOML; unresolvable references; unknown keys; ffmpeg not on PATH **including the snapshot tier-3 fallback case** (FR-11a.4); `--target-network <CIDR>` with no matching local interface (FR-5b). |
| 7 | Partial batch/group failure | ≥1 sub-op succeeded AND ≥1 sub-op failed. |
| 64 | Usage error | Invalid CLI invocation: missing required arg, mutually-exclusive flags, group target on `stream`/`record`, `record` in non-tty without `--duration`/`--max-bytes` (FR-13a), `osd` text >32 codepoints (FR-37a), `reboot` non-tty without `--yes`, `--snapshot-budget` sum > `--timeout` (FR-11a.3), `--output -` with `--json`/`--jsonl` (FR-11d). |
| 130 | SIGINT | Ctrl-C during execution; partial JSONL stream emitted with trailing `{"event":"interrupted",...}` line; ffmpeg child (if any) gets forwarded SIGINT. |
| 143 | SIGTERM | Same partial-result + interrupted-line behavior as 130. |

The exception hierarchy in `errors.py` is one class per exit code (`DeviceError` → 1, `AuthError` → 2, `NetworkError` → 3, etc.). Verbs raise the appropriate subclass; the runner in `runner.py` catches and maps to the exit code, emitting the structured error to stderr per §11.2.

### Disambiguation rules (v1.1)

- **Missing credentials** (no source configured at all) → exit **2** (auth, not config). The user has not configured how to authenticate; this is auth-domain even though a config field is involved.
- **Credentials file chmod violation** → exit **2** (auth). Credential-source integrity failure, not config syntax.
- **Snapshot tier-3 with ffmpeg missing** → exit **6** (config). The CLI cannot complete a request because a documented dependency is absent.
- **`--target-network <CIDR>` no matching interface** → exit **6** (config). Available interface CIDRs MUST be named in the error message.

### Structured error shape (§11.2)

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

The `error` enum is closed and stable (`device_error`, `auth_failed`, `network_error`, `not_found`, `unsupported_feature`, `config_error`, `partial_failure`, `usage_error`, `interrupted`). Tooling MAY pattern-match on it.

## Output format

`auto` mode emits text on a tty, JSONL on anything else — including file redirects (FR-46, S12). Pipes (`| cat`), redirects (`> out.txt`), and command substitutions (`$(...)`) all count as machine consumers.

```
isatty(stdout) == True   →  text format (default for terminal use)
isatty(stdout) == False  →  JSONL (default for any redirect / pipe / non-tty)

--json                   →  pretty JSON, regardless of tty
--jsonl                  →  one JSON object per line, regardless of tty
--quiet                  →  suppress stdout entirely
```

The detection lives in `output.detect_mode()`; verbs use `emit(record, mode)` and `emit_stream(records, mode)` to render in whichever mode the runner picked. Mutually-exclusive `--json` + `--jsonl` exits 64.

**JSON validity invariant** (FR-49a). On any non-zero exit, stdout is valid parseable JSON or empty. The CLI never emits malformed JSON. For batch and group operations with mixed results, stdout JSONL contains one result object per attempted operation including those that failed (each with its own `error` field). Stderr emits the structured summary error once.

**Determinism** (§7.2):

- All timestamps are RFC 3339 UTC `Z`.
- Multi-record output is sorted by `target` ascending in resolved-config order, with ties broken by event timestamp ascending. Motion history is sorted by `ts` ascending (single-target).
- Numeric fields are JSON numbers, never strings.

## Signal handling

`runner.py` installs SIGINT and SIGTERM handlers that propagate cleanly to in-flight operations.

- **SIGINT** → exit **130**, with a `{"event":"interrupted",...}` summary line on stdout when the verb supports streaming output (`batch`, `events --follow`, `motion history` with large `--limit`).
- **SIGTERM** → exit **143**, same partial-result + interrupted-line behavior.

**Verb-specific signal flows:**

- `record` (FR-13b): forward signal to ffmpeg child, wait up to **5 seconds** for ffmpeg to flush and finalize the MP4 (typical case under **2s** for sub-1GB recordings — FR-13g).
- `batch` (FR-45c): cease dispatching new sub-operations, wait up to **2 seconds** for in-flight sub-ops to complete and have their results emitted, emit summary line, exit.
- `events --follow` (FR-58): attempt clean `Unsubscribe` within a hard **2-second budget**, emit `{"event":"interrupted","subscription_age_s":...}` summary line, exit.

## Discovery

WS-Discovery alone fails on most home networks (§3.4). Consumer mesh routers — Eero, Google Nest Wifi, TP-Link Deco, Asus AiMesh — drop or fail to forward multicast across mesh nodes by default. Wi-Fi client-isolation (often enabled on guest networks and IoT-segregated SSIDs) silently swallows it on a single AP. Multi-NIC hosts (Wi-Fi + Tailscale + Docker bridges) frequently send the probe out the wrong interface.

So `tapo-cli discover` runs **two co-equal primary paths in parallel** by default (FR-1, B1):

1. ONVIF WS-Discovery multicast probe on UDP `239.255.255.250:3702`.
2. TCP/443 HTTPS probe-scan across the local subnet.

Results are merged and deduplicated by MAC (preferred key), falling back to IP when MAC is unavailable. When the same device responds on both paths, the WS-Discovery record's ONVIF metadata is preserved and merged with the probe-scan record's reachability evidence.

A zero-result run within `--timeout` is success (exit 0, empty output) per FR-5a — no cameras responding is a valid answer to "what's on the LAN." Exit 3 is reserved for the case where both transports fail at the OS level.

`--no-scan` / `--ws-discovery-only` / `--scan-only` / `--target-network <CIDR>` give explicit control. `--target-network` with no matching local interface exits 6 with a message naming available interface CIDRs (FR-5b).

## Considered alternatives, rejected

- **Reimplement the Tapo HTTPS protocol.** Months of work, ongoing churn, no benefit.
- **TypeScript wrapper shelling to pytapo.** ~2× latency floor, three libraries instead of one to plumb, ffmpeg is process-spawn anyway. Wrong stack.
- **Pure ONVIF (no pytapo).** Cuts feature surface to the Profile-S subset (snapshot, RTSP URL, basic PTZ). No alarm, no OSD, no named night-vision modes, no motion history, no audio TTS — those are proprietary Tapo verbs.
- **Drop flock, keep atomic-rename only.** Settled in v1.1.0 reviewer block. flock is cheap insurance against rare same-machine concurrent invocations; the simplification trades observability for purity, which is the wrong direction.

See SRD §4.5, §4.6, §16.4.5 for full rejection rationale.

## See also

- [`SRD-tapo-cli.md`](SRD-tapo-cli.md) — the authoritative spec.
- [`USAGE.md`](USAGE.md) — every verb's flags and exit codes.
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — dev setup and test policy.
