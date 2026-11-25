# Q-ARM v1: quantum instructions inside an AArch64-compatible code image

This branch adds an experimental quantum extension to the existing ARM-on-QPU
interpreter. It is not an official Arm ISA extension, a full OpenQASM 2 runtime,
or a claim of quantum speedup. The existing `arm_qpu.py` interface and plain
`QuantumARM` remain unchanged in behavior. Use `qarm.py` / `QuantumQARM` for Q-ARM.

## Quick start

From the repository root, using Python 3.10+:

```sh
python -m pip install -r requirements.txt
python qarm.py compile examples/qarm/bell.qasm -o bell
python qarm.py run bell.bin --shots 1024 --seed 2026
```

Compilation creates:

- `bell.bin`: little-endian AArch64 instruction words containing Q-ARM packets,
  ending in RET. This is a callable function image, not an OS executable.
- `bell.s`: equivalent callable assembly, exporting `qarm_kernel` by default.
- `bell.json`: quantum bank size and terminal QASM measurement mapping. Keep it
  next to the binary for the runner. Measurement metadata is not an ARM opcode.

The Bell example produces only `00` and `11`, approximately equally often. Output
bit strings put the highest classical bit on the left; register declarations
are flattened in declaration order. Unmeasured classical bits remain zero.
If there are no QASM measurements, the runner samples the entire quantum bank.
The sparse simulator is exact up to floating-point arithmetic and pruning of
amplitudes below 1e-12; it is intended for small superpositions, not large-scale
quantum simulation. Encoded angles have the finite precision described below.

## What an ordinary CPU does

Each packet starts with **B .+32**, a standard AArch64 unconditional branch.
A normal CPU jumps over all seven payload words. Q-ARM performs the quantum
operation coherently during that same machine cycle and advances to the same
next ARM instruction. There are no undefined instructions or reserved HINT
encodings in the executed native path. The branch does not change NZCV or LR.

This is control-flow compatibility, not equivalence of quantum results. The CPU
executes the classical part and skips the quantum part. Programs that require
quantum answers need a separate classical fallback; none is silently invented.
Only enter packets at their branch word. Jumps into payload data are invalid;
the validator rejects direct branches into payloads. Indirect targets are the
caller's responsibility. The packet format is opt-in to the Q-ARM runtime.

The generated `.s` can be assembled and linked into an ordinary AArch64 program:

```sh
clang --target=aarch64-linux-gnu -c bell.s -o bell.o
```

Declare `extern void qarm_kernel(void);` in a C caller and link `bell.o` using
your normal AArch64 toolchain/sysroot. It returns normally on a native CPU.
The raw binary requires a caller-provided return address; it cannot be launched
as a process directly. Q-ARM's runner supplies its own return-to-HLT sentinel,
which is not part of the generated CPU function. OS ABI, linking, and the
existing reduced-width ARM execution model are otherwise unchanged.

## Mixing ARM arithmetic and quantum operations

`include/qarm.inc` provides GNU/LLVM assembler macros:
`QARM_U target,theta,phi,lambda`, `QARM_CX control,target`,
`QARM_PHASE theta`, and convenience `QARM_H`, `QARM_X`, `QARM_T`.
Angle operands are integer words, not radians. `0x20000000` represents pi,
`0x10000000` pi/2, and `0x08000000` pi/4.

`examples/qarm/mixed.s` contains MOV, H, CX, ADD, RET. Both execution modes
return 8 in W0. Q-ARM additionally prepares a Bell pair.

```sh
clang --target=aarch64-linux-gnu -c examples/qarm/mixed.s -o mixed.o
llvm-objcopy -O binary --only-section=.text mixed.o mixed.bin
python qarm.py run mixed.bin --qubits 2
```

The Q-ARM CLI consumes flat `.bin` images. Extract the fully linked `.text`
section if your assembly uses external symbols; `objcopy` does not resolve
relocations. It does not yet add Q-ARM support to the original ELF/Mach-O loader.
The convenient CLI configures only registers used by the code and no data RAM.
Use the Python API with `Config(MEM=..., ...)` for ARM data-memory programs.
Supply `--steps` for loops; exhaustion is an error rather than a successful run.
The existing 6..16-bit ARM value model still applies; this is not full-width
64-bit CPU emulation. The CLI increases width as needed for code addresses.

## OpenQASM 2 support

Qiskit's strict OpenQASM 2 parser handles built-in U and CX, its bundled
`qelib1.inc`, custom unitary gate definitions, constant expressions, and register
broadcasting. Unitary operations are lowered to U/CX with optimization disabled.
Any global phase introduced by lowering is emitted as PHASE, so it is retained
when programs themselves are superposed. Only the bundled `qelib1.inc` include
is available; arbitrary filesystem includes are not searched. Additional gate
names must be defined in the source; do not assume all SDK extensions belong
to `qelib1.inc` (for example, define `swap` yourself).

Barriers have no effect in this logical interpreter. Terminal measurements are
kept as readout metadata and performed after the machine halts; repeated writes
to a classical bit use the last listed measurement. This works because no
quantum operation follows the first measurement. It does not implement dynamic
measurement instructions, classical feedback, or reset. These, opaque gates,
and gates following measurement are rejected explicitly. A compiler error
produces no new output artifacts. File I/O errors may leave partial output.

If a compiled function is inserted into a larger quantum program, terminal
readout must still occur only at the END of that larger quantum program. Its
manifest is a final readout specification, not a request to measure on return.

## Packet ABI (version 1)

Every packet is eight 32-bit words, little-endian, aligned to four bytes.

| Word | Contents |
| --- | --- |
| 0 | `0x14000008`: B .+32 |
| 1 | `0x4d524151`: ASCII bytes QARM |
| 2 | `[31:24] version=1`, `[23:16] opcode`, `[15:8] b`, `[7:0] a` |
| 3 | signed theta angle word |
| 4 | signed phi angle word |
| 5 | signed lambda angle word |
| 6, 7 | zero, reserved |

| Opcode | Meaning | Restrictions |
| --- | --- | --- |
| 1 | U(theta,phi,lambda) on quantum qubit a | b=0 |
| 2 | CX with control a, target b | distinct valid indices; all angles zero |
| 3 | PHASE(theta) on the selected execution branch | a=b=phi=lambda=0 |

Angles are signed 32-bit integers multiplied by `pi / 2^29`. The compiler rounds
to the nearest representable value, with maximum per-angle error `pi / 2^30`
radians (about 2.93e-9). Encoding is periodic over 8*pi. In particular, theta is
NOT reduced modulo 2*pi: a controlled U with theta=2*pi has a meaningful minus
sign. Quantum indices range from 0 to 255 and must be inside the configured bank.
The 256 limit is an encoding limit, not a practical simulation capacity.

Unknown versions, invalid operands, nonzero reserved fields, and truncated
packets are rejected before loading. A recognized complete packet with invalid
fields in a quantum program state sets a reversible fault flag and traps.
Superpositions must consist of valid code images and valid entry addresses;
this is not a verifier for every arbitrary quantum program-memory state.

## Coherent execution and reversal

`QuantumQARM` subclasses `QuantumARM`. It allocates a separate `quantum` bank,
fetches a packet from quantum program memory using the quantum PC, and computes
its opcode/operand selections reversibly. It applies U/CX/PHASE in place to the
quantum bank, then uncomputes only the decoder. The usual ARM cycle executes the
packet's B instruction and records its ordinary execution history.

The quantum bank never goes through the ARM result-copy/overwrite mechanism:
copying an arbitrary quantum state there would be wrong. Quantum operations are
outside the ARM compute/uncompute region, so they are not accidentally undone.
ARM registers and flags may still coherently control branches. There is no
mid-execution host dispatch or measurement. For a fixed configuration, the
same circuit handles all valid loaded code words, including superposed operands
and angles. Different branch histories remain entangled until uncomputed.

U is implemented as P(phi) RY(theta) P(lambda), using controlled rotations selected
by angle bits. The inverse must negate rotation angles as well as reverse gate
order. Use `SparseState.apply(m.g, inverse=True)` or `m.to_qiskit().inverse()`;
**reversing the gate list alone is no longer an inverse**. Basis-only
`run_fast` is explicitly disabled for Q-ARM.

```python
from qcpu.qarm import compile_qasm, make_machine
from qcpu.sparsesim import SparseState

program = compile_qasm(open("examples/qarm/bell.qasm").read())
machine, ones = make_machine(program.words, program.qubits)
state = SparseState(machine.n, ones)
state.apply(machine.g)
print(state.probabilities(machine.Q))
state.apply(machine.g, inverse=True)  # restores input, including workspace

# Export the complete interpreter circuit, not merely the compiled Bell gates.
circuit = machine.to_qiskit(ones, measure=["quantum"])

# For Aer MPS, decompose rotations without imposing a backend qubit-count limit.
from qiskit import transpile
from qiskit_aer import AerSimulator
circuit = transpile(circuit, basis_gates=["u", "cx", "ccx", "cswap", "cp"],
                    optimization_level=0)
counts = AerSimulator(method="matrix_product_state").run(circuit, shots=128).result().get_counts()
```

Qiskit export is suitable for simulator/backend transpilation. No physical-QPU
execution or acceleration is claimed. Packet fetch and quantum operand selection
are deliberately simple and expensive: program-memory cost grows with image
size, and the current CX decoder enumerates qubit pairs. Even the Bell example
uses over a thousand total qubits once processor state and history are counted.

## Validation

```sh
python -m pip install pytest
python -m pytest -q tests/test_qarm.py
```

Tests cover Bell amplitudes, phase/interference, custom gates and broadcasts,
negative angles, global phase, inverse workspace restoration, superposed
quantum operands, coherent ARM branching, loops, malformed packets, unsupported
QASM, plain ARM execution, native AArch64 execution under Unicorn, and complete
Qiskit export under Aer. Assembly round-trip tests run when clang and
llvm-objcopy are installed; otherwise pytest reports an explicit skip.

Sources: [OpenQASM 2 paper](https://arxiv.org/abs/1707.03429),
[OpenQASM repository](https://github.com/openqasm/openqasm), and
[Arm A64 ISA guide](https://documentation-service.arm.com/static/68cd1a81cccf2a5517018d62).
