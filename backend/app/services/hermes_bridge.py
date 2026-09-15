"""
Hermes bridge boundary.

Hermes CLI integration is not enabled in this runtime. Keep the route importable
and make unavailability explicit.
"""
from __future__ import annotations

import json
from typing import Dict, List


def build_hermes_prompt(message: str, observations: Dict, history: List[Dict]) -> str:
    payload = {
        "message": message,
        "observations": observations,
        "history": history[-6:] if history else [],
    }
    return json.dumps(payload, ensure_ascii=False, default=str)


def invoke_hermes(prompt: str) -> Dict:
    return {
        "error": "HERMES_NOT_ENABLED",
        "answer": "",
    }


def hermes_error_message(error: str | None = None) -> str:
    return "Hermes Agent 尚未在当前部署中启用。"
