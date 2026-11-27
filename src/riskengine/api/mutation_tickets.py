"""Bounded, process-local admission for portfolio management commands."""

from dataclasses import dataclass
import secrets
from threading import Lock
from time import monotonic
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel

from riskengine.api.portfolio_management import Portfolio

Command = tuple[int | None, str, str | None]
Scope = tuple[str, str, tuple[int, int]]
Outcome = Literal["prepared", "running", "committed", "rolled_back", "not_started", "unknown", "unavailable"]


class TicketResult(BaseModel):
    ticket: str
    outcome: Outcome
    portfolio: Portfolio | None = None
    reason: str | None = None
    http_status: int | None = None


@dataclass
class Entry:
    scope: Scope
    command: Command
    result: TicketResult
    deadline: float


class MutationTickets:
    def __init__(self, capacity=128, lifetime=60, retention=600, clock=monotonic):
        self._entries: dict[str, Entry] = {}
        self._lock = Lock()
        self._capacity = capacity
        self._lifetime = lifetime
        self._retention = retention
        self._clock = clock

    def _prune(self):
        now = self._clock()
        for ticket, entry in list(self._entries.items()):
            if entry.result.outcome != "running" and entry.deadline <= now:
                del self._entries[ticket]

    def prepare(self, scope: Scope, command: Command) -> TicketResult:
        with self._lock:
            self._prune()
            if len(self._entries) >= self._capacity:
                raise HTTPException(503, "Portfolio management is busy. Try again later.")
            ticket = secrets.token_urlsafe(32)
            result = TicketResult(ticket=ticket, outcome="prepared")
            self._entries[ticket] = Entry(scope, command, result, self._clock() + self._lifetime)
            return result

    def inspect(self, ticket: str, session: tuple[str, str], *, close=False, command: Command | None = None,
                identity: tuple[int, int] | None = None):
        """Return the visible state and whether this call won execution admission."""
        with self._lock:
            self._prune()
            entry = self._entries.get(ticket)
            if entry is None or entry.scope[:2] != session:
                return TicketResult(ticket=ticket, outcome="unavailable"), False
            if command is not None and (entry.command != command or entry.scope[2] != identity):
                raise HTTPException(409, "The prepared portfolio change does not match this request.")
            if entry.result.outcome != "prepared":
                return entry.result, False
            if close:
                entry.result = TicketResult(ticket=ticket, outcome="not_started")
                entry.deadline = self._clock() + self._retention
            elif command is not None:
                entry.result = TicketResult(ticket=ticket, outcome="running")
                return entry.result, True
            return entry.result, False

    def finish(self, result: TicketResult):
        with self._lock:
            entry = self._entries[result.ticket]
            entry.result = result
            entry.deadline = self._clock() + self._retention
