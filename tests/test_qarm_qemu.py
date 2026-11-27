# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Quantag IT Solutions GmbH
"""Q-ARM under QEMU: runtime/qemu's plugin in qemu-aarch64 (docs/qarm_v2.md section 8). The binaries are the ordinary
arm64 builds that also run on a plain CPU and under qarm_exec.py.

  1. h2_vqe: the same output as under qarm_exec.py; every ansatz operation (3 qubits) to 1e-9; with -cpu cortex-a57
     (no SVE) and -cpu max (SVE: the vector registers are z0, z1).
  2. examples/qarm/vqe_from_file.c, which reads its Hamiltonian with fopen and sorts with qsort: runs under QEMU;
     qarm_exec.py stops at fopen.
  3. QSEARCH under the plugin, from the guest program's own machine code (tools/qarm_oracle.py, then Grover in
     libqarm): tiny_search finds x = 3; Code Breaker finds the secret with its ~1100-qubit oracle; without the
     helper, QAVAIL reports no QSEARCH and the program searches classically.
  4. The simulator backend (plugin option backend=simulator); without the plugin, the classical path.
  5. (libcurl, qiskit, qiskit-aer) the ibm backend against tests/ibm_mock.py: 3 qubits within 5 standard errors.
  6. examples/qarm/molecule_vqe.c, LiH (L-BFGS with QGRAD): the same output as under qarm_exec.py.
  7. examples/qarm/partition.c: QMIN finds a split of difference 1, QCOUNT counts the nearly balanced splits (32)
     within its error bound, both from the guest's own functions.
  8. QPERIOD, QSIMON, QBV: examples/qarm/shor.c factors 15 and 21, hidden.c finds its two secrets.
Needs QEMU 10.1 or newer built with plugins: QARM_QEMU = its qemu-aarch64, QARM_QEMU_INCLUDE = the directory with
qemu-plugin.h (else qemu-aarch64 on PATH and ../include next to it). Run: python3 -m tests.test_qarm_qemu"""
import json, os, re, shutil, subprocess, sys, tempfile
from tests.test_qarm_exec import ROOT, EXEC, toolchain, sh
from tests.test_qarm_vqe import ALL_OPS, EXACT

EX = os.path.join(ROOT, "examples", "qarm")


def find_qemu():
    q = os.environ.get("QARM_QEMU") or shutil.which("qemu-aarch64")
    if not q:
        return None, None, "no qemu-aarch64 (set QARM_QEMU)"
    v = subprocess.run([q, "--version"], capture_output=True, text=True).stdout
    m = re.search(r"version (\d+)\.(\d+)", v)
    if not m or (int(m.group(1)), int(m.group(2))) < (10, 1):
        return None, None, f"{q} is QEMU {m.group(0) if m else '?'}; the plugin needs 10.1 or newer (set QARM_QEMU)"
    inc = os.environ.get("QARM_QEMU_INCLUDE") or os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(q))),
                                                               "include")
    if not os.path.exists(os.path.join(inc, "qemu-plugin.h")) and not os.path.exists(os.path.join(inc, "qemu", "qemu-plugin.h")):
        return None, None, f"no qemu-plugin.h in {inc} (set QARM_QEMU_INCLUDE)"
    return q, inc, None


def main():
    if sys.platform != "linux":
        print("skipped: QEMU's user mode runs Linux programs on Linux")
        return
    q, inc, why = find_qemu()
    tc = toolchain()
    if why or not tc:
        print("skipped: " + (why or "no arm64 C compiler"))
        return
    cc, native = tc
    sysroot = [] if os.uname().machine in ("aarch64", "arm64") else ["-L", "/usr/aarch64-linux-gnu"]
    with tempfile.TemporaryDirectory() as d:
        b = os.path.join(d, "build")
        sh(["cmake", "-S", os.path.join(ROOT, "runtime", "qemu"), "-B", b, f"-DQEMU_PLUGIN_INCLUDE={inc}"])
        sh(["cmake", "--build", b])
        plugin = os.path.join(b, "libqarm-qemu.so")
        qemu = lambda opts="": [q] + sysroot + ["-plugin", plugin + opts]

        exe = {}
        for name, src in (("pi", os.path.join(EX, "pi.c")), ("shor", os.path.join(EX, "shor.c")), ("hidden", os.path.join(EX, "hidden.c")),
                          ("part", os.path.join(EX, "partition.c")), ("cb", os.path.join(EX, "codebreaker_search.c")),
                          ("lih", os.path.join(EX, "molecule_vqe.c")), ("h2", os.path.join(EX, "h2_vqe.c")), ("all", None), ("tiny", os.path.join(EX, "tiny_search.c")),
                          ("file", os.path.join(EX, "vqe_from_file.c"))):
            if src is None:
                src = os.path.join(d, "all_ops.c")
                open(src, "w").write(ALL_OPS)
            exe[name] = os.path.join(d, name)
            sh(cc + ["-O1", "-iquote", os.path.join(ROOT, "include"), "-iquote", EX, src, "-o", exe[name], "-lm"])

        ref = sh(EXEC + ["-q", exe["h2"]]).stdout
        for cpu in ("cortex-a57", "max"):
            p = sh([q, "-cpu", cpu] + sysroot + ["-plugin", plugin + ",quiet=on", exe["h2"]])
            assert p.stdout == ref, (cpu, p.stdout, ref)
        a = [float(v) for v in sh(EXEC + ["-q", exe["all"]]).stdout.split()]
        c = [float(v) for v in sh(qemu(",quiet=on") + [exe["all"]]).stdout.split()]
        assert len(c) == 6 and all(abs(x - y) < 1e-9 for x, y in zip(a, c)), (a, c)
        e = float(re.search(r"\) = (-?\d+\.\d+) Ha", ref).group(1))
        print(f"1) h2_vqe under qemu-aarch64 with the plugin: the same output as under qarm_exec.py (E = {e:.6f} Ha), "
              "on cortex-a57 and on max (SVE); every operation, 3 qubits: to 1e-9")

        terms = os.path.join(EX, "h2_terms.txt")
        p = sh(qemu(",quiet=on") + [exe["file"], terms])
        m = re.search(r"5 terms from .*; lowest of 64 energies: (-?\d+\.\d+) Ha", p.stdout)
        assert m and abs(float(m.group(1)) - EXACT) < 1e-3, p.stdout
        r = sh(EXEC + [exe["file"], terms], check=False)
        assert r.returncode == 70 and "fopen()" in r.stderr, r.stderr
        print("2) vqe_from_file (fopen, fgets, sscanf, malloc, qsort, snprintf): runs under QEMU; qarm_exec.py "
              "stops at fopen")

        p = sh(qemu() + [exe["tiny"]])
        assert p.stdout == "Q-ARM QSEARCH: x = 3\n" and "the phase oracle of is_three from its machine code" in p.stderr, p
        p = sh(qemu() + [exe["cb"]])
        m = re.search(r"QSEARCH consistent, 8 bits: oracle of (\d+) qubits and (\d+) gates", p.stderr)
        assert m and "the secret code: BYRG" in p.stdout, (p.stdout, p.stderr)
        assert "the phase oracle of consistent from its machine code" in p.stderr, p.stderr
        p = sh(qemu(",helper=/nonexistent/qarm_oracle.py") + [exe["tiny"]])
        assert p.stdout == "classical search: x = 3\n", p
        print(f"3) QSEARCH under the plugin from the guest's own machine code: tiny_search x = 3; Code Breaker BYRG "
              f"with a {m.group(1)}-qubit, {m.group(2)}-gate oracle of consistent(); without the helper, the classical "
              "path")

        p = subprocess.run(qemu(",quiet=on,backend=simulator") + [exe["all"]], capture_output=True, text=True,
                           env=dict(os.environ, QARM_SEED="3"))
        m = [float(v) for v in p.stdout.split()]
        worst = max(abs(x - y) for x, y in zip(m[:5], a[:5]))
        assert p.returncode == 0 and 0 < m[5] < 0.05 and worst < 5 * m[5], (m, a, p.stderr)
        plain = sh([q] + sysroot + [exe["h2"]]).stdout
        assert plain == ref.replace("energies from the quantum coprocessor (Q-ARM)", "exact energies on this CPU"), plain
        print(f"4) backend=simulator: within {worst / m[5]:.1f} standard errors; without the plugin, the classical path")

        try:
            import qiskit, qiskit_aer, qiskit_ibm_runtime  # noqa: F401
            from tests.test_libqarm import has_curl, compiler
            curl = compiler() and has_curl(compiler(), d)
        except ImportError as err:
            print(f"5) skipped: {err.name} is not installed")
            curl = None
        if curl is False:
            print("5) skipped: no libcurl")
        elif curl:
            import warnings
            warnings.filterwarnings("ignore")
            from tests.ibm_mock import Mock
            mock = Mock(noise=False)
            cfg = os.path.join(d, "ibm.json")
            json.dump({"backend": "ibm", "shots": 20000, "options": mock.options()}, open(cfg, "w"))
            p = sh(qemu(f",config={cfg}") + [exe["all"]])
            m = [float(v) for v in p.stdout.split()]
            worst = max(abs(x - y) for x, y in zip(m[:5], a[:5]))
            jobs = re.findall(r"job (mock-\w+) sent to ibm_fez", p.stderr)
            assert 0 < m[5] < 0.02 and worst < 5 * m[5] and len(jobs) == 2, (m, p.stderr)
            mock.close()
            print(f"5) ibm via the mock of IBM's REST API: 3 qubits, {len(jobs)} jobs, within {worst / m[5]:.1f} "
                  "standard errors")
        p, r = sh(qemu(",quiet=on") + [exe["lih"]]), sh(EXEC + ["-q", exe["lih"]])
        assert p.stdout == r.stdout and "VQE - exact:   0.000000 mHa" in p.stdout, (p.stdout, r.stdout)
        print("6) molecule_vqe, LiH (10 qubits, L-BFGS with QGRAD): the same output under QEMU and qarm_exec.py")

        os.environ["QARM_SEED"] = "2"                    # QCOUNT's bound holds with probability >= 8/pi^2: seeded
        p = sh(qemu() + [exe["part"]])
        del os.environ["QARM_SEED"]
        n, err = re.search(r"about (\d+) \(\+- ([\d.]+); QCOUNT\)", p.stdout).groups()
        assert "difference 1\n" in p.stdout and "QMIN on the quantum coprocessor" in p.stdout, p.stdout
        assert abs(int(n) - 32) <= float(err) and "the compute oracle of imbalance" in p.stderr, (p.stdout, p.stderr)
        print(f"7) partition: QMIN a split of difference 1 (Durr-Hoyer, imbalance()'s compute oracle); QCOUNT about "
              f"{n} +- {err} nearly balanced splits (exactly 32)")

        p, p2 = sh(qemu() + [exe["shor"]]), sh(qemu() + [exe["hidden"]])
        assert "15 = 3 x 5" in p.stdout and "21 = 7 x 3" in p.stdout and "QPERIOD on the quantum" in p.stdout, p
        assert "shift is 0x2d (QSIMON)" in p2.stdout and "string is 0x35 (QBV" in p2.stdout, p2
        p = sh(qemu(",quiet=on") + [exe["pi"]])
        est, err = (float(v) for v in re.search(r"pi is about ([\d.]+) \+- ([\d.]+)", p.stdout).groups())
        assert abs(est - 3.1475) <= err, p.stdout
        print(f"8) shor: 15 = 3 x 5 and 21 = 7 x 3 with QPERIOD on modexp's oracle; hidden: QSIMON 0x2d, QBV 0x35; "
              f"pi: QAMPEST {est} +- {err} (the grid: 3.1475)")
    print("PASS")


if __name__ == "__main__":
    main()
