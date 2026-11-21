/* SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 Quantag IT Solutions GmbH */
/* Runtime of generated QPU libraries: sends calls to qpu_server.py (protocol in qpu_server.py). */
#include "qpu_runtime.h"
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#ifndef QPU_HOME_DEFAULT
#define QPU_HOME_DEFAULT "."
#endif

#define API __attribute__((visibility("default")))

static pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;
static int fd = -1;
static char rbuf[1 << 16];
static size_t rlen, rpos;
static int last_status;               /* 0 ok, 1 inexact, 2 failed */
static char last_message[1024];

/* ---- optional queries, exported by every generated library */
API int qpu_last_status(void) { return last_status; }
API const char *qpu_last_message(void) { return last_message; }

static void die(const char *lib, const char *fn, const char *fmt, ...) {
    va_list ap;
    fflush(stdout);
    fprintf(stderr, "qpu: %s: %s: ", lib, fn);
    va_start(ap, fmt); vfprintf(stderr, fmt, ap); va_end(ap);
    fprintf(stderr, "\n");
    exit(70);
}

static const char *env(const char *k, const char *dflt) {
    const char *v = getenv(k);
    return v && *v ? v : dflt;
}

static void socket_path(char *out, size_t n) {
    const char *s = getenv("QPU_SERVER");
    if (s && *s) snprintf(out, n, "%s", s);
    else snprintf(out, n, "/tmp/cpu-qpu-%d.sock", (int)getuid());
}

static int try_connect(const char *path) {
    struct sockaddr_un a;
    int s = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (s < 0) return -1;
    memset(&a, 0, sizeof a);
    a.sun_family = AF_UNIX;
    snprintf(a.sun_path, sizeof a.sun_path, "%s", path);
    if (connect(s, (struct sockaddr *)&a, sizeof a) == 0) return s;
    close(s);
    return -1;
}

static void start_server(const char *path) {
    char script[4096], logf[4200];
    snprintf(script, sizeof script, "%s/qpu_server.py", env("QPU_HOME", QPU_HOME_DEFAULT));
    snprintf(logf, sizeof logf, "%s.log", path);
    pid_t p = fork();
    if (p == 0) {                     /* child: detach, then become the server */
        setsid();
        if (fork() != 0) _exit(0);
        int nul = open("/dev/null", O_RDONLY), lg = open(logf, O_WRONLY | O_CREAT | O_APPEND, 0600);
        dup2(nul, 0);
        dup2(lg >= 0 ? lg : nul, 1);
        dup2(lg >= 0 ? lg : nul, 2);
        const char *py = env("QPU_PYTHON", "python3");
        execlp(py, py, script, "--socket", path, "--idle-exit", "600", "-v", (char *)NULL);
        _exit(127);
    }
    if (p > 0) waitpid(p, NULL, 0);           /* the intermediate child exits at once */
}

static int connect_server(const qpu__lib *lib, const char *fn) {
    char path[512];
    socket_path(path, sizeof path);
    int s = try_connect(path);
    if (s >= 0) return s;
    start_server(path);
    for (int i = 0; i < 600; i++) {   /* up to 60 s: the first start imports the simulator */
        struct timespec t = {0, 100000000};
        nanosleep(&t, NULL);
        if ((s = try_connect(path)) >= 0) return s;
    }
    die(lib->name, fn, "cannot reach or start the QPU server at %s (QPU_HOME=%s; log: %s.log)",
        path, env("QPU_HOME", QPU_HOME_DEFAULT), path);
    return -1;
}

/* ---- buffered I/O on the socket */
static int send_all(const void *p, size_t n) {
    const char *c = p;
    while (n) {
        ssize_t k = write(fd, c, n);
        if (k < 0 && errno == EINTR) continue;
        if (k <= 0) return -1;
        c += k; n -= (size_t)k;
    }
    return 0;
}

static int fill(void) {
    if (rpos < rlen) return 0;
    ssize_t k;
    do k = read(fd, rbuf, sizeof rbuf); while (k < 0 && errno == EINTR);
    if (k <= 0) return -1;
    rlen = (size_t)k; rpos = 0;
    return 0;
}

static int read_bytes(char *out, size_t n) {
    while (n) {
        if (fill()) return -1;
        size_t k = rlen - rpos < n ? rlen - rpos : n;
        if (out) { memcpy(out, rbuf + rpos, k); out += k; }
        rpos += k; n -= k;
    }
    return 0;
}

/* a line without the newline; *dyn grows as needed */
static int read_line(char **dyn, size_t *cap) {
    size_t n = 0;
    for (;;) {
        if (fill()) return -1;
        char c = rbuf[rpos++];
        if (n + 1 >= *cap) { *cap = *cap ? 2 * *cap : 256; *dyn = realloc(*dyn, *cap); }
        if (c == '\n') { (*dyn)[n] = 0; return 0; }
        (*dyn)[n++] = c;
    }
}

/* growable request text */
typedef struct { char *p; size_t n, cap; } sbuf;
static void put(sbuf *b, const char *fmt, ...) {
    va_list ap;
    for (;;) {
        va_start(ap, fmt);
        int k = vsnprintf(b->p ? b->p + b->n : NULL, b->p ? b->cap - b->n : 0, fmt, ap);
        va_end(ap);
        if (b->p && (size_t)k < b->cap - b->n) { b->n += (size_t)k; return; }
        b->cap = (b->cap + (size_t)k + 1) * 2;
        b->p = realloc(b->p, b->cap);
    }
}

static int load_library(const qpu__lib *lib) {
    char hdr[160], *line = NULL;
    size_t cap = 0;
    snprintf(hdr, sizeof hdr, "LOAD %s %zu\n", lib->sha256, lib->size);
    int ok = send_all(hdr, strlen(hdr)) == 0 && send_all(lib->object, lib->size) == 0 &&
             read_line(&line, &cap) == 0 && strcmp(line, "OK") == 0;
    free(line);
    return ok ? 0 : -1;
}

long long qpu__call(const qpu__lib *lib, const char *fn, int nargs, const long long *args,
                    qpu__buf *bufs, int nbufs) {
    pthread_mutex_lock(&lock);
    const char *backend = env("QPU_BACKEND", "sim");
    int width = atoi(env("QPU_WIDTH", "0"));
    if (width <= 0) width = lib->width;
    sbuf req = {0};
    put(&req, "CALL %s %s %s %d %d", lib->sha256, fn, backend, width, nargs);
    for (int i = 0; i < nargs; i++) put(&req, " %lld", args[i]);
    put(&req, "\n");
    for (int b = 0; b < nbufs; b++) {
        const char *d = bufs[b].dir == QPU__IN ? "in" : bufs[b].dir == QPU__OUT ? "out" : "inout";
        if (bufs[b].count < 0) die(lib->name, fn, "negative buffer size");
        put(&req, "BUF %d %s %ld", bufs[b].arg, d, bufs[b].count);
        for (long j = 0; j < bufs[b].count; j++) put(&req, " %d", bufs[b].dir == QPU__OUT ? 0 : bufs[b].data[j]);
        put(&req, "\n");
    }
    put(&req, "END\n");

    char *line = NULL, *out = NULL, *msg = NULL, status[32] = "";
    size_t cap = 0;
    long long result = 0;
    for (int attempt = 0; attempt < 3; attempt++) {
        if (fd < 0) fd = connect_server(lib, fn);
        if (send_all(req.p, req.n) || read_line(&line, &cap)) {   /* server gone: reconnect once more */
            close(fd); fd = -1; rlen = rpos = 0;
            continue;
        }
        if (sscanf(line, "STATUS %31s", status) != 1) die(lib->name, fn, "bad reply from the QPU server: %s", line);
        int done = 0;
        while (!done) {
            if (read_line(&line, &cap)) die(lib->name, fn, "connection to the QPU server lost");
            long n;
            if (sscanf(line, "RESULT %lld", &result) == 1) continue;
            if (strncmp(line, "CYCLES", 6) == 0) continue;
            if (strncmp(line, "BUF ", 4) == 0) {           /* BUF <arg> <count> <values> */
                char *p = line + 4, *e;
                int arg = (int)strtol(p, &e, 10);
                long count = strtol(e, &e, 10);
                for (int b = 0; b < nbufs; b++)
                    if (bufs[b].arg == arg)
                        for (long j = 0; j < count && j < bufs[b].count; j++) bufs[b].data[j] = (int)strtol(e, &e, 10);
                continue;
            }
            if (sscanf(line, "OUT %ld", &n) == 1) { free(out); out = calloc(1, (size_t)n + 1); read_bytes(out, (size_t)n); continue; }
            if (sscanf(line, "MSG %ld", &n) == 1) { free(msg); msg = calloc(1, (size_t)n + 1); read_bytes(msg, (size_t)n); continue; }
            if (strcmp(line, "END") == 0) done = 1;
        }
        if (strcmp(status, "unknown-lib") == 0) {          /* new server, or first call: send the object */
            if (load_library(lib)) die(lib->name, fn, "the QPU server did not accept the library");
            continue;
        }
        break;
    }
    if (!*status) die(lib->name, fn, "the QPU server does not answer");
    if (out && *out) { fputs(out, stdout); }                /* what the function printed on the QPU */
    snprintf(last_message, sizeof last_message, "%s", msg ? msg : "");
    int quiet = atoi(env("QPU_QUIET", "0")), strict = atoi(env("QPU_STRICT", "0"));
    if (strcmp(status, "ok") == 0) {
        last_status = 0;
    } else if (strcmp(status, "inexact") == 0) {
        last_status = 1;
        if (strict) die(lib->name, fn, "result not exact: %s", last_message);
        if (!quiet) { fflush(stdout); fprintf(stderr, "qpu: %s: %s: warning: result may be wrong (outside the %d-bit model): %s\n", lib->name, fn, width, last_message); }
    } else {
        last_status = 2;
        die(lib->name, fn, "%s%s%s", status, *last_message ? ": " : "", last_message);
    }
    free(line); free(out); free(msg); free(req.p);
    pthread_mutex_unlock(&lock);
    return result;
}
