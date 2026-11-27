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
import math
import os
import random
import struct
import sys

STACK_TOP, STACK_SIZE = 0x7ff000000, 0x100000
HEAP, HEAP_SIZE = 0x6f0000000, 0x400000
STUBS = 0x6e0000000             # one 4-byte slot per imported function, handled by this runtime
HALT = STUBS - 0x1000           # main returns here
PACKET_BRANCH, PACKET_MAGIC = 0x14000008, 0x4D524151
OP_QAVAIL, OP_QSEARCH = 0x10, 0x11
OP_QVLOAD, OP_QEXPECT, OP_QEXPECTN, OP_QSESSION, OP_QVFREE = 0x20, 0x21, 0x22, 0x24, 0x26
OPS = (OP_QAVAIL, OP_QSEARCH, OP_QVLOAD, OP_QEXPECT, OP_QEXPECTN, OP_QSESSION, OP_QVFREE)


class QarmError(Exception):
    pass


# ------------------------------------------------------------------ the program
def _c_name(name):
    """A Mach-O symbol name is the C name with one underscore in front: _printf, ___stack_chk_guard."""
    return name[1:] if name.startswith("_") else name


class Program:
    """The executable's segments at their own addresses, its imports, its functions and their names."""

    def __init__(self, path):
        import lief
        b = lief.parse(path)
        if b is None:
            raise QarmError(f"{path}: not an executable lief can read")
        self.path, self.segments, self.imports, self.rebases = path, [], [], []
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
        self.code_segment = (text.virtual_address, text.virtual_size)  # __TEXT: also constant data

    def _elf(self, b, lief):
        if b.header.machine_type != lief.ELF.ARCH.AARCH64:
            raise QarmError(f"{self.path}: not an AArch64 program")
        self.kind = "linux"
        self.base = 0x10000000 if b.header.file_type == lief.ELF.Header.FILE_TYPE.DYN else 0
        for s in b.segments:
            if s.type == lief.ELF.Segment.TYPE.LOAD:
                self.segments.append((self.base + s.virtual_address, s.virtual_size, bytes(s.content), "LOAD"))
        mains = [s for s in b.symbols if s.name == "main" and s.value]
        if not mains:
            raise QarmError(f"{self.path}: no symbol main (do not strip the program)")
        self.main = self.base + mains[0].value
        R = lief.ELF.Relocation.TYPE
        for r in b.relocations:
            addr = self.base + r.address
            if r.type == R.AARCH64_RELATIVE:
                self.rebases.append((addr, self.base + r.addend))
            elif r.type in (R.AARCH64_JUMP_SLOT, R.AARCH64_GLOB_DAT, R.AARCH64_ABS64) and r.has_symbol:
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
            later = [a for a in self.starts if a > addr]
            end = later[0] if later else self.text[0] + self.text[1]
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
        lo, size = self.p.text
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
    def x(self, n):
        return self.mu.reg_read(getattr(self.A, f"UC_ARM64_REG_X{n}"))

    def set_x(self, n, v):
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
        if op == OP_QAVAIL:
            self.set_x(0, 1)
            self.set_x(1, 3)                       # W1: features, QSEARCH | VQE
        elif op == OP_QSEARCH:
            self.qsearch(self.x(1), self.x(2) & 0xFFFFFFFF)
        elif op in (OP_QVLOAD, OP_QEXPECT, OP_QEXPECTN, OP_QSESSION, OP_QVFREE):
            self.vqe_op(op)
        mu.reg_write(self.A.UC_ARM64_REG_PC, address + 32)

    def call(self, fn, arg):
        """Call a function of the program: f(arg) -> W0 (used to check answers)."""
        A = self.A
        saved = {r: self.mu.reg_read(getattr(A, f"UC_ARM64_REG_{r}")) for r in
                 ["SP", "LR", "PC", "NZCV"] + [f"X{i}" for i in range(31)]}
        self.mu.reg_write(A.UC_ARM64_REG_X0, arg)
        self.mu.reg_write(A.UC_ARM64_REG_LR, HALT + 0x800)
        self.mu.reg_write(A.UC_ARM64_REG_SP, saved["SP"] - 0x10000)
        self.mu.emu_start(fn, HALT + 0x800, count=1_000_000)
        v = self.x(0) & 0xFFFFFFFF
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
        if self.qpu is not None:
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
                if self.qpu is not None:
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
                k = 1 if op == OP_QEXPECT else self.x(2) & 0xFFFFFFFF
                if not 1 <= k <= 4096:
                    raise qarm_vqe.VqeError(f"QEXPECTN: k = {k}; 1 to 4096")
                thetas = [self.doubles(self.x(1) + 8 * problem.params * i, problem.params) for i in range(k)]
                shots = (self.x(2) & 0xFFFFFFFF) if op == OP_QEXPECT else 0
                if measured is None:
                    es, errs, how = problem.exact(thetas), [0.0] * k, "exact"
                else:
                    es, errs, job, sec = measured.energies(thetas, shots)
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


def _memcpy(rt):
    d, s, n = rt.x(0), rt.x(1), rt.x(2)
    rt.mu.mem_write(d, bytes(rt.mu.mem_read(s, n)))
    rt.ret(d)


def _stack_chk_fail(rt):
    raise QarmError("stack smashing detected")


LIBC = {"printf": _printf, "__printf_chk": _printf_chk, "puts": _puts, "putchar": _putchar, "exit": _exit, "_exit": _exit, "strlen": _strlen,
        "memset": _memset, "memcpy": _memcpy, "memmove": _memcpy, "__stack_chk_fail": _stack_chk_fail,
        "__memset_chk": _memset, "__memcpy_chk": _memcpy, "putc": _putc, "fputc": _putc, "_IO_putc": _putc,
        "fputs": _fputs, "fwrite": _fwrite, "fflush": _fflush}
STDOUT_FILE, STDERR_FILE = 0x5100, 0x5200      # what the program sees as FILE *stdout, *stderr
DATA_IMPORTS = {"__stack_chk_guard": 0x2F8A5C3E91D04B76, "stdout": STDOUT_FILE, "stderr": STDERR_FILE,
                "__stdoutp": STDOUT_FILE, "__stderrp": STDERR_FILE}


# ------------------------------------------------------------------ oracle and Grover's search
def phase_oracle(rt, fn, name, bits):
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
                           data=data)
    load = frontend.load
    frontend.load = lambda obj, function: image        # qoracle.build reads this image instead of an object file
    try:
        spec = [qoracle.Arg(name="x", bits=bits)]
        full = qoracle.build(name, name, spec, out_bits=32)
        if full.out_bits != 1:
            if not full.report["verification"]["exhaustive"]:
                raise QarmError(f"QSEARCH {name}: its result is not proven to be 0 or 1")
            from qoracle import verify
            cpu = verify.CPU(image)
            for v in range(1 << bits):
                if cpu.call([v]) & 0xFFFFFFFF not in (0, 1):
                    raise QarmError(f"QSEARCH {name}: {name}({v}) is neither 0 nor 1")
        ph = qoracle.build(name, name, spec, out_bits=1, mode="phase")
    except qoracle.OracleError as e:
        raise QarmError(f"QSEARCH {name}: qoracle: {e}")
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
    import numpy as np
    N = 1 << bits
    sign = oracle_signs(circuit, bits)
    rng = random.Random(seed if seed is not None else os.environ.get("QCPU_SEED"))
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
    ap.add_argument("--backend", help="simulator, aer, preview or ibm, over config.json's; implies --qpu")
    a = ap.parse_args(argv)
    try:
        qpu = None
        if a.qpu or a.config or a.backend:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import qcpu_run
            try:
                qpu = qcpu_run.load_config(a.config, a.backend)
            except qcpu_run.Error as e:
                raise QarmError(f"--qpu: {e}")
        rt = Runtime(Program(a.program), [a.program] + a.args, a.quiet, qpu)
        code = rt.run()
    except QarmError as e:
        sys.stdout.flush()
        print(f"qarm_exec: error: {e}", file=sys.stderr)
        return 70
    sys.stdout.flush()
    return code


if __name__ == "__main__":
    sys.exit(main())
