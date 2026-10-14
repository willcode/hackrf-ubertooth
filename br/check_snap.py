#
# SPDX-License-Identifier: BSD-3-Clause

"""Check debug snapshots of the BR channelizer stage by stage (br_fixed arithmetic).

usage: check_snap.py words.(npy|bin)   (bin: raw uint16 little endian from the device)
"""
import sys, os
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import br_fixed as bf

HDR = [0xA55A, 0x5AA5, 0xC33C, 0x3CC3]

def sx(v, bits):
    v &= (1 << bits) - 1
    return v - (1 << bits) if v >> (bits - 1) else v

def snapshots(w):
    i = 0
    while i + 900 <= len(w):
        if list(w[i:i + 4]) == HDR:
            e = w[i + 4:i + 772].astype(np.int64).reshape(256, 3)
            x = w[i + 772:i + 900].astype(np.int64)
            xi = (x & 255) - ((x & 128) << 1)
            xq = (x >> 8) - ((x & 0x8000) >> 7)
            yield e[:, 0] | (e[:, 1] << 16) | (e[:, 2] << 32), np.stack([xi, xq], 1)
            i += 900
        else:
            i += 1

def frames(entries, fields):
    """Group 64 entries into complete frames (index 0..15 in order, one parity)."""
    rows = [fields(int(v)) for v in entries]
    out = []
    for s in range(len(rows) - 15):
        blk = rows[s:s + 16]
        if [r[2] for r in blk] == list(range(16)) and len({r[3] for r in blk}) == 1:
            out.append((blk[0][3], np.array([[r[0], r[1]] for r in blk])))
    return out

pfb_f = lambda v: (sx(v, 12), sx(v >> 12, 12), (v >> 24) & 15, (v >> 28) & 1)
st1_f = lambda v: (sx(v, 14), sx(v >> 14, 14), (v >> 28) & 15, (v >> 32) & 1)
tw_f = lambda v: (sx(v, 15), sx(v >> 15, 15), (v >> 30) & 15, (v >> 34) & 1)
y_f = lambda v: (sx(v, 16), sx(v >> 16, 16), (v >> 32) & 15, (v >> 36) & 1)

def stage1(u):
    """u indexed by PFB order j (p1 = j >> 2, p2 = j & 3) -> A[idx = p1*4 + k1]."""
    out = np.zeros((16, 2), np.int64)
    for p1 in range(4):
        for k1 in range(4):
            sr = si = 0
            for p2 in range(4):
                r, i = u[p1 * 4 + p2]
                rot = (p2 * k1) % 4
                r, i = [(r, i), (-i, r), (-r, -i), (i, -r)][rot]
                sr += r; si += i
            out[p1 * 4 + k1] = (sr, si)
    return out

def twiddle(A):
    out = np.zeros((16, 2), np.int64)
    for idx in range(16):
        p1, k1 = idx >> 2, idx & 3
        c, d = bf.TW_RE[p1, k1], bf.TW_IM[p1, k1]
        a, b = A[idx]
        out[idx] = (bf.rshift_round(a * c - b * d, bf.TW_BITS), bf.rshift_round(a * d + b * c, bf.TW_BITS))
    return out

def stage2(B, par):
    """B[idx = p1*4 + k1] -> Y in output order (k1 outer, k2 inner): Y[k1*4 + k2] = Y_{k1 + 4 k2}."""
    out = np.zeros((16, 2), np.int64)
    for k1 in range(4):
        for k2 in range(4):
            sr = si = 0
            for p1 in range(4):
                r, i = B[p1 * 4 + k1]
                rot = (p1 * k2) % 4
                r, i = [(r, i), (-i, r), (-r, -i), (i, -r)][rot]
                sr += r; si += i
            k = k1 + 4 * k2
            if par and (k & 1):
                sr, si = -sr, -si
            out[k1 * 4 + k2] = (bf.sat(sr, 16), bf.sat(si, 16))
    return out

def yframes(entries):
    rows = [y_f(int(v)) for v in entries]
    order = [k1 + 4 * k2 for k1 in range(4) for k2 in range(4)]
    out = []
    for s in range(len(rows) - 15):
        blk = rows[s:s + 16]
        if [r[2] for r in blk] == order and len({r[3] for r in blk}) == 1:
            out.append((blk[0][3], np.array([[r[0], r[1]] for r in blk])))
    return out

def match(pred_list, got_list, name):
    """For each captured frame, the best matching prediction: (mismatching values, details)."""
    res = []
    for par, got in got_list:
        best = None
        for ppar, pred in pred_list:
            if ppar != par:
                continue
            bad = int(np.sum(pred != got))
            if best is None or bad < best[0]:
                best = (bad, pred, got)
        if best:
            res.append(best)
    return res

def pfb_all(x):
    """Every possible frame end e (sample index): u_p = sum_q h[p+16q] x[e-p-16q] >> 5."""
    out = []
    for e in range(63, len(x)):
        u = np.zeros((16, 2), np.int64)
        for p in range(16):
            acc = np.zeros(2, np.int64)
            for q in range(4):
                acc += bf.H[p + 16 * q] * x[e - p - 16 * q]
            u[p] = bf.sat(acc >> bf.U_SHIFT, 12)
        # in PFB order j: p = (j >> 2) + 4 (j & 3)
        out.append((e, np.array([u[(j >> 2) + 4 * (j & 3)] for j in range(16)])))
    return out

def check(w, verbose=True, per_frame=None):
    """Check every snapshot in the words w; returns {stage: [mismatching values per snapshot]},
    counting the best matching frame of each snapshot. per_frame, a dict, receives every frame's
    count: {stage: [[per frame] per snapshot]}; a stage's first frame may have no predecessor in
    the snapshot."""
    tot = {}
    for n, (snap, x) in enumerate(snapshots(w)):
        P = frames(snap[0:64], pfb_f)
        cand = pfb_all(x)
        pbad = []
        for par, u in P:
            bad = min(int(np.sum(c != u)) for e, c in cand)
            pbad.append(bad)
        tot.setdefault('bank', []).append(min(pbad) if pbad else -1)
        if verbose and n < 6:
            print('snapshot %d: bank frames vs model from the input: %s wrong of 32' % (n, pbad))
            if pbad and min(pbad):
                par, u = P[int(np.argmin(pbad))]
                e, c = min(cand, key=lambda ec: int(np.sum(ec[1] != u)))
                for j in range(16):
                    print('   j %2d got %s model %s' % (j, u[j], c[j]))
        A = frames(snap[64:128], st1_f)
        B = frames(snap[128:192], tw_f)
        Y = yframes(snap[192:256])
        r1 = match([(p, stage1(u)) for p, u in P], A, 'stage1')
        r2 = match([(p, twiddle(a)) for p, a in A], B, 'twiddle')
        r3 = match([(p, stage2(b, p)) for p, b in B], Y, 'fft')
        line = []
        for name, r in (('stage1', r1), ('twiddle', r2), ('fft', r3)):
            if per_frame is not None:
                per_frame.setdefault(name, []).append([x[0] for x in r])
            if r:
                bad = min(x[0] for x in r)
                tot.setdefault(name, []).append(bad)
                line.append('%s %d/32 wrong' % (name, bad))
                if verbose and bad and n < 2:
                    b, pred, got = min(r, key=lambda x: x[0])
                    rows = np.nonzero((pred != got).any(axis=1))[0]
                    for i in rows[:6]:
                        line.append('\n   idx %2d pred %s got %s' % (i, pred[i], got[i]))
            else:
                line.append('%s n/a' % name)
        if verbose and n < 6:
            print('snapshot %d: frames %d/%d/%d/%d: ' % (n, len(P), len(A), len(B), len(Y)) + ', '.join(line))
    return tot


def main():
    w = np.load(sys.argv[1]) if sys.argv[1].endswith('.npy') else np.fromfile(sys.argv[1], '<u2')
    tot = check(w)
    for k, v in tot.items():
        print('%-8s %d snapshots, %d with mismatches' % (k, len(v), sum(1 for x in v if x)))


if __name__ == '__main__':
    main()
