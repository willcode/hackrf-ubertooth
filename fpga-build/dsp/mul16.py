#
# SPDX-License-Identifier: BSD-3-Clause

"""Signed 16 x 16 multiplier on one SB_MAC16, fully pipelined.

`o` holds a * b two cycles after `a` and `b` are presented: the input
registers and the 16 x 16 product register are both used, so the DSP runs
well above its 50 MHz unregistered limit.
"""

from amaranth               import Module, ClockSignal
from amaranth.lib           import wiring
from amaranth.lib.wiring    import In, Out

from dsp.sb_mac16           import SB_MAC16

LATENCY = 2


class Mul16(wiring.Component):
    a: In(16)   # signed
    b: In(16)   # signed
    o: Out(32)  # signed product

    def elaborate(self, platform):
        m = Module()
        m.submodules.mac = mac = SB_MAC16(
            A_REG=1,
            B_REG=1,
            PIPELINE_16x16_MULT_REG2=1,
            TOPOUTPUT_SELECT=3,
            BOTOUTPUT_SELECT=3,
            A_SIGNED=1,
            B_SIGNED=1,
        )
        m.d.comb += [
            mac.CLK.eq(ClockSignal("sync")),
            mac.CE.eq(1),
            mac.A.eq(self.a),
            mac.B.eq(self.b),
            self.o.eq(mac.O),
        ]
        return m
