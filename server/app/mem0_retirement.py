"""Opt-in retirement admission from a current native clear; no product route.

Retirement is terminal for the existing store identity. This is a durable
logical receipt, not a claim of deleting bytes or authorization to do so.
"""
from dataclasses import dataclass
import hashlib
from pathlib import Path

from .mem0_index import (
    Mem0IndexDisabled, Mem0IndexScopeInvalid, NativeMemorySourceReader, canonical,
)
from .mem0_scope_store import ScopeStore


@dataclass(frozen=True)
class RetirementReceipt:
    proof: str
    state: str = "retired"
    cleanup_status: str = "not_performed"


def retire_committed_clear(service, store: ScopeStore, user_id: str,
                           conversation_id: str, operation_key: str) -> RetirementReceipt:
    """Explicit host maintenance; never called from a model or retrieval.

    The existing SQLite write reservation excludes concurrent authority changes
    by cooperating DB connections until the filesystem receipt is committed.
    No derived body or provider is opened. A failed receipt can only be
    inspected by an explicit retry with the same still-current clear.
    """
    if not store.config.enabled:
        raise Mem0IndexDisabled("Mem0 scope store disabled")
    if store.root.parent != Path(service.settings.data_dir).absolute():
        raise Mem0IndexScopeInvalid("scope store does not belong to authority")
    reader = NativeMemorySourceReader(service.memory)
    with service.db.transaction():
        scope = reader._admitted_scope(user_id, conversation_id)
        if scope.digest != hashlib.sha256(b"").hexdigest():
            raise Mem0IndexScopeInvalid("current native snapshot is not empty")
        reader.require_committed_clear(scope, operation_key)
        proof = hashlib.sha256(canonical([
            "native-clear-retirement-v1", store.config.agent_id,
            scope.generation, operation_key,
        ])).hexdigest()
        return RetirementReceipt(store._retire_with_clear_proof(scope, proof))
