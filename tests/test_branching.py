# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Quantag IT Solutions GmbH
"""Quantum branching: an input in superposition sends the machine down two different control-flow
paths of different lengths, in one run (Qiskit Aer, matrix-product-state simulator).

    f:  cbz  w0, 1f          // x = 0: return at once (3 cycles including HLT)
        mov  w1, #3          // x = 1: loop three times (13 cycles)
    2:  add  w2, w2, #2
        subs w1, w1, #1
        b.ne 2b
    1:  ret

x is bit 0 of X0, put in superposition with a Hadamard gate.
  A) measure after U: (x, X2) is (0, 0) or (1, 6), and the 'running' qubit of cycle 3 shows that the
     x = 0 branch has halted while the x = 1 branch is still running.
  B) interference after uncompute: H U Z(X2[1]) U^-1 H, measure x -> deterministic, and the opposite
     outcome without Z. The two branches differ in path, length and history, yet interfere.
  C) the same without U^-1: H U Z H -> 50/50. The branches stay entangled with their histories
     (the per-cycle PC and register history), as the Quantum Control Machine theorem predicts.
Run: python3 -m tests.test_branching"""
SEED = 2026          # Aer seed_simulator: the shot counts below are reproducible
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from qiskit import ClassicalRegister, QuantumCircuit
from qiskit_aer import AerSimulator
from qcpu.machine import Config, QuantumARM
from qcpu.reference import layout
from tests.common import assemble

SRC = ["cbz w0, 1f", "mov w1, #3", "2:", "add w2, w2, #2", "subs w1, w1, #1", "b.ne 2b", "1:", "ret"]

if __name__ == "__main__":
    words = layout(assemble(SRC), 8)
    m = QuantumARM(Config(W=8, regs=(0, 1, 2, 30), SLOTS=8, STEPS=13, MEM=0,
                          classes=("cbz", "movw", "addsub_imm", "bcond", "bra", "hlt")))
    ones = m.load_bits(words, {30: 4 * 7}, pc=0)
    U = m.to_qiskit()
    x = m.X[0][0]
    sim = AerSimulator(method="matrix_product_state")
    print(f"machine: {m.n} qubits, {len(m.g)} gates ({m.counts().get('ccx', 0)} Toffoli), 13 cycles")

    def prep():
        qc = QuantumCircuit(m.n)
        for q in ones: qc.x(q)
        qc.h(x)
        return qc

    qc = prep().compose(U)
    c = ClassicalRegister(10, "c"); qc.add_register(c)
    qc.measure(x, c[0])
    for i in range(8): qc.measure(m.X[2][i], c[1 + i])
    qc.measure(m.run[3], c[9])                    # running after cycle 3?
    counts = sim.run(qc, shots=2000, seed_simulator=SEED).result().get_counts()
    seen = {}
    for k, n in counts.items():
        b = int(k, 2)
        seen[(b & 1, (b >> 1) & 255, b >> 9)] = n
    print("A) (x, X2, running after cycle 3):", seen)
    assert set(seen) == {(0, 0, 0), (1, 6, 1)}

    def interfere(with_z, uncompute=True):
        qc = prep().compose(U)
        if with_z: qc.z(m.X[2][1])
        if uncompute: qc = qc.compose(U.inverse())
        qc.h(x)
        cr = ClassicalRegister(1, "b"); qc.add_register(cr); qc.measure(x, cr)
        return sim.run(qc, shots=2000, seed_simulator=SEED).result().get_counts()
    a, b = interfere(False), interfere(True)
    print("B) after uncompute, without phase:", a, " with phase on X2[1]:", b)
    assert len(a) == 1 and len(b) == 1 and set(a) != set(b)
    cnt = interfere(True, uncompute=False)
    print("C) without uncompute:", cnt)
    assert len(cnt) == 2 and min(cnt.values()) > 800
    print("PASS: superposed input, two paths of different length, coherent after uncompute")
