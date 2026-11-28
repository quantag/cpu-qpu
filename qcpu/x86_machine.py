# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Quantag IT Solutions GmbH
"""
Quantum stored-program (von Neumann) interpreter for a subset of x86-64.

The third front end on the shared core (core.py). Program memory holds bytes, the program counter is
a byte offset, and every cycle parses one variable-length instruction with gates:

  fetch      an 11-byte window at the PC: the 16-byte block that contains the PC and the start of the
             next block are selected with a one-hot decode of the upper PC bits, then a byte shifter
             controlled by the low four PC bits aligns the window
  prefixes   an alignment NOP's prefixes (up to six 66 and one 2E, x86.MAX66): their number P, one-hot, and a
             shift of the window by P; any other instruction with them traps
  normalise  drop the REX prefix and the 0F escape byte (controlled byte shifts), which puts the
             opcode and the bytes after it at fixed positions
  decode     exact patterns over the opcode and the next byte (the table KINDS in x86.py), then the
             ModRM / SIB layout: displacement and immediate position and size, instruction length
  execute    effective address, one memory read, ALU / shifter / multiplier / divider from the core,
             flags ZF SF CF OF, result to a register or to memory, stack pointer and RDX updates
  next PC    PC + length, a relative branch, or an indirect target (register, memory, return address)

This file mirrors x86.decode() and x86.model() step by step; the tests compare the circuit with the
CPU after every cycle, and the reference compares x86.model() with the CPU for every instruction.
A byte sequence that matches no kind, breaks a validity rule, or divides by zero traps.
"""
from dataclasses import dataclass
from .core import Machine
from .x86 import KINDS, ALL_CLASSES, MODRM, S8_OP0, D1, REG8_REG, WINDOW, MAX66, RAX, RCX, RDX, RSP, RBP

FIXED = (RAX, RCX, RDX, RSP, RBP)          # registers with implicit uses; always instantiated
PUSHES = ("push", "pushrm", "pushi", "call", "callr")


@dataclass
class X86Config:
    W: int = 12                             # register width (bits)
    regs: tuple = tuple(range(16))          # instantiated registers (RAX = 0 ... R15 = 15)
    SLOTS: int = 64                         # program memory BYTES (power of two)
    STEPS: int = 16                         # machine cycles in the circuit
    MEM: int = 16                           # data memory: 4-byte cells (W bits each); power of two, >= 4
    classes: tuple = ALL_CLASSES            # instruction kinds the decoder implements

    def __post_init__(self):
        assert 8 <= self.W <= 16
        assert self.SLOTS & (self.SLOTS - 1) == 0 and self.SLOTS >= 16
        self.regs = tuple(sorted(set(r for r in self.regs if 0 <= r <= 15) | set(FIXED)))
        self.pbits = (self.SLOTS - 1).bit_length()
        assert self.MEM >= 4 and self.MEM & (self.MEM - 1) == 0, "x86-64 needs data memory: the return address is on the stack"
        self.mbits = (self.MEM - 1).bit_length()
        assert 2 + self.mbits <= self.W
        self.classes = tuple(c for c in ALL_CLASSES if c in self.classes)
        assert self.pbits + 1 <= self.W, "code offsets (SLOTS bytes) must fit in W - 1 bits"


class QuantumX86(Machine):
    def _alloc_state(self):
        c, W = self.cfg, self.cfg.W
        self.mem = [self.reg(f"mem{s}", 8) for s in range(c.SLOTS)]
        self.pc = self.reg("pc0", c.pbits)
        self.pc_hist = [self.pc]
        self.X = {k: self.reg(f"r{k}", W) for k in c.regs}
        self.nzcv = self.reg("flags", 4)            # [ZF, SF, CF, OF]
        self.ram = [self.reg(f"ram{m}", W) for m in range(c.MEM)]
        self.run = [self.reg("run0", 1)[0]]
        self.trap = []

    # ------------------------------------------------------------------ small helpers
    def bus(self, dst, ctrls, src):
        """dst ^= src if all ctrls hold. ctrls: list of (qubit or constant, value)."""
        for s_, d in zip(src, dst):
            if s_ != self.ZERO:
                self.mcx(ctrls + [(s_, 1)], d)

    def gated_copy(self, ctrl, src, dst):
        """dst ^= src if ctrl; ctrl and the bits of src may be constants."""
        for s_, d in zip(src, dst):
            self.mcx([(ctrl, 1), (s_, 1)], d)

    def OR(self, *qs):
        t = self.a1(); self.x(t); self.mcx([(q, 0) for q in qs], t); return t

    def read_sel(self, sel, enable, dst):
        """dst ^= X[number given by the 4 selector bits] if enable."""
        for k, q in self.X.items():
            ctrls = [(sel[j], (k >> j) & 1) for j in range(4)] + [(enable, 1)]
            self.local(lambda: self.fresh_match(ctrls), lambda s_: self.gated_copy(s_, q, dst))

    def _cc(self, cb):
        """x86 condition code cb (4 bits, LSB first) on the current flags [ZF, SF, CF, OF]."""
        zf, sf, cf, of = self.nzcv
        lt = self.fresh_xor(sf, of)
        base = self.a1()
        pick = lambda k: [(cb[1], k & 1), (cb[2], (k >> 1) & 1), (cb[3], (k >> 2) & 1)]
        self.mcx(pick(0) + [(of, 1)], base)
        self.mcx(pick(1) + [(cf, 1)], base)
        self.mcx(pick(2) + [(zf, 1)], base)
        self.mcx(pick(3) + [(self.OR(cf, zf), 1)], base)
        self.mcx(pick(4) + [(sf, 1)], base)
        self.mcx(pick(6) + [(lt, 1)], base)
        self.mcx(pick(7) + [(self.OR(zf, lt), 1)], base)
        return self.fresh_xor(base, cb[0])

    # ------------------------------------------------------------------ one step
    def _step(self, s):
        c, W = self.cfg, self.cfg.W
        self.ptr = 0
        s0 = len(self.g)
        r = self.run[s]
        Z, ONE = self.ZERO, self.ONE
        p, m = c.pbits, c.mbits
        X = self.X
        AND = lambda *t: self.fresh_match(list(t))
        XOR = self.fresh_xor
        NOT = self.fresh_not

        # FETCH: an 11-byte window at the PC. Blocks of 16 bytes: copy the block that holds the PC and the
        # first 10 bytes of the block after it (26 bytes), then shift left by the low four PC bits.
        blk = self.onehot(self.pc[4:], ONE)
        cur = [self.alloc(8) for _ in range(26)]
        for j in range(c.SLOTS // 16):
            for k in range(26):
                self.gated_copy(blk[j], self.mem[(16 * j + k) % c.SLOTS], cur[k])
        for bit, keep in ((3, 18), (2, 14), (1, 12), (0, WINDOW)):
            sh, stay = self.pc[bit], NOT(self.pc[bit])
            nxt = [self.alloc(8) for _ in range(keep)]
            for i in range(keep):
                self.gated_copy(stay, cur[i], nxt[i]); self.gated_copy(sh, cur[i + (1 << bit)], nxt[i])
            cur = nxt
        win = cur

        # PREFIXES of an alignment NOP: j = 0 .. MAX66 bytes 66, then an optional 2E; P = j + (2E). sel[P] one-hot
        byte = lambda q, v: AND(*[(q[j], (v >> j) & 1) for j in range(8)])
        is66 = [byte(win[i], 0x66) for i in range(MAX66)]
        is2e = [byte(win[i], 0x2E) for i in range(MAX66 + 1)]
        sel = [self.a1() for _ in range(MAX66 + 2)]
        run = []                                               # run[j]: the first j bytes are 66
        for j in range(MAX66 + 1):
            ctrls = [(is66[i], 1) for i in range(j)] + ([(is66[j], 0)] if j < MAX66 else [])   # exactly j of them
            self.mcx(ctrls + [(is2e[j], 0)], sel[j])
            self.mcx(ctrls + [(is2e[j], 1)], sel[j + 1])
        npre = [XOR(*[sel[v] for v in range(1, MAX66 + 2) if v >> b & 1]) for b in range(3)]    # P in binary
        haspre = NOT(sel[0])
        pw = [self.alloc(8) for _ in range(WINDOW)]
        for k in range(WINDOW):
            for v in range(MAX66 + 2):
                if k + v < WINDOW:
                    self.bus(pw[k], [(sel[v], 1)], win[k + v])
        win = pw

        # NORMALISE: REX prefix, then the 0F escape byte
        hasrex = AND((win[0][4], 0), (win[0][5], 0), (win[0][6], 1), (win[0][7], 0))
        rb, rx, rr, rw = (AND((hasrex, 1), (win[0][j], 1)) for j in range(4))
        w1 = [self.alloc(8) for _ in range(WINDOW - 1)]
        for k in range(WINDOW - 1):
            self.bus(w1[k], [(hasrex, 0)], win[k]); self.bus(w1[k], [(hasrex, 1)], win[k + 1])
        esc = AND(*[(w1[0][j], (0x0F >> j) & 1) for j in range(8)])
        op = self.alloc(8)
        self.bus(op, [(esc, 0)], w1[0]); self.bus(op, [(esc, 1)], w1[1])
        rest = [self.alloc(8) for _ in range(8)]                 # the bytes after the opcode
        for k in range(8):
            self.bus(rest[k], [(esc, 0)], w1[k + 1]); self.bus(rest[k], [(esc, 1)], w1[k + 2])
        r0, r1 = rest[0], rest[1]
        key = op + r0

        # DECODE: raw kind flags, attributes, validity; then the gated kind flags that drive every effect
        raw = {}
        for name in c.classes:
            raw[name] = self.a1()
            for e, mask, val, q in KINDS[name]:
                self.mcx([(r, 1), (esc, e)] + [(key[b], (val >> b) & 1) for b in range(16) if (mask >> b) & 1]
                         + ([(rw, q)] if q is not None else []), raw[name])
        RG = lambda *names: XOR(*[raw[n] for n in names if n in raw])
        rhas = lambda *names: any(n in raw for n in names)
        modrm = RG(*MODRM)
        s8 = self.a1()
        self.mcx([(RG(*S8_OP0), 1), (op[0], 0)], s8)
        if rhas("alu_imm"): self.mcx([(raw["alu_imm"], 1), (op[0], 0), (op[1], 0)], s8)      # opcode 80
        if rhas("mov_ri"): self.mcx([(raw["mov_ri"], 1), (op[3], 0)], s8)                    # B0 + r
        mod11 = AND((r0[6], 1), (r0[7], 1))
        mem = AND((modrm, 1), (mod11, 0))
        sib = AND((mem, 1), (r0[0], 0), (r0[1], 0), (r0[2], 1))
        bad = []
        bad.append(AND((s8, 1), (mem, 1)))                                                   # byte memory operand
        if rhas("lea"): bad.append(AND((raw["lea"], 1), (mod11, 1)))                         # LEA reg
        rm8 = XOR(AND((s8, 1), (modrm, 1)), RG("movx8", "setcc"))                            # rm is a byte register
        bad.append(AND((hasrex, 0), (rm8, 1), (r0[2], 1)))                                   # AH CH DH BH
        bad.append(AND((hasrex, 0), (s8, 1), (RG(*REG8_REG), 1), (r0[5], 1)))
        if rhas("mov_ri"): bad.append(AND((hasrex, 0), (s8, 1), (raw["mov_ri"], 1), (op[2], 1)))
        if rhas("pop"): bad.append(AND((raw["pop"], 1), (op[0], 0), (op[1], 0), (op[2], 1), (rb, 0)))   # POP RSP
        bad.append(AND((haspre, 1), (RG("nop", "nopm") if rhas("nop", "nopm") else Z, 0)))    # prefixes: NOPs only
        bad.append(AND((haspre, 1), (hasrex, 1)))
        invalid = self.OR(*bad)
        f = {n: AND((raw[n], 1), (invalid, 0)) for n in raw}
        has = lambda *names: any(n in f for n in names)
        G = lambda *names: XOR(*[f[n] for n in names if n in f])
        fk = lambda n: f.get(n, Z)

        # ModRM / SIB layout
        mod00 = AND((r0[6], 0), (r0[7], 0))
        rm5 = AND((r0[0], 1), (r0[1], 0), (r0[2], 1))
        sb5 = AND((r1[0], 1), (r1[1], 0), (r1[2], 1))
        riprel = AND((mem, 1), (mod00, 1), (rm5, 1))
        nobase = AND((sib, 1), (mod00, 1), (sb5, 1))
        dsz1 = AND((mem, 1), (r0[6], 1), (r0[7], 0))
        dsz4 = XOR(AND((mem, 1), (r0[6], 0), (r0[7], 1)), riprel, nobase)
        dany = XOR(dsz1, dsz4)
        DISP = self.alloc(W)                                     # sign-extended displacement, low W bits
        for i in range(W):
            for sv, b0, b1 in ((0, rest[1], rest[2]), (1, rest[2], rest[3])):
                if i < 8:
                    self.mcx([(dany, 1), (sib, sv), (b0[i], 1)], DISP[i])
                else:
                    self.mcx([(dsz4, 1), (sib, sv), (b1[i - 8], 1)], DISP[i])
                    self.mcx([(dsz1, 1), (sib, sv), (b0[7], 1)], DISP[i])
        # immediate size
        isz4a = AND((fk("alu_imm"), 1), (op[0], 1), (op[1], 0))                              # opcode 81
        fi = G("alu_acc", "test_acc", "test_imm", "mov_imm")
        isz1 = XOR(AND((fi, 1), (op[0], 0)), fk("alu_imm"), isz4a, AND((fk("mov_ri"), 1), (op[3], 0)),
                   AND((fk("imul_imm"), 1), (op[1], 1)), AND((fk("shift"), 1), (op[4], 0)), fk("jcc8"), fk("jmp8"),
                   AND((fk("pushi"), 1), (op[1], 1)))
        isz4 = XOR(AND((fi, 1), (op[0], 1)), isz4a, AND((fk("mov_ri"), 1), (op[3], 1), (rw, 0)),
                   AND((fk("imul_imm"), 1), (op[1], 0)), fk("jcc32"), fk("jmp32"), fk("call"), AND((fk("pushi"), 1), (op[1], 0)))
        isz8 = AND((fk("mov_ri"), 1), (op[3], 1), (rw, 1))
        # immediate position: number of bytes between the opcode and the immediate
        d0 = NOT(dany)
        pos = {0: NOT(modrm), 1: AND((modrm, 1), (sib, 0), (d0, 1)),
               2: XOR(AND((sib, 1), (d0, 1)), AND((modrm, 1), (sib, 0), (dsz1, 1))), 3: AND((sib, 1), (dsz1, 1)),
               5: AND((modrm, 1), (sib, 0), (dsz4, 1)), 6: AND((sib, 1), (dsz4, 1))}
        iA, iB = self.alloc(8), self.alloc(8)
        for o, sel in pos.items():
            self.gated_copy(sel, rest[o], iA); self.gated_copy(sel, rest[o + 1], iB)
        IMM = self.alloc(W)                                      # sign-extended immediate, low W bits
        iany = XOR(isz1, isz4, isz8)
        for i in range(W):
            if i < 8:
                self.ccx(iany, iA[i], IMM[i])
            else:
                self.ccx(isz1, iA[7], IMM[i])
                self.mcx([(isz1, 0), (iany, 1), (iB[i - 8], 1)], IMM[i])
        # instruction length and the address of the next instruction
        ln = [ONE, Z, Z, Z]
        for term in (npre + [Z], [hasrex, Z, Z, Z], [esc, Z, Z, Z], [modrm, Z, Z, Z], [sib, Z, Z, Z], [dsz1, Z, dsz4, Z], [isz1, Z, isz4, isz8]):
            ln, _ = self.add(ln, term, Z, 4)
        nip, _ = self.add(self.pc, (ln + [Z] * p)[:p], Z, p)

        # REGISTER READS: the reg field, the "port" (r/m register, base, or the register in the opcode), the index
        REGV = self.alloc(W)
        self.read_sel([r0[3], r0[4], r0[5], rr], modrm, REGV)
        psel = self.alloc(3)
        for j in range(3):
            self.mcx([(modrm, 1), (sib, 0), (r0[j], 1)], psel[j])
            self.mcx([(sib, 1), (r1[j], 1)], psel[j])
            self.mcx([(modrm, 0), (op[j], 1)], psel[j])
        PORTV = self.alloc(W)
        self.read_sel(psel + [rb], ONE, PORTV)
        noidx = AND((r1[3], 0), (r1[4], 0), (r1[5], 1), (rx, 0))
        INDEXV = self.alloc(W)
        self.read_sel([r1[3], r1[4], r1[5], rx], AND((sib, 1), (noidx, 0)), INDEXV)

        # EFFECTIVE ADDRESS: base (or next instruction for RIP-relative, or nothing) + index * scale + displacement
        BASEV = self.alloc(W)
        self.bus(BASEV, [(mem, 1), (riprel, 0), (nobase, 0)], PORTV)
        self.bus(BASEV, [(riprel, 1)], nip)
        scaled = self.shifter(INDEXV, [r1[6], r1[7]], Z, Z, 2)
        EA, _ = self.add(self.add(BASEV, scaled, Z, W)[0], DISP, Z, W)

        # MEMORY READ: the r/m operand, or the top of the stack (POP, RET), or [RBP] (LEAVE)
        fpop, fleave = G("pop", "ret"), fk("leave")
        nold = XOR(G("lea", "nopm", "mov_imm"), AND((fk("mov_rm"), 1), (op[1], 0)))          # r/m is not read
        ldrm = AND((mem, 1), (nold, 0), (invalid, 0))
        ld = XOR(ldrm, fpop, fleave)
        RAi = self.alloc(m)
        for j in range(m):
            self.ccx(ldrm, EA[2 + j], RAi[j]); self.mcx([(fpop, 1), (X[RSP][2 + j], 1)], RAi[j])
            self.mcx([(fleave, 1), (X[RBP][2 + j], 1)], RAi[j])
        MEMV = self.alloc(W)
        for cell, sl in enumerate(self.onehot(RAi, ld)):
            self.gated_copy(sl, self.ram[cell], MEMV)
        RMV = self.alloc(W)                                      # value of the r/m operand (or the value read from the stack)
        self.bus(RMV, [(modrm, 1), (mod11, 1)], PORTV)
        self.xor_into_each(RMV, MEMV)

        # ALU: ADD OR AND SUB XOR CMP, TEST, NEG, INC, DEC
        falu = G("alu_rm", "alu_acc", "alu_imm", "test_rm", "test_acc", "test_imm", "neg", "incdec")
        fA, ftest, fneg, finc = G("alu_rm", "alu_acc"), G("test_rm", "test_acc", "test_imm"), fk("neg"), fk("incdec")
        dec = AND((finc, 1), (r0[3], 1))
        aop = [self.a1() for _ in range(3)]                      # 000 ADD 001 OR 100 AND 101 SUB 110 XOR 111 CMP
        for j in range(3):
            self.ccx(fA, op[3 + j], aop[j])
            if has("alu_imm"): self.ccx(f["alu_imm"], r0[3 + j], aop[j])
        self.xor_into(aop[2], [ftest, fneg, dec]); self.xor_into(aop[0], [fneg, dec])
        sub = AND((aop[0], 1), (aop[2], 1))
        f_or, f_and, f_xor = AND((aop[0], 1), (aop[2], 0)), AND((aop[0], 0), (aop[1], 0), (aop[2], 1)), \
            AND((aop[0], 0), (aop[1], 1), (aop[2], 1))
        arith = AND((falu, 1), (XOR(aop[0], aop[2]), 0))
        cmp_ = AND((aop[0], 1), (aop[1], 1), (aop[2], 1))
        facc = G("alu_acc", "test_acc")
        ad1 = AND((fk("alu_rm"), 1), (op[1], 1))                 # reg is the first operand and the destination
        arm = XOR(AND((fk("alu_rm"), 1), (op[1], 0)), G("alu_imm", "test_rm", "test_imm", "incdec"))
        fimm = G("alu_acc", "alu_imm", "test_acc", "test_imm")
        a, b = self.alloc(W), self.alloc(W)
        self.gated_copy(facc, X[RAX], a); self.gated_copy(ad1, REGV, a); self.gated_copy(arm, RMV, a)
        self.gated_copy(fimm, IMM, b); self.gated_copy(XOR(ad1, fneg), RMV, b)
        self.gated_copy(XOR(AND((fk("alu_rm"), 1), (op[1], 0)), fk("test_rm")), REGV, b)
        self.xor_into(b[0], [finc])
        Q = [XOR(q, sub) for q in b]
        S, C = self.add(a, Q, sub, W)
        RES = self.alloc(W)
        for i in range(W):
            an, xo = self.fresh_and(a[i], b[i]), XOR(a[i], b[i])
            self.ccx(arith, S[i], RES[i])
            self.mcx([(falu, 1), (f_and, 1), (an, 1)], RES[i])
            self.mcx([(falu, 1), (f_xor, 1), (xo, 1)], RES[i])
            self.mcx([(falu, 1), (f_or, 1), (xo, 1)], RES[i]); self.mcx([(falu, 1), (f_or, 1), (an, 1)], RES[i])
        ns8 = NOT(s8)
        zf_a = XOR(AND((s8, 1), *[(q, 0) for q in RES[:8]]), AND((s8, 0), *[(q, 0) for q in RES]))
        sf_a = XOR(AND((s8, 1), (RES[7], 1)), AND((s8, 0), (RES[W - 1], 1)))
        cout = XOR(AND((s8, 1), (C[8], 1)), AND((s8, 0), (C[W], 1)))
        cin_top = XOR(AND((s8, 1), (C[7], 1)), AND((s8, 0), (C[W - 1], 1)))
        cf_a = AND((arith, 1), (XOR(cout, sub), 1))
        of_a = AND((arith, 1), (XOR(cout, cin_top), 1))

        # SHIFTS: SHL SHR SAR by an immediate, by 1, or by CL
        fsh = fk("shift")
        zf_s = sf_s = cnz = Z
        SH = [Z] * W
        if has("shift"):
            amt = [self.a1() for _ in range(6)]
            for j in range(6):
                extra = [(rw, 1)] if j == 5 else []
                self.mcx([(fsh, 1), (op[4], 0), (IMM[j], 1)] + extra, amt[j])
                self.mcx([(fsh, 1), (op[4], 1), (op[1], 1), (X[RCX][j], 1)] + extra, amt[j])
            self.mcx([(fsh, 1), (op[4], 1), (op[1], 0)], amt[0])
            cnz = AND((fsh, 1), (self.OR(*amt), 1))
            SH = self.shifter(RMV, amt, r0[3], r0[4], 6, lsr=AND((r0[3], 1), (r0[4], 0)), sf=rw)
            zf_s, sf_s = AND(*[(q, 0) for q in SH]), SH[W - 1]

        # conditions, multiply, divide
        cond = self._cc(op[0:4]) if has("setcc", "cmov", "jcc8", "jcc32") else Z
        fmul = G("imul_rm", "imul_imm")
        PROD = [Z] * W
        if has("imul_rm", "imul_imm"):
            m1 = self.alloc(W)
            self.gated_copy(fk("imul_rm"), REGV, m1); self.gated_copy(fk("imul_imm"), IMM, m1)
            PROD = self._mul(m1, RMV)
        fdiv = div0 = Z
        QUO = REM = [Z] * W
        if has("div"):
            QUO, REM, nz = self._divrem(X[RAX], RMV, r0[3])
            div0 = AND((f["div"], 1), (nz, 0))                   # division by zero: trap, nothing written
            fdiv = AND((f["div"], 1), (nz, 1))

        # RESULT: the value before the byte merge
        SRC = self.alloc(W)
        wr_alu = AND((G("alu_rm", "alu_acc", "alu_imm"), 1), (cmp_, 0))
        fw_alu = XOR(wr_alu, fneg, finc)                         # ALU kinds that write their result
        self.gated_copy(fw_alu, RES, SRC)
        mv1, mv0 = AND((fk("mov_rm"), 1), (op[1], 1)), AND((fk("mov_rm"), 1), (op[1], 0))
        cm1, cm0 = AND((fk("cmov"), 1), (cond, 1)), AND((fk("cmov"), 1), (cond, 0))
        self.gated_copy(XOR(mv1, fk("movsxd"), cm1, fk("pop"), fleave), RMV, SRC)
        self.gated_copy(XOR(mv0, cm0), REGV, SRC)
        self.gated_copy(G("mov_imm", "mov_ri"), IMM, SRC)
        if has("lea"): self.gated_copy(f["lea"], EA, SRC)
        if has("not"):
            for i in range(W): self.cx(f["not"], SRC[i]); self.ccx(f["not"], RMV[i], SRC[i])
        self.gated_copy(fmul, PROD, SRC)
        self.gated_copy(fdiv, QUO, SRC)
        self.gated_copy(fsh, SH, SRC)
        if has("movx8"):
            for i in range(W):
                self.mcx([(f["movx8"], 1), (RMV[i], 1)] if i < 8 else [(f["movx8"], 1), (op[3], 1), (RMV[7], 1)], SRC[i])
        if has("setcc"): self.ccx(f["setcc"], cond, SRC[0])
        # byte writes keep the upper bits of the destination register
        fs8w = AND((s8, 1), (XOR(fw_alu, G("mov_rm", "mov_imm", "mov_ri")), 1))
        bw = XOR(fs8w, fk("setcc"))
        bw_reg = AND((fs8w, 1), (XOR(ad1, mv1), 1))
        bw_acc = AND((fs8w, 1), (fk("alu_acc"), 1))
        bw_port = XOR(bw, bw_reg, bw_acc)
        R = self.alloc(W)
        for i in range(W):
            if i < 8:
                self.cx(SRC[i], R[i])
            else:
                self.mcx([(bw, 0), (SRC[i], 1)], R[i])
                self.ccx(bw_reg, REGV[i], R[i]); self.mcx([(bw_acc, 1), (X[RAX][i], 1)], R[i]); self.ccx(bw_port, PORTV[i], R[i])

        # DESTINATION of R: a register (number DSEL) or the memory operand
        to_rm = XOR(AND((fk("alu_rm"), 1), (op[1], 0), (cmp_, 0)), AND((fk("alu_imm"), 1), (cmp_, 0)), mv0, fk("mov_imm"),
                    fk("not"), fneg, finc, fsh)
        d_port = XOR(AND((XOR(to_rm, fk("setcc")), 1), (mod11, 1)), fk("mov_ri"), fk("pop"))
        d_reg = XOR(AND((ad1, 1), (cmp_, 0)), mv1, G("lea", "movsxd", "movx8", "imul_rm", "imul_imm", "cmov"))
        d_acc = XOR(AND((fk("alu_acc"), 1), (cmp_, 0)), fdiv)
        d_mem = AND((to_rm, 1), (mem, 1))
        DSEL = [self.a1() for _ in range(4)]
        for j, (pq, rq) in enumerate(zip(psel + [rb], [r0[3], r0[4], r0[5], rr])):
            self.ccx(d_port, pq, DSEL[j]); self.ccx(d_reg, rq, DSEL[j])
        self.xor_into(DSEL[0], [fleave]); self.xor_into(DSEL[2], [fleave])                   # LEAVE writes RBP (5)
        wA = XOR(d_port, d_reg, d_acc, fleave)
        we = [{k: self.fresh_match([(DSEL[j], (k >> j) & 1) for j in range(4)] + [(wA, 1)]) for k in X}]
        ports = [R]
        # RDX: remainder of a division, or the sign of RAX (CDQ / CQO)
        if has("div", "cdq"):
            R2 = self.alloc(W)
            self.gated_copy(fdiv, REM, R2)
            if has("cdq"): self.gated_copy(f["cdq"], [X[RAX][W - 1]] * W, R2)
            ports.append(R2); we.append({RDX: XOR(fdiv, fk("cdq"))})
        # RSP: PUSH / CALL subtract 8, POP / RET add 8, LEAVE sets RBP + 8
        fpush = G(*PUSHES)
        fsp = XOR(fpush, fpop, fleave)
        spb = self.alloc(W)
        self.gated_copy(XOR(fpush, fpop), X[RSP], spb); self.gated_copy(fleave, X[RBP], spb)
        SPV, _ = self.add(spb, [Z, Z, Z, fsp] + [fpush] * (W - 4), Z, W)
        ports.append(SPV); we.append({RSP: XOR(fsp)})

        # MEMORY WRITE: the result to the memory operand, or a push
        WD = self.alloc(W)
        self.gated_copy(d_mem, R, WD)
        self.gated_copy(fk("push"), PORTV, WD); self.gated_copy(fk("pushrm"), RMV, WD); self.gated_copy(fk("pushi"), IMM, WD)
        self.gated_copy(G("call", "callr"), nip, WD)
        WAi = self.alloc(m)
        for j in range(m):
            self.ccx(d_mem, EA[2 + j], WAi[j]); self.ccx(fpush, SPV[2 + j], WAi[j])
        st = XOR(d_mem, fpush)
        wide = XOR(AND((d_mem, 1), (rw, 1)), fpush)                                           # 8-byte store: the next cell gets the sign
        wsel = self.onehot(WAi, st)
        wm = [wsel, [self.fresh_and(wsel[cell - 1], wide) for cell in range(c.MEM)]]
        dm = [WD, [WD[W - 1]] * W]

        # FLAGS: [ZF, SF, CF, OF]; a flag that the instruction does not write keeps its value
        shf = AND((fsh, 1), (cnz, 1)) if has("shift") else Z
        new = [XOR(AND((falu, 1), (zf_a, 1)), AND((shf, 1), (zf_s, 1))), XOR(AND((falu, 1), (sf_a, 1)), AND((shf, 1), (sf_s, 1))),
               cf_a, of_a]
        wen = [XOR(falu, shf), XOR(falu, shf), XOR(falu, finc), XOR(falu)]
        FL = self.alloc(4)
        for i in range(4):
            self.cx(self.nzcv[i], FL[i])
            self.ccx(wen[i], XOR(new[i], self.nzcv[i]), FL[i])
        setf = XOR(falu, shf)

        # NEXT PC
        trap = AND((r, 1), (G(*c.classes), 0))
        self.xor_into(trap, [div0])
        inc = XOR(r, fk("hlt"), trap)                                                        # running and not halting
        rel = XOR(G("jmp8", "jmp32", "call"), AND((G("jcc8", "jcc32"), 1), (cond, 1)))
        tgt, _ = self.add(nip, [self.fresh_and(rel, IMM[i]) for i in range(p)], Z, p)
        ind, fret = G("jmpr", "callr"), fk("ret")
        NPC = self.alloc(p)
        for i in range(p):
            self.mcx([(inc, 1), (ind, 0), (fret, 0), (tgt[i], 1)], NPC[i])
            self.mcx([(ind, 1), (RMV[i], 1)], NPC[i]); self.mcx([(fret, 1), (MEMV[i], 1)], NPC[i])
            self.mcx([(inc, 0), (self.pc[i], 1)], NPC[i])

        self.finish_step(s, s0, ports, we, NPC, inc, trap, flags=FL, setf=setf, wm=wm, dm=dm)
