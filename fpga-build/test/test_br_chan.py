#
# SPDX-License-Identifier: BSD-3-Clause

"""The BR channelizer gateware against its bit-exact model (br/br_fixed.py) on synthetic signals:
the output words with one input sample every 3 cycles and with 2 to 4 cycles (a clock domain
crossing), and the debug snapshot's stages (filter bank, FFT stages) checked by br/check_snap.py."""

import os
import sys
import unittest

import numpy as np
from amaranth.sim import Simulator

from dsp.br_chan import BRChannelizer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "br"))
import br_fixed  # noqa: E402
import check_snap  # noqa: E402
import synth  # noqa: E402


def signal(n):
    """Strong and weak bursts on several channels, a carrier and noise."""
    packets = [(3, 0x9e8b33, -6, 0), (-5, 0xc6a2d7, -30, 300), (6, 0x5a1c39, -20, 900),
               (0, 0x123456, -12, 1700), (-7, 0x3b0f4e, -24, 2200), (1, 0x9e8b33, -9, 2600)]
    return synth.synthesize(n, packets, carriers=[(-2.3e6, -26)], seed=3).astype(np.int64)


def run(dut, x, cycles, on_tick, settle=400):
    """Feed x (one sample per cycles() clocks) and call on_tick(ctx) after every clock; on_tick
    returns True to stop."""
    async def bench(ctx):
        ctx.set(dut.e_th, br_fixed.E_TH_DEFAULT)
        ctx.set(dut.leak_de, br_fixed.LEAK_DE_DEFAULT)
        if hasattr(dut, "dbg_on"):
            ctx.set(dut.dbg_on, 1)
        for s in range(len(x) + settle):
            for c in range(cycles()):
                if c == 0 and s < len(x):
                    ctx.set(dut.input.valid, 1)
                    ctx.set(dut.input.p, {"i": int(x[s, 0]), "q": int(x[s, 1])})
                else:
                    ctx.set(dut.input.valid, 0)
                await ctx.tick()
                if on_tick(ctx):
                    return

    sim = Simulator(dut)
    sim.add_clock(1 / 48e6)
    sim.add_testbench(bench)
    sim.run()


class TestWords(unittest.TestCase):
    N = 3200

    def words(self, cycles):
        x = signal(self.N)
        want = br_fixed.Model().process(x)
        dut = BRChannelizer()
        got = []

        def on_tick(ctx):
            if ctx.get(dut.out_valid):
                got.append(ctx.get(dut.out_data))

        run(dut, x, cycles, on_tick)
        return got, want

    def check(self, got, want):
        self.assertEqual(len(got), len(want))
        bad = [i for i in range(len(want)) if got[i] != want[i]]
        self.assertEqual(len(bad), 0, "%d words differ, the first at word %d" % (len(bad), bad[0]) if bad else "")
        # the comparison means something: the words carry signal on several channels
        w = np.array(want)
        self.assertGreater(len({k for k in range(16) if k != 8 and np.ptp((w >> k) & 1)}), 6)

    def test_words_every_3_cycles(self):
        self.check(*self.words(lambda: 3))

    def test_words_jittered_2_to_4_cycles(self):
        rng = np.random.default_rng(1)
        self.check(*self.words(lambda: int(rng.integers(2, 5))))


class TestSnapshot(unittest.TestCase):
    def test_snapshot_stages_match_the_model(self):
        x = signal(3200)
        dut = BRChannelizer(debug=True)
        out = []

        def on_tick(ctx):
            if ctx.get(dut.dbg_valid):
                out.append(ctx.get(dut.dbg_data))
            return len(out) >= 900

        run(dut, x, lambda: 3, on_tick)
        frames = {}
        tot = check_snap.check(np.array(out, np.int64), verbose=False, per_frame=frames)
        self.assertEqual(sorted(tot), ["bank", "fft", "stage1", "twiddle"])
        self.assertEqual(tot["bank"], [0])
        for stage, (bad,) in frames.items():
            # every frame whose input frame is in the snapshot matches exactly
            self.assertGreaterEqual(len(bad), 3, stage)
            self.assertEqual(bad[1:], [0] * (len(bad) - 1), stage)


if __name__ == "__main__":
    unittest.main()
