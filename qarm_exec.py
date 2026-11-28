#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Quantag IT Solutions GmbH
"""qarm_exec: run an arm64 program with a quantum coprocessor (Q-ARM v2, docs/qarm_v2.md).

    python3 qarm_exec.py PROGRAM [ARG ...]

PROGRAM is an ordinary executable for arm64: a macOS one (Mach-O, as built on an Apple Silicon Mac) or a
Linux one (ELF, dynamically linked). Its ordinary instructions run at full 64-bit width in an ARM
emulator (Unicorn); calls into the C library are served by this runtime (printf, puts, putchar, exit,
strlen, memset, memcpy). Q-ARM packets (include/qarm.h), which a plain CPU jumps over, run on the quantum
coprocessor here:

    QAVAIL               W0 = 1
    QSEARCH              X1 = address of f, W2 = bits: Grover's search for an x with f(x) = 1. The oracle is
                         built by qoracle from f's machine code in this program; every measured answer is
                         checked by running f itself. W0 = 1 and W3 = x, or W0 = 2 if there is none.
    QVLOAD QEXPECT       VQE (qarm_vqe.py): a Hamiltonian and an ansatz from two tables in the program; the
    QEXPECTN QSESSION    energy <psi(theta)|H|psi(theta)> in D0 (QEXPECT) or for k parameter vectors (QEXPECTN)
    QVFREE

The coprocessor is the exact simulator of this runtime (synchronous: QSEARCH answers at once). With --qpu
it is the backend of config.json (qcpu_run.py: simulator, aer, preview = a device's noise model, ibm = a
real IBM quantum computer), --backend over its "backend". A QPU is asynchronous: jobs wait in a queue, so
QSEARCH sends one job holding Grover's circuit for every iteration count 0..floor(pi/4 sqrt(2^bits)), waits
for it (the job id is reported at once) and checks the measured answers by running f, most frequent first.
Noise costs answers that fail the check, never a wrong W3. A report of each quantum instruction goes to
stderr (-q: none)."""
import argparse
import json
import math
import os
import random
import struct
import sys

STACK_TOP, STACK_SIZE = 0x7ff000000, 0x100000
HEAP, HEAP_SIZE = 0x6f0000000, 0x4000000
STUBS = 0x6e0000000             # one 4-byte slot per imported function, handled by this runtime
HALT = STUBS - 0x1000           # main returns here
PACKET_BRANCH, PACKET_MAGIC = 0x14000008, 0x4D524151
OP_QAVAIL, OP_QSEARCH, OP_QMIN, OP_QCOUNT = 0x10, 0x11, 0x15, 0x16
OP_QPERIOD, OP_QSIMON, OP_QBV, OP_QAMPEST = 0x18, 0x19, 0x1A, 0x17
OP_QRAND, OP_QCIRC, OP_QCAPS, OP_QCOLLISION = 0x1C, 0x30, 0x31, 0x1B
GATES = {1: "x", 2: "y", 3: "z", 4: "h", 5: "s", 6: "sdg", 7: "t", 8: "tdg", 9: "sx", 10: "rx", 11: "ry", 12: "rz",
         13: "cx", 14: "cz", 15: "swap", 16: "ccx"}
OP_QVLOAD, OP_QEXPECT, OP_QEXPECTN, OP_QGRAD, OP_QSESSION, OP_QVFREE = 0x20, 0x21, 0x22, 0x23, 0x24, 0x26
OP_QVSAMPLE = 0x25
OP_QSUBMIT, OP_QPOLL, OP_QWAIT = 0x12, 0x13, 0x14
FEATURES = 16383          # QAVAIL's W1: QSEARCH VQE QMIN QCOUNT QPERIOD QSIMON QBV QAMPEST QCIRC QRAND QCAPS QVSAMPLE ASYNC
                          #              QCOLLISION
OPS = (OP_QAVAIL, OP_QSEARCH, OP_QCOLLISION, OP_QMIN, OP_QCOUNT, OP_QAMPEST, OP_QPERIOD, OP_QSIMON, OP_QBV, OP_QRAND, OP_QCIRC,
       OP_QCAPS, OP_QVLOAD, OP_QEXPECT, OP_QEXPECTN, OP_QGRAD, OP_QVSAMPLE, OP_QSESSION, OP_QVFREE, OP_QSUBMIT, OP_QPOLL,
       OP_QWAIT)


class QarmError(Exception):
    pass


# ------------------------------------------------------------------ the program
def _c_name(name):
    """A Mach-O symbol name is the C name with one underscore in front: _printf, ___stack_chk_guard."""
    return name[1:] if name.startswith("_") else name


class Program:
    """The executable's segments at their own addresses, its imports, its functions and their names."""

    def __init__(self, path, base=None):
        import lief
        self.want_base = base                                # ELF PIE: where it is loaded (default 0x10000000)
        b = lief.parse(path)
        if b is None:
            raise QarmError(f"{path}: not an executable lief can read")
        self.path, self.segments, self.imports, self.rebases, self.code_ranges = path, [], [], [], []
        self.functions = {}                                  # address -> name
        if isinstance(b, lief.MachO.Binary):
            self._macho(b, lief)
        elif isinstance(b, lief.ELF.Binary):
            self._elf(b, lief)
        else:
            raise QarmError(f"{path}: neither Mach-O nor ELF")

    def _macho(self, b, lief):
        if b.header.cpu_type != lief.MachO.Header.CPU_TYPE.ARM64:
            raise QarmError(f"{self.path}: not an arm64 program")
        self.kind, self.base = "macos", 0
        for s in b.segments:
            if s.name == "__PAGEZERO" or not s.virtual_size:
                continue
            self.segments.append((s.virtual_address, s.virtual_size, bytes(s.content), s.name))
            if s.name != "__TEXT" and s.init_protection & 4:     # an executable segment added by a tool
                self.code_ranges.append((s.virtual_address, len(s.content)))
        text = b.get_segment("__TEXT")
        self.main = text.virtual_address + b.main_command.entrypoint
        for bnd in b.bindings:                               # imported functions and data (GOT entries)
            if bnd.has_symbol:
                self.imports.append((bnd.address, _c_name(bnd.symbol.name)))
        if b.has_dyld_chained_fixups:
            for r in b.relocations:
                if isinstance(r, lief.MachO.RelocationFixup):
                    self.rebases.append((r.address, r.target))
        for s in b.symbols:
            if s.value and s.name and s.type != lief.MachO.Symbol.TYPE.UNDEFINED and text.virtual_address <= s.value < text.virtual_address + text.virtual_size:
                self.functions.setdefault(s.value, _c_name(s.name))
        starts = set(self.functions)
        if b.has_function_starts:                            # offsets from the start of __TEXT
            starts |= {f + text.virtual_address if f < text.virtual_address else f for f in b.function_starts.functions}
        self.starts = sorted(starts)
        code = b.get_section("__text")
        self.text = (code.virtual_address, code.size)                  # the code: function ends, packets
        self.code_ranges.append(self.text)
        self.code_segment = (text.virtual_address, text.virtual_size)  # __TEXT: also constant data

    def _elf(self, b, lief):
        if b.header.machine_type not in (lief.ELF.ARCH.AARCH64, lief.ELF.ARCH.X86_64):
            raise QarmError(f"{self.path}: neither an AArch64 nor an x86-64 program")
        self.arch = "x86_64" if b.header.machine_type == lief.ELF.ARCH.X86_64 else "aarch64"
        self.kind = "linux"
        pie = b.header.file_type == lief.ELF.Header.FILE_TYPE.DYN
        self.base = (0x10000000 if self.want_base is None else self.want_base) if pie else 0
        for s in b.segments:
            if s.type == lief.ELF.Segment.TYPE.LOAD:
                self.segments.append((self.base + s.virtual_address, s.virtual_size, bytes(s.content), "LOAD"))
                if s.has(lief.ELF.Segment.FLAGS.X):                # packets may be in any executable segment
                    self.code_ranges.append((self.base + s.virtual_address, s.physical_size))
        mains = [s for s in b.symbols if s.name == "main" and s.value]
        if not mains:
            raise QarmError(f"{self.path}: no symbol main (do not strip the program)")
        self.main = self.base + mains[0].value
        R = lief.ELF.Relocation.TYPE
        for r in b.relocations:
            addr = self.base + r.address
            if r.type in (R.AARCH64_RELATIVE, R.X86_64_RELATIVE):
                self.rebases.append((addr, self.base + r.addend))
            elif r.type in (R.AARCH64_JUMP_SLOT, R.AARCH64_GLOB_DAT, R.AARCH64_ABS64, R.X86_64_JUMP_SLOT,
                            R.X86_64_GLOB_DAT, R.X86_64_64) and r.has_symbol:
                if r.symbol.value:                           # defined in the program itself
                    self.rebases.append((addr, self.base + r.symbol.value + r.addend))
                else:
                    self.imports.append((addr, r.symbol.name))
        sizes = {}
        for s in b.symbols:
            if s.type == lief.ELF.Symbol.TYPE.FUNC and s.value:
                self.functions.setdefault(self.base + s.value, s.name)
                sizes[self.base + s.value] = s.size
        self.sizes = sizes
        self.starts = sorted(self.functions)
        text = next(s for s in b.segments if s.type == lief.ELF.Segment.TYPE.LOAD and s.has(lief.ELF.Segment.FLAGS.X))
        self.text = (self.base + text.virtual_address, text.virtual_size)
        self.code_segment = self.text

    def function_bytes(self, addr, mem):
        """The machine code of the function at addr: up to its size (ELF), or to the next function start."""
        size = getattr(self, "sizes", {}).get(addr)
        if not size:
            text_end = self.text[0] + self.text[1]
            later = [a for a in self.starts if a > addr]
            end = min(later[0], text_end) if later else text_end     # never past the code: __cstring follows
            size = end - addr
        return bytes(mem.mem_read(addr, size))


# ------------------------------------------------------------------ the C library, served by the runtime
def c_format(fmt, args, read_str):
    """printf: %d %i %u %x %X %c %s %p %f %e %g %% with flags, width, precision, l/ll/z/h.
    args(floating) returns the next variadic argument's 64 bits."""
    import re
    out, pos = [], 0
    for m in re.finditer(r"%([-+ #0]*)(\d+|\*)?(?:\.(\d+|\*))?(hh|h|ll|l|L|z|j|t)?([diouxXcspfFeEgGaA%])", fmt):
        out.append(fmt[pos:m.start()])
        pos = m.end()
        flags, width, prec, length, conv = m.groups()
        if conv == "%":
            out.append("%")
            continue

        def nxt(floating=False):
            return args(floating)
        if width == "*":
            width = str(nxt())
        if prec == "*":
            prec = str(nxt())
        v = nxt(conv in "fFeEgGaA")
        if conv in "fFeEgGaA":
            v = struct.unpack("<d", struct.pack("<Q", v & 0xFFFFFFFFFFFFFFFF))[0]
            conv = {"a": "e", "A": "E"}.get(conv, conv)
        elif conv in "di":
            v = v & 0xFFFFFFFFFFFFFFFF if length in ("l", "ll", "z", "j", "t") else v & 0xFFFFFFFF
            bits = 64 if length in ("l", "ll", "z", "j", "t") else 32
            v = v - (1 << bits) if v >> (bits - 1) else v
            conv = "d"
        elif conv in "ouxX":
            v = v & (0xFFFFFFFFFFFFFFFF if length in ("l", "ll", "z", "j", "t") else 0xFFFFFFFF)
        elif conv == "c":
            v = chr(v & 0xFF)
        elif conv == "s":
            v = read_str(v)
            if prec:
                v = v[:int(prec)]
                prec = None
        elif conv == "p":
            v, conv, flags = v, "x", (flags or "") + "#"
        spec = "%" + (flags or "") + (width or "") + ("." + prec if prec else "") + conv
        out.append(spec % v)
    out.append(fmt[pos:])
    return "".join(out)


class Runtime:
    def __init__(self, program, argv, quiet=False, qpu=None):
        from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM
        from unicorn import arm64_const as A
        self.p, self.A, self.quiet, self.qpu = program, A, quiet, qpu
        self.vqe, self.session = {}, False
        self.mu = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
        page = lambda v: v & ~0xFFF
        for addr, size, content, _ in program.segments:
            lo, hi = page(addr), (addr + size + 0xFFF) & ~0xFFF
            try:
                self.mu.mem_map(lo, hi - lo)
            except Exception:
                pass                                         # shares a page with the previous segment
            self.mu.mem_write(addr, content[:size])
        for addr, target in program.rebases:
            self.mu.mem_write(addr, struct.pack("<Q", target))
        self.mu.mem_map(STACK_TOP - STACK_SIZE, STACK_SIZE)
        self.mu.mem_map(HEAP, HEAP_SIZE)
        self.mu.mem_map(HALT, 0x1000 + 0x100000)
        self.heap = HEAP + 0x1000
        self.handlers, self.data_imports = {}, {}
        names = sorted({n for _, n in program.imports})
        for i, name in enumerate(names):
            if name in DATA_IMPORTS:
                cell = self.alloc(64)
                self.mu.mem_write(cell, struct.pack("<Q", DATA_IMPORTS[name]))
                self.data_imports[name] = cell
                continue
            slot = STUBS + 4 * i
            self.mu.mem_write(slot, struct.pack("<I", 0xD65F03C0))      # RET, if ever executed directly
            self.handlers[slot] = name
        for addr, name in program.imports:
            target = self.data_imports.get(name) or next(s for s, n in self.handlers.items() if n == name)
            self.mu.mem_write(addr, struct.pack("<Q", target))
        self.packets = self.find_packets()
        from unicorn import UC_HOOK_CODE
        self.mu.hook_add(UC_HOOK_CODE, self.on_stub, begin=STUBS, end=STUBS + 4 * len(names))
        self.mu.hook_add(UC_HOOK_CODE, self.on_halt, begin=HALT, end=HALT + 3)
        for addr in self.packets:
            self.mu.hook_add(UC_HOOK_CODE, self.on_packet, begin=addr, end=addr + 3)
        sp = STACK_TOP - 0x1000
        ptrs = []
        for s in argv:
            a = self.alloc(len(s) + 1)
            self.mu.mem_write(a, s.encode() + b"\0")
            ptrs.append(a)
        av = self.alloc(8 * (len(ptrs) + 2))
        self.mu.mem_write(av, b"".join(struct.pack("<Q", x) for x in ptrs) + bytes(16))
        self.mu.reg_write(A.UC_ARM64_REG_X0, len(argv))
        self.mu.reg_write(A.UC_ARM64_REG_X1, av)
        self.mu.reg_write(A.UC_ARM64_REG_X2, av + 8 * len(ptrs))         # envp: empty
        self.mu.reg_write(A.UC_ARM64_REG_X3, av + 8 * len(ptrs))         # apple (macOS): empty
        self.mu.reg_write(A.UC_ARM64_REG_SP, sp)
        self.mu.reg_write(A.UC_ARM64_REG_LR, HALT)
        self.exit_code, self.out = None, sys.stdout

    def alloc(self, n):
        a = self.heap
        self.heap += (n + 15) & ~15
        if self.heap > HEAP + HEAP_SIZE:
            raise QarmError("out of runtime memory")
        return a

    def find_packets(self):
        found = []
        for lo, size in self.p.code_ranges or [self.p.text]:
            found += self._packets_in(lo, size)
        return sorted(set(found))

    def _packets_in(self, lo, size):
        code = bytes(self.mu.mem_read(lo, size))
        found = []
        for off in range(0, len(code) - 31, 4):
            w0, w1, w2 = struct.unpack_from("<III", code, off)
            if w0 == PACKET_BRANCH and w1 == PACKET_MAGIC:
                version, op = w2 >> 24, (w2 >> 16) & 0xFF
                if version == 2 and op in OPS:
                    found.append(lo + off)
                elif version == 1:
                    raise QarmError(f"Q-ARM v1 gate packet at {lo + off:#x}: run it with qarm.py")
        return found

    # registers and memory
    vregs = None                                   # QSUBMIT: the request's registers X0-X5, D0, D1

    def x(self, n):
        if self.vregs is not None and n < 6:
            return self.vregs["x"][n]
        return self.mu.reg_read(getattr(self.A, f"UC_ARM64_REG_X{n}"))

    def set_x(self, n, v):
        if self.vregs is not None and n < 6:
            self.vregs["x"][n] = v & 0xFFFFFFFFFFFFFFFF
            return
        self.mu.reg_write(getattr(self.A, f"UC_ARM64_REG_X{n}"), v & 0xFFFFFFFFFFFFFFFF)

    def cstr(self, addr, limit=1 << 16):
        out = bytearray()
        while len(out) < limit:
            chunk = bytes(self.mu.mem_read(addr + len(out), 64))
            k = chunk.find(b"\0")
            if k >= 0:
                return (out + chunk[:k]).decode("utf-8", "replace")
            out += chunk
        return out.decode("utf-8", "replace")

    def vararg(self, first_reg):
        """The variadic arguments in turn: get(floating) -> 64 bits. On macOS all on the stack; on Linux integers in
        X registers and doubles in D registers, then both on the stack."""
        sp = self.mu.reg_read(self.A.UC_ARM64_REG_SP)
        state = {"x": first_reg, "d": 0, "stack": 0}
        stack = lambda: struct.unpack("<Q", self.mu.mem_read(sp + 8 * state["stack"], 8))[0]

        def get(floating=False):
            if self.p.kind != "macos":
                kind, limit = ("d", 8) if floating else ("x", 8)
                if state[kind] < limit:
                    r = state[kind]
                    state[kind] += 1
                    return self.mu.reg_read(self.A.UC_ARM64_REG_D0 + r) if floating else self.x(r)
            v = stack()
            state["stack"] += 1
            return v
        return get

    def ret(self, value=None):
        if value is not None:
            self.set_x(0, value)
        self.mu.reg_write(self.A.UC_ARM64_REG_PC, self.mu.reg_read(self.A.UC_ARM64_REG_LR))

    # hooks
    def on_halt(self, mu, address, size, user):
        self.exit_code = self.x(0) & 0xFF
        mu.emu_stop()

    def on_stub(self, mu, address, size, user):
        name = self.handlers[address]
        fn = LIBC.get(name)
        if fn is None:
            raise QarmError(f"the program calls {name}(), which this runtime does not provide")
        fn(self)

    def on_packet(self, mu, address, size, user):
        w2 = struct.unpack("<I", mu.mem_read(address + 8, 4))[0]
        op = (w2 >> 16) & 0xFF
        if op == OP_QSUBMIT:
            self.qsubmit(self.x(1))
        elif op in (OP_QPOLL, OP_QWAIT):           # every request is done at QSUBMIT: this runtime is synchronous
            state = struct.unpack("<I", bytes(mu.mem_read(self.x(1) + 4, 4)))[0]
            self.set_x(0, 1)
            if op == OP_QPOLL:
                self.set_x(1, state)
        else:
            self.dispatch(op)
        mu.reg_write(self.A.UC_ARM64_REG_PC, address + 32)

    REQUEST = struct.Struct("<II6Q2dQ")            # qarm_request: opcode, state, x[6], d[2], ticket

    def qsubmit(self, addr):
        """QSUBMIT: the request's instruction, run now on a register file of its own; state 1 at once."""
        op, _, *rest = self.REQUEST.unpack(bytes(self.mu.mem_read(addr, self.REQUEST.size)))
        xs, ds = list(rest[:6]), list(rest[6:8])
        if op not in OPS or op in (OP_QSUBMIT, OP_QPOLL, OP_QWAIT):
            self.set_x(0, 2)
            self.note(f"QSUBMIT: opcode {op:#x} cannot be submitted")
            return
        self.vregs = {"x": [0] + xs[1:], "d": ds}
        try:
            self.dispatch(op)
            v = self.vregs
        finally:
            self.vregs = None
        self.mu.mem_write(addr, self.REQUEST.pack(op, 1, *v["x"], *v["d"], addr))
        self.set_x(0, 1)

    def dispatch(self, op):
        if op == OP_QAVAIL:
            self.set_x(0, 1)
            self.set_x(1, FEATURES)
        elif op == OP_QSEARCH:
            self.qsearch(self.x(1), self.x(2) & 0xFFFFFFFF)
        elif op == OP_QMIN:
            self.qmin(self.x(1), self.x(2) & 0xFFFFFFFF)
        elif op == OP_QCOLLISION:
            self.qcollision(self.x(1), self.x(2) & 0xFFFFFFFF)
        elif op == OP_QCOUNT:
            self.qcount(self.x(1), self.x(2) & 0xFFFFFFFF)
        elif op == OP_QCIRC:
            self.qcirc(self.x(1), self.x(2) & 0xFFFFFFFF, self.x(3))
        elif op == OP_QRAND:
            self.qrand(self.x(1), self.x(2) & 0xFFFFFFFF)
        elif op == OP_QCAPS:
            self.qcaps(self.x(1))
        elif op == OP_QAMPEST:
            self.qampest(self.x(1), self.x(2) & 0xFFFFFFFF, self.x(3) & 0xFFFFFFFF)
        elif op in (OP_QPERIOD, OP_QSIMON, OP_QBV):
            self.qstructure(op, self.x(1), self.x(2) & 0xFFFFFFFF)
        elif op in (OP_QVLOAD, OP_QEXPECT, OP_QEXPECTN, OP_QGRAD, OP_QVSAMPLE, OP_QSESSION, OP_QVFREE):
            self.vqe_op(op)

    def call(self, fn, arg):
        """Call a function of the program: f(arg) -> W0 (used to check answers)."""
        A = self.A
        saved = {r: self.mu.reg_read(getattr(A, f"UC_ARM64_REG_{r}")) for r in
                 ["SP", "LR", "PC", "NZCV"] + [f"X{i}" for i in range(31)]}
        self.mu.reg_write(A.UC_ARM64_REG_X0, arg)
        self.mu.reg_write(A.UC_ARM64_REG_LR, HALT + 0x800)
        self.mu.reg_write(A.UC_ARM64_REG_SP, saved["SP"] - 0x10000)
        self.mu.emu_start(fn, HALT + 0x800, count=1_000_000)
        v = self.mu.reg_read(A.UC_ARM64_REG_X0) & 0xFFFFFFFF
        for r, val in saved.items():
            self.mu.reg_write(getattr(A, f"UC_ARM64_REG_{r}"), val)
        return v

    # the quantum coprocessor
    def qsearch(self, fn, bits):
        name = self.p.functions.get(fn, f"function at {fn:#x}")
        if not 1 <= bits <= 16:
            self.set_x(0, 0)
            raise QarmError(f"QSEARCH {name}: bits = {bits}; 1 to 16")
        circuit = phase_oracle(self, fn, name, bits)
        if self.qpu is not None and self.qpu["backend"] != "cudaq":   # cudaq: the oracle searches stay exact
            return self.qsearch_qpu(fn, name, bits, circuit)
        x, calls, rounds = grover(circuit, bits, lambda v: self.call(fn, v) == 1)
        if x is None:
            self.set_x(0, 2)
            self.note(f"QSEARCH {name}, {bits} bits: Grover's search, nothing found in {calls} oracle calls "
                      f"({rounds} rounds)")
        else:
            self.set_x(0, 1)
            self.set_x(3, x)
            self.note(f"QSEARCH {name}, {bits} bits: Grover's search with a {circuit['qubits']}-qubit, "
                      f"{circuit['gates']}-gate oracle from {name}'s machine code: x = {x} after {calls} oracle "
                      f"calls in {rounds} rounds, checked by running {name}")

    def qmin(self, fn, bits):
        """QMIN: X1 = cost, W2 = bits; W0 = 1, W3 = an x with the least cost(x), W4 = cost(x) (signed). Durr-Hoyer:
        Grover searches for an x cheaper than the best so far, with cost's compute oracle from its machine code."""
        name = self.p.functions.get(fn, f"function at {fn:#x}")
        if not 1 <= bits <= 16:
            raise QarmError(f"QMIN {name}: bits = {bits}; 1 to 16")
        circuit = compute_oracle(self, fn, name, bits)
        values = oracle_values(circuit, bits)
        x, calls, searches = durr_hoyer(values, bits)
        v = self.call(fn, x)
        v = v - (1 << 32) if v >= 1 << 31 else v
        if v != values[x]:
            raise QarmError(f"QMIN {name}: the oracle gives {values[x]} for x = {x}, {name} gives {v}")
        self.set_x(0, 1)
        self.set_x(3, x)
        self.set_x(4, v & 0xFFFFFFFF)
        self.note(f"QMIN {name}, {bits} bits: Durr-Hoyer with a {circuit['qubits']}-qubit, {circuit['gates']}-gate "
                  f"compute oracle ({circuit['out_bits']} result bits) from {name}'s machine code: x = {x}, "
                  f"{name}(x) = {v}, {calls} oracle calls in {searches} Grover searches (simulated)")

    def qcollision(self, fn, bits):
        """QCOLLISION: X1 = f, W2 = bits; W0 = 1, W3 = x, W4 = y with x != y and f(x) = f(y), or W0 = 2: none found.
        Brassard-Hoyer-Tapp: f on a random set K of about N^(1/3) inputs, then Grover's search (BBHT) for an x
        outside K whose f(x) is among f(K): O(N^(1/3)) oracle calls in all, against O(sqrt N) classically."""
        name = self.p.functions.get(fn, f"function at {fn:#x}")
        if not 1 <= bits <= 16:
            raise QarmError(f"QCOLLISION {name}: bits = {bits}; 1 to 16")
        circuit = compute_oracle(self, fn, name, bits, "QCOLLISION")
        values = oracle_values(circuit, bits)
        pair, calls, tries = collision(values, bits)
        if pair is None:
            self.set_x(0, 2)
            self.note(f"QCOLLISION {name}, {bits} bits: no collision found in {calls} oracle calls ({tries} tries)")
            return
        x, y = pair
        fx, fy = self.call(fn, x), self.call(fn, y)
        if fx != fy:
            raise QarmError(f"QCOLLISION {name}: the oracle says f({x}) = f({y}), {name} gives {fx} and {fy}")
        self.set_x(0, 1)
        self.set_x(3, x)
        self.set_x(4, y)
        self.note(f"QCOLLISION {name}, {bits} bits: Brassard-Hoyer-Tapp with a {circuit['qubits']}-qubit compute "
                  f"oracle from {name}'s machine code: {name}({x}) = {name}({y}) = {fx}, {calls} oracle calls "
                  f"(a table of {round((1 << bits) ** (1 / 3))}, then Grover; simulated)")

    def qcount(self, fn, bits):
        """QCOUNT: X1 = f, W2 = bits; W0 = 1, X3 = the estimated number of x with f(x) = 1, D0 = its error bound.
        Quantum counting: phase estimation of the Grover operator (simulated from its exact outcome distribution)."""
        name = self.p.functions.get(fn, f"function at {fn:#x}")
        if not 1 <= bits <= 16:
            raise QarmError(f"QCOUNT {name}: bits = {bits}; 1 to 16")
        circuit = phase_oracle(self, fn, name, bits, "QCOUNT")
        sign = oracle_signs(circuit, bits)
        est, bound, t, calls = quantum_count(int((sign < 0).sum()), bits)
        self.set_x(0, 1)
        self.set_x(3, est)
        self.set_d(0, bound)
        self.note(f"QCOUNT {name}, {bits} bits: quantum counting with {t} precision qubits (the median of "
                  f"{MEDIAN_OF} runs), {calls} controlled Grover iterations, the {circuit['qubits']}-qubit oracle from {name}'s machine code: about {est} "
                  f"(error bound {bound:.1f}; simulated)")

    def qvsample(self, problem, measured):
        """QVSAMPLE: X1 = theta, W2 = shots, X3 = the handle, X4 = shots uint64 outcomes of |psi(theta)> in the Z
        basis (bit q = qubit q). Exact: the state vector, sampled; on a backend: the ansatz circuit, measured."""
        import numpy as np
        shots = self.x(2) & 0xFFFFFFFF
        if not 1 <= shots <= 1 << 22:
            raise QarmError(f"QVSAMPLE: {shots} shots")
        theta = self.doubles(self.x(1), problem.params)
        if measured is None:
            p = np.abs(problem._prepare(theta)) ** 2
            rng = np.random.default_rng(int(os.environ["QCPU_SEED"]) if os.environ.get("QCPU_SEED") else None)
            out, where = [int(v) for v in rng.choice(len(p), size=shots, p=p / p.sum())], "the exact simulator"
        else:
            out, job, sec = measured.sample(problem.angles(theta), shots)
            where = f"{measured.where.split(';')[0]} (job {job}, {sec} s)"
        self.mu.mem_write(self.x(4), struct.pack(f"<{shots}Q", *out))
        self.set_x(0, 1)
        self.note(f"QVSAMPLE: {problem.n} qubits, {shots} shots of the ansatz state on {where}")

    def read_circuit(self, addr):
        """A qarm_circuit {u32 qubits, gates; qarm_gate *g} and its gates {u8 kind, q0, q1, q2; u32; double angle}."""
        n, count, gp = struct.unpack("<IIQ", bytes(self.mu.mem_read(addr, 16)))
        if not 1 <= n <= 64 or count > 100000:
            raise QarmError(f"QCIRC: a circuit of {n} qubits and {count} gates")
        raw = bytes(self.mu.mem_read(gp, 16 * count)) if count else b""
        gates = []
        for i in range(count):
            k, q0, q1, q2, _, ang = struct.unpack_from("<BBBBId", raw, 16 * i)
            name = GATES.get(k)
            arity = 3 if name == "ccx" else 2 if name in ("cx", "cz", "swap") else 1
            qs = (q0, q1, q2)[:arity]
            if name is None or any(q >= n for q in qs) or len(set(qs)) != arity:
                raise QarmError(f"QCIRC: gate {i}: kind {k}, qubits {qs} of {n}")
            gates.append((name, qs, ang))
        return n, gates

    def qcirc(self, addr, shots, out):
        """QCIRC: X1 = a qarm_circuit, W2 = shots, X3 = shots uint64 outcomes (bit q = qubit q); W0 = 1, or 2:
        refused. Exact: the state vector, sampled; --qpu: config.json's backend (a real device's samples on ibm)."""
        try:
            n, gates = self.read_circuit(addr)
        except QarmError as e:
            self.set_x(0, 2)
            self.note(str(e))
            return
        if self.qpu is None:
            if n > 20:
                self.set_x(0, 2)
                self.note(f"QCIRC: {n} qubits; the exact simulator takes up to 20")
                return
            outcomes, where = circuit_samples(n, gates, shots), "the exact simulator"
        else:
            outcomes, where = circuit_on_backend(n, gates, shots, self.qpu, self.note)
        self.mu.mem_write(out, struct.pack(f"<{shots}Q", *outcomes))
        self.set_x(0, 1)
        twoq = sum(1 for g in gates if g[0] in ("cx", "cz", "swap", "ccx"))
        self.note(f"QCIRC: {n} qubits, {len(gates)} gates ({twoq} multi-qubit), {shots} shots on {where}")

    def qrand(self, buf, n):
        """QRAND: X1 = a buffer, W2 = bytes: 8 qubits in H, measured once per byte. W0 = 1 on a real device
        (quantum randomness), 3 on a simulator (pseudo-random: numpy's generator)."""
        gates = [("h", (q,), 0.0) for q in range(8)]
        if self.qpu is None:
            outcomes, where = circuit_samples(8, gates, n), "the exact simulator: pseudo-random"
        else:
            outcomes, where = circuit_on_backend(8, gates, n, self.qpu, self.note)
        self.mu.mem_write(buf, bytes(o & 0xFF for o in outcomes))
        self.set_x(0, 1 if self.qpu is not None and self.qpu["backend"] == "ibm" else 3)
        self.note(f"QRAND: {n} bytes from H on 8 qubits, measured on {where}")

    def qcaps(self, addr):
        """QCAPS: X1 = a qarm_capabilities {u32 version, features, qubits, flags; char backend[48]}."""
        if self.qpu is None:
            qubits, flags, name = 20, 0, "qarm_exec.py: exact simulator"
        else:
            b = self.qpu["backend"]
            name = f"qarm_exec.py: {b}" + (f" {self.qpu['ibm'].get('backend')}" if b == "ibm" else "")
            qubits, flags = (156 if b in ("ibm", "preview") else 30), (1 if b == "ibm" else 0) | 2 | (4 if b == "ibm" else 0)
            if b == "cudaq":
                name, flags = f"qarm_exec.py: CUDA-Q {self.qpu.get('cudaq', {}).get('target') or 'default target'}", 0
        self.mu.mem_write(addr, struct.pack("<IIII48s", 1, FEATURES, qubits, flags, name.encode()[:47]))
        self.set_x(0, 1)

    def qampest(self, fn, bits, vbits):
        """QAMPEST: X1 = g, W2 = bits, W3 = value bits k (g(x) in 0 .. 2^k - 1); D0 = the estimated mean of g over all
        x, D1 = its error bound. Amplitude estimation (Brassard, Hoyer, Mosca, Tapp): the mean a = E[g] / 2^k as the
        amplitude of a marked state, read by phase estimation (simulated from its exact outcome distribution)."""
        name = self.p.functions.get(fn, f"function at {fn:#x}")
        if not 1 <= bits <= 16 or not 1 <= vbits <= 31:
            raise QarmError(f"QAMPEST {name}: bits = {bits}, value bits = {vbits}; 1 to 16, 1 to 31")
        circuit = compute_oracle(self, fn, name, bits, "QAMPEST")
        values = oracle_values(circuit, bits)
        if values.min() < 0 or values.max() >= 1 << vbits:
            raise QarmError(f"QAMPEST {name}: values from {values.min()} to {values.max()}, outside 0 .. 2^{vbits} - 1")
        a = float(values.mean()) / (1 << vbits)
        est, bound, t, calls = amplitude_estimate(a, bits)
        scale = 1 << vbits
        self.set_x(0, 1)
        self.set_d(0, est * scale)
        self.set_d(1, bound * scale)
        mc = int(est * (1 - est) / max(bound, 1e-12) ** 2)
        self.note(f"QAMPEST {name}, {bits} bits: amplitude estimation with {t} precision qubits (the median of "
                  f"{MEDIAN_OF} runs, {calls} queries; "
                  f"Monte Carlo would need about {mc} samples for the same standard error), the "
                  f"{circuit['qubits']}-qubit oracle from {name}'s machine code: mean {est * scale:.4f} +- "
                  f"{bound * scale:.4f} (simulated)")

    def qstructure(self, op, fn, bits):
        """QPERIOD, QSIMON, QBV: X1 = f, W2 = bits; W0 = 1 and W3 = the period r, the hidden shift s or the hidden
        string s; W0 = 2: none (QPERIOD, QSIMON). Oracles from f's machine code; simulated."""
        kind = {OP_QPERIOD: "QPERIOD", OP_QSIMON: "QSIMON", OP_QBV: "QBV"}[op]
        name = self.p.functions.get(fn, f"function at {fn:#x}")
        if not 1 <= bits <= 16:
            raise QarmError(f"{kind} {name}: bits = {bits}; 1 to 16")
        if op == OP_QBV:
            circuit = phase_oracle(self, fn, name, bits, kind)
            r, queries = bernstein_vazirani(oracle_signs(circuit, bits), bits)
            what = "phase oracle"
        else:
            circuit = compute_oracle(self, fn, name, bits, kind)
            values = oracle_values(circuit, bits)
            r, queries = (period_find if op == OP_QPERIOD else simon)(values, bits)
            what = "compute oracle"
        head = (f"{kind} {name}, {bits} bits: the {circuit['qubits']}-qubit, {circuit['gates']}-gate {what} from "
                f"{name}'s machine code, {queries} quantum quer{'y' if queries == 1 else 'ies'} (simulated)")
        if r is None:
            self.set_x(0, 2)
            self.note(f"{head}: none")
        else:
            self.set_x(0, 1)
            self.set_x(3, r)
            label = {"QPERIOD": "period r", "QSIMON": "hidden shift s", "QBV": "hidden string s"}[kind]
            self.note(f"{head}: {label} = {r}" + (f" ({r:#x})" if kind != "QPERIOD" else ""))

    def qsearch_qpu(self, fn, name, bits, circuit):
        """QSEARCH on config.json's backend: one job, then the answers checked by running f."""
        x, info = grover_qpu(circuit, bits, self.qpu, lambda v: self.call(fn, v) == 1, self.note)
        head = (f"QSEARCH {name}, {bits} bits on {info['backend']}: {circuit['qubits']} qubits, "
                f"{info['circuits']} circuits ({info['shots']} shots each, up to {info['two_qubit_gates']} two-qubit "
                f"gates), job {info['job_id']}, {info['seconds']} s")
        if x is None:
            self.set_x(0, 2)
            self.note(f"{head}: none of the {info['distinct']} measured answers passes {name}")
        else:
            self.set_x(0, 1)
            self.set_x(3, x)
            self.note(f"{head}: x = {x}, measured {info['hits']} times of {info['total']} "
                      f"({info['rank']}. most frequent answer), checked by running {name}")

    # VQE (qarm_vqe.py)
    def set_d(self, n, v):
        if self.vregs is not None:
            self.vregs["d"][n] = float(v)
            return
        self.mu.reg_write(self.A.UC_ARM64_REG_D0 + n, struct.unpack("<Q", struct.pack("<d", v))[0])

    def doubles(self, addr, n):
        return list(struct.unpack(f"<{n}d", bytes(self.mu.mem_read(addr, 8 * n)))) if n else []

    def vqe_op(self, op):
        from unicorn import UcError
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import qarm_vqe
        try:
            if op == OP_QVLOAD:
                read = lambda a, n: bytes(self.mu.mem_read(a, n))
                problem = qarm_vqe.Problem(read, self.x(1), self.x(2))
                if self.qpu is not None and self.qpu["backend"] == "cudaq":
                    measured = qarm_vqe.Cudaq(problem, self.qpu, self.note)
                    where = f"on CUDA-Q ({measured.where})"
                elif self.qpu is not None:
                    measured = qarm_vqe.Measured(problem, self.qpu, self.note)
                    where = (f"on {measured.where.split(';')[0]}: {len(measured.isa)} measurement bases"
                             + (", readout correction" if measured.mitigate else "") + ", up to "
                             f"{measured.two_qubit} two-qubit gates after transpiling")
                else:
                    if problem.n > qarm_vqe.MAX_EXACT_QUBITS:
                        raise qarm_vqe.VqeError(f"{problem.n} qubits: the exact simulator takes up to "
                                                f"{qarm_vqe.MAX_EXACT_QUBITS}")
                    measured, where = None, "on the exact simulator"
                handle = len(self.vqe) + 1
                self.vqe[handle] = (problem, measured)
                if measured is not None and self.session:
                    self.note("QSESSION: " + measured.open_session())
                self.set_x(0, 1)
                self.set_x(3, handle)
                self.note(f"QVLOAD: {problem.describe()}; {where}")
            elif op == OP_QSESSION:
                self.session = bool(self.x(1) & 0xFFFFFFFF)
                done = []
                for problem, measured in self.vqe.values():
                    if measured is not None:
                        done.append(measured.open_session() if self.session else (measured.close_session() or "closed"))
                self.set_x(0, 1)
                if self.qpu is not None and done:
                    self.note("QSESSION: " + "; ".join(done))
            elif op == OP_QVFREE:
                entry = self.vqe.pop(self.x(3), None)
                if entry and entry[1] is not None:
                    entry[1].close_session()
                self.set_x(0, 1)
            else:
                entry = self.vqe.get(self.x(3))
                if entry is None:
                    raise qarm_vqe.VqeError(f"no problem loaded with handle {self.x(3)}")
                problem, measured = entry
                if op == OP_QGRAD:
                    self.qgrad(problem, measured)
                    return
                if op == OP_QVSAMPLE:
                    self.qvsample(problem, measured)
                    return
                k = 1 if op == OP_QEXPECT else self.x(2) & 0xFFFFFFFF
                if not 1 <= k <= 4096:
                    raise qarm_vqe.VqeError(f"QEXPECTN: k = {k}; 1 to 4096")
                thetas = [self.doubles(self.x(1) + 8 * problem.params * i, problem.params) for i in range(k)]
                shots = (self.x(2) & 0xFFFFFFFF) if op == OP_QEXPECT else 0
                if measured is None:
                    es, errs, how = problem.exact(thetas), [0.0] * k, "exact"
                else:
                    es, errs, job, sec = measured.energies([problem.angles(t) for t in thetas], shots)
                    how = f"job {job}, {sec} s"
                name = "QEXPECT" if op == OP_QEXPECT else "QEXPECTN"
                if op == OP_QEXPECT:
                    self.set_d(0, es[0])
                    self.set_d(1, errs[0])
                    self.note(f"{name}: E = {es[0]:.6f}" + (f" +- {errs[0]:.6f}" if errs[0] else "") + f" Ha ({how})")
                else:
                    self.mu.mem_write(self.x(4), struct.pack(f"<{k}d", *es))
                    m = min(range(k), key=lambda i: es[i])
                    self.note(f"{name}: {k} energies ({how}); lowest {es[m]:.6f} Ha" +
                              (f" +- {errs[m]:.6f}" if errs[m] else ""))
                self.set_x(0, 1)
        except qarm_vqe.VqeError as e:
            self.set_x(0, 2)
            self.note(f"VQE instruction refused: {e}")
            if op != OP_QVLOAD:
                raise QarmError(f"VQE: {e}")
        except UcError as e:
            raise QarmError(f"VQE: a table or an array is not readable or writable: {e}")

    def qgrad(self, problem, measured):
        """QGRAD: X1 = theta, X4 = the gradient (one double per parameter); D0 = the energy. Exact: adjoint
        differentiation. Measured: the parameter-shift rule, every shifted energy in one job."""
        theta = self.doubles(self.x(1), problem.params)
        if measured is None:
            e, g = problem.grad(theta)
            how = "exact, adjoint"
        else:
            rot = problem.rotations()
            settings, coef = [problem.angles(theta)], []
            for r, i in enumerate(rot):
                d = math.pi / 4 if problem.ops[i][0] == "pauli_rot" else math.pi / 2
                settings += [problem.angles(theta, r, d), problem.angles(theta, r, -d)]
                coef.append((problem.ops[i][3], problem.ops[i][4] * (1.0 if d == math.pi / 4 else 0.5)))
            es, errs, job, sec = measured.energies(settings, 0)
            e, g = es[0], [0.0] * problem.params
            for r, (p, f) in enumerate(coef):
                g[p] += f * (es[1 + 2 * r] - es[2 + 2 * r])
            how = f"parameter-shift rule, {len(settings) - 1} shifted energies, job {job}, {sec} s"
        self.mu.mem_write(self.x(4), struct.pack(f"<{problem.params}d", *g))
        self.set_d(0, e)
        self.set_x(0, 1)
        self.note(f"QGRAD: E = {e:.9f} Ha, |gradient| = {math.sqrt(sum(v * v for v in g)):.3g} ({how})")

    def note(self, text):
        if not self.quiet:
            print(f"[qarm: {text}]", file=sys.stderr, flush=True)

    def run(self):
        from unicorn import UcError
        try:
            self.mu.emu_start(self.p.main, HALT + 0xF00)       # never reached: main returns to HALT, which stops
        except UcError as e:
            pc = self.mu.reg_read(self.A.UC_ARM64_REG_PC)
            raise QarmError(f"the program stopped at {pc:#x}: {e}")
        return self.exit_code if self.exit_code is not None else 0


def _printf(rt):
    rt.out.write(c_format(rt.cstr(rt.x(0)), rt.vararg(1), rt.cstr))
    rt.ret(0)


def _printf_chk(rt):                    # glibc's fortified printf: (flag, format, ...)
    rt.out.write(c_format(rt.cstr(rt.x(1)), rt.vararg(2), rt.cstr))
    rt.ret(0)


def _puts(rt):
    rt.out.write(rt.cstr(rt.x(0)) + "\n")
    rt.ret(0)


def _putchar(rt):
    rt.out.write(chr(rt.x(0) & 0xFF))
    rt.ret(rt.x(0) & 0xFF)


def _stream(rt, fp):
    return sys.stderr if fp == STDERR_FILE else rt.out


def _putc(rt):                          # putc / fputc (c, FILE *)
    _stream(rt, rt.x(1)).write(chr(rt.x(0) & 0xFF))
    rt.ret(rt.x(0) & 0xFF)


def _fputs(rt):                         # fputs (s, FILE *)
    _stream(rt, rt.x(1)).write(rt.cstr(rt.x(0)))
    rt.ret(0)


def _fwrite(rt):                        # fwrite (p, size, n, FILE *)
    n = rt.x(1) * rt.x(2)
    _stream(rt, rt.x(3)).write(bytes(rt.mu.mem_read(rt.x(0), n)).decode("utf-8", "replace"))
    rt.ret(rt.x(2))


def _fflush(rt):
    rt.out.flush()
    rt.ret(0)


def _exit(rt):
    rt.out.flush()
    rt.exit_code = rt.x(0) & 0xFF
    rt.mu.emu_stop()


def _strlen(rt):
    rt.ret(len(rt.cstr(rt.x(0)).encode("utf-8")))


def _memset(rt):
    d, c, n = rt.x(0), rt.x(1) & 0xFF, rt.x(2)
    rt.mu.mem_write(d, bytes([c]) * n)
    rt.ret(d)


def _malloc(rt):                        # a bump allocator: free() does nothing; the size is kept before the block
    n = rt.x(0)
    a = rt.alloc(n + 16) + 16
    rt.mu.mem_write(a - 8, struct.pack("<Q", n))
    rt.ret(a)


def _calloc(rt):                        # fresh heap memory is zero
    rt.mu.reg_write(rt.A.UC_ARM64_REG_X0, rt.x(0) * rt.x(1))
    _malloc(rt)


def _realloc(rt):
    old, n = rt.x(0), rt.x(1)
    a = rt.alloc(n + 16) + 16
    rt.mu.mem_write(a - 8, struct.pack("<Q", n))
    if old:
        size = struct.unpack("<Q", bytes(rt.mu.mem_read(old - 8, 8)))[0]
        rt.mu.mem_write(a, bytes(rt.mu.mem_read(old, min(size, n))))
    rt.ret(a)


def _free(rt):
    rt.ret(0)


def _bzero(rt):
    rt.mu.mem_write(rt.x(0), bytes(rt.x(1)))
    rt.ret(0)


def _memcpy(rt):
    d, s, n = rt.x(0), rt.x(1), rt.x(2)
    rt.mu.mem_write(d, bytes(rt.mu.mem_read(s, n)))
    rt.ret(d)


def _stack_chk_fail(rt):
    raise QarmError("stack smashing detected")


def _math(fn):
    """A libm function of one double: the argument in D0, the result in D0."""
    def call(rt):
        v = struct.unpack("<d", struct.pack("<Q", rt.mu.reg_read(rt.A.UC_ARM64_REG_D0)))[0]
        try:
            r = fn(v)
        except (ValueError, OverflowError):
            r = float("nan")
        rt.set_d(0, r)
        rt.ret(None)
    return call


def _strcmp(rt):
    a, b = rt.cstr(rt.x(0)).encode("utf-8"), rt.cstr(rt.x(1)).encode("utf-8")
    rt.ret((a > b) - (a < b) & 0xFFFFFFFFFFFFFFFF)


def _strncmp(rt):
    n = rt.x(2)
    a, b = rt.cstr(rt.x(0)).encode("utf-8")[:n], rt.cstr(rt.x(1)).encode("utf-8")[:n]
    rt.ret((a > b) - (a < b) & 0xFFFFFFFFFFFFFFFF)


def _atoi(rt):
    import re as _re
    m = _re.match(r"\s*([+-]?\d+)", rt.cstr(rt.x(0)))
    rt.ret((int(m.group(1)) if m else 0) & 0xFFFFFFFFFFFFFFFF)


def _strtol(rt):
    """strtol/strtoul(s, endptr, base): base 0 (prefix), 2..36; *endptr after the digits."""
    import re as _re
    addr, endp, base = rt.x(0), rt.x(1), rt.x(2) & 0xFFFFFFFF
    text = rt.cstr(addr)
    m = _re.match(r"\s*([+-]?)(0[xX])?([0-9a-zA-Z]*)", text)
    sign, prefix, digits = m.group(1), m.group(2), m.group(3)
    if base == 0:
        base = 16 if prefix else (8 if digits.startswith("0") and len(digits) > 1 else 10)
    elif base != 16 and prefix:
        digits, prefix = "0", None
    used = ""
    for ch in digits:
        if int(ch, 36) >= base:
            break
        used += ch
    value = int(used, base) if used else 0
    consumed = (m.start(3) + len(used)) if used else 0
    if endp:
        rt.mu.mem_write(endp, struct.pack("<Q", addr + len(text[:consumed].encode("utf-8"))))
    rt.ret((-value if sign == "-" else value) & 0xFFFFFFFFFFFFFFFF)


MATH = {name: _math(getattr(math, name)) for name in ("sin", "cos", "tan", "exp", "log", "sqrt", "atan", "fabs")}


LIBC = {**MATH, "strcmp": _strcmp, "strncmp": _strncmp, "atoi": _atoi, "atol": _atoi, "strtol": _strtol, "strtoul": _strtol,
        "strtoll": _strtol, "strtoull": _strtol, "__isoc23_strtol": _strtol, "__isoc23_strtoul": _strtol, "printf": _printf, "__printf_chk": _printf_chk, "puts": _puts, "putchar": _putchar, "exit": _exit, "_exit": _exit, "strlen": _strlen,
        "memset": _memset, "memcpy": _memcpy, "memmove": _memcpy, "__stack_chk_fail": _stack_chk_fail,
        "__memset_chk": _memset, "bzero": _bzero, "__bzero": _bzero, "malloc": _malloc, "calloc": _calloc,
        "realloc": _realloc, "free": _free, "__memcpy_chk": _memcpy, "putc": _putc, "fputc": _putc, "_IO_putc": _putc,
        "fputs": _fputs, "fwrite": _fwrite, "fflush": _fflush}
STDOUT_FILE, STDERR_FILE = 0x5100, 0x5200      # what the program sees as FILE *stdout, *stderr
DATA_IMPORTS = {"__stack_chk_guard": 0x2F8A5C3E91D04B76, "stdout": STDOUT_FILE, "stderr": STDERR_FILE,
                "__stdoutp": STDOUT_FILE, "__stderrp": STDERR_FILE}


# ------------------------------------------------------------------ oracle and Grover's search
def _explain(rt, e):
    """qoracle's error, with the undecodable instruction's word and disassembly when it names an address."""
    import re as _re
    text = str(e)
    m = _re.search(r"at offset (0x[0-9a-f]+)", text)
    if m:
        addr = int(m.group(1), 16)
        try:
            word = bytes(rt.mu.mem_read(addr, 4))
            text += f" (word {int.from_bytes(word, 'little'):08x}"
            try:
                import capstone
                md = capstone.Cs(capstone.CS_ARCH_ARM64, capstone.CS_MODE_ARM)
                ins = next(md.disasm(word, addr), None)
                if ins:
                    text += f": {ins.mnemonic} {ins.op_str}"
            except ImportError:
                pass
            text += ")"
        except Exception:
            pass
    return text


def phase_oracle(rt, fn, name, bits, kind="QSEARCH"):
    """qoracle's phase oracle of f, from the program's machine code at the program's own addresses."""
    try:
        import qoracle
        from qoracle import frontend
    except ImportError:
        raise QarmError("QSEARCH needs qoracle: python3 -m pip install git+https://github.com/quantag/qoracle")
    lo, size = rt.p.code_segment                         # the segment with the code, constants included
    page = lo & ~0xFFF
    section = bytes(rt.mu.mem_read(page, ((lo + size + 0xFFF) & ~0xFFF) - page))
    data = [(a & ~0xFFF, bytes(rt.mu.mem_read(a & ~0xFFF, ((a + s + 0xFFF) & ~0xFFF) - (a & ~0xFFF))), seg)
            for a, s, _, seg in rt.p.segments if not (a <= lo < a + s)]
    image = frontend.Image(code=rt.p.function_bytes(fn, rt.mu), entry=fn, section=section, section_addr=page,
                           data=data, arch=getattr(rt.p, "arch", "aarch64"))
    load = frontend.load
    frontend.load = lambda obj, function: image        # qoracle.build reads this image instead of an object file
    try:
        spec = [qoracle.Arg(name="x", bits=bits)]
        full = qoracle.build(name, name, spec, out_bits=32)
        if full.out_bits != 1:
            if not full.report["verification"]["exhaustive"]:
                raise QarmError(f"{kind} {name}: its result is not proven to be 0 or 1")
            from qoracle import verify
            cpu = verify.CPU(image)
            for v in range(1 << bits):
                if cpu.call([v]) & 0xFFFFFFFF not in (0, 1):
                    raise QarmError(f"{kind} {name}: {name}({v}) is neither 0 nor 1")
        ph = qoracle.build(name, name, spec, out_bits=1, mode="phase")
    except qoracle.OracleError as e:
        raise QarmError(f"{kind} {name}: qoracle: {_explain(rt, e)}")
    finally:
        frontend.load = load
    c = ph.circuit
    first = dict((n, f) for n, f, _ in c.regs)["x"]
    return {"gates": len([g for g in c.gates if g[0] != "gphase"]), "qubits": c.nqubits, "list": c.gates,
            "n": c.nqubits, "first": first}


def oracle_signs(circuit, bits):
    """(-1)^f(x) for every x: the phase oracle's gates on all 2^bits basis inputs at once (bit planes)."""
    import numpy as np
    N = 1 << bits
    xs = np.arange(N, dtype=np.int64)
    q = [np.zeros(N, dtype=bool) for _ in range(circuit["n"])]
    for b in range(bits):
        q[circuit["first"] + b] = ((xs >> b) & 1).astype(bool)
    sign = np.ones(N, dtype=np.int8)
    for g in circuit["list"]:
        if g[0] == "x":
            q[g[1]] = ~q[g[1]]
        elif g[0] == "cx":
            q[g[2]] = q[g[2]] ^ q[g[1]]
        elif g[0] == "ccx":
            q[g[3]] = q[g[3]] ^ (q[g[1]] & q[g[2]])
        elif g[0] == "z":
            sign[q[g[1]]] *= -1
    for i in range(circuit["n"]):
        inp = circuit["first"] <= i < circuit["first"] + bits
        want = ((xs >> (i - circuit["first"])) & 1).astype(bool) if inp else np.zeros(N, dtype=bool)
        if (q[i] != want).any():
            raise QarmError("the phase oracle does not return its work qubits to 0")
    return sign


def grover(circuit, bits, check, seed=None):
    """Grover's search for an unknown number of solutions (Boyer, Brassard, Hoyer, Tapp 1998), simulated exactly on
    the argument register; each measured x is checked. Returns (x or None, oracle calls, rounds)."""
    return grover_signs(oracle_signs(circuit, bits), bits, check, seed)


def grover_signs(sign, bits, check, seed=None, rng=None):
    """BBHT on a sign vector (-1 for the marked x)."""
    import numpy as np
    N = 1 << bits
    rng = rng or random.Random(seed if seed is not None else os.environ.get("QCPU_SEED"))
    m, root, calls, rounds = 1.0, math.sqrt(N), 0, 0
    while calls <= 4.5 * root + 4:
        rounds += 1
        j = int(rng.random() * m)
        amp = np.full(N, 1 / root)
        for _ in range(j):
            amp *= sign                                      # the oracle
            amp = 2 * amp.mean() - amp                       # diffusion
        calls += j
        p = amp * amp
        x = int(np.searchsorted(np.cumsum(p), rng.random() * p.sum()))
        x = min(x, N - 1)
        if check(x):
            return x, calls, rounds
        m = min(m * 6 / 5, root)
    return None, calls, rounds


def compute_oracle(rt, fn, name, bits, kind="QMIN"):
    """qoracle's compute oracle of a cost function: |x>|0> -> |x>|cost(x)>, from its machine code."""
    try:
        import qoracle
        from qoracle import frontend
    except ImportError:
        raise QarmError("QMIN needs qoracle: python3 -m pip install git+https://github.com/quantag/qoracle")
    lo, size = rt.p.code_segment
    page = lo & ~0xFFF
    section = bytes(rt.mu.mem_read(page, ((lo + size + 0xFFF) & ~0xFFF) - page))
    data = [(a & ~0xFFF, bytes(rt.mu.mem_read(a & ~0xFFF, ((a + s + 0xFFF) & ~0xFFF) - (a & ~0xFFF))), seg)
            for a, s, _, seg in rt.p.segments if not (a <= lo < a + s)]
    image = frontend.Image(code=rt.p.function_bytes(fn, rt.mu), entry=fn, section=section, section_addr=page,
                           data=data, arch=getattr(rt.p, "arch", "aarch64"))
    load = frontend.load
    frontend.load = lambda obj, function: image
    try:
        o = qoracle.build(name, name, [qoracle.Arg(name="x", bits=bits)], out_bits=32)
    except qoracle.OracleError as e:
        raise QarmError(f"{kind} {name}: qoracle: {_explain(rt, e)}")
    finally:
        frontend.load = load
    c = o.circuit
    regs = dict((n, (f, k)) for n, f, k in c.regs)
    return {"gates": len([g for g in c.gates if g[0] != "gphase"]), "qubits": c.nqubits, "list": c.gates,
            "n": c.nqubits, "first": regs["x"][0], "out_first": regs["out"][0], "out_bits": regs["out"][1]}


def oracle_values(circuit, bits):
    """cost(x) for every x: the compute oracle's gates on all inputs at once (bit planes); work qubits checked.
    A 32-bit result is signed; with constant top bits dropped (always 0) it is unsigned."""
    import numpy as np
    N = 1 << bits
    xs = np.arange(N, dtype=np.int64)
    q = [np.zeros(N, dtype=bool) for _ in range(circuit["n"])]
    for b in range(bits):
        q[circuit["first"] + b] = ((xs >> b) & 1).astype(bool)
    for g in circuit["list"]:
        if g[0] == "x":
            q[g[1]] = ~q[g[1]]
        elif g[0] == "cx":
            q[g[2]] = q[g[2]] ^ q[g[1]]
        elif g[0] == "ccx":
            q[g[3]] = q[g[3]] ^ (q[g[1]] & q[g[2]])
    lo, w = circuit["out_first"], circuit["out_bits"]
    for i in range(circuit["n"]):
        if lo <= i < lo + w:
            continue
        inp = circuit["first"] <= i < circuit["first"] + bits
        want = ((xs >> (i - circuit["first"])) & 1).astype(bool) if inp else np.zeros(N, dtype=bool)
        if (q[i] != want).any():
            raise QarmError("the compute oracle does not return its work qubits to 0")
    v = np.zeros(N, dtype=np.int64)
    for b in range(w):
        v |= q[lo + b].astype(np.int64) << b
    if w == 32:
        v = np.where(v >= 1 << 31, v - (1 << 32), v)
    return v


def durr_hoyer(values, bits, seed=None):
    """The least value's index (Durr, Hoyer 1996): from a random x, Grover searches (BBHT) for an x with a smaller
    value, until none is found within the budget 22.5 sqrt(N) + 1.4 log2(N)^2. Returns (x, oracle calls, searches)."""
    import numpy as np
    N = 1 << bits
    rng = random.Random(seed if seed is not None else os.environ.get("QCPU_SEED"))
    best = int(rng.random() * N)
    calls, searches, budget = 0, 0, 22.5 * math.sqrt(N) + 1.4 * bits * bits
    while calls < budget:
        sign = np.where(values < values[best], -1, 1)
        searches += 1
        x, c, _ = grover_signs(sign, bits, lambda v: values[v] < values[best], rng=rng)
        calls += c + 1
        if x is None:
            break
        best = x
    return best, calls, searches


def collision(values, bits, seed=None):
    """Brassard-Hoyer-Tapp: ((x, y) or None, oracle calls, tries). K: k = ceil(N^(1/3)) random inputs, queried
    classically; then BBHT for an x outside K with f(x) in f(K). Up to 3 tries with fresh K."""
    import numpy as np
    N = 1 << bits
    rng = random.Random(seed if seed is not None else os.environ.get("QCPU_SEED"))
    k = max(1, math.ceil(N ** (1 / 3)))
    calls = 0
    for tries in range(1, 4):
        K = rng.sample(range(N), min(k, N))
        calls += len(K)
        seen = {}
        for x in K:
            if values[x] in seen:
                return (seen[values[x]], x), calls, tries
            seen[values[x]] = x
        inK = np.zeros(N, dtype=bool)
        inK[K] = True
        marked = np.isin(values, list(seen)) & ~inK
        sign = np.where(marked, -1, 1)
        y, c, _ = grover_signs(sign, bits, lambda v: bool(marked[v]), rng=rng)
        calls += c
        if y is not None:
            return (seen[values[y]], int(y)), calls, tries
    return None, calls, 3


def _walsh_hadamard(v):
    """The normalized Walsh-Hadamard transform: H on every qubit."""
    import numpy as np
    v = np.array(v, dtype=float)
    h = 1
    while h < len(v):
        v = v.reshape(-1, 2, h)
        v = np.stack((v[:, 0] + v[:, 1], v[:, 0] - v[:, 1]), axis=1).reshape(-1)
        h *= 2
    return v / math.sqrt(len(v))


def _sample(p, rng):
    import numpy as np
    return min(int(np.searchsorted(np.cumsum(p), rng.random() * p.sum())), len(p) - 1)


def period_find(values, bits, seed=None, rounds=24):
    """Shor's period finding, simulated: measure f (a random value), quantum Fourier transform of the inputs with
    that value, measure y; the continued fractions of y / 2^bits give candidates, each checked on all inputs.
    Returns (the least period found or None, quantum queries)."""
    import numpy as np
    from fractions import Fraction
    N = 1 << bits
    rng = random.Random(seed if seed is not None else os.environ.get("QCPU_SEED"))

    def periodic(r):
        return 0 < r < N and bool((values[:N - r] == values[r:]).all())
    best = None
    for q in range(1, rounds + 1):
        x0 = int(rng.random() * N)
        amp = (values == values[x0]).astype(float)
        amp /= math.sqrt(amp.sum())
        p = np.abs(np.fft.fft(amp) / math.sqrt(N)) ** 2
        y = _sample(p, rng)
        if y == 0:
            continue
        frac, cands = Fraction(y, N), set()
        a, b = frac.numerator, frac.denominator          # the convergents of y/N, by the continued fraction
        h0, h1, k0, k1 = 0, 1, 1, 0
        while b:
            t = a // b
            a, b = b, a - t * b
            h0, h1, k0, k1 = h1, t * h1 + h0, k1, t * k1 + k0
            cands.update(k1 * m for m in range(1, 5))
        for r in sorted(cands):
            if periodic(r):
                best = min(d for d in range(1, r + 1) if r % d == 0 and periodic(d))   # its least periodic divisor
                return best, q
    return None, rounds


def simon(values, bits, seed=None):
    """Simon's algorithm, simulated: Walsh-Hadamard samples y with y.s = 0 until n - 1 independent ones, then s
    from the null space over GF(2), checked on all inputs. Returns (s or None for a one-to-one f, queries)."""
    import numpy as np
    N = 1 << bits
    rng = random.Random(seed if seed is not None else os.environ.get("QCPU_SEED"))
    basis = {}                                           # leading bit -> row
    queries = 0
    while len(basis) < bits - 1 and queries < 4 * bits + 8:
        queries += 1
        x0 = int(rng.random() * N)
        amp = (values == values[x0]).astype(float)
        amp /= math.sqrt(amp.sum())
        y = _sample(_walsh_hadamard(amp) ** 2, rng)
        for lead in sorted(basis, reverse=True):         # reduce y by the rows so far
            if y >> lead & 1:
                y ^= basis[lead]
        if y:
            basis[y.bit_length() - 1] = y
    for s in range(1, N):                                # the null space of at most n - 1 rows: a nonzero s
        if all(bin(row & s).count("1") % 2 == 0 for row in basis.values()):
            if bool((values == values[np.arange(N) ^ s]).all()):
                return s, queries
    return None, queries


def bernstein_vazirani(sign, bits):
    """Bernstein-Vazirani: H, the phase oracle once, H; for f(x) = s.x the result is s with certainty."""
    import numpy as np
    N = 1 << bits
    p = _walsh_hadamard(sign / math.sqrt(N)) ** 2
    return int(np.argmax(p)), 1


def quantum_count(M, bits, seed=None):
    """Quantum counting (Brassard, Hoyer, Tapp 1998): amplitude estimation of M / N. Returns (estimate, error bound,
    t, calls)."""
    N = 1 << bits
    a, bound, t, calls = amplitude_estimate(M / N, bits, seed)
    return int(round(N * a)), N * bound, t, calls


MEDIAN_OF = 5


def amplitude_estimate(a, bits, seed=None):
    """Amplitude estimation, simulated: phase estimation with t precision qubits of the operator whose eigenphases
    are +-2 theta, sin^2 theta = a; the median of MEDIAN_OF runs. Returns (estimate of a, error bound, t, queries).
    One run's bound holds with probability >= 8/pi^2; the median's with about 0.98."""
    t = (bits + 1) // 2 + 3
    T = 1 << t
    phi = math.asin(math.sqrt(min(max(a, 0.0), 1.0))) / math.pi
    def fejer(d):
        d = (d + T / 2) % T - T / 2
        if abs(d) < 1e-12:
            return 1.0
        return (math.sin(math.pi * d) / (T * math.sin(math.pi * d / T))) ** 2
    p = [0.5 * fejer(y - T * phi) + 0.5 * fejer(y + T * phi) for y in range(T)]
    rng = random.Random(seed if seed is not None else os.environ.get("QCPU_SEED"))
    runs = []
    for _ in range(MEDIAN_OF):                       # the median of 5: the bound then holds with probability ~0.98
        u, acc, y = rng.random() * sum(p), 0.0, 0
        for y in range(T):
            acc += p[y]
            if u < acc:
                break
        runs.append(math.sin(math.pi * y / T) ** 2)
    est = sorted(runs)[MEDIAN_OF // 2]
    bound = 2 * math.pi * math.sqrt(est * (1 - est)) / T + math.pi ** 2 / T ** 2
    return est, bound, t, MEDIAN_OF * (T - 1)


def circuit_samples(n, gates, shots, seed=None):
    """The state vector of a gate list on |0..0>, then shots outcomes (bit q = qubit q)."""
    import numpy as np
    N = 1 << n
    psi = np.zeros(N, dtype=complex)
    psi[0] = 1
    idx = np.arange(N)
    r2 = 1 / math.sqrt(2)
    one = {"x": [[0, 1], [1, 0]], "y": [[0, -1j], [1j, 0]], "z": [[1, 0], [0, -1]], "h": [[r2, r2], [r2, -r2]],
           "s": [[1, 0], [0, 1j]], "sdg": [[1, 0], [0, -1j]], "t": [[1, 0], [0, complex(r2, r2)]],
           "tdg": [[1, 0], [0, complex(r2, -r2)]], "sx": [[0.5 + 0.5j, 0.5 - 0.5j], [0.5 - 0.5j, 0.5 + 0.5j]]}
    for name, qs, ang in gates:
        if name in ("rx", "ry", "rz"):
            c, s_ = math.cos(ang / 2), math.sin(ang / 2)
            m = {"rx": [[c, -1j * s_], [-1j * s_, c]], "ry": [[c, -s_], [s_, c]],
                 "rz": [[complex(c, -s_), 0], [0, complex(c, s_)]]}[name]
        else:
            m = one.get(name)
        if m is not None:
            q = 1 << qs[0]
            lo = idx[(idx & q) == 0]
            a, b = psi[lo].copy(), psi[lo | q].copy()
            psi[lo], psi[lo | q] = m[0][0] * a + m[0][1] * b, m[1][0] * a + m[1][1] * b
        elif name == "cx" or name == "ccx":
            ctl = (1 << qs[0]) | ((1 << qs[1]) if name == "ccx" else 0)
            t = 1 << qs[-1]
            sel = idx[((idx & ctl) == ctl) & ((idx & t) == 0)]
            psi[sel], psi[sel | t] = psi[sel | t].copy(), psi[sel].copy()
        elif name == "cz":
            both = (1 << qs[0]) | (1 << qs[1])
            psi[(idx & both) == both] *= -1
        elif name == "swap":
            a, b = 1 << qs[0], 1 << qs[1]
            sel = idx[((idx & a) != 0) & ((idx & b) == 0)]
            psi[sel], psi[sel ^ a ^ b] = psi[sel ^ a ^ b].copy(), psi[sel].copy()
    p = np.abs(psi) ** 2
    rng = np.random.default_rng(int(os.environ["QCPU_SEED"]) if os.environ.get("QCPU_SEED") else None)
    return [int(v) for v in rng.choice(N, size=shots, p=p / p.sum())]


def circuit_on_cudaq(n, gates, shots, cfg):
    """A gate list as a CUDA-Q kernel (make_kernel), sampled on cfg's CUDA-Q target. Returns (outcomes, where)."""
    import cudaq
    target = cfg.get("cudaq", {}).get("target")
    if target and cudaq.get_target().name != target:
        cudaq.set_target(target)
    k = cudaq.make_kernel()
    q = k.qalloc(n)
    for name, qs, ang in gates:
        if name in ("rx", "ry", "rz"):
            getattr(k, name)(ang, q[qs[0]])
        elif name == "ccx":
            k.cx([q[qs[0]], q[qs[1]]], q[qs[2]])
        elif name in ("cx", "cz", "swap"):
            getattr(k, name)(q[qs[0]], q[qs[1]])
        elif name == "sx":                                # sqrt(X) = e^{i pi/4} RX(pi/2)
            k.rx(math.pi / 2, q[qs[0]])
        else:
            getattr(k, name)(q[qs[0]])
    k.mz(q)
    counts = cudaq.sample(k, shots_count=int(shots))
    outcomes = [sum(int(b) << i for i, b in enumerate(key)) for key, c in counts.items() for _ in range(c)]
    random.Random(0).shuffle(outcomes)
    return outcomes[:shots], f"CUDA-Q ({cudaq.get_target().name})"


def circuit_on_backend(n, gates, shots, cfg, note=lambda t: None):
    """A gate list on a backend of qcpu_run.py (Sampler; one job). Returns (outcomes, where)."""
    if cfg["backend"] == "cudaq":
        return circuit_on_cudaq(n, gates, shots, cfg)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import qcpu_run
    from qiskit import QuantumCircuit
    qc = QuantumCircuit(n, n)
    for name, qs, ang in gates:
        if name in ("rx", "ry", "rz"):
            getattr(qc, name)(ang, *qs)
        else:
            getattr(qc, name)(*qs)
    qc.measure(range(n), range(n))
    try:
        dev, where = qcpu_run.backend_for(cfg, n)
    except qcpu_run.Error as e:
        raise QarmError(f"QCIRC: {e}")
    counts, job_id, twoq, depth, secs = qcpu_run.sample(qc, dev, cfg, shots)
    outcomes = [int(k, 2) for k, c in counts.items() for _ in range(c)]
    random.Random(0).shuffle(outcomes)
    return outcomes[:shots], f"{where.split(';')[0]} (job {job_id}, {two_q_note(twoq)}, {secs:.1f} s)"


def two_q_note(n):
    return f"{n} two-qubit gates after transpiling"


def qiskit_oracle(circuit):
    """The phase oracle as a Qiskit circuit (its global phase is dropped: it does not change what is measured)."""
    from qiskit import QuantumCircuit
    qc = QuantumCircuit(circuit["n"], name="oracle")
    for g in circuit["list"]:
        if g[0] == "x":
            qc.x(g[1])
        elif g[0] == "cx":
            qc.cx(g[1], g[2])
        elif g[0] == "ccx":
            qc.ccx(g[1], g[2], g[3])
        elif g[0] == "z":
            qc.z(g[1])
    return qc


def grover_qpu(circuit, bits, cfg, check, note=lambda t: None):
    """Grover's search on a (possibly asynchronous) backend of qcpu_run: every iteration count 0..floor(pi/4 sqrt N)
    in one job, since the number of solutions is unknown and a QPU queues each job. The measured answers are checked,
    most frequent first. Returns (x or None, report)."""
    import time
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        import qcpu_run
        from qiskit import transpile
        from qiskit_ibm_runtime import SamplerV2
    except ImportError as e:
        raise QarmError(f"--qpu needs qiskit, qiskit-aer and qiskit-ibm-runtime ({e})")
    xs = list(range(circuit["first"], circuit["first"] + bits))
    oracle = qiskit_oracle(circuit)
    top = max(1, int(math.pi / 4 * math.sqrt(1 << bits)))
    qcs = [qcpu_run.build("grover", oracle, xs, j) for j in range(top + 1)]
    try:
        dev, where = qcpu_run.backend_for(cfg, circuit["n"])      # refuses a circuit larger than the device
    except qcpu_run.Error as e:
        raise QarmError(f"--qpu: {e}")
    level = int(cfg["ibm"].get("optimization_level", 1))
    ts = transpile(qcs, dev, optimization_level=level, seed_transpiler=1)
    twoq = max(sum(v for k, v in t.count_ops().items() if k in ("cx", "cz", "ecr", "rzz")) for t in ts)
    shots = int(cfg["shots"])
    t0 = time.time()
    job = SamplerV2(mode=dev).run(ts, shots=shots)             # submitted: on a QPU it now waits in the queue
    job_id = job.job_id() if hasattr(job, "job_id") else None
    if cfg["backend"] == "ibm":
        note(f"QSEARCH: job {job_id} sent to {dev.name}, waiting for it (a QPU job queues; see the IBM Quantum dashboard)")
    counts = {}
    for t, r in zip(ts, job.result()):                          # blocks until the job is done
        for k, v in r.data[t.cregs[0].name].get_counts().items():
            counts[int(k, 2)] = counts.get(int(k, 2), 0) + v
    info = {"backend": where.split(";")[0], "job_id": job_id, "circuits": len(ts), "shots": shots,
            "two_qubit_gates": twoq, "seconds": round(time.time() - t0, 1), "distinct": len(counts),
            "total": sum(counts.values())}
    for rank, (x, n) in enumerate(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])), 1):
        if check(x):
            info.update(hits=n, rank=rank)
            return x, info
    return None, info


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("program")
    ap.add_argument("args", nargs="*")
    ap.add_argument("-q", "--quiet", action="store_true", help="no report of quantum instructions")
    ap.add_argument("--qpu", action="store_true", help="QSEARCH on the backend of config.json instead of the exact "
                    "simulator")
    ap.add_argument("--config", help="the configuration (default: config.json in this repository); implies --qpu")
    ap.add_argument("--backend", help="simulator, aer, preview, ibm or cudaq, over config.json's; implies --qpu")
    ap.add_argument("--cudaq-target", help="with --backend cudaq: the CUDA-Q target (qpp-cpu, nvidia, ...)")
    a = ap.parse_args(argv)
    try:
        qpu = None
        if a.qpu or a.config or a.backend:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import qcpu_run
            user = json.load(open(a.config)) if a.config and os.path.exists(a.config) else {}
            if (a.backend or user.get("backend")) == "cudaq":       # CUDA-Q: VQE, QCIRC, QRAND; oracles stay exact
                qpu = {"backend": "cudaq", "shots": int(user.get("shots", 0)),
                       "cudaq": {"target": a.cudaq_target or user.get("options", {}).get("target")}}
            else:
                try:
                    qpu = qcpu_run.load_config(a.config, a.backend)
                except qcpu_run.Error as e:
                    raise QarmError(f"--qpu: {e}")
        prog = Program(a.program)
        if getattr(prog, "arch", "aarch64") != "aarch64":
            raise QarmError(f"{a.program}: an x86-64 program: run it under qemu-x86_64 with the plugin of runtime/qemu "
                            "(qarm_exec.py emulates AArch64)")
        rt = Runtime(prog, [a.program] + a.args, a.quiet, qpu)
        code = rt.run()
    except QarmError as e:
        sys.stdout.flush()
        print(f"qarm_exec: error: {e}", file=sys.stderr)
        return 70
    sys.stdout.flush()
    return code


if __name__ == "__main__":
    sys.exit(main())
