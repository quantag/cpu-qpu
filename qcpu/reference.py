# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Quantag IT Solutions GmbH
"""Conventional reference: execute the same A64 bytes on an AArch64 CPU emulator (Unicorn = QEMU's
ARM CPU core), one instruction at a time, recording the PC trace and checking that every value
fits the W-bit sign-extended model used by the quantum machine.

Memory layout shared by both machines (addresses whose low W bits agree):
  code  at CODE_BASE: the loaded code, padded; the last slot holds HLT. X30 (LR) starts pointing
        at it, so the final RET of the entry function lands on HLT and ends the run.
  data  at DATA_BASE: MEM 4-byte cells; SP starts at the top of data memory.
"""
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UcError, UC_HOOK_MEM_READ, UC_HOOK_MEM_WRITE
from unicorn import arm64_const as A
from .a64 import check, bits, datasize, MEMOPS, is_load

CODE_BASE = 0x400000            # low 22 bits zero
DATA_BASE = 0x10000000          # low 28 bits zero: a pointer's low W bits = offset into data memory
HLT_WORD = 0xD4400000
XREG = [getattr(A, f"UC_ARM64_REG_X{i}") for i in range(29)] + [A.UC_ARM64_REG_X29, A.UC_ARM64_REG_X30]


def _sext(v, n):
    v &= (1 << n) - 1
    return v - (1 << n) if v >> (n - 1) else v


def _decode_bitmask(n, imms, immr, ds):
    v = (n << 6) | (~imms & 0x3F)
    esize = 1 << (v.bit_length() - 1)
    S, R = imms & (esize - 1), immr & (esize - 1)
    welem = (1 << (S + 1)) - 1
    elem = ((welem >> R) | (welem << (esize - R))) & ((1 << esize) - 1)
    out = 0
    for i in range(0, ds, esize):
        out |= elem << i
    return out


def _cond_holds(cond, nzcv):
    n, z, c, v = (nzcv >> 3) & 1, (nzcv >> 2) & 1, (nzcv >> 1) & 1, nzcv & 1
    base = [z, c, n, v, c and not z, n == v, n == v and not z, True][cond >> 1]
    return bool(base) != bool(cond & 1 and cond != 15)


def _flag_result(w, cls, reg, nzcv):
    """Result of a flag-setting instruction (also when it is discarded into XZR); None if none."""
    ds = 64 if bits(w, 31, 1) else 32
    m = (1 << ds) - 1
    sub = bits(w, 30, 1)
    if cls == "addsub_imm" and bits(w, 29, 1):
        b = bits(w, 10, 12) << (12 * bits(w, 22, 1))
    elif cls in ("addsub_reg", "addsub_ext") and bits(w, 29, 1) or cls == "logic_reg" and bits(w, 29, 2) == 3:
        b = reg(bits(w, 16, 5), False) & m
        if cls == "addsub_ext":
            opt = bits(w, 13, 3)
            esz = 8 << (opt & 3)
            if esz < 64:
                b &= (1 << esz) - 1
                if opt & 4 and b >> (esz - 1):
                    b -= 1 << esz
            b = (b << bits(w, 10, 3)) & m
        else:
            k, t = bits(w, 10, 6), bits(w, 22, 2)
            b = (b << k) & m if t == 0 else (b >> k) if t == 1 else (_sext(b, ds) >> k) & m
            if cls == "logic_reg" and bits(w, 21, 1):
                b ^= m
    elif cls == "logic_imm" and bits(w, 29, 2) == 3:
        b = _decode_bitmask(bits(w, 22, 1), bits(w, 10, 6), bits(w, 16, 6), ds)
    elif cls == "ccmp":
        if not _cond_holds(bits(w, 12, 4), nzcv):
            return None, ds, None
        b = bits(w, 16, 5) if bits(w, 11, 1) else reg(bits(w, 16, 5), False) & m
    else:
        return None, ds, None
    rn = reg(bits(w, 5, 5), cls in ("addsub_imm", "addsub_ext")) & m
    if cls.startswith("logic"):
        return rn & b, ds, b
    return ((rn - b) if sub else (rn + b)) & m, ds, b


def _nzcv(w, cls, a, b, ds):
    """NZCV of a flag-setting instruction with ds-bit operands a (Rn) and b (second operand)."""
    m, top = (1 << ds) - 1, 1 << (ds - 1)
    if cls.startswith("logic"):
        r = a & b
        return ((r & top) and 8) | ((r == 0) and 4)
    sub = bits(w, 30, 1)
    bb = (~b & m) if sub else b
    full = a + bb + sub
    r = full & m
    c = full >> ds
    v = ((a ^ r) & (bb ^ r) & top) != 0
    return ((r & top) and 8) | ((r == 0) and 4) | (c << 1) | v


def read_width(w, cls, r, ds):
    """Width at which instruction w reads register r."""
    if cls in MEMOPS:
        if r == bits(w, 5, 5) or r == 31:
            return 64                                   # base address
        if cls == "ldst_reg" and r == bits(w, 16, 5):
            return 32 if bits(w, 13, 1) == 0 else 64    # index: UXTW/SXTW read W, LSL/SXTX read X
        return 64 if (bits(w, 31, 1) if cls in ("ld_pair", "st_pair") else bits(w, 30, 1)) else 32
    if cls == "bfm" and bits(w, 10, 6) < 32:
        return 32                                       # SXTW / UXTW-like: low half only
    if cls == "addsub_ext" and r == bits(w, 16, 5):
        return 64 if bits(w, 13, 2) == 3 else 32        # extended operand: X only for UXTX/SXTX
    if cls == "maddl" and r != bits(w, 10, 5):
        return 32                                       # SMULL/UMULL sources are W registers
    if cls == "bra":
        return 64
    return ds


def layout(code_words, slots):
    """Program memory image: code, zero padding, HLT in the last slot."""
    assert len(code_words) < slots
    return list(code_words) + [0] * (slots - 1 - len(code_words)) + [HLT_WORD]


def run_reference(words, W, entry=0, args=(), mem_words=0, mem_init=(), max_steps=100000, sp_off=None,
                  snapshots=False):
    """words: full program-memory image from layout()."""
    slots = len(words)
    halt_idx = slots - 1
    code = b"".join(w.to_bytes(4, "little") for w in words)
    mu = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    mu.mem_map(CODE_BASE, max(0x1000, (len(code) + 0xFFF) & ~0xFFF))
    mu.mem_write(CODE_BASE, code)
    mu.mem_map(DATA_BASE, 0x10000)
    for i, v in enumerate(mem_init):
        mu.mem_write(DATA_BASE + 4 * i, (v & 0xFFFFFFFF).to_bytes(4, "little"))
    for r in XREG:
        mu.reg_write(r, 0)
    for i, v in enumerate(args):
        mu.reg_write(XREG[i], v & 0xFFFFFFFFFFFFFFFF)
    mu.reg_write(A.UC_ARM64_REG_X30, CODE_BASE + 4 * halt_idx)
    sp0 = DATA_BASE + (4 * mem_words if sp_off is None else sp_off)
    mu.reg_write(A.UC_ARM64_REG_SP, sp0)
    mu.reg_write(A.UC_ARM64_REG_NZCV, 0)
    issues = []
    mask = (1 << W) - 1
    step_ref = [0]

    def on_mem(uc, access, address, size, value, user):
        if address % 4 or size not in (4, 8) or not (DATA_BASE <= address and address + size <= DATA_BASE + 4 * mem_words):
            issues.append(f"step {step_ref[0]}: {size}-byte memory access at 0x{address:x} is outside data "
                          "memory, unaligned, or not a 32/64-bit access")
    mu.hook_add(UC_HOOK_MEM_READ | UC_HOOK_MEM_WRITE, on_mem)

    def plain(v, ds):
        view = v & ((1 << ds) - 1)
        return _sext(view & mask, W) % (1 << ds) == view

    def enc(v):
        """The value the W-bit machine holds for v, as 64-bit: sign extension of its low W bits."""
        return _sext(v & mask, W) % (1 << 64)

    def fits(v, ds):
        view = v & ((1 << ds) - 1)
        if _sext(view & mask, W) % (1 << ds) == view:
            return True
        if -(1 << (W - 1)) <= view - DATA_BASE < (1 << (W - 1)) or 0 <= view - CODE_BASE < (1 << (W - 1)):
            return True                                    # pointer into data memory / code (also its 32-bit view)
        return False

    def reg(r, sp_ctx):
        if r == 31:
            return mu.reg_read(A.UC_ARM64_REG_SP) if sp_ctx else 0
        return mu.reg_read(XREG[r])

    trace, snaps = [], []

    def snap():
        if snapshots:
            nz = mu.reg_read(A.UC_ARM64_REG_NZCV) >> 28
            snaps.append(dict(regs=[mu.reg_read(XREG[k]) for k in range(31)] + [mu.reg_read(A.UC_ARM64_REG_SP)],
                              flags=[(nz >> 3) & 1, (nz >> 2) & 1, (nz >> 1) & 1, nz & 1],
                              mem=bytes(mu.mem_read(DATA_BASE, 4 * mem_words)) if mem_words else b""))
    pc = CODE_BASE + 4 * entry
    halted, error = False, None
    pending_flags = None
    for step in range(max_steps):
        step_ref[0] = step
        idx = (pc - CODE_BASE) // 4
        if not 0 <= idx < slots or pc % 4:
            error = f"PC left program memory (0x{pc:x})"
            break
        trace.append(idx)
        if idx == halt_idx or (0 <= idx < slots and words[idx] == HLT_WORD):   # HLT (also exit())
            halted = True
            snap()
            break
        w = words[idx]
        chk = check(w, idx, slots)
        if not chk.ok:
            error = f"reached instruction {idx} (0x{w:08x}) which is not supported: {chk.reason}"
            break
        ds = datasize(w, chk.cls)
        # every register value the instruction reads must fit the model in the width it is read at
        for r in chk.reads:
            v = reg(r, True)
            rw = read_width(w, chk.cls, r, ds)
            if not fits(v, rw):
                issues.append(f"step {step} (instr {idx}): {rw}-bit read of {'SP' if r == 31 else 'X%d' % r}"
                              f" = 0x{v:x} does not fit the {W}-bit model")
        # pointers are held as their offset; operations that depend on the bits above the encoding
        # are exact only if they give the same outcome on the encoding as on the real pointer
        ptrs = [r for r in chk.reads if not plain(reg(r, True), read_width(w, chk.cls, r, ds))]
        if ptrs:
            highbit = chk.cls in ("div", "maddl", "bfm", "shiftv") or \
                chk.cls in ("addsub_reg", "logic_reg") and bits(w, 16, 5) in ptrs and bits(w, 22, 2) and bits(w, 10, 6)
            if highbit:
                issues.append(f"step {step} (instr {idx}): pointer X{ptrs[0]} is used by an operation that depends "
                              "on bits above its W-bit encoding")
        # Semantic check of the operations whose outcome depends on bits above the W-bit encoding.
        # The machine holds every value v -- integer, address or immediate -- as enc(v), the sign
        # extension of its low W bits. The outcome on the encodings must equal the outcome on the CPU.
        if chk.cls in ("cbz", "tbz"):
            v = reg(bits(w, 0, 5), False)
            if chk.cls == "cbz":
                m_ = (1 << ds) - 1
                same = ((v & m_) == 0) == ((enc(v) & m_) == 0)
            else:
                b_ = bits(w, 31, 1) << 5 | bits(w, 19, 5)
                same = (v >> b_ & 1) == (enc(v) >> b_ & 1)
            if not same:
                issues.append(f"step {step} (instr {idx}): {chk.cls.upper()} on X{bits(w, 0, 5)} = 0x{v:x} "
                              "takes a different branch on its W-bit encoding (e.g. a pointer encoded as 0)")
        nz0 = mu.reg_read(A.UC_ARM64_REG_NZCV) >> 28
        _, fds, fb = _flag_result(w, chk.cls, reg, nz0)
        if fb is not None:
            # second operand as the machine has it: computed from encoded registers, then encoded
            # itself (an immediate such as a bit mask is held by its low W bits, like any value)
            fm = (1 << fds) - 1
            _, _, eb = _flag_result(w, chk.cls, lambda r, sp: enc(reg(r, sp)), nz0)
            ea = enc(reg(bits(w, 5, 5), chk.cls in ("addsub_imm", "addsub_ext"))) & fm
            eb = enc(eb) & fm
            if chk.cls.startswith("logic"):
                er = ea & eb
            else:
                er = ((ea - eb) if bits(w, 30, 1) else (ea + eb)) & fm
            if not plain(er, fds):
                issues.append(f"step {step} (instr {idx}): the flag-setting result on the W-bit encodings "
                              "does not fit the model")
            else:
                pending_flags = (_nzcv(w, chk.cls, ea, eb, fds), step, idx)
        res, rds, op2 = _flag_result(w, chk.cls, reg, mu.reg_read(A.UC_ARM64_REG_NZCV) >> 28)
        if res is not None and not fits(res, rds):
            issues.append(f"step {step} (instr {idx}): flag-setting result 0x{res:x} does not fit the {W}-bit model")
        if op2 is not None and not fits(op2, rds):
            issues.append(f"step {step} (instr {idx}): flag-setting operand 0x{op2:x} does not fit the {W}-bit model")
        try:
            mu.emu_start(pc, CODE_BASE + 4 * slots + 0x1000, count=1)
        except UcError as e:
            error = f"CPU exception at instruction {idx}: {e}"
            break
        pc = mu.reg_read(A.UC_ARM64_REG_PC)
        if pending_flags is not None:
            if pending_flags[0] != mu.reg_read(A.UC_ARM64_REG_NZCV) >> 28:
                issues.append(f"step {step} (instr {idx}): NZCV computed on the W-bit encodings differs from the CPU")
            pending_flags = None
        snap()
        for r in chk.writes:
            v = reg(r, True)
            base_wb = chk.cls in MEMOPS and r == bits(w, 5, 5) and not (is_load(w, chk.cls) and r == bits(w, 0, 5))
            wds = 64 if r == 31 or base_wb or (r == 30 and chk.cls in ("bl", "bra")) else ds
            if not fits(v, wds):
                issues.append(f"step {step} (instr {idx}): {'SP' if r == 31 else 'X%d' % r} = 0x{v:x} "
                              f"does not fit the {W}-bit model")
    else:
        error = f"did not finish within {max_steps} steps"
    regs = {k: mu.reg_read(XREG[k]) for k in range(31)}
    regs[31] = mu.reg_read(A.UC_ARM64_REG_SP)
    nzcv = mu.reg_read(A.UC_ARM64_REG_NZCV) >> 28
    flags = [(nzcv >> 3) & 1, (nzcv >> 2) & 1, (nzcv >> 1) & 1, nzcv & 1]
    mem = [int.from_bytes(mu.mem_read(DATA_BASE + 4 * i, 4), "little") for i in range(mem_words)]
    return dict(regs=regs, flags=flags, mem=mem, trace=trace, halted=halted, error=error,
                issues=issues, steps=len(trace), sp0=sp0, snapshots=snaps)
