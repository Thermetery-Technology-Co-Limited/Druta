# RM escape transport in the native core: design

Status: design only, nothing implemented. Line numbers are `src/druta/nvbackend.py` on `cpp/core` at 9b72e74
unless another file is named. This document builds on an earlier design-panel output. Where the two disagree,
the Python code was re-read and wins; section 3.10 lists every such correction.

Terms:

- **escape**: one call of `gdi32!D3DKMTEscape(const D3DKMT_ESCAPE*)`.
- **packet**: the private driver data an escape carries. Every RM packet starts with a 17-word (68-byte) NVDA
  header.
- **transport**: a captured `(header[17], fields)` pair, where `fields` are `hAdapter, hDevice, Type, Flags,
  hContext`.

## 1. Scope and non-goals

Druta does not allocate an RM client. It borrows the client that NVAPI already holds, in three ways:

1. **Capture.** Druta temporarily patches the first bytes of `gdi32!D3DKMTEscape` in its own process and calls
   an ordinary read-only NVAPI getter. nvapi64 issues its own escape through the patched entry. The thunk
   copies the 17-word header (`hClient` is word 12, `hObject` is word 13, words 6-10 are opaque routing words)
   and the escape fields, and changes nothing. It restores the original bytes before the real function runs.
2. **Replay.** Afterwards Druta issues escapes directly, without a hook. It reuses the captured header and
   changes only the command, the sizes and the parameters. Each escape goes to an adapter handle that Druta
   opens itself and matches by PCI address (4767-4811).
3. **Splice (rail writer only).** While hooked, Druta recognizes nvapi64's own rail GET, lets it complete,
   rewrites the same buffer into the paired SET and forwards it once (6954-7080).

| hooked operation | getter called while patched | Python |
|---|---|---|
| power-transport capture | `ClientPowerPoliciesGetInfo`, id `0x34206D86` (nvbackend.py:173) | `_capture_current_limit_transport` 5027-5121 |
| legacy clock-lock capture | `ClientVoltRailsGetControl`, id `0xA3070DB0` (nvbackend.py:201), V1 block | `_capture_legacy_clk_transport` 4813-4870 |
| rail-writer splice | the same rails getter, `control_version` from the rail profile | `_write_rail_records_locked` 6954-7080 |

**In scope:**

- The gdi32 `D3DKMT*` exports, treated as capabilities.
- Adapter opening and PCI matching.
- The raw escape (`_legacy_clk_escape`).
- The patcher and its thunk, with the three policies.
- The per-GPU transport cache and its validation (`_rm_power_transport`, `_current_limit_transport_layouts`).
- `_legacy_clk_limit_records`.
- The rail splice, after its Python preconditions.
- Later: the pure current-limit tables, as `core/current_limits`.

**Stays in Python for now:**

- Every consumer outside nvbackend: `perf_diagnostics`, `client_power_runtime`, `clock_bios_runtime`,
  `clock_vf_*`, `fan_*runtime`, `gr_gating_live`, `grpg/lpwr/ms_runtime`, `power_integral_runtime`,
  `rated_clock_runtime`, `vfe_equation_runtime`, `voltage_*`. These keep calling `gpu._legacy_clk_escape` and
  resetting `gpu._current_limit_transport`.
- The current-limit policy logic (5182-5569).
- The rail preconditions and wrapper: `volt_rail_limits_supported`, `_volt_rail_profile`,
  `read_voltage_boost`, the `settle_vmin` re-send and `_verify_rail_records` (6920-6952, 6962-6975).
- `read_clk_lock` and `_read_legacy_clk_lock` (4636, 4703-4722).

**Non-goals:**

- Allocating an RM client, IAT hooking, or building a rail SET from scratch. None of these exist in Python.
- Persistence. Everything here is session-only: a driver restart invalidates the borrowed handles. That
  surfaces as NTSTATUS != 0, followed by a drop and a fresh capture.

## 2. Behaviour inventory (Python)

### 2.1 Escape structure and header

`GPU._Escape` (6915-6918) is `hAdapter, hDevice, Type, Flags` (u32 each), then `pPrivateDriverData` (c_void_p),
`PrivateDriverDataSize` (u32) and `hContext` (u32). That is 32 bytes on x64 (+16 pointer, +24 size, +28
hContext) and 28 bytes on x86. Python only ever runs the x64 form.

NVDA header words:

| word | meaning | word | meaning |
|---|---|---|---|
| 0 | `0x4E564441` ("NVDA") | 2 | total bytes = 68 + params |
| 1 | `0x10002` | 3 | `0x4E562A2A` |
| 4 | `0x0500002B` | 5 | context flag (output) |
| 6-10 | opaque | 11 | completion (output) |
| 12 | hClient | 13 | hObject |
| 14 | RM command | 15 | parameter bytes |
| 16 | RM status (0 = success) | | |

The envelope constants come from `clock_bios_runtime.py:33`, `fan_policy_runtime.py:33` and
`clock_vf_registry.py:30`. Words 14-16 are defined at `rail_transport.py:15-17`. The nvbackend transport never
checks words 0, 1, 3, 4, 5 or 11: it copies them from the capture. Only the runtime modules check the envelope.

Live escape fields are recorded for one device and driver only: an RTX 5080 on 617.42. There they are
`hAdapter 0x40000CC0 (1073745088), hDevice 0, Type 0, Flags 8, hContext 0`
(`experiments/pcie-speed-5080-61742-20261105.json:49-55`,
`experiments/individual-power-gating-validation-20261102.json:28-34`). Nothing validates these fields. They are
reused verbatim, except `hAdapter`, which is replaced on every escape (4805). They must never become constants.

### 2.2 Exports and adapter identification

`_legacy_clk_gdi` (4751-4765) loads `gdi32.dll` and requires `D3DKMTEnumAdapters2`, `D3DKMTQueryAdapterInfo`,
`D3DKMTCloseAdapter` and `D3DKMTEscape`. On AttributeError or OSError it returns `None`. It binds each export
as `long fn(void*)`. Export presence is the only check.

`_legacy_clk_escape(packet, fields)` (4767-4811):

1. `nvapi.selected.get("slot","")` must fully match `0+:([0-9a-fA-F]{2}):([0-9a-fA-F]{2})\.([0-7])`
   (4782-4785). That means domain all zeros, two hex digits each for bus and device, and function 0-7.
   Otherwise it returns `None`.
2. If `_legacy_clk_gdi()` is `None`, it returns `None` and calls no gdi export
   (tests/test_legacy_frequency_lock.py:157-168).
3. It calls `D3DKMTEnumAdapters2({16, items[16]})`. Each item is `{handle u32, luid u32[2], sources u32,
   preferred i32}`, 20 bytes. A nonzero return gives `None`, and nothing is closed (4791-4793).
4. For each of the first `min(count,16)` items, it calls `D3DKMTQueryAdapterInfo({handle, 6, &u32[3], 12})`.
   Type 6 is `KMTQAITYPE_ADAPTERADDRESS`. An item matches when the status is 0 and `(bus, device, function)`
   equals the slot (4795-4801).
5. If there is not exactly one match, it returns `None` (4802-4803).
6. Otherwise it calls `D3DKMTEscape` with `fields`, `hAdapter` set to the match, `&packet` and
   `sizeof(packet)`. It returns the NTSTATUS as a signed `c_long` (4804-4808).
7. A `finally` block closes every enumerated handle, even when no escape was issued (4809-4811;
   tests/test_legacy_frequency_lock.py:119-155).

Adapter handles are never cached. The function enumerates again on every escape (comment at 4768-4770) and
takes no lock of its own. Main's Python does not use `D3DKMTOpenAdapterFromLuid`, `D3DKMTEnumAdapters`,
`D3DKMTOpenAdapterFromHdc` or `D3DKMTOpenAdapterFromGdiDisplayName` anywhere.

### 2.3 Hook install and remove

**The two captures** (4817-4870, 5042-5121):

1. Take `GPU._RAIL_HOOK_LOCK`, a process-wide `threading.RLock` (6913).
2. If `_legacy_clk_gdi()` is `None`, return `None` and install no hook (4818-4820, 5043-5045;
   test_legacy_frequency_lock.py:170-180).
3. Record `owner = GetCurrentThreadId()` and `address = &D3DKMTEscape`. Save the 14 bytes at that address.
4. Call `VirtualProtect(address, 14, 0x40 PAGE_EXECUTE_READWRITE, &old)`. On failure, return `None`
   (4856-4857, 5109-5110).
5. Write the patch: `FF 25 00 00 00 00` (`jmp qword ptr [rip+0]`), then the 8-byte little-endian address of the
   WINFUNCTYPE callback. Call `FlushInstructionCache(GetCurrentProcess(), address, 14)` (4859-4862,
   5106-5107, 5112-5113).
6. In `finally`, call `restore()`, then `VirtualProtect(address, 14, old)` (4865-4869, 5116-5120). The page is
   RWX for the whole hooked window.

`restore()` writes the saved bytes back and flushes (4835-4838, 5060-5063). The thunk forwards by calling the
same address after `restore()`: `proto(address)(ptr)` (4852, 5097). There is no trampoline and no relocated
code.

**The rail writer** (6977-7079) differs:

- The hook lock is taken by its caller, `_write_rail_records` (6923).
- It skips `_legacy_clk_gdi` and takes `D3DKMTEscape` directly (6977-6978).
- It never flushes the instruction cache, neither in `restore()` (6993-6994) nor on re-arm (7052).
- On a `VirtualProtect` failure it sets `_volt_rail_write_error = "Could not inspect the driver rail request.
  Nothing was written."` and returns `(False, None)` (7058-7061).
- Its `finally` sets `active=False`, then calls `restore()` and `VirtualProtect(old)` (7074-7079).

### 2.4 Capture matching

**Power capture** (5065-5104):

- Each entry first calls `restore()`.
- Inspection runs only on the owner thread, with `ptr` and `pPrivateDriverData` non-null and
  `PrivateDriverDataSize >= 68`.
- If `w[2] == size` and `w[14]` is `0x2080A612` or `0x20802612`, it sets `_current_limit_observed_transport =
  {command:"0x%08X", command_id, packet_bytes=w[2], parameter_bytes=w[15]}`; the last escape wins (5076-5085).
- If, in addition, `w[2] == 68 + w[15]`, it appends `(w[0:17], fields)` to `found` (5094-5096).
- Every thread then forwards. While `found` is empty, the patch is re-armed (with a flush) on any thread
  (5098-5104).
- Re-arming has no bound. The comment there says PowerPolInfo may query the architecture first.
- `observed` is reset to `{}` at the start of every attempt, before the profile checks (5034).

**Legacy capture** (4840-4852):

- Calls `restore()`.
- On the owner thread only, it accepts an escape with `PrivateDriverDataSize == 716`, `w[2]==716`,
  `w[14]==0x20803213`, `w[15]==648` and `w[17]==1`.
- Forwards, and never re-arms. A foreign thread's escape that arrives first therefore defeats the capture.

**Result** (4870, 5121): `found[0]` if the getter returns status 0 and `len(found) == 1`; otherwise `None`.

**Getter arguments:**

- Power capture: `_PwrPolInfo` (343-345; `u32 version, u8 valid, u8 count, u8 pad[2], 4 x 11 u32`, 184
  bytes), with `version = 184 | 1<<16` (`NvAPI.ver`, 297-298; 5114-5115).
- Legacy capture: `u32[0xAC8/4 = 690]` beginning `{0x10AC8, 1}` (4863-4864).

**Preconditions, checked before any hook is installed:**

- Power capture: `nvapi.ok`, and a non-empty `CURRENT_LIMIT_GENERATION_POLICIES[arch()]` (4880; 5023-5025,
  5035-5036). It also needs a PowerPolInfo interface (5038-5040).
- Legacy capture: `nvapi.ok` and `VoltRailsCtlGet` (4814-4816).

### 2.5 Rail-writer splice (6996-7073)

On each thunk entry, `calls += 1`.

1. **Non-owner thread.** The callback sets `aborted` and the error `"Another driver thread interrupted rail
   detection. Retry the write."` (7000-7003).
2. **Owner thread inspection.** This runs only when the splice is not yet done, not aborted, and `68 <= size <=
   8192`. The callback copies the payload. While fewer than 8 are stored, it appends
   `rail_transport.packet_summary` to the observed packets. If `recognize_packet(payload, rail_mask)` returns
   a layout, it remembers `(data, size, payload, layout)` (7004-7013). Any exception sets `aborted` with
   `"Rail packet validation failed: %s"` (7014-7016).
3. **Forward.** It calls `restore()`, then forwards the escape (7017-7018).
4. **Splice**, when step 2 matched (7019-7050):
   1. The escape must still have the same `data` and `size`. Otherwise: `"native GET changed the packet buffer
      or size"`.
   2. `rm_status` is the u32 at byte 64. `rc` and `rm_status` must both be 0. Otherwise: `"native GET failed
      (NTSTATUS 0x%08X, RM 0x%08X)"`.
   3. `recognize_packet(response)` must return the same layout. Otherwise: `"native GET response does not
      match its request layout and mask"`.
   4. `current = original[:record0*4] + returned[record0*4:]`. This keeps the request header, because the GET
      output carries a valid mask that the SET constructor would leave zero.
   5. `prepare_write(current, mask, records, boost)`. The result is copied over the driver's own buffer.
   6. It sets `done=True` and `status=0xFFFFFFFF`, and records the protocol (get/set command, packet size,
      getter version).
   7. It forwards again, which issues the SET. Then `status = w[16]` if `rc==0`, else `rc & 0xFFFFFFFF`. The
      getter receives the SET's `rc`.
   8. Any exception in these steps sets `aborted` with `"Could not preserve the current native rail record:
      %s"`.
5. **Re-arm.** Otherwise the callback re-arms, but only on the owner thread, while the session is active, not
   aborted, not done and `calls < 16` (7051-7052).
6. **After the getter returns.** If nothing was done and no error was set, the error becomes `"The driver did
   not emit a recognized rail-control packet. Nothing was written. Getter status: %d. Copy rail diagnostics
   for this driver."` (7069-7073).

The function returns `(done, status)`. At most one SET is ever issued. The known layouts (1104/1036, 976/908
and 716/648; rail_transport.py:38-45) are already ported to `native/src/core/rail_transport.h`, and
`tests/test_native_core.py` runs both against the same tests.

### 2.6 Transport cache and validation

**`_rm_power_transport`** (5123-5162) returns `_current_limit_transport` when one is cached. Otherwise it
clears the layout choice and captures. If the capture returns `None`:

- When `observed` is non-empty and `(command_id, packet_bytes, parameter_bytes)` is not a
  `_CURRENT_LIMIT_LAYOUTS` key (4916), it raises **`CurrentLimitUnsupported`** `"unsupported current-policy
  transport: observed 0x%08X, %d/%d bytes"`.
- Otherwise it raises **`ValueError`**: `"…, but the capture did not complete"` or `"unsupported current-policy
  transport: no recognized power GET captured"`.

Cached and new transports are then both validated. The header must have `len(header)==17`, `header[14]` in
`{0x2080A612, 0x20802612}`, and `header[2]==68+header[15]`. Otherwise the transport and the choice are
dropped, and `CurrentLimitUnsupported("unvalidated current-policy transport geometry")` is raised.

**`_current_limit_transport_layouts`** (5169-5180) keys the layout by `(h[14], h[2], h[15])` (5164-5167).

- An unknown key clears only the choice. The transport is kept because other consumers reuse it, and the same
  `CurrentLimitUnsupported` is raised.
- A known key returns the layout plus its fallbacks (4989).

**`_legacy_clk_limit_records`** (4724-4749) uses the cached transport or captures one.

- **Packet:** 21 words. The header, then `{2, 0, lo32(&records), hi32(&records)}`, with `[2]=84`,
  `[14]=0x20802077`, `[15]=16`, `[16]=0`.
- **Records:** `u32[2][82]`, with `[0][0]=0x4C` and `[1][0]=0x4B`.
- **Success:** `status == 0`, `[16] == 0` and `[17] == 2`. Otherwise the transport is dropped and `None` is
  returned.
- It never issues the paired SET.

### 2.7 Error model

- **Two classes.** `CurrentLimitUnsupported(ValueError)` (1695-1700) means a fixed property of the adapter and
  driver. A plain `ValueError` means a failed read that a retry may recover. `exc.fixed = True` marks a probe
  rejection that belongs to the layout (5223-5233). `RM_CURRENT_POLICY_SIZE_REJECTED = 0x1F` (1692).
- **Raw escape.** `None` means there is nothing to issue on: a slot, export, enumeration or match failure. An
  int is the NTSTATUS.
- **Policy probes and requests.** On `None` or a nonzero status, `_current_limit_probe_info` (5217-5220) and
  `_current_limit_rm` (5303-5306) drop the transport and the layout choice. Both raise `ValueError("policy
  request failed (NTSTATUS %s, RM 0x%X)")`. Because `None != 0`, an unavailable escape prints `NTSTATUS None`.
  With NTSTATUS 0, RM 0x1F on a GET raises `CurrentLimitUnsupported("policy GET 0x%08X size %d rejected (RM
  0x%X)")` (5307-5309). Any other RM status, and any RM refusal of a SET, raises `ValueError`.
- **Legacy records.** `_legacy_clk_limit_records` drops its transport and returns `None`.
- **Hook layer.** It raises nothing. A capture returns `None`; the rail writer returns `(False, None|status)`
  and sets `_volt_rail_write_error`.
- **Main and the legacy branches classify a missing gdi export differently.**
  - On main, an image without `D3DKMTEnumAdapters2` reaches the `ValueError` "no recognized power GET
    captured", which is a failed read.
  - On `codex/windows-7-support`, and identically on `codex/windows-vista-support`, `_rm_power_transport`
    raises `CurrentLimitUnsupported("unsupported current-policy transport: gdi32 lacks D3DKMTEnumAdapters2 or
    the other adapter APIs on this Windows version")`. It does so only when `not observed and _legacy_clk_gdi()
    is None` (branch nvbackend.py:5173-5179).
  - The Windows 7 branch's `read_clk_lock_status` also returns a named refusal (branch 4704-4712).
  - Neither branch opens adapters any other way: both still require `D3DKMTEnumAdapters2` (branch 4782, 4815).

### 2.8 Locks, retries, timeouts

- **Lock order.** The loop at 9129-9152 wraps every public entry point that reaches a capture or the rail
  writer in `_synchronized`, the per-GPU `RLock` (1609-1618). That covers `get_current_limits`,
  `set_current_limit_ma`, `read_clk_lock[_status]`, `set/reset_volt_rail_limits` and
  `apply/restore_hold_headroom`. So the GPU lock is taken first, then `_RAIL_HOOK_LOCK`.
- **One hook at a time.** Two GPU objects never hold hooks concurrently (tests/test_volt_rails_legacy.py:155-183).
- **Re-arm bounds.** Power capture: unbounded until something is found. Legacy capture: never re-arms. Rail
  splice: re-arms while `calls < 16`.
- **No timeouts, sleeps or retry loops** in the transport itself. Consumers own the retries. For example,
  `PerfDiagnosticsReader(attempts=2)` allows 1-3 attempts (perf_diagnostics.py:217-219) and resets the
  transport between them.
- **Readback.** Transport-level readback is the response checks in 2.5 and 2.6. The value-level exact readback
  of a rail write (`_verify_rail_records`) stays in Python.

## 3. Native design

### 3.1 Files and layering

```
native/src/win/d3dkmt.h                 D3DKMT layouts for both bitnesses; static_asserts; no windows.h
native/src/backend/gdi.{h,cpp}          gdi32 exports as capabilities, EscapeCapability, adapter opening, escape_raw
native/src/backend/hook.{h,cpp}         CodePatcher (VirtualProtectPatcher, BufferPatcher), HookSession, hook_entry
native/src/backend/rm_transport.{h,cpp} RmTransport cache, power_transport, power_layouts, legacy_clk_records,
                                        rail_write_locked
native/src/core/current_limits.{h,cpp}  later increment: pure layout tables and capacity (Linux-testable)
native/src/core/abi.cpp                 C ABI additions (3.7)
src/druta/native.py                     bindings; maps dc_error back to ValueError / CurrentLimitUnsupported
```

The layers, bottom up:

- Layer 0: `escape_raw`.
- Layer 1: the hook's captures and the rail splice.
- Layer 2: the cache and its validation.

The Python consumers sit on top.

### 3.2 Layouts (`win/d3dkmt.h`)

```cpp
using KmtFn = long(NTAPI*)(void*);   // every D3DKMT entry point (Python: argtypes [c_void_p], restype c_long)
struct D3DKMT_ESCAPE { uint32_t hAdapter, hDevice, Type, Flags; void* pPrivateDriverData;
                       uint32_t PrivateDriverDataSize, hContext; };                          // 32 / 28
struct D3DKMT_ADAPTERINFO { uint32_t hAdapter, LuidLow; int32_t LuidHigh; uint32_t NumOfSources;
                            int32_t bPresentMoveRegionsPreferred; };                         // 20
struct D3DKMT_ENUMADAPTERS2 { uint32_t NumAdapters; D3DKMT_ADAPTERINFO* pAdapters; };        // 16 / 8
struct D3DKMT_ENUMADAPTERS  { uint32_t NumAdapters; D3DKMT_ADAPTERINFO Adapters[16]; };      // 324
struct D3DKMT_QUERYADAPTERINFO { uint32_t hAdapter, Type; void* pPrivateDriverData;
                                 uint32_t PrivateDriverDataSize; };                          // 24 / 16
struct D3DKMT_ADAPTERADDRESS { uint32_t BusNumber, DeviceNumber, FunctionNumber; };          // 12
struct D3DKMT_CLOSEADAPTER { uint32_t hAdapter; };                                           // 4
struct D3DKMT_OPENADAPTERFROMGDIDISPLAYNAME { wchar_t DeviceName[32]; uint32_t hAdapter, LuidLow;
                                              int32_t LuidHigh; uint32_t VidPnSourceId; };   // 80
struct D3DKMT_OPENADAPTERFROMHDC { void* hDc; uint32_t hAdapter, LuidLow; int32_t LuidHigh;
                                   uint32_t VidPnSourceId; };                                // 24 / 20
constexpr uint32_t KMTQAITYPE_ADAPTERADDRESS = 6;
```

Sizes are asserted per build (x64: 32/20/16/24; i686: 28/20/8/16). The Linux tests assert both variants
through fixed-width mirrors. Main's Python exercises only `ESCAPE`, `ADAPTERINFO`, `ENUMADAPTERS2`,
`QUERYADAPTERINFO`, `ADAPTERADDRESS` and `CLOSEADAPTER`. The other layouts are taken from the public WDK headers
and have no Python counterpart.

### 3.3 Capabilities, adapter opening, `escape_raw` (`backend/gdi`)

Exports resolve through `loader()`, which gains a third library, `"gdi32"`. The test resolver serves it exactly
as it serves `"nvapi"` and `"nvml"`. A `"user32"` library is needed only by the display-name and HDC methods.
The kernel32 functions are imported statically, since kernel32 is the core's one static import
(native/README.md): `VirtualProtect`, `FlushInstructionCache`, `GetCurrentProcess`, `GetCurrentThreadId`.

```cpp
enum class AdapterMethod { None, Enum2, Enum, GdiDisplayName, Hdc };
struct EscapeCapability {                 // computed once per process: export presence does not change
    bool gdi32_loaded, escape, query_adapter_info, close_adapter;
    bool enum_adapters2, enum_adapters, open_from_display_name, open_from_hdc;
    AdapterMethod method;                 // the first present of Enum2, Enum, GdiDisplayName, Hdc
    bool complete() const;                // escape && query_adapter_info && close_adapter && method != None
    std::string detail;                   // "" when complete, else names exactly the missing exports
};
const EscapeCapability& escape_capability();

enum class EscapeOutcome { Issued, Unavailable };
struct EscapeResult { EscapeOutcome outcome; long ntstatus; RmError why; };   // why: only when Unavailable
EscapeResult escape_raw(Gpu&, void* packet, uint32_t size, const EscapeFields& fields);
```

Rules:

1. **Faithful default: `Enum2`.** It is the only method main implements, and it behaves exactly as described
   in 2.2.
2. **Fallback methods, added in a separate increment after `Enum2` parity.** The fallbacks are selected by
   export presence only, never by a Windows or driver version:
   - `Enum` uses `D3DKMTEnumAdapters` with a fixed 16-entry array, and the same loop as `Enum2`.
   - `GdiDisplayName` and `Hdc` walk `EnumDisplayDevicesW` and open each `\\.\DISPLAYn`. They query type 6 on
     each opened handle, de-duplicate matches by LUID, and close every handle.

   A method that is present but fails at run time does not fall through to the next one; this is the NVML
   v1/v2 rule in `backend/nvml.cpp`. None of the fallbacks has a Python counterpart, and none is tested on
   hardware (section 5).
3. **No `D3DKMTOpenAdapterFromLuid`.** It needs a LUID that only enumeration supplies. The public
   documentation also suggests it is absent on Windows 7. That is an educated guess; the probe lists the
   export, so a probe report can confirm it.
4. **Slot matching.** Add `escape_slot(std::string_view)` to reproduce the 2.2 regex exactly. Do not reuse
   `druta::parse_slot`, which also accepts surrounding whitespace and a one-digit function.
5. **Order of steps.** Slot, then capability (no gdi call when it is incomplete), then enumerate, query, match
   exactly one, escape. Every opened handle is closed by a scope guard. Handles are never cached.
6. **Buffer and result.** The caller's buffer is used in place. `why` separates fixed causes (the capability is
   absent) from transient ones (enumeration failed, zero or several matches, unparsable slot). The Python
   binding maps `Unavailable` to `None`.
7. **Missing-export wording.** When `D3DKMTEscape` itself is missing (XP/XPDM), `detail` reuses the probe's
   wording (native/src/probe/druta_probe.cpp:231-232). Otherwise it names the missing exports.

### 3.4 The hook (`backend/hook`)

```cpp
class CodePatcher {                                    // one address, N bytes
public:
    virtual ~CodePatcher() = default;
    virtual bool begin(void* address, size_t n) = 0;  // save n bytes; VirtualProtect(address, n, 0x40, &old)
    virtual void write_patch() = 0;                   // memcpy patch; FlushInstructionCache
    virtual void write_original() = 0;                // memcpy saved bytes; FlushInstructionCache
    virtual void end() = 0;                           // write_original(); VirtualProtect(old)
    virtual bool patched() const = 0;
};
class VirtualProtectPatcher final : public CodePatcher { /* kernel32; the real one */ };
class BufferPatcher final : public CodePatcher { /* owned scratch bytes; begin() can be told to fail */ };
```

The core selects `BufferPatcher` whenever a test resolver is installed, and always on non-Windows builds. A
served fake `D3DKMTEscape` is a libffi trampoline and must never be overwritten.

Thunk bytes, with `kPatchBytes = sizeof(void*) == 8 ? 14 : 5`:

```
x64, 14 bytes (identical to nvbackend.py:4859-4861):
    FF 25 00 00 00 00            jmp qword ptr [rip+0]
    <8 bytes LE: (uint64_t)&hook_entry>
x86, 5 bytes:
    E9 <rel32>                   rel32 = (uint32_t)((uintptr_t)&hook_entry - ((uintptr_t)address + 5))
```

`hook_entry` is declared `extern "C" long NTAPI hook_entry(const D3DKMT_ESCAPE*)`. On x86 that is stdcall: the
callee pops 4 bytes, so nvapi gets back the same stack the real function would leave. The entry forwards by
calling `address` after `write_original()`, as Python does. There is no trampoline.

The session is a single static instance, because the patch is process-wide:

```cpp
enum class HookPolicy { PowerCapture, LegacyClkCapture, RailSplice };
struct HookSession {
    std::atomic<bool> active{false};
    void* address; KmtFn original;            // original == address
    uint32_t owner_thread;                    // GetCurrentThreadId() at install
    HookPolicy policy; CodePatcher* patcher;
    // the Python closures' locals: found (capacity 2 decides "exactly one"), observed, done, status,
    // error, observed_packets[8], protocol, matched; plus preallocated rail buffers (8192-byte copy, SET)
    std::atomic<bool> aborted; std::atomic<uint32_t> calls;
};
static HookSession g_session;  // namespace scope, not function-local statics: the MinGW win32
static Mutex g_hook_mutex;     // thread model is safer without guarded local statics
```

`hook_entry` is bitness-neutral, and each policy follows its 2.4 or 2.5 steps one for one:

- **`!active`** (a foreign thread arrives after uninstall): forward to `original`. Python would call a freed
  closure here. This is a deliberate safety deviation.
- **PowerCapture:** `write_original()`. On the owner thread with a non-null `ptr`, run the 2.4 predicates.
  Forward. Re-arm while `found` is empty, on any thread.
- **LegacyClkCapture:** `write_original()`. On the owner thread with a non-null `ptr`, run the predicates.
  Forward. Never re-arm.
- **RailSplice:** increment `calls`. On a foreign thread, set `aborted` and the error. Inspect only on the
  owner thread. `write_original()`, forward, then either splice and forward the SET, or re-arm under the 2.5
  condition.

**Thunk rules.**

- No C++ exception may leave `hook_entry`. Every step that can throw is wrapped in `try { } catch (...)`, and
  the handler sets the same string Python's `except Exception` sets at that point.
- The rail splice does not allocate inside the thunk. Its buffers are reserved before install.
- Only the owner thread mutates session state. A foreign thread touches only atomics and the write-once error.

**Install and uninstall** follow 2.3. The captures require a complete capability first. The rail splice keeps
Python's narrower check, `D3DKMTEscape` only (open question 3). A failed `begin()` makes a capture return
`None`, and makes the rail splice report "Could not inspect…".

**Deviation D1.** The native rail splice also flushes after restore and after re-arm. This is harmless and no
test can observe it.

**Deviation D5 (implemented in increment 2).** When `_rm_power_transport` fails because the capture stopped at
the gdi32 check (`_legacy_clk_gdi()` would be `None`), and for no earlier reason such as a missing generation
policy or getter, the native message is main's retryable text with the capability detail appended in
parentheses: `"unsupported current-policy transport: no recognized power GET
captured (gdi32 lacks D3DKMTEnumAdapters2)"`. The classification is unchanged (D4); only the text says which
export is missing. The Python twin's message is the bare text, so the parity test compares the prefix there.

**Increment 2 as built.** `backend/hook` holds the session as namespace-scope atomics; `original`, `owner`,
`policy` and `patcher` are atomics so a foreign thread's reads are defined. `found` saturates at two entries.
The buffer patcher is a flag pair (`active`, `patched`): it writes nothing anywhere, which is the whole point.
`VirtualProtectPatcher::end()` clears its `active` flag before restoring the protection, so a late foreign
re-arm after uninstall is a no-op instead of a write to a protected page (the residual race in 3.8 rule 6
remains). `CapturedTransport` carries a `header_count` so a fixture can store a header of another length and
have the next use reject it, as Python does for `len(header) != 17`. The rail splice is increment 4.

**Supporting additions.**

- `PwrPolInfo` (184 bytes, version `0x100B8`) goes into `backend/structs.h`.
- `PowerPolInfo` (0x34206D86) and `VoltRailsCtlGet` (0xA3070DB0) join the NVAPI interface table.
- The power capture also needs `arch()`, which already exists as `dc_gpu_architecture`, and the pure generation
  table (4880-4910).

### 3.5 Cache and validation (`backend/rm_transport`)

```cpp
struct EscapeFields { uint32_t hAdapter, hDevice, Type, Flags, hContext; };
struct CapturedTransport { std::array<uint32_t, 17> header; EscapeFields fields; };
struct Observed { bool seen; uint32_t command_id, packet_bytes, parameter_bytes; };
struct RmTransport {                                // member of backend::Gpu, guarded by Gpu::lock()
    std::optional<CapturedTransport> power;         // _current_limit_transport
    int layout_choice = -1;                         // _current_limit_layout_choice (-1 = None)
    std::optional<CapturedTransport> legacy_clk;    // _legacy_clk_transport
    Observed observed{};                            // _current_limit_observed_transport
    uint32_t power_policy_mask = 0;                 // _power_policy_mask
};
```

- `power_transport(Gpu&)` ports `_rm_power_transport`, with both error classes and every string from 2.6.
  Values that tests or fixtures assign directly are validated when used, not when set. This keeps
  `test_a_captured_header_of_unknown_geometry` and `test_a_malformed_envelope_is_not_a_transport_at_all`
  meaningful.
- `power_layouts(Gpu&)` ports `_current_limit_transport_layouts`. It needs only the key table (4916-5005),
  copied verbatim.
- `legacy_clk_records(Gpu&, uint32_t out[2][82]) -> bool` ports `_legacy_clk_limit_records`. The record array
  lives on the issuing frame, and its address goes into words 19-20 (`hi32 = 0` on x86).
- `rail_write_locked(Gpu&, control_version, rail_mask, records, boost)` ports `_write_rail_records_locked`
  after its Python preconditions. It takes `Gpu::lock()` first, then the hook mutex.

### 3.6 Error model across the ABI

```c
typedef int32_t dc_error_kind;
enum { DC_ERR_NONE = 0,
       DC_ERR_TRANSIENT = 1,     /* ValueError after touching the driver: a failed read; retry may recover */
       DC_ERR_INVALID = 2,       /* ValueError raised before the driver was touched */
       DC_ERR_UNSUPPORTED = 3 }; /* CurrentLimitUnsupported: a fixed property */
typedef struct dc_error {
    uint32_t size;               /* sizeof(dc_error), set by the caller */
    dc_error_kind kind;
    int32_t fixed;               /* ValueError.fixed */
    int32_t has_ntstatus;        /* 0 when no escape was issued (Python prints "None") */
    int32_t ntstatus;            /* signed, as ctypes c_long reports it */
    uint32_t rm_status;          /* packet word 16 after the escape */
    char message[1024];          /* exactly str(exc) of the Python path */
} dc_error;
```

The binding maps the kinds back to Python:

- `UNSUPPORTED` becomes `CurrentLimitUnsupported(message)`.
- `TRANSIENT` and `INVALID` become `ValueError(message)` with `exc.fixed = bool(fixed)`.
- `DC_E_UNAVAILABLE` from the raw escape becomes `None`.

The consumer kinds `identity` and `malformed` stay with the Python consumers.

**D4: classifying a missing capability. Default: main's behaviour; the change is the user's decision.** When
`escape_capability()` is incomplete and nothing was observed, main raises the retryable `ValueError`
`"unsupported current-policy transport: no recognized power GET captured"` (2.6, 2.7), and the native
`power_transport` reproduces that, so the parity tests hold. The legacy branches instead raise the fixed
`CurrentLimitUnsupported("... gdi32 lacks D3DKMTEnumAdapters2 or the other adapter APIs on this Windows
version")`. CLAUDE.md says main's refusal logic takes precedence over the branches' older refusals and that
remaining differences are reported to the user rather than carried forward. So the fixed classification (which
AGENTS.md's "a missing capability is not a transient read failure" would favour, and which would stop a
Windows image without the exports from retrying forever) is listed as open question 1 and is not implemented
until the user decides. Whichever is chosen, the capability detail text (3.3, rule 7) is attached to the
message so the user sees which export is missing.

### 3.7 C ABI additions (in the style of `drutacore.h`)

`DC_E_UNAVAILABLE` is already `-8`, so the new status takes the next value. `DC_ABI_VERSION` stays 1, because
functions are only added. Every `DC_API` body catches everything and returns `DC_E_FAILED` with kind
`TRANSIENT` and the message "out of memory", so no exception crosses the ABI.

```c
enum { DC_E_FAILED = -9 };    /* the operation failed; the dc_error passed says how */

typedef struct dc_escape_capability {
    uint32_t size;
    int32_t gdi32_loaded, escape, query_adapter_info, close_adapter;
    int32_t enum_adapters2, enum_adapters, open_from_display_name, open_from_hdc;
    int32_t complete;
    int32_t method;           /* 0 none, 1 enum2, 2 enum, 3 gdi display name, 4 hdc */
    uint32_t patch_bytes;     /* 14 on x64, 5 on x86 */
    char detail[256];
} dc_escape_capability;
/* Never fails for a missing capability: the struct says what is missing. */
DC_API dc_status DC_CALL dc_escape_capability_get(dc_escape_capability* out);

typedef struct dc_escape_fields { uint32_t hAdapter, hDevice, Type, Flags, hContext; } dc_escape_fields;
typedef struct dc_rm_transport {
    uint32_t size; int32_t captured; uint32_t header[17]; dc_escape_fields fields;
} dc_rm_transport;
enum { DC_TRANSPORT_POWER = 1, DC_TRANSPORT_LEGACY_CLK = 2 };
typedef struct dc_rm_observed {
    uint32_t size; int32_t seen; uint32_t command_id, packet_bytes, parameter_bytes;
} dc_rm_observed;

/* _legacy_clk_escape. packet is used in place; fields.hAdapter is replaced. DC_OK + *ntstatus when issued;
 * DC_E_UNAVAILABLE + error when not. Every adapter handle is closed before returning. */
DC_API dc_status DC_CALL dc_rm_escape_raw(dc_gpu* gpu, uint8_t* packet, size_t size,
                                          const dc_escape_fields* fields, int32_t* ntstatus, dc_error* error);

/* The cache: get never captures; set stores unvalidated; resetting POWER also clears the layout choice. */
DC_API dc_status DC_CALL dc_rm_transport_get(dc_gpu* gpu, int32_t kind, dc_rm_transport* out);
DC_API dc_status DC_CALL dc_rm_transport_set(dc_gpu* gpu, int32_t kind, const dc_rm_transport* transport);
DC_API dc_status DC_CALL dc_rm_transport_reset(dc_gpu* gpu, int32_t kind);
DC_API dc_status DC_CALL dc_rm_observed_get(dc_gpu* gpu, dc_rm_observed* out);

/* _rm_power_transport (captures on a miss, validates) and _current_limit_transport_layouts. */
DC_API dc_status DC_CALL dc_rm_power_transport(dc_gpu* gpu, dc_rm_transport* out, dc_error* error);
DC_API dc_status DC_CALL dc_rm_power_layouts(dc_gpu* gpu, uint32_t* count, dc_error* error);

/* _legacy_clk_limit_records: *available = 0 for Python's None. */
DC_API dc_status DC_CALL dc_legacy_clk_records(dc_gpu* gpu, uint32_t records[2][82], int32_t* available);

typedef struct dc_rail_write_request {
    uint32_t size; uint32_t control_version; uint32_t rail_mask;
    dc_rail_record records[2]; uint32_t record_count; uint32_t boost;
} dc_rail_write_request;
typedef struct dc_rail_write_result {
    uint32_t size; int32_t done; int32_t has_status; uint32_t status;
    char error[512];          /* _volt_rail_write_error, "" for None */
    int32_t has_protocol; uint32_t get_command, set_command, packet_size, getter_version;
    uint32_t observed_count; char observed[8][160];
} dc_rail_write_result;
DC_API dc_status DC_CALL dc_rail_write_locked(dc_gpu* gpu, const dc_rail_write_request* request,
                                              dc_rail_write_result* result);

/* Test seams. The resolver also serves "gdi32" (and "user32" for the display-name methods). While a resolver
 * is installed, BufferPatcher is used and the thunk body is reached through dc_hook_test_entry. */
DC_API int32_t DC_CALL dc_hook_test_entry(const void* escape);       /* returns the forwarded NTSTATUS */
DC_API dc_status DC_CALL dc_hook_test_state(int32_t* installed, int32_t* patched, uint32_t* calls);
DC_API void DC_CALL dc_hook_test_fail_protect(int32_t fail);         /* BufferPatcher::begin() fails */
```

### 3.8 Threading rules

1. Take `Gpu::lock()` first, then `g_hook_mutex`, which is the order Python already uses (2.8). Never take them
   in the reverse order.
2. The transport cache and every escape on a GPU are guarded by `Gpu::lock()`. Adapter handles live for one
   escape.
3. There is one hook session per process. `g_hook_mutex` serializes the three installers across GPU objects.
4. The thunk takes no lock: the owner already holds both locks. A foreign thread only forwards, re-arms (in
   the power policy) or flags an abort (in the rail policy).
5. Python's GIL serialized its thunk. The native thunk has no GIL, so foreign-thread writes must be atomics.
6. A residual race is shared with Python: a thread executing the first 14 or 5 bytes of `D3DKMTEscape` while
   they are being rewritten can crash. Only suspending threads would remove it, so keep hooked windows to one
   getter call.
7. `dc_gpu_close` discards the cache. No RM handle is freed, because none is owned.

### 3.9 Increments (each with its parity module, section 4)

1. `d3dkmt.h`, `gdi` with `Enum2`, `escape_raw`, `dc_escape_capability_get`. (Done: `tests/test_native_transport.py`.)
2. The hook, both captures, `RmTransport`, `power_transport` and `power_layouts`. (Done, with the pure tables
   in `backend/power_layouts`: `tests/test_native_rm_transport.py`, `tests/test_native_power_layouts.py`.)
3. `legacy_clk_records`. (Done: `VfLock::legacy_clk_limit_records` and `legacy_clk_lock` through `RmTransport`
   and `escape_raw`; `dc_legacy_clk_records`; `tests/test_native_legacy_clk.py`.)
4. The rail splice. (Done: `rail_write_locked` in `backend/hook`, `dc_rail_write_locked`;
   `tests/test_native_rail_splice.py`. The error channel between threads is an atomic code, turned into
   Python's text by the owner after the getter returns, so last-writer-wins holds without a shared string.)
5. The fallback openers, after the probe evidence in section 5.
6. The current-limit tables, then the logic. (Tables done, see increment 2. The RM operations are done:
   `CurrentLimits` in `backend/current_limits` with `abi`, `probe_info`, `rm`, `policy_table` and `capacity`,
   the `dc_cl_*` ABI, `tests/test_native_current_limits.py`. The rows, getter and verified setter are done:
   `state`, `get_current_limits`, `set_current_limit_ma`; `tests/test_native_current_limit_state.py`. The one
   unported dependency, `read_live_power_request`, is a callback the caller supplies (`dc_cl_confirm_fn`);
   the binding's stub never confirms until the client-power runtime is ported.)
7. The Python consumers, one module at a time. (In progress: the paired target binding shared by
   `perf_diagnostics` and `client_power_runtime` is done as `backend/target`, `dc_gpu_target_bind`,
   `tests/test_native_target_binding.py`; the pure `power_arbitration` decoder as `backend/power_arbitration`,
   `dc_power_arbitration_*`, `tests/test_native_power_arbitration.py`; the pure rail-limit and hold-headroom
   helpers as `backend/rail_limits`, `dc_rl_*`, `tests/test_native_rail_limits.py`; the pure client-power
   tables and geometry helpers as `backend/client_power`, `dc_cp_*`, `tests/test_native_client_power.py`; the
   controller's transport and guarded exchange as `backend/client_power_controller`, `dc_cpc_*`,
   `tests/test_native_client_power_controller.py`; the PERF layouts, transport and GET as
   `backend/perf_diagnostics`, `dc_perf_*`, `tests/test_native_perf_diagnostics.py`, with the pure decoder and
   the per-group read loop as `perf_decode_group` / `PerfDiagnostics::read_group`, `dc_perf_decode_group`,
   `dc_perf_read_group` and the embedded SHA-256 as `core/sha256`, `dc_sha256_hex` (fuzzed by
   `native/fuzz/perf_decode_fuzz.cpp`); the clock-BIOS read path (the user-clock CONTROL GET, the INFO GET
   through the paired transport, `_decode` and the two-sample `read`) as `backend/clock_bios`,
   `dc_clock_bios_*`, `tests/test_native_clock_bios.py`; the client-power metadata read (`_client_group`,
   `_metadata_once`, `_stable_locked`, `read_metadata`) as `ClientPowerController::read_metadata`,
   `dc_cpc_read_metadata`, `tests/test_native_client_power_metadata.py`, and its full read (`_once`,
   `_arbitration`, `_read_locked`, `read`) as `ClientPowerController::read`, `dc_cpc_read`,
   `tests/test_native_client_power_read.py`; the voltage-rail getters and their
   read caches as `backend/rails`, `dc_rails_*`, `tests/test_native_rails.py`, with the monotonic clock behind
   their backoff replaceable through `dc_set_clock_hook`; the legacy P0 hold and the core support for the
   verification holds (`legacy_p0_supported`, `hold_legacy_p0`, `release_legacy_p0`, the request half of
   `verification_legacy_p0`, the recovery record and lock-table entry accessors, `read_vcore_mv`) as
   `backend/legacy_p0`, `dc_legacy_p0_*`, `dc_gpu_vf_lock_recovery_get` / `_set`, `dc_gpu_vf_lock_entry_get`,
   `dc_gpu_read_vcore_mv`, `tests/test_native_legacy_p0.py`, with the two context managers composed in
   `druta.native_legacy_p0`.)

### 3.10 Corrections to the design-panel output

- The recorded `hAdapter` is `0x40000CC0` (1073745088), not `0x40001040`. All recorded fields come from one
  RTX 5080 on driver 617.42.
- The legacy capture block is `0xAC8` bytes, which is 690 words, not 688.
- `DC_E_UNAVAILABLE` is already `-8`, so `DC_E_FAILED` is `-9`.
- Taking the GPU lock before the hook lock is not a deviation. Python's public entry points already do that
  (9129-9152). `read_volt_rail_limits` is not separately `_synchronized`.
- The probe's XPDM text is at `druta_probe.cpp:231-232`.
- The legacy branch refuses only when `not observed and gdi is None`, and it has no fallback openers; those
  are new in this design.
- An unavailable escape becomes a transient "NTSTATUS None" `ValueError` in `_current_limit_probe_info` and
  `_current_limit_rm`. The panel output omitted this.

## 4. Test plan

### 4.1 Pattern

Follow `tests/test_native_fan.py`. A `Rig` (tests/test_native_telemetry.py:41) holds one set of ctypes fakes
and a call log. `both(rig, action)` runs the action twice: on a `nvbackend.GPU` built with `GPU.__new__`
(`Rig.python_gpu`), then on `native.Gpu` through `install()` / `dc_set_export_resolver`
(tests/test_native_identity.py:149). It asserts equal results, driver call sequences and fake state. A new
`tests/test_native_transport.py` adds a `TransportRig(Rig)` with the following parts.

**Fake gdi32.** `D3DKMTEnumAdapters2`, `QueryAdapterInfo`, `CloseAdapter` and `Escape` are
`CFUNCTYPE(c_long, c_void_p)` callbacks written against the 3.2 layouts, as tests/test_legacy_frequency_lock.py
does at lines 119-155. The rig is configured with an adapter list `(handle, bus, device, function)`, an
enumeration status and a set of missing export names. On the Python side, `patch.object(nvbackend.ctypes,
"WinDLL")` returns them. On the native side, the resolver serves them as library `"gdi32"`.

**Modelled RM.** The fake escape views the packet with `(c_uint32*n).from_address(pPrivateDriverData)` and
answers by command. It reuses two existing models: `fixture()` in `tests/test_current_limits.py`
(A618/A619/A61A/E61B arrays, RM 0x1F refusals) and `exercise_native_write` in
`tests/test_rail_runtime_detection.py`. It logs `(hAdapter, command, size, fields)` for every call.

**Getters that escape.** `PowerPolInfo` (0x34206D86) and `VoltRailsCtlGet` (0xA3070DB0) join
`FakeNvapi.interfaces` and call an injected `enter(escape_address)`.

- Python side: `enter` is the WINFUNCTYPE target, obtained by patching `nvbackend.ctypes.WINFUNCTYPE`, with a
  `create_string_buffer` standing in for the code bytes. This is the existing `prototype` technique in
  test_rail_runtime_detection.py. "Restore before forward" and "re-arm after an unrelated call" are asserted
  with `bytes(code)`.
- Native side: `enter` is `dc_hook_test_entry`, and the same two properties are asserted with
  `dc_hook_test_state`.

**Thread identity.** On the Python side, patch `GetCurrentThreadId` on the fake kernel32. On the native side,
call `dc_hook_test_entry` from a real `threading.Thread`. Compare outcomes.

**Escape fields in fixtures.** Use `{hDevice:0, Type:0, Flags:8, hContext:0}` with the fake `hAdapter`. This is
the recorded 5080/617.42 shape. Assert only that these fields pass through unchanged (except `hAdapter`),
never their values.

### 4.2 Modules and fixtures to re-run

- `tests/test_legacy_frequency_lock.py`: cache reuse, fresh record storage, identity and close-all, a missing
  export making no call, and a missing adapter API installing no hook.
- `tests/test_current_limit_capture_policy.py`, `test_current_limits.py`, `test_current_limits_617_geometry.py`
  and `test_current_limits_rm26.py`: the transport cases now, the policy cases once the tables move.
  `tests/fixture_current_47212.py` (`TITAN_INFO`) supplies real info params.
- `tests/test_rail_runtime_detection.py`, `tests/test_volt_rails.py` (`RailEscapeTests`, 482-600) and
  `tests/test_volt_rails_legacy.py` (130-183): the splice, the non-owner abort and serialization across two
  GPUs.
- `tests/test_rail_transport.py`: `tests/test_native_core.py` already re-collects it.
- For hardware-shaped header and field fixtures: `experiments/pcie-speed-5080-61742-20261105.json`,
  `experiments/individual-power-gating-validation-20261102.json` and `experiments/runtime-rails-*.json` (rail
  packets).

### 4.3 Failure cases (each through `both`)

**Missing exports.** Each of the four `Enum2`-path exports absent in turn: no gdi call, `None` /
`DC_E_UNAVAILABLE`, no hook installed. gdi32 absent entirely: the capability detail says so. After the fallback
increment: each method is selected by export presence alone, and a present method that fails does not fall
through.

**Alternate identities, wrong adapter.**

- An adapter at `0000:04:00.0` does not match.
- `" 0000:01:00.0"`, `"0000:1:00.0"`, `"0001:01:00.0"` and `"0000:01:00.8"` are refused with no gdi call.
- Two adapters at one address give `None`, and a QueryAdapterInfo failure skips only that handle.
- An EnumAdapters2 failure closes nothing; on success every handle is closed.
- No test gates on a developer-specific device or subsystem ID.

**Geometry mismatch.**

- Power capture: a header with `[2] != 68+[15]` is not found; an observed unknown key gives `UNSUPPORTED`; a
  cached bad envelope is dropped with `UNSUPPORTED`; an unknown layout key keeps the transport.
- Legacy capture: a packet with `[17] != 1` is not captured.
- Rail: a GET of the wrong size, command or mask is forwarded unchanged; a response with a changed buffer, size
  or layout issues no SET.

**Transient status.**

- A nonzero getter status, or two found transports, gives no transport.
- NTSTATUS != 0 on a replay drops the transport, and the next call recaptures.
- A nonzero RM status is `TRANSIENT`, except RM 0x1F on a GET, which is `UNSUPPORTED`.
- `dc_hook_test_fail_protect` makes a capture return `None` and the rail writer report "Could not inspect…".
- A foreign thread re-arms the power capture, defeats the legacy capture, and aborts a rail write.

**Readback mismatch.**

- Legacy records with `[16] != 0` or `[17] != 2` give `None` and drop the transport.
- A rail GET with nonzero rc or RM status issues no SET.
- A failed SET is never reported as success.
- Never more than one SET, even after 16 or more escapes.

**Partial capabilities.** Without PowerPolInfo, VoltRailsCtlGet or generation policies, no hook is installed.
A missing gdi32 changes no NVML/NVAPI telemetry result.

**Linux-only pure tests.** D3DKMT sizes for both bitnesses, x86 rel32 arithmetic including wrap-around, the
x64 patch bytes, and the `escape_slot` table.

## 5. Open questions

1. **D4.** Should a missing escape capability stay a failed read (`ValueError`, as on main, which the design
   implements by default) or become a fixed refusal (`CurrentLimitUnsupported`, as on the legacy branches and
   as AGENTS.md's transient-versus-unsupported rule suggests)? The change is user-visible (a fixed refusal
   stops profiles and Reset all from retrying on an image that can never succeed) and needs the user's
   decision; until then main's behaviour is kept, per CLAUDE.md.
2. Should the native text reproduce `"policy request failed (NTSTATUS None, RM 0x0)"` for an unavailable
   escape byte for byte? That wording is an accident of `None != 0`. Parity says yes.
3. The rail splice installs its hook without `_legacy_clk_gdi()` and needs only `D3DKMTEscape`. Should the
   native version keep that narrower check, or require the complete capability? The default is to keep
   Python's behaviour, because the splice never uses the adapter APIs.
4. Do `D3DKMTEnumAdapters` (documented for Windows 7) and `OpenAdapterFromGdiDisplayName` / `FromHdc`
   (documented for Vista) enumerate an NVIDIA adapter that has no display attached? Does type 6
   (`KMTQAITYPE_ADAPTERADDRESS`) answer on WDDM 1.0/1.1? The method order and the LUID de-duplication are
   educated guesses from public documentation. A probe report from Vista or 7 is the first evidence.
5. What are the first bytes of `D3DKMTEscape` in Vista and Windows 7 gdi32 (x64, x86, WOW64)? Does the
   14-byte or 5-byte overwrite stay inside one stub? This is unknown. The probe should dump 16 bytes there,
   read-only, before any hook runs on such an image.
6. On Windows 10+, `GetProcAddress(gdi32, "D3DKMTEscape")` probably resolves into win32u (an educated guess).
   Python patches whatever address it receives, and that has worked on the developers' machines. That is local
   evidence only.
7. Do Vista and Windows 7 era drivers emit a `0x2080A612` / `0x20802612` escape from PowerPolInfo at all? The
   capture's premise has been verified only on the drivers the experiments record.
8. The 32-bit path (28-byte escape, `E9` thunk, `nvapi.dll` under WOW64) has no Python counterpart and has
   never run on hardware.
9. Is the unbounded power re-arm still wanted without Python threads? The design keeps it. How often foreign
   escapes occur in the native front end is unknown.
10. `nvbackend.GPU` holds no `dc_gpu` today; `src/druta/native.py` has a separate `Gpu`. Until it does, the
    Python consumers keep their own cache. Forwarding the five cache attributes through class properties is
    proposed here but not designed.
11. Arbitrary Code Guard refuses `PAGE_EXECUTE_READWRITE`. The native front end must not enable it. Nothing in
    the repo states this today.
12. The sizes of `message` (1024) and `observed[8][160]` are chosen, not derived. `packet_summary` output is
    about 100 bytes today.
