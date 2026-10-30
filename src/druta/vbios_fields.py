# Druta - bounded inspection and planning of driver VBIOS overrides.
# Copyright (C) 2026 Thermetery Technology Co Limited
# Format research and adapted decoding rules: NVIDIA VBIOS Vix Editor,
# Copyright (C) 2026 panchovix. Modified for bounded runtime captures.
# SPDX-License-Identifier: GPL-3.0-or-later
"""Typed edits to the image returned by NvAPI_GPU_GetVbiosImage.

No hardware or registry access occurs here. This is a *runtime image* parser,
not a flash image editor: the final PCI/NPDS archive can be cut short by NVAPI,
but every selected header, object, table and field must be present. In-memory
romOverride patches do not repair archive hashes or fabricate missing bytes.

Layout research: Panchovix/nvidia-vbios-vix-editor, GPL-3.0-or-later,
b0921c9d83046ee140a082e692be5ba3cc9da465, rom/blackwell.py, tables.py,
structure.py. NVIDIA nvgpu bios.h corroborates older table prefixes. This
module deliberately does not import its board-ID eligibility rules or assume
that an encoded value establishes a hardware limit. Runtime effectiveness is
the caller's separate readback obligation.
"""

from __future__ import annotations

import hashlib
import math
import struct
from collections.abc import Mapping

SOURCE = "https://github.com/Panchovix/nvidia-vbios-vix-editor/tree/b0921c9d83046ee140a082e692be5ba3cc9da465"
SCHEMA_VERSION = 1
# Decoded from the V4x row-class switch, not correlated from one board's rows.
# Evidence records the parser, descriptor, jump targets and common object copy.
POWER_V40_RUNTIME_TYPES = {
    1: 0,
    2: 3,
    3: 4,
    4: 27,
    5: 28,
    6: 17,
    7: 18,
    8: 19,
    9: 20,
    10: 31,
    11: 30,
    12: 33,
    13: 32,
    14: 34,
    15: 36,
    16: 35,
}
POWER_CLASS_EVIDENCE = "experiments/registry-overrides/vbios-fields/power-class-mapping.json"
POWER_V30_CLASS_EVIDENCE = "experiments/power-v30-class1-20261028.json"
# V30 rail type 1 / 65-byte rows: only these words are copied as VFE roots by
# the matched rail constructor. Other row bytes remain opaque and unchanged.
VOLTAGE_RAIL_VFE_SELECTORS = (13, 15, 17, 19, 33, 35)
# These independently decoded bytes have an unresolved consumer namespace.
# Treat values that name active VFE rows as possible dependencies.
VOLTAGE_RAIL_POSSIBLE_VFE_BYTES = (21, 23)


def power_runtime_type(version, kind):
    """Return only independently traced BIOS-to-runtime policy classes."""
    if version == 0x40:
        return POWER_V40_RUNTIME_TYPES.get(kind)
    if version == 0x30 and kind == 1:
        return 0
    return None


class FormatError(ValueError):
    """The selected operation does not have a bounded, understood layout."""


def _sha(blob):
    return hashlib.sha256(blob).hexdigest()


class _Image:
    def __init__(self, blob):
        if not isinstance(blob, (bytes, bytearray, memoryview)):
            raise TypeError("VBIOS must be bytes")
        self.b = bytes(blob)
        if not 0x1A <= len(self.b) <= 16 * 1024 * 1024:
            raise FormatError("VBIOS capture size is outside the bounded parser")
        self.fields, self.tables, self.diagnostics, self.constraints = [], [], [], []
        self.images = self._images()
        legacy = [p for p in self.images if p["type"] == 0 and p["vendor"] == 0x10DE]
        if len(legacy) != 1 or legacy[0]["start"] != 0 or not legacy[0]["complete"]:
            raise FormatError("expected one complete NVIDIA legacy image at offset zero")
        self.legacy = legacy[0]
        next_image = [p for p in self.images if p["start"] == self.legacy["end"]]
        self.efi = next_image[0] if len(next_image) == 1 and next_image[0]["type"] == 3 else None
        if self.efi and (
            not self.efi["complete"]
            or (self.efi["vendor"], self.efi["device"]) not in ((0, 0), (0x10DE, 0), (0x10DE, self.legacy["device"]))
        ):
            raise FormatError("inconsistent or truncated EFI image")
        self.tokens = self._bit()
        token = self.tokens.get("i")
        if not token or token["version"] != 2 or token["size"] < 5:
            raise FormatError("no understood BIT i version selector")
        at = token["offset"]
        self.selector = {"version": self.u("I", at), "oem": self.b[at + 4]}
        self.selector["hex"] = self.selector["version"].to_bytes(4, "big").hex() + f"{self.selector['oem']:02x}"
        self.objects = self._objects()
        self.family = "blackwell" if self.objects else "legacy-tables"

    def span(self, offset, size):
        if (
            not isinstance(offset, int)
            or not isinstance(size, int)
            or offset < 0
            or size < 0
            or offset + size > len(self.b)
        ):
            raise FormatError(f"capture does not contain {offset:#x}+{size}")
        return self.b[offset : offset + size]

    def u(self, fmt, at):
        return struct.unpack("<" + fmt, self.span(at, struct.calcsize("<" + fmt)))[0]

    def _images(self):
        images = []
        for start in range(0, len(self.b) - 26 + 1, 512):
            signature = self.b[start : start + 2]
            if signature not in (b"\x55\xaa", b"VN"):
                continue
            at = start + self.u("H", start + 24)
            magic = b"PCIR" if signature == b"\x55\xaa" else b"NPDS"
            if self.b[at : at + 4] != magic:
                continue
            self.span(at, 24)
            size, hs = self.u("H", at + 16) * 512, self.u("H", at + 10)
            if at < start + 26 or hs < 24 or size <= 0 or at + hs > start + size:
                raise FormatError("invalid PCI/NPDS header geometry")
            self.span(at, hs)
            images.append(
                {
                    "start": start,
                    "end": start + size,
                    "size": size,
                    "header": at,
                    "vendor": self.u("H", at + 4),
                    "device": self.u("H", at + 6),
                    "type": self.b[at + 20],
                    "last": bool(self.b[at + 21] & 128),
                    "complete": start + size <= len(self.b),
                    "magic": magic.decode(),
                }
            )
        for a, b in zip(images, images[1:], strict=False):
            if a["end"] > b["start"]:
                raise FormatError("overlapping PCI/NPDS images")
        return images

    def _bit(self):
        found, cursor = [], 0
        end = self.legacy["end"]
        while True:
            at = self.b.find(b"\xff\xb8BIT\x00", cursor, end)
            if at < 0:
                break
            self.span(at, 12)
            hs, ts, count = self.b[at + 8 : at + 11]
            if hs < 12 or ts < 6 or not count or at + hs + ts * count > end:
                raise FormatError("invalid BIT dimensions")
            if sum(self.span(at, hs)) & 255:
                raise FormatError("BIT header checksum mismatch")
            tokens = {}
            for i in range(count):
                tid, version, size, ptr = struct.unpack("<BBHH", self.span(at + hs + ts * i, 6))
                if chr(tid) in tokens or ptr + size > end:
                    raise FormatError("duplicate or out-of-bounds BIT token")
                tokens[chr(tid)] = {"version": version, "size": size, "offset": ptr}
            found.append(tokens)
            cursor = at + 1
        if len(found) != 1:
            raise FormatError(f"expected one BIT directory, found {len(found)}")
        return found[0]

    def _objects(self):
        # A v3 directory belongs to signed metadata after EFI; never infer a
        # Blackwell directory from an arbitrary signature in payload bytes.
        if not self.efi:
            return []
        meta = [p for p in self.images if p["start"] == self.efi["end"] and p["type"] == 0xE0 and p["magic"] == "PCIR"]
        if not meta:
            return []
        if len(meta) != 1 or meta[0]["vendor"] != 0x10DE:
            raise FormatError("inconsistent metadata image")
        meta = meta[0]
        candidates, cursor = [], meta["start"]
        marker = struct.pack("<II", 3, 16)
        while True:
            at = self.b.find(marker, cursor, meta["header"])
            if at < 0:
                break
            cursor = at + 1
            count, device = self.u("I", at + 8), self.u("I", at + 12)
            if not 2 <= count <= 1024 or device != self.legacy["device"]:
                continue
            base = at + 16 + 60 * count
            if base > meta["header"]:
                continue
            entries = []
            for i in range(count):
                h = at + 16 + 60 * i
                oid, offset, size = struct.unpack("<III", self.span(h, 12))
                data = base + offset
                # Some archive objects lie past the runtime capture. They are
                # described, but cannot own a writable field unless present.
                if not size or size > 16 * 1024 * 1024 or data + size > max(p["end"] for p in self.images):
                    break
                entries.append(
                    {
                        "id": oid,
                        "offset": data,
                        "size": size,
                        "hash_offset": h + 12,
                        "captured": data + size <= len(self.b),
                    }
                )
            if (
                len(entries) != count
                or [e["id"] for e in entries[:2]] != [0xFB, 0xBE]
                or entries[1]["offset"] != meta["header"]
            ):
                continue
            if len({e["id"] for e in entries}) != count:
                raise FormatError("duplicate object IDs")
            candidates.append(entries)
        if len(candidates) != 1:
            raise FormatError("no unique bounded v3 metadata object directory")
        return candidates[0]

    def pointer(self, token, offset):
        t = self.tokens.get(token)
        if not t or t["version"] != 2 or offset + 4 > t["size"]:
            raise FormatError(f"BIT {token}+{offset:#x} is absent or unrecognized")
        ptr = self.u("I", t["offset"] + offset)
        if not ptr:
            raise FormatError(f"BIT {token}+{offset:#x} is not populated")
        if ptr >= self.legacy["size"] and self.efi:
            ptr += self.efi["size"]
        self.span(ptr, 4)
        if self.efi and self.efi["start"] <= ptr < self.efi["end"]:
            raise FormatError("table pointer addresses EFI code")
        return ptr

    def own(self, offset, size, expected=None):
        self.span(offset, size)
        if self.objects and offset >= self.legacy["end"]:
            owners = [o for o in self.objects if o["offset"] <= offset and offset + size <= o["offset"] + o["size"]]
            if len(owners) != 1 or (expected is not None and owners[0]["id"] != expected):
                raise FormatError(f"field/table lacks its expected object owner {expected}")
            return owners[0]["id"]
        owners = [p for p in self.images if p["type"] != 3 and p["start"] <= offset and offset + size <= p["end"]]
        if len(owners) != 1:
            raise FormatError("table crosses image ownership boundary")
        return None

    def table(self, name, token, ptr, shapes, owner=None):
        at = self.pointer(token, ptr)
        version, hs, stride, count = self.span(at, 4)
        if (version, hs, stride) not in shapes or not count or count > 255:
            raise FormatError(f"unrecognized {name} table geometry")
        size = hs + stride * count
        oid = self.own(at, size, owner)
        t = {
            "name": name,
            "offset": at,
            "size": size,
            "version": version,
            "header_size": hs,
            "stride": stride,
            "count": count,
            "object_id": oid,
        }
        self.tables.append(t)
        return t

    def field(
        self,
        key,
        label,
        group,
        at,
        fmt,
        unit,
        *,
        minimum=None,
        maximum=None,
        scale=1,
        mask=None,
        editable=True,
        reason="",
        meta=None,
        offsets=None,
        require_equal=True,
    ):
        n = struct.calcsize("<" + fmt)
        offsets = list(offsets or [at])
        values = [self.u(fmt, a) for a in offsets]
        raw = values[0] if require_equal else self.u(fmt, at)
        if require_equal and any(
            (v & mask if mask is not None else v) != (raw & mask if mask is not None else raw) for v in values
        ):
            editable, reason = False, "compound field encodings disagree"
        if isinstance(raw, float) and not math.isfinite(raw):
            editable, reason, current = False, "nonfinite encoding", None
        else:
            current = bool(raw & mask) if mask is not None else raw / scale
            if isinstance(current, float) and current.is_integer():
                current = int(current)
        if minimum is None:
            minimum = 0 if fmt.isupper() else (-2147483648 if fmt == "i" else -32768 if fmt == "h" else 0)
        if maximum is None:
            maximum = {"B": 255, "H": 65535, "I": 4294967295, "h": 32767, "i": 2147483647, "f": 3.402823466e38}[
                fmt
            ] / scale
        self.fields.append(
            dict(
                key=key,
                label=label,
                group=group,
                units=unit,
                current=current,
                minimum=minimum,
                maximum=maximum,
                capability="editable" if editable else "read-only",
                reason=reason,
                offsets=offsets,
                width=n,
                encoding=fmt,
                scale=scale,
                mask=mask,
                raw=raw if not isinstance(raw, float) or math.isfinite(raw) else None,
                raw_values=[v if not isinstance(v, float) or math.isfinite(v) else None for v in values],
                evidence={"layout": SOURCE, "runtime_effect": "unverified"},
                **(meta or {}),
            )
        )

    def optional(self, group, fn):
        before = len(self.fields)
        try:
            fn()
        except (FormatError, struct.error, IndexError) as exc:
            # Independent capabilities: failure here must not hide good fields
            # from another table. Partially decoded fields in this group are
            # retained read-only so a late relationship failure cannot leak a
            # write capability.
            for field in self.fields[before:]:
                field.update(capability="read-only", reason=str(exc))
            self.diagnostics.append({"group": group, "message": str(exc)})

    def power(self):
        shapes = (
            ((0x40, 0x38, 0x66), (0x40, 0x3D, 0x66))
            if self.objects
            else ((0x30, 0x32, 0x53), (0x30, 0x34, 0x5B), (0x30, 0x2C, 0x4B), (0x30, 0x2C, 0x47))
        )
        t = self.table("power", "P", 0x2C, shapes, 0x13)
        modern = t["version"] == 0x40
        for i in range(t["count"]):
            row = t["offset"] + t["header_size"] + i * t["stride"]
            kind = self.b[row] if modern else (self.b[row] & 15) | ((self.b[row + 0x42] & 15) << 4)
            inspection_only = not modern and t["stride"] == 0x47 and kind != 1
            if not kind:
                continue
            # The common flags word has limit units in bit 0; other bits
            # describe policy behavior (e.g. fixed and coupled policies) and
            # must not be mistaken for an enum or overwritten.
            discriminator = (self.u("I", row + 1) & 1) if modern else (self.b[row] >> 4) & 1
            unit = {0: "mW", 1: "mA"}.get(discriminator, "raw")
            base = row + (13 if modern else 2)
            values = [self.u("I", base + k * 4) for k in range(3)]
            valid = discriminator in (0, 1) and kind in (1, 2, 3, 6, 7, 9, 11, 12, 13) and values == sorted(values)
            reason = "" if valid else "unknown units, class, or inconsistent limits"
            if inspection_only:
                reason = "short v30 power class is qualified for read-only inspection only"
            meta = {
                "policy": i,
                "policy_type": kind,
                "channel": self.b[row + (5 if modern else 1)],
                "range_group": f"power.{i}",
                "runtime_binding": {
                    "policy_index": i,
                    "vbios_type": kind,
                    "channel": self.b[row + (5 if modern else 1)],
                    "units": unit,
                    "table_version": t["version"],
                    "runtime_type": power_runtime_type(t["version"], kind),
                    "class_evidence": POWER_CLASS_EVIDENCE
                    if modern
                    else POWER_V30_CLASS_EVIDENCE
                    if kind == 1
                    else None,
                    "required": "compare PCI-paired runtime policy index, class translation, channel, units and all three limits",
                },
            }
            for j, role in enumerate(("minimum", "default", "maximum")):
                self.field(
                    f"power.{i}.{role}",
                    f"Policy {i} {role}",
                    "Power / current",
                    base + j * 4,
                    "I",
                    unit,
                    maximum=2147483647,
                    editable=valid and not inspection_only,
                    reason=reason,
                    meta=meta,
                )
            # Type13 +33 is a WORD relationship selector. Resolve the kind1
            # relationship to its current policy; the two ordinals need not
            # match. +61 stores the separate linked current limit.
            if modern and kind == 13:
                relationship = self.u("H", row + 33)
                target, relation, relation_at = None, None, None
                recognized = False
                try:
                    relation_stride, relation_count = self.span(t["offset"] + 4, 2)
                    if relation_stride == 18 and relationship < relation_count:
                        relation_base = t["offset"] + t["size"]
                        self.own(t["offset"], t["size"] + relation_stride * relation_count, 0x13)
                        relation_at = relation_base + relationship * relation_stride
                        data = self.span(relation_at, relation_stride)
                        target = data[1]
                        relation = {
                            "kind": data[0] & 15,
                            "stride": relation_stride,
                            "count": relation_count,
                            "offset": relation_at,
                            "target_policy": target,
                            "parameter_u16": self.u("H", relation_at + 2),
                        }
                        recognized = discriminator == 0 and relation["kind"] == 1 and target < t["count"]
                except FormatError:
                    pass  # This optional link must not remove other power fields.
                if recognized:
                    other = t["offset"] + t["header_size"] + target * t["stride"]
                    related = [self.u("I", other + 13 + j * 4) for j in range(3)]
                    recognized = (
                        self.b[other] == 11
                        and self.u("I", other + 1) & 1 == 1
                        and related == sorted(related)
                        and related[0] <= self.u("I", row + 61) <= related[-1]
                    )
                self.field(
                    f"power.{i}.linked_current",
                    f"Policy {i} linked policy {target} current" if recognized else f"Policy {i} linked current",
                    "Power / current",
                    row + 61,
                    "I",
                    "mA",
                    minimum=1,
                    maximum=2147483647,
                    editable=recognized,
                    reason="" if recognized else "linked-current relationship or target has not been identified",
                    meta={
                        "policy": i,
                        "linked_relationship": relationship,
                        "linked_relationship_offset": relation_at,
                        "relationship_binding": relation,
                        "linked_policy": target,
                    },
                )

    def clocks(self):
        at = self.pointer("C", 8)
        ver, hs, es, count, subcount, subsize = self.span(at, 6)
        modern = ver == 0x50 and (hs, es, count, subcount) == (13, 15, 9, 3) and subsize in (1, 2)
        ada = self.span(at, 9) == bytes.fromhex("40 0d 0f 0a 03 01 03 e8 03")
        if not (modern or ada):
            raise FormatError("unrecognized clock-range layout")
        stride = es + subsize * subcount
        self.own(at, hs + stride * count, 3)
        names = (
            ("GPC", "XBAR", "DRAM", "SYS", "VID", "domain5", "domain6", "domain7", "domain8")
            if modern
            else ("GPC", "XBAR", "DRAM", "SYS", "HUB", "VID", "PWR", "DISP", "PCIEGEN", "HOST")
        )
        descriptors = (0x43, 0x84, 0x182, 0xC5, 0x106, 0x147, 0x8, 0x200, 0x1C1)
        for i in range(count):
            row = at + hs + stride * i
            known = (
                self.b[row] == 2
                and (self.u("H", row + 9) == descriptors[i] or i == 2 and self.u("H", row + 9) == 0x1182)
                if modern
                else self.b[row] == 1
            )
            lo, hi = self.u("h", row + 3), self.u("h", row + 5)
            for role, delta in (("minimum", 3), ("maximum", 5)):
                self.field(
                    f"clock.{i}.{role}",
                    f"{names[i]} offset {role}",
                    "Clock offsets",
                    row + delta,
                    "h",
                    "MHz",
                    editable=known and lo <= hi,
                    reason="" if known and lo <= hi else "unrecognized domain or offset bounds",
                    meta={
                        "range_group": f"clock.{i}",
                        "domain": names[i],
                        "clock_table_version": ver,
                        **(
                            {
                                "clock_binding": {
                                    "schema": 1,
                                    "table_version": ver,
                                    "bios_row_type": self.b[row],
                                    "bios_index": i,
                                    "bios_hal_selector": self.b[at + 6],
                                    "table_offset": at,
                                    "header_size": hs,
                                    "entry_size": es,
                                    "entry_count": count,
                                    "subentry_count": subcount,
                                    "subentry_size": subsize,
                                }
                            }
                            if modern
                            else {}
                        ),
                    },
                )

    def factory(self):
        at = self.pointer("P", 0x98)
        if self.span(at, 6) != bytes.fromhex("10 07 05 02 05 02") or self.b[at + 6] not in (0, 1):
            raise FormatError("unrecognized factory clock layout")
        self.own(at, 27, 0x26)
        clocks = {
            f.get("domain"): f for f in self.fields if f["key"].startswith("clock.") and f["capability"] == "editable"
        }
        for i in range(4):
            row = at + 7 + 5 * i
            known = self.b[row] in (0, 1) and i < 2 and ("GPC", "DRAM")[i] in clocks
            self.field(
                f"factory.{i}.enabled",
                f"Factory clock record {i} enabled",
                "Factory clocks",
                row,
                "B",
                "bool",
                mask=1,
                editable=False,
                reason="flag is managed with the corresponding effective offset or rated-clock override",
            )
            self.field(
                f"factory.{i}.value",
                ("Core factory offset", "Memory factory offset", "Stored base clock", "Stored boost clock")[i],
                "Factory clocks",
                row + 1,
                "i" if i < 2 else "I",
                "kHz",
                editable=False,
                reason="use the corresponding whole-MHz offset or rated-clock override",
            )
            if i < 2:
                khz = self.u("i", row + 1) if self.b[row] == 1 else 0
                self.field(
                    f"factory.{i}.offset",
                    ("Core factory offset", "Memory factory offset")[i],
                    "Factory clocks",
                    row + 1,
                    "i",
                    "MHz",
                    scale=1000,
                    minimum=min(khz / 1000, (-500, -5000)[i]),
                    maximum=max(khz / 1000, (500, 5000)[i]),
                    editable=known and khz % 1000 == 0,
                    reason="" if known and khz % 1000 == 0 else "unrecognized domain or fractional-MHz stored offset",
                    meta={
                        "factory_flag_offset": row,
                        "factory_effective": True,
                        "factory_binding": {
                            "schema": 1,
                            "table_offset": at,
                            "header_hex": bytes(self.b[at : at + 7]).hex(),
                            "record_index": i,
                            "record_offset": row,
                            "domain": ("GPC", "DRAM")[i],
                            "clock_binding": clocks.get(("GPC", "DRAM")[i], {}).get("clock_binding"),
                        },
                    },
                )
                self.fields[-1]["current"] = khz // 1000 if khz % 1000 == 0 else khz / 1000
            else:
                enabled = self.b[row] == 1
                khz = self.u("I", row + 1) if enabled else 0
                known = (
                    self.b[row] in (0, 1)
                    and "GPC" in clocks
                    and khz % 1000 == 0
                    and (not enabled or 1 <= khz // 1000 <= 65535)
                )
                self.field(
                    f"factory.{i}.rated",
                    ("Base clock override", "Boost clock override")[i - 2],
                    "Factory clocks",
                    row + 1,
                    "I",
                    "MHz",
                    scale=1000,
                    minimum=1,
                    maximum=65535,
                    editable=known,
                    reason="" if known else "unrecognized GPC binding, flag or unsigned whole-MHz frequency",
                    meta={
                        "factory_flag_offset": row,
                        "factory_rated": True,
                        "factory_enabled": enabled,
                        "factory_binding": {
                            "schema": 1,
                            "table_offset": at,
                            "header_hex": bytes(self.b[at : at + 7]).hex(),
                            "record_index": i,
                            "record_offset": row,
                            "domain": "GPC",
                            "clock_binding": clocks.get("GPC", {}).get("clock_binding"),
                        },
                        "detail": "0 means this stored override is inactive. Enter a positive whole-MHz frequency to enable it. "
                        "Restore removes your saved changes. This changes rated clock metadata; achieved clocks depend on the GPU's running policies.",
                    },
                )
                self.fields[-1]["current"] = khz // 1000 if khz % 1000 == 0 else khz / 1000

    def thermal(self):
        shapes = ((0x20, 12, 48), (0x20, 22, 48)) if self.objects else ((0x10, 11, 40),)
        t = self.table("thermal", "P", 0x50, shapes, 0x1B)
        operating = 0 if self.objects else self.b[t["offset"] + 9]
        for i in range(t["count"]):
            row = t["offset"] + t["header_size"] + t["stride"] * i
            kind = self.b[row]
            for j in range(3):
                self.field(
                    f"thermal.{i}.temperature{j}",
                    f"Thermal policy {i} threshold {j}",
                    "Thermal",
                    row + 2 + j * 2,
                    "h",
                    "C",
                    scale=32,
                    minimum=-1024,
                    maximum=1023.96875,
                    editable=False,
                    reason="raw thresholds are inspection only; the identified operating policy changes all three together",
                    meta={"policy_type": kind, "channel": self.b[row + 1]},
                )
            temps = [self.u("h", row + 2 + j * 2) for j in range(3)]
            if i == operating:
                known = (
                    kind == 1 and (not self.objects or self.b[row + 1] == 1) and len(set(temps)) == 1 and temps[0] > 0
                )
                current = temps[0] / 32
                self.field(
                    "thermal.operating.maximum",
                    "Maximum operating temperature",
                    "Thermal",
                    row + 2,
                    "h",
                    "C",
                    scale=32,
                    offsets=[row + 2, row + 4, row + 6],
                    minimum=min(40, current),
                    maximum=current,
                    editable=known,
                    reason="raising the operating limit needs an independently established upper bound"
                    if known
                    else "operating thermal policy shape is unrecognized",
                    meta={"policy": i, "raising_supported": False},
                )

    def perf(self):
        shapes = ((0x10, 6, 57), (0x10, 14, 57)) if self.objects else ((0x10, 6, 49),)
        t = self.table("perf_cf_controllers", "P", 0xA8, shapes, 0x60)
        policies = self.table("perf_cf_policies", "P", 0xAC, ((0x10, 5, 10),), 0x61)
        if self.b[policies["offset"] + 4]:
            raise FormatError("unrecognized PERF_CF policy flags")
        p0 = policies["offset"] + 5
        members = self.u("I", p0 + 6)
        policy_ok = self.b[p0] & 15 == 1 and members & 7 == 7 and not members >> t["count"]
        domain_map = (0, 2, 4) if self.objects else (0, 2, 5)
        for i in range(t["count"]):
            row = t["offset"] + t["header_size"] + t["stride"] * i
            kind = self.b[row] & 15
            target, jump = self.u("I", row + 3), self.u("I", row + 7)
            known = (
                policy_ok
                and i < 3
                and kind in (1, 5)
                and self.b[row + 19] == domain_map[i]
                and 0 < target < jump <= 4096
                and self.b[row + 20] > 0
                and self.b[row + 21] > 0
            )
            for name, delta, fmt, scale, unit in (
                ("target", 3, "I", 4096, "fraction"),
                ("jump", 7, "I", 4096, "fraction"),
                ("hysteresis_up", 20, "B", 1, "samples"),
                ("hysteresis_down", 21, "B", 1, "samples"),
            ):
                self.field(
                    f"perf_cf.{i}.{name}",
                    f"Controller {i} {name.replace('_', ' ')}",
                    "Clock controllers",
                    row + delta,
                    fmt,
                    unit,
                    scale=scale,
                    minimum=1 / 4096 if scale == 4096 else 1,
                    maximum=1 if scale == 4096 else 255,
                    editable=known,
                    reason="" if known else "controller/policy/domain or utilization relationships unrecognized",
                    meta={
                        "controller_type": kind,
                        "domain_code": self.b[row + 19],
                        "depends_on": "perf_cf_policy.0.enabled",
                    },
                )
            if known:
                self.constraints.append({"kind": "perf_cf", "controller": i})
        t = policies
        for i in range(t["count"]):
            row = t["offset"] + 5 + 10 * i
            self.field(
                f"perf_cf_policy.{i}.enabled",
                f"Controller policy {i} enabled",
                "Clock controllers",
                row,
                "B",
                "bool",
                mask=128,
                editable=i == 0 and policy_ok,
                reason="" if i == 0 and policy_ok else "only the identified utilization policy is writable",
            )

    def lpwr(self):
        tables = {
            "gr_pg": (0x80, 0x39, 10, 5, "I"),
            "ms": (0x84, 0x3A, 7, 16, "I"),
            "gr_rg": (0xD4, 0x7E, 12, 1, "B"),
            "difr": (0xE8, 0x8E, 11, 18, "I"),
            "nvdec": (0x11C, 0xB4, 10, 2, "H"),
            "nvenc": (0x120, 0xB5, 10, 2, "H"),
            "ofa": (0x124, 0xB6, 10, 2, "H"),
            "nvjpg": (0x128, 0xB7, 10, 2, "H"),
        }
        if not self.objects:
            tables = {
                "gr_pg": (0x80, None, 10, 5, "I"),
                "ms": (0x84, None, 7, 15, "I"),
                "gr_rg": (0xD4, None, 9, 1, "B"),
                "difr": (0xE8, None, 11, 17, "I"),
            }
        for name, (ptr, oid, hs, es, fmt) in tables.items():

            def add(name=name, ptr=ptr, oid=oid, hs=hs, es=es, fmt=fmt):
                t = self.table("lpwr_" + name, "P", ptr, ((0x10, hs, es),), oid)
                enabled_mask = 2 if name == "nvjpg" else 1
                for i in range(t["count"]):
                    at = t["offset"] + hs + es * i
                    enabled = bool(self.u(fmt, at) & enabled_mask)
                    self.field(
                        f"lpwr.{name}.{i}.enabled",
                        f"{name.upper()} entry {i} enabled",
                        "Idle power management",
                        at,
                        fmt,
                        "bool",
                        mask=enabled_mask,
                        editable=enabled,
                        reason="may disable this captured enabled entry; restore it from the transaction journal"
                        if enabled
                        else "disabled entry: captured bytes cannot prove it is supported when enabled",
                        meta={"disable_only": True},
                    )

            self.optional("lpwr_" + name, add)

    def fans(self):
        modern = bool(self.objects)
        shapes = {
            "cooler": (0x58, 0x1D, 9 if modern else 6, 26, 4 if modern else 2),
            "policy": (0x5C, 0x1E, 4, 51, 11 if modern else 8),
            "mapping": (0x64, 0x20, 4, 5, 4 if modern else 2),
            "control": (0xDC, 0x80, 4, 13, 4 if modern else 2),
            "speed": (0xE0, 0x83, 4, 6, 4 if modern else 2),
        }
        tables = {}
        for name, (ptr, owner, hs, es, count) in shapes.items():
            t = self.table("fan_" + name, "P", ptr, ((0x20 if name == "policy" else 0x10, hs, es),), owner)
            if t["count"] != count:
                raise FormatError("fan table channel counts disagree")
            tables[name] = t

        def row(name, i):
            t = tables[name]
            return t["offset"] + t["header_size"] + t["stride"] * i

        count = shapes["cooler"][4]
        cooler = tables["cooler"]
        role_count = 4 if cooler["header_size"] >= 8 else 2
        client_roles = {
            str(role + 1): (
                self.b[cooler["offset"] + 4 + role]
                if role < role_count and self.b[cooler["offset"] + 4 + role] != 255
                else None
            )
            for role in range(4)
        }
        active_rows = []
        for i in range(count):
            c, p, m, ctl, s = (row(n, i) for n in ("cooler", "policy", "mapping", "control", "speed"))
            active = bool(self.b[c])
            if any(bool(self.b[a]) != active for a in (p, m, ctl, s)):
                raise FormatError("fan enable state disagrees across related tables")
            if not active:
                continue
            active_rows.append(i)
            if (
                self.b[p + 1] != 1
                or self.b[m + 1] != i
                or self.b[ctl + 2] != i
                or self.b[ctl + 5] != (0x30 | 1 << i)
                or self.b[s + 1] != i
            ):
                raise FormatError("fan identity differs across related tables")
            for role, delta, fmt, unit, maxi in (
                ("level_minimum", 2, "B", "%", 100),
                ("level_maximum", 3, "B", "%", 100),
                ("rpm_minimum", 14, "H", "RPM", 32767),
                ("rpm_maximum", 16, "H", "RPM", 32767),
            ):
                self.field(
                    f"fan.{i}.{role}",
                    f"Fan {i} {role.replace('_', ' ')}",
                    "Fans",
                    c + delta,
                    fmt,
                    unit,
                    maximum=maxi,
                    meta={
                        "range_group": f"fan.{i}." + role.split("_")[0],
                        "client_id_to_index": client_roles.copy(),
                    },
                )
            # The high byte of the 24-bit PWM value is retained. The vetted
            # corpus uses 16-bit Hz; other shapes are inspectable only.
            self.field(
                f"fan.{i}.pwm_hz",
                f"Fan {i} PWM frequency",
                "Fans",
                c + 11,
                "H",
                "Hz",
                minimum=21000,
                maximum=28000,
                editable=self.b[c + 13] == 0,
                reason="" if self.b[c + 13] == 0 else "PWM frequency exceeds known encoding",
                meta={"client_id_to_index": client_roles.copy()},
            )
            self.constraints.append({"kind": "fan_speed", "channel": i, "speed": self.u("H", s + 2)})
        if not active_rows:
            raise FormatError("no active GPU-driven fan channel was identified")
        entries = []
        for i in range(tables["policy"]["count"]):
            p = row("policy", i)
            if not self.b[p] or (i < count and i not in active_rows):
                continue
            owners = [i] if i < count else active_rows
            entries.append((i, p))
            self.constraints.append({"kind": "fan_curve", "policy": i, "channels": owners})
            for j in range(3):
                self.field(
                    f"fan_curve.{i}.{j}.level",
                    f"Fan curve {i} point {j} level",
                    "Fan curves",
                    p + 14 + j,
                    "B",
                    "%",
                    maximum=100,
                )
                self.field(
                    f"fan_curve.{i}.{j}.temperature",
                    f"Fan curve {i} point {j} temperature",
                    "Fan curves",
                    p + 18 + 4 * j,
                    "H",
                    "C",
                    scale=32,
                    maximum=110,
                )
                self.field(
                    f"fan_curve.{i}.{j}.rpm",
                    f"Fan curve {i} point {j} speed",
                    "Fan curves",
                    p + 20 + 4 * j,
                    "H",
                    "RPM",
                    maximum=32767,
                )
            low, high = self.u("H", p + 30), self.u("H", p + 32)
            stop = 0 < low < high <= 110 * 32 and self.b[p + 46] != 255
            for name, delta in (("low", 30), ("high", 32)):
                self.field(
                    f"fan_stop.{i}.{name}",
                    f"Fan policy {i} zero RPM {name} temperature",
                    "Fan curves",
                    p + delta,
                    "H",
                    "C",
                    scale=32,
                    minimum=1 / 32,
                    maximum=110,
                    editable=stop and (modern or self.b[p + 50] & 6 == 6),
                    reason="" if stop else "no recognized fan-stop parameter pair",
                )
            self.constraints.append({"kind": "fan_stop", "policy": i})
        if modern and entries:
            masks = {self.b[p + 50] & 6 for _, p in entries}
            known = (
                len(masks) == 1
                and masks <= {0, 6}
                and all(
                    0 < self.u("H", p + 30) < self.u("H", p + 32) <= 110 * 32 and self.b[p + 46] != 255
                    for _, p in entries
                )
            )
            self.field(
                "fan.zero_rpm",
                "Fan zero RPM",
                "Fans",
                entries[0][1] + 50,
                "B",
                "bool",
                mask=6,
                offsets=[p + 50 for _, p in entries],
                meta={"policy_indices": [i for i, _ in entries]},
                editable=known,
                reason="" if known else "fan policies disagree or lack recognized stop parameters",
            )
        # Never offer writable fan fields on already inconsistent tables.
        _validate_constraints(
            self.constraints,
            {f["key"]: f["current"] for f in self.fields},
            {f["key"]: f for f in self.fields},
            fan=True,
        )
        # Publish only after the entire related-table group is understood.
        # Runtime verification binds policies to their actual controller routes.
        self.fan_policy_binding = {
            "version": tables["policy"]["version"],
            "policies": [
                {
                    "index": i,
                    "selector_raw": self.b[p + 1],
                    "flags": self.b[p + 50],
                    "thermal_offset_q8": self.u("h", p + 47) * 8,
                    "stop_word_44": self.u("H", p + 44),
                    "stop_selector_46": self.b[p + 46],
                }
                for i, p in entries
                if self.b[p] == 1
            ],
            "routes": [
                {
                    "index": i,
                    "kind": int(self.b[row("control", i) + 1] == 0),
                    "cooler_index": self.b[row("control", i) + 2],
                    "word": self.u("H", row("control", i) + 3),
                    "policy_mask_a": self.u("I", row("control", i) + 5),
                    "policy_mask_b": self.u("I", row("control", i) + 9),
                }
                for i in range(tables["control"]["count"])
                if self.b[row("control", i)] == 1
            ],
            "client_id_to_index": client_roles,
        }

    def voltage_range(self, base, count, index, seen=frozenset()):
        """Conservative bounds of a complete voltage list, including additions."""
        low = high = 0.0
        while index != 65535:
            if index >= count or index in seen or len(seen) > 512:
                return None
            seen = seen | {index}
            e = self.span(base + 25 * index, 25)
            lo, hi, c0, c1, c2 = struct.unpack("<5f", e[3:23])
            if e[23] & 15 != 2 or not all(math.isfinite(x) for x in (lo, hi)) or lo > hi:
                return None
            if e[0] in (1, 7):
                constant = e[0] == 1 and c1 == c2 == 0 and math.isfinite(c0)
                a, z = (c0, c0) if constant else (-math.inf, math.inf)
            elif e[0] in (2, 3):
                ks = (11, 12) if e[0] == 2 else (15, 16)
                children = [self.voltage_range(base, count, e[k] | e[k + 4] << 8, seen) for k in ks]
                if any(c is None for c in children):
                    return None
                a, z = min(c[0] for c in children), max(c[1] for c in children)
            else:
                return None
            a, z = max(lo, min(hi, a)), min(hi, max(lo, z))
            if not all(math.isfinite(x) and abs(x) < 1e30 for x in (a, z)):
                return None
            low, high = low + a, high + z
            index = e[2] | e[24] << 8
        return low, high

    def voltage_graph(self, vfe, base, count):
        """All equation edges, including multivariate variable/coefficient terms.

        The v30 loader expands five-byte term records to eight-byte runtime
        descriptors. A type-3 term references an equation, including when used
        as a polynomial coefficient. Ignoring those edges falsely reports an
        equation as unshared. Unknown or incomplete graphs grant no writes.
        Evidence: experiments/registry-overrides/vbios-fields/voltage-equation-*
        (loader 8E6820, parser 8E6CC0, term validators C30AC0/C30D60).
        """
        rows = [self.span(base + 25 * i, 25) for i in range(count)]
        refs, edges = {}, [[] for _ in rows]
        terms, term_count = base + 25 * count, 0
        if any(e[0] == 7 for e in rows):
            if self.b[vfe + 11] != 5:
                raise FormatError("unrecognized VFE polynomial term stride")
            term_count = self.u("H", vfe + 12)
            if not term_count:
                raise FormatError("VFE polynomial term table is absent")
            self.own(vfe, terms + 5 * term_count - vfe, 0x14)
        for i, e in enumerate(rows):
            if e[0] not in (0, 1, 2, 3, 4, 7):
                raise FormatError("unrecognized VFE equation kind in sharing graph")
            if not e[0]:
                continue
            links = [e[2] | e[24] << 8]
            if e[0] in (2, 3):
                ks = (11, 12) if e[0] == 2 else (15, 16)
                links += [e[k] | e[k + 4] << 8 for k in ks]
            if e[0] == 7:
                first = struct.unpack_from("<H", e, 11)[0]
                variables = e[13] & 15
                coefficients = {1: 3, 2: 6, 3: 10}.get(variables)
                if coefficients is None or first + variables + coefficients > term_count:
                    raise FormatError("VFE polynomial terms exceed understood geometry")
                for j in range(first, first + variables + coefficients):
                    at = terms + 5 * j
                    kind = self.b[at]
                    if kind == 1:
                        if not math.isfinite(self.u("f", at + 1)):
                            raise FormatError("nonfinite VFE polynomial literal")
                    elif kind == 2:
                        if self.b[at + 1] >= self.b[vfe + 3]:
                            raise FormatError("VFE polynomial variable exceeds its table")
                    elif kind == 3:
                        target = self.u("H", at + 1)
                        if target == 65535:
                            raise FormatError("VFE polynomial equation term is absent")
                        links.append(target)
                    else:
                        raise FormatError("unrecognized active VFE polynomial term kind")
            for target in links:
                if target == 65535:
                    continue
                if target >= count or not rows[target][0]:
                    raise FormatError("VFE equation link does not identify an active equation")
                refs.setdefault(target, []).append(i)
                edges[i].append(target)
        # Kahn's algorithm bounds stack use even for long valid equation lists.
        indegree = [0] * count
        for links in edges:
            for target in links:
                indegree[target] += 1
        ready = [i for i, n in enumerate(indegree) if not n]
        visited = 0
        while ready:
            i = ready.pop()
            visited += 1
            for target in edges[i]:
                indegree[target] -= 1
                if not indegree[target]:
                    ready.append(target)
        if visited != count:
            raise FormatError("cyclic VFE sharing graph")
        return {i: sorted(set(users)) for i, users in refs.items()}

    def voltage_clock_roots(self, base, count):
        """Documented C+0C main-entry roots, not every external BIOS consumer.

        C is optional. A present table must have a reviewed main-row layout and
        a fully captured extent. Type 1 is the VFE-driven clock root; the same
        selector on DRAM type 3 is counted conservatively as an external user.
        Opaque appended fields/subentries are preserved, not decoded as roots.
        """
        token = self.tokens.get("C")
        if token is None:
            return {}
        if token["version"] != 2 or token["size"] < 0x10:
            raise FormatError("external sharing: unrecognized BIT C pointer layout")
        if not self.u("I", token["offset"] + 0x0C):
            return {}
        at = self.pointer("C", 0x0C)
        version, hs, size, rows, c1, s1, c2, s2 = self.span(at, 8)
        if hs != 8 or (version, size) not in ((0x40, 15), (0x50, 23)):
            raise FormatError("external sharing: unrecognized clock programming geometry")
        if (c1 and not s1) or (c2 and not s2):
            raise FormatError("external sharing: invalid clock programming subentry geometry")
        stride = size + c1 * s1 + c2 * s2
        self.own(at, hs + stride * rows, 4)
        roots = {}
        for i in range(rows):
            row = at + hs + stride * i
            kind = self.b[row] & 15
            idx = self.u("H", row + 3) if version == 0x50 else self.b[row + 3] | self.b[row + 13] << 8
            if kind == 0 and idx == 65535:
                continue
            if kind not in (1, 3):
                raise FormatError("external sharing: unrecognized active clock programming kind")
            if idx == 65535:
                continue
            if idx >= count or not self.b[base + 25 * idx]:
                raise FormatError("external sharing: clock root does not identify an active VFE equation")
            roots.setdefault(idx, []).append({"entry": i, "kind": kind, "offset": row + 3})
        return roots

    def voltage_polynomial_terms(self, base, count, equation):
        """Typed values after voltage_graph validated the complete term table."""
        row = self.span(base + 25 * equation, 25)
        first = struct.unpack_from("<H", row, 11)[0]
        variables = row[13] & 15
        coefficients = {1: 3, 2: 6, 3: 10}[variables]
        terms = []
        for i in range(first, first + variables + coefficients):
            at = base + 25 * count + 5 * i
            kind = self.b[at]
            value = self.u({1: "I", 2: "B", 3: "H"}[kind], at + 1)
            terms.append({"type": kind, "value": value})
        return terms[:variables], terms[variables:]

    def voltage(self):
        rail = self.pointer("P", 0x68)
        vfe = self.pointer("P", 0x20)
        if self.span(rail, 3) != bytes((0x30, 5, 65)) or self.b[rail + 3] not in (1, 2):
            raise FormatError("unrecognized rail selector layout")
        count = self.b[rail + 3]
        self.own(rail, 5 + 65 * count)
        if self.span(vfe, 5) != bytes((0x30, 14, 29, 255, 25)):
            raise FormatError("unrecognized voltage equation layout")
        base = vfe + 14 + 29 * 255
        n = self.b[vfe + 5] | self.b[vfe + 10] << 8
        if not n or n > 4096:
            raise FormatError("voltage equation count outside understood layout")
        self.own(vfe, base + 25 * n - vfe, 0x14)
        graph_error = None
        try:
            refs = self.voltage_graph(vfe, base, n)
        except (FormatError, struct.error, IndexError) as exc:
            refs, graph_error = None, str(exc)
        clock_error = None
        try:
            clock_refs = self.voltage_clock_roots(base, n)
        except (FormatError, struct.error, IndexError) as exc:
            clock_refs, clock_error = None, str(exc)
        selected, rail_roots = [], []
        role_names = {13: "REL", 15: "ALT", 17: "OV", 19: "VMIN", 33: "INFO_VMIN"}
        selector_offsets = VOLTAGE_RAIL_VFE_SELECTORS
        for i in range(count):
            row = rail + 5 + 65 * i
            if self.span(row, 2) != bytes((1, i + 1)):
                raise FormatError("unrecognized rail identity")
            indices = [self.u("H", row + d) for d in selector_offsets]
            if any(idx != 65535 and (idx >= n or not self.b[base + 25 * idx]) for idx in indices):
                refs, graph_error = None, "rail selector does not identify an active VFE equation"
            selected += indices
            rail_roots += [
                {
                    "rail_index": i,
                    "rail_identity": self.b[row + 1],
                    "role": role_names.get(d, "unnamed"),
                    "selector_offset": row + d,
                    "equation": idx,
                }
                for d, idx in zip(selector_offsets, indices, strict=True)
                if idx != 65535
            ]
            for d in VOLTAGE_RAIL_POSSIBLE_VFE_BYTES:
                idx = self.b[row + d]
                if idx != 255 and idx < n and self.b[base + 25 * idx]:
                    selected.append(idx)
                    rail_roots.append({"rail_index": i, "rail_identity": self.b[row + 1],
                                       "role": "possible_byte", "selector_offset": row + d,
                                       "equation": idx})
        forward, closures = {}, {}
        for target, users in (refs or {}).items():
            for user in users:
                forward.setdefault(user, []).append(target)

        def closure(index, adjacency):
            seen, todo = set(), [index]
            while todo:
                node = todo.pop()
                if node not in seen:
                    seen.add(node)
                    todo.extend(adjacency.get(node, []))
            return sorted(seen)

        def dependencies(index):
            if index not in closures:
                closures[index] = closure(index, forward)
            return closures[index]

        for i in range(count):
            row = rail + 5 + 65 * i
            label = ("NVVDD", "MSVDD")[i]
            rel_index = self.u("H", row + 13)
            rel_range = self.voltage_range(base, n, rel_index)
            for role, d in (("REL", 13), ("ALT", 15), ("OV", 17), ("INFO_VMIN", 33), ("VMIN", 19)):
                idx = self.u("H", row + d)
                if idx == 65535:
                    continue
                if idx >= n:
                    raise FormatError("rail selector exceeds voltage equations")
                at = base + 25 * idx
                e = self.span(at, 25)
                lo, hi, c0, c1, c2 = struct.unpack("<5f", e[3:23])
                next_index = e[2] | e[24] << 8
                known = (
                    e[0] in ((1, 7) if role == "REL" else (1,))
                    and e[23] & 15 == 2
                    and all(math.isfinite(v) for v in (lo, hi))
                    and 0 <= lo <= hi
                )
                unique = (
                    refs is not None
                    and clock_refs is not None
                    and not refs.get(idx)
                    and not clock_refs.get(idx)
                    and selected.count(idx) == 1
                )
                meta = {
                    "rail": label,
                    "role": role,
                    "equation": idx,
                    "equation_type": e[0],
                    "equation_offset": at,
                    "variable_index": e[1],
                    "output_type": e[23] & 15,
                    "next_equation": next_index,
                    "shared": not unique,
                    "sharing_graph_complete": refs is not None and clock_refs is not None,
                    "sharing_graph_error": graph_error or clock_error,
                    "external_sharing_complete": clock_refs is not None,
                    "external_sharing_error": clock_error,
                    "sharing_scope": "understood VFE edges, rail selectors, possible byte aliases and C+0C main-entry roots; other BIOS consumers unverified",
                    "rel_lowest_uv": rel_range[0] if rel_range else None,
                    "referenced_by": refs.get(idx, []) if refs is not None else None,
                    "clock_referenced_by": clock_refs.get(idx, []) if clock_refs is not None else None,
                }
                if refs is not None and clock_refs is not None:
                    affected = closure(idx, refs)
                    meta.update(
                        {
                            "dependency_indices": dependencies(idx),
                            "affected_equation_indices": affected,
                            "affected_rail_roots": [
                                {**r, "dependency_indices": dependencies(r["equation"])}
                                for r in rail_roots
                                if r["equation"] in affected
                            ],
                            "affected_clock_roots": [
                                {"equation": root, **use, "dependency_indices": dependencies(root)}
                                for root, uses in clock_refs.items()
                                if root in affected
                                for use in uses
                            ],
                        }
                    )
                if role == "REL":
                    binding = None
                    if (
                        known
                        and e[0] == 7
                        and refs is not None
                        and clock_refs is not None
                        and lo == int(lo)
                        and hi == int(hi)
                        and 0 < hi <= 2147483647
                    ):
                        variables, coefficients = self.voltage_polynomial_terms(base, n, idx)
                        binding = {
                            "index": idx,
                            "lower_uv": int(lo),
                            "before_uv": int(hi),
                            "next_index": next_index,
                            "variable": e[1],
                            "output_type": e[23] & 15,
                            "variables": variables,
                            "coefficients": coefficients,
                        }
                    meta.update({"upper_clamp_binding": binding, "whole_uv": True})
                if role == "VMIN":
                    constant = known and lo == hi == c0 and c1 == c2 == 0 and next_index == 65535
                    # A polynomial's conservative lower bound is not the
                    # active rail ceiling. Valid firmware can already have a
                    # VMIN above that bound. Runtime contracts establish the
                    # current limits; this parser establishes the encoding.
                    bounded = constant and unique and rel_range is not None and 0 < c0 <= 2147483647 and c0 == int(c0)
                    self.field(
                        f"voltage.{i}.VMIN",
                        f"{label} constant VMIN",
                        "Voltage",
                        at + 3,
                        "f",
                        "uV",
                        minimum=1,
                        maximum=2147483647,
                        offsets=[at + 3, at + 7, at + 11],
                        editable=bounded,
                        reason="requires exact effective VMIN readback within the current rail limits"
                        if bounded
                        else graph_error
                        or clock_error
                        or "VMIN is not an understood unshared constant with a complete REL chain",
                        meta=meta,
                    )
                else:
                    constant = known and e[0] == 1 and lo == hi == c0 and c1 == c2 == 0
                    pinned = constant and role in ("ALT", "OV")
                    writable = (
                        known and role != "INFO_VMIN" and selected.count(idx) == 1 and refs is not None and not pinned
                    )
                    if role == "REL":
                        writable = meta["upper_clamp_binding"] is not None
                    reason = (
                        "pinned constant: use the compound ALT addend or OV constant field"
                        if pinned
                        else graph_error
                        if graph_error
                        else clock_error
                        if role == "REL" and clock_error
                        else "requires a complete type7 voltage-clamp binding"
                        if role == "REL" and not writable
                        else "equation clamp only; unchanged coefficients and chained equations can still limit the evaluated voltage"
                        if writable
                        else "INFO VMIN is not the evaluated VMIN"
                        if role == "INFO_VMIN"
                        else "unrecognized or aliased rail equation"
                    )
                    self.field(
                        f"voltage.{i}.{role}.maximum",
                        f"{label} {role} equation ceiling",
                        "Voltage",
                        at + 7,
                        "f",
                        "uV",
                        minimum=max(0, lo) if math.isfinite(lo) else 0,
                        maximum=2147483647,
                        editable=writable,
                        reason=reason,
                        meta=meta,
                    )
                    if role == "REL":
                        detail = (
                            "initial input is the stored upper bound; selecting this sets both equation head bounds "
                            "to the entered value while preserving coefficients and the next equation. Linked tails, "
                            "rail offsets and device rounding mean the final reported REL can differ"
                        )
                        self.field(
                            f"voltage.{i}.REL.value",
                            f"{label} fixed REL equation bounds",
                            "Voltage",
                            at + 7,
                            "f",
                            "uV",
                            minimum=1,
                            maximum=2147483647,
                            offsets=[at + 3, at + 7],
                            require_equal=False,
                            editable=writable,
                            reason=detail if writable else reason,
                            meta={**meta, "explicit_selection": True, "detail": detail},
                        )
                    if role in ("ALT", "OV"):
                        linked = role == "ALT" and next_index == rel_index and rel_range is not None
                        terminal = role == "OV" and next_index == 65535
                        minimum = 0 if role == "ALT" else 1
                        bounded = (
                            constant
                            and unique
                            and (linked or terminal)
                            and minimum <= c0 <= 2147483647
                            and c0 == int(c0)
                        )
                        suffix = "addend" if role == "ALT" else "constant"
                        self.field(
                            f"voltage.{i}.{role}.{suffix}",
                            f"{label} ALT constant addend" if role == "ALT" else f"{label} OV constant",
                            "Voltage",
                            at + 3,
                            "f",
                            "uV",
                            minimum=minimum,
                            maximum=2147483647,
                            offsets=[at + 3, at + 7, at + 11],
                            editable=bounded,
                            reason=(
                                "constant added to the complete REL equation; requires exact loaded-equation readback"
                                if bounded and linked
                                else "absolute terminal constant; requires exact loaded-equation readback"
                                if bounded
                                else graph_error
                                or clock_error
                                or "requires an unshared pinned constant linked to REL (ALT) or terminal (OV)"
                            ),
                            meta={**meta, "whole_uv": True, "constant_kind": suffix, "rel_equation": rel_index},
                        )

    def report(self):
        for group, fn in (
            ("power", self.power),
            ("clocks", self.clocks),
            ("factory", self.factory),
            ("thermal", self.thermal),
            ("perf_cf", self.perf),
            ("lpwr", self.lpwr),
            ("fans", self.fans),
            ("voltage", self.voltage),
        ):
            self.optional(group, fn)
        keys = [f["key"] for f in self.fields]
        if len(keys) != len(set(keys)):
            raise FormatError("duplicate field binding")
        identity = {
            "vbios_version_raw": self.selector["version"],
            "oem_revision": self.selector["oem"],
            "pci_vendor": self.legacy["vendor"],
            "pci_device": self.legacy["device"],
        }
        return {
            "schema_version": SCHEMA_VERSION,
            "byte_count": len(self.b),
            "sha256": _sha(self.b),
            "family": self.family,
            "generation": self.family,
            "identity": identity,
            "selector": self.selector,
            "images": self.images,
            "tokens": self.tokens,
            "tables": self.tables,
            "fields": self.fields,
            "constraints": self.constraints,
            "fan_policy_binding": getattr(self, "fan_policy_binding", None),
            "diagnostics": self.diagnostics,
            "source": SOURCE,
            "preservation": "only selected typed bytes; archive hashes and every other byte remain unchanged",
        }


def inspect(blob):
    """Return JSON-safe field capabilities; raise FormatError for unsafe identity."""
    return _Image(blob).report()


def _validate_constraints(constraints, values, fields, changed=None, fan=False):
    """Validate semantic relationships on a joint prospective result."""
    changed = set(changed or ())
    fan_changed = fan or any(k.startswith(("fan.", "fan_curve.", "fan_stop.")) for k in changed)
    for c in constraints:
        kind = c["kind"]
        if kind == "perf_cf" and not fan and any(k.startswith(f"perf_cf.{c['controller']}.") for k in changed):
            prefix = f"perf_cf.{c['controller']}"
            if not values["perf_cf_policy.0.enabled"]:
                raise FormatError("utilization controller changes require its policy to remain enabled")
            if not 0 < values[prefix + ".target"] < values[prefix + ".jump"] <= 1:
                raise FormatError("utilization target must remain below jump threshold, at most 100 percent")
            if min(values[prefix + ".hysteresis_up"], values[prefix + ".hysteresis_down"]) <= 0:
                raise FormatError("utilization hysteresis counts must remain nonzero")
        elif kind == "fan_speed" and fan_changed:
            prefix = f"fan.{c['channel']}"
            if not values[prefix + ".rpm_minimum"] <= c["speed"] <= values[prefix + ".rpm_maximum"]:
                raise FormatError("fan RPM limits must retain the linked speed-table value")
            if (
                not values[prefix + ".rpm_minimum"] < values[prefix + ".rpm_maximum"]
                or not values[prefix + ".level_minimum"] < values[prefix + ".level_maximum"]
            ):
                raise FormatError("fan minimum must remain strictly below maximum")
        elif kind == "fan_curve" and fan_changed:
            prefix = f"fan_curve.{c['policy']}"
            points = {
                role: [values[f"{prefix}.{j}.{role}"] for j in range(3)] for role in ("temperature", "rpm", "level")
            }
            temps = points["temperature"]
            if not 0 <= temps[0] < temps[1] < temps[2] <= 110:
                raise FormatError("fan curve temperatures must rise strictly from 0 to 110 C")
            for role in ("rpm", "level"):
                vals = points[role]
                if vals != sorted(vals):
                    raise FormatError("fan curve RPM and level values must not fall")
                for i in c["channels"]:
                    prefix = f"fan.{i}.{role}_"
                    if not values[prefix + "minimum"] <= vals[0] <= vals[-1] <= values[prefix + "maximum"]:
                        raise FormatError("fan curve must remain inside every associated channel's limits")
        elif kind == "fan_stop" and fan_changed:
            prefix = f"fan_stop.{c['policy']}"
            selected = any(k.startswith(prefix + ".") for k in changed)
            known = fields[prefix + ".low"]["capability"] == "editable"
            if (selected or known) and not 0 < values[prefix + ".low"] < values[prefix + ".high"] <= 110:
                raise FormatError("fan-stop temperature pair must rise strictly from above 0 to at most 110 C")


def plan(blob, changes):
    """Plan exact typed byte replacements without hardware/registry side effects.

    Values use each field's displayed units. Boolean fields require bool. All
    requested changes are validated together (including min/default/max); a
    bad selection fails the entire plan. Hex before/after bytes are suitable
    for a registry transaction journal. Existing unrelated bytes are retained.
    """
    if not isinstance(changes, Mapping) or not changes:
        raise ValueError("changes must be a nonempty field/value mapping")
    report = inspect(blob)
    fields = {f["key"]: f for f in report["fields"]}
    proposed = {key: f["current"] for key, f in fields.items()}
    writes = {}
    for key, value in changes.items():
        if key not in fields:
            raise ValueError(f"unknown field {key}")
        f = fields[key]
        if f["capability"] != "editable":
            raise ValueError(f"{key} is read-only: {f['reason']}")
        if f["mask"] is not None:
            if type(value) is not bool:
                raise ValueError(f"{key} requires a boolean")
            raw = (f["raw"] | f["mask"]) if value else (f["raw"] & ~f["mask"])
        else:
            if type(value) not in (int, float) or not math.isfinite(value) or not f["minimum"] <= value <= f["maximum"]:
                raise ValueError(f"{key} is outside its encoding bounds")
            raw = value * f["scale"]
            if (f.get("factory_effective") or f.get("factory_rated")) and int(value) != value:
                raise ValueError("factory clocks must be whole MHz")
            if f.get("whole_uv") and int(value) != value:
                raise ValueError("voltage constants must be whole microvolts")
            if f["encoding"] != "f":
                if int(raw) != raw:
                    raise ValueError(f"{key} cannot represent the requested precision")
                raw = int(raw)
        encoded = struct.pack("<" + f["encoding"], raw)
        decoded = struct.unpack("<" + f["encoding"], encoded)[0]
        if f["encoding"] == "f" and decoded != raw:
            raise ValueError(f"{key} cannot exactly represent the requested float32 value")
        proposed[key] = value
        if f.get("disable_only") and value and not f["current"]:
            raise ValueError("enabling an initially disabled idle-power entry is unverified")
        chunks = []
        if f.get("factory_effective"):
            if value == f["current"]:
                continue
            chunks.append((f["factory_flag_offset"], bytes((bool(value),))))
        if f.get("factory_rated"):
            if value == f["current"]:
                continue
            chunks.append((f["factory_flag_offset"], b"\x01"))
        for index, at in enumerate(f["offsets"]):
            chunk = encoded
            if f["mask"] is not None:
                old = f["raw_values"][index]
                chunk = struct.pack("<" + f["encoding"], old | f["mask"] if value else old & ~f["mask"])
            chunks.append((at, chunk))
        for at, chunk in chunks:
            for i, byte in enumerate(chunk):
                if at + i in writes and writes[at + i][0] != byte:
                    raise ValueError("conflicting overlapping selected fields")
                if at + i not in writes:
                    writes[at + i] = [byte, set()]
                writes[at + i][1].add(key)
    groups = {fields[k]["range_group"] for k in changes if "range_group" in fields[k]}
    for group in groups:
        members = [f for f in fields.values() if f.get("range_group") == group]
        ordered = []
        for role in ("minimum", "default", "maximum"):
            ordered += [proposed[f["key"]] for f in members if f["key"].endswith(role)]
        if all(v is not None for v in ordered) and ordered != sorted(ordered):
            raise ValueError(f"{group} requires minimum <= default <= maximum")
    _validate_constraints(report["constraints"], proposed, fields, changes)
    original = bytes(blob)
    result = bytearray(original)
    # Return minimal contiguous runs of actually differing bytes. This avoids
    # consuming override capacity for unchanged bytes in a typed value.
    differing = sorted(i for i, (byte, _) in writes.items() if original[i] != byte)
    edits = []
    for at in differing:
        byte, keys = writes[at]
        result[at] = byte
        if edits and edits[-1]["offset"] + len(bytes.fromhex(edits[-1]["before"])) == at:
            edit = edits[-1]
            edit["before"] += f"{original[at]:02x}"
            edit["after"] += f"{byte:02x}"
            edit["field_keys"] = sorted(set(edit["field_keys"]) | keys)
        else:
            edits.append(
                {"offset": at, "before": f"{original[at]:02x}", "after": f"{byte:02x}", "field_keys": sorted(keys)}
            )
    actual = [i for i, (a, b) in enumerate(zip(original, result, strict=True)) if a != b]
    if actual != differing or len(result) != len(original):
        raise AssertionError("unselected VBIOS bytes changed")
    after = inspect(result)
    after_fields = {f["key"]: f for f in after["fields"]}
    for key, value in changes.items():
        if key not in after_fields or after_fields[key]["current"] != value:
            raise FormatError(f"{key} did not survive independent reparse")
        if after_fields[key]["capability"] != "editable" and not fields[key].get("disable_only"):
            raise FormatError(f"{key} would invalidate its structural or semantic capability")
    if any(k.startswith("voltage.") for k in changes):
        for f in after["fields"]:
            old = fields.get(f["key"], {})
            affected = (
                f.get("rel_lowest_uv") is not None
                and old.get("rel_lowest_uv") is not None
                and f["rel_lowest_uv"] < old["rel_lowest_uv"]
            )
            if (
                affected
                and f["key"].endswith(".VMIN")
                and f["current"] is not None
                and f.get("rel_lowest_uv") is not None
                and f["current"] > f["rel_lowest_uv"]
            ):
                raise FormatError("evaluated VMIN would cross the complete REL chain's lower bound")
    return {
        "schema_version": SCHEMA_VERSION,
        "baseline_sha256": _sha(original),
        "expected_sha256": _sha(result),
        "byte_count": len(original),
        "changed_byte_count": len(differing),
        "selector": report["selector"],
        "patches": edits,
        "changes": dict(changes),
        "fields": [after_fields[k] for k in changes],
        "preservation_verified": True,
        "runtime_effect": "requires driver restart and separate exact readback",
    }
