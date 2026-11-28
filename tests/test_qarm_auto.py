# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Quantag IT Solutions GmbH
"""QArmAuto, the compiler pass that gives search and minimum loops to the coprocessor (compiler/, docs/qarm_auto.md).

  1. The plugin builds for this clang's LLVM (cmake, llvm-config of the same major version).
  2. tests/qarm_auto_cases.c: every loop gets the remark its comment expects: QSEARCH or QMIN (rewritten), missed
     (marked, but not a search or minimum: a counter, side effects, a bound over 65536, a start of 1, a step of 2,
     a maximum, an inverted update, a store), candidate (unmarked). The IR verifies.
  3. The rewritten loops keep their promises, on this CPU with a stand-in coprocessor (tests/qarm_auto_fake.c)
     that answers differently from the loops (the largest solution, the last minimizer), the same way, or not at
     all: on 40 seeded tables, every search answer is a solution and "none" only when there is none, every minimum
     the least with an argument that has it. When it declines, and with the real runtime side on a CPU without a
     coprocessor, the output is byte for byte the program's without the pass.
  4. examples/qarm/auto.c, plain C with QARM_AUTO: on this CPU the same output with and without the pass; under
     qarm_exec.py (arm64) and the QEMU plugin (arm64, and x86-64 on an x86-64 host; QARM_QEMU as for
     test_qarm_qemu) the same answers, from QSEARCH and QMIN.
Run: python3 -m tests.test_qarm_auto"""
import os, re, shutil, subprocess, sys, tempfile
from tests.test_qarm_exec import ROOT, sh

INC = os.path.join(ROOT, "include")
CASES = os.path.join(ROOT, "tests", "qarm_auto_cases.c")
FAKE = os.path.join(ROOT, "tests", "qarm_auto_fake.c")
RUNTIME = os.path.join(ROOT, "compiler", "qarm_auto.c")
DEMO = os.path.join(ROOT, "examples", "qarm", "auto.c")


def llvm_config(v):
    for c in (f"llvm-config-{v}", f"/usr/lib/llvm-{v}/bin/llvm-config", f"/opt/homebrew/opt/llvm@{v}/bin/llvm-config",
              "/opt/homebrew/opt/llvm/bin/llvm-config", "/usr/local/opt/llvm/bin/llvm-config", "llvm-config"):
        p = shutil.which(c)
        if p and subprocess.run([p, "--version"], capture_output=True, text=True).stdout.split(".")[0] == v:
            return p
    return None


def main():
    clang = os.environ.get("QARM_CLANG", "clang")
    if os.name == "nt" or not shutil.which(clang) or not shutil.which("cmake"):
        print("skipped: needs clang and cmake on Linux or macOS")
        print("PASS")
        return
    v = sh([clang, "-dumpversion"]).stdout.strip().split(".")[0]
    cfg = llvm_config(v)
    if not cfg:
        print(f"skipped: no llvm-config for clang {v} (llvm-{v}-dev)")
        print("PASS")
        return
    with tempfile.TemporaryDirectory() as d:
        b = os.path.join(d, "build")
        sh(["cmake", "-S", os.path.join(ROOT, "compiler"), "-B", b, "-DCMAKE_BUILD_TYPE=Release",
            "-DLLVM_DIR=" + sh([cfg, "--cmakedir"]).stdout.strip()])
        sh(["cmake", "--build", b])
        plugin = os.path.join(b, "QArmAuto.so")
        print(f"1) QArmAuto.so for LLVM {v}")
        cc = [clang, "-O1", "-iquote", INC, "-fpass-plugin=" + plugin]

        # 2. remarks
        p = sh(cc + ["-Rpass=qarm-auto", "-Rpass-missed=qarm-auto", "-Rpass-analysis=qarm-auto", "-c", CASES,
                     "-o", os.path.join(d, "c.o")])
        kinds = {}
        for line in p.stderr.splitlines():
            m = re.match(r".*qarm_auto_cases\.c:(\d+):\d+: remark: (.*)", line)
            if m:
                text = m.group(2)
                k = ("QSEARCH" if ": QSEARCH (" in text else "QMIN" if ": QMIN (" in text else
                     "missed" if text.startswith("not a search") else "candidate" if "mark the function" in text else "?")
                kinds.setdefault(int(m.group(1)), []).append(k)
        src = open(CASES).read().splitlines()
        checked = 0
        for n, line in enumerate(src, 1):
            m = re.search(r"expect: (\w+)", line)
            if m and "for (" in line and not line.rstrip().endswith("\\"):   # a macro's lines: by count below
                assert kinds.get(n) == [m.group(1)], (n, line, kinds.get(n))
                checked += 1
        total = {k: sum(v.count(k) for v in kinds.values()) for k in ("QSEARCH", "QMIN", "missed", "candidate", "?")}
        assert total == {"QSEARCH": 6, "QMIN": 5, "missed": 8, "candidate": 11, "?": 0}, total
        bc = os.path.join(d, "c.bc")
        sh(cc + ["-emit-llvm", "-c", CASES, "-o", bc])
        opt = os.path.join(os.path.dirname(cfg), "opt")
        if os.path.exists(opt):
            sh([opt, "-passes=verify", bc, "-o", os.devnull])
        print(f"2) {total['QSEARCH']} searches and {total['QMIN']} minimums rewritten, {total['missed']} marked loops left "
              f"alone with the reason, {total['candidate']} unmarked candidates reported ({checked} by line); the IR verifies")

        # 3. the promises, with a stand-in coprocessor
        plain = os.path.join(d, "plain")
        sh([clang, "-O1", "-iquote", INC, CASES, "-o", plain])
        ref = sh([plain, "40"]).stdout
        fake = os.path.join(d, "fake")
        sh(cc + [CASES, FAKE, "-o", fake])
        for mode in ("largest", "first", "none"):
            out = sh([fake, "40"], env=dict(os.environ, QARM_FAKE=mode)).stdout
            assert out.endswith("all checks passed\n"), (mode, out[-600:])
            if mode == "none":
                assert out == ref, "declined: not the program as written"
            if mode == "largest":
                assert out != ref, "the stand-in's answers never differed from the loops'"
        real = os.path.join(d, "real")
        sh(cc + [CASES, RUNTIME, "-o", real])
        assert sh([real, "40"]).stdout == ref
        print("3) 40 seeded tables: with a coprocessor that answers differently, the same way, or not at all, every "
              "search answer is a solution, \"none\" only when there is none, every minimum the least; declined, and "
              "with the real runtime side on this CPU, byte for byte the program without the pass")

        # 4. examples/qarm/auto.c
        host = os.path.join(d, "auto_host")
        sh(cc + [DEMO, RUNTIME, "-o", host])
        sh([clang, "-O1", "-iquote", INC, DEMO, "-o", plain])
        want = sh([plain]).stdout
        assert sh([host]).stdout == want and "BYRG" in want and "difference 1" in want, want
        ran = ["this CPU"]
        if sys.platform == "linux" and shutil.which("aarch64-linux-gnu-gcc"):
            a64 = os.path.join(d, "auto.a64")
            sh(cc + ["--target=aarch64-linux-gnu", DEMO, RUNTIME, "-o", a64])
            try:
                import unicorn  # noqa: F401
                p = sh([sys.executable, os.path.join(ROOT, "qarm_exec.py"), a64])
                assert p.stdout == want and "QSEARCH consistent" in p.stderr and "QMIN imbalance" in p.stderr, p.stdout + p.stderr
                ran.append("qarm_exec.py")
            except ImportError:
                pass
            from tests.test_qarm_qemu import find_qemu
            q, inc, why = find_qemu()
            if not why:
                qb = os.path.join(d, "qemu")
                sh(["cmake", "-S", os.path.join(ROOT, "runtime", "qemu"), "-B", qb, f"-DQEMU_PLUGIN_INCLUDE={inc}"])
                sh(["cmake", "--build", qb])
                plug = os.path.join(qb, "libqarm-qemu.so") + ",helper=" + os.path.join(ROOT, "tools", "qarm_oracle.py")
                sysroot = [] if os.uname().machine == "aarch64" else ["-L", "/usr/aarch64-linux-gnu"]
                p = sh([q] + sysroot + ["-plugin", plug, a64])
                assert p.stdout == want and "QSEARCH" in p.stderr and "QMIN imbalance" in p.stderr, p.stdout + p.stderr
                ran.append("QEMU (arm64)")
                q86 = os.path.join(os.path.dirname(q), "qemu-x86_64")
                if os.uname().machine == "x86_64" and os.path.exists(q86):
                    x64 = os.path.join(d, "auto.x64")
                    sh(cc + [DEMO, RUNTIME, "-o", x64])
                    p = sh([q86, "-plugin", plug, x64])
                    assert p.stdout == want and "QMIN imbalance" in p.stderr, p.stdout + p.stderr
                    ran.append("QEMU (x86-64)")
        print("4) examples/qarm/auto.c: the same answers on " + ", ".join(ran) + "; QSEARCH and QMIN under the runtimes")
    print("PASS")


if __name__ == "__main__":
    main()
