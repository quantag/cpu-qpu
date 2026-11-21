# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Quantag IT Solutions GmbH
"""Boundary tests: operand extensions, flags and conditions, shifts, 32/64-bit (W/X) forms, memory,
multiply/divide and bitfields, at the edges of the W-bit model, for W = 8, 12 and 16.

Each case is run on the CPU reference and on the quantum machine and compared after every cycle.
Requirement: no UNFLAGGED mismatch (QPU != CPU while the model check reports nothing).
Run: python3 -m tests.test_edges [widths, e.g. 8,12,16]"""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from qcpu.machine import Config, QuantumARM
from tests.common import assemble, run_case

CONDS = "eq ne hs lo mi pl vs vc hi ls ge lt gt le".split()


def values(W):
    L = 1 << (W - 1)
    return sorted({0, 1, -1, 2, 7, 127, 128, 255, 256, -128, -129, L - 1, -L, L, -L - 1, 0x1F0, L // 2})


def cases(W):
    V = values(W)
    out = []
    # 1. operand extensions in ADD/SUB (extended register), X and W destinations
    for ext in ("uxtb", "uxth", "uxtw", "sxtb", "sxth", "sxtw"):
        for s in (0, 2, 4):
            for v in V:
                out.append(("extend", [f"add x0, x2, w1, {ext} #{s}", f"subs x3, x2, w1, {ext} #{s}", "cset w4, lt"], [0, v, 5]))
                out.append(("extend", [f"add w0, w2, w1, {ext} #{s}"], [0, v, -3]))
    # 2. flags and all conditions after ADDS/SUBS/CMN on boundary pairs, W and X forms
    for a in V:
        for b in (0, 1, -1, V[-1], V[0]):
            for form in ("w", "x"):
                body = [f"cmp {form}0, {form}1"] + [f"cset w{2 + i}, {c}" for i, c in enumerate(CONDS[:7])]
                out.append(("flags", body, [a, b]))
                body = [f"adds {form}2, {form}0, {form}1"] + [f"cset w{3 + i}, {c}" for i, c in enumerate(CONDS[7:])]
                out.append(("flags", body, [a, b]))
    # 3. shifts by immediate and by register, all amounts that matter
    for v in V:
        for k in sorted({0, 1, W - 1, W, 15, 31}):
            out.append(("shift", [f"lsl w1, w0, #{k}", f"lsr w2, w0, #{k}", f"asr w3, w0, #{k}"], [v]))
        for k in sorted({0, 1, W - 1, W, 31, 40, 63}):
            out.append(("shift", [f"lsl x1, x0, #{k}", f"lsr x2, x0, #{k}", f"asr x3, x0, #{k}"], [v]))
        for k in (0, 1, W - 1, W, 31, 32, 63, 64, -1):
            out.append(("shift", ["lslv x2, x0, x1", "lsrv x3, x0, x1", "asrv x4, x0, x1",
                                  "lslv w5, w0, w1", "lsrv w6, w0, w1", "asrv w7, w0, w1"], [v, k]))
            out.append(("shift", ["add x2, x0, x1, lsr #3", "sub x3, x0, x1, asr #2", "eor w4, w0, w1, lsl #5"], [v, k]))
    # 4. 32-bit results used as 64-bit values (zero extension of W writes)
    for v in V:
        out.append(("w/x", ["add w1, w0, #1", "add x2, x1, #0", "sxtw x3, w1", "uxtw x4, w1", "mov w5, w0", "cmp x5, x0", "cset w6, eq"], [v]))
    # 5. memory: W/X stores and loads, sign-extending loads, pairs with writeback, mixed widths
    for v in V:
        out.append(("memory", ["str w0, [x7]", "ldr w1, [x7]", "ldrsw x2, [x7]", "str x0, [x7, #8]", "ldr x3, [x7, #8]",
                               "ldr w4, [x7, #12]", "stp w0, w1, [x7, #16]!", "ldp w5, w6, [x7], #4", "ldr x2, [x7, #-20]"], [v]))
        out.append(("memory", ["str w0, [x7]", "str w0, [x7, #4]", "ldr x1, [x7]", "stp x0, x1, [x7, #8]", "ldp x2, x3, [x7, #8]"], [v]))
    # 6. multiply, divide, bitfields
    for a in V:
        for b in (0, 1, -1, 3, V[-1]):
            out.append(("mul/div", ["mul w2, w0, w1", "madd x3, x0, x1, x0", "smull x4, w0, w1", "umull x5, w0, w1"], [a, b]))
            out.append(("mul/div", ["sdiv w2, w0, w1", "udiv w3, w0, w1", "sdiv x4, x0, x1", "udiv x5, x0, x1"], [a, b]))
        out.append(("bitfield", ["ubfx w1, w0, #3, #4", "sbfx w2, w0, #2, #5", "ubfiz w3, w0, #4, #3", "sbfiz x4, x0, #1, #6",
                                 "sxtb w5, w0", "uxth w6, w0"], [a]))
    # 7. pointers (held as their offset from DATA_BASE): NULL tests, compares, high-bit operations.
    #    X7 = DATA_BASE is encoded as 0, so a NULL test on it must be flagged, not silently wrong.
    for k in (0, 4, -4, 8):
        pre = [f"add x7, x7, #{k}" if k >= 0 else f"sub x7, x7, #{-k}"]
        for body in (["cbz x7, 1f", "mov w1, #1", "1:"], ["cbnz x7, 1f", "mov w1, #1", "1:"],
                     ["tbnz x7, #28, 1f", "mov w1, #1", "1:"], ["tbz x7, #2, 1f", "mov w1, #1", "1:"],
                     ["cmp x7, #0", "cset w1, eq", "cset w2, lt", "cset w3, hi"],
                     ["add x6, x7, #8", "cmp x6, x7", "cset w1, hi", "cset w2, eq", "cset w3, lt"],
                     ["sub x6, x7, #4", "cmp x7, x6", "cset w1, hs", "cset w2, gt"],
                     ["tst x7, #7", "cset w1, eq"], ["lsr x1, x7, #2"], ["mov x2, #3", "udiv x1, x7, x2"],
                     ["add x1, x7, #12", "sub x2, x1, x7", "str x1, [x7, #16]", "ldr x3, [x7, #16]"]):
            out.append(("pointer", pre + body, [0] * 7))
    # 8. constants that coincide with DATA_BASE or CODE_BASE but are used as integers or bit masks
    #    (regression: tst x1, #0x10000000 with X1 = -1 gave X0 = 1 unflagged at W = 12)
    L = 1 << (W - 1)
    for x in (-1, 0, 1, L - 1, -L):
        for body in (["tst x1, #0x10000000", "cset x0, eq"], ["tst x1, #0x400000", "cset x0, ne"],
                     ["ands x0, x1, #0x10000000", "cset x2, eq"],
                     ["mov x2, #0x10000000", "cmp x1, x2", "cset x0, lt", "cset x3, eq"],
                     ["mov x2, #0x400000", "adds x0, x1, x2", "cset x3, mi"],
                     ["and x0, x1, #0x10000000", "cbz x0, 1f", "mov w3, #1", "1:"],
                     ["eor x0, x1, #0x10000000", "tbnz x0, #28, 1f", "mov w3, #1", "1:"]):
            out.append(("constants", body, [0, x, 0, 0, 0, 0, 0]))
    return out


def run(W):
    m = QuantumARM(Config(W=W, regs=tuple(range(8)) + (30,), sp=False, SLOTS=32, STEPS=24, MEM=16), defer=True)
    stats, bad = {}, []
    for group, body, args in cases(W):
        words = assemble(body + ["ret"])
        args = (list(args) + [0] * 8)[:7] + [0]
        cls, why = run_case(m, words, args, [0] * 16, data_ptr_reg=7)
        g = stats.setdefault(group, {})
        g[cls] = g.get(cls, 0) + 1
        if cls == "UNFLAGGED":
            bad.append((group, body, args, why))
    return stats, bad


if __name__ == "__main__":
    widths = [int(x) for x in (sys.argv[1] if len(sys.argv) > 1 else "8,12,16").split(",")]
    total_bad = 0
    for W in widths:
        stats, bad = run(W)
        print(f"W={W}: " + json.dumps(stats))
        for b in bad[:10]:
            print("  UNFLAGGED:", b)
        total_bad += len(bad)
    print("no unflagged mismatch" if not total_bad else f"{total_bad} UNFLAGGED mismatches")
    sys.exit(1 if total_bad else 0)
