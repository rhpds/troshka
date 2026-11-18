"""Raw events never install a namespace finalizer or require a live project CR."""

import kopf
from helpers.managed_power import PowerClient
from helpers.native_namespace_cleanup import ANNOTATION, acknowledge


@kopf.on.event("", "v1", "namespaces")
async def namespace_cleanup_event(body, **_):
    meta = body.get("metadata", {})
    if ANNOTATION in meta.get("annotations", {}):
        await acknowledge(PowerClient(), meta["name"])
