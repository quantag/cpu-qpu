/* SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 Quantag IT Solutions GmbH
 *
 * QARM_AUTO: marks a function whose search and minimum loops the QArmAuto compiler pass (compiler/) may give to
 * the quantum coprocessor, QSEARCH and QMIN. The only change to the source:
 *
 *   #include "qarm_auto.h"
 *   QARM_AUTO int find_key(void) { for (int x = 0; x < 65536; x++) if (opens(x)) return x; return -1; }
 *
 * What the mark allows: a search may return any solution, not the smallest; a minimum may return any minimizer
 * (with high probability: Durr-Hoyer). "None found" stays definitive: the loop then runs in full. Without the
 * pass, or without a coprocessor, the function runs as written. docs/qarm_auto.md.
 *
 * QARM_ESTIMATE: QARM_AUTO's loops, and also counts and sums, which become estimates (QCOUNT, QAMPEST):
 *
 *   QARM_ESTIMATE long fits(void) { long n = 0; for (int x = 0; x < 4096; x++) if (ok(x)) n++; return n; }
 *   QARM_ESTIMATE double mean(void) { double s = 0; for (int x = 0; x < 4096; x++) s += byte(x); return s / 4096; }
 *
 * A count is n++ (or n += a bool) under a condition on a pure f(x); a sum adds a value of at most 16 known bits
 * (an unsigned char or short, a mask such as & 0xff). qarm_auto_error() is the error bound of the last estimate
 * (it holds with probability >= 8/pi^2), 0 when the loop ran exactly: on a CPU, every loop does. */
#ifndef QARM_AUTO_H
#define QARM_AUTO_H
#if defined(__clang__)
#define QARM_AUTO __attribute__((annotate("qarm_auto")))
#define QARM_ESTIMATE __attribute__((annotate("qarm_estimate")))
/* set by compiler/qarm_auto.c, linked into a program the pass rewrote; this weak 0 stands in without it */
__attribute__((weak)) double __qarm_auto_last_err = 0;
static inline double qarm_auto_error(void) { return __qarm_auto_last_err; }
#else
#define QARM_AUTO                                     /* GCC: no pass; the loops run as written */
#define QARM_ESTIMATE
static inline double qarm_auto_error(void) { return 0; }
#endif
#endif
