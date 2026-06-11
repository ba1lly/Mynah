/* Mynah frontend. Renders exclusively from backend state (get_state /
   pushed "state" events); all mutations go through window.pywebview.api.
   No frameworks — a single render(state) keeps every control's
   enabled/disabled status derived in one place (the old Tk GUI scattered
   this across 7 callbacks; see issue #2). */
"use strict";

const $ = (sel) => document.querySelector(sel);

const ui = {
  state: null,
  selectedRecording: null,   // path string
  pendingSelect: null,       // path to auto-select after next state render
  consentText: null,
  timerHandle: null,
  logAutoScroll: true,
  update: null,              // {version, notes, url} when newer release exists
};

/* ---------------------------- backend bridge --------------------------- */

function api() {
  return window.pywebview && window.pywebview.api;
}

window.__mynah = {
  dispatch(msg) {
    try {
      handleEvent(msg.event, msg.payload);
    } catch (e) {
      console.error("event dispatch failed", e);
    }
  },
};

function handleEvent(event, payload) {
  switch (event) {
    case "state":
      render(payload);
      break;
    case "log":
      appendLog(payload);
      break;
    case "error":
      toast(`${payload.title}: ${payload.message}`, "err", 10000);
      break;
    case "confirm-close":
      $("#overlay-close").hidden = false;
      break;
    case "recording-saved":
      ui.pendingSelect = payload.path;
      toast("Recording saved.", "ok");
      break;
    case "transcribed":
      toast(`Transcript saved:\n${payload.path}`, "ok", 12000);
      break;
  }
}

/* ------------------------------- render -------------------------------- */

function render(s) {
  ui.state = s;

  $("#version-tag").textContent = `v${s.version}`;

  // LEDs
  const ledLink = $("#led-link");
  ledLink.className = "led " + (s.connected ? "on-ok" : s.connecting ? "on-warn" : "");
  const ledRec = $("#led-rec");
  ledRec.className = "led " + (s.recording ? "on-rec" : "");

  // Transport card
  const btnConnect = $("#btn-connect");
  btnConnect.disabled = s.connected || s.connecting;
  btnConnect.textContent = s.connecting
    ? "Connecting…"
    : s.connected
    ? "Connected"
    : "Connect to Discord";
  $("#btn-refresh-parts").disabled = !s.connected;

  const link = $("#link-status");
  if (s.connected) {
    link.textContent = "Discord RPC: connected ✓";
    link.className = "link-status ok";
  } else if (s.connecting) {
    link.textContent = "Discord RPC: connecting…";
    link.className = "link-status";
  } else if (s.rpcError) {
    link.textContent = `Discord RPC: ${s.rpcError}`;
    link.className = "link-status err";
  } else {
    link.textContent = "Discord RPC: not connected";
    link.className = "link-status";
  }

  const parts = $("#participants");
  parts.replaceChildren();
  if (s.participants.length) {
    for (const p of s.participants) {
      const chip = document.createElement("span");
      chip.className = "chip";
      chip.textContent = p;
      chip.title = p;
      parts.appendChild(chip);
    }
  } else {
    const hint = document.createElement("span");
    hint.className = "hint";
    hint.textContent = s.connected
      ? "No participants — join a voice channel."
      : "Connect, then join a voice channel.";
    parts.appendChild(hint);
  }

  // Recorder card
  const live = s.recording;
  $("#onair").hidden = !live;
  $("#vu").classList.toggle("live", live);
  $("#counter").classList.toggle("live", live);
  const btnRec = $("#btn-rec");
  btnRec.disabled = !s.connected || s.recording || s.starting || s.stopping;
  btnRec.classList.toggle("live", live);
  $("#btn-stop").disabled = !s.recording || s.stopping;

  const deckStatus = $("#deck-status");
  if (s.starting) {
    deckStatus.textContent = "Starting…";
    deckStatus.className = "deck-status";
  } else if (s.stopping) {
    deckStatus.textContent = "Saving…";
    deckStatus.className = "deck-status";
  } else if (s.recording) {
    deckStatus.textContent = `Recording — ${s.participants.length} participant(s)`;
    deckStatus.className = "deck-status live";
  } else {
    deckStatus.textContent = "Idle";
    deckStatus.className = "deck-status";
  }
  if (!live) $("#counter").textContent = "00:00:00";

  // Archive card
  renderRecordings(s);

  // Update pill
  const pill = $("#update-pill");
  if (s.update) {
    pill.hidden = false;
    $("#update-pill-ver").textContent = `v${s.update.version}`;
    ui.update = s.update;
  } else {
    pill.hidden = true;
  }

  // Status bar
  $("#statusbar-text").textContent = s.status || "Ready";
}

function openUpdateModal() {
  if (!ui.update) return;
  $("#update-versions").textContent =
    `Mynah v${ui.update.version} is available — you have v${ui.state.version}.`;
  $("#update-notes").textContent =
    ui.update.notes || "(no release notes provided)";
  $("#overlay-update").hidden = false;
}

function renderRecordings(s) {
  const list = $("#rec-list");
  list.replaceChildren();

  if (ui.pendingSelect && s.recordings.some((r) => r.path === ui.pendingSelect)) {
    ui.selectedRecording = ui.pendingSelect;
    ui.pendingSelect = null;
  }
  if (!s.recordings.some((r) => r.path === ui.selectedRecording)) {
    ui.selectedRecording = s.recordings.length ? s.recordings[0].path : null;
  }

  if (!s.recordings.length) {
    const hint = document.createElement("div");
    hint.className = "hint pad";
    hint.textContent = "No recordings yet.";
    list.appendChild(hint);
  }

  for (const rec of s.recordings) {
    const row = document.createElement("div");
    row.className = "rec-row" + (rec.path === ui.selectedRecording ? " sel" : "");
    // Backend labels look like "Meeting  —  2026-05-28 00:39"; split for layout.
    const sep = rec.label.lastIndexOf("  —  ");
    const name = sep > 0 ? rec.label.slice(0, sep) : rec.label;
    const date = sep > 0 ? rec.label.slice(sep + 5) : "";
    const nameEl = document.createElement("span");
    nameEl.className = "rec-name";
    nameEl.textContent = name;
    nameEl.title = rec.label;
    const dateEl = document.createElement("span");
    dateEl.className = "rec-date";
    dateEl.textContent = date;
    row.append(nameEl, dateEl);
    row.addEventListener("click", () => {
      ui.selectedRecording = rec.path;
      renderRecordings(ui.state);
    });
    list.appendChild(row);
  }

  const btnTx = $("#btn-transcribe");
  btnTx.disabled =
    !ui.selectedRecording || s.transcribing || s.whisperxAvailable === false;
  btnTx.textContent = s.transcribing ? "Transcribing…" : "Transcribe selected";

  const hint = $("#tx-hint");
  if (s.whisperxAvailable === false) {
    hint.textContent = "WhisperX not installed — transcription unavailable.";
  } else if (s.transcribing) {
    hint.textContent = "Running Whisper — this is the slow step.";
  } else {
    hint.textContent = "";
  }
}

/* ------------------------------ elapsed timer --------------------------- */

function tickCounter() {
  const s = ui.state;
  if (!s || !s.recording || !s.recordingStartedAt) return;
  const secs = Math.max(0, Math.floor(Date.now() / 1000 - s.recordingStartedAt));
  const h = String(Math.floor(secs / 3600)).padStart(2, "0");
  const m = String(Math.floor((secs % 3600) / 60)).padStart(2, "0");
  const sec = String(secs % 60).padStart(2, "0");
  $("#counter").textContent = `${h}:${m}:${sec}`;
}

/* --------------------------------- log ---------------------------------- */

const MAX_LOG_LINES = 2000;

function appendLog(entry) {
  const logEl = $("#log");
  const nearBottom =
    logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 32;
  const ln = document.createElement("span");
  ln.className = `ln ${entry.level || ""}`;
  ln.textContent = entry.line;
  logEl.appendChild(ln);
  while (logEl.childNodes.length > MAX_LOG_LINES) {
    logEl.removeChild(logEl.firstChild);
  }
  if (nearBottom) logEl.scrollTop = logEl.scrollHeight;
}

/* -------------------------------- toasts -------------------------------- */

function toast(message, kind = "info", ttl = 6000) {
  const host = $("#toasts");
  const el = document.createElement("div");
  el.className = `toast ${kind === "err" ? "err" : kind === "ok" ? "ok" : ""}`;
  const msg = document.createElement("span");
  msg.className = "toast-msg";
  msg.textContent = message;
  const x = document.createElement("button");
  x.className = "toast-x";
  x.textContent = "✕";
  x.addEventListener("click", () => el.remove());
  el.append(msg, x);
  host.appendChild(el);
  setTimeout(() => el.remove(), ttl);
}

/* -------------------------------- theme --------------------------------- */

const THEME_ORDER = ["system", "light", "dark"];

function applyTheme(pref) {
  const sysDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
  const effective = pref === "system" ? (sysDark ? "dark" : "light") : pref;
  document.documentElement.dataset.theme = effective;
  $("#theme-icon-system").hidden = pref !== "system";
  $("#theme-icon-light").hidden = pref !== "light";
  $("#theme-icon-dark").hidden = pref !== "dark";
  $("#btn-theme").title = `Theme: ${pref}`;
}

function initTheme() {
  let pref = localStorage.getItem("mynah-theme") || "system";
  if (!THEME_ORDER.includes(pref)) pref = "system";
  applyTheme(pref);
  window
    .matchMedia("(prefers-color-scheme: dark)")
    .addEventListener("change", () => {
      const cur = localStorage.getItem("mynah-theme") || "system";
      if (cur === "system") applyTheme("system");
    });
  $("#btn-theme").addEventListener("click", () => {
    const cur = localStorage.getItem("mynah-theme") || "system";
    const next = THEME_ORDER[(THEME_ORDER.indexOf(cur) + 1) % THEME_ORDER.length];
    localStorage.setItem("mynah-theme", next);
    applyTheme(next);
  });
}

/* ------------------------------- settings -------------------------------- */

async function openSettings() {
  const cfg = await api().get_settings();
  const v = cfg.values;
  const o = cfg.options;

  $("#set-client-id").value = v.discord_client_id;
  $("#set-hf-token").value = v.hf_token;

  fillSelect($("#set-model"), o.whisper_models.map((m) => ({ value: m, label: m })), v.whisper_model);
  fillSelect($("#set-source"), o.audio_sources, v.audio_source);

  const loopOpts = [
    { value: "", label: o.loopback_default_label },
    ...o.loopback_devices.map((d) => ({ value: d, label: d })),
  ];
  // A saved device that is no longer enumerated falls back to the default
  // row, matching the Tk dialog's behaviour.
  const loopSel = o.loopback_devices.includes(v.loopback_device_name)
    ? v.loopback_device_name
    : "";
  fillSelect($("#set-loopback"), loopOpts, loopSel);

  $("#set-recdir").value = v.recordings_dir;
  $("#set-check-updates").checked = !!v.check_updates;
  $("#settings-error").hidden = true;
  $("#overlay-settings").hidden = false;
  $("#set-client-id").focus();
}

function fillSelect(sel, options, selected) {
  sel.replaceChildren();
  for (const opt of options) {
    const el = document.createElement("option");
    el.value = opt.value;
    el.textContent = opt.label;
    if (opt.value === selected) el.selected = true;
    sel.appendChild(el);
  }
}

async function saveSettings() {
  const values = {
    discord_client_id: $("#set-client-id").value,
    hf_token: $("#set-hf-token").value,
    whisper_model: $("#set-model").value,
    audio_source: $("#set-source").value,
    recordings_dir: $("#set-recdir").value,
    loopback_device_name: $("#set-loopback").value || null,
    check_updates: $("#set-check-updates").checked,
  };
  const res = await api().save_settings(values);
  if (!res.ok) {
    const err = $("#settings-error");
    err.textContent = res.error;
    err.hidden = false;
    return;
  }
  $("#overlay-settings").hidden = true;
  toast("Settings saved.", "ok");
}

/* ------------------------------- actions --------------------------------- */

async function doConnect() {
  const res = await api().connect();
  if (!res.ok && res.error === "not_configured") {
    toast("Set your Discord Client ID first.", "err");
    openSettings();
  } else if (!res.ok) {
    toast(res.error, "err");
  }
}

async function askConsentThenRecord() {
  if (ui.consentText === null) {
    ui.consentText = await api().get_consent_text();
  }
  $("#consent-text").textContent = ui.consentText;
  $("#overlay-consent").hidden = false;
}

async function doStartRecording(granted) {
  $("#overlay-consent").hidden = true;
  const name = $("#meeting-name").value;
  const res = await api().start_recording(name, granted);
  if (!res.ok && res.error !== "consent_declined") {
    toast(res.error, "err");
  }
}

async function doTranscribe() {
  if (!ui.selectedRecording) return;
  const res = await api().transcribe_recording(ui.selectedRecording);
  if (!res.ok) toast(res.error, "err");
}

/* --------------------------------- init ---------------------------------- */

async function init() {
  initTheme();

  $("#btn-connect").addEventListener("click", doConnect);
  $("#btn-refresh-parts").addEventListener("click", () => api().refresh_participants());
  $("#btn-rec").addEventListener("click", askConsentThenRecord);
  $("#btn-stop").addEventListener("click", () => api().stop_recording());
  $("#btn-refresh-recs").addEventListener("click", () => api().refresh_recordings());
  $("#btn-open-folder").addEventListener("click", async () => {
    const res = await api().open_recordings_folder();
    if (!res.ok) toast(res.error, "err");
  });
  $("#btn-transcribe").addEventListener("click", doTranscribe);
  $("#btn-settings").addEventListener("click", openSettings);
  $("#btn-save-settings").addEventListener("click", saveSettings);
  $("#btn-pick-dir").addEventListener("click", async () => {
    const path = await api().pick_folder();
    if (path) $("#set-recdir").value = path;
  });
  $("#link-portal").addEventListener("click", (e) => {
    e.preventDefault();
    api().open_url("developer_portal");
  });
  $("#btn-consent-yes").addEventListener("click", () => doStartRecording(true));
  $("#btn-consent-no").addEventListener("click", () => doStartRecording(false));
  $("#update-pill").addEventListener("click", openUpdateModal);
  $("#btn-get-update").addEventListener("click", () => {
    api().open_url("latest_release");
    $("#overlay-update").hidden = true;
  });
  $("#btn-check-updates").addEventListener("click", async () => {
    const btn = $("#btn-check-updates");
    btn.disabled = true;
    btn.textContent = "Checking…";
    try {
      const res = await api().check_for_updates();
      if (res.update) {
        ui.update = res.update;
        $("#overlay-settings").hidden = true;
        openUpdateModal();
      } else {
        toast("You're on the latest version.", "ok");
      }
    } finally {
      btn.disabled = false;
      btn.textContent = "Check now";
    }
  });
  $("#btn-close-yes").addEventListener("click", () => api().request_quit());
  $("#btn-close-no").addEventListener("click", () => {
    $("#overlay-close").hidden = true;
  });
  document.querySelectorAll("[data-close]").forEach((btn) => {
    btn.addEventListener("click", () => {
      $("#" + btn.dataset.close).hidden = true;
    });
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      $("#overlay-settings").hidden = true;
      $("#overlay-consent").hidden = true;
      $("#overlay-update").hidden = true;
    }
  });

  // Initial pull: state + log backlog (events only stream changes).
  render(await api().get_state());
  for (const entry of await api().get_log_backlog()) appendLog(entry);

  ui.timerHandle = setInterval(tickCounter, 500);
  // Heal any missed push (worker thread emitted while the page reloaded,
  // etc.) with a slow poll. Events remain the primary update path.
  setInterval(async () => {
    if (api()) render(await api().get_state());
  }, 5000);
}

if (window.pywebview && window.pywebview.api) {
  init();
} else {
  window.addEventListener("pywebviewready", init);
}
