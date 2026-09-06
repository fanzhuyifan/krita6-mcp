"""Command-line entry point; diagnostics only read bridge state."""

from __future__ import annotations

import argparse
import json

from krita6_bridge.protocol import BridgeError
from krita6_mcp.bridge_client import BridgeClient


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="krita6-mcp")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("serve", help="Run the MCP stdio server (default)")
    doctor = subparsers.add_parser(
        "doctor", help="Read discovery and authenticated bridge diagnostics; changes no documents"
    )
    doctor.add_argument("--instance-id")
    doctor.add_argument("--state-dir", help="Override KRITA6_MCP_STATE_DIR")
    doctor.add_argument("--json", action="store_true", help="Print machine-readable diagnostics")
    args = parser.parse_args(argv)
    if args.command != "doctor":
        from krita6_mcp.server import main as serve

        serve()
        return 0
    client = BridgeClient(state_dir=args.state_dir)
    try:
        status = client.status(args.instance_id)
    except BridgeError as exc:
        status = {"available": False, "error": {"code": exc.code, "message": exc.message}}
    available = status.get("available", "instance_id" in status)
    if args.json:
        print(json.dumps(status, ensure_ascii=False, indent=2))
    elif available:
        instances = status.get("instances", [status])
        for item in instances:
            if item.get("reachable", True):
                print(
                    f"Krita bridge {item['instance_id']}: protocol {item.get('bridge_protocol')}, Krita {item.get('krita_version', 'unknown')}, plugin {item.get('plugin_version', 'unknown')}"
                )
                print(json.dumps(item.get("capabilities", {}), ensure_ascii=False))
    else:
        print("No reachable Krita 6 bridge. Start Krita with the enabled Python bridge plugin.")
        if status.get("error"):
            print(f"{status['error']['code']}: {status['error']['message']}")
    return 0 if available else 1


if __name__ == "__main__":
    raise SystemExit(main())
