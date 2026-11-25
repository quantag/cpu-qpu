# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Quantag IT Solutions GmbH
"""qcpu_search(): Grover's search with a __qcpu__ predicate as the oracle (docs/qcpu_spec.md 5.5.3).

  1. examples/codebreaker/solver.c built with --qcpu-mode qoracle: Grover's search finds the secret BYRG
     with every seed; the oracle calls stay far below a classical search (one solution in 256 codes).
  2. The same program with QCPU_MODE=native, built in qcpu mode, and built by a plain C compiler: a
     classical search, the same answer.
  3. Several solutions (any one is a right answer), no solution (0, reported), a solution at x = 0.
  4. Errors: bits other than the declared QCPU_BITS; QCPU_MODE=qoracle without a phase oracle.
Run: python3 -m tests.test_qcpu_search"""
import os, re, statistics, subprocess, sys, tempfile, textwrap
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import qcpucc

QCPUCC = [sys.executable, os.path.join(ROOT, "qcpucc.py")]
SOLVER = os.path.join(ROOT, "examples", "codebreaker", "solver.c")
RT = os.path.join(ROOT, "runtime")


def sh(cmd, check=True, env=None):
    p = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if check and p.returncode:
        raise AssertionError(" ".join(cmd) + "\n" + p.stdout + p.stderr)
    return p


def env(**kw):
    return dict(os.environ, **kw)


def main():
    try:
        import qoracle  # noqa: F401
    except ImportError:
        print("skipped: qoracle is not installed")
        return
    host_cc = (os.environ.get("CC") or qcpucc.default_cc()).split()
    with tempfile.TemporaryDirectory() as d:
        # 1. Grover's search for the Code Breaker secret
        exe = os.path.join(d, "solver")
        out = sh(QCPUCC + ["--qcpu-mode", "qoracle", SOLVER, "-o", exe]).stderr
        assert "consistent: phase oracle for qcpu_search(), 1044 qubits, 4255 gates" in out, out
        calls = []
        for seed in range(1, 41):
            p = sh([exe], env=env(QCPU_SEED=str(seed)))
            assert "the secret code: BYRG" in p.stdout, (seed, p.stdout)
            m = re.search(r"Grover's search: (\d+) oracle call\(s\) in (\d+) round\(s\) over 256 inputs; consistent\(78\) = 1 checked",
                          p.stdout)
            assert m, p.stdout
            calls.append(int(m.group(1)) + int(m.group(2)))          # oracle calls plus the checking calls
        mean = statistics.mean(calls)
        assert mean < 64 and max(calls) < 128, calls
        print(f"1) Code Breaker solver, --qcpu-mode qoracle: Grover's search finds BYRG with all 40 seeds; oracle and "
              f"checking calls: mean {mean:.1f}, at most {max(calls)} (classical search: 128.5 on average for one code "
              "in 256)")

        # 2. the classical searches
        p = sh([exe], env=env(QCPU_MODE="native"))
        assert "BYRG" in p.stdout and "[classical search: 79 call(s) of consistent]" in p.stdout, p.stdout
        q = os.path.join(d, "solver_qcpu")
        sh(QCPUCC + [SOLVER, "-o", q])
        p = sh([q])
        assert "BYRG" in p.stdout and "[classical search: 79 call(s) of consistent]" in p.stdout, p.stdout
        plain = os.path.join(d, "plain")
        sh(host_cc + ["-I", RT, SOLVER, "-o", plain])
        assert "the secret code: BYRG" in sh([plain]).stdout
        print("2) QCPU_MODE=native, a --qcpu-mode qcpu build (each call on the quantum CPU) and a plain C compiler: "
              "a classical search, the same code")

        # 3. several solutions, none, x = 0
        src = os.path.join(d, "preds.c")
        open(src, "w").write(textwrap.dedent("""\
            #include <stdio.h>
            #include <stdlib.h>
            #include <qcpu.h>
            __qcpu__ int QCPU_RESULT_BITS(1) three_mod_four(int QCPU_BITS(6) x) { return (x & 3) == 3; }
            __qcpu__ int QCPU_RESULT_BITS(1) square_is_two(int QCPU_BITS(6) x) { return (x * x) % 64 == 2; }
            __qcpu__ int QCPU_RESULT_BITS(1) is_zero(int QCPU_BITS(5) x) { return x == 0; }
            int main(int argc, char **argv) {
                int x = -1, r;
                qcpu_call_info i;
                if (atoi(argv[1]) == 0) { r = qcpu_search(three_mod_four, 6, &x); }
                else if (atoi(argv[1]) == 1) { r = qcpu_search(square_is_two, 6, &x); }
                else if (atoi(argv[1]) == 2) { r = qcpu_search(is_zero, 5, &x); }
                else { r = qcpu_search(is_zero, 6, &x); }
                qcpu_last_call(&i);
                printf("%d %d %s %ld\\n", r, x, i.mode, i.cycles);
                return 0;
            }
            """))
        exe = os.path.join(d, "preds")
        sh(QCPUCC + ["--qcpu-mode", "qoracle", src, "-o", exe])
        for seed in range(1, 11):
            r, x, mode, _ = sh([exe, "0"], env=env(QCPU_SEED=str(seed))).stdout.split()
            assert r == "1" and int(x) % 4 == 3 and mode == "grover", (seed, r, x, mode)
            r, x, mode, _ = sh([exe, "2"], env=env(QCPU_SEED=str(seed))).stdout.split()
            assert (r, x, mode) == ("1", "0", "grover"), (seed, r, x, mode)
        r, x, mode, n = sh([exe, "1"], env=env(QCPU_SEED="5")).stdout.split()
        assert (r, x, mode) == ("0", "-1", "grover") and int(n) <= 4.5 * 8 + 4 + 8, (r, x, mode, n)   # stops after about 4.5 sqrt(N) calls
        print("3) several solutions (x mod 4 = 3): a right one every time; x = 0 is found; no solution "
              f"(x * x mod 64 = 2): 0 after {n} oracle calls")

        # 4. errors
        p = sh([exe, "3"], check=False)
        assert p.returncode == 70 and "qcpu_search(is_zero): QCPU_BAD_ARGS: its argument is declared QCPU_BITS(5)" in p.stderr, p.stderr
        q = os.path.join(d, "preds_qcpu")
        sh(QCPUCC + [src, "-o", q])
        p = sh([q, "0"], env=env(QCPU_MODE="qoracle"), check=False)
        assert p.returncode == 70 and "three_mod_four has no phase oracle in this program" in p.stderr, p.stderr
        print("4) errors: bits other than the declared QCPU_BITS; QCPU_MODE=qoracle in a program without phase oracles")
    print("PASS")


if __name__ == "__main__":
    main()
