# 提示词对照评估计划 v1

比较普通教学提示词 `prompts/baseline.txt` 与协议教学提示词 `prompts/protocol.txt`。基线同样要求准确、适龄、关注学生错误和帮助偏好；两版共享 `output_contract.txt`，都走同样的本地检查、只读笔记和状态确认边界。这是教学提示词版本比较，不是“本系统 vs 无约束聊天”的总体性能比较。

## 固定用例

`teaching_cases.jsonl` 与 `evaluation_cases.py` 的人工定义逐条一致，否则程序拒绝运行。不是模型生成的数据集，没有真实学生信息。

| 维度 | 取值 |
|---|---|
| 题目 | 两道分数加法、彩带剩余、蛋糕剩余，共四题 |
| 设置 | 简洁小提示；中等步骤 + 详细示例 + 分项引导 + 方法提示；完整直接讲解 + 一个知识联系 |
| 输入 | 不知如何开始；错答；答案正确但等值变形错误；应用题读题卡错选 / 加法题无法自动验证的文字理由 |

共 4 × 3 × 4 = **48 条**。前三种情景为开发集 36 条，第四种为保留集 12 条；同题同情景的三组设置不跨集。开发集应用题错答也带“整体”错选；保留集应用题带第二、第三问错选。另有四条 starting/default 的 smoke 子集，每题只请求协议版一次，作为接口冒烟检查，不能当独立质量样本。

每条分别运行 baseline/protocol，默认一次，即 **96 行**；支持 `--repeats 2` 或 `3`，相应增大明确的请求上限。重复运行仍是同题样本，不当成独立学生观察。每次各用一个全新的初始任务状态；同对输入、设置、来源、模型、温度、thinking 和 token 上限一致，轮换两版执行先后次序。每条请求记录哈希，与真正发出的请求一致性另行检查。

## 不接模型先演练

以下命令在项目根目录执行：

```bash
../../work/venv-stage2/bin/python evaluate_teaching.py --output evaluation_runs/preview-v1
../../work/venv-stage2/bin/python evaluate_teaching.py --suite smoke --local-http --output evaluation_runs/smoke-local-v1
../../work/venv-stage2/bin/python evaluate_teaching.py --local-http --output evaluation_runs/local-v1
../../work/venv-stage2/bin/python evaluation_scoring.py --output evaluation_runs/local-v1
```

第一条零 HTTP；后两条实际访问 127.0.0.1 的手写服务，分别 4 和 96 次。它们验证传输、协议、报告和评分流程，**不评价模型教学质量**。演练评分表保留空白；单元测试中的人工分数只写临时目录。

## 报告文件

- `manifest.json`：模式、配置、完整用例计划、代码/提示词/笔记/用例/评分标准哈希和创建时间。
- `requests/rNNN.json`：实际教学请求及 Chat Completions 请求体；不包含密钥。
- `responses/rNNN.json`：运行状态、校验后的教学回复、来源、本地检查、耗时、用量和实际请求次数；失败的原始模型正文不保存。
- `scores.csv`：每份回复一行，五项 0/1/2 人工分、评分人和备注；只有适用项可评分。
- `review.md`：逐行链接、用例名和该条检查重点。
- `run_status.json`：成功、失败、未知、待运行数量；与教学分数分开。

评估使用临时工作区，不读取日常学生存档、不确认建议。`evaluation_runs/` 不进入 Git；可根据冻结版本重建，人工评分和真实结果需要自行保留。源码、提示词、固定用例、标准与小型验收记录进入 Git。

## 恢复与评分

每次调用前先原子写 `running`。进程中断后，这行是否完成可能未知；`--resume` 仅请求没有结果文件的行，绝不自动重试失败或 running 行。已有报告不能覆盖；代码/用例/提示词/配置变化拒绝续跑；同目录的并发运行被锁拒绝。

真实执行步骤见 [API_SETUP](../API_SETUP.md)，人工标准见 [RUBRIC](RUBRIC.md)。`evaluation_scoring.py` 不使用模型评分；只按人填的有效数字汇总，空分不是零分，失败不能填教学分，只有完整配对进入均分差。

开发集可用于诊断和修改。改 prompt 后新建报告并重新冻结，再跑保留集。看过保留回复后再调整 prompt，不得把同组继续称作未见过的验证集。报告需披露失败率、评分覆盖率、自评限制和同四道题的小样本限制；未安排真实学习效果实验。
