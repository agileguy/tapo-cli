# Test Fixtures

Captured protocol artifacts from real devices live here. Two subdirectories:

- **`raw/`** — verbatim output from `scripts/smoke.py` runs (XML, JPEG, run reports). **Gitignored** (see top-level `.gitignore`). Captures often include MAC addresses, internal IPs, serial numbers, and credentialed snapshot URIs. Never commit `raw/` contents until they have been scrubbed.
- **`scrubbed/`** — review-cleared fixtures used by unit tests. Created by hand from `raw/` artifacts. This is what `pytest` consumes.

## Why `raw/` is gitignored

The Phase 0 smoke harness writes:

- `smoke-report-<UTC-iso8601>.json` — per-camera, per-mechanism status + timings.
- `<alias>-getprofiles.xml` — ONVIF GetProfiles SOAP response (raw).
- `<alias>-getsnapshoturi.xml` — ONVIF GetSnapshotUri SOAP response (raw — the `<Uri>` element typically embeds a credential token).
- `<alias>-onvif.jpg` — JPEG fetched from GetSnapshotUri.
- `<alias>-ffmpeg.jpg` — JPEG extracted from the credentialed RTSP URL via `ffmpeg -frames:v 1`.
- `<alias>-pytapo.txt` — pytapo native-tier diagnostic note.

These contain device-identifying material and credentialed URLs.

## Scrubbing checklist (before promoting `raw/` → `scrubbed/`)

Before copying any `raw/<alias>-*.xml` into `scrubbed/`:

1. Replace MAC addresses (`AA:BB:CC:DD:EE:FF` and `AABBCCDDEEFF` forms) with `00:11:22:33:44:55`.
2. Replace serial numbers in `GetDeviceInformation` responses (`SerialNumber`, `HardwareId`) with `SERIAL-REDACTED`.
3. Replace internal IPs (`192.168.x.y`, `10.x.y.z`, `172.16.x.y`) with `192.0.2.10` (TEST-NET-1, RFC 5737).
4. Replace any `://user:pass@host` credential payload in URIs with `://user:pass@host` placeholder text or strip outright.
5. Replace any URL token query parameter (`?token=...`, `?key=...`) with `?token=REDACTED`.
6. Re-name aliases to generic names (`front-door` → `cam1`, etc.) if the alias hints at the operator's deployment.

Open the scrubbed file, eyeball it, then commit. JPEGs from `raw/` should never be committed — they show whatever the camera could see.

## Naming convention for `scrubbed/`

`<model>-<firmware>-<feature>.xml`, e.g., `c320ws-1.3.13-getprofiles.xml`. Multiple firmwares per model are kept side-by-side because Tapo's ONVIF surface drifts between firmware revisions.

## Phase 0 vs Phase 1+

Phase 0 only populates `raw/`. Promoting fixtures to `scrubbed/` and wiring them into mock-replay tests is a Phase 1 task (per SRD §12.3).
