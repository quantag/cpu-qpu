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
  7. TCP transport (the Windows default) on Linux: endpoint file with a token, readable only by the
     user; the host output is the same; a connection without the token is refused.
  8. Windows: the library and the host program cross-compiled with MinGW. The DLL exports exactly
     the header's functions, the import library links, main.exe imports them from the DLL. (Running
     main.exe needs Windows or Wine; it is run under Wine when Wine works.)
Run: python3 -m tests.test_dll [arch,...]"""
import os, shutil, socket, subprocess, sys, tempfile, textwrap, time
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
    if sock.endswith(".txt") and os.path.exists(sock):
        for _ in range(50):
            if not os.path.exists(sock):
                break
            time.sleep(0.1)
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

            # 7. TCP transport on Linux
            tenv = dict(os.environ, QPU_SERVER=os.path.join(d, "server.txt"))
            exe = os.path.join(d, archs[0], "main")
            p = sh([exe], tenv)
            assert p.stdout == want and not p.stderr, (p.stdout, p.stderr)
            host_, port, token = open(tenv["QPU_SERVER"]).read().split()
            assert host_ == "127.0.0.1" and len(token) == 32 and os.stat(tenv["QPU_SERVER"]).st_mode & 0o077 == 0
            c = socket.create_connection((host_, int(port)), timeout=5)
            c.sendall(b"AUTH wrong\nPING\n")
            assert c.recv(100) == b"", "a connection with a wrong token was answered"
            c.close()
            stop(tenv)
            print("7) TCP transport: same output; endpoint file 127.0.0.1 port token (mode 600); wrong token refused")

            # 8. Windows cross-build
            mingw = shutil.which("x86_64-w64-mingw32-gcc")
            if not mingw:
                print("8) Windows: SKIPPED (no x86_64-w64-mingw32-gcc)")
            else:
                wd = os.path.join(d, "win")
                os.makedirs(wd)
                dll = os.path.join(wd, "mathlib_qpu.dll")
                sh([sys.executable, os.path.join(ROOT, "qpu_dll.py"), os.path.join(EX, "mathlib.c"), "--header",
                    os.path.join(EX, "mathlib.h"), "--target", "windows", "-o", dll])
                exe = os.path.join(wd, "main.exe")
                sh([mingw, "-Wall", "-Werror", "-I", RT, "-I", EX, os.path.join(EX, "main.c"), os.path.join(wd, "mathlib_qpu.lib"), "-o", exe])
                exports = sh(["x86_64-w64-mingw32-objdump", "-p", dll]).stdout
                table = exports.split("[Ordinal/Name Pointer] Table")[1].split("\n\n")[0]
                names = sorted(line.split("]")[1].strip() for line in table.strip().splitlines())
                want_names = sorted(["gcd", "modexp", "is_prime", "sum", "sort", "squares", "report", "qpu_last_status", "qpu_last_message"])
                assert names == want_names, names
                imports = sh(["x86_64-w64-mingw32-objdump", "-p", exe]).stdout
                block = imports.split("DLL Name: mathlib_qpu.dll")[1].split("DLL Name:")[0]
                assert all(f" {n}" in block for n in want_names[:7] if not n.startswith("qpu_")), block
                wine = shutil.which("wine") or ("/usr/lib/wine/wine64" if os.path.exists("/usr/lib/wine/wine64") else None)
                ran = "not run (no Wine)"
                if wine:
                    w = subprocess.run([wine, "cmd", "/c", "exit"], capture_output=True, timeout=300,
                                       env=dict(os.environ, WINEDEBUG="-all", WINEPREFIX=os.path.join(d, "wine")))
                    ran = "not run (Wine does not start here)" if w.returncode else "run under Wine: see below"
                    if not w.returncode:
                        wenv = dict(os.environ, WINEDEBUG="-all", WINEPREFIX=os.path.join(d, "wine"),
                                    QPU_SERVER="Z:" + os.path.join(d, "winserver.txt").replace("/", "\\"))
                        srv = subprocess.Popen([sys.executable, os.path.join(ROOT, "qpu_server.py"), "--socket",
                                                os.path.join(d, "winserver.txt")])
                        try:
                            for _ in range(100):
                                if os.path.exists(os.path.join(d, "winserver.txt")):
                                    break
                                time.sleep(0.1)
                            p = subprocess.run([wine, exe], capture_output=True, text=True, env=wenv, timeout=600)
                            assert p.stdout.replace("\r\n", "\n") == want, p.stdout + p.stderr
                            ran = "main.exe under Wine prints the same as the CPU build"
                        finally:
                            srv.terminate()
                print(f"8) Windows: DLL exports exactly the {len(want_names)} functions, import library links, "
                      f"main.exe imports them from mathlib_qpu.dll; {ran}")
        finally:
            stop(env)
    print("PASS")


if __name__ == "__main__":
    main()
