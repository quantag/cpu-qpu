# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Quantag IT Solutions GmbH
"""QPU libraries (qpu_dll.py, qpu_server.py, runtime/): a host program calls functions that run on the QPU.

  1. examples/dll: the host program linked against the generated library prints exactly what the
     same program prints when mathlib.c is compiled for the host, for AArch64, RV64, RV32 and
     x86-64 QPU code. Every call is checked against the CPU by the server (status ok).
  2. QPU_BACKEND=cpu gives the same output.
  3. A result outside the W-bit model: warning on stderr (QPU_STRICT=1: exit status 70).
  4. A function that does not halt within the step limit: message and exit status 70.
  5. The server is restarted between two calls: the library sends its object again.
  6. Header errors are reported: pointer without QPU_IN/OUT, struct parameter, missing function.
Run: python3 -m tests.test_dll [arch,...]"""
import os, subprocess, sys, tempfile, textwrap, time
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EX = os.path.join(ROOT, "examples", "dll")
RT = os.path.join(ROOT, "runtime")


def sh(cmd, env=None, check=True):
    p = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=1200)
    if check and p.returncode:
        raise AssertionError(f"{' '.join(cmd)} failed ({p.returncode}):\n{p.stdout}\n{p.stderr}")
    return p


def build(d, src, header, arch, extra=()):
    lib = os.path.join(d, f"lib{os.path.splitext(os.path.basename(src))[0]}_qpu.so")
    sh([sys.executable, os.path.join(ROOT, "qpu_dll.py"), src, "--header", header, "--arch", arch, "-o", lib, *extra])
    return lib


def host(d, main, libdir, libname, name):
    exe = os.path.join(d, name)
    sh(["cc", "-Wall", "-Werror", "-I", RT, "-I", EX, main, "-L", libdir, "-l" + libname, "-Wl,-rpath," + libdir, "-o", exe])
    return exe


def stop(env):
    sock = env["QPU_SERVER"]
    subprocess.run(["pkill", "-f", f"qpu_server.py --socket {sock}"], capture_output=True)
    for _ in range(50):
        if not os.path.exists(sock):
            break
        time.sleep(0.1)


def main():
    archs = sys.argv[1].split(",") if len(sys.argv) > 1 else ["arm64", "riscv64", "riscv32", "x86_64"]
    with tempfile.TemporaryDirectory() as d:
        env = dict(os.environ, QPU_SERVER=os.path.join(d, "qpu.sock"))
        native = os.path.join(d, "main_cpu")
        sh(["cc", "-Wall", "-I", RT, os.path.join(EX, "main.c"), os.path.join(EX, "mathlib.c"), "-o", native])
        want = sh([native]).stdout
        try:
            for arch in archs:
                ad = os.path.join(d, arch)
                os.makedirs(ad)
                build(ad, os.path.join(EX, "mathlib.c"), os.path.join(EX, "mathlib.h"), arch)
                exe = host(ad, os.path.join(EX, "main.c"), ad, "mathlib_qpu", "main")
                t = time.time()
                p = sh([exe], env)
                assert p.stdout == want, f"{arch}: output differs\n--- QPU\n{p.stdout}--- CPU\n{want}--- stderr\n{p.stderr}"
                assert not p.stderr, f"{arch}: unexpected messages: {p.stderr}"
                print(f"1) {arch}: host program with mathlib on the QPU == host program with mathlib on the CPU "
                      f"({len(want.splitlines())} lines, {time.time() - t:.1f} s)")
                p = sh([exe], dict(env, QPU_BACKEND="cpu"))
                assert p.stdout == want and not p.stderr, arch
                print(f"2) {arch}: QPU_BACKEND=cpu gives the same output")

            # 3./4. results outside the model, a function that never returns
            src = os.path.join(d, "edge.c"); hdr = os.path.join(d, "edge.h")
            open(hdr, "w").write("int twice(int x);\nint spin(int x);\n")
            open(src, "w").write(textwrap.dedent("""\
                #include "edge.h"
                int twice(int x) { return 2 * x; }
                int spin(int x) { volatile int k = x; while (k) k = k + 2; return k; }
                """))
            open(os.path.join(d, "edge_main.c"), "w").write(textwrap.dedent("""\
                #include <stdio.h>
                #include <stdlib.h>
                #include "edge.h"
                int main(int argc, char **argv) {
                    int x = atoi(argv[1]);
                    printf("%d\\n", argc > 2 ? spin(x) : twice(x));
                    return 0;
                }
                """))
            build(d, src, hdr, "arm64", ["--width", "10"])
            exe = os.path.join(d, "edge")
            sh(["cc", "-I", RT, "-I", d, os.path.join(d, "edge_main.c"), "-L", d, "-ledge_qpu", "-Wl,-rpath," + d, "-o", exe])
            p = sh([exe, "50"], env)
            assert p.stdout == "100\n" and not p.stderr, (p.stdout, p.stderr)
            p = sh([exe, "300"], env)                       # 600 does not fit 10 bits
            assert "warning: result may be wrong" in p.stderr and p.returncode == 0, p.stderr
            p = sh([exe, "300"], dict(env, QPU_STRICT="1"), check=False)
            assert p.returncode == 70 and "not exact" in p.stderr, (p.returncode, p.stderr)
            p = sh([exe, "300"], dict(env, QPU_WIDTH="16"))
            assert p.stdout == "600\n" and not p.stderr, (p.stdout, p.stderr)
            print("3) result outside the 10-bit model: warning; QPU_STRICT=1: exit 70; QPU_WIDTH=16: exact")
            p = sh([exe, "1", "spin"], env, check=False)
            assert p.returncode == 70 and "did not finish" in p.stderr, (p.returncode, p.stderr)
            print("4) function that does not halt: " + p.stderr.strip().splitlines()[-1])

            # 5. server restarted: unknown library -> LOAD -> call again
            stop(env)
            p = sh([exe, "21"], env)
            assert p.stdout == "42\n", p.stdout
            print("5) after a server restart the library is sent again and the call succeeds")

            # 6. header errors
            for decl, msg in (("int f(int *p);", "needs QPU_IN"), ("struct s; int f(struct s x);", "integers only"),
                              ("double f(int x);", "return type"), ("int twice(int x); int nothere(int x);", "not defined")):
                open(hdr, "w").write(decl + "\n")
                p = sh([sys.executable, os.path.join(ROOT, "qpu_dll.py"), src, "--header", hdr, "-o", os.path.join(d, "x.so")], check=False)
                assert p.returncode and msg in p.stderr, (decl, p.stderr)
            print("6) header errors reported: pointer without annotation, struct parameter, double return, missing function")
        finally:
            stop(env)
    print("PASS")


if __name__ == "__main__":
    main()
