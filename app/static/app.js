/* TopicFeed UI — vanilla ES2020, no build step. */
"use strict";

const state = {
  feeds: [],
  topics: [],
  entries: [],
  issues: [],
  fixtures: [],
  filters: { topic: null, feed: null, q: "", absent: true },
  selectedEntryId: null,
  checked: new Set(),
  entryDetail: null,
  busy: false,
  serverTime: null,
};

const $ = (id) => document.getElementById(id);

/* ------------------------------------------------------------ utilities */

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
}

function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return esc(iso);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function fmtRelative(iso) {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const delta = Math.round((Date.now() - then) / 1000);
  if (delta < 60) return "刚刚";
  if (delta < 3600) return `${Math.floor(delta / 60)} 分钟前`;
  if (delta < 86400) return `${Math.floor(delta / 3600)} 小时前`;
  if (delta < 86400 * 30) return `${Math.floor(delta / 86400)} 天前`;
  return "";
}

function toast(message, kind = "ok", ttl = 4200) {
  const stack = $("toast-stack");
  const node = document.createElement("div");
  node.className = `toast ${kind}`;
  node.innerHTML = `<span>${kind === "err" ? "⚠" : kind === "warn" ? "◔" : "✓"}</span><span>${esc(message)}</span>`;
  stack.appendChild(node);
  setTimeout(() => {
    node.style.opacity = "0";
    node.style.transition = "opacity .2s";
    setTimeout(() => node.remove(), 220);
  }, ttl);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: options.body ? { "Content-Type": "application/json" } : {},
    ...options,
  });
  const contentType = response.headers.get("Content-Type") || "";
  if (!contentType.includes("application/json")) {
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response;
  }
  const payload = await response.json();
  if (!response.ok) {
    const detail = payload?.error?.detail || `HTTP ${response.status}`;
    throw new Error(detail);
  }
  return payload;
}

function queryString() {
  const params = new URLSearchParams();
  if (state.filters.topic) params.set("topic", state.filters.topic);
  if (state.filters.feed) params.set("feed", state.filters.feed);
  if (state.filters.q) params.set("q", state.filters.q);
  if (!state.filters.absent) params.set("absent", "0");
  const qs = params.toString();
  return qs ? `?${qs}` : "";
}

/* ------------------------------------------------------------------ load */

async function loadState({ quiet = false } = {}) {
  const data = await api(`/api/state${queryString()}`);
  state.feeds = data.feeds;
  state.topics = data.topics;
  state.entries = data.entries;
  state.issues = data.issues;
  state.fixtures = data.fixtures;
  state.serverTime = data.server_time;
  state.scheduler = data.scheduler;
  // Drop selections that no longer exist so exports stay accurate.
  const visibleIds = new Set(state.entries.map((e) => e.id));
  for (const id of [...state.checked]) if (!visibleIds.has(id)) state.checked.delete(id);
  if (state.selectedEntryId && !visibleIds.has(state.selectedEntryId)) {
    const stillThere = state.entries.some((e) => e.id === state.selectedEntryId);
    if (!stillThere) state.selectedEntryId = null;
  }
  renderAll();
}

function renderAll() {
  renderTopics();
  renderFeeds();
  renderEntries();
  renderSelection();
  if (state.selectedEntryId) renderDetail(state.selectedEntryId);
}

/* ---------------------------------------------------------------- topics */

function renderTopics() {
  const node = $("topic-list");
  const total = state.entries.length;
  const all = state.topics.reduce((sum, t) => sum + t.entries, 0);
  const chips = [`<button class="topic-chip" data-topic="" aria-pressed="${!state.filters.topic}">
      <span>全部</span><span class="count">${all}</span></button>`];
  for (const topic of state.topics) {
    const active = state.filters.topic === topic.topic;
    chips.push(`<button class="topic-chip" data-topic="${esc(topic.topic)}" aria-pressed="${active}">
      <span>${esc(topic.topic)}</span><span class="count">${topic.entries}</span>
      ${topic.paused_feeds ? '<span class="badge badge-warn" title="有暂停的源">暂停</span>' : ""}
    </button>`);
  }
  node.innerHTML = chips.join("");
  for (const button of node.querySelectorAll(".topic-chip")) {
    button.addEventListener("click", () => {
      const topic = button.dataset.topic || null;
      state.filters.topic = state.filters.topic === topic ? null : topic;
      state.filters.feed = null;
      loadState().catch((error) => toast(error.message, "err"));
    });
  }
}

/* ----------------------------------------------------------------- feeds */

function feedStatusBadge(feed) {
  if (feed.paused) return '<span class="badge badge-warn">已暂停</span>';
  if (feed.last_result === "failed") return '<span class="badge badge-bad">失败</span>';
  if (feed.last_result === "parse_error") return '<span class="badge badge-bad">坏 XML</span>';
  if (feed.last_result === "not_modified") return '<span class="badge badge-muted">未变化</span>';
  if (feed.last_result === "ok") return '<span class="badge badge-ok">正常</span>';
  return '<span class="badge badge-muted">未抓取</span>';
}

function feedItemHtml(feed) {
  const versionOptions = fixtureVersionOptions(feed);
  const active = String(state.filters.feed) === String(feed.id);
  const issues = feed.active_issues
    ? `<span class="badge badge-warn">${feed.active_issues} 个问题</span>` : "";
  const failed = feed.last_result === "failed" || feed.last_result === "parse_error";
  const retryBadge = failed && feed.next_retry_at
    ? `<span class="badge badge-muted" title="自动重试时间">重试 ${fmtTime(feed.next_retry_at)}</span>`
    : "";
  const errorLine = failed && feed.last_error_detail
    ? `<div class="feed-error" title="${esc(feed.last_error_detail)}">
         ${esc(issueLabel(feed.last_error_code))}：${esc(feed.last_error_detail)}
       </div>`
    : "";
  return `
  <div class="feed-item ${active ? "active" : ""}" role="group" aria-current="${active}">
    <div class="feed-title-row">
      <button class="feed-name" data-open-feed="${feed.id}" title="${esc(feed.url)}">${esc(feed.title)}</button>
      ${feedStatusBadge(feed)}
    </div>
    <div class="feed-meta">
      <span>${feed.entry_count} 篇</span>
      ${feed.revised_count ? `<span class="dot">·</span><span>${feed.revised_count} 篇有修订</span>` : ""}
      ${feed.absent_count ? `<span class="dot">·</span><span>${feed.absent_count} 篇不在最新一轮</span>` : ""}
      ${issues ? `<span class="dot">·</span>${issues}` : ""}
      ${retryBadge}
    </div>
    ${errorLine}
    <div class="feed-meta">
      ${feed.kind === "fixture" ? `
        <label class="version-pick">版本
          <select data-version-feed="${feed.id}">${versionOptions}</select>
        </label>` : ""}
      <button class="link-btn" data-refresh-feed="${feed.id}">刷新</button>
      ${failed && feed.next_retry_at ? `<button class="link-btn" data-clear-backoff="${feed.id}">清除重试计时</button>` : ""}
      <button class="link-btn" data-toggle-feed="${feed.id}" data-paused="${feed.paused ? 1 : 0}">
        ${feed.paused ? "恢复" : "暂停"}
      </button>
    </div>
  </div>`;
}

function fixtureVersionOptions(feed) {
  const fixture = state.fixtures.find((f) => f.slug === feed.fixture_slug);
  if (!fixture) return "";
  return fixture.versions.map((v) => `
    <option value="${esc(v.id)}" ${v.id === feed.fixture_version ? "selected" : ""}
      title="${esc(v.notes || v.label)}">${esc(v.label)}</option>`).join("");
}

function renderFeeds() {
  const node = $("feed-list");
  if (!state.feeds.length) {
    node.innerHTML = '<p class="hint">还没有订阅源，可以从下方添加。</p>';
    return;
  }
  node.innerHTML = state.feeds.map(feedItemHtml).join("");

  node.querySelectorAll("[data-open-feed]").forEach((el) => {
    el.addEventListener("click", () => {
      const id = Number(el.dataset.openFeed);
      state.filters.feed = state.filters.feed === id ? null : id;
      loadState().catch((error) => toast(error.message, "err"));
    });
  });
  node.querySelectorAll("[data-refresh-feed]").forEach((el) => {
    el.addEventListener("click", () => refreshFeed(Number(el.dataset.refreshFeed), el));
  });
  node.querySelectorAll("[data-toggle-feed]").forEach((el) => {
    el.addEventListener("click", async () => {
      const id = Number(el.dataset.toggleFeed);
      const paused = el.dataset.paused === "1";
      try {
        await api(`/api/feeds/${id}/pause`, {
          method: "POST",
          body: JSON.stringify({ paused: !paused }),
        });
        toast(paused ? "已恢复抓取" : "已暂停该源；已收录文章保留");
        await loadState();
      } catch (error) { toast(error.message, "err"); }
    });
  });
  node.querySelectorAll("[data-clear-backoff]").forEach((el) => {
    el.addEventListener("click", async () => {
      const id = Number(el.dataset.clearBackoff);
      try {
        await api(`/api/feeds/${id}/clear-backoff`, { method: "POST", body: "{}" });
        toast("已清除重试计时，下一次定时刷新会立即尝试");
        await loadState();
      } catch (error) { toast(error.message, "err"); }
    });
  });
  node.querySelectorAll("[data-version-feed]").forEach((el) => {
    el.addEventListener("change", async () => {
      const id = Number(el.dataset.versionFeed);
      const version = el.value;
      el.disabled = true;
      try {
        const result = await api(`/api/feeds/${id}/version`, {
          method: "POST",
          body: JSON.stringify({ version }),
        });
        describeResult(result.result);
        await loadState();
      } catch (error) {
        toast(error.message, "err");
      } finally {
        el.disabled = false;
      }
    });
  });
}

/* --------------------------------------------------------------- entries */

function entryCardHtml(entry) {
  const checked = state.checked.has(entry.id);
  const selected = state.selectedEntryId === entry.id;
  const badges = [];
  if (entry.revision_count > 1) badges.push(`<span class="badge badge-purple">${entry.revision_count} 个修订</span>`);
  if (entry.active_issues) badges.push('<span class="badge badge-warn">有问题</span>');
  if (entry.absent_from_latest) badges.push('<span class="badge badge-muted" title="该文章未出现在最近一轮抓取结果中，但记录已保留">旧文保留</span>');
  if (entry.republished) badges.push('<span class="badge badge-info" title="源中新出现，但发布时间明显更早">补发旧文</span>');
  if (entry.identity_kind === "link") badges.push('<span class="badge badge-muted" title="源未提供稳定标识，使用规范化链接">链接标识</span>');
  if (entry.identity_kind === "content") badges.push('<span class="badge badge-warn" title="源未提供标识或链接">内容指纹</span>');
  return `
  <div class="entry-card ${selected ? "selected" : ""} ${entry.absent_from_latest ? "absent" : ""}"
       data-entry="${entry.id}" tabindex="0" role="button" aria-selected="${selected}">
    <input class="entry-check" type="checkbox" data-check="${entry.id}"
           ${checked ? "checked" : ""} aria-label="选择文章用于导出">
    <div>
      <h3 class="entry-title">${esc(entry.title || "（无标题）")}</h3>
      <p class="entry-summary">${esc(entry.summary || "")}</p>
      <div class="entry-meta">
        <span class="meta-feed">${esc(entry.feed_title)}</span>
        <span class="dot">·</span>
        <span title="发布时间 ${esc(entry.published_at || "未知")}">${fmtTime(entry.published_at)}</span>
        ${badges.join("")}
      </div>
    </div>
  </div>`;
}

function renderEntries() {
  const node = $("entry-list");
  if (!state.entries.length) {
    node.innerHTML = `<div class="list-empty">
      <p>没有匹配的文章。</p>
      <p>切换话题或清除搜索条件，或在左侧刷新订阅源。</p>
    </div>`;
  } else {
    node.innerHTML = state.entries.map(entryCardHtml).join("");
    node.querySelectorAll(".entry-card").forEach((card) => {
      const id = Number(card.dataset.entry);
      card.addEventListener("click", (event) => {
        if (event.target.closest(".entry-check")) return;
        selectEntry(id);
      });
      card.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          selectEntry(id);
        }
      });
    });
    node.querySelectorAll("[data-check]").forEach((box) => {
      box.addEventListener("change", () => {
        const id = Number(box.dataset.check);
        if (box.checked) state.checked.add(id); else state.checked.delete(id);
        renderSelection();
        syncCheckAll();
      });
    });
  }
  const absent = state.entries.filter((e) => e.absent_from_latest).length;
  $("list-summary").textContent =
    `${state.entries.length} 篇${absent ? ` · ${absent} 篇本轮未见` : ""}`;
  syncCheckAll();
}

function syncCheckAll() {
  const box = $("check-all");
  const visible = state.entries.map((e) => e.id);
  const checkedVisible = visible.filter((id) => state.checked.has(id)).length;
  box.checked = visible.length > 0 && checkedVisible === visible.length;
  box.indeterminate = checkedVisible > 0 && checkedVisible < visible.length;
}

function renderSelection() {
  const count = state.checked.size;
  $("selection-count").textContent = count ? `已选 ${count} 篇` : "未选择文章";
  $("export-json").disabled = count === 0;
  $("export-md").disabled = count === 0;
  const scheduler = state.scheduler;
  if (scheduler) {
    const every = scheduler.interval_seconds >= 60
      ? `${Math.round(scheduler.interval_seconds / 60)} 分钟`
      : `${scheduler.interval_seconds} 秒`;
    const last = scheduler.last_run
      ? `上次自动刷新 ${fmtTime(scheduler.last_run)}`
      : "尚未自动刷新";
    $("auto-refresh").textContent = `每 ${every} 自动检查 · ${last}`;
  }
}

function selectEntry(id) {
  state.selectedEntryId = id;
  document.querySelectorAll(".entry-card").forEach((card) => {
    const on = Number(card.dataset.entry) === id;
    card.classList.toggle("selected", on);
    card.setAttribute("aria-selected", String(on));
  });
  renderDetail(id);
}

/* ---------------------------------------------------------------- detail */

let detailRequest = 0;

async function renderDetail(id) {
  const requestId = ++detailRequest;
  const detailNode = $("entry-detail");
  const emptyNode = $("empty-state");
  try {
    const data = await api(`/api/entries/${id}`);
    if (requestId !== detailRequest) return;
    state.entryDetail = data.entry;
    detailNode.hidden = false;
    emptyNode.hidden = true;
    detailNode.innerHTML = detailHtml(data.entry);
    wireDetail(data.entry);
  } catch (error) {
    if (requestId !== detailRequest) return;
    toast(error.message, "err");
  }
}

function issueLabel(kind) {
  if (!kind) return "错误";
  const known = {
    bad_date: "异常日期",
    empty_body: "空正文",
    bad_xml: "XML 解析失败",
    missing_identity: "缺少稳定标识",
    http_error: "HTTP 错误",
    network_error: "网络错误",
    bad_url: "地址无效",
    bad_fixture: "本地源缺失",
    fixture_read_error: "本地源读取失败",
    ingest_error: "入库失败",
    internal_error: "内部错误",
  };
  if (known[kind]) return known[kind];
  const httpMatch = /^http_(\d{3})$/.exec(kind);
  if (httpMatch) return `源返回 HTTP ${httpMatch[1]}`;
  return kind;
}

function detailHtml(entry) {
  const chips = [];
  chips.push(`<span class="badge badge-info">${esc(entry.topic)}</span>`);
  chips.push(`<span class="badge badge-muted">${esc(entry.identity_kind === "guid" ? "稳定标识" : entry.identity_kind === "link" ? "链接标识" : "内容指纹")}</span>`);
  if (entry.revisions.length > 1) chips.push(`<span class="badge badge-purple">${entry.revisions.length} 个修订</span>`);
  if (entry.missing_rounds > 0) chips.push(`<span class="badge badge-muted">最近 ${entry.missing_rounds} 轮未见，记录保留</span>`);

  const notices = [];
  const pub = entry.published_at ? new Date(entry.published_at).getTime() : null;
  const seen = entry.first_seen_at ? new Date(entry.first_seen_at).getTime() : null;
  if (pub && seen && seen - pub > 30 * 86400 * 1000) {
    notices.push(`<div class="notice notice-info"><span class="notice-icon">ⓘ</span>
      <span>这是补发的旧文：发布时间是 ${fmtTime(entry.published_at)}，收录时间是 ${fmtTime(entry.first_seen_at)}。
      发布时间来自订阅源，不会被收录时间覆盖。</span></div>`);
  }
  if (entry.updated_at && entry.published_at && entry.updated_at !== entry.published_at) {
    notices.push(`<div class="notice notice-info"><span class="notice-icon">ⓘ</span>
      <span>源提供了单独的更新时间 ${fmtTime(entry.updated_at)}，与发布时间分开保存。</span></div>`);
  }
  const activeIssues = entry.issues.filter((i) => !i.resolved_at);
  for (const issue of activeIssues) {
    const klass = issue.kind === "missing_identity" ? "notice-info" : "notice-warn";
    notices.push(`<div class="notice ${klass}"><span class="notice-icon">◔</span>
      <span><strong>${esc(issueLabel(issue.kind))}</strong>：${esc(issue.detail)}
      ${issue.field ? `（字段 <code>${esc(issue.field)}</code>）` : ""}
      首次发现于 ${fmtTime(issue.first_seen_at)}，累计 ${issue.occurrences} 次。</span></div>`);
  }

  const revisionRows = entry.revisions.map((rev, index) => {
    const latest = index === entry.revisions.length - 1;
    return `<div class="revision-row ${latest ? "latest" : ""}">
      <div class="rev-badge">r${rev.revision_no}</div>
      <div>
        <div class="rev-title">${esc(rev.title || "（无标题）")}</div>
        <div class="rev-meta">观察于 ${fmtTime(rev.observed_at)} · 源更新时间 ${fmtTime(rev.updated_at)}</div>
      </div>
      <div class="rev-source">${esc(rev.source_version || "—")}</div>
    </div>`;
  }).join("");

  const issueRows = entry.issues.map((issue) => `
    <div class="issue-row">
      <span class="badge ${issue.resolved_at ? "badge-muted" : "badge-warn"}">${issue.resolved_at ? "已解决" : "未解决"}</span>
      <div>
        <div class="issue-detail"><strong>${esc(issueLabel(issue.kind))}</strong>：${esc(issue.detail)}</div>
        <div class="issue-meta">${issue.field ? `字段 ${esc(issue.field)} · ` : ""}
          首次 ${fmtTime(issue.first_seen_at)} · 最近 ${fmtTime(issue.last_seen_at)} · 出现 ${issue.occurrences} 次</div>
      </div>
    </div>`).join("");

  const linkRows = entry.links.map((link) => `
    <div class="link-row">
      <a href="${esc(link.link)}" target="_blank" rel="noopener noreferrer">${esc(link.link)}</a>
      <span class="link-when">${fmtTime(link.first_seen_at)}</span>
    </div>`).join("");

  const revOptions = entry.revisions.map(
    (rev) => `<option value="${rev.revision_no}">r${rev.revision_no}</option>`
  ).join("");

  const diffControls = entry.revisions.length > 1 ? `
    <div class="diff-controls">
      <select id="diff-left" aria-label="对比左版本">${revOptions}</select>
      <span>→</span>
      <select id="diff-right" aria-label="对比右版本">${revOptions}</select>
      <button class="btn" id="open-diff">对比</button>
    </div>` : "";

  return `
  <div class="detail-head">
    <div class="detail-chips">${chips.join("")}</div>
    <h2 class="detail-title">${esc(entry.title || "（无标题）")}</h2>
    <div class="meta-grid">
      <div class="meta-item"><span class="meta-label">来源</span><span class="meta-value">${esc(entry.feed_title)}</span></div>
      <div class="meta-item"><span class="meta-label">发布时间</span><span class="meta-value">${fmtTime(entry.published_at)}</span></div>
      <div class="meta-item"><span class="meta-label">更新时间</span><span class="meta-value">${fmtTime(entry.updated_at)}</span></div>
      <div class="meta-item"><span class="meta-label">首次收录</span><span class="meta-value">${fmtTime(entry.first_seen_at)}</span></div>
      <div class="meta-item"><span class="meta-label">最近见到</span><span class="meta-value">${fmtTime(entry.last_seen_at)} ${fmtRelative(entry.last_seen_at) ? `（${fmtRelative(entry.last_seen_at)}）` : ""}</span></div>
      <div class="meta-item"><span class="meta-label">标识</span><span class="meta-value mono">${esc(entry.identity_key)}</span></div>
    </div>
    ${notices.join("")}
  </div>

  <div class="detail-body">${entry.content_html || '<p style="color:var(--text-faint)">（该条目没有正文）</p>'}</div>

  <section class="section">
    <div class="section-head">
      <h3>修订历史</h3>
      ${diffControls}
    </div>
    <div class="revision-list">${revisionRows}</div>
  </section>

  <section class="section">
    <div class="section-head"><h3>问题记录</h3></div>
    ${issueRows ? `<div class="issue-list">${issueRows}</div>` : '<p class="hint">该文章解析正常，没有发现问题。</p>'}
  </section>

  <section class="section">
    <div class="section-head"><h3>出现过的链接</h3></div>
    <div class="links-list">${linkRows || '<p class="hint">没有记录到链接。</p>'}</div>
  </section>
  `;
}

function wireDetail(entry) {
  const openDiff = $("open-diff");
  if (openDiff) {
    const revisions = entry.revisions;
    $("diff-left").value = String(revisions[Math.max(0, revisions.length - 2)].revision_no);
    $("diff-right").value = String(revisions[revisions.length - 1].revision_no);
    openDiff.addEventListener("click", () => {
      openDiff.disabled = true;
      openDiff.innerHTML = '<span class="spin">◌</span> 对比中';
      showDiff(entry.id, Number($("diff-left").value), Number($("diff-right").value))
        .catch((error) => toast(error.message, "err"))
        .finally(() => {
          openDiff.disabled = false;
          openDiff.textContent = "对比";
        });
    });
  }
}

async function showDiff(entryId, left, right) {
  const data = await api(`/api/entries/${entryId}/diff?left=${left}&right=${right}`);
  const diff = data.diff;
  const render = (text, signClass, sign) =>
    text.split("\n").map((line) => `<span class="diff-line ${signClass}">${sign} ${esc(line)}</span>`).join("\n");

  const unified = diff.unified.length
    ? diff.unified.map((line) => {
        let klass = "ctx";
        if (line.startsWith("+++") || line.startsWith("---")) klass = "hunk";
        else if (line.startsWith("@@")) klass = "hunk";
        else if (line.startsWith("+")) klass = "add";
        else if (line.startsWith("-")) klass = "del";
        return `<span class="diff-line ${klass}">${esc(line)}</span>`;
      }).join("\n")
    : '<span class="diff-line ctx">（两次修订正文一致）</span>';

  $("modal-body").innerHTML = `
    <p class="diff-summary">
      <span class="badge badge-muted">r${diff.left.revision_no || "?"}</span>
      <span>${fmtTime(diff.left.observed_at)}</span>
      <span>→</span>
      <span class="badge badge-purple">r${diff.right.revision_no || "?"}</span>
      <span>${fmtTime(diff.right.observed_at)}</span>
      ${diff.title_changed ? '<span class="badge badge-warn">标题有变化</span>' : '<span class="badge badge-muted">标题未变</span>'}
      ${diff.identical ? '<span class="badge badge-muted">正文未变</span>' : '<span class="badge badge-info">正文有变化</span>'}
    </p>
    ${diff.title_changed ? `
      <div class="notice notice-info" style="margin:0 0 14px">
        <span class="notice-icon">ⓘ</span>
        <span>标题：<b>${esc(diff.left_title || "（无标题）")}</b> → <b>${esc(diff.right_title || "（无标题）")}</b></span>
      </div>` : ""}
    <div class="diff-columns">
      <div class="diff-col">
        <h4>r${diff.left.revision_no} · 旧</h4>
        <pre class="diff-text">${render(diff.left_text, "del", "−")}</pre>
      </div>
      <div class="diff-col">
        <h4>r${diff.right.revision_no} · 新</h4>
        <pre class="diff-text">${render(diff.right_text, "add", "+")}</pre>
      </div>
    </div>
    <h4 style="margin:18px 0 8px;font-size:11.5px;letter-spacing:.05em;text-transform:uppercase;color:var(--text-faint)">统一差异</h4>
    <pre class="diff-text">${unified}</pre>
  `;
  $("modal").hidden = false;
}

/* --------------------------------------------------------------- actions */

function describeResult(result) {
  if (!result) return;
  if (result.status === "ok") {
    const s = result.stats || {};
    toast(`抓取成功：${s.seen || 0} 篇，新增 ${s.new || 0}，变更 ${s.changed || 0}，未见 ${s.missing || 0}`);
  } else if (result.status === "not_modified") {
    toast("源未变化（304），未做任何写入", "warn");
  } else if (result.status === "paused") {
    toast("该源已暂停", "warn");
  } else if (result.status === "retry_scheduled") {
    toast(`未到重试时间：${fmtTime(result.next_retry_at)}`, "warn");
  } else {
    toast(`抓取失败：${result.error_detail || result.status}`, "err", 6500);
  }
}

async function refreshFeed(id, button) {
  const original = button.textContent;
  button.textContent = "刷新中…";
  button.disabled = true;
  try {
    const data = await api(`/api/feeds/${id}/refresh`, { method: "POST", body: "{}" });
    describeResult(data.result);
    await loadState();
  } catch (error) {
    toast(error.message, "err");
  } finally {
    button.textContent = original;
    button.disabled = false;
  }
}

async function refreshAll() {
  const button = $("refresh-all");
  button.disabled = true;
  button.innerHTML = '<span class="spin">◌</span> 刷新中';
  try {
    const data = await api("/api/refresh", { method: "POST", body: JSON.stringify({ force: true }) });
    const results = data.results || [];
    const done = results.filter((r) => r.status === "ok").length;
    const unchanged = results.filter((r) => r.status === "not_modified").length;
    const paused = results.filter((r) => r.status === "paused").length;
    const failed = results.filter((r) => !["ok", "not_modified", "paused", "retry_scheduled"].includes(r.status));
    let message = `刷新 ${results.length} 个源：成功 ${done}，未变化 ${unchanged}`;
    if (paused) message += `，暂停 ${paused}`;
    if (failed.length) message += `，失败 ${failed.length}`;
    toast(message, failed.length ? "warn" : "ok");
    if (failed.length) {
      toast(`失败源：${failed.map((f) => `#${f.feed_id} ${f.error_code || ""}`).join("；")}`, "err", 6500);
    }
    await loadState();
    if (state.selectedEntryId) renderDetail(state.selectedEntryId);
  } catch (error) {
    toast(error.message, "err");
  } finally {
    button.disabled = false;
    button.textContent = "刷新全部";
  }
}

async function exportSelection(format) {
  if (!state.checked.size) return;
  try {
    const response = await fetch("/api/export", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ids: [...state.checked], format }),
    });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      throw new Error(payload?.error?.detail || `HTTP ${response.status}`);
    }
    const blob = await response.blob();
    const disposition = response.headers.get("Content-Disposition") || "";
    const match = disposition.match(/filename="([^"]+)"/);
    const filename = match ? match[1] : `topicfeed-export.${format === "json" ? "json" : "md"}`;
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1500);
    toast(`已导出 ${state.checked.size} 篇（${filename}）`);
  } catch (error) {
    toast(error.message, "err");
  }
}

async function addFeed(event) {
  event.preventDefault();
  const url = $("add-url").value.trim();
  const topic = $("add-topic").value.trim();
  const hint = $("add-hint");
  hint.classList.remove("error");
  if (!url) {
    hint.textContent = "请填写订阅源地址。";
    hint.classList.add("error");
    return;
  }
  const submit = event.target.querySelector("button[type=submit]");
  submit.disabled = true;
  submit.textContent = "添加中…";
  try {
    const data = await api("/api/feeds", {
      method: "POST",
      body: JSON.stringify({ url, topic: topic || undefined }),
    });
    describeResult(data.result);
    $("add-url").value = "";
    $("add-topic").value = "";
    await loadState();
  } catch (error) {
    hint.textContent = error.message;
    hint.classList.add("error");
  } finally {
    submit.disabled = false;
    submit.textContent = "添加";
  }
}

async function previewFeed() {
  const url = $("add-url").value.trim();
  const box = $("preview-box");
  if (!url) {
    box.hidden = false;
    box.className = "preview-box bad";
    box.textContent = "请先填写地址。";
    return;
  }
  box.hidden = false;
  box.className = "preview-box";
  box.textContent = "正在读取…";
  try {
    const data = await api("/api/preview", {
      method: "POST",
      body: JSON.stringify({ url }),
    });
    if (!data.ok) {
      box.className = "preview-box bad";
      box.innerHTML = `<strong>${esc(issueLabel(data.error?.code))}</strong>：${esc(data.error?.detail || "无法解析")}`;
      return;
    }
    const issues = data.entries.filter((e) => (e.issues || []).length).length;
    box.className = "preview-box good";
    box.innerHTML = `
      <div><strong>${esc(data.title || "（无标题源）")}</strong> · ${data.entries.length} 个条目</div>
      ${issues ? `<div class="warn-line">其中 ${issues} 个条目带有日期或正文问题，订阅后可在问题记录里查看。</div>` : ""}
      <ul>${data.entries.slice(0, 3).map((e) => `
        <li>${esc(e.title || "（无标题）")}
          <span class="muted">${fmtTime(e.published_at)}</span></li>`).join("")}</ul>
      ${data.entries.length > 3 ? `<div class="muted">仅显示前 3 条</div>` : ""}
    `;
  } catch (error) {
    box.className = "preview-box bad";
    box.textContent = error.message;
  }
}

/* ------------------------------------------------------------------ wire */

function wire() {
  $("refresh-all").addEventListener("click", refreshAll);
  $("add-feed-form").addEventListener("submit", addFeed);
  $("preview-btn").addEventListener("click", previewFeed);
  $("export-json").addEventListener("click", () => exportSelection("json"));
  $("export-md").addEventListener("click", () => exportSelection("markdown"));
  $("check-all").addEventListener("change", (event) => {
    if (event.target.checked) state.entries.forEach((e) => state.checked.add(e.id));
    else state.entries.forEach((e) => state.checked.delete(e.id));
    renderEntries();
    renderSelection();
  });
  let searchTimer = null;
  $("search").addEventListener("input", (event) => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      state.filters.q = event.target.value.trim();
      loadState().catch((error) => toast(error.message, "err"));
    }, 220);
  });
  $("show-absent").addEventListener("change", (event) => {
    state.filters.absent = event.target.checked;
    loadState().catch((error) => toast(error.message, "err"));
  });
  $("clear-filters").addEventListener("click", () => {
    state.filters = { topic: null, feed: null, q: "", absent: state.filters.absent };
    $("search").value = "";
    loadState().catch((error) => toast(error.message, "err"));
  });
  $("modal-close").addEventListener("click", () => { $("modal").hidden = true; });
  $("modal").addEventListener("click", (event) => {
    if (event.target === $("modal")) $("modal").hidden = true;
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") $("modal").hidden = true;
    if (event.key === "/" && document.activeElement !== $("search")) {
      event.preventDefault();
      $("search").focus();
    }
  });
}

wire();
loadState().catch((error) => toast(`无法加载：${error.message}`, "err", 8000));
