"""Context assembly (FR-7, ADR-26, LLD 3.11).

Sits between canonical internal messages and what a specific provider receives.
Distinct from ContextPolicy (Phase 2 -- what a subagent may see) and
ContextCompactor (Phase 7 -- token-budget summarisation); collapsing those three
into one component is exactly what this separation prevents.

Phase 0 behaviour: pass the whole history through -- one agent, no curated
briefing yet -- while carrying each tool result's ContentProvenance as request
METADATA rather than as text the model reads.

That distinction is the point. Provenance injected into the prompt would be
content the model can be talked out of; as metadata it is a policy input that
travels with the request and can never be argued with. Provenance informs
policy; it does not enforce it, and it is never an instruction to the model.
"""

from __future__ import annotations

import json
from typing import Any

from .model import ModelRequest
from .primitives import ContentProvenance, Message, Role


def _schema_instruction(output_schema: dict[str, Any]) -> str:
    """What the model is told when its answer must match a schema."""
    return (
        "Answer with a single JSON document and nothing else: no prose, no explanation "
        "and no code fences. It must validate against this JSON Schema:\n"
        + json.dumps(output_schema, sort_keys=True)
    )


def _briefed_entry(uri: str, p: ContentProvenance) -> dict[str, Any]:
    """FR-83 (M18a): what a subagent was briefed with, by its uri and its labels.

    Never its content: that would put the content in the metadata channel as well as
    the prompt, and the metadata channel is the one the model cannot argue with
    (ADR-26).
    """
    return {
        "briefed_input": uri,
        "origin": p.origin.value,
        "instruction_authority": p.instruction_authority.value,
        "trust_zone": p.trust_zone.value,
        "taint_flags": sorted(flag.value for flag in p.taint_flags),
    }


class ContextAssembler:
    def build(
        self,
        history: list[Message],
        tool_schemas: list[dict[str, Any]] | None = None,
        *,
        instructions: str | None = None,
        model_settings: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        output_schema: dict[str, Any] | None = None,
        briefed_inputs: tuple[tuple[str, ContentProvenance], ...] = (),
    ) -> ModelRequest:
        if output_schema is not None:
            # NFR-1: every provider reads the instructions, and not every provider has
            # a structured-output mode. The schema is stated here so a child is asked
            # for JSON whatever it is running on; an adapter that can also ask for it
            # in the provider's own way does that as well (FR-71, M18).
            instructions = "\n\n".join(
                part for part in (instructions, _schema_instruction(output_schema)) if part
            )
        request_metadata = dict(metadata or {})
        provenance = [_briefed_entry(uri, p) for uri, p in briefed_inputs]
        provenance += self._provenance_manifest(history)
        if provenance:
            request_metadata["provenance"] = provenance
        return ModelRequest(
            messages=tuple(history),
            tools=tuple(tool_schemas or ()),
            instructions=instructions,
            # None until a node asks for structured output (FR-71, M18): the slot
            # existed from Phase 0 so this call site did not have to change.
            output_schema=output_schema,
            model_settings=dict(model_settings or {}),
            metadata=request_metadata,
        )

    def _provenance_manifest(self, history: list[Message]) -> list[dict[str, Any]]:
        """One entry per tool result carried in the history.

        Phase 2's ContextPolicy reads this to decide what a subagent may see;
        Phase 4's policy engine reads it to decide what a tainted result may
        trigger. Nothing reads it in Phase 0 -- but it is assembled correctly
        now so neither has to retrofit it.
        """
        manifest: list[dict[str, Any]] = []
        for message in history:
            if message.role is not Role.TOOL:
                continue
            for result in message.tool_results:
                p = result.provenance
                manifest.append(
                    {
                        "tool_call_id": result.tool_call_id,
                        "origin": p.origin.value,
                        "instruction_authority": p.instruction_authority.value,
                        "trust_zone": p.trust_zone.value,
                        "taint_flags": sorted(flag.value for flag in p.taint_flags),
                        "is_error": result.is_error,
                    }
                )
        return manifest
