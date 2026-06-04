import os
from dynaconf import Dynaconf

_config_dir = os.path.join(os.path.dirname(__file__), "..", "..", "config")

config = Dynaconf(
    envvar_prefix="TROSHKA",
    settings_files=[
        os.path.join(_config_dir, "config.yaml"),
        os.path.join(_config_dir, "config.local.yaml"),
    ],
    environments=False,
    load_dotenv=False,
    merge_enabled=True,
)
