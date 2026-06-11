"""
LLM calls (OpenAI-compatible) + robust JSON parsing
"""
from __future__ import annotations

import json
import re
from json import JSONDecodeError
from typing import Any, Dict, List

import requests


def call_chat(
    messages: List[Dict[str, Any]],
    model: str,
    api_key: str,
    base_url: str,
    temperature: float = 0.0,
    timeout: float = 180.0,
) -> str:
    if not api_key:
        raise ValueError("API key not set")
    url = base_url.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {"model": model, "messages": messages, "temperature": temperature}
    resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def safe_json_loads(text: str) -> Any:
    """Robust parse: strip ```json fences, then grab the first balanced {...} block."""
    if text is None:
        return None
    s = text.strip()
    # strip markdown fences
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    try:
        return json.loads(s)
    except JSONDecodeError:
        pass
    # take the first balanced curly-brace block
    start = s.find("{")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(s)):
        if s[i] == "{":
            depth += 1
        elif s[i] == "}":
            depth -= 1
            if depth == 0:
                cand = s[start:i + 1]
                try:
                    return json.loads(cand)
                except JSONDecodeError:
                    break
    return None


def is_error_response(parsed: Any) -> bool:
    if not isinstance(parsed, dict):
        return False
    if isinstance(parsed.get("result"), dict):
        return str(parsed["result"].get("status", "")).lower() == "error"
    return str(parsed.get("status", "")).lower() == "error"


def extract_with_prompt(
    prompt: str,
    model: str,
    api_key: str,
    base_url: str,
    temperature: float = 0.0,
    max_retries: int = 2,
) -> Dict[str, Any]:
    """Send one extraction request; returns the parsed result dict
    (id/name/value/unit/source_text)."""
    messages = [{"role": "user", "content": prompt}]
    last_err = None
    for attempt in range(max_retries + 1):
        try:
            raw = call_chat(messages, model, api_key, base_url, temperature)
            parsed = safe_json_loads(raw)
            if parsed is None:
                last_err = "JSON parse failed"
                continue
            result = parsed.get("result", parsed) if isinstance(parsed, dict) else None
            if isinstance(result, dict):
                return result
            last_err = "response has no result object"
        except Exception as e:
            last_err = str(e)
    return {"value": None, "unit": None, "source_text": None,
            "_error": last_err or "unknown error"}
