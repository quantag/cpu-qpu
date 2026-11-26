# Q-ARM v2: quantum instructions with a classical fallback, in one ARM binary

Status: *proof of concept*. Two instructions, a runtime with the exact simulator as the quantum
coprocessor, tested on Linux (arm64, and x86 with qemu) and macOS on Apple Silicon. Not an official Arm
extension, and no claim of a speedup today (section 7).

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

## 5. What is checked

`tests/test_qarm_exec.py`, in CI on Linux x86-64 (GCC cross compiler, qemu), Linux arm64 and macOS:

- the Code Breaker binary on the plain CPU (classical, `BYRG`) and under the runtime (Grover, `BYRG`);
- a second program: `printf` formats, `puts`, `putchar`, the exit code, `QAVAIL`; `QSEARCH` with several
  solutions, none, and 12 bits; the same output on the plain CPU, except `QAVAIL`;
- errors: a predicate that calls a function, a C library function the runtime lacks.

## 6. Next

- More instructions on the same model: `QCOUNT` (quantum counting), `QMIN` (Dürr–Høyer minimum),
  `QPERIOD` (period finding).
- Backends from `config.json`: the noise model of an IBM device and real devices, for oracles small
  enough (`qcpu_run.py`).
- Vector instructions in qoracle, so that `-O2` builds work.

## 7. What this is and is not

It does not make programs faster today: the classical part runs as fast as before, but a quantum
instruction is simulated, and on hardware Grover's search pays off only for large searches. What it
shows is a way for quantum computing to enter ordinary software, the way earlier processor features did:
one binary, a quantum path where a coprocessor exists, a classical path elsewhere, and an oracle that is
the program's own code, so that both paths compute the same function and each quantum answer can be
checked against it.
