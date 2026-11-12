from typing import Literal

from pydantic import BaseModel, Field


class GroupCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str = Field(default="", max_length=10000)
    is_organization: Literal[True] = True
    cost_center_id: str | None = Field(default=None, max_length=255)


class GroupUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=10000)
    is_organization: Literal[True] | None = None
    cost_center_id: str | None = Field(default=None, max_length=255)


class ManagerAssignment(BaseModel):
    email: str = Field(min_length=1, max_length=255)
