#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Quantag IT Solutions GmbH
"""qcpu_run: run a quantum algorithm around an exported __qcpu__ oracle, on a simulator or a real IBM quantum computer.

    python3 qcpucc.py --qcpu-mode qoracle --qcpu-export oracle game.c -o game
    python3 qcpu_run.py bv oracle/hidden_phase.qasm                    # Bernstein-Vazirani: f(x) = parity(x & s)
    python3 qcpu_run.py grover oracle/is_three_phase.qasm --expect 3   # Grover's search

Where it runs comes from config.json in this repository (your own file, ignored by git; copy config.example.json):
    {"backend": "ibm", "mode": "sampler", "shots": 1024,
     "options": {"ibm_token": "your API key", "instance": "your instance", "device": "ibm_aachen"}}
"backend" (or --backend):
    simulator   an ideal simulator (qiskit-aer)
    aer         Qiskit Aer; options: method
    preview     IBM's noise model of a real device, no account; options: device (fez, torino, ...)
    ibm         a real IBM quantum computer; options: ibm_token (or "env:VAR"), instance, device (a name or
                least_busy), channel, optimization_level
The oracle is the phase form written by qcpucc --qcpu-export (|x>|0> -> (-1)^f(x) |x>|0>), with its .json beside it."""
import argparse
import json
import math
import os
import sys
import time
import warnings

# qiskit-ibm-runtime warns that the noise figures of one of its device models are not representative; that model is
# loaded with all the others, and is not used here unless asked for
warnings.filterwarnings("ignore", message="Properties of fake_")

ROOT = os.path.dirname(os.path.abspath(__file__))
BACKENDS = ("simulator", "aer", "preview", "ibm")


class Error(Exception):
    pass


def load_config(path=None, backend=None):
    """config.json (cpu_qpu.load_config), with --backend over "backend"."""
    if path is None:                          # the repository's config.json, if you made one; else the defaults
        path = os.path.join(ROOT, "config.json")
    elif not os.path.exists(path):
        raise Error(f"{path}: no such file")
    sys.path.insert(0, ROOT)
    import cpu_qpu
    cfg = cpu_qpu.load_config(path)
    if backend:
        cfg["backend"] = backend
    if cfg["backend"] not in BACKENDS:
        raise Error(f"backend '{cfg['backend']}': use " + ", ".join(BACKENDS))
    return cfg


def load_oracle(qasm):
    """The exported phase oracle and the qubits of its argument (from the .json beside it)."""
    import qiskit.qasm3 as q3
    stem = qasm[:-len("_phase.qasm")] if qasm.endswith("_phase.qasm") else None
    info_path = (stem or qasm[:-5]) + ".json"
    if not os.path.exists(info_path):
        raise Error(f"{info_path}: not found; export the oracle with qcpucc --qcpu-mode qoracle --qcpu-export DIR")
    info = next((x for x in json.load(open(info_path)) if x["form"] == "phase"), None)
    if info is None or not stem:
        raise Error(f"{qasm}: give the phase oracle (NAME_phase.qasm), written for a function with a one-bit result")
    ins = [r for r in info["registers"] if r["role"] == "input"]
    if len(ins) != 1:
        raise Error(f"{info['function']}: the algorithms here take a function of one argument")
    reg = ins[0]
    return q3.load(qasm), list(range(reg["first"], reg["first"] + reg["bits"])), info


def build(kind, oracle, xs, iterations):
    """Bernstein-Vazirani or Grover's search around the phase oracle; measures the argument qubits."""
    from qiskit import QuantumCircuit
    n = len(xs)
    qc = QuantumCircuit(oracle.num_qubits, n, name=f"{kind}")
    qc.h(xs)
    for _ in range(iterations):
        qc.compose(oracle, range(oracle.num_qubits), inplace=True)
        if kind == "grover":                         # diffusion: reflection about the uniform superposition
            qc.h(xs)
            qc.x(xs)
            if n == 1:
                qc.z(xs[0])
            else:
                qc.h(xs[-1])
                qc.mcx(xs[:-1], xs[-1])
                qc.h(xs[-1])
            qc.x(xs)
            qc.h(xs)
    if kind == "bv":
        qc.h(xs)
    qc.measure(xs, range(n))
    return qc


def backend_for(cfg, min_qubits):
    """(backend object, its description) for config.json's backend."""
    if cfg["backend"] in ("simulator", "aer"):
        from qiskit_aer import AerSimulator
        method = cfg.get("aer", {}).get("method", "automatic") if cfg["backend"] == "aer" else "automatic"
        return AerSimulator(method=method), f"{cfg['backend']} (ideal, Qiskit Aer {method})"
    if cfg["backend"] == "preview":
        from qiskit_ibm_runtime import fake_provider
        name = cfg.get("preview", {}).get("device", "fez").replace("ibm_", "").replace("fake_", "")
        cls = getattr(fake_provider, "Fake" + name.capitalize(), None)
        if cls is None:
            raise Error(f"preview device '{name}' is not a known IBM device (for example fez, torino)")
        dev = cls()
        if dev.num_qubits < min_qubits:
            raise Error(f"the circuit needs {min_qubits} qubits, {dev.name} has {dev.num_qubits}: not run")
        return dev, f"preview: noise model of {dev.name} ({dev.num_qubits} qubits); no account used"
    from qiskit_ibm_runtime import QiskitRuntimeService
    ibm = cfg["ibm"]
    token = ibm.get("token")
    if token and token.startswith("env:"):
        token = os.environ.get(token[4:])
    if ibm.get("channel") != "local" and not token:
        where = ibm.get("token", "")
        raise Error("backend 'ibm' needs your IBM Quantum API key: \"options\": {\"ibm_token\": \"...\"} in "
                    "config.json (see config.example.json)"
                    + (f", or set the environment variable {where[4:]}" if where.startswith("env:") else ""))
    kw = {"channel": ibm.get("channel", "ibm_quantum_platform")}
    if kw["channel"] != "local":
        kw["token"] = token
        if ibm.get("instance"):
            kw["instance"] = ibm["instance"]
    try:
        service = QiskitRuntimeService(**kw)
        if ibm.get("backend") in (None, "least_busy"):
            dev = service.least_busy(min_num_qubits=min_qubits, operational=True, simulator=False)
        else:
            dev = service.backend(ibm["backend"])
    except Exception as e:
        raise Error(f"IBM Quantum: {type(e).__name__}: {e}")
    if dev.num_qubits < min_qubits:
        raise Error(f"the circuit needs {min_qubits} qubits, {dev.name} has {dev.num_qubits}: not submitted")
    return dev, f"ibm: {dev.name} ({dev.num_qubits} qubits)"


def sample(qc, dev, cfg, shots):
    """Transpile qc for dev and run it with the Sampler. Returns (counts, job id, two-qubit gates, depth, seconds)."""
    from qiskit import transpile
    from qiskit_ibm_runtime import SamplerV2
    t = transpile(qc, dev, optimization_level=int(cfg["ibm"].get("optimization_level", 1)), seed_transpiler=1)
    ops = t.count_ops()
    twoq = sum(v for k, v in ops.items() if k in ("cx", "cz", "ecr", "rzz"))
    t0 = time.time()
    job = SamplerV2(mode=dev).run([t], shots=shots)
    res = job.result()[0]
    counts = res.data[t.cregs[0].name].get_counts()
    return counts, (job.job_id() if hasattr(job, "job_id") else None), twoq, t.depth(), time.time() - t0


def run(kind, qasm, cfg, iterations=None, expect=None, shots=None):
    oracle, xs, info = load_oracle(qasm)
    n, N = len(xs), 1 << len(xs)
    if kind == "bv":
        iterations = 1
    elif iterations is None:
        iterations = max(1, int(math.pi / 4 * math.sqrt(N)))   # for one solution
    qc = build(kind, oracle, xs, iterations)
    dev, where = backend_for(cfg, oracle.num_qubits)
    shots = int(shots or cfg["shots"])
    counts, job_id, twoq, depth, elapsed = sample(qc, dev, cfg, shots)
    ranked = sorted(counts.items(), key=lambda kv: -kv[1])
    out = {"algorithm": kind, "oracle": os.path.abspath(qasm), "function": info["function"], "prototype": info["prototype"],
           "backend": where, "job_id": job_id, "shots": shots,
           "iterations": iterations, "argument_bits": n, "circuit_qubits": oracle.num_qubits,
           "two_qubit_gates": twoq, "depth": depth, "seconds": round(elapsed, 1),
           "results": [{"x": int(b, 2), "bits": b, "count": c} for b, c in ranked[:8]]}
    if expect is not None:
        out["expected"] = expect
        out["right_answer_fraction"] = counts.get(format(expect, f"0{n}b"), 0) / shots
        out["random_guess_fraction"] = 1 / N
    return out


def report(r):
    print(f"{r['algorithm']} with {r['function']} ({r['prototype']}) on {r['backend']}")
    print(f"  circuit: {r['circuit_qubits']} qubits, {r['iterations']} oracle call(s), {r['two_qubit_gates']} two-qubit "
          f"gates and depth {r['depth']} on the device; {r['shots']} shots in {r['seconds']} s"
          + (f"; job {r['job_id']}" if r["job_id"] else ""))
    for x in r["results"][:5]:
        print(f"  x = {x['x']:>6}  ({x['bits']})  {x['count']:>6} shots  {x['count'] / r['shots']:6.1%}")
    if "expected" in r:
        print(f"  the expected answer {r['expected']}: {r['right_answer_fraction']:.1%} of shots "
              f"(a random guess: {r['random_guess_fraction']:.1%})")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("algorithm", choices=("bv", "grover"),
                    help="bv: Bernstein-Vazirani (f(x) = parity(x & s): s in one call); grover: Grover's search")
    ap.add_argument("oracle", help="NAME_phase.qasm written by qcpucc --qcpu-export")
    ap.add_argument("--config",
                    help="configuration (default: config.json of this repository; see config.example.json)")
    ap.add_argument("--backend", choices=BACKENDS, help="overrides config.json's backend")
    ap.add_argument("--iterations", type=int, help="Grover iterations (default: for one solution)")
    ap.add_argument("--expect", type=lambda v: int(v, 0), help="the right answer, to report how often it was measured")
    ap.add_argument("--shots", type=int, help="overrides config.json")
    ap.add_argument("--save", metavar="FILE", help="also write the result as JSON")
    a = ap.parse_args(argv)
    try:
        cfg = load_config(a.config, a.backend)
        r = run(a.algorithm, a.oracle, cfg, a.iterations, a.expect, a.shots)
    except Error as e:
        print(f"qcpu_run: error: {e}", file=sys.stderr)
        return 1
    except ImportError as e:
        print(f"qcpu_run: error: {e.name} is not installed: python3 -m pip install qiskit qiskit-qasm3-import "
              "qiskit-aer qiskit-ibm-runtime", file=sys.stderr)
        return 1
    report(r)
    if a.save:
        json.dump(r, open(a.save, "w"), indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
