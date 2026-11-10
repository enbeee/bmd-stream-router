# Bitfocus Companion module: Blackmagic Cloud Stream Router

Controls a Blackmagic Cloud Stream Router from Bitfocus Companion: switch inputs to
outputs, disconnect routes, rename endpoints, and configure Streaming Output services
(including custom RTMP servers the web UI does not offer).

## Install

**From a release package (recommended):** grab
`bmd-stream-router-<version>.tgz` from this repo's GitHub Releases
(`yarn package` / `npm run package` regenerates it), then in Companion:
Connections → Add module → import the file. The package declares
`runtime.apiVersion 2.1.3` (@companion-module/base 2.1.3), the range
Companion 5.x accepts for the v2 module contract.

**Headless / remote UI:** Companion disables the "Import Package" buttons for
web-UI clients that are not on the same machine (installing code over the
network is a security decision). Either of:

- Restart Companion with `--enable-restricted-modules` (or the env
  `COMPANION_ENABLE_RESTRICTED_MODULES=1`), import, then remove it again.
  Note the flag lets *any* remote client install code while it is set.
- Open the UI from inside the VM (`http://localhost:8000`) — loopback is
  always allowed.
- Skip the UI entirely: extract the `.tgz` and drop the contained
  `bmd-stream-router/` folder into Companion's modules directory as
  `<config-dir>/modules/bmd-stream-router-0.2.0/`, then restart Companion.
  The config dir is printed at startup (`configuration directory: …`;
  on Linux typically `~/.config/companion`).

**From source (dev mode):** the API version is read from the installed
`@companion-module/base`, so dependencies must exist first:

```sh
cd companion-module-bmd-stream-router && npm install
```

Then point Companion's developer module path at this folder and restart
Companion.

## Configuration

Paste the **`bmcloud.authToken` cookie value**:

1. Log in at https://apps.cloud.blackmagicdesign.com/ in your browser.
2. DevTools (F12) -> Application -> Cookies -> `apps.cloud.blackmagicdesign.com`.
3. Copy the value of `bmcloud.authToken` into the module config.

That cookie carries the access token (used first, ~1 h) and the OAuth refresh token (used
only when the access token nears expiry). **Caveat:** the refresh token is a single
active family - if the Blackmagic Cloud web app stays open anywhere (it rotates the
family in the background), refreshes will fail; paste a fresh cookie then. Logging in on
a new device can have the same effect.

## Actions

- **Route source to destination** - live crosspoint switch
- **Route input N to destination** - fast switch by input number
- **Disconnect destination**
- **Rename endpoint**
- **Streaming output: predefined platform** (providerId/urlId from the provider catalog,
  stream key, passphrase)
- **Streaming output: custom RTMP service** - your own server URL + key + username +
  passphrase. This works on the API even though the Blackmagic web UI only shows
  YouTube RTMP / Twitch / X / Restream.IO / Vimeo / BoxCast / Castr.
- **Streaming Output: set live state** - toggle the gateway switch (the egress
  pusher only runs while live)

## Feedbacks & variables

- `destination_source`: green when a destination is routed from a given source
- `source_live`: red when an input is streaming (STATUS_ACTIVE)
- `source_claimed`: amber when an input is claimed but idle (STATUS_INACTIVE)
- `gateway_live`: blue when a Streaming Output's gateway switch is on
- `gateway_status`: green when the egress pusher is attached (STATUS_ACTIVE)
- Variables: `router_name`, `router_region`, `route_count`,
  `dest_N_name/source/source_id/status`, `input_N_name/status`
  (statuses: unclaimed / inactive = claimed, idle / active = live)

## Verified behavior notes

- Routing: `UpdateRoutes {routerId, upserts|deletions: [{source, destination}]}`; changes
  apply immediately (the web UI's "Use Take" is client-side only).
- Gateway sink config: `UpdateEndpoint {routerId, id, gateway: {...}}`. Clearing a sink
  entirely is not supported upstream (empty gateway errors 503) - replace it instead.
- Destination endpoints stay `STATUS_UNCLAIMED` until a Blackmagic device claims them;
  routing works regardless.
