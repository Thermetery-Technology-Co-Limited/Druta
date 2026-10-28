# Owner-reported FurMark A/B observation: TU104 extended power limit

On 2026-10-28, the owner reported a FurMark A/B test on the local Gigabyte
AORUS Xtreme RTX 2080 SUPER (TU104). The board is shunt-modified and has the
previously documented imperfect 16 GB VRAM modification. This report records
the owner's observations supplied after the
[581.42 idle qualification](turing-tu104-live-power-20261028.md); no independent
load capture or calibrated input-power measurement accompanied the report.

| Condition | Owner-reported behavior |
| --- | --- |
| Before extending the power slider | Approximately 140% TDP with frequent power throttling, consistent with the original 140% ordinary power limit |
| After extending the power slider | Reliably reached more than 150% TDP under FurMark |
| Session interruption | Owner reports that the increased demand tripped the 750 W PSU's over-power protection (OPP) |

The increase beyond the previous approximately 140% ceiling supports the
owner's conclusion that the extended power slider changed operative power
limiting on this board under load. This complements the earlier software
request, readback and restoration checks. Those earlier trials were performed
at idle and did not themselves measure physical enforcement.

The owner estimates that the interruption involved a transient above 900 W.
That figure is an estimate, not a measured GPU input-power value. The PSU's
750 W rating and the reported OPP event do not supply a calibrated transient
measurement. The percentages above are the owner's reported TDP readings;
with the board's shunt modification they must not be converted directly into
actual board watts using the 250 W rated software value.

This evidence applies to this modified board and the reported workload. It
does not establish a universal Turing power ceiling, four-digit request
enforcement, behavior on other drivers or boards, or a measured value for
the physical power limit. The exact extended request, synchronized throttle
telemetry and measurement equipment were not recorded in the supplied
account. Runtime comparison of 431.60 and 621.01 is separate work; this report
makes no qualification claim for either driver.
