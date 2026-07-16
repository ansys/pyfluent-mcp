# Copyright (C) 2026 Synopsys, Inc. and ANSYS, Inc. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""In-memory conversation store with TTL.

Provides lightweight state tracking for request/response flows that need
short-lived session continuity.

Pluggable: swap ``_now()`` and the dict for a Redis-backed implementation
later without touching the rest of the codebase.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import threading
import time
from typing import Any, Optional
import uuid


@dataclass
class ConversationEntry:
    """Conversation entry for a single session."""

    session_id: str
    created_at: float
    updated_at: float
    history: list[dict[str, Any]] = field(default_factory=list)
    pending_followup: Optional[dict[str, Any]] = None
    extra: dict[str, Any] = field(default_factory=dict)


class ConversationStore:
    """Thread-safe TTL-bounded conversation store."""

    def __init__(self, *, ttl_seconds: float = 60 * 60, max_entries: int = 256) -> None:
        """Initialize the ConversationStore instance.

        Parameters
        ----------
        ttl_seconds : float
            Ttl seconds to supply to the function.
        max_entries : int
            Max entries to supply to the function.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        self._ttl = ttl_seconds
        self._max = max_entries
        self._lock = threading.RLock()
        self._entries: dict[str, ConversationEntry] = {}

    @staticmethod
    def _now() -> float:
        """Return the current timestamp used by the store.

        Returns
        -------
        float
            Floating-point result produced by the operation.
        """
        return time.monotonic()

    # ---- lifecycle ---------------------------------------------------

    def create(self) -> ConversationEntry:
        """Create the value needed by the operation.

        Returns
        -------
        ConversationEntry
            ConversationEntry produced by the operation.
        """
        with self._lock:
            self._evict_locked()
            sid = uuid.uuid4().hex
            entry = ConversationEntry(
                session_id=sid, created_at=self._now(), updated_at=self._now()
            )
            self._entries[sid] = entry
            return entry

    def get(self, session_id: str) -> Optional[ConversationEntry]:
        """Return a fake mapping or attribute value.

        Parameters
        ----------
        session_id : str
            Identifier for the conversation or tool session.

        Returns
        -------
        Optional[ConversationEntry]
            Optional value produced by the operation.
        """
        with self._lock:
            entry = self._entries.get(session_id)
            if entry is None:
                return None
            if self._now() - entry.updated_at > self._ttl:
                self._entries.pop(session_id, None)
                return None
            return entry

    def get_or_create(self, session_id: Optional[str]) -> ConversationEntry:
        """Return the or create.

        Parameters
        ----------
        session_id : Optional[str]
            Identifier for the conversation or tool session.

        Returns
        -------
        ConversationEntry
            ConversationEntry produced by the operation.
        """
        if session_id:
            entry = self.get(session_id)
            if entry is not None:
                return entry
        return self.create()

    def touch(self, session_id: str) -> None:
        """Update the conversation access timestamp.

        Parameters
        ----------
        session_id : str
            Identifier for the conversation or tool session.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        with self._lock:
            entry = self._entries.get(session_id)
            if entry is not None:
                entry.updated_at = self._now()

    def append_history(self, session_id: str, role: str, content: Any) -> None:
        """Append history.

        Parameters
        ----------
        session_id : str
            Identifier for the conversation or tool session.
        role : str
            Role to supply to the function.
        content : Any
            Content returned by the mocked response.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        with self._lock:
            entry = self._entries.get(session_id)
            if entry is None:
                return
            entry.history.append({"role": role, "content": content, "ts": self._now()})
            entry.updated_at = self._now()

    def set_pending_followup(self, session_id: str, followup: dict[str, Any] | None) -> None:
        """Set the pending follow-up prompt.

        Parameters
        ----------
        session_id : str
            Identifier for the conversation or tool session.
        followup : dict[str, Any] | None
            Pending follow-up payload to store.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        with self._lock:
            entry = self._entries.get(session_id)
            if entry is not None:
                entry.pending_followup = followup
                entry.updated_at = self._now()

    def has_pending_followup_id(self, session_id: str, followup_id: str) -> bool:
        """Return True if ``session_id`` already has a pending follow-up with this ID.

        Parameters
        ----------
        session_id : str
            Session identifier to supply to the function.
        followup_id : str
            Pending follow-up identifier to check.

        Returns
        -------
        bool
            Boolean result produced by the function.
        """
        with self._lock:
            entry = self._entries.get(session_id)
            if entry is None or entry.pending_followup is None:
                return False
            return entry.pending_followup.get("id") == followup_id

    def followup_was_just_asked(self, session_id: str, question_text: str) -> bool:
        """Detect a follow-up loop: same question text already pending.

        Parameters
        ----------
        session_id : str
            Session identifier to supply to the function.
        question_text : str
            Question text to supply to the function.

        Returns
        -------
        bool
            Boolean result produced by the function.
        """
        with self._lock:
            entry = self._entries.get(session_id)
            if entry is None or entry.pending_followup is None:
                return False
            existing = (entry.pending_followup.get("question") or "").strip().lower()
            return bool(existing) and existing == (question_text or "").strip().lower()

    # ---- maintenance -------------------------------------------------

    def _evict_locked(self) -> None:
        """Evict stale locked conversation entries.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        if len(self._entries) < self._max:
            # Always opportunistically drop expired entries.
            cutoff = self._now() - self._ttl
            stale = [sid for sid, e in self._entries.items() if e.updated_at < cutoff]
            for sid in stale:
                self._entries.pop(sid, None)
            return
        # Capacity hit: drop oldest first.
        ordered = sorted(self._entries.items(), key=lambda kv: kv[1].updated_at)
        for sid, _ in ordered[: max(1, len(self._entries) - self._max + 1)]:
            self._entries.pop(sid, None)
