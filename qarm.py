"""Compile OpenQASM 2 into CPU-skippable Q-ARM packets and execute Q-ARM images."""
import argparse
import json
from pathlib import Path
import struct
from qcpu.qarm import compile_qasm, make_machine
from qcpu.sparsesim import SparseState


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    comp = sub.add_parser("compile", help="emit .bin, callable AArch64 .s, and readout .json")
    comp.add_argument("source", type=Path)
    comp.add_argument("-o", "--output", type=Path, required=True, help="output filename prefix")
    comp.add_argument("--symbol", default="qarm_kernel")
    run = sub.add_parser("run", help="execute a raw mixed ARM/Q-ARM .bin image")
    run.add_argument("binary", type=Path)
    run.add_argument("--qubits", type=int, help="quantum bank size (otherwise from matching .json)")
    run.add_argument("--steps", type=int, help="required cycle budget for loops")
    run.add_argument("--width", type=int, default=8, choices=range(6,17))
    run.add_argument("--shots", type=int, default=1024)
    run.add_argument("--seed", type=int, default=2026)
    a = parser.parse_args()
    try:
        if a.command == "compile":
            compiled = compile_qasm(a.source.read_text())
            assembly = compiled.assembly(a.symbol)
            prefix = str(a.output)
            Path(prefix + ".bin").write_bytes(struct.pack(f"<{len(compiled.words)}I", *compiled.words))
            Path(prefix + ".s").write_text(assembly)
            Path(prefix + ".json").write_text(json.dumps({"format": "qarm-v1", "qubits": compiled.qubits,
                "clbits": compiled.clbits, "measurements": compiled.measurements}, indent=2) + "\n")
            print(f"Wrote {prefix}.bin, {prefix}.s, {prefix}.json")
        else:
            if a.shots <= 0:
                raise ValueError("shots must be positive")
            raw = a.binary.read_bytes()
            if len(raw) % 4:
                raise ValueError("binary length must be a multiple of four bytes")
            words = list(struct.unpack(f"<{len(raw)//4}I", raw))
            meta_path = a.binary.with_suffix(".json")
            meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
            if meta and meta.get("format") != "qarm-v1":
                raise ValueError("unsupported readout manifest format")
            qubits = a.qubits if a.qubits is not None else meta.get("qubits")
            if qubits is None:
                raise ValueError("supply --qubits or a matching .json readout manifest")
            if meta and meta.get("qubits") != qubits:
                raise ValueError("--qubits disagrees with readout manifest")
            measurements = meta.get("measurements", [])
            clbits = meta.get("clbits", 0)
            if not isinstance(clbits, int) or clbits < 0:
                raise ValueError("invalid classical register size")
            for q, c in measurements:
                if not (isinstance(q, int) and isinstance(c, int) and 0 <= q < qubits and 0 <= c < clbits):
                    raise ValueError("invalid terminal measurement mapping")
            m, ones = make_machine(words, qubits, a.steps, a.width)
            state = SparseState(m.n, ones)
            state.apply(m.g)
            if state.probabilities(m.trap).get(0, 0) < 1 - 1e-9:
                raise ValueError("execution trapped")
            if state.probabilities(m.run[-1:]).get(0, 0) < 1 - 1e-9:
                raise ValueError("step budget exhausted before halt; increase --steps")
            counts = state.sample(m.Q, a.shots, a.seed)
            if measurements:
                mapped = {}
                for qvalue, count in counts.items():
                    value = 0
                    for q, c in measurements:
                        value = (value & ~(1 << c)) | (((qvalue >> q) & 1) << c)
                    key = format(value, f"0{clbits}b")
                    mapped[key] = mapped.get(key, 0) + count
                counts = mapped
            else:
                counts = {format(k, f"0{qubits}b"): v for k, v in counts.items()}
            print(json.dumps({"counts": counts, "shots": a.shots, "seed": a.seed,
                "readout": "QASM classical bits" if measurements else "quantum bank",
                "qubits": m.n, "gates": len(m.g), "cycles": m.cfg.STEPS,
                "arm_registers": {f"x{k}": state.probabilities(q) for k,q in m.X.items()}}, indent=2))
    except Exception as exc:
        parser.exit(1, f"Q-ARM: {exc}\n")


if __name__ == "__main__":
    main()
