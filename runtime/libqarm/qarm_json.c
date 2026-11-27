/* SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 Quantag IT Solutions GmbH */
#include "qarm_json.h"
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef struct { const char *p; char *err; size_t errlen; } qj_in;

static void ws(qj_in *in) { while (*in->p == ' ' || *in->p == '\t' || *in->p == '\n' || *in->p == '\r') in->p++; }

static qj *fail(qj_in *in, const char *what)
{
    if (in->err && !in->err[0])
        snprintf(in->err, in->errlen, "%s near \"%.20s\"", what, in->p);
    return NULL;
}

static qj *node(qj_type t) { qj *v = calloc(1, sizeof *v); v->type = t; return v; }

static void utf8(qbuf *b, unsigned c)
{
    char o[4];
    if (c < 0x80) { o[0] = (char)c; qb_addn(b, o, 1); }
    else if (c < 0x800) { o[0] = (char)(0xC0 | c >> 6); o[1] = (char)(0x80 | (c & 63)); qb_addn(b, o, 2); }
    else { o[0] = (char)(0xE0 | c >> 12); o[1] = (char)(0x80 | (c >> 6 & 63)); o[2] = (char)(0x80 | (c & 63)); qb_addn(b, o, 3); }
}

static char *string(qj_in *in)
{
    qbuf b = {0};
    in->p++;                                        /* the opening quote */
    while (*in->p && *in->p != '"') {
        if (*in->p == '\\') {
            in->p++;
            switch (*in->p) {
            case 'n': qb_add(&b, "\n"); break;
            case 't': qb_add(&b, "\t"); break;
            case 'r': qb_add(&b, "\r"); break;
            case 'b': qb_add(&b, "\b"); break;
            case 'f': qb_add(&b, "\f"); break;
            case 'u': {
                unsigned c = 0;
                for (int i = 1; i <= 4; i++) {
                    char h = in->p[i];
                    c = c * 16 + (unsigned)(h >= 'a' ? h - 'a' + 10 : h >= 'A' ? h - 'A' + 10 : h - '0');
                }
                utf8(&b, c);
                in->p += 4;
                break;
            }
            default: qb_addn(&b, in->p, 1);
            }
            in->p++;
        } else
            qb_addn(&b, in->p++, 1);
    }
    if (*in->p != '"') { qb_free(&b); return NULL; }
    in->p++;
    if (!b.s) qb_add(&b, "");
    return b.s;
}

static qj *value(qj_in *in, int depth)
{
    ws(in);
    if (depth > 64) return fail(in, "nested too deeply");
    char c = *in->p;
    if (c == '{' || c == '[') {
        qj *v = node(c == '{' ? QJ_OBJ : QJ_ARR), **tail = &v->child;
        in->p++;
        ws(in);
        if (*in->p == (c == '{' ? '}' : ']')) { in->p++; return v; }
        for (;;) {
            char *key = NULL;
            ws(in);
            if (c == '{') {
                if (*in->p != '"' || !(key = string(in))) { qj_free(v); return fail(in, "a key expected"); }
                ws(in);
                if (*in->p != ':') { free(key); qj_free(v); return fail(in, "':' expected"); }
                in->p++;
            }
            qj *e = value(in, depth + 1);
            if (!e) { free(key); qj_free(v); return NULL; }
            e->key = key;
            *tail = e;
            tail = &e->next;
            ws(in);
            if (*in->p == ',') { in->p++; continue; }
            if (*in->p == (c == '{' ? '}' : ']')) { in->p++; return v; }
            qj_free(v);
            return fail(in, "',' or a closing bracket expected");
        }
    }
    if (c == '"') {
        qj *v = node(QJ_STR);
        if (!(v->str = string(in))) { free(v); return fail(in, "an unterminated string"); }
        return v;
    }
    if (!strncmp(in->p, "true", 4)) { in->p += 4; qj *v = node(QJ_BOOL); v->num = 1; return v; }
    if (!strncmp(in->p, "false", 5)) { in->p += 5; return node(QJ_BOOL); }
    if (!strncmp(in->p, "null", 4)) { in->p += 4; return node(QJ_NULL); }
    if (c == '-' || (c >= '0' && c <= '9')) {
        char *end;
        qj *v = node(QJ_NUM);
        v->num = strtod(in->p, &end);
        in->p = end;
        return v;
    }
    return fail(in, "a value expected");
}

qj *qj_parse(const char *text, char *err, size_t errlen)
{
    qj_in in = {text, err, errlen};
    if (err && errlen) err[0] = 0;
    qj *v = value(&in, 0);
    if (!v) return NULL;
    ws(&in);
    if (*in.p) { qj_free(v); return fail(&in, "text after the value"); }
    return v;
}

void qj_free(qj *v)
{
    while (v) {
        qj *n = v->next;
        qj_free(v->child);
        free(v->str);
        free(v->key);
        free(v);
        v = n;
    }
}

const qj *qj_get(const qj *o, const char *key)
{
    if (!o || o->type != QJ_OBJ) return NULL;
    for (const qj *e = o->child; e; e = e->next)
        if (!strcmp(e->key, key)) return e;
    return NULL;
}

const qj *qj_at(const qj *a, int i)
{
    if (!a || (a->type != QJ_ARR && a->type != QJ_OBJ)) return NULL;
    const qj *e = a->child;
    while (e && i-- > 0) e = e->next;
    return e;
}

int qj_len(const qj *a)
{
    int n = 0;
    if (a && (a->type == QJ_ARR || a->type == QJ_OBJ))
        for (const qj *e = a->child; e; e = e->next) n++;
    return n;
}

const char *qj_str(const qj *v, const char *dflt) { return v && v->type == QJ_STR ? v->str : dflt; }
double qj_num(const qj *v, double dflt) { return v && (v->type == QJ_NUM || v->type == QJ_BOOL) ? v->num : dflt; }

const qj *qj_path(const qj *v, const char *path)
{
    char part[128];
    while (v && *path) {
        size_t n = strcspn(path, ".");
        if (n >= sizeof part) return NULL;
        memcpy(part, path, n);
        part[n] = 0;
        v = v->type == QJ_ARR ? qj_at(v, atoi(part)) : qj_get(v, part);
        path += n + (path[n] == '.');
    }
    return v;
}

void qb_addn(qbuf *b, const char *s, size_t n)
{
    if (b->n + n + 1 > b->cap) {
        b->cap = (b->n + n + 1) * 2;
        b->s = realloc(b->s, b->cap);
    }
    memcpy(b->s + b->n, s, n);
    b->n += n;
    b->s[b->n] = 0;
}

void qb_add(qbuf *b, const char *s) { qb_addn(b, s, strlen(s)); }

void qb_printf(qbuf *b, const char *fmt, ...)
{
    char small[512];
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(small, sizeof small, fmt, ap);
    va_end(ap);
    if (n < (int)sizeof small) { qb_addn(b, small, (size_t)n); return; }
    char *big = malloc((size_t)n + 1);
    va_start(ap, fmt);
    vsnprintf(big, (size_t)n + 1, fmt, ap);
    va_end(ap);
    qb_addn(b, big, (size_t)n);
    free(big);
}

void qb_json_str(qbuf *b, const char *s)
{
    qb_add(b, "\"");
    for (; *s; s++) {
        unsigned char c = (unsigned char)*s;
        if (c == '"' || c == '\\') { char e[2] = {'\\', (char)c}; qb_addn(b, e, 2); }
        else if (c == '\n') qb_add(b, "\\n");
        else if (c < 0x20) qb_printf(b, "\\u%04x", c);
        else qb_addn(b, (const char *)&c, 1);
    }
    qb_add(b, "\"");
}

void qb_free(qbuf *b) { free(b->s); b->s = NULL; b->n = b->cap = 0; }
