/* SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 Quantag IT Solutions GmbH
 *
 * The exact state vector of Q-ARM's VQE tables (include/qarm.h): the classical path of qarm.h and libqarm's exact
 * backend. Six arrays of 2^n doubles, given by the caller:
 *   qarm_sv_prepare(&s, a, theta)            |psi(theta)> from the ansatz
 *   qarm_sv_energy(&s, h)                    <psi|H|psi>
 *   qarm_sv_grad(&s, h, a, theta, grad)      the energy and its gradient, by adjoint differentiation: one forward and
 *                                            one backward pass, whatever the number of parameters
 * Included by qarm.h; needs qarm_hamiltonian, qarm_ansatz and qarm_op. Link with -lm. */
#ifndef QARM_SV_H
#define QARM_SV_H
#include <math.h>
#include <stdint.h>

typedef struct { int n; double *re, *im, *tr, *ti, *lr, *li; } qarm_sv;

static int qarm_sv_odd(uint64_t v) { int n = 0; while (v) { v &= v - 1; n ^= 1; } return n; }

/* (tr, ti) = P (sr, si), P = i^popcount(x & z) X^x Z^z */
static void qarm_sv_pauli(const qarm_sv *s, const double *sr, const double *si, uint64_t x, uint64_t z)
{
    uint64_t y = x & z;
    int k = 0;
    while (y) { y &= y - 1; k++; }
    for (long b = 0; b < (1L << s->n); b++) {
        double r = sr[b], i = si[b], t;
        if (qarm_sv_odd((uint64_t)b & z)) { r = -r; i = -i; }
        for (int j = 0; j < (k & 3); j++) { t = r; r = -i; i = t; }
        s->tr[b ^ (long)x] = r;
        s->ti[b ^ (long)x] = i;
    }
}

static int qarm_sv_rot(int kind) { return kind == QARM_RY || kind == QARM_RZ || kind == QARM_PAULI_ROT; }

/* one operation on (re, im), at angle ang (the angle of RY, RZ: exp(-i ang G/2); of a Pauli rotation: exp(-i ang P)) */
static void qarm_sv_apply(qarm_sv *s, double *re, double *im, const qarm_op *op, double ang)
{
    long N = 1L << s->n, m0 = 1L << op->q0, m1 = 1L << op->q1;
    double c, sn, t;
    if (op->kind == QARM_PAULI_ROT) {                    /* cos(ang) psi - i sin(ang) P psi */
        qarm_sv_pauli(s, re, im, op->x, op->z);
        c = cos(ang); sn = sin(ang);
        for (long b = 0; b < N; b++) {
            t = re[b];
            re[b] = c * t + sn * s->ti[b];
            im[b] = c * im[b] - sn * s->tr[b];
        }
        return;
    }
    c = cos(ang / 2); sn = sin(ang / 2);
    for (long b = 0; b < N; b++) {
        long u = b | m0;
        switch (op->kind) {
        case QARM_X:
            if (b & m0) break;
            t = re[b]; re[b] = re[u]; re[u] = t;
            t = im[b]; im[b] = im[u]; im[u] = t;
            break;
        case QARM_H:
            if (b & m0) break;
            t = re[b]; re[b] = 0.70710678118654752440 * (t + re[u]); re[u] = 0.70710678118654752440 * (t - re[u]);
            t = im[b]; im[b] = 0.70710678118654752440 * (t + im[u]); im[u] = 0.70710678118654752440 * (t - im[u]);
            break;
        case QARM_RY:
            if (b & m0) break;
            t = re[b]; re[b] = c * t - sn * re[u]; re[u] = sn * t + c * re[u];
            t = im[b]; im[b] = c * t - sn * im[u]; im[u] = sn * t + c * im[u];
            break;
        case QARM_RZ: {
            double s2 = (b & m0) ? sn : -sn;
            t = re[b]; re[b] = c * t - s2 * im[b]; im[b] = s2 * t + c * im[b];
            break;
        }
        case QARM_CX:
            if (!(b & m0) || (b & m1)) break;
            u = b | m1;
            t = re[b]; re[b] = re[u]; re[u] = t;
            t = im[b]; im[b] = im[u]; im[u] = t;
            break;
        case QARM_CZ:
            if ((b & m0) && (b & m1)) { re[b] = -re[b]; im[b] = -im[b]; }
            break;
        }
    }
}

static double qarm_sv_angle(const qarm_op *op, const double *theta)
{
    return qarm_sv_rot(op->kind) ? op->scale * theta[op->param] : 0;
}

static void qarm_sv_prepare(qarm_sv *s, const qarm_ansatz *a, const double *theta)
{
    for (long b = 0; b < (1L << s->n); b++) s->re[b] = s->im[b] = 0;
    s->re[a->init] = 1;
    for (uint32_t o = 0; o < a->ops; o++)
        qarm_sv_apply(s, s->re, s->im, &a->op[o], qarm_sv_angle(&a->op[o], theta));
}

/* (lr, li) = H (re, im) - constant (re, im); returns <psi|H|psi> */
static double qarm_sv_hpsi(qarm_sv *s, const qarm_hamiltonian *h)
{
    long N = 1L << s->n;
    double e = h->constant;
    for (long b = 0; b < N; b++) s->lr[b] = s->li[b] = 0;
    for (uint32_t j = 0; j < h->terms; j++) {
        qarm_sv_pauli(s, s->re, s->im, h->t[j].x, h->t[j].z);
        double c = h->t[j].c, v = 0;
        for (long b = 0; b < N; b++) {
            v += s->re[b] * s->tr[b] + s->im[b] * s->ti[b];
            s->lr[b] += c * s->tr[b];
            s->li[b] += c * s->ti[b];
        }
        e += c * v;
    }
    return e;
}

static double qarm_sv_energy(qarm_sv *s, const qarm_hamiltonian *h)
{
    long N = 1L << s->n;
    double e = h->constant;
    for (uint32_t j = 0; j < h->terms; j++) {
        double v = 0;
        qarm_sv_pauli(s, s->re, s->im, h->t[j].x, h->t[j].z);
        for (long b = 0; b < N; b++) v += s->re[b] * s->tr[b] + s->im[b] * s->ti[b];
        e += h->t[j].c * v;
    }
    return e;
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
    for (long o = (long)a->ops - 1; o >= 0; o--) {
        const qarm_op *op = &a->op[o];
        double ang = qarm_sv_angle(op, theta);
        if (qarm_sv_rot(op->kind)) {
            double d = 0;                                 /* 2 Re <lambda| -i G |phi>, G = Y/2, Z/2 or P */
            if (op->kind == QARM_PAULI_ROT) {
                qarm_sv_pauli(s, s->re, s->im, op->x, op->z);
                for (long b = 0; b < N; b++) d += s->lr[b] * s->ti[b] - s->li[b] * s->tr[b];
                d *= 2;
            } else {
                long m0 = 1L << op->q0;
                for (long b = 0; b < N; b++) {
                    if (op->kind == QARM_RY) {            /* -iY: |0> -> |1>, |1> -> -|0> */
                        if (b & m0) continue;
                        long u = b | m0;
                        d += s->lr[b] * -s->re[u] + s->li[b] * -s->im[u] + s->lr[u] * s->re[b] + s->li[u] * s->im[b];
                    } else {                              /* -iZ: (r + i m) -> (m - i r) on |0>, (-m + i r) on |1> */
                        double sg = (b & m0) ? -1 : 1;
                        d += sg * (s->lr[b] * s->im[b] - s->li[b] * s->re[b]);
                    }
                }
            }
            grad[op->param] += op->scale * d;
        }
        qarm_sv_apply(s, s->re, s->im, op, -ang);       /* the inverse: X, H, CX, CZ are their own */
        qarm_sv_apply(s, s->lr, s->li, op, -ang);
    }
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
