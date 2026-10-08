// The broadcast's overlay: the match's spectator view full-frame, and the widgets of the presentation's layout over it,
// drawn from the lobby (who plays, the teams), the match (status, clock, outcomes, scores) and the presentation (title,
// names, theme, banners). state.json (serve.py) has all of it; a page widget is sent it on every change.
"use strict";

const W = 1920, H = 1080, BANNER_MS = 30000, OVER = ["finished", "cancelled", "failed"];
const ANCHOR = {
  "top": "top:24px;left:50%;transform:translateX(-50%)",
  "top-left": "top:24px;left:24px",
  "top-right": "top:24px;right:24px",
  "bottom": "bottom:24px;left:50%;transform:translateX(-50%)",
  "bottom-left": "bottom:24px;left:24px",
  "bottom-right": "bottom:24px;right:24px",
  "left": "left:24px;top:50%;transform:translateY(-50%)",
  "right": "right:24px;top:50%;transform:translateY(-50%)",
  "center": "left:50%;top:50%;transform:translate(-50%,-50%)",
};
const AT = {score_bug: "top", banners: "bottom-right", title_card: "center", end_card: "center"};
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;"})[c]);

let S = null, viewSrc;
const widgets = [];

function fit() {
  $("#stage").style.transform = `scale(${Math.min(innerWidth / W, innerHeight / H)})`;
}

// ---- what the state says ----

const slots = () => S.lobby?.player_slots || [];
const state = (id) => S.match?.player_states?.[id] || {};
function nameOf(id) {
  const given = S.broadcast.names?.[id], slot = slots().find((s) => s.player_id === id);
  return given || slot?.game_settings?.label || slot?.player_name || (slot?.player_kind === "ai" ? "AI" : id);
}
function teams() {
  const t = S.lobby?.player_teams;
  return t?.length ? t : slots().map((s) => ({team_id: s.player_id, player_ids: [s.player_id]}));
}
function score(id) {
  const s = state(id).scores?.[0];
  return s ? s.value : null;
}
const number = (v) => Number.isInteger(v) ? v.toLocaleString("en-US") : v.toFixed(1);
function duration(s) {
  s = Math.max(0, Math.floor(s));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = String(s % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${x}` : `${m}:${x}`;
}
function clock(c) {
  if (!c) return "";
  const limit = c.limit ?? null;
  if (c.unit === "seconds" || c.unit === "wall_seconds") return duration(c.value) + (limit != null ? ` / ${duration(limit)}` : "");
  if (c.unit === "turns") return `Turn ${c.value}` + (limit != null ? ` of ${limit}` : "");
  return `${number(c.value)}${limit != null ? ` / ${number(limit)}` : ""} ${c.unit}`;
}
function result() {
  const players = Object.entries(S.match.player_states || {});
  const won = players.filter(([, s]) => s.status === "won").map(([id]) => nameOf(id));
  if (won.length) return `${won.join(" and ")} won`;
  if (players.some(([, s]) => s.status === "drawn")) return "A draw";
  return S.match.status_detail || S.match.status;
}
function status() {
  const m = S.match;
  if (!m) return "Waiting for the match";
  if (m.status === "not_started") return "Starting soon";
  if (m.status === "paused") return "Paused" + (m.status_detail ? `: ${m.status_detail}` : "");
  return OVER.includes(m.status) ? result() : clock(m.progress?.[0]);
}

// ---- the widgets ----

function scoreBug(el) {
  el.hidden = !S.lobby?.player_slots?.length;
  const side = (t) => `<div class="team">${t.player_ids.map((id) => {
    const s = score(id);
    return `<span class="player ${esc(state(id).status || "")}">${esc(nameOf(id))}${s != null ? `<b>${number(s)}</b>` : ""}</span>`;
  }).join("")}</div>`;
  const ts = teams(), mid = `<div class="mid">${esc(status())}</div>`;
  el.innerHTML = ts.length === 2 ? side(ts[0]) + mid + side(ts[1]) : mid + ts.map(side).join("");
}

function banners(el, w) {
  const all = S.broadcast.banners || [];
  el.hidden = !all.length;
  if (!all.length || (w.at && Date.now() - w.at < BANNER_MS && w.drawn === all.length)) return;
  w.index = w.at ? (w.index + 1) % all.length : 0;
  w.at = Date.now();
  w.drawn = all.length;
  const b = all[w.index], parts = b.text.split(/\{([a-z][a-z0-9_-]{0,19})\}/);
  el.classList.toggle("light", b.theme === "light");
  el.innerHTML = `<div class="ad">${parts.map((s, k) => k % 2 ? `<img alt="" src="${esc(b.logos[s])}">` : esc(s)).join("")}</div>`;
}

function titleCard(el) {
  el.hidden = !!S.match && S.match.status !== "not_started";
  if (el.hidden) return;
  const sides = teams().map((t) => esc(t.player_ids.map(nameOf).join(" & ")));
  el.innerHTML = (S.broadcast.title ? `<h1>${esc(S.broadcast.title)}</h1>` : "")
    + `<div class="sides">${sides.join('<span class="vs">vs</span>')}</div><p>${esc(status())}</p>`;
}

function endCard(el) {
  el.hidden = !S.match || !OVER.includes(S.match.status);
  if (el.hidden) return;
  const rows = teams().flatMap((t) => t.player_ids).map((id) => {
    const s = score(id);
    return `<tr class="${esc(state(id).status || "")}"><td>${esc(nameOf(id))}</td><td>${esc(state(id).status || "")}</td>`
      + `<td>${s != null ? number(s) : ""}</td></tr>`;
  });
  el.innerHTML = `<h1>${esc(result())}</h1><table>${rows.join("")}</table>`;
}

function page(el) {
  el.contentWindow?.postMessage({type: "agentenv-broadcast", lobby: S.lobby, match: S.match, broadcast: S.broadcast}, "*");
}

const DRAW = {score_bug: scoreBug, banners, title_card: titleCard, end_card: endCard, page};

function place(el, w) {
  if (w.box) {
    const [x, y, bw, bh] = w.box;
    el.style.cssText = `left:${x}px;top:${y}px;width:${bw}px;height:${bh}px`;
    return;
  }
  el.style.cssText = ANCHOR[w.at || AT[w.widget]];
  if (w.widget === "page") {
    const [pw, ph] = w.size || [480, 270];
    el.style.width = `${pw}px`;
    el.style.height = `${ph}px`;
  }
}

function build() {
  const theme = S.broadcast.theme || {};
  document.documentElement.style.setProperty("--accent", theme.accent || "#e8b04a");
  if (theme.font) document.documentElement.style.setProperty("--font", `"${theme.font}", sans-serif`);
  for (const w of S.broadcast.layout || [{widget: "score_bug"}]) {
    const el = document.createElement(w.widget === "page" ? "iframe" : "div");
    el.className = `widget ${w.widget}`;
    place(el, w);
    if (w.widget === "page") el.src = w.url.startsWith("env:") ? S.env + w.url.slice(4) : w.url;
    $("#widgets").append(el);
    widgets.push({w, el});
  }
}

function view() {
  if (S.view !== viewSrc) {
    viewSrc = S.view;
    $("#view").hidden = !viewSrc;
    if (viewSrc) $("#view").src = viewSrc;
  }
  $("#slate").hidden = !!viewSrc;
  $("#slate").innerHTML = (S.broadcast.title ? `<h1>${esc(S.broadcast.title)}</h1>` : "") + `<p>${esc(status())}</p>`;
}

async function poll() {
  try {
    S = await (await fetch("state.json", {cache: "no-store"})).json();
  } catch {
    return;
  }
  if (!widgets.length) build();
  view();
  for (const w of widgets) DRAW[w.w.widget](w.el, w);
}

addEventListener("resize", fit);
fit();
poll();
setInterval(poll, 1000);
