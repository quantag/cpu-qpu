/* SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 Quantag IT Solutions GmbH */
/* qcpu.h: mark functions that run on the quantum CPU (see docs/qcpu_spec.md).
 *
 *     #include <qcpu.h>
 *     __qcpu__ int score(int guess);                       runs on the quantum CPU
 *     __qcpu__ int sum(const int *QCPU_IN(n) v, int n);     buffers: QCPU_IN / QCPU_OUT / QCPU_INOUT
 *
 * Build with qcpucc. Compiled by a plain C compiler, the markers expand to nothing and the program is an
 * ordinary C program. After a call, qcpu_last_call() describes how it ran and whether it is exact. */
#ifndef QCPU_H
#define QCPU_H

#define __qcpu__
#define QCPU_IN(count)
#define QCPU_OUT(count)
#define QCPU_INOUT(count)
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
    long cycles;                 /* machine cycles of the quantum CPU run (0 when not run) */
    long long qubits;            /* qubits of the whole circuit of this call (machine + one history register per cycle) */
    long long gates;             /* gates of the whole circuit of this call */
    char message[256];           /* why the call failed or is not confirmed */
} qcpu_call_info;

#ifdef __cplusplus
extern "C" {
#endif
/* Information about the last __qcpu__ call of this thread's program (calls are serialised). Returns its status. */
int qcpu_last_call(qcpu_call_info *info);
const char *qcpu_status_name(int status);
#ifdef __cplusplus
}
#endif
#endif
