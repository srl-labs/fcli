# Network instance related methods extracted from srlinux.py
from __future__ import annotations

from typing import Any, Dict, List, Optional
import jmespath

from .down_reason import ParentReasons
from .helpers import as_list, first_payload


class NetworkInstanceMixin:
    """Mixin providing network-instance related getters."""

    def get(
        self,
        paths: List[str],
        datatype: Optional[str] = "config",
        strip_mod: Optional[bool] = True,
    ) -> List[Dict[str, Any]]:
        """Placeholder method implemented in :class:`SrLinux`."""
        raise NotImplementedError

    def get_nwi_itf(self, nw_instance: str = "*") -> Dict[str, Any]:
        SUBITF_PATH = "/interface[name=*]/subinterface"
        path_spec = {
            "path": f"/network-instance[name={nw_instance}]",
            "jmespath": '"network-instance"[].{NI:name,oper:"oper-state",type:type,"router-id":protocols.bgp."router-id",\
                    "vxlan-itf":"vxlan-interface"[].name || `[]` | join(\', \',@), \
                    "In-RT":"In-RT", "Out-RT":"Out-RT",\
                    itfs: interface[].{Subitf:name,"assoc-ni":"_other_ni","if-oper":"oper-state", "ip-prefix":*.address[]."ip-prefix",\
                        vlan:vlan.encap."single-tagged"."vlan-id", "mtu":"_mtu"}}',
            "datatype": "all",
        }
        subitf: Dict[str, Any] = {}
        resp = self.get(paths=[SUBITF_PATH], datatype="all")
        for itf in as_list(first_payload(resp).get("interface")):
            for si in as_list(itf.get("subinterface")):
                subif_name = f"{itf.get('name', '')}.{si.get('index', '')}"
                # A copy, because these details are merged into the interfaces of
                # every network-instance that binds them and the response itself
                # may be a cached one the streaming server hands out repeatedly.
                details = {k: v for k, v in si.items() if k != "index"}
                details["_mtu"] = (
                    si.get("l2-mtu") if "l2-mtu" in si else si.get("ip-mtu", "")
                )
                subitf[subif_name] = details

        resp = self.get(
            paths=[path_spec.get("path", "")], datatype=path_spec["datatype"]
        )
        ni_list = as_list(first_payload(resp).get("network-instance"))
        for ni in ni_list:
            bgp_vpn = ni.get("protocols", {}).get("bgp-vpn", {})
            in_rts = []
            out_rts = []
            bgp_instances = bgp_vpn.get("bgp-instance", [])
            if isinstance(bgp_instances, dict):
                bgp_instances = [bgp_instances]

            for inst in bgp_instances:
                rt_cfg = inst.get("route-target", {})

                if inst.get("import-policy"):
                    imp_pol = inst.get("import-policy")
                    if isinstance(imp_pol, str):
                        imp_pol = [imp_pol]
                    in_rts.extend(imp_pol)
                else:
                    import_rt = rt_cfg.get("import-rt", [])
                    if isinstance(import_rt, (str, dict)):
                        import_rt = [import_rt]
                    for rt in import_rt:
                        target = rt.get("target") if isinstance(rt, dict) else rt
                        if target:
                            in_rts.append(target.replace("target:", ""))

                if inst.get("export-policy"):
                    exp_pol = inst.get("export-policy")
                    if isinstance(exp_pol, str):
                        exp_pol = [exp_pol]
                    out_rts.extend(exp_pol)
                else:
                    export_rt = rt_cfg.get("export-rt", [])
                    if isinstance(export_rt, (str, dict)):
                        export_rt = [export_rt]
                    for rt in export_rt:
                        target = rt.get("target") if isinstance(rt, dict) else rt
                        if target:
                            out_rts.append(target.replace("target:", ""))
            ni["In-RT"] = ", ".join(sorted(list(set(in_rts))))
            ni["Out-RT"] = ", ".join(sorted(list(set(out_rts))))

            for ni_itf in ni.get("interface", []):
                ni_itf.update(subitf.get(ni_itf["name"], {}))
                if ni_itf["name"].startswith("irb"):
                    ni_itf["_other_ni"] = " ".join(
                        f"{vrf['name']}"
                        for vrf in ni_list
                        if ni_itf["name"]
                        in [i["name"] for i in vrf.get("interface", [])]
                        and vrf["name"] != ni["name"]
                    )

        # The projection runs against the normalized list, so a lone
        # network-instance that gNMI returned unwrapped still yields a row.
        res = jmespath.search(path_spec["jmespath"], {"network-instance": ni_list})
        return {"nwi_itfs": res}

    def get_lag(self, lag_id: str = "*") -> Dict[str, Any]:
        path_spec = {
            "path": f"/interface[name=lag{lag_id}]",
            "jmespath": '"interface"[].{lag:name, oper:"oper-state",mtu:mtu,"min":lag."min-links",desc:description, type:lag."lag-type", speed:lag."lag-speed","stby-sig":ethernet."standby-signaling",\
                  "lacp-key":lag.lacp."admin-key","lacp-itvl":lag.lacp.interval,"lacp-mode":lag.lacp."lacp-mode","lacp-sysid":lag.lacp."system-id-mac","lacp-prio":lag.lacp."system-priority",\
                    members:lag.member[].{"member-itf":name, "member-oper":"oper-state","act":lacp."activity"}}',
            "datatype": "all",
        }
        resp = self.get(
            paths=[path_spec.get("path", "")], datatype=path_spec["datatype"]
        )
        lags = as_list(first_payload(resp).get("interface"))
        for itf in lags:
            for member in as_list(itf.get("lag", {}).get("member")):
                member["name"] = str(member.get("name", "")).replace("ethernet", "et")
        res = jmespath.search(path_spec["jmespath"], {"interface": lags})
        return {"lag": res}

    def get_sum_subitf(self, interface: str = "*") -> Dict[str, Any]:
        path_spec = {
            "path": f"/interface[name={interface}]/subinterface",
            "datatype": "all",
        }
        resp = self.get(
            paths=[path_spec.get("path", "")], datatype=path_spec["datatype"]
        )

        # resp[0] is usually a dict like {'interface[name=...]': {...}} or {'interface': [...]}
        itf_list = []
        if resp and isinstance(resp[0], dict):
            for k, v in resp[0].items():
                if k.startswith("interface"):
                    if isinstance(v, list):
                        itf_list.extend(v)
                    elif isinstance(v, dict):
                        # For specific interface name, v is {'subinterface': [...]}
                        # and we might need to add back the name if it's missing from the dict
                        if "name" not in v:
                            if "[" in k and "]" in k:
                                v["name"] = k.split("[name=")[1].split("]")[0]
                        itf_list.append(v)

        # A subinterface reports itself 'port-down' without saying what is wrong
        # with the port, so the reason worth showing lives one level up.
        parents = ParentReasons(self.get)

        results = []
        for itf in itf_list:
            itf_name = itf.get("name", "")
            subitfs = []
            for si in itf.get("subinterface", []):
                # Construct proper subinterface name
                index = si.get("index", "")
                si_name = si.get("name", "")
                if not si_name:
                    si_name = f"{itf_name}.{index}"
                elif str(si_name).isdigit():
                    si_name = f"{itf_name}.{si_name}"

                # A port held in standby by its ethernet-segment is down on
                # purpose, so its subinterfaces are called what they are rather
                # than counted as faults.
                own_reason = si.get("oper-down-reason")
                oper = parents.state(si.get("oper-state"), si_name, own_reason)

                # Extract interesting fields
                sub_data = {
                    "Subitf": si_name,
                    "type": si.get("type"),
                    "admin": si.get("admin-state"),
                    "oper": oper,
                    "down-reason": (
                        "" if oper == "up" else parents.resolve(si_name, own_reason)
                    ),
                    "ip-mtu": si.get("ip-mtu"),
                    "vlan": jmespath.search('vlan.encap."single-tagged"."vlan-id"', si),
                }

                # IPv4 details
                ipv4 = si.get("ipv4")
                if ipv4:
                    sub_data["ipv4"] = [
                        addr.get("ip-prefix") for addr in ipv4.get("address", [])
                    ]

                # IPv6 details
                ipv6 = si.get("ipv6")
                if ipv6:
                    sub_data["ipv6"] = [
                        addr.get("ip-prefix") for addr in ipv6.get("address", [])
                    ]

                subitfs.append(sub_data)

            results.append({"Itf": itf_name, "subitfs": subitfs})

        return {"subinterface": results}
