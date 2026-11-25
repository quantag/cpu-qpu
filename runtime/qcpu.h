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
/* Find an x in 0 .. 2^bits - 1 with predicate(x) == 1; returns 1 and stores it in *x, or 0 if none was found.
 * predicate: a __qcpu__ function int f(int QCPU_BITS(bits) x) with QCPU_RESULT_BITS(1). Built with qcpucc
 * --qcpu-mode qoracle, this is Grover's search with the function's phase oracle (simulated); the answer is
 * checked with an ordinary call. Otherwise (QCPU_MODE=qcpu or native, or a plain compiler) every input is tried.
 * qcpu_last_call() then reports mode "grover" or "classical", and the oracle calls in info.cycles. */
int qcpu_search(int (*predicate)(int), int bits, int *x);
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
static inline int qcpu_search(int (*predicate)(int), int bits, int *x) {
    for (long v = 0; bits > 0 && bits <= 24 && v < (1L << bits); v++)
        if (predicate((int)v)) { *x = (int)v; return 1; }
    return 0;
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
