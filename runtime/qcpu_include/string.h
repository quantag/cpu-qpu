/* SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 Quantag IT Solutions GmbH */
/* Stand-in C library header for the quantum-CPU side of a qcpucc build: declarations only. Code that
 * __qcpu__ functions do not reach is left out; reaching a function declared here is a build error. */
#ifndef QCPU_STRING_H
#define QCPU_STRING_H
#include <stddef.h>
void *memcpy(void *, const void *, size_t);
void *memmove(void *, const void *, size_t);
void *memset(void *, int, size_t);
int memcmp(const void *, const void *, size_t);
size_t strlen(const char *);
int strcmp(const char *, const char *);
int strncmp(const char *, const char *, size_t);
char *strcpy(char *, const char *);
char *strncpy(char *, const char *, size_t);
char *strcat(char *, const char *);
char *strchr(const char *, int);
char *strrchr(const char *, int);
char *strstr(const char *, const char *);
#endif
