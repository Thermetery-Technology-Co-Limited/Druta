# GTX 745 DDR3 timing query and write verification

Driver 472.12; GM107 at P0, memory 900 MHz. All 33 fields queried; 16 fields accepted one-cycle increases. Every timing register matched the original at completion. 270 checked 64 MiB transfers completed without mismatches or CUDA errors.

Readback establishes register response, not necessarily a change in effective DDR3 behavior. CL, WL, preambles and WRCRC were queried only; no CAS-latency write was attempted.

| Field | Register | Original | Request → readback | Result |
|---|---|---:|---|---|
| RC | CONFIG0 | 45 | 46 → 46 | Landed; restored |
| RFC | CONFIG0 | 237 | 238 → 238 | Landed; restored |
| RAS | CONFIG0 | 32 | 33 → 33 | Landed; restored |
| RP | CONFIG0 | 13 | 14 → 14 | Landed; restored |
| CL | CONFIG1 | 13 | — | query-only: latency, preamble or protocol field |
| WL | CONFIG1 | 9 | — | query-only: latency, preamble or protocol field |
| RD_RCD | CONFIG1 | 13 | 14 → 14 | Landed; restored |
| WR_RCD | CONFIG1 | 13 | 14 → 14 | Landed; restored |
| RPRE | CONFIG2 | 1 | — | query-only: latency, preamble or protocol field |
| WPRE | CONFIG2 | 1 | — | query-only: latency, preamble or protocol field |
| CDLR | CONFIG2 | 7 | 8 → 8 | Landed; restored |
| WR | CONFIG2 | 14 | 15 → 15 | Landed; restored |
| W2R_BUS | CONFIG2 | 4 | 5 → 5 | Landed; restored |
| R2W_BUS | CONFIG2 | 4 | 5 → 5 | Landed; restored |
| PDEX | CONFIG3 | 22 | 23 → 23 | Landed; restored |
| PDEN2PDEX | CONFIG3 | 5 | 6 → 6 | Landed; restored |
| FAW | CONFIG3 | 38 | 39 → 39 | Landed; restored |
| AOND | CONFIG3 | 0 | — | query-only: nvtune warning |
| CCDL | CONFIG3 | 0 | 1 → 1 | Landed; restored |
| CCDS | CONFIG3 | 0 | 1 → 1 | Landed; restored |
| REFRESH_LO | CONFIG4 | 0 | — | query-only: structural or split field |
| REFRESH | CONFIG4 | 13 | — | query-only: structural or split field |
| RRD | CONFIG4 | 7 | 8 → 8 | Landed; restored |
| DELAY0 | CONFIG4 | 54 | — | query-only: structural or split field |
| ADR_MIN | CONFIG5 | 0 | — | query-only: structural or split field |
| WRCRC | CONFIG5 | 0 | — | query-only: latency, preamble or protocol field |
| OFFSET0 | CONFIG5 | 39 | — | query-only: structural or split field |
| DELAY0_MSB | CONFIG5 | 0 | — | query-only: structural or split field |
| OFFSET1 | CONFIG5 | 0 | — | query-only: structural or split field |
| OFFSET2 | CONFIG5 | 0 | — | query-only: structural or split field |
| DELAY0_HI | CONFIG5 | 0 | — | query-only: structural or split field |
| RFCSBA | TIMING22 | 8 | — | query-only: inferred register location |
| RFCSBR | TIMING22 | 0 | — | query-only: inferred register location |

CCDL and CCDS originally read zero. nvtune permits 0 → 1 but warns when restoring 1 → 0. The sweep stopped at CCDL; a P-state transition restored it, verified against the entire original register set under a fresh load. CCDS used a narrowly scoped `--force` restoration of its captured original zero after verifying the current register image. Both partitions and the broadcast register matched at completion.

[Machine-readable evidence](maxwell-gtx745-timing-sweep-47212.json).
