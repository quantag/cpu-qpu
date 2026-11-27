/* SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 Quantag IT Solutions GmbH
 *
 * libqarm's internals: one gate list feeds the state vector (exact, simulator) and the transpiler (ibm). */
#ifndef QARM_INTERNAL_H
#define QARM_INTERNAL_H
#include <stdint.h>
#include "qarm_json.h"

enum { G_X, G_H, G_S, G_SDG, G_SX, G_RY, G_RZ, G_CX, G_CZ };

typedef struct { int kind, q0, q1; double a; } qgate;
typedef struct { qgate *g; int n, cap, qubits; } qcirc;    /* every qubit q is measured at the end into bit q */

void qc_add(qcirc *c, int kind, int q0, int q1, double a);
void qc_free(qcirc *c);

/* a state vector of 2^n amplitudes; the probabilities of the basis states after the circuit */
int qsv_probabilities(const qcirc *c, double *p, char *err, size_t errlen);

void qarm_note(const char *fmt, ...);
double qarm_now(void);
void qarm_sleep(double seconds);

/* ibm: IBM Quantum's REST API (qarm_ibm.c) */
typedef struct qibm qibm;
qibm *qibm_open(const qj *opts, int qubits, char *err, size_t errlen);
void qibm_close(qibm *b);
const char *qibm_device(const qibm *b);
/* the circuit as an ISA OpenQASM 3 program for the device; final[q] = the chain position measured into bit q */
char *qibm_qasm(qibm *b, const qcirc *c, int *final, int *twoq);
int qibm_submit(qibm *b, char **qasm, int count, int shots, char *job_id, size_t idlen, char *err, size_t errlen);
int qibm_status(qibm *b, const char *job_id, char *err, size_t errlen);    /* 1 done, 0 not yet, -1 failed */
/* samples[i][s]: the bits of shot s of circuit i (bit q = classical bit q); returns shots, or -1 */
int qibm_results(qibm *b, const char *job_id, int count, uint64_t **samples, char *err, size_t errlen);
int qibm_session(qibm *b, int open, char *err, size_t errlen);              /* 1 open, 0 not available */
#endif
