# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Quantag IT Solutions GmbH
"""QEVOLVE (docs/qarm_isa.md 6.6, ISA 1.1): its error bounds are proven, so they must hold on every case.

  1. include/qarm_sv.h's plans (tests/qarm_evolve_driver.c, the host's compiler) on random Hamiltonians of 1 to 4
     qubits against exp(-iHt) from numpy: exact, every product formula with fixed steps, the runtime's choice for
     eps, qDRIFT within its bound plus 5 standard errors. With steps = 0 the bound meets eps. The commutator bounds
     at small t, where they are tightest.
  2. qarm_evolve.py (qarm_exec.py's) plans the same: method, order, steps, and the bound to 1e-9.
  3. The refusals of section 5.7, and an eps no formula reaches: refused, never a weaker answer. Extreme requests
     (times up to 1.7e308, eps from 1e-12 to 1e300, fixed steps up to 1e9, every method): both planners refuse or
     plan the same, without an exception; qDRIFT for t = 400 at eps 1e-3 is refused for its work, and a feasible
     qDRIFT request gets a finite bound within eps.
  4. examples/qarm/quench.c on this CPU (the library's classical path); under qarm_exec.py (arm64 compiler and
     Unicorn); under the QEMU plugin (QARM_QEMU as for test_qarm_qemu) on libqarm's exact backend, and on its
     simulator backend, where the product formula is one QEXPECTN batch.
Run: python3 -m tests.test_qarm_evolve"""
import os, shutil, subprocess, sys, tempfile
import numpy as np
from tests.test_qarm_exec import ROOT, sh, toolchain

INC = os.path.join(ROOT, "include")


def pauli(x, z, n):
    X = np.array([[0, 1], [1, 0]], complex)
    Z = np.diag([1.0, -1.0]).astype(complex)
    m = np.eye(1, dtype=complex)
    for q in reversed(range(n)):
        bx, bz = x >> q & 1, z >> q & 1
        m = np.kron(m, X @ Z if bx and bz else X if bx else Z if bz else np.eye(2))
    return 1j ** (bin(x & z).count("1") & 3) * m


def dense(terms, c, n):
    return sum((cc * pauli(x, z, n) for x, z, cc in terms), c * np.eye(2 ** n, dtype=complex))


def exact_values(n, H, O, init, ts):
    w, v = np.linalg.eigh(dense(H[0], H[1], n))
    od = dense(O[0], O[1], n)
    psi = np.zeros(2 ** n, complex)
    psi[init] = 1
    out = []
    for t in ts:
        phi = v @ (np.exp(-1j * w * t) * (v.conj().T @ psi))
        out.append(float(np.real(np.vdot(phi, od @ phi))))
    return out


class Driver:
    def __init__(self, exe):
        self.exe = exe

    def __call__(self, n, H, O, init, ts, method, order=0, steps=0, eps=0.0, shots=0, exact_ok=1, plan_only=0):
        lines = [str(n)]
        for terms, c in (H, O):
            lines += [str(len(terms))] + [f"{x} {z} {cc!r}" for x, z, cc in terms] + [repr(c)]
        lines += [f"{init} {len(ts)}", " ".join(map(repr, ts)), f"{method} {order} {steps} {eps!r} {shots} {exact_ok} {plan_only}"]
        out = sh([self.exe], input="\n".join(lines) + "\n").stdout.split("\n")
        if out[0].startswith("refused"):
            return out[0][8:], None, None, None
        _, m, o, st, b = out[0].split()
        if plan_only:
            return (int(m), int(o), int(st), float(b)), None, [], None
        return (int(m), int(o), int(st), float(b)), float(out[1].split()[1]), [float(v) for v in out[2:2 + len(ts)]], None


def random_case(rng):
    n = int(rng.integers(1, 5))
    H = ([(int(rng.integers(0, 2 ** n)), int(rng.integers(0, 2 ** n)), float(rng.normal())) for _ in range(int(rng.integers(1, 6)))],
         float(rng.normal()))
    O = ([(int(rng.integers(0, 2 ** n)), int(rng.integers(0, 2 ** n)), float(rng.normal())) for _ in range(2)], float(rng.normal()))
    return n, H, O, int(rng.integers(0, 2 ** n)), [float(v) for v in rng.uniform(-3, 3, 4)] + [0.0]


def python_plan(n, H, O, init, ts, method, order=0, steps=0, eps=0.0, shots=0, exact_ok=1):
    sys.path.insert(0, ROOT)
    import qarm_evolve as qe
    e = qe.Evolution(qe.Ham(n, H[1], H[0]), qe.Ham(n, O[1], O[0]), ts, method, order, shots, init, eps, steps, 0)
    why = qe.check(e)
    if why:
        return why
    p = qe.plan(e, exact_ok)
    return p if isinstance(p, str) else (p.method, p.order, p.steps, p.bound)


def main():
    cc = next((c for c in ("gcc", "clang", "cc") if shutil.which(c)), None)
    if cc is None:
        print("skipped: no C compiler")
        return
    with tempfile.TemporaryDirectory() as d:
        exe = os.path.join(d, "driver" + (".exe" if os.name == "nt" else ""))
        flags = ["-D__USE_MINGW_ANSI_STDIO=1"] if os.name == "nt" else []
        sh([cc, "-O2", "-std=c99", "-Wall", "-Wextra", "-Werror", "-Wno-unused-function", "-iquote", INC] + flags +
           [os.path.join(ROOT, "tests", "qarm_evolve_driver.c"), "-o", exe, "-lm"])
        run = Driver(exe)

        # 1. the bounds hold
        rng = np.random.default_rng(2026)
        plans = [(1, 0, 0, 0.0), (2, 1, 5, 0.0), (2, 2, 3, 0.0), (2, 4, 2, 0.0), (2, 6, 1, 0.0), (2, 0, 0, 1e-2),
                 (2, 1, 0, 1e-3), (2, 2, 0, 1e-4), (0, 0, 0, 0.0)]
        worst, cases, agree = 0.0, [], 0
        for _ in range(60):
            n, H, O, init, ts = random_case(rng)
            ex = exact_values(n, H, O, init, ts)
            for m, o, st, eps in plans:
                plan, se, vals, _ = run(n, H, O, init, ts, m, o, st, eps)
                assert vals is not None, plan
                err = max(abs(a - b) for a, b in zip(vals, ex))
                assert err <= plan[3] + 1e-9, (n, H, O, init, ts, m, o, st, eps, err, plan)
                if eps:
                    assert plan[3] <= eps, (plan, eps)
                if plan[3] > 1e-12:
                    worst = max(worst, err / plan[3])
                cases.append((n, H, O, init, ts, m, o, st, eps, plan))
            plan, se, vals, _ = run(n, H, O, init, ts, 3, 0, 200, 0.0, 400)
            err = max(abs(a - b) for a, b in zip(vals, ex))
            assert plan[0] == 3 and err <= plan[3] + 5 * se + 1e-9, (err, plan, se)
        tight = 0.0
        for H in ([(1, 0, 1.0), (0, 1, 0.7)], [(1, 0, 0.9), (0, 1, -0.6), (1, 1, 0.4)], [(1, 0, 1.0), (0, 3, 0.5), (2, 0, 0.8)]):
            O = ([(0, 1, 1.0), (2, 0, 0.5)], 0.0)
            for init in range(4):
                for t in (0.05, 0.2, 0.6):
                    ex = exact_values(2, (H, 0.0), O, init, [t])[0]
                    for o in (1, 2):
                        plan, _, vals, _ = run(2, (H, 0.0), O, init, [t], 2, o, 1)
                        assert abs(vals[0] - ex) <= plan[3] + 1e-12, (H, init, t, o, vals[0], ex, plan)
                        tight = max(tight, abs(vals[0] - ex) / plan[3])
        print(f"1) {len(cases)} plans on 60 random Hamiltonians (1 to 4 qubits), and qDRIFT: every error within its "
              f"bound (the worst at {worst:.2f} of it); eps met; the commutator bounds at small t: up to {tight:.2f} of "
              "the bound")

        # 2. qarm_evolve.py plans the same
        for n, H, O, init, ts, m, o, st, eps, plan in cases:
            p = python_plan(n, H, O, init, ts, m, o, st, eps)
            assert not isinstance(p, str) and p[:3] == plan[:3] and abs(p[3] - plan[3]) <= 1e-9 * max(1.0, plan[3]), (p, plan)
            agree += 1
        print(f"2) qarm_evolve.py: the same method, order, steps and bound in all {agree} plans")

        # 3. refusals
        ising = ([(0, 3, 0.8), (1, 0, 0.5), (2, 0, 0.5)], 0.3)
        mag = ([(0, 1, 1.0), (2, 0, 0.25)], 1.0)
        bad = [((2, ([(4, 0, 1.0)], 0.0), mag, 0, [1.0], 0), "a term on qubit 2 of 2"),
               ((2, ising, mag, 4, [1.0], 0), "init 4"),
               ((2, ising, mag, 0, [1.0], 4), "method 4"),
               ((2, ising, mag, 0, [1.0], 2, 3), "order 3"),
               ((2, ising, mag, 0, [1.0], 1, 2), "an order for exact"),
               ((2, ising, mag, 0, [1.0], 0, 0, 0, -1.0), "eps -1"),
               ((2, ising, mag, 0, [float("nan")], 0), "a NaN time"),
               ((2, ising, mag, 0, [1.0], 2, 1, 0, 1e-300), "eps 1e-300 by Lie-Trotter"),
               ((2, ising, mag, 0, [1.0], 1, 0, 0, 0.0, 0, 0), "exact on a measured backend")]
        for args, what in bad:
            why, _, vals, _ = run(*args)
            assert vals is None, (what, why)
            p = python_plan(*args)
            assert isinstance(p, str), (what, p)
        # extreme requests: the same disposition from both planners, and no exception
        two, z1 = ([(1, 0, 1.0), (0, 3, 2.0)], 0.0), ([(0, 1, 1.0)], 0.0)
        extreme = 0
        for m in range(4):
            for t in (1e-3, 1.0, 400.0, 1e8, 1e150, 1e300, 1.7e308):
                for eps in (0.0, 1e-3, 1e-12, 1e300):
                    for st in (0, 1, 10 ** 9):
                        for ok in (1, 0):
                            plan, _, _, _ = run(2, two, z1, 0, [t, -t], m, 0, st, eps, 0, ok, 1)
                            p = python_plan(2, two, z1, 0, [t, -t], m, 0, st, eps, 0, ok)
                            if isinstance(plan, str):
                                assert isinstance(p, str), (m, t, eps, st, ok, plan, p)
                            else:
                                assert not isinstance(p, str) and p[:3] == plan[:3], (m, t, eps, st, ok, plan, p)
                                assert p[3] == plan[3] or abs(p[3] - plan[3]) <= 1e-9 * max(1.0, plan[3]), (plan, p)
                            extreme += 1
        x1 = ([(1, 0, 1.0)], 0.0)
        why, _, _, _ = run(1, x1, z1, 0, [400.0], 3, 0, 0, 1e-3)
        p = python_plan(1, x1, z1, 0, [400.0], 3, 0, 0, 1e-3)
        assert isinstance(why, str) and "work" in why and isinstance(p, str) and "work" in p, (why, p)
        plan, se, vals, _ = run(1, x1, z1, 0, [1.0], 3, 0, 0, 1e-2)
        assert plan[0] == 3 and 0 < plan[3] <= 1e-2, plan
        p = python_plan(1, x1, z1, 0, [1.0], 3, 0, 0, 1e-2)
        assert p[:3] == plan[:3] and abs(p[3] - plan[3]) <= 1e-12, (p, plan)
        print(f"3) {len(bad)} tables refused, by both planners: bad masks, init, method, order, eps, a NaN time; an eps "
              f"no formula reaches in 10^12 steps; exact evolution where only measurements exist. {extreme} extreme "
              "requests: the same disposition from both, no exception; qDRIFT for t = 400 refused for its work, for "
              f"t = 1 planned with {plan[2]} samples, bound {plan[3]:.4g} <= eps 1e-2")

        # 4. quench.c on every runtime here
        src = os.path.join(ROOT, "examples", "qarm", "quench.c")
        host = os.path.join(d, "quench" + (".exe" if os.name == "nt" else ""))
        sh([cc, "-O1", "-iquote", INC, src, "-o", host, "-lm"])
        p = sh([host])
        want = ["on this CPU", "1. the runtime's choice: exact", "2. eps 1e-2: order 2, 273 steps, bound 0.0099"]
        assert all(w in p.stdout for w in want), p.stdout
        ran = ["this CPU"]
        tc = toolchain()
        if tc and sys.platform == "linux":
            a64 = os.path.join(d, "quench.a64")
            sh(tc[0] + ["-O1", "-iquote", INC, src, "-o", a64, "-lm"])
            try:
                import unicorn  # noqa: F401
                p = sh([sys.executable, os.path.join(ROOT, "qarm_exec.py"), a64])
                assert "on the quantum coprocessor" in p.stdout and "product formula of order 2, 273 steps" in p.stderr, \
                    p.stdout + p.stderr
                ran.append("qarm_exec.py")
            except ImportError:
                pass
            from tests.test_qarm_qemu import find_qemu
            q, inc, why = find_qemu()
            if not why:
                b = os.path.join(d, "qemu")
                sh(["cmake", "-S", os.path.join(ROOT, "runtime", "qemu"), "-B", b, f"-DQEMU_PLUGIN_INCLUDE={inc}"])
                sh(["cmake", "--build", b])
                plug = os.path.join(b, "libqarm-qemu.so")
                sysroot = [] if os.uname().machine in ("aarch64", "arm64") else ["-L", "/usr/aarch64-linux-gnu"]
                p = sh([q] + sysroot + ["-plugin", plug, a64])
                assert "on the quantum coprocessor" in p.stdout and "2. eps 1e-2: order 2, 273 steps" in p.stdout, p.stdout
                env = dict(os.environ, QARM_BACKEND="simulator", QCPU_SEED="3")
                p = sh([q] + sysroot + ["-plugin", plug, a64], env=env, check=False)
                assert "QEXPECTN: 13 energies (simulator)" in p.stderr and "1. the runtime's choice: order" in p.stdout, \
                    p.stdout + p.stderr
                ran.append("the QEMU plugin (exact; simulator: one QEXPECTN batch per call)")
        print("4) examples/qarm/quench.c on " + ", ".join(ran))
    print("PASS")


if __name__ == "__main__":
    main()
