/**
 * SwiftPlay WatchParty Relay — Cloudflare Worker + Durable Object
 * Zero dependencies, Hibernatable WebSockets, free-tier compatible.
 */

const VALID_CODE_RE = /^[A-Z0-9_-]{4,24}$/;
const VALID_DECKS = new Set(["video", "music"]);

function jsonResponse(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "no-store",
    },
  });
}

function normalizeCode(raw) {
  return String(raw || "").trim().toUpperCase();
}

function sanitizeDeckState(raw, deck) {
  if (!raw || typeof raw !== "object") {
    return null;
  }
  const kindRaw = String(raw.kind || (deck === "music" ? "music" : "video"))
    .trim()
    .toLowerCase();
  const kind = deck === "music" ? "music" : kindRaw === "twitch" ? "twitch" : "video";
  const videoId = String(raw.video_id || raw.id || "").trim().slice(0, 128);
  const title = String(raw.title || "").trim().slice(0, 180);
  if (!videoId && !title) {
    return null;
  }
  const posNum = Number(raw.pos ?? 0);
  const durNum = Number(raw.duration_sec ?? 0);
  const speedNum = Number(raw.speed ?? 1.0);
  const tsNum = Number(raw.updated_at ?? Date.now() / 1000);
  return {
    video_id: videoId,
    title,
    author: String(raw.author || "").trim().slice(0, 120),
    thumb_url: String(raw.thumb_url || "").trim().slice(0, 512),
    kind,
    is_live: Boolean(raw.is_live) || kind === "twitch",
    paused: Boolean(raw.paused),
    pos: Number.isFinite(posNum) && posNum >= 0 ? Math.round(posNum * 100) / 100 : 0,
    duration_sec: Number.isFinite(durNum) && durNum >= 0 ? Math.round(durNum) : 0,
    speed: Number.isFinite(speedNum) && speedNum >= 0.5 && speedNum <= 3.0 ? speedNum : 1.0,
    updated_at: Number.isFinite(tsNum) && tsNum > 0 ? tsNum : Date.now() / 1000,
    game_name: String(raw.game_name || "").trim().slice(0, 120),
    viewer_count: Math.max(0, Math.round(Number(raw.viewer_count || 0) || 0)),
    duration: String(raw.duration || "").trim().slice(0, 24),
  };
}

export class WatchPartyHub {
  constructor(ctx, env) {
    this.ctx = ctx;
    this.env = env;
    this._socketsByCode = new Map();
    this._rooms = new Map(); // roomKey (`${hostCode}:${deck}`) -> Set<code>
    this._roomRev = new Map(); // roomKey -> number
    this._hydrated = false;
  }

  _ensureHydrated() {
    if (this._hydrated) return;
    this._hydrated = true;
    for (const ws of this.ctx.getWebSockets()) {
      const att = this._getAttachment(ws);
      if (!att || !att.code) continue;
      this._socketsByCode.set(att.code, ws);
      for (const deck of ["video", "music"]) {
        const hostCode = att.joined?.[deck];
        if (hostCode) {
          this._addMemberToRoom(hostCode, deck, att.code);
        }
      }
    }
  }

  _roomKey(hostCode, deck) {
    return `${hostCode}:${deck}`;
  }

  _addMemberToRoom(hostCode, deck, memberCode) {
    const key = this._roomKey(hostCode, deck);
    let set = this._rooms.get(key);
    if (!set) {
      set = new Set();
      this._rooms.set(key, set);
    }
    set.add(memberCode);
  }

  _removeMemberFromRoom(hostCode, deck, memberCode) {
    const key = this._roomKey(hostCode, deck);
    const set = this._rooms.get(key);
    if (!set) return;
    set.delete(memberCode);
    if (set.size === 0) {
      this._rooms.delete(key);
    }
  }

  _getAttachment(ws) {
    try {
      return ws.deserializeAttachment() || null;
    } catch {
      return null;
    }
  }

  _setAttachment(ws, data) {
    try {
      ws.serializeAttachment(data);
    } catch {
      // Ignore if closing
    }
  }

  _send(ws, payload) {
    try {
      ws.send(JSON.stringify(payload));
      return true;
    } catch {
      return false;
    }
  }

  async _loadProfile(code) {
    return (await this.ctx.storage.get(`profile:${code}`)) || null;
  }

  async _saveProfile(code, profile) {
    await this.ctx.storage.put(`profile:${code}`, profile);
  }

  _roomMembersSummary(hostCode, deck) {
    const key = this._roomKey(hostCode, deck);
    const set = this._rooms.get(key);
    if (!set || set.size === 0) return [];
    const out = [];
    for (const mCode of set) {
      const ws = this._socketsByCode.get(mCode);
      const att = ws ? this._getAttachment(ws) : null;
      if (!att) continue;
      out.push({
        code: att.code,
        name: att.name || att.code,
        avatar_url: att.avatar_url || "",
        is_host: att.code === hostCode,
      });
    }
    return out;
  }

  _publicFriendPayload(code, storedProfile) {
    const ws = this._socketsByCode.get(code);
    const att = ws ? this._getAttachment(ws) : null;
    if (att) {
      const shareOn = att.share_enabled !== false;
      const vidMembers = this._roomMembersSummary(code, "video");
      const musMembers = this._roomMembersSummary(code, "music");
      return {
        code,
        name: att.name || storedProfile?.name || code,
        avatar_url: att.avatar_url || storedProfile?.avatar_url || "",
        status: att.status === "idle" ? "idle" : "active",
        share_enabled: shareOn,
        allow_guest_control: att.allow_guest_control !== false,
        video: shareOn ? att.video || null : null,
        music: shareOn ? att.music || null : null,
        video_guests: Math.max(0, vidMembers.length - (vidMembers.some((m) => m.code === code) ? 1 : 0)),
        music_guests: Math.max(0, musMembers.length - (musMembers.some((m) => m.code === code) ? 1 : 0)),
        last_seen: Date.now() / 1000,
      };
    }
    return {
      code,
      name: storedProfile?.name || code,
      avatar_url: storedProfile?.avatar_url || "",
      status: "offline",
      share_enabled: false,
      allow_guest_control: true,
      video: null,
      music: null,
      video_guests: 0,
      music_guests: 0,
      last_seen: storedProfile?.last_seen || 0,
    };
  }

  async _buildFriendsSnapshot(friendCodes) {
    const out = [];
    const seen = new Set();
    for (const raw of friendCodes || []) {
      const fc = normalizeCode(raw);
      if (!fc || !VALID_CODE_RE.test(fc) || seen.has(fc)) continue;
      seen.add(fc);
      const ws = this._socketsByCode.get(fc);
      const stored = ws ? null : await this._loadProfile(fc);
      out.push(this._publicFriendPayload(fc, stored));
    }
    return out;
  }

  async _broadcastFriendState(targetCode) {
    const stored = this._socketsByCode.has(targetCode)
      ? null
      : await this._loadProfile(targetCode);
    const payload = {
      type: "friend_state",
      friend: this._publicFriendPayload(targetCode, stored),
    };
    for (const [subCode, ws] of this._socketsByCode.entries()) {
      if (subCode === targetCode) continue;
      const att = this._getAttachment(ws);
      if (!att || !Array.isArray(att.friends)) continue;
      if (att.friends.includes(targetCode)) {
        this._send(ws, payload);
      }
    }
  }

  _bumpRev(hostCode, deck) {
    const key = this._roomKey(hostCode, deck);
    const next = (this._roomRev.get(key) || 0) + 1;
    this._roomRev.set(key, next);
    return next;
  }

  _broadcastRoomMembers(hostCode, deck) {
    const key = this._roomKey(hostCode, deck);
    const set = this._rooms.get(key);
    if (!set || set.size === 0) return;
    const hostWs = this._socketsByCode.get(hostCode);
    const hostAtt = hostWs ? this._getAttachment(hostWs) : null;
    const allowGuest = hostAtt ? hostAtt.allow_guest_control !== false : true;
    const members = this._roomMembersSummary(hostCode, deck);
    const msg = {
      type: "party_members",
      host_code: hostCode,
      deck,
      members,
      allow_guest_control: allowGuest,
    };
    for (const mCode of set) {
      const mWs = this._socketsByCode.get(mCode);
      if (mWs) this._send(mWs, msg);
    }
  }

  _broadcastPartyState(hostCode, deck, reason, action, updatedBy, updatedByName, excludeCode = null) {
    const key = this._roomKey(hostCode, deck);
    const set = this._rooms.get(key);
    if (!set || set.size === 0) return;
    const hostWs = this._socketsByCode.get(hostCode);
    const hostAtt = hostWs ? this._getAttachment(hostWs) : null;
    const state = hostAtt ? (deck === "music" ? hostAtt.music : hostAtt.video) : null;
    const allowGuest = hostAtt ? hostAtt.allow_guest_control !== false : true;
    const rev = this._roomRev.get(key) || 1;
    const msg = {
      type: "party_state",
      host_code: hostCode,
      host_name: hostAtt?.name || hostCode,
      deck,
      reason,
      action: action || "",
      rev,
      state: state || null,
      updated_by: updatedBy || hostCode,
      updated_by_name: updatedByName || updatedBy || hostCode,
      allow_guest_control: allowGuest,
      members: this._roomMembersSummary(hostCode, deck),
    };
    for (const mCode of set) {
      if (excludeCode && mCode === excludeCode) continue;
      const mWs = this._socketsByCode.get(mCode);
      if (mWs) this._send(mWs, msg);
    }
  }

  _leaveDeckRoom(ws, att, deck) {
    const prevHost = att.joined?.[deck];
    if (!prevHost) return;
    att.joined[deck] = null;
    this._removeMemberFromRoom(prevHost, deck, att.code);
    this._setAttachment(ws, att);
    this._broadcastRoomMembers(prevHost, deck);
  }

  async fetch(request) {
    this._ensureHydrated();
    const url = new URL(request.url);
    if (request.headers.get("Upgrade")?.toLowerCase() !== "websocket") {
      return jsonResponse({
        ok: true,
        service: "swiftplay-watchparty",
        online: this._socketsByCode.size,
      });
    }

    const code = normalizeCode(url.searchParams.get("code"));
    const secret = String(url.searchParams.get("secret") || "").trim();
    if (!VALID_CODE_RE.test(code) || secret.length < 16) {
      return new Response("Invalid WatchParty credentials", { status: 400 });
    }

    const stored = await this._loadProfile(code);
    if (stored && stored.secret && stored.secret !== secret) {
      return new Response("WatchParty code ownership mismatch", { status: 403 });
    }

    const oldWs = this._socketsByCode.get(code);
    if (oldWs) {
      try {
        oldWs.close(4001, "Replaced by newer connection");
      } catch {
        // Ignore
      }
      this._cleanupSocket(oldWs, { notifyOffline: false });
    }

    const pair = new WebSocketPair();
    const [client, server] = Object.values(pair);

    const initialAtt = {
      code,
      secret,
      name: stored?.name || code,
      avatar_url: stored?.avatar_url || "",
      friends: [],
      status: "idle",
      share_enabled: true,
      allow_guest_control: true,
      video: null,
      music: null,
      joined: { video: null, music: null },
    };

    this.ctx.acceptWebSocket(server);
    this._setAttachment(server, initialAtt);
    this._socketsByCode.set(code, server);
    if (!stored) {
      await this._saveProfile(code, {
        code,
        secret,
        name: code,
        avatar_url: "",
        last_seen: Date.now() / 1000,
      });
    }

    return new Response(null, { status: 101, webSocket: client });
  }

  async webSocketMessage(ws, rawMessage) {
    this._ensureHydrated();
    const att = this._getAttachment(ws);
    if (!att || !att.code) return;

    let msg;
    try {
      const txt = typeof rawMessage === "string" ? rawMessage : new TextDecoder().decode(rawMessage);
      msg = JSON.parse(txt);
    } catch {
      return;
    }
    if (!msg || typeof msg !== "object") return;
    const type = String(msg.type || "").trim();

    if (type === "ping") {
      this._send(ws, { type: "pong", ts: Date.now() / 1000 });
      return;
    }

    if (type === "hello") {
      const name = String(msg.name || att.name || att.code).trim().slice(0, 64) || att.code;
      const avatar = String(msg.avatar_url || "").trim().slice(0, 512);
      const friends = Array.isArray(msg.friends)
        ? [...new Set(msg.friends.map(normalizeCode).filter((c) => VALID_CODE_RE.test(c) && c !== att.code))].slice(0, 100)
        : att.friends;
      att.name = name;
      att.avatar_url = avatar;
      att.friends = friends;
      att.status = msg.status === "idle" ? "idle" : "active";
      att.share_enabled = msg.share_enabled !== false;
      att.allow_guest_control = msg.allow_guest_control !== false;
      att.video = sanitizeDeckState(msg.video, "video");
      att.music = sanitizeDeckState(msg.music, "music");
      this._setAttachment(ws, att);

      await this._saveProfile(att.code, {
        code: att.code,
        secret: att.secret,
        name: att.name,
        avatar_url: att.avatar_url,
        last_seen: Date.now() / 1000,
      });

      this._send(ws, { type: "welcome", code: att.code, name: att.name });
      this._send(ws, {
        type: "friends_snapshot",
        friends: await this._buildFriendsSnapshot(att.friends),
      });
      await this._broadcastFriendState(att.code);
      return;
    }

    if (type === "set_friends") {
      const friends = Array.isArray(msg.friends)
        ? [...new Set(msg.friends.map(normalizeCode).filter((c) => VALID_CODE_RE.test(c) && c !== att.code))].slice(0, 100)
        : [];
      att.friends = friends;
      this._setAttachment(ws, att);
      this._send(ws, {
        type: "friends_snapshot",
        friends: await this._buildFriendsSnapshot(att.friends),
      });
      return;
    }

    if (type === "lookup") {
      const target = normalizeCode(msg.code);
      if (!VALID_CODE_RE.test(target) || target === att.code) {
        this._send(ws, { type: "lookup_result", code: target, found: false });
        return;
      }
      const onlineWs = this._socketsByCode.get(target);
      const onlineAtt = onlineWs ? this._getAttachment(onlineWs) : null;
      const stored = onlineAtt ? null : await this._loadProfile(target);
      if (onlineAtt) {
        this._send(ws, {
          type: "lookup_result",
          code: target,
          found: true,
          name: onlineAtt.name || target,
          avatar_url: onlineAtt.avatar_url || "",
          status: onlineAtt.status === "idle" ? "idle" : "active",
        });
      } else if (stored) {
        this._send(ws, {
          type: "lookup_result",
          code: target,
          found: true,
          name: stored.name || target,
          avatar_url: stored.avatar_url || "",
          status: "offline",
        });
      } else {
        this._send(ws, { type: "lookup_result", code: target, found: false });
      }
      return;
    }

    if (type === "presence") {
      const prevAllow = att.allow_guest_control !== false;
      const prevVideoKey = JSON.stringify(att.video || null);
      const prevMusicKey = JSON.stringify(att.music || null);

      if (msg.name !== undefined) {
        att.name = String(msg.name || att.name || att.code).trim().slice(0, 64) || att.code;
      }
      if (msg.avatar_url !== undefined) {
        att.avatar_url = String(msg.avatar_url || "").trim().slice(0, 512);
      }
      if (msg.status !== undefined) {
        att.status = msg.status === "idle" ? "idle" : "active";
      }
      if (msg.share_enabled !== undefined) {
        att.share_enabled = Boolean(msg.share_enabled);
      }
      if (msg.allow_guest_control !== undefined) {
        att.allow_guest_control = Boolean(msg.allow_guest_control);
      }
      if (msg.video !== undefined) {
        att.video = sanitizeDeckState(msg.video, "video");
      }
      if (msg.music !== undefined) {
        att.music = sanitizeDeckState(msg.music, "music");
      }
      this._setAttachment(ws, att);

      if (prevAllow !== att.allow_guest_control) {
        this._broadcastRoomMembers(att.code, "video");
        this._broadcastRoomMembers(att.code, "music");
      }

      if (prevVideoKey !== JSON.stringify(att.video || null)) {
        const vidSet = this._rooms.get(this._roomKey(att.code, "video"));
        if (vidSet && vidSet.size > 0) {
          this._bumpRev(att.code, "video");
          this._broadcastPartyState(att.code, "video", "sync", "presence", att.code, att.name, att.code);
        }
      }
      if (prevMusicKey !== JSON.stringify(att.music || null)) {
        const musSet = this._rooms.get(this._roomKey(att.code, "music"));
        if (musSet && musSet.size > 0) {
          this._bumpRev(att.code, "music");
          this._broadcastPartyState(att.code, "music", "sync", "presence", att.code, att.name, att.code);
        }
      }

      await this._broadcastFriendState(att.code);
      return;
    }

    if (type === "join_party") {
      const hostCode = normalizeCode(msg.host_code);
      const deck = String(msg.deck || "").trim().toLowerCase();
      if (!VALID_CODE_RE.test(hostCode) || !VALID_DECKS.has(deck)) return;
      const hostWs = this._socketsByCode.get(hostCode);
      const hostAtt = hostWs ? this._getAttachment(hostWs) : null;
      if (!hostWs || !hostAtt) {
        this._send(ws, {
          type: "party_notice",
          message: "Tu amigo no está conectado en SwiftPlay en este momento.",
        });
        return;
      }

      this._leaveDeckRoom(ws, att, deck);
      // Ensure host is registered as member of their own deck room so bidirectional controls reach them
      if (!hostAtt.joined?.[deck]) {
        hostAtt.joined[deck] = hostCode;
        this._addMemberToRoom(hostCode, deck, hostCode);
        this._setAttachment(hostWs, hostAtt);
      }

      att.joined[deck] = hostCode;
      this._addMemberToRoom(hostCode, deck, att.code);
      this._setAttachment(ws, att);

      const key = this._roomKey(hostCode, deck);
      const rev = this._roomRev.get(key) || this._bumpRev(hostCode, deck);
      const deckState = deck === "music" ? hostAtt.music : hostAtt.video;
      this._send(ws, {
        type: "party_state",
        host_code: hostCode,
        host_name: hostAtt.name || hostCode,
        deck,
        reason: "join",
        action: "join",
        rev,
        state: deckState || null,
        updated_by: hostCode,
        updated_by_name: hostAtt.name || hostCode,
        allow_guest_control: hostAtt.allow_guest_control !== false,
        members: this._roomMembersSummary(hostCode, deck),
      });
      this._broadcastRoomMembers(hostCode, deck);
      await this._broadcastFriendState(hostCode);
      return;
    }

    if (type === "leave_party") {
      const deck = String(msg.deck || "").trim().toLowerCase();
      if (!VALID_DECKS.has(deck)) return;
      const prevHost = att.joined?.[deck];
      this._leaveDeckRoom(ws, att, deck);
      if (prevHost) {
        await this._broadcastFriendState(prevHost);
      }
      return;
    }

    if (type === "party_sync") {
      const hostCode = normalizeCode(msg.host_code || att.code);
      const deck = String(msg.deck || "").trim().toLowerCase();
      const action = String(msg.action || "sync").trim().slice(0, 32);
      if (!VALID_CODE_RE.test(hostCode) || !VALID_DECKS.has(deck)) return;

      const hostWs = this._socketsByCode.get(hostCode);
      const hostAtt = hostWs ? this._getAttachment(hostWs) : null;
      if (!hostWs || !hostAtt) return;

      const isHost = att.code === hostCode;
      const room = this._rooms.get(this._roomKey(hostCode, deck));
      const inRoom = Boolean(room && room.has(att.code));
      if (!isHost && (!inRoom || hostAtt.allow_guest_control === false)) {
        this._send(ws, {
          type: "party_notice",
          message: "El anfitrión desactivó el control compartido en esta sala.",
        });
        return;
      }

      if (!hostAtt.joined?.[deck]) {
        hostAtt.joined[deck] = hostCode;
        this._addMemberToRoom(hostCode, deck, hostCode);
      }

      const nextState = sanitizeDeckState(msg.state, deck);
      if (deck === "music") {
        hostAtt.music = nextState;
      } else {
        hostAtt.video = nextState;
      }
      hostAtt.status = nextState && !nextState.paused ? "active" : hostAtt.status;
      this._setAttachment(hostWs, hostAtt);

      this._bumpRev(hostCode, deck);
      this._broadcastPartyState(hostCode, deck, "sync", action, att.code, att.name, att.code);
      await this._broadcastFriendState(hostCode);
    }
  }

  async _cleanupSocket(ws, { notifyOffline = true } = {}) {
    const att = this._getAttachment(ws);
    if (!att || !att.code) return;
    const currentWs = this._socketsByCode.get(att.code);
    if (currentWs === ws) {
      this._socketsByCode.delete(att.code);
    }

    for (const deck of ["video", "music"]) {
      const hostCode = att.joined?.[deck];
      if (hostCode) {
        this._removeMemberFromRoom(hostCode, deck, att.code);
        this._broadcastRoomMembers(hostCode, deck);
      }
      // Also notify any room where this user was host
      const ownRoom = this._rooms.get(this._roomKey(att.code, deck));
      if (ownRoom && ownRoom.size > 0) {
        for (const memberCode of [...ownRoom]) {
          const mWs = this._socketsByCode.get(memberCode);
          const mAtt = mWs ? this._getAttachment(mWs) : null;
          if (mWs && mAtt) {
            mAtt.joined[deck] = null;
            this._setAttachment(mWs, mAtt);
            this._send(mWs, {
              type: "party_notice",
              message: `${att.name || att.code} cerró SwiftPlay.`,
            });
            this._send(mWs, {
              type: "party_members",
              host_code: att.code,
              deck,
              members: [],
              allow_guest_control: false,
            });
          }
        }
        this._rooms.delete(this._roomKey(att.code, deck));
      }
    }

    if (notifyOffline) {
      await this._saveProfile(att.code, {
        code: att.code,
        secret: att.secret,
        name: att.name,
        avatar_url: att.avatar_url,
        last_seen: Date.now() / 1000,
      });
      await this._broadcastFriendState(att.code);
    }
  }

  async webSocketClose(ws) {
    this._ensureHydrated();
    await this._cleanupSocket(ws, { notifyOffline: true });
  }

  async webSocketError(ws) {
    this._ensureHydrated();
    await this._cleanupSocket(ws, { notifyOffline: true });
  }
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/health" || url.pathname === "/") {
      if (request.headers.get("Upgrade")?.toLowerCase() !== "websocket") {
        const id = env.WATCHPARTY_HUB.idFromName("global");
        return env.WATCHPARTY_HUB.get(id).fetch(request);
      }
    }
    if (url.pathname === "/ws" || request.headers.get("Upgrade")?.toLowerCase() === "websocket") {
      const id = env.WATCHPARTY_HUB.idFromName("global");
      return env.WATCHPARTY_HUB.get(id).fetch(request);
    }
    return new Response("Not found", { status: 404 });
  },
};
