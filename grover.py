#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Quantag IT Solutions GmbH
"""Grover search with a compiled C function as the oracle, run on the quantum ARM machine.

    python3 grover.py examples/grover_square.c --entry is_root --bits 4
    python3 grover.py examples/grover_divisor.c --entry divides91 --bits 4 --iterations 2

The predicate f(x) is an ordinary C function (or an AArch64 object file). Its machine code is loaded
into the program memory of the quantum ARM machine. The search runs over x = 0 .. 2^n - 1, where x is
the low n bits of X0, the first argument.

One Grover iteration is

    U          run the ARM machine for T cycles: X0 <- f(x), history kept in per-cycle registers
    Z          phase -1 on the result bit (X0 bit 0)
    U^-1       run the machine backwards: X0 <- x again, history and work registers back to 0
    D          diffusion on the n input qubits: H X (multi-controlled Z) X H

U^-1 is what makes the oracle clean (Bennett). Without it the input stays entangled with the
result and the history, and amplification does not happen (see --no-uncompute).

Before any quantum run the tool checks every basis input classically:
  * the CPU (Unicorn) runs f(x) for every x; f must return 0 or 1, halt, and stay inside the W-bit model;
  * T is the largest number of cycles over the whole domain (one common bound, so every branch halts);
  * the quantum machine, simulated gate by gate on each basis input, must give the same f(x).

The quantum run uses Qiskit Aer (matrix-product-state method) and measures the input qubits and
every work qubit, so it also checks that the workspace returns to its initial state in every shot.
All results are from simulation.
"""
import argparse, json, math, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from arm_qpu import load_config, compile_c
from qcpu.a64 import check, MEMOPS
from qcpu.loader import ObjectImage, LinkError
from qcpu.machine import Config, QuantumARM
from qcpu.bitsim import simulate, value
from qcpu.reference import run_reference, layout
from qcpu.sparsesim import SparseState
import numpy as np


def theory(N, M, k):
    """Probability of measuring a solution after k Grover iterations (M solutions out of N)."""
    th = math.asin(math.sqrt(M / N))
    return math.sin((2 * k + 1) * th) ** 2


def wilson(s, n, z=1.96):
    if n == 0:
        return (0.0, 1.0)
    p = s / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def prepare(path, entry, cfg, cc=None, opt=None):
    """Compile/link the predicate. Returns code words (padded to SLOTS), entry index, slots."""
    if path.endswith(".c"):
        cflags = [f for f in cfg["cflags"] if not f.startswith("-O")] + [f"-O{opt}"] if opt else cfg["cflags"]
        path, how = compile_c(path, cc or cfg["compiler"], cflags)
        print("compiled:", how)
    try:
        prog = ObjectImage(path).link(entry)
    except LinkError as e:
        sys.exit(f"error: {e}")
    if prog["uses_output"]:
        sys.exit("error: the predicate must not print (output is not reversible here)")
    code = prog["words"]
    slots = 2
    while slots < len(code) + 1:
        slots *= 2
    checks = [check(w, i, slots) for i, w in enumerate(code)]
    bad = [c for c in checks if not c.ok]
    if bad:
        sys.exit("error: unsupported instructions in the predicate: " + "; ".join(c.reason for c in bad))
    if any(c.cls in MEMOPS for c in checks) or prog["data_end"] > prog["data_offset"]:
        sys.exit("error: the function uses memory; this tool supports register-only functions "
                 "(compile with -O1 or -O2 so that locals stay in registers)")
    return prog, code, slots, checks


def diffusion(qc, xq, mcz):
    for q in xq: qc.h(q)
    for q in xq: qc.x(q)
    mcz()
    for q in xq: qc.x(q)
    for q in xq: qc.h(q)


def run_exact(m, ones, xq, out, k, a, work, sols):
    """Exact simulation of the whole state. Returns sampled counts, P(workspace restored), P(solution)."""
    st = SparseState(m.n, ones)
    for q in xq: st.h(q)
    for _ in range(k):
        st.apply(m.g)                       # U
        st.z(out)                           # oracle phase
        if not a.no_uncompute:
            st.apply(m.g, inverse=True)     # U^-1
        diffusion(st, xq, lambda: st.mcz(xq))
    init = np.zeros(m.n, dtype=np.uint8); init[list(ones)] = 1
    p = np.abs(st.amp) ** 2
    clean = float(np.sum(p[np.all(st.bits[:, work] == init[work], axis=1)]))
    p_sol = sum(v for x, v in st.probabilities(xq).items() if x in sols)
    return st.sample(xq, a.shots, a.seed), clean, p_sol


def run_aer(m, ones, xq, out, k, a, work, n):
    """Qiskit Aer, matrix-product-state method; measures the input and every work qubit."""
    from qiskit import QuantumCircuit, ClassicalRegister
    from qiskit_aer import AerSimulator
    U = m.to_qiskit()
    anc = list(range(m.n, m.n + max(0, n - 2)))

    def mcz():                              # Toffoli chain (the MPS method has no mcx)
        c, t = xq[:-1], xq[-1]
        qc.h(t)
        if len(c) == 1:
            qc.cx(c[0], t)
        else:
            chain = [(c[0], c[1], anc[0])] + [(c[i + 1], anc[i - 1], anc[i]) for i in range(1, len(c) - 1)]
            for g in chain: qc.ccx(*g)
            qc.cx(anc[len(c) - 2], t)
            for g in reversed(chain): qc.ccx(*g)
        qc.h(t)
    qc = QuantumCircuit(m.n + len(anc))
    for q in ones: qc.x(q)
    qc.h(xq)
    for _ in range(k):
        qc.compose(U, inplace=True)
        qc.z(out)
        if not a.no_uncompute:
            qc.compose(U.inverse(), inplace=True)
        diffusion(qc, xq, mcz)
    wq = work + anc
    cx = ClassicalRegister(len(xq), "x"); cw = ClassicalRegister(len(wq), "w")
    qc.add_register(cx); qc.add_register(cw)
    qc.measure(xq, cx); qc.measure(wq, cw)
    counts = AerSimulator(method="matrix_product_state").run(qc, shots=a.shots, seed_simulator=a.seed) \
        .result().get_counts()
    init_w = [1 if q in set(ones) else 0 for q in wq]
    dist, clean = {}, 0
    for key, c in counts.items():
        wbits, xbits = key.split()
        dist[int(xbits, 2)] = dist.get(int(xbits, 2), 0) + c
        clean += c if [int(b) for b in reversed(wbits)] == init_w else 0
    return dist, clean / a.shots, None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("binary", help="C file or AArch64 object with the predicate")
    ap.add_argument("--entry", required=True, help="predicate function: int f(int x), returns 0 or 1")
    ap.add_argument("--bits", type=int, default=4, help="search over x = 0 .. 2^bits - 1 (default 4)")
    ap.add_argument("--iterations", default="auto", help="Grover iterations, or 'auto' (optimal), or a list 0,1,2")
    ap.add_argument("--shots", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=2026, help="Aer seed_simulator (reproducible counts)")
    ap.add_argument("--backend", choices=("exact", "aer"), default="exact",
                    help="exact: sparse state-vector simulation (default); aer: Qiskit Aer MPS (slow)")
    ap.add_argument("--no-uncompute", action="store_true", help="omit U^-1 (shows that amplification then fails)")
    ap.add_argument("--config", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"))
    ap.add_argument("--cc"), ap.add_argument("-O", dest="opt")
    ap.add_argument("--json", help="write the results to this file")
    a = ap.parse_args()
    cfg = load_config(a.config)
    n = a.bits
    N = 1 << n

    prog, code, slots, checks = prepare(a.binary, a.entry, cfg, a.cc, a.opt)
    words = layout(code, slots)
    W0 = max(8, (4 * slots - 1).bit_length() + 1, n + 2)

    # ---- classical check of every basis input on the CPU; one common cycle bound T.
    #      W is the smallest register width (>= W0) at which every input stays inside the model.
    for W in range(W0, 17):
        f, steps, why = {}, {}, None
        for x in range(N):
            r = run_reference(words, W, prog["entry_index"], [x], 0, [], max_steps=4096)
            if r["error"] or not r["halted"]:
                sys.exit(f"error: f({x}) did not run to completion on the CPU: {r['error']}")
            if r["regs"][0] not in (0, 1):
                sys.exit(f"error: f({x}) = {r['regs'][0]}; the predicate must return 0 or 1")
            if r["issues"]:
                why = f"f({x}): {r['issues'][0]}"
                break
            f[x], steps[x] = r["regs"][0], r["steps"]
        if why is None:
            break
    else:
        sys.exit(f"error: the predicate leaves the W-bit model even at W = 16: {why}")
    sols = sorted(x for x in range(N) if f[x])
    M = len(sols)
    T = max(steps.values())
    print(f"predicate {prog['entry']}: {len(code)} instructions, solutions over 0..{N - 1}: {sols} "
          f"(M = {M} of N = {N}); cycles per input {min(steps.values())}..{T}, common bound T = {T}")
    if M == 0 or M == N:
        sys.exit("nothing to search: the predicate is constant on the domain")

    # ---- the machine: decoder with the classes the predicate uses, registers it touches
    used = {0, 30}
    for c in checks:
        used |= set(c.reads) | set(c.writes)
    used.discard(31)
    classes = tuple(sorted({c.cls for c in checks} | {"hlt"}))
    m = QuantumARM(Config(W=W, regs=tuple(sorted(used)), sp=any(c.uses_sp for c in checks),
                          SLOTS=slots, STEPS=T, MEM=0, classes=classes))
    ones = m.load_bits(words, {0: 0, 30: 4 * (slots - 1)}, pc=prog["entry_index"])
    cnt = m.counts()
    print(f"machine: W = {W}, {len(classes)} instruction classes, {slots} program slots, {T} cycles: "
          f"{m.n} qubits, {len(m.g)} gates ({cnt.get('ccx', 0)} Toffoli) per run of U")

    # ---- the quantum machine on every basis input, gate by gate
    xq = m.X[0][:n]
    for x in range(N):
        init = sorted(set(ones) | {xq[i] for i in range(n) if x >> i & 1})
        st = simulate(m.g, m.n, init)
        got = value(st, m.X[0])
        if got != f[x] or st[m.run[T]] or any(st[t] for t in m.trap):
            sys.exit(f"error: quantum machine gives f({x}) = {got}, CPU {f[x]}")
    print(f"basis check: the quantum machine computes f(x) correctly for all {N} inputs")

    # ---- Grover's algorithm
    out = m.X[0][0]
    work = [q for q in range(m.n) if q not in xq]
    kopt = max(1, round(math.pi / (4 * math.asin(math.sqrt(M / N))) - 0.5))
    ks = [kopt] if a.iterations == "auto" else [int(v) for v in a.iterations.split(",")]
    print(f"simulator: {'exact sparse state vector' if a.backend == 'exact' else 'Qiskit Aer, matrix product state'}; "
          f"optimal number of iterations for M/N = {M}/{N}: {kopt}")
    results = []
    for k in ks:
        t0 = time.time()
        if a.backend == "exact":
            dist, clean, p_sol = run_exact(m, ones, xq, out, k, a, work, sols)
        else:
            dist, clean, p_sol = run_aer(m, ones, xq, out, k, a, work, n)
        hit = sum(c for x, c in dist.items() if x in sols)
        lo, hi = wilson(hit, a.shots)
        p_th = theory(N, M, k)
        res = dict(iterations=k, shots=a.shots, measured_success=hit / a.shots, ci95=[lo, hi], theory=p_th,
                   exact_success=p_sol, clean_workspace=clean, distribution={str(x): dist[x] for x in sorted(dist)},
                   seconds=round(time.time() - t0, 1))
        results.append(res)
        top = ", ".join(f"{x}:{dist[x]}" for x in sorted(dist, key=lambda v: -dist[v])[:8])
        ex = f"exact {p_sol:.4f}, " if p_sol is not None else ""
        print(f"k = {k}: P(solution) {ex}theory {p_th:.4f}; measured {hit}/{a.shots} = {hit / a.shots:.3f} "
              f"[95% CI {lo:.3f}..{hi:.3f}]; workspace restored with probability {clean:.4f} ({res['seconds']} s)\n"
              f"        counts {top}")
    ok = all((r["exact_success"] is None or abs(r["exact_success"] - r["theory"]) < 1e-9)
             and r["ci95"][0] <= r["theory"] <= r["ci95"][1] and abs(r["clean_workspace"] - 1) < 1e-9
             for r in results)
    if a.no_uncompute:
        print("without U^-1 the oracle is not clean: the input stays entangled with result and history")
    else:
        print("PASS: amplitudes amplified as Grover's algorithm predicts, workspace restored in every shot"
              if ok else "FAIL: results differ from theory or the workspace was not restored")
    if a.json:
        json.dump(dict(predicate=prog["entry"], bits=n, solutions=sols, cycles=T, W=W, qubits=m.n,
                       gates_per_U=len(m.g), toffoli_per_U=cnt.get("ccx", 0), uncompute=not a.no_uncompute,
                       seed=a.seed, results=results), open(a.json, "w"), indent=1)
    sys.exit(0 if ok or a.no_uncompute else 1)


if __name__ == "__main__":
    main()
