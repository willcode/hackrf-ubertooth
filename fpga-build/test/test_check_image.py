#
# SPDX-License-Identifier: BSD-3-Clause

import contextlib
import copy
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import check_image
import pack
import record_versions

# A small netlist with the stock register port (one SB_SPI on pins 14 to 17), one input, one output, the ADC
# clock pin, and one flip-flop in each of the three clock domains.
PINS = {"spi_0__cipo__io": "14", "spi_0__copi__io": "17", "spi_0__clk__io": "15", "spi_0__cs__io": "16",
        "trigger_in_0__io": "48", "trigger_out_0__io": "2", "afe_clk_0__io": "35"}
SET_FREQUENCY = "set_frequency clkgen.signal_out 40.0\nset_frequency cdc.daclk_clk 40.0\n" \
                "set_frequency afe_clk_0__io 40.0\n"
IN_PORTS = {"PACKAGE_PIN": "input", "D_IN_0": "output", "CLOCK_ENABLE": "input"}
OUT_PORTS = {"PACKAGE_PIN": "output", "D_OUT_0": "input", "OUTPUT_ENABLE": "input", "CLOCK_ENABLE": "input"}
DFF_PORTS = {"C": "input", "D": "input", "Q": "output"}

TIMING = """Info: constraining clock net 'clkgen.signal_out' to 40.00 MHz
Info: constraining clock net 'cdc.daclk_clk' to 40.00 MHz
Info: constraining clock net 'afe_clk_0__io' to 40.00 MHz
Info:     Derived frequency constraint of 48.0 MHz for net clk
Info: Max frequency for clock 'adclk_clk_$glb_clk': 31.00 MHz (FAIL at 40.00 MHz)
Info: Routing complete.
Info: Max frequency for clock                'clk': 54.14 MHz (PASS at 48.00 MHz)
Info: Max frequency for clock 'daclk_clk_$glb_clk': 42.29 MHz (PASS at 40.00 MHz)
Info: Max frequency for clock 'adclk_clk_$glb_clk': 44.61 MHz (PASS at 40.00 MHz)
Info: Program finished normally.
"""

VERSIONS = {"yosys": "Yosys 0.68 (git sha1 38e001a6f)", "nextpnr": "(Version nextpnr-0.11.1)",
            "since": "2026-10-01T00:00:00-04:00"}
HERE = os.path.dirname(os.path.abspath(__file__))


def io_cell(port_bit, direction, net, pin_type, cell_type="SB_IO"):
    if direction == "in":
        return {"type": cell_type, "parameters": {"IO_STANDARD": "SB_LVCMOS", "PIN_TYPE": pin_type},
                "port_directions": IN_PORTS,
                "connections": {"PACKAGE_PIN": [port_bit], "D_IN_0": [net], "CLOCK_ENABLE": ["1"]}}
    return {"type": cell_type, "parameters": {"IO_STANDARD": "SB_LVCMOS", "PIN_TYPE": pin_type},
            "port_directions": OUT_PORTS,
            "connections": {"PACKAGE_PIN": [port_bit], "D_OUT_0": [net[0]], "OUTPUT_ENABLE": [net[1]],
                            "CLOCK_ENABLE": ["1"]}}


def dff(clock):
    return {"type": "SB_DFF", "parameters": {}, "port_directions": DFF_PORTS,
            "connections": {"C": [clock], "D": [25], "Q": [50 + clock]}}


def netlist():
    ports = {name: {"direction": "inout", "bits": [2 + i]} for i, name in enumerate(PINS)}
    cells = {
        "cipo": io_cell(2, "out", (20, 21), "101001"),
        "copi": io_cell(3, "in", 22, "000001"),
        "clk": io_cell(4, "in", 23, "000001"),
        "cs": io_cell(5, "in", 24, "000001"),
        "trig_in": io_cell(6, "in", 25, "000000"),
        "trig_out": io_cell(7, "out", (26, "1"), "011000"),
        "afe": io_cell(8, "in", 39, "000001", "SB_GB_IO"),
        "spi": {"type": "SB_SPI", "parameters": {"BUS_ADDR74": "0b0010"},
                "port_directions": {"SO": "output", "SOE": "output", "SI": "input", "SCKI": "input",
                                    "SCSNI": "input"},
                "connections": {"SO": [20], "SOE": [21], "SI": [22], "SCKI": [23], "SCSNI": [30]}},
        "cs_inv": {"type": "SB_LUT4", "parameters": {"LUT_INIT": "0101010101010101"},
                   "port_directions": {"I0": "input", "I1": "input", "I2": "input", "I3": "input", "O": "output"},
                   "connections": {"I0": [24], "I1": ["0"], "I2": ["0"], "I3": ["0"], "O": [30]}},
        "delay": {"type": "SB_LUT4", "parameters": {"LUT_INIT": "0000000000000010"},
                  "port_directions": {"I0": "input", "I1": "input", "I2": "input", "I3": "input", "O": "output"},
                  "connections": {"I0": [39], "I1": ["0"], "I2": ["0"], "I3": ["0"], "O": [40]}},
        "osc": {"type": "SB_HFOSC", "parameters": {}, "port_directions": {"CLKHF": "output"},
                "connections": {"CLKHF": [42]}},
        "ff_ad": dff(40), "ff_da": dff(41), "ff_sys": dff(42),
    }
    netnames = {"adclk_clk": {"bits": [40]}, "clkgen.signal_out": {"bits": [40]},
                "daclk_clk": {"bits": [41]}, "cdc.daclk_clk": {"bits": [41]}, "clk": {"bits": [42]}}
    return {"modules": {"top": {"attributes": {"top": "1"}, "ports": ports, "cells": cells,
                                "netnames": netnames}}}


class GateTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.write(netlist())
        self.ref = check_image.make_reference(self.dir, "img")
        # The tests use the netlist's own reference as the stock reference.
        self.stock_path = os.path.join(self.dir, "stock-ref.json")
        with open(self.stock_path, "w") as f:
            json.dump(self.ref, f)
        patcher = mock.patch.object(check_image, "STOCK_REFERENCE", self.stock_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def path(self, ext):
        return os.path.join(self.dir, "img" + ext)

    def write(self, design, pins=PINS, set_frequency=SET_FREQUENCY, timing=TIMING, versions=VERSIONS):
        with open(self.path(".json"), "w") as f:
            json.dump(design, f)
        with open(self.path(".pcf"), "w") as f:
            f.writelines(f"set_io {name} {pin}\n" for name, pin in pins.items())
            f.write(set_frequency)
        with open(self.path(".tim"), "w") as f:
            f.write(timing)
        with open(self.path(".ys"), "w") as f:
            f.write("# Automatically generated by Amaranth 0.5.8. Do not edit.\n")
        with open(self.path(".rpt"), "w") as f:
            f.write(" Yosys 0.68 (git sha1 38e001a6f)\n")
        with open(self.path(".asc"), "w") as f:
            f.write(".comment from next-pnr\n")
        with open(self.path(".bin"), "wb") as f:
            f.write(b"\xff\x00\x00\xff" + check_image.SYNC_WORD + bytes(check_image.IMAGE_SIZE - 8))
        with open(os.path.join(self.dir, check_image.VERSIONS_FILE), "w") as f:
            json.dump(dict(versions, images=record_versions.image_hashes(self.dir, ["img"])[0]), f)

    def failures(self, design=None, allow_pll=False, extra_clocks=None, **files):
        self.write(design or netlist(), **files)
        facts = check_image.describe(self.dir, "img")
        timing = check_image.read_timing(self.path(".tim"))
        failures = check_image.check(facts, self.ref, allow_pll)
        failures += check_image.check_constraints(facts, self.ref)
        failures += check_image.check_timing(timing, facts["module"], self.ref, extra_clocks)[0]
        return failures

    def run_main(self, *extra):
        ref_path = os.path.join(self.dir, "ref.json")
        if not os.path.exists(ref_path):
            with open(ref_path, "w") as f:
                json.dump(self.ref, f)
        return check_image.main(["check_image.py", "--reference", ref_path, *extra, self.dir, "img"])

    def gate(self):
        with open(self.path(".gate.json")) as f:
            return json.load(f)


class CellAndPinTest(GateTest):

    def add_cell(self, cell_type):
        design = netlist()
        design["modules"]["top"]["cells"]["extra"] = {"type": cell_type, "parameters": {},
                                                      "port_directions": {}, "connections": {}}
        return design

    def test_reference_passes(self):
        self.assertEqual(self.failures(), [])
        self.assertEqual(self.ref["spi"][0]["pins"],
                         {"SO": ["14"], "SOE": ["14"], "SI": ["17"], "SCKI": ["15"], "SCSNI": ["16"]})
        self.assertEqual(self.ref["set_frequency"], {"adclk_clk": 40.0, "daclk_clk": 40.0, "afe_clk_0__io": 40.0})
        self.assertEqual(self.ref["clocks"], {"clk": 48.0, "daclk_clk_$glb_clk": 40.0, "adclk_clk_$glb_clk": 40.0})

    def test_forbidden_cells(self):
        for cell_type in check_image.FORBIDDEN_CELLS:
            with self.subTest(cell=cell_type):
                failures = self.failures(self.add_cell(cell_type))
                self.assertEqual(len(failures), 1)
                self.assertIn(cell_type, failures[0])

    def test_unknown_cell(self):
        self.assertEqual(len(self.failures(self.add_cell("SB_FILTER_50NS"))), 1)

    def test_pll_needs_flag(self):
        for cell_type in sorted(check_image.PLL_CORE):
            with self.subTest(cell=cell_type):
                self.assertEqual(len(self.failures(self.add_cell(cell_type))), 1)
                self.assertEqual(self.failures(self.add_cell(cell_type), allow_pll=True), [])

    def test_pll_pad_refused_with_flag(self):
        failures = self.failures(self.add_cell("SB_PLL40_2F_PAD"), allow_pll=True)
        self.assertEqual(len(failures), 1)
        self.assertIn("admits only", failures[0])

    def test_second_spi(self):
        design = netlist()
        cells = design["modules"]["top"]["cells"]
        cells["spi2"] = copy.deepcopy(cells["spi"])
        cells["spi2"]["parameters"]["BUS_ADDR74"] = "0b0000"
        failures = self.failures(design)
        self.assertEqual(len(failures), 1)
        self.assertIn("2 SB_SPI", failures[0])

    def test_spi_other_core(self):
        design = netlist()
        design["modules"]["top"]["cells"]["spi"]["parameters"]["BUS_ADDR74"] = "0b0000"
        self.assertEqual(len(self.failures(design)), 1)

    def test_spi_other_pin(self):
        design = netlist()
        design["modules"]["top"]["cells"]["spi"]["connections"]["SI"] = [25]
        failures = self.failures(design)
        self.assertEqual(len(failures), 1)
        self.assertIn("spi", failures[0])

    def test_pin_type(self):
        design = netlist()
        design["modules"]["top"]["cells"]["trig_in"]["parameters"]["PIN_TYPE"] = "101001"
        failures = self.failures(design)
        self.assertEqual(len(failures), 1)
        self.assertIn("pin 48", failures[0])

    def test_pin_moved(self):
        failures = self.failures(pins=dict(PINS, trigger_in_0__io="47"))
        self.assertTrue(any(f.startswith("pins:") for f in failures))
        self.assertTrue(any("pin 47" in f for f in failures))


class ConstraintTest(GateTest):

    def test_constraint_lowered(self):
        failures = self.failures(set_frequency=SET_FREQUENCY.replace("cdc.daclk_clk 40.0", "cdc.daclk_clk 30.0"))
        self.assertEqual(failures, ["constraints: set_frequency on net 'daclk_clk' is 30.0 MHz, stock 40.0 MHz"])

    def test_constraint_on_renamed_net(self):
        design = netlist()
        design["modules"]["top"]["netnames"]["other.daclk_clk"] = {"bits": [41]}
        sf = SET_FREQUENCY.replace("cdc.daclk_clk", "other.daclk_clk")
        self.assertEqual(self.failures(design, set_frequency=sf, timing=TIMING.replace("cdc.", "other.")), [])

    def test_constraint_missing(self):
        failures = self.failures(set_frequency=SET_FREQUENCY.replace("set_frequency afe_clk_0__io 40.0\n", ""))
        self.assertEqual(failures, ["constraints: no set_frequency on net 'afe_clk_0__io', stock 40.0 MHz"])


class TimingTest(GateTest):

    def test_pre_route_fail_ignored(self):
        self.assertEqual(self.failures(), [])

    def test_timing_fail(self):
        failures = self.failures(timing=TIMING.replace("44.61 MHz (PASS", "38.65 MHz (FAIL"))
        self.assertEqual(len(failures), 1)
        self.assertIn("38.65", failures[0])

    def test_default_target_beside_derived_12_mhz(self):
        timing = TIMING.replace("constraining clock net 'clkgen.signal_out' to 40.00 MHz",
                                "Derived frequency constraint of 12.0 MHz for net osc12") \
                       .replace("44.61 MHz (PASS at 40.00 MHz)", "44.61 MHz (PASS at 12.00 MHz)")
        failures = self.failures(timing=timing)
        self.assertIn("timing: clock 'adclk_clk_$glb_clk' has target 12.00 MHz and no constraint on net "
                      "'adclk_clk'", failures)

    def test_target_lowered_with_constraint(self):
        timing = TIMING.replace("'cdc.daclk_clk' to 40.00", "'cdc.daclk_clk' to 30.00") \
                       .replace("42.29 MHz (PASS at 40.00 MHz)", "35.00 MHz (PASS at 30.00 MHz)")
        failures = self.failures(timing=timing)
        self.assertEqual(failures, ["timing: clock 'daclk_clk_$glb_clk' has target 30.00 MHz, "
                                    "below the stock 40.00 MHz"])

    def test_repeated_clock_line(self):
        timing = TIMING.replace("Info: Program", "Info: Max frequency for clock 'adclk_clk_$glb_clk': "
                                                 "44.61 MHz (PASS at 40.00 MHz)\nInfo: Program")
        failures = self.failures(timing=timing)
        self.assertEqual(len(failures), 1)
        self.assertIn("reported twice", failures[0])

    def test_unparsed_clock_line(self):
        timing = TIMING.replace("42.29 MHz (PASS at 40.00 MHz)", "42.29 MHz (failed, target 40.00 MHz)")
        failures = self.failures(timing=timing)
        self.assertTrue(any(f.startswith("timing: unparsed clock line") for f in failures))

    def test_one_and_two_decimal_forms(self):
        timing = TIMING.replace("Derived frequency constraint of 48.0 MHz", "Derived frequency constraint of 33.3 MHz") \
                       .replace("(PASS at 48.00 MHz)", "(PASS at 33.33 MHz)")
        self.write(netlist(), timing=timing)
        self.ref = check_image.make_reference(self.dir, "img")
        self.assertEqual(self.failures(timing=timing), [])

    def test_stock_clock_without_global_suffix(self):
        # The same net routed through a buffer the design placed itself (or no global
        # buffer) is reported without the suffix; it is still the stock clock.
        timing = TIMING.replace("'daclk_clk_$glb_clk': 42.29 MHz (PASS at 40.00 MHz)",
                                "'daclk_clk': 42.29 MHz (PASS at 40.00 MHz)")
        self.assertIn("'daclk_clk': 42.29", timing)
        self.assertEqual(self.failures(timing=timing), [])

    def test_stock_clock_without_global_suffix_lowered(self):
        timing = TIMING.replace("'daclk_clk_$glb_clk': 42.29 MHz (PASS at 40.00 MHz)",
                                "'daclk_clk': 42.29 MHz (PASS at 30.00 MHz)")
        timing = timing.replace("constraining clock net 'daclk_clk' to 40.00 MHz",
                                "constraining clock net 'daclk_clk' to 30.00 MHz")
        failures = self.failures(timing=timing)
        self.assertIn("timing: clock 'daclk_clk' has target 30.00 MHz, below the stock 40.00 MHz", failures)

    def test_unknown_clock(self):
        design = netlist()
        design["modules"]["top"]["netnames"]["extra_clk"] = {"bits": [43]}
        timing = TIMING.replace("Info: Program", "Info: constraining clock net 'extra_clk' to 20.00 MHz\n"
                                "Info: Max frequency for clock 'extra_clk': 30.00 MHz (PASS at 20.00 MHz)\n"
                                "Info: Program")
        failures = self.failures(design, timing=timing)
        self.assertEqual(failures, ["timing: clock 'extra_clk' is not in the reference; declare it with --extra-clock"])
        self.assertEqual(self.failures(design, timing=timing, extra_clocks={"extra_clk": 20.0}), [])
        self.assertEqual(self.failures(design, timing=timing, extra_clocks={"extra_clk": 25.0}),
                         ["timing: clock 'extra_clk' has target 20.00 MHz, below the --extra-clock 25.00 MHz"])

    def test_extra_clock_missing_with_fabric_loads(self):
        design = netlist()
        design["modules"]["top"]["netnames"]["extra_clk"] = {"bits": [43]}
        design["modules"]["top"]["cells"]["ff_extra"] = dff(43)
        failures = self.failures(design, extra_clocks={"extra_clk": 20.0})
        self.assertEqual(failures, ["timing: extra clock 'extra_clk' is not reported and net 'extra_clk' "
                                    "clocks 1 cells"])

    def test_clock_loads_on_ram_and_spram_ports(self):
        timing = "\n".join(line for line in TIMING.splitlines() if "44.61" not in line)
        for port, cell_type in (("CLOCK", "SB_SPRAM256KA"), ("RCLKN", "SB_RAM40_4KNR"), ("WCLKN", "SB_RAM40_4KNW")):
            with self.subTest(port=port):
                design = netlist()
                cells = design["modules"]["top"]["cells"]
                del cells["ff_ad"]
                cells["mem"] = {"type": cell_type, "parameters": {}, "port_directions": {port: "input"},
                                "connections": {port: [40]}}
                self.assertEqual(self.failures(design, timing=timing),
                                 ["timing: stock clock 'adclk_clk_$glb_clk' is not reported and net "
                                  "'adclk_clk' clocks 1 cells"])

    def test_stock_clock_missing_with_fabric_loads(self):
        timing = "\n".join(line for line in TIMING.splitlines() if "44.61" not in line)
        failures = self.failures(timing=timing)
        self.assertEqual(failures, ["timing: stock clock 'adclk_clk_$glb_clk' is not reported and net "
                                    "'adclk_clk' clocks 1 cells"])

    def test_stock_clock_missing_without_fabric_loads(self):
        design = netlist()
        del design["modules"]["top"]["cells"]["ff_ad"]
        timing = "\n".join(line for line in TIMING.splitlines() if "44.61" not in line)
        self.assertEqual(self.failures(design, timing=timing), [])

    def test_timing_not_routed(self):
        self.assertIn("timing: the log has no completed route",
                      self.failures(timing=TIMING.replace("Routing complete", "Routing failed")))

    def test_timing_no_clock(self):
        timing = "\n".join(line for line in TIMING.splitlines() if "Max frequency" not in line)
        self.assertIn("timing: no post-route clock reports a constraint", self.failures(timing=timing))


class GateFileTest(GateTest):

    def test_pass_records_hash_of_bytes_checked(self):
        self.assertEqual(self.run_main(), 0)
        with open(self.path(".bin"), "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
        gate = self.gate()
        self.assertEqual((gate["verdict"], gate["bitstream_sha256"]), ("PASS", digest))
        self.assertEqual(gate["tools"]["nextpnr"], VERSIONS["nextpnr"])

    def test_stale_gate_file_replaced_on_error(self):
        self.assertEqual(self.run_main(), 0)
        os.remove(self.path(".tim"))
        self.assertEqual(self.run_main(), 1)
        gate = self.gate()
        self.assertEqual(gate["verdict"], "FAIL")
        self.assertTrue(gate["failures"][0].startswith("error: FileNotFoundError"))

    def test_bitstream_older_than_log(self):
        stamp = time.time()
        os.utime(self.path(".bin"), (stamp - 100, stamp - 100))
        self.assertEqual(self.run_main(), 1)
        self.assertEqual(self.gate()["failures"], ["bitstream: the .bin is older than the .asc",
                                                   "bitstream: the .bin is older than the .tim"])

    def test_bitstream_size_and_sync(self):
        with open(self.path(".bin"), "wb") as f:
            f.write(bytes(100))
        self.assertEqual(self.run_main(), 1)
        failures = self.gate()["failures"]
        self.assertEqual(len([f for f in failures if f.startswith("bitstream:")]), 2)
        self.assertIn("versions: img.bin differs from the file tool-versions.json recorded", failures)

    def test_no_bitstream(self):
        os.remove(self.path(".bin"))
        self.assertEqual(self.run_main(), 1)
        self.assertEqual(self.gate()["failures"], ["bitstream: no .bin"])
        self.assertIsNone(self.gate()["bitstream_sha256"])

    def test_versions_file_required(self):
        os.remove(os.path.join(self.dir, check_image.VERSIONS_FILE))
        self.assertEqual(self.run_main(), 1)
        self.assertTrue(self.gate()["failures"][0].startswith("versions: no tool-versions.json"))

    def test_versions_tied_to_files(self):
        with open(self.path(".json")) as f:
            design = json.load(f)
        with open(self.path(".json"), "w") as f:
            json.dump(design, f, indent=1)
        self.assertEqual(self.run_main(), 1)
        self.assertEqual(self.gate()["failures"],
                         ["versions: img.json differs from the file tool-versions.json recorded"])

    def test_versions_without_image_record(self):
        with open(os.path.join(self.dir, check_image.VERSIONS_FILE), "w") as f:
            json.dump(VERSIONS, f)
        self.assertEqual(self.run_main(), 1)
        self.assertEqual(self.gate()["failures"], ["versions: tool-versions.json holds no record of img"])

    def test_gate_records_reference_and_flags(self):
        design = netlist()
        design["modules"]["top"]["netnames"]["extra_clk"] = {"bits": [43]}
        self.write(design)
        self.assertEqual(self.run_main("--allow-pll", "--extra-clock", "extra_clk:20"), 0)
        gate = self.gate()
        with open(os.path.join(self.dir, "ref.json"), "rb") as f:
            self.assertEqual(gate["reference"]["sha256"], hashlib.sha256(f.read()).hexdigest())
        self.assertEqual(gate["flags"], {"allow_pll": True, "extra_clocks": {"extra_clk": 20.0}})

    def test_write_reference_refuses_failing_image(self):
        design = netlist()
        design["modules"]["top"]["cells"]["i2c"] = {"type": "SB_I2C", "parameters": {},
                                                    "port_directions": {}, "connections": {}}
        self.write(design)
        os.remove(self.stock_path)
        self.assertEqual(check_image.main(["check_image.py", "--write-reference", self.dir, "img"]), 1)
        self.assertFalse(os.path.exists(self.stock_path))

    def test_write_reference_needs_force(self):
        with open(self.stock_path) as f:
            before = f.read()
        self.assertEqual(check_image.main(["check_image.py", "--write-reference", self.dir, "img"]), 2)
        with open(self.stock_path) as f:
            self.assertEqual(f.read(), before)
        self.assertEqual(check_image.main(["check_image.py", "--write-reference", "--force", self.dir, "img"]), 0)
        with open(self.stock_path) as f:
            self.assertEqual(json.load(f)["source"]["image"], "img")

    def test_write_reference_keeps_stock_entries(self):
        design = netlist()
        design["modules"]["top"]["cells"]["trig_in"] = io_cell(6, "out", (25, "1"), "011000")
        self.write(design)
        with open(self.stock_path) as f:
            before = f.read()
        self.assertEqual(check_image.main(["check_image.py", "--write-reference", "--force", self.dir, "img"]), 1)
        with open(self.stock_path) as f:
            self.assertEqual(f.read(), before)
        self.assertEqual(check_image.main(["check_image.py", self.dir, "img"]), 1)
        self.assertTrue(any(f.startswith("io: pin 48 is SB_IO") for f in self.gate()["failures"]))
        out = os.path.join(self.dir, "img.pack")
        self.assertEqual(pack.main(["pack.py", self.path(".bin"), out]), 1)
        self.assertFalse(os.path.exists(out))

    def test_write_reference_refreshes_clocks(self):
        timing = TIMING.replace("Derived frequency constraint of 48.0 MHz", "Derived frequency constraint of 50.0 MHz") \
                       .replace("(PASS at 48.00 MHz)", "(PASS at 50.00 MHz)")
        self.write(netlist(), timing=timing)
        self.assertEqual(check_image.main(["check_image.py", "--write-reference", "--force", self.dir, "img"]), 0)
        with open(self.stock_path) as f:
            self.assertEqual(json.load(f)["clocks"]["clk"], 50.0)

    def test_write_reference_without_stock_reference_uses_pack_hash(self):
        os.remove(self.stock_path)
        self.assertEqual(check_image.main(["check_image.py", "--write-reference", self.dir, "img"]), 1)
        self.assertFalse(os.path.exists(self.stock_path))

    def test_write_reference_writes_the_stock_reference_only(self):
        other = os.path.join(self.dir, "other-ref.json")
        for argv in (["--write-reference", other, self.dir, "img"],
                     ["--write-reference", "--force", "--reference", other, self.dir, "img"]):
            with self.subTest(argv=argv):
                with self.assertRaises(SystemExit):
                    check_image.main(["check_image.py", *argv])
                self.assertFalse(os.path.exists(other))


class StockEntriesTest(GateTest):

    def other_reference(self, ref):
        path = os.path.join(self.dir, "other-ref.json")
        with open(path, "w") as f:
            json.dump(ref, f)
        return path

    def run_against(self, ref_path):
        return check_image.main(["check_image.py", "--reference", ref_path, self.dir, "img"])

    def test_output_on_input_pin_fails_against_any_reference(self):
        design = netlist()
        design["modules"]["top"]["cells"]["trig_in"] = io_cell(6, "out", (25, "1"), "011000")
        self.write(design)
        own = self.other_reference(check_image.make_reference(self.dir, "img"))
        with open(own) as f:
            self.assertEqual(json.load(f)["io"]["48"]["parameters"]["PIN_TYPE"], "011000")
        out = os.path.join(self.dir, "img.pack")
        for ref_path in (self.stock_path, own):
            with self.subTest(reference=os.path.basename(ref_path)):
                self.assertEqual(self.run_against(ref_path), 1)
                self.assertTrue(any(f.startswith("io: pin 48 is SB_IO") for f in self.gate()["failures"]))
                self.assertEqual(pack.main(["pack.py", self.path(".bin"), out]), 1)
                self.assertFalse(os.path.exists(out))

    def test_other_reference_supplies_clocks_and_constraints_only(self):
        other = copy.deepcopy(self.ref)
        other["pins"] = dict(other["pins"], trigger_in_0__io="47")
        other["io"]["48"]["parameters"]["PIN_TYPE"] = "011000"
        other["spi"][0]["BUS_ADDR74"] = "0b0000"
        path = self.other_reference(other)
        self.assertEqual(self.run_against(path), 0)
        self.assertEqual(self.gate()["reference"]["path"], path)
        other["clocks"]["clk"] = 60.0
        self.other_reference(other)
        self.assertEqual(self.run_against(path), 1)
        self.assertEqual(self.gate()["failures"],
                         ["timing: clock 'clk' has target 48.00 MHz, below the stock 60.00 MHz"])


class RecordTest(GateTest):

    def record(self, *args):
        errors = io.StringIO()
        with mock.patch.object(record_versions, "first_line", side_effect=[VERSIONS["yosys"], VERSIONS["nextpnr"]]), \
                contextlib.redirect_stderr(errors), contextlib.redirect_stdout(io.StringIO()):
            status = record_versions.main(["record_versions.py", *args])
        return status, errors.getvalue()

    def versions_path(self):
        return os.path.join(self.dir, check_image.VERSIONS_FILE)

    def recorded(self):
        with open(self.versions_path()) as f:
            return json.load(f)

    def stamp(self):
        """Return a start stamp 100 s older than the image files."""
        path = os.path.join(self.dir, ".build-start")
        with open(path, "w"):
            pass
        then = time.time() - 100
        os.utime(path, (then, then))
        return path

    def test_record_holds_the_named_images_only(self):
        for ext in (".pcf", ".json", ".tim", ".ys", ".rpt", ".asc", ".bin"):
            shutil.copy2(self.path(ext), os.path.join(self.dir, "old" + ext))
        self.assertEqual(self.record("--since", self.stamp(), self.dir, "img")[0], 0)
        self.assertEqual(list(self.recorded()["images"]), ["img"])
        self.assertEqual(sorted(self.recorded()["images"]["img"]),
                         ["asc_sha256", "bin_sha256", "json_sha256", "pcf_sha256", "tim_sha256"])
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(check_image.main(["check_image.py", self.dir, "old"]), 1)
        with open(os.path.join(self.dir, "old.gate.json")) as f:
            self.assertEqual(json.load(f)["failures"], ["versions: tool-versions.json holds no record of old"])

    def test_record_refuses_files_older_than_the_start_stamp(self):
        stamp = os.path.join(self.dir, ".build-start")
        with open(stamp, "w"):
            pass
        now = time.time()
        os.utime(stamp, (now - 100, now - 100))
        os.utime(self.path(".pcf"), (now - 200, now - 200))
        os.remove(self.versions_path())
        status, errors = self.record("--since", stamp, self.dir, "img")
        self.assertEqual(status, 1)
        self.assertIn("img.pcf is older than the build's start stamp", errors)
        self.assertFalse(os.path.exists(self.versions_path()))
        os.utime(self.path(".pcf"), (now, now))
        self.assertEqual(self.record("--since", stamp, self.dir, "img")[0], 0)
        self.assertIsNotNone(self.recorded()["since"])

    def test_record_refuses_missing_file(self):
        os.remove(self.path(".tim"))
        os.remove(self.versions_path())
        status, errors = self.record("--since", self.stamp(), self.dir, "img")
        self.assertEqual(status, 1)
        self.assertIn("no img.tim", errors)
        self.assertFalse(os.path.exists(self.versions_path()))

    def test_record_needs_start_stamp(self):
        os.remove(self.versions_path())
        with self.assertRaises(SystemExit):
            self.record(self.dir, "img")
        with self.assertRaises(SystemExit):
            self.record("--since", os.path.join(self.dir, "no-stamp"), self.dir, "img")
        self.assertFalse(os.path.exists(self.versions_path()))

    def test_record_without_start_stamp_fails(self):
        for since in (None, ""):
            with self.subTest(since=since):
                recorded = self.recorded()
                recorded["since"] = since
                with open(self.versions_path(), "w") as f:
                    json.dump(recorded, f)
                self.assertEqual(self.run_main(), 1)
                self.assertEqual(self.gate()["failures"],
                                 ["versions: tool-versions.json has no build start stamp; record with --since"])

    def test_versions_cover_pcf_and_tim(self):
        for ext, line in ((".pcf", "# comment\n"), (".tim", "Info: done\n")):
            with self.subTest(ext=ext):
                self.write(netlist())
                with open(self.path(ext), "a") as f:
                    f.write(line)
                self.assertEqual(self.run_main(), 1)
                self.assertIn(f"versions: img{ext} differs from the file tool-versions.json recorded",
                              self.gate()["failures"])

    def test_record_without_pcf_and_tim_hashes(self):
        recorded = self.recorded()
        del recorded["images"]["img"]["pcf_sha256"], recorded["images"]["img"]["tim_sha256"]
        with open(self.versions_path(), "w") as f:
            json.dump(recorded, f)
        self.assertEqual(self.run_main(), 1)
        self.assertEqual(self.gate()["failures"], ["versions: tool-versions.json holds no SHA-256 of img.pcf",
                                                   "versions: tool-versions.json holds no SHA-256 of img.tim"])


# A gateware build that writes images 0 and 1, then fails as build.py does at a failing image.
FAKE_BUILD = "\n".join([
    "import os, sys",
    "for name in ('0_standard', '1_halfprec'):",
    "    for ext in ('.pcf', '.json', '.tim', '.asc', '.bin', '.ys', '.rpt'):",
    "        with open(os.path.join('build', name + ext), 'w') as f:",
    "            f.write(name + ext + ' new\\n')",
    "sys.exit('image 2 failed')",
    ""])


@unittest.skipUnless(os.path.exists(os.path.join(HERE, "..", "build-images.sh")),
                     "build-images.sh (the stock images' build) is not in this tree")
class WrapperTest(unittest.TestCase):

    def test_relative_venv_and_stale_images(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = os.path.join(tmp, "venv", "bin")
            os.makedirs(bin_dir)
            tools = {"python": f'exec "{sys.executable}" "$@"',
                     "yowasp-yosys": f"echo '{VERSIONS['yosys']}'",
                     "yowasp-nextpnr-ice40": f"echo '{VERSIONS['nextpnr']}'"}
            for name, body in tools.items():
                path = os.path.join(bin_dir, name)
                with open(path, "w") as f:
                    f.write(f"#!/bin/sh\n{body}\n")
                os.chmod(path, 0o755)
            gateware = os.path.join(tmp, "gateware")
            build = os.path.join(gateware, "build")
            os.makedirs(build)
            with open(os.path.join(gateware, "build.py"), "w") as f:
                f.write(FAKE_BUILD)
            for name in ("1_halfprec", "3_extprec_tx"):
                for ext in (".pcf", ".json", ".tim", ".asc", ".bin"):
                    with open(os.path.join(build, name + ext), "w") as f:
                        f.write("old\n")
            with open(os.path.join(build, check_image.VERSIONS_FILE), "w") as f:
                json.dump({"images": {"3_extprec_tx": {}}}, f)
            run = subprocess.run(["sh", os.path.join(HERE, "..", "build-images.sh"), "--venv", "venv", gateware],
                                 cwd=tmp, capture_output=True, text=True, timeout=120)
            self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
            with open(os.path.join(build, check_image.VERSIONS_FILE)) as f:
                record = json.load(f)
            self.assertEqual((record["yosys"], record["nextpnr"]), (VERSIONS["yosys"], VERSIONS["nextpnr"]))
            self.assertEqual(sorted(record["images"]), ["0_standard", "1_halfprec"])
            self.assertIsNotNone(record["since"])
            with open(os.path.join(build, "1_halfprec.pcf")) as f:
                self.assertEqual(f.read(), "1_halfprec.pcf new\n")
            self.assertFalse(os.path.exists(os.path.join(build, "3_extprec_tx.asc")))
            with open(os.path.join(build, "3_extprec_tx.gate.json")) as f:
                self.assertEqual(json.load(f)["verdict"], "FAIL")


if __name__ == "__main__":
    unittest.main()
