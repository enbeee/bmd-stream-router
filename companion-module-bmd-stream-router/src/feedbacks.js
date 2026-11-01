'use strict'

/**
 * Real input statuses (verified): STATUS_UNCLAIMED, STATUS_INACTIVE
 * (claimed, not streaming), STATUS_ACTIVE (live). Destination endpoints
 * stay STATUS_UNCLAIMED until a Blackmagic device claims them; a gateway
 * (Streaming Output) shows STATUS_ACTIVE only while a consumer/pusher is
 * actually attached, gateway.live is the configured switch.
 */

const STATUS_CHOICES_NOTE = 'unclaimed / inactive (claimed, idle) / active (live)'

module.exports = function updateFeedbacks(self) {
	const destChoices = () =>
		self.endpoints().destinations.map((d) => ({ id: d.id, label: d.name }))
	const sourceChoices = () =>
		self.endpoints().sources.map((s) => ({ id: s.id, label: d_name(s) }))

	function d_name(s) {
		return s.name
	}

	self.setFeedbackDefinitions({
		destination_source: {
			type: 'boolean',
			name: 'Destination is routed from source',
			description: 'Green when the selected destination is currently routed from the selected source',
			options: [
				{ id: 'destination', type: 'dropdown', label: 'Destination', choices: destChoices(), default: destChoices()[0]?.id },
				{ id: 'source', type: 'dropdown', label: 'Source', choices: sourceChoices(), default: sourceChoices()[0]?.id },
			],
			defaultStyle: { bgcolor: 0x008800, fgcolor: 0xffffff },
			callback: (event) => {
				const rt = self.routeFor(event.options.destination)
				return !!rt && rt.source === event.options.source
			},
			subscribe: () => self.updateFeedbacks(),
		},

		source_live: {
			type: 'boolean',
			name: 'Input is LIVE (active)',
			description: 'Active when the input has a streaming session (STATUS_ACTIVE)',
			options: [
				{ id: 'source', type: 'dropdown', label: 'Input', choices: sourceChoices(), default: sourceChoices()[0]?.id },
			],
			defaultStyle: { bgcolor: 0xcc0000, fgcolor: 0xffffff },
			callback: (event) => {
				const ep = self.endpoints().sources.find((s) => s.id === event.options.source)
				return !!ep && ep.status === 'STATUS_ACTIVE'
			},
			subscribe: () => self.updateFeedbacks(),
		},

		source_claimed: {
			type: 'boolean',
			name: 'Input claimed by a device',
			description: `Active when claimed but not streaming (STATUS_INACTIVE). Statuses: ${STATUS_CHOICES_NOTE}`,
			options: [
				{ id: 'source', type: 'dropdown', label: 'Input', choices: sourceChoices(), default: sourceChoices()[0]?.id },
			],
			defaultStyle: { bgcolor: 0x884400, fgcolor: 0xffffff },
			callback: (event) => {
				const ep = self.endpoints().sources.find((s) => s.id === event.options.source)
				return !!ep && ep.status === 'STATUS_INACTIVE'
			},
			subscribe: () => self.updateFeedbacks(),
		},

		gateway_live: {
			type: 'boolean',
			name: 'Streaming Output gateway live',
			description: 'Active when the gateway switch is on (egress configured to push)',
			options: [
				{ id: 'endpoint', type: 'dropdown', label: 'Streaming Output', choices: self.endpoints().destinations.filter((d) => d.type === 'TYPE_GATEWAY_DESTINATION').map((d) => ({ id: d.id, label: d.name })), default: self.endpoints().destinations.find((d) => d.type === 'TYPE_GATEWAY_DESTINATION')?.id },
			],
			defaultStyle: { bgcolor: 0x006688, fgcolor: 0xffffff },
			callback: (event) => {
				const ep = self.destinationById(event.options.endpoint)
				return !!(ep && ep.gateway && ep.gateway.live)
			},
			subscribe: () => self.updateFeedbacks(),
		},

		gateway_status: {
			type: 'boolean',
			name: 'Streaming Output is pushing (egress attached)',
			description: 'Active when the endpoint shows STATUS_ACTIVE - a consumer/pusher is attached right now',
			options: [
				{ id: 'endpoint', type: 'dropdown', label: 'Streaming Output', choices: self.endpoints().destinations.filter((d) => d.type === 'TYPE_GATEWAY_DESTINATION').map((d) => ({ id: d.id, label: d.name })), default: self.endpoints().destinations.find((d) => d.type === 'TYPE_GATEWAY_DESTINATION')?.id },
			],
			defaultStyle: { bgcolor: 0x00cc66, fgcolor: 0xffffff },
			callback: (event) => {
				const ep = self.destinationById(event.options.endpoint)
				return !!ep && ep.status === 'STATUS_ACTIVE'
			},
			subscribe: () => self.updateFeedbacks(),
		},
	})
}
