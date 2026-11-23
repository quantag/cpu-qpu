/* SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 Quantag IT Solutions GmbH */
/* Stand-in C library header for the quantum-CPU side of a qcpucc build: declarations only. Code that
 * __qcpu__ functions do not reach is left out; reaching a function declared here is a build error. */
#ifndef QCPU_STDIO_H
#define QCPU_STDIO_H
#include <stddef.h>
#include <stdarg.h>
typedef struct qcpu__FILE FILE;
extern FILE *stdin, *stdout, *stderr;
#define EOF (-1)
int printf(const char *, ...);
int fprintf(FILE *, const char *, ...);
int sprintf(char *, const char *, ...);
int snprintf(char *, size_t, const char *, ...);
int vprintf(const char *, va_list);
int vfprintf(FILE *, const char *, va_list);
int scanf(const char *, ...);
int fscanf(FILE *, const char *, ...);
int sscanf(const char *, const char *, ...);
int puts(const char *);
int fputs(const char *, FILE *);
int putchar(int);
int fputc(int, FILE *);
int putc(int, FILE *);
int getchar(void);
int fgetc(FILE *);
int getc(FILE *);
char *fgets(char *, int, FILE *);
int fflush(FILE *);
FILE *fopen(const char *, const char *);
int fclose(FILE *);
size_t fread(void *, size_t, size_t, FILE *);
size_t fwrite(const void *, size_t, size_t, FILE *);
void perror(const char *);
#endif
