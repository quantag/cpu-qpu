# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Quantag IT Solutions GmbH
"""qcpucc: programs with __qcpu__ functions (docs/qcpu_spec.md).

  1. examples/codebreaker: scripted games print the same as the program compiled by a plain C compiler
     (where __qcpu__ expands to nothing), every score() call runs on the virtual CPU and is checked
     (QCPU_OK, guarantee E1); also with QCPU_BACKEND=native. The CMake build gives the same program.
  2. The quantum-CPU side keeps only what __qcpu__ functions reach: main(), scanf and printf are left out.
  3. Several source files; a __qcpu__ function calling another; buffers (QCPU_INOUT): incremented once.
  4. Build errors: a __qcpu__ function that prints, a static __qcpu__ function.
  5. Exactness: f(x) = (x << 8) >> 8 with the truncating intermediate in the machine code (checked in
     the emitted instructions); at W = 12, f(16) is QCPU_MISMATCH, never a silent 0; at W = 16 it is exact.
  6. Data models (spec 5.6.1): sizeof(long), (char)200 < 0 and a struct size agree with the host;
     the probe reports sizeof(long) for a Windows host compiler.
  7. --qcpu-mode qoracle (spec 5.5): Code Breaker with score() as its own circuit: the same games as the plain
     and the virtual-CPU builds, every call checked; all 65536 inputs of score() through the embedded circuit
     equal the host build; QCPU_BITS / QCPU_SBITS / QCPU_RESULT_BITS (a false claim fails the build);
     arguments outside their bits are QCPU_BAD_ARGS; a function qoracle cannot build is a build error.
Run: python3 -m tests.test_qcpucc"""
import os, shutil, subprocess, sys, tempfile, textwrap
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import qcpucc

QCPUCC = [sys.executable, os.path.join(ROOT, "qcpucc.py")]
EX = os.path.join(ROOT, "examples", "codebreaker")
RT = os.path.join(ROOT, "runtime")


def sh(cmd, env=None, inp=None, check=True):
    p = subprocess.run(cmd, capture_output=True, text=True, env=env, input=inp)
    if check and p.returncode:
        raise AssertionError(" ".join(cmd) + "\n" + p.stdout + p.stderr)
    return p


HOST_CC = (os.environ.get("CC") or qcpucc.default_cc()).split()      # the compiler qcpucc uses too
OBJDUMP = next((t for t in ("llvm-objdump", "objdump") if shutil.which(t) and
                "LLVM" in subprocess.run([t, "--version"], capture_output=True, text=True).stdout), None)


def env(**kw):
    return dict(os.environ, **kw)


def main():
    with tempfile.TemporaryDirectory() as d:
        # 1. the game
        game = os.path.join(d, "codebreaker")
        built = sh(QCPUCC + [os.path.join(EX, "codebreaker.c"), "-o", game])
        plain = os.path.join(d, "plain")
        sh(HOST_CC + ["-I", RT, os.path.join(EX, "codebreaker.c"), "-o", plain])      # plain compiler, no qcpucc
        games = [("7", "RRRR\nRGBY\nxx\nYYGG\n"), ("11", "BBBB\nGGRR\nRGBY\nYBGR\nBYRG\n"), ("3", "RGBY\n" * 11)]
        for seed, moves in games:
            want = sh([plain, seed], inp=moves, check=False)
            got = sh([game, seed], inp=moves, check=False)
            strip = lambda t: "\n".join(l.split("    [virtual CPU")[0] for l in t.splitlines())
            assert strip(got.stdout) == strip(want.stdout) and got.returncode == want.returncode, (got.stdout, want.stdout)
            n = got.stdout.count("checked]")
            assert n == got.stdout.count("black "), (n, got.stdout)   # one quantum-CPU call per scored guess
            nat = sh([game, seed], env=env(QCPU_BACKEND="native"), inp=moves, check=False)
            assert nat.stdout == want.stdout and nat.returncode == want.returncode
        print(f"1) codebreaker: {len(games)} scripted games identical to the plain C build; every score() call on the "
              "virtual CPU and checked; QCPU_BACKEND=native identical")
        if shutil.which("cmake"):
            b = os.path.join(d, "cmake")
            sh(["cmake", "-S", EX, "-B", b])
            sh(["cmake", "--build", b])
            moves = "RGBY\nYYGG\nBBRR\n"      # the secret for a seed depends on the C library's rand()
            got = sh([os.path.join(b, "codebreaker"), "7"], inp=moves, check=False)
            want = sh([game, "7"], inp=moves, check=False)
            assert got.stdout == want.stdout and got.stdout.count("checked]") == got.stdout.count("black ") > 0, (got.stdout, want.stdout)
            print("   CMake build (add_qcpu_executable): same program")

        # 2. pruning
        out = built.stdout + built.stderr
        line = next((l for l in out.splitlines() if "__qcpu__ function(s)" in l), "")
        assert line.rstrip().endswith("): score") and "34 instructions" in line, out
        print("2) quantum-CPU side: only score() and what it reaches (34 instructions); main, fgets, printf, rand left out")

        # 3. several files, calls between __qcpu__ functions, buffers
        a_c, m_c = os.path.join(d, "lib.c"), os.path.join(d, "main.c")
        open(a_c, "w").write(textwrap.dedent("""\
            #include <qcpu.h>
            __qcpu__ int sq(int x) { return x * x; }
            __qcpu__ int sum_sq(int a, int b) { return sq(a) + sq(b); }
            __qcpu__ void increment(int *QCPU_INOUT(1) p) { *p += 1; }
            """))
        open(m_c, "w").write(textwrap.dedent("""\
            #include <stdio.h>
            #include <qcpu.h>
            int sum_sq(int a, int b);
            void increment(int *p);
            int main(void) {
                qcpu_call_info i;
                int x = 41;
                increment(&x);
                printf("%d %d %s\\n", sum_sq(3, 4), x, qcpu_status_name(qcpu_last_call(&i)));
                return 0;
            }
            """))
        exe = os.path.join(d, "multi")
        sh(QCPUCC + [a_c, m_c, "-o", exe])
        p = sh([exe])
        assert p.stdout == "25 42 QCPU_OK\n" and not p.stderr, (p.stdout, p.stderr)
        print("3) two source files, sum_sq() calling sq(), increment() on an in/out buffer: 25 42 QCPU_OK")

        # 4. build errors
        bad = os.path.join(d, "bad.c")
        for body, msg in (("#include <stdio.h>\n#include <qcpu.h>\n__qcpu__ int f(int x) { printf(\"%d\", x); return x; }\nint main(void) { return f(1); }\n", "must be pure"),
                          ("#include <qcpu.h>\nstatic __qcpu__ int f(int x) { return x; }\nint main(void) { return f(1); }\n", "is static")):
            open(bad, "w").write(body)
            p = sh(QCPUCC + [bad, "-o", os.path.join(d, "bad")], check=False)
            assert p.returncode and msg in p.stderr, p.stderr
        print("4) build errors: printf in a __qcpu__ function (must be pure), static __qcpu__ function")

        # 5. exactness: the truncating intermediate must exist in the machine code
        ex = os.path.join(d, "trunc.c")
        open(ex, "w").write(textwrap.dedent("""\
            #include <stdio.h>
            #include <stdint.h>
            #include <stdlib.h>
            #include <qcpu.h>
            __qcpu__ uint32_t f(uint32_t x) {
                uint32_t y = x << 8;
                __asm__("" : "+r"(y));          /* keeps the intermediate: no folding into a mask */
                return y >> 8;
            }
            int main(int argc, char **argv) {
                qcpu_call_info i;
                uint32_t r = f((uint32_t)atoi(argv[1]));
                printf("%u %s\\n", r, qcpu_status_name(qcpu_last_call(&i)));
                return 0;
            }
            """))
        obj = os.path.join(d, "trunc.o")
        sh(["clang", "--target=aarch64-linux-gnu", "-O1", "-nostdlibinc", "-isystem", qcpucc.QINC, "-I", RT, "-c", ex, "-o", obj])
        if OBJDUMP:                       # LLVM's objdump (macOS: the system objdump)
            dis = sh([OBJDUMP, "-d", obj]).stdout.split("<f>:")[1].split("\n\n")[0]
            assert "lsl" in dis and "lsr" in dis and "and" not in dis, dis
        else:
            print("   (no LLVM objdump: the emitted instructions are not checked)")
        # W = 12 is the narrowest machine that holds code and memory; 16 << 8 = 4096 needs 13 bits
        for w, mode, want in (("12", {}, None), ("12", {"QCPU_ON_FAIL": "native", "QCPU_QUIET": "1"}, "16 QCPU_MISMATCH\n"),
                              ("16", {}, "16 QCPU_OK\n")):
            exe = os.path.join(d, f"trunc{w}")
            if not os.path.exists(exe):
                sh(QCPUCC + ["--qcpu-width", w, ex, "-o", exe])
            p = sh([exe, "16"], env=env(**mode), check=False)
            if want is None:
                assert p.returncode == 70 and "QCPU_MISMATCH" in p.stderr, (p.returncode, p.stdout, p.stderr)
            else:
                assert p.stdout == want, (w, mode, p.stdout, p.stderr)
        print("5) (x << 8) >> 8 with lsl and lsr in the machine code, f(16): W = 12: QCPU_MISMATCH (exit 70; with "
              "ON_FAIL=native the host result 16), never a silent 0; W = 16: 16 QCPU_OK")

        # 6. data models
        dm = os.path.join(d, "dm.c")
        open(dm, "w").write(textwrap.dedent("""\
            #include <stdio.h>
            #include <qcpu.h>
            __qcpu__ int size_long(void) { return (int)sizeof(long); }
            __qcpu__ int char_negative(int x) { return (char)x < 0; }
            __qcpu__ int struct_size(void) { struct { char c; long l; short s; } v; return (int)sizeof v; }
            int main(void) {
                qcpu_call_info i;
                printf("%d %d %d\\n", size_long(), char_negative(200), struct_size());
                printf("%s\\n", qcpu_status_name(qcpu_last_call(&i)));
                return 0;
            }
            """))
        exe = os.path.join(d, "dm")
        sh(QCPUCC + [dm, "-o", exe])
        sh(HOST_CC + ["-I", RT, dm, "-o", os.path.join(d, "dmn")])
        want = sh([os.path.join(d, "dmn")]).stdout
        _, host_diffs = qcpucc.data_model(HOST_CC, ["clang"] + qcpucc.isas.get("arm64").clang)
        if not any("sizeof(long)" in x for x in host_diffs):
            p = sh([exe])
            assert p.stdout == want and not p.stderr, (p.stdout, want, p.stderr)
            seen = f"equal to the host ({want.splitlines()[0]})"
        else:                             # Windows (LLP64): long is 4 bytes here, 8 on the quantum CPU
            p = sh([exe], check=False)
            assert p.returncode == 70 and "QCPU_MISMATCH" in p.stderr and not p.stdout, (p.returncode, p.stdout, p.stderr)
            p = sh([exe], env=env(QCPU_ON_FAIL="native"))       # the order of the three calls is the compiler's
            assert p.stdout.splitlines()[0] == want.splitlines()[0] and "QCPU_MISMATCH" in p.stderr, (p.stdout, p.stderr, want)
            seen = (f"differ on this host (sizeof(long) 4): caught as QCPU_MISMATCH, exit 70; with ON_FAIL=native the "
                    f"host values ({want.splitlines()[0]})")
        flags, diffs = qcpucc.data_model(["clang", "--target=x86_64-pc-windows-msvc"], ["clang", "--target=aarch64-linux-gnu"])
        assert any("sizeof(long): host 4, quantum CPU 8" in x for x in diffs) and "-fsigned-char" in flags, (flags, diffs)
        print(f"6) data models: sizeof(long), (char)200 < 0, struct size {seen}; "
              "a Windows host compiler: reported " + "; ".join(x for x in diffs if "long)" in x))
        compiled_mode(d)
    print("PASS")


def compiled_mode(d):
    try:
        import qoracle  # noqa: F401
    except ImportError:
        print("7) --qcpu-mode qoracle: skipped (qoracle is not installed)")
        return
    src = os.path.join(EX, "codebreaker.c")
    game = os.path.join(d, "cb_qoracle")
    built = sh(QCPUCC + ["--qcpu-mode", "qoracle", src, "-o", game])
    out = built.stdout + built.stderr
    assert "1 __qcpu__ function(s) as qoracle circuits: score" in out and "virtual CPU" not in out, out
    assert "score: qoracle circuit, 544 qubits (16 in, 6 out), 1844 gates (614 Toffoli); checked on all 65536 inputs" in out, out
    plain, vcpu = os.path.join(d, "plain"), os.path.join(d, "codebreaker")     # built in item 1
    moves = "RRGG\nRGBY\nYYGG\nBGRY\nBBBB\n"
    strip = lambda t: "\n".join(l.split("    [")[0] for l in t.splitlines())
    for seed in ("7", "11", "3"):
        want = sh([plain, seed], inp=moves, check=False)
        got = sh([game, seed], inp=moves, check=False)
        assert strip(got.stdout) == strip(want.stdout) and got.returncode == want.returncode, (got.stdout, want.stdout)
        assert got.stdout.count("[qoracle circuit: 1844 gates on 544 qubits, checked]") == got.stdout.count("black "), got.stdout
        v = sh([vcpu, seed], inp=moves, check=False)
        assert strip(v.stdout) == strip(want.stdout) and "[virtual CPU: 245 cycles" in v.stdout, v.stdout
    size_q, size_v = os.path.getsize(game + (".exe" if os.name == "nt" else "")), os.path.getsize(vcpu + (".exe" if os.name == "nt" else ""))

    # every input of score() through the C engine's copy of the circuit, each checked against the host build
    every = os.path.join(d, "every.c")
    with open(src) as f:
        body = f.read().split("int main(")[0]
    open(every, "w").write(body + textwrap.dedent("""\
        int main(void) {
            long sum = 0; int n = 0; qcpu_call_info i;
            for (int g = 0; g < 256; g++)
                for (int t = 0; t < 256; t++) {
                    sum += score(g, t);
                    qcpu_last_call(&i);
                    n += i.status == QCPU_OK && strcmp(i.mode, "qoracle") == 0 && strcmp(i.guarantee, "E1") == 0;
                }
            printf("%ld %d\\n", sum, n);
            return 0;
        }
        """))
    exe = os.path.join(d, "every")
    sh(QCPUCC + ["--qcpu-mode", "qoracle", every, "-o", exe])
    p = sh([exe])
    sum_, n = p.stdout.split()
    assert n == "65536" and not p.stderr, (p.stdout, p.stderr)
    print(f"7) --qcpu-mode qoracle: Code Breaker with score() as a 544-qubit, 1844-gate circuit (the virtual CPU: 35.3M "
          f"gates per call): same games as the plain and vcpu builds, every call checked; program {size_q // 1024} KB "
          f"(vcpu: {size_v // 1024} KB)")
    print(f"   all 65536 inputs of score() through the embedded circuit equal the host build (sum {sum_})")

    m = os.path.join(d, "modes.c")
    open(m, "w").write(textwrap.dedent("""\
        #include <stdio.h>
        #include <stdlib.h>
        #include <string.h>
        #include <qcpu.h>
        __qcpu__ int QCPU_RESULT_BITS(4) low(int a, int b) { return (a ^ b) & 15; }
        __qcpu__ int triple(int QCPU_SBITS(8) a) { return 3 * a; }
        __qcpu__ unsigned popc(unsigned QCPU_BITS(12) x) { unsigned n = 0; while (x) { n += x & 1; x >>= 1; } return n; }
        static const char *mode(void) { qcpu_call_info i; qcpu_last_call(&i); return i.mode; }
        int main(int argc, char **argv) {
            int a = atoi(argv[1]);
            int r1 = low(a, 7); const char *m1 = mode();
            int r2 = triple(a); const char *m2 = mode();
            unsigned r3 = popc((unsigned)a); const char *m3 = mode();
            printf("%d %s %d %s %u %s\\n", r1, m1, r2, m2, r3, m3);
            return 0;
        }
        """))
    exe = os.path.join(d, "modes")
    built = sh(QCPUCC + ["--qcpu-mode", "qoracle", m, "-o", exe])
    out = built.stdout + built.stderr
    assert "low: qoracle circuit" in out and "triple: qoracle circuit" in out and "popc: qoracle circuit" in out, out
    assert sh([exe, "100"]).stdout == "3 qoracle 300 qoracle 3 qoracle\n"
    assert sh([exe, "-100"], check=False).returncode == 70        # popc(-100): outside its 12 bits
    p = sh([exe, "-100"], env=env(QCPU_ON_FAIL="native", QCPU_QUIET="1"))
    assert p.stdout.split()[:4] == ["11", "qoracle", "-300", "qoracle"], p.stdout
    p = sh([exe, "4095"], check=False)
    assert p.returncode == 70 and "QCPU_BAD_ARGS" in p.stderr and "signed 8 bits" in p.stderr, p.stderr
    buf = os.path.join(d, "buf.c")
    open(buf, "w").write("#include <qcpu.h>\n__qcpu__ void inc(int *QCPU_INOUT(1) p) { *p += 1; }\n"
                         "int main(void) { int x = 1; inc(&x); return x - 2; }\n")
    p = sh(QCPUCC + ["--qcpu-mode", "qoracle", buf, "-o", os.path.join(d, "buf")], check=False)
    assert p.returncode and "inc: no qoracle circuit: it takes buffers" in p.stderr and "--qcpu-mode vcpu" in p.stderr, p.stderr
    sh(QCPUCC + [buf, "-o", os.path.join(d, "buf")])
    assert sh([os.path.join(d, "buf")]).returncode == 0
    print("   QCPU_BITS(12), QCPU_SBITS(8), QCPU_RESULT_BITS(4) on 64 input bits (proven in the logic); arguments outside "
          "their bits: QCPU_BAD_ARGS; a buffer function: build error with qoracle, runs with vcpu")

    bad = os.path.join(d, "claim.c")
    for body, msg in (("int QCPU_RESULT_BITS(4) f(int QCPU_BITS(8) a) { return a + 1; }", "is wrong: for a = 15 the result is 16"),
                      ("int QCPU_RESULT_BITS(4) f(int a, int b) { return (a + b) & 31; }", "cannot be proven")):
        open(bad, "w").write(f"#include <qcpu.h>\n__qcpu__ {body}\nint main(void) {{ return 0; }}\n")
        p = sh(QCPUCC + ["--qcpu-mode", "qoracle", bad, "-o", os.path.join(d, "claim")], check=False)
        assert p.returncode and msg in p.stderr, p.stderr
    print("   a false QCPU_RESULT_BITS fails the build, with a counterexample or the reason it cannot be proven")


if __name__ == "__main__":
    main()
