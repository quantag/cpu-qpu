/* SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 Quantag IT Solutions GmbH
 *
 * libqarm: gate lists, the state vector, and small utilities. */
#if !defined(_WIN32) && !defined(_POSIX_C_SOURCE) && !defined(__APPLE__)
#define _POSIX_C_SOURCE 200809L
#endif
#include "qarm_internal.h"
#include <math.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#ifdef _WIN32
#include <windows.h>
#else
#endif

void qc_add(qcirc *c, int kind, int q0, int q1, double a)
{
    if (c->n == c->cap) {
        c->cap = c->cap ? 2 * c->cap : 64;
        c->g = realloc(c->g, sizeof *c->g * (size_t)c->cap);
    }
    c->g[c->n++] = (qgate){kind, q0, q1, a};
}

void qc_free(qcirc *c) { free(c->g); c->g = NULL; c->n = c->cap = 0; }

int qsv_probabilities(const qcirc *c, double *p, char *err, size_t errlen)
{
    int n = c->qubits;
    if (n > 24) { snprintf(err, errlen, "%d qubits: the state vector takes up to 24", n); return -1; }
    long N = 1L << n;
    double *re = calloc((size_t)N, sizeof *re), *im = calloc((size_t)N, sizeof *im);
    if (!re || !im) { free(re); free(im); snprintf(err, errlen, "out of memory for %d qubits", n); return -1; }
    re[0] = 1;
    const double r2 = 0.70710678118654752440;
    for (int i = 0; i < c->n; i++) {
        const qgate *g = &c->g[i];
        long m0 = 1L << g->q0, m1 = 1L << g->q1;
        double cs = cos(g->a / 2), sn = sin(g->a / 2), t, u;
        for (long b = 0; b < N; b++) {
            long o = b | m0;
            switch (g->kind) {
            case G_X:
                if (b & m0) break;
                t = re[b]; re[b] = re[o]; re[o] = t;
                t = im[b]; im[b] = im[o]; im[o] = t;
                break;
            case G_H:
                if (b & m0) break;
                t = re[b]; re[b] = r2 * (t + re[o]); re[o] = r2 * (t - re[o]);
                t = im[b]; im[b] = r2 * (t + im[o]); im[o] = r2 * (t - im[o]);
                break;
            case G_S:                                   /* |1> -> i|1> */
            case G_SDG:                                 /* |1> -> -i|1> */
                if (!(b & m0)) break;
                t = re[b];
                if (g->kind == G_S) { re[b] = -im[b]; im[b] = t; } else { re[b] = im[b]; im[b] = -t; }
                break;
            case G_SX: {                                /* (1/2) [[1+i, 1-i], [1-i, 1+i]] */
                if (b & m0) break;
                double ar = re[b], ai = im[b], br = re[o], bi = im[o];
                re[b] = 0.5 * ((ar - ai) + (br + bi)); im[b] = 0.5 * ((ar + ai) + (bi - br));
                re[o] = 0.5 * ((ar + ai) + (br - bi)); im[o] = 0.5 * ((ai - ar) + (br + bi));
                break;
            }
            case G_RY:
                if (b & m0) break;
                t = re[b]; u = re[o]; re[b] = cs * t - sn * u; re[o] = sn * t + cs * u;
                t = im[b]; u = im[o]; im[b] = cs * t - sn * u; im[o] = sn * t + cs * u;
                break;
            case G_RZ: {                                /* exp(-i a Z / 2) */
                double s2 = (b & m0) ? sn : -sn;
                t = re[b]; re[b] = cs * t - s2 * im[b]; im[b] = s2 * t + cs * im[b];
                break;
            }
            case G_CX:
                if (!(b & m0) || (b & m1)) break;
                o = b | m1;
                t = re[b]; re[b] = re[o]; re[o] = t;
                t = im[b]; im[b] = im[o]; im[o] = t;
                break;
            case G_CZ:
                if ((b & m0) && (b & m1)) { re[b] = -re[b]; im[b] = -im[b]; }
                break;
            }
        }
    }
    for (long b = 0; b < N; b++) p[b] = re[b] * re[b] + im[b] * im[b];
    free(re);
    free(im);
    return 0;
}

void qarm_note(const char *fmt, ...)
{
    const char *q = getenv("QARM_QUIET");
    if (q && *q && strcmp(q, "0")) return;
    va_list ap;
    va_start(ap, fmt);
    fputs("[qarm: ", stderr);
    vfprintf(stderr, fmt, ap);
    fputs("]\n", stderr);
    va_end(ap);
}

double qarm_now(void)
{
#ifdef _WIN32
    return (double)GetTickCount64() / 1000.0;
#else
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return (double)t.tv_sec + t.tv_nsec / 1e9;
#endif
}

void qarm_sleep(double seconds)
{
#ifdef _WIN32
    Sleep((DWORD)(seconds * 1000));
#else
    struct timespec t = {(time_t)seconds, (long)((seconds - (double)(time_t)seconds) * 1e9)};
    nanosleep(&t, NULL);
#endif
}
