/* SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 Quantag IT Solutions GmbH
 *
 * A minimal Q-ARM runtime: the Core and Circuit profiles of docs/qarm_isa.md, section 8, as a QEMU plugin, in
 * one file. It is the companion of docs/qarm_runtime_guide.md: each step of the guide is a section here.
 *
 *   QAVAIL  W0 = 1, W1 = QARM_F_CAPS | QARM_F_CIRC | QARM_F_RAND
 *   QCAPS   the 64 bytes of qarm_capabilities
 *   QCIRC   the gate table read from guest memory, simulated with include/qarm_sv.h's state vector, sampled
 *   QRAND   bytes from 8 simulated qubits in H: W0 = 3 (a simulator's, not quantum hardware's)
 *   any other opcode: W0 stays 0, so the program takes its classical path
 *
 * It needs only include/qarm_isa.h and include/qarm_sv.h. The conformance program passes its Core and Circuit
 * tests under it and skips the rest:
 *   cmake -S runtime/minimal -B build/minimal -DQEMU_PLUGIN_INCLUDE=<dir of qemu-plugin.h> && cmake --build build/minimal
 *   qemu-aarch64 -plugin build/minimal/libqarm-minimal.so ./conformance         (or qemu-x86_64) */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <qemu-plugin.h>
#include "qarm_isa.h"
#include "qarm_sv.h"

QEMU_PLUGIN_EXPORT int qemu_plugin_version = QEMU_PLUGIN_VERSION;

/* ---------------------------------------------------------------- step 1: the registers of the ISA (3, 2.1) */
static struct qemu_plugin_register *reg_x[6];   /* X0..X5 */
static int x86;

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

/* ---------------------------------------------------------------- step 2: guest memory */
static int rd(uint64_t addr, void *out, size_t n)
{
    g_autoptr(GByteArray) b = g_byte_array_new();
    if (!qemu_plugin_read_memory_vaddr(addr, b, n) || b->len < n) return 0;
    memcpy(out, b->data, n);
    return 1;
}

static int wr(uint64_t addr, const void *in, size_t n)
{
    GByteArray b = {(guint8 *)in, (guint)n};
    return qemu_plugin_write_memory_vaddr(addr, &b);
}

/* ---------------------------------------------------------------- step 3: the instructions */
static uint64_t rng = 0x9E3779B97F4A7C15ULL;
static double uniform(void)                      /* splitmix64 */
{
    uint64_t z = (rng += 0x9E3779B97F4A7C15ULL);
    z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ULL;
    z = (z ^ (z >> 27)) * 0x94D049BB133111EBULL;
    return (double)((z ^ (z >> 31)) >> 11) / 9007199254740992.0;
}

#define MAX_QUBITS 20
#define MAX_SHOTS (1 << 22)

/* the state of a circuit table, sampled into shots outcomes; 1, or 2: refused (5.4) */
static int circuit(const qarm_circuit *c, int shots, uint64_t *out)
{
    if (c->qubits < 1 || c->qubits > MAX_QUBITS || shots < 1) return QARM_ST_NEGATIVE;
    for (uint32_t i = 0; i < c->gates; i++)
        if (!qarm_sv_gate_ok(&c->g[i], c->qubits)) return QARM_ST_NEGATIVE;
    long N = 1L << c->qubits;
    qarm_sv s = {(int)c->qubits, 0, 0, 0, 0, 0, 0};
    double **v[6] = {&s.re, &s.im, &s.tr, &s.ti, &s.lr, &s.li};
    for (int i = 0; i < 6; i++) *v[i] = g_new0(double, N);
    qarm_sv_circuit(&s, c);
    for (long b = 0; b < N; b++) s.tr[b] = (b ? s.tr[b - 1] : 0) + s.re[b] * s.re[b] + s.im[b] * s.im[b];
    for (int k = 0; k < shots; k++) {            /* inverse transform sampling on the cumulative probabilities */
        double u = uniform() * s.tr[N - 1];
        long lo = 0, hi = N - 1;
        while (lo < hi) { long mid = (lo + hi) / 2; if (u < s.tr[mid]) hi = mid; else lo = mid + 1; }
        out[k] = (uint64_t)lo;
    }
    for (int i = 0; i < 6; i++) g_free(*v[i]);
    return QARM_ST_DONE;
}

static int qcirc(void)                           /* X1 = qarm_circuit *, W2 = shots, X3 = the outcomes */
{
    struct { uint32_t qubits, gates; uint64_t g; } h;
    int shots = (int)(uint32_t)x(2), st = QARM_ST_NEGATIVE;
    if (!rd(x(1), &h, sizeof h) || h.gates > 1000000 || shots > MAX_SHOTS) return st;
    qarm_gate *g = g_new0(qarm_gate, h.gates + 1);
    uint64_t *out = g_new(uint64_t, shots > 0 ? shots : 1);
    if (!h.gates || rd(h.g, g, sizeof *g * h.gates)) {
        qarm_circuit c = {h.qubits, h.gates, g};
        st = circuit(&c, shots, out);
        if (st == QARM_ST_DONE && !wr(x(3), out, sizeof *out * (size_t)shots)) st = QARM_ST_NEGATIVE;
    }
    g_free(g);
    g_free(out);
    return st;
}

static int qrand(void)                           /* X1 = buffer, W2 = n */
{
    int n = (int)(uint32_t)x(2);
    if (n < 1 || n > MAX_SHOTS) return QARM_ST_NOT_EXECUTED;
    qarm_gate g[8];
    for (int q = 0; q < 8; q++) g[q] = (qarm_gate){QARM_G_H, (uint8_t)q, 0, 0, 0, 0};
    qarm_circuit c = {8, 8, g};
    uint64_t *out = g_new(uint64_t, n);
    uint8_t *bytes = g_new(uint8_t, n);
    circuit(&c, n, out);
    for (int i = 0; i < n; i++) bytes[i] = (uint8_t)out[i];
    int ok = wr(x(1), bytes, (size_t)n);
    g_free(out);
    g_free(bytes);
    return ok ? QARM_ST_SIMULATED : QARM_ST_NOT_EXECUTED;
}

static int qcaps(void)                           /* X1 = qarm_capabilities * */
{
    qarm_capabilities c = {QARM_ISA_CAPS_VERSION, QARM_PROFILE_CORE | QARM_PROFILE_CIRCUIT, MAX_QUBITS, 0, ""};
    snprintf(c.backend, sizeof c.backend, "qarm-minimal: a %d-qubit state vector", MAX_QUBITS);
    return wr(x(1), &c, sizeof c) ? QARM_ST_DONE : QARM_ST_NOT_EXECUTED;
}

/* ---------------------------------------------------------------- step 4: recognize and execute a packet (2) */
static void packet(unsigned int vcpu, void *udata)
{
    (void)vcpu;
    uint64_t pc = (uint64_t)(uintptr_t)udata;
    uint32_t w[2];
    if (!rd(pc + 4, w, sizeof w) || w[0] != QARM_ISA_MAGIC) return;      /* an ordinary jump */
    if (w[1] >> 24 != QARM_ISA_PACKET_FORMAT || (w[1] & 0xFFFF)) return; /* not 1.0's packet */
    switch (w[1] >> 16 & 0xFF) {
    case QARM_OPC_QAVAIL:
        set_x(0, QARM_ST_DONE);
        set_x(1, QARM_PROFILE_CORE | QARM_PROFILE_CIRCUIT);
        break;
    case QARM_OPC_QCAPS: set_x(0, (uint64_t)qcaps()); break;
    case QARM_OPC_QCIRC: set_x(0, (uint64_t)qcirc()); break;
    case QARM_OPC_QRAND: set_x(0, (uint64_t)qrand()); break;
    default: break;                              /* not implemented: W0 stays 0 (3, "Unknown opcodes") */
    }
}

static void tb_trans(qemu_plugin_id_t id, struct qemu_plugin_tb *tb)
{
    (void)id;
    for (size_t i = 0; i < qemu_plugin_tb_n_insns(tb); i++) {
        struct qemu_plugin_insn *insn = qemu_plugin_tb_get_insn(tb, i);
        uint32_t word = 0;
        size_t n = qemu_plugin_insn_size(insn);
        int jump = x86 ? n == 2 && qemu_plugin_insn_data(insn, &word, 2) == 2 && word == (QARM_ISA_X64_JUMP & 0xFFFF)
                       : n == 4 && qemu_plugin_insn_data(insn, &word, 4) == 4 && word == QARM_ISA_A64_BRANCH;
        if (jump)                                /* the packet's first instruction: run it before the jump */
            qemu_plugin_register_vcpu_insn_exec_cb(insn, packet, QEMU_PLUGIN_CB_RW_REGS,
                                                   (void *)(uintptr_t)qemu_plugin_insn_vaddr(insn));
    }
}

static void vcpu_init(qemu_plugin_id_t id, unsigned int vcpu)
{
    (void)id; (void)vcpu;
    static const char *a64[6] = {"x0", "x1", "x2", "x3", "x4", "x5"}, *amd64[6] = {"rax", "rdi", "rsi", "rdx", "rcx", "r8"};
    const char **names = x86 ? amd64 : a64;
    g_autoptr(GArray) regs = qemu_plugin_get_registers();
    for (guint i = 0; i < regs->len; i++) {
        qemu_plugin_reg_descriptor *r = &g_array_index(regs, qemu_plugin_reg_descriptor, i);
        for (int k = 0; k < 6; k++)
            if (!strcmp(r->name, names[k])) reg_x[k] = r->handle;
    }
    for (int k = 0; k < 6; k++)
        if (!reg_x[k]) { fprintf(stderr, "qarm-minimal: no register %s\n", names[k]); exit(70); }
}

QEMU_PLUGIN_EXPORT int qemu_plugin_install(qemu_plugin_id_t id, const qemu_info_t *info, int argc, char **argv)
{
    (void)argc; (void)argv;
    x86 = !strcmp(info->target_name, "x86_64");
    if (!x86 && strcmp(info->target_name, "aarch64")) return -1;
    qemu_plugin_register_vcpu_init_cb(id, vcpu_init);
    qemu_plugin_register_vcpu_tb_trans_cb(id, tb_trans);
    return 0;
}
