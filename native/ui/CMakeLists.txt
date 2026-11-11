cmake_minimum_required(VERSION 3.20)
cmake_policy(SET CMP0091 NEW)
if(CMAKE_SOURCE_DIR STREQUAL CMAKE_CURRENT_SOURCE_DIR)
  project(druta_ui LANGUAGES CXX)
  enable_testing()
endif()
if(NOT WIN32)
  message(FATAL_ERROR "The native UI preview requires Windows. The core remains portable.")
endif()

# UI requirements are deliberately separate from druta_options/core's XP audit.
add_library(druta_ui_options INTERFACE)
target_compile_features(druta_ui_options INTERFACE cxx_std_17)
target_compile_definitions(druta_ui_options INTERFACE
  _WIN32_WINNT=0x0600 WINVER=0x0600 WIN32_LEAN_AND_MEAN NOMINMAX UNICODE _UNICODE
  IMGUI_IMPL_WIN32_DISABLE_GAMEPAD)
if(MSVC)
  set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")
  target_compile_options(druta_ui_options INTERFACE /utf-8 /permissive-)
endif()

add_library(druta_ui_vendor STATIC
  vendor/imgui/imgui.cpp vendor/imgui/imgui_draw.cpp
  vendor/imgui/imgui_widgets.cpp vendor/imgui/imgui_tables.cpp
  vendor/imgui/backends/imgui_impl_win32.cpp vendor/imgui/backends/imgui_impl_dx9.cpp
  vendor/implot/implot.cpp vendor/implot/implot_items.cpp)
target_include_directories(druta_ui_vendor PUBLIC vendor/imgui vendor/imgui/backends vendor/implot)
target_link_libraries(druta_ui_vendor PUBLIC druta_ui_options)

add_executable(druta-ui WIN32 main.cpp ui_view.cpp)
target_link_libraries(druta-ui PRIVATE druta_ui_vendor d3d9 user32 gdi32 dwmapi imm32 shell32)
file(GENERATE OUTPUT ${CMAKE_CURRENT_BINARY_DIR}/$<CONFIG>/COPYING
  INPUT ${CMAKE_CURRENT_SOURCE_DIR}/../../COPYING)
file(GENERATE OUTPUT ${CMAKE_CURRENT_BINARY_DIR}/$<CONFIG>/ImGui-LICENSE.txt
  INPUT ${CMAKE_CURRENT_SOURCE_DIR}/vendor/imgui/LICENSE.txt)
file(GENERATE OUTPUT ${CMAKE_CURRENT_BINARY_DIR}/$<CONFIG>/ImPlot-LICENSE.txt
  INPUT ${CMAKE_CURRENT_SOURCE_DIR}/vendor/implot/LICENSE)
if(MSVC)
  target_compile_options(druta-ui PRIVATE /W4 /WX)
  target_link_options(druta-ui PRIVATE /SUBSYSTEM:WINDOWS,6.00
    /MANIFEST:EMBED /MANIFESTINPUT:${CMAKE_CURRENT_SOURCE_DIR}/druta-ui.manifest)
endif()

add_executable(druta-ui-policy-test tests/refresh_policy_test.cpp)
target_link_libraries(druta-ui-policy-test PRIVATE druta_ui_options)
target_include_directories(druta-ui-policy-test PRIVATE ${CMAKE_CURRENT_SOURCE_DIR})
add_test(NAME druta-ui-policy COMMAND druta-ui-policy-test)

# Audit the actual output, not a remembered developer SDK/driver combination.
find_package(Python3 COMPONENTS Interpreter REQUIRED)
set(audit_stamp ${CMAKE_CURRENT_BINARY_DIR}/$<CONFIG>/ui-imports-ok)
add_custom_command(OUTPUT ${audit_stamp}
  COMMAND ${CMAKE_COMMAND} -E rm -f ${audit_stamp}
  COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tools/audit_imports.py
    $<TARGET_FILE:druta-ui> --report ${CMAKE_CURRENT_BINARY_DIR}/$<CONFIG>/imports.json
  COMMAND ${CMAKE_COMMAND} -E touch ${audit_stamp}
  DEPENDS druta-ui tools/audit_imports.py ../tools/check_imports.py VERBATIM)
add_custom_target(druta-ui-import-audit ALL DEPENDS ${audit_stamp})
add_test(NAME druta-ui-import-policy
  COMMAND ${Python3_EXECUTABLE} -m pytest -q ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_audit_imports.py)
set_tests_properties(druta-ui-import-policy PROPERTIES WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}/../..)
