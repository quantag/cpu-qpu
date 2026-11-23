/* SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 Quantag IT Solutions GmbH */
/* Engine of generated QPU libraries (qpu_dll.py), for Linux (.so) and Windows (.dll).
 *
 * A generated library contains the quantum machine as data: the gate list of one machine cycle (X,
 * CNOT, Toffoli, controlled SWAP), the initial state (the program's bits, the data image, the entry
 * state), and which qubits hold the argument registers, the return register, data memory and the PC.
 * A call sets the arguments and the PC, runs the cycle until the machine halts, and reads the
 * results out of the qubits: the same gates, in the same order, as the Python simulator of this
 * repository runs. No Python, server or network is involved at run time.
 *
 * Each call is also checked against the same functions compiled for the host (QPU_VERIFY=0 turns
 * this off): the return value and the output buffers must be equal.
 *
 * The call contract is docs/qcpu_spec.md, sections 5.6, 5.6.2 and 5.7: a call returns the host
 * build's exact result or fails with a status; with verification, the quantum CPU and the host build
 * each work on their own copy of the buffers, and the caller's buffers change once, only on success.
 *
 * Environment (read at each call; the older QPU_* names are accepted too):
 *   QCPU_BACKEND    sim (default; also "qpu"): the quantum CPU in the built-in simulator | native
 *   QCPU_VERIFY     1 (default): compare every call with the host build (guarantee E1) | 0
 *   QCPU_EXACT      1 (default): a call needs a guarantee, else QCPU_UNVERIFIED | 0: allow unchecked
 *   QCPU_ON_FAIL    abort (default): stop with a message | native: use the host build's result | status
 *   QCPU_MAX_STEPS  machine cycles before a call is stopped (default: given at build time)
 *   QCPU_QUIET      1: no notes on stderr */
#ifndef QPU_ENGINE_H
#define QPU_ENGINE_H
#include <stddef.h>
#include <stdint.h>
#include "qcpu.h"

#ifdef _WIN32
#  define QPU__EXPORT __declspec(dllexport)
#else
#  define QPU__EXPORT __attribute__((visibility("default")))
#endif

typedef struct {
    const char *name;                 /* library name, for messages */
    int W;                            /* register width */
    uint32_t nqubits;
    const uint32_t *gates;            /* 3 words per gate: op << 30 | q0, q1, q2 (op 0 x, 1 cx, 2 ccx, 3 cswap) */
    long ngates;
    const uint32_t *ones;             /* qubits that are 1 in the initial state (program, data, entry state) */
    long nones;
    const uint32_t *step_ids;         /* per-cycle qubits, reset to 0 before each cycle */
    long nstep;
    const uint32_t *pc_in, *pc_next;  /* PC before and after a cycle, pbits each */
    int pbits;
    uint32_t run_in, run_out, trap;   /* running flag before / after a cycle; trap of the cycle */
    const uint32_t *arg_q;            /* W qubits per argument register */
    int nargs_max;
    const uint32_t *ret_q;            /* W qubits of the return register */
    const uint32_t *ram_q;            /* W qubits per data cell */
    int mem_cells;
    int user_cell, user_cells;        /* where buffers go (cell index, number of cells) */
    long max_steps;
    int has_native;                   /* the host build of the functions is in the library */
} qpu__lib;

enum { QPU__IN = 1, QPU__OUT = 2, QPU__INOUT = 3 };

typedef struct {
    int arg;                          /* index of the pointer parameter */
    int dir;                          /* QPU__IN / QPU__OUT / QPU__INOUT */
    int *data;                        /* host buffer */
    long long count;
} qpu__buf;

#define QPU__MAXBUF 8

typedef struct {                      /* one call, between qpu__start and qpu__finish */
    const qpu__lib *lib;
    const char *fn;
    qpu__buf *bufs;
    int nbufs;
    long long ret;                    /* QPU return value (sign-extended from W bits) */
    int *qout[QPU__MAXBUF];           /* QPU values of the output buffers */
    int *ncopy[QPU__MAXBUF];          /* buffers given to the host build */
    long cycles;
    int qpu, verify, inexact;
    char why[512];
    int status;                       /* enum qcpu_status, set during the call */
    int need_native;                  /* the wrapper must run the host build */
    int on_fail;                      /* 0 abort, 1 native, 2 status */
    int exact;                        /* QCPU_EXACT */
    int alias[QPU__MAXBUF];           /* buffer b shares its data with buffer alias[b] (== b if not) */
    long long cell_of[QPU__MAXBUF];   /* first data-memory cell of buffer b */
} qpu__ctx;

/* Run fn (entry PC) on the quantum machine. Holds a lock until qpu__finish. */
void qpu__start(qpu__ctx *c, const qpu__lib *lib, const char *fn, long entry, int nargs, const long long *args,
                qpu__buf *bufs, int nbufs);
/* For the host build: a copy of buffer b to call it with. */
int *qpu__native_copy(qpu__ctx *c, int b);
/* Compare a host-build result with the QPU's. */
void qpu__check(qpu__ctx *c, int equal, const char *what);
/* Write the QPU's output buffers to the host and report. Returns the QPU's return value (with
 * QPU_BACKEND=native, the host build's, which the caller passes as native_ret). */
long long qpu__finish(qpu__ctx *c, long long native_ret);

#endif
