# QArmAuto: quantum speedups for loops you already wrote

QArmAuto is an LLVM pass plugin. It finds four kinds of loop in ordinary C and gives them to the quantum
coprocessor: a search (QSEARCH, Grover's search), a minimum (QMIN, Dürr–Høyer), a count (QCOUNT, quantum
counting) and a sum or average (QAMPEST, amplitude estimation). The source needs no quantum calls and no
`qarm.h`. The only change is one mark on the function:

```c
#include "qarm_auto.h"

QARM_AUTO int find_code(void)
{
    for (int code = 0; code < 256; code++)
        if (consistent(code)) return code;          /* a search: QSEARCH */
    return -1;
}

QARM_AUTO int best_split(int *diff)
{
    int best = 1 << 30, mask = 0;
    for (int m = 0; m < 256; m++) {
        int d = imbalance(m);
        if (d < best) { best = d; mask = m; }       /* a minimum: QMIN */
    }
    *diff = best;
    return mask;
}
```

```
$ qarm-cc --auto --remarks examples/qarm/auto.c -o auto
auto.c:47:5: remark: a search over consistent(x), x < 256: QSEARCH (any solution; ...)
auto.c:62:5: remark: a minimum over imbalance(x), x < 256: QMIN (a minimizer with high probability; ...)
$ ./auto                      # a plain CPU: the program as written
$ qarm-run ./auto             # Q-ARM: [qarm: QSEARCH consistent ...], [qarm: QMIN imbalance ...]
```

Counts and sums come back as estimates, so they have their own mark, `QARM_ESTIMATE`
([examples/qarm/estimate.c](../examples/qarm/estimate.c)):

```c
QARM_ESTIMATE long codes_left(void)
{
    long n = 0;
    for (int code = 0; code < 256; code++)
        if (fits(code)) n++;                        /* a count: QCOUNT */
    return n;
}

QARM_ESTIMATE double mean_imbalance(void)          /* imbalance() returns an unsigned char */
{
    double s = 0;
    for (int m = 0; m < 256; m++) s += imbalance(m); /* a sum: QAMPEST */
    return s / 256;
}
```

```
$ qarm-cc --auto examples/qarm/estimate.c -o estimate
$ ./estimate                  # a plain CPU: exact
codes that fit RRGG -> black 1 white 1: 56 (bound 0.00)
$ qarm-run ./estimate         # Q-ARM: [qarm: QCOUNT fits ...]
codes that fit RRGG -> black 1 white 1: 57 (bound 5.38)
```

`qarm_auto_error()` (in `qarm_auto.h`) returns the error bound of the last count or sum, and 0 when the loop ran
exactly.

Without `qarm-cc`, the command is
`clang -O1 -fpass-plugin=QArmAuto.so prog.c compiler/qarm_auto.c`. `compiler/qarm_auto.c` is the small
runtime side, and `qarm-cc --auto` adds it.

## What the mark allows

A plain loop promises more than the quantum instruction gives, so the rewrite is opt-in:

| loop | as written | after the rewrite, with a coprocessor |
|---|---|---|
| search | the **smallest** x with p(x), or none | **a** solution, or none. "None" still means none: the loop then searches in full |
| minimum | the least cost, at the **first** minimizer (`<`) or the last (`<=`) | the least cost **with high probability** (Dürr–Høyer, at least 1/2 per run), at **some** minimizer; the returned argument's cost is checked |
| count | the exact number of x with cond(x) | an **estimate** M̃, \|M̃ − M\| ≤ `qarm_auto_error()` with probability ≥ 8/π² |
| sum | the exact sum of v(x) | an **estimate**, within `qarm_auto_error()` with probability ≥ 8/π²; an integer sum is rounded, so allow half a unit more |

`QARM_AUTO` on a function accepts the first two rows for its loops; `QARM_ESTIMATE` accepts all four.
`-mllvm -qarm-auto=all`, or `qarm-cc --auto-all`, accepts searches and minimums for every eligible loop in the
file; `-qarm-auto=all-estimate` (`qarm-cc --auto-estimate`) accepts all four. Nothing can check an estimate
the way a search's answer is checked, so a count or sum in a `QARM_AUTO` function is only reported, as a
candidate. Without a coprocessor, or when the coprocessor declines, each
loop runs exactly as it was compiled without the pass. `tests/test_qarm_auto.py` checks that the output is then
byte for byte the same.

## How the rewrite works

The pass runs early in the `-O1` pipeline, before inlining. Inlining and constant folding would otherwise hide
the loops; LLVM can fold a search over a constant table to its answer. At that point a loop is top-tested, and
the pass changes the loop's **start values** only:

- **Search.** The counter starts at QSEARCH's answer when it found one, and at 0 otherwise. The loop then runs
  as written. With an answer x, the loop tests p(x) and leaves at once through its own exit, so the code after
  it sees exactly what it would for a hit. With no answer, it searches all N candidates.
- **Minimum.** When QMIN answered, the counter starts at N, so no iteration runs. The best value and its
  argument start at QMIN's value and x. The loop's own rule on its first values still applies: they are
  replaced only when QMIN's value is below them (or not above, for `<=`). When QMIN did not answer, the loop
  starts at 0 and runs as written.

- **Count and sum.** When the coprocessor answered, the counter starts at N and the count or sum at its first
  value plus the estimate, so no iteration runs. When it did not, the loop starts at 0 and runs as written,
  and `qarm_auto_error()` is 0.

When N is not a power of two, the coprocessor gets a wrapper over 0 … 2^bits − 1. The wrapper is 0 or
`INT_MAX` above N, so no answer falls outside the loop, and nothing is counted or added there. For a count
the wrapper is the condition as 0 or 1, and for a sum the added value: the pass copies the casts, comparisons
and arithmetic with constants that the loop applies to the function's result. A predicate that returns a `bool` is widened to an int
the same way, since the ABI leaves a bool's upper bits undefined. The wrapper calls the loop's function with
`alwaysinline`, so the oracle builder gets one leaf function.

## Which loops

| condition | why |
|---|---|
| `for (x = 0; x < N; x++)`: an `int` counter from 0, step 1, a constant N from 2 to 65536 | QSEARCH and QMIN take 1 to 16 bits |
| search: the only value that changes between iterations is x; the loop leaves when p(x) is true | so that starting at a solution skips nothing that matters |
| minimum: the only values that change are x, the best cost and its argument, updated together when `cost(x) < best` (or `<=`), signed; as an if, a select, or `smin` | QMIN minimizes a signed int |
| p and cost are pure functions of x: they may read memory, and they write none but their own locals and call only pure functions defined in the file | the oracle is built from their machine code, and the loop may skip calls |
| count: the only values that change are x and n; `n++` (or `n = c ? n + 1 : n`) when a condition on f(x) holds, tested every iteration, or `n += b` for a bool b of f(x); n an integer of 32 bits or more | QCOUNT counts the x with f(x) = 1 |
| sum: the only values that change are x and s; `s += v` where v is f(x) through casts and constant arithmetic, of at most 16 bits that the compiler can see (an `unsigned char` or `unsigned short` result, a mask such as `& 0xff`, a bool); s an integer of 32 bits or more, a `float` or a `double` | QAMPEST estimates the mean of values in 0 … 2^k − 1; a sum of a bool is a count |
| no other side effects in the loop, and no value computed in it used after it, except x, the best and its argument (or the count or sum) | |

A signed `char`, an `int` result with no mask, or a count that the loop also reads (`if (n < 10 && ...)`) is
left alone: its values are not known to be in range, or the count changes what the loop does.

In a `QARM_AUTO` or `QARM_ESTIMATE` function, every other loop gets a `-Rpass-missed=qarm-auto` remark with the first reason it
was left alone. Outside one, `-Rpass-analysis=qarm-auto` reports each loop that would qualify.

At run time the oracle builder still decides. If the function's machine code is not a leaf function without
vector code (docs/qarm_isa.md 5.5), it declines. Then `__qarm_auto_*` returns 0 and the loop runs as written.

## Building the pass

The plugin must match the clang that loads it. `qarm-cc --auto` handles this for whichever clang it finds:
- **An installed build:** it uses `lib/qarm/QArmAuto-llvmN.so` when the installation has one;
- **Otherwise:** on first use it builds the plugin into `~/.cache/qarm` from `share/qarm/compiler`. That
  needs `llvm-N-dev` and cmake.

The container image has clang 18 with the pass built. To build it by hand:

```
cmake -S compiler -B build/auto -DLLVM_DIR=$(llvm-config --cmakedir) && cmake --build build/auto
```

GCC has no such plugin interface. With GCC, `QARM_AUTO` and `QARM_ESTIMATE` are empty, the loops run as
written, and `qarm_auto_error()` is 0.
