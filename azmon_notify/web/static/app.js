// ── state ────────────────────────────────────────────────────────────
const S = { section: "open", severities: new Set(), query: "", showAcked: false,
            sort: "fired_desc", groupBy: "smart", withinHours: 0,
            sound: true, collapsed: new Set(), audio: null, quiet: false,
            theme: "dark", plugins: [], subs: {}, lastGroupKeys: [], byId: {},
            pimSubs: new Set() };
const SEV = ["Critical", "Error", "Warning", "Informational", "Verbose"];

const $ = (id) => document.getElementById(id);

// ── persisted UI prefs (survive reloads) ──────────────────────────────
const PREF_KEY = "azmon.prefs";
function loadPrefs() {
  try {
    const p = JSON.parse(localStorage.getItem(PREF_KEY) || "{}");
    if (typeof p.sound === "boolean") S.sound = p.sound;
    if (p.groupBy) S.groupBy = p.groupBy;
    if (p.sort) S.sort = p.sort;
    if (typeof p.withinHours === "number") S.withinHours = p.withinHours;
    if (typeof p.showAcked === "boolean") S.showAcked = p.showAcked;
    if (p.section) S.section = p.section;
    if (p.theme) S.theme = p.theme;
    if (Array.isArray(p.collapsed)) S.collapsed = new Set(p.collapsed);
  } catch { /* corrupt/absent prefs — use defaults */ }
}
function savePrefs() {
  try {
    localStorage.setItem(PREF_KEY, JSON.stringify({
      sound: S.sound, groupBy: S.groupBy, sort: S.sort, withinHours: S.withinHours,
      showAcked: S.showAcked, section: S.section, theme: S.theme,
      collapsed: [...S.collapsed],
    }));
  } catch { /* storage full/blocked — non-fatal */ }
}

// ── auth / poll health banner ─────────────────────────────────────────
async function fetchHealth() {
  try {
    const h = await (await fetch("/api/health")).json();
    S.quiet = !!h.quiet;                     // suppress the in-page beep overnight
    const bad = !h.auth_ok || h.poll_ok === false;
    $("authBanner").hidden = !bad;
    if (bad)
      $("authBannerText").textContent = h.error
        || (!h.auth_ok ? "Azure sign-in expired — run az login and restart."
                       : "Polling is failing — check the console/logs.");
  } catch { /* server unreachable — the connection dot already reflects that */ }
}

// ── api ──────────────────────────────────────────────────────────────
async function fetchAlerts() {
  const p = new URLSearchParams({
    section: S.section, severities: [...S.severities].join(","), q: S.query,
    show_acked: S.showAcked, sort: S.sort, group_by: S.groupBy,
    within_hours: S.withinHours });
  const r = await fetch("/api/alerts?" + p);
  const data = await r.json();
  S.byId = {};   // rebuilt during render, for the detail drawer lookup
  if (S.groupBy === "none") renderFlat(data); else renderGroups(data);
}
async function fetchAccount() {
  try {
    const r = await fetch("/api/account");
    const a = await r.json();
    const el = $("acctName");
    el.textContent = a.logged_in ? (a.name || "signed in") : "not signed in";
    el.title = a.logged_in
      ? [a.name, a.subscription].filter(Boolean).join(" · ")
      : "run .\\run.ps1 to az login";
    el.classList.toggle("acct-warn", !a.logged_in);
  } catch { /* transient — leave last known value on screen */ }
}
async function fetchSubs() {
  try { S.subs = (await (await fetch("/api/subscriptions")).json()) || {}; }
  catch { /* fills in after the first poll; keep whatever we have */ }
}
// ── PIM status + caution ──────────────────────────────────────────────
async function fetchPim() {
  try {
    const d = await (await fetch("/api/pim")).json();
    S.pimSubs = new Set(d.subs || []);
    const act = d.active || [], chip = $("pimChip"), sep = document.querySelector(".pim-sep");
    if (act.length) {
      chip.hidden = false; sep.hidden = false;
      chip.textContent = "PIM: YES";
      chip.title = "Activated PIM roles:\n" + act.map((p) =>
        `• ${p.role} on ${p.sub_name || p.sub}` +
        (p.expiry ? ` (until ${new Date(p.expiry).toLocaleString()})` : "")).join("\n");
    } else { chip.hidden = true; sep.hidden = true; }
  } catch { /* transient */ }
}
function subFromArm(x) {
  const m = (x || "").match(/\/subscriptions\/([0-9a-fA-F-]{36})/);
  return m ? m[1] : "";
}
function maybeCaution(sub) {
  if (!sub || !S.pimSubs.has(sub)) return;
  $("cautionText").textContent =
    `PIM is active on ${subLabel(sub)} — you're acting with elevated access.`;
  const t = $("cautionToast"); t.hidden = false;
  clearTimeout(t._t); t._t = setTimeout(() => { t.hidden = true; }, 6500);
}

// ── plugins (list / toggle / upload, inside the settings menu) ─────────
async function fetchPlugins() {
  try {
    S.plugins = (await (await fetch("/api/plugins/list")).json()).plugins || [];
    renderExtBadge();
  } catch { /* transient */ }
}
async function ack(id) {
  maybeCaution(S.byId[id] && S.byId[id].subscription);
  await fetch("/api/ack/" + encodeURIComponent(id), { method: "POST" });
  fetchAlerts();
}
async function unack(id) {
  await fetch("/api/unack/" + encodeURIComponent(id), { method: "POST" });
  fetchAlerts();
}
async function ackAll() {
  await fetch("/api/ack_all", { method: "POST" });
  fetchAlerts();
}
async function unackAll() {
  await fetch("/api/unack_all", { method: "POST" });
  fetchAlerts();
}

// ── render: summary tiles + header pulse ──────────────────────────────
function renderSummary(s) {
  const c = s.counts || {};
  $("t-total").textContent = s.total_active || 0;
  $("t-crit").textContent = c.Critical || 0;
  $("t-err").textContent = c.Error || 0;
  $("t-warn").textContent = c.Warning || 0;
  $("t-info").textContent = c.Informational || 0;
  $("t-verb").textContent = c.Verbose || 0;

  document.body.dataset.worst = (s.total_active ? s.worst : 5);
  const line = $("statusLine");
  line.textContent = s.total_active
    ? `${s.total_active} active · worst: ${SEV[s.worst] || "—"}`
    : "All clear";
  const updatedEl = $("updated");
  updatedEl._ts = s.last_updated ? s.last_updated * 1000 : null;
  updatedEl.textContent = s.last_updated
    ? "updated " + rel(s.last_updated * 1000) : "—";

  // hint when still-firing alerts are hidden because they were dismissed
  const hidden = s.dismissed_hidden || 0;
  const showHint = hidden > 0 && !S.showAcked && S.section === "open";
  $("dismissHint").hidden = !showHint;
  $("unackAll").hidden = hidden === 0;
  if (showHint)
    $("dismissHintText").textContent =
      `${hidden} active alert${hidden > 1 ? "s are" : " is"} hidden as dismissed.`;
}

// ── render: grouped / flat alerts ──────────────────────────────────────
function updateEmptyText() {
  const closed = S.section === "closed";
  $("emptyTitle").textContent = closed ? "Nothing resolved yet" : "Nothing's on fire";
  $("emptyBody").textContent = closed
    ? "Alerts move here once Azure reports them resolved. Nothing's closed yet."
    : "No active alerts match your filter. New ones land here the moment they fire.";
}
function groupSevChips(alerts) {
  const counts = {};
  for (const a of alerts) counts[a.sev_label] = (counts[a.sev_label] || 0) + 1;
  return SEV.filter((l) => counts[l]).map((l) =>
    `<span class="hchip s${sevIdx(l)}" title="${l}: ${counts[l]}">${counts[l]}</span>`
  ).join("");
}
// newest activity in a group (resolved time for closed, else fired time)
function groupLatest(alerts) {
  let m = 0;
  for (const a of alerts) {
    const t = (a.condition === "Resolved" && a.resolved_at)
      ? a.resolved_at * 1000 : Date.parse(a.fired_at);
    if (t && t > m) m = t;
  }
  return m;
}
function renderGroups(groups) {
  updateEmptyText();
  const root = $("groups");
  root.innerHTML = "";
  $("empty").hidden = groups.length > 0;
  S.lastGroupKeys = groups.map((g) => g.key);
  for (const g of groups) {
    const el = document.createElement("div");
    el.className = `group s${g.worst}` + (S.collapsed.has(g.key) ? " collapsed" : "")
      + (g.muted ? " muted" : "");
    const latest = groupLatest(g.alerts);
    const vmA = g.alerts.find(isVM);          // representative VM / log alert
    const lqA = g.alerts.find(isLogQuery);    //   for the header action icons
    const hdrGraph = vmA
      ? `<button class="metrics-btn" data-res="${esc(vmA.target_resource)}" data-name="${esc(vmA.resource_name || vmA.target_resource_name)}" title="Resource metrics">
          <svg viewBox="0 0 24 24" class="ic"><path d="M4 19V5M4 19h16M8 15l3-4 3 3 4-6"/></svg>
        </button>` : "";
    const hdrTable = lqA
      ? `<button class="metrics-btn logq-btn" data-rule="${esc(lqA.rule)}" data-name="${esc(lqA.rule_name)}" title="Query results">
          <svg viewBox="0 0 24 24" class="ic"><path d="M4 5h16v14H4zM4 10h16M4 15h16M10 5v14"/></svg>
        </button>` : "";
    const muteKey = g.alerts[0].mute_key || g.key;
    const hdrMute = g.muted
      ? `<button class="metrics-btn unmute-btn" data-key="${esc(muteKey)}" title="Unmute this rule">
          <svg viewBox="0 0 24 24" class="ic"><path d="M11 5L6 9H3v6h3l5 4V5zM16 9a4 4 0 010 6"/></svg>
        </button>`
      : `<button class="metrics-btn mute-btn" data-key="${esc(muteKey)}" title="Mute / snooze this rule">
          <svg viewBox="0 0 24 24" class="ic"><path d="M11 5L6 9H3v6h3l5 4V5zM23 9l-6 6M17 9l6 6"/></svg>
        </button>`;
    el.innerHTML = `
      <div class="group-head">
        <svg viewBox="0 0 24 24" class="ic chev"><path d="M6 9l6 6 6-6"/></svg>
        ${resIcon(g.alerts[0])}
        <span class="gname" title="${esc(g.key)}">${esc(g.key)}</span>
        ${hdrGraph}${hdrTable}${hdrMute}
        ${g.muted ? '<span class="muted-badge">MUTED</span>' : ""}
        <span class="hchips">${groupSevChips(g.alerts)}</span>
        <span class="gtime" title="latest activity: ${fmtDate(latest)}">${rel(latest)}</span>
        <span class="gcount">${g.count}</span>
      </div>
      <div class="group-body">${g.alerts.map(rowHTML).join("")}</div>`;
    g.alerts.forEach((a) => { S.byId[a.id] = a; });
    el.querySelector(".group-head").onclick = () => {
      S.collapsed.has(g.key) ? S.collapsed.delete(g.key) : S.collapsed.add(g.key);
      el.classList.toggle("collapsed");
      updateCollapseBtn(); savePrefs();
    };
    bindRowActions(el);
    root.appendChild(el);
  }
  updateCollapseBtn();
  $("collapseBtn").style.display = "";
}
function renderFlat(alerts) {
  updateEmptyText();
  const root = $("groups");
  root.innerHTML = "";
  $("empty").hidden = alerts.length > 0;
  S.lastGroupKeys = [];
  $("collapseBtn").style.display = "none";   // nothing to collapse when flat
  if (!alerts.length) return;
  const el = document.createElement("div");
  el.className = "group flat";
  el.innerHTML = `<div class="group-body">${alerts.map(rowHTML).join("")}</div>`;
  alerts.forEach((a) => { S.byId[a.id] = a; });
  bindRowActions(el);
  root.appendChild(el);
}
function bindRowActions(el) {
  el.querySelectorAll(".ack").forEach((b) =>
    b.onclick = (e) => { e.stopPropagation(); ack(b.dataset.id); });
  el.querySelectorAll(".unack").forEach((b) =>
    b.onclick = (e) => { e.stopPropagation(); unack(b.dataset.id); });
  el.querySelectorAll(".alert").forEach((row) =>
    row.addEventListener("click", () => openDrawer(S.byId[row.dataset.id])));
  el.querySelectorAll(".metrics-btn:not(.logq-btn):not(.mute-btn):not(.unmute-btn)").forEach((b) =>
    b.onclick = (e) => { e.stopPropagation(); openMetrics(b.dataset.res, b.dataset.name); });
  el.querySelectorAll(".logq-btn").forEach((b) =>
    b.onclick = (e) => { e.stopPropagation(); openLogResults(b.dataset.rule, b.dataset.name); });
  el.querySelectorAll(".mute-btn").forEach((b) =>
    b.onclick = (e) => { e.stopPropagation(); openMuteMenu(b, b.dataset.key); });
  el.querySelectorAll(".unmute-btn").forEach((b) =>
    b.onclick = (e) => { e.stopPropagation(); unmuteRule(b.dataset.key); });
}
function isVM(a) {
  return (a.resource_type || "").includes("virtualmachines") && !!a.target_resource;
}
function isLogQuery(a) {
  return (a.rule || "").toLowerCase().includes("scheduledqueryrules");
}
function subLabel(id) {
  if (!id) return "";
  return S.subs[id] || (id.length > 8 ? id.slice(0, 8) + "…" : id);
}
// resource-type glyph shown before each alert name — quick visual triage
function resIcon(a) {
  const t = (a.resource_type || "").toLowerCase();
  const k = (a.alert_kind || "").toLowerCase();
  let p, cls;
  if (k.includes("service health") || k.includes("resource health")) {
    p = "M20.8 5.6a5.5 5.5 0 00-8.8 1.4A5.5 5.5 0 003.2 5.6C1 7.8 1 11.3 3.2 13.5L12 22l8.8-8.5c2.2-2.2 2.2-5.7 0-7.9z";
    cls = "ri-health";
  } else if (/virtualmachine|compute|scalesets/.test(t)) {
    p = "M3 4h18v11H3zM8 20h8M12 15v5"; cls = "ri-vm";
  } else if (/sql|database|cosmos|mysql|postgres|mariadb|\/servers/.test(t)) {
    p = "M4 6c0-1.7 3.6-3 8-3s8 1.3 8 3-3.6 3-8 3-8-1.3-8-3zM4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3";
    cls = "ri-db";
  } else if (/storage/.test(t)) {
    p = "M3 5h18v4H3zM3 9v10h18V9M7 13h5"; cls = "ri-store";
  } else if (/sites|serverfarm|web|frontdoor|cdn|apimanagement/.test(t)) {
    p = "M12 3a9 9 0 100 18 9 9 0 000-18zM3 12h18M12 3c2.6 2.7 2.6 15.3 0 18M12 3c-2.6 2.7-2.6 15.3 0 18";
    cls = "ri-web";
  } else if (/vault|keyvault/.test(t)) {
    p = "M7 11V7a5 5 0 0110 0v4M5 11h14v10H5z"; cls = "ri-key";
  } else if (/network|loadbalancer|publicip|virtualnetwork|dns|firewall|gateway/.test(t)) {
    p = "M12 2v6M12 22v-6M2 12h6M22 12h-6M12 8a4 4 0 100 8 4 4 0 000-8z"; cls = "ri-net";
  } else if (/kubernetes|managedcluster|container|registr/.test(t)) {
    p = "M12 2l9 5v10l-9 5-9-5V7z"; cls = "ri-k8s";
  } else {
    p = "M4 5h16v14H4zM4 10h16"; cls = "ri-generic";
  }
  return `<svg viewBox="0 0 24 24" class="ic ri ${cls}" aria-hidden="true"><path d="${p}"/></svg>`;
}
function rowHTML(a) {
  const s = sevIdx(a.sev_label);
  const firedMs = Date.parse(a.fired_at);
  const resolved = a.condition === "Resolved" && a.resolved_at;
  const stampMs = resolved ? a.resolved_at * 1000 : firedMs;
  const timeLabel = `${resolved ? "resolved " : ""}${rel(stampMs)}`;
  const dateLabel = fmtDate(stampMs);
  const portal = a.portal_url
    ? `<a class="portal" href="${esc(a.portal_url)}" target="_blank" rel="noopener"
        title="Open in Azure portal" onclick="event.stopPropagation()">
        <svg viewBox="0 0 24 24" class="ic"><path d="M14 3h7v7M21 3l-9 9M5 5h5v0M5 5v14h14v-5"/></svg>
      </a>` : "";
  // meta line: resource · resource group · subscription — only what exists
  const meta = [
    a.resource_name
      ? `<span class="m-res" title="Resource">${esc(a.resource_name)}</span>` : "",
    a.resource_group
      ? `<span title="Resource group">${esc(a.resource_group)}</span>` : "",
    a.subscription
      ? `<span title="Subscription ${esc(a.subscription)}">${esc(subLabel(a.subscription))}</span>` : "",
  ].filter(Boolean).join('<span class="m-dot">·</span>');
  const name = a.rule_name || a.rule || "(unnamed alert)";
  return `<div class="alert ${a.acked ? "acked" : ""} ${a.condition === "Resolved" ? "resolved" : ""}" data-id="${esc(a.id)}">
    <span class="chip s${s}">${a.sev_label}</span>
    ${resIcon(a)}
    <div class="ainfo">
      <div class="aline">
        ${a.alert_kind ? `<span class="akind">${esc(a.alert_kind)}</span>` : ""}
        <span class="aname" title="${esc(a.rule)}">${esc(name)}</span>
      </div>
      <div class="ameta">${meta}</div>
    </div>
    <span class="atime"><span class="arel">${timeLabel}</span><span class="adate">${dateLabel}</span></span>
    ${portal}
    ${a.acked
      ? `<button class="unack" data-id="${esc(a.id)}" title="Undismiss (restore)">
          <svg viewBox="0 0 24 24" class="ic"><path d="M3 12a9 9 0 109-9 9 9 0 00-7 3.3M3 4v4h4"/></svg>
        </button>`
      : `<button class="ack" data-id="${esc(a.id)}" title="Dismiss">
          <svg viewBox="0 0 24 24" class="ic"><path d="M5 12l5 5L20 7"/></svg>
        </button>`}</div>`;
}

// ── extensions (list + enable/disable toggles, in the settings menu) ───
function renderExtBadge() {
  $("gmExt").innerHTML = S.plugins.length
    ? S.plugins.map((p) =>
        `<div class="ext-row">
          <label class="ext-tog"><input type="checkbox" data-n="${esc(p.name)}" ${p.enabled ? "checked" : ""}><span></span></label>
          <div class="ext-meta"><span class="ext-name">${esc(p.name)}</span>` +
          (p.description ? `<span class="ext-desc">${esc(p.description)}</span>` : "") +
          `</div></div>`).join("")
    : `none registered — click <b>+ add</b> to add one`;
  $("gmExt").querySelectorAll("input[data-n]").forEach((cb) =>
    cb.onchange = () => togglePlugin(cb.dataset.n, cb.checked));
}
async function togglePlugin(name, enabled) {
  await fetch(`/api/plugins/toggle?name=${encodeURIComponent(name)}&enabled=${enabled}`,
    { method: "POST" });
  fetchPlugins();
}

// ── SSE live stream ───────────────────────────────────────────────────
function connect() {
  const es = new EventSource("/api/stream");
  es.addEventListener("open", () => setConn(true));
  es.addEventListener("error", () => setConn(false));
  es.addEventListener("summary", (e) => {
    renderSummary(JSON.parse(e.data)); fetchAlerts();
    if (!Object.keys(S.subs).length) fetchSubs();  // fills after first poll
  });
  es.addEventListener("new", (e) => onNew(JSON.parse(e.data)));
}
function setConn(up) {
  $("connDot").className = "dot " + (up ? "live" : "down");
  $("conn").textContent = up ? "live" : "reconnecting…";
}
function onNew(d) {
  fetchAlerts();
  if (d.worst <= 0) flashScreen();
  beep(d.worst);
  notify(d);
}

// ── notifications (in-page sound + optional browser notification) ─────
function ensureAudio() {
  if (!S.audio) S.audio = new (window.AudioContext || window.webkitAudioContext)();
  if (S.audio.state === "suspended") S.audio.resume();
  return S.audio;
}
function beep(worst) {
  if (!S.sound || S.quiet) return;         // quiet hours: no in-page sound
  const ctx = ensureAudio(), t = ctx.currentTime;
  const freq = [880, 740, 620, 520, 440][worst] || 440;
  const o = ctx.createOscillator(), g = ctx.createGain();
  o.type = "sine"; o.frequency.value = freq;
  g.gain.setValueAtTime(0, t);
  g.gain.linearRampToValueAtTime(0.18, t + 0.02);
  g.gain.exponentialRampToValueAtTime(0.001, t + 0.5);
  o.connect(g).connect(ctx.destination); o.start(t); o.stop(t + 0.5);
  if (worst <= 1) setTimeout(() => beepOnce(freq + 120), 260); // double for severe
}
function beepOnce(freq) {
  const ctx = ensureAudio(), t = ctx.currentTime;
  const o = ctx.createOscillator(), g = ctx.createGain();
  o.frequency.value = freq; g.gain.setValueAtTime(0.14, t);
  g.gain.exponentialRampToValueAtTime(0.001, t + 0.4);
  o.connect(g).connect(ctx.destination); o.start(t); o.stop(t + 0.4);
}
function notify(d) {
  // Browser notifications need a secure context (https or localhost).
  // Over plain http on the LAN they're blocked — sound + flash cover that.
  if (!("Notification" in window) || !window.isSecureContext) return;
  if (Notification.permission !== "granted") return;
  const a = d.items[0];
  new Notification(`${a.sev_label} · ${a.target_resource_name}`,
    { body: `${a.rule}${d.count > 1 ? `  (+${d.count - 1} more)` : ""}`,
      tag: "azmon" });
}
function flashScreen() {
  const f = $("flash"); f.classList.remove("go");
  void f.offsetWidth; f.classList.add("go");
}

// ── interactions ──────────────────────────────────────────────────────
$("search").addEventListener("input", (e) => {
  S.query = e.target.value; clearTimeout(window._dq);
  window._dq = setTimeout(fetchAlerts, 200);
});
$("showAcked").addEventListener("change", (e) => {
  S.showAcked = e.target.checked; savePrefs(); fetchAlerts();
});
$("sortSel").addEventListener("change", (e) => { S.sort = e.target.value; savePrefs(); fetchAlerts(); });
$("groupSel").addEventListener("change", (e) => { S.groupBy = e.target.value; savePrefs(); fetchAlerts(); });
$("rangeSel").addEventListener("change", (e) => {
  S.withinHours = parseFloat(e.target.value) || 0; savePrefs(); fetchAlerts();
});
$("unackAll").addEventListener("click", unackAll);
$("hintUndismiss").addEventListener("click", unackAll);

// ── mute / snooze ─────────────────────────────────────────────────────
let _muteKey = "";
function openMuteMenu(btn, key) {
  _muteKey = key;
  const m = $("muteMenu"), r = btn.getBoundingClientRect();
  m.style.top = (r.bottom + 6) + "px";
  m.style.left = Math.max(8, r.right - 160) + "px";
  m.hidden = false;
}
async function muteRule(key, hours) {
  await fetch(`/api/mute?key=${encodeURIComponent(key)}&hours=${hours}`, { method: "POST" });
  $("muteMenu").hidden = true; fetchAlerts();
}
async function unmuteRule(key) {
  await fetch("/api/unmute?key=" + encodeURIComponent(key), { method: "POST" });
  fetchAlerts();
}
$("muteMenu").addEventListener("click", (e) => e.stopPropagation());
$("muteMenu").querySelectorAll("button").forEach((b) =>
  b.addEventListener("click", () => muteRule(_muteKey, b.dataset.h)));
document.addEventListener("click", () => { $("muteMenu").hidden = true; });

// ── alert detail drawer ───────────────────────────────────────────────
function drawRow(label, value, mono) {
  if (!value && value !== 0) return "";
  return `<div class="dr-row"><span class="dr-k">${esc(label)}</span>` +
    `<span class="dr-v${mono ? " mono" : ""}">${esc(String(value))}</span></div>`;
}
function openDrawer(a) {
  if (!a) return;
  maybeCaution(a.subscription);
  const s = sevIdx(a.sev_label);
  $("dChip").className = "chip s" + s;
  $("dChip").textContent = a.sev_label;
  $("dName").textContent = a.rule_name || a.rule || "(unnamed alert)";
  const fired = Date.parse(a.fired_at);
  const links = [];
  if (a.portal_url)
    links.push(`<a class="dr-link" href="${esc(a.portal_url)}" target="_blank" rel="noopener">Open resource in Azure portal ↗</a>`);
  if (isVM(a))
    links.push(`<button class="dr-link" data-act="metrics">View resource metrics</button>`);
  if (isLogQuery(a))
    links.push(`<button class="dr-link" data-act="logq">Run query results</button>`);
  $("dBody").innerHTML =
    (a.muted ? `<div class="dr-muted">This rule is muted.</div>` : "") +
    (a.description ? `<p class="dr-desc">${esc(a.description)}</p>` : "") +
    drawRow("Kind", a.alert_kind) +
    drawRow("Condition", a.condition + (a.state ? ` · ${a.state}` : "")) +
    drawRow("Resource", a.resource_name || a.target_resource_name, true) +
    drawRow("Resource group", a.resource_group, true) +
    drawRow("Subscription", subLabel(a.subscription)) +
    drawRow("Signal", a.signal_type) +
    drawRow("Fired", fired ? fmtDate(fired) + `  (${rel(fired)})` : "") +
    (a.resolved_at ? drawRow("Resolved", fmtDate(a.resolved_at * 1000) + `  (${rel(a.resolved_at * 1000)})`) : "") +
    drawRow("Rule", a.rule, true) +
    (links.length ? `<div class="dr-links">${links.join("")}</div>` : "");
  // wire the in-drawer action buttons
  $("dBody").querySelectorAll("[data-act]").forEach((b) =>
    b.onclick = () => {
      closeDrawer();
      if (b.dataset.act === "metrics") openMetrics(a.target_resource, a.resource_name || a.target_resource_name);
      else openLogResults(a.rule, a.rule_name);
    });
  $("drawer").hidden = false; $("drawerBack").hidden = false;
}
function closeDrawer() { $("drawer").hidden = true; $("drawerBack").hidden = true; }
$("dClose").addEventListener("click", closeDrawer);
$("drawerBack").addEventListener("click", closeDrawer);

// ── settings (gear) menu ──────────────────────────────────────────────
async function refreshGmStatus() {
  try {
    const s = await (await fetch("/api/status")).json();
    const d = s.db || {};
    $("gmStatus").innerHTML =
      `<b>${d.total || 0}</b> alerts stored · ${d.open || 0} open · ${d.closed || 0} closed` +
      `<br>${s.subscriptions || 0} subs · retain ${s.retention_hours}h · poll ${s.poll_interval_seconds}s`;
  } catch { $("gmStatus").textContent = "status unavailable"; }
}
function toggleGear(show) {
  const m = $("gearMenu");
  const open = show ?? m.hidden;
  m.hidden = !open;
  $("gearBtn").setAttribute("aria-expanded", String(open));
  if (open) refreshGmStatus();
}
$("gearBtn").addEventListener("click", (e) => { e.stopPropagation(); toggleGear(); });
$("gearMenu").addEventListener("click", (e) => e.stopPropagation());
document.addEventListener("click", () => toggleGear(false));

$("miPoll").addEventListener("click", async () => {
  $("miPoll").querySelector("span").textContent = "Polling…";
  await fetch("/api/poll_now", { method: "POST" });
  $("miPoll").querySelector("span").textContent = "Poll now";
  fetchAlerts(); toggleGear(false);
});
// ── config editor (view/edit config.yaml, hot-applied) ────────────────
$("miConfig").addEventListener("click", async () => {
  toggleGear(false); $("cfgMsg").textContent = ""; $("configModal").hidden = false;
  try {
    const c = await (await fetch("/api/config")).json();
    $("cfgText").value = c.text || ""; $("cfgSub").textContent = c.path || "config.yaml";
  } catch { $("cfgText").value = "# couldn't load config"; }
});
$("cfgClose").addEventListener("click", () => { $("configModal").hidden = true; });
$("cfgSave").addEventListener("click", async () => {
  $("cfgMsg").textContent = "saving…";
  const r = await fetch("/api/config", { method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text: $("cfgText").value }) });
  const d = await r.json();
  if (r.ok) { $("cfgMsg").textContent = `saved & applied (${d.senders} notify channels)`; fetchAlerts(); }
  else $("cfgMsg").textContent = "✗ " + (d.error || "invalid config — not saved");
});

// ── backend log viewer ────────────────────────────────────────────────
let _logTimer = null, _logLines = [];
async function loadLogs() {
  try {
    _logLines = (await (await fetch("/api/logs?n=400")).json()).lines || [];
    renderLogs();
  } catch { $("logsPre").textContent = "logs unavailable"; }
}
function renderLogs() {
  const q = $("logsSearch").value.toLowerCase();
  const pre = $("logsPre"), atBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
  pre.innerHTML = _logLines
    .filter((l) => !q || l.toLowerCase().includes(q))
    .map((l) => {
      let c = "lg-info";
      if (/ ERROR /.test(l)) c = "lg-err";
      else if (/ WARNING /.test(l)) c = "lg-warn";
      else if (/ DEBUG /.test(l)) c = "lg-dbg";
      return `<div class="lg ${c}">${esc(l)}</div>`;
    }).join("");
  if (atBottom && !q) pre.scrollTop = pre.scrollHeight;
}
document.addEventListener("input", (e) => { if (e.target.id === "logsSearch") renderLogs(); });
$("miLogs").addEventListener("click", () => {
  toggleGear(false); $("logsModal").hidden = false; loadLogs();
  if ($("logsAuto").checked) _logTimer = setInterval(loadLogs, 3000);
});
function closeLogs() { $("logsModal").hidden = true; clearInterval(_logTimer); _logTimer = null; }
$("logsClose").addEventListener("click", closeLogs);
$("logsAuto").addEventListener("change", (e) => {
  clearInterval(_logTimer); _logTimer = null;
  if (e.target.checked) _logTimer = setInterval(loadLogs, 3000);
});

// ── re-authenticate (az login) ────────────────────────────────────────
$("miReauth").addEventListener("click", async () => {
  toggleGear(false);
  $("acctName").textContent = "signing in… (check the az login window)";
  try { await fetch("/api/reauth", { method: "POST" }); } catch { /* */ }
  fetchAccount(); fetchHealth(); fetchPim();
});

// ── plugin add (file picker → upload) ─────────────────────────────────
$("extAdd").addEventListener("click", (e) => { e.stopPropagation(); $("extFile").click(); });
$("extFile").addEventListener("change", async (e) => {
  const f = e.target.files[0]; if (!f) return;
  const text = await f.text();
  const r = await fetch("/api/plugins/upload", { method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ filename: f.name, text }) });
  const d = await r.json();
  if (r.ok) fetchPlugins(); else alert("Upload failed: " + (d.error || "?"));
  e.target.value = "";
});

// ── light / dark theme ────────────────────────────────────────────────
function applyTheme() { document.body.dataset.theme = S.theme; }
$("miTheme").addEventListener("click", () => {
  S.theme = S.theme === "light" ? "dark" : "light";
  applyTheme(); savePrefs(); toggleGear(false);
});

$("miTest").addEventListener("click", async () => {
  await fetch("/api/test_notify", { method: "POST" }); toggleGear(false);
});
function expHref() {
  return `/api/export?section=${S.section}&fmt=`;
}
$("miExpJson").addEventListener("click", (e) => { e.target.href = expHref() + "json"; });
$("miExpCsv").addEventListener("click", (e) => { e.target.href = expHref() + "csv"; });

// clear-old-alerts modal
$("miClear").addEventListener("click", () => { toggleGear(false); $("clearModal").hidden = false; });
$("clearCancel").addEventListener("click", () => { $("clearModal").hidden = true; });
$("clearModal").addEventListener("click", (e) => { if (e.target.id === "clearModal") $("clearModal").hidden = true; });
document.querySelectorAll("#clearModal [data-h]").forEach((b) =>
  b.addEventListener("click", async () => {
    const h = b.dataset.h;
    if (h === "0" && !confirm("Wipe the ENTIRE local alert DB? Azure is untouched; alerts re-populate on the next poll.")) return;
    await fetch("/api/purge?older_than_hours=" + h, { method: "POST" });
    $("clearModal").hidden = true; fetchAlerts();
  }));
$("hintShow").addEventListener("click", () => {
  S.showAcked = true; $("showAcked").checked = true;
  $("dismissHint").hidden = true; fetchAlerts();
});

// ── resource metrics (uPlot charts) ───────────────────────────────────
const MET = { res: "", name: "", hours: 6, defs: [], sel: new Set(),
              charts: [], search: "" };
const CHART_COLORS = ["#2DD4BF", "#42A5F5", "#FF7043", "#FFB300", "#B48EF0", "#56C596"];
// default view: CPU + RAM only (matches how the portal opens), if present
const PREFERRED = ["Percentage CPU", "Available Memory Bytes"];

// value/axis formatting per Azure metric unit, so each chart reads correctly
function humanBytes(v) {
  const u = ["B", "KB", "MB", "GB", "TB", "PB"]; let i = 0; v = +v;
  const neg = v < 0; v = Math.abs(v);
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return (neg ? "-" : "") + (v < 10 && i > 0 ? v.toFixed(1) : Math.round(v)) + " " + u[i];
}
function fmtNum(v) {
  v = +v;
  if (Math.abs(v) >= 1e6) return (v / 1e6).toFixed(1) + "M";
  if (Math.abs(v) >= 1e3) return (v / 1e3).toFixed(1) + "k";
  return Math.abs(v) < 10 ? v.toFixed(2).replace(/\.?0+$/, "") : Math.round(v).toString();
}
function fmtUnit(unit, v) {
  if (v == null || Number.isNaN(v)) return "–";
  const u = (unit || "").toLowerCase();
  if (u === "percent") return fmtNum(v) + "%";
  if (u === "bytespersecond") return humanBytes(v) + "/s";
  if (u === "bytes") return humanBytes(v);
  if (u === "countpersecond") return fmtNum(v) + "/s";
  if (u === "milliseconds") return Math.round(v) + " ms";
  if (u === "seconds") return fmtNum(v) + " s";
  if (u === "bitspersecond") return fmtNum(v) + " bps";
  return fmtNum(v);
}

async function openMetrics(res, name) {
  maybeCaution(subFromArm(res));
  MET.res = res; MET.name = name; MET.hours = 6; MET.sel = new Set(); MET.search = "";
  $("mmTitle").textContent = name || "Metrics";
  $("mmSub").textContent = "loading available metrics…";
  $("mmSearch").value = ""; $("mmMetrics").innerHTML = ""; $("mmMsg").textContent = "";
  document.querySelectorAll("#mmRange button").forEach((b) =>
    b.classList.toggle("on", b.dataset.h === "6"));
  destroyCharts();
  $("metricsModal").hidden = false;
  try {
    const r = await fetch("/api/metrics/definitions?resource=" + encodeURIComponent(res));
    const defs = await r.json();
    if (!Array.isArray(defs))
      throw new Error(defs.error || defs.detail || ("HTTP " + r.status));
    if (!defs.length) throw new Error("this resource exposes no metrics");
    MET.defs = defs;
    $("mmSub").textContent = name;
    const names = defs.map((d) => d.name);
    PREFERRED.filter((p) => names.includes(p)).forEach((p) => MET.sel.add(p));
    if (!MET.sel.size && names.length) MET.sel.add(names[0]);
    renderMetricChips();
    loadMetrics();
  } catch (e) {
    $("mmSub").textContent = "";
    const rbac = /403|forbidden|authoriz/i.test(e.message)
      ? " — you may need the Monitoring Reader role on this resource." : "";
    $("mmMsg").textContent = "Couldn't load metrics: " + e.message + rbac;
  }
}
function renderMetricChips() {
  const q = MET.search.toLowerCase();
  // selected always shown; others filtered by the search box
  const list = MET.defs.filter((d) =>
    MET.sel.has(d.name) || !q || d.label.toLowerCase().includes(q));
  $("mmMetrics").innerHTML = list.map((d) =>
    `<button class="mm-chip ${MET.sel.has(d.name) ? "on" : ""}" data-m="${esc(d.name)}"
       title="unit: ${esc(d.unit)}">${esc(d.label)}</button>`).join("")
    || `<span class="mm-none">no metric matches “${esc(MET.search)}”</span>`;
  $("mmMetrics").querySelectorAll(".mm-chip").forEach((b) =>
    b.onclick = () => {
      const m = b.dataset.m;
      MET.sel.has(m) ? MET.sel.delete(m) : MET.sel.add(m);
      renderMetricChips();
      loadMetrics();
    });
}
async function loadMetrics() {
  destroyCharts();
  const names = [...MET.sel];
  if (!names.length) { $("mmMsg").textContent = "Pick a metric above."; return; }
  $("mmMsg").textContent = "loading…";
  try {
    const r = await fetch(`/api/metrics?resource=${encodeURIComponent(MET.res)}`
      + `&names=${encodeURIComponent(names.join(","))}&hours=${MET.hours}`);
    const series = await r.json();
    if (!Array.isArray(series))
      throw new Error(series.error || series.detail || ("HTTP " + r.status));
    drawCharts(series);
    $("mmMsg").textContent = series.length ? "" : "No data for the selected metrics.";
  } catch (e) { $("mmMsg").textContent = "Couldn't load data: " + e.message; }
}
function labelFor(name) {
  const d = MET.defs.find((x) => x.name === name);
  return d ? d.label : name;
}
// one adaptive chart PER metric — its own unit-scaled Y-axis (no mixed scales)
function drawCharts(series) {
  const host = $("mmCharts");
  host.innerHTML = "";
  series.forEach((s, i) => {
    const xs = s.points.map((p) => Date.parse(p[0]) / 1000);
    const ys = s.points.map((p) => p[1]);
    const card = document.createElement("div");
    card.className = "mm-card";
    card.innerHTML = `<div class="mm-card-h">${esc(labelFor(s.name))}`
      + `<span class="mm-card-u">${esc(s.unit || "")}</span></div>`
      + `<div class="mm-plot"></div>`;
    host.appendChild(card);
    const plot = card.querySelector(".mm-plot");
    const color = CHART_COLORS[i % CHART_COLORS.length];
    const opts = {
      width: plot.clientWidth || 720, height: 190,
      scales: { x: { time: true } },
      axes: [
        { stroke: "#8B949E", grid: { stroke: "rgba(255,255,255,.06)" }, ticks: { stroke: "rgba(255,255,255,.1)" } },
        { stroke: "#8B949E", size: 62, grid: { stroke: "rgba(255,255,255,.06)" },
          ticks: { stroke: "rgba(255,255,255,.1)" },
          values: (u, splits) => splits.map((v) => fmtUnit(s.unit, v)) },
      ],
      legend: { show: true },
      series: [
        { value: (u, v) => (v == null ? "–" : new Date(v * 1000).toLocaleString()) },
        { label: labelFor(s.name), stroke: color, width: 2, fill: color + "18",
          points: { show: false }, value: (u, v) => fmtUnit(s.unit, v) },
      ],
    };
    MET.charts.push(new uPlot(opts, [xs, ys], plot));
  });
}
function destroyCharts() {
  MET.charts.forEach((c) => c.destroy());
  MET.charts = [];
  const h = $("mmCharts"); if (h) h.innerHTML = "";
}
$("mmSearch").addEventListener("input", (e) => { MET.search = e.target.value; renderMetricChips(); });
$("mmClose").addEventListener("click", () => { $("metricsModal").hidden = true; destroyCharts(); });
$("metricsModal").addEventListener("click", (e) => {
  if (e.target.id === "metricsModal") { $("metricsModal").hidden = true; destroyCharts(); }
});
$("mmRange").querySelectorAll("button").forEach((b) =>
  b.addEventListener("click", () => {
    MET.hours = parseFloat(b.dataset.h);
    $("mmRange").querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
    loadMetrics();
  }));

// ── log-query (KQL) alert results ─────────────────────────────────────
function fmtCell(v) {
  if (v == null) return "–";
  if (typeof v === "number") return fmtNum(v);
  if (typeof v === "string" && /^\d{4}-\d\d-\d\dT\d\d:\d\d/.test(v)) {
    const d = new Date(v); if (!isNaN(d)) return fmtDate(d.getTime());
  }
  return String(v);
}
async function openLogResults(rule, name) {
  maybeCaution(subFromArm(rule));
  $("lqTitle").textContent = name || "Query results";
  $("lqSub").textContent = "running the alert's query…";
  $("lqTable").innerHTML = ""; $("lqQuery").textContent = ""; $("lqMsg").textContent = "";
  $("logModal").hidden = false;
  try {
    const r = await fetch("/api/logquery?rule=" + encodeURIComponent(rule));
    const res = await r.json();
    if (res.error || !Array.isArray(res.columns))
      throw new Error(res.error || res.detail || ("HTTP " + r.status));
    $("lqQuery").textContent = res.query || "";
    $("lqSub").textContent = `${res.rows.length} row${res.rows.length === 1 ? "" : "s"}`
      + (res.window ? ` · window ${res.window}` : "");
    renderLqTable(res.columns, res.rows);
    if (!res.rows.length) $("lqMsg").textContent = "Query returned no rows for its window.";
  } catch (e) {
    $("lqSub").textContent = "";
    const rbac = /403|forbidden|authoriz/i.test(e.message)
      ? " — you may need the Log Analytics Reader role on the workspace." : "";
    $("lqMsg").textContent = "Couldn't run query: " + e.message + rbac;
  }
}
function renderLqTable(cols, rows) {
  const head = "<tr>" + cols.map((c) => `<th>${esc(c)}</th>`).join("") + "</tr>";
  const body = rows.slice(0, 500).map((row) =>
    "<tr>" + row.map((v) => `<td>${esc(fmtCell(v))}</td>`).join("") + "</tr>").join("");
  const more = rows.length > 500 ? `<div class="lq-more">showing first 500 of ${rows.length} rows</div>` : "";
  $("lqTable").innerHTML = `<table class="lq-table"><thead>${head}</thead><tbody>${body}</tbody></table>` + more;
}
$("lqClose").addEventListener("click", () => { $("logModal").hidden = true; });
$("logModal").addEventListener("click", (e) => {
  if (e.target.id === "logModal") $("logModal").hidden = true;
});
function allCollapsed() {
  return S.lastGroupKeys.length > 0 && S.lastGroupKeys.every((k) => S.collapsed.has(k));
}
function updateCollapseBtn() {
  const expanded = !allCollapsed();
  $("collapseBtn").title = expanded ? "Collapse all groups" : "Expand all groups";
  $("collapseBtn").classList.toggle("is-collapsed", !expanded);
}
$("collapseBtn").addEventListener("click", () => {
  if (allCollapsed()) S.collapsed.clear();
  else S.lastGroupKeys.forEach((k) => S.collapsed.add(k));
  savePrefs(); fetchAlerts();
});
$("ackAll").addEventListener("click", ackAll);
$("soundBtn").addEventListener("click", () => {
  S.sound = !S.sound;
  if (S.sound) ensureAudio();
  $("soundBtn").setAttribute("aria-pressed", S.sound);
  $("soundLabel").textContent = S.sound ? "Sound on" : "Sound off";
  savePrefs();
  if (S.sound && window.isSecureContext && "Notification" in window
      && Notification.permission === "default") Notification.requestPermission();
});
document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => {
  S.section = t.dataset.section;
  document.querySelectorAll(".tab").forEach((x) => x.classList.remove("sel"));
  t.classList.add("sel");
  savePrefs(); fetchAlerts();
}));
document.querySelectorAll(".tile").forEach((t) => t.addEventListener("click", () => {
  const v = t.dataset.sev;
  if (v === "all") S.severities.clear();
  else {
    const label = SEV[parseInt(v, 10)];
    S.severities.has(label) ? S.severities.delete(label) : S.severities.add(label);
  }
  updateTileSelection();
  fetchAlerts();
}));
function updateTileSelection() {
  document.querySelectorAll(".tile").forEach((t) => {
    const v = t.dataset.sev;
    const sel = v === "all" ? S.severities.size === 0 : S.severities.has(SEV[parseInt(v, 10)]);
    t.classList.toggle("sel", sel);
  });
}

// ── helpers ───────────────────────────────────────────────────────────
function sevIdx(label) { const i = SEV.indexOf(label); return i < 0 ? 4 : i; }
function esc(s) { return (s || "").replace(/[&<>"]/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }
function rel(ms) {
  if (!ms) return "—";
  const d = (Date.now() - ms) / 1000;
  if (d < 60) return "just now";
  if (d < 3600) return Math.floor(d / 60) + "m ago";
  if (d < 86400) return Math.floor(d / 3600) + "h ago";
  return Math.floor(d / 86400) + "d ago";
}
const _MON = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
function fmtDate(ms) {           // compact local date+time, e.g. "2 Jul, 14:30"
  if (!ms) return "";
  const d = new Date(ms);
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  return `${d.getDate()} ${_MON[d.getMonth()]}, ${hh}:${mm}`;
}

// ── boot ──────────────────────────────────────────────────────────────
loadPrefs();                              // restore last session's choices
applyTheme();
$("groupSel").value = S.groupBy;
$("sortSel").value = S.sort;
$("rangeSel").value = String(S.withinHours);
$("showAcked").checked = S.showAcked;
$("soundBtn").setAttribute("aria-pressed", S.sound);
$("soundLabel").textContent = S.sound ? "Sound on" : "Sound off";
document.querySelectorAll(".tab").forEach((t) =>
  t.classList.toggle("sel", t.dataset.section === S.section));
updateTileSelection();
if (S.sound) ensureAudio();               // may stay suspended until a user gesture
document.addEventListener("click", () => { if (S.sound) ensureAudio(); }, { once: true });
document.addEventListener("keydown", () => { if (S.sound) ensureAudio(); }, { once: true });
// keyboard: Esc closes overlays, "/" focuses the filter box
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    ["configModal", "logsModal", "metricsModal", "logModal", "clearModal", "drawer", "drawerBack"]
      .forEach((id) => { const el = $(id); if (el) el.hidden = true; });
    closeLogs(); destroyCharts();
  } else if (e.key === "/" && document.activeElement.tagName !== "INPUT"
             && document.activeElement.tagName !== "TEXTAREA") {
    e.preventDefault(); $("search").focus();
  }
});
connect();
fetchAccount();
fetchSubs();
fetchPlugins();
fetchHealth();
fetchPim();
setInterval(fetchAccount, 5 * 60 * 1000);
setInterval(fetchPlugins, 5 * 60 * 1000);
setInterval(fetchHealth, 30 * 1000);
setInterval(fetchPim, 3 * 60 * 1000);
setInterval(() => {  // refresh relative times
  const s = $("updated"); if (s && s._ts) s.textContent = "updated " + rel(s._ts);
}, 30000);
