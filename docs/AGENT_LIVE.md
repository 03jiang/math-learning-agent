# Agent 工具选择：真实联调与首次失败

> 本页保留该阶段的操作与复盘；当前汇总见 [评估状态](EVALUATION_STATUS.md)。阶段描述不代表最新整体进度。

本入口复用主页面的 `AgentStudyService`，冻结输入、模型配置、首轮请求、资料文件和调用上限。2026-09-24 首次真实计划实际发送2次请求：直接回答完成，查笔记场景在工具请求解析处失败，其余3项未执行，工具实际执行0次。已补充离线兼容和诊断，**真实工具往返尚未验收**，见[失败复核与修复](C_FAILURE_REVIEW.md)。所有题目、原作答和历史记录均为自写样例，不读取日常错题本或用户照片。

## 本轮范围

| 编号 | 场景 | 观察什么 |
|---|---|---|
| b01 | 一元一次方程，等价正确步骤 | 是否可直接回答；也如实记录模型额外查资料的选择 |
| b02 | 分数错算，请参考课程笔记 | 是否查询、返回哪些来源，引用能否支持讲解 |
| b03 | 同类分数题，独立笔记目录为空 | 是否得到无结果，是否凭空编造来源 |
| b04 | 同类分数题，允许查询自写历史样例 | 是否按需查询，是否区分旧作答与当前证据 |
| b05 | 同类分数题，笔记含一份损坏 JSON | 真实模型选择查询后，程序是否失败停止 |

这是 **5 个执行场景、2 道题目母版**，不是 5 道独立数学题、保留集、学生实验或策略效果对照。第五项是预先说明的本机故障注入，不是供应商故障。模型始终使用 `tool_choice=auto` 自己选择工具，不在真实模式硬塞手写 tool_calls。未覆盖的分支如实记为 `not_exercised`，不重发来凑覆盖率。

每个场景至多 2 次模型请求、3 次只读工具请求，总共最多 10 次模型请求。工具累计仍限 3 秒，模型单次最多 60 秒、每场景最多 180 秒。第二次请求禁用更多工具，模型仍索取工具时停止。任何格式、权限、来源、HTTP 或工具失败均停下剩余场景；失败与中断不能直接续跑或跳过。

使用 `deepseek-flash`、非思考模式、Chat Completions、JSON 最终正文，输出上限 4096 token；每个请求 UTF-8 JSON 限 26,000 字节，超出即停止，不静默截断题目。结合官方 [工具调用](https://api-docs.deepseek.com/guides/tool_calls/) 格式实现；真实兼容性仍待本轮确认。

## 费用说明

2026-09-17 核对的 [官方价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)：Flash 高峰输入缓存未命中 2 元/百万 token、输出 8 元/百万 token。本轮建议独立预留 1 元，不复用旧计划的批准。

保守粗估为 `10 × ((26000 + 1000) × 2 + 4096 × 8) / 1000000 = 0.86768 元`：将 UTF-8 请求字节近似当作输入 token，并另留 1000 token 协议余量，输出按上限计算，忽略缓存折扣。这不是实际 tokenizer 计数或供应商金额封顶；实际用量、执行次数与价格决定账单。未知费用保持空值，不能把测试估算当作已支付费用。

阶段 D 之后代码已变化；在提交 `4e07742` 创建、尚未执行的旧计划已失效，保留原样。真实调用前需按最终提交重新冻结并确认，不能直接运行旧启动脚本。

## 零请求预览与本机演练

在当前版本的仓库根目录执行；真实预览应在代码提交、工作区干净后创建。输出目录必须不存在。

```bash
python -B -m tools.verify_agent_live preview --output evaluation_runs/agent-live-plan
python -B -m tools.verify_agent_live local --output evaluation_runs/agent-live-local
python -B -m tools.verify_agent_live status --directory evaluation_runs/agent-live-plan
```

预览生成 `PLAN.md`、`manifest.json`、`inputs/`、`REPORT.md` 与 `status.json`，不读取密钥、不调用模型。首轮请求在 manifest 中逐字保留，人工核对参考另存，不能进入模型请求。每轮调用共享同一累计名额；真实代码、输入或资料变化后旧计划失效。

本机演练通常为 8 次 HTTP 手写响应：1 次直接回答，3 组“工具请求＋最终回复”，最后 1 次触发故障。第五项 `failed` 且 `injected_tool_failure_observed` 是预期停机；其余四项应完成。演练响应不能冒充真实模型决策。

## 批准后才运行

先查看 `PLAN.md` 并确认输入、最多 10 次请求与 1 元预留预算。执行时必须提供完整的预览计划编号，不能改模型或调高上限：

```bash
python -B -m tools.verify_agent_live run \
  --directory evaluation_runs/agent-live-plan \
  --confirm-plan '替换为完整计划编号' \
  --max-model-requests 10 --budget-cny 1
```

仅在本机交互终端隐藏输入密钥；不读旧环境密钥、不收命令行密钥参数、不从聊天取密钥。输入密钥前检查代码、范围和已有运行状态；失败/未完成的计划不再询问密钥。程序不会主动创建新计划重试。

返回结果后先检查 `REPORT.md`、`responses/bNN.json` 与对应 `agent-runs/*.json`：请求次数是实际预留的 HTTP 名额；超时或中断可能已计费，会保留“完成情况未知”。诊断保留限长、脱敏的对外正文；Agent v2 另记录工具请求的字段形状、ID、名称和限长原参数，不记录未知字段值或内部 `reasoning_content`。执行记录是私人数据，按白名单导出时排除。

行为分支被观察到不代表调用合理、引用支持结论或数学正确；真实模型未选工具时不能把该项勾选为完成。状态查看只读，不修改日志、不续发请求。

## 看过回复后再确认收藏

没有自动收藏或人工评分。只有用户明确接受/拒绝后才执行对应命令，保存到本计划独立的 `notebook/`，与作为工具资料的 `inputs/*/history/` 和日常错题本分开。

```bash
python -B -m tools.verify_agent_live decide --directory evaluation_runs/agent-live-plan --row b01 --accept
python -B -m tools.verify_agent_live decide --directory evaluation_runs/agent-live-plan --row b02 --reject
```

接受前验证原候选、24 小时有效期、代码和来源；失败、过期、被改写或被拒绝的候选不能写入。重复确认不追加记录，重启可恢复，不写掌握标记。保存和决定日志是两个文件事务：若保存成功后日志失败，可再次明确确认以恢复日志，题目不会重复创建。

关键代码：`study/agent_live.py` 的 `CountedTransport.send()` 在真实发送前登记名额，`execute()` 持锁并遇错停止；`decide()` 复用 Agent 候选检查与 Notebook 保存。`tools/verify_agent_live.py` 先核对计划，再请求隐藏输入。`test_agent_live.py` 覆盖预算、并发、崩溃未知状态、资料变化、无自动重试、诊断脱敏和确认恢复。
