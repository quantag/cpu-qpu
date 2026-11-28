/* SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 Quantag IT Solutions GmbH
 *
 * A stand-in for compiler/qarm_auto.c in tests/test_qarm_auto.py: a "coprocessor" whose answers differ from the
 * loops' own, chosen by QARM_FAKE: largest (the largest solution, the last minimizer), none (it declines: the
 * loops must run as written), first (the smallest solution, the first minimizer). It searches 0 .. 2^bits - 1 with
 * the function it is given, so the pass's domain wrappers (N not a power of two) are tested too. */
#include <stdlib.h>
#include <string.h>

static const char *mode(void) { const char *m = getenv("QARM_FAKE"); return m ? m : "largest"; }

int __qarm_auto_search(int (*p)(int), int bits, int *x)
{
    if (!strcmp(mode(), "none")) return 0;
    int found = 0;
    for (int v = 0; v < 1 << bits; v++)
        if (p(v)) { *x = v; found = 1; if (!strcmp(mode(), "first")) break; }
    return found;
}

int __qarm_auto_min(int (*c)(int), int bits, int *x, int *value)
{
    if (!strcmp(mode(), "none")) return 0;
    int best = 0, arg = -1;
    for (int v = 0; v < 1 << bits; v++) {
        int k = c(v);
        if (arg < 0 || k < best || (k == best && strcmp(mode(), "first"))) { best = k; arg = v; }
    }
    *x = arg;
    *value = best;
    return 1;
}
