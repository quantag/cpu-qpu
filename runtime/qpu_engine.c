/* SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 Quantag IT Solutions GmbH */
/* Engine of generated QPU libraries: runs the quantum machine's circuit on a basis state (see
 * qpu_engine.h). Included into each generated library as source, together with the circuit data. */
#ifdef _WIN32
#  ifndef WIN32_LEAN_AND_MEAN
#    define WIN32_LEAN_AND_MEAN
#  endif
#  define _CRT_SECURE_NO_WARNINGS
#  include <windows.h>
static SRWLOCK qpu__lock = SRWLOCK_INIT;
#  define QPU__LOCK() AcquireSRWLockExclusive(&qpu__lock)
#  define QPU__UNLOCK() ReleaseSRWLockExclusive(&qpu__lock)
#else
#  include <pthread.h>
static pthread_mutex_t qpu__lock = PTHREAD_MUTEX_INITIALIZER;
#  define QPU__LOCK() pthread_mutex_lock(&qpu__lock)
#  define QPU__UNLOCK() pthread_mutex_unlock(&qpu__lock)
#endif
#include "qpu_engine.h"
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int qpu__last_status;          /* 0 exact, 1 not exact */
static char qpu__last_message[512];
QPU__EXPORT int qpu_last_status(void) { return qpu__last_status; }
QPU__EXPORT const char *qpu_last_message(void) { return qpu__last_message; }

static int qpu__env(const char *k, int dflt) {
    const char *v = getenv(k);
    return v && *v ? atoi(v) : dflt;
}

static void qpu__die(qpu__ctx *c, const char *fmt, ...) {
    va_list ap;
    fflush(stdout);
    fprintf(stderr, "qpu: %s: %s: ", c->lib->name, c->fn);
    va_start(ap, fmt); vfprintf(stderr, fmt, ap); va_end(ap);
    fprintf(stderr, "\n");
    exit(70);
}

static void qpu__note(qpu__ctx *c, const char *fmt, ...) {      /* first reason a result is not exact */
    va_list ap;
    c->inexact = 1;
    if (c->why[0]) return;
    va_start(ap, fmt); vsnprintf(c->why, sizeof c->why, fmt, ap); va_end(ap);
}

static int qpu__fits(long long v, int W) { return v >= -(1LL << (W - 1)) && v < (1LL << (W - 1)); }

static void qpu__set(unsigned char *st, const uint32_t *q, int W, long long v) {
    for (int i = 0; i < W; i++) st[q[i]] = (unsigned char)((v >> i) & 1);
}

static long long qpu__get(const unsigned char *st, const uint32_t *q, int W) {
    long long v = 0;
    for (int i = 0; i < W; i++) v |= (long long)st[q[i]] << i;
    return v >= (1LL << (W - 1)) ? v - (1LL << W) : v;     /* sign extension: the W-bit model */
}

/* One machine cycle: the gate list, gate by gate. Qubit values are 0 or 1. */
static void qpu__cycle(unsigned char *st, const uint32_t *g, long n) {
    for (const uint32_t *e = g + 3 * n; g < e; g += 3) {
        uint32_t a = g[0] & 0x3FFFFFFFu;
        switch (g[0] >> 30) {
        case 0: st[a] ^= 1; break;                                     /* X */
        case 1: st[g[1]] ^= st[a]; break;                              /* CNOT */
        case 2: st[g[2]] ^= (unsigned char)(st[a] & st[g[1]]); break;  /* Toffoli */
        default:                                                       /* controlled SWAP */
            if (st[a]) { unsigned char t = st[g[1]]; st[g[1]] = st[g[2]]; st[g[2]] = t; }
        }
    }
}

/* Replay the cycle until the machine halts. Exactly what the Python simulator does (core.run_fast):
 * reset the per-cycle qubits, run the gates, carry the PC and the running flag into the next cycle.
 * Returns the number of cycles, -1 on a trap, -2 when max cycles were not enough. */
static long qpu__machine(const qpu__lib *L, unsigned char *st, long max) {
    for (long k = 0; k < max; k++) {
        for (long i = 0; i < L->nstep; i++) st[L->step_ids[i]] = 0;
        qpu__cycle(st, L->gates, L->ngates);
        if (st[L->trap]) return -1;
        for (int i = 0; i < L->pbits; i++) st[L->pc_in[i]] = st[L->pc_next[i]];
        st[L->run_in] = st[L->run_out];
        if (!st[L->run_out]) return k + 1;                             /* halted: the function returned */
    }
    return -2;
}

static void qpu__init(const qpu__lib *L, unsigned char *st, long entry) {
    memset(st, 0, L->nqubits);
    for (long i = 0; i < L->nones; i++) st[L->ones[i]] ^= 1;
    for (int i = 0; i < L->pbits; i++) st[L->pc_in[i]] = (unsigned char)((entry >> i) & 1);
}

#ifdef QPU__SELFTEST
/* for tests/test_dll.py: integer arguments only; the whole final state */
static long qpu__selftest_run(const qpu__lib *L, long entry, int nargs, const long long *args, unsigned char *out) {
    qpu__init(L, out, entry);
    for (int i = 0; i < nargs; i++) qpu__set(out, L->arg_q + (size_t)i * L->W, L->W, args[i]);
    return qpu__machine(L, out, L->max_steps);
}
#endif

void qpu__start(qpu__ctx *c, const qpu__lib *L, const char *fn, long entry, int nargs, const long long *args,
                qpu__buf *bufs, int nbufs) {
    QPU__LOCK();
    memset(c, 0, sizeof *c);
    c->lib = L; c->fn = fn; c->bufs = bufs; c->nbufs = nbufs;
    const char *backend = getenv("QPU_BACKEND");
    c->qpu = !(backend && strcmp(backend, "native") == 0);
    c->verify = L->has_native && qpu__env("QPU_VERIFY", 1);
    if (!c->qpu && !L->has_native) qpu__die(c, "QPU_BACKEND=native, but the library has no host build (it was made from an object file)");
    if (nbufs > QPU__MAXBUF) qpu__die(c, "more than %d buffers", QPU__MAXBUF);
    long long cells = 0;
    for (int b = 0; b < nbufs; b++) {
        if (bufs[b].count < 0) qpu__die(c, "negative buffer size");
        cells += bufs[b].count;
    }
    if (cells > L->user_cells)
        qpu__die(c, "buffers of %lld ints do not fit the machine's data memory (%d ints; build with a larger --memory)",
                 cells, L->user_cells);
    if (!c->qpu) return;

    unsigned char *st = malloc(L->nqubits);
    if (!st) qpu__die(c, "out of memory (%u qubits)", L->nqubits);
    qpu__init(L, st, entry);
    /* arguments: integers as they are; a pointer argument is the byte offset of its buffer in data memory */
    long long cell = L->user_cell;
    for (int i = 0; i < nargs; i++) {
        long long v = args[i];
        int b;
        for (b = 0; b < nbufs && bufs[b].arg != i; b++) {}
        if (b < nbufs) {
            v = 4 * cell;
            for (long long j = 0; j < bufs[b].count; j++, cell++) {
                long long x = bufs[b].dir == QPU__OUT ? 0 : bufs[b].data[j];
                if (!qpu__fits(x, L->W)) qpu__note(c, "%s[%lld] = %lld does not fit %d bits", "buffer", j, x, L->W);
                qpu__set(st, L->ram_q + (size_t)cell * L->W, L->W, x);
            }
        } else if (!qpu__fits(v, L->W)) {
            qpu__note(c, "argument %d = %lld does not fit %d bits", i + 1, v, L->W);
        }
        qpu__set(st, L->arg_q + (size_t)i * L->W, L->W, v);
    }
    long max = qpu__env("QPU_MAX_STEPS", (int)L->max_steps);
    c->cycles = qpu__machine(L, st, max);
    if (c->cycles == -1) { free(st); qpu__die(c, "the machine trapped (an instruction it does not implement)"); }
    if (c->cycles == -2) { free(st); qpu__die(c, "did not return within %ld machine cycles (QPU_MAX_STEPS)", max); }
    c->ret = qpu__get(st, L->ret_q, L->W);
    cell = L->user_cell;
    for (int b = 0; b < nbufs; b++) {
        if (bufs[b].dir != QPU__IN) {
            c->qout[b] = malloc(sizeof(int) * (size_t)(bufs[b].count ? bufs[b].count : 1));
            for (long long j = 0; j < bufs[b].count; j++)
                c->qout[b][j] = (int)qpu__get(st, L->ram_q + (size_t)(cell + j) * L->W, L->W);
        }
        cell += bufs[b].count;
    }
    free(st);
}

int *qpu__native_copy(qpu__ctx *c, int b) {
    qpu__buf *B = &c->bufs[b];
    c->ncopy[b] = malloc(sizeof(int) * (size_t)(B->count ? B->count : 1));
    for (long long j = 0; j < B->count; j++) c->ncopy[b][j] = B->dir == QPU__OUT ? 0 : B->data[j];
    return c->ncopy[b];
}

void qpu__check(qpu__ctx *c, int equal, const char *what) {
    if (!equal) qpu__note(c, "%s differs from the host build", what);
}

long long qpu__finish(qpu__ctx *c, long long native_ret) {
    const qpu__lib *L = c->lib;
    for (int b = 0; b < c->nbufs; b++) {
        qpu__buf *B = &c->bufs[b];
        if (B->dir == QPU__IN) continue;
        int *src = c->qpu ? c->qout[b] : c->ncopy[b];
        if (c->qpu && c->verify && c->ncopy[b])
            for (long long j = 0; j < B->count; j++)
                if (c->qout[b][j] != c->ncopy[b][j]) {
                    qpu__note(c, "buffer element %lld: QPU %d, host build %d", j, c->qout[b][j], c->ncopy[b][j]);
                    break;
                }
        if (src) memcpy(B->data, src, sizeof(int) * (size_t)B->count);
    }
    long long ret = c->qpu ? c->ret : native_ret;
    qpu__last_status = c->inexact;
    snprintf(qpu__last_message, sizeof qpu__last_message, "%s", c->why);
    if (c->inexact && c->qpu) {
        if (qpu__env("QPU_STRICT", 0)) qpu__die(c, "result not exact: %s", c->why);
        if (!qpu__env("QPU_QUIET", 0)) {
            fflush(stdout);
            fprintf(stderr, "qpu: %s: %s: warning: result may be wrong (%d-bit machine): %s\n", L->name, c->fn, L->W, c->why);
        }
    }
    for (int b = 0; b < c->nbufs; b++) { free(c->qout[b]); free(c->ncopy[b]); }
    QPU__UNLOCK();
    return ret;
}
