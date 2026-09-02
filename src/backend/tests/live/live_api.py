"""Thin HTTP client + OCP status poller for the live-env harness."""

from __future__ import annotations

import time

import httpx


class LiveClient:
    def __init__(self, base_url: str, token: str | None = None, timeout: float = 30.0):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.base_url = base_url
        self.raw = httpx.Client(base_url=base_url, headers=headers, timeout=timeout)

    def get_json(self, path: str) -> dict:
        r = self.raw.get(path)
        r.raise_for_status()
        return r.json()

    def post_json(self, path: str, body: dict) -> httpx.Response:
        return self.raw.post(path, json=body)

    def delete(self, path: str) -> httpx.Response:
        return self.raw.delete(path)

    def status(self, pid: str) -> dict:
        return self.get_json(f"/api/v1/projects/{pid}")

    def close(self) -> None:
        self.raw.close()


def poll_ocp(
    client: LiveClient,
    pid: str,
    until: set[str],
    timeout_s: float,
    interval_s: float = 15.0,
) -> dict:
    """Poll GET /projects/{pid} until ocp_status is in `until`.

    Returns the final status dict. Raises AssertionError if ocp_status becomes
    "error" while "error" not in `until`; raises TimeoutError past timeout_s.
    """
    deadline = time.monotonic() + timeout_s
    last: dict = {}
    while True:
        last = client.status(pid)
        st = last.get("ocp_status")
        if st in until:
            return last
        if st == "error" and "error" not in until:
            raise AssertionError(
                f"ocp_status became 'error': {last.get('ocp_status_detail')}"
            )
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"ocp_status={st!r} not in {until} after {timeout_s}s "
                f"(detail={last.get('ocp_status_detail')})"
            )
        time.sleep(interval_s)
