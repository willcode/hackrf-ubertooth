#!/usr/bin/env python3
#
# SPDX-License-Identifier: BSD-3-Clause

"""Record a build's tool versions and image hashes in <build dir>/tool-versions.json.

usage: record_versions.py --since FILE <build dir> <image>...

Touch FILE before the build. Run this script in the environment that ran
the build, right after the build, and name the images that build made.
build-images.sh does this around build.py. It runs the commands named by
$YOSYS and $NEXTPNR_ICE40, as the build does, defaulting to yosys and
nextpnr-ice40.

For each named image it records the SHA-256 of the .pcf, the .json, the
.tim, the .asc and, when present, the .bin. It refuses an image with one of
the first four files missing, or with one of its files older than FILE. On
a refusal it writes nothing and exits 1. The record carries FILE's time as
its start stamp.

The file it writes replaces any earlier one and holds only the named
images. check_image.py fails a record with no start stamp, an image with no
record, and an image whose files differ from their recorded hashes.
"""

import argparse
import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys

# Files each image must have, then the bitstream, which a failed route does not write.
REQUIRED = (".pcf", ".json", ".tim", ".asc")
OPTIONAL = (".bin",)


def first_line(command, flag, marker):
    out = subprocess.run([command, flag], capture_output=True, text=True, timeout=600, check=True)
    lines = (out.stdout + out.stderr).splitlines()
    return next((line.strip() for line in lines if marker in line), None)


def file_sha256(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def image_hashes(build_dir, images, since_ns=None):
    """Return ({image: {"<ext>_sha256": hex}}, refusals) for the named images."""
    records, refusals = {}, []
    for image in images:
        base = os.path.join(build_dir, image)
        entry = {}
        for ext in REQUIRED + OPTIONAL:
            path = base + ext
            if not os.path.exists(path):
                if ext in REQUIRED:
                    refusals.append(f"{image}: no {image}{ext}")
                continue
            if since_ns is not None and os.stat(path).st_mtime_ns < since_ns:
                refusals.append(f"{image}: {image}{ext} is older than the build's start stamp")
                continue
            entry[ext[1:] + "_sha256"] = file_sha256(path)
        records[image] = entry
    return records, refusals


def main(argv):
    parser = argparse.ArgumentParser(description="Record a build's tool versions and image hashes.")
    parser.add_argument("--since", metavar="FILE", required=True,
                        help="stamp touched before the build; refuse image files older than it")
    parser.add_argument("build_dir")
    parser.add_argument("images", nargs="+", metavar="image")
    args = parser.parse_args(argv[1:])
    if not os.path.exists(args.since):
        parser.error(f"--since {args.since}: no such file; touch it before the build")
    since_ns = os.stat(args.since).st_mtime_ns
    images, refusals = image_hashes(args.build_dir, args.images, since_ns)
    if refusals:
        for refusal in refusals:
            sys.stderr.write(f"record_versions.py: refused: {refusal}\n")
        sys.stderr.write("record_versions.py: no record written\n")
        return 1
    yosys = os.environ.get("YOSYS", "yosys")
    nextpnr = os.environ.get("NEXTPNR_ICE40", "nextpnr-ice40")
    record = {
        "yosys": first_line(yosys, "-V", "Yosys"),
        "yosys_command": os.path.basename(shutil.which(yosys) or yosys),  # no path of this machine
        "nextpnr": first_line(nextpnr, "--version", "Version"),
        "nextpnr_command": os.path.basename(shutil.which(nextpnr) or nextpnr),
        "recorded": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "since": datetime.datetime.fromtimestamp(since_ns / 1e9).astimezone().isoformat(timespec="seconds"),
        "images": images,
    }
    with open(os.path.join(args.build_dir, "tool-versions.json"), "w") as f:
        json.dump(record, f, indent=1)
        f.write("\n")
    print(json.dumps({k: v for k, v in record.items() if k != "images"}, indent=1))
    print("images: " + ", ".join(record["images"]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
