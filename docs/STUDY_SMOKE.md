# 当前主流程的文字验证（B 阶段准备）

本入口复用页面使用的 `StudyService`、分析/订正校验和 `Notebook`，验证任意文字输入的调用与保存链路。审计账本目前在这个验证入口启用，普通页面维持原有行为。工具循环、跨题历史与教学设置接入仍是后续阶段。

默认只预览，不需要密钥，不调用模型。所有输出留在本机，使用新目录；验证存档在报告目录的 `notebook/`，不修改日常错题本。

## 先预览或演练

在仓库根目录、装好锁定依赖的 Python 环境执行：

```bash
python tools/verify_study_live.py preview --output evaluation_runs/study-preview-001
python tools/verify_study_live.py local --output evaluation_runs/study-local-001
python tools/verify_study_live.py status --directory evaluation_runs/study-local-001
python -B -m unittest test_study_smoke -v
python -B -m unittest discover -v
```

目录已存在时不会覆盖；换一个新的名字。`local` 的全部回复和用量都是手写 HTTP 数据，自动保存决定标为 `local_fixture`，真实请求为 0。它不能作为解题准确率或模型自主行为的证据。

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

本次开发没有执行以下真实命令。运行前应核对官方当前模型、接口和单价，审阅 `manifest.json`，另行确认最多请求数和可接受费用。费用目前没有估算；未知不等于免费。

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

HTTP 错误正文不记录。合法回复只保存最终 content 的结构化内容；非法 JSON 可以保留脱敏、限长诊断文本；凭证回显整条拒绝。供应商 `reasoning_content` 不展示或保存。

如果进程在请求期间中断、运行记录写入失败或服务报错，会保留 `running` 或 `failed`。再次运行会停止；先查明这次尝试，不能删除记录来自动重试。要再尝试必须另建计划、重新确认预算，并在人工记录中保留原失败。已有私人数据和错误记录不会被覆盖。

## 关键代码与下一验收

- `study/service.py`：`analysis_context` / `build_payload` 让预览与发送共用输入；可选审计先登记后发送，普通页面不强制新增日志。
- `study/run_audit.py`：原子日志、运行锁、尝试记录、去重与未知状态；业务存档和审计日志分开。
- `study/smoke.py`：冻结计划、真实执行门禁、依赖检查、保存确认与重新读取。
- `tools/verify_study_live.py`：用户入口；本机模式和真实模式显式分开。
- `test_study_smoke.py`：针对新调用链的边界测试；旧页面、协议和持久化继续跑全量回归。

下一验收是：另行批准预算后，完成一组非预设文字题的真实分析—确认保存—再次订正—重新打开，检查真实输出并记录失败。随后再验证图片原始转录、人工修正和分析。本轮尚未验收真实文字质量、照片质量或模型选工具能力。
