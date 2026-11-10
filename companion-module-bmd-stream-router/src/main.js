import { InstanceBase, InstanceStatus } from '@companion-module/base'
import { UpgradeScripts } from './upgrades.js'
import { UpdateActions } from './actions.js'
import { UpdateFeedbacks } from './feedbacks.js'
import { UpdateVariableDefinitions } from './variables.js'
import { BmdCloudClient } from './api.js'

export { UpgradeScripts }

export default class StreamRouterInstance extends InstanceBase {
	constructor(internal) {
		super(internal)
		this.state = null // last GetAndWatchRouter frame {router: {...}}
		this.routers = []
		this.watchHandle = null
		this.client = null
	}

	async init(config) {
		this.config = config

		// export definitions before any async state arrives, so the host never
		// sees values for undefined variables
		this.updateActions()
		this.updateFeedbacks()
		this.updateVariableDefinitions()

		this.updateStatus(InstanceStatus.Connecting)
		await this.connect()
	}

	async connect() {
		try {
			if (!this.config.session) throw new Error('no session configured')
			const parsed = BmdCloudClient.parseAuthCookie(this.config.session.trim())
			this.client = new BmdCloudClient({
				refreshToken: parsed.refreshToken,
				accessToken: parsed.accessToken,
				accessTokenExpiresAt: parsed.expiresAt,
				log: (level, msg) => this.log(level, msg),
			})
			const routers = await this.client.getRouters()
			this.routers = routers.routers || []
			if (!this.routers.length) throw new Error('no Stream Routers on this account')

			this.routerId =
				this.config.routerId && this.routers.some((r) => r.id === this.config.routerId)
					? this.config.routerId
					: this.routers[0].id

			if (this.watchHandle) this.watchHandle.close()
			this.watchHandle = this.client.watchRouter(
				this.routerId,
				(frame) => {
					this.state = frame.router
					this.updateActions() // refresh dropdown choice lists
					this.updateFeedbacks()
					this.checkFeedbacks()
					this.setVariableValues(this.collectVariables())
				},
				(err) => this.log('warn', `watch stream: ${err.message}`),
			)
			this.updateStatus(InstanceStatus.Ok)
			this.log('info', `watching router ${this.routerId}`)
		} catch (e) {
			this.updateStatus(InstanceStatus.ConnectionFailure, e.message)
			this.log('error', e.message)
			setTimeout(() => this.connect(), 15000)
		}
	}

	async configUpdated(config) {
		this.config = config
		if (this.client) this.client.accessToken = null // force re-auth from new session
		await this.connect()
	}

	async destroy() {
		if (this.watchHandle) this.watchHandle.close()
	}

	endpoints() {
		const r = this.state
		if (!r) return { sources: [], destinations: [] }
		return { sources: r.sources || [], destinations: r.destinations || [] }
	}

	routeFor(destinationId) {
		if (!this.state || !this.state.routes) return null
		return this.state.routes.find((x) => x.destination === destinationId) || null
	}

	sourceName(id) {
		const { sources, destinations } = this.endpoints()
		const all = sources.concat(destinations)
		const ep = all.find((e) => e.id === id)
		return ep ? ep.name : id
	}

	destinationById(id) {
		const { destinations } = this.endpoints()
		return destinations.find((d) => d.id === id) || null
	}

	collectVariables() {
		const vars = {
			router_name: (this.state && this.state.name) || '',
			router_region: (this.state && this.state.region) || '',
			route_count: String(this.state?.routes?.length ?? 0),
		}
		const { sources, destinations } = this.endpoints()
		destinations.forEach((d, i) => {
			const rt = this.routeFor(d.id)
			vars[`dest_${i + 1}_name`] = d.name
			vars[`dest_${i + 1}_source`] = rt ? this.sourceName(rt.source) : ''
			vars[`dest_${i + 1}_source_id`] = rt ? rt.source : ''
			vars[`dest_${i + 1}_status`] = (d.status || '').replace('STATUS_', '').toLowerCase()
		})
		sources.forEach((s, i) => {
			vars[`input_${i + 1}_name`] = s.name
			vars[`input_${i + 1}_status`] = (s.status || '').replace('STATUS_', '').toLowerCase()
		})
		return vars
	}

	// Return config fields for web config
	getConfigFields() {
		return [
			{
				type: 'textinput',
				id: 'session',
				label: 'Session cookie (bmcloud.authToken value)',
				tooltip:
					'Log in at apps.cloud.blackmagicdesign.com, open DevTools > Application > Cookies, copy the value of bmcloud.authToken and paste it here (URL-encoded is fine, the module decodes it). Contains a refresh token, so it keeps working until you log out.',
				width: 12,
			},
			{
				type: 'textinput',
				id: 'routerId',
				label: 'Router ID (blank = first router)',
				width: 8,
			},
		]
	}

	updateActions() {
		UpdateActions(this)
	}

	updateFeedbacks() {
		UpdateFeedbacks(this)
	}

	updateVariableDefinitions() {
		UpdateVariableDefinitions(this)
	}
}
