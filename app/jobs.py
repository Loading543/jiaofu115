"""同步与自动推送引擎。"""
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


def _pick(magnets, rule, min_gb, max_gb):
    cand = []
    for m in magnets:
        size = m.get("size_bytes") or 0
        if min_gb and size and size < min_gb * 1024 ** 3:
            continue
        if max_gb and size and size > max_gb * 1024 ** 3:
            continue
        cand.append(m)
    if not cand:
        return None
    if rule == "smallest":
        return sorted(cand, key=lambda x: (x.get("size_bytes") or 0))[0]
    if rule == "latest":
        return cand[0]
    return sorted(cand, key=lambda x: (x.get("size_bytes") or 0), reverse=True)[0]


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
    result = {"ok": True, "new": 0, "pushed": 0, "cats": {}, "msg": ""}
    try:
        cfg = store.settings()
        auto = cfg["auto"]
        sc = site.SiteClient()
        enabled = [c for c in cfg["categories"] if c.get("on")]
        if not enabled:
            result["msg"] = "没有启用任何栏目"
            return result

        # 没登录站点就别去抓了：否则三个栏目各报一条「登录失效」，红字堆一屏，
        # 刚部署的人会以为是程序坏了。只提示一次，登录成功后清掉标记。
        if not sc.logged_in():
            result.update(ok=False, msg="还没登录站点，先去「站点账号」登录")
            if not store.get_kv("nologin_notified"):
                store.set_kv("nologin_notified", 1)
                store.log("还没登录站点，已跳过抓取。去「站点账号」登录后会自动恢复", "info")
            return result

        do_full = bool(auto.get("full_crawl")) if full is None else bool(full)
        if backfill is None:
            backfill = do_full
        pages = int(auto.get("max_pages") or 50) if do_full else max(1, int(auto.get("pages") or 3))
        _state["full"] = do_full
        # 首次运行 = 建立基线：库里已有的一切都算历史，永不自动推送
        baseline = not store.get_kv("baseline_done")
        base_ts = int(time.time())

        for cat_no, cat in enumerate(enabled):
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
            # 站点列表按“最新在前”，把位次编码进 ts，跨栏目按位次交错排序
            # 关键：只有第 1 页（最新 48 条）里“以前没见过”的条目才算新上架，
            # 整站补抓的 2~50 页全是历史，永远不会被自动推送。
            NEW_ZONE = 48
            for idx, it in enumerate(items):
                it["ts"] = base_ts - idx * 10 - cat_no
                is_new = (not baseline) and (not backfill) and idx < NEW_ZONE
                if store.upsert_item(it, mark_new=is_new):
                    fresh += 1
            result["new"] += fresh
            result["cats"][slug] = f"{len(items)} 条 / 新增 {fresh}"
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

        # 自动推送
        if auto.get("enabled") and not auto.get("paused"):
            result.update(_auto_push(sc, cfg, result))
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
    P = pan.Pan115()
    if not P.logged_in():
        store.log("115 未登录，跳过自动推送", "warn")
        return {"pushed": 0}
    budget = int(auto.get("max_push_per_run") or 8)
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
                store.log(f"未找到磁力: {it['title']}", "warn")
                store.mark_failed(it["id"])
                continue
            best = _pick(magnets, auto.get("pick", "largest"),
                         auto.get("min_gb") or 0, auto.get("max_gb") or 0)
            if not best:
                store.log(f"磁力均被大小规则过滤: {it['title']}", "warn")
                store.mark_failed(it["id"])
                continue
            if store.already_pushed(best["btih"]):
                store.mark_pushed(it["id"], best.get("size_text") or "", best["btih"])
                continue

            res = P.add_magnet(best["url"], cfg["pan"].get("save_path_id"))
            size_txt = best.get("size_text") or site.human_size(best.get("size_bytes"))
            qs = site.quality_of(best.get("label") or best.get("url") or "")["q"]
            if res.get("ok"):
                store.mark_pushed(it["id"], size_txt, best["btih"],
                                  best.get("size_bytes") or 0, qs)
                store.mark_pushed_global(best["btih"], it["id"], it["title"])
                store.add_task(it["id"], it["title"], best["url"], size_txt,
                               best.get("size_bytes") or 0, "sent", "已提交 115")
                store.log(f"✅ 已推送: {it['title']} [{size_txt}]", "ok")
                pushed += 1
            else:
                store.add_task(it["id"], it["title"], best["url"], size_txt,
                               best.get("size_bytes") or 0, "failed", res.get("msg", ""))
                store.mark_failed(it["id"], size_txt)
                store.log(f"❌ 推送失败 {it['title']}: {res.get('msg')}", "error")
                if "登录态失效" in str(res.get("msg")):
                    return {"pushed": pushed}
            time.sleep(float(auto.get("delay") or 1.5))
    return {"pushed": pushed}


def push_single(item_id, magnet_url=None):
    """手动把某个条目推送到 115（选最大磁力）"""
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
    if not magnets:
        return {"ok": False, "msg": "该条目未找到磁力链接"}
    if magnet_url:
        best = next((m for m in magnets if m["url"] == magnet_url or m["btih"] == magnet_url), None)
        if not best:
            return {"ok": False, "msg": "指定磁力不在列表中"}
    else:
        best = _pick(magnets, cfg["auto"].get("pick", "largest"),
                     cfg["auto"].get("min_gb") or 0, cfg["auto"].get("max_gb") or 0)
        best = best or magnets[0]
    P = pan.Pan115()
    res = P.add_magnet(best["url"], cfg["pan"].get("save_path_id"))
    size_txt = best.get("size_text") or site.human_size(best.get("size_bytes"))
    qs = site.quality_of(best.get("label") or best.get("url") or "")["q"]
    if res.get("ok"):
        store.mark_pushed(it["id"], size_txt, best["btih"],
                          best.get("size_bytes") or 0, qs)
        store.mark_pushed_global(best["btih"], it["id"], it["title"])
        store.add_task(it["id"], it["title"], best["url"], size_txt, best.get("size_bytes") or 0, "sent", "手动推送")
        store.log(f"✅ 手动推送: {it['title']} [{size_txt}]", "ok")
        return {"ok": True, "msg": "已提交到 115", "size": size_txt}
    store.add_task(it["id"], it["title"], best["url"], size_txt, best.get("size_bytes") or 0,
                   "failed", res.get("msg", ""))
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
    """
    ct, nt = _tier(cur_q), _tier(new_q)
    if min_gain >= 10000:
        return nt > ct
    if nt > ct:
        return True
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

    deleted = None
    if delete_old and old_btih and old_btih != best["btih"]:
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
                store.add_wash(it["id"], it["title"], old_btih, old_size, best["btih"], new_size,
                               str(old_q), str(new_q), "failed", deleted.get("msg", "")[:200])
                store.log(f"❌ 洗版失败（旧文件删不掉，已中止，避免重复占用空间）: {it['title']}",
                          "error")
                return {"ok": False, "msg": "旧文件删除失败，已中止：" + str(deleted.get("msg"))}
            time.sleep(1.0)

    res = P.add_magnet(best["url"], cfg["pan"].get("save_path_id"))
    if not res.get("ok"):
        store.add_wash(it["id"], it["title"], old_btih, old_size, best["btih"], new_size,
                       str(old_q), str(new_q), "failed", res.get("msg", "")[:200])
        store.log(f"❌ 洗版提交失败 {it['title']}: {res.get('msg')}", "error")
        return {"ok": False, "msg": res.get("msg", "提交失败")}

    store.mark_pushed(it["id"], new_size, best["btih"], best.get("size_bytes") or 0, new_q)
    store.mark_pushed_global(best["btih"], it["id"], it["title"])
    store.add_task(it["id"], it["title"] + " · 洗版", best["url"], new_size,
                   best.get("size_bytes") or 0, "sent", "洗版替换")
    store.add_wash(it["id"], it["title"], old_btih, old_size, best["btih"], new_size,
                   str(old_q), str(new_q), "done",
                   f"{old_size} → {new_size}")
    store.log(f"🔄 已洗版: {it['title']}  {old_size} → {new_size} ({new_qtext})", "ok")
    return {"ok": True, "msg": "已换新", "old": old_size, "new": new_size,
            "quality": new_qtext, "deleted": deleted}


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
