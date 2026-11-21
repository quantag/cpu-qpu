# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Quantag IT Solutions GmbH
"""Grover search with compiled C predicates as oracles (grover.py), and a check of the exact sparse
simulator it uses.

  1. SparseState against Qiskit's dense Statevector on 200 random circuits of X, CX, CCX, CSWAP, H, Z
     (10 qubits): all amplitudes agree.
  2. examples/grover_tiny.c on both simulators: exact sparse and Qiskit Aer (MPS) give P = 1.
  3. examples/grover_divisor.c, 4 input bits, k = 0..3 iterations: the exact success probability equals
     sin^2((2k+1) theta), the workspace (all registers and history) returns to its initial state,
     and the sampled counts agree with theory.
  4. examples/grover_square.c: 4 solutions of 16, one iteration finds a solution with certainty.
  5. control: without U^-1 the solution is not amplified.
Run: python3 -m tests.test_grover"""
import json, os, random, subprocess, sys, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import numpy as np
from qiskit import QuantumCircuit
from qiskit.quantum_info import Statevector
from qcpu.sparsesim import SparseState


def sparse_vs_dense(trials=200, n=10, seed=7):
    r = random.Random(seed)
    for _ in range(trials):
        qc, st = QuantumCircuit(n), SparseState(n)
        for _ in range(40):
            g = r.choice(["x", "cx", "ccx", "cswap", "h", "h", "z"])
            k = {"x": 1, "h": 1, "z": 1, "cx": 2, "ccx": 3, "cswap": 3}[g]
            qs = r.sample(range(n), k)
            getattr(qc, g)(*qs); getattr(st, g)(*qs)
        dense = Statevector(qc).data                      # qiskit: qubit 0 is the least significant bit
        mine = np.zeros(2 ** n, dtype=complex)
        for b, a in zip(st.bits, st.amp):
            mine[int(sum(int(v) << i for i, v in enumerate(b)))] += a
        assert np.allclose(dense, mine, atol=1e-9)
    return trials


def grover(src, entry, *args):
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        out = f.name
    p = subprocess.run([sys.executable, os.path.join(ROOT, "grover.py"), os.path.join(ROOT, src), "--entry", entry,
                        "--json", out, *args], capture_output=True, text=True)
    print(p.stdout.strip())
    return p.returncode, json.load(open(out))


if __name__ == "__main__":
    print(f"1) sparse simulator = dense Statevector on {sparse_vs_dense()} random circuits")
    rc, r = grover("examples/grover_tiny.c", "is_two", "--bits", "2")
    assert rc == 0 and r["results"][0]["exact_success"] > 1 - 1e-9
    rc, r = grover("examples/grover_tiny.c", "is_two", "--bits", "2", "--backend", "aer", "--shots", "200")
    assert rc == 0 and r["results"][0]["measured_success"] == 1.0 and r["results"][0]["clean_workspace"] == 1.0
    print("2) grover_tiny: exact and Aer agree (P = 1)")
    rc, r = grover("examples/grover_divisor.c", "divides91", "--bits", "4", "--iterations", "0,1,2,3")
    assert rc == 0 and r["solutions"] == [7, 13]
    for x in r["results"]:
        assert abs(x["exact_success"] - x["theory"]) < 1e-9 and abs(x["clean_workspace"] - 1) < 1e-9
    print("3) grover_divisor: P(k) = sin^2((2k+1) theta) for k = 0..3, workspace restored")
    rc, r = grover("examples/grover_square.c", "is_root", "--bits", "4", "--iterations", "1")
    assert rc == 0 and r["solutions"] == [3, 5, 11, 13] and r["results"][0]["exact_success"] > 1 - 1e-9
    print("4) grover_square: one iteration, P = 1")
    rc, r = grover("examples/grover_divisor.c", "divides91", "--bits", "4", "--iterations", "2", "--no-uncompute")
    assert r["results"][0]["exact_success"] < 0.2 and r["results"][0]["clean_workspace"] < 1e-9
    print("5) without U^-1: no amplification")
    print("PASS")
