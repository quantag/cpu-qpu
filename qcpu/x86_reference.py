# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Quantag IT Solutions GmbH
"""Conventional reference for x86-64: execute the same bytes on an x86-64 CPU emulator (Unicorn = QEMU's
x86 core), one instruction at a time, and check that the run stays inside the W-bit model.

Model check, as for RISC-V: before each instruction the reference evaluates x86.model() on the W-bit
encodings of the CPU's registers, flags and memory. After the CPU has executed the instruction it
reports an issue if a register, a flag, a memory cell or the next PC differs (in its low W bits) from
the model's result, if a value written does not fit the model at the width of the operation, or if a
memory access is outside data memory, unaligned or not 4 or 8 bytes wide.

Flags the machine does not model (see x86.py) are tracked: after such an instruction the flag is
'unknown', it is not compared, and an instruction that reads it is reported.

Layout: code bytes at CODE_BASE, the last byte of program memory is HLT. The run starts like a call:
RSP points at a return address that leads to that HLT, so the entry function's final RET ends the run.
"""
from unicorn import Uc, UC_ARCH_X86, UC_MODE_64, UcError, UC_HOOK_MEM_READ, UC_HOOK_MEM_WRITE
from unicorn import x86_const as X
from .reference import CODE_BASE, DATA_BASE, _sext
from . import x86

UREG = [X.UC_X86_REG_RAX, X.UC_X86_REG_RCX, X.UC_X86_REG_RDX, X.UC_X86_REG_RBX, X.UC_X86_REG_RSP, X.UC_X86_REG_RBP,
        X.UC_X86_REG_RSI, X.UC_X86_REG_RDI] + [getattr(X, f"UC_X86_REG_R{i}") for i in range(8, 16)]
ARGS = (x86.RDI, x86.RSI, x86.RDX, x86.RCX, 8, 9)
FLAG_BIT = (6, 7, 0, 11)                # ZF, SF, CF, OF in EFLAGS
FLAG_NAME = ("ZF", "SF", "CF", "OF")
STACK_KINDS = ("push", "pop", "pushrm", "pushi", "call", "callr", "ret", "leave")


def layout(code_bytes, slots):
    """Program memory image: code bytes, zero padding, HLT in the last byte."""
    assert len(code_bytes) < slots
    return list(code_bytes) + [0] * (slots - 1 - len(code_bytes)) + [x86.HLT]


def entry_cells(cells, sp_off, slots):
    """Put the return address (the HLT slot) on the stack, as the caller of the entry function would.
    cells: 32-bit data cells. Returns (cells, initial RSP offset)."""
    cells = list(cells)
    rsp = sp_off - 8
    cells[rsp // 4], cells[rsp // 4 + 1] = slots - 1, 0
    return cells, rsp


def run_reference(words, W, entry=0, args=(), mem_words=0, mem_init=(), max_steps=100000, sp_off=None, snapshots=False):
    """words: program-memory bytes from layout(); mem_init: cells from entry_cells()."""
    slots = len(words)
    halt = slots - 1
    code = bytes(words)
    mu = Uc(UC_ARCH_X86, UC_MODE_64)
    mu.mem_map(CODE_BASE, max(0x1000, (len(code) + 0xFFF) & ~0xFFF))
    mu.mem_write(CODE_BASE, code)
    mu.mem_map(DATA_BASE, 0x10000)
    for i, v in enumerate(mem_init):
        mu.mem_write(DATA_BASE + 4 * i, (v & 0xFFFFFFFF).to_bytes(4, "little"))
    for r in UREG:
        mu.reg_write(r, 0)
    for i, v in enumerate(args):
        mu.reg_write(UREG[ARGS[i]], v & (1 << 64) - 1)
    sp_off = 4 * mem_words if sp_off is None else sp_off
    sp0 = DATA_BASE + sp_off - 8
    mu.reg_write(X.UC_X86_REG_RSP, sp0)
    mu.mem_write(sp0, (CODE_BASE + halt).to_bytes(8, "little"))
    mu.reg_write(X.UC_X86_REG_EFLAGS, 2)
    issues, mask, step_ref, accesses = [], (1 << W) - 1, [0], []

    def on_mem(uc, access, address, size, value, user):
        accesses.append((address, size))
        if address % 4 or size not in (4, 8) or not (DATA_BASE <= address and address + size <= DATA_BASE + 4 * mem_words):
            issues.append(f"step {step_ref[0]}: {size}-byte memory access at 0x{address:x} is outside data "
                          "memory, unaligned, or not a 32/64-bit access")
    mu.hook_add(UC_HOOK_MEM_READ | UC_HOOK_MEM_WRITE, on_mem)

    def fits(v, ds):
        v &= (1 << ds) - 1
        if _sext(v & mask, W) & ((1 << ds) - 1) == v:
            return True
        return -(1 << (W - 1)) <= v - DATA_BASE < (1 << (W - 1)) or 0 <= v - CODE_BASE < (1 << (W - 1))

    reg = lambda k: mu.reg_read(UREG[k])
    cpu_flags = lambda: [mu.reg_read(X.UC_X86_REG_EFLAGS) >> b & 1 for b in FLAG_BIT]
    cell = lambda i: int.from_bytes(mu.mem_read(DATA_BASE + 4 * (i % mem_words), 4), "little") if mem_words else 0
    for i, v in enumerate(args):
        if not fits(v, 64):
            issues.append(f"argument {i} = 0x{v & (1 << 64) - 1:x} does not fit the {W}-bit model")
    mflags, known = [0, 0, 0, 0], [True] * 4          # the machine's flags; which of them equal the CPU's
    trace, snaps = [], []

    def flags_now():
        f = cpu_flags()
        return [f[i] if known[i] else mflags[i] for i in range(4)]

    def snap():
        if snapshots:
            snaps.append(dict(regs=[reg(k) for k in range(16)], flags=flags_now(),
                              mem=bytes(mu.mem_read(DATA_BASE, 4 * mem_words)) if mem_words else b""))
    pc = CODE_BASE + entry
    halted, error = False, None
    for step in range(max_steps):
        step_ref[0] = step
        off = pc - CODE_BASE
        if not 0 <= off < slots:
            error = f"PC left program memory (0x{pc:x})"
            break
        trace.append(off)
        if off == halt:
            halted = True
            snap()
            break
        d = x86.decode(code[off:off + 15])
        if not d["ok"]:
            error = f"reached the instruction at offset {off} ({code[off:off + d['length']].hex()}) which is not supported: {d['why']}"
            break
        before = [reg(k) for k in range(16)]
        mdl = x86.model(d, W, lambda k: before[k] & mask, mflags, lambda i: cell(i) & mask, off, slots)
        where = f"step {step} (offset {off})"
        for i in sorted(mdl["uses"]):
            if not known[i]:
                issues.append(f"{where}: the condition uses {FLAG_NAME[i]}, which the machine does not model after the "
                              "previous shift, multiply or divide")
        del accesses[:]
        try:
            mu.emu_start(pc, CODE_BASE + slots + 0x1000, count=1)
        except UcError as e:
            error = f"CPU exception at offset {off}: {e}"
            break
        pc = mu.reg_read(X.UC_X86_REG_RIP)
        # registers: every register must end with the low W bits the model predicts (written or not)
        stack_op = d["kind"] in STACK_KINDS
        for k in range(16):
            v, want = reg(k), mdl["regs"].get(k, before[k] & mask)
            if v & mask != want:
                issues.append(f"{where}: {x86.REG[k]} = 0x{v:x} on the CPU, but the {W}-bit machine computes 0x{want:x} "
                              "(the operation depends on bits above the encoding of an operand)")
            elif k in mdl["regs"] and not (d["s8"] or d["kind"] == "setcc"):
                ds = 64 if d["w"] or stack_op or d["kind"] in ("movsxd", "cdqe") else 32
                if not fits(v, ds):
                    issues.append(f"{where}: {x86.REG[k]} = 0x{v:x} does not fit the {W}-bit model")
        f = cpu_flags()
        for i, bit in mdl["flags"].items():
            known[i], mflags[i] = True, bit
            if f[i] != bit:
                issues.append(f"{where}: {FLAG_NAME[i]} = {f[i]} on the CPU, the {W}-bit machine computes {bit}")
        for i in mdl["unknown"]:
            known[i] = False
        if pc - CODE_BASE != mdl["npc"]:
            issues.append(f"{where}: the CPU continues at 0x{pc:x}, the {W}-bit machine at offset {mdl['npc']} "
                          "(a branch decided differently on the encodings, or a jump outside the code)")
        if mem_words:
            cells = {c % mem_words for c, _ in mdl["stores"]} | {(c + j) % mem_words for c in mdl["loads"] for j in (0, 1)}
            for address, size in accesses:
                for j in range(size // 4):
                    if ((address - DATA_BASE) // 4 + j) % mem_words not in cells:
                        issues.append(f"{where}: memory access at 0x{address:x}, which the {W}-bit machine does not touch")
            for c, val in mdl["stores"]:
                if cell(c) & mask != val:
                    issues.append(f"{where}: memory cell {c % mem_words} holds 0x{cell(c):x} on the CPU, "
                                  f"the {W}-bit machine stores 0x{val:x}")
            for address, size in accesses:
                v = int.from_bytes(mu.mem_read(address, size), "little") if DATA_BASE <= address < DATA_BASE + 4 * mem_words - 7 else 0
                if mdl["stores"] and not fits(v, 8 * size):
                    issues.append(f"{where}: the value 0x{v:x} in memory does not fit the {W}-bit model")
        snap()
    else:
        error = f"did not finish within {max_steps} steps"
    return dict(regs={k: reg(k) for k in range(16)}, flags=flags_now(), mem=[cell(i) for i in range(mem_words)],
                trace=trace, halted=halted, error=error, issues=issues, steps=len(trace), sp0=sp0, snapshots=snaps)
