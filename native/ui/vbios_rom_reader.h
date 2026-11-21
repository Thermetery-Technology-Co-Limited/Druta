// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
// ROM format research adapted from NVIDIA VBIOS Vix Editor (panchovix).
#pragma once
#include "vbios_voltage_graph.h"
#include <algorithm>
#include <cstring>
#include <map>
#include <set>
#include <stdexcept>
#include <utility>

namespace druta::ui {
class VbiosRomReader {
  struct Token {
    uint8_t version;
    uint16_t size, offset;
  };
  VbiosImageRegion legacy;
  std::optional<VbiosImageRegion> efi;

protected:
  explicit VbiosRomReader(const std::vector<uint8_t> &image) : b(image) {}
  const std::vector<uint8_t> &b;
  std::map<uint8_t, Token> tokens;
  std::vector<VbiosImageRegion> image_regions;
  std::vector<VbiosObjectRegion> object_regions;
  uint32_t selector_version = 0;
  uint8_t selector_oem = 0;
  static void require(bool condition, const char *message) {
    if (!condition)
      throw std::runtime_error(message);
  }
  void read_directory() {
    require(b.size() >= 0x1a && b.size() <= vbios_voltage_max_image_bytes, "VBIOS size outside bounded parser");
    images();
    bit();
    objects();
  }

  void span(size_t at, size_t size) const {
    require(at <= b.size() && size <= b.size() - at, "Capture does not contain selected ROM extent");
  }
  uint8_t byte(size_t at) const {
    span(at, 1);
    return b[at];
  }
  uint16_t u16(size_t at) const {
    span(at, 2);
    return uint16_t(b[at] | uint16_t(b[at + 1]) << 8);
  }
  uint32_t u32(size_t at) const {
    span(at, 4);
    return uint32_t(b[at]) | uint32_t(b[at + 1]) << 8 | uint32_t(b[at + 2]) << 16 | uint32_t(b[at + 3]) << 24;
  }

private:
  bool magic(size_t at, const char *value, size_t n) const {
    return at <= b.size() && n <= b.size() - at && std::memcmp(b.data() + at, value, n) == 0;
  }
  static size_t end(const VbiosImageRegion &image) { return size_t(image.start) + image.size; }
  void images() {
    for (size_t start = 0; start + 26 <= b.size(); start += 512) {
      const bool pci = magic(start, "\x55\xaa", 2), npds = magic(start, "VN", 2);
      if (!pci && !npds)
        continue;
      const size_t at = start + u16(start + 24);
      if (!magic(at, pci ? "PCIR" : "NPDS", 4))
        continue;
      span(at, 24);
      const auto size = uint32_t(u16(at + 16)) * 512u;
      const auto header = u16(at + 10);
      require(at >= start + 26 && header >= 24 && size > 0 && at + header <= start + size,
              "Invalid PCI/NPDS header geometry");
      span(at, header);
      image_regions.push_back({static_cast<uint32_t>(start), size, static_cast<uint32_t>(at), u16(at + 4),
                               u16(at + 6), byte(at + 20), start + size <= b.size(), npds});
    }
    for (size_t i = 1; i < image_regions.size(); ++i)
      require(end(image_regions[i - 1]) <= image_regions[i].start, "Overlapping PCI/NPDS images");
    unsigned found = 0;
    for (const auto &image : image_regions)
      if (image.type == 0 && image.vendor == 0x10de) {
        legacy = image;
        ++found;
      }
    require(found == 1 && legacy.start == 0 && legacy.complete,
            "Expected one complete NVIDIA legacy image at offset zero");
    for (const auto &image : image_regions)
      if (image.start == end(legacy) && image.type == 3)
        efi = image;
    if (efi)
      require(efi->complete && ((efi->vendor == 0 && efi->device == 0) ||
                                (efi->vendor == 0x10de && (efi->device == 0 || efi->device == legacy.device))),
              "Inconsistent or truncated EFI image");
  }
  void bit() {
    unsigned found = 0;
    for (size_t at = 0; at + 6 <= end(legacy); ++at) {
      if (!magic(at,
                 "\xff\xb8"
                 "BIT\0",
                 6))
        continue;
      span(at, 12);
      const auto hs = byte(at + 8), stride = byte(at + 9), count = byte(at + 10);
      require(hs >= 12 && stride >= 6 && count && at + hs + size_t(stride) * count <= end(legacy),
              "Invalid BIT dimensions");
      unsigned checksum = 0;
      span(at, hs);
      for (size_t k = at; k < at + hs; ++k)
        checksum += byte(k);
      require((checksum & 255) == 0, "BIT header checksum mismatch");
      std::map<uint8_t, Token> candidate;
      for (unsigned i = 0; i < count; ++i) {
        const auto row = at + hs + size_t(stride) * i;
        const auto id = byte(row);
        Token token{byte(row + 1), u16(row + 2), u16(row + 4)};
        require(!candidate.count(id) && size_t(token.offset) + token.size <= end(legacy),
                "Duplicate or out-of-bounds BIT token");
        candidate.emplace(id, token);
      }
      tokens = std::move(candidate);
      ++found;
    }
    require(found == 1, "Expected one BIT directory");
    const auto i = tokens.find('i');
    require(i != tokens.end() && i->second.version == 2 && i->second.size >= 5,
            "No understood BIT i version selector");
    selector_version = u32(i->second.offset);
    selector_oem = byte(size_t(i->second.offset) + 4);
  }
  void objects() {
    if (!efi)
      return;
    std::optional<VbiosImageRegion> meta;
    for (const auto &image : image_regions)
      if (image.start == end(*efi) && image.type == 0xe0 && !image.npds) {
        require(!meta, "Inconsistent metadata image");
        meta = image;
      }
    if (!meta)
      return;
    require(meta->vendor == 0x10de, "Inconsistent metadata image");
    size_t archive_end = 0;
    for (const auto &image : image_regions)
      archive_end = std::max(archive_end, end(image));
    unsigned candidates = 0;
    for (size_t at = meta->start; at + 8 <= meta->header; ++at) {
      if (u32(at) != 3 || u32(at + 4) != 16)
        continue;
      span(at, 16);
      const auto count = u32(at + 8);
      if (count < 2 || count > 1024 || u32(at + 12) != legacy.device)
        continue;
      const auto base = at + 16 + 60u * count;
      if (base > meta->header)
        continue;
      std::vector<VbiosObjectRegion> entries;
      for (uint32_t i = 0; i < count; ++i) {
        const auto row = at + 16 + 60u * i;
        const auto size = u32(row + 8);
        const auto data = base + u32(row + 4);
        if (!size || size > vbios_voltage_max_image_bytes || data + size > archive_end)
          break;
        entries.push_back({u32(row), static_cast<uint32_t>(data), size, static_cast<uint32_t>(row + 12),
                           data + size <= b.size()});
      }
      if (entries.size() != count || entries[0].id != 0xfb || entries[1].id != 0xbe ||
          entries[1].offset != meta->header)
        continue;
      std::set<uint32_t> ids;
      for (const auto &entry : entries)
        require(ids.insert(entry.id).second, "Duplicate metadata object IDs");
      object_regions = std::move(entries);
      ++candidates;
    }
    require(candidates == 1, "No unique bounded v3 metadata object directory");
  }

protected:
  uint32_t pointer(uint8_t token, uint32_t offset) const {
    const auto it = tokens.find(token);
    require(it != tokens.end() && it->second.version == 2 && offset + 4 <= it->second.size,
            "Absent or unrecognized BIT table pointer");
    size_t ptr = u32(size_t(it->second.offset) + offset);
    require(ptr != 0, "Unpopulated BIT table pointer");
    if (ptr >= legacy.size && efi)
      ptr += efi->size;
    span(ptr, 4);
    require(!efi || ptr < efi->start || ptr >= end(*efi), "Table pointer addresses EFI code");
    return static_cast<uint32_t>(ptr);
  }
  void own(size_t at, size_t size, std::optional<uint32_t> expected = {}) const {
    span(at, size);
    unsigned owners = 0;
    uint32_t id = 0;
    if (!object_regions.empty() && at >= end(legacy)) {
      for (const auto &object : object_regions)
        if (object.offset <= at && at + size <= size_t(object.offset) + object.size) {
          ++owners;
          id = object.id;
        }
      require(owners == 1 && (!expected || id == *expected), "Table lacks its unique expected object owner");
      return;
    }
    for (const auto &image : image_regions)
      if (image.type != 3 && image.start <= at && at + size <= end(image))
        ++owners;
    require(owners == 1, "Table crosses image ownership boundary");
  }
};
} // namespace druta::ui
