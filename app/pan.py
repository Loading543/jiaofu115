"""115 网盘客户端（纯标准库）：扫码登录 + 磁力离线下载。

扫码渠道（app）：
  web        115生活网页端
  android    115 App 安卓
  ios        115 App 苹果
  alipaymini 支付宝小程序
  wechatmini 微信小程序
  qandroid   115 网盘 TV 版
  harmony    鸿蒙
"""
import gzip
import http.cookiejar
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

from . import store

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

QR_HOST = "https://qrcodeapi.115.com"
DL_HOST = "https://clouddownload.115.com"

# 保留的 cookie 名（115 登录态关键字段）
KEEP_COOKIES = ("UID", "CID", "SEID", "KID", "OOFL", "PHPSESSID")

CHANNELS = [
    {"app": "web", "name": "115 网页版", "desc": "用 115 生活 App 扫", "icon": "🌐"},
    {"app": "android", "name": "手机 App", "desc": "115 App 安卓扫码", "icon": "📱"},
    {"app": "ios", "name": "iPhone App", "desc": "115 App 苹果扫码", "icon": "🍎"},
    {"app": "alipaymini", "name": "支付宝", "desc": "支付宝扫一扫授权", "icon": "🅰️"},
    {"app": "wechatmini", "name": "微信", "desc": "微信扫一扫授权", "icon": "💬"},
    {"app": "qandroid", "name": "TV 版", "desc": "115 网盘 TV 端", "icon": "📺"},
    {"app": "harmony", "name": "鸿蒙", "desc": "HarmonyOS 客户端", "icon": "🧩"},
]


class PanError(Exception):
    pass


class Pan115:
    def __init__(self, cookies=None):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        for k, v in (cookies if cookies is not None else store.pan_cookies()).items():
            self.set_cookie(k, v)

    # ---------- cookies ----------
    def set_cookie(self, name, value, domain=".115.com"):
        c = http.cookiejar.Cookie(
            version=0, name=name, value=str(value), port=None, port_specified=False,
            domain=domain, domain_specified=True, domain_initial_dot=True, path="/",
            path_specified=True, secure=True, expires=None, discard=False,
            comment=None, comment_url=None, rest={}, rfc2109=False)
        self.jar.set_cookie(c)

    def cookie_dict(self):
        return {c.name: c.value for c in self.jar}

    def save(self):
        store.set_pan_cookies({k: v for k, v in self.cookie_dict().items() if k in KEEP_COOKIES})

    def clear(self):
        self.jar.clear()
        store.set_pan_cookies({})

    def logged_in(self):
        d = self.cookie_dict()
        return bool(d.get("UID") and (d.get("SEID") or d.get("CID")))

    # ---------- http ----------
    def _req(self, url, data=None, method=None, timeout=20, referer="https://115.com/"):
        headers = {
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Accept-Encoding": "gzip, deflate",
            "Referer": referer,
        }
        body = None
        if data is not None:
            body = urllib.parse.urlencode(data, doseq=True).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=timeout) as r:
                raw = r.read()
                enc = (r.headers.get("Content-Encoding") or "").lower()
                if enc == "gzip":
                    raw = gzip.decompress(raw)
                elif enc == "deflate":
                    try:
                        raw = zlib.decompress(raw)
                    except zlib.error:
                        raw = zlib.decompress(raw, -zlib.MAX_WBITS)
                txt = raw.decode("utf-8", "replace")
                setc = r.headers.get_all("Set-Cookie") or []
                return r.status, txt, setc
        except urllib.error.HTTPError as e:
            try:
                txt = e.read().decode("utf-8", "replace")
            except Exception:
                txt = ""
            return e.code, txt, []
        except TimeoutError as e:
            raise PanError("TIMEOUT") from e
        except urllib.error.URLError as e:
            if isinstance(getattr(e, "reason", None), TimeoutError):
                raise PanError("TIMEOUT") from e
            raise PanError(f"网络错误: {e}")
        except Exception as e:
            if "timed out" in str(e).lower():
                raise PanError("TIMEOUT") from e
            raise PanError(f"网络错误: {e}")

    @staticmethod
    def _json(txt):
        try:
            return json.loads(txt)
        except Exception:
            m = re.search(r"\{.*\}", txt or "", re.S)
            if m:
                try:
                    return json.loads(m.group(0))
                except Exception:
                    pass
        return {}

    @staticmethod
    def _ok(j):
        if not isinstance(j, dict):
            return False
        if j.get("state") is True:
            return True
        return j.get("code") == 0 and j.get("state") is not False

    # ---------- 扫码登录 ----------
    def qr_token(self, app="web"):
        st, txt, _ = self._req(f"{QR_HOST}/api/1.0/{app}/1.0/token/", timeout=15)
        j = self._json(txt)
        d = j.get("data") or {}
        if not d.get("uid"):
            raise PanError(f"获取二维码失败: {j.get('message') or txt[:100]}")
        return {"uid": d["uid"], "time": d.get("time"), "sign": d.get("sign"),
                "qrcode": d.get("qrcode") or "", "app": app}

    def qr_status(self, uid, t, sign, timeout=35):
        """115 的该接口是长轮询：无状态变化时会挂住，超时视为「继续等待」。"""
        q = urllib.parse.urlencode({"uid": uid, "time": t, "sign": sign})
        try:
            st, txt, _ = self._req(f"{QR_HOST}/get/status/?{q}", timeout=timeout)
        except PanError as e:
            if str(e) == "TIMEOUT":
                return {"ok": True, "status": None, "msg": "", "timeout": True}
            raise
        j = self._json(txt)
        d = j.get("data") or {}
        return {"ok": self._ok(j) or j.get("code") == 0,
                "status": d.get("status"), "msg": d.get("msg") or j.get("message") or ""}

    def qr_result(self, uid, app="web"):
        st, txt, setc = self._req(f"{QR_HOST}/app/1.0/{app}/1.0/login/qrcode/",
                                  data={"account": uid}, timeout=20)
        j = self._json(txt)
        got = {}
        for sc in setc:
            m = re.match(r"([A-Za-z_]+)=([^;]+)", sc)
            if m and m.group(1) in KEEP_COOKIES:
                got[m.group(1)] = m.group(2)
        data = j.get("data") or {}
        cookie = data.get("cookie") if isinstance(data.get("cookie"), dict) else {}
        for k in KEEP_COOKIES:
            if cookie.get(k):
                got[k] = str(cookie[k])
        if got:
            for k, v in got.items():
                self.set_cookie(k, v)
            self.save()
            return {"ok": True, "cookies": self.cookie_dict()}
        msg = j.get("message") or data.get("msg") or txt[:150]
        return {"ok": False, "msg": f"取登录态失败: {msg}"}

    # ---------- 离线下载 ----------
    def add_magnet(self, magnet, wp_path_id=None):
        if not self.logged_in():
            return {"ok": False, "msg": "115 未登录"}
        # 115 对带 dn=/长文件名的磁力容易报 decode fail，先用纯 infohash 提交
        m = re.search(r"btih:([0-9a-fA-F]{40}|[A-Za-z2-7]{32})", magnet or "")
        variants = []
        if m:
            variants.append("magnet:?xt=urn:btih:%s" % m.group(1))
        if magnet and magnet not in variants:
            variants.append(magnet)
        last = ""
        for mg in variants:
            payload = {"url[0]": mg}
            if wp_path_id not in (None, "", 0, "0"):
                payload["wp_path_id"] = str(wp_path_id)
            attempts = [
                (f"{DL_HOST}/lixianssp/?ac=add_task_urls", payload),
                (f"{DL_HOST}/web/?ac=add_task_urls", payload),
                (f"{DL_HOST}/lixianssp/?ac=add_task_url",
                 {"url": mg, **({"wp_path_id": str(wp_path_id)} if payload.get("wp_path_id") else {})}),
            ]
            for url, data in attempts:
                try:
                    st, txt, _ = self._req(url, data=data, method="POST", timeout=25,
                                           referer="https://115.com/?ct=lixian")
                except PanError as e:
                    last = str(e)
                    continue
                j = self._json(txt)
                if self._ok(j):
                    return {"ok": True, "data": j, "endpoint": url, "magnet": mg}
                last = j.get("message") or j.get("msg") or j.get("error_msg") or txt[:180] or f"HTTP {st}"
                if "登录" in str(last):
                    return {"ok": False, "msg": f"115 登录态失效：{last}"}
                if "decode fail" not in str(last).lower():
                    continue
        return {"ok": False, "msg": last or "提交失败"}

    def tasks(self, page=1, page_size=30, stat=0):
        q = urllib.parse.urlencode({"ac": "task_lists", "page": page, "page_size": page_size, "stat": stat})
        st, txt, _ = self._req(f"{DL_HOST}/?{q}", timeout=20, referer="https://115.com/?ct=lixian")
        j = self._json(txt)
        if not isinstance(j, dict):
            return {"ok": False, "count": 0, "tasks": [], "raw": txt[:200]}
        # 115 把 count/tasks 放顶层，老接口放 data 里，两者都兼容
        data = j.get("data")
        if isinstance(data, dict):
            cnt = data.get("count") or data.get("total") or 0
            lst = data.get("tasks") or data.get("list") or []
        elif isinstance(data, list):
            lst = data
            cnt = j.get("count") or len(lst)
        else:
            cnt = j.get("count") or j.get("total") or 0
            lst = j.get("tasks") or j.get("list") or []
        ok = self._ok(j)
        return {"ok": ok, "count": cnt, "tasks": lst if isinstance(lst, list) else [],
                "raw": None if ok else j}

    def quota(self):
        st, txt, _ = self._req(f"{DL_HOST}/?ac=get_quota_info", timeout=15, referer="https://115.com/?ct=lixian")
        j = self._json(txt)
        d = j.get("data")
        if not isinstance(d, dict):
            d = {"quota": j.get("quota"), "total": j.get("total")}
        return {"ok": self._ok(j), "data": d, "raw": j}

    def find_task(self, info_hash, max_pages=8, page_size=50):
        """按 infohash 找离线任务记录（洗版要拿到旧的 file_id 才能删）。"""
        h = str(info_hash or "").lower()
        if not h:
            return None
        for pg in range(1, max_pages + 1):
            r = self.tasks(page=pg, page_size=page_size)
            lst = r.get("tasks") or []
            for t in lst:
                if str(t.get("info_hash") or "").lower() == h:
                    return t
            if len(lst) < page_size:
                break
        return None

    def delete_file(self, fid):
        """把文件/目录移入 115 回收站（可还原，不是物理删除）。"""
        fid = str(fid or "")
        if not fid:
            return {"ok": False, "msg": "缺少 file_id"}
        info = self.file_info(fid)
        pid = str(info.get("pid") or "0")
        variants = [
            {f"fid[{0}]": fid, "pid": pid},
            {"fid[]": fid, "pid": pid},
            {"fid": fid, "pid": pid},
        ]
        last = ""
        for data in variants:
            try:
                st, txt, _ = self._req("https://webapi.115.com/rb/delete", data=data,
                                       method="POST", timeout=25, referer="https://115.com/")
            except PanError as e:
                last = str(e)
                continue
            j = self._json(txt)
            if self._ok(j):
                return {"ok": True, "fid": fid, "pid": pid, "raw": j}
            last = j.get("error") or j.get("message") or txt[:150] or f"HTTP {st}"
        return {"ok": False, "msg": last or "删除失败", "fid": fid, "pid": pid}

    def del_task(self, info_hash):
        """删掉离线任务记录；del_source_file=1 连源文件一起清。"""
        h = str(info_hash or "").lower()
        if not h:
            return {"ok": False, "msg": "缺少 infohash"}
        for endpoint, data in (
            (f"{DL_HOST}/lixianssp/", {"ac": "task_del", "hash[0]": h, "del_source_file": "1"}),
            (f"{DL_HOST}/", {"ac": "task_del", "hash[0]": h, "del_source_file": "1"}),
        ):
            try:
                st, txt, _ = self._req(endpoint, data=data, method="POST", timeout=20,
                                       referer="https://115.com/?ct=lixian")
            except PanError:
                continue
            j = self._json(txt)
            if self._ok(j):
                return {"ok": True, "raw": j}
        return {"ok": False, "msg": "删任务失败"}

    def file_info(self, file_id):
        url = f"https://webapi.115.com/files/get_info?file_id={file_id}"
        st, txt, _ = self._req(url, timeout=20, referer="https://115.com/")
        j = self._json(txt)
        d = j.get("data")
        if isinstance(d, list):
            d = d[0] if d else {}
        d = d if isinstance(d, dict) else {}
        return {"ok": bool(d), "cid": d.get("cid"), "pid": d.get("pid"),
                "name": d.get("n") or d.get("name"), "size": d.get("s"),
                "is_dir": str(d.get("fid") or "") == "0", "raw": j if not d else None}

    def files(self, cid=0, keyword=None, limit=30, offset=0):
        """查网盘文件；keyword 走 115 的搜索接口"""
        if keyword:
            q = urllib.parse.urlencode({"search_value": keyword, "aid": 1, "cid": cid,
                                        "offset": offset, "limit": limit})
            url = f"https://webapi.115.com/files/search?{q}"
        else:
            q = urllib.parse.urlencode({"aid": 1, "cid": cid, "o": "user_ptime", "asc": 0,
                                        "offset": offset, "show_dir": 1, "limit": limit})
            url = f"https://webapi.115.com/files?{q}"
        st, txt, _ = self._req(url, timeout=25, referer="https://115.com/")
        j = self._json(txt)
        data = j.get("data")
        if isinstance(data, list):
            rows = data
        elif isinstance(data, dict):
            rows = data.get("list") or []
        else:
            rows = j.get("list") or []
        out = []
        for r in rows if isinstance(rows, list) else []:
            if not isinstance(r, dict):
                continue
            name = r.get("n") or r.get("name")
            fid = str(r.get("fid") or "")
            # 115 结构：目录把自己的 id 放在 cid、没有 fid；文件才有 fid，cid 是父目录
            is_dir = (not fid) and bool(str(r.get("cid") or ""))
            if str(r.get("is_dir")).lower() in ("1", "true") or str(r.get("ico") or "").lower() in ("folder", "dir"):
                is_dir = True
            out.append({"name": name,
                        "cid": str(r.get("cid") or r.get("fid") or ""),
                        "size": int(r.get("s") or r.get("size") or 0),
                        "is_dir": bool(is_dir),
                        "time": r.get("t") or r.get("user_ptime")})
        return {"ok": self._ok(j) or bool(out), "count": j.get("count"), "items": out,
                "sample": rows[:3] if isinstance(rows, list) else None,
                "raw": None if out else j}
