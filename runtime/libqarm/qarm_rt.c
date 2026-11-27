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
    group *groups;
    qibm *ibm;
    char where[200];
    struct qarm_rt *next;
};

struct qarm_job {
    qarm_rt *r;
    int k, shots, state, ncirc;          /* state: 1 done, 0 on the QPU, -1 failed */
    double *e, *err, t0;
    int *final;                          /* per circuit: the chain position measured into each bit */
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
static void circuit(const qarm_rt *r, const double *theta, const group *g, qcirc *c)
{
    c->qubits = r->n;
    for (int q = 0; q < r->n; q++)
        if (r->init >> q & 1) qc_add(c, G_X, q, 0, 0);
    for (int i = 0; i < r->nops; i++) {
        const qarm_op *o = &r->ops[i];
        double a = o->kind >= QARM_RY ? o->scale * theta[o->param] : 0;
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
    r->backend = cfg.backend;
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
            circuit(r, zero, &r->groups[i], &c);
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
              "native libqarm on %s: %d measurement bases%s", r->n, h->terms, r->nops, two_ansatz, r->params,
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
static void finish_local(qarm_job *j, const double *thetas)
{
    qarm_rt *r = j->r;
    long N = 1L << r->n;
    double *p = malloc(sizeof *p * (size_t)N), *q = malloc(sizeof *q * (size_t)N);
    char err[200];
    for (int i = 0; i < j->k; i++) {
        double e = r->identity, var = 0;
        for (int g = 0; g < r->ngroups; g++) {
            qcirc c = {0};
            circuit(r, thetas + (long)i * r->params, &r->groups[g], &c);
            if (qsv_probabilities(&c, p, err, sizeof err) < 0) { qarm_note("%s", err); j->state = -1; qc_free(&c); goto done; }
            qc_free(&c);
            const double *dist = p;
            if (r->backend == B_SIMULATOR) {                 /* shots sampled from p */
                for (long b = 0; b < N; b++) q[b] = 0;
                for (int s = 0; s < j->shots; s++) {
                    double u = uniform(), acc = 0;
                    long b = 0;
                    for (; b < N - 1; b++) { acc += p[b]; if (u < acc) break; }
                    q[b] += 1.0 / j->shots;
                }
                dist = q;
            }
            double m, v;
            moments(r, &r->groups[g], dist, &m, &v);
            e += m;
            var += v / j->shots;
        }
        j->e[i] = e;
        j->err[i] = r->backend == B_SIMULATOR ? sqrt(var) : 0;
    }
    j->state = 1;
done:
    free(p);
    free(q);
}

qarm_job *qarm_rt_submit(qarm_rt *r, const double *thetas, int k, int shots)
{
    if (!r || k < 1) { qarm_note("QEXPECTN: %s", r ? "k < 1" : "no problem loaded"); return NULL; }
    qarm_job *j = calloc(1, sizeof *j);
    j->r = r;
    j->k = k;
    j->shots = shots > 0 ? shots : cfg.shots;
    j->e = calloc((size_t)k, sizeof *j->e);
    j->err = calloc((size_t)k, sizeof *j->err);
    j->t0 = qarm_now();
    if (r->backend != B_IBM) { finish_local(j, thetas); return j; }

    int cal = r->mitigate ? 2 : 0;
    j->ncirc = k * r->ngroups + cal;
    char **qasm = calloc((size_t)j->ncirc, sizeof *qasm);
    j->final = calloc((size_t)j->ncirc * (size_t)r->n, sizeof *j->final);
    int t, ci = 0;
    for (int i = 0; i < k; i++)
        for (int g = 0; g < r->ngroups; g++, ci++) {
            qcirc c = {0};
            circuit(r, thetas + (long)i * r->params, &r->groups[g], &c);
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
    if (!ok) { qarm_note("QEXPECTN: the job was not accepted: %s", err); free(j->final); free(j->e); free(j->err); free(j); return NULL; }
    qarm_note("QEXPECTN: job %s sent to %s (%d parameter vectors x %d measurement bases%s); asynchronous", j->id,
              qibm_device(r->ibm), k, r->ngroups, cal ? " + 2 readout calibrations" : "");
    return j;
}

/* the energies from the samples, with readout correction from the two calibration circuits */
static int collect(qarm_job *j)
{
    qarm_rt *r = j->r;
    char err[400];
    uint64_t **samples = calloc((size_t)j->ncirc, sizeof *samples);
    int shots = qibm_results(r->ibm, j->id, j->ncirc, samples, err, sizeof err);
    if (shots <= 0) { qarm_note("QEXPECTN: job %s: %s", j->id, shots == 0 ? "no shots" : err); goto fail; }
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
    if (s < 0) { qarm_note("QEXPECTN: job %s: %s", j->id, err); j->state = -1; return -1; }
    if (s == 0) return 0;
    return collect(j);
}

int qarm_rt_wait(qarm_job *j, double *e, double *err)
{
    if (!j) return -1;
    int s, waited = 0;
    while ((s = qarm_rt_poll(j)) == 0) {
        if (!waited++) qarm_note("QEXPECTN: waiting for job %s on %s (it may be queued)", j->id, qibm_device(j->r->ibm));
        qarm_sleep(cfg.poll);
    }
    if (s > 0) {
        int m = 0;
        for (int i = 0; i < j->k; i++) {
            e[i] = j->e[i];
            if (err) err[i] = j->err[i];
            if (j->e[i] < j->e[m]) m = i;
        }
        char how[200];
        if (j->r->backend == B_IBM) snprintf(how, sizeof how, "job %s, %.1f s", j->id, qarm_now() - j->t0);
        else snprintf(how, sizeof how, "%s", j->r->where);
        if (j->k == 1 && j->err[0] > 0)
            qarm_note("QEXPECT: E = %.6f +- %.6f Ha (%s)", j->e[0], j->err[0], how);
        else if (j->k == 1)
            qarm_note("QEXPECT: E = %.6f Ha (%s)", j->e[0], how);
        else
            qarm_note("QEXPECTN: %d energies (%s); lowest %.6f Ha", j->k, how, j->e[m]);
    }
    free(j->final);
    free(j->e);
    free(j->err);
    free(j);
    return s > 0 ? 1 : -1;
}

int qarm_rt_expect_n(qarm_rt *r, const double *thetas, int k, int shots, double *e, double *err)
{
    double *tmp = err ? NULL : malloc(sizeof *tmp * (size_t)(k > 0 ? k : 1));
    int s = qarm_rt_wait(qarm_rt_submit(r, thetas, k, shots), e, err ? err : tmp);
    free(tmp);
    return s;
}
