// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 Quantag IT Solutions GmbH
//
// Shor's factoring with QPERIOD: the order r of a mod N is the period of x -> a^x mod N, an ordinary C function;
// the quantum coprocessor builds its oracle from the function's machine code, finds the period with the quantum
// Fourier transform, and the program factors N from r classically (docs/qarm_v2.md, section 3). On a plain CPU the
// period is found by trying every r.
//
//   clang -O1 -iquote include examples/qarm/shor.c -o shor
//   ./shor  |  python3 qarm_exec.py shor  |  qemu-aarch64 -plugin libqarm-qemu.so ./shor
#include <stdio.h>
#include "qarm.h"

#define INLINE static inline __attribute__((always_inline))

INLINE unsigned modexp(unsigned a, unsigned x, unsigned n)
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

static int f15(int x) { return (int)modexp(7, (unsigned)x, 15); }    // order 4
static int f21(int x) { return (int)modexp(2, (unsigned)x, 21); }    // order 6

static unsigned gcd(unsigned a, unsigned b)
{
    while (b) {
        unsigned t = a % b;
        a = b;
        b = t;
    }
    return a;
}

static void factor(int (*f)(int), unsigned a, unsigned n, int bits)
{
    int r;
    if (!qarm_period(f, bits, &r)) {
        printf("N = %u: no period found\n", n);
        return;
    }
    printf("N = %u, a = %u: the period of a^x mod N is %d", n, a, r);
    if (r % 2 == 0) {
        unsigned h = modexp(a, (unsigned)r / 2, n), p = gcd(h + n - 1, n);
        if (p > 1 && p < n)
            printf(" -> %u = %u x %u", n, p, n / p);
    }
    printf("\n");
}

int main(void)
{
    printf("%s\n", qarm_features() & QARM_F_PERIOD ? "QPERIOD on the quantum coprocessor (Q-ARM)"
                                                   : "periods found by trying every r on this CPU");
    factor(f15, 7, 15, 8);
    factor(f21, 2, 21, 9);
    return 0;
}
