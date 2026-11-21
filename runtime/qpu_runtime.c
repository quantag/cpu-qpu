/* SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 Quantag IT Solutions GmbH */
/* Runtime of generated QPU libraries: sends calls to qpu_server.py (protocol in qpu_server.py).
 *
 * Transport: a Unix socket (Linux default), or TCP on 127.0.0.1 with a secret token (Windows default;
 * on Linux when QPU_SERVER names a file ending in .txt). In TCP mode the server writes
 * "127.0.0.1 <port> <token>" to that endpoint file, and every connection starts with "AUTH <token>". */
#ifdef _WIN32
#  ifndef WIN32_LEAN_AND_MEAN
#    define WIN32_LEAN_AND_MEAN
#  endif
#  define _CRT_SECURE_NO_WARNINGS
#  include <winsock2.h>
#  include <ws2tcpip.h>
#  include <windows.h>
#endif
#include "qpu_runtime.h"
#include <errno.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifdef _WIN32
typedef SOCKET sock_t;
#  define BAD_SOCK INVALID_SOCKET
static SRWLOCK lock = SRWLOCK_INIT;
#  define LOCK() AcquireSRWLockExclusive(&lock)
#  define UNLOCK() ReleaseSRWLockExclusive(&lock)
#  define SEND_FLAGS 0
static void sleep_ms(int ms) { Sleep((DWORD)ms); }
static void sock_close(sock_t s) { closesocket(s); }
#else
#  include <arpa/inet.h>
#  include <fcntl.h>
#  include <netinet/in.h>
#  include <pthread.h>
#  include <sys/socket.h>
#  include <sys/un.h>
#  include <sys/wait.h>
#  include <time.h>
#  include <unistd.h>
typedef int sock_t;
#  define BAD_SOCK (-1)
static pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;
#  define LOCK() pthread_mutex_lock(&lock)
#  define UNLOCK() pthread_mutex_unlock(&lock)
#  define SEND_FLAGS MSG_NOSIGNAL                   /* a server that went away must not kill the host */
static void sleep_ms(int ms) { struct timespec t = {ms / 1000, (ms % 1000) * 1000000L}; nanosleep(&t, NULL); }
static void sock_close(sock_t s) { close(s); }
#endif

#ifndef QPU_HOME_DEFAULT
#define QPU_HOME_DEFAULT "."
#endif

static sock_t fd = BAD_SOCK;
static char rbuf[1 << 16];
static size_t rlen, rpos;
static int last_status;               /* 0 ok, 1 inexact */
static char last_message[1024];

/* ---- optional queries, exported by every generated library */
QPU__EXPORT int qpu_last_status(void) { return last_status; }
QPU__EXPORT const char *qpu_last_message(void) { return last_message; }

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

static int is_tcp(const char *endpoint) {
    size_t n = strlen(endpoint);
    return n > 4 && strcmp(endpoint + n - 4, ".txt") == 0;
}

/* where the server is: QPU_SERVER, else the platform default */
static void endpoint(char *out, size_t n) {
    const char *s = getenv("QPU_SERVER");
    if (s && *s) { snprintf(out, n, "%s", s); return; }
#ifdef _WIN32
    snprintf(out, n, "%s\\cpu-qpu\\qpu-server.txt", env("LOCALAPPDATA", "."));
#else
    snprintf(out, n, "/tmp/cpu-qpu-%d.sock", (int)getuid());
#endif
}

/* ---- buffered I/O on the socket */
static int send_all(const void *p, size_t n) {
    const char *c = p;
    while (n) {
        int k = (int)send(fd, c, (int)(n > (1 << 20) ? (1 << 20) : n), SEND_FLAGS);
        if (k < 0 && errno == EINTR) continue;
        if (k <= 0) return -1;
        c += k; n -= (size_t)k;
    }
    return 0;
}

static int fill(void) {
    if (rpos < rlen) return 0;
    int k;
    do k = (int)recv(fd, rbuf, (int)sizeof rbuf, 0); while (k < 0 && errno == EINTR);
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
        if (c != '\r') (*dyn)[n++] = c;
    }
}

/* ---- connecting */
static sock_t try_tcp(const char *file) {
    char host[64] = "", token[128] = "", *line = NULL;
    int port = 0;
    size_t cap = 0;
    FILE *f = fopen(file, "r");
    if (!f) return BAD_SOCK;
    int ok = fscanf(f, "%63s %d %127s", host, &port, token) == 3;
    fclose(f);
    if (!ok) return BAD_SOCK;
    struct sockaddr_in a;
    memset(&a, 0, sizeof a);
    a.sin_family = AF_INET;
    a.sin_port = htons((unsigned short)port);
    if (inet_pton(AF_INET, host, &a.sin_addr) != 1) return BAD_SOCK;
    sock_t s = socket(AF_INET, SOCK_STREAM, 0);
    if (s == BAD_SOCK) return BAD_SOCK;
    if (connect(s, (struct sockaddr *)&a, sizeof a) != 0) { sock_close(s); return BAD_SOCK; }
    char hello[160];
    snprintf(hello, sizeof hello, "AUTH %s\n", token);
    sock_t keep = fd;
    fd = s; rlen = rpos = 0;
    ok = send_all(hello, strlen(hello)) == 0 && read_line(&line, &cap) == 0 && strcmp(line, "OK") == 0;
    free(line);
    fd = keep;
    if (!ok) { sock_close(s); return BAD_SOCK; }
    return s;
}

static sock_t try_connect(const char *ep) {
    if (is_tcp(ep)) return try_tcp(ep);
#ifdef _WIN32
    return BAD_SOCK;                  /* Windows: TCP only */
#else
    struct sockaddr_un a;
    sock_t s = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (s < 0) return BAD_SOCK;
    memset(&a, 0, sizeof a);
    a.sun_family = AF_UNIX;
    snprintf(a.sun_path, sizeof a.sun_path, "%s", ep);
    if (connect(s, (struct sockaddr *)&a, sizeof a) == 0) return s;
    close(s);
    return BAD_SOCK;
#endif
}

static void start_server(const char *ep) {
    char script[4096], logf[4200];
    const char *home = env("QPU_HOME", QPU_HOME_DEFAULT), *py = env("QPU_PYTHON", "");
#ifdef _WIN32
    snprintf(script, sizeof script, "%s\\qpu_server.py", home);
    snprintf(logf, sizeof logf, "%s.log", ep);
    char dir[4096], *slash;                                 /* the endpoint folder must exist */
    snprintf(dir, sizeof dir, "%s", ep);
    if ((slash = strrchr(dir, '\\')) != NULL) { *slash = 0; CreateDirectoryA(dir, NULL); }
    SECURITY_ATTRIBUTES sa = {sizeof sa, NULL, TRUE};
    HANDLE lg = CreateFileA(logf, FILE_APPEND_DATA, FILE_SHARE_READ | FILE_SHARE_WRITE, &sa, OPEN_ALWAYS,
                            FILE_ATTRIBUTE_NORMAL, NULL);
    HANDLE nul = CreateFileA("NUL", GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, &sa, OPEN_EXISTING, 0, NULL);
    const char *tries[] = {*py ? py : "python", "py -3"};
    for (int t = 0; t < (*py ? 1 : 2); t++) {
        char cmd[9000];
        snprintf(cmd, sizeof cmd, "%s \"%s\" --socket \"%s\" --idle-exit 600 -v", tries[t], script, ep);
        STARTUPINFOA si;
        PROCESS_INFORMATION pi;
        memset(&si, 0, sizeof si);
        si.cb = sizeof si;
        si.dwFlags = STARTF_USESTDHANDLES;
        si.hStdInput = nul; si.hStdOutput = lg; si.hStdError = lg;
        if (CreateProcessA(NULL, cmd, NULL, NULL, TRUE, DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP, NULL, NULL, &si, &pi)) {
            CloseHandle(pi.hThread); CloseHandle(pi.hProcess);
            break;
        }
    }
    if (lg != INVALID_HANDLE_VALUE) CloseHandle(lg);
    if (nul != INVALID_HANDLE_VALUE) CloseHandle(nul);
#else
    snprintf(script, sizeof script, "%s/qpu_server.py", home);
    snprintf(logf, sizeof logf, "%s.log", ep);
    if (!*py) py = "python3";
    pid_t p = fork();
    if (p == 0) {                     /* child: detach, then become the server */
        setsid();
        if (fork() != 0) _exit(0);
        int nul = open("/dev/null", O_RDONLY), lg = open(logf, O_WRONLY | O_CREAT | O_APPEND, 0600);
        dup2(nul, 0);
        dup2(lg >= 0 ? lg : nul, 1);
        dup2(lg >= 0 ? lg : nul, 2);
        execlp(py, py, script, "--socket", ep, "--idle-exit", "600", "-v", (char *)NULL);
        _exit(127);
    }
    if (p > 0) waitpid(p, NULL, 0);   /* the intermediate child exits at once */
#endif
}

static sock_t connect_server(const qpu__lib *lib, const char *fn) {
    char ep[1024];
#ifdef _WIN32
    static int wsa;
    if (!wsa) {
        WSADATA d;
        if (WSAStartup(MAKEWORD(2, 2), &d)) die(lib->name, fn, "Winsock could not be started");
        wsa = 1;
    }
#endif
    endpoint(ep, sizeof ep);
    sock_t s = try_connect(ep);
    if (s != BAD_SOCK) return s;
    start_server(ep);
    for (int i = 0; i < 600; i++) {   /* up to 60 s: the first start imports the simulator */
        sleep_ms(100);
        if ((s = try_connect(ep)) != BAD_SOCK) return s;
    }
    die(lib->name, fn, "cannot reach or start the QPU server at %s (QPU_HOME=%s, QPU_PYTHON=%s; log: %s.log)",
        ep, env("QPU_HOME", QPU_HOME_DEFAULT), env("QPU_PYTHON", "default"), ep);
    return BAD_SOCK;
}

/* growable request text */
typedef struct { char *p; size_t n, cap; } sbuf;
static void put(sbuf *b, const char *fmt, ...) {
    va_list ap;
    for (;;) {
        if (b->cap - b->n < 64) { b->cap = b->cap * 2 + 256; b->p = realloc(b->p, b->cap); }
        va_start(ap, fmt);
        int k = vsnprintf(b->p + b->n, b->cap - b->n, fmt, ap);
        va_end(ap);
        if (k >= 0 && (size_t)k < b->cap - b->n) { b->n += (size_t)k; return; }
        b->cap = b->cap * 2 + (size_t)(k > 0 ? k : 256);
        b->p = realloc(b->p, b->cap);
    }
}

static int load_library(const qpu__lib *lib) {
    char hdr[160], *line = NULL;
    size_t cap = 0;
    snprintf(hdr, sizeof hdr, "LOAD %s %lu\n", lib->sha256, (unsigned long)lib->size);
    int ok = send_all(hdr, strlen(hdr)) == 0 && send_all(lib->object, lib->size) == 0 &&
             read_line(&line, &cap) == 0 && strcmp(line, "OK") == 0;
    free(line);
    return ok ? 0 : -1;
}

long long qpu__call(const qpu__lib *lib, const char *fn, int nargs, const long long *args,
                    qpu__buf *bufs, int nbufs) {
    LOCK();
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
        put(&req, "BUF %d %s %lld", bufs[b].arg, d, bufs[b].count);
        for (long long j = 0; j < bufs[b].count; j++) put(&req, " %d", bufs[b].dir == QPU__OUT ? 0 : bufs[b].data[j]);
        put(&req, "\n");
    }
    put(&req, "END\n");

    char *line = NULL, *out = NULL, *msg = NULL, status[32] = "";
    size_t cap = 0;
    long long result = 0;
    for (int attempt = 0; attempt < 3; attempt++) {
        if (fd == BAD_SOCK) { fd = connect_server(lib, fn); rlen = rpos = 0; }
        status[0] = 0;
        if (send_all(req.p, req.n) || read_line(&line, &cap)) {   /* server gone: connect again */
            sock_close(fd); fd = BAD_SOCK;
            continue;
        }
        if (sscanf(line, "STATUS %31s", status) != 1) die(lib->name, fn, "bad reply from the QPU server: %s", line);
        int done = 0;
        while (!done) {
            long long n;
            if (read_line(&line, &cap)) die(lib->name, fn, "connection to the QPU server lost");
            if (sscanf(line, "RESULT %lld", &result) == 1) continue;
            if (strncmp(line, "CYCLES", 6) == 0) continue;
            if (strncmp(line, "BUF ", 4) == 0) {           /* BUF <arg> <count> <values> */
                char *p = line + 4, *e;
                int arg = (int)strtol(p, &e, 10);
                long long count = strtoll(e, &e, 10);
                for (int b = 0; b < nbufs; b++)
                    if (bufs[b].arg == arg)
                        for (long long j = 0; j < count && j < bufs[b].count; j++) bufs[b].data[j] = (int)strtol(e, &e, 10);
                continue;
            }
            if (sscanf(line, "OUT %lld", &n) == 1) { free(out); out = calloc(1, (size_t)n + 1); read_bytes(out, (size_t)n); continue; }
            if (sscanf(line, "MSG %lld", &n) == 1) { free(msg); msg = calloc(1, (size_t)n + 1); read_bytes(msg, (size_t)n); continue; }
            if (strcmp(line, "END") == 0) done = 1;
        }
        if (strcmp(status, "unknown-lib") == 0) {          /* new server, or first call: send the object */
            if (load_library(lib)) die(lib->name, fn, "the QPU server did not accept the library");
            status[0] = 0;
            continue;
        }
        break;
    }
    if (!*status) die(lib->name, fn, "the QPU server does not answer");
    if (out && *out) fputs(out, stdout);                  /* what the function printed on the QPU */
    snprintf(last_message, sizeof last_message, "%s", msg ? msg : "");
    int quiet = atoi(env("QPU_QUIET", "0")), strict = atoi(env("QPU_STRICT", "0"));
    if (strcmp(status, "ok") == 0) {
        last_status = 0;
    } else if (strcmp(status, "inexact") == 0) {
        last_status = 1;
        if (strict) die(lib->name, fn, "result not exact: %s", last_message);
        if (!quiet) {
            fflush(stdout);
            fprintf(stderr, "qpu: %s: %s: warning: result may be wrong (outside the %d-bit model): %s\n",
                    lib->name, fn, width, last_message);
        }
    } else {
        die(lib->name, fn, "%s%s%s", status, *last_message ? ": " : "", last_message);
    }
    free(line); free(out); free(msg); free(req.p);
    UNLOCK();
    return result;
}
