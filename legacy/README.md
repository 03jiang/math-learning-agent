# 早期四题学习原型

这里保留最早的分数与应用题版本，包括设置确认、步骤检查、固定题库和旧评估入口。新版拍照、错题本、上下文及有限工具循环在 `study/`，根目录 `app.py` 只启动新版页面。

部分底层函数仍由新版复用：`model_api.py` 的配置与错误类型、`http_worker.py` 的请求进程、`model_boundary.py` 的严格 JSON 解析、`step_check.py` 的受限算式解析、`retrieval.py` 的本地笔记查询、`core.py` 的设置选项。导入显式使用 `legacy.*`，不通过模块别名掩盖依赖。

在仓库根目录运行旧示例：

```bash
python -m legacy.demo
python -m legacy.demo_reading
python -m legacy.demo_steps
python -m streamlit run legacy/app.py --server.address 127.0.0.1
```

旧版配套数据仍保留在 `prompts/`、`examples/` 和 `evaluation/` 的早期题集文件中。`evaluation/agent_ab_v1/` 属于新版 Agent 对照评估。全部回归测试放在 `tests/`，运行 `python -B -m unittest discover -v` 会一起执行。
