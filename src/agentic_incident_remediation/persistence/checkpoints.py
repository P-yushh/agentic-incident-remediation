"""Redis checkpoint construction and execution identity helpers."""

from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.redis import RedisSaver


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
    saver.setup()
    return saver
