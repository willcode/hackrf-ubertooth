#!/usr/bin/env python3
#
# SPDX-License-Identifier: BSD-3-Clause

"""Pack one bitstream in the gateware pack format.

The pack holds an image count of 1, the offset table, and the bitstream as
raw LZ4 blocks of 4 KiB with a zero-length end marker. The layout and the
compression call are those of build.py. Each compressed block is at most
4096 bytes, the size of the firmware loader's input buffer. unpack.py
reverses the format.

usage: pack.py [--gate FILE] [--allow-tx] [--allow-pll] [--allow-extra-clock]
               <bitstream> <pack>

The bitstream must be 104,090 bytes and carry the iCE40 sync word. Stock
images 0, 1 and 2 pack as they are, and stock image 3, the TX image, only
with --allow-tx. Any other image needs a gate file from check_image.py with
verdict PASS and the bitstream's SHA-256, made against reference/standard.json
beside this script. A PASS made against another reference does not pack. A
PASS made with --allow-pll or --extra-clock needs the matching --allow flag
here. The gate file defaults to <bitstream without .bin>.gate.json.

The pins, I/O cells and SPI core of reference/standard.json must hash to
STOCK_ENTRIES_SHA256, so an edited stock reference packs nothing but the
stock images.
"""

import argparse
import hashlib
import io
import json
import os
import struct
import sys

import lz4.block

from unpack import PackError

BLOCK_SIZE = 4096
IMAGE_SIZE = 104090
SYNC_WORD = b"\x7e\xaa\x99\x7e"

REFERENCE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reference", "standard.json")

# SHA-256 of the four images in the checked-in praline_fpga.bin.
STOCK_SHA256 = {
    "1f0c1890b3ffb03ded5b05354892daa2d94ffc95de7e84e80611ee6b866169bf": "stock image 0",
    "897403e1d28ea299f07d4eae9a0c9f17ccc08b26178d4e711596eaa660009c22": "stock image 1",
    "b72857b2509670759e60d16ab36e3eee014aa000064b43d33992986a0a3e4ac2": "stock image 2",
    "7a3c34833799ac2d01b71549b7c6a4690250255aeb0cae410ea7fa6fd8384769": "stock image 3",
}
STOCK_TX = "stock image 3"

# SHA-256 of the board's pins, I/O cells and SPI core in reference/standard.json, in the form of entries_sha256.
STOCK_ENTRIES = ("pins", "io", "spi")
STOCK_ENTRIES_SHA256 = "a4c5e95dc297cbc8b5d25dd9ed1fe2d7613e45f665628f69f65b484652105d5d"


def entries_sha256(ref):
    """Return the SHA-256 of a reference's pins, I/O cells and SPI core, as sorted compact JSON."""
    entries = {key: ref.get(key) for key in STOCK_ENTRIES}
    return hashlib.sha256(json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def compress_blockwise(f_in, f_out):
    """Write f_in as length-prefixed raw LZ4 blocks with a zero end marker.

    Raises PackError when a block compresses to more than BLOCK_SIZE bytes.
    """
    index = 0
    while block := f_in.read(BLOCK_SIZE):
        compressed_block = lz4.block.compress(block, store_size=False,
            mode="high_compression", compression=12)
        if len(compressed_block) > BLOCK_SIZE:
            start = index * BLOCK_SIZE
            raise PackError(f"block {index} (input bytes {start} to {start + len(block) - 1}) compresses to "
                            f"{len(compressed_block)} bytes, over the loader's {BLOCK_SIZE}-byte input buffer")
        f_out.write(struct.pack("<H", len(compressed_block)))
        f_out.write(compressed_block)
        index += 1
    f_out.write(struct.pack("<H", 0))


def pack(images):
    """Return a pack holding the given bitstreams in order."""
    out = io.BytesIO()
    out.write(struct.pack("<I", len(images)))
    out.write(bytes(4 * len(images)))
    offsets = []
    for image in images:
        offsets.append(out.tell())
        compress_blockwise(io.BytesIO(image), out)
    out.seek(4)
    for offset in offsets:
        out.write(struct.pack("<I", offset))
    return out.getvalue()


def admit(image, gate_path, allow_tx=False, allow_pll=False, allow_extra_clock=False, reference=REFERENCE):
    """Raise PackError unless the bitstream may leave the desk; return the reason it may."""
    if len(image) != IMAGE_SIZE:
        raise PackError(f"bitstream is {len(image)} bytes, an iCE40UP5K image is {IMAGE_SIZE}")
    if SYNC_WORD not in image[:64]:
        raise PackError("bitstream has no iCE40 sync word in its first 64 bytes")
    digest = hashlib.sha256(image).hexdigest()
    if digest in STOCK_SHA256:
        if STOCK_SHA256[digest] == STOCK_TX and not allow_tx:
            raise PackError("stock image 3 is the TX image; add --allow-tx to pack it")
        return STOCK_SHA256[digest]
    if not os.path.exists(gate_path):
        raise PackError(f"no gate file {gate_path}; run check_image.py first")
    with open(gate_path) as f:
        gate = json.load(f)
    if gate.get("verdict") != "PASS":
        raise PackError(f"gate file {gate_path} has verdict {gate.get('verdict')}")
    if gate.get("bitstream_sha256") != digest:
        raise PackError(f"gate file {gate_path} was written for SHA-256 {gate.get('bitstream_sha256')}, "
                        f"the bitstream has {digest}")
    flags = gate.get("flags", {})
    if flags.get("allow_pll") and not allow_pll:
        raise PackError(f"gate file {gate_path} passed under --allow-pll; add --allow-pll to pack it")
    if flags.get("extra_clocks") and not allow_extra_clock:
        raise PackError(f"gate file {gate_path} passed with extra clocks {flags['extra_clocks']}; "
                        f"add --allow-extra-clock to pack it")
    with open(reference, "rb") as f:
        reference_bytes = f.read()
    reference_sha256 = hashlib.sha256(reference_bytes).hexdigest()
    if entries_sha256(json.loads(reference_bytes)) != STOCK_ENTRIES_SHA256:
        raise PackError(f"{reference} has pins, I/O cells or an SPI core other than the board's")
    gate_reference = gate.get("reference", {})
    if gate_reference.get("sha256") != reference_sha256:
        raise PackError(f"gate file {gate_path} was checked against {gate_reference.get('path')} "
                        f"(SHA-256 {gate_reference.get('sha256')}), not {reference}; "
                        f"run check_image.py without --reference")
    return f"gate PASS in {gate_path}, flags {flags}, reference {gate_reference.get('path')}"


def main(argv):
    parser = argparse.ArgumentParser(description="Pack one bitstream in the gateware pack format.")
    parser.add_argument("bitstream")
    parser.add_argument("pack")
    parser.add_argument("--gate", help="gate file written by check_image.py")
    parser.add_argument("--allow-tx", action="store_true", help="pack stock image 3, the TX image")
    parser.add_argument("--allow-pll", action="store_true", help="accept a PASS made under --allow-pll")
    parser.add_argument("--allow-extra-clock", action="store_true", help="accept a PASS made with --extra-clock")
    args = parser.parse_args(argv[1:])
    gate_path = args.gate or (args.bitstream[:-4] if args.bitstream.endswith(".bin") else args.bitstream) \
        + ".gate.json"
    try:
        with open(args.bitstream, "rb") as f:
            image = f.read()
    except OSError as e:
        sys.stderr.write(f"pack.py: refused: cannot read the bitstream: {e}\n")
        return 1
    try:
        reason = admit(image, gate_path, args.allow_tx, args.allow_pll, args.allow_extra_clock)
        packed = pack([image])
    except (PackError, OSError, ValueError) as e:
        sys.stderr.write(f"pack.py: refused: {e}\n")
        return 1
    with open(args.pack, "wb") as f:
        f.write(packed)
    print(f"wrote {args.pack}: {len(packed)} bytes, {reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
