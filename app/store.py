"""SQLite 存储层：配置、条目、磁力、任务、日志。"""
import json
import os
import re
import sqlite3
import threading
import time

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
        "save_path_id": "",
        "save_path_name": "",
    },
    "auto": {
        "enabled": True,
        "paused": False,
        "interval": 15,
        "pick": "largest",
        "min_gb": 0,
        "max_gb": 0,
        "max_push_per_run": 8,
        "delay": 1.5,
        "backfill": False,
        # 整站抓取：每栏目最多翻多少页（站点固定 50 页 × 48 条）
        "full_crawl": False,
        "max_pages": 50,
        # 日常增量同步只翻前几页，足够发现新上架
        "pages": 3,
    },
    "categories": [
        {"slug": "mv", "name": "电影", "on": True},
        {"slug": "tv", "name": "电视剧", "on": True},
        {"slug": "ac", "name": "动漫", "on": True},
    ],
}


def conn():
    os.makedirs(DATA_DIR, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    return c


def init():
    with _lock, conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
            CREATE TABLE IF NOT EXISTS items (
                id TEXT PRIMARY KEY, cat TEXT, title TEXT, url TEXT,
                poster TEXT, sub TEXT, ts INTEGER, first_seen INTEGER,
                pushed INTEGER DEFAULT 0, best_size TEXT, best_btih TEXT
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
            """
        )
        # 轻量迁移：isnew 标记“基线之后新上架”，只有它能被自动推送
        cols = {r["name"] for r in c.execute("PRAGMA table_info(items)").fetchall()}
        if "isnew" not in cols:
            c.execute("ALTER TABLE items ADD COLUMN isnew INTEGER DEFAULT 0")
        # 洗版/排序需要：已推资源体积（字节）、质量分、豆瓣分
        for name, ddl in (("best_bytes", "INTEGER DEFAULT 0"),
                          ("qscore", "INTEGER DEFAULT 0"),
                          ("douban", "REAL DEFAULT 0")):
            if name not in cols:
                c.execute(f"ALTER TABLE items ADD COLUMN {name} {ddl}")
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


def upsert_item(it: dict, mark_new=False) -> bool:
    """返回 True 表示是新条目。mark_new=True 时把新条目标记为“新上架”。"""
    db = _douban(it.get("sub"))
    with _lock, conn() as c:
        row = c.execute("SELECT id, ts FROM items WHERE id=?", (it["id"],)).fetchone()
        if row:
            c.execute(
                "UPDATE items SET title=?, url=?, sub=?, "
                "poster=CASE WHEN ?<>'' THEN ? ELSE poster END, "
                "douban=CASE WHEN ?>0 THEN ? ELSE douban END, "
                "ts=CASE WHEN ts=0 THEN ? ELSE ts END WHERE id=?",
                (it.get("title"), it.get("url"), it.get("sub"),
                 it.get("poster") or "", it.get("poster") or "",
                 db, db, int(it.get("ts") or 0), it["id"]),
            )
            return False
        c.execute(
            "INSERT INTO items(id,cat,title,url,poster,sub,ts,first_seen,pushed,isnew,douban) "
            "VALUES(?,?,?,?,?,?,?,?,0,?,?)",
            (it["id"], it.get("cat"), it.get("title"), it.get("url"), it.get("poster"),
             it.get("sub"), int(it.get("ts") or 0), int(time.time()),
             1 if mark_new else 0, db),
        )
        return True


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
    "new": "ts DESC, first_seen DESC",
    "old": "ts ASC, first_seen ASC",
    "size": "_bytes DESC, ts DESC",
    "douban": "douban DESC, ts DESC",
    "title": "title COLLATE NOCASE ASC",
}

# 体积排序：优先已推版本的字节数，没有就用缓存磁力里最大那条
_BYTES_EXPR = ("COALESCE(NULLIF(best_bytes,0), "
               "(SELECT MAX(size_bytes) FROM magnets WHERE magnets.item_id=items.id), 0)")


def _order_of(sort):
    o = SORTS.get(sort or "new", SORTS["new"])
    return o.replace("_bytes", _BYTES_EXPR)


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
        c.execute("UPDATE items SET pushed=1, best_size=?, best_btih=?, best_bytes=?, qscore=? WHERE id=?",
                  (size_text, btih, int(size_bytes or 0), int(qscore or 0), item_id))


def mark_failed(item_id, size_text=""):
    """pushed=2 表示推送失败，不再自动重试，避免每轮都刷同一条"""
    with _lock, conn() as c:
        c.execute("UPDATE items SET pushed=2, best_size=? WHERE id=?", (size_text, item_id))


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


def new_items(cat, limit=8):
    """待自动推送的条目：基线之后新上架、且从未推送过。

    双保险：
      1. isnew=1 —— 只有同步时出现在第 1 页（最新 48 条）且首次入库的条目才会被标记
      2. first_seen >= baseline_at —— 基线之前站点上就存在的片子，永远不自动推送
    老条目只能手动点“推送”。
    """
    base = int(get_kv("baseline_at") or 0)
    with _lock, conn() as c:
        rows = c.execute(
            "SELECT * FROM items WHERE pushed=0 AND isnew=1 AND cat=? AND first_seen>=? "
            "ORDER BY first_seen ASC, ts DESC LIMIT ?",
            (cat, base, int(limit)),
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
    with _lock, conn() as c:
        total = c.execute("SELECT COUNT(*) n FROM items").fetchone()["n"]
        pushed = c.execute("SELECT COUNT(*) n FROM items WHERE pushed=1").fetchone()["n"]
        fresh = c.execute("SELECT COUNT(*) n FROM items WHERE pushed=0 AND isnew=1").fetchone()["n"]
        history = c.execute("SELECT COUNT(*) n FROM items WHERE pushed=3").fetchone()["n"]
        tasks = c.execute("SELECT COUNT(*) n FROM tasks").fetchone()["n"]
        today = c.execute("SELECT COUNT(*) n FROM tasks WHERE created > ?",
                          (int(time.time()) - 86400,)).fetchone()["n"]
        washed = c.execute("SELECT COUNT(*) n FROM washes WHERE status='done'").fetchone()["n"]
    return {"items": total, "pushed": pushed, "fresh": fresh, "history": history,
            "tasks": tasks, "today": today, "washed": washed}


def count_by_cat():
    with _lock, conn() as c:
        rows = c.execute("SELECT cat, COUNT(*) n FROM items GROUP BY cat").fetchall()
    return {r["cat"]: r["n"] for r in rows}
