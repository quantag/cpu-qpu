# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Quantag IT Solutions GmbH
#
# libqarm, the native Q-ARM runtime (docs/qarm_v2.md, section 7):
#   cmake -S runtime/libqarm -B build/libqarm && cmake --build build/libqarm
# Links a program to it with target_link_libraries(prog qarm): QARM_RUNTIME is defined for it.
# Without libcurl (found by find_package(CURL)) it is built without the ibm backend.
cmake_minimum_required(VERSION 3.16)
project(libqarm C)

add_library(qarm STATIC qarm_rt.c qarm_sim.c qarm_json.c qarm_ibm.c)
# only qarm.h, qarm_rt.h and qarm_sv.h: include/ also holds the minimal stdio.h of the QPU programs, which must not shadow libc's
foreach(h qarm.h qarm_rt.h qarm_sv.h)
  configure_file(${CMAKE_CURRENT_SOURCE_DIR}/../../include/${h} ${CMAKE_CURRENT_BINARY_DIR}/include/${h} COPYONLY)
endforeach()
target_include_directories(qarm PUBLIC ${CMAKE_CURRENT_BINARY_DIR}/include)
target_compile_definitions(qarm PUBLIC QARM_RUNTIME)
set_property(TARGET qarm PROPERTY C_STANDARD 99)
find_package(CURL QUIET)
if(CURL_FOUND)
  target_link_libraries(qarm PUBLIC CURL::libcurl)
else()
  target_compile_definitions(qarm PRIVATE QARM_NO_CURL)
  message(STATUS "libqarm: libcurl not found: no ibm backend (exact and simulator only)")
endif()
if(NOT WIN32)
  target_link_libraries(qarm PUBLIC m)
endif()

option(QARM_EXAMPLES "Build examples/qarm/h2_vqe.c against libqarm" ON)
if(QARM_EXAMPLES)
  add_executable(h2_vqe ${CMAKE_CURRENT_SOURCE_DIR}/../../examples/qarm/h2_vqe.c)
  target_link_libraries(h2_vqe PRIVATE qarm)
endif()
