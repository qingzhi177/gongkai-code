"""运行时可调参数（Dashboard 上能改的那些）。

存在 app_settings 表（迁移 005）：「组名.参数名」→ JSON 值。没改过的项用默认值（.env 或代码里的常量），
「恢复默认」就是删掉那一行。召回参数（recall.*）、聊天里放什么（compose.*），以及各层的开关。

召回编排每一轮读一次 get("recall")：有 5 秒缓存，保存 / 恢复默认后立即失效，不用重启。
"""
import json
import re
import sqlite3
import threading
import time

import timeutil
from app.config import get_conn
from app.recall import decide as D
from app.recall import rerank as R

CACHE_TTL = 5.0


def _item(key, typ, default, lo, hi, step, section, label, help_text):
    return {"key": key, "type": typ, "default": default, "min": lo, "max": hi, "step": step,
            "section": section, "label": label, "help": help_text}


SPECS = {
    "recall": [
        _item("t_high", float, D.T_HIGH, 0.0, 1.0, 0.01, "门槛", "强门槛",
              "精排分 ≥ 这个值：一定会想起（受「每轮最多几条」限制）。调低会想起得更多、更频繁。"),
        _item("t_low", float, D.T_LOW, 0.0, 1.0, 0.01, "门槛", "中门槛",
              "精排分在中门槛和强门槛之间：按「中层概率」偶尔想起；低于中门槛的不会想起。不能高于强门槛。"),
        _item("t_recall", float, D.T_RECALL, 0.0, 1.0, 0.01, "门槛", "回忆门槛",
              "问「还记得吗」「上次……」时（回忆模式），精排分 ≥ 这个值的取前几条，附原话，不走概率。比平时的门槛低：你在主动问。"),
        _item("min_sim", float, D.MIN_SIM, 0.0, 1.0, 0.01, "门槛", "向量兜底门槛",
              "精排暂时不可用（超时、出错、关掉了）时：平时不想起；问「还记得吗」时改看向量相似度，≥ 这个值的取前几条。"),
        _item("medium_prob", float, D.MEDIUM_PROB, 0.0, 1.0, 0.01, "概率", "中层概率",
              "有「中」候选时，这一轮真的想起的概率（0.3 = 30%）。"),
        _item("task_medium_prob", float, D.TASK_MEDIUM_PROB, 0.0, 1.0, 0.01, "概率", "做事时的中层概率",
              "识别成「当下任务」（帮我写、帮我查……）时改用这个概率，默认更低，专心做事。"),
        _item("serendipity_prob", float, D.SERENDIPITY_PROB, 0.0, 1.0, 0.01, "概率", "无由来浮现概率",
              "每一轮和话题无关、忽然想起一件情绪浓或核心的事的概率（0.05 = 5%）。"),
        _item("emotion_bonus", float, D.EMOTION_BONUS, 0.0, 0.3, 0.01, "概率", "情绪加分",
              "聊到情绪（难过、想你……）时，给情绪浓的记忆额外加的分。"),
        _item("max_picks", int, D.MAX_PICKS, 0, 5, 1, "条数和冷却", "每轮最多几条",
              "平时每一轮最多想起几条（0 = 不想起）。"),
        _item("recall_top", int, D.RECALL_TOP, 1, 6, 1, "条数和冷却", "回忆时最多几条",
              "识别成「回忆」（还记得吗、上次……）时最多取几条。"),
        _item("recent_turns", int, D.RECENT_TURNS, 0, 50, 1, "条数和冷却", "最近几轮不重复",
              "同一个对话里，最近这么多轮出现过的不再出现。"),
        _item("medium_cooldown_h", float, D.MEDIUM_COOLDOWN_H, 0.0, 720.0, 1, "条数和冷却", "中层冷却（小时）",
              "因为「中」被想起过的，这么久之内不再因为「中」出现（跨对话）。"),
        _item("serendipity_cooldown_d", float, D.SERENDIPITY_COOLDOWN_D, 0.0, 90.0, 1, "条数和冷却", "无由来冷却（天）",
              "无由来浮现过的，这么多天内不再无由来出现。"),
        _item("time_enabled", bool, True, None, None, None, "时间", "启用时间通道",
              "话里提到时间（昨天、上周三、去年夏天、10 月 15 号……）时，把发生在那段时间里的记忆也找出来。"),
        _item("time_bonus", float, D.TIME_BONUS, 0.0, 0.3, 0.01, "时间", "时间加分",
              "发生在话里提到的那段时间里的记忆，精排分额外加这么多。"),
        _item("rerank_enabled", bool, R.RERANK_ENABLED, None, None, None, "精排", "启用精排",
              "关掉以后平时都不会想起，只有问「还记得吗」时按向量相似度找。"),
        _item("rerank_model", str, R.RERANK_MODEL, None, None, None, "精排", "精排模型",
              "阿里云百炼的文本排序模型，比如 qwen3.7-text-rerank、qwen3-rerank、gte-rerank-v2。"),
        _item("rerank_timeout", float, R.RERANK_TIMEOUT, 0.3, 10.0, 0.1, "精排", "精排超时（秒）",
              "超过这个时间就放弃精排、改用向量相似度。"),
    ],
    "compose": [
        _item("recall_live", bool, True, None, None, None, "召回", "用新的召回（正式）",
              "开：每一轮由记忆服务决定想起什么（分层、冷却、日常报备不召回），放进聊天；"
              "关：回到以前网关的随机浮现，新逻辑只在旁边记录（影子模式），方便对比或者出问题时退回去。"),
        _item("facts", bool, True, None, None, None, "放进聊天的内容", "事实",
              "「事实」页的档案几行（关于你 / 关于我 / 我们之间），接在人格画像后面。"),
        _item("chapters", bool, True, None, None, None, "放进聊天的内容", "章节",
              "章节目录 + 进行中的这一章，代替以前整篇的「共同经历叙事」。关掉就放回旧的叙事。"),
        _item("arcs", bool, True, None, None, None, "放进聊天的内容", "主题线",
              "进行中的主题卷：每条一行（标题 + 一句话）。"),
        _item("lexicon", bool, True, None, None, None, "放进聊天的内容", "共同语汇",
              "收下的称呼、梗、固定说法和它们的意思（常用的在前，有长度上限）。"),
        _item("state", bool, True, None, None, None, "放进聊天的内容", "关系状态",
              "「我们最近」这张卡（节奏、你的天气、正在一起经历的、最近的亲近时刻、轻放的话题）。"),
        _item("digest", bool, True, None, None, None, "放进聊天的内容", "近七天摘要",
              "新版的近七天摘要，代替以前的「近期摘要」。关掉就放回旧的。"),
        _item("episodes", bool, True, None, None, None, "放进聊天的内容", "最近几天的片段",
              "最近两个聊过天的日子里每日片段的标题；话里提到某段时间（昨天、上周三……）时，那段时间的片段也会浮现。"),
        _item("loops", bool, True, None, None, None, "放进聊天的内容", "还没了结的事",
              "「未完的事」的开窗几件（「不主动提」的不放）。"),
    ],
    "windows": [
        _item("notes", bool, True, None, None, None, "窗口里的时间", "窗口里的时间标记",
              "窗口开头那条前面写明它是几点的；和上一句隔了很久（下一项）、或者中间省略了几条（挑选式搬家）的地方，"
              "前面加一行附注写明隔了多久。只加在发给模型的消息里，不存进记忆，前端也看不到。"),
        _item("gap_hours", float, 3.0, 0.5, 72.0, 0.5, "窗口里的时间", "隔多久算断口（小时）",
              "窗口里两句话隔了这么久以上，就在后一句前面写明隔了多久（比如昨晚的晚安和今早的早安之间）。"),
        _item("bridge", bool, True, None, None, None, "滑出窗口的那段", "滑出窗口的那段写小结",
              "窗口一直在往后滑：滑出去、但还没整理成每日片段的那段（每日片段要等一天过完才写），每 16 条左右写一两句小结，"
              "放在窗口开头那行附注里，带一个「那会儿」的温度词（会调用模型，花一点点钱；快滑出去时就提前写好）。"),
        _item("bridge_max_chars", int, 900, 200, 3000, 50, "滑出窗口的那段", "小结最多多少字",
              "窗口开头那行附注里小结的总长度上限，超过时去掉最早的几段。"),
        _item("prev_window", bool, True, None, None, None, "新窗口", "新窗口接上一个窗口",
              "新开一个对话时，第一条附上：上一个窗口停在什么时候、还没整理成片段的那部分说了什么、最后几句。"),
    ],
    "notebook": [
        _item("enabled", bool, True, None, None, None, "小本子", "小本子",
              "模型可以用 jot 工具随手记下聊天里细碎的细节、有意思的小瞬间（和画像分开：画像是对彼此稳定的认识，小本子是一件件具体的小事）。"
              "关掉以后不再给模型这个工具，也不再放进聊天；已经记下的留着。"),
        _item("days", int, 14, 3, 60, 1, "放进聊天", "放最近几天记的",
              "今天之前这么多天里记的放进 system（每天只变一次，不影响缓存）；今天新记的放在窗口开头那行附注里，窗口下次往后挪时带上。钉住的一直放。"),
        _item("max_items", int, 12, 3, 40, 1, "放进聊天", "最多放几条", "超过时去掉最早的（钉住的先留）。"),
        _item("max_chars", int, 700, 200, 3000, 50, "放进聊天", "最多放多少字", "超过时去掉最早的。"),
    ],
    "cache": [
        _item("ttl_1h", bool, True, None, None, None, "缓存", "聊天记录缓存 1 小时",
              "开：发给模型的聊天记录那部分缓存 1 小时（写一次贵一点：2 倍，但隔了 5 分钟以上再回也能命中）；"
              "关：5 分钟（写一次 1.25 倍，适合一直不停地聊）。system（画像、记忆这些）不管开关都是 1 小时。前端打的缓存标记由网关统一换成这里的时长。"),
        _item("heartbeat", bool, True, None, None, None, "缓存", "缓存心跳（保温）",
              "停下来没聊的时候，网关隔一段时间把上一轮原样再发一次（只读缓存、让模型只回一个字，前端看不到、不存记忆），"
              "缓存就不会过期，回来接着聊时不用重新写一遍。每次大约是整段读一遍缓存的钱（全价的十分之一）加几个字的输出。"),
        _item("sticky_provider", bool, True, None, None, None, "缓存", "同一段对话留在同一家（OpenRouter）",
              "OpenRouter 会把同一个模型分给 Anthropic、Google Vertex、Amazon Bedrock 几家，每家的缓存是分开的，换一家就要重新写。"
              "开着时，下一轮优先发给上一轮回答的那一家（那家不可用时照样换别家）。别的中转不受影响。"),
        _item("beat_minutes_1h", float, 50.0, 30.0, 58.0, 1, "心跳", "1 小时缓存：每隔几分钟心跳一次",
              "从上一次读写缓存算起，隔这么久心跳一次（要小于 60，留一点余量）。"),
        _item("beat_minutes_5m", float, 4.5, 2.0, 4.8, 0.1, "心跳", "5 分钟缓存：每隔几分钟心跳一次",
              "从上一次读写缓存算起，隔这么久心跳一次（要小于 5）。"),
        _item("idle_hours_1h", float, 4.0, 0.5, 24.0, 0.5, "心跳", "1 小时缓存：停下多久以后不再心跳（小时）",
              "最后一次真聊天以后超过这么久就不再保温（比如去睡觉了），下次回来重新写一遍缓存。"
              "粗算：每次心跳的钱大约是重新写一遍的二十分之一，所以 4 小时（4 次）以内回来都划算。"),
        _item("idle_minutes_5m", float, 30.0, 5.0, 180.0, 5, "心跳", "5 分钟缓存：停下多久以后不再心跳（分钟）",
              "5 分钟缓存心跳得勤，停得也要早：粗算 30 分钟以内回来划算。"),
        _item("daily_max", int, 40, 0, 500, 1, "心跳", "每天最多心跳几次",
              "兜底的上限（0 = 不心跳），按你所在时区的一天算。"),
    ],
    "wake": [
        _item("enabled", bool, False, None, None, None, "总开关", "自动醒来",
              "你不在的时候，ta 偶尔醒来一下：能看到这段对话，可以想一想、翻翻记忆、记小本子，想说话就给你发一条消息，也可以不说。"
              "醒来走的是平时聊天那条路（同样的 system、历史和工具，缓存也一样），醒一次大约多花一轮的钱。"
              "前端要能收消息（接入约定第 17 节）才有意义；打开之前，想先在聊天里和 ta 说一声也可以。"),
        _item("random", bool, True, None, None, None, "两种醒法", "随机醒来",
              "你停下一段时间以后，每隔一阵掷一次骰子，掷中了就醒（白天、夜里的概率分开设）。"),
        _item("plans", bool, True, None, None, None, "两种醒法", "ta 自己约时间",
              "给 ta 一个 schedule_wake 工具：平时聊天时 ta 可以约下次几点醒（比如「你考完试那会儿我来问问」）。"
              "到点时你们正在聊天，就不单独醒，下次聊天时告诉 ta。打开 / 关掉会让工具清单变一次（那一轮缓存重写一次）。"),
        _item("speak", bool, True, None, None, None, "两种醒法", "醒来可以给你发消息",
              "关掉就是「静默档」：ta 醒来只自己待一会儿（可以翻记忆、记小本子），不会给你发消息。"),
        _item("idle_minutes", int, 120, 20, 1440, 10, "随机醒来", "停下多久以后才开始掷骰子（分钟）",
              "从你最后一次聊天算起。"),
        _item("check_minutes", int, 60, 15, 720, 5, "随机醒来", "每隔多久掷一次（分钟）", ""),
        _item("day_prob", float, 0.3, 0.0, 1.0, 0.05, "随机醒来", "白天每次掷中的概率",
              "0.3 = 30%。"),
        _item("night_prob", float, 0.0, 0.0, 1.0, 0.05, "随机醒来", "夜里每次掷中的概率",
              "默认 0：夜里不随机醒（ta 自己约的照样醒）。"),
        _item("night_start", int, 23, 0, 23, 1, "白天和夜里", "夜里从几点开始", "按你所在的时区。"),
        _item("night_end", int, 8, 0, 23, 1, "白天和夜里", "夜里到几点结束", "和开始一样就是不分白天夜里。"),
        _item("day_max", int, 4, 0, 24, 1, "上限", "白天最多醒几次",
              "随机的和 ta 约的一起算；白天、夜里分开计数。"),
        _item("night_max", int, 1, 0, 12, 1, "上限", "夜里最多醒几次", ""),
        _item("min_gap_minutes", int, 90, 10, 1440, 10, "上限", "随机醒来离上一次醒来至少隔多久（分钟）",
              "ta 约好的时间不受这条限制。"),
        _item("unanswered_stop", int, 2, 1, 10, 1, "上限", "连着说了几句你都没回，就先不随机醒",
              "你回来聊天以后重新算。ta 约好的时间照样醒（ta 会知道你还没回）。"),
        _item("max_rounds", int, 5, 1, 12, 1, "醒来时", "最多几轮工具调用", "超过就停下，这次醒来到此为止。"),
        _item("allow_recall", bool, True, None, None, None, "醒来时能用的工具", "想起以前的事（recall）", ""),
        _item("allow_feel", bool, True, None, None, None, "醒来时能用的工具", "记下感受（feel）", ""),
        _item("allow_jot", bool, True, None, None, None, "醒来时能用的工具", "记小本子（jot）", ""),
        _item("allow_profile", bool, False, None, None, None, "醒来时能用的工具", "更新对彼此的认识（update_profile）",
              "默认关：稳定的认识最好在聊天里、你也在的时候改。"),
        _item("allow_web", bool, False, None, None, None, "醒来时能用的工具", "联网搜索（bocha_search）", ""),
        _item("plan_quiet_minutes", int, 15, 0, 240, 5, "ta 自己约时间", "到点前这么久之内聊过天，就不单独醒（分钟）",
              "你们正在聊的时候不用单独醒；下次聊天时会告诉 ta 这次没醒。"),
        _item("push_url", str, "", None, None, None, "推送", "推送地址（可不填）",
              "说了话的时候往这个地址发一条推送，兼容 ntfy（比如 https://ntfy.sh/一个只有你知道的名字，手机上装 ntfy 订阅它）。"
              "不填就只等前端来取。只接受 https。"),
        _item("push_title", str, "", None, None, None, "推送", "推送的标题（可不填）",
              "不填就是「新消息」。"),
        _item("push_preview", bool, True, None, None, None, "推送", "推送里带上 ta 说的话",
              "关掉的话推送只写「有一条新消息」，锁屏上看不到内容。"),
    ],
    "episodes": [
        _item("enabled", bool, True, None, None, None, "整理", "自动整理每日片段",
              "每天把对话整理成片段（会调用模型，花一点点钱）。关掉以后只能手动点「整理」。"),
    ],
    "arcs": [
        _item("enabled", bool, True, None, None, None, "归卷", "自动归卷和写卷",
              "新片段整理好以后自动归进主题卷，确认过的卷有新素材时自动写（会调用模型，花一点点钱）。"),
        _item("propose_min_episodes", int, 3, 2, 20, 1, "归卷", "几个片段才提议开卷",
              "一个新主题积累到这么多个片段（并且跨了足够的天数），才在 Dashboard 上请你确认。"),
        _item("propose_min_days", int, 2, 1, 30, 1, "归卷", "跨几天才提议开卷",
              "一个新主题的片段至少分布在这么多天，才请你确认（避免一次聊天就开一个卷）。"),
        _item("narrate_min_new", int, 2, 1, 20, 1, "写卷", "攒几个新片段写一次",
              "确认过的卷，攒够这么多个还没写进去的新片段，就追加一小节。"),
        _item("dormant_days", int, 30, 7, 365, 1, "写卷", "多少天没新素材算休眠",
              "这么多天没有新素材的卷转为「休眠」，有新素材时自动醒来。"),
    ],
    "digests": [
        _item("enabled", bool, True, None, None, None, "摘要", "自动写日摘、周摘和近七天摘要",
              "每日片段整理好以后自动写那天的日摘，一周结束写周摘，近七天摘要跟着更新（会调用模型，花一点点钱）。"),
    ],
    "lexicon": [
        _item("enabled", bool, True, None, None, None, "语汇", "自动找共同语汇",
              "每日片段整理好以后，扫一遍那天的对话找新的称呼、小梗、固定说法（会调用模型，花一点点钱）。"),
        _item("auto_activate_days", int, 2, 1, 10, 1, "语汇", "出现几天就自动收下",
              "候选的词在这么多天的对话里都出现过，就自动收下（1 = 找到就收下）。没到的留在候选里等你看。"),
    ],
    "loops": [
        _item("enabled", bool, True, None, None, None, "未完的事", "自动找未完的事",
              "每日片段整理好以后，扫一遍那天的对话：记下新的约定、计划、在等的结果、牵挂，也看看以前的事有没有下文（会调用模型，花一点点钱）。"),
        _item("expire_after_days", int, 14, 1, 120, 1, "未完的事", "过了期限几天自动过期",
              "有软期限的事，过了期限这么多天、之后也没再提，就自动标成「过期了」（随时可以重新打开）。"),
        _item("stale_days", int, 45, 7, 365, 1, "未完的事", "多少天没提自动过期",
              "没有期限的事，这么多天都没再提，就自动标成「过期了」（随时可以重新打开）。"),
        _item("pack_size", int, 5, 1, 10, 1, "未完的事", "开窗时最多列几件",
              "以后每次开始聊天时，最多把这么多件还没了结的事告诉我（「不主动提」的不算）。"),
    ],
    "facts": [
        _item("enabled", bool, True, None, None, None, "事实", "自动找事实",
              "每日片段整理好以后，扫一遍那天的对话：记下住在哪、在读什么、家里的猫叫什么、我们之间的称呼这类不常变的事，"
              "说法变了能分清是真变了还是随口一提（会调用模型，花一点点钱）。"),
    ],
    "state": [
        _item("enabled", bool, True, None, None, None, "关系状态", "自动更新关系状态卡",
              "日摘、未完的事、主题卷、话题边界有变化时，自动重写「我们最近」这张卡（会调用模型，花一点点钱）。"),
    ],
    "chapters": [
        _item("enabled", bool, True, None, None, None, "章节", "自动写章节",
              "每日片段整理好以后，把新片段写进进行中的那一章；封存的章自动写摘要（会调用模型，花一点点钱）。"),
        _item("seal_after_days", int, 30, 7, 365, 1, "章节", "一章跨了多少天就提议分章",
              "进行中的这一章跨了这么多天、而且写得够长了，就请模型提议在哪里分开（要你确认才生效）。"),
        _item("seal_min_chars", int, 2500, 500, 20000, 100, "章节", "一章写了多少字就提议分章",
              "和上一项一起满足才提议。"),
        _item("min_chapter_days", int, 7, 1, 90, 1, "章节", "每章至少几天",
              "提议分章时，每一章至少跨这么多天（进行中的最后一章除外）。"),
    ],
}

TYPE_NAMES = {float: "float", int: "int", bool: "bool", str: "str"}


def _check_https(s, label):
    if not re.match(r"^https://[^\s/]+(/\S*)?$", s):
        raise ValueError(f"「{label}」要是 https:// 开头的地址")


# 可以留空的文字参数（键名 → 填了时的检查）。目前只有推送地址。
EMPTY_OK_STR = {"push_url": _check_https, "push_title": None}

_lock = threading.Lock()
_cache = {}   # 组名 -> (时间, {参数: 值})


def _spec(group):
    if group not in SPECS:
        raise ValueError(f"没有这组参数：{group}")
    return {s["key"]: s for s in SPECS[group]}


def _fmt(x):
    return int(x) if isinstance(x, float) and x.is_integer() else x


def coerce(spec, v):
    """把传进来的值转成参数的类型并检查范围；不合法时抛 ValueError（中文说明）"""
    t, label = spec["type"], spec["label"]
    if t is bool:
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)) and v in (0, 1):
            return bool(v)
        if isinstance(v, str) and v.strip().lower() in ("1", "true", "on", "yes", "0", "false", "off", "no"):
            return v.strip().lower() in ("1", "true", "on", "yes")
        raise ValueError(f"「{label}」只能是开或关")
    if t is str:
        s = str(v if v is not None else "").strip()
        if spec["key"] in EMPTY_OK_STR:     # 可以不填的（推送地址这类）
            if len(s) > 300:
                raise ValueError(f"「{label}」不能超过 300 个字符")
            check = EMPTY_OK_STR[spec["key"]]
            if s and check:
                check(s, label)
            return s
        if not s or len(s) > 100:
            raise ValueError(f"「{label}」不能为空，也不能超过 100 个字符")
        return s
    if isinstance(v, bool) or v is None or (isinstance(v, str) and not v.strip()):
        raise ValueError(f"「{label}」要填一个数字")
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"「{label}」要填一个数字")
    if x != x or x in (float("inf"), float("-inf")):
        raise ValueError(f"「{label}」要填一个数字")
    if t is int:
        if not x.is_integer():
            raise ValueError(f"「{label}」要填整数")
        x = int(x)
    if (spec["min"] is not None and x < spec["min"]) or (spec["max"] is not None and x > spec["max"]):
        raise ValueError(f"「{label}」要在 {_fmt(spec['min'])} 到 {_fmt(spec['max'])} 之间")
    return x


def _check_recall(vals):
    if vals["t_low"] > vals["t_high"]:
        raise ValueError(f"中门槛（{vals['t_low']}）不能高于强门槛（{vals['t_high']}）")


CHECKS = {"recall": _check_recall}


def _overrides(conn, group):
    """{参数: (值, 更新时间)}；表还没建（迁移前）时返回空"""
    try:
        rows = conn.execute("SELECT key, value, updated_at FROM app_settings WHERE key LIKE ?",
                            (group + ".%",)).fetchall()
    except sqlite3.OperationalError:
        return {}
    out = {}
    for k, v, ts in rows:
        try:
            out[k[len(group) + 1:]] = (json.loads(v), ts)
        except Exception:
            pass
    return out


def _effective(group, ov):
    vals = {}
    for s in SPECS[group]:
        v = s["default"]
        if s["key"] in ov:
            try:
                v = coerce(s, ov[s["key"]][0])
            except ValueError:
                pass   # 库里的值不合法（手改过？）就当没改过
        vals[s["key"]] = v
    return vals


def invalidate(group=None):
    with _lock:
        if group:
            _cache.pop(group, None)
        else:
            _cache.clear()


def get(group, fresh=False):
    """这一组参数的当前值（改过的用改过的，其余用默认值）"""
    _spec(group)
    now = time.monotonic()
    if not fresh:
        with _lock:
            hit = _cache.get(group)
            if hit and now - hit[0] < CACHE_TTL:
                return dict(hit[1])
    conn = get_conn()
    try:
        vals = _effective(group, _overrides(conn, group))
    finally:
        conn.close()
    with _lock:
        _cache[group] = (now, vals)
    return dict(vals)


def describe(group):
    """给 Dashboard 用：每个参数的名称、说明、范围、默认值、当前值、是否改过"""
    conn = get_conn()
    try:
        ov = _overrides(conn, group)
    finally:
        conn.close()
    vals = _effective(group, ov)
    items = []
    for s in SPECS[group]:
        items.append({**{k: s[k] for k in ("key", "label", "help", "section", "min", "max", "step", "default")},
                      "type": TYPE_NAMES[s["type"]], "value": vals[s["key"]],
                      "overridden": s["key"] in ov, "updated_at": ov[s["key"]][1] if s["key"] in ov else None})
    times = [ov[k][1] for k in ov]
    return {"group": group, "items": items, "updated_at": max(times) if times else None}


def update(group, values):
    """保存一批参数（只存传进来的）。任何一个不合法就整批不存，抛 ValueError。返回保存后的当前值。"""
    spec = _spec(group)
    clean = {}
    for k, v in (values or {}).items():
        if k not in spec:
            raise ValueError(f"没有这个参数：{k}")
        clean[k] = coerce(spec[k], v)
    if not clean:
        return get(group, fresh=True)
    now = timeutil.utc_now_str()
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        merged = {**_effective(group, _overrides(conn, group)), **clean}
        if group in CHECKS:
            CHECKS[group](merged)
        for k, v in clean.items():
            conn.execute("INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?) "
                         "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                         (f"{group}.{k}", json.dumps(v, ensure_ascii=False), now))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
        invalidate(group)
    return get(group, fresh=True)


def reset(group, keys=None):
    """恢复默认：删掉这些参数（不传就是整组）。恢复后如果和别的改过的参数冲突（比如中门槛高于强门槛），抛 ValueError、不改。"""
    spec = _spec(group)
    keys = list(keys or [])
    for k in keys:
        if k not in spec:
            raise ValueError(f"没有这个参数：{k}")
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if keys:
            conn.executemany("DELETE FROM app_settings WHERE key = ?", [(f"{group}.{k}",) for k in keys])
        else:
            conn.execute("DELETE FROM app_settings WHERE key LIKE ?", (group + ".%",))
        merged = _effective(group, _overrides(conn, group))
        if group in CHECKS:
            CHECKS[group](merged)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
        invalidate(group)
    return get(group, fresh=True)
