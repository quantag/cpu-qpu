OPENQASM 2.0;
include "qelib1.inc";
qreg q[1];
creg result[1];
h q[0];
t q[0];
h q[0];
measure q -> result;
