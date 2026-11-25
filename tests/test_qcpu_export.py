# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Quantag IT Solutions GmbH
"""qcpucc --qcpu-export: qoracle circuits for other quantum toolkits.

  1. The exported OpenQASM 3 compute oracles give the C function's result for every input (read back and
     run gate by gate), and the JSON describes the same registers.
  2. Qiskit loads the files (if qiskit-qasm3-import is installed).
  3. CUDA-Q (if installed): the exported Python kernels run in CUDA-Q: the compute oracle is right for all
     inputs, and Grover's search with the phase oracle finds the marked value.
  4. The C++ header type-checks against CUDA-Q's own cudaq.h (if CUDA-Q's headers and clang++ are there).
  5. examples/codebreaker: the solver's predicate as a 1,044-qubit phase oracle; Grover's search finds the
     secret code BYRG in 12 oracle calls.
  6. --qcpu-export needs --qcpu-mode qoracle.
Run: python3 -m tests.test_qcpu_export"""
import glob, importlib.util, json, os, shutil, subprocess, sys, tempfile, textwrap
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "examples", "codebreaker"))
import grover_solver

QCPUCC = [sys.executable, os.path.join(ROOT, "qcpucc.py")]


def sh(cmd, check=True, **kw):
    p = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if check and p.returncode:
        raise AssertionError(" ".join(cmd) + "\n" + p.stdout + p.stderr)
    return p


SRC = textwrap.dedent("""\
    #include <qcpu.h>
    __qcpu__ int QCPU_RESULT_BITS(1) is_five(unsigned QCPU_BITS(4) x) { return x == 5; }
    __qcpu__ int sq(unsigned QCPU_BITS(3) x) { return x * x; }
    __qcpu__ int mix(int QCPU_BITS(3) a, int QCPU_SBITS(3) b) { return a * 3 - b; }
    int main(void) { return is_five(5) - 1 + sq(2) - 4 + mix(1, -1) - 4; }
    """)
REFERENCE = {"is_five": lambda x: int(x == 5), "sq": lambda x: x * x, "mix": lambda a, b: a * 3 - b}


def main():
    try:
        import qoracle  # noqa: F401
    except ImportError:
        print("skipped: qoracle is not installed")
        return
    with tempfile.TemporaryDirectory() as d:
        src, out = os.path.join(d, "p.c"), os.path.join(d, "oracle")
        open(src, "w").write(SRC)
        p = sh(QCPUCC + ["--qcpu-mode", "qoracle", "--qcpu-export", out, src, "-o", os.path.join(d, "p")])
        assert "exported 13 file(s)" in p.stderr, p.stderr
        assert sh([os.path.join(d, "p")]).returncode == 0

        # 1. every input through the exported compute oracle
        for fn, ref in REFERENCE.items():
            info = json.load(open(os.path.join(out, f"{fn}.json")))[0]
            regs, nq, gates = grover_solver.read_qasm(os.path.join(out, f"{fn}.qasm"))
            assert nq == info["qubits"] and info["form"] == "compute", (fn, nq, info)
            ins = [r for r in info["registers"] if r["role"] == "input"]
            res = next(r for r in info["registers"] if r["role"] == "output")
            n = sum(r["bits"] for r in ins)
            for v in range(1 << n):
                state, args, shift = 0, [], 0
                for r in ins:
                    raw = (v >> shift) & ((1 << r["bits"]) - 1)
                    shift += r["bits"]
                    state |= raw << r["first"]
                    args.append(raw - (1 << r["bits"]) if r["signed"] and raw >> (r["bits"] - 1) else raw)
                _, after = grover_solver.oracle(gates, state)
                got = (after >> res["first"]) & ((1 << res["bits"]) - 1)
                want = ref(*args) & ((1 << res["bits"]) - 1)
                assert got == want, (fn, args, got, want)
                assert after & ~(((1 << res["bits"]) - 1) << res["first"]) == state, (fn, args, "work qubits not 0")
        phase = json.load(open(os.path.join(out, "is_five.json")))
        assert [x["form"] for x in phase] == ["compute", "phase"] and not os.path.exists(os.path.join(out, "sq_phase.qasm"))
        print("1) exported OpenQASM 3 compute oracles (is_five, sq, mix with a signed argument) give the C function's "
              "result for every input; work qubits return to 0; a one-bit function also has its phase oracle")

        # 2. Qiskit
        try:
            import qiskit.qasm3 as q3
            import qiskit_qasm3_import  # noqa: F401
        except ImportError:
            print("2) Qiskit: skipped (qiskit and qiskit-qasm3-import not installed)")
        else:
            for f in sorted(glob.glob(os.path.join(out, "*.qasm"))):
                stem = os.path.basename(f)[:-5]
                info = json.load(open(os.path.join(out, stem.replace("_phase", "") + ".json")))
                c = q3.load(f)
                want = next(x for x in info if x["form"] == ("phase" if stem.endswith("_phase") else "compute"))
                assert c.num_qubits == want["qubits"] and c.size() == want["gates"], (f, c.num_qubits, c.size())
            print("2) Qiskit loads every exported file (qiskit.qasm3.load): qubits and gates as in the JSON")

        # 3. CUDA-Q
        try:
            import warnings
            warnings.filterwarnings("ignore")
            import cudaq
        except ImportError:
            print("3) CUDA-Q: skipped (pip install cudaq)")
        else:
            cudaq_tests(out, cudaq)

        # 4. the C++ header against CUDA-Q's cudaq.h
        spec = importlib.util.find_spec("cudaq_core")
        inc = next((p for loc in (spec.submodule_search_locations if spec else [])
                    for p in glob.glob(os.path.join(loc, "**", "include", "cudaq.h"), recursive=True)), None)
        if inc and shutil.which("clang++"):
            use = os.path.join(d, "use.cpp")
            open(use, "w").write(textwrap.dedent("""\
                #include "is_five.cudaq.h"
                #include "mix.cudaq.h"
                struct grover {
                  void operator()(int iterations) __qpu__ {
                    cudaq::qvector q(is_five_phase_qubits);
                    for (int b = 0; b < 4; b++) h(q[b]);
                    for (int i = 0; i < iterations; i++) {
                      is_five_phase(q);
                      for (int b = 0; b < 4; b++) { h(q[b]); x(q[b]); }
                      z<cudaq::ctrl>(q[0], q[1], q[2], q[3]);
                      for (int b = 0; b < 4; b++) { x(q[b]); h(q[b]); }
                    }
                    for (int b = 0; b < 4; b++) mz(q[b]);
                  }
                };
                """))
            sh(["clang++", "-std=c++20", "-fsyntax-only", "-I", os.path.dirname(inc), "-I", out, use])
            print("4) the C++ headers type-check against CUDA-Q's own cudaq.h (clang++; nvq++ is needed to run them)")
        else:
            print("4) C++ header: skipped (CUDA-Q's headers or clang++ not found)")

        # 5. the Code Breaker solver
        oracle = os.path.join(d, "solver_oracle")
        p = sh(QCPUCC + ["--qcpu-mode", "qoracle", "--qcpu-export", oracle,
                         os.path.join(ROOT, "examples", "codebreaker", "solver.c"), "-o", os.path.join(d, "solver")])
        assert "consistent: qoracle circuit, 1045 qubits (8 in, 1 out), 4255 gates" in p.stderr, p.stderr
        assert sh([os.path.join(d, "solver")]).stdout.strip().endswith(": BYRG")
        p = sh([sys.executable, os.path.join(ROOT, "examples", "codebreaker", "grover_solver.py"),
                os.path.join(oracle, "consistent_phase.qasm")])
        assert "after 12 Grover iterations" in p.stdout and "  BYRG  probability 0.9999" in p.stdout, p.stdout
        print("5) Code Breaker solver: consistent() as a 1,044-qubit phase oracle; Grover's search finds BYRG with "
              "probability 0.9999 in 12 oracle calls (classically: up to 256 codes)")

        # 6. export needs qoracle mode
        p = sh(QCPUCC + ["--qcpu-export", out, src, "-o", os.path.join(d, "q")], check=False)
        assert p.returncode and "--qcpu-export writes qoracle circuits" in p.stderr, p.stderr
        print("6) --qcpu-export with --qcpu-mode qcpu: a clear error")
    print("PASS")


def cudaq_tests(out, cudaq):
    spec = importlib.util.spec_from_file_location("is_five_cudaq", os.path.join(out, "is_five_cudaq.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    is_five_oracle, is_five_phase = m.is_five_oracle, m.is_five_phase

    @cudaq.kernel
    def evaluate(v: int):
        q = cudaq.qvector(19)
        for b in range(4):
            if (v >> b) & 1:
                x(q[b])
        is_five_oracle(q)
        mz(q[4])

    @cudaq.kernel
    def grover(iterations: int):
        q = cudaq.qvector(18)
        for b in range(4):
            h(q[b])
        for _ in range(iterations):
            is_five_phase(q)
            for b in range(4):
                h(q[b])
                x(q[b])
            z.ctrl(q[0], q[1], q[2], q[3])
            for b in range(4):
                x(q[b])
                h(q[b])
        for b in range(4):
            mz(q[b])

    assert m.IS_FIVE_ORACLE_QUBITS == 19 and m.IS_FIVE_PHASE_QUBITS == 18
    for v in range(16):
        counts = cudaq.sample(evaluate, v, shots_count=10)
        assert {k: counts.count(k) for k in counts} == {("1" if v == 5 else "0"): 10}, v
    counts = cudaq.sample(grover, 3, shots_count=1000)
    hits = counts.count("1010")                     # q[0] first: x = 5
    assert hits > 900, {k: counts.count(k) for k in counts}
    print(f"3) CUDA-Q {cudaq.__version__.split()[2] if len(cudaq.__version__.split()) > 2 else ''}: the exported "
          f"kernels run: is_five_oracle right for all 16 inputs; Grover with is_five_phase finds x = 5 in "
          f"{hits} of 1000 shots")


if __name__ == "__main__":
    main()
