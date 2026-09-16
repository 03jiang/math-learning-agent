# AI 产品 / 教育科技产品求职材料

## 简历项目条目

**数学学习与错题复盘助手｜个人 AI 产品原型，Codex 辅助开发**

- 定义数学错题复盘场景，将照片输入、题干/作答核对、参考解法对照、确认收藏和再次订正串成可运行流程，使用 Codex 辅助实现 Python / Streamlit 原型。
- 设计 AI 分析边界：只有答案时不推断错误过程，错因引用学生原文并以假设呈现；区分模型建议、用户确认与学习掌握，支持放弃更新、历史追溯和误删恢复。
- 建立无密钥完整演示、13 张合成图片评估场景和分层验收方法；以自动测试验证交互与持久化，规划转录准确性、差异定位有效率及订正完成率等产品指标。

不添加未经验证的学习提升、用户数或准确率，也不称为自主多 Agent 系统。工程测试数可以引用实际 CI 结果，不能改写为教学准确率。

## English résumé version

**Math Learning Agent — Personal AI Product Prototype, built with Codex assistance**

- Defined a math mistake-review workflow spanning problem and student-work capture, learner verification, step comparison, confirmed saves, and correction review; translated it into a Python / Streamlit prototype with Codex assistance.
- Designed evidence and confirmation boundaries: answer-only submissions do not trigger inferred reasoning diagnoses; suggested causes reference student work, while model feedback, self-assessment, and mastery remain distinct.
- Prepared a deterministic no-key demo, 13 synthetic image cases, automated workflow tests, and a measurement plan covering transcription accuracy, useful error localization, and correction completion. Live photo quality and learning outcomes remain unvalidated.

## 60 秒介绍

“这个数学错题复盘原型关注学生自己的解题过程，让学生找到最早的差异，把订正变成可复习的记录。

项目先从四道分数题验证确认与保存，再扩展到上传自有题目和作答。关键选择是先核对输入，再看有原文证据的分析，确认后才保存；只有最终答案时不让系统猜学生怎么算的。

原型用 Codex 辅助开发，提供无密钥演示和自动测试。现在能证明流程可运行、更新可追溯，还不能证明真实识图和学习效果。下一步先做教师核对与学生任务观察，再决定移动端和复习推荐的优先级。”

## 面试准备

| 问题 | 回答线索 |
|---|---|
| 需求有何证据？ | 当前是场景假设与原型探索，没有访谈与真实用户数据，不虚构调研 |
| 与拍照搜题相比关注什么？ | 原作答证据、错误差异、订正历史；价值仍要验证 |
| 为什么增加核对步骤？ | 输入错误会传递给诊断；需衡量准确性与操作成本的取舍 |
| AI 判断错了怎么办？ | 缺条件先澄清、保留原文、错因待核对、用户可不保存；校验不能代替教师评价 |
| 如何定义成功？ | 分层看输入耗时、差异定位、订正完成和同类题迁移，不只看生成成功 |
| 个人贡献与 AI 辅助？ | 方向、需求和验收由人与 Codex 协作形成，代码与测试大量由 Codex 辅助；讲清实际参与，不声称独立手写全部代码 |

先独立走一次 [五分钟演示](../docs/DEMO.md)，能解释原文引用、未确认不保存、订正不等于掌握。简历附 GitHub 链接，以 [产品定义](../docs/PRODUCT.md) 展示产品判断，以可运行流程证明可落地。

这是项目材料，不是完整个人简历。教育、实习、其他技能和岗位信息尚未提供，没有代填。
