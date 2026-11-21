# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Quantag IT Solutions GmbH
"""
Quantum stored-program (von Neumann) interpreter for a subset of AArch64.

ONE fixed reversible quantum circuit, built from X / CX / CCX / CSWAP only, implements
  program memory (32-bit A64 words), program counter, instruction fetch, decoder,
  register file X0..X30 (+SP), NZCV flags, data memory, ALU, branch unit and halt logic.
A program is loaded as the initial basis state of the program-memory qubits; nothing
program-specific is compiled into the circuit, and nothing is measured or classically
dispatched while it runs.

One machine step (repeated STEPS times):
  [compute]   fetch IR = MEM[PC]; decode one-hot class flags; read operands; ALU, shifter,
              multiplier, bitmask decoder, condition evaluation; next PC; write enables
  [copy]      copy results into fresh per-step registers  (R_s, F_s, NPC_s, enables, ...)
  [uncompute] run the compute block backwards -> every scratch qubit is |0> again
  [commit]    controlled-SWAP the per-step results into the architectural state;
              the displaced old values stay in the per-step registers (history).
This is Bennett/Landauer-style reversible embedding: overwrites and jumps become unitary,
at the cost of O(W) history qubits per step. The PC history doubles as an execution trace.

Width model: registers hold W bits (6 <= W <= 16). A W-bit register represents the 64-bit
(or 32-bit) value obtained by sign-extending it. Whenever every value of a run has that form,
the machine reproduces the low W bits of the AArch64 result exactly, including NZCV flags
and control flow. The CPU reference reports any value that does not.
"""
from dataclasses import dataclass, field
from .core import Machine
from .a64 import CLASSES, ALL_CLASSES, SINGLE, PAIRS, MEMOPS

ADDSUB = ("addsub_imm", "addsub_reg", "addsub_ext")
LOGIC = ("logic_reg", "logic_imm")


@dataclass
class Config:
    W: int = 8                          # register width (bits)
    regs: tuple = tuple(range(31))      # instantiated architectural registers (0..30)
    sp: bool = False                    # instantiate SP
    SLOTS: int = 16                     # program memory words (power of two)
    STEPS: int = 16                     # machine cycles in the circuit
    MEM: int = 0                        # data memory: 4-byte cells (W bits each); power of two
    classes: tuple = ALL_CLASSES        # instruction classes the decoder implements
    select: str = "tree"                # how program slots and data cells are selected: "tree" = one binary decoder
                                        # per address; "comparator" = one comparator per slot (the original circuit)

    def __post_init__(self):
        assert 6 <= self.W <= 16
        assert self.select in ("tree", "comparator")
        assert self.SLOTS & (self.SLOTS - 1) == 0
        self.pbits = max(1, (self.SLOTS - 1).bit_length())
        self.mbits = (self.MEM - 1).bit_length() if self.MEM > 1 else (1 if self.MEM else 0)
        if self.MEM:
            assert self.MEM & (self.MEM - 1) == 0 and 2 + self.mbits <= self.W
        self.classes = tuple(c for c in ALL_CLASSES if c in self.classes)
        if not self.MEM:
            self.classes = tuple(c for c in self.classes if c not in MEMOPS)
        assert self.pbits + 2 <= self.W, "code addresses (4 * SLOTS) must fit in W bits"


class QuantumARM(Machine):
    def _alloc_state(self):
        c, W = self.cfg, self.cfg.W
        self.mem = [self.reg(f"mem{s}", 32) for s in range(c.SLOTS)]
        self.pc = self.reg("pc0", c.pbits)
        self.pc_hist = [self.pc]
        self.X = {k: self.reg(f"x{k}", W) for k in c.regs}
        if c.sp:
            self.X[31] = self.reg("sp", W)
        self.nzcv = self.reg("nzcv", 4)             # [N, Z, C, V]
        self.ram = [self.reg(f"ram{m}", W) for m in range(c.MEM)]
        self.run = [self.reg("run0", 1)[0]]
        self.trap = []

    def read_reg(self, ir, lo, enable, sp_ctx, dst):
        """dst ^= X[field] if enable. Field 31 = SP when sp_ctx, else XZR (reads 0)."""
        for k, q in self.X.items():
            ctrls = self.field_eq(ir, lo, 5, k) + [(enable, 1)]
            if k == 31:
                if sp_ctx is None: continue
                ctrls.append((sp_ctx, 1))
            self.local(lambda: self.fresh_match(ctrls), lambda sel: self.gated_copy(sel, q, dst))
    # ------------------------------------------------------------------ one step
    def _step(self, s):
        c, W = self.cfg, self.cfg.W
        self.ptr = 0
        s0 = len(self.g)
        r = self.run[s]
        Z, ONE = self.ZERO, self.ONE
        p = c.pbits

        # FETCH: IR = MEM[PC]
        ir = self.alloc(32)
        tree = c.select == "tree"
        if tree:
            for slot, sel in enumerate(self.onehot(self.pc, ONE)[:c.SLOTS]):
                self.gated_copy(sel, self.mem[slot], ir)
        else:
            for slot in range(c.SLOTS):
                self.local(lambda: self.fresh_match(self.field_eq(self.pc, 0, p, slot)),
                           lambda sel: self.gated_copy(sel, self.mem[slot], ir))

        # DECODE: one-hot class flags (only while running)
        f = {}
        for name in c.classes:
            mask, val = CLASSES[name]
            f[name] = self.fresh_match([(r, 1)] + [(ir[b], (val >> b) & 1) for b in range(32) if (mask >> b) & 1])
        has = lambda *names: any(n in f for n in names)
        G = lambda *names: self.fresh_xor(*[f[n] for n in names if n in f])
        anyf = G(*c.classes)
        trap = self.fresh_match([(r, 1), (anyf, 0)])
        op30, s29, sf = ir[30], ir[29], ir[31]
        fadd = G(*ADDSUB)                       # adder-result classes
        fcc = f.get("ccmp", Z)
        fas2 = self.fresh_xor(fadd, fcc) if has("ccmp") else fadd
        fmul = G("madd", "maddl")
        fmem = G(*MEMOPS)
        fsg = G(*SINGLE)
        fld = self.fresh_xor(f.get("ld_pair", Z))       # loads: single with opc != 00, LDP
        self.mcx([(fsg, 1), (ir[22], 1)], fld); self.mcx([(fsg, 1), (ir[22], 0), (ir[23], 1)], fld)
        fst = self.fresh_xor(f.get("st_pair", Z))       # stores: single with opc == 00, STP
        self.mcx([(fsg, 1), (ir[22], 0), (ir[23], 0)], fst)

        # OPERANDS
        rn_sp = G("addsub_imm", "addsub_ext", *MEMOPS) if c.sp else None
        A = self.alloc(W)
        self.read_reg(ir, 5, G(*ADDSUB, *LOGIC, "bfm", "shiftv", "div", "csel", "ccmp", "madd", "maddl", "bra", *MEMOPS),
                      rn_sp, A)                                             # A = Rn
        self.read_reg(ir, 0, G("movw", "cbz", "tbz"), None, A)            # A = Rt (MOVK, CBZ, TBZ)
        Bm = self.alloc(W)
        self.read_reg(ir, 16, G("addsub_reg", "addsub_ext", "logic_reg", "csel", "ccmp", "madd", "maddl", "shiftv",
                                "div", "ldst_reg"), None, Bm)   # Bm = Rm
        Ra = Dt = Dt2 = None
        if has("madd", "maddl"):
            Ra = self.alloc(W); self.read_reg(ir, 10, fmul, None, Ra)
        if has(*MEMOPS):
            Dt = self.alloc(W); self.read_reg(ir, 0, fst, None, Dt)
            if has("st_pair"):
                Dt2 = self.alloc(W); self.read_reg(ir, 10, f["st_pair"], None, Dt2)

        # second operand
        Beff = self.alloc(W)
        if has("addsub_reg", "logic_reg"):
            shreg = G("addsub_reg", "logic_reg")
            right = self.fresh_xor(ir[22], ir[23])
            self.gated_copy(shreg, self.shifter(Bm, ir[10:16], right, ir[23], 6, lsr=ir[22], sf=sf), Beff)
        if has("addsub_ext"):
            # extend Rm by option = ir[15:13] (size = option<1:0>, signed = option<2>), then LSL #imm3.
            # Byte forms change bits 8.. (zero, or copies of bit 7). Halfword and word forms change only
            # bits >= 16 / >= 32, which a W <= 16 register does not hold: their low W bits are exact.
            isB = self.fresh_match([(ir[13], 0), (ir[14], 0)])
            Bx = self.alloc(W)
            for i in range(W):
                if i < 8:
                    self.cx(Bm[i], Bx[i])
                else:
                    self.mcx([(isB, 0), (Bm[i], 1)], Bx[i])
                    self.mcx([(isB, 1), (ir[15], 1), (Bm[7], 1)], Bx[i])
            self.gated_copy(f["addsub_ext"], self.shifter(Bx, ir[10:13], Z, Z, 3), Beff)
        if has("addsub_imm"):
            nosh = self.fresh_match([(f["addsub_imm"], 1), (ir[22], 0)])
            ysh = self.fresh_match([(f["addsub_imm"], 1), (ir[22], 1)])
            for i in range(W):
                if i < 12: self.ccx(nosh, ir[10 + i], Beff[i])
                if i >= 12: self.ccx(ysh, ir[10 + i - 12], Beff[i])
        if has("logic_imm"):
            self.gated_copy(f["logic_imm"], self._bitmask(ir), Beff)
        if has("ccmp"):
            for i in range(W):
                self.mcx([(fcc, 1), (ir[11], 0), (Bm[i], 1)], Beff[i])
                if i < 5: self.mcx([(fcc, 1), (ir[11], 1), (ir[16 + i], 1)], Beff[i])

        # ADDER: add/sub/ccmp: A + (B^op) + op | csel: 0 + (Rm^op) + o2 | madd: Ra + (prod^o0) + o0
        P = self.alloc(W); Q = self.alloc(W)
        self.gated_copy(fas2, A, P)
        inv_as = self.fresh_and(fas2, op30)
        for i in range(W):
            self.ccx(fas2, Beff[i], Q[i]); self.cx(inv_as, Q[i])
        cin = self.fresh_xor(inv_as)
        if has("csel"):
            fc = f["csel"]
            self.gated_copy(fc, Bm, Q)
            invc = self.fresh_and(fc, op30)
            for i in range(W): self.cx(invc, Q[i])
            self.mcx([(fc, 1), (ir[10], 1)], cin)
        if has("madd", "maddl"):
            prod = self._mul(A, Bm)
            for i in range(W): self.cx(Ra[i], P[i])
            self.gated_copy(fmul, prod, Q)
            invm = self.fresh_and(fmul, ir[15])
            for i in range(W): self.cx(invm, Q[i])
            self.mcx([(fmul, 1), (ir[15], 1)], cin)
        S, C = self.add(P, Q, cin, W)

        cond_c = self._cond(ir[12:16]) if has("csel", "ccmp") else None
        cond_b = self._cond(ir[0:4]) if has("bcond") else None

        # RESULT (port 0)
        R = self.alloc(W)
        for i in range(W): self.ccx(fadd, S[i], R[i])
        flog = None
        if has(*LOGIC):
            flog = G(*LOGIC)
            ninv = self.fresh_and(f["logic_reg"], ir[21]) if "logic_reg" in f else Z
            for i in range(W):
                b = self.fresh_xor(Beff[i], ninv)
                an = self.fresh_and(A[i], b)
                xo = self.fresh_xor(A[i], b)
                self.mcx([(flog, 1), (ir[29], 0), (ir[30], 0), (an, 1)], R[i])   # AND
                self.mcx([(flog, 1), (ir[29], 1), (ir[30], 1), (an, 1)], R[i])   # ANDS
                self.mcx([(flog, 1), (ir[29], 1), (ir[30], 0), (xo, 1)], R[i])   # ORR = xor ^ and
                self.mcx([(flog, 1), (ir[29], 1), (ir[30], 0), (an, 1)], R[i])
                self.mcx([(flog, 1), (ir[29], 0), (ir[30], 1), (xo, 1)], R[i])   # EOR
        if has("movw"):
            fw = f["movw"]
            hw0 = self.fresh_match([(ir[21], 0), (ir[22], 0)])
            isn = self.fresh_match([(fw, 1), (ir[29], 0), (ir[30], 0)])
            isz = self.fresh_match([(fw, 1), (ir[29], 0), (ir[30], 1)])
            isk = self.fresh_match([(fw, 1), (ir[29], 1), (ir[30], 1)])
            for i in range(W):
                t = self.fresh_and(hw0, ir[5 + i]) if i < 16 else Z
                self.mcx([(isz, 1), (t, 1)], R[i])
                self.cx(isn, R[i]); self.mcx([(isn, 1), (t, 1)], R[i])
                self.mcx([(isk, 1), (hw0, 1), (ir[5 + i], 1)], R[i])
                self.mcx([(isk, 1), (hw0, 0), (A[i], 1)], R[i])
        if has("csel"):
            for i in range(W):
                self.mcx([(f["csel"], 1), (cond_c, 1), (A[i], 1)], R[i])
                self.mcx([(f["csel"], 1), (cond_c, 0), (S[i], 1)], R[i])
        if has("madd", "maddl"):
            for i in range(W): self.ccx(fmul, S[i], R[i])
        if has("bfm"):
            self.gated_copy(f["bfm"], self._bfm(ir, A), R)
        if has("shiftv"):
            amt = list(Bm[0:5]) + [self.fresh_and(Bm[5], sf)]
            right = self.fresh_xor(ir[10], ir[11])
            self.gated_copy(f["shiftv"], self.shifter(A, amt, right, ir[11], 6, lsr=ir[10], sf=sf), R)
        if has("div"):
            self.gated_copy(f["div"], self._div(A, Bm, ir[10]), R)
        if has("adrp"):     # ((4*PC) & ~0xFFF) + (imm21 << 12): bits below 12 are zero
            if W > 12:
                imm = [ir[29], ir[30]] + list(ir[5:24])
                pcp = [self.pc[10 + i] if 10 + i < p else Z for i in range(W - 12)]
                pg, _ = self.add(pcp, imm[:W - 12], Z, W - 12)
                for i in range(W - 12): self.ccx(f["adrp"], pg[i], R[12 + i])

        # DATA MEMORY: byte offset, effective address, writeback value
        R2 = WBv = None
        wm = dm = None
        wb = Z
        if has(*MEMOPS):
            m = c.mbits
            fpair = G("ld_pair", "st_pair")
            fsingle = self.fresh_xor(fmem, fpair)
            sz = self.a1()
            self.ccx(fsingle, ir[30], sz); self.ccx(fpair, ir[31], sz)
            off = self.alloc(W)
            fui, fi9, freg = G("ldst_ui"), G("ldst_i9"), G("ldst_reg")
            for i in range(W):      # unsigned offset: imm12 << (2 + sz)
                if 0 <= i - 2 < 12: self.mcx([(fui, 1), (sz, 0), (ir[10 + i - 2], 1)], off[i])
                if 0 <= i - 3 < 12: self.mcx([(fui, 1), (sz, 1), (ir[10 + i - 3], 1)], off[i])
                self.ccx(fi9, ir[12 + min(i, 8)], off[i])                     # imm9, sign-extended
                if i - 2 >= 0: self.mcx([(fpair, 1), (sz, 0), (ir[15 + min(i - 2, 6)], 1)], off[i])   # imm7 << 2
                if i - 3 >= 0: self.mcx([(fpair, 1), (sz, 1), (ir[15 + min(i - 3, 6)], 1)], off[i])   # imm7 << 3
                self.mcx([(freg, 1), (ir[12], 0), (Bm[i], 1)], off[i])         # register, no shift
                if i - 2 >= 0: self.mcx([(freg, 1), (ir[12], 1), (sz, 0), (Bm[i - 2], 1)], off[i])
                if i - 3 >= 0: self.mcx([(freg, 1), (ir[12], 1), (sz, 1), (Bm[i - 3], 1)], off[i])
            post = self.a1()
            self.mcx([(fi9, 1), (ir[10], 1), (ir[11], 0)], post)
            self.mcx([(fpair, 1), (ir[23], 1), (ir[24], 0)], post)
            wb = self.a1()
            self.mcx([(fi9, 1), (ir[10], 1)], wb)                     # mode 01 / 11
            self.mcx([(fpair, 1), (ir[23], 1)], wb)
            eoff = self.alloc(W)
            for i in range(W): self.mcx([(post, 0), (off[i], 1)], eoff[i])
            E, _ = self.add(A, eoff, Z, W)
            WBv, _ = self.add(A, off, Z, W)
            idx = [E[2 + j] for j in range(m)]
            if tree:
                # cell k of a decoder of idx, taken at position (cell - k), is the one-hot of idx + k
                at = lambda t, k: [t[(cell - k) % (1 << m)] for cell in range(c.MEM)]
            else:
                idxc = [idx] + [self.add(idx, [ONE if (k >> j) & 1 else Z for j in range(m)], Z, m)[0] for k in (1, 2, 3)]
                sel_eq = lambda ix, slot: [(ix[j], (slot >> j) & 1) for j in range(m)]
            if has(*MEMOPS) and tree:
                for cell, sl in enumerate(at(self.onehot(idx, fld), 0)):
                    self.gated_copy(sl, self.ram[cell], R)
            elif has(*MEMOPS):
                for slot in range(c.MEM):
                    self.local(lambda: self.fresh_match([(fld, 1)] + sel_eq(idx, slot)),
                               lambda sl: self.gated_copy(sl, self.ram[slot], R))
            if has("ld_pair"):
                R2 = self.alloc(W)
                if tree:        # second register of a pair: the next cell (W pair) or the one after (X pair)
                    lp = [at(self.onehot(idx, self.fresh_match([(f["ld_pair"], 1), (sz, v)])), 1 + v) for v in (0, 1)]
                    for cell in range(c.MEM):
                        self.gated_copy(self.fresh_xor(lp[0][cell], lp[1][cell]), self.ram[cell], R2)
                else:
                    for slot in range(c.MEM):
                        self.local(lambda: self.fresh_match([(f["ld_pair"], 1), (sz, 0)] + sel_eq(idxc[1], slot)),
                                   lambda sl: self.gated_copy(sl, self.ram[slot], R2))
                        self.local(lambda: self.fresh_match([(f["ld_pair"], 1), (sz, 1)] + sel_eq(idxc[2], slot)),
                                   lambda sl: self.gated_copy(sl, self.ram[slot], R2))
            if has(*MEMOPS):
                stp = f.get("st_pair", Z)
                xs = self.fresh_and(fst, sz)
                e = [fst, self.fresh_xor(xs, self.fresh_match([(stp, 1), (sz, 0)])),
                     self.fresh_match([(stp, 1), (sz, 1)])]
                e.append(e[2])
                v2 = Dt2 if Dt2 is not None else [Z] * W
                d1 = self.alloc(W)
                for i in range(W):
                    self.mcx([(sz, 1), (Dt[W - 1], 1)], d1[i])      # X store: high word = sign fill
                    self.mcx([(sz, 0), (v2[i], 1)], d1[i])          # W pair: second register
                dm = [Dt, d1, v2, [v2[W - 1]] * W]
                if tree:        # write port k goes to cell idx + k; ports 2 and 3 share their enable
                    dec = [self.onehot(idx, e[k]) for k in range(3)]
                    wm = [at(dec[min(k, 2)], k) for k in range(4)]
                else:
                    wm = [[self.fresh_match([(e[k], 1)] + sel_eq(idxc[k], slot)) for slot in range(c.MEM)] for k in range(4)]

        # FLAGS
        Nn = self.fresh_xor(R[W - 1], self.fresh_and(fcc, S[W - 1]) if has("ccmp") else Z)
        Zn = self.fresh_match([(q, 0) for q in R] + ([(fcc, 0)] if has("ccmp") else []))
        if has("ccmp"):
            self.mcx([(fcc, 1)] + [(q, 0) for q in S], Zn)
        Cn = self.fresh_and(fas2, C[W])
        Vn = self.fresh_and(fas2, self.fresh_xor(C[W], C[W - 1]))
        NZCV = [Nn, Zn, Cn, Vn]
        if has("ccmp"):     # condition false -> flags = #nzcv immediate (bits 3..0 = N Z C V)
            usei = self.fresh_match([(fcc, 1), (cond_c, 0)])
            fin = self.alloc(4)
            for k, q in enumerate(NZCV):
                self.mcx([(usei, 0), (q, 1)], fin[k])
                self.mcx([(usei, 1), (ir[3 - k], 1)], fin[k])
            NZCV = fin
        setf = self.fresh_and(fadd, s29)
        if flog is not None:
            self.mcx([(flog, 1), (ir[29], 1), (ir[30], 1)], setf)
        if has("ccmp"):
            self.cx(fcc, setf)

        # WRITE PORTS: (value, field position, enable, sp_ctx, fixed register)
        rd_sp = self.fresh_match([(G("addsub_imm", "addsub_ext"), 1), (s29, 0)]) if c.sp else None
        w0 = G(*ADDSUB, *LOGIC, "movw", "csel", "madd", "maddl", "bfm", "shiftv", "div", "adrp")
        if has(*MEMOPS): self.cx(fld, w0)
        ports = [(R, 0, w0, rd_sp, None)]
        if R2 is not None:
            ports.append((R2, 10, f["ld_pair"], None, None))
        if WBv is not None:
            ports.append((WBv, 5, self.fresh_and(fmem, wb), ONE, None))
        link = None
        if has("bl", "bra") and 30 in self.X:
            link = self.fresh_xor(f.get("bl", Z))
            if "bra" in f: self.mcx([(f["bra"], 1), (ir[21], 1), (ir[22], 0)], link)   # BLR
            pc1, _ = self.add(self.pc, [ONE] + [Z] * (p - 1), Z, p)
            LR = [Z, Z] + pc1 + [Z] * W
            ports.append((LR[:W], None, link, None, 30))
        we = []
        for val, pos, en, spc, fixed in ports:
            pw = {}
            for k in self.X:
                if fixed is not None:
                    if k == fixed: pw[k] = self.fresh_xor(en)
                    continue
                if k == 31:
                    if spc is None: continue
                    pw[k] = self.fresh_match(self.field_eq(ir, pos, 5, 31) + [(en, 1), (spc, 1)])
                else:
                    pw[k] = self.fresh_match(self.field_eq(ir, pos, 5, k) + [(en, 1)])
            we.append(pw)

        # NEXT PC
        taken = G("b", "bl")
        if has("bcond"): self.mcx([(f["bcond"], 1), (cond_b, 1)], taken)
        if has("cbz", "tbz"):
            zA = self.fresh_match([(q, 0) for q in A])
            if has("cbz"):
                self.mcx([(f["cbz"], 1), (zA, 1), (ir[24], 0)], taken)
                self.mcx([(f["cbz"], 1), (zA, 0), (ir[24], 1)], taken)
            if has("tbz"):
                bpos = ir[19:24] + [ir[31]]
                bit = self.a1()
                for j in range(W - 1):
                    self.mcx([(bpos[i], (j >> i) & 1) for i in range(6)] + [(A[j], 1)], bit)
                self.mcx([(self.ge_const(bpos, W - 1), 1), (A[W - 1], 1)], bit)
                self.mcx([(f["tbz"], 1), (bit, 0), (ir[24], 0)], taken)
                self.mcx([(f["tbz"], 1), (bit, 1), (ir[24], 1)], taken)
        fbr = f.get("bra", Z)
        inc = self.fresh_xor(r, f.get("hlt", Z), trap)           # running and not halting
        f26, f19, f14 = G("b", "bl"), G("bcond", "cbz"), G("tbz")
        D = []
        for i in range(p):
            d = self.a1()
            self.mcx([(taken, 1), (f26, 1), (ir[min(i, 25)], 1)], d)
            self.mcx([(taken, 1), (f19, 1), (ir[5 + min(i, 18)], 1)], d)
            self.mcx([(taken, 1), (f14, 1), (ir[5 + min(i, 13)], 1)], d)
            if i == 0: self.mcx([(taken, 0), (fbr, 0), (inc, 1)], d)
            D.append(d)
        NPCr, _ = self.add(self.pc, D, Z, p)
        NPC = self.alloc(p)
        for i in range(p):
            self.mcx([(fbr, 0), (NPCr[i], 1)], NPC[i])
            if 2 + i < W: self.mcx([(fbr, 1), (A[2 + i], 1)], NPC[i])

        self.finish_step(s, s0, [port[0] for port in ports], we, NPC, inc, trap, flags=NZCV, setf=setf, wm=wm, dm=dm)

    # ------------------------------------------------------------------ units
    def _cond(self, cb):
        """ARM ConditionHolds(cond) on current NZCV. cb = 4 cond bits (LSB first)."""
        N, Zf, Cf, V = self.nzcv
        base = self.a1()
        nv = self.fresh_xor(N, V)                     # N != V
        terms = {0: Zf, 1: Cf, 2: N, 3: V}
        for k, q in terms.items():
            self.mcx([(cb[1], k & 1), (cb[2], (k >> 1) & 1), (cb[3], 0), (q, 1)], base)
        self.mcx([(cb[1], 0), (cb[2], 0), (cb[3], 1), (Cf, 1), (Zf, 0)], base)    # HI: C && !Z
        self.mcx([(cb[1], 1), (cb[2], 0), (cb[3], 1), (nv, 0)], base)            # GE: N == V
        self.mcx([(cb[1], 0), (cb[2], 1), (cb[3], 1), (nv, 0), (Zf, 0)], base)   # GT: N == V && !Z
        self.mcx([(cb[1], 1), (cb[2], 1), (cb[3], 1)], base)                     # AL
        # cond<0> inverts, except for 1111 (NV behaves as AL)
        out = self.fresh_xor(base)
        self.mcx([(cb[0], 1)], out)
        self.mcx([(cb[0], 1), (cb[1], 1), (cb[2], 1), (cb[3], 1)], out)
        return out
    def _bitmask(self, ir):
        """DecodeBitMasks(N, imms, immr) for logical immediates, low W bits."""
        W = self.cfg.W
        N, immr, imms = ir[22], ir[16:22], ir[10:16]
        mask = self.alloc(W)
        # esize one-hot from N:NOT(imms)
        esz = {64: self.fresh_xor(N)}
        prefix = [(N, 0)]
        for k, e in ((5, 32), (4, 16), (3, 8), (2, 4), (1, 2)):
            esz[e] = self.fresh_match(prefix + [(imms[k], 0)])
            prefix = prefix + [(imms[k], 1)]
        for e, fe in esz.items():
            k = e.bit_length() - 1
            Re, Se = immr[:k], imms[:k]
            for i in range(W):
                cst = i % e
                def compute(Re=Re, Se=Se, cst=cst, k=k):
                    cb = [self.ONE if (cst >> j) & 1 else self.ZERO for j in range(k)]
                    t, _ = self.add(Re, cb, self.ZERO, k)          # (i mod e) + R  (mod e)
                    nt = [self.fresh_not(q) for q in t]
                    _, C = self.add(Se, nt, self.ONE, k)           # S - t >= 0
                    return C[k]
                self.local(compute, lambda le, fe=fe, i=i: self.mcx([(fe, 1), (le, 1)], mask[i]))
        return mask

    def _bfm(self, ir, A):
        """UBFM / SBFM (opc = ir[30]: 1 -> UBFM, 0 -> SBFM) on the W-bit sign-extended model."""
        W, Z = self.cfg.W, self.ZERO
        immr, imms, sf = ir[16:22], ir[10:16], ir[31]
        sb = self.fresh_not(ir[30])                               # SBFM
        # sign bit = A[min(imms, W-1)]
        sbit = self.a1()
        for j in range(W - 1):
            self.mcx(self.field_eq(ir, 10, 6, j) + [(A[j], 1)], sbit)
        geW1 = self.ge_const(imms, W - 1)
        self.mcx([(geW1, 1), (A[W - 1], 1)], sbit)
        fill = self.fresh_and(sb, sbit)
        # keep bits j <= imms, above that: fill
        src = self.alloc(W)
        for j in range(W):
            gej = self.ge_const(imms, j)
            self.mcx([(gej, 1), (A[j], 1)], src[j])
            self.mcx([(gej, 0), (fill, 1)], src[j])
        # case imms >= immr: extract = src >> immr ; else insert = src << (datasize - immr)
        nr = [self.fresh_not(q) for q in immr]
        _, Cc = self.add(imms, nr, self.ONE, 6)
        ge = Cc[6]
        d, _ = self.add(imms, nr, self.ONE, 6)                    # imms - immr
        asr = self.shifter(A, immr, self.ONE, self.ONE, 6)
        shr = self.alloc(W)
        for i in range(W):
            gi = self.ge_const(d, i)
            self.mcx([(gi, 1), (asr[i], 1)], shr[i])
            self.mcx([(gi, 0), (fill, 1)], shr[i])
        neg, _ = self.add(nr, [Z] * 6, self.ONE, 6)               # -immr mod 64
        L = neg[:5] + [self.fresh_xor(neg[5], sf, self.ONE)]       # 32 - immr when sf = 0
        shl = self.shifter(src, L, self.ZERO, self.ZERO, 6)
        out = self.alloc(W)
        for i in range(W):
            self.mcx([(ge, 1), (shr[i], 1)], out[i])
            self.mcx([(ge, 0), (shl[i], 1)], out[i])
        return out
