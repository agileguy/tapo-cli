# tapo-cli

Deterministic, scriptable command-line tool for discovering, querying, and controlling TP-Link Tapo cameras and wired doorbells on the local LAN. Sister project to [`kasa-cli`](https://github.com/agileguy/kasa-cli) — same product philosophy, different protocol surface.

**Status:** v0.3.0 — feature-complete for v1. SRD frozen at v1.1.1. All Phase 0 / 1 / 2 / 3 verbs ship, verified against a live Tapo C200 at every phase boundary.

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
| 0 | Hardware smoke-test gate (pytapo SHA pin + per-camera fixture capture) | Shipped |
| 1 | MVP — discover, list, info, snapshot, stream, basic state, reboot | Shipped |
| 2 | PTZ, presets, alarm, audio, OSD | Shipped |
| 3 | record, motion history, groups, batch, signal handling | Shipped (v0.3.0) |
| 4 | Reserved (no commitment) | Deferred |

## v0.3.0 verbs

Every in-scope SRD verb ships in v0.3.0. The full surface:

| Verb | What it does |
|------|--------------|
| `auth status / flush / migrate` | pytapo session-cache management + credential migration |
| `config show / validate` | inspect / lint the resolved TOML config |
| `discover` | LAN-scan for Tapo cameras over WS-Discovery |
| `list` | enumerate configured aliases |
| `info <target>` | full Camera record over pytapo `getBasicInfo` |
| `snapshot <target> --output PATH` | three-mechanism JPEG capture (pytapo → ONVIF → ffmpeg) |
| `stream <target>` | emit `rtsp://...` URL on stdout (Unix philosophy; pipe to ffmpeg/mpv) |
| `record <target> --output PATH` | one-shot ffmpeg recording with `--duration` / `--max-bytes` cap |
| `privacy <target> enable\|disable\|status` | privacy-mode (lens cover / feed disable) |
| `led <target> on\|off\|status` | front status LED |
| `night-vision <target> auto\|on\|off\|ir-only` | night-vision mode |
| `motion <target> enable\|disable\|status` | motion-detection toggle + sensitivity report |
| `motion history <target> [--since ... --limit ... --event-type ...]` | RFC 3339 UTC motion-event timeline |
| `reboot <target>` | reboot the camera (tty prompt + `--yes` non-tty guard) |
| `ptz <target> pan\|tilt\|zoom\|move\|stop` | pan / tilt / zoom motors |
| `preset <target> list\|goto\|save\|delete` | saved-position registry |
| `alarm <target> enable\|disable\|trigger\|status` | siren control |
| `audio <target> volume\|mic\|speaker\|tts` | speaker volume / mic-mute / TTS playback |
| `osd <target> set\|clear\|status` | on-screen-display overlay |
| `groups list` | read-only group enumeration (mutations by hand-editing config) |
| `batch --stdin\|--file` | newline-delimited sub-command runner with B10 JSONL output |

## License

MIT.
