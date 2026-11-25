# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Quantag IT Solutions GmbH
"""QPU libraries (qpu_dll.py, runtime/qpu_engine.c): a host program calls functions that run on the
quantum machine inside a shared library; no Python at run time.

  1. examples/dll: the host program linked against the generated library prints exactly what the
     same program prints with mathlib.c compiled for the host, for QPU code in AArch64, RV64, RV32
     and x86-64; no warning. The same with QCPU_BACKEND=native, with QCPU_VERIFY=0 QCPU_EXACT=0, and with
     the older QPU_* names. QCPU_VERIFY=0 alone is refused (QCPU_UNVERIFIED, exit 70).
  2. Same circuit: the C engine's final state equals the Python simulator's (core.run_fast) on every
     qubit, for calls of every integer function with random arguments, on all four architectures.
  3. The call contract (docs/qcpu_spec.md 5.6-5.7): a result outside the W-bit model is a QCPU_MISMATCH
     (exit 70); QCPU_ON_FAIL=native returns the host build's result; QCPU_ON_FAIL=status returns 0 and
     the status; an argument outside the model whose result is still confirmed is QCPU_OK; a 16-bit
     build gives the exact result.
  4. A function that does not return within QCPU_MAX_STEPS cycles: QCPU_INCOMPLETE, exit status 70.
  5. An instruction the machine does not implement: warning when building, QCPU_TRAP when reached.
  5b. Buffers (5.6.2): an in/out buffer is incremented once, not twice; aliasing of the same buffer is
     kept; overlapping buffers are QCPU_BAD_ARGS; a failed call leaves the caller's buffer untouched.
  6. Build errors: printf in the library, pointer without QPU_IN/OUT, struct parameter, double return,
     a declared function that is not defined.
  7. Windows: library and host program cross-compiled with MinGW. The DLL exports exactly the
     header's functions, the import library links, main.exe imports them from the DLL. (Running
     main.exe needs Windows or Wine; it is run under Wine when Wine works.)
Run: python3 -m tests.test_dll [arch,...]"""
import ctypes, os, random, shutil, subprocess, sys, tempfile, textwrap
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import cpu_qpu
import qpu_dll
from qcpu import isa as isas

EX = os.path.join(ROOT, "examples", "dll")
RT = os.path.join(ROOT, "runtime")
TOOL = [sys.executable, os.path.join(ROOT, "qpu_dll.py")]


def sh(cmd, env=None, check=True):
    p = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=1200)
    if check and p.returncode:
        raise AssertionError(f"{' '.join(cmd)} failed ({p.returncode}):\n{p.stdout}\n{p.stderr}")
    return p


def host(d, main, lib_dir, lib, exe, incs=(EX,)):
    sh(["cc", "-Wall", "-Werror", "-I", RT] + [x for i in incs for x in ("-I", i)] +
       [main, "-L", lib_dir, "-l" + lib, "-Wl,-rpath," + lib_dir, "-o", exe])
    return exe


def same_circuit(arch, d, rng, calls=12):
    """C engine == Python simulator on every qubit."""
    isa = isas.get(arch)
    obj, _ = cpu_qpu.compile_c(os.path.join(EX, "mathlib.c"), "auto", ["-O1", "-fno-stack-protector"], arch)
    so = os.path.join(d, f"libself_{arch}.so")
    sh(TOOL + [obj, "--header", os.path.join(EX, "mathlib.h"), "-o", so, "--selftest"])
    funcs = qpu_dll.parse_header(os.path.join(EX, "mathlib.h"))
    B = qpu_dll.build_machine(obj, isa, funcs)
    m = B["m"]
    lib = ctypes.CDLL(so)
    lib.qpu__selftest.restype = ctypes.c_long
    lib.qpu__selftest.argtypes = [ctypes.c_long, ctypes.c_int, ctypes.POINTER(ctypes.c_longlong), ctypes.c_char_p]
    lib.qpu__selftest_qubits.restype = ctypes.c_ulong
    assert lib.qpu__selftest_qubits() == m.n
    cases = [("gcd", lambda: [rng.randrange(1, 2000), rng.randrange(0, 2000)]),
             ("modexp", lambda: [rng.randrange(2, 30), rng.randrange(0, 12), rng.randrange(2, 60)]),
             ("is_prime", lambda: [rng.randrange(-5, 400)])]
    for k in range(calls):
        fn, gen = cases[k % len(cases)]
        args = gen()
        out = ctypes.create_string_buffer(m.n)
        cycles = lib.qpu__selftest(B["entry"][fn], len(args), (ctypes.c_longlong * len(args))(*args), out)
        assert cycles > 0, (arch, fn, args, cycles)
        st = bytearray(m.n)                          # the same initial state, built in Python
        for q in B["ones"]:
            st[q] ^= 1
        for i, q in enumerate(B["pc_in"]):
            st[q] = (B["entry"][fn] >> i) & 1
        for i, v in enumerate(args):
            for j, q in enumerate(m.X[isa.arg_regs[i]]):
                st[q] = (v >> j) & 1
        m.cfg.STEPS = cycles
        py = m.run_fast([q for q in range(m.n) if st[q]])
        assert bytes(py["state"][:m.n]) == out.raw, f"{arch} {fn}{tuple(args)}: C engine and Python simulator differ"
        assert py["run"] == 0, (arch, fn, args)
    return calls, m.n, len(B["tmpl"])


def main():
    archs = sys.argv[1].split(",") if len(sys.argv) > 1 else ["arm64", "riscv64", "riscv32", "x86_64"]
    rng = random.Random(7)
    with tempfile.TemporaryDirectory() as d:
        native = os.path.join(d, "main_cpu")
        sh(["cc", "-Wall", "-I", RT, os.path.join(EX, "main.c"), os.path.join(EX, "mathlib.c"), "-o", native])
        want = sh([native]).stdout
        for arch in archs:
            ad = os.path.join(d, arch)
            os.makedirs(ad)
            sh(TOOL + [os.path.join(EX, "mathlib.c"), "--header", os.path.join(EX, "mathlib.h"), "--arch", arch,
                       "-o", os.path.join(ad, "libmathlib_qpu.so")])
            exe = host(d, os.path.join(EX, "main.c"), ad, "mathlib_qpu", os.path.join(ad, "main"))
            for env in ({}, {"QCPU_BACKEND": "native"}, {"QCPU_VERIFY": "0", "QCPU_EXACT": "0"},
                        {"QPU_BACKEND": "qpu", "QPU_VERIFY": "1"}):
                p = sh([exe], dict(os.environ, **env))
                assert p.stdout == want and not p.stderr, f"{arch} {env}:\n{p.stdout}\n{p.stderr}"
            p = sh([exe], dict(os.environ, QCPU_VERIFY="0"), check=False)
            assert p.returncode == 70 and "QCPU_UNVERIFIED" in p.stderr, (p.returncode, p.stderr)
            print(f"1) {arch}: host program with mathlib on the QPU == with mathlib on the CPU "
                  f"({len(want.splitlines())} lines); also native, unchecked and with QPU_* names; "
                  "QCPU_VERIFY=0 alone: QCPU_UNVERIFIED")
        for arch in archs:
            calls, nq, ng = same_circuit(arch, d, rng)
            print(f"2) {arch}: C engine == Python simulator on all {nq} qubits, {calls} calls ({ng} gates per cycle)")

        # 3.-5. edge cases
        hdr, src = os.path.join(d, "edge.h"), os.path.join(d, "edge.c")
        open(hdr, "w").write("int twice(int x);\nint spin(int x);\nunsigned swap_bytes(unsigned x);\n")
        open(src, "w").write(textwrap.dedent("""\
            #include "edge.h"
            int twice(int x) { return 2 * x; }
            int spin(int x) { volatile int k = x; while (k) k = k + 2; return k; }
            unsigned swap_bytes(unsigned x) { return __builtin_bswap32(x); }
            """))
        open(os.path.join(d, "edge_main.c"), "w").write(textwrap.dedent("""\
            #include <stdio.h>
            #include <stdlib.h>
            #include <string.h>
            #include "edge.h"
            int main(int argc, char **argv) {
                int x = atoi(argv[2]);
                if (!strcmp(argv[1], "twice")) printf("%d\\n", twice(x));
                if (!strcmp(argv[1], "spin")) printf("%d\\n", spin(x));
                if (!strcmp(argv[1], "swap")) printf("%u\\n", swap_bytes((unsigned)x));
                return argc - 3;
            }
            """))
        for w in ("12", "16"):
            sh(TOOL + [src, "--header", hdr, "--arch", "arm64", "--width", w, "-o", os.path.join(d, f"w{w}", "libedge_qpu.so")])
        x86 = sh(TOOL + [src, "--header", hdr, "--arch", "x86_64", "-o", os.path.join(d, "x86", "libedge_qpu.so")])
        e12 = host(d, os.path.join(d, "edge_main.c"), os.path.join(d, "w12"), "edge_qpu", os.path.join(d, "e12"), (d,))
        e16 = host(d, os.path.join(d, "edge_main.c"), os.path.join(d, "w16"), "edge_qpu", os.path.join(d, "e16"), (d,))
        ex86 = host(d, os.path.join(d, "edge_main.c"), os.path.join(d, "x86"), "edge_qpu", os.path.join(d, "ex86"), (d,))
        p = sh([e12, "twice", "500"])
        assert p.stdout == "1000\n" and not p.stderr, (p.stdout, p.stderr)
        p = sh([e12, "twice", "1500"], check=False)      # 3000 does not fit 12 bits
        assert p.returncode == 70 and "QCPU_MISMATCH" in p.stderr and "return value differs" in p.stderr, p.stderr
        p = sh([e12, "twice", "1500"], dict(os.environ, QCPU_ON_FAIL="native"))
        assert p.stdout == "3000\n" and "QCPU_MISMATCH" in p.stderr and "host build's result" in p.stderr, (p.stdout, p.stderr)
        p = sh([e12, "twice", "1500"], dict(os.environ, QCPU_ON_FAIL="status", QCPU_QUIET="1"))
        assert p.stdout == "0\n" and not p.stderr, (p.stdout, p.stderr)
        p = sh([e12, "twice", "-1"])                     # in the model
        assert p.stdout == "-2\n" and not p.stderr, (p.stdout, p.stderr)
        p = sh([e16, "twice", "1500"])
        assert p.stdout == "3000\n" and not p.stderr, (p.stdout, p.stderr)
        print("3) outside the 12-bit model: QCPU_MISMATCH (exit 70); ON_FAIL=native: host result; "
              "ON_FAIL=status: 0; 16-bit build: exact")
        p = sh([e12, "spin", "1"], dict(os.environ, QCPU_MAX_STEPS="3000"), check=False)
        assert p.returncode == 70 and "QCPU_INCOMPLETE" in p.stderr and "3000" in p.stderr, (p.returncode, p.stderr)
        print("4) " + p.stderr.strip())
        assert "does not implement" in x86.stderr and "bswap" in x86.stderr, x86.stderr
        p = sh([ex86, "swap", "1"], check=False)
        assert p.returncode == 70 and "QCPU_TRAP" in p.stderr, (p.returncode, p.stderr)
        msg = p.stderr.strip()
        p = sh([ex86, "twice", "21"])
        assert p.stdout == "42\n" and not p.stderr
        print("5) bswap on x86-64: warning when building; the call: " + msg + "; other functions work")

        # 5b. buffers under verification
        bh, bs, bm = (os.path.join(d, n) for n in ("buf.h", "buf.c", "buf_main.c"))
        open(bh, "w").write("#define QCPU_LIBRARY 1\n#include <qcpu.h>\nvoid increment(int *QCPU_INOUT(1) p);\n"
                            "int add_to(int *QCPU_INOUT(n) dst, const int *QCPU_IN(n) src, int n);\n"
                            "int big(int *QCPU_INOUT(1) p);\n")
        open(bs, "w").write("#include \"buf.h\"\nvoid increment(int *p) { *p += 1; }\n"
                            "int add_to(int *dst, const int *src, int n) { int s = 0; for (int i = 0; i < n; i++) { dst[i] += src[i]; s += dst[i]; } return s; }\n"
                            "int big(int *p) { *p = *p * 64; return *p; }\n")
        open(bm, "w").write(textwrap.dedent("""\
            #include <stdio.h>
            #include "buf.h"
            int main(void) {
                qcpu_call_info info;
                int x = 41; increment(&x); printf("increment %d %s\\n", x, qcpu_status_name(qcpu_last_call(&info)));
                int v[3] = {1, 2, 3};
                int s = add_to(v, v, 3);                 /* the same buffer twice: aliasing kept */
                printf("alias %d %d %d %d\\n", s, v[0], v[1], v[2]);
                s = add_to(v, v + 1, 2);                 /* overlapping, not identical */
                printf("overlap %d %s %d %d %d\\n", s, qcpu_status_name(qcpu_last_call(&info)), v[0], v[1], v[2]);
                int y = 1000; s = big(&y);               /* 64000 does not fit 16 bits */
                printf("big %d %s %d\\n", s, qcpu_status_name(qcpu_last_call(&info)), y);
                return 0;
            }
            """))
        sh(TOOL + [bs, "--header", bh, "-o", os.path.join(d, "bufl", "libbuf_qpu.so")])
        eb = host(d, bm, os.path.join(d, "bufl"), "buf_qpu", os.path.join(d, "ebuf"), (d,))
        p = sh([eb], dict(os.environ, QCPU_ON_FAIL="status", QCPU_QUIET="1"))
        want_b = ("increment 42 QCPU_OK\nalias 12 2 4 6\noverlap 0 QCPU_BAD_ARGS 2 4 6\n"
                  "big 0 QCPU_MISMATCH 1000\n")
        assert p.stdout == want_b, p.stdout + p.stderr
        print("5b) buffers: in/out incremented once; aliasing kept; overlap QCPU_BAD_ARGS; a failed call "
              "leaves the buffer untouched")

        # 6. build errors
        for decl, body, msg in (("int f(int *p);", "int f(int *p) { return *p; }", "needs QPU_IN"),
                                ("struct s; int f(struct s x);", "", "integers only"),
                                ("double f(int x);", "", "return type"),
                                ("int twice(int x); int nothere(int x);", "int twice(int x) { return 2 * x; }", "not defined"),
                                ("int hello(int x);", "#include <stdio.h>\nint hello(int x) { printf(\"%d\", x); return x; }", "cannot print")):
            open(hdr, "w").write(decl + "\n")
            open(src, "w").write(body + "\n")
            p = sh(TOOL + [src, "--header", hdr, "-o", os.path.join(d, "x.so")], check=False)
            assert p.returncode and msg in p.stderr, (decl, p.stderr)
        print("6) build errors reported: pointer without annotation, struct parameter, double return, "
              "missing function, printf in the library")

        # 7. Windows cross-build
        mingw = shutil.which("x86_64-w64-mingw32-gcc")
        if not mingw:
            print("7) Windows: SKIPPED (no x86_64-w64-mingw32-gcc)")
        else:
            wd = os.path.join(d, "win")
            dll = os.path.join(wd, "mathlib_qpu.dll")
            sh(TOOL + [os.path.join(EX, "mathlib.c"), "--header", os.path.join(EX, "mathlib.h"), "--target", "windows", "-o", dll])
            exe = os.path.join(wd, "main.exe")
            sh([mingw, "-Wall", "-Werror", "-I", RT, "-I", EX, os.path.join(EX, "main.c"), os.path.join(wd, "mathlib_qpu.lib"), "-o", exe])
            exports = sh(["x86_64-w64-mingw32-objdump", "-p", dll]).stdout
            table = exports.split("[Ordinal/Name Pointer] Table")[1].split("\n\n")[0]
            names = sorted(line.split("]")[1].strip() for line in table.strip().splitlines())
            fns = sorted(["gcd", "modexp", "is_prime", "sum", "sort", "squares"])
            assert names == sorted(fns + ["qpu_last_status", "qpu_last_message", "qcpu_last_call", "qcpu_status_name", "qcpu_search"]), names
            block = sh(["x86_64-w64-mingw32-objdump", "-p", exe]).stdout.split("DLL Name: mathlib_qpu.dll")[1].split("DLL Name:")[0]
            assert all(f" {n}\n" in block for n in fns), block
            deps = sorted({l.split(":")[1].strip() for l in exports.splitlines() if "DLL Name:" in l})
            ran = "not run (no Wine)"
            wine = shutil.which("wine") or ("/usr/lib/wine/wine64" if os.path.exists("/usr/lib/wine/wine64") else None)
            if wine:
                wenv = dict(os.environ, WINEDEBUG="-all", WINEPREFIX=os.path.join(d, "wine"))
                w = subprocess.run([wine, "cmd", "/c", "exit"], capture_output=True, timeout=300, env=wenv)
                if w.returncode:
                    ran = "not run (Wine does not start here)"
                else:
                    p = subprocess.run([wine, exe], capture_output=True, text=True, env=wenv, timeout=600)
                    assert p.stdout.replace("\r\n", "\n") == want, p.stdout + p.stderr
                    ran = "main.exe under Wine prints the same as the CPU build"
            print(f"7) Windows: DLL exports exactly the {len(fns)} functions (+5 runtime queries) and needs only "
                  f"{', '.join(deps)}; import library links; main.exe imports from mathlib_qpu.dll; {ran}")
    print("PASS")


if __name__ == "__main__":
    main()
