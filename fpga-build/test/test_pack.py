#
# SPDX-License-Identifier: BSD-3-Clause

import contextlib
import hashlib
import io
import json
import os
import struct
import tempfile
import unittest

import lz4.block

import pack
import unpack

HERE = os.path.dirname(os.path.abspath(__file__))
PACK_CANDIDATES = [
    os.environ.get("PRALINE_FPGA_BIN", ""),
    os.path.join(HERE, "..", "build", "praline_fpga.bin"),
    os.path.join(HERE, "..", "..", "hackrf", "firmware", "fpga", "build", "praline_fpga.bin"),
    os.path.join(HERE, "..", "..", "firmware", "hackrf", "firmware", "fpga", "build", "praline_fpga.bin"),
]

# The four stock images in the checked-in pack, in order.
STOCK_SIZE = 104090
STOCK_SHA256_PREFIXES = ["1f0c1890b3ffb03d", "897403e1d28ea299", "b72857b250967075", "7a3c34833799ac2d"]


def stock_pack():
    for path in PACK_CANDIDATES:
        if path and os.path.exists(path):
            with open(path, "rb") as f:
                return f.read()
    raise FileNotFoundError("praline_fpga.bin not found; set PRALINE_FPGA_BIN")


class StockPackTest(unittest.TestCase):

    def setUp(self):
        self.stock = stock_pack()
        self.images = unpack.unpack(self.stock)

    def test_unpack_stock(self):
        self.assertEqual(len(self.images), 4)
        for image, prefix in zip(self.images, STOCK_SHA256_PREFIXES):
            self.assertEqual(len(image), STOCK_SIZE)
            self.assertEqual(hashlib.sha256(image).hexdigest()[:16], prefix)

    def test_single_image_round_trip(self):
        for index, image in enumerate(self.images):
            with self.subTest(image=index):
                packed = pack.pack([image])
                self.assertEqual(struct.unpack_from("<II", packed, 0), (1, 8))
                self.assertEqual(unpack.image_extent(packed, 8), len(packed))
                self.assertEqual(unpack.unpack(packed), [image])

    def test_repack_matches_stock(self):
        self.assertEqual(pack.pack(self.images), self.stock)


class MalformedPackTest(unittest.TestCase):

    def setUp(self):
        self.image = bytes(range(256)) * 20 + b"\x5a" * 1000
        self.packed = pack.pack([self.image])

    def test_blocks(self):
        lengths, pos = [], 8
        while True:
            (length,) = struct.unpack_from("<H", self.packed, pos)
            lengths.append(length)
            pos += 2 + length
            if length == 0:
                break
        self.assertEqual(len(lengths) - 1, -(-len(self.image) // pack.BLOCK_SIZE))
        self.assertEqual(pos, len(self.packed))

    def test_truncated(self):
        for cut in (2, 6, 10, len(self.packed) - 1):
            with self.subTest(cut=cut):
                with self.assertRaises(unpack.PackError):
                    unpack.unpack(self.packed[:cut])

    def test_trailing_bytes(self):
        with self.assertRaises(unpack.PackError):
            unpack.unpack(self.packed + b"\0")

    def test_bad_count(self):
        for count in (0, 2):
            with self.subTest(count=count):
                with self.assertRaises(unpack.PackError):
                    unpack.unpack(struct.pack("<I", count) + self.packed[4:])

    def test_bad_offset(self):
        with self.assertRaises(unpack.PackError):
            unpack.unpack(self.packed[:4] + struct.pack("<I", 10) + self.packed[8:])

    def test_block_past_end(self):
        bad = bytearray(self.packed)
        struct.pack_into("<H", bad, 8, 0xFFFF)
        with self.assertRaises(unpack.PackError):
            unpack.unpack(bytes(bad))

    def test_pack_refuses_incompressible_block(self):
        image = bytes(range(256)) * 16 + os.urandom(pack.BLOCK_SIZE)
        with self.assertRaises(unpack.PackError) as caught:
            pack.pack([image])
        self.assertIn("block 1 (input bytes 4096 to 8191)", str(caught.exception))

    def test_unpack_refuses_long_compressed_block(self):
        block = bytes(pack.BLOCK_SIZE + 1)
        bad = struct.pack("<II", 1, 8) + struct.pack("<H", len(block)) + block + struct.pack("<H", 0)
        with self.assertRaises(unpack.PackError) as caught:
            unpack.unpack(bad)
        self.assertIn("block 0 at 8 holds 4097 compressed bytes", str(caught.exception))

    def test_oversized_block(self):
        block = lz4.block.compress(bytes(2 * pack.BLOCK_SIZE), store_size=False)
        bad = struct.pack("<II", 1, 8) + struct.pack("<H", len(block)) + block + struct.pack("<H", 0)
        with self.assertRaises(unpack.PackError):
            unpack.unpack(bad)


class AdmitTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bin = os.path.join(self.tmp.name, "img.bin")
        self.gate = os.path.join(self.tmp.name, "img.gate.json")
        self.out = os.path.join(self.tmp.name, "img.pack")
        self.image = b"\xff\x00\x00\xff" + pack.SYNC_WORD + bytes(range(256)) * 406 + bytes(pack.IMAGE_SIZE - 8 - 256 * 406)
        self.write_bin(self.image)

    def tearDown(self):
        self.tmp.cleanup()

    def write_bin(self, data):
        with open(self.bin, "wb") as f:
            f.write(data)

    def write_gate(self, verdict="PASS", digest=None, flags=None, reference_sha256=None):
        with open(pack.REFERENCE, "rb") as f:
            stock_reference = hashlib.sha256(f.read()).hexdigest()
        with open(self.gate, "w") as f:
            json.dump({"verdict": verdict,
                       "bitstream_sha256": digest or hashlib.sha256(self.image).hexdigest(),
                       "reference": {"path": "ref.json", "sha256": reference_sha256 or stock_reference},
                       "flags": flags or {"allow_pll": False, "extra_clocks": {}}}, f)

    def run_pack(self, *flags):
        return pack.main(["pack.py", *flags, self.bin, self.out])

    def test_gated_image_packs(self):
        self.write_gate()
        self.assertEqual(self.run_pack(), 0)
        with open(self.out, "rb") as f:
            self.assertEqual(unpack.unpack(f.read()), [self.image])

    def test_no_gate_file(self):
        self.assertEqual(self.run_pack(), 1)
        self.assertFalse(os.path.exists(self.out))

    def test_gate_failed(self):
        self.write_gate(verdict="FAIL")
        self.assertEqual(self.run_pack(), 1)

    def test_gate_for_other_bytes(self):
        self.write_gate(digest="0" * 64)
        self.assertEqual(self.run_pack(), 1)

    def test_wrong_size(self):
        self.write_gate()
        for data in (b"", self.image[:-1], self.image + b"\0"):
            with self.subTest(size=len(data)):
                self.write_bin(data)
                self.assertEqual(self.run_pack(), 1)

    def test_no_sync_word(self):
        self.image = bytes(pack.IMAGE_SIZE)
        self.write_bin(self.image)
        self.write_gate()
        self.assertEqual(self.run_pack(), 1)

    def test_stock_image_without_gate(self):
        images = unpack.unpack(stock_pack())
        for index, image in enumerate(images[:3]):
            with self.subTest(image=index):
                self.write_bin(image)
                self.assertEqual(self.run_pack(), 0)
                with open(self.out, "rb") as f:
                    self.assertEqual(unpack.unpack(f.read()), [image])

    def test_stock_tx_image_needs_flag(self):
        self.write_bin(unpack.unpack(stock_pack())[3])
        self.assertEqual(self.run_pack(), 1)
        self.assertFalse(os.path.exists(self.out))
        self.assertEqual(self.run_pack("--allow-tx"), 0)

    def test_pass_under_allow_pll_needs_flag(self):
        self.write_gate(flags={"allow_pll": True, "extra_clocks": {}})
        self.assertEqual(self.run_pack(), 1)
        self.assertEqual(self.run_pack("--allow-pll"), 0)

    def test_pass_with_extra_clock_needs_flag(self):
        self.write_gate(flags={"allow_pll": False, "extra_clocks": {"pll_out": 60.0}})
        self.assertEqual(self.run_pack(), 1)
        self.assertEqual(self.run_pack("--allow-extra-clock"), 0)

    def test_pass_against_other_reference_refused(self):
        self.write_gate(reference_sha256="1" * 64)
        self.assertEqual(self.run_pack(), 1)
        with self.assertRaises(SystemExit):
            self.run_pack("--allow-other-reference")
        self.assertFalse(os.path.exists(self.out))

    def test_missing_bitstream(self):
        os.remove(self.bin)
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            self.assertEqual(self.run_pack(), 1)
        self.assertTrue(errors.getvalue().startswith("pack.py: refused: cannot read the bitstream:"))
        self.assertNotIn("Traceback", errors.getvalue())
        self.assertFalse(os.path.exists(self.out))


class StockEntriesTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        with open(pack.REFERENCE) as f:
            self.stock = json.load(f)
        self.image = b"\xff\x00\x00\xff" + pack.SYNC_WORD + bytes(range(256)) * 406 + \
            bytes(pack.IMAGE_SIZE - 8 - 256 * 406)

    def tearDown(self):
        self.tmp.cleanup()

    def admit_against(self, ref):
        """Gate the image PASS against a copy of ref and admit it with that copy as the stock reference."""
        ref_path = os.path.join(self.tmp.name, "standard.json")
        with open(ref_path, "w") as f:
            json.dump(ref, f)
        with open(ref_path, "rb") as f:
            ref_sha256 = hashlib.sha256(f.read()).hexdigest()
        gate_path = os.path.join(self.tmp.name, "img.gate.json")
        with open(gate_path, "w") as f:
            json.dump({"verdict": "PASS", "bitstream_sha256": hashlib.sha256(self.image).hexdigest(),
                       "reference": {"path": ref_path, "sha256": ref_sha256},
                       "flags": {"allow_pll": False, "extra_clocks": {}}}, f)
        return pack.admit(self.image, gate_path, reference=ref_path)

    def test_constant_matches_stock_reference(self):
        self.assertEqual(pack.entries_sha256(self.stock), pack.STOCK_ENTRIES_SHA256)

    def test_reference_with_pin_48_output_refused(self):
        self.stock["io"]["48"]["parameters"]["PIN_TYPE"] = "011000"
        with self.assertRaises(unpack.PackError) as caught:
            self.admit_against(self.stock)
        self.assertIn("other than the board's", str(caught.exception))

    def test_reference_with_other_clocks_admitted(self):
        self.stock["clocks"]["clk"] = 50.0
        self.assertTrue(self.admit_against(self.stock).startswith("gate PASS"))


if __name__ == "__main__":
    unittest.main()
