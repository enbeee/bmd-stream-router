'use strict'

module.exports = function updateVariableDefinitions(self) {
	const vars = [{ variableId: 'router_name', name: 'Router name' }]
	const { sources, destinations } = self.endpoints()
	destinations.forEach((d, i) => {
		vars.push({ variableId: `dest_${i + 1}_name`, name: `Destination ${i + 1} name` })
		vars.push({ variableId: `dest_${i + 1}_source`, name: `Destination ${i + 1} currently routed source` })
	})
	sources.forEach((s, i) => {
		vars.push({ variableId: `input_${i + 1}_status`, name: `Input ${i + 1} claim status` })
	})
	self.setVariableDefinitions(vars)
	if (self.state) {
		self.setVariableValues({ router_name: self.state.name || '' })
	}
}
