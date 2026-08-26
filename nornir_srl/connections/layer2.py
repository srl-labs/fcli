from __future__ import annotations

from typing import Any, Dict, List, Optional
import jmespath

from .routing import _gnmi_path_missing, _suppress_pygnmi_client_logging


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

    def get_lldp_sum(self, interface: Optional[str] = "*") -> Dict[str, Any]:
        path_spec = {
            "path": f"/system/lldp/interface[name={interface}]/neighbor",
            "jmespath": '"system/lldp".interface[].{interface:name, Neighbors:neighbor[].{"Nbr-port":"port-id","Nbr-System":"system-name", "Nbr-port-desc":"port-description"}}',
            "datatype": "state",
        }
        resp = self.get(
            paths=[path_spec.get("path", "")], datatype=path_spec["datatype"]
        )
        res = jmespath.search(path_spec["jmespath"], resp[0])
        return {"lldp_nbrs": res}

    def get_mac_table(self, network_instance: Optional[str] = "*") -> Dict[str, Any]:
        path_spec = {
            "path": f"/network-instance[name={network_instance}]/bridge-table/mac-table/mac",
            "jmespath": '"network-instance"[].{"NI":name, Fib:"bridge-table"."mac-table".mac[].{Address:address, Dest:destination, Type:type}}',
            "datatype": "state",
        }
        if (
            "bridged"
            not in self.get(paths=["/system/features"], datatype="state")[0][
                "system/features"
            ]
        ):
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
        res = jmespath.search(path_spec["jmespath"], resp[0])
        return {"mac_table": res}

    def get_es(self) -> Dict[str, Any]:
        path_spec = {
            "path": f"/system/network-instance/protocols/evpn/ethernet-segments",
            "jmespath": '"system/network-instance/protocols/evpn/ethernet-segments"."bgp-instance"[]."ethernet-segment"[].{name:name, esi:esi, type:type, "mh-mode":"multi-homing-mode", oper:"oper-state", "itf/nh":"_itf_or_nh", "ni-peers":association."network-instance"[]."_ni_peers"|join(\', \',@) }',
            "datatype": "all",
        }

        def set_es_fields(resp: List[Dict[str, Any]]) -> None:
            for bgp_inst in (
                resp[0]
                .get("system/network-instance/protocols/evpn/ethernet-segments", {})
                .get("bgp-instance", [])
            ):
                for es in bgp_inst.get("ethernet-segment", []):
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
                        es_peers = (
                            vrf["bgp-instance"][0]
                            .get("computed-designated-forwarder-candidates", {})
                            .get("designated-forwarder-candidate", [])
                        )
                        vrf["_peers"] = " ".join(
                            (
                                f"{peer['address']}(DF)"
                                if peer["designated-forwarder"]
                                else peer["address"]
                            )
                            for peer in es_peers
                        )
                        vrf["_ni_peers"] = f"{vrf['name']}:[{vrf['_peers']}]"

        if (
            "evpn"
            not in self.get(paths=["/system/features"], datatype="state")[0][
                "system/features"
            ]
        ):
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
        res = jmespath.search(path_spec["jmespath"], resp[0])
        return {"es": res}

    def get_es_dest(self) -> Dict[str, Any]:
        path_spec = {
            "path": "/tunnel-interface[name=*]/vxlan-interface/bridge-table/unicast-destinations/es-destination",
            "jmespath": '"tunnel-interface"[].{tunnel:name, "es-dest":"vxlan-interface"[]."bridge-table"."unicast-destinations"."es-destination"[].{esi:esi, vteps:"_vteps"}}',
            "datatype": "state",
        }

        def set_vtep_fields(resp: List[Dict[str, Any]]) -> None:
            for tun in resp[0].get("tunnel-interface", []):
                for vxlan in tun.get("vxlan-interface", []):
                    bt = vxlan.get("bridge-table", {})
                    ucast = bt.get("unicast-destinations", {})
                    for es_dest in ucast.get("es-destination", []):
                        vteps = es_dest.get("vtep", [])
                        es_dest["_vteps"] = " ".join(
                            v.get("address", "") for v in vteps
                        )

        if (
            "bridged"
            not in self.get(paths=["/system/features"], datatype="state")[0][
                "system/features"
            ]
        ):
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
        res = jmespath.search(path_spec["jmespath"], resp[0])
        return {"es_dest": res}

    def get_vxlan(self) -> Dict[str, Any]:
        path_spec = {
            "path": "/tunnel-interface[name=*]/vxlan-interface",
            "jmespath": '"tunnel-interface"[]."_vxlan_itfs"[].{"vxlan-itf":name, NI:ni, "ing-vni":"ing-vni", destinations:destinations}',
            "datatype": "state",
        }

        def set_vxlan_fields(
            resp: List[Dict[str, Any]],
            ni_map: Dict[str, str],
        ) -> None:
            for tun in resp[0].get("tunnel-interface", []):
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

        if (
            "bridged"
            not in self.get(paths=["/system/features"], datatype="state")[0][
                "system/features"
            ]
        ):
            return {"vxlan": []}

        # build vxlan-interface to network-instance map
        ni_resp = self.get(paths=["/network-instance[name=*]"], datatype="config")
        ni_map: Dict[str, str] = {}
        for ni in ni_resp[0].get("network-instance", []):
            for vxlan_itf in ni.get("vxlan-interface", []):
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
        res = jmespath.search(path_spec["jmespath"], resp[0])
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
        for ni in ni_itfs[0].get("network-instance", []):
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

        for itf in resp[0].get("interface", []):
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

        res = jmespath.search(path_spec["jmespath"], resp[0])
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

        if not resp or not isinstance(resp[0], dict):
            return {"bridge_domains": []}

        ni_list = resp[0].get("network-instance", [])

        # Probe subinterface details for IRB IPv4/IPv6, anycast-gw, & VLAN encap
        subitf_details: Dict[str, Dict[str, Any]] = {}
        with _suppress_pygnmi_client_logging():
            try:
                sub_resp = self.get(paths=["/interface[name=*]/subinterface"], datatype="all")
                if sub_resp and isinstance(sub_resp[0], dict):
                    itf_list = sub_resp[0].get("interface", [])
                    if isinstance(itf_list, dict):
                        itf_list = [itf_list]
                    for itf in itf_list:
                        if not isinstance(itf, dict):
                            continue
                        itf_name = itf.get("name", "")
                        for si in itf.get("subinterface", []):
                            if not isinstance(si, dict):
                                continue
                            index = si.get("index", "")
                            si_name = si.get("name", "")
                            if not si_name:
                                si_name = f"{itf_name}.{index}"
                            elif str(si_name).isdigit():
                                si_name = f"{itf_name}.{si_name}"
                            subitf_details[si_name] = si
            except BaseException as e:
                if not _gnmi_path_missing(e):
                    pass

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

        def _to_subnet(pfx: str) -> str:
            try:
                import ipaddress
                net = ipaddress.ip_network(pfx, strict=False)
                return str(net)
            except Exception:
                return pfx

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

        def _format_irb_item_and_subnets(itf_name: str, itf_dict: Dict[str, Any], assoc_vrfs: List[str]) -> Tuple[str, List[str]]:
            si = subitf_details.get(itf_name, {})
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

            subnets = [_to_subnet(pfx) for pfx in ips]

            ip_part = f": {', '.join(ips)}" if ips else ""
            gw_part = f" (anycast-gw: {'true' if is_anycast else 'false'})"
            vrf_part = f" -> {', '.join(assoc_vrfs)}" if assoc_vrfs else ""

            return f"{itf_name}{ip_part}{gw_part}{vrf_part}", subnets

        for ni in ni_list:
            if not isinstance(ni, dict):
                continue
            ni_name = ni.get("name", "")
            ni_type = ni.get("type", "")
            if ni_type != "mac-vrf":
                continue
            oper_state = ni.get("oper-state", "unknown")

            bgp_vpn = ni.get("protocols", {}).get("bgp-vpn", {})
            bgp_instances = bgp_vpn.get("bgp-instance", [])
            if isinstance(bgp_instances, dict):
                bgp_instances = [bgp_instances]

            rts = set()
            for inst in bgp_instances:
                if not isinstance(inst, dict):
                    continue
                rt_cfg = inst.get("route-target", {})
                if isinstance(rt_cfg, dict):
                    for key in ("import-rt", "export-rt"):
                        rts_raw = rt_cfg.get(key, [])
                        if isinstance(rts_raw, (str, dict)):
                            rts_raw = [rts_raw]
                        for item in rts_raw:
                            target = item.get("target") if isinstance(item, dict) else item
                            if target:
                                target_str = str(target)
                                if not target_str.startswith("target:"):
                                    target_str = f"target:{target_str}"
                                rts.add(target_str)

            rt_list = sorted(list(rts))
            rt_display = ", ".join(rt_list) if rt_list else f"mac-vrf:{ni_name}"

            irb_subitfs = []
            bridge_subitfs = []
            all_subnets = []
            for i in ni.get("interface", []):
                if isinstance(i, dict) and i.get("name"):
                    name = i["name"]
                    if name.startswith("irb"):
                        assoc_vrfs = irb_to_ip_vrf.get(name, [])
                        irb_item_str, subnets = _format_irb_item_and_subnets(name, i, assoc_vrfs)
                        irb_subitfs.append(irb_item_str)
                        all_subnets.extend(subnets)
                    else:
                        vlan_info = _extract_vlan_encap(name, i)
                        bridge_subitfs.append(f"{name} (VLAN: {vlan_info})")

            vxlan_itfs = [
                v.get("name", "")
                for v in ni.get("vxlan-interface", [])
                if isinstance(v, dict) and v.get("name")
            ]

            primary_bd = rt_list[0] if rt_list else f"mac-vrf:{ni_name}"
            results.append(
                {
                    "Bridge Domain": primary_bd,
                    "MAC-VRF": ni_name,
                    "Oper State": oper_state,
                    "Route Targets": rt_display,
                    "Subnets": ", ".join(all_subnets) if all_subnets else "",
                    "IRB Interface": ", ".join(irb_subitfs) if irb_subitfs else "-",
                    "Sub-Interfaces": ", ".join(bridge_subitfs) if bridge_subitfs else "-",
                    "VXLAN Interface": ", ".join(vxlan_itfs) if vxlan_itfs else "-",
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

        if not resp or not isinstance(resp[0], dict):
            return {"routers": []}

        ni_list = resp[0].get("network-instance", [])
        if isinstance(ni_list, dict):
            ni_list = [ni_list]

        # Probe subinterface details for IPv4/IPv6 addresses
        subitf_details: Dict[str, Dict[str, Any]] = {}
        with _suppress_pygnmi_client_logging():
            try:
                sub_resp = self.get(paths=["/interface[name=*]/subinterface"], datatype="all")
                if sub_resp and isinstance(sub_resp[0], dict):
                    itf_list = sub_resp[0].get("interface", [])
                    if isinstance(itf_list, dict):
                        itf_list = [itf_list]
                    for itf in itf_list:
                        if not isinstance(itf, dict):
                            continue
                        itf_name = itf.get("name", "")
                        for si in itf.get("subinterface", []):
                            if not isinstance(si, dict):
                                continue
                            index = si.get("index", "")
                            si_name = si.get("name", "")
                            if not si_name:
                                si_name = f"{itf_name}.{index}"
                            elif str(si_name).isdigit():
                                si_name = f"{itf_name}.{si_name}"
                            subitf_details[si_name] = si
            except BaseException as e:
                if not _gnmi_path_missing(e):
                    pass

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
                for addr in ip_cfg.get("address", []):
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

            bgp_vpn = ni.get("protocols", {}).get("bgp-vpn", {})
            bgp_instances = bgp_vpn.get("bgp-instance", [])
            if isinstance(bgp_instances, dict):
                bgp_instances = [bgp_instances]

            rts = set()
            for inst in bgp_instances:
                if not isinstance(inst, dict):
                    continue
                rt_cfg = inst.get("route-target", {})
                if isinstance(rt_cfg, dict):
                    for key in ("import-rt", "export-rt"):
                        rts_raw = rt_cfg.get(key, [])
                        if isinstance(rts_raw, (str, dict)):
                            rts_raw = [rts_raw]
                        for item in rts_raw:
                            target = item.get("target") if isinstance(item, dict) else item
                            if target:
                                target_str = str(target)
                                if not target_str.startswith("target:"):
                                    target_str = f"target:{target_str}"
                                rts.add(target_str)

            rt_list = sorted(list(rts))
            rt_display = ", ".join(rt_list) if rt_list else "none (isolated)"

            mac_vrfs_items = []
            routed_itfs_items = []

            for i in ni.get("interface", []):
                if isinstance(i, dict) and i.get("name"):
                    name = i["name"]
                    ips = _get_ip_addresses(name, i)
                    ip_str = ", ".join(ips) if ips else ""

                    if name.startswith("irb"):
                        mac_name = irb_to_mac_vrf.get(name, "unknown")
                        if ip_str:
                            mac_vrfs_items.append(f"{mac_name} (IRB-interface: {ip_str})")
                        else:
                            mac_vrfs_items.append(f"{mac_name} ( )")
                    else:
                        if ip_str:
                            routed_itfs_items.append(f"{name} ({ip_str})")
                        else:
                            routed_itfs_items.append(f"{name} ()")

            vxlan_itfs = [
                v.get("name", "")
                for v in ni.get("vxlan-interface", [])
                if isinstance(v, dict) and v.get("name")
            ]

            primary_router = rt_list[0] if rt_list else f"none (isolated) - {ni_name}"
            results.append(
                {
                    "Router": primary_router,
                    "IP-VRF": ni_name,
                    "Oper State": oper_state,
                    "Route Targets": rt_display,
                    "MAC-VRFs": ", ".join(mac_vrfs_items) if mac_vrfs_items else "-",
                    "Routed Interfaces": ", ".join(routed_itfs_items) if routed_itfs_items else "-",
                    "VXLAN Interface": ", ".join(vxlan_itfs) if vxlan_itfs else "-",
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

