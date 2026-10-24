"""实时存记录时的序列对齐（app/l0_merge.py）的测试：临时库 + 编出来的对话，不碰真实记忆库，不调用模型。

运行：python3 tests/manual/test_l0_merge.py
（最后一部分走一遍 main.py 的 save_conversation，缺依赖时自动跳过；在 VPS 的 venv 里会跑。）
"""
import asyncio
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="l0mergetest_"))
os.environ["DATA_DIR"] = str(TMP / "data")          # 必须在导入 app.config 之前设置
os.environ["USER_TZ"] = "Asia/Shanghai"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "services" / "memory-service"))
try:
    import dotenv  # noqa: F401
except ImportError:
    sys.modules["dotenv"] = types.SimpleNamespace(load_dotenv=lambda *a, **k: None)

import db  # noqa: E402
from app import config  # noqa: E402
from app import l0_edit as LE  # noqa: E402
from app import l0_merge as M  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


def q(sql, args=()):
    conn = config.get_conn()
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def run(sql, args=()):
    conn = config.get_conn()
    try:
        cur = conn.execute(sql, args)
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def T(n):
    """第 n 分钟（10 月 20 日 12:00 UTC 起）"""
    return f"2026-10-20 {12 + n // 60:02d}:{n % 60:02d}:00"


def save(conv, msgs, now):
    conn = config.get_conn()
    try:
        r = M.merge(conn.cursor(), conv, "pluvia", [{"role": a, "content": b} for a, b in msgs],
                    edited=LE.aliases(conn.cursor(), conv), now=now)
        conn.commit()
        return r
    finally:
        conn.close()


def active(conv):
    return q("SELECT id, role, content, ts, msg_idx, extracted FROM l0_messages WHERE conv_id=? AND status='active' "
             "ORDER BY msg_idx, id", (conv,))


def texts(conv):
    return [r[2] for r in active(conv)]


def sup(conv):
    return [r[0] for r in q("SELECT content FROM l0_messages WHERE conv_id=? AND status='superseded' ORDER BY id", (conv,))]


def orderly(conv):
    """msg_idx 从 0 连续，时间跟着顺序不往回走"""
    rows = active(conv)
    return [r[4] for r in rows] == list(range(len(rows))) and all(a[3] <= b[3] for a, b in zip(rows, rows[1:]))


def conv_msgs(n):
    """编一段 n 轮的对话：[(role, text)]"""
    out = []
    for i in range(1, n + 1):
        out += [("user", f"第{i}轮我说的话，带点内容{i}"), ("assistant", f"第{i}轮你的回复，也带点内容{i}")]
    return out


def chat(conv, full, window, start_min=0):
    """按真实聊天的样子一轮一轮存：每轮窗口 = 前面最近 window-1 条 + 这一轮的问 + 回复"""
    for k in range(0, len(full), 2):
        hist = full[:k]
        w = hist[-(window - 1):] if window > 1 else []
        save(conv, w + full[k:k + 2], T(start_min + k))


def main():
    try:
        db.migrate(config.SQLITE_PATH, log=lambda *a, **k: None)

        print("\n[1] 平时：一轮一轮存，窗口 6 条")
        full = conv_msgs(8)
        chat("A", full, 6)
        check("存下来的就是整段对话，没有作废的", texts("A") == [t for _, t in full] and not sup("A"), (len(texts("A")), sup("A")))
        check("msg_idx 连续，时间跟着顺序走", orderly("A"))
        check("每条的时间是存它的那一轮", active("A")[0][3] == T(0) and active("A")[-1][3] == T(14))

        print("\n[2] 调滑动窗口：变小、只带这一轮、变大")
        more = conv_msgs(14)[16:]
        save("A", full[-1:] + more[0:2], T(20))                       # 窗口变成 2：上一条 + 这一轮
        save("A", more[2:4], T(22))                                  # 只带这一轮（和库里一条都对不上）
        save("A", (full + more[:4])[-3:] + more[4:6], T(24))
        cur = full + more[:6]
        check("窗口变小、只带这一轮：都接在后面，前面的一条不作废", texts("A") == [t for _, t in cur] and not sup("A"), sup("A"))
        save("A", (full + more[:6])[-20:] + more[6:8], T(26))       # 窗口变大到 20 条
        cur = full + more[:8]
        check("窗口变大：重合的都认得，不重复存", texts("A") == [t for _, t in cur] and not sup("A"))
        check("顺序和时间还是对的", orderly("A"))

        print("\n[3] 窗口开头那条对不上（以前会把更早的整段作废）")
        n_before = len(active("A"))
        hist = [(r[1], r[2]) for r in active("A")]
        w = hist[-6:]
        w[0] = (w[0][0], "前端在开头放的一段摘要，和库里哪条都不一样")
        save("A", w + [("user", "新的一句话呀"), ("assistant", "新的回复呀")], T(30))
        rows = active("A")
        extra = {"前端在开头放的一段摘要，和库里哪条都不一样", "新的一句话呀", "新的回复呀"}
        check("更早的消息一条都没作废，顺序不变", not sup("A") and [r[2] for r in rows if r[2] not in extra] == [t for _, t in hist], sup("A"))
        check("对不上的那条另外存下（插在原来那条后面），新的一轮接在最后",
              len(rows) == n_before + 3 and rows[-1][2] == "新的回复呀"
              and [r[2] for r in rows].index("前端在开头放的一段摘要，和库里哪条都不一样") == n_before - 5)
        check("插进去的那条时间夹在前后两条之间", orderly("A"))
        hist = [(r[1], r[2]) for r in active("A")]
        w = hist[-5:]
        w[0] = (w[0][0], w[0][1] + "！")                              # 开头那条改了一个字
        old_id = active("A")[-5][0]
        save("A", w + [("user", "再说一句"), ("assistant", "再回一句")], T(32))
        check("开头那条只改了几个字：只换这一条，沿用原来的时间", sup("A") == [hist[-5][1]]
              and active("A")[-7][2] == hist[-5][1] + "！" and active("A")[-7][3] == q("SELECT ts FROM l0_messages WHERE id=?", (old_id,))[0][0])

        print("\n[4] 重新生成、编辑最后一句")
        chat("B", conv_msgs(3), 10)
        r = save("B", [(a, b) for a, b in conv_msgs(3)[:-1]] + [("assistant", "完全不同的第二个版本回复")], T(40))
        rows = active("B")
        check("重新生成：旧回复作废，新回复在同一个位置，用现在的时间", sup("B") == ["第3轮你的回复，也带点内容3"]
              and rows[-1][2] == "完全不同的第二个版本回复" and rows[-1][3] == T(40) and r["saved"] == 1, (sup("B"), rows[-1]))
        u_ts = active("B")[-2][3]
        save("B", [(a, b) for a, b in conv_msgs(3)[:-2]] + [("user", "第3轮我说的话，带点内容3呀"), ("assistant", "改了问题以后的回复")], T(45))
        rows = active("B")
        check("编辑最后一句再发：问和回复都换成新的，问沿用原来的时间", rows[-2][2] == "第3轮我说的话，带点内容3呀" and rows[-2][3] == u_ts
              and rows[-1][2] == "改了问题以后的回复" and len(rows) == 6, rows[-2:])
        conn = config.get_conn()
        conn.execute("UPDATE l0_messages SET extracted=1 WHERE conv_id='B'")
        conn.commit()
        conn.close()
        r = save("B", [(a, b) for a, b in conv_msgs(3)[:-2]] + [("user", "第3轮我说的话，带点内容3呀"), ("assistant", "又一个版本")], T(46))
        check("提取过的被换下来：返回给调用方去作废那一批记忆", len(r["superseded"]) == 1, r)

        print("\n[5] 发图：这一轮只有字，下一轮历史里多了图片占位")
        save("C", [("user", "你好呀"), ("assistant", "你好")], T(50))
        save("C", [("user", "你好呀"), ("assistant", "你好"), ("user", "看看今天的晚霞"), ("assistant", "好漂亮的晚霞")], T(51))
        run("UPDATE l0_messages SET extracted=1 WHERE conv_id='C'")
        img = active("C")[2]
        r = save("C", [("user", "你好呀"), ("assistant", "你好"), ("user", "[图片：sky.jpg]\n看看今天的晚霞"), ("assistant", "好漂亮的晚霞"),
                       ("user", "是吧"), ("assistant", "嗯嗯")], T(70))
        now_img = q("SELECT id, content, ts, extracted, status FROM l0_messages WHERE id=?", (img[0],))[0]
        check("认得是同一条：不作废、不重存，占位补进去，时间和提取状态不变",
              not sup("C") and r["upgraded"] == 1 and r["superseded"] == [] and now_img[1] == "[图片：sky.jpg]\n看看今天的晚霞"
              and now_img[2] == img[3] and now_img[3] == 1 and now_img[4] == "active", (sup("C"), r, now_img))
        r = save("C", [("user", "看看今天的晚霞"), ("assistant", "好漂亮的晚霞"), ("user", "是吧"), ("assistant", "嗯嗯"),
                       ("user", "再来一句"), ("assistant", "好")], T(72))
        check("之后哪一轮又没带占位：也认得，不会把占位去掉", not sup("C") and r["upgraded"] == 0
              and active("C")[2][2] == "[图片：sky.jpg]\n看看今天的晚霞")
        save("C", [("user", "再来一句"), ("assistant", "好"), ("assistant", "看到你发的图了")], T(74))    # 只发了图：这一轮没有字
        r = save("C", [("user", "再来一句"), ("assistant", "好"), ("user", "[图片：cat.png]"), ("assistant", "看到你发的图了"),
                       ("user", "可爱吧"), ("assistant", "可爱")], T(80))
        rows = active("C")
        i = [x[2] for x in rows].index("[图片：cat.png]")
        check("只发了图的那条下一轮补进来：插在原来的位置，时间夹在前后两条之间",
              rows[i - 1][2] == "好" and rows[i + 1][2] == "看到你发的图了" and rows[i - 1][3] <= rows[i][3] <= rows[i + 1][3] and orderly("C"),
              [(x[2], x[3]) for x in rows[i - 1:i + 2]])

        print("\n[6] 接着导入的旧会话聊（以前和旧聊天一字不差的话会被当成重复丢掉）")
        imp = [("user", "晚安"), ("assistant", "晚安，好梦"), ("user", "之前出去的时候拍的"), ("assistant", "拍得真好"),
               ("user", "早呀"), ("assistant", "早")]
        for i, (role, text) in enumerate(imp):
            run("INSERT INTO l0_messages (conv_id, msg_idx, role, content, ts, client, status, extracted, group_id, version) "
                "VALUES ('D', ?, ?, ?, ?, 'kelivo', 'active', 1, ?, 0)", (i, role, text, T(100 + i), f"g{i}"))
        r = save("D", [("user", "[图片]\n之前出去的时候拍的"), ("assistant", "拍得真好"), ("user", "早呀"), ("assistant", "早"),
                       ("user", "晚安"), ("assistant", "晚安呀")], T(200))
        rows = active("D")
        check("窗口里导入过的不重复存；和旧聊天一样的「晚安」照样存下来", [x[2] for x in rows] ==
              ["晚安", "晚安，好梦", "[图片]\n之前出去的时候拍的", "拍得真好", "早呀", "早", "晚安", "晚安呀"] and r["saved"] == 2,
              [x[2] for x in rows])
        check("导入的那条补上了图片占位，没有作废", not sup("D") and rows[2][3] == T(102))
        check("新的接在导入的后面，顺序和时间对", orderly("D") and rows[-1][3] == T(200))

        print("\n[7] 碰巧只对上一句很短的话")
        save("E", conv_msgs(6) + [("user", "嗯"), ("assistant", "怎么啦")] + conv_msgs(9)[12:], T(300))
        n = len(active("E"))
        r = save("E", [("user", "嗯"), ("assistant", "今天的新回复")], T(310))            # 窗口只带这一轮，「嗯」在前面出现过
        check("后面还有一大段对不上：当成一条都没对上，接在最后，不作废", not sup("E") and len(active("E")) == n + 2
              and texts("E")[-2:] == ["嗯", "今天的新回复"], (sup("E"), texts("E")[-3:]))
        save("E", [("user", "嗯嗯"), ("assistant", "第一版")], T(320))
        r = save("E", [("user", "嗯嗯"), ("assistant", "第二版")], T(321))                # 窗口 2 条时重新生成
        check("窗口只有这一轮时重新生成：照样换掉旧回复", sup("E") == ["第一版"] and texts("E")[-1] == "第二版")

        print("\n[8] 在前端改了很早的一句再发（后面的都不要了）")
        chat("F", conv_msgs(6), 20)
        full6 = conv_msgs(6)
        r = save("F", full6[:4] + [("user", "第3轮我说的话，改成别的了"), ("assistant", "针对新问题的回复")], T(400))
        check("改的那句之后的旧消息都作废，新的接上", texts("F") == [t for _, t in full6[:4]] + ["第3轮我说的话，改成别的了", "针对新问题的回复"]
              and len(sup("F")) == 8, (texts("F"), len(sup("F"))))

        print("\n[9] 只有一轮的会话，改了第一句再发")
        save("G", [("user", "你好，我叫小雨"), ("assistant", "你好小雨")], T(500))
        save("G", [("user", "你好，我叫小雨呀"), ("assistant", "你好呀小雨")], T(501))
        check("整段换成新的（不是接在后面）", texts("G") == ["你好，我叫小雨呀", "你好呀小雨"] and len(sup("G")) == 2)
        save("H", [("user", "第一天的话"), ("assistant", "第一天的回复")], T(510))
        save("H", [("user", "完全另一件事"), ("assistant", "另一件事的回复")], T(511))
        check("第一句完全不一样（窗口只带这一轮）：接在后面", texts("H") == ["第一天的话", "第一天的回复", "完全另一件事", "另一件事的回复"])

        print("\n[10] 在 Dashboard 上改过的消息")
        chat("I", conv_msgs(3), 10)
        mid = active("I")[1][0]
        LE.edit(mid, "第1轮你的回复，改过了", "fix")
        save("I", conv_msgs(3) + [("user", "第4轮"), ("assistant", "第4轮回复")], T(600))
        check("前端带着改之前的原文来：认得，修改不被冲掉，也不重复存", texts("I")[1] == "第1轮你的回复，改过了" and not sup("I") and len(texts("I")) == 8)

        print("\n[11] 很长的会话：只在最后一段里对齐")
        save("J", conv_msgs(150), T(700))
        before = {r[0]: r[4] for r in active("J")}
        hist = [(r[1], r[2]) for r in active("J")]
        save("J", [("user", "和哪条都不像的开头")] + hist[-3:] + [("user", "新"), ("assistant", "新回复")], T(710))
        after = {r[0]: r[4] for r in active("J")}
        check("前面的消息位置一条都没动、一条都没作废", all(after.get(k) == v for k, v in list(before.items())[:250]) and not sup("J"))

        print("\n[12] 中间某条换了版本（后面的消息还在）、碰巧对上前面一句很短的话")
        save("L", [("user", "你好"), ("assistant", "你好呀，今天怎么样"), ("user", "还不错，出去走了走"), ("assistant", "真好，去了哪里")], T(900))
        save("L", [("user", "你好"), ("assistant", "换了一个版本的问候"), ("user", "还不错，出去走了走"), ("assistant", "真好，去了哪里")], T(901))
        check("中间那条换了版本：旧版本作废，新版本在原来的位置", texts("L") == ["你好", "换了一个版本的问候", "还不错，出去走了走", "真好，去了哪里"]
              and sup("L") == ["你好呀，今天怎么样"], texts("L"))
        save("N", [("user", "嗯")] + conv_msgs(8)[1:] + [("user", "最后一句话"), ("assistant", "最后一句回复")], T(910))
        n = len(active("N"))
        save("N", [("user", "嗯"), ("assistant", "完全不相干的一句"), ("user", "最后一句话"), ("assistant", "最后一句回复"),
                   ("user", "新问题"), ("assistant", "新回答")], T(911))
        check("前面碰巧对上「嗯」、中间库里多出很多条：中间的不作废", not sup("N") and len(active("N")) == n + 3, (sup("N"), len(active("N")), n))

        print("\n[13] 走一遍 save_conversation（要导入 main.py）")
        try:
            import main as MAIN
        except Exception as ex:      # 本地缺 fastapi 之类的依赖
            print(f"  - 跳过：导入 main.py 失败（{type(ex).__name__}）")
            MAIN = None
        if MAIN:
            for i, (role, text) in enumerate(imp):
                run("INSERT INTO l0_messages (conv_id, msg_idx, role, content, ts, client, status, extracted, group_id, version) "
                    "VALUES ('K', ?, ?, ?, ?, 'kelivo', 'active', 1, ?, 0)", (i, role, text, T(800 + i), f"k{i}"))
            res = asyncio.run(MAIN.save_conversation(MAIN.SaveRequest(conv_id="K", client="pluvia", messages=[
                {"role": "user", "content": "早呀"}, {"role": "assistant", "content": "早"},
                {"role": "user", "content": "晚安"}, {"role": "assistant", "content": "晚安呀"}])))
            check("接着导入的会话聊：「晚安」存下来了，导入过的不重复存", res.get("saved") == 2 and texts("K")[-2:] == ["晚安", "晚安呀"]
                  and len(texts("K")) == 8, (res, texts("K")))
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n通过 {PASS}，失败 {FAIL}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
