"""Web 服务：HTTP API + 静态页面。"""
import json
import mimetypes
import os
import re
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__, jobs, pan, site, store, subs

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
PORT = int(os.environ.get("PORT", "8123"))

mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")

_qr_cache = {}
_qr_state = {}
_lock = threading.Lock()


def _set_qr(uid, **kw):
    with _lock:
        st = _qr_state.setdefault(uid, {})
        st.update(kw)


def _qr(app):
    c = pan.Pan115()
    tok = c.qr_token(app)
    tok["app"] = app
    with _lock:
        _qr_cache[tok["uid"]] = tok
        _qr_state[tok["uid"]] = {"state": 1, "msg": "等待扫码", "logged": False, "app": app}
    threading.Thread(target=_watch_qr, args=(tok,), daemon=True).start()
    return tok


def _watch_qr(tok):
    """115 状态接口是长轮询，这里单线程盯着，前端只读缓存。"""
    uid = tok["uid"]
    c = pan.Pan115()
    deadline = time.time() + 300
    retry = 0
    while time.time() < deadline:
        try:
            r = c.qr_status(uid, tok["time"], tok["sign"], timeout=35)
        except pan.PanError as e:
            _set_qr(uid, msg=str(e))
            time.sleep(3)
            continue
        if r.get("timeout"):
            continue
        code, msg = r.get("status"), r.get("msg") or ""
        if code == 2:
            res = c.qr_result(uid, tok["app"])
            if res.get("ok"):
                _set_qr(uid, state=2, msg="登录成功", logged=True)
                store.log("115 扫码登录成功", "ok")
                return
            _set_qr(uid, msg=res.get("msg") or "取登录态失败")
            retry += 1
            if retry >= 3:
                return
            time.sleep(2)
            continue
        if code == 1:
            _set_qr(uid, state=1, msg="已扫码，请在手机上确认")
        elif code in (0, -1, -2):
            _set_qr(uid, state=code, msg=msg or "二维码已失效，请刷新")
            if "过期" in msg:
                return
        time.sleep(1)


# ---- 手动转存任务（两个按钮）跟踪：后台跑，前端轮询 ----
_conv_tasks = {}


def _conv_latest():
    """当前最值得看的转存任务（running/queued 优先，否则 10 分钟内完成的）。

    挂在 /api/status 里返回，前端顶部横幅用它显示进度 —— 任何页面都能看到。
    """
    with _lock:
        if not _conv_tasks:
            return None
        running = {k: v for k, v in _conv_tasks.items()
                   if v.get("state") in ("running", "queued")}
        tid = max(running or _conv_tasks)
        t = dict(_conv_tasks[tid])
    t["id"] = tid
    fresh = t.get("state") in ("running", "queued") or (
        t.get("done_at") and time.time() - t["done_at"] < 600)
    return t if fresh else None
_conv_seq = [0]


def _conv_run(tid, item_id, mode, magnet, want_eps):
    with _lock:
        _conv_tasks[tid]["state"] = "running"

    def _prog(done, total, _t0=[time.time()]):
        if not total:
            return
        now = time.time()
        speed = 0.0
        if now > _t0[0]:
            speed = 0.0  # 由 share_pull 侧计算更准，这里只报百分比
        with _lock:
            _conv_tasks[tid]["msg"] = f"下载中 {done * 100 // total}%（{done // 1048576}/{total // 1048576} MB）"

    try:
        r = subs.manual_convert(item_id, mode, magnet=magnet, want_eps=want_eps,
                                on_progress=_prog)
    except Exception as e:
        r = {"ok": False, "msg": f"{type(e).__name__}: {str(e)[:160]}"}
    with _lock:
        t = _conv_tasks[tid]
        t["state"] = "done"
        t["ok"] = bool(r.get("ok"))
        t["msg"] = r.get("msg") or ("完成" if r.get("ok") else "失败")
        t["result"] = {k: v for k, v in r.items() if k in
                       ("ok", "mode", "action", "n", "eps", "size", "size_text", "msg")}
        t["done_at"] = time.time()
    # 写进系统日志：任务内存态 5 分钟就清了，日志里永久可查
    store.log(f"手动转存[{tid}] {mode} {item_id}: {t['msg']}",
              "ok" if t["ok"] else "err")
    # 五分钟后清理，避免无限增长
    def _gc():
        time.sleep(300)
        with _lock:
            _conv_tasks.pop(tid, None)
    threading.Thread(target=_gc, daemon=True).start()


class Handler(BaseHTTPRequestHandler):
    server_version = "JiaofuBot/1.1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    # ---------- 基础 ----------
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _json_body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            return json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except Exception:
            return {}

    def _static(self, path):
        rel = path.lstrip("/")
        if rel in ("", "index.html"):
            rel = "index.html"
        full = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if not full.startswith(STATIC_DIR) or not os.path.isfile(full):
            return self._send(404, {"ok": False, "msg": "not found"})
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        with open(full, "rb") as f:
            data = f.read()
        self._send(200, data, ctype + ("; charset=utf-8" if ctype.startswith("text") or ctype.endswith("javascript") else ""))

    # ---------- 路由 ----------
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        path, q = u.path, urllib.parse.parse_qs(u.query)
        try:
            if path.startswith("/static/"):
                return self._static(path[len("/static/"):])
            if path in ("/", "/index.html"):
                return self._static("index.html")
            return self._api_get(path, q)
        except Exception:
            store.log("接口异常: " + traceback.format_exc()[-600:], "error")
            return self._send(500, {"ok": False, "msg": "服务器内部错误"})

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        try:
            return self._api_post(u.path, self._json_body())
        except Exception:
            store.log("接口异常: " + traceback.format_exc()[-600:], "error")
            return self._send(500, {"ok": False, "msg": "服务器内部错误"})

    # ---------- GET ----------
    def _api_get(self, path, q):
        g = lambda k, d=None: (q.get(k) or [d])[0]

        if path == "/api/status":
            cfg = store.settings()
            sc = site.SiteClient()
            chk = sc.check_login() if store.site_cookies() else {"ok": False, "msg": "未登录"}
            P = pan.Pan115()
            safe = json.loads(json.dumps(cfg))
            safe.setdefault("site", {})["password"] = "***" if cfg["site"].get("password") else ""
            return self._send(200, {
                "ok": True,
                "version": __version__,
                "site": {"base": cfg["site"]["base"], "username": cfg["site"].get("username", ""),
                         "logged": chk["ok"], "msg": chk["msg"]},
                "pan": {"logged": P.logged_in(), "channels": pan.CHANNELS},
                "settings": safe,
                "stats": store.stats(),
                "by_cat": store.count_by_cat(),
                "baseline_at": store.get_kv("baseline_at", 0),
                "sync": jobs.state(),
                "subs": {"state": subs.state(), "stats": store.sub_stats()},
                "conv": _conv_latest(),
            })

        if path == "/api/site/captcha":
            c = site.SiteClient()
            return self._send(200, {"ok": True, "captcha": c.captcha()})

        if path == "/api/site/check":
            c = site.SiteClient()
            return self._send(200, c.check_login())

        if path == "/api/categories/discover":
            sc = site.SiteClient()
            try:
                cats = sc.nav_categories()
            except site.SiteError as e:
                return self._send(200, {"ok": False, "msg": str(e)})
            cfg = store.settings()
            known = {c["slug"] for c in cfg["categories"]}
            for c in cats:
                if c["slug"] not in known:
                    cfg["categories"].append({"slug": c["slug"], "name": c["name"], "on": False})
            store.save_settings({"categories": cfg["categories"]})
            return self._send(200, {"ok": True, "categories": cfg["categories"], "discovered": cats})

        if path == "/api/items":
            items, total = store.list_items(cat=g("cat"), limit=int(g("limit", 60)),
                                            offset=int(g("offset", 0)),
                                            keyword=g("q") or None,
                                            sort=g("sort", "new"))
            return self._send(200, {"ok": True, "items": items, "total": total,
                                    "offset": int(g("offset", 0)),
                                    "sort": g("sort", "new"),
                                    "by_cat": store.count_by_cat()})

        if path == "/api/wash/state":
            return self._send(200, {"ok": True, "wash": jobs.wash_state()})

        if path == "/api/wash/last":
            return self._send(200, {"ok": True, **jobs.wash_last()})

        if path == "/api/wash/scan":
            # 兼容老调用：GET 直接同步扫描（条目少时够用）
            if jobs.wash_state()["running"]:
                return self._send(200, {"ok": False, "msg": "正在扫描中，请稍候"})
            res = jobs.scan_wash(limit=int(g("limit", 40)),
                                 offset=int(g("offset", 0)),
                                 cat=g("cat") or None,
                                 min_gain=int(g("min_gain", 1) or 1),
                                 only_id=g("item_id") or None)
            return self._send(200, res)

        if path == "/api/wash/log":
            return self._send(200, {"ok": True, "washes": store.list_washes(int(g("limit", 60)))})

        if path == "/api/subs":
            return self._send(200, {
                "ok": True,
                "subs": store.subs_list(),
                "state": subs.state(),
                "cfg": store.settings().get("sub") or {},
                "stats": store.sub_stats(),
                "last": store.get_kv("sub_last_run", {}) or {},
            })

        if path == "/api/subs/state":
            return self._send(200, {"ok": True, "state": subs.state()})

        if path == "/api/subs/log":
            return self._send(200, {"ok": True, "logs": store.sub_logs(int(g("limit", 80))),
                                    "stats": store.sub_stats()})

        if path == "/api/item":
            it = store.get_item(g("id"))
            if not it:
                return self._send(404, {"ok": False, "msg": "条目不存在"})
            cached = store.get_magnets(it["id"])
            if g("refresh") != "1" and cached:
                return self._send(200, {"ok": True, "item": it, "magnets": cached, "cached": True})
            sc = site.SiteClient()
            try:
                info = sc.detail(it["url"], it["cat"])
            except site.SiteError as e:
                return self._send(200, {"ok": False, "msg": str(e), "item": it})
            magnets = store.set_magnets(it["id"], info.get("magnets") or [])
            return self._send(200, {"ok": True, "item": it, "info": info, "magnets": magnets})

        if path == "/api/search":
            kw = g("q", "").strip()
            if not kw:
                return self._send(200, {"ok": True, "items": []})
            sc = site.SiteClient()
            try:
                items = sc.search(kw)
            except site.SiteError as e:
                return self._send(200, {"ok": False, "msg": "站点登录失效" if str(e) == "NEED_LOGIN" else str(e)})
            for it in items:
                store.upsert_item(it)
            return self._send(200, {"ok": True, "items": items})

        if path == "/api/sync/state":
            return self._send(200, {"ok": True, "sync": jobs.state()})

        if path == "/api/tasks":
            return self._send(200, {"ok": True, "tasks": store.list_tasks(int(g("limit", 80)))})

        if path == "/api/logs":
            return self._send(200, {"ok": True, "logs": store.list_logs(int(g("limit", 120)))})

        if path == "/api/115/qr":
            app = g("app", "web")
            try:
                tok = _qr(app)
            except pan.PanError as e:
                return self._send(200, {"ok": False, "msg": str(e)})
            return self._send(200, {"ok": True, "uid": tok["uid"], "time": tok["time"],
                                    "sign": tok["sign"], "qrcode": tok["qrcode"], "app": app})

        if path == "/api/115/qr/status":
            uid = g("uid")
            if not uid:
                return self._send(400, {"ok": False, "msg": "缺少 uid"})
            with _lock:
                st = dict(_qr_state.get(uid) or {})
            if not st:
                return self._send(200, {"ok": False, "msg": "二维码不存在，请刷新"})
            return self._send(200, {"ok": True, "state": st.get("state", 1),
                                    "msg": st.get("msg", ""), "logged": st.get("logged", False)})

        if path == "/api/115/quota":
            c = pan.Pan115()
            out = {"ok": True, **c.quota()}
            # 离线配额（约 1.5T，下载临时空间）和网盘空间（100T 级）是两回事，
            # 页面上分开显示，免得「剩 132G」被当成网盘要满了
            try:
                out["space"] = c.space()
            except Exception as e:
                out["space"] = {"ok": False, "err": f"{type(e).__name__}: {e}"[:120]}
            return self._send(200, out)

        if path == "/api/115/tasks":
            c = pan.Pan115()
            pg = int(g("page", "1") or 1)
            return self._send(200, {"ok": True, **c.tasks(page=pg, page_size=int(g("size", "30") or 30), stat=int(g("stat", "0") or 0))})

        if path == "/api/115/dirs":
            c = pan.Pan115()
            if not c.logged_in():
                return self._send(200, {"ok": False, "msg": "115 未登录", "dirs": []})
            cid = g("cid", "0") or "0"
            try:
                r = c.files(cid=int(cid), limit=int(g("limit", "200") or 200))
            except Exception as e:
                return self._send(200, {"ok": False, "msg": str(e), "dirs": []})
            dirs = [{"cid": it.get("cid"), "name": it.get("name")}
                    for it in (r.get("items") or []) if it.get("is_dir") and it.get("cid")]
            return self._send(200, {"ok": True, "cid": cid, "dirs": dirs,
                                    "saved": store.settings()["pan"]})

        if path == "/api/debug/115/info":
            c = pan.Pan115()
            return self._send(200, {"ok": True, **c.file_info(g("file_id"))})

        if path == "/api/debug/115/probe":
            # 排障：试探 115 接口地址（只允许 115 域名，避免变成任意 URL 代理）
            u = g("url") or ""
            if not re.match(r"^https://(?:webapi|clouddownload|115)\.115\.com/", u):
                return self._send(400, {"ok": False, "msg": "只允许 115 域名"})
            return self._send(200, pan.Pan115().probe(u))

        if path == "/api/debug/site/pan":
            # 排障：看详情页里的「网盘」资源长什么样（盘种 / 链接 / 是否分集）
            iid = g("id") or ""
            it = store.get_item(iid) or {}
            url = it.get("url") or g("url") or ""
            if not url:
                return self._send(400, {"ok": False, "msg": "缺少条目"})
            sc = site.SiteClient()
            try:
                p = sc.fetch_page(url)
            except site.SiteError as e:
                return self._send(200, {"ok": False, "msg": str(e), "url": url})
            obj, html = p.get("obj"), p.get("html") or ""
            d = obj.get("d") if isinstance(obj, dict) else None
            PAT = (r"pan\.quark\.cn|drive\.quark\.cn|pan\.baidu\.com|aliyundrive\.com|"
                   r"alipan\.com|cloud\.189\.cn|123pan\.com|pan\.xunlei\.com|"
                   r"drive\.uc\.cn|115\.com/s/|115cdn\.com")
            hosts = {}
            for m in re.finditer(PAT, html):
                k = m.group(0)
                hosts[k] = hosts.get(k, 0) + 1
            frags = []
            for m in re.finditer(r"https?://[^\"'\s<>]{6,160}", html):
                u = m.group(0)
                if re.search(PAT, u) and u not in frags:
                    frags.append(u)
            around = []
            for kw in ("网盘", "夸克", "百度", "阿里", "迅雷", "123"):
                i = html.find(kw)
                if i >= 0:
                    around.append(re.sub(r"\s+", " ",
                                         html[max(0, i - 90):i + 150]))
            # 网盘链接大概率在独立接口里：把 cblist 和一批候选路径都探一遍
            cblist = obj.get("cblist") if isinstance(obj, dict) else None
            dj = d if isinstance(d, dict) else {}
            dirname = str(dj.get("dir") or g("cat") or "tv")
            ident = str(dj.get("id") or g("iid") or "")
            tries = {}
            if ident:
                for cand in ("panurl", "pandownurl", "pan", "wangpan", "purl",
                             "downpan", "pan_downurl", "pandown", "cloudurl"):
                    pth = f"/res/{cand}/{dirname}/{ident}"
                    try:
                        st, txt = sc.get(pth, referer=url)
                        tries[pth] = [st, (txt or "")[:400]]
                    except site.SiteError as e:
                        tries[pth] = ["ERR", str(e)[:120]]
                    except Exception as e:
                        tries[pth] = ["ERR", f"{type(e).__name__}: {e}"[:120]]
            try:
                st0, txt0 = sc.get(f"/res/downurl/{dirname}/{ident}", referer=url)
                tries["/res/downurl/... (基线)"] = [st0, (txt0 or "")[:300]]
            except Exception as e:
                st0, txt0 = 0, ""
                tries["/res/downurl/... (基线)"] = ["ERR", str(e)[:120]]
            try:
                j0 = json.loads(txt0 or "{}")
            except Exception:
                j0 = {}
            pl = j0.get("panlist") or {}
            paninfo = {"keys": list(pl.keys())[:20] if isinstance(pl, dict) else type(pl).__name__,
                       "n": len(pl.get("id") or []) if isinstance(pl, dict) else 0}
            if isinstance(pl, dict) and pl.get("id"):
                paninfo["sample"] = {
                    k: (v[:5] if isinstance(v, list) else str(v)[:120])
                    for k, v in pl.items() if not isinstance(v, dict)}
                for k, v in pl.items():
                    if isinstance(v, dict):
                        paninfo["sample"][k] = {kk: (str(vv)[:90]) for kk, vv in list(v.items())[:6]}
                # type 是 tname 的下标：按盘种分组统计，并给出每种的样例链接
                tnames = pl.get("tname") or []
                urls0 = pl.get("url") or []
                names0 = pl.get("name") or []
                types0 = pl.get("type") or []
                groups = {}
                for i, tp in enumerate(types0):
                    try:
                        lab = tnames[int(tp)]
                    except (IndexError, ValueError, TypeError):
                        lab = f"?{tp}"
                    g = groups.setdefault(lab, {"n": 0, "urls": [], "names": []})
                    g["n"] += 1
                    if len(g["urls"]) < 3:
                        g["urls"].append(str(urls0[i])[:120] if i < len(urls0) else "")
                        g["names"].append(str(names0[i])[:90] if i < len(names0) else "")
                paninfo["groups"] = groups
            return self._send(200, {"ok": True, "url": url, "len": len(html),
                                    "pan": paninfo,
                                    "cblist": str(cblist)[:900],
                                    "d": {k: str(v)[:100] for k, v in dj.items()},
                                    "tries": tries,
                                    "obj_keys": list(obj.keys())[:24] if isinstance(obj, dict) else [],
                                    "hosts": hosts, "urls": frags[:20],
                                    "around": around[:6]})

        if path == "/api/debug/site/pan115":
            # 排障：这条片子的 115 网盘分享（磁力全死时的兜底源）
            iid = g("id") or ""
            it = store.get_item(iid) or {}
            url = it.get("url") or g("url") or ""
            if not url:
                return self._send(400, {"ok": False, "msg": "缺少条目"})
            sc = site.SiteClient()
            try:
                p = sc.fetch_page(url)
            except site.SiteError as e:
                return self._send(200, {"ok": False, "msg": str(e), "url": url})
            d = p.get("obj") if isinstance(p.get("obj"), dict) else {}
            dj = d.get("d") if isinstance(d.get("d"), dict) else {}
            dirname = str(dj.get("dir") or g("cat") or "tv")
            ident = str(dj.get("id") or g("iid") or "")
            raw = sc.downurl_raw(dirname, ident) if ident else {}
            kind = g("kind", "115")
            pans = site.pans_from_downurl(raw)
            if kind != "all":
                if kind == "115":
                    pans = [x for x in pans if x.get("is_115")]
                else:
                    pans = [x for x in pans
                            if kind in str(x.get("kind") or "")
                            or kind in str(x.get("url") or "").lower()]
            return self._send(200, {"ok": True, "url": url, "n": len(pans),
                                    "bad": sorted(store.pan_bad_urls())[:20],
                                    "items": pans[:60]})

        if path == "/api/debug/115/share":
            # 排障：验一条 115 分享（默认只看不转；save=1 才真转存）
            u = g("url") or ""
            pwd = g("pwd") or ""
            if not pan.parse_share(u)[0]:
                return self._send(400, {"ok": False, "msg": "只支持 115 分享链接"})
            c = pan.Pan115()
            code, pwd2 = pan.parse_share(u)
            snap = c.share_ls(code, pwd or pwd2)
            out = {"ok": snap.get("ok"), "code": code, "msg": snap.get("msg"),
                   "count": snap.get("count"),
                   "items": [{k: x.get(k) for k in ("name", "size", "is_dir")}
                             for x in (snap.get("list") or [])][:40]}
            if str(g("save", "0")) == "1":
                cid = g("cid") or jobs._save_path(store.settings(), g("cat") or "")
                want = g("eps") or ""
                want_eps = [int(x) for x in re.split(r"[,\s]+", want) if x.isdigit()] \
                    if want else None
                out["pull"] = c.share_pull(u, pwd or pwd2, cid=str(cid or "0"),
                                            want_eps=want_eps,
                                            allow_pack=str(g("pack", "0")) == "1")
            return self._send(200, out)

        if path == "/api/debug/115/files":
            c = pan.Pan115()
            return self._send(200, {"ok": True, **c.files(cid=int(g("cid", "0") or 0),
                                                        keyword=g("q") or None,
                                                        limit=int(g("limit", "30") or 30))})

        if path == "/api/debug/115/ls":
            c = pan.Pan115()
            pth = g("path") or "/"
            cid = c.cid_of(pth)
            if not cid:
                return self._send(200, {"ok": False, "msg": "路径不存在", "path": pth})
            r = c.files(cid=int(cid), limit=int(g("limit", "100") or 100))
            items = [{"name": x.get("name"), "is_dir": bool(x.get("is_dir")),
                      "size": x.get("size"), "size_txt": x.get("size_txt"),
                      "t": x.get("t")} for x in (r.get("items") or [])]
            return self._send(200, {"ok": True, "path": pth, "cid": str(cid),
                                    "n": len(items), "items": items})

        if path == "/api/debug/115/tasks":
            c = pan.Pan115()
            out = []
            total = None
            pages = max(1, min(6, int(g("pages", "4") or 4)))
            for pg in range(1, pages + 1):
                r = c.tasks(page=pg, page_size=50)
                if total is None:
                    total = r.get("count")
                lst = r.get("tasks") or []
                for t in lst:
                    out.append({"h": str(t.get("info_hash") or "").lower(),
                                "name": str(t.get("name") or "")[:64],
                                "pc": t.get("percentDone"), "st": t.get("status"),
                                "size": t.get("size")})
                if len(lst) < 50:
                    break
            return self._send(200, {"ok": True, "count": total,
                                    "n": len(out), "tasks": out})

        if path == "/api/debug/115/reap":
            # 手动跑一次死种收尾：?hash=a,b 只判死这几条；否则按宽限期跑全量。
            from . import jobs as _jobs
            hs = [x.strip() for x in (g("hash") or "").split(",") if x.strip()]
            if hs:
                r = _jobs.kill_hashes(pan.Pan115(), hs,
                                      str(g("why") or "手动判死"))
                return self._send(200, {"ok": True, **r})
            h = g("hours")
            try:
                age = float(h) if h not in (None, "") else None
            except ValueError:
                age = None
            r = _jobs._reap_tasks(pan.Pan115(), min_age_h=age)
            return self._send(200, {"ok": True, **r})

        if path == "/api/debug/115/add":
            h = (g("hash") or "").lower()
            m = g("magnet") or ""
            if not m and not h:
                return self._send(400, {"ok": False, "msg": "缺少 hash / magnet"})
            mg = m or f"magnet:?xt=urn:btih:{h}"
            r = pan.Pan115().add_magnet(mg)
            return self._send(200, {"ok": bool(r.get("ok")), "raw": r})

        if path == "/api/debug/115/deltask":
            h = (g("hash") or "").lower()
            if not h:
                return self._send(400, {"ok": False, "msg": "缺少 hash"})
            files = str(g("files", "0")).lower() not in ("0", "false", "no", "")
            return self._send(200, {"ok": True,
                                    **pan.Pan115().del_task(h, del_files=files)})

        if path == "/api/debug/115/bad":
            return self._send(200, {"ok": True, "n": len(store.bad_hashes()),
                                    "bad": store.bad_detail()})

        if path == "/api/debug/115/find":
            h = g("hash") or ""
            task = pan.Pan115().find_task(h)
            if not task:
                return self._send(200, {"ok": False, "msg": "没找到该 infohash 的任务", "hash": h})
            return self._send(200, {"ok": True, "hash": h,
                                    "file_id": task.get("delete_file_id") or task.get("file_id"),
                                    "name": task.get("name"), "size": task.get("size"),
                                    "status": task.get("status_text"),
                                    "wp_path_id": task.get("wp_path_id"),
                                    "info": pan.Pan115().file_info(
                                        task.get("delete_file_id") or task.get("file_id"))})

        if path == "/api/debug/115/del":
            fid = g("fid") or ""
            if g("confirm") != "1":
                return self._send(200, {"ok": True, "dry": True, "fid": fid,
                                        "info": pan.Pan115().file_info(fid)})
            return self._send(200, pan.Pan115().delete_file(fid))

        if path == "/api/debug/115":
            c = pan.Pan115()
            out = {"logged": c.logged_in(), "cookies": sorted(c.cookies().keys()) if hasattr(c, "cookies") else []}
            for stat in (0, 1, 2, 3, 4):
                try:
                    r = c.tasks(stat=stat)
                    out[f"stat{stat}"] = {"ok": r["ok"], "count": r["count"],
                                          "n": len(r["tasks"]), "raw": r["raw"],
                                          "sample": r["tasks"][:2]}
                except Exception as e:
                    out[f"stat{stat}"] = {"err": f"{type(e).__name__}: {e}"}
            try:
                out["quota"] = c.quota()
            except Exception as e:
                out["quota"] = {"err": str(e)}
            return self._send(200, out)

        if path == "/api/debug/pages":
            cat = g("cat", "mv")
            a, b = int(g("from", "1") or 1), int(g("to", "12") or 12)
            sc = site.SiteClient()
            if g("delay"):
                sc.page_delay = float(g("delay"))
            rows = []
            for pg in range(a, b + 1):
                t0 = time.time()
                try:
                    r = sc.listing_page(cat, pg)
                    rows.append({"pg": pg, "n": len(r["items"]), "pages": r.get("pages"),
                                 "curr": r.get("curr"), "len": r.get("len"),
                                 "ptype": r.get("ptype"), "s": round(time.time() - t0, 1),
                                 "head": r.get("head") if not r["items"] else None})
                except Exception as e:
                    rows.append({"pg": pg, "err": f"{type(e).__name__}: {e}",
                                 "s": round(time.time() - t0, 1)})
                time.sleep(sc.page_delay)
            return self._send(200, {"ok": True, "cat": cat, "rows": rows})

        if path == "/api/debug/raw":
            p = g("path", "/")
            sc = site.SiteClient()
            st, html = sc.get(p)
            obj = sc.parse_obj(html)
            keys = {k: (list(v.keys())[:20] if isinstance(v, dict) else
                        (f"list[{len(v)}]" if isinstance(v, list) else type(v).__name__))
                    for k, v in obj.items()}
            out = {"ok": True, "status": st, "len": len(html),
                   "page_type": sc.page_type(html),
                   "obj_keys": keys,
                   "html_head": html[:1200]}
            if g("full"):
                out["obj"] = obj
                out["html"] = html[:int(g("html", "6000"))]
            return self._send(200, out)

        # ---- 手动转存（磁力 / 115网盘 两个按钮）----
        if path == "/api/convert/status":
            tid = g("id") or ""
            with _lock:
                if tid:
                    t = dict(_conv_tasks.get(tid) or {})
                else:
                    # 不带 id：返回当前最值得看的任务（运行中优先，否则最新一条）
                    running = {k: v for k, v in _conv_tasks.items()
                               if v.get("state") in ("running", "queued")}
                    t = dict(_conv_tasks[max(running or _conv_tasks)]) if _conv_tasks else {}
                    if t:
                        t["id"] = max(running or _conv_tasks)
            if not t:
                return self._send(200, {"ok": False, "msg": "任务不存在或已过期"})
            return self._send(200, {"ok": True, **t})

        return self._send(404, {"ok": False, "msg": "unknown api"})

    # ---------- POST ----------
    def _api_post(self, path, body):
        if path == "/api/site/login":
            username = (body.get("username") or "").strip()
            password = body.get("password") or ""
            code = body.get("code") or ""
            if not username or not password:
                return self._send(400, {"ok": False, "msg": "请填写账号与密码"})
            sc = site.SiteClient()
            try:
                res = sc.login(username, password, code)
            except site.SiteError as e:
                return self._send(200, {"ok": False, "msg": str(e)})
            if res.get("ok"):
                store.save_settings({"site": {"username": username, "password": password}})
                store.set_kv("nologin_notified", 0)
                store.set_kv("nologin_at", 0)
                store.log(f"站点登录成功: {username}", "ok")
                return self._send(200, {"ok": True, "msg": "登录成功"})
            if res.get("captcha"):
                cap = sc.captcha()
                return self._send(200, {"ok": False, "need_captcha": True,
                                        "msg": res.get("msg") or "需要验证码", "captcha": cap})
            return self._send(200, {"ok": False, "msg": res.get("msg") or "登录失败"})

        if path == "/api/site/captcha/check":
            pts = body.get("points") or []
            if not pts:
                return self._send(400, {"ok": False, "msg": "缺少点击坐标"})
            sc = site.SiteClient()
            ok = sc.captcha_check([str(p) for p in pts])
            info = "%s;350;200" % "-".join(str(p) for p in pts)
            return self._send(200, {"ok": ok, "info": info if ok else "",
                                    "msg": "验证通过" if ok else "点选不正确，请重试"})

        if path == "/api/site/logout":
            site.SiteClient().logout()
            return self._send(200, {"ok": True})

        if path == "/api/settings":
            cfg = store.save_settings(body or {})
            return self._send(200, {"ok": True, "settings": cfg})

        if path == "/api/sync":
            if jobs.state()["running"]:
                return self._send(200, {"ok": False, "msg": "正在同步中，请稍候"})
            full = bool((body or {}).get("full"))
            backfill = bool((body or {}).get("backfill", full))
            threading.Thread(target=jobs.sync_once,
                             kwargs={"full": full, "backfill": backfill},
                             daemon=True).start()
            return self._send(200, {"ok": True, "full": full,
                                    "msg": "已开始整站抓取" if full else "已开始同步"})

        if path == "/api/push/pause":
            paused = bool((body or {}).get("paused"))
            cfg = store.save_settings({"auto": {"paused": paused}})
            store.log("⏸ 已暂停自动推送" if paused else "▶ 已恢复自动推送",
                      "warn" if paused else "ok")
            return self._send(200, {"ok": True, "paused": paused,
                                    "auto": cfg["auto"]})

        if path == "/api/wash/scan":
            body = body or {}
            res = jobs.start_scan(limit=int(body.get("limit") or 40),
                                  offset=int(body.get("offset") or 0),
                                  cat=body.get("cat") or None,
                                  min_gain=int(body.get("min_gain", 1) or 1),
                                  only_id=body.get("item_id") or None)
            return self._send(200, res)

        if path == "/api/wash/run":
            body = body or {}
            one = body.get("item_id")
            many = body.get("items")
            targets = []
            if one:
                targets = [{"item_id": one, "btih": body.get("btih")}]
            elif isinstance(many, list):
                targets = [{"item_id": x.get("item_id") or x.get("id"),
                            "btih": x.get("btih")} for x in many if isinstance(x, dict)]
            if not targets:
                return self._send(400, {"ok": False, "msg": "缺少 item_id"})

            def _run():
                ok = fail = 0
                for t in targets:
                    try:
                        r = jobs.wash_one(t["item_id"], t.get("btih"))
                    except Exception as e:
                        store.log("洗版异常: " + str(e)[:200], "error")
                        r = {"ok": False}
                    if r.get("ok"):
                        ok += 1
                    else:
                        fail += 1
                    time.sleep(1.5)
                store.log(f"洗版完成：成功 {ok} 条，失败 {fail} 条", "ok" if not fail else "warn")

            threading.Thread(target=_run, daemon=True).start()
            return self._send(200, {"ok": True, "count": len(targets), "msg": "已开始洗版"})

        if path == "/api/subs/add":
            raw = str(body.get("kw") or body.get("title") or "").strip()
            if not raw:
                return self._send(200, {"ok": False, "msg": "先填片名"})
            # 从卡片/候选点进来的是站点原始标题（带画质、广告、年份），先洗成片名
            kw = subs.clean_kw(raw) or raw
            year = str(body.get("year") or "").strip()
            if not year:
                year = subs.year_of({"title": raw, "sub": body.get("sub") or ""})
            try:
                sid, created = store.sub_add({
                    "kw": kw,
                    "cat": body.get("cat") or "",
                    "year": year,
                    "season": body.get("season") or 0,
                    "qmin": body.get("qmin") or body.get("resolution") or "",
                    "min_gb": body.get("min_gb") or 0,
                    "max_gb": body.get("max_gb") or 0,
                    "auto_wash": body.get("auto_wash", True),
                    "best_version": body.get("best_version", body.get("auto_wash", True)),
                    "best_version_full": body.get("best_version_full") or 0,
                    "wait_end": body.get("wait_end", True),
                    "ep_mode": body.get("ep_mode") or "",
                    "ep_from": body.get("ep_from") or 0,
                    "filter_inc": body.get("filter_inc") or "",
                    "filter_exc": body.get("filter_exc") or "",
                    "effect": body.get("effect") or "",
                    "search_interval": body.get("search_interval") or 0,
                    "media_id": body.get("media_id") or body.get("item_id") or "",
                    "save_path": body.get("save_path") or "",
                })
            except ValueError as e:
                return self._send(200, {"ok": False, "msg": str(e)})
            if not created:
                return self._send(200, {"ok": True, "id": sid, "dup": True,
                                        "msg": "这个片子已经在订阅里了"})
            store.log(f"新增订阅：{kw}" + (f"（{year}）" if year else ""), "ok")
            store.sub_log(sid, kw, "add", "已加入订阅")
            r = subs.run(ids=[sid])
            return self._send(200, {"ok": True, "id": sid, "created": True, "run": r,
                                    "msg": "已订阅，马上查一遍片源" if r.get("started")
                                           else "已订阅，稍后自动检查"})

        if path == "/api/subs/update":
            sid = int(body.get("id") or 0)
            cur = store.sub_get(sid)
            if not cur:
                return self._send(404, {"ok": False, "msg": "订阅不存在"})
            patch = {}
            for k in ("cat", "year", "qmin", "min_gb", "max_gb"):
                if k in body:
                    patch[k] = body[k] if k in ("min_gb", "max_gb") else str(body[k] or "")
            # MP V3 模型的新字段
            for k in ("filter_inc", "filter_exc", "effect", "save_path"):
                if k in body:
                    patch[k] = str(body.get(k) or "")[:200]
            for k in ("season", "search_interval", "best_version_full",
                      "best_version", "auto_wash", "wait_end", "enabled",
                      "start_episode", "total_episode"):
                if k in body:
                    try:
                        patch[k] = max(0, int(float(body.get(k) or 0)))
                    except (TypeError, ValueError):
                        patch[k] = 0
            if "best_version" in patch:
                patch["auto_wash"] = patch["best_version"]
            if "ep_mode" in body:
                em = str(body.get("ep_mode") or "").strip().lower()
                patch["ep_mode"] = em if em in ("follow", "all") else ""
            if "ep_from" in body:
                try:
                    patch["ep_from"] = max(0, int(float(body.get("ep_from") or 0)))
                except (TypeError, ValueError):
                    patch["ep_from"] = 0
            # 复用 V3 状态机：enabled=0 等价于 S（暂停）
            if patch.get("enabled") == 0:
                patch["state"] = "S"
            elif patch.get("enabled") == 1:
                patch["state"] = "R"
            # 追更本身就是不等完结；两个开关别互相打架
            if patch.get("ep_mode") == "follow":
                patch["wait_end"] = 0
            if "kw" in body:
                new_kw = subs.clean_kw(str(body.get("kw") or "")) or str(body.get("kw") or "").strip()
                if new_kw:
                    patch["kw"] = new_kw
            # 换了目标（片名/栏目/年份）就等于重新订阅：旧的推送记录清空，
            # 否则新目标会被旧的 btih 卡住，永远「已是最佳」
            if any(k in patch and str(patch[k]) != str(cur.get(k) or "")
                   for k in ("kw", "cat", "year")):
                patch.update(btih="", item_id="", title="", size_text="",
                             size_bytes=0, qscore=0, status="waiting")
            # 改了剧集更新方式 / 起始集：追更进度要归零，否则新设置根本不生效
            elif (("ep_mode" in patch and str(patch["ep_mode"]) != str(cur.get("ep_mode") or ""))
                  or ("ep_from" in patch
                      and int(patch["ep_from"] or 0) != int(cur.get("ep_from") or 0))):
                patch.update(btih="", size_text="", size_bytes=0, qscore=0,
                             last_ep=0, ep_done="", status="waiting",
                             state="N", current_priority=0, note="[]",
                             episode_priority="{}")
            # 改了过滤规则 / 最低画质：基线作废，重新按新规则判一次洗版
            elif any(k in patch and str(patch[k]) != str(cur.get(k) or "")
                     for k in ("filter_inc", "filter_exc", "qmin", "effect")):
                patch.update(current_priority=0)
            store.sub_update(sid, **patch)
            sub = store.sub_get(sid)
            # 暂停中恢复、改了追更方式 / 起始集 / 过滤规则，都立刻按新规则查一遍
            if (patch.get("enabled") == 1 or "ep_mode" in patch
                    or "ep_from" in patch or "filter_inc" in patch
                    or "filter_exc" in patch or "qmin" in patch):
                subs.run(ids=[sid])
            store.log(f"订阅已更新：{sub.get('kw')}", "info")
            return self._send(200, {"ok": True, "sub": sub})

        if path == "/api/subs/status":
            # 改状态机：N 新建 / R 订阅中 / P 待定 / S 暂停
            sid = int(body.get("id") or 0)
            if not store.sub_get(sid):
                return self._send(404, {"ok": False, "msg": "订阅不存在"})
            st = str(body.get("state") or "").upper()[:1]
            if st not in ("N", "R", "P", "S"):
                return self._send(400, {"ok": False, "msg": "state 只能是 N/R/P/S"})
            store.sub_state_set(sid, st)
            sub = store.sub_get(sid)
            store.sub_log(sid, sub.get("kw"), "state",
                          f"状态改为 {subs.STATE_LABEL.get(st, st)}")
            if st in ("N", "R"):
                subs.run(ids=[sid])
            return self._send(200, {"ok": True, "sub": sub})

        if path == "/api/subs/del":
            sid = int(body.get("id") or 0)
            sub = store.sub_get(sid)
            if not sub:
                return self._send(404, {"ok": False, "msg": "订阅不存在"})
            store.sub_del(sid)
            store.log(f"已删除订阅：{sub.get('kw')}", "info")
            return self._send(200, {"ok": True})

        if path == "/api/subs/check":
            sid = int(body.get("id") or 0)
            r = subs.run(ids=[sid] if sid else None)
            return self._send(200, r)

        if path == "/api/subs/find":
            r = subs.find(str(body.get("kw") or ""), limit=int(body.get("limit") or 20))
            return self._send(200, r)

        if path == "/api/subs/log/clear":
            n = store.sub_log_clear()
            return self._send(200, {"ok": True, "deleted": n})

        if path == "/api/subs/config":
            patch = {}
            if "enabled" in body:
                patch["enabled"] = bool(body.get("enabled"))
            for k in ("pan_fallback", "pan_allow_pack"):
                if k in body:
                    patch[k] = bool(body.get(k))
            for k in ("interval", "max_per_run", "delay", "pan_max_try"):
                if k in body:
                    try:
                        patch[k] = max(1, int(float(body.get(k) or 0)))
                    except (TypeError, ValueError):
                        pass
            cfg = store.save_settings({"sub": patch})
            return self._send(200, {"ok": True, "sub": cfg["sub"]})

        if path == "/api/push":
            item_id = body.get("item_id")
            if not item_id:
                return self._send(400, {"ok": False, "msg": "缺少 item_id"})
            res = jobs.push_single(item_id, body.get("magnet"))
            return self._send(200, res)

        if path == "/api/115/logout":
            pan.Pan115().clear()
            store.log("已退出 115 登录", "warn")
            return self._send(200, {"ok": True})

        if path == "/api/115/mkdir":
            # 115 上建目录（逐级），给按栏目分目录用；已存在则直接返回现有 cid
            p = (body.get("path") or "").strip()
            if not p:
                return self._send(400, {"ok": False, "msg": "缺少 path"})
            c = pan.Pan115()
            if not c.logged_in():
                return self._send(200, {"ok": False, "msg": "115 未登录"})
            r = c.mkdir(p)
            if r.get("ok") and r.get("created"):
                store.log(f"已新建 115 目录：{r['path']}", "ok")
            return self._send(200, r)

        if path == "/api/115/savepath":
            pid = str(body.get("id", "") or "")
            name = body.get("name") or ""
            cat = str(body.get("cat") or "")
            # 按栏目存：电视剧和电影用不同目录，后面自己整理时不会混在一起
            if cat in ("mv", "tv", "ac"):
                pan_cfg = dict(store.settings().get("pan") or {})
                paths = dict(pan_cfg.get("paths") or {})
                if pid:
                    paths[cat] = {"id": pid, "name": name}
                else:
                    paths.pop(cat, None)
                pan_cfg["paths"] = paths
                cfg = store.save_settings({"pan": pan_cfg})
                label = {"mv": "电影", "tv": "电视剧", "ac": "动漫"}.get(cat, cat)
                store.log(f"{label}的 115 保存目录已设为：{name or '跟随默认目录'}", "ok")
            else:
                cfg = store.save_settings({"pan": {"save_path_id": pid, "save_path_name": name}})
                store.log(f"115 默认保存目录已设为：{name or '根目录'}", "ok")
            return self._send(200, {"ok": True, "pan": cfg["pan"]})

        # ---- 手动转存（磁力 / 115网盘 两个按钮）----
        if path == "/api/convert":
            item_id = body.get("item_id")
            mode = str(body.get("mode") or "").strip()
            if not item_id:
                return self._send(400, {"ok": False, "msg": "缺少 item_id"})
            if mode not in ("magnet", "pan115"):
                return self._send(400, {"ok": False, "msg": "未知路线"})
            magnet = body.get("magnet") or None
            eps = body.get("eps") or None
            want_eps = None
            if eps:
                try:
                    want_eps = [int(x) for x in (eps if isinstance(eps, list)
                                                  else re.split(r"[,\s]+", str(eps))) if str(x).isdigit()]
                except Exception:
                    want_eps = None
            with _lock:
                _conv_seq[0] += 1
                tid = str(_conv_seq[0])
                _conv_tasks[tid] = {"state": "queued", "mode": mode, "item_id": str(item_id),
                                    "ok": False, "msg": "排队中", "result": {}, "started": time.time()}
            threading.Thread(target=_conv_run,
                             kwargs={"tid": tid, "item_id": item_id, "mode": mode,
                                     "magnet": magnet, "want_eps": want_eps},
                             daemon=True).start()
            return self._send(200, {"ok": True, "id": tid,
                                    "msg": "已开始" + {"magnet": "磁力离线",
                                                    "pan115": "115 网盘转存"}.get(mode, "转存")})

        return self._send(404, {"ok": False, "msg": "unknown api"})


def main():
    store.init()
    store.log("服务启动", "info")
    jobs.start()
    subs.start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    srv.daemon_threads = True
    print(f"[jiaofu115] listening on :{PORT}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
