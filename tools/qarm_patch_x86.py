# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Quantag IT Solutions GmbH
"""x86-64 for tools/qarm_patch.py: the trampoline, the patch point, and the scan for brute-force loops.

At FUNC (after an ENDBR64, kept in place) the first whole instructions covering 5 bytes become JMP rel32 to the
trampoline, padded with INT3; those instructions are moved into the trampoline's fallback path, which then jumps
back. Refused: a moved instruction that is RIP-relative or a branch, or a jump inside FUNC that lands between the
moved instructions. The trampoline (System V; Q-ARM's X0..X5 are RAX RDI RSI RDX RCX R8, docs/qarm_isa.md 2.1):

    push rdi, rsi, rdx, rcx, r8, r9; sub rsp, 24      the arguments; RSP stays 16-byte aligned for the call
    xor eax, eax; lea rdi, [rip + PRED]; mov esi, BITS
    the packet (JMP +30, "QARM", version 2 and the opcode, zeros)
    cmp rax, 1; jne fallback
    mov [rsp], rdx (and [rsp + 8], rcx for QMIN)      the answer, and its cost
    mov edi, edx; call PRED; cmp eax, 1 (QMIN: cmp eax, [rsp + 8]); jne fallback
    movsxd rax, dword [rsp]; add rsp, 24; pop the six; ret
  fallback:
    add rsp, 24; pop the six; the moved instructions; jmp FUNC + their length

Every branch is direct, so CET's indirect-branch tracking needs no new ENDBR64, and the call and return pairs
keep the shadow stack balanced."""
import struct

QSEARCH, QMIN = 0x11, 0x15
MAGIC = 0x4d524151


class PatchError(Exception):
    pass


def rel32(frm_end, to):
    d = to - frm_end
    if not -(1 << 31) <= d < (1 << 31):
        raise PatchError(f"a jump of {d} bytes: out of range")
    return struct.pack("<i", d)


def trampoline(base, op, pred, bits, moved, resume):
    """Bytes of the trampoline at base. moved: the bytes of the moved instructions; resume: where FUNC goes on."""
    out = bytearray()
    pushes = b"\x57\x56\x52\x51\x41\x50\x41\x51"                 # push rdi rsi rdx rcx r8 r9
    pops = b"\x41\x59\x41\x58\x59\x5a\x5e\x5f"                   # pop r9 r8 rcx rdx rsi rdi
    out += pushes + b"\x48\x83\xec\x18"                          # sub rsp, 24
    out += b"\x31\xc0"                                           # xor eax, eax: "not executed" on a plain CPU
    out += b"\x48\x8d\x3d" + rel32(base + len(out) + 7, pred)    # lea rdi, [rip + PRED]
    out += b"\xbe" + struct.pack("<I", bits)                     # mov esi, BITS
    out += b"\xeb\x1e\x00\x00" + struct.pack("<II", MAGIC, 2 << 24 | op << 16) + bytes(20)
    out += b"\x48\x83\xf8\x01"                                   # cmp rax, 1
    jne1 = len(out)
    out += b"\x0f\x85" + bytes(4)                                # jne fallback (filled below)
    out += b"\x48\x89\x14\x24"                                   # mov [rsp], rdx
    if op == QMIN:
        out += b"\x48\x89\x4c\x24\x08"                           # mov [rsp + 8], rcx
    out += b"\x89\xd7"                                           # mov edi, edx
    out += b"\xe8" + rel32(base + len(out) + 5, pred)            # call PRED: the check
    out += b"\x3b\x44\x24\x08" if op == QMIN else b"\x83\xf8\x01"   # cmp eax, [rsp + 8] | cmp eax, 1
    jne2 = len(out)
    out += b"\x0f\x85" + bytes(4)
    out += b"\x48\x63\x04\x24"                                   # movsxd rax, dword [rsp]
    out += b"\x48\x83\xc4\x18" + pops + b"\xc3"                  # add rsp, 24; pops; ret
    fallback = len(out)
    for j in (jne1, jne2):
        out[j + 2:j + 6] = rel32(base + j + 6, base + fallback)
    out += b"\x48\x83\xc4\x18" + pops
    out += moved
    out += b"\xe9" + rel32(base + len(out) + 5, resume)          # jmp back into FUNC
    return bytes(out)


def _md():
    import capstone
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    md.detail = True
    return md


def patch_point(binary, fa, size=None):
    """(the address to patch, the moved bytes, their text, their length) of FUNC at fa."""
    import capstone
    from capstone import x86 as X
    code = bytes(binary.get_content_from_virtual_address(fa, size or 64))
    insns = list(_md().disasm(code, fa))
    if not insns:
        raise PatchError(f"no instructions at {fa:#x}")
    i = 0
    if insns[0].mnemonic in ("endbr64", "endbr32"):
        i = 1
    at, n, moved, text = insns[i].address, 0, b"", []
    while n < 5:
        if i >= len(insns):
            raise PatchError(f"the function at {fa:#x} is shorter than a jump")
        ins = insns[i]
        m = ins.mnemonic
        riprel = any(o.type == X.X86_OP_MEM and o.mem.base == X.X86_REG_RIP for o in ins.operands)
        if m.startswith("j") or m in ("call", "ret", "loop", "jrcxz") or riprel:
            raise PatchError(f"the instruction at {ins.address:#x} ({m} {ins.op_str}) is a branch or RIP-relative: "
                             "not moved")
        moved += bytes(ins.bytes)
        text.append(f"{m} {ins.op_str}".strip())
        n += ins.size
        i += 1
    for ins in insns:                                     # no jump of the function may land inside the moved bytes
        if ins.mnemonic.startswith("j") or ins.mnemonic == "call":
            for o in ins.operands:
                if o.type == X.X86_OP_IMM and at < o.imm < at + n:
                    raise PatchError(f"a jump at {ins.address:#x} lands inside the moved instructions")
    return at, moved, "; ".join(text), n


def jump_bytes(at, n, to):
    return b"\xe9" + rel32(at + 5, to) + b"\xcc" * (n - 5)        # jmp rel32, padded with int3


def reg(r):
    """The 64-bit register a name belongs to: ebx, bx, bl -> rbx; r12d -> r12."""
    r = r.strip()
    if r[:1] == "r" and r[1:2].isdigit():
        return r.rstrip("dwb")
    base = {"al": "ax", "bl": "bx", "cl": "cx", "dl": "dx", "sil": "si", "dil": "di", "bpl": "bp", "spl": "sp"}
    r = base.get(r, r)
    return "r" + r[-2:] if len(r) >= 2 else r


def scan(binary, funcs):
    """[(op, func, callee, bits, why)]: a counter to 2^BITS passed (mov edi, R) to a leaf function, its result tested
    (test eax, eax; jcc: QSEARCH) or compared with the best so far (cmp eax, R'/cmp R', eax: QMIN)."""
    from capstone import x86 as X
    md = _md()
    insns = {}
    for a, (name, size) in funcs.items():
        try:
            insns[a] = list(md.disasm(bytes(binary.get_content_from_virtual_address(a, size)), a))
        except Exception:
            insns[a] = []
    leaf = {a for a, ins in insns.items() if ins and not any(i.mnemonic in ("call", "syscall") for i in ins)
            and not any(r in i.op_str for i in ins for r in ("xmm", "ymm", "rsp]", "rbp]", "rsp +", "rbp -"))}
    found = []
    for a, ins in insns.items():
        name = funcs[a][0].lstrip("_")
        bounds = []
        for k, i in enumerate(ins[:-1]):
            if i.mnemonic == "cmp" and len(i.operands) == 2 and i.operands[1].type == X.X86_OP_IMM \
                    and i.operands[0].type == X.X86_OP_REG and ins[k + 1].mnemonic.startswith("j"):
                v = i.operands[1].imm
                for bits in range(1, 32):
                    if v in (1 << bits, (1 << bits) - 1):
                        bounds.append((reg(i.reg_name(i.operands[0].reg)), bits))
        for r, bits in bounds:
            hit = None
            for k, i in enumerate(ins[:-2]):
                ops = [t.strip() for t in i.op_str.split(",")]
                if not (i.mnemonic == "mov" and len(ops) == 2 and ops[0] == "edi" and reg(ops[1]) == r
                        and ins[k + 1].mnemonic == "call"):
                    continue
                try:
                    callee = int(ins[k + 1].op_str, 16)
                except ValueError:
                    continue
                if callee not in leaf or callee not in funcs:
                    continue
                after, pname = ins[k + 2], funcs[callee][0].lstrip("_")
                ops2 = [t.strip() for t in after.op_str.split(",")]
                if after.mnemonic == "test" and ops2 == ["eax", "eax"] and ins[k + 3].mnemonic.startswith("j"):
                    hit = (QSEARCH, name, pname, bits, f"a counter to {1 << bits} passed to {pname}, the loop left "
                           f"when it returns nonzero (test eax, eax; {ins[k + 3].mnemonic})")
                elif after.mnemonic == "cmp" and "eax" in ops2 and len(ops2) == 2 and all(t[:1] in "er" for t in ops2):
                    hit = (QMIN, name, pname, bits, f"a counter to {1 << bits} passed to {pname}, its result "
                           "compared with the best so far")
                if hit:
                    break
            if hit:
                found.append(hit)
                break
    return found
