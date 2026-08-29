from __future__ import annotations

import ipaddress
from typing import Any, Dict, List, Optional, Tuple
import jmespath

from .helpers import as_list, first_payload
from .routing import _gnmi_path_missing, _suppress_pygnmi_client_logging


def _clean_state(value: Any) -> str:
    """A YANG state value as a bare lowercase word, without its module prefix."""
    if not value:
        return ""
    return str(value).lower().split(":")[-1]


def _subinterface_state(itf: Dict[str, Any], details: Dict[str, Any]) -> str:
    """The oper-state of a subinterface as its network-instance sees it.

    SR Linux answers this differently depending on where it is asked. An IRB in a
    disabled mac-vrf reads ``up`` under ``/interface`` - the subinterface itself
    is fine - while the mac-vrf holding it reports it ``down`` with
    ``net-inst-down``. In a service listing the latter is the truthful one, so the
    network-instance's own view wins and ``/interface`` only fills in what the
    network-instance does not carry.
    """
    state = _clean_state(itf.get("oper-state")) or _clean_state(details.get("oper-state"))
    if state:
        return state
    admin = _clean_state(itf.get("admin-state")) or _clean_state(details.get("admin-state"))
    if admin in ("disable", "disabled"):
        return "down"
    return "up"


def _subinterface_state_label(itf: Dict[str, Any], details: Dict[str, Any]) -> str:
    """How to label a subinterface's state, with the reason when it is down.

    A bare ``[down]`` next to a service that is itself down invites the question
    this answers: ``[down: net-inst-down]`` says the subinterface is only down
    because the service is.
    """
    state = _subinterface_state(itf, details)
    if state != "down":
        return state
    reason = _clean_state(itf.get("oper-down-reason")) or _clean_state(
        details.get("oper-down-reason")
    )
    return f"{state}: {reason}" if reason else state


def _host_address(pfx: str) -> str:
    """The address of an ``ip-prefix``, without its prefix length."""
    text = str(pfx).strip()
    if not text:
        return ""
    try:
        return str(ipaddress.ip_interface(text).ip)
    except ValueError:
        return text.split("/")[0]


def _family_addresses(ip_cfg: Any, version: int) -> List[str]:
    """Unicast host addresses of one IP family, skipping link-local and multicast."""
    if not isinstance(ip_cfg, dict):
        return []
    addrs: List[str] = []
    for addr in as_list(ip_cfg.get("address")):
        pfx = None
        if isinstance(addr, dict):
            pfx = addr.get("ip-prefix") or addr.get("prefix")
        elif isinstance(addr, str):
            pfx = addr
        if not pfx:
            continue
        host = _host_address(str(pfx))
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            continue
        if ip.version != version or ip.is_link_local or ip.is_multicast or ip.is_unspecified:
            continue
        if host not in addrs:
            addrs.append(host)
    return addrs


def _system0_addresses(subitf_details: Dict[str, Dict[str, Any]]) -> Tuple[str, str]:
    """IPv4 and IPv6 addresses configured on system0, as display strings.

    system0 is the loopback the fabric uses as a node identifier; the services
    tree shows these next to the node name. Link-local IPv6 is omitted.
    """
    si: Dict[str, Any] = {}
    for key in ("system0.0", "system0"):
        if key in subitf_details:
            si = subitf_details[key]
            break
    else:
        for key, details in subitf_details.items():
            if str(key).startswith("system0"):
                si = details
                break
    ipv4 = ", ".join(_family_addresses(si.get("ipv4"), 4))
    ipv6 = ", ".join(_family_addresses(si.get("ipv6"), 6))
    return ipv4, ipv6


def _to_subnet(pfx: str) -> str:
    """The network prefix of an interface address, e.g. ``10.1.100.1/24`` → ``10.1.100.0/24``.

    Link-local and unspecified addresses are omitted; they are not service subnets.
    """
    text = str(pfx).strip()
    if not text:
        return ""
    try:
        net = ipaddress.ip_network(text, strict=False)
    except ValueError:
        return text
    if net.is_link_local or net.is_unspecified or net.is_multicast:
        return ""
    return str(net)


def _host_ip_if_host_route(pfx: Any) -> str:
    """The address of a host route (``/32`` or ``/128``), otherwise ``""``."""
    text = str(pfx or "").strip()
    if not text:
        return ""
    try:
        net = ipaddress.ip_network(text, strict=False)
    except ValueError:
        return ""
    if net.prefixlen != net.max_prefixlen:
        return ""
    if net.is_link_local or net.is_unspecified or net.is_multicast:
        return ""
    return str(net.network_address)


def _host_ips_from_payload(payload: Any) -> List[str]:
    """Host-route addresses nested anywhere in a gNMI route-table payload."""
    found: List[str] = []
    seen: set[str] = set()

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            for key in ("ipv4-prefix", "ipv6-prefix"):
                if key in obj:
                    ip = _host_ip_if_host_route(obj[key])
                    if ip and ip not in seen:
                        seen.add(ip)
                        found.append(ip)
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(payload)
    return found


def _split_host_ips(text: Any) -> List[str]:
    """Bare addresses from a comma- or space-separated cell."""
    if not text:
        return []
    ips: List[str] = []
    for part in str(text).replace(",", " ").split():
        host = part.split("/")[0].strip()
        if host:
            ips.append(host)
    return ips


def _underlay_hosts_from_instances(ni_list: List[Any]) -> str:
    """Host routes in the default instance's route-table, if that table is present."""
    for ni in as_list(ni_list):
        if isinstance(ni, dict) and str(ni.get("name", "")).lower() == "default":
            return ", ".join(_host_ips_from_payload(ni.get("route-table") or {}))
    return ""


def assign_underlay_sites(rows: List[Dict[str, Any]]) -> Dict[str, str]:
    """Number underlay islands ``1``, ``2``, … from default-RIB reachability of system0.

    Two nodes share a site when each has the other's system0 address as a host
    route in network-instance ``default``. When the set also includes
    non-gateway nodes (leaves), edges between two Gateway nodes are ignored:
    DCGWs learn each other over the WAN and would otherwise glue every DC into
    one component. A service that is only Gateways (the WAN/DCI side) keeps
    those edges, so dcgw1..4 that all have each other's loopbacks stay one tile.

    Returns ``{}`` when there is only one island (or none), so a single fabric
    is not labelled ``(1)``.
    """
    by_node: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        name = str(row.get("Node") or "unknown")
        by_node.setdefault(name, []).append(row)
    nodes = sorted(by_node)
    if len(nodes) < 2:
        return {}

    system: Dict[str, List[str]] = {}
    rib: Dict[str, set[str]] = {}
    gateway: set[str] = set()
    any_rib = False
    for name in nodes:
        sample = by_node[name][0]
        system[name] = _split_host_ips(sample.get("System IPv4")) + _split_host_ips(
            sample.get("System IPv6")
        )
        hosts = set(_split_host_ips(sample.get("Underlay Hosts")))
        rib[name] = hosts
        if hosts:
            any_rib = True
        if any(
            r.get("Gateway") in ("Y", True, "true")
            for r in by_node[name]
        ):
            gateway.add(name)

    if not any_rib:
        return {}

    skip_gateway_pairs = bool(nodes) and any(n not in gateway for n in nodes)

    def sees(observer: str, other: str) -> bool:
        other_ips = system[other]
        if not other_ips:
            return False
        table = rib[observer]
        return any(ip in table for ip in other_ips)

    parent = {n: n for n in nodes}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i, a in enumerate(nodes):
        for b in nodes[i + 1 :]:
            if skip_gateway_pairs and a in gateway and b in gateway:
                continue
            if sees(a, b) and sees(b, a):
                union(a, b)

    components: Dict[str, List[str]] = {}
    for name in nodes:
        components.setdefault(find(name), []).append(name)
    ordered = sorted(components.values(), key=lambda members: members[0])
    if len(ordered) <= 1:
        return {}
    return {
        name: str(index)
        for index, members in enumerate(ordered, start=1)
        for name in members
    }


def stamp_underlay_sites(rows: List[Dict[str, Any]]) -> bool:
    """Stamp ``Site`` on each row, clustered per Bridge Domain / Router.

    A DCGW appears in both the DC-side and WAN-side tiles; those tiles must be
    clustered on their own members so the WAN tile can stay one Router while
    the DC tile still splits by fabric.
    """
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        if row.get("Bridge Domain") or row.get("Service Type") == "Bridge Domain":
            key = "bd:" + str(row.get("Bridge Domain") or row.get("Route Targets") or "")
        else:
            key = "rt:" + str(row.get("Router") or row.get("Route Targets") or "")
        groups.setdefault(key, []).append(row)
    any_site = False
    for group in groups.values():
        sites = assign_underlay_sites(group)
        for row in group:
            site = sites.get(str(row.get("Node") or ""), "")
            if site:
                any_site = True
            row["Site"] = site
    return any_site


def _format_route_target(target: Any) -> str:
    """A route-target as ``target:x:y``, or ``""`` if *target* is empty."""
    if not target:
        return ""
    text = str(target)
    return text if text.startswith("target:") else f"target:{text}"


def _instance_route_targets(inst: Dict[str, Any]) -> List[str]:
    """The import and export route-targets of one ``bgp-vpn`` instance."""
    if not isinstance(inst, dict):
        return []
    rt_cfg = inst.get("route-target", {})
    if not isinstance(rt_cfg, dict):
        return []
    rts: set[str] = set()
    for key in ("import-rt", "export-rt"):
        for item in as_list(rt_cfg.get(key)):
            target = item.get("target") if isinstance(item, dict) else item
            formatted = _format_route_target(target)
            if formatted:
                rts.add(formatted)
    return sorted(rts)


def _vpn_tile_groups(ni: Dict[str, Any], isolated_label: str) -> List[Dict[str, Any]]:
    """How a network-instance is shown as service tiles, one group per tile.

    A single enabled ``bgp-vpn`` instance (or none) is one tile, keyed by its
    route-target. Two or more enabled instances with route-targets are a
    Gateway: each instance becomes its own tile with that instance's RT so the
    DC and WAN sides of a DCGW stay distinct.
    """
    enabled: List[Dict[str, Any]] = []
    for i, inst in enumerate(
        as_list(ni.get("protocols", {}).get("bgp-vpn", {}).get("bgp-instance")),
        start=1,
    ):
        if not isinstance(inst, dict):
            continue
        if _clean_state(inst.get("admin-state")) in ("disable", "disabled"):
            continue
        iid = inst.get("id")
        if iid is None:
            iid = inst.get("index")
        enabled.append(
            {
                "id": str(iid) if iid is not None and str(iid) != "" else str(i),
                "rts": _instance_route_targets(inst),
            }
        )

    with_rts = [e for e in enabled if e["rts"]]
    if len(with_rts) < 2:
        rts = sorted({rt for e in enabled for rt in e["rts"]})
        return [
            {
                "primary": rts[0] if rts else isolated_label,
                "rts": rts,
                "id": with_rts[0]["id"] if len(with_rts) == 1 else "",
                "gateway": False,
            }
        ]

    primary_counts: Dict[str, int] = {}
    for e in with_rts:
        primary = e["rts"][0]
        primary_counts[primary] = primary_counts.get(primary, 0) + 1

    groups = []
    for e in with_rts:
        primary = e["rts"][0]
        if primary_counts[primary] > 1:
            primary = f"{primary} (bgp-instance {e['id']})"
        groups.append(
            {
                "primary": primary,
                "rts": e["rts"],
                "id": e["id"],
                "gateway": True,
            }
        )
    return groups


class Layer2Mixin:
    """Mixin providing Layer2 related getters."""

    def get(
        self,
        paths: List[str],
        datatype: Optional[str] = "config",
        strip_mod: Optional[bool] = True,
    ) -> List[Dict[str, Any]]:
        """Placeholder method implemented in :class:`SrLinux`."""
        raise NotImplementedError

    def _has_feature(self, feature: str) -> bool:
        """Whether the device advertises *feature* under ``/system/features``.

        Reports gate on this so a node that does no bridging or EVPN renders an
        empty table instead of failing on a path it does not implement.
        """
        payload = first_payload(self.get(paths=["/system/features"], datatype="state"))
        return feature in (payload.get("system/features") or [])

    def _subinterface_details(self) -> Dict[str, Dict[str, Any]]:
        """Map ``<interface>.<index>`` to that subinterface's state.

        The network-instance tree names the subinterfaces placed in a service but
        carries none of their detail, so the addresses, anycast-gw flag and VLAN
        encapsulation that the service reports show come from this second Get.
        """
        details: Dict[str, Dict[str, Any]] = {}
        with _suppress_pygnmi_client_logging():
            try:
                resp = self.get(
                    paths=["/interface[name=*]/subinterface"], datatype="all"
                )
            except BaseException as e:
                if _gnmi_path_missing(e):
                    return details
                raise
        for itf in as_list(first_payload(resp).get("interface")):
            if not isinstance(itf, dict):
                continue
            itf_name = itf.get("name", "")
            for si in as_list(itf.get("subinterface")):
                if not isinstance(si, dict):
                    continue
                si_name = si.get("name", "")
                if not si_name:
                    si_name = f"{itf_name}.{si.get('index', '')}"
                elif str(si_name).isdigit():
                    si_name = f"{itf_name}.{si_name}"
                details[si_name] = si
        return details

    def _default_underlay_hosts(self, ni_list: Optional[List[Any]] = None) -> str:
        """Host routes (``/32``, ``/128``) in network-instance ``default``.

        These are the loopbacks this node can see in the underlay; services use
        them to split a Route-Target that spans more than one DC.
        """
        from_ni = _underlay_hosts_from_instances(ni_list or [])
        if from_ni:
            return from_ni
        hosts: List[str] = []
        seen: set[str] = set()
        for path in (
            "/network-instance[name=default]/route-table/ipv4-unicast/route/ipv4-prefix",
            "/network-instance[name=default]/route-table/ipv6-unicast/route/ipv6-prefix",
        ):
            with _suppress_pygnmi_client_logging():
                try:
                    resp = self.get(paths=[path], datatype="state")
                except BaseException as e:
                    if _gnmi_path_missing(e) or isinstance(e, KeyError):
                        continue
                    raise
            payload: Any = first_payload(resp)
            if not payload:
                payload = resp
            for ip in _host_ips_from_payload(payload):
                if ip not in seen:
                    seen.add(ip)
                    hosts.append(ip)
        return ", ".join(hosts)

    def get_lldp_sum(self, interface: Optional[str] = "*") -> Dict[str, Any]:
        path_spec = {
            "path": f"/system/lldp/interface[name={interface}]/neighbor",
            "jmespath": '"system/lldp".interface[].{interface:name, Neighbors:neighbor[].{"Nbr-port":"port-id","Nbr-System":"system-name", "Nbr-port-desc":"port-description"}}',
            "datatype": "state",
        }
        resp = self.get(
            paths=[path_spec.get("path", "")], datatype=path_spec["datatype"]
        )
        res = jmespath.search(path_spec["jmespath"], first_payload(resp))
        return {"lldp_nbrs": res}

    def get_mac_table(self, network_instance: Optional[str] = "*") -> Dict[str, Any]:
        path_spec = {
            "path": f"/network-instance[name={network_instance}]/bridge-table/mac-table/mac",
            "jmespath": '"network-instance"[].{"NI":name, Fib:"bridge-table"."mac-table".mac[].{Address:address, Dest:destination, Type:type}}',
            "datatype": "state",
        }
        if not self._has_feature("bridged"):
            return {"mac_table": []}
        with _suppress_pygnmi_client_logging():
            try:
                resp = self.get(
                    paths=[path_spec.get("path", "")], datatype=path_spec["datatype"]
                )
            except BaseException as e:
                if _gnmi_path_missing(e):
                    return {"mac_table": []}
                raise
        res = jmespath.search(path_spec["jmespath"], first_payload(resp))
        return {"mac_table": res}

    def get_es(self) -> Dict[str, Any]:
        path_spec = {
            "path": "/system/network-instance/protocols/evpn/ethernet-segments",
            "jmespath": '"system/network-instance/protocols/evpn/ethernet-segments"."bgp-instance"[]."ethernet-segment"[].{name:name, esi:esi, type:type, "mh-mode":"multi-homing-mode", oper:"oper-state", "itf/nh":"_itf_or_nh", "ni-peers":association."network-instance"[]."_ni_peers"|join(\', \',@) }',
            "datatype": "all",
        }

        def set_es_fields(resp: List[Dict[str, Any]]) -> None:
            segments = first_payload(resp).get(
                "system/network-instance/protocols/evpn/ethernet-segments", {}
            )
            for bgp_inst in as_list(segments.get("bgp-instance")):
                for es in as_list(bgp_inst.get("ethernet-segment")):
                    # compute interface or next-hop display field
                    if "interface" in es:
                        es["_itf_or_nh"] = " ".join(
                            i["ethernet-interface"] for i in es["interface"]
                        )
                    elif "next-hop" in es:
                        es["_itf_or_nh"] = " ".join(
                            nh["l3-next-hop"] for nh in es["next-hop"]
                        )
                    else:
                        es["_itf_or_nh"] = ""
                    if "association" not in es:
                        es["association"] = {}
                    if "network-instance" not in es["association"]:
                        es["association"]["network-instance"] = []
                    for vrf in es["association"]["network-instance"]:
                        # Only the first bgp-instance elects a DF for the segment.
                        instances = as_list(vrf.get("bgp-instance"))
                        candidates = (instances[0] if instances else {}).get(
                            "computed-designated-forwarder-candidates", {}
                        )
                        es_peers = as_list(
                            candidates.get("designated-forwarder-candidate")
                        )
                        vrf["_peers"] = " ".join(
                            (
                                f"{peer.get('address')}(DF)"
                                if peer.get("designated-forwarder")
                                else str(peer.get("address"))
                            )
                            for peer in es_peers
                        )
                        vrf["_ni_peers"] = f"{vrf['name']}:[{vrf['_peers']}]"

        if not self._has_feature("evpn"):
            return {"es": []}
        with _suppress_pygnmi_client_logging():
            try:
                resp = self.get(
                    paths=[path_spec.get("path", "")], datatype=path_spec["datatype"]
                )
            except BaseException as e:
                if _gnmi_path_missing(e):
                    return {"es": []}
                raise
        set_es_fields(resp)
        res = jmespath.search(path_spec["jmespath"], first_payload(resp))
        return {"es": res}

    def get_es_dest(self) -> Dict[str, Any]:
        path_spec = {
            "path": "/tunnel-interface[name=*]/vxlan-interface/bridge-table/unicast-destinations/es-destination",
            "jmespath": '"tunnel-interface"[].{tunnel:name, "es-dest":"vxlan-interface"[]."bridge-table"."unicast-destinations"."es-destination"[].{esi:esi, vteps:"_vteps"}}',
            "datatype": "state",
        }

        def set_vtep_fields(resp: List[Dict[str, Any]]) -> None:
            for tun in as_list(first_payload(resp).get("tunnel-interface")):
                for vxlan in tun.get("vxlan-interface", []):
                    bt = vxlan.get("bridge-table", {})
                    ucast = bt.get("unicast-destinations", {})
                    for es_dest in ucast.get("es-destination", []):
                        vteps = es_dest.get("vtep", [])
                        es_dest["_vteps"] = " ".join(
                            v.get("address", "") for v in vteps
                        )

        if not self._has_feature("bridged"):
            return {"es_dest": []}
        with _suppress_pygnmi_client_logging():
            try:
                resp = self.get(
                    paths=[path_spec.get("path", "")], datatype=path_spec["datatype"]
                )
            except BaseException as e:
                if _gnmi_path_missing(e):
                    return {"es_dest": []}
                raise
        set_vtep_fields(resp)
        res = jmespath.search(path_spec["jmespath"], first_payload(resp))
        return {"es_dest": res}

    def get_vxlan(self) -> Dict[str, Any]:
        path_spec = {
            "path": "/tunnel-interface[name=*]/vxlan-interface",
            "jmespath": '"tunnel-interface"[]."_vxlan_itfs"[].{"vxlan-itf":name, NI:ni, "ing-vni":"ing-vni", destinations:destinations}',
            # ``all`` rather than ``state``: up to 25.3 the state datastore also
            # carried the configured ``ingress/vni`` and ``type``, and from 25.10
            # it does not, which left the VNI column empty on newer releases.
            # The destinations only exist in state, so both datastores are needed.
            "datatype": "all",
        }

        def set_vxlan_fields(
            resp: List[Dict[str, Any]],
            ni_map: Dict[str, str],
        ) -> None:
            for tun in as_list(first_payload(resp).get("tunnel-interface")):
                tun["_vxlan_itfs"] = []
                for vxlan in tun.get("vxlan-interface", []):
                    vxlan_name = f"{tun['name']}.{vxlan['index']}"
                    dests = (
                        vxlan.get("bridge-table", {})
                        .get("unicast-destinations", {})
                        .get("destination", [])
                    )
                    vteps = ", ".join(
                        f"({d.get('vtep', '')}, {d.get('vni', '')})" for d in dests
                    )
                    tun["_vxlan_itfs"].append(
                        {
                            "name": vxlan_name,
                            "ni": ni_map.get(vxlan_name, ""),
                            "ing-vni": vxlan.get("ingress", {}).get("vni", "-"),
                            "destinations": vteps if vteps else "-",
                        }
                    )

        if not self._has_feature("bridged"):
            return {"vxlan": []}

        # build vxlan-interface to network-instance map
        ni_resp = self.get(paths=["/network-instance[name=*]"], datatype="config")
        ni_map: Dict[str, str] = {}
        for ni in as_list(first_payload(ni_resp).get("network-instance")):
            for vxlan_itf in as_list(ni.get("vxlan-interface")):
                ni_map[vxlan_itf["name"]] = ni["name"]

        with _suppress_pygnmi_client_logging():
            try:
                resp = self.get(
                    paths=[path_spec.get("path", "")], datatype=path_spec["datatype"]
                )
            except BaseException as e:
                if _gnmi_path_missing(e):
                    return {"vxlan": []}
                raise
        set_vxlan_fields(resp, ni_map)
        res = jmespath.search(path_spec["jmespath"], first_payload(resp))
        return {"vxlan": res}

    def get_irb(self) -> Dict[str, Any]:
        path_spec = {
            "path": "/interface[name=irb*]/subinterface",
            "jmespath": (
                '"interface"[].subinterface[].{name:"_subitf",'
                ' "net-inst":"_ni",'
                ' "ipv4-addr":"_ipv4_addrs",'
                ' "ipv6-addr":"_ipv6_addrs",'
                ' "AGW?":"_anycast_gw",'
                ' arp:"_arp_summary",'
                ' nd:"_nd_summary",'
                ' "arp-evpn":"_arp_evpn",'
                ' "nd-evpn":"_nd_evpn",'
                ' "IFL?":"_ilr"}'
            ),
            "datatype": "all",
        }

        # build NI-to-interface map
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

        def _format_addrs(addrs: List[Dict[str, Any]]) -> str:
            parts = []
            for a in addrs:
                s = a.get("ip-prefix", "")
                flags = []
                if a.get("primary") is not None:
                    flags.append("P")
                if a.get("anycast-gw"):
                    flags.append("AGW")
                if flags:
                    s += f" ({','.join(flags)})"
                parts.append(s)
            return ", ".join(parts) if parts else ""

        def _arp_summary(ipv4: Dict[str, Any]) -> str:
            arp = ipv4.get("arp", {})
            parts = []
            if arp.get("proxy-arp"):
                parts.append("proxy")
            if arp.get("learn-unsolicited"):
                parts.append("learn-unsol")
            for hr in arp.get("host-route", {}).get("populate", []):
                dp = "dp" if hr.get("datapath-programming") else "no-dp"
                parts.append(f"host-rt:{hr.get('route-type', '?')}/{dp}")
            return ", ".join(parts) if parts else "-"

        def _nd_summary(ipv6: Dict[str, Any]) -> str:
            nd = ipv6.get("neighbor-discovery", {})
            parts = []
            if nd.get("proxy-nd"):
                parts.append("proxy")
            learn = nd.get("learn-unsolicited", "none")
            if learn and learn != "none":
                parts.append(f"learn-unsol:{learn}")
            for hr in nd.get("host-route", {}).get("populate", []):
                dp = "dp" if hr.get("datapath-programming") else "no-dp"
                parts.append(f"host-rt:{hr.get('route-type', '?')}/{dp}")
            return ", ".join(parts) if parts else "-"

        def _evpn_adv(proto_cfg: Dict[str, Any]) -> str:
            evpn = proto_cfg.get("evpn", {})
            advs = evpn.get("advertise", [])
            if not advs:
                return "-"
            return ", ".join(a.get("route-type", "?") for a in advs)

        for itf in as_list(first_payload(resp).get("interface")):
            for subitf in itf.get("subinterface", []):
                subitf_name = f"{itf['name']}.{subitf['index']}"
                subitf["_subitf"] = subitf_name
                subitf["_ni"] = ", ".join(ni_itf_map.get(subitf_name, []))

                ipv4 = subitf.get("ipv4", {})
                ipv6 = subitf.get("ipv6", {})

                subitf["_ipv4_addrs"] = _format_addrs(ipv4.get("address", []))
                subitf["_ipv6_addrs"] = _format_addrs(ipv6.get("address", []))

                agw = subitf.get("anycast-gw", {})
                if agw:
                    subitf["_anycast_gw"] = "Y"
                else:
                    subitf["_anycast_gw"] = "N"

                subitf["_arp_summary"] = _arp_summary(ipv4)
                subitf["_nd_summary"] = _nd_summary(ipv6)
                subitf["_arp_evpn"] = _evpn_adv(ipv4.get("arp", {}))
                subitf["_nd_evpn"] = _evpn_adv(ipv6.get("neighbor-discovery", {}))

                arp_advs = ipv4.get("arp", {}).get("evpn", {}).get("advertise", [])
                nd_advs = (
                    ipv6.get("neighbor-discovery", {})
                    .get("evpn", {})
                    .get("advertise", [])
                )
                has_ilr = any("interface-less-routing" in a for a in arp_advs + nd_advs)
                subitf["_ilr"] = "Y" if has_ilr else "N"

        res = jmespath.search(path_spec["jmespath"], first_payload(resp))
        return {"irb": res}

    def get_bridge_domains(self, nw_instance: str = "*") -> Dict[str, Any]:
        """Return EVPN Bridge Domains (mac-vrf) grouped by Route-Target."""
        path_spec = {
            "path": f"/network-instance[name={nw_instance}]",
            "datatype": "all",
        }
        with _suppress_pygnmi_client_logging():
            try:
                resp = self.get(paths=[path_spec["path"]], datatype=path_spec["datatype"])
            except BaseException as e:
                if _gnmi_path_missing(e):
                    return {"bridge_domains": []}
                raise

        if not first_payload(resp):
            return {"bridge_domains": []}

        ni_list = as_list(first_payload(resp).get("network-instance"))

        # for IRB IPv4/IPv6, anycast-gw, VLAN encapsulation, and system0
        subitf_details = self._subinterface_details()
        system_ipv4, system_ipv6 = _system0_addresses(subitf_details)
        underlay_hosts = self._default_underlay_hosts(ni_list)

        # Build mapping of irb_subinterface -> associated ip-vrf / L3 network instances
        irb_to_ip_vrf: Dict[str, List[str]] = {}
        for ni in ni_list:
            if isinstance(ni, dict) and ni.get("type") != "mac-vrf":
                vrf_name = ni.get("name", "")
                for itf in ni.get("interface", []):
                    if isinstance(itf, dict) and itf.get("name"):
                        itf_name = itf["name"]
                        if itf_name.startswith("irb"):
                            if itf_name not in irb_to_ip_vrf:
                                irb_to_ip_vrf[itf_name] = []
                            irb_to_ip_vrf[itf_name].append(vrf_name)

        results = []

        def _extract_vlan_encap(itf_name: str, itf_dict: Dict[str, Any]) -> str:
            si = subitf_details.get(itf_name, {})
            for obj in (si, itf_dict):
                if not isinstance(obj, dict):
                    continue
                vlan = obj.get("vlan", {})
                if isinstance(vlan, dict):
                    encap = vlan.get("encap", {})
                    if isinstance(encap, dict):
                        if "untagged" in encap:
                            return "untagged"
                        single = encap.get("single-tagged", {})
                        if isinstance(single, dict) and "vlan-id" in single:
                            return str(single["vlan-id"])
                        if "vlan-id" in encap:
                            return str(encap["vlan-id"])
                    if "vlan-id" in vlan:
                        return str(vlan["vlan-id"])
                if "vlan-id" in obj:
                    return str(obj["vlan-id"])
                if "vlan" in obj and isinstance(obj["vlan"], (int, str)):
                    return str(obj["vlan"])

            if itf_name.endswith(".0"):
                return "untagged"
            parts = itf_name.split(".")
            if len(parts) > 1 and parts[-1].isdigit():
                return parts[-1]
            return "untagged"

        def _format_irb_item_and_subnets(itf_name: str, itf_dict: Dict[str, Any], assoc_vrfs: List[str]) -> Tuple[str, List[str], str]:
            si = subitf_details.get(itf_name, {})
            st = _subinterface_state_label(itf_dict, si)
            ips: List[str] = []
            is_anycast = False

            def _check_ip_block(ip_cfg: Any) -> None:
                nonlocal is_anycast
                if not isinstance(ip_cfg, dict):
                    return
                if ip_cfg.get("anycast-gw") is True or str(ip_cfg.get("anycast-gw")).lower() == "true":
                    is_anycast = True
                for addr in ip_cfg.get("address", []):
                    if isinstance(addr, dict):
                        pfx = addr.get("ip-prefix")
                        if pfx:
                            ips.append(str(pfx))
                        if addr.get("anycast-gw") is True or str(addr.get("anycast-gw")).lower() == "true":
                            is_anycast = True
                    elif isinstance(addr, str):
                        ips.append(addr)

            _check_ip_block(si.get("ipv4"))
            _check_ip_block(si.get("ipv6"))
            _check_ip_block(itf_dict.get("ipv4"))
            _check_ip_block(itf_dict.get("ipv6"))

            for obj in (si, itf_dict):
                if obj.get("anycast-gw") is True or str(obj.get("anycast-gw")).lower() == "true":
                    is_anycast = True
                if obj.get("anycast-gateway") is True or str(obj.get("anycast-gateway")).lower() == "true":
                    is_anycast = True

            subnets = [s for pfx in ips if (s := _to_subnet(pfx))]

            ip_part = f": {', '.join(ips)}" if ips else ""
            gw_part = f" (anycast-gw: {'true' if is_anycast else 'false'})"
            vrf_part = f" -> {', '.join(assoc_vrfs)}" if assoc_vrfs else ""

            return f"{itf_name} [{st}]{ip_part}{gw_part}{vrf_part}", subnets, st

        for ni in ni_list:
            if not isinstance(ni, dict):
                continue
            ni_name = ni.get("name", "")
            ni_type = ni.get("type", "")
            if ni_type != "mac-vrf":
                continue
            oper_state = ni.get("oper-state", "unknown")

            irb_subitfs = []
            bridge_subitfs = []
            all_subnets = []
            subitf_states = []
            for i in ni.get("interface", []):
                if isinstance(i, dict) and i.get("name"):
                    name = i["name"]
                    details = subitf_details.get(name, {})
                    # The bare state feeds the service's aggregate oper-state
                    # below, which counts up against down; the label is only for
                    # display and can carry a reason with it.
                    subitf_states.append(_subinterface_state(i, details))
                    if name.startswith("irb"):
                        assoc_vrfs = irb_to_ip_vrf.get(name, [])
                        irb_item_str, subnets, _st = _format_irb_item_and_subnets(name, i, assoc_vrfs)
                        irb_subitfs.append(irb_item_str)
                        all_subnets.extend(subnets)
                    else:
                        vlan_info = _extract_vlan_encap(name, i)
                        label = _subinterface_state_label(i, details)
                        bridge_subitfs.append(f"{name} [{label}] (VLAN: {vlan_info})")

            vxlan_itfs = [
                v.get("name", "")
                for v in ni.get("vxlan-interface", [])
                if isinstance(v, dict) and v.get("name")
            ]

            ni_oper = _clean_state(oper_state)
            if ni_oper == "down":
                effective_oper = "down"
            elif subitf_states:
                up_cnt = sum(1 for s in subitf_states if s in ("up", "enable", "enabled", "active"))
                down_cnt = sum(1 for s in subitf_states if s in ("down", "disable", "disabled"))
                if up_cnt == len(subitf_states):
                    effective_oper = "up"
                elif down_cnt == len(subitf_states):
                    effective_oper = "down"
                else:
                    effective_oper = "degraded"
            else:
                effective_oper = ni_oper if ni_oper else "up"

            for group in _vpn_tile_groups(ni, f"mac-vrf:{ni_name}"):
                rt_display = ", ".join(group["rts"]) if group["rts"] else f"mac-vrf:{ni_name}"
                results.append(
                    {
                        "Bridge Domain": group["primary"],
                        "MAC-VRF": ni_name,
                        "Oper State": effective_oper,
                        "Route Targets": rt_display,
                        "Subnets": ", ".join(all_subnets) if all_subnets else "",
                        "IRB Interface": ", ".join(irb_subitfs) if irb_subitfs else "-",
                        "Sub-Interfaces": ", ".join(bridge_subitfs) if bridge_subitfs else "-",
                        "VXLAN Interface": ", ".join(vxlan_itfs) if vxlan_itfs else "-",
                        "Gateway": "Y" if group["gateway"] else "",
                        "BGP Instance": group["id"] if group["gateway"] else "",
                        "System IPv4": system_ipv4,
                        "System IPv6": system_ipv6,
                        "Underlay Hosts": underlay_hosts,
                    }
                )

        return {"bridge_domains": results}

    def get_routers(self, nw_instance: str = "*") -> Dict[str, Any]:
        """Return EVPN Routers (ip-vrf) grouped by Route-Target."""
        path_spec = {
            "path": f"/network-instance[name={nw_instance}]",
            "datatype": "all",
        }
        with _suppress_pygnmi_client_logging():
            try:
                resp = self.get(paths=[path_spec["path"]], datatype=path_spec["datatype"])
            except BaseException as e:
                if _gnmi_path_missing(e):
                    return {"routers": []}
                raise

        if not first_payload(resp):
            return {"routers": []}

        ni_list = as_list(first_payload(resp).get("network-instance"))

        # for the IPv4/IPv6 addresses of the interfaces placed in each router,
        # and system0 shown next to the node name
        subitf_details = self._subinterface_details()
        system_ipv4, system_ipv6 = _system0_addresses(subitf_details)
        underlay_hosts = self._default_underlay_hosts(ni_list)

        # Build mapping of irb_subinterface -> mac-vrf network instance name
        irb_to_mac_vrf: Dict[str, str] = {}
        for ni in ni_list:
            if isinstance(ni, dict) and ni.get("type") == "mac-vrf":
                mac_vrf_name = ni.get("name", "")
                for itf in ni.get("interface", []):
                    if isinstance(itf, dict) and itf.get("name"):
                        itf_name = itf["name"]
                        if itf_name.startswith("irb"):
                            irb_to_mac_vrf[itf_name] = mac_vrf_name

        def _get_ip_addresses(itf_name: str, itf_dict: Dict[str, Any]) -> List[str]:
            si = subitf_details.get(itf_name, {})
            ips: List[str] = []

            def _check_ip_block(ip_cfg: Any) -> None:
                if not isinstance(ip_cfg, dict):
                    return
                for addr in as_list(ip_cfg.get("address")):
                    if isinstance(addr, dict):
                        pfx = addr.get("ip-prefix")
                        if pfx:
                            ips.append(str(pfx))
                    elif isinstance(addr, str):
                        ips.append(addr)

            _check_ip_block(si.get("ipv4"))
            _check_ip_block(si.get("ipv6"))
            _check_ip_block(itf_dict.get("ipv4"))
            _check_ip_block(itf_dict.get("ipv6"))
            return ips

        results = []

        for ni in ni_list:
            if not isinstance(ni, dict):
                continue
            ni_name = ni.get("name", "")
            ni_type = ni.get("type", "")
            if ni_type != "ip-vrf" or ni_name.lower() == "mgmt":
                continue
            oper_state = ni.get("oper-state", "unknown")

            mac_vrfs_items = []
            routed_itfs_items = []
            subitf_states = []
            irb_subnets: List[str] = []

            for i in as_list(ni.get("interface")):
                if isinstance(i, dict) and i.get("name"):
                    name = i["name"]
                    details = subitf_details.get(name, {})
                    subitf_states.append(_subinterface_state(i, details))
                    st = _subinterface_state_label(i, details)
                    ips = _get_ip_addresses(name, i)
                    ip_str = ", ".join(ips) if ips else ""

                    if name.startswith("irb"):
                        mac_name = irb_to_mac_vrf.get(name, "unknown")
                        if ip_str:
                            mac_vrfs_items.append(f"{mac_name} ({name} [{st}]: {ip_str})")
                        else:
                            mac_vrfs_items.append(f"{mac_name} ({name} [{st}])")
                        for pfx in ips:
                            subnet = _to_subnet(pfx)
                            if subnet and subnet not in irb_subnets:
                                irb_subnets.append(subnet)
                    else:
                        if ip_str:
                            routed_itfs_items.append(f"{name} [{st}] ({ip_str})")
                        else:
                            routed_itfs_items.append(f"{name} [{st}]")

            vxlan_itfs = [
                v.get("name", "")
                for v in ni.get("vxlan-interface", [])
                if isinstance(v, dict) and v.get("name")
            ]

            ni_oper = _clean_state(oper_state)
            if ni_oper == "down":
                effective_oper = "down"
            elif subitf_states:
                up_cnt = sum(1 for s in subitf_states if s in ("up", "enable", "enabled", "active"))
                down_cnt = sum(1 for s in subitf_states if s in ("down", "disable", "disabled"))
                if up_cnt == len(subitf_states):
                    effective_oper = "up"
                elif down_cnt == len(subitf_states):
                    effective_oper = "down"
                else:
                    effective_oper = "degraded"
            else:
                effective_oper = ni_oper if ni_oper else "up"

            for group in _vpn_tile_groups(ni, "none (isolated)"):
                isolated = not group["rts"]
                rt_display = ", ".join(group["rts"]) if group["rts"] else "none (isolated)"
                primary = (
                    f"none (isolated) - {ni_name}" if isolated else group["primary"]
                )
                results.append(
                    {
                        "Router": primary,
                        "IP-VRF": ni_name,
                        "Oper State": effective_oper,
                        "Route Targets": rt_display,
                        "MAC-VRFs": ", ".join(mac_vrfs_items) if mac_vrfs_items else "-",
                        "Routed Interfaces": ", ".join(routed_itfs_items) if routed_itfs_items else "-",
                        "VXLAN Interface": ", ".join(vxlan_itfs) if vxlan_itfs else "-",
                        "Subnets": ", ".join(irb_subnets) if irb_subnets else "",
                        "Gateway": "Y" if group["gateway"] else "",
                        "BGP Instance": group["id"] if group["gateway"] else "",
                        "System IPv4": system_ipv4,
                        "System IPv6": system_ipv6,
                        "Underlay Hosts": underlay_hosts,
                    }
                )

        return {"routers": results}

    def get_services(self) -> Dict[str, Any]:
        bds = self.get_bridge_domains().get("bridge_domains", [])
        for bd in bds:
            bd["Service Type"] = "Bridge Domain"
        rts = self.get_routers().get("routers", [])
        for rt in rts:
            rt["Service Type"] = "Router"
        return {"services": bds + rts}

