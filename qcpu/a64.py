# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Quantag IT Solutions GmbH
"""Classical side of the A64 front end: instruction classes, static support check, disassembly.

The same (mask, value) class table drives the *quantum* decoder in machine.py, so what the
report calls "supported" is exactly what the quantum circuit decodes. This module adds the
finer checks (unallocated / not-implemented sub-forms) that the loader performs before running.
"""
from dataclasses import dataclass

# class name -> (mask, value).  Bit 31 (sf, 32/64-bit) is generally left free.
CLASSES = {
    # data processing
    "movw":        (0x1F800000, 0x12800000),  # MOVN / MOVZ / MOVK
    "addsub_imm":  (0x1F800000, 0x11000000),  # ADD/ADDS/SUB/SUBS #imm (CMP, CMN, MOV to/from SP)
    "addsub_reg":  (0x1F200000, 0x0B000000),  # ADD/ADDS/SUB/SUBS shifted register (CMP, NEG)
    "addsub_ext":  (0x1FE00000, 0x0B200000),  # ADD/SUB extended register (..., w1, sxtw #2)
    "logic_reg":   (0x1F000000, 0x0A000000),  # AND/BIC/ORR/ORN/EOR/EON/ANDS/BICS (MOV, MVN, TST)
    "logic_imm":   (0x1F800000, 0x12000000),  # AND/ORR/EOR/ANDS #bitmask (TST, MOV)
    "bfm":         (0x3F800000, 0x13000000),  # SBFM/UBFM: LSL/LSR/ASR #, UXTB/H, SXTB/H/W, UBFX, SBFX
    "shiftv":      (0x7FE0F000, 0x1AC02000),  # LSLV/LSRV/ASRV
    "div":         (0x7FE0F800, 0x1AC00800),  # UDIV/SDIV
    "csel":        (0x3FE00800, 0x1A800000),  # CSEL/CSINC/CSINV/CSNEG (CSET, CSETM, CINC, CNEG)
    "ccmp":        (0x3FE00410, 0x3A400000),  # CCMP/CCMN (register and immediate)
    "madd":        (0x7FE00000, 0x1B000000),  # MADD/MSUB (MUL, MNEG)
    "maddl":       (0xFF600000, 0x9B200000),  # SMADDL/UMADDL/SMSUBL/UMSUBL (SMULL, UMULL)
    # branches
    "b":           (0xFC000000, 0x14000000),  # B
    "bl":          (0xFC000000, 0x94000000),  # BL
    "bcond":       (0xFF000010, 0x54000000),  # B.cond
    "cbz":         (0x7E000000, 0x34000000),  # CBZ/CBNZ
    "tbz":         (0x7E000000, 0x36000000),  # TBZ/TBNZ
    "bra":         (0xFF9FFC1F, 0xD61F0000),  # BR / BLR / RET
    "hlt":         (0xFFE0001F, 0xD4400000),  # HLT (end of run)
    "nop":         (0xFFFFF01F, 0xD503201F),  # NOP and other hints
    "adrp":        (0x9F000000, 0x90000000),  # ADRP (page address of a global / string literal)
    # loads / stores of 32/64-bit values: LDR/STR Wt|Xt, LDRSW (byte/halfword forms: not implemented)
    "ldst_ui":     (0xBB000000, 0xB9000000),  # [Xn|SP, #imm]  (unsigned, scaled offset)
    "ldst_i9":     (0xBB200000, 0xB8000000),  # LDUR/STUR, pre-index [Xn, #imm]!, post-index [Xn], #imm
    "ldst_reg":    (0xBB200C00, 0xB8200800),  # [Xn, Xm{, lsl #s}] / [Xn, Wm, sxtw|uxtw {#s}]
    "ld_pair":     (0x7E400000, 0x28400000),  # LDP  Wt/Xt (offset, pre-, post-index)
    "st_pair":     (0x7E400000, 0x28000000),  # STP  Wt/Xt (offset, pre-, post-index)
}
ALL_CLASSES = tuple(CLASSES)
SINGLE = ("ldst_ui", "ldst_i9", "ldst_reg")
PAIRS = ("ld_pair", "st_pair")
MEMOPS = SINGLE + PAIRS


def is_load(w, cls):
    return bits(w, 22, 1) == 1 if cls in PAIRS else bits(w, 22, 2) != 0


def bits(w, lo, n):
    return (w >> lo) & ((1 << n) - 1)


def classify(w):
    for name, (mask, val) in CLASSES.items():
        if w & mask == val:
            return name
    return None


@dataclass
class Check:
    cls: str | None
    ok: bool
    reason: str = ""
    reads: tuple = ()      # architectural registers read (31 = SP where SP context)
    writes: tuple = ()
    uses_sp: bool = False


def _decode_bitmask_valid(n, imms, sf):
    if not sf and n:
        return False
    v = (n << 6) | (~imms & 0x3F)
    if v == 0:
        return False
    length = v.bit_length() - 1
    if length < 1:
        return False
    levels = (1 << length) - 1
    return (imms & levels) != levels


def datasize(w, cls):
    """Width (32/64) of the value an instruction produces/consumes in its registers."""
    if cls == "maddl":
        return 64
    if cls in PAIRS:
        return 64 if bits(w, 31, 1) else 32
    if cls in SINGLE:
        size, opc = bits(w, 30, 2), bits(w, 22, 2)
        return 64 if size == 3 or opc == 2 else 32
    if cls == "adrp":
        return 64
    return 64 if bits(w, 31, 1) else 32


def check(w, pc_index, n_instr, enabled=ALL_CLASSES):
    """Static support check of one instruction word at instruction index pc_index."""
    c = classify(w)
    rd, rn, rm, ra = bits(w, 0, 5), bits(w, 5, 5), bits(w, 16, 5), bits(w, 10, 5)
    sf = bits(w, 31, 1)
    zr = lambda *rs: tuple(r for r in rs if r != 31)
    if c is None:
        if w & 0xBB000000 in (0x39000000, 0x38000000, 0x19000000, 0x18000000) and not bits(w, 26, 1):
            return Check(None, False, "byte/halfword load/store not implemented (32/64-bit only)")
        return Check(None, False, "instruction not implemented")
    if c not in enabled:
        return Check(c, False, f"class '{c}' disabled in config")
    if c == "movw":
        if bits(w, 29, 2) == 1:
            return Check(c, False, "unallocated")
        return Check(c, True, reads=zr(rd) if bits(w, 29, 2) == 3 else (), writes=zr(rd))
    if c in ("addsub_imm", "addsub_ext"):
        s = bits(w, 29, 1)
        if c == "addsub_ext" and (bits(w, 10, 3) > 4 or bits(w, 22, 2)):
            return Check(c, False, "unallocated")
        sp_rn, sp_rd = rn == 31, rd == 31 and not s
        reads = ((31,) if sp_rn else (rn,)) + (zr(rm) if c == "addsub_ext" else ())
        writes = (31,) if sp_rd else zr(rd)
        return Check(c, True, reads=reads, writes=writes, uses_sp=sp_rn or sp_rd)
    if c == "addsub_reg":
        if bits(w, 22, 2) == 3:
            return Check(c, False, "reserved shift type")
        if not sf and bits(w, 15, 1):
            return Check(c, False, "unallocated shift amount")
        return Check(c, True, reads=zr(rn, rm), writes=zr(rd))
    if c == "logic_reg":
        if bits(w, 22, 2) == 3:
            return Check(c, False, "ROR shifted operand not implemented")
        if not sf and bits(w, 15, 1):
            return Check(c, False, "unallocated shift amount")
        return Check(c, True, reads=zr(rn, rm), writes=zr(rd))
    if c == "logic_imm":
        if not _decode_bitmask_valid(bits(w, 22, 1), bits(w, 10, 6), sf):
            return Check(c, False, "reserved bitmask immediate")
        if rd == 31 and bits(w, 29, 2) != 3:
            return Check(c, False, "write to SP from logical immediate not implemented")
        return Check(c, True, reads=zr(rn), writes=zr(rd))
    if c == "bfm":
        if bits(w, 22, 1) != sf:
            return Check(c, False, "unallocated (N != sf)")
        if not sf and (bits(w, 21, 1) or bits(w, 15, 1)):
            return Check(c, False, "unallocated")
        return Check(c, True, reads=zr(rn), writes=zr(rd))
    if c == "shiftv":
        if bits(w, 10, 2) == 3:
            return Check(c, False, "RORV not implemented")
        return Check(c, True, reads=zr(rn, rm), writes=zr(rd))
    if c in ("div", "csel", "madd", "maddl"):
        if c == "csel" and bits(w, 29, 1):
            return Check(c, False, "unallocated")
        return Check(c, True, reads=zr(rn, rm) + (zr(ra) if c in ("madd", "maddl") else ()), writes=zr(rd))
    if c == "ccmp":
        return Check(c, True, reads=zr(rn) + (() if bits(w, 11, 1) else zr(rm)))
    if c in ("b", "bl", "bcond", "cbz", "tbz"):
        lo, nb = {"b": (0, 26), "bl": (0, 26), "tbz": (5, 14)}.get(c, (5, 19))
        off = bits(w, lo, nb)
        if off >> (nb - 1):
            off -= 1 << nb
        tgt = pc_index + off
        if not 0 <= tgt < n_instr:
            return Check(c, False, f"branch target {tgt} outside the loaded code")
        return Check(c, True, reads=zr(rd) if c in ("cbz", "tbz") else (), writes=(30,) if c == "bl" else ())
    if c == "bra":
        k = bits(w, 21, 2)
        if k == 3:
            return Check(c, False, "unallocated")
        return Check(c, True, reads=zr(rn), writes=(30,) if k == 1 else ())
    if c in ("hlt", "nop"):
        return Check(c, True)
    if c == "adrp":
        return Check(c, True, writes=zr(rd))
    if c in MEMOPS:
        if c in SINGLE:
            size, opc = bits(w, 30, 2), bits(w, 22, 2)
            if size == 3 and opc >= 2:
                return Check(c, False, "prefetch (PRFM) not implemented" if opc == 2 else "unallocated")
            if size == 2 and opc == 3:
                return Check(c, False, "unallocated")
        if c == "ldst_i9" and bits(w, 10, 2) == 2:
            return Check(c, False, "unprivileged load/store not implemented")
        if c == "ldst_reg" and bits(w, 13, 3) not in (2, 3, 6, 7):
            return Check(c, False, "register offset extend must be UXTW/LSL/SXTW/SXTX")
        wb = (c == "ldst_i9" and bits(w, 10, 2) in (1, 3)) or (c in PAIRS and bits(w, 23, 2) in (1, 3))
        base = (31,) if rn == 31 else (rn,)
        rt2 = bits(w, 10, 5) if c in PAIRS else None
        reads = base + (zr(rm) if c == "ldst_reg" else ())
        writes = base if wb else ()
        if is_load(w, c):
            writes += zr(rd) + (zr(rt2) if rt2 is not None else ())
        else:
            reads += zr(rd) + (zr(rt2) if rt2 is not None else ())
        return Check(c, True, reads=reads, writes=writes, uses_sp=rn == 31)
    return Check(c, False, "?")


def disassemble(code, base=0):
    try:
        from capstone import Cs, CS_ARCH_ARM64, CS_MODE_ARM
        md = Cs(CS_ARCH_ARM64, CS_MODE_ARM)
        out = []
        for i in range(0, len(code), 4):
            ins = list(md.disasm(code[i:i + 4], base + i))
            out.append(f"{ins[0].mnemonic} {ins[0].op_str}".strip() if ins else ".inst 0x%08x" % int.from_bytes(code[i:i+4], "little"))
        return out
    except ImportError:
        return [".inst 0x%08x" % int.from_bytes(code[i:i + 4], "little") for i in range(0, len(code), 4)]
