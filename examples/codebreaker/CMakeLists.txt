# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Quantag IT Solutions GmbH
cmake_minimum_required(VERSION 3.20)
project(codebreaker C)
include(${CMAKE_CURRENT_LIST_DIR}/../../cmake/QCPU.cmake)
add_qcpu_executable(codebreaker SOURCES codebreaker.c)
