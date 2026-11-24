# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Quantag IT Solutions GmbH
"""qcpucc: programs with __qcpu__ functions (docs/qcpu_spec.md).

  1. examples/codebreaker: scripted games print the same as the program compiled by a plain C compiler
     (where __qcpu__ expands to nothing), every score() call runs on the quantum CPU and is checked
     (QCPU_OK, guarantee E1); also with QCPU_BACKEND=native. The CMake build gives the same program.
  2. The quantum-CPU side keeps only what __qcpu__ functions reach: main(), scanf and printf are left out.
  3. Several source files; a __qcpu__ function calling another; buffers (QCPU_INOUT): incremented once.
  4. Build errors: a __qcpu__ function that prints, a static __qcpu__ function.
  5. Exactness: f(x) = (x << 8) >> 8 with the truncating intermediate in the machine code (checked in
     the emitted instructions); at W = 12, f(16) is QCPU_MISMATCH, never a silent 0; at W = 16 it is exact.
  6. Data models (spec 5.6.1): sizeof(long), (char)200 < 0 and a struct size agree with the host;
     the probe reports sizeof(long) for a Windows host compiler.
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
            strip = lambda t: "\n".join(l.split("    [quantum CPU")[0] for l in t.splitlines())
            assert strip(got.stdout) == strip(want.stdout) and got.returncode == want.returncode, (got.stdout, want.stdout)
            n = got.stdout.count("checked]")
            assert n == got.stdout.count("black "), (n, got.stdout)   # one quantum-CPU call per scored guess
            nat = sh([game, seed], env=env(QCPU_BACKEND="native"), inp=moves, check=False)
            assert nat.stdout == want.stdout and nat.returncode == want.returncode
        print(f"1) codebreaker: {len(games)} scripted games identical to the plain C build; every score() call on the "
              "quantum CPU and checked; QCPU_BACKEND=native identical")
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
    print("PASS")


if __name__ == "__main__":
    main()
