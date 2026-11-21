# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Quantag IT Solutions GmbH
"""The fast simulator (one generated cycle replayed T times, QuantumARM.run_fast) must give exactly the
same result as simulating the fully materialised circuit (all T cycles, every qubit), and, for small
machines, as Qiskit Aer running the exported circuit.

Compared: every register, NZCV, every data-memory cell, the PC after every cycle, running, trap.
Run: python3 -m tests.test_simulators [N] [seed]"""
import os, random, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from qiskit import ClassicalRegister
from qiskit_aer import AerSimulator
from qcpu.bitsim import simulate, value
from qcpu.machine import Config, QuantumARM
from qcpu.reference import layout
from tests.common import assemble
from tests.test_fuzz import gen


def full_vs_fast(N, seed):
    cfg = dict(W=8, regs=tuple(range(8)) + (29, 30), sp=False, SLOTS=32, STEPS=20, MEM=8)
    full = QuantumARM(Config(**cfg))                       # every cycle materialised
    fast = QuantumARM(Config(**cfg), defer=True)
    r = random.Random(seed)
    done = same = 0
    while done < N:
        words = assemble(gen(r, r.randrange(3, 10)))
        if words is None or len(words) >= 32:
            continue
        words = layout(words, 32)
        init = {i: r.randrange(-40, 40) for i in range(7)}
        init[30] = 4 * 31
        mem = [r.randrange(-60, 60) for _ in range(8)]
        st = simulate(full.g, full.n, full.load_bits(words, init, mem))
        a = ([value(st, full.X[k]) for k in sorted(full.X)], [st[q] for q in full.nzcv], [value(st, c) for c in full.ram],
             [value(st, p) for p in full.pc_hist], st[full.run[-1]], any(st[t] for t in full.trap))
        o = fast.run_fast(fast.load_bits(words, init, mem))
        s2 = o["state"]
        b = ([value(s2, fast.X[k]) for k in sorted(fast.X)], [s2[q] for q in fast.nzcv], [value(s2, c) for c in fast.ram],
             o["trace"], o["run"], o["trap"])
        done += 1
        same += a == b
    return done, same, full.n, len(full.g), fast.n_total, sum(fast.total.values())


def aer_vs_fast():
    progs = [["movz w0, #5", "add w0, w0, #3", "ret"],
             ["mov w1, #3", "2:", "add w2, w2, #2", "subs w1, w1, #1", "b.ne 2b", "ret"],
             ["cmp w0, #4", "csel w1, w0, w2, gt", "eor w2, w1, w0", "ret"]]
    classes = ("movw", "addsub_imm", "addsub_reg", "logic_reg", "csel", "bcond", "bra", "hlt")
    res = []
    for src in progs:
        words = layout(assemble(src), 8)
        cfg = dict(W=8, regs=(0, 1, 2, 30), SLOTS=8, STEPS=12, MEM=0, classes=classes)
        init = {0: 7, 2: -3, 30: 28}
        fast = QuantumARM(Config(**cfg), defer=True)
        o = fast.run_fast(fast.load_bits(words, init))
        want = [value(o["state"], fast.X[k]) for k in (0, 1, 2)]
        m = QuantumARM(Config(**cfg))
        qc = m.to_qiskit(m.load_bits(words, init))
        cr = ClassicalRegister(24, "r"); qc.add_register(cr)
        for j, k in enumerate((0, 1, 2)):
            for i in range(8): qc.measure(m.X[k][i], cr[8 * j + i])
        counts = AerSimulator(method="matrix_product_state").run(qc, shots=32, seed_simulator=2026).result().get_counts()
        assert len(counts) == 1, counts
        b = int(next(iter(counts)), 2)
        got = [(b >> (8 * j)) & 255 for j in range(3)]
        res.append((src[0], m.n, want, got, want == got))
    return res


if __name__ == "__main__":
    N = int(sys.argv[1]) if len(sys.argv) > 1 else 100
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    done, same, nq, ng, nq2, ng2 = full_vs_fast(N, seed)
    print(f"full circuit vs fast replay: {same}/{done} identical (seed {seed}); "
          f"full: {nq} qubits {ng} gates, fast reports {nq2} qubits {ng2} gates")
    ok = same == done and nq == nq2 and ng == ng2
    for name, n, want, got, eq in aer_vs_fast():
        print(f"Aer vs fast, program starting '{name}': {n} qubits, fast {want}, Aer {got}, {'equal' if eq else 'DIFFERENT'}")
        ok &= eq
    print("PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)
