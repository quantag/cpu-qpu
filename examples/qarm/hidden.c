// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 Quantag IT Solutions GmbH
//
// Hidden structure in ordinary C functions: QSIMON finds the hidden XOR shift s of a two-to-one function
// (f(x) = f(x ^ s)), QBV the hidden string s of a parity function (f(x) = s.x mod 2) with one query. The oracles come
// from the functions' machine code (docs/qarm_v2.md, section 3); a plain CPU solves both classically.
//
//   clang -O1 -iquote include examples/qarm/hidden.c -o hidden
//   ./hidden  |  python3 qarm_exec.py hidden  |  qemu-aarch64 -plugin libqarm-qemu.so ./hidden
#include <stdio.h>
#include "qarm.h"

#define SHIFT 0x2D        // the secrets: 101101 and 110101 (6 bits)
#define STRING 0x35

static int two_to_one(int x)                     // the smaller of x and x ^ SHIFT: f(x) = f(x ^ SHIFT)
{
    int y = x ^ SHIFT;
    return x < y ? x : y;
}

static int parity(int x)                         // popcount(x & STRING) mod 2
{
    int p = 0;
    for (int v = x & STRING; v; v &= v - 1)
        p ^= 1;
    return p;
}

int main(void)
{
    int f = qarm_features(), s;
    if (qarm_simon(two_to_one, 6, &s))
        printf("Simon: the hidden shift is %#x (%s)\n", s, f & QARM_F_SIMON ? "QSIMON" : "classical");
    else
        printf(f & QARM_F_SIMON ? "Simon: no hidden shift found\n" : "Simon: no hidden shift\n");
    qarm_bv(parity, 6, &s);
    printf("Bernstein-Vazirani: the hidden string is %#x (%s)\n", s, f & QARM_F_BV ? "QBV, one query" : "classical");
    return 0;
}
