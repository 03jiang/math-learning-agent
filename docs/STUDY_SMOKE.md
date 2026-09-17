# 当前主流程的文字验证（B 阶段准备）

本入口复用页面使用的 `StudyService`、分析/订正校验和 `Notebook`，验证任意文字输入的调用与保存链路。本入口的冻结账本保持原行为。普通页面另有默认关闭的 [有限只读工具循环](AGENT_LOOP.md) 和独立执行记录，已接通可选跨题历史并离线测试；教学设置与完整上下文管理仍待实现。

默认只预览，不需要密钥，不调用模型。所有输出留在本机，使用新目录；验证存档在报告目录的 `notebook/`，不修改日常错题本。

## 先预览或演练

在仓库根目录、装好锁定依赖的 Python 环境执行：

```bash
python tools/verify_study_live.py preview --output evaluation_runs/study-preview-001
python tools/verify_study_live.py preview --output evaluation_runs/study-focused-001 --rows b01 b02 b03
python tools/verify_study_live.py local --output evaluation_runs/study-local-001
python tools/verify_study_live.py status --directory evaluation_runs/study-local-001
python -B -m unittest test_study_smoke -v
python -B -m unittest discover -v
```

目录已存在时不会覆盖；换一个新的名字。`local` 的全部回复和用量都是手写 HTTP 数据，自动保存决定标为 `local_fixture`，真实请求为 0。它不能作为解题准确率或模型自主行为的证据。

`--rows` 只能在 preview 时选择范围，默认仍是完整 7 次。所选范围写入冻结计划，不能在运行时扩展；选择订正行必须同时包含原分析行。仅复测 b01–b03 时，计划和累计请求上限均为 3，不读取或拼接其他计划的原分析。选中原分析并不意味着自动确认保存。

| 请求 | 输入范围 | 预期（待人工检查） |
|---|---|---|
| b01 | `2/5 + 1/4`，有正确通分步骤 | 认可学生方法，解释共同分数单位 |
| b02 | `2/3 + 1/6`，错误地得到 `3/9` | 定位相加错误；约分步骤本身正确，不整段误判 |
| b03 | `5/6 - 1/4`，只有答案 `1/2` | 说明结果不符，不猜学生计算过程 |
| b04 | `5x - 4 = 16`，先除以 5 再相加 | 认可等价方法，不强制参考步骤顺序 |
| b05 | 一个数增加未知数量后等于 18 | 条件不足先澄清，不补造唯一答案 |
| b06 | b02 的新作答 | 依赖用户确认保存 b02；检查新步骤和原文证据 |
| b07 | b04 的另一种正确方法 | 依赖用户确认保存 b04；方法改变不代表从错到对 |

这 5 道自写题与 2 次订正属于初始接口验证材料，不是 7 道独立题目、保留集或真实学生样本。参考答案及预期行为在计划和人工检查表中；不会进入模型请求。b06/b07 的旧分析要等实际回复且用户确认后才可确定，预览不放入模拟旧分析。

## 真实运行：先确认计划、上限与预算

以下是使用方式，不是自动执行指令。运行前应核对官方当前模型、接口和单价，审阅 `manifest.json`，另行确认最多请求数和可接受费用。费用估算与批准记录保存在各次本机运行材料中；脚本本身不自动计算费用。

`--max-requests` 是**整份计划累计**尝试名额的硬限制，最多 7；失败和完成情况未知的尝试不会自动重试。`--budget-note` 是人工费用确认记录，**不是自动计价器或供应商扣费封顶**。需要金额硬限制时，应先在供应商侧设置可用的限额；不要把下面的文字确认当成金额保护。

真实模式要求代码已提交、工作区干净，并与预览时完全一致。用源码导出包开发时，先在自己的 Git 仓库提交，然后重新生成预览。代码、配置或案例变化后必须新建计划。

```bash
python tools/verify_study_live.py run \
  --directory evaluation_runs/study-preview-001 \
  --max-requests 7 \
  --confirm-plan '复制预览显示的完整计划编号' \
  --budget-note '填写实际已确认的费用范围与依据，不能照抄占位文字'
```

没有本进程的 `DEEPSEEK_API_KEY` 时，程序在交互终端隐藏输入；不接受命令行密钥参数、不写配置、日志或 Git。真实模式不能注入本机手写响应，也不能把 local 报告改成 live 续跑。

首次最多发送 b01–b05，遇到失败立即停止。每行的 `responses/bXX.json` 保存实际请求、最终回复、格式检查状态和调用记录。先阅读输出，参考 `review.json` 人工填分；`reply_valid` 只表示结构和原文引用等程序检查通过，不代表数学正确。只有主动确认才进入验证错题本：

```bash
python tools/verify_study_live.py decide --directory evaluation_runs/study-preview-001 --row b02 --accept
python tools/verify_study_live.py decide --directory evaluation_runs/study-preview-001 --row b04 --accept
```

也可以用 `--reject`，它不会创建或改写对应错题文件；若拒绝原分析，相应订正请求被阻止，不收费。输入与结果已经冻结，若发现内容需要修改，先拒绝，不在报告里手改 JSON 后强行确认。候选结果超过 24 小时会拒绝保存。

确认 b02/b04 后，用**完全相同**的计划编号、累计上限和预算说明再次执行 `run`，才发送 b06/b07；已执行的五行不会重发，也不能把上限从 7 当作“再加 7”。随后分别阅读订正输出，决定接受或拒绝。重复同一确认不会再次修改；确认后不能用反向决定覆盖历史。

`decide` 保存后会重新实例化 Notebook 读取校验，结果在 `decision.reopen_verified`。这验证了本地持久化，不代表教学掌握。接受订正不会自动增加“独立做对”的自评。

## 看哪些记录

### 只复测订正：复用已确认的真实原分析

若旧计划的原分析已由用户确认保存，但后续订正失败，可只读复用原分析另建一条订正预览：

```bash
python tools/verify_study_live.py preview --output evaluation_runs/correction-only-001 \
  --reuse-from evaluation_runs/old-confirmed-run --rows b06 --output-mode strict_tool
```

这不是续跑失败计划。`parent_source` 冻结来源计划编号、原回复、用户接受决定、版本 1 存档及文件哈希；题目和原作答必须与该订正案例匹配。只接受仍未改动的原分析，本版不复用已有后续订正的版本。真实模式不能复用本机模拟回复。预览已含完整订正 payload，人工参考答案不进入请求。

真实发送前仍需另行确认**新计划的 1 次请求与预算**，使用新 plan_id；运行方法与上文相同，`--max-requests 1`。旧计划的失败行和调用次数保持原样。新计划只统计本次订正，不把复用分析算作新模型调用。

未确认或拒绝时不创建新 Notebook。用户接受订正后，原分析副本与订正使用同一个原子写入保存到**新计划的 notebook/**；旧计划目录始终只读。来源在预览后被修改、结果过期或证据非法时拒绝发送/保存。重复确认以及保存后决定日志中断的重放，不会追加第二条订正，也不标记掌握。新的 Python 进程可重新读取整条记录。

这一复用入口已通过本机 HTTP、真实来源只读核对和自动测试；新的真实订正尚待另行执行及用户确认。不能把本机模拟闭环计作真实验收。

| 文件 | 内容 |
|---|---|
| `manifest.json` | 提交与源码指纹、参数、版本、7 行计划；plan_id 冻结整份计划 |
| `responses/bXX.json` | 发送前的 running 标记、真实 payload、输入长度摘要、返回模型（如有）、用量、耗时、结果和用户决定 |
| `approval.json` | 真实执行的计划编号、累计上限与人工预算说明；没有密钥 |
| `status.json` / `review.md` | 状态汇总；`status` 命令直接重读账本，不自动续跑 |
| `review.json` | 逐条人工检查表；空值为未评分，程序不自动给数学分数 |
| `notebook/` | 仅明确确认后的验证错题与订正；与日常数据分开 |
| `local_checks.json` | 仅本机演练生成：未确认不写、拒绝不改、去重、重新读取等结果 |

`saved` 统计接受保存的操作数，不是独立题目数。`reserved_attempts` 统计已登记名额；发出情况未知时另外标记 `real_api_calls_unknown`，不会把未知算作 0 次。`usage_recorded_rows` 显示有用量的行数，用量缺失不当成零；当前不计算费用。`usage_source=handwritten_fixture` 表示本机用量也是模拟数据。

`failures` 列出失败题号、接口/内容错误码与已知的固定校验错误码。例如 `student_review_fields` 表示 student_review 的字段不符合约定；不会把模型自造字段名、错误正文或凭证写进错误说明。旧记录没有细分类别时保持空值，不补造历史日志。

HTTP 错误正文不记录。合法回复只保存最终 content（json_object 模式）或指定函数 arguments（strict_tool 模式）的结构化内容；非法 JSON 可以保留脱敏、限长诊断文本；凭证回显整条拒绝。供应商 `reasoning_content` 不展示或保存。

如果进程在请求期间中断、运行记录写入失败或服务报错，会保留 `running` 或 `failed`。再次运行会停止；先查明这次尝试，不能删除记录来自动重试。要再尝试必须另建计划、重新确认预算，并在人工记录中保留原失败。已有私人数据和错误记录不会被覆盖。

## 关键代码与下一验收

2026-09-16 首次真实运行：在提交 `bc397a0e8305cd1b8ad7180ac068ac758ca34186` 上，计划 7 次、已尝试 3 次、结构通过 2 条、结构失败 1 条、未运行 4 条、保存 0 条。三次均收到 HTTP 200，模型请求与返回标识均为 `deepseek-flash`；使用量为输入 2,814、输出 1,709、共 4,523 token。没有教师评分；别名不等同于可永久复现的供应商模型版本。

- b03 仅有答案：回复同时含顶层 `diagnosis` 和额外的 `student_review.diagnosis`，严格字段检查拒绝保存，执行停止。这是内容协议失败，HTTP 200 不代表分析已通过。
- b02 有错误步骤：回复格式通过，但将后续正确的约分也标为 incorrect，说明结构检查不能替代数学核对。原始输出与未评分检查表保留在本机。
- 提示词修订：明确 `diagnosis` 与 `student_review` 并列；各作答类型使用明确字段路径；逐步判断区分本步变形与整体结论，避免错误传播。分析提示词版本为 `photo-study-v4`，订正为 `photo-correction-v3`，识图提示词不变，分析 JSON schema 仍为 2。
- 保留原解析、校验、确认和停止规则，不删除未知字段来把失败改成通过。新增手写回归案例复现字段错误、停止与禁止重试；它们不能证明真实模型已改好。再次付费验证需新建冻结计划并确认次数和预算，不能覆盖或续跑这份失败记录。

同日修订版本 `eb5406fe4248a82c49df28cd42edcfb313dcd24a` 的实测结果：b01 第一条再次返回额外的 student_review.diagnosis，结构失败后停止；其余 6 条没有发送。输入 1,215、输出 757 token，HTTP 200。因此上轮提示词修订没有解决已观察到的结构问题，不能宣称改进成功；b02 未执行，正确约分误判的变化仍未知。两轮共 4 次尝试、2 条结构通过、2 条结构失败，不是 4 道独立题。

这条新回复还合理指出了学生没有回答“为什么通分”。旧校验把 partial 一律要求成必须找到错误步骤，会拒绝这种漏答反馈。当前修订允许：有步骤、步骤全部正确、answer_feedback 明确说明漏答、diagnosis 为空的 partial。程序能验证反馈存在及证据一致性，不能自动证明漏答判断本身正确，仍须内容复核。判 incorrect 仍必须有错误步骤；不确定的步骤不能冒充全部正确，漏答也不能编造成错因证据。

当前提示词改用另一道自写加法题展示完整 JSON 输出，分析/订正分别使用完整外层结构，版本为 `photo-study-v5` / `photo-correction-v4`。格式示例不是当前验证题的参考答案，验证题干、学生原作答和判断标准没有改动。原始失败仍失败，内存中的诊断副本不会保存。

[DeepSeek 官方 JSON Output 说明](https://api-docs.deepseek.com/zh-cn/guides/json_mode/)要求提供格式样例；`json_object` 不能替代本应用的字段、重复键与教学约束检查。第三轮使用完整样例后的实际结果见下文，不能据此宣称真实失败率降低。

2026-09-16 第三轮提交 `6efb4c44c52fec751f34a3a2bd658e45391a2166`：定向计划 b01–b03，第一条 b01 收到 HTTP 200，但 student_review 内同一个 answer_feedback 出现两次，内容相同也被拒绝；另两条未发，保存 0 条。用量输入 1,634、输出 629 token。此次没有再出现嵌套 diagnosis；回复指出漏答理由，但结构仍失败。旧摘要 validation_issue 为空，因为重复键由通用解析器拦截；现在新增固定错误码 duplicate_json_key，旧记录保持原样。

三轮共 5 次调用、2 条结构通过、3 条失败，累计输入 5,663、输出 3,095 token，均未保存或教师评分。重复调试题不是独立测试样本；数学正确性及已知约分误判仍需复核。原始报告只保存在本机，不加入公开包。

## 可选的 strict 返回格式（分析已验证，订正仍待验收）

为减少自由生成 JSON 的结构错误，新增 `strict_tool`：单次强制返回指定函数的参数，并保留本地所有检查。它是结果格式通道，不执行任何函数或工具，不发送第二轮工具结果，也不构成模型选工具的 Agent 循环。

[DeepSeek 官方 strict 说明](https://api-docs.deepseek.com/zh-cn/guides/tool_calls/)要求使用 Beta 地址、函数 strict=true，所有对象属性必填且 additionalProperties=false。[接口说明](https://api-docs.deepseek.com/api/create-chat-completion/)说明指定工具的强制调用不支持思考模式，因此本通道只接受 thinking=disabled。使用基础类型与 enum；长度、逐字引用、错因证据和保存决定仍由应用检查。

```bash
python tools/verify_study_live.py local --output evaluation_runs/strict-local-001 --output-mode strict_tool
python tools/verify_study_live.py preview --output evaluation_runs/strict-one-001 --rows b01 --output-mode strict_tool
python -B -m unittest test_structured_output -v
```

预览冻结 `https://api.deepseek.com/beta/chat/completions`、返回模式、schema、指定函数与所有参数。模型名仍来自配置，默认不变。真实运行只读取冻结模式，不接受临时切换；第一条返回 HTTP 400、多个函数调用、未知函数、截断内容、重复字段或不合格引用，都会失败停止，不改用原接口重试。b01 单条分析已获真实接受，见下文；不能把这一次结果外推到其他模型、操作或全部输入。后续验证须另行冻结计划并确认范围，已用完的单条计划不重跑。

`study/output_contract.py` 的 parse_output 拦截相同值的重复字段、非标准数值和非法 JSON；strict_response_text 只提取一个指定函数的 arguments，不执行它。根目录旧教学解析器继续拒绝工具调用。`study/service.py` 的三个操作共用此通道，页面可用环境配置选择；分析已通过两条真实请求，首次真实订正失败，照片转录仍只有手写 HTTP 验证，不表示真实识图或教学正确。

### 第四轮实际结果与内容检查

本地时间 2026-09-17（供应商请求记录 UTC 为 2026-09-16 22:07），提交 `946ac9c320d235c1a6e1404cdca75d0589bedb0f`，计划 `da69f49a33bf3c3b454d1145a7935f5bb03a526ef39248c9212221c0052f58f3`，只发送 b01 一次，无重试或回退。模型请求与返回标识均为 deepseek-flash，HTTP 200，指定返回函数为 return_math_analysis，结构及引用校验通过。

- 答案 13/20 与精确分数运算 `2/5 + 1/4` 一致。
- 两条引用分别对应学生通分及加法原文，均标 correct；整体判 partial 的理由是题目要求说明通分原因，而学生只写了计算。
- diagnosis 为空，没有为正确计算编造错因；下一步请学生补写通分理由。
- 耗时 3,397.9 毫秒，输入 2,467、输出 796，共 3,263 token。实际账单未知。
- 请求结束时保存 0、拒绝 0，仍待用户决定；未确认不创建 Notebook，未标记掌握。检查表未填分。再次运行预检显示累计 1/1，没有可发请求，不读取密钥。

内容核对后，用户明确选择接受保存。b01 已写入独立验证错题本；新的 Python 进程读取到同一份原始模型结果，版本为 1。再次确认后，存档与该响应账本的字节均未改变；没有新增调用，没有自评或订正记录，未自动宣布掌握。日常 890 个私人数据文件及前三轮报告保持不变。四轮累计确认保存 1 条；真实再次订正尚未完成。

这是 Codex 辅助内容核对及确定性运算检查，不是教师或独立人工评分。一次通过不能证明失败率降低或全部数学内容正确；当时 b02 的正确约分误判、b03 的仅答案诊断边界、真实订正和图片质量仍待验收。没有用此条替换之前的失败。

| 轮次 | 尝试 | 结构通过 | 失败 | 本轮计划中未发送 |
|---|---:|---:|---:|---:|
| 初始 json_object | 3 | 2 | 1 | 4 |
| 层级提示修订 | 1 | 0 | 1 | 6 |
| 完整格式样例 | 1 | 0 | 1 | 2 |
| strict 单条分析 | 1 | 1 | 0 | 0 |

合计 6 次尝试、3 条结构通过、3 条失败，输入 8,130、输出 3,891 token；各轮有重复题目，未发送数也不是独立未测题数。原始回复及用户决定留在本机，仓库仅记录这一脱敏摘要。

### 第五份计划：b02 通过，b06 的纠错声明失败

本地时间 2026-09-17，提交 `c46756b175048b527294c93e13643c05dd696f8c`，计划 `eab41e4ec57ee2e967641a0025dd7292a745e47475d7a87828e30c0cf48a49e6`，累计上限 2 次。先调用 b02，用户明确确认保存后，才调用 b06；旧分析来自实际存档，没有使用手写分析代替。

- b02：答案 5/6 正确，第一步相加判错，后续 3/9 = 1/3 判对；首轮约分误判在这次未出现。对照栏的参考步骤不够贴切已记录，原文保留。用户已确认保存、重新读取一致，重复确认未改文件。耗时 3,225.14 毫秒，输入 2,452、输出 779 token。
- b06：HTTP 200，strict 函数及 JSON 字段有效，当前解答 5/6 和当前两步判断均正确。但 changes 将旧步骤 3/9 = 1/3 标为 corrected，而 previous_analysis 中该步是 correct；不能因为最后答案改变，就把旧的合法约分说成错误已修正。应用证据检查拒绝整条结果，无可保存订正。耗时 3,734.03 毫秒，输入 3,748、输出 854 token。
- 旧 b06 仍标记 failed，原错误细项为 null，不覆盖旧记录。b02 存档仍为版本 1，没有 corrections 或自评变更。这份计划已用完 2/2 次且有失败，不能续跑或删除后自动重试。

五份计划共 8 次尝试、4 条应用校验通过、4 条失败、2 条确认保存，输入 14,330、输出 5,524 token；未人工评分。前四份的失败为 JSON/字段问题，第五份失败为前后证据矛盾，不能统称为接口不可用。当前尚未完成真实订正—确认保存—重启恢复，B 阶段继续保留未通过状态。

### 本轮修订：先限定纠错声明的证据范围

本轮保留原有拒绝规则，不把 corrected 自动改成 changed，也不删除错误项使原报告通过。`CorrectionValidationError` 新增 `correction_previous_not_incorrect`、`correction_current_verdict_mismatch`、`correction_steps_unavailable`，今后的审计可明确区分失败位置。

`correction_change_schema` 从当前实际保存的 previous_analysis 生成 strict 订正 schema：用 anyOf 分开两种对象。corrected/still_incorrect 分支的 previous_excerpt 只能从旧分析判为 incorrect 的完整引用中选择；changed/uncertain 分支只描述变化。旧步骤全部正确或不确定时不开放纠错声明；不生成空 enum。它不读取参考答案，也不把整体错答转换成每一步都错。当前步骤是否正确及引用是否属实继续由原应用检查，格式约束不能证明数学正确。

[官方 strict 文档](https://api-docs.deepseek.com/zh-cn/guides/tool_calls/)列出 anyOf 与 enum 支持；这份新增的动态 schema 尚未发送真实请求，其供应商接受情况和质量影响仍未知。订正 prompt 升为 photo-correction-v5，明确“旧步骤正确但中间量来自前错”不能声称已订正；strict 协议升为 study-strict-output-v2，分析 prompt 仍为 photo-study-v5。

新增手写测试复现 b06 的错误形状：服务端 schema 拒绝、本地仍拒绝、固定错误码、旧存档不变、失败后不重试；同时验证真正错误步骤的订正、正常 changed、无错误旧步骤、仅答案/旧版分析和当前判断不匹配等边界。没有修改原模型回复，没有新增真实调用或下一付费计划。

- `study/service.py`：`analysis_context` / `build_payload` 让预览与发送共用输入；可选审计先登记后发送，普通页面不强制新增日志。
- `study/run_audit.py`：原子日志、运行锁、尝试记录、去重与未知状态；业务存档和审计日志分开。
- `study/smoke.py`：冻结计划、真实执行门禁、依赖检查、保存确认与重新读取。
- `tools/verify_study_live.py`：用户入口；本机模式和真实模式显式分开。
- `test_study_smoke.py`：针对新调用链的边界测试；旧页面、协议和持久化继续跑全量回归。
- `test_structured_output.py`：严格返回格式、本机七步流程、重复键、HTTP 拒绝与无回退、确认及恢复的回归。

下一验收是：另行批准预算后，完成一组非预设文字题的真实分析—确认保存—再次订正—重新打开，检查真实输出并记录失败。随后再验证图片原始转录、人工修正和分析。本轮尚未验收真实文字质量、照片质量或模型选工具能力。

### 第六份计划：真实回复正确，跨步骤引用被校验器误拒绝

提交 `95810c9`，单条计划 `ca64b681527614757524ebe3fb3c0380935eea725bca3fe961b5f53e2bce8309`，只复用已确认 b02 后请求 b06 一次。HTTP 200，用量输入 4099、输出 807 token，4382.28 毫秒；返回符合动态 strict schema。当前 5/6、通分和相加判断均正确，summary 明确认可旧约分本身正确，没有重复第五份计划的错误。

失败原因：changes.current_excerpt 同时引用两条当前正确步骤，而旧 excerpt_has_verdict 只判断“整段引用是否位于某一条 comparison 内”。两条分开检查都正确，合起来就被误判为 correction_current_verdict_mismatch。这是本地证据覆盖算法的错误拒绝，不能记为模型数学错误。

修复为按已核对原文的位置检查覆盖：忽略空白，但不改运算符、数字、顺序；每个引用字符都须由对应 verdict 的 comparison 覆盖。只覆盖其中一步、夹带未分析内容或有冲突/不确定判断时仍拒绝。旧正确步骤不得声明 corrected/still_incorrect 的规则保留。证据版本为 correction-evidence-v2，不改提示词或服务端 schema，不自动切分/修改模型回复。

原始六份账本累计 9 次调用、4 条 reply_valid、5 条 failed、2 条分析确认保存；输入 18,429、输出 6,331 token。此次误拒绝保持 failed，另记原文离线复核通过 1 条，不能用复核覆盖原失败或计作新请求。未新增付费调用，数学核对为 Codex 辅助检查，非教师评分。当前没有新订正保存。

### 已返回回复的离线复核与确认

```bash
python tools/revalidate_correction.py preview --source evaluation_runs/failed-single-correction \
  --row b06 --output evaluation_runs/revalidated-correction
python tools/revalidate_correction.py status --directory evaluation_runs/revalidated-correction
# 阅读原文后，由用户明确选择 accept 或 reject：
python tools/revalidate_correction.py accept --directory evaluation_runs/revalidated-correction
```

此入口没有密钥或网络请求参数。只支持本次已完整返回、strict 格式可核对、因当前步骤证据检查失败的单条复用订正。HTTP/JSON 错误、可能截断、来源变化、仍不符合 schema 或新版证据检查的回复不生成候选。不恢复供应商内部推理，不读取未记录的内容。

`candidate.json` 记录原失败和原文字节哈希、来源计划及父存档、新版校验代码、原样解析结果和复核编号；`decision.json` 单独记录用户决定。原审计目录始终只读。复核候选从本次复核起 24 小时有效；代码、原文或父存档变化即失效。确认时再次检查并使用原子写入保存，重复确认和确认日志写入中断的恢复不会追加第二条订正。未确认/拒绝不创建 notebook；保存不改变自评或宣布掌握。

`study/revalidation.py` 负责来源校验、候选冻结和确认；`tools/revalidate_correction.py` 提供明确标为 offline_revalidation 的入口；`test_revalidation.py` 使用本机手写响应检查确认、过期、篡改、保存失败、新进程恢复等边界。真实原文离线复核通过后，仍须用户确认保存才能完成持久化验收。

### 用户已确认：最小文字订正持久化验收完成

2026-09-17，用户明确“接受并保存”。在冻结提交 `60cd29e044a429b60cc78b19405cd6c51c3247ab` 上，复核候选被接受，原样真实回复写入独立复核错题本。保存记录为版本 2，包含原分析副本和 1 条以 original 为对照的订正，结果为 5/6；原作答、原分析、自评与用户分类均保留，自评为空，不标记掌握。

已实际另起 Python 进程读取磁盘，并与保存内容逐项比较一致；又在另一进程重复接受同一候选，错题 JSON 和决定日志字节均不变，没有重复订正。49 份原运行文件、890 份私人文件哈希未变；原 b02 保持版本 1，原 b06 仍为 failed，复核候选原文不变。保存阶段新增调用为 0。

这完成第 1 项的真实分析—用户确认—真实订正回复—离线复核—确认保存—新进程恢复链路。原始调用统计仍为 9 次、4 条当时通过、5 条失败、2 条原分析保存，另记离线复核后保存订正 1 条。不是重新请求模型后得到的成功，也不是教师评分或独立质量样本。第 2 项真实照片与后续 Agent、上下文和评估未在本轮开展。

代码及 404 项全量测试对应上述冻结提交；本次实际保存与恢复检查单独记录，不累加成新的测试用例数。原始回复、用户决定和保存验证详情仅保存在本机，仓库只保留本段脱敏摘要。
