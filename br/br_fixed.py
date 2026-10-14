#!/usr/bin/env python3
#
# SPDX-License-Identifier: BSD-3-Clause

"""
br_fixed - bit-exact model of the HackRF Pro BR channelizer gateware.

Input: int8 I/Q at 16 Msps. Output: one 16-bit word per symbol (1 Msym/s);
bit k is the decision for channel k (fc + k MHz for k = 0..7, fc + (k-16)
MHz for k = 9..15); bit 8 (the Nyquist bin, not a channel) is set on the
first word of each 400-symbol bank. br_words_to_dump() turns the words into
an ubertooth-dump file for ubertooth-rx -i.

Arithmetic (each step is what the gateware does):
  filter bank  M = 16 branches, T = 4 taps, D = 8: u_p = sum_q h[p+16q] x[n-p-16q],
               8-bit x 8-bit products, 18-bit sums, then >> U_SHIFT, saturated to 12 bits
  FFT          Y_k = sum_p u_p exp(+j 2 pi k p / 16), radix 4: 4-point stage (no
               multiplies, 14 bits), twiddles W^(p1 k1) in Q1.11 (rounded >> 11),
               4-point stage (16 bits); output index k1 + 4 k2
  rotate       Y_k * (-1)^(k m) for frame m
  demod        5-bit normalization, 6-bit phase from a table, phase step over two
               frames (one symbol), carrier-offset threshold and sampling-phase
               metrics as leaky averages (>> 4) updated when the channel's exponent
               is at least E_TH
"""
import struct
import sys

import numpy as np

M, T, D = 16, 4, 8
U_SHIFT = 5
TW_BITS = 11
E_TH_DEFAULT = 6
# Leak suppression. A strong signal also decodes on nearby channels (1 MHz
# away it turns 2 pi per symbol, invisible to the one-symbol detector) and
# at the LO's bins (+-fs/4). A channel within 4 of the previous frame's
# strongest channel, or at bin 4 or 12, whose exponent is LEAK_DE or more
# steps (6 dB each) below that maximum sends 0101... instead of decisions.
LEAK_DE_DEFAULT = 3
BANK = 400
# the order in which the gateware's FFT delivers the channels of a frame
ORDER = np.array([k1 + 4 * k2 for k1 in range(4) for k2 in range(4)])


def coefficients(cutoff_hz=0.6e6, fs=16e6, beta=6.0):
    L = M * T
    n = np.arange(L) - (L - 1) / 2
    fc = cutoff_hz / fs
    h = 2 * fc * np.sinc(2 * fc * n) * np.kaiser(L, beta)
    return np.round(h / h.max() * 127).astype(np.int64)


H = coefficients()


def twiddles():
    """W^(p1 k1) for p1, k1 in 0..3, W = exp(+j 2 pi / 16), Q1.11 integers."""
    p1 = np.arange(4)[:, None]
    k1 = np.arange(4)[None, :]
    w = np.exp(2j * np.pi * p1 * k1 / 16)
    s = 1 << TW_BITS
    return (np.round(w.real * s).astype(np.int64), np.round(w.imag * s).astype(np.int64))


TW_RE, TW_IM = twiddles()


def phase_table():
    """6-bit phase of 5-bit I, Q (index (I & 31) << 5 | (Q & 31))."""
    t = np.zeros(1024, np.int64)
    for i in range(-16, 16):
        for q in range(-16, 16):
            t[((i & 31) << 5) | (q & 31)] = int(np.round(np.arctan2(q, i) / (2 * np.pi) * 64)) & 63
    return t


PHASE = phase_table()


def sat(v, bits):
    lo, hi = -(1 << (bits - 1)), (1 << (bits - 1)) - 1
    return np.clip(v, lo, hi)


def rshift_round(v, s):
    return (v + (1 << (s - 1))) >> s


class Model:
    def __init__(self, e_th=E_TH_DEFAULT, leak_de=LEAK_DE_DEFAULT):
        self.e_th = e_th
        self.leak_de = leak_de          # 0: no leak suppression
        self.kmax = 0                   # the previous frame's strongest channel
        self.emax = 0
        self.hist = np.zeros((M * T - 1, 2), np.int64)
        self.m = 0
        self.ph1 = np.zeros(M, np.int64)       # phase one frame ago
        self.ph2 = np.zeros(M, np.int64)       # two frames ago
        self.thr = np.zeros(M, np.int64)
        self.acc = np.zeros((2, M), np.int64)  # metric per frame parity
        self.bit_even = np.zeros(M, np.int64)
        self.nwords = 0

    def frontend(self, x):
        """x: (n, 2) int I/Q, n a multiple of D. Returns Y (frames, 16, 2) int."""
        buf = np.concatenate([self.hist, x])
        L = M * T
        nfr = len(x) // D
        idx = (np.arange(nfr) * D + D - 1 + L - 1)[:, None] - np.arange(L)[None, :]
        seg = buf[idx]                                  # (nfr, L, 2), newest first
        prod = seg * H[None, :, None]                   # 8 x 8 bit products
        u = prod.reshape(nfr, T, M, 2).sum(axis=1)      # 18-bit branch sums
        u = sat(u >> U_SHIFT, 12)
        self.hist = buf[-(L - 1):]
        ur, ui = u[..., 0], u[..., 1]
        # stage 1: A[p1][k1] = sum_p2 u[p1 + 4 p2] j^(p2 k1)
        Ar = np.zeros((nfr, 4, 4), np.int64)
        Ai = np.zeros((nfr, 4, 4), np.int64)
        for p1 in range(4):
            a = [(ur[:, p1 + 4 * p2], ui[:, p1 + 4 * p2]) for p2 in range(4)]
            for k1 in range(4):
                sr = np.zeros(nfr, np.int64)
                si = np.zeros(nfr, np.int64)
                for p2 in range(4):
                    r, i = a[p2]
                    rot = (p2 * k1) % 4            # multiply by j^rot
                    if rot == 0:
                        sr += r; si += i
                    elif rot == 1:
                        sr -= i; si += r
                    elif rot == 2:
                        sr -= r; si -= i
                    else:
                        sr += i; si -= r
                Ar[:, p1, k1] = sr
                Ai[:, p1, k1] = si
        # twiddles
        Br = rshift_round(Ar * TW_RE[None] - Ai * TW_IM[None], TW_BITS)
        Bi = rshift_round(Ar * TW_IM[None] + Ai * TW_RE[None], TW_BITS)
        # stage 2: Y[k1 + 4 k2] = sum_p1 B[p1][k1] j^(p1 k2)
        Y = np.zeros((nfr, 16, 2), np.int64)
        for k1 in range(4):
            for k2 in range(4):
                sr = np.zeros(nfr, np.int64)
                si = np.zeros(nfr, np.int64)
                for p1 in range(4):
                    r, i = Br[:, p1, k1], Bi[:, p1, k1]
                    rot = (p1 * k2) % 4
                    if rot == 0:
                        sr += r; si += i
                    elif rot == 1:
                        sr -= i; si += r
                    elif rot == 2:
                        sr -= r; si -= i
                    else:
                        sr += i; si -= r
                Y[:, k1 + 4 * k2, 0] = sr
                Y[:, k1 + 4 * k2, 1] = si
        # (-1)^(k m)
        mm = (self.m + np.arange(nfr))[:, None]
        flip = ((mm * np.arange(16)[None, :]) & 1).astype(bool)
        Y[flip] = -Y[flip]
        self.m += nfr
        return sat(Y, 16)

    @staticmethod
    def normalize(Y):
        """(…, 2) 16-bit -> exponent e, 5-bit I, Q, 6-bit phase."""
        I, Q = Y[..., 0], Y[..., 1]
        mag = np.maximum(np.where(I < 0, ~I, I), np.where(Q < 0, ~Q, Q))
        e = np.zeros_like(mag)
        for b in range(16):
            e = np.where(mag >> b != 0, b + 1, e)
        s = np.maximum(e - 4, 0)
        i5 = I >> s
        q5 = Q >> s
        ph = PHASE[((i5 & 31) << 5) | (q5 & 31)]
        return e, ph

    def demod(self, Y):
        """Per frame recursions. Returns the output words."""
        e, ph = self.normalize(Y)
        words = []
        for f in range(len(Y)):
            parity = (self.m - len(Y) + f) & 1
            d = (ph[f] - self.ph2) & 63
            d = np.where(d >= 32, d - 64, d)
            leak = np.zeros(M, bool)
            if self.leak_de:
                k = np.arange(M)
                dist = np.minimum((k - self.kmax) % M, (self.kmax - k) % M)
                near = ((dist >= 1) & (dist <= 4)) | (k == 4) | (k == 12)
                leak = near & (e[f] + self.leak_de <= self.emax)
            self.kmax = int(ORDER[np.argmax(e[f][ORDER])])   # first to arrive on ties
            self.emax = int(e[f][self.kmax])
            self.ph2 = self.ph1
            self.ph1 = ph[f]
            sig = e[f] >= self.e_th
            c = (d << 4) - self.thr
            # a suppressed channel sends 0101... (zeros would look like a
            # repetition-coded header to the access code validator)
            bit = np.where(leak, self.nwords & 1, (c > 0).astype(np.int64))
            self.thr = np.where(sig, self.thr + (((d << 4) - self.thr) >> 4), self.thr)
            a = self.acc[parity]
            self.acc[parity] = np.where(sig, a + ((np.abs(c) - a) >> 4), a)
            if parity == 0:
                self.bit_even = bit
            else:
                use_odd = self.acc[1] > self.acc[0]
                out = np.where(use_odd, bit, self.bit_even)
                w = int(sum(int(out[k]) << k for k in range(16) if k != 8))
                if self.nwords % BANK == 0:
                    w |= 1 << 8
                words.append(w)
                self.nwords += 1
        return words

    def process(self, x):
        return self.demod(self.frontend(x))


def words_to_dump(words, fc_mhz, path, t0_ns=0):
    """Pack the words into ubertooth-dump records, one per channel per bank."""
    words = np.asarray(words, np.int64)
    starts = np.flatnonzero(words & (1 << 8))
    chans = {k: (int(fc_mhz) + (k if k < 8 else k - 16)) - 2402 for k in range(16) if k != 8}
    chans = {k: c for k, c in chans.items() if 0 <= c <= 78}
    with open(path, 'wb') as f:
        for b, s in enumerate(starts):
            blk = words[s:s + BANK]
            if len(blk) < BANK:
                break
            t_ns = t0_ns + s * 1000
            clk100ns = (t_ns // 100) & 0xffffffff
            clkn = int(t_ns / 312500)
            for k, c in sorted(chans.items()):
                bits = ((blk >> k) & 1).astype(np.uint8)
                data = np.packbits(bits).tobytes()
                pkt = struct.pack('<BBBBIbbbB2x', 0, 0, c, (clkn >> 20) & 0xff, clk100ns,
                                  0, 0, 0, 0) + data
                f.write(struct.pack('>I', t_ns // 1000000000) + pkt)


def main():
    import argparse
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('iq')
    ap.add_argument('--fc', type=float, required=True)
    ap.add_argument('--seconds', type=float, default=3)
    ap.add_argument('--start', type=float, default=0, help='seconds into the capture')
    ap.add_argument('--e-th', type=int, default=E_TH_DEFAULT)
    ap.add_argument('--leak-de', type=int, default=LEAK_DE_DEFAULT,
                    help='leak suppression: exponent steps (6 dB) below the strongest channel; 0 disables')
    ap.add_argument('-o', '--out', required=True)
    ap.add_argument('--words', help='also save the words (np.save)')
    args = ap.parse_args()
    raw = np.memmap(args.iq, dtype=np.int8, mode='r')
    n = int(args.seconds * 16e6) // 6400 * 6400
    s0 = int(args.start * 16e6) // 6400 * 6400
    mdl = Model(args.e_th, args.leak_de)
    words = []
    for s in range(s0, s0 + n, 64000):
        x = np.asarray(raw[2 * s:2 * (s + 64000)], np.int64).reshape(-1, 2)
        words += mdl.process(x)
    if args.words:
        np.save(args.words, np.array(words, np.uint16))
    words_to_dump(words, args.fc, args.out)
    print('%d words' % len(words), file=sys.stderr)


if __name__ == '__main__':
    main()
