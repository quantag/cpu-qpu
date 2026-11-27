/* SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 Quantag IT Solutions GmbH
 *
 * libqarm: the native Q-ARM runtime, in C. A program built with -DQARM_RUNTIME and linked with libqarm runs at
 * full speed on its own CPU; the VQE calls of include/qarm.h come here (docs/qarm_v2.md, section 7).
 *
 * Backends, from the config file named by QARM_CONFIG (config.json's format; without QARM_CONFIG none is read, so
 * no run spends QPU time unless asked); QARM_BACKEND overrides its "backend":
 *   exact       the default: a state vector, synchronous, standard error 0
 *   simulator   the same state vector, measured: shots sampled in each measurement basis
 *   ibm         IBM Quantum's REST API (libcurl): the circuits are transpiled here to the device's gates and
 *               qubits, sent as OpenQASM 3 in one Sampler job per batch, and read back asynchronously
 * Reports go to stderr ("[qarm: ...]"; QARM_QUIET=1: none). This header is included by qarm.h; include that. */
#ifndef QARM_RT_H
#define QARM_RT_H
#include <stddef.h>
#ifdef __cplusplus
extern "C" {
#endif

typedef struct qarm_rt qarm_rt;          /* a loaded problem: a Hamiltonian, an ansatz, a backend */
typedef struct qarm_job qarm_job;        /* a batch of energies, possibly still on the QPU */

int qarm_rt_load(const qarm_hamiltonian *h, const qarm_ansatz *a, qarm_rt **out);  /* 1 measured, 0 exact, -1 refused */
int qarm_rt_expect_n(qarm_rt *r, const double *thetas, int k, int shots, double *e, double *err);  /* 1, or -1 */
qarm_job *qarm_rt_submit(qarm_rt *r, const double *thetas, int k, int shots);       /* NULL: failed (stderr) */
int qarm_rt_poll(qarm_job *j);                                                     /* 1 done, 0 not yet, -1 failed */
int qarm_rt_wait(qarm_job *j, double *e, double *err);                             /* frees j: 1, or -1 */
int qarm_rt_grad(qarm_rt *r, const double *theta, int shots, double *e, double *grad);      /* 1, or -1 */
int qarm_rt_session(int open);
void qarm_rt_free(qarm_rt *r);
const char *qarm_rt_where(const qarm_rt *r);                                       /* "ibm: ibm_aachen", ... */

/* QSEARCH: a phase oracle as tools/qarm_oracle.py writes it; Grover simulated on the search register's amplitudes */
typedef struct qarm_oracle qarm_oracle;
qarm_oracle *qarm_rt_oracle_parse(const char *text, char *err, size_t errlen);
void qarm_rt_oracle_free(qarm_oracle *o);
const char *qarm_rt_oracle_name(const qarm_oracle *o);
int qarm_rt_oracle_qubits(const qarm_oracle *o);
int qarm_rt_oracle_gates(const qarm_oracle *o);
int qarm_rt_search(const qarm_oracle *o, int *x, char *report, size_t len);          /* 1 x found, 2 none, -1 */

#ifdef __cplusplus
}
#endif
#endif
