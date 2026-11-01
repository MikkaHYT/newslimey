import { get, post } from "../api.js";
import { debounce, esc, icon, toast } from "../ui.js";

let activeTab = "prefix";

export async function mount(container, ctx) {
  const guildId = ctx.store.guildId;
  const guild = ctx.store.guild || (await ctx.loadGuild(guildId));

  let data;
  try {
    data = await get(`/api/commands?guild_id=${guildId}`);
  } catch (error) {
    ctx.setTopbar({ title: "Commands" });
    container.innerHTML = `<div class="empty">${esc(error.message)}</div>`;
    return;
  }

  const topbar = ctx.setTopbar({
    title: "Commands",
    sub: `${guild.name} · ${data.commands.length + data.slash.length} commands`,
    actions: `<button class="btn small" id="commands-refresh">${icon("refresh", 15)} Refresh</button>`,
  });

  container.innerHTML = `
    <div class="tabs">
      <button class="tab" data-tab="prefix" type="button">Prefix commands</button>
      <button class="tab" data-tab="slash" type="button">Slash commands</button>
    </div>
    <div class="searchbar" style="max-width:420px;margin-bottom:16px">
      ${icon("search")}
      <input id="command-search" placeholder="Search commands…" autocomplete="off">
    </div>
    <div id="command-list"></div>

    <div class="section-title">Maintenance</div>
    <div class="grid-2">
      <div class="card">
        <h3>Whole bot</h3>
        <p class="muted tiny">When set, every command is refused with this reason.</p>
        <label class="field"><span>Reason</span>
          <input id="global-maintenance" placeholder="e.g. upgrading the bot" value="${esc(
            data.maintenance.global || ""
          )}">
        </label>
        <div style="display:flex;gap:8px">
          <button class="btn primary small" id="save-global-maintenance" type="button">Save</button>
          <button class="btn small ghost" id="clear-global-maintenance" type="button">Clear</button>
        </div>
      </div>
      <div class="card">
        <h3>One command</h3>
        <p class="muted tiny">Turn off a single command while you work on it.</p>
        <div class="grid-2" style="grid-template-columns:1fr 1fr;gap:8px">
          <label class="field" style="margin:0"><span>Command</span>
            <select id="maintenance-command">
              ${data.commands
                .map(
                  (command) =>
                    `<option value="${esc(command.qualified)}">${esc(command.qualified)}</option>`
                )
                .join("")}
            </select>
          </label>
          <label class="field" style="margin:0"><span>Reason</span>
            <input id="maintenance-reason" placeholder="short reason">
          </label>
        </div>
        <div style="display:flex;gap:8px;margin-top:10px">
          <button class="btn primary small" id="apply-maintenance" type="button">Apply</button>
        </div>
        <div id="maintenance-list" style="margin-top:12px"></div>
      </div>
    </div>`;

  const list = container.querySelector("#command-list");
  let query = "";

  const drawMaintenance = () => {
    const holder = container.querySelector("#maintenance-list");
    const entries = Object.entries(data.maintenance.commands || {});
    holder.innerHTML = entries.length
      ? entries
          .map(
            ([name, reason]) => `<div style="display:flex;align-items:center;gap:8px;margin-top:6px">
              <span class="chip">${esc(name)}</span>
              <span class="tiny muted" style="flex:1">${esc(reason)}</span>
              <button class="btn small ghost" data-clear-maintenance="${esc(name)}" type="button">Remove</button>
            </div>`
          )
          .join("")
      : `<p class="tiny muted">No command is paused.</p>`;

    holder.querySelectorAll("[data-clear-maintenance]").forEach((button) => {
      button.addEventListener("click", async () => {
        try {
          const response = await post("/api/maintenance", {
            guild_id: guildId,
            command: button.dataset.clearMaintenance,
            reason: "",
          });
          data.maintenance = response.maintenance;
          drawMaintenance();
          toast("Command resumed");
        } catch (error) {
          toast(error.message, "error");
        }
      });
    });
  };

  const rowsFor = (items) =>
    items
      .filter(
        (command) =>
          !query ||
          command.qualified.toLowerCase().includes(query) ||
          (command.help || "").toLowerCase().includes(query) ||
          (command.cog || "").toLowerCase().includes(query)
      )
      .map(
        (command) => `<div class="card pad-sm" style="display:flex;gap:14px;align-items:center;margin-bottom:8px">
          <div style="flex:1;min-width:0">
            <div class="mono" style="font-weight:600;font-size:13px">
              ${esc(activeTab === "prefix" ? `,${command.qualified}` : `/${command.qualified}`)}
              ${
                command.signature
                  ? `<span class="muted" style="font-weight:400">${esc(command.signature)}</span>`
                  : ""
              }
            </div>
            <div class="tiny muted">${esc(command.help || command.description || "No description")}</div>
            ${
              command.aliases?.length
                ? `<div class="tiny muted">aliases: ${esc(command.aliases.join(", "))}</div>`
                : ""
            }
          </div>
          ${
            activeTab === "prefix"
              ? `<span class="tag">${esc(command.cog || "Other")}</span>`
              : ""
          }
          <label class="switch" title="Enable or disable in this server">
            <input type="checkbox" data-command="${esc(command.qualified)}" ${
              command.disabled ? "" : "checked"
            }>
            <span></span>
          </label>
        </div>`
      );

  const draw = () => {
    container.querySelectorAll(".tab").forEach((tab) => {
      tab.classList.toggle("active", tab.dataset.tab === activeTab);
    });

    if (activeTab === "prefix") {
      const groups = new Map();
      for (const command of data.commands) {
        if (!groups.has(command.cog)) groups.set(command.cog, []);
        groups.get(command.cog).push(command);
      }
      list.innerHTML = [...groups.keys()]
        .sort()
        .map(
          (cog) => `<div class="section-title">${esc(cog)}</div>${rowsFor(groups.get(cog)).join("")}`
        )
        .join("");
    } else {
      const filtered = data.slash.filter(
        (command) =>
          !query ||
          command.qualified.toLowerCase().includes(query) ||
          (command.description || "").toLowerCase().includes(query)
      );
      list.innerHTML =
        filtered
          .map(
            (command) => `<div class="card pad-sm" style="display:flex;gap:14px;align-items:center;margin-bottom:8px">
              <div style="flex:1;min-width:0">
                <div class="mono" style="font-weight:600;font-size:13px">/${esc(command.qualified)}</div>
                <div class="tiny muted">${esc(command.description || "No description")}</div>
              </div>
              <label class="switch" title="Enable or disable in this server">
                <input type="checkbox" data-command="${esc(command.qualified)}" ${
              command.disabled ? "" : "checked"
            }>
                <span></span>
              </label>
            </div>`
          )
          .join("") || `<div class="empty">No slash commands match that search.</div>`;
    }

    list.querySelectorAll("[data-command]").forEach((input) => {
      input.addEventListener("change", async () => {
        const name = input.dataset.command;
        input.disabled = true;
        try {
          await post(`/api/guilds/${guildId}/commands/${encodeURIComponent(name)}`, {
            enabled: input.checked,
          });
          const record = [...data.commands, ...data.slash].find(
            (command) => command.qualified === name
          );
          if (record) record.disabled = !input.checked;
          toast(`${name} ${input.checked ? "enabled" : "disabled"} in this server`);
        } catch (error) {
          input.checked = !input.checked;
          toast(error.message, "error");
        } finally {
          input.disabled = false;
        }
      });
    });
  };

  container.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      activeTab = tab.dataset.tab;
      draw();
    });
  });

  container
    .querySelector("#command-search")
    .addEventListener("input", debounce((event) => {
      query = event.target.value.trim().toLowerCase();
      draw();
    }, 200));

  topbar.querySelector("#commands-refresh").addEventListener("click", async () => {
    try {
      data = await get(`/api/commands?guild_id=${guildId}`);
      draw();
      drawMaintenance();
    } catch (error) {
      toast(error.message, "error");
    }
  });

  container.querySelector("#save-global-maintenance").addEventListener("click", async () => {
    const reason = container.querySelector("#global-maintenance").value.trim();
    try {
      const response = await post("/api/maintenance", { reason });
      data.maintenance = response.maintenance;
      drawMaintenance();
      toast(reason ? "Maintenance mode on" : "Maintenance mode off");
    } catch (error) {
      toast(error.message, "error");
    }
  });

  container.querySelector("#clear-global-maintenance").addEventListener("click", async () => {
    container.querySelector("#global-maintenance").value = "";
    try {
      const response = await post("/api/maintenance", { reason: "" });
      data.maintenance = response.maintenance;
      drawMaintenance();
      toast("Maintenance mode off");
    } catch (error) {
      toast(error.message, "error");
    }
  });

  container.querySelector("#apply-maintenance").addEventListener("click", async () => {
    const command = container.querySelector("#maintenance-command").value;
    const reason = container.querySelector("#maintenance-reason").value.trim();
    if (!reason) {
      toast("Give a reason first", "warn");
      return;
    }
    try {
      const response = await post("/api/maintenance", {
        guild_id: guildId,
        command,
        reason,
      });
      data.maintenance = response.maintenance;
      drawMaintenance();
      container.querySelector("#maintenance-reason").value = "";
      toast(`${command} paused`);
    } catch (error) {
      toast(error.message, "error");
    }
  });

  draw();
  drawMaintenance();
}
