# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Quantag IT Solutions GmbH
"""libqarm, the native Q-ARM runtime in C (runtime/libqarm; docs/qarm_v2.md section 7). Host compiler, no emulator.

  1. -DQARM_RUNTIME with libqarm, backend exact: h2_vqe prints what the plain build prints; every ansatz operation
     (3 qubits, Y terms, Pauli rotations) agrees with the plain build's state vector to 1e-9.
  2. backend simulator: the sampled energies agree with the exact ones within 5 standard errors.
  3. The async API (qarm_expect_n_async, qarm_job_ready, qarm_job_wait) in the plain and the native build.
  4. Refused: inconsistent tables; "aer" (Python only) falls back to exact; ibm in a build without libcurl.
  7. QCIRC, QRAND, QCAPS: examples/qarm/ghz.c (a GHZ state, a Toffoli truth table) in the plain and the native
     build; circuits with a qubit out of range or a CX on one qubit refused. With 5: ghz.c on the ibm mock, its
     samples a device's, QRAND's bytes measured, QCAPS flags hardware.
  8. QVSAMPLE: examples/qarm/maxcut_qaoa.c finds the largest cut natively; RY CX sampled on exact (0: here),
     simulator and, with 5, the ibm mock (1: the coprocessor): only 00 and 11.
  6. QSEARCH's engine (qarm_search.c) on hand-written oracles: x == 3 in 2 bits; x & 5 == 5 in 4 bits (four answers);
     a predicate with no answer; an oracle that leaves a work qubit set (refused).
  5. (libcurl, qiskit, qiskit-aer) backend ibm against tests/ibm_mock.py, a local stand-in for IBM's REST API that
     checks every circuit is an ISA circuit for ibm_fez: the 3-qubit ansatz (a SWAP on the chain) within 5 standard
     errors; h2_vqe with Fez's calibrated errors, readout corrected, within 20 mHa; a session; the Open Plan's
     refusal of sessions; the async program polling while the job is queued.
Run: python3 -m tests.test_libqarm"""
import json, os, re, shutil, subprocess, sys, tempfile
from tests.test_qarm_vqe import ALL_OPS, EXACT, MAXCUT, VSAMPLE

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INC = os.path.join(ROOT, "include")
LIB = [os.path.join(ROOT, "runtime", "libqarm", f) for f in ("qarm_rt.c", "qarm_sim.c", "qarm_json.c", "qarm_ibm.c",
                                                                  "qarm_search.c")]
H2 = os.path.join(ROOT, "examples", "qarm", "h2_vqe.c")
GHZ = os.path.join(ROOT, "examples", "qarm", "ghz.c")
BADCIRC = r"""
#include <stdio.h>
#include "qarm.h"
static const qarm_gate g[] = {{QARM_G_H, 0, 0, 0, 0, 0}, {QARM_G_CX, 0, 3, 0, 0, 0}};
static const qarm_gate h[] = {{QARM_G_CX, 1, 1, 0, 0, 0}};
int main(void)
{
    static uint64_t s[4];
    qarm_circuit a = {3, 2, g}, b = {2, 1, h};
    printf("%d %d\n", qarm_run(&a, 4, s), qarm_run(&b, 4, s));
    return 0;
}
"""
EXE = ".exe" if os.name == "nt" else ""

ASYNC = r"""
#include <stdio.h>
#include "qarm.h"
static const qarm_pauli t[] = {{0, 0, -1.052373245772859}, {0, 1, 0.39793742484318045},
    {0, 2, -0.39793742484318045}, {0, 3, -0.01128010425623538}, {3, 0, 0.18093119978423156}};
static const qarm_hamiltonian h = {2, 5, 0.7199689944489797, t};
static const qarm_op ops[] = {{QARM_RY, 0, 0, 0, 0, 1.0, 0, 0}, {QARM_X, 1, 0, 0, 0, 0, 0, 0},
    {QARM_CX, 0, 1, 0, 0, 0, 0, 0}};
static const qarm_ansatz a = {2, 1, 3, 0, 0, ops};
int main(void) {
    qarm_vqe v;
    if (qarm_vqe_load(&h, &a, &v) < 0) return 2;
    double th[3] = {-2.9, -2.918398, -2.95}, e[3], err[3];
    qarm_job *j = qarm_expect_n_async(&v, th, 3);
    if (!j) return 3;
    int polls = 0, r;
    volatile double work = 0;
    while ((r = qarm_job_ready(j)) == 0) {        /* the CPU keeps working while the QPU has the job */
        polls++;
        for (long i = 0; i < 3000000; i++) work += 1e-9 * i;
    }
    if (r < 0 || qarm_job_wait(j, e, err) < 0) return 4;
    printf("polls %d\n", polls);
    for (int i = 0; i < 3; i++) printf("%.9f %.6f\n", e[i], err[i]);
    qarm_vqe_free(&v);
    return 0;
}
"""


SEARCH = r"""
#include <stdio.h>
#include "qarm.h"
static int run(const char *text) {
    char err[300], report[600];
    qarm_oracle *o = qarm_rt_oracle_parse(text, err, sizeof err);
    if (!o) { printf("parse: %s\n", err); return -1; }
    int x = -1, s = qarm_rt_search(o, &x, report, sizeof report);
    printf("%d %d | %s\n", s, x, report);
    qarm_rt_oracle_free(o);
    return s;
}
int main(void) {
    /* f(x) = x0 and x1: ccx onto a work qubit, z on it, uncompute */
    run("qarm-oracle 1 is_three 3 0 2 3\nccx 0 1 2\nz 2\nccx 0 1 2\n");
    run("qarm-oracle 1 bits_0_2 5 0 4 3\nccx 0 2 4\nz 4\nccx 0 2 4\n");
    run("qarm-oracle 1 never 3 0 2 1\nx 2\n");
    run("qarm-oracle 1 none 3 0 2 0\n");
    {   /* QMIN: f(x) = x xor 5 on 3 bits (cx from each input bit, x on bits 0 and 2): least at x = 5 */
        char err[300], report[600];
        qarm_oracle *o = qarm_rt_oracle_parse("qarm-oracle 2 xor5 6 0 3 3 3 5\ncx 0 3\ncx 1 4\ncx 2 5\nx 3\nx 5\n",
                                              err, sizeof err);
        int x = -1; long v = -1;
        int r = o ? qarm_rt_min(o, &x, &v, report, sizeof report) : -9;
        printf("min %d %d %ld | %s\n", r, x, v, o ? report : err);
        qarm_rt_oracle_free(o);
        /* QCOUNT: bits 0 and 2 set, 4 bits: 4 of 16 */
        o = qarm_rt_oracle_parse("qarm-oracle 1 bits_0_2 5 0 4 3\nccx 0 2 4\nz 4\nccx 0 2 4\n", err, sizeof err);
        long n = -1; double b = -1;
        r = qarm_rt_count(o, &n, &b, report, sizeof report);
        printf("count %d %ld %.3f | %s\n", r, n, b, report);
        qarm_rt_oracle_free(o);
        /* QPERIOD: f(x) = x mod 4 on 4 bits (out = x's low two bits): period 4 */
        o = qarm_rt_oracle_parse("qarm-oracle 2 mod4 6 0 4 4 2 2\ncx 0 4\ncx 1 5\n", err, sizeof err);
        int pr = -1; r = qarm_rt_period(o, &pr, report, sizeof report);
        printf("period %d %d | %s\n", r, pr, report);
        qarm_rt_oracle_free(o);
        /* QSIMON: f(x) = x0 xor x1 on 2 bits: f(x) = f(x ^ 3) */
        o = qarm_rt_oracle_parse("qarm-oracle 2 xor01 3 0 2 2 1 2\ncx 0 2\ncx 1 2\n", err, sizeof err);
        int sh = -1; r = qarm_rt_simon(o, &sh, report, sizeof report);
        printf("simon %d %d | %s\n", r, sh, report);
        qarm_rt_oracle_free(o);
        /* QBV: f(x) = x0 xor x2 on 3 bits (z on qubits 0 and 2): s = 5 */
        o = qarm_rt_oracle_parse("qarm-oracle 1 bv5 3 0 3 2\nz 0\nz 2\n", err, sizeof err);
        int sv = -1; r = qarm_rt_bv(o, &sv, report, sizeof report);
        printf("bv %d %d | %s\n", r, sv, report);
        qarm_rt_oracle_free(o);
        /* QAMPEST: g(x) = x on 2 bits (out = x), values 0..3: mean 1.5 */
        o = qarm_rt_oracle_parse("qarm-oracle 2 identity 4 0 2 2 2 2\ncx 0 2\ncx 1 3\n", err, sizeof err);
        double mean = -1, mb = -1; r = qarm_rt_ampest(o, 2, &mean, &mb, report, sizeof report);
        printf("ampest %d %.4f %.4f | %s\n", r, mean, mb, report);
        qarm_rt_oracle_free(o);
    }
    return 0;
}
"""


def compiler():
    for c in (os.environ.get("CC"), "cc", "clang", "gcc"):
        if c and shutil.which(c):
            return [c]
    if os.name == "nt":
        cand = os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "LLVM", "bin", "clang.exe")
        if os.path.exists(cand):
            return [cand]
    return None


def sh(cmd, env=None, check=True):
    p = subprocess.run(cmd, capture_output=True, text=True, env=dict(os.environ, **(env or {})))
    if check and p.returncode:
        raise AssertionError(f"{' '.join(cmd)}: exit {p.returncode}\n{p.stdout}\n{p.stderr}")
    return p


def has_curl(cc, d):
    src = os.path.join(d, "curl_probe.c")
    open(src, "w").write("#include <curl/curl.h>\nint main(void) { return curl_global_init(CURL_GLOBAL_DEFAULT); }\n")
    return sh(cc + [src, "-o", os.path.join(d, "curl_probe" + EXE), "-lcurl"], check=False).returncode == 0


def build(cc, src, exe, native, curl=True):
    cmd = cc + ["-O2", "-iquote", INC, src, "-o", exe]
    if native:
        cmd = cc + ["-O2", "-DQARM_RUNTIME", "-iquote", INC] + ([] if curl else ["-DQARM_NO_CURL"]) + [src] + LIB \
            + ["-o", exe] + (["-lcurl"] if curl else [])
    if os.name != "nt":
        cmd.append("-lm")
    sh(cmd)


def numbers(text):
    return [float(v) for v in re.findall(r"-?\d+\.\d+", text)]


def main():
    cc = compiler()
    if not cc:
        print("skipped: no C compiler")
        return
    with tempfile.TemporaryDirectory() as d:
        curl = has_curl(cc, d)
        env = {"QARM_CONFIG": os.path.join(d, "none.json")}             # no config: the exact backend
        exe = {}
        for name, src in (("h2", H2), ("all", None), ("async", None), ("ghz", GHZ), ("badcirc", None),
                          ("maxcut", MAXCUT), ("vsample", None)):
            if src is None:
                src = os.path.join(d, name + ".c")
                open(src, "w").write({"all": ALL_OPS, "async": ASYNC, "badcirc": BADCIRC, "vsample": VSAMPLE}[name])
            for native in (False, True):
                exe[name, native] = os.path.join(d, f"{name}_{'native' if native else 'plain'}{EXE}")
                build(cc, src, exe[name, native], native, curl)

        plain, nat = sh([exe["h2", False]]).stdout, sh([exe["h2", True]], env).stdout
        assert plain == nat and abs(numbers(nat)[-2] - EXACT) < 1e-6, (plain, nat)
        a, b = numbers(sh([exe["all", False]]).stdout), numbers(sh([exe["all", True]], env).stdout)
        assert len(a) == 6 and all(abs(x - y) < 1e-9 for x, y in zip(a, b)), (a, b)
        print(f"1) native, exact: h2_vqe as the plain build (E = {numbers(nat)[-2]:.6f} Ha); every operation, "
              "3 qubits: the same energies to 1e-9")

        cfg = os.path.join(d, "sim.json")
        json.dump({"backend": "simulator", "shots": 20000}, open(cfg, "w"))
        m = numbers(sh([exe["all", True]], {"QARM_CONFIG": cfg, "QARM_SEED": "5"}).stdout)
        worst = max(abs(x - y) for x, y in zip(m[:5], a[:5]))
        assert 0 < m[5] < 0.02 and worst < 5 * m[5], (m, a)
        print(f"2) native, simulator: within {worst / m[5]:.1f} standard errors of the exact energies")

        out = [sh([exe["async", n]], env).stdout for n in (False, True)]
        assert out[0] == out[1] and out[0].startswith("polls 0\n"), out
        print("3) async API: the same results in the plain and the native build; exact: ready at once")

        bad = os.path.join(d, "bad.c")
        open(bad, "w").write(ALL_OPS.replace("static const qarm_ansatz a = {3, 3, 9, 0, 1, ops};",
                                             "static const qarm_ansatz a = {2, 3, 9, 0, 1, ops};"))
        bexe = os.path.join(d, "bad" + EXE)
        build(cc, bad, bexe, True, curl)
        p = sh([bexe], env, check=False)
        assert p.returncode == 2 and "QVLOAD refused: the ansatz has 2 qubits, the Hamiltonian 3" in p.stderr, p.stderr
        p = sh([exe["h2", True]], dict(env, QARM_BACKEND="aer"))
        assert "needs Python (qarm_exec.py)" in p.stderr and p.stdout == plain, p.stderr
        nocurl = os.path.join(d, "h2_nocurl" + EXE)
        build(cc, H2, nocurl, True, curl=False)
        p = sh([nocurl], dict(env, QARM_BACKEND="ibm"), check=False)
        assert p.returncode == 2 and "built without libcurl" in p.stderr, p.stderr
        print("4) refused: an ansatz of the wrong size; aer falls back to exact; ibm without libcurl")

        outs = []
        for native in (False, True):
            p = sh([exe["ghz", native]], dict(env, QARM_SEED="3"))
            assert re.search(r"GHZ\(5\): 00000 \d+, 11111 \d+, other 0 of 2000", p.stdout), p.stdout
            assert "11->1 100%" in p.stdout and "00->0 100%" in p.stdout and "pseudo-random" in p.stdout, p.stdout
            outs.append(p)
        assert "libqarm: exact" in outs[1].stdout and "simulated here" in outs[0].stdout, outs[1].stdout
        assert "on the coprocessor" in outs[1].stdout and "QCIRC: 5 qubits, 5 gates (4 multi-qubit)" in outs[1].stderr
        p = sh([exe["badcirc", True]], env)
        assert p.stdout.split() == ["-1", "-1"] and p.stderr.count("QCIRC refused") == 2, (p.stdout, p.stderr)
        assert sh([exe["badcirc", False]]).stdout.split() == ["-1", "-1"]
        print("7) QCIRC, QRAND, QCAPS: ghz.c in the plain and the native build (GHZ: only 00000 and 11111; Toffoli's "
              "truth table); a qubit out of range and a CX on one qubit refused by both")

        p = sh([exe["maxcut", True]], env)
        assert "best cut 10" in p.stdout and "QVSAMPLE: 8 qubits, 1000 shots" in p.stderr, (p.stdout, p.stderr)
        assert "best cut 10" in sh([exe["maxcut", False]]).stdout
        sim = os.path.join(d, "sim1.json")
        json.dump({"backend": "simulator", "shots": 1000}, open(sim, "w"))
        for cfgfile, want in ((env["QARM_CONFIG"], 0), (sim, 1)):
            w, *c = (int(v) for v in sh([exe["vsample", True]], {"QARM_CONFIG": cfgfile}).stdout.split())
            assert w == want and c[1] == c[2] == 0 and abs(c[0] - 1000) < 150, (w, c)
        print("8) QVSAMPLE: maxcut_qaoa natively finds the largest cut, 10; RY CX on exact and simulator: only 00 and 11")

        src = os.path.join(d, "search.c")
        open(src, "w").write(SEARCH)
        sexe = os.path.join(d, "search" + EXE)
        build(cc, src, sexe, True, curl)
        lines = sh([sexe], {"QARM_SEED": "1"}).stdout.splitlines()
        assert lines[0].startswith("1 3 |"), lines
        x = int(lines[1].split()[1])
        assert lines[1].startswith("1 ") and x & 5 == 5, lines
        assert lines[2].startswith("-1 ") and "leaves qubit 2 at 1" in lines[2], lines
        assert lines[3].startswith("2 ") and "no x with f(x) = 1" in lines[3], lines
        assert lines[4].startswith("min 1 5 0 |"), lines
        n, b = int(lines[5].split()[2]), float(lines[5].split()[3])
        assert lines[5].startswith("count 1 ") and abs(n - 4) <= b, lines
        assert lines[6].startswith("period 1 4 |") and lines[7].startswith("simon 1 3 |") and lines[8].startswith("bv 1 5 |"), lines
        mean, mb = float(lines[9].split()[2]), float(lines[9].split()[3])
        assert lines[9].startswith("ampest 1 ") and abs(mean - 1.5) <= mb, lines
        print(f"6) QSEARCH's engine: x == 3 found; x & 5 == 5 found ({x}); an oracle that leaves a work qubit set refused;"
              f" no answer reported. QMIN: x xor 5 least at 5. QCOUNT: {n} +- {b:.1f} of 4. QPERIOD: x mod 4 has period 4; "
              f"QSIMON: x0 xor x1 has shift 3; QBV: x0 xor x2 gives 5; QAMPEST: the mean of x, {mean} +- {mb} of 1.5")

        try:
            import qiskit, qiskit_aer, qiskit_ibm_runtime  # noqa: F401
        except ImportError as e:
            print(f"5) skipped: {e.name} is not installed")
            print("PASS")
            return
        if not curl:
            print("5) skipped: no libcurl")
            print("PASS")
            return
        import warnings
        warnings.filterwarnings("ignore")
        sys.path.insert(0, ROOT)
        from tests.ibm_mock import Mock
        mock = Mock(noise=False)
        cfg = os.path.join(d, "ibm.json")
        json.dump({"backend": "ibm", "shots": 20000, "options": mock.options()}, open(cfg, "w"))
        p = sh([exe["all", True]], {"QARM_CONFIG": cfg})
        m = numbers(p.stdout)
        worst = max(abs(x - y) for x, y in zip(m[:5], a[:5]))
        assert 0 < m[5] < 0.02 and worst < 5 * m[5], (m, a, p.stderr)
        assert "readout correction" in p.stderr and len(re.findall(r"job mock-\w+ sent to ibm_fez", p.stderr)) == 2
        qasm = [pub[0] for meth, path, b in mock.log if path == "/api/v1/jobs" for pub in json.loads(b)["params"]["pubs"]]
        cz = max(q.count("cz ") for q in qasm)            # 6 two-qubit gates in the ansatz, 3 more for a SWAP
        assert cz >= 9, cz
        print(f"5a) ibm (mock of IBM's REST API, ideal): 3 qubits on a chain of ibm_fez, ISA circuits with a SWAP "
              f"({cz} CZ), within {worst / m[5]:.1f} standard errors")
        p = sh([exe["ghz", True]], {"QARM_CONFIG": cfg})
        g = re.search(r"00000 (\d+), 11111 (\d+), other (\d+)", p.stdout)
        assert g and int(g[3]) < 100 and "quantum hardware" in p.stdout and "flags 7 (hardware)" in p.stdout, p.stdout
        assert len(re.findall(r"QCIRC: .* shots on ibm_fez \(job mock-", p.stderr)) == 5 and "QRAND: 16 bytes" in p.stderr
        print(f"5d) ghz.c on ibm (mock): GHZ {g[1]}/{g[2]}/{g[3]}, Toffoli's table, QRAND measured, QCAPS: hardware")
        w, *c = (int(v) for v in sh([exe["vsample", True]], {"QARM_CONFIG": cfg}).stdout.split())
        assert w == 1 and c[1] == c[2] == 0 and abs(c[0] - 1000) < 150, (w, c)
        print(f"5e) QVSAMPLE on ibm (mock): 00 {c[0]}, 11 {c[3]}")
        p = sh([exe["async", True]], {"QARM_CONFIG": cfg})
        polls = int(p.stdout.split()[1])
        assert polls >= 2, p.stdout
        print(f"5b) async: the program polled {polls} times while the job was queued and running")
        mock.close()

        mock = Mock(noise=True)
        json.dump({"backend": "ibm", "shots": 4000, "options": mock.options()}, open(cfg, "w"))
        p = sh([exe["h2", True]], {"QARM_CONFIG": cfg})
        e = numbers(p.stdout.splitlines()[-1])[1]
        jobs = [json.loads(b) for meth, path, b in mock.log if meth == "POST" and path == "/api/v1/jobs"]
        assert abs(e - EXACT) < 0.02 and len(jobs) == 5 and all(j.get("session_id") for j in jobs), (p.stdout, p.stderr)
        assert re.search(r"QSESSION: session session-\w+ on ibm_fez", p.stderr) and "closed" in p.stderr
        mock.close()
        mock = Mock(noise=True, sessions=False)
        json.dump({"backend": "ibm", "shots": 4000, "options": mock.options()}, open(cfg, "w"))
        p = sh([exe["h2", True]], {"QARM_CONFIG": cfg})
        e2 = numbers(p.stdout.splitlines()[-1])[1]
        assert "no session" in p.stderr and "Open Plan" in p.stderr and abs(e2 - EXACT) < 0.02, p.stderr
        mock.close()
        print(f"5c) h2_vqe on ibm_fez's calibrated errors: E = {e:.4f} Ha ({(e - EXACT) * 1000:+.1f} mHa), 5 jobs in "
              f"a session; without sessions (Open Plan): E = {e2:.4f} Ha, each job on its own")
    print("PASS")


if __name__ == "__main__":
    main()
