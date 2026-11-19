cmake_minimum_required(VERSION 3.16)
cmake_policy(SET CMP0091 NEW)  # MSVC runtime library selection below
project(drutacore LANGUAGES C CXX)

# Druta native core: one shared library (drutacore.dll / libdrutacore.so) with
# a C ABI, built for every Windows from XP up and for Linux (tests only).
#
#   Linux, sanitized, for the test suite:   cmake -S native -B native/build/linux-asan -DDRUTA_SANITIZE=ON
#   Windows x64 from Linux (MinGW-w64):      -DCMAKE_TOOLCHAIN_FILE=native/cmake/mingw-x86_64.cmake
#   Windows x86 from Linux (MinGW-w64):      -DCMAKE_TOOLCHAIN_FILE=native/cmake/mingw-i686.cmake
#   Windows with MSVC:                       cmake -S native -B native/build/msvc -T v141_xp (XP) or default
#
# Rules that keep one binary running on XP through 11:
#   * The C runtime is linked statically: no UCRT or VC redistributable.
#   * Only KERNEL32 and the system C runtime are imported. Every other Windows
#     DLL (gdi32 D3DKMT*, setupapi, shell32, nvapi64, nvml, ...) is loaded with
#     LoadLibrary/GetProcAddress at run time and each export is a capability.
#   * MinGW: after every link, tools/check_imports.py limits each Windows
#     binary's imports to the allowlist in tools/xp-imports.txt (kernel32
#     functions documented for XP by Microsoft and an export history, msvcrt
#     functions from two secondary sources) and its PE header versions to XP's.
#     That is a check of the import tables and headers, not a proof that the
#     binaries start on XP. GCC's runtime references five Vista functions;
#     src/win/runtime_imports.c binds those at run time instead.
#   * No exceptions cross the C ABI; no C++ types in it.

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_C_VISIBILITY_PRESET hidden)
set(CMAKE_CXX_VISIBILITY_PRESET hidden)
set(CMAKE_VISIBILITY_INLINES_HIDDEN ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)
if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
  set(CMAKE_BUILD_TYPE RelWithDebInfo CACHE STRING "" FORCE)
endif()

option(DRUTA_SANITIZE "Build with AddressSanitizer and UndefinedBehaviorSanitizer (Linux/clang/gcc)" OFF)
option(DRUTA_WERROR "Treat warnings as errors" ON)
option(DRUTA_FUZZ "Build the libFuzzer targets (clang only)" OFF)
option(DRUTA_IMPORT_AUDIT "MinGW: fail the build when a Windows binary imports a DLL or function outside tools/xp-imports.txt (documented for XP) or its PE header asks for a newer Windows" ON)
option(DRUTA_BUILD_UI "Build the Windows native UI preview (independent of the core XP target)" OFF)

add_library(druta_options INTERFACE)
if(MSVC)
  target_compile_options(druta_options INTERFACE /W4 /permissive- /utf-8 /Zc:__cplusplus)
  if(DRUTA_WERROR)
    target_compile_options(druta_options INTERFACE /WX)
  endif()
  # Static CRT: no vcruntime/UCRT redistributable on the target machine.
  set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")
else()
  target_compile_options(druta_options INTERFACE -Wall -Wextra -Wpedantic -Wshadow -Wconversion
                         -Wsign-conversion -Wcast-align -Wformat=2 -Wundef)
  if(DRUTA_WERROR)
    target_compile_options(druta_options INTERFACE -Werror)
  endif()
endif()

if(WIN32)
  # XP as the floor for the headers and the PE operating-system and subsystem
  # versions: 5.1 for 32-bit (XP), 5.2 for 64-bit (XP x64 is NT 5.2 and there is
  # no older 64-bit Windows for this machine, so nothing is lost; 5.2 is also
  # the GNU linker's own default for x86-64 images).
  target_compile_definitions(druta_options INTERFACE _WIN32_WINNT=0x0501 WINVER=0x0501
                             WIN32_LEAN_AND_MEAN NOMINMAX UNICODE _UNICODE)
  if(MINGW)
    if(CMAKE_SIZEOF_VOID_P EQUAL 8)
      set(DRUTA_PE_MINOR_VERSION 2)
    else()
      set(DRUTA_PE_MINOR_VERSION 1)
    endif()
    set(DRUTA_WIN_LINK_FLAGS -static -static-libgcc -static-libstdc++
        -Wl,--major-subsystem-version=5,--minor-subsystem-version=${DRUTA_PE_MINOR_VERSION}
        -Wl,--major-os-version=5,--minor-os-version=${DRUTA_PE_MINOR_VERSION})
    target_link_options(druta_options INTERFACE ${DRUTA_WIN_LINK_FLAGS})
  endif()
endif()

if(DRUTA_SANITIZE)
  if(MSVC)
    target_compile_options(druta_options INTERFACE /fsanitize=address)
  else()
    target_compile_options(druta_options INTERFACE -fsanitize=address,undefined -fno-omit-frame-pointer -g)
    target_link_options(druta_options INTERFACE -fsanitize=address,undefined)
  endif()
endif()
if(DRUTA_FUZZ)
  # Coverage and comparison tracing inside the core itself, not just the harness:
  # without this libFuzzer never learns the packet sizes and command words.
  target_compile_options(druta_options INTERFACE -fsanitize=fuzzer-no-link)
endif()

# ---- XP import shims for the MinGW GCC runtime (see src/win/runtime_imports.c) ---------------
# An OBJECT library linked straight into each Windows binary: its import-pointer
# definitions must be on the link line before libgcc.a and libkernel32.a are
# searched, which a member of the static drutacore_impl archive would not be.
if(MINGW)
  add_library(druta_runtime_imports OBJECT src/win/runtime_imports.c)
  target_link_libraries(druta_runtime_imports PRIVATE druta_options)
endif()

# Audit a Windows binary's imports and header versions after every link. The
# audit is a stamp-producing step of the default build, not a POST_BUILD
# command: a failed POST_BUILD would leave the binary looking up to date, while
# a missing stamp makes every later build run the audit again until it passes
# (and a changed allowlist or script reruns it too).
function(druta_import_audit target)
  if(MINGW AND DRUTA_IMPORT_AUDIT)
    set(stamp ${CMAKE_CURRENT_BINARY_DIR}/${target}.imports-ok)
    add_custom_command(OUTPUT ${stamp}
      COMMAND ${CMAKE_COMMAND} -E remove -f ${stamp}
      COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tools/check_imports.py --quiet
              $<TARGET_FILE:${target}>
      COMMAND ${CMAKE_COMMAND} -E touch ${stamp}
      DEPENDS ${target} ${CMAKE_CURRENT_SOURCE_DIR}/tools/check_imports.py
              ${CMAKE_CURRENT_SOURCE_DIR}/tools/xp-imports.txt
      COMMENT "Auditing the imports of ${target} against tools/xp-imports.txt"
      VERBATIM)
    add_custom_target(${target}-import-audit ALL DEPENDS ${stamp})
  endif()
endfunction()
# A shipped Windows binary: the shims (seam undefined) and the audit.
function(druta_windows_binary target)
  if(MINGW)
    target_link_libraries(${target} PRIVATE druta_runtime_imports)
    druta_import_audit(${target})
  endif()
endfunction()
if(MINGW AND DRUTA_IMPORT_AUDIT)
  find_package(Python3 COMPONENTS Interpreter)
  if(NOT Python3_Interpreter_FOUND)
    message(FATAL_ERROR "The XP import audit needs Python 3; "
                        "install it or configure with -DDRUTA_IMPORT_AUDIT=OFF")
  endif()
endif()

# ---- core (implementation, static) -------------------------------------------------------------
add_library(drutacore_impl STATIC
  src/backend/i2c.cpp
  src/backend/clkdom.cpp
  src/backend/clkdom_names.cpp
  src/backend/clocks.cpp
  src/backend/current_limits.cpp
  src/backend/client_power.cpp
  src/backend/client_power_controller.cpp
  src/backend/perf_diagnostics.cpp
  src/backend/clock_bios.cpp
  src/backend/vbios_image.cpp
  src/backend/voltage_peers.cpp
  src/backend/power_arbitration.cpp
  src/backend/rail_limits.cpp
  src/backend/rails.cpp
  src/backend/legacy_p0.cpp
  src/backend/target.cpp
  src/backend/exports.cpp
  src/backend/fan.cpp
  src/backend/gdi.cpp
  src/backend/gpu.cpp
  src/backend/hook.cpp
  src/backend/nvapi.cpp
  src/backend/nvml.cpp
  src/backend/power_layouts.cpp
  src/backend/power_limit.cpp
  src/backend/reset_all.cpp
  src/backend/rm_transport.cpp
  src/backend/telemetry.cpp
  src/backend/vfcurve.cpp
  src/backend/vflock.cpp
  src/backend/voltage.cpp
  src/core/rail_transport.cpp
  src/core/sleep.cpp
  src/core/slot.cpp
  src/core/sha256.cpp
  src/core/vf_plan.cpp
)
if(NOT WIN32)
  find_package(Threads REQUIRED)
  target_link_libraries(drutacore_impl PUBLIC Threads::Threads)
endif()
target_include_directories(drutacore_impl PUBLIC include src)
target_link_libraries(drutacore_impl PUBLIC druta_options)

# ---- drutacore (the C ABI shared library) ------------------------------------------------------
add_library(drutacore SHARED
  src/core/abi.cpp
  src/core/abi_i2c.cpp
  src/core/abi_clkdom.cpp
  src/core/abi_current_limits.cpp
  src/core/abi_client_power.cpp
  src/core/abi_client_power_controller.cpp
  src/core/abi_perf_diagnostics.cpp
  src/core/abi_clock_bios.cpp
  src/core/abi_vbios_image.cpp
  src/core/abi_voltage_peers.cpp
  src/core/abi_power_arbitration.cpp
  src/core/abi_rail_limits.cpp
  src/core/abi_rails.cpp
  src/core/abi_legacy_p0.cpp
  src/core/abi_target.cpp
  src/core/abi_power_layouts.cpp
  src/core/abi_power_limit.cpp
  src/core/abi_reset.cpp
  src/core/abi_transport.cpp
  src/core/abi_vfplan.cpp
)
target_compile_definitions(drutacore PRIVATE DRUTACORE_BUILDING=1)
target_link_libraries(drutacore PRIVATE drutacore_impl)
set_target_properties(drutacore PROPERTIES PREFIX "" OUTPUT_NAME "drutacore")
druta_windows_binary(drutacore)
if(NOT WIN32)
  set_target_properties(drutacore PROPERTIES PREFIX "lib")
endif()

# ---- druta-probe (Windows only: read-only capability report) -----------------------------------
if(WIN32)
  add_executable(druta-probe src/probe/druta_probe.cpp)
  target_link_libraries(druta-probe PRIVATE drutacore_impl)
  druta_windows_binary(druta-probe)
endif()

# ---- tests (Linux and Windows hosts) -----------------------------------------------------------
enable_testing()
add_executable(drutacore-selftest src/core/selftest.cpp)
target_link_libraries(drutacore-selftest PRIVATE drutacore_impl)
druta_windows_binary(drutacore-selftest)
add_test(NAME drutacore-selftest COMMAND drutacore-selftest)

# ---- druta-runtime-imports-test (MinGW only: the shims' fallbacks, run under Wine by build.sh) ----
# runtime_imports.c compiled into this executable alone with its test seam; the
# shipped binaries above link druta_runtime_imports, where the seam is undefined.
if(MINGW)
  add_executable(druta-runtime-imports-test src/win/runtime_imports_test.cpp src/win/runtime_imports.c)
  target_compile_definitions(druta-runtime-imports-test PRIVATE DRUTA_RUNTIME_IMPORTS_TEST=1)
  target_link_libraries(druta-runtime-imports-test PRIVATE druta_options)
  druta_import_audit(druta-runtime-imports-test)
endif()

# ---- optional native desktop preview (separate Windows UI requirements) ---------------------
if(DRUTA_BUILD_UI)
  add_subdirectory(ui)
endif()

# ---- fuzzers (clang + libFuzzer; run native/fuzz/run.sh) --------------------------------------
if(DRUTA_FUZZ)
  add_executable(rail_transport_fuzz fuzz/rail_transport_fuzz.cpp)
  target_link_libraries(rail_transport_fuzz PRIVATE drutacore_impl)
  target_compile_options(rail_transport_fuzz PRIVATE -fsanitize=fuzzer,address,undefined)
  target_link_options(rail_transport_fuzz PRIVATE -fsanitize=fuzzer,address,undefined)
  add_executable(perf_decode_fuzz fuzz/perf_decode_fuzz.cpp)
  target_link_libraries(perf_decode_fuzz PRIVATE drutacore_impl)
  target_compile_options(perf_decode_fuzz PRIVATE -fsanitize=fuzzer,address,undefined)
  target_link_options(perf_decode_fuzz PRIVATE -fsanitize=fuzzer,address,undefined)
endif()
# Injected NVAPI packet and target checks; no driver or GPU access.
add_executable(drutacore-i2c-test tests/i2c_test.cpp)
target_include_directories(drutacore-i2c-test PRIVATE include src)
target_link_libraries(drutacore-i2c-test PRIVATE drutacore drutacore_impl druta_options)
druta_windows_binary(drutacore-i2c-test)
add_test(NAME drutacore-i2c COMMAND drutacore-i2c-test)
add_executable(drutacore-rails-request-packet-test tests/rails_request_packet_test.cpp)
target_include_directories(drutacore-rails-request-packet-test PRIVATE include src)
target_link_libraries(drutacore-rails-request-packet-test PRIVATE drutacore drutacore_impl druta_options)
druta_windows_binary(drutacore-rails-request-packet-test)
add_test(NAME drutacore-rails-request-packet COMMAND drutacore-rails-request-packet-test)
add_executable(drutacore-vbios-image-test tests/vbios_image_test.cpp)
target_include_directories(drutacore-vbios-image-test PRIVATE include src)
target_link_libraries(drutacore-vbios-image-test PRIVATE drutacore drutacore_impl druta_options)
druta_windows_binary(drutacore-vbios-image-test)
add_test(NAME drutacore-vbios-image COMMAND drutacore-vbios-image-test)
add_executable(drutacore-voltage-peers-test tests/voltage_peers_test.cpp)
target_include_directories(drutacore-voltage-peers-test PRIVATE include src)
target_link_libraries(drutacore-voltage-peers-test PRIVATE drutacore drutacore_impl druta_options)
druta_windows_binary(drutacore-voltage-peers-test)
add_test(NAME drutacore-voltage-peers COMMAND drutacore-voltage-peers-test)
