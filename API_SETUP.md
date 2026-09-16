# 模型配置与评估入口

无需模型先运行 `python -m study.launch --demo`，见 [README](README.md)。固定回放不执行 OCR，当前真实照片效果尚未验收。

## 普通照片模式

运行 `python -m study.launch`，在侧栏隐藏输入 DeepSeek API 密钥，模型名称可调整为账号当前支持的模型。默认示例配置见 [model_config.deepseek.example.json](model_config.deepseek.example.json)。不要把密钥写进 JSON、聊天、截图或 Git 提交。

本项目使用 Chat Completions：`base_url` 为官方基础地址，程序追加 `/chat/completions`。识图和分析分别由按钮触发，发送本题相关文字与照片；可能计费。超时或失败不自动重试，用户明确清除失败记录后才可再次请求。普通存档位于 `data/study/`，与离线演示分开。

真实识图应先使用自写题小范围验证，再检查学生原文归属、数学内容和诊断依据。字段校验成功不能替代教师评分。

## 原四题模块的评估

默认仅生成请求预览，零 API 调用。报告必须使用新目录；已有输出不会被静默覆盖。

```bash
python evaluate_teaching.py --suite smoke --output evaluation_runs/preview-new
python evaluate_teaching.py --suite revision --local-http --output evaluation_runs/local-new
python evaluate_teaching.py --help
```

真实评估须显式 `--live`，提供 API 模式配置与当前进程环境密钥；先通过四题 smoke 再考虑批量。通过终端的隐藏输入或其他明确的会话方式设置 `DEEPSEEK_API_KEY`，不要把值直接写进会留历史的命令。

```bash
python evaluate_teaching.py --suite smoke --live --config model_config.deepseek.example.json --output evaluation_runs/live-smoke-new
```

本页不自动执行真实调用。完整参数、请求冻结和续跑限制见 `--help` 以及 [评估计划](evaluation/PLAN.md)。批量是原四题教学协议，不是照片 OCR 批量工具。

评分用 [RUBRIC](evaluation/RUBRIC.md)，人工填分后才能汇总。空白不算 0 分，也不代表通过；当前人工评分暂缓。
