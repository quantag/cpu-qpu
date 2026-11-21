# QPU libraries: call functions on the quantum machine like a DLL

A host program calls functions of a library as ordinary C functions; the functions run on the
quantum machine, which is inside the library. At run time there is only your program and the
library: no Python, no server, no network.

```
main.c (host program)                 libmathlib_qpu.so / mathlib_qpu.dll
---------------------                 ------------------------------------
printf("%d", gcd(84, 36));   ──>      gcd(a, b):
                                        set the machine's qubits: program, X0 = 84, X1 = 36, PC = gcd
                                        run the cycle's gates until the machine halts
                                        read the result qubits: X0 = 12
                                        check: the host build of gcd also gives 12
                             <──      return 12
```

## How the library is made

`qpu_dll.py` (Python, at build time only, like a compiler):

1. compiles the library for the QPU: AArch64, RISC-V or x86-64 code;
2. builds the quantum machine's circuit for that code. Every machine cycle is the same list of
   gates (X, CNOT, Toffoli, controlled SWAP), so the circuit is that list plus the initial state
   (the program in program memory, the data, the return address) and the qubits that hold the
   arguments, the result, data memory and the PC;
3. writes all of it into a C file as data, together with the engine (`runtime/qpu_engine.c`, a
   short loop that applies the gates) and a wrapper for each exported function;
4. compiles that C file, and the library source once more for the host (to check each call), into
   a shared library.

The engine runs the same gates in the same order as the Python simulator; `tests/test_dll.py`
compares the two on every qubit.

## Files

| file | what it is |
|---|---|
| `mathlib.h` | the library interface, shared by host and QPU |
| `mathlib.c` | the library: ordinary C |
| `main.c` | the host program: ordinary C, the same whether mathlib runs on the QPU or the CPU |
| `Makefile` | builds both versions |

## Build and run: Linux

```bash
make run                           # QPU code for AArch64
make clean; make ARCH=x86_64 run   # or riscv64, riscv32
```

By hand:

```bash
python3 ../../qpu_dll.py mathlib.c --header mathlib.h -o libmathlib_qpu.so
cc -I ../../runtime main.c -L. -lmathlib_qpu -Wl,-rpath,'$ORIGIN' -o main_qpu
./main_qpu

cc -I ../../runtime main.c mathlib.c -o main_cpu       # the same program, mathlib on the CPU
./main_cpu
```

Both print:

```
gcd(84, 36)       = 12
modexp(7, 4, 15)  = 1
primes below 30  : 2 3 5 7 11 13 17 19 23 29
sum               = 18
sorted            = -3 0 1 4 7 9
squares           = 0 1 4 9 16 25 36 49
```

`main_qpu` makes 35 calls to the quantum machine and takes about 0.2 seconds. The library is about
1.8 MB: for AArch64 code, 148,000 gates per cycle on 25,504 qubits.

## Build and run: Windows

The same, with a `.dll`. Building needs Python with this repository's requirements and clang (as for
`cpu_qpu.py`, see the main README), and a C compiler for the library: clang, MinGW gcc or MSVC `cl`.
Running needs only `main.exe` and `mathlib_qpu.dll`.

```powershell
python ..\..\qpu_dll.py mathlib.c --header mathlib.h -o mathlib_qpu.dll    # also writes mathlib_qpu.lib
clang -I ..\..\runtime main.c mathlib_qpu.lib -o main.exe                   # or: gcc ... mathlib_qpu.dll
                                                                           # or: cl /I ..\..\runtime main.c mathlib_qpu.lib
.\main.exe
```

From Linux, MinGW builds the Windows files:

```bash
python3 ../../qpu_dll.py mathlib.c --header mathlib.h --target windows -o mathlib_qpu.dll
x86_64-w64-mingw32-gcc -I ../../runtime main.c mathlib_qpu.lib -o main.exe
```

The DLL depends only on `KERNEL32.dll` and the C runtime.

## Writing a library header

```c
#include "qpu_dll.h"

int gcd(int a, int b);                       // integers in, integer out
int sum(const int *QPU_IN(n) v, int n);      // buffer copied into the machine's data memory
void squares(int *QPU_OUT(n) v, int n);      // buffer copied back to the host
void sort(int *QPU_INOUT(n) v, int n);       // both
```

- Parameters and return values: integer types (`char` to `long long`, `intN_t`) or `void`.
- Pointers: only to `int` buffers, and only with `QPU_IN`, `QPU_OUT` or `QPU_INOUT`. The count is a
  C expression over the other parameters (`n`, `2*n`, `16`). The machine cannot read host memory,
  so the buffer is copied. The macros expand to nothing, so the header compiles unchanged on both sides.
- The library cannot print (`printf`, `puts`, `putchar`): building stops with an error.
- No structs, floating point, or callbacks into the host. Nothing is kept between calls.

## Values, checks and errors

The machine has W-bit registers: W = 16 by default (`--width`). Data memory holds 512 ints by default
(`--memory`); buffers, data and stack share it, and buffers that do not fit stop the call.

Each call is checked against the same function compiled for the host: the return value and the
output buffers must be equal, and arguments must fit W bits. Otherwise the call prints a warning
and returns the machine's value:

```
qpu: edge: twice: warning: result may be wrong (12-bit machine): return value differs from the host build
```

With `QPU_STRICT=1` it stops instead (exit status 70). A call that cannot complete, because the
machine reached an instruction it does not implement or did not return within `QPU_MAX_STEPS`
cycles, prints a message and exits with status 70. `qpu_dll.py` warns at build time about
instructions the machine does not implement. After a call, `int qpu_last_status(void)` (0 exact,
1 not exact) and `const char *qpu_last_message(void)` give the details, if you declare and call them.
A library built from an object file instead of C source has no host build, so its calls are not
checked.

Known limits are those of `cpu_qpu.py` (main README, "Known limits"). One appears easily: on RV64 at
`-O1`, clang turns an `int` array index into `slli 32 / srli 32`, which the W-bit model cannot hold.
`mathlib.c` writes `sort` with pointers for that reason; with indices, `sort` on RV64 is reported as
not exact.

## Environment

| variable | meaning |
|---|---|
| `QPU_VERIFY` | `1` (default): check each call against the host build. `0`: do not. |
| `QPU_BACKEND` | `qpu` (default): the quantum machine. `native`: only the host build, for speed. |
| `QPU_STRICT` | `1`: stop when a result is not exact |
| `QPU_QUIET` | `1`: no warnings |
| `QPU_MAX_STEPS` | machine cycles before a call is stopped (default: `--max-steps`, 1,000,000) |

## What this is, and what it is not

The quantum machine here runs on a classical basis state, one call at a time, exactly as the
simulator does: a call computes what the CPU computes, much more slowly, with no quantum speed-up.
The library carries the complete circuit, so the same file is what a quantum backend would execute;
quantum calls (a function run on all inputs at once, as `grover.py` and `shor.py` do) are not part of
this interface yet.

Status of the Windows version: the DLL, its import library and a host program are built with MinGW,
and their exports and imports are checked by `tests/test_dll.py`; the engine is plain C with no
system calls except a lock. It has not yet been run on a Windows machine.
