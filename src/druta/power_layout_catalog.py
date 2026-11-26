# Druta - a monitor and tuner for NVIDIA GPUs.
# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later

"""Measured current-policy layouts shared by the Python and native backends.

After editing, run python native/tools/generate_power_layouts.py.
"""

# Keyed by the exact unmodified power GET: its command AND its packet and
# parameter sizes. The command belongs in the key because two families can
# present the same packet geometry with different command mappings.
# Read-only INFO probes distinguish layouts that also share a command.
CURRENT_LIMIT_LAYOUTS = {
    (0x20802612, 2524, 2456): {
        "sizes": {0x2080A618: 5008, 0x2080A619: 14148,
                  0x2080A61A: 2108, 0x2080E61B: 2108},
        "commands": {0x2080A618: 0x20802618, 0x2080A619: 0x20802619,
                     0x2080A61A: 0x2080261A, 0x2080E61B: 0x2080261B},
        "info": (52, 116), "status": (12, 372),
        "control": (4, 52), "status_mask_word": 0, "control_mask_word": 0,
    },
    (0x20802612, 3548, 3480): {
        "sizes": {0x2080A618: 5520, 0x2080A619: 69148,
                  0x2080A61A: 2124, 0x2080E61B: 2124},
        "commands": {0x2080A618: 0x20802618, 0x2080A619: 0x20802619,
                     0x2080A61A: 0x2080261A, 0x2080E61B: 0x2080261B},
        "info": (0x3C, 0x84), "status": (0x64, 0x828),
        "control": (0x14, 0x34), "status_mask_word": 0,
    },
    # The same packet geometry as the entry above, reached through the A612
    # command family instead, and NOT the same records: a 128-byte info
    # record in a 5392-byte block and a 2136-byte status record in a
    # 70684-byte one. Measured on a GP106 / 516.94, where every 26xx size
    # is refused. Each base and stride was solved against live descriptors.
    (0x2080A612, 3548, 3480): {
        "sizes": {0x2080A618: 5392, 0x2080A619: 70684,
                  0x2080A61A: 2124, 0x2080E61B: 2124},
        "info": (0x3C, 0x80), "status": (0x64, 0x858),
        "control": (0x14, 0x34), "status_mask_word": 0,
    },
    (0x2080A612, 11748, 11680): {
        "sizes": {0x2080A618: 8620, 0x2080A619: 172080,
                  0x2080A61A: 4432, 0x2080E61B: 4432},
        "info": (0x58, 0xE4), "status": (0x70, 0x1454),
        "control": (0x14, 0x7C),
    },
    # Shares the 8620-byte info and 4432-byte control blocks above, but NOT
    # their status block: this driver's is 1032 bytes shorter, its header
    # 8 bytes shorter and its per-policy record 32 bytes shorter. Measured
    # on a GP106 / 576.80, where 172080 is refused (RM 0x1F) and 171048 is
    # the only size accepted across a 140000..210000 sweep. The record
    # geometry was then solved from live data rather than carried over:
    # base 0x68 / stride 0x1434 is the only pair in that packet for which
    # all ten occupied policies' status type bytes match their info types
    # and every effective value falls inside its own info bounds.
    # Two blocks agreeing is not evidence that the third does.
    (0x2080A612, 11740, 11672): {
        "sizes": {0x2080A618: 8620, 0x2080A619: 171048,
                  0x2080A61A: 4432, 0x2080E61B: 4432},
        "info": (0x58, 0xE4), "status": (0x68, 0x1434),
        "control": (0x14, 0x7C),
    },
    # The same status and control blocks as the entry above, with a
    # 216-byte policy INFO record instead of 228 in an 8236-byte block.
    # Measured on the same GP106 under a different driver, where the
    # 8620- and 8632-byte info GETs are both refused (RM 0x1F). The info
    # base and the status/control geometry were each re-solved against
    # live descriptors rather than assumed from the neighbouring entry.
    (0x2080A612, 3676, 3608): {
        "sizes": {0x2080A618: 8236, 0x2080A619: 171048,
                  0x2080A61A: 4432, 0x2080E61B: 4432},
        "info": (0x58, 0xD8), "status": (0x68, 0x1434),
        "control": (0x14, 0x7C),
    },
    (0x2080A612, 54420, 54352): {
        "sizes": {0x2080A618: 20000, 0x2080A619: 396024,
                  0x2080A61A: 13840, 0x2080E61B: 13840},
        "info": (0xCC, 0xFC), "status": (0x9C, 0x1720),
        "control": (0x14, 0xC4),
    },
}
# Same power-GET size as the 610.88 layout, but a different info GET.
# nvapi64 pairs 8632/190272/6356 with commands A618/A619/A61A, and the
# 20000-byte info GET is rejected (RM 0x1F) on the measured GA104.
# Record bases were checked against every occupied policy's info type.
CURRENT_LIMIT_LAYOUT_FALLBACKS = {
    # The 26xx family can also return the compact 128-byte INFO and
    # 2136-byte STATUS records. Retain its wire commands for this geometry.
    # On the TITAN Xp the driver rejected the larger INFO and STATUS blocks.
    # All 13 occupied policies matched across INFO, CONTROL and STATUS.
    (0x20802612, 3548, 3480): ({
        **CURRENT_LIMIT_LAYOUTS[(0x20802612, 3548, 3480)],
        **CURRENT_LIMIT_LAYOUTS[(0x2080A612, 3548, 3480)],
    },),
    # The same discovery GET can also have compact INFO, STATUS and CONTROL
    # blocks. The driver's dispatcher confirms the four parameter sizes.
    # On GP107 and GA104, the live masks, all occupied record types and
    # requested/effective bounds agree at these three bases and strides.
    (0x2080A612, 3676, 3608): ({
        "sizes": {0x2080A618: 7724, 0x2080A619: 149928,
                  0x2080A61A: 4172, 0x2080E61B: 4172},
        "info": (0x58, 0xC8), "status": (0x68, 0x11A8),
        "control": (0x14, 0x74),
    },),
    (0x2080A612, 54420, 54352): ({
        "sizes": {0x2080A618: 8632, 0x2080A619: 0x2E740,
                  0x2080A61A: 0x18D4, 0x2080E61B: 0x18D4},
        "info": (0x58, 0xE4), "status": (0x80, 0x168C),
        "control": (0x14, 0xB8),
    }, {
        # The implementation DLL's A618/A619/A61A dispatch supplies these
        # sizes. Live GETs on AD103 echoed the masks and agreed on every
        # occupied policy's type; the two preceding info sizes returned
        # RM 0x1F. The accepted info GET, never a driver string, selects it.
        "sizes": {0x2080A618: 20512, 0x2080A619: 397048,
                  0x2080A61A: 13876, 0x2080E61B: 13876},
        "info": (0xCC, 0x104), "status": (0x9C, 0x1730),
        "control": (0x14, 0xC4),
    },),
}
