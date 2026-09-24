# 照片流程：识图原文与人工核对

**最新进展（2026-09-24）：** 两张合成图复用已确认OCR完成新版真实分析，用户确认后分别收藏；原图、原始转录、核对记录与分析在新进程恢复一致，重复确认不改存档或日志。两条没有新增订正、自评或掌握标记。局部错步与仅答案两项重点表现符合本次预期，参考步骤配对仍有问题，人工评分继续为空。见[复测与保存记录](B_RETEST.md)。

以下介绍共享流程，并保留9月17日首次运行及其当时的缺陷；旧回复未被替换为新版结果。

已补齐收藏后的可追溯性：原始识图、人工核对文字和最终分析分别保存。照片来源用规范化图片 SHA-256 关联；识图来源区分真实接口、本机 HTTP 与离线演示。核对记录列出题目、学生作答、作答类型中的修改项。哈希用于检测记录变化，不是对供应商来源的数字签名。

在页面上传 → 识别 → 对照原文并修改 → 勾选核对 → 分析 → 确认加入错题本。识图和编辑不自动收藏；只有最后一步写错题 JSON。换图、移除、裁剪或旋转使旧识图记录失效。重启后可展开“对照原始识别与核对内容”。

带识图记录的错题使用 schema 4；原题、原作答和原始识图保留不改。旧 schema 1–3 仍能读取，不做批量迁移；订正、回收站及恢复保留 schema 4 和识图记录。纯手动录入不伪造 OCR 记录。

## 两张合成图片的首次真实识图计划

`study.photo_smoke` 固定验证 p01_fraction_steps 与 q10_teacher_annotation，只识图，最多两次请求，不自动分析或收藏。前者检查是否保留学生错误计算，后者检查教师红色批注是否混入学生答案。均为程序排版样本，不是真实手写。参考文字和检查表不进入请求；图片上可见内容自然属于识图输入。

```bash
# 零付费：新的目录，运行本机 HTTP 手写响应
python -B -m study.photo_smoke local --directory /tmp/math-photo-local-new

# 零付费：干净 Git 提交后，冻结图片、代码、完整 payload 和配置
python -B -m study.photo_smoke preview --directory /tmp/math-photo-live-new
python -B -m study.photo_smoke check --directory /tmp/math-photo-live-new
python -B -m study.photo_smoke status --directory /tmp/math-photo-live-new
```

真实运行需要用户单独确认完整 plan_id、最多两次和预算说明，然后在本机交互终端运行：

```bash
python -B -m study.photo_smoke run --directory /tmp/math-photo-live-new \
  --confirm-plan 替换为预览中的完整plan_id --max-requests 2 \
  --budget-note 替换为用户已确认的预算说明
```

密钥由 `getpass` 隐藏输入，仅在进程内使用；不能作为命令参数或发进聊天。次数为硬上限，预算说明不保证供应商扣费封顶。先登记 running 再发请求，失败或完成情况未知会停止整个计划；重启不重发已有记录。代码、图片、配置改变时也不能继续旧计划。

`responses/` 保留实际请求、回复和 usage；`b01-observation.json` 等保留页面同格式的识图记录；`review.json` 是单独核对参考，评分为空。识图成功只表示字段和格式有效，不能声称识别正确。成功回复缺少原始识图文件时也会阻止继续，需先检查磁盘写入问题。

## 2026-09-17 实际结果

代码提交 `30633ef` 的两次真实 OCR 已完成，均为 HTTP 200 并通过结构校验。助手逐字段对照自写参考及原图：p01 完整保留错误计算过程，q10 仅转录学生答案、没有混入教师批注；题干和作答类型也一致，未提出文字修改。此处是助手对两张样本的核对，不是教师评分或独立效果评估。

本次输入 2,180、输出 107、合计 2,287 token，累计请求耗时 3,078.30 ms。实际费用未知。本计划两次额度已用完，完成状态再次检查不会读取密钥或发请求；原始报告和人工评分表未变，没有分析、收藏或自动标记掌握。

上述输入与预算后来已获用户确认，两次分析已运行，结果见下文。首次运行时尚未完成真实分析的内容核对、主动收藏与新进程恢复；后续结果见文首。真实手机手写照片仍待提供；13 张合成图片不替代该验证。人工评分继续暂缓；Agent 工具循环现已完成离线实现（见 [说明](AGENT_LOOP.md)），真实决策验收与新版独立评估尚未完成。

DeepSeek 官方[视觉理解文档](https://api-docs.deepseek.com/zh-cn/guides/vision/)说明 `deepseek-flash` 支持 Chat Completions 的用户消息图片。当前入口复用应用配置和 `/chat/completions`，用 `image_url` 传规范化图片，保留 `json_object` 默认返回格式。官方支持不等于本项目真实照片已验收。

## 识图后分析：真实运行完成，内容发现问题

`tools/verify_study_live.py --ocr-from` 只读复用上述两条完整的 OCR 回复，不重复识图；来源模式、请求、原图、回复和观察记录必须相符。新的分析计划冻结来源文件哈希，之后来源变化会阻止发送和保存。本机 OCR 不能被用作真实分析的来源。

先预览两份输入，再用 `confirm-inputs` 记录用户核对。这一步不调用模型、不接受分析、不创建错题本。执行分析仍需单独确认 plan_id、累计请求上限及预算。分析结果的接受或拒绝是第三个决定；接受后，原图、原始 OCR、核对记录和分析一起进入本计划独立的 schema 4 错题本，不改日常数据、不增加自评或掌握状态。

```bash
# 只生成请求预览；目录必须不存在
python -B tools/verify_study_live.py preview --ocr-from /tmp/existing-real-ocr \
  --output /tmp/photo-analysis-new --output-mode strict_tool

# 用户核对两份文字后才执行；零模型请求
python -B tools/verify_study_live.py confirm-inputs --directory /tmp/photo-analysis-new

# 另行确认预算后，在本机终端隐藏输入密钥
python -B tools/verify_study_live.py run --directory /tmp/photo-analysis-new \
  --confirm-plan 替换为完整plan_id --max-requests 2 --budget-note 替换为已确认的预算说明

# 查看真实分析后再决定；重复确认不重复保存
python -B tools/verify_study_live.py decide --directory /tmp/photo-analysis-new --row b01 --accept
python -B tools/verify_study_live.py decide --directory /tmp/photo-analysis-new --row b02 --reject
```

本次已完成的真实分析使用 `strict_tool` 返回格式约束分析字段，thinking 为 disabled，输出最多 4096 token，超时 60 秒。根据[官方 strict 文档](https://api-docs.deepseek.com/zh-cn/guides/tool_calls/)，该格式使用 `/beta/chat/completions`。实际仍是一轮分析请求，不执行模型选出的工具，也没有自动回退。图片和 strict 组合已收到两次真实 HTTP 200，格式通过但内容复核发现问题；不把格式约束说成教学正确性保证。应用页面默认返回模式不变。

本机完整演练可用两个新目录：

```bash
python -B -m study.photo_smoke local --directory /tmp/photo-ocr-local-new
python -B tools/verify_study_live.py local --ocr-from /tmp/photo-ocr-local-new \
  --output /tmp/photo-analysis-local-new --output-mode strict_tool
```

这会使用标明来源的手写 HTTP 响应，模拟核对输入、拒绝第二题、接受第一题、重复确认与重新读取。真实请求数为零；模拟决定与质量评分分开。真实结果、用户照片和原始调用报告不进入源码仓库。

## 内容缺陷与离线修复

两次真实分析合计输入 6,363、输出 1,254、总计 7,617 token，累计请求耗时 6,362.08 ms，实际费用未知。参考答案均为 3/4，但有步骤样本将后续成立的局部运算也标错；只有答案样本在 answer_feedback 中推断计算方法。结构化字段为空不能阻止自由文本中的无依据推断。

两条原始记录保留当时的 reply_valid，助手复核发现的问题单独记录，不补填教师评分；这两条旧候选仍未接受或拒绝；本轮保存的是独立新计划的回复。该计划两次额度已用完，不续跑、不自动新建付费计划。

本轮新增固定答案反馈、受限等式检查及回归测试；详见[分析证据检查与边界](ANALYSIS_EVIDENCE.md)。新版提示词 photo-study-v6 / photo-correction-v6、strict 协议 study-strict-output-v3 尚未真实回归，不能将离线通过记为模型改善。旧候选不会自动改写为正确结果。
