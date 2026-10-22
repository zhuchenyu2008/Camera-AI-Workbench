"""OpenAI-compatible Chat Completions 网关。

业务层只依赖本模块定义的 ChatRequest/ChatResult；Provider 的私有字段和错误响应不向上泄漏。
所有模型 URL 都在请求前经过显式白名单和 SSRF 检查，禁止系统代理和重定向。
"""
from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import random
import socket
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence
from urllib.parse import urlsplit, urlunsplit

import httpx


class ProviderError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
        retry_after_ms: int | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retryable = retryable
        self.retry_after_ms = retry_after_ms


@dataclass(frozen=True)
class CapabilitySet:
    image_support: bool = False
    tools_support: bool = False
    json_mode_support: bool = False
    usage_support: bool = False


@dataclass(frozen=True)
class ChatRequest:
    model: str
    messages: list[dict[str, Any]]
    temperature: float | None = None
    max_tokens: int | None = None
    response_format: dict[str, Any] | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict[str, Any] | None = None
    timeout_seconds: float = 60.0
    max_response_bytes: int = 4 * 1024 * 1024

    def json(self) -> dict[str, Any]:
        body: dict[str, Any] = {"model": self.model, "messages": self.messages}
        if self.temperature is not None:
            body["temperature"] = self.temperature
        if self.max_tokens is not None:
            body["max_tokens"] = self.max_tokens
        if self.response_format is not None:
            body["response_format"] = self.response_format
        if self.tools:
            body["tools"] = self.tools
            if self.tool_choice is not None:
                body["tool_choice"] = self.tool_choice
        return body


@dataclass(frozen=True)
class ChatUsage:
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None


@dataclass(frozen=True)
class ToolCall:
    id: str | None
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ChatResult:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: ChatUsage | None = None
    finish_reason: str | None = None
    request_id: str | None = None
    provider_latency_ms: int = 0
    raw_safety: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    capabilities: CapabilitySet
    model: str
    latency_ms: int
    code: str | None = None
    message: str | None = None
    text_support: bool = False


class ProviderLike(Protocol):
    base_url: str
    api_key: str | None
    model_name: str


def image_content(image: bytes, *, detail: str = "low", mime_type: str = "image/jpeg") -> dict[str, Any]:
    if not image:
        raise ValueError("图片不能为空")
    if detail not in {"low", "high", "auto", "omit"}:
        raise ValueError("detail 不合法")
    item: dict[str, Any] = {
        "type": "image_url",
        "image_url": {"url": f"data:{mime_type};base64,{base64.b64encode(image).decode('ascii')}"},
    }
    if detail != "omit":
        item["image_url"]["detail"] = detail
    return item


def text_content(text: str) -> dict[str, str]:
    return {"type": "text", "text": text}


def _provider_value(provider: Any, name: str, default: Any = None) -> Any:
    if isinstance(provider, Mapping):
        return provider.get(name, default)
    return getattr(provider, name, default)


def _safe_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        raise ProviderError("PROVIDER_URL_INVALID", "Provider 地址必须是 HTTP(S) URL")
    if parsed.username or parsed.password:
        raise ProviderError("PROVIDER_URL_INVALID", "Provider 地址不能包含用户凭据")
    if parsed.fragment:
        raise ProviderError("PROVIDER_URL_INVALID", "Provider 地址不能包含 fragment")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ProviderError("PROVIDER_URL_INVALID", "Provider 端口不合法") from exc
    if parsed.scheme == "https" and port not in (None, 443):
        # HTTPS 自定义端口允许由显式 host allowlist 决定；不在这里阻断本地网关。
        pass
    path = parsed.path.rstrip("/")
    if not path.endswith("/v1") and not path.endswith("/v1/chat/completions"):
        # OpenAI-compatible 服务通常使用 /v1；允许用户配置根地址并统一补齐。
        path = f"{path}/v1" if path else "/v1"
    if path.endswith("/chat/completions"):
        endpoint = path
    else:
        endpoint = f"{path}/chat/completions"
    return urlunsplit((parsed.scheme, parsed.netloc, endpoint, parsed.query, ""))


def _is_private_or_local(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
        return not ip.is_global
    except ValueError:
        lowered = host.lower().rstrip(".")
        return lowered in {"localhost", "localhost.localdomain"} or lowered.endswith(".local")


def _normalized_host_port(host: str, port: int) -> str:
    normalized_host = host.lower().rstrip(".")
    if ":" in normalized_host and not normalized_host.startswith("["):
        normalized_host = f"[{normalized_host}]"
    return f"{normalized_host}:{port}"

def _configured_local_endpoints(values: Sequence[str]) -> set[str]:
    """Parse exact host:port entries; a bare host must never authorize every port."""
    result: set[str] = set()
    for value in values:
        try:
            parsed = urlsplit("//" + str(value).strip())
            if parsed.hostname and parsed.port and not parsed.path and not parsed.query and not parsed.fragment:
                result.add(_normalized_host_port(parsed.hostname, parsed.port))
        except ValueError:
            continue
    return result

def validate_provider_url(
    url: str,
    *,
    allowed_hosts: Sequence[str] = (),
    local_endpoints: Sequence[str] = (),
    development_mode: bool = False,
) -> str:
    endpoint = _safe_url(url)
    parsed = urlsplit(endpoint)
    host = (parsed.hostname or "").lower().rstrip(".")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    host_port = _normalized_host_port(host, port)
    allowed = {value.lower().strip().rstrip(".") for value in allowed_hosts if value.strip()}
    explicitly_local = host_port in _configured_local_endpoints(local_endpoints)
    if not development_mode and host not in allowed and host_port not in allowed and not explicitly_local:
        raise ProviderError("PROVIDER_HOST_DENIED", "Provider 地址不在允许列表中")
    if not development_mode and not explicitly_local:
        if _is_private_or_local(host):
            raise ProviderError("PROVIDER_ADDRESS_DENIED", "Provider 地址解析到受限网络")
        try:
            addresses = {item[4][0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)}
        except OSError as exc:
            raise ProviderError("PROVIDER_DNS_FAILED", "Provider 主机无法解析") from exc
        if not addresses or any(_is_private_or_local(address) for address in addresses):
            raise ProviderError("PROVIDER_ADDRESS_DENIED", "Provider 地址解析到受限网络")
    return endpoint


class OpenAICompatibleGateway:
    def __init__(
        self,
        provider: Any,
        *,
        allowed_hosts: Sequence[str] = (),
        local_endpoints: Sequence[str] = (),
        development_mode: bool = False,
        client: httpx.AsyncClient | None = None,
        max_retries: int = 2,
        backoff_base: float = 0.5,
        max_request_images: int = 60,
        max_request_bytes: int = 20 * 1024 * 1024,
    ) -> None:
        self.provider = provider
        self.allowed_hosts = tuple(allowed_hosts)
        self.local_endpoints = tuple(local_endpoints)
        self.development_mode = development_mode
        self._client = client
        self._owns_client = client is None
        self.max_retries = max(0, min(int(max_retries), 5))
        self.backoff_base = max(0.05, float(backoff_base))
        self.max_request_images = max(1, max_request_images)
        self.max_request_bytes = max(1024, max_request_bytes)

    async def __aenter__(self) -> "OpenAICompatibleGateway":
        await self._ensure_client()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.aclose()

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(follow_redirects=False, trust_env=False)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    def _endpoint(self) -> str:
        return validate_provider_url(
            str(_provider_value(self.provider, "base_url", "")),
            allowed_hosts=self.allowed_hosts,
            local_endpoints=self.local_endpoints,
            development_mode=self.development_mode,
        )

    def _headers(self) -> dict[str, str]:
        key = _provider_value(self.provider, "api_key")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    @staticmethod
    def _count_images(messages: list[dict[str, Any]]) -> tuple[int, int]:
        count = 0
        size = 0
        for message in messages:
            content = message.get("content", [])
            if isinstance(content, str):
                continue
            for item in content or []:
                if not isinstance(item, dict) or item.get("type") != "image_url":
                    continue
                count += 1
                url = str(item.get("image_url", {}).get("url", ""))
                size += len(url.encode("utf-8"))
        return count, size

    @staticmethod
    def _redact_error(value: Any) -> str:
        text = str(value).replace("\r", " ").replace("\n", " ")
        for marker in ("Bearer ", "api_key", "authorization", "password"):
            while marker.lower() in text.lower():
                index = text.lower().find(marker.lower())
                text = text[:index] + marker + "[REDACTED]"
        return text[:500]

    async def complete(self, request: ChatRequest) -> ChatResult:
        image_count, encoded_size = self._count_images(request.messages)
        if image_count > self.max_request_images:
            raise ProviderError("IMAGE_LIMIT_EXCEEDED", "图片数量超过 Provider 请求上限")
        if encoded_size > self.max_request_bytes:
            raise ProviderError("REQUEST_TOO_LARGE", "图片请求体超过上限")
        endpoint = self._endpoint()
        client = await self._ensure_client()
        attempts = 0
        last_error: ProviderError | None = None
        while attempts <= self.max_retries:
            started = time.perf_counter()
            attempts += 1
            try:
                async with client.stream(
                    "POST",
                    endpoint,
                    headers=self._headers(),
                    json=request.json(),
                    timeout=httpx.Timeout(request.timeout_seconds),
                ) as response:
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > request.max_response_bytes:
                            raise ProviderError("RESPONSE_TOO_LARGE", "Provider 响应超过上限")
                    if response.status_code >= 400:
                        retryable = response.status_code == 429 or response.status_code >= 500
                        retry_after = response.headers.get("retry-after")
                        message = f"Provider 请求失败（HTTP {response.status_code}）"
                        try:
                            data = json.loads(body.decode("utf-8", "replace"))
                            detail = data.get("error", {}).get("message") if isinstance(data, dict) else None
                            if detail:
                                message = self._redact_error(detail)
                        except Exception:
                            pass
                        error = ProviderError(
                            "PROVIDER_RATE_LIMITED" if response.status_code == 429 else "PROVIDER_HTTP_ERROR",
                            message,
                            status_code=response.status_code,
                            retryable=retryable,
                            retry_after_ms=_retry_after_ms(retry_after),
                        )
                        if retryable and attempts <= self.max_retries:
                            await self._sleep_retry(retry_after, attempts)
                            last_error = error
                            continue
                        raise error
                    try:
                        payload = json.loads(body.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise ProviderError("PROVIDER_INVALID_JSON", "Provider 返回不是合法 JSON") from exc
                    return self._parse_result(payload, int((time.perf_counter() - started) * 1000), response)
            except ProviderError as exc:
                if exc.code in {"RESPONSE_TOO_LARGE", "PROVIDER_INVALID_JSON", "IMAGE_LIMIT_EXCEEDED", "REQUEST_TOO_LARGE"}:
                    raise
                if not exc.retryable or attempts > self.max_retries:
                    raise
                last_error = exc
                await self._sleep_retry(None, attempts)
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                error = ProviderError("PROVIDER_NETWORK_ERROR", "Provider 网络请求失败", retryable=True)
                last_error = error
                if attempts > self.max_retries:
                    raise error from exc
                await self._sleep_retry(None, attempts)
            except httpx.HTTPError as exc:
                raise ProviderError("PROVIDER_HTTP_CLIENT_ERROR", "Provider 请求失败") from exc
        raise last_error or ProviderError("PROVIDER_ERROR", "Provider 请求失败")

    async def _sleep_retry(self, retry_after: str | None, attempt: int) -> None:
        delay: float | None = None
        retry_ms = _retry_after_ms(retry_after)
        if retry_ms is not None:
            delay = min(60.0, retry_ms / 1000)
        if delay is None:
            delay = min(30.0, self.backoff_base * (2 ** max(0, attempt - 1))) + random.uniform(0, 0.2)
        await asyncio.sleep(delay)

    @staticmethod
    def _parse_result(payload: Any, latency_ms: int, response: httpx.Response) -> ChatResult:
        if not isinstance(payload, dict) or not isinstance(payload.get("choices"), list) or not payload["choices"]:
            raise ProviderError("PROVIDER_INVALID_RESPONSE", "Provider 响应缺少 choices")
        first = payload["choices"][0]
        if not isinstance(first, dict) or not isinstance(first.get("message"), dict):
            raise ProviderError("PROVIDER_INVALID_RESPONSE", "Provider 响应缺少 message")
        message = first["message"]
        content = message.get("content", "")
        if isinstance(content, list):
            content = "".join(str(item.get("text", "")) for item in content if isinstance(item, dict))
        if not isinstance(content, str):
            content = str(content)
        calls: list[ToolCall] = []
        raw_calls = message.get("tool_calls") or []
        if not isinstance(raw_calls, list):
            raise ProviderError("PROVIDER_INVALID_TOOL_CALL", "Provider 工具调用列表无效")
        for call in raw_calls:
            if not isinstance(call, dict):
                raise ProviderError("PROVIDER_INVALID_TOOL_CALL", "Provider 工具调用结构无效")
            call_id = call.get("id")
            fn = call.get("function")
            if not isinstance(call_id, str) or not call_id or not isinstance(fn, dict):
                raise ProviderError("PROVIDER_INVALID_TOOL_CALL", "Provider 工具调用结构无效")
            name = fn.get("name")
            if not isinstance(name, str) or not name or len(name) > 100:
                raise ProviderError("PROVIDER_INVALID_TOOL_CALL", "Provider 工具名称无效")
            args = fn.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError as exc:
                    raise ProviderError("PROVIDER_INVALID_TOOL_CALL", "Provider 工具参数不是 JSON") from exc
            if not isinstance(args, dict):
                raise ProviderError("PROVIDER_INVALID_TOOL_CALL", "Provider 工具参数必须是对象")
            calls.append(ToolCall(call_id, name, args))
        usage_payload = payload.get("usage")
        usage = None
        if isinstance(usage_payload, dict):
            usage = ChatUsage(
                _int_or_none(usage_payload.get("prompt_tokens")),
                _int_or_none(usage_payload.get("completion_tokens")),
                _int_or_none(usage_payload.get("total_tokens")),
            )
        return ChatResult(
            content=content,
            tool_calls=calls,
            usage=usage,
            finish_reason=first.get("finish_reason"),
            request_id=response.headers.get("x-request-id") or payload.get("id"),
            provider_latency_ms=latency_ms,
            raw_safety={"id": payload.get("id"), "finish_reason": first.get("finish_reason")},
        )

    async def probe(self, requirements: CapabilitySet | None = None) -> ProbeResult:
        """Probe this exact model with the requested capabilities, not the vendor brand."""
        requirements = requirements or CapabilitySet()
        model = str(_provider_value(self.provider, "model_name", _provider_value(self.provider, "model", "")))
        timeout = float(_provider_value(self.provider, "timeout_ms", 60_000)) / 1000
        started = time.perf_counter()
        detected = {"text": False, "image": False, "tools": False, "json_mode": False, "usage": False}
        async def send(request: ChatRequest) -> ChatResult:
            return await self.complete(request)

        try:
            text_result = await send(ChatRequest(
                model=model,
                messages=[{"role": "user", "content": "只回复 OK"}],
                max_tokens=8,
                timeout_seconds=timeout,
            ))
            detected["text"] = bool(text_result.content or text_result.tool_calls)
            detected["usage"] = text_result.usage is not None

            if requirements.image_support:
                # A valid one-pixel PNG avoids shipping user imagery during a capability test.
                pixel = base64.b64decode(
                    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGNgYGAAAAAEAAH2FzhVAAAAAElFTkSuQmCC"
                )
                result = await send(ChatRequest(
                    model=model,
                    messages=[{"role": "user", "content": [
                        text_content("请识别图片中是否有可见内容，只回复 OK。"),
                        image_content(pixel, mime_type="image/png"),
                    ]}],
                    max_tokens=8,
                    timeout_seconds=timeout,
                ))
                detected["image"] = bool(result.content)

            if requirements.tools_support:
                tool = {"type": "function", "function": {
                    "name": "probe_capability",
                    "description": "Capability probe; call exactly once.",
                    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
                }}
                result = await send(ChatRequest(
                    model=model,
                    messages=[{"role": "user", "content": "请调用 probe_capability 工具。"}],
                    tools=[tool],
                    tool_choice={"type": "function", "function": {"name": "probe_capability"}},
                    max_tokens=16,
                    timeout_seconds=timeout,
                ))
                detected["tools"] = any(call.name == "probe_capability" for call in result.tool_calls)

            if requirements.json_mode_support:
                result = await send(ChatRequest(
                    model=model,
                    messages=[{"role": "user", "content": "仅输出 JSON 对象：{\"ok\":true}"}],
                    response_format={"type": "json_object"},
                    max_tokens=16,
                    timeout_seconds=timeout,
                ))
                try:
                    detected["json_mode"] = isinstance(json.loads(result.content), dict)
                except (TypeError, json.JSONDecodeError):
                    detected["json_mode"] = False

            capabilities = CapabilitySet(
                image_support=detected["image"],
                tools_support=detected["tools"],
                json_mode_support=detected["json_mode"],
                usage_support=detected["usage"],
            )
            missing = [
                name for flag, name in (
                    (requirements.image_support, "image"),
                    (requirements.tools_support, "tools"),
                    (requirements.json_mode_support, "json_mode"),
                    (requirements.usage_support, "usage"),
                ) if flag and not detected[name]
            ]
            code = "CAPABILITY_MISSING" if missing else None
            message = ",".join(missing) if missing else None
            return ProbeResult(
                not missing and detected["text"], capabilities, model,
                int((time.perf_counter() - started) * 1000), code, message, detected["text"],
            )
        except ProviderError as exc:
            capabilities = CapabilitySet(
                image_support=detected["image"], tools_support=detected["tools"],
                json_mode_support=detected["json_mode"], usage_support=detected["usage"],
            )
            return ProbeResult(
                False, capabilities, model, int((time.perf_counter() - started) * 1000), exc.code, exc.message, detected["text"]
            )


def _retry_after_ms(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return max(0, int(float(value) * 1000))
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        from email.utils import parsedate_to_datetime
        from datetime import UTC, datetime

        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return max(0, int((parsed - datetime.now(UTC)).total_seconds() * 1000))
    except (TypeError, ValueError, OverflowError):
        return None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


__all__ = [
    "CapabilitySet", "ChatRequest", "ChatResult", "ChatUsage", "OpenAICompatibleGateway", "ProbeResult",
    "ProviderError", "ToolCall", "image_content", "text_content", "validate_provider_url",
]
