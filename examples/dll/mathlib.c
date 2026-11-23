/* SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 Quantag IT Solutions GmbH */
/* mathlib: ordinary C. Compiled for the QPU by qpu_dll.py, or for the host for comparison. */
#include "mathlib.h"

int gcd(int a, int b) {
    while (b) { int t = a % b; a = b; b = t; }
    return a;
}

int modexp(int a, int x, int n) {
    int r = 1;
    a %= n;
    while (x > 0) {
        if (x & 1) r = r * a % n;
        a = a * a % n;
        x >>= 1;
    }
    return r;
}

int is_prime(int n) {
    if (n < 2) return 0;
    for (int d = 2; d * d <= n; d++)
        if (n % d == 0) return 0;
    return 1;
}

int sum(const int *v, int n) {
    int s = 0;
    for (int i = 0; i < n; i++) s += v[i];
    return s;
}

void sort(int *v, int n) {             /* bubble sort; pointers instead of int indices (see README.md) */
    for (int *end = v + n; end > v + 1; end--)
        for (int *p = v; p + 1 < end; p++)
            if (p[0] > p[1]) { int t = p[0]; p[0] = p[1]; p[1] = t; }
}

void squares(int *v, int n) {
    for (int i = 0; i < n; i++) v[i] = i * i;
}
