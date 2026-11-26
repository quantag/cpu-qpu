// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 Quantag IT Solutions GmbH
//
// VQE for the hydrogen molecule, H2 at 0.735 Angstrom (STO-3G), in one ordinary arm64 binary (docs/qarm_v2.md,
// section 6). The optimizer is this C code, on the CPU; the energies come from the quantum coprocessor.
//
//   clang -O1 -iquote include examples/qarm/h2_vqe.c -o h2_vqe -lm
//   ./h2_vqe                                     a plain CPU: the exact energy from the state vector
//   python3 qarm_exec.py h2_vqe                  Q-ARM, the exact simulator
//   python3 qarm_exec.py --backend preview h2_vqe    Q-ARM, IBM's noise model of a real device
//   python3 qarm_exec.py --qpu h2_vqe            Q-ARM, config.json's backend (a real IBM device: 5 jobs)
#include <stdio.h>
#include "qarm.h"

// The electronic Hamiltonian in the parity mapping with two qubits removed by symmetry (the coefficients of
// Qiskit Nature for this geometry), plus the nuclear repulsion as the constant. Ground state: -1.137306 Ha.
static const qarm_pauli h2_terms[] = {
    {0, 0, -1.052373245772859},                   // I I
    {0, 1, 0.39793742484318045},                  // Z on qubit 0
    {0, 2, -0.39793742484318045},                 // Z on qubit 1
    {0, 3, -0.01128010425623538},                 // Z Z
    {3, 0, 0.18093119978423156},                  // X X
};
static const qarm_hamiltonian h2 = {2, 5, 0.7199689944489797, h2_terms};

// one parameter, one CNOT: cos(theta/2) |10> + sin(theta/2) |01>
static const qarm_op ops[] = {
    {QARM_RY, 0, 0, 0, 0, 1.0, 0, 0},
    {QARM_X, 1, 0, 0, 0, 0, 0, 0},
    {QARM_CX, 0, 1, 0, 0, 0, 0, 0},
};
static const qarm_ansatz ansatz = {2, 1, 3, 0, 0, ops};

#define K 17                                      // energies per batch: one job on a QPU
#define ROUNDS 4

int main(void)
{
    qarm_vqe v;
    int where = qarm_vqe_load(&h2, &ansatz, &v);
    if (where < 0)
        return 2;
    printf("H2 VQE, %s\n", where ? "energies from the quantum coprocessor (Q-ARM)" : "exact energies on this CPU");
    qarm_session(1);
    double lo = -3.141592653589793, hi = 3.141592653589793, best = 0, theta[K], e[K];
    for (int r = 0; r < ROUNDS; r++) {             // a grid, then a finer grid around the lowest point
        for (int i = 0; i < K; i++)
            theta[i] = lo + (hi - lo) * i / (K - 1);
        qarm_expect_n(&v, theta, K, e);
        int m = 0;
        for (int i = 1; i < K; i++)
            if (e[i] < e[m])
                m = i;
        best = theta[m];
        double step = (hi - lo) / (K - 1);
        lo = best - step;
        hi = best + step;
        printf("round %d: theta = %+.6f, E = %.6f Ha\n", r + 1, best, e[m]);
    }
    double err, energy = qarm_expect(&v, &best, &err);   // measured again: the lowest of K noisy values is biased
    qarm_session(0);
    printf("E(%.6f) = %.6f Ha", best, energy);
    if (err > 0)
        printf(" +- %.6f", err);
    printf("; exact ground state -1.137306 Ha, difference %.1f mHa\n", (energy + 1.1373060357534) * 1000);
    qarm_vqe_free(&v);
    return 0;
}
