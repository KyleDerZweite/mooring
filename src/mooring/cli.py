"""Stable, JSON-first interface for people, automation and AI agents."""

import argparse
import json
import sys

from . import __version__
from .common import Error
from .config import load
from .deployment import Deployment, in_window
from .immediate import immediate_update
from .updates import update


def main(argv=None):
    parser = argparse.ArgumentParser(description="Host-local Git-revision container deployments")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config", default="~/.config/mooring/host.json")
    parser.add_argument(
        "--json", action="store_true", help="JSON is always emitted; retained for explicit callers"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("services")
    commands.add_parser("run", help="Run enabled services within their host-configured windows")
    for command in ["plan", "apply", "status", "history", "recover", "update"]:
        sub = commands.add_parser(command)
        sub.add_argument("service")
        sub.add_argument("--json", action="store_true")
        if command == "apply":
            sub.add_argument("--automatic", action="store_true")
            sub.add_argument("--revision", help="Refuse if the branch is no longer at this commit")
        if command == "recover":
            sub.add_argument("--mode", choices=["rollback", "accept"], required=True)
        if command == "update":
            mode = sub.add_mutually_exclusive_group()
            mode.add_argument(
                "--commit", action="store_true", help="Publish a version-only Git commit; does not deploy"
            )
            mode.add_argument(
                "--now",
                action="store_true",
                help="Publish and deploy immediately, bypassing schedule and release age",
            )
            sub.add_argument(
                "--version", dest="target_version", help="Exact higher stable tag; requires --now"
            )
            sub.add_argument("--revision", help="Refuse if the branch advanced")
    args = parser.parse_args(argv)
    if args.command == "update" and args.target_version is not None and not args.now:
        parser.error("--version requires --now")
    try:
        cfg = load(args.config)
        if args.command == "services":
            data = [
                {
                    "name": name,
                    "runtime": item["runtime"],
                    "project": item["project"],
                    "automatic": item.get("automatic", False),
                }
                for name, item in cfg["services"].items()
            ]
        elif args.command == "run":
            data = []
            for name, item in cfg["services"].items():
                deployment = Deployment(cfg, name)
                if item.get("automatic") and in_window(item) and item.get("update", {}).get("enabled"):
                    try:
                        discovered = update(deployment, commit=True, automatic=True)
                        data.append({"service": name, "update": discovered})
                    except Error as error:
                        data.append(
                            {
                                "service": name,
                                "phase": "update",
                                "error": {
                                    "code": error.code,
                                    "message": str(error),
                                    "retryable": error.retryable,
                                },
                            }
                        )
                try:
                    data.append(deployment.apply(automatic=True))
                except Error as error:
                    data.append(
                        {
                            "service": name,
                            "phase": "apply",
                            "error": {
                                "code": error.code,
                                "message": str(error),
                                "retryable": error.retryable,
                            },
                        }
                    )
        else:
            deployment = Deployment(cfg, args.service)
            if args.command == "plan":
                data = deployment.plan()
            elif args.command == "apply":
                data = deployment.apply(automatic=args.automatic, expected_revision=args.revision)
            elif args.command == "status":
                data = deployment.status()
            elif args.command == "history":
                data = deployment.history()
            elif args.command == "recover":
                data = deployment.recover(args.mode)
            elif args.now:
                data = immediate_update(
                    deployment, version=args.target_version, expected_revision=args.revision
                )
            else:
                data = update(deployment, commit=args.commit, expected_revision=args.revision)
        success = not (args.command == "run" and any("error" in item for item in data))
        print(json.dumps({"schema": 1, "ok": success, "data": data}, sort_keys=True))
        return 0 if success else 1
    except Error as error:
        print(
            json.dumps(
                {
                    "schema": 1,
                    "ok": False,
                    "error": {"code": error.code, "message": str(error), "retryable": error.retryable},
                    **({"data": error.data} if hasattr(error, "data") else {}),
                }
            )
        )
        return 75 if error.retryable else 1
    except Exception:  # noqa: BLE001 - sanitize unexpected errors at the public CLI
        print(
            json.dumps(
                {
                    "schema": 1,
                    "ok": False,
                    "error": {
                        "code": "internal_error",
                        "message": "Unexpected failure; no unfiltered subprocess output is exposed",
                        "retryable": False,
                    },
                }
            )
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
