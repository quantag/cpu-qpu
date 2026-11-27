/* SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 Quantag IT Solutions GmbH
 *
 * QAOA for MaxCut with the VQE instructions and QVSAMPLE. The graph: the Wagner graph, 8 vertices (a ring and its
 * four long diagonals), 12 edges, the largest cut 10.
 *
 *   cost  C = sum over edges (i, j) of (Z_i Z_j - 1) / 2 = -(the number of cut edges): a Hamiltonian table
 *   state |gamma, beta> = prod over layers of exp(-i beta_l sum X) exp(-i gamma_l C) H^n |0>: an ansatz table
 *
 * The angles are optimized on <C> (QEXPECT, QGRAD; L-BFGS in qarm_opt.h). Then QVSAMPLE measures the state: each
 * bitstring is a cut, and the program keeps the best one it sees, checked by counting its edges in C. */
#include <stdio.h>
#include "qarm.h"
#include "qarm_opt.h"

#define N 8
#define LAYERS 3
static const int edges[][2] = {{0, 1}, {1, 2}, {2, 3}, {3, 4}, {4, 5}, {5, 6}, {6, 7}, {7, 0},
                               {0, 4}, {1, 5}, {2, 6}, {3, 7}};
#define E (int)(sizeof edges / sizeof edges[0])

static qarm_pauli terms[E];
static qarm_op ops[N + LAYERS * (E + N)];

static int cut(uint64_t b)
{
    int c = 0;
    for (int e = 0; e < E; e++) c += (int)((b >> edges[e][0] ^ b >> edges[e][1]) & 1);
    return c;
}

int main(void)
{
    int n = 0;
    for (int e = 0; e < E; e++) terms[e] = (qarm_pauli){0, 1ULL << edges[e][0] | 1ULL << edges[e][1], 0.5};
    for (int q = 0; q < N; q++) ops[n++] = (qarm_op){QARM_H, (uint8_t)q, 0, 0, 0, 0, 0, 0};
    for (int l = 0; l < LAYERS; l++) {
        for (int e = 0; e < E; e++)              /* exp(-i gamma (Z_i Z_j) / 2) */
            ops[n++] = (qarm_op){QARM_PAULI_ROT, 0, 0, (uint8_t)(2 * l), 0, 0.5, 0, terms[e].z};
        for (int q = 0; q < N; q++)              /* exp(-i beta X_q) */
            ops[n++] = (qarm_op){QARM_PAULI_ROT, 0, 0, (uint8_t)(2 * l + 1), 0, 1.0, 1ULL << q, 0};
    }
    const qarm_hamiltonian h = {N, E, -0.5 * E, terms};
    const qarm_ansatz a = {N, 2 * LAYERS, (uint32_t)n, 0, 0, ops};

    qarm_vqe v;
    int where = qarm_vqe_load(&h, &a, &v);
    if (where < 0) return 2;
    double theta[2 * LAYERS];
    for (int l = 0; l < LAYERS; l++) {           /* a linear ramp: gamma grows, beta shrinks */
        theta[2 * l] = 0.4 * (l + 1) / LAYERS;
        theta[2 * l + 1] = 0.4 * (LAYERS - l) / LAYERS;
    }
    double e = qarm_minimize(&v, theta, 200, 1e-6, 0, 0);
    printf("QAOA, %d layers, on %s: <C> = %.4f, the expected cut %.3f\n", LAYERS,
           where ? "the quantum coprocessor" : "this CPU", e, -e);

    static uint64_t s[1000];
    int got = qarm_vsample(&v, theta, 1000, s);
    if (got < 0) return 2;
    int best = 0, at_best = 0, best_known = 0;
    uint64_t best_b = 0;
    for (int i = 0; i < 1000; i++) {
        int c = cut(s[i]);
        if (c > best) { best = c; best_b = s[i]; at_best = 0; }
        at_best += c == best;
    }
    for (uint64_t b = 0; b < (1u << N); b++) best_known = cut(b) > best_known ? cut(b) : best_known;
    printf("QVSAMPLE (%s): best cut %d (", got == 1 ? "the coprocessor's measurements" : "sampled here", best);
    for (int q = 0; q < N; q++) putchar('0' + (int)(best_b >> q & 1));
    printf("), in %.1f%% of 1000 shots; the largest cut is %d\n", at_best / 10.0, best_known);
    qarm_vqe_free(&v);
    return best == best_known ? 0 : 1;
}
