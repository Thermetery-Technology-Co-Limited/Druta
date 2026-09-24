# Druta - a monitor and tuner for NVIDIA GPUs.
# Copyright (C) 2026 Thermetery Technology Co Limited
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Memory-timing WRITES. The only module in Druta that can build a writing
nvtune command line.

WHY THIS IS A SEPARATE MODULE. `timings.py` states in its own docstring that no
code path in it - "not behind a flag, not disabled, not dead" - can construct an
argv able to write a timing register. That guarantee is worth more than the
convenience of putting the writer next to the reader, so it stays true: the read
path still cannot write, and everything that can write is here, in one file, for
one auditor to read.

WHAT A WRITE COSTS IF IT GOES WRONG. An FBPA register write can hang the machine
and corrupt VRAM. Every guard below exists because of something that actually
happened during this project.

THE DISTINCTION THIS MODULE EXISTS TO GET RIGHT: "the tool refused" and "the
hardware dropped it" are NOT the same result, and a sweep that conflates them
produces a confident wrong conclusion. Our own Turing field sweep classified
purely on read-back, discarded nvtune's return, and never passed --force - so
every field whose value tripped nvtune's range check was recorded as a hardware
rejection despite never reaching BAR0. Four of twenty-five. Here the two are
different outcome constants. A read-only preview always runs first, and actual
process status and readback determine the outcome of the commit.

MEASURED, and the reason the architecture note is not decoration:
  TU102 (Titan RTX)  every timing write rejected by the hardware
  GP102 (Titan Xp)   FAW 24->25 applied, verified, held, restored clean
  GK104 (GTX 770)    FAW 32->33 applied across all four partitions, held,
                    restored exactly twice on 472.12; VRAM copy checks passed
Same tool, same driver, same slot. So this module reports what happened; it does
not promise a write will land.
"""
import json
import hashlib
import os
import re
import subprocess
from dataclasses import dataclass

from . import timings

# ---- outcomes -------------------------------------------------------------- #
LANDED = "landed"            # written, read back changed
DROPPED = "dropped"          # reached hardware, read back UNCHANGED
TOOL_REFUSED = "refused"     # Druta/nvtune declined; BAR0 was never touched
FAILED = "failed"            # nvtune errored, or we could not parse it

OUTCOME_TEXT = {
    LANDED: "written and verified",
    DROPPED: "reached the hardware and was rejected",
    TOOL_REFUSED: "refused before hardware write",
    FAILED: "nvtune failed",
}

# Fields nvtune marks [structural] are training and phase parameters: there is
# no "looser" direction and a bad value breaks memory training rather than
# merely running slow. Writing one is possible but never accidental.
STRUCTURAL_BLOCKED = ("structural - a training/phase fragment, not a delay. "
                      "There is no safe direction to nudge this.")

_OP_RE = re.compile(
    r"^\s*(?P<reg>\w+)\s+@(?P<off>0x[0-9A-Fa-f]+)\s+"
    r"(?P<old>0x[0-9A-Fa-f]+)\s*->\s*(?P<new>0x[0-9A-Fa-f]+)\s*"
    r"\[(?P<mode>would write|write)\]")
_CHG_RE = re.compile(r"^\s+(?P<name>\w+)\s+(?P<old>\d+)\s*->\s*(?P<new>\d+)\s*$")
# nvtune's print_ops() (tool/src/cli.cpp) prints a register whose requested
# fields already hold their values as exactly
#   "  " NAME " @" hex(offset, 6) "  unchanged (" hex(old_word, 8) ")"
# where hex(v, w) is "0x" plus uppercase digits zero-padded to w, and prints
# nothing else for that op. The row is neither a write nor a warning. Only
# this exact row is skipped; a near miss is still classified below as before.
_UNCHANGED_RE = re.compile(r"  [A-Za-z0-9_]+ @0x[0-9A-F]{6,8}  unchanged \(0x[0-9A-F]{8}\)")
_REFUSE_RE = re.compile(r"refusing to write with warnings", re.I)


class WriteError(RuntimeError):
    pass


class Plan:
    """A native dry run or a local preview calculated from read-only data.

    Druta requires force for preview warnings even when the helper itself
    treats them as advisory. Local previews cannot reproduce hidden helper
    policy, such as the typical ranges in older nvtune builds.
    """

    def __init__(self, assignments, ops, warnings, raw, ok=True, error="", notes=""):
        self.assignments = dict(assignments)
        self.ops = ops              # [{reg, offset, old, new, changes:[...]}]
        self.warnings = warnings
        self.raw = raw
        self.ok = ok
        self.error = error
        self.notes = notes

    @property
    def needs_force(self):
        return bool(self.warnings)

    @property
    def touches(self):
        return [c["name"] for op in self.ops for c in op["changes"]]

    def summary(self):
        if not self.ok:
            return self.error or "the preview failed"
        if not self.ops:
            return "nothing to write - every field already holds that value" + (
                f"\n{self.notes}" if self.notes else "")
        bits = []
        for op in self.ops:
            for c in op["changes"]:
                bits.append(f"{c['name']} {c['old']}->{c['new']}")
        regs = ", ".join(f"{op['reg']} {op['old']}->{op['new']}"
                         for op in self.ops)
        head = f"{len(bits)} field(s): {', '.join(bits)}   [{regs}]"
        if self.warnings:
            head += (f"\n{len(self.warnings)} warning(s) - a commit WITHOUT "
                     f"force will be refused and nothing will reach the card:"
                     + "".join(f"\n  - {w}" for w in self.warnings))
        if self.notes:
            head += "\n" + self.notes
        return head


class Result:
    """One field's outcome, with the two failure modes kept apart."""

    def __init__(self, name, before, requested, after, outcome, detail=""):
        self.name = name
        self.before = before
        self.requested = requested
        self.after = after
        self.outcome = outcome
        self.detail = detail

    @property
    def reverted(self):
        return self.outcome == LANDED and self.after != self.requested

    def __repr__(self):
        return (f"<{self.name} {self.before}->{self.requested} "
                f"got {self.after} {self.outcome}>")


# ---- the single choke point ------------------------------------------------ #
@dataclass(frozen=True)
class HelperContract:
    exe: str
    fingerprint: tuple
    preview_flags: tuple | None
    commit_flags: tuple | None
    supports_force: bool


_HELPER_CONTRACTS = {}


def _exe_fingerprint(exe):
    stat = os.stat(exe)
    # Windows ctime is creation time. A same-size copy preserving mtime can
    # replace a legacy helper without changing any of the stat fields.
    with open(exe, "rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    return (os.path.normcase(os.path.realpath(exe)), stat.st_size,
            stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino, digest)


def _helper(override=None):
    """Discover preview, commit and force separately, without a trial write."""
    exe = timings.find_exe(override)
    if not exe:
        raise WriteError("nvtune.exe not found")
    key = _exe_fingerprint(exe)
    if key in _HELPER_CONTRACTS:
        return _HELPER_CONTRACTS[key]
    result = subprocess.run([exe, "--help"], capture_output=True, text=True,
                            timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode != 0:
        raise WriteError("cannot determine nvtune's command contract: --help failed")
    help_text = (result.stdout or "") + (result.stderr or "")

    def option(name):
        return bool(re.search(r"^\s*(?:-\w,\s*)?" + re.escape(name) + r"\b",
                              help_text, re.M))

    legacy = ("Everything defaults to a dry run" in help_text
              and "--commit is required to touch hardware" in help_text)
    # Both published upstream releases describe immediate writes this way.
    # Recognize the command contract, not a version or a binary allowlist.
    direct = ("nvtune - NVIDIA FBPA memory timing tool" in help_text
              and re.search(r"^\s*set FIELD=VALUE\.\.\.\s+write fields\s*$", help_text, re.M)
              and "A first write snapshots stock values" in help_text
              and "--commit" not in help_text)
    if option("--dry-run"):
        preview_flags = ("--dry-run",)
    elif legacy:
        preview_flags = ()
    elif direct:
        preview_flags = None
    else:
        raise WriteError("nvtune does not advertise a recognized command contract; no set command sent")
    commit_flags = (("--commit",) if option("--commit") or legacy
                    else () if direct else None)
    if _exe_fingerprint(exe) != key:
        raise WriteError("nvtune changed while its command contract was being checked")
    helper = HelperContract(exe, key, preview_flags, commit_flags, option("--force"))
    _HELPER_CONTRACTS[key] = helper
    return helper


def _assert_helper(helper):
    if _exe_fingerprint(helper.exe) != helper.fingerprint:
        raise WriteError("nvtune changed during the operation; nothing further sent")


def _run(args, override=None, timeout=90, slot=None, *, helper=None):
    """Spawn nvtune. This is the ONLY place in Druta that may build an argv
    containing a writing subcommand.

    `slot` is MANDATORY here, and missing it raises rather than defaulting.
    nvtune's `-d` defaults to "all NVIDIA GPUs", so on a two-card host the
    argv this function builds without a slot does not write the card the user
    was looking at - it writes EVERY card. Verified on the two-card rig:

        nvtune set FAW=13        (dry run, no -d)
        0000:01:00.0  TU102 (Turing)   CONFIG3 0x2200104C -> 0x22001A4C  FAW  8 -> 13
        0000:02:00.0  GP102 (Pascal)   CONFIG3 0x2200194A -> 0x22001B4A  FAW 12 -> 13

    One number entered in the UI, planned into two different chips whose stock
    values are not even the same. With --commit that is a two-card write, and
    the second card was never named on screen. The read path has the same shape
    in reverse: `get FAW` prints one line per card and read_fields() keeps the
    LAST, so the before/after comparison that classifies a write as LANDED or
    DROPPED would have been read off the wrong silicon."""
    if not slot:
        raise WriteError(
            "refused: no PCI slot given. nvtune's default target is every "
            "NVIDIA GPU in the machine, so a write without -d would reach "
            "cards the user never selected.")
    exe = helper.exe if helper else timings.find_exe(override)
    if not exe:
        raise WriteError("nvtune.exe not found")
    args = list(args)
    if not args:
        raise WriteError("refused: empty nvtune argv")
    if args[0] in ("set", "apply", "restore"):
        helper = helper or _helper(exe)
        if "--commit" in args:
            if "--dry-run" in args:
                raise WriteError("preview and commit cannot be combined")
            if helper.commit_flags is None:
                raise WriteError("nvtune does not advertise a recognized commit contract")
            index = args.index("--commit")
            # Internal intent; not every CLI accepts this token.
            args[index:index + 1] = helper.commit_flags
        else:
            if args[0] == "restore" or helper.preview_flags is None:
                raise WriteError("this nvtune has no native preview; no write command sent")
            if "--dry-run" in args:
                args.remove("--dry-run")
            args.extend(helper.preview_flags)
        if "--force" in args and not helper.supports_force:
            args.remove("--force")
        _assert_helper(helper)
    # -d goes AFTER the subcommand. nvtune parses argv[1] as the command name,
    # so `nvtune -d SLOT set ...` exits with "unknown command '-d'" - which,
    # being a non-zero exit with no ops parsed, would have surfaced as a plain
    # write failure rather than as the argv bug it is.
    argv = [exe, args[0], "-d", str(slot)] + args[1:]
    r = subprocess.run(argv, capture_output=True,
                       text=True, timeout=timeout,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return ((r.stdout or "") + (r.stderr or "")).strip(), r.returncode


def _parse(out):
    ops, warnings = [], []
    cur = None
    for line in out.splitlines():
        m = _OP_RE.match(line)
        if m:
            cur = {"reg": m.group("reg"), "offset": m.group("off"),
                   "old": m.group("old"), "new": m.group("new"),
                   "changes": []}
            ops.append(cur)
            continue
        m = _CHG_RE.match(line)
        if m and cur is not None:
            cur["changes"].append({"name": m.group("name"),
                                   "old": int(m.group("old")),
                                   "new": int(m.group("new"))})
            continue
        if _UNCHANGED_RE.fullmatch(line):
            continue
        s = line.strip()
        # nvtune prints warnings as bare indented lines under an op; anything
        # that is not an op, a change, a banner or a reminder is one.
        if (s and cur is not None and not s.startswith("[")
                and "applied and verified" not in s
                and not s.startswith("reminder:")
                and not _OP_RE.match(line) and not s.startswith("0000:")
                and s != "dry run complete: no registers written"
                and "stock values saved" not in s):
            warnings.append(s)
    return ops, warnings


def read_fields(names, slot, override=None):
    """Current cycle counts for `names`. Read-only; uses the `get` subcommand.

    Single-card by contract. With no slot nvtune prints one line per card and
    the loop below - which keys by field name - would silently keep whichever
    card came last."""
    if not names:
        return {}
    out, rc = _run(["get"] + list(names), override, slot=slot)
    if rc != 0:
        raise WriteError(out or f"nvtune get exited {rc}")
    vals = {}
    for tok in out.replace(",", " ").split():
        if "=" in tok:
            k, _, v = tok.partition("=")
            if k in names and v.isdigit():
                vals[k] = int(v)
    return vals


def plan(assignments, slot, override=None, *, helper=None):
    """Read-only preview, including for helpers whose bare set writes."""
    if not assignments:
        return Plan({}, [], [], "", ok=True)
    args = ["set"] + [f"{k}={v}" for k, v in assignments.items()]
    try:
        if not timings.nvbackend.parse_slot(slot):
            raise WriteError("no valid PCI slot for the selected card")
        helper = helper or _helper(override)
        if helper.preview_flags is None:
            return _local_plan(assignments, slot, helper)
        out, rc = _run(args, helper.exe, slot=slot, helper=helper)
    except (OSError, subprocess.SubprocessError, timings.TimingsError, WriteError) as e:
        return Plan(assignments, [], [], "", ok=False, error=str(e))
    ops, warnings = _parse(out)
    # A warning-only preview exits successfully. Any nonzero status remains
    # a failure even if a partial plan was printed first; force cannot fix it.
    if rc != 0:
        return Plan(assignments, [], warnings, out, ok=False,
                    error=f"nvtune dry run exited {rc}: {out or 'no error text'}")
    return Plan(assignments, ops, warnings, out)


_DUMP_DEVICE_RE = re.compile(r"^(\S+)\s+(\S+)\s+\([^\n]*\)\s*$")
_DUMP_SCOPE_RE = re.compile(r"^\s*\[([^\]]+)\]\s+base\s+(0x[0-9a-fA-F]+)\s*$")
_DUMP_REG_RE = re.compile(
    r"^\s*(\w+)\s+@(0x[0-9a-fA-F]+)\s*=\s*(0x[0-9a-fA-F]+)"
    r"(?:\s+\((\w+)\))?\s*$")


def _local_plan(assignments, slot, helper):
    """Use the helper's field geometry and actual broadcast words/addresses.

    Only `fields` and selected-card `dump --raw` are dispatched. In particular,
    trying `set` to discover whether it previews is never safe.
    """
    _assert_helper(helper)
    r = timings._run(helper.exe, "fields")
    if r.returncode != 0:
        raise WriteError(f"nvtune fields exited {r.returncode}: {(r.stderr or r.stdout).strip()}")
    ft = timings.parse_fields(r.stdout)
    if any(type(value) is not int for value in assignments.values()):
        raise WriteError("local preview refused: timing values must be integers")
    problems = check(assignments, ft)
    masks = {}
    for name, value in assignments.items():
        fields = [f for f in ft.fields if f.name == name]
        if len(fields) != 1:
            problems.append(f"{name}: missing or duplicate field definition")
            continue
        f = fields[0]
        if not (0 <= f.lo <= f.hi < 32) or not f.width_consistent:
            problems.append(f"{name}: invalid field bit geometry")
            continue
        mask = f.max_value << f.lo
        if masks.get(f.register, 0) & mask:
            problems.append(f"{name}: overlapping requested fields in {f.register}")
        masks[f.register] = masks.get(f.register, 0) | mask
    if problems:
        raise WriteError("local preview refused: " + "; ".join(problems))

    r = timings._run(helper.exe, "dump", ["--raw"], slot=slot)
    if r.returncode != 0:
        raise WriteError(f"nvtune dump exited {r.returncode}: {(r.stderr or r.stdout).strip()}")
    words, device, broadcast = {}, False, False
    for line in r.stdout.splitlines():
        if not line.strip():
            continue
        m = _DUMP_DEVICE_RE.match(line)
        if m and timings.nvbackend.parse_slot(m[1]):
            if device or not timings.nvbackend.same_slot(m[1], slot):
                raise WriteError("nvtune dump did not identify only the selected PCI slot")
            if not timings.known_timing_layout(m[2]):
                raise WriteError("nvtune has no confirmed timing layout for this chip")
            device = True
            continue
        m = _DUMP_SCOPE_RE.match(line)
        if m:
            if not device or broadcast or m[1] != "broadcast":
                raise WriteError("nvtune dump did not identify one broadcast scope")
            broadcast = True
            continue
        m = _DUMP_REG_RE.match(line)
        if not m or not broadcast:
            raise WriteError("unrecognized nvtune dump output; local preview refused")
        name, offset, word, confidence = m.groups()
        if (name in words or int(word, 16) > 0xFFFFFFFF
                or int(offset, 16) > 0xFFFFFFFF or int(offset, 16) % 4
                or any(int(existing[0], 16) == int(offset, 16) for existing in words.values())):
            raise WriteError("invalid or duplicate register in nvtune dump")
        words[name] = (offset, int(word, 16), confidence)
    if not device or not broadcast:
        raise WriteError("nvtune dump is missing the selected card or broadcast scope")

    ops, warnings = [], []
    for register in ft.registers:
        selected = [f for f in ft.fields if f.register == register and f.name in assignments]
        if not selected:
            continue
        if register not in words:
            raise WriteError(f"nvtune dump is missing {register}")
        offset, old, confidence = words[register]
        if confidence and confidence.upper() != "DOCUMENTED":
            raise WriteError(f"{register}: unconfirmed register offset ({confidence})")
        new, changes = old, []
        for f in selected:
            value, before = assignments[f.name], f.extract(old)
            new = (new & ~(f.max_value << f.lo)) | (value << f.lo)
            if before != value:
                changes.append({"name": f.name, "old": before, "new": value})
            if before and value * 2 < before:
                warnings.append(f"{f.name} more than halved ({before} -> {value}); step in small increments instead.")
        if changes:
            ops.append({"reg": register, "offset": offset, "old": f"0x{old:08X}",
                        "new": f"0x{new:08X}", "changes": changes})
    _assert_helper(helper)
    return Plan(assignments, ops, warnings, r.stdout, notes=(
        "Read-only preview calculated by Druta from nvtune fields and dump. "
        "Additional checks inside nvtune may still refuse Apply."))


def check(assignments, field_table, snapshot=None):
    """Refusals this module makes on its own, before nvtune is consulted.

    Returns [] when the write may be attempted, else a list of reasons."""
    problems = []
    for name, value in assignments.items():
        f = field_table.by_name(name) if field_table else None
        if f is None:
            problems.append(f"{name}: not a field this nvtune build knows")
            continue
        if f.structural:
            problems.append(f"{name}: {STRUCTURAL_BLOCKED}")
        if not (0 <= int(value) <= f.max_value):
            problems.append(f"{name}: {value} outside the field's "
                            f"0..{f.max_value} range")
        if f.inferred:
            problems.append(f"{name}: lives in {f.register}, whose offset is "
                            f"INFERRED rather than observed - writing it means "
                            f"writing an address we have not confirmed")
    if snapshot is not None:
        if not timings.known_timing_layout(getattr(snapshot, "codename", None)):
            problems.append("nvtune has no confirmed timing layout for this chip; raw register captures cannot authorize writes")
        if not getattr(snapshot, "perf_band", False):
            problems.append(
                "the card is not in its top memory band. Timings are selected per "
                "band, so a write here edits the band the card is in NOW, which is "
                "not the one you are tuning.")
    return problems


def apply(assignments, slot, force=False, override=None, *, before_commit=None):
    """Commit, then classify each field by what ACTUALLY happened.

    A read-only preview runs first. The helper is pinned throughout preparation
    and commit, so changing the configured path cannot change its target or
    command convention mid-operation. An optional before_commit() guard returns (ok, reason) and
    runs after preparation, immediately before the writing subprocess.
    """
    names = list(assignments)
    if not names:
        return Plan({}, [], [], "", ok=True), []
    # Checked here rather than left to _run's raise: every other exit from this
    # function is a (Plan, [Result]) pair, and tw_apply() unpacks it without a
    # try, so raising would surface as a dead button instead of a refusal.
    if not slot:
        p = Plan(assignments, [], [], "", ok=False,
                 error="no PCI slot for the selected card")
        return p, [Result(n, None, assignments[n], None, FAILED,
                          "no PCI slot for the selected card - refusing, "
                          "because an un-targeted nvtune write reaches every "
                          "card in the machine") for n in names]
    try:
        helper = _helper(override)
        before = read_fields(names, slot, helper.exe)
    except (OSError, subprocess.SubprocessError, WriteError) as e:
        error = f"pre-write read failed; nothing committed: {e}"
        return Plan(assignments, [], [], "", ok=False, error=error), [
            Result(n, None, assignments[n], None, FAILED, error) for n in names]
    missing = [n for n in names if n not in before]
    if missing:
        error = "pre-write values unavailable; nothing committed: " + ", ".join(missing)
        return Plan(assignments, [], [], "", ok=False, error=error), [
            Result(n, before.get(n), assignments[n], None, FAILED, error) for n in names]

    pre = plan(assignments, slot, helper.exe, helper=helper)
    if not pre.ok:
        return pre, [Result(n, before.get(n), assignments[n], before.get(n),
                            FAILED, pre.error) for n in names]
    if pre.needs_force and not force:
        return pre, [Result(n, before.get(n), assignments[n], before.get(n),
                            TOOL_REFUSED,
                            "the preview has warnings outstanding and force was not "
                            "given, so nothing was sent to the card")
                     for n in names]

    args = (["set"] + [f"{k}={v}" for k, v in assignments.items()]
            + ["--commit"] + (["--force"] if force else []))
    if before_commit is not None:
        try:
            ok, reason = before_commit()
        except Exception as e:
            return pre, [Result(n, before[n], assignments[n], None, FAILED,
                                f"pre-commit guard failed; nothing committed: {e}")
                         for n in names]
        if not ok:
            return pre, [Result(n, before[n], assignments[n], None, TOOL_REFUSED,
                                reason or "pre-commit guard refused; nothing committed")
                         for n in names]
    try:
        out, rc = _run(args, helper.exe, slot=slot, helper=helper)
    except (OSError, subprocess.SubprocessError, WriteError) as e:
        return pre, [Result(n, before[n], assignments[n], None, FAILED,
                            f"commit failed; current state is unconfirmed: {e}") for n in names]

    read_error = ""
    try:
        after = read_fields(names, slot, helper.exe)
    except (OSError, subprocess.SubprocessError, WriteError) as e:
        after, read_error = {}, str(e)
    # A process can fail before touching BAR0, or after only some writes. Keep
    # any actual read-back, but do not classify either case as hardware refusal
    # (or a successful write) merely because values happen to match.
    # Older upstream helpers refuse warned registers individually and continue
    # with the rest. Refusal text alone cannot prove the whole batch untouched.
    refused = bool(_REFUSE_RE.search(out))
    if (refused and not read_error and all(after.get(n) == before[n] for n in names)
            and "applied and verified" not in out and "readback mismatch" not in out):
        return pre, [Result(n, before[n], assignments[n], after[n], TOOL_REFUSED,
                            "nvtune refused the commit; readback is unchanged") for n in names]
    if rc != 0 or refused:
        detail = f"nvtune commit exited {rc}: {out or 'no error text'}"
        if read_error:
            detail += f"; read-back failed: {read_error}"
        return pre, [Result(n, before[n], assignments[n], after.get(n), FAILED, detail)
                     for n in names]
    results = []
    for n in names:
        want = int(assignments[n])
        got = after.get(n)
        if got is None:
            results.append(Result(n, before[n], want, None, FAILED,
                                  "post-write read-back unavailable"
                                  + (f": {read_error}" if read_error else "")))
        elif got == want:
            results.append(Result(n, before.get(n), want, got, LANDED))
        elif got == before.get(n):
            results.append(Result(n, before.get(n), want, got, DROPPED,
                                  "the write reached the hardware and the "
                                  "register did not change"))
        else:
            results.append(Result(n, before.get(n), want, got, FAILED,
                                  "read back a third value - the register is "
                                  "being driven by something else"))
    return pre, results


# ---- backups --------------------------------------------------------------- #
def _slot_of(gpu):
    """The PCI slot of the card `gpu` drives, or "" if it cannot say."""
    try:
        return (gpu.slot() if gpu is not None else "") or ""
    except Exception:
        return ""


def card_backup_path(gpu):
    """A backup path keyed by the CARD - specifically by its UUID.

    nvtune's own default is `<slot>.stock.json`, and `ensure_stock_backup()`
    only checks whether that file EXISTS. Swap cards in one slot and the first
    write silently skips taking a backup because the previous card's file is
    sitting there - then `restore` correctly refuses it on a boot0 mismatch, and
    there is no way back. Found live: a TU102 backup was occupying the Titan
    Xp's path.

    Name and VBIOS fixed that while one card was supported at a time, but two
    IDENTICAL cards in one host share both, and the second card's write would
    find the first's backup at its path and skip taking one - the same hole,
    reopened by a multi-card rig.

    The UUID closes it without reopening the original. The slot deliberately
    stays OUT of the key: keying on position would orphan a backup the moment
    the card moved slots, and this machine has already done exactly that (the
    Titan Xp's stock file records slot 0000:01:00.0 and the card now answers at
    0000:02:00.0). The UUID is the silicon, wherever it is plugged in. VBIOS
    stays in because a reflash can change what "stock" means."""
    base = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
                        "nvtune")
    st = getattr(gpu, "static", {}) or {}
    # Falls back to the slot only when NVML gave no UUID, where a
    # position-keyed name still beats letting two cards share one file.
    ident = st.get("uuid") or _slot_of(gpu)
    tag = re.sub(r"[^A-Za-z0-9]+", "-",
                 f"{st.get('name', 'gpu')}-{st.get('vbios', '')}"
                 f"-{ident}").strip("-")
    return os.path.join(base, f"{tag}.stock.json")


def legacy_backup_path(gpu):
    """Where a stock backup taken BEFORE the slot joined the key would be.

    Not a compatibility nicety. If this were ignored, a card whose stock
    backup already exists under the old name would look un-backed-up, and
    ensure_backup() would happily take a fresh "stock" snapshot of a card that
    is currently TUNED - overwriting nothing, but recording modified timings
    under the name the restore path trusts. The real stock values would still
    be on disk, one filename away, with nothing pointing at them."""
    base = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
                        "nvtune")
    st = getattr(gpu, "static", {}) or {}
    tag = re.sub(r"[^A-Za-z0-9]+", "-",
                 f"{st.get('name', 'gpu')}-{st.get('vbios', '')}").strip("-")
    return os.path.join(base, f"{tag}.stock.json")


def existing_backup_path(gpu):
    """The stock backup this card already has, or "" - new name first.

    The legacy file is accepted only if its boot0 does not contradict this card.
    That is a weak check and is meant as one: the old filename already encodes
    name and VBIOS, so a file sitting there is for this MODEL, and boot0 is a
    chip identifier that two identical cards also share. Neither can separate
    two same-model cards - which is the collision the slot was added to break,
    and it is broken only for backups taken from here on. gpu.static carries no
    boot0 today, so in practice this accepts on the filename."""
    new = card_backup_path(gpu)
    if os.path.exists(new):
        return new
    old = legacy_backup_path(gpu)
    if old != new and os.path.exists(old):
        _code, boot0 = backup_describes(old)
        live = (getattr(gpu, "static", {}) or {}).get("boot0")
        if not live or not boot0 or str(boot0).lower() == str(live).lower():
            return old
    return ""


def ensure_backup(gpu, override=None):
    """(path, made, err). Takes a card-specific stock snapshot if absent.

    The slot comes from `gpu`, the same object card_backup_path() names the file
    after, so the contents and the filename cannot disagree about which card
    they describe. Without -d this was the worst bug in the module: `nvtune save
    -o P` with two cards saves the FIRST card to P, then fails the second with
    "cannot replace" - so the file named for the Titan Xp would hold the Titan
    RTX's registers, `restore` would correctly refuse it on the boot0 mismatch,
    and the card would have no way back. That is precisely the hazard this
    function's card-keyed path was introduced to close, reopened one layer
    down."""
    held = existing_backup_path(gpu)
    if held:
        return held, False, ""
    path = card_backup_path(gpu)
    slot = _slot_of(gpu)
    if not slot:
        return path, False, ("the GPU object could not name its PCI slot, so a "
                             "stock backup would capture whichever card nvtune "
                             "picked")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        out, rc = _run(["save", "-o", path], override, slot=slot)
        if rc != 0 or not os.path.exists(path):
            return path, False, out or f"nvtune save exited {rc}"
    except (OSError, subprocess.SubprocessError, WriteError) as e:
        return path, False, str(e)
    return path, True, ""


def restore(path, slot, override=None):
    """Write a snapshot back. nvtune validates boot0 and refuses a file taken
    on a different chip, which is the one guard we are relying on rather than
    reimplementing - but that guard only helps if the restore is aimed at one
    card. Aimed at all of them, the matching card is restored and the others
    each refuse, which reads as a partial failure of a successful operation."""
    if not os.path.exists(path):
        return False, f"no backup at {path}"
    try:
        out, rc = _run(["restore", "-i", path, "--commit"], override, slot=slot)
    except (OSError, subprocess.SubprocessError, WriteError) as e:
        return False, str(e)
    return rc == 0, out


def backup_describes(path):
    """(codename, boot0) of a backup file, so the UI can name what it would
    restore instead of offering an opaque path."""
    try:
        with open(path, encoding="utf-8") as f:
            j = json.load(f)
        return j.get("codename", "?"), j.get("boot0", "?")
    except (OSError, ValueError):
        return None, None
