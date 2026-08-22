"""LLM orchestration scaffold for the Navi Analytics Agent.

The model receives a user question and approved tool definitions. It can request
an allowed tool, receive an aggregate result, and then produce a grounded answer.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI

from agent_tools import TOOLS, dispatch_tool

load_dotenv()
client = OpenAI()
MODEL = os.getenv("OPENAI_MODEL", "gpt-5-mini")

SYSTEM_INSTRUCTIONS = """
You are Navi Analytics Agent, an assistant for product analytics questions.
Use approved tools for every numerical claim. Never invent metric values.
Use dashboard context as a default date range and filter set when it is useful.
If a request is ambiguous, ask one concise clarification question.
For each data-backed answer, state the metric, date range, filters, and result.
Do not return user-level records, personal data, or unrestricted SQL.
""".strip()


def run_analytics_agent(question: str, dashboard_context: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """Return a grounded answer and a compact tool-call audit trail."""
    input_items: list[Any] = [
        {
            "role": "user",
            "content": (
                f"Dashboard context: {json.dumps(dashboard_context)}\n"
                f"Question: {question}"
            ),
        }
    ]
    audit: list[dict[str, Any]] = []
    started = time.perf_counter()

    # A short loop keeps the design simple while allowing the model to select a
    # tool, receive a result, and produce a final natural-language explanation.
    for _ in range(4):
        response = client.responses.create(
            model=MODEL,
            instructions=SYSTEM_INSTRUCTIONS,
            tools=TOOLS,
            input=input_items,
            max_output_tokens=400,
        )
        input_items.extend(response.output)
        function_calls = [item for item in response.output if item.type == "function_call"]

        if not function_calls:
            audit.append({"agent_latency_ms": round((time.perf_counter() - started) * 1000, 1)})
            return response.output_text, audit

        for call in function_calls:
            arguments = json.loads(call.arguments)
            result = dispatch_tool(call.name, arguments)
            audit.append({"tool": call.name, "arguments": arguments, "result": result})
            input_items.append(
                {
                    "type": "function_call_output",
                    "call_id": call.call_id,
                    "output": json.dumps(result, default=str),
                }
            )

    return "I was unable to complete that request within the tool-call limit.", audit
