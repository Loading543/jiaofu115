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
  $("#side-sync").textContent = sy.running
    ? (sy.phase || "同步中…")
    : sy.last_run
      ? "上次同步 " + new Date(sy.last_run * 1000).toLocaleTimeString("zh-CN")
      : "尚未同步";
  paintPause();
  renderCatControls();
  paintSavePath();
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
  if (it.pushed == 1) badge = `<div class="badge done">已推送</div>`;
  else if (it.pushed == 2) badge = `<div class="badge fail">推送失败</div>`;
  else if (it.pushed == 3) badge = `<div class="badge old">历史</div>`;
  else if (it.isnew == 1) badge = `<div class="badge new">新上架</div>`;
  else badge = `<div class="badge old">历史</div>`;
  const size = it.best_size ? `<div class="badge size">${esc(it.best_size)}</div>` : "";
  const washBtn = it.pushed == 1
    ? `<button class="btn wash small" data-wash="${esc(it.id)}">洗版</button>` : "";
  return `<div class="item" data-id="${esc(it.id)}">
    <div class="poster"><span class="ph">${ph}</span>${img}${badge}${size}</div>
    <div class="item-body">
      <div class="item-title" title="${esc(it.title)}">${esc(it.title)}</div>
      <div class="item-sub">${esc(it.sub || "")}</div>
      <div class="acts">
        <button class="btn small" data-push="${esc(it.id)}">秒传 115</button>
        <button class="btn ghost small" data-detail="${esc(it.id)}">磁力</button>
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
    btn.dataset.old = btn.textContent;
    btn.textContent = "推送中…";
  }
  const r = await api("/api/push", { item_id: id, magnet: magnet || null });
  toast(r.ok ? "已提交到 115 · " + (r.size || "") : r.msg || "推送失败", r.ok ? "ok" : "err");
  if (btn) {
    btn.disabled = false;
    btn.textContent = magnet ? "推送这条" : "秒传 115";
  }
  refreshStatus();
  if (!magnet) loadLatest(true);
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
  const best = mags[0] || null;
  const rows = mags.length
    ? mags
        .map(
          (m, i) => `<div class="mag ${i === 0 ? "sel" : ""}" data-mag="${esc(m.url)}">
        <div class="sz">${esc(m.size_text || fmtSize(m.size_bytes) || "未知体积")}</div>
        <div class="nm">${esc(m.label || m.btih)}</div>
        <button class="btn small" data-magpush="${esc(m.url)}">推送</button>
      </div>`
        )
        .join("")
    : `<div class="empty">未解析到磁力链接</div>`;
  $("#modal-body").innerHTML = `
    <h2 style="margin:0 0 4px;font-size:16px">${esc(it.title || "")}</h2>
    <div class="muted">${esc(it.sub || "")} · 共 ${mags.length} 条磁力，已按体积从大到小排序</div>
    <div class="mag-list">${rows}</div>
    <div class="row">
      <div class="muted">默认推送体积最大的一条</div>
      <button class="btn" id="modal-push-best" ${best ? "" : "disabled"}>推送到 115</button>
    </div>`;
  const btn = $("#modal-push-best");
  if (best)
    btn.onclick = async () => {
      await pushItem(id, btn, best.url);
      $("#modal").classList.add("hidden");
      loadLatest(true);
    };
  $$("[data-magpush]").forEach((b) => {
    b.onclick = async () => {
      const url = b.getAttribute("data-magpush");
      await pushItem(id, b, url);
      $("#modal").classList.add("hidden");
      loadLatest(true);
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
      const left = d.quota != null ? d.quota : d.count;
      const total = d.total;
      const txt = left != null
        ? `离线配额 ${left}${total != null ? " / " + total : ""}`
        : JSON.stringify(d).slice(0, 120);
      $("#pan-info").textContent = "已登录 115 · " + txt;
    }
  }
}

/* ---------------- 115 目录选择 ---------------- */
function paintSavePath() {
  const p = (S.settings && S.settings.pan) || {};
  const txt = p.save_path_name
    ? `${p.save_path_name}${p.save_path_id ? "  (ID " + p.save_path_id + ")" : ""}`
    : p.save_path_id ? "已选目录（ID " + p.save_path_id + "）" : "未选择（默认云下载）";
  ["#pan-path-show", "#pan-path-show2"].forEach((sel) => {
    const el = $(sel);
    if (!el) return;
    el.textContent = txt;
    el.classList.toggle("empty", !p.save_path_id && !p.save_path_name);
  });
}

async function openDirPicker() {
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
  body.innerHTML = `
    <h2 style="margin:0 0 10px;font-size:16px">选择 115 保存目录</h2>
    ${dirCrumbs()}
    <div class="dirlist">${list || `<div class="empty">${r.ok ? "该目录下没有子目录" : esc(r.msg || "读取失败")}</div>`}</div>
    <div class="row">
      <div class="muted">选中后，所有推送都会落到这个目录</div>
      <div style="display:flex;gap:8px">
        <button class="btn ghost small" id="dir-cancel">取消</button>
        <button class="btn" id="dir-pick">选择「${esc(cur.name)}」</button>
      </div>
    </div>`;
  $("#dir-pick").onclick = () => saveDir(cur);
  $("#dir-cancel").onclick = () => $("#modal").classList.add("hidden");
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
  const r = await api("/api/115/savepath", {
    id: isRoot ? "" : cur.cid,
    name: isRoot ? "" : name,
  });
  if (!r.ok) return toast("保存失败", "err");
  S.settings.pan = r.pan;
  paintSavePath();
  $("#modal").classList.add("hidden");
  toast(isRoot ? "已恢复默认目录" : "保存目录已设为：" + name, "ok");
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
  $("#auto-maxpush").value = a.max_push_per_run || 8;
  $("#auto-pages").value = a.pages || 3;
  const at = S.status && S.status.baseline_at;
  const st = (S.status && S.status.stats) || {};
  $("#baseline-info").innerHTML = at
    ? `只自动推送 <b>${tsText(at)}</b> 之后新上架的影视。当前库里 <b>${st.items || 0}</b> 条中，
       <b>${st.history || 0}</b> 条已归为历史（不自动推），<b>${st.fresh || 0}</b> 条是新上架待推。`
    : `尚未建立基线，下一次同步会把当前全部内容标记为历史。`;
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
      max_push_per_run: Number($("#auto-maxpush").value || 8),
      pages: Number($("#auto-pages").value || 3),
    },
  };
  const r = await api("/api/settings", patch);
  if (r.ok) {
    S.settings = r.settings;
    paintPause();
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

/* ---------------- 路由 / 初始化 ---------------- */
const TITLES = {
  latest: ["最新更新", "整站抓取 · 按上架时间排序"],
  search: ["搜索资源", "搜索后一键磁力到 115"],
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

  document.addEventListener("click", (e) => {
    const chip = e.target.closest("[data-cat]");
    if (chip) {
      S.cat = chip.getAttribute("data-cat");
      renderCatControls();
      loadLatest(true);
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
    if (confirm("整站抓取会把每个栏目翻到最后一页（约 150 页、7000+ 条），大概需要 3-6 分钟。开始吗？")) sync(true);
  };
  $("#btn-more").onclick = () => loadLatest(false);
  $("#btn-pause").onclick = togglePause;
  $("#btn-pause2").onclick = togglePause;
  $("#sort-select").onchange = (e) => {
    S.sort = e.target.value;
    loadLatest(true);
  };
  $("#btn-wash-scan").onclick = scanWash;

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
  $("#btn-pick-dir").onclick = openDirPicker;
  $("#btn-pick-dir2").onclick = openDirPicker;
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

(async function init() {
  bind();
  await refreshStatus();
  renderChannels();
  route();
  setInterval(refreshStatus, 15000);
  setInterval(() => {
    if (!$("#view-settings") || $("#view-settings").classList.contains("hidden")) return;
    loadLogs();
  }, 12000);
})();
