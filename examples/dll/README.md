# QPU libraries: call functions on the QPU like a DLL

A host program on the PC calls functions of a library as ordinary C functions; the library's
functions run on the quantum machine. The host code does not mention the QPU.

```
main.c (host, x86-64 Linux)          libmathlib_qpu.so (generated)        qpu_server.py
---------------------------          -----------------------------        -------------
printf("%d", gcd(84, 36));   ──>     gcd(a, b): send the call   ──────>   run gcd on the quantum
                                                                          machine (AArch64, RISC-V
                                                                          or x86-64 code), check it
                             <──     return 12                  <──────   against the CPU
```

## Files

| file | what it is |
|---|---|
| `mathlib.h` | the library interface, shared by host and QPU |
| `mathlib.c` | the library: ordinary C, compiled for the QPU |
| `main.c` | the host program: ordinary C, unchanged whether mathlib runs on the QPU or the CPU |
| `Makefile` | builds both versions |

## Build and run

```bash
make run                     # QPU code for AArch64
make clean; make ARCH=riscv64 run      # or riscv32, x86_64
```

By hand:

```bash
python3 ../../qpu_dll.py mathlib.c --header mathlib.h -o libmathlib_qpu.so
cc -I ../../runtime main.c -L. -lmathlib_qpu -Wl,-rpath,'$ORIGIN' -o main_qpu
./main_qpu

cc -I ../../runtime main.c mathlib.c -o main_cpu       # the same program, mathlib on the CPU
./main_cpu
```

Both print the same:

```
gcd(84, 36)       = 12
modexp(7, 4, 15)  = 1
primes below 30  : 2 3 5 7 11 13 17 19 23 29
sum               = 18
sorted            = -3 0 1 4 7 9
squares           = 0 1 4 9 16 25 36 49
  (printed on the QPU: x = 21)
report(21)        = 42
```

The first call starts the QPU server (`qpu_server.py`) in the background; it stops itself after 10
minutes without calls. Each call takes 0.1 to 3 seconds in the simulator.

## Windows

The same, with a `.dll`. You need Python with this repository's requirements (as for `cpu_qpu.py`,
see the main README, "Step-by-step: Windows"), clang for the QPU code, and a C compiler for the host:
clang, MinGW gcc, or MSVC `cl`.

```powershell
python ..\..\qpu_dll.py mathlib.c --header mathlib.h -o mathlib_qpu.dll    # also writes mathlib_qpu.lib
clang -I ..\..\runtime main.c mathlib_qpu.lib -o main.exe                   # or: gcc ... mathlib_qpu.dll
                                                                           # or: cl /I ..\..\runtime main.c mathlib_qpu.lib
.\main.exe
```

`qpu_dll.py` picks the first of `clang`, `gcc`, `cl` it finds; `--host-cc` chooses one. Keep
`mathlib_qpu.dll` next to `main.exe` (or on `PATH`). On Windows the library talks to the server over
TCP on 127.0.0.1: the server writes its port and a random token to
`%LOCALAPPDATA%\cpu-qpu\qpu-server.txt`, and a connection without the token is refused. The server
is started with `python`, else `py -3`; set `QPU_PYTHON` if your Python has another name (the
Microsoft Store placeholder `python` does not work).

From Linux, MinGW builds the Windows files:

```bash
python3 ../../qpu_dll.py mathlib.c --header mathlib.h --target windows -o mathlib_qpu.dll
x86_64-w64-mingw32-gcc -I ../../runtime main.c mathlib_qpu.lib -o main.exe
```

On the Windows machine, set `QPU_HOME` to the folder of `qpu_server.py` there (or give
`--qpu-home` when building).

## Writing a library header

```c
#include "qpu_dll.h"

int gcd(int a, int b);                       // integers in, integer out
int sum(const int *QPU_IN(n) v, int n);      // buffer copied to the QPU
void squares(int *QPU_OUT(n) v, int n);      // buffer copied back to the host
void sort(int *QPU_INOUT(n) v, int n);       // both
```

- Parameters and return values: integer types (`char` to `long long`, `intN_t`) or `void`.
- Pointers: only to `int` buffers, and only with `QPU_IN`, `QPU_OUT` or `QPU_INOUT`. The count is
  a C expression over the other parameters (`n`, `2*n`, `16`). The QPU cannot read host memory, so
  the buffer is copied. The macros expand to nothing, so the header compiles unchanged on both sides.
- No structs, floating point, or callbacks into the host. Nothing is kept between calls.
- `printf`, `puts` and `putchar` inside the library work; the text appears on the host's stdout
  when the call returns.

## Values and the register width

The machine has W-bit registers, W = 16 by default (`--width` when generating, or `QPU_WIDTH` at run
time). Arguments, results and intermediate values must fit; a run where one does not is detected by
the model check (the same check as in `cpu_qpu.py -v`), and the call prints a warning:

```
qpu: mathlib: twice: warning: result may be wrong (outside the 10-bit model): ...
```

The program goes on with the QPU's value. With `QPU_STRICT=1` it stops instead (exit status 70).
A call that cannot run (unsupported instruction, no return within the step limit) prints a message
and exits with status 70. After a call, `int qpu_last_status(void)` (0 exact, 1 not exact) and
`const char *qpu_last_message(void)` give the details, if you declare and call them.

Known limits are the same as for `cpu_qpu.py` (README, "Known limits"). One that appears easily:
on RV64 at `-O1`, clang turns an `int` array index into `slli 32 / srli 32`, which the model flags.
`mathlib.c` writes `sort` with pointers for that reason; with indices, `sort` on RV64 prints a
warning and returns wrong values.

## Environment

| variable | meaning |
|---|---|
| `QPU_BACKEND` | `sim` (default): the quantum machine, checked against the CPU. `cpu`: the CPU reference only, fast. |
| `QPU_WIDTH` | register width 8..16 (default: `--width`, 16) |
| `QPU_STRICT` | `1`: stop when a result is not exact |
| `QPU_QUIET` | `1`: no warnings |
| `QPU_SERVER` | where the server listens: a Unix socket path, or an endpoint file ending in `.txt` for TCP. Default: `/tmp/cpu-qpu-<uid>.sock` (Linux), `%LOCALAPPDATA%\cpu-qpu\qpu-server.txt` (Windows). The server log is `<that>.log`. |
| `QPU_HOME` | folder with `qpu_server.py` (default: the repository the library was generated from) |
| `QPU_PYTHON` | Python for the server (default `python3` on Linux; `python`, then `py -3` on Windows) |

## What this is, and what it is not

It is the classical interface to the machine: one call, one run, one measured result. A function
called like this computes the same thing as on the CPU, only much more slowly; there is no quantum
speed-up in this mode. It is the base for quantum calls (a function run on all inputs at once, as
`grover.py` and `shor.py` do), which need superposition and interference around the call and are not
part of this interface yet.

The generated library contains the QPU object file, so it can be copied on its own; it needs this
repository on the machine for the server.

Status of the Windows version: the DLL, its import library and a host program are built with MinGW
and checked (exports, imports) by `tests/test_dll.py`, and the TCP transport it uses is tested end to
end on Linux. It has not yet been run on a Windows machine.
