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

/* amplitude estimation (Brassard, Hoyer, Mosca, Tapp), simulated: phase estimation with t precision qubits of the
 * operator with eigenphases +-2 theta, sin^2 theta = a, the outcome drawn from its exact distribution (Fejer kernels
 * around +-theta/pi); the median of 5 runs, so the bound 2 pi sqrt(a(1-a))/T + pi^2/T^2 holds with probability ~0.98 */
#define MEDIAN_OF 5
static double amp_estimate(double a, int bits, double *bound, int *t_out, long *queries)
{
    int t = (bits + 1) / 2 + 3;
    long T = 1L << t;
    double phi = asin(sqrt(a < 0 ? 0 : a > 1 ? 1 : a)) / M_PI, total = 0, *p = malloc(sizeof *p * (size_t)T);
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
    double runs[MEDIAN_OF];
    for (int r = 0; r < MEDIAN_OF; r++) {
        double u = s_uniform() * total, acc = 0;
        long y = 0;
        for (; y < T - 1; y++) { acc += p[y]; if (u < acc) break; }
        runs[r] = pow(sin(M_PI * (double)y / T), 2);
    }
    free(p);
    for (int i = 1; i < MEDIAN_OF; i++)                  /* the median */
        for (int j = i; j > 0 && runs[j] < runs[j - 1]; j--) { double x = runs[j]; runs[j] = runs[j - 1]; runs[j - 1] = x; }
    double est = runs[MEDIAN_OF / 2];
    *bound = 2 * M_PI * sqrt(est * (1 - est)) / T + M_PI * M_PI / ((double)T * T);
    *t_out = t;
    *queries = MEDIAN_OF * (T - 1);
    return est;
}

/* QCOUNT: quantum counting, the amplitude estimation of M / N */
int qarm_rt_count(const qarm_oracle *o, long *count, double *bound, char *report, size_t len)
{
    char err[300];
    if (o->out_bits) { snprintf(report, len, "QCOUNT %s: a compute oracle, not a phase oracle", o->name); return -1; }
    long N = 1L << o->bits, M = 0, queries;
    unsigned char *mark = malloc((size_t)N);
    if (evaluate(o, mark, NULL, err, sizeof err) < 0) { snprintf(report, len, "%s", err); free(mark); return -1; }
    for (long b = 0; b < N; b++) M += mark[b];
    free(mark);
    int t;
    double b, est = amp_estimate((double)M / (double)N, o->bits, &b, &t, &queries);
    *count = lround(est * N);
    *bound = b * N;
    snprintf(report, len, "QCOUNT %s, %d bits: phase oracle of %d qubits and %d gates from the machine code; quantum "
             "counting with %d precision qubits (the median of %d runs), %ld controlled Grover iterations: about %ld "
             "(error bound %.1f; simulated)", o->name, o->bits, o->n, o->ngates, t, MEDIAN_OF, queries, *count, *bound);
    return 1;
}

/* QAMPEST: the mean of g(x) in 0 .. 2^k - 1, as the amplitude a = E[g] / 2^k */
int qarm_rt_ampest(const qarm_oracle *o, int k, double *mean, double *bound, char *report, size_t len)
{
    char err[300];
    if (!o->out_bits) { snprintf(report, len, "QAMPEST %s: a phase oracle, not a compute oracle", o->name); return -1; }
    if (k < 1 || k > 31) { snprintf(report, len, "QAMPEST %s: value bits %d: 1 to 31", o->name, k); return -1; }
    long N = 1L << o->bits, queries;
    int64_t *v = malloc(sizeof *v * (size_t)N);
    if (evaluate(o, NULL, v, err, sizeof err) < 0) { snprintf(report, len, "%s", err); free(v); return -1; }
    double sum = 0, top = (double)(1L << k);
    for (long x = 0; x < N; x++) {
        if (v[x] < 0 || v[x] >= (int64_t)(1L << k)) {
            snprintf(report, len, "QAMPEST %s: g(%ld) = %lld, outside 0 .. 2^%d - 1", o->name, x, (long long)v[x], k);
            free(v);
            return -1;
        }
        sum += (double)v[x];
    }
    free(v);
    int t;
    double b, est = amp_estimate(sum / N / top, o->bits, &b, &t, &queries);
    *mean = est * top;
    *bound = b * top;
    snprintf(report, len, "QAMPEST %s, %d bits: compute oracle of %d qubits and %d gates from the machine code; "
             "amplitude estimation with %d precision qubits (the median of %d runs, %ld queries): mean %.4f +- %.4f "
             "(simulated)", o->name, o->bits, o->n, o->ngates, t, MEDIAN_OF, queries, *mean, *bound);
    return 1;
}

/* ---------------------------------------------------------------- QPERIOD, QSIMON, QBV */
static long sample_p(const double *p, long N)
{
    double total = 0, acc = 0;
    for (long b = 0; b < N; b++) total += p[b];
    double u = s_uniform() * total;
    for (long b = 0; b < N - 1; b++) { acc += p[b]; if (u < acc) return b; }
    return N - 1;
}

static void fft(double *re, double *im, long N)          /* in place, radix 2, e^{+2 pi i jk / N} */
{
    for (long i = 1, j = 0; i < N; i++) {
        long bit = N >> 1;
        for (; j & bit; bit >>= 1) j ^= bit;
        j ^= bit;
        if (i < j) { double t = re[i]; re[i] = re[j]; re[j] = t; t = im[i]; im[i] = im[j]; im[j] = t; }
    }
    for (long len = 2; len <= N; len <<= 1) {
        double a = 2 * M_PI / (double)len, wr = cos(a), wi = sin(a);
        for (long i = 0; i < N; i += len) {
            double cr = 1, ci = 0;
            for (long k = 0; k < len / 2; k++) {
                long u = i + k, v = i + k + len / 2;
                double tr = re[v] * cr - im[v] * ci, ti = re[v] * ci + im[v] * cr;
                re[v] = re[u] - tr; im[v] = im[u] - ti;
                re[u] += tr; im[u] += ti;
                double nr = cr * wr - ci * wi;
                ci = cr * wi + ci * wr;
                cr = nr;
            }
        }
    }
}

static void wht(double *v, long N)                       /* the normalized Walsh-Hadamard transform */
{
    for (long h = 1; h < N; h <<= 1)
        for (long i = 0; i < N; i += 2 * h)
            for (long j = i; j < i + h; j++) {
                double a = v[j], b = v[j + h];
                v[j] = a + b;
                v[j + h] = a - b;
            }
    double s = 1 / sqrt((double)N);
    for (long i = 0; i < N; i++) v[i] *= s;
}

static int periodic(const int64_t *v, long N, long r)
{
    if (r <= 0 || r >= N) return 0;
    for (long x = 0; x + r < N; x++)
        if (v[x] != v[x + r]) return 0;
    return 1;
}

static int cmp_long(const void *a, const void *b) { long x = *(const long *)a, y = *(const long *)b; return (x > y) - (x < y); }

int qarm_rt_period(const qarm_oracle *o, int *r, char *report, size_t len)
{
    char err[300];
    if (!o->out_bits) { snprintf(report, len, "QPERIOD %s: a phase oracle, not a compute oracle", o->name); return -1; }
    long N = 1L << o->bits;
    int64_t *v = malloc(sizeof *v * (size_t)N);
    if (evaluate(o, NULL, v, err, sizeof err) < 0) { snprintf(report, len, "%s", err); free(v); return -1; }
    double *re = malloc(sizeof *re * (size_t)N), *im = malloc(sizeof *im * (size_t)N);
    long found = -1;
    int q;
    for (q = 1; q <= 24 && found < 0; q++) {
        long x0 = (long)(s_uniform() * N), n = 0;            /* measuring f: the inputs with f(x0)'s value */
        for (long x = 0; x < N; x++) n += v[x] == v[x0];
        for (long x = 0; x < N; x++) { re[x] = v[x] == v[x0] ? 1 / sqrt((double)n) : 0; im[x] = 0; }
        fft(re, im, N);
        for (long x = 0; x < N; x++) re[x] = (re[x] * re[x] + im[x] * im[x]) / (double)N;
        long y = sample_p(re, N);
        if (!y) continue;
        long cand[256], nc = 0, a = y, b = N, h0 = 0, h1 = 1, k0 = 1, k1 = 0;
        while (b && nc < 250) {                              /* the convergents of y / N */
            long t = a / b, rem = a - t * b, h2 = t * h1 + h0, k2 = t * k1 + k0;
            a = b; b = rem; h0 = h1; h1 = h2; k0 = k1; k1 = k2;
            for (long m = 1; m <= 4; m++) cand[nc++] = k1 * m;
        }
        qsort(cand, (size_t)nc, sizeof *cand, cmp_long);
        for (long i = 0; i < nc && found < 0; i++)
            if (periodic(v, N, cand[i]))
                for (long d = 1; d <= cand[i]; d++)          /* its least periodic divisor */
                    if (cand[i] % d == 0 && periodic(v, N, d)) { found = d; break; }
    }
    free(re); free(im); free(v);
    if (found < 0) {
        snprintf(report, len, "QPERIOD %s, %d bits: no period after 24 quantum queries (simulated)", o->name, o->bits);
        return 2;
    }
    *r = (int)found;
    snprintf(report, len, "QPERIOD %s, %d bits: compute oracle of %d qubits and %d gates from the machine code; the "
             "quantum Fourier transform and continued fractions: period r = %ld after %d quantum quer%s (simulated)",
             o->name, o->bits, o->n, o->ngates, found, q - 1, q - 1 == 1 ? "y" : "ies");
    return 1;
}

int qarm_rt_simon(const qarm_oracle *o, int *s, char *report, size_t len)
{
    char err[300];
    if (!o->out_bits) { snprintf(report, len, "QSIMON %s: a phase oracle, not a compute oracle", o->name); return -1; }
    long N = 1L << o->bits;
    int64_t *v = malloc(sizeof *v * (size_t)N);
    if (evaluate(o, NULL, v, err, sizeof err) < 0) { snprintf(report, len, "%s", err); free(v); return -1; }
    double *amp = malloc(sizeof *amp * (size_t)N);
    long rows[64] = {0};                                     /* rows[lead]: a row with that leading bit, or 0 */
    int rank = 0, queries = 0;
    while (rank < o->bits - 1 && queries < 4 * o->bits + 8) {
        queries++;
        long x0 = (long)(s_uniform() * N), n = 0;
        for (long x = 0; x < N; x++) n += v[x] == v[x0];
        for (long x = 0; x < N; x++) amp[x] = v[x] == v[x0] ? 1 / sqrt((double)n) : 0;
        wht(amp, N);
        for (long x = 0; x < N; x++) amp[x] *= amp[x];
        long y = sample_p(amp, N);
        for (int lead = o->bits - 1; lead >= 0; lead--)
            if ((y >> lead & 1) && rows[lead]) y ^= rows[lead];
        if (y) {
            int lead = 63;
            while (!(y >> lead & 1)) lead--;
            rows[lead] = y;
            rank++;
        }
    }
    long found = -1;
    for (long c = 1; c < N && found < 0; c++) {              /* a nonzero s orthogonal to every row */
        int ok = 1;
        for (int l = 0; l < o->bits && ok; l++)
            if (rows[l]) { long p = rows[l] & c; int odd = 0; while (p) { p &= p - 1; odd ^= 1; } ok = !odd; }
        for (long x = 0; x < N && ok; x++) ok = v[x] == v[x ^ c];
        if (ok) found = c;
    }
    free(amp); free(v);
    if (found < 0) {
        snprintf(report, len, "QSIMON %s, %d bits: no hidden shift after %d quantum queries (simulated)", o->name,
                 o->bits, queries);
        return 2;
    }
    *s = (int)found;
    snprintf(report, len, "QSIMON %s, %d bits: compute oracle of %d qubits and %d gates from the machine code; "
             "hidden shift s = %#lx after %d quantum queries (simulated)", o->name, o->bits, o->n, o->ngates, found,
             queries);
    return 1;
}

int qarm_rt_bv(const qarm_oracle *o, int *s, char *report, size_t len)
{
    char err[300];
    if (o->out_bits) { snprintf(report, len, "QBV %s: a compute oracle, not a phase oracle", o->name); return -1; }
    long N = 1L << o->bits;
    unsigned char *mark = malloc((size_t)N);
    if (evaluate(o, mark, NULL, err, sizeof err) < 0) { snprintf(report, len, "%s", err); free(mark); return -1; }
    double *amp = malloc(sizeof *amp * (size_t)N);
    for (long x = 0; x < N; x++) amp[x] = (mark[x] ? -1 : 1) / sqrt((double)N);
    wht(amp, N);
    long best = 0;
    for (long x = 1; x < N; x++) if (amp[x] * amp[x] > amp[best] * amp[best]) best = x;
    free(amp); free(mark);
    *s = (int)best;
    snprintf(report, len, "QBV %s, %d bits: phase oracle of %d qubits and %d gates from the machine code; hidden "
             "string s = %#lx with one quantum query (simulated)", o->name, o->bits, o->n, o->ngates, best);
    return 1;
}
