/* SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 Quantag IT Solutions GmbH */
/* Stand-in C library header for the quantum-CPU side of a qcpucc build: declarations only. Code that
 * __qcpu__ functions do not reach is left out; reaching a function declared here is a build error. */
#ifndef QCPU_TIME_H
#define QCPU_TIME_H
typedef long time_t;
typedef long clock_t;
time_t time(time_t *);
clock_t clock(void);
#endif
