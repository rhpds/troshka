"""One serialized, stop-aware maintenance loop per operator process."""
import logging
import threading
import time

import kopf
from helpers.collector_transport import KubernetesTransport, Rejected, reconcile

logger = logging.getLogger("troshka-operator")
_stop = threading.Event()
_thread = None


def refresh_loop():
    while not _stop.is_set():
        api = None
        try:
            api = KubernetesTransport()
            try:
                result = reconcile(api)
            except Rejected as exc:
                result = {"ready": False, "reason": str(exc)}
            except Exception:  # noqa: BLE001 -- API errors can contain credentials
                result = {"ready": False, "reason": "verification_failed"}
            api.status({**result, "observed_at": time.time()})
        except Exception:  # noqa: BLE001 -- keep the loop alive without exposing credentials
            # API errors can contain credentials; never emit exception details.
            logger.warning("Collector maintenance status unavailable")
        finally:
            if api is not None:
                api.close()
        _stop.wait(300)


@kopf.on.startup()
def start_collector_transport(**_):
    global _thread
    if _thread is None or not _thread.is_alive():
        _stop.clear()
        _thread = threading.Thread(target=refresh_loop, daemon=True, name="collector-transport")
        _thread.start()


@kopf.on.cleanup()
def stop_collector_transport(**_):
    _stop.set()
