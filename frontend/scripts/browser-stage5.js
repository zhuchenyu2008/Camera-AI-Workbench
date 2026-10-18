async (page) => {
  const checks = [],
    base = "http://127.0.0.1:5173",
    assert = (ok, message) => {
      if (!ok) throw new Error(message);
      checks.push(message);
    };
  const state = await (
    await page.request.get(base + "/api/_test/state")
  ).json();
  const scoped = "?date=2026-10-18&subject_id=" + state.person;
  const routes = [
    "/",
    "/timeline",
    "/minutes",
    "/reports",
    "/cameras",
    "/cameras/new",
    "/cameras/" + state.camera,
    "/entities",
    "/entities/" + state.person,
    "/settings/ai",
    "/settings/analysis",
    "/status",
    "/usage",
    "/account",
    "/more",
    "/events/" + state.event,
    "/minutes/" + state.minute,
    "/reports/" + state.summary,
  ];
  for (const width of [360, 390, 768, 1440]) {
    await page.setViewportSize({ width, height: 960 });
    for (const route of width === 360
      ? routes
      : ["/", "/timeline", "/reports/" + state.summary]) {
      await page.goto(base + route + scoped);
      await page.locator("main h1").first().waitFor();
      await page.waitForFunction(
        () => !document.querySelector("main .skeleton-wrap"),
      );
      assert(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
        `F13 ${width}px ${route} 无页面横向溢出`,
      );
      if (
        route === "/" ||
        route === "/timeline" ||
        route === "/reports/" + state.summary ||
        route === "/minutes/" + state.minute
      )
        await page.screenshot({
          path: `output/playwright/${route === "/" ? "overview" : route === "/timeline" ? "timeline" : route.startsWith("/reports/") ? "report" : "minute"}-${width}.png`,
          fullPage: true,
        });
    }
  }
  assert(
    (await page
      .locator(".sidebar")
      .evaluate((el) => Math.round(el.getBoundingClientRect().width))) === 208,
    "视觉规范：宽屏侧栏 208px",
  );
  await page.setViewportSize({ width: 768, height: 960 });
  assert(
    await page
      .locator(".sidebar .nav-item")
      .evaluateAll((links) => links.every((a) => a.getAttribute("aria-label"))),
    "F13 平板收起导航仍保留每个入口的可访问名称",
  );
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto(base + "/" + scoped);
  await page.locator(".route-content").waitFor();
  assert(
    (await page
      .locator(".route-content")
      .evaluate((el) => getComputedStyle(el).animationName)) === "none",
    "F13 减少动效偏好禁用位移动画",
  );
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await page.setViewportSize({ width: 720, height: 540 });
  await page.goto(base + "/settings/analysis");
  await page.getByLabel("现场图保存（分钟）", { exact: true }).waitFor();
  assert(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
    "F13 1440px 在 200% 缩放等效视口中表单无溢出",
  );
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(base + "/more");
  assert(
    (await page.locator(".more-list a").count()) === 8,
    "F13 手机更多包含分钟记录及全部管理入口",
  );
  await page.goto(base + "/usage?date=2026-10-18");
  await page.getByRole("heading", { name: "调用明细", exact: true }).waitFor();
  let calls = 0;
  const block = async (route) => {
    calls++;
    await route.fulfill({
      status: 429,
      contentType: "application/json",
      headers: { "Retry-After": "15" },
      body: JSON.stringify({
        error: {
          code: "RATE_LIMITED",
          message: "请求较多",
          field_errors: [],
          retryable: true,
          details: {},
        },
        meta: { request_id: "req_browser_rate_limit", server_time: state.now },
      }),
    });
  };
  await page.route("**/api/usage?**", block);
  await page.getByLabel("分组", { exact: true }).selectOption("purpose");
  await page.getByText(/请求较多，请于/).waitFor();
  assert(calls === 1, "429 显示北京时间重试时刻且不自动重复请求");
  await page.unroute("**/api/usage?**", block);
  await page.getByRole("button", { name: "重新读取", exact: true }).click();
  await page.getByRole("heading", { name: "调用明细", exact: true }).waitFor();
  await page.goto(base + "/status?date=2026-10-18");
  await page.getByRole("heading", { name: "AI 队列", exact: true }).waitFor();
  let requests = 0;
  const watch = (req) => {
    if (new URL(req.url()).pathname === "/api/status") requests++;
  };
  page.on("request", watch);
  await page.waitForTimeout(6100);
  assert(requests <= 2, "F14 可见状态页没有同 key 重复轮询");
  await page.evaluate(() => {
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      value: "hidden",
    });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  const before = requests;
  await page.waitForTimeout(5500);
  assert(requests === before, "F14 隐藏状态页后停止轮询");
  await page.evaluate(() => {
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      value: "visible",
    });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  page.off("request", watch);
  const fallbackContext = await page
    .context()
    .browser()
    .newContext({
      storageState: await page.context().storageState(),
      viewport: { width: 390, height: 844 },
      hasTouch: true,
    });
  try {
    await fallbackContext.addInitScript(() => {
      const native = HTMLMediaElement.prototype.canPlayType;
      HTMLMediaElement.prototype.canPlayType = function (type) {
        return type.includes("mpegurl") ? "" : native.call(this, type);
      };
      for (const key of [
        "MediaSource",
        "ManagedMediaSource",
        "WebKitMediaSource",
      ])
        Object.defineProperty(window, key, {
          configurable: true,
          value: undefined,
        });
    });
    const fallback = await fallbackContext.newPage();
    await fallback.goto(base + "/" + scoped);
    await fallback.locator("img.preview-image").waitFor({ timeout: 25000 });
    assert(
      (await fallback.locator("main").innerText()).includes("已退回每秒快照"),
      "F04 无原生 HLS 与 MSE 的上下文自动降级快照",
    );
  } finally {
    await fallbackContext.close();
  }
  const hlsContext = await page
    .context()
    .browser()
    .newContext({
      storageState: await page.context().storageState(),
      viewport: { width: 768, height: 960 },
    });
  try {
    await hlsContext.addInitScript(() => {
      const native = HTMLMediaElement.prototype.canPlayType;
      HTMLMediaElement.prototype.canPlayType = function (type) {
        return type.includes("mpegurl") ? "" : native.call(this, type);
      };
    });
    const hlsPage = await hlsContext.newPage();
    let loaded = false;
    hlsPage.on("request", (req) => {
      if (/\/hls-[^/]+\.js$/.test(new URL(req.url()).pathname)) loaded = true;
    });
    await hlsPage.goto(base + "/" + scoped);
    await hlsPage.waitForFunction(
      () => {
        const video = document.querySelector("video");
        return video && video.readyState >= 2;
      },
      {},
      { timeout: 25000 },
    );
    assert(loaded, "F04 无原生 HLS 时按需加载 hls.js 并实际播放");
  } finally {
    await hlsContext.close();
  }
  assert(
    await page.evaluate(() => localStorage.length === 0),
    "F01/F14 所有流程结束后无秘密或图片持久缓存",
  );
  return { checks };
};
