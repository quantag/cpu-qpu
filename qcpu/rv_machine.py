"""
Quantum stored-program (von Neumann) interpreter for RISC-V: RV32IM and RV64IM, 32-bit instructions.

The front end for the shared core in core.py. ONE fixed reversible circuit of X / CX / CCX / CSWAP
gates implements program memory (32-bit instruction words), program counter, fetch, decoder,
registers x1..x31 (x0 reads as zero), data memory, ALU with multiplier and divider, and the branch
unit. A program is loaded as the initial basis state of the program-memory qubits.

RISC-V has no condition flags: comparisons are part of the branch and set-less-than instructions.
The run ends at EBREAK: the entry function's return address (ra) starts at the last program slot,
which holds EBREAK.

Width model: as for AArch64, a W-bit register stands for the XLEN-bit value obtained by sign
extension. The decoder accepts exactly the patterns listed in rv.py; any other word traps.
"""
from dataclasses import dataclass
from .core import Machine
from .rv import classes as rv_classes, ALL_CLASSES, MEMOPS


@dataclass
class RVConfig:
    W: int = 8                              # register width (bits)
    regs: tuple = tuple(range(1, 32))       # instantiated registers (x0 is never instantiated)
    SLOTS: int = 16                         # program memory words (power of two)
    STEPS: int = 16                         # machine cycles in the circuit
    MEM: int = 0                            # data memory: 4-byte cells (W bits each); power of two
    classes: tuple = ALL_CLASSES            # instruction classes the decoder implements
    xlen: int = 64                          # 64 = RV64IM, 32 = RV32IM
    select: str = "tree"                    # program slot / data cell selection: "tree" (binary decoder) or
                                            # "comparator" (one comparator per slot: the original circuit)

    def __post_init__(self):
        assert 8 <= self.W <= 16 and self.xlen in (32, 64)
        assert self.select in ("tree", "comparator")
        assert self.SLOTS & (self.SLOTS - 1) == 0
        self.regs = tuple(sorted(r for r in self.regs if 1 <= r <= 31))
        self.pbits = max(1, (self.SLOTS - 1).bit_length())
        self.mbits = (self.MEM - 1).bit_length() if self.MEM > 1 else (1 if self.MEM else 0)
        if self.MEM:
            assert self.MEM & (self.MEM - 1) == 0 and 2 + self.mbits <= self.W
        self.classes = tuple(c for c in ALL_CLASSES if c in self.classes)
        if not self.MEM:
            self.classes = tuple(c for c in self.classes if c not in MEMOPS)
        assert self.pbits + 2 <= self.W, "code addresses (4 * SLOTS) must fit in W bits"


class QuantumRISCV(Machine):
    def _alloc_state(self):
        c, W = self.cfg, self.cfg.W
        self.mem = [self.reg(f"mem{s}", 32) for s in range(c.SLOTS)]
        self.pc = self.reg("pc0", c.pbits)
        self.pc_hist = [self.pc]
        self.X = {k: self.reg(f"x{k}", W) for k in c.regs}
        self.nzcv = []                              # no flags register
        self.ram = [self.reg(f"ram{m}", W) for m in range(c.MEM)]
        self.run = [self.reg("run0", 1)[0]]
        self.trap = []

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

        # DECODE: one flag per class, set by exactly one of its patterns, only while running
        pats = rv_classes(c.xlen)
        f = {}
        for name in c.classes:
            f[name] = self.a1()
            for mask, val in pats[name]:
                self.mcx([(r, 1)] + [(ir[b], (val >> b) & 1) for b in range(32) if (mask >> b) & 1], f[name])
        has = lambda *names: any(n in f for n in names)
        G = lambda *names: self.fresh_xor(*[f[n] for n in names if n in f])
        trap = self.fresh_match([(r, 1), (G(*c.classes), 0)])
        f3, alt = ir[12:15], ir[30]
        wf = ir[3] if c.xlen == 64 else Z                    # opcode bit 3: a 32-bit "W" form
        sf = self.fresh_not(wf) if c.xlen == 64 else Z       # operand size is 64 bits
        fI = G("opimm", "shimm", "load", "jalr")             # second operand: I-type immediate
        fB = G("op", "shreg", "branch", "mul", "div")        # second operand: rs2
        fst, fld = f.get("store", Z), f.get("load", Z)

        # OPERANDS
        A = self.alloc(W)
        self.read_field(ir, 15, 5, G("jalr", "branch", "opimm", "shimm", "op", "shreg", "mul", "div", "load", "store"), A)
        B = self.alloc(W)
        self.read_field(ir, 20, 5, G("branch", "op", "shreg", "mul", "div", "store"), B)
        Y = self.alloc(W)                                    # second operand
        self.gated_copy(fB, B, Y)
        for i in range(W):
            self.ccx(fI, ir[20 + min(i, 11)], Y[i])                                   # I: imm[11:0]
            if has("store"):
                self.ccx(fst, ir[7 + i] if i < 5 else ir[25 + min(i - 5, 6)], Y[i])   # S: imm[11:5|4:0]
            if i >= 12 and has("auipc"):
                self.ccx(f["auipc"], ir[i], Y[i])                                     # U: imm[31:12]

        # ADDER: A + Y, or A - Y for sub, set-less-than and branches; auipc: 4 * PC + imm
        fop, fopi = f.get("op", Z), f.get("opimm", Z)
        inv = self.fresh_xor(f.get("branch", Z))
        self.mcx([(fop, 1), (alt, 1)], inv)                                  # sub, subw
        self.mcx([(fop, 1), (f3[2], 0), (f3[1], 1)], inv)                    # slt, sltu
        self.mcx([(fopi, 1), (f3[2], 0), (f3[1], 1)], inv)                   # slti, sltiu
        P = self.alloc(W)
        self.gated_copy(G("jalr", "branch", "opimm", "op", "load", "store"), A, P)
        if has("auipc"):
            self.gated_copy(f["auipc"], [Z, Z] + list(self.pc), P)
        Q = self.alloc(W)
        for i in range(W):
            self.cx(Y[i], Q[i]); self.cx(inv, Q[i])
        S, C = self.add(P, Q, inv, W)
        lt_s = self.fresh_xor(S[W - 1], C[W], C[W - 1])                      # N xor V
        lt_u = self.fresh_not(C[W])                                          # borrow
        eq = self.fresh_match([(q, 0) for q in S])

        # RESULT
        R = self.alloc(W)
        if has("op", "opimm", "auipc"):
            fal = G("op", "opimm")
            arith = self.fresh_xor(f.get("auipc", Z))
            self.mcx([(fal, 1), (f3[0], 0), (f3[1], 0), (f3[2], 0)], arith)   # add, sub, addi (+ W forms)
            for i in range(W): self.ccx(arith, S[i], R[i])
            if has("op", "opimm"):
                self.mcx([(fal, 1), (f3[2], 0), (f3[1], 1), (f3[0], 0), (lt_s, 1)], R[0])    # slt
                self.mcx([(fal, 1), (f3[2], 0), (f3[1], 1), (f3[0], 1), (lt_u, 1)], R[0])    # sltu
                for i in range(W):
                    an = self.fresh_and(A[i], Y[i])
                    xo = self.fresh_xor(A[i], Y[i])
                    self.mcx([(fal, 1), (f3[2], 1), (f3[1], 0), (xo, 1)], R[i])              # xor (f3 = 100)
                    self.mcx([(fal, 1), (f3[2], 1), (f3[1], 1), (f3[0], 0), (xo, 1)], R[i])  # or = xor ^ and
                    self.mcx([(fal, 1), (f3[2], 1), (f3[1], 1), (an, 1)], R[i])              # or, and
        if has("lui"):
            for i in range(12, W): self.ccx(f["lui"], ir[i], R[i])
        if has("shimm", "shreg"):
            fsh = G("shimm", "shreg")
            amt = [self.a1() for _ in range(6)]
            for j in range(6):
                if has("shimm"): self.ccx(f["shimm"], ir[20 + j], amt[j])
                if has("shreg"):
                    self.mcx([(f["shreg"], 1), (B[j], 1)] + ([(sf, 1)] if j == 5 else []), amt[j])
            lsr = self.fresh_match([(f3[2], 1), (alt, 0)])
            self.gated_copy(fsh, self.shifter(A, amt, f3[2], alt, 6, lsr=lsr, sf=sf), R)
        if has("mul"):
            self.gated_copy(f["mul"], self._mul(A, B), R)
        if has("div"):
            quo, rem, nz = self._divrem(A, B, self.fresh_not(f3[0]))
            fq = self.fresh_match([(f["div"], 1), (f3[1], 0)])
            fr = self.fresh_match([(f["div"], 1), (f3[1], 1)])
            for i in range(W):
                self.mcx([(fq, 1), (nz, 1), (quo[i], 1)], R[i])
                self.mcx([(fq, 1), (nz, 0)], R[i])                 # division by zero: all ones
                self.ccx(fr, rem[i], R[i])                         # remainder (the dividend when dividing by zero)
        link = G("jal", "jalr")
        if has("jal", "jalr"):
            pc1, _ = self.add(self.pc, [ONE] + [Z] * (p - 1), Z, p)
            self.gated_copy(link, ([Z, Z] + pc1 + [Z] * W)[:W], R)           # return address 4 * (PC + 1)

        # DATA MEMORY: one cell for lw/lwu/sw; ld reads the low cell; sd also writes the sign into the next
        wm = dm = None
        if has(*MEMOPS):
            m = c.mbits
            idx = [S[2 + j] for j in range(m)]
            if not tree:
                idx1 = self.add(idx, [ONE] + [Z] * (m - 1), Z, m)[0]
                sel_eq = lambda ix, slot: [(ix[j], (slot >> j) & 1) for j in range(m)]
            if has("load") and tree:
                for cell, sl in enumerate(self.onehot(idx, fld)[:c.MEM]):
                    self.gated_copy(sl, self.ram[cell], R)
            elif has("load"):
                for slot in range(c.MEM):
                    self.local(lambda: self.fresh_match([(fld, 1)] + sel_eq(idx, slot)),
                               lambda sl: self.gated_copy(sl, self.ram[slot], R))
            if has("store"):
                sd = self.fresh_and(fst, f3[0]) if c.xlen == 64 else Z
                if tree:
                    dec = self.onehot(idx, fst)
                    wm = [dec[:c.MEM]]
                else:
                    wm = [[self.fresh_match([(fst, 1)] + sel_eq(idx, slot)) for slot in range(c.MEM)]]
                dm = [B]
                if c.xlen == 64 and tree:               # sd: the cell after idx gets the sign
                    wm.append([self.fresh_and(dec[(cell - 1) % (1 << m)], sd) for cell in range(c.MEM)])
                    dm.append([B[W - 1]] * W)
                elif c.xlen == 64:
                    wm.append([self.fresh_match([(sd, 1)] + sel_eq(idx1, slot)) for slot in range(c.MEM)])
                    dm.append([B[W - 1]] * W)

        # WRITE PORT: rd (x0 is not instantiated, so writes to it vanish)
        w0 = G("lui", "auipc", "jal", "jalr", "opimm", "shimm", "op", "shreg", "mul", "div", "load")
        we = [{k: self.fresh_match(self.field_eq(ir, 7, 5, k) + [(w0, 1)]) for k in self.X}]

        # NEXT PC
        taken = self.fresh_xor(f.get("jal", Z))
        if has("branch"):
            fb = f["branch"]
            base = self.a1()
            self.mcx([(f3[2], 0), (eq, 1)], base)                            # beq / bne
            self.mcx([(f3[2], 1), (f3[1], 0), (lt_s, 1)], base)              # blt / bge
            self.mcx([(f3[2], 1), (f3[1], 1), (lt_u, 1)], base)              # bltu / bgeu
            cond = self.fresh_xor(base, f3[0])
            self.ccx(fb, cond, taken)
        fjr = f.get("jalr", Z)
        inc = self.fresh_xor(r, f.get("ebreak", Z), trap)                    # running and not halting
        fj, fbr = f.get("jal", Z), f.get("branch", Z)
        # instruction-index offsets: imm >> 2.  B: imm[4:1|11|10:5|12] = ir[11:8|7|30:25|31];  J: imm[10:1|11|19:12|20]
        db = [ir[9], ir[10], ir[11]] + ir[25:31] + [ir[7]]
        dj = ir[22:31] + [ir[20]] + ir[12:20]
        D = []
        for i in range(p):
            d = self.a1()
            self.mcx([(taken, 1), (fbr, 1), (db[i] if i < len(db) else ir[31], 1)], d)
            self.mcx([(taken, 1), (fj, 1), (dj[i] if i < len(dj) else ir[31], 1)], d)
            if i == 0: self.mcx([(taken, 0), (fjr, 0), (inc, 1)], d)
            D.append(d)
        NPCr, _ = self.add(self.pc, D, Z, p)
        NPC = self.alloc(p)
        for i in range(p):
            self.mcx([(fjr, 0), (NPCr[i], 1)], NPC[i])
            if 2 + i < W: self.mcx([(fjr, 1), (S[2 + i], 1)], NPC[i])

        self.finish_step(s, s0, [R], we, NPC, inc, trap, wm=wm, dm=dm)
