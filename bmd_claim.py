#!/usr/bin/env python3
"""Software Stream Router claimant - replicates what DaVinci Resolve / ATEM do.

Flow: password-grant login -> GetEndpoints -> Claim -> Connect -> (SRT stream) -> Unclaim.

Usage:
  python3 bmd_claim.py stream [endpoint_id]       # one-shot: claim a free input, connect,
                                                  # print a ready-to-paste OBS srt:// URL
  python3 bmd_claim.py stream --obs               # same, but drive OBS directly: inject the
                                                  # stream key and StartStream via OBS's
                                                  # WebSocket server (Tools -> WebSocket Server
                                                  # Settings, OBS 28+); watches and releases the
                                                  # input automatically when the stream stops
        --obs-password PW   if the OBS WebSocket server uses a password
        --obs-host H        default 127.0.0.1
        --obs-port N        default 4455
  python3 bmd_claim.py router                     # show router state: inputs, outputs,
                                                  # streaming-output destinations, routes
  python3 bmd_claim.py setdest <endpoint> --url <rtmp/srt-url> [--key K] [--pass P]
                                                 # set a Streaming Output's destination
  python3 bmd_claim.py route <source> <dest>      # route an input to an output
  python3 bmd_claim.py unroute <source> <dest>    # remove a route
        (--router <id> overrides the default router)
  python3 bmd_claim.py obscheck                   # verify OBS WebSocket control works
                                                    (run this first if --obs misbehaves)
  python3 bmd_claim.py endpoints                  # list router inputs as the device sees them
  python3 bmd_claim.py claim <endpoint_id>        # claim + connect, print session credentials
  python3 bmd_claim.py unclaim <endpoint_id>      # release an input

Requirements: Python 3.8+ and  pip install httpx[http2]
Credentials:  put your BMD Cloud email on line 1 and password on line 2 of
              credentials.md next to this script.
"""
import gzip, json, os, re, struct, sys, time, urllib.parse

import httpx

HERE = os.path.dirname(os.path.abspath(__file__))
AUTH = 'https://auth.cloud.blackmagicdesign.com'
BMD = 'https://bmd-api.cloud.blackmagicdesign.com'
CLIENT_ID = 'ydUVBMZNONl563WQBtGo9p7gYnYAj9PS'
CLIENT_SECRET = 'LhiETUC0arqEVmYzTROkDbkNYE8ND2foVYFOYAMEd3qvzSBnytVTV6HQNI7PsY9S'
SCOPE = 'offline_access openid profile email read:current_user update:current_user_metadata'
AUDIENCE = 'https://bmd-api.cloud.blackmagicdesign.com/'
SVC = 'bmd.cloud.bmdapi.streaming.v1.StreamRouterEdgeDeviceService'
SESSION_FILE = os.path.join(HERE, 'session_claim.json')
ROUTER_ID = 'router_0_0000000000000000000000000'   # placeholder - your router
# id (GetRouters / web UI URL); override per-call with --router, or globally
# via set_router() / BRIDGE_ROUTER in bmd_bridge.py / bmd_view.py
REGION = 'eu-west-2'   # gRPC region header; set per-router by set_router()

try:
    creds = [l.strip() for l in open(os.path.join(HERE, 'credentials.md'))
             if l.strip()]
    USERNAME, PASSWORD = creds[0], creds[1]
except OSError:
    USERNAME = PASSWORD = None   # create credentials.md: email line 1,
    # password line 2 (see BMD_STREAM_ROUTER.md)

def _device_id():
    try:
        return json.load(open(SESSION_FILE))['deviceId']
    except Exception:
        did = f'obs_encoder_{abs(hash(str(time.time()))) % 0xffffffff:08x}'
        try:
            s = json.load(open(SESSION_FILE))
            s['deviceId'] = did
            json.dump(s, open(SESSION_FILE, 'w'))
        except Exception:
            pass
        return did

DEVICE_ID = _device_id()

def login():
    if not USERNAME:
        sys.exit('credentials.md missing: line 1 = email, line 2 = password')
    r = httpx.post(f'{AUTH}/oauth/token', data={
        'grant_type': 'password', 'client_id': CLIENT_ID, 'client_secret': CLIENT_SECRET,
        'username': USERNAME, 'password': PASSWORD, 'scope': SCOPE, 'audience': AUDIENCE,
    }, headers={
        'User-Agent': 'DaVinci Resolve Studio/21.1.1.0010 (Windows; x86_64;) BMDDavCloudClient/1.8_4-a11',
        'BMD-Product-Info': 'app=DaVinci Resolve Studio; version=21.1.1.0010; os=Windows; arch=x86_64; BMDDavCloudClient=1.8_4-a11',
    }, timeout=30)
    if r.status_code != 200:
        sys.exit(f'login failed: {r.status_code} {r.text[:200]}')
    j = r.json()
    claims = json.loads(__import__('base64').urlsafe_b64decode(
        j['access_token'].split('.')[1] + '=' * (-len(j['access_token'].split('.')[1]) % 4)))
    print(f"login ok: aud={claims['aud'][0].split('//')[1].split('.')[0]} gty={claims.get('gty')} expires={j['expires_in']}s")
    json.dump({'access_token': j['access_token'], 'refresh_token': j.get('refresh_token'),
               'expiresAt': time.time() + j.get('expires_in', 3600),
               'deviceId': DEVICE_ID}, open(SESSION_FILE, 'w'))
    return j['access_token']

def token():
    try:
        s = json.load(open(SESSION_FILE))
        if time.time() < s.get('expiresAt', 0) - 120:
            return s['access_token']
    except Exception:
        pass
    return login()

# ---- minimal protobuf ----
def pb_str(field, s):
    b = s.encode()
    return bytes([(field << 3) | 2]) + bytes([len(b) & 0x7f]) + b

def pb_varint(field, v):
    out = bytes([(field << 3)])
    while True:
        b = v & 0x7f
        v >>= 7
        out += bytes([b | (0x80 if v else 0)])
        if not v:
            return out

def pb_parse(buf):
    """-> {field: [values]} (strings as str, varints as int, nested as raw bytes)"""
    out = {}
    i = 0
    while i < len(buf):
        tag = 0; shift = 0
        while True:
            b = buf[i]; i += 1
            tag |= (b & 0x7f) << shift
            if not (b & 0x80): break
            shift += 7
        field, wt = tag >> 3, tag & 7
        if wt == 2:
            ln = 0; shift = 0
            while True:
                b = buf[i]; i += 1
                ln |= (b & 0x7f) << shift
                if not (b & 0x80): break
                shift += 7
            v = buf[i:i+ln]; i += ln
            try:
                s = v.decode('utf-8')
                if all(32 <= ord(c) < 127 or c in '\n\t' for c in s):
                    v = s
            except Exception:
                pass
        elif wt == 0:
            v = 0; shift = 0
            while True:
                b = buf[i]; i += 1
                v |= (b & 0x7f) << shift
                if not (b & 0x80): break
                shift += 7
        else:
            raise ValueError(f'wiretype {wt} at {i}')
        out.setdefault(field, []).append(v)
    return out

class Grpc:
    def __init__(self, tok):
        self.tok = tok
        self.client = httpx.Client(http2=True, timeout=30)

    def call(self, method, message, region=False):
        frame = b'\x00' + len(message).to_bytes(4, 'big') + message
        headers = {
            'authorization': f'Bearer {self.tok}',
            'x-api-key': 'Md2oJrLfDP+kLQMOTPufI',
            'content-type': 'application/grpc',
            'te': 'trailers',
            'grpc-accept-encoding': 'identity',
            'user-agent': 'grpc-c++/1.51.1 grpc-c/29.0.0 (windows; chttp2)',
            'bmd-product-info': 'app=DaVinci Resolve Studio; version=21.1.1.0010; os=Windows; arch=x86_64; BMDDavCloudClient=1.8_4-a11',
        }
        if region:
            headers['region'] = REGION
        r = self.client.post(f'{BMD}/{SVC}/{method}', content=frame, headers=headers)
        if r.status_code != 200:
            return r.status_code, r.text[:200], None
        data = r.content
        if len(data) < 5:
            return 200, 'empty', None
        flag = data[0]
        ln = int.from_bytes(data[1:5], 'big')
        msg = data[5:5+ln]
        if flag == 1:
            msg = gzip.decompress(msg)
        return 200, 'ok', msg

def endpoints(tok):
    g = Grpc(tok)
    msg = pb_str(1, DEVICE_ID) + pb_varint(4, 100)
    st, info, resp = g.call('GetEndpoints', msg)
    if st != 200:
        print(f'GetEndpoints failed: {st} {info}')
        return
    pb = pb_parse(resp)
    print('GetEndpoints OK, response fields:')
    for f, vals in sorted(pb.items()):
        for v in vals:
            print(f'  field {f}: {str(v)[:120]}')

def claim(tok, endpoint_id, quiet=False):
    g = Grpc(tok)
    msg = (pb_str(1, ROUTER_ID) + pb_str(2, endpoint_id) + pb_str(3, DEVICE_ID) +
           pb_str(4, '{ "device_name":"OBS Encoder" }'))
    st, info, resp = g.call('Claim', msg, region=True)
    if not quiet:
        print(f'Claim: {st} {info}')
    if st == 200 and resp and not quiet:
        print('  ClaimResponse fields:', {k: [str(x)[:80] for x in v] for k, v in pb_parse(resp).items()})
    msg2 = pb_str(1, ROUTER_ID) + pb_str(2, endpoint_id) + pb_str(3, DEVICE_ID)
    st, info, resp = g.call('Connect', msg2, region=True)
    if not quiet:
        print(f'Connect: {st} {info}')
    if st == 200 and resp:
        fields = pb_parse(resp)
        if not quiet:
            for f, vals in sorted(fields.items()):
                for v in vals:
                    print(f'  ConnectResponse field {f}: {str(v)[:140]}')
        try:
            val = lambda f: fields[f][0].decode() if isinstance(fields[f][0], bytes) else str(fields[f][0])
            claim, secret, expiry = val(2), val(3), val(4)
            sid = build_streamid(claim, secret, expiry)
            if not quiet:
                print(f'  SRT STREAMID  : {sid}')
                print(f'  SRT PASSPHRASE: {secret}   (PBKEYLEN 16 / AES-128)')
            fields['_streamid'] = [sid]
            fields['_passphrase'] = [secret]
        except Exception as ex:
            if not quiet:
                print(f'  (streamid build failed: {ex})')
        return fields
    return None

class ObsClosed(RuntimeError):
    pass

def _recv_msg(ws, where='OBS'):
    """One obs-websocket message; explains connection closes instead of crashing."""
    opcode, data = ws.recv_data()
    if opcode == 8:  # close frame: 2-byte code + reason
        code = struct.unpack('>H', data[:2])[0] if len(data) >= 2 else 0
        reason = data[2:].decode('utf-8', 'replace') if len(data) > 2 else ''
        hints = {
            4010: 'RPC version rejected - OBS is very old, or a proxy/plugin is on this port',
            4014: 'the WebSocket password is wrong',
        }
        extra = hints.get(code, '')
        raise ObsClosed(f'OBS closed the connection (close code {code}'
                        + (f': {reason}' if reason else '') + ')'
                        + (f' - {extra}' if extra else ''))
    if not data:
        raise ObsClosed(f'{where} sent an empty frame - connection closed '
                        '(usually a wrong WebSocket password)')
    return json.loads(data.decode('utf-8'))

def obs_connect(host='127.0.0.1', port=4455, password=None, report=False):
    """Connect + authenticate to obs-websocket v5. Returns (ws, request_fn)."""
    import hashlib
    import base64
    try:
        import websocket  # pip install websocket-client
    except ImportError:
        sys.exit('OBS control needs:  pip install websocket-client')
    try:
        ws = websocket.create_connection(f'ws://{host}:{port}', timeout=5)
    except Exception as ex:
        sys.exit(f'cannot reach OBS WebSocket at {host}:{port}: {ex}\n'
                 '  (OBS running? Tools -> WebSocket Server Settings -> enable it)')

    try:
        hello = _recv_msg(ws)['d']
        if report:
            print(f"  hello: obs-websocket {hello.get('obsWebSocketVersion')}  "
                  f"rpcVersion {hello.get('rpcVersion')}  "
                  f"auth requested: {'YES' if 'authentication' in hello else 'no'}")
        if 'authRequired' in hello or (isinstance(hello, dict) and 'authRequired' in hello):
            sys.exit('old obs-websocket v4 protocol detected (OBS 27 or the legacy plugin) - '
                     'upgrade OBS to 28+, or use the manual-paste flow')
        ident = {'rpcVersion': 1}   # obs-websocket 'v5' = framing version; the rpcVersion is 1
        if 'authentication' in hello:
            if password is None:
                sys.exit('OBS requires a WebSocket password - pass:  --obs-password "..."  '
                         '(or re-set it in OBS Tools -> WebSocket Server Settings first)')
            secret = base64.b64encode(hashlib.sha256(
                (password + hello['authentication']['salt']).encode()).digest()).decode()
            ident['authentication'] = base64.b64encode(hashlib.sha256(
                (secret + hello['authentication']['challenge']).encode()).digest()).decode()
        ws.send(json.dumps({'op': 1, 'd': ident}))
        while True:
            m = _recv_msg(ws, 'OBS (after Identify)')
            if m.get('op') == 2:
                break
    except ObsClosed as ex:
        sys.exit(str(ex))

    def request(rtype, rdata=None):
        ws.send(json.dumps({'op': 6, 'd': {'requestType': rtype,
                                           'requestId': rtype,
                                           'requestData': rdata or {}}}))
        while True:
            m = _recv_msg(ws, f'OBS ({rtype})')
            if m.get('op') == 7 and m['d'].get('requestId') == rtype:
                st = m['d']['requestStatus']
                if not st.get('result'):
                    raise RuntimeError(f"{rtype} failed: {st.get('code')} {st.get('comment', '')}")
                return m['d'].get('responseData', {})

    return ws, request

def obscheck(host, port, password):
    """Verify OBS WebSocket control without claiming or touching stream settings."""
    try:
        ws, request = obs_connect(host, port, password, report=True)
        v = request('GetVersion')
        print(f"OBS {v.get('obsVersion')} / obs-websocket {v.get('obsWebSocketVersion')}"
              f" - connected + authenticated OK")
        s = request('GetStreamServiceSettings')
        cur = s.get('streamServiceSettings', {}) or {}
        print(f"current stream service : {s.get('streamServiceType')}")
        print(f"current server setting : {str(cur.get('server', ''))[:100]}")
        print('OBS control is working - use:  python bmd_claim.py stream --obs')
    except Exception as ex:
        sys.exit(f'OBS control check failed: {ex}')

def stream(tok, endpoint_id=None, obs=False, obs_password=None, obs_host='127.0.0.1', obs_port=4455):
    """Claim a free input, connect, print a ready-to-paste OBS srt:// URL."""
    if obs:
        ws, obs_request = obs_connect(obs_host, obs_port, obs_password)  # fail fast, nothing claimed yet
    g = Grpc(tok)
    msg = b'\x0a\x01\x01' + pb_str(2, DEVICE_ID) + pb_varint(4, 100)
    st, info, resp = g.call('GetEndpoints', msg)
    if st != 200 or not resp:
        sys.exit(f'GetEndpoints failed: {st} {info}')
    ids = [i.decode() for i in re.findall(rb'endpoint_[0-9a-z]+', resp)]
    names = [n.decode() for n in re.findall(rb'Input \d', resp)]
    todo = [endpoint_id] if endpoint_id else ids

    for eid in todo:
        r = claim(tok, eid, quiet=True)
        if not (r and r.get(1) and r.get(2) and r.get(3) and r.get(4)):
            unclaim(tok, eid)  # input busy - release and try the next
            continue
        val = lambda f: r[f][0].decode() if isinstance(r[f][0], bytes) else str(r[f][0])
        hostport, claimid, secret, expiry = val(1), val(2), val(3), val(4)
        name = names[ids.index(eid)] if eid in ids else eid
        sid = build_streamid(claimid, secret, expiry)
        # NOTE: the streamid goes into the URL RAW (no percent-encoding).
        # OBS passes query params through verbatim; an encoded sid arrives at
        # the relay as literal '%23%21...' and gets silently dropped.
        url = f'srt://{hostport}?streamid={sid}&passphrase={secret}&pbkeylen=16'
        print()
        print(f'  claimed: {name} ({eid})')
        print(f'  edge   : {hostport}')
        print()
        try:
            if obs:
                obs_request('SetStreamServiceSettings', {
                    'streamServiceType': 'rtmp_custom',
                    'streamServiceSettings': {'server': url, 'key': ''},
                })
                obs_request('StartStream')
                print('  OBS: stream key set, streaming STARTED.')
                print('  watching; the input is released automatically when you stop the OBS stream.')
                print('  (Ctrl+C detaches the watcher without touching the stream)')
                while True:
                    time.sleep(5)
                    if not obs_request('GetStreamStatus').get('outputActive'):
                        break
                print('  OBS stream stopped - releasing input.')
                unclaim(tok, eid)
            else:
                print('  OBS (Settings -> Stream -> Custom server):')
                print(f'    {url}')
                print()
                print('  plain values for ffmpeg / srt-live-transmit / ffplay:')
                print(f'    streamid   = {sid}')
                print(f'    passphrase = {secret}   (PBKEYLEN 16 / AES-128)')
                print()
                print(f'  to drive OBS directly next time:  python3 {os.path.basename(__file__)} stream --obs')
                print(f'  to release afterwards:  python3 {os.path.basename(__file__)} unclaim {eid}')
                print('  (or just wait ~2 min - the claim lapses when nothing streams)')
        except KeyboardInterrupt:
            print('  watcher detached. release later with:')
            print(f'    python3 {os.path.basename(__file__)} unclaim {eid}')
        except Exception as ex:
            unclaim(tok, eid)
            print(f'  ERROR: {ex}')
            print(f'  input {eid} released - fix the problem and re-run.')
            print('  diagnostics:  python3 bmd_claim.py obscheck --obs-password ...')
            sys.exit(1)
        return
    sys.exit('no free input found')

def unclaim(tok, endpoint_id):
    g = Grpc(tok)
    msg = pb_str(1, ROUTER_ID) + pb_str(2, endpoint_id) + pb_str(3, DEVICE_ID)
    st, info, resp = g.call('Unclaim', msg, region=True)
    print(f'Unclaim: {st} {info}')

# ---- user plane (router config: routes + streaming output destinations) ----
# Connect-RPC JSON on web-api; the device-plane token is accepted directly.
WEBAPI = 'https://web-api.cloud.blackmagicdesign.com'
RSVC = 'bmd.cloud.webapi.streaming.v1.StreamRouterService'

def web_rpc(tok, method, body, client=None):
    r = (client or httpx.Client(http2=True, timeout=30)).post(
        f'{WEBAPI}/{RSVC}/{method}', json=body,
        headers={'authorization': f'Bearer {tok}',
                 'content-type': 'application/json',
                 'connect-protocol-version': '1'})
    if r.status_code != 200:
        sys.exit(f'{method}: {r.status_code} {r.text[:200]}')
    return r.json()

def router_state(tok, router_id=None):
    body = json.dumps({'id': router_id or ROUTER_ID}).encode()
    env = b'\x00' + len(body).to_bytes(4, 'big') + body
    with httpx.stream('POST', f'{WEBAPI}/{RSVC}/GetAndWatchRouter', content=env,
                      headers={'authorization': f'Bearer {tok}',
                               'content-type': 'application/connect+json',
                               'connect-protocol-version': '1'}, timeout=20) as r:
        if r.status_code != 200:
            sys.exit(f'GetAndWatchRouter: {r.status_code}')
        buf = b''
        for chunk in r.iter_bytes():
            buf += chunk
            if len(buf) >= 5:
                ln = int.from_bytes(buf[1:5], 'big')
                if len(buf) >= 5 + ln:
                    return json.loads(buf[5:5 + ln]).get('router', {})
    sys.exit('no router state received')

def cmd_router(tok, args):
    st = router_state(tok, resolve_router(tok, args.router))
    print(f"router: {st.get('name')} ({st.get('id')}) region {st.get('region')}")
    print('\nsources (inputs):')
    for e in st.get('sources', []):
        gw = ' gateway=' + ','.join(k for k in (e.get('gateway') or {})) if e.get('gateway') else ''
        print(f"  {e['id']}  {e.get('name','?'):18} {e.get('status','')}{gw}")
    print('\ndestinations:')
    for e in st.get('destinations', []):
        gw = e.get('gateway')
        if gw:
            sink = gw.get('predefinedSinkConfig') or gw.get('customSinkConfig') or {}
            dest = (sink.get('providerId', '') + '/' + str(sink.get('urlId', ''))) \
                if 'providerId' in sink else str(sink.get('urls', ''))
            live = ' LIVE' if gw.get('live') else ''
            print(f"  {e['id']}  {e.get('name','?'):18} -> {dest}{live}")
        else:
            print(f"  {e['id']}  {e.get('name','?'):18} ({e.get('type','')})")
    print('\nroutes:')
    for rt in st.get('routes', []):
        print(f"  {rt.get('source','?')} -> {rt.get('destination','?')}")

def cmd_setdest(tok, args):
    args.router = resolve_router(tok, args.router)
    gw = {'streamKey': args.key or ''}
    if args.sinkpass:
        gw['passphrase'] = args.sinkpass
    if args.username:
        gw['username'] = args.username
    if args.provider:
        cfg = {'providerId': args.provider}
        if args.url_id:
            cfg['urlId'] = args.url_id
        gw['predefinedSinkConfig'] = cfg
    else:
        if not args.url:
            sys.exit('setdest needs --url ... (custom) or --provider ... (predefined)')
        gw['customSinkConfig'] = {'urls': args.url}
    web_rpc(tok, 'UpdateEndpoint',
            {'routerId': args.router or ROUTER_ID, 'id': args.endpoint, 'gateway': gw})
    print('destination updated (router %s / %s)' % (args.router or ROUTER_ID, args.endpoint))

def cmd_route(tok, args):
    args.router = resolve_router(tok, args.router)
    body = {'routerId': args.router or ROUTER_ID,
            'upserts': [{'source': args.source, 'destination': args.destination}]}
    web_rpc(tok, 'UpdateRoutes', body)
    print('routed')

def cmd_unroute(tok, args):
    args.router = resolve_router(tok, args.router)
    body = {'routerId': args.router or ROUTER_ID,
            'deletions': [{'source': args.source, 'destination': args.destination}]}
    web_rpc(tok, 'UpdateRoutes', body)
    print('unrouted')

# ---- streamid + passphrase (SOLVED, verified live against the relay) ----
# The SRT streamid is the plain credential string:  #!::s=<expiry>,u=<claim>
# (s = ConnectResponse field 4, u = field 2; the secret never enters the sid).
# On the wire the SID handshake extension shows every 4-byte chunk reversed
# (libsrt packs the string into uint32 words in host byte order), which is
# why packet captures look like `::!#10=s...u,...a10=...` gibberish.
# The passphrase is ConnectResponse field 3 (the secret) as a raw string,
# PBKEYLEN 16 (AES-128) - it authenticates the claim via the SRT KM exchange.
def build_streamid(claim, secret, expiry):
    return f'#!::s={expiry},u={claim}'

def set_router(rid, tok=None):
    """Point all calls at another router (and its region)."""
    global ROUTER_ID, REGION
    ROUTER_ID = rid
    try:
        rr = web_rpc(tok or token(), 'GetRouters', {})
        for rt in rr.get('routers', []):
            if rt.get('id') == rid and rt.get('region'):
                REGION = rt['region']
    except Exception:
        pass


def resolve_router(tok=None, rid=None):
    """Return the router to use, auto-discovering it when none is set.

    With the placeholder default (fresh clone), queries GetRouters: one
    router on the account -> select it (and its region) automatically;
    several -> list them and exit. An explicit rid always wins."""
    global ROUTER_ID
    rid = rid or ROUTER_ID
    if rid != 'router_0_0000000000000000000000000':
        return rid
    rr = web_rpc(tok or token(), 'GetRouters', {}).get('routers', [])
    if len(rr) == 1:
        rt = rr[0]
        set_router(rt['id'], tok)
        print(f"auto-selected router {rt.get('name')} ({rt['id']}) "
              f"region {REGION}")
        return ROUTER_ID
    for rt in rr:
        print(f"  {rt['id']}  {rt.get('name','?'):18} {rt.get('region','')}")
    sys.exit('select one with --router <id> (or BRIDGE_ROUTER=...)')


def cmd_routers(tok, args):
    rr = web_rpc(tok, 'GetRouters', {}).get('routers', [])
    for rt in rr:
        print(f"  {rt['id']}  {rt.get('name','?'):18} {rt.get('region','')}")


def parse_router_flag(argv):
    router = ROUTER_ID
    rest = []
    it = iter(argv)
    for a in it:
        if a == '--router':
            router = next(it)
        else:
            rest.append(a)
    return router, rest

if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'login'
    tok = token()
    if cmd == 'login':
        pass
    elif cmd == 'endpoints':
        endpoints(tok)
    elif cmd == 'claim':
        claim(tok, sys.argv[2])
    elif cmd in ('stream', 'obscheck'):
        rest = []
        obs = False
        obs_password = None
        obs_host, obs_port = '127.0.0.1', 4455
        it = iter(sys.argv[2:])
        for a in it:
            if a == '--obs':
                obs = True
            elif a == '--obs-password':
                obs_password = next(it)
            elif a == '--obs-host':
                obs_host = next(it)
            elif a == '--obs-port':
                obs_port = int(next(it))
            else:
                rest.append(a)
        if cmd == 'obscheck':
            obscheck(obs_host, obs_port, obs_password)
        else:
            stream(tok, rest[0] if rest else None, obs=obs,
                   obs_password=obs_password, obs_host=obs_host, obs_port=obs_port)
    elif cmd == 'unclaim':
        unclaim(tok, sys.argv[2])
    elif cmd == 'routers':
        cmd_routers(tok, None)
    elif cmd in ('router', 'setdest', 'route', 'unroute'):
        import argparse
        ap = argparse.ArgumentParser()
        ap.add_argument('--router', default=None)
        if cmd == 'router':
            ap.add_argument('unused', nargs='*')
        elif cmd == 'setdest':
            ap.add_argument('endpoint')
            ap.add_argument('--url', action='append')
            ap.add_argument('--provider')
            ap.add_argument('--url-id', dest='url_id')
            ap.add_argument('--key', default='')
            ap.add_argument('--pass', dest='sinkpass')
            ap.add_argument('--username')
        else:
            ap.add_argument('source')
            ap.add_argument('destination')
        router_flag, rest = parse_router_flag(sys.argv[2:])
        args = ap.parse_args(rest)
        args.router = args.router or router_flag
        {'router': cmd_router, 'setdest': cmd_setdest,
         'route': cmd_route, 'unroute': cmd_unroute}[cmd](tok, args)
    else:
        print(__doc__)
