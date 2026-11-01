import { get, post } from "../api.js";
import {
  avatar,
  confirmDialog,
  esc,
  fmtDateTime,
  icon,
  timeAgo,
  toast,
} from "../ui.js";

let activeTab = "cases";

export async function mount(container, ctx) {
  const guildId = ctx.store.guildId;
  const guild = ctx.store.guild || (await ctx.loadGuild(guildId));

  const topbar = ctx.setTopbar({
    title: "Moderation",
    sub: `${guild.name} · recent actions and bans`,
    actions: `<button class="btn small" id="mod-refresh">${icon("refresh", 15)} Refresh</button>`,
  });

  container.innerHTML = `
    <div class="tabs">
      <button class="tab" data-tab="cases" type="button">Cases</button>
      <button class="tab" data-tab="bans" type="button">Bans</button>
    </div>
    <div class="searchbar" style="max-width:380px;margin-bottom:16px">
      ${icon("search")}
      <input id="mod-search" placeholder="Filter by member, moderator or reason…" autocomplete="off">
    </div>
    <div id="mod-body"></div>`;

  const body = container.querySelector("#mod-body");
  let cases = [];
  let bans = [];
  let filter = "";

  const drawCases = () => {
    const query = filter.toLowerCase();
    const rows = cases.filter((entry) =>
      !query ||
      [entry.type, entry.reason, entry.moderator?.display, entry.target?.display, String(entry.case_id)]
        .filter(Boolean)
        .some((part) => String(part).toLowerCase().includes(query))
    );
    body.innerHTML = rows.length
      ? `<div class="card" style="padding:6px 10px"><table class="table">
          <thead><tr><th>Case</th><th>Type</th><th>Member</th><th>Reason</th><th>Moderator</th><th>When</th></tr></thead>
          <tbody>${rows
            .map(
              (entry) => `<tr>
                <td class="muted">#${entry.case_id}</td>
                <td><span class="chip">${esc(entry.type)}</span></td>
                <td><div class="identity">${avatar(entry.target, 26)}<div class="who"><div class="n">${esc(
                entry.target?.display || "unknown"
              )}</div></div></div></td>
                <td>${esc(entry.reason || "—")}</td>
                <td class="muted">${esc(entry.moderator?.display || "unknown")}</td>
                <td class="muted tiny" title="${esc(entry.created_on || "")}">${timeAgo(entry.created_on)}</td>
              </tr>`
            )
            .join("")}</tbody></table></div>`
      : `<div class="empty">No moderation cases recorded for this server.</div>`;
  };

  const drawBans = () => {
    const query = filter.toLowerCase();
    const rows = bans.filter(
      (entry) =>
        !query ||
        [entry.user?.display, entry.user?.username, entry.reason]
          .filter(Boolean)
          .some((part) => String(part).toLowerCase().includes(query))
    );
    body.innerHTML = rows.length
      ? `<div class="card" style="padding:6px 10px"><table class="table">
          <thead><tr><th>Member</th><th>Reason</th><th></th></tr></thead>
          <tbody>${rows
            .map(
              (entry) => `<tr>
                <td><div class="identity">${avatar(entry.user, 30)}<div class="who">
                  <div class="n">${esc(entry.user?.display || "unknown")}</div>
                  <div class="h">@${esc(entry.user?.username || "")}</div>
                </div></div></td>
                <td>${esc(entry.reason || "—")}</td>
                <td style="text-align:right"><button class="btn small" data-unban="${esc(
                  entry.user?.id
                )}">Unban</button></td>
              </tr>`
            )
            .join("")}</tbody></table></div>`
      : `<div class="empty">No bans on this server.</div>`;

    body.querySelectorAll("[data-unban]").forEach((button) => {
      button.addEventListener("click", async () => {
        const entry = bans.find((item) => item.user?.id === button.dataset.unban);
        if (!(await confirmDialog("Lift the ban?", `Unban ${entry?.user?.display || "this member"}?`, { confirmLabel: "Unban" }))) {
          return;
        }
        button.disabled = true;
        try {
          await post(`/api/guilds/${guildId}/members/${button.dataset.unban}/unban`, {});
          bans = bans.filter((item) => item.user?.id !== button.dataset.unban);
          drawBans();
          toast("Ban lifted");
        } catch (error) {
          toast(error.message, "error");
          button.disabled = false;
        }
      });
    });
  };

  const draw = () => {
    container.querySelectorAll(".tab").forEach((tab) => {
      tab.classList.toggle("active", tab.dataset.tab === activeTab);
    });
    if (activeTab === "cases") drawCases();
    else drawBans();
  };

  const load = async () => {
    body.innerHTML = `<div class="loading-row">Loading…</div>`;
    try {
      if (activeTab === "cases") {
        const data = await get(`/api/guilds/${guildId}/cases?limit=200`);
        cases = data.cases;
      } else {
        const data = await get(`/api/guilds/${guildId}/bans`);
        bans = data.bans;
      }
      draw();
    } catch (error) {
      body.innerHTML = `<div class="empty">${esc(error.message)}</div>`;
    }
  };

  container.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      activeTab = tab.dataset.tab;
      load();
    });
  });

  container.querySelector("#mod-search").addEventListener("input", (event) => {
    filter = event.target.value.trim();
    draw();
  });

  topbar.querySelector("#mod-refresh").addEventListener("click", load);

  await load();
}
