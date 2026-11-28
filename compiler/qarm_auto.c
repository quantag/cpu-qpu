/* SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 Quantag IT Solutions GmbH
 *
 * The runtime side of the QArmAuto pass (compiler/QArmAuto.cpp): link it into a program that the pass rewrote
 * (qarm-cc --auto does). Each function asks the coprocessor only when it executes that instruction, so that on a
 * plain CPU the rewritten loop runs exactly as written, with no extra work.
 *   __qarm_auto_search(p, bits, &x)    1 and a solution x (checked by p), or 0: the loop searches in full
 *   __qarm_auto_min(c, bits, &x, &v)   1 with the coprocessor's x and v = c(x) (checked), or 0: the loop runs
 *   __qarm_auto_count(f, bits, &m)     1 and m, the number of x with f(x) = 1 (an estimate), or 0: the loop runs
 *   __qarm_auto_sum(g, bits, k, &s)    1 and s, the sum of g(x) in 0 .. 2^k - 1 over all x (an estimate), or 0
 * __qarm_auto_last_err: the error bound of the last count or sum, 0 when it was exact (qarm_auto_error() in
 * qarm_auto.h). This file defines it; qarm_auto.h's weak definition is for programs without it. */
#include "qarm.h"

int __qarm_auto_search(int (*p)(int), int bits, int *x)
{
    if (!(qarm_features() & QARM_F_SEARCH)) return 0;
    return qarm_search(p, bits, x) == 1;
}

int __qarm_auto_min(int (*c)(int), int bits, int *x, int *v)
{
    if (!(qarm_features() & QARM_F_MIN)) return 0;
    return qarm_min(c, bits, x, v) == 1;
}

double __qarm_auto_last_err;

int __qarm_auto_count(int (*f)(int), int bits, long long *m)
{
    __qarm_auto_last_err = 0;
    if (!(qarm_features() & QARM_F_COUNT)) return 0;
    double err = 0;
    *m = qarm_count(f, bits, &err);
    __qarm_auto_last_err = err;
    return 1;
}

int __qarm_auto_sum(int (*g)(int), int bits, int k, double *s)
{
    __qarm_auto_last_err = 0;
    if (!(qarm_features() & QARM_F_AMPEST)) return 0;
    double err = 0, scale = (double)(1L << bits);
    *s = qarm_mean(g, bits, k, &err) * scale;
    __qarm_auto_last_err = err * scale;
    return 1;
}
