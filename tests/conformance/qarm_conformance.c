/* SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 Quantag IT Solutions GmbH
 *
 * The Q-ARM conformance program (docs/qarm_isa.md, section 9). An arm64 or x86-64 program that issues the raw packets of
 * every instruction and checks what a runtime does with them, independently of include/qarm.h's fallbacks:
 *
 *   - W0's protocol: 0 is left by a plain CPU; 1 done; 2 refused (each refusal case of the spec is tried);
 *   - the output registers and buffers; nothing else changes: X5-X15 and the bytes around every output buffer
 *     are checked after each packet;
 *   - the answers, against exact values (probabilistic instructions: up to 3 tries, error bounds as specified);
 *   - feature bits: QAVAIL's W1 and QCAPS's features agree, and every claimed feature is tested. An unclaimed
 *     one is skipped; it must still leave W0 = 0 or 2 (never a wrong answer with W0 = 1).
 *
 * Output: one line per test, "PASS name", "FAIL name: why" or "SKIP name: why"; the last line "conformance:
 * P passed, F failed, S skipped (features 0x...)". Exit code: the number of failures. On a plain CPU every test is
 * skipped (no runtime). Build at -O1 (the oracles must be leaf functions without vector instructions). */
#include <math.h>
#include <stdio.h>
#include <string.h>
#include "qarm.h"

#if defined(__x86_64__)                  /* registers that are not operands: they must survive every packet */
#define S9 "rbx"
#define S10 "r9"
#define S11 "r10"
#define S12 "r11"
#define S13 "r12"
#define S14 "r13"
#define S15 "r14"
#else
#define S9 "x9"
#define S10 "x10"
#define S11 "x11"
#define S12 "x12"
#define S13 "x13"
#define S14 "x14"
#define S15 "x15"
#endif
typedef struct { uint64_t x[6]; double d[2]; } regs;
#define SENT(i) (0x5157000000000000ULL | (uint64_t)(i) * 0x1111)
static int clobbered;

/* the packet with X0..X5, D0, D1 in and out; X9..X15 hold sentinels that must survive */
#define PACKET(OPW, r)                                                                                               \
    do {                                                                                                             \
        register uint64_t x0 __asm__(QARM_X0) = (r)->x[0], x1 __asm__(QARM_X1) = (r)->x[1], x2 __asm__(QARM_X2) = (r)->x[2];  \
        register uint64_t x3 __asm__(QARM_X3) = (r)->x[3], x4 __asm__(QARM_X4) = (r)->x[4], x5 __asm__(QARM_X5) = (r)->x[5];  \
        register double d0 __asm__(QARM_D0) = (r)->d[0], d1 __asm__(QARM_D1) = (r)->d[1];                                  \
        register uint64_t s9 __asm__(S9) = SENT(9), s10 __asm__(S10) = SENT(10), s11 __asm__(S11) = SENT(11);  \
        register uint64_t s12 __asm__(S12) = SENT(12), s13 __asm__(S13) = SENT(13), s14 __asm__(S14) = SENT(14);\
        register uint64_t s15 __asm__(S15) = SENT(15);                                                             \
        __asm__ volatile(QARM_PACKET(OPW)                                                                            \
                         : "+r"(x0), "+r"(x1), "+r"(x2), "+r"(x3), "+r"(x4), "+r"(x5), "+" QARM_FP(d0), "+" QARM_FP(d1),           \
                           "+r"(s9), "+r"(s10), "+r"(s11), "+r"(s12), "+r"(s13), "+r"(s14), "+r"(s15)                \
                         :                                                                                           \
                         : "memory");                                                                                \
        uint64_t before5 = (r)->x[5];                                                                                \
        (r)->x[0] = x0; (r)->x[1] = x1; (r)->x[2] = x2; (r)->x[3] = x3; (r)->x[4] = x4; (r)->x[5] = x5;              \
        (r)->d[0] = d0; (r)->d[1] = d1;                                                                              \
        if (x5 != before5 || s9 != SENT(9) || s10 != SENT(10) || s11 != SENT(11) || s12 != SENT(12)                  \
            || s13 != SENT(13) || s14 != SENT(14) || s15 != SENT(15))                                                \
            clobbered = 1;                                                                                           \
    } while (0)

static int passed, failed, skipped;
static uint32_t features;
static char why[200];

/* 8. Profiles: each test belongs to one; a profile passes when all its tests pass, and is not claimed when all
 * are skipped */
enum { P_CORE, P_ORACLE, P_VARIATIONAL, P_CIRCUIT, P_ASYNC, P_DYNAMICS, NPROFILES };
static const char *profile_name[NPROFILES] = {"Core", "Oracle", "Variational", "Circuit", "Async", "Dynamics"};
static int prof_pass[NPROFILES], prof_fail[NPROFILES], prof_skip[NPROFILES], current = P_CORE;

static void result(const char *name, int ok)
{
    (ok == 1 ? prof_pass : ok == 0 ? prof_fail : prof_skip)[current]++;
    if (ok == 1) { printf("PASS %s\n", name); passed++; }
    else if (ok == 0) { printf("FAIL %s: %s\n", name, why); failed++; }
    else { printf("SKIP %s: %s\n", name, why); skipped++; }
    why[0] = 0;
}
#define REQUIRE(c, ...) do { if (!(c)) { snprintf(why, sizeof why, __VA_ARGS__); return 0; } } while (0)
#define CLEAN() do { clobbered = 0; } while (0)
#define PRESERVED() REQUIRE(!clobbered, "a register other than the outputs changed (X5, and X9-X15 or RBX R9-R14)")

/* guarded buffers: 8 bytes of 0xA5 before and after */
static unsigned char arena[1 << 16];
static void *guarded(size_t n)
{
    memset(arena, 0xA5, sizeof arena);
    (void)n;
    return arena + 64;
}
static int guards_ok(size_t n)
{
    for (int i = 0; i < 64; i++)
        if (arena[i] != 0xA5 || arena[64 + n + (size_t)i] != 0xA5) return 0;
    return 1;
}

/* ---------------------------------------------------------------- the oracles: leaf functions */
__attribute__((noinline)) static int is_eleven(int x) { return x == 11; }
__attribute__((noinline)) static int never(int x) { return x == 100; }       /* not in 4 bits */
__attribute__((noinline)) static int odd(int x) { return x & 1; }
__attribute__((noinline)) static int square_dist(int x) { return (x - 5) * (x - 5) - 3; }
__attribute__((noinline)) static int low2(int x) { return x & 3; }
__attribute__((noinline)) static int mod3(int x) { return x % 3; }
__attribute__((noinline)) static int one_to_one(int x) { return x ^ 9; }
__attribute__((noinline)) static int shift6(int x) { int y = x ^ 6; return x < y ? x : y; }
__attribute__((noinline)) static int half(int x) { return x >> 1; }        /* collisions: 2k and 2k + 1 */
__attribute__((noinline)) static int dot_b(int x) { return ((x ^ x >> 1 ^ x >> 3) & 1) ^ 1; }   /* 0xB . x, plus 1 */

static int have(uint32_t bit, const char *name)
{
    if (features & bit) return 1;
    snprintf(why, sizeof why, "%s not claimed in QAVAIL", name);
    return 0;
}

/* ---------------------------------------------------------------- tests */
static int t_qavail(void)
{
    regs r = {{0}, {0}};
    CLEAN();
    PACKET(0x02100000, &r);
    if (r.x[0] == 0) { snprintf(why, sizeof why, "no runtime: W0 stayed 0 (a plain CPU)"); return -1; }
    REQUIRE(r.x[0] == 1, "W0 = %llu", (unsigned long long)r.x[0]);
    PRESERVED();
    features = (uint32_t)r.x[1];
    REQUIRE(features != 0, "W1 = 0: no features");
    REQUIRE((features >> 15) == 0, "W1 = %#x: bits above QARM_F_EVOLVE are reserved", features);
    return 1;
}

static int t_qcaps(void)
{
    qarm_capabilities *c = guarded(sizeof *c);
    regs r = {{0, (uint64_t)(uintptr_t)c}, {0}};
    if (!have(QARM_F_CAPS, "QCAPS")) return -1;
    CLEAN();
    PACKET(0x02310000, &r);
    REQUIRE(r.x[0] == 1, "W0 = %llu", (unsigned long long)r.x[0]);
    PRESERVED();
    REQUIRE(guards_ok(sizeof *c), "wrote outside the qarm_capabilities");
    REQUIRE(c->version == 1, "version %u", c->version);
    REQUIRE(c->features == features, "features %#x, QAVAIL said %#x", c->features, features);
    int nul = 0;
    for (size_t i = 0; i < sizeof c->backend; i++) nul |= c->backend[i] == 0;
    REQUIRE(nul, "backend not NUL-terminated");
    REQUIRE(c->qubits >= 1, "qubits %u", c->qubits);
    REQUIRE((c->flags & ~7u) == 0, "flags %#x: reserved bits", c->flags);
    return 1;
}

static int unknown_opcode(void)
{
    regs r = {{0}, {0}};
    CLEAN();
    PACKET(0x027f0000, &r);                      /* not assigned */
    REQUIRE(r.x[0] == 0, "an unassigned opcode returned W0 = %llu (must stay 0)", (unsigned long long)r.x[0]);
    PRESERVED();
    return 1;
}

#define SEARCH_LIKE(OPW, fn, bits)                                                                                   \
    regs r = {{0, (uint64_t)(uintptr_t)(fn), (bits)}, {0}};                                                          \
    CLEAN();                                                                                                         \
    PACKET(OPW, &r);                                                                                                 \
    PRESERVED();

static int t_qsearch(void)
{
    if (!have(QARM_F_SEARCH, "QSEARCH")) return -1;
    int ok = 0;
    for (int tries = 0; tries < 3 && !ok; tries++) {
        SEARCH_LIKE(0x02110000, is_eleven, 4);
        REQUIRE(r.x[0] == 1 || r.x[0] == 2, "W0 = %llu", (unsigned long long)r.x[0]);
        ok = r.x[0] == 1 && (uint32_t)r.x[3] == 11;
        REQUIRE(r.x[0] != 1 || ok, "W3 = %u: not an answer (f(11) = 1 only)", (unsigned)r.x[3]);
    }
    REQUIRE(ok, "no answer in 3 tries");
    SEARCH_LIKE(0x02110000, never, 4);
    REQUIRE(r.x[0] == 2, "no solution: W0 = %llu, must be 2", (unsigned long long)r.x[0]);
    return 1;
}

static int t_qmin(void)
{
    if (!have(QARM_F_MIN, "QMIN")) return -1;
    for (int tries = 0; tries < 3; tries++) {
        SEARCH_LIKE(0x02150000, square_dist, 4);
        REQUIRE(r.x[0] == 1, "W0 = %llu", (unsigned long long)r.x[0]);
        int x = (int)(uint32_t)r.x[3], v = (int)(uint32_t)r.x[4];
        REQUIRE(v == square_dist(x), "W4 = %d is not cost(W3 = %d) = %d", v, x, square_dist(x));
        if (x == 5 && v == -3) return 1;
    }
    REQUIRE(0, "the least cost (-3 at 5) not found in 3 tries");
}

static int t_qcount(void)
{
    if (!have(QARM_F_COUNT, "QCOUNT")) return -1;
    for (int tries = 0; tries < 3; tries++) {
        SEARCH_LIKE(0x02160000, odd, 4);
        REQUIRE(r.x[0] == 1, "W0 = %llu", (unsigned long long)r.x[0]);
        REQUIRE(r.d[0] >= 0 && r.d[0] < 16, "D0 = %g: not an error bound", r.d[0]);
        if (fabs((double)r.x[3] - 8) <= r.d[0] + 1e-9) return 1;
    }
    REQUIRE(0, "8 not within the bound in 3 tries");
}

static int t_qampest(void)
{
    if (!have(QARM_F_AMPEST, "QAMPEST")) return -1;
    for (int tries = 0; tries < 3; tries++) {
        regs r = {{0, (uint64_t)(uintptr_t)low2, 4, 2}, {0}};
        CLEAN();
        PACKET(0x02170000, &r);
        PRESERVED();
        REQUIRE(r.x[0] == 1, "W0 = %llu", (unsigned long long)r.x[0]);
        REQUIRE(r.d[1] >= 0 && r.d[0] >= 0 && r.d[0] <= 3, "D0 = %g, D1 = %g", r.d[0], r.d[1]);
        if (fabs(r.d[0] - 1.5) <= r.d[1] + 1e-9) return 1;
    }
    REQUIRE(0, "the mean 1.5 not within the bound in 3 tries");
}

static int t_structure(uint32_t bit, const char *name, unsigned opw_sel)
{
    if (!have(bit, name)) return -1;
    for (int tries = 0; tries < 3; tries++) {
        regs r = {{0}, {0}};
        int want = 0;
        if (opw_sel == 0x18) { r.x[1] = (uint64_t)(uintptr_t)mod3; r.x[2] = 5; want = 3; CLEAN(); PACKET(0x02180000, &r); }
        else if (opw_sel == 0x19) { r.x[1] = (uint64_t)(uintptr_t)shift6; r.x[2] = 4; want = 6; CLEAN(); PACKET(0x02190000, &r); }
        else { r.x[1] = (uint64_t)(uintptr_t)dot_b; r.x[2] = 4; want = 0xB; CLEAN(); PACKET(0x021a0000, &r); }
        PRESERVED();
        REQUIRE(r.x[0] == 1 || r.x[0] == 2, "W0 = %llu", (unsigned long long)r.x[0]);
        if (r.x[0] == 1) {
            REQUIRE((int)(uint32_t)r.x[3] == want, "W3 = %d, the answer is %d", (int)(uint32_t)r.x[3], want);
            if (opw_sel == 0x19) {                                 /* one-to-one: none */
                regs q = {{0, (uint64_t)(uintptr_t)one_to_one, 4}, {0}};
                CLEAN();
                PACKET(0x02190000, &q);
                PRESERVED();
                REQUIRE(q.x[0] == 2, "QSIMON of a one-to-one f: W0 = %llu, must be 2", (unsigned long long)q.x[0]);
            }
            return 1;
        }
    }
    REQUIRE(0, "no answer in 3 tries");
}

static int t_qcollision(void)
{
    if (!have(QARM_F_COLLISION, "QCOLLISION")) return -1;
    for (int tries = 0; tries < 3; tries++) {
        SEARCH_LIKE(0x021b0000, half, 5);
        REQUIRE(r.x[0] == 1 || r.x[0] == 2, "W0 = %llu", (unsigned long long)r.x[0]);
        if (r.x[0] == 1) {
            unsigned a = (unsigned)r.x[3], b = (unsigned)r.x[4];
            REQUIRE(a != b && a < 32 && b < 32 && half((int)a) == half((int)b), "W3 = %u, W4 = %u: not a collision", a, b);
            SEARCH_LIKE(0x021b0000, one_to_one, 4);
            REQUIRE(r.x[0] == 2, "a one-to-one f: W0 = %llu, must be 2", (unsigned long long)r.x[0]);
            return 1;
        }
    }
    REQUIRE(0, "no collision in 3 tries");
}

static const qarm_gate bell_g[] = {{QARM_G_H, 0, 0, 0, 0, 0}, {QARM_G_CX, 0, 1, 0, 0, 0}, {QARM_G_X, 2, 0, 0, 0, 0},
                                   {QARM_G_RY, 3, 0, 0, 0, 3.14159265358979323846}};
static const qarm_circuit bell = {4, 4, bell_g};
static const qarm_gate bad_g[] = {{QARM_G_CX, 1, 1, 0, 0, 0}};
static const qarm_circuit bad1 = {2, 1, bad_g}, bad2 = {1, 1, bell_g + 1};

static int t_qcirc(void)
{
    if (!have(QARM_F_CIRC, "QCIRC")) return -1;
    const int shots = 400;
    uint64_t *s = guarded(8 * shots);
    regs r = {{0, (uint64_t)(uintptr_t)&bell, shots, (uint64_t)(uintptr_t)s}, {0}};
    CLEAN();
    PACKET(0x02300000, &r);
    PRESERVED();
    REQUIRE(r.x[0] == 1, "W0 = %llu", (unsigned long long)r.x[0]);
    REQUIRE(guards_ok(8 * shots), "wrote outside the samples");
    int n0 = 0, n3 = 0;
    for (int i = 0; i < shots; i++) {
        REQUIRE(s[i] == 0xC || s[i] == 0xF, "sample %d = %#llx: H CX X RY(pi) gives 1100 or 1111", i,
                (unsigned long long)s[i]);
        n0 += s[i] == 0xC;
        n3 += s[i] == 0xF;
    }
    REQUIRE(n0 > shots / 4 && n3 > shots / 4, "1100 %d, 1111 %d of %d: not half each", n0, n3, shots);
    const qarm_circuit *bad[] = {&bad1, &bad2};
    for (int b = 0; b < 2; b++) {
        regs q = {{0, (uint64_t)(uintptr_t)bad[b], 4, (uint64_t)(uintptr_t)s}, {0}};
        CLEAN();
        PACKET(0x02300000, &q);
        PRESERVED();
        REQUIRE(q.x[0] == 2, "%s: W0 = %llu, must be 2", b ? "a qubit outside the register" : "CX on one qubit",
                (unsigned long long)q.x[0]);
    }
    return 1;
}

static int t_qrand(void)
{
    if (!have(QARM_F_RAND, "QRAND")) return -1;
    unsigned char *b = guarded(256);
    regs r = {{0, (uint64_t)(uintptr_t)b, 256}, {0}};
    CLEAN();
    PACKET(0x021c0000, &r);
    PRESERVED();
    REQUIRE(r.x[0] == 1 || r.x[0] == 3, "W0 = %llu (1 hardware, 3 simulated)", (unsigned long long)r.x[0]);
    REQUIRE(guards_ok(256), "wrote outside the buffer");
    int ones = 0, distinct = 0, seen[256] = {0};
    for (int i = 0; i < 256; i++) {
        ones += __builtin_popcount(b[i]);
        distinct += !seen[b[i]]++;
    }
    REQUIRE(ones > 900 && ones < 1150 && distinct > 100, "%d ones of 2048 bits, %d distinct bytes", ones, distinct);
    return 1;
}

/* VQE: H = Z on one qubit, the ansatz RY(theta): E = cos(theta), dE/dtheta = -sin(theta) */
static const qarm_pauli z_t[] = {{0, 1, 1.0}};
static const qarm_hamiltonian z_h = {1, 1, 0.25, z_t};
static const qarm_op ry_o[] = {{QARM_RY, 0, 0, 0, 0, 1.0, 0, 0}};
static const qarm_ansatz ry_a = {1, 1, 1, 0, 0, ry_o};
static const qarm_op bad_o[] = {{QARM_RY, 3, 0, 0, 0, 1.0, 0, 0}};
static const qarm_ansatz bad_a = {1, 1, 1, 0, 0, bad_o};

static int t_vqe(void)
{
    if (!have(QARM_F_VQE, "VQE")) return -1;
    regs r = {{0, (uint64_t)(uintptr_t)&z_h, (uint64_t)(uintptr_t)&ry_a}, {0}};
    CLEAN();
    PACKET(0x02200000, &r);
    PRESERVED();
    REQUIRE(r.x[0] == 1, "QVLOAD: W0 = %llu", (unsigned long long)r.x[0]);
    uint64_t h = r.x[3];
    double theta[3] = {1.0471975511965976, 0, 3.14159265358979323846}, e[3];
    regs q = {{0, (uint64_t)(uintptr_t)theta, 4000, h}, {0}};
    CLEAN();
    PACKET(0x02210000, &q);
    PRESERVED();
    REQUIRE(q.x[0] == 1, "QEXPECT: W0 = %llu", (unsigned long long)q.x[0]);
    REQUIRE(q.d[1] >= 0 && fabs(q.d[0] - 0.75) <= 5 * q.d[1] + 1e-6, "QEXPECT: E = %g +- %g, exactly 0.75", q.d[0], q.d[1]);
    double *eg = guarded(24);
    regs n = {{0, (uint64_t)(uintptr_t)theta, 3, h, (uint64_t)(uintptr_t)eg}, {0}};
    CLEAN();
    PACKET(0x02220000, &n);
    PRESERVED();
    REQUIRE(n.x[0] == 1 && guards_ok(24), "QEXPECTN: W0 = %llu", (unsigned long long)n.x[0]);
    memcpy(e, eg, sizeof e);
    double err = q.d[1] > 0 ? 0.1 : 1e-6;
    REQUIRE(fabs(e[0] - 0.75) < err && fabs(e[1] - 1.25) < err && fabs(e[2] + 0.75) < err,
            "QEXPECTN: %g %g %g, exactly 0.75 1.25 -0.75", e[0], e[1], e[2]);
    double *g = guarded(8);
    regs gr = {{0, (uint64_t)(uintptr_t)theta, 0, h, (uint64_t)(uintptr_t)g}, {0}};
    CLEAN();
    PACKET(0x02230000, &gr);
    PRESERVED();
    REQUIRE(gr.x[0] == 1 && guards_ok(8), "QGRAD: W0 = %llu", (unsigned long long)gr.x[0]);
    REQUIRE(fabs(gr.d[0] - 0.75) < err && fabs(*g + 0.8660254037844386) < 10 * err,
            "QGRAD: E = %g, dE = %g; exactly 0.75, -0.866", gr.d[0], *g);
    if (features & QARM_F_VSAMPLE) {
        uint64_t *s = guarded(8 * 800);
        regs v = {{0, (uint64_t)(uintptr_t)theta, 800, h, (uint64_t)(uintptr_t)s}, {0}};
        CLEAN();
        PACKET(0x02250000, &v);
        PRESERVED();
        REQUIRE(v.x[0] == 1 && guards_ok(8 * 800), "QVSAMPLE: W0 = %llu", (unsigned long long)v.x[0]);
        int ones = 0;
        for (int i = 0; i < 800; i++) {
            REQUIRE(s[i] <= 1, "QVSAMPLE: sample %#llx on 1 qubit", (unsigned long long)s[i]);
            ones += (int)s[i];
        }
        REQUIRE(ones > 140 && ones < 260, "QVSAMPLE: %d ones of 800, P(1) = 0.25", ones);
    }
    regs ss = {{0, 1}, {0}};
    CLEAN();
    PACKET(0x02240000, &ss);
    PRESERVED();
    REQUIRE(ss.x[0] == 1, "QSESSION: W0 = %llu", (unsigned long long)ss.x[0]);
    ss = (regs){{0, 0}, {0}};
    PACKET(0x02240000, &ss);
    regs f = {{0, 0, 0, h}, {0}};
    CLEAN();
    PACKET(0x02260000, &f);
    PRESERVED();
    REQUIRE(f.x[0] == 1, "QVFREE: W0 = %llu", (unsigned long long)f.x[0]);
    regs b = {{0, (uint64_t)(uintptr_t)&z_h, (uint64_t)(uintptr_t)&bad_a}, {0}};
    CLEAN();
    PACKET(0x02200000, &b);
    PRESERVED();
    REQUIRE(b.x[0] == 2, "QVLOAD of an operation on qubit 3 of 1: W0 = %llu, must be 2", (unsigned long long)b.x[0]);
    return 1;
}

/* QSUBMIT, QPOLL, QWAIT: QEXPECT and QSEARCH as requests; a request for QSUBMIT itself is refused */
static int t_async(void)
{
    if (!have(QARM_F_ASYNC, "QSUBMIT")) return -1;
    static qarm_request rq[3];
    regs r = {{0, (uint64_t)(uintptr_t)&z_h, (uint64_t)(uintptr_t)&ry_a}, {0}};
    PACKET(0x02200000, &r);
    REQUIRE(r.x[0] == 1, "QVLOAD: W0 = %llu", (unsigned long long)r.x[0]);
    static const double theta[1] = {1.0471975511965976};
    memset(rq, 0, sizeof rq);
    rq[0].opcode = 0x21; rq[0].x[1] = (uint64_t)(uintptr_t)theta; rq[0].x[3] = r.x[3];
    int search = (features & QARM_F_SEARCH) != 0;
    rq[1].opcode = 0x11; rq[1].x[1] = (uint64_t)(uintptr_t)is_eleven; rq[1].x[2] = 4;
    rq[2].opcode = 0x12;
    for (int i = 0; i < 2 + search; i++) {
        int k = i == 2 ? 1 : i == 1 ? 2 : 0;                     /* QEXPECT, QSUBMIT (refused), QSEARCH */
        regs s = {{0, (uint64_t)(uintptr_t)&rq[k]}, {0}};
        CLEAN();
        PACKET(0x02120000, &s);
        PRESERVED();
        if (k == 2) { REQUIRE(s.x[0] == 2, "QSUBMIT of QSUBMIT: W0 = %llu, must be 2", (unsigned long long)s.x[0]); continue; }
        REQUIRE(s.x[0] == 1, "QSUBMIT: W0 = %llu", (unsigned long long)s.x[0]);
        for (int polls = 0;; polls++) {
            regs p = {{0, (uint64_t)(uintptr_t)&rq[k]}, {0}};
            CLEAN();
            PACKET(0x02130000, &p);
            PRESERVED();
            REQUIRE(p.x[0] == 1 && p.x[1] <= 1, "QPOLL: W0 = %llu, W1 = %llu", (unsigned long long)p.x[0],
                    (unsigned long long)p.x[1]);
            if (p.x[1] == 1 || polls > 100) break;
        }
        regs w = {{0, (uint64_t)(uintptr_t)&rq[k]}, {0}};
        PACKET(0x02140000, &w);
        REQUIRE(w.x[0] == 1 && rq[k].state == 1, "QWAIT: W0 = %llu, state %u", (unsigned long long)w.x[0], rq[k].state);
    }
    REQUIRE(rq[0].x[0] == 1 && fabs(rq[0].d[0] - 0.75) <= 5 * rq[0].d[1] + 1e-6, "QEXPECT as a request: W0 = %llu, E = %g",
            (unsigned long long)rq[0].x[0], rq[0].d[0]);
    REQUIRE(!search || (rq[1].x[0] == 2 || (rq[1].x[0] == 1 && rq[1].x[3] == 11)), "QSEARCH as a request: W0 = %llu, W3 = %llu",
            (unsigned long long)rq[1].x[0], (unsigned long long)rq[1].x[3]);
    regs f = {{0, 0, 0, r.x[3]}, {0}};
    PACKET(0x02260000, &f);
    return 1;
}

/* ---------------------------------------------------------------- QEVOLVE (1.1) */
/* the reference: exp(-iHt)|init> on at most 4 qubits by small Taylor steps in complex doubles, independent of any
 * runtime; accurate to about 1e-13 for the cases below */
#define RQ 16
static void ref_apply(const qarm_hamiltonian *h, const double *re, const double *im, double *ore, double *oim, int n)
{
    for (int b = 0; b < (1 << n); b++) ore[b] = oim[b] = 0;
    for (uint32_t j = 0; j < h->terms; j++) {
        const qarm_pauli *p = &h->t[j];
        int k = __builtin_popcountll(p->x & p->z) & 3;
        for (int b = 0; b < (1 << n); b++) {
            double sg = __builtin_parityll((uint64_t)b & p->z) ? -p->c : p->c, vr = sg * re[b], vi = sg * im[b];
            int u = b ^ (int)p->x;                    /* i^k s(b) psi[b] lands on u */
            double wr = k == 0 ? vr : k == 1 ? -vi : k == 2 ? -vr : vi, wi = k == 0 ? vi : k == 1 ? vr : k == 2 ? -vi : -vr;
            ore[u] += wr; oim[u] += wi;
        }
    }
    for (int b = 0; b < (1 << n); b++) { ore[b] += h->constant * re[b]; oim[b] += h->constant * im[b]; }
}
static double ref_value(const qarm_hamiltonian *h, const qarm_hamiltonian *o, uint64_t init, double t)
{
    int n = (int)h->qubits;
    double re[RQ] = {0}, im[RQ] = {0}, tr[RQ], ti[RQ], ar[RQ], ai[RQ], hr[RQ], hi[RQ];
    re[init] = 1;
    int m = 2000;
    double dt = t / m;
    for (int s = 0; s < m; s++) {
        memcpy(tr, re, sizeof tr); memcpy(ti, im, sizeof ti); memcpy(ar, re, sizeof ar); memcpy(ai, im, sizeof ai);
        for (int j = 1; j <= 20; j++) {
            ref_apply(h, tr, ti, hr, hi, n);
            for (int b = 0; b < (1 << n); b++) { tr[b] = dt / j * hi[b]; ti[b] = -dt / j * hr[b]; ar[b] += tr[b]; ai[b] += ti[b]; }
        }
        memcpy(re, ar, sizeof re); memcpy(im, ai, sizeof im);
    }
    ref_apply(o, re, im, hr, hi, n);
    double v = 0;
    for (int b = 0; b < (1 << n); b++) v += re[b] * hr[b] + im[b] * hi[b];
    return v;
}

static const qarm_pauli xz_t[2] = {{1, 0, 1.0}, {0, 1, 1.0}}, zq_t[1] = {{0, 1, 1.0}};
static const qarm_hamiltonian xz_h = {1, 2, 0, xz_t}, z1_h = {1, 1, 0, zq_t};
static const qarm_pauli ising_t[3] = {{0, 3, 0.8}, {1, 0, 0.5}, {2, 0, 0.5}}, mag_t[2] = {{0, 1, 1.0}, {2, 0, 0.25}};
static const qarm_hamiltonian ising_h = {2, 3, 0.3, ising_t}, mag_h = {2, 2, 1.0, mag_t};
static const qarm_pauli bad_t[1] = {{4, 0, 1.0}};                         /* X on qubit 2 of 2 */
static const qarm_hamiltonian bad_h = {2, 1, 0, bad_t}, three_h = {3, 3, 0, ising_t};

/* one QEVOLVE: W0, and the values within D0 + 5 D1 of the reference (-1: refused, 0: wrong) */
static int evolve(qarm_evolution *e, double *ref, regs *out, int expect_method)
{
    double *v = guarded(8 * e->k);
    regs r = {{0, (uint64_t)(uintptr_t)e, (uint64_t)(uintptr_t)v}, {0}};
    CLEAN();
    PACKET(0x02270000, &r);
    PRESERVED();
    *out = r;
    if (r.x[0] == 2) return -1;
    REQUIRE(r.x[0] == 1, "W0 = %llu", (unsigned long long)r.x[0]);
    REQUIRE(guards_ok(8 * e->k), "wrote outside the k values");
    uint32_t m = (uint32_t)r.x[3] & 0xff, ord = (uint32_t)r.x[3] >> 8 & 0xff;
    REQUIRE(m >= 1 && m <= 3 && (m == 2 ? ord == 1 || ord == 2 || ord == 4 || ord == 6 : ord == 0),
            "W3 = %#llx: method %u, order %u", (unsigned long long)r.x[3], m, ord);
    REQUIRE(!expect_method || m == (uint32_t)expect_method, "method %u, asked for %d", m, expect_method);
    REQUIRE(r.d[0] >= 0 && r.d[1] >= 0, "D0 = %g, D1 = %g", r.d[0], r.d[1]);
    for (uint32_t i = 0; i < e->k; i++)
        REQUIRE(fabs(v[i] - ref[i]) <= r.d[0] + 5 * r.d[1] + 1e-9, "t = %g: %.9g, exactly %.9g; bound %g, error %g",
                e->t[i], v[i], ref[i], r.d[0], r.d[1]);
    return 1;
}

static int t_qevolve(void)
{
    if (!have(QARM_F_EVOLVE, "QEVOLVE")) return -1;
    static const double t1[5] = {0, 0.3, -0.7, 1.1, 2.0}, t2[4] = {0.4, 1.5, -1.0, 2.5};
    double ref1[5], ref2[4], refz[4];
    regs r;
    for (int i = 0; i < 5; i++) ref1[i] = 0.5 + 0.5 * cos(2 * sqrt(2.0) * t1[i]);    /* <Z(t)>, H = X + Z, |0> */
    for (int i = 0; i < 4; i++) { ref2[i] = ref_value(&ising_h, &mag_h, 1, t2[i]); refz[i] = 1; }
    /* the runtime's choice, eps asked for: D0 <= eps */
    qarm_evolution a = {&xz_h, &z1_h, t1, 5, QARM_EV_AUTO, 0, 4000, 0, 1e-4, 0, 0};
    int s = evolve(&a, ref1, &r, 0);
    if (s <= 0) { if (s < 0) snprintf(why, sizeof why, "the runtime's choice: refused"); return 0; }
    REQUIRE(r.d[0] <= 1e-4, "the runtime's choice: D0 = %g, eps 1e-4", r.d[0]);
    /* a product formula on two qubits, the order the runtime's */
    qarm_evolution b = {&ising_h, &mag_h, t2, 4, QARM_EV_PRODUCT, 0, 4000, 1, 1e-3, 0, 0};
    if ((s = evolve(&b, ref2, &r, QARM_EV_PRODUCT)) <= 0) {
        if (s < 0) snprintf(why, sizeof why, "a product formula with eps 1e-3: refused");
        return 0;
    }
    REQUIRE(r.d[0] <= 1e-3, "a product formula: D0 = %g, eps 1e-3", r.d[0]);
    /* exactly the steps and the order asked for, with the bound that comes with them */
    qarm_evolution c = {&ising_h, &mag_h, t2, 4, QARM_EV_PRODUCT, 1, 4000, 1, 0, 3, 0};
    if ((s = evolve(&c, ref2, &r, QARM_EV_PRODUCT)) <= 0) {
        if (s < 0) snprintf(why, sizeof why, "Lie-Trotter with 3 steps: refused");
        return 0;
    }
    REQUIRE(r.x[3] == (2 | 1 << 8) && r.x[4] == 3, "Lie-Trotter, 3 steps: W3 = %#llx, X4 = %llu",
            (unsigned long long)r.x[3], (unsigned long long)r.x[4]);
    /* exact and qDRIFT: optional (a measured backend refuses them), right when done */
    qarm_evolution d = {&ising_h, &mag_h, t2, 4, QARM_EV_EXACT, 0, 0, 1, 0, 0, 0};
    if (!evolve(&d, ref2, &r, QARM_EV_EXACT)) return 0;                        /* why says what was wrong */
    qarm_evolution q = {&ising_h, &mag_h, t2, 4, QARM_EV_QDRIFT, 0, 400, 1, 0, 400, 0};
    if (!evolve(&q, ref2, &r, QARM_EV_QDRIFT)) return 0;
    /* the output may be the times: all inputs are read first */
    static double tt[4];
    memcpy(tt, t2, sizeof tt);
    qarm_evolution o = {&ising_h, &mag_h, tt, 4, QARM_EV_PRODUCT, 2, 4000, 1, 1e-3, 0, 0};
    regs ro = {{0, (uint64_t)(uintptr_t)&o, (uint64_t)(uintptr_t)tt}, {0}};
    CLEAN();
    PACKET(0x02270000, &ro);
    PRESERVED();
    REQUIRE(ro.x[0] == 1, "out = t: W0 = %llu", (unsigned long long)ro.x[0]);
    for (int i = 0; i < 4; i++)
        REQUIRE(fabs(tt[i] - ref2[i]) <= ro.d[0] + 5 * ro.d[1] + 1e-9, "out = t: t = %g gave %g, exactly %g", t2[i],
                tt[i], ref2[i]);
    /* H = Z: <Z> stays 1 for every method, every t */
    qarm_evolution zz = {&z1_h, &z1_h, t2, 4, QARM_EV_AUTO, 0, 0, 0, 0, 0, 0};
    if ((s = evolve(&zz, refz, &r, 0)) <= 0) {
        if (s < 0) snprintf(why, sizeof why, "H = Z, O = Z: refused");
        return 0;
    }
    /* 5.7: what MUST be refused */
    static const double nan_t[1] = {NAN};
    struct { qarm_evolution e; const char *what; } bad[] = {
        {{&ising_h, &mag_h, t2, 0, 0, 0, 0, 0, 0, 0, 0}, "k = 0"},
        {{&ising_h, &mag_h, t2, 5000, 0, 0, 0, 0, 0, 0, 0}, "k = 5000"},
        {{&three_h, &mag_h, t2, 1, 0, 0, 0, 0, 0, 0, 0}, "3 qubits in H, 2 in O"},
        {{&bad_h, &mag_h, t2, 1, 0, 0, 0, 0, 0, 0, 0}, "a term on qubit 2 of 2"},
        {{&ising_h, &mag_h, t2, 1, 0, 0, 0, 4, 0, 0, 0}, "init 4 on 2 qubits"},
        {{&ising_h, &mag_h, t2, 1, 4, 0, 0, 0, 0, 0, 0}, "method 4"},
        {{&ising_h, &mag_h, t2, 1, QARM_EV_PRODUCT, 3, 0, 0, 0, 0, 0}, "order 3"},
        {{&ising_h, &mag_h, t2, 1, QARM_EV_EXACT, 2, 0, 0, 0, 0, 0}, "an order for exact"},
        {{&ising_h, &mag_h, t2, 1, 0, 0, 0, 0, -1, 0, 0}, "eps = -1"},
        {{&ising_h, &mag_h, t2, 1, 0, 0, 0, 0, 0, 0, 1}, "reserved = 1"},
        {{&ising_h, &mag_h, nan_t, 1, 0, 0, 0, 0, 0, 0, 0}, "a NaN time"},
        {{&ising_h, 0, t2, 1, 0, 0, 0, 0, 0, 0, 0}, "no observable"},
    };
    for (size_t i = 0; i < sizeof bad / sizeof bad[0]; i++) {
        regs rb = {{0, (uint64_t)(uintptr_t)&bad[i].e, (uint64_t)(uintptr_t)guarded(8)}, {0}};
        CLEAN();
        PACKET(0x02270000, &rb);
        PRESERVED();
        REQUIRE(rb.x[0] == 2, "%s: W0 = %llu, must be 2", bad[i].what, (unsigned long long)rb.x[0]);
    }
    /* an accuracy no product formula reaches: refused, never a weaker answer */
    qarm_evolution hard = {&ising_h, &mag_h, t2, 4, QARM_EV_PRODUCT, 1, 0, 1, 1e-300, 0, 0};
    s = evolve(&hard, ref2, &r, QARM_EV_PRODUCT);
    REQUIRE(s == -1 || (s == 1 && r.d[0] <= 1e-300), "eps 1e-300 by Lie-Trotter: W0 = %llu, D0 = %g",
            (unsigned long long)r.x[0], r.d[0]);
    return 1;
}

int main(void)
{
    printf("Q-ARM ISA %d.%d conformance (docs/qarm_isa.md, section 9)\n", QARM_ISA_VERSION_MAJOR,
           QARM_ISA_VERSION_MINOR);
    int rt = t_qavail();
    result("QAVAIL", rt);
    if (rt != 1) {
        printf("conformance: %d passed, %d failed, %d skipped (no runtime)\n", passed, failed, skipped);
        return failed;
    }
    result("unassigned opcode", unknown_opcode());
    result("QCAPS", t_qcaps());
    current = P_ORACLE;
    result("QSEARCH", t_qsearch());
    result("QMIN", t_qmin());
    result("QCOUNT", t_qcount());
    result("QAMPEST", t_qampest());
    result("QPERIOD", t_structure(QARM_F_PERIOD, "QPERIOD", 0x18));
    result("QSIMON", t_structure(QARM_F_SIMON, "QSIMON", 0x19));
    result("QBV", t_structure(QARM_F_BV, "QBV", 0x1a));
    result("QCOLLISION", t_qcollision());
    current = P_CIRCUIT;
    result("QCIRC", t_qcirc());
    result("QRAND", t_qrand());
    current = P_VARIATIONAL;
    result("VQE (QVLOAD QEXPECT QEXPECTN QGRAD QVSAMPLE QSESSION QVFREE)", t_vqe());
    current = P_ASYNC;
    result("QSUBMIT QPOLL QWAIT", t_async());
    current = P_DYNAMICS;
    result("QEVOLVE", t_qevolve());
    printf("profiles:");
    for (int p = 0; p < NPROFILES; p++)
        printf(" %s %s%s", profile_name[p],
               prof_fail[p] ? "FAILED" : !prof_pass[p] ? "not claimed" : prof_skip[p] ? "partly claimed" : "passed",
               p + 1 < NPROFILES ? ";" : "\n");
    printf("conformance: %d passed, %d failed, %d skipped (features %#x)\n", passed, failed, skipped, features);
    return failed;
}
