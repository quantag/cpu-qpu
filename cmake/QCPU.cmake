# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Quantag IT Solutions GmbH
#
# CMake functions for programs with __qcpu__ functions (docs/qcpu_spec.md). They run qcpucc, which
# needs Python 3 and clang at build time; the result runs without Python.
#
#   include(path/to/cpu-qpu/cmake/QCPU.cmake)
#   add_qcpu_executable(game SOURCES game.c [QCPU_ISA arm64] [QCPU_WIDTH 16] [QCPU_MEMORY 512]
#                       [QCPU_MAX_STEPS 1000000] [QCPU_MODE qcpu|qoracle]
#                       [OPTIONS -O2 ...] [LIBS m ...])
#   add_qcpu_library(codebreaker SOURCES codebreaker.c [same options])
#       a shared library and an imported target "codebreaker" that other targets link with:
#   target_link_libraries(other PRIVATE codebreaker)

find_package(Python3 REQUIRED COMPONENTS Interpreter)
set(QCPU_ROOT "${CMAKE_CURRENT_LIST_DIR}/.." CACHE PATH "cpu-qpu repository")
set(QCPU_CC "${Python3_EXECUTABLE}" "${QCPU_ROOT}/qcpucc.py")

function(_qcpu_command out_var name)
  cmake_parse_arguments(Q "" "QCPU_ISA;QCPU_WIDTH;QCPU_MEMORY;QCPU_MAX_STEPS;QCPU_MODE" "SOURCES;OPTIONS;LIBS" ${ARGN})
  if(NOT Q_SOURCES)
    message(FATAL_ERROR "add_qcpu_*(${name}): SOURCES is required")
  endif()
  set(args)
  if(Q_QCPU_ISA)
    list(APPEND args --qcpu-isa ${Q_QCPU_ISA})
  endif()
  if(Q_QCPU_WIDTH)
    list(APPEND args --qcpu-width ${Q_QCPU_WIDTH})
  endif()
  if(Q_QCPU_MEMORY)
    list(APPEND args --qcpu-memory ${Q_QCPU_MEMORY})
  endif()
  if(Q_QCPU_MODE)
    list(APPEND args --qcpu-mode ${Q_QCPU_MODE})
  endif()
  if(Q_QCPU_MAX_STEPS)
    list(APPEND args --qcpu-max-steps ${Q_QCPU_MAX_STEPS})
  endif()
  set(srcs)
  foreach(s ${Q_SOURCES})
    get_filename_component(abs "${s}" ABSOLUTE)
    list(APPEND srcs "${abs}")
  endforeach()
  set(libs)
  foreach(l ${Q_LIBS})
    list(APPEND libs "-l${l}")
  endforeach()
  # the host side uses CMake's C compiler when it takes GCC-style options (gcc, clang; not MSVC cl)
  if(CMAKE_C_COMPILER AND CMAKE_C_COMPILER_ID MATCHES "GNU|Clang" AND NOT CMAKE_C_COMPILER_FRONTEND_VARIANT STREQUAL "MSVC")
    list(APPEND args --cc "${CMAKE_C_COMPILER}")
  endif()
  set(${out_var} ${args} ${Q_OPTIONS} ${srcs} ${libs} PARENT_SCOPE)
  set(_qcpu_sources ${srcs} PARENT_SCOPE)
endfunction()

function(add_qcpu_executable name)
  _qcpu_command(cmd ${name} ${ARGN})
  set(out "${CMAKE_CURRENT_BINARY_DIR}/${name}${CMAKE_EXECUTABLE_SUFFIX}")
  add_custom_command(OUTPUT "${out}"
    COMMAND ${QCPU_CC} ${cmd} -o "${out}"
    DEPENDS ${_qcpu_sources} "${QCPU_ROOT}/qcpucc.py" "${QCPU_ROOT}/runtime/qpu_engine.c"
    COMMENT "qcpucc: building ${name} (__qcpu__ functions on the quantum CPU)"
    VERBATIM)
  add_custom_target(${name} ALL DEPENDS "${out}")
endfunction()

function(add_qcpu_library name)
  _qcpu_command(cmd ${name} ${ARGN})
  set(out "${CMAKE_CURRENT_BINARY_DIR}/${CMAKE_SHARED_LIBRARY_PREFIX}${name}${CMAKE_SHARED_LIBRARY_SUFFIX}")
  add_custom_command(OUTPUT "${out}"
    COMMAND ${QCPU_CC} --shared ${cmd} -o "${out}"
    DEPENDS ${_qcpu_sources} "${QCPU_ROOT}/qcpucc.py" "${QCPU_ROOT}/runtime/qpu_engine.c"
    COMMENT "qcpucc: building ${name} (__qcpu__ functions on the quantum CPU)"
    VERBATIM)
  add_custom_target(${name}_build ALL DEPENDS "${out}")
  add_library(${name} SHARED IMPORTED GLOBAL)
  set_target_properties(${name} PROPERTIES IMPORTED_LOCATION "${out}" IMPORTED_NO_SONAME TRUE
                        INTERFACE_INCLUDE_DIRECTORIES "${QCPU_ROOT}/runtime")
  add_dependencies(${name} ${name}_build)
endfunction()
