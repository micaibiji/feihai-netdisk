const app = document.querySelector("#app"),
  modalRoot = document.querySelector("#modalRoot");
const state = {
  page: "home",
  overview: null,
  query: "",
  search: null,
  provider: "all",
  rankingType: "all",
  rankingPage: 1,
  rankingLoading: false,
  rankingFilters: { year: "", genre: "", country: "" },
};
const pm = {
  115: { label: "115网盘", short: "115", color: "#2f66ff" },
  baidu: { label: "百度网盘", short: "百", color: "#4169e1" },
  quark: { label: "夸克网盘", short: "夸", color: "#1c2530" },
  china_mobile: { label: "中国移动云盘", short: "移", color: "#17a4df" },
};
const resourceWorkCache = new Map(), resourceWorkRequests = new Map();
const titles = {
  search: ["全网资源搜索", "同时搜索影视作品、Telegram 频道与四个网盘来源。"],
  following: ["我的追更", "一个影视可保留多个网盘来源，自动使用最新可用来源。"],
  library: ["飞牛影视媒体库", "统一命名、STRM、字幕、封面和 NFO 的整理中心。"],
  accounts: ["网盘账号", "按 115、百度、夸克、移动顺序管理授权。"],
  risk: ["风险中心", "检测授权失效、访问受限和网关异常，不绕过网盘风控。"],
  settings: ["设置", "Telegram、元数据、命名规则与飞牛影视目录。"],
};
const esc = (v = "") => {
  const n = document.createElement("div");
  n.textContent = String(v);
  return n.innerHTML;
};
const date = (v) => {
  if (!v) return "尚未检查";
  try {
    return new Intl.DateTimeFormat("zh-CN", {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    }).format(new Date(v));
  } catch {
    return v;
  }
};
function toast(msg, type = "ok") {
  const e = document.createElement("div");
  e.className = `toast ${type}`;
  e.innerHTML = `<i>${type === "ok" ? "✓" : "!"}</i>${esc(msg)}`;
  document.querySelector("#toastRoot").append(e);
  setTimeout(() => e.remove(), 3000);
}
async function api(url, opt = {}) {
  const cleanUrl = new URL(url, location.origin),
    { keepUnauthorized = false, ...requestOpt } = opt;
  const r = await fetch(cleanUrl, {
    ...requestOpt,
    headers: {
      "Content-Type": "application/json",
      ...(requestOpt.headers || {}),
    },
  });
  if (r.status === 401 && !keepUnauthorized) {
    location.href = "/login";
    throw Error("登录已过期，请重新登录");
  }
  let b = {};
  try {
    b = await r.json();
  } catch {}
  if (!r.ok) throw Error(b.detail || `请求失败 ${r.status}`);
  return b;
}
async function boot() {
  try {
    state.overview = await api("/api/overview");
    document.querySelector("#systemState span").textContent = "系统正常";
    render();
  } catch (e) {
    document.querySelector("#systemState span").textContent = "需要检查";
    app.innerHTML = `<section class="error-card"><h1>启动信息读取失败</h1><p>${esc(e.message)}</p><button class="primary" onclick="location.reload()">重新加载</button></section>`;
  }
}
function nav(page) {
  if (!state.overview) {
    toast("正在读取数据，请稍候");
    return;
  }
  state.page = page;
  document
    .querySelectorAll("[data-page]")
    .forEach((e) => e.classList.toggle("active", e.dataset.page === page));
  render();
  scrollTo({ top: 0, behavior: "smooth" });
}
document.addEventListener("click", (e) => {
  const t = e.target.closest("[data-page]");
  if (t) nav(t.dataset.page);
});
document.querySelector("#globalSearch").onsubmit = (e) => {
  e.preventDefault();
  state.query = new FormData(e.currentTarget).get("q").trim();
  nav("search");
  search();
};

function render() {
  if (state.page === "home") return home();
  const [a, b] = titles[state.page];
  app.innerHTML = `<header class="page-heading"><div><span>飞海网盘 · FNOS</span><h1>${a}</h1><p>${b}</p></div><em>数据保存在本机 NAS</em></header>${{ search: searchPage, following, library, accounts, risk, settings }[state.page]()}`;
  bind();
}
function providerCards() {
  return state.overview.providers
    .map((x) => {
      const m = pm[x.name],
        ok = x.configured;
      return `<button class="provider-mini" data-page="accounts"><i style="background:${m.color}">${m.short}</i><span><b>${m.label}</b><small>${ok ? x.account_mask || "已连接" : "等待授权"}</small></span><em class="${x.risk_status === "normal" ? "ok" : x.risk_status.includes("unreachable") ? "warn" : "muted"}">${x.risk_status === "normal" ? "正常" : ok ? "待检测" : "未连接"}</em></button>`;
    })
    .join("");
}
function poster(x) {
  const pic = x.poster
    ? `<img src="${esc(x.poster)}" alt="${esc(x.title)}" loading="lazy">`
    : `<div class="poster-placeholder"><span>${esc((x.title || "影")[0])}</span><small>飞海网盘</small></div>`;
  return `<button class="movie-card" data-movie='${esc(JSON.stringify(x))}'><div class="poster">${pic}<span class="rank">#${x.rank || "·"}</span><span class="score">★ ${x.score || "—"}</span><div class="poster-hover">查看详情</div></div><h3>${esc(x.title)}</h3><p>${esc(x.year || "待定")} · ${x.media_type === "movie" ? "电影" : "剧集"}</p></button>`;
}
function rankingPagination(r) {
  const page = Number(r.page || 1),
    total = Math.max(1, Math.min(500, Number(r.total_pages || 1))),
    first = Math.max(1, Math.min(page - 2, total - 4)),
    last = Math.min(total, first + 4),
    pages = [];
  for (let value = first; value <= last; value++) pages.push(value);
  return `<nav class="ranking-pagination" aria-label="影视排行翻页"><span>每页 24 部 · 第 ${page} / ${total} 页</span><div><button data-ranking-page="${page - 1}" ${page <= 1 ? "disabled" : ""}>← 上一页</button>${pages.map((value) => `<button data-ranking-page="${value}" class="${value === page ? "active" : ""}">${value}</button>`).join("")}<button data-ranking-page="${page + 1}" ${page >= total ? "disabled" : ""}>下一页 →</button></div></nav>`;
}
function rankingFilters() {
  const selected = (name, value) =>
      state.rankingFilters[name] === String(value) ? "selected" : "",
    currentYear = new Date().getFullYear(),
    years = Array.from({ length: currentYear - 1949 }, (_, index) => currentYear - index),
    genres = [
      ["action", "动作冒险"], ["animation", "动画"], ["comedy", "喜剧"],
      ["crime", "犯罪"], ["documentary", "纪录片"], ["drama", "剧情"],
      ["family", "家庭"], ["mystery", "悬疑"], ["romance", "爱情"],
      ["scifi", "科幻奇幻"],
    ],
    countries = [
      ["CN", "中国大陆"], ["HK", "中国香港"], ["TW", "中国台湾"],
      ["US", "美国"], ["GB", "英国"], ["JP", "日本"], ["KR", "韩国"],
      ["IN", "印度"], ["FR", "法国"], ["DE", "德国"],
    ];
  return `<form id="rankingFilters" class="ranking-filters"><label><span>年份</span><select name="year"><option value="">全部年份</option>${years.map((year) => `<option value="${year}" ${selected("year", year)}>${year}年</option>`).join("")}</select></label><label><span>类型</span><select name="genre"><option value="">全部类型</option>${genres.map(([value, label]) => `<option value="${value}" ${selected("genre", value)}>${label}</option>`).join("")}</select></label><label><span>国家/地区</span><select name="country"><option value="">全部国家/地区</option>${countries.map(([value, label]) => `<option value="${value}" ${selected("country", value)}>${label}</option>`).join("")}</select></label><button type="button" id="resetRankingFilters" ${Object.values(state.rankingFilters).some(Boolean) ? "" : "disabled"}>清除筛选</button></form>`;
}
function home() {
  const r = state.overview.ranking,
    x = r.items || [],
    hero = x[0] || {},
    has = x.length > 0;
  app.innerHTML = `<section class="hero ${has ? "" : "hero-empty"}" ${hero.backdrop ? `style="background-image:url('${esc(hero.backdrop)}')"` : ""}><div class="hero-shade"></div><div class="hero-copy"><span>${has ? `最新排行 · #${hero.rank || 1}` : "实时榜单尚未启用"}</span><h1>${esc(hero.title || "连接 TMDB")}</h1><p class="meta">${has ? `${esc(hero.year || "")} · ${hero.media_type === "movie" ? "电影" : "剧集"} · TMDB ${hero.score || "—"} 分` : "首页不会使用示例影视或虚构排名"}</p><p>${esc(hero.overview || r.message || "请在设置中填写 TMDB API 密钥，测试成功后立即显示真实海报榜单。")}</p><div>${has ? `<button class="primary" data-movie='${esc(JSON.stringify(hero))}'>查看与搜索资源</button><button class="glass" data-follow-title="${esc(hero.title || "")}" data-follow-type="${hero.media_type || "tv"}">+ 订阅追更</button>` : '<button class="primary" data-page="settings">前往 TMDB 设置</button>'}</div></div><div class="hero-badge"><b>${r.live ? "实时" : "未连接"}</b><span>${r.live ? `更新于 ${date(r.updated_at)}` : "没有展示假数据"}</span></div></section><section class="provider-strip"><header><div><span>网盘连接</span><small>独立使用，不进行跨盘秒传</small></div><button data-page="accounts">管理账号 →</button></header><div class="provider-grid">${providerCards()}</div></section><section class="content-section"><header class="section-head"><div><span>全量影视排行</span><h2>最新上映与热门内容</h2><p>不限制日期，按上映或首播时间从新到旧；同日按热度排序</p></div><div class="segments">${[
    ["all", "全部"],
    ["movie", "电影"],
    ["tv", "剧集"],
  ]
    .map(
      ([k, v]) =>
        `<button data-ranking="${k}" class="${k === state.rankingType ? "active" : ""}">${v}</button>`,
    )
    .join(
      "",
    )}</div></header>${rankingFilters()}${has ? `<div class="poster-grid">${x.map(poster).join("")}</div>${rankingPagination(r)}` : '<div class="empty-state compact"><h2>没有符合筛选的内容</h2><p>可以清除年份、类型或国家/地区后重试；未配置 TMDB 时请先前往设置。</p></div>'}</section><section class="pipeline-card"><div><span>自动化流程</span><h2>最新来源自动进入飞牛影视</h2><p>检测网站明确判定失效的来源自动隐藏，其余来源保留并显示检测状态。</p></div><div class="pipeline"><span><i>⌕</i><b>发现资源</b></span><em>→</em><span><i>✓</i><b>有效性检测</b></span><em>→</em><span><i>✦</i><b>命名刮削</b></span><em>→</em><span><i>▦</i><b>飞牛影视</b></span></div></section>`;
  bind();
}

async function loadRanking(type, page) {
  if (state.rankingLoading) return;
  state.rankingLoading = true;
  try {
    const params = new URLSearchParams({ media_type: type, page: String(page) });
    Object.entries(state.rankingFilters).forEach(([key, value]) => {
      if (value) params.set(key, value);
    });
    const ranking = await api(`/api/tmdb/trending?${params}`);
    state.overview.ranking = ranking;
    state.rankingType = type;
    state.rankingPage = ranking.page || page;
    home();
    document
      .querySelector(".content-section")
      ?.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    toast(error.message, "error");
  } finally {
    state.rankingLoading = false;
  }
}

function searchPage() {
  return `<section class="search-panel"><form id="resourceSearch"><span>⌕</span><input name="q" value="${esc(state.query)}" placeholder="输入电影、剧集、动漫或综艺名称" required><button class="primary">搜索</button></form><p>通过你在后台连接的 Pansou 搜索，再交给你的检测网站筛除失效链接。</p></section><section id="searchOutput">${state.search ? results() : `<div class="empty-state"><i>⌕</i><h2>搜索全网影视资源</h2><p>请先在“设置”中连接自己的 Pansou 和检测网站</p></div>`}</section>`;
}
function results() {
  const works = state.search.works || [],
    all = state.search.resources || [],
    p = state.search.progress || {},
    detector = state.search.detector || {},
    list =
      state.provider === "all"
        ? all
        : all.filter((x) => x.provider === state.provider);
  return `<div class="result-tabs"><button class="active">影视作品 <em>${works.length}</em></button><button>可用网盘资源 <em>${all.length}</em></button></div><div class="search-progress ${detector.status === "unavailable" ? "service-error" : ""}"><b>${detector.status === "unavailable" ? "检测服务异常，已保留全部未判失效资源" : "Pansou 搜索与外部检测完成"}</b><span>发现 ${p.discovered || 0}</span><span class="ok">有效 ${p.valid || 0}</span><span>暂不可验证 ${p.unverifiable || 0}</span><span>已失效并隐藏 ${p.invalid || 0}</span>${detector.message ? `<small>${esc(detector.message)}</small>` : ""}</div><div class="work-row">${works.slice(0, 5).map(poster).join("") || '<p class="muted-copy">未配置 TMDB 或没有匹配作品。</p>'}</div><div class="filters"><b>仅隐藏检测网站明确判定失效的资源</b>${[
    ["all", "全部"],
    ["115", "115"],
    ["baidu", "百度"],
    ["quark", "夸克"],
    ["china_mobile", "移动"],
  ]
    .map(
      ([k, v]) =>
        `<button data-provider-filter="${k}" class="${state.provider === k ? "active" : ""}">${v}</button>`,
    )
    .join(
      "",
    )}<span>共 ${list.length} 条 · 已去重</span></div><div class="resource-list">${list.map(resource).join("") || '<div class="empty-state compact"><p>没有可显示的资源；检测网站明确判定失效的链接已隐藏。</p></div>'}</div>`;
}
function resource(x) {
  const m = pm[x.provider],
    work = matchedWork(x),
    episode =
      x.episode > 0
        ? `S${String(x.season || 1).padStart(2, "0")}E${String(x.episode).padStart(2, "0")}`
        : "集数待识别",
    validation =
      x.validation_state === "valid"
        ? { cls: "valid", label: "✓ 检测有效", hint: x.provider === "115" ? "优先来源" : "可用来源" }
        : x.validation_state === "detector_unavailable"
          ? { cls: "pending", label: "! 检测服务异常", hint: "未判定失效" }
          : { cls: "pending", label: "↻ 暂不可验证", hint: "未判定失效" },
    posterImage = work?.poster
      ? `<img src="${esc(work.poster)}" alt="${esc(work.title)}" loading="lazy">`
      : `<span class="resource-poster-placeholder" style="background:${m.color}">${m.short}</span>`;
  return `<article class="resource-row"><button class="resource-poster" data-resource-detail="${esc(x.fingerprint)}" aria-label="查看${esc(work?.title || x.normalized_title || x.title)}简介">${posterImage}<em>查看简介</em></button><div class="resource-main"><div><span>${m.label}</span><small>${esc(x.source)}</small></div><button class="resource-title" data-resource-detail="${esc(x.fingerprint)}">${esc(work?.title || x.normalized_title || x.title)}</button>${work ? `<small class="resource-meta">${esc(work.year || "待定")} · ${work.media_type === "movie" ? "电影" : "剧集"} · TMDB ${work.score || "—"}</small>` : '<small class="resource-meta">暂未匹配到 TMDB 影视信息</small>'}<p><b>${esc(x.quality || "画质待识别")}</b><b>${episode}</b><b>已去重</b></p></div><div class="resource-status ${validation.cls}"><span>${validation.label}</span><small>${validation.hint}</small></div><div class="row-actions"><button data-resource-detail="${esc(x.fingerprint)}">查看简介</button><button data-copy="${esc(x.url)}">复制链接</button><button class="primary" data-intake='${esc(JSON.stringify(x))}'>一键入库</button></div></article>`;
}

function normalizedMediaTitle(value = "") {
  return String(value).normalize("NFKC").toLowerCase().replace(/[^0-9a-z\u4e00-\u9fff]+/g, "");
}

function matchedWork(resource) {
  if (resourceWorkCache.has(resource.fingerprint))
    return resourceWorkCache.get(resource.fingerprint);
  const works = state.search?.works || [],
    expected = normalizedMediaTitle(resource.normalized_title || resource.title);
  if (!expected) return null;
  const direct = works.find((work) => {
      const title = normalizedMediaTitle(work.title);
      return title && (title.includes(expected) || expected.includes(title));
    });
  if (direct) resourceWorkCache.set(resource.fingerprint, direct);
  return direct || null;
}

function resourceLookupTitle(resource) {
  if (resource.recognition_state === "recognized" && resource.normalized_title)
    return resource.normalized_title;
  let value = String(resource.title || "").replace(/^【[^】]+】\s*/, "").trim();
  const named = value.match(/名称[：:]\s*([^。\.]+?)(?:[。\.]描述[：:]|$)/);
  if (named?.[1]) value = named[1].trim();
  value = value.replace(/^【[^】]+】\s*/, "")
    .split(/(?:[。\.]?描述[：:]|[。\.]?链接[：:])/)[0]
    .replace(/\s+(?:全\s*\d+\s*集|更新至?\s*\d+\s*集|S\d{1,2}E\d+|第\d+季|(?:19|20)\d{2}|4K|2160P|1080P|国语|中字).*$/i, "")
    .trim();
  return (value || resource.normalized_title || state.query).slice(0, 100);
}

async function loadResourceWork(resource) {
  const local = matchedWork(resource);
  if (local) return local;
  if (resourceWorkRequests.has(resource.fingerprint))
    return resourceWorkRequests.get(resource.fingerprint);
  const request = api(`/api/tmdb/search?q=${encodeURIComponent(resourceLookupTitle(resource))}`)
    .then((works) => {
      const expected = normalizedMediaTitle(resourceLookupTitle(resource));
      const work =
        works.find((item) => {
          const title = normalizedMediaTitle(item.title);
          return title && (title.includes(expected) || expected.includes(title));
        }) || works[0] || null;
      resourceWorkCache.set(resource.fingerprint, work);
      return work;
    })
    .catch(() => null)
    .finally(() => resourceWorkRequests.delete(resource.fingerprint));
  resourceWorkRequests.set(resource.fingerprint, request);
  return request;
}

function hydrateResourcePosters() {
  const observer = new IntersectionObserver((entries) => {
    entries.filter((entry) => entry.isIntersecting).forEach(async (entry) => {
      observer.unobserve(entry.target);
      const resource = (state.search?.resources || []).find(
        (item) => item.fingerprint === entry.target.dataset.resourceDetail,
      );
      if (!resource || matchedWork(resource)) return;
      const work = await loadResourceWork(resource);
      if (!work || !entry.target.isConnected) return;
      if (work.poster)
        entry.target.innerHTML = `<img src="${esc(work.poster)}" alt="${esc(work.title)}" loading="lazy"><em>查看简介</em>`;
      const row = entry.target.closest(".resource-row");
      const meta = row?.querySelector(".resource-meta");
      if (meta)
        meta.textContent = `${work.year || "待定"} · ${work.media_type === "movie" ? "电影" : "剧集"} · TMDB ${work.score || "—"}`;
    });
  }, { rootMargin: "240px 0px" });
  document.querySelectorAll(".resource-poster").forEach((item) => {
    if (!item.querySelector("img")) observer.observe(item);
  });
}

async function resourceDetails(resource) {
  let work = matchedWork(resource);
  if (!work) {
    modalRoot.innerHTML = `<div class="modal-backdrop"><section class="detail-modal resource-detail-modal"><button class="modal-close">×</button><div class="empty-state compact"><span class="spinner"></span><h2>正在匹配影视资料</h2><p>从 TMDB 获取真实海报和简介…</p></div></section></div>`;
    modalRoot.querySelector(".modal-close").onclick = close;
    work = await loadResourceWork(resource);
  }
  const m = pm[resource.provider],
    title = work?.title || resource.normalized_title || resource.title || "未知影视",
    visual = work?.backdrop || work?.poster || "",
    validation =
      resource.validation_state === "valid"
        ? "检测网站确认有效"
        : resource.validation_state === "detector_unavailable"
          ? "检测服务暂时不可用，未判定失效"
          : "暂不可验证，未判定失效";
  modalRoot.innerHTML = `<div class="modal-backdrop"><section class="detail-modal resource-detail-modal"><button class="modal-close">×</button><div class="detail-visual">${visual ? `<img src="${esc(visual)}" alt="${esc(title)}">` : ""}<div></div><span><small>${m.label} · ${esc(validation)}</small><h2>${esc(title)}</h2><p>${work ? `${esc(work.year || "待定")} · ${work.media_type === "movie" ? "电影" : "剧集"} · TMDB ${work.score || "—"}` : "暂未匹配到 TMDB 影视信息"}</p><p>${esc(work?.overview || "TMDB 暂无该影视简介，网盘资源仍可复制或入库。")}</p></span></div><div class="resource-detail-info"><span><b>网盘来源</b>${m.label}</span><span><b>画质</b>${esc(resource.quality || "待识别")}</span><span><b>搜索来源</b>${esc(resource.source || "未知")}</span></div><footer><button data-copy-detail>复制链接</button><button class="primary" data-intake-detail>一键入库</button></footer></section></div>`;
  modalRoot.querySelector(".modal-close").onclick = close;
  modalRoot.querySelector("[data-copy-detail]").onclick = () =>
    navigator.clipboard.writeText(resource.url).then(() => toast("链接已复制"));
  modalRoot.querySelector("[data-intake-detail]").onclick = () => {
    close();
    intake(resource);
  };
}

function following() {
  const xs = state.overview.subscriptions;
  return `<div class="summary-grid"><article><b>${xs.length}</b><span>正在追更</span></article><article><b>${xs.filter((x) => x.current_episode > 0).length}</b><span>已发现更新</span></article><article><b>${xs.reduce((n, x) => n + x.sources.length, 0)}</b><span>全部来源</span></article><button class="primary" id="newFollow">+ 添加追更</button></div><div class="follow-grid">${
    xs
      .map(
        (x) =>
          `<article class="follow-card"><header><span class="green-chip">${x.enabled ? "追更中" : "已暂停"}</span><button data-toggle-follow="${x.id}" data-enabled="${!x.enabled}">${x.enabled ? "暂停" : "恢复"}</button></header><h2>${esc(x.keyword)}</h2><p>当前最新 <b>S${String(x.season).padStart(2, "0")}E${String(x.current_episode).padStart(2, "0")}</b></p><div class="progress"><i style="width:${Math.min(100, x.current_episode * 3)}%"></i></div><div class="source-stack">${
            x.sources
              .slice(0, 4)
              .map(
                (s) =>
                  `<span class="${s.active ? "active" : ""}"><i style="background:${pm[s.provider].color}">${pm[s.provider].short}</i><b>${pm[s.provider].label}</b><em>S${String(s.season).padStart(2, "0")}E${String(s.episode).padStart(2, "0")}</em>${s.active ? "<small>当前使用</small>" : ""}</span>`,
              )
              .join("") || "<p>等待首次搜索来源</p>"
          }</div><footer><button data-add-source="${x.id}">+ 添加备用链接</button><small>${date(x.last_checked_at)}</small></footer></article>`,
      )
      .join("") ||
    '<div class="empty-state"><i>◎</i><h2>还没有追更</h2><p>添加影视后，系统会定时搜索多个网盘链接并自动选最新。</p><button class="primary" id="newFollow">添加第一个追更</button></div>'
  }</div>`;
}
function library() {
  return `<section class="library-hero"><div><span>飞牛影视目录</span><h2>${esc(state.overview.settings.fnos_library_path || "/app/strm")}</h2><p>STRM 与 NFO 保存在本机目录，由飞牛影视直接扫描。</p></div><button class="primary" id="previewNaming">查看命名示例</button></section><div class="metric-grid"><article><span>最近任务</span><b>${state.overview.jobs.length}</b><small>任务与整理记录</small></article><article><span>追更影视</span><b>${state.overview.subscriptions.length}</b><small>自动检查更新</small></article><article><span>可用网盘</span><b>${state.overview.providers.filter((x) => x.configured).length}</b><small>已授权账号</small></article><article><span>风控事件</span><b>${state.overview.risk_events.length}</b><small>最近检测记录</small></article></div><section class="naming-card"><div><h3>统一命名规则</h3><pre>电影/流浪地球 (2019)/流浪地球 (2019).strm\n电视剧/庆余年 (2019)/Season 01/\n  庆余年 (2019) - S01E01.strm\n  庆余年 (2019) - S01E01.nfo\n  庆余年 (2019) - S01E01.zh-CN.srt</pre></div><div><h3>最近活动</h3>${
    state.overview.jobs
      .slice(0, 6)
      .map(
        (j) =>
          `<p><i class="${j.status}"></i><span><b>${esc(j.title)}</b><small>${esc(j.detail.message || j.kind)}</small></span><time>${date(j.created_at)}</time></p>`,
      )
      .join("") || "<p>暂无任务</p>"
  }</div></section>`;
}
function accounts() {
  return `<section class="accounts-intro"><div><h2>飞牛挂载、扫码与 Token 登录</h2><p>优先使用飞牛文件管理的原生网盘挂载；Token、Cookie 和授权信息只作为备用方式。</p></div><span><i></i>${state.overview.providers.filter((x) => x.configured).length} / 4 已连接</span></section><div class="accounts-grid">${state.overview.providers
    .map((x) => {
      const m = pm[x.name],
        ok = x.configured,
        ready = (x.auth_methods || []).length > 0;
      const method = x.native_mount
        ? "飞牛原生挂载"
        : ok && x.auth_method === "token"
          ? "Token / Cookie"
          : ready
            ? authMethodLabel(x.auth_methods[0])
            : "Token / Cookie";
      const authButton = x.native_mount
        ? `<button data-auth-guide="${x.name}">飞牛挂载详情</button>`
        : `<button ${ready ? `data-auth="${x.name}"` : `data-auth-guide="${x.name}"`}>${ready ? "扫码 / 官方授权" : "授权说明"}</button>`;
      return `<article class="account-card"><header><i style="background:${m.color}">${m.short}</i><span><h3>${m.label}</h3><p>${esc(x.account_mask || "尚未授权")}</p></span><em class="${ok ? "connected" : "offline"}">${ok ? "已连接" : "未连接"}</em></header><dl><div><dt>可用方式</dt><dd>${method}</dd></div><div><dt>风控状态</dt><dd>${esc(x.risk_status || "未检测")}</dd></div><div><dt>默认顺序</dt><dd>第 ${Object.keys(pm).indexOf(x.name) + 1} 位</dd></div></dl><footer>${authButton}<button class="primary" data-token-auth="${x.name}">${x.native_mount ? "备用 Token" : ok ? "更新 Token" : "Token 登录"}</button></footer></article>`;
    })
    .join(
      "",
    )}</div><aside class="security-note"><span>⌾</span><div><b>Token、Cookie 与扫码凭证都只保存在本机</b><p>请只填写从对应网盘官方客户端或官方网页取得的凭证；页面和日志不会回显原文。</p></div></aside>`;
}
function authMethodLabel(x) {
  return (
    {
      qr: "页面内二维码",
      oauth: "官方一键授权",
      gateway_qr: "页面内二维码",
      gateway_password: "账号密码登录",
      token: "Token / Cookie",
      fnos_mount: "飞牛原生挂载",
    }[x] || x
  );
}
function risk() {
  const xs = state.overview.providers,
    n = xs.filter((x) => x.risk_status === "normal").length;
  return `<section class="risk-hero"><div class="risk-score"><b>${n === xs.length ? "A" : "B"}</b><small>当前评级</small></div><div><span>网盘风控检测</span><h2>${n} 个正常 · ${xs.length - n} 个待检查</h2><p>异常时停止或降频，保留原来源并发送 Telegram 通知。</p></div><button class="primary" id="riskScan">立即检测</button></section><div class="risk-stats">${[
    ["green", "正常账号", n],
    ["orange", "待检查", xs.length - n],
    ["red", "已阻断", xs.filter((x) => x.risk_status === "blocked").length],
    [
      "blue",
      "备用来源",
      state.overview.subscriptions.reduce(
        (a, x) => a + Math.max(0, x.sources.length - 1),
        0,
      ),
    ],
  ]
    .map(
      (x) =>
        `<article><i class="${x[0]}"></i><span>${x[1]}</span><b>${x[2]}</b></article>`,
    )
    .join(
      "",
    )}</div><section class="risk-table"><header><h3>最近检测记录</h3><span>不会绕过验证码或频率限制</span></header>${state.overview.risk_events.map((x) => `<div><i class="${x.level}">${x.level === "safe" ? "✓" : "!"}</i><span><b>${pm[x.provider]?.label || "系统"}</b><small>${esc(x.event_type)}</small></span><p>${esc(x.message)}</p><em>${esc(x.action)}</em><time>${date(x.created_at)}</time></div>`).join("") || '<div class="empty-row">点击“立即检测”生成第一份风险报告。</div>'}</section>`;
}
function settings() {
  const s = {
    telegram_enabled: true,
    auto_metadata: true,
    auto_subtitles: true,
    auto_organize: true,
    fnos_library_path: "/app/strm",
    naming_language: "zh-CN",
    tmdb_language: "zh-CN",
    tmdb_region: "CN",
    ...state.overview.settings,
  };
  if (s.tmdb_region === "TW") s.tmdb_region = "zh-TW";
  return `<div class="settings-grid"><form id="tmdbForm" class="settings-card full"><header><i>影</i><div><h3>TMDB 影视数据</h3><p>首页海报、全量排行、影视详情和自动识别；保存后立即生效，无需重启容器</p></div><em class="setting-state ${s.tmdb_configured ? "connected" : "offline"}">${s.tmdb_configured ? "已连接" : "未配置"}</em></header><label class="field"><div class="field-heading"><span>TMDB API 密钥</span><nav><a href="https://www.themoviedb.org/settings/api" target="_blank" rel="noreferrer">获取密钥 ↗</a><button type="button" id="tmdbGuide">填写教程</button></nav></div><div class="secret-field"><input type="password" name="api_key" placeholder="${s.tmdb_configured ? "已保存；留空表示不更换" : "请输入 API Key (v3 auth)"}"><button type="button" data-toggle-secret>显示</button></div></label><label class="field"><span>语言</span><select name="language"><option value="zh-CN" ${s.tmdb_language === "zh-CN" ? "selected" : ""}>简体中文</option><option value="zh-TW" ${s.tmdb_language === "zh-TW" ? "selected" : ""}>繁体中文</option></select></label><label class="field"><span>地区</span><select name="region"><option value="CN" ${s.tmdb_region === "CN" ? "selected" : ""}>中国大陆</option><option value="HK" ${s.tmdb_region === "HK" ? "selected" : ""}>中国香港</option><option value="TW" ${s.tmdb_region === "zh-TW" ? "selected" : ""}>中国台湾</option></select></label><label class="field"><span>首页排行</span><div class="setting-static"><b>全量内容，不限制日期</b><small>上映或首播时间从新到旧；同一天按热度从高到低</small></div></label><div class="settings-actions"><button type="button" id="testTmdb">测试连接</button><button class="primary">保存并刷新首页</button></div></form><form id="pansouForm" class="settings-card full"><header><i>搜</i><div><h3>连接自己的 Pansou</h3><p>只使用你的 Pansou 搜索 Telegram 与插件资源；飞海网盘不再内置 Pansou</p></div><em class="setting-state ${s.pansou_configured ? "connected" : "offline"}">${s.pansou_configured ? "已连接" : "未配置"}</em></header><label class="field"><span>Pansou 地址</span><input name="base_url" type="url" required value="${esc(s.pansou_base_url || "")}" placeholder="例如：http://192.168.100.225:8888"></label><label class="field"><span>搜索接口</span><input name="api_path" required value="${esc(s.pansou_api_path || "/api/search")}"></label><label class="field"><span>搜索来源</span><select name="source"><option value="all" ${s.pansou_source === "all" ? "selected" : ""}>全部来源</option><option value="tg" ${s.pansou_source === "tg" ? "selected" : ""}>Telegram频道</option><option value="plugin" ${s.pansou_source === "plugin" ? "selected" : ""}>搜索插件</option></select></label><label class="field"><span>账号（可选）</span><input name="username" autocomplete="off" placeholder="Pansou未启用登录时留空"></label><label class="field"><span>密码（可选）</span><div class="secret-field"><input type="password" name="password" autocomplete="new-password" placeholder="${s.pansou_auth_configured ? "已保存；留空表示不更换" : "可留空"}"><button type="button" data-toggle-secret>显示</button></div></label><label class="field"><span>Token（可选）</span><div class="secret-field"><input type="password" name="token" autocomplete="off" placeholder="${s.pansou_auth_configured ? "已保存；留空表示不更换" : "Bearer Token，可留空"}"><button type="button" data-toggle-secret>显示</button></div></label><div class="settings-actions"><button type="button" id="testPansou">测试已保存连接</button><button class="primary">测试并保存</button></div></form><form id="checkerForm" class="settings-card full"><header><i>验</i><div><h3>连接自己的网盘检测网站</h3><p>兼容 PanCheck 的批量检测接口；失效资源隐藏，服务异常时不会误判为失效</p></div><em class="setting-state ${s.checker_configured ? "connected" : "offline"}">${s.checker_configured ? "已连接" : "未配置"}</em></header><label class="field"><span>检测网站地址</span><input name="base_url" type="url" required value="${esc(s.checker_base_url || "")}" placeholder="例如：http://192.168.100.225:6080"></label><label class="field"><span>批量检测接口</span><input name="api_path" required value="${esc(s.checker_api_path || "/api/v1/links/check")}"></label><label class="field"><span>Token（可选）</span><div class="secret-field"><input type="password" name="token" autocomplete="off" placeholder="${s.checker_auth_configured ? "已保存；留空表示不更换" : "检测网站无需鉴权时留空"}"><button type="button" data-toggle-secret>显示</button></div></label><label class="field"><span>检测超时（秒）</span><input name="timeout_seconds" type="number" min="5" max="120" value="${s.checker_timeout_seconds || 35}"></label><label class="field"><span>结果缓存（分钟）</span><input name="cache_minutes" type="number" min="0" max="10080" value="${s.checker_cache_minutes ?? 120}"></label><div class="settings-actions"><button type="button" id="testChecker">测试已保存连接</button><button class="primary">测试并保存</button></div></form><form id="settingsForm" class="settings-card full"><header><i>▦</i><div><h3>通知、自动整理与飞牛影视</h3><p>保存后只影响后续任务，不自动移动或删除已有文件</p></div></header><label class="toggle-row"><span><b>Telegram 通知</b><small>追更、入库失败和风控异常提醒</small></span><input type="checkbox" name="telegram_enabled" ${s.telegram_enabled ? "checked" : ""}><i></i></label>${[
    ["auto_metadata", "自动匹配 TMDB 信息"],
    ["auto_subtitles", "自动整理中文字幕"],
    ["auto_organize", "入库后自动生成 STRM / NFO"],
  ]
    .map(
      ([n, l]) =>
        `<label class="toggle-row"><span><b>${l}</b></span><input type="checkbox" name="${n}" ${s[n] ? "checked" : ""}><i></i></label>`,
    )
    .join(
      "",
    )}<label class="field"><span>飞牛影视映射目录</span><input name="fnos_library_path" value="${esc(s.fnos_library_path)}"></label><label class="field"><span>命名语言</span><select name="naming_language"><option value="zh-CN">简体中文</option></select></label><div class="path-preview">电视剧/庆余年 (2019)/Season 01/庆余年 (2019) - S01E01.strm</div><div class="settings-actions"><button type="button" id="testNotify">发送测试通知</button><button class="primary">保存设置</button></div></form></div>`;
}

function showTmdbGuide() {
  modalRoot.innerHTML = `<div class="modal-backdrop"><section class="form-modal tmdb-guide"><button class="modal-close">×</button><small>TMDB 官方密钥</small><h2>获取与填写教程</h2><ol><li><b>打开 TMDB 密钥页面</b><span>点击下方“打开官方页面”，登录或注册 TMDB 账号。</span></li><li><b>申请开发者 API</b><span>在 API 页面按官方提示填写用途；个人飞牛 NAS 可如实说明为私人媒体整理。</span></li><li><b>复制正确的密钥</b><span>请复制 <strong>API Key (v3 auth)</strong>，不要复制较长的 API Read Access Token。</span></li><li><b>粘贴并保存</b><span>返回飞海网盘，把密钥粘贴到输入框，点击“保存并刷新首页”。</span></li></ol><aside>密钥只会加密保存在你的飞牛 NAS，不会提交到 GitHub。</aside><div class="guide-actions"><a class="primary" href="https://www.themoviedb.org/settings/api" target="_blank" rel="noreferrer">打开 TMDB 官方密钥页面 ↗</a><a href="https://developer.themoviedb.org/docs/authentication-application" target="_blank" rel="noreferrer">查看官方说明</a></div></section></div>`;
  modalRoot.querySelector(".modal-close").onclick = close;
}

function showProviderAuthGuide(provider) {
  const account = state.overview.providers.find((item) => item.name === provider);
  if (account?.native_mount) {
    const label = pm[provider]?.label || account.label;
    modalRoot.innerHTML = `<div class="modal-backdrop"><section class="form-modal provider-auth-guide"><button class="modal-close">×</button><small>已安全接入 · 飞牛原生挂载</small><h2>${esc(label)}已连接</h2><p>飞海网盘直接使用飞牛文件管理已经登录的网盘挂载，不需要再次扫码，也不需要填写账号密码。</p><ol><li>登录状态由飞牛文件管理负责维护。</li><li>入库时可以逐级选择这个网盘里的目标文件夹。</li><li>挂载断开时，飞海网盘会停止对应任务并提示重新连接。</li></ol><aside>飞海网盘只映射网盘目录，不安装 OpenList，也不会新增管理端口。</aside><div class="guide-actions"><button type="button" class="primary guide-close">知道了</button></div></section></div>`;
    modalRoot.querySelector(".modal-close").onclick = close;
    modalRoot.querySelector(".guide-close").onclick = close;
    return;
  }
  const guides = {
    baidu: {
      status: "可安全接入 · 需要一次性准备",
      title: "百度网盘官方授权",
      summary:
        "百度要求每个接入程序先拥有官方应用信息。配置一次后，账号页就能打开百度官方授权页面，不需要填写 Cookie 或 Token。",
      steps: [
        "登录百度网盘开放平台并创建“软件”类型应用。",
        "取得 AppKey 与 SecretKey，并配置授权回调地址。",
        "在飞海网盘中加密保存应用信息，然后点击百度官方授权。",
      ],
      link: "https://pan.baidu.com/union/console/applist",
      linkLabel: "打开百度网盘开放平台 ↗",
    },
    quark: {
      status: "暂不开放 · 等待本机直连方案",
      title: "夸克网盘扫码限制",
      summary:
        "夸克网页版可以扫码，但没有向私人 NAS 提供可完成挂载的官方授权接口。现有扫码挂载方案会经过第三方服务，不符合凭据只保存在飞牛 NAS 的要求。",
      steps: [
        "当前不要求你粘贴 Cookie、Token 或浏览器数据。",
        "不会把二维码授权结果发送给第三方中转。",
        "出现可靠的本机直连方式后，再启用“立即登录”。",
      ],
      link: "https://pan.quark.cn/",
      linkLabel: "查看夸克网盘官网 ↗",
    },
    china_mobile: {
      status: "暂不开放 · 官方个人授权能力不足",
      title: "中国移动云盘授权限制",
      summary:
        "中国移动开放平台面向申请接入的应用；现有个人盘长期连接方式仍依赖账号密码和邮箱登录凭据，无法只靠普通扫码安全完成。",
      steps: [
        "当前不会让你手工填写邮箱 Cookie 或 Authorization。",
        "不会绕过短信、验证码或账号风控。",
        "获得官方应用接入能力后，可改为账号授权或手机号验证码流程。",
      ],
      link: "https://open.yun.139.com/",
      linkLabel: "查看中国移动云盘开放平台 ↗",
    },
  };
  const guide = guides[provider];
  if (!guide) return;
  modalRoot.innerHTML = `<div class="modal-backdrop"><section class="form-modal provider-auth-guide"><button class="modal-close">×</button><small>${guide.status}</small><h2>${guide.title}</h2><p>${guide.summary}</p><ol>${guide.steps.map((step) => `<li>${step}</li>`).join("")}</ol><aside>飞海网盘会优先选择本机、安全、可持续的授权方式；功能未接通时会明确显示原因。</aside><div class="guide-actions"><a class="primary" href="${guide.link}" target="_blank" rel="noreferrer">${guide.linkLabel}</a><button type="button" class="guide-close">知道了</button></div></section></div>`;
  modalRoot.querySelector(".modal-close").onclick = close;
  modalRoot.querySelector(".guide-close").onclick = close;
}

function bind() {
  document
    .querySelectorAll("[data-movie]")
    .forEach((e) => (e.onclick = () => movie(JSON.parse(e.dataset.movie))));
  document
    .querySelectorAll("[data-follow-title]")
    .forEach(
      (e) =>
        (e.onclick = () =>
          quickFollow(e.dataset.followTitle, e.dataset.followType)),
    );
  document
    .querySelectorAll("[data-ranking]")
    .forEach((e) => (e.onclick = () => loadRanking(e.dataset.ranking, 1)));
  document
    .querySelectorAll("[data-ranking-page]")
    .forEach(
      (e) =>
        (e.onclick = () =>
          loadRanking(state.rankingType, Number(e.dataset.rankingPage))),
    );
  const rankingFilterForm = document.querySelector("#rankingFilters");
  if (rankingFilterForm)
    rankingFilterForm.onchange = () => {
      const data = new FormData(rankingFilterForm);
      state.rankingFilters = {
        year: String(data.get("year") || ""),
        genre: String(data.get("genre") || ""),
        country: String(data.get("country") || ""),
      };
      loadRanking(state.rankingType, 1);
    };
  const resetRankingFilters = document.querySelector("#resetRankingFilters");
  if (resetRankingFilters)
    resetRankingFilters.onclick = () => {
      state.rankingFilters = { year: "", genre: "", country: "" };
      loadRanking(state.rankingType, 1);
    };
  const sf = document.querySelector("#resourceSearch");
  if (sf)
    sf.onsubmit = (e) => {
      e.preventDefault();
      state.query = new FormData(sf).get("q").trim();
      search();
    };
  document.querySelectorAll("[data-provider-filter]").forEach(
    (e) =>
      (e.onclick = () => {
        state.provider = e.dataset.providerFilter;
        render();
      }),
  );
  document.querySelectorAll("[data-resource-detail]").forEach(
    (e) =>
      (e.onclick = () => {
        const resource = (state.search?.resources || []).find(
          (item) => item.fingerprint === e.dataset.resourceDetail,
        );
        if (resource) resourceDetails(resource);
      }),
  );
  document
    .querySelectorAll("[data-copy]")
    .forEach(
      (e) =>
        (e.onclick = () =>
          navigator.clipboard
            .writeText(e.dataset.copy)
            .then(() => toast("链接已复制"))),
    );
  document
    .querySelectorAll("[data-intake]")
    .forEach((e) => (e.onclick = () => intake(JSON.parse(e.dataset.intake))));
  document
    .querySelectorAll("#newFollow")
    .forEach((e) => (e.onclick = newFollow));
  document
    .querySelectorAll("[data-add-source]")
    .forEach((e) => (e.onclick = () => addSource(Number(e.dataset.addSource))));
  document.querySelectorAll("[data-toggle-follow]").forEach(
    (e) =>
      (e.onclick = async () => {
        await api(
          `/api/subscriptions/${e.dataset.toggleFollow}?enabled=${e.dataset.enabled}`,
          { method: "PATCH" },
        );
        await refresh("追更状态已更新");
      }),
  );
  document
    .querySelectorAll("[data-auth]")
    .forEach((e) => (e.onclick = () => startAuth(e.dataset.auth)));
  document
    .querySelectorAll("[data-auth-guide]")
    .forEach(
      (e) => (e.onclick = () => showProviderAuthGuide(e.dataset.authGuide)),
    );
  document
    .querySelectorAll("[data-token-auth]")
    .forEach((e) => (e.onclick = () => tokenLogin(e.dataset.tokenAuth)));
  const rs = document.querySelector("#riskScan");
  if (rs) rs.onclick = runRisk;
  const pn = document.querySelector("#previewNaming");
  if (pn) pn.onclick = showNaming;
  const set = document.querySelector("#settingsForm");
  if (set) set.onsubmit = saveSettings;
  const tmdb = document.querySelector("#tmdbForm");
  if (tmdb) tmdb.onsubmit = saveTmdb;
  const pansou = document.querySelector("#pansouForm");
  if (pansou) pansou.onsubmit = savePansou;
  const checker = document.querySelector("#checkerForm");
  if (checker) checker.onsubmit = saveChecker;
  const tmdbGuide = document.querySelector("#tmdbGuide");
  if (tmdbGuide) tmdbGuide.onclick = showTmdbGuide;
  document.querySelectorAll("[data-toggle-secret]").forEach(
    (e) =>
      (e.onclick = () => {
        const i = e.parentElement.querySelector("input");
        i.type = i.type === "password" ? "text" : "password";
        e.textContent = i.type === "password" ? "显示" : "隐藏";
      }),
  );
  const nt = document.querySelector("#testNotify");
  if (nt) nt.onclick = testNotify;
  const tt = document.querySelector("#testTmdb");
  if (tt) tt.onclick = testTmdb;
  const tp = document.querySelector("#testPansou");
  if (tp) tp.onclick = testPansou;
  const tc = document.querySelector("#testChecker");
  if (tc) tc.onclick = testChecker;
  if (document.querySelector(".resource-poster")) hydrateResourcePosters();
}
async function search() {
  const out = document.querySelector("#searchOutput");
  if (out)
    out.innerHTML =
      '<div class="empty-state"><span class="spinner"></span><h2>正在搜索并检查有效性</h2><p>你的 Pansou、检测网站和 TMDB</p></div>';
  try {
    state.search = await api(
      `/api/search?q=${encodeURIComponent(state.query)}`,
    );
    render();
  } catch (e) {
    out.innerHTML = `<div class="error-card"><h2>搜索未完成</h2><p>${esc(e.message)}</p></div>`;
  }
}
async function quickFollow(title, type = "tv") {
  if (!title) return;
  try {
    await api("/api/subscriptions", {
      method: "POST",
      body: JSON.stringify({
        keyword: title,
        auto_intake: true,
        media_type: type === "movie" ? "movie" : "tv",
      }),
    });
    await refresh(`已订阅追更：${title}`);
    nav("following");
  } catch (e) {
    toast(e.message, "error");
  }
}
function movie(x) {
  modalRoot.innerHTML = `<div class="modal-backdrop"><section class="detail-modal"><button class="modal-close">×</button><div class="detail-visual">${x.backdrop ? `<img src="${esc(x.backdrop)}">` : ""}<div></div><span><small>影视详情</small><h2>${esc(x.title || "未知影视")}</h2><p>${esc(x.year || "")} · ${x.media_type === "movie" ? "电影" : "剧集"} · TMDB ${x.score || "—"}</p><p>${esc(x.overview || "暂无简介")}</p></span></div><footer><button class="search-this">搜索网盘资源</button><button class="primary follow-this">+ 订阅追更</button></footer></section></div>`;
  modalRoot.querySelector(".modal-close").onclick = close;
  modalRoot.querySelector(".search-this").onclick = () => {
    state.query = x.title;
    close();
    nav("search");
    search();
  };
  modalRoot.querySelector(".follow-this").onclick = () => {
    close();
    quickFollow(x.title, x.media_type);
  };
}
function newFollow() {
  formModal(
    "添加自动追更",
    `<label>影视名称<input name="keyword" required placeholder="例如：庆余年 第二季"></label><label>类型<select name="media_type"><option value="tv">电视剧</option><option value="movie">电影</option><option value="anime">动漫</option><option value="variety">综艺</option></select></label><label>年份（可选）<input name="year" type="number" min="1900" max="2200"></label>`,
    async (d) => {
      d.auto_intake = true;
      if (!d.year) delete d.year;
      else d.year = Number(d.year);
      await api("/api/subscriptions", {
        method: "POST",
        body: JSON.stringify(d),
      });
      await refresh("追更已添加");
    },
  );
}
function addSource(id) {
  formModal(
    "添加备用网盘链接",
    `<label>分享链接<input name="share_url" type="url" required placeholder="115、百度、夸克或移动网盘链接"></label><div class="form-pair"><label>季<input name="season" type="number" value="1" min="0"></label><label>最新集<input name="episode" type="number" value="0" min="0"></label></div><label>资源标题<input name="title" placeholder="用于自动识别画质和集数"></label>`,
    async (d) => {
      d.season = Number(d.season);
      d.episode = Number(d.episode);
      await api(`/api/subscriptions/${id}/sources`, {
        method: "POST",
        body: JSON.stringify(d),
      });
      await refresh("备用来源已添加，已重新选择最新来源");
    },
  );
}
function intake(x, target = "") {
  const m = pm[x.provider];
  formModal(
    "一键入库",
    `<div class="chosen-resource"><i style="background:${m.color}">${m.short}</i><span><b>${esc(state.query || x.normalized_title || x.title)}</b><small>${m.label}链接只能存入${m.label}</small></span></div><label>影视名称<input name="title" value="${esc(state.query || x.normalized_title || x.title)}" required></label><input type="hidden" name="share_url" value="${esc(x.url)}"><label>目标网盘<input value="${m.label}" disabled></label><label>目标目录<div class="directory-input"><input name="target_folder" value="${esc(target)}" placeholder="请逐级选择真实目录" readonly required><button type="button" id="pickDirectory">选择目录</button></div></label><aside class="form-warning">确认时会再次交给你的检测网站验证；只有明确判定失效或网盘未授权时才会阻止入库。</aside>`,
    async (d) => {
      d.auto_organize = true;
      await api("/api/intake", { method: "POST", body: JSON.stringify(d) });
      await refresh(`已加入 ${m.label} 入库队列`);
    },
  );
  document.querySelector("#pickDirectory").onclick = () =>
    directoryPicker(x, target);
}
async function directoryPicker(x, path = "") {
  const m = pm[x.provider];
  modalRoot.innerHTML = `<div class="modal-backdrop"><section class="form-modal directory-modal"><button class="modal-close">×</button><h2>选择 ${m.label} 文件夹</h2><p class="directory-path">正在读取真实目录…</p><div class="directory-list"><span class="spinner"></span></div><button class="primary submit-modal" disabled>选择当前目录</button></section></div>`;
  const box = modalRoot.querySelector(".directory-modal"),
    list = box.querySelector(".directory-list"),
    current = box.querySelector(".directory-path"),
    choose = box.querySelector(".submit-modal");
  box.querySelector(".modal-close").onclick = () => intake(x, path);
  try {
    const data = await api(`/api/providers/${x.provider}/directories`, {
      method: "POST",
      body: JSON.stringify({ path: path || "/" }),
    });
    path = data.path;
    current.textContent = path;
    choose.disabled = false;
    list.innerHTML = `${path !== data.root ? '<button class="directory-up">← 返回上一级</button>' : ""}${data.directories.map((d) => `<button class="directory-item" data-path="${esc(d.path)}"><span>▣</span><b>${esc(d.name)}</b><em>进入 →</em></button>`).join("") || "<p>这个目录没有子文件夹</p>"}`;
    list
      .querySelectorAll(".directory-item")
      .forEach((e) => (e.onclick = () => directoryPicker(x, e.dataset.path)));
    const up = list.querySelector(".directory-up");
    if (up)
      up.onclick = () =>
        directoryPicker(
          x,
          path.substring(0, path.lastIndexOf("/")) || data.root,
        );
    choose.onclick = () => intake(x, path);
  } catch (e) {
    current.textContent = "无法读取目录";
    list.innerHTML = `<div class="form-error">${esc(e.message)}</div><button class="directory-up">返回入库设置</button>`;
    list.querySelector("button").onclick = () => intake(x, path);
  }
}
async function startAuth(p) {
  try {
    const x = await api(`/api/providers/${p}/auth/start`, { method: "POST" }),
      m = pm[p],
      data = x.public_payload || {};
    modalRoot.innerHTML = `<div class="modal-backdrop"><section class="auth-modal"><button class="modal-close">×</button><i class="auth-logo" style="background:${m.color}">${m.short}</i><h2>${m.label}登录</h2>${data.qr_image_url ? `<img class="auth-qr" src="${esc(data.qr_image_url)}" alt="${m.label}登录二维码"><p class="auth-status">${esc(data.message || "等待扫码")}</p><button class="auth-refresh" type="button">刷新二维码</button>` : data.authorize_url ? `<p>${esc(data.message)}</p><a class="primary auth-link" href="${esc(data.authorize_url)}" target="_blank" rel="noreferrer">打开官方授权页面</a><small>授权结果接入完成后会自动更新，无需粘贴 Token 或 Cookie。</small>` : '<div class="setup-box">当前登录方式尚未完成，请等待后续版本启用。</div>'}</section></div>`;
    modalRoot.querySelector(".modal-close").onclick = close;
    const refreshButton = modalRoot.querySelector(".auth-refresh");
    if (refreshButton) refreshButton.onclick = () => startAuth(p);
    if (data.qr_image_url) pollAuth(x.id, p);
  } catch (e) {
    toast(e.message, "error");
  }
}
function tokenLogin(p) {
  const m = pm[p],
    labels = {
      115: "Access Token 或 Cookie",
      baidu: "Access Token",
      quark: "Cookie 或 Token",
      china_mobile: "Token 或 Cookie",
    };
  formModal(
    `${m.label} Token 登录`,
    `<aside class="form-warning">凭证会使用飞牛 NAS 本机密钥加密保存。请勿填写网盘账号密码。</aside><label>${labels[p]}<textarea name="credential" required minlength="6" autocomplete="off" placeholder="粘贴从${m.label}官方客户端或官方网页取得的凭证"></textarea></label>`,
    async (d) => {
      await api(`/api/providers/${p}/credential`, {
        method: "POST",
        body: JSON.stringify({
          credential: d.credential.trim(),
          account_mask: `${m.label} · Token 已保存`,
        }),
      });
      await refresh(`${m.label} Token 已加密保存`);
    },
  );
}
async function pollAuth(id, p) {
  for (let i = 0; i < 90 && modalRoot.querySelector(".auth-modal"); i++) {
    await new Promise((r) => setTimeout(r, 2000));
    try {
      const x = await api(`/api/providers/auth/${id}`),
        node = modalRoot.querySelector(".auth-status");
      if (node) node.textContent = x.public_payload?.message || x.state;
      if (x.state === "succeeded") {
        await refresh(`${pm[p].label}登录成功`);
        close();
        return;
      }
      if (["expired", "canceled", "failed"].includes(x.state)) return;
    } catch (e) {
      const node = modalRoot.querySelector(".auth-status");
      if (node) node.textContent = e.message;
      return;
    }
  }
}
function showNaming() {
  modalRoot.innerHTML = `<div class="modal-backdrop"><section class="form-modal"><button class="modal-close">×</button><h2>飞牛影视命名示例</h2><div class="tree-preview"><b>电视剧 / 庆余年 (2019) /</b><span>Season 01 /</span><em>庆余年 (2019) - S01E01.strm</em><em>庆余年 (2019) - S01E01.nfo</em><em>庆余年 (2019) - S01E01.zh-CN.srt</em><span>poster.jpg · fanart.jpg · tvshow.nfo</span></div></section></div>`;
  modalRoot.querySelector(".modal-close").onclick = close;
}
function formModal(title, fields, submit) {
  modalRoot.innerHTML = `<div class="modal-backdrop"><form class="form-modal"><button type="button" class="modal-close">×</button><h2>${title}</h2>${fields}<p class="form-error"></p><button class="primary submit-modal">确认</button></form></div>`;
  const f = modalRoot.querySelector("form");
  f.querySelector(".modal-close").onclick = close;
  f.onsubmit = async (e) => {
    e.preventDefault();
    const b = f.querySelector(".submit-modal");
    b.disabled = true;
    b.textContent = "正在处理…";
    try {
      await submit(Object.fromEntries(new FormData(f)));
      close();
    } catch (x) {
      f.querySelector(".form-error").textContent = x.message;
      b.disabled = false;
      b.textContent = "确认";
    }
  };
}
function close() {
  modalRoot.innerHTML = "";
}
async function runRisk() {
  const b = document.querySelector("#riskScan");
  b.disabled = true;
  b.textContent = "正在检测…";
  try {
    await api("/api/providers/risk-scan", { method: "POST" });
    await refresh("风控检测完成");
  } catch (e) {
    toast(e.message, "error");
  }
}
async function saveSettings(e) {
  e.preventDefault();
  const f = e.currentTarget,
    d = Object.fromEntries(new FormData(f));
  [
    "telegram_enabled",
    "auto_metadata",
    "auto_subtitles",
    "auto_organize",
  ].forEach((n) => (d[n] = f.elements[n].checked));
  try {
    await api("/api/settings", { method: "PUT", body: JSON.stringify(d) });
    await refresh("设置已保存");
  } catch (x) {
    toast(x.message, "error");
  }
}
async function savePansou(e) {
  e.preventDefault();
  const f = e.currentTarget,
    b = f.querySelector("button.primary"),
    d = Object.fromEntries(new FormData(f));
  d.clear_credentials = false;
  b.disabled = true;
  b.textContent = "正在连接并保存…";
  try {
    await api("/api/settings/pansou", {
      method: "PUT",
      body: JSON.stringify(d),
    });
    await refresh("Pansou 已连接，资源搜索将使用你的服务");
  } catch (x) {
    toast(x.message, "error");
    b.disabled = false;
    b.textContent = "测试并保存";
  }
}
async function saveChecker(e) {
  e.preventDefault();
  const f = e.currentTarget,
    b = f.querySelector("button.primary"),
    d = Object.fromEntries(new FormData(f));
  d.timeout_seconds = Number(d.timeout_seconds);
  d.cache_minutes = Number(d.cache_minutes);
  d.clear_token = false;
  b.disabled = true;
  b.textContent = "正在连接并保存…";
  try {
    await api("/api/settings/checker", {
      method: "PUT",
      body: JSON.stringify(d),
    });
    await refresh("检测网站已连接，搜索会自动隐藏失效资源");
  } catch (x) {
    toast(x.message, "error");
    b.disabled = false;
    b.textContent = "测试并保存";
  }
}
async function saveTmdb(e) {
  e.preventDefault();
  const f = e.currentTarget,
    b = f.querySelector("button.primary");
  b.disabled = true;
  b.textContent = "正在测试并保存…";
  try {
    await api("/api/settings/tmdb", {
      method: "PUT",
      body: JSON.stringify(Object.fromEntries(new FormData(f))),
    });
    await refresh("TMDB 已连接，首页榜单已刷新");
  } catch (x) {
    toast(x.message, "error");
    b.disabled = false;
    b.textContent = "保存并刷新首页";
  }
}
async function testTmdb() {
  try {
    const x = await api("/api/settings/tmdb/test", { method: "POST" });
    toast(`TMDB 连接正常，读取到 ${x.items} 条榜单`);
  } catch (e) {
    toast(e.message, "error");
  }
}
async function testPansou() {
  try {
    await api("/api/settings/pansou/test", { method: "POST" });
    toast("Pansou 连接正常");
  } catch (e) {
    toast(e.message, "error");
  }
}
async function testChecker() {
  try {
    await api("/api/settings/checker/test", { method: "POST" });
    toast("检测网站连接正常");
  } catch (e) {
    toast(e.message, "error");
  }
}
async function testNotify() {
  try {
    const x = await api("/api/notify", {
      method: "POST",
      body: JSON.stringify({
        message: "飞海网盘测试通知：飞牛 NAS 连接正常。",
      }),
    });
    toast(
      x.configured ? "测试通知已发送" : "尚未配置 Telegram Bot",
      x.configured ? "ok" : "error",
    );
  } catch (e) {
    toast(e.message, "error");
  }
}
async function refresh(msg) {
  state.overview = await api("/api/overview");
  render();
  if (msg) toast(msg);
}
const gate = document.querySelector("#sessionGate"),
  gateForm = document.querySelector("#sessionGateForm"),
  gateKey = "feihai-tab-verified";
function startVerifiedApp() {
  gate.hidden = true;
  document.body.classList.remove("session-locked");
  boot();
}
async function verifyCurrentTab(e) {
  e.preventDefault();
  const button = gateForm.querySelector("button"),
    error = gateForm.querySelector(".gate-error"),
    password = new FormData(gateForm).get("password");
  button.disabled = true;
  button.textContent = "正在验证…";
  error.textContent = "";
  try {
    await api("/api/verify-password", {
      method: "POST",
      body: JSON.stringify({ password }),
      keepUnauthorized: true,
    });
    sessionStorage.setItem(gateKey, "1");
    startVerifiedApp();
  } catch (x) {
    error.textContent = x.message;
    button.disabled = false;
    button.innerHTML = "验证并进入 <i>→</i>";
    gateForm.password.select();
  }
}
function requireTabVerification() {
  const params = new URLSearchParams(location.search);
  if (params.get("verified") === "1") {
    sessionStorage.setItem(gateKey, "1");
    history.replaceState({}, "", location.pathname);
  }
  if (sessionStorage.getItem(gateKey) === "1") startVerifiedApp();
  else {
    document.body.classList.add("session-locked");
    gate.hidden = false;
    gateForm.onsubmit = verifyCurrentTab;
    setTimeout(() => gateForm.password.focus(), 50);
  }
}
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") close();
});
requireTabVerification();
