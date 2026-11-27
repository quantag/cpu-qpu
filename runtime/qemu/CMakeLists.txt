# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Quantag IT Solutions GmbH
#
# libqarm-qemu.so: Q-ARM for qemu-aarch64 (docs/qarm_v2.md, section 8). Needs QEMU >= 10.1's qemu-plugin.h:
#   cmake -S runtime/qemu -B build/qemu -DQEMU_PLUGIN_INCLUDE=<dir with qemu-plugin.h> && cmake --build build/qemu
#   qemu-aarch64 -plugin build/qemu/libqarm-qemu.so ./program
cmake_minimum_required(VERSION 3.16)
project(qarm_qemu C)

set(QEMU_PLUGIN_INCLUDE "" CACHE PATH "The directory with qemu-plugin.h (QEMU 10.1 or newer)")
find_path(QEMU_PLUGIN_DIR qemu-plugin.h HINTS ${QEMU_PLUGIN_INCLUDE} PATH_SUFFIXES qemu)
if(NOT QEMU_PLUGIN_DIR)
  message(FATAL_ERROR "qemu-plugin.h not found: set QEMU_PLUGIN_INCLUDE to QEMU's include directory")
endif()
file(STRINGS ${QEMU_PLUGIN_DIR}/qemu-plugin.h has_write REGEX "qemu_plugin_write_register")
if(NOT has_write)
  message(FATAL_ERROR "${QEMU_PLUGIN_DIR}/qemu-plugin.h has no qemu_plugin_write_register: QEMU 10.1 or newer is needed")
endif()
find_package(PkgConfig REQUIRED)
pkg_check_modules(GLIB REQUIRED glib-2.0)

set(CMAKE_POSITION_INDEPENDENT_CODE ON)
set(QARM_EXAMPLES OFF CACHE BOOL "" FORCE)
add_subdirectory(${CMAKE_CURRENT_SOURCE_DIR}/../libqarm libqarm)

add_library(qarm-qemu MODULE qarm_qemu.c)
set_target_properties(qarm-qemu PROPERTIES PREFIX "lib" C_STANDARD 11)
target_include_directories(qarm-qemu PRIVATE ${QEMU_PLUGIN_DIR} ${GLIB_INCLUDE_DIRS})
target_link_libraries(qarm-qemu PRIVATE qarm)       # glib's symbols come from QEMU itself
get_filename_component(QARM_TOOLS_DIR ${CMAKE_CURRENT_SOURCE_DIR}/../../tools ABSOLUTE)
target_compile_definitions(qarm-qemu PRIVATE QARM_TOOLS_DIR="${QARM_TOOLS_DIR}")
