from __future__ import annotations

from typing import Literal, Annotated
from pydantic import BaseModel, ConfigDict, Field

Scope = Literal["private", "team"]
Tag = Annotated[str, Field(min_length=1, max_length=40)]


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Login(InputModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=256)


class UserCreate(Login):
    display_name: str = Field(min_length=1, max_length=100)
    role: Literal["admin", "member"] = "member"


class UserPatch(InputModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=100)
    role: Literal["admin", "member"] | None = None
    enabled: bool | None = None
    password: str | None = None


class ConnectionCreate(InputModel):
    name: str = Field(min_length=1, max_length=100)
    transport: Literal["http"] = "http"
    url: str = Field(max_length=2048)
    auth_type: Literal["none", "bearer"] = "none"
    credential: str | None = Field(default=None, max_length=8192)
    enabled: bool = True
    scope: Scope = "private"
    source_metadata: dict[str, str] = Field(default_factory=dict, max_length=20)


class ConnectionPatch(InputModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    transport: Literal["http"] | None = None
    url: str | None = Field(default=None, max_length=2048)
    auth_type: Literal["none", "bearer"] | None = None
    credential: str | None = Field(default=None, max_length=8192)
    clear_credential: bool = False
    enabled: bool | None = None
    scope: Scope | None = None
    source_metadata: dict[str, str] | None = Field(default=None, max_length=20)


class Access(InputModel):
    all_members: bool = True
    user_ids: list[str] = Field(default_factory=list, max_length=1000)


class Binding(InputModel):
    connection_id: str
    allowed_tools: list[str] = Field(default_factory=list, max_length=1000)
    auto_approved_tools: list[str] = Field(default_factory=list, max_length=1000)


class AssistantCreate(InputModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=1000)
    instructions: str = Field(default="", max_length=20000)
    enabled: bool = True
    access: Access = Field(default_factory=Access)
    connections: list[Binding] = Field(default_factory=list, max_length=32)
    scope: Scope = "private"
    knowledge_base_ids: list[str] | None = Field(default=None, max_length=64)
    skill_ids: list[str] = Field(default_factory=list, max_length=64)
    web_search_enabled: bool = False
    default_model_id: str | None = Field(default=None, max_length=100)
    default_reasoning_level: str | None = Field(default=None, max_length=30)


class AssistantPatch(InputModel):
    expected_revision: int | None = Field(default=None, ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=1000)
    instructions: str | None = Field(default=None, max_length=20000)
    enabled: bool | None = None
    access: Access | None = None
    connections: list[Binding] | None = Field(default=None, max_length=32)
    scope: Scope | None = None
    knowledge_base_ids: list[str] | None = Field(default=None, max_length=64)
    skill_ids: list[str] | None = Field(default=None, max_length=64)
    web_search_enabled: bool | None = None
    default_model_id: str | None = Field(default=None, max_length=100)
    default_reasoning_level: str | None = Field(default=None, max_length=30)


class ConversationCreate(InputModel):
    assistant_id: str
    title: str = Field(default="新对话", min_length=1, max_length=200)
    model_id: str | None = Field(default=None, min_length=1, max_length=100)
    reasoning_level: str | None = Field(default=None, min_length=1, max_length=30)
    category: str = Field(default="", max_length=100)
    tags: list[Tag] = Field(default_factory=list, max_length=10)


class ConversationPatch(InputModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    archived: bool | None = None
    model_id: str | None = Field(default=None, min_length=1, max_length=100)
    reasoning_level: str | None = Field(default=None, min_length=1, max_length=30)
    category: str | None = Field(default=None, max_length=100)
    tags: list[Tag] | None = Field(default=None, max_length=10)


class ModelRecommendationRequest(InputModel):
    """A bounded, advisory model-selection request.

    The request deliberately carries a short task summary rather than a
    conversation id or message history. The response never mutates a session.
    """
    task_summary: str = Field(min_length=1, max_length=2000)
    candidate_model_ids: list[str] | None = Field(default=None, max_length=20)


class SendMessage(InputModel):
    client_request_id: str = Field(min_length=8, max_length=100)
    text: str = Field(default="", max_length=50000)
    file_ids: list[str] = Field(default_factory=list, max_length=10)
    skill_ids: list[str] = Field(default_factory=list, max_length=5)


class ApprovalDecision(InputModel):
    decision: Literal["allow", "deny"]
