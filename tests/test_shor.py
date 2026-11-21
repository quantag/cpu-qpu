# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Quantag IT Solutions GmbH
"""Shor's algorithm with modexp compiled from C as the oracle (shor.py), and checks of the exact
sparse simulator and the circuit pieces it relies on.

  1. SparseState against Qiskit's dense Statevector on 200 random circuits of X, CX, CCX, CSWAP, H, Z
     and controlled-phase gates (10 qubits); applying the inverse circuit returns the start state.
  2. The bit-parallel routine for permutation gates equals gate-by-gate application (50 circuits).
  3. The QFT gate list equals the discrete Fourier transform for t = 1..6 qubits, on every basis state.
  4. Classical post-processing: the ideal peaks give the order; y = 0 gives nothing.
  5. N = 15, a = 7: factors 3 x 5; outcome distribution equals the textbook one; workspace restored.
  6. The inverse QFT applied to the whole state gives the same distribution as applying it to the
     independent parts (N = 15, 5 exponent qubits).
  7. N = 21, a = 2: factors 3 x 7 (period 6 does not divide 2^t, so the peaks are broad).
  8. Control without U^-1: the outcome is uniform and the success rate is the chance level.
  9. N = 21, a = 5: the order is found, but 5^3 = -1 mod 21, so this base gives no factor (exit code 2).
 10. N = 33 with no base given: bases 2 and 4 give no factor, base 5 gives 3 x 11.
Run: python3 -m tests.test_shor"""
import json, math, os, random, subprocess, sys, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import numpy as np
from qiskit import QuantumCircuit
from qiskit.quantum_info import Statevector
from qcpu.sparsesim import SparseState
from shor import qft_gates, order_from, factors_from


def dense(st):
    v = np.zeros(2 ** st.n, dtype=complex)
    for b, a in zip(st.bits, st.amp):
        v[int(sum(int(x) << i for i, x in enumerate(b)))] += a
    return v


def sparse_vs_dense(trials=200, n=10, seed=11):
    r = random.Random(seed)
    for _ in range(trials):
        qc, st, gates = QuantumCircuit(n), SparseState(n), []
        for _ in range(40):
            g = r.choice(["x", "cx", "ccx", "cswap", "h", "h", "z", "cp", "cp"])
            if g == "cp":
                a, b = r.sample(range(n), 2)
                th = r.uniform(-math.pi, math.pi)
                qc.cp(th, a, b); gates.append(("cp", th, a, b))
            else:
                qs = r.sample(range(n), {"x": 1, "h": 1, "z": 1, "cx": 2, "ccx": 3, "cswap": 3}[g])
                getattr(qc, g)(*qs); gates.append((g, *qs))
        st.apply(gates)
        assert np.allclose(Statevector(qc).data, dense(st), atol=1e-9)
        st.apply(gates, inverse=True)
        assert len(st.amp) == 1 and not st.bits.any() and abs(st.amp[0] - 1) < 1e-9
    return trials


def fast_vs_plain(trials=50, n=14, seed=3):
    r = random.Random(seed)
    for _ in range(trials):
        gates = []
        for _ in range(300):
            g = r.choice(["x", "cx", "ccx", "cswap"])
            gates.append((g, *r.sample(range(n), {"x": 1, "cx": 2, "ccx": 3, "cswap": 3}[g])))
        a, b = SparseState(n, [1, 5]), SparseState(n, [1, 5])
        for q in (0, 2, 3, 7, 9):
            a.h(q); b.h(q)
        a.apply(gates, fast=False); b.apply(gates)
        assert (a.bits == b.bits).all() and np.allclose(a.amp, b.amp)
        a.apply(gates, inverse=True, fast=False); b.apply(gates, inverse=True)
        assert (a.bits == b.bits).all()
    return trials


def qft_is_dft():
    for t in range(1, 7):
        Q = 1 << t
        for x in range(Q):
            st = SparseState(t, [i for i in range(t) if x >> i & 1])
            st.apply(qft_gates(list(range(t))))
            want = np.exp(2j * np.pi * x * np.arange(Q) / Q) / math.sqrt(Q)
            assert np.allclose(dense(st), want, atol=1e-9)
            st.apply(qft_gates(list(range(t))), inverse=True)
            assert len(st.amp) == 1 and st.values(list(range(t)))[0] == x


def post_processing():
    for a, N, t in ((7, 15, 8), (2, 21, 9), (2, 35, 11), (5, 33, 11)):
        Q = 1 << t
        r = next(k for k in range(1, N) if pow(a, k, N) == 1)
        assert order_from(0, Q, a, N) is None
        for k in range(1, r):
            assert order_from(round(k * Q / r), Q, a, N) == r, (a, N, k)
        p, q = factors_from(r, a, N)
        assert 1 < p < q < N and p * q == N


def shor(*args):
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        out = f.name
    p = subprocess.run([sys.executable, os.path.join(ROOT, "shor.py"), "--json", out, *args],
                       capture_output=True, text=True)
    print("\n".join("    " + l for l in p.stdout.strip().splitlines() if not l.startswith("compiled")))
    assert p.returncode in (0, 2), p.stderr
    return p.returncode, json.load(open(out))


if __name__ == "__main__":
    print(f"1) sparse simulator = dense Statevector on {sparse_vs_dense()} random circuits (with phase gates and inverses)")
    print(f"2) bit-parallel permutation routine = gate-by-gate on {fast_vs_plain()} circuits")
    qft_is_dft()
    print("3) QFT gate list = discrete Fourier transform, t = 1..6")
    post_processing()
    print("4) post-processing: ideal peaks give the order and the factors")

    rc, r = shor("--N", "15", "--a", "7")
    x = r["runs"][0]
    assert rc == 0 and r["status"] == "factored" and x["factors"] == [[3, 5]] and x["order"] == 4
    assert x["workspace_restored"] and x["exponent_restored"] and x["max_abs_diff_from_theory"] < 1e-9
    assert abs(x["exact_success"] - 0.75) < 1e-9 and x["chance_level"] < 0.1
    print("5) N = 15, a = 7: 3 x 5; distribution equals the textbook one; workspace restored")

    _, r1 = shor("--N", "15", "--a", "7", "--bits", "5")
    _, r2 = shor("--N", "15", "--a", "7", "--bits", "5", "--full-state")
    d1, d2 = r1["runs"][0]["distribution"], r2["runs"][0]["distribution"]
    assert d1.keys() == d2.keys() and all(abs(d1[k] - d2[k]) < 1e-12 for k in d1)
    assert r1["runs"][0]["parts"] == 4 and r2["runs"][0]["parts"] == 1
    print("6) inverse QFT on the whole state = on the independent parts")

    rc, r = shor("--N", "21", "--a", "2")
    x = r["runs"][0]
    assert rc == 0 and x["factors"] == [[3, 7]] and x["order"] == 6 and x["max_abs_diff_from_theory"] < 1e-9
    assert x["workspace_restored"] and x["exact_success"] > 0.6 and x["chance_level"] < 0.1
    print("7) N = 21, a = 2: 3 x 7")

    rc, r = shor("--N", "15", "--a", "7", "--no-uncompute")
    x = r["runs"][0]
    assert r["status"] == "control" and not x["workspace_restored"]
    assert len(x["distribution"]) == 256 and max(x["distribution"].values()) - min(x["distribution"].values()) < 1e-12
    assert abs(x["exact_success"] - x["chance_level"]) < 1e-12
    print("8) without U^-1: uniform outcome, success = chance level")

    rc, r = shor("--N", "21", "--a", "5")
    x = r["runs"][0]
    assert rc == 2 and r["status"] == "unlucky" and x["factors"] == [] and x["exact_order"] > 0.6
    print("9) N = 21, a = 5: order found, base gives no factor")

    rc, r = shor("--N", "33")
    assert rc == 0 and [x["a"] for x in r["runs"]] == [2, 4, 5]
    assert [x["status"] for x in r["runs"]] == ["unlucky", "unlucky", "factored"] and r["runs"][-1]["factors"] == [[3, 11]]
    print("10) N = 33, no base given: bases 2 and 4 give no factor, base 5 gives 3 x 11")
    print("PASS")
