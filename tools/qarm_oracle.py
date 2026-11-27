#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Quantag IT Solutions GmbH
"""The phase oracle of a predicate in an arm64 Linux program, for QSEARCH under QEMU (runtime/qemu, docs/qarm_v2.md
section 8). The QEMU plugin runs it once per predicate; the program itself needs no Python.

    python3 tools/qarm_oracle.py --elf PROGRAM --function 0x5500000a40 --code 1f0c0071... --bits 2

--function: the predicate's address in the running program; --code: its first bytes there (hex). The load base of a
PIE program is the one at which a function of the file has exactly these bytes (--base gives it instead).
qoracle builds the phase oracle from the function's machine code at those addresses (the same as qarm_exec.py's
QSEARCH). --kind phase (the default; QSEARCH, QCOUNT): f returns 0 or 1, and the oracle flips the phase where f(x) = 1.
--kind compute (QMIN): |x>|0> -> |x>|f(x)>. Output, on stdout:
    qarm-oracle 1 NAME QUBITS FIRST BITS GATES                         (phase)
    qarm-oracle 2 NAME QUBITS FIRST BITS OUT_FIRST OUT_BITS GATES     (compute: f(x) on OUT_FIRST.., signed if 32 bits)
    then one gate per line: x q | cx c t | ccx c1 c2 t | z q
The qubits FIRST .. FIRST+BITS-1 hold x; every other qubit (but the output) starts and ends at 0. Errors: stderr,
exit status 1."""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class FileMemory:
    """The program's segments, from the file, at their load addresses: mem_read as Unicorn's."""

    def __init__(self, segments):
        self.segments = segments

    def mem_read(self, addr, size):
        out = bytearray(size)
        for a, s, content, _ in self.segments:
            lo, hi = max(addr, a), min(addr + size, a + s)
            if lo < hi:
                chunk = content[lo - a:hi - a]
                out[lo - addr:lo - addr + len(chunk)] = chunk
        return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--elf", required=True)
    ap.add_argument("--base", type=lambda v: int(v, 0))
    ap.add_argument("--code", help="the function's first bytes in the running program, hex")
    ap.add_argument("--function", required=True, type=lambda v: int(v, 0))
    ap.add_argument("--bits", required=True, type=int)
    ap.add_argument("--kind", choices=("phase", "compute"), default="phase")
    a = ap.parse_args(argv)
    sys.path.insert(0, ROOT)
    import qarm_exec
    try:
        base = a.base
        if base is None:
            if not a.code:
                raise qarm_exec.QarmError("give --base or --code")
            code = bytes.fromhex(a.code)
            probe = qarm_exec.Program(a.elf, base=0)
            mem = FileMemory(probe.segments)
            bases = {a.function - f for f in probe.functions
                     if (a.function - f) % 4096 == 0 and bytes(mem.mem_read(f, len(code))) == code}
            if len(bases) != 1:
                raise qarm_exec.QarmError(f"{'no' if not bases else 'more than one'} function of {a.elf} has the "
                                          f"code at {a.function:#x}")
            base = bases.pop()
        prog = qarm_exec.Program(a.elf, base=base)
        if prog.kind != "linux":
            raise qarm_exec.QarmError("an arm64 Linux (ELF) program is needed")
        name = prog.functions.get(a.function)
        if name is None:
            raise qarm_exec.QarmError(f"no function starts at {a.function:#x} (is the program stripped, or the base "
                                      "wrong?)")
        if not 1 <= a.bits <= 20:
            raise qarm_exec.QarmError(f"bits = {a.bits}: 1 to 20")

        class Rt:                                        # what phase_oracle reads: the program and its memory
            p, mu = prog, FileMemory(prog.segments)
        build = qarm_exec.compute_oracle if a.kind == "compute" else qarm_exec.phase_oracle
        c = build(Rt, a.function, name, a.bits)
    except qarm_exec.QarmError as e:
        print(f"qarm_oracle: {e}", file=sys.stderr)
        return 1
    gates = [g for g in c["list"] if g[0] in ("x", "cx", "ccx", "z")]
    other = {g[0] for g in c["list"]} - {"x", "cx", "ccx", "z", "gphase"}
    if other:
        print(f"qarm_oracle: unexpected gates {sorted(other)}", file=sys.stderr)
        return 1
    if a.kind == "compute":
        out = [f"qarm-oracle 2 {name} {c['n']} {c['first']} {a.bits} {c['out_first']} {c['out_bits']} {len(gates)}"]
    else:
        out = [f"qarm-oracle 1 {name} {c['n']} {c['first']} {a.bits} {len(gates)}"]
    out += [" ".join(str(v) for v in g) for g in gates]
    sys.stdout.write("\n".join(out) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
