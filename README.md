# 数学错题助手

把题目、自己的解题过程和订正记录放在一起，方便以后复习。

你可以上传题目和作答照片，也可以直接输入文字。先核对识别出来的内容，再对照参考解法，看看可能错在哪里。值得复习的题目，由你确认后存进错题本；重新做一遍时，可以比较前后的变化，保留每次订正。

这是一个用 Python 和 Streamlit 做的本地原型，使用 Codex 辅助开发。目前提供离线演示和 DeepSeek 接口配置，真实照片的识别、解题质量和学习效果还没有验证。

## 可以做什么

- **整理题目。** 题目和作答可以分开上传，支持旋转、裁剪，也可以直接输入文字。
- **对照解题过程。** 查看参考步骤、自己的原作答、可能的错因和练习建议。只有最终答案时，不猜测中间哪里做错了；缺少条件时，先补充题目。
- **保存和复习。** 确认后加入错题本，补充笔记，再次作答并选择是否保存订正。可以按知识点回顾、导出记录，误删的题目可以恢复。

模型的分析只是建议。上传照片、查看分析不会自动收藏；一次答对也不会自动被记为“已经掌握”。

## 先试试演示版

不需要 API 密钥，也不会产生模型调用费用。

```bash
git clone https://github.com/03jiang/math-learning-agent.git
cd math-learning-agent
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-lock.txt
python -m study.launch --demo
```

打开终端显示的本地地址，默认是 [127.0.0.1:8518](http://127.0.0.1:8518/)。端口被占用时，可以在启动命令后加 `--port 8520`。

点击“载入演示题”，核对题目后分析，再点“确认加入错题本”。进入错题本，点击“填入演示订正”，就能试一遍分析、放弃或保存订正的过程。具体操作见[演示说明](docs/DEMO.md)。

**演示版使用一道人工作答样例和预先写好的回复，不做真实识图，也不调用模型。** 改成其他题目后，不会继续给出这道示例题的分析。演示记录单独保存在 `data/demo-study/`。

接入真实模型时，运行 `python -m study.launch`，在侧栏填写密钥。只有点击识图、分析或订正分析时，才会发送相关题目内容；请求可能计费，失败后不自动重试。配置方法见 [API_SETUP.md](API_SETUP.md)。

发布记录中的本地测试环境是 macOS / Python 3.12。Linux 测试结果见仓库的 Actions 页面；当前文件锁实现不支持 Windows 原生运行。

## 项目说明

| 想了解什么 | 从这里看 |
|---|---|
| 为什么做、功能怎么取舍 | [设计说明](docs/PRODUCT.md) |
| 怎么演示主要功能 | [五分钟演示](docs/DEMO.md) |
| 代码怎么组织、数据怎么保存 | [技术说明](docs/ARCHITECTURE.md) |
| 哪些测过、哪些还没有验证 | [测试与待办](docs/STATUS.md) |
| 怎么介绍这个项目 | [项目介绍与面试准备](portfolio/APPLICATION_PACK.md) |

## 开发和测试

```bash
python -m unittest discover -v
```

这些测试使用临时文件、合成图片和本机模拟的 HTTP 回复，不需要模型密钥，也不向真实模型发送请求。图片测试需要中文字体：macOS 使用 STHeiti，Ubuntu 安装 `fonts-noto-cjk`。CI 使用 `requirements-lock.txt` 安装依赖后运行测试。

主要代码在 `study/`：图片处理、模型回复检查、错题保存、订正和页面都在这里。`test_*.py` 是相关测试。`evaluation/photo_cases_v1/` 中有 13 张程序生成的自写题图片，不是真实学生照片，目前还没有人工评分。`evaluation/` 和 `prompts/` 还保留了早期四道分数题的评估代码，它们不代表当前照片功能的测试结果。

每道题用一个 JSON 文件保存原作答、分析来源和订正历史。写入时会检查记录版本，防止重复保存或用旧分析覆盖新记录。密钥、个人照片和错题记录不随源码公开；公开图片样例均为自写合成内容。

## 目前的限制

这是单机、单用户原型，不是已经上线的教学产品，没有账号系统或真实学生使用数据。程序按预先写好的流程调用模型，并不是能自主行动的多 Agent 系统。测试可以检查保存、确认和异常处理是否正常，不能证明所有题目都能讲对，更不能证明学习成绩会提高。详细记录见[测试与待办](docs/STATUS.md)。

**English:** A local app for reviewing math mistakes. Add a problem and your work, check the text, compare the steps, and choose what to save. It keeps correction history for later review. Built with Python, Streamlit, and Codex assistance. The offline demo uses prepared examples; live photo analysis and learning outcomes have not been evaluated.
