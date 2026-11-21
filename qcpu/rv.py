# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Quantag IT Solutions GmbH
"""Classical side of the RISC-V front end (RV32IM and RV64IM, 32-bit instructions only, no compressed
instructions): instruction classes, static support check, disassembly, and a W-bit model of every
implemented instruction.

The class table lists exact (mask, value) patterns. The quantum decoder in rv_machine.py matches
exactly these patterns, so an instruction word that is not listed here sets no class in the circuit
and traps: the circuit itself rejects what the checker rejects.
"""
from .a64 import Check, bits


def classes(xlen):
    """class name -> list of (mask, value) patterns; all patterns are mutually exclusive."""
    sh = 0xFC00707F if xlen == 64 else 0xFE00707F          # shift by immediate: 6 / 5 bits of shamt
    R = 0xFE00707F
    c = {
        "lui":    [(0x7F, 0x37)],
        "auipc":  [(0x7F, 0x17)],
        "jal":    [(0x20007F, 0x6F)],                        # imm[1] = 0: target is 4-byte aligned
        "jalr":   [(0x707F, 0x67)],
        "branch": [(0x617F, 0x0063), (0x417F, 0x4063)],      # beq bne | blt bge bltu bgeu; imm[1] = 0
        "opimm":  [(0x107F, 0x0013), (0x307F, 0x3013)],      # addi slti xori ori | sltiu andi
        "shimm":  [(sh, 0x00001013), (sh, 0x00005013), (sh, 0x40005013)],                  # slli srli srai
        "op":     [(0xFE00107F, 0x00000033), (0xFE00307F, 0x00003033), (R, 0x40000033)],   # add slt xor or | sltu and | sub
        "shreg":  [(R, 0x00001033), (R, 0x00005033), (R, 0x40005033)],                     # sll srl sra
        "mul":    [(R, 0x02000033)],                                                       # mul
        "div":    [(0xFE00407F, 0x02004033)],                                              # div divu rem remu
        "load":   [(0x707F, 0x2003)],                                                      # lw
        "store":  [(0x707F, 0x2023)],                                                      # sw
        "ebreak": [(0xFFFFFFFF, 0x00100073)],                                              # end of run
    }
    if xlen == 64:
        c["opimm"] += [(0x707F, 0x001B)]                                                   # addiw
        c["shimm"] += [(R, 0x0000101B), (R, 0x0000501B), (R, 0x4000501B)]                  # slliw srliw sraiw
        c["op"] += [(R, 0x0000003B), (R, 0x4000003B)]                                      # addw subw
        c["shreg"] += [(R, 0x0000103B), (R, 0x0000503B), (R, 0x4000503B)]                  # sllw srlw sraw
        c["mul"] += [(R, 0x0200003B)]                                                      # mulw
        c["div"] += [(0xFE00407F, 0x0200403B)]                                             # divw divuw remw remuw
        c["load"] += [(0x707F, 0x3003), (0x707F, 0x6003)]                                  # ld lwu
        c["store"] += [(0x707F, 0x3023)]                                                   # sd
    return c


ALL_CLASSES = tuple(classes(64))
MEMOPS = ("load", "store")
ABI = ["zero", "ra", "sp", "gp", "tp", "t0", "t1", "t2", "s0", "s1"] + [f"a{i}" for i in range(8)] + \
      [f"s{i}" for i in range(2, 12)] + [f"t{i}" for i in range(3, 7)]
EBREAK = 0x00100073


def classify(w, xlen):
    for name, pats in classes(xlen).items():
        if any(w & m == v for m, v in pats):
            return name
    return None


def sext(v, n):
    v &= (1 << n) - 1
    return v - (1 << n) if v >> (n - 1) else v


def imm_b(w):
    return sext(bits(w, 31, 1) << 12 | bits(w, 7, 1) << 11 | bits(w, 25, 6) << 5 | bits(w, 8, 4) << 1, 13)


def imm_j(w):
    return sext(bits(w, 31, 1) << 20 | bits(w, 12, 8) << 12 | bits(w, 20, 1) << 11 | bits(w, 21, 10) << 1, 21)


def check(w, pc_index, n_instr, xlen=64, enabled=None):
    """Static support check of one instruction word at instruction index pc_index."""
    c = classify(w, xlen)
    rd, f3, rs1, rs2 = bits(w, 7, 5), bits(w, 12, 3), bits(w, 15, 5), bits(w, 20, 5)
    nz = lambda *rs: tuple(r for r in rs if r != 0)
    if c is None:
        op = w & 0x7F
        if w & 3 != 3:
            why = "compressed (16-bit) instruction: compile with -march=rv64im / rv32im (no 'c')"
        elif op in (0x03, 0x23):
            why = "byte/halfword load/store not implemented (32/64-bit only)" if xlen == 64 or f3 < 2 else \
                "64-bit load/store on RV32"
        elif op in (0x33, 0x3B) and bits(w, 25, 7) == 1:
            why = "mulh/mulhsu/mulhu (high half of a product) not implemented"
        elif op in (0x1B, 0x3B) and xlen == 32:
            why = "RV64-only instruction in RV32 code"
        elif op == 0x73:
            why = "ecall / CSR instruction not implemented"
        elif op in (0x07, 0x27, 0x43, 0x47, 0x4B, 0x4F, 0x53):
            why = "floating-point instruction not implemented"
        elif op == 0x2F:
            why = "atomic instruction not implemented"
        elif op in (0x63, 0x6F):
            why = "branch target is not 4-byte aligned"
        else:
            why = "instruction not implemented"
        return Check(None, False, why)
    if enabled is not None and c not in enabled:
        return Check(c, False, f"class '{c}' disabled in config")
    if c in ("branch", "jal"):
        tgt = pc_index + (imm_b(w) if c == "branch" else imm_j(w)) // 4
        if not 0 <= tgt < n_instr:
            return Check(c, False, f"branch target {tgt} outside the loaded code")
    reads = {"lui": (), "auipc": (), "jal": (), "ebreak": (), "jalr": nz(rs1), "opimm": nz(rs1), "shimm": nz(rs1),
             "load": nz(rs1)}.get(c, nz(rs1, rs2))
    writes = () if c in ("branch", "store", "ebreak") else nz(rd)
    return Check(c, True, reads=reads, writes=writes)


def disassemble(code, base=0, xlen=64):
    try:
        from capstone import Cs, CS_ARCH_RISCV, CS_MODE_RISCV64, CS_MODE_RISCV32
        md = Cs(CS_ARCH_RISCV, CS_MODE_RISCV64 if xlen == 64 else CS_MODE_RISCV32)
        out = []
        for i in range(0, len(code), 4):
            ins = list(md.disasm(code[i:i + 4], base + i))
            out.append(f"{ins[0].mnemonic} {ins[0].op_str}".strip() if ins else ".word 0x%08x" % int.from_bytes(code[i:i + 4], "little"))
        return out
    except ImportError:
        return [".word 0x%08x" % int.from_bytes(code[i:i + 4], "little") for i in range(0, len(code), 4)]


def model(w, cls, xlen, W, reg, pc, load, slots):
    """What the W-bit machine computes for instruction word w of class cls, in plain Python.

    reg(k) -> the W-bit value the machine holds in register k (0 for x0); load(cell) -> W-bit cell.
    Returns dict(rd=None | W-bit value written to register bits(w, 7, 5), npc=next instruction index,
    addr=None | W-bit effective address, stores=[(cell offset from addr >> 2, W-bit value)]).
    The CPU reference compares this, instruction by instruction, with what the CPU did: where they
    agree and the CPU's values fit the model, the machine state stays the encoding of the CPU state."""
    mk = (1 << W) - 1
    sx = lambda v: sext(v, W)
    f3, rs1, rs2, alt = bits(w, 12, 3), bits(w, 15, 5), bits(w, 20, 5), bits(w, 30, 1)
    ds = 32 if xlen == 32 or bits(w, 3, 1) else 64          # W-forms (opcode bit 3) work on 32 bits
    a, b = reg(rs1), reg(rs2)
    ii = sext(bits(w, 20, 12), 12) & mk
    out = dict(rd=None, npc=(pc + 1) & (slots - 1), addr=None, stores=[])
    if cls == "lui":
        out["rd"] = (w & 0xFFFFF000) & mk
    elif cls == "auipc":
        out["rd"] = (4 * pc + (w & 0xFFFFF000)) & mk
    elif cls == "jal":
        out["rd"] = 4 * (pc + 1) & mk
        out["npc"] = (pc + imm_j(w) // 4) & (slots - 1)
    elif cls == "jalr":
        out["rd"] = 4 * (pc + 1) & mk
        out["npc"] = (((a + ii) & mk) >> 2) & (slots - 1)
    elif cls == "branch":
        lt_s, lt_u, eq = sx(a) < sx(b), a < b, a == b
        base = eq if f3 < 4 else lt_s if f3 < 6 else lt_u
        if bool(base) != bool(f3 & 1):
            out["npc"] = (pc + imm_b(w) // 4) & (slots - 1)
    elif cls in ("opimm", "op"):
        y = ii if cls == "opimm" else b
        out["rd"] = [(a - y) if cls == "op" and alt else (a + y), None, int(sx(a) < sx(y)), int(a < y),
                     a ^ y, None, a | y, a & y][f3] & mk
    elif cls in ("shimm", "shreg"):
        k = bits(w, 20, 6) if cls == "shimm" else b & (ds - 1)
        if f3 == 1:
            out["rd"] = (a << k) & mk
        elif alt:
            out["rd"] = (sx(a) >> k) & mk
        else:
            out["rd"] = ((sx(a) & ((1 << ds) - 1)) >> k) & mk       # logical shift of the ds-bit value
    elif cls == "mul":
        out["rd"] = (a * b) & mk
    elif cls == "div":
        if b == 0:
            q, r = mk, a
        elif f3 & 1:
            q, r = a // b, a % b
        else:
            x, y = sx(a), sx(b)
            q = abs(x) // abs(y) * (1 if (x < 0) == (y < 0) else -1)
            r = x - q * y
        out["rd"] = (r if f3 & 2 else q) & mk
    elif cls == "load":
        out["addr"] = (a + ii) & mk
        out["rd"] = load(out["addr"] >> 2) & mk
    elif cls == "store":
        out["addr"] = (a + (sext(bits(w, 25, 7) << 5 | bits(w, 7, 5), 12) & mk)) & mk
        out["stores"] = [(0, b)] + ([(1, mk if b >> (W - 1) else 0)] if f3 == 3 else [])
    elif cls == "ebreak":
        out["npc"] = pc
    return out
