#!/bin/sh
# SPDX-License-Identifier: BSD-3-Clause
#
# Build everything from committed source into a new directory: the HackRF firmware, libhackrf,
# libbtbb, the Ubertooth host tools (with the HackRF Pro backend), the Ubertooth firmware and the BR
# gateware image, then compare the results with the committed ones.
#
# usage: build.sh [--venv DIR] [--tests] [--only STEPS] OUT
#
# OUT must be empty or absent. Nothing outside OUT is written. The sources are a clone of this
# repository at HEAD (uncommitted changes are not built), with its submodules taken from this
# checkout:
#   OUT/src                  the clone
#   OUT/build/firmware       hackrf_usb.bin (flash image) and hackrf_usb_fpga.dfu (RAM firmware)
#   OUT/build/ubertooth-fw   bluetooth_rxtx.dfu, the Ubertooth One firmware
#   OUT/build/gateware       br_rx.pack and its build directory
#   OUT/install              libhackrf, libbtbb and the Ubertooth host tools (bin/, lib/, share/)
#   OUT/logs                 one log per step
#
# STEPS is a comma-separated list of firmware, host, libbtbb, ubertooth, ubertooth-fw and gateware
# (default: all). --venv uses an existing gateware environment instead of making OUT/venv from
# fpga-build/requirements-lock.txt, which needs network access. --tests also runs the test suites:
# gateware, BR models, libbtbb, libubertooth, the Ubertooth firmware's native tests, and an end-to-end
# check (synthetic I/Q through the bit-exact model into ubertooth-rx). The HackRF firmware is
# compared with prebuilt/SHA256SUMS, and the pack with the committed fpga-build/packs/br_rx.pack.
set -eu

here=$(cd "$(dirname "$0")" && pwd)
venv= tests= steps=firmware,host,libbtbb,ubertooth,ubertooth-fw,gateware
while [ $# -gt 1 ]; do
    case $1 in
        --venv) venv=$(cd "$2" && pwd); shift 2 ;;
        --tests) tests=1; shift ;;
        --only) steps=$2; shift 2 ;;
        *) break ;;
    esac
done
[ $# -eq 1 ] || { sed -n '8p' "$0" | sed 's/^# //'; exit 2; }
mkdir -p "$1"
out=$(cd "$1" && pwd)
[ -z "$(ls -A "$out")" ] || { echo "build.sh: $out is not empty"; exit 2; }
want() { case ",$steps," in *",$1,"*) return 0 ;; esac; return 1; }

src=$out/src build=$out/build logs=$out/logs inst=$out/install
mkdir -p "$build" "$logs"
step() { echo "== $1 (log: $logs/$2.log)"; }
fail() { echo "build.sh: $1 failed; see $logs/$2.log"; tail -5 "$logs/$2.log"; exit 1; }
# Compare a built file with its line in prebuilt/SHA256SUMS.
check() {
    want_sum=$(awk -v f="$2" '$2 == f { print $1 }' "$src/prebuilt/SHA256SUMS")
    got_sum=$(sha256sum "$1" | cut -d' ' -f1)
    echo "   $got_sum  $2"
    if [ "$want_sum" = "$got_sum" ]; then
        echo "   $2 is identical to prebuilt/$2"
    else
        echo "   $2 differs from prebuilt/$2"
    fi
}

step "sources" sources
for sm in firmware/hackrf ubertooth libbtbb; do
    [ -e "$here/$sm/.git" ] || {
        echo "build.sh: $sm is not checked out; run: git submodule update --init --recursive"; exit 2; }
done
fw=$here/firmware/hackrf
{
    git clone "$here" "$src" &&
    git -C "$src" submodule init &&
    for sm in firmware/hackrf ubertooth libbtbb; do
        git -C "$src" config "submodule.$sm.url" "$here/$sm" || exit 1
    done &&
    git -C "$src" -c protocol.file.allow=always submodule update &&
    if [ -e "$fw/firmware/libopencm3/.git" ]; then
        git -C "$src/firmware/hackrf" submodule init firmware/libopencm3 &&
        git -C "$src/firmware/hackrf" config submodule.firmware/libopencm3.url "$fw/firmware/libopencm3"
    fi &&
    git -C "$src/firmware/hackrf" -c protocol.file.allow=always submodule update --init firmware/libopencm3
} >"$logs/sources.log" 2>&1 || fail sources sources
echo "   $(git -C "$src" rev-parse --short HEAD), firmware $(git -C "$src/firmware/hackrf" describe --always --dirty)," \
     "ubertooth $(git -C "$src/ubertooth" rev-parse --short HEAD), libbtbb $(git -C "$src/libbtbb" rev-parse --short HEAD)"

export PKG_CONFIG_PATH="$inst/lib/pkgconfig:$inst/lib64/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"

if want firmware; then
    step "HackRF firmware" firmware
    {
        cmake -S "$src/firmware/hackrf/firmware/hackrf_usb" -B "$build/firmware" -DBOARD=PRALINE &&
        cmake --build "$build/firmware" -j6 --target hackrf_usb.bin hackrf_usb_fpga.dfu
    } >"$logs/firmware.log" 2>&1 || fail "HackRF firmware" firmware
    check "$build/firmware/hackrf_usb.bin" hackrf_usb.bin
    check "$build/firmware/hackrf_usb_fpga.dfu" hackrf_usb_fpga.dfu
fi

if want host; then
    step "libhackrf and the hackrf tools" host
    {
        cmake -S "$src/firmware/hackrf/host" -B "$build/host" -DCMAKE_INSTALL_PREFIX="$inst" \
            -DINSTALL_UDEV_RULES=OFF &&
        cmake --build "$build/host" -j6 &&
        cmake --install "$build/host"
    } >"$logs/host.log" 2>&1 || fail "libhackrf" host
fi

if want libbtbb; then
    step "libbtbb" libbtbb
    {
        cmake -S "$src/libbtbb" -B "$build/libbtbb" -DCMAKE_INSTALL_PREFIX="$inst" \
            -DCMAKE_INSTALL_LIBDIR=lib -DENABLE_PYTHON=OFF &&
        cmake --build "$build/libbtbb" -j6 &&
        cmake --install "$build/libbtbb"
    } >"$logs/libbtbb.log" 2>&1 || fail libbtbb libbtbb
fi

if want ubertooth; then
    step "Ubertooth host tools" ubertooth
    {
        cmake -S "$src/ubertooth/host" -B "$build/ubertooth" -DCMAKE_INSTALL_PREFIX="$inst" \
            -DCMAKE_PREFIX_PATH="$inst" -DCMAKE_INSTALL_RPATH="$inst/lib" -DINSTALL_UDEV_RULES=OFF \
            -DENABLE_PYTHON=OFF -DUBERTOOTH_HACKRF_PACK_FILE="$src/fpga-build/packs/br_rx.pack" &&
        cmake --build "$build/ubertooth" -j6 &&
        cmake --install "$build/ubertooth"
    } >"$logs/ubertooth.log" 2>&1 || fail "Ubertooth host tools" ubertooth
    grep -q "Building the HackRF Pro backend" "$logs/ubertooth.log" &&
        echo "   with the HackRF Pro backend" || echo "   without the HackRF Pro backend (libhackrf not found)"
fi

if want ubertooth-fw; then
    step "Ubertooth firmware" ubertooth-fw
    {
        make -C "$src/ubertooth/firmware/bluetooth_rxtx" &&
        mkdir -p "$build/ubertooth-fw" &&
        cp "$src/ubertooth/firmware/bluetooth_rxtx/bluetooth_rxtx.dfu" "$build/ubertooth-fw/"
    } >"$logs/ubertooth-fw.log" 2>&1 || fail "Ubertooth firmware" ubertooth-fw
fi

if want gateware || [ -n "$tests" ]; then
    if [ -z "$venv" ]; then
        step "gateware environment" venv
        venv=$out/venv
        {
            python3 -m venv "$venv" &&
            "$venv/bin/pip" install -r "$src/fpga-build/requirements-lock.txt"
        } >"$logs/venv.log" 2>&1 || fail "gateware environment" venv
    fi
fi

if want gateware; then
    step "BR image" gateware
    gw=$src/fpga-build
    g=$build/gateware/build-br
    (
        export PATH="$venv/bin:$PATH" PYTHONPATH="$gw"
        export YOSYS=yowasp-yosys NEXTPNR_ICE40=yowasp-nextpnr-ice40 ICEPACK=yowasp-icepack
        mkdir -p "$build/gateware" && cd "$gw" &&
        rm -rf build-br && mkdir -p build-br && touch build-br/.build-start &&
        SEED=1 python top/br_rx.py &&
        python record_versions.py --since build-br/.build-start build-br br_rx &&
        python check_image.py build-br br_rx &&
        python pack.py --gate build-br/br_rx.gate.json build-br/br_rx.bin "$build/gateware/br_rx.pack" &&
        cp -r build-br "$g"
    ) >"$logs/gateware.log" 2>&1 || fail "BR image" gateware
    grep -h "Max frequency for clock" "$g/br_rx.tim" | tail -3 | sed 's/^.*clock /   /'
    echo "   $(sha256sum "$build/gateware/br_rx.pack" | cut -d' ' -f1)  br_rx.pack"
    if cmp -s "$build/gateware/br_rx.pack" "$src/fpga-build/packs/br_rx.pack"; then
        echo "   br_rx.pack is identical to fpga-build/packs/br_rx.pack"
    else
        echo "   br_rx.pack differs from fpga-build/packs/br_rx.pack"
    fi
fi

if [ -n "$tests" ]; then
    step "gateware tests" tests-gateware
    (cd "$src/fpga-build" && PYTHONPATH=. "$venv/bin/python" -m unittest discover -s test) \
        >"$logs/tests-gateware.log" 2>&1 || fail "gateware tests" tests-gateware
    grep -hE "^(Ran|OK|FAILED)" "$logs/tests-gateware.log" | sed 's/^/   /'
    step "BR model tests" tests-br
    (cd "$src/br" && "$venv/bin/python" -m unittest discover -p 'test_*.py') \
        >"$logs/tests-br.log" 2>&1 || fail "BR model tests" tests-br
    grep -hE "^(Ran|OK|FAILED)" "$logs/tests-br.log" | sed 's/^/   /'
    if want libbtbb; then
        step "libbtbb tests" tests-libbtbb
        (cd "$build/libbtbb" && ctest --output-on-failure) >"$logs/tests-libbtbb.log" 2>&1 ||
            fail "libbtbb tests" tests-libbtbb
        grep -h "tests passed" "$logs/tests-libbtbb.log" | sed 's/^/   /'
    fi
    if want ubertooth; then
        step "libubertooth tests" tests-ubertooth
        (cd "$build/ubertooth" && ctest --output-on-failure) >"$logs/tests-ubertooth.log" 2>&1 ||
            fail "libubertooth tests" tests-ubertooth
        grep -h "tests passed" "$logs/tests-ubertooth.log" | sed 's/^/   /'
        step "end to end: synthetic I/Q, bit-exact model, ubertooth-rx" tests-e2e
        (
            set -e
            t=$build/e2e && mkdir -p "$t" && cd "$src/br"
            "$venv/bin/python" synth.py --fc 2441 --samples 64000 \
                --packet 3:0x9e8b33:-10 --packet -5:0x123456:-30 --packet 6:0x5a1c39:-20:20000 -o "$t/e2e.iq"
            "$venv/bin/python" br_fixed.py "$t/e2e.iq" --fc 2441 --seconds 0.004 -o "$t/e2e.dump"
            "$inst/bin/ubertooth-rx" -i "$t/e2e.dump" > "$t/e2e.txt"
            cat "$t/e2e.txt"
            for want in "ch=42 LAP=9e8b33" "ch=34 LAP=123456" "ch=45 LAP=5a1c39"; do
                grep -q "$want" "$t/e2e.txt" || { echo "missing: $want"; exit 1; }
            done
        ) >"$logs/tests-e2e.log" 2>&1 || fail "end-to-end test" tests-e2e
        echo "   3 of 3 synthetic access codes found on their channels"
    fi
    if want ubertooth-fw; then
        step "Ubertooth firmware native tests" tests-ubertooth-fw
        make -C "$src/ubertooth/firmware/tests" >"$logs/tests-ubertooth-fw.log" 2>&1 ||
            fail "Ubertooth firmware tests" tests-ubertooth-fw
        grep -h "passed" "$logs/tests-ubertooth-fw.log" | sed 's/^/   /'
    fi
fi

echo "== done: $out"
