# 项目介绍与面试准备

下面只整理这个项目，不代替完整简历。介绍个人分工时，只写自己实际参与、能够解释的部分。

## 中文简历条目

**数学错题助手｜个人项目，Codex 辅助开发**

- 使用 Python 和 Streamlit，在 Codex 辅助下搭建数学错题整理原型，支持题目与作答上传、文字核对、步骤对照、错题保存和再次订正。
- 将模型分析和保存操作分开：用户确认后才写入错题本，保留原作答与订正历史，支持放弃更新和误删恢复。只有最终答案时，不推测学生的错误过程。
- 准备无需密钥的演示、13 张自写合成题图和自动测试，检查回复格式、页面操作、重复保存及异常处理；整理了后续识图、分析质量和使用体验的评估计划。

需要写测试数量时，引用对应版本的实际结果。不要把软件测试通过数写成教学准确率，也不要添加没有测过的用户量或学习提升。

## English résumé version

**Math Mistake Review App — Personal project, built with Codex assistance**

- Built a local Python and Streamlit prototype with Codex assistance. Users can add a math problem and their work, check the text, compare solution steps, and save corrections for later review.
- Kept AI analysis separate from saving: users choose whether to keep an update, while original work and correction history remain available. Answer-only submissions are not used to guess the student's reasoning errors.
- Prepared a no-key demo, 13 synthetic problem images, and automated checks for response formats, UI actions, duplicate saves, and failures. Planned further evaluation of photo recognition, analysis quality, and usability; these outcomes have not yet been validated.

## 一分钟介绍

“我做的是一个数学错题整理工具。学生把题目和自己的解题过程放进来，核对文字后，可以看参考步骤和自己哪里不一样，再决定要不要保存。

这个项目最早从四道分数题开始，先验证确认和保存，后来加入了照片输入和订正记录。我比较在意的是保留学生原来的过程：如果只写了一个答案，就不能让模型猜他中间怎么算错的。模型给出分析以后，也不能直接替学生改记录。

代码和测试大量使用了 Codex 辅助。现在有可以运行的原型和不用密钥的演示，但真实照片识别、分析质量和学习效果还没验证。接下来先请教师核对结果，再观察学生用起来是否方便。”

## 常见追问

**为什么做这个？有用户调研吗？**

想尝试让学生把自己的解题过程和参考步骤放在一起看，而不只是留一份正确答案。目前是场景假设和原型探索，还没有用户访谈或真实学生数据。

**为什么要先核对识别结果？**

识别错了一个符号，后面的分析就可能跟着错。如果把学生的错答提前改对，也会丢掉原来的过程。不过核对会增加操作，所以还要观察用户愿不愿意做、要花多久。

**模型判断错了怎么办？**

保留原文，把错因当作待核对的建议，允许用户不保存。程序会检查引用和回复格式，但这些检查不能替代教师判断数学内容。

**这个 Agent 能自主做什么？**

目前按程序写好的流程调用模型，不是自主多 Agent 系统。模型不直接改文件，也不能自行判断学生已经学会。这里主要展示模型交互、回复检查和用户确认怎样连在一起。

**怎么判断项目有没有用？**

先看题目有没有识别对、分析有没有找到关键错误，再看用户能否方便地完成订正。至于能不能帮助学生独立做出类似的新题，需要另外设计学习测试，不能用演示回答代替。

**哪些是自己做的，哪些用了 Codex？**

这是人与 Codex 协作完成的项目，代码和测试大量使用了 AI 辅助。面试前要按实际情况整理自己参与的需求、检查过的实现和处理过的问题，不声称全部代码都是独立手写，也不把还讲不清的模块算作熟练掌握。

准备介绍前，先独立走一遍[五分钟演示](../docs/DEMO.md)，对照[技术说明](../docs/ARCHITECTURE.md)找到保存、引用检查和异常处理的代码。项目的实际测试与限制见[测试与待办](../docs/STATUS.md)。
