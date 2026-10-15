 Dashboard 定位：

> Memory Observatory（记忆观测与编辑中心）

用户可以观察、理解、调整 AI 的长期记忆形成过程。

---

Dashboard Memory Observatory 扩展需求

目标

新增 Dashboard 可视化管理能力。

让用户可以：

查看记忆形成过程

手动干预记忆提取

修改模型配置

理解不同模型负责什么任务

编辑长期记忆内容


Dashboard 不只是配置页面，而是：

> 记忆系统的可观察、可编辑控制中心。

---

一、L1 Memory 提取管理
功能目标
允许用户查看：
哪些 L0 对话已经完成 L1 提取。
哪些仍未提取。

---

页面：
新增：
Memory Extraction

---

展示内容：
提取状态
例如：
原始消息总量：
12500条
已提取L1：
11200条
待处理：
1300条

---

列表：
显示：
时间	消息范围	状态
2026-08-01	msg 12000-12500	已提取
2026-08-12	msg 12501-13000	待提取

---

操作：
提供按钮：
立即提取
允许：
手动触发 L1 extraction。

---

要求：
不要影响自动提取。
手动提取只是补充触发机制。

---

二、L1 提取进度显示
Dashboard增加：
Memory Pipeline Status
展示：
L0
原始聊天：total messages

L1
processed messages
pending messages
failed messages

Embedding
展示：
vector count
last embedding time

Narrative
展示：
last update time
pending update

---

类似：

Memory Pipeline

L0 Conversation
██████████ 100%

L1 Extraction
████████░░ 80%

Embedding
██████████ 100%

Shared Narrative
██████░░░░ 60%

或者我们要不设计月相进度条叭
然后你来设计得更加具有审美感与设计感显示，例如一整轮盈亏 展示进度（同时显示百分比进度）进度对应月相的细分相位，表面有极淡的噪声纹理（月海感）等等，具体你来设计就好哦

---

四、模型配置中心

功能目标

统一管理不同模块使用的 LLM。（目前完整的主聊天模型配置页面已实现并成功应用，架构可参考，但这部分不用更改逻辑架构）

需要明确：
不同任务使用什么模型。

Model Configuration
配置区域：
1.主聊天模型
用途：
Main Conversation
显示：
Provider
URL
API Key
Model Name

2.L1 Memory Extraction
用途：
L1 extraction
独立配置。

3.Embedding
用途：
Vector embedding
配置：
Embedding API URL
Key
Model

4.Shared Narrative
用途：
共同经历叙事生成
配置：
URL
Key
Model

5.Recent Timeline
用途：
近期摘要生成
配置：
URL
Key
Model

重要：

Dashboard必须明确显示：

例如：

claude-sonnet-x

五、配置安全要求

API Key：不要明文展示。
可显示：sk-xxxx********xxxx或参考当前主聊天模型配置的方式
修改：
重新输入。

六、模型配置数据库设计
新增：
model_configs
字段：
id
name
provider
base_url
api_key_encrypted
model_name
purpose
enabled
created_at
updated_at
purpose:
chat
l1_extract
embedding
shared_narrative
recent_summary
（按具体情况设计就好）

七、关于 LLM 自主维护“小便签”功能（暂缓）

这个功能先不要实现。

原因：

可能和：

L1 memory

about_user

shared narrative


产生职责重叠。

未来可以作为：

AI Notes

独立层。

定位：

不是事实记忆。

不是长期画像。

而是：

AI自主记录：

“以后理解这个人时可能有帮助的小观察”。

例如：

今天发现用户最近更关注……

但需要后续单独设计：

生命周期

删除机制

是否进入cache

是否进入画像更新


当前版本：

保留设计空间。

不要实现。

九、实现顺序建议

不要一次实现。

给 Codex / Claude Code 的执行要求
开始前：
先检查：
1. 当前 Dashboard 技术栈
2. API结构
3. database migration方式
4. 当前memory service接口
输出：
Dashboard当前架构分析
新增页面规划
API新增列表
数据库修改列表
确认后再实现。


---

最终目标

Dashboard成为：

用户
 |
 | 查看/编辑
 ↓

Memory System

L0
 ↓
L1
 ↓
L0.5 Shared Narrative
 ↓
L2 Understanding
 ↓
Cache Context
 ↓
Claude

用户能够理解：
“AI为什么知道这些？”
“这些记忆从哪里来？”
“哪些东西正在影响当前对话？”
（大概就是记忆系统的逻辑架构尽可能可视化，减少黑箱感叭）

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
