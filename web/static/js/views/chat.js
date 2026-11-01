// Discord-style chat: channel list, message history, live updates, member list,
// composer with uploads and inline attachment rendering.

import { get, post, patch, del, upload } from "../api.js";
import {
  avatar,
  colorFor,
  dayKey,
  esc,
  fmtTime,
  icon,
  openModal,
  renderAttachments,
  renderContent,
  renderEmbeds,
  renderReactions,
  renderReply,
  renderStickers,
  statusRank,
  toast,
} from "../ui.js";
import { openMember } from "./memberModal.js";

const MAX_FILES = 10;
const MAX_UPLOAD = 48 * 1024 * 1024;
const GROUP_WINDOW_MS = 7 * 60 * 1000;
const QUICK_EMOJI = ["👍", "🔥", "😂", "💀", "❤️", "🎵", "👀", "🙏", "😭", "🫡"];
const SYSTEM_TYPES = new Set([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]);

function sameGroup(previous, current) {
  if (!previous) return false;
  if (previous.author.id !== current.author.id) return false;
  if (dayKey(previous.timestamp) !== dayKey(current.timestamp)) return false;
  const delta = new Date(current.timestamp) - new Date(previous.timestamp);
  return delta >= 0 && delta < GROUP_WINDOW_MS;
}

function systemText(message) {
  const who = message.author?.display || "someone";
  switch (message.type) {
    case 1:
      return `${who} added someone to the group.`;
    case 2:
      return `${who} removed someone from the group.`;
    case 3:
      return `${who} started a call.`;
    case 4:
    case 5:
      return `${who} changed the channel.`;
    case 6:
      return `${who} pinned a message.`;
    case 7:
      return `${who} joined the server.`;
    case 8:
      return `${who} boosted the server.`;
    default:
      return message.clean_content || `${who} did something.`;
  }
}

function messageNode(message, previous, chat) {
  if (message.system || SYSTEM_TYPES.has(message.type)) {
    return `<div class="system-line" data-id="${message.id}">${esc(systemText(message))} · ${fmtTime(
      message.timestamp
    )}</div>`;
  }

  const compact = sameGroup(previous, message);
  const handle = message.author.nickname || message.author.global_name || message.author.username;
  const canEdit = message.author.id === chat.botId;
  const canDelete = chat.permissions.manage || canEdit;

  const actions = `<div class="msg-actions">
    <button type="button" data-act="react" data-emoji="👍" title="React">👍</button>
    <button type="button" data-act="reply" title="Reply">${icon("reply", 15)}</button>
    ${canEdit ? `<button type="button" data-act="edit" title="Edit">${icon("edit", 15)}</button>` : ""}
    ${canDelete ? `<button type="button" data-act="delete" title="Delete">${icon("trash", 15)}</button>` : ""}
  </div>`;

  return `<div class="msg-group${compact ? " compact" : ""}" data-id="${message.id}" data-author="${
    message.author.id
  }">
    ${
      compact
        ? `<div class="msg-avatar spacer"></div>`
        : avatar(message.author, 40).replace('class="avatar', 'class="avatar msg-avatar')
    }
    <div class="msg-body">
      ${
        compact
          ? ""
          : `<div class="msg-head">
              <span class="msg-name" style="color:${colorFor(message.author)}" data-member="${
              message.author.id
            }">${esc(message.author.display)}</span>
              <span class="msg-handle">${handle && handle !== message.author.display ? `@${esc(handle)}` : ""}</span>
              ${message.author.bot ? `<span class="tag bot">bot</span>` : ""}
              <span class="msg-time" title="${esc(message.timestamp)}">${fmtTime(message.timestamp)}</span>
              ${message.pinned ? `<span class="tag">pinned</span>` : ""}
            </div>`
      }
      ${renderReply(message.reply)}
      ${
        message.content
          ? `<div class="msg-text">${renderContent(message.content, message)}</div>`
          : ""
      }
      ${renderAttachments(message.attachments)}
      ${renderEmbeds(message.embeds)}
      ${renderStickers(message.stickers)}
      ${renderReactions(message.reactions)}
      ${message.edited ? `<span class="msg-edited">(edited ${fmtTime(message.edited)})</span>` : ""}
    </div>
    ${actions}
  </div>`;
}

function messagesHtml(messages, chat) {
  let html = "";
  let previous = null;
  let lastDay = null;
  for (const message of messages) {
    const day = dayKey(message.timestamp);
    if (day && day !== lastDay) {
      html += `<div class="day-divider">${esc(day)}</div>`;
      lastDay = day;
      previous = null;
    }
    html += messageNode(message, previous, chat);
    previous = message;
  }
  return html;
}

function channelListHtml(chat) {
  const groups = new Map();
  for (const channel of chat.channels) {
    const key = channel.category || "";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(channel);
  }
  const keys = [...groups.keys()].sort((a, b) => (a === "" ? -1 : b === "" ? 1 : a.localeCompare(b)));
  return keys
    .map((key) => {
      const channels = groups.get(key).sort((a, b) => a.position - b.position);
      return `${key ? `<div class="cat">${esc(key)}</div>` : ""}${channels
        .map(
          (channel) => `<button type="button" class="channel-item${
            channel.id === chat.channelId ? " active" : ""
          }" data-channel="${channel.id}">
            <span class="hash">#</span><span class="cname">${esc(channel.name)}</span>
          </button>`
        )
        .join("")}`;
    })
    .join("");
}

export async function mount(container, ctx) {
  const guildId = ctx.store.guildId;
  let guild = ctx.store.guild;
  if (!guild) {
    try {
      guild = await ctx.loadGuild(guildId);
    } catch (error) {
      ctx.setTopbar({ title: "Chat" });
      container.innerHTML = `<div class="empty">${esc(error.message)}</div>`;
      return;
    }
  }

  const channels = (guild.chat_channels || []).filter(
    (channel) => channel.permissions?.read !== false
  );
  const first =
    channels.find((channel) => channel.permissions?.send && channel.type === "text") ||
    channels.find((channel) => channel.permissions?.send) ||
    channels[0];

  let channelId = ctx.route.channelId || first?.id;
  if (!channelId) {
    ctx.setTopbar({ title: guild.name, sub: "Chat" });
    container.innerHTML = `<div class="empty">No text channels the bot can read in this server.</div>`;
    return;
  }

  const chat = {
    guildId,
    guild,
    channels,
    channelId: String(channelId),
    channel: null,
    permissions: {},
    messages: [],
    oldest: null,
    hasMore: true,
    loading: false,
    sending: false,
    reply: null,
    files: [],
    typing: new Map(),
    members: [],
    membersTotal: 0,
    showMembers: window.innerWidth > 1120,
    botId: ctx.store.overview?.bot?.id,
  };

  container.innerHTML = `
    <div class="chat">
      <aside class="chat-channels" id="channel-list"></aside>
      <section class="chat-main" id="chat-main">
        <header class="chat-header">
          <button class="btn icon ghost mobile-only" id="channel-toggle" type="button" title="Channels">${icon(
            "hash"
          )}</button>
          <div style="min-width:0">
            <h2 id="chat-title"><span class="hash">#</span> loading…</h2>
            <div class="topic" id="chat-topic"></div>
          </div>
          <div class="spacer"></div>
          <button class="btn icon ghost" id="toggle-members" type="button" title="Members">${icon(
            "members"
          )}</button>
          <button class="btn icon ghost" id="reload-chat" type="button" title="Reload">${icon(
            "refresh"
          )}</button>
        </header>
        <div class="chat-scroll" id="chat-scroll">
          <div class="loading-row">Loading messages…</div>
        </div>
        <div class="typing-bar" id="typing-bar"></div>
        <form class="composer" id="composer">
          <div id="reply-bar"></div>
          <div class="composer-box" id="composer-box">
            <button class="btn icon ghost" type="button" id="attach-btn" title="Attach files">${icon(
              "attach"
            )}</button>
            <button class="btn icon ghost" type="button" id="emoji-btn" title="Emoji">🙂</button>
            <textarea id="chat-input" rows="1" placeholder="Message #channel" autocomplete="off"></textarea>
            <button class="btn primary icon" type="submit" id="send-btn" title="Send">${icon(
              "send"
            )}</button>
          </div>
          <div class="composer-files hidden" id="file-preview"></div>
          <div id="emoji-panel" class="hidden" style="display:flex;gap:4px;flex-wrap:wrap;margin-top:8px"></div>
          <input type="file" id="file-input" multiple hidden>
        </form>
      </section>
      <aside class="chat-members hidden" id="chat-members"></aside>
    </div>`;

  const root = container;
  const scroll = root.querySelector("#chat-scroll");
  const input = root.querySelector("#chat-input");
  const fileInput = root.querySelector("#file-input");
  const preview = root.querySelector("#file-preview");
  const emojiPanel = root.querySelector("#emoji-panel");
  const replyBar = root.querySelector("#reply-bar");
  const membersPane = root.querySelector("#chat-members");

  // ------------------------------------------------------------- channels
  const refreshChannelList = () => {
    root.querySelector("#channel-list").innerHTML = channelListHtml(chat);
    root.querySelectorAll("#channel-list .channel-item").forEach((button) => {
      button.addEventListener("click", () => switchChannel(button.dataset.channel));
    });
  };

  const updateTopbar = () => {
    ctx.setTopbar({
      title: chat.channel ? `#${chat.channel.name}` : guild.name,
      sub: `${guild.name} · live chat`,
      actions: `<a class="btn small ghost" href="https://discord.com/channels/${guild.id}/${chat.channelId}" target="_blank" rel="noopener noreferrer">Open in Discord</a>`,
    });
    root.querySelector("#chat-title").innerHTML = `<span class="hash">#</span> ${esc(
      chat.channel?.name || "channel"
    )}`;
    root.querySelector("#chat-topic").textContent = chat.channel?.topic || "";
    input.placeholder = chat.permissions.send
      ? `Message #${chat.channel?.name || ""}`
      : "You do not have permission to send here";
    root.querySelector("#send-btn").disabled = !chat.permissions.send;
    input.disabled = !chat.permissions.send;
    root.querySelector("#attach-btn").disabled = !chat.permissions.send;
  };

  const isNearBottom = () => scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 140;

  const setDayDividerIfNeeded = (message) => {
    const last = chat.messages[chat.messages.length - 1];
    if (!last || dayKey(last.timestamp) !== dayKey(message.timestamp)) {
      return `<div class="day-divider">${esc(dayKey(message.timestamp))}</div>`;
    }
    return "";
  };

  const appendMessage = (message, { forceScroll = false } = {}) => {
    if (chat.messages.some((existing) => existing.id === message.id)) return;
    const atBottom = isNearBottom();
    const previous = chat.messages[chat.messages.length - 1] || null;
    const html = setDayDividerIfNeeded(message) + messageNode(message, previous, chat);
    const holder = scroll.querySelector("#messages");
    if (holder) {
      holder.insertAdjacentHTML("beforeend", html);
    } else {
      scroll.innerHTML = `<div id="messages">${html}</div><div class="typing-bar" id="typing-bar"></div>`;
    }
    chat.messages.push(message);
    if (atBottom || forceScroll) {
      requestAnimationFrame(() => {
        scroll.scrollTop = scroll.scrollHeight;
      });
    }
  };

  const replaceMessageNode = (message) => {
    const node = scroll.querySelector(`.msg-group[data-id="${message.id}"]`);
    if (!node) return;
    const index = chat.messages.findIndex((entry) => entry.id === message.id);
    const previous = index > 0 ? chat.messages[index - 1] : null;
    const template = document.createElement("template");
    template.innerHTML = messageNode(message, previous, chat).trim();
    node.replaceWith(template.content.firstElementChild);
  };

  const renderMessages = () => {
    scroll.innerHTML = `<div id="messages">${messagesHtml(chat.messages, chat)}</div>`;
    scroll.scrollTop = scroll.scrollHeight;
  };

  const loadMessages = async () => {
    scroll.innerHTML = `<div class="loading-row">Loading messages…</div>`;
    try {
      const data = await get(`/api/channels/${chat.channelId}/messages?limit=50`);
      chat.channel = data.channel;
      chat.permissions = data.permissions || {};
      chat.messages = data.messages;
      chat.hasMore = data.has_more;
      chat.oldest = data.messages[0]?.id || null;
      renderMessages();
      updateTopbar();
    } catch (error) {
      scroll.innerHTML = `<div class="empty">${esc(error.message)}</div>`;
    }
  };

  const loadOlder = async () => {
    if (chat.loading || !chat.hasMore || !chat.oldest) return;
    chat.loading = true;
    const previousHeight = scroll.scrollHeight;
    try {
      const data = await get(
        `/api/channels/${chat.channelId}/messages?limit=50&before=${chat.oldest}`
      );
      if (!data.messages.length) {
        chat.hasMore = false;
        return;
      }
      const holder = scroll.querySelector("#messages");
      holder.insertAdjacentHTML("afterbegin", messagesHtml(data.messages, chat));
      chat.messages = [...data.messages, ...chat.messages];
      chat.oldest = data.messages[0].id;
      chat.hasMore = data.has_more;
      scroll.scrollTop += scroll.scrollHeight - previousHeight;
    } catch (error) {
      toast(error.message, "error");
    } finally {
      chat.loading = false;
    }
  };

  const renderMembers = () => {
    const pane = root.querySelector("#chat-members");
    if (!pane) return;
    const sorted = [...chat.members].sort((a, b) => {
      const rank = statusRank(a.status) - statusRank(b.status);
      return rank || a.display.localeCompare(b.display);
    });
    const online = sorted.filter((member) => member.status !== "offline");
    const offline = sorted.filter((member) => member.status === "offline");
    const row = (member) => `<div class="row" data-member="${member.id}">
        <span class="status-bulb ${member.status}"></span>
        ${avatar(member, 28)}
        <span class="nm" style="color:${colorFor(member)}">${esc(member.display)}</span>
        ${member.bot ? `<span class="tag bot">bot</span>` : ""}
      </div>`;
    pane.innerHTML = `
      <h3>Online — ${online.length}</h3>
      ${online.map(row).join("") || `<p class="muted tiny" style="padding:0 8px">Nobody online.</p>`}
      <h3>Offline — ${offline.length}</h3>
      ${offline.map(row).join("")}`;
    pane.querySelectorAll("[data-member]").forEach((element) => {
      element.addEventListener("click", () =>
        openMember({ guildId, userId: element.dataset.member, guild: chat.guild, onChange: () => {} })
      );
    });
  };

  const loadMembers = async () => {
    try {
      const data = await get(`/api/guilds/${guildId}/members?limit=200`);
      chat.members = data.members;
      chat.membersTotal = data.total;
      renderMembers();
    } catch {
      /* the member pane is optional */
    }
  };

  const switchChannel = async (nextId) => {
    if (String(nextId) === chat.channelId) return;
    chat.channelId = String(nextId);
    chat.messages = [];
    chat.oldest = null;
    chat.hasMore = true;
    chat.reply = null;
    chat.typing.clear();
    renderReplyBar();
    root.querySelector("#channel-list").classList.remove("open");
    history.replaceState(null, "", `#/g/${guildId}/chat/${chat.channelId}`);
    refreshChannelList();
    updateTopbar();
    await loadMessages();
  };

  // -------------------------------------------------------------- composer
  const renderReplyBar = () => {
    replyBar.innerHTML = chat.reply
      ? `<div class="reply-bar"><span class="bar"></span>Replying to <strong>${esc(
          chat.reply.author?.display || "someone"
        )}</strong><span class="muted">${esc((chat.reply.content || "").slice(0, 70))}</span>
          <div class="spacer"></div>
          <button class="btn icon ghost small" type="button" id="cancel-reply">${icon("close", 14)}</button>
        </div>`
      : "";
    replyBar.querySelector("#cancel-reply")?.addEventListener("click", () => {
      chat.reply = null;
      renderReplyBar();
    });
  };

  const renderFilePreview = () => {
    preview.classList.toggle("hidden", !chat.files.length);
    preview.innerHTML = chat.files
      .map(
        (file, index) => `<div class="file-chip">
          ${
            file.type?.startsWith("image/")
              ? `<img src="${URL.createObjectURL(file)}" alt="">`
              : icon("file", 22)
          }
          <span>${esc(file.name)}<br><span class="muted tiny">${Math.round(file.size / 1024)} KB</span></span>
          <button type="button" data-remove="${index}" title="Remove">×</button>
        </div>`
      )
      .join("");
    preview.querySelectorAll("[data-remove]").forEach((button) => {
      button.addEventListener("click", () => {
        chat.files.splice(Number(button.dataset.remove), 1);
        renderFilePreview();
      });
    });
  };

  const addFiles = (list) => {
    const incoming = [...list];
    for (const file of incoming) {
      if (chat.files.length >= MAX_FILES) {
        toast(`A message can carry at most ${MAX_FILES} files`, "warn");
        break;
      }
      const total = chat.files.reduce((sum, item) => sum + item.size, 0) + file.size;
      if (total > MAX_UPLOAD) {
        toast("Those files are too big together (48 MB max)", "warn");
        break;
      }
      chat.files.push(file);
    }
    renderFilePreview();
  };

  const clearComposer = () => {
    input.value = "";
    input.style.height = "auto";
    chat.files = [];
    chat.reply = null;
    renderFilePreview();
    renderReplyBar();
  };

  const send = async () => {
    if (chat.sending || !chat.permissions.send) return;
    const content = input.value.trim();
    if (!content && !chat.files.length) return;

    chat.sending = true;
    const button = root.querySelector("#send-btn");
    button.disabled = true;
    const form = new FormData();
    if (content) form.append("content", content);
    if (chat.reply) form.append("reply_to", chat.reply.id);
    for (const file of chat.files) form.append("files", file, file.name);

    try {
      const response = await upload(`/api/channels/${chat.channelId}/messages`, form);
      clearComposer();
      appendMessage(response.message, { forceScroll: true });
      // The live event may have beaten this response to the screen, in which
      // case appendMessage deduplicated it - still make sure it is visible.
      requestAnimationFrame(() => {
        scroll.scrollTop = scroll.scrollHeight;
      });
    } catch (error) {
      toast(error.message, "error");
    } finally {
      chat.sending = false;
      button.disabled = false;
      input.focus();
    }
  };

  root.querySelector("#composer").addEventListener("submit", (event) => {
    event.preventDefault();
    send();
  });

  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      send();
    }
  });

  input.addEventListener("input", () => {
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 180)}px`;
  });

  input.addEventListener("paste", (event) => {
    const files = [...(event.clipboardData?.files || [])];
    if (files.length) {
      event.preventDefault();
      addFiles(files);
    }
  });

  root.querySelector("#attach-btn").addEventListener("click", () => fileInput.click());
  fileInput.addEventListener("change", () => {
    addFiles(fileInput.files);
    fileInput.value = "";
  });

  emojiPanel.innerHTML = QUICK_EMOJI.map(
    (emoji) => `<button type="button" class="btn icon ghost" data-emoji="${emoji}">${emoji}</button>`
  ).join("");
  root.querySelector("#emoji-btn").addEventListener("click", () => {
    emojiPanel.classList.toggle("hidden");
  });
  emojiPanel.querySelectorAll("[data-emoji]").forEach((button) => {
    button.addEventListener("click", () => {
      input.value += button.dataset.emoji;
      input.focus();
    });
  });

  root.querySelector("#composer-box").addEventListener("dragover", (event) => {
    event.preventDefault();
    root.querySelector("#composer-box").classList.add("dragging");
  });
  root.querySelector("#composer-box").addEventListener("dragleave", () => {
    root.querySelector("#composer-box").classList.remove("dragging");
  });
  root.querySelector("#composer-box").addEventListener("drop", (event) => {
    event.preventDefault();
    root.querySelector("#composer-box").classList.remove("dragging");
    if (event.dataTransfer?.files?.length) addFiles(event.dataTransfer.files);
  });

  // --------------------------------------------------------------- actions
  scroll.addEventListener("scroll", () => {
    if (scroll.scrollTop < 80) loadOlder();
  });

  scroll.addEventListener("click", async (event) => {
    const reaction = event.target.closest(".reaction");
    if (reaction) {
      const group = reaction.closest(".msg-group");
      const message = chat.messages.find((entry) => entry.id === group?.dataset.id);
      const current = message?.reactions?.find(
        (entry) => entry.name === (reaction.dataset.name || reaction.dataset.emoji)
      );
      try {
        const data = await post(`/api/channels/${chat.channelId}/messages/${group.dataset.id}/react`, {
          emoji: reaction.dataset.emoji,
          remove: Boolean(current?.me),
        });
        message.reactions = data.reactions;
        replaceMessageNode(message);
      } catch (error) {
        toast(error.message, "error");
      }
      return;
    }

    const name = event.target.closest(".msg-name");
    if (name) {
      openMember({ guildId, userId: name.dataset.member, guild: chat.guild, onChange: () => {} });
      return;
    }

    const action = event.target.closest("[data-act]");
    if (!action) return;
    const group = action.closest(".msg-group");
    const message = chat.messages.find((entry) => entry.id === group?.dataset.id);
    if (!message) return;

    if (action.dataset.act === "reply") {
      chat.reply = message;
      renderReplyBar();
      input.focus();
    } else if (action.dataset.act === "react") {
      try {
        const data = await post(`/api/channels/${chat.channelId}/messages/${message.id}/react`, {
          emoji: action.dataset.emoji,
        });
        message.reactions = data.reactions;
        replaceMessageNode(message);
      } catch (error) {
        toast(error.message, "error");
      }
    } else if (action.dataset.act === "delete") {
      try {
        await del(`/api/channels/${chat.channelId}/messages/${message.id}`);
        group.remove();
        chat.messages = chat.messages.filter((entry) => entry.id !== message.id);
      } catch (error) {
        toast(error.message, "error");
      }
    } else if (action.dataset.act === "edit") {
      const modal = openModal({
        title: "Edit message",
        body: `<label class="field"><span>Content</span><textarea id="edit-content">${esc(
          message.content
        )}</textarea></label>`,
        actions: [
          { label: "Cancel", onClick: () => {} },
          {
            label: "Save",
            primary: true,
            onClick: async (close) => {
              const content = modal.body.querySelector("#edit-content").value.trim();
              if (!content) return false;
              const data = await patch(
                `/api/channels/${chat.channelId}/messages/${message.id}`,
                { content }
              );
              const index = chat.messages.findIndex((entry) => entry.id === message.id);
              chat.messages[index] = data.message;
              replaceMessageNode(data.message);
              toast("Message edited");
              close();
            },
          },
        ],
      });
    }
  });

  root.querySelector("#toggle-members").addEventListener("click", () => {
    chat.showMembers = !chat.showMembers;
    membersPane.classList.toggle("hidden", !chat.showMembers);
    if (chat.showMembers && !chat.members.length) loadMembers();
  });

  root.querySelector("#channel-toggle").addEventListener("click", () => {
    root.querySelector("#channel-list").classList.toggle("open");
  });

  root.querySelector("#reload-chat").addEventListener("click", () => loadMessages());

  // ------------------------------------------------------------------ live
  const typingBar = root.querySelector("#typing-bar");
  const renderTyping = () => {
    const now = Date.now();
    for (const [userId, entry] of chat.typing) {
      if (entry.until < now) chat.typing.delete(userId);
    }
    const names = [...chat.typing.values()].map((entry) => entry.name);
    typingBar.textContent = names.length
      ? `${names.slice(0, 3).join(", ")}${names.length > 3 ? " and others" : ""} ${
          names.length === 1 ? "is" : "are"
        } typing…`
      : "";
  };
  const typingTimer = setInterval(renderTyping, 1000);

  const offLive = ctx.onLive((event) => {
    if (event.guild_id !== guildId) return;

    if (event.type === "channels_changed") {
      ctx.loadGuild(guildId, { force: true }).then((fresh) => {
        chat.guild = fresh;
        chat.channels = (fresh.chat_channels || []).filter(
          (channel) => channel.permissions?.read !== false
        );
        refreshChannelList();
      });
      return;
    }

    if (event.type === "member_join" || event.type === "member_update") {
      const index = chat.members.findIndex((member) => member.id === event.member.id);
      if (index >= 0) chat.members[index] = event.member;
      else chat.members.push(event.member);
      if (chat.showMembers) renderMembers();
      return;
    }

    if (event.type === "member_remove") {
      chat.members = chat.members.filter((member) => member.id !== event.member_id);
      if (chat.showMembers) renderMembers();
      return;
    }

    if (event.channel_id !== chat.channelId) return;

    if (event.type === "message") {
      appendMessage(event.message);
      if (chat.typing) chat.typing.delete(event.message.author.id);
      return;
    }

    if (event.type === "message_edit") {
      const index = chat.messages.findIndex((message) => message.id === event.message.id);
      if (index >= 0) {
        chat.messages[index] = event.message;
        replaceMessageNode(event.message);
      } else {
        appendMessage(event.message);
      }
      return;
    }

    if (event.type === "message_delete") {
      chat.messages = chat.messages.filter((message) => message.id !== event.message_id);
      scroll.querySelector(`.msg-group[data-id="${event.message_id}"]`)?.remove();
      return;
    }

    if (event.type === "messages_bulk_delete") {
      const ids = new Set(event.message_ids);
      chat.messages = chat.messages.filter((message) => !ids.has(message.id));
      for (const id of ids) {
        scroll.querySelector(`.msg-group[data-id="${id}"]`)?.remove();
      }
      return;
    }

    if (event.type === "typing" && event.user?.id !== chat.botId) {
      chat.typing.set(event.user.id, { name: event.user.display, until: Date.now() + 7000 });
      renderTyping();
    }
  });

  refreshChannelList();
  updateTopbar();
  await loadMessages();
  if (chat.showMembers) {
    membersPane.classList.remove("hidden");
    loadMembers();
  }

  return () => {
    offLive();
    clearInterval(typingTimer);
  };
}
