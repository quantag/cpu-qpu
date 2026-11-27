/* SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 Quantag IT Solutions GmbH
 *
 * VQE optimizers for qarm.h. They are ordinary C on the CPU; the coprocessor gives the energies and gradients.
 *
 *   double e = qarm_minimize(&v, theta, max_iterations, gradient_tolerance, report, user_data);
 *       L-BFGS on the energy (qarm_expect, QEXPECT) and its gradient (qarm_grad, QGRAD), with an Armijo line
 *       search. For exact energies: shot noise breaks its line search.
 *   qarm_spsa_options o = QARM_SPSA_DEFAULTS;
 *   double e = qarm_spsa(&v, theta, &o, report, user_data);
 *       SPSA (Spall): each iteration estimates the gradient from E(theta + c d) - E(theta - c d) along random
 *       directions d of +-1, all in ONE qarm_expect_n batch (one QPU job per iteration). For measured energies.
 *
 * theta: the start (zeros: the Hartree-Fock state of a UCCSD ansatz) and the result. report, if not NULL, is called
 * once per iteration. Static arrays, no malloc: they also run under qarm_exec.py. Up to QARM_OPT_MAX_PARAMS
 * parameters. */
#ifndef QARM_OPT_H
#define QARM_OPT_H
#include "qarm.h"

#ifndef QARM_OPT_MAX_PARAMS
#define QARM_OPT_MAX_PARAMS 256
#endif
#define QARM_OPT_HISTORY 8

typedef void (*qarm_report_fn)(int iteration, double energy, double gradient_norm, void *user);

static double qarm__dot(const double *a, const double *b, int n)
{
    double s = 0;
    for (int i = 0; i < n; i++) s += a[i] * b[i];
    return s;
}

static double qarm_minimize(qarm_vqe *v, double *theta, int max_iter, double gtol, qarm_report_fn report, void *user)
{
    static double g[QARM_OPT_MAX_PARAMS], gn[QARM_OPT_MAX_PARAMS], d[QARM_OPT_MAX_PARAMS], xn[QARM_OPT_MAX_PARAMS];
    static double S[QARM_OPT_HISTORY][QARM_OPT_MAX_PARAMS], Y[QARM_OPT_HISTORY][QARM_OPT_MAX_PARAMS];
    static double rho[QARM_OPT_HISTORY], alpha[QARM_OPT_HISTORY];
    int n = (int)v->a->params, stored = 0, head = 0;
    if (n > QARM_OPT_MAX_PARAMS) return NAN;
    double e = qarm_grad(v, theta, g);
    for (int it = 0; it < max_iter; it++) {
        double gnorm = sqrt(qarm__dot(g, g, n));
        if (report) report(it, e, gnorm, user);
        if (!(gnorm > gtol)) break;                          /* also stops on NaN */
        for (int i = 0; i < n; i++) d[i] = -g[i];             /* the two-loop recursion: d = -H g */
        for (int k = 0; k < stored; k++) {
            int j = (head - 1 - k + QARM_OPT_HISTORY) % QARM_OPT_HISTORY;
            alpha[j] = rho[j] * qarm__dot(S[j], d, n);
            for (int i = 0; i < n; i++) d[i] -= alpha[j] * Y[j][i];
        }
        if (stored) {
            int j = (head - 1 + QARM_OPT_HISTORY) % QARM_OPT_HISTORY;
            double gamma = qarm__dot(S[j], Y[j], n) / qarm__dot(Y[j], Y[j], n);
            for (int i = 0; i < n; i++) d[i] *= gamma;
        }
        for (int k = stored - 1; k >= 0; k--) {
            int j = (head - 1 - k + QARM_OPT_HISTORY) % QARM_OPT_HISTORY;
            double beta = rho[j] * qarm__dot(Y[j], d, n);
            for (int i = 0; i < n; i++) d[i] += S[j][i] * (alpha[j] - beta);
        }
        double slope = qarm__dot(d, g, n);
        if (slope >= 0) {                                      /* not a descent direction: steepest descent */
            for (int i = 0; i < n; i++) d[i] = -g[i];
            slope = -gnorm * gnorm;
            stored = 0;
        }
        double step = 1, en = e;
        int ok = 0;
        for (int tries = 0; tries < 30; tries++, step *= 0.5) {
            for (int i = 0; i < n; i++) xn[i] = theta[i] + step * d[i];
            en = qarm_expect(v, xn, 0);
            if (en <= e + 1e-4 * step * slope) { ok = 1; break; }
        }
        if (!ok) break;                                        /* no decrease: converged as far as it can */
        double e_new = qarm_grad(v, xn, gn);
        int j = head;
        for (int i = 0; i < n; i++) { S[j][i] = xn[i] - theta[i]; Y[j][i] = gn[i] - g[i]; }
        double sy = qarm__dot(S[j], Y[j], n);
        if (sy > 1e-12) {
            rho[j] = 1 / sy;
            head = (head + 1) % QARM_OPT_HISTORY;
            if (stored < QARM_OPT_HISTORY) stored++;
        }
        for (int i = 0; i < n; i++) { theta[i] = xn[i]; g[i] = gn[i]; }
        e = e_new;
    }
    return e;
}
/* ---------------------------------------------------------------- SPSA */
typedef struct {
    int iterations;          /* gradient steps */
    int resamplings;         /* random directions per step, averaged: 2 * resamplings energies per job */
    double a, c;             /* step sizes a_k = a / (k + 1 + A)^alpha, c_k = c / (k + 1)^gamma; a = 0: calibrated */
    double A, alpha, gamma;  /* A < 0: 10% of the iterations */
    double target_step;      /* the calibration: a such that the first step moves each parameter about this far */
    int calibration;         /* random directions for the calibration (one batch) */
    int average_last;        /* the result: the mean of the last iterates (iterate averaging), then one QEXPECT */
    unsigned long long seed; /* the random directions: the same run on every runtime */
} qarm_spsa_options;

#define QARM_SPSA_DEFAULTS {150, 2, 0, 0.05, -1, 0.602, 0.101, 0.02, 8, 25, 1}
#define QARM_SPSA_MAX_BATCH 64

static unsigned long long qarm__rng;
static int qarm__sign(void)
{
    unsigned long long z = (qarm__rng += 0x9E3779B97F4A7C15ULL);
    z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ULL;
    z = (z ^ (z >> 27)) * 0x94D049BB133111EBULL;
    return (int)((z ^ (z >> 31)) >> 63) ? 1 : -1;
}

static double qarm__powd(double x, double y) { return exp(y * log(x)); }

/* energies at theta +- c * d_r for r < n (directions in dirs), one batch: e[2r] = E(+), e[2r + 1] = E(-) */
static int qarm__spsa_batch(qarm_vqe *v, const double *theta, const signed char (*dirs)[QARM_OPT_MAX_PARAMS], int nd,
                            double c, double *e)
{
    static double pts[2 * QARM_SPSA_MAX_BATCH * QARM_OPT_MAX_PARAMS];
    int n = (int)v->a->params;
    for (int r = 0; r < nd; r++)
        for (int i = 0; i < n; i++) {
            pts[(2 * r) * n + i] = theta[i] + c * dirs[r][i];
            pts[(2 * r + 1) * n + i] = theta[i] - c * dirs[r][i];
        }
    return qarm_expect_n(v, pts, 2 * nd, e);
}

static double qarm_spsa(qarm_vqe *v, double *theta, const qarm_spsa_options *o, qarm_report_fn report, void *user)
{
    static signed char dirs[QARM_SPSA_MAX_BATCH][QARM_OPT_MAX_PARAMS];
    static double e[2 * QARM_SPSA_MAX_BATCH], g[QARM_OPT_MAX_PARAMS], avg[QARM_OPT_MAX_PARAMS];
    int n = (int)v->a->params, R = o->resamplings < 1 ? 1 : o->resamplings;
    if (n > QARM_OPT_MAX_PARAMS || R > QARM_SPSA_MAX_BATCH || o->calibration > QARM_SPSA_MAX_BATCH) return NAN;
    qarm__rng = o->seed;
    double A = o->A < 0 ? 0.1 * o->iterations : o->A, a = o->a;
    if (a <= 0) {                                      /* calibration: the mean gradient size along random d */
        int K = o->calibration < 1 ? 1 : o->calibration;
        for (int r = 0; r < K; r++)
            for (int i = 0; i < n; i++) dirs[r][i] = (signed char)qarm__sign();
        if (qarm__spsa_batch(v, theta, (const signed char (*)[QARM_OPT_MAX_PARAMS])dirs, K, o->c, e) < 0) return NAN;
        double mag = 0;
        for (int r = 0; r < K; r++) mag += fabs(e[2 * r] - e[2 * r + 1]) / (2 * o->c);
        mag /= K;
        a = o->target_step * qarm__powd(A + 1, o->alpha) / (mag > 1e-12 ? mag : 1e-12);
    }
    int navg = 0;
    for (int i = 0; i < n; i++) avg[i] = 0;
    for (int k = 0; k < o->iterations; k++) {
        double ak = a / qarm__powd(k + 1 + A, o->alpha), ck = o->c / qarm__powd(k + 1, o->gamma), est = 0, gn = 0;
        for (int r = 0; r < R; r++)
            for (int i = 0; i < n; i++) dirs[r][i] = (signed char)qarm__sign();
        if (qarm__spsa_batch(v, theta, (const signed char (*)[QARM_OPT_MAX_PARAMS])dirs, R, ck, e) < 0) return NAN;
        for (int i = 0; i < n; i++) g[i] = 0;
        for (int r = 0; r < R; r++) {
            double diff = (e[2 * r] - e[2 * r + 1]) / (2 * ck);
            for (int i = 0; i < n; i++) g[i] += diff * dirs[r][i] / R;   /* 1/d_i = d_i for d_i = +-1 */
            est += (e[2 * r] + e[2 * r + 1]) / (2 * R);
        }
        for (int i = 0; i < n; i++) { theta[i] -= ak * g[i]; gn += g[i] * g[i]; }
        if (report) report(k, est, sqrt(gn), user);
        if (k >= o->iterations - o->average_last) {
            for (int i = 0; i < n; i++) avg[i] += theta[i];
            navg++;
        }
    }
    if (navg)
        for (int i = 0; i < n; i++) theta[i] = avg[i] / navg;
    double err;
    return qarm_expect(v, theta, &err);
}
#endif
