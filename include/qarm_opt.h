/* SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 Quantag IT Solutions GmbH
 *
 * A VQE optimizer for qarm.h: L-BFGS on the energy (qarm_expect, QEXPECT) and its gradient (qarm_grad, QGRAD).
 * The optimizer is ordinary C on the CPU; the coprocessor gives the energies and gradients.
 *
 *   double e = qarm_minimize(&v, theta, max_iterations, gradient_tolerance, report, user_data);
 *
 * theta: the start (zeros: the Hartree-Fock state of a UCCSD ansatz) and the result. report, if not NULL, is called
 * once per iteration. L-BFGS with an Armijo backtracking line search; for exact energies. With measured energies
 * (shot noise) its line search can stall: a noise-tolerant optimizer such as SPSA fits those better.
 * Static arrays, no malloc: it also runs under qarm_exec.py. Up to QARM_OPT_MAX_PARAMS parameters. */
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
#endif
