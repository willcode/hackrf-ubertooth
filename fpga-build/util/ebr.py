#
# SPDX-License-Identifier: BSD-3-Clause

"""A simple dual-port RAM of up to 256 words as iCE40 block RAMs (SB_RAM40_4K, 256 x 16 each), with a
simulation model.

Read data appears on `rdata` the cycle after `raddr` is presented with `re` high, and holds while `re`
is low. A write stores `wdata` at `waddr` on the clock edge with `we` high.

The primitives are instantiated directly: an Amaranth memory's read port promises a defined result
when a read and a write meet at one address, which the block RAM does not, so yosys adds registers
and bypass multiplexers to emulate it (hundreds of cells on a wide memory). The user of this RAM
guarantees no such collision, and the simulation model returns the old word if one happens.
"""

from amaranth               import Module, Instance, ClockSignal, Const, Signal, Cat
from amaranth.lib           import wiring
from amaranth.lib.memory    import Memory
from amaranth.lib.wiring    import In, Out
from amaranth.vendor        import SiliconBluePlatform


class EBR(wiring.Component):
    def __init__(self, width, depth):
        assert depth <= 256
        self._width, self._depth = width, depth
        abits = max(1, (depth - 1).bit_length())
        super().__init__({
            "raddr": In(abits), "re": In(1), "rdata": Out(width),
            "waddr": In(abits), "we": In(1), "wdata": In(width),
        })
        self.mem = Memory(shape=width, depth=depth, init=[])   # simulation model only

    def elaborate(self, platform):
        m = Module()
        if isinstance(platform, SiliconBluePlatform):
            for i in range(0, self._width, 16):
                n = min(16, self._width - i)
                rd = Signal(16, name=f"rd{i // 16}")
                m.submodules[f"ram{i // 16}"] = Instance("SB_RAM40_4K",
                    p_READ_MODE  = 0,
                    p_WRITE_MODE = 0,
                    i_RCLK       = ClockSignal("sync"),
                    i_RCLKE      = Const(1),
                    i_RE         = self.re,
                    i_RADDR      = Cat(self.raddr, Const(0, 11 - len(self.raddr))),
                    i_WCLK       = ClockSignal("sync"),
                    i_WCLKE      = Const(1),
                    i_WE         = self.we,
                    i_WADDR      = Cat(self.waddr, Const(0, 11 - len(self.waddr))),
                    i_MASK       = Const(0, 16),
                    i_WDATA      = Cat(self.wdata[i:i + n], Const(0, 16 - n)),
                    o_RDATA      = rd,
                )
                m.d.comb += self.rdata[i:i + n].eq(rd[:n])
        else:
            m.submodules.mem = mem = self.mem
            rd = mem.read_port(domain="sync", transparent_for=())
            wr = mem.write_port(domain="sync")
            m.d.comb += [
                rd.addr.eq(self.raddr),
                rd.en.eq(self.re),
                self.rdata.eq(rd.data),
                wr.addr.eq(self.waddr),
                wr.data.eq(self.wdata),
                wr.en.eq(self.we),
            ]
        return m
