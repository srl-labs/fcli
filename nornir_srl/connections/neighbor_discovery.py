from __future__ import annotations

from typing import Any, Dict, Iterator, List, Optional, Tuple
import datetime
import math

from ..records import NeighborCache, NeighborEntry
from .helpers import as_list, first_payload


def _seconds_until(timestamp: Any) -> Optional[int]:
    """How long until a device timestamp, in whole seconds, or ``None`` if it is not one.

    SR Linux reports these in UTC (the trailing ``Z``), so they have to be
    compared against UTC rather than the local clock - otherwise every entry is
    off by the timezone offset of whoever is running fcli.
    """
    try:
        at = datetime.datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=datetime.timezone.utc
        )
    except (TypeError, ValueError):
        return None
    return math.floor((at - datetime.datetime.now(datetime.timezone.utc)).total_seconds())


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

    def _ni_names_by_subitf(self) -> Dict[str, Tuple[str, ...]]:
        """Map ``<interface>.<index>`` to the network-instances that bind it.

        Only the interface lists are asked for. On the server a Get is served
        from what is subscribed, and a subscription to the whole
        network-instance subtree streams every node's BGP RIBs and statistics
        along with it - enough to fall behind on, and then everything on that
        node's stream goes stale.
        """
        ni_itfs = self.get(paths=["/network-instance[name=*]/interface"], datatype="config")
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
        return {subitf: tuple(names) for subitf, names in ni_itf_map.items()}

    def _subinterfaces(self, path: str) -> Iterator[Tuple[str, Tuple[str, ...], Dict[str, Any]]]:
        """Every subinterface *path* answers with: its name, its instances, its payload.

        gNMI often unwraps a one-entry YANG list to a dict, so both levels are
        read as lists whether or not they came as one.
        """
        bound = self._ni_names_by_subitf()
        resp = self.get(paths=[path], datatype="all")
        for itf in as_list(first_payload(resp).get("interface")):
            if not isinstance(itf, dict):
                continue
            for subitf in as_list(itf.get("subinterface")):
                if not isinstance(subitf, dict):
                    continue
                name = f"{itf.get('name', '')}.{subitf.get('index', '')}"
                yield name, bound.get(name, ()), subitf

    def get_arp(self) -> Dict[str, Any]:
        caches = [
            NeighborCache(
                interface=name,
                nis=nis,
                entries=tuple(
                    NeighborEntry(
                        address=str(entry.get("ipv4-address") or ""),
                        mac=str(entry.get("link-layer-address") or ""),
                        origin=str(entry.get("origin") or ""),
                        expires_in=_seconds_until(entry.get("expiration-time")),
                    )
                    for entry in as_list(
                        ((subitf.get("ipv4") or {}).get("arp") or {}).get("neighbor")
                    )
                    if isinstance(entry, dict)
                ),
            )
            for name, nis, subitf in self._subinterfaces(
                "/interface[name=*]/subinterface[index=*]/ipv4/arp/neighbor"
            )
        ]
        return {"arp": caches}

    def get_nd(self) -> Dict[str, Any]:
        caches = [
            NeighborCache(
                interface=name,
                nis=nis,
                entries=tuple(
                    NeighborEntry(
                        address=str(entry.get("ipv6-address") or ""),
                        mac=str(entry.get("link-layer-address") or ""),
                        origin=str(entry.get("origin") or ""),
                        state=str(entry.get("current-state") or ""),
                        expires_in=_seconds_until(entry.get("next-state-time")),
                    )
                    for entry in as_list(
                        ((subitf.get("ipv6") or {}).get("neighbor-discovery") or {}).get("neighbor")
                    )
                    if isinstance(entry, dict)
                ),
            )
            for name, nis, subitf in self._subinterfaces(
                "/interface[name=*]/subinterface[index=*]/ipv6/neighbor-discovery/neighbor"
            )
        ]
        return {"nd": caches}
