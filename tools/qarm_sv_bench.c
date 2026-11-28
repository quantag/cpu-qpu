/* SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 Quantag IT Solutions GmbH
 *
 * The speed of include/qarm_sv.h on a molecule header from tools/qarm_molecule.py: the ansatz, the energy, and the
 * energy with its gradient (adjoint), each averaged over REPS runs after one warm-up.
 *   cc -O2 -fopenmp -DQARM_VQE_MAX_QUBITS=20 -DMOLECULE='"lih18.h"' -iquote include -iquote . \
 *      tools/qarm_sv_bench.c -o qarm_sv_bench -lm && OMP_NUM_THREADS=8 ./qarm_sv_bench 5
 * (without -fopenmp: one core) */
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#include "qarm.h"
#ifndef MOLECULE
#define MOLECULE "lih.h"
#endif
#include MOLECULE
#ifdef _OPENMP
#include <omp.h>
#endif

static double now(void)
{
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec + t.tv_nsec * 1e-9;
}

int main(int argc, char **argv)
{
    int reps = argc > 1 ? atoi(argv[1]) : 3, n = (int)molecule_h.qubits, threads = 1;
    long N = 1L << n;
    qarm_sv s = {n, 0, 0, 0, 0, 0, 0};
    double **b[6] = {&s.re, &s.im, &s.tr, &s.ti, &s.lr, &s.li};
    for (int i = 0; i < 6; i++)
        if (!(*b[i] = malloc(sizeof(double) * N))) { fprintf(stderr, "out of memory\n"); return 1; }
    static double th[256], g[256];
    for (uint32_t p = 0; p < molecule_ansatz.params; p++) th[p] = 0.01 * (p % 7);
#ifdef _OPENMP
    threads = omp_get_max_threads();
#endif
    qarm_sv_prepare(&s, &molecule_ansatz, th);
    double t0 = now();
    qarm_sv_energy(&s, &molecule_h);                     /* the first call keeps the Hamiltonian's factors */
    double tc = now() - t0;
    t0 = now();
    for (int r = 0; r < reps; r++) qarm_sv_prepare(&s, &molecule_ansatz, th);
    double tp = (now() - t0) / reps;
    t0 = now();
    double e = 0;
    for (int r = 0; r < reps; r++) e = qarm_sv_energy(&s, &molecule_h);
    double te = (now() - t0) / reps;
    t0 = now();
    for (int r = 0; r < reps; r++) qarm_sv_grad(&s, &molecule_h, &molecule_ansatz, th, g);
    double tg = (now() - t0) / reps;
    printf("%d qubits, %u terms, %u operations, %u parameters, %d thread%s: ansatz %.1f ms, energy %.1f ms, "
           "energy and gradient %.1f ms (first energy %.1f ms); E = %.10f\n", n, molecule_h.terms, molecule_ansatz.ops,
           molecule_ansatz.params, threads, threads > 1 ? "s" : "", tp * 1e3, te * 1e3, tg * 1e3, tc * 1e3, e);
    return 0;
}
