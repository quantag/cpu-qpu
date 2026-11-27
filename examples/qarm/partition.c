// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 Quantag IT Solutions GmbH
//
// Number partitioning, an NP-hard problem (the classic QUBO behind many QAOA demos), as ordinary C: split eight
// weights into two groups with sums as equal as possible. QMIN finds the best split, QCOUNT how many splits are
// nearly balanced; the quantum coprocessor builds both oracles from these functions' machine code
// (docs/qarm_v2.md, section 3). On a plain CPU every split is tried in turn.
//
//   clang -O1 -iquote include examples/qarm/partition.c -o partition
//   ./partition                                  a plain CPU
//   python3 qarm_exec.py partition               Q-ARM
//   qemu-aarch64 -plugin libqarm-qemu.so ./partition     Q-ARM under QEMU
#include <stdio.h>
#include "qarm.h"

#define INLINE static inline __attribute__((always_inline))

static const int weight[8] = {13, 7, 9, 4, 11, 6, 3, 8};

INLINE int difference(int mask)                    // bit i set: weight i in the first group
{
    int s = 0;
    for (int i = 0; i < 8; i++)
        s += (mask >> i & 1) ? weight[i] : -weight[i];
    return s < 0 ? -s : s;
}

static int imbalance(int mask) { return difference(mask); }
static int nearly_balanced(int mask) { return difference(mask) <= 3; }

int main(void)
{
    int f = qarm_features(), mask, d;
    qarm_min(imbalance, 8, &mask, &d);
    printf("weights 13 7 9 4 11 6 3 8 (sum 61), %s\n",
           f & QARM_F_MIN ? "QMIN on the quantum coprocessor (Q-ARM)" : "every split tried on this CPU");
    printf("best split: {");
    for (int i = 0, first = 1; i < 8; i++)
        if (mask >> i & 1) { printf(first ? "%d" : " %d", weight[i]); first = 0; }
    printf("} vs {");
    for (int i = 0, first = 1; i < 8; i++)
        if (!(mask >> i & 1)) { printf(first ? "%d" : " %d", weight[i]); first = 0; }
    printf("}: difference %d\n", d);
    double err;
    long n = qarm_count(nearly_balanced, 8, &err);
    if (err > 0)
        printf("splits within 3 of balance: about %ld (+- %.1f; QCOUNT)\n", n, err);
    else
        printf("splits within 3 of balance: %ld (counted)\n", n);
    return 0;
}
