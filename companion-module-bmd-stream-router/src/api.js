/**
 * Blackmagic Cloud Stream Router API client.
 *
 * Speaks Connect-RPC (JSON) to web-api.cloud.blackmagicdesign.com with the same
 * session the Blackmagic Cloud web app uses. Auth = OAuth refresh token from the
 * `bmcloud.authToken` cookie of a logged-in browser session.
 *
 * Protocol notes (verified against the live service):
 *  - unary RPC: POST JSON to /<package>.<Service>/<Method>, headers
 *    Content-Type: application/json, Connect-Protocol-Version: 1, Authorization: Bearer <jwt>
 *  - server-stream: same but Content-Type application/connect+json; response is
 *    framed as [1 flag byte][4 bytes big-endian length][JSON payload] chunks.
 */

const AUTH = 'https://auth.cloud.blackmagicdesign.com'
const CLIENT_ID = 'GXME7NMpelSg0biEqCuLJp8y5k50toDx' // Blackmagic Cloud web app client
const API = 'https://web-api.cloud.blackmagicdesign.com'
const SVC = 'bmd.cloud.webapi.streaming.v1.StreamRouterService'

export class BmdCloudClient {
	/**
	 * @param {object} opts
	 * @param {string} [opts.refreshToken] OAuth refresh token (single active family - if the
	 *   cloud web app is open anywhere it rotates the family, paste a fresh cookie then)
	 * @param {string} [opts.accessToken] access token from the cookie (used until it expires)
	 * @param {number} [opts.accessTokenExpiresAt] ms epoch; when absent and accessToken is set,
	 *   it is trusted for 55 minutes
	 * @param {(level: 'info'|'warn'|'error'|'debug', msg: string) => void} [opts.log]
	 */
	constructor(opts) {
		this.refreshToken = opts.refreshToken || null
		this.accessToken = opts.accessToken || null
		this.tokenExpiresAt =
			opts.accessTokenExpiresAt || (this.accessToken ? Date.now() + 55 * 60000 : 0)
		this.log = opts.log || (() => {})
	}

	/** Extract the session from a pasted bmcloud.authToken cookie value. */
	static parseAuthCookie(cookieValue) {
		const outer = JSON.parse(cookieValue)
		const body = outer.body || outer
		return {
			accessToken: body.access_token,
			refreshToken: body.refresh_token,
			expiresAt: outer.expiresAt || 0,
		}
	}

	async ensureToken() {
		if (this.accessToken && Date.now() < this.tokenExpiresAt - 120000) return this.accessToken
		if (!this.refreshToken) throw new Error('no refresh token configured')
		const res = await fetch(AUTH + '/oauth/token', {
			method: 'POST',
			headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
			body: new URLSearchParams({
				grant_type: 'refresh_token',
				client_id: CLIENT_ID,
				refresh_token: this.refreshToken,
			}),
		})
		const j = await res.json()
		if (!j.access_token) throw new Error(`token refresh failed: ${res.status} ${JSON.stringify(j).slice(0, 200)}`)
		this.accessToken = j.access_token
		if (j.refresh_token) this.refreshToken = j.refresh_token // rotated
		this.tokenExpiresAt = Date.now() + (j.expires_in || 3600) * 1000
		this.log('debug', 'access token refreshed')
		return this.accessToken
	}

	async rpc(method, body) {
		const token = await this.ensureToken()
		const res = await fetch(`${API}/${SVC}/${method}`, {
			method: 'POST',
			headers: {
				'Content-Type': 'application/json',
				'Connect-Protocol-Version': '1',
				Authorization: `Bearer ${token}`,
			},
			body: JSON.stringify(body || {}),
		})
		const text = await res.text()
		if (res.status !== 200) {
			let msg = text
			try {
				msg = JSON.parse(text).message || text
			} catch (_e) {
				/* plain 404 etc */
			}
			throw new Error(`${method}: ${res.status} ${msg}`)
		}
		return JSON.parse(text)
	}

	getRouters() {
		return this.rpc('GetRouters', {})
	}

	async updateRoutes(routerId, upserts, deletions) {
		const body = { routerId }
		if (upserts && upserts.length) body.upserts = upserts
		if (deletions && deletions.length) body.deletions = deletions
		return this.rpc('UpdateRoutes', body)
	}

	route(routerId, sourceId, destinationId) {
		return this.updateRoutes(routerId, [{ source: sourceId, destination: destinationId }])
	}

	unroute(routerId, sourceId, destinationId) {
		return this.updateRoutes(routerId, null, [{ source: sourceId, destination: destinationId }])
	}

	/**
	 * Update an endpoint. For gateway destinations (streaming outputs):
	 * gateway = { streamKey, passphrase?, username?, predefinedSinkConfig?: {providerId, urlId},
	 *             customSinkConfig?: {urls: [..], passphrase?} }
	 * Note: sending gateway: {} (clearing) errors upstream - replace configs instead.
	 */
	updateEndpoint(routerId, endpointId, fields) {
		return this.rpc('UpdateEndpoint', { routerId, id: endpointId, ...fields })
	}

	setSinkProvider(routerId, endpointId, providerId, urlId, streamKey, passphrase) {
		const gateway = { streamKey: streamKey || '' }
		if (passphrase) gateway.passphrase = passphrase
		gateway.predefinedSinkConfig = { providerId, urlId }
		return this.updateEndpoint(routerId, endpointId, { gateway })
	}

	setSinkCustom(routerId, endpointId, urls, streamKey, opts) {
		const gateway = { streamKey: streamKey || '', customSinkConfig: { urls: Array.isArray(urls) ? urls : [urls] } }
		if (opts && opts.passphrase) gateway.passphrase = opts.passphrase
		if (opts && opts.username) gateway.username = opts.username
		return this.updateEndpoint(routerId, endpointId, { gateway })
	}

	/**
	 * Open the GetAndWatchRouter server stream and invoke onState(json) for every frame.
	 * Returns a handle with close(). Auto-reconnects unless close() was called.
	 */
	watchRouter(routerId, onState, onError) {
		let closed = false
		let controller = null
		const run = async () => {
			while (!closed) {
				try {
					const token = await this.ensureToken()
					controller = new AbortController()
					const bytes = Buffer.from(JSON.stringify({ id: routerId }), 'utf8')
					const lenBuf = Buffer.alloc(4)
					lenBuf.writeUInt32BE(bytes.length)
					const env = Buffer.concat([Buffer.from([0]), lenBuf, bytes])
					const res = await fetch(`${API}/${SVC}/GetAndWatchRouter`, {
						method: 'POST',
						headers: {
							'Content-Type': 'application/connect+json',
							'Connect-Protocol-Version': '1',
							Authorization: `Bearer ${token}`,
						},
						body: new Uint8Array(env),
						signal: controller.signal,
					})
					if (!res.ok || !res.body) throw new Error(`watch failed: ${res.status}`)
					const reader = res.body.getReader()
					let buf = Buffer.alloc(0)
					for (;;) {
						const { done, value } = await reader.read()
						if (done) break
						buf = Buffer.concat([buf, Buffer.from(value)])
						while (buf.length >= 5) {
							const len = buf.readUInt32BE(1)
							if (buf.length < 5 + len) break
							const frame = buf.subarray(5, 5 + len)
							buf = buf.subarray(5 + len)
							try {
								onState(JSON.parse(frame.toString('utf8')))
							} catch (e) {
								this.log('debug', 'bad frame: ' + e.message)
							}
						}
					}
				} catch (e) {
					if (closed) return
					if (onError) onError(e)
				}
				if (!closed) await new Promise((r) => setTimeout(r, 3000))
			}
		}
		run()
		return {
			close: () => {
				closed = true
				if (controller) controller.abort()
			},
		}
	}
}

