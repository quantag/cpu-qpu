# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Quantag IT Solutions GmbH
"""Run every test and experiment and write docs/test_results.md: seeds, tool versions, commit,
counts per outcome class including discarded runs. Takes about an hour (the C suites dominate).
Run: python3 tools/test_report.py [--quick]"""
import json, os, platform, re, subprocess, sys, time
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
quick = "--quick" in sys.argv


def sh(cmd):
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, shell=isinstance(cmd, str))
    return p.returncode, p.stdout + p.stderr


def ver(mod):
    try:
        import importlib.metadata as md
        return md.version(mod)
    except Exception:
        return "?"


commit = sh("git rev-parse HEAD")[1].strip()
dirty = bool(sh("git status --porcelain -- qcpu arm_qpu.py cpu_qpu.py tests tools include")[1].strip())
L = ["# Test results", "", f"Generated {time.strftime('%Y-%m-%d %H:%M %Z')} by `tools/test_report.py`.", "",
     f"- code: commit `{commit}`" + (" (with uncommitted changes)" if dirty else ""),
     f"- Python {platform.python_version()} on {platform.platform()}",
     f"- qiskit {ver('qiskit')}, qiskit-aer {ver('qiskit-aer')}, unicorn {ver('unicorn')}, lief {ver('lief')}, capstone {ver('capstone')}",
     f"- {sh('clang --version')[1].splitlines()[0]}",
     f"- {sh('aarch64-linux-gnu-gcc --version')[1].splitlines()[0]}",
     f"- {sh('gcc --version')[1].splitlines()[0]} (x86-64, {platform.machine()} host)", "",
     "Outcome classes: **match** (QPU = CPU after every cycle, model check clean); **match-flagged** (equal, "
     "but the model check reported values outside the W-bit model); **flagged** (different, and the model "
     "check reported an issue at or before the first cycle where they differ); **UNFLAGGED** (different, and "
     "the model check silent or only about a later instruction: a bug; required to be 0); "
     "**discarded** (the CPU run did not halt normally, the program did not assemble, or it is too long).", ""]

sections = [
    ("Circuit fingerprints (no front end changed by accident)", [sys.executable, "-m", "tests.test_fingerprint"]),
    ("Differential fuzzing", [sys.executable, "-m", "tests.test_fuzz", "60" if quick else "300", "7", "8,12,16"]),
    ("Boundary cases", [sys.executable, "-m", "tests.test_edges", "8,12,16"]),
    ("Fast simulator vs full circuit vs Aer", [sys.executable, "-m", "tests.test_simulators", "30" if quick else "100", "5"]),
    ("Coherence: superposition of two programs", [sys.executable, "-m", "tests.test_coherence"]),
    ("Quantum branching", [sys.executable, "-m", "tests.test_branching"]),
    ("Grover search with compiled C oracles", [sys.executable, "-m", "tests.test_grover"]),
    ("Shor's algorithm with modexp compiled from C", [sys.executable, "-m", "tests.test_shor"]),
    ("Interpreter vs specialised circuit", [sys.executable, "tools/compare_specialized.py"]),
    ("RISC-V: decoder, traps, boundary cases, random programs",
     [sys.executable, "-m", "tests.test_riscv", "40" if quick else "150", "7", "8" if quick else "8,12,16"]),
    ("x86-64: decoder, circuit against the instruction model, traps, boundary cases, random programs",
     [sys.executable, "-m", "tests.test_x86", "40" if quick else "150", "7", "8" if quick else "8,12,16"]),
]
if not quick:
    sections += [("C programs, clang", [sys.executable, "-m", "tests.test_c_programs", "clang"]),
                 ("C programs, gcc", [sys.executable, "-m", "tests.test_c_programs", "gcc"]),
                 ("C programs, clang, RISC-V RV64IM", [sys.executable, "-m", "tests.test_c_programs", "clang", "riscv64"]),
                 ("C programs, clang, RISC-V RV32IM", [sys.executable, "-m", "tests.test_c_programs", "clang", "riscv32"]),
                 ("C programs, clang, x86-64", [sys.executable, "-m", "tests.test_c_programs", "clang", "x86_64"]),
                 ("C programs, gcc, x86-64", [sys.executable, "-m", "tests.test_c_programs", "gcc", "x86_64"])]
ok_all = True
for title, cmd in sections:
    t0 = time.time()
    rc, out = sh(cmd)
    ok_all &= rc == 0
    lines = [l for l in out.splitlines() if not l.startswith("     ") or "PASS" in l]
    if title.startswith("C programs"):
        p = sum(1 for l in lines if l.startswith("PASS"))
        f = sum(1 for l in lines if l.startswith("FAIL"))
        lim = sum(1 for l in lines if "model limit reported" in l)
        lines = [f"{p} passed ({lim} of them are the expected, flagged model-limit cases), {f} failed"] + \
                [l for l in lines if l.startswith("FAIL")]
    L += [f"## {title}", "", f"`{' '.join(os.path.relpath(c, ROOT) if c.startswith(ROOT) else c for c in cmd[1:])}` "
          f"exit code {rc}, {time.time() - t0:.0f} s", "", "```", *lines[-40:], "```", ""]
    print(title, "rc", rc, flush=True)
L.insert(4, f"**Overall: {'all tests passed' if ok_all else 'FAILURES, see below'}.**\n")
open(os.path.join(ROOT, "docs", "test_results.md"), "w").write("\n".join(L) + "\n")
print("wrote docs/test_results.md")
sys.exit(0 if ok_all else 1)
