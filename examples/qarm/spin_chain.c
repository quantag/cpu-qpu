/* SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 Quantag IT Solutions GmbH
 *
 * Time evolution (qarm_evolve_n, Trotter steps on the VQE instructions): a quench of the transverse-field Ising
 * chain, 8 spins, H = -J sum Z_i Z_i+1 - g sum X_i at the critical point J = g = 1. All spins start up (|00000000>);
 * the magnetization M(t) = <sum Z_i> / 8 falls and oscillates. Each column is one table and one QEXPECTN batch for
 * all 13 times (one job on a QPU): 4 Trotter steps, then 32, which agree with the exact curve to about 1e-3. */
#include <stdio.h>
#include "qarm.h"

#define N 8
#define K 13
static qarm_pauli hterms[2 * N - 1], mterms[N];
static qarm_op ops[2 * 32 * (2 * N - 1) + 1];

int main(void)
{
    int n = 0;
    for (int i = 0; i + 1 < N; i++) hterms[n++] = (qarm_pauli){0, 3ULL << i, -1.0};       /* -J Z_i Z_i+1 */
    for (int i = 0; i < N; i++) hterms[n++] = (qarm_pauli){1ULL << i, 0, -1.0};           /* -g X_i */
    for (int i = 0; i < N; i++) mterms[i] = (qarm_pauli){0, 1ULL << i, 1.0 / N};
    const qarm_hamiltonian h = {N, (uint32_t)n, 0, hterms}, m = {N, N, 0, mterms};

    double t[K], coarse[K], fine[K];
    for (int i = 0; i < K; i++) t[i] = 0.25 * i;
    int w4 = qarm_evolve_n(&h, &m, 0, t, K, 4, ops, sizeof ops / sizeof ops[0], coarse);
    int w32 = qarm_evolve_n(&h, &m, 0, t, K, 32, ops, sizeof ops / sizeof ops[0], fine);
    if (w4 < 0 || w32 < 0) return 2;
    printf("Ising chain, %d spins, quench from all up: M(t) on %s\n", N, w32 ? "the quantum coprocessor" : "this CPU");
    printf("   t    4 steps   32 steps\n");
    for (int i = 0; i < K; i++) printf("%5.2f  %8.4f  %8.4f\n", t[i], coarse[i], fine[i]);
    return 0;
}
