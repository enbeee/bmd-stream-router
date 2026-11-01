#!/usr/bin/env python3
"""Software viewer for a Blackmagic Cloud Stream Router output.

Claims a router OUTPUT endpoint (like a Streaming Decoder / Remote Monitor
does), connects, receives the BMOS stream, decrypts it, re-muxes to MPEG-TS
and pipes it to ffmpeg (file, RTMP push, or local playback).

Usage:
  python3 bmd_view.py <output_endpoint_id> [--out FILE.ts|--push URL|--play]

  --out FILE   write demuxed MPEG-TS to FILE (then Ctrl+C)
  --push URL   pipe to: ffmpeg -f mpegts -i - -c copy <URL>
  --play       pipe to ffplay

Requires: pip install httpx[http2] cryptography ; ffmpeg in PATH for push/play.
The routed INPUT must be streaming before you start (Connect waits for it).
"""
import argparse
import hashlib
import os
import queue
import random
import socket
import struct
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import bmd_claim as B                      # claim plane + crypto helpers
if os.environ.get('BRIDGE_ROUTER'):
    B.set_router(os.environ['BRIDGE_ROUTER'], B.token())  # alt router+region
elif __name__ == '__main__':       # never at import (offline tools import us)
    B.resolve_router(B.token())     # auto-select when the account has one
from bmd_bridge import (ctrl, now_us, le_pack_sid, rfc3394_wrap,
                        make_km, HSREQ, TsToBmos)


def unwrap(kek, w):
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    C = [int.from_bytes(w[i:i + 8], 'big') for i in range(0, len(w), 8)]
    n = len(C) - 1
    A = C[0]
    R = C[1:]
    dec = Cipher(algorithms.AES(kek), modes.ECB()).decryptor()
    for j in range(5, -1, -1):
        for i in range(n, 0, -1):
            blk = dec.update((A ^ (n * j + i)).to_bytes(8, 'big')
                             + R[i - 1].to_bytes(8, 'big'))
            A = int.from_bytes(blk[:8], 'big')
            R[i - 1] = int.from_bytes(blk[8:], 'big')
    return (b''.join(x.to_bytes(8, 'big') for x in R)
            if A == 0xA6A6A6A6A6A6A6A6 else None)


class Viewer(threading.Thread):
    """SRT caller that receives the router's BMOS stream."""

    def __init__(self, host, port, sid, passphrase, log, wire_f=None):
        super().__init__(daemon=True)
        self.addr = (host, int(port))
        self.sid, self.passwd = sid, passphrase
        self.log = log
        self.wire_f = wire_f   # optional dump of ENCRYPTED wire packets
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(5)
        self.ok = threading.Event()
        self.dead = threading.Event()
        self.t0 = time.time()
        self.isn = random.getrandbits(31)
        self.relay_sock = None
        self.salt = None
        self.sek = None            # relay->us SEK from KMRSP
        self.keys = []             # keyring: [(sek, salt)] newest first
        self.in_q = queue.Queue()  # decrypted BMOS packets
        self.last_ack = 0
        self.last_full_t = 0.0

    def send(self, pkt):
        self.sock.sendto(pkt, self.addr)

    def run(self):
        try:
            self._handshake()
        except Exception as ex:
            self.log(f'handshake failed: {ex}')
            return
        self.ok.set()
        self._receive()

    def _handshake(self):
        mysock = random.getrandbits(31)
        indu = (ctrl(0, 0, 0, now_us(self.t0), 0)
                + struct.pack('>IIIII', 4, 2, self.isn, 1500, 8192)
                + struct.pack('>II', 1, mysock) + bytes(4)
                + bytes.fromhex('0025142d') + bytes(12))
        self.send(indu)
        resp, _ = self.sock.recvfrom(2048)
        cookie = resp[16 + 28:16 + 32]
        sek, salt, km = make_km(self.passwd)     # our tx key (upstream is idle)
        self.v_sek, self.v_salt = sek, salt      # what we offered upstream
        sidw = le_pack_sid(self.sid)
        # viewer-role announce, byte-exact from a real Remote Monitor capture:
        # role word3 = 0008 0020 (the 0x20 receive flag is the LOW byte -
        # 0x2000 in the wrong byte position made the relay treat us as an
        # encoder-role peer)
        blob = (bytes.fromhex('424d4f53010001000008002000080000')
                + struct.pack('<HH', 2, 1) + struct.pack('>H', random.getrandbits(16)) + b'\x00\x00')
        # HSREQ latency words 0086 - the Remote Monitor's viewer value (the
        # encoder profile uses 0088)
        HSREQ_V = bytes.fromhex('00010500000000bf00860086')
        exts = (struct.pack('>HH', 1, 3) + HSREQ_V
                + struct.pack('>HH', 5, len(sidw) // 4) + sidw
                + struct.pack('>HH', 3, len(km) // 4) + km
                + struct.pack('>HH', 0xBD01, 6) + blob)
        concl = (ctrl(0, 0, 0, now_us(self.t0), 0)
                 + struct.pack('>IIIII', 5, 7, self.isn, 1500, 8192)
                 + struct.pack('>II', 0xFFFFFFFF, mysock) + cookie
                 + bytes(16) + exts)
        self.send(concl)
        self.send(concl)      # RTM sends the conclusion twice
        # the relay may push cached stream data IMMEDIATELY - data packets can
        # land before/around the agreement. Only a CONTROL handshake packet
        # (cmd 0, >100B) is the agreement; buffer early data for later.
        ag = None
        early = []
        for _ in range(24):
            pkt, _ = self.sock.recvfrom(65536)
            if (len(pkt) >= 16 and (pkt[0] & 0x80)
                    and (struct.unpack('>H', pkt[:2])[0] & 0x7FFF) == 0
                    and len(pkt) > 100):
                ag = pkt
                break
            if pkt[:1] == b'\x80' and b'\x12\x20\x29' in pkt:
                # a KM control can beat the agreement - ingest it now
                cd = os.environ.get('VIEW_CTRL_DUMP')
                if cd:
                    with open(cd, 'ab') as fh:
                        fh.write(struct.pack('<I', len(pkt)) + pkt)
                self._km_update(pkt)
            early.append(pkt)
        self._early = early
        if not ag:
            raise RuntimeError('no agreement from relay')
        self.relay_sock = struct.unpack('>I', ag[16 + 24:16 + 28])[0]
        # KMRSP (type 4) in the agreement carries the relay's downstream key
        body = ag[16:]
        i = 48
        exts_seen = []
        while i + 4 <= len(body):
            t, l = struct.unpack_from('>HH', body, i)
            c = body[i + 4:i + 4 + l * 4]
            exts_seen.append(f'{t:#x}/{l}w')
            if t == 4:
                rsalt, rwrap = c[16:32], c[32:56]
                kek = hashlib.pbkdf2_hmac('sha1', self.passwd.encode(),
                                          rsalt[-8:], 2048, 16)
                self.salt = rsalt
                self.sek = unwrap(kek, rwrap)
                if self.sek:
                    self.keys.insert(0, (self.sek, self.salt))
            elif t in (3, 4, 5) or b'\x12\x20\x29' in c:
                # KM block in some other ext type: unwrap too
                if len(c) >= 56 and c[:4] == b'\x12\x20\x29':
                    rsalt, rwrap = c[16:32], c[32:56]
                    kek = hashlib.pbkdf2_hmac('sha1', self.passwd.encode(),
                                              rsalt[-8:], 2048, 16)
                    k2 = unwrap(kek, rwrap)
                    if k2:
                        self.keys.append((k2, rsalt))
                        if not self.sek:
                            self.sek, self.salt = k2, rsalt
            i += 4 + l * 4
        self.log(f'agreement {len(ag)}B relay_sock={self.relay_sock:#x} '
                 f'exts=[{" ".join(exts_seen)}] '
                 f'downstream-key={"ok" if self.sek else "MISSING"}')
        if not self.sek:
            self.log('no key in agreement (relay may push one mid-stream); '
                     f'agreement hex: {ag.hex()[:200]}')
        dk = os.environ.get('VIEW_DUMP_KEYS')
        if dk and self.sek:
            __import__('json').dump({'r_sek': self.sek.hex(),
                                     'r_salt': self.salt.hex(),
                                     'v_sek': self.v_sek.hex(),
                                     'v_salt': self.v_salt.hex(),
                                     'pass': self.passwd,
                                     'agreement_hex': ag.hex()},
                                    open(dk, 'w'))

    def _km_update(self, pkt):
        """Any packet carrying a KMSG: unwrap SEK with OUR passphrase and
        add it to the keyring (the relay re-wraps the source's SEK for us)."""
        o = pkt.find(b'\x12\x20\x29')
        if o < 0 or len(pkt) < o + 56:
            return
        rsalt, rwrap = pkt[o + 16:o + 32], pkt[o + 32:o + 56]
        kek = hashlib.pbkdf2_hmac('sha1', self.passwd.encode(),
                                  rsalt[-8:], 2048, 16)
        sek = unwrap(kek, rwrap)
        if sek:
            if not any(k[0] == sek for k in self.keys):
                self.keys.insert(0, (sek, rsalt))
                own = sek == getattr(self, 'v_sek', b'')
                self.log(f'key update: sek={"OUR-OWN-ECHO" if own else "NEW-KEY"}'
                         f' (salt {rsalt[:4].hex()}.., {len(self.keys)} key(s) in ring)')
                cd = os.environ.get('VIEW_CTRL_DUMP')
                if cd:
                    with open(cd, 'ab') as fh:
                        fh.write(struct.pack('<I', len(pkt)) + pkt)
                dk = os.environ.get('VIEW_DUMP_KEYS')
                if dk:
                    try:
                        j = __import__('json').load(open(dk))
                    except Exception:
                        j = {}
                    j['km_update_sek'] = sek.hex()
                    j['km_update_salt'] = rsalt.hex()
                    j['km_update_hex'] = pkt.hex()
                    __import__('json').dump(j, open(dk, 'w'))
        else:
            self.log('key update: SEK unwrap FAILED (passphrase mismatch?)')

    def _handle_data(self, pkt):
        """Process one wire DATA packet: dump, decrypt via keyring, queue,
        ACK. Returns False if the packet is not data."""
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        if len(pkt) < 16 or pkt[0] & 0x80:
            return False
        seq = struct.unpack('>I', pkt[:4])[0] & 0x7FFFFFFF
        if self.wire_f:
            self.wire_f.write(struct.pack('<I', len(pkt)) + pkt)
        pt = None
        for sek, salt in self.keys:
            iv = bytearray(16)
            iv[0:10] = salt[0:10]
            iv[10:14] = bytes(a ^ b for a, b in zip(
                salt[10:14], seq.to_bytes(4, 'big')))
            cand = Cipher(algorithms.AES(sek), modes.CTR(bytes(iv))) \
                .decryptor().update(pkt[16:])
            # full structural check (bit31 alone passes on random data
            # half the time and lets a WRONG key win):
            if len(cand) >= 4:
                w0 = int.from_bytes(cand[0:4], 'little')
                if ((w0 >> 31) & 1 and ((w0 >> 16) & 0x3FFF) == len(cand) - 4) \
                        or (cand[0] == 0x01
                            and struct.unpack('<H', cand[2:4])[0] <= 1312):
                    pt = cand
                    break
        if pt is None and self.keys:
            sek, salt = self.keys[0]
            iv = bytearray(16)
            iv[0:10] = salt[0:10]
            iv[10:14] = bytes(a ^ b for a, b in zip(
                salt[10:14], seq.to_bytes(4, 'big')))
            pt = Cipher(algorithms.AES(sek), modes.CTR(bytes(iv))) \
                .decryptor().update(pkt[16:])
        if pt is not None:
            self.in_q.put(pt)
        self.last_ack = seq
        # stock-SRT receiver ACKing: one full ACK per ~10 ms carrying the
        # cumulative ack number (flooding light ACKs or sending a wrong ack
        # number makes the relay stop forwarding to this viewer).
        now = time.time()
        if now - self.last_full_t >= 0.010:
            self.send(ctrl(2, 0, self.last_ack, now_us(self.t0),
                           self.relay_sock,
                           struct.pack('>IIIIIII', self.last_ack, 1000, 500,
                                       8192, 125000, 1000, 0)))
            self.last_full_t = now
        return True

    def _receive(self):
        self.sock.settimeout(1.0)
        self.last_ack = 0
        self.last_full_t = 0.0
        for pkt in getattr(self, '_early', []):   # cached packets that beat
            if pkt[:1] == b'\x80':                # the agreement
                if b'\x12\x20\x29' in pkt:
                    self._km_update(pkt)
            else:
                self._handle_data(pkt)
        self._early = []
        while not self.dead.is_set():
            try:
                pkt, _ = self.sock.recvfrom(65536)
            except socket.timeout:
                self.send(ctrl(1, 0, 0, now_us(self.t0), self.relay_sock))
                continue
            except OSError:
                break
            if len(pkt) < 16:
                continue
            if pkt[0] & 0x80:                     # control
                cmd = struct.unpack('>H', pkt[:2])[0] & 0x7FFF
                if b'\x12\x20\x29' in pkt:
                    self._km_update(pkt)
                if cmd == 1:                       # keepalive
                    self.send(ctrl(1, 0, 0, now_us(self.t0), self.relay_sock))
                elif cmd not in (0, 2, 3, 5, 6, 7, 8):
                    self.log(f'relay ctrl cmd={cmd:#x} sub='
                             f'{struct.unpack(">H", pkt[2:4])[0]:#x} '
                             f'len={len(pkt)} {pkt[:48].hex()}')
                continue
            self._handle_data(pkt)

class BmosToTs:
    """Re-muxes received BMOS messages into a standards-shaped MPEG-TS:
    PAT/PMT, PES packets carrying the stream's own PTS (BMOS body word 0 =
    PTS_90kHz << 2), PCR on the video pid, per-pid continuity counters.
    A real PMT is what makes the audio track visible to VLC/ffprobe - bare
    elementary streams in TS packets only get sniffed by ffmpeg, if that."""

    PID_V, PID_A, PID_PMT = 0x100, 0x101, 0x1000
    STREAM_TYPES = {'h264': 0x1B, 'hevc': 0x24}   # audio: AAC ADTS 0x0F
    PAT_S = 0.4                      # re-send PAT/PMT at this cadence (s)

    def __init__(self):
        self.codec = None            # 'h264' | 'hevc', set from first NAL
        self.cc = {}                 # pid -> continuity counter
        self._last_pat = 0.0
        self._pend_audio = []        # audio TS bytes held until codec known
        self._t = 0                  # fallback PTS counter (90 kHz ticks)

    # ---- MPEG-2 CRC (poly 0x04C11DB7, init/final all-ones) ----------
    @staticmethod
    def _crc(buf):
        crc = 0xFFFFFFFF
        for b in buf:
            crc ^= b << 24
            for _ in range(8):
                crc = ((crc << 1) ^ 0x04C11DB7 if crc & 0x80000000
                       else crc << 1) & 0xFFFFFFFF
        return crc

    # ---- 188-byte packet with optional adaptation field -------------
    def _pkt(self, pid, payload, pusi=False, af=None):
        cc = self.cc.get(pid, 0)
        self.cc[pid] = (cc + 1) & 0xF
        afc = 0b01 if af is None else 0b11
        hdr = bytes((0x47,
                     (0x40 if pusi else 0) | ((pid >> 8) & 0x1F),
                     pid & 0xFF,
                     (afc << 4) | cc))
        return hdr + (af or b'') + payload

    @staticmethod
    def _stuff_af(pad):
        # adaptation field whose only content is `pad` stuffing bytes
        return bytes((pad - 1,)) + b'\xff' * (pad - 1) if pad > 1 else b'\x00'

    def _section_pkt(self, pid, sec):
        payload = b'\x00' + sec                 # pointer_field 0
        pad = 184 - len(payload)
        return self._pkt(pid, payload + b'', pusi=True,
                         af=self._stuff_af(pad)) if pad > 0 else \
            self._pkt(pid, payload, pusi=True)

    def _pat_pmt(self):
        # section heads: table_id | (len patched later) | tsid/program u16
        # | version/CNI | section# | last#  (tsid/program_number are 16-bit!)
        pat = struct.pack('>BHHBBB', 0x00, 0xB00D, 1, 0xC1, 0, 0) \
            + struct.pack('>HH', 1, 0xE000 | self.PID_PMT)
        pat += struct.pack('>I', self._crc(pat))
        pmt = struct.pack('>BHHBBB', 0x02, 0xB000, 1, 0xC1, 0, 0) \
            + struct.pack('>HH', 0xE000 | self.PID_V, 0xF000)
        if self.codec:
            pmt += struct.pack('>BHH', self.STREAM_TYPES[self.codec],
                               0xE000 | self.PID_V, 0xF000)
            pmt += struct.pack('>BHH', 0x0F, 0xE000 | self.PID_A, 0xF000)
        slen = len(pmt) - 3 + 4
        pmt = pmt[:1] + struct.pack('>H', 0xB000 | slen) + pmt[3:]
        pmt += struct.pack('>I', self._crc(pmt))
        return self._section_pkt(0x0000, pat) + self._section_pkt(self.PID_PMT, pmt)

    # ---- PES with PTS ------------------------------------------------
    @staticmethod
    def _pts_bytes(pts):
        pts &= (1 << 33) - 1
        return bytes((0x20 | ((pts >> 29) & 0x0E) | 1,
                      (pts >> 22) & 0xFF,
                      ((pts >> 14) & 0xFE) | 1,
                      (pts >> 7) & 0xFF,
                      ((pts << 1) & 0xFE) | 1))

    @staticmethod
    def _pcr_bytes(pcr):                     # pcr in 27 MHz units
        base, ext = pcr // 300, pcr % 300
        return bytes(((base >> 25) & 0xFF, (base >> 17) & 0xFF,
                      (base >> 9) & 0xFF, (base >> 1) & 0xFF,
                      ((base & 1) << 7) | 0x7E | ((ext >> 8) & 1),
                      ext & 0xFF))

    @staticmethod
    def _pes(sid, pts, payload, bounded):
        rest = b'\x80\x80\x05' + BmosToTs._pts_bytes(pts)
        ln = (len(rest) + len(payload)) if bounded else 0
        return (bytes((0, 0, 1, sid)) + struct.pack('>H', ln)
                + rest + payload)

    def _emit_pes(self, pid, pes, pcr=None, rai=False):
        out = bytearray()
        total = len(pes)
        pos = 0
        first = True
        while True:
            af = None
            if first and (pcr is not None or rai):
                body = bytearray()
                body.append((0x40 if rai else 0) | (0x10 if pcr is not None else 0))
                if pcr is not None:
                    body += self._pcr_bytes(pcr)
                af = bytearray((0,)) + body        # [len][flags][...] patched
            room = 184 - (len(af) if af is not None else 0)
            chunk = pes[pos:pos + room]
            pos += len(chunk)
            last = pos >= total
            pad = room - len(chunk)
            if last and pad > 0:
                if af is not None:             # extend the existing AF body
                    af += b'\xff' * pad
                else:                          # AF consisting of stuffing only
                    af = bytearray(self._stuff_af(pad))
            if af is not None:
                af[0] = len(af) - 1
                af = bytes(af)
            out += self._pkt(pid, chunk, pusi=first, af=af)
            first = False
            if last:
                break
        return bytes(out)

    # ---- public API ---------------------------------------------------
    def set_codec(self, name):
        """Called once, from the first video NAL. Returns PAT/PMT + any
        audio that arrived before the codec (and thus the PMT) was known."""
        self.codec = name
        flush = b''.join(self._pend_audio)
        self._pend_audio = []
        return self._pat_pmt() + flush

    def add_video(self, nals, pts, keyframe):
        annexb = b''.join(b'\x00\x00\x00\x01' + n for n in nals)
        if pts is None:
            self._t += 3600
            pts = self._t
        pre = b''
        if keyframe or time.time() - self._last_pat >= self.PAT_S:
            self._last_pat = time.time()
            pre = self._pat_pmt()
        pes = self._pes(0xE0, pts, annexb, bounded=False)
        return pre + self._emit_pes(self.PID_V, pes, pcr=pts * 300, rai=keyframe)

    def add_audio(self, adts, pts):
        if pts is None:
            self._t += 1920
            pts = self._t
        pes = self._pes(0xC0, pts, adts, bounded=True)
        pkts = self._emit_pes(self.PID_A, pes)
        if not self.codec:            # PMT needs the video stream type
            self._pend_audio.append(pkts)
            return b''
        return pkts




def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('endpoint', help='output endpoint id (see bmd_claim.py router)')
    ap.add_argument('--out', help='write demuxed TS here')
    ap.add_argument('--push', help='ffmpeg -c copy push target URL')
    ap.add_argument('--play', action='store_true')
    ap.add_argument('--raw', help='also dump raw decrypted BMOS here')
    ap.add_argument('--wire', help='dump ENCRYPTED wire packets here (for '
                                   'offline multi-key analysis)')
    args = ap.parse_args()

    def log(m):
        print(time.strftime('%H:%M:%S'), m, flush=True)

    tok = B.token()
    g = B.Grpc(tok)
    msg = (B.pb_str(1, B.ROUTER_ID) + B.pb_str(2, args.endpoint)
           + B.pb_str(3, B.DEVICE_ID))
    st, info, resp = g.call('Claim', msg, region=True)
    if st != 200:
        sys.exit(f'Claim failed: {st} {info[:120]}')
    log('output claimed; waiting for a live source on its route...')
    st = info = resp = None
    while True:
        # report the routed source's state so the user sees what's missing
        try:
            rs = B.router_state(tok)
            srcs = {e['id']: e.get('status') for e in rs.get('sources', [])}
            routed = [r['source'] for r in rs.get('routes', [])
                      if r['destination'] == args.endpoint]
            desc = ', '.join(f'{s}={srcs.get(s, "?")}' for s in routed) or 'NO ROUTE'
            live = any(srcs.get(s) == 'STATUS_ACTIVE' for s in routed)
            log(f'route source: {desc}')
        except Exception as ex:
            log(f'(state check failed: {ex})')
            live = False
        g.client.timeout = 12
        try:
            st, info, resp = g.call('Connect', msg, region=True)
            if st == 200 and resp:
                break
            log(f'Connect: {st} {str(info)[:60]} - retrying in 5s')
        except Exception as ex:
            log(f'Connect waiting ({type(ex).__name__}) - retry in 5s; '
                'start the bridge+OBS now' if not live else
                f'Connect failed: {type(ex).__name__} - retry in 5s')
        time.sleep(5)
    f = B.pb_parse(resp)
    v = lambda n: f[n][0].decode() if isinstance(f[n][0], bytes) else str(f[n][0])
    if len(f) > 4:
        log(f'ConnectResponse extra fields: '
            f'{ {k: [str(x)[:60] for x in vs] for k, vs in f.items()} }')
    hostport, pw = v(1), v(3)
    sid = B.build_streamid(v(2), v(3), v(4))
    log(f'viewer session: {hostport}')

    host, port = hostport.split(':')
    wire_f = open(args.wire, 'wb') if args.wire else None
    viewer = Viewer(host, port, sid, pw, log, wire_f=wire_f)
    # VIEW_EXTRA_KEYS=json-file with sek/salt hex: seed the keyring with keys
    # from another party (e.g. the bridge's BRIDGE_DUMP_KEYS file) - the relay
    # tunnels the SOURCE's SEK, so the agreement key alone may not decrypt.
    xf = os.environ.get('VIEW_EXTRA_KEYS')
    if xf and os.path.exists(xf):
        j = __import__('json').load(open(xf))
        for sk, sa in ((j.get('b_sek'), j.get('b_salt')),
                       (j.get('sek'), j.get('salt'))):
            if sk and sa:
                viewer.keys.append((bytes.fromhex(sk), bytes.fromhex(sa)))
                log(f'keyring: extra key loaded from {xf}')
    viewer.start()
    if not viewer.ok.wait(10):
        sys.exit('viewer handshake failed')

    raw_f = open(args.raw, 'wb') if args.raw else None
    out_f = open(args.out, 'wb') if args.out else None
    ff = None
    fmt = [None]     # 'h264' | 'hevc', detected from first NAL

    mux = BmosToTs()

    def emit(b):
        if not b:
            return
        if out_f:
            out_f.write(b)
        if ff:
            try:
                ff.stdin.write(b)
            except (BrokenPipeError, ValueError):
                pass

    def detect_and_start(nal):
        if fmt[0]:
            return
        h264 = (nal[0] & 0x1F) in (7, 8, 5, 1)
        fmt[0] = 'h264' if h264 else 'hevc'
        log(f'video format detected: {fmt[0]} (writing MPEG-TS with audio)')
        nonlocal ff
        if args.push:
            ff = subprocess.Popen(
                ['ffmpeg', '-hide_banner', '-loglevel', 'error',
                 '-f', 'mpegts', '-i', '-', '-c', 'copy', '-f', 'flv',
                 args.push])
        elif args.play:
            ff = subprocess.Popen(
                ['ffplay', '-hide_banner', '-loglevel', 'error',
                 '-f', 'mpegts', '-'])
        emit(mux.set_codec(fmt[0]))   # PAT/PMT + audio held so far

    total = vid = aud = 0

    def _nals_of(sec):
        nals = []
        off = 0
        while off + 4 <= len(sec):
            ln = int.from_bytes(sec[off:off + 4], 'big')
            if ln == 0 or off + 4 + ln > len(sec):
                break
            nals.append(sec[off + 4:off + 4 + ln])
            off += 4 + ln
        return nals or None

    t0 = time.time()
    rejoins = int(os.environ.get('VIEW_REJOINS', '0'))
    pend1 = None      # [outer_len, keyframe, section, pts] multi-chunk video
    try:
        while viewer.is_alive() and not viewer.dead.is_set():
            if total == 0 and time.time() - t0 > 8:
                # we joined before the routed source went live: the relay
                # bound this session to nothing and it will never carry
                # data. Release and restart (the join-loop then attaches to
                # the now-live source).
                if rejoins >= 5:
                    log('still no data after re-joins - source really idle?')
                    break
                log('no data 8 s after join - re-joining behind the live source')
                viewer.dead.set()
                try:
                    viewer.sock.close()
                except Exception:
                    pass
                B.unclaim(tok, args.endpoint)
                os.environ['VIEW_REJOINS'] = str(rejoins + 1)
                os.execv(sys.executable,
                         [sys.executable, os.path.abspath(__file__)] + sys.argv[1:])
            try:
                m = viewer.in_q.get(timeout=2)
            except queue.Empty:
                continue
            total += 1
            if raw_f:
                raw_f.write(m)
            vid_ev = aud_ev = None
            w0 = int.from_bytes(m[0:4], 'little') if len(m) >= 4 else 0
            if (w0 >> 31) & 1:
                # BMOS chunk (bit31 checked FIRST - class-1 heads also begin
                # with byte 0x01 and must not be taken for continuations)
                body = m[4:4 + ((w0 >> 16) & 0x3FFF)]
                pts = (int.from_bytes(body[0:4], 'little') >> 2) \
                    if len(body) >= 4 else None
                cls = w0 & 0xFF
                if cls == 1 and (w0 >> 30) & 1 and len(body) >= 20:
                    outer = struct.unpack('<I', body[16:20])[0]
                    vid_ev = (_nals_of(body[20:20 + (outer & 0x7FFFFFFF)]),
                              pts, bool(outer & 0x80000000))
                elif cls == 1 and len(body) >= 20:
                    # multi-chunk head: [hdr][ts][12z][outer][section..]
                    outer = struct.unpack('<I', body[16:20])[0]
                    pend1 = [outer & 0x7FFFFFFF, bool(outer & 0x80000000),
                             body[20:], pts]
                elif cls == 2 and (w0 >> 30) & 1 and len(body) >= 12:
                    # audio: [ts][u32 0][u32 len][ADTS frame]
                    ln = struct.unpack('<I', body[8:12])[0]
                    aud_ev = (body[12:12 + ln], pts)
            elif len(m) >= 4 and m[0] == 0x01:
                # BMD continuation chunk: [0x01][cnt][u16 LE len][section]
                if pend1 is not None:
                    lnb = struct.unpack('<H', m[2:4])[0]
                    pend1[2] += m[4:4 + lnb]
                    if len(pend1[2]) >= pend1[0]:
                        vid_ev = (_nals_of(pend1[2][:pend1[0]]),
                                  pend1[3], pend1[1])
                        pend1 = None
            if vid_ev and vid_ev[0]:
                nals, pts, kf = vid_ev
                vid += 1
                if fmt[0] is None:
                    detect_and_start(nals[0])
                emit(mux.add_video(nals, pts, kf))
            if aud_ev and aud_ev[0]:
                aud += 1
                emit(mux.add_audio(aud_ev[0], aud_ev[1]))
            if total % 200 == 0:
                log(f'received {total} msgs ({total/(time.time()-t0):.0f}/s), '
                    f'video {vid}, audio {aud}')
    except KeyboardInterrupt:
        pass
    log(f'total {total} msgs (video {vid}); releasing output')
    if ff:
        try:
            ff.stdin.close()
        except Exception:
            pass
    for h in (raw_f, out_f, wire_f):
        if h:
            h.close()
    B.unclaim(tok, args.endpoint)
    log('done')


if __name__ == '__main__':
    main()
