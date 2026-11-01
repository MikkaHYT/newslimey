import { get, put } from "../api.js";
import { esc, icon, toast } from "../ui.js";

const DEFAULT_CHANNEL_TYPES = ["text", "news", "public_thread", "private_thread", "forum"];

function option(value, label, selected) {
  return `<option value="${esc(value)}"${selected ? " selected" : ""}>${esc(label)}</option>`;
}

function channelOptions(channels, selected, filter) {
  const allowed = filter || DEFAULT_CHANNEL_TYPES;
  return (
    option("", "Not set", !selected) +
    channels
      .filter((channel) => allowed.includes(channel.type))
      .map((channel) =>
        option(
          channel.id,
          channel.category && channel.type !== "category"
            ? `${channel.category} / ${channel.name}`
            : channel.name,
          String(selected) === String(channel.id)
        )
      )
      .join("")
  );
}

function roleOptions(roles, selected) {
  return (
    option("", "Not set", !selected) +
    roles
      .filter((role) => !role.is_default)
      .map((role) => option(role.id, role.name, String(selected) === String(role.id)))
      .join("")
  );
}

function pickerHtml(field, value, guild) {
  const kind = field.type === "roles" ? "role" : "channel";
  const selected = new Set((value || []).map(String));
  const lookup = (id) => {
    if (kind === "role") return guild?.roles?.find((role) => role.id === id)?.name;
    return guild?.channels?.find((channel) => channel.id === id)?.name;
  };
  const options =
    kind === "role"
      ? (guild?.roles || [])
          .filter((role) => !role.is_default)
          .map((role) => option(role.id, role.name, false))
      : (guild?.channels || [])
          .filter((channel) => DEFAULT_CHANNEL_TYPES.includes(channel.type))
          .map((channel) => option(channel.id, channel.name, false));

  return `<div class="picker" data-picker="${esc(field.key)}" data-kind="${kind}">
    ${[...selected]
      .map(
        (id) => `<span class="pick" data-id="${esc(id)}">${esc(lookup(id) || `#${id}`)}
          <button type="button" data-remove-pick="${esc(id)}" title="Remove">×</button></span>`
      )
      .join("")}
    <select data-add>${option("", `Add ${kind}…`, true)}${options}</select>
  </div>`;
}

function controlHtml(field, value, guild) {
  const key = esc(field.key);
  switch (field.type) {
    case "toggle":
      return `<label class="switch"><input type="checkbox" data-key="${key}" ${
        value ? "checked" : ""
      }><span class="muted tiny">${value ? "On" : "Off"}</span></label>`;
    case "number":
      return `<input type="number" data-key="${key}" value="${value ?? ""}" ${
        field.min != null ? `min="${field.min}"` : ""
      } ${field.max != null ? `max="${field.max}"` : ""} placeholder="default">`;
    case "select":
      return `<select data-key="${key}">${option("", "Default", value == null)}${(
        field.options || []
      )
        .map((item) => option(item.value, item.label, String(value) === String(item.value)))
        .join("")}</select>`;
    case "channel":
      return `<select data-key="${key}">${channelOptions(
        guild?.channels || [],
        value,
        field.channel_type === "voice"
          ? ["voice", "stage_voice"]
          : field.channel_type === "category"
          ? ["category"]
          : undefined
      )}</select>`;
    case "role":
      return `<select data-key="${key}">${roleOptions(guild?.roles || [], value)}</select>`;
    case "channels":
    case "roles":
      return pickerHtml(field, value, guild);
    case "textlist":
      return `<textarea data-key="${key}" rows="4" placeholder="One per line">${esc(
        (value || []).join("\n")
      )}</textarea>`;
    case "idlist":
      return `<input data-key="${key}" value="${esc((value || []).join(", "))}" placeholder="123456789, 987654321">`;
    case "welcome": {
      const config = value || {};
      const color = Number.isFinite(Number(config.color)) && Number(config.color)
        ? `#${Number(config.color).toString(16).padStart(6, "0")}`
        : "#2b2d31";
      return `<div data-welcome>
        <label class="field"><span>Channel</span><select data-welcome="channel_id">${channelOptions(
          guild?.channels || [],
          config.channel_id
        )}</select></label>
        <label class="field"><span>Title</span><input data-welcome="title" value="${esc(
          config.title || ""
        )}" placeholder="Welcome to {server}!"></label>
        <label class="field"><span>Description</span><textarea data-welcome="description" rows="4" placeholder="Hey {user}, welcome!">${esc(
          config.description || ""
        )}</textarea></label>
        <label class="field"><span>Footer</span><input data-welcome="footer" value="${esc(
          config.footer || ""
        )}" placeholder="Member #{membercount}"></label>
        <div style="display:flex;gap:14px;align-items:center;flex-wrap:wrap">
          <label class="field" style="margin:0"><span>Colour</span><input type="color" data-welcome="color" value="${color}" style="width:70px"></label>
          <label class="switch"><input type="checkbox" data-welcome="ping" ${
            config.ping ? "checked" : ""
          }><span class="muted tiny">Ping the member</span></label>
        </div>
      </div>`;
    }
    default:
      return `<input data-key="${key}" value="${esc(value ?? "")}">`;
  }
}

function groupHtml(group, values, guild, fieldsByKey) {
  const rows = group.fields
    .map((field) => {
      const value = values[field.key];
      return `<div class="setting-row" data-field="${esc(field.key)}">
        <div>
          <div class="label">${esc(field.label)}</div>
          ${field.help ? `<div class="help">${esc(field.help)}</div>` : ""}
        </div>
        <div class="control">${controlHtml(field, value, guild)}</div>
      </div>`;
    })
    .join("");

  return `<section class="settings-group" id="group-${group.id}">
    <div class="card-head">
      <h2>${esc(group.label)}</h2>
      <span class="muted">${icon(group.icon || "settings", 17)}</span>
    </div>
    <p>${esc(group.description || "")}</p>
    ${rows}
  </section>`;
}

function readField(wrapper, fieldsByKey) {
  const key = wrapper.dataset.field;
  const field = fieldsByKey[key];
  const picker = wrapper.querySelector("[data-picker]");
  if (picker) {
    return [...picker.querySelectorAll(".pick")].map((pick) => Number(pick.dataset.id));
  }
  const welcome = wrapper.querySelector("[data-welcome]");
  if (welcome) {
    const getValue = (name) => welcome.querySelector(`[data-welcome="${name}"]`);
    return {
      channel_id: getValue("channel_id").value ? Number(getValue("channel_id").value) : null,
      title: getValue("title").value.trim(),
      description: getValue("description").value.trim(),
      footer: getValue("footer").value.trim(),
      color: Number.parseInt(getValue("color").value.replace("#", ""), 16) || 0,
      ping: getValue("ping").checked,
    };
  }
  const input = wrapper.querySelector("[data-key]");
  if (!input) return null;
  if (field.type === "toggle") return input.checked;
  if (field.type === "number" || field.type === "channel" || field.type === "role") {
    return input.value === "" ? null : Number(input.value);
  }
  if (field.type === "idlist") {
    return input.value
      .split(",")
      .map((part) => Number(part.trim()))
      .filter((number) => Number.isFinite(number) && number > 0);
  }
  if (field.type === "textlist") {
    return input.value
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean);
  }
  const text = input.value.trim();
  return text || null;
}

export async function mount(container, ctx) {
  const isGlobal = ctx.route.view === "global-settings";
  const guildId = ctx.store.guildId;
  const guild = isGlobal ? null : ctx.store.guild || (await ctx.loadGuild(guildId));
  const endpoint = isGlobal ? "/api/settings/global" : `/api/guilds/${guildId}/settings`;

  let data;
  try {
    data = await get(endpoint);
  } catch (error) {
    ctx.setTopbar({ title: isGlobal ? "Bot-wide settings" : "Settings" });
    container.innerHTML = `<div class="empty">${esc(error.message)}</div>`;
    return;
  }

  const fieldsByKey = {};
  for (const group of data.schema) {
    for (const field of group.fields) fieldsByKey[field.key] = field;
  }

  const topbar = ctx.setTopbar({
    title: isGlobal ? "Bot-wide settings" : "Settings",
    sub: isGlobal ? "Applies to every server the bot is in" : guild.name,
    actions: `<button class="btn small primary" id="save-settings">Save changes</button>`,
  });

  container.innerHTML = `
    <div class="settings-layout">
      <aside class="settings-nav">
        ${data.schema
          .map(
            (group) =>
              `<button type="button" data-jump="group-${group.id}">${esc(group.label)}</button>`
          )
          .join("")}
        <button type="button" data-jump="group-advanced">Advanced</button>
      </aside>
      <div>
        ${
          isGlobal
            ? `<p class="muted" style="margin-top:0">These switches sit above every server. Per-server options live under each server's <strong>Settings</strong> tab.</p>`
            : ""
        }
        ${data.schema.map((group) => groupHtml(group, data.values, guild, fieldsByKey)).join("")}

        <section class="settings-group" id="group-advanced">
          <div class="card-head"><h2>Advanced</h2><span class="muted">${icon("sliders", 17)}</span></div>
          <p>Raw <code>bot_settings</code> keys the dashboard does not have a prettier control for. Values are JSON.</p>
          <div class="card" style="padding:8px 12px">
            <table class="advanced-table" id="advanced-table"></table>
          </div>
          <div class="card" style="margin-top:12px">
            <h3>Add a setting</h3>
            <div class="grid-2" style="grid-template-columns:180px 1fr;gap:8px">
              <label class="field" style="margin:0"><span>Key</span><input id="new-key" placeholder="my_key"></label>
              <label class="field" style="margin:0"><span>JSON value</span><input id="new-value" placeholder='"value" or 123 or [1, 2]'></label>
            </div>
            <button class="btn small primary" id="add-setting" type="button" style="margin-top:10px">Add</button>
          </div>
        </section>

        <div class="savebar">
          <span class="note" id="save-note">No unsaved changes.</span>
          <div class="spacer"></div>
          <button class="btn small" id="discard-changes" type="button">Discard</button>
          <button class="btn small primary" id="save-settings-bottom" type="button">Save changes</button>
        </div>
      </div>
    </div>`;

  const note = container.querySelector("#save-note");
  const setDirty = (dirty) => {
    note.textContent = dirty ? "Unsaved changes." : "No unsaved changes.";
    note.style.color = dirty ? "var(--warn)" : "";
  };

  // ---------------------------------------------------------- advanced rows
  const drawAdvanced = (advanced) => {
    const table = container.querySelector("#advanced-table");
    table.innerHTML =
      advanced
        .map(
          (entry) => `<tr>
            <td>${esc(entry.key)}</td>
            <td><textarea data-advanced="${esc(entry.key)}" rows="2" style="font-family:ui-monospace,monospace;font-size:12px">${esc(
            JSON.stringify(entry.value)
          )}</textarea></td>
            <td style="white-space:nowrap;text-align:right">
              <button class="btn small" data-save-advanced="${esc(entry.key)}" type="button">Save</button>
              <button class="btn small ghost" data-delete-advanced="${esc(entry.key)}" type="button">Delete</button>
            </td>
          </tr>`
        )
        .join("") ||
      `<tr><td colspan="3" class="muted tiny" style="padding:12px">Nothing custom stored for this scope.</td></tr>`;

    table.querySelectorAll("[data-save-advanced]").forEach((button) => {
      button.addEventListener("click", async () => {
        const key = button.dataset.saveAdvanced;
        const raw = table.querySelector(`[data-advanced="${CSS.escape(key)}"]`).value;
        let parsed;
        try {
          parsed = JSON.parse(raw);
        } catch {
          toast("That is not valid JSON", "error");
          return;
        }
        button.disabled = true;
        try {
          await put(endpoint, { updates: { [key]: parsed } });
          toast(`${key} saved`);
        } catch (error) {
          toast(error.message, "error");
        } finally {
          button.disabled = false;
        }
      });
    });

    table.querySelectorAll("[data-delete-advanced]").forEach((button) => {
      button.addEventListener("click", async () => {
        const key = button.dataset.deleteAdvanced;
        button.disabled = true;
        try {
          const response = await put(endpoint, { deletes: [key] });
          drawAdvanced(response.advanced);
          toast(`${key} removed`);
        } catch (error) {
          toast(error.message, "error");
          button.disabled = false;
        }
      });
    });
  };

  drawAdvanced(data.advanced);

  // ------------------------------------------------------------- pickers
  const bindPickers = (root) => {
    root.querySelectorAll("[data-picker]").forEach((picker) => {
      const kind = picker.dataset.kind;
      const lookup = (id) => {
        if (kind === "role") return guild?.roles?.find((role) => role.id === id)?.name;
        return guild?.channels?.find((channel) => channel.id === id)?.name;
      };
      picker.querySelector("[data-add]").addEventListener("change", (event) => {
        const id = event.target.value;
        if (!id) return;
        if ([...picker.querySelectorAll(".pick")].some((pick) => pick.dataset.id === id)) {
          toast("Already added", "warn");
        } else {
          const chip = document.createElement("span");
          chip.className = "pick";
          chip.dataset.id = id;
          chip.innerHTML = `${esc(lookup(id) || `#${id}`)}<button type="button" data-remove-pick="${esc(
            id
          )}" title="Remove">×</button>`;
          picker.insertBefore(chip, picker.querySelector("[data-add]"));
        }
        event.target.value = "";
        setDirty(true);
      });
      picker.addEventListener("click", (event) => {
        const remove = event.target.closest("[data-remove-pick]");
        if (!remove) return;
        remove.closest(".pick")?.remove();
        setDirty(true);
      });
    });
  };

  const bindControls = (root) => {
    root.querySelectorAll("input, select, textarea").forEach((input) => {
      input.addEventListener("change", () => setDirty(true));
      input.addEventListener("input", () => setDirty(true));
      // Keep the little On/Off caption next to a switch in sync with it.
      if (input.type === "checkbox" && input.closest(".switch")) {
        input.addEventListener("change", () => {
          const caption = input.closest(".switch").querySelector("span.muted");
          if (caption) caption.textContent = input.checked ? "On" : "Off";
        });
      }
    });
  };

  bindPickers(container);
  bindControls(container);

  // ---------------------------------------------------------------- save
  const save = async () => {
    const updates = {};
    container.querySelectorAll("[data-field]").forEach((wrapper) => {
      updates[wrapper.dataset.field] = readField(wrapper, fieldsByKey);
    });

    const buttons = [
      topbar.querySelector("#save-settings"),
      container.querySelector("#save-settings-bottom"),
    ].filter(Boolean);
    buttons.forEach((button) => (button.disabled = true));
    try {
      const response = await put(endpoint, { updates });
      data = { ...data, values: response.values, advanced: response.advanced };
      const errors = Object.entries(response.errors || {});
      if (errors.length) {
        toast(errors.map(([key, message]) => `${key}: ${message}`).join(" · "), "error", 7000);
      } else {
        toast("Settings saved");
        setDirty(false);
        container.querySelectorAll(".switch span").forEach((span) => {
          /* labels are refreshed on the next load */
        });
      }
      drawAdvanced(response.advanced);
    } catch (error) {
      toast(error.message, "error");
    } finally {
      buttons.forEach((button) => (button.disabled = false));
    }
  };

  topbar.querySelector("#save-settings").addEventListener("click", save);
  container.querySelector("#save-settings-bottom").addEventListener("click", save);
  container.querySelector("#discard-changes").addEventListener("click", () => {
    container.querySelectorAll("[data-field]").forEach((wrapper) => {
      const field = fieldsByKey[wrapper.dataset.field];
      const current = data.values[field.key];
      wrapper.querySelector(".control").innerHTML = controlHtml(field, current, guild);
    });
    bindPickers(container);
    bindControls(container);
    setDirty(false);
    toast("Changes discarded");
  });

  container.querySelectorAll("[data-jump]").forEach((button) => {
    button.addEventListener("click", () => {
      container
        .querySelector(`#${button.dataset.jump}`)
        ?.scrollIntoView({ behavior: "smooth", block: "start" });
      container
        .querySelectorAll("[data-jump]")
        .forEach((other) => other.classList.toggle("active", other === button));
    });
  });

  container.querySelector("#add-setting").addEventListener("click", async () => {
    const key = container.querySelector("#new-key").value.trim();
    const raw = container.querySelector("#new-value").value.trim();
    if (!key) {
      toast("Give the setting a key", "warn");
      return;
    }
    let parsed;
    try {
      parsed = JSON.parse(raw || "null");
    } catch {
      toast("That is not valid JSON", "error");
      return;
    }
    try {
      const response = await put(endpoint, { updates: { [key]: parsed } });
      drawAdvanced(response.advanced);
      container.querySelector("#new-key").value = "";
      container.querySelector("#new-value").value = "";
      toast(`${key} saved`);
    } catch (error) {
      toast(error.message, "error");
    }
  });
}
