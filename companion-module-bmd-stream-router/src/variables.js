'use strict'

module.exports = function updateVariableDefinitions(self) {
	const vars = [
		{ variableId: 'router_name', name: 'Router name' },
		{ variableId: 'router_region', name: 'Router region' },
		{ variableId: 'route_count', name: 'Number of active routes' },
	]
	const { sources, destinations } = self.endpoints()
	destinations.forEach((d, i) => {
		vars.push({ variableId: `dest_${i + 1}_name`, name: `Destination ${i + 1} name` })
		vars.push({ variableId: `dest_${i + 1}_source`, name: `Destination ${i + 1} currently routed source name` })
		vars.push({ variableId: `dest_${i + 1}_source_id`, name: `Destination ${i + 1} currently routed source endpoint id` })
		vars.push({ variableId: `dest_${i + 1}_status`, name: `Destination ${i + 1} status` })
	})
	sources.forEach((s, i) => {
		vars.push({ variableId: `input_${i + 1}_name`, name: `Input ${i + 1} name` })
		vars.push({ variableId: `input_${i + 1}_status`, name: `Input ${i + 1} status (unclaimed/inactive/active)` })
	})
	self.setVariableDefinitions(vars)
	if (self.state) {
		self.setVariableValues({ router_name: self.state.name || '' })
	}
}
