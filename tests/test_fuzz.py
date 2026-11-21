# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Quantag IT Solutions GmbH
"""Differential fuzzing: random A64 programs (assembled by clang), run on ONE fixed quantum interpreter
circuit per register width and on the CPU reference, compared after every cycle (registers, flags,
memory, PC). See tests/common.py for the outcome classes.

Run: python3 -m tests.test_fuzz [N per width] [seed] [widths, e.g. 8,12,16]   -> prints a JSON summary"""
import json, os, random, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from qcpu.machine import Config, QuantumARM
from tests.common import assemble, run_case

MEM, NREG, SLOTS, STEPS = 8, 8, 32, 32
COND = "eq ne cs cc mi pl vs vc hi ls ge lt gt le al".split()
LIMM = [1, 3, 7, 0xf, 0x3c, 0x55, 0xaa, 0xf0, 0xfe, 0x7f, 0x80, 0xfffffff0, 0xffffff00, 0x6, 0x18]
EXT = ["uxtb", "uxth", "uxtw", "sxtb", "sxth", "sxtw"]


def gen(r, n):
    R = lambda: f"w{r.randrange(7)}"           # x7 holds the data pointer
    X = lambda: f"x{r.randrange(7)}"
    sh = lambda: r.choice(["", f", lsl #{r.randrange(4)}", f", lsr #{r.randrange(3)}", f", asr #{r.randrange(3)}"])
    out = []
    for i in range(n):
        k = r.randrange(24)
        if k == 0: out.append(f"{r.choice(['movz', 'movn'])} {R()}, #{r.randrange(40)}")
        elif k == 1: out.append(f"movk {R()}, #{r.randrange(40)}")
        elif k == 2: out.append(f"{r.choice(['add', 'sub', 'adds', 'subs'])} {R()}, {R()}, #{r.randrange(30)}")
        elif k == 3: out.append(f"{r.choice(['add', 'sub', 'adds', 'subs'])} {R()}, {R()}, {R()}{sh()}")
        elif k == 4: out.append(f"{r.choice(['and', 'orr', 'eor', 'bic', 'orn', 'eon', 'ands', 'bics'])} {R()}, {R()}, {R()}{sh()}")
        elif k == 5: out.append(f"{r.choice(['and', 'orr', 'eor', 'ands'])} {R()}, {R()}, #{r.choice(LIMM)}")
        elif k == 6: out.append(f"{r.choice(['lsl', 'lsr', 'asr'])} {R()}, {R()}, #{r.randrange(8)}")
        elif k == 7: out.append(r.choice([f"sxtb {R()}, {R()}", f"uxtb {R()}, {R()}", f"sxth {R()}, {R()}",
                                          f"ubfx {R()}, {R()}, #{r.randrange(4)}, #{r.randrange(1, 4)}",
                                          f"sbfx {R()}, {R()}, #{r.randrange(4)}, #{r.randrange(1, 4)}",
                                          f"ubfiz {R()}, {R()}, #{r.randrange(4)}, #{r.randrange(1, 4)}"]))
        elif k == 8: out.append(f"{r.choice(['lslv', 'lsrv', 'asrv'])} {R()}, {R()}, {R()}")
        elif k == 9: out.append(f"{r.choice(['csel', 'csinc', 'csinv', 'csneg'])} {R()}, {R()}, {R()}, {r.choice(COND)}")
        elif k == 10: out.append(r.choice([f"mul {R()}, {R()}, {R()}", f"madd {R()}, {R()}, {R()}, {R()}", f"msub {R()}, {R()}, {R()}, {R()}"]))
        elif k == 11: out.append(f"{r.choice(['cmp', 'cmn'])} {R()}, {r.choice(['#' + str(r.randrange(20)), R()])}")
        elif k == 12: out.append(f"{r.choice(['str', 'ldr'])} {R()}, [x7, #{4 * r.randrange(MEM)}]")
        elif k == 13: out.append(f"cset {R()}, {r.choice(COND)}")
        elif k == 14: out.append(f"{r.choice(['b.' + r.choice(COND), 'cbz ' + R() + ',', 'cbnz ' + R() + ','])} L{min(n, i + 1 + r.randrange(3))}")
        elif k == 15: out.append(f"tst {R()}, #{r.choice(LIMM)}")
        elif k == 16: out.append(f"{r.choice(['udiv', 'sdiv'])} {R()}, {R()}, {R()}")
        elif k == 17: out.append(f"{r.choice(['ccmp', 'ccmn'])} {R()}, {r.choice(['#' + str(r.randrange(32)), R()])}, #{r.randrange(16)}, {r.choice(COND)}")
        elif k == 18: out.append(f"{r.choice(['tbz', 'tbnz'])} {R()}, #{r.randrange(9)}, L{min(n, i + 1 + r.randrange(3))}")
        elif k == 19: out.append(r.choice([f"stur {R()}, [x7, #{4 * r.randrange(MEM)}]", f"ldur {R()}, [x7, #{4 * r.randrange(MEM)}]",
                                           f"stp {R()}, {R()}, [x7, #{4 * r.randrange(MEM - 1)}]", f"ldp {R()}, {R()}, [x7, #{4 * r.randrange(MEM - 1)}]",
                                           (lambda q: f"and w{q}, w{q}, #7\n    ldr {R()}, [x7, x{q}, lsl #2]")(r.randrange(7)),
                                           f"ldrsw {X()}, [x7, #{4 * r.randrange(MEM)}]", f"str {X()}, [x7, #{8 * r.randrange(MEM // 2)}]"]))
        elif k == 20: out.append(f"{r.choice(['add', 'sub', 'adds', 'subs'])} {X()}, {X()}, {R()}, {r.choice(EXT)} #{r.randrange(5)}")
        elif k == 21: out.append(f"{r.choice(['smull', 'umull'])} {X()}, {R()}, {R()}")
        elif k == 22: out.append(f"{r.choice(['add', 'sub', 'eor', 'lsl', 'asr'])} {X()}, {X()}, " +
                                 r.choice([X(), "#" + str(r.randrange(1, 6))]))
        else: out.append("bl FN")
    lines = []
    for i, ins in enumerate(out):
        lines += [f"L{i}:", "    " + ins]
    return [".globl f", "f:", "    mov x29, x30"] + lines + [f"L{n}:", "    mov x30, x29", "    ret",
                                                            "FN:", "    add w5, w5, #3", "    ret"]


def fuzz(W, N, seed, verbose=True):
    m = QuantumARM(Config(W=W, regs=tuple(range(NREG)) + (29, 30), sp=False, SLOTS=SLOTS, STEPS=STEPS, MEM=MEM), defer=True)
    r = random.Random(seed)
    stats = {"generated": 0, "rejected by assembler": 0, "too long": 0, "discarded (CPU did not halt)": 0,
             "match": 0, "match-flagged": 0, "flagged": 0, "UNFLAGGED": 0}
    lim = 1 << (W - 1)
    for t in range(N):
        stats["generated"] += 1
        lines = gen(r, r.randrange(4, 12))
        words = assemble(lines)
        if words is None:
            stats["rejected by assembler"] += 1; continue
        # inputs: small values most of the time, boundary values of the W-bit model some of the time
        pool = [0, 1, -1, 2, 3, lim - 1, -lim, lim // 2, -(lim // 2), 7, -7, 100 % lim]
        args = [r.choice(pool) if r.random() < 0.3 else r.randrange(-20, 40) for _ in range(7)] + [0]
        mem = [r.choice(pool) if r.random() < 0.3 else r.randrange(-50, 50) for _ in range(MEM)]
        cls, why = run_case(m, words, args, mem, data_ptr_reg=7)
        if cls == "discarded":
            stats["too long" if why == "program too long" else "discarded (CPU did not halt)"] += 1
            continue
        stats[cls] += 1
        if cls == "UNFLAGGED" and verbose:
            print("UNFLAGGED MISMATCH (bug)\n  " + "\n  ".join(lines) + f"\n  args {args} mem {mem}\n  {why}")
    return stats


if __name__ == "__main__":
    N = int(sys.argv[1]) if len(sys.argv) > 1 else 100
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else 7
    widths = [int(x) for x in (sys.argv[3] if len(sys.argv) > 3 else "8,12,16").split(",")]
    res = {}
    for W in widths:
        res[W] = fuzz(W, N, seed + W)
        print(f"W={W} seed={seed + W}: {json.dumps(res[W])}")
    sys.exit(1 if any(s["UNFLAGGED"] for s in res.values()) else 0)
