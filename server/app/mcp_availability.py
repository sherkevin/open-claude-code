"""Recover configured MCP discovery on demand, without executing business tools."""
from __future__ import annotations

import asyncio
import json
import time
from collections import OrderedDict

from .db import offload
from .security import AppError

DISCOVERY_RETRY_SECONDS = 10
DISCOVERY_TIMEOUT_SECONDS = 2
MAX_CONCURRENT_DISCOVERIES = 6
MAX_TRACKED_CONNECTIONS = 1024


class MCPAvailability:
    def __init__(self, service):
        self.service = service
        self.in_flight = set()
        self.next_attempt = OrderedDict()

    def _candidates(self, conversation_id):
        service = self.service
        conversation = service.require("conversations", conversation_id)
        if conversation["archived"] or not conversation["active_run_id"]:
            return []
        user = service.require("users", conversation["user_id"])
        if not user["enabled"]:
            return []
        assistant = service.usable_assistant(conversation["assistant_id"], user)
        current_ids = {item["connection_id"] for item in json.loads(assistant["connections"])
                       if item["allowed_tools"]}
        run = service.require("runs", conversation["active_run_id"])
        if run["status"] not in {"queued", "running"}:
            return []
        if json.loads(run.get("execution_meta") or "{}").get("recovery_mode") == "answer_only":
            return []
        snapshot = json.loads(run.get("capability_snapshot") or "null")
        if not snapshot:
            return []
        candidates = []
        for item in snapshot.get("connections", []):
            connection_id = item["connection_id"]
            if connection_id not in current_ids or not item["allowed_tools"]:
                continue
            try:
                row = service.require_resource("connections", connection_id, user)
            except AppError:
                continue
            if (row["enabled"] and row["status"] == "failed"
                    and row["revision"] == snapshot.get("connection_revisions", {}).get(connection_id)):
                candidates.append(row)
        return candidates

    async def _probe(self, key, row):
        try:
            await self.service.probe_connection(row, timeout=DISCOVERY_TIMEOUT_SECONDS)
        finally:
            self.in_flight.discard(key)

    async def refresh(self, conversation_id):
        if self.service.policy.is_trial:
            return
        candidates = await offload(self._candidates, conversation_id)
        probes = []
        current = time.monotonic()
        for row in candidates:
            if len(self.in_flight) >= MAX_CONCURRENT_DISCOVERIES:
                break
            key = row["id"], row["revision"]
            if key in self.in_flight or current < self.next_attempt.get(key, 0):
                continue
            self.in_flight.add(key)
            self.next_attempt[key] = current + DISCOVERY_RETRY_SECONDS
            self.next_attempt.move_to_end(key)
            if len(self.next_attempt) > MAX_TRACKED_CONNECTIONS:
                self.next_attempt.popitem(last=False)
            probes.append(self._probe(key, row))
        await asyncio.gather(*probes)
