import logging
import threading
import time

logger = logging.getLogger(__name__)

_INTERVAL_SECONDS = 3600  # 1 hour


def _check_workload_pruning() -> dict:
    """Check and prune old WorkloadRun records."""
    from app.core.config import config
    from app.core.database import SessionLocal
    from app.services.workloads.run_service import prune_workload_runs

    result = {"pruned": 0}

    s = SessionLocal()
    try:
        retention_days = config.workloads.run_retention_days
        count = prune_workload_runs(s, retention_days=retention_days)
        result["pruned"] = count
    except Exception:
        logger.exception("Workload pruning check error")
        s.rollback()
    finally:
        s.close()

    return result


def _timer_loop():
    logger.info("Workload pruning timer started (interval=%ds)", _INTERVAL_SECONDS)
    while True:
        time.sleep(_INTERVAL_SECONDS)
        try:
            _check_workload_pruning()
        except Exception:
            logger.exception("Workload pruning timer loop error")


def start_workload_timer():
    thread = threading.Thread(target=_timer_loop, daemon=True, name="workload-timer")
    thread.start()
    return thread
