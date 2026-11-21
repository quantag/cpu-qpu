# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Quantag IT Solutions GmbH
"""End-to-end: C programs compiled by clang or gcc (-O0/-O1/-O2), run by cpu_qpu.py on the quantum
interpreter (exact simulator backend) and on the CPU reference; every case must MATCH.
Run: python3 -m tests.test_c_programs [clang|gcc] [arm64|riscv64|riscv32|x86_64]"""
import json, os, shutil, subprocess, sys, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from qcpu import isa as isas
CASES = [
    ("factor.c", "smallest_factor", ["91"], ""),
    ("factor.c", "factorize", ["84", "&0"], ""),
    ("factor.c", "factorize", ["97", "&0"], ""),
    ("algorithms.c", "gcd", ["84", "36"], ""),
    ("algorithms.c", "fib", ["10"], ""),
    ("algorithms.c", "is_prime", ["89"], ""),
    ("algorithms.c", "is_prime", ["91"], ""),
    ("algorithms.c", "popcount", ["109"], ""),
    ("algorithms.c", "collatz_steps", ["6"], ""),
    ("algorithms.c", "isqrt", ["70"], ""),
    ("algorithms.c", "power_mod", ["3", "5", "13"], ""),
    ("algorithms.c", "max3", ["-7", "12", "5"], ""),
    ("algorithms.c", "sum_of_squares", ["5", "-6"], ""),
    ("algorithms.c", "bubble_sort", ["&0", "6"], "9,-3,7,1,0,4"),
    ("algorithms.c", "binary_search", ["&0", "7", "11"], "-5,0,3,8,11,20,31"),
    ("algorithms.c", "digit_sum", ["97"], ""),
    # whole programs: main() with printf; the QPU's output must equal the CPU's
    ("hello.c", "main", [], ""),
    ("factor.c", "main", [], ""),
    ("algorithms.c", "main", [], ""),
]
MAIN_LEVELS = ("O1", "O2")         # -O0 builds of the printf programs are slow to simulate
# Known model limit: compilers turn n / 10 into a multiply by 0xCCCCCCCD and a 35-bit shift (clang from -O1,
# gcc already at -O0). Those values do not fit W <= 16 bits; the reference must flag it (no match required).
# Known limits on RISC-V; each must be refused before the run or flagged, never reported as exact:
#   digit_sum          the same division needs a 32-bit constant, or the high half of a product (mulhu)
#   -O0 collatz_steps, power_mod   clang keeps a boolean temporary in a single byte (lbu / sb)
#   RV64 bubble_sort, factor.c main at -O2   an unsigned 32-bit index is zero-extended with slli 32 / srli
# Known limit on x86-64: gcc -O2 (without SSE) swaps two adjacent ints by packing both into one 64-bit word
# on the stack; such a word is not a sign-extended W-bit value. Flagged at the store.
DIGIT = {("digit_sum", "O0"), ("digit_sum", "O1"), ("digit_sum", "O2")}
BYTES = {("collatz_steps", "O0"), ("power_mod", "O0")}
EXPECT_MODEL_LIMIT = {("clang", "arm64"): {("digit_sum", "O1"), ("digit_sum", "O2")},
                      ("gcc", "arm64"): DIGIT,
                      ("clang", "riscv64"): DIGIT | BYTES | {("bubble_sort", "O1"), ("bubble_sort", "O2"), ("factor.c:main", "O2")},
                      ("clang", "riscv32"): DIGIT | BYTES,
                      ("clang", "x86_64"): {("digit_sum", "O1"), ("digit_sum", "O2")},
                      ("gcc", "x86_64"): DIGIT | {("bubble_sort", "O2")}}
FLAGS = ["-fno-stack-protector", "-I" + os.path.join(ROOT, "include")]

if __name__ == "__main__":
    cc_name = sys.argv[1] if len(sys.argv) > 1 else "clang"
    arch = sys.argv[2] if len(sys.argv) > 2 else "arm64"
    isa = isas.get(arch)
    cc = ["clang"] + isa.clang if cc_name == "clang" else [next((g for g in isa.gcc if shutil.which(g)), isa.gcc[0])] + isa.gcc_flags
    limits = EXPECT_MODEL_LIMIT.get((cc_name, arch), set())
    fails = 0
    with tempfile.TemporaryDirectory() as d:
        for src in sorted({c[0] for c in CASES}):
            for o in ("O0", "O1", "O2"):
                subprocess.run(cc + FLAGS + [f"-{o}", "-c", os.path.join(ROOT, "examples", src),
                                "-o", os.path.join(d, f"{src}.{o}.o")], check=True)
        for src, fn, args, mem in CASES:
            for o in (MAIN_LEVELS if fn == "main" else ("O0", "O1", "O2")):
                obj = os.path.join(d, f"{src}.{o}.o")
                cmd = [sys.executable, os.path.join(ROOT, "cpu_qpu.py"), obj, "--entry", fn, "--args", *args,
                       "--report", os.path.join(d, "r")] + ([f"--mem={mem}"] if mem else [])
                p = subprocess.run(cmd, capture_output=True, text=True)
                rep = json.load(open(os.path.join(d, "r.json"))) if os.path.exists(os.path.join(d, "r.json")) else {}
                ok = rep.get("verdict", "").startswith("MATCH") and not rep.get("cpu", {}).get("issues")
                outp = rep.get("result", {}).get("output", {})
                if fn == "main":
                    ok = ok and outp.get("match") and bool(outp.get("qpu"))
                limit = (fn, o) in limits or (f"{src}:{fn}", o) in limits
                if limit:                                           # must be detected and reported, or refused
                    ok = bool(rep.get("cpu", {}).get("issues")) or rep.get("verdict", "").startswith("NOT RUN")
                fails += not ok
                res = rep.get("result", {})
                mc = rep.get("machine", {})
                if fn == "main" and ok:
                    print("     " + (outp.get("qpu") or "").replace("\n", "\n     ").rstrip())
                print(f"{'PASS' if ok else 'FAIL'}{' (model limit reported)' if limit else ''} {src}:{fn}({', '.join(args)}) -{o}: QPU={res.get('return_value', {}).get('qpu')} "
                      f"CPU={res.get('return_value', {}).get('cpu')} steps={mc.get('steps')} qubits={mc.get('qubits')}"
                      + ("" if ok else f"\n     {rep.get('verdict') or p.stderr[-500:]}"))
                os.path.exists(os.path.join(d, "r.json")) and os.remove(os.path.join(d, "r.json"))
    print("all passed" if not fails else f"{fails} failed")
    sys.exit(1 if fails else 0)
