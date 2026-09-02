"""Env-var-driven configuration + skip decisions for the live-env harness."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class LiveConfig:
    url: str | None
    token: str | None
    troshkad_host: str | None
    kubeconfig: str | None
    kubevirt_host: str | None
    tier2_enabled: bool
    timeout_s: int

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> LiveConfig:
        src: Mapping[str, str] = os.environ if env is None else env

        def val(key: str) -> str | None:
            return src.get(key) or None

        return cls(
            url=val("TROSHKA_LIVE_URL"),
            token=val("TROSHKA_LIVE_TOKEN"),
            troshkad_host=val("TROSHKA_LIVE_TROSHKAD_HOST"),
            kubeconfig=val("TROSHKA_LIVE_KUBECONFIG"),
            kubevirt_host=val("TROSHKA_LIVE_KUBEVIRT_HOST"),
            tier2_enabled=src.get("TROSHKA_LIVE_TIER2") == "1",
            timeout_s=int(src.get("TROSHKA_LIVE_TIMEOUT_S") or "4200"),
        )

    @property
    def configured(self) -> bool:
        return bool(self.url)

    @property
    def troshkad_ready(self) -> bool:
        return bool(self.troshkad_host)

    @property
    def kubevirt_ready(self) -> bool:
        return bool(self.kubeconfig and self.kubevirt_host)
