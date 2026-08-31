"""
INDUCE a higher GPU performance state by running an ordinary workload.

INDUCE, NOT FORCE - and the difference is not pedantry, it is the failure mode:

  FORCE would mean commanding the p-state through an API. That is NOT
  AVAILABLE on this card. nvmlDeviceSetMemoryLockedClocks answers
  NVML_ERROR_NOT_SUPPORTED, and `nvidia-smi -lmc 7001,7001` fails identically
  ("Setting locked Memory clocks is not supported for GPU 0000:01:00.0") -
  two independent entry points, one answer. Memory-clock locking does exist in
  NVML and works on datacenter parts, so this is most likely a consumer-segment
  restriction rather than a Turing limit; that is not proven here and is not
  claimed. Scope: not supported on THIS card through THIS driver path.

  INDUCE means creating the conditions under which the DRIVER raises the state
  itself, then watching what it decides. That works - but the state remains the
  driver's to withdraw at any moment.

A forced state would be stable and could be read at leisure. An induced state
can drop out mid-read, which is precisely why timings.snapshot() brackets the
register read with a clock+p-state sample on each side, and why a capture that
straddled a reclock computed RC as 385 ns instead of 42.

WHAT THIS REACHES, measured on this card:
  graphics clock lock alone   memory reaches only 810
  CUDA memcpy load (below)    P-STATE 2, memory 7228, ~450 GB/s of traffic
  3D / graphics load          P-STATE 0, memory 7428
7228 is ABOVE the top clock the driver enumerates (7001) and is still P2:
7228 - 427 = 6801 and 7428 - 427 = 7001, the same memory offset on both, which
is what identifies 6801 as the P2 state and 7001 as P0.

AND FOR READING TIMINGS, P2 IS ENOUGH. Measured: the timing registers are
BIT-IDENTICAL at 7228 and 7428 - all of CONFIG0..CONFIG5 and TIMING22 - because
timings are selected per clock BAND and 50 MHz of true clock does not cross a
band boundary. So this load is a complete substitute for a game: no 3D
workload, and no lifting the compute cap with `nvidia-smi -cc 1` (which this
module does not run in any case - footguns are documented, not wired to
buttons). What is NOT interchangeable is an idle capture: 405 and 810 program
genuinely slacker values.

That identity is scoped to READING. A throughput benchmark would still have to
run in the state it claims to describe.

The load uses the CUDA DRIVER API through ctypes on nvcuda.dll, which ships
with every NVIDIA driver: no toolkit, no compiler, no PTX. A device-to-device
memcpy is pure bandwidth and needs no kernel, so there is nothing to compile.

This runs an ordinary GPU workload. It writes no register and touches no tuning
knob.
"""
import ctypes
import threading
import time

CUDA_SUCCESS = 0
MIB = 1 << 20

# Buffer sizing, from FREE VRAM rather than a fixed number: this card has 24 GB
# but the module has no business assuming it, and a hardcoded allocation is the
# one that fails on the day something else is holding memory.
VRAM_SHARE = 0.30          # of what is free, across both buffers
MIN_BUF = 64 * MIB
MAX_BUF = 1024 * MIB
MIN_FREE = 256 * MIB       # below this, do not try

# A hard ceiling on how long the GPU can be held busy, enforced INSIDE the
# loop. The UI stops the load as soon as it has its capture; this is what
# happens if the UI never gets there.
DEFAULT_MAX_SECONDS = 60.0


class LoadError(RuntimeError):
    pass


def available():
    """(ok, reason). nvcuda.dll ships with the driver, so its absence means
    something is wrong with the driver install rather than a missing toolkit."""
    try:
        ctypes.WinDLL("nvcuda.dll")
        return True, ""
    except OSError as e:
        return False, (f"nvcuda.dll could not be loaded ({e}). It ships with "
                       f"the NVIDIA driver; without it Druta cannot run a "
                       f"CUDA load. A game or a benchmark induces P0 anyway - "
                       f"and does it better than this can.")


def _bind(cu):
    """Explicit signatures. CUdeviceptr is a 64-bit integer handle, not a
    pointer to anything in this process - declaring it as c_void_p works by
    accident on this ABI and is wrong in principle."""
    P = ctypes.POINTER
    cu.cuInit.argtypes = [ctypes.c_uint]
    cu.cuDeviceGet.argtypes = [P(ctypes.c_int), ctypes.c_int]
    cu.cuDeviceGetName.argtypes = [ctypes.c_char_p, ctypes.c_int,
                                   ctypes.c_int]
    cu.cuCtxCreate_v2.argtypes = [P(ctypes.c_void_p), ctypes.c_uint,
                                  ctypes.c_int]
    cu.cuCtxDestroy_v2.argtypes = [ctypes.c_void_p]
    cu.cuCtxSynchronize.argtypes = []
    cu.cuMemGetInfo_v2.argtypes = [P(ctypes.c_size_t), P(ctypes.c_size_t)]
    cu.cuMemAlloc_v2.argtypes = [P(ctypes.c_ulonglong), ctypes.c_size_t]
    cu.cuMemFree_v2.argtypes = [ctypes.c_ulonglong]
    cu.cuMemcpyDtoD_v2.argtypes = [ctypes.c_ulonglong, ctypes.c_ulonglong,
                                   ctypes.c_size_t]
    cu.cuGetErrorName.argtypes = [ctypes.c_int, P(ctypes.c_char_p)]


def _err_name(cu, rc):
    try:
        p = ctypes.c_char_p()
        if cu.cuGetErrorName(rc, ctypes.byref(p)) == CUDA_SUCCESS and p.value:
            return p.value.decode("ascii", "replace")
    except Exception:
        pass
    return "?"


class BandwidthLoad:
    """A device-to-device memcpy loop on its own thread.

    Every CUDA call happens on that one thread on purpose: a driver-API context
    is bound to the thread that created it, so creating it here and freeing it
    from the UI thread would be a different context to the runtime.
    """

    def __init__(self, max_seconds=DEFAULT_MAX_SECONDS, device=0):
        self.max_seconds = float(max_seconds)
        self.device = int(device)
        self.started = threading.Event()   # set once copies are in flight
        self.done = threading.Event()
        self._stop = threading.Event()
        self.error = ""
        self.stats = {}
        self.device_name = ""
        self._thread = None

    # ---- lifecycle -------------------------------------------------------- #
    def start(self):
        if self._thread is not None:
            raise LoadError("this load has already been started")
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="Druta-gpuload")
        self._thread.start()

    def stop(self):
        self._stop.set()

    def join(self, timeout=None):
        if self._thread:
            self._thread.join(timeout=timeout)

    @property
    def running(self):
        return (self._thread is not None and self._thread.is_alive()
                and not self.done.is_set())

    def wait_started(self, timeout=15.0):
        """True once the loop is actually copying. Returns False on timeout or
        if the load failed on its way up - check .error."""
        self.started.wait(timeout)
        return self.started.is_set() and not self.error

    # ---- the loop --------------------------------------------------------- #
    def _run(self):
        cu = ctx = None
        a = b = None
        try:
            ok, why = available()
            if not ok:
                raise LoadError(why)
            cu = ctypes.WinDLL("nvcuda.dll")
            _bind(cu)

            def chk(rc, what):
                if rc != CUDA_SUCCESS:
                    raise LoadError(f"{what} failed: rc={rc} "
                                    f"{_err_name(cu, rc)}")

            chk(cu.cuInit(0), "cuInit")
            dev = ctypes.c_int(0)
            chk(cu.cuDeviceGet(ctypes.byref(dev), self.device), "cuDeviceGet")
            nm = ctypes.create_string_buffer(256)
            if cu.cuDeviceGetName(nm, 256, dev) == CUDA_SUCCESS:
                self.device_name = nm.value.decode("ascii", "replace")

            ctx = ctypes.c_void_p()
            chk(cu.cuCtxCreate_v2(ctypes.byref(ctx), 0, dev), "cuCtxCreate")
            # EVERYTHING below is inside try/finally down to the context
            # destroy. A leaked CUDA context keeps the card in a raised
            # p-state after the load ends - an invisible leftover exactly like
            # the clock lock this app goes out of its way to release on exit.
            try:
                free_b = ctypes.c_size_t(0)
                total_b = ctypes.c_size_t(0)
                chk(cu.cuMemGetInfo_v2(ctypes.byref(free_b),
                                       ctypes.byref(total_b)), "cuMemGetInfo")
                buf = self._buf_size(free_b.value)
                a, b = ctypes.c_ulonglong(0), ctypes.c_ulonglong(0)
                chk(cu.cuMemAlloc_v2(ctypes.byref(a), ctypes.c_size_t(buf)),
                    f"cuMemAlloc({buf >> 20} MiB)")
                try:
                    chk(cu.cuMemAlloc_v2(ctypes.byref(b),
                                         ctypes.c_size_t(buf)),
                        f"cuMemAlloc({buf >> 20} MiB)")
                    try:
                        self._hammer(cu, chk, a, b, buf, free_b.value,
                                     total_b.value)
                    finally:
                        cu.cuMemFree_v2(b)
                finally:
                    cu.cuMemFree_v2(a)
            finally:
                cu.cuCtxDestroy_v2(ctx)
        except Exception as e:
            self.error = str(e) if isinstance(e, LoadError) else \
                f"{type(e).__name__}: {e}"
        finally:
            # unblock anyone in wait_started() even when the load never got
            # off the ground - otherwise a failure here is a UI hang
            self.started.set()
            self.done.set()

    def _buf_size(self, free_bytes):
        if free_bytes < MIN_FREE:
            raise LoadError(
                f"only {free_bytes // MIB} MiB of VRAM free - not enough to "
                f"run a bandwidth load without competing with whatever is "
                f"already using the card")
        buf = int(free_bytes * VRAM_SHARE) // 2       # two buffers
        buf = max(MIN_BUF, min(MAX_BUF, buf))
        return (buf // MIB) * MIB

    def _hammer(self, cu, chk, a, b, buf, free_b, total_b):
        deadline = time.perf_counter() + self.max_seconds
        t0 = time.perf_counter()
        copies = 0
        self.started.set()
        while not self._stop.is_set() and time.perf_counter() < deadline:
            for _ in range(8):
                # checked every copy: an unchecked failing memcpy would spin
                # this thread hot for the whole duration and induce nothing
                chk(cu.cuMemcpyDtoD_v2(a, b, ctypes.c_size_t(buf)),
                    "cuMemcpyDtoD")
                copies += 1
            chk(cu.cuCtxSynchronize(), "cuCtxSynchronize")
        el = max(1e-6, time.perf_counter() - t0)
        moved = copies * buf
        self.stats = {
            "copies": copies, "bytes": moved, "seconds": el,
            "buf_mib": buf // MIB,
            "free_mib": free_b // MIB, "total_mib": total_b // MIB,
            # each copy reads one buffer and writes another, so the traffic the
            # memory controller sees is twice the bytes moved
            "gbps_traffic": 2.0 * moved / 1e9 / el,
            "hit_deadline": time.perf_counter() >= deadline,
        }


def induce(gpu, settle_timeout=15.0, max_seconds=DEFAULT_MAX_SECONDS,
           on_settled=None, poll=0.4):
    """Run a load, wait for the memory clock to settle, call `on_settled()`
    WHILE THE LOAD IS STILL RUNNING, then stop it.

    The callback is where the caller takes its capture. That ordering is the
    entire point: a capture taken after the load stops is a capture of the card
    coming back down.

    Returns a dict: reached/pstate/mem/samples/stats/error.
    """
    out = {"error": "", "mem": None, "pstate": None, "samples": [],
           "stats": {}, "settled": False, "result": None}
    load = BandwidthLoad(max_seconds=max_seconds)
    try:
        load.start()
        if not load.wait_started(timeout=settle_timeout):
            out["error"] = load.error or "the GPU load never started"
            return out
        # let the driver notice the work and settle somewhere
        last, stable, t0 = None, 0, time.perf_counter()
        while time.perf_counter() - t0 < settle_timeout:
            if load.done.is_set() and load.error:
                out["error"] = load.error
                return out
            mem, ps = _read(gpu)
            out["samples"].append((round(time.perf_counter() - t0, 2), mem, ps))
            if mem is not None and mem == last:
                stable += 1
                # two consecutive identical reads: the driver has picked a
                # state rather than being on its way to one
                if stable >= 2:
                    out["settled"] = True
                    break
            else:
                stable = 0
            last = mem
            time.sleep(poll)
        out["mem"], out["pstate"] = _read(gpu)
        if on_settled is not None:
            out["result"] = on_settled()
    finally:
        # stop and JOIN before returning: the caller is entitled to assume the
        # card is back to its own devices once this returns
        load.stop()
        load.join(timeout=10.0)
        out["stats"] = load.stats
        if load.error and not out["error"]:
            out["error"] = load.error
    return out


def _read(gpu):
    try:
        d = gpu.read()
        return d.get("mem"), d.get("pstate")
    except Exception:
        return None, None


# ---------------------------------------------------------------------------- #
#  standalone:  python gpuload.py
# ---------------------------------------------------------------------------- #
if __name__ == "__main__":
    import sys
    ok, why = available()
    print("nvcuda :", "ok" if ok else why)
    if not ok:
        raise SystemExit(1)
    try:
        from nvbackend import GPU, slot_from_argv
        g = GPU(slot_from_argv())
    except Exception as e:
        print("no GPU backend:", e)
        raise SystemExit(1)
    print("states :", g.static.get("mem_clocks"))
    print("idle   :", _read(g))
    seen = []
    r = induce(g, on_settled=lambda: seen.append(_read(g)) or _read(g),
               max_seconds=25.0)
    print("\nsamples (t, mem, pstate):")
    for s in r["samples"]:
        print("  ", s)
    print("\nsettled:", r["settled"], " during load:", r["result"])
    print("stats  :", r["stats"])
    if r["error"]:
        print("error  :", r["error"])
    top = (g.static.get("mem_clocks") or [None])[-1]
    mem, ps = r["result"] or (None, None)
    print(f"\nreached mem={mem} pstate={ps}  (top enumerated {top})")
    print("VERDICT:", "P0" if ps == 0 else
          f"P{ps} - not P0" + (" (compute P2 cap)" if ps == 2 else ""))
    sys.stdout.flush()
