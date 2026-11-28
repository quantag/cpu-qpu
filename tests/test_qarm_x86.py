# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Quantag IT Solutions GmbH
"""Q-ARM on x86-64: the same packets behind JMP +30 (docs/qarm_isa.md 2.1), oracles from x86-64 machine code
(qoracle's x64 front end), the QEMU plugin in qemu-x86_64, and tools/qarm_patch.py on x86-64 ELF binaries.

  1. The examples built for x86-64 run natively on an x86-64 host: every packet is jumped over, and the classical
     paths of qarm.h give the answers (the conformance program reports no runtime).
  2. Under qemu-x86_64 with the plugin: tiny_search (QSEARCH), shor (QPERIOD, 15 and 21 factored from x86-64
     modexp code), hidden (QSIMON, QBV), collision (QCOLLISION), h2_vqe (VQE to 1e-6), maxcut_qaoa (QVSAMPLE), ghz
     (QCIRC), and the conformance program: 17 of 17.
  3. tools/qarm_patch.py on legacy_search.c built for x86-64 without qarm.h: --scan finds both loops, --auto patches
     them; natively the original loops, under the plugin QSEARCH and QMIN: the same output.
Needs gcc or clang for x86-64 and, for 2 and 3, QEMU 10.1+ with plugins: QARM_QEMU_X86 = its qemu-x86_64 (else
qemu-x86_64 next to QARM_QEMU, or on PATH), QARM_QEMU_INCLUDE as for test_qarm_qemu. Run: python3 -m tests.test_qarm_x86"""
import os, re, shutil, subprocess, sys, tempfile
from tests.test_qarm_exec import ROOT, sh

EX = os.path.join(ROOT, "examples", "qarm")
INC = os.path.join(ROOT, "include")


def compiler():
    """A C compiler for x86-64 Linux, and whether its programs run here."""
    host = os.uname().machine if hasattr(os, "uname") else ""
    if sys.platform == "linux" and host == "x86_64" and shutil.which("gcc"):
        return ["gcc"], True
    if shutil.which("x86_64-linux-gnu-gcc"):
        return ["x86_64-linux-gnu-gcc"], False
    return None, False


def find_qemu():
    q = os.environ.get("QARM_QEMU_X86")
    if not q and os.environ.get("QARM_QEMU"):
        q = os.path.join(os.path.dirname(os.environ["QARM_QEMU"]), "qemu-x86_64")
    q = q if q and os.path.exists(q) else shutil.which("qemu-x86_64")
    if not q:
        return None, None
    v = subprocess.run([q, "--version"], capture_output=True, text=True).stdout
    m = re.search(r"version (\d+)\.(\d+)", v)
    if not m or (int(m.group(1)), int(m.group(2))) < (10, 1):
        return None, None                                  # the plugin API needs QEMU 10.1
    inc = os.environ.get("QARM_QEMU_INCLUDE") or os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(q))),
                                                               "include")
    if not os.path.exists(os.path.join(inc, "qemu-plugin.h")):
        return None, None
    return q, inc


def main():
    if sys.platform != "linux":
        print("skipped: x86-64 Linux programs")
        return
    cc, runs = compiler()
    if not cc:
        print("skipped: no x86-64 C compiler")
        return
    want = {"tiny": "x = 3", "shor": "21 = 7 x 3", "hidden": "hidden string is 0x35", "collision": "tag(0x",
            "h2": "difference 0.0 mHa", "maxcut": "largest cut is 10", "ghz": "other 0 of 2000",
            "conformance": None}
    srcs = {"tiny": "tiny_search.c", "shor": "shor.c", "hidden": "hidden.c", "collision": "collision.c",
            "h2": "h2_vqe.c", "maxcut": "maxcut_qaoa.c", "ghz": "ghz.c"}
    with tempfile.TemporaryDirectory() as d:
        exe = {}
        for name in want:
            src = os.path.join(ROOT, "tests", "conformance", "qarm_conformance.c") if name == "conformance" \
                else os.path.join(EX, srcs[name])
            exe[name] = os.path.join(d, name)
            sh(cc + ["-O1", "-iquote", INC, "-iquote", EX, src, "-o", exe[name], "-lm"])
        if runs:
            for name, w in want.items():
                p = sh([exe[name]])
                assert ("no runtime" if w is None else w) in p.stdout, (name, p.stdout)
            assert "simulated here" in sh([exe["ghz"]]).stdout
            print("1) the examples as x86-64 programs, natively: every packet jumped over, the classical answers "
                  "(the conformance program: no runtime)")
        else:
            print("1) skipped: not an x86-64 host")

        q, inc = find_qemu()
        if not q:
            print("2) and 3) skipped: no qemu-x86_64 10.1+ with plugins (set QARM_QEMU_X86)")
            print("PASS")
            return
        b = os.path.join(d, "build")
        sh(["cmake", "-S", os.path.join(ROOT, "runtime", "qemu"), "-B", b, f"-DQEMU_PLUGIN_INCLUDE={inc}"])
        sh(["cmake", "--build", b])
        plugin = os.path.join(b, "libqarm-qemu.so")
        qemu = lambda: [q, "-plugin", plugin + ",quiet=on"]
        os.environ["QARM_SEED"] = "2"
        for name, w in want.items():
            p = sh(qemu() + [exe[name]], check=False)
            if name == "conformance":
                assert p.returncode == 0 and "17 passed, 0 failed, 0 skipped" in p.stdout, p.stdout
            else:
                assert p.returncode == 0 and w in p.stdout, (name, p.stdout, p.stderr[-2000:])
        p = sh([q, "-plugin", plugin] + [exe["shor"]])
        assert len(re.findall(r"QPERIOD \w+, \d+ bits: compute oracle of \d+ qubits", p.stderr)) == 2, p.stderr
        print("2) under qemu-x86_64 with the plugin: QSEARCH, QPERIOD (Shor: 15 and 21 from x86-64 modexp code), QSIMON, "
              "QBV, QCOLLISION, VQE (H2 to 1e-6), QVSAMPLE, QCIRC; conformance 17 of 17")

        lexe, lout = os.path.join(d, "legacy"), os.path.join(d, "legacy.qarm")
        sh(cc + ["-O1", os.path.join(EX, "legacy_search.c"), "-o", lexe])
        patch = [sys.executable, os.path.join(ROOT, "tools", "qarm_patch.py")]
        p = sh(patch + [lexe, "--scan"])
        assert "--search find_key=valid_key:12" in p.stdout and "--min cheapest_route=route_cost:12" in p.stdout, p.stdout
        sh(patch + [lexe, "--auto", "-o", lout])
        out = "key: 90 (valid)\ncheapest route: 1381 (cost 0)\n"
        if runs:
            assert sh([lout]).stdout == out
        p = sh([q, "-plugin", plugin, lout])
        assert p.stdout == out and "QSEARCH valid_key, 12 bits" in p.stderr and "QMIN route_cost" in p.stderr, p
        print("3) qarm_patch on x86-64: legacy_search's loops found and patched (JMP to a trampoline after ENDBR64); "
              + ("natively the original loops, " if runs else "") + "under the plugin QSEARCH and QMIN: the same output")
    print("PASS")


if __name__ == "__main__":
    main()
