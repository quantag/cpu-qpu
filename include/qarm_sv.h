/* SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 Quantag IT Solutions GmbH
 *
 * The exact state vector of Q-ARM's VQE tables (include/qarm.h): the classical path of qarm.h and libqarm's exact
 * backend. Six arrays of 2^n doubles, given by the caller:
 *   qarm_sv_prepare(&s, a, theta)            |psi(theta)> from the ansatz
 *   qarm_sv_energy(&s, h)                    <psi|H|psi>
 *   qarm_sv_grad(&s, h, a, theta, grad)      the energy and its gradient, by adjoint differentiation: one forward and
 *                                            one backward pass, whatever the number of parameters
 * Every kernel works in place, one pass over the vector: a Pauli rotation pairs |b> with |b ^ x>; an energy term is
 * a sum over those pairs; the terms of H that share an X mask share one pass (H psi and <psi|H|psi> together).
 * Built with OpenMP (-fopenmp; libqarm does it when the compiler has it), the passes over 2^n amplitudes run on all
 * cores from QARM_SV_PAR_MIN amplitudes on; without it the pragmas are ignored and the code is the same, serial.
 * OMP_NUM_THREADS sets the threads. Included by qarm.h; the tables are qarm_isa.h's.
 * Link with -lm. */
#ifndef QARM_SV_H
#define QARM_SV_H
#include <math.h>
#include <stdint.h>
#include <stdlib.h>
#include "qarm_isa.h"

#ifndef QARM_SV_PAR_MIN
#define QARM_SV_PAR_MIN 32768                    /* smaller vectors stay on one thread: the overhead would dominate */
#endif

typedef struct { int n; double *re, *im, *tr, *ti, *lr, *li; } qarm_sv;

#if defined(__GNUC__) || defined(__clang__)
#define qarm_sv_odd(v) __builtin_parityll((unsigned long long)(v))
#else
static int qarm_sv_odd(uint64_t v) { v ^= v >> 32; v ^= v >> 16; v ^= v >> 8; v ^= v >> 4; v ^= v >> 2; v ^= v >> 1; return (int)(v & 1); }
#endif
static int qarm_sv_popcount(uint64_t v) { int n = 0; while (v) { v &= v - 1; n++; } return n; }
static int qarm_sv_top(uint64_t v) { int t = 63; while (!(v >> t & 1)) t--; return t; }   /* v != 0 */

#ifdef _OPENMP
#define QARM_SV_PAR(N) QARM_SV_STR(omp parallel for schedule(static) if((N) >= QARM_SV_PAR_MIN))
#define QARM_SV_PAR_SUM(N, v) QARM_SV_STR(omp parallel for schedule(static) reduction(+:v) if((N) >= QARM_SV_PAR_MIN))
#define QARM_SV_STR(x) _Pragma(#x)
#else
#define QARM_SV_PAR(N)
#define QARM_SV_PAR_SUM(N, v)
#endif

static int qarm_sv_rot(int kind) { return kind == QARM_RY || kind == QARM_RZ || kind == QARM_PAULI_ROT; }

/* the low set bit of x: the pairs (b, b ^ x) are visited once, from the b with that bit clear */
static long qarm_sv_low(uint64_t x) { return (long)(x & (~x + 1)); }

/* b with a 0 inserted at bit position of the single-bit mask m: the k-th index whose bit m is clear */
static long qarm_sv_ins(long k, long m) { return ((k & ~(m - 1)) << 1) | (k & (m - 1)); }

/* exp(-i ang P) in place, P = i^popcount(x & z) X^x Z^z: cos(ang) psi - i sin(ang) P psi */
static void qarm_sv_pauli_rot(const qarm_sv *s, double *re, double *im, uint64_t x, uint64_t z, double ang)
{
    long N = 1L << s->n;
    double c = cos(ang), sn = sin(ang);
    int k = qarm_sv_popcount(x & z) & 3;
    /* -i sin * i^k = sin * (-i)^(1 - k): as a complex factor (fr, fi) on s(b) psi[b^x] */
    double fr = (k == 1) ? sn : (k == 3) ? -sn : 0, fi = (k == 0) ? -sn : (k == 2) ? sn : 0;
    if (!x) {                                       /* diagonal: exp(-i ang s(b)), P = +-Z..Z (k = 0) */
        QARM_SV_PAR(N)
        for (long b = 0; b < N; b++) {
            double g = qarm_sv_odd((uint64_t)b & z) ? -sn : sn, r = re[b], i = im[b];
            re[b] = c * r + g * i;
            im[b] = c * i - g * r;
        }
        return;
    }
    long m = qarm_sv_low(x), half = N >> 1;
    QARM_SV_PAR(half)
    for (long j = 0; j < half; j++) {
        long b = qarm_sv_ins(j, m), u = b ^ (long)x;
        double sb = qarm_sv_odd((uint64_t)b & z) ? -1 : 1, su = qarm_sv_odd((uint64_t)u & z) ? -1 : 1;
        double br = re[b], bi = im[b], ur = re[u], ui = im[u];
        /* (P psi)[u] = i^k s(b) psi[b], (P psi)[b] = i^k s(u) psi[u] */
        re[b] = c * br + su * (fr * ur - fi * ui);
        im[b] = c * bi + su * (fr * ui + fi * ur);
        re[u] = c * ur + sb * (fr * br - fi * bi);
        im[u] = c * ui + sb * (fr * bi + fi * br);
    }
}

/* one operation on (re, im), at angle ang (the angle of RY, RZ: exp(-i ang G/2); of a Pauli rotation: exp(-i ang P)) */
static void qarm_sv_apply(qarm_sv *s, double *re, double *im, const qarm_op *op, double ang)
{
    long N = 1L << s->n, m0 = 1L << op->q0, m1 = 1L << op->q1, half = N >> 1;
    double c = cos(ang / 2), sn = sin(ang / 2);
    const double h = 0.70710678118654752440;
    switch (op->kind) {
    case QARM_PAULI_ROT:
        qarm_sv_pauli_rot(s, re, im, op->x, op->z, ang);
        return;
    case QARM_X:
        QARM_SV_PAR(half)
        for (long j = 0; j < half; j++) {
            long b = qarm_sv_ins(j, m0), u = b | m0;
            double t = re[b]; re[b] = re[u]; re[u] = t;
            t = im[b]; im[b] = im[u]; im[u] = t;
        }
        return;
    case QARM_H:
        QARM_SV_PAR(half)
        for (long j = 0; j < half; j++) {
            long b = qarm_sv_ins(j, m0), u = b | m0;
            double t = re[b]; re[b] = h * (t + re[u]); re[u] = h * (t - re[u]);
            t = im[b]; im[b] = h * (t + im[u]); im[u] = h * (t - im[u]);
        }
        return;
    case QARM_RY:
        QARM_SV_PAR(half)
        for (long j = 0; j < half; j++) {
            long b = qarm_sv_ins(j, m0), u = b | m0;
            double t = re[b]; re[b] = c * t - sn * re[u]; re[u] = sn * t + c * re[u];
            t = im[b]; im[b] = c * t - sn * im[u]; im[u] = sn * t + c * im[u];
        }
        return;
    case QARM_RZ:
        QARM_SV_PAR(N)
        for (long b = 0; b < N; b++) {
            double s2 = (b & m0) ? sn : -sn, t = re[b];
            re[b] = c * t - s2 * im[b]; im[b] = s2 * t + c * im[b];
        }
        return;
    case QARM_CX:                                   /* control q0, target q1: the pairs with q0 set */
        QARM_SV_PAR(half)
        for (long j = 0; j < half; j++) {
            long b = qarm_sv_ins(j, m1), u = b | m1;
            if (!(b & m0)) continue;
            double t = re[b]; re[b] = re[u]; re[u] = t;
            t = im[b]; im[b] = im[u]; im[u] = t;
        }
        return;
    case QARM_CZ:
        QARM_SV_PAR(N)
        for (long b = 0; b < N; b++)
            if ((b & m0) && (b & m1)) { re[b] = -re[b]; im[b] = -im[b]; }
        return;
    }
}

static double qarm_sv_angle(const qarm_op *op, const double *theta)
{
    return qarm_sv_rot(op->kind) ? op->scale * theta[op->param] : 0;
}

/* A UCC excitation is several Pauli rotations with one parameter and one X mask that commute (a double: 8): on each
 * pair (b, b ^ x) their sum A = sum scale_j P_j is the 2x2 block [[0, f(u)], [f(b), 0]], f(b) = sum scale_j i^k_j
 * s_j(b), and exp(-i t A) is exactly cos(t|f|) - i sin(t|f|) A / |f| there: one pass for the whole excitation.
 * f(b) depends only on the parities of b & z_j: a table of 2^ops entries, made once per pass (cos and sin
 * included), indexed by those parities; u's index is b's xor the parities of x & z_j.
 * qarm_sv_block: the end of the block that starts at op o (o + 1 when o is not such a rotation). */
#define QARM_SV_BLOCK 8
static uint32_t qarm_sv_block(const qarm_ansatz *a, uint32_t o)
{
    const qarm_op *p = &a->op[o];
    uint32_t e = o + 1;
    if (p->kind != QARM_PAULI_ROT || !p->x) return e;
    for (; e < a->ops && e - o < QARM_SV_BLOCK; e++) {
        const qarm_op *q = &a->op[e];
        if (q->kind != QARM_PAULI_ROT || q->param != p->param || q->x != p->x) break;
        int commute = 1;
        for (uint32_t k = o; k < e; k++)              /* X^x Z^z, X^x Z^z': they commute when x . (z ^ z') is even */
            if (qarm_sv_popcount(p->x & (a->op[k].z ^ q->z)) & 1) { commute = 0; break; }
        if (!commute) break;
    }
    return e;
}

typedef struct { uint64_t x, z[QARM_SV_BLOCK]; int n; unsigned flip; double fr[1 << QARM_SV_BLOCK],
                 fi[1 << QARM_SV_BLOCK], c[1 << QARM_SV_BLOCK], sn[1 << QARM_SV_BLOCK]; } qarm_sv_blk;

static void qarm_sv_blk_make(qarm_sv_blk *k, const qarm_ansatz *a, uint32_t o, uint32_t e, double t)
{
    k->x = a->op[o].x;
    k->n = (int)(e - o);
    k->flip = 0;
    for (int j = 0; j < k->n; j++) {
        k->z[j] = a->op[o + j].z;
        if (qarm_sv_odd(k->x & k->z[j])) k->flip |= 1u << j;   /* s_j(b ^ x) = s_j(b) (-1)^parity(x & z_j) */
    }
    for (unsigned idx = 0; idx < (1u << k->n); idx++) {
        double r = 0, i = 0;
        for (int j = 0; j < k->n; j++) {
            const qarm_op *q = &a->op[o + j];
            double c = (idx >> j & 1) ? -q->scale : q->scale;
            switch (qarm_sv_popcount(q->x & q->z) & 3) {
            case 0: r += c; break;
            case 1: i += c; break;
            case 2: r -= c; break;
            default: i -= c;
            }
        }
        double mag = sqrt(r * r + i * i);
        k->fr[idx] = r;
        k->fi[idx] = i;
        k->c[idx] = cos(t * mag);
        k->sn[idx] = mag > 0 ? sin(t * mag) / mag : 0;
    }
}

static unsigned qarm_sv_blk_idx(const qarm_sv_blk *k, long b)
{
    unsigned idx = 0;
    for (int j = 0; j < k->n; j++) idx |= (unsigned)qarm_sv_odd((uint64_t)b & k->z[j]) << j;
    return idx;
}

/* exp(-i t A) of the block on (re, im) */
static void qarm_sv_block_rot(const qarm_sv *s, const qarm_ansatz *a, uint32_t o, uint32_t e, double *re, double *im,
                              double t)
{
    static qarm_sv_blk k;                             /* 8 KB: not on the stack (macOS probes large frames) */
    qarm_sv_blk_make(&k, a, o, e, t);
    uint64_t x = k.x;
    long half = (1L << s->n) >> 1, m = qarm_sv_low(x);
    QARM_SV_PAR(half)
    for (long j = 0; j < half; j++) {
        long b = qarm_sv_ins(j, m), u = b ^ (long)x;
        unsigned ib = qarm_sv_blk_idx(&k, b), iu = ib ^ k.flip;
        double c = k.c[ib], sn = k.sn[ib];           /* |f(b)| = |f(u)| (A is Hermitian) */
        double fr = k.fr[ib], fi = k.fi[ib], gr = k.fr[iu], gi = k.fi[iu];
        double br = re[b], bi = im[b], ur = re[u], ui = im[u];
        /* new[b] = c psi[b] - i sn f(u) psi[u]; new[u] = c psi[u] - i sn f(b) psi[b] */
        double ar = gr * ur - gi * ui, ai = gr * ui + gi * ur;    /* f(u) psi[u] */
        double cr = fr * br - fi * bi, ci = fr * bi + fi * br;    /* f(b) psi[b] */
        re[b] = c * br + sn * ai; im[b] = c * bi - sn * ar;
        re[u] = c * ur + sn * ci; im[u] = c * ui - sn * cr;
    }
}

/* 2 Re <l| -i A |p> of the block */
static double qarm_sv_block_d(const qarm_sv *s, const qarm_ansatz *a, uint32_t o, uint32_t e)
{
    static qarm_sv_blk k;
    qarm_sv_blk_make(&k, a, o, e, 0);
    uint64_t x = k.x;
    long N = 1L << s->n;
    double d = 0;
    QARM_SV_PAR_SUM(N, d)
    for (long b = 0; b < N; b++) {
        unsigned ib = qarm_sv_blk_idx(&k, b);
        double fr = k.fr[ib], fi = k.fi[ib];
        long u = b ^ (long)x;
        double pr = fr * s->re[b] - fi * s->im[b], pi = fr * s->im[b] + fi * s->re[b];   /* (A p)[u] */
        d += s->lr[u] * pi - s->li[u] * pr;                       /* Re conj(l) (-i) (A p) */
    }
    return 2 * d;
}

static void qarm_sv_prepare(qarm_sv *s, const qarm_ansatz *a, const double *theta)
{
    long N = 1L << s->n;
    QARM_SV_PAR(N)
    for (long b = 0; b < N; b++) s->re[b] = s->im[b] = 0;
    s->re[a->init] = 1;
    for (uint32_t o = 0, e; o < a->ops; o = e) {
        e = qarm_sv_block(a, o);
        if (e - o > 1) qarm_sv_block_rot(s, a, o, e, s->re, s->im, theta[a->op[o].param]);
        else qarm_sv_apply(s, s->re, s->im, &a->op[o], qarm_sv_angle(&a->op[o], theta));
    }
}

/* H's terms in groups of equal X mask: an index array sorted by x (malloc'd), and the groups' bounds */
typedef struct { uint32_t *idx, *start, groups; } qarm_sv_groups;

static int qarm_sv_cmp_x(const void *a, const void *b, const qarm_hamiltonian *h)
{
    uint64_t p = h->t[*(const uint32_t *)a].x, q = h->t[*(const uint32_t *)b].x;
    return p < q ? -1 : p > q;
}

static qarm_sv_groups qarm_sv_group(const qarm_hamiltonian *h)
{
    qarm_sv_groups g = {(uint32_t *)malloc(sizeof(uint32_t) * (h->terms + 1)),
                        (uint32_t *)malloc(sizeof(uint32_t) * (h->terms + 2)), 0};
    for (uint32_t j = 0; j < h->terms; j++) g.idx[j] = j;
    for (uint32_t j = 1; j < h->terms; j++) {      /* insertion sort: stable, and tables come mostly in order */
        uint32_t v = g.idx[j], k = j;
        while (k > 0 && qarm_sv_cmp_x(&g.idx[k - 1], &v, h) > 0) { g.idx[k] = g.idx[k - 1]; k--; }
        g.idx[k] = v;
    }
    for (uint32_t j = 0; j < h->terms; j++)
        if (j == 0 || h->t[g.idx[j]].x != h->t[g.idx[j - 1]].x) g.start[g.groups++] = j;
    g.start[g.groups] = h->terms;
    return g;
}

/* f_g(b) = sum over the terms of group g of c i^k s(b), with s(b) = (-1)^popcount(b & z): H = sum_g X^x_g F_g, F_g
 * diagonal. f_g does not depend on the state, so it is computed once per Hamiltonian and kept (the largest groups
 * first, within QARM_SV_CACHE_MB): then a group's pass is one multiply-add per amplitude. A real Hamiltonian
 * (an even number of Y in every term, as molecular ones) needs only the real part. */
#ifndef QARM_SV_CACHE_MB
#define QARM_SV_CACHE_MB 1024
#endif
/* A group whose z masks span r <= QARM_SV_RANK dimensions over GF(2) needs no stored f: with a basis v_1..v_r of
 * that span, s_j(b) depends only on the r parities of b & v_i, so f(b) is a table of 2^r entries indexed by them. */
#ifndef QARM_SV_RANK
#define QARM_SV_RANK 10
#endif
typedef struct { int r; uint64_t v[QARM_SV_RANK]; double *tr, *ti; } qarm_sv_rank_t;
typedef struct { const qarm_hamiltonian *h; const qarm_pauli *t; uint32_t terms; int n; qarm_sv_groups g;
                 double **fr, **fi; qarm_sv_rank_t *rk; } qarm_sv_cache;

/* the basis of the group's z masks, and each term's coordinates in it; 0 when the rank is above QARM_SV_RANK */
static int qarm_sv_rank_make(const qarm_hamiltonian *h, const qarm_sv_groups *g, uint32_t gi, qarm_sv_rank_t *rk)
{
    uint64_t piv[64] = {0}, comb[64] = {0};       /* echelon rows by top bit, and which basis vectors they hold */
    int r = 0;
    rk->r = 0; rk->tr = rk->ti = NULL;
    for (uint32_t j = g->start[gi]; j < g->start[gi + 1]; j++) {
        uint64_t z = h->t[g->idx[j]].z, c = 0;
        while (z) {
            int top = qarm_sv_top(z);
            if (!piv[top]) {
                if (r == QARM_SV_RANK) return 0;
                rk->v[r] = h->t[g->idx[j]].z;     /* a new basis vector: this term's own z */
                piv[top] = z;
                comb[top] = c ^ (1ULL << r);
                r++;
                z = 0;
                break;
            }
            z ^= piv[top];
            c ^= comb[top];
        }
    }
    rk->r = r;
    unsigned T = 1u << r;
    rk->tr = (double *)calloc(T, sizeof(double));
    rk->ti = (double *)calloc(T, sizeof(double));
    uint64_t x = h->t[g->idx[g->start[gi]]].x;
    for (uint32_t j = g->start[gi]; j < g->start[gi + 1]; j++) {
        uint64_t z = h->t[g->idx[j]].z, c = 0;     /* the coordinates of z in the basis v */
        while (z) {
            int top = qarm_sv_top(z);
            z ^= piv[top];
            c ^= comb[top];
        }
        double cf = h->t[g->idx[j]].c;
        int k = qarm_sv_popcount(x & h->t[g->idx[j]].z) & 3;
        for (unsigned pat = 0; pat < T; pat++) {   /* s_j = (-1)^(the parities of the basis vectors in c) */
            double v = qarm_sv_popcount(pat & c) & 1 ? -cf : cf;
            if (k == 0) rk->tr[pat] += v; else if (k == 1) rk->ti[pat] += v;
            else if (k == 2) rk->tr[pat] -= v; else rk->ti[pat] -= v;
        }
    }
    return 1;
}
static qarm_sv_cache qarm_sv_hc;                      /* one Hamiltonian at a time (libqarm: under its lock) */

static void qarm_sv_factor(const qarm_hamiltonian *h, const qarm_sv_groups *g, uint32_t gi, long b, double *fr,
                           double *fi)
{
    uint64_t x = h->t[g->idx[g->start[gi]]].x;
    double r = 0, i = 0;
    for (uint32_t j = g->start[gi]; j < g->start[gi + 1]; j++) {
        const qarm_pauli *t = &h->t[g->idx[j]];
        double c = qarm_sv_odd((uint64_t)b & t->z) ? -t->c : t->c;
        switch (qarm_sv_popcount(x & t->z) & 3) {
        case 0: r += c; break;
        case 1: i += c; break;
        case 2: r -= c; break;
        default: i -= c;
        }
    }
    *fr = r;
    *fi = i;
}

static qarm_sv_cache *qarm_sv_cached(const qarm_sv *s, const qarm_hamiltonian *h)
{
    qarm_sv_cache *c = &qarm_sv_hc;
    if (c->h == h && c->t == h->t && c->terms == h->terms && c->n == s->n) return c;
    if (c->fr) {
        for (uint32_t gi = 0; gi < c->g.groups; gi++) {
            free(c->fr[gi]); free(c->fi[gi]); free(c->rk[gi].tr); free(c->rk[gi].ti);
        }
        free(c->fr); free(c->fi); free(c->rk); free(c->g.idx); free(c->g.start);
    }
    long N = 1L << s->n;
    c->h = h; c->t = h->t; c->terms = h->terms; c->n = s->n;
    c->g = qarm_sv_group(h);
    c->fr = (double **)calloc(c->g.groups, sizeof(double *));
    c->fi = (double **)calloc(c->g.groups, sizeof(double *));
    c->rk = (qarm_sv_rank_t *)calloc(c->g.groups, sizeof(qarm_sv_rank_t));
    for (uint32_t gi = 0; gi < c->g.groups; gi++) qarm_sv_rank_make(h, &c->g, gi, &c->rk[gi]);
    uint32_t *order = (uint32_t *)malloc(sizeof(uint32_t) * (c->g.groups + 1));
    for (uint32_t gi = 0; gi < c->g.groups; gi++) order[gi] = gi;
    for (uint32_t a = 1; a < c->g.groups; a++) {      /* the largest groups first: they save the most */
        uint32_t v = order[a], k = a, sz = c->g.start[v + 1] - c->g.start[v];
        while (k > 0 && c->g.start[order[k - 1] + 1] - c->g.start[order[k - 1]] < sz) { order[k] = order[k - 1]; k--; }
        order[k] = v;
    }
    double budget = (double)QARM_SV_CACHE_MB * 1048576, used = 0;
    const char *env = getenv("QARM_SV_CACHE_MB");
    if (env) budget = atof(env) * 1048576;
    for (uint32_t a = 0; a < c->g.groups; a++) {
        uint32_t gi = order[a];
        int imag = 0, real = 0;
        uint64_t x = h->t[c->g.idx[c->g.start[gi]]].x;
        for (uint32_t j = c->g.start[gi]; j < c->g.start[gi + 1]; j++) {
            if (qarm_sv_popcount(x & h->t[c->g.idx[j]].z) & 1) imag = 1; else real = 1;
        }
        double bytes = (double)N * sizeof(double) * (real + imag);
        if (c->rk[gi].tr || c->g.start[gi + 1] - c->g.start[gi] < 2 || used + bytes > budget) continue;
        used += bytes;
        double *fr = real ? (double *)malloc(sizeof(double) * N) : NULL;
        double *fi = imag ? (double *)malloc(sizeof(double) * N) : NULL;
        QARM_SV_PAR(N)
        for (long b = 0; b < N; b++) {
            double r, i;
            qarm_sv_factor(h, &c->g, gi, b, &r, &i);
            if (fr) fr[b] = r;
            if (fi) fi[b] = i;
        }
        c->fr[gi] = fr ? fr : NULL;
        c->fi[gi] = fi;
        if (!fr && !fi) continue;
        if (!fr) c->fr[gi] = NULL;
    }
    free(order);
    return c;
}

/* <psi|H|psi>; with hr, hi also (hr, hi) = (H - constant) psi. One pass per X group: for each b the group's
 * factor f(b) multiplies psi[b] into (H psi)[b ^ x]. */
static double qarm_sv_hamiltonian(const qarm_sv *s, const qarm_hamiltonian *h, double *hr, double *hi)
{
    long N = 1L << s->n;
    const double *re = s->re, *im = s->im;
    double e = h->constant;
    if (hr) {
        QARM_SV_PAR(N)
        for (long b = 0; b < N; b++) hr[b] = hi[b] = 0;
    }
    qarm_sv_cache *c = qarm_sv_cached(s, h);
    const qarm_sv_groups *g = &c->g;
    for (uint32_t gi = 0; gi < g->groups; gi++) {
        uint64_t x = h->t[g->idx[g->start[gi]]].x;
        const double *Fr = c->fr[gi], *Fi = c->fi[gi];
        const qarm_sv_rank_t *rk = &c->rk[gi];
        int cached = Fr || Fi;
        double v = 0;
        QARM_SV_PAR_SUM(N, v)
        for (long b = 0; b < N; b++) {
            double fr, fi;
            if (rk->tr) {
                unsigned pat = 0;
                for (int i = 0; i < rk->r; i++) pat |= (unsigned)qarm_sv_odd((uint64_t)b & rk->v[i]) << i;
                fr = rk->tr[pat]; fi = rk->ti[pat];
            } else if (cached) { fr = Fr ? Fr[b] : 0; fi = Fi ? Fi[b] : 0; }
            else qarm_sv_factor(h, g, gi, b, &fr, &fi);
            long u = b ^ (long)x;
            double pr = fr * re[b] - fi * im[b], pi = fr * im[b] + fi * re[b];
            v += re[u] * pr + im[u] * pi;           /* Re <psi[u] | (P psi)[u]> */
            if (hr) { hr[u] += pr; hi[u] += pi; }   /* b -> u is a bijection: no two b write one u */
        }
        e += v;
    }
    return e;
}

static double qarm_sv_energy(qarm_sv *s, const qarm_hamiltonian *h) { return qarm_sv_hamiltonian(s, h, NULL, NULL); }

/* (lr, li) = H (re, im) - constant (re, im); returns <psi|H|psi> */
static double qarm_sv_hpsi(qarm_sv *s, const qarm_hamiltonian *h) { return qarm_sv_hamiltonian(s, h, s->lr, s->li); }

/* 2 Re <l| -i P |p>, P = i^k X^x Z^z: the derivative of a Pauli rotation's energy, in one pass */
static double qarm_sv_dpauli(const qarm_sv *s, uint64_t x, uint64_t z)
{
    long N = 1L << s->n;
    int k = qarm_sv_popcount(x & z) & 3;
    double fr = (k == 1) ? 1 : (k == 3) ? -1 : 0, fi = (k == 0) ? -1 : (k == 2) ? 1 : 0;   /* -i * i^k */
    double d = 0;
    QARM_SV_PAR_SUM(N, d)
    for (long b = 0; b < N; b++) {
        double sg = qarm_sv_odd((uint64_t)b & z) ? -1 : 1, pr = s->re[b], pi = s->im[b];
        long u = b ^ (long)x;                       /* (-iP p)[u] = sg (fr + i fi) p[b] */
        d += sg * (s->lr[u] * (fr * pr - fi * pi) + s->li[u] * (fr * pi + fi * pr));
    }
    return 2 * d;
}

/* E(theta) and dE/dtheta: with |phi_k> the state after operation k and <lambda_k| = <psi| H U_n .. U_k+1,
 * dE/d(angle_k) = 2 Re <lambda_k| dU_k/d(angle) U_k^-1 |phi_k>; both are stepped back by U_k^-1 */
static double qarm_sv_grad(qarm_sv *s, const qarm_hamiltonian *h, const qarm_ansatz *a, const double *theta,
                           double *grad)
{
    long N = 1L << s->n;
    for (uint32_t p = 0; p < a->params; p++) grad[p] = 0;
    qarm_sv_prepare(s, a, theta);
    double e = qarm_sv_hpsi(s, h);
    uint32_t *ends = (uint32_t *)malloc(sizeof(uint32_t) * (a->ops + 1)), nb = 0;   /* the blocks, walked back */
    for (uint32_t o = 0; o < a->ops; o = ends[nb++]) ends[nb] = qarm_sv_block(a, o);
    for (long bi = (long)nb - 1; bi >= 0; bi--) {
        uint32_t o0 = bi ? ends[bi - 1] : 0, o1 = ends[bi];
        if (o1 - o0 > 1) {
            double t = theta[a->op[o0].param];
            grad[a->op[o0].param] += qarm_sv_block_d(s, a, o0, o1);
            qarm_sv_block_rot(s, a, o0, o1, s->re, s->im, -t);
            qarm_sv_block_rot(s, a, o0, o1, s->lr, s->li, -t);
            continue;
        }
        long o = o0;
        const qarm_op *op = &a->op[o];
        double ang = qarm_sv_angle(op, theta);
        if (qarm_sv_rot(op->kind)) {
            double d = 0;                                 /* 2 Re <lambda| -i G |phi>, G = Y/2, Z/2 or P */
            if (op->kind == QARM_PAULI_ROT) {
                d = qarm_sv_dpauli(s, op->x, op->z);
            } else if (op->kind == QARM_RY) {            /* -iY: |0> -> |1>, |1> -> -|0> */
                long m0 = 1L << op->q0, half = N >> 1;
                QARM_SV_PAR_SUM(half, d)
                for (long j = 0; j < half; j++) {
                    long b = qarm_sv_ins(j, m0), u = b | m0;
                    d += s->lr[b] * -s->re[u] + s->li[b] * -s->im[u] + s->lr[u] * s->re[b] + s->li[u] * s->im[b];
                }
            } else {                                      /* -iZ: (r + i m) -> (m - i r) on |0>, (-m + i r) on |1> */
                long m0 = 1L << op->q0;
                QARM_SV_PAR_SUM(N, d)
                for (long b = 0; b < N; b++) {
                    double sg = (b & m0) ? -1 : 1;
                    d += sg * (s->lr[b] * s->im[b] - s->li[b] * s->re[b]);
                }
            }
            grad[op->param] += op->scale * d;
        }
        qarm_sv_apply(s, s->re, s->im, op, -ang);       /* the inverse: X, H, CX, CZ are their own */
        qarm_sv_apply(s, s->lr, s->li, op, -ang);
    }
    free(ends);
    return e;
}

/* QCIRC: a qarm_gate as up to 15 operations of the VQE engine (X, H, CX, CZ, RY, RZ; angles in scale, param 0), equal
 * up to a global phase: Y = RY(pi), Z = RZ(pi), S = RZ(pi/2), T = RZ(pi/4), SX = H RZ(pi/2) H, RX = H RZ H, SWAP =
 * three CX, CCX the standard Clifford+T circuit. Returns the number of operations, 0 for an unknown kind. */
static int qarm_sv_lower(const qarm_gate *g, qarm_op *out)
{
    const double pi = 3.14159265358979323846;
    int n = 0;
#define OP(k, a, b, ang) (out[n++] = (qarm_op){(uint8_t)(k), (uint8_t)(a), (uint8_t)(b), 0, 0, (ang), 0, 0})
    switch (g->kind) {
    case QARM_G_X: OP(QARM_X, g->q0, 0, 0); break;
    case QARM_G_Y: OP(QARM_RY, g->q0, 0, pi); break;
    case QARM_G_Z: OP(QARM_RZ, g->q0, 0, pi); break;
    case QARM_G_H: OP(QARM_H, g->q0, 0, 0); break;
    case QARM_G_S: OP(QARM_RZ, g->q0, 0, pi / 2); break;
    case QARM_G_SDG: OP(QARM_RZ, g->q0, 0, -pi / 2); break;
    case QARM_G_T: OP(QARM_RZ, g->q0, 0, pi / 4); break;
    case QARM_G_TDG: OP(QARM_RZ, g->q0, 0, -pi / 4); break;
    case QARM_G_SX: OP(QARM_H, g->q0, 0, 0); OP(QARM_RZ, g->q0, 0, pi / 2); OP(QARM_H, g->q0, 0, 0); break;
    case QARM_G_RX: OP(QARM_H, g->q0, 0, 0); OP(QARM_RZ, g->q0, 0, g->angle); OP(QARM_H, g->q0, 0, 0); break;
    case QARM_G_RY: OP(QARM_RY, g->q0, 0, g->angle); break;
    case QARM_G_RZ: OP(QARM_RZ, g->q0, 0, g->angle); break;
    case QARM_G_CX: OP(QARM_CX, g->q0, g->q1, 0); break;
    case QARM_G_CZ: OP(QARM_CZ, g->q0, g->q1, 0); break;
    case QARM_G_SWAP: OP(QARM_CX, g->q0, g->q1, 0); OP(QARM_CX, g->q1, g->q0, 0); OP(QARM_CX, g->q0, g->q1, 0); break;
    case QARM_G_CCX: {
        int a = g->q0, b = g->q1, c = g->q2;
        OP(QARM_H, c, 0, 0); OP(QARM_CX, b, c, 0); OP(QARM_RZ, c, 0, -pi / 4); OP(QARM_CX, a, c, 0);
        OP(QARM_RZ, c, 0, pi / 4); OP(QARM_CX, b, c, 0); OP(QARM_RZ, c, 0, -pi / 4); OP(QARM_CX, a, c, 0);
        OP(QARM_RZ, b, 0, pi / 4); OP(QARM_RZ, c, 0, pi / 4); OP(QARM_H, c, 0, 0); OP(QARM_CX, a, b, 0);
        OP(QARM_RZ, a, 0, pi / 4); OP(QARM_RZ, b, 0, -pi / 4); OP(QARM_CX, a, b, 0);
        break;
    }
    }
#undef OP
    return n;
}

/* a gate's qubits inside 0 .. n - 1 and different from each other */
static int qarm_sv_gate_ok(const qarm_gate *g, uint32_t n)
{
    int k = g->kind, two = k == QARM_G_CX || k == QARM_G_CZ || k == QARM_G_SWAP, three = k == QARM_G_CCX;
    if (k < 1 || k > QARM_G_LAST || g->q0 >= n) return 0;
    if ((two || three) && (g->q1 >= n || g->q1 == g->q0)) return 0;
    if (three && (g->q2 >= n || g->q2 == g->q0 || g->q2 == g->q1)) return 0;
    return 1;
}

/* the circuit on |0..0>: (re, im) */
static void qarm_sv_circuit(qarm_sv *s, const qarm_circuit *c)
{
    static const double one = 1;
    qarm_op ops[16];
    for (long b = 0; b < (1L << s->n); b++) s->re[b] = s->im[b] = 0;
    s->re[0] = 1;
    for (uint32_t i = 0; i < c->gates; i++) {
        int k = qarm_sv_lower(&c->g[i], ops);
        for (int j = 0; j < k; j++) qarm_sv_apply(s, s->re, s->im, &ops[j], qarm_sv_angle(&ops[j], &one));
    }
}
#endif
