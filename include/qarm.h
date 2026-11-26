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
 *   qarm_search(f, bits, &x)    an x in 0 .. 2^bits - 1 with f(x) == 1: Grover's search on the coprocessor (QSEARCH),
 *                               with f's oracle built from f's own machine code; on a plain CPU, every x in turn.
 *                               Returns 1 and stores x, or 0 if there is none. f returns 0 or 1, calls no other
 *                               function and writes no memory; bits <= 16.
 *
 * VQE (section 6 of docs/qarm_v2.md): the program's optimizer runs on the CPU; the coprocessor prepares the
 * ansatz state and measures the energy. On a plain CPU each call computes the energy exactly from the state
 * vector, in C (up to QARM_VQE_MAX_QUBITS qubits; link with -lm).
 *
 *   qarm_vqe_load(&h, &a, &v)          QVLOAD: check the tables, prepare the circuits once. 1: on the
 *                                      coprocessor, 0: classical, -1: refused (the reason on stderr)
 *   qarm_expect(&v, theta, &err)       QEXPECT: <psi(theta)|H|psi(theta)>; err: its standard error (0 if exact)
 *   qarm_expect_n(&v, thetas, k, e)    QEXPECTN: k energies for k parameter vectors, one job on a QPU
 *   qarm_session(1) / qarm_session(0)  QSESSION: reserve a QPU for the optimizer loop, release it
 *   qarm_vqe_free(&v)                  QVFREE
 *
 * Other compilers and processors get the classical path only. */
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
typedef struct { const qarm_hamiltonian *h; const qarm_ansatz *a; long handle; int quantum; } qarm_vqe;

#ifndef QARM_VQE_MAX_QUBITS
#define QARM_VQE_MAX_QUBITS 12
#endif
#include <math.h>
#include <stdio.h>

static double qarm__re[1 << QARM_VQE_MAX_QUBITS], qarm__im[1 << QARM_VQE_MAX_QUBITS];
static double qarm__tr[1 << QARM_VQE_MAX_QUBITS], qarm__ti[1 << QARM_VQE_MAX_QUBITS];

static int qarm__odd(uint64_t v) { int n = 0; while (v) { v &= v - 1; n ^= 1; } return n; }

/* (tr, ti) = P (re, im), P = i^popcount(x & z) X^x Z^z */
static void qarm__pauli(int n, uint64_t x, uint64_t z)
{
    uint64_t y = x & z;
    int k = 0;
    while (y) { y &= y - 1; k++; }
    for (long b = 0; b < (1L << n); b++) {
        double r = qarm__re[b], i = qarm__im[b], t;
        if (qarm__odd((uint64_t)b & z)) { r = -r; i = -i; }
        for (int j = 0; j < (k & 3); j++) { t = r; r = -i; i = t; }
        qarm__tr[b ^ (long)x] = r;
        qarm__ti[b ^ (long)x] = i;
    }
}

/* the classical path: the energy from the state vector */
static double qarm__energy(const qarm_hamiltonian *h, const qarm_ansatz *a, const double *theta)
{
    int n = (int)h->qubits;
    long N = 1L << n;
    for (long b = 0; b < N; b++) qarm__re[b] = qarm__im[b] = 0;
    qarm__re[a->init] = 1;
    for (uint32_t o = 0; o < a->ops; o++) {
        const qarm_op *op = &a->op[o];
        long m0 = 1L << op->q0, m1 = 1L << op->q1;
        int rot = op->kind == QARM_RY || op->kind == QARM_RZ || op->kind == QARM_PAULI_ROT;
        double ang = rot ? op->scale * theta[op->param] : 0, c, s, t;
        for (long b = 0; b < N; b++) {
            long u = b | m0;
            switch (op->kind) {
            case QARM_X:
                if (b & m0) break;
                t = qarm__re[b]; qarm__re[b] = qarm__re[u]; qarm__re[u] = t;
                t = qarm__im[b]; qarm__im[b] = qarm__im[u]; qarm__im[u] = t;
                break;
            case QARM_H:
                if (b & m0) break;
                c = 0.70710678118654752440;
                t = qarm__re[b]; qarm__re[b] = c * (t + qarm__re[u]); qarm__re[u] = c * (t - qarm__re[u]);
                t = qarm__im[b]; qarm__im[b] = c * (t + qarm__im[u]); qarm__im[u] = c * (t - qarm__im[u]);
                break;
            case QARM_RY:
                if (b & m0) break;
                c = cos(ang / 2); s = sin(ang / 2);
                t = qarm__re[b]; qarm__re[b] = c * t - s * qarm__re[u]; qarm__re[u] = s * t + c * qarm__re[u];
                t = qarm__im[b]; qarm__im[b] = c * t - s * qarm__im[u]; qarm__im[u] = s * t + c * qarm__im[u];
                break;
            case QARM_RZ:                                   /* exp(-i ang Z / 2) */
                c = cos(ang / 2); s = (b & m0) ? sin(ang / 2) : -sin(ang / 2);
                t = qarm__re[b]; qarm__re[b] = c * t - s * qarm__im[b]; qarm__im[b] = s * t + c * qarm__im[b];
                break;
            case QARM_CX:
                if (!(b & m0) || (b & m1)) break;
                u = b | m1;
                t = qarm__re[b]; qarm__re[b] = qarm__re[u]; qarm__re[u] = t;
                t = qarm__im[b]; qarm__im[b] = qarm__im[u]; qarm__im[u] = t;
                break;
            case QARM_CZ:
                if ((b & m0) && (b & m1)) { qarm__re[b] = -qarm__re[b]; qarm__im[b] = -qarm__im[b]; }
                break;
            }
        }
        if (op->kind == QARM_PAULI_ROT) {               /* cos(ang) psi - i sin(ang) P psi */
            qarm__pauli(n, op->x, op->z);
            c = cos(ang); s = sin(ang);
            for (long b = 0; b < N; b++) {
                t = qarm__re[b];
                qarm__re[b] = c * t + s * qarm__ti[b];
                qarm__im[b] = c * qarm__im[b] - s * qarm__tr[b];
            }
        }
    }
    double e = h->constant;
    for (uint32_t j = 0; j < h->terms; j++) {
        double v = 0;
        qarm__pauli(n, h->t[j].x, h->t[j].z);
        for (long b = 0; b < N; b++) v += qarm__re[b] * qarm__tr[b] + qarm__im[b] * qarm__ti[b];
        e += h->t[j].c * v;
    }
    return e;
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

static inline int qarm_search(int (*f)(int), int bits, int *x)
{
    register long x0 __asm__("x0") = 0;                     /* 0: not executed, 1: found, 2: none */
    register int (*x1)(int) __asm__("x1") = f;
    register long x2 __asm__("x2") = bits;
    register long x3 __asm__("x3") = 0;
    __asm__ volatile(QARM_PACKET(0x02110000) : "+r"(x0), "+r"(x3) : "r"(x1), "r"(x2) : "memory");
    if (x0 == 1) { *x = (int)x3; return 1; }
    if (x0 == 2) return 0;
    for (long v = 0; v < (1L << bits); v++)                 /* the classical path */
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
static inline int qarm_session(int open) { (void)open; return 0; }
static inline void qarm_vqe_free(qarm_vqe *v) { v->quantum = 0; }
static inline int qarm_available(void) { return 0; }
static inline int qarm_search(int (*f)(int), int bits, int *x)
{
    for (long v = 0; v < (1L << bits); v++)
        if (f((int)v)) { *x = (int)v; return 1; }
    return 0;
}
#endif
#endif
