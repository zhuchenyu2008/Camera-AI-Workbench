# 后端详细设计文档

- **关联产品**：摄像头 AI 总结工作台
- **版本**：1.0
- **状态**：实现基线
- **部署目标**：Linux + Docker Compose，单用户，多路 RTSP
- **接口基线**：REST `/api/v1` + SSE；AI 端使用可配置 OpenAI-compatible Chat Completions 子集
- **原则**：采集不依赖 AI；任务持久化；输出可追溯；推断不伪装成观察

---

## 1. 后端职责与边界

后端负责摄像头接入、每秒采集与持久化、轻量帧差/去重、事件缓存、AI 任务编排、三层推理、证据与时间轴存储、保留期清理、使用量统计、认证和向前端提供稳定 API。

后端不负责：OCR、人脸身份识别、人物真实姓名推断、用户端图像编辑、在 AI 不可用时猜测活动。首版不要求 GPU；采集容器负责 RTSP/解码，AI 由用户配置的 OpenAI-compatible 服务处理。

### 1.1 实现基线（建议）

- API、调度器、Worker：Python + FastAPI、Pydantic、SQLAlchemy、Alembic。
- RTSP：独立采集进程/容器，FFmpeg 子进程或等价稳定解码适配器；每路摄像头隔离进程生命周期。
- 元数据：SQLite WAL；首版单实例，保持事务短小并限制 Worker 并发，依靠 SQLite 文件锁序列化写入。必须对多摄像头、采集写入和 Job 领取同时压测；若锁等待超出目标，迁移到 PostgreSQL，而不是丢任务。数据库访问集中在 repository 层，为后续 PostgreSQL 留接口。
- 图像：本机数据卷中的 JPEG 文件；不将图片 BLOB 写入 SQLite。
- 任务队列：SQLite 持久化任务表 + lease/心跳；首版不依赖 Redis。队列压力测试不足时优先增加数据库容量或再评估 PostgreSQL/外部队列，不得悄悄丢任务。
- AI HTTP：异步 HTTP 客户端，连接池、明确超时、有限重试、响应体大小限制。
- 前端通过 API 和 SSE 通信；模型供应商凭据只在后端。

以上是新实现的推荐基线，不依赖或假设仓库已有技术栈。实现前如项目已有约定，应在技术决策记录中解释是否沿用，而不是静默改变本设计的行为要求。

---

## 2. 运行组件与部署拓扑

### 2.1 Compose 服务

```text
web          静态前端文件，可与 api 合并部署
api          REST API、单用户认证、配置和查询
capture      多路 RTSP 管理、每秒取帧、本地轻量检测、事件缓存
worker       持久化 AI Job 消费者；可横向扩展但受数据库并发限制
scheduler    分钟窗口封账、5 分钟 L2、小时/日报任务、留存清理
```

首版允许 `api + scheduler + worker` 在同一 Python 镜像的不同命令模式下运行，以降低镜像和维护成本。采集仍建议与 API 分开，以免网络摄像头断流或解码崩溃影响 UI/API。所有服务共享同一持久化数据库卷和图像卷；容器间只通过数据库、内部 HTTP 或本地队列约定通信，不允许依赖容器临时文件作为唯一数据。

### 2.2 进程职责

- **Capture Manager**：加载启用的 Camera 配置；为每路 RTSP 启动/停止采集器；保存健康状态、最近帧、断线区间和重连计数。
- **Frame Writer**：为输入帧生成 UUID；计算缩略图上的轻量相似度/差异信号；先写 `.part` 临时文件，再 fsync/原子重命名，之后写 Frame 元数据。写库失败则隔离未引用文件，后台可回收。
- **Change Detector**：平滑多帧差异，合并相邻触发；产生 ChangeEvent、前置环形缓存引用和后置收集计划。
- **Window Scheduler**：为每分钟建立 L1 任务或 `LOCAL_STABLE_CONTINUATION` 记录；5 分钟封账后创建 L2；整点/日界创建 L3。
- **AI Worker**：获取带 lease 的任务、准备图片、调用 AI Gateway、校验结构化响应、保存结果和 usage。
- **Retention Worker**：按策略扫描到期帧，尊重收藏和任务引用，分批清理，记录每次清理结果。

---

## 3. 核心数据流

### 3.1 采集到分钟观察

```text
每路 RTSP
  → 解码器输出约 1 fps 帧
  → captured_at_utc / sequence 校验
  → 小图差异计算 + 原图 JPEG 原子落盘
  → 记录 Frame 和 local signal
  → 更新 30 秒环形缓存
  → 稳定抽样/变化帧选择
  → 分钟到期：形成 L1 请求或本地稳定延续记录
```

采集使用计划 tick 作为缺帧观测依据，但不伪造图像：每个成功 tick 必须保留独立的 Frame 时间戳/序号记录和可取回图像内容。若不同 tick 的 JPEG 字节完全相同，可按 SHA-256 内容寻址共享物理 blob 以节省磁盘，但不能省略该秒的 Frame 记录。若解码器没有产生新帧、持续返回 stale buffer 或断流，则记录缺帧/冻结信号，不能将旧帧复制成新的视觉证据。

### 3.2 L1 到 L2

- L1 的输入快照在任务创建时固定为 `input_frame_ids`，重试使用相同输入版本。
- 每一分钟都产生一个 `MinuteObservation`；模型调用可以被跳过，但 `source=local_stable` 的记录不能冒充模型观察。
- 每 5 分钟封账后，Scheduler 创建一个 L2 Job，初始输入包括最近五分钟 L1 记录和相邻窗口摘要。
- L2 初始只用结构化数据与证据索引；识别不确定区间后才用服务端工具取帧。
- L2 输出经 Schema、时间、引用和区间规则校验后才进入 ActivitySegment。
- 后续窗口证据需要修改相邻段时，生成新版本并写 Change Log；已锁定段不能被自动覆盖。

### 3.3 小时与日报

整点/日界任务只读取第二层 ActivitySegment、活动目录和用户更正；对未完成的 L2 批次采用产品设置规定的等待策略（默认等到 L2 完成或超时后生成标注为不完整的摘要）。摘要通过 `source_segment_ids` 建立可追溯引用。

---

## 4. 时间、窗口和幂等约定

- DB 内统一存 UTC epoch milliseconds（整数）；API 输入输出 RFC 3339 UTC 时间戳。UI 使用用户配置的时区渲染。
- 所有区间统一左闭右开 `[start, end)`，避免邻接窗口重复计时。
- 摄像头采集时间、服务器接收时间分别保存：`captured_at` 与 `ingested_at`。摄像头/解码器时钟不可信时，以服务器 monotonic/UTC 校准时间为主，保留原始时间戳供诊断。
- 1 分钟窗口按整分钟对齐；5 分钟窗口默认按本地时区的自然 5 分钟区间，落库时转换 UTC。DST/时区偏移变化由 IANA 时区库处理，不用固定 UTC+8 算法。
- Job 幂等键：`layer + camera_id + window_start + window_end + input_version + prompt_version`。同一幂等键已有成功任务时不重复写入结果。
- 一分钟任务、L2 窗口和 L3 摘要分别有版本。新输入/提示词需要重算时显式创建新版本，不复用旧结果 ID。
- 活动段时间不得超出其 L2 批次的允许上下文；如果引用跨窗口段，分段数据归属必须明确。

---

## 5. SQLite 数据模型

SQLite 开启 WAL、`foreign_keys=ON`、合理 `busy_timeout`；事务保持短小。schema 由 Alembic 管理。所有用户内容按 `created_at`/`updated_at` 审计；模型返回的自由文本单独保存，不能拼进 SQL。

### 5.1 核心表

#### `camera`

| 字段 | 类型/规则 | 说明 |
|---|---|---|
| id | UUID PK | 摄像头 ID |
| name | TEXT NOT NULL | UI 名称 |
| rtsp_url_encrypted | BLOB/TEXT | 加密后的凭据与 URL；API 返回脱敏值 |
| enabled | BOOL | 是否采集 |
| sampling_interval_ms | INT DEFAULT 1000 | 首版默认 1000 |
| frame_width/frame_height | INT NULL | 采集/存储尺寸策略 |
| status | TEXT | starting/online/offline/reconnecting/error |
| last_frame_at_ms | INT NULL | 最近成功帧 |
| retention_policy_id | FK | 摄像头级策略覆盖 |
| created_at_ms/updated_at_ms | INT | UTC |

#### `frame`

`id UUID PK, camera_id FK, captured_at_ms, ingested_at_ms, sequence, relative_path, width, height, byte_size, sha256, perceptual_hash, diff_score, duplicate_of_id NULL, event_id NULL, retained_until_ms, favorite BOOL, state`。

唯一索引 `(camera_id, sequence)`；检索索引 `(camera_id, captured_at_ms)`、`(retained_until_ms, favorite)`。相对路径在服务端拼接数据卷根目录，禁止来自 API/模型的任意路径。`frame_blob` 以摄像头范围内 SHA-256 为主键，Frame 表引用 blob；到期清理单条 Frame 后，只有没有其他有效 Frame/任务 pin 引用时才能删除物理 blob。

#### `camera_gap`

`id, camera_id, start_at_ms, end_at_ms NULL, reason, retry_count, recovered_at_ms NULL`。断流未恢复时 `end_at` 为空。

#### `change_event`

`id, camera_id, start_at_ms, end_at_ms NULL, trigger_score, trigger_reason, mode(normal/high_precision), pre_roll_start_ms, post_roll_end_ms, state, config_snapshot_json`。

#### `minute_observation`

`id, camera_id, start_at_ms, end_at_ms, source(ai/local_stable/failed), state, observation_json, candidates_json, evidence_frame_ids_json, gaps_json, coverage_ratio, confidence, ai_job_id NULL, version, created_at_ms`。

#### `activity_segment`

`id, camera_id, l2_window_id, start_at_ms, end_at_ms, canonical_activity_id NULL, display_description, category, confidence, evidence_coverage, state(final/provisional/unknown/failed), locked, version, supersedes_id NULL, source_observation_ids_json, created_at_ms, updated_at_ms`。

#### `activity_interval`

一个 segment 可以由多个证据等级不同的子区间组成：
`id, segment_id, start_at_ms, end_at_ms, evidence_kind(direct/inferred/unknown), confidence, reason, anchor_frame_ids_json`。

#### `activity_catalog` / `activity_alias`

标准活动 ID、名称、大类、用户启用状态；别名单独存表并唯一约束归一化后别名（同一活动内）。跨活动别名冲突要求用户解决，禁止静默任选。

#### `summary`

`id, camera_id, period_kind(hour/day), period_start_ms, period_end_ms, timezone, body, statistics_json, source_segment_ids_json, state, version, stale, ai_job_id, created_at_ms`。唯一索引 `(camera_id, period_kind, period_start_ms, version)`。

#### `ai_provider` / `ai_binding`

Provider 存 `id,label,base_url,api_key_ciphertext,capability_json,timeout_ms,enabled`。Binding 存 `layer,provider_id,model_name,fallback_provider_id,temperature,max_output_tokens,concurrency`。禁止将密钥返回 UI。

#### `ai_job`

`id, idempotency_key UNIQUE, layer, camera_id NULL, period_start_ms, period_end_ms, state, priority, input_version, prompt_version, schema_version, input_frame_ids_json, attempts, max_attempts, available_at_ms, lease_owner, lease_expires_at_ms, heartbeat_at_ms, started_at_ms, finished_at_ms, result_ref, error_code, error_safe_message`。

#### `ai_usage` / `tool_call`

Usage 逐请求记录 provider/model、frame_count、image_bytes、prompt/completion/total tokens（允许 NULL）、latency、retry、tool_count、cost_estimate（允许 NULL）、currency/source。ToolCall 记录工具名、脱敏参数、帧数、结果摘要、耗时和是否影响最终结论；不得存储模型对任意路径的请求。

#### `person_cluster` / `person_appearance`

匿名 ID、摄像头范围、首次/最近出现、置信度、代表帧引用、启用状态。首版不得存真实姓名或身份标签；聚类特征可只保留短期或以受控方式保存，用户关闭/删除时清除派生特征引用。

#### `user_edit` / `audit_log` / `outbox_event` / `app_setting`

分别存用户修改前后值、关键管理操作、安全审计、待推送 SSE 的事件以及可配置策略。API Key、RTSP 密码、图像内容不得写入通用审计日志。

### 5.2 文件目录

```text
/data/
  db/app.sqlite3
  frames/{camera_id}/{sha256-prefix}/{sha256}.jpg
  thumbs/{camera_id}/{YYYY}/{MM}/{DD}/{frame_id}.jpg
  backups/{date}/...
```

目录以 UUID 分桶，避免用户可控名称进入路径。照片 ID 不可猜测即不等同访问控制；所有内容请求仍需认证并校验 frame 归属。过期清理先将记录标记为 `deleting`，删除文件成功后删除/归档元数据；失败留可重试状态。

---

## 6. 采集与轻量变化检测

### 6.1 采集器接口

定义 `CameraSource` 抽象：`start(config)`, `read_frame()`, `health()`, `stop()`。RTSP 适配器负责重连、解码和输出帧；Frame Writer 负责时间戳、缩放、JPEG 编码和持久化。任何采集异常都转成结构化健康事件，不能无界重启。

建议控制参数：连接超时、读取超时、重连指数退避上限、JPEG quality、最大分辨率、摄像头并发数。禁止将用户名/密码放进异常文字或 subprocess 日志。

### 6.2 低资源信号

默认只在缩小到可配置尺寸（例如 320×180）的灰度图上计算：

- perceptual hash / mean absolute difference；
- 连续多帧平滑后的 change score；
- freeze/duplicate signal；
- 采集断流与时间戳跳变。

不在本地做语义类别、人脸或 OCR。算法必须是可替换模块，返回 `LocalSignal`：`score, stable, duplicate, frozen, frame_gap, trigger_reason, detector_version`。

### 6.3 事件状态机

```text
NORMAL
  └─ score 持续超过阈值 → EVENT_PREBUFFER_FROZEN
       → HIGH_PRECISION_CAPTURE
       → POST_ROLL
       → NORMAL
```

- 单帧异常不触发：默认连续 2–3 个有效采样点达到阈值才确认，参数可配置。
- 前置缓存默认 30 秒，缓存引用在事件任务成功入队前不能被普通清理抢先删除。
- 后置观察默认至少 60 秒；活动持续信号允许续期，但总时长默认不超过 5 分钟后必须重新判断。
- 同路重叠事件合并；跨摄像头事件不自动合并。
- 事件阈值/触发原因、模式变化及原配置快照入库，便于复现。

### 6.4 智能抽帧

L1 普通分钟：从每 10 秒候选帧、变化关键帧、窗口边界帧中去重，初始目标最多 6 张稳定代表帧，变化额外加入关键帧，普通模式每分钟上限 12 张。严格模式可送该分钟全部 60 张。高精度模式按事件区间可送 1 fps。所有帧 ID 在请求前固定，任务重试不得临时换入新帧。

图片转换应保留宽高比，限制最长边、总字节数和总张数；变换后的图片可短暂缓存，记录 transform version 与源帧 ID。不能因缩小或失败而改变原始证据。

---

## 7. 持久任务队列和调度

### 7.1 Job 状态

```text
queued → leased → running → succeeded
                      ├→ retry_wait → queued
                      ├→ failed
                      └→ needs_review
```

额外 `cancelled` 只用于用户显式取消；已运行中的 HTTP 请求可能无法撤回，UI 必须说明。

### 7.2 获取任务与租约

Worker 在短事务中选取 `state=queued AND available_at<=now` 的最高优先级任务，将其原子更新为 leased，写 `lease_owner` 和 `lease_expires_at` 后提交。Worker 心跳续租；超时后调度器将任务放回队列。数据库写锁只用于领取/提交状态，网络调用必须在事务外。

### 7.3 优先级

1. 高精度事件 L1；
2. 近实时 L1；
3. 已封账 L2；
4. 当前小时/日报 L3；
5. 用户触发的历史重算（可配置插队）；
6. 低优先级历史整理。

### 7.4 重试

- 网络超时、429、5xx：指数退避 + 随机抖动，尊重 `Retry-After`（若存在），有限次数。
- 401/403、模型不存在、模型不支持图像：停止快速重试，提示配置错误。
- 结构 JSON 错误：最多 1 次修复请求；仍失败标记 `needs_review`/`failed`。
- 原始帧已过期：标记 `evidence_expired`，不能以邻近日新帧代替旧时间段。
- 对实际请求、重试和供应商返回 usage 分别计数；重试同样计入用量。

### 7.5 Scheduler 调度点

- 每分钟结束：封存 MinuteObservation 输入；容忍配置内的摄像头时钟延迟。
- 每 5 分钟边界：创建 L2 Job；附加默认前后各 30 秒上下文索引，但输出活动不得越界覆盖。
- 每小时/日边界：L3 任务；只读第二层数据。
- 清理：按摄像头策略分批工作；限制每轮删除量，避免和采集写盘争用。
- 启动恢复：修复过期租约、校验 `.part` 文件、扫描悬空帧文件、恢复未完成的清理任务。

---

## 8. AI Gateway 与三层执行协议

### 8.1 Provider 配置

每个服务配置：`label, base_url, api_key, model, timeout, max_retries, concurrency, image_support, tools_support, json_mode_support`。密钥用应用主密钥加密保存；主密钥通过 Docker Secret 或受限环境变量注入，不写入 SQLite 明文、镜像或日志。

L1/L2/L3 分别配置主端点和可选备用端点。连接测试必须用当前层实际要求进行能力探测；探测结果只对该 provider + model 组合有效，不按供应商品牌整体推断。

### 8.2 OpenAI-compatible 子集

默认使用 Chat Completions 风格的 `messages` 请求；视觉请求以多模态内容项发送图片，L2 可以声明 tools/function 调用。不同兼容端点的图片类型、图片大小、工具调用和 usage 字段并不保证完全相同，因此 adapter 要做逐模型能力测试、错误归一化和可解释的降级，不把专有字段泄漏进业务层。核心需求不依赖 Responses API 或供应商私有 SDK。

适配层接口：

```python
class ChatCompletionAdapter(Protocol):
    async def complete(self, request: ChatRequest) -> ChatResult: ...
    async def probe(self, requirements: CapabilitySet) -> ProbeResult: ...
```

`ChatResult` 返回 `content`, `tool_calls`, `usage?`, `finish_reason`, `request_id?`, `provider_latency_ms`。输入消息、图片数量/字节、工具 schema 在发出前做硬限制。

### 8.3 第一层契约

输入：窗口时间、摄像头名称（不含 URL 凭据）、选定帧、上一观察摘要（可空）、本地变化/缺口信号。

输出 JSON（精简示意）：

```json
{
  "window": {"start":"2026-10-22T01:00:00Z","end":"2026-10-22T01:01:00Z"},
  "observations": [
    {"start":"...","end":"...","description":"桌面可见纸张和笔","evidence_frame_ids":["uuid"]}
  ],
  "candidate_activities": [
    {"start":"...","end":"...","name":"写作业","category":"学习","confidence":0.78,"evidence_frame_ids":["uuid"]}
  ],
  "gaps": [{"start":"...","end":"...","kind":"occluded","reason":"画面被遮挡"}],
  "needs_more_evidence": false,
  "uncertainties": []
}
```

校验：所有时间在窗口/允许边界内；证据帧 ID 属于任务输入；区间正长度；置信度 0–1；`unknown` 合法。

### 8.4 第二层推理与工具协议

初始上下文包括五条 MinuteObservation、前后相邻窗口的压缩活动、变化信号和目录。初始照片通常取变化点/边界帧。模型可以请求工具，但仅由服务端执行白名单工具：

- `get_frames(start,end,limit)`；
- `get_keyframes(start,end,limit)`；
- `compare_frames(frame_a,frame_b)`；
- `crop_frame(frame_id,region)`；
- `get_local_signals(start,end)`；
- `get_adjacent_activity(start,end)`；
- `request_high_precision(start,end)`（只允许可用缓存和受限历史范围）。

建议默认每个 L2 Job 最多 4 个工具轮次、初始最多 30 张帧、额外最多 30 张帧；高精度重算单独配置，最多覆盖 5 分钟内 1 fps 帧。达到限制仍不确定就返回 unknown/needs_review，不扩大到无限图片或工具调用。实际参数可在压测后配置。

工具可由模型原生 function calling 请求，也可以在模型不支持原生 tools 时走严格 JSON action 协议；两种路径均进入相同服务端授权和参数校验。工具结果回给模型时只提供指定帧/差异摘要，不暴露存储路径。

输出 JSON：

```json
{
  "window": {"start":"...","end":"..."},
  "segments": [
    {
      "start":"...","end":"...",
      "canonical_activity":"写作业",
      "display_description":"在桌前完成作业",
      "category":"学习","confidence":0.84,
      "intervals":[
        {"start":"...","end":"...","evidence_kind":"direct","evidence_frame_ids":["uuid"]},
        {"start":"...","end":"...","evidence_kind":"inferred","reason":"遮挡前后观察一致且无反证","anchor_frame_ids":["uuid","uuid"]}
      ],
      "source_observation_ids":["uuid"],"needs_review":false
    }
  ],
  "unknown_intervals": [],
  "transitions": [],
  "tool_summary": []
}
```

**连续性校验器**：

1. 检查遮挡前后候选是否相容、是否有反证、缺口是否低于配置上限（默认 5 分钟）。
2. 缺口必须使用 `inferred` 子区间，填明锚点和理由；降低置信度，置信度阈值可通过评测校准。
3. 时间段冲突、活动重叠、证据 ID 越权、区间超范围均拒绝写入。
4. 后续窗口改写已有段时，写新版本 + `supersedes_id` + 自动更正原因；锁定段不改。
5. 模型输出不能证明缺口期间内容时，保留未知区间，不将持续性假设当成确定事实。

### 8.5 第三层契约

输入只包含已经完成/用户修正后的活动段和活动目录统计。输出包括 `summary_text`、按标准活动的时长/次数、主要连续时段、unknown/inferred 时长、source segment IDs。源活动缺失时总结标记不完整；不能把“无数据”转写成“休息/没有活动”。

### 8.6 Prompt/Schema 版本

提示词与 JSON Schema 存于后端版本化资源文件/配置，不允许 UI 任意提交系统提示词。AIJob 固定 `prompt_version`、`schema_version`、模型 ID、采样策略快照。结构失败最多一次修复调用；所有响应在审计前移除密钥/内部路径/大段图像 payload。

---

## 9. REST API 契约

### 9.1 通用规则

- 前缀 `/api/v1`，JSON UTF-8。
- 时间使用 RFC3339 UTC；分页使用 `limit + cursor`，默认 limit 50，最大 200。
- 写操作支持 `Idempotency-Key`（尤其是补算、重试、创建 provider）。
- 成功响应直接返回对象或集合 envelope：`{"data":...,"meta":...}`。
- 错误统一：`{"error":{"code":"VALIDATION_ERROR","message":"可读说明","request_id":"...","details":{}}}`。
- `request_id` 由服务端生成并写日志；不向用户显示堆栈、SQL 或供应商私密响应。

### 9.2 端点清单

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/overview` | 摄像头状态、当前窗口、任务数、用量概览 |
| GET/POST | `/cameras` | 列表/创建 |
| GET/PATCH/DELETE | `/cameras/{id}` | 详情/编辑/停用；删除历史需单独确认参数 |
| POST | `/cameras/{id}/test` | RTSP 连接测试 |
| POST | `/cameras/{id}/reconnect` | 手动重连 |
| GET | `/cameras/{id}/health` | 状态、帧率、断流和缺口 |
| GET | `/timeline` | 按 camera/from/to 拉活动段、分钟临时态、缺口 |
| GET | `/segments/{id}` | 活动详情、证据索引、修订历史 |
| PATCH | `/segments/{id}` | 修改时间、目录映射、描述/分类、锁定状态 |
| POST | `/segments/{id}/split` | 在指定时间拆段 |
| POST | `/segments/merge` | 合并兼容相邻段 |
| POST | `/segments/{id}/reprocess` | 请求局部重新分析 |
| GET | `/frames/{id}/content` | 认证后的图片内容（短缓存/过期检查） |
| POST | `/frames/{id}/favorite` | 收藏/取消收藏 |
| GET | `/summaries` | 查询小时/日报 |
| POST | `/summaries/{id}/regenerate` | 重新生成摘要 |
| GET/POST | `/activity-catalog` | 列表/新增标准活动 |
| PATCH/DELETE | `/activity-catalog/{id}` | 修改、停用或删除（有引用时需迁移） |
| GET/POST | `/ai/providers` | Provider 脱敏列表/新增 |
| PATCH/DELETE | `/ai/providers/{id}` | 修改/删除 Provider |
| POST | `/ai/providers/{id}/test` | 探测文本/图像/tools/usage 能力 |
| GET/PUT | `/ai/bindings` | 查询/保存 L1/L2/L3 模型映射 |
| GET | `/ai/usage` | 用量筛选与分页 |
| GET | `/jobs` | 任务状态与错误摘要 |
| POST | `/jobs/{id}/retry` | 手动重试可恢复任务 |
| GET/PUT | `/settings/retention` | 留存配置 |
| GET/PUT | `/settings/analysis` | 抽帧、窗口、变化阈值、最大推断缺口 |
| GET | `/persons` | 匿名人物簇；可被整体关闭 |
| POST | `/persons/{id}/merge` | 手动合并匿名簇 |
| POST | `/persons/{id}/disable` | 停用并移除特征引用 |
| GET | `/events` | SSE 状态事件 |

### 9.3 关键 API DTO 示例

#### 时间轴查询

`GET /api/v1/timeline?camera_id=...&from=...&to=...&include_observations=true`

返回：

```json
{
  "data": {
    "segments": [],
    "observations": [],
    "camera_gaps": [],
    "window_states": []
  },
  "meta": {"timezone":"Asia/Shanghai","next_cursor":null}
}
```

#### 修改活动段

```json
{
  "expected_version": 3,
  "start": "2026-10-22T01:00:00Z",
  "end": "2026-10-22T01:20:00Z",
  "canonical_activity_id": "uuid",
  "display_description": "完成数学作业",
  "category": "学习",
  "locked": true,
  "edit_reason": "用户修正"
}
```

`expected_version` 不匹配返回 `409 VERSION_CONFLICT`，前端刷新后让用户确认，不静默覆盖。

#### 创建 AI Provider

```json
{
  "label": "主视觉模型",
  "base_url": "https://provider.example/v1",
  "api_key": "仅创建/轮换请求可写入",
  "timeout_ms": 60000,
  "enabled": true
}
```

GET 永远返回 `api_key_configured: true/false` 和脱敏尾缀（可选），绝不返回原密钥。

### 9.4 SSE 事件

`GET /api/v1/events` 发送：

- `camera.status`：在线/断流/重连；
- `frame.latest`：最近帧时间与缩略图引用；
- `job.updated`：L1/L2/L3 状态、进度和安全错误；
- `timeline.updated`：相机、时间区间、版本；
- `summary.updated`：摘要已更新/过期；
- `storage.updated`：容量/清理状态。

SSE payload 不包含 RTSP URL、API Key、原始模型 prompt 或完整图片数据。客户端断线后以 `Last-Event-ID` 续接；无法续接时前端重新拉 `/overview` 与活动时间轴。

---

## 10. 留存与磁盘管理

- 默认普通帧 1 小时、事件帧 6 小时；每个摄像头可继承全局默认或覆盖。
- 保留期是按实际 `captured_at` 计算；收藏默认覆盖自动清理，直到用户取消收藏/手动删除。
- AI 任务引用帧不自动无限延长保留。调度器在创建任务时为输入帧设置 `pin_until`，至少覆盖任务最大等待/重试周期；超过原始保留期仍无法处理时需标明证据过期。
- 用户选择“清除原图但保留时间轴”后，删除 Frame 文件和关联缩略图，保留 frame ID、时间戳和 `expired=true` 的证据元数据。
- 磁盘低水位提醒；首版只提示，不偷偷改变用户留存策略或删除收藏内容。可提供“立即清理已到期数据”按钮。
- 清理分批、可恢复、有统计：扫描数、删除数、跳过收藏数、失败数、释放字节数。

---

## 11. 安全、隐私和凭据

### 11.1 单用户认证

- 首次部署设置管理员密码；仅存 Argon2id 哈希。
- Session Cookie：`HttpOnly`, `SameSite=Lax/Strict`, HTTPS 时 `Secure`；写请求验证 CSRF token。
- 支持通过受信反向代理暴露；文档要求 TLS 和最小化端口。
- 可提供本地开发模式跳过认证，但生产 compose 默认不允许空密码启动。

### 11.2 密钥与 RTSP

- RTSP 凭据、AI Key 用应用数据密钥加密；主密钥由 Docker Secret/安全环境变量注入，启动时校验长度与版本。
- 轮换主密钥提供迁移命令；旧 key 未可用时拒绝启动敏感功能，不回退明文。
- 日志过滤 Authorization、URL userinfo、签名 URL、供应商响应中的敏感字段。

### 11.3 SSRF 与工具安全

- Provider base URL 必须 `https`，除非用户明确开启本地模型/内网端点策略；禁用意外重定向到未允许域/IP。
- 显式区分用户主动配置的内网 AI 服务与从 AI 响应解析出的目标；只有前者可通过策略允许访问。
- AI 工具执行服务端白名单，不接受模型给出的文件路径、任意 URL、shell 命令或 SQL。
- frame ID 查询必须验证其摄像头和时间范围属于当前任务输入/授权上下文。
- 上游 Prompt Injection 防护：图中内容仅为待观察数据，模型不得将其当作系统/工具指令。

### 11.4 图像外发告知

UI 显示每层当前 Provider、图片输入开关、采样策略和保留时间；更换 Provider 或启用严格模式时显示将发送图片的提示。AI 用量日志只保存计量与安全摘要，不复制完整图片。

---

## 12. 配置与运维

### 12.1 环境变量（示意）

```env
APP_ENV=production
APP_DATA_DIR=/data
APP_DB_URL=sqlite:////data/db/app.sqlite3
APP_ENCRYPTION_KEY_FILE=/run/secrets/app_encryption_key
APP_BOOTSTRAP_PASSWORD_FILE=/run/secrets/bootstrap_password
APP_TIMEZONE=Asia/Shanghai
API_BIND=0.0.0.0:8000
CAPTURE_DEFAULT_INTERVAL_MS=1000
WORKER_CONCURRENCY=2
LOG_LEVEL=INFO
```

Provider API Keys 优先通过 UI 加密保存；不在环境变量模板中要求把密钥写入版本控制。环境文件只提供无敏感默认值。

### 12.2 健康端点

- `/health/live`：进程存活。
- `/health/ready`：数据库迁移完成、数据卷可写、worker scheduler 配置正常。
- `/health/dependencies`：分别报告数据库、磁盘、摄像头与上游 AI 状态；AI 离线不应让 API readiness 失败，但应在能力状态中呈现。

### 12.3 备份恢复

- `sqlite` 在线备份使用 SQLite backup API 或一致性快照，不能在写入期间直接复制单个 DB 文件而忽略 WAL。
- 配置、数据库和图片卷分别备份；用户可以选择是否纳入图片。
- 恢复流程：停服务 → 恢复数据卷 → 校验 DB 和相对文件 → 执行迁移/修复 → 启动并检查健康状态。
- 提供迁移前自动备份和回滚说明；历史表结构迁移不得清理原图。

---

## 13. 可观测性

结构化 JSON 日志包含 `timestamp, level, service, request_id, camera_id?, job_id?, layer?, event`。禁止记录图像二进制、密码、API Key、完整 RTSP URL、模型原始多模态 payload。

指标：

- Capture：预期/实际帧数、帧率、解码错误、断流时长、重连数、磁盘写入延迟。
- Detector：diff score 分布、事件数、误触发/合并数、普通/高精度时长。
- Queue：排队数、最老任务等待、lease 超时、重试、失败、每层耗时。
- AI：provider/model/layer 请求成功率、耗时、图片数/字节、token、限流、费用估算可用率。
- Storage：总量、到期帧量、收藏量、清理释放量、磁盘可用量。

用户界面给可读诊断；堆栈只在受保护的服务端日志中，且去敏。

---

## 14. 测试策略

### 14.1 单元测试

- 时间窗口切分、UTC/local/DST 边界、左闭右开。
- 帧去重与变化平滑、事件合并、前后缓存截取。
- idempotency key、lease 过期、重试退避和优先级。
- Schema 校验：无效时间、重叠、未知帧 ID、越界证据、推断区间缺理由。
- 留存：收藏覆盖、活动帧期限、任务引用、清理失败重试。
- Provider URL/密钥脱敏/工具参数白名单。

### 14.2 集成测试

- 使用本地 RTSP 测试源/录制视频验证 1 fps 输出，不依赖真实云模型。
- 假 Chat Completions Server：正常 JSON、多图请求、tool call、429、401、超时、非法响应、usage 缺失。
- SQLite 迁移、WAL、并发领取、进程强杀后 lease 恢复。
- SSE 重连、事件序号续接。

### 14.3 端到端与稳定性

- 多摄像头中单路离线不影响其他路。
- 24 小时运行、Docker 重启、DB/WAL 恢复、磁盘低空间。
- 用户编辑、锁定、相邻窗口自动修订、摘要过期与重生成。
- 安全测试：未认证访问、任意 frame ID、路径穿越、SSRF、日志密钥泄漏、CSRF。
- L2 评测集分开测直接观察边界和遮挡推断；所有推断样例检查是否显示 infer label 和 anchors。

---

## 15. 后端完成定义

后端工作可验收须同时满足：

1. Docker Compose 在全新 Linux 数据卷启动、迁移、健康检查正常。
2. RTSP 多路每秒采集；断流、重连、每帧时间戳和缺口可见。
3. 普通帧/事件帧配置留存可生效，收藏帧不会被误删。
4. 每分钟形成 L1 AI 或本地稳定延续记录；状态来源准确。
5. 每 5 分钟 L2 可产生多个活动段，能够工具补证和显式推断遮挡区间。
6. 小时/日报严格引用 L2，并能因用户编辑自动失效/重算。
7. OpenAI-compatible Provider 可以按层绑定，能力探测、密钥脱敏、任务重试和 usage 记录可验证。
8. 所有前端需要的状态都可通过 REST 首屏加载和 SSE 增量同步。
9. 未知、处理中、失败、证据过期均是显式状态，不用空数据或默认活动掩盖。
10. 自动化测试覆盖任务幂等、存储清理、推断校验和安全边界。


