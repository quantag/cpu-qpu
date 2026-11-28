/* SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 Quantag IT Solutions GmbH
 *
 * The Q-ARM instruction set, version 1.1 (docs/qarm_isa.md): its constants and data layouts, and nothing else.
 * A runtime, a library or a tool includes this header to speak the ISA; include/qarm.h (the library) includes it.
 * Every value here is normative: the section of the specification is named beside it. C99; LP64, little-endian. */
#ifndef QARM_ISA_H
#define QARM_ISA_H
#include <stdint.h>

/* ---------------------------------------------------------------- 10. Versions */
#define QARM_ISA_VERSION_MAJOR 1
#define QARM_ISA_VERSION_MINOR 1
#define QARM_ISA_PACKET_FORMAT 2                 /* the V of word 2 */
#define QARM_ISA_CAPS_VERSION 1                  /* qarm_capabilities.version */

/* ---------------------------------------------------------------- 2. The packet: eight 32-bit words */
#define QARM_ISA_A64_BRANCH 0x14000008u          /* word 0 on AArch64: B .+32 */
#define QARM_ISA_X64_JUMP 0x00001eebu            /* word 0 on x86-64: bytes eb 1e 00 00, JMP .+32 (2.1) */
#define QARM_ISA_MAGIC 0x4d524151u               /* word 1: "QARM" */
#define QARM_ISA_WORD2(op) ((uint32_t)QARM_ISA_PACKET_FORMAT << 24 | (uint32_t)(op) << 16)   /* words 3-7: 0 */

/* ---------------------------------------------------------------- 6. Opcodes */
enum {
    QARM_OPC_QAVAIL = 0x10,                      /* 6.1 */
    QARM_OPC_QSEARCH = 0x11,                     /* 6.2 */
    QARM_OPC_QSUBMIT = 0x12, QARM_OPC_QPOLL = 0x13, QARM_OPC_QWAIT = 0x14,   /* 6.5 */
    QARM_OPC_QMIN = 0x15, QARM_OPC_QCOUNT = 0x16, QARM_OPC_QAMPEST = 0x17, QARM_OPC_QPERIOD = 0x18,
    QARM_OPC_QSIMON = 0x19, QARM_OPC_QBV = 0x1A, QARM_OPC_QCOLLISION = 0x1B,  /* 6.2 */
    QARM_OPC_QRAND = 0x1C,                       /* 6.4 */
    QARM_OPC_QVLOAD = 0x20, QARM_OPC_QEXPECT = 0x21, QARM_OPC_QEXPECTN = 0x22, QARM_OPC_QGRAD = 0x23,
    QARM_OPC_QSESSION = 0x24, QARM_OPC_QVSAMPLE = 0x25, QARM_OPC_QVFREE = 0x26,   /* 6.3 */
    QARM_OPC_QEVOLVE = 0x27,                     /* 6.6 (1.1) */
    QARM_OPC_QCIRC = 0x30, QARM_OPC_QCAPS = 0x31 /* 6.4, 6.1 */
};

/* ---------------------------------------------------------------- 3. W0: the status */
enum { QARM_ST_NOT_EXECUTED = 0, QARM_ST_DONE = 1, QARM_ST_NEGATIVE = 2, QARM_ST_SIMULATED = 3 };

/* ---------------------------------------------------------------- 4. Feature bits (QAVAIL's W1) */
enum { QARM_F_SEARCH = 1, QARM_F_VQE = 2, QARM_F_MIN = 4, QARM_F_COUNT = 8, QARM_F_PERIOD = 16, QARM_F_SIMON = 32,
       QARM_F_BV = 64, QARM_F_AMPEST = 128, QARM_F_CIRC = 256, QARM_F_RAND = 512, QARM_F_CAPS = 1024,
       QARM_F_VSAMPLE = 2048, QARM_F_ASYNC = 4096, QARM_F_COLLISION = 8192, QARM_F_EVOLVE = 16384 /* 1.1 */ };
#define QARM_F_ALL_1_0 0x3fffu                   /* every bit of 1.0 */
#define QARM_F_ALL_1_1 0x7fffu                   /* every bit of 1.1; bits 15-31 are reserved */

/* 8. Profiles */
#define QARM_PROFILE_CORE QARM_F_CAPS
#define QARM_PROFILE_ORACLE (QARM_F_SEARCH | QARM_F_MIN | QARM_F_COUNT | QARM_F_AMPEST | QARM_F_PERIOD | \
                             QARM_F_SIMON | QARM_F_BV | QARM_F_COLLISION)
#define QARM_PROFILE_VARIATIONAL (QARM_F_VQE | QARM_F_VSAMPLE)
#define QARM_PROFILE_CIRCUIT (QARM_F_CIRC | QARM_F_RAND)
#define QARM_PROFILE_ASYNC QARM_F_ASYNC
#define QARM_PROFILE_DYNAMICS QARM_F_EVOLVE        /* 1.1 */

/* ---------------------------------------------------------------- 5.2 Pauli operators and Hamiltonians */
/* P(x, z) = i^popcount(x & z) X^x Z^z: on qubit q, I X Z Y for (x_q, z_q) = 00 10 01 11; H = constant + sum c P */
typedef struct { uint64_t x, z; double c; } qarm_pauli;                                            /* 24 bytes */
typedef struct { uint32_t qubits, terms; double constant; const qarm_pauli *t; } qarm_hamiltonian;  /* 24 */

/* ---------------------------------------------------------------- 5.3 Ansatz tables */
/* |init>, then the operations in order; RY, RZ: exp(-i a G/2), a Pauli rotation: exp(-i a P), a = scale theta[param] */
enum { QARM_X = 1, QARM_H, QARM_CX, QARM_CZ, QARM_RY, QARM_RZ, QARM_PAULI_ROT };
typedef struct { uint8_t kind, q0, q1, param; uint32_t reserved; double scale; uint64_t x, z; } qarm_op;   /* 32 */
typedef struct { uint32_t qubits, params, ops, reserved; uint64_t init; const qarm_op *op; } qarm_ansatz; /* 32 */

/* ---------------------------------------------------------------- 5.4 Circuit tables */
/* from |0..0>, every qubit measured at the end; CX, CZ: q0 control; CCX: q0, q1 controls, q2 target */
enum { QARM_G_X = 1, QARM_G_Y, QARM_G_Z, QARM_G_H, QARM_G_S, QARM_G_SDG, QARM_G_T, QARM_G_TDG, QARM_G_SX, QARM_G_RX,
       QARM_G_RY, QARM_G_RZ, QARM_G_CX, QARM_G_CZ, QARM_G_SWAP, QARM_G_CCX, QARM_G_LAST = QARM_G_CCX };
typedef struct { uint8_t kind, q0, q1, q2; uint32_t reserved; double angle; } qarm_gate;            /* 16 */
typedef struct { uint32_t qubits, gates; const qarm_gate *g; } qarm_circuit;                         /* 16 */

/* ---------------------------------------------------------------- 5.7 Time evolution (1.1) */
/* <init| exp(iHt) O exp(-iHt) |init> for k times t; the method: the runtime's choice, exact, a product formula of
 * order 1, 2, 4 or 6 (Lie-Trotter, Strang, Suzuki), or qDRIFT */
enum { QARM_EV_AUTO = 0, QARM_EV_EXACT, QARM_EV_PRODUCT, QARM_EV_QDRIFT };
typedef struct {
    const qarm_hamiltonian *h;                   /* H, the generator */
    const qarm_hamiltonian *o;                   /* O, the observable; o->qubits == h->qubits */
    const double *t;                             /* the k times */
    uint32_t k, method;                          /* 1 <= k <= 4096; QARM_EV_* */
    uint32_t order, shots;                       /* PRODUCT: 1, 2, 4, 6 or 0 (the runtime's); otherwise 0. shots: per
                                                    time on a measured backend, sequences for qDRIFT; 0: default */
    uint64_t init;                               /* the initial basis state */
    double eps;                                  /* the accuracy asked for (D0 <= eps); 0: 1e-3 */
    uint32_t steps, reserved;                    /* 0: the fewest that meet eps; otherwise exactly these (eps unused) */
} qarm_evolution;                                                                                    /* 64 */

/* ---------------------------------------------------------------- 5.6 Capabilities */
enum { QARM_CAPS_HARDWARE = 1, QARM_CAPS_MEASURED = 2, QARM_CAPS_ASYNC = 4 };
typedef struct { uint32_t version, features, qubits, flags; char backend[48]; } qarm_capabilities;   /* 64 */

/* ---------------------------------------------------------------- 6.5 Asynchronous execution */
typedef struct { uint32_t opcode, state; uint64_t x[6]; double d[2]; uint64_t ticket; } qarm_request;  /* 80 */

/* the layouts' sizes on 64-bit targets (the ISA's): a compile-time check in C99 */
#if UINTPTR_MAX == 0xFFFFFFFFFFFFFFFFu
#define QARM_ISA_SIZE(t, n) typedef char qarm_isa_size_##t[(sizeof(t) == (n)) ? 1 : -1]
QARM_ISA_SIZE(qarm_pauli, 24);
QARM_ISA_SIZE(qarm_hamiltonian, 24);
QARM_ISA_SIZE(qarm_op, 32);
QARM_ISA_SIZE(qarm_ansatz, 32);
QARM_ISA_SIZE(qarm_gate, 16);
QARM_ISA_SIZE(qarm_circuit, 16);
QARM_ISA_SIZE(qarm_capabilities, 64);
QARM_ISA_SIZE(qarm_evolution, 64);
QARM_ISA_SIZE(qarm_request, 80);
#undef QARM_ISA_SIZE
#endif

#endif
