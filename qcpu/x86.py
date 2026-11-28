# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Quantag IT Solutions GmbH
"""Classical side of the x86-64 front end: instruction table, decoder, static check, disassembly, and
a W-bit model of every implemented instruction.

x86-64 instructions are 1 to 15 bytes long: [REX] [0F] opcode [ModRM] [SIB] [disp8/32] [imm8/32/64].
decode() parses one instruction from the bytes at the program counter; the quantum front end in
x86_machine.py implements the same parse with gates. The kind table lists exact patterns over the
opcode byte and the byte that follows it; a byte sequence that matches no pattern, or that fails one
of the validity rules in decode(), traps in the circuit.

Subset: 8-, 32- and 64-bit integer instructions as compilers emit them for C code. Prefixes: only the
alignment NOPs that assemblers pad code with, up to 6 operand-size bytes 66 and one CS segment byte 2E before
NOP (90) or the multi-byte NOP (0F 1F /0), e.g. GCC 14's 66 66 2E 0F 1F 84 00 00 00 00 00. Not implemented:
the 16-bit operand size and every other prefix on any other instruction, byte or word memory operands, ADC/SBB, rotates, one-operand
MUL/IMUL, string instructions, x87/SSE/AVX, system instructions.

Flags: ZF, SF, CF, OF (in this order). The machine does not model the flags that x86 leaves undefined
or that are rarely used after these instructions: CF and OF after shifts, all four after IMUL and
DIV/IDIV. It leaves them unchanged, and the reference reports a run that goes on to use one.
"""
from .a64 import Check

REG = ["rax", "rcx", "rdx", "rbx", "rsp", "rbp", "rsi", "rdi"] + [f"r{i}" for i in range(8, 16)]
RAX, RCX, RDX, RBX, RSP, RBP, RSI, RDI = range(8)
HLT = 0xF4
WINDOW = 11                    # bytes the circuit fetches: enough for the low 16 bits of every field
MAX66 = 6                      # operand-size prefixes of an alignment NOP (then an optional 2E): at most 7 bytes
PREFIXES = (0x66, 0x67, 0xF2, 0xF3, 0x2E, 0x3E, 0x26, 0x36, 0x64, 0x65, 0xF0)
ZF, SF, CF, OF = range(4)

# kind -> patterns (escape 0F present, mask, value, required REX.W or None); the 16-bit key is
# opcode | next byte << 8, so a pattern can also fix ModRM.mod (bits 14-15) and ModRM.reg (bits 11-13)
_cc = lambda e, base: [(e, 0x00F8, base, None), (e, 0x00FC, base | 0xC, None), (e, 0x00FE, base | 0x8, None)]   # cc != P/NP
KINDS = {
    "alu_rm":   [(0, 0x00F4, 0x0000, None), (0, 0x00E4, 0x0020, None)],        # ADD OR | AND SUB XOR CMP  r/m, r  and  r, r/m
    "alu_acc":  [(0, 0x00F6, 0x0004, None), (0, 0x00E6, 0x0024, None)],        # the same with AL / EAX and an immediate
    "alu_imm":  [(0, 0x30FE, 0x0080, None), (0, 0x20FE, 0x2080, None),         # 80 / 81 /0 /1 /4 /5 /6 /7
                 (0, 0x30FF, 0x0083, None), (0, 0x20FF, 0x2083, None)],        # 83 (sign-extended imm8)
    "test_rm":  [(0, 0x00FE, 0x0084, None)],
    "test_acc": [(0, 0x00FE, 0x00A8, None)],
    "test_imm": [(0, 0x38FE, 0x00F6, None)],                                   # F6 / F7 /0
    "mov_rm":   [(0, 0x00FC, 0x0088, None)],                                   # 88 89 8A 8B
    "mov_imm":  [(0, 0x38FE, 0x00C6, None)],                                   # C6 / C7 /0
    "mov_ri":   [(0, 0x00F0, 0x00B0, None)],                                   # B0+r imm8, B8+r imm32 (imm64 with REX.W)
    "lea":      [(0, 0x00FF, 0x008D, None)],
    "movsxd":   [(0, 0x00FF, 0x0063, 1)],
    "movx8":    [(1, 0xC0F7, 0xC0B6, None)],                                   # MOVZX / MOVSX r, r8
    "not":      [(0, 0x38FF, 0x10F7, None)],
    "neg":      [(0, 0x38FF, 0x18F7, None)],
    "div":      [(0, 0x30FF, 0x30F7, None)],                                   # F7 /6 DIV, /7 IDIV
    "incdec":   [(0, 0x30FF, 0x00FF, None)],                                   # FF /0 INC, /1 DEC
    "callr":    [(0, 0x38FF, 0x10FF, None)],
    "jmpr":     [(0, 0x38FF, 0x20FF, None)],
    "pushrm":   [(0, 0x38FF, 0x30FF, None)],
    "imul_rm":  [(1, 0x00FF, 0x00AF, None)],
    "imul_imm": [(0, 0x00FD, 0x0069, None)],                                   # 69 imm32, 6B imm8
    "cdq":      [(0, 0x00FF, 0x0099, None)],                                   # CDQ / CQO
    "cdqe":     [(0, 0x00FF, 0x0098, 1)],
    "shift":    [(0, m, v | o, None) for o in (0xC1, 0xD1, 0xD3) for m, v in ((0x30FF, 0x2000), (0x38FF, 0x3800))],  # /4 /5 /7
    "setcc":    [(1, m | 0xC000, v | 0xC000, None) for _, m, v, _ in _cc(1, 0x90)],
    "cmov":     _cc(1, 0x40),
    "jcc8":     _cc(0, 0x70),
    "jcc32":    _cc(1, 0x80),
    "jmp8":     [(0, 0x00FF, 0x00EB, None)],
    "jmp32":    [(0, 0x00FF, 0x00E9, None)],
    "call":     [(0, 0x00FF, 0x00E8, None)],
    "ret":      [(0, 0x00FF, 0x00C3, None)],
    "push":     [(0, 0x00F8, 0x0050, None)],
    "pop":      [(0, 0x00F8, 0x0058, None)],
    "pushi":    [(0, 0x00FD, 0x0068, None)],                                   # 68 imm32, 6A imm8
    "leave":    [(0, 0x00FF, 0x00C9, None)],
    "nop":      [(0, 0x00FF, 0x0090, None)],
    "nopm":     [(1, 0x38FF, 0x001F, None)],                                   # 0F 1F /0: multi-byte NOP
    "hlt":      [(0, 0x00FF, 0x00F4, None)],                                   # end of run
}
ALL_CLASSES = tuple(KINDS)
MEMOPS = ()                    # every x86-64 run uses data memory: the return address is on the stack
MODRM = ("alu_rm", "alu_imm", "test_rm", "test_imm", "mov_rm", "mov_imm", "lea", "movsxd", "movx8", "not", "neg", "div",
         "incdec", "callr", "jmpr", "pushrm", "imul_rm", "imul_imm", "shift", "setcc", "cmov", "nopm")
S8_OP0 = ("alu_rm", "alu_acc", "test_rm", "test_acc", "test_imm", "mov_rm", "mov_imm")   # opcode bit 0 = 0: byte operands
D1 = ("lea", "movsxd", "movx8", "imul_rm", "imul_imm", "cmov")                            # the reg field is the destination
REG8_REG = ("alu_rm", "test_rm", "mov_rm")                                                # byte forms that use the reg field
CC_FLAGS = {0: (OF,), 1: (CF,), 2: (ZF,), 3: (CF, ZF), 4: (SF,), 6: (SF, OF), 7: (ZF, SF, OF)}


def sext(v, n):
    v &= (1 << n) - 1
    return v - (1 << n) if v >> (n - 1) else v


def decode(b):
    """Parse one instruction from the byte sequence b (at least 15 bytes; shorter input is zero-padded).
    Returns a dict; d['ok'] is False for anything the machine does not implement (d['why'] says why)."""
    b = list(b[:15]) + [0] * 24
    pre = 0                                                  # alignment-NOP prefixes: 66 ... 66 [2E]
    while pre < MAX66 and b[pre] == 0x66:
        pre += 1
    pre += b[pre] == 0x2E
    b = b[pre:] + [0] * pre
    i = hasrex = rex = 0
    if b[0] & 0xF0 == 0x40:
        rex, hasrex, i = b[0] & 15, 1, 1
    esc = int(b[i] == 0x0F)
    op = b[i + esc]
    r = b[i + esc + 1:]
    w, rr, rx, rb = rex >> 3 & 1, rex >> 2 & 1, rex >> 1 & 1, rex & 1
    key = op | r[0] << 8
    kind = next((n for n, ps in KINDS.items() if any(e == esc and key & m == v and (q is None or q == w) for e, m, v, q in ps)), None)
    d = dict(kind=kind, ok=kind is not None, why="instruction not implemented", op=op, esc=esc, hasrex=hasrex, w=w, length=1,
             pre=pre)
    if (pre and (kind not in ("nop", "nopm") or hasrex)) or (kind is None and b[0] in PREFIXES):
        d.update(ok=False, why="prefix byte (16-bit operand size, REP, segment, LOCK) not implemented"
                 + (" (alignment NOPs only)" if pre else ""))
        return d
    if kind is None:
        return d
    modrm = kind in MODRM
    s8 = (kind in S8_OP0 and not op & 1) or (kind == "alu_imm" and op == 0x80) or (kind == "mov_ri" and not op & 8)
    d1 = (kind in ("alu_rm", "mov_rm") and bool(op & 2)) or kind in D1
    isz = {"alu_acc": 1 if s8 else 4, "alu_imm": 4 if op == 0x81 else 1, "test_acc": 1 if s8 else 4, "test_imm": 1 if s8 else 4,
           "mov_imm": 1 if s8 else 4, "mov_ri": 1 if s8 else 8 if w else 4, "imul_imm": 1 if op & 2 else 4,
           "shift": 1 if op == 0xC1 else 0, "jcc8": 1, "jmp8": 1, "jcc32": 4, "jmp32": 4, "call": 4,
           "pushi": 1 if op & 2 else 4}.get(kind, 0)
    mod, reg, rm = r[0] >> 6, r[0] >> 3 & 7, r[0] & 7
    mem = modrm and mod != 3
    sib = int(mem and rm == 4)
    scale, index, base = r[1] >> 6, r[1] >> 3 & 7, r[1] & 7
    dsz = 0 if not mem else 1 if mod == 1 else 4 if mod == 2 or (mod == 0 and (base if sib else rm) == 5) else 0
    o = 1 + sib
    disp = sext(r[o], 8) if dsz == 1 else sext(int.from_bytes(bytes(r[o:o + 4]), "little"), 32) if dsz == 4 else 0
    o = int(modrm) + sib + dsz
    imm = sext(int.from_bytes(bytes(r[o:o + isz]), "little"), 8 * isz) if isz else 0
    d.update(modrm=modrm, s8=bool(s8), d1=bool(d1), isz=isz, mod=mod, reg=reg | rr << 3, rm=rm | rb << 3, mem=bool(mem), sib=sib,
             scale=scale if sib else 0, index=index | rx << 3, base=(base if sib else rm) | rb << 3, dsz=dsz, disp=disp, imm=imm,
             opreg=op & 7 | rb << 3, riprel=bool(mem and mod == 0 and not sib and rm == 5),
             nobase=bool(mem and mod == 0 and sib and base == 5), noindex=not sib or (index | rx << 3) == 4,
             digit=reg, cc=op & 15, length=pre + hasrex + esc + 1 + int(modrm) + sib + dsz + isz)
    d["port"] = d["base"] if modrm else d["opreg"]           # register selected by the "base" read port
    bad = None
    if s8 and modrm and mem:
        bad = "byte memory operand not implemented"
    elif kind == "lea" and not mem:
        bad = "LEA with a register operand is not a valid instruction"
    elif not hasrex and ((((s8 and modrm) or kind in ("movx8", "setcc")) and rm >= 4) or (s8 and kind in REG8_REG and reg >= 4)
                         or (s8 and kind == "mov_ri" and op & 7 >= 4)):
        bad = "AH/CH/DH/BH byte registers not implemented"
    elif kind == "pop" and d["opreg"] == RSP:
        bad = "POP RSP not implemented"
    if bad:
        d.update(ok=False, why=bad)
    return d


def registers(d):
    """Register numbers an instruction reads or writes, explicitly or implicitly."""
    if not d["ok"]:
        return set()
    k, s = d["kind"], set()
    if d["modrm"]:
        if d["mem"]:
            s |= set() if d["riprel"] or d["nobase"] else {d["base"]}
            s |= set() if d["noindex"] else {d["index"]}
        else:
            s.add(d["rm"])
        if k in ("alu_rm", "test_rm", "mov_rm", "lea", "movsxd", "movx8", "imul_rm", "imul_imm", "cmov"):
            s.add(d["reg"])
    if k in ("mov_ri", "push", "pop"):
        s.add(d["opreg"])
    if k in ("alu_acc", "test_acc", "cdqe"):
        s.add(RAX)
    if k in ("div", "cdq"):
        s |= {RAX, RDX}
    if k == "shift" and d["op"] == 0xD3:
        s.add(RCX)
    if k in ("push", "pop", "pushrm", "pushi", "call", "callr", "ret", "leave"):
        s.add(RSP)
    if k == "leave":
        s.add(RBP)
    return s


def check(d):
    """Static support check of a decoded instruction."""
    if not d["ok"]:
        return Check(None, False, d["why"])
    return Check(d["kind"], True, reads=tuple(sorted(registers(d))), writes=tuple(sorted(registers(d))))


def listing(code, slots, base=0):
    """Linear disassembly of the code image: one entry per instruction."""
    try:
        from capstone import Cs, CS_ARCH_X86, CS_MODE_64
        md = Cs(CS_ARCH_X86, CS_MODE_64)
    except ImportError:
        md = None
    out, i, code = [], 0, bytes(code)
    while i < len(code):
        d = decode(code[i:i + 15])
        ins = next(iter(md.disasm(code[i:i + 15], base + i, 1)), None) if md else None
        n = ins.size if ins is not None else d["length"]
        c = check(d)
        if c.ok and d["kind"] in ("jcc8", "jcc32", "jmp8", "jmp32", "call") and not 0 <= i + d["length"] + d["imm"] < slots:
            c = Check(d["kind"], False, f"branch target {i + d['length'] + d['imm']} outside the loaded code")
        if c.ok and ins is not None and n != d["length"]:
            c = Check(d["kind"], False, "decoder length differs from the disassembler's")
        out.append(dict(index=i, size=n, word=code[i:i + n].hex(), check=c,
                        asm=f"{ins.mnemonic} {ins.op_str}".strip() if ins is not None else ".byte 0x%02x" % code[i]))
        i += max(1, n)
    return out


def loads_rm(d):
    """True if the instruction reads its memory operand (a plain store, LEA and NOP do not)."""
    return d["mem"] and d["kind"] not in ("lea", "nopm", "mov_imm") and not (d["kind"] == "mov_rm" and not d["d1"])


def cond(cc, f):
    base = [f[OF], f[CF], f[ZF], f[CF] | f[ZF], f[SF], 0, f[SF] ^ f[OF], f[ZF] | (f[SF] ^ f[OF])][cc >> 1]
    return base ^ (cc & 1)


def alu(aop, a, b, n):
    """n-bit ADD (0) OR (1) AND (4) SUB (5) XOR (6) CMP (7): result and [ZF, SF, CF, OF]."""
    m = (1 << n) - 1
    a, b = a & m, b & m
    if aop == 0:
        full = a + b
        r, cf = full & m, full >> n
        of = ((a ^ r) & (b ^ r)) >> (n - 1) & 1
    elif aop in (5, 7):
        full = a + (~b & m) + 1
        r, cf = full & m, 1 - (full >> n)
        of = ((a ^ b) & (a ^ r)) >> (n - 1) & 1
    else:
        r, cf, of = {1: a | b, 4: a & b, 6: a ^ b}[aop], 0, 0
    return r, [int(r == 0), r >> (n - 1) & 1, cf, of]


def model(d, W, reg, flags, load, pc, slots):
    """What the W-bit machine does for the decoded instruction d, in plain Python.

    reg(k) -> W-bit register value; flags = [ZF, SF, CF, OF] as the machine holds them; load(i) -> W-bit
    value of data cell i (cells are 4 bytes); pc = byte offset of the instruction.
    Returns dict(regs={k: value}, flags={index: bit}, unknown={flag indices left unmodelled},
    uses={flag indices read}, stores=[(cell, value)], loads=[cell], npc, trap)."""
    mk = (1 << W) - 1
    sx = lambda v: sext(v, W)
    k, op = d["kind"], d["op"]
    nxt = (pc + d["length"]) & (slots - 1)
    out = dict(regs={}, flags={}, unknown=set(), uses=set(), stores=[], loads=[], npc=nxt & (slots - 1), trap=False)
    ds = 64 if d["w"] else 32
    wide = d["w"] or k in ("push", "pop", "pushrm", "pushi", "call", "callr", "ret", "leave", "jmpr")     # 8-byte memory access

    def rd(cell):
        out["loads"].append(cell)
        return load(cell) & mk

    def wr(addr, v):
        out["stores"].append((addr >> 2, v & mk))
        if wide:
            out["stores"].append(((addr >> 2) + 1, mk if v >> (W - 1) & 1 else 0))
    ea = None
    if d["modrm"] and d["mem"]:
        basev = nxt if d["riprel"] else 0 if d["nobase"] else reg(d["base"])
        ea = (basev + (0 if d["noindex"] else reg(d["index"]) << d["scale"]) + d["disp"]) & mk
    rmv = 0
    if d["modrm"] and not d["mem"]:
        rmv = reg(d["rm"])
    elif d["modrm"] and loads_rm(d):
        rmv = rd(ea >> 2)
    regv, imm = reg(d["reg"]) if d["modrm"] else 0, d["imm"] & mk

    def put_rm(v):                       # destination: the r/m operand
        if d["mem"]:
            wr(ea, v)
        else:
            out["regs"][d["rm"]] = v & mk

    def merge8(old, v):                  # byte write: the upper bits of the register are kept
        return (old & ~0xFF | v & 0xFF) & mk

    def push(v):
        a = (reg(RSP) - 8) & mk
        wr(a, v)
        out["regs"][RSP] = a
    n = 8 if d["s8"] else W
    if k in ("alu_rm", "alu_acc", "alu_imm", "test_rm", "test_acc", "test_imm", "neg", "incdec"):
        aop = 4 if k.startswith("test") else 5 if k == "neg" else (5 if d["digit"] & 1 else 0) if k == "incdec" else \
            d["digit"] if k == "alu_imm" else op >> 3 & 7
        dst = "acc" if k.endswith("acc") else "reg" if d["d1"] else "rm"
        a = reg(RAX) if dst == "acc" else regv if dst == "reg" else rmv
        b = imm if k in ("alu_acc", "alu_imm", "test_acc", "test_imm") else rmv if dst == "reg" else regv
        if k == "neg":
            a, b = 0, rmv
        if k == "incdec":
            b = 1
        res, fl = alu(aop, a, b, n)
        out["flags"] = {i: fl[i] for i in ((ZF, SF, OF) if k == "incdec" else range(4))}
        if aop != 7 and not k.startswith("test"):
            old = rmv if k == "neg" else a
            v = merge8(old, res) if d["s8"] else res
            if dst == "acc":
                out["regs"][RAX] = v
            elif dst == "reg":
                out["regs"][d["reg"]] = v
            else:
                put_rm(v)
    elif k == "mov_rm":
        if d["d1"]:
            out["regs"][d["reg"]] = merge8(regv, rmv) if d["s8"] else rmv
        else:
            put_rm(merge8(rmv, regv) if d["s8"] else regv)
    elif k == "mov_imm":
        put_rm(merge8(rmv, imm) if d["s8"] else imm)
    elif k == "mov_ri":
        out["regs"][d["opreg"]] = merge8(reg(d["opreg"]), imm) if d["s8"] else imm
    elif k == "lea":
        out["regs"][d["reg"]] = ea
    elif k == "movsxd":
        out["regs"][d["reg"]] = rmv
    elif k == "movx8":
        out["regs"][d["reg"]] = (sext(rmv, 8) if op & 8 else rmv & 0xFF) & mk
    elif k == "not":
        put_rm(~rmv)
    elif k in ("imul_rm", "imul_imm"):
        out["regs"][d["reg"]] = (regv if k == "imul_rm" else imm) * rmv & mk
        out["unknown"] = {ZF, SF, CF, OF}
    elif k == "div":
        a = reg(RAX)
        if rmv == 0:
            out["trap"] = True
        elif d["digit"] & 1:
            x, y = sx(a), sx(rmv)
            q = abs(x) // abs(y) * (1 if (x < 0) == (y < 0) else -1)
            out["regs"] = {RAX: q & mk, RDX: (x - q * y) & mk}
        else:
            out["regs"] = {RAX: a // rmv, RDX: a % rmv}
        out["unknown"] = {ZF, SF, CF, OF}
    elif k == "cdq":
        out["regs"][RDX] = mk if reg(RAX) >> (W - 1) & 1 else 0
    elif k == "shift":
        c = (imm if op == 0xC1 else 1 if op == 0xD1 else reg(RCX)) & (ds - 1)
        v = (rmv << c) if not d["digit"] & 1 else (sx(rmv) >> c) if d["digit"] & 2 else ((sx(rmv) & ((1 << ds) - 1)) >> c)
        put_rm(v)
        if c:
            out["flags"] = {ZF: int(v & mk == 0), SF: v >> (W - 1) & 1}
            out["unknown"] = {CF, OF}
    elif k in ("setcc", "cmov", "jcc8", "jcc32"):
        out["uses"] = set(CC_FLAGS[d["cc"] >> 1])
        c = cond(d["cc"], flags)
        if k == "setcc":
            out["regs"][d["rm"]] = merge8(rmv, c)
        elif k == "cmov":
            out["regs"][d["reg"]] = rmv if c else regv
        elif c:
            out["npc"] = (nxt + d["imm"]) & (slots - 1)
    elif k in ("jmp8", "jmp32"):
        out["npc"] = (nxt + d["imm"]) & (slots - 1)
    elif k == "jmpr":
        out["npc"] = rmv & (slots - 1)
    elif k in ("call", "callr"):
        out["npc"] = (rmv if k == "callr" else nxt + d["imm"]) & (slots - 1)
        push(nxt)
    elif k == "ret":
        out["npc"] = rd(reg(RSP) >> 2) & (slots - 1)
        out["regs"][RSP] = (reg(RSP) + 8) & mk
    elif k in ("push", "pushrm", "pushi"):
        push(reg(d["opreg"]) if k == "push" else rmv if k == "pushrm" else imm)
    elif k == "pop":
        out["regs"][RSP] = (reg(RSP) + 8) & mk
        out["regs"][d["opreg"]] = rd(reg(RSP) >> 2)
    elif k == "leave":
        out["regs"] = {RSP: (reg(RBP) + 8) & mk, RBP: rd(reg(RBP) >> 2)}
    elif k == "hlt":
        out["npc"] = pc
    if out["trap"]:
        out.update(regs={}, flags={}, stores=[], npc=pc)
    return out
