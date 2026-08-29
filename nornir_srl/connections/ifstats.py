"""Interface statistics mixin – computes in/out bps from two consecutive gNMI samples."""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from .helpers import as_list, first_payload


class InterfaceStatsMixin:
    """Mixin providing interface traffic-rate statistics."""

    def get(
        self,
        paths: List[str],
        datatype: Optional[str] = "config",
        strip_mod: Optional[bool] = True,
    ) -> List[Dict[str, Any]]:
        """Placeholder method implemented in :class:`SrLinux`."""
        raise NotImplementedError

    def get_ifstats(self, interface: str = "*", interval: int = 5) -> Dict[str, Any]:
        """Return per-interface in/out bps computed from two samples *interval* seconds apart.

        Args:
            interface: Interface name filter (default ``*`` = all interfaces).
            interval: Seconds between the two gNMI samples (default 5).
        """
        path = f"/interface[name={interface}]/statistics"

        def _sample() -> tuple:
            resp = self.get(paths=[path], datatype="state")
            ts = time.monotonic()
            return resp, ts

        resp1, t1 = _sample()
        time.sleep(interval)
        resp2, t2 = _sample()

        dt = t2 - t1

        # Build lookup: interface-name -> counters for each sample
        def _parse(resp: List[Dict[str, Any]]) -> Dict[str, Dict[str, int]]:
            result: Dict[str, Dict[str, int]] = {}
            for itf in as_list(first_payload(resp).get("interface")):
                name = itf.get("name", "")
                stats = itf.get("statistics", {})
                result[name] = {
                    "in-packets": int(stats.get("in-packets", 0)),
                    "out-packets": int(stats.get("out-packets", 0)),
                    "in-octets": int(stats.get("in-octets", 0)),
                    "out-octets": int(stats.get("out-octets", 0)),
                    "in-errors": int(stats.get("in-error-packets", 0)),
                    "out-errors": int(stats.get("out-error-packets", 0)),
                    "in-discards": int(stats.get("in-discarded-packets", 0)),
                    "out-discards": int(stats.get("out-discarded-packets", 0)),
                }
            return result

        s1 = _parse(resp1)
        s2 = _parse(resp2)

        rows: List[Dict[str, Any]] = []

        def _delta(name: str, counter: str) -> int:
            # A counter that went backwards was reset (or the interface was
            # re-created) between the two samples; a negative rate is never a
            # truthful reading of that, so report no traffic for this interval.
            return max(s2[name][counter] - s1[name][counter], 0)

        for name in sorted(s2.keys()):
            if name not in s1:
                continue
            in_bps = round(_delta(name, "in-octets") * 8 / dt)
            out_bps = round(_delta(name, "out-octets") * 8 / dt)
            in_err = _delta(name, "in-errors")
            out_err = _delta(name, "out-errors")
            in_disc = _delta(name, "in-discards")
            out_disc = _delta(name, "out-discards")
            # Cumulative counters are always reported (even for idle interfaces)
            # so tests and agents can read raw packet/octet totals.
            rows.append(
                {
                    "interface": name,
                    "in-Kbps": round(in_bps / 1000, 1),
                    "out-Kbps": round(out_bps / 1000, 1),
                    "in-err": in_err,
                    "out-err": out_err,
                    "in-disc": in_disc,
                    "out-disc": out_disc,
                    "in-pkts": s2[name]["in-packets"],
                    "out-pkts": s2[name]["out-packets"],
                    "in-octets": s2[name]["in-octets"],
                    "out-octets": s2[name]["out-octets"],
                }
            )

        return {"ifstats": rows}
