// Voice loop: mic -> /api/stt -> /api/chat -> /api/tts -> speaker.
const $ = (id) => document.getElementById(id);
const state = { session: null, sttOnServer: true, recorder: null, audio: null, busy: false, lang: "en-GB" };

const SILENCE_MS = 1200;   // stop after this much quiet once speech was heard
const MAX_RECORD_MS = 20000;

function setStatus(text) { $("status").textContent = text; }

function addMessage(role, text, extra = "") {
  const li = document.createElement("li");
  li.className = `msg ${role} ${extra}`.trim();
  li.textContent = text;
  $("transcript").append(li);
  li.scrollIntoView({ block: "end", behavior: "smooth" });
  return li;
}

function addEvent(text) {
  const li = document.createElement("li");
  li.className = "event";
  li.textContent = text;
  $("transcript").append(li);
}

async function api(path, body) {
  const res = await fetch(path, {
    method: "POST",
    headers: body instanceof FormData ? {} : { "Content-Type": "application/json" },
    body: body instanceof FormData ? body : JSON.stringify(body ?? {}),
  });
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
  return res;
}

// ---- speaking -------------------------------------------------------------

function stopSpeaking() {
  if (state.audio) { state.audio.pause(); state.audio = null; }
  if (window.speechSynthesis) speechSynthesis.cancel();
}

async function speak(text) {
  stopSpeaking();
  try {
    const blob = await (await api("/api/tts", { text })).blob();
    const audio = new Audio(URL.createObjectURL(blob));
    state.audio = audio;
    setStatus("Speaking… tap the mic to interrupt");
    await audio.play();
    await new Promise((resolve) => { audio.onended = resolve; audio.onpause = resolve; });
  } catch (err) {
    console.warn("Server TTS unavailable, using the browser voice:", err);
    await browserSpeak(text);
  } finally {
    if (state.audio) state.audio = null;
    if (!state.busy && !state.recorder) setStatus("Tap the mic or hold Space to talk");
  }
}

function browserSpeak(text) {
  if (!window.speechSynthesis) return Promise.resolve();
  return new Promise((resolve) => {
    const u = new SpeechSynthesisUtterance(text);
    u.lang = state.lang;
    u.onend = u.onerror = resolve;
    speechSynthesis.speak(u);
  });
}

// ---- conversation ---------------------------------------------------------

async function send(text) {
  text = text.trim();
  if (!text) return;
  addMessage("user", text);
  state.busy = true;
  $("mic").disabled = true;
  setStatus("Thinking…");
  const pending = addMessage("assistant", "…", "pending");
  try {
    const data = await (await api("/api/chat", { session_id: state.session, text })).json();
    pending.classList.remove("pending");
    pending.textContent = data.reply;
    for (const ev of data.events) describeEvent(ev);
    if (data.events.length) refreshDesk(true);
    state.busy = false;
    $("mic").disabled = false;
    await speak(data.reply);
  } catch (err) {
    pending.classList.remove("pending");
    pending.textContent = `Error: ${err.message}`;
  } finally {
    state.busy = false;
    $("mic").disabled = false;
    if (!state.audio && !state.recorder) setStatus("Tap the mic or hold Space to talk");
  }
}

function describeEvent(ev) {
  if (ev.type === "booked") addEvent(`Booked · ${ev.appointment.start.replace("T", " ")} · ref ${ev.appointment.reference}`);
  if (ev.type === "cancelled") addEvent(`Cancelled · ref ${ev.appointment.reference}`);
  if (ev.type === "message") addEvent(`Callback request logged for ${ev.message.name}`);
}

// ---- listening ------------------------------------------------------------

async function startRecording() {
  stopSpeaking();
  if (!state.sttOnServer) return browserRecognition();
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
  } catch {
    setStatus("Microphone blocked. Allow it in the browser, or type below.");
    return;
  }
  const mime = MediaRecorder.isTypeSupported("audio/webm;codecs=opus") ? "audio/webm;codecs=opus" : "";
  const recorder = new MediaRecorder(stream, mime ? { mimeType: mime } : {});
  const chunks = [];
  state.recorder = recorder;
  recorder.ondataavailable = (e) => e.data.size && chunks.push(e.data);

  // Silence detection, so the caller does not have to press stop.
  const ctx = new AudioContext();
  const analyser = ctx.createAnalyser();
  analyser.fftSize = 1024;
  ctx.createMediaStreamSource(stream).connect(analyser);
  const samples = new Float32Array(analyser.fftSize);
  let heard = false, quietSince = performance.now();
  const started = performance.now();
  const tick = () => {
    if (recorder.state !== "recording") return;
    analyser.getFloatTimeDomainData(samples);
    const rms = Math.sqrt(samples.reduce((s, v) => s + v * v, 0) / samples.length);
    $("ring").style.transform = `scale(${1 + Math.min(rms * 8, 0.45)})`;
    const now = performance.now();
    if (rms > 0.02) { heard = true; quietSince = now; }
    if ((heard && now - quietSince > SILENCE_MS) || now - started > MAX_RECORD_MS) stopRecording();
    else requestAnimationFrame(tick);
  };

  recorder.onstop = async () => {
    stream.getTracks().forEach((t) => t.stop());
    ctx.close();
    $("mic").classList.remove("recording");
    $("ring").style.transform = "";
    state.recorder = null;
    if (!heard) { setStatus("Didn't catch anything. Tap the mic to try again."); return; }
    setStatus("Transcribing…");
    $("mic").disabled = true;
    try {
      const form = new FormData();
      form.append("audio", new Blob(chunks, { type: recorder.mimeType || "audio/webm" }), "speech.webm");
      const { text } = await (await api("/api/stt", form)).json();
      $("mic").disabled = false;
      if (text) await send(text);
      else setStatus("Didn't catch that. Tap the mic to try again.");
    } catch (err) {
      $("mic").disabled = false;
      setStatus(`Speech-to-text failed: ${err.message}`);
    }
  };

  recorder.start(250);
  $("mic").classList.add("recording");
  setStatus("Listening…");
  requestAnimationFrame(tick);
}

function stopRecording() {
  if (state.recorder && state.recorder.state === "recording") state.recorder.stop();
}

// Fallback when the server has no faster-whisper: the browser's own recognizer (Chrome/Edge).
function browserRecognition() {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR) { setStatus("No speech-to-text available here. Type below instead."); return; }
  const rec = new SR();
  rec.lang = state.lang;
  rec.onresult = (e) => send(e.results[0][0].transcript);
  rec.onend = () => { $("mic").classList.remove("recording"); state.recorder = null; };
  state.recorder = { state: "recording", stop: () => rec.stop() };
  $("mic").classList.add("recording");
  setStatus("Listening…");
  rec.start();
}

function toggleMic() {
  if (state.busy) return;
  if (state.recorder) stopRecording();
  else startRecording();
}

// ---- front desk panel -----------------------------------------------------

function card(html, fresh) {
  const li = document.createElement("li");
  li.innerHTML = html;
  if (fresh) li.classList.add("fresh");
  return li;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function refreshDesk(highlight = false) {
  const [appts, msgs] = await Promise.all([
    fetch("/api/appointments").then((r) => r.json()),
    fetch("/api/messages").then((r) => r.json()),
  ]);
  const fmt = (iso) => new Date(iso).toLocaleString(undefined, { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
  const newest = appts.reduce((a, b) => (!a || b.created_at > a.created_at ? b : a), null);
  $("appointments").replaceChildren(...(appts.length ? appts.map((a) => card(
    `<strong>${escapeHtml(a.name)}</strong>${escapeHtml(a.label)}<br><span class="meta">${fmt(a.start)} · <span class="ref">${a.reference}</span></span>`,
    highlight && a === newest)) : [card("None yet")]));
  $("messages").replaceChildren(...(msgs.length ? msgs.map((m, i) => card(
    `<strong>${escapeHtml(m.name)} · ${escapeHtml(m.phone)}</strong>${escapeHtml(m.message)}`, highlight && i === 0)) : [card("None yet")]));
  for (const id of ["appointments", "messages"]) {
    const only = $(id).children[0];
    if ($(id).children.length === 1 && only.textContent === "None yet") only.className = "empty";
  }
}

// ---- start ----------------------------------------------------------------

async function startCall() {
  $("start").disabled = true;
  try {
    const s = await (await api("/api/session")).json();
    state.session = s.session_id;
    state.sttOnServer = s.stt;
    state.lang = s.language === "sl" ? "sl-SI" : "en-GB";
    $("assistant-name").textContent = s.assistant;
    $("business").textContent = s.business;
    $("mode").textContent = s.mode === "claude" ? `Claude · ${s.model}` : "Offline demo brain";
    if (s.language === "sl") $("mode").textContent += " · slovenščina";
    $("mode").classList.toggle("offline", s.mode !== "claude");
    $("start").classList.add("hidden");
    $("talk").classList.remove("hidden");
    $("type-input").disabled = $("type-send").disabled = false;
    addMessage("assistant", s.greeting);
    await speak(s.greeting);
  } catch (err) {
    $("start").disabled = false;
    setStatus(`Could not start: ${err.message}`);
  }
}

$("start").addEventListener("click", startCall);
$("mic").addEventListener("click", toggleMic);
$("type-form").addEventListener("submit", (e) => {
  e.preventDefault();
  if (state.busy) return;  // keep the text until the current reply is in
  const text = $("type-input").value;
  $("type-input").value = "";
  stopSpeaking();
  send(text);
});
// Hold Space to talk (when not typing).
document.addEventListener("keydown", (e) => {
  if (e.code !== "Space" || e.repeat || !state.session || document.activeElement === $("type-input")) return;
  e.preventDefault();
  if (!state.recorder && !state.busy) startRecording();
});
document.addEventListener("keyup", (e) => {
  if (e.code === "Space" && document.activeElement !== $("type-input")) stopRecording();
});

refreshDesk();
$("mode").textContent = "Not connected";
