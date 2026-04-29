# Changelog

All notable changes to `tapo-cli`.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
