# arm-qpu: run an ARM64 binary on a quantum computer

> **Q-ARM experimental branch:** OpenQASM 2 unitary gates can now be embedded in
> CPU-skippable AArch64 packets. The quantum interpreter executes them; a normal
> ARM CPU branches over them. See [Q-ARM guide and packet ABI](docs/qarm.md).
>
> Quick start: `python qarm.py compile examples/qarm/bell.qasm -o bell`, then
> `python qarm.py run bell.bin`. Reset, mid-circuit measurement and feedback are
> not supported in this first coherent version. Plain ARM behavior is unchanged.


Write an ordinary C program, compile it to an AArch64 binary with clang or gcc, and run that
binary on a **quantum stored-program (von Neumann) processor** made entirely of quantum gates.
The program's output appears as if you had run it locally:

```
$ python3 arm_qpu.py hello.o
Hello from the QPU!
84 = 2 2 3 7
```

The quantum processor is **one fixed reversible circuit** (X, CNOT, Toffoli, Fredkin gates).
It has program memory, a program counter, fetch, a decoder for 27 A64 instruction classes,
31 registers + SP, NZCV flags, data memory with a stack, an ALU (adder, barrel shifter,
multiplier, divider, bitmask decoder), and a branch unit with calls and returns.
The binary is **not compiled into the circuit**: its 32-bit instruction words are loaded as
the initial state of the program-memory qubits. Nothing inside the circuit is measured or
classically dispatched. The same bytes also run on an ARM CPU emulator as a reference, so
every run is checked.

## Quick start

`examples/hello.c` is plain C:

```c
#include <stdio.h>

int smallest_factor(int n) {
    for (int d = 2; d * d <= n; d++)
        if (n % d == 0)
            return d;
    return n;
}

int main(void) {
    int n = 84;
    printf("Hello from the QPU!\n");
    printf("%d =", n);
    while (n > 1) {
        int p = smallest_factor(n);
        printf(" %d", p);
        n /= p;
    }
    printf("\n");
    return 0;
}
```

**Step 1: compile it to an ARM64 binary** with clang or gcc:

```bash
clang --target=aarch64-linux-gnu -O1 -fno-stack-protector -Iinclude -c examples/hello.c -o hello.o
# or
aarch64-linux-gnu-gcc -O1 -fno-stack-protector -Iinclude -c examples/hello.c -o hello.o
```

`hello.o` is a normal AArch64 ELF object file: these are the bytes that run. `-Iinclude`
picks up the small `include/stdio.h` of this repository (no system headers or libc needed).
Inspect the machine code with `llvm-objdump -d hello.o`.

**Step 2: run the binary on the QPU:**

```bash
python3 arm_qpu.py hello.o
```

```
Hello from the QPU!
84 = 2 2 3 7
```

Like a local run, the exit code is `main`'s return value.

**Step 3 (optional): see what happened** with `-v`:

```bash
python3 arm_qpu.py hello.o -v
```

```
Hello from the QPU!
84 = 2 2 3 7
------------------------------------------------------------
MATCH: the QPU produced the same result as the CPU
return value X0: CPU = 0, QPU = 0
program output: CPU and QPU identical
quantum machine: 520364 qubits, 91883088 gates (57841320 Toffoli), 228 steps, W = 12
report: hello.main.report.md
```

The report (`.md` and `.json`) contains:
- the program output from both machines;
- registers, flags, data memory and the step-by-step PC trace compared between CPU and QPU;
- the disassembly, with every instruction marked supported or not and how often it ran;
- the circuit's resources and the model check.

`--report PREFIX` writes the report without printing the analysis.

**Run a single function** instead of `main`:

```bash
python3 arm_qpu.py factor.o --entry factorize --args 84 '&0'
# factorize(84, &0) = 4          (&0 = pointer to user data cell 0; the factors are written there)
```

Options: `--entry FUNC` (default `main`), `--args` for X0, X1, ... (`&k` = pointer to user data
cell k), `--mem=1,2,3` to initialise user data cells (32-bit), `-v`, `--report PREFIX`,
`--config config.json`.

*Shortcut:* `arm_qpu.py` also accepts a `.c` file and runs step 1 itself (`--cc clang|gcc`,
`-O 0|1|2|s`). The compile command it used is shown with `-v`. The binary is still what runs.

## Examples

| file | what it prints |
|---|---|
| `examples/hello.c` | a greeting and the factorization of 84 |
| `examples/factor.c` | prime factorizations of 84, 91 and 97 |
| `examples/algorithms.c` | gcd, Fibonacci, primality, popcount, integer square root, modular power, max |

```
$ python3 arm_qpu.py algorithms.o
Classic algorithms on a quantum von Neumann machine
  gcd(84, 36)      = 12
  fib(10)          = 55
  is_prime(89)     = yes
  popcount(109)    = 5
  isqrt(70)        = 8
  3^5 mod 13       = 9
  max3(-7, 12, 5)  = 12
```

The inputs go through a `volatile` variable, so the compiler cannot compute the answers in
advance: the QPU computes them. `factor.c` and `algorithms.c` each take about 20 seconds to
simulate on a laptop; `hello.c` about 7 seconds.

## Grover search with a C function as the oracle

`grover.py` uses a compiled C predicate as the oracle of Grover's algorithm. The predicate is
ordinary C, `int f(int x)` returning 0 or 1, and runs as ARM machine code on the quantum machine:

```c
/* examples/grover_divisor.c */
int divides91(int x)
{
    return x > 1 && 91 % x == 0;
}
```

```bash
python3 grover.py examples/grover_divisor.c --entry divides91 --bits 4 --iterations 0,1,2,3
```

```
predicate divides91: 10 instructions, solutions over 0..15: [7, 13] (M = 2 of N = 16); cycles per input 5..9, common bound T = 9
machine: W = 8, 9 instruction classes, 16 program slots, 9 cycles: 1762 qubits, 85374 gates (49680 Toffoli) per run of U
basis check: the quantum machine computes f(x) correctly for all 16 inputs
simulator: exact sparse state vector; optimal number of iterations for M/N = 2/16: 2
k = 0: P(solution) exact 0.1250, theory 0.1250; measured 142/1000 = 0.142 [95% CI 0.122..0.165]; workspace restored with probability 1.0000
k = 1: P(solution) exact 0.7812, theory 0.7813; measured 796/1000 = 0.796 [95% CI 0.770..0.820]; workspace restored with probability 1.0000
k = 2: P(solution) exact 0.9453, theory 0.9453; measured 950/1000 = 0.950 [95% CI 0.935..0.962]; workspace restored with probability 1.0000
k = 3: P(solution) exact 0.3301, theory 0.3301; measured 344/1000 = 0.344 [95% CI 0.315..0.374]; workspace restored with probability 1.0000
PASS: amplitudes amplified as Grover's algorithm predicts, workspace restored in every shot
```

`x` is the low `--bits` bits of X0, put into superposition with Hadamard gates. One iteration is:

1. **U**: run the ARM machine for T cycles. X0 becomes f(x); the history stays in per-cycle registers.
2. **Z** on bit 0 of X0: phase −1 where f(x) = 1.
3. **U⁻¹**: run the machine backwards. X0 is x again, and all work registers and the history are back to 0.
4. **Diffusion** on the input qubits.

Different inputs take different paths through the code (5 to 9 cycles here); T is the largest
cycle count over the whole domain, so every branch halts. Before the quantum run, the tool checks
every input on the CPU (it must halt, return 0 or 1 and stay inside the W-bit model) and on the
quantum machine gate by gate. `--no-uncompute` drops step 3: the input then stays entangled with
the result and the history, and the success probability falls to 5% instead of 95%.

**Simulator.** The machine is a permutation circuit (X, CNOT, Toffoli, Fredkin), so with k input
qubits in superposition the state has at most 2^k nonzero amplitudes, however many qubits the machine
has. `qcpu/sparsesim.py` stores exactly those amplitudes. This is an exact simulation of the quantum
state, including the interference, and it runs in about a second. `--backend aer` uses Qiskit Aer
(matrix product state) instead; it agrees, but is practical only for tiny examples such as
`examples/grover_tiny.c`. `tests/test_grover.py` checks the sparse simulator against Qiskit's dense
statevector on random circuits and against Aer on the interpreter. All results are from simulation,
not from quantum hardware. Grover's algorithm gives a quadratic reduction in oracle calls, but the
interpreter makes each call far more expensive than a hand-built oracle, so this is a demonstration,
not a speed-up.

More examples: `examples/grover_square.c` (x² ≡ 9 mod 16, 4 solutions of 16, found with certainty in
one iteration), and `--bits 6` with `divides91` (2 solutions of 64, P = 0.999 after 4 iterations).

## Shor's algorithm with modexp written in C

`shor.py` factors a small number with Shor's algorithm. The classical core of the algorithm,
`a^x mod N`, is an ordinary C function that runs as ARM machine code on the quantum machine, with the
exponent `x` in superposition:

```c
/* examples/shor_modexp.c */
unsigned modexp(unsigned a, unsigned x, unsigned n)
{
    unsigned r = 1;
    while (x) {
        if (x & 1)
            r = r * a % n;
        a = a * a % n;
        x >>= 1;
    }
    return r;
}
```

```bash
python3 shor.py --N 15 --a 7
```

```
modexp: 18 instructions; a = 7, N = 15; exponent register: t = 8 qubits (x = 0..255)
CPU check: a^x mod N correct for all 256 exponents, 4..101 machine cycles, all inside the 9-bit model
machine: W = 9, 12 instruction classes, 32 program slots, 101 cycles: 6526 qubits, 2581560 gates (1229776 Toffoli) per run of U
circuit: H on x, U, copy, U^-1, inverse QFT: 6530 qubits, 5163180 gates
after U: X0 = a^x mod N in all 256 branches, every branch halted, no trap
after U^-1: exponent register restored: True; all 6518 work and history qubits back in their initial state: True
measurement of x, exact probabilities (textbook value in brackets):
    y =     0  P = 0.2500 (0.2500)  y/2^t = 0.0000  no order found
    y =    64  P = 0.2500 (0.2500)  y/2^t = 0.2500  r = 4 -> 15 = 3 x 5
    y =   128  P = 0.2500 (0.2500)  y/2^t = 0.5000  r = 4 -> 15 = 3 x 5
    y =   192  P = 0.2500 (0.2500)  y/2^t = 0.7500  r = 4 -> 15 = 3 x 5
largest difference from the textbook distribution: 6.1e-16
1000 shots (seed 2026): 777 gave the order r = 4, 777 gave the factors = 0.777 [95% CI 0.750..0.802]
exact probability per shot: order 0.7500, factors 0.7500; a uniformly random y would give the factors with probability 0.0586
RESULT: 15 = 3 x 5   (order of 7 modulo 15: r = 4)
```

One run is:

1. **H** on the exponent qubits: all 2^t exponents at once.
2. **U**: the ARM machine runs `modexp`. The loop runs once per bit of `x`, so the branches take
   different numbers of cycles (4 to 101 here); the machine runs for the largest.
3. **Copy** the result into a fresh register (CNOT gates).
4. **U⁻¹**: the machine runs backwards. The exponent and every work and history qubit are restored.
5. **Inverse quantum Fourier transform** on the exponent qubits, then measure them.
6. Classical: continued fractions turn the measured `y` into the order `r`; `gcd(a^(r/2) ± 1, N)`
   gives the factors.

| N | base a | exponent qubits | cycles | qubits | gates | P(factors) per shot | same for a random y |
|---|---|---|---|---|---|---|---|
| 15 = 3 × 5 | 7 | 8 | 4–101 | 6 530 | 5.2 million | 0.750 | 0.059 |
| 21 = 3 × 7 | 2 | 9 | 4–113 | 7 434 | 6.3 million | 0.623 | 0.045 |
| 33 = 3 × 11 | 5 | 11 | 4–137 | 9 418 | 9.0 million | 0.679 | 0.018 |
| 35 = 5 × 7 | 2 | 11 | 4–137 | 9 418 | 9.0 million | 0.706 | 0.011 |

Without `--a` the tool tries the bases 2, 3, 4, ... as the classical part of Shor's algorithm does:
for N = 33, bases 2 and 4 find the order but give no factor, and base 5 gives 3 × 11.
`--cc gcc` and `-O 2` work as well; `-O0` does not, because it keeps locals in memory.

**What is checked on every run.** The CPU runs `modexp` for every exponent of the domain (correct value,
halts, inside the W-bit model). After U, every branch of the superposition holds the right value and
has halted. After U⁻¹, the exponent is back and every work qubit is in its initial state. The exact
outcome distribution equals the textbook distribution of order finding, computed independently with a
classical FFT. With `--no-uncompute` (no step 4) the outcome is uniform and the success rate drops to
the chance level in the last column: the history must be uncomputed for the branches to interfere.

**What this shows, and what it does not.**

- The oracle is the general square-and-multiply routine, compiled by an ordinary C compiler. Nothing
  in it uses the order or the factors; the same binary serves every `a` and `N` that fit the register
  width.
- The numbers are tiny and everything is simulated (`qcpu/sparsesim.py`, exact; the circuit is far
  too large for Qiskit Aer). The simulator is checked against Qiskit's dense statevector on random
  circuits, and the QFT gate list against the discrete Fourier transform (`tests/test_shor.py`).
- It is not an efficient way to factor. A circuit built by hand for modular exponentiation needs
  about a dozen qubits for N = 15; the interpreter needs 6 530, almost all of them history. The speed-up
  belongs to Shor's algorithm. What the machine adds is that the classical part comes straight from
  compiled code.

## How printf works

All computation, including every value that gets printed, happens on the QPU. Text formatting
happens on the host:

1. The linker step in `arm_qpu.py` binds `printf`, `puts` and `putchar` to a small output stub,
   about 20 A64 instructions that run on the QPU like any other code.
2. Each call appends a record to an **output log** in data memory: the kind of call, the format
   string's address and the integer or pointer arguments.
3. After the run, the log is read out of the measured data memory and formatted on the host
   with C printf rules. The format strings come from the program's `.rodata`.
4. The CPU reference runs the same stub, so the two outputs are compared; `-v` shows the result.

This is how a real computer works too: the CPU writes to an output device, and the text is
drawn elsewhere. On a quantum computer it also keeps the circuit coherent to the end, because
everything is measured once, after the run.

Limits: integers and pointers only (no `%f`); up to 6 arguments per call; `%s` works for
string constants, not for text built in memory at run time; 32 output calls per run by
default (`output_records`). Return values: `putchar` returns its character and `puts`
returns 0, as in C. `printf` returns 0, not the number of characters written, because the
text is only formatted after the run. `printf("text\n")` is often turned into `puts` by the compiler,
which is supported.

## Compiling C to an ARM64 binary: clang or gcc

Both compilers are supported and tested; they produce the same results. Compile with `-c` to
an object file (`.o`) and pass that file to `arm_qpu.py`. Recommended flags:
`-O1 -fno-stack-protector -Iinclude`. Ubuntu's gcc enables a stack protector by default, which
needs a libc symbol.

| compiler | command | output |
|---|---|---|
| clang, any OS (cross-compiles, no ARM machine needed) | `clang --target=aarch64-linux-gnu -O1 -fno-stack-protector -Iinclude -c prog.c -o prog.o` | ELF |
| clang on macOS, Apple Silicon | `clang -O1 -fno-stack-protector -Iinclude -c prog.c -o prog.o` | Mach-O |
| gcc, Linux cross toolchain | `aarch64-linux-gnu-gcc -O1 -fno-stack-protector -Iinclude -c prog.c -o prog.o` | ELF |
| gcc, Arm GNU Toolchain (Windows, macOS, Linux) | `aarch64-none-elf-gcc -O1 -fno-stack-protector -Iinclude -c prog.c -o prog.o` | ELF |
| gcc on a native ARM64 Linux machine (Raspberry Pi, Graviton, ...) | `gcc -O1 -fno-stack-protector -Iinclude -c prog.c -o prog.o` | ELF |

Programs use no libc beyond `printf`/`puts`/`putchar`; there is no `malloc` or file I/O.

## Step-by-step: Linux (Ubuntu / Debian)

```bash
# 1. tools: Python, git, and a compiler (clang, or gcc for AArch64, or both)
sudo apt update
sudo apt install -y python3 python3-venv git clang llvm gcc-aarch64-linux-gnu

# 2. get the code and its Python packages
git clone https://github.com/quantag/arm-qpu.git
cd arm-qpu
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. compile C to an ARM64 binary: with clang ...
clang --target=aarch64-linux-gnu -O1 -fno-stack-protector -Iinclude -c examples/hello.c -o hello.o
#    ... or with gcc
aarch64-linux-gnu-gcc -O1 -fno-stack-protector -Iinclude -c examples/hello.c -o hello.o

# 4. (optional) look at the ARM machine code you are about to run
llvm-objdump -d hello.o

# 5. run the binary on the quantum machine
python3 arm_qpu.py hello.o

# 6. (optional) CPU/QPU comparison, resources, report
python3 arm_qpu.py hello.o -v
```

On Fedora: `sudo dnf install clang llvm gcc-aarch64-linux-gnu python3 git`. On Arch Linux:
`sudo pacman -S clang llvm aarch64-linux-gnu-gcc python git`.

## Step-by-step: Windows

### Option A: native Windows (PowerShell)

```powershell
# 1. Python, git and clang/LLVM (clang includes the AArch64 cross compiler)
winget install -e --id Python.Python.3.12
winget install -e --id Git.Git
winget install -e --id LLVM.LLVM
# the LLVM installer does not add itself to PATH: add it (current window + permanently)
$env:Path += ";C:\Program Files\LLVM\bin"
[Environment]::SetEnvironmentVariable("Path", [Environment]::GetEnvironmentVariable("Path", "User") + ";C:\Program Files\LLVM\bin", "User")
clang --version            # must print a version; if not, open a NEW PowerShell window

# 2. get the code and its Python packages
git clone https://github.com/quantag/arm-qpu.git
cd arm-qpu
py -m venv .venv
.venv\Scripts\Activate.ps1        # if blocked: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
pip install -r requirements.txt

# 3. compile C to an ARM64 binary with clang
clang --target=aarch64-linux-gnu -O1 -fno-stack-protector -Iinclude -c examples\hello.c -o hello.o

# 4. (optional) look at the ARM machine code
llvm-objdump -d hello.o

# 5. run the binary on the quantum machine
python arm_qpu.py hello.o

# 6. (optional) CPU/QPU comparison, resources, report
python arm_qpu.py hello.o -v
```

**gcc on Windows.** Use the Arm GNU Toolchain:

1. Download the Windows-hosted (mingw-w64) package for the target "AArch64 bare-metal
   (aarch64-none-elf)" from https://developer.arm.com/downloads/-/arm-gnu-toolchain-downloads.
2. Unzip it, for example to `C:\arm-gnu-toolchain`.
3. Add its `bin` folder to PATH, then check: `aarch64-none-elf-gcc --version`.
4. Compile with gcc instead of clang (step 3 above), then run the binary as before:

```powershell
aarch64-none-elf-gcc -O1 -fno-stack-protector -Iinclude -c examples\hello.c -o hello.o
python arm_qpu.py hello.o
```

To run a single function with a pointer argument, quote it: `'&0'` in PowerShell, `"&0"` in
`cmd.exe`.

### Option B: WSL2 (Linux inside Windows)

```powershell
wsl --install -d Ubuntu      # once, then reboot and open "Ubuntu" from the Start menu
```

Inside Ubuntu, follow the Linux steps above. This gives you both compilers through `apt`.

## Step-by-step: macOS (Apple Silicon)

```bash
# 1. tools: Apple clang compiles natively for arm64
xcode-select --install

# 2. code and Python packages
git clone https://github.com/quantag/arm-qpu.git && cd arm-qpu
python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt

# 3. compile C to an ARM64 binary (a native arm64 Mach-O object)
clang -O1 -fno-stack-protector -Iinclude -c examples/hello.c -o hello.o
#    or with gcc:  brew install aarch64-elf-gcc
#                  aarch64-elf-gcc -O1 -fno-stack-protector -Iinclude -c examples/hello.c -o hello.o

# 4. run the binary on the quantum machine
python3 arm_qpu.py hello.o
```

## config.json

```json
{
  "backend": "simulator",
  "register_width": "auto",
  "registers": "auto",
  "memory_words": "auto",
  "max_steps": "auto",
  "isa": "full",
  "compiler": "auto",
  "cflags": ["-O1", "-fno-stack-protector"],
  "output_records": 32,
  "stack_bytes": 256,
  "shots": 1024,
  "aer": {"method": "matrix_product_state"},
  "ibm": {"channel": "ibm_quantum_platform", "token": "env:IBM_QUANTUM_TOKEN",
          "instance": null, "backend": "least_busy", "optimization_level": 1}
}
```

| key | meaning |
|---|---|
| `backend` | `simulator`: exact simulation of the circuit on its basis-state input (fast, any size). `aer`: Qiskit Aer, sampled with `shots`. `ibm`: IBM Quantum hardware through Qiskit Runtime (`token` may be `env:VAR`). |
| `register_width` | W, bits per register (8 to 16). `auto` = smallest W that holds the code and data addresses. |
| `registers` | `auto` = only registers the code uses; `all` = X0..X30; or a list. |
| `memory_words` | data memory in 4-byte cells; `auto` = enough for the output log, data sections, user cells and stack. |
| `max_steps` | machine cycles in the circuit; `auto` = what the CPU reference needed. |
| `isa` | `full` = decoder implements every supported class; `auto` = only the classes the program uses (smaller circuit). |
| `compiler` | for `.c` input: `auto`, `clang`, `gcc`, or a command name / path. |
| `cflags` | compiler flags for `.c` input, default `["-O1", "-fno-stack-protector"]`. |
| `output_records` | how many printf/puts/putchar calls the output log keeps (default 32). |
| `stack_bytes` | stack reserved at the top of data memory (default 256). |

Before submitting to hardware the runner checks the circuit's width against the device. A run
that does not fit is reported as not submitted, with the reason.

## What is implemented

| group | instructions |
|---|---|
| move | MOVZ, MOVN, MOVK, MOV (all aliases) |
| arithmetic | ADD, ADDS, SUB, SUBS (immediate, shifted register, extended register), CMP, CMN, NEG |
| logic | AND, ORR, EOR, ANDS, BIC, ORN, EON, BICS (register and bitmask immediate), TST, MVN |
| shifts / bitfields | LSL, LSR, ASR (immediate and register), UBFM, SBFM, UBFX, SBFX, UBFIZ, SBFIZ, UXTB/H, SXTB/H/W |
| multiply / divide | MUL, MADD, MSUB, MNEG, SMULL, UMULL, SMADDL, UMADDL, SDIV, UDIV |
| conditional | CSEL, CSINC, CSINV, CSNEG, CSET, CSETM, CINC, CNEG, CCMP, CCMN |
| branches | B, B.cond, CBZ, CBNZ, TBZ, TBNZ, BL, BLR, BR, RET |
| memory | LDR, STR, LDRSW (unsigned offset, unscaled LDUR/STUR, pre-index, post-index, register offset with LSL/UXTW/SXTW), LDP, STP (offset, pre-, post-index) |
| addresses | ADRP + ADD :lo12: (addresses of string literals and globals) |
| output | `printf`, `puts`, `putchar` (see "How printf works") |
| other | NOP and hints; HLT ends the run |

Anything else (byte/halfword loads and stores, ROR, BFI/BFXIL, CLZ/REV, ADR, floating point,
SIMD, system instructions) is listed in the report's "Unsupported instructions" table. A run that
reaches one stops with a trap; the CPU reference reports it first.

## The W-bit model, and when results are exact

The quantum registers are W bits wide. A W-bit register stands for the 64-bit (or 32-bit)
value you get by sign-extending it. When every value of a run has that form, the quantum
machine reproduces the AArch64 result exactly: the low W bits of every register and memory
cell, the NZCV flags, and the control flow.

The CPU reference checks every value of the run and lists any that does not fit in the
report's "Model check" section. Example: clang at `-O1` (and gcc at every level) compiles
`n / 10` as a multiply by `0xCCCCCCCD` and a 35-bit shift. Those values cannot fit in W ≤ 16,
so the report flags them. With clang at `-O0` the same function is a real `sdiv` and matches.

Code addresses are `4 × slot` and data pointers are offsets into data memory. Both must be
non-negative W-bit values, which is why `register_width: auto` grows W with program size.

## Tests

```bash
python3 -m tests.test_c_programs clang   # C cases x -O0/-O1/-O2 + whole programs with printf
python3 -m tests.test_c_programs gcc     # the same with aarch64-linux-gnu-gcc
python3 -m tests.test_fuzz 300 7 8,12,16 # random A64 programs at three register widths
python3 -m tests.test_edges 8,12,16      # boundary cases: extensions, flags, shifts, W/X forms, memory, mul/div
python3 -m tests.test_simulators         # fast replay == fully materialised circuit == Qiskit Aer
python3 -m tests.test_coherence          # a superposition of two ARM programs, and interference
python3 -m tests.test_branching          # a superposed input taking two paths of different length
python3 -m tests.test_grover             # Grover search with compiled C predicates as oracles
python3 -m tests.test_shor               # Shor's algorithm with modexp compiled from C
python3 tools/compare_specialized.py     # interpreter vs a specialised reversible circuit
python3 tools/test_report.py             # runs everything, writes docs/test_results.md
```

Every comparison with the CPU is done after **every machine cycle** (registers, flags, data memory,
PC), not only at the end. Each run falls into one class: *match*; *match-flagged* (it matched, but
the model check reported values outside the W-bit model); *flagged* (it differed and the model check
reported why); or *unflagged mismatch* (it differed and the check said nothing). An unflagged
mismatch would be a bug, and the tests require that there are none. `docs/test_results.md` lists
seeds, tool versions, the commit and the number of discarded runs.

The largest C case (`bubble_sort` at `-O0`, about 800 000 qubits and 180 million gates) needs
several GB of RAM. Run the two C suites one after the other, not in parallel.

## How the quantum machine works

One machine cycle is a reversible block:

1. **Fetch.** IR ^= MEM[PC], with each program slot selected by a comparator on the PC qubits.
2. **Decode.** One-hot class flags from IR bit patterns, all gated by a `running` qubit.
3. **Execute.** Register-file reads are multiplexers on the Rn/Rm/Rt fields. Then the ALU:
   ripple adder with flags, barrel shifter, shift-add multiplier, restoring divider, logical
   bitmask decoder, condition evaluation, and address generation.
4. **Copy out, then uncompute.** The cycle's results are copied into fresh per-step
   registers, and the whole compute block runs backwards, so every scratch qubit returns to |0⟩.
5. **Commit.** Controlled-SWAPs move the results into registers, flags, memory and PC. The
   displaced old values stay behind as history, which makes overwrites and jumps unitary
   (Landauer/Bennett embedding). The PC history doubles as an execution trace.

After `HLT` (the return address the entry function starts with), the remaining cycles are
no-ops. That is the synchronisation the Quantum Control Machine paper requires.

Every cycle is the same gate list; only its per-cycle registers (results, history, next PC)
are new qubits. The simulator therefore generates one cycle once and replays it, resetting
the per-cycle qubits and feeding the previous cycle's PC in. That is the same computation as
the full circuit, without holding hundreds of millions of gates in memory. The reported qubit
and gate counts are those of the full circuit.

Code: `qcpu/machine.py` (circuit), `qcpu/a64.py` (instruction classes and static check),
`qcpu/reference.py` (CPU reference and model check), `qcpu/loader.py` (ELF/Mach-O loader and
linker, output stub), `qcpu/console.py` (output log to text), `qcpu/bitsim.py` (exact
simulator), `arm_qpu.py` (CLI and reports), `include/stdio.h`.

## Honest status

* Simulation: verified as above.
* Hardware: the backend is implemented, but the circuits are far larger than today's devices.
  The smallest real program (`subs; cneg; ret`, `isa: auto`) needs 666 qubits and about 9 000
  Toffoli gates; `hello.c` needs about 500 000 qubits and 56 million Toffoli gates. Running on a QPU means a much smaller
  machine profile, or fault-tolerant hardware.
* There is no speed-up: a classical program on a QPU is a very expensive reversible computer.
  The value is a verified bridge from ordinary binaries to quantum circuits: reversible oracles,
  resource estimation, and a quantum control-flow test bed. See `docs/prior_work.md` and the
  QA64 roadmap there.

## Prior work

* Mariantoni et al., *Implementing the quantum von Neumann architecture with superconducting circuits*, Science 2011: quantum CPU and memory, instructions classical.
* Fitzjohn et al., Qx86 (Intel 8080/Z80 on IBM Q), IEEE TQE 2022 / QIP 2024: the PC and fetch/decode are classical; each instruction runs as a separate quantum job.
* Yuan, Villanyi, Carbin, *Quantum Control Machine*, OOPSLA 2024: limits of control flow in superposition (injectivity, synchronisation).
* Zhang, Ying, *Quantum Register Machine*, PLDI 2025: an ISA with a quantum PC, qif/fiq and recursion; no public implementation.
