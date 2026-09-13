# Network instance related methods extracted from srlinux.py
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
import jmespath

from ..records import BgpVpnInstance, NetworkInstance, Subinterface, as_int
from .down_reason import ParentReasons
from .helpers import as_list, bgp_evpn_evis, first_payload


def _route_targets(inst: Dict[str, Any], direction: str) -> Tuple[str, ...]:
    """The ``import`` or ``export`` route-targets of one bgp-vpn instance.

    Where a policy sets them instead of a target list, the policy's name is
    what there is to show.
    """
    policy = inst.get(f"{direction}-policy")
    if policy:
        return tuple(str(p) for p in as_list(policy))
    targets = []
    for rt in as_list((inst.get("route-target") or {}).get(f"{direction}-rt")):
        target = rt.get("target") if isinstance(rt, dict) else rt
        if target:
            targets.append(str(target).replace("target:", ""))
    return tuple(sorted(set(targets)))


def _bgp_vpn_instances(bgp_vpn: Dict[str, Any]) -> Tuple[BgpVpnInstance, ...]:
    """The bgp-vpn instances of a network-instance, each with its own targets."""
    return tuple(
        BgpVpnInstance(
            id=as_int(inst.get("id")) or index,
            import_rts=_route_targets(inst, "import"),
            export_rts=_route_targets(inst, "export"),
            rd=str((inst.get("route-distinguisher") or {}).get("rd") or ""),
        )
        for index, inst in enumerate(as_list(bgp_vpn.get("bgp-instance")), start=1)
        if isinstance(inst, dict)
    )


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
        subitf: Dict[str, Dict[str, Any]] = {}
        resp = self.get(paths=[SUBITF_PATH], datatype="all")
        for itf in as_list(first_payload(resp).get("interface")):
            for si in as_list(itf.get("subinterface")):
                subitf[f"{itf.get('name', '')}.{si.get('index', '')}"] = si

        resp = self.get(paths=[f"/network-instance[name={nw_instance}]"], datatype="all")
        ni_list = [
            ni for ni in as_list(first_payload(resp).get("network-instance")) if isinstance(ni, dict)
        ]
        # interface -> the network-instances it is bound to, which is how an
        # irb names the ip-vrf a mac-vrf routes into.
        bound: Dict[str, List[str]] = {}
        for ni in ni_list:
            for itf in as_list(ni.get("interface")):
                if isinstance(itf, dict) and itf.get("name"):
                    bound.setdefault(str(itf["name"]), []).append(str(ni.get("name", "")))

        records = []
        for ni in ni_list:
            name = str(ni.get("name", ""))
            protocols = ni.get("protocols") or {}
            bgp_vpn = protocols.get("bgp-vpn") or {}
            interfaces = []
            for itf in as_list(ni.get("interface")):
                if not isinstance(itf, dict):
                    continue
                itf_name = str(itf.get("name", ""))
                details = subitf.get(itf_name, {})
                interfaces.append(
                    Subinterface(
                        name=itf_name,
                        oper=str(details.get("oper-state") or itf.get("oper-state") or ""),
                        prefixes=tuple(
                            str(p) for p in jmespath.search('*.address[]."ip-prefix"', details) or []
                        ),
                        mtu=as_int(details.get("l2-mtu") if "l2-mtu" in details else details.get("ip-mtu")),
                        vlan=as_int(
                            jmespath.search('vlan.encap."single-tagged"."vlan-id"', details)
                        ),
                        associated=tuple(
                            other for other in bound.get(itf_name, []) if other != name
                        )
                        if itf_name.startswith("irb")
                        else (),
                    )
                )
            records.append(
                NetworkInstance(
                    name=name,
                    type=str(ni.get("type") or ""),
                    oper=str(ni.get("oper-state") or ""),
                    router_id=str((protocols.get("bgp") or {}).get("router-id") or ""),
                    overlays=tuple(
                        str(v.get("name", ""))
                        for v in as_list(ni.get("vxlan-interface"))
                        if isinstance(v, dict)
                    ),
                    # The EVI the service advertises with, which is also what a
                    # virtual ethernet-segment names to say which
                    # network-instance it serves.
                    evis=tuple(bgp_evpn_evis(ni).values()),
                    instances=_bgp_vpn_instances(bgp_vpn),
                    interfaces=tuple(interfaces),
                )
            )
        return {"nwi_itfs": records}

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
