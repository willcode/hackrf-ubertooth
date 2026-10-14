#!/usr/bin/env python3
#
# SPDX-License-Identifier: BSD-3-Clause

"""Check a built gateware image against the stock standard image before it is loaded.

usage: check_image.py [--reference FILE] [--allow-pll] [--extra-clock NET:MHZ]...
                      <build dir> <image>
       check_image.py --write-reference [--force] <build dir> <image>

The build directory holds the files Amaranth writes for the image:
<image>.pcf, <image>.json (Yosys netlist), <image>.rpt (Yosys log),
<image>.tim (nextpnr log), <image>.ys, <image>.asc and <image>.bin. It also
holds tool-versions.json, which build-images.sh writes with
record_versions.py right after build.py. The file names the Yosys and
nextpnr versions and the SHA-256 of the .pcf, .json, .tim, .asc and .bin of
each image that build made. The checks are:

- pins: the .pcf assigns the same port names to the same pins as the
  reference;
- constraints: each stock set_frequency line is present, on the same clock
  net, at the stock frequency or higher;
- cells: every cell type in the netlist is on the allowed list.
  SB_RGBA_DRV, SB_RGB_DRV, SB_LEDDA_IP, SB_I2C, SB_WARMBOOT and the other
  current-sink drivers fail. With --allow-pll, SB_PLL40_CORE and
  SB_PLL40_2F_CORE pass; every other PLL fails, and the PAD variants also
  take a pin from its stock I/O cell;
- spi: exactly one SB_SPI, with the same core address and the same pins on
  each SPI port as the reference;
- io: each pin has the same I/O cell type, PIN_TYPE and I/O parameters as
  the reference, and no pin is added or missing;
- timing: the nextpnr log reports each post-route clock once, in a form the
  check parses. Each clock passes and its target equals its own constraint.
  Its name is in the reference, or its net is given with --extra-clock, and
  its target is not below the reference's or the flag's. A reference or
  extra clock missing from the report fails, unless its net clocks no cell
  outside the I/O cells;
- versions: tool-versions.json carries the start stamp of the build
  (record_versions.py --since) and records this image, and the SHA-256 of
  each of its .pcf, .json, .tim, .asc and .bin equals the recorded one;
- bitstream: the .bin is 104,090 bytes, carries the iCE40 sync word, and is
  not older than the .asc and the .tim.

The checks do not read the logic behind an output enable. An image that
drives the bidirectional SGPIO pins at the wrong time passes when their
PIN_TYPE matches the reference.

The result, the tool versions, the SHA-256 of the bitstream bytes checked,
the reference's path and SHA-256, and the flags used are written to
<image>.gate.json beside the image. An older gate file is
removed first, and the new one is written last, also when a check raises.
The exit status is 0 when every check passes and 1 otherwise.

The reference defaults to reference/standard.json beside this script, the
stock reference. Another reference given with --reference supplies the
clocks and set_frequency lines only. The pins, the I/O cells and the SPI
core always come from the stock reference.

--write-reference regenerates the stock reference from a stock standard
build. It refuses to replace the existing file without --force. It refuses
an image that fails the cell checks, and an image whose pins, I/O cells or
SPI core differ from the existing stock reference, or, with no stock
reference present, from the hash pack.py holds. Only the clocks, the
set_frequency lines and the source record change.
"""

import argparse
import collections
import hashlib
import json
import os
import re
import sys

# Cell types an image may contain. SB_SPI and SB_PLL40* have their own checks.
ALLOWED_CELLS = {
    "SB_LUT4", "SB_CARRY",
    "SB_DFF", "SB_DFFE", "SB_DFFSR", "SB_DFFR", "SB_DFFSS", "SB_DFFS",
    "SB_DFFESR", "SB_DFFER", "SB_DFFESS", "SB_DFFES",
    "SB_DFFN", "SB_DFFNE", "SB_DFFNSR", "SB_DFFNR", "SB_DFFNSS", "SB_DFFNS",
    "SB_DFFNESR", "SB_DFFNER", "SB_DFFNESS", "SB_DFFNES",
    "SB_RAM40_4K", "SB_RAM40_4KNR", "SB_RAM40_4KNW", "SB_RAM40_4KNRNW",
    "SB_SPRAM256KA", "SB_MAC16",
    "SB_IO", "SB_GB_IO", "SB_GB",
    "SB_HFOSC", "SB_LFOSC",
}

# Cell types that fail outright, with the reason printed.
FORBIDDEN_CELLS = {
    "SB_RGBA_DRV": "drives pins 39 to 41, which carry ADC data on this board",
    "SB_RGB_DRV": "drives pins 39 to 41, which carry ADC data on this board",
    "SB_LEDDA_IP": "LED controller for the RGB drivers",
    "SB_LED_DRV_CUR": "current reference for the RGB drivers",
    "SB_IR_DRV": "current-sink driver",
    "SB_IR400_DRV": "current-sink driver",
    "SB_IR500_DRV": "current-sink driver",
    "SB_BARCODE_DRV": "current-sink driver",
    "SB_I2C": "hard I2C core on configuration and SGPIO pins",
    "SB_WARMBOOT": "triggers a reconfiguration of the FPGA",
}

# PLLs that --allow-pll admits: the variants fed from fabric, which take no pin.
PLL_CORE = {"SB_PLL40_CORE", "SB_PLL40_2F_CORE"}

SPI_PORTS = {"SO": "out", "SOE": "out", "SI": "in", "SCKI": "in", "SCSNI": "in"}
IO_CELLS = ("SB_IO", "SB_GB_IO")
IO_PIN_PORTS = {"D_OUT_0", "D_OUT_1", "OUTPUT_ENABLE", "D_IN_0", "D_IN_1",
                "GLOBAL_BUFFER_OUTPUT"}
CLOCK_PORTS = {"C", "CLK", "CLOCK", "RCLK", "RCLKN", "WCLK", "WCLKN", "SBCLKI", "INPUT_CLK", "OUTPUT_CLK"}

IMAGE_SIZE = 104090
SYNC_WORD = b"\x7e\xaa\x99\x7e"
VERSIONS_FILE = "tool-versions.json"
STOCK_REFERENCE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reference", "standard.json")

# The image files tool-versions.json hashes; a failed route writes no .bin.
HASHED_FILES = (".pcf", ".json", ".tim", ".asc", ".bin")

# Reference entries that always come from the stock reference.
STOCK_ENTRIES = ("pins", "io", "spi")

# nextpnr names a clock on a global buffer after its net with this suffix.
GLOBAL_SUFFIX = "_$glb_clk"

FREQ_LINE = re.compile(r"Max frequency for clock\s+'([^']+)': ([0-9.]+) MHz \((PASS|FAIL) at ([0-9.]+) MHz\)$")
SET_LINE = re.compile(r"constraining clock net '([^']+)' to ([0-9.]+) MHz")
DERIVED_LINE = re.compile(r"Derived frequency constraint of ([0-9.]+) MHz for net (\S+)")


def same_mhz(a, b):
    """Compare frequencies on the 0.1 MHz grid of nextpnr's coarser printed form."""
    return round(a, 1) == round(b, 1)


def read_pcf(path):
    pins, freqs = {}, []
    with open(path) as f:
        lines = f.readlines()
    for line in lines:
        parts = line.split()
        if parts[:1] == ["set_io"]:
            pins[parts[-2]] = parts[-1]
        elif parts[:1] == ["set_frequency"]:
            freqs.append((parts[1], float(parts[2])))
    return pins, freqs


def top_module(path):
    with open(path) as f:
        design = json.load(f)
    for module in design["modules"].values():
        if module.get("attributes", {}).get("top"):
            return module
    raise ValueError(f"{path}: no top module")


def bit_names(module):
    """Map each top-level port bit to its .pcf name."""
    names = {}
    for name, port in module["ports"].items():
        bits = port["bits"]
        for i, bit in enumerate(bits):
            names[bit] = f"{name}[{i}]" if len(bits) > 1 else name
    return names


def net_bits(module, name):
    """Return the bits of a net or top-level port, or None."""
    if name in module.get("netnames", {}):
        return module["netnames"][name]["bits"]
    if name in module["ports"]:
        return module["ports"][name]["bits"]
    return None


def io_cells(module, pins):
    """Return {pin: (cell name, cell type, parameters, port name)} for every I/O cell."""
    names = bit_names(module)
    out = {}
    for cell_name, cell in module["cells"].items():
        if cell["type"] not in IO_CELLS:
            continue
        for bit in cell["connections"].get("PACKAGE_PIN", []):
            port = names.get(bit)
            pin = pins.get(port, f"unassigned:{port}")
            out[pin] = (cell_name, cell["type"], dict(cell.get("parameters", {})), port)
    return out


def net_maps(module):
    drivers, sinks = {}, collections.defaultdict(list)
    for cell_name, cell in module["cells"].items():
        directions = cell.get("port_directions", {})
        for port, bits in cell["connections"].items():
            for bit in bits:
                if not isinstance(bit, int):
                    continue
                if directions.get(port) == "output":
                    drivers[bit] = (cell_name, port)
                else:
                    sinks[bit].append((cell_name, port))
    return drivers, sinks


def spi_pins(module, pins, cell):
    """Return {SPI port: sorted pins} for one SB_SPI, following at most two LUTs."""
    cells = module["cells"]
    pin_of = {name: pin for pin, (name, _, _, _) in io_cells(module, pins).items()}
    drivers, sinks = net_maps(module)
    result = {}
    for port, direction in SPI_PORTS.items():
        found = set()
        frontier = [b for b in cell["connections"].get(port, []) if isinstance(b, int)]
        for _ in range(3):
            following = []
            for bit in frontier:
                ends = sinks.get(bit, []) if direction == "out" else [drivers[bit]] if bit in drivers else []
                for name, cell_port in ends:
                    if name in pin_of and cell_port in IO_PIN_PORTS:
                        found.add(pin_of[name])
                    elif cells[name]["type"] == "SB_LUT4":
                        conns = cells[name]["connections"]
                        if direction == "out" and cell_port.startswith("I"):
                            following += [b for b in conns["O"] if isinstance(b, int)]
                        elif direction == "in" and cell_port == "O":
                            following += [b for p in ("I0", "I1", "I2", "I3")
                                          for b in conns.get(p, []) if isinstance(b, int)]
            frontier = following
        result[port] = sorted(found)
    return result


def fabric_clock_loads(module, net):
    """Count the cells outside the I/O cells whose clock input is on the net; None if the net is absent."""
    bits = net_bits(module, net)
    if bits is None:
        return None
    count = 0
    for cell in module["cells"].values():
        if cell["type"] in IO_CELLS:
            continue
        if any(set(cell["connections"].get(p, [])) & set(bits) for p in CLOCK_PORTS):
            count += 1
    return count


def canonical_net(module, name, clock_nets):
    """Name a constrained net by the clock net it aliases, else by itself."""
    bits = net_bits(module, name)
    for net in clock_nets:
        if bits is not None and net_bits(module, net) == bits:
            return net
    return name


def describe(build_dir, image):
    """Read the build files into the facts the checks compare."""
    base = os.path.join(build_dir, image)
    pins, freqs = read_pcf(base + ".pcf")
    module = top_module(base + ".json")
    types = collections.Counter(c["type"] for c in module["cells"].values() if c["type"] != "$scopeinfo")
    io = {pin: {"cell": t, "port": port, "parameters": params}
          for pin, (_, t, params, port) in io_cells(module, pins).items()}
    spi = [{"BUS_ADDR74": c.get("parameters", {}).get("BUS_ADDR74"), "pins": spi_pins(module, pins, c)}
           for c in module["cells"].values() if c["type"] == "SB_SPI"]
    return {"pins": pins, "set_frequency": freqs, "cell_types": dict(sorted(types.items())),
            "io": dict(sorted(io.items(), key=lambda kv: kv[0])), "spi": spi, "module": module}


def read_timing(tim_path):
    """Parse a nextpnr log: (failures, post-route clocks, applied constraints, derived constraints)."""
    with open(tim_path) as f:
        lines = f.read().splitlines()
    applied = [(m.group(1), float(m.group(2))) for line in lines for m in [SET_LINE.search(line)] if m]
    derived = {m.group(2): float(m.group(1)) for line in lines for m in [DERIVED_LINE.search(line)] if m}
    routed = [i for i, line in enumerate(lines) if "Routing complete" in line]
    if not routed:
        return ["timing: the log has no completed route"], {}, applied, derived
    failures, clocks = [], {}
    for line in lines[routed[-1]:]:
        if "Max frequency for clock" not in line:
            continue
        m = FREQ_LINE.search(line.rstrip())
        if not m:
            failures.append(f"timing: unparsed clock line: {line.strip()}")
        elif m.group(1) in clocks:
            failures.append(f"timing: clock '{m.group(1)}' is reported twice after routing")
        else:
            clocks[m.group(1)] = (float(m.group(2)), m.group(3), float(m.group(4)))
    if not clocks:
        failures.append("timing: no post-route clock reports a constraint")
    return failures, clocks, applied, derived


def clock_net(name):
    """Return the net of a clock as nextpnr names it in its report."""
    return name[:-len(GLOBAL_SUFFIX)] if name.endswith(GLOBAL_SUFFIX) else name


def check_timing(timing, module, ref, extra_clocks=None):
    """Return (failures, report lines) for the post-route clocks against their constraints and the reference.

    extra_clocks maps the net of each clock the reference lacks to its lowest allowed target in MHz.
    """
    failures, clocks, applied, derived = timing
    failures = list(failures)
    extra_clocks = extra_clocks or {}
    ref_clocks = ref["clocks"]
    stock_nets = {clock_net(name) for name in ref_clocks}
    # nextpnr names a clock after its net, with a suffix when it inserted the global buffer
    # itself; the same net is the same stock clock either way.
    stock_by_net = {clock_net(name): name for name in ref_clocks}
    clock_nets = sorted({clock_net(name) for name in set(ref_clocks) | set(clocks)} | set(extra_clocks))
    own = dict(derived)
    for name, freq in applied:
        own[canonical_net(module, name, clock_nets)] = freq
    report = []
    for name, (fmax, verdict, target) in clocks.items():
        report.append(f"{name}: {fmax:.2f} MHz, {verdict} at {target:.2f} MHz")
        net = name[:-len(GLOBAL_SUFFIX)] if name.endswith(GLOBAL_SUFFIX) else name
        if verdict != "PASS":
            failures.append(f"timing: clock '{name}' reaches {fmax:.2f} MHz against {target:.2f} MHz")
        if net not in own:
            failures.append(f"timing: clock '{name}' has target {target:.2f} MHz and no constraint on net '{net}'")
        elif not same_mhz(own[net], target):
            failures.append(f"timing: clock '{name}' has target {target:.2f} MHz, its constraint is "
                            f"{own[net]:.2f} MHz")
        if name in ref_clocks or net in stock_by_net:
            stock = name if name in ref_clocks else stock_by_net[net]
            if stock != name:
                report.append(f"{name}: the stock clock '{stock}', named without the global-buffer suffix")
            if round(target, 1) < round(ref_clocks[stock], 1):
                failures.append(f"timing: clock '{name}' has target {target:.2f} MHz, below the stock "
                                f"{ref_clocks[stock]:.2f} MHz")
        elif net in extra_clocks and net not in stock_nets:
            if round(target, 1) < round(extra_clocks[net], 1):
                failures.append(f"timing: clock '{name}' has target {target:.2f} MHz, below the "
                                f"--extra-clock {extra_clocks[net]:.2f} MHz")
        else:
            failures.append(f"timing: clock '{name}' is not in the reference; declare it with --extra-clock")
    reported = {clock_net(name) for name in clocks}
    expected = [(name, "stock clock") for name in sorted(ref_clocks)] + \
               [(net, "extra clock") for net in sorted(set(extra_clocks) - stock_nets)]
    for name, kind in expected:
        net = clock_net(name)
        if net in reported:
            continue
        loads = fabric_clock_loads(module, net)
        if loads == 0:
            report.append(f"{name}: not reported; net '{net}' clocks no cell outside the I/O cells")
        elif loads is None:
            failures.append(f"timing: {kind} '{name}' is not reported and net '{net}' is not in the netlist")
        else:
            failures.append(f"timing: {kind} '{name}' is not reported and net '{net}' clocks {loads} cells")
    return failures, report


def check_constraints(facts, ref):
    """Fail when a stock set_frequency line is missing or lower."""
    module = facts["module"]
    clock_nets = [name[:-len(GLOBAL_SUFFIX)] if name.endswith(GLOBAL_SUFFIX) else name for name in ref["clocks"]]
    have = {}
    for name, freq in facts["set_frequency"]:
        net = canonical_net(module, name, clock_nets)
        have[net] = min(freq, have.get(net, freq))
    failures = []
    for net, freq in sorted(ref["set_frequency"].items()):
        if net not in have:
            failures.append(f"constraints: no set_frequency on net '{net}', stock {freq} MHz")
        elif round(have[net], 1) < round(freq, 1):
            failures.append(f"constraints: set_frequency on net '{net}' is {have[net]} MHz, stock {freq} MHz")
    return failures


def check_cells(facts, allow_pll):
    """Fail on a cell type off the allowed list, and on any SB_SPI count but one."""
    failures = []
    for cell_type, count in facts["cell_types"].items():
        if cell_type in FORBIDDEN_CELLS:
            failures.append(f"cells: {count} x {cell_type}: {FORBIDDEN_CELLS[cell_type]}")
        elif cell_type.startswith("SB_PLL40"):
            if not allow_pll:
                failures.append(f"cells: {count} x {cell_type} without --allow-pll")
            elif cell_type not in PLL_CORE:
                failures.append(f"cells: {count} x {cell_type}: --allow-pll admits only "
                                f"{' and '.join(sorted(PLL_CORE))}")
        elif cell_type != "SB_SPI" and cell_type not in ALLOWED_CELLS:
            failures.append(f"cells: {count} x {cell_type} is not on the allowed list")
    if len(facts["spi"]) != 1:
        failures.append(f"spi: {len(facts['spi'])} SB_SPI cells, stock has 1")
    return failures


def check(facts, ref, allow_pll):
    failures = []
    if facts["pins"] != ref["pins"]:
        for name in sorted(set(facts["pins"]) | set(ref["pins"])):
            got, want = facts["pins"].get(name), ref["pins"].get(name)
            if got != want:
                failures.append(f"pins: '{name}' on pin {got}, stock on pin {want}")
    failures += check_cells(facts, allow_pll)
    if len(facts["spi"]) == 1 and facts["spi"] != ref["spi"]:
        failures.append(f"spi: SB_SPI {facts['spi'][0]} differs from stock {ref['spi'][0]}")
    for pin in sorted(set(facts["io"]) | set(ref["io"]), key=str):
        got, want = facts["io"].get(pin), ref["io"].get(pin)
        if got is None:
            failures.append(f"io: pin {pin} has no I/O cell, stock has {want['cell']}")
        elif want is None:
            failures.append(f"io: pin {pin} has an I/O cell, stock has none")
        elif (got["cell"], got["parameters"]) != (want["cell"], want["parameters"]):
            failures.append(f"io: pin {pin} is {got['cell']} {got['parameters']}, "
                            f"stock {want['cell']} {want['parameters']}")
    return failures


def read_bitstream(build_dir, image):
    """Return (bytes or None, failures) for the image's bitstream."""
    base = os.path.join(build_dir, image)
    path = base + ".bin"
    if not os.path.exists(path):
        return None, ["bitstream: no .bin"]
    with open(path, "rb") as f:
        data = f.read()
    failures = []
    if len(data) != IMAGE_SIZE:
        failures.append(f"bitstream: {len(data)} bytes, an iCE40UP5K image is {IMAGE_SIZE}")
    if SYNC_WORD not in data[:64]:
        failures.append("bitstream: no iCE40 sync word in the first 64 bytes")
    for ext in (".asc", ".tim"):
        if os.path.exists(base + ext) and os.path.getmtime(path) < os.path.getmtime(base + ext):
            failures.append(f"bitstream: the .bin is older than the {ext}")
    return data, failures


def tool_versions(build_dir, image):
    """Return (versions, failures) from the .ys, the .rpt and tool-versions.json.

    tool-versions.json must hold the SHA-256 of this image's .pcf, .json, .tim and .asc, and of its .bin when
    a .bin exists, equal to the files in the directory.
    """
    versions, failures = {}, []
    with open(os.path.join(build_dir, image + ".ys")) as f:
        m = re.search(r"generated by (Amaranth [0-9][^ ]*?)\.? ", f.read(), re.IGNORECASE)
        versions["amaranth"] = m.group(1) if m else None
    with open(os.path.join(build_dir, image + ".rpt")) as f:
        m = re.search(r"^ *(Yosys \d.*)$", f.read(), re.MULTILINE)
        versions["yosys"] = m.group(1).strip() if m else None
    path = os.path.join(build_dir, VERSIONS_FILE)
    if not os.path.exists(path):
        failures.append(f"versions: no {VERSIONS_FILE} in the build directory; run record_versions.py")
        versions["nextpnr"] = None
        return versions, failures
    with open(path) as f:
        recorded = json.load(f)
    versions["nextpnr"] = recorded.get("nextpnr")
    versions["recorded"] = recorded
    if not versions["nextpnr"]:
        failures.append(f"versions: {VERSIONS_FILE} names no nextpnr version")
    if not recorded.get("since"):
        failures.append(f"versions: {VERSIONS_FILE} has no build start stamp; record with --since")
    if versions["yosys"] and recorded.get("yosys") and not versions["yosys"].startswith(recorded["yosys"]):
        failures.append(f"versions: the .rpt names {versions['yosys']}, {VERSIONS_FILE} names {recorded['yosys']}")
    entry = recorded.get("images", {}).get(image)
    if entry is None:
        failures.append(f"versions: {VERSIONS_FILE} holds no record of {image}")
        return versions, failures
    for ext in HASHED_FILES:
        key = ext[1:] + "_sha256"
        path = os.path.join(build_dir, image + ext)
        if not os.path.exists(path):
            if ext != ".bin":
                failures.append(f"versions: no {image}{ext} to compare with {VERSIONS_FILE}")
            continue
        if key not in entry:
            failures.append(f"versions: {VERSIONS_FILE} holds no SHA-256 of {image}{ext}")
        elif entry[key] != file_sha256(path):
            failures.append(f"versions: {image}{ext} differs from the file {VERSIONS_FILE} recorded")
    return versions, failures


def file_sha256(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def load_reference(path):
    """Return the reference at path, with its pins, I/O cells and SPI core taken from the stock reference."""
    with open(path) as f:
        ref = json.load(f)
    with open(STOCK_REFERENCE) as f:
        stock = json.load(f)
    ref.update({key: stock[key] for key in STOCK_ENTRIES})
    return ref


def check_stock_entries(facts):
    """Fail when the image's pins, I/O cells or SPI core differ from the stock reference's."""
    if os.path.exists(STOCK_REFERENCE):
        with open(STOCK_REFERENCE) as f:
            stock = json.load(f)
        return check(facts, stock, allow_pll=False)
    # pack.py needs lz4, which the other checks do not.
    import pack
    if pack.entries_sha256(facts) != pack.STOCK_ENTRIES_SHA256:
        return ["stock: the pins, I/O cells or SPI core differ from the hash pack.py holds"]
    return []


def make_reference(build_dir, image):
    """Return the reference recorded from a stock build."""
    facts = describe(build_dir, image)
    _, clocks, applied, _ = read_timing(os.path.join(build_dir, image + ".tim"))
    clock_nets = [name[:-len(GLOBAL_SUFFIX)] if name.endswith(GLOBAL_SUFFIX) else name for name in clocks]
    set_frequency = {canonical_net(facts["module"], name, clock_nets): freq for name, freq in facts["set_frequency"]}
    data, _ = read_bitstream(build_dir, image)
    versions, _ = tool_versions(build_dir, image)
    return {
        "source": {"image": image, "bitstream_sha256": hashlib.sha256(data).hexdigest() if data else None,
                   "tools": versions},
        "pins": facts["pins"], "io": facts["io"], "spi": facts["spi"],
        "set_frequency": set_frequency,
        "clocks": {name: target for name, (_, _, target) in clocks.items()},
    }


def main(argv):
    parser = argparse.ArgumentParser(description="Check a built gateware image before it is loaded.")
    parser.add_argument("build_dir")
    parser.add_argument("image")
    parser.add_argument("--reference", help="reference for the clocks and set_frequency lines; "
                                            "default the stock reference")
    parser.add_argument("--write-reference", action="store_true",
                        help="regenerate the stock reference from this image")
    parser.add_argument("--force", action="store_true", help="let --write-reference replace the stock reference")
    parser.add_argument("--allow-pll", action="store_true")
    parser.add_argument("--extra-clock", action="append", default=[], metavar="NET:MHZ",
                        help="admit a clock the reference lacks, at this target or higher")
    args = parser.parse_args(argv[1:])
    extra_clocks = {}
    for item in args.extra_clock:
        net, sep, mhz = item.rpartition(":")
        try:
            extra_clocks[net] = float(mhz)
        except ValueError:
            net = ""
        if not sep or not net:
            parser.error(f"--extra-clock takes NET:MHZ, not {item}")

    if args.write_reference:
        if args.reference:
            parser.error("--write-reference writes the stock reference and takes no --reference")
        if os.path.exists(STOCK_REFERENCE) and not args.force:
            sys.stderr.write(f"check_image.py: {STOCK_REFERENCE} exists; add --force to replace it\n")
            return 2
        facts = describe(args.build_dir, args.image)
        refused = check_cells(facts, allow_pll=False) or check_stock_entries(facts)
        if refused:
            for failure in refused:
                sys.stderr.write(f"check_image.py: FAIL {failure}\n")
            sys.stderr.write(f"check_image.py: no reference written from {args.image}\n")
            return 1
        ref = make_reference(args.build_dir, args.image)
        with open(STOCK_REFERENCE, "w") as f:
            json.dump(ref, f, indent=1, sort_keys=True)
            f.write("\n")
        print(f"wrote {STOCK_REFERENCE} from {args.build_dir}/{args.image}")
        return 0

    gate_path = os.path.join(args.build_dir, args.image + ".gate.json")
    if os.path.exists(gate_path):
        os.remove(gate_path)
    failures, timing_report, versions, data, cell_types, set_frequency = [], [], {}, None, {}, []
    reference_path = args.reference or STOCK_REFERENCE
    reference = {"path": os.path.abspath(reference_path), "sha256": None}
    try:
        reference["sha256"] = file_sha256(reference_path)
        ref = load_reference(reference_path)
        facts = describe(args.build_dir, args.image)
        cell_types, set_frequency = facts["cell_types"], facts["set_frequency"]
        failures += check(facts, ref, args.allow_pll)
        failures += check_constraints(facts, ref)
        timing = read_timing(os.path.join(args.build_dir, args.image + ".tim"))
        timing_failures, timing_report = check_timing(timing, facts["module"], ref, extra_clocks)
        failures += timing_failures
        versions, version_failures = tool_versions(args.build_dir, args.image)
        failures += version_failures
        data, bitstream_failures = read_bitstream(args.build_dir, args.image)
        failures += bitstream_failures
    except Exception as e:
        failures.append(f"error: {type(e).__name__}: {e}")
    result = {
        "image": args.image,
        "verdict": "FAIL" if failures else "PASS",
        "failures": failures,
        "bitstream_sha256": hashlib.sha256(data).hexdigest() if data is not None else None,
        "reference": reference,
        "flags": {"allow_pll": args.allow_pll, "extra_clocks": extra_clocks},
        "tools": versions,
        "cell_types": cell_types,
        "post_route_clocks": timing_report,
        "set_frequency": [f"{name} {freq}" for name, freq in set_frequency],
    }

    print(f"{args.image}: {result['verdict']}")
    print("  cells: " + ", ".join(f"{t} {n}" for t, n in cell_types.items()))
    for line in timing_report:
        print(f"  clock {line}")
    for failure in failures:
        print(f"  FAIL {failure}")
    print(f"  bitstream sha256: {result['bitstream_sha256'] or 'none'}")
    print(f"  reference: {reference['path']} sha256 {reference['sha256']}")
    if args.allow_pll or extra_clocks:
        print(f"  flags: allow_pll {args.allow_pll}, extra clocks {extra_clocks}")
    for tool in ("amaranth", "yosys", "nextpnr"):
        print(f"  {tool}: {versions.get(tool)}")
    with open(gate_path, "w") as f:
        json.dump(result, f, indent=1)
        f.write("\n")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
