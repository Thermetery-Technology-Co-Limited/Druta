# Native UI dependencies

The frontend builds from these checked-in upstream sources. No dependency is
downloaded during configuration or compilation. Files are unmodified, including
the upstream license texts. Build options belong in the parent CMake target,
not in the vendored configuration header.

| Directory | Upstream version | Exact commit | License |
| --- | --- | --- | --- |
| `imgui/` | Dear ImGui v1.93.0 | [`c1aaac65e896a45f81f47a4d68f9959e0f86c859`](https://github.com/ocornut/imgui/tree/c1aaac65e896a45f81f47a4d68f9959e0f86c859) | [MIT](imgui/LICENSE.txt) |
| `implot/` | ImPlot v1.1 | [`819f200639455a58b6b3d4156b70e4271aeb2d00`](https://github.com/epezent/implot/tree/819f200639455a58b6b3d4156b70e4271aeb2d00) | [MIT](implot/LICENSE) |

Imported on 2026-11-11 from `raw.githubusercontent.com` at the commits above.
All 20 imported files were checked against the corresponding Git blob IDs from
the upstream commit trees. [SHA256SUMS.txt](SHA256SUMS.txt) records their byte
hashes with paths relative to this directory; it excludes this provenance note
and the checksum file itself.

## Included upstream paths

- Dear ImGui core: `imgui.cpp`, `imgui_draw.cpp`, `imgui_tables.cpp`,
  `imgui_widgets.cpp`, `imgui.h`, `imgui_internal.h`, `imconfig.h`.
- Dear ImGui bundled helpers: `imstb_rectpack.h`, `imstb_textedit.h`,
  `imstb_truetype.h`. Their license notices remain in the original headers.
- Dear ImGui platform/renderer backends: `backends/imgui_impl_win32.cpp`,
  `backends/imgui_impl_win32.h`, `backends/imgui_impl_dx9.cpp`,
  `backends/imgui_impl_dx9.h`.
- Dear ImGui license: `LICENSE.txt`.
- ImPlot: `implot.cpp`, `implot_items.cpp`, `implot.h`, `implot_internal.h`,
  `LICENSE`.

Demos, example applications, additional renderers, SDKs and prebuilt binaries
are not included. Selecting these source revisions does not itself qualify
the resulting executable for Windows Vista/7; toolchain/import audits and
actual target-OS testing remain separate acceptance work.

When updating a dependency, select an explicit upstream commit, replace only
its listed files, retain the license texts, verify the exact upstream bytes and
regenerate the checksums. Re-run the frontend build, behavior tests and platform
qualification appropriate to the update.
