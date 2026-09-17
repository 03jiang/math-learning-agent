# 照片流程：识图原文与人工核对

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

下一验收点是：用户确认这两份识别文字作为输入 → 新分析计划预览与预算确认 → 分析与主动收藏 → 新进程恢复。真实手机手写照片仍待提供；13 张合成图片不替代该验证。人工评分继续暂缓，Agent 工具循环和新版独立评估尚未完成。

DeepSeek 官方[视觉理解文档](https://api-docs.deepseek.com/zh-cn/guides/vision/)说明 `deepseek-flash` 支持 Chat Completions 的用户消息图片。当前入口复用应用配置和 `/chat/completions`，用 `image_url` 传规范化图片，保留 `json_object` 默认返回格式。官方支持不等于本项目真实照片已验收。
