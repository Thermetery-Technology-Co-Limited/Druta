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
  # XP as the floor for the headers and the PE subsystem version.
  target_compile_definitions(druta_options INTERFACE _WIN32_WINNT=0x0501 WINVER=0x0501
                             WIN32_LEAN_AND_MEAN NOMINMAX UNICODE _UNICODE)
  if(MINGW)
    set(DRUTA_WIN_LINK_FLAGS -static -static-libgcc -static-libstdc++
        -Wl,--major-subsystem-version=5,--minor-subsystem-version=1
        -Wl,--major-os-version=5,--minor-os-version=1)
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

# ---- core (implementation, static) -------------------------------------------------------------
add_library(drutacore_impl STATIC
  src/backend/exports.cpp
  src/backend/fan.cpp
  src/backend/gpu.cpp
  src/backend/nvapi.cpp
  src/backend/nvml.cpp
  src/backend/telemetry.cpp
  src/backend/vflock.cpp
  src/backend/voltage.cpp
  src/core/rail_transport.cpp
  src/core/sleep.cpp
  src/core/slot.cpp
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
)
target_compile_definitions(drutacore PRIVATE DRUTACORE_BUILDING=1)
target_link_libraries(drutacore PRIVATE drutacore_impl)
set_target_properties(drutacore PROPERTIES PREFIX "" OUTPUT_NAME "drutacore")
if(NOT WIN32)
  set_target_properties(drutacore PROPERTIES PREFIX "lib")
endif()

# ---- druta-probe (Windows only: read-only capability report) -----------------------------------
if(WIN32)
  add_executable(druta-probe src/probe/druta_probe.cpp)
  target_link_libraries(druta-probe PRIVATE drutacore_impl)
endif()

# ---- tests (Linux and Windows hosts) -----------------------------------------------------------
enable_testing()
add_executable(drutacore-selftest src/core/selftest.cpp)
target_link_libraries(drutacore-selftest PRIVATE drutacore_impl)
add_test(NAME drutacore-selftest COMMAND drutacore-selftest)

# ---- fuzzers (clang + libFuzzer; run native/fuzz/run.sh) --------------------------------------
if(DRUTA_FUZZ)
  add_executable(rail_transport_fuzz fuzz/rail_transport_fuzz.cpp)
  target_link_libraries(rail_transport_fuzz PRIVATE drutacore_impl)
  target_compile_options(rail_transport_fuzz PRIVATE -fsanitize=fuzzer,address,undefined)
  target_link_options(rail_transport_fuzz PRIVATE -fsanitize=fuzzer,address,undefined)
endif()
