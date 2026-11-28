/* SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 Quantag IT Solutions GmbH
 *
 * tests/test_qarm_lindblad.py: QEVOLVE's qDRIFT on a measured backend. H = X + Z on one qubit, O = Z, from |0>:
 * <Z(t)> = (1 + cos(2 sqrt(2) t)) / 2. 24 random sequences, eps 0.05; prints "within the bound" when every mean is
 * within the bound plus 5 standard errors. */
#include <math.h>
#include <stdio.h>
#include "qarm.h"

static const qarm_pauli ht[2] = {{1, 0, 1.0}, {0, 1, 1.0}}, zt[1] = {{0, 1, 1.0}};
static const qarm_hamiltonian h = {1, 2, 0, ht}, z = {1, 1, 0, zt};

int main(void)
{
    double t[3] = {0.3, 0.8, 1.5}, out[3], ok = 1;
    qarm_evolution e = {&h, &z, t, 3, QARM_EV_QDRIFT, 0, 24, 0, 0.05, 0, 0};
    qarm_evolve_info i;
    int w = qarm_evolve(&e, out, &i);
    for (int j = 0; j < 3; j++) {
        double ex = 0.5 + 0.5 * cos(2 * sqrt(2.0) * t[j]);
        printf("t %.1f: %.4f, exactly %.4f\n", t[j], out[j], ex);
        ok = ok && fabs(out[j] - ex) <= i.bound + 5 * i.err;
    }
    printf("%s (bound %.3g, standard error %.3g, %s)\n", ok ? "within the bound" : "OUTSIDE the bound", i.bound, i.err,
           w == 1 ? "the coprocessor" : "this CPU");
    return ok ? 0 : 1;
}
