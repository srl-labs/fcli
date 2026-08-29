from __future__ import annotations

from typing import Any, Dict, List, Optional
import datetime
import jmespath

from .helpers import as_list, first_payload


def _normalize_interfaces(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Ensure ``interface`` / ``subinterface`` are lists, even for a single entry.

    gNMI often unwraps a one-entry YANG list to a dict, and the ARP/ND
    projections walk ``interface[*].subinterface[]``.
    """
    itfs = as_list(payload.get("interface"))
    for itf in itfs:
        if isinstance(itf, dict):
            itf["subinterface"] = as_list(itf.get("subinterface"))
    return {"interface": itfs}


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

    def _ni_names_by_subitf(self) -> Dict[str, str]:
        """Map ``<interface>.<index>`` to the network-instances that bind it."""
        ni_itfs = self.get(paths=["/network-instance[name=*]"], datatype="config")
        ni_itf_map: Dict[str, List[str]] = {}
        for ni in as_list(first_payload(ni_itfs).get("network-instance")):
            if not isinstance(ni, dict):
                continue
            ni_name = str(ni.get("name", "") or "")
            if not ni_name:
                continue
            for ni_itf in as_list(ni.get("interface")):
                if isinstance(ni_itf, str):
                    itf_name = ni_itf
                elif isinstance(ni_itf, dict):
                    itf_name = ni_itf.get("name")
                else:
                    continue
                if itf_name:
                    ni_itf_map.setdefault(str(itf_name), []).append(ni_name)
        return {subitf: ", ".join(names) for subitf, names in ni_itf_map.items()}

    def get_arp(self) -> Dict[str, Any]:
        path_spec = {
            "path": "/interface[name=*]/subinterface[index=*]/ipv4/arp/neighbor",
            "jmespath": '"interface"[*].subinterface[].{interface:"_subitf", NI:"_ni", entries:ipv4.arp.neighbor[].{IPv4:"ipv4-address",MAC:"link-layer-address",Type:origin,expiry:"_rel_expiry" }}',
            "datatype": "all",
        }
        ni_itf_map = self._ni_names_by_subitf()
        resp = self.get(
            paths=[path_spec.get("path", "")], datatype=path_spec["datatype"]
        )
        payload = _normalize_interfaces(first_payload(resp))
        for itf in payload["interface"]:
            if not isinstance(itf, dict):
                continue
            for subitf in itf.get("subinterface") or []:
                if not isinstance(subitf, dict):
                    continue
                subitf["_subitf"] = f"{itf['name']}.{subitf['index']}"
                subitf["_ni"] = ni_itf_map.get(subitf["_subitf"], "")
                for arp_entry in as_list(
                    subitf.get("ipv4", {}).get("arp", {}).get("neighbor")
                ):
                    arp_entry["_rel_expiry"] = _relative_expiry(
                        arp_entry.get("expiration-time")
                    )
        res = jmespath.search(path_spec["jmespath"], payload)
        return {"arp": res}

    def get_nd(self) -> Dict[str, Any]:
        path_spec = {
            "path": "/interface[name=*]/subinterface[index=*]/ipv6/neighbor-discovery/neighbor",
            "jmespath": '"interface"[*].subinterface[].{interface:"_subitf", NI:"_ni", entries:ipv6."neighbor-discovery".neighbor[].{IPv6:"ipv6-address",MAC:"link-layer-address",State:"current-state",Type:origin,next_state:"_rel_expiry" }}',
            "datatype": "all",
        }
        ni_itf_map = self._ni_names_by_subitf()
        resp = self.get(
            paths=[path_spec.get("path", "")], datatype=path_spec["datatype"]
        )
        payload = _normalize_interfaces(first_payload(resp))
        for itf in payload["interface"]:
            if not isinstance(itf, dict):
                continue
            for subitf in itf.get("subinterface") or []:
                if not isinstance(subitf, dict):
                    continue
                subitf["_subitf"] = f"{itf['name']}.{subitf['index']}"
                subitf["_ni"] = ni_itf_map.get(subitf["_subitf"], "")
                for nd_entry in as_list(
                    subitf.get("ipv6", {})
                    .get("neighbor-discovery", {})
                    .get("neighbor")
                ):
                    nd_entry["_rel_expiry"] = _relative_expiry(
                        nd_entry.get("next-state-time")
                    )
        res = jmespath.search(path_spec["jmespath"], payload)
        return {"nd": res}
