#
# This file is part of HackRF.
#
# SPDX-License-Identifier: BSD-3-Clause

"""Read-only identity block for gateware images.

Eight registers at the top of the 7-bit register space tell firmware and host
which image is loaded. An image without the block reads 0 there, so a missing
magic means an unidentified (stock or older) image.

    0x78, 0x79  magic "GW" (0x47, 0x57)
    0x7A        layout version (1)
    0x7B        image kind (KIND_*)
    0x7C        capabilities (CAP_* bits)
    0x7D-0x7F   24-bit build ID, least significant byte first
"""

IDENTITY_BASE   = 0x78
MAGIC           = (0x47, 0x57)
LAYOUT_VERSION  = 1

# Image kinds. 0x00-0x3F follow the stock images; 0x40 and up are new images.
KIND_STANDARD        = 0x00
KIND_HALF_PRECISION  = 0x01
KIND_EXT_PRECISION_RX = 0x02
KIND_EXT_PRECISION_TX = 0x03
KIND_STANDARD_RX     = 0x40
KIND_SPECTRUM_RX     = 0x41
KIND_SPECTRUM_STREAM = 0x42
KIND_GPS             = 0x43

# Capability bits.
CAP_RX           = 1 << 0
CAP_TX           = 1 << 1
CAP_STANDARD_MAP = 1 << 2  # registers 0x01-0x06 mean what the standard image's do


def identity_bytes(kind, capabilities, build_id=0):
    """The eight register values, in address order."""
    assert 0 <= kind < 256 and 0 <= capabilities < 256 and 0 <= build_id < (1 << 24)
    return [MAGIC[0], MAGIC[1], LAYOUT_VERSION, kind, capabilities,
            build_id & 0xFF, (build_id >> 8) & 0xFF, (build_id >> 16) & 0xFF]


def add_identity(spi_regs, kind, capabilities, build_id=0):
    """Add the identity block to an SPIRegisterInterface."""
    for offset, value in enumerate(identity_bytes(kind, capabilities, build_id)):
        spi_regs.add_read_only_register(IDENTITY_BASE + offset, read=value)
