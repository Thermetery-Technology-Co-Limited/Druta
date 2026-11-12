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
target_include_directories(druta_ui_options INTERFACE ${CMAKE_CURRENT_SOURCE_DIR}/../include)
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

add_library(druta_ui_session STATIC core_client.cpp session.cpp profile_io.cpp rail_controls.cpp
  i2c_controls.cpp i2c_session.cpp i2c_nct3933.cpp i2c_ncp4206.cpp i2c_up9512r.cpp
  i2c_icx.cpp i2c_mp2888_offset.cpp i2c_mp29816.cpp icx_preset.cpp i2c_verify_hold.cpp i2c_warmup.cpp
  tuning_actions.cpp timings_service.cpp timing_transport.cpp)
add_library(druta_ui_curve_draft STATIC curve_draft.cpp)
add_library(druta_ui_risk STATIC risk_policy.cpp)
add_library(druta_ui_planner STATIC curve_planner.cpp)
target_link_libraries(druta_ui_risk PUBLIC druta_ui_options)
target_link_libraries(druta_ui_planner PUBLIC druta_ui_options)
target_include_directories(druta_ui_risk PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})
target_include_directories(druta_ui_planner PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})
target_link_libraries(druta_ui_curve_draft PUBLIC druta_ui_options)
target_include_directories(druta_ui_curve_draft PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})
target_link_libraries(druta_ui_session PUBLIC druta_ui_options druta_ui_planner advapi32)
target_include_directories(druta_ui_session PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})
add_executable(druta-ui WIN32 main.cpp ui_view.cpp advanced_view.cpp i2c_view.cpp timings_view.cpp)
target_link_libraries(druta-ui PRIVATE druta_ui_session druta_ui_curve_draft druta_ui_risk druta_ui_planner)
target_link_libraries(druta-ui PRIVATE druta_ui_vendor d3d9 user32 gdi32 dwmapi imm32 shell32 comdlg32)
file(GENERATE OUTPUT ${CMAKE_CURRENT_BINARY_DIR}/$<CONFIG>/COPYING
  INPUT ${CMAKE_CURRENT_SOURCE_DIR}/../../COPYING)
file(GENERATE OUTPUT ${CMAKE_CURRENT_BINARY_DIR}/$<CONFIG>/ImGui-LICENSE.txt
  INPUT ${CMAKE_CURRENT_SOURCE_DIR}/vendor/imgui/LICENSE.txt)
file(GENERATE OUTPUT ${CMAKE_CURRENT_BINARY_DIR}/$<CONFIG>/ImPlot-LICENSE.txt
  INPUT ${CMAKE_CURRENT_SOURCE_DIR}/vendor/implot/LICENSE)
if(MSVC)
  target_compile_options(druta_ui_session PRIVATE /W4 /WX)
  target_compile_options(druta-ui PRIVATE /W4 /WX)
  target_link_options(druta-ui PRIVATE /SUBSYSTEM:WINDOWS,6.00
    /MANIFEST:EMBED /MANIFESTINPUT:${CMAKE_CURRENT_SOURCE_DIR}/druta-ui.manifest)
endif()

# The shared C ABI stays the same boundary used by Python experiments.
set(DRUTA_UI_CORE_LIBRARY "" CACHE FILEPATH "Standalone UI: explicit drutacore.dll to stage beside the EXE")
if(TARGET drutacore)
  add_custom_target(druta-ui-core-stage
    COMMAND ${CMAKE_COMMAND} -E make_directory ${CMAKE_CURRENT_BINARY_DIR}/$<CONFIG>
    COMMAND ${CMAKE_COMMAND} -E copy_if_different
      $<TARGET_FILE:drutacore> ${CMAKE_CURRENT_BINARY_DIR}/$<CONFIG>/drutacore.dll
    DEPENDS drutacore VERBATIM)
  add_dependencies(druta-ui druta-ui-core-stage)
elseif(DRUTA_UI_CORE_LIBRARY)
  add_custom_target(druta-ui-core-stage
    COMMAND ${CMAKE_COMMAND} -E make_directory ${CMAKE_CURRENT_BINARY_DIR}/$<CONFIG>
    COMMAND ${CMAKE_COMMAND} -E copy_if_different
      ${DRUTA_UI_CORE_LIBRARY} ${CMAKE_CURRENT_BINARY_DIR}/$<CONFIG>/drutacore.dll VERBATIM)
  add_dependencies(druta-ui druta-ui-core-stage)
endif()

add_executable(druta-ui-client-test tests/core_client_test.cpp)
target_link_libraries(druta-ui-client-test PRIVATE druta_ui_session)
add_test(NAME druta-ui-client COMMAND druta-ui-client-test)
add_executable(druta-ui-session-test tests/session_test.cpp)
target_link_libraries(druta-ui-session-test PRIVATE druta_ui_session)
add_test(NAME druta-ui-session COMMAND druta-ui-session-test)
add_executable(druta-ui-view-test tests/ui_view_test.cpp ui_view.cpp advanced_view.cpp i2c_view.cpp timings_view.cpp)
target_link_libraries(druta-ui-view-test PRIVATE druta_ui_vendor druta_ui_curve_draft druta_ui_risk druta_ui_planner)
target_include_directories(druta-ui-view-test PRIVATE ${CMAKE_CURRENT_SOURCE_DIR})
add_test(NAME druta-ui-view COMMAND druta-ui-view-test)
add_executable(druta-ui-advanced-view-test tests/advanced_view_test.cpp advanced_view.cpp)
target_link_libraries(druta-ui-advanced-view-test PRIVATE druta_ui_vendor druta_ui_risk)
target_include_directories(druta-ui-advanced-view-test PRIVATE ${CMAKE_CURRENT_SOURCE_DIR})
add_test(NAME druta-ui-advanced-view COMMAND druta-ui-advanced-view-test)
add_executable(druta-ui-profile-test tests/profile_io_test.cpp)
target_link_libraries(druta-ui-profile-test PRIVATE druta_ui_session)
add_test(NAME druta-ui-profile COMMAND druta-ui-profile-test ${CMAKE_CURRENT_BINARY_DIR})
add_executable(druta-ui-curve-test tests/curve_draft_test.cpp)
target_link_libraries(druta-ui-curve-test PRIVATE druta_ui_curve_draft)
add_test(NAME druta-ui-curve COMMAND druta-ui-curve-test)
add_executable(druta-ui-risk-test tests/risk_policy_test.cpp)
target_link_libraries(druta-ui-risk-test PRIVATE druta_ui_risk)
add_test(NAME druta-ui-risk COMMAND druta-ui-risk-test)
add_executable(druta-ui-planner-test tests/curve_planner_test.cpp)
target_link_libraries(druta-ui-planner-test PRIVATE druta_ui_planner)
if(TARGET drutacore)
  add_test(NAME druta-ui-planner COMMAND druta-ui-planner-test $<TARGET_FILE:drutacore>)
else()
  add_test(NAME druta-ui-planner COMMAND druta-ui-planner-test)
endif()
add_executable(druta-ui-rails-test tests/rail_controls_test.cpp)
target_link_libraries(druta-ui-rails-test PRIVATE druta_ui_session)
add_test(NAME druta-ui-rails COMMAND druta-ui-rails-test)
add_executable(druta-ui-i2c-test tests/i2c_controls_test.cpp)
target_link_libraries(druta-ui-i2c-test PRIVATE druta_ui_session)
add_test(NAME druta-ui-i2c COMMAND druta-ui-i2c-test)
add_executable(druta-ui-i2c-session-test tests/i2c_session_test.cpp)
target_link_libraries(druta-ui-i2c-session-test PRIVATE druta_ui_session)
add_test(NAME druta-ui-i2c-session COMMAND druta-ui-i2c-session-test)
add_executable(druta-ui-i2c-catalog-view-test tests/i2c_catalog_view_test.cpp i2c_view.cpp)
target_link_libraries(druta-ui-i2c-catalog-view-test PRIVATE druta_ui_vendor)
target_include_directories(druta-ui-i2c-catalog-view-test PRIVATE ${CMAKE_CURRENT_SOURCE_DIR})
add_test(NAME druta-ui-i2c-catalog-view COMMAND druta-ui-i2c-catalog-view-test)
foreach(controller_test i2c_nct_ncp i2c_up_icx i2c_mp2888_offset i2c_mp29816 icx_preset icx_session i2c_verify_hold i2c_warmup)
  add_executable(druta-ui-${controller_test}-test tests/${controller_test}_test.cpp)
  target_link_libraries(druta-ui-${controller_test}-test PRIVATE druta_ui_session)
  if(MSVC)
    target_compile_options(druta-ui-${controller_test}-test PRIVATE /UNDEBUG)
  endif()
  add_test(NAME druta-ui-${controller_test} COMMAND druta-ui-${controller_test}-test)
endforeach()
add_executable(druta-ui-tuning-test tests/tuning_actions_test.cpp)
target_link_libraries(druta-ui-tuning-test PRIVATE druta_ui_session)
add_test(NAME druta-ui-tuning COMMAND druta-ui-tuning-test)
add_executable(druta-ui-timings-test tests/timings_service_test.cpp)
target_link_libraries(druta-ui-timings-test PRIVATE druta_ui_session)
add_test(NAME druta-ui-timings COMMAND druta-ui-timings-test)
add_executable(druta-ui-timings-view-test tests/timings_view_test.cpp timings_view.cpp)
target_link_libraries(druta-ui-timings-view-test PRIVATE druta_ui_vendor)
target_include_directories(druta-ui-timings-view-test PRIVATE ${CMAKE_CURRENT_SOURCE_DIR})
add_test(NAME druta-ui-timings-view COMMAND druta-ui-timings-view-test)
add_executable(druta-ui-live-check tools/live_check.cpp)
add_executable(druta-ui-i2c-live-check tools/i2c_live_check.cpp)
add_executable(druta-ui-actions-live-check tools/actions_live_check.cpp)
target_link_libraries(druta-ui-actions-live-check PRIVATE druta_ui_session)
target_link_libraries(druta-ui-i2c-live-check PRIVATE druta_ui_session)
target_link_libraries(druta-ui-live-check PRIVATE druta_ui_session)
file(GENERATE OUTPUT ${CMAKE_CURRENT_BINARY_DIR}/$<CONFIG>/JSON-LICENSE.txt
  INPUT ${CMAKE_CURRENT_SOURCE_DIR}/vendor/json/LICENSE.MIT)

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
