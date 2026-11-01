#!/usr/bin/env python3
"""ACK/flow-control profile of a router session capture (pcap/pcapng).

For each UDP flow between the PC and the relay, reconstruct:
  - handshake (induction/conclusion) fields incl. advertised windows
  - data sequence space (ISN, count, dup pattern)
  - every relay ACK: full payload fields vs the sender's sent-count at that
    moment -> answers "is the ack number absolute (ISN space) or 0-based?"
  - ACK buffer-field trajectory (does the relay advertise a shrinking window?)
  - sender pauses (gaps in data) and what preceded them
  - keepalive/ACKACK cadence, shutdown at the end
Usage: tools_ack_profile.py 10.pcap
"""
import struct
import sys


def ip(b):
    return '.'.join(str(x) for x in b)


def iter_pcap(d):
    if d[:4] in (b'\xd4\xc3\xb2\xa1', b'\x4d\x3c\xb2\xa1', b'\x4d\x3c\xb2\x1a'):
        end = '<'
    elif d[:4] in (b'\xa1\xb2\xc3\xd4', b'\xa1\xb2\x3c\x4d', b'\x1a\xb2\x3c\x4d'):
        end = '>'
    else:
        return
    link = struct.unpack(end + 'I', d[20:24])[0]
    i = 24
    n = 0
    while i + 16 <= len(d):
        ts, tu, cap, orig = struct.unpack(end + 'IIII', d[i:i + 16])
        i += 16
        pkt = d[i:i + cap]
        i += cap
        n += 1
        div = 1e9 if d[:4] in (b'\x4d\x3c\xb2\xa1', b'\xa1\xb2\x3c\x4d') else 1e6
        yield n, ts + tu / div, link, pkt


def iter_pcapng(d):
    if len(d) < 12 or struct.unpack('<I', d[8:12])[0] != 0x1A2B3C4D:
        return
    end = '<'
    i = 0
    n = 0
    link = 1
    while i + 12 <= len(d):
        btype, blen = struct.unpack(end + 'II', d[i:i + 8])
        if blen < 12 or i + blen > len(d):
            break
        body = d[i + 8:i + blen - 4]
        if btype == 1:      # IDB
            link = struct.unpack(end + 'H', body[:2])[0]
        elif btype == 6:    # EPB
            iid, hi, lo, cap, orig = struct.unpack(end + 'IIIII', body[:20])
            pkt = body[20:20 + cap]
            n += 1
            # fraction uses the full 32-bit scale (binary ns): lo/2^32 s
            yield n, hi + lo / 4294967296.0, link, pkt
        i += blen


def udp_flows(link, pkt):
    off = None
    if link == 1:
        if len(pkt) < 14:
            return
        et = struct.unpack('>H', pkt[12:14])[0]
        off = 14
        while et in (0x8100, 0x88A8) and len(pkt) >= off + 4:
            et = struct.unpack('>H', pkt[off + 2:off + 4])[0]
            off += 4
        if et != 0x0800:
            return
    elif link == 101:
        off = 0
    elif link == 113:
        off = 16
    if off is None or len(pkt) < off + 20 or pkt[off] >> 4 != 4:
        return
    ihl = (pkt[off] & 0xF) * 4
    if pkt[off + 9] != 17:
        return
    src, dst = pkt[off + 12:off + 16], pkt[off + 16:off + 20]
    uo = off + ihl
    if len(pkt) < uo + 8:
        return
    sp, dp, ul, _ = struct.unpack('>HHHH', pkt[uo:uo + 8])
    return (f'{ip(src)}:{sp}', f'{ip(dst)}:{dp}', pkt[uo + 8:min(uo + ul, len(pkt))])




def load(fn):
    d = open(fn, 'rb').read()
    pkts = list(iter_pcap(d)) or list(iter_pcapng(d))
    if not pkts:
        sys.exit(f'{fn}: unparseable')
    return pkts


def main(fn, relay_prefix):
    global RELAY_PREFIX
    RELAY_PREFIX = relay_prefix
    pkts = load(fn)
    t0 = pkts[0][1]
    print(f'=== {fn}: {len(pkts)} packets, span {pkts[-1][1] - t0:.1f}s')
    flows = {}
    alludp = {}
    for n, ts, link, pkt in pkts:
        fl = udp_flows(link, pkt)
        if not fl:
            continue
        src, dst, payload = fl
        key = tuple(sorted((src, dst)))
        alludp[key] = alludp.get(key, 0) + 1
        flows.setdefault(key, []).append((ts - t0, src, dst, payload, n))
    print('all UDP flows:')
    for k, v in sorted(alludp.items(), key=lambda x: -x[1]):
        print(f'  {k[0]} <-> {k[1]}: {v}')

    for key, evs in flows.items():
        a, b = key
        relay_side = a if is_relay(a) else (b if is_relay(b) else None)
        if not relay_side:
            continue
        print(f'\n--- flow {a} <-> {b}: {len(evs)} pkts')
        profile_flow(evs, relay_side)


RELAY_PREFIX = '216.66.81.'      # override with --relay <prefix>


def is_relay(ep):
    return ep.startswith(RELAY_PREFIX)


def profile_flow(evs, relay):
    isn = None
    sent = 0                       # distinct data seqs sent (dedup by seq)
    sent_seq = []
    last_data_t = None
    gaps = []                      # (t, gap_s, seq_at_gap)
    dups = 0
    ctrl_out = {}                  # PC -> relay control cmds
    ctrl_in = {}                   # relay -> PC control cmds
    acks = []                      # (t, info, payload_fields, sent_by_then)
    ackacks = []
    shutdowns = []
    hs = []
    for t, src, dst, p, n in evs:
        from_pc = dst.startswith('216.66.81.')
        if not (p[0] & 0x80):      # data
            seq = struct.unpack('>I', p[:4])[0] & 0x7FFFFFFF
            if from_pc:
                if isn is None:
                    isn = seq
                if sent_seq and sent_seq[-1] != seq and t - last_data_t > 0.25:
                    gaps.append((t, t - last_data_t, seq))
                if seq in sent_seq[-3:]:
                    dups += 1
                sent_seq.append(seq)
                sent += 1
                last_data_t = t
            continue
        cmd, sub, info = struct.unpack('>HHI', p[:8])
        cmd &= 0x7FFF
        if from_pc:
            ctrl_out[cmd] = ctrl_out.get(cmd, 0) + 1
            if cmd == 6:
                ackacks.append((t, info))
            if cmd == 0:
                hs.append((t, 'out', p))
        else:
            ctrl_in[cmd] = ctrl_in.get(cmd, 0) + 1
            if cmd == 2:
                fields = struct.unpack('>7I', p[16:16 + 28]) \
                    if len(p) >= 16 + 28 else None
                acks.append((t, info, fields, p[16:16 + 28].hex()
                             if fields is None else None, sent))
            if cmd == 5:
                shutdowns.append(t)
            if cmd == 0:
                hs.append((t, 'in', p))

    print(f'  PC->relay data: {sent} pkts, ISN={isn if isn is None else hex(isn)},'
          f' dup-pattern pkts={dups}')
    if sent_seq:
        seqs = [((s - sent_seq[0]) & 0x7FFFFFFF) for s in sent_seq]
        print(f'  seq span: first={sent_seq[0]:#x} last={sent_seq[-1]:#x} '
              f'(delta {seqs[-1]})')
    print(f'  ctrl PC->relay: { {hex(k): v for k, v in sorted(ctrl_out.items())} }')
    print(f'  ctrl relay->PC: { {hex(k): v for k, v in sorted(ctrl_in.items())} }')
    for t, direction, p in hs[:6]:
        body = p[16:]
        print(f'  handshake {direction} t={t:.3f}s len={len(p)} '
              f'body[:32]={body[:32].hex()}')
    if acks:
        print(f'  relay ACKs: {len(acks)} total')
        show = acks[:8] + (acks[len(acks) // 2:len(acks) // 2 + 4] + acks[-8:]
                           if len(acks) > 24 else [])
        seen = set()
        for t, info, f, rawhex, sbt in show:
            k = round(t, 3)
            if k in seen:
                continue
            seen.add(k)
            if f:
                (la, rtt, rttv, buf, rate, cap, recvrate) = f
                print(f'    t={t:7.2f}s ack_info={info:#010x} '
                      f'last_ack={la:#010x} rtt={rtt} var={rttv} buf={buf} '
                      f'rate={rate} cap={cap} recv={recvrate} | pc_sent={sbt}'
                      f' | info-0={info} la-isn='
                      f'{(la - isn) & 0x7FFFFFFF if isn is not None else "?"}')
            else:
                print(f'    t={t:7.2f}s ack_info={info:#010x} raw={rawhex} '
                      f'pc_sent={sbt}')
        ack_ints = [acks[i + 1][0] - acks[i][0] for i in range(len(acks) - 1)]
        if ack_ints:
            ack_ints.sort()
            print(f'    ACK interval ms: p50={ack_ints[len(ack_ints)//2]*1e3:.1f} '
                  f'p90={ack_ints[int(len(ack_ints)*0.9)]*1e3:.1f} '
                  f'max={ack_ints[-1]*1e3:.1f}')
        infos = [a[1] for a in acks]
        las = [a[2][0] if a[2] else 0 for a in acks]
        print(f'    info range: {min(infos)}..{max(infos)}  '
              f'last_ack range: {min(las)}..{max(las)}')
        if isn is not None:
            rel = [(la - isn) & 0x7FFFFFFF for la in las]
            print(f'    last_ack - ISN range: {min(rel)}..{max(rel)} '
                  f'(pc sent {sent})')
    if ackacks:
        ints = [ackacks[i + 1][0] - ackacks[i][0]
                for i in range(len(ackacks) - 1)]
        ints.sort()
        print(f'  PC ACKACKs: {len(ackacks)}, interval p50='
              f'{ints[len(ints)//2]*1e3:.1f} ms')
    if gaps:
        print(f'  sender pauses > 250 ms ({len(gaps)}):')
        for t, g, s in gaps[:12]:
            print(f'    t={t:7.2f}s gap={g*1e3:6.0f} ms (next seq '
                  f'{(s - (isn or 0)) & 0x7FFFFFFF if sent_seq else s})')
    else:
        print('  sender pauses > 250 ms: none')
    if shutdowns:
        print(f'  shutdown packets at: {[round(t, 2) for t in shutdowns]}')


if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('capture')
    p.add_argument('--relay', default='216.66.81.', help='relay IP prefix')
    a = p.parse_args()
    main(a.capture, a.relay)
