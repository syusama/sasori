# Sasori × LeAgent × ToFu：源码级对标与胜出路线

> 研究日期：2026-08-09
>
> Sasori 最新已托管验证基线：[`94f4d0e`](https://github.com/syusama/sasori/commit/94f4d0e58823e88c868c10297a7844289e4fbd5d)，[Hosted run 31302552621](https://github.com/syusama/sasori/actions/runs/31302552621) 全绿
>
> 本文对 Sasori Current 能力的结论绑定上述 exact commit/run；后续 revision 必须重新取得自己的证据，不能继承该结论
>
> LeAgent 基线：[`1f16badc`](https://github.com/vixues/LeAgent/tree/1f16badc834abbd829d3cb7e9f8fcb5b2d57f443)
>
> ToFu 基线：[`8b459a6f`](https://github.com/NiuTrans/ToFu/tree/8b459a6f3ca771e82136fc583d588664469850a1)

这不是按 README 关键词计数的功能表。LeAgent/ToFu 结论来自上述固定 commit
的入口、运行循环、事件、持久化、工具、上下文、插件、部署、测试和 UI 源码；
Sasori 的 Hosted 结论只绑定 `94f4d0e` 与 run `31302552621`。上游随时会变化；
重新做产品决策前必须刷新 Sasori、LeAgent、ToFu 的 commit 与证据。

## 结论先行

Sasori 已经在“小而可信的 Agent 机制层”胜出，但还没有在产品广度上全面
胜出：

- 相比 LeAgent，Sasori 的工具副作用语义、批准指纹、崩溃歧义恢复、运行时
  权限披露、发布锁定和真实浏览器闭环更严格；LeAgent 的文件产物、上下文、
  Workflow、Memory、GenUI 和桌面产品更完整。
- 相比 ToFu，Sasori 的核心可读性、不可变契约、纯事件投影、工具协议拒绝、
  依赖/镜像完整性和 claim 边界更强；ToFu 的上下文工程、长期 Memory、项目
  协作、多 Agent、API/客户端和产品覆盖面更广。
- “比两者强”不能等价为堆出更多菜单。Sasori 的胜出公式是：**先让每一项
  机制在失败与恢复路径上更可信，再通过核心外模块快速扩展产品面**。

因此，Sasori 当前可准确定位为：

> **One kernel. Many puppets.** 一个标准库轻核，把 Python、CLI、HTTP 与
> Workbench 牵引到同一条可恢复、可审计的单 Agent 路径；Provider、Context、
> Storage、RAG、Apps 与 UI 在核心外按需装配。

不得在当前 README 中宣称：已具备完整语义 Memory、Artifact access grant/版本
生命周期、Workflow、多 Agent、插件沙箱、水平扩展、中央市场或正式签名发布。

## 判定口径

| 标记 | 含义 |
|---|---|
| 已证实 | 对应固定 commit/revision 有源码入口、运行链和明确标注的测试/验收；本地与 Hosted 证据不得混写 |
| 部分证实 | 有实现，但只覆盖部分入口、部署或故障路径 |
| 声明存在 | README、配置或类型存在，端到端运行链未证明 |
| 路线图 | 设计目标，不属于当前已交付能力 |

评审优先级为：运行时事实 > 可运行测试 > 静态实现 > README/配置声明。

## 当前能力矩阵

| 能力 | Sasori | LeAgent | ToFu | 判定 |
|---|---|---|---|---|
| 核心体量 | `sasori` 核心仅标准库，单 `Harness`/Loop | 大型模块化单体，后端/React/Electron/大量工具 | 约 3,453 文件，`lib` Python 约 239K LOC，产品型巨型单体 | Sasori 胜 |
| 单运行路径 | Python/CLI/HTTP/Workbench 共用 `Harness.run/resume → _drive` | Chat/SDK/Task/Subagent/Workflow Agent 多数汇入 `run_loop → QueryEngine` | SDK、chat 与 agent API 汇入 task kernel，但 Endpoint/Autopilot/Swarm/Flow 仍有多轨 | Sasori 最干净 |
| 公共事件 | 版本化语义投影；SQLite commit 后 sink；live/cold/reconnect 共用纯 reducer | 结构化事件丰富，但热 event/approval/output registry 有进程内状态 | `EVENT_CONTRACT_VERSION=1`、durable-before-visible、committed-before-done、稳定 `_msgId` | Sasori/ToFu 各有长处 |
| 工具协议 | malformed/truncated 不执行；异常显式 tool error；duplicate call 拒绝 | 统一 ToolExecutor，类型/审批面丰富 | ToolSpec/registry 与统一 dispatch 较完整 | Sasori 故障语义胜 |
| 副作用恢复 | effect/revision/idempotency、dispatch intent、`effect_unknown`、人工恢复、真实 crash tests | 有审批/checkpoint，但未见等价的 side-effect ambiguity contract | 有事件、审批、写入 freshness/idempotency 元数据；崩溃后执行恢复边界不等价 | Sasori 明显胜 |
| Context | `94f4d0e` 已托管验证核心外、结构安全的确定性预算投影与拒绝调用恢复 | ContextSource、预算、压缩与 recall 比 Sasori 全面 | 三层 compaction、cache-stable prefix、token 压力与 archive 很强 | ToFu 广度胜；Sasori 底座已托管验证 |
| Memory | RAG/FTS5 是独立插件，不等于长期 Memory | episodic/semantic/procedural 持久化 + lexical fallback；存在 turn 去重缺陷 | BM25 top-40 + LLM rerank、profile core/detail、失败不注入 | Sasori 落后 |
| Artifact/FileRef | `94f4d0e` 已托管验证核心外 immutable Ref、同事务 event/metadata、no-overwrite blob、run-scoped list/content/HEAD/Range、text/JSON Workbench 与真实浏览器/重启/篡改门禁；尚无用户 grant、版本、GC、PDF/image preview | `FileRef`、FileService、预签名预览/下载、工作流资产复用，但 blob/metadata 原子性、locator 泄露与进程内 cleanup 边界较弱 | Artifacts/Canvas、版本面板与 CSP 较丰富，但 ownership/raw/view/export 授权及 HTML/SVG 网络边界较弱 | Sasori 的最小可靠闭环胜；竞品广度仍胜 |
| Skills/plugins | 严格 manifest/digest/catalog；installed entry point 明示 trusted code；市场为空 | Skills 安装/注册面完整，但安装/依赖/脚本供应链权限过宽 | Skills Store、MCP、工具插件面广 | Sasori 信任边界胜、生态落后 |
| Workflow | 未交付 | ReactFlow DAG、typed tool nodes、持久化状态与 UI | Autopilot/Endpoint/Flow 等产品流丰富但实现多轨 | Sasori 落后 |
| 多 Agent/项目协作 | 未交付 | Subagent 与 workflow agent | Swarm、Charter/Board/activity/path soft lease | Sasori 落后 |
| UI | 独特 Puppet Workbench；真实浏览器覆盖批准→恢复→重载 | React 19、Workflow、GenUI、Desktop，产品面更完整 | 全功能 SPA、真截图、桌面/浏览器/移动端 | Sasori 审美有辨识度、广度落后 |
| Provider | OpenAI/Anthropic stdlib 适配，共享 conformance；未宣称 live smoke | Provider/路由/降级更多 | OpenAI-compatible 与多 provider/多 key 路由更广 | 竞品广度胜，Sasori 合约胜 |
| 本地交付 | Python/CLI/HTTP、no-build UI、国内源 Compose | dev/Docker/Desktop 安装路径丰富 | 一键安装、Docker/Desktop/Agents/SDK 丰富 | 竞品易用性胜 |
| 依赖与镜像完整性 | base digest、Python/build hash、国内镜像、SBOM/binding、wheel/sdist matrix | base/apt/pip 未等价锁定，Docker label 版本漂移 | 多 `>=` 无 hash、Docker 未用国内源/digest、Playwright 安装可吞错 | Sasori 明显胜 |
| 默认沙箱表述 | 明示 full host process privilege；路径限制不冒充沙箱 | README 称 isolation，但当前 inproc/subprocess 实现与宿主同权 | Shell/桌面/浏览器执行主要是主机能力与可选防护，取消 best-effort | Sasori 诚实性胜 |
| 水平扩展 | 明确单 owner/单 mutation，不声称多 worker | PG 不消除 process-local registry/queue 热状态 | 多用户/PG 功能广，但 task dict/进程执行与恢复边界复杂 | 三者均不能仅凭数据库宣称完整 HA |
| 测试证据 | `94f4d0e` 的 Windows/Linux × Python 3.11–3.13、277 项源码、wheel/sdist、国内源容器、同尺寸篡改、Chrome 竞态与真实 17-event Artifact lifecycle 已由 Hosted run `31302552621` 全绿验证 | 测试覆盖广，但 README claim 与 release/container 证据未系统绑定 | 测试规模巨大，但当前固定 HEAD 的 hosted workflow 非绿 | Sasori 当前证据链胜 |

ToFu 当前 HEAD 的 hosted jobs 没有获得 GitHub hosted runner，页面显示平台内部
错误；这意味着“缺少当前绿色托管证据”，**不**意味着源码测试已被证明失败。

## LeAgent：值得学什么

### 1. README 是产品落地页

[LeAgent README](https://github.com/vixues/LeAgent/blob/1f16badc834abbd829d3cb7e9f8fcb5b2d57f443/README.md)
先用 logo、slogan、badges 与 hero 截图完成第一印象，再按“能做什么 → 架构 →
执行模型 → 工具目录 → 深入能力 → Quick Start → Desktop → Operations”展开。
它讲场景，不只是列类名。这一点应完整吸收。

Sasori 的改进方式：首屏更短，先给 30 秒运行路径；机制卖点链接到确定性测试
或 CI；已交付与 Next 分栏；详细风险转入 ADR/FOUNDATION，避免让首页像审计
报告。

### 2. `ExecutionRun` 是很好的关联根

LeAgent 把入口统一到 `ExecutionRun`、runtime context、`run_loop`、QueryEngine、
ToolExecutor，再分流到持久状态与观测。这与 Sasori “一个 run、一条 loop、多个
adapter”方向一致。可借鉴其 parent run、状态归属与工具/workflow 复用方式，但
不能把热 run handle 只留在进程内后又宣称数据库切换即可横向扩展。

### 3. FileRef/Context/Workflow/GenUI 是产品价值高地

LeAgent 的通用
[`FileRef`](https://github.com/vixues/LeAgent/blob/1f16badc834abbd829d3cb7e9f8fcb5b2d57f443/backend/leagent/file/service.py)、
ContextSource/预算、Checkpoint、WorkflowDocument、typed tool nodes、MediaRef 与
GenUI 把 Agent 的“回复”变成可继续加工的产物。Sasori 应按顺序建设
Artifact → Context/Memory → Workflow → GenUI，而不是先复制 100+ 工具名称。

### 4. 三类 Memory 的准确边界

当前代码确实构造 episodic/semantic/procedural store，并通过 recall source 注入
prompt；Milvus 默认关闭，向量不可用时走 BM25/ILIKE lexical fallback。因此应
学习其分层与预算，不应复制“默认全向量记忆”的营销简写。

### 不复制的 LeAgent 问题

1. **沙箱命名漂移。**
   [`tools/_sandbox/inproc.py`](https://github.com/vixues/LeAgent/blob/1f16badc834abbd829d3cb7e9f8fcb5b2d57f443/backend/leagent/tools/_sandbox/inproc.py)
   已移除限制并直接 `exec`；
   [`code/sandbox.py`](https://github.com/vixues/LeAgent/blob/1f16badc834abbd829d3cb7e9f8fcb5b2d57f443/backend/leagent/code/sandbox.py)
   也明示与宿主同权、无 namespace/rlimit。没有 OS/container boundary 就不得称
   isolated sandbox。
2. **进程热状态。** `ExecutionRunRegistry`、approval、event/output stream 与 memory
   queue 是进程内状态；PostgreSQL 和 sticky session 不是 durable worker protocol。
3. **Memory 去重缺陷。** `observe_turn()` 以 `turn_id` 去重，但当前
   `TurnObservation` 无该字段，key 退化为 session；同 session 后续回合可能被误判
   为重复。Sasori 的 Memory 验收必须包含同会话两个不同 turn 与崩溃重放。
4. **Skill 供应链。** 普通登录用户可走 API 安装全局 skill；URL 重定向/私网边界、
   archive link、可选 sha256、自动向 backend interpreter 安装 Python 依赖等组合
   风险不能进入 Sasori。第三方 skill 必须继续被称为 trusted installed code，直到
   真正隔离。
5. **发布漂移。** 根/backend/frontend 版本、tag target 与 Docker label 并不完全
   对齐；镜像和 Python 安装也无 Sasori 等价的 digest/hash 门禁。

## ToFu：值得学什么

### 1. 先让用户跑起来

[ToFu 中文 README](https://github.com/NiuTrans/ToFu/blob/8b459a6f3ca771e82136fc583d588664469850a1/README_CN.md)
按品牌/截图 → 五个差异点 → 分 OS Quick Start → 模型设置 → Headless API/SDK →
场景化功能 → 架构/安全/贡献组织。每项功能用“什么时候用、怎么工作”解释，
对非框架作者非常友好；还把面向人类和面向 coding agent 的文档分开。

Sasori 应学习其真截图与场景语言，但避免 800+ 行、重复介绍、owner/CI badge
漂移和无证据的绝对承诺。README 只保留决策所需信息，深挖链接到专题文档。

### 2. 事件顺序是可靠性的核心

ToFu 的 `EVENT_CONTRACT_VERSION=1`、集中 EventType/build/emit、persistent
`task_events` commit-before-push、final commit-before-done，以及跨流/冷加载/重连
的 `_msgId` 都值得保留。Sasori 已通过 SQLite commit-before-sink、`(run_id, seq)`
游标、pure reducer 与真实浏览器竞态测试实现更小的对应机制；后续所有消费者
继续共享同一 golden trace，不能各写一套状态机。

### 3. Context 工程领先

ToFu 的 context pipeline 包含零 LLM micro-compaction、结构裁剪、压力触发 LLM
summary、原文 archive，以及 prompt-cache 稳定前缀。Memory 采用 bounded recall
与 cheap-model rerank，失败/超时宁可不注入。Sasori 本次先落地了
[`sasori_context`](CONTEXT.md) 的确定性预算与工具组原子性；下一阶段再增加可评测
的 semantic summary 和检索 Memory。

### 4. 截断工具调用防线值得学，但授权语义仍可更严格

ToFu 的 `unparseable_tool_calls()`、stream analyser 与
`tests/test_stream_truncation_guards.py` 会在 `_missing_done=true` 且 arguments JSON
不可解析时丢弃整轮并拒绝执行；固定快照的 24 条专项测试可直接通过。但同一测试
也规定：没有 `_missing_done` 证据时，损坏参数可以进入 JSON/schema repair，修复
成功后执行。Sasori 保持更严格的不变量：原始 envelope 结构无效就永远没有执行
授权；repair 只能提示模型重新发出一个新的、完整的调用。

### 5. Project Brain 提供了多 Agent 之前的共享协议

Charter、Board、activity、conversation message 与 path soft lease 比“直接递归
spawn Agent”更有产品价值。Sasori 后续应先定义项目状态、ownership、lease 与
人工批准协议，再让多个 worker 复用同一 Harness；soft lease 必须明示 advisory，
不能冒充文件锁。

### 不复制的 ToFu 问题

1. 开放可变 task dictionary 作为跨线程状态中心。
2. Endpoint/Autopilot/Swarm/FlowExecutor 演化出的多条编排路径；Sasori 的新能力
   必须复用同一 Harness 或在核心外成为明确上层 orchestration。
3. 约 239K `lib` Python LOC 与巨大 Vanilla JS 全局面；功能数量不能以牺牲可读
   runtime 为代价。
4. host subprocess/path blocklist/portable guard 不能称默认安全沙箱；cancel/abort
   只能描述为 cooperative/best-effort。
5. durable event 可重读不等于崩溃进程里的执行线程自动恢复。
6. 当前 `pyproject.toml`/README/package.json 声明 MIT，但固定 commit 没有顶层
   `LICENSE`/`COPYING`；CI badge 仍指旧 owner，README 链接存在迁移漂移。
7. Docker 未默认中国大陆源、未锁 base digest；多项 Python 依赖为 `>=` 且无
   hashes，浏览器安装路径存在吞错。不能降低 Sasori 的供应链标准换取“一键”。

## README 改写规则

### 必须保留

- 有辨识度的蠍/傀儡品牌，而不是通用渐变 AI 图标。
- 一句话说明“Python-first、小内核、一条运行路径”。
- 30 秒可复制 Quick Start。
- 真 Workbench 图、真实架构图、真实失败/恢复路径。
- 当前能力矩阵和明确的 Next，不把路线图混入 feature list。
- 每个安全/恢复 claim 指向 ADR、测试或精确 Hosted run。
- 中英文入口、贡献路径、Security、MIT License。

### 禁止出现

- “100% secure / production ready / exactly once / isolated sandbox”。
- “支持所有模型、所有部署、无限扩展”。
- 未真实运行的 provider smoke、marketplace、workflow、多 Agent、Memory。
- 把安装 entry point、路径检查、manifest permission 说成隔离或权限 enforcement。
- 为了显得功能多，复制竞品的菜单、文案或视觉资产。

## 胜出路线与验收门禁

### P0：可信开发者底座

| 增量 | 状态 | 必须通过的验收 |
|---|---|---|
| 品牌 README + 中英文入口 + 真图 + current/next | 本次 | 链接检查、图片渲染、claim review、`git diff --check` |
| 固定 commit 的 LeAgent/ToFu 对标文档 | 本次 | commit/许可证/证据路径复核 |
| 结构安全 context budget | `94f4d0e` 已托管验证 | under/over budget、parallel calls、orphan/incomplete、custom estimator、adapter 回归 |
| ArtifactRef + immutable blob metadata + run association | `94f4d0e` 已托管验证 | filename/MIME 不可信、digest/size 校验、同事务 event/metadata、no-overwrite、range/download、跨重启、cross-run 拒绝、篡改 fail closed |
| cooperative cancellation | 暂缓至独立 ADR | awaiting approval/resume/effect_unknown/running/terminal race matrix；不误杀其他 run |

### P1：有用的模块生态

| 增量 | 验收定义 |
|---|---|
| Semantic compaction | 工具组不拆分；事实保真评测；成本/模型/失败可见；原 transcript 不变 |
| Durable Memory | bounded retrieval；source/score/version；同 session 多 turn；删除/重建；注入失败关闭 |
| Artifact Workbench | `94f4d0e` 已交付并托管验证 text/JSON 安全预览、认证下载、冷加载、stale-run 隔离与真实浏览器链；图片/PDF 需独立内容校验后再开放 |
| Skill selection | progressive disclosure；确定性 eligibility；预算；恶意 SKILL.md；不自动执行安装脚本 |
| Curated marketplace | immutable digest、publisher/review、compatibility、撤回、升级差异、权限再批准 |
| Provider breadth | 每个适配器跑共享 malformed/timeout/429/interrupted/duplicate/cancel suite + 可选 live smoke |

### P2：同一内核上的完整产品

| 增量 | 验收定义 |
|---|---|
| Workflow | typed DAG、tool node 复用、durable node state、human gate、retry/effect policy、visual editor |
| Project/多 Agent | Charter/Board/lease/ownership；每个 worker 仍走 Harness；预算、取消与故障隔离 |
| GenUI | versioned safe component schema；无任意 HTML/JS；live/cold reducer parity；导出/Artifact 链 |
| Durable executor | lease/heartbeat/fencing、queue、crash takeover、per-run identity、backpressure、multi-process tests |
| Strong isolation | 明确容器/VM boundary、filesystem/network/resource policy、escape tests；不复用 trusted-process 文案 |
| Product suite | Chat、skills、数字员工、artifacts、workflow 与 admin UI；响应式、键盘、reduced motion、真浏览器 E2E |

## 统一验收命令

每个增量至少执行：

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
node --test tests/workbench_event_reducer.test.cjs
python tests/workbench_browser_acceptance.py --require-browser
python tests/workbench_browser_journey.py --require-browser
git diff --check
```

涉及 Docker 时，还必须通过默认 DaoCloud base、清华 PyPI 与国内 Debian mirror
的真实 build，并执行 Compose 的 Agent workflow/restart/exclusive-owner 验收。
涉及 public events、recovery、golden trace 或 plugin permissions 时，先更新 ADR，
再更新实现和消费者，最后由集成者重跑完整门禁。

## 许可证与复用边界

- Sasori：MIT，详见 [`LICENSE`](../LICENSE) 与
  [`THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md)。
- LeAgent 固定 commit：顶层 `LICENSE` 为 Apache License 2.0。
- ToFu 固定 commit：`pyproject.toml`/README 声明 MIT，但仓库树中无顶层许可证
  文本；在复制代码前必须让上游补齐或取得可审计的授权文本。
- 本研究提取架构思想、运行时不变量与反模式；没有复制两项目代码、文案或视觉
  资产。以后复用任何源文件都必须单独记录 origin、commit、license、modification
  与 NOTICE 义务。
