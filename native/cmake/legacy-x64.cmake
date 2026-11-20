# The legacy package uses the same MSVC C++17 sources and static CRT as the
# normal desktop package. Only these four shipped images link the OS fallbacks.
if(NOT MSVC OR NOT CMAKE_CXX_COMPILER_ID STREQUAL "MSVC" OR NOT MSVC_CXX_ARCHITECTURE_ID STREQUAL "x64"
   OR NOT DRUTA_BUILD_UI)
  message(FATAL_ERROR "DRUTA_LEGACY_X64 requires the x64 MSVC desktop build with DRUTA_BUILD_UI=ON")
endif()

set(DRUTA_YY_THUNKS_ARCHIVE "" CACHE FILEPATH "Offline copy of the official YY-Thunks 1.2.3 YY-Thunks-Objs.zip")
set(_yy_dir "${CMAKE_BINARY_DIR}/yy-thunks-1.2.3")
set(_yy_hash 4dc68ec8784e0afed7284e4393aec672873608161c3a5ae52faf618875d95b81)
file(MAKE_DIRECTORY "${_yy_dir}")
if(DRUTA_YY_THUNKS_ARCHIVE)
  set(_yy_zip "${DRUTA_YY_THUNKS_ARCHIVE}")
else()
  set(_yy_zip "${_yy_dir}/YY-Thunks-Objs.zip")
  file(DOWNLOAD "https://github.com/Chuyu-Team/YY-Thunks/releases/download/v1.2.3/YY-Thunks-Objs.zip"
       "${_yy_zip}" EXPECTED_HASH "SHA256=${_yy_hash}" TLS_VERIFY ON SHOW_PROGRESS)
endif()
file(SHA256 "${_yy_zip}" _yy_actual)
if(NOT _yy_actual STREQUAL _yy_hash)
  message(FATAL_ERROR "YY-Thunks archive does not match the pinned v1.2.3 release")
endif()
set(_yy_obj "objs/x64/YY_Thunks_for_WinXP.obj")
file(ARCHIVE_EXTRACT INPUT "${_yy_zip}" DESTINATION "${_yy_dir}" PATTERNS
     "LICENSE" "${_yy_obj}" "Config/x64/5.2.3790.txt" "Config/x64/6.0.6000.txt" "Config/x64/6.1.7600.txt")
set(DRUTA_LEGACY_OBJECT "${_yy_dir}/${_yy_obj}")
set(DRUTA_LEGACY_EXPORT_MAP_DIR "${_yy_dir}/Config/x64")
set(DRUTA_LEGACY_LICENSE "${_yy_dir}/LICENSE")

function(druta_legacy_x64_binary target subsystem)
  # link.exe resolves the runtime's import pointers from this object before
  # searching Win32 import libraries. Use the upstream entry point for the
  # dynamically loaded DLL so thread-local storage is initialized on XP x64.
  target_sources(${target} PRIVATE "${DRUTA_LEGACY_OBJECT}")
  target_link_options(${target} PRIVATE /SUBSYSTEM:${subsystem},5.02)
  get_target_property(kind ${target} TYPE)
  if(kind STREQUAL "SHARED_LIBRARY")
    target_link_options(${target} PRIVATE /ENTRY:DllMainCRTStartupForYY_Thunks
      /alternatename:YY_ThunksOriginalDllMainCRTStartup=_DllMainCRTStartup)
  endif()
endfunction()
