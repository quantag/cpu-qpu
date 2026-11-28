/* SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 Quantag IT Solutions GmbH
 *
 * tests/test_qarm_evolve.py's driver: a qarm_evolution from stdin, planned and run by include/qarm_sv.h on this
 * CPU. Input: n, H's terms (count, then x z c each, then the constant), O's the same, init, k, the k times, method,
 * order, steps, eps, shots, exact_ok, and optionally plan_only (1: the plan, no run). Output: "plan method order
 * steps bound" or "refused <reason>", then
 * "se <value>" and one value per time. */
#include <stdio.h>
#include <stdlib.h>
#include "qarm_sv.h"

static int read_h(qarm_hamiltonian *h, unsigned n)
{
    unsigned m;
    if (scanf("%u", &m) != 1) return 0;
    qarm_pauli *t = calloc(m + 1, sizeof *t);
    for (unsigned i = 0; i < m; i++) {
        unsigned long long x, z;
        if (scanf("%llu %llu %lf", &x, &z, &t[i].c) != 3) return 0;
        t[i].x = x; t[i].z = z;
    }
    h->qubits = n; h->terms = m; h->t = t;
    return scanf("%lf", &h->constant) == 1;
}

int main(void)
{
    unsigned n, k, method, order, steps, shots;
    unsigned long long init;
    int exact_ok;
    qarm_hamiltonian h, o;
    double eps;
    if (scanf("%u", &n) != 1 || !read_h(&h, n) || !read_h(&o, n) || scanf("%llu %u", &init, &k) != 2) return 2;
    double *t = calloc(k + 1, sizeof *t);
    for (unsigned i = 0; i < k; i++) if (scanf("%lf", &t[i]) != 1) return 2;
    int plan_only = 0;
    if (scanf("%u %u %u %lf %u %d", &method, &order, &steps, &eps, &shots, &exact_ok) != 6) return 2;
    if (scanf("%d", &plan_only) != 1) plan_only = 0;
    qarm_evolution e = {&h, &o, t, k, method, order, shots, init, eps, steps, 0};
    const char *why = qarm_ev_check(&e);
    if (why) { printf("refused %s\n", why); return 0; }
    qarm_ev_plan_t p;
    if (qarm_ev_plan(&e, exact_ok, 0, &p) < 0) { printf("refused %s\n", p.why); return 0; }
    p.seed = 12345;
    printf("plan %u %u %llu %.17g\n", p.method, p.order, (unsigned long long)p.steps, p.bound);
    if (plan_only) return 0;
    long N = 1L << n;
    double *buf = malloc(sizeof *buf * 6 * (size_t)N), se;
    qarm_sv s = {(int)n, buf, buf + N, buf + 2 * N, buf + 3 * N, buf + 4 * N, buf + 5 * N};
    if (qarm_sv_evolve(&s, &e, &p, t, &se) < 0) return 3;    /* out = t: the overlap is allowed */
    printf("se %.17g\n", se);
    for (unsigned i = 0; i < k; i++) printf("%.17g\n", t[i]);
    return 0;
}
