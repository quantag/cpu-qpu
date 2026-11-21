/* SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 Quantag IT Solutions GmbH */
/* Host program: an ordinary C program that uses mathlib. Linked against libmathlib_qpu.so, every
 * mathlib call runs on the QPU; linked with mathlib.c, the same program runs entirely on the CPU. */
#include <stdio.h>
#include "mathlib.h"

int main(void) {
    printf("gcd(84, 36)       = %d\n", gcd(84, 36));
    printf("modexp(7, 4, 15)  = %d\n", modexp(7, 4, 15));
    printf("primes below 30  :");
    for (int n = 0; n < 30; n++)
        if (is_prime(n)) printf(" %d", n);
    printf("\n");

    int v[] = {9, -3, 7, 1, 0, 4};
    printf("sum               = %d\n", sum(v, 6));
    sort(v, 6);
    printf("sorted            =");
    for (int i = 0; i < 6; i++) printf(" %d", v[i]);
    printf("\n");

    int sq[8];
    squares(sq, 8);
    printf("squares           =");
    for (int i = 0; i < 8; i++) printf(" %d", sq[i]);
    printf("\n");

    printf("report(21)        = %d\n", report(21));
    return 0;
}
