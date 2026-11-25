# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Quantag IT Solutions GmbH
"""Grover's search for the Code Breaker secret, with the C predicate consistent() from solver.c as the oracle.

    python3 qcpucc.py --qcpu-mode qoracle --qcpu-export oracle examples/codebreaker/solver.c -o solver
    python3 examples/codebreaker/grover_solver.py oracle/consistent_phase.qasm

The oracle is read from the exported OpenQASM 3 file alone (the same file loads in Qiskit, and
oracle/consistent_cudaq.py holds it as a CUDA-Q kernel). It has 1,045 qubits, far too many for a
state-vector simulator, but Grover's search puts only the 8 qubits of the secret in superposition:
the state is a sum over the 256 codes, every work qubit is 0 between oracle calls, so this script
keeps the 256 amplitudes and runs each basis state through the circuit's gates exactly. Each code is
run through the circuit once; the result is reused in later iterations, as the circuit is fixed."""
import math
import re
import sys

COLOURS = "RGBY"


def read_qasm(path):
    """Qubit registers and gates of an OpenQASM 3 file written by qcpucc --qcpu-export (x, cx, ccx, z)."""
    regs, index, gates = {}, {}, []
    for line in open(path):
        line = line.split("//")[0].strip()
        m = re.fullmatch(r"qubit\[(\d+)\] (\w+);", line)
        if m:
            regs[m.group(2)] = (len(index), int(m.group(1)))
            for b in range(int(m.group(1))):
                index[f"{m.group(2)}[{b}]"] = len(index)
            continue
        m = re.fullmatch(r"(x|cx|ccx|z) (.+);", line)
        if m:
            gates.append((m.group(1), *[index[q.strip()] for q in m.group(2).split(",")]))
        elif line and not line.startswith(("OPENQASM", "include", "gphase")):
            raise ValueError(f"{path}: not supported here: {line}")
    return regs, len(index), gates


def oracle(gates, state):
    """One basis state through the circuit: (sign, state after)."""
    sign = 1
    for g in gates:
        if g[0] == "x":
            state ^= 1 << g[1]
        elif g[0] == "cx":
            state ^= ((state >> g[1]) & 1) << g[2]
        elif g[0] == "ccx":
            state ^= ((state >> g[1]) & (state >> g[2]) & 1) << g[3]
        else:                                                # z
            sign = -sign if (state >> g[1]) & 1 else sign
    return sign, state


def name(code):
    return "".join(COLOURS[(code >> (2 * k)) & 3] for k in range(4))


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "oracle/consistent_phase.qasm"
    regs, nq, gates = read_qasm(path)
    first, bits = regs["secret"]
    N = 1 << bits
    print(f"oracle: {path}: {nq} qubits, {len(gates)} gates; search register 'secret': {bits} qubits, {N} codes")
    signs = []
    for code in range(N):
        sign, after = oracle(gates, code << first)
        assert after == code << first, "work qubits must return to 0"
        signs.append(sign)
    amp = [1 / math.sqrt(N)] * N                             # H on every qubit of 'secret'
    iterations = int(math.pi / 4 * math.sqrt(N))             # for one solution
    for _ in range(iterations):
        amp = [s * a for s, a in zip(signs, amp)]            # the oracle: (-1)^consistent(code)
        mean = sum(amp) / N
        amp = [2 * mean - a for a in amp]                    # diffusion: H, X, multi-controlled Z, X, H
    probs = sorted(((a * a, c) for c, a in enumerate(amp)), reverse=True)
    print(f"after {iterations} Grover iterations ({iterations} oracle calls; a classical search tries up to {N} codes):")
    for p, c in probs[:3]:
        print(f"  {name(c)}  probability {p:.4f}")
    return name(probs[0][1])


if __name__ == "__main__":
    main()
