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

from . import jobs, pan, site, store

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


class Handler(BaseHTTPRequestHandler):
    server_version = "JiaofuBot/1.0"
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
                "site": {"base": cfg["site"]["base"], "username": cfg["site"].get("username", ""),
                         "logged": chk["ok"], "msg": chk["msg"]},
                "pan": {"logged": P.logged_in(), "channels": pan.CHANNELS},
                "settings": safe,
                "stats": store.stats(),
                "by_cat": store.count_by_cat(),
                "baseline_at": store.get_kv("baseline_at", 0),
                "sync": jobs.state(),
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
            return self._send(200, {"ok": True, **c.quota()})

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

        if path == "/api/debug/115/files":
            c = pan.Pan115()
            return self._send(200, {"ok": True, **c.files(cid=int(g("cid", "0") or 0),
                                                        keyword=g("q") or None,
                                                        limit=int(g("limit", "30") or 30))})

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

        if path == "/api/115/savepath":
            pid = str(body.get("id", "") or "")
            name = body.get("name") or ""
            cfg = store.save_settings({"pan": {"save_path_id": pid, "save_path_name": name}})
            store.log(f"115 保存目录已设为：{name or '根目录'}", "ok")
            return self._send(200, {"ok": True, "pan": cfg["pan"]})

        return self._send(404, {"ok": False, "msg": "unknown api"})


def main():
    store.init()
    store.log("服务启动", "info")
    jobs.start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    srv.daemon_threads = True
    print(f"[jiaofu115] listening on :{PORT}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
