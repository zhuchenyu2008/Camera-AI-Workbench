import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  render,
  renderHook,
  screen,
  act,
  cleanup,
} from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { api, ApiError, apiUrl, safeReturnTo, setCsrf } from "../api/client";
import {
  canonical,
  pollingCount,
  queryClient,
  stopPolling,
  useApi,
} from "../api/query";
import {
  dayRange,
  localDate,
  beijingInput,
  duration,
  syncServerTime,
} from "../shared/time";
import { containedRect, normalizePoint, polygonError } from "../shared/roi";
import {
  sourceHref,
  Field,
  ErrorNotice,
  ActivityChart,
  CoverageTrack,
} from "../shared/components";
import { analysisLabels } from "../shared/labels";
import {
  tokenText,
  costText,
  formatCnyCost,
} from "../features/operations/Operations";
import { useEphemeralImage } from "../shared/media/EphemeralImage";
import { useAction } from "../shared/hooks";
import { mergedWeek, defaultOverrideEnd } from "../shared/schedule";
import fixture from "./fixtures/contracts.json";

beforeEach(() => {
  vi.useRealTimers();
  queryClient.clear();
  setCsrf("test-csrf");
  Object.defineProperty(document, "visibilityState", {
    configurable: true,
    value: "visible",
  });
  URL.createObjectURL = vi.fn(() => "blob:test");
  URL.revokeObjectURL = vi.fn();
});
afterEach(() => {
  cleanup();
  stopPolling();
  queryClient.clear();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});
describe("北京时间和服务端时长", () => {
  it("午夜按 UTC+8 分日", () => {
    expect(localDate("2026-10-17T16:00:00Z")).toBe("2026-10-18");
    expect(localDate("2026-10-17T15:59:59Z")).toBe("2026-10-17");
  });
  it("日范围精确映射到 UTC，今日截断未来计划", () => {
    expect(dayRange("2026-10-17", Date.parse("2026-10-18T04:00:00Z"))).toEqual({
      from: "2026-10-16T16:00:00.000Z",
      to: "2026-10-17T16:00:00.000Z",
    });
    expect(dayRange("2026-10-18", Date.parse("2026-10-18T03:20:00Z")).to).toBe(
      "2026-10-18T03:20:00.000Z",
    );
  });
  it("datetime-local 始终按北京时间提交", () =>
    expect(beijingInput("2026-10-18T00:05")).toBe("2026-10-17T16:05:00.000Z"));
  it("显示后端有效时长，不四舍五入中断", () => {
    expect(duration(1180000)).toBe("约 19 分钟 40 秒");
    expect(duration(undefined)).toBe("未知");
  });
});
describe("坐标和可追溯引用", () => {
  it("选择框标签不会混入全部选项，字段错误关联并聚焦输入", () => {
    render(
      <form>
        <Field label="主要活动">
          <select>
            <option>阅读</option>
            <option>书写</option>
          </select>
        </Field>
        <Field label="端口">
          <input name="source.port" />
        </Field>
        <ErrorNotice
          error={
            new ApiError(422, "VALIDATION_ERROR", "字段错误", "req_test", [
              {
                path: "source.port",
                code: "range",
                message: "端口不在允许范围",
              },
            ])
          }
        />
      </form>,
    );
    expect(
      screen.getByLabelText("主要活动", { exact: true }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("端口")).toHaveFocus();
    expect(screen.getByLabelText("端口")).toHaveAttribute(
      "aria-invalid",
      "true",
    );
  });
  it("contain 留白不能生成 ROI 顶点", () => {
    const rect = containedRect(
      { width: 400, height: 400 },
      { width: 1600, height: 900 },
    );
    expect(rect).toEqual({ x: 0, y: 87.5, width: 400, height: 225 });
    expect(normalizePoint(200, 20, rect)).toBeNull();
    expect(normalizePoint(200, 200, rect)).toEqual([0.5, 0.5]);
  });
  it("拒绝越界、自交和零面积区域", () => {
    expect(
      polygonError([
        [0, 0],
        [1, 0],
        [1, 1],
        [0, 1],
      ]),
    ).toBeNull();
    expect(
      polygonError([
        [0, 0],
        [1, 1],
        [1, 0],
        [0, 1],
      ]),
    ).not.toBeNull();
    expect(
      polygonError([
        [0, 0],
        [0, 0],
        [0, 0],
      ]),
    ).not.toBeNull();
    expect(
      polygonError([
        [0, 0],
        [2, 0],
        [1, 1],
      ]),
    ).not.toBeNull();
  });
  it("只根据已知资源 ID 创建内部引用", () => {
    expect(sourceHref({ kind: "event", id: "ev/1", revision: 2 })).toBe(
      "/events/ev%2F1?revision=2",
    );
    expect(
      sourceHref({
        resource_kind: "summary",
        resource_id: "sum_x",
        revision: 1,
      }),
    ).toBe("/reports/sum_x?revision=1");
    expect(sourceHref({ url: "javascript:alert(1)", kind: "url" })).toBeNull();
  });
  it("登录返回地址不能跳转外站", () => {
    expect(safeReturnTo("//evil.test")).toBe("/");
    expect(safeReturnTo("/\\evil.test")).toBe("/");
    expect(safeReturnTo("https://evil.test")).toBe("/");
    expect(safeReturnTo("/timeline?date=2026-10-18")).toBe(
      "/timeline?date=2026-10-18",
    );
  });
});
describe("契约、错误与幂等", () => {
  it("机位集合排序去重，不产生另一份查询", () => {
    expect(
      canonical({ query: { camera_ids: ["b", "a", "b"], subject_id: null } }),
    ).toEqual({ query: { camera_ids: ["a", "b"] } });
    expect(
      apiUrl("getTimeline", {
        query: { from: "a", to: "b", camera_ids: ["b", "a", "a"] },
      }),
    ).toBe("/api/timeline?camera_ids=a&camera_ids=b&from=a&to=b");
  });
  it("写请求携带 Cookie、CSRF、固定幂等 key 和原 revision", async () => {
    const fetcher = vi.fn(
      async (_url: RequestInfo | URL, _init?: RequestInit) =>
        new Response(
          JSON.stringify({
            data: {},
            meta: { server_time: "2026-10-18T03:20:00Z" },
          }),
          { headers: { "Content-Type": "application/json" } },
        ),
    );
    vi.stubGlobal("fetch", fetcher);
    await api("updateEvent", {
      path: { event_id: "event" },
      body: { reason: "修订", base_source_revision: 1, intervals: [] },
      revision: 3,
      intent: "same-intent",
    });
    const init = fetcher.mock.calls[0][1] as RequestInit;
    expect(init.credentials).toBe("same-origin");
    expect(new Headers(init.headers).get("If-Match")).toBe('"rev-3"');
    expect(new Headers(init.headers).get("Idempotency-Key")).toBe(
      "same-intent",
    );
    expect(new Headers(init.headers).get("X-CSRF-Token")).toBe("test-csrf");
  });
  it("不自动重放写入，410 保留请求 ID", async () => {
    const fetcher = vi.fn(
      async () =>
        new Response(JSON.stringify(fixture.expired_evidence), { status: 410 }),
    );
    vi.stubGlobal("fetch", fetcher);
    await expect(
      api("getEvidence", { path: { evidence_id: "ev_fixture" } }),
    ).rejects.toMatchObject({
      status: 410,
      code: "EVIDENCE_EXPIRED",
      requestId: "req_fixture",
    });
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
  it("未知 token 与未配置费用不等于零", () => {
    expect(tokenText({ value: null, known_count: 0, unknown_count: 4 })).toBe(
      "未知",
    );
    expect(
      tokenText({ value: 200, known_count: 1, unknown_count: 2 }),
    ).toContain("另 2 次未知");
    expect(costText({ cost: null } as never)).toBe("未配置");

    // 人民币费用格式化与小额精度
    expect(formatCnyCost(null)).toBe("未折算");
    expect(formatCnyCost("0")).toBe("¥0");
    expect(formatCnyCost("0.0000001")).toBe("小于 ¥0.000001");
    expect(formatCnyCost("24.5")).toBe("¥24.50");
    expect(formatCnyCost("0.014")).toBe("¥0.014");

    // 费用核算完整度与说明
    expect(
      costText({
        requests: 2,
        cost_summary: {
          completeness: "full",
          estimated_cny_subtotal: "24.5",
          converted_requests: 2,
          unconverted_requests: 0,
          unpriced_requests: 0,
          reasons: [],
        },
      } as never),
    ).toBe("¥24.50");

    expect(
      costText({
        requests: 3,
        cost_summary: {
          completeness: "partial",
          estimated_cny_subtotal: "10.5",
          converted_requests: 1,
          unconverted_requests: 1,
          unpriced_requests: 1,
          reasons: ["missing_exchange_rate"],
        },
      } as never),
    ).toContain("已知 ¥10.50（2 笔缺失）");

    expect(
      costText({
        requests: 1,
        cost_summary: {
          completeness: "none",
          estimated_cny_subtotal: null,
          converted_requests: 0,
          unconverted_requests: 1,
          unpriced_requests: 0,
          reasons: ["missing_exchange_rate"],
        },
      } as never),
    ).toBe("未折算");
  });
  it("共享 fixture 覆盖值守恒", () => {
    const c = fixture.overview.data.coverage;
    expect(c.classified_ms + c.unknown_ms + c.unanalysed_ms + c.gap_ms).toBe(
      c.planned_ms,
    );
  });
});
describe("生命周期", () => {
  it("同一写入意图响应丢失后复用幂等键，双击不重复提交", async () => {
    const hook = renderHook(() => useAction<unknown>()),
      keys: string[] = [];
    await act(async () => {
      await hook.result.current.run({ body: 1 }, async (key) => {
        keys.push(key);
        throw new TypeError("network");
      });
    });
    await act(async () => {
      await hook.result.current.run({ body: 1 }, async (key) => {
        keys.push(key);
        return true;
      });
    });
    expect(keys[0]).toBe(keys[1]);
    let finish: (v: boolean) => void = () => {};
    const pending = new Promise<boolean>((r) => {
      finish = r;
    });
    let first: Promise<unknown>;
    act(() => {
      first = hook.result.current.run({ body: 2 }, async (key) => {
        keys.push(key);
        return pending;
      });
      void hook.result.current.run({ body: 2 }, async (key) => {
        keys.push(key);
        return true;
      });
    });
    expect(keys.length).toBe(3);
    expect(keys[2]).not.toBe(keys[1]);
    await act(async () => {
      finish(true);
      await first;
    });
  });
  it("同一 key 只有一个轮询，隐藏与卸载清理", async () => {
    vi.useFakeTimers();
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(JSON.stringify(fixture.session), {
            headers: { "Content-Type": "application/json" },
          }),
      ),
    );
    function Probe() {
      useApi("getSession", {}, { poll: 5000 });
      return null;
    }
    const tree = render(
      <QueryClientProvider client={queryClient}>
        <Probe />
        <Probe />
      </QueryClientProvider>,
    );
    expect(pollingCount()).toBe(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      value: "hidden",
    });
    act(() => document.dispatchEvent(new Event("visibilitychange")));
    const count = vi.mocked(fetch).mock.calls.length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(15000);
    });
    expect(vi.mocked(fetch).mock.calls.length).toBe(count);
    tree.unmount();
    expect(pollingCount()).toBe(0);
  });
  it("原图到期立即移除并释放对象 URL", async () => {
    vi.useFakeTimers();
    syncServerTime("2026-10-18T03:20:00Z");
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(new Blob(["jpeg"]), {
            headers: { "Content-Type": "image/jpeg" },
          }),
      ),
    );
    function Probe() {
      const image = useEphemeralImage("/api/evidence/a", {
        expiresAt: "2026-10-18T03:20:01Z",
      });
      return image.url ? (
        <img src={image.url} alt="evidence" />
      ) : (
        <p>原图不可用</p>
      );
    }
    render(<Probe />);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10);
    });
    expect(screen.queryByAltText("evidence")).toBeInTheDocument();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1100);
    });
    expect(screen.queryByAltText("evidence")).not.toBeInTheDocument();
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:test");
  });
  it("410 图片错误不无限重试", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(JSON.stringify(fixture.expired_evidence), {
            status: 410,
          }),
      ),
    );
    function Probe() {
      const image = useEphemeralImage("/api/evidence/a");
      return (
        <span>
          {image.error instanceof ApiError ? image.error.code : "loading"}
        </span>
      );
    }
    render(<Probe />);
    expect(await screen.findByText("EVIDENCE_EXPIRED")).toBeInTheDocument();
    expect(fetch).toHaveBeenCalledTimes(1);
  });
});
describe("北京时间周计划", () => {
  it("合并重叠计划并正确拆分周日跨周一", () => {
    expect(
      mergedWeek([
        { weekdays: [7], start: "23:00", end: "01:00", end_day_offset: 1 },
        { weekdays: [1], start: "00:30", end: "02:00" },
      ]),
    ).toEqual([
      [0, 120],
      [10020, 10080],
    ]);
  });
  it("临时控制默认结束于下一计划边界，无计划时一小时", () => {
    const now = Date.parse("2026-10-18T11:20:00+08:00");
    expect(
      defaultOverrideEnd(
        [{ weekdays: [7], start: "08:00", end: "12:00" }],
        now,
      ),
    ).toBe(Date.parse("2026-10-18T12:00:00+08:00"));
    expect(defaultOverrideEnd([], now)).toBe(now + 3600000);
  });
});

describe("自由活动名称", () => {
  it("新活动直接显示，历史分类仍显示中文，不依赖人物档案", () => {
    render(
      <ActivityChart
        items={[
          { activity: "睡觉", duration_ms: 3600000 },
          { activity: "洗漱", duration_ms: 600000 },
          { activity: "reading", duration_ms: 60000 },
        ]}
      />,
    );
    expect(screen.getByText("睡觉")).toBeInTheDocument();
    expect(screen.getByText("洗漱")).toBeInTheDocument();
    expect(screen.getByText("阅读")).toBeInTheDocument();
    expect(screen.queryByText("人物未确认")).not.toBeInTheDocument();
    const bars = document.querySelectorAll<HTMLElement>(".activity-fill");
    expect(bars[0].style.getPropertyValue("--activity")).toBeTruthy();
    expect(bars[0].style.width).toBe("100%");
  });
});

describe("分析状态文案", () => {
  it("按处理阶段区分分钟状态", () => {
    expect(analysisLabels).toMatchObject({
      pending: "分析排队中",
      running: "分析进行中",
      partial: "部分范围已分析",
      failed: "分析失败",
      expired: "未完成分析，现场图已过期",
    });
  });

  it("覆盖统计说明聚合状态及未完成原因", () => {
    render(
      <CoverageTrack
        coverage={{
          planned_ms: 600000,
          captured_ms: 600000,
          classified_ms: 120000,
          unknown_ms: 60000,
          unanalysed_ms: 360000,
          unanalysed_pending_ms: 60000,
          unanalysed_failed_ms: 120000,
          unanalysed_expired_ms: 180000,
          gap_ms: 60000,
        }}
      />,
    );
    expect(screen.getByText("暂无可用分析结果")).toBeInTheDocument();
    expect(screen.getByText("其中分析处理中 1 分钟")).toBeInTheDocument();
    expect(screen.getByText("其中分析失败 2 分钟")).toBeInTheDocument();
    expect(
      screen.getByText("其中现场图已过期，未形成分析结果 3 分钟"),
    ).toBeInTheDocument();
    expect(screen.queryByText("待分析")).not.toBeInTheDocument();
  });
});
