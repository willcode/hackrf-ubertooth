#
# This file is part of HackRF.
#
# Copyright (c) 2025 Great Scott Gadgets <info@greatscottgadgets.com>
# SPDX-License-Identifier: BSD-3-Clause

from amaranth               import Module, Instance, ClockDomain

from board                  import ClockDomainGenerator


class ClockDomainGeneratorGB(ClockDomainGenerator):
    """The board's clock domains, with the DAC clock on an explicit global buffer.

    nextpnr gives its eight global buffers to the highest-fanout nets. The DAC clock has
    a fanout of about 20, so in a design with many enables it can lose its buffer.
    """

    def elaborate(self, platform):
        m = Module()
        m.domains.adclk = cd_adclk = ClockDomain(name="adclk", reset_less=True)
        m.domains.daclk = cd_daclk = ClockDomain(name="daclk", reset_less=True)

        adclk_ref = platform.request("afe_clk").i
        fpgaclk_ref = platform.request("fpga_clk").i

        delayed_adclk = self.lut_delay(m, adclk_ref, depth=1, bel="X12/Y30")
        m.d.comb += cd_adclk.clk.eq(delayed_adclk)
        platform.add_clock_constraint(delayed_adclk, 40e6)

        m.submodules.daclk_gb = Instance("SB_GB",
            i_USER_SIGNAL_TO_GLOBAL_BUFFER=fpgaclk_ref,
            o_GLOBAL_BUFFER_OUTPUT=cd_daclk.clk,
        )
        platform.add_clock_constraint(cd_daclk.clk, 40e6)
        return m
