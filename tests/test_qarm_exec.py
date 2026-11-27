# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Quantag IT Solutions GmbH
"""Q-ARM v2: one arm64 binary, run on a plain CPU and under the Q-ARM runtime (qarm_exec.py, docs/qarm_v2.md).

  1. examples/qarm/codebreaker_search.c, built for arm64 (macOS or Linux) at -O1: on the CPU (natively on an
     arm64 machine, or under qemu-aarch64) the classical search finds BYRG; the same file under qarm_exec.py
     runs QSEARCH: Grover's search with an oracle built from consistent()'s machine code, BYRG, checked.
  2. A second program: printf formats, puts, putchar, an exit code; QSEARCH with several solutions (any one is
     right), with none (0), and at 12 bits; QAVAIL.
  3. Errors: a predicate that calls another function (exit 70, the reason); a missing library function.
     3b. QMIN and QCOUNT: examples/qarm/partition.c (number partitioning), oracles from imbalance()'s and
     nearly_balanced()'s machine code; on the plain CPU every split is tried.
     3c. QPERIOD, QSIMON, QBV: examples/qarm/shor.c (factoring 15 and 21) and hidden.c.
     3d. QCIRC, QRAND, QCAPS: examples/qarm/ghz.c (a GHZ state, a Toffoli truth table), the circuit tables read
     from the program's memory; with 4's packages, the same binary on the noise model of Fez.
  5. The conformance program (tests/conformance/qarm_conformance.c, docs/qarm_isa.md): every test passes under
     qarm_exec.py with all 12 feature bits; on the plain CPU it reports no runtime.
  4. (with qiskit, qiskit-aer, qiskit-ibm-runtime) QSEARCH on a QPU backend from a config file:
     examples/qarm/tiny_search.c on IBM's noise model of Fez and through the "ibm" backend's local channel (the
     code path of a real device, one asynchronous job, without the login); Code Breaker's 1000-qubit oracle is
     refused as larger than the device.
Run: python3 -m tests.test_qarm_exec   (needs an arm64 C compiler: the host's on arm64, else aarch64-linux-gnu-gcc)"""
import os, platform, re, shutil, subprocess, sys, tempfile, textwrap
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXEC = [sys.executable, os.path.join(ROOT, "qarm_exec.py")]
DEMO = os.path.join(ROOT, "examples", "qarm", "codebreaker_search.c")
TINY = os.path.join(ROOT, "examples", "qarm", "tiny_search.c")


def toolchain():
    """(compile command, how to run natively) for an arm64 program, or None."""
    arm = platform.machine().lower() in ("arm64", "aarch64")
    if arm and shutil.which("cc"):
        return ["cc"], []
    if shutil.which("aarch64-linux-gnu-gcc"):
        qemu = shutil.which("qemu-aarch64") or shutil.which("qemu-aarch64-static")
        return ["aarch64-linux-gnu-gcc"], ([qemu, "-L", "/usr/aarch64-linux-gnu"] if qemu else None)
    return None


def sh(cmd, check=True, **kw):
    p = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if check and p.returncode:
        raise AssertionError(" ".join(cmd) + "\n" + p.stdout + p.stderr)
    return p


def main():
    try:
        import qoracle, unicorn, numpy  # noqa: F401
    except ImportError as e:
        print(f"skipped: {e.name} is not installed")
        return
    tc = toolchain()
    if not tc:
        print("skipped: no arm64 C compiler (cc on arm64, or aarch64-linux-gnu-gcc)")
        return
    cc, native = tc
    with tempfile.TemporaryDirectory() as d:
        exe = os.path.join(d, "codebreaker_search")
        sh(cc + ["-O1", "-iquote", os.path.join(ROOT, "include"), DEMO, "-o", exe])
        if native is not None:
            p = sh(native + [exe])
            assert "searching classically on this CPU" in p.stdout and "the secret code: BYRG" in p.stdout, p.stdout
            how = "natively" if not native else "under qemu-aarch64"
        else:
            how = None
        p = sh(EXEC + [exe], env=dict(os.environ, QCPU_SEED="3"))
        assert "searching with Grover's search on the quantum coprocessor (Q-ARM)" in p.stdout, (p.stdout, p.stderr)
        assert "the secret code: BYRG" in p.stdout, p.stdout
        m = re.search(r"QSEARCH consistent, 8 bits: Grover's search with a (\d+)-qubit, (\d+)-gate oracle from "
                      r"consistent's machine code: x = 78 after (\d+) oracle calls", p.stderr)
        assert m, p.stderr
        print(f"1) one binary ({sh(['file', exe], check=False).stdout.split(':', 1)[1].split(',')[0].strip()}): "
              + (f"{how}, the classical search finds BYRG; " if how else "")
              + f"under qarm_exec.py QSEARCH finds BYRG with Grover's search, the oracle ({m.group(1)} qubits, "
              f"{m.group(2)} gates) built from consistent()'s machine code, {m.group(3)} oracle calls, checked")

        src = os.path.join(d, "more.c")
        open(src, "w").write(textwrap.dedent("""\
            #include <stdio.h>
            #include <stdlib.h>
            #include "qarm.h"
            static int three_mod_four(int x) { return (x & 3) == 3; }
            static int square_is_two(int x) { return (x * x) % 64 == 2; }
            static int big(int x) { return x == 3000; }
            int main(int argc, char **argv) {
                int x = -1, r;
                printf("%s|%5d|%-4x|%c|%.3s|%ld\\n", "fmt", 42, 255, 'Q', "abcdef", -7L);
                puts("puts");
                putchar('!'); putchar('\\n');
                printf("avail %d\\n", qarm_available());
                r = qarm_search(three_mod_four, 6, &x); printf("mod4 %d %d\\n", r, r ? x % 4 : -1);
                r = qarm_search(square_is_two, 6, &x); printf("none %d\\n", r);
                r = qarm_search(big, 12, &x); printf("big %d %d\\n", r, x);
                if (argc > 1) exit(atoi(argv[1]));
                return 5;
            }
            """))
        exe2 = os.path.join(d, "more")
        sh(cc + ["-O1", "-iquote", os.path.join(ROOT, "include"), src, "-o", exe2])
        p = sh(EXEC + ["-q", exe2], check=False)
        want = "fmt|   42|ff  |Q|abc|-7\nputs\n!\navail 1\nmod4 1 3\nnone 0\nbig 1 3000\n"
        assert p.stdout == want and p.returncode == 5 and not p.stderr, (p.stdout, p.returncode, p.stderr)
        if native is not None:
            q = sh(native + [exe2], check=False)
            assert q.stdout == want.replace("avail 1", "avail 0") and q.returncode == 5, q.stdout
        print("2) printf formats, puts, putchar, the exit code; QSEARCH with several solutions, none, and 12 bits; "
              "QAVAIL" + ("; the same output natively, but avail 0" if native is not None else ""))

        bad = os.path.join(d, "bad.c")
        open(bad, "w").write(textwrap.dedent("""\
            #include <stdio.h>
            #include "qarm.h"
            __attribute__((noinline)) static int helper(int x) { return x * 3; }
            static int calls(int x) { return helper(x) == 9; }
            int main(void) { int x; printf("%d\\n", qarm_search(calls, 4, &x)); return 0; }
            """))
        exe3 = os.path.join(d, "bad")
        sh(cc + ["-O1", "-iquote", os.path.join(ROOT, "include"), bad, "-o", exe3])
        p = sh(EXEC + [exe3], check=False)
        assert p.returncode == 70 and "QSEARCH calls: qoracle:" in p.stderr, (p.returncode, p.stderr)
        open(bad, "w").write("#include <stdlib.h>\nint main(void) { return abs(-3) + (int)(long)getenv(\"X\"); }\n")
        sh(cc + ["-O0", bad, "-o", exe3])
        p = sh(EXEC + [exe3], check=False)
        assert p.returncode == 70 and "which this runtime does not provide" in p.stderr, p.stderr
        print("3) errors: a predicate that calls another function, a library function the runtime lacks: exit 70 "
              "with the reason")

        part = os.path.join(ROOT, "examples", "qarm", "partition.c")
        pexe = os.path.join(d, "partition")
        sh(cc + ["-O1", "-iquote", os.path.join(ROOT, "include"), part, "-o", pexe])
        os.environ["QCPU_SEED"] = "2"                    # QCOUNT's bound holds with probability >= 8/pi^2: seeded
        p = sh(EXEC + [pexe])
        del os.environ["QCPU_SEED"]
        n, err = re.search(r"about (\d+) \(\+- ([\d.]+); QCOUNT\)", p.stdout).groups()
        assert "difference 1\n" in p.stdout and abs(int(n) - 32) <= float(err), p.stdout
        assert re.search(r"QMIN imbalance, 8 bits: Durr-Hoyer with a \d+-qubit", p.stderr), p.stderr
        if native:
            q = sh(native + [pexe])
            assert "every split tried on this CPU" in q.stdout and "difference 1\n" in q.stdout and "32 (counted)" in q.stdout
        print(f"3b) QMIN and QCOUNT (examples/qarm/partition.c): a split of difference 1; about {n} +- {err} nearly "
              "balanced splits (32 counted on the CPU)")

        for ex, want in (("shor", ["15 = 3 x 5", "21 = 7 x 3"]), ("hidden", ["shift is 0x2d", "string is 0x35"])):
            sexe = os.path.join(d, ex)
            sh(cc + ["-O1", "-iquote", os.path.join(ROOT, "include"), os.path.join(ROOT, "examples", "qarm", ex + ".c"),
                     "-o", sexe])
            p = sh(EXEC + [sexe])
            assert all(w in p.stdout for w in want) and "(simulated)" in p.stderr, (p.stdout, p.stderr)
            if native:
                q = sh(native + [sexe])
                assert all(w in q.stdout for w in want), q.stdout
        pexe = os.path.join(d, "pi")
        sh(cc + ["-O1", "-iquote", os.path.join(ROOT, "include"), os.path.join(ROOT, "examples", "qarm", "pi.c"), "-o", pexe])
        p = sh(EXEC + [pexe])
        est, err = (float(v) for v in re.search(r"pi is about ([\d.]+) \+- ([\d.]+)", p.stdout).groups())
        assert abs(est - 3.1475) <= err, p.stdout                 # the grid's exact value: 3.1475
        print("3c) QPERIOD (examples/qarm/shor.c: 15 = 3 x 5, 21 = 7 x 3 from C modexp's oracle), QSIMON (shift 0x2d), "
              f"QBV (string 0x35): the same answers as the plain CPU; QAMPEST (pi.c): {est} +- {err} (the grid: 3.1475)")

        gexe = os.path.join(d, "ghz")
        sh(cc + ["-O1", "-iquote", os.path.join(ROOT, "include"), os.path.join(ROOT, "examples", "qarm", "ghz.c"),
                 "-o", gexe, "-lm"])
        p = sh(EXEC + [gexe])
        assert re.search(r"GHZ\(5\): 00000 \d+, 11111 \d+, other 0 of 2000 shots \(on the coprocessor\)", p.stdout), p.stdout
        assert "11->1 100%" in p.stdout and "qarm_exec.py: exact simulator" in p.stdout, p.stdout
        assert p.stderr.count("QCIRC: 3 qubits") == 4 and "QRAND: 16 bytes" in p.stderr, p.stderr
        if native:
            q = sh(native + [gexe])
            assert "simulated here" in q.stdout and "11->1 100%" in q.stdout and "device: classical" in q.stdout, q.stdout
        print("3d) QCIRC, QRAND, QCAPS (examples/qarm/ghz.c): GHZ on 5 qubits, only 00000 and 11111; Toffoli's truth "
              "table; the plain CPU's state vector gives the same")

        cexe = os.path.join(d, "conformance")
        sh(cc + ["-O1", "-iquote", os.path.join(ROOT, "include"),
                 os.path.join(ROOT, "tests", "conformance", "qarm_conformance.c"), "-o", cexe, "-lm"])
        p = sh(EXEC + ["-q", cexe], check=False)
        assert p.returncode == 0 and "conformance: 14 passed, 0 failed, 0 skipped (features 0x1fff)" in p.stdout, \
            (p.stdout, p.stderr)
        if native:
            q = sh(native + [cexe])
            assert "SKIP QAVAIL: no runtime" in q.stdout, q.stdout
        print("5) conformance (tests/conformance/qarm_conformance.c): 14 of 14 under qarm_exec.py, features 0x1fff"
              + ("; the plain CPU: no runtime" if native else ""))

        try:
            import qiskit, qiskit_aer, qiskit_ibm_runtime  # noqa: F401
        except ImportError as e:
            print(f"4) skipped: {e.name} is not installed")
        else:
            import json
            def config(**c):
                path = os.path.join(d, "config.json")
                json.dump(dict({"mode": "sampler"}, **c), open(path, "w"))
                return path
            exe4 = os.path.join(d, "tiny_search")
            sh(cc + ["-O1", "-iquote", os.path.join(ROOT, "include"), TINY, "-o", exe4])
            done = []
            for c in ({"backend": "preview", "shots": 400, "options": {"device": "fez"}},
                      {"backend": "ibm", "shots": 400, "options": {"channel": "local", "device": "fake_torino"}}):
                p = sh(EXEC + ["--config", config(**c), exe4], check=False)
                assert p.returncode == 0 and p.stdout == "Q-ARM QSEARCH: x = 3\n", (p.returncode, p.stdout, p.stderr)
                m = re.search(r"QSEARCH is_three, 2 bits on (.+?): (\d+) qubits, 2 circuits .* job (\S+), .*: x = 3, "
                              r"measured (\d+) times of 800", p.stderr)
                assert m and m.group(3) != "None", p.stderr
                done.append(f"{m.group(1).replace('preview: ', '')} x = 3 ({int(m.group(4)) / 8:.0f}% of shots)")
            p = sh(EXEC + ["--config", config(backend="preview", shots=100), exe], check=False)
            assert p.returncode == 70 and re.search(r"needs \d+ qubits, fake_fez has 156: not run", p.stderr), p.stderr
            print("4) QSEARCH on a QPU backend from a config file, one job: " + "; ".join(done)
                  + "; Code Breaker's oracle refused as larger than the device")
            p = sh(EXEC + ["--config", config(backend="preview", shots=100, options={"device": "fez"}), gexe], check=False)
            g = re.search(r"00000 (\d+), 11111 (\d+), other (\d+)", p.stdout)
            assert g and int(g[1]) + int(g[2]) > 1600 and "156 qubits, flags 2" in p.stdout, (p.stdout, p.stderr)
            assert re.search(r"QCIRC: 5 qubits, 5 gates .* on preview: noise model of fake_fez", p.stderr), p.stderr
            print(f"4b) ghz.c on the noise model of Fez: GHZ {g[1]}/{g[2]}, {g[3]} other (device errors), one job each")
    print("PASS")


if __name__ == "__main__":
    main()
