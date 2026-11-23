# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Quantag IT Solutions GmbH
"""Linked macOS arm64 executables (Mach-O, as built on an Apple Silicon Mac) on the quantum machine.

Off a Mac, the executables are built with clang --target=arm64-apple-macos and ld64.lld against a stub
libSystem (tests/macos/libSystem.tbd) and two stand-in headers. Both binding formats are covered: classic
dyld info and chained fixups (the default of Apple's linker for current macOS). Every run must MATCH the
CPU reference, and its printed output and exit status must equal those of the same C program compiled
natively on this host.
Run: python3 -m tests.test_macos"""
import json, os, shutil, subprocess, sys, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAC = os.path.join(ROOT, "tests", "macos")
CASES = [  # source, optimisation levels, extra flags
    (os.path.join(ROOT, "examples", "hello.c"), ("O1", "O2"), []),
    (os.path.join(ROOT, "examples", "factor.c"), ("O1",), []),
    (os.path.join(MAC, "globals.c"), ("O1", "O2"), []),
    (os.path.join(MAC, "funcptr.c"), ("O1", "O2"), []),
    (os.path.join(MAC, "exit.c"), ("O1",), []),
    (os.path.join(MAC, "stackprot.c"), ("O0", "O1"), ["-fstack-protector-all"]),
]
LD = shutil.which("ld64.lld") or shutil.which("ld64.lld-18")


def build(src, o, flags, out, chained):
    obj = out + ".o"
    subprocess.run(["clang", "--target=arm64-apple-macos13", f"-{o}", "-isystem", os.path.join(MAC, "include"),
                    "-c", src, "-o", obj] + flags, check=True)
    subprocess.run([LD, "-arch", "arm64", "-platform_version", "macos", "13.0", "13.0", "-o", out, obj,
                    "-L", MAC, "-lSystem"] + (["-fixup_chains"] if chained else []), check=True)


def native(src, d):
    exe = os.path.join(d, os.path.basename(src) + ".native")
    if subprocess.run(["cc", "-O1", "-w", "-o", exe, src], capture_output=True).returncode:
        return None
    p = subprocess.run([exe], capture_output=True, text=True)
    return p.stdout, p.returncode


if __name__ == "__main__":
    if not LD:
        print("SKIP: ld64.lld not found (needed to build macOS executables off a Mac)")
        sys.exit(0)
    fails = 0
    with tempfile.TemporaryDirectory() as d:
        for src, levels, flags in CASES:
            want = native(src, d)
            for o in levels:
                for chained in (False, True):
                    exe = os.path.join(d, f"{os.path.basename(src)}.{o}.{'chained' if chained else 'dyldinfo'}")
                    build(src, o, flags, exe, chained)
                    p = subprocess.run([sys.executable, os.path.join(ROOT, "cpu_qpu.py"), exe, "--report",
                                        os.path.join(d, "r")], capture_output=True, text=True)
                    rep = json.load(open(os.path.join(d, "r.json"))) if os.path.exists(os.path.join(d, "r.json")) else {}
                    out = rep.get("result", {}).get("output", {})
                    ok = rep.get("verdict", "").startswith("MATCH") and out.get("match") and not rep.get("cpu", {}).get("issues")
                    if want is not None:
                        ok = ok and out.get("qpu") == want[0] and p.returncode == want[1]
                    fails += not ok
                    mc = rep.get("machine", {})
                    print(f"{'PASS' if ok else 'FAIL'} {os.path.basename(src)} -{o} {'chained fixups' if chained else 'dyld info'}: "
                          f"exit {p.returncode}, {mc.get('steps')} cycles, {mc.get('qubits')} qubits"
                          + ("" if ok else f"\n     {rep.get('verdict') or p.stderr[-400:]}"))
                    os.path.exists(os.path.join(d, "r.json")) and os.remove(os.path.join(d, "r.json"))
    print("all passed" if not fails else f"{fails} failed")
    sys.exit(1 if fails else 0)
