#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Quantag IT Solutions GmbH
"""Make a shared library (Linux .so, Windows .dll) whose functions run on the quantum machine.

    python3 qpu_dll.py mathlib.c --header mathlib.h -o libmathlib_qpu.so            # AArch64 code (default)
    python3 qpu_dll.py mathlib.c --header mathlib.h --arch riscv64 -o libmathlib_qpu.so
    python3 qpu_dll.py mathlib.c --header mathlib.h -o mathlib_qpu.dll              # on Windows
    python3 qpu_dll.py mathlib.c --header mathlib.h --target windows -o mathlib_qpu.dll   # from Linux (MinGW)

The functions declared in the header are compiled for the QPU (AArch64, RISC-V or x86-64). This tool
then builds the quantum machine's circuit for that code, once, and puts it into the library as data:
the gate list of one machine cycle, the initial state (the program in program memory, the data,
the entry state), and the qubits that hold the arguments, the result, data memory and the PC. The
library also contains a small C engine that runs those gates, and the same functions compiled for
the host, to check each call.

The library exports the header's functions with the same signatures. A host program links against
it and calls them as ordinary C functions; at run time there is no Python, no server and no network:

    cc main.c -L. -lmathlib_qpu -Wl,-rpath,'$ORIGIN' -o main          Linux
    clang main.c mathlib_qpu.lib -o main.exe                         Windows (or gcc main.c mathlib_qpu.dll)

Supported signatures: integer parameters and return values (char, short, int, long, their unsigned
forms, intN_t / uintN_t, void return), and pointers to int buffers annotated with QPU_IN(count),
QPU_OUT(count) or QPU_INOUT(count) from runtime/qpu_dll.h. Values must fit the register width of the
machine (--width, default 16 bits); a call where one does not is reported. The library functions
cannot print (printf, puts, putchar).
"""
import argparse, os, re, shutil, subprocess, sys, tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
import cpu_qpu
from qcpu import isa as isas
from qcpu.loader import ObjectImage, LinkError, DATA_BASE

INT_TYPES = {"char", "signed char", "unsigned char", "short", "unsigned short", "int", "unsigned", "unsigned int",
             "signed", "signed int", "long", "unsigned long", "long long", "unsigned long long", "short int",
             "long int", "unsigned short int", "unsigned long int", "_Bool", "bool",
             "int8_t", "int16_t", "int32_t", "int64_t", "uint8_t", "uint16_t", "uint32_t", "uint64_t",
             "intptr_t", "size_t", "ssize_t"}
BUF_TYPES = {"int", "unsigned", "unsigned int", "signed", "signed int", "int32_t", "uint32_t"}
ANNOT = re.compile(r"\bQC?PU_(IN|OUT|INOUT)\s*\(")
OPS = {"x": 0, "cx": 1, "ccx": 2, "cswap": 3}


class Param:
    def __init__(self, ctype, name, direction=None, count=None):
        self.ctype, self.name, self.direction, self.count = ctype, name, direction, count


def norm(t):
    t = re.sub(r"\b(const|volatile|extern|static|inline|register|restrict|__restrict)\b", " ", t)
    return " ".join(t.split())


def take_annotation(text):
    """'const int *QPU_IN(2*n) v' -> ('const int * v', 'IN', '2*n')"""
    m = ANNOT.search(text)
    if not m:
        return text, None, None
    depth, i = 1, m.end()
    while depth:
        if i >= len(text):
            raise ValueError(f"unbalanced parentheses in '{text}'")
        depth += {"(": 1, ")": -1}.get(text[i], 0)
        i += 1
    return text[:m.start()] + text[i:], m.group(1), text[m.end():i - 1].strip()


def parse_header(path):
    """Function prototypes of the header: [(name, return type, [Param])]."""
    src = open(path).read()
    src = re.sub(r"/\*.*?\*/", " ", src, flags=re.S)
    src = re.sub(r"//[^\n]*", " ", src)
    src = re.sub(r"^\s*#[^\n]*(\\\n[^\n]*)*", " ", src, flags=re.M)        # preprocessor lines
    funcs = []
    for decl in src.split(";"):
        decl = " ".join(decl.split())
        if "(" not in decl or decl.startswith(("typedef", "struct", "enum", "union")) or "{" in decl:
            continue
        m = re.match(r"^(.*?)\b([A-Za-z_]\w*)\s*\((.*)\)\s*$", decl)
        if not m:
            raise ValueError(f"cannot read the declaration '{decl}'")
        ret, name, plist = norm(m.group(1)), m.group(2), m.group(3).strip()
        if ret != "void" and ret not in INT_TYPES:
            raise ValueError(f"{name}: return type '{ret}' is not supported (integers or void)")
        params = []
        if plist and plist != "void":
            depth, cur, parts = 0, "", []
            for ch in plist:                     # split at top-level commas (annotations contain parentheses)
                if ch == "," and depth == 0:
                    parts.append(cur); cur = ""
                    continue
                depth += {"(": 1, ")": -1}.get(ch, 0)
                cur += ch
            parts.append(cur)
            for i, p in enumerate(parts):
                p, direction, count = take_annotation(p.strip())
                pm = re.match(r"^(.*?)(\**)\s*([A-Za-z_]\w*)$", " ".join(p.split()))
                if not pm or not pm.group(1).strip():
                    raise ValueError(f"{name}: parameter {i + 1} ('{p}') needs a type and a name")
                base, stars, pname = norm(pm.group(1).replace("*", " ")), pm.group(2) + "*" * pm.group(1).count("*"), pm.group(3)
                if stars == "":
                    if base not in INT_TYPES:
                        raise ValueError(f"{name}: parameter '{pname}' has type '{base}' (integers only)")
                    if direction:
                        raise ValueError(f"{name}: QPU_{direction} on '{pname}', which is not a pointer")
                    params.append(Param(base, pname))
                elif stars == "*" and base in BUF_TYPES and direction:
                    params.append(Param(" ".join(pm.group(1).split()) + " *", pname, direction, count))
                elif stars == "*" and base in BUF_TYPES:
                    raise ValueError(f"{name}: pointer '{pname}' needs QPU_IN(count), QPU_OUT(count) or QPU_INOUT(count) "
                                     "(the QPU cannot read host memory, so the buffer is copied)")
                else:
                    raise ValueError(f"{name}: parameter '{pname}' ({base} {stars}): only int buffers can be passed by pointer")
        funcs.append((name, ret, params))
    if not funcs:
        raise ValueError(f"no function declarations in {path}")
    return funcs



class BuildError(Exception):
    pass


def build_machine(obj_path, isa, funcs, W=16, mem_cells=512, stack_bytes=None, roots=None):
    """Link the library and build the quantum machine for it. Returns a dict with the machine m, the
    initial state (qubits that are 1), the cycle template, the function entry PCs and the layout."""
    cfg = cpu_qpu.load_config(os.path.join(ROOT, "config.json"))
    try:
        obj = ObjectImage(obj_path, isa.name, roots)
        prog = obj.link(funcs[0][0])
    except LinkError as e:
        raise BuildError(str(e))
    if prog["uses_output"]:
        raise BuildError("the library calls printf / puts / putchar; functions in a QPU library cannot print")
    if prog["unresolved"]:
        raise BuildError("unresolved symbols: " + "; ".join(prog["unresolved"]))
    for name, _, params in funcs:
        if name not in prog["functions"]:
            raise BuildError(f"{name} is declared in the header but not defined in the library")
        if len(params) > len(isa.arg_regs):
            raise BuildError(f"{name} has {len(params)} parameters; {isa.title} passes at most {len(isa.arg_regs)} in registers")
    code = prog["words"]
    slots = isa.min_slots
    while slots < len(code) + 1:
        slots *= 2
    words = isa.layout(code, slots)
    listing = isa.listing(code, slots, 0x400000)
    bad = [e for e in listing if not e["check"].ok]
    if mem_cells < 16 or mem_cells & (mem_cells - 1):
        raise BuildError("--memory must be a power of two, at least 16")
    user_off = (prog["data_end"] + 15) // 16 * 16
    stack = int(cfg["stack_bytes"]) if stack_bytes is None else stack_bytes
    sp_off = 4 * mem_cells - 64
    user_cells = (sp_off - stack - user_off) // 4
    if user_cells < 0:
        raise BuildError(f"--memory {mem_cells} is too small for the library's data and stack")
    need_w = max((isa.unit * slots - 1).bit_length() + 1, (4 * mem_cells).bit_length())
    if not need_w <= W <= 16:
        raise BuildError(f"the code ({len(code)} {'bytes' if isa.unit == 1 else 'instructions'}) and data memory "
                         f"({mem_cells} ints) need a register width of at least {need_w} (--width is {W}, at most 16)")
    nargs = max([len(p) for _, _, p in funcs] + [0])
    used = set(isa.arg_regs[:max(nargs, 1)]) | {isa.ret_reg} | set(isa.entry_regs)
    for e in listing:
        if e["check"].ok:
            used |= set(e["check"].reads) | set(e["check"].writes)
    m = isa.Machine(isa.config(W, used, slots, 1, mem_cells, isa.ALL_CLASSES, registers="auto", select="tree"), defer=True)
    cells = [0] * mem_cells
    img = prog["data_image"]
    for i in range(0, len(img), 4):
        cells[(prog["data_offset"] + i) // 4] = int.from_bytes(img[i:i + 4], "little")
    init, cells0 = isa.entry({}, cells, slots, sp_off, isa.sp_reg in m.X)     # every function returns to the halt slot
    ones = m.load_bits(words, init, cells0, pc=0)
    tmpl, pc_in, run_in, npc, run_out, trap, step_ids = m.cycle_template()
    ops = {g[0] for g in tmpl}
    if not ops <= set(OPS):
        raise BuildError(f"unexpected gates in the cycle: {ops - set(OPS)}")
    return dict(m=m, isa=isa, W=W, ones=ones, tmpl=tmpl, pc_in=pc_in, run_in=run_in, npc=npc, run_out=run_out,
                trap=trap, step_ids=step_ids, entry={n: prog["functions"][n] for n, _, _ in funcs}, slots=slots,
                mem_cells=mem_cells, user_cell=user_off // 4, user_cells=user_cells, nargs=nargs, code=code,
                unsupported=bad, data_base=DATA_BASE)


def c_array(name, values, ctype="uint32_t"):
    vals = list(values) or [0]
    rows = [", ".join(map(str, vals[i:i + 24])) for i in range(0, len(vals), 24)]
    return f"static const {ctype} {name}[] = {{\n" + ",\n".join(rows) + "\n};\n"


def wrappers(funcs, entry, native, circuits):
    """C lines: the declarations of the native bodies and one exported wrapper per function."""
    L = []
    if native:
        for name, ret, params in funcs:
            L.append(f"{ret} qpu__native_{name}({', '.join(p.ctype + ' ' + p.name for p in params) or 'void'});")
        L.append("")
    for name, ret, params in funcs:       # locals are named qpu__*, so they cannot clash with parameter names
        sig = ", ".join(f"{p.ctype} {p.name}" for p in params) or "void"
        bufs = [(i, p) for i, p in enumerate(params) if p.direction]
        L.append(f"QPU__EXPORT {ret} {name}({sig}) {{")
        L.append("    qpu__ctx qpu__c;")
        if params:
            L.append("    long long qpu__a[%d] = {%s};" % (len(params), ", ".join("0" if p.direction else f"(long long){p.name}" for p in params)))
        if bufs:
            L.append("    qpu__buf qpu__b[%d] = {%s};" % (len(bufs), ", ".join(
                f"{{{i}, QPU__{p.direction}, (int *){p.name}, (long long)({p.count})}}" for i, p in bufs)))
        circ = f"&{circuits[name]}" if name in circuits else "NULL"
        L.append(f'    qpu__start_c(&qpu__c, &qpu__L, {circ}, "{name}", {entry[name]}L, {len(params)}, '
                 f'{"qpu__a" if params else "NULL"}, {"qpu__b" if bufs else "NULL"}, {len(bufs)});')
        L.append("    long long qpu__n = 0;")
        if native:
            bidx = {i: k for k, (i, _) in enumerate(bufs)}
            args = ", ".join(f"qpu__native_copy(&qpu__c, {bidx[i]})" if p.direction else p.name for i, p in enumerate(params))
            L.append("    if (qpu__c.need_native) {")
            if ret == "void":
                L.append(f"        qpu__native_{name}({args});")
            else:
                L.append(f"        {ret} qpu__r = qpu__native_{name}({args});")
                L.append(f'        qpu__check(&qpu__c, ({ret})qpu__c.ret == qpu__r, "return value");')
                L.append("        qpu__n = (long long)qpu__r;")
            L.append("    }")
        L.append(f"    {'' if ret == 'void' else f'return ({ret})'}qpu__finish(&qpu__c, qpu__n);")
        L.append("}")
    return L


def search_table(searches, funcs):
    """C lines: the qcpu_search() table and qcpu_search(). searches: (function, C name of its phase circuit, bits);
    every other function of one argument is listed too, without a circuit, so that a search names it."""
    L = ["", "static const qpu__search_entry qpu__searchable[] = {"]
    L += [f'    {{"{n}", (void (*)(void)){n}, &{var}, {bits}}},' for n, var, bits in searches]
    have = {n for n, _, _ in searches}
    L += [f'    {{"{n}", (void (*)(void)){n}, NULL, 0}},' for n, _, params in funcs if len(params) == 1 and n not in have]
    L += ["    {NULL, NULL, NULL, 0}", "};",
          "QPU__EXPORT int qcpu_search(int (*f)(int), int bits, int *x) { return qpu__search(qpu__searchable, f, bits, x); }"]
    return L


def library_source(libname, header, funcs, B, max_steps, native, circuits=None, extra_c="", qoracle_first=False,
                   searches=()):
    """The C file of the library: the engine, the circuit as data, and one wrapper per function.
    circuits: {function: C name of its qpu__circuit}, defined by extra_c (qcpucc --qcpu-mode qoracle); B is None
    when the library has no quantum CPU. qoracle_first: QCPU_MODE defaults to qoracle (--qcpu-mode qoracle,qcpu)."""
    circuits = circuits or {}
    if B is None:
        L = ["/* Generated by qpu_dll.py (qcpucc --qcpu-mode qoracle): one circuit per function, no quantum CPU. */",
             '#include "qpu_engine.c"', f'#include "{os.path.basename(header)}"', "",
             "static const qpu__lib qpu__L = {",
             f'    "{libname}", 0, 0u, NULL, 0, NULL, 0, NULL, 0, NULL, NULL, 0, 0u, 0u, 0u, NULL, 0, NULL, NULL, 0, 0, 0,',
             f"    {max_steps}L, {1 if native else 0}, 1", "};", "", extra_c]
        return "\n".join(L + wrappers(funcs, {n: 0 for n, _, _ in funcs}, native, circuits) + search_table(searches, funcs)) + "\n"
    m, W, isa = B["m"], B["W"], B["isa"]
    gates = []
    for g in B["tmpl"]:
        op = OPS[g[0]]
        q = list(g[1:]) + [0] * (4 - len(g))
        gates += [op << 30 | q[0], q[1], q[2]]
    argq = [q for i in range(B["nargs"]) for q in m.X[isa.arg_regs[i]]]
    L = ["/* Generated by qpu_dll.py. The quantum machine of this library as data, the engine that runs it,",
         f" * and the exported functions. QPU code: {isa.title}; machine: W = {W}, {B['slots']} program slots,",
         f" * {B['mem_cells']} data cells, {m.n} qubits, {len(B['tmpl'])} gates per cycle. */",
         '#include "qpu_engine.c"', f'#include "{os.path.basename(header)}"', "",
         c_array("qpu__gates", gates), c_array("qpu__ones", sorted(B["ones"])), c_array("qpu__step", B["step_ids"]),
         c_array("qpu__pc_in", B["pc_in"]), c_array("qpu__pc_next", B["npc"]), c_array("qpu__argq", argq),
         c_array("qpu__retq", m.X[isa.ret_reg]), c_array("qpu__ramq", [q for c in m.ram for q in c]),
         "static const qpu__lib qpu__L = {",
         f'    "{libname}", {W}, {m.n}u, qpu__gates, {len(B["tmpl"])}, qpu__ones, {len(B["ones"])}, qpu__step, {len(B["step_ids"])},',
         f"    qpu__pc_in, qpu__pc_next, {len(B['pc_in'])}, {B['run_in']}u, {B['run_out']}u, {B['trap']}u,",
         f"    qpu__argq, {B['nargs']}, qpu__retq, qpu__ramq, {B['mem_cells']}, {B['user_cell']}, {B['user_cells']},",
         f"    {max_steps}L, {1 if native else 0}, {1 if qoracle_first else 0}", "};", "", extra_c]
    L += wrappers(funcs, B["entry"], native, circuits) + search_table(searches, funcs)
    L += ["", "#ifdef QPU__SELFTEST",
          "QPU__EXPORT long qpu__selftest(long entry, int nargs, const long long *args, unsigned char *out) {",
          "    return qpu__selftest_run(&qpu__L, entry, nargs, args, out);", "}",
          f"QPU__EXPORT unsigned long qpu__selftest_qubits(void) {{ return {m.n}ul; }}", "#endif"]
    return "\n".join(L) + "\n"


def host_compiler(target, native):
    """A C compiler for the library on this machine."""
    if target == "linux":
        return "cc"
    names = ["clang", "gcc", "cl"] if native == "windows" else ["x86_64-w64-mingw32-gcc", "x86_64-w64-mingw32-clang"]
    for n in names:
        if shutil.which(n):
            return n
    sys.exit("error: no C compiler for Windows found (tried " + ", ".join(names) + "); pass --host-cc")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", help="C file of the library (an object file also works, without the host check)")
    ap.add_argument("--header", required=True, help="header that declares the functions to export")
    ap.add_argument("-o", "--output", help="library to write (default: libNAME_qpu.so / NAME_qpu.dll)")
    ap.add_argument("--arch", help="QPU architecture: " + " | ".join(isas.NAMES) + " (default arm64)")
    ap.add_argument("--cc", help="compiler for the QPU code: clang | gcc | command")
    ap.add_argument("-O", dest="opt", default="1", help="optimization level for the QPU code (default 1)")
    ap.add_argument("--width", type=int, default=16, help="register width of the quantum machine, 8..16 (default 16)")
    ap.add_argument("--memory", type=int, default=512, help="data memory in ints, a power of two (default 512)")
    ap.add_argument("--max-steps", type=int, default=1000000, help="machine cycles before a call is stopped (default 1000000)")
    ap.add_argument("--target", choices=("linux", "windows"), help="system the library is for (default: this one; "
                    "windows when --host-cc is a MinGW compiler)")
    ap.add_argument("--host-cc", help="C compiler for the library: Linux cc; Windows clang, gcc or cl; "
                    "from Linux to Windows x86_64-w64-mingw32-gcc (default: $CC, else found automatically)")
    ap.add_argument("--keep", action="store_true", help="keep the generated C file next to the library")
    ap.add_argument("--selftest", action="store_true", help=argparse.SUPPRESS)
    a = ap.parse_args()
    native_os = "windows" if os.name == "nt" else "linux"
    target = a.target or ("windows" if a.host_cc and "mingw" in a.host_cc else native_os)
    host_cc = a.host_cc or os.environ.get("CC") or host_compiler(target, native_os)
    try:
        funcs = parse_header(a.header)
    except (ValueError, OSError) as e:
        sys.exit(f"error: {a.header}: {e}")
    cfg = cpu_qpu.load_config(os.path.join(ROOT, "config.json"))
    from_c = a.source.endswith(".c")
    if from_c:
        isa = isas.get(a.arch or cfg["arch"])
        cflags = [f for f in cfg["cflags"] if not f.startswith("-O")] + [f"-O{a.opt}"]
        obj_path, _ = cpu_qpu.compile_c(a.source, a.cc or cfg["compiler"], cflags, isa.name)
    else:
        obj_path = a.source
        try:
            found = isas.detect(obj_path)
        except (ValueError, OSError) as e:
            sys.exit(f"error: {e}")
        if a.arch and isas.get(a.arch).name != found:
            sys.exit(f"error: {obj_path} is a {found} file, but --arch {a.arch} was given")
        isa = isas.get(found)
        print("note: built from an object file, so the library cannot check calls against a host build", file=sys.stderr)
    try:
        B = build_machine(obj_path, isa, funcs, a.width, a.memory)
    except BuildError as e:
        sys.exit(f"error: {e}")
    if B["unsupported"]:
        print(f"warning: {len(B['unsupported'])} instruction(s) the machine does not implement; a call that reaches "
              "one stops with an error: " + "; ".join(e["asm"] for e in B["unsupported"][:5]), file=sys.stderr)
    base = os.path.splitext(os.path.basename(a.source))[0]
    out = a.output or (f"{base}_qpu.dll" if target == "windows" else f"lib{base}_qpu.so")
    if os.path.dirname(out):
        os.makedirs(os.path.dirname(out), exist_ok=True)
    implib = os.path.splitext(out)[0] + ".lib" if target == "windows" else None
    src = library_source(base, a.header, funcs, B, a.max_steps, from_c)
    includes = [os.path.join(ROOT, "runtime"), os.path.dirname(os.path.abspath(a.header))]
    with tempfile.TemporaryDirectory() as d:
        gen = os.path.splitext(out)[0] + ".gen.c" if a.keep else os.path.join(d, "library.c")
        open(gen, "w").write(src)
        native_src = []
        if from_c:                      # the same source for the host, with the exported names prefixed
            native_src = [a.source]
        renames = [f"{n}=qpu__native_{n}" for n, _, _ in funcs]
        cmds = []
        objs = []
        for i, (f, defs) in enumerate([(gen, ["QPU__SELFTEST"] if a.selftest else [])] + [(s, renames) for s in native_src]):
            o = os.path.join(d, f"part{i}.o")
            cmds.append(compile_one(host_cc, target, f, o, includes, defs))
            objs.append(o)
        cmds.append(link_command(host_cc, target, objs, out, implib))
        for cmd in cmds:
            p = subprocess.run(cmd, capture_output=True, text=True)
            if p.returncode:
                sys.exit("error: building the library failed:\n" + " ".join(cmd) + "\n" + p.stdout + p.stderr)
    m = B["m"]
    extra = f" and {implib}" if implib and os.path.exists(implib) else ""
    print(f"{out}{extra}: {len(funcs)} function(s) on the QPU ({isa.title}, W = {B['W']}, {m.n} qubits, "
          f"{len(B['tmpl'])} gates per cycle){'' if from_c else ', no host check'}: " + ", ".join(f[0] for f in funcs))


def compile_one(cc, target, src, obj, includes, defines):
    name = os.path.basename(cc).lower().removesuffix(".exe")
    if name in ("cl", "clang-cl"):
        return [cc, "/nologo", "/c", "/O2", "/W3"] + [f"/I{i}" for i in includes] + [f"/D{d}" for d in defines] + [src, f"/Fo{obj}"]
    pic = ["-fPIC", "-fvisibility=hidden"] if target == "linux" else []
    return [cc, "-c", "-O2", "-Wall"] + pic + [x for i in includes for x in ("-I", i)] + [f"-D{d}" for d in defines] + [src, "-o", obj]


def link_command(cc, target, objs, out, implib):
    name = os.path.basename(cc).lower().removesuffix(".exe")
    if name in ("cl", "clang-cl"):
        return [cc, "/nologo", "/LD"] + objs + [f"/Fe{out}", "/link", f"/IMPLIB:{implib}"]
    if target == "linux":
        return [cc, "-shared"] + objs + ["-o", out, "-lpthread"]
    flavor = subprocess.run([cc, "-dumpmachine"], capture_output=True, text=True).stdout
    return [cc, "-shared"] + objs + ["-o", out] + ([] if "msvc" in flavor else [f"-Wl,--out-implib,{implib}"])


if __name__ == "__main__":
    main()
