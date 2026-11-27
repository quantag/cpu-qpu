// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 Quantag IT Solutions GmbH
//
// Estimating pi with QAMPEST, amplitude estimation, the quantum replacement for Monte Carlo sampling: the share of
// points of a 64 x 64 grid inside a quarter circle is the mean of an ordinary C function over its 4096 inputs. The
// quantum coprocessor builds the function's oracle from its machine code and estimates the mean with about
// sqrt(4096) queries (docs/qarm_v2.md, section 3). A plain CPU computes the mean exactly.
//
//   clang -O1 -iquote include examples/qarm/pi.c -o pi
//   ./pi  |  python3 qarm_exec.py pi  |  qemu-aarch64 -plugin libqarm-qemu.so ./pi
#include <stdio.h>
#include "qarm.h"

static int inside(int x)                         // x = 12 bits: i (low 6), j (high 6); 1 if (i, j) is in the circle
{
    int i = x & 63, j = x >> 6 & 63;
    return 4 * (i * i + j * j) + 4 * (i + j) + 2 < 4 * 64 * 64;     // the cell's centre (i + 1/2, j + 1/2)
}

int main(void)
{
    double err;
    double share = qarm_mean(inside, 12, 1, &err);
    if (err > 0)
        printf("QAMPEST: pi is about %.4f +- %.4f (amplitude estimation)\n", 4 * share, 4 * err);
    else
        printf("the grid: pi is about %.4f (every point counted on this CPU)\n", 4 * share);
    return 0;
}
