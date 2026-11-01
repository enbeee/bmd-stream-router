# Blackmagic Cloud Stream Router — Protocol Reference

Blackmagic's free Cloud Stream Router is a 4-input SRT router: encoders (DaVinci
Resolve, ATEM, or any software encoder via the bridge described here) stream into
it; the router forwards each input to viewing outputs and to "Streaming Outputs"
that push to external destinations (YouTube, Twitch, or custom RTMP/SRT URLs).

Everything in this document was verified against the live service (Oct 2026).
Reference implementations: `bmd_claim.py` (control/user planes) and
`bmd_bridge.py` (SRT edge with BMOS announce). Requirements:
`pip install httpx[http2] cryptography` plus `credentials.md` (email line 1,
password line 2) next to the scripts.

---

## 1. System overview

Three independent planes:

| plane | endpoint | protocol | purpose |
|---|---|---|---|
| Auth | `auth.cloud.blackmagicdesign.com` | OAuth2 (Auth0) | bearer tokens |
| Device plane (claim) | `bmd-api.cloud.blackmagicdesign.com` | gRPC over HTTP/2 | list inputs, claim an input, mint per-session SRT credentials |
| User plane (config) | `web-api.cloud.blackmagicdesign.com` | Connect-RPC JSON | router state, routes, Streaming Output destinations |
| SRT edge (media) | `216.66.81.x:<port>` (per session) | SRT, AES-128 encrypted | carries the MPEG-TS |

The web app at cloud.blackmagicdesign.com uses the user plane; Resolve/ATEM use
the device plane plus the SRT edge. A bearer token from one password grant is
accepted by both API planes.

**End-to-end stream path:** encoder → (SRT edge) → router input → route →
destination (viewing output, or Streaming Output → external push URL).

---

## 2. Product model (user plane state)

A router (`router_6_…`, name, region e.g. `eu-west-2`) contains:

- **sources** — the 4 inputs (`type: TYPE_SOURCE`), each `endpoint_…` with
  `status`: `STATUS_UNCLAIMED` / `STATUS_INACTIVE` (claimed, not streaming) /
  `STATUS_ACTIVE` (live), plus `deviceMetadata` and the claiming `user`.
- **destinations** —
  - viewing outputs: `type: TYPE_DESTINATION` ("Output 1..3") — other Cloud
    users/apps watch these;
  - streaming outputs: `type: TYPE_GATEWAY_DESTINATION` ("Streaming Output 1")
    with a `gateway` object:
    ```
    gateway: {
      live: true,
      streamKey: "…",                  # provider stream key
      predefinedSinkConfig: {          # OR customSinkConfig
        providerId: "gateway_egress_sink_provider_…",
        urlId:    "gateway_egress_sink_url_…"
      },
      customSinkConfig: { urls: ["rtmp://…", "srt://…"], passphrase: "…" }
    }
    ```
- **routes** — list of `{source: endpoint_id, destination: endpoint_id}`.

---

## 3. Authentication

`POST https://auth.cloud.blackmagicdesign.com/oauth/token`:

```
grant_type=password
client_id=ydUVBMZNONl563WQBtGo9p7gYnYAj9PS      (Resolve's public app id)
client_secret=LhiETUC0arqEVmYzTROkDbkNYE8ND2foVYFOYAMEd3qvzSBnytVTV6HQNI7PsY9
username=<BMD Cloud email>
password=<password>
scope=offline_access openid profile email read:current_user update:current_user_metadata
audience=https://bmd-api.cloud.blackmagicdesign.com/
```

Returns a ~1 h JWT (`access_token`) + `refresh_token`. The same access token is
accepted by the user plane. (The web app's own client
`GXME7NMpelSg0biEqCuLJp8y5k50toDx` does not allow password grants; it uses the
browser session's refresh token.)

---

## 4. Device plane — gRPC claim API

Base: `https://bmd-api.cloud.blackmagicdesign.com/bmd.cloud.bmdapi.streaming.v1.StreamRouterEdgeDeviceService/<Method>`

Framing: standard gRPC (`1-byte compressed flag + 4-byte BE length + protobuf`);
responses may be gzip (flag=1). Headers on every call:
`authorization: Bearer …`, `x-api-key: Md2oJrLfDP+kLQMOTPufI`,
`content-type: application/grpc`, `te: trailers`, plus `region: eu-west-2` on
Claim/Connect/Unclaim.

### GetEndpoints — `{f1: 1, f2: deviceId, f4: 100}`
Repeated f1 per input: `f1 endpoint_id · f2 "Input N" · f3 router_id · f7 state
(2/3 = free, 4 = claimed) · f8 1 = claimed · f9 region`.

### Claim — `{f1 router, f2 endpoint, f3 deviceId, f4 '{"device_name":"…"}'}`
Takes the seat for `deviceId`. An occupied input returns 200 with an empty
body (no error) — the reliable success signal is the following Connect.

### Connect — `{f1 router, f2 endpoint, f3 deviceId}`
Response (the **session credential set**):

| field | meaning | use |
|---|---|---|
| 1 | SRT edge `host:port` (port varies: 7002, 7008…) | dial this |
| 2 | claim (uuid7) | streamid `u=` |
| 3 | secret (uuid4, rotates per Connect) | **SRT passphrase** |
| 4 | expiry (uuid7; same mint ms as field 2) | streamid `s=` |

### Unclaim — same request shape as Connect. Releases instantly.

Session rules: credential lapses ~2 min after Connect if no stream attaches;
while streaming the seat holds (no keep-alive Connect needed); one caller per
input, IP-independent; Unclaim kills a live session.

---

## 5. User plane — Connect-RPC config API

Base: `https://web-api.cloud.blackmagicdesign.com/bmd.cloud.webapi.streaming.v1.StreamRouterService/<Method>`

Unary calls: `POST` JSON body, headers `Content-Type: application/json`,
`Connect-Protocol-Version: 1`, `Authorization: Bearer …`. Streaming calls use
`Content-Type: application/connect+json` and a 5-byte envelope per frame
(`0x00` + 4-byte BE length + JSON) in both directions.

| method | request | notes |
|---|---|---|
| `GetRouters` | `{}` | `{routers: [{id, name, region, …}]}` |
| `GetAndWatchRouter` | `{id}` | server stream; each frame `{router: …}` (§2 model); stays open |
| `UpdateRoutes` | `{routerId, upserts: [{source, destination}], deletions: [{source, destination}]}` | route / unroute |
| `UpdateEndpoint` | `{routerId, id, …fields}` | for streaming outputs pass `gateway` (§2 shape); `gateway: {}` (clearing) errors — replace configs instead |

---

## 6. SRT edge protocol

The edge is a **custom SRT listener**. Caller handshake (libsrt-compatible)
with the standard extensions plus one proprietary block:

1. induction (48-byte UDT handshake, cookie 0) → listener responds with cookie
2. conclusion, version 5, ext-flags word `0x7` (HSREQ|KMREQ|CONFIG), extensions:
   - `HSREQ` (type 1): `00010500 000000bf 008f008f`
   - `SID` (type 5): the credential string (below), each 4-byte chunk stored
     little-endian (wire captures therefore show reversed chunks)
   - `KMREQ` (type 3): AES-128 keying material (below)
   - **`0xBD01` (24 bytes)** — the BMOS announce:
     `424d4f5301000100 0008000000080000 02000100 <u16> 0000`
     Without it the relay completes the handshake and ACKs all data but
     **never routes the stream** — the 20-byte-shorter agreement (140 vs
     160 B, no BD01 echo) is the observable difference. Stock libsrt
     (OBS, ffmpeg) cannot send custom handshake extensions — use the bridge.
3. agreement → contains the listener socket id (body word 6); relay echoes the
   BD01 block.

**ACKs on the sender leg** (full SRT ACKs, type 2, ~1.8 ms cadence under load):

- header type-specific `info` = **ACK event counter** (increments once per ACK:
  1, 2, 3, … — it is *not* the data ack)
- payload word 0 = **`last_ack`**, the next-expected sequence number in the
  *sender's own* ISN space; then RTT (µs), RTT variance, receive-buffer size
  (8192 packets, constant), packet rate, link capacity, recv rate
- the sender answers every ACK with an ACKACK (type 6) echoing the event
  counter
- flow control: the receiver window is 8192 packets; a paced sender stays at
  ≤ ~10 un-acked. A sender that mistakes `info` for the data ack computes
  `sent − acked` growing ~1 per packet and stalls itself at its own window
  limit (≈ 2048 pkts ≈ 2.7 MB) — which looks exactly like a server-side
  per-session cap.

**Stream id (the "stream key")** — plain credential string, no transform:

```
#!::s=<ConnectResponse field 4>,u=<ConnectResponse field 2>
```

**Encryption (PBKEYLEN 16 / AES-128)**, all libsrt-compatible:

- passphrase = ConnectResponse field 3 verbatim (36 chars)
- KEK = PBKDF2-HMAC-SHA1(passphrase, salt[-8:], 2048 iterations, 16 B)
- SEK = random 16 B; KMREQ = `1220290100000000 0200 0200 0000 0404` ‖ salt(16)
  ‖ RFC3394-wrap(KEK, SEK) (24 B)
- data packets: AES-CTR(SEK), IV = `salt[0:10] ‖ (salt[10:14] ⊕ seq_be) ‖ 0x0000`
  with a 16-bit block counter at IV bytes 14–15; header word 2 =
  `0xE8000000 | msgno`; payload = one BMOS chunk (below), not MPEG-TS

Encoder-side session habits observed from Resolve (the bridge replicates
them; the relay also tolerates single-copy sends): every outgoing datagram is
sent twice ~10 ms apart with identical seq/msgno (receiver dedups by seq);
the handshake conclusion twice; **no** mid-session key refresh exists — the
KM exchange happens only in the handshake. The relay sends an SRT `shutdown`
~5 s after media stops; a paused/stopped encoder therefore needs a re-dial
(the bridge does this automatically, minting a fresh claim if the old one
lapsed).

---

## 7. Putting a stream on the air (any encoder, no BMD hardware)

```
python3 bmd_bridge.py --claim          # claims a free input, listens on 127.0.0.1:9100
```

Point the encoder at `srt://127.0.0.1:9100?streamid=obs` (no passphrase),
start streaming within ~90 s of the claim. The bridge re-dials the relay with
the BMOS announce; `relay agreement 160B … BMOS-echo=YES` confirms activation.
Ctrl+C stops and releases the input.

Then ensure routing (one-time; also settable in the Cloud web UI):

```
python3 bmd_claim.py routers                                  # list routers
python3 bmd_claim.py router                                   # inspect state
python3 bmd_claim.py route <input-endpoint> <output-endpoint>
python3 bmd_claim.py setdest <streaming-output> --url rtmp://… [--key K] [--pass P]
                                       # or --provider <id> --url-id <id> --key K
```

With exactly one router on the account it is auto-selected (bridge, viewer
and these commands all do this); otherwise pass `--router <id>` or set
`BRIDGE_ROUTER=<id>`.

#### BMOS container (data channel)

After the handshake the relay-leg payload is NOT MPEG-TS. Small (single-chunk)
messages are one packet: 4-byte LE header `[1|last|len:14|counter:8|class:8]`
(bit 31 set, bit 30 = last, `len` = body length, `counter` = per-class rolling
message counter, `class` = 0/1/2) followed by the body. **Messages larger
  than 0x520 (1312) bytes are chunked BMD-style (byte-verified from Resolve
  captures and the davstream.dll receiver): only the FIRST chunk carries the
  BMOS header (with last=0); chunks 2..N carry a 4-byte continuation header
  instead — `[0x01][msg_counter][u16 LE len | 0x4000 on the FINAL PARTIAL
chunk][section bytes]` — and the message completes when the accumulated
section reaches the head chunk's outer length. The 0x4000 bit is the
last-marker the receiver's dispatcher requires: omitting it on a partial
final chunk leaves every multi-chunk message unassembled — the Remote
Monitor then connects and stays black (its receiver silently drops units
that never complete), and flagging exact-fit full-size finals instead makes
the RELAY itself stop ACKing within seconds. Repeating the BMOS header on
continuation chunks is forwarded by the relay but CRASHES the Remote
Monitor: BMD's parser treats every headered chunk as a new message head.**

- class 0 — `"BTST"` stream table: `"BTST" + 00000100` + 16-byte entries
  (video `[u32 0x30][u32 1][fourCC "H264"/"H265"][u32 0]`, audio
  `[0][0]["aac "][0]`), padded to 16. Re-send every ~50 frames.
- class 1 — one video message per frame:
  `[u32 LE ts = PTS_90kHz<<2][12 zero bytes][u32 LE outer = section_len |
  0x80000000 for keyframes][section]`, section = concatenated
  `[u32 BE nal_len][bare NAL]` (no Annex-B start codes). A GOP message is
  VPS+SPS+PPS+IDR **in one message**; P messages are a single TRAIL slice.
  Timestamps run at 360 kHz.
- class 2 — one audio message per ADTS frame:
  `[u32 LE ts][u32 0][u32 LE len][ADTS frame]`.

The router forwards messages **verbatim** (live-verified: every unique chunk
body sent re-arrived byte-identical, keyframes included). It does not parse
or rewrite them — what the encoder sends is what every output receives.

#### Encoder requirements (the stream must contain keyframes!)

The single failure that makes every output dark while the router still shows
routed activity: a stream whose keyframe slices never reach the router. H.265
encoders default to **open GOP** — every keyframe after the first is
CRA_NUT (NAL 21), not IDR — so a remuxer that keeps only IDR NAL types
silently deletes all keyframes. Symptoms of a keyframe-less stream: YouTube
egress dark, Remote Monitor crash on join, web-UI input tiles dark while
output tiles light. Rules the bridge enforces:

- H.264: keep NAL 1/5/7/8 (x264's closed-GOP IDR 5 is fine as-is).
- H.265: keep NAL 0/1/19/20/**21**/32/33/34. IDR_N_LP (20) is safely
  retyped to IDR_W_RADL (19) — identical slice-segment header syntax.
  **CRA (21) must NOT be retyped** (CRA slice headers differ from IDR: the
  router stops forwarding that input after a couple of retyped messages) —
  prefer closed GOP in the encoder (`x265: open-gop=0`; OBS/NVENC defaults
  are closed). RASL/RADL (8/9) leading pictures are dropped.
- Strip SEI and AUD NALs. Emit paramsets + IDR as ONE class-1 message.

### OBS specifically

Either the bridge (above, custom server `srt://127.0.0.1:9100?streamid=obs`),
or fully driven:

```
python3 bmd_claim.py stream --obs      # injects the key into OBS and starts
```

Note `stream --obs` (no bridge) completes the handshake and the relay even
forwards the raw MPEG-TS to routed outputs — but it is plain TS, not BMOS:
BMD's consumers (Remote Monitor, Streaming Output egress, the Python
viewer) find no decodable messages in it. The bridge is required for
generic encoders.

### Native clients

Resolve (Remote Monitoring → Stream via Blackmagic Cloud) and ATEM devices do
the same device-plane flow with the BMOS announce built in. Their streamids
use device markers in the same credential fields.

### Software viewer (receiving an output)

`bmd_view.py` claims a viewing output like the Remote Monitor does and
re-muxes the BMOS stream back to standards-shaped MPEG-TS with both tracks
(video H.264/H.265 + AAC audio, PMT/PES/PTS/PCR): `--out FILE.ts` for a
VLC-playable recording, `--push rtmp://…` to relay it on (bit-exact copy),
`--play` for live ffplay, plus `--raw`/`--wire` dumps for offline analysis:

```
python3 bmd_view.py <output-endpoint> --play     # after the source is live
```

The same output feeds **Blackmagic's Remote Monitor app** with no extra
setup: sign into the app with the BMD Cloud account, select the router,
and open the output the input is routed to (join after the source is
ACTIVE, same as the viewer).

Viewer-side requirements (all live-verified):

- **Join after the source is ACTIVE.** A viewer that connects before the
  source's relay session exists receives nothing (Connect can still succeed
  on the claim alone). Poll `router` state first.
- **Crypto:** the relay tunnels the *source's* SEK to each viewer: the
  agreement's KMRSP wraps either a pre-source placeholder or the source key,
  and a mid-stream KM control packet may deliver the real one at any time —
  keep a key ring, not a single key. When pairing with a local bridge, pass
  its `BRIDGE_DUMP_KEYS` file via `VIEW_EXTRA_KEYS=<file>`.
- **ACK like stock SRT:** one full ACK per ~10 ms carrying the cumulative
  ack number in the header's info field. ACK-flooding (per-packet light
  ACKs) or sending a wrong ack number makes the relay stop forwarding to the
  viewer after the first couple of multi-chunk GOP messages — silently, with
  the session left open.

---

## 8. Troubleshooting

| symptom | cause |
|---|---|
| connect times out ~3 s, 0 bytes | streamid/passphrase wrong, no PBKEYLEN, claim expired, or seat taken |
| handshake OK, router routes, but every output dark (YT dark, Remote Monitor crashes, input tiles dark while output tiles light) | the stream carries no keyframe slices — open-GOP H.265 encoder (CRA keyframes) through a filter that drops NAL 21; use closed GOP / a current bridge |
| handshake OK, nothing routed at all | BMOS announce missing — encoder not using the bridge |
| `relay ignored the conclusion` (bridge) | stale credential — re-run `--claim` |
| bridge log `edge rejected the session` on re-dial | claim lapsed; with `--claim` the bridge re-claims automatically, otherwise restart it |
| viewer receives ~2 GOP messages then silence | viewer ACK pattern (see § viewer requirements) |
| viewer connects before source live, receives nothing | join after the source is ACTIVE |
| Claim 200-empty then Connect without fields | input occupied; another input |
| works once, later fails | session values rotate per Connect; re-claim |
| streaming output not pushing | check `router` output shows the route and gateway `live: true`; verify stream key/destination |

## 9. Scope

Verified end-to-end: login, claim, connect, SRT handshake + BMOS activation
(160-byte agreement), BMOS remux of live x265 encodes with **all keyframe
messages forwarded by the router (50/50 over a 100 s run)**, and full
receive-side decode (2 374 HEVC frames from the wire dump), plus
router/routes/destination API and bridge auto-reconnect across encoder
stop/start cycles. Session uuids are ~2-minute-lived values; examples here
are dead sessions. `client_id`/`client_secret`/`x-api-key` are Blackmagic
app constants shipped inside Resolve, not account secrets.
