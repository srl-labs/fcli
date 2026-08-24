"""fcli server: live SR Linux report tables in the browser, fed by gNMI subscriptions."""

from .app import create_app, serve

__all__ = ["create_app", "serve"]
