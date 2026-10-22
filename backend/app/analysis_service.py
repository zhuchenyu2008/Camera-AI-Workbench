from __future__ import annotations

"""三层分析 Worker 应用服务。

模块不导入 FastAPI、Workbench 或具体模型；主 agent 注入 SQLAlchemy repository 与
OpenAI-compatible gateway adapter。图片/网络调用在数据库事务外；结果与 Job 成功状态在同一
Session 事务提交。主 ORM 只需在 repository 中适配文档的 minute_observation、activity_segment、
summary 等字段。
"""

import asyncio
import inspect
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping, Protocol, Sequence

from sqlalchemy.orm import Session

from app.queue import JobRecord, JobState, LeaseLostError, TaskQueue, utc_now_ms
from app.scheduler import SessionRunner


UTC = timezone.utc
LOG = logging.getLogger("camera_ai_workbench.analysis")

class AnalysisServiceError(RuntimeError):
    pass


class InvalidModelOutput(AnalysisServiceError):
    pass


class EvidenceExpired(AnalysisServiceError):
    pass


@dataclass(frozen=True)
class AnalysisRequest:
    job_id: str
    layer: str
    camera_id: str | None
    start_at_ms: int | None
    end_at_ms: int | None
    input_frame_ids: tuple[str, ...]
    payload: Mapping[str, Any]
    attempt: int
    prompt_version: str
    schema_version: str


@dataclass(frozen=True)
class AnalysisResponse:
    data: Mapping[str, Any]
    provider: str | None = None
    model: str | None = None
    usage: Mapping[str, Any] = field(default_factory=dict)
    latency_ms: int | None = None
    request_id: str | None = None
    authorized_frame_ids: frozenset[str] = frozenset()


@dataclass(frozen=True)
class AnalysisRunResult:
    state: str
    job: JobRecord
    output: Mapping[str, Any] | None = None
    error_code: str | None = None


class AnalysisGateway(Protocol):
    async def analyze(self, request: AnalysisRequest) -> AnalysisResponse | Mapping[str, Any]: ...


class AnalysisRepository(Protocol):
    """应用 ORM adapter。所有时间均 UTC epoch milliseconds，区间 [start, end)。"""

    def validate_input_evidence(
        self, session: Session, camera_id: str | None, frame_ids: Sequence[str]
    ) -> None: ...

    def persist_l1(self, session: Session, job: JobRecord, output: Mapping[str, Any]) -> Any: ...

    def persist_l2(self, session: Session, job: JobRecord, output: Mapping[str, Any]) -> Any: ...

    def persist_l3(self, session: Session, job: JobRecord, output: Mapping[str, Any]) -> Any: ...

    def record_usage(
        self, session: Session, job: JobRecord, response: AnalysisResponse, *, retry: bool
    ) -> None: ...


def _parse_time(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        # 输入一般为 ms；显式小量按秒处理以兼容工具/模型给出的 epoch seconds。
        number = float(value)
        return int(number * 1000) if abs(number) < 10**11 else int(number)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return int(parsed.astimezone(UTC).timestamp() * 1000)


def _validate_window(job: JobRecord, data: Mapping[str, Any]) -> None:
    window = data.get("window")
    if isinstance(window, Mapping):
        returned_start = _parse_time(window.get("start_ms", window.get("start")))
        returned_end = _parse_time(window.get("end_ms", window.get("end")))
        if returned_start is not None and job.period_start_ms is not None and returned_start != job.period_start_ms:
            raise InvalidModelOutput("响应窗口开始时间不匹配任务输入")
        if returned_end is not None and job.period_end_ms is not None and returned_end != job.period_end_ms:
            raise InvalidModelOutput("响应窗口结束时间不匹配任务输入")


def _validate_frame_references(job: JobRecord, data: Mapping[str, Any]) -> None:
    allowed = set(job.input_frame_ids)
    frame_keys = {"evidence_frame_ids", "evidence_ids", "anchor_frame_ids", "frame_ids"}

    def visit(value: Any, key: str = "") -> None:
        if isinstance(value, Mapping):
            for child_key, child in value.items():
                visit(child, str(child_key))
        elif isinstance(value, (list, tuple)):
            if key in frame_keys:
                for reference in value:
                    if not isinstance(reference, str) or reference not in allowed:
                        raise InvalidModelOutput("响应引用了不属于本任务快照的图片")
            else:
                for child in value:
                    visit(child, key)

    visit(data)


def _validate_segment_ranges(job: JobRecord, data: Mapping[str, Any]) -> None:
    if job.period_start_ms is None or job.period_end_ms is None:
        return
    start_bound, end_bound = job.period_start_ms, job.period_end_ms
    keys = ("segments", "observations", "candidate_activities", "intervals", "unknown_intervals", "gaps")

    def validate_item(item: Any) -> None:
        if not isinstance(item, Mapping):
            return
        start = _parse_time(item.get("start_ms", item.get("start")))
        end = _parse_time(item.get("end_ms", item.get("end")))
        if start is not None or end is not None:
            if start is None or end is None or end <= start:
                raise InvalidModelOutput("模型返回了无效时间区间")
            if start < start_bound or end > end_bound:
                raise InvalidModelOutput("模型输出区间越过任务窗口边界")
        for nested in ("intervals", "observations", "segments", "unknown_intervals", "gaps"):
            values = item.get(nested)
            if isinstance(values, list):
                for child in values:
                    validate_item(child)

    for key in keys:
        values = data.get(key)
        if isinstance(values, list):
            for item in values:
                validate_item(item)


def validate_model_output(job: JobRecord, output: Mapping[str, Any], *, authorized_frame_ids: Sequence[str] = ()) -> dict[str, Any]:
    """Strictly parse a layer contract and bind every reference to this Job snapshot."""
    if not isinstance(output, Mapping):
        raise InvalidModelOutput("模型响应必须是 JSON 对象")

    layer = job.layer.strip().lower()
    canonical_layer = "L1" if layer in {"l1", "minute", "event_l1", "realtime_l1"} else (
        "L2" if layer == "l2" else "L3" if layer in {"l3", "summary", "hourly_summary", "daily_summary"} else None
    )
    if canonical_layer is None:
        raise InvalidModelOutput(f"不支持的分析层级: {job.layer}")

    try:
        from app.ai_schemas import L1Response, L2Response, L3Response, TimeRange, parse_layer_result, validate_authorized_evidence

        parsed = parse_layer_result(canonical_layer, dict(output))
        expected_start = int(job.period_start_ms) if job.period_start_ms is not None else None
        expected_end = int(job.period_end_ms) if job.period_end_ms is not None else None
        if expected_start is not None and expected_end is not None:
            returned = parsed.window if canonical_layer in {"L1", "L2"} else parsed.period
            if int(returned.start.timestamp() * 1000) != expected_start or int(returned.end.timestamp() * 1000) != expected_end:
                raise ValueError("模型返回窗口必须与任务窗口完全一致")
        if isinstance(parsed, (L1Response, L2Response)):
            allowed_observations = job.payload.get("observation_ids")
            validate_authorized_evidence(
                parsed,
                allowed_frame_ids=set(job.input_frame_ids) | set(authorized_frame_ids),
                allowed_observation_ids=(set(allowed_observations) if isinstance(allowed_observations, (list, tuple)) else None),
                allowed_window=(TimeRange(start=parsed.window.start, end=parsed.window.end)),
                max_inference_gap_seconds=int(job.payload.get("max_inference_gap_seconds", 300)),
            )
        elif isinstance(parsed, L3Response):
            allowed_segments = set(job.payload.get("segment_ids", job.payload.get("source_segment_ids", ())))
            if not set(parsed.source_segment_ids).issubset(allowed_segments):
                raise ValueError("摘要引用了不属于本次输入的第二层活动段")
        return parsed.model_dump(mode="json")
    except Exception as exc:
        # Never pass validation payloads or model text into safe errors/logs.
        raise InvalidModelOutput("模型输出未通过对应层级的结构、时间或证据校验") from exc


def _response(value: AnalysisResponse | Mapping[str, Any]) -> AnalysisResponse:
    if isinstance(value, AnalysisResponse):
        return value
    if not isinstance(value, Mapping):
        raise InvalidModelOutput("AI Gateway 返回了无效结果")
    if "data" in value and isinstance(value.get("data"), Mapping):
        return AnalysisResponse(
            data=value["data"],
            provider=value.get("provider"),
            model=value.get("model"),
            usage=value.get("usage") or {},
            latency_ms=value.get("latency_ms"),
            request_id=value.get("request_id"),
            authorized_frame_ids=frozenset(value.get("authorized_frame_ids") or ()),
        )
    return AnalysisResponse(data=value)


class AnalysisService:
    """可由 Worker 主循环调用的三层任务执行器。"""

    def __init__(
        self,
        queue: TaskQueue,
        repository: AnalysisRepository,
        gateway: AnalysisGateway,
        *,
        session_factory: Callable[[], Session] | None = None,
        clock: Callable[[], int] = utc_now_ms,
        lease_ms: int = 60_000,
    ) -> None:
        self.queue = queue
        self.repository = repository
        self.gateway = gateway
        self.session_factory = session_factory
        self.runner = SessionRunner(session_factory)
        self.clock = clock
        self.lease_ms = max(1000, int(lease_ms))

    async def run_once(self, worker_id: str) -> AnalysisRunResult | None:
        """领取并执行一个任务；返回 None 表示队列当前为空。"""
        job = await asyncio.to_thread(self.queue.claim, worker_id, lease_ms=self.lease_ms)
        if job is None:
            return None
        return await self.process_claimed(job)

    async def process_claimed(self, job: JobRecord) -> AnalysisRunResult:
        if self.session_factory is None:
            # 允许显式注入 session_factory，避免把同步 Session 生命周期泄漏到 async 网络调用。
            return await self._process_without_sessions(job)
        try:
            running = await asyncio.to_thread(self.queue.mark_running, job)
            request = await asyncio.to_thread(self._prepare_request, running)
            heartbeat_stop = asyncio.Event()
            heartbeat_task = asyncio.create_task(self._heartbeat_loop(running, heartbeat_stop))
            try:
                result = self.gateway.analyze(request)
                if inspect.isawaitable(result):
                    result = await result
                response = _response(result)
                output = validate_model_output(running, response.data, authorized_frame_ids=response.authorized_frame_ids)
                completed = await asyncio.to_thread(self._persist_success, running, output, response)
                return AnalysisRunResult(state=completed.state, job=completed, output=output)
            finally:
                heartbeat_stop.set()
                heartbeat_task.cancel()
                await asyncio.gather(heartbeat_task, return_exceptions=True)
        except EvidenceExpired:
            failed = await asyncio.to_thread(
                self.queue.fail,
                job,
                error_code="EVIDENCE_EXPIRED",
                safe_message="任务所需原始证据已过期，未用邻近新帧替代",
                retryable=False,
            )
            return AnalysisRunResult(state=failed.state, job=failed, error_code=failed.error_code)
        except InvalidModelOutput:
            failed = await asyncio.to_thread(
                self.queue.fail,
                job,
                error_code="INVALID_MODEL_OUTPUT",
                safe_message="模型输出未通过结构、时间或证据引用校验",
                retryable=False,
                needs_review=True,
            )
            return AnalysisRunResult(state=failed.state, job=failed, error_code=failed.error_code)
        except LeaseLostError:
            raise
        except Exception as exc:
            # 不持久化原始 exception 文本，可能包含 URL、凭据、模型 payload 等敏感信息。
            code, retryable, retry_after = self._classify_gateway_error(exc)
            failed = await asyncio.to_thread(
                self.queue.fail,
                job,
                error_code=code,
                safe_message="分析任务执行失败；请查看服务端脱敏日志",
                retryable=retryable,
                retry_after_ms=retry_after,
            )
            return AnalysisRunResult(state=failed.state, job=failed, error_code=failed.error_code)

    async def _heartbeat_loop(self, job: JobRecord, stop: asyncio.Event) -> None:
        """Renew the lease while a provider call is in flight."""
        interval = max(1.0, min(self.lease_ms / 3000.0, 10.0))
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                return
            except asyncio.TimeoutError:
                pass
            try:
                renewed = await asyncio.to_thread(
                    self.queue.heartbeat,
                    job,
                    lease_token=job.lease_token,
                    lease_ms=self.lease_ms,
                )
                job = renewed
            except LeaseLostError:
                LOG.warning("analysis_lease_lost_during_call job_id=%s", job.id)
                return
            except Exception as exc:
                # A transient database failure is safe to retry on the next tick;
                # the fenced commit still prevents stale workers from publishing.
                LOG.warning("analysis_lease_heartbeat_failed job_id=%s error_type=%s", job.id, type(exc).__name__)
    async def _process_without_sessions(self, job: JobRecord) -> AnalysisRunResult:
        """显式 Session 模式下供适配器先 mark_running，再自行接管事务的提示性错误路径。"""
        raise AnalysisServiceError(
            "process_claimed 需要 session_factory；如需外部事务编排，请复用 prepare_request/commit_result 接口"
        )

    def _prepare_request(self, job: JobRecord) -> AnalysisRequest:
        def operation(session: Session | None):
            self.repository.validate_input_evidence(session, job.camera_id, job.input_frame_ids)
            return AnalysisRequest(
                job_id=job.id,
                layer=job.layer,
                camera_id=job.camera_id,
                start_at_ms=job.period_start_ms,
                end_at_ms=job.period_end_ms,
                input_frame_ids=tuple(job.input_frame_ids),
                payload={**dict(job.payload), **dict(getattr(job, "input_ref_json", {}) or {})},
                attempt=job.attempts,
                prompt_version=job.prompt_version,
                schema_version=job.schema_version,
            )

        return self.runner.run(operation)

    def _persist_success(
        self, job: JobRecord, output: Mapping[str, Any], response: AnalysisResponse
    ) -> JobRecord:
        def operation(session: Session | None):
            current = self.queue.repository.get(session, job.id)
            if current is None or current.lease_token != job.lease_token:
                raise LeaseLostError("结果提交时任务租约已失效")
            layer = job.layer.lower()
            if layer in {"l1", "minute", "event_l1", "realtime_l1"}:
                result_ref = self.repository.persist_l1(session, job, output)
            elif layer == "l2":
                result_ref = self.repository.persist_l2(session, job, output)
            elif layer in {"l3", "summary", "hourly_summary", "daily_summary"}:
                result_ref = self.repository.persist_l3(session, job, output)
            else:
                raise AnalysisServiceError(f"不支持的分析层级: {layer}")
            self.repository.record_usage(session, job, response, retry=job.attempts > 1)
            return self.queue.complete(job, result_ref=result_ref, session=session)

        return self.runner.run(operation)

    @staticmethod
    def _classify_gateway_error(error: Exception) -> tuple[str, bool, int | None]:
        code = getattr(error, "code", None)
        status = getattr(error, "status_code", None)
        retry_after = getattr(error, "retry_after_ms", None)
        normalized = str(code or "").strip().upper()
        if normalized in {"EVIDENCE_EXPIRED", "EVIDENCE_GONE"} or status == 410:
            return "EVIDENCE_EXPIRED", False, None
        if status in {401, 403} or normalized in {"AUTH_FAILED", "PROVIDER_AUTH_FAILED"}:
            return "PROVIDER_AUTH_FAILED", False, None
        if status == 429:
            return "RATE_LIMITED", True, retry_after
        if isinstance(status, int) and status >= 500:
            return f"HTTP_{status}", True, retry_after
        if normalized in {"MODEL_NOT_FOUND", "UNSUPPORTED_IMAGE", "UNSUPPORTED_TOOLS", "INVALID_REQUEST", "TOOL_BUDGET_EXCEEDED", "PROVIDER_INVALID_TOOL_CALL", "PROVIDER_INVALID_TOOL_RESULT", "PROVIDER_INVALID_RESPONSE"}:
            return normalized, False, None
        if isinstance(status, int) and 400 <= status < 500:
            return f"HTTP_{status}", False, None
        if isinstance(retry_after, int) or "TIMEOUT" in normalized or "NETWORK" in normalized:
            return normalized or "NETWORK_ERROR", True, retry_after
        return "ANALYSIS_FAILED", False, None

    def prepare_request(self, job: JobRecord, *, session: Session) -> AnalysisRequest:
        """供外部 Worker 编排在现有短事务内固化输入；返回后可关闭 Session 再调用模型。"""
        self.repository.validate_input_evidence(session, job.camera_id, job.input_frame_ids)
        return AnalysisRequest(
            job_id=job.id,
            layer=job.layer,
            camera_id=job.camera_id,
            start_at_ms=job.period_start_ms,
            end_at_ms=job.period_end_ms,
            input_frame_ids=tuple(job.input_frame_ids),
            payload=dict(job.payload),
            attempt=job.attempts,
            prompt_version=job.prompt_version,
            schema_version=job.schema_version,
        )

    def commit_result(
        self,
        job: JobRecord,
        response: AnalysisResponse | Mapping[str, Any],
        *,
        session: Session,
    ) -> JobRecord:
        """外部 Worker 持有 Session 时，用同一事务持久化结果并将 Job 标为 succeeded。"""
        normalized = _response(response)
        output = validate_model_output(job, normalized.data, authorized_frame_ids=normalized.authorized_frame_ids)
        layer = job.layer.lower()
        if layer in {"l1", "minute", "event_l1", "realtime_l1"}:
            result_ref = self.repository.persist_l1(session, job, output)
        elif layer == "l2":
            result_ref = self.repository.persist_l2(session, job, output)
        elif layer in {"l3", "summary", "hourly_summary", "daily_summary"}:
            result_ref = self.repository.persist_l3(session, job, output)
        else:
            raise AnalysisServiceError(f"不支持的分析层级: {layer}")
        self.repository.record_usage(session, job, normalized, retry=job.attempts > 1)
        return self.queue.complete(job, result_ref=result_ref, session=session)


AnalysisWorker = AnalysisService

__all__ = [
    "AnalysisGateway",
    "AnalysisRepository",
    "AnalysisRequest",
    "AnalysisResponse",
    "AnalysisRunResult",
    "AnalysisService",
    "AnalysisServiceError",
    "AnalysisWorker",
    "EvidenceExpired",
    "InvalidModelOutput",
    "validate_model_output",
]
