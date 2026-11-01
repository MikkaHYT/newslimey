// Member profile used by the chat member list and the members view: nickname,
// role toggles and moderation actions in one place.

import { get, post } from "../api.js";
import {
  avatar,
  colorFor,
  confirmDialog,
  esc,
  fmtDateTime,
  icon,
  openModal,
  timeAgo,
  toast,
} from "../ui.js";

function roleChip(role, selected) {
  const color = role.color ? `#${Number(role.color).toString(16).padStart(6, "0")}` : "";
  return `<button type="button" class="role-chip${selected ? " on" : ""}" data-role="${role.id}" ${
    role.managed ? "disabled title=\"Managed by an integration\"" : ""
  }>
    <span class="swatch" style="background:${color || "var(--muted-2)"}"></span>${esc(role.name)}
  </button>`;
}

export async function openMember({ guildId, userId, guild, onChange }) {
  const modal = openModal({ title: "Member", body: `<div class="loading-row">Loading…</div>` });

  const loadGuild = async () =>
    guild || (await get(`/api/guilds/${guildId}`));

  let detail = null;
  let guildData = null;

  const draw = () => {
    if (!detail) return;
    const roles = (guildData?.roles || [])
      .filter((role) => !role.is_default && !role.managed)
      .sort((a, b) => b.position - a.position);
    const held = new Set(detail.roles || []);

    modal.body.innerHTML = `
      <div style="display:flex;gap:16px;align-items:center;margin-bottom:18px">
        ${avatar(detail, 64)}
        <div style="min-width:0">
          <div style="font-size:18px;font-weight:700;color:${colorFor(detail)}">${esc(detail.display)}</div>
          <div class="muted tiny">@${esc(detail.username)}${detail.nickname ? ` · nick: ${esc(detail.nickname)}` : ""}</div>
          <div style="margin-top:6px;display:flex;gap:6px;flex-wrap:wrap">
            ${detail.bot ? `<span class="tag bot">bot</span>` : ""}
            ${detail.is_owner ? `<span class="tag owner">owner</span>` : ""}
            <span class="tag">${esc(detail.status || "offline")}</span>
            <span class="tag">${esc(detail.id)}</span>
          </div>
        </div>
      </div>

      <div class="grid-2" style="gap:14px">
        <div class="card pad-sm">
          <div class="muted tiny">Joined server</div>
          <div>${detail.joined_at ? fmtDateTime(detail.joined_at) : "—"} <span class="muted tiny">(${detail.joined_at ? timeAgo(detail.joined_at) : "—"})</span></div>
        </div>
        <div class="card pad-sm">
          <div class="muted tiny">Account created</div>
          <div>${detail.created_at ? fmtDateTime(detail.created_at) : "—"}</div>
        </div>
      </div>

      <div class="section-title">Nickname</div>
      <div style="display:flex;gap:8px">
        <input id="mm-nick" placeholder="${esc(detail.nickname || "no nickname")}" value="${esc(detail.nickname || "")}">
        <button class="btn small" id="mm-save-nick" type="button">Save</button>
        <button class="btn small ghost" id="mm-clear-nick" type="button">Clear</button>
      </div>

      <div class="section-title">Roles</div>
      <div class="role-chips" id="mm-roles">
        ${roles.map((role) => roleChip(role, held.has(role.id))).join("") || `<span class="muted">No assignable roles.</span>`}
      </div>

      <div class="section-title">Moderation</div>
      <label class="field"><span>Reason</span><input id="mm-reason" placeholder="Shown in the audit log"></label>
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        <label class="field" style="margin:0;width:120px"><span>Minutes</span><input id="mm-minutes" type="number" min="0" max="40320" value="10"></label>
        <button class="btn small danger" data-mod="timeout" type="button">Timeout</button>
        <button class="btn small danger" data-mod="kick" type="button">Kick</button>
        <button class="btn small danger" data-mod="ban" type="button">Ban</button>
        <label class="field" style="margin:0;width:130px"><span>Delete days</span><input id="mm-days" type="number" min="0" max="7" value="0"></label>
        <button class="btn small ghost" id="mm-unban" type="button">Unban</button>
      </div>

      <div class="section-title">Recent cases</div>
      ${
        detail.cases?.length
          ? `<table class="table"><tbody>${detail.cases
              .map(
                (entry) => `<tr>
                  <td class="muted">#${entry.case_id}</td>
                  <td><span class="chip">${esc(entry.type)}</span></td>
                  <td>${esc(entry.reason || "no reason")}</td>
                  <td class="muted tiny">by ${esc(entry.moderator?.display || "unknown")} · ${timeAgo(entry.created_on)}</td>
                </tr>`
              )
              .join("")}</tbody></table>`
          : `<p class="muted tiny">No moderation history for this member.</p>`
      }`;

    const reason = () => modal.body.querySelector("#mm-reason")?.value?.trim() || "";
    const refresh = async () => {
      detail = await get(`/api/guilds/${guildId}/members/${detail.id}`);
      draw();
      onChange?.(detail);
    };

    modal.body.querySelector("#mm-save-nick").addEventListener("click", async () => {
      try {
        const nickname = modal.body.querySelector("#mm-nick").value.trim();
        await post(`/api/guilds/${guildId}/members/${detail.id}/nickname`, { nickname });
        toast("Nickname updated");
        await refresh();
      } catch (error) {
        toast(error.message, "error");
      }
    });

    modal.body.querySelector("#mm-clear-nick").addEventListener("click", async () => {
      try {
        await post(`/api/guilds/${guildId}/members/${detail.id}/nickname`, { nickname: "" });
        toast("Nickname cleared");
        await refresh();
      } catch (error) {
        toast(error.message, "error");
      }
    });

    modal.body.querySelectorAll("#mm-roles .role-chip").forEach((chip) => {
      chip.addEventListener("click", async () => {
        const roleId = chip.dataset.role;
        const adding = !chip.classList.contains("on");
        chip.disabled = true;
        try {
          await post(`/api/guilds/${guildId}/members/${detail.id}/roles`, {
            add: adding ? [roleId] : [],
            remove: adding ? [] : [roleId],
          });
          chip.classList.toggle("on", adding);
        } catch (error) {
          toast(error.message, "error");
        } finally {
          chip.disabled = false;
        }
      });
    });

    modal.body.querySelectorAll("[data-mod]").forEach((button) => {
      button.addEventListener("click", async () => {
        const action = button.dataset.mod;
        const minutes = Number(modal.body.querySelector("#mm-minutes").value || 0);
        const days = Number(modal.body.querySelector("#mm-days").value || 0);
        const text =
          action === "timeout"
            ? `${minutes ? `Time ${detail.display} out for ${minutes} minutes` : `Remove ${detail.display}'s timeout`}?`
            : `${action === "kick" ? "Kick" : "Ban"} ${detail.display}?`;
        if (!(await confirmDialog("Are you sure?", text, { confirmLabel: action, danger: true }))) {
          return;
        }
        try {
          if (action === "timeout") {
            await post(`/api/guilds/${guildId}/members/${detail.id}/timeout`, {
              minutes,
              reason: reason() || undefined,
            });
            toast(minutes ? `Timed out for ${minutes} minutes` : "Timeout removed");
            await refresh();
          } else if (action === "kick") {
            await post(`/api/guilds/${guildId}/members/${detail.id}/kick`, {
              reason: reason() || undefined,
            });
            toast("Kicked");
            modal.close();
          } else {
            await post(`/api/guilds/${guildId}/members/${detail.id}/ban`, {
              reason: reason() || undefined,
              delete_days: days,
            });
            toast("Banned");
            modal.close();
          }
          onChange?.(detail);
        } catch (error) {
          toast(error.message, "error");
        }
      });
    });

    modal.body.querySelector("#mm-unban").addEventListener("click", async () => {
      if (!(await confirmDialog("Lift the ban?", `Unban ${detail.display}?`, { confirmLabel: "Unban" }))) {
        return;
      }
      try {
        await post(`/api/guilds/${guildId}/members/${detail.id}/unban`, {
          reason: reason() || undefined,
        });
        toast("Ban lifted");
      } catch (error) {
        toast(error.message, "error");
      }
    });
  };

  try {
    const [member, guildInfo] = await Promise.all([
      get(`/api/guilds/${guildId}/members/${userId}`),
      loadGuild().catch(() => null),
    ]);
    detail = member;
    guildData = guildInfo;
    draw();
    const heading = modal.card.querySelector(".modal-head h2");
    if (heading) heading.textContent = detail.display;
  } catch (error) {
    modal.body.innerHTML = `<div class="empty">${esc(error.message)}</div>`;
  }
}
