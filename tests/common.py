"""Shared helpers for the tests: assemble with clang, run a program on the CPU reference and on
the quantum machine, and classify the outcome. Works for every architecture in qcpu/isa.py.

Outcome classes (the soundness requirement is that 'unflagged mismatch' never occurs):
  match          QPU == CPU at every cycle (registers, flags, memory, PC) and the model check is clean
  match-flagged  QPU == CPU, the model check reported values outside the W-bit model
  flagged        QPU != CPU, and the model check reported an issue at or before the first cycle
                 where they differ   (a known, detected limit)
  UNFLAGGED      QPU != CPU and the model check said nothing, or only about a later instruction   (a bug)
  discarded      the CPU run did not halt normally (exception, memory fault, step limit)
"""
import os, re, subprocess, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from qcpu.bitsim import value
from qcpu.reference import DATA_BASE
from qcpu import isa as isas

ARM = isas.get("arm64")


def assemble(lines, isa=ARM):
    """lines: assembly source lines -> list of 32-bit words, or None if clang rejects them."""
    with tempfile.TemporaryDirectory() as d:
        s, o, b = (os.path.join(d, n) for n in ("p.s", "p.o", "p.bin"))
        open(s, "w").write(".text\n" + "\n".join(lines) + "\n")
        r = subprocess.run(["clang"] + [f for f in isa.clang if f != "-fno-pic"] + ["-c", s, "-o", o], capture_output=True)
        if r.returncode:
            return None
        subprocess.run(["llvm-objcopy", "-O", "binary", "--only-section=.text", o, b], check=True)
        code = open(b, "rb").read()
        return [int.from_bytes(code[i:i + 4], "little") for i in range(0, len(code), 4)]


def run_case(m, code_words, args=(), mem=(), data_ptr_reg=None, isa=ARM, info=None):
    """Run one program on machine m (built with defer=True) and on the CPU; compare every cycle.
    args: initial values of the argument registers; data_ptr_reg: index of the one that points to data memory.
    info: optional dict that receives the reference's issues and the first diverging cycle."""
    c = m.cfg
    W, mk = c.W, (1 << c.W) - 1
    if len(code_words) >= c.SLOTS:
        return "discarded", "program too long"
    words = isa.layout(code_words, c.SLOTS)
    cpu_args = list(args)
    if data_ptr_reg is not None:
        cpu_args[data_ptr_reg] = DATA_BASE
    ref = isa.run_reference(words, W, 0, cpu_args, c.MEM, mem, max_steps=c.STEPS, snapshots=True,
                        sp_off=4 * c.MEM - 64 if c.MEM else None)
    if not ref["halted"]:
        return "discarded", ref["error"] or "did not halt"
    init = {isa.arg_regs[i]: (0 if i == data_ptr_reg else a) for i, a in enumerate(args) if isa.arg_regs[i] in m.X}
    init[isa.link_reg] = 4 * (c.SLOTS - 1)
    if isa.sp_reg in m.X:
        init[isa.sp_reg] = 4 * c.MEM - 64
    ones = m.load_bits(words, init, [v & 0xFFFFFFFF for v in mem], pc=0)
    diverged = []

    def on_cycle(k, st):
        if diverged or k >= len(ref["snapshots"]):
            return
        sn = ref["snapshots"][k]
        for r, q in m.X.items():
            if value(st, q) != sn["regs"][r] & mk:
                diverged.append(f"cycle {k}: {isa.reg_name(r)}"); return
        if [st[x] for x in m.nzcv] != sn["flags"]:
            diverged.append(f"cycle {k}: nzcv"); return
        for i, cell in enumerate(m.ram):
            if value(st, cell) != int.from_bytes(sn["mem"][4 * i:4 * i + 4], "little") & mk:
                diverged.append(f"cycle {k}: mem[{i}]"); return
    out = m.run_fast(ones, on_cycle=on_cycle)
    same = not diverged and out["trace"][:ref["steps"]] == ref["trace"] and not out["trap"] and out["run"] == 0
    flagged = bool(ref["issues"])
    steps = [int(mm.group(1)) if mm else -1 for mm in (re.match(r"step (\d+)", i) for i in ref["issues"])]
    cycle = int(re.match(r"cycle (\d+)", diverged[0]).group(1)) if diverged else None
    if info is not None:
        info.update(issues=ref["issues"], cycle=cycle, same=same)
    if same:
        return ("match-flagged" if flagged else "match"), ""
    # an issue excuses a mismatch only if it concerns an instruction at or before the first difference
    explained = flagged and (cycle is None or min(steps) <= cycle)
    return ("flagged" if explained else "UNFLAGGED"), (diverged[0] if diverged else "trace/halt") + \
        (" | " + ref["issues"][0] if flagged else "")
