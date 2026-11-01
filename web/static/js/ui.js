// Shared presentation helpers. No state beyond the toast/modal containers.

// ------------------------------------------------------------------ escape
export function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

// ------------------------------------------------------------------- icons
const ICONS = {
  overview:
    '<path d="M4 13h6V4H4v9Zm10 7h6v-9h-6v9ZM4 20h6v-3H4v3Zm10-13h6V4h-6v3Z"/>',
  chat: '<path d="M4 6a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H9l-5 4V6Z"/>',
  members:
    '<circle cx="9" cy="8" r="3.2"/><path d="M3.5 19c.6-3.2 2.8-5 5.5-5s4.9 1.8 5.5 5"/><circle cx="17" cy="9" r="2.4"/><path d="M15.4 14.2c2.2.2 3.9 1.7 4.4 4.3"/>',
  shield: '<path d="M12 3 5 6v5.5c0 4 2.9 7.5 7 9.5 4.1-2 7-5.5 7-9.5V6l-7-3Z"/><path d="m9 12 2.2 2.2L15.5 10"/>',
  commands: '<rect x="3.5" y="4.5" width="17" height="15" rx="2.5"/><path d="m8 9 3 3-3 3M13 15h3.5"/>',
  settings:
    '<path d="M4 7h9M17 7h3M4 12h3M11 12h9M4 17h7M15 17h5"/><circle cx="15" cy="7" r="2"/><circle cx="9" cy="12" r="2"/><circle cx="13" cy="17" r="2"/>',
  globe:
    '<circle cx="12" cy="12" r="8.5"/><path d="M3.5 12h17M12 3.5c4.5 4.7 4.5 12.3 0 17-4.5-4.7-4.5-12.3 0-17Z"/>',
  search: '<circle cx="11" cy="11" r="6.5"/><path d="m20 20-4-4"/>',
  send: '<path d="m4 12 16-7-6 15-3.2-6.2L4 12Z"/>',
  attach: '<path d="M20 11.5 12 19.5a4.5 4.5 0 0 1-6.4-6.4l8-8a3 3 0 0 1 4.3 4.3l-8 8a1.5 1.5 0 0 1-2.2-2.2l7.2-7.2"/>',
  close: '<path d="m6 6 12 12M18 6 6 18"/>',
  trash: '<path d="M5 7h14M10 7V5h4v2M7 7l1 13h8l1-13M10 11v5M14 11v5"/>',
  edit: '<path d="M4 20h4L20 8l-4-4L4 16v4Z"/><path d="m13.5 6.5 4 4"/>',
  reply: '<path d="M9 7 4 12l5 5"/><path d="M4 12h9a6 6 0 0 1 6 6v1"/>',
  refresh: '<path d="M20 12a8 8 0 1 1-2.3-5.6"/><path d="M20 4v4h-4"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  menu: '<path d="M4 7h16M4 12h16M4 17h16"/>',
  hash: '<path d="M9 4 7 20M17 4l-2 16M4 9h16M3 15h16"/>',
  users: '<circle cx="9" cy="8" r="3"/><path d="M3.8 19c.5-3 2.7-4.8 5.2-4.8S13.7 16 14.2 19"/><path d="M16 5.5a3 3 0 0 1 0 5.6M17 14.4c2 .4 3.4 2 3.8 4.6"/>',
  check: '<path d="m5 13 4 4L19 7"/>',
  external: '<path d="M14 4h6v6M20 4l-9 9"/><path d="M18 14v4a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4"/>',
  image: '<rect x="3.5" y="4.5" width="17" height="15" rx="2.5"/><circle cx="9" cy="10" r="1.6"/><path d="m5 17 4.5-4.5 3.5 3.5 2.5-2.5L20 17"/>',
  file: '<path d="M6 3h8l4 4v14H6V3Z"/><path d="M14 3v5h5"/>',
  music: '<circle cx="7" cy="18" r="2.5"/><circle cx="18" cy="15.5" r="2.5"/><path d="M9.5 18V7l11-2v10.5"/>',
  star: '<path d="m12 3 2.7 5.7 6.3.8-4.6 4.3 1.2 6.2-5.6-3.1-5.6 3.1 1.2-6.2L3 9.5l6.3-.8L12 3Z"/>',
  hand: '<path d="M9 11V5.5a1.5 1.5 0 0 1 3 0V11"/><path d="M12 10.5V4.5a1.5 1.5 0 0 1 3 0V11"/><path d="M15 11V6.5a1.5 1.5 0 0 1 3 0V15a6 6 0 0 1-6 6h-1a7 7 0 0 1-6.2-3.8L3 13.5a1.6 1.6 0 0 1 2.8-1.5L9 15"/>',
  scroll: '<path d="M6 4h11a2 2 0 0 1 2 2v12a2 2 0 0 0 2 2H8a2 2 0 0 1-2-2V4Z"/><path d="M9 8h7M9 12h7M9 16h4"/>',
  target: '<circle cx="12" cy="12" r="8.5"/><circle cx="12" cy="12" r="4.5"/><circle cx="12" cy="12" r="1"/>',
  sparkles: '<path d="M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5 18 18M18 6l-2.5 2.5M8.5 15.5 6 18"/>',
  mic: '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5.5 11.5a6.5 6.5 0 0 0 13 0M12 18v3"/>',
  sliders: '<path d="M4 7h9M17 7h3M4 12h3M11 12h9M4 17h7M15 17h5"/><circle cx="15" cy="7" r="2"/><circle cx="9" cy="12" r="2"/><circle cx="13" cy="17" r="2"/>',
};

export function icon(name, size = 18) {
  const body = ICONS[name] || ICONS.chat;
  return `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${body}</svg>`;
}

// ------------------------------------------------------------- formatting
export function fmtNumber(value) {
  return new Intl.NumberFormat().format(Number(value) || 0);
}

export function fmtBytes(bytes) {
  const size = Number(bytes) || 0;
  if (size < 1024) return `${size} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = size;
  let unit = -1;
  do {
    value /= 1024;
    unit += 1;
  } while (value >= 1024 && unit < units.length - 1);
  return `${value.toFixed(value >= 10 ? 0 : 1)} ${units[unit]}`;
}

export function fmtUptime(seconds) {
  const total = Math.max(0, Math.floor(Number(seconds) || 0));
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  if (days) return `${days}d ${hours}h`;
  if (hours) return `${hours}h ${minutes}m`;
  if (minutes) return `${minutes}m`;
  return `${total}s`;
}

const timeFmt = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" });
const dateFmt = new Intl.DateTimeFormat(undefined, {
  day: "numeric",
  month: "short",
  year: "numeric",
});
const fullFmt = new Intl.DateTimeFormat(undefined, {
  day: "numeric",
  month: "short",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
});

function asDate(value) {
  if (!value) return null;
  const date = value instanceof Date ? value : new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

export function fmtTime(value) {
  const date = asDate(value);
  return date ? timeFmt.format(date) : "";
}

export function fmtDate(value) {
  const date = asDate(value);
  return date ? dateFmt.format(date) : "";
}

export function fmtDateTime(value) {
  const date = asDate(value);
  return date ? fullFmt.format(date) : "";
}

export function dayKey(value) {
  const date = asDate(value);
  return date ? date.toDateString() : "";
}

export function timeAgo(value) {
  const date = asDate(value);
  if (!date) return "";
  const diff = (Date.now() - date.getTime()) / 1000;
  if (diff < 45) return "just now";
  if (diff < 3600) return `${Math.round(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.round(diff / 3600)}h ago`;
  if (diff < 604800) return `${Math.round(diff / 86400)}d ago`;
  return fmtDate(value);
}

// ---------------------------------------------------------------- identity
export function colorFor(user) {
  const value = Number(user?.color);
  return value ? `#${value.toString(16).padStart(6, "0")}` : "inherit";
}

export function avatar(user, size = 36) {
  const name = (user?.display || user?.username || "?").trim();
  const initial = esc(name.slice(0, 1).toUpperCase() || "?");
  if (user?.avatar) {
    return `<img class="avatar" src="${esc(user.avatar)}" width="${size}" height="${size}" alt="" loading="lazy">`;
  }
  return `<span class="avatar fallback" style="width:${size}px;height:${size}px;font-size:${Math.round(
    size * 0.4
  )}px">${initial}</span>`;
}

export function identity(user, { size = 36, sub = true } = {}) {
  const handle = user?.nickname || user?.global_name || user?.username || "";
  const secondary = sub && handle && handle !== user?.display ? `@${esc(handle)}` : "";
  return `<div class="identity">
    ${avatar(user, size)}
    <div class="who">
      <div class="n" style="color:${colorFor(user)}">${esc(user?.display || "Unknown")}</div>
      ${secondary ? `<div class="h">${secondary}</div>` : ""}
    </div>
  </div>`;
}

// ---------------------------------------------------------------- markdown
const URL_RE = /\bhttps?:\/\/[^\s<>"']+/g;

export function renderContent(raw, context = {}) {
  if (!raw) return "";
  const mentions = context.mentions || {};
  const roles = context.role_mentions || {};
  const channels = context.channel_mentions || {};

  let text = esc(raw);
  const blocks = [];
  const hold = (html) => {
    blocks.push(html);
    return `\u0000B${blocks.length - 1}\u0000`;
  };

  text = text.replace(/```([\s\S]*?)```/g, (_, body) =>
    hold(`<pre><code>${body.replace(/^[a-zA-Z0-9_-]*\n/, "")}</code></pre>`)
  );
  text = text.replace(/`([^`\n]+)`/g, (_, body) => hold(`<code>${body}</code>`));

  text = text.replace(/&lt;(a?):([A-Za-z0-9_~]+):(\d+)&gt;/g, (_, animated, name, id) => {
    const ext = animated ? "gif" : "png";
    return hold(
      `<img class="emoji" src="https://cdn.discordapp.com/emojis/${id}.${ext}" alt=":${name}:" title=":${name}:" loading="lazy">`
    );
  });

  text = text.replace(/&lt;@!?(\d+)&gt;/g, (_, id) => {
    const user = mentions[id];
    const name = user?.display || user?.username || "user";
    const color = user?.color ? colorFor(user) : "";
    return hold(`<span class="mention"${color ? ` style="color:${color}"` : ""}>@${esc(name)}</span>`);
  });

  text = text.replace(/&lt;@&(\d+)&gt;/g, (_, id) => {
    const role = roles[id];
    return hold(`<span class="mention">@${esc(role?.name || "role")}</span>`);
  });

  text = text.replace(/&lt;#(\d+)&gt;/g, (_, id) => {
    const channel = channels[id];
    return hold(`<span class="mention">#${esc(channel?.name || "channel")}</span>`);
  });

  text = text.replace(URL_RE, (url) => {
    let clean = url;
    let tail = "";
    const match = clean.match(/[).,!?;:]+$/);
    if (match) {
      tail = match[0];
      clean = clean.slice(0, -tail.length);
    }
    return hold(
      `<a href="${clean}" target="_blank" rel="noopener noreferrer">${clean}</a>${tail}`
    );
  });

  text = text
    .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
    .replace(/~~([^~\n]+)~~/g, "<del>$1</del>")
    .replace(/__([^_\n]+)__/g, "<u>$1</u>")
    .replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s).,!?]|$)/g, "$1<em>$2</em>");

  text = text.replace(/\n/g, "<br>");
  return text.replace(/\u0000B(\d+)\u0000/g, (_, index) => blocks[Number(index)]);
}

// ------------------------------------------------------------- attachments
export function proxyUrl(url) {
  return `/api/media?url=${encodeURIComponent(url)}`;
}

// Discord's CDN is proxied so signed links keep working and the browser never
// hotlinks it; anything else (Tenor, Giphy, ...) is loaded directly.
export function mediaSrc(url) {
  try {
    const host = new URL(url).hostname.toLowerCase();
    if (host.endsWith("discordapp.com") || host.endsWith("discordapp.net")) {
      return proxyUrl(url);
    }
  } catch {
    /* a relative or broken url is passed through */
  }
  return url;
}

export function renderAttachments(attachments = []) {
  if (!attachments.length) return "";
  return `<div class="attachments">${attachments
    .map((file) => {
      const src = mediaSrc(file.url);
      const name = esc(file.filename);
      if (file.kind === "image") {
        return `<a class="att-image" href="${esc(file.url)}" target="_blank" rel="noopener noreferrer"><img src="${src}" alt="${name}" loading="lazy"${
          file.width && file.height ? ` width="${file.width}" height="${file.height}"` : ""
        }></a>`;
      }
      if (file.kind === "video") {
        return `<div class="att-video"><video controls preload="metadata" src="${src}"></video></div>`;
      }
      if (file.kind === "audio") {
        return `<div class="att-audio"><audio controls preload="metadata" src="${src}"></audio></div>`;
      }
      return `<a class="att-file" href="${esc(file.url)}" target="_blank" rel="noopener noreferrer">
        ${icon("file", 22)}
        <span class="meta"><span class="n">${name}</span><span class="s">${fmtBytes(file.size)}</span></span>
      </a>`;
    })
    .join("")}</div>`;
}

export function renderEmbeds(embeds = []) {
  return embeds
    .map((embed) => {
      const color = embed.color != null ? `#${Number(embed.color).toString(16).padStart(6, "0")}` : "";
      const title = embed.url
        ? `<a class="e-title" href="${esc(embed.url)}" target="_blank" rel="noopener noreferrer">${esc(embed.title || embed.url)}</a>`
        : embed.title
        ? `<span class="e-title">${esc(embed.title)}</span>`
        : "";
      const author = embed.author?.name
        ? `<div class="tiny muted">${esc(embed.author.name)}</div>`
        : "";
      const description = embed.description
        ? `<div class="e-desc">${renderContent(embed.description)}</div>`
        : "";
      const fields = (embed.fields || []).length
        ? `<div class="e-fields">${embed.fields
            .map(
              (field) =>
                `<div class="e-field"><div class="k">${esc(field.name)}</div><div class="v">${renderContent(
                  field.value
                )}</div></div>`
            )
            .join("")}</div>`
        : "";
      const image = embed.image?.url
        ? `<img src="${mediaSrc(embed.image.url)}" alt="" loading="lazy">`
        : "";
      const thumb = embed.thumbnail?.url
        ? `<img src="${mediaSrc(embed.thumbnail.url)}" alt="" loading="lazy" style="max-width:110px;float:right;margin-left:10px">`
        : "";
      const footer = [embed.footer?.text, embed.timestamp ? fmtDateTime(embed.timestamp) : ""]
        .filter(Boolean)
        .join(" · ");
      return `<div class="embed"${color ? ` style="border-left-color:${color}"` : ""}>
        ${author}${title}${thumb}${description}${fields}${image}
        ${footer ? `<div class="e-footer">${esc(footer)}</div>` : ""}
      </div>`;
    })
    .join("");
}

export function renderStickers(stickers = []) {
  return stickers
    .map(
      (sticker) =>
        `<div class="sticker"><img src="${esc(sticker.url)}" alt="${esc(sticker.name)}" title="${esc(
          sticker.name
        )}" loading="lazy"></div>`
    )
    .join("");
}

export function renderReactions(reactions = []) {
  if (!reactions.length) return "";
  return `<div class="reactions">${reactions
    .map((reaction) => {
      const face = reaction.id
        ? `<img src="https://cdn.discordapp.com/emojis/${reaction.id}.${
            reaction.animated ? "gif" : "png"
          }" alt="">`
        : `<span>${esc(reaction.name)}</span>`;
      return `<button class="reaction${reaction.me ? " me" : ""}" data-emoji="${esc(
        reaction.id ? `<${reaction.animated ? "a" : ""}:${reaction.name}:${reaction.id}>` : reaction.name
      )}" data-name="${esc(reaction.name)}">${face}<span>${reaction.count}</span></button>`;
    })
    .join("")}</div>`;
}

export function renderReply(reply) {
  if (!reply) return "";
  const who = reply.author?.display || "someone";
  const text = reply.deleted ? "original message deleted" : reply.content || (reply.attachment ? "attachment" : "");
  return `<div class="reply-preview"><span class="bar"></span><strong>${esc(
    who
  )}</strong><span>${esc(text.slice(0, 90))}</span></div>`;
}

// ------------------------------------------------------------------ modals
let modalEscape = null;
let modalBackdrop = null;

export function openModal({ title, body = "", actions = [], width = "" }) {
  const root = document.getElementById("modal");
  if (modalBackdrop) {
    root.removeEventListener("click", modalBackdrop);
    modalBackdrop = null;
  }
  root.innerHTML = "";
  root.classList.remove("hidden");

  const card = document.createElement("div");
  card.className = "modal-card";
  if (width) card.style.width = width;
  card.innerHTML = `
    <div class="modal-head"><h2>${esc(title)}</h2><div class="spacer"></div>
      <button class="btn icon ghost" data-close type="button" aria-label="Close">${icon("close", 16)}</button>
    </div>
    <div class="modal-body">${body}</div>
    <div class="modal-foot"></div>`;

  const foot = card.querySelector(".modal-foot");
  const close = () => {
    root.classList.add("hidden");
    root.innerHTML = "";
    if (modalEscape) {
      document.removeEventListener("keydown", modalEscape);
      modalEscape = null;
    }
    if (modalBackdrop) {
      root.removeEventListener("click", modalBackdrop);
      modalBackdrop = null;
    }
  };

  for (const action of actions) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `btn ${action.primary ? "primary" : ""} ${action.danger ? "danger" : ""}`;
    button.textContent = action.label;
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        const result = await action.onClick?.(close, card);
        if (result !== false && action.close !== false) close();
      } catch (error) {
        toast(error.message || "That did not work", "error");
      } finally {
        button.disabled = false;
      }
    });
    foot.appendChild(button);
  }

  card.querySelector("[data-close]").addEventListener("click", close);
  modalBackdrop = (event) => {
    if (event.target === root) close();
  };
  root.addEventListener("click", modalBackdrop);

  modalEscape = (event) => {
    if (event.key === "Escape") close();
  };
  document.addEventListener("keydown", modalEscape);

  root.appendChild(card);
  return { close, card, body: card.querySelector(".modal-body") };
}

export function confirmDialog(title, message, { confirmLabel = "Confirm", danger = false } = {}) {
  return new Promise((resolve) => {
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      resolve(value);
    };
    const modal = openModal({
      title,
      body: `<p>${esc(message)}</p>`,
      actions: [
        { label: "Cancel", onClick: () => finish(false) },
        {
          label: confirmLabel,
          primary: !danger,
          danger,
          onClick: () => finish(true),
        },
      ],
    });
    const observer = new MutationObserver(() => {
      if (document.getElementById("modal").classList.contains("hidden")) {
        finish(false);
        observer.disconnect();
      }
    });
    observer.observe(document.getElementById("modal"), { attributes: true });
    return modal;
  });
}

// ------------------------------------------------------------------ toasts
export function toast(message, kind = "ok", ttl = 3800) {
  const root = document.getElementById("toasts");
  const node = document.createElement("div");
  node.className = `toast ${kind}`;
  node.textContent = message;
  root.appendChild(node);
  setTimeout(() => {
    node.style.opacity = "0";
    node.style.transition = "opacity .2s ease";
    setTimeout(() => node.remove(), 220);
  }, ttl);
}

export function debounce(fn, delay = 250) {
  let handle = null;
  return (...args) => {
    clearTimeout(handle);
    handle = setTimeout(() => fn(...args), delay);
  };
}

export function statusRank(status) {
  return { online: 0, idle: 1, dnd: 2, offline: 3 }[status] ?? 3;
}
