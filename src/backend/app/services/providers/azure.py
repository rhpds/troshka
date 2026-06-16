"""Azure provider driver.

Provisions Azure VMs with nested virtualization,
manages VNet/NSG networking, Azure DNS, public IPs, and Azure Files NFS.
"""

from app.services.providers.base import ProviderDriver


class AzureDriver(ProviderDriver):
    pass
