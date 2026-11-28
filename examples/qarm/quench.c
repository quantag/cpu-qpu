/* SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 Quantag IT Solutions GmbH
 *
 * QEVOLVE (ISA 1.1): the quench of examples/qarm/spin_chain.c, the transverse-field Ising chain of 8 spins at its
 * critical point, H = -sum Z_i Z_i+1 - sum X_i, from all spins up. The program states the accuracy it needs; the
 * runtime chooses how to meet it and returns the error bound it guarantees for every t:
 *   1. the runtime's choice: exact on a simulator, the cheapest product formula on a QPU;
 *   2. a product formula, eps = 1e-2: the runtime picks the order and the fewest steps whose bound meets it;
 *   3. Strang (order 2) with 4 steps, as spin_chain.c's coarse column, and the bound that comes with it.
 * The same binary runs on any CPU (the same plans, computed here) and under a Q-ARM runtime. */
#include <math.h>
#include <stdio.h>
#include "qarm.h"

#define N 8
#define K 13
static qarm_pauli hterms[2 * N - 1], mterms[N];

static void show(const char *what, const qarm_evolve_info *i)
{
    if (i->method == QARM_EV_PRODUCT)
        printf("  %s: order %u, %llu steps, bound %.2g\n", what, i->order, (unsigned long long)i->steps, i->bound);
    else printf("  %s: %s, bound %.2g\n", what, i->method == QARM_EV_EXACT ? "exact" : "qDRIFT", i->bound);
}

int main(void)
{
    int n = 0;
    for (int i = 0; i + 1 < N; i++) hterms[n++] = (qarm_pauli){0, 3ULL << i, -1.0};
    for (int i = 0; i < N; i++) hterms[n++] = (qarm_pauli){1ULL << i, 0, -1.0};
    for (int i = 0; i < N; i++) mterms[i] = (qarm_pauli){0, 1ULL << i, 1.0 / N};
    const qarm_hamiltonian h = {N, (uint32_t)n, 0, hterms}, m = {N, N, 0, mterms};

    double t[K], a[K], b[K], c[K];
    for (int i = 0; i < K; i++) t[i] = 0.25 * i;
    qarm_evolution e = {&h, &m, t, K, QARM_EV_AUTO, 0, 0, 0, 1e-6, 0, 0};
    qarm_evolve_info ia, ib, ic;
    int w = qarm_evolve(&e, a, &ia);
    e.method = QARM_EV_PRODUCT; e.eps = 1e-2;
    int w2 = qarm_evolve(&e, b, &ib);
    e.order = 2; e.steps = 4;
    int w3 = qarm_evolve(&e, c, &ic);
    if (w < 0 || w2 < 0 || w3 < 0) return 2;
    printf("Ising chain, %d spins, quench from all up: M(t) by QEVOLVE on %s\n", N,
           w ? "the quantum coprocessor" : "this CPU");
    show("1. the runtime's choice", &ia);
    show("2. eps 1e-2", &ib);
    show("3. Strang, 4 steps", &ic);
    printf("   t         1.         2.         3.\n");
    double worst2 = 0, worst3 = 0;
    for (int i = 0; i < K; i++) {
        printf("%5.2f  %9.6f  %9.6f  %9.6f\n", t[i], a[i], b[i], c[i]);
        if (fabs(b[i] - a[i]) > worst2) worst2 = fabs(b[i] - a[i]);
        if (fabs(c[i] - a[i]) > worst3) worst3 = fabs(c[i] - a[i]);
    }
    printf("largest differences from 1.: %.2g (bound %.2g), %.2g (bound %.2g)\n", worst2, ib.bound, worst3, ic.bound);
    double noise = 4 * (ia.err + ib.err + ic.err);    /* a measured backend's standard errors */
    return worst2 <= ib.bound + ia.bound + noise + 1e-9 && worst3 <= ic.bound + ia.bound + noise + 1e-9 ? 0 : 1;
}
