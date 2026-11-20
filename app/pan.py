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
import os
import random
import re
import threading
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

# 提交离线时补的公共 tracker。
# 站点给的磁力常常只有 xt + dn，一个 tracker 都没有，只能靠 DHT 找 peer；
# 它自己压的种在 DHT 里经常一个 peer 也没有，115 就一直「等待中 / 0%」。
# 带上几个公共 tracker 后，只要发布者的 seedbox 有向其中任一 announce，就能连上。
TRACKERS = (
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
    "http://tracker.openbittorrent.com:80/announce",
)


def _with_trackers(magnet):
    """给没有 tracker 的磁力补上公共 tracker；已有 tr= 的原样返回。"""
    mg = (magnet or "").strip()
    if not mg or "tr=" in mg:
        return mg
    q = "&".join("tr=" + urllib.parse.quote(t, safe="") for t in TRACKERS)
    return mg + ("&" if "?" in mg else "?") + q


# ---------------- 分享链接 / 集号 ----------------
_SHARE_RE = re.compile(r"https?://(?:www\.)?(?:115|115cdn|anxia)\.com/s/([A-Za-z0-9]+)")
_PWD_RE = re.compile(r"[?&#](?:password|pwd|code|passcode)=([A-Za-z0-9]{2,12})", re.I)
_PWD_TXT = re.compile(r"(?:访问码|提取码|密码|密碼)\s*[：:=]?\s*([A-Za-z0-9]{4,12})")

# 这些数字几乎一定是分辨率/编码，不是集号
_EP_SKIP = {264, 265, 480, 576, 720, 1080, 1280, 1920, 2160, 3840, 4096}


def parse_share(url):
    """从 115 分享链接里取 (share_code, 提取码)。"""
    s = str(url or "").strip()
    m = _SHARE_RE.search(s)
    if not m:
        return "", ""
    pwd = ""
    pm = _PWD_RE.search(s) or _PWD_TXT.search(s)
    if pm:
        pwd = pm.group(1)
    return m.group(1), pwd


def _ok_ep(n, name=""):
    try:
        n = int(n)
    except (TypeError, ValueError):
        return 0
    if not (0 < n < 1900) or n in _EP_SKIP:
        return 0
    if re.search(r"(?<![0-9])%dp" % n, str(name or ""), re.I):
        return 0
    return n


def guess_ep(name):
    """从文件名/目录名猜集号：'第9集' / S01E09 / EP09 / '09.mkv' / '片名.09.mkv'。

    网盘分享里的单集文件经常只叫 '01.mkv'，集号只能从行首数字取。
    """
    s = str(name or "").strip()
    if not s:
        return 0
    m = re.search(r"第\s*(\d{1,4})\s*[集话話期回]", s)
    if m:
        return _ok_ep(m.group(1), s)
    m = re.search(r"[Ss](\d{1,2})\s*[._\-\s]?[Ee][Pp]?\s*(\d{1,4})", s)
    if m:
        return _ok_ep(m.group(2), s)
    m = re.search(r"更新至\s*(\d{1,4})", s)
    if m:
        return _ok_ep(m.group(1), s)
    m = re.search(r"(?<![A-Za-z0-9])(?:[Ee][Pp]|[Ee])\s*[._\-\s]?\s*(\d{1,4})(?![0-9])", s)
    if m:
        return _ok_ep(m.group(1), s)
    m = re.search(r"(?:^|[.\s_\-\[【(（])(\d{1,4})(?:[vV]\d)?(?=[.\s_\-\[\]】)）]|$)", s)
    if m:
        return _ok_ep(m.group(1), s)
    return 0


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


# ---------------- 全局限流 ----------------
# 115 对短时间内的密集请求很敏感：批量整理时「列目录 → 改名 → 再列目录」几秒内能打十几个请求，
# 容易触发风控（表现为返回「请求过于频繁」或直接 HTTP 429），严重时会把账号临时关小黑屋。
# 这里给所有 115 请求加一道全局闸门：保证最小间隔 + 命中风控后整站冷却，任何线程都绕不过去。
_MIN_GAP = max(0.0, float(os.environ.get("PAN_MIN_GAP") or 0.5))
_RL_COOLDOWN = max(10, int(os.environ.get("PAN_RL_COOLDOWN") or 40))

_gate_lock = threading.Lock()
_last_call = 0.0
_rl = {"until": 0.0, "logged": 0.0, "hits": 0}

# 路径 → (目录 id, 时间)：一次扫描里同一目录会被反复查，缓存能省掉大量请求
_CID_CACHE = {}

# 115 风控的几种常见措辞（不同接口说法不一样）
_RATE_HINTS = ("请求过于频繁", "操作过于频繁", "访问过于频繁", "过于频繁", "稍后再试",
               "频率限制", "访问频率", "too many requests", "rate limit", "rate_limit")
# 会被 115 当成「忙」的 HTTP 状态
_RATE_CODES = (429, 503)


def rate_limited():
    """当前是否处于冷却期（供上层决定「这轮先别推了」）。"""
    return time.time() < _rl["until"]


def _note_rate_limit():
    now = time.time()
    _rl["until"] = now + _RL_COOLDOWN
    _rl["hits"] = int(_rl.get("hits") or 0) + 1
    # 冷却日志 5 分钟内最多一条，避免刷屏
    if now - float(_rl.get("logged") or 0) > 300:
        _rl["logged"] = now
        try:
            store.log(f"115 提示操作过于频繁，已自动冷却 {_RL_COOLDOWN} 秒后继续", "warn")
        except Exception:
            pass


def _gate():
    """所有 115 请求的入口闸门：保证最小间隔；冷却期内直接拒绝，不再打 115。"""
    global _last_call
    with _gate_lock:
        now = time.time()
        if now < _rl["until"]:
            raise PanError("RATE_LIMIT")
        gap = _last_call + _MIN_GAP - now
        if gap > 0:
            time.sleep(gap + random.uniform(0, 0.25))
        _last_call = time.time()


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
        _gate()
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
                # webapi 下面有几个接口（storage_info 就是）不声明 Content-Encoding，
                # 却直接吐 gzip 字节；不按魔数兜底解一次，JSON 永远解析不了。
                # 用 decompressobj：尾部带垃圾字节时 gzip.decompress 会整段失败。
                if raw[:2] == b"\x1f\x8b":
                    try:
                        raw = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(raw)
                    except Exception:
                        pass
                txt = raw.decode("utf-8", "replace")
                setc = r.headers.get_all("Set-Cookie") or []
                if self._looks_throttled(r.status, txt):
                    _note_rate_limit()
                    raise PanError("RATE_LIMIT")
                return r.status, txt, setc
        except urllib.error.HTTPError as e:
            try:
                txt = e.read().decode("utf-8", "replace")
            except Exception:
                txt = ""
            if self._looks_throttled(e.code, txt):
                _note_rate_limit()
                raise PanError("RATE_LIMIT")
            return e.code, txt, []
        except PanError:
            raise
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
    def _looks_throttled(st, txt):
        """判断这次响应是不是 115 的风控拦截。

        115 的表达方式不统一：有的返回 HTTP 429，有的 200 + 「请求过于频繁」，
        还有的给 errno/code 996。宁可按误判处理（冷却几十秒），也别继续硬打。
        """
        if st in _RATE_CODES:
            return True
        t = (txt or "")[:800]
        if any(h in t for h in _RATE_HINTS):
            return True
        if re.search(r'"?(?:errno|code|errno)"?\s*[:=]\s*"?(?:996|429)\b', t):
            return True
        return False

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
        # 115 对带 dn=/长文件名的磁力容易报 decode fail，先用纯 infohash 提交。
        # 但纯 hash 没有任何 tracker，只能靠 DHT 找源——站点自己压的种在 DHT 里
        # 常常一个 peer 都没有，任务就一直 0%（2026-11-19 全站卡死就是这么来的）。
        # 所以提交时把公共 tracker 一起带上：有 tracker 的源能立刻连上，没源的
        # 也不比原来更差。
        m = re.search(r"btih:([0-9a-fA-F]{40}|[A-Za-z2-7]{32})", magnet or "")
        variants = []
        if m:
            variants.append(_with_trackers("magnet:?xt=urn:btih:%s" % m.group(1)))
        if magnet and magnet not in variants:
            variants.append(_with_trackers(magnet))
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
                    if last == "RATE_LIMIT":
                        # 已被 115 拦下：别再换接口重试，越试越冷
                        return {"ok": False, "rate_limited": True,
                                "msg": f"115 提示操作过于频繁，已自动冷却 {_RL_COOLDOWN} 秒"}
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

    # ---------- 分享转存（网盘兜底） ----------
    # 磁力全死的时候，站点的网盘资源是唯一还能用的源。115 的转存是
    # **服务端复制**：不经过本机、不占下载带宽，几秒就能把几十 G 挪进网盘。
    # 关键接口两个：
    #   GET  /share/snap      看分享里有什么（顺便验活/验提取码）
    #   POST /share/receive   把选中的文件/目录转存到自己的 cid
    def share_snap(self, share_code, pwd="", cid="0", offset=0, limit=100):
        q = urllib.parse.urlencode({
            "share_code": share_code, "receive_code": pwd or "",
            "cid": str(cid or "0"), "offset": offset, "limit": limit,
            "show_dir": 1,
        })
        st, txt, _ = self._req(f"https://webapi.115.com/share/snap?{q}", timeout=25,
                               referer="https://115.com/")
        j = self._json(txt)
        if not isinstance(j, dict):
            return {"ok": False, "msg": f"返回格式异常 HTTP {st}"}
        if not self._ok(j):
            errno = j.get("errno")
            msg = j.get("error") or j.get("message") or txt[:120] or "分享不可用"
            return {"ok": False, "msg": str(msg)[:120], "errno": errno,
                    "dead": str(errno) in ("4100026", "4100027"),
                    "need_pwd": str(errno) == "4100012",
                    "bad_pwd": str(errno) == "4100008"}
        d = j.get("data") or {}
        rows = []
        for it in (d.get("list") or []):
            if not isinstance(it, dict):
                continue
            fc = str(it.get("fc") or "0")
            fid = str(it.get("fid") or "")
            cid2 = str(it.get("cid") or "")
            rows.append({
                "name": str(it.get("n") or it.get("name") or ""),
                "id": fid or cid2,
                "fid": fid, "cid": cid2, "is_dir": fc == "0",
                "size": int(it.get("s") or 0),
            })
        si = d.get("shareinfo") or {}
        return {"ok": True, "list": rows, "count": int(d.get("count") or len(rows)),
                "size": int(si.get("file_size") or 0),
                "title": str(si.get("share_title") or ""),
                "state": si.get("share_state")}

    def share_ls(self, share_code, pwd="", cid="0", max_n=300):
        """把一个分享目录完整列出来（分页），返回所有条目。"""
        out, off, total = [], 0, None
        while off < int(max_n):
            r = self.share_snap(share_code, pwd, cid=cid, offset=off, limit=100)
            if not r.get("ok"):
                return r if not out else {"ok": True, "list": out}
            batch = r.get("list") or []
            out.extend(batch)
            total = int(r.get("count") or 0)
            if not batch or len(out) >= total:
                break
            off += len(batch)
        return {"ok": True, "list": out[:int(max_n)], "count": total or len(out)}

    def share_receive(self, share_code, pwd="", file_ids=None, cid="0"):
        """转存选中的文件/目录到 cid。115 要求 file_id 用逗号拼。"""
        ids = [str(x) for x in (file_ids or []) if x]
        if not ids:
            return {"ok": False, "msg": "没有要转存的文件"}
        data = {"share_code": share_code, "receive_code": pwd or "",
                "file_id": ",".join(ids), "cid": str(cid or "0")}
        last = ""
        for i in range(2):
            try:
                st, txt, _ = self._req("https://webapi.115.com/share/receive", data=data,
                                       method="POST", timeout=35, referer="https://115.com/")
            except PanError as e:
                last = str(e)
                if last == "RATE_LIMIT":
                    return {"ok": False, "rate_limited": True,
                            "msg": f"115 提示操作过于频繁，已自动冷却 {_RL_COOLDOWN} 秒"}
                time.sleep(1.2)
                continue
            j = self._json(txt)
            if self._ok(j):
                d = j.get("data") or {}
                return {"ok": True, "n": len(ids),
                        "files": int(d.get("recv_file_count") or 0),
                        "folders": int(d.get("recv_folder_count") or 0),
                        "size": int(d.get("receive_size") or 0),
                        "title": str(d.get("receive_title") or "")}
            last = j.get("error") or j.get("message") or txt[:150] or f"HTTP {st}"
            if "频繁" in str(last):
                return {"ok": False, "msg": last[:120], "rate_limited": True}
            time.sleep(1.2)
        return {"ok": False, "msg": last or "转存失败"}

    def share_pull(self, url, pwd="", cid="0", want_eps=None, need_eps=None,
                   allow_pack=False):
        """把一条分享转存到 cid。

        want_eps/need_eps：
          · 给了（追更/补集）→ 尽量只转这几集的文件，避免整季重下一遍；
            分享里只有整包、且 allow_pack=True 时才整包转。
          · 不给（电影/整季）→ 转存分享的顶层条目。
        返回 {ok, msg, eps, pack, n, size, code, title}
        """
        code, pwd2 = parse_share(url)
        pwd = pwd or pwd2
        if not code:
            return {"ok": False, "msg": "不是 115 分享链接"}
        want = sorted({int(x) for x in (want_eps or need_eps or []) if int(x) > 0})
        top = self.share_ls(code, pwd)
        if not top.get("ok"):
            return top
        items = top.get("list") or []
        if not items:
            return {"ok": False, "msg": "分享里是空的", "code": code}

        # 顶层文件（有些分享直接就是单集文件）
        eps_map = {}
        for it in items:
            if it["is_dir"]:
                continue
            e = guess_ep(it["name"])
            if e:
                eps_map.setdefault(e, it["id"])

        # 顶层目录里通常是 01.mkv / 第09集.mp4 —— 只有进目录才知道有哪些集
        dirs = [it for it in items if it["is_dir"]]
        child = []          # [(ep, fid, dir_id)]
        for d in dirs[:4]:
            sub = self.share_ls(code, pwd, cid=d["cid"] or d["id"], max_n=300)
            for ch in (sub.get("list") or []):
                if ch["is_dir"]:
                    continue
                e = guess_ep(ch["name"])
                if e:
                    child.append((e, ch["id"], d["id"]))
        all_eps = sorted(set(eps_map) | {e for e, _, _ in child})

        picked, got = [], []
        if want:
            seen = set()
            for e, fid, _ in child:
                if e in want and e not in seen:
                    picked.append(fid)
                    seen.add(e)
                    got.append(e)
            for e in want:
                if e in eps_map and e not in seen:
                    picked.append(eps_map[e])
                    seen.add(e)
                    got.append(e)
            if not picked:
                # 分享里能认出集号、但没有我们要的集 → 整包转了也没用，别浪费空间。
                # 只有「结构认不出集号」（all_eps 为空）时才允许整包赌一把。
                if not allow_pack or all_eps:
                    return {"ok": False, "code": code, "msg": "分享里没有这几集的单集文件",
                            "need_pack": bool(dirs) and not all_eps, "eps": [],
                            "all_eps": all_eps}
                # 整包：转顶层目录/文件，转完之后这几集自然就在里面
                picked = [it["id"] for it in items]
                pack = True
            else:
                pack = False
        else:
            picked = [it["id"] for it in items]
            pack = bool(dirs) and not eps_map

        if not picked:
            return {"ok": False, "msg": "分享里没有可转存的文件", "code": code}

        r = self.share_receive(code, pwd, picked, cid=cid)
        if not r.get("ok"):
            r.update(code=code)
            return r
        # 整包转存：落地的是整个目录，集号以目录里的实际文件为准
        out_eps = sorted(set(got)) if not pack else (all_eps or sorted(set(got)))
        return {"ok": True, "code": code, "eps": out_eps, "all_eps": all_eps,
                "pack": pack, "n": len(picked), "size": int(r.get("size") or 0),
                "title": r.get("title") or "", "files": r.get("files"),
                "folders": r.get("folders")}

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
        """离线下载配额。

        注意这是「离线临时空间」，不是网盘存储：加磁力后先下到离线空间，
        下完再转存进网盘。这个数一般是 1.5T 级别，跟网盘那 100T 不是一回事。
        返回的 quota 是**剩余**，不是已用。
        """
        st, txt, _ = self._req(f"{DL_HOST}/?ac=get_quota_info", timeout=15, referer="https://115.com/?ct=lixian")
        j = self._json(txt)
        d = j.get("data")
        if not isinstance(d, dict):
            d = {"quota": j.get("quota"), "total": j.get("total")}
        return {"ok": self._ok(j), "data": d, "raw": j}

    def space(self):
        """网盘存储空间（字节）：total / used / free。

        跟 quota() 完全是两个概念：这里才是「116T 用了 31T」那种总容量。
        接口是 /user/space：字段叫 all_total / all_use / all_remain，
        115 给的是 size_format（不是 size_alias）。主接口挂了就退到
        /files/index_info 里的 space_info，结构一样。
        自己读原始 body、按 gzip 魔数解，不依赖 _req 那层（它看响应头判断）。
        """
        def num(v):
            try:
                return int(float(v))
            except (TypeError, ValueError):
                return 0

        def pick(d, *keys):
            for k in keys:
                v = d.get(k)
                if isinstance(v, dict) and v.get("size") is not None:
                    return num(v.get("size")), \
                        (v.get("size_format") or v.get("size_alias") or "")
            return 0, ""

        last = {}
        for url, bucket in (("https://webapi.115.com/user/space", ""),
                            ("https://webapi.115.com/files/index_info", "space_info")):
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9",
                # 明确要求不压缩；有的节点会忽略，所以下面仍按魔数兜底
                "Accept-Encoding": "identity",
                "Referer": "https://115.com/",
            })
            _gate()
            try:
                with self.opener.open(req, timeout=15) as r:
                    raw = r.read()
                    enc = (r.headers.get("Content-Encoding") or "").lower()
            except Exception as e:
                last = {"err": f"{type(e).__name__}: {e}"[:120]}
                continue
            if raw[:2] == b"\x1f\x8b":
                try:
                    raw = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(raw)
                except Exception as e:
                    last = {"err": "gzip 解压失败: " + str(e)[:80], "head": raw[:6].hex()}
                    continue
            elif enc == "deflate":
                try:
                    raw = zlib.decompressobj().decompress(raw)
                except Exception:
                    pass
            txt = raw.decode("utf-8", "replace")
            j = self._json(txt) or {}
            d = j.get("data") if isinstance(j.get("data"), dict) else j
            if bucket and isinstance(d, dict) and isinstance(d.get(bucket), dict):
                d = d[bucket]
            if not isinstance(d, dict):
                last = {"err": "返回格式不认识", "raw": txt[:120]}
                continue
            total, total_txt = pick(d, "all_total")
            used, used_txt = pick(d, "all_use", "all_used")
            free, free_txt = pick(d, "all_remain", "all_free")
            if total:
                # 115 只保证给其中两个，缺的那个自己减
                if not free and used:
                    free, free_txt = max(0, total - used), ""
                if not used and free:
                    used, used_txt = max(0, total - free), ""
                return {"ok": True, "total": total, "total_txt": total_txt,
                        "used": used, "used_txt": used_txt,
                        "free": free, "free_txt": free_txt}
            # 保留诊断信息：字段名变了能一眼看出来，不用再猜
            last = {"err": "没拿到总容量", "keys": sorted(d.keys())[:14],
                    "raw": txt[:120]}
        return {"ok": False, **last}

    def probe(self, url):
        """排障用：原样 GET 一个 115 接口，返回状态码和响应开头。

        115 的接口路径时不时改（storage_info 就这么 404 的），有了它可以直接
        试候选地址，不用改一次代码重启一次容器。只允许 115 自己的域名。
        """
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Encoding": "identity",
            "Referer": "https://115.com/",
        })
        _gate()
        try:
            with self.opener.open(req, timeout=15) as r:
                raw = r.read()
                code = r.status
                ctype = r.headers.get("Content-Type") or ""
        except urllib.error.HTTPError as e:
            return {"ok": False, "code": e.code,
                    "body": e.read()[:200].decode("utf-8", "replace")}
        except Exception as e:
            return {"ok": False, "err": f"{type(e).__name__}: {e}"[:160]}
        if raw[:2] == b"\x1f\x8b":
            try:
                raw = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(raw)
            except Exception:
                pass
        txt = raw.decode("utf-8", "replace")
        return {"ok": True, "code": code, "ctype": ctype, "len": len(raw),
                "body": txt[:400]}

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

    def mkdir_at(self, pid, name):
        """在指定目录下建子目录（幂等），返回 cid。

        网盘转存必须落进「片名目录」而不是离线根目录：单集文件名常常只有
        01.mkv，全丢进 /离线 会乱成一锅粥，后面没法按片找。
        """
        pid = str(pid or "0")
        cname = str(name or "").strip().replace("/", " ")[:80]
        if not cname:
            return ""
        try:
            r = self.files(cid=pid, limit=200)
            for it in (r.get("items") or []):
                if it.get("is_dir") and str(it.get("name") or "").strip() == cname:
                    return str(it.get("cid") or "")
        except Exception:
            pass
        try:
            st, txt, _ = self._req("https://webapi.115.com/files/add",
                                   data={"pid": pid, "cname": cname},
                                   method="POST", timeout=25, referer="https://115.com/")
        except PanError:
            return ""
        j = self._json(txt)
        cid = ""
        if isinstance(j, dict):
            cid = j.get("cid") or (j.get("data") or {}).get("cid") or ""
        return str(cid or "")

    def mkdir(self, path):
        """在 115 上建目录，父级不存在就逐级补齐。

        幂等：已存在的层级直接复用，全部已存在时返回现有 cid，不会报错。
        用来给「电视剧/动漫」建各自的落地目录（/影视/电视剧/离线 之类）——
        目录选择器只能挑已有目录，缺一级就没法设置。
        """
        p = (path or "").strip().strip("/")
        if not p:
            return {"ok": False, "msg": "缺少目录路径"}
        cur_cid, cur_path, created = "0", "", []
        for seg in [s for s in p.split("/") if s]:
            cur_path += "/" + seg
            cid = self.cid_of(cur_path, ttl=0)
            if cid:
                cur_cid = cid
                continue
            try:
                st, txt, _ = self._req("https://webapi.115.com/files/add",
                                       data={"pid": cur_cid, "cname": seg},
                                       method="POST", timeout=25,
                                       referer="https://115.com/")
            except PanError as e:
                return {"ok": False, "msg": str(e), "path": cur_path, "created": created}
            j = self._json(txt)
            new_cid = ""
            if isinstance(j, dict):
                new_cid = j.get("cid") or (j.get("data") or {}).get("cid") or ""
            if not new_cid:
                # 同名目录已存在时 115 不返回 cid，回查一次当作成功
                new_cid = self.cid_of(cur_path, ttl=0) or ""
            if not new_cid:
                msg = ""
                if isinstance(j, dict):
                    msg = j.get("error") or j.get("message") or ""
                return {"ok": False, "msg": msg or txt[:120] or "创建目录失败",
                        "path": cur_path, "created": created}
            cur_cid = str(new_cid)
            _CID_CACHE[cur_path] = (cur_cid, time.time())
            created.append(cur_path)
        return {"ok": True, "cid": cur_cid, "path": "/" + p, "created": created}

    def rename(self, path, new_name):
        """给 115 上的文件或目录改名。

        先列父目录拿到目标的 fid（目录用 cid），再调 batch_rename。
        拿不到目标或接口不认就返回 ok=False，调用方退回原名继续，不影响主流程。
        """
        p = (path or "").strip().rstrip("/")
        new_name = (new_name or "").strip()
        if not p or not new_name:
            return {"ok": False, "msg": "缺少路径或新名字"}
        parent, _, old = p.rpartition("/")
        parent = parent or "/"
        if not old:
            return {"ok": False, "msg": "路径不合法"}
        if new_name == old:
            return {"ok": True, "path": p, "unchanged": True}
        ch = self.dir_children(parent, limit=500)
        if not ch.get("ok"):
            return {"ok": False, "msg": ch.get("msg") or "列父目录失败"}
        fid = ""
        for it in ch["items"]:
            if it["name"] == old:
                fid = it["fid"] or it["cid"]
                break
        if not fid:
            return {"ok": False, "msg": "父目录里找不到该条目"}
        pid = str(ch.get("cid") or "0")
        tries = [
            ("https://webapi.115.com/files/batch_rename",
             {f"files_new_name[{fid}]": new_name, "pid": pid}),
            ("https://webapi.115.com/files/batch_rename",
             {f"files_new_name[{fid}]": new_name}),
            ("https://webapi.115.com/files/rename",
             {"fid": fid, "file_name": new_name, "pid": pid}),
        ]
        last = ""
        for url, data in tries:
            try:
                st, txt, _ = self._req(url, data=data, method="POST", timeout=25,
                                       referer=f"https://115.com/?cid={pid}&offset=0&mode=wangpan")
            except PanError as e:
                last = str(e)
                continue
            j = self._json(txt)
            if self._ok(j):
                return {"ok": True, "path": parent.rstrip("/") + "/" + new_name, "fid": fid}
            last = j.get("error") or j.get("message") or (txt[:150] if txt else "改名失败")
        return {"ok": False, "msg": last or "改名失败"}

    def del_task(self, info_hash, del_files=True):
        """删掉离线任务记录。

        del_files=True 会把下载下来的源文件一起清掉（离线空间里的那份）；
        False 只删任务记录，网盘里的文件原样保留 —— 清理「已完成」的队列
        占用时必须用 False，否则会把刚入库的片子删了。
        """
        h = str(info_hash or "").lower()
        if not h:
            return {"ok": False, "msg": "缺少 infohash"}
        flag = "1" if del_files else "0"
        for endpoint, data in (
            (f"{DL_HOST}/lixianssp/", {"ac": "task_del", "hash[0]": h, "del_source_file": flag}),
            (f"{DL_HOST}/", {"ac": "task_del", "hash[0]": h, "del_source_file": flag}),
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

    def del_tasks(self, hashes, del_files=False):
        """批量删离线任务记录。只删记录（del_files=False）不会碰网盘文件。"""
        hs = [str(h).lower() for h in (hashes or []) if h]
        if not hs:
            return {"ok": False, "msg": "缺少 infohash"}
        flag = "1" if del_files else "0"
        data = {"ac": "task_del", "del_source_file": flag}
        for i, h in enumerate(hs):
            data[f"hash[{i}]"] = h
        for endpoint in (f"{DL_HOST}/lixianssp/", f"{DL_HOST}/"):
            try:
                st, txt, _ = self._req(endpoint, data=data, method="POST", timeout=25,
                                       referer="https://115.com/?ct=lixian")
            except PanError:
                continue
            j = self._json(txt)
            if self._ok(j):
                return {"ok": True, "n": len(hs), "raw": j}
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

    def cid_of(self, path, ttl=120):
        """把 /影视/电影/离线 这样的路径换成 115 的目录 id。不存在返回 None。

        带 2 分钟缓存：一次扫描里同一个目录会被反复列（扫描 → 改名 → 再列），
        每次都查一遍既慢又白送请求，属于最容易触发风控的行为之一。
        """
        p = (path or "").strip().rstrip("/")
        if p in ("", "/"):
            return "0"
        hit = _CID_CACHE.get(p)
        if hit and time.time() - hit[1] < ttl:
            return hit[0]
        url = "https://webapi.115.com/files/getid?path=" + urllib.parse.quote(p)
        st, txt, _ = self._req(url, timeout=20, referer="https://115.com/")
        j = self._json(txt)
        if not isinstance(j, dict):
            return None
        cid = j.get("id") or j.get("cid")
        if not cid and isinstance(j.get("data"), dict):
            cid = j["data"].get("id") or j["data"].get("cid")
        if cid:
            _CID_CACHE[p] = (str(cid), time.time())
            return str(cid)
        return None

    def dir_children(self, path, limit=300):
        """按路径列子项，保留 age/创建时间等整理需要用的原始字段。

        115 的目录列表里：目录把自身 id 放在 cid 且没有 fid；文件才有 fid。
        """
        cid = self.cid_of(path)
        if not cid:
            return {"ok": False, "msg": "目录不存在", "cid": None, "items": []}
        q = urllib.parse.urlencode({
            "aid": 1, "cid": cid, "o": "user_ptime", "asc": 0, "offset": 0,
            "show_dir": 1, "limit": int(limit), "snap": 0, "natsort": 1,
            "record_open_time": 1, "source": "", "format": "json",
        })
        st, txt, _ = self._req(f"https://webapi.115.com/files?{q}", timeout=30,
                               referer="https://115.com/")
        j = self._json(txt)
        rows = j.get("data")
        if not isinstance(rows, list):
            rows = (rows or {}).get("list") if isinstance(rows, dict) else None
            rows = rows or j.get("list") or []
        out = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            name = r.get("n") or r.get("name") or ""
            fid = str(r.get("fid") or "")
            is_dir = (not fid) and bool(str(r.get("cid") or ""))
            if str(r.get("is_dir")).lower() in ("1", "true"):
                is_dir = True
            t = int(r.get("te") or r.get("t") or r.get("user_ptime") or 0)
            out.append({
                "name": name, "fid": fid, "cid": str(r.get("cid") or ""),
                "is_dir": bool(is_dir), "size": int(r.get("s") or 0),
                "t": t, "pc": r.get("pc") or "",
            })
        return {"ok": True, "cid": cid, "items": out}

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
