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
  const session = await (await page.request.get(base + "/api/session")).json();
  const control = (data) =>
    page.request.post(base + "/api/_test/control", {
      headers: { "X-CSRF-Token": session.data.csrf_token },
      data,
    });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(
    base +
      "/minutes/" +
      state.minute +
      "?date=2026-10-18&subject_id=" +
      state.person,
  );
  await page
    .getByRole("button", { name: "用仍有效的原图重新分析", exact: true })
    .click();
  let dialog = page.getByRole("dialog", {
    name: "用仍有效的原图重新分析",
    exact: true,
  });
  await dialog
    .getByRole("button", { name: "检查范围与资料", exact: true })
    .click();
  await dialog.getByText(/仅部分现场图可用/).waitFor();
  assert(
    (await dialog
      .getByRole("checkbox", { name: /只分析仍有原图的部分/ })
      .isChecked()) === false,
    "F07 部分原图缺失默认不允许静默部分分析",
  );
  await dialog.getByRole("checkbox", { name: /只分析仍有原图的部分/ }).check();
  await dialog
    .getByRole("button", { name: "检查范围与资料", exact: true })
    .click();
  await dialog
    .getByRole("button", { name: "确认创建任务", exact: true })
    .waitFor();
  await control({ expire_preview: true });
  await dialog
    .getByRole("button", { name: "确认创建任务", exact: true })
    .click();
  await dialog.getByText(/请核对新预检后再次确认/).waitFor();
  assert(
    await dialog
      .getByRole("button", { name: "确认创建任务", exact: true })
      .isVisible(),
    "F07 实际失效 token 重新预检且保留第二次确认",
  );
  const accepted = [];
  const watch = (r) => {
    if (
      r.request().method() === "POST" &&
      new URL(r.url()).pathname === "/api/analysis-jobs" &&
      r.status() === 202
    )
      accepted.push(r.request().headers()["idempotency-key"]);
  };
  page.on("response", watch);
  await dialog
    .getByRole("button", { name: "确认创建任务", exact: true })
    .evaluate((el) => {
      el.click();
      el.click();
    });
  await dialog.getByText(/^(已完成|部分完成)$/).waitFor({ timeout: 30000 });
  assert(accepted.length === 1, "F07 双击只创建一个后端分析任务");
  page.off("response", watch);
  await dialog.getByRole("button", { name: "关闭对话框", exact: true }).click();
  const browser = page.context().browser();
  const otherContext = await browser.newContext({
    viewport: { width: 390, height: 844 },
    timezoneId: "America/Los_Angeles",
  });
  const other = await otherContext.newPage();
  try {
    await page.goto(base + "/events/" + state.event + "?date=2026-10-18");
    await page.getByRole("button", { name: "人工修订", exact: true }).click();
    const edit = page.getByRole("dialog", {
      name: "人工修订活动",
      exact: true,
    });
    await edit
      .getByLabel("修订原因", { exact: true })
      .fill("浏览器 A 的修订草稿，等待对比");
    assert(
      (await edit.getByLabel("主要活动", { exact: true }).count()) === 2,
      "F08 编辑保留中断，连续同类片段合并为两个有序区间",
    );
    await other.goto(base + "/login");
    await other.getByLabel("用户名", { exact: true }).fill("frontend-test");
    await other
      .getByLabel("密码", { exact: true })
      .fill("frontend-browser-test-2026");
    await other.getByRole("button", { name: "登录", exact: true }).click();
    await other
      .getByRole("heading", { name: "今日概览", exact: true })
      .waitFor();
    await other.goto(base + "/events/" + state.event + "?date=2026-10-18");
    await other.getByRole("button", { name: "人工修订", exact: true }).click();
    const otherEdit = other.getByRole("dialog", {
      name: "人工修订活动",
      exact: true,
    });
    await otherEdit
      .getByLabel("主要活动", { exact: true })
      .first()
      .selectOption("writing");
    await otherEdit
      .getByLabel("修订原因", { exact: true })
      .fill("浏览器 B 先保存的人工修订");
    const updated = other.waitForResponse(
      (r) =>
        r.request().method() === "PATCH" &&
        r.url().includes("/api/events/") &&
        r.status() === 200,
    );
    await otherEdit
      .getByRole("button", { name: "保存人工修订", exact: true })
      .click();
    await updated;
    await edit
      .getByRole("button", { name: "保存人工修订", exact: true })
      .click();
    await edit.getByText(/其他会话已更新此记录/).waitFor();
    assert(
      (
        await edit.getByLabel("修订原因", { exact: true }).inputValue()
      ).includes("浏览器 A"),
      "F08 两个独立浏览器会话竞争返回真实 412，草稿保留",
    );
    await edit
      .getByRole("button", { name: "读取服务器新版并对比", exact: true })
      .click();
    await edit.getByText(/服务器 v/).waitFor();
    assert(
      await edit
        .getByRole("button", { name: "保存人工修订", exact: true })
        .isDisabled(),
      "F08 未核对新版前禁止覆盖提交",
    );
    await page.screenshot({
      path: "output/playwright/conflict-390.png",
      fullPage: true,
    });
    await edit.getByRole("button", { name: "取消", exact: true }).click();
    await page.goto(base + "/reports/" + state.summary + "?date=2026-10-18");
    await page
      .getByRole("button", { name: "按现有记录重新汇总", exact: true })
      .click();
    const job = page.getByRole("dialog", { name: "任务进度", exact: true });
    await job.waitFor();
    await job.getByText("已完成", { exact: true }).waitFor({ timeout: 30000 });
    assert(
      (await page.locator(".observation").count()) > 0,
      "F10 汇总任务期间旧文字与统计继续保留",
    );
    await job.getByRole("button", { name: "关闭对话框", exact: true }).click();
    await page.getByLabel("记录版本", { exact: true }).selectOption("1");
    await page.getByText("历史版本 · 只读", { exact: false }).waitFor();
    assert(
      await page
        .getByRole("button", { name: "按现有记录重新汇总", exact: true })
        .isDisabled(),
      "F10 历史报告可读且不能直接覆盖",
    );
    await page.screenshot({
      path: "output/playwright/report-390.png",
      fullPage: true,
    });
    await other.goto(
      base + "/reports/" + state.summary + "?revision=1&date=2026-10-18",
    );
    await other
      .getByRole("heading", { name: "2026-10-18 日报", exact: true })
      .waitFor();
    assert(
      (await other.locator(".page-heading").innerText()).includes(
        "08:00—11:20",
      ),
      "F10 浏览器系统时区为洛杉矶，报告仍按北京时间日界与范围显示",
    );
    await page.goto(base + "/settings/analysis");
    await page.getByLabel("现场图保存（分钟）", { exact: true }).fill("30");
    await page
      .getByRole("button", { name: "预检保存影响", exact: true })
      .click();
    const impact = page.getByRole("dialog", {
      name: "确认设置影响",
      exact: true,
    });
    await impact.waitFor();
    const expireCount = await impact
      .locator(".definition-list > div")
      .filter({ hasText: "将立即到期图片" })
      .locator("dd")
      .innerText();
    assert(parseInt(expireCount) > 0, "F11 缩短 TTL 显示具体将到期图片数");
    await control({ expire_preview: true });
    await impact
      .getByRole("button", { name: "保存并使这些图片到期", exact: true })
      .click();
    await impact.getByText(/预检来源变化，请重新核对新影响/).waitFor();
    assert(await impact.isVisible(), "F11 过期的设置预检不能直接提交");
    await impact
      .getByRole("button", { name: "保存并使这些图片到期", exact: true })
      .click();
    await impact.waitFor({ state: "hidden" });
    if (
      await page.getByRole("dialog", { name: "任务进度", exact: true }).count()
    ) {
      const apply = page.getByRole("dialog", { name: "任务进度", exact: true });
      await apply.getByText("已完成", { exact: true }).waitFor();
      await apply
        .getByRole("button", { name: "关闭对话框", exact: true })
        .click();
    }
    assert(
      (
        await page.request.get(base + "/api/evidence/" + state.older_evidence)
      ).status() === 410,
      "F11 设置生效后受影响原图通过真实 API 返回 410",
    );
    await page.goto(base + "/usage?date=2026-10-18");
    await page
      .getByRole("heading", { name: "调用明细", exact: true })
      .waitFor();
    assert(
      (await page.locator("main").innerText()).includes("未知") &&
        (await page.locator("main").innerText()).includes("未配置"),
      "F12 未返回用量与未配置费用保留未知，不伪造零值",
    );
    await page.goto(base + "/status?date=2026-10-18");
    await page
      .getByRole("heading", { name: "媒体存储", exact: true })
      .waitFor();
    assert(
      (await page
        .getByRole("heading", { name: "采集状态", exact: true })
        .count()) === 1 &&
        (await page
          .getByRole("heading", { name: "AI 队列", exact: true })
          .count()) === 1,
      "F12 采集、队列、存储、模型与后台进程独立呈现",
    );
  } finally {
    await otherContext.close();
  }
  return { checks };
};
