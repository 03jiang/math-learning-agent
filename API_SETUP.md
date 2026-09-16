# 连接模型和运行评估

只想先看看怎么用，运行 `python -m study.launch --demo` 即可，不需要密钥。演示版使用预先写好的题目和回复，不做真实识图。安装步骤见 [README](README.md)。

## 连接 DeepSeek

运行 `python -m study.launch`，展开侧栏的“连接 DeepSeek”，在隐藏输入框里填写 API 密钥。模型名称填写你的账号实际支持的名称，示例配置见 [model_config.deepseek.example.json](model_config.deepseek.example.json)。不要把密钥放进 JSON 文件、聊天、截图或 Git 提交。

程序使用 Chat Completions 接口：配置中的 `base_url` 是基础地址，程序会追加 `/chat/completions`。点击识图或分析按钮后，才会发送当前题目的相关文字和照片。订正分析还会发送此前的作答和分析，请注意这些请求可能计费。

失败或超时后，程序不会自动重试。需要再次尝试时，先点击页面上清除失败记录的按钮，再重新发起请求。普通记录保存在 `data/study/`，和演示记录分开。

**接口已接入代码，不等于照片识别效果已经验证。** 照片输入是否可用还取决于所选模型和接口。请先用自写题做小范围检查，核对题目、学生原作答和分析内容；回复格式正确，也不代表数学答案正确。

## 早期四道分数题的评估工具

下面的命令用于早期四题模块，不是照片识别的批量评估。默认只生成请求预览，不调用真实模型。输出必须放在新目录中，程序不会直接覆盖已有报告。

```bash
python evaluate_teaching.py --suite smoke --output evaluation_runs/preview-new
python evaluate_teaching.py --suite revision --local-http --output evaluation_runs/local-new
python evaluate_teaching.py --help
```

`--local-http` 使用本机模拟回复。只有明确添加 `--live`，并提供 API 配置和当前进程的密钥，才会调用真实模型。先完成四题小范围检查，再考虑扩大调用数量。

通过终端隐藏输入或其他明确的会话方式设置 `DEEPSEEK_API_KEY`，不要把密钥直接写进会保留历史的命令。

```bash
python evaluate_teaching.py --suite smoke --live --config model_config.deepseek.example.json --output evaluation_runs/live-smoke-new
```

完整参数和继续运行已有评估的限制，见 `--help` 与[评估计划](evaluation/PLAN.md)。本文只是操作说明，不会执行这些请求。

评分标准见 [RUBRIC](evaluation/RUBRIC.md)。人工填分后才能汇总；空白表示还没评分，不是 0 分，也不是通过。目前人工评分尚未完成。
