"""Shared Feishu bot session-state helpers."""

from typing import Optional, Tuple

from .ai.types import ChatState


def ensure_state(state: Optional[ChatState]) -> ChatState:
    return state if state is not None else ChatState()


def bind_engine_state(engine, state: ChatState):
    engine.state = state
    engine.executor.state = state
    return engine


def sync_state_cache(
    state: ChatState,
    *,
    search_cache=None,
    resource_cache=None,
) -> ChatState:
    if search_cache is not None:
        state.search_cache = search_cache
    if resource_cache is not None:
        state.resource_cache = resource_cache
    return state


def cache_counts(state: ChatState) -> Tuple[int, int]:
    return len(state.search_cache), len(state.resource_cache)
