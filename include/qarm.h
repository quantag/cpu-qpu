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
 *   qarm_min(cost, bits, &x, &v)  QMIN: an x with the least cost(x) (signed), and v = cost(x): Durr-Hoyer, Grover
 *                               searches for a cheaper x, cost's oracle from its machine code; on a plain CPU every x
 *                               in turn. The answer's cost is checked by calling cost; that it is the least is not
 *                               (that is the search's job), so a quantum answer is right with high probability
 *   qarm_collision(f, bits, &x, &y)  QCOLLISION: x != y with f(x) = f(y): Brassard-Hoyer-Tapp, O(N^(1/3)) oracle
 *                               calls; checked by calling f; classically f's values sorted (up to 16 bits). 1, or 0
 *   qarm_run(&circuit, shots, samples)  QCIRC: any circuit as a table (qarm_gate: X Y Z H S SDG T TDG SX RX RY RZ
 *                               CX CZ SWAP CCX), every qubit measured at the end; samples[i] bit q = qubit q. 1: run
 *                               by the coprocessor (qarm_caps says whether it is hardware), 0: simulated here (up
 *                               to QARM_VQE_MAX_QUBITS qubits), -1: refused
 *   qarm_rand(buf, n)           QRAND: n random bytes. 1: measurements on quantum hardware (8 qubits in H, one shot
 *                               per byte); 0: pseudo-random (a simulator's measurements, or made here)
 *   qarm_caps(&caps)            QCAPS: the coprocessor: features, qubits, backend name, flags (hardware, measured)
 *   qarm_mean(g, bits, k, &err) QAMPEST: the mean of g(x) over all x, g(x) in 0 .. 2^k - 1: amplitude estimation;
 *                               err: its error bound (probability >= 8/pi^2), 0 when computed classically
 *   qarm_period(f, bits, &r)    QPERIOD: the least r > 0 with f(x + r) = f(x): Shor's period finding (quantum Fourier
 *                               transform, continued fractions); checked on f(0..3). 0 if none is found
 *   qarm_simon(f, bits, &s)     QSIMON: s != 0 with f(x) = f(x ^ s) for all x (Simon); checked on f(0..3). 0: none
 *   qarm_bv(f, bits, &s)        QBV: s with f(x) = s.x mod 2 (+ a constant): one query (Bernstein-Vazirani); checked
 *                               on four inputs
 *   qarm_count(f, bits, &err)   QCOUNT: the number of x with f(x) = 1: quantum counting; err: its error bound, which
 *                               holds with probability >= 8/pi^2 (0 when
 *                               counted classically)
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
 *   qarm_vsample(&v, theta, shots, s)  QVSAMPLE: shots bitstrings of |psi(theta)> measured in the Z basis (bit q =
 *                                      qubit q): the answer of a QAOA or VQE run. 1: on the coprocessor, 0:
 *                                      sampled here, -1: refused
 *   qarm_evolve(&e, out, &info)        QEVOLVE (ISA 1.1): <O(t)> for k times with a guaranteed error bound; the
 *                                      runtime chooses exact, a product formula (order 1, 2, 4, 6) or qDRIFT
 *   qarm_evolve_n(&h, &o, init, t, k, steps, ops, cap, out)  time evolution on the 1.0 instructions: <O(t)> for k
 *                                      times, second-order Trotter tables (qarm_trotter) on QVLOAD and QEXPECTN
 *   qarm_session(1) / qarm_session(0)  QSESSION: reserve a QPU for the optimizer loop, release it
 *   qarm_vqe_free(&v)                  QVFREE
 *
 *   qarm_expect_n_async(&v, thetas, k) a batch that may still be on the QPU: a qarm_job
 *   qarm_job_ready(job)                1 done, 0 not yet, -1 failed; never waits
 *   (thetas must stay valid until qarm_job_wait: a runtime reads them at once, the classical path at the end)
 *   qarm_submit(&req), qarm_poll(&req), qarm_wait(&req)   QSUBMIT, QPOLL, QWAIT: any instruction as a qarm_request
 *                                      (arm64 packet builds; QARM_HAVE_SUBMIT)
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
#include <stdlib.h>

#include "qarm_isa.h"                          /* the ISA's constants and layouts (docs/qarm_isa.md) */

typedef struct { const qarm_hamiltonian *h; const qarm_ansatz *a; intptr_t handle; int quantum; } qarm_vqe;

/* the classical paths of QMIN and QCOUNT */
static inline int qarm__min(int (*cost)(int), int bits, int *x, int *value)
{
    int bx = 0, bv = cost(0);
    for (long v = 1; v < (1L << bits); v++) {
        int c = cost((int)v);
        if (c < bv) { bv = c; bx = (int)v; }
    }
    *x = bx;
    if (value) *value = bv;
    return 1;
}
/* QCOLLISION's classical path: f's values sorted (up to 16 bits), equal neighbours. 1: found, 0: one-to-one */
static int qarm__cmp_pair(const void *a, const void *b)
{
    const int64_t *p = (const int64_t *)a, *q = (const int64_t *)b;
    return *p < *q ? -1 : *p > *q;
}
static inline int qarm__collision(int (*f)(int), int bits, int *x, int *y)
{
    static int64_t t[1 << 16];                           /* f(v) in the high 32 bits, v in the low 32 */
    if (bits < 1 || bits > 16) return -1;
    long N = 1L << bits;
    for (long v = 0; v < N; v++) t[v] = (int64_t)((uint64_t)(uint32_t)f((int)v) << 32 | (uint64_t)v);
    qsort(t, (size_t)N, sizeof t[0], qarm__cmp_pair);
    for (long i = 1; i < N; i++)
        if ((t[i] >> 32) == (t[i - 1] >> 32)) { *x = (int)(t[i - 1] & 0xFFFFFFFF); *y = (int)(t[i] & 0xFFFFFFFF); return 1; }
    return 0;
}
static inline int qarm__is_collision(int (*f)(int), int bits, int x, int y)
{
    return x != y && x >= 0 && y >= 0 && x < (1 << bits) && y < (1 << bits) && f(x) == f(y);
}
/* checks of QPERIOD's, QSIMON's and QBV's answers, and their classical paths */
static inline int qarm__is_period(int (*f)(int), int r)
{
    for (int i = 0; i < 4; i++)
        if (f(i) != f(i + r)) return 0;
    return r > 0;
}
static inline int qarm__is_shift(int (*f)(int), int s)
{
    for (int i = 0; i < 4; i++)
        if (f(i) != f(i ^ s)) return 0;
    return s != 0;
}
static inline int qarm__is_bv(int (*f)(int), int bits, int s)
{
    static const int probe[4] = {0x5A5A5A5, 0x3333333, 0x0F0F0F0, 0x7FFFFFF};
    int b0 = f(0) & 1;
    for (int i = 0; i < 4; i++) {
        int x = probe[i] & (int)((1L << bits) - 1), p = 0;
        for (int v = x & s; v; v &= v - 1) p ^= 1;
        if ((f(x) & 1) != (p ^ b0)) return 0;
    }
    return 1;
}
static inline int qarm__period(int (*f)(int), int bits, int *r)
{
    for (long v = 1; v < (1L << bits); v++)
        if (qarm__is_period(f, (int)v)) { *r = (int)v; return 1; }
    return 0;
}
static inline int qarm__simon(int (*f)(int), int bits, int *s)
{
    int f0 = f(0);
    for (long v = 1; v < (1L << bits); v++)            /* s is f(0)'s other input: f(0) = f(0 ^ s) */
        if (f((int)v) == f0 && qarm__is_shift(f, (int)v)) { *s = (int)v; return 1; }
    return 0;
}
static inline int qarm__bv(int (*f)(int), int bits, int *s)
{
    int b0 = f(0) & 1;
    *s = 0;
    for (int i = 0; i < bits; i++) *s |= ((f(1 << i) & 1) ^ b0) << i;
    return 1;
}
static inline double qarm__mean(int (*g)(int), int bits, double *err)
{
    double s = 0;
    for (long v = 0; v < (1L << bits); v++) s += g((int)v);
    if (err) *err = 0;
    return s / (double)(1L << bits);
}
static inline long qarm__count(int (*f)(int), int bits, double *err)
{
    long n = 0;
    for (long v = 0; v < (1L << bits); v++) n += f((int)v) == 1;
    if (err) *err = 0;
    return n;
}

#if defined(QARM_RUNTIME)
#include <math.h>
#include "qarm_rt.h"

static inline int qarm_available(void) { return 0; }
static inline int qarm_features(void) { return 0; }
static inline int qarm_min(int (*cost)(int), int bits, int *x, int *value) { return qarm__min(cost, bits, x, value); }
static inline int qarm_collision(int (*f)(int), int bits, int *x, int *y) { return qarm__collision(f, bits, x, y); }
static inline int qarm_period(int (*f)(int), int bits, int *r) { return qarm__period(f, bits, r); }
static inline double qarm_mean(int (*g)(int), int bits, int k, double *err) { (void)k; return qarm__mean(g, bits, err); }
static inline int qarm_simon(int (*f)(int), int bits, int *s) { return qarm__simon(f, bits, s); }
static inline int qarm_bv(int (*f)(int), int bits, int *s) { return qarm__bv(f, bits, s); }
static inline long qarm_count(int (*f)(int), int bits, double *err) { return qarm__count(f, bits, err); }
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
static inline int qarm_run(const qarm_circuit *c, int shots, uint64_t *samples)
{
    return qarm_rt_circuit(c, shots, samples);
}
static inline int qarm_vsample(qarm_vqe *v, const double *theta, int shots, uint64_t *samples)
{
    if (!v->handle) return -1;
    return qarm_rt_vsample((qarm_rt *)v->handle, theta, shots, samples) > 0 ? v->quantum : -1;
}
static inline int qarm_rand(void *buf, int n) { return qarm_rt_rand(buf, n); }
static inline int qarm_caps(qarm_capabilities *caps) { return qarm_rt_caps(caps); }
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

/* QCIRC's, QRAND's, QCAPS's and QVSAMPLE's classical paths */
static uint64_t qarm__sample_rng = 0x2545F4914F6CDD1DULL;
static double qarm__uniform(void)
{
    uint64_t z = (qarm__sample_rng += 0x9E3779B97F4A7C15ULL);
    z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ULL;
    z = (z ^ (z >> 27)) * 0x94D049BB133111EBULL;
    return (double)((z ^ (z >> 31)) >> 11) / 9007199254740992.0;
}
static void qarm__draw(qarm_sv *s, int shots, uint64_t *samples)    /* shots outcomes of the state in s */
{
    long N = 1L << s->n;
    for (long b = 0; b < N; b++) s->tr[b] = (b ? s->tr[b - 1] : 0) + s->re[b] * s->re[b] + s->im[b] * s->im[b];
    for (int k = 0; k < shots; k++) {
        double u = qarm__uniform() * s->tr[N - 1];
        long lo = 0, hi = N - 1;
        while (lo < hi) { long mid = (lo + hi) / 2; if (u < s->tr[mid]) hi = mid; else lo = mid + 1; }
        samples[k] = (uint64_t)lo;
    }
}
static int qarm__vsample(const qarm_ansatz *a, const double *theta, int shots, uint64_t *samples)
{
    if (shots < 0 || a->qubits < 1 || a->qubits > QARM_VQE_MAX_QUBITS) return -1;
    qarm_sv s = qarm__sv((int)a->qubits);
    qarm_sv_prepare(&s, a, theta);
    qarm__draw(&s, shots, samples);
    return 0;
}
static int qarm__run(const qarm_circuit *c, int shots, uint64_t *samples)
{
    if (c->qubits < 1 || c->qubits > QARM_VQE_MAX_QUBITS || shots < 0) return -1;
    for (uint32_t i = 0; i < c->gates; i++)
        if (!qarm_sv_gate_ok(&c->g[i], c->qubits)) return -1;
    qarm_sv s = qarm__sv((int)c->qubits);
    qarm_sv_circuit(&s, c);
    qarm__draw(&s, shots, samples);
    return 0;
}
static int qarm__rand(void *buf, int n)
{
    for (int i = 0; i < n; i++) ((unsigned char *)buf)[i] = (unsigned char)(qarm__uniform() * 256);
    return 0;
}
static int qarm__caps(qarm_capabilities *c)
{
    *c = (qarm_capabilities){1, 0, QARM_VQE_MAX_QUBITS, 0, "classical"};
    return 0;
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

#if (defined(__aarch64__) || defined(__x86_64__)) && (defined(__GNUC__) || defined(__clang__))
#if defined(__aarch64__)
#define QARM_PACKET(op) \
    ".inst 0x14000008\n\t"            /* B .+32: a plain CPU continues after the packet */ \
    ".inst 0x4d524151\n\t"            /* "QARM" */ \
    ".inst " #op "\n\t"               /* version 2, opcode */ \
    ".inst 0\n\t.inst 0\n\t.inst 0\n\t.inst 0\n\t.inst 0\n\t"
#define QARM_X0 "x0"                     /* the operand registers X0..X5, D0, D1 (docs/qarm_isa.md, section 3) */
#define QARM_X1 "x1"
#define QARM_X2 "x2"
#define QARM_X3 "x3"
#define QARM_X4 "x4"
#define QARM_X5 "x5"
#define QARM_D0 "d0"
#define QARM_D1 "d1"
#define QARM_FP "w"                      /* the constraint of a floating-point register */
#else                                    /* x86-64: the same packet behind JMP +30 (docs/qarm_isa.md, section 2.1) */
#define QARM_PACKET(op) \
    ".byte 0xeb, 0x1e, 0, 0\n\t"       /* JMP .+32: a plain CPU continues after the packet */ \
    ".long 0x4d524151\n\t"            /* "QARM" */ \
    ".long " #op "\n\t"               /* version 2, opcode */ \
    ".long 0, 0, 0, 0, 0\n\t"
#define QARM_X0 "rax"                    /* X0..X5 are RAX RDI RSI RDX RCX R8; D0, D1 are XMM0, XMM1 */
#define QARM_X1 "rdi"
#define QARM_X2 "rsi"
#define QARM_X3 "rdx"
#define QARM_X4 "rcx"
#define QARM_X5 "r8"
#define QARM_D0 "xmm0"
#define QARM_D1 "xmm1"
#define QARM_FP "x"
#endif
#define QARM_OP_QAVAIL  0x02100000
#define QARM_OP_QSEARCH 0x02110000

static inline int qarm_available(void)
{
    register long x0 __asm__(QARM_X0) = 0;                     /* a plain CPU leaves 0 */
    __asm__ volatile(QARM_PACKET(0x02100000) : "+r"(x0) : : "memory");
    return (int)x0;
}

static inline int qarm_features(void)
{
    register long x0 __asm__(QARM_X0) = 0;
    register long x1 __asm__(QARM_X1) = 0;
    __asm__ volatile(QARM_PACKET(0x02100000) : "+r"(x0), "+r"(x1) : : "memory");
    return x0 == 1 ? (int)(x1 ? x1 : QARM_F_SEARCH) : 0;     /* a runtime without feature bits: QSEARCH only */
}

static inline int qarm_search(int (*f)(int), int bits, int *x)
{
    register long x0 __asm__(QARM_X0) = 0;                     /* 0: not executed, 1: found, 2: none */
    register int (*x1)(int) __asm__(QARM_X1) = f;
    register long x2 __asm__(QARM_X2) = bits;
    register long x3 __asm__(QARM_X3) = 0;
    __asm__ volatile(QARM_PACKET(0x02110000) : "+r"(x0), "+r"(x3) : "r"(x1), "r"(x2) : "memory");
    if (x0 == 1 && f((int)x3) == 1) { *x = (int)x3; return 1; }   /* every answer checked by f itself */
    if (x0 == 2) return 0;
    for (long v = 0; v < (1L << bits); v++)                 /* the classical path, also after a wrong answer */
        if (f((int)v)) { *x = (int)v; return 1; }
    return 0;
}

static inline int qarm_vqe_load(const qarm_hamiltonian *h, const qarm_ansatz *a, qarm_vqe *v)
{
    register long x0 __asm__(QARM_X0) = 0;                     /* 0: not executed, 1: loaded, 2: refused */
    register const qarm_hamiltonian *x1 __asm__(QARM_X1) = h;
    register const qarm_ansatz *x2 __asm__(QARM_X2) = a;
    register long x3 __asm__(QARM_X3) = 0;
    __asm__ volatile(QARM_PACKET(0x02200000) : "+r"(x0), "+r"(x3) : "r"(x1), "r"(x2) : "memory");
    if (x0 == 1) { v->h = h; v->a = a; v->handle = x3; v->quantum = 1; return 1; }
    if (x0 == 2) { v->quantum = 0; return -1; }
    return qarm__vqe_classical(h, a, v);
}

static inline double qarm_expect(qarm_vqe *v, const double *theta, double *err)
{
    if (v->quantum) {
        register long x0 __asm__(QARM_X0) = 0;
        register const double *x1 __asm__(QARM_X1) = theta;
        register long x2 __asm__(QARM_X2) = 0;                 /* shots: config.json's */
        register long x3 __asm__(QARM_X3) = v->handle;
        register double d0 __asm__(QARM_D0) = 0, d1 __asm__(QARM_D1) = 0;
        __asm__ volatile(QARM_PACKET(0x02210000) : "+r"(x0), "+" QARM_FP(d0), "+" QARM_FP(d1) : "r"(x1), "r"(x2), "r"(x3)
                         : "memory");
        if (x0 == 1) { if (err) *err = d1; return d0; }
    }
    if (err) *err = 0;
    return qarm__energy(v->h, v->a, theta);
}

static inline int qarm_expect_n(qarm_vqe *v, const double *thetas, int k, double *e)
{
    if (v->quantum) {
        register long x0 __asm__(QARM_X0) = 0;
        register const double *x1 __asm__(QARM_X1) = thetas;
        register long x2 __asm__(QARM_X2) = k;
        register long x3 __asm__(QARM_X3) = v->handle;
        register double *x4 __asm__(QARM_X4) = e;
        __asm__ volatile(QARM_PACKET(0x02220000) : "+r"(x0) : "r"(x1), "r"(x2), "r"(x3), "r"(x4) : "memory");
        if (x0 == 1) return 1;
    }
    for (int i = 0; i < k; i++)
        e[i] = qarm__energy(v->h, v->a, thetas + (long)i * v->a->params);
    return 0;
}

static inline int qarm_min(int (*cost)(int), int bits, int *x, int *value)
{
    register long x0 __asm__(QARM_X0) = 0;
    register int (*x1)(int) __asm__(QARM_X1) = cost;
    register long x2 __asm__(QARM_X2) = bits;
    register long x3 __asm__(QARM_X3) = 0;
    register long x4 __asm__(QARM_X4) = 0;
    __asm__ volatile(QARM_PACKET(0x02150000) : "+r"(x0), "+r"(x3), "+r"(x4) : "r"(x1), "r"(x2) : "memory");
    if (x0 == 1 && cost((int)x3) == (int)x4) {             /* the answer's cost checked by cost itself */
        *x = (int)x3;
        if (value) *value = (int)x4;
        return 1;
    }
    return qarm__min(cost, bits, x, value);
}

/* QCOLLISION 0x1B: X1 = f, W2 = bits -> W0 = 1, W3 = x, W4 = y; W0 = 2: none found */
static inline int qarm_collision(int (*f)(int), int bits, int *x, int *y)
{
    register long x0 __asm__(QARM_X0) = 0;
    register int (*x1)(int) __asm__(QARM_X1) = f;
    register long x2 __asm__(QARM_X2) = bits;
    register long x3 __asm__(QARM_X3) = 0;
    register long x4 __asm__(QARM_X4) = 0;
    __asm__ volatile(QARM_PACKET(0x021b0000) : "+r"(x0), "+r"(x3), "+r"(x4) : "r"(x1), "r"(x2) : "memory");
    if (x0 == 1 && qarm__is_collision(f, bits, (int)x3, (int)x4)) {   /* checked by calling f twice */
        *x = (int)x3;
        *y = (int)x4;
        return 1;
    }
    return qarm__collision(f, bits, x, y);
}

/* QPERIOD 0x18, QSIMON 0x19, QBV 0x1A: X1 = f, W2 = bits -> W0 = 1 and W3 = the answer, W0 = 2: none */
static inline long qarm__structure(unsigned op, int (*f)(int), int bits, int *out)
{
    register long x0 __asm__(QARM_X0) = 0;
    register int (*x1)(int) __asm__(QARM_X1) = f;
    register long x2 __asm__(QARM_X2) = bits;
    register long x3 __asm__(QARM_X3) = 0;
    if (op == 0x18) __asm__ volatile(QARM_PACKET(0x02180000) : "+r"(x0), "+r"(x3) : "r"(x1), "r"(x2) : "memory");
    else if (op == 0x19) __asm__ volatile(QARM_PACKET(0x02190000) : "+r"(x0), "+r"(x3) : "r"(x1), "r"(x2) : "memory");
    else __asm__ volatile(QARM_PACKET(0x021a0000) : "+r"(x0), "+r"(x3) : "r"(x1), "r"(x2) : "memory");
    *out = (int)x3;
    return x0;
}
static inline int qarm_period(int (*f)(int), int bits, int *r)
{
    long s = qarm__structure(0x18, f, bits, r);
    if (s == 1 && qarm__is_period(f, *r)) return 1;
    if (s == 2) return 0;
    return qarm__period(f, bits, r);
}
static inline int qarm_simon(int (*f)(int), int bits, int *sh)
{
    long s = qarm__structure(0x19, f, bits, sh);
    if (s == 1 && qarm__is_shift(f, *sh)) return 1;
    if (s == 2) return 0;
    return qarm__simon(f, bits, sh);
}
static inline int qarm_bv(int (*f)(int), int bits, int *sv)
{
    long s = qarm__structure(0x1a, f, bits, sv);
    if (s == 1 && qarm__is_bv(f, bits, *sv)) return 1;
    return qarm__bv(f, bits, sv);
}

static inline double qarm_mean(int (*g)(int), int bits, int k, double *err)
{
    register long x0 __asm__(QARM_X0) = 0;
    register int (*x1)(int) __asm__(QARM_X1) = g;
    register long x2 __asm__(QARM_X2) = bits;
    register long x3 __asm__(QARM_X3) = k;
    register double d0 __asm__(QARM_D0) = 0, d1 __asm__(QARM_D1) = 0;
    __asm__ volatile(QARM_PACKET(0x02170000) : "+r"(x0), "+" QARM_FP(d0), "+" QARM_FP(d1) : "r"(x1), "r"(x2), "r"(x3) : "memory");
    if (x0 == 1) {
        if (err) *err = d1;
        return d0;
    }
    return qarm__mean(g, bits, err);
}

static inline long qarm_count(int (*f)(int), int bits, double *err)
{
    register long x0 __asm__(QARM_X0) = 0;
    register int (*x1)(int) __asm__(QARM_X1) = f;
    register long x2 __asm__(QARM_X2) = bits;
    register long x3 __asm__(QARM_X3) = 0;
    register double d0 __asm__(QARM_D0) = 0;
    __asm__ volatile(QARM_PACKET(0x02160000) : "+r"(x0), "+r"(x3), "+" QARM_FP(d0) : "r"(x1), "r"(x2) : "memory");
    if (x0 == 1) {
        if (err) *err = d0;
        return x3;
    }
    return qarm__count(f, bits, err);
}

static inline int qarm_run(const qarm_circuit *c, int shots, uint64_t *samples)
{
    register long x0 __asm__(QARM_X0) = 0;                     /* 0: not executed, 1: sampled, 2: refused */
    register const qarm_circuit *x1 __asm__(QARM_X1) = c;
    register long x2 __asm__(QARM_X2) = shots;
    register uint64_t *x3 __asm__(QARM_X3) = samples;
    __asm__ volatile(QARM_PACKET(0x02300000) : "+r"(x0) : "r"(x1), "r"(x2), "r"(x3) : "memory");
    if (x0 == 1) return 1;
    if (x0 == 2) return -1;
    return qarm__run(c, shots, samples);
}

static inline int qarm_rand(void *buf, int n)
{
    register long x0 __asm__(QARM_X0) = 0;                     /* 1: hardware's bytes, 3: a simulator's */
    register void *x1 __asm__(QARM_X1) = buf;
    register long x2 __asm__(QARM_X2) = n;
    __asm__ volatile(QARM_PACKET(0x021c0000) : "+r"(x0) : "r"(x1), "r"(x2) : "memory");
    if (x0 == 1 || x0 == 3) return x0 == 1;
    return qarm__rand(buf, n);
}

static inline int qarm_caps(qarm_capabilities *caps)
{
    register long x0 __asm__(QARM_X0) = 0;
    register qarm_capabilities *x1 __asm__(QARM_X1) = caps;
    __asm__ volatile(QARM_PACKET(0x02310000) : "+r"(x0) : "r"(x1) : "memory");
    return x0 == 1 ? 1 : qarm__caps(caps);
}

static inline double qarm_grad(qarm_vqe *v, const double *theta, double *grad)
{
    if (v->quantum) {
        register long x0 __asm__(QARM_X0) = 0;
        register const double *x1 __asm__(QARM_X1) = theta;
        register long x3 __asm__(QARM_X3) = v->handle;
        register double *x4 __asm__(QARM_X4) = grad;
        register double d0 __asm__(QARM_D0) = 0;
        __asm__ volatile(QARM_PACKET(0x02230000) : "+r"(x0), "+" QARM_FP(d0) : "r"(x1), "r"(x3), "r"(x4) : "memory");
        if (x0 == 1) return d0;
    }
    return qarm__grad(v->h, v->a, theta, grad);
}

static inline int qarm_vsample(qarm_vqe *v, const double *theta, int shots, uint64_t *samples)
{
    if (v->quantum) {
        register long x0 __asm__(QARM_X0) = 0;                 /* 1: sampled, 2: refused */
        register const double *x1 __asm__(QARM_X1) = theta;
        register long x2 __asm__(QARM_X2) = shots;
        register long x3 __asm__(QARM_X3) = v->handle;
        register uint64_t *x4 __asm__(QARM_X4) = samples;
        __asm__ volatile(QARM_PACKET(0x02250000) : "+r"(x0) : "r"(x1), "r"(x2), "r"(x3), "r"(x4) : "memory");
        if (x0 == 1) return 1;
        if (x0 == 2) return -1;
    }
    return qarm__vsample(v->a, theta, shots, samples);
}

static inline int qarm_session(int open)
{
    register long x0 __asm__(QARM_X0) = 0;
    register long x1 __asm__(QARM_X1) = open;
    __asm__ volatile(QARM_PACKET(0x02240000) : "+r"(x0) : "r"(x1) : "memory");
    return (int)x0;
}

static inline void qarm_vqe_free(qarm_vqe *v)
{
    if (v->quantum) {
        register long x0 __asm__(QARM_X0) = 0;
        register long x3 __asm__(QARM_X3) = v->handle;
        __asm__ volatile(QARM_PACKET(0x02260000) : "+r"(x0) : "r"(x3) : "memory");
    }
    v->quantum = 0;
}

/* 1: the runtime took the request (state 0: running, 1: done); 0: not executed (no runtime, or not async) */
static inline int qarm_submit(qarm_request *r)
{
    register long x0 __asm__(QARM_X0) = 0;
    register qarm_request *x1 __asm__(QARM_X1) = r;
    r->state = 0;
    __asm__ volatile(QARM_PACKET(0x02120000) : "+r"(x0) : "r"(x1) : "memory");
    return x0 == 1;
}
static inline int qarm_poll(qarm_request *r)              /* 1 done, 0 running; never waits */
{
    register long x0 __asm__(QARM_X0) = 0;
    register long x1 __asm__(QARM_X1) = (long)(intptr_t)r;
    __asm__ volatile(QARM_PACKET(0x02130000) : "+r"(x0), "+r"(x1) : : "memory");
    return x0 == 1 ? (int)x1 : 1;
}
static inline void qarm_wait(qarm_request *r)
{
    register long x0 __asm__(QARM_X0) = 0;
    register qarm_request *x1 __asm__(QARM_X1) = r;
    __asm__ volatile(QARM_PACKET(0x02140000) : "+r"(x0) : "r"(x1) : "memory");
}
#define QARM_HAVE_SUBMIT 1
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
static inline int qarm_run(const qarm_circuit *c, int shots, uint64_t *samples) { return qarm__run(c, shots, samples); }
static inline int qarm_vsample(qarm_vqe *v, const double *theta, int shots, uint64_t *samples)
{
    return qarm__vsample(v->a, theta, shots, samples);
}
static inline int qarm_rand(void *buf, int n) { return qarm__rand(buf, n); }
static inline int qarm_caps(qarm_capabilities *caps) { return qarm__caps(caps); }
static inline void qarm_vqe_free(qarm_vqe *v) { v->quantum = 0; }
static inline int qarm_available(void) { return 0; }
static inline int qarm_features(void) { return 0; }
static inline int qarm_min(int (*cost)(int), int bits, int *x, int *value) { return qarm__min(cost, bits, x, value); }
static inline int qarm_collision(int (*f)(int), int bits, int *x, int *y) { return qarm__collision(f, bits, x, y); }
static inline int qarm_period(int (*f)(int), int bits, int *r) { return qarm__period(f, bits, r); }
static inline double qarm_mean(int (*g)(int), int bits, int k, double *err) { (void)k; return qarm__mean(g, bits, err); }
static inline int qarm_simon(int (*f)(int), int bits, int *s) { return qarm__simon(f, bits, s); }
static inline int qarm_bv(int (*f)(int), int bits, int *s) { return qarm__bv(f, bits, s); }
static inline long qarm_count(int (*f)(int), int bits, double *err) { return qarm__count(f, bits, err); }
static inline int qarm_search(int (*f)(int), int bits, int *x)
{
    for (long v = 0; v < (1L << bits); v++)
        if (f((int)v)) { *x = (int)v; return 1; }
    return 0;
}
#endif

/* without the native runtime: QEXPECTN through QSUBMIT when a runtime takes it (under QEMU with libqarm's ibm
 * backend the job really runs while the program does), else the batch computed at once */
#include <stdlib.h>
struct qarm_job { int k, ok, pending; double *e; qarm_vqe *v; const double *thetas; qarm_request r; };
typedef struct qarm_job qarm_job;
static inline qarm_job *qarm_expect_n_async(qarm_vqe *v, const double *thetas, int k)
{
    qarm_job *j = (qarm_job *)calloc(1, sizeof *j);
    j->k = k;
    j->v = v;
    j->thetas = thetas;
    j->e = (double *)malloc(sizeof(double) * (size_t)(k > 0 ? k : 1));
#ifdef QARM_HAVE_SUBMIT
    if (v->quantum) {
        j->r.opcode = 0x22;
        j->r.x[1] = (uint64_t)(uintptr_t)thetas;
        j->r.x[2] = (uint64_t)k;
        j->r.x[3] = (uint64_t)v->handle;
        j->r.x[4] = (uint64_t)(uintptr_t)j->e;
        if (qarm_submit(&j->r)) { j->pending = 1; j->ok = 1; return j; }
    }
#endif
    j->ok = qarm_expect_n(v, thetas, k, j->e) >= 0;
    return j;
}
static inline int qarm_job_ready(qarm_job *j)
{
    if (!j || !j->ok) return -1;
#ifdef QARM_HAVE_SUBMIT
    if (j->pending) return qarm_poll(&j->r);
#endif
    return 1;
}
static inline int qarm_job_wait(qarm_job *j, double *e, double *err)
{
#ifdef QARM_HAVE_SUBMIT
    if (j->pending) {
        qarm_wait(&j->r);
        if (j->r.x[0] != 1) j->ok = qarm_expect_n(j->v, j->thetas, j->k, j->e) >= 0;   /* not executed: here */
    }
#endif
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

/* ---------------------------------------------------------------- time evolution, on the VQE instructions
 * exp(-i H t) as an ansatz table: second-order (symmetric) Trotter steps, each Pauli term of h a QARM_PAULI_ROT.
 * The rotations' angles are scale * theta[0] with theta[0] = dt = t / steps, so one table serves every t: k times
 * are k parameter vectors of one QEXPECTN batch (one job on a QPU). */
static inline uint32_t qarm_trotter(const qarm_hamiltonian *h, int steps, uint64_t init, qarm_op *ops, uint32_t cap,
                                    qarm_ansatz *a)
{
    uint32_t n = 0;
    for (int s = 0; s < steps; s++)
        for (int half = 0; half < 2; half++)
            for (uint32_t i = 0; i < h->terms; i++) {
                const qarm_pauli *p = &h->t[half ? h->terms - 1 - i : i];
                if (!p->x && !p->z) continue;                        /* the identity: a global phase */
                if (n && ops[n - 1].x == p->x && ops[n - 1].z == p->z) { ops[n - 1].scale += p->c / 2; continue; }
                if (n == cap) return 0;
                ops[n++] = (qarm_op){QARM_PAULI_ROT, 0, 0, 0, 0, p->c / 2, p->x, p->z};
            }
    *a = (qarm_ansatz){h->qubits, 1, n, 0, init, ops};
    return n;
}

/* <O(t)> = <init| exp(iHt) O exp(-iHt) |init> for k times, steps Trotter steps each; ops: room for the table
 * (about 2 * steps * terms). 1: on the coprocessor, 0: on this CPU, -1: refused */
static inline int qarm_evolve_n(const qarm_hamiltonian *h, const qarm_hamiltonian *o, uint64_t init, const double *t,
                                int k, int steps, qarm_op *ops, uint32_t cap, double *out)
{
    qarm_ansatz a;
    qarm_vqe v;
    if (steps < 1 || k < 1 || o->qubits != h->qubits || !qarm_trotter(h, steps, init, ops, cap, &a)) return -1;
    int where = qarm_vqe_load(o, &a, &v);
    if (where < 0) return -1;
    for (int i = 0; i < k; i++) out[i] = t[i] / steps;      /* the parameter vectors, replaced by the energies */
    int ok = qarm_expect_n(&v, out, k, out);
    qarm_vqe_free(&v);
    return ok < 0 ? -1 : where;
}

/* ---------------------------------------------------------------- QEVOLVE (docs/qarm_isa.md 6.6, ISA 1.1)
 * out[i] = <init| exp(iHt_i) O exp(-iHt_i) |init> for the k times of e, within info->bound of the exact value for
 * every t (for qDRIFT: its mean; info->err is a standard error from shots or random sequences). The runtime picks
 * the method unless e asks for one: exact on a simulator, the cheapest product formula that meets e->eps on a QPU.
 * 1: on the coprocessor, 0: on this CPU (the same plans, exact by default, up to QARM_VQE_MAX_QUBITS qubits), -1:
 * refused (the reason on stderr). out may be e->t. */
typedef struct { double bound, err; uint32_t method, order; uint64_t steps; } qarm_evolve_info;
#if defined(QARM_RUNTIME)
static inline int qarm_evolve(const qarm_evolution *e, double *out, qarm_evolve_info *info)
{
    qarm_evolve_info i = {0, 0, 0, 0, 0};
    uint32_t how = 0;
    int w = qarm_rt_evolve(e, out, &i.bound, &i.err, &how, &i.steps);
    i.method = how & 0xff; i.order = how >> 8;
    if (info) *info = i;
    return w;
}
#else
static inline int qarm__evolve(const qarm_evolution *e, double *out, qarm_evolve_info *info)
{
    const char *why = qarm_ev_check(e);
    qarm_ev_plan_t p;
    double se = 0;
    if (why || e->h->qubits > QARM_VQE_MAX_QUBITS) {
        fprintf(stderr, "qarm_evolve: %s\n", why ? why : "more qubits than the classical path takes");
        return -1;
    }
    if (qarm_ev_plan(e, 1, 0, &p) < 0) { fprintf(stderr, "qarm_evolve: %s\n", p.why); return -1; }
    p.seed = 0x5eed;
    qarm_sv s = qarm__sv((int)e->h->qubits);
    if (qarm_sv_evolve(&s, e, &p, out, &se) < 0) return -1;
    if (info) { info->bound = p.bound; info->err = se; info->method = p.method; info->order = p.order; info->steps = p.steps; }
    return 0;
}
#ifdef QARM_PACKET
static inline int qarm_evolve(const qarm_evolution *e, double *out, qarm_evolve_info *info)
{
    register long x0 __asm__(QARM_X0) = 0;                     /* 0: not executed, 1: done, 2: refused */
    register const qarm_evolution *x1 __asm__(QARM_X1) = e;
    register double *x2 __asm__(QARM_X2) = out;
    register long x3 __asm__(QARM_X3) = 0, x4 __asm__(QARM_X4) = 0;
    register double d0 __asm__(QARM_D0) = 0, d1 __asm__(QARM_D1) = 0;
    __asm__ volatile(QARM_PACKET(0x02270000) : "+r"(x0), "+r"(x3), "+r"(x4), "+" QARM_FP(d0), "+" QARM_FP(d1)
                     : "r"(x1), "r"(x2) : "memory");
    if (x0 == 1) {
        if (info) { info->bound = d0; info->err = d1; info->method = (uint32_t)x3 & 0xff; info->order = (uint32_t)x3 >> 8 & 0xff; info->steps = (uint64_t)x4; }
        return 1;
    }
    if (x0 == 2) return -1;
    return qarm__evolve(e, out, info);
}
#else
static inline int qarm_evolve(const qarm_evolution *e, double *out, qarm_evolve_info *info) { return qarm__evolve(e, out, info); }
#endif
#endif
#endif
