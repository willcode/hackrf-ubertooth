#
# SPDX-License-Identifier: BSD-3-Clause

"""One iCE40UP 16K x 16 single-port RAM (SB_SPRAM256KA), with a simulation model.

Read data appears on `dout` the cycle after `addr` is presented with `we` low.
A write stores `din` at `addr` on the clock edge with `we` high.
"""

from amaranth               import Module, Instance, ClockSignal, Const
from amaranth.lib           import wiring
from amaranth.lib.memory    import Memory
from amaranth.lib.wiring    import In, Out
from amaranth.vendor        import SiliconBluePlatform


class SPRAM(wiring.Component):
    addr: In(14)
    din:  In(16)
    we:   In(1)
    dout: Out(16)

    def __init__(self, sim_depth=16384):
        super().__init__()
        self._sim_depth = sim_depth
        self.mem = Memory(shape=16, depth=sim_depth, init=[])   # simulation model only

    def elaborate(self, platform):
        m = Module()
        if isinstance(platform, SiliconBluePlatform):
            m.submodules.spram = Instance("SB_SPRAM256KA",
                i_ADDRESS    = self.addr,
                i_DATAIN     = self.din,
                i_MASKWREN   = Const(0b1111, 4),
                i_WREN       = self.we,
                i_CHIPSELECT = Const(1),
                i_CLOCK      = ClockSignal("sync"),
                i_STANDBY    = Const(0),
                i_SLEEP      = Const(0),
                i_POWEROFF   = Const(1),
                o_DATAOUT    = self.dout,
            )
        else:
            m.submodules.mem = mem = self.mem
            rd = mem.read_port(domain="sync", transparent_for=())
            wr = mem.write_port(domain="sync")
            m.d.comb += [
                rd.addr.eq(self.addr),
                rd.en.eq(~self.we),
                self.dout.eq(rd.data),
                wr.addr.eq(self.addr),
                wr.data.eq(self.din),
                wr.en.eq(self.we),
            ]
        return m
