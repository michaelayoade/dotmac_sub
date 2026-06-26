/* Live chat client. Brokers a session through the sub (which asserts the
 * authenticated customer/reseller identity to the CRM), then talks to the CRM
 * chat_widget channel directly: WebSocket for real-time, REST for send/history.
 *
 * The browser never supplies identity — it only ever holds the opaque
 * visitor_token returned by the broker. */
(function () {
  "use strict";

  var root = document.getElementById("dm-chat");
  if (!root) return;

  var els = {
    root: root,
    bubble: document.getElementById("dm-chat-bubble"),
    unread: document.getElementById("dm-chat-unread"),
    panel: document.getElementById("dm-chat-panel"),
    close: document.getElementById("dm-chat-close"),
    log: document.getElementById("dm-chat-log"),
    typing: document.getElementById("dm-chat-typing"),
    form: document.getElementById("dm-chat-form"),
    input: document.getElementById("dm-chat-input"),
    send: document.getElementById("dm-chat-send"),
    status: document.getElementById("dm-chat-status"),
  };

  var sessionEndpoint = root.getAttribute("data-session-endpoint") ||
    "/api/v1/me/chat/session";

  var state = {
    session: null, // {visitor_token, conversation_id, ws_url, api_base, session_id}
    ws: null,
    started: false,
    open: false,
    unread: 0,
    seen: {}, // message id -> true (dedupe)
    typingTimer: null,
    reconnect: 0,
  };

  // ── rendering ──────────────────────────────────────────────────────────
  function esc(s) {
    var d = document.createElement("div");
    d.textContent = s == null ? "" : String(s);
    return d.innerHTML;
  }

  function appendMessage(msg) {
    // msg: {id, body, direction, author_name, created_at}
    var id = msg.id || msg.message_id;
    if (id && state.seen[id]) return;
    if (id) state.seen[id] = true;
    var dir = msg.direction === "outbound" ? "in" : "out"; // outbound (agent)=incoming to us
    var row = document.createElement("div");
    row.className = "dm-chat-msg dm-chat-msg-" + dir;
    var who = dir === "in" ? (msg.author_name || "Support") : "You";
    row.innerHTML =
      '<span class="dm-chat-msg-who">' + esc(who) + "</span>" +
      '<span class="dm-chat-msg-body">' + esc(msg.body) + "</span>";
    els.log.appendChild(row);
    els.log.scrollTop = els.log.scrollHeight;
  }

  function setStatus(text) {
    if (!text) {
      els.status.hidden = true;
      els.status.textContent = "";
    } else {
      els.status.hidden = false;
      els.status.textContent = text;
    }
  }

  function setUnread(n) {
    state.unread = n;
    if (n > 0) {
      els.unread.hidden = false;
      els.unread.textContent = n > 9 ? "9+" : String(n);
    } else {
      els.unread.hidden = true;
    }
  }

  function enableComposer(on) {
    els.input.disabled = !on;
    els.send.disabled = !on;
  }

  function cookieValue(name) {
    var escaped = name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    var match = document.cookie.match(new RegExp("(?:^|;\\s*)" + escaped + "=([^;]+)"));
    return match ? decodeURIComponent(match[1]) : "";
  }

  function csrfToken() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    if (meta && meta.content) return meta.content;
    return cookieValue("csrf_token");
  }

  // ── CRM REST (direct, X-Visitor-Token) ─────────────────────────────────
  function crm(path, opts) {
    opts = opts || {};
    opts.headers = opts.headers || {};
    opts.headers["X-Visitor-Token"] = state.session.visitor_token;
    return fetch(state.session.api_base + path, opts);
  }

  function loadHistory() {
    return crm(
      "/session/" + state.session.session_id + "/messages?limit=50"
    )
      .then(function (r) { return r.ok ? r.json() : { messages: [] }; })
      .then(function (data) {
        (data.messages || []).forEach(appendMessage);
      })
      .catch(function () {});
  }

  function sendMessage(body) {
    return crm("/session/" + state.session.session_id + "/message", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ body: body }),
    })
      .then(function (r) {
        if (!r.ok) throw new Error("send failed");
        return r.json();
      })
      .then(function (m) {
        appendMessage({
          id: m.message_id,
          body: m.body,
          direction: "inbound", // our own message → render as outgoing
        });
      });
  }

  function markRead() {
    if (!state.session) return;
    crm("/session/" + state.session.session_id + "/read", { method: "POST" })
      .catch(function () {});
    setUnread(0);
  }

  // ── WebSocket ──────────────────────────────────────────────────────────
  function connectWs() {
    if (!state.session || !state.session.ws_url) return;
    var url = state.session.ws_url + "?token=" +
      encodeURIComponent(state.session.visitor_token);
    var ws;
    try {
      ws = new WebSocket(url);
    } catch (e) {
      return;
    }
    state.ws = ws;

    ws.onopen = function () {
      state.reconnect = 0;
      setStatus("");
    };

    ws.onmessage = function (ev) {
      var data;
      try { data = JSON.parse(ev.data); } catch (e) { return; }
      var type = data.event || data.type;
      var payload = data.data || data.payload || data;
      if (type === "message_new") {
        appendMessage({
          id: payload.id || payload.message_id,
          body: payload.body,
          direction: payload.direction || "outbound",
          author_name: payload.author_name,
        });
        if (!state.open) setUnread(state.unread + 1);
        else markRead();
      } else if (type === "user_typing") {
        showTyping();
      }
    };

    ws.onclose = function () {
      state.ws = null;
      // Backoff reconnect while the panel is alive.
      if (state.started) {
        var delay = Math.min(1000 * Math.pow(2, state.reconnect++), 15000);
        setStatus("Reconnecting…");
        setTimeout(connectWs, delay);
      }
    };
    ws.onerror = function () { try { ws.close(); } catch (e) {} };
  }

  function showTyping() {
    els.typing.hidden = false;
    if (state.typingTimer) clearTimeout(state.typingTimer);
    state.typingTimer = setTimeout(function () {
      els.typing.hidden = true;
    }, 3000);
  }

  function sendTyping() {
    if (state.ws && state.ws.readyState === 1) {
      try {
        state.ws.send(JSON.stringify({ type: "typing", is_typing: true }));
      } catch (e) {}
    }
  }

  // ── lifecycle ──────────────────────────────────────────────────────────
  function start() {
    if (state.started) return Promise.resolve();
    state.started = true;
    enableComposer(false);
    setStatus("Connecting…");
    return fetch(sessionEndpoint, {
      method: "POST",
      credentials: "same-origin",
      headers: (function () {
        var headers = { "Content-Type": "application/json" };
        var token = csrfToken();
        if (token) headers["X-CSRF-Token"] = token;
        return headers;
      })(),
      body: "{}",
    })
      .then(function (r) {
        if (r.status === 503) throw new Error("disabled");
        if (!r.ok) throw new Error("session failed");
        return r.json();
      })
      .then(function (s) {
        state.session = s;
        setStatus("");
        enableComposer(true);
        els.input.focus();
        connectWs();
        return loadHistory();
      })
      .catch(function (e) {
        state.started = false;
        setStatus(
          e && e.message === "disabled"
            ? "Chat is currently unavailable."
            : "Couldn't start chat. Please try again."
        );
      });
  }

  function openPanel() {
    els.panel.hidden = false;
    els.bubble.setAttribute("aria-expanded", "true");
    state.open = true;
    start().then(markRead);
  }

  function closePanel() {
    els.panel.hidden = true;
    els.bubble.setAttribute("aria-expanded", "false");
    state.open = false;
  }

  // ── wiring ─────────────────────────────────────────────────────────────
  els.bubble.addEventListener("click", function () {
    state.open ? closePanel() : openPanel();
  });
  els.close.addEventListener("click", closePanel);

  els.form.addEventListener("submit", function (ev) {
    ev.preventDefault();
    var body = (els.input.value || "").trim();
    if (!body || !state.session) return;
    els.input.value = "";
    sendMessage(body).catch(function () {
      setStatus("Message failed to send.");
      els.input.value = body;
    });
  });

  var typingThrottle = 0;
  els.input.addEventListener("input", function () {
    var now = Date.now();
    if (now - typingThrottle > 1500) {
      typingThrottle = now;
      sendTyping();
    }
  });

  root.hidden = false;
})();
