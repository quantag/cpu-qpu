# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Quantag IT Solutions GmbH
"""Put Q-ARM into an existing AArch64 binary (ELF for Linux, Mach-O for macOS), without its source and without qarm.h.

    python3 tools/qarm_patch.py PROGRAM --search FUNC=PRED:BITS [--min FUNC=COST:BITS] ... -o OUT

--search FUNC=PRED:BITS   FUNC is a brute-force search: it returns some x in 0 .. 2^BITS - 1 with PRED(x) = 1 (or
                          gives up). FUNC's entry is redirected to a trampoline in a new segment that emits QSEARCH
                          on PRED's machine code, checks the answer by calling PRED, and returns it. If the packet
                          is not executed (a plain CPU), declined, finds nothing, or the check fails, the trampoline
                          restores the arguments and runs FUNC's own code.
--min FUNC=COST:BITS      FUNC returns an x in 0 .. 2^BITS - 1 with the least COST(x) (signed int): QMIN, checked by
                          calling COST, then x is returned.
--scan                    list the functions that look like such loops (a counter from 0 to 2^BITS passed to a leaf
                          function, its result tested, or compared with the best so far), with the options to patch
                          them. --auto patches every one found (-o needed). The scan is a heuristic over the machine
                          code; what the function promises is still the user's to confirm.

FUNC and PRED are symbol names (the binary must not be stripped of them) or addresses (0x...). FUNC's contract is
the user's statement: the patch is right exactly when FUNC's result may be any such x. PRED and COST must satisfy
docs/qarm_isa.md, section 5.5 (leaf functions, no SIMD; build with -O1). The patched binary runs on every AArch64
CPU, with the original behaviour, and on every Q-ARM runtime (qarm_exec.py, the QEMU plugin) with the quantum one.

The patch: at FUNC (after a BTI or PACIASP landing pad, kept in place) one instruction becomes B to the trampoline;
the instruction it replaces is moved into the trampoline's fallback path, which then branches back to the next one.
A PC-relative first instruction (ADR, ADRP, a branch, a literal load) is refused. Every branch is direct (B, BL),
so BTI-guarded pages need no landing pads, and a signed return address (PACIASP) is authenticated before the
trampoline returns. The new segment must be within 128 MB of the code (it is placed after the program)."""
import argparse, os, struct, sys

QSEARCH, QMIN = 0x11, 0x15
MAGIC, B32 = 0x4d524151, 0x14000008


class PatchError(Exception):
    pass


# ---------------------------------------------------------------- A64 encodings (only what the trampoline needs)
def _imm(v, bits, what):
    if not -(1 << (bits - 1)) <= v < (1 << (bits - 1)):
        raise PatchError(f"{what}: offset {v} out of range")
    return v & ((1 << bits) - 1)


def stp_pre(rt, rt2, imm): return 0xA9800000 | _imm(imm // 8, 7, "stp") << 15 | rt2 << 10 | 31 << 5 | rt
def ldp_post(rt, rt2, imm): return 0xA8C00000 | _imm(imm // 8, 7, "ldp") << 15 | rt2 << 10 | 31 << 5 | rt
def stp(rt, rt2, imm): return 0xA9000000 | (imm // 8) << 15 | rt2 << 10 | 31 << 5 | rt
def ldp(rt, rt2, imm): return 0xA9400000 | (imm // 8) << 15 | rt2 << 10 | 31 << 5 | rt
def str_x(rt, imm): return 0xF9000000 | (imm // 8) << 10 | 31 << 5 | rt
def ldr_x(rt, imm): return 0xF9400000 | (imm // 8) << 10 | 31 << 5 | rt
def mov_fp_sp(): return 0x910003FD                                   # add x29, sp, #0
def movz(rd, imm): return 0xD2800000 | (imm & 0xFFFF) << 5 | rd
def mov_w(rd, rm): return 0x2A0003E0 | rm << 16 | rd                # orr wd, wzr, wm
def sxtw_w(rd, rm): return 0x93407C00 | rm << 5 | rd                # sxtw xd, wm
def add_imm(rd, rn, imm): return 0x91000000 | (imm & 0xFFF) << 10 | rn << 5 | rd
def cmp_x(rn, imm): return 0xF100001F | imm << 10 | rn << 5         # subs xzr, xn, #imm
def cmp_w(rn, imm): return 0x7100001F | imm << 10 | rn << 5
def cmp_wr(rn, rm): return 0x6B00001F | rm << 16 | rn << 5          # subs wzr, wn, wm
def b_cond(pc, to, cond): return 0x54000000 | _imm((to - pc) // 4, 19, "b.cond") << 5 | cond
def b(pc, to): return 0x14000000 | _imm((to - pc) // 4, 26, "b")
def bl(pc, to): return 0x94000000 | _imm((to - pc) // 4, 26, "bl")
def ret(): return 0xD65F03C0
AUT = {"paciasp": 0xD50323BF, "pacibsp": 0xD50323FF}                # autiasp, autibsp
NE = 1


def adrp_add(pc, rd, target):
    """ADRP rd, target; ADD rd, rd, :lo12:target: two words, position independent."""
    pages = (target >> 12) - (pc >> 12)
    v = _imm(pages, 21, "adrp")
    return [0x90000000 | (v & 3) << 29 | (v >> 2) << 5 | rd, add_imm(rd, rd, target & 0xFFF)]


# ---------------------------------------------------------------- the trampoline
def trampoline(base, op, pred, bits, displaced, resume, pac=None):
    """The code at base: the packet, the check, the fallback. Returns a list of words. Branches are direct (B, BL,
    B.NE), so that BTI-guarded pages need no landing pads; pac: the PACIASP or PACIBSP that FUNC ran before the
    patch point, undone (AUTIASP, AUTIBSP) before the trampoline returns for it."""
    w = []
    pc = lambda: base + 4 * len(w)
    frame = 80
    w += [stp_pre(29, 30, -frame), mov_fp_sp(), stp(0, 1, 16), stp(2, 3, 32), stp(4, 5, 48)]
    w.append(movz(0, 0))                                            # W0 = 0: "not executed" on a plain CPU
    w += adrp_add(pc(), 1, pred)                                    # X1 = PRED or COST
    w.append(movz(2, bits))
    w += [B32, MAGIC, 2 << 24 | op << 16, 0, 0, 0, 0, 0]
    w.append(cmp_x(0, 1))
    fallback_branches = [len(w)]
    w.append(0)                                                     # b.ne fallback (patched below)
    w.append(str_x(3, 64))                                          # the answer
    if op == QMIN:
        w.append(str_x(4, 72))                                      # its cost, as the runtime says
    w.append(mov_w(0, 3))                                           # the check: PRED(x) == 1, COST(x) == W4
    w.append(bl(pc(), pred))
    if op == QMIN:
        w.append(ldr_x(1, 72))
        w.append(cmp_wr(0, 1))
    else:
        w.append(cmp_w(0, 1))
    fallback_branches.append(len(w))
    w.append(0)
    w.append(ldr_x(0, 64))
    w.append(sxtw_w(0, 0))                                          # an int result
    w.append(ldp_post(29, 30, frame))
    if pac:
        w.append(AUT[pac])
    w.append(ret())
    fallback = pc()
    for i in fallback_branches:
        w[i] = b_cond(base + 4 * i, fallback, NE)
    w += [ldp(0, 1, 16), ldp(2, 3, 32), ldp(4, 5, 48), ldp_post(29, 30, frame)]
    w.append(displaced)
    w.append(b(pc(), resume))
    return w


PC_RELATIVE = ("adr", "adrp", "b", "bl", "cbz", "cbnz", "tbz", "tbnz", "ldr", "ldrsw", "prfm")


def patch_point(code, addr):
    """(the address to patch, its instruction word): FUNC's first instruction after a landing pad."""
    import capstone
    md = capstone.Cs(capstone.CS_ARCH_ARM64, capstone.CS_MODE_ARM)
    insns = list(md.disasm(code[:16], addr))
    if not insns:
        raise PatchError(f"no instructions at {addr:#x}")
    i, pac = 0, None
    while i < len(insns) - 1 and (insns[i].mnemonic in ("bti", "paciasp", "pacibsp", "hint")):
        if insns[i].mnemonic in AUT:
            pac = insns[i].mnemonic
        elif insns[i].mnemonic == "hint":
            raise PatchError(f"{insns[i].mnemonic} {insns[i].op_str} at {insns[i].address:#x}: an unknown hint")
        i += 1
    ins = insns[i]
    m = ins.mnemonic.split(".")[0]
    literal = m in ("ldr", "ldrsw", "prfm") and "[" not in ins.op_str
    if m in PC_RELATIVE and (m not in ("ldr", "ldrsw", "prfm") or literal):
        raise PatchError(f"the first instruction at {ins.address:#x} is PC-relative ({ins.mnemonic} {ins.op_str}): "
                         "not moved")
    return ins.address, struct.unpack_from("<I", code, 4 * i)[0], f"{ins.mnemonic} {ins.op_str}", pac


def _functions(binary):
    """{address: (name, size)} of the defined functions with sizes (ELF) or the next symbol's address (Mach-O)."""
    out = {}
    for s in binary.symbols:
        if s.value and s.name and getattr(s, "size", 0):
            out[s.value] = (s.name, s.size)
    if not out:                                           # Mach-O: sizes from the next function's start
        starts = sorted({s.value for s in binary.symbols if s.value and s.name.startswith("_")})
        names = {s.value: s.name for s in binary.symbols if s.value and s.name.startswith("_")}
        for a, nxt in zip(starts, starts[1:] + [starts[-1] + 4096] if starts else []):
            out[a] = (names[a], min(nxt - a, 4096))
    return out


def scan(binary):
    """[(op, func, callee, bits, why)]: brute-force loops over a counter that is passed to a leaf function."""
    import capstone
    md = capstone.Cs(capstone.CS_ARCH_ARM64, capstone.CS_MODE_ARM)
    funcs = _functions(binary)
    insns = {}
    for a, (name, size) in funcs.items():
        try:
            insns[a] = list(md.disasm(bytes(binary.get_content_from_virtual_address(a, size)), a))
        except Exception:
            insns[a] = []
    leaf = {a for a, ins in insns.items()
            if ins and not any(i.mnemonic in ("bl", "blr", "svc") for i in ins)
            and not any(r in i.op_str for i in ins for r in (" v", " q", " d0", " s0"))}
    found = []
    reg = lambda r: r.strip().lstrip("wx")                # w19 and x19 are one register
    for a, ins in insns.items():
        name = funcs[a][0].lstrip("_")
        bounds = []
        for k, i in enumerate(ins[:-1]):                # cmp R, #2^k or 2^k - 1 (lsl #12 allowed), a backward b.cond
            if i.mnemonic != "cmp":
                continue
            parts = [p.strip() for p in i.op_str.split(",")]
            if len(parts) < 2 or not parts[1].startswith("#") or parts[0][0] not in "wx":
                continue
            v = int(parts[1][1:], 0) << (12 if len(parts) == 3 and parts[2] == "lsl #12" else 0)
            nxt = ins[k + 1]
            if not (nxt.mnemonic.startswith("b.") and int(nxt.op_str.lstrip("#"), 16) < nxt.address):
                continue
            for bits in range(1, 32):
                if v in ((1 << bits), (1 << bits) - 1):
                    bounds.append((reg(parts[0]), bits))
        for r, bits in bounds:
            hit = None
            for k, i in enumerate(ins[:-2]):            # mov w0, wR (or x0, xR); bl P (a leaf); the test of w0
                if not (i.mnemonic == "mov" and [reg(t) for t in i.op_str.split(",")] == ["0", r]
                        and ins[k + 1].mnemonic == "bl"):
                    continue
                callee = int(ins[k + 1].op_str.lstrip("#"), 16)
                if callee not in leaf or callee not in funcs:
                    continue
                after, pname = ins[k + 2], funcs[callee][0].lstrip("_")
                ops = [t.strip() for t in after.op_str.split(",")]
                if after.mnemonic in ("cbnz", "cbz", "tbnz", "tbz") and reg(ops[0]) == "0":
                    hit = (QSEARCH, name, pname, bits, f"a counter to {1 << bits} passed to {pname}, the loop left "
                           f"when it returns nonzero ({after.mnemonic})")
                elif after.mnemonic == "cmp" and len(ops) == 2 and "0" in (reg(ops[0]), reg(ops[1])) \
                        and ops[1][0] in "wx":
                    hit = (QMIN, name, pname, bits, f"a counter to {1 << bits} passed to {pname}, its result "
                           "compared with the best so far")
                if hit:
                    break
            if hit:
                found.append(hit)
                break
    return found


def resolve(binary, name):
    if name.startswith("0x"):
        return int(name, 16)
    for want in (name, "_" + name):                       # Mach-O: C names begin with _
        for s in binary.symbols:
            if s.name == want and s.value:
                return s.value
    raise PatchError(f"no symbol {name} (a stripped binary? give its address as 0x...)")


def patch_macho(path, out, jobs, note):
    """Mach-O (macOS): a __QARM segment (r-x), then the patches; the signature is removed and, on macOS, the
    output is signed ad hoc (codesign -s -), which arm64 macOS requires to run it."""
    import lief, os, shutil, subprocess
    fat = lief.MachO.parse(path)
    binary = fat.at(0) if fat is not None else None
    if binary is None or binary.header.cpu_type != lief.MachO.Header.CPU_TYPE.ARM64:
        raise PatchError(f"{path}: not an arm64 Mach-O file")
    for op, func, pred, bits in jobs:
        if not 1 <= bits <= 31:
            raise PatchError(f"{bits} bits: 1 to 31")
        fa = resolve(binary, func)
        resolve(binary, pred)
        patch_point(bytes(binary.get_content_from_virtual_address(fa, 16)), fa)
    size = 0x4000                                         # one 16 KB page: macOS arm64's page size
    seg = lief.MachO.SegmentCommand("__QARM", [0] * size)
    seg.init_protection = seg.max_protection = 5          # r-x
    if binary.has_code_signature:
        binary.remove_signature()
    seg = binary.add(seg)
    base = seg.virtual_address
    code = []
    for op, func, pred, bits in jobs:
        fa, pa = resolve(binary, func), resolve(binary, pred)
        at, word, text, pac = patch_point(bytes(binary.get_content_from_virtual_address(fa, 16)), fa)
        t = base + 4 * len(code)
        code += trampoline(t, op, pa, bits, word, at + 4, pac)
        binary.patch_address(at, list(struct.pack("<I", b(at, t))))
        note(f"{func} ({fa:#x}): {'QSEARCH' if op == QSEARCH else 'QMIN'} on {pred} ({pa:#x}), {bits} bits; the "
             f"instruction at {at:#x} ({text}) moved to the trampoline at {t:#x}")
    blob = struct.pack(f"<{len(code)}I", *code)
    binary.patch_address(base, list(blob))
    binary.write(out)
    os.chmod(out, 0o755)
    if sys.platform == "darwin" and shutil.which("codesign"):
        p = subprocess.run(["codesign", "-f", "-s", "-", out], capture_output=True, text=True)
        if p.returncode:
            raise PatchError(f"codesign: {p.stderr.strip()}")
        note("signed ad hoc (codesign -s -)")
    return base, code


def patch(path, out, jobs, note=print):
    import lief
    with open(path, "rb") as f:
        magic = f.read(4)
    if magic in (b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe"):
        return patch_macho(path, out, jobs, note)
    binary = lief.parse(path)
    if binary is None or not isinstance(binary, lief.ELF.Binary):
        raise PatchError(f"{path}: neither ELF nor Mach-O")
    if binary.header.machine_type == lief.ELF.ARCH.X86_64:
        return patch_x86(binary, out, jobs, note)
    if binary.header.machine_type != lief.ELF.ARCH.AARCH64:
        raise PatchError(f"{path}: neither AArch64 nor x86-64")
    for op, func, pred, bits in jobs:                             # refuse before changing anything
        if not 1 <= bits <= 31:
            raise PatchError(f"{bits} bits: 1 to 31")
        fa = resolve(binary, func)
        resolve(binary, pred)
        patch_point(bytes(binary.get_content_from_virtual_address(fa, 16)), fa)
    seg = lief.ELF.Segment()
    seg.type = lief.ELF.Segment.TYPE.LOAD
    seg.flags = lief.ELF.Segment.FLAGS.R | lief.ELF.Segment.FLAGS.X
    seg.alignment = 0x1000
    size = 4 * 64 * len(jobs)
    seg.content = [0] * size
    seg = binary.add(seg)               # LIEF may move the program's sections (room for the program headers)
    base = seg.virtual_address
    plans = []
    for op, func, pred, bits in jobs:                             # so the addresses are taken afterwards
        fa, pa = resolve(binary, func), resolve(binary, pred)
        at, word, text, pac = patch_point(bytes(binary.get_content_from_virtual_address(fa, 16)), fa)
        plans.append((op, func, pred, bits, fa, pa, at, word, text, pac))
    code = []
    for op, func, pred, bits, fa, pa, at, word, text, pac in plans:
        t = base + 4 * len(code)
        code += trampoline(t, op, pa, bits, word, at + 4, pac)
        binary.patch_address(at, list(struct.pack("<I", b(at, t))))
        name = "QSEARCH" if op == QSEARCH else "QMIN"
        note(f"{func} ({fa:#x}): {name} on {pred} ({pa:#x}), {bits} bits; the instruction at {at:#x} ({text}) "
             f"moved to the trampoline at {t:#x}")
    blob = struct.pack(f"<{len(code)}I", *code)
    seg.content = list(blob + bytes(size - len(blob)))
    binary.write(out)
    import os
    os.chmod(out, 0o755)
    return base, code


def patch_x86(binary, out, jobs, note):
    """x86-64 ELF: tools/qarm_patch_x86.py's trampolines in a new r-x segment."""
    import lief, os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import qarm_patch_x86 as X
    sizes = {a: s for a, (_, s) in _functions(binary).items()}
    try:
        for op, func, pred, bits in jobs:
            if not 1 <= bits <= 31:
                raise PatchError(f"{bits} bits: 1 to 31")
            fa = resolve(binary, func)
            resolve(binary, pred)
            X.patch_point(binary, fa, sizes.get(fa))
        seg = lief.ELF.Segment()
        seg.type = lief.ELF.Segment.TYPE.LOAD
        seg.flags = lief.ELF.Segment.FLAGS.R | lief.ELF.Segment.FLAGS.X
        seg.alignment = 0x1000
        size = 256 * len(jobs)
        seg.content = [0] * size
        seg = binary.add(seg)
        base = seg.virtual_address
        sizes = {a: s for a, (_, s) in _functions(binary).items()}      # LIEF may have moved the code
        blob = b""
        for op, func, pred, bits in jobs:
            fa, pa = resolve(binary, func), resolve(binary, pred)
            at, moved, text, n = X.patch_point(binary, fa, sizes.get(fa))
            t = base + len(blob)
            blob += X.trampoline(t, op, pa, bits, moved, at + n)
            blob += b"\xcc" * (-len(blob) % 16)
            binary.patch_address(at, list(X.jump_bytes(at, n, t)))
            note(f"{func} ({fa:#x}): {'QSEARCH' if op == QSEARCH else 'QMIN'} on {pred} ({pa:#x}), {bits} bits; "
                 f"the instructions at {at:#x} ({text}) moved to the trampoline at {t:#x}")
    except X.PatchError as e:
        raise PatchError(str(e))
    seg.content = list(blob + bytes(size - len(blob)))
    binary.write(out)
    os.chmod(out, 0o755)
    return base, blob


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("program")
    ap.add_argument("-o", "--output")
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--auto", action="store_true")
    ap.add_argument("--search", action="append", default=[], metavar="FUNC=PRED:BITS")
    ap.add_argument("--min", action="append", default=[], metavar="FUNC=COST:BITS")
    a = ap.parse_args(argv)
    jobs = []
    for op, specs in ((QSEARCH, a.search), (QMIN, a.min)):
        for spec in specs:
            try:
                func, rest = spec.split("=")
                pred, bits = rest.split(":")
                jobs.append((op, func, pred, int(bits, 0)))
            except ValueError:
                ap.error(f"{spec}: expected FUNC=PRED:BITS")
    if a.scan or a.auto:
        import lief
        b = lief.parse(a.program)
        b = b.at(0) if hasattr(b, "at") else b
        if b is None:
            print(f"qarm_patch: {a.program}: neither ELF nor Mach-O", file=sys.stderr)
            return 2
        if getattr(b, "header", None) is not None and isinstance(b, lief.ELF.Binary) \
                and b.header.machine_type == lief.ELF.ARCH.X86_64:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import qarm_patch_x86
            cands = qarm_patch_x86.scan(b, _functions(b))
        else:
            cands = scan(b)
        for op, func, pred, bits, why in cands:
            flag = "--search" if op == QSEARCH else "--min"
            print(f"{func}: {'QSEARCH' if op == QSEARCH else 'QMIN'} on {pred}, {bits} bits ({why}): "
                  f"{flag} {func}={pred}:{bits}")
        if not cands:
            print("no brute-force loops found")
        if a.auto:
            jobs += [(op, func, pred, bits) for op, func, pred, bits, _ in cands]
        else:
            return 0
    if not jobs:
        ap.error("nothing to patch: give --search, --min, --scan or --auto")
    if not a.output:
        ap.error("-o OUT is needed to patch")
    try:
        patch(a.program, a.output, jobs, note=lambda t: print("qarm_patch: " + t, file=sys.stderr))
    except PatchError as e:
        print(f"qarm_patch: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
