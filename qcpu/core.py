# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Quantag IT Solutions GmbH
"""
Shared core of the quantum stored-program machines: everything that does not depend on the
instruction set.

A machine is ONE fixed reversible circuit built from X / CX / CCX / CSWAP. This module provides
  * qubit allocation: named architectural registers, per-cycle registers, and a scratch pool;
  * gate helpers: multi-controlled X, compute/use/uncompute blocks, constants;
  * reversible units: adder, barrel shifter, comparator, multiplier, dividers;
  * the cycle protocol: compute -> copy results into fresh per-cycle registers -> uncompute ->
    commit with controlled swaps (the displaced values stay behind as history);
  * simulation hooks: full circuit, template replay (run_fast), export to Qiskit.

An instruction-set front end (qcpu/machine.py for AArch64, qcpu/rv_machine.py for RISC-V) subclasses
Machine and supplies _alloc_state() and _step(): fetch, decode and execute for one cycle.
"""


class Machine:
    # ------------------------------------------------------------------ low level
    def __init__(self, cfg, defer=False):
        """defer=True: allocate the machine state only; call build(sink) to generate the cycles.
        With a sink, each cycle's gates are passed to sink(gates, n_qubits) and then dropped."""
        self.cfg = cfg
        self.g = []          # gate list: ('x',t) ('cx',c,t) ('ccx',a,b,t) ('cswap',c,a,b)
        self.n = 0
        self.names = {}      # name -> list of qubit ids (LSB first)
        self.pool, self.ptr = [], 0
        self._alloc_state()  # front end: mem, pc, pc_hist, X, nzcv, ram, run, trap
        self.total = {}
        if not defer:
            self.build()

    def _alloc_state(self):
        raise NotImplementedError

    def _step(self, s):
        raise NotImplementedError

    def run_fast(self, ones, on_cycle=None):
        """Exact basis-state simulation without materialising every cycle.

        Every cycle is the same gate list; only its per-cycle registers (results, history, next PC)
        are fresh qubits. So one cycle is generated once (the template) and replayed STEPS times:
        before each replay the per-cycle qubits are reset to |0> and the PC / running inputs are
        taken from the previous cycle's outputs -- exactly what the full circuit does with new
        qubits. Returns the final state (machine-state qubits are valid) and the per-cycle
        PC trace, trap and running bits. Qubit/gate totals are those of the full circuit."""
        from .bitsim import Streamer
        c = self.cfg
        if not hasattr(self, "_tmpl"):                    # generate the cycle once, reuse it for every input
            pc_in, run_in = self.pc, self.run[0]
            n0 = self.n
            self._step(0)
            tmpl = self.g
            self.g = []
            pool = set(self.pool)
            step_ids = [q for q in range(n0, self.n) if q not in pool]  # per-cycle qubits (scratch excluded)
            self._tmpl = (tmpl, pc_in, run_in, self.pc, self.run[1], self.trap[0], step_ids)
        tmpl, pc_in, run_in, npc, run_out, trap, step_ids = self._tmpl
        per_step = len(step_ids)
        self.template_gates = len(tmpl)
        sim = Streamer(ones)
        sim.s.extend(bytes(max(0, self.n - len(sim.s))))
        st = sim.s
        trace = [sum(st[q] << i for i, q in enumerate(pc_in))]
        runs, traps = [], []
        for k in range(c.STEPS):
            for q in step_ids: st[q] = 0
            sim(tmpl, self.n)
            trace.append(sum(st[q] << i for i, q in enumerate(npc)))
            runs.append(st[run_out]); traps.append(st[trap])
            if on_cycle is not None:
                on_cycle(k, st)
            for i, q in enumerate(pc_in): st[q] = st[npc[i]]
            st[run_in] = st[run_out]
        from collections import Counter
        cnt = Counter(g[0] for g in tmpl)
        self.total = {k: v * c.STEPS for k, v in cnt.items()}
        self.n_total = self.n + per_step * (c.STEPS - 1)
        return dict(state=st, trace=trace, run=runs[-1] if runs else st[run_in], trap=any(traps))

    def build(self, sink=None):
        for s in range(self.cfg.STEPS):
            self._step(s)
            if sink is not None:
                for g in self.g:
                    self.total[g[0]] = self.total.get(g[0], 0) + 1
                sink(self.g, self.n)
                self.g = []
        self.final_pc = self.pc
        return self

    def reg(self, name, size):
        q = list(range(self.n, self.n + size))
        self.n += size
        self.names[name] = q
        return q

    def alloc(self, size=1):
        while self.ptr + size > len(self.pool):
            self.pool.append(self.n); self.n += 1
        q = self.pool[self.ptr:self.ptr + size]
        self.ptr += size
        return q

    def a1(self):
        return self.alloc(1)[0]

    def x(self, t): self.g.append(("x", t))
    def cx(self, a, t): self.g.append(("cx", a, t))
    def ccx(self, a, b, t): self.g.append(("ccx", a, b, t))
    def cswap(self, c_, a, b): self.g.append(("cswap", c_, a, b))

    def mcx(self, ctrls, t):
        """t ^= AND over (q == val) for (q, val) in ctrls; q may also be a python constant 0/1."""
        cs = []
        for q, v in ctrls:
            if isinstance(q, int) and q < 0:   # constants are encoded as -1 (=1) and -2 (=0)
                if (q == -1) != bool(v):
                    return
                continue
            cs.append((q, v))
        neg = [q for q, v in cs if not v]
        for q in neg: self.x(q)
        qs = [q for q, _ in cs]
        if len(qs) == 0: self.x(t)
        elif len(qs) == 1: self.cx(qs[0], t)
        elif len(qs) == 2: self.ccx(qs[0], qs[1], t)
        else:
            p = self.ptr
            anc = self.alloc(len(qs) - 2)
            chain = [("ccx", qs[0], qs[1], anc[0])] + [("ccx", qs[i + 2], anc[i], anc[i + 1]) for i in range(len(qs) - 3)]
            self.g.extend(chain)
            self.ccx(qs[-1], anc[-1], t)
            self.g.extend(reversed(chain))
            self.ptr = p
        for q in neg: self.x(q)

    def local(self, compute, use):
        """compute -> use -> uncompute; scratch qubits are released afterwards."""
        m, p = len(self.g), self.ptr
        v = compute()
        m1 = len(self.g)
        use(v)
        self.g.extend(reversed(self.g[m:m1]))
        self.ptr = p

    # bits are qubit ids (>= 0) or constants ONE = -1 / ZERO = -2
    ONE, ZERO = -1, -2

    def xor_into(self, t, terms):
        for a in terms:
            if a == self.ONE: self.x(t)
            elif a >= 0: self.cx(a, t)

    def and_into(self, t, a, b):
        self.mcx([(a, 1), (b, 1)], t)

    def fresh_xor(self, *terms):
        t = self.a1(); self.xor_into(t, terms); return t

    def fresh_and(self, *ctrls):
        t = self.a1(); self.mcx([(q, 1) for q in ctrls], t); return t

    def fresh_match(self, ctrls):
        t = self.a1(); self.mcx(ctrls, t); return t

    def fresh_not(self, a):
        return self.fresh_xor(a, self.ONE)

    def field_eq(self, ir, lo, n, k):
        return [(ir[lo + j], (k >> j) & 1) for j in range(n)]

    def gated_copy(self, ctrl, src, dst):
        for s_, d in zip(src, dst):
            if s_ == self.ONE: self.cx(ctrl, d)
            elif s_ >= 0: self.ccx(ctrl, s_, d)

    def add(self, P, Q, cin, n):
        """Out-of-place n-bit adder. Returns (sum[n], carries[n+1]); entries may be constants."""
        S, C = [], [cin]
        for i in range(n):
            p, q, c_ = P[i], Q[i], C[i]
            S.append(self.fresh_xor(p, q, c_))
            t = self.a1()
            for a, b in ((p, q), (p, c_), (q, c_)):
                if a == self.ZERO or b == self.ZERO: continue
                if a == self.ONE and b == self.ONE: self.x(t)
                elif a == self.ONE: self.cx(b, t)
                elif b == self.ONE: self.cx(a, t)
                else: self.ccx(a, b, t)
            C.append(t)
        return S, C

    def shifter(self, src, amount, right, arith, n_amount, lsr=None, sf=None):
        """Out-of-place barrel shifter on W bits. amount: list of qubits/consts (LSB first);
        right / arith: qubit or const. Shift amounts >= W give 0 (or the sign for ASR).
        lsr (qubit): logical shift right of the sign-extended 32/64-bit value (sf selects): result
        bit i is the sign while i + k < datasize and 0 beyond -- exact LSR in the W-bit model."""
        W = self.cfg.W
        if lsr is not None:
            arith = right
            out = self.shifter(src, amount, right, arith, n_amount)
            fin = self.alloc(W)
            for i in range(W):
                k64 = self.ge_const(amount[:n_amount], 64 - i)
                k32 = self.ge_const(amount[:n_amount], 32 - i)
                kill = self.a1()
                self.mcx([(lsr, 1), (sf, 1), (k64, 1)], kill)
                self.mcx([(lsr, 1), (sf, 0), (k32, 1)], kill)
                self.mcx([(kill, 0), (out[i], 1)], fin[i])
            return fin
        fill = self.fresh_and(arith, src[W - 1]) if arith >= 0 and src[W - 1] >= 0 else \
            (src[W - 1] if arith == self.ONE else self.ZERO)
        prev = src
        stages = (W - 1).bit_length()
        for j in range(min(stages, n_amount)):
            k, a = 1 << j, amount[j]
            if a == self.ZERO: continue
            nxt = self.alloc(W)
            for i in range(W):
                self.mcx([(a, 0), (prev[i], 1)], nxt[i])
                if i - k >= 0: self.mcx([(a, 1), (right, 0), (prev[i - k], 1)], nxt[i])
                r_src = prev[i + k] if i + k < W else fill
                self.mcx([(a, 1), (right, 1), (r_src, 1)], nxt[i])
            prev = nxt
        hi = [q for q in amount[stages:n_amount] if q != self.ZERO]
        if hi:
            big = self.a1(); self.x(big); self.mcx([(q, 0) for q in hi], big)  # big = OR(hi)
            out = self.alloc(W)
            for i in range(W):
                self.mcx([(big, 0), (prev[i], 1)], out[i])
                self.mcx([(big, 1), (right, 1), (fill, 1)], out[i])
            prev = out
        return prev

    def ge_const(self, val_bits, k):
        """fresh qubit = (unsigned value of val_bits) >= constant k."""
        n = len(val_bits)
        if k <= 0: t = self.a1(); self.x(t); return t
        if k >= (1 << n): return self.a1()
        notk = k ^ ((1 << n) - 1)
        nk = [self.ONE if (notk >> i) & 1 else self.ZERO for i in range(n)]
        _, C = self.add(val_bits, nk, self.ONE, n)      # val - k = val + ~k + 1; carry = no borrow
        return C[n]
    def _div(self, N, D, signed):
        """UDIV / SDIV (signed = qubit) on W bits; division by zero gives 0 as on ARM."""
        W, Z, ONE = self.cfg.W, self.ZERO, self.ONE

        def absval(v):
            s_ = self.fresh_and(signed, v[W - 1])
            x = [self.fresh_xor(q, s_) for q in v]
            out, _ = self.add(x, [Z] * W, s_, W)
            return out, s_
        n, sn = absval(N)
        d, sd = absval(D)
        rem = [Z] * (W + 1)
        q = [None] * W
        nd = [self.fresh_not(b) for b in d] + [ONE]
        for i in reversed(range(W)):
            sh = [n[i]] + rem[:W]                                  # rem << 1 | n_i   (W+1 bits)
            t, cc = self.add(sh, nd, ONE, W + 1)                   # sh - d
            qi = cc[W + 1]                                         # no borrow -> sh >= d
            q[i] = qi
            nr = self.alloc(W + 1)
            for j in range(W + 1):
                self.mcx([(qi, 1), (t[j], 1)], nr[j])
                if sh[j] != Z: self.mcx([(qi, 0), (sh[j], 1)], nr[j])
            rem = nr
        sq = self.fresh_xor(sn, sd)
        x = [self.fresh_xor(b, sq) for b in q]
        res, _ = self.add(x, [Z] * W, sq, W)
        nz = self.a1(); self.x(nz); self.mcx([(b, 0) for b in D], nz)  # divisor != 0
        out = self.alloc(W)
        for i in range(W): self.ccx(nz, res[i], out[i])
        return out

    def xor_into_each(self, dst, src):
        for d, s_ in zip(dst, src):
            self.xor_into(d, [s_])
    def _mul(self, A, B):
        W, Z = self.cfg.W, self.ZERO
        acc = [Z] * W
        for i in range(W):
            add = [Z] * i + [self.fresh_and(B[i], A[j]) for j in range(W - i)]
            acc, _ = self.add(acc, add, Z, W)
        return acc
    # ------------------------------------------------------------------ helpers shared by the front ends
    def read_field(self, ir, lo, nbits, enable, dst):
        """dst ^= X[register number in ir[lo : lo + nbits]] if enable. A register that is not
        instantiated (for example a hard-wired zero register) reads as 0."""
        for k, q in self.X.items():
            ctrls = self.field_eq(ir, lo, nbits, k) + [(enable, 1)]
            self.local(lambda: self.fresh_match(ctrls), lambda sel: self.gated_copy(sel, q, dst))

    def _divrem(self, N, D, signed):
        """Restoring division on W bits; signed = qubit. Returns (q, r, nz):
        q = N / D rounded towards zero, r = N - q * D (it has the sign of N), nz = (D != 0).
        For D = 0 the remainder output is N and the quotient output is not meaningful; each
        instruction set defines its own result for that case and applies it with nz."""
        W, Z, ONE = self.cfg.W, self.ZERO, self.ONE

        def absval(v):
            s_ = self.fresh_and(signed, v[W - 1])
            x = [self.fresh_xor(q, s_) for q in v]
            out, _ = self.add(x, [Z] * W, s_, W)
            return out, s_
        n, sn = absval(N)
        d, sd = absval(D)
        rem = [Z] * (W + 1)
        q = [None] * W
        nd = [self.fresh_not(b) for b in d] + [ONE]
        for i in reversed(range(W)):
            sh = [n[i]] + rem[:W]
            t, cc = self.add(sh, nd, ONE, W + 1)
            qi = cc[W + 1]
            q[i] = qi
            nr = self.alloc(W + 1)
            for j in range(W + 1):
                self.mcx([(qi, 1), (t[j], 1)], nr[j])
                if sh[j] != Z: self.mcx([(qi, 0), (sh[j], 1)], nr[j])
            rem = nr
        sq = self.fresh_xor(sn, sd)
        quo, _ = self.add([self.fresh_xor(b, sq) for b in q], [Z] * W, sq, W)
        rr, _ = self.add([self.fresh_xor(b, sn) for b in rem[:W]], [Z] * W, sn, W)
        nz = self.a1(); self.x(nz); self.mcx([(b, 0) for b in D], nz)
        return quo, rr, nz

    def finish_step(self, s, s0, ports, we, NPC, inc, trap, flags=None, setf=None, wm=None, dm=None):
        """Second half of a machine cycle, the same for every instruction set.

        The front end has computed, since gate index s0:
          ports  list of W-bit values to write;  we[k] = {register key: enable qubit} for ports[k]
          NPC    next program counter;  inc = 'running after this cycle';  trap
          flags  new flag bits and setf, their write enable (None if the ISA has no flags)
          wm/dm  data-memory write ports: wm[k][cell] = enable qubit, dm[k] = W-bit data
        This routine copies them into fresh per-cycle registers, runs the compute block backwards
        (every scratch qubit returns to |0>), and commits with controlled swaps. The values that the
        swaps displace stay in the per-cycle registers: that is the history."""
        c, W = self.cfg, self.cfg.W
        s1 = len(self.g)
        pv = []
        for k, val in enumerate(ports):
            v = self.reg(f"v{s}_{k}", W); self.xor_into_each(v, val); pv.append(v)
        wes = []
        for k, pw in enumerate(we):
            d = {reg: self.reg(f"we{s}_{k}_{reg}", 1)[0] for reg in pw}
            for reg in pw: self.cx(pw[reg], d[reg])
            wes.append(d)
        Fs = sfs = None
        if flags is not None:
            Fs = self.reg(f"f{s}", len(flags)); self.xor_into_each(Fs, flags)
            sfs = self.reg(f"setf{s}", 1)[0]; self.cx(setf, sfs)
        npc = self.reg(f"pc{s + 1}", len(NPC)); self.xor_into_each(npc, NPC)
        run1 = self.reg(f"run{s + 1}", 1)[0]; self.cx(inc, run1)
        tr = self.reg(f"trap{s}", 1)[0]; self.cx(trap, tr)
        mws = mds = None
        if wm is not None:
            mws = [self.reg(f"wm{s}_{k}", c.MEM) for k in range(len(wm))]
            mds = [self.reg(f"dm{s}_{k}", W) for k in range(len(wm))]
            for k in range(len(wm)):
                self.xor_into_each(mws[k], wm[k]); self.xor_into_each(mds[k], dm[k])
        # ---- UNCOMPUTE
        self.g.extend(reversed(self.g[s0:s1]))
        # ---- COMMIT (controlled swaps; displaced old values stay behind as history)
        for k, pw in enumerate(wes):
            for reg, q in pw.items():
                for i in range(W): self.cswap(q, self.X[reg][i], pv[k][i])
        if Fs is not None:
            for i in range(len(Fs)): self.cswap(sfs, self.nzcv[i], Fs[i])
        if mws is not None:
            for k in range(len(mws)):
                for mm in range(c.MEM):
                    for i in range(W):
                        self.cswap(mws[k][mm], self.ram[mm][i], mds[k][i])
        self.pc = npc
        self.pc_hist.append(npc)
        self.run.append(run1)
        self.trap.append(tr)

    # ------------------------------------------------------------------ program loading
    def load_bits(self, words, regs_init=None, ram_init=None, nzcv=0, pc=0):
        """Initial basis state (list of qubit ids set to 1): program, PC, running=1, inputs."""
        c, ones = self.cfg, []
        ones += [self.pc_hist[0][i] for i in range(c.pbits) if (pc >> i) & 1]
        assert len(words) <= c.SLOTS
        for s, w in enumerate(words):
            ones += [self.mem[s][b] for b in range(len(self.mem[s])) if (w >> b) & 1]
        ones.append(self.run[0])
        mask = (1 << c.W) - 1
        for k, v in (regs_init or {}).items():
            ones += [self.X[k][i] for i in range(c.W) if ((v & mask) >> i) & 1]
        for m, v in enumerate(ram_init or []):
            ones += [self.ram[m][i] for i in range(c.W) if ((v & mask) >> i) & 1]
        ones += [self.nzcv[i] for i in range(4) if (nzcv >> i) & 1]
        return ones

    def counts(self):
        from collections import Counter
        out = Counter(g[0] for g in self.g)
        for k, v in self.total.items(): out[k] += v
        return dict(out)

    def to_qiskit(self, init_ones=(), measure=None):
        """Build a qiskit QuantumCircuit: X on init qubits, the interpreter, measurements.
        measure: list of register names to measure into classical registers of the same name."""
        from qiskit import QuantumCircuit, ClassicalRegister, QuantumRegister
        qr = QuantumRegister(self.n, "q")
        qc = QuantumCircuit(qr, name="arm_qpu")
        for q in init_ones: qc.x(q)
        for g in self.g:
            getattr(qc, g[0])(*g[1:])
        for name in measure or []:
            cr = ClassicalRegister(len(self.names[name]), name)
            qc.add_register(cr)
            qc.measure(self.names[name], cr)
        return qc
