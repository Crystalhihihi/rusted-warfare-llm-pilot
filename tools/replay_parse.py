# -*- coding: utf-8 -*-
"""Rusted Warfare .replay 最小解析器（格式实证自反编译源码，见文件尾注）。

用法: python tools/replay_parse.py <file.replay> [--json out.ndjson] [--save out.bin]
  默认：打印概要（版本/玩家/时长/每玩家建造落点统计）
  --json：建造落点事件流写 NDJSON（{frame,team,action,x,y,units}）
  --save：解出的 gamesave 原始字节落盘（玩家名/地图名解析留待 v2）

格式（big-endian，全部实证）：
  文件头  : writeUTF("rustedWarfareReplay") int176 int96 writeUTF(游戏版本) byte0
  gamesave: writeUTF("gamesave") int size → [writeUTF("rustedWarfareSave") int176 int96 byte0
            writeUTF("saveCompression") int gzipLen gzipBytes short12345 writeUTF("<SAVE END>")]
  指令流  : 重复 [writeUTF(tag) int size content]：
            rc   = int frame + writeUTF("c") int csize + 指令体（见 parse_command）
            cs   = int frame + int 校验和；wait = int frame；chat = int frame + int + 2×可空字符串
            es/resync/end/endReplayMetaData = 按 size 跳过（结尾含 {frames,time,commandCount}）
"""
import json
import struct
import sys
import zlib
from collections import Counter, defaultdict


class R:
    """大端字节游标。所有读法对照反编译 DataOutputStream 写法。"""

    def __init__(self, buf, off=0):
        self.b = buf
        self.o = off

    def byte(self):
        v = self.b[self.o]
        self.o += 1
        return v - 256 if v > 127 else v

    def bool(self):
        return self.byte() != 0

    def short(self):
        v = struct.unpack_from(">h", self.b, self.o)[0]
        self.o += 2
        return v

    def int(self):
        v = struct.unpack_from(">i", self.b, self.o)[0]
        self.o += 4
        return v

    def long(self):
        v = struct.unpack_from(">q", self.b, self.o)[0]
        self.o += 8
        return v

    def float(self):
        v = struct.unpack_from(">f", self.b, self.o)[0]
        self.o += 4
        return v

    def utf(self):
        n = struct.unpack_from(">H", self.b, self.o)[0]
        self.o += 2
        s = self.b[self.o:self.o + n].decode("utf-8", "replace")
        self.o += n
        return s

    def utf_null(self):
        """as.b(String)：boolean 非空标记 + writeUTF。"""
        return self.utf() if self.bool() else None

    def take(self, n):
        v = self.b[self.o:self.o + n]
        self.o += n
        return v


def parse_au(r):
    """units.au（单位组=订单本体）：enum int + 单位类型(int 或-2+UTF) + 2float(目标点) + long + byte + 2float + 3bool + 可空UTF。
    建造指令的"建什么/在哪建"全在这里（实证：type=extractorT1, 坐标=(1970,130)），指令体的 z/action 反而为空。"""
    au = {"av": r.int()}
    t = r.int()                              # units.as：ordinal / -1 空 / -2 自定义+writeUTF
    if t == -2:
        au["utype"] = r.utf()
    elif t >= 0:
        au["utype"] = f"#{t}"                # 原版单位 ordinal（v1 不映射名字）
    au["x"] = round(r.float(), 1)
    au["y"] = round(r.float(), 1)
    r.long()                                 # g 或 h.eh（-1=空）
    r.byte()                                 # d
    r.float(); r.float()                     # k, l
    r.bool(); r.bool(); r.bool()             # m, j, n
    au["sub"] = r.utf_null()                 # a.c.a() 静态：可空动作字符串
    return au


def parse_command(r, frame):
    """gameFramework.e 指令体（writer 布局，e.java:145）。
    返回 {frame,team,cmd,x,y,units,action} 或 None（解析失败由调用方统计）。"""
    team = r.byte()
    au = parse_au(r) if r.bool() else None
    r.bool(); r.bool()                       # e, g
    r.int()                                  # 旧版动作 int（恒 -1）
    cmd = r.int()                            # units.a 枚举 ordinal（混淆 a-g，仅作数字）
    x = y = None
    if r.bool():                             # hasZ：目标点
        x = round(r.float(), 1)
        y = round(r.float(), 1)
    r.bool()                                 # o
    n = r.int()
    if not (0 <= n <= 100000):
        raise ValueError(f"unit count insane: {n}")
    units = [r.long() for _ in range(n)]     # v + A 的单位 id（eh，long）
    if r.bool():                             # hasP（另一 team 引用）
        r.byte()
    if r.bool():                             # hasL（集结点？）
        r.float(); r.float()
    r.long()                                 # m（目标单位引用，-1=空）
    action = r.utf()                         # 动作 id 字符串（建造类此字段为空，真动作在 au.utype）
    r.bool()                                 # f
    r.short()                                # q
    if r.bool():                             # r：c(0)+2float+int
        r.byte(); r.float(); r.float(); r.int()
    wn = r.int()                             # w 编队路点扩展（d.java）
    if not (0 <= wn <= 10000):
        raise ValueError(f"waypoint count insane: {wn}")
    # 编队扩展在目标字段之后、且嵌套 k.k 含 gzip 子块（v1 不解析）——
    # 建造/坐标/动作等全部目标字段已在前面拿到，直接跳过尾部，事件照样落。
    skipped_w = wn
    if wn == 0:
        r.bool()                             # h（仅 wn=0 时尾部对齐才可靠）
        skipped_w = 0
    if x is None and au is not None:         # 坐标回退到 au（建造/移动类指令的真实落点）
        x, y = au["x"], au["y"]
    # 动作解析优先级：au 单位类型（建造类）> 指令动作串（训练类 u_*）> au 子动作 > "-1"（移动/攻击占位）
    if au is not None and au.get("utype"):
        action = au["utype"]
    elif action in ("", "-1") and au is not None and au.get("sub"):
        action = au["sub"]
    return {"frame": frame, "team": team, "cmd": cmd, "x": x, "y": y,
            "units": units, "action": action, "av": au["av"] if au else None,
            "w": skipped_w}


def parse_replay(path):
    data = open(path, "rb").read()
    r = R(data)
    if r.utf() != "rustedWarfareReplay":
        raise ValueError("not a rustedWarfareReplay file")
    proto, ver96, game_ver = r.int(), r.int(), r.utf()
    r.byte()                                 # 0
    # gamesave 块
    if r.utf() != "gamesave":
        raise ValueError("gamesave block missing")
    gsave = R(r.take(r.int()))
    if gsave.utf() != "rustedWarfareSave":
        raise ValueError("rustedWarfareSave missing")
    gsave.int(); gsave.int(); gsave.byte()
    if gsave.utf() != "saveCompression":
        raise ValueError("saveCompression missing")
    save_raw = zlib.decompress(gsave.take(gsave.int()), 16 + zlib.MAX_WBITS)
    if gsave.short() != 12345 or gsave.utf() != "<SAVE END>":
        raise ValueError("save trailer mismatch")
    # 指令流
    events, failures, meta = [], 0, {}
    fail_samples = []
    chat = []
    while r.o < len(data):
        tag = r.utf()
        size = r.int()
        body = R(r.take(size))
        if tag == "rc":
            frame = body.int()
            if body.utf() != "c":
                failures += 1
                continue
            blob = body.take(body.int())
            try:
                events.append(parse_command(R(blob), frame))
            except Exception:
                failures += 1
                if len(fail_samples) < 3:
                    fail_samples.append((frame, blob[:60].hex(" ")))
        elif tag == "chat":
            chat.append({"frame": body.int(), "who": body.int(),
                         "a": body.utf_null(), "b": body.utf_null()})
        elif tag == "endReplayMetaData":
            body.byte()                        # c(0)
            body.int(); body.int(); body.int(); body.int()
            txt = body.utf()
            meta = dict(kv.split(":") for kv in
                        txt.strip("{}").split(",") if ":" in kv)
        # cs/wait/es/resync/end：按 size 跳过即可
    return {"proto": proto, "game_ver": game_ver, "save": save_raw,
            "events": events, "chat": chat, "meta": meta, "failures": failures,
            "fail_samples": fail_samples}


def save_strings(blob, min_len=3):
    """从 gamesave 原始字节里捞可读字符串（玩家名/地图名的 v1 土法，正经解析留 v2）。"""
    out, cur = [], bytearray()
    for ch in blob:
        if 32 <= ch < 127:
            cur.append(ch)
        else:
            if len(cur) >= min_len:
                out.append(cur.decode("ascii", "replace"))
            cur = bytearray()
    if len(cur) >= min_len:
        out.append(cur.decode("ascii", "replace"))
    return out


def main():
    import glob
    path = sys.argv[1]
    m = glob.glob(path)  # 中文文件名经 shell 传参会坏：允许直接传 glob 模式
    if m:
        path = sorted(m)[-1]
    json_out = sys.argv[sys.argv.index("--json") + 1] if "--json" in sys.argv else None
    save_out = sys.argv[sys.argv.index("--save") + 1] if "--save" in sys.argv else None
    rep = parse_replay(path)
    evs = rep["events"]
    print(f"游戏版本={rep['game_ver']} 协议={rep['proto']} 指令={len(evs)} 解析失败={rep['failures']}")
    for fr, hx in rep["fail_samples"]:
        print(f"  失败样本 frame={fr}: {hx}")
    print(f"结尾元数据: {rep['meta']}")
    if rep["chat"]:
        print(f"聊天 {len(rep['chat'])} 条（样例 3 条）: {rep['chat'][:3]}")
    builds = [e for e in evs if e["action"] and e["x"] is not None]
    print(f"\n带坐标指令 {len(builds)} 条；动作字符串 Top15：")
    for a, c in Counter(e["action"] for e in builds).most_common(15):
        print(f"  {c:5d}  {a}")
    per = defaultdict(Counter)
    for e in builds:
        per[e["team"]][e["action"]] += 1
    print(f"\n分队统计（team: 指令数 | 带坐标数 | Top5 动作）:")
    for t in sorted(per):
        c = per[t]
        tot = sum(1 for e in evs if e["team"] == t)
        top = ", ".join(f"{a}x{n}" for a, n in c.most_common(5))
        print(f"  team{t}: {tot} 指令 | {sum(c.values())} 带坐标 | {top}")
    if json_out:
        with open(json_out, "w", encoding="utf-8") as f:
            for e in evs:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        print(f"\nNDJSON 已写 {json_out}")
    if save_out:
        open(save_out, "wb").write(rep["save"])
        strs = save_strings(rep["save"])
        print(f"gamesave 已写 {save_out}（{len(rep['save'])} 字节）；可读字符串样例: {strs[:25]}")


if __name__ == "__main__":
    main()
