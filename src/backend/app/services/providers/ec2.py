"""EC2 provider driver.

Delegates to the existing provisioner.py functions. This keeps the
refactor safe — all EC2 behavior is unchanged, just routed through
the driver interface.
"""

from app.services.providers.base import ProviderDriver


class EC2Driver(ProviderDriver):
    def provision_host(
        self, provider, host_id, instance_type, storage_size_gb, **kwargs
    ):
        from app.services.provisioner import provision_host

        creds = provider.get_credentials()
        return provision_host(
            instance_type=instance_type,
            host_id=host_id,
            region=kwargs.get("region") or provider.default_region,
            credentials=creds,
            storage_size_gb=storage_size_gb,
            ami_id=kwargs.get("ami_id") or provider.default_ami,
            vpc_id=kwargs.get("vpc_id") or provider.vpc_id,
            subnet_id=kwargs.get("subnet_id") or provider.subnet_id,
            security_group_id=kwargs.get("security_group_id")
            or provider.security_group_id,
            subnet_override=kwargs.get("subnet_override"),
            console_zone_id=provider.console_zone_id,
            nfs_server=kwargs.get("nfs_server"),
            nfs_path=kwargs.get("nfs_path"),
            host_type=kwargs.get("host_type", "shared"),
        )

    def terminate_host(self, provider, instance_id):
        from app.services.provisioner import terminate_host

        creds = provider.get_credentials()
        terminate_host(instance_id, credentials=creds)

    def get_host_status(self, provider, instance_id):
        from app.services.provisioner import get_host_status

        creds = provider.get_credentials()
        return get_host_status(instance_id, credentials=creds)

    def resize_host(self, provider, instance_id, new_instance_type):
        from app.services.provisioner import resize_instance

        creds = provider.get_credentials()
        return resize_instance(instance_id, new_instance_type, credentials=creds)

    def extend_host_storage(self, provider, host, db, increment_gb=None):
        from app.services.storage_extend import extend_host_ebs

        return extend_host_ebs(host, db, increment_gb)

    def setup_console(self, provider, base_domain):
        pass

    def create_console_record(self, provider, host, hostname, ip_address):
        from app.services.console_dns import upsert_dns_record

        creds = provider.get_credentials()
        upsert_dns_record(
            hostname, ip_address, provider.console_zone_id, credentials=creds
        )

    def delete_console_record(self, provider, host, hostname, ip_address):
        from app.services.console_dns import delete_dns_record

        creds = provider.get_credentials()
        delete_dns_record(
            hostname, ip_address, provider.console_zone_id, credentials=creds
        )

    def get_host_powerstate(self, provider, instance_id):
        from app.services.provisioner import _get_ec2_client

        creds = provider.get_credentials()
        ec2 = _get_ec2_client(credentials=creds)
        desc = ec2.describe_instances(InstanceIds=[instance_id])
        return desc["Reservations"][0]["Instances"][0]["State"]["Name"]

    def start_host(self, provider, instance_id):
        from app.services.provisioner import _get_ec2_client

        creds = provider.get_credentials()
        ec2 = _get_ec2_client(credentials=creds)
        ec2.start_instances(InstanceIds=[instance_id])

    def stop_host(self, provider, instance_id):
        from app.services.provisioner import _get_ec2_client

        creds = provider.get_credentials()
        ec2 = _get_ec2_client(credentials=creds)
        ec2.stop_instances(InstanceIds=[instance_id])

    def delete_key_pair(self, provider, key_pair_name):
        from app.services.provisioner import _get_ec2_client

        creds = provider.get_credentials()
        ec2 = _get_ec2_client(credentials=creds)
        ec2.delete_key_pair(KeyName=key_pair_name)

    def allocate_eip(self, provider, host, eip_id):
        from app.services.provisioner import _get_ec2_client

        creds = provider.get_credentials()
        ec2 = _get_ec2_client(credentials=creds)

        response = ec2.allocate_address(Domain="vpc")
        allocation_id = response["AllocationId"]
        public_ip = response["PublicIp"]

        tags = [
            {"Key": "ManagedBy", "Value": "troshka"},
            {"Key": "troshka-provider-id", "Value": provider.id},
            {"Key": "troshka-eip-id", "Value": eip_id},
        ]
        ec2.create_tags(Resources=[allocation_id], Tags=tags)

        return {"public_ip": public_ip, "allocation_id": allocation_id}

    def associate_eip(self, provider, host, allocation_id):
        from app.services.provisioner import _get_ec2_client

        creds = provider.get_credentials()
        ec2 = _get_ec2_client(credentials=creds)

        desc = ec2.describe_instances(InstanceIds=[host.instance_id])
        eni_id = None
        for eni in desc["Reservations"][0]["Instances"][0]["NetworkInterfaces"]:
            if eni["Attachment"]["DeviceIndex"] == 0:
                eni_id = eni["NetworkInterfaceId"]
                break
        if not eni_id:
            raise ValueError(f"No primary ENI found for {host.instance_id}")

        assign_resp = ec2.assign_private_ip_addresses(
            NetworkInterfaceId=eni_id, SecondaryPrivateIpAddressCount=1
        )
        private_ip = assign_resp["AssignedPrivateIpAddresses"][0]["PrivateIpAddress"]

        assoc_resp = ec2.associate_address(
            AllocationId=allocation_id,
            NetworkInterfaceId=eni_id,
            PrivateIpAddress=private_ip,
        )

        return {
            "private_ip": private_ip,
            "association_id": assoc_resp["AssociationId"],
        }

    def release_eip(self, provider, allocation_id, namespace=None):
        from app.services.provisioner import _get_ec2_client

        creds = provider.get_credentials()
        ec2 = _get_ec2_client(credentials=creds)
        ec2.release_address(AllocationId=allocation_id)
