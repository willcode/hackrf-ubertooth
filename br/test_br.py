#
# SPDX-License-Identifier: BSD-3-Clause

"""Checks of the BR host models on synthetic signals: the access code generator, the bit-exact
model (br_fixed) and the floating-point model (br_channelizer) on every channel, leak
suppression, and the ubertooth-dump records."""

import os
import struct
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import br_channelizer  # noqa: E402
import br_fixed  # noqa: E402
import synth  # noqa: E402

# every channel of the filter bank (bin 8, fc +- 8 MHz, is not a channel)
OFFSETS = list(range(-7, 8))
LAPS = [0x9e8b33, 0xc6a2d7, 0x5a1c39, 0x123456, 0x3b0f4e, 0xa4e1d2, 0x6d2b90, 0x1f77c3]


def channel_bits(words, k):
    return (np.asarray(words, np.int64) >> (k % 16)) & 1


def best_match(bits, lap):
    """(matching bits, offset) of the best alignment of lap's 72-bit access code in bits."""
    ac = np.array(synth.access_code(lap))
    return max((int((bits[j:j + len(ac)] == ac).sum()), j) for j in range(len(bits) - len(ac)))


def staggered(level, spacing=1600):
    """One burst per channel, one after another (so leak suppression never acts), each starting
    on a symbol boundary."""
    return [(k, LAPS[i % len(LAPS)], level, 192 + spacing * i) for i, k in enumerate(OFFSETS)]


class TestAccessCode(unittest.TestCase):
    def test_sync_words(self):
        # cross-checked with libbtbb's btbb_gen_syncword()
        self.assertEqual(synth.sync_word(0x9e8b33), 0x4e7a2cce331a3ae2)    # GIAC
        self.assertEqual(synth.sync_word(0x000000), 0xb0000002c7820e7e)
        self.assertEqual(synth.sync_word(0xffffff), 0x4ffffffe44ad1ae7)

    def test_codewords(self):
        rng = np.random.default_rng(1)
        for lap in [int(v) for v in rng.integers(0, 1 << 24, 200)] + LAPS:
            s = synth.sync_word(lap)
            # a codeword of the (64,30) code after removing the PN, carrying the LAP and the
            # Barker sequence chosen by its MSB
            self.assertEqual(synth.poly_mod(s ^ synth.PN, synth.G), 0, hex(lap))
            self.assertEqual((s >> 34) & 0xffffff, lap)
            self.assertEqual(s >> 58, 0b010011 if lap >> 23 else 0b101100)
            ac = synth.access_code(lap)
            self.assertEqual(len(ac), 72)
            # the preamble alternates into s0, the trailer out of s63
            self.assertEqual(ac[0:5], [ac[4], ac[4] ^ 1, ac[4], ac[4] ^ 1, ac[4]])
            self.assertEqual(ac[67:72], [ac[67], ac[67] ^ 1, ac[67], ac[67] ^ 1, ac[67]])


class TestFixedModel(unittest.TestCase):
    def check_all_channels(self, level, noise=-50.0):
        # Each burst starts cold (threshold and sampling-phase averages
        # converge during the preamble and the first sync word bits), and a
        # change of sampling phase inside a packet can slip a bit: a few
        # sync word errors are normal, which the access code search allows.
        pk = staggered(level)
        n = (pk[-1][3] + 2000) // 6400 * 6400 + 6400
        x = synth.synthesize(n, pk, noise_db=noise).astype(np.int64)
        w = br_fixed.Model().process(x)
        self.assertEqual(len(w), n // 16)
        sync_errors = []
        for k, lap, _, start in pk:
            bits = channel_bits(w, k)
            ac = np.array(synth.access_code(lap))
            m, j = best_match(bits, lap)
            # 4 guard symbols before the access code, 6 of filter and detector delay
            self.assertEqual(j - start // 16, 10, "offset %d MHz" % k)
            sync_errors.append(int((bits[j + 4:j + 68] != ac[4:68]).sum()))
        self.assertLessEqual(max(sync_errors), 4, sync_errors)
        self.assertGreaterEqual(sum(e <= 1 for e in sync_errors), 10, sync_errors)

    def test_every_channel_strong(self):
        self.check_all_channels(-6)

    def test_every_channel_weak(self):
        self.check_all_channels(-40)

    def test_bank_marks(self):
        w = np.array(br_fixed.Model().process(np.zeros((6400 * 3, 2), np.int64)))
        self.assertEqual(list(np.flatnonzero(w & 0x100)), [0, 400, 800])

    def test_leak_suppression(self):
        # a strong burst on +2 MHz, a weak one on -5 MHz (7 channels away) at the same time
        lap_s, lap_w = 0x9e8b33, 0xc6a2d7
        x = synth.synthesize(12800, [(2, lap_s, -6, 3000), (-5, lap_w, -36, 3000)]).astype(np.int64)
        burst = slice(3000 // 16 + 12, 3000 // 16 + 60)
        on = np.array(br_fixed.Model().process(x))
        off = np.array(br_fixed.Model(leak_de=0).process(x))
        for w in (on, off):
            self.assertEqual(best_match(channel_bits(w, 2), lap_s)[0], 72)
            self.assertEqual(best_match(channel_bits(w, -5), lap_w)[0], 72)
        alt = (np.arange(len(on)) & 1)[burst]
        for k in (-2, -1, 0, 1, 3, 4, 5, 6, -4):      # within 4 of +2 MHz, and bins 4 and 12
            self.assertTrue((channel_bits(on, k)[burst] == alt).all(), "offset %d MHz" % k)
            self.assertFalse((channel_bits(off, k)[burst] == alt).all(), "offset %d MHz" % k)
        for k in (7, -7, -6, -3):                    # not near, and quiet: noise decisions
            self.assertFalse((channel_bits(on, k)[burst] == alt).all(), "offset %d MHz" % k)


class TestFloatModel(unittest.TestCase):
    def test_every_channel(self):
        # this model sets its threshold and sampling phase once per 400-symbol bank: one burst
        # per bank
        pk = staggered(-20, spacing=2 * br_channelizer.BANK * 8)
        n_bank = 2 * br_channelizer.BANK * 8
        n = (pk[-1][3] + 2000) // n_bank * n_bank + n_bank
        x = synth.synthesize(n, pk).astype(np.float32)
        ch = br_channelizer.Channelizer(16, 4, 16e6, 0.6e6)
        Y = np.concatenate([ch.process((x[s:s + n_bank, 0] + 1j * x[s:s + n_bank, 1]).astype(np.complex64))
                            for s in range(0, n, n_bank)])
        good = []
        for k, lap, _, _ in pk:
            bits = np.concatenate([br_channelizer.demod_bank(Y[s:s + 2 * br_channelizer.BANK, k % 16])[0]
                                   for s in range(0, len(Y), 2 * br_channelizer.BANK)])
            good.append(best_match(bits, lap)[0] >= 70)
        # the prototype decides once per bank; a burst near a bank's
        # threshold estimate can come out poorly
        self.assertGreaterEqual(sum(good), 13, good)


class TestDump(unittest.TestCase):
    def test_records(self):
        pk = staggered(-12)
        n = 6400 * 4
        w = br_fixed.Model().process(synth.synthesize(n, pk[:8]).astype(np.int64))
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "t.dump")
            br_fixed.words_to_dump(w, 2405, path)        # channels 0..10: offsets -3..7 MHz
            raw = open(path, "rb").read()
        rec = 4 + 64
        self.assertEqual(len(raw), 4 * 11 * rec)
        got = {}
        for r in range(len(raw) // rec):
            b = raw[r * rec + 4:(r + 1) * rec]
            ch, clk100ns = b[2], struct.unpack("<I", b[4:8])[0]
            bits = np.unpackbits(np.frombuffer(b[14:64], np.uint8))
            got.setdefault(ch, []).append((clk100ns, bits))
        self.assertEqual(sorted(got), list(range(11)))
        for ch, banks in got.items():
            self.assertEqual([c for c, _ in banks], [0, 4000, 8000, 12000])
            k = ch + 2402 - 2405
            self.assertTrue((np.concatenate([b for _, b in banks]) == channel_bits(w, k)).all())


if __name__ == "__main__":
    unittest.main()
