/* 教父 → 115 自动离线 · 前端 */
const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

const S = {
  status: null,
  settings: null,
  cat: "all",
  qr: null,
  qrTimer: null,
  qrUid: null,
  creds: { username: "", password: "" },
  capPts: [],
  capChars: 0,
  offset: 0,
  limit: 60,
  total: 0,
  loading: false,
  dirStack: [],
  sort: "new",
  wash: [],
};

async function api(path, body) {
  const opt = body === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
  const r = await fetch(path, opt);
  try {
    return await r.json();
  } catch (e) {
    return { ok: false, msg: "响应解析失败" };
  }
}

let toastTimer = null;
function toast(msg, type = "") {
  const el = $("#toast");
  el.textContent = msg;
  el.className = "toast " + type;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), 2600);
}

function fmtSize(n) {
  n = Number(n || 0);
  if (!n) return "";
  const u = [["TB", 1099511627776], ["GB", 1073741824], ["MB", 1048576], ["KB", 1024]];
  for (const [k, v] of u) if (n >= v) return (n / v).toFixed(2) + " " + k;
  return n + " B";
}

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function tsText(sec) {
  if (!sec) return "—";
  return new Date(sec * 1000).toLocaleString("zh-CN", { hour12: false }).replace(/\//g, "-");
}

// 后端 qscore = 分辨率*10000 + 片源*1000 + 编码/HDR 加分，这里只取分辨率部分
function resOfScore(q) {
  const r = Math.floor((Number(q) || 0) / 10000);
  if (r >= 5) return "2160p";
  if (r === 4) return "1080p";
  if (r === 3) return "720p";
  if (r === 2) return "480p";
  return "";
}

// 从磁力文件名里取分辨率，跟后端 quality_of 的判定保持一致
function resOfLabel(s) {
  const t = String(s || "").toLowerCase();
  if (/2160p|2160|4k|uhd/.test(t)) return "2160p";
  if (/1080/.test(t)) return "1080p";
  if (/720/.test(t)) return "720p";
  if (/576|480/.test(t)) return "480p";
  return "";
}

// 条目的年份：列表副标题以年份开头；标题里一般写成 (2024)
function yearOfItem(it) {
  const m = String((it && it.sub) || "").match(/^\s*((?:19|20)\d{2})/);
  if (m) return m[1];
  const t = String((it && it.title) || "").match(/[\(\[（【]\s*((?:19|20)\d{2})\s*[\)\]）】]/);
  return t ? t[1] : "";
}

/* ---------------- 状态 ---------------- */
function paused() {
  return !!(S.settings && S.settings.auto && S.settings.auto.paused);
}

function paintPause() {
  const p = paused();
  const txt = p ? "▶ 恢复推送" : "⏸ 暂停推送";
  ["#btn-pause", "#btn-pause2"].forEach((sel) => {
    const b = $(sel);
    if (b) {
      b.textContent = txt;
      b.classList.toggle("danger", p);
    }
  });
  const pill = $("#pill-auto");
  if (pill) {
    pill.textContent = p ? "自动推送 · 已暂停" : "自动推送 · 运行中";
    pill.className = "pill " + (p ? "warn" : "ok");
  }
}

async function refreshStatus() {
  const r = await api("/api/status");
  if (!r.ok) return;
  S.status = r;
  S.settings = r.settings;
  const ps = $("#pill-site"), pp = $("#pill-pan");
  ps.textContent = r.site.logged ? "站点 · 已登录" : "站点 · 未登录";
  ps.className = "pill " + (r.site.logged ? "ok" : "bad");
  pp.textContent = r.pan.logged ? "115 · 已登录" : "115 · 未登录";
  pp.className = "pill " + (r.pan.logged ? "ok" : "bad");
  $("#st-items").textContent = r.stats.items;
  $("#st-fresh").textContent = r.stats.fresh;
  const sy = r.sync || {};
  const elSync = $("#side-sync");
  if (sy.running) {
    elSync.textContent = sy.phase || "同步中…";
    elSync.className = "side-hint";
  } else if (sy.last_run) {
    // 带上日期：只显示「19:44」的话，昨天甚至前天停摆都看不出来
    const t = new Date(sy.last_run * 1000);
    const stamp = `${t.getMonth() + 1}-${t.getDate()} `
      + `${String(t.getHours()).padStart(2, "0")}:${String(t.getMinutes()).padStart(2, "0")}`;
    const msg = (sy.last_msg || "").trim();
    const bad = !!msg && !/完成|已暂停/.test(msg);
    elSync.textContent = bad ? `同步异常 · ${msg}` : `上次同步 ${stamp}`;
    elSync.className = "side-hint" + (bad ? " warn" : "");
  } else {
    elSync.textContent = "尚未同步";
    elSync.className = "side-hint";
  }
  paintPause();
  renderCatControls();
  paintSavePath();
  // 手动转存任务进度（后端随 /api/status 一起返回，兜底刷新）
  if (r.conv !== undefined) paintConv(r.conv);
}

async function togglePause() {
  const next = !paused();
  const r = await api("/api/push/pause", { paused: next });
  if (!r.ok) return toast("操作失败", "err");
  S.settings.auto = r.auto;
  paintPause();
  toast(next ? "已暂停自动推送" : "已恢复自动推送", next ? "" : "ok");
}

/* ---------------- 分类 ---------------- */
function renderCatControls() {
  const cats = (S.settings && S.settings.categories) || [];
  const chips = [{ slug: "all", name: "全部" }, ...cats];
  $("#cat-chips").innerHTML = chips
    .map((c) => `<div class="chip ${S.cat === c.slug ? "active" : ""}" data-cat="${esc(c.slug)}">${esc(c.name)}</div>`)
    .join("");
  $("#cat-switches").innerHTML = cats
    .map(
      (c) => `<label class="mini-switch"><span class="switch" style="width:32px;height:18px;flex:0 0 32px">
        <input type="checkbox" data-toggle="${esc(c.slug)}" ${c.on ? "checked" : ""}>
        <span></span></span>${esc(c.name)}</label>`
    )
    .join("");
}

/* ---------------- 条目渲染 ---------------- */
function posterOf(it) {
  if (it.poster) return it.poster;
  const iid = String(it.id || "").split(":")[1];
  const cat = it.cat || "";
  return iid && cat ? `https://s.tutu.pm/img/${cat}/${iid}/256.webp` : "";
}

function itemCard(it) {
  const url = posterOf(it);
  const ph = esc((it.title || "?").slice(0, 1));
  const img = url
    ? `<img src="${esc(url)}" loading="lazy" referrerpolicy="no-referrer" alt=""
         onerror="this.style.display='none'">`
    : "";
  let badge;
  // 完结补推的老条目也是待推状态，但标「新上架」会误导 —— 用基线时间区分：
  // 基线之前就存在、现在才被补推的，标成「完结补推」
  const base = (S.status && S.status.baseline_at) || 0;
  const bumped = it.isnew == 1 && base && (it.first_seen || 0) < base;
  if (it.pushed == 1) badge = `<div class="badge done">已推送</div>`;
  else if (it.pushed == 2) badge = `<div class="badge fail">推送失败</div>`;
  else if (it.pushed == 3) badge = `<div class="badge old">历史</div>`;
  else if (it.isnew == 1) badge = bumped
    ? `<div class="badge new">完结补推</div>`
    : `<div class="badge new">新上架</div>`;
  else badge = `<div class="badge old">历史</div>`;
  const size = it.best_size ? `<div class="badge size">${esc(it.best_size)}</div>` : "";
  // 更新状态（全24集 / 更新至12集）拼在副标题最前面，一眼能看出进度
  const meta = [resOfScore(it.qscore), it.stat, it.sub].filter(Boolean).join(" · ");
  const washBtn = it.pushed == 1
    ? `<button class="btn wash small" data-wash="${esc(it.id)}" title="搜索更高画质的版本替换">洗版</button>` : "";
  const subBtn = `<button class="btn ghost small" data-subfrom="${esc(it.title)}"
    data-subcat="${esc(it.cat || "")}" data-subyear="${esc(yearOfItem(it))}"
    data-submeta="${esc(String(it.sub || "").slice(0, 60))}"
    title="订阅：没片源也会等，有源自动推，出更高画质自动洗版">订阅</button>`;
  return `<div class="item" data-id="${esc(it.id)}">
    <div class="poster"><span class="ph">${ph}</span>${img}${badge}${size}</div>
    <div class="item-body">
      <div class="item-title" title="${esc(it.title)}">${esc(it.title)}</div>
      <div class="item-sub">${esc(meta)}</div>
      <div class="acts">
        <button class="btn small" data-convert="magnet" data-id="${esc(it.id)}" title="只用磁力离线到 115">磁力转</button>
        <button class="btn small" data-convert="pan115" data-id="${esc(it.id)}" title="用站点的 115 网盘分享转存（秒级）">115转</button>
        <button class="btn ghost small" data-detail="${esc(it.id)}" title="选具体磁力 / 手动转存">选集</button>
        ${subBtn}
        ${washBtn}
      </div>
    </div>
  </div>`;
}

async function loadLatest(reset = true) {
  if (S.loading) return;
  S.loading = true;
  const grid = $("#grid-latest");
  if (reset) {
    S.offset = 0;
    grid.innerHTML = `<div class="empty">加载中…</div>`;
  }
  const more = $("#btn-more");
  if (more) more.disabled = true;
  const r = await api(`/api/items?limit=${S.limit}&offset=${S.offset}&cat=${encodeURIComponent(S.cat)}&sort=${encodeURIComponent(S.sort || "new")}`);
  S.loading = false;
  if (more) more.disabled = false;
  if (!r.ok) {
    if (reset) grid.innerHTML = `<div class="empty">加载失败</div>`;
    return;
  }
  S.total = r.total || 0;
  const html = (r.items || []).map(itemCard).join("");
  if (reset) grid.innerHTML = html || `<div class="empty">还没有数据，点右上角「抓取全站」或「立即同步」</div>`;
  else grid.insertAdjacentHTML("beforeend", html);
  S.offset += (r.items || []).length;
  const bc = r.by_cat || {};
  $("#latest-count").textContent =
    `共 ${S.total} 条（电影 ${bc.mv || 0} · 电视剧 ${bc.tv || 0} · 动漫 ${bc.ac || 0}）· 已显示 ${S.offset}`;
  const btn = $("#btn-more");
  if (btn) btn.classList.toggle("hidden", S.offset >= S.total);
}

async function pushItem(id, btn, magnet) {
  if (btn) {
    btn.disabled = true;
    // 存 innerHTML 而不是 textContent：「秒传 115」里的 115 包在 span 里，
    // 窄屏会隐藏它，用纯文本恢复会让 span 丢掉、手机上又挤成三个字
    btn.dataset.old = btn.innerHTML;
    btn.textContent = "推送中…";
  }
  const r = await api("/api/push", { item_id: id, magnet: magnet || null });
  toast(r.ok ? "已提交到 115 · " + (r.size || "") : r.msg || "推送失败", r.ok ? "ok" : "err");
  if (btn) {
    btn.disabled = false;
    btn.innerHTML = btn.dataset.old || (magnet ? "推送这条" : '秒传<span class="btn-115"> 115</span>');
  }
  refreshStatus();
  if (!magnet) loadLatest(true);
}

/* ---------------- 手动转存：磁力 / 115网盘 ---------------- */
const CONV_LABEL = { magnet: "磁力转", pan115: "115 转" };

async function runConvert(id, mode, magnet, btn) {
  if (!id) return;
  if (btn) {
    btn.disabled = true;
    btn.dataset.old = btn.innerHTML;
    btn.textContent = "提交中…";
  }
  let r;
  try {
    r = await api("/api/convert", { item_id: id, mode: mode, magnet: magnet || null });
  } catch (e) {
    r = { ok: false, msg: "网络错误" };
  }
  if (!r || !r.ok) {
    toast((r && r.msg) || "提交失败", "err");
    if (btn) { btn.disabled = false; btn.innerHTML = btn.dataset.old || CONV_LABEL[mode]; }
    return;
  }
  const name = CONV_LABEL[mode] || "转存";
  toast(name + "：已开始", "ok");
  if (btn) btn.textContent = "进行中…";

  // 轮询后台任务状态（转存可能要几分钟）
  const tid = r.id;
  let ticks = 0;
  const label = {};
  const poll = async () => {
    let st;
    try {
      st = await api("/api/convert/status?id=" + encodeURIComponent(tid));
    } catch (e) {
      st = { ok: false, msg: "网络错误" };
    }
    if (!st.ok && !st.state) {
      toast((st.msg || "查询失败"), "err");
      if (btn) { btn.disabled = false; btn.innerHTML = btn.dataset.old || name; }
      return;
    }
    if (st.state === "done") {
      if (btn) { btn.disabled = false; btn.innerHTML = btn.dataset.old || name; }
      toast(name + "：" + (st.msg || (st.ok ? "完成" : "失败")), st.ok ? "ok" : "err");
      refreshStatus();
      loadLatest(true);
      return;
    }
    ticks += 1;
    setTimeout(poll, 1500);
  };
  setTimeout(poll, 800);
}

/* ---------------- 详情弹窗 ---------------- */
async function openDetail(id) {
  $("#modal").classList.remove("hidden");
  $("#modal-body").innerHTML = `<div class="empty">正在读取磁力…</div>`;
  const r = await api("/api/item?id=" + encodeURIComponent(id) + "&refresh=1");
  if (!r.ok) {
    $("#modal-body").innerHTML = `<div class="empty">${esc(r.msg || "读取失败")}</div>`;
    return;
  }
  const it = r.item || {};
  const mags = r.magnets || [];
  // 4K 优先排序：同分辨率才比体积，省得在一百多条里翻 2160p
  const rank = { "2160p": 5, "1080p": 4, "720p": 3, "480p": 2 };
  const sorted = mags.slice().sort((a, b) =>
    ((rank[resOfLabel(b.label)] || 1) - (rank[resOfLabel(a.label)] || 1)) ||
    ((b.size_bytes || 0) - (a.size_bytes || 0)));
  const best = sorted[0] || null;
  const n4k = sorted.filter((m) => resOfLabel(m.label) === "2160p").length;
  const tip = n4k
    ? `共 ${mags.length} 条磁力，其中 <b>${n4k}</b> 条 4K，已排在最前面`
    : `共 ${mags.length} 条磁力 —— 这条站点上<b>没有 4K</b>，最高只有 1080p`;
  const rows = sorted.length
    ? sorted
        .map(
          (m, i) => `<div class="mag ${i === 0 ? "sel" : ""}" data-mag="${esc(m.url)}">
        <div class="sz">${esc(m.size_text || fmtSize(m.size_bytes) || "未知体积")}</div>
        ${qBadge(resOfLabel(m.label))}
        <div class="nm">${esc(m.label || m.btih)}</div>
        <button class="btn small" data-magpush="${esc(m.url)}">推送</button>
      </div>`
        )
        .join("")
    : `<div class="empty">未解析到磁力链接</div>`;
  $("#modal-body").innerHTML = `
    <h2 style="margin:0 0 4px;font-size:16px">${esc(it.title || "")}</h2>
    <div class="muted">${esc(it.sub || "")} · ${tip}</div>
    <div class="convert-box">
      <div class="convert-head">手动转存 · 自己选路线</div>
      <div class="convert-acts">
        <button class="btn small" data-convert="magnet" data-id="${esc(it.id)}" data-magnet="${esc(best ? best.url : "")}">磁力转</button>
        <button class="btn small" data-convert="pan115" data-id="${esc(it.id)}">115 转</button>
      </div>
      <div class="muted small">磁力转：离线种子到 115；115 转：站点网盘秒级复制。每条只走你选的那条路。</div>
    </div>
    <div class="mag-list">${rows}</div>
    <div class="row">
      <div class="muted">点某条磁力的「推送」＝只用那条磁力离线；不指定则按规则优先 4K</div>
      <div class="row" style="margin:0;gap:8px">
        <button class="btn ghost" id="modal-sub">订阅此片</button>
      </div>
    </div>`;
  const subBtn = $("#modal-sub");
  if (subBtn)
    subBtn.onclick = () => {
      $("#modal").classList.add("hidden");
      addSub({ kw: it.title, cat: it.cat, year: yearOfItem(it), sub: it.sub || "" }, false);
    };
  $$("[data-magpush]").forEach((b) => {
    b.onclick = async () => {
      // 点某条磁力的「推送」＝只用这条磁力走「磁力转」，不掺网盘兜底
      const url = b.getAttribute("data-magpush");
      b.disabled = true;
      const old = b.innerHTML;
      b.textContent = "转中…";
      const r = await api("/api/convert", { item_id: id, mode: "magnet", magnet: url });
      if (r.ok) {
        toast("已提交：" + (r.msg || "磁力离线"), "ok");
        $("#modal").classList.add("hidden");
        loadLatest(true);
      } else {
        toast(r.msg || "提交失败", "err");
        b.disabled = false;
        b.innerHTML = old;
      }
      refreshStatus();
    };
  });
}

/* ---------------- 搜索 ---------------- */
async function doSearch() {
  const q = $("#q").value.trim();
  const grid = $("#grid-search");
  if (!q) return;
  grid.innerHTML = `<div class="empty">搜索中…</div>`;
  const r = await api("/api/search?q=" + encodeURIComponent(q));
  if (!r.ok) {
    grid.innerHTML = `<div class="empty">${esc(r.msg || "搜索失败")}</div>`;
    return;
  }
  if (!r.items.length) {
    grid.innerHTML = `<div class="empty">没有找到相关资源</div>`;
    return;
  }
  grid.innerHTML = r.items.map(itemCard).join("");
}

/* ---------------- 推送记录 ---------------- */
async function loadTasks() {
  const box = $("#task-table");
  const r = await api("/api/tasks?limit=80");
  if (!r.ok || !r.tasks.length) {
    box.innerHTML = `<div class="empty">还没有推送记录</div>`;
    return;
  }
  const head = `<div class="trow head"><div>标题</div><div>磁力</div><div>体积</div><div>状态</div><div>时间</div></div>`;
  const rows = r.tasks
    .map(
      (t) => `<div class="trow">
      <div class="t" title="${esc(t.title)}">${esc(t.title || "")}</div>
      <div class="t" title="${esc(t.magnet)}">${esc(t.magnet || "")}</div>
      <div>${esc(t.size_text || "-")}</div>
      <div><span class="tag ${t.status === "sent" ? "sent" : "failed"}">${t.status === "sent" ? "已提交" : "失败"}</span></div>
      <div class="t">${t.created ? new Date(t.created * 1000).toLocaleString("zh-CN") : ""}</div>
    </div>`
    )
    .join("");
  box.innerHTML = head + rows;
}

/* ---------------- 115 扫码 ---------------- */
function renderChannels() {
  const ch = (S.status && S.status.pan && S.status.pan.channels) || [];
  $("#channels").innerHTML = ch
    .map(
      (c) => `<button class="channel" data-app="${esc(c.app)}" data-name="${esc(c.name)}">
      <div class="ico">${c.icon || "🔗"}</div>
      <div class="nm">${esc(c.name)}</div>
      <div class="ds">${esc(c.desc || "")}</div>
    </button>`
    )
    .join("");
  $$("[data-app]").forEach((b) => {
    b.onclick = () => startQr(b.getAttribute("data-app"), b.getAttribute("data-name"), b);
  });
}

async function startQr(app, name, btn) {
  $$(".channel").forEach((x) => x.classList.remove("active"));
  if (btn) btn.classList.add("active");
  $("#qr-status").textContent = "正在生成二维码…";
  $("#qr-desc").textContent = name || "";
  const r = await api("/api/115/qr?app=" + encodeURIComponent(app));
  if (!r.ok) {
    $("#qrbox").innerHTML = `<div class="qr-empty">${esc(r.msg || "生成失败")}</div>`;
    return;
  }
  S.qr = r;
  S.qrUid = r.uid;
  try {
    const qr = qrcode(0, "M");
    qr.addData(r.qrcode);
    qr.make();
    $("#qrbox").innerHTML = qr.createImgTag(6, 4);
  } catch (e) {
    $("#qrbox").innerHTML = `<div class="qr-empty">二维码渲染失败</div>`;
  }
  $("#qr-status").textContent = "等待扫码";
  pollQr();
}

function pollQr() {
  clearInterval(S.qrTimer);
  S.qrTimer = setInterval(async () => {
    if (!S.qr) return;
    const q = `uid=${encodeURIComponent(S.qr.uid)}&time=${S.qr.time}&sign=${S.qr.sign}&app=${S.qr.app}`;
    const r = await api("/api/115/qr/status?" + q);
    if (r.logged) {
      clearInterval(S.qrTimer);
      $("#qr-status").textContent = "✅ 登录成功";
      $("#qrbox").innerHTML = `<div class="qr-empty" style="color:#16a34a;font-weight:700">已登录</div>`;
      toast("115 登录成功", "ok");
      refreshStatus();
      return;
    }
    if (r.state === 2) $("#qr-status").textContent = "已扫码，请在手机上确认";
    else if (r.state === 0 || r.state === 3) $("#qr-status").textContent = "二维码已失效，请刷新";
    else if (r.msg) $("#qr-status").textContent = "等待扫码 · " + r.msg;
  }, 2000);
}

async function loadPanInfo() {
  const P = S.status && S.status.pan;
  $("#pan-info").textContent = P && P.logged ? "已登录 115 账号" : "当前未登录 115";
  if (P && P.logged) {
    const q = await api("/api/115/quota");
    if (q.ok && q.data) {
      const d = q.data;
      const bits = [];
      // 网盘总空间（116T 那种）和离线配额（1.5T 下载临时空间）是两回事，分开写
      const sp = q.space || {};
      if (sp.ok && sp.total) {
        bits.push(`网盘 ${sp.used_txt || fmtSize(sp.used)} / ${sp.total_txt || fmtSize(sp.total)}`);
      }
      // 115 的 quota 字段就是「剩余」，不是「已用」——写清楚免得看反
      const left = d.quota != null ? d.quota : d.count;
      if (left != null) {
        bits.push(`离线配额 ${Math.round(Number(left))}`
          + (d.total != null ? " / " + Math.round(Number(d.total)) : "") + "G");
      }
      $("#pan-info").textContent = "已登录 115" + (bits.length ? " · " + bits.join(" · ") : "");
    }
  }
}

/* ---------------- 115 目录选择 ---------------- */
function paintSavePath() {
  const p = (S.settings && S.settings.pan) || {};
  const paths = p.paths || {};
  const txt = p.save_path_name
    ? `${p.save_path_name}${p.save_path_id ? "  (ID " + p.save_path_id + ")" : ""}`
    : p.save_path_id ? "已选目录（ID " + p.save_path_id + "）" : "未选择（默认云下载）";
  ["#pan-path-show", "#pan-path-show2"].forEach((sel) => {
    const el = $(sel);
    if (!el) return;
    el.textContent = txt;
    el.classList.toggle("empty", !p.save_path_id && !p.save_path_name);
  });
  [["mv", "#pan-path-mv"], ["tv", "#pan-path-tv"], ["ac", "#pan-path-ac"]].forEach(([k, sel]) => {
    const el = $(sel);
    if (!el) return;
    const it = paths[k] || {};
    el.textContent = it.name ? it.name + (it.id ? "  (ID " + it.id + ")" : "") : "跟随默认";
    el.classList.toggle("empty", !it.id && !it.name);
  });
}

const PICK_LABEL = { "": "默认目录", mv: "电影", tv: "电视剧", ac: "动漫" };

async function openDirPicker(cat) {
  S.pickCat = typeof cat === "string" ? cat : "";
  S.dirStack = [{ cid: "0", name: "根目录" }];
  $("#modal").classList.remove("hidden");
  $("#modal-body").innerHTML = `<h2 style="margin:0 0 10px;font-size:16px">选择 115 保存目录</h2>
    <div class="empty">正在读取目录…</div>`;
  await renderDirPicker("0");
}

function dirCrumbs() {
  return `<div class="crumb">当前位置：` +
    S.dirStack.map((d, i) =>
      `<button data-crumb="${i}">${esc(d.name)}</button>${i < S.dirStack.length - 1 ? " / " : ""}`
    ).join("") + `</div>`;
}

async function renderDirPicker(cid) {
  const body = $("#modal-body");
  const r = await api("/api/115/dirs?cid=" + encodeURIComponent(cid));
  const list = (r.dirs || [])
    .map(
      (d) => `<div class="dirline" data-dir="${esc(d.cid)}" data-name="${esc(d.name)}">
      <span class="ico">📁</span><span class="nm">${esc(d.name)}</span><span class="muted">进入 ›</span>
    </div>`
    )
    .join("");
  const cur = S.dirStack[S.dirStack.length - 1];
  const who = PICK_LABEL[S.pickCat || ""] || "默认目录";
  body.innerHTML = `
    <h2 style="margin:0 0 10px;font-size:16px">选择 115 保存目录 · ${esc(who)}</h2>
    ${dirCrumbs()}
    <div class="dirlist">${list || `<div class="empty">${r.ok ? "该目录下没有子目录" : esc(r.msg || "读取失败")}</div>`}</div>
    <div class="row">
      <div class="muted">选中后，${esc(who)}的推送会落到这个目录</div>
      <div style="display:flex;gap:8px">
        <button class="btn ghost small" id="dir-mkdir">新建文件夹</button>
        <button class="btn ghost small" id="dir-cancel">取消</button>
        <button class="btn" id="dir-pick">选择「${esc(cur.name)}」</button>
      </div>
    </div>`;
  $("#dir-pick").onclick = () => saveDir(cur);
  $("#dir-cancel").onclick = () => $("#modal").classList.add("hidden");
  $("#dir-mkdir").onclick = async () => {
    const nm = (prompt("新建文件夹（建在当前位置下）") || "").trim();
    if (!nm) return;
    const base = S.dirStack.slice(1).map((d) => d.name).join("/");
    const full = "/" + [base, nm].filter(Boolean).join("/");
    const r = await api("/api/115/mkdir", { path: full });
    if (!r.ok) return toast(r.msg || "新建失败", "err");
    toast(r.created && r.created.length ? "已新建：" + full : "目录已存在", "ok");
    await renderDirPicker(cur.cid);
  };
  $$("[data-dir]").forEach((el) => {
    el.onclick = async () => {
      S.dirStack.push({ cid: el.getAttribute("data-dir"), name: el.getAttribute("data-name") });
      await renderDirPicker(el.getAttribute("data-dir"));
    };
  });
  $$("[data-crumb]").forEach((el) => {
    el.onclick = async () => {
      const i = Number(el.getAttribute("data-crumb"));
      S.dirStack = S.dirStack.slice(0, i + 1);
      await renderDirPicker(S.dirStack[i].cid);
    };
  });
}

async function saveDir(cur) {
  const isRoot = cur.cid === "0";
  const name = S.dirStack.map((d) => d.name).join(" / ");
  const cat = S.pickCat || "";
  const r = await api("/api/115/savepath", {
    id: isRoot ? "" : cur.cid,
    name: isRoot ? "" : name,
    cat,
  });
  if (!r.ok) return toast("保存失败", "err");
  S.settings.pan = r.pan;
  paintSavePath();
  $("#modal").classList.add("hidden");
  const who = PICK_LABEL[cat] || "默认目录";
  toast(isRoot ? `${who}已恢复为跟随默认` : `${who}保存目录已设为：` + name, "ok");
}

/* ---------------- 站点登录 / 验证码 ---------------- */
async function siteLogin() {
  const username = $("#site-user").value.trim();
  const password = $("#site-pass").value;
  const msg = $("#site-msg");
  if (!username || !password) {
    msg.className = "msg err";
    msg.textContent = "请填写账号和密码";
    return;
  }
  S.creds = { username, password };
  msg.className = "msg";
  msg.textContent = "登录中…";
  const r = await api("/api/site/login", { username, password, code: "" });
  if (r.ok) {
    msg.className = "msg ok";
    msg.textContent = "登录成功";
    toast("站点登录成功", "ok");
    refreshStatus();
    return;
  }
  if (r.need_captcha) {
    msg.className = "msg";
    msg.textContent = "需要点选验证码";
    showCaptcha(r.captcha);
    return;
  }
  msg.className = "msg err";
  msg.textContent = r.msg || "登录失败";
}

function showCaptcha(cap) {
  S.capPts = [];
  S.capChars = (cap.chars || []).length;
  $("#modal").classList.remove("hidden");
  $("#modal-body").innerHTML = `
    <h2 style="margin:0 0 6px;font-size:16px">点选验证码</h2>
    <div class="muted">请按顺序点击图中提示的字符</div>
    <div class="cap-chars" id="cap-chars">
      <span>请依次点击：</span>
      ${(cap.chars || []).map((c) => `<img src="data:image/png;base64,${c}">`).join("、")}
    </div>
    <div class="cap-img" id="cap-img"><img src="${cap.img}"></div>
    <div class="msg" id="cap-msg">已点击 0/${S.capChars}</div>`;
  const box = $("#cap-img");
  box.onclick = (e) => {
    if (S.capPts.length >= S.capChars) return;
    const rect = box.getBoundingClientRect();
    const x = Math.round(((e.clientX - rect.left) / rect.width) * 350);
    const y = Math.round(((e.clientY - rect.top) / rect.height) * 200);
    S.capPts.push(`${x},${y}`);
    const mark = document.createElement("div");
    mark.className = "cap-mark";
    mark.style.left = (e.clientX - rect.left) + "px";
    mark.style.top = (e.clientY - rect.top) + "px";
    mark.textContent = S.capPts.length;
    box.appendChild(mark);
    $("#cap-msg").textContent = `已点击 ${S.capPts.length}/${S.capChars}`;
    if (S.capPts.length === S.capChars) submitCaptcha();
  };
}

async function submitCaptcha() {
  const m = $("#cap-msg");
  m.className = "msg";
  m.textContent = "校验中…";
  const r = await api("/api/site/captcha/check", { points: S.capPts });
  if (!r.ok) {
    m.className = "msg err";
    m.textContent = r.msg || "验证失败";
    setTimeout(() => {
      S.capPts = [];
      const box = $("#cap-img");
      if (box) {
        box.querySelectorAll(".cap-mark").forEach((x) => x.remove());
        $("#cap-msg").textContent = `已点击 0/${S.capChars}`;
      }
    }, 900);
    return;
  }
  m.textContent = "验证通过，正在登录…";
  const lr = await api("/api/site/login", {
    username: S.creds.username,
    password: S.creds.password,
    code: r.info,
  });
  if (lr.ok) {
    $("#modal").classList.add("hidden");
    toast("站点登录成功", "ok");
    refreshStatus();
  } else {
    m.className = "msg err";
    m.textContent = lr.msg || "登录失败";
  }
}

/* ---------------- 设置 ---------------- */
function fillSettings() {
  const s = S.settings || {};
  const a = s.auto || {};
  $("#auto-enabled").checked = a.enabled !== false;
  $("#auto-interval").value = a.interval || 15;
  $("#auto-pick").value = a.pick || "largest";
  $("#auto-min").value = a.min_gb || 0;
  $("#auto-max").value = a.max_gb || 0;
  const ms = Number(a.min_score == null ? 7 : a.min_score);
  const selScore = $("#auto-minscore");
  if (selScore) {
    // 下拉里没有的分数（接口改的自定义值）也补一个选项，别让值悄悄变回默认
    let opt = Array.from(selScore.options).find((o) => Number(o.value) === ms);
    if (!opt) {
      opt = document.createElement("option");
      opt.value = String(ms);
      opt.textContent = `≥ ${ms} 分`;
      selScore.appendChild(opt);
    }
    selScore.value = opt.value;
  }
  $("#auto-scoreunknown").checked = !!a.score_unknown;
  $("#auto-maxpush").value = a.max_push_per_run || 8;
  $("#auto-maxday").value = a.max_push_per_day == null ? 30 : a.max_push_per_day;
  $("#auto-minfree").value = a.min_free_gb == null ? 50 : a.min_free_gb;
  $("#auto-sec").checked = a.prefer_sec !== false;
  $("#auto-4k").checked = a.prefer_4k !== false;
  $("#auto-pages").value = a.pages || 3;
  const at = S.status && S.status.baseline_at;
  const st = (S.status && S.status.stats) || {};
  const gate = Number(a.min_score || 0);
  const gateTxt = gate > 0
    ? `评分门槛 <b>≥ ${gate} 分</b>（豆瓣优先，无豆瓣用 IMDB，${a.score_unknown ? "没分的也推" : "没分的不推"}${st.blocked ? `；另有 <b>${st.blocked}</b> 条评分不够没推` : ""}）。订阅和手动推不受门槛限制。`
    : `评分门槛未开（0 = 不限，什么分都推）。`;
  $("#baseline-info").innerHTML = at
    ? `只自动推送 <b>${tsText(at)}</b> 之后新上架的影视。当前库里 <b>${st.items || 0}</b> 条中，
       <b>${st.history || 0}</b> 条已归为历史（不自动推），<b>${st.fresh || 0}</b> 条待推。
       <br>${gateTxt}
       <br>另外：老剧从「更新至 N 集」变成「全 N 集」时会补推一次（每个条目只触发一次）。`
    : `尚未建立基线，下一次同步会把当前全部内容标记为历史。<br>${gateTxt}`;
  paintPause();
  paintSavePath();
  renderCatManage();
}

function renderCatManage() {
  const cats = (S.settings && S.settings.categories) || [];
  $("#cat-manage").innerHTML = cats
    .map(
      (c) => `<label class="cat-line">
      <span class="switch"><input type="checkbox" data-caton="${esc(c.slug)}" ${c.on ? "checked" : ""}><span></span></span>
      <div><div class="nm">${esc(c.name)}</div><div class="slug">/${esc(c.slug)}</div></div>
      <div class="sp"></div>
      <span class="tag">${c.on ? "已启用" : "已关闭"}</span>
    </label>`
    )
    .join("");
  $$("[data-caton]").forEach((el) => {
    el.onchange = async () => {
      const slug = el.getAttribute("data-caton");
      const cats2 = (S.settings.categories || []).map((c) =>
        c.slug === slug ? { ...c, on: el.checked } : c
      );
      const r = await api("/api/settings", { categories: cats2 });
      if (!r.ok) return toast("保存失败", "err");
      S.settings = r.settings;
      renderCatManage();
      renderCatControls();
      toast(`${slug} 已${el.checked ? "开启" : "关闭"}`, "ok");
    };
  });
}

async function saveSettings() {
  const patch = {
    auto: {
      enabled: $("#auto-enabled").checked,
      interval: Number($("#auto-interval").value || 15),
      pick: $("#auto-pick").value,
      min_gb: Number($("#auto-min").value || 0),
      max_gb: Number($("#auto-max").value || 0),
      min_score: Number($("#auto-minscore").value || 0),
      score_unknown: $("#auto-scoreunknown").checked,
      max_push_per_run: Number($("#auto-maxpush").value || 8),
      max_push_per_day: Number($("#auto-maxday").value || 0),
      min_free_gb: Number($("#auto-minfree").value || 0),
      prefer_sec: $("#auto-sec").checked,
      prefer_4k: $("#auto-4k").checked,
      pages: Number($("#auto-pages").value || 3),
    },
  };
  const r = await api("/api/settings", patch);
  if (r.ok) {
    S.settings = r.settings;
    paintPause();
    // 重画一遍，让上面的门槛说明 / 栏目状态马上跟上新值
    fillSettings();
    toast("设置已保存", "ok");
  } else toast("保存失败", "err");
}

async function loadLogs() {
  const r = await api("/api/logs?limit=80");
  if (!r.ok) return;
  $("#logs").innerHTML = r.logs
    .map((l) => {
      const d = new Date(l.ts * 1000);
      const ts = d.toLocaleString("zh-CN", { hour12: false }).replace(/\//g, "-");
      return `<div class="logline"><span class="ts">${ts}</span><span class="lv ${l.level}">${l.level}</span><span>${esc(l.msg)}</span></div>`;
    })
    .join("");
}

/* ---------------- 洗版 ---------------- */
function qBadge(txt) {
  if (!txt) return "";
  const t = txt.toLowerCase();
  const cls = /2160|4k/.test(t) ? "q4k" : /1080/.test(t) ? "q1080" : /720/.test(t) ? "q720" : "qother";
  return `<span class="qtag ${cls}">${esc(txt)}</span>`;
}

function washRow(c) {
  if (c.err) {
    return `<div class="washrow err"><div class="wash-main"><b>${esc(c.title)}</b>
      <div class="muted">抓取失败：${esc(c.err)}</div></div></div>`;
  }
  const free = fmtSize((c.new || {}).bytes);
  return `<div class="washrow" data-wash-row="${esc(c.id)}">
    <div class="wash-poster"><span class="wp-ph">${esc((c.title || "?").slice(0, 1))}</span>
      <img src="${esc(c.poster || "")}" referrerpolicy="no-referrer" alt=""
        onerror="this.style.display='none'"></div>
    <div class="wash-main">
      <b>${esc(c.title)}</b>
      <div class="wc-row"><span class="wc-label cur">现在</span>
        ${qBadge(c.cur.text)}<span class="wc-size">${esc(c.cur.size || "")}</span></div>
      <div class="wc-row"><span class="wc-label new">换成</span>
        ${qBadge(c.new.text)}<span class="wc-size">${esc(c.new.size || "")}</span>
        <span class="gain">${c.gtype === "tier" ? "升档" : "同档码率更高"}</span></div>
    </div>
    <div class="wash-acts">
      <label class="pick"><input type="checkbox" data-pick="${esc(c.id)}" data-btih="${esc(c.new.btih)}" checked> 选中</label>
      <button class="btn small" data-wash-one="${esc(c.id)}" data-btih="${esc(c.new.btih)}">换掉</button>
    </div>
  </div>`;
}

async function scanWash() {
  const btn = $("#btn-wash-scan");
  btn.disabled = true;
  const old = btn.textContent;
  btn.textContent = "扫描中…";
  $("#wash-list").innerHTML = "";
  $("#wash-stat").textContent = "正在逐条对比站点资源，站点有会话限流，40 条大约 1~2 分钟…";
  const payload = {
    limit: Number($("#wash-limit").value),
    min_gain: Number($("#wash-gain").value),
    cat: S.cat === "all" ? null : S.cat,
  };
  const st = await api("/api/wash/scan", payload);
  if (!st.ok) {
    btn.disabled = false; btn.textContent = old;
    $("#wash-stat").textContent = st.msg || "扫描失败";
    return;
  }
  const poll = setInterval(async () => {
    const s = await api("/api/wash/state");
    const w = s.wash || {};
    if (w.running) {
      $("#wash-stat").textContent =
        `扫描中 ${w.done}/${w.total} · 已发现 ${w.found} 个可洗版 · ${w.phase || ""}`;
      return;
    }
    clearInterval(poll);
    btn.disabled = false;
    btn.textContent = old;
    const r = await api("/api/wash/last");
    renderWashResult(r);
  }, 1500);
}

function renderWashResult(r) {
  if (!r || r.ok === false) {
    $("#wash-stat").textContent = (r && r.msg) || "扫描失败";
    return;
  }
  S.wash = r.candidates || [];
  const okCount = S.wash.filter((c) => !c.err).length;
  $("#wash-stat").innerHTML = r.scanned
    ? `检查了 ${r.scanned} 条（已推送共 ${r.pushed_total} 条），发现 <b>${okCount}</b> 条可以换成更好的版本。`
    : `没有已推送的条目可检查。先推送一些资源再来洗版。`;
  if (!S.wash.length) {
    $("#wash-list").innerHTML = `<div class="empty">没有找到更好的版本，当前都是最优了 👍</div>`;
    return;
  }
  $("#wash-list").innerHTML = `
    <div class="wash-toolbar">
      <label class="pick"><input type="checkbox" id="wash-all" checked> 全选</label>
      <span class="sp"></span>
      <button class="btn" id="btn-wash-run">执行洗版</button>
    </div>` + S.wash.map(washRow).join("");
}

async function runWash(items) {
  if (!items.length) return toast("没有选中任何条目", "err");
  const ok = confirm(
    `将把 ${items.length} 条在 115 上的旧文件删掉，换成更好的版本。\n\n` +
    `旧文件会进 115 回收站（可以还原）。确定继续？`
  );
  if (!ok) return;
  const r = await api("/api/wash/run", { items });
  toast(r.ok ? `已开始洗版 ${r.count} 条，稍后看日志` : r.msg || "失败", r.ok ? "ok" : "err");
  if (r.ok) {
    S.wash = S.wash.filter((c) => !items.some((i) => i.item_id === c.id));
    $("#wash-list").innerHTML = S.wash.length
      ? S.wash.map(washRow).join("")
      : `<div class="empty">已全部提交，刷新页面看洗版记录。</div>`;
    setTimeout(loadWashLog, 4000);
  }
}

async function loadWashLog() {
  const r = await api("/api/wash/log?limit=30");
  if (!r.ok) return;
  const rows = r.washes || [];
  if (!rows.length) {
    $("#wash-log").innerHTML = `<div class="empty">暂无记录</div>`;
    return;
  }
  $("#wash-log").innerHTML = rows
    .map((w) => `<div class="washlog">
      <span class="ts">${tsText(w.ts).slice(5)}</span>
      <span class="wl-title">${esc(w.title)}</span>
      <span class="wl-size">${esc(w.old_size || "")} → ${esc(w.new_size || "")}</span>
      <span class="lv ${w.status === "done" ? "ok" : "err"}">${w.status === "done" ? "已换新" : "失败"}</span>
    </div>`)
    .join("");
}

async function washOne(id, btih, btn) {
  if (!confirm("确定把这条在 115 上的旧文件删掉，换成更好的版本？\n旧文件会进回收站，可以还原。")) return;
  if (btn) { btn.disabled = true; btn.textContent = "处理中…"; }
  const r = await api("/api/wash/run", { item_id: id, btih });
  toast(r.ok ? "已开始处理" : r.msg || "失败", r.ok ? "ok" : "err");
  if (r.ok) setTimeout(loadWashLog, 6000);
  if (btn) { btn.disabled = false; btn.textContent = "换掉"; }
}

/* 单条卡片上的「洗版」：只检查这一条，有更好版本就跳到洗版页让你确认 */
async function scanWashOne(id, btn) {
  if (btn) { btn.disabled = true; btn.textContent = "检查…"; }
  const st = await api("/api/wash/scan", { item_id: id, min_gain: 1, limit: 1 });
  if (!st.ok) {
    if (btn) { btn.disabled = false; btn.textContent = "洗版"; }
    return toast(st.msg || "检查失败", "err");
  }
  const wait = setInterval(async () => {
    const s = await api("/api/wash/state");
    if ((s.wash || {}).running) return;
    clearInterval(wait);
    if (btn) { btn.disabled = false; btn.textContent = "洗版"; }
    const r = await api("/api/wash/last");
    const list = (r.candidates || []).filter((c) => !c.err);
    if (!list.length) return toast("这条已经是最优版本了 👍", "ok");
    S.wash = list;
    location.hash = "#wash";
    $("#wash-stat").innerHTML = `检查了「${esc(list[0].title)}」，发现更好的版本。确认后点「换掉」。`;
    $("#wash-list").innerHTML = `<div class="wash-toolbar">
        <span class="muted">单条洗版</span><span class="sp"></span></div>` + list.map(washRow).join("");
  }, 1200);
}

/* ---------------- 订阅追片 ---------------- */
const SUBS = { list: [], cfg: {}, stats: {}, state: null, timer: null };

const SUB_STATUS = {
  waiting: ["等片源", "wait"],
  pushed: ["已推送", "ok"],
  error: ["异常", "bad"],
};
const SUB_CAT = { mv: "电影", tv: "电视剧", ac: "动漫" };
const SUB_EV = { add: "新增", push: "推送", wash: "洗版", miss: "没找到",
                 wait: "等待", hit: "命中", fail: "失败", adopt: "认领", ep: "进度" };

function subRow(s) {
  const follow = s.ep_mode === "follow";
  const cur = follow
    ? (Number(s.last_ep) > 0
        ? `已追到第 ${s.last_ep} 集${s.size_text ? " · 最近 " + s.size_text : ""}`
        : "还没开始追")
    : (s.btih
        ? `当前 ${s.size_text || "已推送"}${resOfScore(s.qscore) ? " · " + resOfScore(s.qscore) : ""}`
        : "还没推送");
  const rules = [{ "2160p": "只接受 4K", "1080p": "至少 1080p" }[s.qmin] || "画质不限"];
  if (Number(s.min_gb) || Number(s.max_gb)) {
    rules.push(`体积 ${Number(s.min_gb) || 0}–${Number(s.max_gb) || "∞"}G`);
  }
  rules.push(Number(s.best_version || s.auto_wash) ? "自动洗版" : "不自动洗版");
  if (Number(s.best_version_full)) rules.push("只洗整包");
  if (s.filter_inc) rules.push(`含 ${s.filter_inc}`);
  if (s.filter_exc) rules.push(`排除 ${s.filter_exc}`);
  if (Number(s.search_interval)) rules.push(`每 ${s.search_interval}h 查`);
  if (s.cat !== "mv") {
    rules.push(follow ? "追更" : (s.ep_mode === "all" ? "有就推" : "等完结"));
  }
  if (follow && Number(s.ep_from) > 0) rules.push(`从第 ${s.ep_from} 集补`);
  // MP V3 状态机：N 新建 / R 订阅中 / P 待定 / S 暂停
  const stMap = { N: ["新建", ""], R: ["订阅中", "ok"], P: ["待定", "warn"], S: ["暂停", ""] };
  const st = (stMap[s.state] || stMap[s.enabled ? "R" : "S"]).slice();
  // 追更订阅等着下一集是常态，显示「追更中」比「订阅中」更具体
  if (follow && s.state === "R") st[0] = "追更中";
  const pr = Number(s.current_priority) || 0;
  const lack = Number(s.lack_episode) || 0;
  const last = s.last_check ? `上次检查 ${tsText(s.last_check).slice(5)}` : "还没检查";
  return `<div class="subrow">
    <div class="sm">
      <div class="st">
        <b>${esc(s.title || s.kw)}</b>
        <span class="badge-org ${st[1]}">${st[0]}</span>
        <span class="tag">${SUB_CAT[s.cat] || "不限"}</span>
        ${follow ? `<span class="tag ep">追更</span>` : ""}
        ${s.year ? `<span class="tag">${esc(s.year)}</span>` : ""}
        ${pr ? `<span class="tag">pri ${pr}</span>` : ""}
        ${s.media_source === "pan115" ? `<span class="tag">115 网盘</span>` : ""}
        ${lack ? `<span class="tag">缺 ${lack} 集</span>` : ""}
        ${s.hits ? `<span class="tag">命中 ${s.hits} 次</span>` : ""}
      </div>
      <div class="smeta">关键词 ${esc(s.kw)} · ${rules.map(esc).join(" · ")} · ${esc(cur)}</div>
      <div class="slast">${last}${s.last_msg ? " · " + esc(s.last_msg) : ""}</div>
    </div>
    <div class="sacts">
      ${s.cat !== "mv" ? `<select class="mini-sel" data-sub-epmode="${s.id}" title="剧集更新方式">
        <option value=""${s.ep_mode === "" ? " selected" : ""}>等完结</option>
        <option value="follow"${s.ep_mode === "follow" ? " selected" : ""}>追更</option>
        <option value="all"${s.ep_mode === "all" ? " selected" : ""}>有就推</option>
      </select>` : ""}
      <button class="btn ghost small" data-sub-check="${s.id}">检查</button>
      <button class="btn ghost small" data-sub-toggle="${s.id}" data-on="${s.enabled ? 1 : 0}">${s.enabled ? "暂停" : "恢复"}</button>
      <button class="btn ghost small danger" data-sub-del="${s.id}">删除</button>
    </div>
  </div>`;
}

function paintSubKpis() {
  const list = SUBS.list || [];
  const running = list.filter((x) => (x.state || "N") === "R").length;
  const pending = list.filter((x) => (x.state || "N") === "P").length;
  const stopped = list.filter((x) => (x.state || "N") === "S" || !x.enabled).length;
  const lack = list.reduce((n, x) => n + (Number(x.lack_episode) || 0), 0);
  $("#sub-kpis").innerHTML = `
    <div class="kpi"><div class="kpi-n">${list.length}</div><div class="kpi-k">订阅总数</div></div>
    <div class="kpi ok"><div class="kpi-n">${running}</div><div class="kpi-k">订阅中</div></div>
    <div class="kpi warn"><div class="kpi-n">${pending}</div><div class="kpi-k">待定</div></div>
    <div class="kpi ${lack ? "bad" : ""}"><div class="kpi-n">${lack}</div>
      <div class="kpi-k">缺集${stopped ? ` · 暂停 ${stopped}` : ""}</div></div>`;
  $("#sub-count").textContent = list.length
    ? `共 ${list.length} 个 · 订阅中 ${running} 个`
      + (pending ? ` · 待定 ${pending}` : "") + (lack ? ` · 缺 ${lack} 集` : "")
    : "";
}

function paintSubHead() {
  const en = $("#sub-enabled");
  en.checked = SUBS.cfg.enabled !== false;
  const iv = $("#sub-interval");
  // 正在输入时不要被轮询覆盖
  if (document.activeElement !== iv) iv.value = SUBS.cfg.interval || 60;
  const pf = $("#sub-panfallback"), pp = $("#sub-panpack");
  if (pf) pf.checked = SUBS.cfg.pan_fallback !== false;
  if (pp) pp.checked = SUBS.cfg.pan_allow_pack !== false;
  const st = SUBS.state || {};
  $("#sub-run-msg").textContent = st.running ? (st.phase || "检查中…")
    : (st.last_msg ? `上次：${st.last_msg}` : "");
  if (st.running) startSubPoll();
}

async function loadSubs() {
  const r = await api("/api/subs");
  if (!r.ok) return;
  SUBS.list = r.subs || [];
  SUBS.cfg = r.cfg || {};
  SUBS.stats = r.stats || {};
  SUBS.state = r.state || {};
  paintSubKpis();
  paintSubHead();
  renderSubList();
  loadSubLogs();
}

function renderSubList() {
  const box = $("#sub-list");
  if (!SUBS.list.length) {
    box.innerHTML = `<div class="empty">还没有订阅。上面填个片名就能加，站点暂时没有也会一直盯着。</div>`;
    return;
  }
  box.innerHTML = SUBS.list.map(subRow).join("");
}

async function loadSubLogs() {
  const r = await api("/api/subs/log?limit=60");
  if (!r.ok) return;
  const logs = r.logs || [];
  const box = $("#sub-log");
  if (!logs.length) {
    box.innerHTML = `<div class="empty">暂无记录</div>`;
    return;
  }
  box.innerHTML = logs.map((x) => `<div class="sublogline">
    <span class="ts">${tsText(x.ts).slice(5)}</span>
    <span class="ev ${esc(x.event)}">${SUB_EV[x.event] || esc(x.event)}</span>
    <span class="msg">${esc(x.kw || "")}${x.msg ? " · " + esc(x.msg) : ""}</span>
  </div>`).join("");
}

// 「追更起始集」只在选了追更时才有意义，其它情况置灰，避免用户填了却没生效
function syncEpFrom() {
  const sel = $("#sub-epmode"), f = $("#f-epfrom"), i = $("#sub-epfrom");
  if (!sel || !f || !i) return;
  const on = sel.value === "follow";
  f.style.opacity = on ? "1" : ".45";
  i.disabled = !on;
  if (!on) i.value = "";
}

async function addSub(preset, useFormRules) {
  const p = preset || {};
  const fromForm = !preset;
  const rules = fromForm || useFormRules;
  const cat = p.cat !== undefined ? p.cat : $("#sub-cat").value;
  const isTv = cat === "tv" || cat === "ac";
  // 剧集从卡片/候选点进来默认「追更」，电影不受影响
  const epmode = rules ? ($("#sub-epmode").value || "")
    : (p.ep_mode !== undefined ? p.ep_mode : (isTv ? "follow" : ""));
  const body = {
    kw: p.kw || $("#sub-kw").value.trim(),
    cat,
    year: p.year !== undefined ? p.year : $("#sub-year").value.trim(),
    qmin: rules ? $("#sub-qmin").value : "",
    min_gb: rules ? Number($("#sub-mingb").value || 0) : 0,
    max_gb: rules ? Number($("#sub-maxgb").value || 0) : 0,
    auto_wash: rules ? $("#sub-wash").checked : true,
    best_version: rules ? $("#sub-wash").checked : true,
    best_version_full: rules ? $("#sub-washfull").checked : false,
    filter_inc: rules ? $("#sub-inc").value.trim() : "",
    filter_exc: rules ? $("#sub-exc").value.trim() : "",
    search_interval: rules ? Number($("#sub-iv").value || 0) : 0,
    // 只有「等完结」才需要 wait_end；追更 / 有就推都不等
    wait_end: epmode === "",
    ep_mode: epmode,
    ep_from: rules && !$("#sub-epfrom").disabled ? Number($("#sub-epfrom").value || 0) : 0,
    sub: p.sub || "",
  };
  if (!body.kw) return toast("先填片名", "err");
  $("#sub-form-msg").textContent = "提交中…";
  const r = await api("/api/subs/add", body);
  $("#sub-form-msg").textContent = "";
  if (!r.ok) return toast(r.msg || "添加失败", "err");
  toast(r.msg || (r.dup ? "已经在订阅里了" : "已添加"), r.dup ? "" : "ok");
  if (!r.dup) {
    $("#sub-kw").value = "";
    $("#sub-year").value = "";
    $("#sub-cand").innerHTML = "";
  }
  await loadSubs();
  startSubPoll();
}

async function findSubCands() {
  const kw = $("#sub-kw").value.trim();
  if (!kw) return toast("先填片名", "err");
  const box = $("#sub-cand");
  box.innerHTML = `<div class="muted" style="padding:8px 2px">搜索中…（站点可能有点慢）</div>`;
  const r = await api("/api/subs/find", { kw });
  if (!r.ok) {
    box.innerHTML = `<div class="muted" style="padding:8px 2px">${esc(r.msg || "搜索失败")}</div>`;
    return;
  }
  const items = r.items || [];
  if (!items.length) {
    box.innerHTML = `<div class="muted" style="padding:8px 2px">站点上暂时没有，直接点「添加订阅」让系统一直盯着就行</div>`;
    return;
  }
  box.innerHTML = items.map((x) => `<div class="subcand">
    <span class="tag">${SUB_CAT[x.cat] || "其他"}</span>
    <div class="nm">${esc(x.title)}</div>
    <div class="meta">${esc([x.year, x.meta].filter(Boolean).join(" · ").slice(0, 46))}</div>
    <button class="btn small" data-sub-pick="${esc(x.kw)}" data-pickyear="${esc(x.year)}"
      data-pickcat="${esc(x.cat)}" data-picksub="${esc(x.sub || "")}">订阅这个</button>
  </div>`).join("");
}

async function toggleSub(id, on) {
  const r = await api("/api/subs/update", { id, enabled: !on });
  if (!r.ok) return toast(r.msg || "操作失败", "err");
  toast(on ? "已暂停这个订阅" : "已恢复，马上查一遍", "ok");
  await loadSubs();
  if (on) startSubPoll();
}

async function delSub(id) {
  const s = SUBS.list.find((x) => String(x.id) === String(id));
  if (!confirm(`删除订阅「${(s && (s.title || s.kw)) || id}」？\n\n已经推到 115 的片子不会动，只是不再帮你盯新版本了。`)) return;
  const r = await api("/api/subs/del", { id });
  toast(r.ok ? "已删除订阅" : r.msg || "删除失败", r.ok ? "ok" : "err");
  if (r.ok) loadSubs();
}

async function checkSub(id) {
  const r = await api("/api/subs/check", id ? { id } : {});
  toast(r.ok ? (r.msg || "开始检查") : (r.msg || "稍后再试"), r.ok ? "ok" : "err");
  if (r.ok) startSubPoll();
}

async function saveSubCfg() {
  const r = await api("/api/subs/config", {
    enabled: $("#sub-enabled").checked,
    interval: Number($("#sub-interval").value || 60),
    pan_fallback: $("#sub-panfallback") ? $("#sub-panfallback").checked : true,
    pan_allow_pack: $("#sub-panpack") ? $("#sub-panpack").checked : true,
  });
  if (!r.ok) return toast(r.msg || "保存失败", "err");
  SUBS.cfg = r.sub || {};
  paintSubHead();
  toast("订阅检查设置已保存", "ok");
}

function startSubPoll() {
  if (SUBS.timer) return;
  SUBS.timer = setInterval(async () => {
    const r = await api("/api/subs/state");
    const st = (r && r.state) || {};
    const hidden = $("#view-subs").classList.contains("hidden");
    if (st.running) {
      if (!hidden) $("#sub-run-msg").textContent = st.phase || "检查中…";
      return;
    }
    clearInterval(SUBS.timer);
    SUBS.timer = null;
    if (!hidden) {
      await loadSubs();
      toast(st.last_msg || "检查完成", "ok");
    }
  }, 3000);
}

/* ---------------- 路由 / 初始化 ---------------- */
const TITLES = {
  latest: ["最新更新", "与网站列表同序 · 新更新的排前面"],
  search: ["搜索资源", "搜索后一键磁力到 115"],
  subs: ["订阅追片", "有源自动离线 · 更高画质自动洗版"],
  wash: ["洗版升级", "删旧换新 · 只保留更高画质"],
  tasks: ["推送记录", "最近的离线任务提交情况"],
  pan115: ["115 账号", "多通道扫码登录 · 保存目录"],
  site: ["站点登录", "输入教父站点账号密码"],
  settings: ["设置", "自动推送规则与栏目开关"],
};

function route() {
  const v = (location.hash || "#latest").slice(1);
  const view = TITLES[v] ? v : "latest";
  $$(".view").forEach((el) => el.classList.add("hidden"));
  const el = $("#view-" + view);
  if (el) el.classList.remove("hidden");
  $$("[data-nav]").forEach((a) => a.classList.toggle("active", a.getAttribute("data-nav") === view));
  $("#page-title").textContent = TITLES[view][0];
  $("#page-sub").textContent = TITLES[view][1];
  if (view === "latest") loadLatest(true);
  if (view === "tasks") loadTasks();
  if (view === "subs") loadSubs();
  if (view === "wash") {
    loadWashLog();
    // 上次扫描的结果还在服务端内存里，切回来直接还原
    api("/api/wash/last").then((r) => {
      if (r && r.scanned && !S.washRendered) renderWashResult(r);
    });
  }
  if (view === "pan115") {
    renderChannels();
    loadPanInfo();
    paintSavePath();
  }
  if (view === "site") {
    if (S.status && S.status.site.username) $("#site-user").value = S.status.site.username;
  }
  if (view === "settings") {
    fillSettings();
    loadLogs();
  }
}

function bind() {
  window.addEventListener("hashchange", route);

  // 订阅行里直接切换「等完结 / 追更 / 有就推」，改完立刻重查一遍
  document.addEventListener("change", async (e) => {
    const sel = e.target.closest("[data-sub-epmode]");
    if (!sel) return;
    const r = await api("/api/subs/update", {
      id: Number(sel.getAttribute("data-sub-epmode")),
      ep_mode: sel.value,
    });
    if (!r.ok) {
      toast(r.msg || "修改失败", "err");
      loadSubs();
      return;
    }
    toast("更新方式已改为「" + sel.options[sel.selectedIndex].textContent + "」", "ok");
    await loadSubs();
    startSubPoll();
  });

  document.addEventListener("click", (e) => {
    const chip = e.target.closest("[data-cat]");
    if (chip) {
      S.cat = chip.getAttribute("data-cat");
      renderCatControls();
      loadLatest(true);
      return;
    }
    // 手动转存三按钮（卡片 + 详情弹窗通用）
    const cv = e.target.closest("[data-convert]");
    if (cv) {
      e.stopPropagation();
      runConvert(cv.getAttribute("data-id"), cv.getAttribute("data-convert"),
                 cv.getAttribute("data-magnet"), cv);
      return;
    }
    const push = e.target.closest("[data-push]");
    if (push) {
      e.stopPropagation();
      pushItem(push.getAttribute("data-push"), push);
      return;
    }
    const det = e.target.closest("[data-detail]");
    if (det) {
      e.stopPropagation();
      openDetail(det.getAttribute("data-detail"));
      return;
    }
    const w1 = e.target.closest("[data-wash-one]");
    if (w1) {
      e.stopPropagation();
      washOne(w1.getAttribute("data-wash-one"), w1.getAttribute("data-btih"), w1);
      return;
    }
    const wb = e.target.closest("[data-wash]");
    if (wb) {
      e.stopPropagation();
      scanWashOne(wb.getAttribute("data-wash"), wb);
      return;
    }
    const sf = e.target.closest("[data-subfrom]");
    if (sf) {
      e.stopPropagation();
      addSub({ kw: sf.getAttribute("data-subfrom"), cat: sf.getAttribute("data-subcat") || "",
               year: sf.getAttribute("data-subyear") || "",
               sub: sf.getAttribute("data-submeta") || "" }, false);
      return;
    }
    const sp = e.target.closest("[data-sub-pick]");
    if (sp) {
      e.stopPropagation();
      addSub({ kw: sp.getAttribute("data-sub-pick"), year: sp.getAttribute("data-pickyear") || "",
               cat: sp.getAttribute("data-pickcat") || "",
               sub: sp.getAttribute("data-picksub") || "" }, true);
      return;
    }
    const sck = e.target.closest("[data-sub-check]");
    if (sck) {
      e.stopPropagation();
      checkSub(sck.getAttribute("data-sub-check"));
      return;
    }
    const stg = e.target.closest("[data-sub-toggle]");
    if (stg) {
      e.stopPropagation();
      toggleSub(stg.getAttribute("data-sub-toggle"), stg.getAttribute("data-on") === "1");
      return;
    }
    const sdl = e.target.closest("[data-sub-del]");
    if (sdl) {
      e.stopPropagation();
      delSub(sdl.getAttribute("data-sub-del"));
      return;
    }
    if (e.target.id === "btn-wash-run") {
      const picks = $$("[data-pick]").filter((x) => x.checked)
        .map((x) => ({ item_id: x.getAttribute("data-pick"), btih: x.getAttribute("data-btih") }));
      runWash(picks);
      return;
    }
    if (e.target.id === "wash-all") {
      $$("[data-pick]").forEach((x) => (x.checked = e.target.checked));
      return;
    }
    const card = e.target.closest(".item");
    if (card && !e.target.closest("button")) openDetail(card.getAttribute("data-id"));
  });

  document.addEventListener("change", async (e) => {
    const tg = e.target.closest("[data-toggle]");
    if (tg) {
      const slug = tg.getAttribute("data-toggle");
      const cats = S.settings.categories.map((c) => (c.slug === slug ? { ...c, on: tg.checked } : c));
      const r = await api("/api/settings", { categories: cats });
      if (r.ok) S.settings = r.settings;
      renderCatControls();
      toast(`${slug} 已${tg.checked ? "开启" : "关闭"}`, "ok");
    }
  });

  const sync = async (full) => {
    const r = await api("/api/sync", { full });
    toast(r.msg || (r.ok ? "开始同步" : "失败"), r.ok ? "ok" : "err");
    if (!r.ok) return;
    const t = setInterval(async () => {
      const s = await api("/api/sync/state");
      const st = s.sync || {};
      if (st.running) {
        $("#side-sync").textContent = st.phase || "同步中…";
        return;
      }
      clearInterval(t);
      await refreshStatus();
      if (!$("#view-latest").classList.contains("hidden")) loadLatest(true);
      if (!$("#view-settings").classList.contains("hidden")) fillSettings();
    }, 2500);
  };

  $("#btn-sync").onclick = () => sync(false);
  $("#btn-full-crawl").onclick = () => {
    if (confirm("整站抓取会把每个栏目翻到最后一页（约 150 页、7000+ 条），大概需要 6-10 分钟。开始吗？")) sync(true);
  };
  $("#btn-more").onclick = () => loadLatest(false);
  $("#btn-pause").onclick = togglePause;
  $("#btn-pause2").onclick = togglePause;
  $("#sort-select").onchange = (e) => {
    S.sort = e.target.value;
    loadLatest(true);
  };
  $("#btn-wash-scan").onclick = scanWash;

  $("#btn-sub-add").onclick = () => addSub();
  $("#btn-sub-find").onclick = findSubCands;
  $("#sub-epmode").onchange = syncEpFrom;
  syncEpFrom();
  $("#btn-sub-all").onclick = () => checkSub();
  $("#btn-sub-savecfg").onclick = saveSubCfg;
  $("#btn-sub-logclear").onclick = async () => {
    if (!confirm("清空订阅日志？（只删日志，不动订阅，也不动 115 上的文件）")) return;
    const r = await api("/api/subs/log/clear", {});
    toast(r.ok ? `已清空 ${r.deleted} 条日志` : "操作失败", r.ok ? "ok" : "err");
    if (r.ok) loadSubLogs();
  };
  $("#sub-kw").addEventListener("keydown", (e) => {
    if (e.key === "Enter") addSub();
  });
  $("#sub-enabled").onchange = async () => {
    const on = $("#sub-enabled").checked;
    const r = await api("/api/subs/config", { enabled: on });
    if (r.ok) {
      SUBS.cfg = r.sub || {};
      toast("订阅自动检查已" + (on ? "开启" : "关闭"), "ok");
    } else toast("保存失败", "err");
  };
  // 网盘兜底两个开关：点了立刻生效，不用再点「保存」
  ["#sub-panfallback", "#sub-panpack"].forEach((sel) => {
    const el = $(sel);
    if (!el) return;
    el.onchange = async () => {
      const r = await api("/api/subs/config", {
        pan_fallback: $("#sub-panfallback").checked,
        pan_allow_pack: $("#sub-panpack").checked,
      });
      if (r.ok) {
        SUBS.cfg = r.sub || {};
        toast("网盘兜底设置已保存", "ok");
      } else toast("保存失败", "err");
    };
  });
  $("#btn-search").onclick = doSearch;
  $("#q").addEventListener("keydown", (e) => {
    if (e.key === "Enter") doSearch();
  });

  $("#btn-site-login").onclick = siteLogin;
  $("#btn-site-logout").onclick = async () => {
    await api("/api/site/logout", {});
    toast("已退出站点", "ok");
    refreshStatus();
  };

  $("#btn-save-settings").onclick = saveSettings;
  $("#auto-enabled").onchange = async () => {
    const r = await api("/api/settings", { auto: { enabled: $("#auto-enabled").checked } });
    if (r.ok) {
      S.settings = r.settings;
      toast("自动推送已" + ($("#auto-enabled").checked ? "开启" : "关闭"), "ok");
    } else toast("保存失败", "err");
  };
  $("#btn-pick-dir") && ($("#btn-pick-dir").onclick = () => openDirPicker(""));
  $("#btn-pick-dir2") && ($("#btn-pick-dir2").onclick = () => openDirPicker(""));
  $$("[data-pickdir]").forEach((el) => {
    el.onclick = () => openDirPicker(el.getAttribute("data-pickdir") || "");
  });
  $("#btn-discover").onclick = async () => {
    const r = await api("/api/categories/discover");
    if (r.ok) {
      toast("已识别 " + (r.discovered || []).length + " 个栏目", "ok");
      await refreshStatus();
      fillSettings();
    } else toast(r.msg || "识别失败", "err");
  };

  $("#btn-qr-refresh").onclick = () => {
    const act = document.querySelector(".channel.active");
    if (act) startQr(act.getAttribute("data-app"), act.getAttribute("data-name"), act);
    else toast("先选择一种扫码方式", "err");
  };

  $("#btn-pan-logout").onclick = async () => {
    await api("/api/115/logout", {});
    toast("已退出 115", "ok");
    refreshStatus();
  };

  $("#modal-close").onclick = () => $("#modal").classList.add("hidden");
  $("#modal").onclick = (e) => {
    if (e.target.id === "modal") $("#modal").classList.add("hidden");
  };
}

/* ---------------- 全局任务进度条 ----------------
   转存是慢操作，用户点完按钮可能切到别的页面或刷新过，
   这里常驻一条底部进度条：只要服务端有进行中的转存任务就显示，
   完成后继续显示 20 秒再消失，保证任何页面都能看到进度。 */
function convBarPaint(t) {
  let bar = document.getElementById("conv-bar");
  if (!t) {
    if (bar) bar.remove();
    return;
  }
  if (!bar) {
    bar = document.createElement("button");
    bar.id = "conv-bar";
    bar.style.cssText = "position:fixed;left:0;right:0;bottom:0;z-index:999;padding:8px 14px;"
      + "background:var(--panel);border:0;border-top:2px solid var(--accent,#f43f5e);font-size:13px;"
      + "display:flex;gap:10px;align-items:center;cursor:pointer;width:100%;text-align:left;color:inherit";
    bar.onclick = () => location.hash = "#tasks";
    // 挂到 main 容器末尾（不是 body）：某些辅助工具只遍历主容器，便于暴露
    (document.querySelector("main.main") || document.body).appendChild(bar);
  }
  const label = CONV_LABEL[t.mode] || "转存";
  const st = t.state === "running" ? "进行中"
    : t.state === "queued" ? "排队中" : (t.ok ? "完成 ✓" : "失败");
  bar.innerHTML = `<b>${esc(label)}</b>`
    + `<span class="tag ${t.ok ? "ok" : (t.state === "running" || t.state === "queued" ? "" : "")}">${esc(st)}</span>`
    + `<span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(t.msg || "")}</span>`
    + `<span class="muted small">点此看记录</span>`;
}

/* ---------------- 手动转存进度显示 ----------------
   双通道显示同一状态：① 顶部横幅（静态元素，最可靠）
   ② 底部悬浮条。数据来自 3 秒轮询的 /api/convert/status，
   refreshStatus(15 秒) 也会用 /api/status 里的 conv 刷新一次兜底。 */
function paintConv(c) {
  const cb = $("#conv-banner");
  if (cb) {
    if (c && c.msg) {
      const label = CONV_LABEL[c.mode] || "转存";
      const st = c.state === "running" ? "进行中"
        : c.state === "queued" ? "排队中" : (c.ok ? "完成 ✓" : "失败");
      cb.innerHTML = `<b>${esc(label)}</b><span class="tag">${esc(st)}</span>`
        + `<span class="cb-msg">${esc(c.msg)}</span>`;
      cb.classList.remove("hidden");
      cb.onclick = () => location.hash = "#tasks";
    } else {
      cb.classList.add("hidden");
    }
  }
  convBarPaint(c);
}

async function watchConvert() {
  try {
    const r = await api("/api/convert/status");
    const t = r && r.ok && r.state ? r : null;
    const fresh = t && (t.state === "running" || t.state === "queued"
      || (t.done_at && Date.now() / 1000 - t.done_at < 120));
    paintConv(fresh ? t : null);
  } catch {}
}

(async function init() {
  bind();
  await refreshStatus();
  renderChannels();
  route();
  setInterval(refreshStatus, 15000);
  watchConvert();
  setInterval(watchConvert, 3000);
  setInterval(() => {
    if (!$("#view-settings") || $("#view-settings").classList.contains("hidden")) return;
    loadLogs();
  }, 12000);
  // 订阅页停在上面时保持刷新：状态、上次检查结果、日志都跟着变
  setInterval(() => {
    if (!$("#view-subs") || $("#view-subs").classList.contains("hidden")) return;
    if (SUBS.timer) return;      // 正在轮询检查进度，别重复打
    loadSubs();
  }, 20000);
})();
