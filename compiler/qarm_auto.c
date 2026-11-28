/* SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 Quantag IT Solutions GmbH
 *
 * The runtime side of the QArmAuto pass (compiler/QArmAuto.cpp): link it into a program that the pass rewrote
 * (qarm-cc --auto does). Each function asks the coprocessor only when it executes that instruction, so that on a
 * plain CPU the rewritten loop runs exactly as written, with no extra work.
 *   __qarm_auto_search(p, bits, &x)    1 and a solution x (checked by p), or 0: the loop searches in full
 *   __qarm_auto_min(c, bits, &x, &v)   1 with the coprocessor's x and v = c(x) (checked), or 0: the loop runs */
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
