/* SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 Quantag IT Solutions GmbH
 *
 * tests/test_qarm_auto.py's loops. Each line with a loop says what QArmAuto must do with it:
 *   expect: QSEARCH | QMIN | QCOUNT | QAMPEST     rewritten (-Rpass=qarm-auto)
 *   expect: missed               marked, but not a loop the mark allows (-Rpass-missed)
 *   expect: candidate            a search or minimum outside QARM_AUTO, a count or sum outside QARM_ESTIMATE
 *                                (-Rpass-analysis)
 * main() runs every rewritten function against a copy that the pass leaves alone (no QARM_AUTO) on tables
 * filled from a seed, and checks what the rewrite promises: a search's answer is a solution, and "none" only when
 * there is none; a minimum's value is the least, and its argument has it; a count or sum is within
 * qarm_auto_error() of the exact one (exact when that is 0). It prints one line per seed. */
#include <stdio.h>
#include <stdlib.h>
#include "qarm_auto.h"

static int table[1024], target;
static int hit(int x) { return table[x] == target; }
static _Bool hit_bool(int x) { return table[x] == target; }
static int cost(int x) { return table[x] - 500; }
static int cost2(int x) { return (table[x] * 7) % 13 - (table[(x + 1) & 1023] & 3); }
static int nohit(int x) { return table[x] == -1; }
int writes;
static int impure(int x) { writes++; return table[x] == target; }
static unsigned char low(int x) { return (unsigned char)table[x]; }
static unsigned short wide(int x) { return (unsigned short)(table[x] * 61); }
static signed char sbyte(int x) { return (signed char)table[x]; }

#define SEARCH(name, N, p) \
    QARM_AUTO static int name(void) { for (int x = 0; x < N; x++) if (p(x)) return x; return -1; }    /* expect: QSEARCH */ \
    static int name##_ref(void) { for (int x = 0; x < N; x++) if (p(x)) return x; return -1; }
SEARCH(s256, 256, hit)
SEARCH(s1000, 1000, hit)
SEARCH(s3, 3, hit)
SEARCH(sbool, 1024, hit_bool)
SEARCH(snone, 512, nohit)

QARM_AUTO static int sflag(int *out)
{
    int found = 0;
    for (int x = 0; x < 700; x++) if (hit(x)) { *out = x; found = 1; break; }              /* expect: QSEARCH */
    return found;
}

#define MIN(name, N, c, OP, b0, a0) \
    QARM_AUTO static int name(int *arg) { int b = b0, a = a0; for (int x = 0; x < N; x++) { int v = c(x); if (v OP b) { b = v; a = x; } } *arg = a; return b; }   /* expect: QMIN */ \
    static int name##_ref(int *arg) { int b = b0, a = a0; for (int x = 0; x < N; x++) { int v = c(x); if (v OP b) { b = v; a = x; } } *arg = a; return b; }
MIN(m256, 256, cost, <, 1 << 30, 0)
MIN(m1000, 1000, cost, <, 1 << 30, -1)
MIN(mle, 512, cost2, <=, 1 << 30, -1)
MIN(mlow, 300, cost, <, -400, 77)        /* a first value below most costs: the loop's own rule on it */
MIN(mties, 256, cost2, <, 100, 5)

/* counts and sums: estimates, with QARM_ESTIMATE; the reference copies keep the loops exact */
#define EST(T, name, body) \
    QARM_ESTIMATE static T name(void) body   /* expect: QCOUNT|QAMPEST */ \
    static T name##_ref(void) body
EST(int, cbranch, { int n = 0; for (int x = 0; x < 256; x++) if (hit(x)) n++; return n; })
EST(long, cbool, { long n = 5; for (int x = 0; x < 300; x++) n += hit_bool(x); return n; })
EST(int, ccond, { int n = 3; for (int x = 0; x < 1000; x++) if (cost(x) > 200) n++; return n; })
EST(int, cnot, { int n = 0; for (int x = 0; x < 512; x++) if (!hit(x)) n++; return n; })
EST(long, cselect, { long n = -2; for (int x = 0; x < 700; x++) n = cost2(x) == 3 ? n + 1 : n; return n; })
EST(long, schar, { long s = 0; for (int x = 0; x < 256; x++) s += low(x); return s; })
EST(int, smask, { int s = 10; for (int x = 0; x < 1000; x++) s += cost(x) & 0x3f; return s; })
EST(double, smean, { double s = 0; for (int x = 0; x < 512; x++) s += low(x); return s / 512; })
EST(long, sshort, { long s = 1; for (int x = 0; x < 1024; x++) s += wide(x); return s; })
EST(float, sfloat, { float s = 0; for (int x = 0; x < 128; x++) s += (float)(cost(x) & 7); return s; })

QARM_ESTIMATE long sraw(void) { long s = 0; for (int x = 0; x < 256; x++) s += table[x]; return s; }                     /* expect: missed */
QARM_ESTIMATE long ssigned(void) { long s = 0; for (int x = 0; x < 256; x++) s += sbyte(x); return s; }                 /* expect: missed */
QARM_ESTIMATE int capped(void) { int n = 0; for (int x = 0; x < 256; x++) if (n < 10 && hit(x)) n++; return n; }         /* expect: missed */
QARM_ESTIMATE long product(void) { long s = 1; for (int x = 0; x < 256; x++) s *= 1 + hit(x); return s; }               /* expect: missed */
QARM_ESTIMATE int esearch(void) { for (int x = 0; x < 256; x++) if (hit(x)) return x; return -1; }                       /* expect: QSEARCH */
long unmarked_sum(void) { long s = 0; for (int x = 0; x < 256; x++) s += low(x); return s; }                              /* expect: candidate */

/* not searches or minimums: the pass must leave them alone */
QARM_AUTO int count(void) { int n = 0; for (int x = 0; x < 256; x++) if (hit(x)) n++; return n; }                         /* expect: candidate */
QARM_AUTO int side(void) { for (int x = 0; x < 256; x++) if (impure(x)) return x; return -1; }                           /* expect: missed */
QARM_AUTO int big(void) { for (int x = 0; x < 100000; x++) if (hit(x & 1023)) return x; return -1; }                     /* expect: missed */
QARM_AUTO int from1(void) { for (int x = 1; x < 256; x++) if (hit(x)) return x; return -1; }                             /* expect: missed */
QARM_AUTO int step2(void) { for (int x = 0; x < 256; x += 2) if (hit(x)) return x; return -1; }                          /* expect: missed */
QARM_AUTO int maxi(int *arg) { int b = -(1 << 30), a = 0; for (int x = 0; x < 256; x++) { int c = cost(x); if (c > b) { b = c; a = x; } } *arg = a; return b; }   /* expect: missed */
QARM_AUTO int inverted(int *arg) { int b = 1 << 30, a = 0; for (int x = 0; x < 256; x++) { int c = cost(x); if (c < b) {} else { b = c; a = x; } } *arg = a; return b; }   /* expect: missed */
QARM_AUTO int stores(int *out) { for (int x = 0; x < 256; x++) { out[x] = 0; if (hit(x)) return x; } return -1; }         /* expect: missed */
int unmarked(void) { for (int x = 0; x < 256; x++) if (hit(x)) return x; return -1; }                                    /* expect: candidate */

static int check_est(const char *n, double got, double ref, double err, int integral)
{
    /* within the bound; an integer result is rounded, half a unit more */
    if (!(err >= 0 && (err == 0 ? got == ref : got >= ref - err - 0.5 * integral - 1e-9 && got <= ref + err + 0.5 * integral + 1e-9))) {
        printf("FAIL %s: %g (bound %g), the loop as written %g\n", n, got, err, ref);
        return 1;
    }
    return 0;
}

static int check_search(const char *n, int got, int ref, int (*p)(int))
{
    if (ref < 0 ? got != -1 : (got < 0 || !p(got))) { printf("FAIL %s: %d, the loop as written %d\n", n, got, ref); return 1; }
    return 0;
}
static int check_min(const char *n, int got, int arg, int ref, int refarg, int (*c)(int), int N, int b0, int a0)
{
    /* the least value; its argument has it, or is the loop's first argument when nothing beat the first value */
    int ok = got == ref && (arg == a0 && ref == b0 ? 1 : arg >= 0 && arg < N && c(arg) == got);
    if (!ok) { printf("FAIL %s: %d at %d, the loop as written %d at %d\n", n, got, arg, ref, refarg); return 1; }
    return 0;
}

int main(int argc, char **argv)
{
    int seeds = argc > 1 ? atoi(argv[1]) : 20, fails = 0;
    for (int seed = 0; seed < seeds; seed++) {
        srand(seed * 7919 + 1);
        for (int i = 0; i < 1024; i++) table[i] = rand() % 1000;
        target = table[rand() % 1024];
        if (seed % 5 == 4) target = 5000;                  /* no solution anywhere */
        int a, ra;
        fails += check_search("s256", s256(), s256_ref(), hit);
        fails += check_search("s1000", s1000(), s1000_ref(), hit);
        fails += check_search("s3", s3(), s3_ref(), hit);
        fails += check_search("sbool", sbool(), sbool_ref(), hit);
        fails += check_search("snone", snone(), snone_ref(), nohit);
        int out = -7, f = sflag(&out);
        fails += check_search("sflag", f ? out : -1, s1000_ref() < 700 ? s1000_ref() : -1, hit);
        int v, rv;
#define RUN(name, N, c, b0, a0) v = name(&a); rv = name##_ref(&ra); fails += check_min(#name, v, a, rv, ra, c, N, b0, a0);
        RUN(m256, 256, cost, 1 << 30, 0)
        RUN(m1000, 1000, cost, 1 << 30, -1)
        RUN(mle, 512, cost2, 1 << 30, -1)
        RUN(mlow, 300, cost, -400, 77)
        RUN(mties, 256, cost2, 100, 5)
        double e, g;
#define EST_RUN(name, integral) g = (double)name(); e = qarm_auto_error(); fails += check_est(#name, g, (double)name##_ref(), e, integral); \
        printf(" %s %g", #name, g);
        printf("seed %d estimates:", seed);
        EST_RUN(cbranch, 1) EST_RUN(cbool, 1) EST_RUN(ccond, 1) EST_RUN(cnot, 1) EST_RUN(cselect, 1)
        EST_RUN(schar, 1) EST_RUN(smask, 1) EST_RUN(sshort, 1) EST_RUN(sfloat, 0)
        g = smean(); e = qarm_auto_error() / 512; fails += check_est("smean", g, smean_ref(), e, 0);
        printf(" smean %g\n", g);
        printf("seed %d: s256 %d s1000 %d s3 %d sbool %d snone %d sflag %d/%d m256 %d m1000 %d mle %d mlow %d mties %d\n", seed,
               s256(), s1000(), s3(), sbool(), snone(), f, out, m256(&a), m1000(&a), mle(&a), mlow(&a), mties(&a));
    }
    (void)count; (void)side; (void)big; (void)from1; (void)step2; (void)maxi; (void)inverted; (void)stores; (void)unmarked;
    (void)sraw; (void)ssigned; (void)capped; (void)product; (void)esearch; (void)unmarked_sum;
    printf(fails ? "FAILED %d\n" : "all checks passed\n", fails);
    return fails != 0;
}
