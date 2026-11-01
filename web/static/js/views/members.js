import { get } from "../api.js";
import { avatar, colorFor, debounce, esc, fmtNumber, icon, toast } from "../ui.js";
import { openMember } from "./memberModal.js";

const PAGE = 60;

export async function mount(container, ctx) {
  const guildId = ctx.store.guildId;
  const guild = ctx.store.guild || (await ctx.loadGuild(guildId));

  const topbar = ctx.setTopbar({
    title: "Members",
    sub: `${guild.name} · ${fmtNumber(guild.member_count)} members`,
    actions: `<button class="btn small" id="members-refresh">${icon("refresh", 15)} Refresh</button>`,
  });

  container.innerHTML = `
    <div class="card" style="padding:14px;margin-bottom:16px">
      <div class="searchbar" style="max-width:none">
        ${icon("search")}
        <input id="member-search" placeholder="Search by name, nickname or id…" autocomplete="off">
      </div>
    </div>
    <div id="member-results"></div>`;

  const results = container.querySelector("#member-results");
  let query = "";
  let offset = 0;
  let total = 0;
  let loading = false;
  let items = [];

  const roleName = (roleId) => guild.roles.find((role) => role.id === roleId)?.name;
  const roleColor = (roleId) => guild.roles.find((role) => role.id === roleId)?.color;

  const card = (member) => {
    const top = member.top_role ? roleName(member.top_role) : null;
    return `<div class="member-card" data-member="${member.id}">
      ${avatar(member, 44)}
      <div style="min-width:0;flex:1">
        <div style="font-weight:600;color:${colorFor(member)};overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
          ${esc(member.display)}
        </div>
        <div class="tiny muted" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
          @${esc(member.username)}${member.nickname ? ` · ${esc(member.nickname)}` : ""}
        </div>
        <div style="margin-top:5px;display:flex;gap:5px;flex-wrap:wrap">
          ${member.bot ? `<span class="tag bot">bot</span>` : ""}
          ${top ? `<span class="tag">${esc(top)}</span>` : ""}
        </div>
      </div>
    </div>`;
  };

  const draw = (replace) => {
    if (replace) {
      results.innerHTML = `<div class="member-grid">${items.map(card).join("")}</div>
        ${items.length < total ? `<div style="display:flex;justify-content:center;margin-top:16px"><button class="btn" id="members-more">Load more (${fmtNumber(total - items.length)} left)</button></div>` : ""}
        ${items.length ? "" : `<div class="empty">No members match that search.</div>`}`;
    }
    results.querySelectorAll("[data-member]").forEach((element) => {
      element.addEventListener("click", () =>
        openMember({
          guildId,
          userId: element.dataset.member,
          guild,
          onChange: () => load(true),
        })
      );
    });
    results.querySelector("#members-more")?.addEventListener("click", () => load(false));
  };

  const load = async (reset) => {
    if (loading) return;
    loading = true;
    if (reset) {
      offset = 0;
      items = [];
      results.innerHTML = `<div class="loading-row">Loading members…</div>`;
    }
    try {
      const data = await get(
        `/api/guilds/${guildId}/members?limit=${PAGE}&offset=${offset}&query=${encodeURIComponent(
          query
        )}`
      );
      total = data.total;
      items = reset ? data.members : [...items, ...data.members];
      offset = items.length;
      draw(true);
    } catch (error) {
      toast(error.message, "error");
      results.innerHTML = `<div class="empty">${esc(error.message)}</div>`;
    } finally {
      loading = false;
    }
  };

  container
    .querySelector("#member-search")
    .addEventListener("input", debounce((event) => {
      query = event.target.value.trim();
      load(true);
    }, 280));

  topbar.querySelector("#members-refresh").addEventListener("click", () => load(true));

  await load(true);
}
