"""同步与自动推送引擎。"""
import random
import re
import threading
import time
import traceback

from . import pan, site, store

_sync_lock = threading.Lock()
_thread = None
_stop = threading.Event()
_state = {"running": False, "last_run": 0, "last_msg": "", "next_run": 0,
          "phase": "", "page": 0, "pages": 0, "cat": "", "found": 0, "full": False}


def state():
    return dict(_state)


_NOLOGIN_TTL = 6 * 3600

# 评分门槛把队列挡空时，最多每 6 小时提示一次，别每轮刷屏。
_score_note = {"at": 0, "n": -1}


def _note_blocked(n, gate):
    if n <= 0:
        return
    now = time.time()
    if n == _score_note["n"] and now - _score_note["at"] < 6 * 3600:
        return
    _score_note.update({"at": now, "n": n})
    store.log(f"评分门槛 {float(gate):g}：另有 {n} 条评分不够暂不推（设置里可调低或勾「没评分的也推」）", "info")


def _notify_nologin():
    """登录失效时最多每 6 小时记一条日志。

    以前是「一辈子只记一条」，结果站点改了 cookie 名、抓取静默停了两天也没人发现。
    现在既不刷屏，又能在日志里看到，界面上也有同步状态提示。
    """
    now = int(time.time())
    if now - int(store.get_kv("nologin_at", 0) or 0) < _NOLOGIN_TTL:
        return
    store.set_kv("nologin_at", now)
    store.log("还没登录站点，已跳过抓取。去「站点账号」登录后会自动恢复", "warn")


def _cand(magnets, min_gb, max_gb):
    """按体积范围过滤候选磁力（0 表示不限），死种/广告包直接排除。"""
    try:
        bad = store.bad_hashes()
    except Exception:
        bad = set()
    out = []
    for m in magnets:
        h = str(m.get("btih") or "").lower()
        if h and h in bad:
            continue
        size = m.get("size_bytes") or 0
        if min_gb and size and size < min_gb * 1024 ** 3:
            continue
        if max_gb and size and size > max_gb * 1024 ** 3:
            continue
        out.append(m)
    return out


def _pick_from(cand, rule):
    if not cand:
        return None
    if rule == "smallest":
        return sorted(cand, key=lambda x: (x.get("size_bytes") or 0))[0]
    if rule == "latest":
        return cand[0]
    return sorted(cand, key=lambda x: (x.get("size_bytes") or 0), reverse=True)[0]


def _sec_pool(P):
    """「能秒传」的 infohash 集合。

    115 没有「磁力秒传预检」接口，能确定的只有两种：
      1）以前推过、推送后几秒内就 100% 的（存在库里）；
      2）115 离线任务列表里已经是「已完成」状态的。
    有这两种就先挑它们，命中率最高，也不额外打 115。
    """
    pool = set()
    try:
        pool |= store.sec_hashes()
    except Exception:
        pass
    try:
        bad = store.bad_hashes()
    except Exception:
        bad = set()
    if P is not None:
        try:
            r = P.tasks(page=1, page_size=50)
            for t in (r.get("tasks") or []):
                if int(t.get("percentDone") or 0) >= 100 or str(t.get("status")) == "2":
                    h = str(t.get("info_hash") or "").lower()
                    if h and h not in bad:
                        pool.add(h)
        except Exception:
            pass
    return pool - bad


def _quality(m):
    """一条磁力的画质信息（分辨率/片源/编码加分），q 越大越好。"""
    return site.quality_of(m.get("label") or m.get("url") or "")


def _pick_best(magnets, cfg, pool=None):
    """挑磁力，优先级：分辨率 > 秒传 > 片源/体积。

    第一层只按分辨率分档（2160p > 1080p > 720p），所以站点上只要有 4K，
    就不会再挑 1080p —— 哪怕那个 1080p 体积更大、能秒传。
    第二层在同一分辨率里优先秒传（4K 能秒传的 WEB 会比 4K REMUX 先中）；
    第三层才按完整画质分（片源/编码/HDR）和体积规则。

    「优先 4K」关掉后直接按体积/顺序走。枪版/抢先版只要还有正常版本就不参与：
    4K 的枪版不如 1080p 的正片。
    """
    auto = cfg["auto"]
    cand = _cand(magnets, auto.get("min_gb") or 0, auto.get("max_gb") or 0)
    if not cand:
        return None

    scored = [(m, _quality(m)) for m in cand]
    clean = [(m, q) for m, q in scored if q.get("src") != "枪版"]
    if clean:
        scored = clean

    if auto.get("prefer_4k", True):
        def res_rank(q):
            # q = 分辨率*10000 + 片源*1000 + 加分，这里只取分辨率那一位
            return int(q.get("q") or 0) // 10000

        top = max(res_rank(q) for _, q in scored)
        top_res = [(m, q) for m, q in scored if res_rank(q) == top]
        if top_res:
            scored = top_res

    if pool and auto.get("prefer_sec", True):
        hot = [(m, q) for m, q in scored if str(m.get("btih") or "").lower() in pool]
        if hot:
            scored = hot

    return _pick_from([m for m, _ in scored], auto.get("pick", "largest"))


def _pan_auto(it, info, P, cfg):
    """自动推送的网盘兜底：磁力全死时，改转 115 网盘分享。

    和订阅共用 subs._pan_push（同一套落目录/记账/去重逻辑），
    只是过滤规则换成「自动推送」的体积范围。
    """
    try:
        from . import subs as _subs
    except Exception:
        return None
    s = {"cat": it.get("cat") or "", "qmin": "",
         "min_gb": cfg["auto"].get("min_gb") or 0,
         "max_gb": cfg["auto"].get("max_gb") or 0,
         "filter_inc": "", "filter_exc": "",
         "best_version": 1, "current_priority": 0}
    try:
        r = _subs._pan_push(s, it, info, P)
        if r:
            return r
    except Exception as e:
        store.log(f"115 网盘兜底异常 {it.get('title')}: {type(e).__name__}: {e}", "warn")
    return None


def _probe_sec(P, btih, wait=4.0):
    """推送后等一下，看 115 是不是直接秒传完成了。是就记下来，下次优先它。"""
    h = str(btih or "").lower()
    if not h:
        return None
    time.sleep(wait)
    try:
        t = P.find_task(h, max_pages=1, page_size=50)
    except Exception:
        return None
    if not t:
        return None
    if int(t.get("percentDone") or 0) >= 100 or str(t.get("status")) == "2":
        try:
            store.mark_sec(h)
        except Exception:
            pass
        return t
    return None


# 115 离线任务「卡住」判定的时间线：
#   一点没动（0%）—— 45 分钟就判死换源。115 是服务端下载，有源的种子
#                     通常几分钟就完成（实测公网种 15 秒、站内剧 3 分钟），
#                     45 分钟还 0% 基本就是没 peer，再等也是白等。
#   已经在动 —— 12 小时。几十 G 的合集包慢慢爬是正常的。
_STALL_ZERO_MIN = 45
_STALL_SLOW_H = 12
# 115 离线任务列表的上限是 100 条。完成的任务记录不清掉，列表很快占满，
# 新推的磁力就永远排不进队列——表现是所有任务一直 0%，一部也下不下来。
_LIXIAN_CAP = 90        # 到 90 条就清一批已完成的记录，留 10 条余量


def _purge_done_tasks(P, tasks, keep_hashes=None):
    """清「已完成」的离线任务记录（只删记录，网盘文件保留）。

    队列满了 → 新任务一直 0% → 落地目录只剩残缺文件，一部也下不完。
    """
    if len(tasks) < _LIXIAN_CAP:
        return 0
    keep = set(keep_hashes or ())
    cand = [h for h, t in tasks.items()
            if _t_state(t) == "done" and h not in keep]
    if not cand:
        return 0
    n = 0
    for i in range(0, len(cand), 20):          # 20 条一批，少打几次接口
        chunk = cand[i:i + 20]
        try:
            r = P.del_tasks(chunk, del_files=False)
            if r.get("ok"):
                n += len(chunk)
        except Exception:
            pass
        if len(tasks) - n < _LIXIAN_CAP - 30:
            break
    if n:
        store.log(f"清理 {n} 条已完成的 115 离线任务记录（只删记录、文件保留），"
                  f"给新任务腾出队列位", "info")
    return n


def _t_state(t):
    """115 离线任务状态归成三档：done / fail / run。"""
    try:
        pd = int(float(t.get("percentDone") or 0))
    except (TypeError, ValueError):
        pd = 0
    st = str(t.get("status") or "")
    if pd >= 100 or st == "2" or "已完成" in st:
        return "done"
    if st == "3" or "失败" in st or "删除" in st:
        return "fail"
    return "run"


def _hash_of(magnet):
    m = re.search(r"btih:([0-9a-fA-F]{40}|[A-Za-z2-7]{32})", magnet or "")
    return m.group(1).lower() if m else ""


def _task_map(P, pages=3):
    """115 任务列表 → {infohash: task}。一次翻页给下面几个检查复用。"""
    out = {}
    for pg in range(1, pages + 1):
        try:
            r = P.tasks(page=pg, page_size=50)
        except Exception:
            break
        lst = r.get("tasks") or []
        for t in lst:
            h = str(t.get("info_hash") or "").lower()
            if h:
                out[h] = t
        if len(lst) < 50:
            break
    return out


def _reap_tasks(P, min_age_h=None):
    """给推送出去的任务收尾。

    之前是「推完就不管了」：115 卡在等待中的任务没人处理，落地目录里
    留着几百 KB 的残缺文件，既占空间又拖慢后面的任务。

    这里做三件事：
      1）下完了 → 任务标记 done；
      2）超过宽限期还在跑 → 继续等，不动；
      3）超时没下完 / 115 明确失败 → 删任务（连文件）+ 拉黑 hash + 条目回队，
         下一轮自动挑别的候选磁力。
    """
    rows = [t for t in store.list_tasks(limit=200) if t.get("status") == "sent"]
    tasks = _task_map(P)
    if not tasks:
        return {"done": 0, "dead": 0, "purged": 0}
    if not rows:
        return {"done": 0, "dead": 0, "purged": _purge_done_tasks(P, tasks)}
    now = int(time.time())
    done = dead = 0
    for r in rows:
        h = _hash_of(r.get("magnet") or "")
        if not h:
            continue
        t = tasks.get(h)
        if not t:
            continue
        age_h = (now - int(r.get("created") or 0)) / 3600.0
        st = _t_state(t)
        if st == "done":
            store.update_task(r["id"], "done", "115 下载完成")
            done += 1
            continue
        try:
            pd = float(t.get("percentDone") or 0)
        except (TypeError, ValueError):
            pd = 0.0
        # 一点没动的 45 分钟就判死；已经在下的给 12 小时慢慢爬。
        # 一刀切 6 小时会误杀几十 G 的大合集，等满 6 小时又太慢——
        # 站点的魔改种说死就死，快点换源反而更容易下成。
        if min_age_h is not None:
            limit = float(min_age_h)
        else:
            limit = (_STALL_ZERO_MIN / 60.0) if pd < 1 else _STALL_SLOW_H
        if st != "fail" and age_h < limit:
            continue
        fid = str(t.get("delete_file_id") or t.get("file_id") or "")
        if _kill(P, h, r, st, age_h, fid):
            dead += 1
    # 队列腾位置：已完成的记录留着只会把 100 条塞满，新任务永远排不进队。
    # 只保留「还没下完、要继续盯进度」的那几条，已完成的记录可以放心删。
    db_h = {_hash_of(r.get("magnet") or "") for r in rows}
    keep = {h for h in db_h if h in tasks and _t_state(tasks[h]) != "done"}
    purged = _purge_done_tasks(P, tasks, keep_hashes=keep)
    return {"done": done, "dead": dead, "purged": purged}


def _kill(P, h, row, st="run", age_h=0.0, fid=""):
    """删掉卡住的 115 任务 + 把落地的残留目录移进回收站 + 拉黑 hash + 条目回队。

    注意：`del_source_file=1` 对「一条都没下下来」的任务不生效——任务记录
    删了，目录和那几个占位文件还留在网盘里。所以还得按 file_id 再删一次
    （走 115 回收站，可还原）。
    """
    try:
        P.del_task(h)
    except Exception:
        pass
    if fid:
        try:
            P.delete_file(fid)
        except Exception:
            pass
    why = "115 下载失败" if st == "fail" else (
        f"卡了 {age_h * 60:.0f} 分钟" if age_h < 1 else f"卡了 {age_h:.0f} 小时")
    store.mark_bad(h, why)
    title = str(row.get("title") or "")
    try:
        store.update_task(row["id"], "dead", "死种，已换源")
        # 订阅任务不能走「条目回队」：追更的条目回队会被当成整季去推，
        # 跟「只补新一集」的规则打架。订阅引擎自己会按 last_ep 再试。
        # 但要把订阅排到队首：种子已拉黑，下一轮会直接换别的磁力 / 走 115 网盘。
        if "订阅" in title:
            store.sub_expedite(row.get("item_id"))
            store.sub_forget_hash(row.get("item_id"), h)
        else:
            store.mark_retry(row["item_id"])
    except Exception:
        pass
    store.log(f"死种换源：{title[:36]}（{why}），"
              f"已删任务+残留文件并入黑名单，下一轮自动挑别的磁力", "warn")
    return True


def kill_hashes(P, hashes, why="手动判死"):
    """按 infohash 直接判死（给「假种清理」用）。"""
    want = {str(x).lower() for x in (hashes or []) if x}
    if not want:
        return {"dead": 0}
    rows = {_hash_of(t.get("magnet") or ""): t for t in store.list_tasks(limit=300)}
    n = 0
    for h in want:
        fid = ""
        try:
            t = P.find_task(h, max_pages=2)
            if t:
                fid = str(t.get("delete_file_id") or t.get("file_id") or "")
        except Exception:
            pass
        try:
            P.del_task(h)
        except Exception:
            pass
        if fid:
            try:
                P.delete_file(fid)
            except Exception:
                pass
        store.mark_bad(h, why)
        r = rows.get(h)
        if r:
            try:
                store.update_task(r["id"], "dead", why)
                if "订阅" in str(r.get("title") or ""):
                    store.sub_expedite(r.get("item_id"))
                    store.sub_forget_hash(r.get("item_id"), h)
                else:
                    store.mark_retry(r["item_id"])
            except Exception:
                pass
        n += 1
    store.log(f"已判死 {n} 个磁力，任务与残留文件已清理（{why}）", "warn")
    return {"dead": n}





_quota = {"at": 0, "free": None, "warned": 0, "detail": {}}


def _free_gb(P, ttl=1800):
    """可用空间（GB）= min(离线配额剩余, 网盘剩余)，查一次缓存半小时。

    115 有两个「空间」，别混：
      · 离线配额（quota()，这里 1500G）：加磁力后先下到这里，满了直接推不动
      · 网盘空间（space()，这里 116T）：下完转存到这里，满了转存会失败
    两个都得够，取小的更保险。以前写成 total - quota，把 1365G 剩余算成 135G，
    保护线一设 200G 就明明有空间也不推片。
    """
    now = time.time()
    if _quota["free"] is not None and now - _quota["at"] < ttl:
        return _quota["free"]
    cand, detail = [], {}
    try:
        d = (P.quota() or {}).get("data") or {}
        total = float(d.get("total") or 0)
        left = float(d.get("quota") or 0)
        # 老接口有把 quota 当已用返回的版本：超过 total 说明口径反了，减一下兜底
        if total > 0 and left > total:
            left = total - left
        if total > 0 and left >= 0:
            cand.append(left)
            detail["offline"] = round(left, 1)
    except Exception:
        pass
    try:
        sp = P.space() or {}
        if sp.get("ok") and sp.get("free") is not None:
            gb = float(sp["free"]) / (1024 ** 3)
            cand.append(gb)
            detail["disk"] = round(gb, 1)
    except Exception:
        pass
    if cand:
        _quota.update(at=now, free=min(cand), detail=detail)
        return _quota["free"]
    return None


def _save_path(cfg, cat):
    """按栏目取 115 保存目录：该栏目单独配了就用它，否则用默认目录。

    电视剧和电影混在一个目录里，后面自己找片、洗版都会乱，
    所以这里必须按栏目区分。
    """
    p = cfg.get("pan") or {}
    per = (p.get("paths") or {}).get(cat or "") or {}
    return per.get("id") or p.get("save_path_id") or ""


def sync_once(verbose=True, full=None, backfill=None):
    """抓取各栏目列表，入库新条目，并按规则自动推送。

    full=None  → 用设置里的 full_crawl 决定
    full=True  → 整站抓取（每栏目翻到站点总页数）
    full=False → 只翻前几页做增量
    backfill   → 纯补历史：不改变任何条目的“新上架”标记（整站抓取默认开启）
    """
    if not _sync_lock.acquire(blocking=False):
        return {"ok": False, "msg": "已有同步任务在运行"}
    _state.update({"running": True, "phase": "准备", "page": 0, "pages": 0,
                   "cat": "", "found": 0})
    t0 = time.time()
    result = {"ok": True, "new": 0, "pushed": 0, "ended": 0, "cats": {}, "msg": ""}
    try:
        cfg = store.settings()
        auto = cfg["auto"]
        sc = site.SiteClient()
        enabled = [c for c in cfg["categories"] if c.get("on")]
        if not enabled:
            result["msg"] = "没有启用任何栏目"
            return result

        # 没登录站点就别去抓了：否则三个栏目各报一条「登录失效」，红字堆一屏。
        # 但光看 cookie 名可能误判（站点改过 cookie 名），所以判死之前再实抓一次首页确认。
        if not sc.logged_in():
            chk = sc.check_login(ttl=0)
            if not chk.get("ok"):
                result.update(ok=False, msg="还没登录站点，先去「站点账号」登录")
                _notify_nologin()
                return result
            store.log("登录 cookie 名和预期不一致，但站点实际可用，继续抓取", "warn")

        do_full = bool(auto.get("full_crawl")) if full is None else bool(full)
        if backfill is None:
            backfill = do_full
        pages = int(auto.get("max_pages") or 50) if do_full else max(1, int(auto.get("pages") or 3))
        _state["full"] = do_full
        # 首次运行 = 建立基线：库里已有的一切都算历史，永不自动推送
        baseline = not store.get_kv("baseline_done")
        base_ts = int(time.time())

        for cat in enabled:
            slug = cat["slug"]
            _state.update({"cat": cat["name"], "page": 0, "pages": pages,
                           "phase": f"{cat['name']} 抓取中"})

            def on_page(pg, total, n, acc, _name=cat["name"]):
                _state.update({"page": pg, "pages": total, "found": acc,
                               "phase": f"{_name} 第 {pg}/{total} 页"})

            try:
                items = sc.listing_pages(slug, pages=pages, on_page=on_page)
            except site.SiteError as e:
                msg = "登录失效" if str(e) == "NEED_LOGIN" else str(e)
                result["cats"][slug] = f"失败: {msg}"
                store.log(f"[{cat['name']}] 抓取失败: {msg}", "error")
                continue
            except Exception as e:
                result["cats"][slug] = f"异常: {e}"
                store.log(f"[{cat['name']}] 抓取异常: {e}", "error")
                continue

            fresh = 0
            ended = 0
            # 站点列表按“最新在前”，真正决定展示顺序的是 pos（第几页第几条），
            # 由 site.listing_page 一并解析出来；这里补上「哪一轮看到的」。
            # 关键：只有第 1 页（最新 48 条）里“以前没见过”的条目才算新上架，
            # 整站补抓的 2~50 页全是历史，永远不会被自动推送。
            NEW_ZONE = site.PAGE_SIZE
            for idx, it in enumerate(items):
                it["ts"] = base_ts
                it["pround"] = base_ts
                is_new = (not baseline) and (not backfill) and idx < NEW_ZONE
                # 完结补推：老条目从「更新至 N 集」变成「全 N 集」时补推一次。
                # 建基线那一轮不算（那时库里全是历史，补推等于重推整个库）。
                kind = store.upsert_item(it, mark_new=is_new, detect_finish=not baseline)
                if kind == "new":
                    fresh += 1
                elif kind == "fin":
                    ended += 1
                    store.log(f"[{cat['name']}] 已完结，加入补推："
                              f"{str(it.get('title') or '')[:40]}（{it.get('stat')}）", "ok")
            result["new"] += fresh
            result["ended"] = int(result.get("ended") or 0) + ended
            result["cats"][slug] = (f"{len(items)} 条 / 新增 {fresh}"
                                    + (f" / 完结 {ended}" if ended else ""))
            if verbose:
                store.log(f"[{cat['name']}] 抓取 {len(items)} 条，新增 {fresh} 条")

        if baseline:
            n = store.mark_baseline()
            store.log(f"已建立基线：现有 {n} 条全部记为历史，只有此后新上架的才会自动推送", "ok")
            result["baseline"] = n
        else:
            # 幂等收尾：任何“非新上架且未推送”的条目一律归为历史。
            # 这样整站补抓、抓取中断、旧版本遗留都不会留下“卡在待推”的死数据。
            n = store.mark_history()
            if n:
                store.log(f"{n} 条老片记为历史（只有新上架的才会自动推送）", "info")

        # 推送出去的 115 任务要收尾：下完的标 done，卡死的删任务+拉黑+回队。
        # 必须放在下一轮推送之前——否则残缺文件一直留在落地目录里，越堆越多。
        try:
            P0 = pan.Pan115()
            if P0.logged_in():
                rr = _reap_tasks(P0)
                if rr.get("dead"):
                    result["reaped"] = rr
        except Exception:
            store.log("115 任务收尾检查异常：" + traceback.format_exc()[-300:], "warn")

        # 自动推送
        if auto.get("enabled") and not auto.get("paused"):
            result.update(_auto_push(sc, cfg, result))
            if result.get("rate_limited"):
                result["msg"] = "115 提示操作频繁，本轮提前收工，下一轮自动继续"
        elif auto.get("paused"):
            store.log("已暂停自动推送：本轮只更新列表，不往 115 推送", "info")
            result["msg"] = "已暂停自动推送"
            result["paused"] = True
        result["msg"] = result.get("msg") or "完成"
        return result
    except Exception as e:
        store.log("同步异常: " + traceback.format_exc()[-800:], "error")
        result.update({"ok": False, "msg": str(e)})
        return result
    finally:
        _state["running"] = False
        _state["last_run"] = int(time.time())
        _state["last_msg"] = result.get("msg", "")
        _state["duration"] = round(time.time() - t0, 1)
        _state["phase"] = ""
        _sync_lock.release()


def _auto_push(sc, cfg, result):
    auto = cfg["auto"]
    if auto.get("paused"):
        return {"pushed": 0}
    # 队列空的时候直接收工：下面 _sec_pool 会打一次 115 任务列表，
    # 每 15 分钟白打一次既浪费也会累积风控暴露面。
    try:
        stt = store.stats()
        if int(stt.get("fresh") or 0) <= 0:
            _note_blocked(int(stt.get("blocked") or 0), auto.get("min_score") or 0)
            return {"pushed": 0}
    except Exception:
        pass
    P = pan.Pan115()
    if not P.logged_in():
        store.log("115 未登录，跳过自动推送", "warn")
        return {"pushed": 0}
    # 空间保护：115 只有 1.5T，几部合集剧就能吃掉几十 G。
    # 剩得太少还继续推，最后是下载失败连环刷屏，不如提前收手。
    keep = float(auto.get("min_free_gb") or 0)
    if keep > 0:
        free = _free_gb(P)
        if free is not None and free < keep:
            if time.time() - float(_quota.get("warned") or 0) > 6 * 3600:
                _quota["warned"] = time.time()
                dt = _quota.get("detail") or {}
                bits = []
                if "offline" in dt:
                    bits.append(f"离线配额 {dt['offline']:.0f}G")
                if "disk" in dt:
                    bits.append(f"网盘 {dt['disk']:.0f}G")
                store.log(f"115 空间不足：{' · '.join(bits) or f'剩 {free:.0f}G'}"
                          f"（保护线 {keep:.0f}G），本轮不推新片", "warn")
            return {"pushed": 0, "low_space": True}
    budget = int(auto.get("max_push_per_run") or 8)
    # 每日额度：补推历史或刚建库时，一轮 8 条、15 分钟一轮也能推掉几百条，
    # 115 会直接风控。这里按自然日封顶，剩下的明天继续。
    cap = int(auto.get("max_push_per_day") or 0)
    if cap:
        done_today = store.pushed_today()
        if done_today >= cap:
            store.log(f"今天已推送 {done_today} 条，达到每日上限 {cap}，明天继续", "info")
            return {"pushed": 0, "daily_cap": cap}
        budget = min(budget, cap - done_today)
    pool = _sec_pool(P) if auto.get("prefer_sec", True) else set()
    pushed = 0
    for cat in [c for c in cfg["categories"] if c.get("on")]:
        if pushed >= budget:
            break
        try:
            pending = store.new_items(cat["slug"], limit=budget - pushed)
        except Exception:
            continue
        for it in pending:
            if pushed >= budget:
                break
            if _stop.is_set() or store.settings()["auto"].get("paused"):
                return {"pushed": pushed}
            # 预告片只有几十秒花絮，站点上「预告」状态时通常连种都没有。
            # 以前每轮都去抓详情、落空 3 次才判死，白占每日额度还把队列堵住；
            # 现在直接归历史，等它变成「全 N 集」会走完结补推。
            if "预告" in str(it.get("stat") or ""):
                store.mark_history_item(it["id"])
                store.log(f"预告片先不推：{it['title']}（正式上线后会自动补推）", "info")
                continue
            try:
                info = sc.detail(it["url"], it["cat"])
            except site.SiteError as e:
                if str(e) == "NEED_LOGIN":
                    store.log("站点登录失效，停止自动推送", "error")
                    return {"pushed": pushed}
                store.log(f"获取详情失败 {it['title']}: {e}", "warn")
                continue
            except Exception as e:
                store.log(f"获取详情异常 {it['title']}: {e}", "warn")
                continue

            magnets = info.get("magnets") or []
            if not magnets:
                # 站点的网盘资源常常比磁力活得久：没有磁力时先看网盘
                pr = _pan_auto(it, info, P, cfg)
                if pr:
                    if pr.get("action") == "push":
                        store.log(f"☁️ 网盘转存: {it['title']} —— {pr.get('msg')}", "ok")
                        pushed += 1
                    else:
                        store.log(f"☁️ 网盘已转过: {it['title']}（不重复）", "info")
                    continue
                # 站点经常「先上架、过会儿才补种」，一次抓不到磁力就判死活，
                # 等于永远推不出去。先记落空次数，下一轮再来，三次还没有才放弃。
                miss = store.bump_miss(it["id"])
                if miss >= 3:
                    store.mark_failed(it["id"])
                    store.log(f"未找到磁力: {it['title']}（第 {miss} 次，放弃）", "warn")
                else:
                    store.log(f"还没磁力: {it['title']}（第 {miss} 次，下一轮再看）", "info")
                continue
            best = _pick_best(magnets, cfg, pool)
            if not best:
                # 磁力全在死种黑名单 / 被规则过滤 → 换网盘
                pr = _pan_auto(it, info, P, cfg)
                if pr:
                    if pr.get("action") == "push":
                        store.log(f"☁️ 网盘转存: {it['title']} —— {pr.get('msg')}", "ok")
                        pushed += 1
                    else:
                        store.log(f"☁️ 网盘已转过: {it['title']}（不重复）", "info")
                    continue
                store.log(f"磁力均被大小规则过滤: {it['title']}", "warn")
                store.mark_failed(it["id"])
                continue
            if store.already_pushed(best["btih"]):
                store.mark_pushed(it["id"], best.get("size_text") or "", best["btih"])
                continue

            res = P.add_magnet(best["url"], _save_path(cfg, it["cat"]))
            size_txt = best.get("size_text") or site.human_size(best.get("size_bytes"))
            qi = _quality(best)
            qs = qi.get("q") or 0
            # 日志里带上分辨率，一眼能看出这一条挑的是 4K 还是 1080p
            rtag = "" if qi.get("res") in ("", "未知", None) else f" · {qi['res']}"
            if res.get("ok"):
                sec = _probe_sec(P, best["btih"]) if auto.get("prefer_sec", True) else None
                store.mark_pushed(it["id"], size_txt, best["btih"],
                                  best.get("size_bytes") or 0, qs)
                store.mark_pushed_global(best["btih"], it["id"], it["title"])
                store.add_task(it["id"], it["title"], best["url"], size_txt,
                               best.get("size_bytes") or 0, "sent",
                               "已提交 115 · 秒传" if sec else "已提交 115")
                store.log(f"✅ 已推送: {it['title']} [{size_txt}{rtag}]"
                          + ("　⚡ 秒传" if sec else ""), "ok")
                pushed += 1
                if sec:
                    pool.add(str(best["btih"]).lower())
            else:
                store.add_task(it["id"], it["title"], best["url"], size_txt,
                               best.get("size_bytes") or 0, "failed", res.get("msg", ""))
                store.mark_failed(it["id"], size_txt)
                store.log(f"❌ 推送失败 {it['title']}: {res.get('msg')}", "error")
                if res.get("rate_limited") or "登录态失效" in str(res.get("msg")):
                    # 被限流就立刻收手，别把冷却期里剩下的条目全判成失败
                    return {"pushed": pushed, "rate_limited": bool(res.get("rate_limited"))}
            # 间隔加抖动：固定节奏的请求最容易被识别成脚本
            time.sleep(max(0.8, float(auto.get("delay") or 1.5)) * random.uniform(0.9, 1.6))
    return {"pushed": pushed}


def push_single(item_id, magnet_url=None, fallback=True):
    """手动把某个条目推送到 115（选最大磁力）。
    fallback=False 时纯磁力：死种/没磁力直接失败，不自动改走网盘（给「手动选路线」用）。
    """
    it = store.get_item(item_id)
    if not it:
        return {"ok": False, "msg": "条目不存在"}
    cfg = store.settings()
    sc = site.SiteClient()
    try:
        info = sc.detail(it["url"], it["cat"])
    except site.SiteError as e:
        return {"ok": False, "msg": "站点登录失效" if str(e) == "NEED_LOGIN" else str(e)}
    magnets = info.get("magnets") or []
    P = pan.Pan115()
    if not magnets:
        if fallback:
            pr = _pan_auto(it, info, P, cfg)
            if pr:
                store.log(f"☁️ 网盘转存: {it['title']} —— {pr.get('msg')}", "ok")
                return {"ok": True, "msg": pr.get("msg") or "已转存", "pan": True}
        return {"ok": False, "msg": "该条目未找到磁力链接，可点「115 转」试试"}
    if magnet_url:
        best = next((m for m in magnets if m["url"] == magnet_url or m["btih"] == magnet_url), None)
        if not best:
            return {"ok": False, "msg": "指定磁力不在列表中"}
    else:
        pool = _sec_pool(P) if cfg["auto"].get("prefer_sec", True) else set()
        best = _pick_best(magnets, cfg, pool)
        if not best:
            if fallback:
                # 磁力全是死种 / 被规则过滤 → 手动推送也走网盘兜底
                pr = _pan_auto(it, info, P, cfg)
                if pr:
                    store.log(f"☁️ 网盘转存: {it['title']} —— {pr.get('msg')}", "ok")
                    return {"ok": True, "msg": pr.get("msg") or "已转存", "pan": True}
            best = magnets[0]
    res = P.add_magnet(best["url"], _save_path(cfg, it["cat"]))
    size_txt = best.get("size_text") or site.human_size(best.get("size_bytes"))
    qi = _quality(best)
    qs = qi.get("q") or 0
    rtag = "" if qi.get("res") in ("", "未知", None) else f" · {qi['res']}"
    if res.get("ok"):
        sec = _probe_sec(P, best["btih"]) if cfg["auto"].get("prefer_sec", True) else None
        store.mark_pushed(it["id"], size_txt, best["btih"],
                          best.get("size_bytes") or 0, qs)
        store.mark_pushed_global(best["btih"], it["id"], it["title"])
        store.add_task(it["id"], it["title"], best["url"], size_txt, best.get("size_bytes") or 0,
                       "sent", "手动推送" + (" · 秒传" if sec else ""))
        store.log(f"✅ 手动推送: {it['title']} [{size_txt}{rtag}]"
                  + ("　⚡ 秒传" if sec else ""), "ok")
        return {"ok": True, "msg": "已提交到 115" + ("（秒传）" if sec else ""),
                "size": size_txt, "sec": bool(sec), "q": qs,
                "res": "" if qi.get("res") == "未知" else qi.get("res")}
    store.add_task(it["id"], it["title"], best["url"], size_txt, best.get("size_bytes") or 0,
                   "failed", res.get("msg", ""))
    if fallback and not res.get("rate_limited"):
        pr = _pan_auto(it, info, P, cfg)
        if pr:
            store.log(f"☁️ 网盘转存: {it['title']} —— {pr.get('msg')}", "ok")
            return {"ok": True, "msg": pr.get("msg") or "已转存", "pan": True}
    return {"ok": False, "msg": res.get("msg", "失败")}


# ---------- 洗版：把 115 上的低质量资源换成分辨率/片源更好的 ----------
# 判定“更好”的优先级：分辨率 > 片源 > 编码/HDR 加成，同分才比体积。
# 洗版是纯手动的：先扫描列出候选，用户确认后才真的删旧 + 下新。

_wash = {"running": False, "phase": "", "done": 0, "total": 0, "found": 0,
         "last": None, "started": 0}


def wash_state():
    s = {k: v for k, v in _wash.items() if k != "last"}
    return s


def wash_last():
    return _wash.get("last") or {}


def start_scan(limit=40, offset=0, cat=None, min_gain=1, only_id=None):
    """后台跑扫描，前端轮询 /api/wash/state，结束后取 /api/wash/last。
    条数多时（站点限流，约 2 秒/条）不会把 HTTP 请求拖死。"""
    if _wash["running"]:
        return {"ok": False, "msg": "正在扫描中，请稍候"}
    _wash.update({"running": True, "phase": "准备", "done": 0, "total": 0,
                  "found": 0, "last": None, "started": int(time.time())})

    def _run():
        try:
            res = scan_wash(limit=limit, offset=offset, cat=cat,
                            min_gain=min_gain, only_id=only_id)
            _wash["last"] = res
        except Exception as e:
            store.log("洗版扫描异常: " + str(e)[:200], "error")
            _wash["last"] = {"ok": False, "msg": str(e), "candidates": []}
        finally:
            _wash["running"] = False
            _wash["phase"] = ""

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "started": True, "msg": "已开始扫描"}


def _tier(q):
    """把质量分压成“档位”：分辨率×10 + 片源。同档才允许比体积。"""
    return int(q) // 1000


def _wash_better(cur_q, cur_bytes, new_q, new_bytes, min_gain=1):
    """新资源是否真的更好。

    - 档位更高（分辨率或片源升级）→ 换
    - 档位相同 → 只认可“体积明显更大”（码率更高），编码/HDR 这类小加分不算数，
      否则会把 15G 的 2160p 换成 2.4G 的重编码，越换越糊。
    - 档位更低 → 绝不换
    min_gain=0 宽松（同档略大即可）；=1 默认（大 15%）；>=10000 严格（必须升档）

    同分辨率只换片源时（比如 2160p HDTV → 2160p WEB-DL），片源分高不代表画质好：
    站点上常见 3.4G 的 HDTV 换成 900M 的 WEB 重编码，体积掉一大截，实际更糊。
    所以这种情况仍要求新文件不低于旧体积的 60%。
    """
    ct, nt = _tier(cur_q), _tier(new_q)
    # 分辨率真的升了（1080p→2160p）才算“画质升级”，才允许体积变小
    res_up = int(new_q) // 10000 > int(cur_q) // 10000

    def too_small():
        return bool(cur_bytes and new_bytes and not res_up
                    and new_bytes < cur_bytes * 0.6)

    if min_gain >= 10000:
        return nt > ct and not too_small()
    if nt > ct:
        return not too_small()
    if nt < ct:
        return False
    if not new_bytes:
        return False
    if not cur_bytes:
        return True
    return new_bytes > cur_bytes * (1.02 if min_gain <= 0 else 1.15)


def scan_wash(limit=40, offset=0, cat=None, min_gain=1, only_id=None):
    """扫描已推送的条目，找出站点上有更好版本的。不发任何写操作。"""
    if not _wash["running"]:
        _wash.update({"running": True, "phase": "扫描中", "done": 0, "total": 0, "found": 0})
    items, total = store.pushed_items(limit=limit, offset=offset, cat=cat)
    _wash["total"] = len(items)
    if only_id:
        one = store.get_item(only_id)
        items = [one] if one else []
    out = []
    sc = site.SiteClient()
    try:
        for n, it in enumerate(items):
            _wash.update({"done": n + 1, "phase": f"检查 {it['title'][:18]}"})
            if n and n % 15 == 0:
                sc.rotate_session()
                time.sleep(1.2)
            try:
                info = sc.detail_safe(it["url"], it["cat"])
            except Exception as e:
                out.append({"id": it["id"], "title": it["title"], "cat": it["cat"],
                            "poster": it.get("poster"), "err": str(e)[:80]})
                continue
            magnets = info.get("magnets") or []
            if not magnets:
                continue
            best = site.pick_best_quality(magnets)
            if not best:
                continue
            nb = int(best.get("size_bytes") or 0)
            nq = (best.get("quality") or {}).get("q", 0)
            # 当前 115 上这条的版本
            cur = next((m for m in magnets if m["btih"] == (it.get("best_btih") or "")), None)
            if cur:
                cq = site.quality_of(cur.get("label") or "").get("q", 0)
                cb = int(cur.get("size_bytes") or 0)
                cur_txt = site.quality_of(cur.get("label") or "").get("text", "")
            else:
                # 老数据没存质量分：用体积反推，避免拿不准就报“可洗版”
                cq = int(it.get("qscore") or 0)
                cb = int(it.get("best_bytes") or 0)
                cur_txt = "已推送"
                if cq <= 0:
                    # 当前版本质量未知 → 当作同档处理，只按体积判断，绝不因“新片分辨率高”就误报
                    cq = nq
            if cur and best["btih"] == cur["btih"]:
                continue
            if not _wash_better(cq, cb, nq, nb, min_gain):
                continue
            tier_up = _tier(nq) > _tier(cq)
            out.append({
                "id": it["id"], "title": it["title"], "cat": it["cat"],
                "poster": it.get("poster"), "sub": it.get("sub"),
                "cur": {"btih": it.get("best_btih"), "size": it.get("best_size") or site.human_size(cb),
                        "bytes": cb, "q": cq, "text": cur_txt},
                "new": {"btih": best["btih"], "size": best.get("size_text") or site.human_size(nb),
                        "bytes": nb, "q": nq, "text": (best.get("quality") or {}).get("text", ""),
                        "url": best["url"], "label": best.get("label")},
                "gain": nq - cq,
                "gtype": "tier" if tier_up else "size",
            })
            _wash["found"] = len(out)
        return {"ok": True, "candidates": out, "scanned": len(items),
                "pushed_total": total, "offset": offset}
    finally:
        _wash.update({"running": False, "phase": ""})


def wash_one(item_id, btih=None, delete_old=True):
    """执行洗版：删掉 115 上的旧文件 → 提交新磁力 → 更新库。"""
    it = store.get_item(item_id)
    if not it:
        return {"ok": False, "msg": "条目不存在"}
    old_btih = it.get("best_btih") or ""
    old_size = it.get("best_size") or ""
    old_q = it.get("qscore") or 0

    cfg = store.settings()
    sc = site.SiteClient()
    try:
        info = sc.detail_safe(it["url"], it["cat"])
    except Exception as e:
        return {"ok": False, "msg": f"取磁力失败: {e}"}
    magnets = info.get("magnets") or []
    if not magnets:
        return {"ok": False, "msg": "站点上没有可用磁力"}
    if btih:
        best = next((m for m in magnets if m["btih"] == btih), None)
        if not best:
            return {"ok": False, "msg": "指定磁力已不在列表中"}
        best["quality"] = site.quality_of(best.get("label") or "")
    else:
        best = site.pick_best_quality(magnets)
    new_size = best.get("size_text") or site.human_size(best.get("size_bytes"))
    new_q = (best.get("quality") or {}).get("q", 0)
    new_qtext = (best.get("quality") or {}).get("text", "")

    P = pan.Pan115()
    if not P.logged_in():
        return {"ok": False, "msg": "115 未登录"}

    # 先提交新磁力，确认「这个种真的能下」之后再删旧文件。
    # 旧版是先删后提交：站点上一堆死种，一旦新种下不动，旧片已经进回收站，
    # 等于洗版把片子洗没了（Santo António 就是这么丢的）。
    res = P.add_magnet(best["url"], _save_path(cfg, it["cat"]))
    if not res.get("ok"):
        store.add_wash(it["id"], it["title"], old_btih, old_size, best["btih"], new_size,
                       str(old_q), str(new_q), "failed", res.get("msg", "")[:200])
        store.log(f"❌ 洗版提交失败 {it['title']}: {res.get('msg')}", "error")
        return {"ok": False, "msg": res.get("msg", "提交失败")}

    # 秒传完成、或者任务已有体积/进度，才算「有资源」。
    # 等待中且 size=0 就是死种，这时候绝不能删旧文件。
    sec = _probe_sec(P, best["btih"], wait=4)
    alive = bool(sec)
    if not alive:
        try:
            t = P.find_task(best["btih"], max_pages=1, page_size=50)
            alive = bool(t and (int(t.get("size") or 0) > 0 or float(t.get("percentDone") or 0) > 0))
        except Exception:
            alive = True      # 查询失败不拦洗版，别把正常流程卡死

    deleted = None
    kept_old = False
    if delete_old and old_btih and old_btih != best["btih"]:
        if not alive:
            kept_old = True
            store.log(f"⚠️ 新磁力暂时没资源，旧版本已保留（避免丢片）: {it['title']}", "warn")
        else:
            task = P.find_task(old_btih)
            if task:
                fid = task.get("delete_file_id") or task.get("file_id")
                deleted = P.delete_file(fid) if fid else {"ok": False, "msg": "任务无 file_id"}
                if not deleted.get("ok"):
                    # 兜底：直接删任务并连带源文件
                    alt = P.del_task(old_btih)
                    deleted = deleted if deleted.get("ok") else {
                        "ok": alt.get("ok", False),
                        "msg": "删文件失败，已尝试删任务：" + (alt.get("msg") or "ok"),
                    }
                if not deleted.get("ok"):
                    store.log(f"⚠️ 旧文件没删掉（新版本已提交，稍后手动清一下）: {it['title']}",
                              "warn")
                time.sleep(1.0)

    store.mark_pushed(it["id"], new_size, best["btih"], best.get("size_bytes") or 0, new_q)
    store.mark_pushed_global(best["btih"], it["id"], it["title"])
    store.add_task(it["id"], it["title"] + " · 洗版", best["url"], new_size,
                   best.get("size_bytes") or 0, "sent", "洗版替换")
    wash_msg = f"{old_size} → {new_size}" + ("（旧版本暂留）" if kept_old else "")
    store.add_wash(it["id"], it["title"], old_btih, old_size, best["btih"], new_size,
                   str(old_q), str(new_q), "done", wash_msg)
    store.log(f"🔄 已洗版: {it['title']}  {wash_msg} ({new_qtext})", "ok")
    return {"ok": True, "msg": "已换新（新种暂无资源，旧版本先留着）" if kept_old else "已换新",
            "old": old_size, "new": new_size, "quality": new_qtext, "deleted": deleted,
            "kept_old": kept_old}


def _loop():
    while not _stop.is_set():
        cfg = store.settings()
        interval = max(5, int(cfg["auto"].get("interval") or 15))
        _state["next_run"] = int(time.time()) + interval * 60
        try:
            sync_once()
        except Exception:
            store.log("调度异常: " + traceback.format_exc()[-500:], "error")
        for _ in range(interval * 60):
            if _stop.is_set():
                return
            time.sleep(1)


def start():
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="sync-loop", daemon=True)
    _thread.start()


def stop():
    _stop.set()
