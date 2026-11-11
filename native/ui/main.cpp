// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
// Fixture-only native host. No core handle, telemetry API or GPU tuning calls.
#include <windows.h>
#include <shellapi.h>
#include <d3d9.h>
#include "imgui.h"
#include "implot.h"
#include "imgui_impl_win32.h"
#include "imgui_impl_dx9.h"
#include "refresh_policy.h"
#include "ui_view.h"
#include <algorithm>
#include <cmath>
#include <cstdio>
#include <string>
#include <vector>

extern IMGUI_IMPL_API LRESULT ImGui_ImplWin32_WndProcHandler(HWND, UINT, WPARAM, LPARAM);

namespace {
constexpr int restore_hotkey = 1;
// Only delivered by newer Windows; do not raise the Vista API import floor.
constexpr UINT dpi_changed_message = 0x02e0;
constexpr wchar_t window_class[] = L"DrutaNativePreview";
double now_seconds() {
    static const double frequency = [] { LARGE_INTEGER n{}; QueryPerformanceFrequency(&n); return double(n.QuadPart); }();
    LARGE_INTEGER n{}; QueryPerformanceCounter(&n); return double(n.QuadPart) / frequency;
}
double process_seconds() {
    FILETIME created{}, exited{}, kernel{}, user{};
    GetProcessTimes(GetCurrentProcess(), &created, &exited, &kernel, &user);
    const auto seconds = [](FILETIME t) {
        return (double(t.dwHighDateTime) * 4294967296.0 + t.dwLowDateTime) / 10000000.0;
    };
    return seconds(kernel) + seconds(user);
}
bool write_bytes(const std::wstring& path, const void* data, DWORD size) {
    HANDLE f = CreateFileW(path.c_str(), GENERIC_WRITE, 0, nullptr, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (f == INVALID_HANDLE_VALUE) return false;
    DWORD written = 0;
    const bool ok = WriteFile(f, data, size, &written, nullptr) && written == size;
    CloseHandle(f); return ok;
}
class MessageWait {
    HANDLE timer_ = nullptr;
public:
    MessageWait() {
        // Probe the high-resolution timer capability, without version gates or
        // timeBeginPeriod's process/system-wide timer-resolution side effects.
        using CreateTimer = HANDLE (WINAPI*)(LPSECURITY_ATTRIBUTES, LPCWSTR, DWORD, DWORD);
        const auto create = reinterpret_cast<CreateTimer>(GetProcAddress(GetModuleHandleW(L"kernel32.dll"), "CreateWaitableTimerExW"));
        if (create) timer_ = create(nullptr, nullptr, 0x00000002, TIMER_MODIFY_STATE | SYNCHRONIZE);
        if (!timer_) timer_ = CreateWaitableTimerW(nullptr, FALSE, nullptr);
    }
    ~MessageWait() { if (timer_) { CancelWaitableTimer(timer_); CloseHandle(timer_); } }
    void wait(double seconds) {
        // Use the precise timer only for interactive frame deadlines. Quiet
        // windows retain ordinary coarse waits; Pause cancels recurring work.
        if (timer_ && std::isfinite(seconds) && seconds <= druta::ui::RefreshPolicy::active_interval) {
            LARGE_INTEGER due{};
            due.QuadPart = -LONGLONG(std::max(1.0, std::ceil(seconds * 10000000.0)));
            if (SetWaitableTimer(timer_, &due, 0, nullptr, nullptr, FALSE)) {
                MsgWaitForMultipleObjectsEx(1, &timer_, INFINITE, QS_ALLINPUT, MWMO_INPUTAVAILABLE);
                CancelWaitableTimer(timer_); return;
            }
        }
        if (timer_) CancelWaitableTimer(timer_);
        const DWORD timeout = std::isfinite(seconds)
            ? DWORD(std::min(60000.0, std::max(1.0, std::ceil(seconds*1000)))) : INFINITE;
        MsgWaitForMultipleObjectsEx(0, nullptr, timeout, QS_ALLINPUT, MWMO_INPUTAVAILABLE);
    }
};
struct Options {
    int width = 1280, height = 800;
    float scale = 0;
    std::wstring smoke_dir, capture;
    druta::ui::Page page = druta::ui::Page::daily;
    bool classic = false;
};
bool parse_options(Options& o) {
    int count = 0;
    LPWSTR* args = CommandLineToArgvW(GetCommandLineW(), &count);
    if (!args) return false;
    bool valid = true;
    for (int i = 1; i < count && valid; ++i) {
        const std::wstring key = args[i];
        if (key == L"--classic") o.classic = true;
        else if (i + 1 >= count) valid = false;
        else {
            const wchar_t* value = args[++i];
            if (key == L"--smoke-test") o.smoke_dir = value;
            else if (key == L"--capture") o.capture = value;
            else if (key == L"--width" || key == L"--height") {
                wchar_t* end = nullptr; const long n = wcstol(value, &end, 10);
                valid = *value && end && !*end && n >= 640 && n <= 7680;
                if (key == L"--width") o.width = int(n); else o.height = int(n);
            } else if (key == L"--scale") {
                wchar_t* end = nullptr; const double n = wcstod(value, &end);
                valid = *value && end && !*end && std::isfinite(n) && n >= 1 && n <= 3;
                o.scale = float(n);
            } else if (key == L"--page") {
                if (wcscmp(value, L"advanced") == 0) o.page = druta::ui::Page::advanced;
                else if (wcscmp(value, L"monitor") == 0) o.page = druta::ui::Page::monitor;
                else if (wcscmp(value, L"daily") != 0) valid = false;
            } else valid = false;
        }
    }
    LocalFree(args); return valid;
}
struct App {
    HWND window = nullptr;
    IDirect3D9* d3d = nullptr;
    IDirect3DDevice9* device = nullptr;
    D3DPRESENT_PARAMETERS present{};
    druta::ui::RefreshPolicy policy{now_seconds()};
    druta::ui::ViewState view;
    UINT width = 1280, height = 800;
    bool resize_pending = false, device_lost = false, initialized = false, quit = false;
    float pending_scale = 0, scale = 1;
    double device_retry_at = 0;
    unsigned long long frames = 0, presents = 0, fixture_sample_ticks = 0, resets = 0;

    void pause() {
        policy.set_explicit_pause(true, now_seconds());
        ShowWindow(window, SW_MINIMIZE);
    }
    void restore() {
        if (IsIconic(window)) ShowWindow(window, SW_RESTORE);
        policy.set_minimized(false, now_seconds());
        policy.set_explicit_pause(false, now_seconds());
        policy.note_activity(now_seconds());
        SetForegroundWindow(window);
    }
    void client_size(int w, int h) {
        RECT r{0, 0, w, h};
        AdjustWindowRectEx(&r, WS_OVERLAPPEDWINDOW, FALSE, 0);
        SetWindowPos(window, nullptr, 0, 0, r.right-r.left, r.bottom-r.top, SWP_NOMOVE | SWP_NOZORDER);
    }
    bool create_device() {
        d3d = Direct3DCreate9(D3D_SDK_VERSION);
        if (!d3d) return false;
        present.Windowed = TRUE; present.SwapEffect = D3DSWAPEFFECT_DISCARD;
        present.BackBufferFormat = D3DFMT_UNKNOWN;
        present.BackBufferWidth = width; present.BackBufferHeight = height;
        // One pacing owner: the policy sleeps after completed frames.
        present.PresentationInterval = D3DPRESENT_INTERVAL_IMMEDIATE;
        // Only vertex processing falls back; do not silently substitute a CPU renderer.
        HRESULT hr = d3d->CreateDevice(D3DADAPTER_DEFAULT, D3DDEVTYPE_HAL, window,
            D3DCREATE_HARDWARE_VERTEXPROCESSING, &present, &device);
        if (FAILED(hr)) hr = d3d->CreateDevice(D3DADAPTER_DEFAULT, D3DDEVTYPE_HAL, window,
            D3DCREATE_SOFTWARE_VERTEXPROCESSING, &present, &device);
        return SUCCEEDED(hr);
    }
    bool recover_device(double now) {
        if (now < device_retry_at) return false;
        const HRESULT status = device->TestCooperativeLevel();
        if (status == D3DERR_DEVICELOST) { device_retry_at = now + 0.25; return false; }
        if (FAILED(status) && status != D3DERR_DEVICENOTRESET) {
            device_retry_at = now + 0.25; return false;
        }
        ImGui_ImplDX9_InvalidateDeviceObjects();
        present.BackBufferWidth = width; present.BackBufferHeight = height;
        if (FAILED(device->Reset(&present))) { device_retry_at = now + 0.25; return false; }
        if (!ImGui_ImplDX9_CreateDeviceObjects()) { device_retry_at = now + 0.25; return false; }
        device_lost = resize_pending = false; ++resets; return true;
    }
    bool capture(const std::wstring& path) {
        IDirect3DSurface9* back = nullptr; IDirect3DSurface9* copy = nullptr;
        if (FAILED(device->GetBackBuffer(0, 0, D3DBACKBUFFER_TYPE_MONO, &back))) return false;
        D3DSURFACE_DESC desc{}; back->GetDesc(&desc);
        bool ok = (desc.Format == D3DFMT_X8R8G8B8 || desc.Format == D3DFMT_A8R8G8B8)
            && SUCCEEDED(device->CreateOffscreenPlainSurface(desc.Width, desc.Height, desc.Format,
                    D3DPOOL_SYSTEMMEM, &copy, nullptr));
        if (ok) ok = SUCCEEDED(device->GetRenderTargetData(back, copy));
        D3DLOCKED_RECT lock{};
        if (ok) ok = SUCCEEDED(copy->LockRect(&lock, nullptr, D3DLOCK_READONLY));
        if (ok) {
            const DWORD pixels = desc.Width * desc.Height * 4;
            BITMAPFILEHEADER file{}; BITMAPINFOHEADER info{};
            file.bfType = 0x4d42; file.bfOffBits = sizeof(file) + sizeof(info);
            file.bfSize = file.bfOffBits + pixels;
            info.biSize = sizeof(info); info.biWidth = LONG(desc.Width); info.biHeight = -LONG(desc.Height);
            info.biPlanes = 1; info.biBitCount = 32; info.biCompression = BI_RGB;
            std::vector<unsigned char> bytes(file.bfSize);
            memcpy(bytes.data(), &file, sizeof(file)); memcpy(bytes.data()+sizeof(file), &info, sizeof(info));
            for (UINT y = 0; y < desc.Height; ++y)
                memcpy(bytes.data()+file.bfOffBits+y*desc.Width*4,
                       static_cast<unsigned char*>(lock.pBits)+y*lock.Pitch, desc.Width*4);
            copy->UnlockRect();
            ok = write_bytes(path, bytes.data(), DWORD(bytes.size()));
        }
        if (copy) copy->Release(); back->Release(); return ok;
    }
    bool render(const std::wstring& capture_path = {}) {
        if ((device_lost || resize_pending) && !recover_device(now_seconds())) return false;
        if (pending_scale > 0) {
            scale = pending_scale; pending_scale = 0;
            druta::ui::apply_theme(scale); ImGui::GetStyle().FontScaleDpi = scale;
        }
        ImGui_ImplDX9_NewFrame(); ImGui_ImplWin32_NewFrame();
        // The Win32 backend reports zero client size when minimized. Always refresh
        // deliberately renders the last full viewport, preserving its draft/layout.
        ImGui::GetIO().DisplaySize = ImVec2(float(width), float(height));
        ImGui::NewFrame(); druta::ui::draw(view); ImGui::Render();
        policy.set_always_refresh(view.always_refresh, now_seconds());
        if (view.pause_requested) {
            view.pause_requested = false; pause(); return false;
        }
        device->SetRenderState(D3DRS_ZENABLE, FALSE);
        device->SetRenderState(D3DRS_ALPHABLENDENABLE, FALSE);
        device->SetRenderState(D3DRS_SCISSORTESTENABLE, FALSE);
        device->Clear(0, nullptr, D3DCLEAR_TARGET, D3DCOLOR_XRGB(12,16,22), 1, 0);
        if (FAILED(device->BeginScene())) { device_lost = true; device_retry_at = now_seconds()+0.25; return false; }
        ImGui_ImplDX9_RenderDrawData(ImGui::GetDrawData()); device->EndScene();
        ++frames;
        const bool captured = capture_path.empty() || capture(capture_path);
        const HRESULT result = device->Present(nullptr, nullptr, nullptr, nullptr);
        if (SUCCEEDED(result)) ++presents;
        else { device_lost = true; device_retry_at = now_seconds() + 0.25; }
        return captured;
    }
};
App* active = nullptr;

LRESULT CALLBACK window_proc(HWND window, UINT message, WPARAM wparam, LPARAM lparam) {
    App* app = active;
    if (app) {
        if ((message >= WM_MOUSEFIRST && message <= WM_MOUSELAST)
            || (message >= WM_KEYFIRST && message <= WM_KEYLAST)) app->policy.note_activity(now_seconds());
        switch (message) {
        case WM_SIZE:
            app->policy.set_minimized(wparam == SIZE_MINIMIZED, now_seconds());
            if (wparam != SIZE_MINIMIZED && LOWORD(lparam) && HIWORD(lparam)) {
                app->width = LOWORD(lparam); app->height = HIWORD(lparam);
                app->resize_pending = app->device != nullptr;
            }
            return 0;
        case dpi_changed_message: {
            app->pending_scale = float(HIWORD(wparam)) / 96.0f;
            const auto* rect = reinterpret_cast<const RECT*>(lparam);
            SetWindowPos(window, nullptr, rect->left, rect->top, rect->right-rect->left,
                rect->bottom-rect->top, SWP_NOZORDER | SWP_NOACTIVATE);
            return 0;
        }
        case WM_GETMINMAXINFO: {
            auto* bounds = reinterpret_cast<MINMAXINFO*>(lparam);
            bounds->ptMinTrackSize = POINT{640, 480}; return 0;
        }
        case WM_HOTKEY:
            if (wparam == restore_hotkey) { app->restore(); return 0; }
            break;
        case WM_SYSCOMMAND:
            if ((wparam & 0xfff0) == SC_KEYMENU) return 0;
            break;
        case WM_DESTROY: app->quit = true; PostQuitMessage(0); return 0;
        }
    }
    if (app && app->initialized && ImGui_ImplWin32_WndProcHandler(window, message, wparam, lparam)) return 1;
    return DefWindowProcW(window, message, wparam, lparam);
}
void load_font() {
    wchar_t directory[MAX_PATH]{};
    if (GetWindowsDirectoryW(directory, MAX_PATH)) {
        const std::wstring path = std::wstring(directory) + L"\\Fonts\\segoeui.ttf";
        HANDLE f = CreateFileW(path.c_str(), GENERIC_READ, FILE_SHARE_READ, nullptr, OPEN_EXISTING, 0, nullptr);
        if (f != INVALID_HANDLE_VALUE) {
            const DWORD length = GetFileSize(f, nullptr);
            if (length > 0 && length < 32*1024*1024) {
                void* bytes = IM_ALLOC(length); DWORD read = 0;
                if (bytes && ReadFile(f, bytes, length, &read, nullptr) && read == length) {
                    ImGui::GetIO().Fonts->AddFontFromMemoryTTF(bytes, int(length), 18.0f);
                    CloseHandle(f); return;
                }
                if (bytes) IM_FREE(bytes);
            }
            CloseHandle(f);
        }
    }
    ImFontConfig config; config.SizePixels = 18;
    ImGui::GetIO().Fonts->AddFontDefault(&config);
}

// An opt-in, bounded fixture harness. Counts policy sample opportunities, NOT
// hardware reads. It exercises the real window/device loop without a GPU handle.
struct Smoke {
    const wchar_t* captures[11] = {L"01-focused.bmp", L"02-always.bmp", L"03-minimized.bmp", nullptr,
        L"05-restored.bmp", nullptr, L"07-default.bmp", L"08-classic.bmp", L"09-advanced.bmp",
        L"10-monitor.bmp", L"11-small.bmp"};
    const char* names[11] = {"visible_default", "always_visible", "always_minimized", "explicit_pause",
        "hotkey_restore", "default_minimized", "default_restored", "classic", "advanced", "monitor", "resize_1024x768"};
    int phase = 0; bool captured = false, passed = true, finished = false;
    double start = 0, cpu = 0;
    unsigned long long frames = 0, presents = 0, samples = 0;
    std::string report = "{\n  \"backend_connected\": false,\n  \"sample_ticks_are_fixture_only\": true,\n  \"phases\": [\n";
    explicit Smoke(App& app) { begin(app); }
    void begin(App& app) {
        start = now_seconds(); cpu = process_seconds(); frames = app.frames;
        presents = app.presents; samples = app.fixture_sample_ticks; captured = false;
    }
    void advance(App& app, const std::wstring& directory) {
        if (finished || now_seconds()-start < 2.0) return;
        const auto f = app.frames-frames, p = app.presents-presents, t = app.fixture_sample_ticks-samples;
        bool ok = (phase == 3 || phase == 5) ? (f == 0 && t == 0) : (f > 0 && p > 0 && captured);
        if (phase >= 7) ok = ok && app.view.memory_offset_mhz == 26.875f;
        if (phase == 10) ok = ok && app.width == 1024 && app.height == 768 && app.resets > 0;
        passed = passed && ok;
        char row[400];
        snprintf(row, sizeof(row), "    {\"phase\": \"%s\", \"frames\": %llu, \"presents\": %llu, \"fixture_ticks\": %llu, \"cpu_seconds\": %.6f, \"passed\": %s}%s\n",
            names[phase], f, p, t, process_seconds()-cpu, ok ? "true" : "false", phase == 10 ? "" : ",");
        report += row;
        if (++phase == 11) {
            report += "  ],\n  \"device_resets\": " + std::to_string(app.resets)
                + ",\n  \"hotkey_registered\": " + (app.view.hotkey_available ? "true" : "false")
                + ",\n  \"passed\": " + (passed ? "true" : "false") + "\n}\n";
            passed = write_bytes(directory + L"\\smoke.json", report.data(), DWORD(report.size())) && passed;
            finished = true; return;
        }
        if (phase == 1) { app.view.always_refresh = true; app.policy.set_always_refresh(true, now_seconds()); }
        if (phase == 2 || phase == 5) ShowWindow(app.window, SW_MINIMIZE);
        if (phase == 3) app.pause();
        if (phase == 4) SendMessageW(app.window, WM_HOTKEY, restore_hotkey, 0); // delivery test, not physical keyboard
        if (phase == 5) { app.view.always_refresh = false; app.policy.set_always_refresh(false, now_seconds()); }
        if (phase == 6) app.restore();
        if (phase == 7) { app.view.memory_offset_mhz = 26.875f; app.view.presentation = druta::ui::Presentation::classic; }
        if (phase == 8) app.view.page = druta::ui::Page::advanced;
        if (phase == 9) { app.view.presentation = druta::ui::Presentation::focused; app.view.page = druta::ui::Page::monitor; }
        if (phase == 10) { app.view.page = druta::ui::Page::daily; app.client_size(1024, 768); }
        begin(app);
    }
};
} // namespace

int WINAPI wWinMain(HINSTANCE instance, HINSTANCE, PWSTR, int) {
    Options options;
    if (!parse_options(options)) {
        MessageBoxW(nullptr, L"Options: --width N --height N --scale 1..3 --classic --page daily|advanced|monitor\n"
            L"--capture FILE.bmp (save and exit) or --smoke-test EXISTING_DIRECTORY", L"Druta native preview", MB_ICONERROR); return 2;
    }
    ImGui_ImplWin32_EnableDpiAwareness();
    App app; active = &app;
    app.width = UINT(options.width); app.height = UINT(options.height);
    WNDCLASSEXW wc{sizeof(wc), CS_CLASSDC, window_proc, 0, 0, instance, nullptr,
        LoadCursorW(nullptr, IDC_ARROW), nullptr, nullptr, window_class, nullptr};
    if (!RegisterClassExW(&wc)) return 3;
    RECT rect{0, 0, options.width, options.height}; AdjustWindowRectEx(&rect, WS_OVERLAPPEDWINDOW, FALSE, 0);
    app.window = CreateWindowExW(0, window_class, L"Druta Native UI - Preview", WS_OVERLAPPEDWINDOW,
        CW_USEDEFAULT, CW_USEDEFAULT, rect.right-rect.left, rect.bottom-rect.top, nullptr, nullptr, instance, nullptr);
    if (!app.window || !app.create_device()) {
        MessageBoxW(nullptr, L"The Direct3D 9 preview window could not be created.", L"Druta native preview", MB_ICONERROR);
        if (app.device) app.device->Release(); if (app.d3d) app.d3d->Release();
        if (app.window) DestroyWindow(app.window); UnregisterClassW(window_class, instance); active = nullptr; return 4;
    }
    IMGUI_CHECKVERSION(); ImGui::CreateContext(); ImPlot::CreateContext();
    ImGuiIO& io = ImGui::GetIO(); io.ConfigFlags |= ImGuiConfigFlags_NavEnableKeyboard;
    io.IniFilename = nullptr; io.LogFilename = nullptr; // no automatic state/tuning persistence
    load_font();
    app.scale = options.scale > 0 ? options.scale : ImGui_ImplWin32_GetDpiScaleForHwnd(app.window);
    druta::ui::apply_theme(app.scale); ImGui::GetStyle().FontScaleDpi = app.scale;
    ImGui_ImplWin32_Init(app.window); ImGui_ImplDX9_Init(app.device); app.initialized = true;
    app.view.page = options.page;
    app.view.presentation = options.classic ? druta::ui::Presentation::classic : druta::ui::Presentation::focused;
    app.view.hotkey_available = RegisterHotKey(app.window, restore_hotkey, MOD_CONTROL | MOD_SHIFT, VK_F11) != FALSE;
    ShowWindow(app.window, SW_SHOW); UpdateWindow(app.window);
    Smoke smoke(app); MessageWait waiter; const bool testing = !options.smoke_dir.empty();
    bool success = true, captured_output = false; const double started = now_seconds();
    while (!app.quit) {
        MSG message;
        for (unsigned drained = 0; drained < 256 && PeekMessageW(&message, nullptr, 0, 0, PM_REMOVE); ++drained) {
            if (message.message == WM_QUIT) app.quit = true;
            TranslateMessage(&message); DispatchMessageW(&message);
        }
        if (app.quit) break;
        if (testing) {
            smoke.advance(app, options.smoke_dir);
            if (smoke.finished) { success = smoke.passed; break; }
        }
        const double now = now_seconds();
        if (app.policy.telemetry_due(now)) {
            ++app.fixture_sample_ticks; app.policy.sampled(now); // reserved cadence; no driver read
        }
        if (app.policy.render_due(now) && now >= app.device_retry_at) {
            std::wstring capture;
            if (testing && !smoke.captured && smoke.captures[smoke.phase] && app.frames-smoke.frames >= 2)
                capture = options.smoke_dir + L"\\" + smoke.captures[smoke.phase];
            if (!options.capture.empty() && app.frames >= 3) capture = options.capture;
            const bool rendered = app.render(capture);
            app.policy.rendered(now_seconds());
            if (!capture.empty() && rendered) {
                if (testing) smoke.captured = true;
                if (!options.capture.empty()) { captured_output = true; break; }
            }
        }
        if ((!options.capture.empty() || testing) && now_seconds()-started > 45) { success = false; break; }
        double delay = app.policy.wait_seconds(now_seconds(), app.device_retry_at);
        if (testing) delay = std::min(delay, std::max(0.001, smoke.start+2.0-now_seconds()));
        if (!options.capture.empty()) delay = std::min(delay, 0.25);
        waiter.wait(delay);
    }
    if (testing && !smoke.finished) success = false;
    if (!options.capture.empty() && !captured_output) success = false;
    if (app.view.hotkey_available) UnregisterHotKey(app.window, restore_hotkey);
    ImGui_ImplDX9_Shutdown(); ImGui_ImplWin32_Shutdown(); app.initialized = false;
    ImPlot::DestroyContext(); ImGui::DestroyContext();
    app.device->Release(); app.d3d->Release();
    if (IsWindow(app.window)) DestroyWindow(app.window);
    UnregisterClassW(window_class, instance); active = nullptr;
    return success ? 0 : 5;
}
