/* SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 Quantag IT Solutions GmbH
 *
 * libqarm: IBM Quantum over its REST API (https://quantum.cloud.ibm.com/docs/api/qiskit-runtime-rest).
 *
 * The service runs only ISA circuits: the device's own gates on physically connected qubits. So the circuits are
 * transpiled here: a chain of n connected qubits with the lowest readout and CZ errors; a SWAP chain for a
 * two-qubit gate between qubits that are not neighbours; CX as H CZ H; the one-qubit gates between two CZs fused
 * into rz sx rz sx rz. Devices with the CZ gate set (Heron: Fez, Torino, Aachen, Marrakesh, Kingston).
 * Built with -DQARM_NO_CURL the ibm backend is absent. */
#include "qarm_internal.h"
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

#ifdef QARM_NO_CURL
struct qibm { int unused; };
qibm *qibm_open(const qj *opts, int qubits, char *err, size_t errlen)
{
    (void)opts; (void)qubits;
    snprintf(err, errlen, "libqarm was built without libcurl (QARM_NO_CURL): no ibm backend");
    return NULL;
}
void qibm_close(qibm *b) { (void)b; }
const char *qibm_device(const qibm *b) { (void)b; return "none"; }
char *qibm_qasm(qibm *b, const qcirc *c, int *final, int *twoq) { (void)b; (void)c; (void)final; *twoq = 0; return NULL; }
int qibm_submit(qibm *b, char **q, int n, int s, char *id, size_t il, char *e, size_t el) { (void)b; (void)q; (void)n; (void)s; (void)id; (void)il; snprintf(e, el, "no libcurl"); return 0; }
int qibm_status(qibm *b, const char *id, char *e, size_t el) { (void)b; (void)id; snprintf(e, el, "no libcurl"); return -1; }
int qibm_results(qibm *b, const char *id, int n, uint64_t **s, char *e, size_t el) { (void)b; (void)id; (void)n; (void)s; snprintf(e, el, "no libcurl"); return -1; }
int qibm_session(qibm *b, int open, char *e, size_t el) { (void)b; (void)open; snprintf(e, el, "no libcurl"); return 0; }
#else
typedef struct { double r, i; } cx;
static cx cmul(cx a, cx b) { return (cx){a.r * b.r - a.i * b.i, a.r * b.i + a.i * b.r}; }
static double cabs_(cx a) { return hypot(a.r, a.i); }
static double carg_(cx a) { return atan2(a.i, a.r); }
typedef struct { cx m[4]; } mat;                          /* [[m0, m1], [m2, m3]] */

static mat mmul(mat a, mat b)
{
    mat r;
    for (int i = 0; i < 2; i++)
        for (int j = 0; j < 2; j++) {
            cx s = cmul(a.m[2 * i], b.m[j]), t = cmul(a.m[2 * i + 1], b.m[2 + j]);
            r.m[2 * i + j] = (cx){s.r + t.r, s.i + t.i};
        }
    return r;
}

static mat gate_matrix(int kind, double a)
{
    const double h = 0.70710678118654752440;
    double c = cos(a / 2), s = sin(a / 2);
    switch (kind) {
    case G_X: return (mat){{{0, 0}, {1, 0}, {1, 0}, {0, 0}}};
    case G_H: return (mat){{{h, 0}, {h, 0}, {h, 0}, {-h, 0}}};
    case G_S: return (mat){{{1, 0}, {0, 0}, {0, 0}, {0, 1}}};
    case G_SDG: return (mat){{{1, 0}, {0, 0}, {0, 0}, {0, -1}}};
    case G_SX: return (mat){{{.5, .5}, {.5, -.5}, {.5, -.5}, {.5, .5}}};
    case G_RY: return (mat){{{c, 0}, {-s, 0}, {s, 0}, {c, 0}}};
    default: return (mat){{{c, -s}, {0, 0}, {0, 0}, {c, s}}};    /* G_RZ */
    }
}

static double wrap(double a)
{
    a = fmod(a, 2 * M_PI);
    if (a > M_PI) a -= 2 * M_PI;
    if (a <= -M_PI) a += 2 * M_PI;
    return a;
}

/* U = U3(theta, phi, lambda) up to phase = rz(lambda) sx rz(theta + pi) sx rz(phi + pi) in time order */
static void emit_1q(qbuf *o, mat u, int phys)
{
    double a00 = cabs_(u.m[0]), a10 = cabs_(u.m[2]);
    double theta = 2 * atan2(a10, a00), phi, lam;
    if (a10 < 1e-10) {                                        /* diagonal: one rz */
        double z = wrap(carg_(u.m[3]) - carg_(u.m[0]));
        if (fabs(z) > 1e-10) qb_printf(o, "rz(%.17g) $%d;\n", z, phys);
        return;
    }
    if (a00 < 1e-10) { lam = 0; phi = carg_(u.m[2]) - carg_((cx){-u.m[1].r, -u.m[1].i}); }
    else {
        double g = carg_(u.m[0]);
        phi = carg_(u.m[2]) - g;
        lam = carg_((cx){-u.m[1].r, -u.m[1].i}) - g;
    }
    double r1 = wrap(lam), r2 = wrap(theta + M_PI), r3 = wrap(phi + M_PI);
    if (fabs(r1) > 1e-10) qb_printf(o, "rz(%.17g) $%d;\n", r1, phys);
    qb_printf(o, "sx $%d;\n", phys);
    if (fabs(r2) > 1e-10) qb_printf(o, "rz(%.17g) $%d;\n", r2, phys);
    qb_printf(o, "sx $%d;\n", phys);
    if (fabs(r3) > 1e-10) qb_printf(o, "rz(%.17g) $%d;\n", r3, phys);
}


#include <curl/curl.h>

#define API_VERSION "2026-04-15"

struct qibm {
    char apikey[1024], iam[512], crn[512], base[512], dev[128], session[128], token[8192];
    double expiry;
    int n, *chain, nq;
};

static size_t on_data(char *p, size_t size, size_t n, void *ud)
{
    qb_addn((qbuf *)ud, p, size * n);
    return size * n;
}

/* one request; returns the HTTP status, or -1 (err) */
static long http(const char *method, const char *url, const char *body, const char *type, const char *auth,
                 const char *crn, qbuf *resp, char *err, size_t errlen)
{
    static int inited;
    if (!inited) { curl_global_init(CURL_GLOBAL_DEFAULT); inited = 1; }
    CURL *h = curl_easy_init();
    struct curl_slist *hd = NULL;
    char line[9000];
    hd = curl_slist_append(hd, "Accept: application/json");
    if (type) { snprintf(line, sizeof line, "Content-Type: %s", type); hd = curl_slist_append(hd, line); }
    if (auth) { snprintf(line, sizeof line, "Authorization: Bearer %s", auth); hd = curl_slist_append(hd, line); }
    if (crn) {
        snprintf(line, sizeof line, "Service-CRN: %s", crn); hd = curl_slist_append(hd, line);
        hd = curl_slist_append(hd, "IBM-API-Version: " API_VERSION);
    }
    resp->n = 0;
    if (resp->s) resp->s[0] = 0;
    curl_easy_setopt(h, CURLOPT_URL, url);
    curl_easy_setopt(h, CURLOPT_CUSTOMREQUEST, method);
    if (body) curl_easy_setopt(h, CURLOPT_POSTFIELDS, body);
    curl_easy_setopt(h, CURLOPT_HTTPHEADER, hd);
    curl_easy_setopt(h, CURLOPT_WRITEFUNCTION, on_data);
    curl_easy_setopt(h, CURLOPT_WRITEDATA, resp);
    curl_easy_setopt(h, CURLOPT_TIMEOUT, 120L);
    curl_easy_setopt(h, CURLOPT_USERAGENT, "libqarm/1");
    CURLcode rc = curl_easy_perform(h);
    long status = -1;
    if (rc != CURLE_OK) snprintf(err, errlen, "%s %s: %s", method, url, curl_easy_strerror(rc));
    else curl_easy_getinfo(h, CURLINFO_RESPONSE_CODE, &status);
    curl_slist_free_all(hd);
    curl_easy_cleanup(h);
    if (!resp->s) qb_add(resp, "");
    return status;
}

/* an error message from a failed response */
static void api_error(const char *what, long status, const qbuf *resp, char *err, size_t errlen)
{
    char e[300];
    qj *v = qj_parse(resp->s, e, sizeof e);
    const char *msg = qj_str(qj_path(v, "errors.0.message"), NULL);
    if (!msg) msg = qj_str(qj_get(v, "message"), NULL);
    if (!msg) msg = qj_str(qj_get(v, "errorMessage"), NULL);
    snprintf(err, errlen, "%s: HTTP %ld%s%.300s", what, status, msg || resp->n ? ": " : "", msg ? msg : resp->s);
    qj_free(v);
}

static int authenticate(qibm *b, char *err, size_t errlen)
{
    if (b->token[0] && qarm_now() < b->expiry - 120) return 1;
    CURL *h = curl_easy_init();
    char *key = curl_easy_escape(h, b->apikey, 0);
    qbuf body = {0}, resp = {0};
    qb_printf(&body, "grant_type=urn%%3Aibm%%3Aparams%%3Aoauth%%3Agrant-type%%3Aapikey&apikey=%s", key);
    curl_free(key);
    curl_easy_cleanup(h);
    char url[600];
    snprintf(url, sizeof url, "%s/identity/token", b->iam);
    long st = http("POST", url, body.s, "application/x-www-form-urlencoded", NULL, NULL, &resp, err, errlen);
    qb_free(&body);
    int ok = 0;
    if (st == 200) {
        char e[200];
        qj *v = qj_parse(resp.s, e, sizeof e);
        const char *tok = qj_str(qj_get(v, "access_token"), NULL);
        if (tok && strlen(tok) < sizeof b->token) {
            snprintf(b->token, sizeof b->token, "%s", tok);
            b->expiry = qarm_now() + qj_num(qj_get(v, "expires_in"), 3600);
            ok = 1;
        } else
            snprintf(err, errlen, "IBM Cloud IAM: no access token in the answer");
        qj_free(v);
    } else if (st > 0)
        api_error("IBM Cloud IAM (is ibm_token in config.json an IBM Quantum API key?)", st, &resp, err, errlen);
    qb_free(&resp);
    return ok;
}

/* a GET or POST to the Qiskit Runtime API; the parsed answer, or NULL (err) */
static qj *api(qibm *b, const char *method, const char *path, const char *body, char *err, size_t errlen)
{
    if (!authenticate(b, err, errlen)) return NULL;
    char url[1024];
    snprintf(url, sizeof url, "%s%s", b->base, path);
    qbuf resp = {0};
    long st = http(method, url, body, body ? "application/json" : NULL, b->token, b->crn, &resp, err, errlen);
    qj *v = NULL;
    if (st >= 200 && st < 300) {
        char e[200];
        v = qj_parse(resp.n ? resp.s : "{}", e, sizeof e);
        if (!v) snprintf(err, errlen, "%s %s: the answer is not JSON (%s)", method, path, e);
    } else if (st > 0) {
        char what[300];
        snprintf(what, sizeof what, "%s %s", method, path);
        api_error(what, st, &resp, err, errlen);
    }
    qb_free(&resp);
    return v;
}

static int find_crn(qibm *b, const char *instance, const char *search, char *err, size_t errlen)
{
    if (!strncmp(instance, "crn:", 4)) { snprintf(b->crn, sizeof b->crn, "%s", instance); return 1; }
    if (!authenticate(b, err, errlen)) return 0;
    char url[600], cursor[2048] = "";
    snprintf(url, sizeof url, "%s/v3/resources/search?limit=100", search);
    for (int page = 0; page < 20; page++) {
        qbuf body = {0}, resp = {0};
        qb_add(&body, "{\"query\": \"service_name:quantum-computing\", \"fields\": [\"crn\", \"name\"]");
        if (cursor[0]) { qb_add(&body, ", \"search_cursor\": "); qb_json_str(&body, cursor); }
        qb_add(&body, "}");
        long st = http("POST", url, body.s, "application/json", b->token, NULL, &resp, err, errlen);
        qb_free(&body);
        if (st != 200) {
            if (st > 0) api_error("IBM Cloud Global Search (the instance's CRN)", st, &resp, err, errlen);
            qb_free(&resp);
            return 0;
        }
        char e[200];
        qj *v = qj_parse(resp.s, e, sizeof e);
        qb_free(&resp);
        const qj *items = qj_get(v, "items");
        for (int i = 0; i < qj_len(items); i++) {
            const qj *it = qj_at(items, i);
            if (!strcmp(qj_str(qj_get(it, "name"), ""), instance)) {
                snprintf(b->crn, sizeof b->crn, "%s", qj_str(qj_get(it, "crn"), ""));
                qj_free(v);
                return 1;
            }
        }
        const char *next = qj_str(qj_get(v, "search_cursor"), NULL);
        int more = next && qj_len(items) == 100;
        if (more) snprintf(cursor, sizeof cursor, "%s", next);
        qj_free(v);
        if (!more) break;
    }
    snprintf(err, errlen, "no IBM Quantum instance named \"%s\" in this account (use its CRN in \"instance\")", instance);
    return 0;
}

/* the chain of n connected qubits with the lowest sum of readout and CZ errors */
typedef struct { int nq, *deg, (*adj)[8]; double *ro, *edge; long visits; double best; int *path, *bestp; } search;
#define VISITS_PER_START 20000

static void dfs(search *s, int depth, int n, double score, char *used)
{
    if (score >= s->best || ++s->visits > VISITS_PER_START) return;
    if (depth == n) { s->best = score; memcpy(s->bestp, s->path, sizeof(int) * (size_t)n); return; }
    int last = s->path[depth - 1];
    for (int i = 0; i < s->deg[last]; i++) {
        int q = s->adj[last][i];
        if (used[q]) continue;
        used[q] = 1;
        s->path[depth] = q;
        dfs(s, depth + 1, n, score + s->ro[q] + s->edge[last * 8 + i], used);
        used[q] = 0;
    }
}

static int choose_chain(qibm *b, const qj *conf, const qj *props, char *err, size_t errlen)
{
    search s = {0};
    s.nq = b->nq;
    s.deg = calloc((size_t)s.nq, sizeof *s.deg);
    s.adj = calloc((size_t)s.nq, sizeof *s.adj);
    s.ro = calloc((size_t)s.nq, sizeof *s.ro);
    s.edge = calloc((size_t)s.nq * 8, sizeof *s.edge);
    const qj *cm = qj_get(conf, "coupling_map");
    for (int i = 0; i < qj_len(cm); i++) {
        int a = (int)qj_num(qj_at(qj_at(cm, i), 0), -1), c = (int)qj_num(qj_at(qj_at(cm, i), 1), -1), dup = 0;
        if (a < 0 || c < 0 || a >= s.nq || c >= s.nq) continue;
        for (int k = 0; k < s.deg[a]; k++) dup |= s.adj[a][k] == c;
        if (!dup && s.deg[a] < 8) { s.edge[a * 8 + s.deg[a]] = 0.01; s.adj[a][s.deg[a]++] = c; }
        dup = 0;
        for (int k = 0; k < s.deg[c]; k++) dup |= s.adj[c][k] == a;
        if (!dup && s.deg[c] < 8) { s.edge[c * 8 + s.deg[c]] = 0.01; s.adj[c][s.deg[c]++] = a; }
    }
    for (int q = 0; q < s.nq; q++) {
        s.ro[q] = 0.05;
        const qj *pq = qj_at(qj_get(props, "qubits"), q);
        for (int k = 0; k < qj_len(pq); k++)
            if (!strcmp(qj_str(qj_get(qj_at(pq, k), "name"), ""), "readout_error"))
                s.ro[q] = qj_num(qj_get(qj_at(pq, k), "value"), 0.05);
    }
    const qj *gates = qj_get(props, "gates");
    for (int i = 0; i < qj_len(gates); i++) {
        const qj *g = qj_at(gates, i);
        if (strcmp(qj_str(qj_get(g, "gate"), ""), "cz")) continue;
        const qj *qs = qj_get(g, "qubits"), *ps = qj_get(g, "parameters");
        int a = (int)qj_num(qj_at(qs, 0), -1), c = (int)qj_num(qj_at(qs, 1), -1);
        double e = 0.01;
        for (int k = 0; k < qj_len(ps); k++)
            if (!strcmp(qj_str(qj_get(qj_at(ps, k), "name"), ""), "gate_error")) e = qj_num(qj_get(qj_at(ps, k), "value"), 0.01);
        for (int pass = 0; pass < 2 && a >= 0 && c >= 0 && a < s.nq && c < s.nq; pass++) {
            for (int k = 0; k < s.deg[a]; k++)
                if (s.adj[a][k] == c) s.edge[a * 8 + k] = e;
            int t = a; a = c; c = t;
        }
    }
    s.best = 1e300;
    s.path = calloc((size_t)b->n, sizeof *s.path);
    s.bestp = calloc((size_t)b->n, sizeof *s.bestp);
    char *used = calloc((size_t)s.nq, 1);
    for (int q = 0; q < s.nq; q++) {
        if (s.ro[q] >= 0.5) continue;
        s.path[0] = q;
        used[q] = 1;
        if (b->n == 1) { if (s.ro[q] < s.best) { s.best = s.ro[q]; s.bestp[0] = q; } }
        else { s.visits = 0; dfs(&s, 1, b->n, s.ro[q], used); }
        used[q] = 0;
    }
    int ok = s.best < 1e299;
    if (ok) { b->chain = s.bestp; s.bestp = NULL; }
    else snprintf(err, errlen, "%s has no chain of %d connected qubits", b->dev, b->n);
    free(used); free(s.path); free(s.bestp); free(s.deg); free(s.adj); free(s.ro); free(s.edge);
    return ok;
}

static const char *opt(const qj *o, const char *a, const char *b2, const char *dflt)
{
    const char *v = qj_str(qj_get(o, a), NULL);
    if (!v && b2) v = qj_str(qj_get(o, b2), NULL);
    return v ? v : dflt;
}

qibm *qibm_open(const qj *opts, int qubits, char *err, size_t errlen)
{
    qibm *b = calloc(1, sizeof *b);
    b->n = qubits;
    const char *key = opt(opts, "ibm_token", "token", "");
    if (!strncmp(key, "env:", 4)) key = getenv(key + 4) ? getenv(key + 4) : "";
    const char *instance = opt(opts, "instance", NULL, ""), *dev = opt(opts, "device", "backend", "");
    if (!*key) { snprintf(err, errlen, "no ibm_token in config.json's options"); goto fail; }
    if (!*instance) { snprintf(err, errlen, "no instance (a CRN or a name) in config.json's options"); goto fail; }
    if (!*dev || !strcmp(dev, "least_busy")) { snprintf(err, errlen, "name the device in config.json's options (\"device\": \"ibm_aachen\"); libqarm does not pick one"); goto fail; }
    snprintf(b->apikey, sizeof b->apikey, "%s", key);
    snprintf(b->dev, sizeof b->dev, "%s", dev);
    snprintf(b->iam, sizeof b->iam, "%s", opt(opts, "iam_url", NULL, "https://iam.cloud.ibm.com"));
    if (!find_crn(b, instance, opt(opts, "search_url", NULL, "https://api.global-search-tagging.cloud.ibm.com"), err, errlen))
        goto fail;
    const char *url = opt(opts, "url", NULL, NULL);
    if (url) snprintf(b->base, sizeof b->base, "%s", url);
    else {                                                     /* the region: the 6th field of the CRN */
        char region[64] = "us-east";
        const char *p = b->crn;
        for (int f = 0; f < 5 && p; f++) p = strchr(p, ':') ? strchr(p, ':') + 1 : NULL;
        if (p) { size_t n = strcspn(p, ":"); if (n && n < sizeof region) { memcpy(region, p, n); region[n] = 0; } }
        if (!strcmp(region, "us-east")) snprintf(b->base, sizeof b->base, "https://quantum.cloud.ibm.com/api/v1");
        else snprintf(b->base, sizeof b->base, "https://%s.quantum.cloud.ibm.com/api/v1", region);
    }
    char path[300];
    snprintf(path, sizeof path, "/backends/%s/configuration", b->dev);
    qj *conf = api(b, "GET", path, NULL, err, errlen);
    if (!conf) goto fail;
    b->nq = (int)qj_num(qj_get(conf, "n_qubits"), 0);
    const qj *basis = qj_get(conf, "basis_gates");
    int have = 0;
    for (int i = 0; i < qj_len(basis); i++) {
        const char *g = qj_str(qj_at(basis, i), "");
        have |= (!strcmp(g, "cz")) << 0 | (!strcmp(g, "rz")) << 1 | (!strcmp(g, "sx")) << 2;
    }
    if (have != 7) { snprintf(err, errlen, "%s: libqarm needs the gates cz, rz and sx (a Heron device)", b->dev); qj_free(conf); goto fail; }
    if (b->nq < qubits) { snprintf(err, errlen, "%d qubits, %s has %d", qubits, b->dev, b->nq); qj_free(conf); goto fail; }
    snprintf(path, sizeof path, "/backends/%s/properties", b->dev);
    char perr[300];
    qj *props = api(b, "GET", path, NULL, perr, sizeof perr);     /* without them every qubit counts the same */
    int ok = choose_chain(b, conf, props, err, errlen);
    qj_free(conf);
    qj_free(props);
    if (!ok) goto fail;
    return b;
fail:
    qibm_close(b);
    return NULL;
}

void qibm_close(qibm *b)
{
    if (!b) return;
    free(b->chain);
    free(b);
}

const char *qibm_device(const qibm *b) { return b->dev; }

char *qibm_qasm(qibm *b, const qcirc *c, int *final, int *twoq)
{
    int n = c->qubits, pos[64], occ[64];
    for (int q = 0; q < n; q++) pos[q] = occ[q] = q;
    qcirc r = {0};                                             /* on chain positions, CX and CZ only */
    r.qubits = n;
    for (int i = 0; i < c->n; i++) {
        const qgate *g = &c->g[i];
        if (g->kind != G_CX && g->kind != G_CZ) { qc_add(&r, g->kind, pos[g->q0], 0, g->a); continue; }
        int pa = pos[g->q0], pb = pos[g->q1];
        while (abs(pa - pb) > 1) {                             /* SWAP the control along the chain */
            int nx = pa + (pb > pa ? 1 : -1);
            qc_add(&r, G_CX, pa, nx, 0); qc_add(&r, G_CX, nx, pa, 0); qc_add(&r, G_CX, pa, nx, 0);
            int la = occ[pa], lb = occ[nx];
            occ[pa] = lb; occ[nx] = la; pos[la] = nx; pos[lb] = pa;
            pa = nx;
        }
        qc_add(&r, g->kind, pa, pb, 0);
    }
    for (int q = 0; q < n; q++) final[q] = pos[q];
    qbuf o = {0};
    qb_printf(&o, "OPENQASM 3.0;\ninclude \"stdgates.inc\";\nbit[%d] c;\n", n);
    mat m[64];
    int pending[64] = {0};
    const mat id = {{{1, 0}, {0, 0}, {0, 0}, {1, 0}}};
    for (int q = 0; q < n; q++) m[q] = id;
    *twoq = 0;
#define APPLY(p, k, a) (m[p] = mmul(gate_matrix(k, a), m[p]), pending[p] = 1)
#define FLUSH(p) do { if (pending[p]) emit_1q(&o, m[p], b->chain[p]); m[p] = id; pending[p] = 0; } while (0)
    for (int i = 0; i < r.n; i++) {
        const qgate *g = &r.g[i];
        if (g->kind == G_CX || g->kind == G_CZ) {
            if (g->kind == G_CX) APPLY(g->q1, G_H, 0);
            FLUSH(g->q0);
            FLUSH(g->q1);
            qb_printf(&o, "cz $%d, $%d;\n", b->chain[g->q0], b->chain[g->q1]);
            ++*twoq;
            if (g->kind == G_CX) APPLY(g->q1, G_H, 0);
        } else
            APPLY(g->q0, g->kind, g->a);
    }
    for (int p = 0; p < n; p++) FLUSH(p);
    for (int q = 0; q < n; q++) qb_printf(&o, "c[%d] = measure $%d;\n", q, b->chain[final[q]]);
#undef APPLY
#undef FLUSH
    qc_free(&r);
    return o.s;
}

int qibm_submit(qibm *b, char **qasm, int count, int shots, char *job_id, size_t idlen, char *err, size_t errlen)
{
    qbuf body = {0};
    qb_add(&body, "{\"program_id\": \"sampler\", \"backend\": ");
    qb_json_str(&body, b->dev);
    if (b->session[0]) { qb_add(&body, ", \"session_id\": "); qb_json_str(&body, b->session); }
    qb_add(&body, ", \"params\": {\"version\": 2, \"options\": {}, \"pubs\": [");
    for (int i = 0; i < count; i++) {
        qb_add(&body, i ? ", [" : "[");
        qb_json_str(&body, qasm[i]);
        qb_printf(&body, ", null, %d]", shots);
    }
    qb_add(&body, "]}}");
    qj *v = api(b, "POST", "/jobs", body.s, err, errlen);
    qb_free(&body);
    const char *id = qj_str(qj_get(v, "id"), NULL);
    if (v && !id) snprintf(err, errlen, "POST /jobs: no job id in the answer");
    if (id) snprintf(job_id, idlen, "%s", id);
    qj_free(v);
    return id != NULL;
}

int qibm_status(qibm *b, const char *job_id, char *err, size_t errlen)
{
    char path[300];
    snprintf(path, sizeof path, "/jobs/%s", job_id);
    qj *v = api(b, "GET", path, NULL, err, errlen);
    if (!v) return -1;
    const qj *st = qj_get(v, "state");
    const char *s = qj_str(qj_get(st, "status"), qj_str(qj_get(v, "status"), ""));
    int r = 0;
    if (!strcmp(s, "Completed") || !strcmp(s, "COMPLETED")) r = 1;
    else if (!strcmp(s, "Failed") || !strcmp(s, "Cancelled") || !strcmp(s, "FAILED") || !strcmp(s, "CANCELLED")) {
        snprintf(err, errlen, "%s: %s", s, qj_str(qj_get(st, "reason"), "no reason given"));
        r = -1;
    }
    qj_free(v);
    return r;
}

int qibm_results(qibm *b, const char *job_id, int count, uint64_t **samples, char *err, size_t errlen)
{
    char path[300];
    snprintf(path, sizeof path, "/jobs/%s/results", job_id);
    qj *v = api(b, "GET", path, NULL, err, errlen);
    if (!v) return -1;
    const qj *res = qj_get(v, "results");
    int shots = -1;
    if (qj_len(res) != count) snprintf(err, errlen, "%d results for %d circuits", qj_len(res), count);
    else
        for (int i = 0; i < count; i++) {
            const qj *smp = qj_path(qj_at(res, i), "data.c.samples");
            int ns = qj_len(smp);
            if (!smp || (shots >= 0 && ns != shots)) {
                snprintf(err, errlen, "result %d: no data.c.samples", i);
                shots = -1;
                break;
            }
            shots = ns;
            samples[i] = malloc(sizeof **samples * (size_t)(ns ? ns : 1));
            for (int s = 0; s < ns; s++) samples[i][s] = strtoull(qj_str(qj_at(smp, s), "0"), NULL, 16);
        }
    qj_free(v);
    return shots;
}

int qibm_session(qibm *b, int open, char *err, size_t errlen)
{
    if (!open) {
        if (!b->session[0]) { snprintf(err, errlen, "no session open"); return 1; }
        char path[300], e[300];
        snprintf(path, sizeof path, "/sessions/%s", b->session);
        qj_free(api(b, "PATCH", path, "{\"accepting_jobs\": false}", e, sizeof e));
        snprintf(err, errlen, "session %s closed", b->session);
        b->session[0] = 0;
        return 1;
    }
    if (b->session[0]) { snprintf(err, errlen, "session %s already open", b->session); return 1; }
    qbuf body = {0};
    qb_add(&body, "{\"mode\": \"dedicated\", \"backend\": ");
    qb_json_str(&body, b->dev);
    qb_add(&body, "}");
    char e[400];
    qj *v = api(b, "POST", "/sessions", body.s, e, sizeof e);
    qb_free(&body);
    const char *id = qj_str(qj_get(v, "id"), NULL);
    if (id) {
        snprintf(b->session, sizeof b->session, "%s", id);
        snprintf(err, errlen, "session %s on %s", id, b->dev);
    } else
        snprintf(err, errlen, "no session (%s); each job queues on its own", v ? "no id in the answer" : e);
    qj_free(v);
    return id != NULL;
}
#endif
