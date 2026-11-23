/* SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 Quantag IT Solutions GmbH */
/* Stand-in C library header for the quantum-CPU side of a qcpucc build: declarations only. Code that
 * __qcpu__ functions do not reach is left out; reaching a function declared here is a build error. */
#ifndef QCPU_STDLIB_H
#define QCPU_STDLIB_H
#include <stddef.h>
#define EXIT_SUCCESS 0
#define EXIT_FAILURE 1
#define RAND_MAX 2147483647
void exit(int);
void abort(void);
int atoi(const char *);
long atol(const char *);
long strtol(const char *, char **, int);
unsigned long strtoul(const char *, char **, int);
int rand(void);
void srand(unsigned);
void *malloc(size_t);
void *calloc(size_t, size_t);
void *realloc(void *, size_t);
void free(void *);
int abs(int);
long labs(long);
char *getenv(const char *);
void qsort(void *, size_t, size_t, int (*)(const void *, const void *));
#endif
