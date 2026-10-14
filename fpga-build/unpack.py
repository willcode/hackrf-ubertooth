#!/usr/bin/env python3
#
# SPDX-License-Identifier: BSD-3-Clause

"""Unpack a gateware pack into its bitstreams.

A pack starts with a little-endian uint32 image count, followed by one
little-endian uint32 offset per image, measured from the start of the pack.
Each image at its offset is a chain of blocks. A block is a little-endian
uint16 length and that many bytes of raw LZ4 block data. A block holds at
most 4096 compressed bytes, the size of the firmware loader's input buffer,
and decompresses to at most 4096 bytes. A block of length 0 ends the image.

usage: unpack.py <pack> <output prefix>

The bitstreams are written to <output prefix><index>.bin.
"""

import struct
import sys

import lz4.block

BLOCK_SIZE = 4096


class PackError(ValueError):
    pass


def image_extent(pack, offset):
    """Return the end of the block chain that starts at offset."""
    pos = offset
    index = 0
    while True:
        if pos + 2 > len(pack):
            raise PackError(f"block length at {pos} runs past the end of the pack")
        (length,) = struct.unpack_from("<H", pack, pos)
        pos += 2
        if length == 0:
            return pos
        if length > BLOCK_SIZE:
            raise PackError(f"block {index} at {pos - 2} holds {length} compressed bytes, "
                            f"over the loader's {BLOCK_SIZE}-byte input buffer")
        if pos + length > len(pack):
            raise PackError(f"block {index} at {pos - 2} runs past the end of the pack")
        pos += length
        index += 1


def unpack_image(pack, offset):
    """Decompress the block chain that starts at offset."""
    out = bytearray()
    pos = offset
    while True:
        (length,) = struct.unpack_from("<H", pack, pos)
        pos += 2
        if length == 0:
            return bytes(out)
        try:
            block = lz4.block.decompress(pack[pos:pos + length],
                uncompressed_size=BLOCK_SIZE)
        except lz4.block.LZ4BlockError as e:
            raise PackError(f"block at {pos - 2} does not decompress to {BLOCK_SIZE} bytes or less") from e
        out += block
        pos += length


def unpack(pack):
    """Return the list of bitstreams in a pack.

    The header and every block chain are checked before any block is
    decompressed: each offset is the end of the offset table or of the
    previous chain, every block holds at most 4096 compressed bytes and lies
    inside the pack, and the last chain ends at the end of the pack.
    """
    if len(pack) < 4:
        raise PackError("pack shorter than its image count")
    (count,) = struct.unpack_from("<I", pack, 0)
    table_end = 4 + 4 * count
    if count == 0 or table_end > len(pack):
        raise PackError(f"image count {count} does not fit the pack")
    offsets = struct.unpack_from(f"<{count}I", pack, 4)
    end = table_end
    for offset in offsets:
        if offset != end:
            raise PackError(f"image offset {offset}, expected {end}")
        end = image_extent(pack, offset)
    if end != len(pack):
        raise PackError(f"last image ends at {end}, pack length {len(pack)}")
    return [unpack_image(pack, offset) for offset in offsets]


def main(argv):
    if len(argv) != 3:
        sys.stderr.write(__doc__)
        return 2
    with open(argv[1], "rb") as f:
        images = unpack(f.read())
    for index, image in enumerate(images):
        with open(f"{argv[2]}{index}.bin", "wb") as f:
            f.write(image)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
