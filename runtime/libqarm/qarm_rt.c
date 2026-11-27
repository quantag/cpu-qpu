/* SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 Quantag IT Solutions GmbH
 *
 * libqarm: the native Q-ARM runtime's VQE calls (qarm_rt.h). */
#ifndef QARM_RUNTIME
#define QARM_RUNTIME
#endif
#include <stdint.h>
#include "qarm.h"
#include "qarm_internal.h"
#include "qarm_sv.h"
#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

enum { B_EXACT, B_SIMULATOR, B_IBM };
#define MAX_QUBITS 20

typedef struct { uint64_t x, z; double c; } term;
typedef struct { uint64_t x, z; int nt; term *t; } group;          /* a measurement basis and its terms */

struct qarm_rt {
    int n, params, nops, ngroups, backend, mitigate;
    uint64_t init;
    double identity;
    qarm_op *ops;
    qarm_pauli *terms;
    qarm_hamiltonian h;                  /* copies of the program's tables */
    qarm_ansatz a;
    double *buf;                         /* exact, simulator: the state vector (qarm_sv.h) */
    qarm_sv sv;
    group *groups;
    qibm *ibm;
    char where[200];
    struct qarm_rt *next;
};

/* a job: k energies, each of a parameter vector with at most one rotation's angle shifted (for QGRAD) */
struct qarm_job {
    qarm_rt *r;
    int k, shots, state, ncirc;          /* state: 1 done, 0 on the QPU, -1 failed */
    double *thetas, *e, *err, t0;
    int *ent_theta, *ent_op;             /* per energy: its parameter vector; the shifted operation, or -1 */
    double *ent_delta;
    int *final;                          /* per circuit: the chain position measured into each bit */
    const char *what;
    char id[128];
};

/* ---------------------------------------------------------------- configuration */
static struct {
    int loaded, backend, shots, mitigate;
    double poll;
    qj *doc;
    const qj *opts;
    char path[512];
} cfg;
static int g_session;
static qarm_rt *g_live;

static void config(void)
{
    if (cfg.loaded) return;
    cfg.loaded = 1;
    cfg.backend = B_EXACT;
    cfg.shots = 1024;
    cfg.mitigate = 1;
    cfg.poll = 5;
    const char *path = getenv("QARM_CONFIG");            /* only when asked: no run spends QPU time by accident */
    snprintf(cfg.path, sizeof cfg.path, "%s", path && *path ? path : "");
    const char *backend = NULL;
    FILE *f = cfg.path[0] ? fopen(cfg.path, "rb") : NULL;
    if (f) {
        fseek(f, 0, SEEK_END);
        long len = ftell(f);
        fseek(f, 0, SEEK_SET);
        char *text = calloc((size_t)len + 1, 1);
        size_t got = fread(text, 1, (size_t)len, f);
        text[got] = 0;
        fclose(f);
        char err[200];
        cfg.doc = qj_parse(text, err, sizeof err);
        free(text);
        if (!cfg.doc)
            qarm_note("%s: not valid JSON (%s); using the exact simulator", cfg.path, err);
        else {
            backend = qj_str(qj_get(cfg.doc, "backend"), NULL);
            cfg.shots = (int)qj_num(qj_get(cfg.doc, "shots"), 1024);
        }
    } else if (path && *path)
        qarm_note("%s: not found; using the exact simulator", cfg.path);
    const char *env = getenv("QARM_BACKEND");
    if (env && *env) backend = env;
    if (backend) {
        if (!strcmp(backend, "exact")) cfg.backend = B_EXACT;
        else if (!strcmp(backend, "simulator")) cfg.backend = B_SIMULATOR;
        else if (!strcmp(backend, "ibm")) cfg.backend = B_IBM;
        else {
            qarm_note("backend \"%s\" needs Python (qarm_exec.py); libqarm has exact, simulator and ibm. "
                      "Using the exact simulator", backend);
            cfg.backend = B_EXACT;
        }
    }
    if (cfg.doc) {
        const char *name = cfg.backend == B_IBM ? "ibm" : "simulator";
        cfg.opts = qj_get(cfg.doc, "options");
        if (!cfg.opts || (qj_get(cfg.doc, name) && cfg.backend != B_IBM)) cfg.opts = qj_get(cfg.doc, name);
        const qj *m = qj_get(cfg.opts, "readout_mitigation");
        if (m) cfg.mitigate = (int)qj_num(m, 1);
        cfg.poll = qj_num(qj_get(cfg.opts, "poll_seconds"), 5);
    }
}

/* ---------------------------------------------------------------- problems */
static int popc(uint64_t v) { int n = 0; while (v) { v &= v - 1; n++; } return n; }

static void build_groups(qarm_rt *r, const qarm_hamiltonian *h)
{
    r->groups = calloc(h->terms ? h->terms : 1, sizeof *r->groups);
    r->identity = h->constant;
    for (uint32_t j = 0; j < h->terms; j++) {
        uint64_t x = h->t[j].x, z = h->t[j].z;
        if (!(x | z)) { r->identity += h->t[j].c; continue; }
        group *g = NULL;
        for (int i = 0; i < r->ngroups && !g; i++) {
            uint64_t both = (x | z) & (r->groups[i].x | r->groups[i].z);
            if ((((x ^ r->groups[i].x) | (z ^ r->groups[i].z)) & both) == 0) g = &r->groups[i];
        }
        if (!g) { g = &r->groups[r->ngroups++]; g->t = calloc(h->terms, sizeof *g->t); }
        g->x |= x;
        g->z |= z;
        g->t[g->nt++] = (term){x, z, h->t[j].c};
    }
}

static const char *check(const qarm_hamiltonian *h, const qarm_ansatz *a, char *why, size_t len)
{
    uint64_t full = h->qubits >= 64 ? ~0ULL : (1ULL << h->qubits) - 1;
    if (h->qubits < 1 || h->qubits > MAX_QUBITS) { snprintf(why, len, "%u qubits: libqarm takes 1 to %d", h->qubits, MAX_QUBITS); return why; }
    if (a->qubits != h->qubits) { snprintf(why, len, "the ansatz has %u qubits, the Hamiltonian %u", a->qubits, h->qubits); return why; }
    if (a->params > 255) { snprintf(why, len, "%u parameters: up to 255", a->params); return why; }
    if (a->init & ~full) { snprintf(why, len, "the initial state has bits beyond %u qubits", h->qubits); return why; }
    for (uint32_t j = 0; j < h->terms; j++)
        if ((h->t[j].x | h->t[j].z) & ~full) { snprintf(why, len, "Pauli term %u acts on a qubit beyond %u", j, h->qubits); return why; }
    for (uint32_t i = 0; i < a->ops; i++) {
        const qarm_op *o = &a->op[i];
        int two = o->kind == QARM_CX || o->kind == QARM_CZ, rot = o->kind >= QARM_RY;
        if (o->kind < QARM_X || o->kind > QARM_PAULI_ROT) { snprintf(why, len, "ansatz operation %u: unknown kind %u", i, o->kind); return why; }
        if (o->kind == QARM_PAULI_ROT ? (!(o->x | o->z) || ((o->x | o->z) & ~full))
                                      : (o->q0 >= h->qubits || (two && (o->q1 >= h->qubits || o->q1 == o->q0)))) {
            snprintf(why, len, "ansatz operation %u: qubits %u, %u of %u", i, o->q0, o->q1, h->qubits);
            return why;
        }
        if (rot && o->param >= a->params) { snprintf(why, len, "ansatz operation %u: parameter %u of %u", i, o->param, a->params); return why; }
    }
    return NULL;
}

/* the ansatz at theta, then the basis change of group g; every qubit measured */
static void circuit(const qarm_rt *r, const double *theta, int shift_op, double delta, const group *g, qcirc *c)
{
    c->qubits = r->n;
    for (int q = 0; q < r->n; q++)
        if (r->init >> q & 1) qc_add(c, G_X, q, 0, 0);
    for (int i = 0; i < r->nops; i++) {
        const qarm_op *o = &r->ops[i];
        double a = (o->kind >= QARM_RY ? o->scale * theta[o->param] : 0) + (i == shift_op ? delta : 0);
        switch (o->kind) {
        case QARM_X: qc_add(c, G_X, o->q0, 0, 0); break;
        case QARM_H: qc_add(c, G_H, o->q0, 0, 0); break;
        case QARM_CX: qc_add(c, G_CX, o->q0, o->q1, 0); break;
        case QARM_CZ: qc_add(c, G_CZ, o->q0, o->q1, 0); break;
        case QARM_RY: qc_add(c, G_RY, o->q0, 0, a); break;
        case QARM_RZ: qc_add(c, G_RZ, o->q0, 0, a); break;
        case QARM_PAULI_ROT: {                         /* exp(-i a P): to Z, a CX ladder, RZ(2a), back */
            int s[64], m = 0;
            for (int q = 0; q < r->n; q++)
                if ((o->x | o->z) >> q & 1) s[m++] = q;
            for (int j = 0; j < m; j++) {
                int xb = o->x >> s[j] & 1, zb = o->z >> s[j] & 1;
                if (xb && zb) { qc_add(c, G_SDG, s[j], 0, 0); qc_add(c, G_H, s[j], 0, 0); }
                else if (xb) qc_add(c, G_H, s[j], 0, 0);
            }
            for (int j = 0; j + 1 < m; j++) qc_add(c, G_CX, s[j], s[j + 1], 0);
            qc_add(c, G_RZ, s[m - 1], 0, 2 * a);
            for (int j = m - 2; j >= 0; j--) qc_add(c, G_CX, s[j], s[j + 1], 0);
            for (int j = 0; j < m; j++) {
                int xb = o->x >> s[j] & 1, zb = o->z >> s[j] & 1;
                if (xb && zb) { qc_add(c, G_H, s[j], 0, 0); qc_add(c, G_S, s[j], 0, 0); }
                else if (xb) qc_add(c, G_H, s[j], 0, 0);
            }
            break;
        }
        }
    }
    if (g)
        for (int q = 0; q < r->n; q++) {
            int xb = g->x >> q & 1, zb = g->z >> q & 1;
            if (xb && zb) { qc_add(c, G_SDG, q, 0, 0); qc_add(c, G_H, q, 0, 0); }
            else if (xb) qc_add(c, G_H, q, 0, 0);
        }
}

/* the group's mean and variance per shot over a distribution p of 2^n outcomes */
static void moments(const qarm_rt *r, const group *g, const double *p, double *mean, double *var)
{
    long N = 1L << r->n;
    double m = 0, m2 = 0;
    for (long b = 0; b < N; b++) {
        if (p[b] == 0) continue;
        double v = 0;
        for (int j = 0; j < g->nt; j++)
            v += g->t[j].c * ((popc((uint64_t)b & (g->t[j].x | g->t[j].z)) & 1) ? -1 : 1);
        m += p[b] * v;
        m2 += p[b] * v * v;
    }
    *mean = m;
    *var = m2 - m * m > 0 ? m2 - m * m : 0;
}

static uint64_t rng_state;
static double uniform(void)
{
    if (!rng_state) {
        const char *s = getenv("QARM_SEED");
        rng_state = s && *s ? strtoull(s, NULL, 10) * 0x9E3779B97F4A7C15ULL + 1
                            : (uint64_t)(qarm_now() * 1e6) ^ (uint64_t)(uintptr_t)&rng_state;
    }
    uint64_t z = (rng_state += 0x9E3779B97F4A7C15ULL);
    z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ULL;
    z = (z ^ (z >> 27)) * 0x94D049BB133111EBULL;
    return (double)((z ^ (z >> 31)) >> 11) / 9007199254740992.0;
}

int qarm_rt_load(const qarm_hamiltonian *h, const qarm_ansatz *a, qarm_rt **out)
{
    char why[200], err[300];
    config();
    *out = NULL;
    if (check(h, a, why, sizeof why)) { qarm_note("QVLOAD refused: %s", why); return -1; }
    qarm_rt *r = calloc(1, sizeof *r);
    r->n = (int)h->qubits;
    r->params = (int)a->params;
    r->nops = (int)a->ops;
    r->init = a->init;
    r->ops = malloc(sizeof *r->ops * (a->ops ? a->ops : 1));
    memcpy(r->ops, a->op, sizeof *r->ops * a->ops);
    build_groups(r, h);
    r->terms = malloc(sizeof *r->terms * (h->terms ? h->terms : 1));
    memcpy(r->terms, h->t, sizeof *r->terms * h->terms);
    r->h = (qarm_hamiltonian){h->qubits, h->terms, h->constant, r->terms};
    r->a = (qarm_ansatz){a->qubits, a->params, a->ops, 0, a->init, r->ops};
    r->backend = cfg.backend;
    if (r->backend != B_IBM) {
        long N = 1L << r->n;
        r->buf = malloc(sizeof *r->buf * 6 * (size_t)N);
        if (!r->buf) { qarm_note("QVLOAD refused: no memory for %d qubits", r->n); qarm_rt_free(r); return -1; }
        r->sv = (qarm_sv){r->n, r->buf, r->buf + N, r->buf + 2 * N, r->buf + 3 * N, r->buf + 4 * N, r->buf + 5 * N};
    }
    int twoq = 0, two_ansatz = 0;
    for (int i = 0; i < r->nops; i++) two_ansatz += r->ops[i].kind == QARM_CX || r->ops[i].kind == QARM_CZ;
    if (r->backend == B_IBM) {
        r->ibm = qibm_open(cfg.opts, r->n, err, sizeof err);
        if (!r->ibm) { qarm_note("QVLOAD refused: ibm: %s", err); qarm_rt_free(r); return -1; }
        r->mitigate = cfg.mitigate;
        double *zero = calloc((size_t)r->params + 1, sizeof *zero);
        for (int i = 0; i < r->ngroups; i++) {
            qcirc c = {0};
            int *final = malloc(sizeof *final * (size_t)r->n), t;
            circuit(r, zero, -1, 0, &r->groups[i], &c);
            free(qibm_qasm(r->ibm, &c, final, &t));
            if (t > twoq) twoq = t;
            free(final);
            qc_free(&c);
        }
        free(zero);
        snprintf(r->where, sizeof r->where, "ibm: %s", qibm_device(r->ibm));
        if (g_session) { qibm_session(r->ibm, 1, err, sizeof err); qarm_note("QSESSION: %s", err); }
    } else
        snprintf(r->where, sizeof r->where, "%s", r->backend == B_EXACT ? "exact" : "simulator");
    r->next = g_live;
    g_live = r;
    *out = r;
    char extra[200] = "";
    if (r->backend == B_IBM)
        snprintf(extra, sizeof extra, ", %s, up to %d two-qubit gates after transpiling",
                 r->mitigate ? "readout correction" : "no readout correction", twoq);
    qarm_note("QVLOAD: %d qubits, %u Pauli terms, an ansatz of %d operations (%d two-qubit) with %d parameter%s; "
              "libqarm on %s: %d measurement bases%s", r->n, h->terms, r->nops, two_ansatz, r->params,
              r->params == 1 ? "" : "s", r->where, r->ngroups, extra);
    return r->backend == B_EXACT ? 0 : 1;
}

void qarm_rt_free(qarm_rt *r)
{
    if (!r) return;
    for (qarm_rt **p = &g_live; *p; p = &(*p)->next)
        if (*p == r) { *p = r->next; break; }
    if (r->ibm) { char e[200]; qibm_session(r->ibm, 0, e, sizeof e); qibm_close(r->ibm); }
    for (int i = 0; i < r->ngroups; i++) free(r->groups[i].t);
    free(r->groups);
    free(r->ops);
    free(r->terms);
    free(r->buf);
    free(r);
}

const char *qarm_rt_where(const qarm_rt *r) { return r ? r->where : "none"; }

int qarm_rt_session(int open)
{
    config();
    g_session = open != 0;
    char err[300];
    for (qarm_rt *r = g_live; r; r = r->next)
        if (r->ibm) {
            qibm_session(r->ibm, g_session, err, sizeof err);
            qarm_note("QSESSION: %s", err);
        }
    return 1;
}

/* ---------------------------------------------------------------- jobs */
static void prepare(qarm_rt *r, const double *theta, int shift_op, double delta)
{
    qarm_sv *sv = &r->sv;
    for (long b = 0; b < (1L << r->n); b++) sv->re[b] = sv->im[b] = 0;
    sv->re[r->init] = 1;
    for (int o = 0; o < r->nops; o++)
        qarm_sv_apply(sv, sv->re, sv->im, &r->ops[o], qarm_sv_angle(&r->ops[o], theta) + (o == shift_op ? delta : 0));
}

static void finish_local(qarm_job *j)
{
    qarm_rt *r = j->r;
    long N = 1L << r->n;
    qarm_sv *sv = &r->sv;
    double *p = malloc(sizeof *p * (size_t)N), *q = malloc(sizeof *q * (size_t)N);
    for (int i = 0; i < j->k; i++) {
        prepare(r, j->thetas + (long)j->ent_theta[i] * r->params, j->ent_op[i], j->ent_delta[i]);
        if (r->backend == B_EXACT) { j->e[i] = qarm_sv_energy(sv, &r->h); j->err[i] = 0; continue; }
        double e = r->identity, var = 0;
        for (int g = 0; g < r->ngroups; g++) {            /* simulator: each basis measured on a copy */
            memcpy(sv->lr, sv->re, sizeof *p * (size_t)N);
            memcpy(sv->li, sv->im, sizeof *p * (size_t)N);
            for (int qb = 0; qb < r->n; qb++) {
                int xb = r->groups[g].x >> qb & 1, zb = r->groups[g].z >> qb & 1;
                qarm_op h = {QARM_H, (uint8_t)qb, 0, 0, 0, 0, 0, 0}, sdg = {QARM_RZ, (uint8_t)qb, 0, 0, 0, 0, 0, 0};
                if (xb && zb) qarm_sv_apply(sv, sv->lr, sv->li, &sdg, -M_PI / 2);   /* S^dagger, up to a phase */
                if (xb) qarm_sv_apply(sv, sv->lr, sv->li, &h, 0);
            }
            for (long b = 0; b < N; b++) p[b] = sv->lr[b] * sv->lr[b] + sv->li[b] * sv->li[b];
            for (long b = 0; b < N; b++) q[b] = 0;
            for (long b = 1; b < N; b++) p[b] += p[b - 1];          /* the cumulative distribution */
            for (int s = 0; s < j->shots; s++) {                      /* each shot by binary search */
                double u = uniform() * p[N - 1];
                long lo = 0, hi = N - 1;
                while (lo < hi) { long mid = (lo + hi) / 2; if (u < p[mid]) hi = mid; else lo = mid + 1; }
                q[lo] += 1.0 / j->shots;
            }
            double m, v;
            moments(r, &r->groups[g], q, &m, &v);
            e += m;
            var += v / j->shots;
        }
        j->e[i] = e;
        j->err[i] = sqrt(var);
    }
    j->state = 1;
    free(p);
    free(q);
}

static void job_free(qarm_job *j)
{
    if (!j) return;
    free(j->thetas); free(j->e); free(j->err); free(j->ent_theta); free(j->ent_op); free(j->ent_delta); free(j->final);
    free(j);
}

/* k energies: of parameter vector ent_theta[i] (of ntheta in thetas), with operation ent_op[i] shifted by ent_delta[i] */
static qarm_job *submit(qarm_rt *r, const double *thetas, int ntheta, int k, const int *ent_theta, const int *ent_op,
                        const double *ent_delta, int shots, const char *what)
{
    qarm_job *j = calloc(1, sizeof *j);
    j->r = r;
    j->k = k;
    j->what = what;
    j->shots = shots > 0 ? shots : cfg.shots;
    j->thetas = malloc(sizeof *j->thetas * (size_t)(ntheta * r->params + 1));
    memcpy(j->thetas, thetas, sizeof *j->thetas * (size_t)(ntheta * r->params));
    j->e = calloc((size_t)k, sizeof *j->e);
    j->err = calloc((size_t)k, sizeof *j->err);
    j->ent_theta = malloc(sizeof(int) * (size_t)k);
    j->ent_op = malloc(sizeof(int) * (size_t)k);
    j->ent_delta = malloc(sizeof(double) * (size_t)k);
    memcpy(j->ent_theta, ent_theta, sizeof(int) * (size_t)k);
    memcpy(j->ent_op, ent_op, sizeof(int) * (size_t)k);
    memcpy(j->ent_delta, ent_delta, sizeof(double) * (size_t)k);
    j->t0 = qarm_now();
    if (r->backend != B_IBM) { finish_local(j); return j; }

    int cal = r->mitigate ? 2 : 0;
    j->ncirc = k * r->ngroups + cal;
    char **qasm = calloc((size_t)j->ncirc, sizeof *qasm);
    j->final = calloc((size_t)j->ncirc * (size_t)r->n, sizeof *j->final);
    int t, ci = 0;
    for (int i = 0; i < k; i++)
        for (int g = 0; g < r->ngroups; g++, ci++) {
            qcirc c = {0};
            circuit(r, j->thetas + (long)ent_theta[i] * r->params, ent_op[i], ent_delta[i], &r->groups[g], &c);
            qasm[ci] = qibm_qasm(r->ibm, &c, j->final + (long)ci * r->n, &t);
            qc_free(&c);
        }
    for (int ones = 0; ones < cal; ones++, ci++) {            /* all 0 and all 1 on the same chain */
        qcirc c = {0};
        c.qubits = r->n;
        if (ones)
            for (int q = 0; q < r->n; q++) qc_add(&c, G_X, q, 0, 0);
        qasm[ci] = qibm_qasm(r->ibm, &c, j->final + (long)ci * r->n, &t);
        qc_free(&c);
    }
    char err[400];
    int ok = qibm_submit(r->ibm, qasm, j->ncirc, j->shots, j->id, sizeof j->id, err, sizeof err);
    for (int i = 0; i < j->ncirc; i++) free(qasm[i]);
    free(qasm);
    if (!ok) { qarm_note("%s: the job was not accepted: %s", what, err); job_free(j); return NULL; }
    qarm_note("%s: job %s sent to %s (%d energies x %d measurement bases%s); asynchronous", what, j->id,
              qibm_device(r->ibm), k, r->ngroups, cal ? " + 2 readout calibrations" : "");
    return j;
}

qarm_job *qarm_rt_submit(qarm_rt *r, const double *thetas, int k, int shots)
{
    if (!r || k < 1) { qarm_note("QEXPECTN: %s", r ? "k < 1" : "no problem loaded"); return NULL; }
    int *et = malloc(sizeof(int) * (size_t)k), *eo = malloc(sizeof(int) * (size_t)k);
    double *ed = calloc((size_t)k, sizeof *ed);
    for (int i = 0; i < k; i++) { et[i] = i; eo[i] = -1; }
    qarm_job *j = submit(r, thetas, k, k, et, eo, ed, shots, "QEXPECTN");
    free(et); free(eo); free(ed);
    return j;
}

/* the energies from the samples, with readout correction from the two calibration circuits */
static int collect(qarm_job *j)
{
    qarm_rt *r = j->r;
    char err[400];
    uint64_t **samples = calloc((size_t)j->ncirc, sizeof *samples);
    int shots = qibm_results(r->ibm, j->id, j->ncirc, samples, err, sizeof err);
    if (shots <= 0) { qarm_note("%s: job %s: %s", j->what, j->id, shots == 0 ? "no shots" : err); goto fail; }
    int n = r->n, cal = r->mitigate ? 2 : 0;
    long N = 1L << n;
    double inv[MAX_QUBITS][4];                                /* per chain position */
    for (int q = 0; q < n; q++) { inv[q][0] = inv[q][3] = 1; inv[q][1] = inv[q][2] = 0; }
    if (cal) {
        const uint64_t *zero = samples[j->ncirc - 2], *one = samples[j->ncirc - 1];
        for (int q = 0; q < n; q++) {
            double e0 = 0, e1 = 0;
            for (int s = 0; s < shots; s++) { e0 += zero[s] >> q & 1; e1 += !(one[s] >> q & 1); }
            e0 /= shots;
            e1 /= shots;
            double a = 1 - e0, b = e1, c = e0, d = 1 - e1, det = a * d - b * c;   /* columns: prepared 0, 1 */
            if (fabs(det) > 0.2) { inv[q][0] = d / det; inv[q][1] = -b / det; inv[q][2] = -c / det; inv[q][3] = a / det; }
        }
    }
    double *p = malloc(sizeof *p * (size_t)N);
    for (int i = 0; i < j->k; i++) {
        double e = r->identity, var = 0;
        for (int g = 0; g < r->ngroups; g++) {
            int ci = i * r->ngroups + g;
            for (long b = 0; b < N; b++) p[b] = 0;
            for (int s = 0; s < shots; s++) p[samples[ci][s] & (uint64_t)(N - 1)] += 1.0 / shots;
            if (cal)
                for (int q = 0; q < n; q++) {
                    const double *m = inv[j->final[(long)ci * n + q]];
                    long bit = 1L << q;
                    for (long b = 0; b < N; b++) {
                        if (b & bit) continue;
                        double p0 = p[b], p1 = p[b | bit];
                        p[b] = m[0] * p0 + m[1] * p1;
                        p[b | bit] = m[2] * p0 + m[3] * p1;
                    }
                }
            double mean, v;
            moments(r, &r->groups[g], p, &mean, &v);
            e += mean;
            var += v / shots;
        }
        j->e[i] = e;
        j->err[i] = sqrt(var);
    }
    free(p);
    for (int i = 0; i < j->ncirc; i++) free(samples[i]);
    free(samples);
    j->state = 1;
    return 1;
fail:
    for (int i = 0; i < j->ncirc; i++) free(samples[i]);
    free(samples);
    j->state = -1;
    return -1;
}

int qarm_rt_poll(qarm_job *j)
{
    if (!j) return -1;
    if (j->state) return j->state;
    char err[400];
    int s = qibm_status(j->r->ibm, j->id, err, sizeof err);
    if (s < 0) { qarm_note("%s: job %s: %s", j->what, j->id, err); j->state = -1; return -1; }
    if (s == 0) return 0;
    return collect(j);
}

static int wait_job(qarm_job *j)
{
    int s, waited = 0;
    while ((s = qarm_rt_poll(j)) == 0) {
        if (!waited++) qarm_note("%s: waiting for job %s on %s (it may be queued)", j->what, j->id, qibm_device(j->r->ibm));
        qarm_sleep(cfg.poll);
    }
    return s;
}

static void how(const qarm_job *j, char *buf, size_t len)
{
    if (j->r->backend == B_IBM) snprintf(buf, len, "job %s, %.1f s", j->id, qarm_now() - j->t0);
    else snprintf(buf, len, "%s", j->r->where);
}

int qarm_rt_wait(qarm_job *j, double *e, double *err)
{
    if (!j) return -1;
    int s = wait_job(j);
    if (s > 0) {
        int m = 0;
        for (int i = 0; i < j->k; i++) {
            e[i] = j->e[i];
            if (err) err[i] = j->err[i];
            if (j->e[i] < j->e[m]) m = i;
        }
        char h[200];
        how(j, h, sizeof h);
        if (j->k == 1 && j->err[0] > 0)
            qarm_note("QEXPECT: E = %.6f +- %.6f Ha (%s)", j->e[0], j->err[0], h);
        else if (j->k == 1)
            qarm_note("QEXPECT: E = %.6f Ha (%s)", j->e[0], h);
        else
            qarm_note("QEXPECTN: %d energies (%s); lowest %.6f Ha", j->k, h, j->e[m]);
    }
    job_free(j);
    return s > 0 ? 1 : -1;
}

int qarm_rt_expect_n(qarm_rt *r, const double *thetas, int k, int shots, double *e, double *err)
{
    double *tmp = err ? NULL : malloc(sizeof *tmp * (size_t)(k > 0 ? k : 1));
    int s = qarm_rt_wait(qarm_rt_submit(r, thetas, k, shots), e, err ? err : tmp);
    free(tmp);
    return s;
}

/* QGRAD: exact by adjoint differentiation; measured by the parameter-shift rule, every shifted energy in one job:
 * dE/d(angle) = E(angle + pi/4) - E(angle - pi/4) for a Pauli rotation, (E(+pi/2) - E(-pi/2)) / 2 for RY and RZ */
int qarm_rt_grad(qarm_rt *r, const double *theta, int shots, double *e, double *grad)
{
    if (!r) { qarm_note("QGRAD: no problem loaded"); return -1; }
    if (r->backend == B_EXACT) {
        *e = qarm_sv_grad(&r->sv, &r->h, &r->a, theta, grad);
        double gn = 0;
        for (int p = 0; p < r->params; p++) gn += grad[p] * grad[p];
        qarm_note("QGRAD: E = %.9f Ha, |gradient| = %.3g (exact, adjoint)", *e, sqrt(gn));
        return 1;
    }
    int k = 1;
    for (int o = 0; o < r->nops; o++) k += 2 * qarm_sv_rot(r->ops[o].kind);
    int *et = calloc((size_t)k, sizeof(int)), *eo = malloc(sizeof(int) * (size_t)k);
    double *ed = calloc((size_t)k, sizeof *ed);
    eo[0] = -1;
    for (int o = 0, i = 1; o < r->nops; o++)
        if (qarm_sv_rot(r->ops[o].kind)) {
            double d = r->ops[o].kind == QARM_PAULI_ROT ? M_PI / 4 : M_PI / 2;
            eo[i] = o; ed[i++] = d;
            eo[i] = o; ed[i++] = -d;
        }
    qarm_job *j = submit(r, theta, 1, k, et, eo, ed, shots, "QGRAD");
    int s = j ? wait_job(j) : -1;
    if (s > 0) {
        *e = j->e[0];
        for (int p = 0; p < r->params; p++) grad[p] = 0;
        for (int o = 0, i = 1; o < r->nops; o++)
            if (qarm_sv_rot(r->ops[o].kind)) {
                double f = r->ops[o].kind == QARM_PAULI_ROT ? 1 : 0.5;
                grad[r->ops[o].param] += r->ops[o].scale * f * (j->e[i] - j->e[i + 1]);
                i += 2;
            }
        char h[200];
        how(j, h, sizeof h);
        qarm_note("QGRAD: E = %.6f Ha, %d shifted energies, parameter-shift rule (%s)", *e, k - 1, h);
    }
    job_free(j);
    free(et); free(eo); free(ed);
    return s > 0 ? 1 : -1;
}

/* ---------------------------------------------------------------- QCIRC, QRAND, QCAPS */
/* a qarm_circuit lowered (qarm_sv_lower) to libqarm's gate list */
static int lower_circuit(const qarm_circuit *c, qcirc *out, char *err, size_t len)
{
    if (!c || c->qubits < 1 || c->qubits > 64 || (c->gates && !c->g)) {
        snprintf(err, len, "not a circuit");
        return -1;
    }
    out->qubits = (int)c->qubits;
    qarm_op ops[16];
    for (uint32_t i = 0; i < c->gates; i++) {
        if (!qarm_sv_gate_ok(&c->g[i], c->qubits)) {
            snprintf(err, len, "gate %u: kind %u on qubits %u %u %u of %u", i, c->g[i].kind, c->g[i].q0, c->g[i].q1,
                     c->g[i].q2, c->qubits);
            return -1;
        }
        int k = qarm_sv_lower(&c->g[i], ops);
        for (int j = 0; j < k; j++) {
            static const int map[] = {[QARM_X] = G_X, [QARM_H] = G_H, [QARM_CX] = G_CX, [QARM_CZ] = G_CZ,
                                      [QARM_RY] = G_RY, [QARM_RZ] = G_RZ};
            qc_add(out, map[ops[j].kind], ops[j].q0, ops[j].q1, ops[j].scale);
        }
    }
    return 0;
}

static int sample_local(const qcirc *c, int shots, uint64_t *samples, char *err, size_t len)
{
    long N = 1L << c->qubits;
    double *p = malloc(sizeof *p * (size_t)N);
    if (!p || qsv_probabilities(c, p, err, len) < 0) { free(p); if (!p) snprintf(err, len, "out of memory"); return -1; }
    for (long b = 1; b < N; b++) p[b] += p[b - 1];
    for (int s = 0; s < shots; s++) {
        double u = uniform() * p[N - 1];
        long lo = 0, hi = N - 1;
        while (lo < hi) { long mid = (lo + hi) / 2; if (u < p[mid]) hi = mid; else lo = mid + 1; }
        samples[s] = (uint64_t)lo;
    }
    free(p);
    return 0;
}

/* one circuit on the configured device, waited for; samples in logical bits */
static int sample_ibm(const qcirc *c, int shots, uint64_t *samples, const char *what, char *where, size_t wlen)
{
    char err[400], id[128];
    qibm *b = qibm_open(cfg.opts, c->qubits, err, sizeof err);
    if (!b) { qarm_note("%s refused: ibm: %s", what, err); return -1; }
    int *final = malloc(sizeof *final * (size_t)c->qubits), twoq = 0, rc = -1;
    char *qasm = qibm_qasm(b, c, final, &twoq);
    double t0 = qarm_now();
    if (qibm_submit(b, &qasm, 1, shots, id, sizeof id, err, sizeof err) < 0) {
        qarm_note("%s: ibm: %s", what, err);
        goto done;
    }
    int s, waited = 0;
    while ((s = qibm_status(b, id, err, sizeof err)) == 0) {
        if (!waited++) qarm_note("%s: waiting for job %s on %s (it may be queued)", what, id, qibm_device(b));
        qarm_sleep(cfg.poll);
    }
    if (s < 0) { qarm_note("%s: job %s: %s", what, id, err); goto done; }
    uint64_t *got = NULL;
    int n = qibm_results(b, id, 1, &got, err, sizeof err);
    if (n <= 0) { qarm_note("%s: job %s: %s", what, id, n == 0 ? "no shots" : err); free(got); goto done; }
    for (int i = 0; i < shots; i++) samples[i] = got[i % n];
    free(got);
    snprintf(where, wlen, "%s (job %s, %d two-qubit gates after transpiling, %.1f s)", qibm_device(b), id, twoq,
             qarm_now() - t0);
    rc = 1;
done:
    free(qasm);
    free(final);
    qibm_close(b);
    return rc;
}

int qarm_rt_circuit(const qarm_circuit *c, int shots, uint64_t *samples)
{
    config();
    char err[300], where[300];
    qcirc q = {0};
    int rc = -1;
    if (shots < 1 || !samples) { qarm_note("QCIRC refused: %d shots", shots); return -1; }
    if (lower_circuit(c, &q, err, sizeof err) < 0) { qarm_note("QCIRC refused: %s", err); goto done; }
    int twoq = 0;
    for (uint32_t i = 0; i < c->gates; i++) twoq += c->g[i].kind >= QARM_G_CX;
    if (cfg.backend == B_IBM) {
        rc = sample_ibm(&q, shots, samples, "QCIRC", where, sizeof where);
    } else {
        if (sample_local(&q, shots, samples, err, sizeof err) < 0) { qarm_note("QCIRC refused: %s", err); goto done; }
        snprintf(where, sizeof where, "the state vector");
        rc = 1;
    }
    if (rc >= 0)
        qarm_note("QCIRC: %u qubits, %u gates (%d multi-qubit), %d shots on %s", c->qubits, c->gates, twoq, shots, where);
done:
    qc_free(&q);
    return rc;
}

int qarm_rt_rand(void *buf, int n)
{
    config();
    if (n < 1 || !buf) return -1;
    uint8_t *out = buf;
    if (cfg.backend == B_IBM) {
        qcirc q = {0};
        q.qubits = 8;
        for (int i = 0; i < 8; i++) qc_add(&q, G_H, i, 0, 0);
        uint64_t *s = malloc(sizeof *s * (size_t)n);
        char where[300];
        int rc = sample_ibm(&q, n, s, "QRAND", where, sizeof where);
        qc_free(&q);
        if (rc > 0) {
            for (int i = 0; i < n; i++) out[i] = (uint8_t)s[i];
            free(s);
            qarm_note("QRAND: %d bytes from H on 8 qubits, measured on %s", n, where);
            return 1;
        }
        free(s);
        return -1;
    }
    for (int i = 0; i < n; i++) out[i] = (uint8_t)(uniform() * 256);
    qarm_note("QRAND: %d bytes, pseudo-random (the %s backend has no quantum randomness)", n,
              cfg.backend == B_EXACT ? "exact" : "simulator");
    return 0;
}

int qarm_rt_caps(qarm_capabilities *caps)
{
    config();
    if (!caps) return -1;
    memset(caps, 0, sizeof *caps);
    caps->version = 1;
    caps->features = 2047;
    caps->qubits = MAX_QUBITS;
    const char *name = cfg.backend == B_EXACT ? "exact" : cfg.backend == B_SIMULATOR ? "simulator" : "ibm";
    if (cfg.backend == B_IBM) {
        caps->flags = QARM_CAPS_HARDWARE | QARM_CAPS_MEASURED | QARM_CAPS_ASYNC;
        const char *dev = qj_str(qj_get(cfg.opts, "device"), NULL);
        if (!dev) dev = qj_str(qj_get(cfg.opts, "backend"), NULL);
        snprintf(caps->backend, sizeof caps->backend, "libqarm: ibm %s", dev ? dev : "(least busy)");
    } else {
        caps->flags = cfg.backend == B_SIMULATOR ? QARM_CAPS_MEASURED : 0;
        snprintf(caps->backend, sizeof caps->backend, "libqarm: %s", name);
    }
    return 1;
}
