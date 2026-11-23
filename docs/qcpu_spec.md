# qcpu: running marked functions on a quantum CPU — design specification

Status: **draft for discussion**. Sections marked *(exists)* describe code that is already in this
repository or on the `qpu-dll` branch; everything else is planned.

## 1. Goal

Write one ordinary C program. Mark some of its functions with `__qcpu__`. Build it once with an
ordinary toolchain. At run time, choose **where** the marked functions run — natively on the CPU, on
the quantum CPU circuit in a simulator, or on a real quantum processor — and **how** they are turned
into a circuit, without changing the source or rebuilding.

```c
#include <stdio.h>
#include <qcpu.h>

static const int secret = 0x9C;
static int peg(int code, int k) { return (code >> (2 * k)) & 3; }

__qcpu__ int score(int guess) { /* uses peg() and secret */ }

int main(void) {
    int g;
    while (scanf("%x", &g) == 1)
        printf("%d\n", score(g));        /* an ordinary call */
}
```

```bash
qcpucc game.c -o game                    # build once
./game                                   # default: simulated quantum CPU
QCPU_BACKEND=native ./game               # same binary, plain CPU
QCPU_VERIFY=1 ./game                     # both, every call compared
```

## 2. Terms

| term | meaning |
|---|---|
| host | the computer that runs the program: macOS, Linux or Windows, any CPU |
| quantum CPU (QCPU) | the reversible stored-program machine of this repository, as a quantum circuit |
| QCPU function | a function marked `__qcpu__`, with everything it calls |
| QCPU ISA | the instruction set the QCPU function is compiled to: `arm64`, `riscv64`, `riscv32`, `x86_64` |
| mode | how a QCPU function becomes a circuit: `interpreted` or `compiled` |
| backend | what executes the circuit: `native`, `sim`, or a driver |
| driver | a plug-in library that runs circuits on a device or service |
| configuration | ISA, register width, memory size, cycle limit, mode, backend, verification |

The four axes are independent; "any code on any configuration" means any combination that the
function's requirements allow:

```
host OS  x  QCPU ISA  x  mode  x  backend
macOS       arm64        interpreted   native
Linux       riscv64      compiled      sim
Windows     riscv32                    driver: ibm, aer, file, ...
            x86_64
```

## 3. Source level

### 3.1 The annotation

`qcpu.h` defines one attribute macro:

```c
__qcpu__ int score(int guess);
```

- On the host build it expands to a weak definition with default visibility: the function's own body
  becomes the native fallback (section 5).
- On the QCPU build it expands to a section attribute (`.text.qcpu`) that lets the build find the
  marked functions.
- Without the build driver (a plain compiler), `qcpu.h` expands `__qcpu__` to nothing: the program is
  an ordinary C program.

The keyword has no run-time meaning. It decides what the build generates.

### 3.2 Rules for QCPU functions

Checked by the build; each violation is an error naming the function and the reason.

1. **External linkage**, defined in a file passed to the build.
2. **Parameters**: up to 8 integers (`int`, `long`, `unsigned`, fixed-width types), and pointers to
   `int` buffers annotated with their direction and length *(exists, `qpu-dll`)*:
   ```c
   __qcpu__ int  sum (const int *QCPU_IN(n) v, int n);
   __qcpu__ void fill(int *QCPU_OUT(n) v, int n, int x);
   __qcpu__ void sort(int *QCPU_INOUT(n) v, int n);
   ```
3. **Result**: one integer or `void`.
4. **Pure**: no I/O, no system or library calls, in the function or anything it calls. Helpers it
   calls (`peg`) and constants it reads (`secret`) are included automatically.
5. **No state between calls**: global variables are read with their initial values; writes do not
   persist to the next call.
6. **Machine limits**: the supported instruction subset of the QCPU ISA, and the reduced-width model
   (register width W ≤ 16); a call whose values do not fit is reported (section 8).

### 3.3 Several files

Any number of files may contain `__qcpu__` functions. QCPU functions may call each other; they are
linked into one QCPU program per executable.

## 4. Build

### 4.1 Pipeline

```
game.c ──┬── QCPU side ── compile for the QCPU ISA ── keep marked functions + callees
         │                ── generate machine configuration and circuit
         │                ── emit qcpu_gen.c: circuit as data + one wrapper per function
         │
         └── host side ── compile natively: marked bodies become weak native fallbacks
                          renamed copy: __qcpu_native_<name>, for verification

qcpu_gen.c + runtime (engine, backend loader) + host objects ── link ── game
```

The circuit generator is Python and runs at build time only. The built program contains no Python.

### 4.2 Command-line driver

```
qcpucc [options] files... -o output

  --qcpu-isa arm64|riscv64|riscv32|x86_64   ISA of the QCPU side (default arm64)
  --qcpu-width N                            register width, 8..16 (default 16)
  --qcpu-memory N                           data memory in ints, power of two (default 512)
  --qcpu-max-steps N                        default cycle limit per call (default 1000000)
  --qcpu-modes interpreted[,compiled]       circuits to embed (default interpreted)
  --shared                                  build a library (.dylib/.so/.dll) instead of an executable
  other options                             passed to the host compiler
```

### 4.3 CMake

```cmake
include(cpu-qpu/cmake/QCPU.cmake)

add_qcpu_executable(game SOURCES game.c
                    QCPU_ISA arm64 QCPU_WIDTH 16 QCPU_MODES interpreted)

add_qcpu_library(codebreaker SHARED SOURCES codebreaker.c)    # for other programs to link
```

### 4.4 What the executable contains

| part | content |
|---|---|
| host code | the program, the wrappers, the native fallbacks, the runtime |
| QCPU program | machine code of the QCPU functions, as program-memory bits |
| circuit(s) | one machine cycle as a gate list (X, CNOT, Toffoli, Fredkin) per embedded mode |
| layout | which qubits hold the argument registers, the return register, data memory, PC, run flag |

## 5. Run time

### 5.1 A call

1. The call goes to the generated wrapper (the linker chose it over the weak native body).
2. The wrapper reads the configuration (section 6) and picks the backend.
3. It builds the initial state: program bits, arguments in the argument registers, input buffers
   in data memory, return address at the halt slot.
4. The backend executes the circuit until the machine halts or the cycle limit is reached.
5. The wrapper reads the return register and output buffers and returns.

### 5.2 Backends

| backend | executes | status |
|---|---|---|
| `native` | the native fallback; no circuit | *exists* (`QPU_BACKEND=native` on `qpu-dll`) |
| `sim` | the circuit, in the exact basis-state simulator built into the runtime | *exists* (C engine on `qpu-dll`) |
| driver | the circuit, through a plug-in (`libqcpu_<name>.dylib/.so/.dll`) | planned |

### 5.3 Modes

| mode | circuit | size | status |
|---|---|---|---|
| `interpreted` | the quantum CPU executes the function's machine code | large: every instruction is a machine cycle | *exists* |
| `compiled` | the function compiled directly to reversible logic (qoracle) | small: e.g. 25 qubits instead of 1,242 | planned; section 5.5 |
| `auto` | `compiled` where the function has a compiled circuit that fits the backend, else `interpreted` | — | planned |

A mode can only be chosen at run time if it was embedded at build time; otherwise the call fails
with a clear message.

### 5.5 Compiled mode: a small dedicated circuit per function (qoracle)

The interpreter runs any supported machine code, but every executed instruction costs a full machine
cycle, so its circuits are far too large for any existing quantum computer. Compiled mode builds,
for each `__qcpu__` function, a circuit that computes only that function: no program counter, no
fetch, no decode. This is what qoracle (`github.com/quantag/qoracle`) does today for single
functions; compiled mode embeds its output.

**How the circuit is made** (qoracle, at build time):
1. compile the function for AArch64;
2. execute the machine code symbolically on all paths: every register bit becomes a Boolean formula of
   the input bits, branches are merged, loops are unrolled (the bound is measured by running the code);
3. reduce the formulas to an AND/XOR network (hash-consing, constant folding);
4. map it to gates: a Toffoli per AND, CNOTs per XOR, Bennett copy-out and uncompute, so that all work
   qubits return to 0;
5. verify the circuit against the real machine code in a CPU emulator: on all inputs up to 16 input
   bits, otherwise on edge cases and 4,096 random inputs. A function that fails is not embedded.

**Input and output widths.** A compiled circuit needs to know how many bits each argument really
uses; the fewer, the smaller the circuit. Annotations give them:

```c
__qcpu__ int QCPU_RESULT_BITS(6) score(int QCPU_BITS(8) guess);
```

Without annotations the build uses the register width W, and reports the circuit size so that the
annotation can be tightened. Values outside the declared width are an error at the call (the wrapper
checks them before running the circuit).

**What compiled mode supports** (the current qoracle front end):

| supported | not yet |
|---|---|
| integer arithmetic incl. multiply, divide, shifts | writing memory: local arrays, pointers, the stack (`-O0`) |
| branches, conditional selects, bounded loops | calls that the compiler does not inline |
| read-only tables (`static const` arrays), switch tables | floating point |
| AArch64 | RISC-V, x86-64 |

A function outside this set keeps only its interpreted circuit, and the build says why, for example
"`score`: local array `cg` is written (stack) — rewrite without arrays or use interpreted mode".

**Measured sizes** (qoracle, clang -O1, verified on all inputs):

| function | inputs | qubits | Toffoli | all gates | interpreted, for comparison |
|---|---|---|---|---|---|
| `is_root` (Grover predicate, phase oracle) | 4 bits | 25 | 28 | 67 | 1,242 qubits, 98,504 gates |
| Code Breaker `score(guess)`, secret fixed, 6-bit result | 8 bits | 312 | 358 | 1,088 | hundreds of thousands of qubits |
| Code Breaker `score` with colour-count arrays | 8 bits | — | — | — | rejected: writes local arrays |

**Running a compiled circuit on hardware.** For an ordinary call the input is a basis state, so the
driver job is: X gates to set the input bits, the compiled circuit, measurement of the result qubits.
Two properties help on noisy devices:
- the answer is deterministic, so a few shots and a majority vote recover it;
- every work qubit must end in 0; the driver also measures them, and any shot with a work qubit at 1 is
  known to be wrong and discarded (free error detection).

**What fits today.** Small predicates and arithmetic on a few bits — `is_root`: 25 qubits and
28 Toffoli gates — are within reach of current devices. Code Breaker's `score` (312 qubits,
358 Toffoli gates, depth 165) is beyond what current noisy devices run reliably; qubit reuse and
measurement-based uncomputation (qoracle roadmap) are the way to bring such functions closer. The
runtime decides per call with the driver's `capacity` (section 5.4): `QCPU_MODE=auto` takes the
compiled circuit when it fits the device, and otherwise the simulator or the interpreter.

**Quantum use.** The same compiled circuit, given an input in superposition, is an oracle: as a phase
oracle in Grover search (find the secret code from `score`), or a compute oracle in period finding.
That is outside ordinary calls (section 11), but the circuit is the same.

### 5.4 Driver interface

```c
typedef struct {
    uint32_t nqubits;
    const uint32_t *gates; long ngates;     /* op << 30 | q0, q1, q2  (x, cx, ccx, cswap) */
    const uint32_t *ones;  long nones;      /* initial basis state: qubits set to 1 */
    const uint32_t *measure; long nmeasure; /* qubits whose values are needed */
    long cycles;                            /* repetitions of the gate list, for drivers that loop */
} qcpu_job;

typedef struct {
    const char *name;
    int  (*capacity)(uint32_t *max_qubits, long *max_gates);
    int  (*run)(const qcpu_job *job, uint8_t *bits_out, char *err, size_t errlen);
} qcpu_driver;

const qcpu_driver *qcpu_driver_entry(void);  /* exported by every driver library */
```

The runtime asks `capacity` first and refuses jobs that do not fit, with the numbers ("circuit needs
N qubits, device has M"). Planned drivers: `file` (writes OpenQASM 3 for inspection), `aer` (Qiskit
Aer, through a small helper process), `ibm` (IBM Quantum; usable only for compiled-mode circuits small
enough for the device).

## 6. Configuration

### 6.1 Settings

| setting | values | default |
|---|---|---|
| `QCPU_BACKEND` | `sim`, `native`, driver name | `sim` |
| `QCPU_MODE` | `interpreted`, `compiled`, `auto` | first embedded mode |
| `QCPU_SHOTS` | shots per call on a hardware driver (majority vote) | `16` |
| `QCPU_VERIFY` | `0`, `1` | `0` |
| `QCPU_STRICT` | `0`, `1`: stop on a non-exact or mismatching call | `0` |
| `QCPU_MAX_STEPS` | cycles per call | build value |
| `QCPU_QUIET` | `0`, `1` | `0` |
| `QCPU_DRIVER_PATH` | directories searched for drivers | next to the executable |
| `QCPU_LOG` | file for a per-call log: function, mode, backend, cycles, qubits, gates, time | none |

### 6.2 Where settings come from (later overrides earlier)

1. values given at build time;
2. `qcpu.toml` next to the executable, then in the working directory;
3. environment variables;
4. per function: `QCPU_BACKEND_score=native` (environment) or a `[function.score]` table;
5. from the program: `qcpu_set("backend", "native")`.

```toml
backend = "sim"
mode    = "interpreted"
verify  = true

[function.score]
backend = "native"          # this one on the CPU, the others simulated
```

The `QPU_*` variables of the `qpu-dll` branch are accepted as aliases.

## 7. Verification

With `QCPU_VERIFY=1` every call also runs the native fallback and compares the return value and the
output buffers. A difference is reported with the function, the arguments and both results, and stops
the program under `QCPU_STRICT=1`. This is how a demo proves itself while it runs.

## 8. Errors and limits

| situation | detected | result |
|---|---|---|
| I/O or library call in a QCPU function | build | error naming the call |
| unsupported instruction | build | error naming the instruction and function |
| state shared between calls | build (writes to globals) | warning |
| value does not fit the register width | run (verify, or the exactness check) | warning or stop (`QCPU_STRICT`) |
| cycle limit reached | run | error; `QCPU_MAX_STEPS` raises it |
| circuit too large for a driver | run | refused with the sizes |
| mode not embedded | run | error naming the build option |
| function outside compiled mode's support | build | message; only the interpreted circuit is embedded |
| argument wider than its `QCPU_BITS` | run, before the circuit | error naming the argument |
| work qubit measured as 1 on hardware | run | that shot is discarded; error if no shot survives |

## 9. Portability

| host | build output | status |
|---|---|---|
| Linux | executable / `.so` | engine *exists*, driver planned |
| Windows | executable / `.dll` (MinGW, MSVC, clang) | engine *exists*, driver planned |
| macOS | executable / `.dylib` | planned |

| QCPU ISA | interpreted | compiled |
|---|---|---|
| arm64 | *exists* (library build on `qpu-dll`) | planned; qoracle *exists* as a separate tool |
| riscv64, riscv32 | *exists* (library build on `qpu-dll`) | — |
| x86_64 | *exists* (library build on `qpu-dll`) | — |

## 10. Implementation plan

1. Update `qpu-dll` to current `main`; macOS `.dylib`; rename `QPU_*` to `QCPU_*` with aliases.
2. `qcpu.h`, `__qcpu__`, the `qcpucc` driver: single-file split, weak fallbacks, call-graph pruning.
3. Compiled mode: `QCPU_BITS` / `QCPU_RESULT_BITS`, qoracle at build time, the compiled circuit run
   by the same engine, `QCPU_MODE=compiled|auto`.
4. CMake module `QCPU.cmake`.
5. Example: interactive Code Breaker (`examples/codebreaker/`) in both modes, tests on all three host
   systems.
6. Driver interface; `file` driver (OpenQASM 3), then `aer` and `ibm` for compiled circuits that fit,
   with shots, majority vote and work-qubit checks.
7. Configuration file and per-function settings; call log.

## 11. Open questions

- Pointers to data other than `int` buffers (`char`, structs) — needs byte memory access on the QCPU.
- Persistent QCPU state between calls (a QCPU "object" with its own memory) — conflicts with the
  rule that a call leaves no history behind; possible as explicit in/out buffers.
- Batched and superposed calls: `score` evaluated on a superposition of guesses, as an oracle inside
  a quantum algorithm — an API beyond ordinary calls.
- Asynchronous calls for slow drivers (`qcpu_call_async`, futures).
- Whether `compiled` mode should become the default when it is available.
- Writing memory in compiled mode (local arrays) — a quantum RAM inside the compiled circuit, planned
  in qoracle.
- Making qoracle a library dependency of cpu-qpu, or calling it as a separate tool at build time.
