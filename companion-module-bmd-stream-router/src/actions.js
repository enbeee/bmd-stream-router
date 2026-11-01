'use strict'

module.exports = function updateActions(self) {
	const destChoices = () =>
		self.endpoints().destinations.map((d) => ({ id: d.id, label: d.name }))
	const sourceChoices = () =>
		self.endpoints().sources.map((s) => ({ id: s.id, label: s.name }))
	const gatewayDestChoices = () =>
		self.endpoints()
			.destinations.filter((d) => d.type === 'TYPE_GATEWAY_DESTINATION')
			.map((d) => ({ id: d.id, label: d.name }))

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
			},
			subscribe: () => self.updateActions(), // refresh choice lists when state changes
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
			},
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
			},
		},
		sink_provider: {
			name: 'Streaming output: predefined platform',
			description: 'Point a Streaming Output at a predefined provider (YouTube RTMP, Twitch, Castr, ...)',
			options: [
				{ id: 'destination', type: 'dropdown', label: 'Streaming output', choices: gatewayDestChoices() },
				{ id: 'providerId', type: 'textinput', label: 'Provider ID', tooltip: 'From GetGatewayEgressSinkProviders, e.g. gateway_egress_sink_provider_...' },
				{ id: 'urlId', type: 'textinput', label: 'Server URL ID', tooltip: 'From GetGatewayEgressSinkProviders, e.g. gateway_egress_sink_url_...' },
				{ id: 'streamKey', type: 'textinput', label: 'Stream key' },
				{ id: 'passphrase', type: 'textinput', label: 'Passphrase (optional)' },
			],
			callback: async (event) => {
				try {
					await self.client.setSinkProvider(
						self.routerId,
						event.options.destination,
						event.options.providerId,
						event.options.urlId,
						event.options.streamKey,
						event.options.passphrase
					)
				} catch (e) {
					self.log('error', `set sink failed: ${e.message}`)
				}
			},
		},
		sink_custom: {
			name: 'Streaming output: custom RTMP service',
			description: 'Point a Streaming Output at your own RTMP server (works via API even though the web UI hides the custom option)',
			options: [
				{ id: 'destination', type: 'dropdown', label: 'Streaming output', choices: gatewayDestChoices() },
				{ id: 'url', type: 'textinput', label: 'RTMP URL', tooltip: 'e.g. rtmp://server.example.com/live' },
				{ id: 'streamKey', type: 'textinput', label: 'Stream key' },
				{ id: 'username', type: 'textinput', label: 'Username (optional)' },
				{ id: 'passphrase', type: 'textinput', label: 'Passphrase (optional)' },
			],
			callback: async (event) => {
				try {
					await self.client.setSinkCustom(self.routerId, event.options.destination, event.options.url, event.options.streamKey, {
						username: event.options.username,
						passphrase: event.options.passphrase,
					})
				} catch (e) {
					self.log('error', `set custom sink failed: ${e.message}`)
				}
			},
		},
	})
}
