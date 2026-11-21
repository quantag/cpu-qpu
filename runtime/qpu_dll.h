/* SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 Quantag IT Solutions GmbH */
/* Annotations for headers of QPU libraries (see qpu_dll.py and docs/qpu_dll.md). Host programs
 * and QPU code both find it with -I <cpu-qpu>/runtime.
 *
 * A function in a QPU library takes integers and pointers to int buffers. A pointer parameter says
 * how its buffer travels between the host and the quantum machine, and how many ints it holds:
 *
 *     int sum(const int *QPU_IN(n) v, int n);       copied to the QPU before the call
 *     void fill(int *QPU_OUT(n) v, int n, int x);    copied back to the host after the call
 *     void sort(int *QPU_INOUT(n) v, int n);         both
 *
 * The count is a C expression over the other parameters (n, 2*n, 16, ...). The macros expand to
 * nothing, so the same header compiles unchanged for the host and for the QPU. */
#ifndef QPU_DLL_H
#define QPU_DLL_H
#define QPU_IN(count)
#define QPU_OUT(count)
#define QPU_INOUT(count)
#endif
