"""Anthropic client wrapper for the discovery agent.

Kept deliberately thin. The agent loop owns the control flow; this module owns
the transport, the tool schema, retries, and writing a redacted trace of every
exchange to evidence. The trace is evidence *about* the run — it is never the
replay representation.
"""

from __future__ import annotations

import inspect
import json
import os
import time
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any

from ..observability.evidence import EvidenceWriter
from ..safety.redact import Redactor

DEFAULT_MODEL = os.environ.get("CUA_MODEL", "claude-sonnet-5")
MODEL_FALLBACKS = ["claude-sonnet-5", "claude-opus-5", "claude-haiku-4-5-20251001"]


def tool_definitions() -> list[dict[str, Any]]:
    """The agent's action vocabulary.

    Note what is absent: there is no free-form "run javascript" or "use this CSS
    selector" tool. The model can only do what a human at the keyboard could do,
    which is what keeps the recording portable to other surfaces.
    """
    expect = {
        "type": "string",
        "description": "Text you expect to be visible on screen afterwards if this worked. "
                       "Becomes the replay checkpoint. Quote it exactly.",
    }
    intent = {
        "type": "string",
        "description": "Why you are doing this, in business terms.",
    }
    return [
        {
            "name": "navigate",
            "description": "Load a URL in the application. Subject to the allowlist.",
            "input_schema": {
                "type": "object",
                "properties": {"url": {"type": "string"}, "intent": intent, "expect": expect},
                "required": ["url", "intent"],
            },
        },
        {
            "name": "click",
            "description": "Click a control by its ref from the latest observation.",
            "input_schema": {
                "type": "object",
                "properties": {"ref": {"type": "string"}, "intent": intent, "expect": expect},
                "required": ["ref", "intent"],
            },
        },
        {
            "name": "type_text",
            "description": "Type into a field. Pass input_name for values that come from the "
                           "task inputs (this makes them parameters of the capability); pass "
                           "text only for values that are genuinely fixed for every run. "
                           "Credentials must always use input_name.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "ref": {"type": "string"},
                    "input_name": {"type": "string"},
                    "text": {"type": "string"},
                    "intent": intent,
                    "expect": expect,
                },
                "required": ["ref", "intent"],
            },
        },
        {
            "name": "select_option",
            "description": "Choose a value in a dropdown.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "ref": {"type": "string"},
                    "value": {"type": "string"},
                    "intent": intent,
                    "expect": expect,
                },
                "required": ["ref", "value", "intent"],
            },
        },
        {
            "name": "press_key",
            "description": "Press a keyboard key such as Enter or Tab.",
            "input_schema": {
                "type": "object",
                "properties": {"key": {"type": "string"}, "intent": intent, "expect": expect},
                "required": ["key", "intent"],
            },
        },
        {
            "name": "extract_output",
            "description": "Declare that the value shown by this control is one of the "
                           "capability's typed outputs.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "snake_case output name"},
                    "ref": {"type": "string"},
                    "type": {
                        "type": "string",
                        "enum": ["string", "integer", "decimal", "money", "boolean", "date"],
                    },
                    "description": {"type": "string"},
                    "sensitivity": {
                        "type": "string",
                        "enum": ["public", "internal", "pii"],
                        "description": "Use 'pii' for anything identifying a person. PII is "
                                       "returned to the caller but redacted in stored evidence.",
                    },
                    "intent": intent,
                },
                "required": ["name", "ref", "type", "description"],
            },
        },
        {
            "name": "declare_outcome",
            "description": "Register a legitimate business result, with the application's exact "
                           "wording as the marker.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "snake_case, e.g. member_not_found"},
                    "marker_text": {
                        "type": "string",
                        "description": "Exact text copied from the screen that identifies this state.",
                    },
                    "disposition": {
                        "type": "string",
                        "enum": ["return_to_caller", "escalate_to_human"],
                    },
                    "message": {"type": "string"},
                    "remediation": {"type": "string"},
                },
                "required": ["name", "marker_text", "disposition", "message"],
            },
        },
        {
            "name": "declare_recovery",
            "description": "Register a transient screen the automation can clear by itself.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "marker_text": {"type": "string"},
                    "control_name": {
                        "type": "string",
                        "description": "Exact label of the control that clears it.",
                    },
                    "control_role": {"type": "string", "enum": ["button", "link"]},
                    "description": {"type": "string"},
                },
                "required": ["name", "marker_text", "control_name", "description"],
            },
        },
        {
            "name": "declare_failure",
            "description": "Register a genuine application fault that must stop a run.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "marker_text": {"type": "string"},
                    "error_class": {"type": "string"},
                    "message": {"type": "string"},
                },
                "required": ["name", "marker_text", "error_class", "message"],
            },
        },
        {
            "name": "request_human_help",
            "description": "Pause and hand the live session to a human operator.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string"},
                    "suggested_actions": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["reason"],
            },
        },
        {
            "name": "finish",
            "description": "End the current phase.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "success": {"type": "boolean"},
                    "summary": {"type": "string"},
                },
                "required": ["success", "summary"],
            },
        },
    ]


@dataclass
class LlmCall:
    index: int
    stop_reason: str
    text: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)


class LlmClient:
    def __init__(
        self,
        *,
        model: str | None = None,
        redactor: Redactor,
        evidence: EvidenceWriter,
        max_tokens: int = 1600,
        effort: str | None = None,
    ) -> None:
        try:
            from anthropic import Anthropic
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("the 'anthropic' package is required for discovery") from exc
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Discovery makes real model calls; "
                "set it in your environment or .env before running `cua discover`."
            )
        self._client = Anthropic()
        self.model = model or DEFAULT_MODEL
        self.redactor = redactor
        self.evidence = evidence
        self.max_tokens = max_tokens
        self.effort = effort or os.environ.get("CUA_EFFORT") or None
        # The SDK's sampling knobs have moved between versions (`temperature`
        # became `output_config.effort`). Rather than pin a version, ask the
        # installed client what it accepts.
        try:
            self._supported = set(
                inspect.signature(self._client.messages.create).parameters
            )
        except (TypeError, ValueError):  # pragma: no cover - defensive
            self._supported = set()
        self.calls = 0
        self.usage = {"input_tokens": 0, "output_tokens": 0}
        self._trace_lines: list[str] = []

    # -- transport --------------------------------------------------------

    def _kwargs(self, model: str, system: str, messages: list[dict[str, Any]],
                tools: list[dict[str, Any]]) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": self.max_tokens,
            "system": system,
            "tools": tools,
            "messages": messages,
        }
        if self.effort and "output_config" in self._supported:
            kwargs["output_config"] = {"effort": self.effort}
        elif self.effort and "temperature" in self._supported:
            kwargs["temperature"] = 0.0
        return kwargs

    def complete(self, *, system: str, messages: list[dict[str, Any]],
                 tools: list[dict[str, Any]]) -> LlmCall:
        last_error: Exception | None = None
        models = [self.model] + [m for m in MODEL_FALLBACKS if m != self.model]
        tried: list[str] = []
        for model in models:
            tried.append(model)
            for attempt in range(3):
                try:
                    response = self._client.messages.create(
                        **self._kwargs(model, system, messages, tools)
                    )
                    self.model = model
                    self.calls += 1
                    return self._record(response, messages)
                except TypeError as exc:
                    # A client-side signature mismatch will fail identically for
                    # every model; retrying wastes time and hides the real cause.
                    raise RuntimeError(
                        f"the installed anthropic SDK rejected the request shape: {exc}"
                    ) from exc
                except Exception as exc:
                    last_error = exc
                    status = getattr(exc, "status_code", None)
                    if status in {400, 401, 403, 404} or "not_found" in str(exc):
                        break  # a different model may exist; retrying this one will not help
                    if attempt == 2:
                        break
                    time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"model call failed for {tried}: {last_error}")

    def _record(self, response: Any, messages: list[dict[str, Any]]) -> LlmCall:
        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append({"id": block.id, "name": block.name, "input": dict(block.input)})

        usage = {
            "input_tokens": getattr(response.usage, "input_tokens", 0),
            "output_tokens": getattr(response.usage, "output_tokens", 0),
        }
        self.usage["input_tokens"] += usage["input_tokens"]
        self.usage["output_tokens"] += usage["output_tokens"]

        call = LlmCall(
            index=self.calls,
            stop_reason=response.stop_reason or "",
            text="\n".join(text_parts).strip(),
            tool_calls=tool_calls,
            usage=usage,
        )
        entry = {
            "call": call.index,
            "model": self.model,
            "stop_reason": call.stop_reason,
            "prompt_messages": _summarize_messages(messages),
            "assistant_text": call.text,
            "tool_calls": call.tool_calls,
            "usage": usage,
        }
        self.evidence.append_jsonl("model_trace.jsonl", entry)
        self._trace_lines.append(json.dumps(self.redactor.scrub(entry), sort_keys=True, default=str))
        return call

    # -- provenance -------------------------------------------------------

    def transcript_sha256(self) -> str:
        digest = sha256()
        for line in self._trace_lines:
            digest.update(line.encode())
        return "sha256:" + digest.hexdigest()

    def transcript_path(self) -> str:
        return self.evidence.rel(self.evidence.root / "model_trace.jsonl")


def _summarize_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the trace readable: images become placeholders, long text is clipped."""
    out = []
    for message in messages[-2:]:
        content = message.get("content")
        if isinstance(content, str):
            out.append({"role": message["role"], "content": content[:1200]})
            continue
        blocks = []
        for block in content or []:
            btype = block.get("type")
            if btype == "image":
                blocks.append({"type": "image", "note": "screenshot omitted from trace"})
            elif btype == "text":
                blocks.append({"type": "text", "text": block["text"][:1500]})
            else:
                blocks.append({k: v for k, v in block.items() if k != "content"} | (
                    {"content": str(block.get("content"))[:600]} if "content" in block else {}
                ))
        out.append({"role": message["role"], "content": blocks})
    return out
