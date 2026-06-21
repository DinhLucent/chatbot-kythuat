from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests
from pydantic import BaseModel, ValidationError

from .utils import short_hash, utc_now, write_json


DEFAULT_LLM_MODEL = "deepseek-v4-flash"


@dataclass
class LLMUsage:
    request_count: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    models: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_count": self.request_count,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "models": self.models,
        }


class LLMUnavailable(RuntimeError):
    pass


class OpenAICompatibleLLM:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
        provider_name: str = "openai_compatible",
    ) -> None:
        self.base_url = (base_url if base_url is not None else os.getenv("LLM_BASE_URL", "")).rstrip("/")
        self.api_key = api_key if api_key is not None else os.getenv("LLM_API_KEY", "")
        self.model = model if model is not None else os.getenv("LLM_MODEL", DEFAULT_LLM_MODEL)
        raw_timeout = timeout if timeout is not None else float(os.getenv("LLM_TIMEOUT_SECONDS", "60") or "60")
        self.timeout = float(raw_timeout)
        self.provider_name = provider_name
        self.usage = LLMUsage()

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.0,
        response_format: dict[str, Any] | None = None,
        max_tokens: int | None = None,
        extra_payload: dict[str, Any] | None = None,
    ) -> str:
        if not self.configured:
            raise LLMUnavailable("LLM is not configured. Set LLM_BASE_URL, LLM_API_KEY, and LLM_MODEL.")
        url = f"{self.base_url}/chat/completions"
        payload: dict[str, Any] = {"model": self.model, "messages": messages, "temperature": temperature}
        if response_format:
            payload["response_format"] = response_format
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if extra_payload:
            payload.update(extra_payload)
        response = requests.post(
            url,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            timeout=self.timeout,
        )
        response.raise_for_status()
        data = response.json()
        self.usage.request_count += 1
        self.usage.models.append(self.model)
        usage = data.get("usage") if isinstance(data, dict) else {}
        if isinstance(usage, dict):
            self.usage.prompt_tokens += int(usage.get("prompt_tokens") or 0)
            self.usage.completion_tokens += int(usage.get("completion_tokens") or 0)
            self.usage.total_tokens += int(usage.get("total_tokens") or 0)
        choices = data.get("choices") if isinstance(data, dict) else None
        if not choices:
            raise LLMUnavailable(f"LLM response has no choices: {data}")
        message = choices[0].get("message") if isinstance(choices[0], dict) else {}
        return str(message.get("content") or "").strip()

    def complete_structured(
        self,
        messages: list[dict[str, str]],
        response_model: type[BaseModel],
        *,
        temperature: float = 0.0,
        max_retries: int = 3,
        max_tokens: int | None = None,
        audit_dir: Path | None = None,
        task_name: str | None = None,
        source_ids: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        allow_json_object_fallback: bool = True,
    ) -> Any:
        schema = get_strict_json_schema(response_model)
        response_format = {
            "type": "json_schema",
            "json_schema": {
                "name": response_model.__name__,
                "schema": schema,
                "strict": True,
            },
        }
        messages_copy = list(messages)
        schema_instruction = (
            "You are a structured-output system. Return only JSON that exactly matches this schema:\n"
            f"{json.dumps(schema, ensure_ascii=False, indent=2)}\n"
            "Do not include prose, markdown fences, or keys outside the schema."
        )
        if messages_copy and messages_copy[0]["role"] == "system":
            messages_copy[0]["content"] = f"{messages_copy[0]['content']}\n\n{schema_instruction}"
        else:
            messages_copy.insert(0, {"role": "system", "content": schema_instruction})

        call_id = build_llm_call_id(task_name or response_model.__name__)
        audit_payload: dict[str, Any] | None = None
        if audit_dir is not None:
            audit_payload = {
                "schema_version": "baxter_llm_structured_call_v1",
                "call_id": call_id,
                "task_name": task_name or response_model.__name__,
                "created_at": utc_now(),
                "provider": self.provider_name,
                "base_url": safe_base_url(self.base_url),
                "model": self.model,
                "source_ids": source_ids or [],
                "metadata": metadata or {},
                "response_model": response_model.__name__,
                "response_schema": schema,
                "messages": messages_copy,
                "attempts": [],
            }

        last_error: Exception | None = None
        for attempt in range(max(1, max_retries)):
            response_text = ""
            attempt_log: dict[str, Any] = {"attempt": attempt + 1, "response_format": "json_schema"}
            try:
                try:
                    response_text = self.complete(
                        messages_copy,
                        temperature=temperature,
                        response_format=response_format,
                        max_tokens=max_tokens,
                    )
                except requests.HTTPError as exc:
                    if allow_json_object_fallback and exc.response is not None and exc.response.status_code == 400:
                        attempt_log["response_format"] = "json_object"
                        attempt_log["fallback_reason"] = "json_schema_rejected_with_400"
                        response_text = self.complete(
                            messages_copy,
                            temperature=temperature,
                            response_format={"type": "json_object"},
                            max_tokens=max_tokens,
                        )
                    else:
                        raise
                parsed = response_model.model_validate_json(response_text)
                attempt_log["ok"] = True
                attempt_log["raw_response"] = clamp_for_audit(response_text)
                if audit_payload is not None:
                    attempt_log["parsed"] = parsed.model_dump()
                    audit_payload["attempts"].append(attempt_log)
                    audit_payload["status"] = "passed"
                    audit_payload["usage"] = self.usage.to_dict()
                    write_llm_audit(audit_dir, call_id, audit_payload)
                return parsed
            except (ValidationError, json.JSONDecodeError, ValueError) as exc:
                last_error = exc
                attempt_log["ok"] = False
                attempt_log["error"] = str(exc)
                attempt_log["raw_response"] = clamp_for_audit(response_text)
                if audit_payload is not None:
                    audit_payload["attempts"].append(attempt_log)
                messages_copy.append({"role": "assistant", "content": response_text})
                messages_copy.append(
                    {
                        "role": "user",
                        "content": f"Your previous output failed schema validation: {str(exc)}. Return corrected JSON only.",
                    }
                )
            except Exception as exc:
                last_error = exc
                attempt_log["ok"] = False
                attempt_log["error"] = str(exc)
                attempt_log["raw_response"] = clamp_for_audit(response_text)
                if audit_payload is not None:
                    audit_payload["attempts"].append(attempt_log)
                break

        if audit_payload is not None:
            audit_payload["status"] = "failed"
            audit_payload["error"] = str(last_error)
            audit_payload["usage"] = self.usage.to_dict()
            write_llm_audit(audit_dir, call_id, audit_payload)
        if last_error is None:
            raise LLMUnavailable("LLM structured call failed without a captured error.")
        raise last_error


def get_strict_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    schema = model.model_json_schema()

    def walk(s: Any) -> None:
        if not isinstance(s, dict):
            return
        if s.get("type") == "object":
            s["additionalProperties"] = False
            props = s.get("properties", {})
            if props:
                s["required"] = list(props.keys())
            for value in props.values():
                walk(value)
        elif s.get("type") == "array" and "items" in s:
            walk(s["items"])

        for unsupported in ("title", "default"):
            s.pop(unsupported, None)

        for key in ("anyOf", "allOf", "oneOf"):
            if key in s:
                for sub in s[key]:
                    walk(sub)

    walk(schema)
    if "$defs" in schema:
        for value in schema["$defs"].values():
            walk(value)
    return schema


def build_llm_call_id(task_name: str) -> str:
    stamp = utc_now().replace(":", "").replace("-", "").replace("+0000", "Z")
    return f"llm_{short_hash(task_name, 8)}_{stamp}_{short_hash(stamp + task_name, 8)}"


def safe_base_url(base_url: str) -> str:
    return base_url.rstrip("/") if base_url else ""


def clamp_for_audit(text: str, limit: int = 20000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "...[truncated]"


def write_llm_audit(audit_dir: Path, call_id: str, payload: dict[str, Any]) -> None:
    write_json(audit_dir / f"{call_id}.json", payload)


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def external_llm_allowed(override: bool | None = None) -> bool:
    if override is not None:
        return bool(override)
    return env_flag("LLM_EXTERNAL_UPLOAD_ALLOWED", False)


def is_local_llm_endpoint(base_url: str) -> bool:
    lowered = base_url.lower()
    return any(host in lowered for host in ("127.0.0.1", "localhost", "::1"))


def source_upload_allowed(client: OpenAICompatibleLLM, override: bool | None = None) -> bool:
    return is_local_llm_endpoint(client.base_url) or external_llm_allowed(override)


def answer_system_prompt() -> str:
    return (
        "You are an internal technical assistant for Baxter IC source manuals. "
        "Answer in Vietnamese, but preserve English technical terms and codes such as Alarm, Access Pressure, SW 3.x, and AW8047. "
        "Use only the supplied evidence blocks. Do not invent service, safety, alarm, unit, part, or software facts. "
        "If the evidence is insufficient, say that the loaded sources do not show enough information. "
        "For service or safety topics, remind the user to follow the official manual and Baxter safety process."
    )


def build_answer_prompt(question: str, context_blocks: list[dict[str, Any]]) -> str:
    return json.dumps(
        {
            "question": question,
            "answer_requirements": [
                "Vietnamese answer",
                "short and actionable",
                "include cited source labels like [1], [2]",
                "do not use facts outside context",
                "do not cite a source unless the cited source directly supports the claim",
            ],
            "context": context_blocks,
        },
        ensure_ascii=False,
    )
