#
# SPDX-License-Identifier: BSD-3-Clause

"""Bluetooth BR channelizer for the HackRF Pro gateware.

16 Msps complex in (one sample every ~3 sync cycles), one 16-bit word per
symbol out: bit k is channel k's decision (fc + k MHz, k >= 9 meaning
k - 16), bit 8 marks the first word of each 400-symbol bank. Bit-exact with
br/br_fixed.py.

  PFB       16 branches x 4 taps, decimation 8; four SB_MAC16 in dual 8x8 mode,
            one per tap row, each fed from its own copy of the sample ring
  FFT16     radix 4: a multiplier-free 4-point stage, twiddles on three
            SB_MAC16 (Gauss's three-multiply form), a block-RAM transpose and a
            second 4-point stage; then (-1)^(k m)
  Demod     5-bit normalization, phase table, per-channel state in block RAM
"""
import math

import numpy as np
from amaranth               import Module, Signal, Cat, Const, Mux, Array, ClockSignal, signed, unsigned
from amaranth.lib           import wiring, stream
from amaranth.lib.wiring    import In, Out
from amaranth.lib.memory    import Memory

from dsp.sb_mac16           import SB_MAC16
from dsp.mul16              import Mul16
from util                   import IQSample

M, T, D = 16, 4, 8
U_SHIFT = 5
TW_BITS = 11
BANK = 400


def coefficients(cutoff_hz=0.6e6, fs=16e6, beta=6.0):
    L = M * T
    n = np.arange(L) - (L - 1) / 2
    fc = cutoff_hz / fs
    h = 2 * fc * np.sinc(2 * fc * n) * np.kaiser(L, beta)
    return [int(v) for v in np.round(h / h.max() * 127)]


def twiddle_consts():
    """Per (p1, k1): c, c + d, d - c of W^(p1 k1), W = exp(+j 2 pi / 16), Q1.11."""
    out = []
    for p1 in range(4):
        for k1 in range(4):
            w = complex(math.cos(2 * math.pi * p1 * k1 / 16), math.sin(2 * math.pi * p1 * k1 / 16))
            c = int(round(w.real * (1 << TW_BITS)))
            d = int(round(w.imag * (1 << TW_BITS)))
            out.append((c, c + d, d - c))
    return out


def phase_table():
    t = []
    for idx in range(1024):
        i = (idx >> 5) & 31
        q = idx & 31
        i = i - 32 if i >= 16 else i
        q = q - 32 if q >= 16 else q
        t.append(int(round(math.atan2(q, i) / (2 * math.pi) * 64)) & 63)
    return t


def rot(re, im, r):
    """(re, im) * j^r for a run-time r: returns (re', im') as expressions."""
    return (Mux(r == 0, re, Mux(r == 1, -im, Mux(r == 2, -re, im))),
            Mux(r == 0, im, Mux(r == 1, re, Mux(r == 2, -im, -re))))


def sat(v, bits):
    lo, hi = -(1 << (bits - 1)), (1 << (bits - 1)) - 1
    return Mux(v > hi, hi, Mux(v < lo, lo, v))


def dft4_load(m, a, cur, width):
    """Partial sums of a 4-point DFT, registered on load: a = three collected
    (re, im) pairs, cur = the fourth. Returns s0 = a0 + a2, s1 = a0 - a2,
    s2 = a1 + a3 and j (a1 - a3) as signals, and the statements to load them."""
    names = ["s0", "s1", "s2", "s3"]
    s = [(Signal(signed(width + 1), name=n + "r"), Signal(signed(width + 1), name=n + "i")) for n in names]
    a0, a1, a2, a3 = a[0], a[1], a[2], cur
    stmts = [
        s[0][0].eq(a0[0] + a2[0]), s[0][1].eq(a0[1] + a2[1]),
        s[1][0].eq(a0[0] - a2[0]), s[1][1].eq(a0[1] - a2[1]),
        s[2][0].eq(a1[0] + a3[0]), s[2][1].eq(a1[1] + a3[1]),
        # s3 is only used as j s3: store (-(a1i - a3i), a1r - a3r)
        s[3][0].eq(a3[1] - a1[1]), s[3][1].eq(a1[0] - a3[0]),
    ]
    return s, stmts


def dft4_out(s, k):
    """X_k = sum_p a_p j^(p k): k even: s0 +- s2; k odd: s1 +- j s3."""
    even = ~k[0]
    xr = Mux(even, s[0][0], s[1][0])
    xi = Mux(even, s[0][1], s[1][1])
    yr = Mux(even, s[2][0], s[3][0])           # s[3] holds j s3
    yi = Mux(even, s[2][1], s[3][1])
    sub = k[1]                                  # k = 2, 3 subtract
    return Mux(sub, xr - yr, xr + yr), Mux(sub, xi - yi, xi + yi)


class PFB(wiring.Component):
    """Outputs u_p (12-bit) in the order p = p1 + 4 p2, p2 inner."""

    def __init__(self):
        super().__init__({
            "input":  In(stream.Signature(IQSample(8), always_ready=True)),
            "valid":  Out(1),
            "j":      Out(4),          # sequence index: p1 = j >> 2, p2 = j & 3
            "ui":     Out(signed(12)),
            "uq":     Out(signed(12)),
            "frame_par": Out(1),       # parity of the frame index m
        })

    def elaborate(self, platform):
        m = Module()
        h = coefficients()
        wp = Signal(8)
        cnt = Signal(3)
        pending = Signal()
        end_pending = Signal(8)
        busy = Signal()
        end = Signal(8)
        j = Signal(4)
        fpar = Signal()            # parity of the frame being computed
        fpar_next = Signal()

        rams = []
        for q in range(T):
            mem = Memory(shape=16, depth=256, init=[])
            m.submodules[f"ring{q}"] = mem
            rams.append((mem.write_port(), mem.read_port()))

        sample = Cat(self.input.p.i, self.input.p.q)     # low byte I, high byte Q
        with m.If(self.input.valid):
            m.d.sync += [wp.eq(wp + 1), cnt.eq(cnt + 1)]
            with m.If(cnt == 7):
                m.d.sync += [pending.eq(1), end_pending.eq(wp)]
        for wport, _ in rams:
            m.d.comb += [wport.en.eq(self.input.valid), wport.addr.eq(wp), wport.data.eq(sample)]

        with m.If(busy):
            m.d.sync += j.eq(j + 1)
            with m.If(j == 15):
                m.d.sync += busy.eq(0)
        start = Signal()
        m.d.comb += start.eq(pending & (~busy | (j == 15)))
        with m.If(start):
            m.d.sync += [busy.eq(1), j.eq(0), end.eq(end_pending), fpar.eq(fpar_next),
                         fpar_next.eq(~fpar_next)]
            with m.If(~(self.input.valid & (cnt == 7))):
                m.d.sync += pending.eq(0)

        p = Signal(4)
        m.d.comb += p.eq(Cat(j[2:4], j[0:2]))           # p1 = j >> 2, p2 = j & 3
        # read addresses, registered
        va = Signal()
        pa = Signal(4)
        ja = Signal(4)
        para = Signal()
        m.d.sync += [va.eq(busy), pa.eq(p), ja.eq(j), para.eq(fpar)]
        for q, (_, rport) in enumerate(rams):
            ra = Signal(8, name=f"raddr{q}")
            m.d.sync += ra.eq(end - p - 16 * q)
            m.d.comb += rport.addr.eq(ra)

        # next stage: RAM data and coefficients to the DSP inputs
        v1 = Signal()
        p_d = Signal(4)
        j_d = Signal(4)
        par_d = Signal()
        m.d.sync += [v1.eq(va), p_d.eq(pa), j_d.eq(ja), par_d.eq(para)]
        prods = []
        for q, (_, rport) in enumerate(rams):
            coef = Array(Const(h[pp + 16 * q], signed(8)) for pp in range(16))[p_d]
            # The 8 x 8 product registers stay off: in the device the top one
            # (TOP_8x8_MULT_REG) did not take effect although both simulation
            # models honor it, so Q came out a cycle before I. The products are
            # registered in the fabric instead.
            mac = SB_MAC16(MODE_8x8=1, A_SIGNED=1, B_SIGNED=1, A_REG=1, B_REG=1,
                           TOPOUTPUT_SELECT=2, BOTOUTPUT_SELECT=2)
            m.submodules[f"mac{q}"] = mac
            m.d.comb += [
                mac.CLK.eq(ClockSignal("sync")), mac.CE.eq(1),
                mac.A.eq(rport.data),                    # {Q, I}
                mac.B.eq(Cat(coef, coef)),
            ]
            pi = Signal(signed(16), name=f"prod_i{q}")
            pq = Signal(signed(16), name=f"prod_q{q}")
            m.d.sync += [pi.eq(mac.O[0:16].as_signed()), pq.eq(mac.O[16:32].as_signed())]
            prods.append((pi, pq))   # (I h, Q h)
        # two more cycles: the DSP's input registers and the product registers
        vp = Signal(2)
        jp = Signal(8)
        parp = Signal(2)
        m.d.sync += [vp.eq(Cat(v1, vp[0])), jp.eq(Cat(j_d, jp[0:4])), parp.eq(Cat(par_d, parp[0]))]
        # pair sums, registered; then the total, registered; then shift and saturate
        pa_i = Signal(signed(17)); pb_i = Signal(signed(17))
        pa_q = Signal(signed(17)); pb_q = Signal(signed(17))
        s_v = Signal(); s_j = Signal(4); s_par = Signal()
        m.d.sync += [pa_i.eq(prods[0][0] + prods[1][0]), pb_i.eq(prods[2][0] + prods[3][0]),
                     pa_q.eq(prods[0][1] + prods[1][1]), pb_q.eq(prods[2][1] + prods[3][1]),
                     s_v.eq(vp[1]), s_j.eq(jp[4:8]), s_par.eq(parp[1])]
        si = Signal(signed(18))
        sq = Signal(signed(18))
        t_v = Signal(); t_j = Signal(4); t_par = Signal()
        m.d.sync += [si.eq(pa_i + pb_i), sq.eq(pa_q + pb_q), t_v.eq(s_v), t_j.eq(s_j), t_par.eq(s_par)]
        m.d.sync += [
            self.valid.eq(t_v), self.j.eq(t_j), self.frame_par.eq(t_par),
            self.ui.eq(sat(si >> U_SHIFT, 12)), self.uq.eq(sat(sq >> U_SHIFT, 12)),
        ]
        return m


class FFT16(wiring.Component):
    """Y_k = sum_p u_p exp(+j 2 pi k p / 16), then (-1)^(k m), saturated to 16 bits."""

    def __init__(self):
        super().__init__({
            "in_valid": In(1), "in_j": In(4), "in_par": In(1),
            "in_i": In(signed(12)), "in_q": In(signed(12)),
            "valid": Out(1), "k": Out(4), "par": Out(1),
            "yi": Out(signed(16)), "yq": Out(signed(16)),
        })
        # internal stages, for the debug snapshot: (valid, index, parity, re, im)
        self.dbg_a = (Signal(name="dbg_a_v"), Signal(4, name="dbg_a_idx"), Signal(name="dbg_a_par"),
                      Signal(signed(14), name="dbg_a_r"), Signal(signed(14), name="dbg_a_i"))
        self.dbg_b = (Signal(name="dbg_b_v"), Signal(4, name="dbg_b_idx"), Signal(name="dbg_b_par"),
                      Signal(signed(15), name="dbg_b_r"), Signal(signed(15), name="dbg_b_i"))

    def elaborate(self, platform):
        m = Module()
        # ---- stage 1: collect a group of four (p2 = 0..3), then emit k1 = 0..3
        col = [(Signal(signed(12), name=f"c{i}r"), Signal(signed(12), name=f"c{i}i")) for i in range(4)]
        seq = Signal(2)
        seq_on = Signal()
        g_p1 = Signal(2)
        g_par = Signal()
        s1, s1_load = dft4_load(m, col, (self.in_i, self.in_q), 12)
        with m.If(self.in_valid):
            p2 = self.in_j[0:2]
            for i in range(4):
                with m.If(p2 == i):
                    m.d.sync += [col[i][0].eq(self.in_i), col[i][1].eq(self.in_q)]
            with m.If(p2 == 3):
                m.d.sync += s1_load
                m.d.sync += [seq_on.eq(1), seq.eq(0), g_p1.eq(self.in_j[2:4]), g_par.eq(self.in_par)]
        with m.If(seq_on & ~(self.in_valid & (self.in_j[0:2] == 3))):
            m.d.sync += seq.eq(seq + 1)
            with m.If(seq == 3):
                m.d.sync += seq_on.eq(0)
        # A[p1][k1] = sum_p2 a[p2] j^(p2 k1)
        k1 = seq
        ar = Signal(signed(14))
        ai = Signal(signed(14))
        o_r, o_i = dft4_out(s1, k1)
        a_v = Signal()
        a_idx = Signal(4)            # p1 * 4 + k1
        a_par = Signal()
        m.d.sync += [
            ar.eq(o_r), ai.eq(o_i),
            a_v.eq(seq_on), a_idx.eq(Cat(k1, g_p1)), a_par.eq(g_par),
        ]

        # ---- twiddle: B = A * W^(p1 k1), Gauss: t1 = c (a + b); re = t1 - b (c + d); im = t1 + a (d - c)
        tw = twiddle_consts()
        c_rom = Array(Const(t[0], signed(16)) for t in tw)
        cpd_rom = Array(Const(t[1], signed(16)) for t in tw)
        dmc_rom = Array(Const(t[2], signed(16)) for t in tw)
        s0 = Signal(signed(15))
        a0 = Signal(signed(14))
        b0 = Signal(signed(14))
        c0 = Signal(signed(16))
        cpd0 = Signal(signed(16))
        dmc0 = Signal(signed(16))
        v0 = Signal()
        idx0 = Signal(4)
        par0 = Signal()
        m.d.sync += [s0.eq(ar + ai), a0.eq(ar), b0.eq(ai), c0.eq(c_rom[a_idx]),
                     cpd0.eq(cpd_rom[a_idx]), dmc0.eq(dmc_rom[a_idx]),
                     v0.eq(a_v), idx0.eq(a_idx), par0.eq(a_par)]
        m.submodules.tm1 = tm1 = Mul16()
        m.submodules.tm2 = tm2 = Mul16()
        m.submodules.tm3 = tm3 = Mul16()
        m.d.comb += [tm1.a.eq(s0), tm1.b.eq(c0), tm2.a.eq(b0), tm2.b.eq(cpd0),
                     tm3.a.eq(a0), tm3.b.eq(dmc0)]
        # two cycles in the DSP, then the products and the sums each
        # registered: a 32-bit carry chain straight off a DSP output fails in
        # the device although the timing report passes it (only the twiddles
        # of k1 = 0, multiples of 2^11, came out right)
        vv = Signal(4)
        ii = Signal(16)
        pp = Signal(4)
        m.d.sync += [vv.eq(Cat(v0, vv[0:3])), ii.eq(Cat(idx0, ii[0:12])), pp.eq(Cat(par0, pp[0:3]))]
        t1 = Signal(signed(32))
        t2 = Signal(signed(32))
        t3 = Signal(signed(32))
        m.d.sync += [t1.eq(tm1.o.as_signed()), t2.eq(tm2.o.as_signed()), t3.eq(tm3.o.as_signed())]
        br_full = Signal(signed(32))
        bi_full = Signal(signed(32))
        m.d.sync += [br_full.eq(t1 - t2), bi_full.eq(t1 + t3)]
        b_v = Signal()
        b_idx = Signal(4)
        b_par = Signal()
        b_r = Signal(signed(15))
        b_i = Signal(signed(15))
        m.d.sync += [
            b_r.eq((br_full + (1 << (TW_BITS - 1))) >> TW_BITS),
            b_i.eq((bi_full + (1 << (TW_BITS - 1))) >> TW_BITS),
            b_v.eq(vv[3]), b_idx.eq(ii[12:16]), b_par.eq(pp[3]),
        ]

        m.d.comb += [d.eq(v) for d, v in zip(self.dbg_a, (a_v, a_idx, a_par, ar, ai))]
        m.d.comb += [d.eq(v) for d, v in zip(self.dbg_b, (b_v, b_idx, b_par, b_r, b_i))]

        # ---- transpose through block RAM: write (p1, k1) at {par, k1, p1}
        tmem = Memory(shape=30, depth=32, init=[])
        m.submodules.tmem = tmem
        tw_port = tmem.write_port()
        tr_port = tmem.read_port()
        b_p1 = b_idx[2:4]
        b_k1 = b_idx[0:2]
        m.d.comb += [tw_port.en.eq(b_v), tw_port.addr.eq(Cat(b_p1, b_k1, b_par)),
                     tw_port.data.eq(Cat(b_r, b_i))]
        # a frame is complete when (p1, k1) = (3, 3) is written: read it out k1-major
        rd_on = Signal()
        rd_cnt = Signal(4)
        rd_par = Signal()
        with m.If(b_v & (b_idx == 15)):
            m.d.sync += [rd_on.eq(1), rd_cnt.eq(0), rd_par.eq(b_par)]
        with m.Elif(rd_on):
            m.d.sync += rd_cnt.eq(rd_cnt + 1)
            with m.If(rd_cnt == 15):
                m.d.sync += rd_on.eq(0)
        m.d.comb += tr_port.addr.eq(Cat(rd_cnt[0:2], rd_cnt[2:4], rd_par))   # p1 inner, k1 outer
        r_v = Signal()
        r_cnt = Signal(4)
        r_par = Signal()
        m.d.sync += [r_v.eq(rd_on), r_cnt.eq(rd_cnt), r_par.eq(rd_par)]
        rd_r = tr_port.data[0:15].as_signed()
        rd_i = tr_port.data[15:30].as_signed()

        # ---- stage 2: collect p1 = 0..3 for a k1, then emit k2 = 0..3
        col2 = [(Signal(signed(15), name=f"d{i}r"), Signal(signed(15), name=f"d{i}i")) for i in range(4)]
        seq2 = Signal(2)
        seq2_on = Signal()
        g2_k1 = Signal(2)
        g2_par = Signal()
        s2, s2_load = dft4_load(m, col2, (rd_r, rd_i), 15)
        with m.If(r_v):
            p1 = r_cnt[0:2]
            for i in range(4):
                with m.If(p1 == i):
                    m.d.sync += [col2[i][0].eq(rd_r), col2[i][1].eq(rd_i)]
            with m.If(p1 == 3):
                m.d.sync += s2_load
                m.d.sync += [seq2_on.eq(1), seq2.eq(0), g2_k1.eq(r_cnt[2:4]), g2_par.eq(r_par)]
        with m.If(seq2_on & ~(r_v & (r_cnt[0:2] == 3))):
            m.d.sync += seq2.eq(seq2 + 1)
            with m.If(seq2 == 3):
                m.d.sync += seq2_on.eq(0)
        k2 = seq2
        o2_r, o2_i = dft4_out(s2, k2)
        yr = Signal(signed(17))
        yi = Signal(signed(17))
        y_v = Signal()
        y_k = Signal(4)
        y_par = Signal()
        m.d.sync += [yr.eq(o2_r), yi.eq(o2_i),
                     y_v.eq(seq2_on), y_k.eq(Cat(g2_k1, k2)), y_par.eq(g2_par)]
        # (-1)^(k m), registered, then saturate
        neg = y_k[0] & y_par
        nr = Signal(signed(18))
        ni = Signal(signed(18))
        n_v = Signal()
        n_k = Signal(4)
        n_par = Signal()
        m.d.sync += [nr.eq(Mux(neg, -yr, yr)), ni.eq(Mux(neg, -yi, yi)),
                     n_v.eq(y_v), n_k.eq(y_k), n_par.eq(y_par)]
        m.d.sync += [
            self.valid.eq(n_v), self.k.eq(n_k), self.par.eq(n_par),
            self.yi.eq(sat(nr, 16)), self.yq.eq(sat(ni, 16)),
        ]
        return m


class Demod(wiring.Component):
    def __init__(self):
        super().__init__({
            "in_valid": In(1), "in_k": In(4), "in_par": In(1),
            "in_i": In(signed(16)), "in_q": In(signed(16)),
            "e_th": In(4), "leak_de": In(3),
            "word_valid": Out(1), "word": Out(16),
        })

    def elaborate(self, platform):
        m = Module()
        I, Q = self.in_i, self.in_q
        # c0: magnitude (ones' complement), registered
        mag = Signal(15)
        mi = Signal(15)
        mq = Signal(15)
        m.d.comb += [mi.eq(Mux(I[15], ~I, I)[0:15]), mq.eq(Mux(Q[15], ~Q, Q)[0:15])]
        r_i = Signal(signed(16))
        r_q = Signal(signed(16))
        v1 = Signal()
        k1 = Signal(4)
        par1 = Signal()
        m.d.sync += [mag.eq(Mux(mi > mq, mi, mq)), r_i.eq(I), r_q.eq(Q),
                     v1.eq(self.in_valid), k1.eq(self.in_k), par1.eq(self.in_par)]
        # c1: exponent and shift, registered
        e = Signal(5)
        for b in range(15):
            with m.If(mag[b]):
                m.d.comb += e.eq(b + 1)
        e_r = Signal(5)
        a_i = Signal(signed(16))
        a_q = Signal(signed(16))
        va = Signal()
        ka = Signal(4)
        para = Signal()
        m.d.sync += [e_r.eq(e), a_i.eq(r_i), a_q.eq(r_q), va.eq(v1), ka.eq(k1), para.eq(par1)]
        sh = Signal(4)
        b_i = Signal(signed(16))
        b_q = Signal(signed(16))
        vb = Signal()
        kb = Signal(4)
        parb = Signal()
        sigb = Signal()
        # leak suppression (see br_fixed.py): the strongest channel of the
        # previous frame, the first to arrive on ties (frames arrive k1-major,
        # 0 4 8 12 1 5 ... 15)
        rmax_e = Signal(5)
        rmax_k = Signal(4)
        pmax_e = Signal(5)
        pmax_k = Signal(4)
        # (the running maximum restarts at e 0, k 0: channel 0 arrives first)
        better = e_r > rmax_e
        with m.If(va):
            with m.If(ka == 15):
                m.d.sync += [pmax_e.eq(Mux(better, e_r, rmax_e)), pmax_k.eq(Mux(better, ka, rmax_k)),
                             rmax_e.eq(0), rmax_k.eq(0)]
            with m.Elif(better):
                m.d.sync += [rmax_e.eq(e_r), rmax_k.eq(ka)]
        dk = Signal(4)
        m.d.comb += dk.eq(ka - pmax_k)
        near = ((dk != 0) & ((dk <= 4) | (dk >= 12))) | (ka == 4) | (ka == 12)
        leakb = Signal()
        m.d.sync += [sh.eq(Mux(e_r > 4, e_r - 4, 0)), b_i.eq(a_i), b_q.eq(a_q),
                     vb.eq(va), kb.eq(ka), parb.eq(para), sigb.eq(e_r >= self.e_th),
                     leakb.eq((self.leak_de != 0) & near & (e_r + self.leak_de <= pmax_e))]
        # c1b: 5-bit values, table and state reads
        i5 = Signal(5)
        q5 = Signal(5)
        m.d.comb += [i5.eq((b_i >> sh)[0:5]), q5.eq((b_q >> sh)[0:5])]
        ptab = Memory(shape=6, depth=1024, init=phase_table())
        m.submodules.ptab = ptab
        prd = ptab.read_port()
        m.d.comb += prd.addr.eq(Cat(q5, i5))
        # state: ph1 6, ph2 6, thr 11, acc0 10, acc1 10, bit_even 1
        smem = Memory(shape=44, depth=16, init=[])
        m.submodules.smem = smem
        srd = smem.read_port()
        swr = smem.write_port()
        m.d.comb += srd.addr.eq(kb)
        v2 = Signal()
        k2 = Signal(4)
        par2 = Signal()
        sig2 = Signal()
        leak2 = Signal()
        m.d.sync += [v2.eq(vb), k2.eq(kb), par2.eq(parb), sig2.eq(sigb), leak2.eq(leakb)]
        # c2: phase step and offset-corrected value, registered
        st = srd.data
        ph = prd.data
        d = Signal(signed(6))
        m.d.comb += d.eq(ph - st[6:12])
        c2_c = Signal(signed(12))
        c2_st = Signal(44)
        c2_ph = Signal(6)
        v3 = Signal()
        k3 = Signal(4)
        par3 = Signal()
        sig3 = Signal()
        leak3 = Signal()
        m.d.sync += [c2_c.eq((d << 4) - st[12:23].as_signed()), c2_st.eq(st), c2_ph.eq(ph),
                     v3.eq(v2), k3.eq(k2), par3.eq(par2), sig3.eq(sig2), leak3.eq(leak2)]
        # a suppressed channel sends 0101... by word: wpar is the word count's parity
        wpar = Signal()
        with m.If(v3 & par3 & (k3 == 15)):
            m.d.sync += wpar.eq(~wpar)
        # c3: |c|, the new threshold and the bit, registered
        thr = c2_st[12:23].as_signed()
        absc = Signal(11)
        c3_thr = Signal(signed(11))
        c3_bit = Signal()
        c3_st = Signal(44)
        c3_ph = Signal(6)
        c3_sig = Signal()
        v3b = Signal()
        k3b = Signal(4)
        par3b = Signal()
        m.d.sync += [
            absc.eq(Mux(c2_c < 0, -c2_c, c2_c)),
            c3_thr.eq(Mux(sig3, thr + (c2_c >> 4), thr)),          # thr + ((d16 - thr) >> 4)
            c3_bit.eq(Mux(leak3, wpar, c2_c > 0)), c3_st.eq(c2_st), c3_ph.eq(c2_ph), c3_sig.eq(sig3),
            v3b.eq(v3), k3b.eq(k3), par3b.eq(par3),
        ]
        # c3b: the metric update for this frame's parity, registered
        acc0, acc1 = c3_st[23:33], c3_st[33:43]
        a_old = Mux(par3b, acc1, acc0)
        c3_acc = Signal(10)
        r_thr = Signal(signed(11))
        r_bit = Signal()
        r_st = Signal(44)
        r_ph = Signal(6)
        v4 = Signal()
        k4 = Signal(4)
        par4 = Signal()
        m.d.sync += [
            c3_acc.eq(Mux(c3_sig, a_old + ((absc - a_old).as_signed() >> 4), a_old)),
            r_thr.eq(c3_thr), r_bit.eq(c3_bit), r_st.eq(c3_st), r_ph.eq(c3_ph),
            v4.eq(v3b), k4.eq(k3b), par4.eq(par3b),
        ]
        c3_thr, c3_bit, c3_st, c3_ph = r_thr, r_bit, r_st, r_ph
        # c4: choose the sampling phase, write the state back, assemble the word
        acc0_new = Mux(par4, c3_st[23:33], c3_acc)
        acc1_new = Mux(par4, c3_acc, c3_st[33:43])
        bit_even = c3_st[43]
        m.d.comb += [
            swr.en.eq(v4), swr.addr.eq(k4),
            swr.data.eq(Cat(c3_ph, c3_st[0:6], c3_thr, acc0_new, acc1_new,
                            Mux(par4, bit_even, c3_bit))),
        ]
        # c5: the metrics and both candidate bits, registered; c6: the choice
        a0r = Signal(10)
        a1r = Signal(10)
        bo = Signal()
        be = Signal()
        v5 = Signal()
        k5 = Signal(4)
        par5 = Signal()
        m.d.sync += [a0r.eq(acc0_new), a1r.eq(acc1_new), bo.eq(c3_bit), be.eq(bit_even),
                     v5.eq(v4), k5.eq(k4), par5.eq(par4)]
        out = Signal()
        v6 = Signal()
        k6 = Signal(4)
        par6 = Signal()
        m.d.sync += [out.eq(Mux(a1r > a0r, bo, be)), v6.eq(v5), k6.eq(k5), par6.eq(par5)]
        v4, k4, par4 = v6, k6, par6
        word = Signal(16)
        nword = Signal(range(BANK))
        with m.If(v4 & par4):
            with m.If(k4 != 8):
                m.d.sync += word.eq(word | (out << k4))
            with m.If(k4 == 15):
                m.d.sync += [
                    self.word_valid.eq(1),
                    self.word.eq((word | (out << 15)) | Mux(nword == 0, 1 << 8, 0)),
                    word.eq(0),
                    nword.eq(Mux(nword == BANK - 1, 0, nword + 1)),
                ]
        with m.If(~(v4 & par4 & (k4 == 15))):
            m.d.sync += self.word_valid.eq(0)
        return m


class BRChannelizer(wiring.Component):
    def __init__(self, debug=False):
        self.debug = debug
        super().__init__({
            "input":  In(stream.Signature(IQSample(8), always_ready=True)),
            "e_th":   In(4),
            "leak_de": In(3),
            "out_valid": Out(1),
            "out_data":  Out(16),
            "dbg_on":    In(1),
            "dbg_valid": Out(1),
            "dbg_data":  Out(16),
        })

    def elaborate(self, platform):
        m = Module()
        m.submodules.pfb = pfb = PFB()
        m.submodules.fft = fft = FFT16()
        m.submodules.dem = dem = Demod()
        m.d.comb += [
            pfb.input.valid.eq(self.input.valid), pfb.input.p.eq(self.input.p),
            fft.in_valid.eq(pfb.valid), fft.in_j.eq(pfb.j), fft.in_par.eq(pfb.frame_par),
            fft.in_i.eq(pfb.ui), fft.in_q.eq(pfb.uq),
            dem.in_valid.eq(fft.valid), dem.in_k.eq(fft.k), dem.in_par.eq(fft.par),
            dem.in_i.eq(fft.yi), dem.in_q.eq(fft.yq), dem.e_th.eq(self.e_th), dem.leak_de.eq(self.leak_de),
            self.out_valid.eq(dem.word_valid), self.out_data.eq(dem.word),
        ]
        if not self.debug:
            return m

        # Debug snapshot (dbg_on): every 2^17 cycles, the next 64 outputs of
        # the filter bank, the first FFT stage, the twiddle stage and the FFT,
        # then streamed out as a header (A55A 5AA5 C33C 3CC3), 4 x 64
        # entries of three 16-bit words (48 bits, low word first) and the
        # 128 input samples (I low byte) from 72 before the stages start:
        #   bank   ui 12, uq 12, j 4, par
        #   stage1 ar 14, ai 14, p1 k1 4 (k1 low), par
        #   twiddle br 15, bi 15, p1 k1 4, par
        #   fft    yi 16, yq 16, k 4, par
        srcs = [
            (pfb.valid, Cat(pfb.ui, pfb.uq, pfb.j, pfb.frame_par)),
            (fft.dbg_a[0], Cat(fft.dbg_a[3], fft.dbg_a[4], fft.dbg_a[1], fft.dbg_a[2])),
            (fft.dbg_b[0], Cat(fft.dbg_b[3], fft.dbg_b[4], fft.dbg_b[1], fft.dbg_b[2])),
            (fft.valid, Cat(fft.yi, fft.yq, fft.k, fft.par)),
        ]
        timer = Signal(17)
        capturing = Signal()
        out_on = Signal()
        m.d.sync += timer.eq(timer + 1)
        # the input samples: 128 from the start; the stages start after 72,
        # so their frames' 64-sample histories are in the snapshot
        xmem = Memory(shape=16, depth=128, init=[])
        m.submodules.snapx = xmem
        xwp = xmem.write_port()
        xrp = xmem.read_port()
        xcnt = Signal(8)
        m.d.comb += [xwp.addr.eq(xcnt[0:7]), xwp.data.eq(Cat(self.input.p.i, self.input.p.q)),
                     xwp.en.eq(capturing & self.input.valid & ~xcnt[7])]
        with m.If(capturing & self.input.valid & ~xcnt[7]):
            m.d.sync += xcnt.eq(xcnt + 1)
        with m.If(~capturing & ~out_on):
            m.d.sync += xcnt.eq(0)
        stages_on = Signal()
        m.d.comb += stages_on.eq(capturing & (xcnt >= 72))
        counts = []
        rports = []
        for n, (v, data) in enumerate(srcs):
            mem = Memory(shape=48, depth=64, init=[])
            m.submodules[f"snap{n}"] = mem
            wp = mem.write_port()
            rp = mem.read_port()
            cnt = Signal(7, name=f"snap_cnt{n}")
            m.d.comb += [wp.addr.eq(cnt[0:6]), wp.data.eq(data),
                         wp.en.eq(stages_on & v & ~cnt[6])]
            with m.If(stages_on & v & ~cnt[6]):
                m.d.sync += cnt.eq(cnt + 1)
            with m.If(~capturing & ~out_on):
                m.d.sync += cnt.eq(0)
            counts.append(cnt)
            rports.append(rp)
        with m.If(self.dbg_on & ~capturing & ~out_on & (timer == 0)):
            m.d.sync += capturing.eq(1)
        all_full = Signal()
        m.d.comb += all_full.eq(counts[0][6] & counts[1][6] & counts[2][6] & counts[3][6] & xcnt[7])
        # readout: one word every 4 cycles; 4 header words, then 256 entries x 3
        xi = Signal(8)                  # input words sent, after the entries
        hdr = Signal(3)                 # header words sent
        entry = Signal(9)               # 0..255, 256 when done
        wsel = Signal(2)
        ph = Signal(2)
        with m.If(capturing & all_full):
            m.d.sync += [capturing.eq(0), out_on.eq(1), hdr.eq(0), entry.eq(0), wsel.eq(0), ph.eq(0),
                         xi.eq(0)]
        for rp in rports:
            m.d.comb += rp.addr.eq(entry[0:6])
        m.d.comb += xrp.addr.eq(xi[0:7])
        rdata = Array(rp.data for rp in rports)[entry[6:8]]
        header = Array([Const(0xA55A, 16), Const(0x5AA5, 16), Const(0xC33C, 16), Const(0x3CC3, 16)])
        m.d.sync += self.dbg_valid.eq(0)
        with m.If(out_on):
            m.d.sync += ph.eq(ph + 1)
            with m.If(ph == 3):
                m.d.sync += self.dbg_valid.eq(1)
                with m.If(~hdr[2]):
                    m.d.sync += [self.dbg_data.eq(header[hdr[0:2]]), hdr.eq(hdr + 1)]
                with m.Elif(~entry[8]):
                    m.d.sync += self.dbg_data.eq(Array([rdata[0:16], rdata[16:32], rdata[32:48]])[wsel])
                    with m.If(wsel == 2):
                        m.d.sync += [wsel.eq(0), entry.eq(entry + 1)]
                    with m.Else():
                        m.d.sync += wsel.eq(wsel + 1)
                with m.Else():
                    m.d.sync += [self.dbg_data.eq(xrp.data), xi.eq(xi + 1)]
                    with m.If(xi == 127):
                        m.d.sync += out_on.eq(0)
        return m
