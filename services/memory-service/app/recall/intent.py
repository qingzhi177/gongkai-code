"""意图识别（方案 8.4 节）：先用规则，以后再考虑小模型。

返回 {"type": ..., "matched": 命中的词}：
- recall    回忆：还记得、上次、那次……       → 回忆模式：取前 3 条、附原话、不走概率
- fact      事实：我叫什么、我的……是什么      → 照常检索（事实通道以后加）
- task      当下任务：帮我查、帮我写……        → 降低召回概率，「活在当下」
- emotion   情绪：难过、想你、累、睡不着……    → 偏向情绪浓的记忆
- greeting  寒暄：嗯、哈哈、好呀、晚安          → 不走相关度召回，只看无由来浮现
- followup  很短的追问：然后呢、真的吗          → 拼上上一句和 AI 的回复再检索，其余和 normal 一样
- normal    其他
"""
import re

# 「之前 / 以前 / 当时」太常见（「睡觉之前」），只在和「我们 / 你 / 说过 / 一起」连用时才算回忆
RECALL = re.compile(r"还记得|记不记得|还记不记得|记得吗|你忘了|忘了吗|忘记了吗|上次|上回|那次|那天|那晚"
                    r"|(我们|咱们|你)[^，。？！?!]{0,6}(之前|以前|当时)"
                    r"|(之前|以前|当时)[^，。？！?!]{0,6}(说过|聊过|提过|讲过|一起|答应)")
FACT = re.compile(r"我叫什么|我是谁|我在哪|我住|我的[^，。？！?!\s]{1,8}(是什么|是啥|是多少|叫什么|是哪天|是几号|在哪)|你知道我[^，。？！?!\s]{0,6}(吗|么)")
TASK = re.compile(r"帮我(查|写|看|找|翻译|改|算|搜|整理|总结|做)|怎么(写|做|用|弄|改)|代码|报错|bug|写个|写一[个段篇]", re.I)
EMOTION = re.compile(r"难过|伤心|难受|开心|高兴|想你|好想|累|睡不着|失眠|哭|委屈|烦|焦虑|害怕|孤单|孤独|崩溃|心累|抱抱|生气|emo", re.I)
GREETING = re.compile(r"^(嗯+|嗯嗯|哦+|噢+|啊+|哈+|嘿+|嘻+|呜+|好+的?|好呀|好哒|好滴|行|可以|对+|是+的?|收到|ok|okay|晚安|早安|早上好|午安|中午好|晚上好|拜拜|再见|在吗|在不在|hi|hello|嗨|谢谢|谢啦|么么|mua)+$", re.I)

_STRIP = re.compile(r"[\s\W_~～]+", re.U)


def core_text(text):
    """去掉空白和标点后的正文（算长度、判断寒暄用）"""
    return _STRIP.sub("", str(text or ""))


def classify(text):
    t = str(text or "").strip()
    core = core_text(t)
    m = RECALL.search(t)
    if m:
        return {"type": "recall", "matched": m.group(0)}
    m = FACT.search(t)
    if m:
        return {"type": "fact", "matched": m.group(0)}
    if not core or GREETING.match(core):
        return {"type": "greeting", "matched": core[:8]}
    if len(core) <= 3:
        return {"type": "followup", "matched": core}
    m = TASK.search(t)
    if m:
        return {"type": "task", "matched": m.group(0)}
    m = EMOTION.search(t)
    if m:
        return {"type": "emotion", "matched": m.group(0)}
    return {"type": "normal", "matched": ""}
