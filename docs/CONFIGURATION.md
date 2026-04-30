# tapo-cli — Configuration

The config file is TOML. It defines aliases (alias → IP/MAC/camera-account-file), optional groups, and a handful of CLI defaults. SRD anchor: §9.

## Resolution order

Config-file lookup follows three rules in order (FR-54):

1. `--config <path>` flag if present.
2. `TAPO_CLI_CONFIG` environment variable if set and non-empty.
3. `~/.config/tapo-cli/config.toml` if it exists.

If `--config` or `TAPO_CLI_CONFIG` is set and the referenced file does not exist or cannot be read, the CLI exits 6 (config error). Silent fallback is forbidden — explicit selection means strict (FR-54a).

If only the default location is consulted and it does not exist, the CLI runs with built-in defaults and emits one INFO line on stderr ("no config file found, using defaults"). Discovery still works without a config file; alias-targeted verbs do not.

```bash
tapo-cli list                                       # uses ~/.config/tapo-cli/config.toml
TAPO_CLI_CONFIG=/etc/tapo-cli/prod.toml tapo-cli list
tapo-cli --config /tmp/test.toml list
```

`--config` wins over `TAPO_CLI_CONFIG` when both are set.

## Inspecting and validating

```bash
$ tapo-cli config show           # print the resolved effective config; passwords redacted to ***
$ tapo-cli config validate       # validate the resolved config; exit 0 / 6
$ tapo-cli config validate /tmp/candidate.toml   # validate a specific file
```

`config validate` parses the file, resolves every alias-to-device reference, resolves every group-to-alias reference, and verifies referenced credential and camera-account files exist with chmod 0600. Any failure exits 6 with a structured error pointing at the problem.

## Schema

Every key in v0.4.1.

### `[defaults]`

| Key | Type | Default | Meaning |
|---|---|---|---|
| `timeout_seconds` | int | `5` | Per-operation timeout. Overridable per-invocation via `--timeout`. |
| `concurrency` | int | `5` | Max parallel device ops in `@group` fan-out and `batch`. Overridable via `--concurrency`. Lower than kasa-cli's 10 because camera control ops are heavier (FR-42). |
| `output_format` | string | `"auto"` | One of `auto`, `text`, `json`, `jsonl`. `auto` emits text on a tty, JSONL on anything else (FR-46). |

### `[credentials]`

| Key | Type | Default | Meaning |
|---|---|---|---|
| `file_path` | string | `"~/.config/kasa-cli/credentials"` | Default cloud-account credentials file (shared with kasa-cli per FR-CRED-3.1). Set to `~/.config/tapo-cli/credentials` if you maintain a separate Tapo-only TP-Link account. |

The cloud-account credentials file is JSON v1: `{"version": 1, "username": "<email>", "password": "<password>"}`, chmod 0600. See [`CREDENTIALS.md`](CREDENTIALS.md) for the dual-credential model.

### `[ffmpeg]`

| Key | Type | Default | Meaning |
|---|---|---|---|
| `path` | string | `"ffmpeg"` | Override the ffmpeg binary path. Default resolves on `PATH`. Used for `snapshot` tier-3 fallback (FR-11a) and the `record` verb (FR-13c). |

### `[logging]`

| Key | Type | Default | Meaning |
|---|---|---|---|
| `file` | string | — | Optional file path for JSON log tee (append, line-buffered). Does not rotate in v1 (§7.3). |

### `[devices.<alias>]`

One block per camera. The alias is the verb target (`tapo-cli info <alias>`).

| Key | Type | Default | Meaning |
|---|---|---|---|
| `ip` | string | — | Static IPv4. Required for direct addressing; skips discovery. |
| `mac` | string | — | MAC address for stable identification across IP churn. Used as the per-device session-cache key (`~/.config/tapo-cli/.tokens/<mac>.json`). |
| `model` | string | — | Optional. Verifies against the live `getBasicInfo` model on first contact. |
| `credential_file` | string | — | Per-device cloud-account override (legacy-firmware fallback). JSON v1 format (FR-CRED-3.1). |
| `camera_account_file` | string | — | Per-device camera-account file. **REQUIRED** for `stream`/`record`. JSON v1: `{"version": 1, "username": "<6-32 chars>", "password": "<6-32 chars>"}`, chmod 0600 (FR-CRED-4..7). |

### `[groups]`

| Key | Type | Meaning |
|---|---|---|
| `<group-name>` | string[] | Array of alias names (in execution order). |

`groups add` / `groups remove` are NOT supported by the CLI — mutations are by hand-editing this section (FR-43b). `tapo-cli groups list` is read-only.

## Worked example

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
camera_account_file = "~/.config/tapo-cli/cam-front-door.json"

[devices.backyard]
ip = "192.168.1.51"
mac = "AA:BB:CC:DD:EE:02"
model = "C320WS"
camera_account_file = "~/.config/tapo-cli/cam-backyard.json"

[devices.office]
ip = "192.168.1.78"
mac = "AA:BB:CC:DD:EE:03"
model = "C225"
camera_account_file = "~/.config/tapo-cli/cam-office.json"

[groups]
perimeter-cams = ["front-door", "backyard"]
all-cams       = ["front-door", "backyard", "office"]
```

Verify:

```bash
$ tapo-cli config validate
$ echo $?
0

$ tapo-cli list
{"alias":"front-door","ip":"192.168.1.42","mac":"AA:BB:CC:DD:EE:01","model":"D230","online":null}
{"alias":"backyard","ip":"192.168.1.51","mac":"AA:BB:CC:DD:EE:02","model":"C320WS","online":null}
{"alias":"office","ip":"192.168.1.78","mac":"AA:BB:CC:DD:EE:03","model":"C225","online":null}

$ tapo-cli groups list
perimeter-cams: front-door (192.168.1.42), backyard (192.168.1.51)
all-cams: front-door (192.168.1.42), backyard (192.168.1.51), office (192.168.1.78)
```

## Path expansion

`~` and `$VAR` references in `file_path`, `camera_account_file`, `credential_file`, `ffmpeg.path`, and `logging.file` are expanded relative to the invoking user's environment.

Symlinks pointing AT credential files are refused (R5 in the SRD). The credential file itself must be a regular chmod-0600 file owned by the invoking user.

## What's NOT in the config

- **Cleartext credentials.** Cloud-account and camera-account credentials live in their own chmod-0600 JSON files (referenced from this config, never embedded in it).
- **Runtime state.** Session caches live at `~/.config/tapo-cli/.tokens/<mac>.json` and are managed by `tapo-cli auth`.
- **Logging rotation.** v1 file logging appends; rotation is the operating system's job (logrotate, etc.).
- **Schedule definitions.** That's cron / systemd timers / launchd. The CLI is a leaf node.

## See also

- [`CREDENTIALS.md`](CREDENTIALS.md) — cloud-account vs camera-account, the kasa-cli sharing default, file formats, session cache.
- [`USAGE.md`](USAGE.md) — every verb's flags and examples.
- [`SRD-tapo-cli.md`](SRD-tapo-cli.md) §9 — the authoritative schema.
