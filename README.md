# tapo-cli

Deterministic, scriptable command-line tool for discovering, querying, and controlling TP-Link Tapo cameras and wired doorbells on the local LAN. Sister project to [`kasa-cli`](https://github.com/agileguy/kasa-cli) — same product philosophy, different protocol surface.

**Status:** Pre-alpha. SRD frozen at v1.1.1. Phase 0 (hardware smoke-test gate) in progress.

---

## What it is

A single CLI binary that takes a verb, a target, and flags, performs one operation against one or more Tapo devices over the local network, prints a result on stdout, and exits with a meaningful status code. The leaf node in a shell pipeline or cron job — nothing more.

```
tapo-cli discover                                # find cameras on the LAN
tapo-cli info @front-door                        # device state
tapo-cli snapshot @front-door --output snap.jpg  # still image
tapo-cli stream @front-door                      # emit RTSP URL
tapo-cli privacy @front-door enable              # privacy mode
tapo-cli ptz @front-door pan left --step 15      # pan/tilt/zoom
tapo-cli motion history @front-door --since 1h   # motion events
```

## What it is not

- Not a media player — `stream` emits a URL; pipe to `mpv`/`ffmpeg` yourself.
- Not an NVR — `record` is a one-shot foreground subprocess with `--duration` cap.
- Not a rules engine — Home Assistant or Node-RED handle automation.
- Not a cloud daemon — local LAN only, no port forwarding, no TP-Link cloud relay.
- Not Kasa — see [`kasa-cli`](https://github.com/agileguy/kasa-cli) for plugs, bulbs, strips, switches.

## Authoritative spec

[`docs/SRD-tapo-cli.md`](docs/SRD-tapo-cli.md) — Software Requirements Document, v1.1.1.

The SRD is the system of record. README is a courtesy summary. If they disagree, the SRD wins.

## Credentials

Cloud-account credentials are **shared with `kasa-cli`** by default — same TP-Link login, same `~/.config/kasa-cli/credentials` JSON file (read-only from `tapo-cli`). See SRD §6 / FR-CRED-3.1 for the dual-credential model (cloud account + per-device camera account).

## Phase plan

| Phase | Scope | Status |
|-------|-------|--------|
| 0 | Hardware smoke-test gate (pytapo SHA pin + per-camera fixture capture) | In progress |
| 1 | MVP — discover, list, info, snapshot, stream, basic state, reboot | Pending Phase 0 |
| 2 | PTZ, presets, alarm, audio, OSD | Pending Phase 1 |
| 3 | record, motion history, groups, batch, signal handling | Pending Phase 2 |
| 4 | Reserved (no commitment) | Deferred |

## License

MIT.
