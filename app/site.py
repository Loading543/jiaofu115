"""教父.com (xn--wcv59z.com) 站点客户端。

- 自动通过 /res/pow 工作量证明（JS 挑战）
- 账号密码登录 / 点选验证码
- 栏目列表、搜索、详情、磁力解析
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

# 封面：站点前端 _fun.img(dir,id) + '/' + _BT.imgc.xxx + '.webp'
#   _BT.imghost = https://s.tutu.pm → /img/{栏目}/{id}/384.webp
POSTER_HOST = "https://s.tutu.pm"


def poster_url(dirname, iid, size=256):
    return f"{POSTER_HOST}/img/{dirname}/{iid}/{size}.webp"

SIZE_UNITS = {
    "B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4,
    "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4,
}


def parse_size(text):
    """'4.31 GB' -> 4627298816"""
    if text is None:
        return 0
    if isinstance(text, (int, float)):
        return int(text)
    s = str(text).strip().replace(",", "")
    m = re.search(r"([\d.]+)\s*([KMGT]?B?)\b", s, re.I)
    if not m:
        m2 = re.search(r"(\d+)", s)
        return int(m2.group(1)) if m2 else 0
    try:
        num = float(m.group(1))
    except ValueError:
        return 0
    unit = (m.group(2) or "B").upper()
    return int(num * SIZE_UNITS.get(unit, 1))


def human_size(n):
    n = int(n or 0)
    if n <= 0:
        return ""
    for u in ("TB", "GB", "MB", "KB"):
        if n >= SIZE_UNITS[u]:
            return f"{n / SIZE_UNITS[u]:.2f} {u}"
    return f"{n} B"


BTIH_RE = re.compile(r"btih:([0-9a-fA-F]{40}|[A-Za-z2-7]{32})")

# ---------- 资源质量评分（洗版用）----------
# 排序优先级：分辨率 > 片源 > 编码/HDR 加成；同分时才比体积。
# 评分做成整数便于入库比较：res*10000 + src*1000 + bonus*10
_Q_RES = [
    (r"2160p|2160|4k|uhd", 5, "2160p"),
    (r"1080i", 4, "1080i"),
    (r"1080p|1080", 4, "1080p"),
    (r"720p|720", 3, "720p"),
    (r"576p|480p|480|dvd", 2, "480p"),
]
_Q_SRC = [
    (r"remux", 5, "REMUX"),
    (r"blu-?ray|bdrip|brrip|bd-?remux", 4, "BluRay"),
    (r"web-?dl|webrip|web-?rip|hdtv.?web", 3, "WEB-DL"),
    (r"web", 3, "WEB"),
    (r"hdtv|hdrip", 2, "HDTV"),
    (r"dvdrip|dvd", 1, "DVDRip"),
    (r"\bcam\b|\bts\b|\btc\b|枪版|抢先", 0, "枪版"),
]


def quality_of(label):
    """给一条磁力打质量分。返回 {q, res, src, tags}，q 越大越好。"""
    s = (label or "").lower()
    res, res_txt = 0, "未知"
    for pat, val, name in _Q_RES:
        if re.search(pat, s):
            res, res_txt = val, name
            break
    if not res:
        res = 1
    src, src_txt = -1, "未知"
    for pat, val, name in _Q_SRC:
        if re.search(pat, s):
            src, src_txt = val, name
            break
    if src < 0:
        src, src_txt = 2, "未知"
    tags, bonus = [], 0
    if re.search(r"hevc|h\.?265|x265", s):
        tags.append("H265"); bonus += 2
    elif re.search(r"av1", s):
        tags.append("AV1"); bonus += 2
    elif re.search(r"h\.?264|x264|avc", s):
        tags.append("H264")
    if re.search(r"dolby.?vision|\bdv\b|hdr10\+?|hdr", s):
        tags.append("HDR"); bonus += 2
    if re.search(r"atmos|truehd|dts-?hd|dts.?x|flac", s):
        tags.append("环绕声"); bonus += 1
    if re.search(r"10 ?bit", s):
        tags.append("10bit"); bonus += 1
    if re.search(r"remux|原盘", s):
        tags.append("原盘")
    return {"q": res * 10000 + src * 1000 + bonus * 10,
            "res": res_txt, "src": src_txt, "tags": tags,
            "text": f"{res_txt} {src_txt}" + (" " + "/".join(tags[:2]) if tags else "")}


def pick_best_quality(magnets):
    """按质量分选最优，同分取体积大的。"""
    best, best_key = None, None
    for m in magnets or []:
        q = quality_of(m.get("label") or m.get("url") or "")
        key = (q["q"], int(m.get("size_bytes") or 0))
        if best_key is None or key > best_key:
            best, best_key = m, key
            best["quality"] = q
    return best


# 登录态缓存，避免每次 /api/status 都去解一次 PoW
_login_cache = {"ts": 0.0, "ok": False, "msg": ""}


def invalidate_login_cache():
    _login_cache["ts"] = 0.0


def btih_of(magnet):
    m = BTIH_RE.search(magnet or "")
    return m.group(1).lower() if m else ""


class SiteError(Exception):
    pass


class SiteClient:
    def __init__(self, base=None):
        s = store.settings()
        self.base = (base or s["site"]["base"]).rstrip("/")
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar),
            urllib.request.HTTPRedirectHandler(),
        )
        for k, v in (store.site_cookies() or {}).items():
            self._set_cookie(k, v)
        self._pow_ok = bool(self.cookie_dict().get("browser_verified") or self.cookie_dict().get("browser_pow"))
        # 站点约 40 次请求/窗口就限流，翻页默认 4s/页，失败后自适应加大
        self.page_delay = 2.0

    # ---------- cookies ----------
    def _set_cookie(self, name, value, domain=".xn--wcv59z.com"):
        c = http.cookiejar.Cookie(
            version=0, name=name, value=value, port=None, port_specified=False,
            domain=domain, domain_specified=True, domain_initial_dot=True,
            path="/", path_specified=True, secure=False, expires=None,
            discard=False, comment=None, comment_url=None, rest={}, rfc2109=False)
        self.jar.set_cookie(c)

    def cookie_dict(self):
        return {c.name: c.value for c in self.jar}

    def save_cookies(self):
        store.set_site_cookies(self.cookie_dict())

    def logged_in(self):
        return bool(self.cookie_dict().get("uid") or self.cookie_dict().get("auth"))

    def rotate_session(self, drop_pow=True):
        """被限流后换一个干净会话：丢掉 PoW cookie，让站点重新下发挑战。"""
        cookies = dict(store.site_cookies() or {})
        if drop_pow:
            cookies.pop("browser_pow", None)
            cookies.pop("browser_verified", None)
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar),
            urllib.request.HTTPRedirectHandler(),
        )
        for k, v in cookies.items():
            self._set_cookie(k, v)
        self._pow_ok = False

    # ---------- http ----------
    def _raw(self, path, data=None, timeout=30, referer=None):
        url = path if path.startswith("http") else self.base + path
        try:
            url.encode("ascii")
        except UnicodeEncodeError:
            url = urllib.parse.quote(url, safe=":/?#[]@!$&'()*+,;=%")
        headers = {
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Accept-Encoding": "gzip, deflate",
            "Referer": referer or (self.base + "/"),
        }
        body = None
        if data is not None:
            body = urllib.parse.urlencode(data, doseq=True).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            headers["X-Requested-With"] = "XMLHttpRequest"
        req = urllib.request.Request(url, data=body, headers=headers)
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
                return r.status, raw.decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            raw = e.read()
            enc = (e.headers.get("Content-Encoding") or "").lower()
            if enc == "gzip":
                try:
                    raw = gzip.decompress(raw)
                except Exception:
                    pass
            return e.code, raw.decode("utf-8", "replace")
        except Exception as e:
            raise SiteError(f"请求失败: {e}")

    def _need_pow(self, html):
        return ("浏览器安全验证" in html) or ("/res/pow" in html and "powSolve" in html)

    def solve_pow(self):
        st, txt = self._raw("/res/pow")
        if st != 200:
            raise SiteError(f"获取验证挑战失败 HTTP {st}")
        try:
            j = json.loads(txt)
        except Exception:
            raise SiteError("验证挑战返回非 JSON")
        n = int(j["N"], 16)
        y = int(j["x"], 16)
        t = int(j["t"])
        for _ in range(t):
            y = (y * y) % n
        st2, txt2 = self._raw("/res/pow", data={"y": format(y, "x")})
        if "success" not in txt2:
            raise SiteError(f"验证提交失败: {txt2[:120]}")
        self._pow_ok = True
        self.save_cookies()
        return True

    def ensure_pow(self, force=False):
        """POST 类接口（登录/验证码）不会触发 GET 的校验流程，需要先手动过一次。"""
        if self._pow_ok and not force:
            return True
        st, html = self._raw("/")
        if st == 200 and self._need_pow(html):
            self.solve_pow()
        else:
            self._pow_ok = True
            self.save_cookies()
        return True

    def get(self, path, data=None, ensure=True, timeout=30, referer=None):
        st, html = self._raw(path, data=data, timeout=timeout, referer=referer)
        if ensure and st == 200 and self._need_pow(html):
            self.solve_pow()
            st, html = self._raw(path, data=data, timeout=timeout, referer=referer)
        return st, html

    def get_checked(self, path, **kw):
        st, html = self.get(path, **kw)
        if "未登录" in html and len(html) < 3000:
            raise SiteError("NEED_LOGIN")
        return st, html

    # ---------- login ----------
    def captcha(self):
        """获取点选验证码。返回 {img, chars:[b64], w, h}"""
        self.ensure_pow()
        st, txt = self.get("/res/captcha/2", data={"webp": ""}, ensure=False)
        try:
            j = json.loads(txt)
        except Exception:
            raise SiteError(f"验证码获取失败: {txt[:120]}")
        return {
            "img": "data:image/%s;base64,%s" % (j.get("type", "jpeg"), j.get("img", "")),
            "chars": j.get("text_img") or [],
            "w": 350, "h": 200,
        }

    def captcha_check(self, points, w=350, h=200):
        self.ensure_pow()
        info = "%s;%s;%s" % ("-".join(points), w, h)
        st, txt = self.get("/res/captcha/2", data={"do": "check", "info": info}, ensure=False)
        try:
            return json.loads(txt).get("code") == 200
        except Exception:
            return False

    def login(self, username, password, code="", remember=True):
        self.ensure_pow()
        payload = {
            "username": username,
            "password": password,
            "cookietime": 10506240 if remember else "",
            "code": code,
            "siteid": 1,
            "dosubmit": 1,
        }
        st, txt = self.get("/user/login", data=payload, ensure=False)
        try:
            j = json.loads(txt)
        except Exception:
            if "未登录" not in txt and len(txt) > 3000:
                j = {"code": 200}
            else:
                raise SiteError(f"登录返回异常: {txt[:150]}")
        if j.get("code") == 200:
            self.save_cookies()
            invalidate_login_cache()
            return {"ok": True, "msg": "登录成功"}
        return {"ok": False, "msg": j.get("msg") or "登录失败", "captcha": j.get("captcha")}

    def logout(self):
        try:
            self.get("/user/logout")
        except Exception:
            pass
        self.jar.clear()
        store.set_site_cookies({})
        invalidate_login_cache()

    def check_login(self, ttl=120):
        now = time.time()
        if now - _login_cache["ts"] < ttl:
            return {"ok": _login_cache["ok"], "msg": _login_cache["msg"], "cached": True}
        try:
            st, html = self.get("/")
        except SiteError as e:
            return {"ok": False, "msg": str(e)}
        if "未登录" in html and len(html) < 3000:
            res = {"ok": False, "msg": "未登录或登录已失效"}
        else:
            res = {"ok": True, "msg": "已登录"}
        _login_cache.update(ts=now, ok=res["ok"], msg=res["msg"])
        return res

    # ---------- parsing ----------
    def parse_obj(self, html):
        """抽取页面里 _obj.<name> = <json> 的数据块"""
        out = {}
        for m in re.finditer(r"_obj\.([A-Za-z_$][\w$]*)\s*=", html):
            name = m.group(1)
            i = m.end()
            while i < len(html) and html[i] in " \t\r\n":
                i += 1
            if i >= len(html) or html[i] not in "{[\"'":
                continue
            val, _ = _scan_json(html, i)
            if val is None:
                continue
            try:
                out[name] = json.loads(val)
            except Exception:
                try:
                    out[name] = json.loads(_repair(val))
                except Exception:
                    continue
        return out

    def page_type(self, html):
        m = re.search(r"_BT\.PC\.HTML\('([^']+)'\)", html)
        return m.group(1) if m else ""

    # ---------- listing / search / detail ----------
    def fetch_page(self, path):
        st, html = self.get_checked(path)
        obj = self.parse_obj(html)
        return {"status": st, "type": self.page_type(html), "obj": obj, "html": html}

    def nav_categories(self, page=None):
        """从首页 header 里探测栏目菜单"""
        try:
            page = page or self.fetch_page("/")
        except SiteError:
            return []
        found = []
        seen = set()

        def walk(node):
            if isinstance(node, dict):
                url = node.get("url") or node.get("link") or node.get("href") or node.get("u")
                name = node.get("name") or node.get("title") or node.get("text") or node.get("n")
                if isinstance(url, str) and isinstance(name, str):
                    mm = re.match(r"^/([a-z]{2,6})/?(\?.*)?$", url.strip())
                    if mm and mm.group(1) not in seen and name.strip() and len(name.strip()) <= 8:
                        seen.add(mm.group(1))
                        found.append({"slug": mm.group(1), "name": name.strip()})
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)

        walk(page["obj"].get("header") or {})
        return found

    def listing_page(self, cat, page=1):
        """真实分页是 /{cat}?page=N，每页 48 条；_obj.page 里带总页数。"""
        path = f"/{cat}" if page <= 1 else f"/{cat}?page={int(page)}"
        p = self.fetch_page(path)
        items = normalize_items(p["obj"], cat, self.base)
        meta = p["obj"].get("page") if isinstance(p["obj"], dict) else {}
        meta = meta if isinstance(meta, dict) else {}
        return {"items": items,
                "pages": int(meta.get("pages") or 0),
                "curr": int(meta.get("curr") or page),
                "len": len(p["html"]),
                "ptype": p["type"],
                "head": p["html"][:400]}

    def listing(self, cat, page=1):
        return self.listing_page(cat, page)["items"]

    def _page_with_retry(self, cat, pg, tries=4, cooldown=120):
        """站点按会话限流：约 40 次请求后返回 1KB 空壳页。

        关键：空壳页不是时间限流，死等没用——重新解一次 PoW 换新会话即可恢复。
        """
        last = {"items": [], "pages": 0, "curr": pg, "len": 0}
        for i in range(tries):
            try:
                r = self.listing_page(cat, pg)
            except SiteError as e:
                if str(e) == "NEED_LOGIN":
                    raise
                last = {"items": [], "pages": 0, "curr": pg, "len": 0}
                time.sleep(min(cooldown, 6 + i * 6))
                continue
            except Exception:
                last = {"items": [], "pages": 0, "curr": pg, "len": 0}
                time.sleep(min(cooldown, 6 + i * 6))
                continue
            if r.get("items"):
                self.page_delay = max(1.5, self.page_delay * 0.98)
                return r
            last = r
            if int(r.get("len") or 0) < 1500:
                # 安全验证页 = 本会话被限流，换干净会话后立刻重试
                self.rotate_session()
                time.sleep(1.0)
                continue
            time.sleep(min(cooldown, 5 + i * 5))
        return last

    def listing_pages(self, cat, pages=2, on_page=None):
        """按页抓取。

        站点偶尔返回 1KB 空壳页（限流），单页失败只跳过、不终止整栏目，
        最后再统一补抓一遍。
        """
        total = max(1, int(pages or 1))
        out, seen, failed = [], set(), []
        done = set()

        def take(items):
            added = 0
            for it in items:
                if it["id"] in seen:
                    continue
                seen.add(it["id"])
                out.append(it)
                added += 1
            return added

        pg = 1
        while pg <= total:
            r = self._page_with_retry(cat, pg, tries=3)
            curr = r.get("curr") or pg
            if pg > 1 and curr < pg:      # 已超出总页数，站点回落到最后一页
                break
            site_pages = int(r.get("pages") or 0)
            if site_pages:
                total = min(total, site_pages)   # 别越过站点真实末页
            items = r.get("items") or []
            if items:
                take(items)
                done.add(pg)
                if on_page:
                    try:
                        on_page(pg, total, len(items), len(out))
                    except Exception:
                        pass
            else:
                failed.append(pg)
                # 头两页都空 = 会话级异常，再抓也没意义
                if not out and len(failed) >= 2:
                    break
            pg += 1
            time.sleep(self.page_delay)
            if pg % 18 == 0:          # 站点约 30 次/会话，主动轮换更稳
                self.rotate_session()
                time.sleep(0.8)

        # 补抓失败页（等冷却后基本都能恢复）
        for fp in failed:
            if fp in done:
                continue
            r = self._page_with_retry(cat, fp, tries=3, cooldown=120)
            items = r.get("items") or []
            if items:
                take(items)
                if on_page:
                    try:
                        on_page(fp, total, len(items), len(out))
                    except Exception:
                        pass
            time.sleep(self.page_delay)
        return out

    def total_pages(self, cat):
        p = self.fetch_page(f"/{cat}")
        meta = p["obj"].get("page") if isinstance(p["obj"], dict) else {}
        return int((meta or {}).get("pages") or 0)

    def downurl(self, dirname, iid):
        """磁力在独立接口里：GET /res/downurl/{dir}/{id}"""
        referer = f"{self.base}/{dirname}/{iid}"
        try:
            st, txt = self.get(f"/res/downurl/{dirname}/{iid}", referer=referer)
        except SiteError:
            return []
        if st != 200:
            return []
        try:
            j = json.loads(txt)
        except Exception:
            return []
        return magnets_from_downurl(j)

    def search(self, keyword):
        path = "/search?q=" + urllib.parse.quote(keyword)
        st, html = self.get_checked(path)
        obj = self.parse_obj(html)
        items = decode_search(obj.get("search") or {}, self.base)
        if not items:
            items = decode_items(obj, "search", self.base)
        return items

    def detail(self, url, cat=""):
        m = re.search(r"/(mv|tv|ac)/([A-Za-z0-9]+)", url)
        if m:
            dirname, iid = m.group(1), m.group(2)
        else:
            dirname = cat if cat in DIR_NAMES else "mv"
            iid = url.rstrip("/").rsplit("/", 1)[-1].split(".")[0]
        p = self.fetch_page(url)
        info = normalize_detail(p["obj"], self.base)
        magnets = self.downurl(dirname, iid)
        if not magnets:
            magnets = extract_magnets(p["obj"], self.base)
        if not magnets:
            magnets = magnets_from_html(p["html"])
        info["magnets"] = _dedup_magnets(magnets)
        info["raw_type"] = p["type"]
        info["dir"], info["iid"] = dirname, iid
        return info

    def detail_safe(self, url, cat="", tries=3):
        """洗版扫描用：撞上「浏览器安全验证」空壳页就换会话重来，避免误判成「没有更好的资源」。"""
        last = None
        for i in range(tries):
            try:
                info = self.detail(url, cat)
            except SiteError as e:
                if str(e) == "NEED_LOGIN":
                    raise
                last = e
                self.rotate_session()
                time.sleep(1.5)
                continue
            except Exception as e:
                last = e
                self.rotate_session()
                time.sleep(1.5)
                continue
            if info.get("magnets"):
                return info
            # 空壳页特征：没有磁力 + 标题也拿不到
            if not info.get("title"):
                last = SiteError("空壳页")
                self.rotate_session()
                time.sleep(1.5)
                continue
            return info
        raise SiteError(f"详情抓取失败: {last}")


# ---------- helpers ----------
def _scan_json(s, start):
    depth = 0
    i = start
    in_str = False
    quote = ""
    esc = False
    while i < len(s):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == quote:
                in_str = False
        else:
            if ch in "\"'":
                in_str = True
                quote = ch
            elif ch in "{[":
                depth += 1
            elif ch in "}]":
                depth -= 1
                if depth == 0:
                    return s[start:i + 1], i + 1
        i += 1
    return None, start


def _repair(s):
    s = re.sub(r"\bundefined\b", "null", s)
    s = re.sub(r"\bNaN\b", "null", s)
    s = re.sub(r"'([^'\\]*)'\s*:", r'"\1":', s)
    return s


def _first(d, keys, default=""):
    for k in keys:
        v = d.get(k)
        if v not in (None, "", [], {}):
            return v
    return default


def _abs(u, base):
    if not isinstance(u, str):
        return ""
    u = u.strip()
    if u.startswith("http"):
        return u
    if u.startswith("//"):
        return "https:" + u
    if u.startswith("/"):
        return base + u
    return base + "/" + u


# 站点把「地区/类型」编码成数字索引，0 起算对应这张表（前端 _fun.rea 用的同一张表）
TAGS = [
    "美国", "大陆", "日本", "剧情", "科幻", "动作", "喜剧", "爱情", "冒险",
    "犯罪", "悬疑", "儿童", "歌舞", "音乐", "奇幻", "动画", "恐怖", "惊悚",
    "丧尸", "战争", "传记", "纪录", "西部", "灾难", "古装", "武侠", "家庭",
    "短片", "校园", "文艺", "运动", "青春", "同性", "励志", "人性", "美食",
    "女性", "治愈", "历史", "真人秀", "脱口秀", "萌系", "日常", "热血",
    "机战", "游戏", "情色", "搞笑", "恋爱", "后宫", "百合", "基腐", "致郁",
    "异世界", "泡面", "战斗", "加拿大", "香港", "台湾", "韩国", "印度",
    "德国", "法国", "英国", "意大利", "巴西", "泰国", "澳大利亚", "荷兰",
    "西班牙", "墨西哥",
]

IMG_HOST = "https://s.tutu.pm"
DIR_NAMES = {"mv": "电影", "tv": "电视剧", "ac": "动漫"}


def _cell(cols, key, idx, default=None):
    v = cols.get(key)
    if isinstance(v, list) and idx < len(v):
        return v[idx]
    return default


def _tag_text(a):
    """a 字段 [年份, 地区索引, 类型索引...] -> '2026 · 英国 · 剧情/动作'"""
    if not isinstance(a, list) or not a:
        return ""
    parts = []
    year = a[0]
    if isinstance(year, (int, str)) and str(year).strip():
        parts.append(str(year))
    regions, kinds = [], []
    for v in a[1:]:
        if isinstance(v, str) and v.strip():
            (regions if len(v) <= 4 and not v.isdigit() else kinds).append(v.strip())
        elif isinstance(v, int) and 0 <= v < len(TAGS):
            t = TAGS[v]
            (regions if (v <= 2 or v >= 56) else kinds).append(t)
    if regions:
        parts.append("/".join(dict.fromkeys(regions)))
    if kinds:
        parts.append("/".join(dict.fromkeys(kinds[:2])))
    return " · ".join(parts)


def _decode_cols(block, cat, base):
    """列式解码：{字段: [值...]} -> 条目列表。站点所有列表页都是这个结构。"""
    if not isinstance(block, dict):
        return []
    cols = {k: v for k, v in block.items() if isinstance(v, list)}
    ids = cols.get("i")
    if not isinstance(ids, list) or not ids:
        return []
    # 有些板块用 ty 数字区分栏目（1=mv 2=tv 3=ac）
    ty = block.get("ty")
    dirs = ["mv", "tv", "ac"]
    out = []
    for idx, iid in enumerate(ids):
        if not isinstance(iid, str) or not iid.strip():
            continue
        title = _cell(cols, "t", idx, "")
        if not isinstance(title, str) or not title.strip():
            continue
        slug = cat
        tcell = _cell(cols, "ty", idx)
        if isinstance(tcell, int) and 1 <= tcell <= 3:
            slug = dirs[tcell - 1]
        elif isinstance(ty, int) and 1 <= ty <= 3 and cat not in DIR_NAMES:
            slug = dirs[ty - 1]
        if slug not in DIR_NAMES:
            slug = cat
        sub = _tag_text(_cell(cols, "a", idx))
        score_db = _cell(cols, "d", idx)
        score_im = _cell(cols, "im", idx)
        if isinstance(score_db, (int, float)) and score_db > 0:
            sub += f" · 豆瓣 {score_db}"
        elif isinstance(score_im, (int, float)) and score_im > 0:
            sub += f" · IMDB {score_im}"
        mcount, pcount = _cell(cols, "b", idx), _cell(cols, "w", idx)
        badge = []
        if isinstance(mcount, int) and mcount > 0:
            badge.append(f"{mcount} 磁力")
        if isinstance(pcount, int) and pcount > 0:
            badge.append(f"{pcount} 网盘")
        if badge:
            sub += " · " + "/".join(badge)
        labels = _cell(cols, "q", idx) or []
        if isinstance(labels, list) and labels:
            sub += " · " + "/".join(str(x) for x in labels[:2])
        out.append({
            "id": f"{slug}:{iid}",
            "cat": slug,
            "title": title.strip(),
            "url": f"{base}/{slug}/{iid}",
            "poster": poster_url(slug, iid),
            "sub": sub.strip(" ·")[:90],
            "ts": 0,
            "heat": _cell(cols, "r", idx) or 0,
        })
    return out


def decode_items(obj, cat, base):
    """把页面 obj 里所有列式板块展开成条目"""
    blocks = []
    inl = obj.get("inlist")
    if isinstance(inl, dict):
        blocks.append(inl)
    elif isinstance(inl, list):
        blocks.extend([b for b in inl if isinstance(b, dict)])
    sr = obj.get("search")
    if isinstance(sr, dict):
        blocks.append(sr)
    elif isinstance(sr, list):
        blocks.extend([b for b in sr if isinstance(b, dict)])
    for key in ("list", "data", "items"):
        v = obj.get(key)
        if isinstance(v, dict) and isinstance(v.get("i"), list):
            blocks.append(v)
        elif isinstance(v, list):
            blocks.extend([b for b in v if isinstance(b, dict) and isinstance(b.get("i"), list)])
    out, seen = [], set()
    for b in blocks:
        for it in _decode_cols(b, cat, base):
            if it["id"] in seen:
                continue
            seen.add(it["id"])
            out.append(it)
    return out


def decode_search(sr, base):
    """搜索结果结构：search.l = {title:[], d:[栏目], i:[id], info:[], year:[], pf:{db:{s:[]}}}"""
    if not isinstance(sr, dict):
        return []
    l = sr.get("l")
    if not isinstance(l, dict):
        return []
    ids = l.get("i")
    if not isinstance(ids, list):
        return []
    pf = l.get("pf") if isinstance(l.get("pf"), dict) else {}
    db_scores = (pf.get("db") or {}).get("s") if isinstance(pf.get("db"), dict) else None
    im_scores = (pf.get("im") or {}).get("s") if isinstance(pf.get("im"), dict) else None
    out = []
    for idx, iid in enumerate(ids):
        if not isinstance(iid, str) or not iid.strip():
            continue
        slug = _cell(l, "d", idx, "")
        if slug not in DIR_NAMES:
            continue
        title = _cell(l, "title", idx, "")
        if not isinstance(title, str) or not title.strip():
            continue
        parts = []
        year = _cell(l, "year", idx)
        if year:
            parts.append(str(year))
        info = _cell(l, "info", idx)
        if isinstance(info, str) and info.strip():
            parts.append(info.strip()[:40])
        score = None
        if isinstance(db_scores, list) and idx < len(db_scores) and db_scores[idx]:
            score = ("豆瓣", db_scores[idx])
        elif isinstance(im_scores, list) and idx < len(im_scores) and im_scores[idx]:
            score = ("IMDB", im_scores[idx])
        if score:
            parts.append(f"{score[0]} {score[1]}")
        out.append({
            "id": f"{slug}:{iid}",
            "cat": slug,
            "title": title.strip(),
            "url": f"{base}/{slug}/{iid}",
            "poster": poster_url(slug, iid),
            "sub": " · ".join(parts)[:90],
            "ts": 0,
            "heat": 0,
        })
    return out


def normalize_items(obj, cat, base):
    """兼容入口：优先列式解码，失败再退回旧的行式探测"""
    items = decode_items(obj, cat, base)
    if items:
        return items
    out = []
    seen = set()

    def walk(node, depth=0):
        if depth > 8:
            return
        if isinstance(node, dict):
            it = _as_item(node, cat, base)
            if it and it["id"] not in seen:
                seen.add(it["id"])
                out.append(it)
            for v in node.values():
                walk(v, depth + 1)
        elif isinstance(node, list):
            for v in node:
                walk(v, depth + 1)

    walk(obj)
    return out


def _as_item(d, cat, base):
    if not isinstance(d, dict):
        return None
    # 必须有 id 与 标题
    if not any(k in d for k in ("id", "aid", "vid", "tid", "rid")):
        return None
    title = _first(d, ("name", "title", "cname", "ename", "mv_name", "post_title"))
    if not isinstance(title, str) or not title.strip():
        return None
    url = _first(d, ("url", "link", "href", "path", "u", "detail_url"))
    if not isinstance(url, str) or not url.strip():
        return None
    if url.strip().startswith("magnet:"):
        return None
    iid = str(_first(d, ("id", "aid", "vid", "tid", "rid"), ""))
    if not iid:
        return None
    poster = _abs(_first(d, ("pic", "img", "poster", "cover", "thumb", "image", "pic_url", "picurl")), base)
    sub_parts = []
    for k in ("note", "remark", "desc", "state", "status", "info", "year", "area",
              "score", "version", "size", "type", "alias", "language", "quality"):
        v = d.get(k)
        if isinstance(v, (str, int, float)) and str(v).strip() and len(str(v)) < 40:
            sub_parts.append(str(v).strip())
    ts = 0
    for k in ("ts", "time", "addtime", "pubtime", "created_at", "update_time", "date", "dt"):
        v = d.get(k)
        if isinstance(v, (int, float)) and v > 1000000000:
            ts = int(v)
            break
        if isinstance(v, str):
            if v.isdigit() and len(v) in (10, 13):
                ts = int(v[:10])
                break
            m = re.match(r"(20\d\d)-(\d\d)-(\d\d)", v)
            if m:
                import datetime
                try:
                    ts = int(datetime.datetime(*map(int, m.groups())).timestamp())
                except Exception:
                    ts = 0
                break
    return {"id": f"{cat}:{iid}", "cat": cat, "title": title.strip(), "url": _abs(url, base),
            "poster": poster, "sub": " · ".join(dict.fromkeys(sub_parts))[:80], "ts": ts}


def items_from_html(html, cat, base):
    out, seen = [], set()
    for m in re.finditer(r'href="(/(?:mv|tv|ac|zjx|rsc|dm)/\d+[^"]*)"[^>]*>(.{0,120}?)</a>', html, re.S):
        url = _abs(m.group(1), base)
        title = re.sub(r"<[^>]+>", "", m.group(2)).strip()
        if not title or url in seen:
            continue
        seen.add(url)
        out.append({"id": f"{cat}:{url.rsplit('/', 1)[-1].split('.')[0]}", "cat": cat,
                    "title": title, "url": url, "poster": "", "sub": "", "ts": 0})
    return out


def magnets_from_downurl(j):
    """解析 /res/downurl 返回：
    {"code":200,"downlist":{"list":{"m":[40位infohash],"t":[文件名],"s":["1.67G"],"k":[0],"u":[种子id]}}}
    k=0 直接是磁力，非 0 的是打包/文件夹种子。
    """
    if not isinstance(j, dict) or str(j.get("code")) != "200":
        return []
    dl = j.get("downlist") or {}
    lst = dl.get("list") or {}
    ms = lst.get("m") or []
    ts = lst.get("t") or []
    ss = lst.get("s") or []
    ks = lst.get("k") or []
    if not isinstance(ms, list):
        return []
    direct, packed = [], []
    for i, h in enumerate(ms):
        if not isinstance(h, str):
            continue
        h = h.strip()
        name = ts[i] if i < len(ts) and isinstance(ts[i], str) else ""
        size_text = ss[i] if i < len(ss) and isinstance(ss[i], str) else ""
        k = ks[i] if i < len(ks) else 0
        if re.fullmatch(r"[0-9a-fA-F]{40}", h):
            url = "magnet:?xt=urn:btih:%s&dn=%s" % (h, urllib.parse.quote(name))
        elif re.fullmatch(r"[A-Za-z2-7]{32}", h):
            url = "magnet:?xt=urn:btih:%s&dn=%s" % (h, urllib.parse.quote(name))
        else:
            continue
        rec = {"url": url, "btih": h.lower(), "size_text": size_text,
               "size_bytes": parse_size(size_text), "label": name, "kind": k}
        (direct if k == 0 else packed).append(rec)
    return direct or packed


def extract_magnets(obj, base, referer=None):
    """从 _obj 里找出磁力及其大小标注"""
    cands = []

    def walk(node, depth=0):
        if depth > 9:
            return
        if isinstance(node, dict):
            vals = list(node.values())
            mags = [v for v in vals if isinstance(v, str) and v.strip().startswith("magnet:")]
            if mags:
                size_text = ""
                label = ""
                for k, v in node.items():
                    if isinstance(v, str):
                        if re.search(r"[\d.]+\s*[KMGT]i?B", v, re.I) and not size_text:
                            size_text = re.search(r"[\d.,]+\s*[KMGT]i?B", v, re.I).group(0)
                        if k in ("name", "title", "label", "filename", "note") and not label:
                            label = v[:80]
                    if isinstance(v, (int, float)) and k in ("size", "filesize", "bytes", "dsize") and v > 1024:
                        size_text = size_text or str(int(v))
                for mg in mags:
                    cands.append({"url": mg.strip(), "size_text": size_text, "label": label,
                                  "referer": referer or base})
            for v in vals:
                walk(v, depth + 1)
        elif isinstance(node, list):
            for v in node:
                walk(v, depth + 1)
        elif isinstance(node, str):
            if "magnet:?xt=" in node:
                for line in re.split(r"[\r\n]+", node):
                    if "magnet:?xt=" in line:
                        m = re.search(r"magnet:\?xt=urn:btih:[^\s\"'<>]+", line)
                        if m:
                            sm = re.search(r"[\d.,]+\s*[KMGT]i?B", line, re.I)
                            cands.append({"url": m.group(0), "size_text": sm.group(0) if sm else "",
                                          "label": "", "referer": referer or base})

    walk(obj)
    out = []
    for c in cands:
        btih = btih_of(c["url"])
        if not btih:
            continue
        out.append({"url": c["url"], "btih": btih, "size_text": c["size_text"],
                    "size_bytes": parse_size(c["size_text"]), "label": c["label"]})
    return out


def magnets_from_html(html):
    out = []
    for m in re.finditer(r"(magnet:\?xt=urn:btih:[0-9a-zA-Z]+[^\"'<>\s]*)", html):
        url = m.group(1)
        btih = btih_of(url)
        if not btih:
            continue
        tail = html[m.end():m.end() + 200]
        sm = re.search(r"[\d.,]+\s*[KMGT]i?B", tail, re.I)
        out.append({"url": url, "btih": btih, "size_text": sm.group(0) if sm else "",
                    "size_bytes": parse_size(sm.group(0)) if sm else 0, "label": ""})
    return out


def _dedup_magnets(ms):
    seen, out = set(), []
    for m in sorted(ms, key=lambda x: x.get("size_bytes") or 0, reverse=True):
        if m["btih"] in seen:
            continue
        seen.add(m["btih"])
        out.append(m)
    return out


def normalize_detail(obj, base):
    """拿标题/海报/简介。该站详情页结构固定为 _obj.d"""
    d = obj.get("d") if isinstance(obj, dict) else None
    if isinstance(d, dict) and (d.get("title") or d.get("name")):
        tags = []
        for k in ("leixing", "diqu", "yuyan"):
            v = d.get(k)
            if isinstance(v, list):
                tags.extend(str(x) for x in v if x)
        desc = d.get("summary") or d.get("content") or ""
        extra = []
        for k in ("year", "stime", "times"):
            v = d.get(k)
            if v:
                extra.append(str(v))
        return {
            "title": str(d.get("title") or d.get("name") or "").strip(),
            "poster": "",
            "desc": re.sub(r"<[^>]+>", "", str(desc))[:600],
            "tags": tags,
            "meta": " · ".join(extra),
            "down_urls": [],
        }

    title, poster, desc, down_urls = "", "", "", []

    def walk(node, depth=0):
        nonlocal title, poster, desc
        if depth > 7:
            return
        if isinstance(node, dict):
            ch = _first(node, ("cname", "name", "title"))
            if isinstance(ch, str) and len(ch) > len(title) and len(ch) < 120:
                title = ch
            pic = _first(node, ("pic", "poster", "cover", "img"))
            if isinstance(pic, str) and pic and not poster and (pic.startswith("http") or pic.startswith("/")):
                poster = _abs(pic, base)
            d = _first(node, ("desc", "content", "intro", "description", "note"))
            if isinstance(d, str) and len(d) > len(desc) and len(d) < 2000:
                desc = d
            for k in ("down_url", "downurl", "download", "down"):
                v = node.get(k)
                if isinstance(v, str) and v.startswith("/"):
                    u = _abs(v, base)
                    if u not in down_urls:
                        down_urls.append(u)
            for v in node.values():
                walk(v, depth + 1)
        elif isinstance(node, list):
            for v in node:
                walk(v, depth + 1)

    walk(obj)
    return {"title": title, "poster": poster,
            "desc": re.sub(r"<[^>]+>", "", desc)[:600], "down_urls": down_urls[:3]}
