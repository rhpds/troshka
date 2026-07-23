import kopf
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("troshka-operator")

CRD_GROUP = "troshka.redhat.com"
CRD_VERSION = "v1alpha1"

import handlers.network  # noqa: F401,E402
import handlers.project  # noqa: F401,E402
import handlers.vm  # noqa: F401,E402
import handlers.container  # noqa: F401,E402


@kopf.on.startup()
def configure(settings: kopf.OperatorSettings, **_):
    settings.posting.level = logging.WARNING
    settings.persistence.finalizer = "troshka.redhat.com/finalizer"
    settings.execution.max_workers = 100
    settings.batching.batch_window = 0.5
    logger.info("Troshka operator starting (max_workers=100)")
