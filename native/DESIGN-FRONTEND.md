# G4 final design: the Python front ends on `native.Gpu`, with `nvbackend.GPU` as the fallback

**Status: proposed, not implemented.** This is the design for increment G4 of the native-core port
(the front-end switch). It was produced on 2026-11-10 by three independent designs, two judges who
checked them against the code, and one synthesis. File and line references are a snapshot of
`cpp/core` at `0c117d1`; function names are the stable reference. The questions in section (h) are
the owner's to decide; each carries a default so the work can proceed until they are answered.
Nothing here changes the session-only rule: no tuning state is persisted or reapplied across a GPU
driver restart, and the factory never decides by driver version or device identity.

**Base and baseline.** This design takes the **facade** design as its base. It grafts in the ideas the judges named and corrects their errors. I re-checked the disputed points against the working tree on 2026-11-10. HEAD is `0c117d1`, so A7 (client-power SET) is committed. A8's power-limit trio is in the tree but uncommitted: it touches `native.py`, `power_limit.*`, `abi_power_limit.cpp` and `tests/test_native_power_limit.py`. `native.py` is still being edited, so its line numbers carry a `~` and the function names are the stable reference. A7, A8, B4 and G3 are assumed to have landed before G4 code starts. **The first act of G4a is to measure the gap list again on the HEAD of that day.**

---

## 0. Corrections to the inventory, the established facts and the three designs

| Claim | What the code says |
|---|---|
| Inventory: R6 "planned" | R6 has landed (`2cf47b5`). `native.Gpu` has `apply_hold_headroom`, `restore_hold_headroom`, `hold_headroom_*`, `user_rail_limits` and `adopt_foreign_hold_headroom`, plus the class default `_foreign_hold_headroom = None`. The `type(gpu)` probe at druta.py:7243 is already satisfied. |
| Inventory: A8 "to land" | `read_power_limit_mw`, `set_power_limit_mw` and `power_limit_bounds_mw` are in the working tree (native.py ~3047). `read_native_power_limit_mw` comes with B4. |
| Fact (2): railctl and the I2C controllers take `gpu._lock` | **Wrong.** `railctl.Rail` keeps only `nvapi` (railctl.py:326-329) and resolves `nvapi._i(I2C_READ_EX/WRITE_EX)` per call (350, 361) without `gpu._lock`. The controllers in `src/druta/controllers/` use their own locks: `nct3933._transaction_lock` (nct3933.py:87), `nvapi._up9512r_mutex` (up9512r.py:97-100) and the module `_ADAPTER_LOCK_GUARD`. The repository-root `i2c/` directory holds TOML profiles, not code. I2C is **not** serialized against nvbackend's `_synchronized` methods today either. The modules that take `gpu._lock` and then call NvAPI or escapes directly are the `*_runtime`, `*_live`, `*_profiles`, `vfe_*`, `perf_diagnostics`, `all_pstates_registry`, `bios_runtime` and `voltage_request_replay` modules, plus druta.py:2339 and 10545. |
| Fact (1): `_lock` used only through `with` | Confirmed. There are 17 `getattr(gpu, "_lock", nullcontext())` lines outside the backends (the facade design said 14) plus the direct `with gpu._lock` sites. No caller uses `acquire`, `release` or `locked`. |
| Fact (3): nvml "similar" | The nvml `_Half` also carries `dll_path`. `nv.dll.*` is used by bios_runtime.py:117-129 and also by profiles.py:132-136. |
| Inventory: front end writes `static[key]` | No such write exists in `src/druta`. The front ends use only `.get`, `[]` and `in` on `static`. |
| Inventory: `_power_policy_pins` strictness (risk 9) | Overstated. `power_action_profiles._validate_data` (70-72) already requires int keys and values. The other writers assign `{}` or copy a backend's dict (druta.py:2874). Keep the native setter strict. |
| Not in the inventory: constants read from the instance | These are hard `AttributeError`s on native: `self.gpu.VOLT_LIMIT_MIN/MAX/XOC_MAX_MV` (druta.py:3006-3008, 8317, 8322), `RAIL_OFFSET_*` (3560, 3567, 8329), `VOLT_LIMIT_FIELDS` (2344), rail_limit_profiles.py:129-133, tuning_profiles.py:168. Other reads use getattr defaults that **silently change a bound**: profiles.py:1384 `min(getattr(gpu, "VOLT_LIMIT_MIN_MV", 300), current_value)` (the real value is 650.0), and also profiles.py:818, 1094, 1367, 1368, 1409. |
| Not in the inventory: MSVDD sets | profiles.py:746-747 and nvbackend `reset_all` (~8931) read `_msvdd_offset_domains_written/_seen`. The core **already keeps** both sets (`ClkDom::msvdd_domains_seen()/written()`, clkdom.h:140-141, 161). Only an export is missing. The risk design's "track them in ClkDom" is wrong. |
| Not in the inventory: core lifetime | `dc_gpu_close` is a bare `delete` without the handle mutex (abi.cpp:194). `dc_gpu_open` has no exception guard: `new (std::nothrow)` covers only the allocation. `DC_ABI_VERSION` is still `1u` (drutacore.h:36), so in practice the version check catches nothing. The real guard is binding every export, and that guard does not detect a struct layout change. |
| `dc_rl_constants` already carries the volt-limit bounds (risk design) | **Wrong.** `dc_rl_constants` holds `architectures`, `max_margin_mv`, `fallback_grain_mv`, `grain_min_mv` and `grain_max_mv` (native_rail_limits.py:75-79). `VOLT_LIMIT_MIN/MAX/XOC_MAX_MV`, `RAIL_OFFSET_*` and the others need a new entry. Only the two hold-headroom constants come from `dc_rl_constants_get`. |
| "Python's `hold_legacy_p0` takes no lock" (native/README.md:214; repeated by the interface design) | **Stale.** nvbackend.py:9135 puts `hold_legacy_p0` and `release_legacy_p0` in the `_synchronized` list, and the hold runs `time.sleep(0.1)` while holding the RLock (~4513). The core drops its lock across those waits, so the poll interleaves under native where it blocked under Python. This is a behaviour difference to record and test (§d). |
| "Both backends load the same DLLs" (risk); auto step 3 is "a bug report" (facade) | Neither holds exactly. The core loads `nvapi.dll` in a 32-bit process (exports.cpp:130). nvbackend loads only `nvapi64.dll` (nvbackend.py:133). The NVML search code is separate on each side. |
| Test files that patch `druta.druta.GPU` as a constructor | Six: test_rel_bounds_ui_cli.py:20, 40; test_lpwr_rows_ui_cli.py:22, 42; test_i2c_discovery_ui.py:44; test_tune_profiles.py:273; test_gpu_switch.py:122; test_vf_lock_ui.py:41, 58. test_verification_lifecycle.py:407 uses only `druta.GPU.ARCH_TURING` and is unaffected. |
| Signatures | No real signature differences between shared members. `_synchronized` (nvbackend.py:1609-1618) hides them because it does not set `__wrapped__`. Kind differences: `msvdd_write_enabled` and `voltage_xoc_enabled` are a plain bool in nvbackend and a settable property in native. |
| Packaging | `Druta.spec` does not bundle `drutacore.dll`. Every frozen build today would fall back to Python. |
| `power_arbitration.py: GPU._current_limit_abi` (inventory) | It is a docstring (:186), not a use. |

---

## (a) The factory

### Module and names

The new module is **`src/druta/gpu_backend.py`**. The name is chosen to avoid a generic `backend.py`; no file of either name exists. It is the only place a backend object is constructed.

```python
ENV = "DRUTA_BACKEND"                     # "auto" | "native" | "python"
KINDS = ("auto", "native", "python")
class BackendRefused(RuntimeError): ...   # a forced "native" that cannot be honoured
class BackendMismatch(RuntimeError): ...  # a second kind asked for in one process
@dataclass(frozen=True)
class Choice: kind: str; requested: str; source: str; library: str | None; abi: int | None; reason: str | None
def consume_argv(argv: list[str]) -> list[str]   # strips --backend X / --backend=X, sets os.environ[ENV]
def open_gpu(slot=None, *, kind=None)            # -> nvbackend.GPU | native.Gpu
def enumerate_gpus() -> list[dict]               # the pinned kind's enumerate_gpus (same keys and order)
def choice() -> Choice | None
def describe() -> str                            # the one log line
```

Surface metadata (`EXEMPT`, `FRONTEND_OWNED`, `PRIVATE_REACHES`, `KNOWN_GAPS`) lives in `tests/frontend_surface.py`, not in the factory module. The judge was right that mixing the two is poor separation. A runtime `missing_members()` lives in `native_frontend.py` (b).

`GPU` stays imported from `nvbackend` in druta.py and app.py, but only as a class namespace for constants and pure static helpers (b2). I rejected a selector class whose `__new__` returns either backend: it would hide the decision inside a type and let the tests that patch `druta.GPU` stub the constructor and the constants together.

### Selection rule

The backend is decided once per process, under a module lock, at the first `open_gpu` or `enumerate_gpus`, and then **pinned**. Precedence for the request: the `kind=` argument, then `--backend` (applied through the environment), then `DRUTA_BACKEND`, then the default. There is one special case: a process started with `--startup-profile` and no explicit `--backend`/`DRUTA_BACKEND` requests `python` (Q7).

- **`python`:** `nvbackend.GPU(slot)`. `native.load()` is never called.
- **`native` (forced):** `native.load()`, then the surface gate `native_frontend.missing_members()` must be empty, then `native.Gpu(slot)`. Any failure raises `BackendRefused(reason)`. **There is no fallback**, because a forced kind that silently changes is useless for diagnosis.
- **`auto`:** choose native only when all three steps succeed:
  1. `native.load()` succeeds: library found, every export bound, `dc_abi_version() == ABI_VERSION` (native.py:693-719). `NativeUnavailable` means Python, with its text as the reason.
  2. `missing_members()` is empty. This is a static class check with no driver calls. Otherwise Python, with reason "native surface incomplete: …".
  3. `native.Gpu(slot)` constructs. On `ValueError` (from `_raise`: `dc_gpu_open` or identity), `OSError` (ctypes turns a Windows SEH fault into `OSError`), `MemoryError` or `NativeUnavailable`, use Python with reason "native open failed: <type>: <text>". **Catch exactly these.** `KeyboardInterrupt` and other `BaseException`s propagate. Unexpected `Exception` subclasses also propagate: they are bugs, not a missing capability.
- **Native opens but `available()` is False: keep native, with no Python comparison object.** (Resolving the facade/interface versus risk disagreement.) Building a second backend in a process that ends up native breaks the one-kind rule in spirit. The loader differences in §0 also mean disagreement is not automatically a defect. `status_line()` carries the core's reason, and the log line adds "start with --backend python to use the Python backend". The opt-in phase (default `python`) and the hardware run catch any systematic native loader defect.
- **After pinning,** every later `open_gpu` uses the pinned kind and its errors propagate. A later explicit different `kind=` raises `BackendMismatch`. The `swap_gpu` failure path already exists: druta.py:13311-13317 logs "cannot switch to …" and keeps the current card.
- **Decisions use only** load success, export presence, ABI equality, the surface gate, and construction and status results. They never use the driver version, device ID, subsystem or VBIOS (AGENTS.md).
- **Default at landing: `python`.** It flips to `auto` in the last increment, after the owner's hardware sign-off (Q2).
- An unknown value raises `ValueError("DRUTA_BACKEND must be auto, native or python")`. The CLI reports it and exits with code 2.

### Overrides, both ways

- **Environment variable `DRUTA_BACKEND`** is the primary mechanism. `DRUTA_NATIVE_LIB` (native.py:663-668) keeps choosing *which* library; `DRUTA_BACKEND` chooses *whether* to use it. Children started through `subprocess` with `startup.application_command` inherit it:
  - second window: druta.py:~13463
  - registry helper: registry_overrides.py:1199-1214
  - snapshot child
  - device-recovery helper and reopen: devicerecovery.py:49-53, 90-93

  The sign-in scheduled task (`startup.launch_command`, startup.py:135-136) does not inherit it and gets the startup-profile rule above. Nothing is written to disk: this is process configuration, not tuning state.
- **Flag `--backend {auto,native,python}` or `--backend=…`.** Argument parsing is hand-rolled:
  - **druta.py `main`** (15128) dispatches on `argv[0]` (15134-15150) before its option loop rejects unknown arguments (15163-). So `consume_argv` runs first, right after `argv = list(...)` at 15133. It strips the flag and sets `os.environ[ENV]`, which also covers the argparse-based `registry_overrides.cli`, `snapshot_cli` and `worker_cli`. One line is added to `--help`.
  - **app.py** tests raw `_sys.argv` membership (1799-1803). It calls `consume_argv(_sys.argv)` before 1799.
  - **gpuload, timings and bench_move `__main__`** call `consume_argv(sys.argv)` before `slot_from_argv()`.

### What is logged

- `Druta.__init__` logs `describe()` right after construction. Before the UI exists, `log` only buffers into `log_lines`. Example lines:
  - `backend: native core (<path>, ABI 2; requested auto via DRUTA_BACKEND)`
  - `backend: Python (requested auto; native core not loaded: <reason>)`, logged with `ok=False`
- This sits beside the existing `backend: {status_line}` line (druta.py:14904). `status_line()` is parity-tested text and is not changed.
- `swap_gpu`'s existing "switched from" line (~13348) is unchanged; the kind is per process.
- `--version` and Help › About also print `describe()`.
- The CLI helpers write to stderr only on an explicit `native` refusal or a fallback when `--backend` was given.
- **Runtime miss log** (graft from the risk design): `native.Gpu.__getattr__` (and the companions') runs only for names not found normally.
  - If the name *is* defined on the type, a property getter raised `AttributeError` internally. It is logged as an internal error and re-raised. This fixes the noise the judge pointed out.
  - Otherwise the name is recorded once and `AttributeError` is raised as before, so `getattr(..., default)` semantics hold.
  - Names in `native_frontend.INTENTIONALLY_ABSENT` (`legacy_p0_profile` if Q4 is answered "absent", `clkdom_debug_report`, `clkdom_mapping_probe`) are not logged.
  - `gpu_backend` drains the record into the UI log as `native backend: front end requested missing member X`, which a hardware run makes visible.

### Native construction failure and the old object

- `native.Gpu.__init__` already closes its handle on an identity failure (native.py ~990-994). Any other partial object is freed by the handle box in (d) when its last reference goes.
- The factory never constructs a Python object after a native one in auto, so it has no abandon-and-close path. If one is ever added, `close()` is legal there because the object has not been published.
- **Front ends never call `close()`.** nvbackend has no `close()`. `swap_gpu` rebinds `self.gpu` (13312-13330) while `poll_loop` (580-617) and the workers (`_induce_worker` ~13821, timings 13602, i2c 4834/6246) capture `gpu` under the `_gpu_gen` design (255-262). The outgoing native handle is freed at its last reference. A lint test (f6) forbids `.close()` on `self.gpu` or `fresh`.

### Construction sites

| Site | Change |
|---|---|
| druta.py:97 | import `open_gpu` and `enumerate_gpus` from `gpu_backend`; drop `enumerate_gpus` from the nvbackend import; keep `GPU`, `is_admin`, `same_slot` and the constants |
| druta.py:242-243 | `self.gpu = open_gpu(slot)`; `self.gpu_list = enumerate_gpus()`; log `describe()` |
| druta.py:13312, ~13319/13324 | `fresh = open_gpu(slot)`; the factory's `enumerate_gpus()` |
| druta.py:15133, `--list-gpus` (~15168), `No GPU backend` (~15342) | `consume_argv` pre-pass; `--list-gpus` through the factory; `BackendRefused` around `Druta(slot)` becomes `_tell(...)` and return 1 |
| app.py:35-36, 1811 | `consume_argv(_sys.argv)`; `gpu = open_gpu(slot_from_argv())` |
| gpuload.py:486-487, bench_move.py:178-181, timings.py:1465-1466 (`__main__`) | `consume_argv(sys.argv)`; `open_gpu(slot_from_argv())`. `DRUTA_BACKEND=native python -m druta.gpuload --gpu SLOT` becomes a cheap smoke test |
| registry_overrides.py:167/176 (`snapshot_cli`), 1368-1472 (six `GPU(target.slot)` in `_execute`, helper process) | `open_gpu(target.slot)`, which follows the inherited kind (Q6). Prerequisite: the `bind_target` equality test (f3). The `gpu_factory=fresh_snapshot` default (1263) stays because it is a subprocess. |
| voltage_offsets_live.py:23, 94, 114, 163 (**app process**) | `open_gpu(slot)`, so it uses the pinned kind. This removes the only in-process mixed site (e). It keeps a fresh object per call, as today (Q9). |
| voltage_replay_worker.py:242-244 (child) | `open_gpu(target.slot)` (inherited kind) |
| `src/druta/tools/*`, nvbackend `__main__` | Unchanged. These are Python experiments that run as their own processes. A module docstring says so. |
| Six test files (§0) | patch `druta.druta.open_gpu` instead of `druta.druta.GPU` as the constructor |

---

## (b) Surface `native.Gpu` must gain

All new members go in a new mixin module, **`src/druta/native_frontend.py`** (`class FrontendSurface`), grafted from the risk design. `native.Gpu(FrontendSurface)` is a one-line edit in native.py, so the front-end compatibility surface stays out of the core binding and has its own owner. The facade's `native_compat.py` delegates go in the same module.

The gap list from the facade design's AST scan had 57 names. Three are not gaps:
- `get` is a false positive (pcie_speed_panel.py:136-140);
- `power_policy_names` is front-end-owned;
- `stock_power_policy`, `max_all_power_policies` and `read_native_power_limit_mw` come with B4.

### b1. Shims over existing native helper modules (no new ABI)

Each shim is a thin delegating method with nvbackend's descriptor kind and signature.

- **`native_rails`:**
  - `read_volt_rail_limits`: druta.py:2342, 2349, 2375, 2392, 2983, 3177, 7924
  - `read_volt_rail_state`: druta.py:2398, 2984, 8353; voltage_bios_runtime.py:54
  - `read_rail_live_mv`: druta.py:2216
  - `_volt_rail_profile`: druta.py:2343
  - `volt_rail_diagnostics`: druta.py:2088, via getattr
  - `volt_rail_limits_supported`: profiles.py:1364
  - `volt_rail_limit_fields`: druta.py:3052; profiles.py:700, 719, 1372; rail_limit_profiles.py:60, 64
  - `stock_limit_mv`: druta.py:8315; profiles.py:1385; rail_limit_profiles.py:133
  - `volt_rail_limits_mv`
  - `_read_volt_rail_deltas`: voltage_bios_runtime.py:54
  - `_refresh_volt_rail_capabilities` → `native_rails.refresh`: voltage_bios_runtime.py:106. Under native today the getattr silently skips the refresh.
- **`native_legacy_p0`:**
  - `legacy_p0_supported`: druta.py:6833, via getattr. Without it **the whole legacy P0 UI vanishes**.
  - `legacy_p0_owned`: druta.py:6139, 6846, 6867, 6878, 6982, 7173, 8238, 13287, 13744
  - `hold_legacy_p0`: druta.py:6875, 13755
  - `release_legacy_p0`: druta.py:6857, 6891, 6983
  - `verification_p0(voltage_mv=None)` and `verification_legacy_p0`, as context managers: gpuload.py:437
  - `_vf_lock_recovery`, a property with a setter over `vf_lock_recovery` and `set_vf_lock_recovery`: druta.py:6958. Its `getattr(..., {})` default would hide a pending verification recovery and pick the wrong previous lock at 6959-6960.
- **`native_vfplan`,** as staticmethods: `peak_info`, `resolve_vf_point`, `compute_deflatten` (called on the instance at app.py:1174), `compute_hard_deflatten`, `compute_ramp`, `compute_rephase`, `evaluate_curve_law`, `predict_curve`, `cascade_meta`.
- **`native_rail_limits`:**
  - classmethods where nvbackend has classmethods: `abs_limit_mv` (instance use at profiles.py:714, 1378; rail_limit_profiles.py:125), `rail_ceiling_mv`, `rail_floor_mv`, `volt_boost_headroom_mv`, `hold_headroom_ceiling_mv`, `hold_headroom_targets`
  - instance methods: `hold_headroom_bound_mv`, `hold_headroom_allowance_mv`
  - The ports have a recorded divergence: they refuse NaN and ints beyond 2^53 (README).
- **`read_power_arbitration` and `read_power_relationships`** (registry_panel.py:1134, 1050): run nvbackend's function bodies (nvbackend.py:5733-5800) over the native object, unless B5 lands first. They compose `PerfDiagnosticsReader(self)` (which already works on native), `_power_policy_table`, `_current_limit_abi/_rm` and the Python decoders. Today registry_panel's `except` shows a misleading `'Gpu' object has no attribute` text.

### b2. Class-level constants and static helpers

**Plain class attributes (Python-side identifiers):**
- `LOCK_STEP`, `VF_LOCK_STEP`, `P0_LOCK_STEP`: druta.py:8226-8230
- `ARCH_*` and `ARCH_NAMES`: druta.py:3066, 4121-4122, 11757-11762
- `EXTRA_POINTS_ABOVE_CAP`, `CLKDOM_DELTA_APPLIES`, `CLKDOM_DELTA_CLEARS_CEILING`, `LIVE_RAIL_*`
- `VOLT_LIMIT_BASE_MV = {}`, `VOLT_LIMIT_POWERON = {}`
- `CURRENT_LIMIT_GENERATION_POLICIES` (from `native_power_layouts`)
- `HOLD_HEADROOM_FIELDS`
- `volt_limits_write_enabled = False` (see b3)

**Constants the core enforces come from the core,** because a slider range must match what the setter enforces:
- `VOLT_LIMIT_MIN/MAX/XOC_MAX_MV` (rails.h:120-122)
- `RAIL_OFFSET_MAX/XOC_MAX_MV` (clkdom.h:98-99)
- `VOLT_LIMIT_FIELDS`, `MAX_ABS_DELTA_KHZ`, `HARD_FLOOR_MV`, `POWER_LIMIT_XOC_MAX_MW`
- They come from a new sized-struct entry, `dc_request_envelopes_get(dc_request_envelopes*)`. It does not duplicate `dc_rl_constants` (§0).
- `HOLD_HEADROOM_MAX_MARGIN_MV` and `HOLD_HEADROOM_FALLBACK_GRAIN_MV` come from the existing `dc_rl_constants_get` (native_rail_limits.py:148-157).
- Each is a lazy class-level descriptor (`_CoreConstant("volt_limit_max_mv")`) that loads on first access. native.py is imported even when the factory picks Python, so a plain class attribute would force `load()` at import. The descriptor serves both `native.Gpu.X` and `gpu.X`.

**Fix the getattr defaults** at profiles.py:818, 1094, 1367, 1368, 1384 and 1409 (graft from the risk design):
- Read the constant without a default (`type(gpu).VOLT_LIMIT_MIN_MV`).
- If the backend lacks it, the validator refuses ("backend does not report its voltage envelope") rather than assuming 300 mV.
- A test checks that a profile limit below 650 mV is refused on both backends.

**Front-end `GPU.X` references stay on `nvbackend.GPU` for G4.** These are pure functions and constants, both implementations are parity-tested, and nvbackend imports on every platform:
- druta.py: 2243, 2441, 2457, 2459, 2762, 2767, 2937, 2961, 3055, 3062, 8313, 8608, 8992-9944, 9598-9682, 9744, 10054, 10139, 10208, 10226, 10254, 10337, 10377, 10443
- app.py: 1143, 1650, 1665, 1778
- vfheadroom.py:31, 93

The conformance test requires each such name to exist on `native.Gpu` with an equal value. The mechanical rewrite to `type(self.gpu).X` belongs to nvbackend's retirement (Q5). Mentions in comments (druta.py:307, 1802, 1992, 7229, 8424; vfheadroom.py:3, 241) are not uses.

**`ResetStep` (G3):** native `reset_all` returns instances of **`nvbackend.ResetStep`** itself (nvbackend.py:1621), with `.name` taken from the `*_STEP` constants. druta.py:8219-8231 picks the release by name, and app.py:1280 unpacks `(ok, m)`. Moving the class to a new module (the facade's proposal) is dropped to avoid colliding with G3. If G3 introduced a different type, G4a's conformance test flags it and the G3 owner aligns it.

### b3. Attributes the front end writes

| Attribute | Writers / readers | Native | Action |
|---|---|---|---|
| `voltage_xoc_enabled`, `msvdd_write_enabled` | druta.py:3131-3132, 13206-13208 | properties | none |
| `volt_limits_write_enabled` | written at druta.py:3136, 13207; read bare at rail_limit_profiles.py:111 (an `AttributeError` before the first unlock); getattr at druta.py:6243 | missing | Class default `False`. It stays a session attribute passed per call (native_rails.py:805-807) and is never stored by the core. |
| `power_policy_names` | druta.py:2709, 2856, 2903; profiles.py:275 | plain attribute | none; `FRONTEND_OWNED` |
| `_power_policy_pins` | druta.py:2874; power_action_profiles.py:137, 176; profiles.py:1668 | strict setter | keep strict (Q8) |
| `_power_policy_mask` | power_integral_runtime.py:84 | setter | none |
| `_current_limit_transport`, `_current_limit_layout_choice` | 18 runtime sites (for example client_power_runtime.py:270-271) | setters | none |
| `_foreign_hold_headroom`, `_hold_headroom_live_trip` | druta.py:7969-7970, 7440 | present (R6) | none |
| `nvapi._up9512r_mutex` | up9512r.py:98-100 | — | the companion is one stable object per Gpu and accepts attributes (c) |

### b4. Private nvbackend members the front end reaches into

| Member | Users | Decision | Reason |
|---|---|---|---|
| `_msvdd_offset_domains_written/_seen` | profiles.py:746-747 | **New public method on both: `msvdd_offset_domains() -> (written, seen)`**; change the call site. Native: `dc_gpu_msvdd_domains_get(gpu, which, u32* out, cap, u32* count)` over the existing `ClkDom` sets. nvbackend: a one-line accessor; the private sets stay for its own `reset_all`. | Without it, native silently drops MSVDD domains from profile capture. A public name serves the eventual native UI (graft from the interface design). |
| `_read_power(d)` (only `pl_now_mw`) | druta.py:6553, via getattr | **New public method on both: `read_enforced_power_limit_mw()`**; change the call site. Native: a new `dc_pl_enforced_read` over the NVML call telemetry already makes (telemetry.cpp ~334). Python: wraps `_read_power`. | One fresh value after a write. A full `read()` sweep (the risk design's proposal) is the wrong cost and the wrong semantics. A8's `read_power_limit_mw` reads the *configured* request, not the enforced limit. |
| `_offset_range(ctype)` | tuning_profiles.py:90, 157 | **New public method on both: `clock_offset_range(ctype)`**; change the call sites. Native runs nvbackend's function body over the (c) companions in G4. | It must stay a fresh read (profile device checks). The body touches only `self.nvml` and `_read_pstates20`. |
| `_read_pstates20`, `_pstate20_clock` (static) | all_pstates_registry.py:346; bios_runtime.py:25, 32; voltage_rel_live.py:53 | **Delegate**: bind nvbackend's function objects in `native_frontend.py` (an explicit list). | Stateless NVAPI reads with validation that touch only `self.nvapi`, and not `_synchronized` in Python either. Port them with their consumers. |
| `_clkdom_get(mask)` (plus `_CLKDOM_BUF`), `_clkdom_valid_block`, `_vfp_curve_error` (static) | voltage_rel_live.py:61-62, 87 | **Delegate**, same mechanism | A raw read (nvbackend.py:3701-3709) and two pure validators; `clkdom_layout()` already returns nvbackend's `ClkDomLayout`. |
| `legacy_p0_profile` | druta.py:6837, via getattr | **Shim returning `None`** (Q4). | `LEGACY_P0_PROFILES` is a developer-board table (nvbackend.py:4448-4468); README records it as not ported. The tooltip falls back to generic text (6838-6839). tests/test_legacy_p0_ui.py:14-32 stays a Python-backend test. |
| Already mirrored: `_lock`, `_legacy_clk_escape`, `_current_limit_abi/_rm/_transport_layouts`, `_rm_power_transport`, `_current_limit_generation_policies`, `_power_policy_table`, `_write_policy_limit`, `_policy_amount`, `power_policy_unwritable_reason` | runtimes | keep | — |
| `clkdom_debug_report`, `clkdom_mapping_probe` | no front-end use | `EXEMPT` | Experiments that write and sample (README). |

Delegation is an **explicit allowlist**. Each entry is recorded in `PRIVATE_REACHES` with the companion attributes it reads, and each has a parity test. A catch-all `__getattr__` forward to nvbackend is rejected: it would run Python driver code, some of it writing, against native state it does not have.

---

## (c) `gpu.nvapi` and `gpu.nvml` under the native backend

**Objects.** These are views over the **core's own loader**, as in the facade and interface designs. The risk design's `nvbackend.NvAPI` subclass is rejected: it re-enumerates through `WinDLL("nvapi64.dll")` (wrong on 32-bit), bypasses `dc_set_export_resolver` (two fake mechanisms in tests), and creates the wrong-target hazard it then has to police.

The new module **`src/druta/native_companions.py`** defines `BoundNvAPI` and `BoundNvml`. Each is created once per `native.Gpu` and holds a strong reference to the handle box (d), not to the Gpu. Each is a superset of today's `_Half` and accepts arbitrary attributes.

**`BoundNvAPI`:**
- `ok`, `err_detail`, `selected` (`slot`, `devid`, `subsys`, `handle`) and `gpu` come from the core's identity record.
- `_i(id, *argtypes)` returns `CFUNCTYPE(c_int, *argtypes)(qi(id))`, or `None` when the interface is absent. This is the shape of nvbackend.py:286-290. `qi` is the core-loaded `nvapi_QueryInterface` (below).
- `ver` is `NvAPI.ver`.
- The named members (`Pstates20Get/Set`, `VfpCurve`, `BoostTableGet/Set`, `VoltCtrlGet/Set`, `VoltRailsCtlGet`, `ClkDomCtlGet/Set`, `AllClocks`, `GetArchInfo`, `ForcePstate`, `GetPCIIds`, …) resolve lazily and are cached. They come from **one shared table**, hoisted from `NvAPI.__init__` (nvbackend.py:142-212) into a module-level `NVAPI_INTERFACES = ((name, id, argtypes), ...)` with no behaviour change.
- A table name that is not present is `None`, as in `NvAPI`. A name not in the table raises `AttributeError`, so the `getattr(api, "X", None)` probes behave as today: tuning_profiles.py:88-117, voltage_rel_live.py:56-77, up9512r.py:120, power_runtime.py:66, voltage_ceiling_runtime.py:70, rated_clock_runtime.py:83, factory_clock_runtime.py:70, voltage_rail_control_runtime.py:108, clock_bios_runtime.py:244.

**`BoundNvml`:**
- `ok`, `err_detail`, `selected` and `dll_path`.
- `dev` comes from the core's selected NVML device.
- `has(name)` reports whether the core resolver serves the export.
- `dll.<name>` returns one cached function object per name, with CDLL semantics (`restype` int, settable `argtypes`). nvbackend loads NVML with `ctypes.CDLL` (nvbackend.py:1473), so this is cdecl, as `CFUNCTYPE`. Uses: bios_runtime.py:119-121, profiles.py:135.
- `ver` and `errstr`.

**New ABI** (resolving facade's three entries versus interface's generic one; the generic one is chosen):

```c
DC_API dc_status DC_CALL dc_export_address(const char* library, const char* name, void** out); /* "nvapi" | "nvml"; same loaded module and test resolver; DC_E_UNRECOGNIZED when absent */
DC_API dc_status DC_CALL dc_gpu_nvml_device(const dc_gpu* gpu, void** out);                    /* the paired NVML device */
```

`_i` uses `dc_export_address("nvapi", "nvapi_QueryInterface")` once and calls it per id. Because both views go through the core's loader and `dc_set_export_resolver`, the parity fakes serve them with no extra patching, and the module instance (and therefore every NvAPI handle) is the core's.

**Pairing.** Pairing holds by construction. `nvapi.gpu` is the core's `identity.nvapi_handle`, the one `_pair` validated, and `nvml.dev` is the core's device. When the core disabled a half after a pairing failure, the view reports `ok=False` with the same `err_detail`. railctl.py:380, icx.py:172-207, nct3933.py:94-123, up9512r.py:137-143 and mp2888.py:128-136 already fail closed on that. `selected` lacks Python's `enum_index` and `name`; no front end reads them, and conformance pins the read set.

**After `close()`:** `ok=False`, `gpu=None`, `_i` returns `None`, and `dll.X` raises.

**Serialization.**
- Companion calls get **no automatic locking**, which matches nvbackend exactly.
- The `with gpu._lock` sites become serialized against **every** core call once the single lock (d) lands. Today they are not serialized against the core at all, so this closes H2 for them.
- railctl and the controllers stay unserialized, as under Python today. The risk design's per-call wrapping is rejected for G4 for two reasons:
  - It changes UI and poll timing, because I2C transactions wait.
  - It creates unanalysed lock-order pairs with `nct3933._transaction_lock`, `_up9512r_mutex` and `_ADAPTER_LOCK_GUARD`.
- A rail SET whose escape hook sees a foreign driver call aborts, which is fail-safe (DESIGN-TRANSPORT 3.8 rule 4).
- Q10 asks the owner whether to serialize I2C later.

**Python hook never reached.** Under native, nothing calls nvbackend's `_capture_current_limit_transport`, `_capture_legacy_clk_transport`, `_write_rail_records` or `GPU._RAIL_HOOK_LOCK`. A test makes them raise under the native kind (graft from the risk design).

---

## (d) Locking

### Recommendation: one lock, the handle mutex exported

**Core (ABI 2):**

```c
DC_API dc_status DC_CALL dc_gpu_lock(dc_gpu* gpu);                         /* blocks; recursive per OS thread */
DC_API dc_status DC_CALL dc_gpu_trylock(dc_gpu* gpu, int32_t* acquired);
DC_API dc_status DC_CALL dc_gpu_unlock(dc_gpu* gpu);                       /* DC_E_STATE if the caller is not the owner */
```

- `druta::Mutex` (native/src/core/mutex.h) gains owner-thread and depth tracking (graft from the risk design): `GetCurrentThreadId` or `std::this_thread::get_id`. An unlock by a non-owner then returns `DC_E_STATE` at the C ABI instead of reaching `LeaveCriticalSection` undefined behaviour, which also protects a future native UI.
- `TryEnterCriticalSection` exists on XP.
- `dc_gpu_open` gains a try/catch returning `DC_E_FAILED`.
- `DC_ABI_VERSION` and `native.ABI_VERSION` go to 2 in the same change.

**Binding:**
- `native.Gpu._lock` becomes a `_HandleLock` with `__enter__`, `__exit__`, `acquire(blocking=True, timeout=-1)`, `release()` and `_is_owned()`.
- `blocking=False` maps to trylock. A timeout other than -1 raises `ValueError`; no caller uses one.
- Owner and depth are also tracked in Python, so a non-owner `release()` raises `RuntimeError` exactly like `RLock`.
- The library stays a `ctypes.CDLL`, so the GIL is released while `dc_gpu_lock` blocks. Never bind these exports through `PyDLL`; a test asserts the loader type.

### Evaluation against the alternatives

| Question | A: one lock (chosen) | B: Python RLock wrapped around every binding call | C: two locks plus an order rule |
|---|---|---|---|
| Runtimes' direct NvAPI and escape calls under `with gpu._lock` versus core calls and the poll | serialized | serialized only if every one of several hundred `load().dc_*` sites is wrapped; one miss reopens the B3 window | not serialized (today's gap) |
| Recursion across separate ABI calls | `with gpu._lock` takes depth 1; each entry's guard takes depth 2 and returns to 1. A Python thread is one OS thread. | RLock | — |
| Callbacks under the mutex: derived-clamp confirm (native.py ~2395-2415), policy inputs (~2743-2775, calls `read_power_limit_mw`), hold-headroom `before_write` (druta.py:7435 → `write_ahead_marker` 7814, file I/O), test seams | run on the holding thread, so taking `gpu._lock` inside them is recursion; **the lock-order rule disappears** | re-enters the RLock | must never wait for `Gpu._lock` (the current rule, native.py ~1012-1018) |
| Message-fetch race (B3) | closed for every caller, a native UI included | closed only in the binding | closed only by `with self._lock` |
| Core waits that drop the handle lock (`LegacyP0::hold` 100 ms waits; `dc_perf_read_group`, `dc_clock_bios_read` per attempt) | kept; an outer `with gpu._lock` holds throughout, as nvbackend's RLock does | held for the whole call | kept |
| Future native UI | the same `dc_gpu_lock` | throwaway | — |

**Legacy P0 parity note** (correcting the interface design and the README):
- Python's `_synchronized` `hold_legacy_p0` holds the RLock across `time.sleep(0.1)` (nvbackend.py:9135, ~4513). The core drops the handle mutex across those waits.
- So under native the poll can run `read()` between the P0 request and its verification, where Python blocked it. That is more responsive. But any telemetry read during a hold would come from the transitional state Python never exposed.
- Decision: keep the core's behaviour. Add a test that a `read()` during the hold returns a valid sample and does not disturb ownership. Correct README:214.
- A caller that needs Python's exact behaviour wraps the call in `with gpu._lock`.

### GIL and deadlock rules

- A waiter in `dc_gpu_lock` holds no GIL. The holder runs Python, and core callbacks reacquire the GIL through ctypes. Only ctypes reaches the mutex, so the GIL and the mutex cannot deadlock.
- The remaining deadlock class is the same one nvbackend has: holding `gpu._lock` while waiting on another Python lock or on a thread that needs `gpu._lock`. Checked:
  - `poll_loop` calls `read()` outside `Druta._lock` (druta.py:586);
  - the `swap_gpu` `with self._lock` blocks make no GPU calls (13321-13324, 13336-13338);
  - `log` takes no lock (487);
  - `native._lock` (native.py:651) is taken only inside `load()`.
- Rules written into `native.py` and `gpu_backend.py`:
  - never hold one GPU's lock while calling another GPU object;
  - callbacks never wait on another thread;
  - the rail hook mutex is always taken after the handle mutex (hook.cpp:161, hook.h:82), and Python never takes it.

### Threads the app runs

| Thread | Where | Backend use |
|---|---|---|
| UI | DearPyGui | all writes, `with self.gpu._lock` (2339, 10545) |
| `Druta-poll` | druta.py:14902; loop 580-617 | `read()`, `retry_clock_capabilities` |
| `i2c-discovery` | 4834 | `gpu.nvapi` I2C, no lock |
| I2C verify (non-daemon) | 6246 | `verification_p0`, `read()`, rail I2C |
| `Druta-iCX-preset` | 5285 | I2C |
| `Druta-timing-profile` | 12752 | timings |
| timings worker | 13602 | `timings.snapshot(gpu)` |
| induce worker / `Druta-read-p0` | 13733 | `verification_p0` (takes `gpu._lock`), `read()` |
| `Druta-gpuload` | gpuload.py:170 | CUDA only |
| Tk poller | app.py:648 | `read()` |

No GPU use: `Druta-filedlg` (13573), `Druta-shutdown` (startup.py:342), device restart (devicereset.py:537).

### close() while locked, and lifetime

The facade's handle box is chosen over the interface design's direct close, which had a use-after-free window.

- A `_HandleBox` owns the `dc_gpu*`. `dc_gpu_close` runs in the box finalizer.
- `Gpu`, `_HandleLock` and both companions reference the box. The lock references the box, not the Gpu, so there is no Gpu↔lock cycle.
- `_Static._gpu` becomes a `weakref` (native.py:946-948), breaking the existing cycle (graft from the risk design).
- `Gpu._handle` becomes a property that raises `NativeClosed(ValueError)` once the box is marked closed.
- `close()`:
  1. raises `RuntimeError` if the calling thread holds the lock;
  2. takes the lock, waiting out current holders;
  3. marks the box closed and detaches the companions;
  4. releases.
- The handle is freed only when the last reference goes. A thread inside a method, or holding or waiting on the lock, keeps the box alive, so the mutex is never deleted while held and no entry uses a freed handle. This changes `close()` from "free now" to "invalidate now, free at last reference" (Q9).
- **Residual risk, documented:** a `KeyboardInterrupt` between `dc_gpu_lock` returning and `__enter__` completing leaks one depth level. This affects console Ctrl+C only. Also, the blocking acquire cannot be interrupted by Ctrl+C.

### What the binding's existing `with self._lock` uses become

- `_cp_set_request` (~1441), `_policy_outcome` (~2849), `set_power_limit_mw` (~3073) and `native_legacy_p0.py` 203/216/230/270 keep their code. They now hold the handle mutex itself across each call and its fetch.
- Rewrite the comments at native.py ~1012-1018, ~1254-1259, ~1409-1410, ~2750, ~3011 and ~3049 ("takes no self._lock", "must never wait").
- Amend the B3 note in DESIGN-TRANSPORT §3.9.
- The facade's "owner-approved" DC_E_CALLER change for `dc_cl_confirm_fn` is **dropped**: nothing in the repository records that approval. It is listed under Q12.

---

## (e) Mixed backends in one process

| Site | Process | Matters? |
|---|---|---|
| voltage_offsets_live.py:94, 114, 163 (from voltage_offsets_panel.py:78, 153, 174, 203; profiles.py:509, 541, 1259; voltage_offset_profiles.py:68-69) | **app** | **Yes.** Its `PerfDiagnosticsReader._transport` (perf_diagnostics.py:263-265) can install nvbackend's capture hook under `GPU._RAIL_HOOK_LOCK` (nvbackend.py:4817, 5042, 6913/6923). The core patches the same `D3DKMTEscape` prologue under its own `hook_mutex()` (hook.cpp:161). Two unrelated mutexes mean a torn prologue: a crash or a misrouted escape. |
| registry_overrides `snapshot_cli` and `_execute` (167/176, 1368-1472) | helper / snapshot child | No in-process mix. The child's `bind_target` (105-145) must equal the parent's, which is bound on the UI's object (registry_panel.py:1318). |
| voltage_replay_worker.py:244 | worker child | same as above |
| gpuload, timings, bench_move `__main__` | own process | no |
| `enumerate_gpus` (druta.py:243, 13319/13324, ~15168) | app | low (read-only, no hook); routed through the factory anyway |
| `src/druta/tools/*` | own process | exempt |

**Rule.**
- Every backend object in a Druta process is built by `gpu_backend.open_gpu`, and the kind is pinned per process, so no process holds objects of both kinds.
- Two objects of the same kind for one card behave as today: separate card locks, with hooks serialized by that kind's process-wide hook mutex.
- Separate processes may run different kinds on one card, as two Python windows can today.
- Enforcement:
  - a static lint test: no `GPU(`, `nvbackend.GPU(` or `native.Gpu(` outside `gpu_backend.py`, `nvbackend.py`, `native*.py`, `tools/` and `tests/`;
  - the runtime pin with `BackendMismatch`;
  - the hook-never-reached test (c).

---

## (f) Test strategy

1. **Shared fakes, `tests/fake_driver.py`.** Promote `FakeNvapi`, `FakeNvml` and `install` (test_native_identity.py:26-160, and the WinDLL / `_load_nvml_library` patch at 297-317) and `Rig` (test_native_telemetry.py:41-316). `card(kind)` installs one fake driver for both paths:
   - `native.set_export_resolver` for the core and its companions;
   - patched `ctypes.WinDLL` and `nvbackend._load_nvml_library` for Python.

   It returns `gpu_backend.open_gpu(slot, kind=kind)`. Native is skipped when the library is not built, as test_native_identity.py:11 does. Existing parity tests migrate gradually.

2. **Surface conformance, `tests/test_backend_conformance.py`, scanner in `tests/frontend_surface.py`.** It fails when any of these holds:
   - (a) A public `nvbackend.GPU` member (class body plus `self.X =` in `__init__`) is missing on `native.Gpu` and not in `EXEMPT` with a reason. The test also fails if an exempt name has vanished from nvbackend or appeared on native. **So it turns red as soon as nvbackend gains a public member.**
   - (b) A name from the AST front-end scan is absent on a `native.Gpu` built over the fake card and not `FRONTEND_OWNED`. The scan covers loads, stores and deletes on `gpu`, `*_gpu`, `self.gpu`, `app.gpu`, `fresh` and `g`; `getattr`/`hasattr`/`setattr` literals; `GPU.X`; and `type(gpu).X`, over `src/druta/**` excluding the backends and `tools/`. Stored names must round-trip through `setattr`.
   - (c) A name used on `nvapi`/`api`/`nvml`/`nv` expressions is absent on the companions, or the companion interface table differs from nvbackend's by id and argtypes.
   - (d) Descriptor kind or `inspect.signature` differs for shared callables. This needs `functools.wraps` in `_synchronized` (nvbackend.py:1609).
   - (e) A constant value differs, including the core envelope.
   - (f) A `PRIVATE_REACHES` delegate reads a companion attribute that is missing.
   - (g) `native_frontend.missing_members()` disagrees with the test's own computation.
   - (h) `_lock.acquire/release/locked` is used on GPU objects outside the backends.

   During the increments a **`KNOWN_GAPS` ratchet** applies: each listed gap must still exist, so closing one forces its removal. The ratchet must be empty by G4h.

3. **Differential front-end suite, `tests/frontend_rig.py` and `tests/test_frontend_backends.py`.** It is parametrized over `{python, native}` on one fake card. These flows must give equal observable results (log lines, `(ok, message)` pairs, captured profile dicts and the fake's driver-call log):
   - `Druta(slot)` construction and the backend line, using the fake-DPG pattern of test_arch_ui_regressions.py:15-34
   - one poll tick
   - the power apply, including `read_enforced_power_limit_mw`
   - `refresh_volt_limits` and the `write_rail_limit_action` reset (2337-2345)
   - `write_tuning_action('power')` (10543)
   - `reset_all` and release by step name
   - `sync_hold_headroom` with a `before_write` refusal
   - legacy P0 hold and release on a Kepler fake, including UI presence and a `read()` during the hold
   - `swap_gpu` success and failure after the pin
   - `profiles.capture` (with MSVDD domains written this session), `tuning_profiles` device checks (`clock_offset_range`), `power_action_profiles` restore, `rail_limit_profiles` preflight, and a 300 mV refusal
   - `voltage_rel_live._clocks`
   - `bios_runtime.read_thermal_thresholds`
   - `registry_overrides.read_vbios` (fake 0xFC13EE11)
   - **`bind_target(native) == bind_target(python)`**
   - `railctl.Rail.read` over a fake I2C export
   - `registry_panel` arbitration and relationships

   Partial and transient cases beyond the local hardware: NVAPI absent, NVML absent, a rail getter transient on the first read (the control is retained), a companion export missing, a pairing error, and alternate identities (two cards).

   A recording proxy logs names probed with a getattr default and found missing; the sets must be equal across kinds. The ~40 existing `Druta` UI suites use `SimpleNamespace`, `Mock` or `GPU.__new__` objects (for example test_gpu_switch.py:15, test_offset_precision.py:40-50). They test UI logic, not the backend, and stay as they are. Where a module suite builds a real backend from fakes, it gains a `both_backends` fixture.

4. **Lock tests, `tests/test_native_lock.py`** (real `libdrutacore.so`, Linux `std::recursive_mutex`):
   - A holds `with gpu._lock` while B's `read()` blocks, and a third thread still runs Python, which proves the GIL is released;
   - recursion across separate ABI calls;
   - a confirm or `before_write` callback that takes `gpu._lock`;
   - a non-owner `release` raises `RuntimeError`, and `dc_gpu_unlock` by a non-owner returns `DC_E_STATE`;
   - trylock;
   - `close()` inside `with` raises;
   - `close()` while another thread holds the lock defers the free (weakref on the box);
   - a dropped Gpu is freed promptly (the `_Static` weakref);
   - call and fetch never interleave under contention;
   - the loader type is `CDLL`.
   - The Windows CRITICAL_SECTION build runs under Wine where present; otherwise it is covered by the hardware soak.

5. **Factory tests, `tests/test_gpu_backend.py`:**
   - the mode × condition matrix (library absent, ABI mismatch, missing export, surface incomplete, open raises `ValueError`, `OSError` or other, open but unavailable);
   - forced `native` never falls back;
   - pinning and `BackendMismatch`;
   - `KeyboardInterrupt` propagates;
   - every `consume_argv` form, stripped before dispatch;
   - children inherit `DRUTA_BACKEND` through `application_command`;
   - the `--startup-profile` rule;
   - the `describe()` text;
   - the miss-log allowlist and the internal-`AttributeError` case;
   - the hook-never-reached check under native.

6. **Lint, `tests/test_backend_constructions.py`:**
   - no constructions outside the factory (tests excluded);
   - no `.close()` on `self.gpu` or `fresh`;
   - no `PyDLL` for the lock exports.

7. **Real hardware.** No GPU exists in the build environment. The owner runs this, or the root agent does on the owner's machine, with one agent writing to a GPU at a time.
   1. `Druta --version` under each kind.
   2. A new read-only **`Druta --backend-compare SLOT OUTDIR`**. It is a druta subcommand so it works in the frozen EXE. It runs the read-only capture set in two child processes (`python` and `native`), so there is no mixing, and writes both JSON files and a diff: identity, `static`, one tick, the V/F curve, rail limits and diagnostics, current limits, policies, clock-domain rows, the companion `_read_pstates20`, and `bind_target`.
   3. The app with `--backend native`, through each write with readback and restore: offsets, power limit, fan, V/F apply and reset, rail limits with opt-in, hold headroom, current limit and policy, MSVDD, profile save and explicit load, Reset all, card switch, second window, `--restart-gpu` relaunch, the voltage-offsets panel, read-only I2C discovery, the registry panel, and legacy P0 on Kepler or Maxwell if available.
   4. A concurrency soak of several minutes: poll, I2C discovery and profile load together.
   5. A clean miss log.

   Results are recorded per device, driver and operating point, as evidence for that configuration only. **No gate or constant may be derived from them.**

---

## (g) Increments

**Ownership across all of G4** (graft from the interface design):
- one owner (O-native) for `src/druta/native.py`, `native/include/drutacore/drutacore.h`, `native/src/core/abi*.cpp` and `mutex.h`;
- one owner (O-nvb) for `src/druta/nvbackend.py`;
- one owner (O-ui) for `src/druta/druta.py`.

Increments that touch those files are serialized on their owner. All delegated executors use the `worker` agent definition, as AGENTS.md requires. The root reviews every diff.

**G4a: conformance baseline**
- **Goal:** measure the gap set on that day's HEAD and freeze it as a failing-on-change ratchet.
- **Files:** new `tests/frontend_surface.py`, `tests/test_backend_conformance.py` (worker W1); `nvbackend.py`: `functools.wraps` in `_synchronized` only (O-nvb).
- **Must not touch:** native files, front ends.
- **Depends on:** A7, A8, B4, G3 landed.
- **Acceptance:** passes with `KNOWN_GAPS` equal to the measured real gaps. The scanner finds every site cited in (b). Adding a dummy public method to nvbackend turns it red. The ResetStep type check is present.
- **Tests:** the conformance checks (a)–(h).
- **Delegable:** yes.

**G4b: factory (default `python`)**
- **Goal:** `gpu_backend.py` with the selection, pin, overrides, logging and startup-profile rule.
- **Files:** new `src/druta/gpu_backend.py`, `tests/test_gpu_backend.py` (W2).
- **Must not touch:** front ends, native.py.
- **Depends on:** none; runs in parallel with G4c.
- **Acceptance:** the f5 matrix is green. With `python` (the default), `native.load()` is never called. `missing_members` is consumed through an injectable hook until G4e provides it.
- **Tests:** f5.
- **Delegable:** yes.

**G4c: one lock**
- **Goal:** export the handle mutex, add owner and depth tracking in the core, and make `Gpu._lock` a `_HandleLock` over a `_HandleBox`.
- **Files** (O-native):
  - `drutacore.h`: `dc_gpu_lock/trylock/unlock`, ABI 2
  - `core/mutex.h`: owner and depth
  - `core/abi.cpp`: entries, `dc_gpu_open` try/catch
  - `native.py`: `ABI_VERSION`, `_HandleBox`, `_HandleLock`, `_handle` property, `NativeClosed`, `close()`, the `_Static` weakref, rewritten comments
  - new `tests/test_native_lock.py`
  - `native/README.md` (correct line 214) and `DESIGN-TRANSPORT.md` §3.8/§3.9
- **Must not touch:** nvbackend, front ends.
- **Depends on:** G4a; native.py quiescent.
- **Acceptance:** f4 green; every `tests/test_native_*` green; ASan build clean; no `threading.RLock` left in `native.Gpu`.
- **Tests:** f4.
- **Delegable:** implementation yes; the root reviews concurrency, with an adversarial review pass.

**G4d: companions**
- **Goal:** `gpu.nvapi` and `gpu.nvml` as paired views over the core loader.
- **Files:**
  - O-native: `drutacore.h`, `abi.cpp` (`dc_export_address`, `dc_gpu_nvml_device`), `native.py` (wiring)
  - O-nvb: `nvbackend.py` (hoist `NVAPI_INTERFACES`, no behaviour change)
  - W3: new `src/druta/native_companions.py`, `tests/test_native_companions.py`
- **Must not touch:** railctl, the controllers, the runtimes.
- **Depends on:** G4c.
- **Acceptance:**
  - the views resolve the rig's fakes;
  - `_i` presence matches `nvapi_interface_present`;
  - the tables are equal;
  - `ok=False` after a pairing failure;
  - behaviour after close;
  - `_up9512r_mutex` persists;
  - `nvml.dll.X.restype` is settable;
  - railctl and up9512r run over a companion;
  - the nvbackend suites are green.
- **Tests:** companion cases, plus the hook-never-reached test.
- **Delegable:** yes; the root reviews the nvbackend refactor.

**G4e: front-end surface mixin**
- **Goal:** b1 shims, b2 constants (core descriptors), the `volt_limits_write_enabled` default, `_vf_lock_recovery`, the b4 delegates, `legacy_p0_profile → None`, the runtime miss log and `missing_members()`.
- **Files:**
  - W4: new `src/druta/native_frontend.py`, `tests/test_native_frontend.py`
  - O-native: the one-line base class in `native.py`, `__getattr__` miss hook; `core/abi_rails.cpp`, `drutacore.h` (`dc_request_envelopes_get`)
- **Must not touch:** front ends, nvbackend.
- **Depends on:** G4d.
- **Acceptance:** each shim and delegate is parity-tested on the shared rig. Constants equal nvbackend's. `KNOWN_GAPS` shrinks to the three public-method gaps.
- **Tests:** per-member parity; envelope equality; miss-log allowlist.
- **Delegable:** yes.

**G4f: public methods on both backends**
- **Goal:** `msvdd_offset_domains()`, `read_enforced_power_limit_mw()`, `clock_offset_range(ctype)`, and their call sites; fix the getattr-default constants.
- **Files:**
  - O-native: `abi_clkdom.cpp`/`abi_power_limit.cpp`, `drutacore.h`, `native.py` or `native_frontend.py`
  - O-nvb: `nvbackend.py`
  - one front-end worker W5: `profiles.py` (746-747, 818, 1094, 1367, 1368, 1384, 1409), `tuning_profiles.py` (90, 157)
  - O-ui: `druta.py`:6553
- **Must not touch:** anything else in those files.
- **Depends on:** G4e.
- **Acceptance:** `KNOWN_GAPS` empty; `missing_members()` empty. On both kinds, a profile capture includes MSVDD domains written this session and a 300 mV limit is refused.
- **Tests:** both-kind unit tests for the three methods and the bounds.
- **Delegable:** split across the owners.

**G4g: construction sites**
- **Goal:** route every construction through the factory, with `--backend` parsed; default still `python`.
- **Files:**
  - O-ui: `druta.py` (97, 242-243, 13312, 13319/13324, 15133, list-gpus, 14904 log, About) and the six test files
  - W6: `app.py`, `gpuload.py`, `bench_move.py`, `timings.py`
  - W7: `registry_overrides.py`, `voltage_offsets_live.py`, `voltage_replay_worker.py`
  - W2: new `tests/test_backend_constructions.py`
- **Must not touch:** backends.
- **Depends on:** G4b.
- **Acceptance:** the full suite is green with `DRUTA_BACKEND` unset and with `=python`; the lint test passes; `--backend` is stripped before dispatch in druta and handled in app.
- **Tests:** f6; CLI parse tests.
- **Delegable:** yes, three workers.

**G4h: differential suite**
- **Goal:** prove the front-end flows equal on both kinds.
- **Files:** new `tests/fake_driver.py`, `tests/frontend_rig.py`, `tests/test_frontend_backends.py` (W8); `both_backends` fixtures, one owner per suite file.
- **Must not touch:** src.
- **Depends on:** G4c–G4g.
- **Acceptance:** every f3 flow is equal under `native` and `python`, including the partial, transient and alternate-identity cases and `bind_target` equality; the getattr-probe sets are equal.
- **Delegable:** yes.

**G4i: packaging and compare tool**
- **Goal:** ship the DLL and the hardware tooling.
- **Files:**
  - one owner: `Druta.spec` and `build.ps1` (bundle the architecture-matched `drutacore.dll`), `tests/test_packaging.py`, `MANUAL.md` (`--backend`, `DRUTA_BACKEND`)
  - O-ui: `druta.py` `--backend-compare`, `--version`
  - W2: the compare helper in `gpu_backend.py`
  - `native/README.md`: a G4 row
- **Depends on:** G4h.
- **Acceptance:** the frozen build finds the DLL through the frozen branch of `candidate_paths` (native.py:671-675) and `describe()` shows native when forced. The default is still `python`.
- **Delegable:** yes.

**G4j: hardware validation**
- **Goal:** f7 on the owner's cards.
- **Owner:** the owner, or the root on the owner's machine, one writer at a time.
- **Depends on:** G4i.
- **Acceptance:** a per-device record, scoped to that configuration; a clean miss log; a clean soak.
- **Delegable:** no.

**G4k: default flip**
- **Goal:** make `auto` the default.
- **Files:** `gpu_backend.py` (one line), release note (root).
- **Depends on:** G4j sign-off and the owner's approval (Q2, Q3).
- **Acceptance:** the factory tests are updated. The `--startup-profile` pin remains unless Q7 says otherwise. The fresh EXE is left running for review (AGENTS.md hand-back). No push to `main` without authorization.
- **Delegable:** no.

---

## (h) Risks and open questions

### Risks

- **The lock change (stall or deadlock).** Mitigated by f4, the adversarial review and the soak. Windows CRITICAL_SECTION behaviour is proven only on hardware, or under Wine where present.
- **Faulting driver exports.** Under Python, ctypes turned these into `OSError`. Under native they crash the process: the core has no SEH or vectored guard (README, rail getters). This is accepted for G4, with `--backend python` as the workaround. A guard is tracked as a separate core item.
- **Silent loss through getattr defaults.** Mitigated by the scan, the ratchet, the differential proxy, the runtime miss log and the bound fixes.
- **Behaviour differences.** The legacy P0 hold lets the poll interleave under native. `legacy_p0_profile` falls back to generic text. The planners refuse NaN.
- **Delegated Python readers** keep nvbackend's struct layouts, so nvbackend cannot retire in G4.
- **Struct layout drift** is not detected by `_bind`. Mitigated by the ABI bump policy (Q11).
- **32-bit and legacy OS** (XP, Vista, the Windows 7 branch): the `nvapi.dll` choice in the core is untested on hardware. On the legacy branches, main's factory and refusals take precedence. Any remaining difference is reported to the owner, not carried forward.
- **I2C stays unserialized** against core calls (as today), and so does voltage_offsets_live's second handle.

### Questions for the owner (each with the default that lets work proceed)

1. **Should an explicit `native` that cannot load fail hard?** Default: **yes.** Only `auto` falls back.
2. **Ship with `python` as the default and flip to `auto` only after your hardware checklist?** Default: **yes.**
3. **Flip criteria?** Default: the f7 checklist passed on at least one card of each generation you own, a clean miss log, and one release as opt-in. Untested generations run the same code, with no device gate.
4. **`legacy_p0_profile` under native: return `None` (generic tooltip)?** Default: **yes.** The developer-board table stays informational, Python only.
5. **Keep front-end `GPU.X` calls on nvbackend's pure helpers for G4, with equality enforced by test, and rewrite them to `type(self.gpu).X` when nvbackend retires?** Default: **yes.**
6. **Should the registry, snapshot and replay children follow the inherited kind?** The alternative is pinning them to `python`. Default: **follow the inherited kind.** `bind_target` equality is tested either way, and the factory's surface gate applies.
7. **Pin `--startup-profile` (the unattended sign-in apply) to `python` until native profile application has your sign-off, even after the flip?** Default: **yes.**
8. **Keep the native `_power_policy_pins` setter strict?** Default: **yes.** A refusal is reported in the existing restore path.
9. **Change `native.Gpu.close()` to "invalidate now, free at last reference"?** Default: **yes.**
10. **Should I2C rail control (railctl, controllers) be serialized under the card lock?** Default: **no in G4** (behaviour unchanged). Revisit with a lock-order analysis against the controllers' own locks.
11. **ABI policy: bump to 2 at G4c, then on every incompatible change, and add sized-struct checks where a struct lacks a `size` field?** Default: **yes.**
12. **Should a `KeyboardInterrupt` inside a native callback (`dc_cl_confirm_fn`) map to `DC_E_CALLER`?** Default: **defer** to a separate item. Not part of G4.
13. **Add `--backend-compare` as a read-only developer command, and show the backend in `--version` and Help › About?** Default: **yes.**
14. **Keep `src/druta/tools/*` on nvbackend, and accept the Python-delegated private readers for G4 with native ports scheduled afterwards?** Default: **yes.**
15. **Should `voltage_offsets_live` keep opening its own fresh handle through the factory, rather than reuse `app.gpu`?** Default: **keep** (parity). Holding the app lock across its in-process device restart would freeze the poll.

### Files referenced

- /home/user/Druta/src/druta/native.py
- /home/user/Druta/src/druta/nvbackend.py
- /home/user/Druta/src/druta/druta.py
- /home/user/Druta/src/druta/app.py
- /home/user/Druta/src/druta/profiles.py
- /home/user/Druta/src/druta/railctl.py
- /home/user/Druta/src/druta/voltage_offsets_live.py
- /home/user/Druta/src/druta/registry_overrides.py
- /home/user/Druta/src/druta/voltage_replay_worker.py
- /home/user/Druta/src/druta/startup.py
- /home/user/Druta/src/druta/native_rail_limits.py
- /home/user/Druta/native/src/core/abi.cpp
- /home/user/Druta/native/src/core/mutex.h
- /home/user/Druta/native/src/backend/clkdom.h
- /home/user/Druta/native/src/backend/exports.cpp
- /home/user/Druta/native/include/drutacore/drutacore.h
- /home/user/Druta/native/README.md
- /home/user/Druta/Druta.spec
- /home/user/Druta/build.ps1