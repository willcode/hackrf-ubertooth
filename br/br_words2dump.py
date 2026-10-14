#!/usr/bin/env python3
#
# SPDX-License-Identifier: BSD-3-Clause

"""
br_words2dump - convert the BR gateware's word stream into an ubertooth-dump file.

With the BR image loaded and register 0x06 set to 0x80 (BR mode), a receive
at 16 Msps streams one 16-bit word per symbol instead of I/Q:

    hackrf_transfer -r words.bin -f 2441000000 -s 16000000 ...
    br_words2dump.py words.bin --fc 2441 -o br.dump
    ubertooth-rx -i br.dump

Bit k of a word is channel k's decision (fc + k MHz, k >= 9 meaning k - 16);
bit 8 marks the first word of each 400-symbol bank.

"""
import argparse

import numpy as np

import br_fixed


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('words')
    ap.add_argument('--fc', type=float, required=True, help='center MHz')
    ap.add_argument('-o', '--out', required=True)
    args = ap.parse_args()
    w = np.fromfile(args.words, dtype='<u2').astype(np.int64)
    br_fixed.words_to_dump(w, args.fc, args.out)
    marks = np.flatnonzero(w & 0x100)
    gaps = np.diff(marks)
    print('%d words, %d banks, bank spacing %s' % (
        len(w), len(marks), 'always 400' if len(gaps) and (gaps == 400).all()
        else 'IRREGULAR (%d of %d not 400)' % ((gaps != 400).sum(), len(gaps))))


if __name__ == '__main__':
    main()
