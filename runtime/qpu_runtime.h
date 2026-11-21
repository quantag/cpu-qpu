/* SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 Quantag IT Solutions GmbH */
/* Runtime of generated QPU libraries (qpu_dll.py). Not called by host programs directly: the
 * generated wrappers call it. Talks to qpu_server.py over a Unix socket and starts the server when
 * none is running.
 *
 * Environment:
 *   QPU_BACKEND  sim (default): the quantum machine, checked against the CPU | cpu: CPU reference only
 *   QPU_WIDTH    register width of the quantum machine, 8..16 (default: the value given to qpu_dll.py)
 *   QPU_STRICT   1: stop the program when a result is not exact (default: warn on stderr)
 *   QPU_QUIET    1: no warnings on stderr
 *   QPU_SERVER   socket path (default /tmp/cpu-qpu-<uid>.sock)
 *   QPU_HOME     directory of qpu_server.py (default: where the library was generated)
 *   QPU_PYTHON   Python interpreter for the server (default python3) */
#ifndef QPU_RUNTIME_H
#define QPU_RUNTIME_H
#include <stddef.h>

typedef struct {
    const char *name;                 /* library name, for messages */
    const unsigned char *object;      /* the QPU object file, embedded */
    size_t size;
    const char *sha256;               /* of object, hex */
    int width;                        /* default register width */
} qpu__lib;

enum { QPU__IN = 1, QPU__OUT = 2, QPU__INOUT = 3 };

typedef struct {
    int arg;                          /* index of the pointer parameter */
    int dir;                          /* QPU__IN / QPU__OUT / QPU__INOUT */
    int *data;                        /* host buffer of count ints */
    long count;
} qpu__buf;

/* Run fn on the QPU. Returns the function's return value. On a failed call, prints a message and
 * exits the program with status 70; an inexact result gives a warning (QPU_STRICT=1: exit too). */
long long qpu__call(const qpu__lib *lib, const char *fn, int nargs, const long long *args,
                    qpu__buf *bufs, int nbufs);

#endif
