# hackrf-ubertooth

> **Disclaimer.** This is independent work. It is not a product of, endorsed by, or supported by
> Great Scott Gadgets. It is provided "as is", without warranty of any kind, express or implied,
> including any warranty of merchantability, fitness for a particular purpose or accuracy. You use
> it entirely at your own risk. Installing modified firmware and loading custom FPGA images can
> leave a device unusable until it is recovered and can affect your warranty. The authors accept no
> liability for any damage or loss arising from its use. You are responsible for operating your
> equipment in accordance with the laws and regulations that apply where you are, including those on
> intercepting communications.

A HackRF Pro as a 15-channel Bluetooth BR receiver for the Ubertooth tools, and the Ubertooth and
libbtbb work behind it. An Ubertooth demodulates one 1 MHz channel and sends the host one 64-byte
packet per 400 symbols; libbtbb searches the symbols for access codes. Here the HackRF Pro's FPGA
does the same for 15 channels at once: a channelizer and demodulator send one 16-bit word per symbol
(one bit per channel), and libubertooth repacks the bits into Ubertooth packets. The Ubertooth
host tools then run on the Pro unchanged: listed as one more device and selected with `-U`.

## What it does

- **Gateware:** a receive-only FPGA image for the HackRF Pro's iCE40UP5K: a 16-branch polyphase
  filter bank and a 16-point FFT split 16 Msps into 1 MHz channels, and a per-channel differential
  phase demodulator makes one decision per symbol. Strong signals that also decode on neighboring
  channels are suppressed there ("leak suppression").
- **libubertooth** (the `ubertooth` submodule, a fork of the Ubertooth project): a HackRF Pro
  backend that loads the image into the FPGA from RAM, answers the Ubertooth's control requests
  (a channel moves the 15-channel window; a sweep scans the band), turns the word stream into
  Ubertooth packets, and returns the board to its stock image when the program ends, however it
  ends. The fork also brings a rewritten LE following engine for the Ubertooth One firmware, LE
  connection recovery, a faster BR search, and hardening against forged packets.
- **libbtbb** (the `libbtbb` submodule, a fork): a faster, checked access code search, and adaptive
  frequency hopping (AFH) that the original does not handle: the hop selection of the
  specification, the piconet clock found without the channel map, and the map deduced from the
  packets and followed as the master changes it.
- **The firmware** (`firmware/hackrf`, the HackRF firmware fork also used by hackrf-gpsdo) loads FPGA
  images staged in RAM. Nothing is written to flash at run time.

What the Pro does not do: follow a piconet's hops (a retune takes longer than a slot, so the window
stays put or scans; an Ubertooth follows), Bluetooth LE (the image is BR only; the Ubertooth does
LE), EDR payloads, or report signal levels.

## Repository layout

| Path | Contents |
|---|---|
| `firmware/hackrf` | submodule: the HackRF firmware and host tools with RAM image loading |
| `ubertooth` | submodule: the Ubertooth fork (firmware, libubertooth with the HackRF Pro backend, tools) |
| `libbtbb` | submodule: the libbtbb fork |
| `fpga-build/` | the gateware (Amaranth): the BR image `top/br_rx.py`, the channelizer `dsp/br_chan.py`, the image gate (`check_image.py`) and packer (`pack.py`), and the gateware tests |
| `fpga-build/packs/br_rx.pack` | the built, gated BR image |
| `br/` | the bit-exact model of the gateware (`br_fixed.py`), a floating-point model (`br_channelizer.py`), a synthetic BR signal generator (`synth.py`), converters and checkers, and their tests |
| `prebuilt/` | the HackRF firmware (`hackrf_usb.bin` for flash, `hackrf_usb_fpga.dfu` for RAM) and `SHA256SUMS` |
| `build.sh` | builds everything from source and compares it with the committed binaries |

## Requirements

- A HackRF Pro. Optionally one or more Ubertooth Ones.
- A Linux host. To build: `git`, `cmake`, `make`, a C compiler, `pkg-config`, the development files
  of `libusb-1.0`, `arm-none-eabi-gcc` with newlib (firmware), and Python 3.12 or newer with `venv`
  (gateware and models). To install the HackRF firmware: `dfu-util` or `hackrf_spiflash`; for the
  Ubertooth firmware, `ubertooth-dfu` (built here).

## Getting the source

```bash
git clone --recurse-submodules https://github.com/willcode/hackrf-ubertooth.git
```

In an existing clone, `git submodule update --init --recursive` fetches the submodules.

## Building

```bash
./build.sh out
```

`out` must be empty or absent. The script clones the repository at `HEAD` into `out/src`, builds
the HackRF firmware, libhackrf, libbtbb, the Ubertooth host tools with the HackRF Pro backend, the
Ubertooth firmware and the BR image, installs the libraries and tools into `out/install`, and
reports whether the firmware and the image match `prebuilt/` and `fpga-build/packs/br_rx.pack`.
`--tests` also runs the test suites; `--venv DIR` reuses a Python environment that has
`fpga-build/requirements-lock.txt` installed; `--only` builds a subset (the steps are listed at the top of the script).
The gateware environment is made with `pip` from the lock file and needs network access; the pinned
YoWASP Yosys and nextpnr versions make the image build deterministic.

To install the tools elsewhere, build the submodules as usual: libbtbb, then `ubertooth/host` with
`-DUBERTOOTH_HACKRF_PACK_FILE=<this repository>/fpga-build/packs/br_rx.pack`, which installs the
image as `share/ubertooth/br_rx.pack`. The HackRF Pro backend is built when `pkg-config` finds
libhackrf; the libhackrf of a distribution is enough.

## Installing the firmware

**HackRF Pro.** The backend needs the firmware from `firmware/hackrf`, which can load FPGA images
into RAM. The flash firmware keeps the stock receive, transmit and sweep functions.

From RAM, nothing written: put the board in DFU mode (hold DFU, press and release RESET, release
DFU) and load the RAM firmware:

```bash
dfu-util --device 1fc9:000c --alt 0 --download prebuilt/hackrf_usb_fpga.dfu
```

It runs until the board is reset or unpowered. A RAM firmware has no FPGA images of its own in
flash, so at exit the tools cannot return the board to the stock image; reset the board for other
use.

In flash: first save the current flash, then write the new firmware and reset:

```bash
hackrf_spiflash -r hackrf-pro-flash-backup.bin
```
```bash
hackrf_spiflash -w prebuilt/hackrf_usb.bin -R
```

`hackrf_spiflash -w` erases the whole flash before writing. To go back, write the backup the same
way. If the board does not start, the boot ROM's DFU mode always works: load a RAM firmware as above,
then write the backup.

**Ubertooth One** (for the fork's LE and other improvements; the HackRF backend does not need it):
list the devices, then flash each Ubertooth by its number:

```bash
out/install/bin/ubertooth-util -L
```
```bash
out/install/bin/ubertooth-dfu -U 0 -d out/build/ubertooth-fw/bluetooth_rxtx.dfu -r
```

If an Ubertooth does not come back on USB within a few seconds, unplug it and plug it in again.
The firmware reports API 1.08; the stock Ubertooth tools work with it (they print a warning about
the newer API), and the tools here need it.

## Running

A qualifying HackRF Pro (the firmware above and the image installed) is listed after the
Ubertooths:

```bash
ubertooth-util -L
```

With no Ubertooth attached the Pro is device 0; with one, device 1. Select it with `-U`:

```bash
ubertooth-rx -U 1 -c 39
```
```bash
ubertooth-rx -U 1 -z
```
```bash
ubertooth-rx -U 1 -l <LAP> -u <UAP>
```

The first receives with the 15-channel window centered on channel 39 (channels 32 to 46); the
second surveys the band for piconets (with no channel, the window scans the 79 channels in six
positions); the third recovers one piconet's clock and, for an AFH piconet, its channel map.

Selecting the Pro loads the image into its FPGA from RAM. When the program ends, normally, by a
signal or by a crash, the receiver is turned off and the stock image is loaded back from flash. A
program killed with `kill -9` cannot do this; the next program that selects the board turns its
receiver off and proceeds. A board that another program is using is refused and left alone. Tools
for modes the image cannot do (LE, spectrum analysis, transmitting) stop with a message.

| Environment variable | Meaning | Default |
|---|---|---|
| `UBERTOOTH_HACKRF_GAIN` | `lna,vga` gain in dB | `16,16` |
| `UBERTOOTH_HACKRF_SCAN_MS` | dwell per window when scanning, ms | 100 |
| `UBERTOOTH_HACKRF_CENTER` | initial window center in MHz, for tools without a channel option | 2441 |
| `UBERTOOTH_HACKRF_LEAK` | leak suppression margin in 6 dB steps, 1–6; 0 turns it off | 3 |
| `UBERTOOTH_HACKRF_PACK` | another gateware pack | the installed one |

The HackRF's 8-bit ADC clips on strong nearby transmitters, which then decode poorly; lower the gain
if a close source is missed. Weak signals need more gain.

## How it works

The gateware (`fpga-build/dsp/br_chan.py`, image `fpga-build/top/br_rx.py`) runs at 16 Msps, three
clock cycles per sample:

- a 16-branch, 4-tap polyphase filter bank decimating by 8 (four SB_MAC16 blocks in dual 8 x 8 mode,
  one per tap row, each with its own copy of the sample ring in block RAM);
- a radix-4 16-point FFT (multiplier-free 4-point stages, twiddles on three SB_MAC16 blocks in
  Gauss's three-multiply form, a block-RAM transpose);
- per channel: a 5-bit normalization, a 6-bit phase table, the phase step over one symbol, a
  carrier-offset threshold and the choice between the two sampling phases as leaky averages, with
  the state in block RAM.

Gateware register 0x06 selects the output: 0 streams I/Q as the receive-only image does; 0x80
streams BR words, with bits 0–3 the "signal present" exponent threshold (0 means 6) and bits 4–6
the leak suppression margin (0 means 3, 7 turns it off). Bit 8 of a word marks the first word of
each 400-symbol bank; bit k is the decision for channel center + k MHz (k ≥ 9 meaning k − 16).

**Leak suppression.** A strong signal also decodes on nearby channels (1 MHz away it turns 2π per
symbol, which the one-symbol detector cannot see) and at the bins of the tuner's ±4 MHz offset, so
one packet can be found on several channels. Each frame the gateware notes the strongest channel;
in the next frame, a channel within 4 of it, or at bin 4 or 12, whose level is the set margin or
more below sends 0101... instead of decisions. libubertooth also treats detections of one LAP
within 2 µs on different channels as one packet and keeps the copy with the fewest access code
errors.

**Retunes and scanning.** Banks around a retune are discarded, located by mapping host time to the
word stream; later banks carry the new center.

**AFH.** libbtbb recovers the clock of an AFH piconet without its channel map and then deduces the
map from the packets, following its changes: a basic-sequence channel that is in use is never
remapped, and each remapped packet gives its channel's rank among the used channels. A receiver
that cannot set its own clock (the HackRF, a capture file) is trimmed in software. `ubertooth-rx -A`
accepts a known map.

## Verification

- `br/br_fixed.py` is a bit-exact model of the gateware; `fpga-build/test/test_br_chan.py` simulates
  the gateware on synthetic signals and compares every output word with the model, with the sample
  spacing fixed and varied (as a clock domain crossing does).
- An image built with `BR_DEBUG=1` streams debug snapshots when register 0x06 is 0x8F: 128 input
  samples and 64 consecutive outputs of the filter bank, the first FFT stage, the twiddle stage and
  the FFT. `br/check_snap.py` recomputes each stage from the one before; the gateware test checks a
  simulated snapshot the same way.
- `br/synth.py` makes BR bursts with a LAP's access code (checked against libbtbb's) at chosen
  channels and levels; `br/test_br.py` decodes them with the models, and `build.sh --tests` runs
  them through the model into `ubertooth-rx`.
- libbtbb's `test_afh_blind` checks the AFH clock search and map deduction on traffic from an
  independent implementation of the hop selection, for fixed and scanning receivers.

## Known limitations

- **Sync word errors.** Each channel's threshold and sampling-phase choice are leaky averages that
  start fresh with each transmitter, and a change of sampling phase within a packet can slip a bit.
  On clean synthetic bursts, about half of the access codes come out with one or more errors (most
  near the start, and nearly all at one unlucky symbol timing). libbtbb's search tolerates a few
  errors, so these packets are still found, but the margin for weak signals is smaller than it
  could be. A better threshold exists as a model but does not yet fit in the FPGA.
- **No hop following, no LE, no signal levels** (see "What it does").
- **Sensitivity** has not been compared with an Ubertooth under controlled conditions.

## Notes for gateware changes

- The filter bank leaves the SB_MAC16 8 x 8 product registers off and registers the products in the
  fabric: on the device, the top product register (`TOP_8x8_MULT_REG`) did not take effect although
  the simulation models honor it, which put Q a cycle ahead of I. The debug snapshots found this;
  simulation could not.
- Keep timing and placement margin. The image fills about three quarters of the iCE40UP5K's logic
  cells; images above about 80% that passed timing in the tools did not run correctly on the device
  (no register access, or wrong decisions).
- Every image must pass the gateware gate (`check_image.py`) before it is loaded. `SEED=n` selects
  the placement seed; the committed image uses seed 1.

## License

The gateware, models, scripts and documentation in this repository are under the BSD-3-Clause
license (`LICENSE`). The submodules keep their own licenses: the HackRF firmware (GPL-2.0), the
Ubertooth project (GPL-2.0) and libbtbb (GPL-2.0).
