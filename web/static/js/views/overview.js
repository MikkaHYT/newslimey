import { get } from "../api.js";
import {
  avatar,
  esc,
  fmtNumber,
  fmtUptime,
  icon,
  timeAgo,
  toast,
} from "../ui.js";

let refreshTimer = null;

function statCard(label, value, foot, iconName) {
  return `<div class="card stat">
    <div style="display:flex;align-items:center;justify-content:space-between">
      <span class="label">${esc(label)}</span>
      <span class="muted">${icon(iconName, 17)}</span>
    </div>
    <span class="value">${value}</span>
    <span class="foot">${esc(foot)}</span>
  </div>`;
}

function serverRow(guild) {
  return `<tr class="clickable" data-guild="${guild.id}">
    <td>
      <div class="identity">
        ${
          guild.icon
            ? `<img class="avatar" src="${esc(guild.icon)}" width="32" height="32" alt="">`
            : `<span class="avatar fallback" style="width:32px;height:32px">${esc(
                guild.name.slice(0, 2)
              )}</span>`
        }
        <div class="who"><div class="n">${esc(guild.name)}</div>
          <div class="h">${guild.bot_is_admin ? "administrator" : "member"}</div></div>
      </div>
    </td>
    <td>${fmtNumber(guild.member_count)}</td>
    <td>${fmtNumber(guild.channel_count)}</td>
    <td>${fmtNumber(guild.role_count)}</td>
    <td class="muted tiny">${guild.created_at ? timeAgo(guild.created_at) : "—"}</td>
  </tr>`;
}

function render(container, data, ctx) {
  const bot = data.bot;
  container.innerHTML = `
    <div class="cards">
      ${statCard("Servers", fmtNumber(bot.guilds), `${fmtNumber(bot.channels)} channels`, "globe")}
      ${statCard("Members", fmtNumber(bot.users), "across every server", "members")}
      ${statCard("Gateway ping", `${bot.latency_ms} ms`, "websocket round trip", "sparkles")}
      ${statCard("Uptime", fmtUptime(bot.uptime), `since ${esc(bot.name)} started`, "star")}
      ${statCard("Commands", fmtNumber(bot.commands), `${fmtNumber(bot.slash_commands)} slash commands`, "commands")}
      ${statCard("Live viewers", fmtNumber(data.clients), "dashboards connected", "users")}
    </div>

    <div class="section-title">Servers</div>
    <div class="card" style="padding:6px 10px">
      <table class="table">
        <thead><tr><th>Server</th><th>Members</th><th>Channels</th><th>Roles</th><th>Created</th></tr></thead>
        <tbody>
          ${data.guilds.map(serverRow).join("") || `<tr><td colspan="5" class="muted">The bot is not in any servers yet.</td></tr>`}
        </tbody>
      </table>
    </div>

    <div class="grid-2" style="margin-top:18px">
      <div class="card">
        <div class="card-head"><h3>Runtime</h3>${icon("sliders", 18)}</div>
        <table class="table">
          <tbody>
            <tr><td class="muted">Bot</td><td>${esc(bot.name)} <span class="muted tiny">(${esc(bot.id)})</span></td></tr>
            <tr><td class="muted">Version</td><td>v${esc(bot.version)}</td></tr>
            <tr><td class="muted">discord.py</td><td>${esc(bot.discord)}</td></tr>
            <tr><td class="muted">Python</td><td>${esc(bot.python)}</td></tr>
            <tr><td class="muted">Memory</td><td>${bot.memory_mb != null ? `${bot.memory_mb} MB` : "—"}</td></tr>
            <tr><td class="muted">CPU</td><td>${bot.cpu != null ? `${bot.cpu}%` : "—"}</td></tr>
            <tr><td class="muted">Shards</td><td>${fmtNumber(bot.shards)}</td></tr>
          </tbody>
        </table>
      </div>

      <div class="card">
        <div class="card-head"><h3>Voice</h3>${icon("mic", 18)}</div>
        ${
          data.voice.length
            ? `<table class="table"><tbody>${data.voice
                .map(
                  (entry) => `<tr>
                    <td>${esc(entry.guild)}</td>
                    <td class="muted">#${esc(entry.channel)}</td>
                    <td><span class="chip">${entry.playing ? (entry.paused ? "paused" : "playing") : "idle"}</span></td>
                  </tr>`
                )
                .join("")}</tbody></table>`
            : `<p class="muted">Not connected to a voice channel right now.</p>`
        }
        <div class="section-title" style="margin-top:18px">Quick links</div>
        <div style="display:flex;gap:8px;flex-wrap:wrap">
          <a class="btn small" href="#/settings">Bot-wide settings</a>
          <a class="btn small" href="#/overview">Refresh</a>
        </div>
      </div>
    </div>`;

  container.querySelectorAll("[data-guild]").forEach((row) => {
    row.addEventListener("click", () => {
      location.hash = `#/g/${row.dataset.guild}/chat`;
    });
  });
}

export async function mount(container, ctx) {
  ctx.setTopbar({
    title: "Overview",
    sub: "A live look at everything the bot is doing",
    actions: `<button class="btn small" id="refresh-overview">Refresh</button>`,
  });

  const load = async () => {
    try {
      const data = await get("/api/overview");
      ctx.store.overview = data;
      render(container, data, ctx);
      container.querySelector("#refresh-overview")?.addEventListener("click", async (event) => {
        event.currentTarget.disabled = true;
        await load();
        event.currentTarget.disabled = false;
      });
    } catch (error) {
      toast(error.message, "error");
      container.innerHTML = `<div class="empty">${esc(error.message)}</div>`;
    }
  };

  await load();
  refreshTimer = setInterval(load, 30000);

  return () => {
    if (refreshTimer) clearInterval(refreshTimer);
    refreshTimer = null;
  };
}
