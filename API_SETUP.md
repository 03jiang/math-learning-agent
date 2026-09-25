# 模型配置与评估入口

无需模型先运行 `python -m study.launch --demo`，见 [README](README.md)。固定回放不执行 OCR，当前真实照片效果尚未验收。

## 普通照片模式

运行 `python -m study.launch`，在侧栏隐藏输入 DeepSeek API 密钥，模型名称可调整为账号当前支持的模型。默认示例配置见 [model_config.deepseek.example.json](model_config.deepseek.example.json)。不要把密钥写进 JSON、聊天、截图或 Git 提交。

本项目使用 Chat Completions：`base_url` 为官方基础地址，程序追加 `/chat/completions`。识图和分析分别由按钮触发，发送本题相关文字与照片；可能计费。超时或失败不自动重试，用户明确清除失败记录后才可再次请求。普通存档位于 `data/study/`，与离线演示分开。

真实识图应先使用自写题小范围验证，再检查学生原文归属、数学内容和诊断依据。字段校验成功不能替代教师评分。

可选 `MATH_STUDY_OUTPUT_MODE=strict_tool python -m study.launch` 使用官方 Beta 地址和严格函数参数格式；默认 `json_object`。设置错误或 Beta 不支持时会停止，不回退后重新收费。它只返回待核对结果，不执行函数、不自动保存。此通道已通过 b01、b02 两条真实文字分析；首次真实订正收到了结构有效的返回，但因前后证据矛盾未通过应用校验。新订正约束、照片及完整订正流程仍未验收。先阅读[文字验证记录](docs/STUDY_SMOKE.md)，任何新增付费验证都需单独冻结范围、确认次数与预算，不能重复运行已经失败或用完名额的计划。

## 原四题模块的评估

当前照片主流程的**文字**请求预览、本机 HTTP 演练和确认保存验证，见 [文字验证说明](docs/STUDY_SMOKE.md)。默认零真实请求；共 5 次分析与 2 次依赖已确认结果的订正，不使用旧四题的参考答案上下文。

默认仅生成请求预览，零 API 调用。报告必须使用新目录；已有输出不会被静默覆盖。

```bash
python -m legacy.evaluate_teaching --suite smoke --output evaluation_runs/preview-new
python -m legacy.evaluate_teaching --suite revision --local-http --output evaluation_runs/local-new
python -m legacy.evaluate_teaching --help
```

真实评估须显式 `--live`，提供 API 模式配置与当前进程环境密钥；先通过四题 smoke 再考虑批量。通过终端的隐藏输入或其他明确的会话方式设置 `DEEPSEEK_API_KEY`，不要把值直接写进会留历史的命令。

```bash
python -m legacy.evaluate_teaching --suite smoke --live --max-requests 4 --config model_config.deepseek.example.json --output evaluation_runs/live-smoke-new
```

本页不自动执行真实调用。完整参数、请求冻结和续跑限制见 `--help` 以及 [评估计划](evaluation/PLAN.md)。批量是原四题教学协议，不是照片 OCR 批量工具。

评分用 [RUBRIC](evaluation/RUBRIC.md)，人工填分后才能汇总。空白不算 0 分，也不代表通过；当前人工评分暂缓。
