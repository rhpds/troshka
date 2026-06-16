from app.services.providers.base import ProviderDriver


def get_provider_driver(provider) -> ProviderDriver:
    """Return the appropriate driver for a provider's type."""
    if provider.type == "ec2":
        from app.services.providers.ec2 import EC2Driver

        return EC2Driver()
    elif provider.type == "ocpvirt":
        from app.services.providers.ocpvirt import OCPVirtDriver

        return OCPVirtDriver()
    raise ValueError(f"Unknown provider type: {provider.type}")
