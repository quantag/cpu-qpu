# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Quantag IT Solutions GmbH
# QArmAuto.so, the LLVM pass plugin: cmake -S compiler -B build/auto [-DLLVM_DIR=$(llvm-config --cmakedir)]
cmake_minimum_required(VERSION 3.16)
project(qarm_auto CXX)
find_package(LLVM REQUIRED CONFIG)
message(STATUS "QArmAuto for LLVM ${LLVM_PACKAGE_VERSION} (${LLVM_DIR})")
add_library(QArmAuto MODULE QArmAuto.cpp)
set_target_properties(QArmAuto PROPERTIES PREFIX "" CXX_STANDARD 17 CXX_STANDARD_REQUIRED ON)
target_include_directories(QArmAuto SYSTEM PRIVATE ${LLVM_INCLUDE_DIRS})
target_compile_definitions(QArmAuto PRIVATE ${LLVM_DEFINITIONS})
if(NOT LLVM_ENABLE_RTTI)
  target_compile_options(QArmAuto PRIVATE -fno-rtti)
endif()
target_compile_options(QArmAuto PRIVATE -Wall)
if(APPLE)
  target_link_options(QArmAuto PRIVATE -undefined dynamic_lookup)
endif()
