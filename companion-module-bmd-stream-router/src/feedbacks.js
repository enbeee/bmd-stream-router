'use strict'

module.exports = function updateFeedbacks(self) {
	self.setFeedbackDefinitions({
		destination_source: {
			type: 'boolean',
			name: 'Destination is routed from source',
			description: 'Green when the selected destination is currently routed from the selected source',
			options: [
				{ id: 'destination', type: 'dropdown', label: 'Destination', choices: self.endpoints().destinations.map((d) => ({ id: d.id, label: d.name })) },
				{ id: 'source', type: 'dropdown', label: 'Source', choices: self.endpoints().sources.map((s) => ({ id: s.id, label: s.name })) },
			],
			defaultStyle: { bgcolor: 0x008800, fgcolor: 0xffffff },
			callback: (event) => {
				const rt = self.routeFor(event.options.destination)
				return !!rt && rt.source === event.options.source
			},
			subscribe: () => self.updateFeedbacks(),
		},
		source_claimed: {
			type: 'boolean',
			name: 'Source claimed by device',
			options: [
				{ id: 'source', type: 'dropdown', label: 'Source', choices: self.endpoints().sources.map((s) => ({ id: s.id, label: s.name })) },
			],
			defaultStyle: { bgcolor: 0x884400, fgcolor: 0xffffff },
			callback: (event) => {
				const ep = self.endpoints().sources.find((s) => s.id === event.options.source)
				return !!ep && ep.status === 'STATUS_CLAIMED'
			},
			subscribe: () => self.updateFeedbacks(),
		},
	})
}
