# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Quantag IT Solutions GmbH
#
#   cmake -B build                 score() on the quantum CPU (qcpucc)
#   cmake -B build -DQCPU=OFF      an ordinary program: the same source, everything on the CPU
#   cmake -B build -DQCPU_MODE=qoracle    score() as its own small circuit (needs qoracle)
cmake_minimum_required(VERSION 3.20)
project(codebreaker C)
option(QCPU "run __qcpu__ functions on the quantum CPU" ON)
set(QCPU_MODE "vcpu" CACHE STRING "vcpu (the virtual CPU) or qoracle (a circuit per function): qcpucc --qcpu-mode")
if(QCPU)
  include(${CMAKE_CURRENT_LIST_DIR}/../../cmake/QCPU.cmake)
  add_qcpu_executable(codebreaker SOURCES codebreaker.c QCPU_MODE ${QCPU_MODE})
else()
  add_executable(codebreaker codebreaker.c)
  target_include_directories(codebreaker PRIVATE ${CMAKE_CURRENT_LIST_DIR}/../../runtime)
endif()
