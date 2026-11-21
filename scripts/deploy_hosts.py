"""Resolve gitignored deploy-hosts.local.yaml for deploy-full.sh."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

DEFAULT_RELATIVE = Path("scripts/deploy-hosts.local.yaml")
OPERATOR_ENV = "OPERATOR_KUBECONFIGS"


def _expand(path: str) -> Path:
    return Path(os.path.expanduser(path)).resolve()


def _kubeconfig_path(entry: str, *, kubeconfig_dir: Path, domain_suffix: str) -> Path:
    raw = (entry or "").strip()
    if not raw:
        raise ValueError("empty host entry")
    if raw.startswith("/") or raw.startswith("~"):
        path = _expand(raw)
    elif raw.endswith(".kubeconfig"):
        path = kubeconfig_dir / raw
    elif domain_suffix and not raw.endswith(domain_suffix):
        path = kubeconfig_dir / f"{raw}.{domain_suffix}.kubeconfig"
    else:
        path = kubeconfig_dir / f"{raw}.kubeconfig"
    return path


def load_hosts(path: Path) -> dict:
    if yaml is None:
        raise RuntimeError("PyYAML is required to load deploy hosts")
    if not path.is_file():
        raise FileNotFoundError(
            f"Missing {path}. Copy scripts/deploy-hosts.local.yaml.example "
            "to scripts/deploy-hosts.local.yaml and set your clusters."
        )
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise ValueError("deploy hosts file must be a mapping")
    return data


def resolve_hosts(path: Path) -> dict[str, object]:
    data = load_hosts(path)
    kubeconfig_dir = _expand(str(data.get("kubeconfig_dir") or "~/secrets"))
    domain_suffix = str(data.get("domain_suffix") or "").strip().lstrip(".")
    production = data.get("production")
    dev = data.get("dev")
    operators = data.get("operators") or []
    if not production or not dev:
        raise ValueError("production and dev host entries are required")
    if not isinstance(operators, list) or not operators:
        raise ValueError("operators must be a non-empty list")
    return {
        "production": str(
            _kubeconfig_path(
                str(production),
                kubeconfig_dir=kubeconfig_dir,
                domain_suffix=domain_suffix,
            )
        ),
        "dev": str(
            _kubeconfig_path(
                str(dev), kubeconfig_dir=kubeconfig_dir, domain_suffix=domain_suffix
            )
        ),
        "operators": [
            str(
                _kubeconfig_path(
                    str(entry),
                    kubeconfig_dir=kubeconfig_dir,
                    domain_suffix=domain_suffix,
                )
            )
            for entry in operators
        ],
    }


def emit_shell(resolved: dict[str, object]) -> None:
    print(f"KC={_shell_quote(resolved['production'])}")
    print(f"DEV_KC={_shell_quote(resolved['dev'])}")
    print(f"{OPERATOR_ENV}=(")
    for path in resolved["operators"]:
        print(f"  {_shell_quote(path)}")
    print(")")


def _shell_quote(value: object) -> str:
    text = str(value)
    return "'" + text.replace("'", "'\"'\"'") + "'"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--file",
        type=Path,
        default=None,
        help="Path to deploy-hosts.local.yaml (default: <repo>/scripts/...)",
    )
    parser.add_argument(
        "action",
        choices=("shell", "json"),
        help="shell: export KC/DEV_KC/OPERATOR_KUBECONFIGS; json: print resolved paths",
    )
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    path = args.file or (root / DEFAULT_RELATIVE)
    resolved = resolve_hosts(path)
    if args.action == "shell":
        emit_shell(resolved)
    else:
        import json

        print(json.dumps(resolved))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"deploy hosts: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
