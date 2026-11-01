# Bitfocus Companion module: Blackmagic Cloud Stream Router

Controls a Blackmagic Cloud Stream Router from Bitfocus Companion: switch inputs to
outputs, disconnect routes, rename endpoints, and configure Streaming Output services
(including custom RTMP servers the web UI does not offer).

## Install (dev mode)

1. `cd companion-module-bmd-stream-router && npm install`
2. Copy/symlink this folder into Companion's module search path, or run Companion in dev
   mode pointing at it (Companion 3: Settings -> Developer -> add module dir), then restart.
3. Add the "bmd-stream-router" connection.

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
- **Disconnect destination**
- **Rename endpoint**
- **Streaming output: predefined platform** (providerId/urlId from the provider catalog,
  stream key, passphrase)
- **Streaming output: custom RTMP service** - your own server URL + key + username +
  passphrase. This works on the API even though the Blackmagic web UI only shows
  YouTube RTMP / Twitch / X / Restream.IO / Vimeo / BoxCast / Castr.

## Feedbacks & variables

- `destination_source`: green when a destination is routed from a given source
- `source_claimed`: active when a device has claimed an input
- Variables: `dest_N_source`, `dest_N_name`, `input_N_status`, `router_name`

## Verified behavior notes

- Routing: `UpdateRoutes {routerId, upserts|deletions: [{source, destination}]}`; changes
  apply immediately (the web UI's "Use Take" is client-side only).
- Gateway sink config: `UpdateEndpoint {routerId, id, gateway: {...}}`. Clearing a sink
  entirely is not supported upstream (empty gateway errors 503) - replace it instead.
- Destination endpoints stay `STATUS_UNCLAIMED` until a Blackmagic device claims them;
  routing works regardless.
