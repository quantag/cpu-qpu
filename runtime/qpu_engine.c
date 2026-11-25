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

static qcpu_call_info qpu__last;
static const char *QPU__NAMES[] = {"QCPU_OK", "QCPU_MISMATCH", "QCPU_INEXACT", "QCPU_INCOMPLETE", "QCPU_TRAP",
    "QCPU_TIE", "QCPU_INSUFFICIENT", "QCPU_UNVERIFIED", "QCPU_BAD_ARGS", "QCPU_PROBABLE", "QCPU_UNCHECKED"};
QPU__EXPORT const char *qcpu_status_name(int s) {
    return s >= 0 && s < (int)(sizeof QPU__NAMES / sizeof *QPU__NAMES) ? QPU__NAMES[s] : "QCPU_?";
}
QPU__EXPORT int qcpu_last_call(qcpu_call_info *info) {
    if (info) *info = qpu__last;
    return qpu__last.status;
}
/* older names */
QPU__EXPORT int qpu_last_status(void) { return qpu__last.status != QCPU_OK; }
QPU__EXPORT const char *qpu_last_message(void) { return qpu__last.message; }

static const char *qpu__getenv(const char *k) {            /* QCPU_X, else the older QPU_X */
    const char *v = getenv(k);
    if (v && *v) return v;
    char old[64];
    snprintf(old, sizeof old, "QPU_%s", k + 5);
    v = getenv(old);
    return v && *v ? v : NULL;
}

static int qpu__env(const char *k, int dflt) {
    const char *v = qpu__getenv(k);
    return v ? atoi(v) : dflt;
}

static void qpu__die(qpu__ctx *c, const char *fmt, ...) {      /* errors in the build or the program */
    va_list ap;
    fflush(stdout);
    fprintf(stderr, "qcpu: %s: %s: ", c->lib->name, c->fn);
    va_start(ap, fmt); vfprintf(stderr, fmt, ap); va_end(ap);
    fprintf(stderr, "\n");
    exit(70);
}

static void qpu__fail(qpu__ctx *c, int status, const char *fmt, ...) {    /* first reason the call fails */
    va_list ap;
    if (c->status != QCPU_OK) return;
    c->status = status;
    va_start(ap, fmt); vsnprintf(c->why, sizeof c->why, fmt, ap); va_end(ap);
}

static void qpu__note(qpu__ctx *c, const char *fmt, ...) {      /* a value outside the W-bit model (informative) */
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

/* qoracle mode: one pass of the function's own circuit. Arguments outside their declared bits are refused. */
static void qpu__run_circuit(qpu__ctx *c, const qpu__circuit *K, int nargs, const long long *args) {
    for (int i = 0; i < nargs && i < K->nargs; i++) {
        int n = K->in_bits[i];
        long long lo = K->in_signed[i] ? -(1LL << (n - 1)) : 0, hi = K->in_signed[i] ? (1LL << (n - 1)) - 1 : (long long)((1ULL << n) - 1);
        if (n < 64 && (args[i] < lo || args[i] > hi)) {
            qpu__fail(c, QCPU_BAD_ARGS, "argument %d = %lld is outside its declared %s%d bits (%lld .. %lld)",
                      i + 1, args[i], K->in_signed[i] ? "signed " : "", n, lo, hi);
            return;
        }
    }
    unsigned char *st = calloc(K->nqubits ? K->nqubits : 1, 1);
    if (!st) qpu__die(c, "out of memory (%u qubits)", K->nqubits);
    for (int i = 0; i < K->nargs; i++)
        for (int b = 0; b < K->in_bits[i]; b++) st[K->in_first[i] + b] = (unsigned char)(((unsigned long long)args[i] >> b) & 1);
    qpu__cycle(st, K->gates, K->ngates);
    unsigned long long v = 0;
    for (int b = 0; b < K->out_bits; b++) v |= (unsigned long long)st[K->out_first + b] << b;
    c->ret = (long long)v;
    for (uint32_t q = K->work_first; q < K->nqubits; q++)
        if (st[q]) { qpu__fail(c, QCPU_INEXACT, "work qubit %u is not returned to 0 (a broken circuit)", q); break; }
    for (int i = 0; i < K->nargs && c->status == QCPU_OK; i++)
        for (int b = 0; b < K->in_bits[i]; b++)
            if (st[K->in_first[i] + b] != (((unsigned long long)args[i] >> b) & 1)) {
                qpu__fail(c, QCPU_INEXACT, "input register %d was changed (a broken circuit)", i + 1);
                break;
            }
    free(st);
}

void qpu__start(qpu__ctx *c, const qpu__lib *L, const char *fn, long entry, int nargs, const long long *args,
                qpu__buf *bufs, int nbufs) {
    qpu__start_c(c, L, NULL, fn, entry, nargs, args, bufs, nbufs);
}

void qpu__start_c(qpu__ctx *c, const qpu__lib *L, const qpu__circuit *circ, const char *fn, long entry, int nargs,
                  const long long *args, qpu__buf *bufs, int nbufs) {
    QPU__LOCK();
    memset(c, 0, sizeof *c);
    c->lib = L; c->fn = fn; c->bufs = bufs; c->nbufs = nbufs;
    c->circ = circ;                       /* qcpucc --qcpu-mode qoracle: every function has its own circuit */
    const char *backend = qpu__getenv("QCPU_BACKEND");
    if (backend && strcmp(backend, "native") && strcmp(backend, "sim") && strcmp(backend, "qpu"))
        qpu__die(c, "QCPU_BACKEND=%s: this program supports sim and native", backend);
    c->qpu = !(backend && strcmp(backend, "native") == 0);
    c->verify = L->has_native && qpu__env("QCPU_VERIFY", 1);
    c->exact = qpu__env("QCPU_EXACT", 1);
    const char *of = qpu__getenv("QCPU_ON_FAIL");
    c->on_fail = !of || !strcmp(of, "abort") ? 0 : !strcmp(of, "native") ? 1 : !strcmp(of, "status") ? 2 : -1;
    if (c->on_fail < 0) qpu__die(c, "QCPU_ON_FAIL=%s: use abort, native or status", of);
    if (!c->qpu && !L->has_native) qpu__die(c, "QCPU_BACKEND=native, but the program has no host build of the function");
    if (c->on_fail == 1 && !L->has_native) qpu__die(c, "QCPU_ON_FAIL=native, but the program has no host build of the function");
    if (nbufs > QPU__MAXBUF) qpu__die(c, "more than %d buffers", QPU__MAXBUF);
    /* buffers: the same buffer twice is shared (aliasing kept); any other overlap is refused */
    long long cells = 0;
    for (int b = 0; b < nbufs; b++) {
        c->alias[b] = b;
        if (bufs[b].count < 0) { qpu__fail(c, QCPU_BAD_ARGS, "buffer %d has a negative size", b + 1); continue; }
        for (int a = 0; a < b; a++) {
            const int *p = bufs[a].data, *q = bufs[b].data;
            if (!bufs[a].count || !bufs[b].count) continue;
            if (p == q && bufs[a].count == bufs[b].count) { c->alias[b] = c->alias[a]; break; }
            if (p < q + bufs[b].count && q < p + bufs[a].count)
                qpu__fail(c, QCPU_BAD_ARGS, "buffers %d and %d overlap", a + 1, b + 1);
        }
        if (c->alias[b] == b) { c->cell_of[b] = L->user_cell + cells; cells += bufs[b].count; }
        else c->cell_of[b] = c->cell_of[c->alias[b]];
    }
    if (c->status == QCPU_OK && cells > L->user_cells)
        qpu__die(c, "buffers of %lld ints do not fit the machine's data memory (%d ints; build with a larger --memory)",
                 cells, L->user_cells);
    if (c->status != QCPU_OK) return;
    c->need_native = L->has_native && (c->verify || !c->qpu || c->on_fail == 1);
    if (!c->qpu) return;
    if (!c->circ && !L->ngates)
        qpu__die(c, "%s has neither a qoracle circuit nor a virtual CPU (a broken build)", fn);
    if (c->circ) {
        qpu__run_circuit(c, c->circ, nargs, args);
        if (c->status != QCPU_OK) c->need_native = L->has_native && c->on_fail == 1;
        return;
    }

    unsigned char *st = malloc(L->nqubits);
    if (!st) qpu__die(c, "out of memory (%u qubits)", L->nqubits);
    qpu__init(L, st, entry);
    /* arguments: integers as they are; a pointer argument is the byte offset of its buffer in data memory */
    for (int i = 0; i < nargs; i++) {
        long long v = args[i];
        int b;
        for (b = 0; b < nbufs && bufs[b].arg != i; b++) {}
        if (b < nbufs) {
            v = 4 * c->cell_of[b];
            if (c->alias[b] == b)
                for (long long j = 0; j < bufs[b].count; j++) {
                    int in = bufs[b].dir != QPU__OUT;
                    for (int a = b + 1; a < nbufs; a++) if (c->alias[a] == b && bufs[a].dir != QPU__OUT) in = 1;
                    long long x = in ? bufs[b].data[j] : 0;
                    if (!qpu__fits(x, L->W)) qpu__note(c, "buffer element %lld = %lld does not fit %d bits", j, x, L->W);
                    qpu__set(st, L->ram_q + (size_t)(c->cell_of[b] + j) * L->W, L->W, x);
                }
        } else if (!qpu__fits(v, L->W)) {
            qpu__note(c, "argument %d = %lld does not fit %d bits", i + 1, v, L->W);
        }
        qpu__set(st, L->arg_q + (size_t)i * L->W, L->W, v);
    }
    long max = qpu__env("QCPU_MAX_STEPS", (int)L->max_steps);
    c->cycles = qpu__machine(L, st, max);
    if (c->cycles == -1) qpu__fail(c, QCPU_TRAP, "the quantum CPU reached an instruction it does not implement");
    else if (c->cycles == -2) qpu__fail(c, QCPU_INCOMPLETE, "did not return within %ld machine cycles (QCPU_MAX_STEPS)", max);
    if (c->status != QCPU_OK) c->need_native = L->has_native && c->on_fail == 1;   /* nothing to compare */
    else {
        c->ret = qpu__get(st, L->ret_q, L->W);
        for (int b = 0; b < nbufs; b++) {
            if (bufs[b].dir == QPU__IN) continue;
            c->qout[b] = malloc(sizeof(int) * (size_t)(bufs[b].count ? bufs[b].count : 1));
            for (long long j = 0; j < bufs[b].count; j++)
                c->qout[b][j] = (int)qpu__get(st, L->ram_q + (size_t)(c->cell_of[b] + j) * L->W, L->W);
        }
    }
    free(st);
}

/* The host build's copy of buffer b: its own snapshot of the caller's data, shared between aliased buffers. */
int *qpu__native_copy(qpu__ctx *c, int b) {
    int a = c->alias[b];
    if (!c->ncopy[a]) {
        qpu__buf *B = &c->bufs[a];
        int in = 0;
        for (int k = 0; k < c->nbufs; k++) if (c->alias[k] == a && c->bufs[k].dir != QPU__OUT) in = 1;
        c->ncopy[a] = malloc(sizeof(int) * (size_t)(B->count ? B->count : 1));
        for (long long j = 0; j < B->count; j++) c->ncopy[a][j] = in ? B->data[j] : 0;
    }
    return c->ncopy[a];
}

void qpu__check(qpu__ctx *c, int equal, const char *what) {
    if (c->qpu && c->verify && c->status == QCPU_OK && !equal)
        qpu__fail(c, QCPU_MISMATCH, "%s differs from the host build", what);
}

long long qpu__finish(qpu__ctx *c, long long native_ret) {
    const qpu__lib *L = c->lib;
    /* status */
    if (c->status == QCPU_OK && c->qpu && c->verify) {
        for (int b = 0; b < c->nbufs && c->status == QCPU_OK; b++) {
            if (c->bufs[b].dir == QPU__IN || c->alias[b] != b) continue;
            for (long long j = 0; j < c->bufs[b].count; j++)
                if (c->qout[b][j] != c->ncopy[b][j]) {
                    qpu__fail(c, QCPU_MISMATCH, "buffer %d, element %lld: quantum CPU %d, host build %d",
                              b + 1, j, c->qout[b][j], c->ncopy[b][j]);
                    break;
                }
        }
    }
    const char *guarantee = !c->qpu ? "native" : c->verify ? "E1" : "none";
    if (c->status == QCPU_OK && c->qpu && !c->verify) {
        if (c->exact) qpu__fail(c, QCPU_UNVERIFIED, "no exactness guarantee: QCPU_VERIFY=0 (set QCPU_EXACT=0 to accept unchecked results)");
        else c->status = QCPU_UNCHECKED;
    }
    int ok = c->status == QCPU_OK || c->status == QCPU_UNCHECKED;
    /* the result and the caller's buffers: changed exactly once, and only by a successful call */
    long long ret = 0;
    int **src = NULL;
    if (ok) { ret = c->qpu ? c->ret : native_ret; src = c->qpu ? c->qout : c->ncopy; }
    else if (c->on_fail == 1 && c->need_native) { ret = native_ret; src = c->ncopy; }
    if (src)
        for (int b = 0; b < c->nbufs; b++)
            if (c->bufs[b].dir != QPU__IN && c->alias[b] == b && src[b])
                memcpy(c->bufs[b].data, src[b], sizeof(int) * (size_t)c->bufs[b].count);
    memset(&qpu__last, 0, sizeof qpu__last);
    qpu__last.status = c->status;
    qpu__last.function = c->fn;
    qpu__last.backend = c->qpu ? "sim" : "native";
    qpu__last.guarantee = guarantee;
    qpu__last.cycles = c->cycles > 0 ? c->cycles : 0;
    qpu__last.mode = !c->qpu ? "none" : c->circ ? "qoracle" : "vcpu";
    if (c->qpu && c->circ) {
        qpu__last.qubits = c->circ->nqubits;
        qpu__last.gates = c->circ->ngates;
    } else if (c->cycles > 0) {
        qpu__last.qubits = (long long)L->nqubits + (long long)(c->cycles - 1) * L->nstep;
        qpu__last.gates = (long long)c->cycles * L->ngates;
    }
    snprintf(qpu__last.message, sizeof qpu__last.message, "%s", c->why);
    for (int b = 0; b < c->nbufs; b++) { free(c->qout[b]); free(c->ncopy[b]); }
    if (!ok) {
        if (c->on_fail == 0) {
            fflush(stdout);
            fprintf(stderr, "qcpu: %s: %s: %s: %s\n", L->name, c->fn, qcpu_status_name(c->status), c->why);
            exit(70);
        }
        if (!qpu__env("QCPU_QUIET", 0)) {
            fflush(stdout);
            fprintf(stderr, "qcpu: %s: %s: %s: %s%s\n", L->name, c->fn, qcpu_status_name(c->status), c->why,
                    c->on_fail == 1 ? " (the host build's result is used)" : "");
        }
    }
    QPU__UNLOCK();
    return ret;
}
