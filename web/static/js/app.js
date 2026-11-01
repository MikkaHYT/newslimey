// Dashboard shell: authentication, guild rail, navigation, routing and the
// live websocket. Views export a `mount(container, ctx)` function.

import { ApiError, get, post, setUnauthorisedHandler } from "./api.js";
import { esc, icon, fmtNumber, toast } from "./ui.js";

import * as overviewView from "./views/overview.js";
import * as chatView from "./views/chat.js";
import * as membersView from "./views/members.js";
import * as moderationView from "./views/moderation.js";
import * as commandsView from "./views/commands.js";
import * as settingsView from "./views/settings.js";

const $ = (selector) => document.querySelector(selector);

const store = {
  session: null,
  overview: null,
  guilds: [],
  guild: null,
  guildId: null,
  guildCache: new Map(),
};

const liveListeners = new Set();
let socket = null;
let socketRetry = 0;
let currentCleanup = null;
let statusTimer = null;

// ------------------------------------------------------------------- live
export function onLive(handler) {
  liveListeners.add(handler);
  return () => liveListeners.delete(handler);
}

function dispatchLive(event) {
  if (event.type === "channels_changed") {
    store.guildCache.delete(String(event.guild_id));
  }
  for (const listener of [...liveListeners]) {
    try {
      listener(event);
    } catch (error) {
      console.error("live handler failed", error);
    }
  }
}

function setSocketState(online) {
  const dot = $("#ws-dot");
  const label = $("#ws-label");
  if (!dot || !label) return;
  dot.classList.toggle("on", online);
  dot.classList.toggle("off", !online);
  label.textContent = online ? "Live" : "Reconnecting…";
}

function connectLive() {
  if (!store.session) return;
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  socket = new WebSocket(`${scheme}://${location.host}/ws`);
  socket.addEventListener("open", () => {
    socketRetry = 0;
    setSocketState(true);
  });
  socket.addEventListener("message", (event) => {
    let data;
    try {
      data = JSON.parse(event.data);
    } catch {
      return;
    }
    if (data.type === "hello") return;
    dispatchLive(data);
  });
  socket.addEventListener("close", () => {
    setSocketState(false);
    if (!store.session) return;
    const wait = Math.min(20000, 800 * 2 ** socketRetry++);
    setTimeout(connectLive, wait);
  });
  socket.addEventListener("error", () => socket?.close());
}

// ------------------------------------------------------------------ shell
export async function loadGuild(guildId, { force = false } = {}) {
  const key = String(guildId);
  if (!force && store.guildCache.has(key)) {
    return store.guildCache.get(key);
  }
  const guild = await get(`/api/guilds/${key}`);
  store.guildCache.set(key, guild);
  return guild;
}

export function refreshShell() {
  return loadOverview();
}

async function loadOverview() {
  const data = await get("/api/overview");
  store.overview = data;
  store.guilds = data.guilds;
  store.session = data.bot;
  renderRail();
  renderBrand();
  return data;
}

function renderBrand() {
  const bot = store.overview?.bot;
  if (!bot) return;
  $("#bot-tag").textContent = `v${bot.version} · ${fmtNumber(bot.guilds)} servers`;
  $("#latency-pill").textContent = `${bot.latency_ms} ms`;
}

function renderRail() {
  const rail = $("#guild-rail");
  const active = store.guildId ? String(store.guildId) : null;
  const homeActive = !active && !location.hash.startsWith("#/settings");
  $("#rail-home")?.classList.toggle("active", homeActive);

  rail.innerHTML = store.guilds
    .map((guild) => {
      const inner = guild.icon
        ? `<img src="${esc(guild.icon)}" alt="" loading="lazy">`
        : `<span class="fallback">${esc(guild.name.slice(0, 2))}</span>`;
      return `<a class="rail-icon${active === guild.id ? " active" : ""}" href="#/g/${guild.id}/chat" title="${esc(
        guild.name
      )}" data-guild="${guild.id}">${inner}</a>`;
    })
    .join("");
}

function renderGuildHeader() {
  const header = $("#guild-header");
  if (!store.guildId) {
    header.innerHTML = `<div class="identity"><span class="avatar fallback" style="width:34px;height:34px">✦</span><div class="who"><div class="n">No server selected</div><div class="h">Pick one on the left</div></div></div>`;
    return;
  }
  const guild = store.guild;
  if (!guild) {
    header.innerHTML = `<span class="muted tiny">Loading server…</span>`;
    return;
  }
  header.innerHTML = `
    ${guild.icon ? `<img src="${esc(guild.icon)}" alt="">` : `<span class="fallback">${esc(guild.name.slice(0, 2))}</span>`}
    <div style="min-width:0">
      <div class="name">${esc(guild.name)}</div>
      <div class="tiny muted">${fmtNumber(guild.member_count)} members</div>
    </div>`;
}

function navLink(href, label, iconName, active) {
  return `<a class="nav-link${active ? " active" : ""}" href="${href}">${icon(iconName)}<span>${label}</span></a>`;
}

function renderNav(view) {
  const nav = $("#nav");
  const guildId = store.guildId;
  const parts = [];
  if (guildId) {
    parts.push(`<div class="nav-section">This server</div>`);
    parts.push(navLink(`#/g/${guildId}/chat`, "Chat", "chat", view === "chat"));
    parts.push(navLink(`#/g/${guildId}/members`, "Members", "members", view === "members"));
    parts.push(
      navLink(`#/g/${guildId}/moderation`, "Moderation", "shield", view === "moderation")
    );
    parts.push(navLink(`#/g/${guildId}/commands`, "Commands", "commands", view === "commands"));
    parts.push(navLink(`#/g/${guildId}/settings`, "Settings", "settings", view === "settings"));
  }
  parts.push(`<div class="nav-section">Bot</div>`);
  parts.push(navLink("#/overview", "Overview", "overview", view === "overview"));
  parts.push(navLink("#/settings", "Bot-wide settings", "globe", view === "global-settings"));
  nav.innerHTML = parts.join("");
}

export function setTopbar({ title, sub = "", actions = "" }) {
  const bar = $("#topbar");
  bar.innerHTML = `
    <button class="btn icon ghost mobile-only" id="nav-toggle" type="button" aria-label="Menu">${icon(
      "menu"
    )}</button>
    <div style="min-width:0">
      <h1>${esc(title)}</h1>
      ${sub ? `<div class="sub">${esc(sub)}</div>` : ""}
    </div>
    <div class="actions">${actions}</div>`;
  $("#nav-toggle")?.addEventListener("click", () => $("#app").classList.toggle("nav-open"));
  return bar.querySelector(".actions");
}

// ------------------------------------------------------------------ routes
function parseRoute() {
  const raw = location.hash.replace(/^#/, "") || "/overview";
  const parts = raw.split("/").filter(Boolean);
  if (parts[0] === "g" && parts[1]) {
    return { view: parts[2] || "chat", guildId: parts[1], channelId: parts[3] || null };
  }
  if (parts[0] === "settings") return { view: "global-settings" };
  return { view: parts[0] || "overview" };
}

const VIEW_MODULES = {
  overview: overviewView,
  chat: chatView,
  members: membersView,
  moderation: moderationView,
  commands: commandsView,
  settings: settingsView,
  "global-settings": settingsView,
};

async function renderRoute() {
  if (!store.session) return;
  const route = parseRoute();
  const view = $("#view");
  const app = $("#app");
  app.classList.remove("nav-open");

  if (currentCleanup) {
    try {
      currentCleanup();
    } catch (error) {
      console.error(error);
    }
    currentCleanup = null;
  }

  store.guildId = route.guildId ? String(route.guildId) : null;
  store.guild = null;

  if (store.guildId) {
    renderGuildHeader();
    try {
      store.guild = await loadGuild(store.guildId);
      renderGuildHeader();
    } catch (error) {
      toast(error.message, "error");
    }
  }

  renderRail();
  renderNav(route.view === "global-settings" ? "global-settings" : route.view);

  const module = VIEW_MODULES[route.view] || overviewView;
  view.className = `view${route.view === "chat" ? " flush" : ""}`;
  view.innerHTML = `<div class="loading-row">Loading…</div>`;

  const ctx = {
    store,
    route,
    loadGuild,
    refreshShell,
    setTopbar,
    onLive,
    navigate: (path) => {
      location.hash = path;
    },
  };

  try {
    currentCleanup = await module.mount(view, ctx);
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) return;
    console.error(error);
    view.innerHTML = `<div class="empty">${esc(error.message || "Could not load that view")}</div>`;
  }
}

function navigateAware() {
  if (!store.session) return;
  renderRoute();
}

// ------------------------------------------------------------------- boot
function showLogin(message = "") {
  store.session = null;
  if (statusTimer) {
    clearInterval(statusTimer);
    statusTimer = null;
  }
  $("#app").classList.add("hidden");
  $("#boot").classList.add("hidden");
  $("#login").classList.remove("hidden");
  $("#login-error").textContent = message;
  $("#login-password").focus();
}

function showApp() {
  $("#login").classList.add("hidden");
  $("#boot").classList.add("hidden");
  $("#app").classList.remove("hidden");
}

async function startDashboard() {
  try {
    await loadOverview();
    showApp();
    if (!location.hash) history.replaceState(null, "", "#/overview");
    await renderRoute();
    connectLive();
    if (statusTimer) clearInterval(statusTimer);
    statusTimer = setInterval(async () => {
      try {
        await loadOverview();
      } catch {
        /* the socket state already tells the user */
      }
    }, 45000);
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) return;
    showApp();
    setTopbar({ title: "Dashboard" });
    $("#view").innerHTML = `<div class="empty">${esc(
      error.message || "Could not reach the bot"
    )} — the bot may still be starting.</div>`;
  }
}

async function boot() {
  setUnauthorisedHandler(() => showLogin("Your session expired. Sign in again."));
  $("#login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = $("#login-submit");
    button.disabled = true;
    $("#login-error").textContent = "";
    try {
      await post("/api/login", { password: $("#login-password").value });
      $("#login-password").value = "";
      await startDashboard();
    } catch (error) {
      showLogin(error.message || "Could not sign in");
    } finally {
      button.disabled = false;
    }
  });

  $("#logout").addEventListener("click", async () => {
    try {
      await post("/api/logout");
    } catch {
      /* the cookie is going away either way */
    }
    store.session = null;
    socket?.close();
    if (statusTimer) clearInterval(statusTimer);
    showLogin("");
  });

  window.addEventListener("hashchange", navigateAware);

  try {
    await get("/api/session");
    await startDashboard();
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      showLogin("");
    } else {
      showLogin(error.message || "The dashboard is starting up");
    }
  }
}

boot();
