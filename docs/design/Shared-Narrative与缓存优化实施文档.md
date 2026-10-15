# 记忆库网关：Memory Continuity Context + Shared Narrative + Anthropic Cache 优化实施文档

## 目标

本次升级不是创建固定人格 AI，而是在已有 L0/L1/L2
记忆架构基础上，增加长期连续性体验。

核心目标：

-   保留过去交流形成的理解
-   保留共同经历的时间叙事
-   让模型理解"我们如何走到现在"
-   不把记忆变成固定规则
-   允许新的交流产生新的理解

# 1. 开始修改前

必须先阅读：

-   services/gateway/server.js
-   services/memory-service/main.py
-   services/memory-service/extract_l1.py
-   profile相关代码
-   database结构
-   Dashboard相关代码

先输出：

1.  当前架构理解
2.  涉及文件列表
3.  每个文件预计修改内容

确认后再编码。

# 2. Identity Context 调整

不要使用：

"你是一位有记忆的AI伙伴"

作为身份定义。

原因：

系统不应该预设 AI 的人格、关系和存在方式。

改为 Memory Continuity Context。

基础system职责：

-   告知模型拥有持续维护的记忆系统
-   告知记忆来自过去交流
-   告知记忆用于理解连续性
-   告知记忆不是固定规则
-   告知当前交流可以产生新的理解

推荐：

你拥有一个持续维护的记忆系统。

这个记忆系统记录了过去交流中逐渐形成的理解，包括对用户的理解、对自身交流方式的理解，以及双方共同经历的重要内容。

这些记忆用于帮助你理解当前交流中的连续性。

记忆不是固定设定，也不是必须遵守的规则。

它们来自过去经验，并可能随着新的交流不断变化。

记忆是沉淀物，不是日记。

记忆不能替代思考。记忆来自过去，情绪和感受来自当下。

请结合当前对话理解当下。

不要机械复述过去，也不要因为过去记录限制新的理解。

新的交流可以形成新的经历、新的认识和新的变化。

# 3. 画像系统定位

现有：

-   about_ai
-   about_user
-   about_us

继续保留。

但它们不是人格配置。

它们是过去交流形成的理解记录。

不预设固定关系性质。

但是，在注入 system context 时，加一个非常轻量的解释层。

例如：
你拥有一个持续维护的记忆系统。

以下内容是过去长期交流中逐渐形成的理解记录：

about_user：关于用户的理解
about_ai：关于自身交流方式和自我理解的记录
about_us：关于双方交流历程和互动方式的理解
这些内容不是固定人格设定，也不是必须遵守的规则。

它们代表过去形成的认识，可以帮助理解当前交流。

随着新的交流，这些理解也可以变化。

然后：

about_user内容

about_ai内容

about_us内容

设计哲学：不限制 AI应该记录什么，只是告诉它，这些东西应该被如何理解。

# 4. L0.5 Shared Narrative

新增共同经历叙事层。

它不是普通summary。

作用：

保存：

-   时间节点
-   共同经历
-   关系变化
-   交流阶段变化
-   重要转折

它回答：

"我们如何一步一步走到这里"。

# 5.Shared Narrative 设计（重要）

设计目标

Shared Narrative 不是普通摘要。

它不是：

单次 conversation summary

每个聊天窗口独立生成的总结

daily/monthly/global 多级递归摘要

它的目标是：

> 保存长期交流过程中形成的共同经历叙事，让模型在不同聊天窗口中继续保持时间连续性。

# 核心原则

不同 conversation 不生成不同的 Shared Narrative。

正确模型：

Conversation A
        \
         \
Conversation B -----> Shared Narrative
         /
        /
Conversation C

多个聊天窗口产生新的经历素材。

这些素材持续更新同一个长期 Shared Narrative。

# 数据层设计

Shared Narrative 在数据层属于 user 级别。

不要按 conversation_id 隔离。

推荐：

narratives

id

user_id

type

content

token_estimate

version

updated_at

其中：

type:

shared_narrative

不推荐

不要设计：

conversation_id

conversation_summary

作为长期共同经历。

原因：

每个窗口独立，会导致：

新窗口无法继承过去

共同经历断裂

失去长期连续感

# Shared Narrative 更新机制

不要采用：

conversation summary

↓

monthly summary

↓

global summary

这种递归压缩。


---

采用：

已有 Shared Narrative

+

新增的重要交流内容

+

相关 L1 memory

↓

更新后的 Shared Narrative

示例：

已有：

最初交流主要围绕问题分析展开。

新的 conversation：

近期开始分享更多生活中的经历。

更新：

最初交流主要围绕问题分析展开。
随着时间推移，交流逐渐包含更多生活分享，
形成了更加连续的交流方式。

# 增量状态设计

增加：

narrative_state

用于记录已经处理到哪里。

字段：

user_id

last_processed_message_id

updated_at

作用：

避免每次更新重新读取全部历史。

流程：

读取：

current Shared Narrative

+

last_processed_message_id之后的新消息


↓

LLM更新


↓

保存新的 Shared Narrative


↓

更新 last_processed_message_id


---

# 与 Cache 的关系

Shared Narrative 是 Anthropic cache②。

每轮聊天：

cache①

Memory Continuity Context

+

L2 Understanding Records


cache②

Shared Narrative


cache③

Recent Timeline


messages

当前聊天窗口

# Shared Narrative 与 L2 三层画像区别

不要混淆。

Shared Narrative

回答：

> 我们经历过什么？



特点：

时间感

变化过程

共同经历


例如：

最初交流更多围绕问题分析。
后来逐渐加入生活分享。


L2 about_user/about_ai/about_us

回答：

> 长期交流中形成了什么理解？



特点：

当前形成的理解

认知沉淀

可以变化


例如：

逐渐形成了一种重视深入理解的交流方式。


#Dashboard 展示调整

Shared Narrative 可视化：

不要拆成数据库章节。

保持：

一整块连续叙事文本

Dashboard 提供：

阅读模式

编辑模式

时间线视觉化模式（展示层）



---

时间线模式：

只是从文本中提取：

时间节点

阶段标题

重要变化


用于观赏。

不要改变底层存储。

# 6. Narrative生成原则

禁止：

summary套summary。

不要：

daily -\> monthly -\> global。

采用：

旧叙事 + 新增对话 + 重要memory -\> 新叙事。

必须：

-   保留真实经历
-   保留时间感
-   保留变化过程

禁止：

-   虚构事件
-   强化不存在的情绪
-   编造承诺

# 7. Anthropic Cache设计

最多4个cache breakpoint。

## Cache① Memory Continuity Context

包含：

-   continuity instruction
-   about_user
-   about_ai
-   about_us
-   custom prompt
-   tools说明

保持稳定system block。

## Cache② Shared Narrative

共同经历叙事。

## Cache③ Recent Timeline

近期阶段摘要。

## Cache④

备用。

# 缓存实现方式（Anthropic现支持一小时与五分钟两种缓存TTL）
server.js 的 cache_control 全部加显式 "ttl": 3600
（应该第78行 systemBlocks 以及后续新增的摘要块；先验证上游中转站是否透传，不透传则无害）


# 8. Gateway修改

修改systemBlocks构造。

最终：

system:

cache① Memory Continuity Context

cache② Shared Narrative

cache③ Recent Timeline

messages：

保持Kelivo原始消息。

禁止：

-   fake user message
-   fake assistant message
-   修改req.body.messages


# 9. Dashboard Prompt

新增用户自定义system prompt。

用途：

interaction preference。

不是：

identity definition。

允许：

-   回答风格
-   分析方式
-   表达偏好

保存：

custom_prompts：

-   id
-   scope_id
-   content
-   enabled
-   updated_at

# 10. Token设置

修改token限制：

只影响注入长度。

不要自动重新生成。

只有点击重新生成才调用LLM。

# 11. 验收

确认：

-   cache_read_input_tokens \> 0
-   L0保存正常
-   recall正常
-   narrative具有叙事与时间连续性
-   prompt修改后cache重新创建
-   Kelivo无需修改

# 最终目标

Claude收到：

过去形成的理解：

L2理解档案

-   

共同经历：

Shared Narrative

-   

近期阶段：

Recent Timeline

-   

当前窗口：

原始对话

形成长期连续交流体验。

项目背景：用户通过Kelivo 前端把 API 指向记忆库网关（OpenAI/Anthropic 兼容），网关转发到 Anthropic API。
当前记忆库项目公开仓库：https://github.com/qingzhi177/gongkai-code
我给你配置了免密重启权限，你可以直接用 
!sudo systemctl restart memory-gateway 重启等
注意事项
- 改代码前先说方案
- 不要改 .env 文件里现有的 key（可以加新字段）
- 每个部分做完让我测试再继续（你也可以执行你自己的测试，可以按需在我测试时开启对应的日志方便你确认测试中的效果等）
- 改完 git commit + push public main
- 用 sudo systemctl restart memory-gateway 或 memory-service 重启
- 每个功能完成后 git commit + push public main
- 如需查看使用的前端kelivo的代码请查看https://github.com/Chevey339/kelivo
版本管理
当前本机项目中应该有个一键脚本同步两个仓库，一个pulic仓库，一个private仓库，你应该可以找到那个脚本
每次改完一个功能，帮我 git commit 并 push 到 public 仓库。并用一键脚本同步到两个仓库，https://github.com/qingzhi177/gongkai-code（这个是个人公开仓库，注意进行信息脱敏处理）
commit message 写清楚改了什么。
执行中有任何问题可以随时和我沟通和确认哦
