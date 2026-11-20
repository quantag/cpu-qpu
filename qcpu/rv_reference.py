"""Conventional reference for RISC-V: execute the same instruction words on a RISC-V CPU emulator
(Unicorn = QEMU's RISC-V core), one instruction at a time, recording the PC trace, and check that the
run stays inside the W-bit model of the quantum machine.

Model check. Before each instruction the reference evaluates rv.model(): what the W-bit machine
computes from the W-bit encodings of the CPU's register and memory values. After the CPU has
executed the instruction it reports an issue if
  * a register value the CPU wrote is neither a representable integer (equal to the sign extension
    of its low W bits) nor an encodable address (inside the code or data window), or
  * the low W bits of that value differ from the model's result, or
  * the CPU's next PC differs from the model's (a branch decided differently), or
  * a memory access is outside data memory, unaligned or not 32/64 bits wide, or goes to another
    cell than the model's, or stores a value whose low W bits differ.
A run with no issue is exact: by induction over the cycles the machine state is the encoding of the
CPU state. The tests compare the circuit with the CPU after every cycle, so a circuit that does not
implement rv.model() shows up as a mismatch that no issue explains.

Memory layout: the same as for AArch64 (reference.py). The last program slot holds EBREAK and the
return address register ra starts pointing at it.
"""
from unicorn import Uc, UC_ARCH_RISCV, UC_MODE_RISCV32, UC_MODE_RISCV64, UcError, UC_HOOK_MEM_READ, UC_HOOK_MEM_WRITE
from unicorn import riscv_const as R
from .reference import CODE_BASE, DATA_BASE, _sext
from .a64 import bits
from . import rv

XREG = [getattr(R, f"UC_RISCV_REG_X{i}") for i in range(32)]
RA, SP, A0 = 1, 2, 10


def layout(code_words, slots):
    """Program memory image: code, zero padding (an illegal instruction), EBREAK in the last slot."""
    assert len(code_words) < slots
    return list(code_words) + [0] * (slots - 1 - len(code_words)) + [rv.EBREAK]


def run_reference(words, W, entry=0, args=(), mem_words=0, mem_init=(), max_steps=100000, sp_off=None,
                  snapshots=False, xlen=64):
    """words: full program-memory image from layout()."""
    slots = len(words)
    halt_idx = slots - 1
    xmask = (1 << xlen) - 1
    code = b"".join(w.to_bytes(4, "little") for w in words)
    mu = Uc(UC_ARCH_RISCV, UC_MODE_RISCV64 if xlen == 64 else UC_MODE_RISCV32)
    mu.mem_map(CODE_BASE, max(0x1000, (len(code) + 0xFFF) & ~0xFFF))
    mu.mem_write(CODE_BASE, code)
    mu.mem_map(DATA_BASE, 0x10000)
    for i, v in enumerate(mem_init):
        mu.mem_write(DATA_BASE + 4 * i, (v & 0xFFFFFFFF).to_bytes(4, "little"))
    for r in XREG[1:]:
        mu.reg_write(r, 0)
    for i, v in enumerate(args):
        mu.reg_write(XREG[A0 + i], v & xmask)
    mu.reg_write(XREG[RA], CODE_BASE + 4 * halt_idx)
    sp0 = DATA_BASE + (4 * mem_words if sp_off is None else sp_off)
    mu.reg_write(XREG[SP], sp0)
    issues = []
    mask = (1 << W) - 1
    step_ref = [0]
    accesses = []

    def on_mem(uc, access, address, size, value, user):
        accesses.append(address)
        if address % 4 or size not in (4, 8) or not (DATA_BASE <= address and address + size <= DATA_BASE + 4 * mem_words):
            issues.append(f"step {step_ref[0]}: {size}-byte memory access at 0x{address:x} is outside data "
                          "memory, unaligned, or not a 32/64-bit access")
    mu.hook_add(UC_HOOK_MEM_READ | UC_HOOK_MEM_WRITE, on_mem)

    def fits(v):
        v &= xmask
        if _sext(v & mask, W) & xmask == v:
            return True                                   # representable integer
        return -(1 << (W - 1)) <= v - DATA_BASE < (1 << (W - 1)) or 0 <= v - CODE_BASE < (1 << (W - 1))   # encodable address

    reg = lambda k: mu.reg_read(XREG[k]) & xmask if k else 0
    cell = lambda i: int.from_bytes(mu.mem_read(DATA_BASE + 4 * (i % mem_words), 4), "little") if mem_words else 0
    name = lambda k: "x%d (%s)" % (k, rv.ABI[k])
    for i, v in enumerate(args):
        if not fits(v):
            issues.append(f"argument {i} = 0x{v & xmask:x} does not fit the {W}-bit model")
    trace, snaps = [], []

    def snap():
        if snapshots:
            snaps.append(dict(regs=[reg(k) for k in range(32)], flags=[],
                              mem=bytes(mu.mem_read(DATA_BASE, 4 * mem_words)) if mem_words else b""))
    pc = CODE_BASE + 4 * entry
    halted, error = False, None
    for step in range(max_steps):
        step_ref[0] = step
        idx = (pc - CODE_BASE) // 4
        if not 0 <= idx < slots or pc % 4:
            error = f"PC left program memory (0x{pc:x})"
            break
        trace.append(idx)
        if idx == halt_idx:
            halted = True
            snap()
            break
        w = words[idx]
        chk = rv.check(w, idx, slots, xlen)
        if not chk.ok:
            error = f"reached instruction {idx} (0x{w:08x}) which is not supported: {chk.reason}"
            break
        mdl = rv.model(w, chk.cls, xlen, W, lambda k: reg(k) & mask, idx, lambda i: cell(i) & mask, slots)
        del accesses[:]
        try:
            mu.emu_start(pc, CODE_BASE + 4 * slots + 0x1000, count=1)
        except UcError as e:
            error = f"CPU exception at instruction {idx}: {e}"
            break
        pc = mu.reg_read(R.UC_RISCV_REG_PC)
        snap()
        where = f"step {step} (instr {idx})"
        rd = bits(w, 7, 5)
        if mdl["rd"] is not None and rd:
            v = reg(rd)
            if not fits(v):
                issues.append(f"{where}: {name(rd)} = 0x{v:x} does not fit the {W}-bit model")
            elif v & mask != mdl["rd"]:
                issues.append(f"{where}: {name(rd)} = 0x{v:x} on the CPU, but the {W}-bit machine computes 0x{mdl['rd']:x} "
                              "(the operation depends on bits above the encoding of an operand)")
        if (pc - CODE_BASE) // 4 != mdl["npc"] or pc % 4:
            issues.append(f"{where}: the CPU continues at 0x{pc:x}, the {W}-bit machine at instruction {mdl['npc']} "
                          "(a branch decided differently on the encodings, or a jump outside the code)")
        if mdl["addr"] is not None and mem_words:
            want = (mdl["addr"] >> 2) % mem_words
            if accesses and (accesses[0] - DATA_BASE) // 4 != want:
                issues.append(f"{where}: memory access at 0x{accesses[0]:x}, the {W}-bit machine uses cell {want}")
            for k, val in mdl["stores"]:
                if cell(want + k) & mask != val:
                    issues.append(f"{where}: memory cell {(want + k) % mem_words} holds 0x{cell(want + k):x} on the CPU, "
                                  f"the {W}-bit machine stores 0x{val:x}")
    else:
        error = f"did not finish within {max_steps} steps"
    regs = {k: reg(k) for k in range(32)}
    mem = [cell(i) for i in range(mem_words)]
    return dict(regs=regs, flags=[], mem=mem, trace=trace, halted=halted, error=error,
                issues=issues, steps=len(trace), sp0=sp0, snapshots=snaps)
