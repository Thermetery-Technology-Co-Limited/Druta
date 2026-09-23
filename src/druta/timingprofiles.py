# Druta - a monitor and tuner for NVIDIA GPUs.
# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Portable, read-only memory-timing profile documents.

This module deliberately has no nvtune process or GPU dependency.  A caller
may load a profile here and put its assignments into the timing editor, but
the normal preview/apply path remains responsible for every write guard.
"""
import json
from pathlib import Path

from . import timings
from .startup import atomic_json


SCHEMA = "druta.timing-profile"
VERSION = 1
NVTUNE_FORMAT = "nvtune-profile-1"


class TimingProfileError(ValueError):
    """A profile cannot safely be saved or staged."""


def _duplicate_key(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise TimingProfileError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _field_index(field_table):
    if field_table is None:
        raise TimingProfileError("nvtune field metadata is unavailable")
    exact, folded = {}, {}
    for field in getattr(field_table, "fields", ()):
        name = getattr(field, "name", None)
        if not isinstance(name, str) or not name:
            raise TimingProfileError("nvtune field metadata has an unnamed field")
        try:
            geometry_ok = field.width_consistent
        except (AttributeError, TypeError, ValueError):
            geometry_ok = False
        if not geometry_ok:
            raise TimingProfileError(f"{name}: nvtune field metadata has inconsistent bit geometry")
        key = name.casefold()
        if name in exact or key in folded:
            raise TimingProfileError(f"nvtune field metadata has duplicate/case-conflicting field {name!r}")
        exact[name] = field
        folded[key] = name
    if not exact:
        raise TimingProfileError("nvtune field metadata contains no fields")
    return exact, folded


def _aliases(field_table, exact, folded):
    aliases = {}
    for alias, target in getattr(field_table, "aliases", {}).items():
        if not isinstance(alias, str) or not isinstance(target, str):
            raise TimingProfileError("nvtune field metadata has an invalid alias")
        if alias.casefold() in folded or alias.casefold() in aliases:
            raise TimingProfileError(f"nvtune field metadata has duplicate/case-conflicting alias {alias!r}")
        if target not in exact:
            raise TimingProfileError(f"nvtune alias {alias!r} points to unknown field {target!r}")
        aliases[alias.casefold()] = (alias, target)
    return aliases


def validate_fields(values, field_table):
    """Canonicalize one nvtune ``fields`` mapping without relaxing input."""
    if not isinstance(values, dict):
        raise TimingProfileError("profile 'fields' must be a JSON object")
    exact, folded = _field_index(field_table)
    aliases = _aliases(field_table, exact, folded)
    out, sources = {}, {}
    for name, value in values.items():
        if not isinstance(name, str) or not name:
            raise TimingProfileError("profile field names must be non-empty strings")
        key = name.casefold()
        if key in folded:
            canonical = folded[key]
            if name != canonical:
                raise TimingProfileError(
                    f"{name}: case does not match nvtune field {canonical}; profile field names are case-sensitive")
        elif key in aliases:
            alias, canonical = aliases[key]
            # The alias is a spelling emitted by nvtune, not a case-insensitive
            # convenience: accepting a case variant would hide a typo.
            if name != alias:
                raise TimingProfileError(
                    f"{name}: case does not match nvtune alias {alias}; profile field names are case-sensitive")
        else:
            raise TimingProfileError(f"{name}: not a field this nvtune build knows")
        if type(value) is not int:
            raise TimingProfileError(f"{name}: timing values must be integers (not booleans or decimals)")
        field = exact[canonical]
        if field.structural:
            raise TimingProfileError(f"{name}: structural timing fields cannot be staged")
        if field.inferred:
            raise TimingProfileError(
                f"{name}: lives in {field.register}, whose offset is inferred and cannot be staged")
        if not 0 <= value <= field.max_value:
            raise TimingProfileError(f"{name}: {value} outside the field's 0..{field.max_value} range")
        if canonical in out:
            prior = sources[canonical]
            if out[canonical] != value:
                raise TimingProfileError(f"{name} conflicts with alias {prior} for {canonical}")
            raise TimingProfileError(f"{name} duplicates {prior} for {canonical}")
        out[canonical], sources[canonical] = value, name
    return dict(sorted(out.items()))


def _upstream_fields(values, field_table):
    if not isinstance(values, dict):
        raise TimingProfileError("nvtune profile 'fields' must be a JSON object")
    for name in values:
        if not isinstance(name, str) or name != name.upper():
            raise TimingProfileError("nvtune profile field names must be uppercase")
    return validate_fields(values, field_table)


def _nonempty(fields):
    if not fields:
        raise TimingProfileError("timing profile has no fields to stage")
    return fields


def _capture(snapshot, staged):
    return {
        "source": "nvtune decoded broadcast capture",
        "gpu": {
            "codename": snapshot.codename,
            "pci_id": snapshot.pci_id,
            "slot": snapshot.slot,
            "boot0": snapshot.boot0,
            "chipset": snapshot.chipset,
            "aperture": snapshot.aperture,
        },
        "band": {
            "memory_mhz_reported": snapshot.mem_nvml,
            "performance_band": snapshot.perf_band,
            "pstate": snapshot.pstate,
            "matched_memory_state_mhz": snapshot.matched_state,
        },
        "pending_modifications": sorted(staged),
    }


def make_profile(snapshot, field_table, staged=None):
    """Build a profile from one safe decoded broadcast capture and edits.

    The capture must have a confirmed decoder and be in the performance band.
    A partition disagreement is a hard error rather than a broadcast guess.
    """
    if snapshot is None or not getattr(snapshot, "ok", False):
        raise TimingProfileError("a successful timing capture is required to save a profile")
    if not timings.known_timing_layout(getattr(snapshot, "codename", None)):
        raise TimingProfileError("raw register snapshots have no confirmed timing layout; capture decoded timings first")
    if getattr(snapshot, "perf_band", None) is not True:
        raise TimingProfileError("capture is not in the top memory band; capture performance timings before saving")
    staged = {} if staged is None else staged
    staged = validate_fields(staged, field_table)
    exact, _folded = _field_index(field_table)
    registers = getattr(snapshot, "registers", None)
    scopes = getattr(snapshot, "scopes", None)
    if not isinstance(registers, dict) or not isinstance(registers.get("broadcast"), dict):
        raise TimingProfileError("capture has no broadcast raw registers to verify decoded fields")
    if not isinstance(scopes, list) or not scopes:
        raise TimingProfileError("capture has no framebuffer partition scopes to verify a broadcast profile")
    broadcast = registers["broadcast"]
    captured = {}
    seen = set()
    for reading in getattr(snapshot, "readings", ()):
        field = getattr(reading, "field", None)
        name = getattr(field, "name", None)
        if name not in exact or field != exact[name]:
            raise TimingProfileError("capture and nvtune field metadata do not describe the same fields")
        if name in seen:
            raise TimingProfileError(f"capture has duplicate decoded field {name!r}")
        seen.add(name)
        if field.structural or field.inferred:
            continue
        try:
            word = broadcast[field.register]
        except (KeyError, TypeError):
            raise TimingProfileError(f"{name}: broadcast capture is missing {field.register}") from None
        if type(word) is not int:
            raise TimingProfileError(f"{name}: broadcast register {field.register} is not an integer word")
        value = field.extract(word)
        if getattr(reading, "cycles", None) != value:
            raise TimingProfileError(f"{name}: decoded value does not match its broadcast register")
        for scope in scopes:
            words = registers.get(scope)
            if not isinstance(words, dict) or field.register not in words:
                raise TimingProfileError(f"{name}: partition {scope!r} is missing {field.register}")
            partition_word = words[field.register]
            if type(partition_word) is not int:
                raise TimingProfileError(f"{name}: partition {scope!r} has a non-integer {field.register}")
            if field.extract(partition_word) != value:
                raise TimingProfileError(
                    f"{name} differs across framebuffer partitions; refusing to export a broadcast profile")
        captured[name] = value
    missing = sorted(name for name, field in exact.items()
                     if not field.structural and not field.inferred and name not in seen)
    fabricated = sorted(set(staged) & set(missing))
    if fabricated:
        raise TimingProfileError(
            "staged fields were not decoded in this capture: " + ", ".join(fabricated))
    if missing:
        raise TimingProfileError(
            "capture is missing decoded values for writable fields: " + ", ".join(missing))
    # Validate the capture by the same contract used for user-supplied fields;
    # this also rejects values that exceed a changed runtime field definition.
    captured = validate_fields(captured, field_table)
    fields = dict(captured)
    fields.update(staged)
    if not fields:
        raise TimingProfileError("timing profile has no writable decoded fields")
    return {"schema": SCHEMA, "version": VERSION,
            "capture": _capture(snapshot, staged), "fields": dict(sorted(fields.items()))}


def save(path, snapshot, field_table, staged=None):
    """Atomically persist a native timing profile and return its document."""
    document = make_profile(snapshot, field_table, staged)
    try:
        atomic_json(path, document)
    except OSError as error:
        raise TimingProfileError(f"could not save timing profile {path}: {error}") from error
    return document


def read_profile(path, field_table):
    """Read and validate a document, preserving capture metadata for the UI."""
    source = Path(path)
    try:
        with source.open("r", encoding="utf-8") as stream:
            document = json.load(stream, object_pairs_hook=_duplicate_key)
    except TimingProfileError:
        raise
    except (OSError, json.JSONDecodeError) as error:
        raise TimingProfileError(f"could not read timing profile {source}: {error}") from error
    if not isinstance(document, dict):
        raise TimingProfileError("timing profile must be a JSON object")
    if "registers" in document:
        raise TimingProfileError(
            "this is a raw nvtune register snapshot, not an apply profile; raw registers cannot be broadcast as timing writes")
    if "fbpa" in document:
        raise TimingProfileError(
            "this profile contains fbpa-scoped fields; Druta only stages broadcast timing profiles")
    if "fields" not in document:
        raise TimingProfileError("timing profile has no 'fields' object")
    if "slot" in document and (not isinstance(document["slot"], str) or not document["slot"]):
        raise TimingProfileError("timing profile has no valid PCI slot")
    if "_format" in document:
        if document.get("_format") != NVTUNE_FORMAT:
            raise TimingProfileError(f"unsupported nvtune profile format {document.get('_format')!r}")
        if not isinstance(document.get("name"), str) or not document["name"]:
            raise TimingProfileError("nvtune profile has no valid name")
        if not isinstance(document.get("slot"), str) or not document["slot"]:
            raise TimingProfileError("nvtune profile has no valid PCI slot")
        fields = _nonempty(_upstream_fields(document["fields"], field_table))
        return {"schema": NVTUNE_FORMAT, "version": 1, "_format": NVTUNE_FORMAT,
                "name": document["name"], "slot": document["slot"],
                "capture": {"source": "nvtune profile", "gpu": {"slot": document["slot"]}},
                "fields": fields}
    native = "schema" in document
    if native:
        if document.get("schema") != SCHEMA:
            raise TimingProfileError(f"unsupported timing profile schema {document.get('schema')!r}")
        if type(document.get("version")) is not int or document["version"] != VERSION:
            raise TimingProfileError(f"unsupported timing profile version {document.get('version')!r}")
        if not isinstance(document.get("capture"), dict):
            raise TimingProfileError("native timing profile has no valid capture metadata")
        result = {"schema": SCHEMA, "version": VERSION,
                  "capture": document["capture"],
                  "fields": _nonempty(validate_fields(document["fields"], field_table))}
    else:
        # nvtune-compatible exchange shape: just a flat fields mapping.
        result = {"schema": "nvtune-fields", "version": None, "capture": {},
                  "fields": _nonempty(validate_fields(document["fields"], field_table))}
    if "slot" in document:
        result["slot"] = document["slot"]
    return result


def load(path, field_table):
    """Return canonical assignments for staging through the existing UI."""
    return read_profile(path, field_table)["fields"]
