# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Quantag IT Solutions GmbH
"""include/qarm_sv.h, the state vector of Q-ARM's VQE tables, against dense matrices in numpy.

  1. Random problems on 3 to 8 qubits: Hamiltonians with every kind of Pauli term (X, Y, Z mixed, an even and an
     odd number of Y, terms that share an X mask), ansatz tables with every operation: X, H, CX, CZ, RY, RZ, and
     Pauli rotations in runs that share a parameter and an X mask, commuting (fused into one pass) or not (kept
     apart). The energy and the gradient (adjoint) equal the dense exp(-i a P) products to 1e-10, and the gradient
     equals central differences to 1e-6.
  2. Two Hamiltonians used in turn: the kept factors follow the table that is passed.
  3. Built with -fopenmp (when the compiler has it) and run on 1 and 4 threads, on 16 qubits (above
     QARM_SV_PAR_MIN): the same numbers to 1e-12.
Run: python3 -m tests.test_qarm_sv"""
import math, os, random, shutil, subprocess, sys, tempfile
import numpy as np
from tests.test_qarm_exec import ROOT

INC = os.path.join(ROOT, "include")
KIND = {"x": 1, "h": 2, "cx": 3, "cz": 4, "ry": 5, "rz": 6, "pr": 7}


def pauli(n, x, z):
    """i^popcount(x & z) X^x Z^z as a dense matrix (bit q = qubit q)."""
    N = 1 << n
    M = np.zeros((N, N), dtype=complex)
    k = bin(x & z).count("1")
    for b in range(N):
        M[b ^ x, b] = (1j ** k) * (-1) ** bin(b & z).count("1")
    return M


def gate(n, kind, q0, q1):
    N = 1 << n
    M = np.zeros((N, N), dtype=complex)
    for b in range(N):
        if kind == "x":
            M[b ^ (1 << q0), b] = 1
        elif kind == "cx":
            M[b ^ (1 << q1) if b >> q0 & 1 else b, b] = 1
        elif kind == "cz":
            M[b, b] = -1 if (b >> q0 & 1) and (b >> q1 & 1) else 1
    if kind == "h":
        h = np.array([[1, 1], [1, -1]]) / math.sqrt(2)
        M = np.eye(1)
        for q in reversed(range(n)):
            M = np.kron(M, h if q == q0 else np.eye(2))
    return M


def state(n, ops, init, theta):
    N = 1 << n
    psi = np.zeros(N, dtype=complex)
    psi[init] = 1
    for kind, q0, q1, p, scale, x, z in ops:
        a = scale * theta[p] if kind in ("ry", "rz", "pr") else 0
        if kind == "ry":
            P = pauli(n, 1 << q0, 1 << q0)            # Y
            U = math.cos(a / 2) * np.eye(N) - 1j * math.sin(a / 2) * P
        elif kind == "rz":
            P = pauli(n, 0, 1 << q0)
            U = math.cos(a / 2) * np.eye(N) - 1j * math.sin(a / 2) * P
        elif kind == "pr":
            P = pauli(n, x, z)
            U = math.cos(a) * np.eye(N) - 1j * math.sin(a) * P
        else:
            U = gate(n, kind, q0, q1)
        psi = U @ psi
    return psi


def energy(n, terms, const, ops, init, theta):
    psi = state(n, ops, init, theta)
    H = const * np.eye(1 << n, dtype=complex)
    for x, z, c in terms:
        H += c * pauli(n, x, z)
    return float(np.real(psi.conj() @ H @ psi))


def random_problem(r, n):
    N = 1 << n
    terms = []
    xs = [0] + [r.randrange(1, N) for _ in range(4)]
    for _ in range(r.randrange(6, 30)):
        x = r.choice(xs)                                 # shared X masks: groups of several terms
        z = r.randrange(N)
        if bin(x & z).count("1") % 2 and r.random() < 0.5:
            z ^= x & -x                                  # both parities of Y counts
        terms.append((x, z, r.uniform(-1, 1)))
    params = r.randrange(2, 6)
    ops = []
    for _ in range(r.randrange(8, 24)):
        kind = r.choice(["x", "h", "cx", "cz", "ry", "rz", "pr", "pr", "pr"])
        q0 = r.randrange(n)
        q1 = r.choice([q for q in range(n) if q != q0])
        p = r.randrange(params)
        if kind == "pr" and r.random() < 0.6:            # a run sharing the parameter and the X mask
            x = r.randrange(1, N)
            zs = [r.randrange(N)]
            for _ in range(r.randrange(1, 8)):
                z = r.randrange(N)
                if r.random() < 0.7:                     # mostly commuting (x . (z ^ z0) even), sometimes not
                    while bin(x & (z ^ zs[0])).count("1") % 2:
                        z = r.randrange(N)
                zs.append(z)
            for z in zs:
                ops.append(("pr", 0, 0, p, r.choice([0.5, -0.5, 0.125, -0.125, 0.3]), x, z))
            continue
        ops.append((kind, q0, q1, p, r.uniform(-1.5, 1.5) if kind in ("ry", "rz", "pr") else 1.0,
                    r.randrange(1, N) if kind == "pr" else 0, r.randrange(N) if kind == "pr" else 0))
    theta = [r.uniform(-2, 2) for _ in range(params)]
    return terms, r.uniform(-1, 1), ops, r.randrange(N), params, theta


def c_tables(name, n, terms, const, ops, init, params):
    t = ",\n".join(f"{{{x:#x}ULL, {z:#x}ULL, {c!r}}}" for x, z, c in terms)
    o = ",\n".join(f"{{{KIND[k]}, {q0}, {q1}, {p}, 0, {s!r}, {x:#x}ULL, {z:#x}ULL}}" for k, q0, q1, p, s, x, z in ops)
    return (f"static const qarm_pauli {name}_t[] = {{{t}}};\n"
            f"static const qarm_hamiltonian {name}_h = {{{n}, {len(terms)}, {const!r}, {name}_t}};\n"
            f"static const qarm_op {name}_o[] = {{{o}}};\n"
            f"static const qarm_ansatz {name}_a = {{{n}, {params}, {len(ops)}, 0, {init}ULL, {name}_o}};\n")


MAIN = r"""
static double run(const qarm_hamiltonian *h, const qarm_ansatz *a, const double *th, double *g)
{
    static double buf[6][1 << 16];
    qarm_sv s = {(int)h->qubits, buf[0], buf[1], buf[2], buf[3], buf[4], buf[5]};
    qarm_sv_prepare(&s, a, th);
    double e = qarm_sv_energy(&s, h);
    double e2 = qarm_sv_grad(&s, h, a, th, g);
    if (fabs(e - e2) > 1e-12) printf("energy %.17g != %.17g\n", e, e2);
    return e;
}
"""


def main():
    cc = next((c for c in ("gcc", "clang", "cc") if shutil.which(c)), None)
    if cc is None:
        print("skipped: no C compiler")
        return
    lm = [] if os.name == "nt" else ["-lm"]
    r = random.Random(7)
    problems = [random_problem(r, n) for n in (3, 4, 5, 6, 7, 8, 5, 6, 4, 8)]
    with tempfile.TemporaryDirectory() as d:
        src = ['#include <stdio.h>\n#include <math.h>\n#include "qarm.h"\n#include "qarm_sv.h"\n', MAIN]
        body = ["int main(void)\n{\n    double g[8];\n"]
        for k, (terms, const, ops, init, params, theta) in enumerate(problems):
            n = [3, 4, 5, 6, 7, 8, 5, 6, 4, 8][k]
            src.append(c_tables(f"p{k}", n, terms, const, ops, init, params))
            th = ", ".join(repr(v) for v in theta)
            body.append(f"    {{ static const double th[] = {{{th}}}; double e = run(&p{k}_h, &p{k}_a, th, g);\n"
                        f"      printf(\"%d %.15g\", {k}, e); for (int i = 0; i < {params}; i++) printf(\" %.15g\", g[i]);"
                        f" printf(\"\\n\"); }}\n")
        body.append("    return 0;\n}\n")
        path = os.path.join(d, "sv.c")
        open(path, "w").write("".join(src + body))
        exe = os.path.join(d, "sv" + (".exe" if os.name == "nt" else ""))
        subprocess.run([cc, "-O2", "-iquote", INC, path, "-o", exe] + lm, check=True)
        out = subprocess.run([exe], capture_output=True, text=True, check=True).stdout.split("\n")
        worst_e = worst_g = worst_fd = 0
        fused = 0
        for k, line in enumerate(l for l in out if l):
            terms, const, ops, init, params, theta = problems[k]
            n = [3, 4, 5, 6, 7, 8, 5, 6, 4, 8][k]
            vals = [float(v) for v in line.split()[1:]]
            e, g = vals[0], vals[1:]
            ref = energy(n, terms, const, ops, init, theta)
            worst_e = max(worst_e, abs(e - ref))
            for p in range(params):
                h = 1e-5
                tp, tm = list(theta), list(theta)
                tp[p] += h
                tm[p] -= h
                fd = (energy(n, terms, const, ops, init, tp) - energy(n, terms, const, ops, init, tm)) / (2 * h)
                worst_fd = max(worst_fd, abs(g[p] - fd))
            fused += sum(1 for a, b in zip(ops, ops[1:]) if a[0] == b[0] == "pr" and a[3] == b[3] and a[5] == b[5])
        assert worst_e < 1e-10 and worst_fd < 1e-6, (worst_e, worst_fd)
        print(f"1) {len(problems)} random problems, 3 to 8 qubits ({fused} Pauli rotations sharing a parameter and an X mask "
              f"with the one before): the energy within {worst_e:.1e} of dense matrices, the gradient within {worst_fd:.1e} "
              "of central differences")

        r2 = random.Random(3)
        a_ = random_problem(r2, 6)
        b_ = random_problem(r2, 6)
        src2 = ['#include <stdio.h>\n#include <math.h>\n#include "qarm.h"\n#include "qarm_sv.h"\n', MAIN,
                c_tables("a", 6, a_[0], a_[1], a_[2], a_[3], a_[4]), c_tables("b", 6, b_[0], b_[1], b_[2], b_[3], b_[4])]
        tha, thb = ", ".join(repr(v) for v in a_[5]), ", ".join(repr(v) for v in b_[5])
        src2.append("int main(void) { double g[8]; static const double ta[] = {%s}, tb[] = {%s};\n"
                    "  for (int i = 0; i < 3; i++) printf(\"%%.15g %%.15g\\n\", run(&a_h, &a_a, ta, g), run(&b_h, &b_a, tb, g));"
                    " return 0; }\n" % (tha, thb))
        path = os.path.join(d, "sv2.c")
        open(path, "w").write("".join(src2))
        subprocess.run([cc, "-O2", "-iquote", INC, path, "-o", exe] + lm, check=True)
        lines = subprocess.run([exe], capture_output=True, text=True, check=True).stdout.split()
        ea, eb = energy(6, *a_[:4], a_[5]), energy(6, *b_[:4], b_[5])
        assert all(abs(float(v) - (ea if i % 2 == 0 else eb)) < 1e-10 for i, v in enumerate(lines)), (lines, ea, eb)
        print("2) two Hamiltonians in turn, three times: each energy is that of its own table")

        omp = subprocess.run([cc, "-fopenmp", "-O2", "-iquote", INC, "-x", "c", "-", "-o", os.path.join(d, "probe" + (".exe" if os.name == "nt" else ""))],
                             input="#include <omp.h>\nint main(void){return omp_get_max_threads() < 1;}\n",
                             capture_output=True, text=True).returncode == 0
        if not omp:
            print("3) skipped: no OpenMP in this compiler")
        else:
            r3 = random.Random(5)
            big = random_problem(r3, 16)
            src3 = ['#include <stdio.h>\n#include <math.h>\n#include "qarm.h"\n#include "qarm_sv.h"\n', MAIN,
                    c_tables("q", 16, *big[:5])]
            th = ", ".join(repr(v) for v in big[5])
            src3.append("int main(void) { double g[8]; static const double t[] = {%s}; double e = run(&q_h, &q_a, t, g);\n"
                        "  printf(\"%%.17g\", e); for (int i = 0; i < %d; i++) printf(\" %%.17g\", g[i]); printf(\"\\n\");"
                        " return 0; }\n" % (th, big[4]))
            path = os.path.join(d, "sv3.c")
            open(path, "w").write("".join(src3))
            subprocess.run([cc, "-O2", "-fopenmp", "-iquote", INC, path, "-o", exe] + lm, check=True)
            res = {}
            for t in (1, 4):
                o = subprocess.run([exe], capture_output=True, text=True, check=True,
                                   env=dict(os.environ, OMP_NUM_THREADS=str(t))).stdout.split()
                res[t] = [float(v) for v in o]
            diff = max(abs(a - b) for a, b in zip(res[1], res[4]))
            assert diff < 1e-12, (res, diff)
            print(f"3) OpenMP, 16 qubits: 1 and 4 threads agree within {diff:.1e}")
    print("PASS")


if __name__ == "__main__":
    main()
