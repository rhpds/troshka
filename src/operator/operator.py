import kopf
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("troshka-operator")

CRD_GROUP = "troshka.redhat.com"
CRD_VERSION = "v1alpha1"


@kopf.on.startup()
def configure(settings: kopf.OperatorSettings, **_):
    settings.posting.level = logging.WARNING
    settings.persistence.finalizer = "troshka.redhat.com/finalizer"
    logger.info("Troshka operator starting")
