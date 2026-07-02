/* Redeem website client: login, catalog polling, redeeming, password change. */
(function () {
  "use strict";

  var POLL_MS = 10000;
  var state = {
    token: localStorage.getItem("wr_token") || "",
    username: localStorage.getItem("wr_user") || "",
    catalog: null,
    countdowns: {},        // reward_id -> epoch-ms when free again
    forcedChange: false,
    pollTimer: null,
  };

  var $ = function (id) { return document.getElementById(id); };

  // ---------- api ----------
  function api(path, opts) {
    opts = opts || {};
    var headers = { "Content-Type": "application/json" };
    if (state.token) headers["X-Session"] = state.token;
    return fetch(path, {
      method: opts.method || "GET",
      headers: headers,
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) {
        if (res.status === 401) { clearSession(); render(); }
        if (!res.ok) {
          var msg = (data && data.detail) || "Fehler " + res.status;
          throw new Error(typeof msg === "string" ? msg : "Anfrage fehlgeschlagen");
        }
        return data;
      });
    });
  }

  function setSession(token, username) {
    state.token = token || "";
    state.username = username || "";
    localStorage.setItem("wr_token", state.token);
    localStorage.setItem("wr_user", state.username);
  }

  function clearSession() {
    state.token = "";
    state.username = "";
    state.forcedChange = false;
    localStorage.removeItem("wr_token");
    localStorage.removeItem("wr_user");
  }

  // ---------- toast ----------
  var toastTimer = null;
  function toast(message, kind) {
    var el = $("toast");
    el.textContent = message;
    el.className = "toast " + (kind || "");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { el.className = "toast hidden"; }, 4200);
  }

  // ---------- rendering ----------
  function show(id, visible) { $(id).classList.toggle("hidden", !visible); }

  function render() {
    var cat = state.catalog;
    var loggedIn = !!state.token && !!(cat && cat.user);

    if (cat) {
      $("page-title").textContent = cat.title || "Redeems";
      document.title = cat.title || "Redeems";
      $("page-tagline").textContent = cat.tagline || "";
      if (cat.twitch_url) {
        $("twitch-link").href = cat.twitch_url;
        $("offline-twitch").href = cat.twitch_url;
        $("footer-twitch").href = cat.twitch_url;
        $("footer-channel").textContent = cat.channel_display || cat.channel || "";
      }
    }

    var offline = cat && !cat.enabled;
    show("view-offline", !!offline);
    show("view-login", !offline && !loggedIn);
    show("view-rewards", !offline && loggedIn);
    show("user-box", loggedIn);

    if (offline) $("offline-text").textContent = cat.offline_text || "";

    if (loggedIn) {
      $("user-name").textContent = cat.user.username;
      $("points-total").textContent =
        cat.points_total != null ? cat.points_total.toLocaleString("de-DE") : "—";
      renderCards(cat.items || []);
      if (cat.user.must_change_password && !state.forcedChange) {
        state.forcedChange = true;
        openPwModal(true);
      }
    }
  }

  function renderCards(items) {
    var wrap = $("cards");
    wrap.innerHTML = "";
    show("rewards-empty", items.length === 0);
    items.forEach(function (item) {
      var card = document.createElement("div");
      card.className = "reward-card";
      card.dataset.rewardId = item.reward_id;

      var head = document.createElement("div");
      head.className = "reward-head";
      var title = document.createElement("h3");
      title.className = "reward-title";
      title.textContent = item.label || item.title || "Belohnung";
      var badge = document.createElement("span");
      badge.className = "cost-badge";
      badge.innerHTML =
        '<svg viewBox="0 0 20 20" aria-hidden="true"><path fill="currentColor" ' +
        'd="M10 1 3 6v8l7 5 7-5V6l-7-5zm0 2.4L15 7v6l-5 3.6L5 13V7l5-3.6z"/>' +
        '<circle cx="10" cy="10" r="2.4" fill="currentColor"/></svg>';
      badge.appendChild(document.createTextNode(
        item.cost != null ? item.cost.toLocaleString("de-DE") : "?"));
      head.appendChild(title);
      head.appendChild(badge);
      card.appendChild(head);

      if (item.description) {
        var desc = document.createElement("p");
        desc.className = "reward-desc";
        desc.textContent = item.description;
        card.appendChild(desc);
      }

      var stateLine = document.createElement("div");
      stateLine.className = "reward-state";
      card.appendChild(stateLine);

      var btn = document.createElement("button");
      btn.className = "btn btn-primary btn-block";
      btn.type = "button";
      btn.textContent = "Einlösen";
      btn.addEventListener("click", function () { redeem(item, btn, card); });
      card.appendChild(btn);

      // server-known cooldown -> local countdown
      if (item.retry_in > 0) {
        var until = Date.now() + item.retry_in * 1000;
        var known = state.countdowns[item.reward_id] || 0;
        state.countdowns[item.reward_id] = Math.max(known, until);
      }
      updateCardState(card, item, btn, stateLine);
      wrap.appendChild(card);
    });
  }

  function updateCardState(card, item, btn, stateLine) {
    var remaining = Math.ceil(((state.countdowns[item.reward_id] || 0) - Date.now()) / 1000);
    if (remaining > 0) {
      btn.disabled = true;
      btn.textContent = "Cooldown · " + formatSecs(remaining);
      stateLine.textContent = "";
      card.classList.add("disabled");
    } else if (!item.available && item.blocked_reason) {
      btn.disabled = true;
      btn.textContent = "Einlösen";
      stateLine.textContent = item.blocked_reason;
      card.classList.add("disabled");
    } else {
      btn.disabled = false;
      btn.textContent = "Einlösen";
      stateLine.textContent = "";
      card.classList.remove("disabled");
    }
  }

  function formatSecs(s) {
    if (s < 60) return s + "s";
    var m = Math.floor(s / 60);
    return m + "m " + (s % 60) + "s";
  }

  // every second: refresh countdown labels without re-rendering everything
  setInterval(function () {
    if (!state.catalog || !state.catalog.items) return;
    state.catalog.items.forEach(function (item) {
      var card = document.querySelector(
        '.reward-card[data-reward-id="' + item.reward_id + '"]');
      if (!card) return;
      var btn = card.querySelector("button");
      var stateLine = card.querySelector(".reward-state");
      updateCardState(card, item, btn, stateLine);
    });
  }, 1000);

  // ---------- actions ----------
  function loadCatalog() {
    return api("/api/catalog").then(function (cat) {
      state.catalog = cat;
      render();
    }).catch(function () { /* transient — next poll retries */ });
  }

  function startPolling() {
    clearInterval(state.pollTimer);
    state.pollTimer = setInterval(loadCatalog, POLL_MS);
  }

  function redeem(item, btn, card) {
    btn.disabled = true;
    btn.textContent = "Wird eingelöst…";
    api("/api/redeem", { method: "POST", body: { reward_id: item.reward_id } })
      .then(function (res) {
        if (res.retry_in > 0) {
          state.countdowns[item.reward_id] = Date.now() + res.retry_in * 1000;
        }
        if (res.ok) {
          card.classList.remove("flash");
          void card.offsetWidth; // restart the animation
          card.classList.add("flash");
          toast(res.message || "Eingelöst!", "ok");
        } else {
          toast(res.message || "Hat nicht geklappt.", "err");
        }
        loadCatalog();
      })
      .catch(function (e) {
        toast(e.message, "err");
        loadCatalog();
      });
  }

  // ---------- login ----------
  $("login-form").addEventListener("submit", function (ev) {
    ev.preventDefault();
    var errBox = $("login-error");
    errBox.classList.add("hidden");
    api("/api/login", {
      method: "POST",
      body: { username: $("login-user").value.trim(), password: $("login-pass").value },
    }).then(function (res) {
      if (!res.ok) {
        errBox.textContent = res.message || "Login fehlgeschlagen.";
        errBox.classList.remove("hidden");
        return;
      }
      setSession(res.token, res.username);
      $("login-pass").value = "";
      loadCatalog();
    }).catch(function (e) {
      errBox.textContent = e.message;
      errBox.classList.remove("hidden");
    });
  });

  // ---------- user menu ----------
  $("user-menu-btn").addEventListener("click", function (ev) {
    ev.stopPropagation();
    $("user-menu").classList.toggle("hidden");
  });
  document.addEventListener("click", function () {
    $("user-menu").classList.add("hidden");
  });
  $("menu-logout").addEventListener("click", function () {
    var token = state.token;
    clearSession();
    render();
    api("/api/logout", { method: "POST", body: { token: token } }).catch(function () {});
    loadCatalog();
  });
  $("menu-password").addEventListener("click", function () { openPwModal(false); });

  // ---------- password modal ----------
  function openPwModal(forced) {
    show("pw-modal", true);
    show("pw-forced-hint", forced);
    $("pw-cancel").classList.toggle("hidden", forced);
    $("pw-error").classList.add("hidden");
    $("pw-old").value = "";
    $("pw-new").value = "";
    $("pw-new2").value = "";
    $("pw-old").focus();
  }
  $("pw-cancel").addEventListener("click", function () { show("pw-modal", false); });
  $("pw-form").addEventListener("submit", function (ev) {
    ev.preventDefault();
    var errBox = $("pw-error");
    errBox.classList.add("hidden");
    if ($("pw-new").value !== $("pw-new2").value) {
      errBox.textContent = "Die neuen Passwörter stimmen nicht überein.";
      errBox.classList.remove("hidden");
      return;
    }
    api("/api/change-password", {
      method: "POST",
      body: { old_password: $("pw-old").value, new_password: $("pw-new").value },
    }).then(function (res) {
      if (!res.ok) {
        errBox.textContent = res.message || "Ändern fehlgeschlagen.";
        errBox.classList.remove("hidden");
        return;
      }
      // the change invalidates all sessions; the server hands us a fresh one
      setSession(res.token, state.username);
      state.forcedChange = false;
      show("pw-modal", false);
      toast("Passwort geändert.", "ok");
      loadCatalog();
    }).catch(function (e) {
      errBox.textContent = e.message;
      errBox.classList.remove("hidden");
    });
  });

  // ---------- boot ----------
  loadCatalog();
  startPolling();
})();
