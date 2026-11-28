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
 * pass, or without a coprocessor, the function runs as written. docs/qarm_auto.md. */
#ifndef QARM_AUTO_H
#define QARM_AUTO_H
#if defined(__clang__)
#define QARM_AUTO __attribute__((annotate("qarm_auto")))
#else
#define QARM_AUTO                                     /* GCC: no pass; the loops run as written */
#endif
#endif
