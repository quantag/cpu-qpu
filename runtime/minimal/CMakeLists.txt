# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Quantag IT Solutions GmbH
#
# libqarm-minimal.so: a minimal Q-ARM runtime (Core and Circuit profiles), the companion of
# docs/qarm_runtime_guide.md. Needs QEMU >= 10.1's qemu-plugin.h and glib:
#   cmake -S runtime/minimal -B build/minimal -DQEMU_PLUGIN_INCLUDE=<dir with qemu-plugin.h>
#   cmake --build build/minimal && qemu-aarch64 -plugin build/minimal/libqarm-minimal.so ./program
cmake_minimum_required(VERSION 3.16)
project(qarm_minimal C)
set(QEMU_PLUGIN_INCLUDE "" CACHE PATH "The directory with qemu-plugin.h (QEMU 10.1 or newer)")
find_path(QEMU_PLUGIN_DIR qemu-plugin.h HINTS ${QEMU_PLUGIN_INCLUDE} PATH_SUFFIXES qemu)
if(NOT QEMU_PLUGIN_DIR)
  message(FATAL_ERROR "qemu-plugin.h not found: set QEMU_PLUGIN_INCLUDE to QEMU's include directory")
endif()
find_package(PkgConfig REQUIRED)
pkg_check_modules(GLIB REQUIRED glib-2.0)
add_library(qarm-minimal MODULE qarm_minimal.c)
set_target_properties(qarm-minimal PROPERTIES PREFIX "lib" C_STANDARD 11 POSITION_INDEPENDENT_CODE ON)
# only qarm_isa.h and qarm_sv.h: include/ also holds the minimal stdio.h of the QPU programs, which must not shadow libc's
foreach(h qarm_isa.h qarm_sv.h)
  configure_file(${CMAKE_CURRENT_SOURCE_DIR}/../../include/${h} ${CMAKE_CURRENT_BINARY_DIR}/include/${h} COPYONLY)
endforeach()
target_include_directories(qarm-minimal PRIVATE ${QEMU_PLUGIN_DIR} ${GLIB_INCLUDE_DIRS} ${CMAKE_CURRENT_BINARY_DIR}/include)
if(NOT WIN32)
  target_link_libraries(qarm-minimal PRIVATE m)
endif()
