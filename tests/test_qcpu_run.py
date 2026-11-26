# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Quantag IT Solutions GmbH
"""qcpu_run.py: quantum algorithms around exported __qcpu__ oracles, on simulators and IBM devices.

  1. backend "simulator" (ideal): Bernstein-Vazirani with hidden(x) = parity(x & 0xB5) gives 181 every shot; Grover's
     search with is_three (x == 3, 2 bits, one iteration) gives 3 every shot.
  2. backend "preview" (IBM's noise model of a real device): the right answers are by far the most frequent.
  3. backend "ibm" through qiskit-ibm-runtime's local channel (the code path of a real device, without the login),
     configured as {"backend": "ibm", "mode": "sampler", "shots": ..., "options": {...}}: a job id, the right answer.
  4. config.json: options go to the chosen backend (ibm_token, instance, device); the older "ibm" section still
     works; a mode other than sampler, a missing API key, an unknown backend or preview device, a missing config
     file, a compute oracle instead of a phase oracle: clear errors. config.json is ignored by git.
Run: python3 -m tests.test_qcpu_run"""
import json, os, subprocess, sys, tempfile, textwrap
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def main():
    try:
        import qoracle, qiskit, qiskit_aer, qiskit_ibm_runtime, qiskit_qasm3_import  # noqa: F401
    except ImportError as e:
        print(f"skipped: {e.name} is not installed")
        return
    import qcpu_run
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "s.c")
        open(src, "w").write(textwrap.dedent("""\
            #include <qcpu.h>
            __qcpu__ int QCPU_RESULT_BITS(1) hidden(int QCPU_BITS(8) x) { return __builtin_parity(x & 0xB5); }
            __qcpu__ int QCPU_RESULT_BITS(1) is_three(int QCPU_BITS(2) x) { return x == 3; }
            int main(void) { return 0; }
            """))
        out = os.path.join(d, "o")
        subprocess.run([sys.executable, os.path.join(ROOT, "qcpucc.py"), "--qcpu-mode", "qoracle", "--qcpu-export", out,
                        src, "-o", os.path.join(d, "s")], check=True, capture_output=True)
        bv, gr = os.path.join(out, "hidden_phase.qasm"), os.path.join(out, "is_three_phase.qasm")

        def cfg(**kw):
            path = os.path.join(d, "config.json")
            json.dump(kw, open(path, "w"))
            return qcpu_run.load_config(path)

        # 1. ideal
        r = qcpu_run.run("bv", bv, cfg(backend="simulator", shots=200), expect=0xB5)
        assert r["right_answer_fraction"] == 1.0 and r["results"][0]["x"] == 181, r
        r = qcpu_run.run("grover", gr, cfg(backend="simulator", shots=200), expect=3)
        assert r["right_answer_fraction"] == 1.0 and r["iterations"] == 1, r
        print("1) simulator (ideal): Bernstein-Vazirani finds the hidden mask 181 (0xB5) in every shot, with one call of "
              "hidden(); Grover's search finds 3 in every shot")

        # 2. preview: the noise model of a real IBM device
        r1 = qcpu_run.run("bv", bv, cfg(backend="preview", shots=1000), expect=0xB5)
        r2 = qcpu_run.run("grover", gr, cfg(backend="preview", shots=1000, options={"device": "fez"}), expect=3)
        assert r1["results"][0]["x"] == 181 and r1["right_answer_fraction"] > 0.6, r1
        assert r2["results"][0]["x"] == 3 and r2["right_answer_fraction"] > 0.5, r2
        print(f"2) preview ({r1['backend'].split(':')[1].split(' (')[0].strip()}): Bernstein-Vazirani "
              f"{r1['right_answer_fraction']:.1%} right ({r1['two_qubit_gates']} two-qubit gates; random 0.4%), "
              f"Grover {r2['right_answer_fraction']:.1%} right ({r2['two_qubit_gates']} two-qubit gates; random 25%)")

        # 3. the IBM code path, local channel
        r = qcpu_run.run("bv", bv, cfg(backend="ibm", mode="sampler", shots=500,
                                       options={"channel": "local", "device": "fake_torino"}), expect=0xB5)
        assert r["backend"].startswith("ibm: fake_torino") and r["job_id"] and r["results"][0]["x"] == 181, r
        print(f"3) ibm, local channel (fake_torino): job {r['job_id'][:8]}..., 181 in {r['right_answer_fraction']:.1%} "
              "of shots: the path of a real device, without the login")

        # 4. errors
        c = cfg(backend="ibm", mode="sampler", shots=1024,
                options={"ibm_token": "abc", "instance": "one", "device": "ibm_aachen"})
        assert (c["ibm"]["token"], c["ibm"]["instance"], c["ibm"]["backend"], c["shots"]) == ("abc", "one", "ibm_aachen", 1024), c
        c = cfg(backend="ibm", ibm={"token": "xyz", "backend": "ibm_fez"})                  # the older form
        assert (c["ibm"]["token"], c["ibm"]["backend"]) == ("xyz", "ibm_fez"), c
        try:
            cfg(backend="ibm", mode="estimator")
            raise AssertionError("estimator")
        except SystemExit as e:
            assert "mode 'estimator': supported: sampler" in str(e), e
        os.environ.pop("IBM_QUANTUM_TOKEN", None)
        for kw, msg in (({"backend": "ibm"}, "needs your IBM Quantum API key: \"options\": {\"ibm_token\""),
                        ({"backend": "qpu"}, "backend 'qpu': use simulator, aer, preview, ibm"),
                        ({"backend": "preview", "options": {"device": "nowhere"}}, "is not a known IBM device")):
            try:
                qcpu_run.run("bv", bv, cfg(**kw))
                raise AssertionError(kw)
            except qcpu_run.Error as e:
                assert msg in str(e), (kw, e)
        p = subprocess.run([sys.executable, os.path.join(ROOT, "qcpu_run.py"), "bv", bv, "--config",
                            os.path.join(d, "missing.json")], capture_output=True, text=True)
        assert p.returncode == 1 and "missing.json: no such file" in p.stderr, p.stderr
        p = subprocess.run([sys.executable, os.path.join(ROOT, "qcpu_run.py"), "bv", os.path.join(out, "hidden.qasm"), "--backend", "simulator"],
                           capture_output=True, text=True, cwd=d)
        assert p.returncode == 1 and "give the phase oracle" in p.stderr, p.stderr
        ignored = subprocess.run(["git", "check-ignore", "-q", "--no-index", "config.json"], cwd=ROOT).returncode == 0
        assert ignored, "config.json must be in .gitignore"
        print("4) config.json: options to the chosen backend (ibm_token, instance, device), the older ibm section, mode "
              "sampler only; errors: no API key, an unknown backend or device, a missing config file, a compute oracle; "
              "config.json is ignored by git")
    print("PASS")


if __name__ == "__main__":
    main()
