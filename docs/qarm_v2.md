# Q-ARM v2: quantum instructions with a classical fallback, in one ARM binary

> The instruction set is specified, vendor-neutrally, in [`docs/qarm_isa.md`](qarm_isa.md) (version 1.0),
> with a conformance program in `tests/conformance`. This document is the reference implementation's.

Status: *proof of concept*. Two instructions, a runtime with the exact simulator as the quantum
coprocessor, tested on Linux (arm64, and x86 with qemu) and macOS on Apple Silicon. Not an official Arm
extension, and no claim of a speedup today (section 11).

## 1. The idea

Processors gain new instructions with a fallback: a program asks whether the feature is there and
otherwise takes a classical path. One binary runs on old and new machines. Q-ARM v2 applies this to a
quantum coprocessor:

- the ordinary instructions of an arm64 program run on the CPU, at full speed;
- a few **algorithm-level quantum instructions**, like `QSEARCH` (Grover's search), go to a quantum
  coprocessor;
- the classical function inside such an instruction, the oracle, is taken from **the program's own
  machine code**: no source code, no second program;
- on a plain ARM CPU the quantum instructions are skipped, and the program takes its classical path.

```c
#include "qarm.h"

static int consistent(int secret) { ... }        // the predicate: ordinary C

int secret;
if (qarm_search(consistent, 8, &secret))         // QSEARCH under Q-ARM; a loop on a plain CPU
    printf("the secret code: ...", ...);
```

```
$ clang -O1 -iquote include examples/qarm/codebreaker_search.c -o codebreaker_search
$ ./codebreaker_search                                    # a plain arm64 CPU
searching classically on this CPU
the secret code: BYRG
$ python3 qarm_exec.py codebreaker_search                 # the same file, Q-ARM runtime
searching with Grover's search on the quantum coprocessor (Q-ARM)
[qarm: QSEARCH consistent, 8 bits: Grover's search with a 1066-qubit, 4349-gate oracle from consistent's machine code: x = 78 after 31 oracle calls in 15 rounds, checked by running consistent]
the secret code: BYRG
```

## 2. Packets

A v2 instruction is a Q-ARM packet, as in v1 (`docs/qarm.md`): eight 32-bit words that begin with
`B .+32`, so that a plain CPU continues after them.

| word | contents |
|---|---|
| 0 | `0x14000008`: `B .+32` |
| 1 | `0x4d524151`: "QARM" |
| 2 | `[31:24]` version = 2, `[23:16]` opcode, `[15:0]` 0 |
| 3–7 | 0 (reserved) |

The operands are in fixed registers, so the packet itself has none:

| opcode | name | in | out |
|---|---|---|---|
| `0x10` | `QAVAIL` | — | `W0 = 1`, `W1` = features: 1 `QSEARCH`, 2 VQE, 4 `QMIN`, 8 `QCOUNT`, 16 `QPERIOD`, 32 `QSIMON`, 64 `QBV`, 128 `QAMPEST`, 256 `QCIRC`, 512 `QRAND`, 1024 `QCAPS`, 2048 `QVSAMPLE`, 4096 `QSUBMIT`/`QPOLL`/`QWAIT` (`qarm_features()`) |
| `0x11` | `QSEARCH` | `X1` = address of `f`, `W2` = bits (1 to 16) | `W0 = 1` and `W3 = x` with `f(x) = 1`; or `W0 = 2`: none |
| `0x15` | `QMIN` | `X1` = address of `cost`, `W2` = bits | `W0 = 1`, `W3 = x` with the least `cost(x)`, `W4 = cost(x)` (signed) |
| `0x16` | `QCOUNT` | `X1` = address of `f`, `W2` = bits | `W0 = 1`, `X3` = the number of `x` with `f(x) = 1`, `D0` = its error bound |
| `0x17` | `QAMPEST` | `X1` = address of `g`, `W2` = bits, `W3` = value bits k | `W0 = 1`, `D0` = the mean of g(x) ∈ [0, 2ᵏ), `D1` = its error bound |
| `0x18` | `QPERIOD` | `X1` = address of `f`, `W2` = bits | `W0 = 1`, `W3` = the least r > 0 with f(x + r) = f(x); or `W0 = 2`: none |
| `0x19` | `QSIMON` | `X1` = address of `f`, `W2` = bits | `W0 = 1`, `W3` = s ≠ 0 with f(x) = f(x ⊕ s); or `W0 = 2`: none |
| `0x1A` | `QBV` | `X1` = address of `f`, `W2` = bits | `W0 = 1`, `W3` = s with f(x) = s·x mod 2 (+ a constant) |
| `0x12`–`0x14` | `QSUBMIT`, `QPOLL`, `QWAIT` | `X1` = `qarm_request` (an instruction and its registers) | the instruction, asynchronously: `qarm_expect_n_async` under QEMU leaves the IBM job running while the program works (docs/qarm_isa.md 6.5) |
| `0x1C` | `QRAND` | `X1` = buffer, `W2` = n bytes | `W0 = 1`: measurements on quantum hardware; `W0 = 3`: a simulator's (pseudo-random) |
| `0x30` | `QCIRC` | `X1` = `qarm_circuit`, `W2` = shots, `X3` = `uint64_t` samples | `W0 = 1`: samples written; `W0 = 2`: refused |
| `0x31` | `QCAPS` | `X1` = `qarm_capabilities` | `W0 = 1`: version, features, qubits, flags (1 hardware, 2 measured, 4 async), backend name |
| `0x20`–`0x26` | VQE | section 6 | |

**The instruction set, planned.** Each algorithm family is one instruction. Its input is an ordinary C
function or table. Each has a classical path in `qarm.h`, a check of the answer where one exists, and a
`QAVAIL` feature bit.

| opcode | name | input | result | check |
|---|---|---|---|---|
| `0x1B` | `QCOLLISION` | `f(x)` | x ≠ y with f(x) = f(y) (Brassard–Høyer–Tapp) | f(x) = f(y) |
| `0x27` | `QEVOLVE` | Hamiltonian table, t | ⟨O(t)⟩ natively in the runtime (qDRIFT, higher-order formulas); today a library function, section 6 | exact for small cases |

QAOA and quantum kernels are generators of tables for `QEXPECT` and `QCIRC` in the C library, not
instructions.

The C functions in `include/qarm.h` set `W0 = 0` before the packet. A plain CPU leaves it at 0, which
means "not executed", and the function takes the classical path. Version 1 packets (gates on a
separate quantum bank, `qarm.py`) and version 2 packets use the same frame; the runtime of each rejects
the other's.

## 3. `QSEARCH`

`f` is a function of the program: `int f(int x)`, returning 0 or 1. The runtime:

1. **builds the oracle from the machine code:** qoracle executes `f`'s instructions symbolically for an
   argument of `bits` bits, at the program's own addresses, and makes the phase oracle
   `|x⟩|0⟩ → (−1)^f(x) |x⟩|0⟩`. It is checked against `f`'s machine code on every input; that `f`
   returns only 0 or 1 is proven, or checked on every input;
2. **runs Grover's search** for an unknown number of solutions (Boyer, Brassard, Høyer, Tapp 1998):
   rounds of a random number of Grover iterations, each ending in a measurement;
3. **checks each measured answer by running `f` itself**, the same machine code, in the program. Only
   a checked answer is returned. After about 4.5·√N oracle calls without one: `W0 = 2`.

What `f` may contain is what qoracle translates: integer arithmetic, comparisons, branches, loops,
reading constant tables. It must not call other functions or write memory, so its helpers must be
inlined (`static inline __attribute__((always_inline))`), and the build must not use vector (NEON)
instructions: build with `-O1`. At `-O2` both GCC and clang vectorize such loops. The runtime names the
first instruction it cannot translate.

### `QMIN` and `QCOUNT`

Both build their oracle from the program's own functions, as `QSEARCH` does. Both have a classical path
in the same binary: every input in turn.

- **`QMIN`** (Dürr–Høyer 1996) takes `int cost(int x)` and returns an x with the least cost.
  - qoracle builds a **compute oracle** |x⟩|0⟩ → |x⟩|cost(x)⟩ from `cost`'s machine code. Its result
    register is 32 bits and signed; when the top bits are always 0, they are dropped and the value is
    unsigned.
  - From a random x, Grover searches (BBHT) look for an x cheaper than the best so far. This stops when
    a search fails, or after 22.5·√N + 1.4·log₂²N oracle calls.
  - `qarm.h` checks the answer's cost by calling `cost`. That it is the least cannot be checked cheaply;
    that is the search's job. The answer is right with high probability.
  - On a QPU each search's oracle is "cost(x) < t", for a threshold t: the compute oracle, a comparator
    with a constant, and uncomputation. The runtimes simulate it today.
- **`QCOUNT`** (Brassard–Høyer–Tapp 1998) takes a predicate and estimates its number of solutions M:
  - phase estimation of the Grover operator, whose eigenphases are ±2θ with sin²θ = M/N;
  - t = ⌈bits/2⌉ + 3 precision qubits, and 2ᵗ − 1 controlled Grover iterations per run;
  - one run's error bound, 2π√(M(N−M))/2ᵗ + π²N/4ᵗ, holds with probability at least 8/π² ≈ 0.81. The
    runtimes return the median of 5 runs, for which it holds with probability about 0.98.
  - The runtimes draw the outcome from its exact distribution.
- **`QAMPEST`** (amplitude estimation; Brassard, Høyer, Mosca, Tapp) takes `int g(int x)` with values in
  [0, 2ᵏ) and estimates their mean over all x, the quantum replacement for Monte Carlo sampling.
  - The mean a = E[g]/2ᵏ is the amplitude of a marked state, and phase estimation reads it with error
    about π/2ᵗ.
  - t = ⌈bits/2⌉ + 3, again the median of 5 runs. Monte Carlo needs about a(1−a)/ε² samples for the
    same error; the report gives both numbers.
  - `examples/qarm/pi.c` estimates π from the share of a 64 × 64 grid inside a quarter circle. Its result
    is 3.17 ± 0.05 from 2,555 queries, where Monte Carlo would need about 6,600 samples. The grid's
    exact value, which the plain CPU computes, is 3.1475.

`examples/qarm/partition.c` is number partitioning, the classic NP-hard QUBO, as ordinary C:

```
$ python3 qarm_exec.py partition
[qarm: QMIN imbalance, 8 bits: Durr-Hoyer with a 997-qubit, 4589-gate compute oracle (31 result bits) from imbalance's machine code: x = 195, imbalance(x) = 1, ...]
best split: {13 7 3 8} vs {9 4 11 6}: difference 1
[qarm: QCOUNT nearly_balanced, 8 bits: quantum counting with 7 precision qubits, 127 controlled Grover iterations, ...]
splits within 3 of balance: about 33 (+- 4.4; QCOUNT)
```

The plain CPU tries all 256 splits: difference 1, and 32 nearly balanced splits. Under QEMU the plugin
asks the helper for a compute oracle (`tools/qarm_oracle.py --kind compute`), and libqarm runs
Dürr–Høyer and the counting (`qarm_rt_min`, `qarm_rt_count`).

### `QPERIOD`, `QSIMON` and `QBV`

These find hidden structure in a function of the program. The oracles come from its machine code; each
answer is checked in the guest by `qarm.h`, and each has a classical path.

- **`QPERIOD`** finds the least r > 0 with f(x + r) = f(x): the core of Shor's factoring and of discrete
  logarithms.
  - Measuring f's register leaves the inputs with one value. Their quantum Fourier transform is measured.
  - The continued fractions of y/2ᵇⁱᵗˢ give candidate periods (the convergents' denominators, and small
    multiples of them). Each is checked against the oracle's values, then reduced to its least periodic
    divisor.
  - `qarm.h` checks f(i) = f(i + r) for i = 0..3.
- **`QSIMON`** finds s ≠ 0 with f(x) = f(x ⊕ s).
  - Walsh–Hadamard samples y satisfy y·s = 0. When n − 1 of them are independent over GF(2), s is in
    their null space.
  - The result is `W0 = 2` when f is one-to-one.
- **`QBV`** finds s with f(x) = s·x mod 2, with one query: H, the phase oracle, then H.

`examples/qarm/shor.c` factors 15 and 21. The period of x ↦ aˣ mod N is found from the oracle of an
ordinary C `modexp`: 6,750 qubits for 15, and 16,626 qubits for 21 (9 bits). The factors then follow
from gcd(a^(r/2) − 1, N). `examples/qarm/hidden.c` finds the shift 0x2d and the string 0x35:

```
$ python3 qarm_exec.py shor
[qarm: QPERIOD f21, 9 bits: the 16626-qubit, 59942-gate compute oracle from f21's machine code, 1 quantum query (simulated): period r = 6]
N = 21, a = 2: the period of a^x mod N is 6 -> 21 = 7 x 3
```

### `QCIRC`, `QRAND` and `QCAPS`

The algorithm instructions above take a C function. `QCIRC` is the escape hatch for everything else:
any circuit, given as data. It is still not OpenQASM. The circuit is a C table in the program's
memory, and the runtime reads it from there.

```c
typedef struct { uint8_t kind, q0, q1, q2; uint32_t reserved; double angle; } qarm_gate;   /* 16 bytes */
typedef struct { uint32_t qubits, gates; const qarm_gate *g; } qarm_circuit;

static const qarm_gate g[] = {{QARM_G_H, 0}, {QARM_G_CX, 0, 1}, {QARM_G_CX, 1, 2}};
static const qarm_circuit ghz = {3, 3, g};
uint64_t samples[1000];
qarm_run(&ghz, 1000, samples);        /* samples[s] bit q = qubit q, every qubit measured at the end */
```

- **Gates:** `X Y Z H S SDG T TDG SX RX RY RZ CX CZ SWAP CCX`, with `angle` for the rotations. For CX
  and CCX, `q0` (and `q1`) are the controls and the last qubit is the target.
- **Refused** (`W0 = 2`, `qarm_run` returns −1): an unknown kind, a qubit outside the register, or a
  multi-qubit gate that uses one qubit twice.
- **Return value of `qarm_run`:** 1 when the coprocessor ran it, 0 when the plain CPU simulated it.
  The plain CPU uses `qarm_sv.h`'s state vector, up to `QARM_VQE_MAX_QUBITS` qubits. `qarm_caps` says
  whether the coprocessor is hardware.
- **Where it runs:**
  - libqarm (and so the QEMU plugin) lowers each gate to X, H, RY, RZ, CX and CZ (`qarm_sv_lower`; CCX
    is the 15-gate Clifford+T circuit). It samples the state vector, or sends the circuit to IBM as one
    Sampler job with routing on a chain.
  - `qarm_exec.py` simulates with numpy, or with `--qpu` sends the circuit to the config's backend.

`QRAND` fills a buffer with bytes from 8 qubits in H, one shot per byte. `qarm_rand` returns 1 only
when the bytes are measurements on quantum hardware. A simulator's measurements are pseudo-random, and
the function says so by returning 0.

`QCAPS` describes the coprocessor: the ISA version, the feature bits, the qubits it simulates or has,
flags, and a backend name.

`examples/qarm/ghz.c` prepares a 5-qubit GHZ state and a Toffoli truth table, and draws 16 random
bytes. Each build runs it differently:

```
$ python3 qarm_exec.py -q ghz                          # the arm64 binary; the exact simulator
device: qarm_exec.py: exact simulator, 20 qubits, flags 0
GHZ(5): 00000 983, 11111 1017, other 0 of 2000 shots (on the coprocessor)
Toffoli: 00->0 100% 10->0 100% 01->0 100% 11->1 100%
$ python3 qarm_exec.py --backend preview -q ghz        # the noise model of Fez
GHZ(5): 00000 953, 11111 927, other 120 of 2000 shots (on the coprocessor)
Toffoli: 00->0 96% 10->0 98% 01->0 94% 11->1 94%
```

## 4. The runtime: `qarm_exec.py`

`python3 qarm_exec.py PROGRAM [ARGS]` runs an ordinary arm64 executable: macOS (Mach-O, as built on an
Apple Silicon Mac) or Linux (ELF, dynamically linked, PIE or not).

- **The program** is loaded at its own addresses; pointers are rebased (chained fixups, `R_AARCH64_RELATIVE`)
  and its imports bound. `main(argc, argv)` runs in Unicorn, a full 64-bit ARM emulator.
- **The C library** is served by the runtime: `printf` (the macOS and Linux variadic conventions, and
  glibc's `__printf_chk`), `puts`, `putchar`, `putc`, `fputs`, `fwrite`, `fflush`, `exit`, `strlen`,
  `memset`, `memcpy`, `stdout`, `stderr`, `__stack_chk_guard`. Any other import stops the program with its
  name (exit code 70).
- **Packets** are found by scanning the code: each v2 packet runs on the coprocessor and execution
  continues after it. A report of each quantum instruction goes to stderr (`-q`: none).
- **The coprocessor** is the runtime's exact simulator. Between oracle calls only the argument register
  is in superposition, so the state is 2^bits amplitudes; the oracle's phases come from running its
  gates on all basis inputs at once (bit planes). `QCPU_SEED=n` makes the measurements repeatable.

## 5. On a real QPU

`QSEARCH` can run on the backends of `qcpu_run.py`, configured in `config.json` (your own file, ignored
by git; see `config.example.json`):

```
$ clang -O1 -iquote include examples/qarm/tiny_search.c -o tiny_search
$ python3 qarm_exec.py tiny_search                     # the exact simulator
$ python3 qarm_exec.py --backend preview tiny_search   # IBM's noise model of Fez, no account
[qarm: QSEARCH is_three, 2 bits on preview: noise model of fake_fez (156 qubits): 9 qubits, 2 circuits (1024 shots each, up to 155 two-qubit gates), job 5597ba3d-..., 9.9 s: x = 3, measured 1029 times of 2048 (1. most frequent answer), checked by running is_three]
Q-ARM QSEARCH: x = 3
$ python3 qarm_exec.py --qpu tiny_search               # config.json's backend: "ibm" is a real device
```

`--qpu` uses `config.json` (`--config FILE`: another one); `--backend simulator|aer|preview|ibm` overrides
its `"backend"`. With none of these, the exact simulator is used, so no run spends QPU time unless asked.

**Synchronous and asynchronous.** The simulator answers at once, so `QSEARCH` there is adaptive: BBHT
picks each round's iteration count after the previous round's answer failed the check. A QPU is
asynchronous. Each job waits in a queue, from seconds to hours, and the free plan has 10 minutes of QPU time
per 28 days. Round-by-round adaptivity would queue once per round, so on a QPU `QSEARCH` is not adaptive.
It sends **one job** holding Grover's circuit for every iteration count `j = 0 .. floor(pi/4 sqrt(2^bits))`.
The job id is reported as soon as the job is sent, so you can follow it on the IBM Quantum dashboard. The
emulated CPU waits at the packet until the job is done, as a CPU waits on a slow device. Then the measured
answers of all the circuits are pooled and checked by running `f`, most frequent first.

**Noise.** It changes which answers are measured, never what `QSEARCH` returns. `W3` is always an answer
for which the program's own `f` returned 1. If noise hides every solution, the result is `W0 = 2` (none
found), the same as for a search with no solution. A program that must tell those two cases apart can call
it again or search classically.

**Size.** The oracle is built from the function's machine code, so its qubit count is the function's
register footprint: `is_three` takes 9 qubits, Code Breaker's `consistent()` takes 1066–1120. The backend
refuses an oracle larger than the device, with the numbers (`needs 1120 qubits, fake_fez has 156: not
run`, exit code 70), before anything is submitted. In practice the limit today is depth, not width:
`is_three` already has 155 two-qubit gates after transpiling to Fez, where one CZ would do.

**Async instructions.** A program that should keep working while the QPU runs needs the asynchrony in
the instruction set, rather than hidden in the runtime.
- `QSUBMIT` takes any instruction as a `qarm_request`: the opcode, and its input registers.
- `QPOLL` reports whether the request is done. `QWAIT` waits for it.
- When the request is done, it holds the output registers, and the memory outputs are written
  (`docs/qarm_isa.md`, section 6.5).

Under QEMU with libqarm on `ibm`, `QEXPECTN` stays on the QPU while the program runs. Every other
instruction, and every instruction under `qarm_exec.py`, completes at `QSUBMIT`. On a plain CPU `W0 = 0`,
and `qarm.h` computes the batch itself. This is how CUDA-Q's `cudaq::sample_async` and Qiskit's
`job.result()` work, but at the instruction level.

## 6. VQE

The variational quantum eigensolver finds the ground-state energy of a molecule. It fits Q-ARM's split
directly. The optimizer is ordinary C on the CPU. The coprocessor prepares a parameterized state
|ψ(θ)⟩ and measures ⟨ψ(θ)|H|ψ(θ)⟩. The quantum part is data, not code: a Hamiltonian and an ansatz,
held in two tables in the program's memory.

### Instructions

| opcode | name | in | out |
|---|---|---|---|
| `0x20` | `QVLOAD` | `X1` = Hamiltonian, `X2` = ansatz | `W0` = 1 and `X3` = handle, or 2 (refused) |
| `0x21` | `QEXPECT` | `X3` = handle, `X1` = θ (doubles), `W2` = shots (0: config.json's) | `W0` = 1, `D0` = E(θ), `D1` = standard error |
| `0x22` | `QEXPECTN` | `X3` = handle, `X1` = k parameter vectors, `W2` = k, `X4` = k doubles | `W0` = 1, the k energies at `X4` |
| `0x23` | `QGRAD` | `X3` = handle, `X1` = θ, `X4` = one double per parameter | `W0` = 1, `D0` = E(θ), the gradient at `X4` |
| `0x24` | `QSESSION` | `W1` = 1 open, 0 close | `W0` = 1 |
| `0x25` | `QVSAMPLE` | `X3` = handle, `X1` = θ, `W2` = shots, `X4` = shots `uint64_t` | `W0` = 1: the outcomes of \|ψ(θ)⟩ in the Z basis at `X4` (bit q = qubit q); 2: refused |
| `0x26` | `QVFREE` | `X3` = handle | `W0` = 1 |

On a plain CPU every packet leaves `W0` = 0, and `include/qarm.h` takes the classical path. That path is
`include/qarm_sv.h`, an exact state vector in C, up to `QARM_VQE_MAX_QUBITS` qubits (default 12; link
with `-lm`). libqarm's exact backend uses the same code.

**`QVSAMPLE`** measures the prepared state itself, not an energy. It gives the answer of a QAOA or VQE run
as bitstrings. On a measured backend the ansatz circuit is transpiled once, measured in the Z basis, and
sent as one job.

`examples/qarm/maxcut_qaoa.c` is QAOA for MaxCut on the Wagner graph (8 vertices, 12 edges, largest
cut 10).
- The cost and the QAOA layers are ordinary `qarm_hamiltonian` and `qarm_ansatz` tables: ZZ terms, and
  Pauli rotations that share γ and β.
- L-BFGS optimizes ⟨C⟩ with `QGRAD`. `QVSAMPLE` then draws 1000 cuts, and the program keeps the best
  one, counting its edges in C:

```
$ python3 qarm_exec.py -q maxcut_qaoa
QAOA, 3 layers, on the quantum coprocessor: <C> = -7.6515, the expected cut 7.651
QVSAMPLE (the coprocessor's measurements): best cut 10 (11010010), in 38.1% of 1000 shots; the largest cut is 10
```

**`QGRAD`** returns the energy and its gradient, computed two ways:
- **Exact backends:** adjoint differentiation, one forward and one backward pass over the state vector,
  whatever the number of parameters. Used by the plain CPU path, libqarm and `qarm_exec.py`.
- **Measured backends:** the parameter-shift rule.
  - Each rotation is shifted on its own: by ±π/4 for a Pauli rotation, where dE/da = E(a+π/4) − E(a−π/4);
    by ±π/2 for RY and RZ, where dE/da = (E(a+π/2) − E(a−π/2))/2.
  - All the shifted energies go out in **one job**: 2R+1 energies for R rotations, so one wait in the
    queue per gradient.
  - The gradient is then summed per parameter, weighted by each rotation's `scale`.

### Time evolution: `qarm_evolve_n`

Hamiltonian simulation needs no new instruction. `qarm_trotter` writes exp(−iHt) as an ansatz table of
second-order (symmetric) Trotter steps. Each Pauli term of H becomes a `QARM_PAULI_ROT`, and adjacent
equal terms are merged.

The rotation angles are scale × θ₀ with θ₀ = t / steps. So one table serves every time t, and k times are
k parameter vectors of one `QEXPECTN` batch: one job on a QPU.

```c
/* <O(t)> for k times from the basis state init; ops: room for about 2 * steps * terms operations */
int qarm_evolve_n(const qarm_hamiltonian *h, const qarm_hamiltonian *o, uint64_t init, const double *t, int k,
                  int steps, qarm_op *ops, uint32_t cap, double *out);
```

`examples/qarm/spin_chain.c` quenches the transverse-field Ising chain at its critical point: 8 spins,
H = −Σ ZᵢZᵢ₊₁ − Σ Xᵢ, all spins up at t = 0. It prints the magnetization for 13 times, with 4 Trotter steps
and with 32.

| backend | M(0) | M(1.0) | the error at 32 steps |
|---|---|---|---|
| plain CPU, `qarm_exec.py`, libqarm, QEMU (exact) | 1.0000 | 0.2388 | ≤ 3.4·10⁻⁴ from exp(−iHt) |
| noise model of Fez, 4 steps (53 two-qubit rotations) | 0.8177 | 0.1784 | — |
| noise model of Fez, 32 steps (834 two-qubit gates) | 0.2647 | 0.0437 | — |

The last row is the hardware's limit. At t = 0 the angles are zero, but the transpiled circuit is still
834 CZ deep, and decoherence takes most of the signal. Fewer, longer steps are the better trade on
today's devices.

### Tables (include/qarm.h)

```c
typedef struct { uint64_t x, z; double c; } qarm_pauli;   // c · P, P on qubit q: I X Z Y for (x_q, z_q) = 00 10 01 11
typedef struct { uint32_t qubits, terms; double constant; const qarm_pauli *t; } qarm_hamiltonian;
typedef struct { uint8_t kind, q0, q1, param; uint32_t reserved; double scale; uint64_t x, z; } qarm_op;
typedef struct { uint32_t qubits, params, ops, reserved; uint64_t init; const qarm_op *op; } qarm_ansatz;
```

The ansatz starts from the basis state `init` (Hartree–Fock, say) and applies `QARM_X`, `QARM_H`,
`QARM_CX`, `QARM_CZ`, `QARM_RY` and `QARM_RZ` (angle = `scale · θ[param]`). It can also apply
`QARM_PAULI_ROT`, exp(−i · scale · θ[param] · P(x, z)), which is how UCC excitations are written. Up to 64
qubits and 255 parameters.

### The C API

```c
qarm_vqe v;
qarm_vqe_load(&h, &ansatz, &v);          // 1: coprocessor, 0: classical, -1: refused
qarm_session(1);                          // reserve a QPU for the loop (no-op elsewhere)
qarm_expect_n(&v, thetas, k, energies);   // a batch: one job
double e = qarm_expect(&v, theta, &err);  // one energy and its standard error
e = qarm_grad(&v, theta, gradient);       // the energy and its gradient (QGRAD)
qarm_session(0);
qarm_vqe_free(&v);
```

### The demo: `examples/qarm/h2_vqe.c`

H₂ at 0.735 Å (STO-3G) uses the parity mapping, reduced to 2 qubits with 5 Pauli terms; the nuclear
repulsion goes in the constant. The ansatz has one parameter and one CNOT. The optimizer does four rounds
of a 17-point grid, each round finer around the lowest point. Each round is one `QEXPECTN`, so on a QPU the
whole optimization is 5 jobs: 4 batches and a final `QEXPECT`. The final energy is measured again, because
the lowest of 17 noisy values is biased low.

```
$ clang -O1 -iquote include examples/qarm/h2_vqe.c -o h2_vqe -lm
$ ./h2_vqe                                    # a plain CPU
E(-2.918398) = -1.137306 Ha; exact ground state -1.137306 Ha, difference 0.0 mHa
$ python3 qarm_exec.py h2_vqe                 # the same, from the runtime's exact simulator
$ python3 qarm_exec.py --backend preview h2_vqe
[qarm: QVLOAD: 2 qubits, 5 Pauli terms, an ansatz of 3 operations (1 two-qubit gates, 0 multi-qubit Pauli rotations) with 1 parameter; on preview: noise model of fake_fez (156 qubits): 2 measurement bases, readout correction, up to 1 two-qubit gates after transpiling]
...
$ python3 qarm_exec.py --qpu h2_vqe           # config.json: a real IBM device
```

### How energies are measured

- **Exact simulator** (the default): a state vector, synchronous, standard error 0.
- **Backends from config.json** (`simulator`, `aer`, `preview`, `ibm`):
  - At `QVLOAD` the terms are grouped greedily into qubit-wise commuting measurement bases. H₂ needs
    two: Z for `IZ`, `ZI` and `ZZ`, and X for `XX`. The circuits are transpiled once.
  - Each `QEXPECTN` is **one Sampler job**: one circuit per basis, each with all k parameter vectors.
  - On `preview` and `ibm` the job also measures all-0 and all-1 on the same physical qubits. Each
    distribution is then corrected with the per-qubit readout matrices: **readout correction** in the
    same job, at no extra trip through the queue. Turn it off with `"options": {"readout_mitigation": false}`.
  - The Sampler is used rather than the Estimator because it runs on every backend, the Open Plan
    included. The local Estimator of the noise models simulates every qubit of the device. IBM's own
    error mitigation (resilience levels, ZNE) is not used yet.
- **`QSESSION`** opens an IBM Session on `ibm`, so the optimizer's jobs skip the queue after the first.
  The Open Plan has no sessions; then each job queues on its own, and the report says so.

Measured on H₂ (`tests/test_qarm_vqe.py`):

| where | energy | from the exact value |
|---|---|---|
| plain CPU, exact simulator | −1.137306 Ha | 0 |
| noise model of Fez, 1024–4000 shots | about −1.10 to −1.13 Ha | 10–35 mHa |
| noise model of Torino, without / with readout correction | −0.967 / −1.127 Ha | 170 / 11 mHa |

Chemical accuracy is 1.6 mHa. Reaching it on hardware needs more shots and the mitigation above.

### Molecules: `tools/qarm_molecule.py` and `examples/qarm/molecule_vqe.c`

A molecule's tables come from a build-time tool. The program needs no Python:

```
$ python3 tools/qarm_molecule.py --name lih --atom "Li 0 0 0; H 0 0 1.5949" --basis sto-3g \
      --active 2,5 -o examples/qarm/lih.h
lih: 10 qubits, 275 terms, 24 parameters; E(HF) = -7.862027, E(CASCI) = -7.882176 Ha
$ clang -O1 -iquote include -iquote examples/qarm examples/qarm/molecule_vqe.c -o molecule_vqe -lm
$ ./molecule_vqe
...
VQE:           -7.882175991 Ha
exact (CASCI): -7.882175991 Ha
VQE - exact:   0.000000 mHa (chemical accuracy: 1.6 mHa)
correlation energy recovered: 100.00%
```

**The generator, step by step:**
- **Integrals.** PySCF computes them: RHF, then the active space, with `--active ELECTRONS,ORBITALS`
  and the other orbitals frozen.
- **Qubits.** The electronic Hamiltonian is mapped with Jordan–Wigner, on the same `(x, z)` bit masks
  as `qarm.h`, with alpha spin orbitals first, then beta.
- **Ansatz.** UCCSD from the Hartree–Fock state. Each excitation's T − T† is a sum of commuting Pauli
  strings, so its exponential is exactly a product of `QARM_PAULI_ROT`s, one parameter per excitation.
- **Self-checks before writing.** The qubit Hamiltonian's lowest energy, among states with the molecule's
  electron counts, must equal CASCI's to 1e-8. The Hartree–Fock state's energy must equal RHF's. The
  Pauli strings of each excitation must commute.
- **Output.** The header holds `molecule_h`, `molecule_ansatz`, `QARM_MOLECULE_E_HF` and
  `QARM_MOLECULE_E_REF`. `examples/qarm/h2.h` and `examples/qarm/lih.h` are committed.

**The optimizer.** `include/qarm_opt.h`, `qarm_minimize()`: L-BFGS on `qarm_expect` and `qarm_grad`, with
an Armijo line search. It is ordinary C with static arrays, so it also runs under `qarm_exec.py`.

| LiH at 1.5949 Å, STO-3G, (2e, 5o): 10 qubits, 24 parameters | time | VQE − CASCI |
|---|---|---|
| plain CPU (`qarm_sv.h`) | 0.05 s | 0.000000 mHa |
| libqarm, native | 0.05 s | the same output |
| QEMU with the plugin | 0.2 s | the same output |
| `qarm_exec.py` (QGRAD by adjoint differentiation in numpy) | 0.8 s | the same output |

L-BFGS converges in 9 iterations from the Hartree–Fock energy, −7.862027 Ha, recovering all of the
correlation energy in the active space. With two active electrons UCCSD is exact, so CASCI is
reachable. More electrons, as in N₂, would leave UCCSD's own small error.

**Measured energies: SPSA.** L-BFGS needs exact energies, because shot noise breaks its line search.
For measured energies `qarm_opt.h` has `qarm_spsa()`.
- **Each iteration** estimates the gradient from E(θ + c·d) − E(θ − c·d) along random directions d of ±1.
  All of an iteration's evaluations go in one `qarm_expect_n` batch, so on a QPU one iteration is one job.
- **Step size:** calibrated from a first batch, so the first step moves each parameter about 0.02.
- **Result:** the mean of the last 25 iterates, measured once more.
- **Defaults** (`QARM_SPSA_DEFAULTS`): 150 iterations, 2 directions each, probe c = 0.05. A sweep on LiH
  showed that UCCSD amplitudes need small steps: a first step of 0.05 lands 26–55 mHa away.
- **Seeded:** the random directions come from a seed, so a run is repeatable.

`molecule_vqe` picks the optimizer itself: SPSA when the first energy comes with a standard error (a
measured backend), L-BFGS otherwise. `molecule_vqe spsa N` or `lbfgs` overrides that.

| SPSA, 150 iterations | H₂ | LiH (24 parameters) |
|---|---|---|
| exact energies (plain CPU, `qarm_exec.py`) | | +0.074 mHa |
| libqarm's simulator, 4096 shots | −0.62 mHa, 0.3 s | +1.55 mHa, 7 s |
| libqarm's simulator, 65,536 shots | | −0.44 mHa, 97 s |

All are within chemical accuracy (1.6 mHa). The reported energy is itself one measurement, which is how
it can fall slightly below the exact value.

**Device noise is another matter.** On the noise model of IBM Fez, H₂'s UCCSD (4 qubits, Jordan–Wigner)
ended 315 mHa from exact after 30 SPSA iterations. The optimizer is not the problem: after Jordan–Wigner
and routing, UCCSD's Pauli rotations take dozens of CNOTs, and two-qubit errors dominate. On today's
hardware, VQE needs shallow ansätze, such as `h2_vqe`'s single-CNOT circuit (10–35 mHa on the same noise
model). The UCCSD path is for exact and shot-noise runs, and for error-corrected machines later.

`QGRAD` works on measured backends too: on H₂ (12 rotations, 25 energies in one batch), the
parameter-shift gradient from libqarm's simulator and from Aer matched the exact gradient to within 0.002
at 200,000 shots.

### Checks

Noise can push an energy below the true ground state, so a measured energy cannot be checked by
re-running something, the way `QSEARCH` checks its answer. The checks are instead:
- three independent implementations of the same tables must agree: the C state vector, the runtime's
  numpy state vector, and the Qiskit circuits on Aer;
- on noisy backends, the result must fall within a tolerance of the exact value.

The exact value is available on the plain-CPU path of the same binary, for molecules small enough.

## 7. The native runtime: libqarm

`qarm_exec.py` emulates the ARM CPU, so a program under it runs at the speed of Unicorn plus Python.
**libqarm** (`runtime/libqarm`, C) is the other way to run the same source. The program runs natively,
at full speed, on any CPU: Apple Silicon, x86-64, ARM Linux. Its VQE calls go to a C runtime that talks
to the QPU itself, with no Python at run time.

```
$ cc -O2 -DQARM_RUNTIME -iquote include examples/qarm/h2_vqe.c runtime/libqarm/*.c -o h2_vqe -lcurl -lm
$ ./h2_vqe                                              # exact, native
$ QARM_CONFIG=config.json ./h2_vqe                      # config.json's backend: a real IBM device
$ QARM_CONFIG=config.json QARM_BACKEND=simulator ./h2_vqe
```

With CMake: `cmake -S runtime/libqarm -B build/libqarm && cmake --build build/libqarm`, then link
`qarm` (it defines `QARM_RUNTIME`). Without libcurl, add `-DQARM_NO_CURL`; the `ibm` backend is then
absent. That is the default on Windows, where libcurl is not part of the system.

**Three builds of one source** (`include/qarm.h`):

| build | VQE calls | where |
|---|---|---|
| plain, any compiler | the classical path: an exact state vector in the header | any CPU |
| arm64, GCC or clang | Q-ARM packets: classical on a CPU, quantum under `qarm_exec.py` | ARM, or emulated |
| `-DQARM_RUNTIME` + libqarm | libqarm: exact, simulator or ibm | any CPU, native |

**Backends.** libqarm reads a config file only when `QARM_CONFIG` names one, in `config.json`'s format,
so no run spends QPU time unless asked. `QARM_BACKEND` overrides the file's `"backend"`.
- `exact`, the default: a state vector, synchronous, standard error 0.
- `simulator`: the same state vector, with shots sampled in each measurement basis. `QARM_SEED` makes
  a run repeatable.
- `ibm`: IBM Quantum's REST API, over libcurl.
- `aer` and `preview` are Qiskit and need Python: use `qarm_exec.py`.

**The `ibm` backend, step by step:**
1. **Sign in.** Exchange `ibm_token` (or `env:VAR`) for a bearer token at IBM Cloud IAM, and find the
   CRN of `instance` by name with IBM Cloud Global Search; a CRN can also be given directly. The CRN's
   region picks the API: `eu-de.quantum.cloud.ibm.com` for European devices such as Aachen.
2. **Read the device.** Its configuration and properties. Only the CZ gate set is supported: Heron
   devices such as Fez, Torino, Aachen, Marrakesh and Kingston.
3. **Transpile in C.** IBM's service runs only ISA circuits: the device's own gates, on connected
   qubits. libqarm therefore:
   - chooses a chain of n connected qubits with the lowest readout and CZ errors;
   - moves qubits along the chain with SWAPs when a two-qubit gate's qubits are not neighbours;
   - writes CX as H CZ H;
   - fuses the one-qubit gates between two CZs into `rz sx rz sx rz`.
4. **Send one job per batch.** Each `QEXPECTN` batch is one Sampler job: OpenQASM 3 on physical qubits
   (`cz $12, $13;`), with every parameter vector and measurement basis, plus the two readout calibration
   circuits.
5. **Wait asynchronously.** The job's state is polled (`poll_seconds`, default 5). The samples are read
   from `data.c.samples` and readout corrected as in section 6.
6. **Sessions.** `qarm_session(1)` opens an IBM session. The Open Plan refuses sessions; then each job
   queues on its own, and the report says so.

**Async in C.** `qarm.h` now has an asynchronous form of `QEXPECTN` in all three builds:

```c
qarm_job *j = qarm_expect_n_async(&v, thetas, k);   // sent; on a QPU it is now in the queue
while (qarm_job_ready(j) == 0)
    do_other_work();                                 // the CPU keeps working
qarm_job_wait(j, energies, errors);                  // the results; frees the job
```

With libqarm on `ibm`, `qarm_job_ready` polls IBM once and never waits. The arm64 packet build sends the
batch with `QSUBMIT` and polls with `QPOLL`. Under QEMU on `ibm` the job is then really asynchronous; in
the other builds the batch is computed when it is sent, so the job is ready at once.

**What is not native yet.** `qarm_search` is classical in a libqarm build. Its oracle is built from the
function's machine code by qoracle, which is Python. A native `QSEARCH` needs that oracle compiled at
build time into a file next to the binary.

**Tested without an account.** `tests/ibm_mock.py` is a local stand-in for IBM's REST API. It has the
same paths, headers and JSON; the real configuration and properties of `ibm_fez` (from
qiskit-ibm-runtime's files); jobs that are Queued, then Running, then Completed. It rejects any circuit
that is not ISA for the device, and runs the rest on Aer, ideal or with Fez's calibrated gate and readout
errors. It is no proof that IBM's service accepts the same requests: that takes a run on a real device.

| `tests/test_libqarm.py` | result |
|---|---|
| native exact vs the plain build | the same output; every operation on 3 qubits to 1e-9 |
| simulator, 20000 shots | within 2 standard errors of exact |
| `ibm` via the mock, ideal, 3 qubits | ISA circuits with a SWAP (12 CZ), within 1 standard error |
| `ibm` via the mock, Fez's errors, H₂ | −1.1330 Ha (+4.3 mHa), 5 jobs in a session |
| the async program on the mock | polls the job while it is queued and running |

## 8. Q-ARM under QEMU

The third way to run a Q-ARM program, after `qarm_exec.py` (section 4) and a libqarm build (section 7):
`runtime/qemu` is a plugin for QEMU's user-mode emulator. It runs **the ordinary arm64 build**, the same
bytes that run on a plain ARM CPU and under `qarm_exec.py`, with Linux's real C library and libqarm as the
coprocessor:

```
$ aarch64-linux-gnu-gcc -O1 -iquote include examples/qarm/h2_vqe.c -o h2_vqe -lm
$ cmake -S runtime/qemu -B build/qemu -DQEMU_PLUGIN_INCLUDE=$HOME/qemu/include && cmake --build build/qemu
$ qemu-aarch64 -L /usr/aarch64-linux-gnu -plugin build/qemu/libqarm-qemu.so ./h2_vqe
$ qemu-aarch64 -plugin build/qemu/libqarm-qemu.so,config=config.json ./h2_vqe     # a real IBM device
```

`-L` points QEMU at the arm64 libraries on an x86-64 host; on an arm64 Linux host it is not needed.
The plugin's options are `config=FILE` (as `QARM_CONFIG`), `backend=exact|simulator|ibm` (as
`QARM_BACKEND`) and `quiet=on`.

**How it works.** QEMU translates the program into host code a block at a time and tells the plugin
about each block.
- The plugin instruments every `B .+32`, the first word of a packet.
- When one executes and the next word is the `QARM` magic, the plugin reads X1–X4 with
  `qemu_plugin_read_register` and the Hamiltonian and ansatz tables with `qemu_plugin_read_memory_vaddr`.
- It calls libqarm, then writes W0, X3, D0/D1 (`v0`/`v1`, or `z0`/`z1` on a CPU with SVE) and the output
  arrays back.
- The branch then skips the packet, as it does on a plain CPU.

An ordinary `B .+32` without the magic is left alone. Writing registers needs QEMU 10.1 or newer, whose
plugin API has `qemu_plugin_write_register`.

**What it adds over `qarm_exec.py`:**
- **The real C library.** `examples/qarm/vqe_from_file.c` reads its Hamiltonian with `fopen`, `fgets` and
  `sscanf`, and uses `malloc`, `qsort` and `snprintf`. It runs under QEMU; `qarm_exec.py`, with its small
  C library, stops at `fopen`.
- **Speed.** QEMU compiles the guest code instead of hooking it from Python: `h2_vqe` takes 0.04 s against
  0.47 s, and the gap grows with the program's own work.

**`QSEARCH` under QEMU** works from the guest program's own machine code. When a `QSEARCH` packet runs:
1. The plugin reads the first 32 bytes of the predicate `f` from guest memory. It runs
   `tools/qarm_oracle.py` with the program's path (`qemu_plugin_path_to_binary`), `f`'s address and those
   bytes.
2. The helper finds the load base: the one at which a function of the file has exactly these bytes. It
   lays the file's segments out there and has qoracle build the phase oracle from `f`'s machine code, as
   `qarm_exec.py` does. It prints the gates (x, cx, ccx, z).
3. libqarm (`qarm_search.c`) runs every basis input through the reversible gates to get the oracle's
   sign, checking that each work qubit returns to 0. It then simulates Grover's search (BBHT, for an
   unknown number of answers) on the 2^bits amplitudes of the search register alone. So a 1120-qubit
   oracle costs no more than its 2^bits inputs.
4. `qarm.h` checks every answer by calling `f` in the guest. A wrong answer leads to the classical search.

Oracles are cached per function. The helper is spawned with `posix_spawn`: a `fork` from a vCPU callback
deadlocks on QEMU's RCU lock.
- `tiny_search` finds `x = 3` from a 9-qubit, 21-gate oracle.
- Code Breaker finds BYRG from `consistent()`'s 1120-qubit, 4543-gate oracle, in 0.75 s. That matches
  `qarm_exec.py`, which also takes 0.75 s.

`QAVAIL` returns the runtime's features in `W1`, read by `qarm_features()`: `QARM_F_SEARCH` (1) and
`QARM_F_VQE` (2). The plugin reports `QSEARCH` only when the helper is there (option `helper=PATH`,
default the repository's `tools/qarm_oracle.py`; option `python=` for the interpreter). Without the helper,
`tiny_search` prints `classical search`.

The helper is Python: qoracle builds the oracle once per predicate, and the program itself stays native.
The search is simulated. Sending it to an IBM device from libqarm needs Toffoli decomposition and a
multi-controlled diffusion in its transpiler; `qarm_exec.py --qpu` does that today.

**Limits.** QEMU's user mode runs Linux programs on a Linux host. On a Mac, use a Linux VM or container,
or build the program natively against libqarm (section 7).

`tests/test_qarm_qemu.py` covers:
- `h2_vqe` gives the same output as under `qarm_exec.py`, on `-cpu cortex-a57` and on `-cpu max` (SVE);
- every ansatz operation agrees to 1e-9;
- `vqe_from_file` runs under QEMU, while `qarm_exec.py` refuses it;
- `QAVAIL`'s features, and `backend=simulator`;
- without the plugin, the classical path;
- with Qiskit and libcurl, the `ibm` backend through the mock of IBM's API.

CI builds QEMU 10.2.4 once and caches it.

### Binaries built without qarm.h: `tools/qarm_patch.py`

A program does not need to be written for Q-ARM. Many programs already contain a brute-force search: a
loop that tries every candidate until a predicate accepts one, or keeps the cheapest. `tools/qarm_patch.py`
rewrites such a binary, ELF for AArch64, after it was built:

```
$ aarch64-linux-gnu-gcc -O1 examples/qarm/legacy_search.c -o legacy_search        # no qarm.h in it
$ python3 tools/qarm_patch.py legacy_search --search find_key=valid_key:12 \
                                            --min cheapest_route=route_cost:12 -o legacy_search.qarm
qarm_patch: find_key (0x17c4): QSEARCH on valid_key (0x1798), 12 bits; the instruction at 0x17c4 (stp x29, x30, [sp, #-0x20]!) moved to the trampoline at 0x22000
qarm_patch: cheapest_route (0x1818): QMIN on route_cost (0x1800), 12 bits; ...
$ qemu-aarch64 -plugin libqarm-qemu.so legacy_search.qarm
[qarm-qemu: QSEARCH valid_key, 12 bits: oracle of 92 qubits and 345 gates from the machine code; Grover (BBHT) found x = 90 with 161 oracle calls in 23 rounds (simulated: ...)]
[qarm-qemu: QMIN route_cost, 12 bits: compute oracle of 193 qubits and 758 gates (32 result bits) from the machine code; Durr-Hoyer found x = 1381, value 0, with 626 oracle calls in 8 Grover searches (simulated)]
key: 90 (valid)
cheapest route: 1381 (cost 0)
```

`--search FUNC=PRED:BITS` states that `FUNC` returns some x < 2^BITS with `PRED(x) = 1`.
`--min FUNC=COST:BITS` states that it returns an x with the least `COST(x)`. The statement is the
user's; the tool cannot prove it.

The patch adds an executable segment with one trampoline per function. `FUNC`'s first instruction
becomes a `B` to the trampoline, which:
1. saves the arguments;
2. emits the `QSEARCH` or `QMIN` packet on `PRED`;
3. checks the runtime's answer by calling `PRED`, the program's own code;
4. returns the answer.

When the packet is not executed (a plain CPU), declined, finds nothing, or the check fails, the
trampoline restores the arguments. It then runs the moved instruction and branches back into `FUNC`'s
own loop. The patched binary behaves as before on every AArch64 CPU.

The patch is careful about the prologue:
- A `BTI` or `PACIASP` landing pad stays in place. With `PACIASP` the trampoline authenticates the
  return address before it returns.
- Every branch is direct, so BTI-guarded pages need no new landing pads.
- A PC-relative first instruction is refused.

`qarm_exec.py` finds packets in every executable segment, so it runs patched binaries too.

## 9. What is checked

`tests/test_qarm_exec.py`, in CI on Linux x86-64 (GCC cross compiler, qemu), Linux arm64 and macOS:

- the Code Breaker binary on the plain CPU (classical, `BYRG`) and under the runtime (Grover, `BYRG`);
- a second program: `printf` formats, `puts`, `putchar`, the exit code, `QAVAIL`; `QSEARCH` with several
  solutions, none, and 12 bits; the same output on the plain CPU, except `QAVAIL`;
- errors: a predicate that calls a function, a C library function the runtime lacks;
- `QMIN`, `QCOUNT`, `QPERIOD`, `QSIMON`, `QBV`, `QAMPEST` on the examples; `QCIRC`, `QRAND`, `QCAPS` on
  `ghz.c` (and, with Qiskit, on the noise model of Fez);
- with Qiskit (the export job, Linux x86-64): `tiny_search` on the noise model of Fez and through the
  `ibm` backend's local channel (the code path of a real device: one job, without the login); Code
  Breaker's oracle refused as larger than the device.

`tests/test_qarm_vqe.py`, in the same places:

- `h2_vqe` on the plain CPU and under the runtime gives the same output, with the ground state to 1e-6;
- every ansatz operation and Y terms, 3 qubits: the C path and the runtime agree to 1e-9. With Qiskit,
  the Aer energies agree with them within 5 standard errors;
- refused tables: an operation outside the register, too many qubits for the exact simulator;
- with Qiskit: `h2_vqe` on the noise model of Fez within 0.1 Ha, and through the local channel of the
  `ibm` backend (a session, 5 jobs).

`tests/test_libqarm.py`, with the host's own compiler (Linux, macOS, Windows without libcurl): the cases
in section 7. Its `ibm` part runs in the export job, which has Qiskit and libcurl.

## 10. Next

- More instructions on the same model: `QEVOLVE` (Trotterized time evolution of a Hamiltonian table),
  `QCOLLISION`.
- `QSEARCH` on an IBM device from libqarm, and natively without the Python helper;
- the QEMU plugin in system mode: Q-ARM as a coprocessor of a whole ARM Linux machine;
- libqarm: a first run on a real IBM device, recorded here;
  devices with the ECR gate set.
- VQE: IBM's error mitigation; larger molecules (N₂ active
  spaces, 16–20 qubits) with a multithreaded state vector.
- Smaller oracles for hardware: qoracle's circuits keep the CPU's register widths, and a 2-bit compare
  becomes 12 Toffolis.
- Vector instructions in qoracle, so that `-O2` builds work.

## 11. What this is and is not

It does not make programs faster today: the classical part runs as fast as before, but a quantum
instruction is simulated, and on hardware Grover's search pays off only for large searches. What it
shows is a way for quantum computing to enter ordinary software, the way earlier processor features did:
one binary, a quantum path where a coprocessor exists, a classical path elsewhere, and an oracle that is
the program's own code, so that both paths compute the same function and each quantum answer can be
checked against it.
