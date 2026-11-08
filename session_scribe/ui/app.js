"use strict";

const $ = (id) => document.getElementById(id);
let api = null;
let last = null;
let settingsLoaded = false;

function fmtTime(sec) {
  sec = Math.floor(sec || 0);
  const h = String(Math.floor(sec / 3600)).padStart(2, "0");
  const m = String(Math.floor((sec % 3600) / 60)).padStart(2, "0");
  const s = String(sec % 60).padStart(2, "0");
  return `${h}:${m}:${s}`;
}

function fmtDuration(sec) {
  sec = Math.floor(sec || 0);
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
  return h ? `${h}h ${String(m).padStart(2, "0")}m` : `${m}m`;
}

// Like node.replaceChildren, but skips null/undefined instead of printing "null".
function fill(node, ...children) {
  node.replaceChildren(...children.filter((c) => c != null && c !== false));
}

function el(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (v !== false && v != null) e.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children) if (c != null) e.append(c);
  return e;
}

// ------------------------------------------------------------- state -----

async function refresh() {
  try {
    const s = await api.state();
    last = s;
    renderDiscord(s);
    renderRecorder(s);
    renderPeople(s);
    renderJobs(s);
    if (!settingsLoaded) loadSettings(s.settings);
  } catch (e) {
    console.error(e);
  }
}

function renderDiscord(s) {
  const d = s.discord;
  const dot = $("discord-dot");
  dot.className = "dot " + (d.status === "connected" ? "ok"
    : d.status.startsWith("error") ? "bad"
    : d.status === "not connected" ? "" : "warn");
  let text = d.status;
  if (d.status === "connected") text = `connected as ${d.user}`;
  $("discord-status").textContent = text;
  const active = d.status !== "not connected" && !d.status.startsWith("add your");
  $("btn-connect").textContent = active ? "Disconnect" : "Connect to Discord";
  $("btn-connect").dataset.active = active ? "1" : "";
  $("channel-name").textContent = d.channel ? `(${d.channel})` : "";
}

function renderRecorder(s) {
  const r = s.recording;
  $("timer").textContent = fmtTime(r.elapsed);
  const st = $("rec-state");
  st.className = "state " + r.state;
  st.textContent = { idle: "Not recording", recording: "Recording", paused: "Paused (not recording)" }[r.state];
  $("btn-record").hidden = r.state !== "idle";
  $("btn-pause").hidden = r.state !== "recording";
  $("btn-resume").hidden = r.state !== "paused";
  $("btn-stop").hidden = r.state === "idle";
  $("campaign").disabled = r.state !== "idle";
  $("title").disabled = r.state !== "idle";
  for (const name of ["discord", "mic"]) {
    $("lvl-" + name).style.width = `${Math.round((r.levels[name] || 0) * 100)}%`;
  }
  const err = $("rec-error");
  err.hidden = !r.error;
  err.textContent = r.error || "";

  if (document.activeElement !== $("campaign")) $("campaign").value = s.campaign;
  const list = $("campaign-list");
  list.replaceChildren(...s.campaigns.map((c) => el("option", { value: c })));
}

const rows = new Map();

function renderPeople(s) {
  const tbody = document.querySelector("#people tbody");
  const seen = new Set();
  for (const p of s.participants) {
    seen.add(p.id);
    let row = rows.get(p.id);
    if (!row) {
      const input = el("input", { type: "text", placeholder: p.is_self ? "GM" : "Character name" });
      input.addEventListener("change", () => api.set_character(p.id, input.value));
      const box = el("input", { type: "checkbox" });
      box.addEventListener("change", () => api.set_ignored(p.id, box.checked));
      const nameCell = el("td");
      row = { tr: el("tr", {}, el("td", {}, el("span", { class: "dot" })), nameCell, el("td", {}, input), el("td", {}, box)),
              input, box, nameCell };
      rows.set(p.id, row);
    }
    fill(row.nameCell, p.name,
      p.is_self ? el("span", { class: "tag" }, "you") : null,
      p.is_bot ? el("span", { class: "tag" }, "bot") : null);
    if (document.activeElement !== row.input) row.input.value = p.character || "";
    row.box.checked = p.ignored;
    row.tr.className = (p.speaking ? "speaking " : "") + (p.ignored ? "ignored" : "");
    tbody.append(row.tr); // keeps order in sync with the server's sort
  }
  for (const [id, row] of rows) {
    if (!seen.has(id)) { row.tr.remove(); rows.delete(id); }
  }
  $("people-empty").hidden = s.participants.length > 0;
}

// ----------------------------------------------------------- sessions ----

async function loadSessions() {
  const list = await api.sessions();
  const box = $("sessions");
  if (!list.length) {
    box.replaceChildren(el("p", { class: "muted" }, "No sessions recorded yet."));
    return;
  }
  box.replaceChildren(...list.map(sessionRow));
  if (last) renderJobs(last);
}

function sessionRow(s) {
  const title = s.title || `${s.campaign}`;
  const status = {
    recording: "recording now",
    recorded: "not transcribed yet",
    transcribed: "transcribed",
    interrupted: "interrupted (app closed during recording, audio kept)",
    failed: "failed to start",
  }[s.status] || s.status;
  const actions = el("div", { class: "actions" });
  if (s.has_transcript) {
    actions.append(el("button", { onclick: () => api.open_path(s.dir + "\\transcript.md") }, "Open transcript"));
    actions.append(el("button", { class: "secondary", title: "Rebuild names/characters after changing tags in session.json",
      onclick: async () => { await api.relabel(s.dir); loadSessions(); } }, "Relabel"));
  }
  if (s.status !== "recording" && s.status !== "failed") {
    actions.append(el("button", { class: s.has_transcript ? "secondary" : "",
      onclick: () => { api.transcribe(s.dir, s.has_transcript); setTimeout(refresh, 200); } },
      s.has_transcript ? "Re-transcribe" : "Transcribe"));
  }
  actions.append(el("button", { class: "ghost", onclick: () => api.open_path(s.dir) }, "Folder"));
  if (s.status !== "recording") {
    actions.append(el("button", { class: "danger", onclick: () => deleteSession(s, title) }, "Delete"));
  }
  const div = el("div", { class: "session", "data-dir": s.dir },
    el("div", {}, el("strong", {}, title)),
    actions,
    el("div", { class: "meta" }, `${(s.started_at || "").replace("T", " ")} · ${fmtDuration(s.duration)} · ${status}`),
    el("div", { class: "progress", hidden: true }));
  return div;
}

async function deleteSession(s, title) {
  const when = (s.started_at || "").replace("T", " ");
  const ok = confirm(`Delete "${title}" (${when})?\n\n` +
    "The recordings and transcript go to the Recycle Bin. " +
    "Empty the Recycle Bin to free up the space.");
  if (!ok) return;
  const error = await api.delete_session(s.dir);
  if (error) alert(error);
  loadSessions();
}

function renderJobs(s) {
  for (const div of document.querySelectorAll(".session")) {
    const job = s.jobs[div.dataset.dir];
    const prog = div.querySelector(".progress");
    if (!job || job.status === "done" || job.status === "cancelled") {
      if (!prog.hidden && job && job.status === "done") loadSessions();
      prog.hidden = true;
      continue;
    }
    prog.hidden = false;
    const pct = Math.round(job.progress * 100);
    fill(prog,
      el("div", {}, el("i", { style: `width:${pct}%` })),
      `${job.status === "error" ? "Error: " : ""}${job.message} ${job.status === "error" ? "" : `(${pct}%)`}`,
      job.status === "running" || job.status === "queued"
        ? el("button", { class: "ghost", onclick: () => api.cancel_transcription(div.dataset.dir) }, "Cancel")
        : null);
  }
}

// ----------------------------------------------------------- settings ----

function loadSettings(values) {
  const form = $("settings-form");
  for (const [k, v] of Object.entries(values)) {
    const input = form.elements[k];
    if (!input) continue;
    if (input.type === "checkbox") input.checked = !!v;
    else input.value = v ?? "";
  }
  settingsLoaded = true;
}

async function fillDeviceLists(settings) {
  const devices = await api.audio_devices();
  const fillSelect = (name, names, current, defaultLabel) => {
    const select = $("settings-form").elements[name];
    const options = [el("option", { value: "" }, defaultLabel)];
    for (const n of names) options.push(el("option", { value: n }, n));
    // Keep a saved device visible even if it's unplugged right now.
    if (current && !names.includes(current)) {
      options.push(el("option", { value: current }, `${current} (not connected)`));
    }
    select.replaceChildren(...options);
  };
  fillSelect("mic_device", devices.mics, settings.mic_device, "Windows default microphone");
  fillSelect("output_device", devices.outputs.map((n) => n), settings.output_device, "Windows default output");
}

function readSettings() {
  const form = $("settings-form");
  const out = {};
  for (const input of form.elements) {
    if (!input.name) continue;
    out[input.name] = input.type === "checkbox" ? input.checked : input.value.trim();
  }
  return out;
}

// ------------------------------------------------------------- wiring ----

function wire() {
  $("btn-connect").addEventListener("click", async () => {
    if ($("btn-connect").dataset.active) await api.disconnect();
    else await api.connect();
    refresh();
  });
  $("btn-settings").addEventListener("click", async () => {
    await fillDeviceLists(last.settings);
    loadSettings(last.settings);
    $("settings").showModal();
  });
  $("settings").addEventListener("close", async () => {
    if ($("settings").returnValue === "save") {
      await api.save_settings(readSettings());
      loadSessions();
    }
    settingsLoaded = false;
  });
  $("btn-forget").addEventListener("click", async () => { await api.forget_login(); $("settings").close(); });

  $("campaign").addEventListener("change", async () => {
    await api.set_campaign($("campaign").value);
    for (const row of rows.values()) row.input.blur();
    refresh();
  });

  $("btn-record").addEventListener("click", async () => {
    if (last && last.discord.status !== "connected" &&
        !confirm("Discord is not connected, so speakers can't be identified. Record anyway?")) return;
    await api.start($("title").value);
    refresh(); loadSessions();
  });
  $("btn-pause").addEventListener("click", async () => { await api.pause(); refresh(); });
  $("btn-resume").addEventListener("click", async () => { await api.resume(); refresh(); });
  $("btn-stop").addEventListener("click", async () => {
    if (!confirm("End this session? Recording stops and transcription starts.")) return;
    $("btn-stop").disabled = true;
    await api.end_session();
    $("btn-stop").disabled = false;
    $("title").value = "";
    refresh(); loadSessions();
  });
}

window.addEventListener("pywebviewready", () => {
  api = window.pywebview.api;
  wire();
  refresh();
  loadSessions();
  setInterval(refresh, 500);
  setInterval(loadSessions, 15000);
});
