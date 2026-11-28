# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Quantag IT Solutions GmbH
"""The installation and its two commands, qarm-run and qarm-cc (packaging/).

  1. The shell scripts parse (sh -n; dash too when it is there).
  2. (Linux, QEMU 10.1+ with plugins as for test_qarm_qemu) packaging/install.sh into a fresh prefix with that QEMU
     bundled, then from another directory: qarm-run --info finds the bundled QEMU, both plugins and the oracle
     helper; qarm-cc and qarm-run run ghz.c; the conformance program passes all 16 tests on AArch64, and on x86-64
     on an x86-64 host; --minimal gives Core and Circuit; --cpu runs it as a plain CPU; --exec under qarm_exec.py;
     --print shows the plugin's options; through a symbolic link to qarm-run too; a file that is not a program, and
     an unknown option, exit 2 with the reason.
Run: python3 -m tests.test_packaging"""
import os, shutil, subprocess, sys, tempfile
from tests.test_qarm_exec import ROOT, sh, toolchain

PK = os.path.join(ROOT, "packaging")


def main():
    if os.name == "nt":
        print("skipped: the launcher is a POSIX shell script (Linux, macOS; on Windows, the container image)")
        print("PASS")
        return
    for f in ("qarm-run", "qarm-cc", "install.sh", "build.sh", "smoke.sh"):
        for shell in ("sh", "dash"):
            if shutil.which(shell):
                sh([shell, "-n", os.path.join(PK, f)])
    print("1) qarm-run, qarm-cc, install.sh, build.sh, smoke.sh parse")
    if sys.platform != "linux":
        print("2) skipped: QEMU's user mode runs on Linux")
        print("PASS")
        return
    from tests.test_qarm_qemu import find_qemu
    q, inc, why = find_qemu()
    if why or not toolchain():
        print("2) skipped: " + (why or "no arm64 C compiler"))
        print("PASS")
        return
    with tempfile.TemporaryDirectory() as d:
        prefix, work = os.path.join(d, "qarm"), os.path.join(d, "work")
        os.mkdir(work)
        sh(["sh", os.path.join(PK, "install.sh"), "--prefix", prefix, "--qemu-include", inc, "--bundle-qemu",
            os.path.dirname(os.path.dirname(os.path.realpath(q))), "--version", "test"])
        run = lambda *a, **kw: sh([os.path.join(prefix, "bin", a[0])] + list(a[1:]), cwd=work, **kw)
        info = run("qarm-run", "--info").stdout
        assert os.path.join(prefix, "libexec", "qarm", "qemu-aarch64") in info and "libqarm-minimal.so" in info, info
        oracle = "oracle helper: off" not in info
        share = os.path.join(prefix, "share", "qarm")
        run("qarm-cc", os.path.join(share, "examples", "ghz.c"), "-o", "ghz")
        assert "other 0 of 2000 shots (on the coprocessor)" in run("qarm-run", "-q", "./ghz").stdout
        conf = os.path.join(share, "conformance", "qarm_conformance.c")
        run("qarm-cc", conf, "-o", "conf")
        full = "17 passed, 0 failed, 0 skipped" if oracle else "9 passed, 0 failed, 8 skipped"
        assert full in run("qarm-run", "-q", "conf").stdout
        archs = ["AArch64"]
        if os.uname().machine == "x86_64" and os.path.exists(os.path.join(prefix, "libexec", "qarm", "qemu-x86_64")):
            run("qarm-cc", "--x86-64", conf, "-o", "conf86")
            assert full in run("qarm-run", "-q", "./conf86").stdout
            archs.append("x86-64")
        assert "5 passed, 0 failed, 12 skipped" in run("qarm-run", "--minimal", "./conf").stdout
        assert "no runtime" in run("qarm-run", "--cpu", "./conf").stdout
        how = ""
        try:
            import unicorn  # noqa: F401
            assert "0 failed" in run("qarm-run", "--exec", "-q", "./conf").stdout
            how = ", --exec (qarm_exec.py)"
        except ImportError:
            pass
        cmd = run("qarm-run", "--print", "-b", "simulator", "./conf").stdout
        assert "-plugin " + os.path.join(prefix, "lib", "qarm", "libqarm-qemu.so") + ",backend=simulator" in cmd, cmd
        os.symlink(os.path.join(prefix, "bin", "qarm-run"), os.path.join(work, "qr"))
        assert full in sh([os.path.join(work, "qr"), "-q", "./conf"], cwd=work).stdout
        open(os.path.join(work, "notes.txt"), "w").write("not a program\n")
        p = run("qarm-run", "notes.txt", check=False)
        assert p.returncode == 2 and "qarm-run:" in p.stderr, p.stderr
        p = run("qarm-run", "--frobnicate", "./conf", check=False)
        assert p.returncode == 2 and "unknown option" in p.stderr, p.stderr
        print(f"2) install.sh with QEMU bundled; qarm-run from elsewhere: conformance {full.split(',')[0]} on "
              f"{' and '.join(archs)}{'' if oracle else ' (no qoracle: the oracle profile not claimed)'}; --minimal, "
              f"--cpu{how}, --print, a symbolic link; a non-program and an unknown option refused")
    print("PASS")


if __name__ == "__main__":
    main()
