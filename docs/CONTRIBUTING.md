# Contributing

This is a personal tool. PRs are welcome but not the focus — most contributions land via direct commits by the author. The notes below are for forks, audit-passes, and the author's future self.

## Development setup

```bash
git clone https://github.com/agileguy/tapo-cli
cd tapo-cli
uv sync --all-extras --dev
uv run tapo-cli --version
```

That's it. `uv sync` builds the venv at `.venv/`, pulls the pytapo SHA-pinned dep, and wires the entry point. Use `uv run tapo-cli ...` for ad-hoc invocations against the working tree, or `uv tool install --reinstall .` to expose a shell-PATH binary off the working tree.

## Running tests

```bash
uv run pytest                    # full unit suite (mock-only, no hardware)
uv run pytest -x                 # stop on first failure
uv run pytest tests/test_snapshot_cmd.py -v
uv run pytest -k fanout          # name filter
```

```bash
uv run mypy src/                 # strict mode
uv run ruff check src/ tests/    # lint
uv run ruff format src/ tests/   # autoformat
```

The test suite ships **mock-only** unit tests for everything — pytapo, ONVIF, ffmpeg, signal handling, the lot. CI runs the unit suite on every push. Hardware tests live in the smoke script (next section); they are not part of the unit run.

## Test policy

**Two layers, both required.**

### 1. Mock-only unit tests

Every verb has a test file under `tests/` exercising:

- Happy path with a mock pytapo / ONVIF / ffmpeg.
- Every documented exit code reachable from at least one test.
- JSON key stability across mock cameras (per the §3.3.1 capability matrix).
- Signal handling where applicable (SIGINT during batch, record, follow).

Mock fixtures live in `tests/fixtures/`. Each model family has its own row in the capability matrix and gets its own mock — outdoor non-PTZ + siren (C320WS), outdoor TC-series (TC85), indoor non-PTZ (C100), indoor PTZ (C200), dual-lens with telephoto path (C225 in BOTH wide-only AND telephoto modes), wired doorbell (D230). The §12.1 SRD section enumerates the mock corpus contract.

CI uses pytest with `pytest-asyncio`, `pytest-cov`, and `mypy --strict`. A test that requires a real device must be skipped under `pytest` and live in `scripts/smoke.py` instead.

### 2. Hardware verification at phase boundaries

`scripts/smoke.py` is the hardware-acceptance script (SRD §16.0). It exercises pytapo `getBasicInfo`, pytapo `getStreamURL`, pytapo native snapshot, ONVIF `GetDeviceInformation`, ONVIF `GetProfiles`, ONVIF `GetSnapshotUri`, and an ffmpeg-from-RTSP single-frame capture against a real Tapo camera.

Run it before merging any phase to `main`:

```bash
uv run python scripts/smoke.py --camera 192.168.86.65 --camera-account-user <user> --camera-account-pass <pass>
```

A camera failing a tier triggers either a §3.3.1 matrix update (move the model into the right capability row) or a flag of "untested in v1" pending firmware-specific investigation. Phase boundaries do NOT pass without a green smoke run.

The integration-test environment variables (SRD §12.2) — `TAPO_TEST_DEVICE_IP`, `TAPO_TEST_DOORBELL_IP`, `TAPO_TEST_PTZ_DEVICE_IP` — gate per-test live-device runs. Default-unset; CI never sets them.

## pytapo SHA pin policy

`pyproject.toml` pins pytapo to a specific git SHA (FR-CRED-9 cache invalidation depends on this):

```toml
"pytapo @ git+https://github.com/JurajNyiri/pytapo@de5ca3787c710151185bc72a35945d4091727c1e",
```

NOT a floating `>=3.4.13`. The HA 2025.11 / pytapo 3.3.51 incident (SRD §3.1) — where HA's pytapo pin became incompatible with shipping firmware overnight — proves single-maintainer firmware-coupled libraries need explicit SHA pinning. The session-cache `pytapo_version` field invalidates cached state on library bumps (FR-CRED-9), so a SHA roll triggers fresh handshakes on every cached camera.

**Rolling forward** requires:

1. Verify the new SHA against `scripts/smoke.py` on every hardware fixture you have.
2. Run the full `pytest` suite — most regressions surface there.
3. Bump the SHA in `pyproject.toml`.
4. Note the SHA bump in `CHANGELOG.md` with the upstream commit subject.
5. Cut a `tapo-cli` release.

Do not roll forward speculatively. Wait until a feature, bugfix, or security patch in upstream pytapo justifies the move.

## Phase-by-phase workflow

Each SRD phase ships as ONE PR against `main` with its own reviewer pass. Phases are kept atomic — no "merge half of Phase 4b and finish later" — so the exit criteria are cleanly verifiable.

### Per-phase checklist

1. **Branch.** `git checkout -b phase-Nx-<short-name>` off main.
2. **Atomic commits.** One commit per logical step (verb skeleton, verb impl, tests, capability gate, fan-out integration, etc.). Bisecting later is a feature.
3. **Tests before code, where it matters.** Test files like `tests/test_<verb>_cmd.py` should compile against the type signatures of the verb module before the impl exists. Red → green → refactor.
4. **Smoke against the live camera.** Phase-completion gates require a green `scripts/smoke.py` run.
5. **Reviewer pass.** Architect, Engineer, Researcher reviewers per SRD §17 v1.1.0 BLOCKING / SHOULD-FIX cadence. v1.0.0 caught 12 BLOCKING issues; v1.1.0 caught 16 SHOULD-FIX. The pattern shipped Phases 1-3 with zero post-merge surprises.
6. **CHANGELOG entry.** Per-phase changelog block with the FR identifiers shipped, the test count delta, and any deferred items.
7. **Merge with squash or rebase.** Personal preference is rebase-and-merge; the PR gives you the squash option if the commit graph got noisy.

### What gets deferred

The §16.4.5 Phase 4 out-of-scope list (and SRD §15) is the source of truth on deferred items:

- **Camera-account auto-creation.** No documented endpoint exists; pytapo does not expose it. Reverse-engineering would re-introduce the firmware-fragility this CLI was designed to avoid. v1.2.0 audit (2026-04-29) re-confirmed this. Stays out.
- **HDR / noise-cancelling / auto-track set flags.** No current operator pressure. v0.4+ candidate, not Phase 4.
- **Atomic-rename refactor (drop flock).** Settled in v1.1.0 reviewer block. flock + atomic-rename are both cheap; the simplification was rejected as observability regression.
- **HLS transcoding shim.** Tapo cameras don't natively serve HLS. `--protocol hls` exits 5; transcoding belongs in an external ffmpeg invocation.
- **Battery-doorbell third-party support.** D210/D235 in battery mode cannot accept a camera account (TP-Link upstream gap). Wired/always-on only.

When proposing a new feature, check those lists first. If it's there, make the case for re-opening; if not, drop it into a fresh SRD revision before implementing.

## Known TODOs

### py3.11 SIGINT-test skipif

Four tests are currently skipped on Python 3.11:

- `tests/test_motion_download_clip.py` — SIGINT-via-daemon-thread + `asyncio.wait_for` hangs on py3.11.
- `tests/test_events_cmd.py` — same.
- (Plus 2 more peers in the SIGINT-during-asyncio family.)

```python
_SKIP_SIGINT_ON_PY311 = pytest.mark.skipif(
    sys.version_info < (3, 12),
    reason="SIGINT-via-daemon-thread + asyncio.wait_for hangs on py3.11",
)
```

The fix is to rewrite them via `loop.add_signal_handler` instead of the daemon-thread + `os.kill` pattern. Pure refactor — the production code under test already supports both flows; only the test plumbing is the issue. Suitable side-quest for whoever next bumps the minimum Python.

### Other deferred items (post-Phase 4)

- **Phase 4c clip download `has_clip` heuristic.** Currently always `False` on emitted ONVIF events; the SD-card ±5s heuristic (FR-62) lands when Phase 4c integrates with `pytapo.getRecordings()`.
- **`groups add` / `groups remove` sub-verbs.** Comment-preserving TOML round-trip is a non-trivial side quest (FR-29b in kasa-cli's SRD; carries forward here). v1 keeps groups hand-edited.

## CHANGELOG conventions

The repo uses [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format with semver. Sections:

- `Added` — new verbs, new flags, new FR coverage.
- `Changed` — behaviour changes (cite the SRD revision that made them).
- `Fixed` — bugs.
- `Notes` — anything that doesn't fit the above (e.g. design rationale, upstream churn flags).

Each version entry references the SRD anchor (FR identifiers, sub-phase number) so the audit trail closes cleanly. Look at the v0.3.1 / v0.4.0 entries for the per-phase format — atomic commits, atomic FR references, atomic test-count deltas.

**No Claude / AI attribution.** The repo's CLAUDE.md is explicit on this. Commits and PRs do not mention LLMs.

## See also

- [`SRD-tapo-cli.md`](SRD-tapo-cli.md) — authoritative spec; §12 (testing strategy), §16 (phase plan), §17 (revision history).
- [`ARCHITECTURE.md`](ARCHITECTURE.md) — code layout and module responsibilities.
- [`USAGE.md`](USAGE.md) — verb reference (useful when adding a new verb to mirror existing conventions).
