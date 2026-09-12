import os
from typing import Any

from dynaconf import Dynaconf

_config_dir = os.path.join(os.path.dirname(__file__), "..", "..", "config")

# Annotated ``Any`` so Pyright resolves the export and Dynaconf's dynamic
# attribute access (``config.redis``, ``config.workloads`` …). Dynaconf ships no
# usable type info, so without this some Pyright/typeshed setups report
# ``config`` as an "unknown import symbol" and flag every ``config.<attr>`` —
# tripping the backend pre-commit hook (``pyright app/``). Runtime is unchanged.
config: Any = Dynaconf(
    envvar_prefix="TROSHKA",
    settings_files=[
        os.path.join(_config_dir, "config.yaml"),
        os.path.join(_config_dir, "config.local.yaml"),
    ],
    environments=False,
    load_dotenv=False,
    merge_enabled=True,
)
