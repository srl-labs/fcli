from __future__ import annotations

from typing import Any, Dict, List, Optional
import datetime
import jmespath

from .helpers import as_list, first_payload


def _relative_expiry(timestamp: Any) -> str:
    """Render a device timestamp as the time left until it, e.g. ``0:03:41s``.

    SR Linux reports these in UTC (the trailing ``Z``), so they have to be
    compared against UTC rather than the local clock - otherwise every entry is
    off by the timezone offset of whoever is running fcli.
    """
    try:
        expires_at = datetime.datetime.strptime(
            timestamp, "%Y-%m-%dT%H:%M:%S.%fZ"
        ).replace(tzinfo=datetime.timezone.utc)
    except (TypeError, ValueError):
        return "-"
    remaining = expires_at - datetime.datetime.now(datetime.timezone.utc)
    return str(remaining).split(".")[0] + "s"


class NeighborDiscoveryMixin:
    """Mixin providing ARP and ND getters."""

    def get(
        self,
        paths: List[str],
        datatype: Optional[str] = "config",
        strip_mod: Optional[bool] = True,
    ) -> List[Dict[str, Any]]:
        """Placeholder method implemented in :class:`SrLinux`."""
        raise NotImplementedError

    def get_arp(self) -> Dict[str, Any]:
        path_spec = {
            "path": "/interface[name=*]/subinterface[index=*]/ipv4/arp/neighbor",
            "jmespath": '"interface"[*].subinterface[].{interface:"_subitf", NI:"_ni"|to_string(@), entries:ipv4.arp.neighbor[].{IPv4:"ipv4-address",MAC:"link-layer-address",Type:origin,expiry:"_rel_expiry" }}',
            "datatype": "all",
        }
        ni_itfs = self.get(paths=["/network-instance[name=*]"], datatype="config")
        ni_itf_map: Dict[str, List[str]] = {}
        for ni in as_list(first_payload(ni_itfs).get("network-instance")):
            for ni_itf in ni.get("interface", []):
                if ni_itf["name"] not in ni_itf_map:
                    ni_itf_map[ni_itf["name"]] = []
                ni_itf_map[ni_itf["name"]].append(ni["name"])
        resp = self.get(
            paths=[path_spec.get("path", "")], datatype=path_spec["datatype"]
        )
        for itf in as_list(first_payload(resp).get("interface")):
            for subitf in itf.get("subinterface", []):
                subitf["_subitf"] = f"{itf['name']}.{subitf['index']}"
                subitf["_ni"] = ni_itf_map.get(subitf["_subitf"], [])
                for arp_entry in (
                    subitf.get("ipv4", {}).get("arp", {}).get("neighbor", [])
                ):
                    arp_entry["_rel_expiry"] = _relative_expiry(
                        arp_entry.get("expiration-time")
                    )
        res = jmespath.search(path_spec["jmespath"], first_payload(resp))
        return {"arp": res}

    def get_nd(self) -> Dict[str, Any]:
        path_spec = {
            "path": "/interface[name=*]/subinterface[index=*]/ipv6/neighbor-discovery/neighbor",
            "jmespath": '"interface"[*].subinterface[].{interface:"_subitf", entries:ipv6."neighbor-discovery".neighbor[].{IPv6:"ipv6-address",MAC:"link-layer-address",State:"current-state",Type:origin,next_state:"_rel_expiry" }}',
            "datatype": "all",
        }
        resp = self.get(
            paths=[path_spec.get("path", "")], datatype=path_spec["datatype"]
        )
        for itf in as_list(first_payload(resp).get("interface")):
            for subitf in itf.get("subinterface", []):
                subitf["_subitf"] = f"{itf['name']}.{subitf['index']}"
                for nd_entry in (
                    subitf.get("ipv6", {})
                    .get("neighbor-discovery", {})
                    .get("neighbor", [])
                ):
                    nd_entry["_rel_expiry"] = _relative_expiry(
                        nd_entry.get("next-state-time")
                    )
        res = jmespath.search(path_spec["jmespath"], first_payload(resp))
        return {"nd": res}
