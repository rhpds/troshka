"""GCP provider driver.

Provisions Compute Engine instances with nested virtualization,
manages GCP networking, Cloud DNS, static IPs, and Filestore.
"""

from app.services.providers.base import ProviderDriver


class GCPDriver(ProviderDriver):
    pass
