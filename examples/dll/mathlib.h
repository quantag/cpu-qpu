/* SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 Quantag IT Solutions GmbH */
/* mathlib: a small library whose functions run on the QPU (see README.md in this folder). */
#ifndef MATHLIB_H
#define MATHLIB_H
#include "qpu_dll.h"

int gcd(int a, int b);
int modexp(int a, int x, int n);                    /* a^x mod n */
int is_prime(int n);
int sum(const int *QPU_IN(n) v, int n);
void sort(int *QPU_INOUT(n) v, int n);
void squares(int *QPU_OUT(n) v, int n);             /* v[i] = i*i */

#endif
