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
    argument_constraints: dict[str, dict] = Field(default_factory=dict, max_length=64)


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
    auto_model_routing: bool | None = None


class ConversationPatch(InputModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    archived: bool | None = None
    model_id: str | None = Field(default=None, min_length=1, max_length=100)
    reasoning_level: str | None = Field(default=None, min_length=1, max_length=30)
    category: str | None = Field(default=None, max_length=100)
    tags: list[Tag] | None = Field(default=None, max_length=10)
    auto_model_routing: bool | None = None


class ModelRecommendationRequest(InputModel):
    """A bounded, advisory model-selection request.

    The request deliberately carries a short task summary rather than a
    conversation id or message history. The response never mutates a session.
    """
    task_summary: str = Field(min_length=1, max_length=2000)
    candidate_model_ids: list[str] | None = Field(default=None, max_length=20)


class ModelCompletionRequest(InputModel):
    """A short draft sent to the opt-in router for real Tab completion."""
    text: str = Field(min_length=1, max_length=500)


class XlsxSheet(InputModel):
    """Bounded table input for a user-visible local Excel export."""
    name: str = Field(default="Sheet 1", min_length=1, max_length=80)
    columns: list[str] = Field(default_factory=list, max_length=50)
    rows: list[list[str | int | float | bool | None] | dict[str, str | int | float | bool | None]] = Field(default_factory=list, max_length=5000)


class XlsxArtifactRequest(InputModel):
    filename: str = Field(default="导出.xlsx", min_length=1, max_length=180)
    sheets: list[XlsxSheet] = Field(min_length=1, max_length=10)


class ImageGenerationRequest(InputModel):
    """Prompt contract kept even while no approved image adapter is present."""
    prompt: str = Field(min_length=1, max_length=2000)
    model_id: str | None = Field(default=None, min_length=1, max_length=100)


class SendMessage(InputModel):
    client_request_id: str = Field(min_length=8, max_length=100)
    text: str = Field(default="", max_length=50000)
    file_ids: list[str] = Field(default_factory=list, max_length=10)
    skill_ids: list[str] = Field(default_factory=list, max_length=5)
    # A one-shot model override is explicit and never invokes the router.
    model_id: str | None = Field(default=None, min_length=1, max_length=100)
    reasoning_level: str | None = Field(default=None, min_length=1, max_length=30)
    auto_route: bool | None = None


class ApprovalDecision(InputModel):
    decision: Literal["allow", "deny"]
    memory_content_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class ModelApprovalPolicy(InputModel):
    """Administrator-owned, local-only model review boundary."""

    enabled: bool = False
    reviewer_mode: Literal["synthetic"] = "synthetic"
    model_id: str = Field(default="synthetic-policy-v1", min_length=1, max_length=100)
    allowed_tools: list[str] = Field(default_factory=list, max_length=100)
    allowed_data_scopes: list[str] = Field(default_factory=list, max_length=100)
    max_amount: float = Field(default=0, ge=0, le=1_000_000_000)
    allowed_operations: list[str] = Field(default_factory=list, max_length=100)


class HighRiskApprovalPolicy(InputModel):
    """Administrator-owned high-risk dual-approval boundary."""

    enabled: bool = False
    policy_id: str | None = Field(default=None, min_length=1, max_length=100)
    allowed_tools: list[str] = Field(default_factory=list, max_length=100)
    admin_allowlist: list[str] = Field(default_factory=list, max_length=100)
    required_approvals: int = Field(default=2, ge=2, le=2)


class SubagentPlanRequest(InputModel):
    """Protected local orchestration contract; model/network modes are absent."""

    mode: Literal["synthetic"] = "synthetic"
    tasks: list[str | dict] = Field(min_length=1, max_length=4)


WorkflowNodeType = Literal[
    "input",
    "agent",
    "skill",
    "knowledge_search",
    "mcp_read",
    "condition",
    "approval",
    "artifact",
]


class WorkflowNode(InputModel):
    """A safe, declarative node used by the first local workflow slice."""

    id: str = Field(min_length=1, max_length=64)
    type: WorkflowNodeType
    label: str = Field(default="", max_length=120)
    config: dict[str, object] = Field(default_factory=dict, max_length=30)


class WorkflowEdge(InputModel):
    source: str = Field(min_length=1, max_length=64)
    target: str = Field(min_length=1, max_length=64)
    label: str = Field(default="", max_length=120)


class WorkflowGraph(InputModel):
    nodes: list[WorkflowNode] = Field(min_length=1, max_length=64)
    edges: list[WorkflowEdge] = Field(default_factory=list, max_length=128)


class WorkflowCreate(InputModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    graph: WorkflowGraph


class WorkflowTemplateCreate(InputModel):
    template_id: str = Field(min_length=1, max_length=80)
    name: str | None = Field(default=None, min_length=1, max_length=120)


class WorkflowDraftPatch(InputModel):
    expected_revision: int | None = Field(default=None, ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    graph: WorkflowGraph


class WorkflowRollback(InputModel):
    version: int = Field(ge=1)


class WorkflowRunRequest(InputModel):
    input: dict[str, object] = Field(default_factory=dict, max_length=30)
    # Optional session binding for the synthetic execution contract.  A
    # bound conversation must belong to the workflow's assistant and the
    # authenticated user; no provider or customer runtime is invoked.
    conversation_id: str | None = Field(default=None, min_length=1, max_length=100)


class WorkflowNodeDebugRequest(InputModel):
    """Bounded input for testing one draft agent node before publishing."""

    input: dict[str, object] = Field(default_factory=dict, max_length=30)
