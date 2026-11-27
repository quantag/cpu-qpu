/* SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 Quantag IT Solutions GmbH
 *
 * libqarm: QSEARCH, QMIN, QCOUNT. Grover's search with a phase oracle given as a reversible circuit (x, cx, ccx, z on qubits that
 * start at 0, the input on FIRST .. FIRST+BITS-1), as tools/qarm_oracle.py writes it from a predicate's machine code.
 *
 * The oracle is a classical reversible circuit, so its sign (-1)^f(x) is found by running its gates on each basis
 * input; that also checks that it returns every work qubit to 0. Grover's search is then simulated on the 2^BITS
 * amplitudes of the search register alone, whatever the oracle's width: BBHT (Boyer, Brassard, Hoyer, Tapp) for an
 * unknown number of solutions, each round's iteration count drawn at random, each measured answer checked. */
#include <stdint.h>
#ifndef QARM_RUNTIME
#define QARM_RUNTIME
#endif
#include "qarm.h"
#include "qarm_internal.h"
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

typedef struct { int op, a, b, c; } ogate;          /* op: 0 x, 1 cx, 2 ccx, 3 z */
struct qarm_oracle { char name[128]; int n, first, bits, ngates, out_first, out_bits; ogate *g; };

qarm_oracle *qarm_rt_oracle_parse(const char *text, char *err, size_t errlen)
{
    qarm_oracle *o = calloc(1, sizeof *o);
    int consumed = 0, version = 0;
    if (sscanf(text, "qarm-oracle %d", &version) == 1 && version == 2) {     /* a compute oracle */
        if (sscanf(text, "qarm-oracle 2 %127s %d %d %d %d %d %d%n", o->name, &o->n, &o->first, &o->bits,
                   &o->out_first, &o->out_bits, &o->ngates, &consumed) != 7 || o->out_bits < 1 || o->out_bits > 32
            || o->out_first < 0 || o->out_first + o->out_bits > o->n) {
            snprintf(err, errlen, "not a valid qarm-oracle 2 text");
            free(o);
            return NULL;
        }
    } else if (sscanf(text, "qarm-oracle 1 %127s %d %d %d %d%n", o->name, &o->n, &o->first, &o->bits, &o->ngates,
                      &consumed) != 5) {
        snprintf(err, errlen, "not a qarm-oracle 1 or 2 text");
        free(o);
        return NULL;
    }
    if (o->n < 1 || o->bits < 1 || o->bits > 24 || o->first < 0 || o->first + o->bits > o->n || o->ngates < 0) {
        snprintf(err, errlen, "an oracle of %d qubits, input %d..%d: inconsistent", o->n, o->first, o->first + o->bits);
        free(o);
        return NULL;
    }
    o->g = calloc((size_t)(o->ngates ? o->ngates : 1), sizeof *o->g);
    const char *p = text + consumed;
    for (int i = 0; i < o->ngates; i++) {
        char op[8];
        int k = 0, args[3] = {-1, -1, -1};
        while (*p == '\n' || *p == ' ' || *p == '\r') p++;
        if (sscanf(p, "%7s%n", op, &k) != 1) { snprintf(err, errlen, "gate %d: missing", i); goto bad; }
        p += k;
        int want = !strcmp(op, "x") || !strcmp(op, "z") ? 1 : !strcmp(op, "cx") ? 2 : !strcmp(op, "ccx") ? 3 : 0;
        if (!want) { snprintf(err, errlen, "gate %d: unknown %s", i, op); goto bad; }
        for (int j = 0; j < want; j++) {
            if (sscanf(p, "%d%n", &args[j], &k) != 1 || args[j] < 0 || args[j] >= o->n) {
                snprintf(err, errlen, "gate %d (%s): a qubit outside 0..%d", i, op, o->n - 1);
                goto bad;
            }
            p += k;
        }
        o->g[i] = (ogate){op[0] == 'x' ? 0 : op[0] == 'z' ? 3 : op[1] == 'x' ? 1 : 2, args[0], args[1], args[2]};
    }
    return o;
bad:
    qarm_rt_oracle_free(o);
    return NULL;
}

void qarm_rt_oracle_free(qarm_oracle *o)
{
    if (!o) return;
    free(o->g);
    free(o);
}

const char *qarm_rt_oracle_name(const qarm_oracle *o) { return o->name; }
int qarm_rt_oracle_qubits(const qarm_oracle *o) { return o->n; }
int qarm_rt_oracle_gates(const qarm_oracle *o) { return o->ngates; }

/* by running the reversible gates on each basis input x: mark[x] = f(x) for a phase oracle, value[x] = f(x) for a
 * compute oracle (signed when 32 bits); -1 if a work qubit stays set */
static int evaluate(const qarm_oracle *o, unsigned char *mark, int64_t *value, char *err, size_t errlen)
{
    int words = (o->n + 63) / 64;
    uint64_t *q = malloc(sizeof *q * (size_t)words);
    long N = 1L << o->bits;
    for (long x = 0; x < N; x++) {
        memset(q, 0, sizeof *q * (size_t)words);
        for (int b = 0; b < o->bits; b++)
            if (x >> b & 1) q[(o->first + b) / 64] |= 1ULL << ((o->first + b) % 64);
        int odd = 0;
#define BIT(i) ((q[(i) / 64] >> ((i) % 64)) & 1)
#define FLIP(i) (q[(i) / 64] ^= 1ULL << ((i) % 64))
        for (int i = 0; i < o->ngates; i++) {
            const ogate *g = &o->g[i];
            switch (g->op) {
            case 0: FLIP(g->a); break;
            case 1: if (BIT(g->a)) FLIP(g->b); break;
            case 2: if (BIT(g->a) && BIT(g->b)) FLIP(g->c); break;
            case 3: odd ^= (int)BIT(g->a); break;
            }
        }
        if (value) {
            uint64_t v = 0;
            for (int b = 0; b < o->out_bits; b++) v |= (uint64_t)BIT(o->out_first + b) << b;
            value[x] = o->out_bits == 32 ? (int64_t)(int32_t)(uint32_t)v : (int64_t)v;
        }
        for (int i = 0; i < o->n; i++) {
            if (o->out_bits && i >= o->out_first && i < o->out_first + o->out_bits) continue;
            int want = i >= o->first && i < o->first + o->bits ? (int)(x >> (i - o->first) & 1) : 0;
            if ((int)BIT(i) != want) {
                snprintf(err, errlen, "%s: the oracle leaves qubit %d at %d for x = %ld", o->name, i, !want, x);
                free(q);
                return -1;
            }
        }
#undef BIT
#undef FLIP
        if (mark) mark[x] = (unsigned char)odd;
    }
    free(q);
    return 0;
}

static uint64_t s_rng;
static double s_uniform(void)
{
    if (!s_rng) {
        const char *s = getenv("QARM_SEED");
        s_rng = s && *s ? strtoull(s, NULL, 10) * 0x9E3779B97F4A7C15ULL + 7 : (uint64_t)(qarm_now() * 1e6) | 1;
    }
    uint64_t z = (s_rng += 0x9E3779B97F4A7C15ULL);
    z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ULL;
    z = (z ^ (z >> 27)) * 0x94D049BB133111EBULL;
    return (double)((z ^ (z >> 31)) >> 11) / 9007199254740992.0;
}

/* BBHT on the marked set: an x with mark[x], or -1 within the budget; calls and rounds added up */
static long bbht(const unsigned char *mark, int bits, long *calls, int *rounds)
{
    long N = 1L << bits;
    double *amp = malloc(sizeof *amp * (size_t)N);   /* real amplitudes: Grover's operators keep them real */
    double m = 1, lambda = 6.0 / 5, root = sqrt((double)N);
    long budget = *calls + (long)(9 * root) + 8, found = -1;
    while (*calls <= budget) {
        int j = (int)(s_uniform() * m);
        ++*rounds;
        for (long b = 0; b < N; b++) amp[b] = 1 / root;
        for (int it = 0; it < j; it++) {             /* the oracle, then the inversion about the mean */
            double mean = 0;
            for (long b = 0; b < N; b++) { if (mark[b]) amp[b] = -amp[b]; mean += amp[b]; }
            mean /= (double)N;
            for (long b = 0; b < N; b++) amp[b] = 2 * mean - amp[b];
        }
        *calls += j + 1;                              /* j iterations, then checking the answer */
        double u = s_uniform(), acc = 0;
        long b = 0;
        for (; b < N - 1; b++) { acc += amp[b] * amp[b]; if (u < acc) break; }
        if (mark[b]) { found = b; break; }
        m = fmin(lambda * m, root);
    }
    free(amp);
    return found;
}

int qarm_rt_search(const qarm_oracle *o, int *x, char *report, size_t len)
{
    char err[300];
    long N = 1L << o->bits, calls = 0;
    int rounds = 0;
    if (o->out_bits) { snprintf(report, len, "QSEARCH %s: a compute oracle, not a phase oracle", o->name); return -1; }
    unsigned char *mark = malloc((size_t)N);
    if (evaluate(o, mark, NULL, err, sizeof err) < 0) { snprintf(report, len, "%s", err); free(mark); return -1; }
    long found = bbht(mark, o->bits, &calls, &rounds);
    long solutions = 0;
    for (long b = 0; b < N; b++) solutions += mark[b];
    free(mark);
    if (found >= 0) {
        *x = (int)found;
        snprintf(report, len, "QSEARCH %s, %d bits: oracle of %d qubits and %d gates from the machine code; Grover "
                 "(BBHT) found x = %ld with %ld oracle calls in %d rounds (simulated: the search register's %ld "
                 "amplitudes)", o->name, o->bits, o->n, o->ngates, found, calls, rounds, N);
        return 1;
    }
    snprintf(report, len, "QSEARCH %s, %d bits: %s after %ld oracle calls in %d rounds", o->name, o->bits,
             solutions ? "no answer measured" : "no x with f(x) = 1", calls, rounds);
    return 2;
}

/* QMIN: Durr-Hoyer. From a random x, Grover searches for an x with a smaller value until none is found within
 * 22.5 sqrt(N) + 1.4 log2(N)^2 oracle calls. */
int qarm_rt_min(const qarm_oracle *o, int *x, long *value, char *report, size_t len)
{
    char err[300];
    if (!o->out_bits) { snprintf(report, len, "QMIN %s: a phase oracle, not a compute oracle", o->name); return -1; }
    long N = 1L << o->bits, calls = 0;
    int rounds = 0, searches = 0;
    int64_t *v = malloc(sizeof *v * (size_t)N);
    unsigned char *mark = malloc((size_t)N);
    if (evaluate(o, NULL, v, err, sizeof err) < 0) { snprintf(report, len, "%s", err); free(v); free(mark); return -1; }
    long best = (long)(s_uniform() * N);
    double budget = 22.5 * sqrt((double)N) + 1.4 * o->bits * o->bits;
    while (calls < budget) {
        for (long b = 0; b < N; b++) mark[b] = v[b] < v[best];
        searches++;
        long y = bbht(mark, o->bits, &calls, &rounds);
        if (y < 0) break;
        best = y;
    }
    *x = (int)best;
    *value = (long)v[best];
    snprintf(report, len, "QMIN %s, %d bits: compute oracle of %d qubits and %d gates (%d result bits) from the machine "
             "code; Durr-Hoyer found x = %ld, value %ld, with %ld oracle calls in %d Grover searches (simulated)",
             o->name, o->bits, o->n, o->ngates, o->out_bits, best, (long)v[best], calls, searches);
    free(v);
    free(mark);
    return 1;
}

/* QCOUNT: quantum counting (Brassard, Hoyer, Tapp), simulated: phase estimation of the Grover operator with t
 * precision qubits; the outcome drawn from its exact distribution (Fejer kernels around +-theta/pi) */
int qarm_rt_count(const qarm_oracle *o, long *count, double *bound, char *report, size_t len)
{
    char err[300];
    if (o->out_bits) { snprintf(report, len, "QCOUNT %s: a compute oracle, not a phase oracle", o->name); return -1; }
    long N = 1L << o->bits, M = 0;
    unsigned char *mark = malloc((size_t)N);
    if (evaluate(o, mark, NULL, err, sizeof err) < 0) { snprintf(report, len, "%s", err); free(mark); return -1; }
    for (long b = 0; b < N; b++) M += mark[b];
    free(mark);
    int t = (o->bits + 1) / 2 + 3;
    long T = 1L << t;
    double phi = asin(sqrt((double)M / (double)N)) / M_PI, total = 0, *p = malloc(sizeof *p * (size_t)T);
    for (long y = 0; y < T; y++) {
        double f[2];
        for (int s = 0; s < 2; s++) {
            double d = y + (s ? 1 : -1) * T * phi;
            d = fmod(d + T / 2.0, (double)T);
            if (d < 0) d += T;
            d -= T / 2.0;
            f[s] = fabs(d) < 1e-12 ? 1 : pow(sin(M_PI * d) / (T * sin(M_PI * d / T)), 2);
        }
        p[y] = 0.5 * (f[0] + f[1]);
        total += p[y];
    }
    double u = s_uniform() * total, acc = 0;
    long y = 0;
    for (; y < T - 1; y++) { acc += p[y]; if (u < acc) break; }
    free(p);
    double est = N * pow(sin(M_PI * (double)y / T), 2);
    *count = lround(est);
    *bound = 2 * M_PI * sqrt(est * (N - est)) / T + M_PI * M_PI * N / ((double)T * T);
    snprintf(report, len, "QCOUNT %s, %d bits: phase oracle of %d qubits and %d gates from the machine code; quantum "
             "counting with %d precision qubits, %ld controlled Grover iterations: about %ld (error bound %.1f; "
             "simulated)", o->name, o->bits, o->n, o->ngates, t, T - 1, *count, *bound);
    return 1;
}
