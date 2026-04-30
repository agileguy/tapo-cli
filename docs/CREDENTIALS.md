# tapo-cli — Credentials

Tapo cameras need two distinct credentials, and tapo-cli treats them as two distinct credentials. There is no auto-conflation, no single-password mode, no "type your TP-Link password and we'll figure it out." Both credentials live in chmod-0600 JSON files local to the host. SRD anchor: §6.

## TL;DR

If you also use [`kasa-cli`](https://github.com/agileguy/kasa-cli), tapo-cli already has your cloud credentials — same TP-Link account, same `~/.config/kasa-cli/credentials` file, read-only. Add a per-camera `camera_account_file` (created in the Tapo app) and you can `stream`/`record`.

```toml
[devices.office]
ip = "192.168.86.65"
mac = "10:5A:95:4C:44:C7"
model = "C200"
camera_account_file = "~/.config/tapo-cli/cam-office.json"
```

```bash
$ chmod 0600 ~/.config/tapo-cli/cam-office.json
$ tapo-cli stream office
rtsp://<camuser>:<campass>@192.168.86.65:554/stream1
```

The rest of this doc covers the why, the alternate paths, and the troubleshooting matrix.

## The dual-credential model

Two credentials, two purposes (FR-CRED-1..8.1, §6.1):

### 1. Camera account (per-device, PRIMARY)

Created in the Tapo app, **per camera**, under:

```
Tapo app → Camera → Settings → Advanced settings → Camera account
```

Username and password are 6-32 chars each. This is the credential the camera itself enforces — it gates RTSP streaming, ONVIF, and (on current firmware) is pytapo's preferred control-plane path. You can use the same `(username, password)` across multiple cameras you own; that's a personal-policy decision, not a CLI requirement.

**Required for `stream` and `record`.** Verbs that build an RTSP URL stop the resolver at this source — if no `camera_account_file` is configured, they exit 2 with a hint pointing at the Tapo-app menu (FR-12e, FR-CRED-7).

### 2. Cloud account (TP-Link app login, FALLBACK)

The email and password you use to log into the Tapo / Kasa mobile app. This is the **TP-Link cloud account** — issued per user, not per device family. Same email + password authenticates Kasa plugs, Kasa bulbs, Kasa strips, AND Tapo cameras.

In v1.1+ the cloud account is a **fallback** for the control plane (PTZ, motion, alarm, LED, privacy, audio, OSD, info, reboot) — only kicks in when pytapo's camera-account login fails with `_AUTH_FAILED` (older C200/C210 / TC55-era firmware). When the fallback fires, the CLI emits one WARN line on stderr per device per invocation:

```
WARN: cloud-account fallback used for office; camera-account login is the supported path on current firmware. See §6.1.
```

This warning is the contract, not a regression — it tells you your firmware is on the legacy path and you should update or move to camera-account auth.

## The kasa-cli sharing default

**Most operators come from kasa-cli.** They already have a TP-Link cloud account configured. Forcing them to re-type the same password into a second file is hostile.

So tapo-cli's default cloud-account credentials path is `~/.config/kasa-cli/credentials` — the same file kasa-cli writes (FR-CRED-3.1). tapo-cli **reads but never writes** that file. kasa-cli owns it. `tapo-cli auth migrate` (FR-55) refuses to touch it; only the tapo-only override path is migratable.

If you maintain a separate Tapo-only TP-Link account (rare), drop a tapo-only credentials file at:

```
~/.config/tapo-cli/credentials
```

When both exist, the tapo-only file wins and the shared file is not consulted (FR-CRED-3.1).

The shared file is subject to the same chmod-0600 enforcement as any other credentials file (FR-CRED-2). kasa-cli writes it 0600 on creation; do not loosen.

## File formats

Both credential files are JSON v1.

### Cloud-account credentials file

`~/.config/kasa-cli/credentials` (or `~/.config/tapo-cli/credentials` for the tapo-only override):

```json
{
  "version": 1,
  "username": "you@example.com",
  "password": "your-tplink-cloud-password"
}
```

- `version` is currently `1`. Missing `version` is treated as v1 with a single deprecation warning on stderr (FR-CRED-1).
- Unknown extra keys exit 6.
- chmod 0600. More-permissive modes exit 2 (FR-CRED-2).
- Symlinks pointing at the credential file are refused outright (R5).

### Camera-account credentials file

`~/.config/tapo-cli/cam-<alias>.json` (path is your choice; reference it from `[devices.<alias>] camera_account_file`):

```json
{
  "version": 1,
  "username": "camuser-office",
  "password": "Sl33py-cam-pw"
}
```

- Both `username` and `password` are 6-32 codepoints (FR-CRED-5). Out-of-range exits 6.
- chmod 0600. More-permissive modes exit 2.

```bash
$ chmod 0600 ~/.config/tapo-cli/cam-office.json
$ ls -l ~/.config/tapo-cli/cam-office.json
-rw-------  1 dan  staff  87 Apr 28 14:02 /Users/dan/.config/tapo-cli/cam-office.json
```

## Adding a camera account on each Tapo camera

The Tapo app creates camera accounts. There is no documented HTTPS endpoint for programmatic creation — pytapo does not expose it, and reverse-engineering it would re-introduce the firmware-fragility this CLI was designed to avoid (Resolved Decision #19, §15). v1.2.0 audit (2026-04-29) re-confirmed there is no public API. Live with the manual step; you do it once per camera, ever.

For each camera:

1. Open the Tapo app on a phone signed into the cloud account that owns the camera.
2. Tap the camera tile.
3. Tap the gear icon (top-right) → **Advanced settings** → **Camera account**.
4. Enable "Camera account" if it isn't already.
5. Set username (6-32 chars) and password (6-32 chars).
6. Tap "Save".

You can use the same `(username, password)` across all your cameras. Some operators prefer a single shared camera account for tooling and a different one stored in the Tapo app's password manager for emergency access — your call.

Battery-mode doorbells (D210, D235 in battery mode) **cannot accept a camera account** — verified against HA-Tapo-Control discussions #739/#794. tapo-cli does not support battery-doorbell third-party integration; wired/always-on operation only (§14, Resolved Decision #7).

## Resolution order

For each invocation that needs auth, the resolver walks (FR-CRED-1..3, §6.2):

1. **Per-device camera account file** — `[devices.<alias>] camera_account_file` (PRIMARY for control plane and the only source for RTSP).
2. **Per-device cloud-account override** — `[devices.<alias>] credential_file` (legacy-firmware fallback for this device).
3. **Default cloud-account credentials file** — `~/.config/tapo-cli/credentials` (tapo-only override) wins over `~/.config/kasa-cli/credentials` (shared) when both exist.
4. **Environment variables** — `TAPO_USERNAME` and `TAPO_PASSWORD`.
5. **No credentials configured** — exit 2 with a hint pointing at the Tapo-app camera-account menu and `[devices.<alias>] camera_account_file` config.

For RTSP-using verbs (`stream`, `record`), the resolver stops at step 1 — only the camera-account file is consulted. If step 1 is empty, those verbs exit 2 immediately with the camera-account hint (FR-CRED-7).

### Partial environment variable fall-through

If exactly ONE of `TAPO_USERNAME` / `TAPO_PASSWORD` is set and the other is empty, the env-var source is treated as "not set" and the resolver falls through to the next source (§6.2). Verbose mode (`-v`) logs this as a single WARN line — half-set env vars are almost always a misconfiguration, but they should not block other sources.

### `--credential-source <env|file|none>`

Override the default resolution order for one invocation (FR-CRED-15, §6.7):

| Value | Behavior |
|---|---|
| `env` | Only `TAPO_USERNAME` / `TAPO_PASSWORD`. Skip per-device files, skip default file. CI / containerized contexts. |
| `file` | Only file-based sources (per-device camera-account file, per-device cloud-account override, default credentials file). Skip env vars. Useful when env vars are present but stale. |
| `none` | Skip all sources. Commands requiring credentials exit 2. Useful for verifying that a cached pytapo session works without re-auth (paired with `auth status`). |

```bash
$ TAPO_USERNAME=ci@example.com TAPO_PASSWORD=ci-cloud-pw \
    tapo-cli --credential-source env info office

$ tapo-cli --credential-source none info office       # cache hit only; no re-auth attempts
```

The flag does NOT relax chmod-0600 enforcement (FR-CRED-2) or the partial-env fall-through rule — those are integrity invariants, not source choices.

## Session caching

Successful pytapo control-plane auth persists session state to (FR-CRED-9..13, §6.5):

```
~/.config/tapo-cli/.tokens/<DEVICE-MAC>.json
```

- Directory chmod 0700 on first use.
- File chmod 0600.
- Atomic writes via tmpfile + `fsync` + rename.
- Per-device advisory lock via `flock`.
- Top-level `pytapo_version` field — cache invalidates on pytapo upgrade.

Subsequent commands deserialize the cached state into pytapo and skip the handshake. If pytapo signals session expiration (or the device returns 401 / `_AUTH_FAILED`), the CLI invalidates the cache, performs a fresh handshake, retries once. Two consecutive auth failures exit 2.

The cache also invalidates when `--credential-source` selects a different source than the one that wrote the cache (the source name is stored in the cache blob — FR-CRED-11).

### Concurrent invocations

Two `tapo-cli` invocations targeting the same camera serialize on the per-device flock. Lock-acquisition timeout is `--timeout` seconds (default 5). Timeout exits **3** (network/contention), NOT 2 (auth), with a structured error naming the holding PID where the OS exposes it (Linux: `/proc/locks`; macOS: best-effort via `lsof`).

```bash
# In two terminals, simultaneously:
$ tapo-cli ptz office pan left --step 10        # acquires lock, runs
$ tapo-cli ptz office pan right --step 10       # waits up to --timeout, then runs
```

## `auth` workflows

```text
auth status     # one row per cached pytapo session-state file
auth flush      # delete cached sessions; --target restricts to one device
auth migrate    # rewrite older versioned credential files in place
```

### `auth status`

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

`expires_at` is `null` when the underlying pytapo state doesn't expose an expiry. `auth status` does NOT issue liveness probes — that's `list --probe`'s job (FR-CRED-14).

### `auth flush`

```bash
$ tapo-cli auth flush                     # everything
$ tapo-cli auth flush --target office     # one alias
$ tapo-cli auth flush --target 10:5A:95:4C:44:C7    # one MAC
```

Run this when you've changed the camera account on the device side, after pytapo upgrades that the auto-invalidation didn't catch, or when an auth error message hints at it.

### `auth migrate`

Rewrites older versioned credential files in place at the **tapo-only** path — never on the shared kasa-cli file (FR-55, FR-CRED-3.1):

```bash
$ tapo-cli auth migrate
```

- Refuses to run if any target file is not chmod 0600 (exit 2).
- Preserves the original at `<path>.v<old>.bak`.
- Acts ONLY on `~/.config/tapo-cli/credentials`. The shared `~/.config/kasa-cli/credentials` is owned by kasa-cli; tapo-cli does not modify it.

The verb exists for forward compat as the credential schema evolves. v0.4.1 ships only v1, so `auth migrate` is currently a no-op on a clean install — keep it in the toolbox for the next bump.

## Redaction

`tapo-cli config show` redacts passwords to the literal string `***` in TOML output (FR-54d). There is no `--show-secrets` flag in v1. To inspect a credential file, `cat` it directly — the file is chmod 0600 and visible only to its owner.

This keeps screenshots and pasted-output from leaking secrets.

The one place credentials DO appear in output is the RTSP URL emitted by `stream` (and the URL `record` constructs internally) — the URL is `rtsp://user:pass@host:port/path`, by RTSP-protocol design. To keep credentials out of shell history and process-list snapshots:

```bash
$ tapo-cli stream office --credentials-via-env --exec ffmpeg -i '{}' -c copy /tmp/cam.mp4
```

`--credentials-via-env` (FR-12f) prints the URL with the credentials redacted to placeholders AND exports `RTSP_USER` / `RTSP_PASS` env vars for the exec'd child. The credential is exported to the child but never written to a shell-visible buffer. Combined with `--exec` (FR-12g) which uses `execvp` to replace the tapo-cli process, this is the lowest-leakage way to hand RTSP into a media tool.

## Troubleshooting

### Exit 2 on a control-plane verb

```json
{"error":"auth_failed","exit_code":2,"target":"office","credential":"camera_account","message":"...","hint":"..."}
```

Check in order:

1. Is `[devices.office] camera_account_file` set in your config?
2. Is the file chmod 0600?
3. Does the file's `username` / `password` match what you set in the Tapo app on the camera (under Advanced settings → Camera account)?
4. Did pytapo recently upgrade? Try `tapo-cli auth flush --target office`.
5. Try `--credential-source file` to skip env vars; if that succeeds, your env vars are stale.

If the structured error names `credential: "cloud_account"`, the camera is on legacy firmware and tried the cloud-account fallback; check that the cloud-account credentials file is readable and the password is current.

### Exit 2 on chmod violation

```json
{"error":"auth_failed","exit_code":2,"message":"credential file mode 0644 too permissive","hint":"chmod 0600 <path>"}
```

```bash
$ chmod 0600 ~/.config/tapo-cli/cam-office.json
```

### Exit 2 with "WARN: cloud-account fallback used"

The camera's firmware is on the legacy auth path. Two options:

- Update the camera firmware via the Tapo app (most current builds support camera-account auth on the control plane).
- Leave it; the fallback works and the warning is observability, not an error.

### Exit 3 on lock-acquisition timeout

```json
{"error":"network_error","exit_code":3,"message":"flock timeout on /Users/dan/.config/tapo-cli/.tokens/105A954C44C7.json","hint":"another tapo-cli is holding the lock; raise --timeout or wait"}
```

Another tapo-cli invocation against the same camera is in flight. Either wait or raise `--timeout`.

### "Camera account" menu is missing in the Tapo app

- Check firmware: very old builds don't expose the menu. Update via the app.
- For battery-mode doorbells (D210, D235 in battery mode), the menu is unavailable by design (TP-Link upstream gap). tapo-cli does not support these — wired/always-on only.

## See also

- [`CONFIGURATION.md`](CONFIGURATION.md) — config schema and resolution order.
- [`USAGE.md`](USAGE.md) — every verb's auth requirements.
- [`SRD-tapo-cli.md`](SRD-tapo-cli.md) §6, §15 row 6, §15 row 19 — the authoritative spec.
