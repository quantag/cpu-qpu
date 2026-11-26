// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 Quantag IT Solutions GmbH
//
// The smallest Q-ARM search, small enough for a real quantum computer today (docs/qarm_v2.md, "On a real QPU").
//
//   clang -O1 -I include examples/qarm/tiny_search.c -o tiny_search
//   ./tiny_search                                    a plain CPU: classical search
//   python3 qarm_exec.py tiny_search                 the exact simulator (synchronous)
//   python3 qarm_exec.py --backend preview tiny_search   IBM's noise model of a real device, no account
//   python3 qarm_exec.py --qpu tiny_search           config.json's backend, such as a real IBM device (asynchronous:
//                                                    the job waits in IBM's queue, QSEARCH waits for it)
#include <stdio.h>
#include "qarm.h"

// the marked item: 2 bits, one of four
static int is_three(int x) { return (x & 3) == 3; }

int main(void)
{
    int x;
    int r = qarm_search(is_three, 2, &x);
    if (r)
        printf("%s: x = %d\n", qarm_available() ? "Q-ARM QSEARCH" : "classical search", x);
    else
        printf("none\n");
    return r ? 0 : 1;
}
