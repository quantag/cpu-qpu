# Q-ARM v2: quantum instructions with a classical fallback, in one ARM binary

Status: *proof of concept*. Two instructions, a runtime with the exact simulator as the quantum
coprocessor, tested on Linux (arm64, and x86 with qemu) and macOS on Apple Silicon. Not an official Arm
extension, and no claim of a speedup today (section 10).

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
| `0x10` | `QAVAIL` | — | `W0 = 1` |
| `0x11` | `QSEARCH` | `X1` = address of `f`, `W2` = bits (1 to 16) | `W0 = 1` and `W3 = x` with `f(x) = 1`; or `W0 = 2`: none |

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

**Async instructions (design, not implemented).** A program that should keep working while the QPU
runs needs the asynchrony in the instruction set rather than hidden in the runtime:

| opcode | name | in | out |
|---|---|---|---|
| `0x12` | `QSUBMIT` | `X1` = f, `W2` = bits | `W0` = 1 and `X3` = ticket, or `W0` = 0 (no coprocessor) |
| `0x13` | `QPOLL` | `X3` = ticket | `W0` = 1 done, 3 still queued or running |
| `0x14` | `QWAIT` | `X3` = ticket | as `QSEARCH`: `W0` = 1 and `W3` = x, or `W0` = 2 |

On the simulator, `QSUBMIT` would compute the answer at once and `QPOLL` would always report done. On a
plain CPU, `W0` = 0 sends the program down the classical path, as now. A ticket is the backend's job id held
by the runtime, so a program could submit several searches and collect them later. That is how CUDA-Q's
`cudaq::sample_async` and Qiskit's `job.result()` already work.

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
| `0x24` | `QSESSION` | `W1` = 1 open, 0 close | `W0` = 1 |
| `0x26` | `QVFREE` | `X3` = handle | `W0` = 1 |

On a plain CPU every packet leaves `W0` = 0, and `include/qarm.h` takes the classical path. That path is
a state-vector simulation in C, exact up to `QARM_VQE_MAX_QUBITS` qubits (default 12; link with `-lm`).
Opcodes `0x23` (`QGRAD`, a parameter-shift gradient in one job) and `0x25` (`QVSAMPLE`, the final
state's bitstrings) are reserved for the next step.

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
[qarm: QVLOAD: 2 qubits, 5 Pauli terms, an ansatz of 3 operations (1 two-qubit) with 1 parameter; on preview: noise model of fake_fez (156 qubits): 2 measurement bases, readout correction, up to 1 two-qubit gates after transpiling]
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

With libqarm on `ibm`, `qarm_job_ready` polls IBM once and never waits. In the other builds the batch is
computed when it is sent, so the job is ready at once. This is the asynchronous instruction set of
section 5 (`QSUBMIT` / `QPOLL` / `QWAIT`), as a C API.

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

## 8. What is checked

`tests/test_qarm_exec.py`, in CI on Linux x86-64 (GCC cross compiler, qemu), Linux arm64 and macOS:

- the Code Breaker binary on the plain CPU (classical, `BYRG`) and under the runtime (Grover, `BYRG`);
- a second program: `printf` formats, `puts`, `putchar`, the exit code, `QAVAIL`; `QSEARCH` with several
  solutions, none, and 12 bits; the same output on the plain CPU, except `QAVAIL`;
- errors: a predicate that calls a function, a C library function the runtime lacks;
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

## 9. Next

- More instructions on the same model: `QCOUNT` (quantum counting), `QMIN` (Dürr–Høyer minimum),
  `QPERIOD` (period finding).
- `QSUBMIT` / `QPOLL` / `QWAIT` (section 5), so the CPU keeps working while a QPU job is queued.
- libqarm: `QSEARCH` from a build-time oracle file; a first run on a real IBM device, recorded here;
  devices with the ECR gate set.
- VQE: `QGRAD` and `QVSAMPLE`; IBM's error mitigation; a build-time tool that writes the tables of
  a molecule as a C header (PySCF, Jordan–Wigner or parity mapping).
- Smaller oracles for hardware: qoracle's circuits keep the CPU's register widths, and a 2-bit compare
  becomes 12 Toffolis.
- Vector instructions in qoracle, so that `-O2` builds work.

## 10. What this is and is not

It does not make programs faster today: the classical part runs as fast as before, but a quantum
instruction is simulated, and on hardware Grover's search pays off only for large searches. What it
shows is a way for quantum computing to enter ordinary software, the way earlier processor features did:
one binary, a quantum path where a coprocessor exists, a classical path elsewhere, and an oracle that is
the program's own code, so that both paths compute the same function and each quantum answer can be
checked against it.
