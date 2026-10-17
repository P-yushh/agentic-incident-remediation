"""Redis checkpoint construction and execution identity helpers."""

from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.redis import RedisSaver
from langgraph.checkpoint.redis.jsonplus_redis import JsonPlusRedisSerializer

from agentic_incident_remediation.models import (
    AgentMessage,
    AgentState,
    ApprovalRecord,
    GeneratedArtifact,
    Incident,
    PlanStep,
    PullRequestRecord,
    RemediationPlan,
    ValidationFinding,
    ValidationResult,
)


def create_checkpoint_serializer() -> JsonPlusRedisSerializer:
    """Revive only our validated domain contracts, never arbitrary application classes."""

    contracts = (
        AgentMessage,
        AgentState,
        ApprovalRecord,
        GeneratedArtifact,
        Incident,
        PlanStep,
        PullRequestRecord,
        RemediationPlan,
        ValidationFinding,
        ValidationResult,
    )
    return JsonPlusRedisSerializer(
        allowed_json_modules=[
            (*contract.__module__.split("."), contract.__name__) for contract in contracts
        ],
        allowed_msgpack_modules=[
            (contract.__module__, contract.__name__) for contract in contracts
        ],
        pickle_fallback=False,
    )


def checkpoint_config(execution_id: UUID, *, recursion_limit: int) -> RunnableConfig:
    """Return the stable LangGraph thread configuration for one execution."""

    return {
        "configurable": {"thread_id": str(execution_id)},
        "recursion_limit": recursion_limit,
    }


def create_redis_checkpointer(redis_url: str) -> RedisSaver:
    """Create and initialize the Redis indices required by LangGraph."""

    if not redis_url.strip():
        raise ValueError("redis_url cannot be empty")
    saver = RedisSaver(redis_url=redis_url)
    saver.serde = create_checkpoint_serializer()
    saver.setup()
    return saver
