#!/usr/bin/env python3
#
# SPDX-License-Identifier: BSD-3-Clause

"""
br_channelizer - Bluetooth BR multi-channel receiver model for the HackRF Pro.

Turns a wideband int8 IQ capture (hackrf_transfer -r) into the stream an
Ubertooth delivers: one 64-byte usb_pkt_rx per channel per 400 symbols,
written as an ubertooth-dump file, so the unmodified host tools
(ubertooth-rx -i, libbtbb) process M channels at once.

    hackrf_transfer -r br.iq -f 2441000000 -s 16000000 ...
    br_channelizer.py br.iq --fc 2441 -o br.dump
    ubertooth-rx -i br.dump

Signal path (the gateware's, in floating point or in its fixed point):

1. polyphase filter bank, M branches, decimation D = M/2: channel k at
   fc + k*fs/M, 2 samples per symbol at fs = M MHz
2. differential phase detector over one symbol (h = 0.32: +-0.32 pi)
3. per 400-symbol buffer: decision threshold = mean phase step over the
   samples with signal (tracks the transmitter's carrier offset), and the
   better of the two sampling phases

"""
import argparse
import struct
import sys

import numpy as np

BANK = 400          # symbols per usb_pkt_rx
SYM_BYTES = 50


def prototype(M, T, cutoff_hz, fs, beta=6.0):
    L = M * T
    n = np.arange(L) - (L - 1) / 2
    fc = cutoff_hz / fs
    h = 2 * fc * np.sinc(2 * fc * n) * np.kaiser(L, beta)
    return h / h.sum()


class Channelizer:
    """Oversampled (D = M/2) polyphase analysis filter bank, streaming."""

    def __init__(self, M, T, fs, cutoff_hz):
        self.M, self.T, self.D = M, T, M // 2
        self.h = prototype(M, T, cutoff_hz, fs)
        self.hist = np.zeros(M * T - 1, np.complex64)
        self.m = 0                                  # output frame counter

    def process(self, x):
        """x: complex samples, a multiple of D long. Returns (frames, M)."""
        M, T, D = self.M, self.T, self.D
        buf = np.concatenate([self.hist, x])
        L = M * T
        nfr = len(x) // D
        # frame m uses buf[mD + D - 1 + L - 1 - (L-1) ... ]: the L samples
        # ending at input index (frame end); reverse so tap 0 is newest
        idx = (np.arange(nfr) * D + D - 1 + L - 1)[:, None] - np.arange(L)[None, :]
        seg = buf[idx]                              # (nfr, L), newest first
        u = (seg * self.h[None, :]).reshape(nfr, T, M).sum(axis=1)   # u_p
        Y = np.fft.ifft(u, axis=1) * M
        k = np.arange(M)
        sign = np.where(((self.m + np.arange(nfr))[:, None] * k[None, :]) % 2 == 1, -1, 1)
        self.hist = buf[-(L - 1):]
        self.m += nfr
        return (Y * sign).astype(np.complex64)


def demod_bank(z):
    """z: 2*BANK complex samples (2 per symbol). Returns BANK bits."""
    d = np.angle(z[2:] * np.conj(z[:-2]))
    d = np.concatenate([np.zeros(2), d])
    p = np.abs(z) ** 2
    floor = np.median(p)
    sig = p > 6 * floor
    thr = d[sig].mean() if sig.sum() > 40 else 0.0
    d = d - thr
    best = max((0, 1), key=lambda ph: np.mean(np.abs(d[ph::2])))
    return (d[best::2] > 0).astype(np.uint8)[:BANK], p


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('iq')
    ap.add_argument('--fc', type=float, required=True, help='center MHz')
    ap.add_argument('--fs', type=float, default=16e6)
    ap.add_argument('-M', type=int, default=16, help='filter bank branches (fs / 1 MHz)')
    ap.add_argument('-T', type=int, default=4, help='taps per branch')
    ap.add_argument('--cutoff', type=float, default=0.6e6)
    ap.add_argument('--edge', type=int, default=1,
                    help='channels to drop at each band edge')
    ap.add_argument('-o', '--out', required=True)
    ap.add_argument('--seconds', type=float, default=0)
    args = ap.parse_args()

    M = args.M
    assert abs(args.fs / M - 1e6) < 1, 'fs / M must be 1 MHz'
    ch = Channelizer(M, args.T, args.fs, args.cutoff)
    ks = [k for k in range(-M // 2 + args.edge, M // 2 - args.edge + 1) if k != M // 2]
    chans = {k: int(round(args.fc + k)) - 2402 for k in ks}
    chans = {k: c for k, c in chans.items() if 0 <= c <= 78}

    raw = np.memmap(args.iq, dtype=np.int8, mode='r')
    n_total = len(raw) // 2
    if args.seconds:
        n_total = min(n_total, int(args.seconds * args.fs))
    per_bank = 2 * BANK * ch.D                     # input samples per 400 symbols
    out = open(args.out, 'wb')
    systime = 0
    n_banks = n_total // per_bank
    for b in range(n_banks):
        x = raw[2 * b * per_bank:2 * (b + 1) * per_bank].astype(np.float32)
        z = (x[0::2] + 1j * x[1::2]).astype(np.complex64)
        Y = ch.process(z)                          # (2*BANK, M)
        t_ns = b * BANK * 1000
        clk100ns = (t_ns // 100) & 0xffffffff
        clkn = int(t_ns / 312500)
        for k, c in chans.items():
            bits, p = demod_bank(Y[:, k % M])
            data = np.packbits(bits)               # MSB first, as the Ubertooth
            pdb = 10 * np.log10(p + 1e-3)
            rmax, rmin, ravg = (int(np.clip(v - 90, -127, 0)) for v in
                                (pdb.max(), np.median(pdb), pdb.mean()))
            pkt = struct.pack('<BBBBIbbbB2x', 0, 0, c, (clkn >> 20) & 0xff, clk100ns,
                              rmax, rmin, ravg, 1) + data.tobytes()
            out.write(struct.pack('>I', systime + b * BANK // 1000000) + pkt)
        if b % 2500 == 0:
            print('%.1f s' % (b * BANK / 1e6), file=sys.stderr)
    out.close()
    print('%d banks x %d channels (%s)' % (n_banks, len(chans), sorted(chans.values())),
          file=sys.stderr)


if __name__ == '__main__':
    main()
