# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Quantag IT Solutions GmbH
"""QLINDBLAD (docs/qarm_isa.md 6.7, ISA 1.2), and QEVOLVE's qDRIFT on measured backends (6.6).

  1. include/qarm_sv.h (tests/qarm_lindblad_driver.c) on random Lindblad equations of 1 to 3 qubits with Pauli
     jump operators, against e^{tL} of the full Liouvillian from numpy: exact on the density matrix, Lie-Trotter
     and Strang with fixed steps, the runtime's choice for eps, and trajectories within the bound plus 5 standard
     errors. With steps = 0 the bound meets eps.
  2. qarm_evolve.py (qarm_exec.py's) plans the same and computes the same values (to 1e-9 on the density matrix).
  3. The refusals of section 5.8, by both.
  4. qDRIFT on measured backends: libqarm's simulator backend under the QEMU plugin (QARM_QEMU as for
     test_qarm_qemu), and qarm_exec.py's simulator backend when Qiskit Aer is installed: R random circuits, the
     mean within the bound plus 5 standard errors of the exact values.
  5. examples/qarm/decoherence.c: the same table on this CPU, under qarm_exec.py (arm64) and the QEMU plugin
     (arm64; x86-64 on an x86-64 host).
Run: python3 -m tests.test_qarm_lindblad"""
import math, os, shutil, subprocess, sys, tempfile
import numpy as np
from tests.test_qarm_exec import ROOT, sh, toolchain
from tests.test_qarm_evolve import pauli, dense

INC = os.path.join(ROOT, "include")


def expm(A):
    """scaling and squaring with a 30-term Taylor series: enough for these small, well-scaled matrices"""
    s = max(0, int(np.ceil(np.log2(max(np.abs(A).sum(axis=1).max(), 1e-300)))) + 1)
    B, E = A / 2 ** s, np.eye(len(A), dtype=complex)
    T = E.copy()
    for k in range(1, 30):
        T = T @ B / k
        E = E + T
    for _ in range(s):
        E = E @ E
    return E


def exact(n, H, O, J, init, ts):
    N = 2 ** n
    Hd, I = dense(H[0], H[1], n), np.eye(N)
    L = -1j * (np.kron(Hd, I) - np.kron(I, Hd.T))
    for x, z, g in J:
        P = pauli(x, z, n)
        L += g * (np.kron(P, P.conj()) - np.eye(N * N))
    r0 = np.zeros((N, N), complex)
    r0[init, init] = 1
    Od = dense(O[0], O[1], n)
    return [float(np.real(np.trace(Od @ (expm(L * t) @ r0.reshape(-1)).reshape(N, N)))) for t in ts]


class Driver:
    def __init__(self, exe):
        self.exe = exe

    def __call__(self, n, H, O, J, init, ts, method, order=0, steps=0, eps=0.0, shots=0, traj=0):
        L = [str(n)]
        for terms, c in (H, O):
            L += [str(len(terms))] + [f"{x} {z} {cc!r}" for x, z, cc in terms] + [repr(c)]
        L += [str(len(J))] + [f"{x} {z} {g!r}" for x, z, g in J]
        L += [f"{init} {len(ts)}", " ".join(map(repr, ts)), f"{method} {order} {steps} {eps!r} {shots} {traj}"]
        out = sh([self.exe], input="\n".join(L) + "\n").stdout.split("\n")
        if out[0].startswith("refused"):
            return out[0][8:], None, None
        f = out[0].split()
        return ((int(f[1]), int(f[2]), int(f[3]), float(f[4]), int(f[5]), int(f[6])), float(out[1].split()[1]),
                [float(v) for v in out[2:2 + len(ts)]])


def python(n, H, O, J, init, ts, method, order=0, steps=0, eps=0.0, shots=0):
    sys.path.insert(0, ROOT)
    import qarm_evolve as qe
    e = qe.Evolution(qe.Ham(n, H[1], H[0]), qe.Ham(n, O[1], O[0]), ts, method, order, shots, init, eps, steps, 0)
    l = qe.Lindblad(e, qe.Ham(n, 0.0, J))
    why = qe.lb_check(l)
    if why:
        return why, None
    p = qe.lb_plan(l, True)
    if isinstance(p, str):
        return p, None
    return p, qe.lb_run(l, p, 1)[0]


def main():
    cc = next((c for c in ("gcc", "clang", "cc") if shutil.which(c)), None)
    if cc is None:
        print("skipped: no C compiler")
        print("PASS")
        return
    with tempfile.TemporaryDirectory() as d:
        exe = os.path.join(d, "driver" + (".exe" if os.name == "nt" else ""))
        flags = ["-D__USE_MINGW_ANSI_STDIO=1"] if os.name == "nt" else []
        sh([cc, "-O2", "-std=c99", "-Wall", "-Wextra", "-Werror", "-Wno-unused-function", "-iquote", INC] + flags +
           [os.path.join(ROOT, "tests", "qarm_lindblad_driver.c"), "-o", exe, "-lm"])
        run = Driver(exe)

        # 1, 2
        rng = np.random.default_rng(2026)
        plans = [(0, 0, 0, 0.0, 0), (1, 0, 0, 0.0, 0), (2, 1, 4, 0.0, 0), (2, 2, 3, 0.0, 0), (2, 0, 0, 1e-2, 0),
                 (2, 1, 0, 1e-3, 0), (2, 2, 6, 0.0, 1)]
        worst, count, agree = 0.0, 0, 0
        for _ in range(30):
            n = int(rng.integers(1, 4))
            H = ([(int(rng.integers(0, 2 ** n)), int(rng.integers(0, 2 ** n)), float(rng.normal()))
                  for _ in range(int(rng.integers(1, 4)))], float(rng.normal()))
            O = ([(int(rng.integers(0, 2 ** n)), int(rng.integers(0, 2 ** n)), float(rng.normal())) for _ in range(2)],
                 float(rng.normal()))
            J = [(int(rng.integers(0, 2 ** n)), int(rng.integers(0, 2 ** n)), float(rng.uniform(0, 0.8)))
                 for _ in range(int(rng.integers(1, 4)))]
            init = int(rng.integers(0, 2 ** n))
            ts = [0.0] + [float(v) for v in rng.uniform(0, 2.5, 3)]
            ex = exact(n, H, O, J, init, ts)
            for m, o, st, eps, traj in plans:
                plan, se, vals = run(n, H, O, J, init, ts, m, o, st, eps, 400 if traj else 0, traj)
                assert vals is not None, plan
                err = max(abs(a - b) for a, b in zip(vals, ex))
                assert err <= plan[3] + 5 * se + 1e-9, (n, H, O, J, init, ts, m, o, st, eps, traj, err, plan, se)
                if eps:
                    assert plan[3] <= eps
                if plan[3] > 1e-12 and not traj:
                    worst = max(worst, err / plan[3])
                count += 1
                if not traj:
                    p, pv = python(n, H, O, J, init, ts, m, o, st, eps)
                    assert not isinstance(p, str) and (p.method, p.order, p.steps) == plan[:3], (p, plan)
                    assert abs(p.bound - plan[3]) <= 1e-9 * max(1.0, plan[3]), (p.bound, plan)
                    assert max(abs(a - b) for a, b in zip(pv, vals)) < 1e-9, (pv, vals)
                    agree += 1
        print(f"1) {count} plans on 30 random Lindblad equations (1 to 3 qubits): every value within its bound of "
              f"e^(tL) (the worst at {worst:.2f} of it; trajectories within 5 standard errors); eps met")
        print(f"2) qarm_evolve.py: the same plans and values (to 1e-9) in all {agree}")

        # 3
        H1, O1 = ([(1, 0, 1.0)], 0.0), ([(0, 1, 1.0)], 0.0)
        bad = [((1, H1, O1, [(0, 1, -0.1)], 0, [1.0], 0), "a negative rate"),
               ((1, H1, O1, [(0, 1, float("nan"))], 0, [1.0], 0), "a NaN rate"),
               ((1, H1, O1, [(2, 0, 0.1)], 0, [1.0], 0), "a jump on qubit 1 of 1"),
               ((1, H1, O1, [(0, 1, 0.1)], 0, [1.0], 2, 4), "order 4"),
               ((1, H1, O1, [(0, 1, 0.1)], 0, [1.0], 3), "qDRIFT"),
               ((1, H1, O1, [(0, 1, 0.1)], 0, [-1.0], 0), "a negative time"),
               ((1, H1, O1, [(0, 1, 0.1)], 0, [1.0], 2, 1, 0, 1e-300), "eps 1e-300 by Lie-Trotter"),
               ((12, ([(1, 0, 1.0)], 0.0), ([(0, 1, 1.0)], 0.0), [(0, 1, 0.1)], 0, [1.0], 1), "exact on 12 qubits")]
        for args, what in bad:
            why, _, vals = run(*args)
            assert vals is None, (what, why)
            p, _ = python(*args)
            assert isinstance(p, str), (what, p)
        print(f"3) {len(bad)} tables refused by both: negative and NaN rates, a jump beyond the qubits, order 4, "
              "qDRIFT, a negative time, an eps no formula reaches, exact evolution beyond the density matrix")

        # 4, 5: on the runtimes
        ran4, ran5 = [], ["this CPU"]
        src = os.path.join(ROOT, "examples", "qarm", "decoherence.c")
        host = os.path.join(d, "deco")
        sh([cc, "-O1", "-iquote", INC, src, "-o", host, "-lm"])
        want = sh([host]).stdout
        rows = [l for l in want.splitlines() if l[:1] == " " and l.strip()[:1].isdigit()]
        assert len(rows) == 9 and "Strang" in want, want
        qd = os.path.join(ROOT, "tests", "qarm_qdrift_measured.c")
        tc = toolchain()
        if tc and sys.platform == "linux":
            a64, q64 = os.path.join(d, "deco.a64"), os.path.join(d, "qd.a64")
            sh(tc[0] + ["-O1", "-iquote", INC, src, "-o", a64, "-lm"])
            sh(tc[0] + ["-O1", "-iquote", INC, qd, "-o", q64, "-lm"])
            try:
                import unicorn  # noqa: F401
                p = sh([sys.executable, os.path.join(ROOT, "qarm_exec.py"), "-q", a64])
                assert [l for l in p.stdout.splitlines() if l in rows] == rows, p.stdout
                ran5.append("qarm_exec.py")
                try:
                    import qiskit_aer  # noqa: F401
                    p = sh([sys.executable, os.path.join(ROOT, "qarm_exec.py"), "--backend", "simulator", q64],
                           env=dict(os.environ, QCPU_SEED="5"))
                    assert "within the bound" in p.stdout and "circuits" in p.stderr, p.stdout + p.stderr
                    ran4.append("qarm_exec.py's simulator (Qiskit Aer)")
                except ImportError:
                    pass
            except ImportError:
                pass
            from tests.test_qarm_qemu import find_qemu
            q, inc, why = find_qemu()
            if not why:
                qb = os.path.join(d, "qemu")
                sh(["cmake", "-S", os.path.join(ROOT, "runtime", "qemu"), "-B", qb, f"-DQEMU_PLUGIN_INCLUDE={inc}"])
                sh(["cmake", "--build", qb])
                plug = os.path.join(qb, "libqarm-qemu.so")
                sysroot = [] if os.uname().machine == "aarch64" else ["-L", "/usr/aarch64-linux-gnu"]
                p = sh([q] + sysroot + ["-plugin", plug, a64])
                assert [l for l in p.stdout.splitlines() if l in rows] == rows and "QLINDBLAD" in p.stderr, p.stdout + p.stderr
                ran5.append("QEMU (arm64)")
                p = sh([q] + sysroot + ["-plugin", plug, q64], env=dict(os.environ, QARM_BACKEND="simulator", QCPU_SEED="5"))
                assert "within the bound" in p.stdout and "one circuit each" in p.stderr, p.stdout + p.stderr
                ran4.append("libqarm's simulator backend (QEMU)")
                q86 = os.path.join(os.path.dirname(q), "qemu-x86_64")
                if os.uname().machine == "x86_64" and os.path.exists(q86):
                    p = sh([q86, "-plugin", plug, host])
                    assert [l for l in p.stdout.splitlines() if l in rows] == rows, p.stdout
                    ran5.append("QEMU (x86-64)")
        print("4) qDRIFT on measured backends: " + (", ".join(ran4) if ran4 else "skipped (no QEMU, no Qiskit Aer)")
              + ": the mean of the random circuits within the bound plus 5 standard errors")
        print("5) examples/qarm/decoherence.c: the same table on " + ", ".join(ran5))
    print("PASS")


if __name__ == "__main__":
    main()
