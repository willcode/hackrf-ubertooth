#
# This file is part of HackRF.
#
# Copyright (c) 2025 Great Scott Gadgets <info@greatscottgadgets.com>
# SPDX-License-Identifier: BSD-3-Clause

import os
from amaranth               import Elaboratable, Module, Signal, Mux, DomainRenamer
from amaranth.lib           import cdc
from amaranth.lib.wiring    import connect

from amaranth_future        import fixed

from board                  import PralinePlatform
from interface              import MAX586xInterface, SGPIOInterface, SPIRegisterInterface
from dsp.fir                import HalfBandDecimator, HalfBandInterpolator
from dsp.cic                import CICInterpolator
from dsp.dc_block           import DCBlock
from dsp.quarter_shift      import QuarterShift
from dsp.nco                import NCO
from util                   import ClockConverter, IQSample, StreamSkidBuffer
from util.clocks            import ClockDomainGeneratorGB
from util.identity          import add_identity, CAP_RX, CAP_STANDARD_MAP
from dsp.br_chan            import BRChannelizer

KIND_BR_RX = 0x4C


class Top(Elaboratable):
    """Receive-only standard image with a Bluetooth BR channelizer (dsp/br_chan.py).

    The decimation chain keeps only the last half-band stage, so the image
    supports sample rates above 10 Msps (decimation by 2; at 10 Msps and below
    the firmware decimates by 4 or more).

    Register 0x06 selects the output. At 0, its value after every load, the image streams
    I/Q exactly as the receive-only standard image does. With bit 7 set (and the sample
    rate at 16 Msps) it streams one 16-bit word of BR symbol decisions per symbol, one bit
    per 1 MHz channel; bits 0-3 set the exponent threshold for "signal present". The
    firmware writes 0x06 only for the transmit NCO and in its SPI self-test, which
    restores it.

    The platform resources, their pins and their directions are those of the
    standard image, so no pin drives a net that the standard image leaves as
    an input.
    """


    def elaborate(self, platform):
        m = Module()

        m.submodules.clkgen = ClockDomainGeneratorGB()
        adc_clk = "adclk"
        dac_clk = "daclk"

        # Submodules.
        # The ADC data pins include 39 to 41, which are also the outputs of the
        # RGB current-sink drivers. They are plain inputs here; no SB_RGBA_DRV,
        # SB_RGB_DRV or SB_LEDDA_IP is instantiated.
        m.submodules.adcdac_intf = adcdac_intf = MAX586xInterface(adc_domain=adc_clk, dac_domain=dac_clk)
        # host_data is shared with the MCU's SGPIO pins. The interface enables
        # its drivers only while the MCU's direction input is low, and releases
        # them when it is high. Its DAC stream stays unconnected, so with the
        # direction high it accepts no samples and drives no clock pulses.
        m.submodules.mcu_intf    = mcu_intf    = SGPIOInterface(sample_width=16, domain="sync")

        m.d.comb += adcdac_intf.q_invert.eq(platform.request("q_invert").i)

        # Half-band filter taps.
        taps = [-2, 0, 7, 0, -18, 0, 41, 0, -92, 0, 320, 512, 320, 0, -92, 0, 41, 0, -18, 0, 7, 0, -2]
        taps = [ tap/1024 for tap in taps ]

        taps2 = [3, 0, -16, 0, 77, 128, 77, 0, -16, 0, 3]
        taps2 = [ tap/256 for tap in taps2 ]

        taps3 = [-9, 0, 73, 128, 73, 0, -9]
        taps3 = [ tap/256 for tap in taps3 ]

        taps4 = [-8, 0, 72, 128, 72, 0, -8]
        taps4 = [ tap/256 for tap in taps4 ]

        taps5 = [-1, 0, 9, 16, 9, 0, -1]
        taps5 = [ tap/32 for tap in taps5 ]

        common_rx_filter_opts = dict(
            data_shape=fixed.SQ(1,7),
            shape_out=fixed.SQ(1,7),
            always_ready=True,
            domain=adc_clk,
        )

        rx_chain = {
            # DC block and quarter shift.
            "dc_block":      DCBlock(width=8, num_channels=2, domain=adc_clk),
            "quarter_shift": DomainRenamer(adc_clk)(QuarterShift()),

            # Half-band decimation: only the last stage. At 16 Msps the firmware
            # runs the ADC at 32 MHz and decimates by 2; in plain I/Q mode the
            # image therefore needs sample rates above 10 Msps.
            "hbfir1":        HalfBandDecimator(taps, **common_rx_filter_opts),

            # Clock domain conversion.
            "clkconv":       ClockConverter(IQSample(8), 8, adc_clk, "sync"),
        }
        for k,v in rx_chain.items():
            m.submodules[f"rx_{k}"] = v

        # Connect receiver chain.
        last = adcdac_intf.adc_stream
        for block in rx_chain.values():
            connect(m, last, block.input)
            last = block.output

        # Bluetooth BR channelizer: 16 channels of 1 MHz at 16 Msps, one
        # 16-bit word of decisions per symbol (1 M words/s) instead of I/Q.
        # BR_DEBUG=1 at build time adds the debug snapshots (gateware/check_snap.py)
        m.submodules.br = br = BRChannelizer(debug=os.environ.get("BR_DEBUG") == "1")
        br_on = Signal()
        br_eth = Signal(4)
        br_de = Signal(3)
        dbg_on = Signal()
        m.d.comb += [
            br.input.valid.eq(last.valid & (br_on | dbg_on)),
            br.dbg_on.eq(dbg_on),
            br.input.p.eq(last.p),
            br.e_th.eq(br_eth),
            br.leak_de.eq(br_de),
        ]
        with m.If(dbg_on):
            m.d.comb += [
                mcu_intf.adc_stream.valid.eq(br.dbg_valid),
                mcu_intf.adc_stream.p.eq(br.dbg_data),
            ]
        with m.Elif(br_on & ~dbg_on):
            m.d.comb += [
                mcu_intf.adc_stream.valid.eq(br.out_valid),
                mcu_intf.adc_stream.p.eq(br.out_data),
            ]
        with m.Else():
            m.d.comb += [
                mcu_intf.adc_stream.valid.eq(last.valid),
                mcu_intf.adc_stream.p.eq(last.p),
            ]

        # The DAC stays at mid-scale so the transmit path carries no signal.
        # With no valid sample the interface outputs 0x200 on both channels,
        # whatever the Q inversion input.
        m.d.comb += adcdac_intf.dac_stream.valid.eq(0)

        # SPI register interface.
        # The register port uses one SB_SPI core on the board's SPI pins 14 to
        # 17, as the standard image does. No other hard SPI or I2C core is
        # instantiated.
        spi_port = platform.request("spi")
        m.submodules.spi_regs = spi_regs = SPIRegisterInterface(spi_port, registered_read=True)

        # Add control registers.
        # The addresses and widths match the standard image, so each firmware
        # write means the same thing. The TX registers at 0x04 to 0x06 only
        # store their value and drive nothing. Register 0x06 stays readable
        # because the firmware's SPI self-test writes it and reads it back.
        ctrl     = spi_regs.add_register(0x01, init=0)
        rx_decim = spi_regs.add_register(0x02, init=0, size=3)
        rx_pstep = spi_regs.add_register(0x03, init=0)
        tx_ctrl  = spi_regs.add_register(0x04, init=0, size=1)
        tx_intrp = spi_regs.add_register(0x05, init=0, size=3)
        tx_pstep = spi_regs.add_register(0x06, init=0)

        # Identity block at 0x78 to 0x7F: an RX-only image that keeps the
        # standard register map.
        add_identity(spi_regs, KIND_BR_RX, CAP_RX | CAP_STANDARD_MAP)

        # Register 0x06: 0 for the plain I/Q stream; bit 7 set for BR words, with
        # bits 0-3 the exponent threshold for "signal present" (0 means 6) and
        # bits 4-6 the leak suppression margin in 6 dB steps (0 means 3, 7 off).
        # 0x8F (threshold 15, which no signal reaches) instead streams debug
        # snapshots of the channelizer (see BRChannelizer)
        m.d.sync += dbg_on.eq(tx_pstep == 0x8F)
        m.d.sync += [br_on.eq(tx_pstep[7]), br_eth.eq(Mux(tx_pstep[0:4] == 0, 6, tx_pstep[0:4])),
                     br_de.eq(Mux(tx_pstep[4:7] == 0, 3, Mux(tx_pstep[4:7] == 7, 0, tx_pstep[4:7])))]

        m.d.sync += [
            # Trigger enable.
            mcu_intf.trigger_en                 .eq(ctrl[7]),

            # PRBS enable.
            mcu_intf.prbs                       .eq(ctrl[6]),

            # RX settings.
            rx_chain["dc_block"].enable         .eq(ctrl[0]),
            rx_chain["quarter_shift"].enable    .eq(rx_pstep[-2]),
            rx_chain["quarter_shift"].up        .eq(rx_pstep[-1]),
        ]

        # RX decimation rate.  
        rx_enables = Signal(5)
        m.d.sync += rx_enables.eq((1 << rx_decim) - 1)  # binary to unary conversion
        m.d.comb += [
            # RX decimation rate.
            rx_chain["hbfir1"].enable           .eq(rx_enables[0]),
        ]

        return m


if __name__ == "__main__":
    import os
    plat = PralinePlatform()
    plat.build(Top(), name="br_rx", build_dir="build-br",
               nextpnr_opts="--seed " + os.environ.get("SEED", "1"))
