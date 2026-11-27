# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Quantag IT Solutions GmbH
"""A local stand-in for IBM Quantum's REST API, for testing libqarm without an account (tests/test_libqarm.py).

It serves the calls libqarm makes, with IBM's paths, headers and JSON:
    POST /identity/token                      IBM Cloud IAM: an API key for a bearer token
    POST /v3/resources/search                 Global Search: the CRN of an instance by name
    GET  /api/v1/backends/<dev>/configuration a fake device's real configuration (qiskit-ibm-runtime's files)
    GET  /api/v1/backends/<dev>/properties
    POST /api/v1/jobs                         a Sampler job: OpenQASM 3 pubs, checked to be ISA circuits
    GET  /api/v1/jobs/<id>                    Queued, Running, then Completed: the client has to poll
    GET  /api/v1/jobs/<id>/results            data.c.samples, as hex strings
    POST /api/v1/sessions, PATCH /api/v1/sessions/<id>
The circuits run on Aer: ideal, or with the fake device's noise model (noise=True)."""
import json
import os
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

KEY, TOKEN = "test-api-key", "test-bearer-token"
CRN = "crn:v1:bluemix:public:quantum-computing:eu-de:a/0123456789:instance-1::"
ISA = {"rz", "sx", "x", "cz", "measure", "barrier"}


def fake_backend(device):
    import qiskit_ibm_runtime.fake_provider as fp
    name = device.replace("ibm_", "")
    cls = getattr(fp, "Fake" + name.capitalize(), None)
    if cls is None:
        return None
    return cls()


class Mock:
    def __init__(self, noise=False, sessions=True, polls=2):
        self.noise, self.sessions, self.polls = noise, sessions, polls
        self.jobs, self.log = {}, []
        from qiskit import qasm3                          # imported first on the main thread: qiskit's first import
        from qiskit_aer import AerSimulator               # in a server thread crashes
        qasm3.loads('OPENQASM 3.0;\ninclude "stdgates.inc";\nbit[1] c;\nx $0;\nc[0] = measure $0;\n')
        AerSimulator()
        threading.stack_size(64 * 1024 * 1024)
        handler = self._handler()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def options(self, device="ibm_fez", instance="one"):
        return {"ibm_token": KEY, "instance": instance, "device": device, "url": self.url + "/api/v1",
                "iam_url": self.url, "search_url": self.url, "poll_seconds": 0.05}

    def close(self):
        self.server.shutdown()

    # ------------------------------------------------------------------
    def check_job(self, device, pubs):
        """The pubs as circuits, after checking that each is an ISA circuit for the device."""
        from qiskit import qasm3
        dev = fake_backend(device)
        edges = {tuple(e) for e in dev.configuration().coupling_map}
        out = []
        for pub in pubs:
            qasm, params, shots = (list(pub) + [None, None])[:3]
            if params is not None:
                raise ValueError("libqarm binds the parameters itself")
            qc = qasm3.loads(qasm)
            for inst in qc.data:
                name = inst.operation.name
                if name not in ISA:
                    raise ValueError(f"not an ISA circuit: the gate {name}")
                if name == "cz":
                    a, b = (qc.find_bit(q).index for q in inst.qubits)
                    if (a, b) not in edges:
                        raise ValueError(f"not an ISA circuit: cz on {a}, {b}, which are not connected")
            out.append((qc, int(shots)))
        return dev, out

    def run_job(self, dev, circuits):
        """Each circuit on its own qubits only, ideal or with the device's calibrated errors on them."""
        from qiskit import QuantumCircuit
        from qiskit_aer import AerSimulator
        from qiskit_aer.noise import NoiseModel, ReadoutError, depolarizing_error
        props = dev.properties() if self.noise else None
        results = []
        for qc, shots in circuits:
            used = sorted({qc.find_bit(q).index for inst in qc.data for q in inst.qubits})
            idx = {p: i for i, p in enumerate(used)}
            small = QuantumCircuit(len(used), qc.num_clbits)
            for inst in qc.data:
                small.append(inst.operation, [idx[qc.find_bit(q).index] for q in inst.qubits],
                             [qc.find_bit(c).index for c in inst.clbits])
            sim = AerSimulator()
            if props is not None:
                nm = NoiseModel(basis_gates=["cz", "rz", "sx", "x"])
                for p, i in idx.items():
                    for g in ("sx", "x"):
                        nm.add_quantum_error(depolarizing_error(min(props.gate_error(g, p), 0.5), 1), g, [i])
                    p01, p10 = props.qubit_property(p, "prob_meas1_prep0")[0], props.qubit_property(p, "prob_meas0_prep1")[0]
                    nm.add_readout_error(ReadoutError([[1 - p01, p01], [p10, 1 - p10]]), [i])
                for inst in qc.data:
                    if inst.operation.name == "cz":
                        a, b = (qc.find_bit(q).index for q in inst.qubits)
                        e = min(props.gate_error("cz", [a, b]), 0.5)
                        nm.add_quantum_error(depolarizing_error(e, 2), "cz", [idx[a], idx[b]])
                sim = AerSimulator(noise_model=nm)
            mem = sim.run(small, shots=shots, memory=True, seed_simulator=11).result().get_memory()
            results.append({"data": {"c": {"samples": [hex(int(m.replace(" ", ""), 2)) for m in mem],
                                           "num_bits": qc.num_clbits}}, "metadata": {"shots": shots}})
        return results

    def _handler(self):
        mock = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, code, obj=None):
                body = json.dumps(obj).encode() if obj is not None else b""
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(n).decode() if n else ""

            def api_auth(self):
                ok = (self.headers.get("Authorization") == f"Bearer {TOKEN}" and self.headers.get("Service-CRN") == CRN
                      and self.headers.get("IBM-API-Version"))
                if not ok:
                    self.reply(401, {"errors": [{"message": "bad credentials or headers"}]})
                return ok

            def handle_any(self, method):
                path, data = self.path.split("?")[0], self.body()
                mock.log.append((method, path, data))
                if method == "POST" and path == "/identity/token":
                    if f"apikey={KEY}" not in data or "grant-type%3Aapikey" not in data:
                        return self.reply(400, {"errorMessage": "Provided API key could not be found."})
                    return self.reply(200, {"access_token": TOKEN, "expires_in": 3600, "token_type": "Bearer"})
                if method == "POST" and path == "/v3/resources/search":
                    if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                        return self.reply(401, {"message": "unauthorized"})
                    return self.reply(200, {"items": [{"name": "other", "crn": "crn:v1:x"}, {"name": "one", "crn": CRN}]})
                if not path.startswith("/api/v1/"):
                    return self.reply(404, {"errors": [{"message": "no such path"}]})
                if not self.api_auth():
                    return
                parts = path[len("/api/v1/"):].split("/")
                if parts[0] == "backends" and len(parts) == 3 and method == "GET":
                    dev = fake_backend(parts[1])
                    if dev is None:
                        return self.reply(404, {"errors": [{"message": f"backend {parts[1]} not found"}]})
                    d = os.path.dirname(__import__(type(dev).__module__, fromlist=["x"]).__file__)
                    kind = "conf" if parts[2] == "configuration" else "props"
                    f = [x for x in os.listdir(d) if x.startswith(kind + "_") and x.endswith(".json")][0]
                    return self.reply(200, json.load(open(os.path.join(d, f))))
                if parts == ["jobs"] and method == "POST":
                    job = json.loads(data)
                    assert job["program_id"] == "sampler" and job["params"]["version"] == 2
                    try:
                        dev, circuits = mock.check_job(job["backend"], job["params"]["pubs"])
                    except Exception as e:
                        return self.reply(400, {"errors": [{"message": str(e)}]})
                    jid = "mock-" + uuid.uuid4().hex[:12]
                    entry = {"polls": 0, "results": None, "error": None, "session": job.get("session_id")}
                    mock.jobs[jid] = entry

                    def work():                                # the job runs after the POST has returned
                        try:
                            entry["results"] = mock.run_job(dev, circuits)
                        except Exception as e:
                            entry["error"] = str(e)
                    threading.Thread(target=work, daemon=True).start()
                    return self.reply(200, {"id": jid, "backend": job["backend"]})
                if parts[0] == "jobs" and len(parts) >= 2 and parts[1] in mock.jobs and method == "GET":
                    j = mock.jobs[parts[1]]
                    if len(parts) == 3 and parts[2] == "results":
                        return self.reply(200, {"results": j["results"], "metadata": {}})
                    j["polls"] += 1
                    if j["error"]:
                        return self.reply(200, {"id": parts[1], "state": {"status": "Failed", "reason": j["error"]}})
                    status = "Queued" if j["polls"] <= mock.polls // 2 else "Running" if j["polls"] <= mock.polls \
                        or j["results"] is None else "Completed"
                    return self.reply(200, {"id": parts[1], "state": {"status": status}})
                if parts == ["sessions"] and method == "POST":
                    if not mock.sessions:
                        return self.reply(403, {"errors": [{"message": "sessions are not available on the Open Plan"}]})
                    return self.reply(200, {"id": "session-" + uuid.uuid4().hex[:8], "mode": json.loads(data)["mode"]})
                if parts[0] == "sessions" and method == "PATCH":
                    return self.reply(204)
                return self.reply(404, {"errors": [{"message": f"{method} {path}: not in the mock"}]})

            def do_GET(self):
                self.handle_any("GET")

            def do_POST(self):
                self.handle_any("POST")

            def do_PATCH(self):
                self.handle_any("PATCH")
        return H
