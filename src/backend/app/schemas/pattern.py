import datetime

from pydantic import BaseModel


class PatternDiskResponse(BaseModel):
    id: str
    source_disk_id: str
    source_vm_id: str
    s3_key: str
    format: str
    size_bytes: int
    virtual_size_bytes: int
    checksum_sha256: str | None = None
    state: str

    model_config = {"from_attributes": True}


class PatternCreate(BaseModel):
    name: str
    description: str | None = None
    visibility: str = "private"
    tags: dict | None = None
    source_project_id: str | None = None
    restart_after: bool = True
    quiesce_cluster: bool = True
    capture_clock_target: bool = False
    recert: bool = False
    topology: dict | None = None
    disk_mappings: list[dict] | None = None


class PatternUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    visibility: str | None = None
    tags: dict | None = None


class PatternResponse(BaseModel):
    id: str
    name: str
    description: str | None = None
    owner_id: str
    visibility: str
    source_project_id: str | None = None
    topology: dict
    state: str
    total_size_bytes: int
    tags: dict | None = None
    clock_target: datetime.datetime | None = None
    recert: bool = False
    created_at: datetime.datetime
    disks: list[PatternDiskResponse] = []

    model_config = {"from_attributes": True}


class PatternListResponse(BaseModel):
    id: str
    name: str
    description: str | None = None
    owner_id: str
    visibility: str
    state: str
    total_size_bytes: int
    tags: dict | None = None
    created_at: datetime.datetime
    disk_count: int = 0
    vm_count: int = 0

    model_config = {"from_attributes": True}


class PatternShareRequest(BaseModel):
    user_email: str


class PatternDeployRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    guid: str | None = None
    domain: str | None = None
    dns_provider_id: str | None = None
    auto_deploy: bool = True
    auto_start: bool = True
    recert: bool | None = None
    common_password: str | None = None
    inject_vars: dict | None = None
    ssh_keys: list[str] | None = None
    host_id: str | None = None


class PatternBulkDeployRequest(BaseModel):
    count: int
    name_template: str
    auto_deploy: bool = False
    guid_template: str | None = None
    domain: str | None = None
    dns_provider_id: str | None = None
