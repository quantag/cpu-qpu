// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 Quantag IT Solutions GmbH
//
// QLINDBLAD (ISA 1.2): the quench of examples/qarm/quench.c, a transverse-field Ising chain from all spins up, on
// 6 spins, now with each spin dephasing at rate gamma: d rho/dt = -i[H, rho] + gamma sum_i (Z_i rho Z_i - rho).
// The magnetization by QEVOLVE (closed, gamma = 0) and by QLINDBLAD for two rates, each with the error bound the
// runtime proves; the last column by Strang steps chosen to meet eps = 5e-2. The same binary runs on any CPU (the
// same plans, computed here) and under a Q-ARM runtime.
#include <math.h>
#include <stdio.h>
#include "qarm.h"

#define N 6
#define K 9
static qarm_pauli hterms[2 * N - 1], mterms[N], zjumps[N];

int main(void)
{
    int n = 0;
    for (int i = 0; i + 1 < N; i++) hterms[n++] = (qarm_pauli){0, 3ULL << i, -1.0};
    for (int i = 0; i < N; i++) hterms[n++] = (qarm_pauli){1ULL << i, 0, -1.0};
    for (int i = 0; i < N; i++) mterms[i] = (qarm_pauli){0, 1ULL << i, 1.0 / N};
    const qarm_hamiltonian h = {N, (uint32_t)n, 0, hterms}, m = {N, N, 0, mterms};
    double t[K], closed[K], weak[K], strong[K], strang[K];
    for (int i = 0; i < K; i++) t[i] = 0.25 * i;

    qarm_evolution e = {&h, &m, t, K, QARM_EV_AUTO, 0, 0, 0, 1e-6, 0, 0};
    qarm_evolve_info ic, iw, is, ip;
    int w = qarm_evolve(&e, closed, &ic);
    for (int i = 0; i < N; i++) zjumps[i] = (qarm_pauli){0, 1ULL << i, 0.05};
    qarm_hamiltonian zj = {N, N, 0, zjumps};
    qarm_lindblad l = {e, &zj};
    int w1 = qarm_evolve_open(&l, weak, &iw);
    for (int i = 0; i < N; i++) zjumps[i].c = 0.5;
    int w2 = qarm_evolve_open(&l, strong, &is);
    l.e.method = QARM_EV_PRODUCT; l.e.eps = 5e-2;
    int w3 = qarm_evolve_open(&l, strang, &ip);
    if (w < 0 || w1 < 0 || w2 < 0 || w3 < 0) return 2;
    printf("Ising chain, %d spins, dephasing at rate gamma: M(t) on %s\n", N, w1 ? "the quantum coprocessor" : "this CPU");
    printf("  closed (QEVOLVE): bound %.2g; gamma 0.05: bound %.2g; gamma 0.5: bound %.2g (all exact)\n", ic.bound,
           iw.bound, is.bound);
    printf("  gamma 0.5 for eps 5e-2: %s, %llu steps, bound %.2g\n", ip.order == 1 ? "Lie-Trotter" : "Strang",
           (unsigned long long)ip.steps, ip.bound);
    printf("   t    closed   gamma 0.05  gamma 0.5   product\n");
    double worst = 0;
    for (int i = 0; i < K; i++) {
        printf("%5.2f  %8.5f  %9.5f  %9.5f  %9.5f\n", t[i], closed[i], weak[i], strong[i], strang[i]);
        if (fabs(strang[i] - strong[i]) > worst) worst = fabs(strang[i] - strong[i]);
    }
    printf("the product formula's largest difference from the exact: %.2g (bound %.2g)\n", worst, ip.bound);
    return worst <= ip.bound + is.bound + 4 * (ip.err + is.err) + 1e-9 ? 0 : 1;
}
