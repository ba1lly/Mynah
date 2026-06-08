# Changelog

All notable changes to Mynah are documented here.
This project follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] — 2026-06-08

First public release.

### Highlights

- **Local-only audio capture** — Windows WASAPI loopback records your mic plus the system audio mix (so screen-shares, music, and every other participant's voice all get captured). No bot ever joins the Discord channel; nothing leaves your machine.
- **Ground-truth speaker labels from Discord** — Mynah subscribes to Discord's per-user `SPEAKING_START` / `SPEAKING_STOP` events over the local RPC named pipe. Transcript labels reflect who Discord says was actually speaking at each moment, not acoustic clustering. Heuristic diarization (pyannote) is the fallback for non-Discord apps or when SPEAKING events are missing.
- **WhisperX transcription with per-word alignment** — Whisper handles the speech-to-text; `whisperx.align()` lines up each word's timestamp with the speaker timeline, so the transcript splits at the right places when two people overlap.
- **Secrets in the OS credential store** — Discord OAuth tokens, the HuggingFace token, and the client secret are stored in Windows Credential Manager (or macOS Keychain / Linux Secret Service on supported platforms). `config.json` carries only non-sensitive settings.
- **Recording consent gate** — Mynah displays a privacy attestation dialog before any audio capture begins. The attestation is recorded into the recording's `participants.json` for auditability.
- **Discord pipe peer verification** — The local Discord IPC pipe is verified against a trusted install root and an Authenticode signature check, so a same-user attacker can't squat the pipe and harvest tokens.
- **Standalone `.exe` build** — `start.ps1 -Build` produces `dist/Mynah/Mynah.exe` (PyInstaller, ~4 GB bundled with PyTorch + CUDA). The `.exe` runs without Python or Git on the target machine.
- **CycloneDX 1.5 SBOM + bundled-DLL manifest** — Every build emits an SBOM of the runtime dependency closure and a hash manifest of every native binary inside the bundle.

### System

- Windows 10 1903+ / Windows 11.
- NVIDIA GPU recommended (RTX 20-series or newer, ≥ 6 GB VRAM); CPU works but is ~10× slower than realtime.
- ~10 GB of disk for Python deps + Whisper weights; ~15 GB recommended.
- Python 3.10–3.13 for dev mode; not needed for the `.exe` build.

[1.0.0]: https://github.com/ba1lly/Mynah/releases/tag/v1.0.0
