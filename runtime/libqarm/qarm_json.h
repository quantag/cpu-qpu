/* SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 Quantag IT Solutions GmbH
 *
 * A small JSON reader and writer for libqarm: config.json and IBM Quantum's REST API. */
#ifndef QARM_JSON_H
#define QARM_JSON_H
#include <stddef.h>

typedef enum { QJ_NULL, QJ_BOOL, QJ_NUM, QJ_STR, QJ_ARR, QJ_OBJ } qj_type;

typedef struct qj {
    qj_type type;
    double num;                 /* QJ_NUM, QJ_BOOL (0/1) */
    char *str;                  /* QJ_STR; the key, for a member of an object */
    char *key;
    struct qj *child, *next;    /* QJ_ARR, QJ_OBJ: the first element; the next sibling */
} qj;

qj *qj_parse(const char *text, char *err, size_t errlen);       /* NULL on an error, described in err */
void qj_free(qj *v);
const qj *qj_get(const qj *obj, const char *key);              /* a member, or NULL */
const qj *qj_at(const qj *arr, int i);
int qj_len(const qj *arr);
const char *qj_str(const qj *v, const char *dflt);             /* a string member's value, or dflt */
double qj_num(const qj *v, double dflt);
const qj *qj_path(const qj *v, const char *path);               /* "a.b.0.c" */

/* a growing string buffer, also for building JSON */
typedef struct { char *s; size_t n, cap; } qbuf;
void qb_add(qbuf *b, const char *s);
void qb_addn(qbuf *b, const char *s, size_t n);
void qb_printf(qbuf *b, const char *fmt, ...);
void qb_json_str(qbuf *b, const char *s);                        /* a quoted, escaped JSON string */
void qb_free(qbuf *b);
#endif
