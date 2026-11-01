# bmd-stream-router

Pure-software tooling for Blackmagic's free **Cloud Stream Router**: stream
into it from any encoder (OBS, ffmpeg, …) with no Blackmagic hardware, watch
an output, and drive it from Bitfocus Companion.

- **`BMD_STREAM_ROUTER.md`** — complete protocol reference (auth, claim
  plane, config API, SRT edge, BMOS container, troubleshooting).
- **`bmd_bridge.py`** — the encoder bridge: listens for a local SRT stream,
  remuxes MPEG-TS to the router's BMOS format, claims an input, and keeps
  the relay session alive across encoder stop/start.
- **`bmd_claim.py`** — control-plane client (login, claim/connect, routes,
  streaming-output destinations; can drive OBS directly).
- **`bmd_view.py`** — viewer: claims a viewing output like the Remote
  Monitor and re-muxes the stream back to MPEG-TS (play / record / push).
- **`companion-module-bmd-stream-router/`** — Bitfocus Companion module.

Quick start (details in `BMD_STREAM_ROUTER.md` §7):

```sh
pip install "httpx[http2]" cryptography
printf 'your@email\nyourpassword\n' > credentials.md   # BMD Cloud login
python3 bmd_bridge.py --claim                          # claims a free input
# then point the encoder at  srt://127.0.0.1:9100?streamid=obs
```

`credentials.md` (and the runtime `session_claim.json`) are git-ignored —
never commit them.
