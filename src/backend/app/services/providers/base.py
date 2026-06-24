"""Abstract provider driver interface.

Each provider type (EC2, OCP Virt) implements this interface
to handle infrastructure-specific operations.
"""


class ProviderDriver:
    def provision_host(
        self, provider, host_id, instance_type, storage_size_gb, **kwargs
    ):
        """Provision a new host. Returns dict with:
        host_id, instance_id, instance_type, public_ip, private_ip,
        total_vcpus, total_ram_mb, private_key, key_pair_name,
        storage_size_gb, max_eips
        """
        raise NotImplementedError

    def terminate_host(self, provider, instance_id):
        """Terminate a host instance."""
        raise NotImplementedError

    def get_host_status(self, provider, instance_id):
        """Get current status. Returns dict with instance_id, state,
        public_ip, private_ip — or None if not found."""
        raise NotImplementedError

    def resize_host(self, provider, instance_id, new_instance_type):
        """Resize a host. Returns dict with new instance_type, total_vcpus, etc."""
        raise NotImplementedError

    def extend_host_storage(self, provider, host, db, increment_gb=None):
        """Extend host storage volume. Returns dict with old_size_gb, new_size_gb."""
        raise NotImplementedError

    def setup_console(self, provider, base_domain):
        """Set up console infrastructure for a provider. Returns dict with
        console_base_domain, console_zone_id, console_nameservers, etc."""
        raise NotImplementedError

    def create_console_record(self, provider, host, hostname, ip_address):
        """Create DNS/Route record for a host's console endpoint."""
        raise NotImplementedError

    def delete_console_record(self, provider, host, hostname, ip_address):
        """Delete DNS/Route record for a host's console endpoint."""
        raise NotImplementedError

    def delete_console(self, provider):
        """Remove all console infrastructure for a provider."""
        raise NotImplementedError

    def get_host_powerstate(self, provider, instance_id):
        """Get VM power state (running, stopped, etc.)."""
        raise NotImplementedError

    def start_host(self, provider, instance_id):
        """Start a stopped host."""
        raise NotImplementedError

    def stop_host(self, provider, instance_id):
        """Stop a running host."""
        raise NotImplementedError

    def delete_key_pair(self, provider, key_pair_name):
        """Delete an SSH key pair. No-op if provider doesn't manage key pairs."""
        pass

    def allocate_eip(self, provider, host, eip_id):
        """Allocate an external IP.
        Returns dict with keys: public_ip, allocation_id."""
        raise NotImplementedError

    def associate_eip(self, provider, host, allocation_id):
        """Associate an EIP with a host.
        Returns dict with optional keys: private_ip, association_id.
        Empty dict if no association step needed (e.g. OCP Virt)."""
        raise NotImplementedError

    def release_eip(self, provider, allocation_id, namespace=None):
        """Release an external IP and clean up infra resources.
        namespace is provider-specific context (k8s namespace for OCP Virt)."""
        raise NotImplementedError

    def update_eip_ports(self, provider, host, allocation_id, ports):
        """Update port mappings on an EIP infra resource.
        ports is a list of dicts: [{port, targetPort, name}].
        No-op for providers that don't need it."""
        pass

    def create_route_access(self, provider, host, project_id, vm_name, int_ip, port):
        """Create Route-based external access for a VM port (OCP Virt only).
        Returns dict with hostname, route_name, service_name."""
        raise NotImplementedError

    def delete_route_access(self, provider, project_id):
        """Delete all Route-based external access resources for a project."""
        pass
