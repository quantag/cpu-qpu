/* SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 Quantag IT Solutions GmbH
 *
 * Q-ARM for QEMU: a TCG plugin that executes Q-ARM packets (docs/qarm_v2.md, section 8) in an unmodified arm64
 * Linux binary run by qemu-aarch64, with libqarm as the coprocessor:
 *
 *   qemu-aarch64 -plugin ./libqarm-qemu.so[,config=config.json][,backend=simulator][,quiet=on] ./program
 *
 * A packet is B .+32 followed by the word "QARM" (0x4d524151) and its opcode. The plugin instruments each B .+32
 * it translates; when one runs and the next word is the magic, the plugin reads the registers and the program's
 * tables from guest memory, calls libqarm and writes the results back. Then the branch skips the packet, as it
 * does on a plain CPU. Needs QEMU 10.1 or newer (plugin API with qemu_plugin_write_register).
 *
 *   QAVAIL    W0 = 1, W1 = QARM_F_VQE | QARM_F_CIRC | QARM_F_RAND | QARM_F_CAPS, with QARM_F_SEARCH | QARM_F_MIN | QARM_F_COUNT when the oracle helper is there
 *   QMIN      X1 = cost, W2 = bits: a compute oracle of cost, Durr-Hoyer in libqarm: W0 = 1, W3 = x, W4 = cost(x)
 *   QCOUNT    X1 = f, W2 = bits: the phase oracle, quantum counting: W0 = 1, X3 = the count, D0 = its error bound
 *   QAMPEST   X1 = g, W2 = bits, W3 = value bits k: a compute oracle, amplitude estimation: W0 = 1, D0 = the mean of
 *             g, D1 = its error bound
 *   QCOLLISION X1 = f, W2 = bits: a compute oracle, Brassard-Hoyer-Tapp in libqarm: W0 = 1, W3 = x, W4 = y with
 *             f(x) = f(y), or W0 = 2: none found
 *   QPERIOD, QSIMON (compute oracles), QBV (phase oracle): X1 = f, W2 = bits: W0 = 1, W3 = r or s; W0 = 2: none
 *   QCIRC     X1 = a qarm_circuit, W2 = shots, X3 = the samples: the gate table read from guest memory and run by
 *             libqarm (state vector, or a real device on ibm): W0 = 1, or 2: refused
 *   QRAND     X1 = a buffer, W2 = bytes: W0 = 1 (ibm's measurements) or 3 (pseudo-random, libqarm's generator)
 *   QCAPS     X1 = a qarm_capabilities: libqarm's backend, with this plugin's feature bits
 *   QSUBMIT, QPOLL, QWAIT   X1 = a qarm_request. QEXPECTN is submitted to libqarm and runs while the guest does
 *             (a QPU job on ibm); every other instruction runs at QSUBMIT, on the request's registers
 *   QVLOAD, QEXPECT, QEXPECTN, QGRAD, QVSAMPLE, QSESSION, QVFREE    on libqarm's backend (exact, simulator, ibm)
 *   QSEARCH   X1 = f, W2 = bits: the phase oracle of f from the guest program's own machine code, built once per
 *             function by tools/qarm_oracle.py (qoracle; plugin option helper=, python=), then Grover's search in
 *             libqarm (qarm_search.c): W0 = 1 and W3 = x, or W0 = 2 for none. If no oracle can be built, W0 stays 0
 *             and the program takes its classical path. qarm.h checks every answer by calling f itself. */
#include <errno.h>
#include <fcntl.h>
#include <glib/gstdio.h>
#include <glib.h>
#include <spawn.h>
#include <sys/wait.h>
#include <unistd.h>
#include <stdarg.h>
#include <qemu-plugin.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#ifndef QARM_RUNTIME
#define QARM_RUNTIME
#endif
#include "qarm.h"

QEMU_PLUGIN_EXPORT int qemu_plugin_version = QEMU_PLUGIN_VERSION;

#define B_32 0x14000008u
#define MAGIC 0x4d524151u
enum { OP_QAVAIL = 0x10, OP_QSEARCH = 0x11, OP_QMIN = 0x15, OP_QCOUNT = 0x16, OP_QAMPEST = 0x17, OP_QPERIOD = 0x18, OP_QSIMON = 0x19,
       OP_QBV = 0x1A, OP_QCOLLISION = 0x1B, OP_QSUBMIT = 0x12, OP_QPOLL = 0x13, OP_QWAIT = 0x14, OP_QRAND = 0x1C, OP_QCIRC = 0x30, OP_QCAPS = 0x31, OP_QVLOAD = 0x20, OP_QEXPECT = 0x21, OP_QEXPECTN = 0x22,
       OP_QGRAD = 0x23, OP_QSESSION = 0x24, OP_QVSAMPLE = 0x25, OP_QVFREE = 0x26 };

static struct qemu_plugin_register *reg_x[31], *reg_v[2];
static GMutex lock;
static int quiet, told_search;
static char *helper, *python;
static int have_search;
static GHashTable *oracles;            /* function address -> qarm_oracle *, or a failure marker */
#define ORACLE_FAILED ((qarm_oracle *)1)
#ifndef QARM_TOOLS_DIR
#define QARM_TOOLS_DIR "tools"
#endif

typedef struct {                       /* a problem loaded by QVLOAD: host copies of the guest's tables */
    qarm_hamiltonian h;
    qarm_ansatz a;
    qarm_pauli *terms;
    qarm_op *ops;
    qarm_rt *rt;
} problem;
static GPtrArray *problems;            /* handle h is problems[h - 1] */

static void note(const char *fmt, ...)
{
    if (quiet) return;
    va_list ap;
    va_start(ap, fmt);
    fputs("[qarm-qemu: ", stderr);
    vfprintf(stderr, fmt, ap);
    fputs("]\n", stderr);
    va_end(ap);
}

/* ---------------------------------------------------------------- the guest's registers and memory */
static struct { int on; uint64_t x[6]; double d[2]; } vr;      /* QSUBMIT: the request's registers X0-X5, D0, D1 */

static uint64_t x(int n)
{
    if (vr.on && n < 6) return vr.x[n];
    g_autoptr(GByteArray) b = g_byte_array_new();
    uint64_t v = 0;
    if (qemu_plugin_read_register(reg_x[n], b) >= 8) memcpy(&v, b->data, 8);
    return v;
}

static void set_x(int n, uint64_t v)
{
    if (vr.on && n < 6) { vr.x[n] = v; return; }
    g_autoptr(GByteArray) b = g_byte_array_new();
    g_byte_array_append(b, (const guint8 *)&v, 8);
    qemu_plugin_write_register(reg_x[n], b);
}

static void set_d(int n, double d)     /* the low 8 bytes of v<n> (z<n> with SVE); the rest cleared, as FMOV d<n> does */
{
    if (vr.on) { vr.d[n] = d; return; }
    g_autoptr(GByteArray) b = g_byte_array_new();
    int size = qemu_plugin_read_register(reg_v[n], b);
    if (size < 8) return;
    memset(b->data, 0, (size_t)size);
    memcpy(b->data, &d, 8);
    qemu_plugin_write_register(reg_v[n], b);
}

static int rd(uint64_t addr, void *out, size_t n)
{
    if (!n) return 1;
    g_autoptr(GByteArray) b = g_byte_array_new();
    if (!qemu_plugin_read_memory_vaddr(addr, b, n) || b->len < n) return 0;
    memcpy(out, b->data, n);
    return 1;
}

static int wr(uint64_t addr, const void *data, size_t n)
{
    g_autoptr(GByteArray) b = g_byte_array_new();
    g_byte_array_append(b, data, (guint)n);
    return qemu_plugin_write_memory_vaddr(addr, b);
}

/* ---------------------------------------------------------------- the instructions */
static void problem_free(gpointer p)
{
    problem *pr = p;
    if (!pr) return;
    qarm_rt_free(pr->rt);
    g_free(pr->terms);
    g_free(pr->ops);
    g_free(pr);
}

/* the guest's qarm_hamiltonian and qarm_ansatz (LP64 layouts of include/qarm.h) */
static int qvload(uint64_t ha, uint64_t aa)
{
    struct { uint32_t qubits, terms; double constant; uint64_t t; } gh;
    struct { uint32_t qubits, params, ops, reserved; uint64_t init, op; } ga;
    _Static_assert(sizeof gh == 24 && sizeof ga == 32 && sizeof(qarm_pauli) == 24 && sizeof(qarm_op) == 32,
                   "the guest and host table layouts differ");
    if (!rd(ha, &gh, sizeof gh) || !rd(aa, &ga, sizeof ga)) { note("QVLOAD: the tables are not readable"); return 2; }
    if (gh.terms > 100000 || ga.ops > 100000) { note("QVLOAD: %u terms, %u operations: too many", gh.terms, ga.ops); return 2; }
    problem *p = g_new0(problem, 1);
    p->terms = g_new0(qarm_pauli, gh.terms ? gh.terms : 1);
    p->ops = g_new0(qarm_op, ga.ops ? ga.ops : 1);
    if (!rd(gh.t, p->terms, sizeof(qarm_pauli) * gh.terms) || !rd(ga.op, p->ops, sizeof(qarm_op) * ga.ops)) {
        note("QVLOAD: the terms or operations are not readable");
        problem_free(p);
        return 2;
    }
    p->h = (qarm_hamiltonian){gh.qubits, gh.terms, gh.constant, p->terms};
    p->a = (qarm_ansatz){ga.qubits, ga.params, ga.ops, 0, ga.init, p->ops};
    if (qarm_rt_load(&p->h, &p->a, &p->rt) < 0) { problem_free(p); return 2; }   /* libqarm gave the reason */
    g_ptr_array_add(problems, p);
    set_x(3, problems->len);
    return 1;
}

static problem *find(uint64_t handle)
{
    if (handle < 1 || handle > problems->len) { note("no problem loaded with handle %llu", (unsigned long long)handle); return NULL; }
    return g_ptr_array_index(problems, handle - 1);
}

static int qexpect(int many)
{
    problem *p = find(x(3));
    if (!p) return 2;
    uint64_t k = many ? (uint32_t)x(2) : 1, P = p->a.params;
    if (k < 1 || k > 4096) { note("QEXPECTN: k = %llu: 1 to 4096", (unsigned long long)k); return 2; }
    double *th = g_new0(double, k * P + 1), *e = g_new0(double, k), *err = g_new0(double, k);
    int status = 2;
    if (!rd(x(1), th, sizeof(double) * k * P)) note("QEXPECT: the parameters are not readable");
    else if (qarm_rt_expect_n(p->rt, th, (int)k, many ? 0 : (int)(uint32_t)x(2), e, err) > 0) {
        if (many) status = wr(x(4), e, sizeof(double) * k) ? 1 : (note("QEXPECTN: the output is not writable"), 2);
        else { set_d(0, e[0]); set_d(1, err[0]); status = 1; }
    }
    g_free(th);
    g_free(e);
    g_free(err);
    return status;
}

static int qgrad(void)
{
    problem *p = find(x(3));
    if (!p) return 2;
    uint64_t P = p->a.params;
    double *th = g_new0(double, P + 1), *g = g_new0(double, P + 1), e = 0;
    int status = 2;
    if (!rd(x(1), th, sizeof(double) * P)) note("QGRAD: the parameters are not readable");
    else if (qarm_rt_grad(p->rt, th, 0, &e, g) > 0) {
        if (wr(x(4), g, sizeof(double) * P)) { set_d(0, e); status = 1; }
        else note("QGRAD: the gradient's array is not writable");
    }
    g_free(th);
    g_free(g);
    return status;
}

/* runs argv; its stdout and stderr in *out, *err. posix_spawn, not fork: fork's atfork handlers take QEMU's RCU lock,
 * which waits for this vCPU thread, so a fork from a vCPU callback deadlocks */
static int qvsample(void)
{
    problem *p = find(x(3));
    int shots = (int)(uint32_t)x(2);
    if (!p) return 2;
    if (shots < 1 || shots > (1 << 22)) { note("QVSAMPLE: %d shots", shots); return 2; }
    double *th = g_new0(double, p->a.params + 1);
    uint64_t *s = g_new(uint64_t, (size_t)shots);
    int status = 2;
    if (!rd(x(1), th, sizeof(double) * p->a.params)) note("QVSAMPLE: the parameters are not readable");
    else if (qarm_rt_vsample(p->rt, th, shots, s) > 0)
        status = wr(x(4), s, sizeof *s * (size_t)shots) ? 1 : (note("QVSAMPLE: the samples are not writable"), 2);
    g_free(th);
    g_free(s);
    return status;
}

static int run(char *const argv[], gchar **out, gchar **err)
{
    extern char **environ;
    gchar *po = NULL, *pe = NULL;
    int fo = g_file_open_tmp("qarm-oracle-XXXXXX", &po, NULL), fe = g_file_open_tmp("qarm-oracle-XXXXXX", &pe, NULL);
    int status = -1;
    pid_t pid;
    posix_spawn_file_actions_t fa;
    posix_spawn_file_actions_init(&fa);
    if (fo >= 0 && fe >= 0) {
        posix_spawn_file_actions_adddup2(&fa, fo, 1);
        posix_spawn_file_actions_adddup2(&fa, fe, 2);
        if (posix_spawnp(&pid, argv[0], &fa, NULL, argv, environ) == 0) {
            while (waitpid(pid, &status, 0) < 0 && errno == EINTR) {}
        }
    }
    posix_spawn_file_actions_destroy(&fa);
    if (fo >= 0) close(fo);
    if (fe >= 0) close(fe);
    if (po) { g_file_get_contents(po, out, NULL, NULL); g_unlink(po); g_free(po); }
    if (pe) { g_file_get_contents(pe, err, NULL, NULL); g_unlink(pe); g_free(pe); }
    return WIFEXITED(status) ? WEXITSTATUS(status) : -1;
}

static qarm_oracle *oracle_for(uint64_t fn, unsigned bits, int compute)
{
    gpointer key = (gpointer)(uintptr_t)(fn * 2 + (compute != 0));     /* f is 4-byte aligned: room for the kind */
    qarm_oracle *o = g_hash_table_lookup(oracles, key);
    if (o) return o == ORACLE_FAILED ? NULL : o;
    o = ORACLE_FAILED;
    const char *path = qemu_plugin_path_to_binary();
    unsigned char code[32];
    if (have_search && path && rd(fn, code, sizeof code)) {
        char func[32], nb[16], hex[2 * sizeof code + 1];
        snprintf(func, sizeof func, "%#llx", (unsigned long long)fn);
        snprintf(nb, sizeof nb, "%u", bits);
        for (size_t i = 0; i < sizeof code; i++) snprintf(hex + 2 * i, 3, "%02x", code[i]);
        char *argv[] = {python, helper, "--elf", (char *)path, "--function", func, "--code", hex, "--bits", nb,
                        "--kind", compute ? "compute" : "phase", NULL};
        gchar *out = NULL, *err = NULL;
        gint64 t0 = g_get_monotonic_time();
        int status = run(argv, &out, &err);
        if (status < 0) {
            note("QSEARCH: cannot run %s %s", python, helper);
        } else if (status != 0) {
            note("QSEARCH: no oracle for the function at %s: %s", func, err && *err ? g_strstrip(err) : "the helper failed");
        } else {
            char perr[300];
            qarm_oracle *p = qarm_rt_oracle_parse(out, perr, sizeof perr);
            if (p) {
                o = p;
                note("the %s oracle of %s from its machine code: %d qubits, %d gates (%.1f s)",
                     compute ? "compute" : "phase", qarm_rt_oracle_name(p), qarm_rt_oracle_qubits(p), qarm_rt_oracle_gates(p), (g_get_monotonic_time() - t0) / 1e6);
            } else
                note("QSEARCH: the helper's oracle: %s", perr);
        }
        g_free(out);
        g_free(err);
    }
    g_free((gpointer)path);
    g_hash_table_insert(oracles, key, o);
    return o == ORACLE_FAILED ? NULL : o;
}

static void qsearch(void)
{
    uint64_t fn = x(1);
    unsigned bits = (uint32_t)x(2);
    qarm_oracle *o = bits >= 1 && bits <= 20 ? oracle_for(fn, bits, 0) : NULL;
    if (!o) {
        if (!told_search++) note("QSEARCH: the program takes its classical path");
        return;                                        /* W0 stays 0 */
    }
    int found = 0;
    char report[600];
    int s = qarm_rt_search(o, &found, report, sizeof report);
    note("%s", report);
    if (s == 1) { set_x(0, 1); set_x(3, (uint64_t)(uint32_t)found); }
    else if (s == 2) set_x(0, 2);
}

/* QMIN and QCOUNT: W0 stays 0 (the classical path) when there is no oracle */
static void qmin_count(int min)
{
    uint64_t fn = x(1);
    unsigned bits = (uint32_t)x(2);
    qarm_oracle *o = bits >= 1 && bits <= 20 ? oracle_for(fn, bits, min) : NULL;
    if (!o) return;
    char report[600];
    if (min) {
        int found = 0;
        long value = 0;
        if (qarm_rt_min(o, &found, &value, report, sizeof report) == 1) {
            set_x(0, 1);
            set_x(3, (uint64_t)(uint32_t)found);
            set_x(4, (uint64_t)(uint32_t)value);
        }
    } else {
        long count = 0;
        double bound = 0;
        if (qarm_rt_count(o, &count, &bound, report, sizeof report) == 1) {
            set_x(0, 1);
            set_x(3, (uint64_t)count);
            set_d(0, bound);
        }
    }
    note("%s", report);
}

static void qcollision(void)
{
    uint64_t fn = x(1);
    unsigned bits = (uint32_t)x(2);
    qarm_oracle *o = bits >= 1 && bits <= 20 ? oracle_for(fn, bits, 1) : NULL;
    if (!o) return;
    char report[600];
    int a = 0, b = 0, s = qarm_rt_collision(o, &a, &b, report, sizeof report);
    if (s == 1) { set_x(0, 1); set_x(3, (uint64_t)(uint32_t)a); set_x(4, (uint64_t)(uint32_t)b); }
    else if (s == 2) set_x(0, 2);
    note("%s", report);
}

static void qampest(void)
{
    uint64_t fn = x(1);
    unsigned bits = (uint32_t)x(2), k = (uint32_t)x(3);
    qarm_oracle *o = bits >= 1 && bits <= 20 ? oracle_for(fn, bits, 1) : NULL;
    if (!o) return;
    char report[600];
    double mean = 0, bound = 0;
    if (qarm_rt_ampest(o, (int)k, &mean, &bound, report, sizeof report) == 1) {
        set_x(0, 1);
        set_d(0, mean);
        set_d(1, bound);
    }
    note("%s", report);
}

static void qstructure(unsigned op)
{
    uint64_t fn = x(1);
    unsigned bits = (uint32_t)x(2);
    qarm_oracle *o = bits >= 1 && bits <= 20 ? oracle_for(fn, bits, op != OP_QBV) : NULL;
    if (!o) return;
    char report[600];
    int out = 0;
    int s = op == OP_QPERIOD ? qarm_rt_period(o, &out, report, sizeof report)
          : op == OP_QSIMON ? qarm_rt_simon(o, &out, report, sizeof report) : qarm_rt_bv(o, &out, report, sizeof report);
    note("%s", report);
    if (s == 1) { set_x(0, 1); set_x(3, (uint64_t)(uint32_t)out); }
    else if (s == 2) set_x(0, 2);
}

static uint32_t features(void)
{
    return QARM_F_VQE | QARM_F_CIRC | QARM_F_RAND | QARM_F_CAPS | QARM_F_VSAMPLE | QARM_F_ASYNC
           | (have_search ? QARM_F_SEARCH | QARM_F_MIN | QARM_F_COUNT | QARM_F_PERIOD | QARM_F_SIMON | QARM_F_BV
                            | QARM_F_AMPEST | QARM_F_COLLISION : 0);
}

#define MAX_SHOTS (1 << 22)
static int qcirc(void)
{
    struct { uint32_t qubits, gates; uint64_t g; } h;
    int shots = (int)(uint32_t)x(2);
    if (!rd(x(1), &h, sizeof h) || h.gates > 1000000 || shots < 1 || shots > MAX_SHOTS) {
        note("QCIRC refused: unreadable circuit at %#llx, or %d shots", (unsigned long long)x(1), shots);
        return 2;
    }
    qarm_gate *g = g_new0(qarm_gate, h.gates + 1);
    uint64_t *samples = g_new(uint64_t, (size_t)shots);
    int rc = 2;
    if (h.gates && !rd(h.g, g, sizeof *g * h.gates)) note("QCIRC refused: unreadable gate table");
    else {
        qarm_circuit c = {h.qubits, h.gates, g};
        if (qarm_rt_circuit(&c, shots, samples) >= 0 && wr(x(3), samples, sizeof *samples * (size_t)shots)) rc = 1;
    }
    g_free(g);
    g_free(samples);
    return rc;
}

static int qrand(void)
{
    int n = (int)(uint32_t)x(2);
    if (n < 1 || n > MAX_SHOTS) return 0;
    uint8_t *b = g_malloc((size_t)n);
    int q = qarm_rt_rand(b, n), rc = q < 0 ? 0 : wr(x(1), b, (size_t)n) ? (q == 1 ? 1 : 3) : 0;
    g_free(b);
    return rc;
}

static int qcaps(void)
{
    qarm_capabilities c;
    if (qarm_rt_caps(&c) < 0) return 0;
    char name[sizeof c.backend];
    memcpy(name, c.backend, sizeof name);
    snprintf(c.backend, sizeof c.backend, "qemu %.42s", name);
    c.features = features();
    return wr(x(1), &c, sizeof c) ? 1 : 0;
}

static void dispatch(unsigned op, uint64_t pc);

/* ---------------------------------------------------------------- QSUBMIT, QPOLL, QWAIT */
typedef struct { uint32_t opcode, state; uint64_t x[6]; double d[2]; uint64_t ticket; } request;
typedef struct { qarm_job *job; uint64_t out; int k; } pending;
static GHashTable *requests;            /* request address -> pending *: QEXPECTN jobs still on libqarm's QPU */

static void finish(uint64_t addr, pending *p)
{
    request q;
    double *e = g_new0(double, p->k);
    int ok = qarm_rt_wait(p->job, e, NULL) > 0 && wr(p->out, e, sizeof *e * (size_t)p->k);
    if (rd(addr, &q, sizeof q)) {
        q.x[0] = ok ? 1 : 2;
        q.state = 1;
        wr(addr, &q, sizeof q);
    }
    g_free(e);
    g_hash_table_remove(requests, GSIZE_TO_POINTER(addr));
}

static void qsubmit(uint64_t addr)
{
    request q;
    _Static_assert(sizeof(request) == 80, "qarm_request is 80 bytes");
    if (!rd(addr, &q, sizeof q)) { note("QSUBMIT: the request is not readable"); set_x(0, 2); return; }
    if (q.opcode == OP_QSUBMIT || q.opcode == OP_QPOLL || q.opcode == OP_QWAIT) {
        note("QSUBMIT: opcode %#x cannot be submitted", q.opcode);
        set_x(0, 2);
        return;
    }
    if (q.opcode == OP_QEXPECTN) {                       /* libqarm submits the job; the guest runs on */
        problem *p = find(q.x[3]);
        uint64_t k = (uint32_t)q.x[2], P = p ? p->a.params : 0;
        if (p && k >= 1 && k <= 4096) {
            double *th = g_new0(double, k * P + 1);
            qarm_job *j = rd(q.x[1], th, sizeof(double) * k * P) ? qarm_rt_submit(p->rt, th, (int)k, 0) : NULL;
            g_free(th);
            if (j) {
                pending *pd = g_new0(pending, 1);
                *pd = (pending){j, q.x[4], (int)k};
                g_hash_table_insert(requests, GSIZE_TO_POINTER(addr), pd);
                q.state = 0;
                q.ticket = addr;
                wr(addr, &q, sizeof q);
                if (qarm_rt_poll(j) != 0) finish(addr, pd);     /* exact and simulator: done at once */
                set_x(0, 1);
                return;
            }
        }
    }
    vr.on = 1;                                           /* any other instruction: now, on the request's registers */
    memcpy(vr.x, q.x, sizeof vr.x);
    memcpy(vr.d, q.d, sizeof vr.d);
    vr.x[0] = 0;
    dispatch(q.opcode, 0);
    vr.on = 0;
    memcpy(q.x, vr.x, sizeof q.x);
    memcpy(q.d, vr.d, sizeof q.d);
    q.state = 1;
    q.ticket = addr;
    wr(addr, &q, sizeof q);
    set_x(0, 1);
}

static void qpoll(uint64_t addr, int wait)
{
    pending *p = g_hash_table_lookup(requests, GSIZE_TO_POINTER(addr));
    if (p && (wait || qarm_rt_poll(p->job) != 0)) finish(addr, p);
    request q;
    set_x(0, rd(addr, &q, sizeof q) ? 1 : 2);
    if (!wait) set_x(1, q.state);
}

static void packet(unsigned int vcpu, void *udata)
{
    (void)vcpu;
    uint64_t pc = (uint64_t)(uintptr_t)udata;
    uint32_t w[3];
    if (!rd(pc + 4, w, sizeof w) || w[0] != MAGIC) return;    /* an ordinary B .+32 */
    unsigned version = w[1] >> 24, op = w[1] >> 16 & 0xFF;
    if (version != 2) return;
    g_mutex_lock(&lock);
    if (op == OP_QSUBMIT) qsubmit(x(1));
    else if (op == OP_QPOLL || op == OP_QWAIT) qpoll(x(1), op == OP_QWAIT);
    else dispatch(op, pc);
    g_mutex_unlock(&lock);
}

static void dispatch(unsigned op, uint64_t pc)
{
    switch (op) {
    case OP_QAVAIL:
        set_x(0, 1);
        set_x(1, features());
        break;
    case OP_QCIRC:
        set_x(0, qcirc());
        break;
    case OP_QRAND:
        set_x(0, qrand());
        break;
    case OP_QCAPS:
        set_x(0, qcaps());
        break;
    case OP_QSEARCH:
        qsearch();
        break;
    case OP_QMIN:
    case OP_QCOUNT:
        qmin_count(op == OP_QMIN);
        break;
    case OP_QAMPEST:
        qampest();
        break;
    case OP_QCOLLISION:
        qcollision();
        break;
    case OP_QPERIOD:
    case OP_QSIMON:
    case OP_QBV:
        qstructure(op);
        break;
    case OP_QVLOAD:
        set_x(0, qvload(x(1), x(2)));
        break;
    case OP_QEXPECT:
    case OP_QEXPECTN: {
        int s = qexpect(op == OP_QEXPECTN);
        set_x(0, s);
        break;
    }
    case OP_QGRAD:
        set_x(0, qgrad());
        break;
    case OP_QVSAMPLE:
        set_x(0, qvsample());
        break;
    case OP_QSESSION:
        qarm_rt_session((uint32_t)x(1) != 0);
        set_x(0, 1);
        break;
    case OP_QVFREE: {
        uint64_t h = x(3);
        if (h >= 1 && h <= problems->len && g_ptr_array_index(problems, h - 1)) {
            problem_free(g_ptr_array_index(problems, h - 1));
            g_ptr_array_index(problems, h - 1) = NULL;
        }
        set_x(0, 1);
        break;
    }
    default:
        note("unknown Q-ARM opcode %#x at %#llx: not executed", op, (unsigned long long)pc);
    }
}

/* ---------------------------------------------------------------- QEMU's callbacks */
static void tb_trans(qemu_plugin_id_t id, struct qemu_plugin_tb *tb)
{
    (void)id;
    for (size_t i = 0; i < qemu_plugin_tb_n_insns(tb); i++) {
        struct qemu_plugin_insn *insn = qemu_plugin_tb_get_insn(tb, i);
        uint32_t word = 0;
        if (qemu_plugin_insn_size(insn) == 4 && qemu_plugin_insn_data(insn, &word, 4) == 4 && word == B_32)
            qemu_plugin_register_vcpu_insn_exec_cb(insn, packet, QEMU_PLUGIN_CB_RW_REGS,
                                                   (void *)(uintptr_t)qemu_plugin_insn_vaddr(insn));
    }
}

static void vcpu_init(qemu_plugin_id_t id, unsigned int vcpu)
{
    (void)id; (void)vcpu;
    g_autoptr(GArray) regs = qemu_plugin_get_registers();
    for (guint i = 0; i < regs->len; i++) {
        qemu_plugin_reg_descriptor *r = &g_array_index(regs, qemu_plugin_reg_descriptor, i);
        if (r->name[0] == 'x') {
            char *end;
            long n = strtol(r->name + 1, &end, 10);
            if (!*end && n >= 0 && n < 31) reg_x[n] = r->handle;
        } else if (!strcmp(r->name, "v0") || (!strcmp(r->name, "z0") && !reg_v[0])) reg_v[0] = r->handle;
        else if (!strcmp(r->name, "v1") || (!strcmp(r->name, "z1") && !reg_v[1])) reg_v[1] = r->handle;
    }
    for (int n = 0; n < 31; n++)
        if (!reg_x[n]) { fprintf(stderr, "qarm-qemu: no register x%d: is this an aarch64 guest?\n", n); exit(70); }
    if (!reg_v[0] || !reg_v[1]) { fprintf(stderr, "qarm-qemu: no registers v0, v1 (or z0, z1)\n"); exit(70); }
}

static void at_exit(qemu_plugin_id_t id, void *p)
{
    (void)id; (void)p;
    g_ptr_array_free(problems, TRUE);
}

QEMU_PLUGIN_EXPORT int qemu_plugin_install(qemu_plugin_id_t id, const qemu_info_t *info, int argc, char **argv)
{
    if (strcmp(info->target_name, "aarch64")) {
        fprintf(stderr, "qarm-qemu: Q-ARM is for aarch64 guests, not %s\n", info->target_name);
        return -1;
    }
    for (int i = 0; i < argc; i++) {
        char *eq = strchr(argv[i], '=');
        if (!eq) { fprintf(stderr, "qarm-qemu: option %s: expected name=value\n", argv[i]); return -1; }
        *eq = 0;
        const char *v = eq + 1;
        if (!strcmp(argv[i], "config")) g_setenv("QARM_CONFIG", v, TRUE);
        else if (!strcmp(argv[i], "backend")) g_setenv("QARM_BACKEND", v, TRUE);
        else if (!strcmp(argv[i], "helper")) helper = g_strdup(v);
        else if (!strcmp(argv[i], "python")) python = g_strdup(v);
        else if (!strcmp(argv[i], "quiet")) {
            quiet = !strcmp(v, "on") || !strcmp(v, "true") || !strcmp(v, "1");
            if (quiet) g_setenv("QARM_QUIET", "1", TRUE);
        } else {
            fprintf(stderr, "qarm-qemu: unknown option %s (config, backend, quiet, helper, python)\n", argv[i]);
            return -1;
        }
    }
    if (!helper) helper = g_strdup(g_getenv("QARM_ORACLE_HELPER") ? g_getenv("QARM_ORACLE_HELPER")
                                                                    : QARM_TOOLS_DIR "/qarm_oracle.py");
    if (!python) python = g_strdup("python3");
    have_search = g_file_test(helper, G_FILE_TEST_EXISTS);
    oracles = g_hash_table_new(g_direct_hash, g_direct_equal);
    requests = g_hash_table_new_full(g_direct_hash, g_direct_equal, NULL, g_free);
    problems = g_ptr_array_new_with_free_func(problem_free);
    qemu_plugin_register_vcpu_init_cb(id, vcpu_init);
    qemu_plugin_register_vcpu_tb_trans_cb(id, tb_trans);
    qemu_plugin_register_atexit_cb(id, at_exit, NULL);
    return 0;
}
