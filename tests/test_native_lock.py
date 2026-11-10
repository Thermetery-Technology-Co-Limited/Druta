# Druta - the one lock per card: native.Gpu._lock over the core's handle mutex (increment G4c).
# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""native.Gpu._lock is the handle mutex every core entry takes (dc_gpu_lock / dc_gpu_trylock /
dc_gpu_unlock / dc_gpu_lock_held), and the handle lives in a _HandleBox freed at its last reference.

These run on the real built library (Linux: std::recursive_mutex behind druta::Mutex, owner recorded
as the kernel thread id). The cases are DESIGN-FRONTEND.md (f) item 4: a holder blocks another
thread's read() while a third thread runs Python (the GIL is released while a waiter blocks; run in a
child process, so a held GIL fails the case by timeout instead of hanging the suite); recursion
across separate ABI calls; a confirm callback and a before_write hook that run under the core's
mutex, take gpu._lock, and meet a second thread that holds gpu._lock around a core entry (the case
the old two-lock rule forbade); an unlock inside a core callback that must never give back the
running entry's own level (with and without a level of the caller's own); close() and release()
inside a callback; a non-owner release and a non-owner dc_gpu_unlock; a thread that exits while
holding a level; trylock; close() inside `with`; close() while another thread holds the lock; a
dropped Gpu freed promptly; a call and its message fetch never interleaved by another caller (a
direct core call and two binding callers with different texts); a read() during a legacy P0 hold;
the loader type and the ABI check before any other export is bound.

Every wait has a timeout. Every test body that takes the lock on its own thread runs on a helper
thread joined with a bound (BoundedCase), so a lock that stops being recursive, or a lock-order
deadlock, fails each case within LIMIT seconds instead of hanging the suite; a body that did not
finish leaves its Gpu unclosed (its thread still holds the box), so no cleanup waits on it either.

Mutations each case set is meant to catch (run against the clean-copy build): the core's
exported-level check dropped (a stray dc_gpu_unlock in a callback then releases the entry's level);
the owner recorded as std::this_thread::get_id on Linux (a later thread with the same pthread_t
passes the owner check); the mutex not recursive (std::mutex); Gpu._lock a private threading.RLock;
close() freeing the handle at once; the library loaded as a PyDLL; `with self._lock` removed around
set_power_limit_mw's call and fetch; the loader binding every export before it reads the ABI.

The fixtures (the derived-clamp rig, the hold-headroom rig, the NVML power-limit rig, the Kepler P0
rig) are the parity suites' own; their values are inputs to the logic, not claims about any board.
"""
import ctypes
import functools
import gc
import inspect
import os
import subprocess
import sys
import textwrap
import threading
import time
import unittest
import weakref
from pathlib import Path
from unittest import mock

from druta import native

if not native.available():
    raise unittest.SkipTest("native core not built (native/build.sh linux)")

WAIT = 10  # seconds: the bound on every wait that should succeed
BLOCKED = 0.3  # seconds a thread is given to (wrongly) get past a held lock
LIMIT = 10  # seconds: the bound on one test body (BoundedCase); a normal body takes under 1 s
_LEAKED = []  # handles deliberately never freed: a dead thread still holds their mutex


def require_cdll(case):
    """Fail at once, instead of hanging the suite, when the library is not a plain ctypes.CDLL: through
    a PyDLL a thread blocked on the handle mutex keeps the GIL, so the holder could never release it
    and no timeout in this process could fire."""
    library = native.load()
    if type(library) is not ctypes.CDLL or library.dc_gpu_lock._flags_ & ctypes._FUNCFLAG_PYTHONAPI:
        case.fail("the core is not loaded as a ctypes.CDLL: the blocking cases would hang (GIL held)")


def other_thread(fn):
    """fn() on a fresh thread, joined with a timeout: its result."""
    out = []
    thread = threading.Thread(target=lambda: out.append(fn()), daemon=True)
    thread.start()
    thread.join(WAIT)
    assert not thread.is_alive(), "the probe thread did not return"
    return out[0]


def trylock_elsewhere(gpu):
    """Whether another thread can take gpu's lock right now (it releases it at once)."""
    def probe():
        if gpu._lock.acquire(blocking=False):
            gpu._lock.release()
            return True
        return False
    return other_thread(probe)


def core_trylock_elsewhere(handle):
    """dc_gpu_trylock on another thread (released again when taken): (status, acquired)."""
    library = native.load()

    def probe():
        acquired = ctypes.c_int32(-1)
        status = library.dc_gpu_trylock(handle, ctypes.byref(acquired))
        if status == native.DC_OK and acquired.value:
            assert library.dc_gpu_unlock(handle) == native.DC_OK
        return status, acquired.value
    return other_thread(probe)


def lock_held(handle):
    held = ctypes.c_int32(-1)
    assert native.load().dc_gpu_lock_held(handle, ctypes.byref(held)) == native.DC_OK
    return held.value


class Holder:
    """A thread that holds `lock` (with) until released; entered/released are bounded."""

    def __init__(self, lock, body=None):
        self.entered, self.go, self.done = threading.Event(), threading.Event(), threading.Event()
        self.body, self.result = body, None

        def run():
            with lock:
                self.entered.set()
                self.go.wait(WAIT)
                if self.body:
                    self.result = self.body()
            self.done.set()
        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        assert self.entered.wait(WAIT), "the holder never took the lock"

    def release(self):
        self.go.set()
        self.thread.join(WAIT)
        assert not self.thread.is_alive(), "the holder did not finish"


class Intruder:
    """Thread B: `with gpu._lock:` and then a core entry inside it. Started from inside a core callback
    on thread A, it must not get in until A's entry is over; under the old two-lock design it took
    the private RLock at once and then waited for the core's mutex while A's callback waited for the
    RLock (a deadlock, which the bounded test body turns into a failure)."""

    def __init__(self, gpu):
        self.entered, self.done = threading.Event(), threading.Event()
        self.result = None

        def run():
            with gpu._lock:
                self.entered.set()
                self.result = gpu.read_vcore_mv()
            self.done.set()
        self.thread = threading.Thread(target=run, daemon=True)

    def start_and_watch(self):
        """Start B; whether it got the lock within BLOCKED (it must not while the caller's entry runs)."""
        self.thread.start()
        return self.entered.wait(BLOCKED)

    def finish(self):
        self.thread.join(WAIT)
        return not self.thread.is_alive() and self.done.is_set()


class BoundedCase(unittest.TestCase):
    """Each test_* method runs on a helper thread joined with LIMIT seconds (see the module docstring).
    A body that does not finish marks the case stuck: cleanups registered through cleanup() are then
    skipped, because they would wait for the lock the stuck thread holds."""

    LIMIT = LIMIT

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        for name, method in list(vars(cls).items()):
            if name.startswith("test") and callable(method):
                setattr(cls, name, cls._bounded(method))

    @staticmethod
    def _bounded(method):
        @functools.wraps(method)
        def run(self):
            out = {}

            def body():
                try:
                    method(self)
                except BaseException as exc:  # re-raised on the test's own thread below
                    out["exc"] = exc
            thread = threading.Thread(target=body, daemon=True, name=f"bounded-{method.__name__}")
            thread.start()
            thread.join(self.LIMIT)
            if thread.is_alive():
                self.stuck = True
                self.fail(f"the test body did not finish within {self.LIMIT} s: a lock wait that never "
                          "returns (the mutex is not recursive, or a lock-order deadlock)")
            if "exc" in out:
                raise out["exc"]
        return run

    stuck = False

    def cleanup(self, function, *args):
        """A cleanup that may take the card lock (a close): skipped when the body is stuck, and run on a
        helper thread joined with WAIT otherwise, because a body that failed while holding a level
        leaves it held by its exited thread."""
        self.addCleanup(self._bounded_cleanup, function, *args)

    def _bounded_cleanup(self, function, *args):
        if self.stuck:
            return
        thread = threading.Thread(target=function, args=args, daemon=True)
        thread.start()
        thread.join(WAIT)
        if thread.is_alive():
            self.stuck = True
            raise AssertionError(f"cleanup {getattr(function, '__qualname__', function)} waited on the card "
                                 f"lock for {WAIT} s: a level was left held")


class LockCase(BoundedCase):
    def setUp(self):
        require_cdll(self)
        self.library = native.load()
        self.gpu = native.Gpu()  # no driver here: both halves unavailable, every entry still locks
        self.cleanup(self._close)

    def _close(self):
        gpu, self.gpu = self.gpu, None
        if gpu is not None:
            gpu.close()

    def patch_library(self, name, replacement):
        """Replace a bound export on the shared CDLL for this test (the attribute ctypes caches)."""
        real = getattr(self.library, name)
        setattr(self.library, name, replacement)
        self.addCleanup(setattr, self.library, name, real)
        return real


class FakeLibrary:
    """A loaded library for native.load: dc_abi_version answers `abi`; the names in `missing` are not
    exported (getattr raises as ctypes does)."""

    def __init__(self, abi, missing=()):
        self._abi, self._missing, self.bound = abi, set(missing), []

    def __getattr__(self, name):
        if name.startswith("_") or name in self._missing:
            raise AttributeError(f"undefined symbol: {name}")
        self.bound.append(name)
        function = mock.Mock(return_value=self._abi if name == "dc_abi_version" else 0)
        object.__setattr__(self, name, function)
        return function


class LoaderTests(unittest.TestCase):
    def test_the_library_is_a_cdll_so_ctypes_releases_the_gil(self):
        library = native.load()
        self.assertIs(type(library), ctypes.CDLL)  # ctypes.PyDLL is a CDLL subclass: exact type
        for name in ("dc_gpu_lock", "dc_gpu_trylock", "dc_gpu_unlock", "dc_gpu_lock_held"):
            with self.subTest(name):
                self.assertFalse(getattr(library, name)._flags_ & ctypes._FUNCFLAG_PYTHONAPI)

    def test_abi_version_and_the_lock_entries_reject_null(self):
        library = native.load()
        self.assertEqual(library.dc_abi_version(), native.ABI_VERSION)
        self.assertEqual(native.ABI_VERSION, 2)
        acquired = ctypes.c_int32(-1)
        self.assertEqual(library.dc_gpu_lock(None), native.DC_E_ARGUMENT)
        self.assertEqual(library.dc_gpu_unlock(None), native.DC_E_ARGUMENT)
        self.assertEqual(library.dc_gpu_trylock(None, ctypes.byref(acquired)), native.DC_E_ARGUMENT)
        self.assertEqual(library.dc_gpu_lock_held(None, ctypes.byref(acquired)), native.DC_E_ARGUMENT)
        self.assertEqual(acquired.value, -1)
        gpu = native.Gpu()
        try:
            self.assertEqual(library.dc_gpu_lock_held(gpu._handle, None), native.DC_E_ARGUMENT)
        finally:
            gpu.close()

    def test_no_threading_rlock_in_native_gpu(self):
        gpu = native.Gpu()
        try:
            self.assertIsInstance(gpu._lock, native._HandleLock)
            self.assertNotIn("RLock", inspect.getsource(native.Gpu))
            self.assertEqual(native._HandleLock.__slots__, ("_box",))  # no owner or depth of its own
        finally:
            gpu.close()

    def load_from(self, library, path):
        """native.load() with its cache cleared, over one candidate path that opens as `library`."""
        with mock.patch.object(native, "_library", None), mock.patch.object(native, "_error", None), \
                mock.patch.object(native, "candidate_paths", lambda environ=None: [path]), \
                mock.patch.object(ctypes, "CDLL", lambda name, *a, **k: library):
            try:
                native.load()
            except native.NativeUnavailable as exc:
                return str(exc)
            return None

    def test_another_abi_is_refused_as_such_before_any_other_export_is_bound(self):
        path = Path(__file__)  # any existing file: CDLL is replaced
        old = FakeLibrary(abi=1, missing={"dc_gpu_lock", "dc_gpu_trylock", "dc_gpu_unlock", "dc_gpu_lock_held"})
        self.assertEqual(self.load_from(old, path),
                         f"native core not loaded: {path}: ABI 1, this build needs {native.ABI_VERSION}")
        self.assertEqual(old.bound, ["dc_abi_version"])
        missing = FakeLibrary(abi=native.ABI_VERSION, missing={"dc_gpu_lock_held"})
        self.assertEqual(self.load_from(missing, path),
                         f"native core not loaded: {path}: undefined symbol: dc_gpu_lock_held")
        self.assertEqual(self.load_from(FakeLibrary(abi=native.ABI_VERSION), path), None)

    def test_a_real_abi_1_library_is_refused_with_the_abi_text(self):
        old = os.environ.get("DRUTA_TEST_ABI1_LIB")  # a build of the core before G4c (ABI 1)
        if not old or not Path(old).is_file():
            self.skipTest("set DRUTA_TEST_ABI1_LIB to a pre-G4c libdrutacore build to run this")
        path = Path(old)
        with mock.patch.object(native, "_library", None), mock.patch.object(native, "_error", None), \
                mock.patch.object(native, "candidate_paths", lambda environ=None: [path]):
            with self.assertRaises(native.NativeUnavailable) as raised:
                native.load()
        self.assertEqual(str(raised.exception),
                         f"native core not loaded: {path}: ABI 1, this build needs {native.ABI_VERSION}")


class GilTests(unittest.TestCase):
    """In a child process: a thread blocked on the handle mutex must not hold the GIL, or nothing else
    (the holder included) could run Python and the child would never finish; the timeout fails it."""

    SCRIPT = textwrap.dedent("""
        import threading, time
        from druta import native
        gpu = native.Gpu()
        started = {name: threading.Event() for name in ("read", "vcore", "lock")}
        finished = {}

        def blocked(name, call):
            def run():
                started[name].set()
                call()
                finished[name] = time.monotonic()
            return run

        def take():
            with gpu._lock:
                pass
        waiters = [threading.Thread(target=blocked(name, call), daemon=True)
                   for name, call in (("read", gpu.read), ("vcore", gpu.read_vcore_mv), ("lock", take))]
        counter, stop = [0], threading.Event()

        def spin():
            while not stop.is_set():
                counter[0] += 1
        with gpu._lock:
            for thread in waiters:
                thread.start()
            assert all(event.wait(10) for event in started.values())
            time.sleep(0.3)  # each waiter is now inside the core, waiting for the mutex
            spinner = threading.Thread(target=spin, daemon=True)
            spinner.start()
            before = counter[0]
            time.sleep(0.3)
            spun = counter[0] - before
            stop.set()
            spinner.join(10)
            early = sorted(finished)
            released = time.monotonic()
        for thread in waiters:
            thread.join(10)
        late = all(finished.get(name, 0) >= released for name in started)
        print("early", early, "spun", spun > 0, "late", late, "alive", any(t.is_alive() for t in waiters))
        gpu.close()
    """)

    def test_a_holder_blocks_other_threads_while_a_third_runs_python(self):
        root = Path(__file__).resolve().parents[1]
        env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(root / "src"), str(root)]))
        library = getattr(native.load(), "path", None)
        if library:
            env["DRUTA_NATIVE_LIB"] = library
        try:
            child = subprocess.run([sys.executable, "-c", self.SCRIPT], capture_output=True, text=True,
                                   timeout=30, env=env, cwd=str(root))
        except subprocess.TimeoutExpired:
            self.fail("the child hung: a waiter on the handle mutex held the GIL")
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(child.stdout.strip(), "early [] spun True late True alive False")


class RecursionTests(LockCase):
    def test_recursion_across_separate_abi_calls(self):
        library, handle = self.library, self.gpu._handle
        self.assertEqual(lock_held(handle), 0)
        self.assertEqual(library.dc_gpu_lock(handle), native.DC_OK)
        self.assertEqual(library.dc_gpu_lock(handle), native.DC_OK)  # the same OS thread: a second level
        self.assertEqual(lock_held(handle), 1)
        self.assertEqual(self.gpu.read_vcore_mv(), None)  # an entry inside takes and returns a third
        self.assertEqual(core_trylock_elsewhere(handle), (native.DC_OK, 0))
        self.assertEqual(other_thread(lambda: lock_held(handle)), 0)  # held, but not by that thread
        self.assertEqual(library.dc_gpu_unlock(handle), native.DC_OK)
        self.assertEqual(core_trylock_elsewhere(handle), (native.DC_OK, 0))  # one level still held
        self.assertEqual(library.dc_gpu_unlock(handle), native.DC_OK)
        self.assertEqual(core_trylock_elsewhere(handle), (native.DC_OK, 1))
        self.assertEqual(library.dc_gpu_unlock(handle), native.DC_E_STATE)  # nothing left to release
        self.assertEqual(lock_held(handle), 0)

    def test_with_blocks_nest_and_entries_inside_add_a_level(self):
        lock = self.gpu._lock
        self.assertFalse(lock._is_owned())
        with lock:
            with lock:
                self.assertTrue(lock._is_owned())
                self.assertIn("held by this thread", repr(lock))
                self.gpu.read()
                self.assertIsNone(self.gpu.read_vcore_mv())
                self.assertFalse(trylock_elsewhere(self.gpu))
            self.assertTrue(lock._is_owned())
            self.assertFalse(trylock_elsewhere(self.gpu))
        self.assertFalse(lock._is_owned())
        self.assertIn("not held by this thread", repr(lock))
        self.assertTrue(trylock_elsewhere(self.gpu))


class OwnershipTests(LockCase):
    def test_a_non_owner_release_raises_and_the_core_refuses_it(self):
        lock, handle = self.gpu._lock, self.gpu._handle
        with self.assertRaisesRegex(RuntimeError, "un-acquired"):
            lock.release()  # nobody holds it
        self.assertEqual(self.library.dc_gpu_unlock(handle), native.DC_E_STATE)
        holder = Holder(lock)
        try:
            with self.assertRaisesRegex(RuntimeError, "un-acquired"):
                lock.release()
            self.assertEqual(self.library.dc_gpu_unlock(handle), native.DC_E_STATE)  # nothing changed:
            self.assertFalse(lock.acquire(blocking=False))  # the holder still holds it
            self.assertFalse(lock._is_owned())
        finally:
            holder.release()
        self.assertTrue(lock.acquire(blocking=False))
        lock.release()

    def test_trylock(self):
        lock = self.gpu._lock
        self.assertTrue(lock.acquire(blocking=False))
        self.assertTrue(lock.acquire(False))  # recursive: one more level
        self.assertFalse(trylock_elsewhere(self.gpu))
        lock.release()
        self.assertFalse(trylock_elsewhere(self.gpu))
        lock.release()
        self.assertTrue(trylock_elsewhere(self.gpu))
        holder = Holder(lock)
        try:
            self.assertFalse(lock.acquire(blocking=False))
            self.assertEqual(core_trylock_elsewhere(self.gpu._handle), (native.DC_OK, 0))
        finally:
            holder.release()
        for timeout in (0, 1, 0.5, None):
            with self.assertRaises(ValueError, msg=f"timeout={timeout!r}"):
                lock.acquire(timeout=timeout)
        self.assertTrue(lock.acquire(True, -1))
        lock.release()
        acquired = ctypes.c_int32(-1)
        self.assertEqual(self.library.dc_gpu_trylock(self.gpu._handle, None), native.DC_E_ARGUMENT)
        self.assertEqual(self.library.dc_gpu_trylock(self.gpu._handle, ctypes.byref(acquired)), native.DC_OK)
        self.assertEqual(acquired.value, 1)
        self.assertEqual(self.library.dc_gpu_unlock(self.gpu._handle), native.DC_OK)

    def test_a_thread_that_exits_holding_a_level_does_not_pass_its_ownership_on(self):
        # The owner is the kernel thread id (what glibc's recursive mutex records), not pthread_self,
        # which glibc hands to the next thread as soon as one exits. The mutex stays held for good
        # (as any mutex whose holder exits), so this Gpu is never closed or freed.
        if not sys.platform.startswith("linux"):
            self.skipTest("the owner identity checked here is Linux's kernel thread id")
        gpu = native.Gpu()
        _LEAKED.append(gpu)
        ctypes.pythonapi.Py_IncRef(ctypes.py_object(gpu._box))  # not freed even at interpreter exit
        handle, ids = gpu._handle, {}

        def leaker():
            ids["leaker"] = (threading.get_ident(), threading.get_native_id())
            gpu._lock.acquire()  # exits without release
        thread = threading.Thread(target=leaker)
        thread.start()
        thread.join(WAIT)
        self.assertFalse(thread.is_alive(), "lock holder did not finish")

        def later():
            ids["later"] = (threading.get_ident(), threading.get_native_id())
            try:
                gpu._lock.release()
                ids["release"] = "accepted"
            except RuntimeError as exc:
                ids["release"] = str(exc)
            ids["unlock"] = self.library.dc_gpu_unlock(handle)
            ids["owned"] = gpu._lock._is_owned()
        thread = threading.Thread(target=later)
        thread.start()
        thread.join(WAIT)
        self.assertFalse(thread.is_alive(), "later owner probe did not finish")
        if ids["later"][1] == ids["leaker"][1]:
            self.skipTest("the kernel gave the new thread the exited thread's id at once")
        self.assertEqual((ids["release"], ids["unlock"], ids["owned"]),
                         ("cannot release un-acquired lock", native.DC_E_STATE, False))
        self.assertEqual(core_trylock_elsewhere(handle), (native.DC_OK, 0))  # still the dead thread's


class LifetimeTests(LockCase):
    def watch_close(self):
        """Record every dc_gpu_close (the real one still runs): the list of handle values. A collection
        can free other tests' leftovers here too, so the cases look for their own handle's value."""
        closed = []
        real = self.library.dc_gpu_close

        def record(handle):
            closed.append(handle.value if isinstance(handle, ctypes.c_void_p) else handle)
            return real(handle)
        self.patch_library("dc_gpu_close", record)
        return closed

    def test_close_inside_with_raises_and_leaves_the_handle_usable(self):
        with self.gpu._lock:
            with self.assertRaisesRegex(RuntimeError, "holds the card's lock"):
                self.gpu.close()
            self.assertIsNone(self.gpu.read_vcore_mv())
        self.gpu.close()
        with self.assertRaisesRegex(native.NativeClosed, "^invalid argument$"):
            self.gpu.read_vcore_mv()  # the text the NULL handle of a closed Gpu gave before (a ValueError)
        self.assertTrue(issubclass(native.NativeClosed, ValueError))
        self.gpu.close()  # twice: a no-op

    def test_close_waits_for_the_holder_and_the_free_waits_for_the_last_reference(self):
        closed = self.watch_close()
        gpu, self.gpu = self.gpu, None
        value = gpu._handle.value
        box = weakref.ref(gpu._box)
        holder = Holder(gpu._lock, body=lambda: self.library.dc_gpu_read_vcore_mv(
            box().handle, ctypes.byref(ctypes.c_int32()), ctypes.byref(ctypes.c_double())))
        closer = threading.Thread(target=gpu.close, daemon=True)
        closer.start()
        closer.join(BLOCKED)
        self.assertTrue(closer.is_alive(), "close() did not wait for the holder")
        self.assertFalse(gpu._box.closed)
        holder.release()
        closer.join(WAIT)
        self.assertFalse(closer.is_alive())
        self.assertEqual(holder.result, native.DC_OK)  # the holder's own entry ran on a live handle
        self.assertTrue(box().closed)
        with self.assertRaises(native.NativeClosed):
            gpu.read()
        self.assertNotIn(value, closed)  # invalidated, not freed: gpu still refers to the box
        del gpu, closer
        self.assertIsNone(box())
        self.assertEqual(closed.count(value), 1)

    def test_a_dropped_gpu_is_not_freed_while_another_thread_holds_or_waits(self):
        closed = self.watch_close()
        gpu, self.gpu = self.gpu, None
        value, lock, box = gpu._handle.value, gpu._lock, weakref.ref(gpu._box)
        holder = Holder(lock)
        got = threading.Event()

        def wait_for_it(lock):
            with lock:
                got.set()
        waiter = threading.Thread(target=wait_for_it, args=(lock,), daemon=True)
        waiter.start()
        self.assertFalse(got.wait(BLOCKED))
        del gpu, lock
        gc.collect()
        self.assertIsNotNone(box())  # the holder's and the waiter's lock keep it
        self.assertNotIn(value, closed)
        holder.release()
        waiter.join(WAIT)
        self.assertTrue(got.is_set())
        del waiter, holder
        gc.collect()
        self.assertIsNone(box())
        self.assertEqual(closed.count(value), 1)

    def test_a_dropped_gpu_is_freed_promptly(self):
        closed = self.watch_close()
        gpu, self.gpu = self.gpu, None
        value, box = gpu._handle.value, weakref.ref(gpu._box)
        gpu.read()
        gpu.static.get("pl_max_mw")  # the capability merge: _Static calls back into its Gpu
        with gpu._lock:
            gpu.read_vcore_mv()
        other = native.Gpu()  # its static dict is never merged
        other_value, other_box, static = other._handle.value, weakref.ref(other._box), other.static
        enabled = gc.isenabled()
        gc.disable()
        try:
            del gpu, other  # reference counting alone: no cycle keeps either Gpu or its box
            self.assertIsNone(box())
            self.assertIsNone(other_box())
            self.assertEqual((closed.count(value), closed.count(other_value)), (1, 1))
        finally:
            if enabled:
                gc.enable()
        with self.assertRaises(native.NativeClosed):
            static.get("pl_max_mw")  # the static dict does not keep its Gpu alive
        self.assertEqual(static["slot"], static.get("slot"))  # the identity keys stay readable


class CallAndFetchTests(LockCase):
    LIMIT = 30

    def test_stock_max_all_and_native_limit_keep_their_message_fetch_locked(self):
        # Force the ABI's documented larger-buffer response even when the no-driver
        # error is short. The real fetch still supplies that error. A second thread
        # tries the core mutex in the gap, releasing it immediately if it succeeds:
        # deleting any wrapper's outer lock then fails, rather than wedging cleanup.
        gpu, library = self.gpu, self.library
        cases = (("dc_pl_stock_power_policy", "dc_pl_message_get", lambda: gpu.stock_power_policy(4)),
                 ("dc_pl_max_all", "dc_pl_step_text_get", gpu.max_all_power_policies),
                 ("dc_pl_native_power_limit", "dc_pl_message_get", gpu.read_native_power_limit_mw))
        for entry_name, fetch_name, action in cases:
            with self.subTest(entry=entry_name):
                expected = action()
                entry, fetch = getattr(library, entry_name), getattr(library, fetch_name)
                probes = []

                def larger(*args, _entry=entry, _name=entry_name):
                    status = _entry(*args)
                    self.assertEqual(status, native.DC_OK)
                    if _name == "dc_pl_max_all":
                        self.assertGreater(args[-1]._obj.value, 0)
                        args[3][0].message_needed = 8192
                    else:
                        args[-1]._obj.value = 8192
                    return status

                def checked_fetch(*args, _fetch=fetch, _probes=probes):
                    _probes.append(core_trylock_elsewhere(gpu._handle))
                    return _fetch(*args)

                with mock.patch.object(library, entry_name, larger), \
                        mock.patch.object(library, fetch_name, checked_fetch):
                    self.assertEqual(action(), expected)
                self.assertEqual(probes, [(native.DC_OK, 0)])

    def test_another_caller_cannot_run_between_a_call_and_its_message_fetch(self):
        # set_power_limit_mw with a first buffer too small for the text: the binding fetches the kept
        # message with dc_pl_message_get under self._lock. A second thread calls the core directly
        # (no binding, as a native UI would) once the call has returned and before the fetch; with
        # one lock its call waits until the fetch is done.
        gpu, library = self.gpu, self.library
        gpu._PL_MESSAGE = 8
        expected = gpu.set_power_limit_mw(123456)
        self.assertGreater(len(expected[1]), 8)
        real = library.dc_pl_message_get
        windows = []

        def fetch(*args):
            between, returned = threading.Event(), threading.Event()

            def intruder():
                between.wait(WAIT)
                has, mv = ctypes.c_int32(), ctypes.c_double()
                library.dc_gpu_read_vcore_mv(gpu._handle, ctypes.byref(has), ctypes.byref(mv))
                returned.set()
            thread = threading.Thread(target=intruder, daemon=True)
            thread.start()
            between.set()
            windows.append((returned.wait(BLOCKED), thread))
            return real(*args)
        self.patch_library("dc_pl_message_get", fetch)
        for _ in range(3):
            self.assertEqual(gpu.set_power_limit_mw(123456), expected)
        for interleaved, thread in windows:
            thread.join(WAIT)
            self.assertFalse(thread.is_alive())
            self.assertFalse(interleaved, "another caller's entry ran between the call and its fetch")
        self.assertEqual(len(windows), 3)

    def nvml_rig(self):
        """The NVML-only power-limit rig of tests/test_native_power_limit.py: a verified write answers
        "power limit configured to <mW/1000> W", a text that differs per request and outgrows the
        8-byte first buffer, so every call goes through its message fetch."""
        from tests.test_native_power_limit import READBACK_STATIC, NvmlPower, PowerDriver, set_static
        from tests.test_native_target_binding import Card
        from tests.test_native_transport import install
        driver = PowerDriver()
        power = NvmlPower(lambda: driver, enforced=349200)
        card = Card(with_nvapi=False)
        card.nvml.exports.update(power.exports)
        install(card.nvml, None, None)
        self.addCleanup(native.set_export_resolver, None)
        gpu = native.Gpu()
        self.cleanup(gpu.close)
        set_static(gpu, READBACK_STATIC)
        gpu._PL_MESSAGE = 8
        return gpu

    REQUESTS = (350000, 250000)

    def test_a_binding_caller_in_another_callers_window_waits_for_its_fetch(self):
        # Deterministic: thread A's fetch starts thread B's set_power_limit_mw (through the binding)
        # and gives it BLOCKED to run; B's call must wait, so A fetches its own text and B its own.
        # Without `with self._lock` around the call and the fetch, B's write replaces the kept text
        # before A reads it.
        gpu = self.nvml_rig()
        a_mw, b_mw = self.REQUESTS
        expected = {mw: (True, f"power limit configured to {mw / 1000:g} W") for mw in self.REQUESTS}
        self.assertEqual({mw: gpu.set_power_limit_mw(mw) for mw in self.REQUESTS}, expected)
        real, me, b_result, b_done = self.library.dc_pl_message_get, threading.get_ident(), [], threading.Event()
        b_thread = threading.Thread(target=lambda: (b_result.append(gpu.set_power_limit_mw(b_mw)), b_done.set()),
                                    daemon=True)
        early = []

        def fetch(*args):
            if threading.get_ident() == me and not b_thread.is_alive() and not early:
                b_thread.start()
                early.append(b_done.wait(BLOCKED))
            return real(*args)
        self.patch_library("dc_pl_message_get", fetch)
        self.assertEqual(gpu.set_power_limit_mw(a_mw), expected[a_mw])
        b_thread.join(WAIT)
        self.assertEqual(early, [False], "B's call ran inside A's call-and-fetch window")
        self.assertEqual(b_result, [expected[b_mw]])

    def test_two_threads_fetching_different_long_messages_each_get_their_own(self):
        # Contention through the binding, with texts that differ per thread. Each call's window (its
        # dc_pl_set returning to its fetch) is widened, each thread pauses briefly outside the lock
        # between calls (glibc's mutex is not fair: without the pause one thread can take it 100
        # times in a row), and the case requires that each thread, inside its window, has seen the
        # other one's call already started and not yet through its dc_pl_set: the two competed.
        gpu = self.nvml_rig()
        expected = {mw: (True, f"power limit configured to {mw / 1000:g} W") for mw in self.REQUESTS}
        library = self.library
        real_set, real_fetch = library.dc_pl_set, library.dc_pl_message_get
        pending, overlaps, by_thread = {mw: False for mw in self.REQUESTS}, {mw: 0 for mw in self.REQUESTS}, {}
        state = threading.Lock()

        def pl_set(*args):
            status = real_set(*args)
            with state:
                pending[by_thread[threading.get_ident()]] = False
            return status

        def fetch(*args):
            time.sleep(0.002)  # a wide window between the call and its fetch
            me = by_thread[threading.get_ident()]
            with state:
                if any(pending[mw] for mw in self.REQUESTS if mw != me):
                    overlaps[me] += 1
            return real_fetch(*args)
        self.patch_library("dc_pl_set", pl_set)
        self.patch_library("dc_pl_message_get", fetch)
        results = {mw: [] for mw in self.REQUESTS}
        start = threading.Barrier(len(self.REQUESTS))

        def worker(mw):
            by_thread[threading.get_ident()] = mw
            start.wait(WAIT)
            for _ in range(100):
                with state:
                    pending[mw] = True
                results[mw].append(gpu.set_power_limit_mw(mw))
                time.sleep(0.001)
        threads = [threading.Thread(target=worker, args=(mw,), daemon=True) for mw in self.REQUESTS]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(40)
            self.assertFalse(thread.is_alive())
        for mw, outcomes in results.items():
            self.assertEqual(outcomes, [expected[mw]] * 100)
        self.assertTrue(all(overlaps.values()), f"the threads never competed: {overlaps}")


def clamp_rig(case):
    """tests/test_native_current_limit_state.py's turing derived-clamp rig: get_current_limits() runs
    dc_cl_state, whose confirm callback calls gpu.read_live_power_request on the holding thread.
    (gpu, the expected (limit, requested, maximum) rows)."""
    import tests.test_native_current_limit_state as state_tests
    base = state_tests.base
    kw, maximum, requested, _ = state_tests.DerivedClampTests.CASES["turing"]
    setup = state_tests.DerivedClampTests.clamp(None, kw, maximum, requested)
    side = base.Side("native", **kw)
    case.cleanup(side.close)
    setup(side)
    return side.gpu, [(maximum, requested, maximum)]


def limits(rows):
    return [(row["limit_ma"], row["requested_ma"], row["maximum_ma"]) for row in rows]


class CallbackCase(BoundedCase):
    def setUp(self):
        require_cdll(self)
        self.library = native.load()

    def tearDown(self):
        native.set_export_resolver(None)

    def in_confirm(self, gpu, callback):
        """Run callback() inside the confirm callback of one get_current_limits() (it then reads the
        live request as before): the rows."""
        read_live = gpu.read_live_power_request

        def confirm_read():
            callback()
            return read_live()
        gpu.read_live_power_request = confirm_read
        try:
            return gpu.get_current_limits()
        finally:
            del gpu.read_live_power_request


class CallbackTests(CallbackCase):
    """Core callbacks run on the thread that holds the handle mutex: taking gpu._lock inside one is
    recursion, not a wait, and another thread's `with gpu._lock` around a core entry is serialized
    with the whole entry (the two-lock rule "callbacks must never wait for Gpu._lock" is gone)."""

    def test_a_confirm_callback_runs_under_the_card_lock_and_takes_it(self):
        gpu, expected = clamp_rig(self)
        handle, seen, intruder = gpu._handle, {}, Intruder(gpu)

        def callback():
            seen["owned"] = gpu._lock._is_owned()  # no `with` here: the entry's own level
            seen["python elsewhere"] = trylock_elsewhere(gpu)
            seen["core elsewhere"] = core_trylock_elsewhere(handle)
            seen["intruder got in"] = intruder.start_and_watch()
            with gpu._lock:
                with gpu._lock:
                    gpu.read_vcore_mv()  # a core entry inside the callback: one more level
                    seen["nested"] = (threading.get_ident(), gpu._lock._is_owned())
        self.assertEqual(limits(self.in_confirm(gpu, callback)), expected)  # confirmed as before
        self.assertTrue(intruder.finish(), "the thread holding gpu._lock around a core entry never finished")
        self.assertEqual(seen, {"owned": True, "python elsewhere": False, "core elsewhere": (native.DC_OK, 0),
                                "intruder got in": False, "nested": (threading.get_ident(), True)})
        self.assertTrue(intruder.entered.is_set())
        self.assertFalse(gpu._lock._is_owned())
        self.assertTrue(trylock_elsewhere(gpu))

    def test_a_before_write_hook_runs_under_the_card_lock_and_takes_it(self):
        from tests.test_native_hold_headroom import HoldNativeSide, HoldRig, card
        from druta import native_rails as nr
        rig = HoldRig(**card())
        self.cleanup(rig.close)
        self.addCleanup(native.hook_test_fail_protect, False)
        gpu = rig.native
        side = HoldNativeSide(gpu, rig)
        side.fake = rig.fake
        rig.fake.rig = rig
        side.seed()
        seen, intruder = [], Intruder(gpu)

        def hook(record):
            owned, python_elsewhere = gpu._lock._is_owned(), trylock_elsewhere(gpu)
            core_elsewhere = core_trylock_elsewhere(gpu._handle)
            got_in = intruder.start_and_watch()
            with gpu._lock:
                with gpu._lock:
                    state = nr.hold_state(gpu)  # a core entry inside the callback: one more level
                    seen.append((threading.get_ident(), owned, python_elsewhere, core_elsewhere, got_in,
                                 gpu._lock._is_owned(), type(state).__name__, record["rail"]))
        self.assertTrue(side._armed(lambda: gpu.apply_hold_headroom(1093.75, 25.0, before_write=hook))[0])
        self.assertTrue(intruder.finish(), "the thread holding gpu._lock around a core entry never finished")
        self.assertTrue(side._armed(gpu.restore_hold_headroom)[0])
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][:6], (threading.get_ident(), True, False, (native.DC_OK, 0), False, True))
        self.assertFalse(gpu._lock._is_owned())
        self.assertTrue(trylock_elsewhere(gpu))


class CallbackUnlockTests(CallbackCase):
    """An unlock inside a core callback gives back only a level the caller took through dc_gpu_lock /
    dc_gpu_trylock, never the running entry's own (the core counts the two apart)."""

    def assert_free_and_usable(self, gpu):
        handle = gpu._handle
        self.assertEqual(lock_held(handle), 0)
        for _ in range(3):
            self.assertEqual((self.library.dc_gpu_lock(handle), self.library.dc_gpu_unlock(handle)),
                             (native.DC_OK, native.DC_OK))
        self.assertEqual(self.library.dc_gpu_unlock(handle), native.DC_E_STATE)
        self.assertEqual(core_trylock_elsewhere(handle), (native.DC_OK, 1))  # nothing left held
        with gpu._lock:
            self.assertFalse(trylock_elsewhere(gpu))
        self.assertTrue(trylock_elsewhere(gpu))

    def test_a_stray_unlock_with_no_level_of_the_callers_own_is_refused(self):
        gpu, expected = clamp_rig(self)
        handle, seen = gpu._handle, {}

        def callback():
            seen["unlock"] = self.library.dc_gpu_unlock(handle)
            seen["elsewhere"] = core_trylock_elsewhere(handle)  # the entry is still serialized
            seen["held"] = lock_held(handle)
        self.assertEqual(limits(self.in_confirm(gpu, callback)), expected)
        self.assertEqual(seen, {"unlock": native.DC_E_STATE, "elsewhere": (native.DC_OK, 0), "held": 1})
        self.assert_free_and_usable(gpu)

    def test_one_unlock_too_many_gives_back_only_the_callers_own_level(self):
        # The documented caller's error: the caller's level goes early, the entry keeps its own.
        gpu, expected = clamp_rig(self)
        handle, seen = gpu._handle, {}
        self.assertEqual(self.library.dc_gpu_lock(handle), native.DC_OK)

        def callback():
            seen["unlock"] = self.library.dc_gpu_unlock(handle)
            seen["again"] = self.library.dc_gpu_unlock(handle)
            seen["elsewhere"] = core_trylock_elsewhere(handle)
            seen["owned"] = gpu._lock._is_owned()
        self.assertEqual(limits(self.in_confirm(gpu, callback)), expected)
        self.assertEqual(seen, {"unlock": native.DC_OK, "again": native.DC_E_STATE,
                                "elsewhere": (native.DC_OK, 0), "owned": True})
        self.assertEqual(self.library.dc_gpu_unlock(handle), native.DC_E_STATE)  # the caller's level is gone
        self.assert_free_and_usable(gpu)

    def test_release_with_only_the_entrys_level_raises_and_changes_nothing(self):
        gpu, expected = clamp_rig(self)
        seen = {}

        def callback():
            try:
                gpu._lock.release()
                seen["release"] = "accepted"
            except RuntimeError as exc:
                seen["release"] = str(exc)
            seen["elsewhere"] = core_trylock_elsewhere(gpu._handle)
        self.assertEqual(limits(self.in_confirm(gpu, callback)), expected)
        self.assertEqual(seen, {"release": "cannot release un-acquired lock", "elsewhere": (native.DC_OK, 0)})
        self.assert_free_and_usable(gpu)

    def test_close_inside_a_callback_raises_and_the_read_completes(self):
        gpu, expected = clamp_rig(self)
        seen = {}

        def callback():
            try:
                gpu.close()
                seen["close"] = "returned"
            except RuntimeError as exc:
                seen["close"] = str(exc)
            seen["closed"] = gpu._box.closed
        self.assertEqual(limits(self.in_confirm(gpu, callback)), expected)  # not an empty list
        self.assertEqual(seen["closed"], False)
        self.assertRegex(seen["close"], r"holds the card's lock .*core callback")
        self.assertEqual(limits(gpu.get_current_limits()), expected)
        gpu.close()
        with self.assertRaises(native.NativeClosed):
            gpu.get_current_limits()


class LegacyP0Tests(BoundedCase):
    """The core drops its levels across the hold's 100 ms waits (nvbackend's _synchronized hold keeps
    its RLock throughout): a read() then runs during the hold and returns a valid sample; holding
    gpu._lock around the call gives Python's behaviour."""

    def setUp(self):
        require_cdll(self)

    def tearDown(self):
        native.set_sleep_hook(None)
        native.set_export_resolver(None)

    def hold_with_probe(self, outer_lock):
        from druta import native_legacy_p0 as nl
        from tests.test_native_legacy_p0 import GOOD, HELD, P0Rig
        rig = P0Rig()
        gpu = rig.native_gpu()
        self.cleanup(gpu.close)
        rig.reset((GOOD,))
        probes = []

        def wait(_ms):
            if probes:
                return
            done, seen = threading.Event(), {}

            def probe():
                seen["owned"] = nl.legacy_p0_owned(gpu)
                seen["sample"] = gpu.read()
                done.set()
            thread = threading.Thread(target=probe, daemon=True)
            thread.start()
            probes.append((done.wait(BLOCKED), seen, thread))
        native.set_sleep_hook(wait)
        if outer_lock:
            with gpu._lock:
                result = nl.hold_legacy_p0(gpu)
        else:
            result = nl.hold_legacy_p0(gpu)
        native.set_sleep_hook(None)
        finished, seen, thread = probes[0]
        thread.join(WAIT)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result, (True, HELD.format(core=705, mem=3004)))
        self.assertTrue(nl.legacy_p0_owned(gpu))
        return finished, seen

    def test_a_read_during_the_hold_returns_a_valid_sample_and_keeps_ownership(self):
        finished, seen = self.hold_with_probe(outer_lock=False)
        self.assertTrue(finished, "the read waited for the whole hold")
        self.assertTrue(seen["owned"])  # the request is owned from before it is made
        sample = seen["sample"]
        self.assertEqual((sample["pstate"], sample["core"], sample["mem"], sample["mem_p0max"]), (0, 705, 3004, 3004))

    def test_holding_the_lock_around_the_hold_gives_pythons_behaviour(self):
        finished, seen = self.hold_with_probe(outer_lock=True)
        self.assertFalse(finished, "the read ran inside a hold the caller locked")
        self.assertTrue(seen["owned"])  # it ran after the hold returned
        self.assertEqual(seen["sample"]["pstate"], 0)


if __name__ == "__main__":
    unittest.main()
