try:
    from ._version import __version__
except ImportError:
    try:
        from importlib.metadata import version
        __version__ = version("nornir-srl")
    except Exception:
        __version__ = "0.0.0.dev0+unknown"

# Printed to stderr on every run, so it never mixes into json/yaml/csv output
# or the MCP stdio stream.
DEPRECATION_NOTICE = (
    "fcli is deprecated and no longer maintained. It is replaced by fcling: "
    "uv tool install fcling  (https://github.com/srl-labs/fcling)"
)
