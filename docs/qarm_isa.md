# The Q-ARM instruction set, version 1.2

**Status:** version 1.2, 2026-11-28. Version 1.1 added `QEVOLVE`, time evolution with a guaranteed error bound,
in the Dynamics profile. Version 1.2 adds `QLINDBLAD`, the same for open systems (Lindblad dynamics with Pauli
jump operators), in the Open profile, and lets `QEVOLVE`'s qDRIFT run on QPUs. Version 1.0 was published on
2026-11-28 (release `isa-v1.0` of github.com/quantag/cpu-qpu). Quantag
IT Solutions GmbH. The specification is under Apache-2.0, like the rest of this repository.

**With it:**
- `include/qarm_isa.h`: the constants and layouts, normative and self-contained (C99);
- `tests/conformance`: the conformance program;
- `docs/qarm_runtime_guide.md`: a guide to writing a runtime, with `runtime/minimal`, a conforming runtime
  of about 200 lines.

Q-ARM is a vendor-neutral way to put quantum work into ordinary ARM64 and x86-64 programs. A program
compiled once runs everywhere:

- on any AArch64 or x86-64 CPU, where every quantum instruction is skipped and the program computes the
  answer classically;
- under a **Q-ARM runtime**, where the same instructions run on a quantum computer or a simulator.

The program's own C functions and tables are the instructions' operands. No OpenQASM or other circuit
language appears in the program.

This document defines what a program may emit and what a runtime must do with it, precisely enough
that a runtime can be written from it alone. `docs/qarm_v2.md` describes the reference implementation
in this repository.

The key words MUST, MUST NOT, SHOULD and MAY are used as in RFC 2119.

## 1. Roles

| role | what it is | in this repository |
|---|---|---|
| **program** | an AArch64 (ARM64) or x86-64 executable or library that contains Q-ARM packets | `examples/qarm/*.c` |
| **library** | the code that emits the packets and implements each instruction's classical path and checks | `include/qarm.h` (header-only C) |
| **runtime** | anything that executes packets: an emulator, a binary translator, a CPU extension, a hypervisor | `qarm_exec.py` (Unicorn), `runtime/qemu` (QEMU TCG plugin) |
| **backend** | where a runtime sends quantum work: a state vector, a noise model, a QPU | libqarm (`runtime/libqarm`): exact, simulator, IBM Quantum |

A program conforms if every packet it emits is well formed (section 2) and every operand satisfies
section 5. A runtime conforms to a **profile** (section 8) if it meets sections 3 and 4 for every
instruction of the profile, and passes the conformance program (section 9).

## 2. The packet

A Q-ARM instruction is a **packet** of eight 32-bit little-endian words, 4-byte aligned, inside
executable code:

| word | value | meaning |
|---|---|---|
| 0 | `0x14000008` | `B .+32`: an unconditional branch over the packet |
| 1 | `0x4d524151` | the magic, "QARM" in ASCII |
| 2 | `V << 24 \| OP << 16` | V = the packet format, 2 for this specification; OP = the opcode; bits 15..0 are 0 |
| 3–7 | `0` | reserved |

On a CPU without a runtime, the branch skips the packet. Words 1–7 are never executed, so the packet is
a no-op on every AArch64 processor.

- A runtime MUST recognize a packet only where the branch in word 0 executes and words 1 and 2 match.
  An ordinary `B .+32` followed by other data is not a packet.
- Packets with V ≠ 2 are not this specification's. Format 1 is the older gate-level format of
  `docs/qarm.md`. A conforming runtime MUST leave them unexecuted.
- Programs MUST write 0 in words 3–7 and in bits 15..0 of word 2. Runtimes conforming to 1.0 MUST ignore
  them. They are reserved for operand encodings in later versions.
- After a packet, execution continues at the instruction after word 7, exactly as on a plain CPU.

### 2.1 x86-64

On x86-64 the packet is the same 32 bytes, with a different word 0:

| bytes | value | meaning |
|---|---|---|
| 0–3 | `eb 1e 00 00` | `JMP .+32` (a short jump over the next 30 bytes), then two zero bytes |
| 4–31 | as words 1–7 above | the magic, `V << 24 \| OP << 16`, five reserved words |

There is no alignment requirement. A runtime recognizes the packet where that `JMP` executes, followed by
the magic.

The ISA's registers map to x86-64 as follows. The mapping keeps `X1`, `X2` in the first two System V
argument registers:

| ISA | X0 (W0) | X1 | X2 | X3 (W3) | X4 (W4) | X5 | D0 | D1 |
|---|---|---|---|---|---|---|---|---|
| x86-64 | RAX (EAX) | RDI | RSI | RDX (EDX) | RCX (ECX) | R8 | XMM0 low 64 bits | XMM1 low 64 bits |

Function operands follow the System V convention (x in `EDI`, the result in `EAX`). Everything else in
this document applies unchanged, the layouts included (LP64, little-endian). `qoracle` builds oracles from
x86-64 machine code, the QEMU plugin runs them in `qemu-x86_64`, and the conformance program passes there too.

## 3. Execution model

**Operands** are in general-purpose registers `X0`–`X5` and in `D0`, `D1` (the low 64 bits of `V0`, `V1`;
of `Z0`, `Z1` when SVE is present; on x86-64 the registers of section 2.1). Memory operands are virtual addresses in the program's address space.
All data is little-endian with LP64 layouts (section 5).

**The status register.** The library MUST set `X0 = 0` before every packet. A runtime that executes the
packet sets `W0` to a status:

| W0 | meaning | the library |
|---|---|---|
| 0 | not executed: no runtime, an unassigned opcode, or a runtime that declines (an operand it cannot translate, a problem larger than its backend) | takes the classical path |
| 1 | executed; the outputs are valid | checks the answer where the instruction defines a check, then returns it |
| 2 | executed; the defined negative result (e.g. "no solution") or, where the instruction says so, refused | returns the negative result, or reports the refusal |
| 3 | QRAND only: filled, but from a simulator | returns the bytes, marked pseudo-random |

**What a runtime may change.**
- A runtime MUST NOT change any register other than an instruction's listed outputs. `X0` and the
  listed outputs are the only exceptions.
- A runtime MUST NOT write any memory other than an instruction's output buffers, within their stated
  sizes.
- Unlisted output registers hold unspecified values when `W0 ≠ 1`.
- The condition flags, `SP` and the PC are preserved. The PC advances past the packet.

**Unknown opcodes.** A runtime MUST leave `W0 = 0` for an opcode it does not implement. It MAY print a
diagnostic. A program can therefore always run on an older runtime.

**Declining.** A runtime that cannot execute an instruction SHOULD leave `W0 = 0`, so that the program
continues classically. A development runtime MAY stop the program instead, with a diagnostic that names
the reason. `qarm_exec.py` does that for an oracle it cannot translate.

**Synchronous.** Each packet completes before the next instruction executes. The runtime may wait for a
queued QPU job while the program is stopped. The asynchronous form of any instruction is `QSUBMIT`
(section 6.5).

**Randomness.** Quantum instructions are probabilistic. Section 6 states, per instruction, what is
guaranteed: an exact property of every answer, or a bound that holds with a stated probability. Where the
answer has a cheap classical check, the library MUST run it (section 6) before it returns the answer.

## 4. Discovery

`QAVAIL` (0x10) returns the feature bits. `QCAPS` (0x31) describes the backend. A runtime MUST implement
`QAVAIL`. Every bit it reports MUST name instructions that it executes, under the rules of section 3.

| bit | value | instructions |
|---|---|---|
| `QARM_F_SEARCH` | 1 | `QSEARCH` |
| `QARM_F_VQE` | 2 | `QVLOAD`, `QEXPECT`, `QEXPECTN`, `QGRAD`, `QSESSION`, `QVFREE` |
| `QARM_F_MIN` | 4 | `QMIN` |
| `QARM_F_COUNT` | 8 | `QCOUNT` |
| `QARM_F_PERIOD` | 16 | `QPERIOD` |
| `QARM_F_SIMON` | 32 | `QSIMON` |
| `QARM_F_BV` | 64 | `QBV` |
| `QARM_F_AMPEST` | 128 | `QAMPEST` |
| `QARM_F_CIRC` | 256 | `QCIRC` |
| `QARM_F_RAND` | 512 | `QRAND` |
| `QARM_F_CAPS` | 1024 | `QCAPS` |
| `QARM_F_VSAMPLE` | 2048 | `QVSAMPLE` |
| `QARM_F_ASYNC` | 4096 | `QSUBMIT`, `QPOLL`, `QWAIT` |
| `QARM_F_COLLISION` | 8192 | `QCOLLISION` |
| `QARM_F_EVOLVE` | 16384 | `QEVOLVE` (1.1) |
| `QARM_F_LINDBLAD` | 32768 | `QLINDBLAD` (1.2) |

Bits 16–31 are reserved and MUST be 0 in 1.2 (bits 15–31 in 1.1, bits 14–31 in 1.0). A runtime that returns `W0 = 1` with `W1 = 0` is a runtime
from before feature bits. Libraries treat it as `QARM_F_SEARCH` only.

## 5. Operands

### 5.1 Qubits and bits

Qubit q corresponds to bit q of every integer that names a basis state, a measurement outcome or a Pauli
mask. Bit 0 is the least significant. A register has at most 64 qubits in 1.0.

### 5.2 Pauli operators and Hamiltonians

A Pauli operator is a pair of 64-bit masks (x, z):

  P(x, z) = i^popcount(x & z) · X^x · Z^z

That is, X on qubit q when only bit q of x is set, Z when only bit q of z is set, and Y when both are.

```c
typedef struct { uint64_t x, z; double c; } qarm_pauli;                                   /* 24 bytes */
typedef struct { uint32_t qubits, terms; double constant; const qarm_pauli *t; } qarm_hamiltonian;  /* 24 */
```

H = constant · I + Σₖ cₖ · P(xₖ, zₖ). The masks MUST NOT have bits at or above `qubits`.

### 5.3 Ansatz tables

```c
enum { QARM_X = 1, QARM_H, QARM_CX, QARM_CZ, QARM_RY, QARM_RZ, QARM_PAULI_ROT };
typedef struct { uint8_t kind, q0, q1, param; uint32_t reserved; double scale; uint64_t x, z; } qarm_op;  /* 32 */
typedef struct { uint32_t qubits, params, ops, reserved; uint64_t init; const qarm_op *op; } qarm_ansatz;  /* 32 */
```

The state |ψ(θ)⟩ is |init⟩ followed by each operation in order. For RY, RZ and Pauli rotations, the
angle is a = scale · θ[param].

| kind | operation |
|---|---|
| `QARM_X`, `QARM_H` | the gate on `q0` |
| `QARM_CX`, `QARM_CZ` | control `q0`, target `q1` (`q0 ≠ q1`) |
| `QARM_RY`, `QARM_RZ` | exp(−i a Y/2), exp(−i a Z/2) on `q0` |
| `QARM_PAULI_ROT` | exp(−i a P(x, z)) on the qubits of x \| z |

A runtime MUST refuse a table with any of the following, with `W0 = 2` on `QVLOAD`:
- an unknown kind;
- a qubit at or above `qubits`, in an operation, a Pauli term or `init`;
- a Pauli rotation with x = z = 0;
- `param ≥ params` on a parameterized operation;
- `ansatz.qubits ≠ hamiltonian.qubits`.

The `reserved` fields MUST be 0.

### 5.4 Circuit tables

```c
enum { QARM_G_X = 1, QARM_G_Y, QARM_G_Z, QARM_G_H, QARM_G_S, QARM_G_SDG, QARM_G_T, QARM_G_TDG, QARM_G_SX,
       QARM_G_RX, QARM_G_RY, QARM_G_RZ, QARM_G_CX, QARM_G_CZ, QARM_G_SWAP, QARM_G_CCX };
typedef struct { uint8_t kind, q0, q1, q2; uint32_t reserved; double angle; } qarm_gate;            /* 16 */
typedef struct { uint32_t qubits, gates; const qarm_gate *g; } qarm_circuit;                         /* 16 */
```

- Single-qubit gates act on `q0`. RX, RY and RZ are exp(−i·angle·σ/2).
- CX and CZ use control `q0` and target `q1`. SWAP exchanges `q0` and `q1`. CCX uses controls `q0` and
  `q1`, and target `q2`.
- The qubits of one gate MUST be distinct and below `qubits`.
- The circuit starts in |0…0⟩. Every qubit is measured in the Z basis at the end.
- Global phases are not observable and are unspecified.

### 5.5 Function operands (oracles)

Several instructions take the address of a function of the program, `int f(int x)` (AAPCS64: x in
`W0`, the result in `W0`; System V on x86-64: x in `EDI`, the result in `EAX`). The runtime builds the quantum oracle from the function's **machine code**,
for x in 0 … 2^bits − 1. For the operand to be valid:

- f MUST be a leaf function: no calls, no system calls.
- f MUST NOT write memory other than its own stack frame.
- f may read constant data.
- f's result MUST depend only on x and on constant data.
- f MUST NOT use SIMD or floating-point instructions. With GCC and clang this means `-O1`, and helpers
  marked `always_inline`.
- On x86-64, f MUST NOT use the stack (`PUSH`, `POP`, `RSP`- or `RBP`-relative memory) or a jump table.

A runtime's oracle MUST agree with f on every x in the domain. A runtime that cannot establish this
(an instruction it cannot translate, a resource limit) MUST decline (section 3). It MUST NOT execute
with an approximate oracle.

Two kinds of oracle are used:
- **phase oracles:** f(x) ∈ {0, 1}, applied as |x⟩ → (−1)^f(x) |x⟩. Used by `QSEARCH`, `QCOUNT` and `QBV`.
- **compute oracles:** |x⟩|0⟩ → |x⟩|f(x)⟩ on the low 32 bits of the result. Used by `QMIN`, `QPERIOD`,
  `QSIMON`, `QAMPEST` and `QCOLLISION`.

### 5.6 Capabilities

```c
typedef struct { uint32_t version, features, qubits, flags; char backend[48]; } qarm_capabilities;   /* 64 */
enum { QARM_CAPS_HARDWARE = 1, QARM_CAPS_MEASURED = 2, QARM_CAPS_ASYNC = 4 };
```

| field | contents |
|---|---|
| `version` | 1 for this specification |
| `features` | equals QAVAIL's W1 |
| `qubits` | the backend's qubits; for a simulator, the largest register it accepts |
| `flags` | `HARDWARE`: results come from a physical quantum device. `MEASURED`: values are estimated from samples and carry statistical error. `ASYNC`: jobs are queued (latency of seconds to hours) |
| `backend` | a NUL-terminated name, for display only |

### 5.7 Time evolution (1.1)

```c
enum { QARM_EV_AUTO = 0, QARM_EV_EXACT, QARM_EV_PRODUCT, QARM_EV_QDRIFT };
typedef struct {
    const qarm_hamiltonian *h, *o;   /* H, the generator; O, the observable */
    const double *t;                 /* the k times */
    uint32_t k, method;              /* 1 <= k <= 4096; QARM_EV_* */
    uint32_t order, shots;
    uint64_t init;                   /* the initial basis state */
    double eps;                      /* the accuracy asked for; 0: 1e-3 */
    uint32_t steps, reserved;
} qarm_evolution;                                                                       /* 64 bytes */
```

The table asks for the k values

  v(tᵢ) = ⟨init| e^{iHtᵢ} O e^{−iHtᵢ} |init⟩.

| field | meaning |
|---|---|
| `h`, `o` | Hamiltonian tables (5.2) with the same `qubits` |
| `method` | `AUTO`: the runtime's choice. `EXACT`: the exact evolution, to the precision of double arithmetic. `PRODUCT`: a product formula. `QDRIFT`: Campbell's randomized compiler |
| `order` | `PRODUCT` only: 1 (Lie–Trotter), 2 (Strang), 4 or 6 (Suzuki), or 0 for the runtime's choice. MUST be 0 for the other methods |
| `shots` | per time, on a backend with `MEASURED`; for `QDRIFT`, the number of random sequences. 0: the runtime's default |
| `eps` | the accuracy asked for (6.6). 0 means 1e-3 |
| `steps` | 0: the runtime chooses the fewest steps that meet `eps`. Otherwise exactly this many steps (`PRODUCT`) or samples per sequence (`QDRIFT`); `eps` is then not used |
| `reserved` | MUST be 0 |

A runtime MUST refuse a table with `W0 = 2` if any of these holds:
- `h` or `o` is null;
- their `qubits` differ, or are outside 1–64;
- a Pauli mask or `init` has a bit at or above `qubits`;
- `k` is outside 1–4096;
- a time or a coefficient is not finite;
- `method` is above 3, or `order` is not as above;
- `eps` is negative or not finite;
- `reserved` ≠ 0.

### 5.8 Open-system evolution (1.2)

```c
typedef struct {
    qarm_evolution e;                /* 5.7 */
    const qarm_hamiltonian *jumps;   /* the jump operators P_k, with c = the rate gamma_k */
} qarm_lindblad;                                                                        /* 72 bytes */
```

The table asks for the k values v(tᵢ) = Tr(O ρ(tᵢ)), where ρ(0) = |init⟩⟨init| and

  dρ/dt = −i[H, ρ] + Σₖ γₖ (Pₖ ρ Pₖ − ρ).

That is the Lindblad equation with Pauli jump operators. Since P†P = I, the dissipator is PρP − ρ. With
them come dephasing (Z), bit flips (X), and depolarizing noise (X, Y and Z at equal rates). `e`'s fields mean
what they mean for `QEVOLVE`, with these differences:
- `method` is `AUTO`, `EXACT` or `PRODUCT`;
- `order` is 1 (Lie–Trotter), 2 (Strang) or 0. Higher orders have negative stage coefficients, which are not
  channels;
- `shots` is the number of trajectories when the runtime samples (6.7). 0 is the runtime's default.

A runtime MUST refuse with `W0 = 2` what 5.7 refuses, and also these:
- `jumps` is null;
- its `qubits` differ from H's, or a mask has a bit at or above them;
- a rate is negative or not finite, or `jumps`' `constant` ≠ 0;
- `method` is `QDRIFT`, or `order` is 4 or 6;
- a time is negative: an open system evolves forward only.

## 6. Instructions

The tables give the inputs and outputs, then what every answer satisfies. "Check" is what the library
MUST verify, by calling the program's own function, before it returns a `W0 = 1` answer. When the check
fails, the library MUST take the classical path.

A check establishes only what it tests:
- QSEARCH's and QCOLLISION's checks verify the answer completely.
- QMIN's check verifies the reported cost, not that it is the least.
- QPERIOD's, QSIMON's and QBV's checks probe four inputs.

The rest is the "guaranteed" column, which the runtime provides with the stated probability. `W0 = 2` from
QSEARCH, QPERIOD, QSIMON and QCOLLISION means that nothing was found within the instruction's budget. It is
not a proof that nothing exists.

### 6.1 Discovery

| opcode | name | in | out |
|---|---|---|---|
| 0x10 | `QAVAIL` | — | `W0 = 1`, `W1` = feature bits |
| 0x31 | `QCAPS` | `X1` = `qarm_capabilities *` | `W0 = 1`; the 64 bytes written |

### 6.2 Oracle instructions

All take `X1` = the address of f and `W2` = bits (1 ≤ bits ≤ 31).

| opcode | name | out | guaranteed | check |
|---|---|---|---|---|
| 0x11 | `QSEARCH` | `W0 = 1`, `W3` = x with f(x) = 1; `W0 = 2`: none found | an answer has f(x) = 1. `W0 = 2` after a search of O(√N) oracle calls (BBHT); with small probability there is a solution after all | f(x) = 1 |
| 0x15 | `QMIN` | `W0 = 1`, `W3` = x, `W4` = cost(x) (signed 32-bit) | x is a least-cost argument with probability ≥ 1/2 per run (Dürr–Høyer) | cost(W3) = W4 |
| 0x16 | `QCOUNT` | `W0 = 1`, `X3` = M̃, `D0` = ε | \|M̃ − M\| ≤ ε with probability ≥ 8/π², where M = \|{x : f(x) = 1}\| | — |
| 0x17 | `QAMPEST` | in: `W3` = k (value bits). `W0 = 1`, `D0` = μ̃, `D1` = ε | \|μ̃ − μ\| ≤ ε with probability ≥ 8/π², where μ = 2^−bits Σₓ f(x), f(x) ∈ [0, 2ᵏ) | — |
| 0x18 | `QPERIOD` | `W0 = 1`, `W3` = r; `W0 = 2`: none found | r > 0 is the least period: f(x + r) = f(x) wherever both are in the domain | f(i) = f(i + r), i = 0…3 |
| 0x19 | `QSIMON` | `W0 = 1`, `W3` = s ≠ 0; `W0 = 2`: no shift found (a runtime that verifies its candidates on every input, as the reference runtimes do, returns it only when there is none) | f(x) = f(x ⊕ s) for every x | f(i) = f(i ⊕ s), i = 0…3 |
| 0x1A | `QBV` | `W0 = 1`, `W3` = s | f(x) = s·x ⊕ c (mod 2) for a constant c, if f has that form | f agrees at four probe inputs |
| 0x1B | `QCOLLISION` | `W0 = 1`, `W3` = x, `W4` = y; `W0 = 2`: none found | x ≠ y and f(x) = f(y). Brassard–Høyer–Tapp: O(N^1/3) oracle calls for a two-to-one f. `W0 = 2` after three tries, likely when f is one-to-one | x ≠ y, f(x) = f(y) |

### 6.3 Variational instructions

`QVLOAD` copies the tables. The program may then modify or free them. A **handle** is a nonzero 64-bit
value. It is valid until `QVFREE` or the end of the process.

| opcode | name | in | out |
|---|---|---|---|
| 0x20 | `QVLOAD` | `X1` = `qarm_hamiltonian *`, `X2` = `qarm_ansatz *` | `W0 = 1`, `X3` = handle; `W0 = 2`: refused (5.3) |
| 0x21 | `QEXPECT` | `X3` = handle, `X1` = θ (`params` doubles), `W2` = shots (0: the runtime's default) | `W0 = 1`, `D0` = ⟨ψ(θ)\|H\|ψ(θ)⟩, `D1` = its standard error (0 when exact) |
| 0x22 | `QEXPECTN` | `X3` = handle, `X1` = k·`params` doubles, `W2` = k (1 ≤ k ≤ 4096), `X4` = k doubles out | `W0 = 1`, the k energies |
| 0x23 | `QGRAD` | `X3` = handle, `X1` = θ, `X4` = `params` doubles out | `W0 = 1`, `D0` = E(θ), the gradient ∂E/∂θ |
| 0x24 | `QSESSION` | `W1` = 1 to open, 0 to close | `W0 = 1` |
| 0x25 | `QVSAMPLE` | `X3` = handle, `X1` = θ, `W2` = shots, `X4` = shots `uint64_t` out | `W0 = 1`: outcomes of \|ψ(θ)⟩ in the Z basis; `W0 = 2`: refused |
| 0x26 | `QVFREE` | `X3` = handle | `W0 = 1` |

- On a backend with `MEASURED`, the values are estimates from samples. `D1` is QEXPECT's standard error
  from shot noise; a device's own errors add bias that it does not include.
- `QEXPECTN` and `QGRAD` SHOULD be one backend job each, so that a QPU queue is waited for once per
  batch.
- `QEXPECTN` MUST read all its inputs before writing its outputs, so that `X1` and `X4` may overlap.
- `QSESSION` is advisory: it asks the backend to reserve a device for the jobs that follow, and has no
  other effect.

### 6.4 Circuit and randomness instructions

| opcode | name | in | out |
|---|---|---|---|
| 0x30 | `QCIRC` | `X1` = `qarm_circuit *`, `W2` = shots, `X3` = shots `uint64_t` out | `W0 = 1`: the outcomes; `W0 = 2`: refused (5.4) |
| 0x1C | `QRAND` | `X1` = buffer, `W2` = n bytes | `W0 = 1`: bytes from measurements on a physical device; `W0 = 3`: from a simulator |

`QRAND`'s bytes MUST be uniformly distributed to the backend's precision. Only `W0 = 1` claims quantum
randomness.

### 6.5 Asynchronous execution

A request holds one instruction and its input registers:

```c
typedef struct { uint32_t opcode, state; uint64_t x[6]; double d[2]; uint64_t ticket; } qarm_request;  /* 80 */
```

| opcode | name | in | out |
|---|---|---|---|
| 0x12 | `QSUBMIT` | `X1` = `qarm_request *` | `W0 = 1`: taken (`state` 0 running, or already 1); `W0 = 2`: refused |
| 0x13 | `QPOLL` | `X1` = the request | `W0 = 1`, `W1` = `state` (1: done); never waits |
| 0x14 | `QWAIT` | `X1` = the request | `W0 = 1` once `state` is 1 |

- The runtime MUST read all of the instruction's inputs, registers and memory, at `QSUBMIT`.
- When `state` becomes 1:
  - `x[0]` holds the instruction's `W0`;
  - `x[1..5]`, `d[0..1]` hold its output registers, and every other register field is unchanged;
  - its memory outputs are written.
- Until then the program MUST NOT read the outputs or modify the request.
- `ticket` is the runtime's. `QSUBMIT` of `QSUBMIT`, `QPOLL` or `QWAIT` MUST be refused.
- A runtime MAY complete any request at `QSUBMIT`. It SHOULD run QPU jobs (`QEXPECTN` at least)
  asynchronously, so that the program keeps computing while the job is queued.

### 6.6 Time evolution (1.1)

| opcode | name | in | out |
|---|---|---|---|
| 0x27 | `QEVOLVE` | `X1` = `qarm_evolution *`, `X2` = k doubles out | `W0 = 1`: the k values; `D0` = the error bound ε, `D1` = the standard error σ; `W3` = method \| order << 8, `X4` = steps. `W0 = 2`: refused |

**The guarantee.** For every i:
- `EXACT` and `PRODUCT`: |vᵢ − v(tᵢ)| ≤ ε, apart from rounding and, on a backend with `MEASURED`, shot noise;
- `QDRIFT`: |E[vᵢ] − v(tᵢ)| ≤ ε, where E is the mean over the random sequences.

σ is the largest standard error of the k values: from shots on a `MEASURED` backend, and from the random
sequences for `QDRIFT`. It is 0 otherwise. A device's own errors are not included.

**The bound MUST be rigorous.** ε is a proven upper bound for the method, the steps and the times actually
used. A runtime MAY use any proven bound, such as a norm bound or a commutator bound. The reference
implementation's bounds, for λ = Σ|cⱼ| and ω = Σ|oⱼ| over the non-identity terms of H and O, are as follows:
- **a product formula of order p with r steps:** a state error δ ≤ 2r·Σ_{n>p} (Υ_p λ|t|/r)ⁿ/n!, where Υ_p is
  the sum of the stages' absolute coefficients. For p = 1 and 2, δ is also at most the commutator bounds of
  Childs, Su, Tran, Wiebe and Zhu (2021). Then ε = ω(2 + δ)δ;
- **qDRIFT with N samples:** twice Campbell's bound on the diamond distance of the channel, 2λ²t²/N·e^{2λ|t|/N},
  times ω;
- **exact:** the truncation of a Taylor series, about 10⁻³⁰ or less.

**eps and steps.**
- With `steps = 0`, a runtime MUST return ε ≤ `eps`, or refuse with `W0 = 2`. It MUST NOT return a less
  accurate answer.
- With `steps ≠ 0`, the runtime MUST use exactly those steps and report the bound that they give, however
  large. `X4` reports the steps used:
  - for `PRODUCT`, r;
  - for `QDRIFT`, N;
  - for `EXACT`, the runtime's own segments, which carry no meaning for the program.

**Methods.**
- `AUTO` lets the runtime choose. A simulator SHOULD evolve exactly. A QPU backend SHOULD choose the product
  formula with the fewest rotations that meets `eps`.
- A runtime MAY refuse a method that its backend cannot run, with `W0 = 2`. For example, a QPU backend refuses
  `EXACT`.
- `W3` reports the method used (1, 2 or 3) and, for `PRODUCT`, the order.
- **qDRIFT on a QPU (1.2).** A measured backend MAY run `QDRIFT` as R random sequences, where R is `shots` and 0
  means the runtime's default. Each sequence is one circuit with the step t/N as its parameter, so all k times are
  that circuit's batch. vᵢ is the mean over the sequences: its expectation is the qDRIFT channel's ⟨O⟩, within ε
  of v(tᵢ). σ is the sequences' spread over √R, shot noise included.

**Other rules.**
- The runtime MUST read every input before it writes any output, so that `X2` may equal `t`.
- On a QPU, all k times SHOULD be one job: the product formula is one circuit with the step dt = t/r as its
  parameter.
- `QEVOLVE` may be submitted with `QSUBMIT` (6.5).

There is no check: a classical path computes the same values in exponential time, so the library returns
the values with ε and σ.

### 6.7 Open-system evolution (1.2)

| opcode | name | in | out |
|---|---|---|---|
| 0x28 | `QLINDBLAD` | `X1` = `qarm_lindblad *`, `X2` = k doubles out | as `QEVOLVE` (6.6): the k values, `D0` = ε, `D1` = σ, `W3` = method \| order << 8, `X4` = steps; `W0 = 2`: refused |

**The guarantee** is `QEVOLVE`'s. For every i, |vᵢ − v(tᵢ)| ≤ ε when the runtime evolves the density matrix,
and |E[vᵢ] − v(tᵢ)| ≤ ε when it samples trajectories. σ is the standard error of the trajectories, and 0
otherwise. The rules on `eps`, `steps`, reading before writing, and `QSUBMIT` are 6.6's. ε MUST be rigorous.

The reference bounds, in the diamond norm:
- **The generators' norms.** −i[cP, ·] has norm at most 2|c|, and γ(P · P − ·) at most 2γ, so the generator L
  has ‖L‖ ≤ Λ = 2λ + 2Σγₖ. Each factor e^{sLⱼ} with s ≥ 0 is a channel, and so a contraction.
- **Exact:** the Taylor series of e^{ΔL} on the density matrix, as in 6.6.
- **Lie–Trotter or Strang with r steps:** ‖e^{tL} − S_p(t/r)^r‖ ≤ 2r·Σ_{n>p} (Λt/r)ⁿ/n!. For p = 1, it is also
  at most t²/(2r) times the sum of 2‖A‖‖B‖ over the pairs of generators that do not commute.
- **From a state error to ε:** ε = ω·δ, because |Tr(O′Δρ)| ≤ ‖O′‖·‖Δρ‖₁.

**Trajectories.** A jump factor e^{xD[P]} = (1 − q)·id + q·P·P, with q = (1 − e^{−2x})/2. A runtime MAY sample
it: apply P with probability q in each trajectory. The mean over the trajectories is then unbiased for the
product formula's ⟨O⟩.

**Methods.** `AUTO` lets the runtime choose. A simulator SHOULD evolve exactly when it can hold the density
matrix. A runtime MAY refuse any method it cannot run; the reference runtimes run `QLINDBLAD` on their exact
simulators only.

There is no check, as for `QEVOLVE`.

## 7. Library obligations and reserved opcodes

### 7.1 Classical paths

For every instruction, the library provides a classical implementation with the same contract: an
exhaustive search, a state vector, a direct sum. When `W0 = 0`, or a check fails, the program computes
the answer classically. A conforming program therefore gives correct results on every AArch64 or x86-64
CPU, only slower.

### 7.2 Library functions on top of the instructions

Some algorithms need no new instruction:
- Time evolution on the 1.0 instructions (`qarm_evolve_n`: Trotter steps as an ansatz, many times t in one
  `QEXPECTN` batch, without an error bound). With 1.1, `QEVOLVE` does this in the runtime, with a guaranteed
  bound (`qarm_evolve`).
- QAOA (a cost Hamiltonian, and layers as Pauli rotations with shared parameters).
- Optimizers (L-BFGS on `QGRAD`, SPSA on `QEXPECTN`).

They are part of `include/qarm.h`, not of the instruction set.

A program also need not include the library. A binary rewriter can emit the packets for it, as
`tools/qarm_patch.py` does for brute-force search functions in finished ELF binaries. The rewriter then
takes on the library's obligations: the check and the classical path.

### 7.3 Reserved

| opcode | name | for |
|---|---|---|
| 0x29–0x2F, 0x32–0x3F | — | future variational and circuit instructions |

0x27 was reserved in 1.0 and is `QEVOLVE` in 1.1; 0x28 is `QLINDBLAD` in 1.2.
| 0x00–0x0F, 0x40–0xFF | — | reserved |

Vendors MAY use opcodes 0xF0–0xFF for experiments. They MUST NOT report those instructions in the
feature bits of 1.0.

## 8. Profiles

A runtime states the profiles it implements. Its feature bits must cover them.

| profile | instructions | feature bits |
|---|---|---|
| Core | `QAVAIL`, `QCAPS` | 1024 |
| Oracle | `QSEARCH`, `QMIN`, `QCOUNT`, `QAMPEST`, `QPERIOD`, `QSIMON`, `QBV`, `QCOLLISION` | 1 4 8 16 32 64 128 8192 |
| Variational | `QVLOAD` … `QVFREE`, `QVSAMPLE` | 2 2048 |
| Circuit | `QCIRC`, `QRAND` | 256 512 |
| Async | `QSUBMIT`, `QPOLL`, `QWAIT` | 4096 |
| Dynamics (1.1) | `QEVOLVE` | 16384 |
| Open (1.2) | `QLINDBLAD` | 32768 |

`qarm_exec.py` and the QEMU plugin with its oracle helper implement all seven. `qarm_exec.py` completes
every request at `QSUBMIT`. The QEMU plugin submits `QEXPECTN` to libqarm, which polls an IBM job while
the program runs. Without its oracle helper, the plugin implements Core, Variational, Circuit, Async, Dynamics and Open.

## 9. Conformance

`tests/conformance/qarm_conformance.c` is an AArch64 or x86-64 program that emits the raw packets. It
does not use the library's fallbacks, so it tests the runtime alone. Build it at `-O1` and run it under
the runtime:

```
$ aarch64-linux-gnu-gcc -O1 -iquote include tests/conformance/qarm_conformance.c -o conformance -lm
$ python3 qarm_exec.py -q conformance
Q-ARM ISA 1.2 conformance (docs/qarm_isa.md, section 9)
PASS QAVAIL
PASS unassigned opcode
...
profiles: Core passed; Oracle passed; Variational passed; Circuit passed; Async passed; Dynamics passed; Open passed
conformance: 17 passed, 0 failed, 0 skipped (features 0xffff)
```

Each profile of section 8 is reported in one of three ways:
- **passed:** all its tests passed;
- **not claimed:** its feature bits are absent, so its tests were skipped;
- **FAILED:** a test failed.

A runtime conforms to the profiles it reports as passed. For example, `runtime/minimal` reports Core and
Circuit.

For each test it checks:

- **the status protocol:** for example, an unassigned opcode MUST leave `W0 = 0`;
- **the refusal cases** of 5.3 and 5.4;
- **side effects:** registers `X5` and `X9`–`X15` (on x86-64, `R8` and `RBX`, `R9`–`R14`) are unchanged after
  every packet, and guard bytes around every output buffer are intact;
- **the answers:** exact ones where defined, otherwise within the stated bounds, with up to three tries
  for probabilistic instructions;
- **the feature bits:** QAVAIL and QCAPS agree, and an instruction that is not claimed is skipped.

The exit status is the number of failures. A plain CPU reports "no runtime". The statistical checks
assume an ideal backend: on a noisy QPU, run it to see the device's error rates, not to judge
conformance.

## 10. Versions

This is version 1.2, packet format 2, `QCAPS.version` 1. An older runtime leaves `QEVOLVE` (1.1) and `QLINDBLAD`
(1.2) unexecuted (`W0 = 0`), so a newer program runs on it through the library's classical path.

- **Minor versions** (1.1, …) may add instructions on reserved opcodes, feature bits and operand fields
  in reserved words. A program discovers additions through the feature bits. Programs and runtimes of
  different minor versions work together, because unknown opcodes are not executed.
- **Major versions** change the packet format byte.

`include/qarm_isa.h` gives the version as `QARM_ISA_VERSION_MAJOR`, `QARM_ISA_VERSION_MINOR` and
`QARM_ISA_PACKET_FORMAT`.

## Revision history

| version | date | changes |
|---|---|---|
| 1.2 | 2026-11-28 | `QLINDBLAD` (0x28, formerly reserved): open-system evolution under the Lindblad equation with Pauli jump operators, with a guaranteed error bound: exact on the density matrix, Lie–Trotter or Strang, on the density matrix or by trajectories; the `qarm_lindblad` table (5.8); feature bit 32768; the Open profile; the conformance program's `QLINDBLAD` test. `QEVOLVE`'s qDRIFT on measured backends, as R random circuits (6.6). 24 instructions in seven profiles. |
| 1.1 | 2026-11-28 | `QEVOLVE` (0x27, formerly reserved): time evolution with a guaranteed error bound, by exact evolution, product formulas of order 1, 2, 4 and 6, or qDRIFT; the `qarm_evolution` table (5.7); feature bit 16384; the Dynamics profile; the conformance program's `QEVOLVE` test. 23 instructions in six profiles. Clarified: what each check establishes (6.2), and that `W0 = 2` from an oracle instruction means none found, not none exists (QSIMON's said "f is one-to-one"). |
| 1.0 | 2026-11-28 | First publication. 22 instructions in five profiles; packets for AArch64 and x86-64; `qarm_isa.h`; the conformance program with per-profile results. |
