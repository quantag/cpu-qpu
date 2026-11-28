# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Quantag IT Solutions GmbH
"""The Q-ARM ISA (1.1) as published: the specification, its header and the implementations say the same thing.

  1. include/qarm_isa.h compiles alone as C99 (-pedantic, -Wall -Wextra), and its layouts have the sizes and the
     field offsets of docs/qarm_isa.md, section 5.
  2. Every opcode and feature bit in the specification's tables equals the header's, and the header's equal those
     of qarm_exec.py and of the QEMU plugin (runtime/qemu/qarm_qemu.c).
  3. (QEMU 10.1+ with plugins, QARM_QEMU as for test_qarm_qemu) runtime/minimal, the guide's 200-line runtime:
     the conformance program passes its Core and Circuit profiles and skips the others, on AArch64 (and on x86-64
     when qemu-x86_64 is there); ghz.c runs on it.
Run: python3 -m tests.test_qarm_isa"""
import os, re, shutil, subprocess, sys, tempfile
from tests.test_qarm_exec import ROOT, sh, toolchain

INC = os.path.join(ROOT, "include")
SPEC = os.path.join(ROOT, "docs", "qarm_isa.md")

LAYOUT = r"""
#include <stddef.h>
#include <stdio.h>
#include "qarm_isa.h"
#define F(t, f) printf(#t "." #f " %lu\n", (unsigned long)offsetof(t, f))
#define S(t) printf(#t " %lu\n", (unsigned long)sizeof(t))
int main(void)
{
    S(qarm_pauli); S(qarm_hamiltonian); S(qarm_op); S(qarm_ansatz); S(qarm_gate); S(qarm_circuit);
    S(qarm_capabilities); S(qarm_request); S(qarm_evolution);
    F(qarm_pauli, c); F(qarm_hamiltonian, constant); F(qarm_hamiltonian, t); F(qarm_op, reserved); F(qarm_op, scale);
    F(qarm_op, x); F(qarm_op, z); F(qarm_ansatz, init); F(qarm_ansatz, op); F(qarm_gate, reserved); F(qarm_gate, angle);
    F(qarm_circuit, g); F(qarm_capabilities, backend); F(qarm_request, x); F(qarm_request, d); F(qarm_request, ticket);
    F(qarm_evolution, o); F(qarm_evolution, t); F(qarm_evolution, k); F(qarm_evolution, method); F(qarm_evolution, order);
    F(qarm_evolution, shots); F(qarm_evolution, init); F(qarm_evolution, eps); F(qarm_evolution, steps);
    F(qarm_evolution, reserved);
    printf("word2 %#x\n", QARM_ISA_WORD2(QARM_OPC_QCIRC));
    return 0;
}
"""
WANT = {"qarm_pauli": 24, "qarm_hamiltonian": 24, "qarm_op": 32, "qarm_ansatz": 32, "qarm_gate": 16, "qarm_circuit": 16,
        "qarm_capabilities": 64, "qarm_request": 80, "qarm_pauli.c": 16, "qarm_hamiltonian.constant": 8,
        "qarm_hamiltonian.t": 16, "qarm_op.reserved": 4, "qarm_op.scale": 8, "qarm_op.x": 16, "qarm_op.z": 24,
        "qarm_ansatz.init": 16, "qarm_ansatz.op": 24, "qarm_gate.reserved": 4, "qarm_gate.angle": 8,
        "qarm_circuit.g": 8, "qarm_capabilities.backend": 16, "qarm_request.x": 8, "qarm_request.d": 56,
        "qarm_request.ticket": 72, "qarm_evolution": 64, "qarm_evolution.o": 8, "qarm_evolution.t": 16,
        "qarm_evolution.k": 24, "qarm_evolution.method": 28, "qarm_evolution.order": 32, "qarm_evolution.shots": 36,
        "qarm_evolution.init": 40, "qarm_evolution.eps": 48, "qarm_evolution.steps": 56, "qarm_evolution.reserved": 60,
        "word2": 0x2300000}


def header_constants():
    text = open(os.path.join(INC, "qarm_isa.h"), encoding="utf-8").read()
    ops = {m[1]: int(m[2], 0) for m in re.finditer(r"QARM_OPC_(\w+) = (0x[0-9A-Fa-f]+)", text)}
    feats = {m[1]: int(m[2]) for m in re.finditer(r"QARM_F_(\w+) = (\d+)", text)}
    return ops, feats


def spec_constants():
    text = open(SPEC, encoding="utf-8").read()
    ops = {}
    for m in re.finditer(r"^\| (0x[0-9A-Fa-f]{2}) \| `(Q[A-Z]+)` \|", text, re.M):
        ops[m[2]] = int(m[1], 16)
    feats = {m[1]: int(m[2]) for m in re.finditer(r"^\| `QARM_F_(\w+)` \| (\d+) \|", text, re.M)}
    return ops, feats


def main():
    cc = next((c for c in ("gcc", "clang", "cc") if shutil.which(c)), None)
    if cc is None:
        print("skipped: no C compiler")
        return
    with tempfile.TemporaryDirectory() as d:
        src, exe = os.path.join(d, "layout.c"), os.path.join(d, "layout" + (".exe" if os.name == "nt" else ""))
        open(src, "w").write(LAYOUT)
        sh([cc, "-std=c99", "-pedantic", "-Wall", "-Wextra", "-Werror", "-I", INC, src, "-o", exe])
        got = {k: int(v, 0) for k, v in (line.split() for line in sh([exe]).stdout.splitlines())}
        assert got == WANT, {k: (got.get(k), v) for k, v in WANT.items() if got.get(k) != v}
        print(f"1) qarm_isa.h alone, C99 -pedantic -Werror: {len(WANT) - 1} sizes and offsets as in section 5")

        hops, hfeats = header_constants()
        sops, sfeats = spec_constants()
        assert sfeats == hfeats, (sfeats, hfeats)
        for name, v in sops.items():
            assert hops.get(name) == v, (name, v, hops.get(name))
        missing = set(hops) - set(sops)
        assert not missing, missing
        sys.path.insert(0, ROOT)
        import qarm_exec
        for name, v in hops.items():
            assert getattr(qarm_exec, "OP_" + name) == v, name
        assert qarm_exec.FEATURES == sum(hfeats.values())
        plugin = open(os.path.join(ROOT, "runtime", "qemu", "qarm_qemu.c"), encoding="utf-8").read()
        for m in re.finditer(r"OP_(Q[A-Z]+) = (0x[0-9A-Fa-f]+)", plugin):
            assert hops[m[1]] == int(m[2], 16), m[0]
        print(f"2) {len(sops)} opcodes and {len(sfeats)} feature bits: the specification, qarm_isa.h, qarm_exec.py and "
              "the QEMU plugin agree")

        from tests.test_qarm_qemu import find_qemu
        q, inc, why = find_qemu()
        tc = toolchain()
        if why or not tc or sys.platform != "linux":
            print("3) skipped: " + (why or "no arm64 C compiler or not Linux"))
            print("PASS")
            return
        cc64, _ = tc
        b = os.path.join(d, "minimal")
        sh(["cmake", "-S", os.path.join(ROOT, "runtime", "minimal"), "-B", b, f"-DQEMU_PLUGIN_INCLUDE={inc}"])
        sh(["cmake", "--build", b])
        plug = os.path.join(b, "libqarm-minimal.so")
        sysroot = [] if os.uname().machine in ("aarch64", "arm64") else ["-L", "/usr/aarch64-linux-gnu"]
        conf = os.path.join(d, "conformance")
        sh(cc64 + ["-O1", "-iquote", INC, os.path.join(ROOT, "tests", "conformance", "qarm_conformance.c"), "-o", conf,
                   "-lm"])
        want = ("profiles: Core passed; Oracle not claimed; Variational not claimed; Circuit passed; Async not claimed; "
                "Dynamics not claimed")
        p = sh([q] + sysroot + ["-plugin", plug, conf], check=False)
        assert p.returncode == 0 and want in p.stdout and "5 passed, 0 failed, 11 skipped" in p.stdout, p.stdout
        ghz = os.path.join(d, "ghz")
        sh(cc64 + ["-O1", "-iquote", INC, os.path.join(ROOT, "examples", "qarm", "ghz.c"), "-o", ghz, "-lm"])
        p = sh([q] + sysroot + ["-plugin", plug, ghz])
        assert "other 0 of 2000 shots (on the coprocessor)" in p.stdout and "qarm-minimal" in p.stdout, p.stdout
        how = ""
        q86 = os.path.join(os.path.dirname(q), "qemu-x86_64")
        if os.path.exists(q86) and shutil.which("gcc") and os.uname().machine == "x86_64":
            c86 = os.path.join(d, "conformance86")
            sh(["gcc", "-O1", "-iquote", INC, os.path.join(ROOT, "tests", "conformance", "qarm_conformance.c"), "-o", c86,
                "-lm"])
            p = sh([q86, "-plugin", plug, c86], check=False)
            assert p.returncode == 0 and want in p.stdout, p.stdout
            how = "; on x86-64 too"
        print(f"3) runtime/minimal ({sum(1 for _ in open(os.path.join(ROOT, 'runtime', 'minimal', 'qarm_minimal.c'), encoding='utf-8'))} "
              f"lines): conformance Core and Circuit passed, the rest not claimed{how}; ghz.c on it")
    print("PASS")


if __name__ == "__main__":
    main()
