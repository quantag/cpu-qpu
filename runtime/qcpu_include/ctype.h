/* SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 Quantag IT Solutions GmbH */
/* Stand-in C library header for the quantum-CPU side of a qcpucc build: declarations only. Code that
 * __qcpu__ functions do not reach is left out; reaching a function declared here is a build error. */
#ifndef QCPU_CTYPE_H
#define QCPU_CTYPE_H
int isdigit(int); int isalpha(int); int isalnum(int); int isspace(int);
int isupper(int); int islower(int); int toupper(int); int tolower(int);
#endif
