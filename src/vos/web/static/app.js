/* VOS kitchen kiosk.
   Plain browser JS, no build step. State machine:
   idle → recording → transcribing → confirming → sending → idle
   The confirm step is the privacy contract made visible: nothing is saved or sent
   until a person has read what Whisper heard and pressed the button. */

"use strict";

const $ = (id) => document.getElementById(id);

const feed = $("feed");
const micBtn = $("mic");
const confirmBox = $("confirm");
const confirmText = $("confirm-text");
const typeInput = $("type-input");
const statusDot = $("status-dot");

// One identity per browser session; gone when the tab's session ends, like the chat
// history on the server. The PIN also lives only in sessionStorage.
const sessionId =
  sessionStorage.getItem("vos-session") ||
  (() => {
    const v = crypto.randomUUID();
    sessionStorage.setItem("vos-session", v);
    return v;
  })();

let mode = "capture"; // "capture" | "ask" | "shopping" | "doctor"
let recorder = null;
let chunks = [];
let pendingTranscript = null; // raw whisper text while the confirm card is open

/* The staples. A hidden card IS a pending list item; it reappears once the item
   is marked bought (on Telegram). Order: fruit, drinks, staples, household, fresh. */
const CARD_ITEMS = [
  ["🍌", "Banana"], ["🍇", "Grapes"], ["🍓", "Strawberry"], ["🥝", "Kiwi"],
  ["🍑", "Peaches"], ["🍎", "Apple"], ["🍊", "Orange"],
  ["🥛", "Oat milk"], ["🧃", "Juice"], ["💧", "Sprudel"],
  ["🍚", "Rice"], ["🧻", "Kitchen tissue"], ["🚽", "Toilet paper"],
  ["🧼", "Soap"], ["🧴", "Kitchen soap"], ["🫧", "Dishwasher pills"],
  ["🥔", "Potato"], ["🍗", "Chicken"], ["🐟", "Fish"],
  ["🥕", "Carrot"], ["🥬", "Spinach"], ["🍅", "Tomato"], ["🥚", "Egg"],
];

let pendingSet = new Set(); // canonical names currently on the list
let shoppingTimer = null;
let doctorTimer = null;

const canon = (name) => name.trim().toLowerCase().replace(/\s+/g, " ");

/* --- helpers -------------------------------------------------------------- */

function pinHeaders() {
  const pin = sessionStorage.getItem("vos-pin");
  return pin ? { "X-VOS-PIN": pin } : {};
}

async function api(path, options = {}) {
  const resp = await fetch(path, {
    ...options,
    headers: { ...(options.headers || {}), ...pinHeaders() },
  });
  if (resp.status === 401) {
    showPin("That PIN stopped working — enter it again.");
    throw new Error("pin");
  }
  return resp;
}

function bubble(text, who, extra = {}) {
  const el = document.createElement("div");
  el.className = `bubble from-${who}`;
  if (extra.error) el.classList.add("is-error");
  if (extra.thinking) el.classList.add("is-thinking");
  el.textContent = text;
  if (extra.meta) {
    const meta = document.createElement("span");
    meta.className = "meta";
    meta.textContent = extra.meta;
    el.appendChild(meta);
  }
  feed.appendChild(el);
  feed.scrollTop = feed.scrollHeight;
  return el;
}

function setMode(next) {
  mode = next;
  for (const m of ["capture", "ask", "shopping", "doctor"]) {
    $(`mode-${m}`).classList.toggle("is-active", next === m);
    $(`mode-${m}`).setAttribute("aria-selected", String(next === m));
  }

  const shopping = next === "shopping";
  const doctor = next === "doctor";
  // Both panels replace the feed and hide the dock: neither takes typed input.
  const panel = shopping || doctor;
  $("cards").classList.toggle("hidden", !shopping);
  $("doctor").classList.toggle("hidden", !doctor);
  feed.classList.toggle("hidden", panel);
  document.querySelector(".dock").classList.toggle("hidden", panel);
  if (panel) closeConfirm();

  if (shopping) {
    refreshShopping();
    // The tablet sits on this tab; bought items must reappear without a touch.
    shoppingTimer = shoppingTimer || setInterval(refreshShopping, 45000);
  } else if (shoppingTimer) {
    clearInterval(shoppingTimer);
    shoppingTimer = null;
  }

  if (doctor) {
    refreshDoctor();
    // Slower than shopping: this hits somebody else's server, and a slot that
    // appears is worth knowing about within a minute, not within a second.
    doctorTimer = doctorTimer || setInterval(refreshDoctor, 120000);
  } else if (doctorTimer) {
    clearInterval(doctorTimer);
    doctorTimer = null;
  }

  $("confirm-send").textContent = next === "capture" ? "Save it" : "Ask";
  typeInput.placeholder =
    next === "capture" ? "…or type a thought to save" : "…or type a question";
}

/* --- doctor slots ----------------------------------------------------------- */

function renderSlots(slots) {
  const list = $("doctor-list");
  list.textContent = "";
  if (!slots.length) {
    const empty = document.createElement("p");
    empty.className = "slots-empty";
    empty.textContent = "Nothing free in the next two weeks.";
    list.appendChild(empty);
    return;
  }
  let currentDay = null;
  let group = null;
  for (const iso of slots) {
    const when = new Date(iso);
    // Grouped by day because the decision is "which day", then "what time" —
    // and a flat column of twenty timestamps is unreadable across a kitchen.
    const day = when.toLocaleDateString(undefined, {
      weekday: "short", day: "numeric", month: "short",
    });
    if (day !== currentDay) {
      const heading = document.createElement("h2");
      heading.className = "slots-day";
      heading.textContent = day;
      list.appendChild(heading);
      group = document.createElement("div");
      group.className = "slots-row";
      list.appendChild(group);
      currentDay = day;
    }
    const chip = document.createElement("span");
    chip.className = "slot";
    chip.textContent = when.toLocaleTimeString(undefined, {
      hour: "2-digit", minute: "2-digit",
    });
    group.appendChild(chip);
  }
}

async function refreshDoctor() {
  const list = $("doctor-list");
  try {
    const resp = await api("/api/doctor");
    if (resp.status === 503) {
      list.textContent = "";
      const off = document.createElement("p");
      off.className = "slots-empty";
      off.textContent = "Doctolib isn't set up on this VOS.";
      list.appendChild(off);
      return;
    }
    if (!resp.ok) return; // transient; the next refresh catches up
    const data = await resp.json();
    if (data.ok === false) {
      list.textContent = "";
      const err = document.createElement("p");
      err.className = "slots-empty";
      err.textContent = data.detail || "Couldn't reach Doctolib.";
      list.appendChild(err);
      return;
    }
    renderSlots(data.slots || []);
  } catch {
    /* transient — the next refresh catches up */
  }
}

/* --- shopping cards --------------------------------------------------------- */

function renderCards() {
  const grid = $("cards-grid");
  grid.textContent = "";
  for (const [emoji, name] of CARD_ITEMS) {
    const btn = document.createElement("button");
    btn.className = "card";
    btn.dataset.name = name;
    if (pendingSet.has(canon(name))) btn.classList.add("is-gone");
    const glyph = document.createElement("span");
    glyph.className = "card-emoji";
    glyph.textContent = emoji;
    const label = document.createElement("span");
    label.className = "card-label";
    label.textContent = name;
    btn.append(glyph, label);
    btn.addEventListener("click", () => addCard(btn, name));
    grid.appendChild(btn);
  }
}

function applyPending(names) {
  pendingSet = new Set(names.map(canon));
  for (const btn of document.querySelectorAll(".card")) {
    btn.classList.toggle("is-gone", pendingSet.has(canon(btn.dataset.name)));
  }
}

async function refreshShopping() {
  try {
    const resp = await api("/api/shopping");
    if (!resp.ok) return;
    const { pending } = await resp.json();
    applyPending(pending);
  } catch {
    /* transient — the next refresh catches up */
  }
}

async function addCard(btn, name) {
  btn.classList.add("is-vanishing"); // optimistic; confirmed or reverted below
  try {
    const resp = await api("/api/shopping/add", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, client_id: crypto.randomUUID() }),
    });
    const body = await resp.json();
    if (!resp.ok || body.saved === false) throw new Error("save failed");
    applyPending(body.pending);
  } catch (err) {
    if (err.message !== "pin") btn.classList.remove("is-gone");
  } finally {
    btn.classList.remove("is-vanishing");
  }
}

/* --- recording -------------------------------------------------------------- */

async function startRecording() {
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch {
    bubble(
      "I can't use the microphone. Check Chrome's mic permission for this page.",
      "bot",
      { error: true }
    );
    return;
  }
  chunks = [];
  recorder = new MediaRecorder(stream, { mimeType: "audio/webm" });
  recorder.ondataavailable = (e) => e.data.size && chunks.push(e.data);
  recorder.onstop = () => {
    stream.getTracks().forEach((t) => t.stop());
    transcribe(new Blob(chunks, { type: "audio/webm" }));
  };
  recorder.start();
  micBtn.classList.add("is-recording");
  micBtn.setAttribute("aria-label", "Recording — tap to finish");
}

function stopRecording() {
  micBtn.classList.remove("is-recording");
  micBtn.setAttribute("aria-label", "Hold a thought — tap to talk");
  if (recorder && recorder.state !== "inactive") recorder.stop();
  recorder = null;
}

async function transcribe(blob) {
  micBtn.classList.add("is-busy");
  const note = bubble("Listening back…", "bot", { thinking: true });
  try {
    const form = new FormData();
    form.append("audio", blob, "clip.webm");
    const resp = await api("/api/transcribe", { method: "POST", body: form });
    if (!resp.ok) throw new Error(String(resp.status));
    const { transcript } = await resp.json();
    note.remove();
    if (!transcript.trim()) {
      bubble("I couldn't hear anything in that — try again a little closer.", "bot");
      return;
    }
    openConfirm(transcript);
  } catch (err) {
    note.remove();
    if (err.message !== "pin") {
      bubble("Transcription failed — give it another try.", "bot", { error: true });
    }
  } finally {
    micBtn.classList.remove("is-busy");
  }
}

/* --- confirm card ------------------------------------------------------------- */

function openConfirm(transcript) {
  pendingTranscript = transcript;
  confirmText.value = transcript;
  confirmBox.classList.remove("hidden");
  confirmText.focus();
  confirmText.setSelectionRange(transcript.length, transcript.length);
}

function closeConfirm() {
  pendingTranscript = null;
  confirmBox.classList.add("hidden");
  confirmText.value = "";
}

/* --- sending ---------------------------------------------------------------------- */

async function send(text, { source, transcript }) {
  bubble(text, "user");
  if (mode === "capture") return capture(text, { source, transcript });
  return ask(text);
}

async function capture(text, { source, transcript }) {
  const note = bubble("Saving…", "bot", { thinking: true });
  try {
    const resp = await api("/api/capture", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        text,
        client_id: crypto.randomUUID(),
        source,
        transcript,
      }),
    });
    const body = await resp.json();
    note.remove();
    if (!resp.ok || body.saved === false) {
      bubble("Not saved — the journal write failed. Please send it again.", "bot", {
        error: true,
      });
      return;
    }
    if (body.status === "classified") {
      let meta = `Filed under ${body.category}`;
      if (body.items && body.items.length) {
        meta += ` · on the shopping list: ${body.items.join(", ")}`;
      }
      bubble(`Saved: ${body.title}`, "bot", { meta });
    } else if (body.status === "pending") {
      bubble("Saved. Filing takes a moment — it'll appear under pending.", "bot");
    } else {
      bubble("Saved. Filing is deferred for now — it's safe in the journal.", "bot", {
        meta: body.error || "",
      });
    }
  } catch (err) {
    note.remove();
    if (err.message !== "pin") {
      bubble("Not saved — I couldn't reach VOS. Please send it again.", "bot", {
        error: true,
      });
    }
  }
}

async function ask(text) {
  const note = bubble("Thinking…", "bot", { thinking: true });
  try {
    const resp = await api("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId, message: text }),
    });
    if (!resp.ok) throw new Error(String(resp.status));
    const { reply } = await resp.json();
    note.remove();
    bubble(reply, "bot");
  } catch (err) {
    note.remove();
    if (err.message !== "pin") {
      bubble("I couldn't reach VOS just now — try again.", "bot", { error: true });
    }
  }
}

/* --- PIN ------------------------------------------------------------------------------ */

function showPin(message = "") {
  $("pin-error").textContent = message;
  $("pin-overlay").classList.remove("hidden");
  $("pin-input").focus();
}

async function checkHealth() {
  try {
    const resp = await fetch("/api/health");
    const body = await resp.json();
    statusDot.classList.add("is-ok");
    if (body.pin_required && !sessionStorage.getItem("vos-pin")) showPin();
  } catch {
    statusDot.classList.add("is-down");
    bubble("VOS isn't reachable right now.", "bot", { error: true });
  }
}

/* --- wiring -------------------------------------------------------------------------- */

micBtn.addEventListener("click", () => {
  if (micBtn.classList.contains("is-recording")) stopRecording();
  else startRecording();
});

$("confirm-send").addEventListener("click", () => {
  const text = confirmText.value.trim();
  const raw = pendingTranscript;
  closeConfirm();
  if (text) send(text, { source: "voice", transcript: raw });
});

$("confirm-discard").addEventListener("click", closeConfirm);

$("type-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const text = typeInput.value.trim();
  typeInput.value = "";
  if (text) send(text, { source: "text", transcript: null });
});

$("mode-capture").addEventListener("click", () => setMode("capture"));
$("mode-ask").addEventListener("click", () => setMode("ask"));
$("mode-shopping").addEventListener("click", () => setMode("shopping"));
$("mode-doctor").addEventListener("click", () => setMode("doctor"));

$("pin-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const pin = $("pin-input").value.trim();
  if (!pin) return;
  const resp = await fetch("/api/ping", { headers: { "X-VOS-PIN": pin } });
  if (resp.ok) {
    sessionStorage.setItem("vos-pin", pin);
    $("pin-overlay").classList.add("hidden");
    $("pin-input").value = "";
  } else {
    $("pin-error").textContent = "That's not it — try again.";
    $("pin-input").value = "";
    $("pin-input").focus();
  }
});

renderCards();
setMode("capture");
checkHealth();
bubble(
  "Tap the dial and say a thought. I'll show you what I heard so you can fix it " +
    "before anything is saved. Switch to Ask to search what the family knows.",
  "bot"
);
