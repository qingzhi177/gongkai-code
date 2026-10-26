"""意图识别（方案 8.4 节）：先用规则，以后再考虑小模型。

返回 {"type": ..., "matched": 命中的词}：
- recall    回忆：还记得、上次、那次……       → 回忆模式：取前 3 条、附原话、不走概率
- fact      事实：我叫什么、我的……是什么      → 照常检索（事实通道以后加）
- task      当下任务：帮我查、帮我写……        → 降低召回概率，「活在当下」
- emotion   情绪：难过、想你、累、睡不着……    → 偏向情绪浓的记忆
- greeting  寒暄、日常报备：嗯、哈哈、晚安、早安哦 + 称呼、下课啦、到教室了、亲亲……
            → 不走相关度召回，只看无由来浮现
- followup  很短的追问：然后呢、真的吗          → 拼上上一句和 AI 的回复再检索，其余和 normal 一样
- normal    其他

日常报备怎么认（routine_rest）：去掉称呼（共同语汇里的昵称、事实里「你叫我什么 / 我叫你什么」，由调用方传进来）、
颜文字和表情、语气词、日常用语（早安、明天见、下课、到教室、洗澡、亲亲……）以后，几乎不剩什么（最多 3 个字）的话。
这类话精排分常常很高——「早安 + 称呼」和「我在哦 + 称呼」这样的记忆几乎一模一样——但想起来只会显得突兀。
剩下的字里有情绪词（想你、难过……）的不算。

strip_address：检索用的文本去掉开头、结尾的称呼（「宝宝 今天吃什么」→「今天吃什么」），
免得称呼本身把带着同一个称呼的记忆排到最前面。去掉以后太短（不到 4 个字）就不去。
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

# 日常用语（语气词已经先去掉了，所以这里不带「了、啦、哦」）
ROUTINE = re.compile(
    r"早安|早上好|早八|早七|午安|中午好|晚安|晚上好|明天见|明儿见|下次见|回头见|晚点见|待会见|一会见|拜拜|再见|"
    r"回来|回?到教室|回?到宿舍|回宿舍|回?到家|回家|上课|下课|放学|起床|(?<![提觉清唤])醒|睡觉|去睡|洗澡|洗漱|吃饭|吃完|"
    r"亲亲|抱抱|贴贴|摸摸|蹭蹭|爱你|muah|mua|么么|啵|在吗|在不在|收到|谢谢|hh+|ok", re.I)
FILLER = re.compile(r"[哦呀啦嘛呢吧叭捏咯哟欸诶嗯啊哈嘿嘻呜喔噢了的也就都还又才个嗷哇耶]")
_ONLY_WORDS = re.compile(r"[^一-鿿A-Za-z0-9]")    # 颜文字里的希腊字母、假名、符号一律去掉
ROUTINE_REST = 3
ADDRESS_MIN = 4
_SEP = r"[\s,，.。!！?？~～…·、:：;；\-_^>＜<（）()　\U0001F300-\U0001FAFF☀-➿]"

_STRIP = re.compile(r"[\s\W_~～]+", re.U)


def core_text(text):
    """去掉空白和标点后的正文（算长度、判断寒暄用）"""
    return _STRIP.sub("", str(text or ""))


def _names(names):
    return sorted({str(n).strip() for n in (names or ()) if len(str(n).strip()) >= 2}, key=len, reverse=True)


def _drop_vocatives(text, names):
    """去掉在叫人的称呼：在开头（后面不是「的」）、在结尾（前面不是「的」）、或者前后都隔开了的。
    「你是我的爱人」里的「爱人」不是在叫人，不去。返回 (去掉以后的, 去掉过没有)"""
    ns = _names(names)
    if not ns:
        return text, False
    alt = "|".join(re.escape(n) for n in ns)
    t = text
    for pat in (rf"^(?:{_SEP}*(?:{alt})(?!的))+", rf"(?<!的)(?:(?:{alt}){_SEP}*)+$", rf"(?<={_SEP})(?:{alt})(?={_SEP})"):
        t = re.sub(pat, " ", t)
    return t, t != text


def routine_rest(text, names=()):
    """去掉称呼、颜文字、语气词和日常用语以后剩下的字，以及有没有去掉过称呼或日常用语。返回 (剩下的, 去掉过没有)"""
    t, hit = _drop_vocatives(str(text or "").strip(), names)
    t = FILLER.sub("", _ONLY_WORDS.sub("", t))
    rest = ROUTINE.sub("", t)
    return rest, hit or rest != t


def is_routine(text, names=()):
    rest, hit = routine_rest(text, names)
    if EMOTION.search(rest):
        return False
    return not rest or (hit and len(rest) <= ROUTINE_REST)


def strip_address(text, names=()):
    """检索用：去掉开头、结尾的称呼（开头的后面不能紧跟「的」——「宝宝的……」不是在叫人）"""
    t = str(text or "").strip()
    ns = _names(names)
    if not t or not ns:
        return t
    alt = "|".join(re.escape(n) for n in ns)
    lead = re.compile(rf"^(?:{_SEP}*(?:{alt})(?!的){_SEP}*)+")
    trail = re.compile(rf"(?:(?:^|{_SEP}+)(?:{alt}){_SEP}*)+$")
    s = trail.sub("", lead.sub("", t)).strip()
    return s if len(core_text(s)) >= ADDRESS_MIN else t


def classify(text, names=()):
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
    if is_routine(t, names):
        return {"type": "greeting", "matched": "日常报备", "routine": True}
    if len(core) <= 3:
        return {"type": "followup", "matched": core}
    m = TASK.search(t)
    if m:
        return {"type": "task", "matched": m.group(0)}
    m = EMOTION.search(t)
    if m:
        return {"type": "emotion", "matched": m.group(0)}
    return {"type": "normal", "matched": ""}
