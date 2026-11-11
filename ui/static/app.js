const $ = (sel) => document.querySelector(sel);

let state = null;
let selected = null;
let busy = false;

const COLOURS = (i) => `hsl(${(i * 57 + 202) % 360} 72% 63%)`;

function esc(text) {
  return String(text).replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
}

async function api(path, options = {}) {
  const res = await fetch(path, options);
  let data = {};
  try { data = await res.json(); } catch { /* empty body */ }
  if (!res.ok || data.ok === false) {
    throw new Error(data.error || `${res.status} ${res.statusText}`);
  }
  return data;
}

function toast(message, bad = false) {
  const el = $("#toast");
  el.textContent = message;
  el.classList.toggle("bad", bad);
  el.hidden = false;
  requestAnimationFrame(() => el.classList.add("show"));
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => {
    el.classList.remove("show");
    setTimeout(() => { el.hidden = true; }, 220);
  }, bad ? 5200 : 2600);
}

function flashSaved() {
  const el = $("#saved");
  el.classList.add("show");
  clearTimeout(flashSaved.timer);
  flashSaved.timer = setTimeout(() => el.classList.remove("show"), 1200);
}

function render() {
  if (!state) return;

  const board = $("#board");
  board.innerHTML = "";
  state.rows.forEach((row) => {
    [...row].forEach((ch) => {
      const owner = state.byKey[ch];
      const index = owner ? state.songs.findIndex((s) => s.name === owner.song) : -1;
      const key = document.createElement("div");
      key.className = "key " + (owner ? "filled" : "empty");
      if (selected === ch) key.classList.add("selected");
      key.dataset.key = ch;
      key.draggable = Boolean(owner);
      if (owner) key.style.setProperty("--c", COLOURS(Math.max(0, index)));
      key.innerHTML = `<span class="cap">${ch.toUpperCase()}</span>` +
        (owner
          ? `<span class="stem">${esc(owner.stem)}</span><span class="song">${esc(owner.song)}</span>`
          : `<span class="stem muted">free</span>`);
      board.appendChild(key);
    });
  });

  const list = $("#songs");
  list.innerHTML = "";
  state.songs.forEach((song, i) => {
    const li = document.createElement("li");
    li.className = "song";
    li.style.setProperty("--c", COLOURS(i));
    const chips = song.stems.map((stem) => {
      const key = Object.keys(state.byKey).find(
        (k) => state.byKey[k].song === song.name && state.byKey[k].stem === stem);
      return `<button class="chip" data-key="${key || ""}" ${key ? "" : "disabled"}>` +
        `${esc(stem)}<b>${key ? key.toUpperCase() : "—"}</b></button>`;
    }).join("");
    li.innerHTML = `<div class="song-head"><span class="swatch"></span>` +
      `<b>${esc(song.name)}</b><span class="count">${song.stems.length} stems</span>` +
      `<button class="mini bench" data-bench="${esc(song.name)}" title="Take this song off the board">bench</button></div>` +
      `<div class="chips">${chips}</div>`;
    list.appendChild(li);
  });

  const benchWrap = $("#benched-wrap");
  const benchList = $("#benched");
  benchList.innerHTML = "";
  const benched = state.benched || [];
  benchWrap.hidden = benched.length === 0;
  benched.forEach((song) => {
    const li = document.createElement("li");
    li.className = "song";
    li.innerHTML = `<div class="song-head"><b>${esc(song.name)}</b>` +
      `<span class="count">${song.stems.length} stems</span></div>` +
      `<div class="chips"><button class="mini add-back" data-unbench="${esc(song.name)}">Add to board</button></div>`;
    benchList.appendChild(li);
  });

  list.querySelectorAll(".chip[data-key]").forEach((chip) => {
    chip.addEventListener("click", () => {
      if (!chip.dataset.key) return;
      selected = chip.dataset.key;
      render();
      [...document.querySelectorAll(".key")]
        .find((el) => el.dataset.key === chip.dataset.key)
        ?.scrollIntoView({ block: "nearest", behavior: "smooth" });
    });
  });
  list.querySelectorAll("[data-bench]").forEach((btn) => {
    btn.addEventListener("click", () => bench(btn.dataset.bench));
  });
  benchList.querySelectorAll("[data-unbench]").forEach((btn) => {
    btn.addEventListener("click", () => unbench(btn.dataset.unbench));
  });

  const capacity = $("#capacity");
  const hidden = state.capacity?.hidden || 0;
  capacity.hidden = hidden === 0;
  if (hidden > 0) {
    capacity.textContent = `${hidden} stem${hidden === 1 ? "" : "s"} have no key — ` +
      `all ${state.capacity.keys} keys are taken. Remove a song or map fewer stems.`;
  }

  const engine = $("#engine");
  const on = state.engine.active;
  engine.textContent = on ? "engine on" : `engine ${state.engine.text}`;
  engine.className = "pill " + (on ? "on" : "off");

  const splitter = $("#splitter");
  splitter.textContent = state.splitter.ready
    ? `splitter ready · ${state.splitter.detail}`
    : `splitter unavailable · ${state.splitter.detail}`;
  splitter.className = "splitter" + (state.splitter.ready ? "" : " bad");
}

async function refresh() {
  try {
    state = await api("/api/state");
    render();
  } catch (err) {
    toast(`can't reach the UI server: ${err.message}`, true);
  }
}

async function move(fromKey, toKey) {
  if (!state || fromKey === toKey) return;
  const from = state.byKey[fromKey];
  if (!from) return;
  const to = state.byKey[toKey];
  const layout = { ...state.layout };
  layout[`${from.song}|${from.stem}`] = toKey;
  if (to) layout[`${to.song}|${to.stem}`] = fromKey;
  try {
    const data = await api("/api/layout", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ layout }),
    });
    state.layout = data.layout;
    await refresh();
    flashSaved();
  } catch (err) {
    toast(err.message, true);
  }
}

async function bench(song) {
  try {
    await api("/api/bench", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ song }),
    });
    toast(`“${song}” moved off the board`);
    await refresh();
  } catch (err) {
    toast(err.message, true);
  }
}

async function unbench(song) {
  try {
    await api("/api/unbench", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ song }),
    });
    toast(`“${song}” added back`);
    await refresh();
  } catch (err) {
    toast(err.message, true);
  }
}

// --- board interaction ----------------------------------------------------

$("#board").addEventListener("dragstart", (event) => {
  const key = event.target.closest(".key");
  if (!key || !state?.byKey[key.dataset.key]) { event.preventDefault(); return; }
  event.dataTransfer.setData("text/plain", key.dataset.key);
  event.dataTransfer.effectAllowed = "move";
});

$("#board").addEventListener("dragover", (event) => {
  const key = event.target.closest(".key");
  if (!key) return;
  event.preventDefault();
  document.querySelectorAll(".key.over").forEach((el) => el.classList.remove("over"));
  key.classList.add("over");
});

$("#board").addEventListener("dragleave", (event) => {
  event.target.closest(".key")?.classList.remove("over");
});

$("#board").addEventListener("drop", (event) => {
  event.preventDefault();
  document.querySelectorAll(".key.over").forEach((el) => el.classList.remove("over"));
  const from = event.dataTransfer.getData("text/plain");
  const to = event.target.closest(".key")?.dataset.key;
  if (from && to) move(from, to);
});

$("#board").addEventListener("click", (event) => {
  const key = event.target.closest(".key");
  if (!key) return;
  const ch = key.dataset.key;
  if (selected === null) {
    if (state.byKey[ch]) selected = ch;
  } else if (selected === ch) {
    selected = null;
  } else {
    const from = selected;
    selected = null;
    move(from, ch);
    return;
  }
  render();
});

// --- toolbar --------------------------------------------------------------

$("#restart").addEventListener("click", async () => {
  try {
    await api("/api/restart", { method: "POST" });
    toast("engine restarting");
    setTimeout(refresh, 2200);
  } catch (err) {
    toast(err.message, true);
  }
});

$("#reset").addEventListener("click", async () => {
  if (!confirm("Reset every key to the default song blocks?")) return;
  try {
    await api("/api/layout", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ layout: {} }),
    });
    selected = null;
    await refresh();
    toast("layout reset");
  } catch (err) {
    toast(err.message, true);
  }
});

// --- add song -------------------------------------------------------------

$("#mode").addEventListener("change", () => {
  const mode = $("#mode").value;
  const link = mode === "link";
  $("#files-field").hidden = link;
  $("#url-field").hidden = !link;
  $("#files").required = !link;
  $("#url").required = link;
  $("#files").multiple = mode === "stems";
  $("#add-btn").textContent = link ? "Add from link" : mode === "mix" ? "Add & split" : "Add stems";
});

function resetAddForm() {
  $("#add").reset();
  $("#mode").dispatchEvent(new Event("change"));
}

$("#add").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (busy) return;

  const name = $("#song-name").value.trim();
  const mode = $("#mode").value;
  const files = [...$("#files").files];
  const url = $("#url").value.trim();
  const replace = $("#replace").checked;
  if (!name) return toast("give the song a name", true);
  if (mode === "link") {
    if (!url) return toast("paste a link", true);
  } else {
    if (!files.length) return toast("choose at least one audio file", true);
    if (mode === "mix" && files.length !== 1) return toast("pick a single full mix", true);
  }

  busy = true;
  $("#add-btn").disabled = true;
  try {
    if (mode === "link") {
      toast("fetching the link…");
      const data = await api("/api/split-url", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ song: name, url, replace }),
      });
      watchJob(data.job, name);
    } else if (mode === "mix") {
      toast("uploading the mix…");
      const up = await api(`/api/temp/${encodeURIComponent(files[0].name)}`, {
        method: "PUT", body: files[0],
      });
      const data = await api("/api/split", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ song: name, token: up.token, replace }),
      });
      watchJob(data.job, name);
    } else {
      await api("/api/song", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, replace }),
      });
      for (const file of files) {
        await api(`/api/stem/${encodeURIComponent(name)}/${encodeURIComponent(file.name)}`, {
          method: "PUT", body: file,
        });
      }
      toast("stems added — engine restarting");
      resetAddForm();
      setTimeout(refresh, 2500);
    }
  } catch (err) {
    toast(err.message, true);
  } finally {
    busy = false;
    $("#add-btn").disabled = false;
  }
});

async function watchJob(id, name) {
  const box = $("#job");
  box.hidden = false;
  $("#job-title").textContent = `${name} — splitting`;
  $("#job-bar").style.width = "0%";
  $("#job-log").textContent = "";

  for (;;) {
    let job;
    try {
      job = await api(`/api/jobs/${id}`);
    } catch (err) {
      toast(err.message, true);
      return;
    }
    $("#job-title").textContent = `${name} — ${job.state}`;
    $("#job-stage").textContent = job.stage || "";
    $("#job-bar").style.width = `${Math.round((job.progress || 0) * 100)}%`;
    $("#job-log").textContent = (job.log || []).slice(-16).join("\n");
    $("#job-log").scrollTop = $("#job-log").scrollHeight;

    if (job.state === "done") {
      toast("stems added — engine restarting");
      resetAddForm();
      setTimeout(refresh, 2500);
      return;
    }
    if (job.state === "error") {
      toast(job.error || "split failed", true);
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 800));
  }
}

// --- player --------------------------------------------------------------

function fmtTime(seconds) {
  const total = Math.max(0, Math.round(seconds || 0));
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
}

function renderNow(now) {
  const active = Boolean(now && now.active);
  $("#now-song").textContent = active ? now.song : "idle";
  $("#now-song").classList.toggle("idle", !active);
  $("#now-time").textContent = active
    ? `${fmtTime(now.position)} / ${fmtTime(now.duration)}` : "";
  $("#now-bar").style.width = active && now.duration > 0
    ? `${Math.min(100, (now.position / now.duration) * 100)}%` : "0%";
  const box = $("#now-stems");
  box.innerHTML = "";
  for (const stem of (active ? now.stems || [] : [])) {
    const chip = document.createElement("span");
    chip.className = "now-stem" + (stem.on ? " on" : "");
    chip.textContent = stem.name;
    box.appendChild(chip);
  }
}

async function refreshNow() {
  try { renderNow(await api("/api/now")); } catch { /* engine restarting */ }
}

// --- boot -----------------------------------------------------------------

refresh();
setInterval(refresh, 5000);
refreshNow();
setInterval(refreshNow, 800);
