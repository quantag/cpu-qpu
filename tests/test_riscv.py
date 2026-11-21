# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Quantag IT Solutions GmbH
"""RISC-V front end (RV64IM and RV32IM): circuit against the CPU (Unicorn), after every cycle.

  1. Decoder table: the class patterns are mutually exclusive, and agree with what the assembler emits.
  2. Trap: words that are not implemented (mulh, byte loads, ecall, CSR, floating point, compressed,
     reserved encodings) set the trap bit and change nothing. This is checked on the circuit itself.
  3. Superposition of a valid and an invalid instruction word: the valid branch computes its result,
     the invalid branch traps.
  4. Boundary cases: every implemented instruction on operands at the edges of the W-bit model.
     - never an unflagged mismatch;
     - with representable inputs the circuit must equal the CPU, except for unsigned division of a
       negative number (whose result depends on the full register width);
     - when the circuit equals the CPU, the reference model must not claim otherwise.
  5. Differential fuzzing with random programs.
  6. The output stub in qcpu/loader.py equals the assembled docs/output_stub_riscv.s.
Run: python3 -m tests.test_riscv [N programs per width] [seed] [widths, e.g. 8,12,16]"""
import os, random, subprocess, sys, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import numpy as np
from qcpu import isa as isas, rv
from qcpu.loader import OUTPUT_STUB_RV
from qcpu.sparsesim import SparseState
from tests.common import assemble, run_case

AREG = list(range(10, 18))                 # a0..a7; a7 is the data pointer in the tests
R_OPS = ["add", "sub", "and", "or", "xor", "slt", "sltu", "sll", "srl", "sra", "mul", "div", "rem"]
R_OPS_U = ["divu", "remu"]                 # exact only for non-negative operands
W_OPS = ["addw", "subw", "sllw", "srlw", "sraw", "mulw", "divw", "remw"]
W_OPS_U = ["divuw", "remuw"]
I_OPS = ["addi", "slti", "sltiu", "xori", "ori", "andi"]
BR = ["beq", "bne", "blt", "bge", "bltu", "bgeu"]


_asm = {}


def asm(lines, isa):
    """assemble() with a cache: the same source is used at several register widths."""
    key = (isa.name, tuple(lines))
    if key not in _asm:
        _asm[key] = assemble(lines, isa)
    return _asm[key]


def machine(isa, W, steps=32, slots=32, mem=8):
    return isa.Machine(isa.config(W=W, used=set(AREG) | {1}, SLOTS=slots, STEPS=steps, MEM=mem), defer=True)


# ------------------------------------------------------------------ 1. decoder table
def decoder_table():
    r = random.Random(1)
    for xlen in (32, 64):
        pats = rv.classes(xlen)
        words = [r.getrandbits(32) for _ in range(20000)] + [r.getrandbits(32) | 3 for _ in range(20000)]
        for w in words:
            hits = [n for n, ps in pats.items() for m, v in ps if w & m == v]
            assert len(hits) <= 1, (hex(w), hits)
        isa = isas.get(f"riscv{xlen}")
        src = [f"{op} a0, a1, a2" for op in R_OPS + R_OPS_U + (W_OPS + W_OPS_U if xlen == 64 else [])] + \
              [f"{op} a0, a1, -7" for op in I_OPS] + ["slli a0, a1, 3", "srli a0, a1, 3", "srai a0, a1, 3",
               "lui a0, 5", "auipc a0, 5", "lw a0, 4(a1)", "sw a0, 4(a1)", "jalr a0, 4(a1)", "ebreak"] + \
              (["addiw a0, a1, 1", "slliw a0, a1, 3", "srliw a0, a1, 3", "sraiw a0, a1, 3", "ld a0, 8(a1)",
                "sd a0, 8(a1)", "lwu a0, 4(a1)"] if xlen == 64 else [])
        code = assemble(src, isa)
        assert code and len(code) == len(src)
        for line, w in zip(src, code):
            assert rv.classify(w, xlen) is not None, line
    return True


# ------------------------------------------------------------------ 2./3. traps
BAD = {"mulh": 0x02B51533, "mulhu": 0x02B53533, "lb": 0x00058503, "lbu": 0x0005C503, "lh": 0x00059503, "sb": 0x00A58023,
       "ecall": 0x00000073, "csrrw": 0x30051573, "fence": 0x0000000F, "fadd.s": 0x00B57553, "zero word": 0x00000000,
       "compressed c.nop": 0x00000001, "branch to odd target": 0x00B50163, "funct7 = 2": 0x04B50533,
       "amoadd.w": 0x00B5252F, "all ones": 0xFFFFFFFF}


def traps():
    n = 0
    for xlen in (32, 64):
        isa = isas.get(f"riscv{xlen}")
        bad = dict(BAD)
        if xlen == 32:
            bad.update({"addiw on RV32": 0x0015851B, "ld on RV32": 0x0085B503, "slli by 32 on RV32": 0x02059513})
        else:
            bad.update({"slliw by 32": 0x0205951B, "sltw (does not exist)": 0x00B5253B})
        m = machine(isa, 8, steps=3, slots=4, mem=4)
        for name, w in bad.items():
            assert not isa.check(w, 0, 4).ok, name
            init = {k: 5 + k for k in AREG} | {1: 12}
            ones = m.load_bits([w, 0, 0, rv.EBREAK], init, [1, 2, 3, 4])
            out = m.run_fast(ones)
            st = out["state"]
            val = lambda q: sum(st[x] << i for i, x in enumerate(q))
            assert out["trap"] and out["run"] == 0, name                    # trapped and stopped
            assert out["trace"] == [0, 0, 0, 0], name                       # the PC did not move
            assert all(val(m.X[k]) == init[k] for k in m.X), name           # no register written
            assert [val(c) for c in m.ram] == [1, 2, 3, 4], name            # no memory written
            n += 1
    return n


def superposed_valid_and_invalid():
    """Program slot 0 holds 'add a0, a0, a1' with bit 26 (in funct7) in superposition: |0> is the
    add, |1> is an unallocated encoding. One run of the circuit does both."""
    isa = isas.get("riscv64")
    m = isa.Machine(isa.config(W=8, used={10, 11, 1}, SLOTS=4, STEPS=3, MEM=0))
    add = 0x00B50533
    assert isa.check(add, 0, 4).ok and not isa.check(add | 1 << 26, 0, 4).ok
    ones = m.load_bits([add, 0x00008067, 0, rv.EBREAK], {10: 20, 11: 22, 1: 12})
    st = SparseState(m.n, ones)
    st.h(m.mem[0][26])
    st.apply(m.g)
    got = {}
    for row, a in zip(st.bits, st.amp):
        v = lambda q: int(sum(int(row[x]) << i for i, x in enumerate(q)))
        got[int(row[m.mem[0][26]])] = (v(m.X[10]), any(row[t] for t in m.trap), round(abs(a) ** 2, 6))
    assert got == {0: (42, False, 0.5), 1: (20, True, 0.5)}, got
    return got


# ------------------------------------------------------------------ 4. boundary cases
def values(W):
    L = 1 << (W - 1)
    return sorted({0, 1, -1, 2, 7, 127, 128, 255, 256, -128, -129, L - 1, -L, L, -L - 1, 0x1F0, L // 2})


def edge_cases(W, xlen):
    """(group, lines, a0, a1, exact) -- exact: the circuit must equal the CPU when a0, a1 are representable."""
    vals, out = values(W), []
    rops = R_OPS + (W_OPS if xlen == 64 else [])
    uops = R_OPS_U + (W_OPS_U if xlen == 64 else [])
    dst = ["a2", "a3", "a4", "a5", "a6"]
    for x in vals:
        for y in vals:
            for i in range(0, len(rops), 5):
                out.append(("register ops", [f"{op} {d}, a0, a1" for op, d in zip(rops[i:i + 5], dst)], x, y, True))
            out.append(("unsigned division", [f"{op} {d}, a0, a1" for op, d in zip(uops, dst)], x, y, x >= 0 and y >= 0))
            br = []
            for k, b in enumerate(BR):
                br += [f"{b} a0, a1, {k + 1}f", f"ori a2, a2, {1 << k}", f"{k + 1}:"]
            out.append(("branches", br, x, y, True))
        L = 1 << (W - 1)
        for imm in (0, 1, -1, 7, 127, 128, -128, 255, 2047, -2048):
            # an immediate is a value too: a comparison is exact only if it is representable in W bits
            out.append(("immediate ops", [f"{op} {d}, a0, {imm}" for op, d in zip(I_OPS, dst + ["a1"])], x, 0, -L <= imm < L))
            if xlen == 64:
                out.append(("immediate ops", [f"addiw a2, a0, {imm}"], x, 0, True))
        for k in sorted({0, 1, 2, 5, W - 2, W - 1, W, 31} | ({32, 33, 63} if xlen == 64 else set())):
            out.append(("shift by immediate", [f"slli a2, a0, {k}", f"srli a3, a0, {k}", f"srai a4, a0, {k}"], x, 0, True))
            if xlen == 64 and k < 32:
                out.append(("shift by immediate", [f"slliw a2, a0, {k}", f"srliw a3, a0, {k}", f"sraiw a4, a0, {k}"], x, 0, True))
        for k in (0, 1, W - 1, W, 31, 32, 33, 63, 64, 65, -1):
            sh = ["sll a2, a0, a1", "srl a3, a0, a1", "sra a4, a0, a1"]
            out.append(("shift by register", sh + (["sllw a5, a0, a1", "srlw a6, a0, a1", "sraw a1, a0, a1"] if xlen == 64 else []), x, k, True))
        # memory: store and reload through the data pointer a7, also with mixed widths
        out.append(("memory", ["sw a0, 0(a7)", "sw a0, 12(a7)", "lw a2, 0(a7)", "lw a3, 12(a7)"], x, 0, True))
        if xlen == 64:
            out.append(("memory", ["sd a0, 8(a7)", "ld a2, 8(a7)", "lw a3, 8(a7)", "lw a4, 12(a7)"], x, 0, True))
            out.append(("memory", ["sw a0, 16(a7)", "lwu a2, 16(a7)"], x, 0, x >= 0))
            out.append(("memory", ["sw a0, 0(a7)", "sw a0, 4(a7)", "ld a2, 0(a7)"], x, 0, x in (0, -1)))
        out.append(("upper immediates", ["lui a2, 1", "auipc a3, 0", "add a4, a2, a0", "sub a5, a3, a0"], x, 0, False))
        out.append(("jumps", ["jal a2, 1f", "addi a0, a0, 1", "1:", "auipc a3, 0", "jalr a4, 12(a3)", "addi a0, a0, 2",
                              "addi a0, a0, 4"], x, 0, True))
    # pointers: a7 = DATA_BASE is encoded as 0, so tests against zero and comparisons must be flagged
    for body in (["beqz a7, 1f", "li a2, 1", "1:"], ["bnez a7, 1f", "li a2, 1", "1:"], ["sltu a2, zero, a7"],
                 ["addi a2, a7, 8", "bltu a7, a2, 1f", "li a3, 1", "1:"], ["addi a2, a7, 8", "sub a3, a2, a7"],
                 ["srli a2, a7, 2"], ["li a2, 3", "divu a3, a7, a2"], ["andi a2, a7, 7"],
                 ["addi a2, a7, 12", "sw a2, 0(a7)", "lw a3, 0(a7)"]):
        out.append(("pointer", body, 0, 0, False))
    return out


def edges(W, xlen):
    isa = isas.get(f"riscv{xlen}")
    m = machine(isa, W, steps=22, slots=32, mem=8)
    L = 1 << (W - 1)
    stats = {}
    for group, body, x, y, exact in edge_cases(W, xlen):
        code = asm(body + ["ret"], isa)
        assert code, body
        info = {}
        cls, why = run_case(m, code, [x, y, 0, 0, 0, 0, 0, 0], [0] * 8, data_ptr_reg=7, isa=isa, info=info)
        g = stats.setdefault(group, {})
        g[cls] = g.get(cls, 0) + 1
        assert cls != "UNFLAGGED", (W, xlen, body, x, y, why)
        assert cls != "discarded", (W, xlen, body, x, y, why)
        representable = -L <= x < L and -L <= y < L
        if exact and representable:
            assert info["same"], f"W={W} RV{xlen} {body} a0={x} a1={y}: circuit differs from the CPU: {why}"
        if info["same"]:
            bad = [i for i in info["issues"] if "does not fit" not in i and "outside data memory" not in i]
            assert not bad, f"W={W} RV{xlen} {body} a0={x} a1={y}: circuit equals the CPU but the model disagrees: {bad[0]}"
    return stats


# ------------------------------------------------------------------ 5. fuzzing
def gen(r, n, xlen, W):
    L, out, lab = 1 << (W - 1), [], 0
    reg = lambda: f"a{r.randrange(7)}"
    for _ in range(n):
        k = r.randrange(12)
        if k == 0:
            out.append(f"{r.choice(R_OPS + R_OPS_U)} {reg()}, {reg()}, {reg()}")
        elif k == 1 and xlen == 64:
            out.append(f"{r.choice(W_OPS + W_OPS_U)} {reg()}, {reg()}, {reg()}")
        elif k == 2:
            out.append(f"{r.choice(I_OPS)} {reg()}, {reg()}, {r.choice([r.randrange(-2048, 2048), r.randrange(-8, 8)])}")
        elif k == 3:
            out.append(f"{r.choice(['slli', 'srli', 'srai'])} {reg()}, {reg()}, {r.choice([r.randrange(xlen), r.randrange(W)])}")
        elif k == 4 and xlen == 64:
            out.append(f"{r.choice(['slliw', 'srliw', 'sraiw', 'addiw'])} {reg()}, {reg()}, {r.randrange(32)}")
        elif k == 5:
            out.append(f"{r.choice(['lui', 'auipc'])} {reg()}, {r.choice([0, 1, r.randrange(16), r.randrange(1 << 20)])}")
        elif k == 6:
            lab += 1
            out += [f"{r.choice(BR)} {reg()}, {reg()}, {lab}f", f"addi {reg()}, {reg()}, {r.randrange(-8, 8)}", f"{lab}:"]
        elif k == 7:
            off = 4 * r.randrange(6)
            out.append(f"{r.choice(['sw', 'lw'] + (['sd', 'ld', 'lwu'] if xlen == 64 else []))} {reg()}, {off}(a7)")
        elif k == 8:
            lab += 1
            out += [f"jal {reg()}, {lab}f", f"xori {reg()}, {reg()}, {r.randrange(64)}", f"{lab}:"]
        elif k == 9:
            a = reg()
            out += [f"auipc {a}, 0", f"jalr {reg()}, 12({a})", f"addi a0, a0, {r.randrange(1, 8)}"]
        elif k == 10:
            out.append(f"li {reg()}, {r.choice([r.randrange(-L, L), r.randrange(-8, 8), r.randrange(-2048, 2048)])}")
        else:
            out.append(f"{r.choice(['add', 'sub', 'mul', 'slt', 'xor'])} {reg()}, {reg()}, {reg()}")
    return out + ["ret"]


def fuzz(W, xlen, N, seed):
    isa = isas.get(f"riscv{xlen}")
    m = machine(isa, W)
    r = random.Random(seed)
    L = 1 << (W - 1)
    st = dict(generated=0, discarded=0)
    while st["generated"] < N:
        st["generated"] += 1
        code = assemble(gen(r, r.randrange(4, 12), xlen, W), isa)
        assert code
        args = [r.choice([r.randrange(-L, L), r.randrange(-6, 7)]) for _ in range(7)] + [0]
        if r.random() < 0.2:                  # sometimes one value outside the model
            args[r.randrange(7)] = r.choice([L, -L - 1, 1 << 20, -(1 << 31)])
        mem = [r.getrandbits(32) if r.random() < 0.1 else r.choice([r.randrange(-L, L), r.randrange(8)]) for _ in range(8)]
        info = {}
        cls, why = run_case(m, code, args, mem, data_ptr_reg=7, isa=isa, info=info)
        if cls == "discarded":
            st["discarded"] += 1
            continue
        st[cls] = st.get(cls, 0) + 1
        assert cls != "UNFLAGGED", (W, xlen, why)
        if info["same"]:
            bad = [i for i in info["issues"] if "does not fit" not in i and "outside data memory" not in i]
            assert not bad, f"circuit equals the CPU but the model disagrees: {bad[0]}"
    return st


def stub_matches_source():
    with tempfile.TemporaryDirectory() as d:
        o, b = os.path.join(d, "s.o"), os.path.join(d, "s.bin")
        subprocess.run(["clang", "--target=riscv64", "-march=rv64im", "-mno-relax", "-c",
                        os.path.join(ROOT, "docs", "output_stub_riscv.s"), "-o", o], check=True)
        subprocess.run(["llvm-objcopy", "-O", "binary", "--only-section=.text", o, b], check=True)
        data = open(b, "rb").read()
    return [int.from_bytes(data[i:i + 4], "little") for i in range(0, len(data), 4)] == OUTPUT_STUB_RV


if __name__ == "__main__":
    N = int(sys.argv[1]) if len(sys.argv) > 1 else 150
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else 7
    widths = [int(x) for x in sys.argv[3].split(",")] if len(sys.argv) > 3 else [8, 12, 16]
    assert decoder_table()
    print("1) decoder patterns: mutually exclusive on 40000 random words; every assembled instruction has a class")
    print(f"2) trap: {traps()} unsupported words trap in the circuit and change no register, memory cell or PC")
    print(f"3) superposition of a valid and an invalid instruction word: {superposed_valid_and_invalid()}")
    total = 0
    for xlen in (64, 32):
        for W in widths:
            s = edges(W, xlen)
            n = sum(sum(v.values()) for v in s.values())
            total += n
            print(f"4) boundary cases RV{xlen} W={W}: {n} cases {s}")
    print(f"   {total} boundary cases: no unflagged mismatch; exact wherever the inputs are representable")
    for xlen in (64, 32):
        for i, W in enumerate(widths):
            print(f"5) fuzz RV{xlen} W={W} seed={seed + 4 * i}: {fuzz(W, xlen, N, seed + 4 * i)}")
    assert stub_matches_source()
    print("6) output stub words equal the assembled docs/output_stub_riscv.s")
    print("PASS")
