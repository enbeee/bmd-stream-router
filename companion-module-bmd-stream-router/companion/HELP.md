## Blackmagic Cloud Stream Router

Control Blackmagic's free Cloud Stream Router from Bitfocus Companion:
crosspoint routing, Streaming Output (gateway) destinations, and live status
feedback — no Blackmagic hardware needed on the Companion machine.

### Configuration

1. Log in at https://apps.cloud.blackmagicdesign.com/ in your browser.
2. DevTools (F12) → Application → Cookies → `apps.cloud.blackmagicdesign.com`.
3. Copy the value of the **`bmcloud.authToken`** cookie into the module config
   ("Session cookie"). The value is usually URL-encoded (`%7B%22body%22...`) -
   paste it as-is; the module decodes it automatically.
4. Router ID: leave blank to use the first router on the account.

**Session caveat:** the cookie carries a single active OAuth refresh-token
family. If the Blackmagic Cloud web app stays open anywhere (it rotates the
family in the background), refreshes fail — paste a fresh cookie then.

### Actions

- **Route source to destination** — live crosspoint switch (dropdowns of the
  router's inputs/outputs)
- **Route input N to destination** — fast switch by input number
- **Disconnect destination** — remove the route feeding a destination
- **Rename endpoint** — rename any input/output
- **Streaming Output: set live state** — the gateway switch; the egress
  pusher only runs while live
- **Streaming Output: predefined platform sink** — providerId/urlId (e.g.
  YouTube RTMP "Primary") + stream key (+ passphrase)
- **Streaming Output: custom RTMP/SRT sink** — your own push URL + key +
  username + passphrase (works on the API even though the web UI hides it)

### Feedbacks

- **Destination is routed from source** — green on an active crosspoint
- **Input is LIVE** — red while an input is streaming (STATUS_ACTIVE)
- **Input claimed by a device** — amber when claimed but idle (STATUS_INACTIVE)
- **Streaming Output gateway live** — blue when the gateway switch is on
- **Streaming Output is pushing** — green when the egress pusher is attached

### Variables

`router_name`, `router_region`, `route_count`, and per endpoint:
`input_N_name`, `input_N_status`, `dest_N_name`, `dest_N_source`,
`dest_N_source_id`, `dest_N_status`. Statuses are `unclaimed` /
`inactive` (claimed, idle) / `active` (live).

### Switching router or account

Endpoint ids are account/router-scoped. If you connect to a different router
(or a different BMD account), existing buttons keep their old endpoint values
and fail with "Value is not in the list of choices" - re-select the endpoint
in each action/feedback config once.

### Notes

- Endpoint statuses: `unclaimed` / `inactive` (claimed, not streaming) /
  `active` (live). Destination endpoints stay `unclaimed` until a Blackmagic
  device claims them; routing works regardless.
- Clearing a Streaming Output sink entirely is not supported by the cloud
  API (an empty gateway errors) — replace the sink config instead.
- Routing changes apply immediately; the web UI's "Use Take" is client-side.
