"""订阅（优先级 + 状态机的追片模型）。

核心思路：不再用「质量分 + 体积」粗略比较，改成一套
**优先级（priority）+ 状态机 + 缺集统计**：

  · 每个候选资源先算一个 pri_order（数值越大越好，100 是顶级档）
  · 订阅记住 current_priority（已收版本的基线）和 episode_priority（按集基线）
  · 只有 pri > 基线才算「值得洗版」，否则一律判“已是最佳”
  · 缺集 = 目标集范围 − 已下载集（note），写在 lack_episode 上
  · 状态机 N 新建 → R 订阅中 / P 待定 / S 暂停

数据字段：best_version / best_version_full / current_priority /
episode_priority / note / total_episode / start_episode / lack_episode / state。

一次检查 = 搜索 → 定位条目 → 取磁力 → 过滤 → 算优先级 → 和基线比 → 投 115。
"""

import random
import re
import threading
import time
import traceback

from . import jobs, pan, site, store

_lock = threading.Lock()
_thread = None
_stop = threading.Event()
_state = {"running": False, "phase": "", "done": 0, "total": 0,
          "last_run": 0, "last_msg": "", "pushed": 0, "washed": 0, "checked": 0}

# ---------------- 状态机（对齐 MP V3） ----------------
ST_NEW = "N"        # 新建，还没成功搜过
ST_RUNNING = "R"    # 订阅中
ST_PENDING = "P"    # 待定：站上暂时没有，继续轮询
ST_STOPPED = "S"    # 暂停：不参与搜索

STATE_LABEL = {ST_NEW: "新建", ST_RUNNING: "订阅中",
               ST_PENDING: "待定", ST_STOPPED: "暂停"}
_STATE_OF_ACT = {"push": ST_RUNNING, "wash": ST_RUNNING, "adopt": ST_RUNNING,
                 "same": ST_RUNNING, "baseline": ST_RUNNING,
                 "wait": ST_RUNNING, "miss": ST_PENDING, "error": ST_PENDING}


def state():
    return dict(_state)


def cfg():
    return store.settings().get("sub") or {}


# ---------------- 优先级档位（对应 V3 的过滤规则组） ----------------
# 按顺序匹配，第一条命中的档位就是这个资源的 pri_order。
# 数值越大越优先；洗版条件是新资源 pri > 已有基线。
DEFAULT_RULES = [
    {"pri": 100, "res": "2160p", "any": ["hdr", "dv", "杜比", "dolby"],
     "kw": ["remux", "原盘"]},
    {"pri": 98, "res": "2160p", "any": ["hdr", "dv", "杜比", "dolby"]},
    {"pri": 96, "res": "2160p", "kw": ["web-dl", "webdl", "web"]},
    {"pri": 95, "res": "2160p"},
    {"pri": 90, "res": "1080p", "kw": ["remux", "原盘", "blu-ray", "bluray", "bdrip"]},
    {"pri": 88, "res": "1080p", "any": ["hdr", "杜比", "dolby"]},
    {"pri": 85, "res": "1080p"},
    {"pri": 70, "res": "720p"},
    {"pri": 60},
]

_RES_RANK = {"2160p": 5, "1080i": 4, "1080p": 4, "720p": 3, "480p": 2}
_RES_FLOOR = {"2160p": 5, "1080p": 4, "720p": 3, "480p": 2}


def rules():
    """优先级档位。settings.sub.rules 可以覆盖，方便以后调档不用改代码。"""
    r = cfg().get("rules")
    if isinstance(r, list) and r:
        out = []
        for x in r:
            if isinstance(x, dict) and x.get("pri"):
                out.append({"pri": int(x.get("pri") or 0),
                            "res": str(x.get("res") or ""),
                            "any": [str(k).lower() for k in (x.get("any") or [])],
                            "kw": [str(k).lower() for k in (x.get("kw") or [])]})
        if out:
            out.sort(key=lambda x: -x["pri"])
            return out
    return DEFAULT_RULES


def pri_order(label):
    """算一个资源的优先级（V3 的 pri_order）。认不出画质给最低档 60。"""
    low = str(label or "").lower()
    q = site.quality_of(label)
    res = str(q.get("res") or "")
    for rule in rules():
        want_res = str(rule.get("res") or "")
        if want_res and res != want_res:
            continue
        any_kw = rule.get("any") or []
        if any_kw and not any(k in low for k in any_kw):
            continue
        must_kw = rule.get("kw") or []
        if must_kw and not any(k in low for k in must_kw):
            continue
        return int(rule.get("pri") or 0)
    return 0


# ---------------- 过滤（对应 V3 的 include / exclude / resolution） ----------------
def _split_kw(text):
    return [x.strip().lower() for x in re.split(r"[|,，;；]", str(text or "")) if x.strip()]


def pass_filter(label, size_bytes, s, allow_cam=False):
    """返回 (是否通过, 原因)。顺序：体积 → 包含 → 排除 → 分辨率 → 枪版。"""
    lo = float(s.get("min_gb") or 0)
    hi = float(s.get("max_gb") or 0)
    b = int(size_bytes or 0)
    if b:
        if lo and b < lo * 1024 ** 3:
            return False, f"体积 {b / 1024 ** 3:.1f}G 低于下限 {lo:g}G"
        if hi and b > hi * 1024 ** 3:
            return False, f"体积 {b / 1024 ** 3:.1f}G 超过上限 {hi:g}G"

    low = str(label or "").lower()
    inc = _split_kw(s.get("filter_inc"))
    if inc and not any(k in low for k in inc):
        return False, "不含必选关键词（" + "、".join(inc[:3]) + "）"
    exc = _split_kw(s.get("filter_exc"))
    if exc:
        hit = next((k for k in exc if k in low), "")
        if hit:
            return False, f"命中排除词「{hit}」"

    floor = _RES_FLOOR.get(str(s.get("qmin") or "").strip())
    if floor:
        rank = _RES_RANK.get(str(site.quality_of(label).get("res") or ""), 1)
        if rank < floor:
            return False, f"分辨率低于 {s.get('qmin')}"

    if not allow_cam and site.quality_of(label).get("src") == "枪版":
        return False, "枪版/抢先版不参与"
    return True, ""


# ---------------- 片名清洗 / 匹配 ----------------
_TAG_RE = re.compile(r"[\[【(（][^\]】)）]{0,80}[\]】)）]")
_PUNC_RE = re.compile(r"[\s\[\]【】()（）{}<>《》〈〉:：·,，.。!！?？'\"“”‘’\-_/\\|+&@#*~`]+")
_QUALITY_TAIL = re.compile(
    r"\b(2160p|1080p|1080i|720p|480p|4k|uhd|web-?dl|webrip|web-?rip|bluray|blu-?ray|"
    r"bdrip|brrip|remux|hdtv|hdrip|dvdrip|x265|x264|h\.?264|hevc|avc|aac|ac3|dts|"
    r"truehd|atmos|hdr10\+?|hdr|dolby|10bit|8bit)\b", re.I)


def clean_kw(text):
    """把站点标题洗成能拿去搜的片名。

    'The Odyssey (1997) S01 (480p DVD x265 10bit AC3 2.0)' → 'The Odyssey'
    纯数字片名（1917 / 2012）不会年号开头就砍掉，整条保留。
    """
    s = str(text or "")
    s = re.sub(r"\.(mp4|mkv|avi|rmvb|ts|m2ts|wmv|flv)$", " ", s, flags=re.I)
    s = _TAG_RE.sub(" ", s)
    s = re.sub(r"[._]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    m = re.search(r"\s(19|20)\d{2}\b", s)
    if m and m.start() > 0:
        s = s[:m.start()].strip(" -·|")
    q = _QUALITY_TAIL.search(s)
    if q and q.start() > 0:
        s = s[:q.start()].strip(" -·|")
    return s[:60].strip()


def _norm(s):
    return _PUNC_RE.sub(" ", str(s or "").lower()).strip()


def match_score(title, kw):
    """标题与关键词的匹配度：3 完全同名 / 2 同名前缀（续集）/ 1 包含 / 0 不相关。"""
    t, k = _norm(title), _norm(kw)
    if not t or not k:
        return 0
    if t == k:
        return 3
    if t.startswith(k):
        return 2
    if k in t:
        return 1
    return 0


def year_of(it):
    """从「2024 · 美国 · 科幻」或标题里的 (2024) 取年份。"""
    m = re.search(r"^\s*((?:19|20)\d{2})", str(it.get("sub") or ""))
    if m:
        return m.group(1)
    m = re.search(r"[\(\[（【]\s*((?:19|20)\d{2})\s*[\)\]）】]", str(it.get("title") or ""))
    return m.group(1) if m else ""


def _stat_short(stat):
    s = str(stat or "").strip()
    if not s:
        return ""
    head = s.split("/")[0].strip()
    if "完结" in s or "已完" in s:
        return (head + " 已完结")[:20]
    return head[:20]


def ended(stat):
    """True=已完结 / False=还在更新 / None=看不出来（这时不拦）。"""
    s = str(stat or "").strip()
    if not s:
        return None
    if "完结" in s or "已完" in s or "全集" in s:
        return True
    if s.startswith("全") and "集" in s:
        return True
    return False


# ---------------- 集数解析 ----------------
_EP_CN = re.compile(r"第\s*(\d{1,4})\s*(?:[-~至到]\s*(\d{1,4})\s*)?[集话話期回]")
_EP_ALL = re.compile(r"(?:全|共)\s*(\d{1,4})\s*[集话話]")
_EP_SE = re.compile(
    r"(?<![A-Za-z0-9])[Ss](\d{1,2})\s*[._\-\s]?\s*[Ee][Pp]?\s*(\d{1,4})"
    r"(?:\s*[-~]\s*(?:[Ee][Pp]?)?\s*(\d{1,4}))?(?![0-9])")
_EP_SIMPLE = re.compile(
    r"(?<![A-Za-z0-9])(?:[Ee][Pp]|EP)\s*[._\-\s]?\s*(\d{1,4})"
    r"(?:\s*[-~]\s*(?:[Ee][Pp]?)?\s*(\d{1,4}))?(?![0-9])")
_EP_DASH = re.compile(r"(?<![\dA-Za-z])[-–—]\s*(\d{1,4})(?:[vV]\d)?(?=\s|$|\[|】|\.|_)")
_EP_TOK = re.compile(r"[\[【(（]\s*([^\]】)）]{1,26}?)\s*[\]】)）]")
_EP_TOK_NUM = re.compile(
    r"^(\d{1,3})(?:\s*[-~]\s*(\d{1,3}))?\s*(?:[vV]\d|fin|end|完|合集)?$", re.I)
_EP_NOT = {480, 576, 720, 264, 265}


def _ep_ok(n):
    return 0 < n < 1900 and n not in _EP_NOT


def ep_span(label):
    """从一条磁力标题里解析出集号范围 (a, b)；单集时 a == b。认不出返回 None。"""
    s = str(label or "")
    if not s:
        return None

    m = _EP_CN.search(s)
    if m:
        a = int(m.group(1))
        b = int(m.group(2)) if m.group(2) else a
        if _ep_ok(a) and _ep_ok(b) and b >= a:
            return (a, b)

    m = _EP_ALL.search(s)
    if m:
        n = int(m.group(1))
        if _ep_ok(n):
            return (1, n)

    m = _EP_SE.search(s)
    if m:
        a = int(m.group(2))
        b = int(m.group(3)) if m.group(3) else a
        if _ep_ok(a) and _ep_ok(b) and b >= a:
            return (a, b)

    m = _EP_SIMPLE.search(s)
    if m:
        a = int(m.group(1))
        b = int(m.group(2)) if m.group(2) else a
        if _ep_ok(a) and _ep_ok(b) and b >= a:
            return (a, b)

    # 括号里先看：范围写法要排在单值之前，否则 [01-16 Fin] 只会取到 16
    for tok in _EP_TOK.findall(s):
        t = tok.strip()
        m = _EP_TOK_NUM.match(t)
        if m:
            a = int(m.group(1))
            b = int(m.group(2)) if m.group(2) else a
            if _ep_ok(a) and _ep_ok(b) and b >= a:
                return (a, b)

    for m in _EP_DASH.finditer(s):
        n = int(m.group(1))
        if _ep_ok(n):
            return (n, n)
    return None


def stat_ep(stat):
    """解析条目的更新状态：「更新至 第39集 / 共52集」「全40集」「12集 / 已完结」。"""
    s = str(stat or "")
    cur = total = 0
    m = re.search(r"第\s*(\d{1,4})\s*集", s)
    if m:
        cur = int(m.group(1))
    m2 = re.search(r"共\s*(\d{1,4})\s*集", s)
    if m2:
        total = int(m2.group(1))
    m3 = re.search(r"(?:全|共)\s*(\d{1,4})\s*集", s)
    if m3 and not total:
        total = int(m3.group(1))
    if not cur:
        m4 = re.match(r"^\s*(\d{1,4})\s*集", s)
        if m4:
            cur = int(m4.group(1))
    if not total:
        m5 = re.search(r"更新至\s*(\d{1,4})", s)
        if m5 and not cur:
            cur = int(m5.group(1))
    return {"cur": cur, "total": total, "done": bool(ended(s))}


def parse_ep_done(text):
    """兼容老字段 ep_done：「39=btih,40=btih」→ {39: btih, ...}"""
    out = {}
    for part in str(text or "").split(","):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        try:
            out[int(k)] = v.strip()
        except ValueError:
            continue
    return out


def dump_ep_done(m, limit=160):
    items = sorted((int(k), str(v)) for k, v in (m or {}).items())[-int(limit):]
    return ",".join(f"{k}={v}" for k, v in items)


# ---------------- note / episode_priority（V3 的按集事实） ----------------
import json


def _load_list(txt):
    try:
        v = json.loads(txt or "[]")
        return [int(x) for x in v] if isinstance(v, list) else []
    except Exception:
        return []


def _load_map(txt):
    try:
        v = json.loads(txt or "{}")
        if not isinstance(v, dict):
            return {}
        out = {}
        for k, x in v.items():
            try:
                out[int(k)] = int(x)
            except (TypeError, ValueError):
                continue
        return out
    except Exception:
        return {}


def note_episodes(s):
    """已下载集号集合。note 是 V3 的主事实，ep_done 作为老库兜底。"""
    got = set(_load_list(s.get("note")))
    got |= set(parse_ep_done(s.get("ep_done")).keys())
    return got


def ep_priority(s):
    return _load_map(s.get("episode_priority"))


def compute_lack(s, total=None, start=None):
    """缺集数 = 目标范围 − 已下载集（V3 的 compute_lack_episode）。"""
    if str(s.get("cat") or "") not in ("tv", "ac"):
        return 0
    total = int(total if total is not None else (s.get("total_episode") or 0))
    if not total:
        return 0
    start = int(start if start is not None else (s.get("start_episode") or 1))
    if total < start:
        return 0
    target = set(range(start, total + 1))
    got = note_episodes(s) | {e for e, p in ep_priority(s).items() if p > 0}
    return len(target - (got & target))


# ---------------- 挑候选 ----------------
def _pick(s, magnets, pool=None, bad=None):
    """按 V3 的语义挑：先过滤死种，再取 pri 最高的；同 pri 取体积大的。

    死种黑名单必须在这里过滤：否则 45 分钟判死 → 回队 → 又挑回同一个种，
    永远卡在「推了死、死了推」的循环里，网盘兜底也永远轮不到。
    返回 (best, why)。best 里带 _pri（算好的优先级）。
    """
    if bad is None:
        try:
            bad = store.bad_hashes()
        except Exception:
            bad = set()
    cand, reasons, dead = [], [], 0
    for m in (magnets or []):
        label = m.get("label") or m.get("url") or ""
        h = str(m.get("btih") or "").lower()
        if h and h in bad:
            dead += 1
            continue
        ok, why = pass_filter(label, m.get("size_bytes"), s)
        if not ok:
            if why:
                reasons.append(why)
            continue
        q = site.quality_of(label)
        cand.append((int(pri_order(label)), int(m.get("size_bytes") or 0), m, q))
    if not cand:
        if dead:
            return None, f"{dead} 条磁力都在死种黑名单里"
        return None, (reasons[0] if reasons else "没有符合规则的片源")

    top = max(c[0] for c in cand)
    cand = [c for c in cand if c[0] == top]
    if pool and store.settings()["auto"].get("prefer_sec", True):
        hot = [c for c in cand if str(c[2].get("btih") or "").lower() in pool]
        if hot:
            cand = hot
    pri, _sz, m, q = max(cand, key=lambda x: (x[0], x[1]))
    best = dict(m)
    best["quality"] = q
    best["_pri"] = pri
    return best, ""


# ---------------- 网盘兜底（磁力全死时用） ----------------
def _pan_open(s, it, info, P, want_eps=None, last_ep=0, force=False):
    """从站点的 115 网盘分享里转一份到落地的 115 目录。

    115 的「转存」是服务端复制：不经过本机、不占带宽，几十 G 也是秒级。
    逐条验活：分享死得比磁力还快，死链直接进黑名单，下次不再试。
    force=True 时忽略「pan_fallback」开关（手动指定路线用）。

    返回 ((cand, result), "") 或 (None, 失败原因)。
    """
    scfg = cfg()
    if not force and not scfg.get("pan_fallback", True):
        return None, ""
    pans = [p for p in (info.get("pans") or []) if p.get("is_115")]
    if not pans:
        return None, "站点这条没有 115 网盘资源"
    bad = store.pan_bad_urls()
    cands = [p for p in pans if p.get("url") not in bad]
    if not cands:
        return None, f"{len(pans)} 条 115 分享都验过且失效"

    ok_c, why_not = [], ""
    for p in cands:
        ok, why = pass_filter(p.get("name") or "", 0, s)
        if ok:
            ok_c.append(p)
        elif not why_not:
            why_not = why
    if not ok_c:
        return None, "115 分享不符合规则：" + why_not

    if want_eps:
        want = {int(x) for x in want_eps}
        lo, hi = min(want), max(want)

        def key(p):
            sp = ep_span(p.get("name"))
            covers = 1 if sp and sp[1] >= hi and sp[0] <= lo else 0
            near = 1 if sp and sp[1] >= lo else 0
            span = (sp[1] - sp[0]) if sp else 999
            return (-covers, -near, span, -pri_order(p.get("name")), int(p.get("e") or 0))
    else:
        def key(p):
            return (int(p.get("e") or 0), -pri_order(p.get("name")),
                    -int(p.get("heart") or 0))
    ok_c.sort(key=key)

    cid = jobs._save_path(store.settings(), it.get("cat"))
    if not cid:
        return None, "还没设置 115 保存目录（设置 → 115 网盘）"
    # 落进「片名 (年份)」子目录：网盘分享里的单集文件常叫 01.mkv，
    # 直接扔进 /离线 根目录，几百个文件混在一起，后面根本没法找。
    dname = clean_kw(it.get("title") or "")
    sub_cid = ""
    if dname:
        y = year_of(it)
        sub_cid = P.mkdir_at(cid, f"{dname} ({y})" if y else dname)
        if sub_cid:
            cid = sub_cid

    def _drop_empty_dir():
        """全都没转成时，把刚建的空目录收掉，别在整理目录里留一堆空壳。"""
        if not sub_cid:
            return
        try:
            ch = P.files(cid=int(sub_cid), limit=5)
            if not (ch.get("items") or []):
                P.delete_file(sub_cid)
        except Exception:
            pass

    allow_pack = bool(scfg.get("pan_allow_pack", True))
    tried = 0
    for p in ok_c:
        if tried >= int(scfg.get("pan_max_try") or 4):
            break
        tried += 1
        code = pan.parse_share(p.get("url"))[0]
        if code and store.already_pushed("pan:" + code):
            return (p, {"ok": True, "code": code, "cached": True, "eps": [],
                        "pack": False, "n": 0, "size": 0}), ""
        r = P.share_pull(p.get("url"), p.get("pwd") or "", cid=cid,
                         want_eps=want_eps, allow_pack=allow_pack)
        if r.get("ok"):
            return (p, r), ""
        msg = str(r.get("msg") or "")
        if r.get("rate_limited"):
            _drop_empty_dir()
            return None, msg
        if (r.get("dead") or r.get("bad_pwd") or r.get("need_pwd")
                or any(k in msg for k in ("不存在", "已删除", "取消", "失效", "违规", "访问码"))):
            store.mark_pan_bad(p.get("url"), msg)
        why_not = msg or why_not
    _drop_empty_dir()
    tail = (why_not or "未知")[:50]
    return None, f"{tried} 条 115 分享都转不了：{tail}"


def _pan_push(s, it, info, P, why="", force=False):
    """网盘兜底（电影 / 整季一次性）：成功返回 action，失败返回 None。

    失败原因挂在 info["_pan_msg"] 上，由调用方拼进订阅提示。
    force=True 时忽略自动兜底开关（手动按钮用）。
    """
    cand_r, msg = _pan_open(s, it, info, P, force=force)
    if not cand_r:
        if msg:
            info["_pan_msg"] = msg
        return None
    cand, r = cand_r
    p, code = cand, r.get("code") or ""
    key = "pan:" + code
    size = int(r.get("size") or 0)
    size_txt = site.human_size(size) or "网盘资源"
    name = str(p.get("name") or "")[:26]
    pri = pri_order(p.get("name") or "")
    if r.get("cached") or store.already_pushed(key):
        # 之前转过：把条目/订阅标成「已推送」，否则自动推送队列里这条
        # 会一直挂着（pushed=0），每轮都来问一次「要不要转存」。
        store.mark_pushed(it["id"], it.get("best_size") or "", key,
                          int(it.get("best_bytes") or 0), int(it.get("qscore") or 0))
        return {"action": "adopt", "item": it,
                "msg": f"之前已用 115 网盘转存过（{name}），不再重复",
                "patch": {"state": ST_RUNNING, "media_source": "pan115", "btih": key}}
    store.upsert_item(it, mark_new=False)
    store.mark_pushed(it["id"], size_txt, key, size, 0)
    store.mark_pushed_global(key, it["id"], it["title"])
    store.add_task(it["id"], f"{it['title']} · 订阅 · 115 网盘", p.get("url") or "",
                   size_txt, size, "done", "115 网盘转存")
    eps = r.get("eps") or []
    tail = f"（{len(eps)} 集）" if eps else ""
    return {"action": "push", "item": it,
            "msg": f"磁力没源，已改用 115 网盘转存：{size_txt}{tail} · {name}",
            "patch": {"item_id": it["id"], "btih": key, "size_text": size_txt,
                      "size_bytes": size, "qscore": 0, "current_priority": pri,
                      "state": ST_RUNNING, "media_source": "pan115", "media_id": code}}


def _pan_follow(s, it, info, P, last_ep, limit=3):
    """追更的网盘兜底：只转「还没看的那几集」，分享里只有整包时才整包转。"""
    want = list(range(int(last_ep) + 1, int(last_ep) + 1 + max(1, int(limit))))
    cand_r, msg = _pan_open(s, it, info, P, want_eps=want, last_ep=last_ep)
    if not cand_r:
        if msg:
            info["_pan_msg"] = msg
        return None
    cand, r = cand_r
    p, code = cand, r.get("code") or ""
    key = "pan:" + code
    if r.get("cached"):
        store.mark_pushed_only(it["id"])
        return {"action": "adopt", "item": it,
                "msg": f"这一集之前已用 115 网盘转过（{str(p.get('name') or '')[:24]}）",
                "patch": {"state": ST_RUNNING, "media_source": "pan115", "btih": key}}
    got = [e for e in (r.get("eps") or []) if int(e) > int(last_ep)]
    if not got:
        if not r.get("pack"):
            return None
        # 整包但认不出集号：按这一轮想补的上限推进，避免每轮重复转同一个包
        got = [max(want)]
    new_last = max(got)
    size = int(r.get("size") or 0)
    size_txt = site.human_size(size) or "网盘"
    pri = pri_order(p.get("name") or "")
    ep_map = ep_priority(s)
    done_map = parse_ep_done(s.get("ep_done"))
    for e in got:
        done_map[int(e)] = key
        ep_map[int(e)] = max(int(ep_map.get(int(e), 0)), pri)
    patch = {"last_ep": new_last, "ep_done": dump_ep_done(done_map),
             "episode_priority": json.dumps({str(k): int(v) for k, v in ep_map.items()},
                                            ensure_ascii=False),
             "btih": key, "size_text": size_txt, "size_bytes": size, "qscore": 0,
             "current_priority": pri, "state": ST_RUNNING,
             "media_source": "pan115", "media_id": code}
    patch["note"] = json.dumps(sorted(note_episodes(s) | set(int(x) for x in got)),
                               ensure_ascii=False)
    t_total = int(s.get("total_episode") or 0)
    t_start = int(s.get("start_episode") or 1)
    patch["lack_episode"] = compute_lack(dict(s, **patch), total=t_total, start=t_start)

    store.upsert_item(it, mark_new=False)
    store.mark_pushed_only(it["id"])
    store.mark_pushed_global(key, it["id"], it["title"])
    store.add_task(it["id"], f"{it['title']} · 第{new_last}集 · 订阅 · 115 网盘",
                   p.get("url") or "", size_txt, size, "done", "115 网盘转存")
    tag = "整包" if r.get("pack") and len(got) > 1 else f"第{new_last}集"
    return {"action": "push", "item": it,
            "msg": f"磁力没源，已改用 115 网盘转存{tag}（{size_txt}）", "patch": patch}


def _pri_of_pushed(s):
    """已推版本的优先级基线。老库没有 current_priority，用质量分反推一次。"""
    p = int(s.get("current_priority") or 0)
    if p:
        return p
    return pri_order(s.get("size_text") or "")


def _has_version(s):
    """订阅里是否已经有一个推出去过的版本（磁力或网盘都算）。

    网盘兜底只在「什么都没有」时接手。已经有一版还去转存，媒体库里会多出
    一份重复剧集；种被判死时 jobs 会调 sub_forget_hash 把 btih 清掉，
    所以这里 btih 非空 = 这一版还活着（网盘版的 key 是 pan:<分享码>）。
    """
    return bool(str(s.get("btih") or "").strip())


# ---------------- 追更：一集一集往下推 ----------------
def _follow_one(s, it, info, P, pool, scfg=None):
    """剧集/动漫追更：只推「比已推进度更新的单集」。

    为什么只认单集：整季合集包会把已经下过的集数再下一遍，白占 115 空间。
    认不出集号的种一律不动 —— 宁可这轮不推，也不把花絮/预告当正片推。

    和 V3 对齐的部分：每一集有自己的 episode_priority，新集的 pri 会记进去；
    同一集出现更高 pri 的版本时只做提示，不自动删旧文件（我们管不到单集文件）。
    """
    scfg = scfg or {}
    stat = str(info.get("stat") or "")
    se = stat_ep(stat)
    cur, total, done_flag = se["cur"], se["total"], se["done"]
    magnets = info.get("magnets") or []
    last_ep = int(s.get("last_ep") or 0)
    ep_from = int(s.get("ep_from") or 0)
    done_map = parse_ep_done(s.get("ep_done"))
    ep_pri = ep_priority(s)
    best_version = bool(int(s.get("best_version") or 0))

    # 第一轮先定基线：默认「只追新」，不会把订阅前的几十集全下一遍
    if last_ep <= 0:
        if ep_from > 0:
            last_ep = max(0, ep_from - 1)
        elif cur > 0:
            # 基线集 = 当前站点进度；订阅目标从「下一集」算起，
            # 这样缺集统计不会把订阅前的老集当成「缺」。
            return {"action": "baseline", "item": it,
                    "msg": f"已记录进度：第{cur}集" + (f"（共{total}集）" if total else "")
                           + "，之后每更新一集自动下载",
                    "patch": {"last_ep": cur, "total_episode": total or cur,
                              "start_episode": ep_from or (cur + 1),
                              "lack_episode": compute_lack(
                                  dict(s, note=json.dumps([]), total_episode=total or cur,
                                       start_episode=ep_from or (cur + 1)))}}

    by_ep, packs = {}, []
    for m in magnets:
        label = m.get("label") or m.get("url") or ""
        sp = ep_span(label)
        if not sp:
            continue
        a, b = sp
        if b <= last_ep:
            continue
        if a == b:
            by_ep.setdefault(b, []).append(m)
        else:
            packs.append((a, b, m))

    limit = max(1, int(scfg.get("max_ep_per_run") or 3))
    if not by_ep:
        # 磁力一条能用的都没有 → 先试 115 网盘分享（只转缺的那几集）
        pr = _pan_follow(s, it, info, P, last_ep, limit)
        if pr:
            return pr
        pan_note = ("（" + info["_pan_msg"] + "）") if info.get("_pan_msg") else ""
        if not last_ep:
            return {"action": "wait", "item": it,
                    "msg": "站点还没有能认出集号的单集种（追更只下单集，不整包重下）" + pan_note}
        if packs:
            a, b, _m = max(packs, key=lambda x: x[1])
            return {"action": "wait", "item": it,
                    "msg": f"已追到第{last_ep}集；站点只有合集包（{a}-{b}集），"
                           f"追更不整包重下，等单集种" + pan_note}
        if done_flag and cur >= last_ep:
            return {"action": "wait", "item": it,
                    "msg": f"已完结（共{total or cur}集），第{last_ep}集之后的单集种站点还没放出来"
                           + pan_note}
        if cur and cur > last_ep:
            return {"action": "wait", "item": it,
                    "msg": f"站点已更新到第{cur}集，但还没有第{last_ep + 1}集的下载种" + pan_note}
        return {"action": "same", "item": it,
                "msg": f"已追到第{last_ep}集" + (f" / 共{total}集" if total else "")
                       + "，暂无更新" + pan_note}

    pushed, failed, notes, skipped = [], [], [], []
    new_btih = new_size = ""
    new_bytes = new_q = 0
    try:
        bad = store.bad_hashes()
    except Exception:
        bad = set()
    for e in sorted(by_ep)[:limit]:
        best, why = _pick(s, by_ep[e], pool, bad)
        if not best:
            failed.append(f"第{e}集：{why}")
            continue
        btih = str(best.get("btih") or "").lower()
        if not btih:
            continue
        pri = int(best.get("_pri") or 0)
        have = ep_pri.get(e, 0)
        if best_version and have and pri <= have:
            skipped.append(f"第{e}集已有 pri {have} 的版本")
            continue
        if btih in done_map.values() or store.already_pushed(btih):
            done_map[e] = btih
            ep_pri[e] = max(have, pri)
            last_ep = max(last_ep, e)
            notes.append(f"第{e}集已推过")
            continue
        size_txt = best.get("size_text") or site.human_size(best.get("size_bytes"))
        nq = int((best.get("quality") or {}).get("q") or 0)
        nb = int(best.get("size_bytes") or 0)
        res = P.add_magnet(best["url"], jobs._save_path(store.settings(), it.get("cat")))
        if not res.get("ok"):
            if not res.get("rate_limited"):
                # 拉黑这条，下一轮才会去挑别的种 / 走网盘，而不是原地重试
                store.mark_bad(btih, "115 提交失败")
            failed.append(f"第{e}集提交失败：{res.get('msg') or '未知'}")
            break
        sec = (jobs._probe_sec(P, btih)
               if store.settings()["auto"].get("prefer_sec", True) else None)
        store.upsert_item(it, mark_new=False)
        store.mark_pushed_only(it["id"])
        store.mark_pushed_global(btih, it["id"], it["title"])
        store.add_task(it["id"], f"{it['title']} · 第{e}集 · 订阅", best["url"],
                       size_txt, nb, "sent", "订阅追更" + (" · 秒传" if sec else ""))
        done_map[e] = btih
        ep_pri[e] = max(have, pri)
        last_ep = max(last_ep, e)
        new_btih, new_size, new_bytes, new_q = btih, size_txt, nb, nq
        pushed.append(f"第{e}集 {size_txt}")

    t_total = total or int(s.get("total_episode") or 0)
    t_start = ep_from or int(s.get("start_episode") or 1)
    patch = {"last_ep": last_ep, "ep_done": dump_ep_done(done_map),
             "episode_priority": json.dumps({str(k): int(v) for k, v in ep_pri.items()},
                                            ensure_ascii=False),
             "total_episode": t_total, "start_episode": t_start}
    patch["note"] = json.dumps(sorted(note_episodes(s) | set(done_map)), ensure_ascii=False)
    patch["lack_episode"] = compute_lack(dict(s, **patch), total=t_total, start=t_start)
    if new_btih:
        patch.update(btih=new_btih, size_text=new_size,
                     size_bytes=new_bytes, qscore=new_q)

    if pushed:
        msg = "已推送 " + "、".join(pushed)
        if notes:
            msg += "（" + "、".join(notes) + "）"
        return {"action": "push", "item": it, "msg": msg, "patch": patch}
    if failed:
        # 有单集种但都被判死/不合规则 → 网盘兜底补这一集
        pr = _pan_follow(s, it, info, P, last_ep, 1)
        if pr:
            return pr
        extra = ("；" + info["_pan_msg"]) if info.get("_pan_msg") else ""
        return {"action": "wait", "item": it,
                "msg": (f"第{last_ep + 1}集有片源但没通过规则：" + failed[0] + extra)[:160],
                "patch": patch}
    if skipped and not notes:
        return {"action": "same", "item": it,
                "msg": "、".join(skipped[:2]) + "，无需洗版", "patch": patch}
    return {"action": "same", "item": it,
            "msg": f"已追到第{last_ep}集" + (f" / 共{total}集" if total else ""),
            "patch": patch}


# ---------------- 单个订阅检查 ----------------
def _local_pushed(kw, cat, year):
    """本地库里已推送、名字对得上的条目（避免重复下载）。"""
    try:
        rows = store.pushed_like(kw, cat)
    except Exception:
        return None
    best = None
    for r in rows:
        if not r.get("best_btih"):
            continue
        if cat and r.get("cat") != cat:
            continue
        score = match_score(r.get("title"), kw)
        if score < 1:
            continue
        y = year_of({"sub": r.get("sub") or "", "title": r.get("title") or ""})
        if year and y and y != year:
            continue
        if best is None or int(r.get("best_bytes") or 0) > int(best.get("best_bytes") or 0):
            best = r
    return best


def check_one(s, sc, P, pool, scfg=None):
    """检查一个订阅。返回 {action, msg, item?, patch?}，不直接写订阅表。

    action: push 首次推送 / wash 洗版 / adopt 该种已推过只是认领 /
            same 已是最佳 / wait 等片源或等完结 / miss 站点没有 / error 出错
    """
    kw = str(s.get("kw") or "").strip()
    if not kw:
        return {"action": "error", "msg": "关键词为空"}

    try:
        items = sc.search(kw)
    except site.SiteError as e:
        if str(e) == "NEED_LOGIN":
            raise
        return {"action": "error", "msg": f"搜索失败: {e}"[:120]}
    except Exception as e:
        return {"action": "error", "msg": f"搜索异常: {type(e).__name__}: {e}"[:120]}

    want_cat = str(s.get("cat") or "")
    want_year = str(s.get("year") or "").strip()
    cands = []
    kw_hits = 0
    for it in items or []:
        if want_cat and it.get("cat") != want_cat:
            continue
        score = match_score(it.get("title"), kw)
        if not score:
            continue
        kw_hits += 1
        y = year_of(it)
        if want_year and y and y != want_year:
            continue
        if "预告" in str(it.get("stat") or ""):
            continue
        cands.append((score, y, it))
    if not cands:
        if kw_hits:
            return {"action": "miss",
                    "msg": f"片名对上了，但没有 {want_year} 年的版本" if want_year
                           else "片名对上了，但都是预告/没有可用条目"}
        if items:
            return {"action": "miss",
                    "msg": f"搜到 {len(items)} 条，但片名对不上（可能是译名）——"
                           f"用「先搜候选看看」选一条再订阅"}
        return {"action": "miss", "msg": "站点上还没找到这部片子"}

    # 完全同名优先；同分时年份对得上的优先；再不行取年份新的
    cands.sort(key=lambda x: (-x[0],
                              0 if (want_year and x[1] == want_year) else 1,
                              -int(x[1] or 0)))

    note = ""
    for score, y, it in cands[:3]:
        try:
            info = sc.detail_safe(it["url"], it.get("cat") or "", tries=2)
        except Exception as e:
            note = f"详情抓取失败: {str(e)[:80]}"
            continue

        dy = str(info.get("year") or "").strip()
        if want_year and dy and dy != want_year:
            note = f"年份不符（站点是 {dy}）"
            continue

        stat = str(info.get("stat") or "")
        it["stat"] = _stat_short(stat)
        magnets = info.get("magnets") or []
        se = stat_ep(stat)

        em = str(s.get("ep_mode") or "")
        if it.get("cat") in ("tv", "ac") and em == "follow":
            if not (se["done"] and not int(s.get("last_ep") or 0)):
                return _follow_one(s, it, info, P, pool, scfg)

        if (it.get("cat") in ("tv", "ac")
                and em != "all"
                and s.get("wait_end") and stat):
            if ended(stat) is False:
                return {"action": "wait", "item": it,
                        "msg": f"更新中（{stat.split('/')[0].strip()}），等完结再推"}

        if not magnets:
            # 没有磁力：网盘兜底（电影/整季一次性最适合走这条路）
            if _has_version(s):
                return {"action": "same", "item": it,
                        "msg": f"已有版本（{s.get('size_text') or '已推送'}），"
                               f"站点磁力失效但这版不受影响，不重复下载"}
            pr = _pan_push(s, it, info, P, why="这条没有磁力")
            if pr:
                return pr
            note = "这条还没有磁力" + (f"；{info['_pan_msg']}" if info.get("_pan_msg") else "")
            continue

        best, why = _pick(s, magnets, pool)
        if not best:
            # 磁力全死 / 全不合规则：换 115 网盘分享转存
            if _has_version(s):
                return {"action": "same", "item": it,
                        "msg": f"已有版本（{s.get('size_text') or '已推送'}），"
                               f"当前磁力都失效但这版还在，不重复下载"}
            pr = _pan_push(s, it, info, P, why=why)
            if pr:
                return pr
            if info.get("_pan_msg"):
                why = f"{why}；{info['_pan_msg']}"
            return {"action": "wait", "item": it, "msg": why[:160]}

        btih = str(best.get("btih") or "").lower()
        if not btih:
            note = "磁力缺 infohash"
            continue
        size_txt = best.get("size_text") or site.human_size(best.get("size_bytes"))
        new_q = int((best.get("quality") or {}).get("q") or 0)
        new_b = int(best.get("size_bytes") or 0)
        new_pri = int(best.get("_pri") or 0)
        qtext = (best.get("quality") or {}).get("text") or ""
        cur_btih = str(s.get("btih") or "").lower()
        base_pri = _pri_of_pushed(s)

        # 已经是这个种：什么都不用做
        if cur_btih and btih == cur_btih:
            return {"action": "same", "item": it,
                    "msg": f"已是最佳版本（{size_txt}{' · ' + qtext if qtext else ''}）",
                    "patch": {"current_priority": max(base_pri, new_pri),
                              "state": ST_RUNNING}}

        # 已推过、但站点上出现了别的版本 → 只有 pri 更高才洗
        if cur_btih:
            if not int(s.get("best_version") or 0):
                return {"action": "same", "item": it,
                        "msg": f"有别的版本（{size_txt}），自动洗版已关",
                        "patch": {"state": ST_RUNNING}}
            if new_pri <= base_pri:
                return {"action": "same", "item": it,
                        "msg": f"已有版本优先级 {base_pri}，新版本 {new_pri} 不更高，不洗",
                        "patch": {"state": ST_RUNNING}}
            r = jobs.wash_one(s.get("item_id") or it["id"], btih=btih, delete_old=True)
            if not r.get("ok"):
                return {"action": "error", "item": it,
                        "msg": f"洗版失败: {r.get('msg') or '未知'}"[:150]}
            return {"action": "wash", "item": it,
                    "msg": f"已洗版 {r.get('old')} → {r.get('new')}（{qtext}，"
                           f"优先级 {base_pri}→{new_pri}）",
                    "patch": {"btih": btih, "size_text": size_txt,
                              "size_bytes": new_b, "qscore": new_q,
                              "current_priority": new_pri, "state": ST_RUNNING}}

        # 本地库里可能早就推过这部片，别再下一份
        if not cur_btih:
            old = _local_pushed(kw, want_cat, want_year)
            if old:
                old_pri = pri_order(old.get("best_size") or "")
                ob = int(old.get("best_bytes") or 0)
                if (int(s.get("best_version") or 0) and old_pri
                        and new_pri > old_pri):
                    r = jobs.wash_one(old["id"], btih=btih, delete_old=True)
                    if r.get("ok"):
                        return {"action": "wash", "item": old,
                                "msg": f"库里已有旧版本，已洗版 {r.get('old')} → {r.get('new')}"
                                       f"（优先级 {old_pri}→{new_pri}）",
                                "patch": {"item_id": old["id"], "title": old.get("title") or "",
                                          "btih": btih, "size_text": size_txt,
                                          "size_bytes": new_b, "qscore": new_q,
                                          "current_priority": new_pri, "state": ST_RUNNING}}
                return {"action": "adopt", "item": old,
                        "msg": f"库里已有这部片（{old.get('best_size') or '已推送'}），不再重复下载",
                        "patch": {"item_id": old["id"], "title": old.get("title") or "",
                                  "btih": old.get("best_btih") or "",
                                  "size_text": old.get("best_size") or "",
                                  "size_bytes": ob,
                                  "current_priority": old_pri or new_pri,
                                  "state": ST_RUNNING}}

        # 同一个种可能已经被「最新更新」那条链路推过了，只认领
        if store.already_pushed(btih):
            store.upsert_item(it, mark_new=False)
            store.mark_pushed(it["id"], size_txt, btih, new_b, new_q)
            return {"action": "adopt", "item": it,
                    "msg": f"这个版本之前已经推过（{size_txt}），已记录",
                    "patch": {"item_id": it["id"], "btih": btih, "size_text": size_txt,
                              "size_bytes": new_b, "qscore": new_q,
                              "current_priority": new_pri, "state": ST_RUNNING}}

        res = P.add_magnet(best["url"], jobs._save_path(store.settings(), it.get("cat")))
        if not res.get("ok"):
            # 磁力提交失败（115 不认 / 该 hash 被判死）→ 换网盘
            if not res.get("rate_limited"):
                store.mark_bad(btih, "115 提交失败")
                if not _has_version(s):
                    pr = _pan_push(s, it, info, P, why=res.get("msg") or "")
                    if pr:
                        return pr
            return {"action": "error", "item": it,
                    "msg": f"提交 115 失败: {res.get('msg') or '未知'}"[:150]}

        sec = jobs._probe_sec(P, btih) if store.settings()["auto"].get("prefer_sec", True) else None
        store.upsert_item(it, mark_new=False)
        store.mark_pushed(it["id"], size_txt, btih, new_b, new_q)
        store.mark_pushed_global(btih, it["id"], it["title"])
        store.add_task(it["id"], f"{it['title']} · 订阅", best["url"], size_txt, new_b,
                       "sent", "订阅推送" + (" · 秒传" if sec else ""))
        patch = {"item_id": it["id"], "btih": btih, "size_text": size_txt,
                 "size_bytes": new_b, "qscore": new_q,
                 "current_priority": new_pri, "state": ST_RUNNING,
                 "media_id": it["id"], "media_source": "site"}
        if it.get("cat") in ("tv", "ac") and se["total"]:
            patch.update(total_episode=se["total"],
                         start_episode=int(s.get("start_episode") or 1))
            patch["lack_episode"] = compute_lack(
                dict(s, **patch), total=se["total"],
                start=int(s.get("start_episode") or 1))
        return {"action": "push", "item": it,
                "msg": f"{size_txt}{' · ' + qtext if qtext else ''}（优先级 {new_pri}）"
                       + ("　⚡ 秒传" if sec else ""),
                "patch": patch}

    return {"action": "miss", "msg": note or "没找到可用的磁力"}


# ---------------- 跑一轮 ----------------
def _run_all(ids=None):
    if not _lock.acquire(blocking=False):
        return {"ok": False, "msg": "正在检查中"}
    _state.update({"running": True, "phase": "准备", "done": 0, "total": 0,
                   "pushed": 0, "washed": 0, "checked": 0})
    t0 = time.time()
    try:
        all_cfg = store.settings()
        scfg = all_cfg.get("sub") or {}
        if ids:
            subs = [x for x in (store.sub_get(i) for i in ids) if x]
        else:
            subs = store.subs_due(limit=int(scfg.get("max_per_run") or 20))
        # 每个订阅可以有自己的 search_interval（小时），0 = 跟随全局
        now = int(time.time())
        due = []
        for s in subs:
            iv = int(s.get("search_interval") or 0)
            if iv and now - int(s.get("last_search") or 0) < iv * 3600:
                continue
            due.append(s)
        subs = due
        if not subs:
            _state.update(last_msg="没有待检查的订阅", last_run=int(time.time()))
            return {"ok": True, "checked": 0, "msg": "没有待检查的订阅"}

        sc = site.SiteClient()
        if not sc.logged_in() and not sc.check_login(ttl=0).get("ok"):
            store.log("订阅检查跳过：站点还没登录", "warn")
            _state["last_msg"] = "站点未登录"
            return {"ok": False, "msg": "站点未登录"}
        P = pan.Pan115()
        if not P.logged_in():
            store.log("订阅检查跳过：115 还没登录", "warn")
            _state["last_msg"] = "115 未登录"
            return {"ok": False, "msg": "115 未登录"}

        keep = float(all_cfg["auto"].get("min_free_gb") or 0)
        if keep > 0:
            free = jobs._free_gb(P)
            if free is not None and free < keep:
                store.log(f"订阅检查跳过：115 空间只剩 {free:.0f}G（保护线 {keep:.0f}G）", "warn")
                _state["last_msg"] = "115 空间不足"
                return {"ok": False, "msg": "115 空间不足"}

        pool = jobs._sec_pool(P) if all_cfg["auto"].get("prefer_sec", True) else set()
        delay = max(1.5, float(scfg.get("delay") or 5))
        pushed = washed = 0
        for n, s in enumerate(subs):
            if _stop.is_set():
                break
            _state.update({"phase": f"检查 {str(s.get('kw') or '')[:16]}",
                           "done": n, "total": len(subs)})
            try:
                r = check_one(s, sc, P, pool, scfg)
            except site.SiteError as e:
                r = {"action": "error",
                     "msg": "站点登录失效" if str(e) == "NEED_LOGIN" else str(e)[:120]}
            except Exception as e:
                r = {"action": "error", "msg": f"{type(e).__name__}: {e}"[:120]}

            act = r.get("action") or "error"
            msg = r.get("msg") or ""
            item = r.get("item") or {}
            patch = dict(r.get("patch") or {})
            # V3 状态机：动作决定状态，显式给了就用显式的
            new_state = patch.pop("state", None) or _STATE_OF_ACT.get(act, ST_PENDING)
            status = {"push": "pushed", "wash": "pushed", "adopt": "pushed",
                      "same": "pushed", "baseline": "waiting", "wait": "waiting",
                      "miss": "waiting", "error": "error"}.get(act, "waiting")
            if act in ("push", "wash", "adopt"):
                patch["hits"] = int(s.get("hits") or 0) + 1
            patch.setdefault("state", new_state)
            patch["last_search"] = int(time.time())
            patch["last_update"] = int(time.time())
            store.sub_touch(s.get("id"), status=status, last_msg=msg, **patch)

            title = item.get("title") or s.get("kw") or ""
            if act == "push":
                pushed += 1
                store.log(f"⭐ 订阅推送: {title} —— {msg}", "ok")
                store.sub_log(s.get("id"), s.get("kw"), "push", msg)
            elif act == "wash":
                washed += 1
                store.log(f"🔄 订阅洗版: {title} —— {msg}", "ok")
                store.sub_log(s.get("id"), s.get("kw"), "wash", msg)
            elif act == "adopt":
                store.sub_log(s.get("id"), s.get("kw"), "adopt", msg)
            elif act == "baseline":
                store.sub_log(s.get("id"), s.get("kw"), "ep", msg)
            elif act == "error":
                if msg != str(s.get("last_msg") or ""):
                    store.log(f"订阅检查失败: {s.get('kw')} —— {msg}", "warn")
                    store.sub_log(s.get("id"), s.get("kw"), "fail", msg)
            elif msg and msg != str(s.get("last_msg") or ""):
                ev = {"miss": "miss", "wait": "wait"}.get(act, "hit")
                store.sub_log(s.get("id"), s.get("kw"), ev, msg)

            if n and n % 8 == 0:
                sc.rotate_session()
                time.sleep(1.2)
            time.sleep(delay * random.uniform(0.85, 1.4))

        _state.update({"done": len(subs), "checked": len(subs), "pushed": pushed,
                       "washed": washed, "last_run": int(time.time()),
                       "last_msg": f"检查 {len(subs)} 个 · 推送 {pushed} · 洗版 {washed}"})
        if pushed or washed:
            store.log(f"订阅检查完成：{len(subs)} 个订阅，推送 {pushed}，洗版 {washed}", "ok")
        store.set_kv("sub_last_run", {"ts": int(time.time()), "n": len(subs),
                                      "pushed": pushed, "washed": washed})
        return {"ok": True, "checked": len(subs), "pushed": pushed,
                "washed": washed, "cost": round(time.time() - t0, 1)}
    except Exception as e:
        store.log("订阅检查异常: " + traceback.format_exc()[-700:], "error")
        _state["last_msg"] = str(e)[:120]
        return {"ok": False, "msg": str(e)[:120]}
    finally:
        _state.update({"running": False, "phase": ""})
        _lock.release()


def run(ids=None, wait=False):
    """触发一轮检查。默认后台线程；wait=True 同步跑（调试/接口自测用）。"""
    if wait:
        return _run_all(ids)
    if _state.get("running") or _lock.locked():
        return {"ok": False, "msg": "正在检查中，稍等"}
    threading.Thread(target=_run_all, args=(ids,), daemon=True, name="subs-run").start()
    return {"ok": True, "started": True, "msg": "已开始检查"}


# ---------------- 候选搜索（添加订阅时用） ----------------
def find(kw, limit=20):
    kw = str(kw or "").strip()
    if not kw:
        return {"ok": False, "msg": "先填片名"}
    sc = site.SiteClient()
    try:
        items = sc.search(kw)
    except site.SiteError as e:
        return {"ok": False, "msg": "站点登录失效" if str(e) == "NEED_LOGIN" else str(e)}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"[:120]}
    out = []
    for it in (items or [])[:int(limit)]:
        sub = str(it.get("sub") or "")
        meta = re.sub(r"^\s*(?:19|20)\d{2}\s*(?:·\s*)?", "", sub)
        out.append({"id": it.get("id"), "cat": it.get("cat"), "title": it.get("title"),
                    "poster": it.get("poster") or "", "sub": sub, "meta": meta,
                    "year": year_of(it), "kw": clean_kw(it.get("title")),
                    "score": match_score(it.get("title"), kw)})
    out.sort(key=lambda x: (-int(x.get("score") or 0), -int(x.get("year") or 0)))
    return {"ok": True, "items": out}


# ---------------- 手动指定转存路线（卡片/详情上的三个按钮）----------------
def manual_convert(item_id, mode, magnet=None, want_eps=None, on_progress=None):
    """按用户手动选择的路线转存一个条目，不掺其它兜底。

    mode:
      magnet   —— 只用磁力离线到 115（纯磁力，死种就失败）
      pan115   —— 只用站点的 115 网盘分享转存（服务端复制，秒级）
    want_eps: 追更时指定集号列表；None=整部/整季。
    返回 {ok, msg, mode, ...}
    """
    it = store.get_item(item_id)
    if not it:
        return {"ok": False, "msg": "条目不存在"}
    mode = str(mode or "").strip()
    if mode not in ("magnet", "pan115"):
        return {"ok": False, "msg": "未知路线：" + mode}

    # 磁力路：复用 push_single 的挑种/秒传逻辑，强制纯磁力（关掉网盘兜底）
    if mode == "magnet":
        try:
            r = jobs.push_single(item_id, magnet, fallback=False)
        except Exception as e:
            return {"ok": False, "mode": mode, "msg": f"磁力推送异常：{str(e)[:120]}"}
        return dict(r, mode=mode)

    # 两条网盘路都需要详情里的 pans
    sc = site.SiteClient()
    try:
        info = sc.detail(it["url"], it.get("cat") or "")
    except site.SiteError as e:
        return {"ok": False, "mode": mode,
                "msg": "站点登录失效" if str(e) == "NEED_LOGIN" else str(e)}
    except Exception as e:
        return {"ok": False, "mode": mode, "msg": f"详情抓取失败：{str(e)[:120]}"}

    P = pan.Pan115()
    if mode == "pan115":
        if not P.logged_in():
            return {"ok": False, "mode": mode, "msg": "115 未登录，先到「115 账号」扫码"}
        # 手动转存不走订阅过滤规则：造一个宽松的 s，且 force=True 忽略自动开关
        s = {"min_gb": 0, "max_gb": 0, "filter_inc": "", "filter_exc": "", "qmin": ""}
        action = _pan_push(s, it, info, P, force=True)
        if not action:
            return {"ok": False, "mode": mode,
                    "msg": info.get("_pan_msg") or "没有可用的 115 网盘分享"}
        store.log(f"☁️ 手动转存(115网盘): {it.get('title')} —— {action.get('msg')}", "ok")
        return {"ok": True, "mode": mode, "msg": action.get("msg") or "已转存到 115",
                "action": action.get("action"), "patch": action.get("patch")}

# ---------------- 后台循环 ----------------
def _sleep(sec):
    for _ in range(int(sec)):
        if _stop.is_set():
            return
        time.sleep(1)


def _loop():
    # 启动后先等两分钟：别跟首轮同步/整理抢站点和 115
    _sleep(120)
    while not _stop.is_set():
        scfg = cfg()
        if scfg.get("enabled", True) and not jobs.state().get("running"):
            try:
                _run_all()
            except Exception:
                store.log("订阅循环异常: " + traceback.format_exc()[-500:], "error")
        _sleep(max(60, int(scfg.get("interval") or 60) * 60))


def start():
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, daemon=True, name="subs-loop")
    _thread.start()


def stop():
    _stop.set()
