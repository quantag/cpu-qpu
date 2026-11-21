/* SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 Quantag IT Solutions GmbH */
/* Small classic algorithms: C -> clang or gcc -> AArch64 binary -> quantum machine.
 *   clang --target=aarch64-linux-gnu -O1 -Iinclude -c examples/algorithms.c -o algorithms.o
 *   python3 arm_qpu.py algorithms.o                            # runs main()
 *   python3 arm_qpu.py algorithms.o --entry gcd --args 84 36   # one function
 */
#include <stdio.h>

int gcd(int a, int b) { while (b) { int t = a % b; a = b; b = t; } return a; }

int fib(int n) { int a = 0, b = 1; for (int i = 0; i < n; i++) { int t = a + b; a = b; b = t; } return a; }

int is_prime(int n) {
    if (n < 2) return 0;
    for (int d = 2; d * d <= n; d++)
        if (n % d == 0) return 0;
    return 1;
}

int popcount(unsigned x) { int c = 0; while (x) { c += x & 1; x >>= 1; } return c; }

int collatz_steps(int n) { int s = 0; while (n != 1) { n = (n & 1) ? 3 * n + 1 : n / 2; s++; } return s; }

int isqrt(int n) { int r = 0; while ((r + 1) * (r + 1) <= n) r++; return r; }

int power_mod(int b, int e, int m) {
    int r = 1; b %= m;
    while (e > 0) { if (e & 1) r = (r * b) % m; b = (b * b) % m; e >>= 1; }
    return r;
}

int max3(int a, int b, int c) { int m = a > b ? a : b; return m > c ? m : c; }

static int sq(int x) { return x * x; }
int sum_of_squares(int a, int b) { return sq(a) + sq(b); }

void bubble_sort(int *a, int n) {
    for (int i = 0; i < n; i++)
        for (int j = 0; j + 1 < n - i; j++)
            if (a[j] > a[j + 1]) { int t = a[j]; a[j] = a[j + 1]; a[j + 1] = t; }
}

int binary_search(const int *a, int n, int key) {
    int lo = 0, hi = n - 1;
    while (lo <= hi) {
        int mid = (lo + hi) / 2;
        if (a[mid] == key) return mid;
        if (a[mid] < key) lo = mid + 1; else hi = mid - 1;
    }
    return -1;
}

int digit_sum(int n) { int s = 0; while (n > 0) { s += n % 10; n /= 10; } return s; }

/* volatile: the compiler cannot precompute the results, the QPU has to */
static int input(int x) { volatile int v = x; return v; }

int main(void) {
    printf("Classic algorithms on a quantum von Neumann machine\n");
    printf("  gcd(84, 36)      = %d\n", gcd(input(84), input(36)));
    printf("  fib(10)          = %d\n", fib(input(10)));
    printf("  is_prime(89)     = %s\n", is_prime(input(89)) ? "yes" : "no");
    printf("  popcount(109)    = %d\n", popcount(input(109)));
    printf("  isqrt(70)        = %d\n", isqrt(input(70)));
    printf("  3^5 mod 13       = %d\n", power_mod(input(3), input(5), input(13)));
    printf("  max3(-7, 12, 5)  = %d\n", max3(input(-7), input(12), input(5)));
    return 0;
}
