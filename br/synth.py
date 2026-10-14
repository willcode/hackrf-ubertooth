#!/usr/bin/env python3
#
# SPDX-License-Identifier: BSD-3-Clause

"""
synth - synthetic Bluetooth BR signals for testing the channelizer.

Writes int8 I/Q at 16 Msps, as hackrf_transfer -r would record it at a
center frequency fc: bursts carrying the access code of a LAP ( GFSK,
BT 0.5, modulation index 0.32, 1 Msym/s) on channels fc + k MHz at chosen
levels, optional unmodulated carriers, and Gaussian noise.

    synth.py --fc 2441 --packet 3:0x9e8b33:-20 --packet -5:0x123456:-40 -o test.iq
    br_fixed.py test.iq --fc 2441 --seconds 0.004 -o test.dump
    ubertooth-rx -i test.dump

Levels are dB relative to the int8 full scale; the noise is -50 dB per
I/Q component by default. The access code follows the Core specification
(Vol 2 Part B 6.3.3); access_code(0x9e8b33) is the inquiry (GIAC) code.
"""
import argparse

import numpy as np

FS = 16e6
SPS = 16                # samples per symbol
H_MOD = 0.32
BT = 0.5

# PN sequence p0..p63 (bit i = p_i) and the generator polynomial of the
# (64,30) expurgated block code, octal 260534236651 (bit i = coefficient of D^i)
PN = 0x83848D96BBCC54FC
G = 0o260534236651


def poly_mod(a, g):
    dg = g.bit_length() - 1
    while a.bit_length() - 1 >= dg:
        a ^= g << (a.bit_length() - 1 - dg)
    return a


def sync_word(lap):
    """The 64-bit sync word of a LAP, bit i = s_i (s_0 is sent first)."""
    lap &= 0xffffff
    barker = 0b101100 if lap & 0x800000 == 0 else 0b010011   # x24..x29 = 001101 / 110010
    x = lap | barker << 24
    xt = x ^ (PN >> 34)
    parity = poly_mod(xt << 34, G)
    return (parity | xt << 34) ^ PN


def access_code(lap, trailer=True):
    """Air bits of the access code: preamble, sync word and, as packets with a
    header send it (not ID packets), the trailer: 72 bits, or 68."""
    s = sync_word(lap)
    sbits = [(s >> i) & 1 for i in range(64)]
    pre = [1, 0, 1, 0] if sbits[0] else [0, 1, 0, 1]
    post = ([0, 1, 0, 1] if sbits[63] else [1, 0, 1, 0]) if trailer else []
    return pre + sbits + post


def gfsk(bits, sps=SPS, h=H_MOD, bt=BT):
    """Complex baseband GFSK of the bits, unit amplitude, sps samples per symbol."""
    nrz = np.repeat(2 * np.asarray(bits, np.float64) - 1, sps)
    t = (np.arange(-2 * sps, 2 * sps + 1)) / sps
    sigma = np.sqrt(np.log(2)) / (2 * np.pi * bt)
    g = np.exp(-t ** 2 / (2 * sigma ** 2))
    g /= g.sum()
    f = np.convolve(nrz, g, mode='same')
    phase = np.pi * h * np.cumsum(f) / sps
    return np.exp(1j * phase)


def synthesize(n, packets=(), carriers=(), noise_db=-50.0, seed=1):
    """n samples. packets: (offset_mhz, lap, level_db, start_sample); carriers:
    (offset_hz, level_db). Returns (n, 2) int8."""
    rng = np.random.default_rng(seed)
    t = np.arange(n) / FS
    z = np.zeros(n, np.complex128)
    for k, lap, level, start in packets:
        bits = [0, 1] * 4 + access_code(lap) + [0, 1] * 4   # a little guard around the burst
        s = gfsk(bits)
        s = s[:max(0, min(len(s), n - start))]
        z[start:start + len(s)] += 10 ** (level / 20) * s * np.exp(2j * np.pi * k * 1e6 * t[start:start + len(s)])
    for f, level in carriers:
        z += 10 ** (level / 20) * np.exp(2j * np.pi * f * t)
    z *= 127
    z += 127 * 10 ** (noise_db / 20) * (rng.standard_normal(n) + 1j * rng.standard_normal(n))
    out = np.stack([np.round(z.real), np.round(z.imag)], 1)
    return np.clip(out, -127, 127).astype(np.int8)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--fc', type=float, default=2441, help='center frequency, MHz (for the message only)')
    ap.add_argument('--samples', type=int, default=64000)
    ap.add_argument('--packet', action='append', default=[],
                    help='OFFSET_MHZ:LAP:LEVEL_DB[:START_SAMPLE], repeatable')
    ap.add_argument('--carrier', action='append', default=[],
                    help='OFFSET_HZ:LEVEL_DB, repeatable')
    ap.add_argument('--noise', type=float, default=-50.0, help='noise level, dB')
    ap.add_argument('--seed', type=int, default=1)
    ap.add_argument('-o', '--out', required=True)
    args = ap.parse_args()
    pk = []
    for i, p in enumerate(args.packet):
        f = p.split(':')
        start = int(f[3]) if len(f) > 3 else 1000 + 2000 * i
        pk.append((int(f[0]), int(f[1], 0), float(f[2]), start))
    cw = [(float(c.split(':')[0]), float(c.split(':')[1])) for c in args.carrier]
    synthesize(args.samples, pk, cw, args.noise, args.seed).tofile(args.out)
    for k, lap, level, start in pk:
        print('LAP %06x on %d MHz (channel %d) at %.0f dB, sample %d'
              % (lap, args.fc + k, args.fc + k - 2402, level, start))


if __name__ == '__main__':
    main()
