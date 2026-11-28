# Writing a Q-ARM runtime

A Q-ARM runtime is anything that executes Q-ARM packets while a program runs: an emulator, a binary
translator, a hypervisor, an OS service, or a CPU with a quantum coprocessor. This guide builds one step by
step, from nothing to a runtime that passes the conformance program. Each step corresponds to a section of
[`runtime/minimal/qarm_minimal.c`](../runtime/minimal/qarm_minimal.c), a QEMU plugin of about 200 lines that
implements the Core and Circuit profiles.

The rules are in [`docs/qarm_isa.md`](qarm_isa.md), the specification (version 1.1). The constants and
layouts are in [`include/qarm_isa.h`](../include/qarm_isa.h). Include it and never retype a number.

## 0. What you need

- **A way to stop the program at an instruction and to read and write its registers and memory.** QEMU's
  TCG plugins (QEMU 10.1 or newer) give exactly that, for AArch64 and x86-64 Linux programs. Unicorn,
  ptrace, a JIT or a hypervisor work the same way.
- **A backend** for the instructions you will implement. For the Circuit profile, a state vector is
  enough, and `include/qarm_sv.h` is one.

## 1. Recognize the packet (specification, section 2)

A packet is 32 bytes inside the code:

| | word 0 | word 1 | word 2 | words 3–7 |
|---|---|---|---|---|
| AArch64 | `0x14000008` (`B .+32`) | `0x4d524151` ("QARM") | `2 << 24 \| opcode << 16` | 0 |
| x86-64 | bytes `eb 1e 00 00` (`JMP .+32`) | `0x4d524151` | the same | 0 |

Hook every instruction that equals word 0. When it executes, read the next two words:
- the magic must match;
- the packet format (bits 31..24 of word 2) must be 2;
- bits 15..0 must be 0.

Anything else is an ordinary jump: do nothing. If the packet is one, do the instruction's work before the
jump runs. The jump then skips the packet, exactly as on a plain CPU.

In the QEMU plugin this is `tb_trans` (register a callback on each such instruction) and `packet` (check
the words, then dispatch on the opcode).

## 2. The registers (section 3, 2.1)

The ISA's operands are `X0`–`X5`, `D0` and `D1`.

| ISA | AArch64 | x86-64 |
|---|---|---|
| X0..X5 | x0..x5 | RAX, RDI, RSI, RDX, RCX, R8 |
| D0, D1 | the low 64 bits of v0, v1 (z0, z1 with SVE) | the low 64 bits of XMM0, XMM1 |

`W0` is the status:

| W0 | meaning |
|---|---|
| 0 | not executed |
| 1 | done |
| 2 | the defined negative result or a refusal |
| 3 | QRAND from a simulator |

The library sets `X0 = 0` before the packet, so an instruction you do not implement needs no code at all.
Leave the registers alone and the program takes its classical path. This is what makes a small runtime
legal.

**Change nothing else.** No register outside the instruction's outputs, and no memory outside its output
buffers. The conformance program puts sentinels in other registers and guard bytes around every buffer,
and checks them.

## 3. Discovery: the Core profile (sections 4, 6.1)

Two instructions are needed:
- **`QAVAIL`** (0x10): `W0 = 1`, and `W1` = the feature bits of what you implement. Report exactly
  those. A bit you set is a promise.
- **`QCAPS`** (0x31): write a 64-byte `qarm_capabilities` at `X1`, and set `W0 = 1`:
  - `version` = 1;
  - `features` = the same bits as `QAVAIL`'s;
  - `qubits`, flags (hardware, measured, async), and a name for display.

## 4. An instruction: the Circuit profile (sections 5.4, 6.4)

**`QCIRC`** (0x30): `X1` points to a `qarm_circuit` (qubits, gates, a pointer to 16-byte gates), `W2` is the
number of shots, and `X3` points to space for that many `uint64_t` outcomes.
1. Read the header and the gate table from guest memory.
2. **Refuse** with `W0 = 2` when there is an unknown gate kind, a qubit outside the register, or a gate
   that uses one qubit twice. The conformance program tries those cases.
3. Run the circuit from |0…0⟩, measure every qubit, and write the outcomes (bit q = qubit q). Set `W0 = 1`.

**`QRAND`** (0x1C): fill `W2` bytes at `X1`. Set `W0 = 1` only if the bytes were measured on quantum hardware.
A simulator answers `W0 = 3`.

`qarm_sv.h` does the work: `qarm_sv_gate_ok` checks a gate, `qarm_sv_circuit` runs the table, and
sampling is a binary search on the cumulative probabilities.

## 5. Run the conformance program (section 9)

```
aarch64-linux-gnu-gcc -O1 -iquote include tests/conformance/qarm_conformance.c -o conformance -lm
qemu-aarch64 -L /usr/aarch64-linux-gnu -plugin build/minimal/libqarm-minimal.so ./conformance
```

On `runtime/minimal` the result is:

```
profiles: Core passed; Oracle not claimed; Variational not claimed; Circuit passed; Async not claimed; Dynamics not claimed
conformance: 5 passed, 0 failed, 11 skipped (features 0x700)
```

So that runtime conforms to Core and Circuit. Any program built with `include/qarm.h` runs on it:
- its circuits are sampled by the runtime;
- every other instruction falls back to the program's classical path.

`examples/qarm/ghz.c` reports "on the coprocessor" under it.

## 6. The other profiles

- **Variational** (section 6.3). `QVLOAD` copies the Hamiltonian and ansatz tables and returns a handle.
  Refuse the cases of section 5.3. `QEXPECT`, `QEXPECTN` and `QGRAD` are energies and an adjoint gradient,
  and `qarm_sv.h` has them: `qarm_sv_prepare`, `qarm_sv_energy`, `qarm_sv_grad`. On a QPU, send each batch
  as one job.
- **Oracle** (sections 5.5, 6.2). The operand is the address of a C function in the program. Build the
  oracle from its machine code:
  - `qoracle` (github.com/quantag/qoracle) does that for AArch64 and x86-64, and verifies it on every
    input. `tools/qarm_oracle.py` is the command line that `runtime/qemu` calls.
  - If you cannot build the oracle exactly, **decline** (`W0 = 0`). Never use an approximate one.
  - Every answer must satisfy the instruction's guarantee. The library checks the cheap ones by calling
    the function itself.
- **Async** (section 6.5). Copy the request's inputs at `QSUBMIT`. Complete it then, or later from a job,
  writing the outputs into the request. Report `state` with `QPOLL`.
- **Dynamics** (sections 5.7, 6.6). `QEVOLVE` needs a plan with a proven error bound. `qarm_sv.h` has the
  reference planner and its bounds:
  - `qarm_ev_check` refuses the cases of 5.7;
  - `qarm_ev_plan` picks the method and steps, and the bound they guarantee;
  - `qarm_sv_evolve` runs the plan on a state vector;
  - `qarm_ev_steps_table` writes a product formula as an ansatz table, for a QPU.

  With `steps = 0`, meet `eps` or refuse. Never report a bound that you have not proven.

The full implementations to read are:
- `runtime/qemu/qarm_qemu.c`, every profile, with libqarm as the backend;
- `qarm_exec.py`, every profile, in Python on Unicorn.

## 7. Claiming conformance

Run the conformance program on your runtime, on every architecture it supports. Then state the profiles
it reports as passed and the specification's version, for example "Q-ARM ISA 1.1: Core, Circuit". The
statistical tests assume an ideal backend. On a noisy QPU they show the device's errors; judge
conformance on its simulator.
