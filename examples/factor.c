/* SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 Quantag IT Solutions GmbH */
/* Prime factorization by trial division.
 * Build an AArch64 binary, then run it on the quantum machine:
 *   clang --target=aarch64-linux-gnu -O1 -Iinclude -c examples/factor.c -o factor.o
 *   python3 arm_qpu.py factor.o
 */
#include <stdio.h>

/* smallest prime factor of n (n itself if n is prime) */
int smallest_factor(int n) {
    for (int d = 2; d * d <= n; d++)
        if (n % d == 0)
            return d;
    return n;
}

/* writes the prime factors of n into out[], returns how many there are */
int factorize(int n, int *out) {
    int k = 0;
    while (n > 1) {
        int p = smallest_factor(n);
        out[k++] = p;
        n /= p;
    }
    return k;
}

/* volatile: the compiler cannot precompute the results, the QPU has to */
static int input(int x) { volatile int v = x; return v; }

static void print_factors(int n) {
    int f[8];
    int k = factorize(n, f);
    printf("%3d =", n);
    for (int i = 0; i < k; i++)
        printf(" %d", f[i]);
    printf(k == 1 ? "  (prime)\n" : "\n");
}

int main(void) {
    printf("Prime factorization, running on a quantum von Neumann machine\n");
    print_factors(input(84));
    print_factors(input(91));
    print_factors(input(97));
    return 0;
}
