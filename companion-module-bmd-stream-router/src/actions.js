/**
 * Actions covering the Stream Router user-plane API (see BMD_STREAM_ROUTER.md §5):
 *  - UpdateRoutes   {routerId, upserts|deletions: [{source, destination}]}
 *  - UpdateEndpoint {routerId, id, name? | gateway?: {live, streamKey, ...}}
 * Gateway (Streaming Output) endpoints carry:
 *  gateway: { live, streamKey, username?, passphrase?,
 *             predefinedSinkConfig?: {providerId, urlId},
 *             customSinkConfig?: {urls: [...]} }
 * Clearing a gateway entirely (gateway: {}) errors upstream - replace configs.
 */

export function UpdateActions(self) {
	const destChoices = () =>
		self.endpoints().destinations.map((d) => ({ id: d.id, label: `${d.name} (${shortId(d.id)})` }))
	const sourceChoices = () =>
		self.endpoints().sources.map((s) => ({ id: s.id, label: `${s.name} (${shortId(s.id)})` }))
	const gatewayDestChoices = () =>
		self.endpoints()
			.destinations.filter((d) => d.type === 'TYPE_GATEWAY_DESTINATION')
			.map((d) => ({ id: d.id, label: `${d.name} (${shortId(d.id)})` }))

	function shortId(id) {
		return (id || '').slice(-6)
	}

	self.setActionDefinitions({
		route: {
			name: 'Route source to destination',
			options: [
				{ id: 'source', type: 'dropdown', label: 'Source', choices: sourceChoices(), default: sourceChoices()[0]?.id },
				{ id: 'destination', type: 'dropdown', label: 'Destination', choices: destChoices(), default: destChoices()[0]?.id },
			],
			callback: async (event) => {
				try {
					await self.client.route(self.routerId, event.options.source, event.options.destination)
				} catch (e) {
					self.log('error', `route failed: ${e.message}`)
				}
			} // refresh choice lists when state changes
		},

		route_numbers: {
			name: 'Route input N to destination (by number)',
			description: 'Fast crosspoint switch using the position in the router list (1-based)',
			options: [
				{ id: 'input', type: 'number', label: 'Input #', default: 1, min: 1, max: 16 },
				{ id: 'destination', type: 'dropdown', label: 'Destination', choices: destChoices(), default: destChoices()[0]?.id },
			],
			callback: async (event) => {
				const src = self.endpoints().sources[event.options.input - 1]
				if (!src) return self.log('error', `no input #${event.options.input}`)
				try {
					await self.client.route(self.routerId, src.id, event.options.destination)
				} catch (e) {
					self.log('error', `route failed: ${e.message}`)
				}
			}
		},

		disconnect: {
			name: 'Disconnect destination',
			options: [{ id: 'destination', type: 'dropdown', label: 'Destination', choices: destChoices(), default: destChoices()[0]?.id }],
			callback: async (event) => {
				const rt = self.routeFor(event.options.destination)
				if (!rt) return
				try {
					await self.client.unroute(self.routerId, rt.source, rt.destination)
				} catch (e) {
					self.log('error', `disconnect failed: ${e.message}`)
				}
			}
		},

		rename: {
			name: 'Rename endpoint',
			options: [
				{ id: 'endpoint', type: 'dropdown', label: 'Endpoint', choices: sourceChoices().concat(destChoices()) },
				{ id: 'name', type: 'textinput', label: 'New name' },
			],
			callback: async (event) => {
				try {
					await self.client.updateEndpoint(self.routerId, event.options.endpoint, { name: event.options.name })
				} catch (e) {
					self.log('error', `rename failed: ${e.message}`)
				}
			}
		},

		gateway_live: {
			name: 'Streaming Output: set live state',
			description: 'The gateway must be live for the egress pusher to run; INACTIVE once claimed/pulling',
			options: [
				{ id: 'endpoint', type: 'dropdown', label: 'Streaming Output', choices: gatewayDestChoices(), default: gatewayDestChoices()[0]?.id },
				{ id: 'live', type: 'checkbox', label: 'Live', default: true },
			],
			callback: async (event) => {
				const ep = self.destinationById(event.options.endpoint)
				// UpdateEndpoint merges fields; pass the existing gateway back with the new
				// live value (an empty gateway {} errors upstream - replace, don't clear).
				const gw = Object.assign({}, ep && ep.gateway ? ep.gateway : {})
				gw.live = !!event.options.live
				try {
					await self.client.updateEndpoint(self.routerId, event.options.endpoint, { gateway: gw })
				} catch (e) {
					self.log('error', `gateway update failed: ${e.message}`)
				}
			}
		},

		set_sink_provider: {
			name: 'Streaming Output: predefined platform sink',
			description: 'providerId/urlId from the provider catalog (e.g. YouTube RTMP "Primary")',
			options: [
				{ id: 'endpoint', type: 'dropdown', label: 'Streaming Output', choices: gatewayDestChoices(), default: gatewayDestChoices()[0]?.id },
				{ id: 'provider', type: 'textinput', label: 'Provider ID', default: '' },
				{ id: 'urlid', type: 'textinput', label: 'URL ID', default: '' },
				{ id: 'key', type: 'textinput', label: 'Stream key' },
				{ id: 'pass', type: 'textinput', label: 'Passphrase (optional)' },
			],
			callback: async (event) => {
				try {
					await self.client.setSinkProvider(self.routerId, event.options.endpoint,
						event.options.provider.trim(), event.options.urlid.trim(),
						event.options.key, event.options.pass || undefined)
				} catch (e) {
					self.log('error', `set sink failed: ${e.message}`)
				}
			}
		},

		set_sink_custom: {
			name: 'Streaming Output: custom RTMP/SRT sink',
			description: 'Your own push URL(s) - works on the API even though the web UI hides it',
			options: [
				{ id: 'endpoint', type: 'dropdown', label: 'Streaming Output', choices: gatewayDestChoices(), default: gatewayDestChoices()[0]?.id },
				{ id: 'url', type: 'textinput', label: 'Push URL (rtmp://… or srt://…)' },
				{ id: 'key', type: 'textinput', label: 'Stream key' },
				{ id: 'user', type: 'textinput', label: 'Username (optional)' },
				{ id: 'pass', type: 'textinput', label: 'Passphrase (optional)' },
			],
			callback: async (event) => {
				try {
					await self.client.setSinkCustom(self.routerId, event.options.endpoint,
						event.options.url.trim(), event.options.key,
						{ passphrase: event.options.pass || undefined, username: event.options.user || undefined })
				} catch (e) {
					self.log('error', `set sink failed: ${e.message}`)
				}
			}
		},
	})
}
