"""Circuit fingerprints: the gate list of each front end for a few fixed configurations.

The shared core (qcpu/core.py) is used by every architecture, so a change made for one front end can
silently change another. This test fails when any of the circuits below changes in any gate. The
AArch64 values were recorded before the core was split out of the AArch64 machine, so they also show
that the split left the AArch64 circuits exactly as they were.

If a circuit change is intended, rerun with --print and paste the new table.
Run: python3 -m tests.test_fingerprint [--print]"""
import hashlib, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from qcpu.machine import Config, QuantumARM
from qcpu.rv_machine import RVConfig, QuantumRISCV

ARM = {
    "full W12 mem16 sp": dict(W=12, regs=tuple(range(31)), sp=True, SLOTS=64, STEPS=3, MEM=16),
    "full W16 mem64 sp": dict(W=16, regs=(0, 1, 2, 3, 8, 9, 19, 29, 30), sp=True, SLOTS=128, STEPS=2, MEM=64),
    "W8 nomem": dict(W=8, regs=tuple(range(8)) + (30,), sp=False, SLOTS=32, STEPS=4, MEM=0),
    "reduced": dict(W=9, regs=(0, 1, 2, 8, 9, 10, 11, 30), sp=False, SLOTS=32, STEPS=5, MEM=0,
                    classes=("addsub_imm", "bcond", "bfm", "bra", "cbz", "csel", "div", "hlt", "logic_imm", "logic_reg", "madd", "movw")),
    "tiny": dict(W=8, regs=(0, 30), SLOTS=4, STEPS=4, MEM=0, classes=("movw", "addsub_imm", "bra", "hlt")),
}
RISCV = {
    "rv64 W12 mem16": dict(W=12, regs=tuple(range(1, 32)), SLOTS=64, STEPS=3, MEM=16, xlen=64),
    "rv32 W12 mem16": dict(W=12, regs=tuple(range(1, 32)), SLOTS=64, STEPS=3, MEM=16, xlen=32),
    "rv64 W8 nomem": dict(W=8, regs=(1, 10, 11, 12), SLOTS=16, STEPS=4, MEM=0, xlen=64),
    "rv64 tiny": dict(W=8, regs=(1, 10), SLOTS=4, STEPS=3, MEM=0, xlen=64, classes=("opimm", "jalr", "ebreak")),
}
EXPECTED = {
    "arm64 / full W12 mem16 sp": (6325, 194487, "ce894a3c80c3caae8db0"),
    "arm64 / full W16 mem64 sp": (10365, 203438, "8bfcfd6e7698da8b9f3a"),
    "arm64 / W8 nomem": (2822, 105676, "f53e9cdbbb410b8e0390"),
    "arm64 / reduced": (2782, 127800, "3fdbfbb79524b58d593a"),
    "arm64 / tiny": (418, 7448, "2366ad83d3cdc1c38dab"),
    "riscv / rv64 W12 mem16": (4602, 86457, "d33cea029d0daf663305"),
    "riscv / rv32 W12 mem16": (4500, 80325, "a3f21fc9c18aded9adda"),
    "riscv / rv64 W8 nomem": (1523, 41496, "c2cc3311e2d72b5916d5"),
    "riscv / rv64 tiny": (336, 5970, "7ec4e45d5371dc65f1db"),
}


def fingerprints():
    out = {}
    for arch, table, cfg, cls in (("arm64", ARM, Config, QuantumARM), ("riscv", RISCV, RVConfig, QuantumRISCV)):
        for name, kw in table.items():
            m = cls(cfg(**kw))
            h = hashlib.sha256(repr((m.n, m.g, sorted(m.names.items()), m.pc, m.run, m.trap)).encode()).hexdigest()[:20]
            out[f"{arch} / {name}"] = (m.n, len(m.g), h)
    return out


if __name__ == "__main__":
    got = fingerprints()
    if "--print" in sys.argv:
        for k, v in got.items():
            print(f'    "{k}": {v},')
        sys.exit(0)
    bad = [k for k in got if k in EXPECTED and got[k] != EXPECTED[k]] + [k for k in got if k not in EXPECTED]
    for k, v in got.items():
        print(f"{'ok  ' if k not in bad else 'DIFF'} {k:28s} {v[0]:6d} qubits {v[1]:7d} gates {v[2]}")
    print("PASS: all circuits unchanged" if not bad else f"FAIL: {len(bad)} circuit(s) changed or not recorded")
    sys.exit(1 if bad else 0)
