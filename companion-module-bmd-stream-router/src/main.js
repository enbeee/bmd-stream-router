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
		// generation guard: a config update or retry overlapping an in-flight
		// connect must invalidate it, or two watch streams race (the log
		// symptom was a doubled "watching router" line)
		if (this.connectTimer) {
			clearTimeout(this.connectTimer)
			this.connectTimer = null
		}
		const gen = (this.connectGen = (this.connectGen || 0) + 1)
		const stale = () => gen !== this.connectGen
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
			if (stale()) return
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
					if (stale()) return
					try {
						// GetAndWatchRouter sends an initial full snapshot
						// {router: {...}} followed by INCREMENTAL deltas
						// {routerUpdate: {id, endpointsUpdate, routesUpdate}}.
						// Applying deltas onto the kept state is essential:
						// replacing state with frame.router (undefined on a
						// delta) wiped the endpoints and every stored action/
						// feedback instantly failed "not in the list of
						// choices" - the "button forgets its target" bug.
						if (frame.router) {
							this.state = frame.router
						} else if (frame.routerUpdate) {
							this.applyRouterUpdate(frame.routerUpdate)
						} else {
							return // unknown frame - keep last good state
						}
						// re-define actions/feedbacks only when the choice-relevant
						// state actually changed: every setActionDefinitions call
						// makes the host re-subscribe + re-validate all stored
						// controls, so re-defining per frame floods it
						const sig = this.stateSignature()
						if (sig !== this.lastStateSig) {
							this.lastStateSig = sig
							this.updateActions()
							this.updateFeedbacks()
						}
						this.checkAllFeedbacks()
						this.setVariableValues(this.collectVariables())
					} catch (e) {
						// never let a bad frame kill the watch stream
						this.log('error', `state update failed: ${e.message}`)
					}
				},
				(err) => {
					if (!stale()) this.log('warn', `watch stream: ${err.message}`)
				},
			)
			if (stale()) return
			this.updateStatus(InstanceStatus.Ok)
			this.log('info', `watching router ${this.routerId}`)
		} catch (e) {
			if (stale()) return
			this.updateStatus(InstanceStatus.ConnectionFailure, e.message)
			this.log('error', e.message)
			this.connectTimer = setTimeout(() => this.connect(), 15000)
		}
	}

	async configUpdated(config) {
		this.config = config
		this.lastStateSig = undefined
		this.connectGen = (this.connectGen || 0) + 1 // invalidate in-flight connects
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

	/** Signature of everything the action/feedback choices depend on. */
	stateSignature() {
		const r = this.state
		if (!r) return 'null'
		const ep = (e) => `${e.id}:${e.name}:${e.status}:${e.type}:${
			e.gateway ? (e.gateway.live ? 1 : 0) : ''
		}`
		return JSON.stringify([
			r.name,
			r.region,
			(r.sources || []).map(ep),
			(r.destinations || []).map(ep),
			r.routes || [],
		])
	}

	/** Apply one {routerUpdate} delta frame onto the current state.
	 * Frame grammar (live-verified): {id, endpointsUpdate: {upserts, deletions},
	 * routesUpdate: {upserts, deletions}}. Endpoint upserts are routed by their
	 * type field into sources vs destinations; route upserts are de-duplicated
	 * on the {source, destination} pair. */
	applyRouterUpdate(upd) {
		if (!this.state || !upd || upd.id !== this.state.id) return
		const eu = upd.endpointsUpdate || {}
		const upsertEndpoint = (list, e) => {
			const i = list.findIndex((x) => x.id === e.id)
			if (i >= 0) list[i] = e
			else list.push(e)
		}
		for (const e of eu.upserts || []) {
			if (e.type === 'TYPE_SOURCE') {
				if (!this.state.sources) this.state.sources = []
				upsertEndpoint(this.state.sources, e)
			} else {
				if (!this.state.destinations) this.state.destinations = []
				upsertEndpoint(this.state.destinations, e)
			}
		}
		const delIds = new Set((eu.deletions || []).map((d) => d.id || d))
		if (delIds.size) {
			this.state.sources = (this.state.sources || []).filter((e) => !delIds.has(e.id))
			this.state.destinations = (this.state.destinations || []).filter((e) => !delIds.has(e.id))
		}
		const ru = upd.routesUpdate || {}
		const routes = this.state.routes || (this.state.routes = [])
		for (const r of ru.upserts || []) {
			const i = routes.findIndex((x) => x.source === r.source && x.destination === r.destination)
			if (i < 0) routes.push(r)
		}
		for (const d of ru.deletions || []) {
			const i = routes.findIndex((x) => x.source === d.source && x.destination === d.destination)
			if (i >= 0) routes.splice(i, 1)
		}
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
