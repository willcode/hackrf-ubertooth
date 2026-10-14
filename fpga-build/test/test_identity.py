#
# This file is part of HackRF.
#
# SPDX-License-Identifier: BSD-3-Clause

"""Simulation of the identity block's register reads.

The SPI port itself is the iCE40's hard SB_SPI core, which cannot be simulated,
so the test replaces SPIRegisterInterface's command interface with plain
signals and checks the value the register file offers for each read address.
"""

import unittest

from amaranth     import Elaboratable, Module, Signal
from amaranth.sim import Simulator

from interface.spi import SPIRegisterInterface
from util.identity import (IDENTITY_BASE, add_identity, identity_bytes,
                           KIND_STANDARD_RX, CAP_RX, CAP_STANDARD_MAP)


class CommandStub(Elaboratable):
    """The signals SPIRegisterInterface uses from SPICommandInterface."""
    def __init__(self):
        self.command       = Signal(8)
        self.command_ready = Signal()
        self.word_received = Signal(8)
        self.word_complete = Signal()
        self.word_to_send  = Signal(8)

    def elaborate(self, platform):
        return Module()


class IdentityTest(unittest.TestCase):

    def _build(self, with_identity):
        # Construct without a port: replicate __init__ with the stub interface.
        regs = SPIRegisterInterface.__new__(SPIRegisterInterface)
        regs.address_size  = 7
        regs.register_size = 8
        regs.interface     = CommandStub()
        regs.registers     = {}
        regs._is_write     = Signal()
        regs._address      = Signal(7)
        ctrl = regs.add_register(0x01, init=0)
        decim = regs.add_register(0x02, init=0, size=3)
        if with_identity:
            add_identity(regs, KIND_STANDARD_RX, CAP_RX | CAP_STANDARD_MAP, build_id=0x123456)
        return regs

    def _read_all(self, regs, addresses, ticks=1):
        sim = Simulator(regs)
        sim.add_clock(1e-6)
        values = {}

        async def bench(ctx):
            for address in addresses:
                ctx.set(regs.interface.command, address)   # write bit 0: a read
                await ctx.tick().repeat(ticks)
                values[address] = ctx.get(regs.interface.word_to_send)

        sim.add_testbench(bench)
        sim.run()
        return values

    def test_identity_reads_back(self):
        regs = self._build(with_identity=True)
        addresses = list(range(IDENTITY_BASE, IDENTITY_BASE + 8))
        values = self._read_all(regs, addresses)
        expected = identity_bytes(KIND_STANDARD_RX, CAP_RX | CAP_STANDARD_MAP, 0x123456)
        self.assertEqual([values[a] for a in addresses], expected)
        self.assertEqual(expected[:3], [0x47, 0x57, 1])
        self.assertEqual(expected[5:], [0x56, 0x34, 0x12])

    def test_two_stage_read(self):
        # The read multiplexer in two registered stages: the same values two cycles after the
        # address, and 0 where no register is.
        regs = self._build(with_identity=True)
        regs.registered_read = 2
        addresses = list(range(IDENTITY_BASE, IDENTITY_BASE + 8)) + [0x03, 0x09, 0x40]
        values = self._read_all(regs, addresses, ticks=2)
        expected = identity_bytes(KIND_STANDARD_RX, CAP_RX | CAP_STANDARD_MAP, 0x123456)
        self.assertEqual([values[a] for a in addresses], expected + [0, 0, 0])

    def test_without_identity_reads_zero(self):
        regs = self._build(with_identity=False)
        values = self._read_all(regs, range(IDENTITY_BASE, IDENTITY_BASE + 8))
        self.assertTrue(all(v == 0 for v in values.values()))

    def test_identity_is_read_only_and_leaves_other_registers(self):
        regs = self._build(with_identity=True)
        sim = Simulator(regs)
        sim.add_clock(1e-6)
        seen = {}

        async def bench(ctx):
            # A write to the identity range must change nothing.
            ctx.set(regs.interface.command, 0x80 | IDENTITY_BASE)
            ctx.set(regs.interface.word_received, 0xAA)
            ctx.set(regs.interface.word_complete, 1)
            await ctx.tick()
            ctx.set(regs.interface.word_complete, 0)
            ctx.set(regs.interface.command, IDENTITY_BASE)
            await ctx.tick()
            seen['magic'] = ctx.get(regs.interface.word_to_send)
            ctx.set(regs.interface.command, 0x01)
            await ctx.tick()
            seen['ctrl'] = ctx.get(regs.interface.word_to_send)

        sim.add_testbench(bench)
        sim.run()
        self.assertEqual(seen['magic'], 0x47)
        self.assertEqual(seen['ctrl'], 0x00)


if __name__ == "__main__":
    unittest.main()
