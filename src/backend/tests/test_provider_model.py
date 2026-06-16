import os

os.environ["TROSHKA_DATABASE__URL"] = "sqlite:///./test_provider_model.db"

from sqlalchemy import create_engine
from sqlalchemy.dialects import sqlite
from sqlalchemy.orm import sessionmaker

sqlite.base.SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"
sqlite.base.SQLiteTypeCompiler.visit_UUID = lambda self, type_, **kw: "VARCHAR(36)"

from app.core.database import Base
from app.models.provider import Provider

engine = create_engine(
    "sqlite:///./test_provider_model.db",
    connect_args={"check_same_thread": False},
)
Base.metadata.drop_all(bind=engine)
Base.metadata.create_all(bind=engine)
Session = sessionmaker(bind=engine)


def test_gcp_provider_columns():
    db = Session()
    p = Provider(
        name="test-gcp",
        type="gcp",
        default_region="us-central1",
        gcp_project_id="my-project-123",
        gcp_network_id="projects/my-project/global/networks/troshka-vpc",
        gcp_subnet_id="projects/my-project/regions/us-central1/subnetworks/troshka-sub",
        gcp_firewall_policy="troshka-fw",
        gcp_zone="us-central1-a",
    )
    p.set_credentials({"service_account_json": {"type": "service_account"}})
    db.add(p)
    db.commit()
    db.refresh(p)

    assert p.gcp_project_id == "my-project-123"
    assert p.gcp_zone == "us-central1-a"
    assert p.get_credentials()["service_account_json"]["type"] == "service_account"
    db.close()


def test_azure_provider_columns():
    db = Session()
    p = Provider(
        name="test-azure",
        type="azure",
        default_region="eastus",
        azure_subscription_id="00000000-0000-0000-0000-000000000000",
        azure_resource_group="troshka-rg",
        azure_vnet_id="/subscriptions/.../resourceGroups/troshka-rg/providers/Microsoft.Network/virtualNetworks/troshka-vnet",
        azure_subnet_id="/subscriptions/.../subnets/troshka-sub",
        azure_nsg_id="/subscriptions/.../networkSecurityGroups/troshka-nsg",
        azure_location="eastus",
    )
    p.set_credentials(
        {
            "client_id": "cid",
            "client_secret": "csec",
            "tenant_id": "tid",
            "subscription_id": "sid",
        }
    )
    db.add(p)
    db.commit()
    db.refresh(p)

    assert p.azure_subscription_id == "00000000-0000-0000-0000-000000000000"
    assert p.azure_location == "eastus"
    creds = p.get_credentials()
    assert creds["client_id"] == "cid"
    db.close()
