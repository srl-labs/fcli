from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..records import SystemInfo
from .helpers import first_payload


def _container(resp: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The leaves of the one container a Get for it answers with.

    The payload is keyed by the path as the device echoes it - ``platform/chassis``
    or ``platform/control[slot=A]`` - so the value is taken whatever the key.
    """
    for value in first_payload(resp).values():
        if isinstance(value, dict):
            return value
    return {}


class SystemMixin:
    """Mixin providing system related getters."""

    def get(
        self,
        paths: List[str],
        datatype: Optional[str] = "config",
        strip_mod: Optional[bool] = True,
    ) -> List[Dict[str, Any]]:
        """Placeholder method implemented in :class:`SrLinux`."""
        raise NotImplementedError

    def get_info(self) -> Dict[str, Any]:
        """Return system information such as chassis and software details."""
        chassis = _container(self.get(paths=["/platform/chassis"], datatype="state"))
        control = _container(self.get(paths=["/platform/control[slot=A]"], datatype="state"))
        # The release alone: SR Linux reports ``v26.7.1-554-g78ed635f70a``.
        version = str(control.get("software-version") or "").split("-")[0].lstrip("v")
        info = SystemInfo(
            type=str(chassis.get("type") or ""),
            serial_number=str(chassis.get("serial-number") or ""),
            part_number=str(chassis.get("part-number") or ""),
            hw_mac_address=str(chassis.get("hw-mac-address") or ""),
            last_booted=str(chassis.get("last-booted") or ""),
            software_version=version,
        )
        return {"sys_info": [info]}
