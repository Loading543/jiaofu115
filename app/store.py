"""SQLite 存储层：配置、条目、磁力、任务、日志。"""
import json
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager

DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"))
DB_PATH = os.path.join(DATA_DIR, "app.db")

_lock = threading.RLock()

DEFAULT_SETTINGS = {
    "site": {
        "base": "https://www.xn--wcv59z.com",
        "username": "",
        "password": "",
    },
    "pan": {
        # 默认保存目录（所有栏目共用）；paths 里可以给某个栏目单独指定，
        # 结构：{"mv": {"id": "...", "name": "..."}, "tv": {...}, "ac": {...}}
        # 为什么要分栏目：电影和剧集混在一个目录里，后面找片、洗版都容易乱。
        "save_path_id": "",
        "save_path_name": "",
        "paths": {},
    },
    "auto": {
        "enabled": True,
        "paused": False,
        "interval": 15,
        "pick": "largest",
        "min_gb": 0,
        "max_gb": 0,
        # 评分门槛：只自动推「评分 ≥ min_score」的影视。豆瓣分优先，没有豆瓣分
        # 就用 IMDB 分（站点大部分外语片只有 IMDB，只看豆瓣会漏掉八成）。
        # 0 = 不限。订阅是用户点名的，不受这个门槛影响；手动推也不受。
        "min_score": 7.0,
        # 站点刚上线的片子常常还没有任何评分。默认「没分不推」，等出分后
        # 下一次同步会补上评分，自然进入待推队列。
        "score_unknown": False,
        "max_push_per_run": 8,
        # 每日推送上限（按自然日算，0 = 不限）：防止一次性补推送几百条把 115 打爆
        "max_push_per_day": 30,
        # 115 剩余空间保护线（GB）：低于它就停止自动推送，避免把网盘撑爆后
        # 下载/整理连环失败。0 = 不检查。
        "min_free_gb": 50,
        # 优先挑「115 能秒传」的磁力：已秒传过的 hash / 115 任务列表里已完成的 hash。
        # 注意：它只在「同一画质档位」里生效，不会为了秒传把 4K 换成 1080p。
        "prefer_sec": True,
        # 优先 4K：磁力里只要有 2160p/4K 档，就不推 1080p。
        # 关掉后完全按上面的「磁力选择」规则（体积/顺序）走。
        "prefer_4k": True,
        "delay": 1.5,
        "backfill": False,
        # 整站抓取：每栏目最多翻多少页（站点固定 50 页 × 48 条）
        "full_crawl": False,
        "max_pages": 50,
        # 日常增量同步只翻前几页，足够发现新上架
        "pages": 3,
    },
    # 订阅追片：盯着某个片名，有磁力就自动离线，出现更高画质自动洗版
    "sub": {
        "enabled": True,
        "interval": 60,       # 每轮检查间隔（分钟）
        "delay": 5,           # 每个订阅之间的间隔（秒），站点搜索接口比列表更敏感
        "max_per_run": 20,    # 单轮最多检查多少个（按上次检查时间轮转）
        "max_ep_per_run": 3,  # 追更订阅单轮最多补几集（站点一次更新很多集时逐轮补齐）
        # 磁力全是死种 / 没速度时，改用站点的 115 网盘分享转存。
        # 115 转存是服务端复制：不下载不上传，几十 G 也是秒级完成。
        "pan_fallback": True,
        # 追更时，分享里找不到单集文件、只有整包，是否允许整包转存。
        # 关掉（默认）可以避免每周重复转一整季；打开则「宁可重下也要有」。
        "pan_allow_pack": True,
        "pan_max_try": 4,     # 每条订阅每轮最多验几个 115 分享（分享死得快）
    },
    "categories": [
        {"slug": "mv", "name": "电影", "on": True},
        {"slug": "tv", "name": "电视剧", "on": True},
        {"slug": "ac", "name": "动漫", "on": True},
    ],
}


@contextmanager
def conn():
    """每次调用开一条连接，用完必须关。

    以前是 `with conn() as c` 直接返回 sqlite3 连接：with 结束只 commit，
    连接靠 GC 才回收。同步/整理/洗版三条线程跑一整天会攒下大量文件句柄，
    极端情况下把 WAL 文件锁住（database is locked）。
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    c.row_factory = sqlite3.Row
    try:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        yield c
        c.commit()
    except Exception:
        try:
            c.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            c.close()
        except Exception:
            pass


def init():
    with _lock, conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
            CREATE TABLE IF NOT EXISTS items (
                id TEXT PRIMARY KEY, cat TEXT, title TEXT, url TEXT,
                poster TEXT, sub TEXT, ts INTEGER, first_seen INTEGER,
                pushed INTEGER DEFAULT 0, best_size TEXT, best_btih TEXT,
                stat TEXT DEFAULT '', miss INTEGER DEFAULT 0,
                pos INTEGER DEFAULT 0, pround INTEGER DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_items_cat ON items(cat, ts DESC);
            CREATE TABLE IF NOT EXISTS magnets (
                item_id TEXT, btih TEXT, url TEXT, size_text TEXT,
                size_bytes INTEGER, label TEXT,
                PRIMARY KEY (item_id, btih)
            );
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT, title TEXT,
                magnet TEXT, size_text TEXT, size_bytes INTEGER,
                status TEXT, msg TEXT, created INTEGER, updated INTEGER
            );
            CREATE TABLE IF NOT EXISTS pushed (btih TEXT PRIMARY KEY, item_id TEXT, title TEXT, ts INTEGER);
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, level TEXT, msg TEXT
            );
            CREATE TABLE IF NOT EXISTS washes (
                id INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT, title TEXT,
                old_btih TEXT, old_size TEXT, new_btih TEXT, new_size TEXT,
                old_q TEXT, new_q TEXT, status TEXT, msg TEXT, ts INTEGER
            );
            CREATE TABLE IF NOT EXISTS subs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kw TEXT NOT NULL,            -- 订阅的关键词（片名）
                cat TEXT DEFAULT '',         -- 限定栏目 mv/tv/ac，空 = 不限
                year TEXT DEFAULT '',        -- 限定年份，空 = 不限
                qmin TEXT DEFAULT '',        -- 最低画质：'' / 1080p / 2160p
                min_gb REAL DEFAULT 0,
                max_gb REAL DEFAULT 0,
                auto_wash INTEGER DEFAULT 1, -- 出现更高画质是否自动洗版
                wait_end INTEGER DEFAULT 1,  -- 剧集是否等完结再推
                -- 剧集更新方式：'' 默认（看 wait_end）/ follow 追更（一集一集下）/ all 有就推
                ep_mode TEXT DEFAULT '',
                ep_from INTEGER DEFAULT 0,   -- 追更起始集：0=只追新，N=从第 N 集补
                last_ep INTEGER DEFAULT 0,   -- 追更已推到第几集
                ep_done TEXT DEFAULT '',     -- 已推的「集号=btih」清单，避免同一集重复下
                enabled INTEGER DEFAULT 1,
                status TEXT DEFAULT 'waiting',  -- waiting / pushed / error
                item_id TEXT DEFAULT '', title TEXT DEFAULT '',
                btih TEXT DEFAULT '', size_text TEXT DEFAULT '',
                size_bytes INTEGER DEFAULT 0, qscore INTEGER DEFAULT 0,
                hits INTEGER DEFAULT 0,
                last_check INTEGER DEFAULT 0, last_msg TEXT DEFAULT '',
                created INTEGER, updated INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_subs_en ON subs(enabled, last_check);
            CREATE TABLE IF NOT EXISTS sub_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, sub_id INTEGER,
                kw TEXT, event TEXT, msg TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_sublog_ts ON sub_log(ts DESC);
            """
        )
        # 轻量迁移：isnew 标记“基线之后新上架”，只有它能被自动推送
        cols = {r["name"] for r in c.execute("PRAGMA table_info(items)").fetchall()}
        if "isnew" not in cols:
            c.execute("ALTER TABLE items ADD COLUMN isnew INTEGER DEFAULT 0")
        # 订阅追更（老库补列）：剧集按集推进的三个字段
        scols = {r["name"] for r in c.execute("PRAGMA table_info(subs)").fetchall()}
        for name, ddl in (("ep_mode", "TEXT DEFAULT ''"),
                          ("ep_from", "INTEGER DEFAULT 0"),
                          ("last_ep", "INTEGER DEFAULT 0"),
                          ("ep_done", "TEXT DEFAULT ''")):
            if name not in scols:
                c.execute(f"ALTER TABLE subs ADD COLUMN {name} {ddl}")
        # 订阅模型：媒体身份 / 过滤 / 优先级 / 缺集 / 状态机
        first_migrate = "state" not in scols
        for name, ddl in (
                ("media_source", "TEXT DEFAULT 'site'"),   # 媒体源：site / themoviedb
                ("media_id", "TEXT DEFAULT ''"),           # 媒体 ID（绑定条目时写 item_id）
                ("season", "INTEGER DEFAULT 0"),           # 季号，0 = 不分季
                ("filter_inc", "TEXT DEFAULT ''"),         # 包含关键词，| 分隔
                ("filter_exc", "TEXT DEFAULT ''"),         # 排除关键词，| 分隔
                ("effect", "TEXT DEFAULT ''"),             # 特效偏好：HDR / DV / 60fps
                ("best_version", "INTEGER DEFAULT 0"),     # 洗版（= MP 的 best_version）
                ("best_version_full", "INTEGER DEFAULT 0"),  # 只接受整包洗版
                ("current_priority", "INTEGER DEFAULT 0"),  # 洗版基线优先级
                ("episode_priority", "TEXT DEFAULT '{}'"),  # {集号: 优先级}
                ("note", "TEXT DEFAULT '[]'"),              # 已下载集号列表
                ("total_episode", "INTEGER DEFAULT 0"),     # 总集数
                ("start_episode", "INTEGER DEFAULT 1"),     # 订阅起始集
                ("lack_episode", "INTEGER DEFAULT 0"),      # 缺失集数
                ("state", "TEXT DEFAULT 'N'"),              # N/R/P/S
                ("search_interval", "INTEGER DEFAULT 0"),   # 单独搜索间隔（小时），0=跟随全局
                ("last_search", "INTEGER DEFAULT 0"),
                ("last_update", "INTEGER DEFAULT 0"),
                ("save_path", "TEXT DEFAULT ''")):
            if name not in scols:
                c.execute(f"ALTER TABLE subs ADD COLUMN {name} {ddl}")
        # 老订阅迁移到新状态机：auto_wash → best_version（只在首次建列时回填，
        # 之后每次启动不能再跑，否则会覆盖用户后来改的设置）
        if first_migrate:
            c.execute("UPDATE subs SET best_version=COALESCE(auto_wash,0)")
            c.execute("UPDATE subs SET state=CASE "
                      "WHEN enabled=0 THEN 'S' "
                      "WHEN status='pushed' THEN 'R' "
                      "WHEN status='error' THEN 'P' "
                      "ELSE 'N' END")
        # 洗版/排序需要：已推资源体积（字节）、质量分、豆瓣分
        for name, ddl in (("best_bytes", "INTEGER DEFAULT 0"),
                          ("qscore", "INTEGER DEFAULT 0"),
                          ("douban", "REAL DEFAULT 0"),
                          # 站点的更新状态（全24集 / 更新至12集），用来判断「刚完结」
                          ("stat", "TEXT DEFAULT ''"),
                          # 「暂时还没磁力」的落空次数，够次数才判死刑
                          ("miss", "INTEGER DEFAULT 0"),
                          # 站点列表位次（0 = 该栏目最新一条）与观察它的轮次。
                          # 站点列表页没有时间字段，「按网站顺序」只能靠这两个字。
                          ("pos", "INTEGER DEFAULT 0"),
                          ("pround", "INTEGER DEFAULT 0")):
            if name not in cols:
                c.execute(f"ALTER TABLE items ADD COLUMN {name} {ddl}")
        # 评分门槛用：score = 豆瓣优先、IMDB 兜底（douban 列仍只存豆瓣，给排序用）
        if "score" not in cols:
            c.execute("ALTER TABLE items ADD COLUMN score REAL DEFAULT 0")
        row = c.execute("SELECT v FROM kv WHERE k='score_backfilled'").fetchone()
        if not row:
            # 老数据按 sub 文本回填一次，否则升级后所有老条目都是「无评分」，
            # 门槛一开全被挡下，要等下一次同步抓到才有分。
            for r in c.execute("SELECT id, sub FROM items").fetchall():
                v = _score(r["sub"])
                if v > 0:
                    c.execute("UPDATE items SET score=? WHERE id=?", (v, r["id"]))
            c.execute("INSERT OR REPLACE INTO kv(k,v) VALUES('score_backfilled','1')")
        c.execute("CREATE INDEX IF NOT EXISTS idx_items_bytes ON items(best_bytes DESC)")
        # 老数据回填：从 '1.67G' 这种文本还原字节数，让「按体积排序」立刻可用
        row = c.execute("SELECT v FROM kv WHERE k='bytes_backfilled'").fetchone()
        if not row:
            rows = c.execute("SELECT id, best_size FROM items WHERE best_bytes=0 AND best_size IS NOT NULL AND best_size<>''").fetchall()
            for r in rows:
                b = parse_size_text(r["best_size"])
                if b:
                    c.execute("UPDATE items SET best_bytes=? WHERE id=?", (b, r["id"]))
            c.execute("INSERT OR REPLACE INTO kv(k,v) VALUES('bytes_backfilled','1')")


# ---------- kv / settings ----------
def get_kv(k, default=None):
    with _lock, conn() as c:
        row = c.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row["v"])
    except Exception:
        return row["v"]


def set_kv(k, v):
    with _lock, conn() as c:
        c.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                  (k, json.dumps(v, ensure_ascii=False)))


def settings():
    s = get_kv("settings")
    if not s:
        return json.loads(json.dumps(DEFAULT_SETTINGS))
    out = json.loads(json.dumps(DEFAULT_SETTINGS))
    for k, v in (s or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k].update(v)
        else:
            out[k] = v
    return out


def save_settings(patch: dict):
    s = settings()
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(s.get(k), dict):
            s[k].update(v)
        else:
            s[k] = v
    set_kv("settings", s)
    return s


# ---------- cookies ----------
def site_cookies():
    return get_kv("site_cookies", {}) or {}


def set_site_cookies(d):
    set_kv("site_cookies", d or {})


def pan_cookies():
    return get_kv("pan_cookies", {}) or {}


def set_pan_cookies(d):
    set_kv("pan_cookies", d or {})


# ---------- items ----------
def _douban(sub):
    """从 '2001 · 香港 · 喜剧/爱情 · 豆瓣 6.2 · 1 磁力' 里抠出豆瓣分。"""
    m = re.search(r"豆瓣\s*([0-9]+(?:\.[0-9]+)?)", sub or "")
    return float(m.group(1)) if m else 0.0


def _score(sub):
    """评分：豆瓣优先，没有豆瓣分就用 IMDB 分。

    站点的电影条目绝大多数只有 IMDB 分（实测 100 条里 82 条只有 IMDB），
    只看豆瓣会让评分门槛对电影几乎失效。
    """
    s = str(sub or "")
    m = re.search(r"豆瓣\s*([0-9]+(?:\.[0-9]+)?)", s)
    if m:
        return float(m.group(1))
    m = re.search(r"IMDB\s*([0-9]+(?:\.[0-9]+)?)", s, re.I)
    if m:
        return float(m.group(1))
    return 0.0


_UNITS = {"B": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}


def parse_size_text(text):
    """'1.67G' / '5.32 GB' / '820M' → 字节数。"""
    m = re.match(r"\s*([0-9]+(?:\.[0-9]+)?)\s*([KMGTBK]?)", str(text or "").upper())
    if not m:
        return 0
    try:
        n = float(m.group(1))
    except ValueError:
        return 0
    u = (m.group(2) or "B")[0]
    return int(n * _UNITS.get(u, 1))


def _ended(stat):
    """站点用 g 字段标更新状态：'全24集' / '更新至12集' / '预告' / '?集'。

    只有「全N集」才算完结 —— 完结那一刻补推一次，连载中不动。
    """
    s = str(stat or "").strip()
    return s.startswith("全") and any(ch.isdigit() for ch in s)


def upsert_item(it: dict, mark_new=False, detect_finish=False):
    """入库/更新一条条目。

    返回 "new" 表示新入库，"fin" 表示这条刚从连载变成完结（值得补推一次），
    其余返回 ""。

    it 里若带 pos/pround（来自列表页抓取），会记下「站点第几条」和「哪一轮看到的」，
    「最新更新」就是按这两个字段排的 —— 直接等价于网站列表顺序。
    """
    db = _douban(it.get("sub"))
    sc = _score(it.get("sub"))
    stat = str(it.get("stat") or "")[:20]
    ts_in = int(it.get("ts") or 0)
    pos = it.get("pos")
    pos = int(pos) if pos is not None else None
    pround = int(it.get("pround") or 0)
    with _lock, conn() as c:
        row = c.execute("SELECT id, stat FROM items WHERE id=?", (it["id"],)).fetchone()
        if row:
            # 老状态必须非空才算「以前见过连载中」：首次抓到 stat 时全是空串，
            # 那时候把所有完结剧都判成刚完结，一上线就会推爆。
            old_stat = str(row["stat"] or "")
            bump = bool(detect_finish and old_stat and not _ended(old_stat) and _ended(stat))
            c.execute(
                "UPDATE items SET title=?, url=?, sub=?, stat=?, "
                "poster=CASE WHEN ?<>'' THEN ? ELSE poster END, "
                "douban=CASE WHEN ?>0 THEN ? ELSE douban END, "
                "score=CASE WHEN ?>0 THEN ? ELSE score END, "
                # 以前这里写成 `?=1 OR ts=0 THEN ?`，于是任何不带 ts 的 upsert
                # （搜索、洗版扫描）都会把已有条目的 ts 覆写成 0，最新排序直接乱掉。
                # 现在只允许「完结补推」和「修 ts=0 的坏行」两种写入。
                "ts=CASE WHEN ?=1 THEN ? WHEN ts=0 AND ?>0 THEN ? ELSE ts END, "
                "isnew=CASE WHEN ?=1 THEN 1 ELSE isnew END, "
                "pushed=CASE WHEN ?=1 THEN 0 ELSE pushed END, "
                # pos 只在这一轮比上次更新时覆盖；乱序补抓不会把位次改坏
                "pos=CASE WHEN ?>0 AND ?>pround THEN ? ELSE pos END, "
                "pround=CASE WHEN ?>0 THEN MAX(COALESCE(pround,0), ?) ELSE pround END "
                "WHERE id=?",
                (it.get("title"), it.get("url"), it.get("sub"), stat,
                 it.get("poster") or "", it.get("poster") or "",
                 db, db,
                 sc, sc,
                 1 if bump else 0, ts_in, ts_in, ts_in,
                 1 if bump else 0,
                 1 if bump else 0,
                 pround, pround, pos,
                 pround, pround,
                 it["id"]),
            )
            return "fin" if bump else ""
        c.execute(
            "INSERT INTO items(id,cat,title,url,poster,sub,ts,first_seen,pushed,isnew,douban,score,stat,pos,pround) "
            "VALUES(?,?,?,?,?,?,?,?,0,?,?,?,?,?,?)",
            (it["id"], it.get("cat"), it.get("title"), it.get("url"), it.get("poster"),
             it.get("sub"), ts_in, int(time.time()),
             1 if mark_new else 0, db, sc, stat,
             pos if pos is not None else 0, pround),
        )
        return "new"


def mark_baseline():
    """把当前库里的条目全部标记为历史：不自动推送，但可以手动推。"""
    with _lock, conn() as c:
        n = c.execute("UPDATE items SET pushed=3 WHERE pushed=0").rowcount
        c.execute("UPDATE items SET isnew=0")
    set_kv("baseline_done", 1)
    set_kv("baseline_at", int(time.time()))
    return n or 0


def mark_history():
    """整站补抓进来的老片统一记为历史（isnew=1 的新上架不动）。"""
    with _lock, conn() as c:
        return c.execute("UPDATE items SET pushed=3 WHERE pushed=0 AND isnew=0").rowcount or 0


SORTS = {
    # 「最新」= 网站顺序：本轮抓到的按站点位次（第几页第几条）排，
    # 本轮没抓到的（pround 更旧，说明已经滑到后面去了）自然排在后面。
    # 末尾的 CASE 只是让「全部」下同一层按 电影→电视剧→动漫 交错，观感稳定。
    "new": ("pround DESC, pos ASC, ts DESC, first_seen DESC, "
            "CASE cat WHEN 'mv' THEN 1 WHEN 'tv' THEN 2 WHEN 'ac' THEN 3 ELSE 4 END"),
    # ts=0 是历史脏数据，别让它占据「最早」榜首
    "old": "CASE WHEN ts=0 THEN 1 ELSE 0 END ASC, ts ASC, first_seen ASC",
    "size": "_bytes DESC, ts DESC",
    "douban": "douban DESC, ts DESC",
    "title": "title COLLATE NOCASE ASC",
}

# 体积排序：优先已推版本的字节数，没有就用缓存磁力里最大那条
_BYTES_EXPR = ("COALESCE(NULLIF(best_bytes,0), "
               "(SELECT MAX(size_bytes) FROM magnets WHERE magnets.item_id=items.id), 0)")


def _order_of(sort):
    o = SORTS.get(sort or "new", SORTS["new"])
    # 末尾补一个唯一键：位次相同的条目（尤其「全部」下三个栏目）分页时才不会
    # 重复或漏条
    return o.replace("_bytes", _BYTES_EXPR) + ", id ASC"


def list_items(cat=None, limit=60, keyword=None, offset=0, sort="new"):
    where, args = ["1=1"], []
    if cat and cat != "all":
        where.append("cat=?")
        args.append(cat)
    if keyword:
        where.append("title LIKE ?")
        args.append("%" + keyword + "%")
    w = " AND ".join(where)
    order = _order_of(sort)
    with _lock, conn() as c:
        total = c.execute(f"SELECT COUNT(*) n FROM items WHERE {w}", args).fetchone()["n"]
        rows = c.execute(f"SELECT * FROM items WHERE {w} ORDER BY {order} LIMIT ? OFFSET ?",
                         args + [int(limit), int(offset)]).fetchall()
    return [dict(r) for r in rows], total


def get_item(item_id):
    with _lock, conn() as c:
        return dict(c.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone() or {})


def mark_pushed(item_id, size_text, btih, size_bytes=0, qscore=0):
    with _lock, conn() as c:
        c.execute("UPDATE items SET pushed=1, best_size=?, best_btih=?, best_bytes=?, qscore=?, miss=0 "
                  "WHERE id=?",
                  (size_text, btih, int(size_bytes or 0), int(qscore or 0), item_id))


def mark_pushed_only(item_id):
    """剧集追更用：只把条目标成「已推」，不写体积/质量分。

    一集 500MB 的种如果写进 best_size，洗版扫描会以为这部剧只有 500MB，
    下次直接把整季判成「有更好的版本」去洗 —— 那是灾难。
    所以追更只关掉「自动推送」，条目自身的体积信息留给洗版扫描按需重算。
    """
    with _lock, conn() as c:
        c.execute("UPDATE items SET pushed=1, miss=0 WHERE id=?", (item_id,))


def mark_failed(item_id, size_text=""):
    """pushed=2 表示推送失败，不再自动重试，避免每轮都刷同一条"""
    with _lock, conn() as c:
        c.execute("UPDATE items SET pushed=2, best_size=? WHERE id=?", (size_text, item_id))


def mark_retry(item_id):
    """放回自动推送队列（pushed=0），并清掉上次挑中的 magnet 信息。

    死种被拉黑以后要重新挑一个候选，不能因为 items 里还留着 best_btih
    就以为「已经推过了」。劣质磁力已经进黑名单，重挑时会被过滤掉，
    所以不会原地打转；候选全被过滤光时 _pick_best 返回 None，条目
    会走 mark_failed 收尾。
    """
    with _lock, conn() as c:
        c.execute("UPDATE items SET pushed=0, best_btih='', best_size='', "
                  "best_bytes=0, miss=0 WHERE id=?", (item_id,))


def mark_history_item(item_id):
    """单条归为历史（pushed=3）：不自动推，但保留手动推的能力。

    用在「预告片」上：站点先挂个预告、过几天才上正片。按历史处理之后，
    等它变成「全 N 集」时会走完结补推，正片不会漏。
    """
    with _lock, conn() as c:
        c.execute("UPDATE items SET pushed=3 WHERE id=? AND pushed=0", (item_id,))


def bump_miss(item_id):
    """「暂时没有磁力」不算失败：站点常常先上架、几小时后才补种。

    记一次落空，返回累计次数，让调用方决定继续等还是放弃。
    """
    with _lock, conn() as c:
        c.execute("UPDATE items SET miss=COALESCE(miss,0)+1 WHERE id=?", (item_id,))
        row = c.execute("SELECT COALESCE(miss,0) m FROM items WHERE id=?", (item_id,)).fetchone()
    return int(row["m"] if row else 0)


def pushed_items(limit=100, offset=0, cat=None):
    """已推送成功的条目 —— 洗版扫描的对象。"""
    where, args = ["pushed=1"], []
    if cat and cat != "all":
        where.append("cat=?")
        args.append(cat)
    w = " AND ".join(where)
    with _lock, conn() as c:
        total = c.execute(f"SELECT COUNT(*) n FROM items WHERE {w}", args).fetchone()["n"]
        rows = c.execute(f"SELECT * FROM items WHERE {w} ORDER BY ts DESC LIMIT ? OFFSET ?",
                         args + [int(limit), int(offset)]).fetchall()
    return [dict(r) for r in rows], total


def pushed_like(keyword, cat="", limit=20):
    """已推送且标题包含关键词的条目 —— 订阅时先看看是不是早就下过了。"""
    where, args = ["pushed=1", "title LIKE ?"], ["%" + str(keyword or "") + "%"]
    if cat and cat != "all":
        where.append("cat=?")
        args.append(cat)
    with _lock, conn() as c:
        rows = c.execute(
            f"SELECT * FROM items WHERE {' AND '.join(where)} ORDER BY ts DESC LIMIT ?",
            args + [int(limit)]).fetchall()
    return [dict(r) for r in rows]


def new_items(cat, limit=8):
    """待自动推送的条目：新上架的，加上「刚完结」被补推的。

    isnew=1 有两个来源，都由同步流程控制：
      1. 出现在列表第 1 页（最新 48 条）且以前没见过的条目 → 新上架
      2. 老条目从「更新至 N 集」变成「全 N 集」→ 完结补推（只触发一次）

    基线之前就存在的片子不会凭空变成 isnew=1，所以这里不再按 first_seen 过滤
    —— 否则完结补推的老条目会被时间线挡住，永远推不出去。

    评分门槛在 SQL 里过滤（而不是取出来再筛），否则低分条目会占满 LIMIT，
    每轮都白跑还不推。门槛关掉（0）或放宽后，被挡的条目会自动回到队列。
    """
    auto = settings().get("auto") or {}
    min_score = float(auto.get("min_score") or 0)
    unknown_ok = bool(auto.get("score_unknown"))
    where = "pushed=0 AND isnew=1 AND cat=?"
    args = [cat]
    if min_score > 0:
        where += " AND (score>=? OR score<=0)" if unknown_ok else " AND score>=?"
        args.append(min_score)
    with _lock, conn() as c:
        rows = c.execute(
            f"SELECT * FROM items WHERE {where} "
            "ORDER BY first_seen ASC, ts DESC LIMIT ?",
            args + [int(limit)],
        ).fetchall()
    return [dict(r) for r in rows]


def unpushed_items(cat, limit=8, since=0):
    """兼容旧调用：等价于 new_items（since 已由 isnew 标记取代）"""
    return new_items(cat, limit)


def set_magnets(item_id, magnets):
    with _lock, conn() as c:
        c.execute("DELETE FROM magnets WHERE item_id=?", (item_id,))
        for m in magnets:
            c.execute("INSERT OR REPLACE INTO magnets(item_id,btih,url,size_text,size_bytes,label) VALUES(?,?,?,?,?,?)",
                      (item_id, m.get("btih"), m.get("url"), m.get("size_text"),
                       int(m.get("size_bytes") or 0), m.get("label")))
        rows = c.execute("SELECT * FROM magnets WHERE item_id=? ORDER BY size_bytes DESC", (item_id,)).fetchall()
    return [dict(r) for r in rows]


def get_magnets(item_id):
    with _lock, conn() as c:
        rows = c.execute("SELECT * FROM magnets WHERE item_id=? ORDER BY size_bytes DESC", (item_id,)).fetchall()
    return [dict(r) for r in rows]


def already_pushed(btih):
    with _lock, conn() as c:
        return bool(c.execute("SELECT 1 FROM pushed WHERE btih=?", (btih,)).fetchone())


def mark_pushed_global(btih, item_id, title):
    with _lock, conn() as c:
        c.execute("INSERT OR REPLACE INTO pushed(btih,item_id,title,ts) VALUES(?,?,?,?)",
                  (btih, item_id, title, int(time.time())))


# ---------- 秒传记忆 / 每日推送额度 ----------
_SEC_MAX = 400


# ---------- 死种 / 假种黑名单 ----------
_BAD_MAX = 500


def bad_hashes():
    """被判定为死种/广告包的 infohash。挑磁力时直接跳过。"""
    d = get_kv("bad_hashes") or {}
    return set(d.keys()) if isinstance(d, dict) else set()


def bad_detail():
    d = get_kv("bad_hashes") or {}
    return d if isinstance(d, dict) else {}


def mark_bad(btih, why=""):
    """拉黑一个 infohash。

    站点上大量「新片」其实是广告包：种子只有几百 KB，里面两个占位视频
    加一个广告 .DOC，名字却套着完整发布名。115 会一直「等待中」或者
    瞬间完成，落地目录里只剩几个占位文件。
    同一个 hash 不能反复试，否则每轮都推它、每轮都失败。
    """
    h = str(btih or "").lower()
    if not h:
        return
    d = get_kv("bad_hashes") or {}
    if not isinstance(d, dict):
        d = {}
    d[h] = {"ts": int(time.time()), "why": str(why or "")[:100]}
    if len(d) > _BAD_MAX:
        old = sorted(d.items(), key=lambda x: (x[1] or {}).get("ts", 0))
        for k, _ in old[: len(d) - _BAD_MAX]:
            d.pop(k, None)
    set_kv("bad_hashes", d)


def unmark_bad(btih):
    h = str(btih or "").lower()
    d = get_kv("bad_hashes") or {}
    if isinstance(d, dict) and h in d:
        d.pop(h, None)
        set_kv("bad_hashes", d)


def sec_hashes():
    """已确认能秒传的 infohash 集合（推送后 5 秒内就 100% 的那批）。"""
    d = get_kv("sec_ok") or {}
    return set(d.keys()) if isinstance(d, dict) else set()


# ---------- 网盘分享黑名单 ----------
# 站点的 115 分享死得比磁力还快（发布者删文件、115 审核），而且同一个链接
# 会挂在十几条资源下面。不记下来就会每轮都去验一遍同一批死链。
_PAN_BAD_MAX = 300


def pan_bad_map():
    d = get_kv("pan_bad") or {}
    return d if isinstance(d, dict) else {}


def pan_bad_urls():
    return set(pan_bad_map().keys())


def is_pan_bad(url):
    return str(url or "").strip() in pan_bad_urls()


def mark_pan_bad(url, why=""):
    u = str(url or "").strip()
    if not u:
        return
    d = pan_bad_map()
    d[u] = {"ts": int(time.time()), "why": str(why or "")[:100]}
    if len(d) > _PAN_BAD_MAX:
        for k, _ in sorted(d.items(), key=lambda x: (x[1] or {}).get("ts", 0))[: len(d) - _PAN_BAD_MAX]:
            d.pop(k, None)
    set_kv("pan_bad", d)


def unmark_pan_bad(url):
    d = pan_bad_map()
    if str(url or "") in d:
        d.pop(str(url), None)
        set_kv("pan_bad", d)


def mark_sec(btih):
    """记下「这个 hash 115 直接秒传」，以后同 hash 的磁力优先挑它。"""
    h = str(btih or "").lower()
    if not h:
        return
    d = get_kv("sec_ok") or {}
    if not isinstance(d, dict):
        d = {}
    d[h] = int(time.time())
    if len(d) > _SEC_MAX:          # 只留最近的一批，别让 kv 无限长
        for k, _ in sorted(d.items(), key=lambda x: x[1])[: len(d) - _SEC_MAX]:
            d.pop(k, None)
    set_kv("sec_ok", d)


def pushed_today():
    """今天（本地时间自然日）已经**新增**提交成功的推送条数，用来卡每日上限。

    洗版任务也是 status='sent'，但它是「替换已有资源」不是「新增下载」，
    不该挤占每日推送额度（否则洗两条少推两条新片）。
    """
    lt = time.localtime()
    start = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))
    with _lock, conn() as c:
        row = c.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE status='sent' AND created>=? "
            "AND COALESCE(msg,'')!='洗版替换'",
            (start,)).fetchone()
    return int(row["n"] or 0)


# ---------- 洗版记录 ----------
def add_wash(item_id, title, old_btih, old_size, new_btih, new_size,
             old_q="", new_q="", status="done", msg=""):
    with _lock, conn() as c:
        cur = c.execute(
            "INSERT INTO washes(item_id,title,old_btih,old_size,new_btih,new_size,"
            "old_q,new_q,status,msg,ts) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (item_id, title, old_btih, old_size, new_btih, new_size,
             old_q, new_q, status, msg, int(time.time())),
        )
        return cur.lastrowid


def list_washes(limit=60):
    with _lock, conn() as c:
        rows = c.execute("SELECT * FROM washes ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
    return [dict(r) for r in rows]


# ---------- tasks ----------
def add_task(item_id, title, magnet, size_text, size_bytes, status="pending", msg=""):
    now = int(time.time())
    with _lock, conn() as c:
        cur = c.execute(
            "INSERT INTO tasks(item_id,title,magnet,size_text,size_bytes,status,msg,created,updated) VALUES(?,?,?,?,?,?,?,?,?)",
            (item_id, title, magnet, size_text, size_bytes, status, msg, now, now),
        )
        return cur.lastrowid


def update_task(tid, status, msg=""):
    with _lock, conn() as c:
        c.execute("UPDATE tasks SET status=?, msg=?, updated=? WHERE id=?", (status, msg, int(time.time()), tid))


def list_tasks(limit=80):
    with _lock, conn() as c:
        rows = c.execute("SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
    return [dict(r) for r in rows]


# ---------- logs ----------
def log(msg, level="info"):
    with _lock, conn() as c:
        c.execute("INSERT INTO logs(ts,level,msg) VALUES(?,?,?)", (int(time.time()), level, str(msg)[:2000]))
        c.execute("DELETE FROM logs WHERE id NOT IN (SELECT id FROM logs ORDER BY id DESC LIMIT 800)")


def list_logs(limit=120):
    with _lock, conn() as c:
        rows = c.execute("SELECT * FROM logs ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
    return [dict(r) for r in rows]


def stats():
    """顶部统计。fresh 是「现在真会推的」，被评分门槛挡住的不算在内。"""
    auto = settings().get("auto") or {}
    min_score = float(auto.get("min_score") or 0)
    unknown_ok = bool(auto.get("score_unknown"))
    blocked_sql = ""
    if min_score > 0:
        blocked_sql = ("SELECT COUNT(*) n FROM items WHERE pushed=0 AND isnew=1 "
                       + ("AND score>0 AND score<?" if unknown_ok else "AND score<?"))
    with _lock, conn() as c:
        total = c.execute("SELECT COUNT(*) n FROM items").fetchone()["n"]
        pushed = c.execute("SELECT COUNT(*) n FROM items WHERE pushed=1").fetchone()["n"]
        fresh = c.execute("SELECT COUNT(*) n FROM items WHERE pushed=0 AND isnew=1").fetchone()["n"]
        blocked = c.execute(blocked_sql, (min_score,)).fetchone()["n"] if blocked_sql else 0
        history = c.execute("SELECT COUNT(*) n FROM items WHERE pushed=3").fetchone()["n"]
        tasks = c.execute("SELECT COUNT(*) n FROM tasks").fetchone()["n"]
        today = c.execute("SELECT COUNT(*) n FROM tasks WHERE created > ?",
                          (int(time.time()) - 86400,)).fetchone()["n"]
        washed = c.execute("SELECT COUNT(*) n FROM washes WHERE status='done'").fetchone()["n"]
    return {"items": total, "pushed": pushed, "fresh": max(0, fresh - blocked),
            "blocked": blocked, "history": history,
            "tasks": tasks, "today": today, "washed": washed}


def count_by_cat():
    with _lock, conn() as c:
        rows = c.execute("SELECT cat, COUNT(*) n FROM items GROUP BY cat").fetchall()
    return {r["cat"]: r["n"] for r in rows}


# ---------- 订阅 ----------
# 订阅字段：best_version / best_version_full / current_priority /
# episode_priority / note / total_episode / start_episode / lack_episode / state
_SUB_SETTABLE = ("kw", "cat", "year", "qmin", "min_gb", "max_gb", "auto_wash",
                 "best_version", "best_version_full", "wait_end",
                 "ep_mode", "ep_from", "last_ep", "ep_done",
                 "enabled", "status", "state", "item_id", "title", "btih",
                 "size_text", "size_bytes", "qscore", "hits", "last_check",
                 "last_msg", "media_source", "media_id", "season",
                 "filter_inc", "filter_exc", "effect",
                 "current_priority", "episode_priority", "note",
                 "total_episode", "start_episode", "lack_episode",
                 "search_interval", "last_search", "last_update", "save_path")


def sub_add(d):
    """新增订阅。同一个关键词 + 栏目已存在时直接返回旧 id（created=False）。"""
    kw = str(d.get("kw") or "").strip()[:80]
    if not kw:
        raise ValueError("缺少关键词")
    cat = str(d.get("cat") or "").strip()
    if cat not in ("", "mv", "tv", "ac"):
        cat = ""

    def _num(v):
        try:
            return max(0.0, float(v or 0))
        except (TypeError, ValueError):
            return 0.0

    def _int(v):
        try:
            return max(0, int(float(v or 0)))
        except (TypeError, ValueError):
            return 0

    ep_mode = str(d.get("ep_mode") or "").strip().lower()
    if ep_mode not in ("", "follow", "all"):
        ep_mode = ""
    best_version = 1 if d.get("best_version", d.get("auto_wash", True)) else 0
    ep_from = _int(d.get("ep_from"))
    now = int(time.time())
    with _lock, conn() as c:
        row = c.execute("SELECT id FROM subs WHERE lower(kw)=lower(?) AND cat=?",
                        (kw, cat)).fetchone()
        if row:
            return int(row["id"]), False
        cur = c.execute(
            "INSERT INTO subs(kw,cat,year,qmin,min_gb,max_gb,auto_wash,best_version,"
            "best_version_full,wait_end,ep_mode,ep_from,enabled,status,state,"
            "media_source,media_id,season,filter_inc,filter_exc,effect,"
            "start_episode,total_episode,lack_episode,search_interval,save_path,"
            "note,episode_priority,created,updated) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1,'waiting','N',?,?,?,?,?,?,?,?,?,?,?,"
            "'[]','{}',?,?)",
            (kw, cat, str(d.get("year") or "").strip()[:8],
             str(d.get("qmin") or d.get("resolution") or "").strip(),
             _num(d.get("min_gb")), _num(d.get("max_gb")),
             best_version, best_version,
             1 if d.get("best_version_full") else 0,
             # 追更模式本身就是「不等完结」，wait_end 不再是它的开关
             0 if ep_mode == "follow" else (1 if d.get("wait_end", True) else 0),
             ep_mode, ep_from,
             str(d.get("media_source") or "site")[:20],
             str(d.get("media_id") or d.get("item_id") or "")[:40],
             _int(d.get("season")),
             str(d.get("filter_inc") or "")[:200],
             str(d.get("filter_exc") or "")[:200],
             str(d.get("effect") or "")[:60],
             ep_from or 1, _int(d.get("total_episode")), 0,
             _int(d.get("search_interval")),
             str(d.get("save_path") or "")[:120],
             now, now),
        )
        return int(cur.lastrowid), True


def subs_list(enabled=None, limit=500):
    where, args = "1=1", []
    if enabled is not None:
        where, args = "enabled=?", [1 if enabled else 0]
    with _lock, conn() as c:
        rows = c.execute(
            f"SELECT * FROM subs WHERE {where} ORDER BY id DESC LIMIT ?",
            args + [int(limit)]).fetchall()
    return [dict(r) for r in rows]


def subs_due(limit=20):
    """按「最久没检查」排队，避免订阅多了以后每次只轮到前几个。

    只取状态机里该跑的三态：N（新建未搜过）、R（订阅中）、P（待定，继续轮询）。
    S（暂停）不进去，跟状态机语义一致 —— 待定不是死信，
    片子晚上架几天还是要能等到。
    """
    with _lock, conn() as c:
        rows = c.execute(
            "SELECT * FROM subs WHERE enabled=1 AND COALESCE(state,'N') IN ('N','R','P') "
            "ORDER BY last_check ASC, id ASC LIMIT ?",
            (int(limit),)).fetchall()
    return [dict(r) for r in rows]


def sub_state_set(sid, state):
    """改状态机状态：N / R / P / S。"""
    st = str(state or "").upper()[:1]
    if st not in ("N", "R", "P", "S"):
        return 0
    with _lock, conn() as c:
        cur = c.execute("UPDATE subs SET state=?, enabled=?, updated=? WHERE id=?",
                        (st, 0 if st == "S" else 1, int(time.time()), int(sid)))
        return cur.rowcount


def sub_get(sid):
    with _lock, conn() as c:
        row = c.execute("SELECT * FROM subs WHERE id=?", (int(sid),)).fetchone()
    return dict(row) if row else {}


def sub_update(sid, **kw):
    fields = [(k, v) for k, v in kw.items() if k in _SUB_SETTABLE]
    if not fields:
        return False
    sets = ", ".join(f"{k}=?" for k, _ in fields)
    with _lock, conn() as c:
        cur = c.execute(f"UPDATE subs SET {sets}, updated=? WHERE id=?",
                        [v for _, v in fields] + [int(time.time()), int(sid)])
        return bool(cur.rowcount)


def sub_del(sid):
    with _lock, conn() as c:
        cur = c.execute("DELETE FROM subs WHERE id=?", (int(sid),))
    return bool(cur.rowcount)


def sub_forget_hash(item_id, btih):
    """种被判死后，把订阅里记的「当前版本」清掉。

    不清的话，check_one 会认为「已经有版本」而拒绝网盘兜底，
    片子就永远卡在「有个其实没下下来的种」上。
    追更订阅不动：它的 btih 只是最近一集，清了会打乱追更进度。
    """
    h = str(btih or "").lower()
    if not item_id or not h:
        return 0
    with _lock, conn() as c:
        cur = c.execute(
            "UPDATE subs SET btih='', size_text='', size_bytes=0, qscore=0, current_priority=0 "
            "WHERE item_id=? AND lower(btih)=? AND COALESCE(ep_mode,'') <> 'follow'",
            (str(item_id), h))
        return cur.rowcount


def sub_expedite(item_id):
    """把某个条目对应的订阅排到队首。

    死种判死（够 45 分钟一点没动）之后走这里：下一轮检查立刻重新挑源，
    磁力全没了就会落到 115 网盘兜底，而不是干等一个小时。
    """
    if not item_id:
        return 0
    with _lock, conn() as c:
        cur = c.execute(
            "UPDATE subs SET last_check=0, last_search=0 "
            "WHERE item_id=? AND enabled=1 AND COALESCE(state,'N') IN ('N','R','P')",
            (str(item_id),))
        return cur.rowcount


def sub_touch(sid, status=None, last_msg=None, **kw):
    """检查完更新状态：分别写，避免把没传的字段覆盖成 None。"""
    patch = dict(kw)
    if status is not None:
        patch["status"] = status
    if last_msg is not None:
        patch["last_msg"] = str(last_msg)[:300]
    patch["last_check"] = int(time.time())
    return sub_update(sid, **patch)


def sub_log(sub_id, kw, event, msg):
    with _lock, conn() as c:
        c.execute("INSERT INTO sub_log(ts,sub_id,kw,event,msg) VALUES(?,?,?,?,?)",
                  (int(time.time()), int(sub_id or 0), str(kw or "")[:80],
                   str(event or "")[:16], str(msg or "")[:300]))
        # 只留最近 600 条，订阅日志不需要永久档案
        c.execute("DELETE FROM sub_log WHERE id NOT IN "
                  "(SELECT id FROM sub_log ORDER BY id DESC LIMIT 600)")


def sub_log_clear():
    with _lock, conn() as c:
        cur = c.execute("DELETE FROM sub_log")
        return cur.rowcount or 0


def sub_logs(limit=80, sub_id=None):
    where, args = "1=1", []
    if sub_id:
        where, args = "sub_id=?", [int(sub_id)]
    with _lock, conn() as c:
        rows = c.execute(
            f"SELECT * FROM sub_log WHERE {where} ORDER BY id DESC LIMIT ?",
            args + [int(limit)]).fetchall()
    return [dict(r) for r in rows]


def sub_stats():
    """按 MP V3 的状态机统计：N 新建 / R 订阅中 / P 待定 / S 暂停。"""
    with _lock, conn() as c:
        rows = c.execute("SELECT COALESCE(state,'N') st, COUNT(*) n FROM subs "
                         "GROUP BY COALESCE(state,'N')").fetchall()
        total = c.execute("SELECT COUNT(*) n FROM subs").fetchone()["n"]
        off = c.execute("SELECT COUNT(*) n FROM subs WHERE enabled=0").fetchone()["n"]
        pushed = c.execute("SELECT COUNT(*) n FROM subs WHERE btih<>''").fetchone()["n"]
    d = {str(r["st"] or "N"): int(r["n"]) for r in rows}
    return {"total": int(total), "paused": int(off), "pushed": int(pushed),
            "new": d.get("N", 0), "running": d.get("R", 0),
            "pending": d.get("P", 0), "stopped": d.get("S", 0),
            # 兼容老前端字段
            "waiting": d.get("N", 0) + d.get("P", 0), "error": d.get("P", 0),
            "done": d.get("R", 0)}
