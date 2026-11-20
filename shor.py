#!/usr/bin/env python3
"""Shor's factoring algorithm with the modular exponentiation written in C and run as ARM machine
code on the quantum ARM machine.

    python3 shor.py --N 15 --a 7
    python3 shor.py --N 21 --a 2
    python3 shor.py examples/shor_modexp.c --entry modexp --N 35 --a 2 --shots 2000

The classical core of Shor's algorithm is f(x) = a^x mod N. Here f is an ordinary C function,
unsigned modexp(unsigned a, unsigned x, unsigned n), compiled by clang or gcc. Its machine code is
loaded into the program memory of the quantum ARM machine; a and N are ordinary register values and
the exponent x (t low bits of X1) is in superposition.

    H on x          all 2^t exponents at once
    U               run the ARM machine for T cycles: X0 <- a^x mod N. The loop runs once per bit of
                    x, so the branches take different numbers of cycles; T is the largest.
    copy            CNOT the result into a fresh output register
    U^-1            run the machine backwards: x and every work and history qubit are restored
    QFT^-1 on x     inverse quantum Fourier transform
    measure x       y is close to a multiple of 2^t / r, where r is the order of a modulo N

Then, classically: continued fractions give r from y / 2^t, and gcd(a^(r/2) +- 1, N) gives the factors.

Checks made on every run:
  * the CPU (Unicorn) runs modexp for every exponent 0 .. 2^t - 1: correct value, halts, stays inside
    the W-bit model; T is the largest cycle count over this whole domain;
  * after U, every branch of the superposition holds the right a^x mod N and has halted;
  * after U^-1, the exponent is restored and every work qubit is back in its initial state;
  * the exact outcome distribution equals the textbook distribution for the order r.

Simulation: exact sparse state vector (qcpu/sparsesim.py). All results are from simulation, not from
quantum hardware, and the interpreter makes this far more expensive than a hand-built modular
exponentiation circuit: it demonstrates the principle, not a practical way to factor.
"""
import argparse, json, math, os, sys, time
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
import numpy as np
from arm_qpu import load_config
from grover import prepare, wilson
from qcpu.machine import Config, QuantumARM
from qcpu.reference import run_reference, layout
from qcpu.sparsesim import SparseState


def qft_gates(q):
    """Quantum Fourier transform on the qubits q (q[0] = least significant bit):
    |x> -> 2^(-t/2) sum_y e^(2 pi i x y / 2^t) |y>.  Gates: H, controlled phase, and swaps as 3 CNOTs."""
    g, t = [], len(q)
    for j in reversed(range(t)):
        g.append(("h", q[j]))
        for k in reversed(range(j)):
            g.append(("cp", math.pi / 2 ** (j - k), q[k], q[j]))
    for i in range(t // 2):
        a, b = q[i], q[t - 1 - i]
        g += [("cx", a, b), ("cx", b, a), ("cx", a, b)]
    return g


def textbook_distribution(f, Q):
    """Outcome distribution of ideal order finding: for each value v of f, the inverse Fourier
    transform of the indicator of {x : f(x) = v}, computed here with a classical FFT."""
    f = np.asarray(f)
    P = np.zeros(Q)
    for v in set(f.tolist()):
        P += np.abs(np.fft.fft((f == v).astype(complex)) / Q) ** 2
    return P


def order_from(y, Q, a, N):
    """Classical post-processing: the order r of a modulo N from one measured y, or None.

    Textbook rule: y/Q is within 1/(2Q) of some k/r, and then k/r (in lowest terms h/d) is a convergent
    of the continued fraction of y/Q. So take the convergent h/d with d < N and |y/Q - h/d| <= 1/(2Q).
    If k and r have a common factor, d is only a divisor of r, so d, 2d, 3d, ... (a few multiples) are
    tested with a^r mod N == 1, which is cheap to check. y = 0 carries no information."""
    if y == 0:
        return None
    num, den, h0, h1, d0, d1 = y, Q, 0, 1, 1, 0
    while den:
        q = num // den
        num, den = den, num - q * den
        h0, h1 = h1, q * h1 + h0
        d0, d1 = d1, q * d1 + d0
        if d1 >= N:
            break
        if d1 > 1 and abs(2 * (y * d1 - h1 * Q)) <= d1:          # |y/Q - h/d| <= 1/(2Q)
            for mult in range(1, N.bit_length() + 1):
                R = d1 * mult
                if R < N and pow(a, R, N) == 1:
                    return next(d for d in range(1, R + 1) if R % d == 0 and pow(a, d, N) == 1)
    return None


def factors_from(r, a, N):
    if r is None or r % 2:
        return None
    h = pow(a, r // 2, N)
    if h == N - 1:
        return None
    p = math.gcd(h - 1, N)
    return tuple(sorted((p, N // p))) if 1 < p < N else None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("binary", nargs="?", default=os.path.join(ROOT, "examples", "shor_modexp.c"),
                    help="C file or AArch64 object with unsigned modexp(unsigned a, unsigned x, unsigned n)")
    ap.add_argument("--entry", default="modexp")
    ap.add_argument("--N", type=int, default=15, help="number to factor (odd, composite, not a prime power)")
    ap.add_argument("--a", type=int, help="base, coprime to N (default: try 2, 3, 4, ... until one gives the factors)")
    ap.add_argument("--bits", type=int, help="exponent qubits t (default: smallest t with 2^t >= N^2)")
    ap.add_argument("--shots", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=2026)
    ap.add_argument("--no-uncompute", action="store_true", help="omit U^-1 (shows that order finding then fails)")
    ap.add_argument("--full-state", action="store_true",
                    help="apply the inverse QFT to the whole state instead of to its independent parts (slow; a cross-check)")
    ap.add_argument("--config", default=os.path.join(ROOT, "config.json"))
    ap.add_argument("--cc"), ap.add_argument("-O", dest="opt")
    ap.add_argument("--json", help="write the results to this file")
    A = ap.parse_args()
    N = A.N
    if N < 15 or N % 2 == 0 or all(N % d for d in range(3, math.isqrt(N) + 1, 2)):
        sys.exit("error: N must be an odd composite number (15, 21, 33, 35, ...)")
    if any(round(N ** (1 / k)) ** k == N for k in range(2, N.bit_length())):
        sys.exit("error: N is a prime power; Shor's algorithm needs two different prime factors")
    if A.a is not None:
        if not 1 < A.a < N:
            sys.exit("error: need 1 < a < N")
        if math.gcd(A.a, N) != 1:
            sys.exit(f"gcd({A.a}, {N}) = {math.gcd(A.a, N)} is already a factor of {N}; choose a coprime to N")
    bases = [A.a] if A.a is not None else [b for b in range(2, N - 1) if math.gcd(b, N) == 1]
    t = A.bits or (N * N - 1).bit_length()
    cfg = load_config(A.config)
    A.select = cfg.get("memory_select", "tree")
    prog, code, slots, checks = prepare(A.binary, A.entry, cfg, A.cc, A.opt)
    runs, status = [], "failed"
    for a in bases:
        status, res = run_base(a, N, t, prog, code, slots, checks, A)
        runs.append(res)
        if status != "unlucky":
            break
        if a != bases[-1]:
            print("trying the next base\n")
    if A.json:
        json.dump(dict(N=N, bits=t, status=status, runs=runs), open(A.json, "w"), indent=1)
    sys.exit({"factored": 0, "control": 0, "unlucky": 2}.get(status, 1))


def run_base(a, N, t, prog, code, slots, checks, A):
    """Order finding for one base a. Returns (status, results)."""
    Q = 1 << t
    words = layout(code, slots)

    # ---- CPU: every exponent of the domain. W = smallest register width that keeps all of them
    #      inside the model; T = the largest cycle count (one bound for every branch).
    t0 = time.time()
    for W in range(max(8, (4 * slots - 1).bit_length() + 1, t + 1), 17):
        f, steps, why = [], [], None
        for x in range(Q):
            r = run_reference(words, W, prog["entry_index"], [a, x, N], 0, [], max_steps=8192)
            if r["error"] or not r["halted"]:
                sys.exit(f"error: modexp({a}, {x}, {N}) did not run to completion on the CPU: {r['error']}")
            if r["regs"][0] != pow(a, x, N):
                sys.exit(f"error: {prog['entry']}({a}, {x}, {N}) returned {r['regs'][0]}, expected {pow(a, x, N)}")
            if r["issues"]:
                why = f"x = {x}: {r['issues'][0]}"
                break
            f.append(r["regs"][0]); steps.append(r["steps"])
        if why is None:
            break
    else:
        sys.exit(f"error: the computation leaves the W-bit model even at W = 16 ({why})")
    T = max(steps)
    r_true = next(k for k in range(1, N) if pow(a, k, N) == 1)
    print(f"{prog['entry']}: {len(code)} instructions; a = {a}, N = {N}; exponent register: t = {t} qubits (x = 0..{Q - 1})")
    print(f"CPU check: a^x mod N correct for all {Q} exponents, {min(steps)}..{T} machine cycles, "
          f"all inside the {W}-bit model ({time.time() - t0:.1f} s)")

    # ---- the machine (unchanged interpreter; decoder with the classes this code uses)
    used = {0, 1, 2, 30}
    for c in checks:
        used |= set(c.reads) | set(c.writes)
    used.discard(31)
    classes = tuple(sorted({c.cls for c in checks} | {"hlt"}))
    m = QuantumARM(Config(W=W, regs=tuple(sorted(used)), sp=any(c.uses_sp for c in checks),
                          SLOTS=slots, STEPS=T, MEM=0, classes=classes, select=A.select))
    ones = m.load_bits(words, {0: a, 2: N, 30: 4 * (slots - 1)}, pc=prog["entry_index"])
    cnt = m.counts()
    nb = N.bit_length()
    xq, OUT = m.X[1][:t], list(range(m.n, m.n + nb))
    n_total = m.n + nb
    iqft = qft_gates(xq)
    total_gates = t + (1 if A.no_uncompute else 2) * len(m.g) + nb + len(iqft)
    print(f"machine: W = {W}, {len(classes)} instruction classes, {slots} program slots, {T} cycles: "
          f"{m.n} qubits, {len(m.g)} gates ({cnt.get('ccx', 0)} Toffoli) per run of U")
    print(f"circuit: H on x, U, copy{'' if A.no_uncompute else ', U^-1'}, inverse QFT: {n_total} qubits, {total_gates} gates")

    # ---- the quantum computation
    t0 = time.time()
    st = SparseState(n_total, ones)
    for q in xq:
        st.h(q)
    xs = st.values(xq)                                   # exponent of each branch (row order is kept below)
    st.apply(m.g)                                        # U
    fx = np.array(f)[xs]
    if not (np.array_equal(st.values(m.X[0]), fx) and not st.bits[:, m.run[T]].any()
            and not st.bits[:, m.trap].any()):
        sys.exit("error: a branch of the superposition has a wrong result, did not halt, or trapped")
    print(f"after U: X0 = a^x mod N in all {len(xs)} branches, every branch halted, no trap")
    for i in range(nb):
        st.cx(m.X[0][i], OUT[i])                         # copy the result
    init = np.zeros(n_total, dtype=np.uint8); init[list(ones)] = 1
    work = np.ones(n_total, dtype=bool); work[xq] = False; work[OUT] = False
    if not A.no_uncompute:
        st.apply(m.g, inverse=True)                      # U^-1
    restored = bool(np.array_equal(st.bits[:, work], np.tile(init[work], (len(st.amp), 1))))
    x_back = bool(np.array_equal(st.values(xq), xs) and np.array_equal(st.values(OUT), fx))
    if A.no_uncompute:
        print("U^-1 omitted: the exponent has been consumed by the program and survives only in the history qubits")
    else:
        print(f"after U^-1: exponent register restored: {x_back}; all {int(work.sum())} work and history qubits "
              f"back in their initial state: {restored}")

    P = np.zeros(Q)
    if A.full_state:
        st.apply(iqft, inverse=True)
        for y, p in st.probabilities(xq).items():
            P[y] += p
        n_parts = 1
    else:
        parts = st.split(xq)                             # parts that differ on untouched qubits never interfere
        n_parts = len(parts)
        local = qft_gates(list(range(t)))
        for part in parts:
            part.apply(local, inverse=True)
            for y, p in part.probabilities(list(range(t))).items():
                P[y] += p
    sim_seconds = time.time() - t0

    # ---- compare with the textbook distribution, sample, post-process
    P_th = textbook_distribution(f, Q)
    err = float(np.max(np.abs(P - P_th)))
    order = {y: order_from(y, Q, a, N) for y in range(Q)}
    info = {y: factors_from(order[y], a, N) for y in range(Q)}
    p_success = float(sum(P[y] for y in range(Q) if info[y]))
    p_order = float(sum(P[y] for y in range(Q) if order[y] == r_true))
    chance = sum(1 for y in range(Q) if info[y]) / Q            # success probability of a uniformly random y
    rng = np.random.default_rng(A.seed)
    shots = rng.choice(Q, size=A.shots, p=P / P.sum())
    ys, cs = np.unique(shots, return_counts=True)
    counts = dict(zip(ys.tolist(), cs.tolist()))
    hits = sum(c for y, c in counts.items() if info[y])
    hits_r = sum(c for y, c in counts.items() if order[y] == r_true)
    lo, hi = wilson(hits, A.shots)
    print(f"measurement of x, exact probabilities ({sim_seconds:.1f} s; textbook value in brackets):")
    if P.max() - P.min() < 1e-12:
        print(f"    uniform: every y = 0..{Q - 1} has probability 1/{Q} (textbook: {int((P_th > 1e-9).sum())} "
              f"outcomes with nonzero probability, largest {P_th.max():.4f})")
    else:
        for y in sorted(range(Q), key=lambda v: -P[v])[:8]:
            if P[y] < 1e-9:
                break
            note = "no order found" if order[y] is None else f"r = {order[y]}" + (
                f" -> {N} = {info[y][0]} x {info[y][1]}" if info[y] else "")
            print(f"    y = {y:5d}  P = {P[y]:.4f} ({P_th[y]:.4f})  y/2^t = {y / Q:.4f}  {note}")
    print(f"largest difference from the textbook distribution: {err:.1e}")
    print(f"{A.shots} shots (seed {A.seed}): {hits_r} gave the order r = {r_true}, {hits} gave the factors "
          f"= {hits / A.shots:.3f} [95% CI {lo:.3f}..{hi:.3f}]")
    print(f"exact probability per shot: order {p_order:.4f}, factors {p_success:.4f}; "
          f"a uniformly random y would give the factors with probability {chance:.4f}")
    found = sorted({info[y] for y in counts if info[y]})
    sound = restored and x_back and err < 1e-9
    h = pow(a, r_true // 2, N) if r_true % 2 == 0 else None
    if A.no_uncompute:
        status = "control"
        print("without U^-1 the outcome carries no information about the period: the success rate is the chance level")
    elif sound and found:
        status = "factored"
        print(f"RESULT: {N} = {found[0][0]} x {found[0][1]}   (order of {a} modulo {N}: r = {r_true})")
    elif sound and hits_r and (h is None or h == N - 1):
        status = "unlucky"
        why = "r is odd" if h is None else f"{a}^(r/2) = -1 mod {N}"
        print(f"order r = {r_true} found correctly, but {why}: this base gives no factor "
              "(the classical part of Shor's algorithm then picks another base)")
    else:
        status = "failed"
        print("FAIL: workspace not restored, distribution differs from theory, or no order found")
    return status, dict(a=a, order=r_true, W=W, cycles=[min(steps), T], qubits=n_total, gates_per_U=len(m.g),
                        toffoli_per_U=cnt.get("ccx", 0), total_gates=total_gates, uncompute=not A.no_uncompute,
                        workspace_restored=restored, exponent_restored=x_back, parts=n_parts,
                        max_abs_diff_from_theory=err, exact_order=p_order, exact_success=p_success,
                        chance_level=chance, shots=A.shots, seed=A.seed, hits=hits,
                        factors=[list(x) for x in found], status=status,
                        distribution={str(y): float(P[y]) for y in range(Q) if P[y] > 1e-12})


if __name__ == "__main__":
    main()
