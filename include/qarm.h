/* SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 Quantag IT Solutions GmbH
 *
 * Q-ARM v2: quantum instructions in an ordinary ARM program (docs/qarm_v2.md).
 *
 * Each function below places a Q-ARM packet in the code: eight 32-bit words that begin with B .+32, an
 * ordinary branch. A plain ARM CPU jumps over the packet and takes the classical path; the Q-ARM runtime
 * (qarm_exec.py) executes the packet instead, on a quantum coprocessor (its simulator, or a backend
 * from config.json). The same binary runs on both.
 *
 *   qarm_available()            1 under the Q-ARM runtime, 0 on a plain CPU (QAVAIL)
 *   qarm_features()             what that runtime executes: QARM_F_SEARCH | QARM_F_VQE (QAVAIL's W1); 0 on a plain
 *                               CPU. The QEMU plugin reports QARM_F_SEARCH when its oracle helper is there
 *   qarm_search(f, bits, &x)    an x in 0 .. 2^bits - 1 with f(x) == 1: Grover's search on the coprocessor (QSEARCH),
 *                               with f's oracle built from f's own machine code; on a plain CPU, every x in turn.
 *                               Returns 1 and stores x, or 0 if there is none. f returns 0 or 1, calls no other
 *                               function and writes no memory; bits <= 16. Every answer is checked by calling f.
 *
 * VQE (section 6 of docs/qarm_v2.md): the program's optimizer runs on the CPU; the coprocessor prepares the
 * ansatz state and measures the energy. On a plain CPU each call computes the energy exactly from the state
 * vector, in C (up to QARM_VQE_MAX_QUBITS qubits; link with -lm).
 *
 *   qarm_vqe_load(&h, &a, &v)          QVLOAD: check the tables, prepare the circuits once. 1: on the
 *                                      coprocessor, 0: classical, -1: refused (the reason on stderr)
 *   qarm_expect(&v, theta, &err)       QEXPECT: <psi(theta)|H|psi(theta)>; err: its standard error (0 if exact)
 *   qarm_expect_n(&v, thetas, k, e)    QEXPECTN: k energies for k parameter vectors, one job on a QPU
 *   qarm_grad(&v, theta, grad)         QGRAD: the energy, and its gradient in grad (one value per parameter); exact
 *                                      by adjoint differentiation, or by the parameter-shift rule on a QPU (one job)
 *   qarm_session(1) / qarm_session(0)  QSESSION: reserve a QPU for the optimizer loop, release it
 *   qarm_vqe_free(&v)                  QVFREE
 *
 *   qarm_expect_n_async(&v, thetas, k) a batch that may still be on the QPU: a qarm_job
 *   qarm_job_ready(job)                1 done, 0 not yet, -1 failed; never waits
 *   qarm_job_wait(job, e, err)         the k energies (err may be NULL); frees the job; 1, or -1
 *
 * Three builds of the same source:
 *   plain (any compiler, any CPU)      the classical path only
 *   arm64 with GCC or clang            Q-ARM packets: the classical path on a CPU, the quantum one under qarm_exec.py
 *   -DQARM_RUNTIME, linked with libqarm (runtime/libqarm): native, any CPU; VQE on the backend of config.json
 *                                      (exact, simulator, or a real IBM device over REST). qarm_search is classical
 *                                      there; under the QEMU plugin it is quantum (docs/qarm_v2.md, section 8). */
#ifndef QARM_H_INCLUDED
#define QARM_H_INCLUDED
#include <stdint.h>

/* H = constant + sum t[j].c P_j; P_j acts on qubit q as I, X, Z or Y for (x_q, z_q) = 00, 10, 01, 11 */
typedef struct { uint64_t x, z; double c; } qarm_pauli;
typedef struct { uint32_t qubits, terms; double constant; const qarm_pauli *t; } qarm_hamiltonian;

/* the ansatz: the basis state init, then the operations in order. RY, RZ: angle = scale * theta[param];
 * QARM_PAULI_ROT: exp(-i * scale * theta[param] * P(x, z)), for UCC excitations */
enum { QARM_X = 1, QARM_H, QARM_CX, QARM_CZ, QARM_RY, QARM_RZ, QARM_PAULI_ROT };
typedef struct { uint8_t kind, q0, q1, param; uint32_t reserved; double scale; uint64_t x, z; } qarm_op;
typedef struct { uint32_t qubits, params, ops, reserved; uint64_t init; const qarm_op *op; } qarm_ansatz;
typedef struct { const qarm_hamiltonian *h; const qarm_ansatz *a; intptr_t handle; int quantum; } qarm_vqe;
enum { QARM_F_SEARCH = 1, QARM_F_VQE = 2 };

#if defined(QARM_RUNTIME)
#include <math.h>
#include "qarm_rt.h"

static inline int qarm_available(void) { return 0; }
static inline int qarm_features(void) { return 0; }
static inline int qarm_search(int (*f)(int), int bits, int *x)
{
    for (long v = 0; v < (1L << bits); v++)
        if (f((int)v)) { *x = (int)v; return 1; }
    return 0;
}
static inline int qarm_vqe_load(const qarm_hamiltonian *h, const qarm_ansatz *a, qarm_vqe *v)
{
    qarm_rt *r = 0;
    int w = qarm_rt_load(h, a, &r);
    v->h = h; v->a = a; v->handle = (intptr_t)r; v->quantum = w > 0;
    return w;
}
static inline double qarm_expect(qarm_vqe *v, const double *theta, double *err)
{
    double e = NAN, s = -1;
    if (qarm_rt_expect_n((qarm_rt *)v->handle, theta, 1, 0, &e, &s) < 0) { e = NAN; s = -1; }
    if (err) *err = s;
    return e;
}
static inline int qarm_expect_n(qarm_vqe *v, const double *thetas, int k, double *e)
{
    return qarm_rt_expect_n((qarm_rt *)v->handle, thetas, k, 0, e, 0);
}
static inline double qarm_grad(qarm_vqe *v, const double *theta, double *grad)
{
    double e = NAN;
    return qarm_rt_grad((qarm_rt *)v->handle, theta, 0, &e, grad) > 0 ? e : NAN;
}
static inline qarm_job *qarm_expect_n_async(qarm_vqe *v, const double *thetas, int k)
{
    return qarm_rt_submit((qarm_rt *)v->handle, thetas, k, 0);
}
static inline int qarm_job_ready(qarm_job *j) { return qarm_rt_poll(j); }
static inline int qarm_job_wait(qarm_job *j, double *e, double *err) { return qarm_rt_wait(j, e, err); }
static inline int qarm_session(int open) { return qarm_rt_session(open); }
static inline void qarm_vqe_free(qarm_vqe *v)
{
    qarm_rt_free((qarm_rt *)v->handle);
    v->handle = 0; v->quantum = 0;
}
#else

#ifndef QARM_VQE_MAX_QUBITS
#define QARM_VQE_MAX_QUBITS 12
#endif
#include <stdio.h>
#include "qarm_sv.h"

/* the classical path: the exact state vector of qarm_sv.h in six static arrays */
static double qarm__buf[6][1 << QARM_VQE_MAX_QUBITS];
static qarm_sv qarm__sv(int n)
{
    qarm_sv s = {n, qarm__buf[0], qarm__buf[1], qarm__buf[2], qarm__buf[3], qarm__buf[4], qarm__buf[5]};
    return s;
}
static double qarm__energy(const qarm_hamiltonian *h, const qarm_ansatz *a, const double *theta)
{
    qarm_sv s = qarm__sv((int)h->qubits);
    qarm_sv_prepare(&s, a, theta);
    return qarm_sv_energy(&s, h);
}
static double qarm__grad(const qarm_hamiltonian *h, const qarm_ansatz *a, const double *theta, double *grad)
{
    qarm_sv s = qarm__sv((int)h->qubits);
    return qarm_sv_grad(&s, h, a, theta, grad);
}

static int qarm__vqe_classical(const qarm_hamiltonian *h, const qarm_ansatz *a, qarm_vqe *v)
{
    v->h = h; v->a = a; v->handle = 0; v->quantum = 0;
    if (h->qubits > QARM_VQE_MAX_QUBITS || a->qubits != h->qubits) {
        fprintf(stderr, "qarm_vqe_load: %u qubits; the classical path takes up to %d\n", h->qubits,
                QARM_VQE_MAX_QUBITS);
        return -1;
    }
    return 0;
}

#if defined(__aarch64__) && (defined(__GNUC__) || defined(__clang__))
#define QARM_PACKET(op) \
    ".inst 0x14000008\n\t"            /* B .+32: a plain CPU continues after the packet */ \
    ".inst 0x4d524151\n\t"            /* "QARM" */ \
    ".inst " #op "\n\t"               /* version 2, opcode */ \
    ".inst 0\n\t.inst 0\n\t.inst 0\n\t.inst 0\n\t.inst 0\n\t"
#define QARM_OP_QAVAIL  0x02100000
#define QARM_OP_QSEARCH 0x02110000

static inline int qarm_available(void)
{
    register long x0 __asm__("x0") = 0;                     /* a plain CPU leaves 0 */
    __asm__ volatile(QARM_PACKET(0x02100000) : "+r"(x0) : : "memory");
    return (int)x0;
}

static inline int qarm_features(void)
{
    register long x0 __asm__("x0") = 0;
    register long x1 __asm__("x1") = 0;
    __asm__ volatile(QARM_PACKET(0x02100000) : "+r"(x0), "+r"(x1) : : "memory");
    return x0 == 1 ? (int)(x1 ? x1 : QARM_F_SEARCH) : 0;     /* a runtime without feature bits: QSEARCH only */
}

static inline int qarm_search(int (*f)(int), int bits, int *x)
{
    register long x0 __asm__("x0") = 0;                     /* 0: not executed, 1: found, 2: none */
    register int (*x1)(int) __asm__("x1") = f;
    register long x2 __asm__("x2") = bits;
    register long x3 __asm__("x3") = 0;
    __asm__ volatile(QARM_PACKET(0x02110000) : "+r"(x0), "+r"(x3) : "r"(x1), "r"(x2) : "memory");
    if (x0 == 1 && f((int)x3) == 1) { *x = (int)x3; return 1; }   /* every answer checked by f itself */
    if (x0 == 2) return 0;
    for (long v = 0; v < (1L << bits); v++)                 /* the classical path, also after a wrong answer */
        if (f((int)v)) { *x = (int)v; return 1; }
    return 0;
}

static inline int qarm_vqe_load(const qarm_hamiltonian *h, const qarm_ansatz *a, qarm_vqe *v)
{
    register long x0 __asm__("x0") = 0;                     /* 0: not executed, 1: loaded, 2: refused */
    register const qarm_hamiltonian *x1 __asm__("x1") = h;
    register const qarm_ansatz *x2 __asm__("x2") = a;
    register long x3 __asm__("x3") = 0;
    __asm__ volatile(QARM_PACKET(0x02200000) : "+r"(x0), "+r"(x3) : "r"(x1), "r"(x2) : "memory");
    if (x0 == 1) { v->h = h; v->a = a; v->handle = x3; v->quantum = 1; return 1; }
    if (x0 == 2) { v->quantum = 0; return -1; }
    return qarm__vqe_classical(h, a, v);
}

static inline double qarm_expect(qarm_vqe *v, const double *theta, double *err)
{
    if (v->quantum) {
        register long x0 __asm__("x0") = 0;
        register const double *x1 __asm__("x1") = theta;
        register long x2 __asm__("x2") = 0;                 /* shots: config.json's */
        register long x3 __asm__("x3") = v->handle;
        register double d0 __asm__("d0") = 0, d1 __asm__("d1") = 0;
        __asm__ volatile(QARM_PACKET(0x02210000) : "+r"(x0), "+w"(d0), "+w"(d1) : "r"(x1), "r"(x2), "r"(x3)
                         : "memory");
        if (x0 == 1) { if (err) *err = d1; return d0; }
    }
    if (err) *err = 0;
    return qarm__energy(v->h, v->a, theta);
}

static inline int qarm_expect_n(qarm_vqe *v, const double *thetas, int k, double *e)
{
    if (v->quantum) {
        register long x0 __asm__("x0") = 0;
        register const double *x1 __asm__("x1") = thetas;
        register long x2 __asm__("x2") = k;
        register long x3 __asm__("x3") = v->handle;
        register double *x4 __asm__("x4") = e;
        __asm__ volatile(QARM_PACKET(0x02220000) : "+r"(x0) : "r"(x1), "r"(x2), "r"(x3), "r"(x4) : "memory");
        if (x0 == 1) return 1;
    }
    for (int i = 0; i < k; i++)
        e[i] = qarm__energy(v->h, v->a, thetas + (long)i * v->a->params);
    return 0;
}

static inline double qarm_grad(qarm_vqe *v, const double *theta, double *grad)
{
    if (v->quantum) {
        register long x0 __asm__("x0") = 0;
        register const double *x1 __asm__("x1") = theta;
        register long x3 __asm__("x3") = v->handle;
        register double *x4 __asm__("x4") = grad;
        register double d0 __asm__("d0") = 0;
        __asm__ volatile(QARM_PACKET(0x02230000) : "+r"(x0), "+w"(d0) : "r"(x1), "r"(x3), "r"(x4) : "memory");
        if (x0 == 1) return d0;
    }
    return qarm__grad(v->h, v->a, theta, grad);
}

static inline int qarm_session(int open)
{
    register long x0 __asm__("x0") = 0;
    register long x1 __asm__("x1") = open;
    __asm__ volatile(QARM_PACKET(0x02240000) : "+r"(x0) : "r"(x1) : "memory");
    return (int)x0;
}

static inline void qarm_vqe_free(qarm_vqe *v)
{
    if (v->quantum) {
        register long x0 __asm__("x0") = 0;
        register long x3 __asm__("x3") = v->handle;
        __asm__ volatile(QARM_PACKET(0x02260000) : "+r"(x0) : "r"(x3) : "memory");
    }
    v->quantum = 0;
}
#else
static inline int qarm_vqe_load(const qarm_hamiltonian *h, const qarm_ansatz *a, qarm_vqe *v)
{
    return qarm__vqe_classical(h, a, v);
}
static inline double qarm_expect(qarm_vqe *v, const double *theta, double *err)
{
    if (err) *err = 0;
    return qarm__energy(v->h, v->a, theta);
}
static inline int qarm_expect_n(qarm_vqe *v, const double *thetas, int k, double *e)
{
    for (int i = 0; i < k; i++)
        e[i] = qarm__energy(v->h, v->a, thetas + (long)i * v->a->params);
    return 0;
}
static inline double qarm_grad(qarm_vqe *v, const double *theta, double *grad)
{
    return qarm__grad(v->h, v->a, theta, grad);
}
static inline int qarm_session(int open) { (void)open; return 0; }
static inline void qarm_vqe_free(qarm_vqe *v) { v->quantum = 0; }
static inline int qarm_available(void) { return 0; }
static inline int qarm_features(void) { return 0; }
static inline int qarm_search(int (*f)(int), int bits, int *x)
{
    for (long v = 0; v < (1L << bits); v++)
        if (f((int)v)) { *x = (int)v; return 1; }
    return 0;
}
#endif

/* without the native runtime a batch is computed at once: the job is only its result */
#include <stdlib.h>
struct qarm_job { int k, ok; double *e; };
typedef struct qarm_job qarm_job;
static inline qarm_job *qarm_expect_n_async(qarm_vqe *v, const double *thetas, int k)
{
    qarm_job *j = (qarm_job *)malloc(sizeof *j);
    j->k = k;
    j->e = (double *)malloc(sizeof(double) * (size_t)(k > 0 ? k : 1));
    j->ok = qarm_expect_n(v, thetas, k, j->e) >= 0;
    return j;
}
static inline int qarm_job_ready(qarm_job *j) { return j && j->ok ? 1 : -1; }
static inline int qarm_job_wait(qarm_job *j, double *e, double *err)
{
    int ok = j->ok;
    for (int i = 0; i < j->k; i++) {
        e[i] = j->e[i];
        if (err) err[i] = 0;
    }
    free(j->e);
    free(j);
    return ok ? 1 : -1;
}
#endif /* QARM_RUNTIME */
#endif
