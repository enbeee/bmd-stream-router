#!/usr/bin/env python3
"""Offline multi-key analysis of a viewer WIRE dump (bmd_view.py --wire) or a
bridge BRIDGE_DUMP_MSGS file, implementing the receiver grammar exactly as
reversed from davstream.dll:

  chunk header (u32 LE):
    bit31 set  -> BMOS head: [1|last|len:14|counter:8|class:8]
                  last=1: single-chunk message (complete in one)
                  last=0: multi-chunk head (unit starts)
    bit31 clear-> BMD continuation: [class:8][counter:8][len:15|last:1]
                  the 0x4000 bit in the length field marks the FINAL chunk
                  of the unit; mid chunks carry len < 0x4000
  class-1 video message body: [u32 ts][16 zero bytes][u32 outer][section]
    where outer = section_len | 0x80000000 for keyframes; the receiver counts
    SECTION bytes only (head chunk contributes len-20 section bytes)

Tries every provided key (json files with sek/salt hex under any prefix),
picks the key whose decrypt yields a structurally valid chunk, reassembles
messages and classifies them.

Usage: tools_analyze_wire.py <wire.bin> <keys.json> [more keys.json]
                                 [--sent <bridge_msgs.bin>]
"""
import hashlib
import json
import struct
import sys

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


def load_keys(paths):
    keys = []
    for p in paths:
        j = json.load(open(p))
        for a, b in (('sek', 'salt'), ('b_sek', 'b_salt'),
                     ('v_sek', 'v_salt'), ('r_sek', 'r_salt')):
            if j.get(a) and j.get(b):
                keys.append((f'{p}:{a}', bytes.fromhex(j[a]),
                             bytes.fromhex(j[b])))
    return keys


def try_decrypt(sek, salt, seq, ct):
    iv = bytearray(16)
    iv[0:10] = salt[0:10]
    iv[10:14] = bytes(x ^ y for x, y in zip(salt[10:14],
                                            seq.to_bytes(4, 'big')))
    return Cipher(algorithms.AES(sek), modes.CTR(bytes(iv))) \
        .decryptor().update(ct)


def chunk_of(pt):
    """Parse one decrypted chunk per the davstream receiver grammar.
    Returns (type, cls, cnt, body, section_delta) or None if invalid."""
    if len(pt) < 4:
        return None
    w0 = int.from_bytes(pt[0:4], 'little')
    bit31 = (w0 >> 31) & 1
    bit30 = (w0 >> 30) & 1
    cls = w0 & 0xFF
    cnt = (w0 >> 8) & 0xFF
    if bit31:
        ln = (w0 >> 16) & 0x3FFF
        if ln != len(pt) - 4:
            return None
        return (2 if not bit30 else 3), cls, cnt, pt[4:], 0
    # continuation: first byte = class, second = counter
    cls = pt[0]
    cnt = pt[1]
    ln = struct.unpack('<H', pt[2:4])[0]
    if (ln & 0x3FFF) != len(pt) - 4 or (ln & 0x3FFF) > 1312:
        return None
    return (1 if (ln & 0x4000) else 0), cls, cnt, pt[4:], 0


def section_len(head_body, cls):
    """Section bytes contributed by a head chunk body (class-1 video carries
    a 20-byte [ts][12z][outer] header the receiver does not count)."""
    if cls == 1 and len(head_body) >= 20:
        return len(head_body) - 20
    return len(head_body)


def main():
    wire = sys.argv[1]
    sentf = None
    key_paths = []
    rest = sys.argv[2:]
    i = 0
    while i < len(rest):
        if rest[i] == '--sent':
            sentf = rest[i + 1]
            i += 2
        else:
            key_paths.append(rest[i])
            i += 1
    keys = load_keys(key_paths)
    d = open(wire, 'rb').read()
    i = 0
    pkts = []
    while i + 4 <= len(d):
        ln = struct.unpack('<I', d[i:i + 4])[0]
        pkts.append(d[i + 4:i + 4 + ln])
        i += 4 + ln
    tally = {}
    chunks = []
    undec = 0
    for pkt in pkts:
        if len(pkt) < 16 or pkt[0] & 0x80:
            continue
        seq = struct.unpack('>I', pkt[:4])[0] & 0x7FFFFFFF
        hit = None
        for name, sek, salt in keys:
            pt = try_decrypt(sek, salt, seq, pkt[16:])
            if chunk_of(pt):
                hit = (name, pt)
                break
        if hit:
            tally[hit[0]] = tally.get(hit[0], 0) + 1
            chunks.append(hit[1])
        else:
            undec += 1
    print(f'{len(pkts)} wire data packets; decrypted: {dict(tally)}, '
          f'failed: {undec}')

    # receiver-accurate reassembly: per class, accumulate section bytes;
    # completion on head last=1, on continuation 0x4000, or when the
    # accumulated section reaches a class-1 head's outer length.
    msgs = []
    pend = {}          # cls -> {'sec': bytes, 'need': int|None, 'body': bytes}
    for pt in chunks:
        c = chunk_of(pt)
        if c is None:
            continue
        typ, cls, cnt, body, _ = c
        st = pend.get(cls)
        if typ in (2, 3):                    # head
            if typ == 3:
                msgs.append((cls, body))
            else:
                need = None
                if cls == 1 and len(body) >= 20:
                    outer = struct.unpack('<I', body[16:20])[0]
                    need = outer & 0x7FFFFFFF
                pend[cls] = {'sec': body[20:] if need is not None else body,
                             'need': need, 'body': body}
        else:                                # continuation
            if st is None:
                continue                     # unsolicited: receiver drops it
            st['sec'] += body
            st['body'] = st['body'][:20] + st['sec']
            done = typ == 1 or (st['need'] is not None
                                and len(st['sec']) >= st['need'])
            if done:
                msgs.append((cls, st['body']))
                pend.pop(cls, None)
    stats = {'table': 0, 'audio': 0, 'paramset_only': 0, 'keyframe_slice': 0,
             'P_only': 0, 'video_other': 0}
    kf = []
    incomplete = sum(1 for cls in pend)
    for cls, body in msgs:
        if cls == 0:
            stats['table'] += 1
        elif cls == 2:
            stats['audio'] += 1
        elif cls == 1 and len(body) >= 20:
            outer = struct.unpack('<I', body[16:20])[0]
            sec = body[20:20 + (outer & 0x7FFFFFFF)]
            types = []
            off = 0
            while off + 4 <= len(sec):
                lnb = int.from_bytes(sec[off:off + 4], 'big')
                if lnb == 0 or off + 4 + lnb > len(sec):
                    break
                n = sec[off + 4:off + 4 + lnb]
                if n:
                    types.append((n[0] >> 1) & 0x3F if n[0] & 0x81 not in (1,)
                                  else n[0] & 0x1F)
                off += 4 + lnb
            ps = [t for t in types if t in (32, 33, 34, 7, 8)]
            kfs = [t for t in types if t in (19, 20, 21, 5)]
            if kfs:
                stats['keyframe_slice'] += 1
                kf.append(types)
            elif ps and len(ps) == len(types):
                stats['paramset_only'] += 1
            elif types:
                stats['P_only'] += 1
            else:
                stats['video_other'] += 1
    print(f'{len(msgs)} messages reassembled '
          f'({incomplete} unit(s) still open at end of dump):')
    for k, v in stats.items():
        print(f'  {k:15}: {v}')
    if kf:
        print('KEYFRAME MESSAGES THAT SURVIVED:')
        for t in kf[:12]:
            print(f'   nal types: {t}')
    else:
        print('>>> NO keyframe-slice messages survived the router <<<')
    if sentf:
        sd = open(sentf, 'rb').read()
        i = 0
        sent_md5 = {}
        while i + 4 <= len(sd):
            ln = struct.unpack('<I', sd[i:i + 4])[0]
            c = sd[i + 4:i + 4 + ln]
            h = hashlib.md5(c[4:]).hexdigest()
            sent_md5[h] = sent_md5.get(h, 0) + 1
            i += 4 + ln
        recv_md5 = {hashlib.md5(pt[4:]).hexdigest() for pt in chunks}
        matched = sum(1 for m in recv_md5 if m in sent_md5)
        print(f'sent {sum(sent_md5.values())} chunks '
              f'({len(sent_md5)} unique bodies); received chunk bodies '
              f'matching sent verbatim: {matched}')


if __name__ == '__main__':
    main()
