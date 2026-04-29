# Changelog

All notable changes to `tapo-cli`.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
