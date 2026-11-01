#!/usr/bin/env python3
"""OBS -> Blackmagic Cloud Stream Router bridge.

OBS (or any SRT encoder) streams to the LOCAL listener this script runs; the
bridge re-dials the BMD relay as an SRT caller and adds the proprietary BMOS
announce extension (0xBD01) to the handshake conclusion - the piece stock
libsrt cannot send and without which the router accepts the stream but never
routes it to the outputs (verified by packet diff, Oct 24 2026).

Local leg is UNENCRYPTED (give OBS no passphrase). Relay leg replicates
libsrt's crypto: PBKDF2-HMAC-SHA1(2048) KEK, RFC3394-wrapped random SEK,
AES-CTR payload with IV = salt[0:10] || (salt[10:14]^seq) || ctr16.

Usage:
  # one-shot: claim a router input and serve the bridge:
  python3 bmd_bridge.py --claim [endpoint_id]

  # manual (values from `python3 bmd_claim.py claim <endpoint>`):
  python3 bmd_bridge.py --host <edge-host> --port <edge-port> \
      --sid '#!::s=<expiry>,u=<claim>' --pass '<secret>'

  OBS: Settings -> Stream -> Custom:
       srt://127.0.0.1:9100?streamid=obs        (no passphrase!)

Requires: pip install httpx[http2] cryptography   (httpx only for --claim)
"""
import argparse
import hashlib
import os
import queue
import random
import socket
import struct
import sys
import threading
import time

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# ---------------------------------------------------------------- crypto ----
def rfc3394_wrap(kek, key):
    n = len(key) // 8
    a = 0xA6A6A6A6A6A6A6A6
    r = [int.from_bytes(key[i*8:(i+1)*8], 'big') for i in range(n)]
    enc = Cipher(algorithms.AES(kek), modes.ECB()).encryptor()
    for j in range(6):
        for i in range(n):
            blk = enc.update(a.to_bytes(8, 'big') + r[i].to_bytes(8, 'big'))
            a = int.from_bytes(blk[:8], 'big') ^ (n*j + i + 1)
            r[i] = int.from_bytes(blk[8:], 'big')
    return a.to_bytes(8, 'big') + b''.join(x.to_bytes(8, 'big') for x in r)

def make_km(passphrase):
    salt = os.urandom(16)
    sek = os.urandom(16)
    kek = hashlib.pbkdf2_hmac('sha1', passphrase.encode(), salt[-8:], 2048, 16)
    wrap = rfc3394_wrap(kek, sek)
    km = (bytes.fromhex('12202901000000000200020000000404') + salt + wrap)
    return sek, salt, km

def ctr_iv(salt, seq):
    iv = bytearray(16)
    iv[0:10] = salt[0:10]
    iv[10:14] = bytes(a ^ b for a, b in zip(salt[10:14],
                                            seq.to_bytes(4, 'big')))
    return bytes(iv)

def encrypt_payload(sek, salt, seq, payload):
    c = Cipher(algorithms.AES(sek), modes.CTR(ctr_iv(salt, seq))).encryptor()
    return c.update(payload)

# ------------------------------------------------------------- SRT helpers --
def ctrl(cmd, sub, info, ts, dst, payload=b''):
    return (struct.pack('>HHIII', 0x8000 | cmd, sub, info, ts, dst) + payload)

def now_us(t0):
    return int((time.time() - t0) * 1_000_000) & 0xFFFFFFFF

def le_pack_sid(sid):
    b = sid.encode()
    b += b'\x00' * (-len(b) % 4)
    return b''.join(b[i:i+4][::-1] for i in range(0, len(b), 4))

def bmos_announce():
    """BD01 blob with a fresh per-connection token (type-2 TLV, u32).
    Resolve increments a per-socket counter here; the router stores this value
    - a stale/replayed token makes the media session a duplicate."""
    tok = random.getrandbits(16)
    return (bytes.fromhex('424d4f53010001000008000000080000')
            + struct.pack('<HH', 2, 1) + struct.pack('>H', tok) + b'\x00\x00')

# In-stream BMOS stream table ("BTST"), captured from a live Resolve session and
# adapted per codec. The router will not start output egress without it.
BTST_H265 = bytes.fromhex(
    '000130c0425453540000010030000000010000004832'
    '363500000000000000000000000061616320')
BTST_H264 = BTST_H265.replace(b'H265', b'H264')
BTST_H264 += bytes(52 - len(BTST_H264))   # captured packet is 52B incl. padding
HSREQ = bytes.fromhex('00010500000000bf00880088')   # today's Resolve latency profile

# ------------------------------------------------------------- relay caller -
class RelayCaller(threading.Thread):
    """Dials the BMD relay: handshake with SID+KMREQ+BMOS, streams TS."""

    def __init__(self, host, port, sid, passphrase, out_q, log, reclaim=None):
        super().__init__(daemon=True)
        self.host, self.port = host, int(port)
        self.sid, self.passphrase = sid, passphrase
        self.out_q = out_q          # queue of TS payload bytes from OBS leg
        self.log = log
        self.reclaim = reclaim      # fn -> (host, port, sid, passphrase) to
        # mint a fresh session when the old claim lapsed (--claim mode only)
        self.need_dial = threading.Event()   # relay shut us down / session dead
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(5.0)
        self.addr = (host, int(port))
        self.relay_sock = None
        self.ok = threading.Event()
        self.dead = threading.Event()
        self.sek = self.salt = None
        self.seq = self.isn = random.getrandbits(31)
        self.msgno = 1
        self.tx = {}                # seq -> full packet (for NAK retransmit)
        self.remux = TsToBmos(log)
        self.cnt = {0: 0, 1: 0, 2: 0}   # per-class rolling message counters
        self.frames_sent = 0
        self.t0 = time.time()
        self.last_send = 0.0
        self.acked = 0
        self.last_ack_rx = 0.0       # relay ACK liveness (for auto-reconnect)
        self.acked_seq = 0           # data pkts acked, ISN-relative count
        # (from ACK payload word0 last_ack - ISN; see the cmd==2 handler)
        self.sent_count = 0          # data packets sent this session
        self.reconnects = 0
        # Resolve's SRT stack sends EVERY outgoing datagram twice (~10 ms
        # apart, identical seq/msgno; the receiver dedups by seq). The relay
        # accepts single-copy streams fine - duplication is optional
        # redundancy and DOUBLES the uplink load (on a constrained line it
        # starves everything else, incl. the sender's own internet).
        # Opt-in with BRIDGE_DUP=1.
        self.dup = os.environ.get('BRIDGE_DUP', '0') == '1'
        self.dupe_q = []            # [(due_time, pkt)]
        self._rx_gen = 0            # bumped on re-dial; retires old rx/duper

    def send(self, pkt):
        self.sock.sendto(pkt, self.addr)
        self.last_send = time.time()

    def _dupe(self, pkt):
        """Queue the second copy of a datagram (Resolve behavior)."""
        if self.dup:
            self.dupe_q.append((time.time() + 0.010, pkt))

    def _duper(self):
        while not self.dead.is_set():
            now = time.time()
            due = [p for t, p in self.dupe_q if t <= now]
            if due:
                self.dupe_q = [(t, p) for t, p in self.dupe_q if t > now]
                for p in due:
                    try:
                        self.sock.sendto(p, self.addr)
                    except OSError:
                        pass
            time.sleep(0.004)

    def _hs_pkts(self):
        mysock = random.getrandbits(31)
        isn = self.isn
        indu = (ctrl(0, 0, 0, now_us(self.t0), 0)
                + struct.pack('>IIIII', 4, 2, isn, 1500, 8192)
                + struct.pack('>II', 1, mysock)
                + bytes(4) + bytes.fromhex('0025142d') + bytes(12))
        return mysock, indu

    def handshake(self):
        if os.environ.get('BRIDGE_DUMP_KEYS'):
            pass  # keys made in conclusion below; dumped there
        mysock, indu = self._hs_pkts()
        self.send(indu)
        try:
            resp, _ = self.sock.recvfrom(2048)
        except socket.timeout:
            self.log('relay ignored the induction (no live claim? try again)')
            raise
        body = resp[16:]
        cookie = body[28:32]
        # conclusion
        sek, salt, km = make_km(self.passphrase)
        self.sek, self.salt = sek, salt
        if os.environ.get('BRIDGE_DUMP_KEYS'):
            import json as _json
            _json.dump({'b_sek': sek.hex(), 'b_salt': salt.hex()},
                       open(os.environ['BRIDGE_DUMP_KEYS'], 'w'))
        exts = b''
        exts += struct.pack('>HH', 1, 3) + HSREQ
        sidw = le_pack_sid(self.sid)
        exts += struct.pack('>HH', 5, len(sidw) // 4) + sidw
        exts += struct.pack('>HH', 3, len(km) // 4) + km
        exts += struct.pack('>HH', 0xBD01, 6) + bmos_announce()
        concl = (ctrl(0, 0, 0, now_us(self.t0), 0)
                 + struct.pack('>IIIII', 5, 7, self.isn, 1500, 8192)
                 + struct.pack('>II', 0xFFFFFFFF, mysock) + cookie
                 + bytes(16) + exts)
        self.send(concl)
        self._dupe(concl)          # Resolve sends the conclusion twice too
        try:
            ag, _ = self.sock.recvfrom(2048)
        except socket.timeout:
            self.log('relay ignored the conclusion (sid/passphrase/KM rejected)')
            raise
        self.relay_sock = struct.unpack('>I', ag[16 + 24:16 + 28])[0]
        if len(ag) < 100 or not self.relay_sock or self.relay_sock == 1:
            # a real agreement is ~160B with a valid socket id; short/empty
            # responses are rejections (stale claim, session still held, ...)
            raise RuntimeError(f'edge rejected the session '
                               f'({len(ag)}B agreement, retry after re-claim)')
        self.acked_seq = 0        # ISN-relative acked count (payload word0)
        self.sent_count = 0
        self.last_ack_rx = time.time()
        # does the agreement echo the BMOS block? (relay->Resolve did)
        bmos = bytes.fromhex('bd010004424d4f53')
        self.log(f'relay agreement {len(ag)}B relay_sock={self.relay_sock:#x} '
                 f'BMOS-echo={"YES" if bmos in ag else "no"}')
        self.ok.set()

    def _reconnect(self):
        """Fresh SRT session on the same socket - after a relay shutdown or
        a silently-dead session (edge kills idle ones). Falls back to a
        brand-new claim (--claim mode) when the old credentials lapsed."""
        try:
            self._dial()
        except Exception as ex:
            if not self.reclaim:
                raise
            self.log(f're-dial rejected ({ex}) - minting a fresh claim')
            creds = self.reclaim()
            if not creds:
                raise RuntimeError('re-claim failed - restart the bridge')
            self.host, self.port, self.sid, self.passphrase = creds
            self.addr = (self.host, int(self.port))
            self._dial()
        self.reconnects += 1
        self.need_dial.clear()
        self.log(f're-dial ok (#{self.reconnects}) - stream continues')

    def _dial(self):
        self._rx_gen += 1
        if getattr(self, '_rx_thread', None):
            self._rx_thread.join(timeout=1.5)   # old rx must not eat our
            self._rx_thread = None              # new handshake responses
        self.seq = self.isn = random.getrandbits(31)
        self.msgno = 1
        self.tx.clear()
        self.dupe_q.clear()
        self.relay_sock = None
        self.last_ack_rx = 0.0
        self.handshake()
        self._rx_thread = threading.Thread(target=self._rx, daemon=True)
        self._rx_thread.start()

    def _rx(self):
        gen = self._rx_gen
        self.sock.settimeout(1.0)
        while not self.dead.is_set() and gen == self._rx_gen:
            try:
                pkt, _ = self.sock.recvfrom(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            if len(pkt) < 16 or not (pkt[0] & 0x80):
                continue
            cmd, sub, info = struct.unpack('>HHI', pkt[:8])
            cmd &= 0x7FFF     # strip the 0x8000 control bit or the
            # ACK/NAK/keepalive branches below NEVER match (this exact bug
            # silently disabled ACKACKs + NAK retransmit since day one)
            if os.environ.get('BRIDGE_DEBUG_CTRL'):
                self._dbg_ctrl = getattr(self, '_dbg_ctrl', {})
                self._dbg_ctrl[cmd] = self._dbg_ctrl.get(cmd, 0) + 1
                if sum(self._dbg_ctrl.values()) % 50 == 1 or cmd not in (2,):
                    self.log(f'relay ctrl {self._dbg_ctrl} last cmd={cmd:#x} '
                             f'sub={sub:#x} info={info:#x}')
            if cmd == 2:      # ACK. 10.pcap ground truth: the header `info`
                self.last_ack_rx = time.time()
                # field is the ACK EVENT COUNTER (1,2,3...1661 in a 5 s
                # session); the actual data-ack is payload word 0 (last_ack,
                # next-expected seq) in OUR sequence space. Treating info as
                # the data ack made inflight grow by 1 per sent packet
                # regardless of acks -> false pause at exactly MAX_INFLIGHT
                # (~2.7 MB) -> idle teardown -> re-dial churn: the entire
                # "relay stops acking at ~2049 packets" wall.
                if len(pkt) >= 20:
                    la = struct.unpack('>I', pkt[16:20])[0] & 0x7FFFFFFF
                    rel = (la - self.isn) & 0x7FFFFFFF
                    if rel <= (((self.seq - self.isn) & 0x7FFFFFFF) + 64):
                        if rel > self.acked_seq:
                            self.acked_seq = rel
                if info:      # ACKACK echoes the event counter (per-ACK,
                    # exactly like Resolve: p50 interval 1.2 ms in 10.pcap)
                    self.send(ctrl(6, 0, info, now_us(self.t0),
                                   self.relay_sock) + bytes(4))
            elif cmd == 3:    # NAK - retransmit
                w = pkt[16:]
                i = 0
                while i + 4 <= len(w):
                    v = struct.unpack('>I', w[i:i+4])[0]
                    i += 4
                    if v & 0x80000000 and i + 4 <= len(w):
                        rng = range(v & 0x7FFFFFFF,
                                    struct.unpack('>I', w[i:i+4])[0] + 1)
                        i += 4
                    else:
                        rng = range(v & 0x7FFFFFFF, (v & 0x7FFFFFFF) + 1)
                    for s in rng:
                        p = self.tx.get(s)
                        if p:
                            self.send(p)
            elif cmd == 1:    # keepalive
                self.send(ctrl(1, 0, 0, now_us(self.t0), self.relay_sock))
            elif cmd == 5:    # shutdown (relay sends this ~5s after media
                # stops; the session is gone but the bridge stays up and
                # re-dials when the encoder sends again)
                self.log('relay sent shutdown - will re-dial on encoder data')
                self._rx_gen += 1        # retire this rx thread
                self.relay_sock = None
                self.need_dial.set()
                return

    @staticmethod
    def _classify_msg(msg):
        """Corpus messages are stored WITHOUT their BMOS chunk header (bare
        bodies): BTST.. = table, [ts][12 zeros].. = video, else = audio."""
        if msg[:4] == b'BTST':
            return 0
        if len(msg) >= 20 and msg[4:16] == bytes(12):
            return 1
        return 2

    def _replay_chunks(self, msg):
        # re-chunk a bare BMOS body: BMOS head chunk + BMD continuation
        # chunks (see _chunk)
        cls = self._classify_msg(msg)
        self.cnt[cls] += 1
        counter = self.cnt[cls]
        return self._chunk(cls, msg, counter)

    MAX_INFLIGHT = 2048     # unACKed packets allowed; beyond this we pause
    CHUNK_GAP = 0.0008     # pacing between chunks of one message (~1.6 MB/s
    # ceiling). The edge FIREWALLS sources whose bursts look like a flood:
    # back-to-back 20-60 packet bursts (large frames) tripped its DoS
    # protection within seconds and it blackholed the sender's IP - which
    # also killed every other flow from that machine ("internet takeover").

    def _send_bmos(self, msg):
        # flow control: without this the bridge blasts at line rate whenever
        # the edge stalls (wedged/blackholed session) and saturates the
        # sender's whole uplink. Both counters are ISN-relative packet counts
        # (see the ACK handler) so this is now a true un-acked window; the
        # reference session (10.pcap) never exceeded ~10 un-acked.
        inflight = ((self.seq - self.isn) & 0x7FFFFFFF) - self.acked_seq
        if inflight > self.MAX_INFLIGHT:
            self.log(f'flow control: {inflight} packets unACKed - pausing send')
            t_pause = time.time()
            while ((((self.seq - self.isn) & 0x7FFFFFFF) - self.acked_seq)
                   > self.MAX_INFLIGHT and not self.dead.is_set()):
                if time.time() - t_pause > 10.0:
                    self.log('relay stopped ACKing for 10 s - forcing re-dial')
                    self.need_dial.set()
                    return
                time.sleep(0.05)
        if not self.relay_sock:
            return          # session mid-re-dial; drop rather than crash
        if os.environ.get('BRIDGE_DUMP_MSGS'):
            with open(os.environ['BRIDGE_DUMP_MSGS'], 'ab') as fh:
                fh.write(struct.pack('<I', len(msg)) + msg)
        hdr = (struct.pack('>II', self.seq, 0xE8000000 | self.msgno)
               + struct.pack('>II', now_us(self.t0), self.relay_sock))
        pkt = hdr + encrypt_payload(self.sek, self.salt, self.seq, msg)
        self.tx[self.seq] = pkt
        if len(self.tx) > 4096:
            self.tx.pop(next(iter(self.tx)))
        self.send(pkt)
        self._dupe(pkt)
        self.sent_count += 1
        time.sleep(self.CHUNK_GAP)     # pacing: avoid flood-looking bursts
        self.seq = (self.seq + 1) & 0x7FFFFFFF or 1
        self.msgno = (self.msgno + 1) & 0x3FFFFFF or 1

    def run(self):
        try:
            self.handshake()
        except Exception as ex:
            self.log(f'relay handshake FAILED: {ex}')
            return
        threading.Thread(target=self._duper, daemon=True).start()
        self._rx_thread = threading.Thread(target=self._rx, daemon=True)
        self._rx_thread.start()
        idle = 0.0
        sent_table = False
        inj = os.environ.get('BRIDGE_INJECT')
        if inj:
            time.sleep(1.0)
            d = open(inj, 'rb').read()
            msgs = []
            i = 0
            while i + 4 <= len(d):
                ln = struct.unpack('<I', d[i:i+4])[0]
                msgs.append(d[i+4:i+4+ln])
                i += 4 + ln
            loops = int(os.environ.get('BRIDGE_INJECT_LOOPS', '3'))
            for lp in range(loops):
                for msg in msgs:
                    for m in self._replay_chunks(msg):
                        self._send_bmos(m)
                    time.sleep(1 / 24)
                self.log(f'inject: loop {lp + 1}/{loops} done ({len(msgs)} msgs)')
            self.log('inject: replay finished, holding session open 30s')
            time.sleep(30)
            self.dead.set()      # let main() exit + unclaim cleanly
            return
        while not self.dead.is_set():
            # relay liveness: the edge kills idle sessions (usually with a
            # shutdown notice, sometimes silently). Detect both and re-dial
            # so OBS stop/start + pause/resume keeps working.
            starved = (self.last_ack_rx and self.tx
                       and time.time() - self.last_ack_rx > 8.0
                       and time.time() - self.last_send < 3.0)
            if (self.need_dial.is_set() or starved) and not self.out_q.empty():
                # only re-dial when the encoder actually has data pending -
                # no point holding an edge session while OBS is idle
                if starved:
                    self.log('relay stopped ACKing (session died) - re-dialing')
                try:
                    self._reconnect()
                except Exception as ex:
                    self.log(f're-dial failed ({ex}) - retrying in 5s')
                    self.need_dial.set()
                    time.sleep(5)
                continue
            if time.time() - getattr(self, '_last_stat', 0.0) > 5.0:
                self._last_stat = time.time()
                self.log(f'stats: {self.frames_sent} frames, '
                         f'{(self.seq - self.isn) & 0x7FFFFFFF} pkts, '
                         f'{self.acked_seq} acked, rc={self.reconnects}')
            try:
                payload = self.out_q.get(timeout=1.0)
                for kind, pes in self.remux.feed(payload):
                    if kind == 'v':
                        if not sent_table and self.remux.codec:
                            self.cnt[0] += 1
                            for m in self.remux.bmos_table(self.cnt[0]):
                                self._send_bmos(m)
                            self.log(f'remux: table sent ({self.remux.codec}'
                                     + ('+aac' if self.remux.audio_pid else '')
                                     + ')')
                            sent_table = True
                        pts, off = self.remux.pes_pts(pes)
                        self.cnt[1] += 1
                        for m in self.remux.frame_to_bmos(
                                pes[off:], self.cnt[1], pts):
                            self._send_bmos(m)
                        self.frames_sent += 1
                        if self.frames_sent % 50 == 0:   # re-announce table
                            self.cnt[0] += 1
                            for m in self.remux.bmos_table(self.cnt[0]):
                                self._send_bmos(m)
                    elif kind == 'a':
                        pts, off = self.remux.pes_pts(pes)
                        es = pes[off:]
                        for i, frame in self.remux.split_adts(es):
                            fpts = None if pts is None else pts + i * 1920
                            self.cnt[2] += 1
                            for m in self.remux.audio_to_bmos(
                                    frame, self.cnt[2], fpts):
                                self._send_bmos(m)
            except queue.Empty:
                if self.relay_sock and time.time() - self.last_send > 1.0:
                    self.send(ctrl(1, 0, 0, now_us(self.t0), self.relay_sock))
            except Exception as ex:
                # a malformed PES/PMT must never kill the relay thread
                self.log(f'remux error (packet dropped): {ex}')

    def stop(self):
        if self.relay_sock:
            try:
                self.send(ctrl(5, 0, 0, now_us(self.t0), self.relay_sock))
            except Exception:
                pass
        self.dead.set()


# ------------------------------------------------ TS -> BMOS remuxer -------
class TsToBmos:
    """Demuxes MPEG-TS (Annex-B H.264/H.265 in PES), re-emits BMOS messages:
    class-1 video message per frame: [u32 ts][16 zero bytes][Annex-B NALs]."""

    CHUNK = int(os.environ.get('BRIDGE_CHUNK', '1312'))  # payload per chunk;
    # lower it (e.g. 1200) to fit paths with MTU < 1500 such as a tailscale
    # exit node (MTU 1280) - full-size SRT packets silently die there

    def __init__(self, log=lambda m: None):
        self.log = log
        self.buf = b''
        self.pmt_pid = None
        self.video_pid = None
        self.audio_pid = None
        self.stream_type = None
        self.pes = {}                 # pid -> partial PES bytes
        self.frame_idx = 0
        self.codec = None
        self._pts_base = None

    @staticmethod
    def pes_pts(pes):
        """PES -> (pts90k or None, payload_offset)."""
        if len(pes) < 9 or pes[:3] != b'\x00\x00\x01':
            return None, 0
        flags = pes[7]
        hlen = pes[8]
        pts = None
        if flags & 0x80 and len(pes) >= 14:
            w = pes[9:14]
            pts = (((w[0] >> 1) & 7) << 30 | w[1] << 22 | (w[2] >> 1) << 15
                   | w[3] << 7 | (w[4] >> 1))
        return pts, 9 + hlen

    def feed(self, ts_chunk):
        """Feed raw TS bytes; yields complete frame payloads (Annex-B)."""
        self.buf += ts_chunk
        out = []
        while len(self.buf) >= 188:
            if self.buf[0] != 0x47:
                i = self.buf.find(b'\x47')
                if i < 0:
                    self.buf = b''
                    return out
                self.buf = self.buf[i:]
                continue
            pkt = self.buf[:188]
            self.buf = self.buf[188:]
            f = self._parse_ts(pkt)
            if f:
                out.append(f)
        return out

    def _parse_ts(self, pkt):
        pid = ((pkt[1] & 0x1F) << 8) | pkt[2]
        pusi = pkt[1] & 0x40
        afc = (pkt[3] >> 4) & 3
        off = 4
        if afc & 2:
            off += 1 + pkt[4]
        if not (afc & 1):
            return None
        if pid == 0:
            if pusi:
                self._parse_pmt_pid(pkt[off:])
        elif pid == self.pmt_pid:
            if pusi:
                self._parse_pmt(pkt[off:])
        elif pid == self.video_pid or pid == self.audio_pid:
            f = self._pes_input(pid, pusi, pkt[off:])
            if f is not None:
                return ('v' if pid == self.video_pid else 'a', f)
        return None

    def _parse_pmt_pid(self, body):
        body = body[body[0] + 1:] if body[0] < 182 else body   # pointer_field
        if len(body) < 12 or body[0] != 0:
            return
        # PAT: [tid][len2][prog#2][ver][sec][last][entry: prog#2 @8, pmt_pid @10]
        self.pmt_pid = ((body[10] & 0x1F) << 8) | body[11]

    def _parse_pmt(self, body):
        body = body[body[0] + 1:] if body[0] < 182 else body
        if len(body) < 12 or body[0] != 2:
            return
        pil = ((body[10] & 0xF) << 8) | body[11]
        i = 12 + pil
        while i + 5 <= len(body):
            st = body[i]; epid = ((body[i+1] & 0x1F) << 8) | body[i+2]
            esil = ((body[i+3] & 0xF) << 8) | body[i+4]
            if st in (0x1B, 0x24, 0x42) and self.video_pid is None:
                self.video_pid = epid
                self.stream_type = st
                self.codec = {0x1B: 'H264', 0x24: 'H265', 0x42: 'H264'}[st]
                self.log(f'remux: video pid {epid:#x} type {st:#x} ({self.codec})')
            if st in (0x0F, 0x11) and self.audio_pid is None:
                self.audio_pid = epid
                self.log(f'remux: audio pid {epid:#x} (ADTS)')
            i += 5 + esil

    def _pes_input(self, pid, pusi, payload):
        if pusi:
            frame = self.pes.pop(pid, None)
            self.pes[pid] = payload
            return frame
        cur = self.pes.get(pid)
        if cur is not None:
            self.pes[pid] = cur + payload
        return None

    @staticmethod
    def strip_pes(pes):
        """Strip PES header, return the elementary stream payload."""
        if len(pes) < 9 or pes[:3] != b'\x00\x00\x01':
            return pes
        hdr_len = pes[8]
        return pes[9 + hdr_len:]

    @staticmethod
    def split_adts(es):
        """Byte stream -> list of complete ADTS frames."""
        frames = []
        i = 0
        while i + 7 <= len(es):
            if es[i] == 0xFF and (es[i+1] & 0xF0) == 0xF0:
                ln = ((es[i+3] & 3) << 11) | (es[i+4] << 3) | (es[i+5] >> 5)
                if ln >= 7 and i + ln <= len(es):
                    frames.append((i, es[i:i+ln]))
                    i += ln
                    continue
            i += 1
        return frames

    @staticmethod
    def split_nals(es):
        """Annex-B -> list of bare NALs (start codes stripped)."""
        nals = []
        i = 0
        n = len(es)
        starts = []
        while i < n:
            j = es.find(b'\x00\x00\x01', i)
            if j < 0:
                break
            z = j
            while z > i and es[z-1] == 0:
                z -= 1
            starts.append((z, j + 3))
            i = j + 3
        for k, (s, d) in enumerate(starts):
            e = starts[k+1][0] if k+1 < len(starts) else n
            if d < e:
                nals.append(es[d:e])
        return nals

    # BMD's pipeline accepts only paramset + slice NALs. No SEI (x264/NVENC
    # stamp the encoder name there - historically rejected by BMD decoders),
    # no AUD, no RASL leading pictures (they reference the open-GOP CRA that
    # we retype to IDR below). Verified against a real Resolve stream: GOP
    # messages contain exactly VPS/SPS/PPS/IDR, nothing else.
    # CRA (21) MUST be kept: x265 defaults to open-GOP, so every keyframe
    # after the first is CRA_NUT(21), never IDR. Filtering 21 out (the old
    # bug) silently deletes every keyframe slice - the stream then reaches
    # the router P-frames-only, which darkens YT/egress, crashes the Remote
    # Monitor and keeps the web-UI input tiles dark.
    KEEP_H264 = {1, 5, 7, 8}
    KEEP_H265 = {0, 1, 19, 20, 21, 32, 33, 34}

    def frame_to_bmos(self, nals_annexb, counter, pts90k=None):
        # normalize pts so the first frame is ts=0 (Resolve-like base)
        if pts90k is not None:
            if self._pts_base is None:
                self._pts_base = pts90k
            pts90k = max(0, pts90k - self._pts_base)
        """One video frame -> BMOS class-1 message(s).
        body = [u32 LE ts][12 zeros][u32 LE outer = len|0x80000000*keyframe]
               section = concat([u32 BE nal_len][NAL])"""
        ts = ((pts90k << 2) if pts90k is not None
              else self.frame_idx * 14400) & 0xFFFFFFFF
        self.frame_idx += 1
        nals = self.split_nals(nals_annexb)
        keep = self.KEEP_H265 if self.codec == 'H265' else self.KEEP_H264
        nals = [n for n in nals if n and (
            ((n[0] >> 1) & 0x3F) in keep if self.codec == 'H265'
            else (n[0] & 0x1F) in keep)]
        if not nals:
            return []
        if self.codec == 'H265':
            # IDR_N_LP(20) -> IDR_W_RADL(19) is a safe header retype (identical
            # slice-segment header syntax). CRA(21) must NOT be retyped: its
            # slice header carries poc_lsb/RPS where IDR carries
            # no_output_of_prior_pics/long_term_reference flags - retyping CRA
            # as IDR produces garbage for any strict parser and the router
            # STOPS forwarding that input (live-verified: fwd died after ~2
            # GOP messages). CRA passes through unchanged; encoders should
            # use closed GOP anyway (x265: open-gop=0, OBS/NVENC default).
            fixed = []
            for n in nals:
                t = (n[0] >> 1) & 0x3F
                if t == 20:
                    n = bytes([(19 << 1) | (n[0] & 1)]) + n[1:]
                fixed.append(n)
            nals = fixed
        # optionally override paramsets with a known-good set (hex json)
        pf = os.environ.get('BRIDGE_PARAMSETS')
        if pf:
            import json as _j
            ps = _j.load(open(pf))
            new = []
            for n in nals:
                t = ((n[0] >> 1) & 0x3F) if self.codec == 'H265' else (n[0] & 0x1F)
                if self.codec == 'H265' and t == 32 and 'vps' in ps:
                    new.append(bytes.fromhex(ps['vps'])); continue
                if self.codec == 'H265' and t == 33 and 'sps' in ps:
                    new.append(bytes.fromhex(ps['sps'])); continue
                if self.codec == 'H265' and t == 34 and 'pps' in ps:
                    new.append(bytes.fromhex(ps['pps'])); continue
                new.append(n)
            nals = new
        keyframe = any(
            ((n[0] >> 1) & 0x3F) in (32, 33, 34, 35, 19, 20) if self.codec == 'H265'
            else (n[0] & 0x1F) in (5, 7) for n in nals if n)
        params = [n for n in nals if self.codec == 'H265'
                  and ((n[0] >> 1) & 0x3F) in (32, 33, 34)
                  or self.codec != 'H265' and (n[0] & 0x1F) in (7, 8)]
        slices = [n for n in nals if n not in params]
        out = []
        if params and slices and os.environ.get('BRIDGE_SPLIT_GOP') == '1':
            # SPLIT mode (opt-in): paramsets as their own message, slices
            # separately. The router DROPS slice-only keyframe messages sent
            # this way (live-tested Oct 24: 16/16 paramset msgs forwarded,
            # 0/19 IDR-slice msgs). Default = UNsplit, Resolve's exact shape:
            # VPS+SPS+PPS+IDR in one message.
            sec_p = b''.join(struct.pack('>I', len(n)) + n for n in params)
            out += self._chunk(1, struct.pack('<I', ts) + bytes(12)
                               + struct.pack('<I', len(sec_p) | 0x80000000) + sec_p,
                               counter)
            counter += 1
            sec_s = b''.join(struct.pack('>I', len(n)) + n for n in slices)
            out += self._chunk(1, struct.pack('<I', ts) + bytes(12)
                               + struct.pack('<I', len(sec_s) | (0x80000000 if keyframe else 0))
                               + sec_s, counter)
            return out
        section = b''.join(struct.pack('>I', len(n)) + n for n in nals if n)
        outer = len(section) | (0x80000000 if keyframe else 0)
        body = (struct.pack('<I', ts) + bytes(12)
                + struct.pack('<I', outer) + section)
        return self._chunk(1, body, counter)

    def audio_to_bmos(self, adts_frame, counter, pts90k=None):
        """One ADTS AAC frame -> BMOS class-2 message.
        body = [u32 LE ts][u32 0][u32 LE len][ADTS frame]"""
        ts = ((pts90k << 2) if pts90k is not None else 0) & 0xFFFFFFFF
        body = (struct.pack('<II', ts, 0) + struct.pack('<I', len(adts_frame))
                + adts_frame)
        return self._chunk(2, body, counter)

    def bmos_table(self, counter):
        # 16-byte entries (grammar from the davstream parser + W1 table)
        e_v = struct.pack('<II', 0x30, 1) + self.codec.encode() + bytes(4)
        e_a = struct.pack('<II', 0, 0) + b'aac ' + bytes(4) if self.audio_pid else b''
        body = b'BTST' + bytes.fromhex('00000100') + e_v + e_a
        if len(body) % 16:
            body += bytes(16 - len(body) % 16)
        return self._chunk(0, body, counter)

    def _chunk(self, cls, body, counter):
        """BMD chunking (byte-verified from a real Resolve capture):
        - single-chunk message: one BMOS-headered chunk, last=1
        - multi-chunk: ONLY the first chunk carries the BMOS header
          (last=0); continuation chunks use their own 4-byte header
          [0x01][msg counter][u16 BE payload length][section bytes].
          Repeating the BMOS header on continuations gets forwarded by the
          relay but CRASHES the Remote Monitor and darkens the egress -
          BMD's parser reads every headered chunk as a new message head."""
        pkts = []
        first = True
        while True:
            part = body[:self.CHUNK]
            body = body[self.CHUNK:]
            if first:
                last = 1 if not body else 0
                hdr = (0x80000000 | (last << 30) | (len(part) << 16)
                       | ((counter & 0xFF) << 8) | cls)
                pkts.append(struct.pack('<I', hdr) + part)
                first = False
            else:
                pkts.append(bytes((0x01, counter & 0xFF))
                            + struct.pack('<H', len(part)) + part)
            if not body:
                return pkts

# ------------------------------------------------------------- OBS listener -
class ObsListener(threading.Thread):
    """Minimal SRT listener for the local encoder leg (unencrypted)."""

    dump_file = None

    def __init__(self, bind_host, bind_port, out_q, log, on_first=None):
        super().__init__(daemon=True)
        self.addr = (bind_host, bind_port)
        self.out_q = out_q
        self.log = log
        self.on_first = on_first
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(1.0)
        self.dead = threading.Event()
        self.peer = None
        self.mysock = random.getrandbits(31)
        self.cookie = random.getrandbits(31)
        self.last_ack = 0
        self.ackno = 0
        self.unacked = 0
        self.last_full = 0.0
        self.t0 = time.time()

    def _ack(self, force=False):
        now = time.time()
        if force or self.unacked >= 8 or (self.unacked and now - self.last_full > 0.01):
            self.ackno += 1
            payload = struct.pack('>IIIIIII', self.last_ack, 1000, 500,
                                  8192, 125000, 1000, 0)
            self.sock.sendto(ctrl(2, 0, self.ackno, now_us(self.t0),
                                  self.peer[1], payload), self.peer[0])
            self.unacked = 0
            self.last_full = now

    def run(self):
        dbg = os.environ.get('BRIDGE_DEBUG')
        self.sock.bind(self.addr)
        self.log(f'local SRT listener on {self.addr[0]}:{self.addr[1]} '
                 f'(unencrypted) - point OBS here')
        agreed = False
        while not self.dead.is_set():
            try:
                pkt, src = self.sock.recvfrom(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            if dbg:
                print(f'DBG rx {len(pkt)}B {pkt[:80].hex()}', flush=True)
            if len(pkt) < 16:
                continue
            if pkt[:4] == b'\x80\x00\x00\x00':                    # handshake
                body = pkt[16:]
                if len(body) >= 48 and body[28:32] == bytes(4):   # induction
                    self.peer = (src, struct.unpack('>I', body[24:28])[0])
                    resp = (ctrl(0, 0, 0, now_us(self.t0), self.peer[1])
                            + struct.pack('>IIIII', 5, 0x00004a17,
                                          struct.unpack('>I', body[8:12])[0],
                                          1500, 8192)
                            + struct.pack('>II', 1, self.peer[1])
                            + struct.pack('>I', self.cookie)
                            + struct.pack('>I', self.mysock) + bytes(12))
                    if dbg: print(f'DBG tx-indu {len(resp)}B {resp.hex()}', flush=True)
                    self.sock.sendto(resp, src)
                elif len(body) >= 48 and body[28:32] == struct.pack('>I', self.cookie):
                    isn = struct.unpack('>I', body[8:12])[0]
                    self.last_ack = (isn - 1) & 0x7FFFFFFF
                    ag = (ctrl(0, 0, 0, now_us(self.t0), self.peer[1])
                          + struct.pack('>IIIII', 5, 1, isn, 1500, 8192)
                          + struct.pack('>II', 0xFFFFFFFF, self.mysock)
                          + bytes(4) + bytes(16)
                          + struct.pack('>HH', 2, 3)
                          + bytes.fromhex('00010500000000bb00780078'))
                    if dbg: print(f'DBG tx-agree {len(ag)}B {ag.hex()}', flush=True)
                    self.sock.sendto(ag, src)
                    agreed = True
                    self.log('encoder connected')
                continue
            if (pkt[0] & 0x80):                                    # control
                cmd, sub, info = struct.unpack('>HHI', pkt[:8])
                if cmd == 1:
                    if self.peer:
                        self.sock.sendto(ctrl(1, 0, 0, now_us(self.t0),
                                              self.peer[1]), src)
                elif cmd == 5:
                    # stay up: the encoder (OBS restart, pause/resume) may
                    # come back - the relay re-dials when data resumes
                    self.log('encoder disconnected - waiting for reconnect')
                    agreed = False
                    self.peer = None
                continue
            if not agreed:
                continue
            seq = struct.unpack('>I', pkt[:4])[0] & 0x7FFFFFFF
            self.last_ack = seq
            self.unacked += 1
            if self.on_first:
                self.on_first()
                self.on_first = None
            if self.dump_file:
                self.dump_file.write(pkt[16:])
            self.out_q.put(pkt[16:])
            self._ack()

    def stop(self):
        if self.peer:
            try:
                self.sock.sendto(ctrl(5, 0, 0, now_us(self.t0), self.peer[1]),
                                 self.peer[0])
            except Exception:
                pass
        self.dead.set()

# ------------------------------------------------------------------- main ---
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--host'), ap.add_argument('--port')
    ap.add_argument('--sid'), ap.add_argument('--pass', dest='password')
    ap.add_argument('--claim', nargs='?', const='')
    ap.add_argument('--listen', default='127.0.0.1:9100')
    ap.add_argument('--keepalive-log', type=int, default=10)
    ap.add_argument('--dump-ts', metavar='FILE',
                    help='write the raw TS received from the encoder to FILE')
    args = ap.parse_args()

    def log(msg):
        print(time.strftime('%H:%M:%S'), msg, flush=True)

    host = port = sid = password = None
    endpoint = None
    if args.claim is not None:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import bmd_claim as B
        tok = B.token()
        if os.environ.get('BRIDGE_ROUTER'):
            B.set_router(os.environ['BRIDGE_ROUTER'], tok)  # alt router+region
        else:
            B.resolve_router(tok)   # auto-select when the account has one
        g = B.Grpc(tok)
        import re as _re
        msg = b'\x0a\x01\x01' + B.pb_str(2, B.DEVICE_ID) + B.pb_varint(4, 100)
        st, info, resp = g.call('GetEndpoints', msg)
        ids = [i.decode() for i in _re.findall(rb'endpoint_[0-9a-z]+', resp)]
        todo = [args.claim] if args.claim else ids
        for eid in todo:
            r = B.claim(tok, eid, quiet=True)
            if r and all(r.get(f) for f in (1, 2, 3, 4)):
                v = lambda f: (r[f][0].decode() if isinstance(r[f][0], bytes)
                               else str(r[f][0]))
                hostport = v(1)
                sid = B.build_streamid(v(2), v(3), v(4))
                password = v(3)
                host, port = hostport.split(':')
                endpoint = eid
                log(f'claimed {eid} -> {hostport}')
                log('IMPORTANT: start OBS streaming within ~90 seconds '
                    '(claim expiry)')
                break
            B.unclaim(tok, eid)
        if not host:
            sys.exit('no free router input')
    else:
        if not all((args.host, args.port, args.sid, args.password)):
            sys.exit('need --host/--port/--sid/--pass or --claim')
        host, port, sid, password = (args.host, args.port, args.sid,
                                     args.password)

    lh, lp = args.listen.rsplit(':', 1)
    q = queue.Queue(maxsize=4096)

    def drop_old():
        while True:
            try:
                q.get_nowait()
            except queue.Empty:
                return

    relay = RelayCaller(host, port, sid, password, q, log)
    if endpoint:
        def do_reclaim():
            """Fresh device-plane credentials when the old claim lapsed."""
            try:
                r = B.claim(B.token(), endpoint, quiet=True)
                if r and all(r.get(f) for f in (1, 2, 3, 4)):
                    v = lambda f: (r[f][0].decode()
                                   if isinstance(r[f][0], bytes) else str(r[f][0]))
                    h, p = v(1).split(':')
                    return h, p, B.build_streamid(v(2), v(3), v(4)), v(3)
            except Exception as ex:
                log(f're-claim failed: {ex}')
            return None
        relay.reclaim = do_reclaim

    def first_data():
        if not relay.is_alive():
            log('first encoder data - dialing relay...')
            relay.start()
            if not relay.ok.wait(6):
                log('relay handshake did not complete - stream will NOT '
                    'reach the router')

    listener = ObsListener(lh, int(lp), q, log, on_first=first_data)
    if args.dump_ts:
        listener.dump_file = open(args.dump_ts, 'wb')
        log(f'dumping encoder TS to {args.dump_ts}')
    listener.start()
    if os.environ.get('BRIDGE_INJECT'):
        first_data()   # inject mode: dial the relay now, no local feeder
    log('bridge up. OBS custom server:')
    log(f'  srt://{lh}:{lp}?streamid=obs')
    log('Ctrl+C to stop (releases nothing; unclaim separately if --claim)')

    dropped = 0
    try:
        while True:
            time.sleep(args.keepalive_log)
            if q.full():
                drop_old(); dropped += 4096
            status = (f'queued={q.qsize()} relay={"UP" if relay.ok.is_set() else "down"}'
                      f' frames={relay.frames_sent} codec={relay.remux.codec}'
                      + (f' (dropped {dropped})' if dropped else ''))
            log(status)
            if relay.dead.is_set():
                log('relay session ended')
                break
    except KeyboardInterrupt:
        pass
    listener.stop()
    relay.stop()
    if endpoint:
        try:
            B.unclaim(B.token(), endpoint)
            log(f'unclaimed {endpoint}')
        except Exception:
            pass

if __name__ == '__main__':
    main()
