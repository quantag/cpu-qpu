# Q-ARM v2: quantum instructions with a classical fallback, in one ARM binary

Status: *proof of concept*. Two instructions, a runtime with the exact simulator as the quantum
coprocessor, tested on Linux (arm64, and x86 with qemu) and macOS on Apple Silicon. Not an official Arm
extension, and no claim of a speedup today (section 8).

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

## 6. What is checked

`tests/test_qarm_exec.py`, in CI on Linux x86-64 (GCC cross compiler, qemu), Linux arm64 and macOS:

- the Code Breaker binary on the plain CPU (classical, `BYRG`) and under the runtime (Grover, `BYRG`);
- a second program: `printf` formats, `puts`, `putchar`, the exit code, `QAVAIL`; `QSEARCH` with several
  solutions, none, and 12 bits; the same output on the plain CPU, except `QAVAIL`;
- errors: a predicate that calls a function, a C library function the runtime lacks;
- with Qiskit (the export job, Linux x86-64): `tiny_search` on the noise model of Fez and through the
  `ibm` backend's local channel (the code path of a real device: one job, without the login); Code
  Breaker's oracle refused as larger than the device.

## 7. Next

- More instructions on the same model: `QCOUNT` (quantum counting), `QMIN` (Dürr–Høyer minimum),
  `QPERIOD` (period finding).
- `QSUBMIT` / `QPOLL` / `QWAIT` (section 5), so the CPU keeps working while a QPU job is queued.
- Smaller oracles for hardware: qoracle's circuits keep the CPU's register widths, and a 2-bit compare
  becomes 12 Toffolis.
- Vector instructions in qoracle, so that `-O2` builds work.

## 8. What this is and is not

It does not make programs faster today: the classical part runs as fast as before, but a quantum
instruction is simulated, and on hardware Grover's search pays off only for large searches. What it
shows is a way for quantum computing to enter ordinary software, the way earlier processor features did:
one binary, a quantum path where a coprocessor exists, a classical path elsewhere, and an oracle that is
the program's own code, so that both paths compute the same function and each quantum answer can be
checked against it.
