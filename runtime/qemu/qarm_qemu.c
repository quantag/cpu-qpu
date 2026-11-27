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
 *   QAVAIL    W0 = 1, W1 = QARM_F_VQE
 *   QVLOAD, QEXPECT, QEXPECTN, QGRAD, QSESSION, QVFREE    on libqarm's backend (exact, simulator, ibm)
 *   QSEARCH   not executed (W0 stays 0): qarm.h takes the classical path; its oracle needs qoracle, which is Python */
#include <glib.h>
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
enum { OP_QAVAIL = 0x10, OP_QSEARCH = 0x11, OP_QVLOAD = 0x20, OP_QEXPECT = 0x21, OP_QEXPECTN = 0x22,
       OP_QGRAD = 0x23, OP_QSESSION = 0x24, OP_QVFREE = 0x26 };

static struct qemu_plugin_register *reg_x[31], *reg_v[2];
static GMutex lock;
static int quiet, told_search;

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
static uint64_t x(int n)
{
    g_autoptr(GByteArray) b = g_byte_array_new();
    uint64_t v = 0;
    if (qemu_plugin_read_register(reg_x[n], b) >= 8) memcpy(&v, b->data, 8);
    return v;
}

static void set_x(int n, uint64_t v)
{
    g_autoptr(GByteArray) b = g_byte_array_new();
    g_byte_array_append(b, (const guint8 *)&v, 8);
    qemu_plugin_write_register(reg_x[n], b);
}

static void set_d(int n, double d)     /* the low 8 bytes of v<n> (z<n> with SVE); the rest cleared, as FMOV d<n> does */
{
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

static void packet(unsigned int vcpu, void *udata)
{
    (void)vcpu;
    uint64_t pc = (uint64_t)(uintptr_t)udata;
    uint32_t w[3];
    if (!rd(pc + 4, w, sizeof w) || w[0] != MAGIC) return;    /* an ordinary B .+32 */
    unsigned version = w[1] >> 24, op = w[1] >> 16 & 0xFF;
    if (version != 2) return;
    g_mutex_lock(&lock);
    switch (op) {
    case OP_QAVAIL:
        set_x(0, 1);
        set_x(1, QARM_F_VQE);
        break;
    case OP_QSEARCH:
        if (!told_search++) note("QSEARCH: not in the QEMU backend yet; the program takes its classical path");
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
    g_mutex_unlock(&lock);
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
        else if (!strcmp(argv[i], "quiet")) {
            quiet = !strcmp(v, "on") || !strcmp(v, "true") || !strcmp(v, "1");
            if (quiet) g_setenv("QARM_QUIET", "1", TRUE);
        } else { fprintf(stderr, "qarm-qemu: unknown option %s (config, backend, quiet)\n", argv[i]); return -1; }
    }
    problems = g_ptr_array_new_with_free_func(problem_free);
    qemu_plugin_register_vcpu_init_cb(id, vcpu_init);
    qemu_plugin_register_vcpu_tb_trans_cb(id, tb_trans);
    qemu_plugin_register_atexit_cb(id, at_exit, NULL);
    return 0;
}
