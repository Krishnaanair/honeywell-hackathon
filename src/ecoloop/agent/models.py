"""Typed protocol-neutral models used by the local agent host."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class AgentModel(BaseModel):
    """Strict base model for controller host data."""

    model_config = ConfigDict(extra="forbid")


class ToolSpec(AgentModel):
    """One dynamically discovered MCP tool."""

    name: str = Field(min_length=1)
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)

    def as_ollama_tool(self) -> dict[str, Any]:
        """Convert MCP's input schema to Ollama's function-tool shape."""

        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }


class ToolRequest(AgentModel):
    """A tool invocation requested by the local model."""

    name: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)


class ModelResponse(AgentModel):
    """Only the operational model output needed by the host."""

    content: str = ""
    tool_calls: list[ToolRequest] = Field(default_factory=list)
    model: str | None = None


class HostToolTrace(AgentModel):
    """One MCP round-trip trace; intentionally excludes private reasoning."""

    sequence: int = Field(ge=1)
    tool_name: str
    arguments: dict[str, Any]
    result: dict[str, Any] | None
    started_at: datetime
    completed_at: datetime
    latency_ms: float = Field(ge=0)
    success: bool
    error: str | None = None


class AgentDecision(AgentModel):
    """Terminal outcome returned to the simulation coordinator."""

    run_id: str
    observation_id: int
    status: Literal["applied", "fallback"]
    result: dict[str, Any]
    model: str
    latency_ms: float = Field(ge=0)
    tool_rounds: int = Field(ge=0)
    trace: list[HostToolTrace]
    corrective_reprompt_used: bool
    timeout_count: int = Field(ge=0)
    fallback_status: str | None = None
    cache_hit: bool = False
