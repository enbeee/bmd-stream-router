export function UpdateVariableDefinitions(self) {
	const vars = {
		router_name: { name: 'Router name' },
		router_region: { name: 'Router region' },
		route_count: { name: 'Number of active routes' },
	}
	const { sources, destinations } = self.endpoints()
	destinations.forEach((d, i) => {
		vars[`dest_${i + 1}_name`] = { name: `Destination ${i + 1} name` }
		vars[`dest_${i + 1}_source`] = { name: `Destination ${i + 1} currently routed source name` }
		vars[`dest_${i + 1}_source_id`] = { name: `Destination ${i + 1} currently routed source endpoint id` }
		vars[`dest_${i + 1}_status`] = { name: `Destination ${i + 1} status` }
	})
	sources.forEach((s, i) => {
		vars[`input_${i + 1}_name`] = { name: `Input ${i + 1} name` }
		vars[`input_${i + 1}_status`] = { name: `Input ${i + 1} status (unclaimed/inactive/active)` }
	})
	self.setVariableDefinitions(vars)
	if (self.state) {
		self.setVariableValues(self.collectVariables())
	}
}
