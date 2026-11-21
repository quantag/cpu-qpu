/* SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 Quantag IT Solutions GmbH */
#include <stdio.h>

int smallest_factor(int n) {
    for (int d = 2; d * d <= n; d++)
        if (n % d == 0)
            return d;
    return n;
}

int main(void) {
    int n = 84;
    printf("Hello from the QPU!\n");
    printf("%d =", n);
    while (n > 1) {
        int p = smallest_factor(n);
        printf(" %d", p);
        n /= p;
    }
    printf("\n");
    return 0;
}
