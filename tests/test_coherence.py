# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Quantag IT Solutions GmbH
"""Coherence test on the current machine (Qiskit Aer, matrix-product-state simulator).

Program (clang-assembled A64):  movz w0, #5 ; add w0, w0, #3 ; ret
1) Superposed program: bit 5 of the first instruction word is imm16[0], i.e. #5 <-> #4.
   Put that program-memory qubit in superposition: one run executes both programs and
   X0 is measured as 8 or 7 with probability 1/2 each.
2) Interference: H(bit) U Z(X0[0]) U^-1 H(bit), then measure bit. The outcome is deterministic
   and opposite to the same circuit without Z. A machine that measured or classically
   dispatched anything inside U would give 50/50 instead.
Run: python3 -m tests.test_coherence"""
SEED = 2026          # Aer seed_simulator: the shot counts below are reproducible
import os, subprocess, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from qiskit import ClassicalRegister, QuantumCircuit
from qiskit_aer import AerSimulator
from qcpu.machine import Config, QuantumARM
from qcpu.reference import layout

with tempfile.TemporaryDirectory() as d:
    open(os.path.join(d, "p.s"), "w").write(".text\nmovz w0, #5\nadd w0, w0, #3\nret\n")
    subprocess.run(["clang", "--target=aarch64-linux-gnu", "-c", os.path.join(d, "p.s"), "-o", os.path.join(d, "p.o")], check=True)
    subprocess.run(["llvm-objcopy", "-O", "binary", "--only-section=.text", os.path.join(d, "p.o"), os.path.join(d, "p.bin")], check=True)
    code = open(os.path.join(d, "p.bin"), "rb").read()
words = layout([int.from_bytes(code[i:i + 4], "little") for i in range(0, len(code), 4)], 4)
m = QuantumARM(Config(W=8, regs=(0, 30), SLOTS=4, STEPS=4, MEM=0, classes=("movw", "addsub_imm", "bra", "hlt")))
ones = m.load_bits(words, {30: 4 * 3}, pc=0)
U = m.to_qiskit()
bit = m.mem[0][5]
sim = AerSimulator(method="matrix_product_state")
print(f"machine: {m.n} qubits, {len(m.g)} gates ({m.counts().get('ccx', 0)} Toffoli)")


def prep():
    qc = QuantumCircuit(m.n)
    for q in ones: qc.x(q)
    qc.h(bit)
    return qc


qc = prep().compose(U)
cr = ClassicalRegister(8, "x0"); qc.add_register(cr); qc.measure(m.X[0], cr)
counts = sim.run(qc, shots=2000, seed_simulator=SEED).result().get_counts()
dist = {int(k, 2): v for k, v in counts.items()}
print("1) superposed program -> X0:", dist)
assert set(dist) == {7, 8}


def interfere(with_z):
    qc = prep().compose(U)
    if with_z: qc.z(m.X[0][0])
    qc = qc.compose(U.inverse()); qc.h(bit)
    cr = ClassicalRegister(1, "b"); qc.add_register(cr); qc.measure(bit, cr)
    return sim.run(qc, shots=2000, seed_simulator=SEED).result().get_counts()


a, b = interfere(False), interfere(True)
print("2) interference without phase:", a, " with phase on X0[0]:", b)
assert len(a) == 1 and len(b) == 1 and set(a) != set(b)
print("PASS: the machine executes a superposition of two ARM programs coherently")
