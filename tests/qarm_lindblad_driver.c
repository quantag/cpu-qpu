/* SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 Quantag IT Solutions GmbH
 *
 * tests/test_qarm_lindblad.py's driver: a qarm_lindblad from stdin, planned and run by include/qarm_sv.h. Input as
 * tests/qarm_evolve_driver.c's, with the jump operators (count, x z gamma each) after O, and a last flag:
 * trajectories (1: plan as if the density matrix were too large). Output: "plan method order steps bound dm R"
 * or "refused <reason>", then "se <value>" and one value per time. */
#include <stdio.h>
#include <stdlib.h>
#include "qarm_sv.h"

static int read_terms(qarm_hamiltonian *h, unsigned n, int constant)
{
    unsigned m;
    if (scanf("%u", &m) != 1) return 0;
    qarm_pauli *t = calloc(m + 1, sizeof *t);
    for (unsigned i = 0; i < m; i++) {
        unsigned long long x, z;
        if (scanf("%llu %llu %lf", &x, &z, &t[i].c) != 3) return 0;
        t[i].x = x; t[i].z = z;
    }
    h->qubits = n; h->terms = m; h->t = t; h->constant = 0;
    return !constant || scanf("%lf", &h->constant) == 1;
}

int main(void)
{
    unsigned n, k, method, order, steps, shots;
    unsigned long long init;
    int traj;
    qarm_hamiltonian h, o, j;
    double eps;
    if (scanf("%u", &n) != 1 || !read_terms(&h, n, 1) || !read_terms(&o, n, 1) || !read_terms(&j, n, 0)
        || scanf("%llu %u", &init, &k) != 2) return 2;
    double *t = calloc(k + 1, sizeof *t);
    for (unsigned i = 0; i < k; i++) if (scanf("%lf", &t[i]) != 1) return 2;
    if (scanf("%u %u %u %lf %u %d", &method, &order, &steps, &eps, &shots, &traj) != 6) return 2;
    qarm_lindblad l = {{&h, &o, t, k, method, order, shots, init, eps, steps, 0}, &j};
    const char *why = qarm_lb_check(&l);
    if (why) { printf("refused %s\n", why); return 0; }
    qarm_lb_plan_t p;
    if (qarm_lb_plan(&l, 1, 0, &p) < 0) { printf("refused %s\n", p.why); return 0; }
    if (traj && p.method == QARM_EV_PRODUCT) { p.dm = 0; p.trajectories = shots ? shots : QARM_LB_TRAJECTORIES; }
    p.seed = 99;
    printf("plan %u %u %llu %.17g %u %u\n", p.method, p.order, (unsigned long long)p.steps, p.bound, p.dm, p.trajectories);
    qarm_dm dm;
    qarm_sv s;
    double *mem, se;
    if (qarm_lb_alloc(&l, &p, &dm, &s, &mem) < 0 || qarm_lb_run(&dm, &s, &l, &p, t, &se) < 0) return 3;
    printf("se %.17g\n", se);
    for (unsigned i = 0; i < k; i++) printf("%.17g\n", t[i]);
    free(mem);
    return 0;
}
