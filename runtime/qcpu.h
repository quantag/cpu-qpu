/* SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 Quantag IT Solutions GmbH */
/* qcpu.h: mark functions that run on the quantum CPU (see docs/qcpu_spec.md).
 *
 *     #include <qcpu.h>
 *     __qcpu__ int score(int guess);                       runs on the quantum CPU
 *     __qcpu__ int sum(const int *QCPU_IN(n) v, int n);     buffers: QCPU_IN / QCPU_OUT / QCPU_INOUT
 *
 * Build with qcpucc. Compiled by a plain C compiler, the markers expand to nothing and the program is an
 * ordinary C program that runs everything on the CPU (qcpu_last_call() then reports mode "native"). After a call, qcpu_last_call() describes how it ran and whether it is exact. */
#ifndef QCPU_H
#define QCPU_H

#define __qcpu__
#define QCPU_IN(count)
#define QCPU_OUT(count)
#define QCPU_INOUT(count)
/* qcpucc --qcpu-mode qoracle: how many bits an argument and the result really use.
 * QCPU_BITS(n): 0 .. 2^n - 1; QCPU_SBITS(n): -2^(n-1) .. 2^(n-1) - 1; QCPU_RESULT_BITS(n): the result is
 * always 0 .. 2^n - 1 (the build proves it). Fewer bits make the qoracle circuit smaller. */
#define QCPU_BITS(n)
#define QCPU_SBITS(n)
#define QCPU_RESULT_BITS(n)
/* the names of the QPU-library tool (qpu_dll.py) */
#define QPU_IN(count)
#define QPU_OUT(count)
#define QPU_INOUT(count)

enum qcpu_status {
    QCPU_OK = 0,          /* exact: confirmed by the native build (E1), or run natively */
    QCPU_MISMATCH,        /* the quantum CPU and the native build differ */
    QCPU_INEXACT,         /* a value left the reduced-width model (per-instruction check) */
    QCPU_INCOMPLETE,      /* the cycle limit was reached */
    QCPU_TRAP,            /* the quantum CPU reached an instruction it does not implement */
    QCPU_TIE,             /* hardware: no clear majority */
    QCPU_INSUFFICIENT,    /* hardware: too few accepted shots */
    QCPU_UNVERIFIED,      /* QCPU_EXACT=1 and no guarantee applies (e.g. QCPU_VERIFY=0) */
    QCPU_BAD_ARGS,        /* overlapping buffers, or an argument outside its declared width */
    QCPU_PROBABLE,        /* QCPU_EXACT=0: hardware result that meets the evidence thresholds */
    QCPU_UNCHECKED        /* QCPU_EXACT=0: no guarantee applied */
};

typedef struct {
    int status;                  /* enum qcpu_status */
    const char *function;        /* name of the last __qcpu__ function called */
    const char *backend;         /* "sim" or "native" */
    const char *guarantee;       /* "E1", "native" or "none" */
    long cycles;                 /* machine cycles of the quantum CPU (0 when it did not run) */
    long long qubits;            /* qubits of the whole circuit of this call (machine + one history register per cycle) */
    long long gates;             /* gates of the whole circuit of this call */
    char message[256];           /* why the call failed or is not confirmed */
    const char *mode;            /* "qcpu" (the quantum CPU, a von Neumann machine of gates, ran the machine code),
                                    "qoracle" (a circuit of the function alone) or "native" (no circuit: the CPU) */
} qcpu_call_info;

#if defined(__QCPUCC__) || defined(QCPU_RUNTIME) || defined(QCPU_LIBRARY)
/* Built by qcpucc (which defines __QCPUCC__), inside the runtime, or by a program that links a QPU library
 * (define QCPU_LIBRARY): the runtime provides these. */
#ifdef __cplusplus
extern "C" {
#endif
/* Information about the last __qcpu__ call of this thread's program (calls are serialised). Returns its status. */
int qcpu_last_call(qcpu_call_info *info);
const char *qcpu_status_name(int status);
#ifdef __cplusplus
}
#endif
#else
/* Built by a plain C compiler (clang, gcc, cl, CMake's add_executable): there is no quantum CPU, every
 * __qcpu__ function ran natively. qcpu_last_call() says so: status QCPU_OK, mode "native", 0 cycles. */
static inline const char *qcpu_status_name(int s) {
    static const char *const names[] = {"QCPU_OK", "QCPU_MISMATCH", "QCPU_INEXACT", "QCPU_INCOMPLETE", "QCPU_TRAP",
        "QCPU_TIE", "QCPU_INSUFFICIENT", "QCPU_UNVERIFIED", "QCPU_BAD_ARGS", "QCPU_PROBABLE", "QCPU_UNCHECKED"};
    return s >= 0 && s < (int)(sizeof names / sizeof *names) ? names[s] : "QCPU_?";
}
static inline int qcpu_last_call(qcpu_call_info *info) {
    if (info) {
        qcpu_call_info z = {QCPU_OK, "", "none", "native", 0, 0, 0, "built without qcpucc: no quantum CPU", "native"};
        *info = z;
    }
    return QCPU_OK;
}
#endif
#endif
