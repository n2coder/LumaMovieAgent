const queryInput = document.getElementById("queryInput");
const sendBtn = document.getElementById("sendBtn");
const voiceBtn = document.getElementById("voiceBtn");
const muteBtn = document.getElementById("muteBtn");
const statusEl = document.getElementById("status");
const topMoviesRow = document.getElementById("topMoviesRow");
const recommendedSection = document.getElementById("recommendedSection");
const recommendedRow = document.getElementById("recommendedRow");
const discoverGrid = document.getElementById("discoverGrid");
const genreChips = document.getElementById("genreChips");
const posterTrack = document.getElementById("posterTrack");
const convUserText = document.getElementById("convUserText");
const convAgentText = document.getElementById("convAgentText");
const audioPlayer = document.getElementById("audioPlayer");
const tabHome = document.getElementById("tabHome");
const tabDiscover = document.getElementById("tabDiscover");
const homePage = document.getElementById("homePage");
const discoverPage = document.getElementById("discoverPage");
const PAGE_API_KEY = new URLSearchParams(window.location.search).get("api_key") || "";

const PLATFORM_VAD_CONFIG = {
  desktop: {
    silenceMs: 700,
    vadIntervalMs: 120,
    bargeHoldMs: 85,
    bargeStrongHoldMs: 150,
    bargeMinAudioMs: 180,
    energyBase: 0.0032,
    calibrationMs: 700,
    calibrationMultiplier: 1.3,
    echoSuppressMs: 320,
    bargeDynamicMultiplier: 1.2,
    bargeAssistantFloorMultiplier: 1.35,
    bargeStrongMultiplier: 1.08,
  },
  android: {
    silenceMs: 820,
    vadIntervalMs: 110,
    bargeHoldMs: 95,
    bargeStrongHoldMs: 170,
    bargeMinAudioMs: 220,
    energyBase: 0.0028,
    calibrationMs: 900,
    calibrationMultiplier: 1.45,
    echoSuppressMs: 380,
    bargeDynamicMultiplier: 1.28,
    bargeAssistantFloorMultiplier: 1.42,
    bargeStrongMultiplier: 1.1,
  },
  ios: {
    silenceMs: 900,
    vadIntervalMs: 110,
    bargeHoldMs: 105,
    bargeStrongHoldMs: 190,
    bargeMinAudioMs: 260,
    energyBase: 0.0024,
    calibrationMs: 1000,
    calibrationMultiplier: 1.55,
    echoSuppressMs: 460,
    bargeDynamicMultiplier: 1.35,
    bargeAssistantFloorMultiplier: 1.5,
    bargeStrongMultiplier: 1.12,
  },
};
const MIC_IDLE_TIMEOUT_MS = 30000;
const TOP_POOL_LIMIT = 50;
const TOP_VISIBLE_COUNT = 18;
const TOP_ROTATE_MS = 18000;
const RECOGNITION_LANGS = ["hi-IN", "en-IN"];
const MIN_AUTO_QUERY_WORDS = 1;
const MIN_AUTO_QUERY_CHARS = 3;

const detectVadProfile = () => {
  const ua = navigator.userAgent || "";
  const isIOS =
    /iPad|iPhone|iPod/i.test(ua) ||
    (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  if (isIOS) return "ios";
  if (/Android/i.test(ua)) return "android";
  return "desktop";
};

const VAD_PROFILE = detectVadProfile();
const VAD = PLATFORM_VAD_CONFIG[VAD_PROFILE] || PLATFORM_VAD_CONFIG.desktop;

let ws = null;
let wsReady = false;
let sessionId = null;
let sessionToken = null;
let awaitingTurn = false;
let assistantStreamText = "";
let assistantDisplayText = "";
let gotAudioThisTurn = false;

let isVoiceMode = false;
let isMicMuted = false;
let stream = null;
let audioContext = null;
let analyser = null;
let monitorTimer = null;
let dynamicEnergyThreshold = VAD.energyBase;
let speakingSince = 0;
let lastSpeechAt = 0;
let pendingTranscript = "";
let playbackInterimText = "";
let playbackInterimAt = 0;
let audioStartedAt = 0;
let bargeRequested = false;
let lastEnergy = 0;
let assistantEnergyFloor = 0;
let suppressBargeUntil = 0;
let lastActivityAt = Date.now();

let speechRecognition = null;
let allowTranscriptDuringPlayback = false;
let recognitionLangIndex = 1;
let lastRecognitionResultAt = 0;
let transcriptBlockUntil = 0;
let lastAssistantUtterance = "";
let currentAssistantSource = "";
let lastDetectedLangHint = "en";

let isAudioPlaying = false;
let activeAudioUrl = null;
let audioQueue = [];
let audioPrimed = false;
let topMoviePool = [];
let topMovieRotateTimer = null;

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const fetchJsonWithRetry = async (url, options = {}, retries = 3, baseDelayMs = 700) => {
  let lastError = null;
  for (let attempt = 0; attempt <= retries; attempt += 1) {
    try {
      const res = await fetch(url, options);
      if (!res.ok) {
        throw new Error(`HTTP ${res.status}`);
      }
      return await res.json();
    } catch (err) {
      lastError = err;
      if (attempt >= retries) break;
      await sleep(baseDelayMs * (attempt + 1));
    }
  }
  throw lastError || new Error("Request failed");
};

const setStatus = (text) => {
  statusEl.textContent = text;
};

const markActivity = () => {
  lastActivityAt = Date.now();
};

const setMicUi = (active) => {
  voiceBtn.classList.toggle("mic-live", active);
  muteBtn.classList.toggle("hidden", !active);
};

const updateConversation = (userText, agentText) => {
  if (userText !== undefined) convUserText.textContent = userText || "...";
  if (agentText !== undefined) convAgentText.textContent = agentText || "...";
};

const safeText = (value) => String(value || "");

const safePosterUrl = (url) => {
  const raw = String(url || "").trim();
  if (!raw) return "";
  try {
    let normalized = raw;
    if (normalized.startsWith("//")) normalized = `https:${normalized}`;
    if (/^http:\/\//i.test(normalized)) normalized = normalized.replace(/^http:\/\//i, "https://");
    const parsed = new URL(normalized, window.location.origin);
    if (parsed.protocol === "http:" || parsed.protocol === "https:") return parsed.href;
  } catch (_) {
    return "";
  }
  return "";
};

const buildMovieTile = (movie) => {
  const link = document.createElement("a");
  link.className = "movie-link";
  const titleText = safeText(movie?.title);
  link.href = `https://www.google.com/search?q=${encodeURIComponent(`${titleText} movie`)}`;
  link.target = "_blank";
  link.rel = "noopener noreferrer";

  const article = document.createElement("article");
  article.className = "movie-tile";

  const img = document.createElement("img");
  img.alt = titleText;
  const poster = safePosterUrl(movie?.poster_url);
  if (poster) img.src = poster;
  article.appendChild(img);

  const meta = document.createElement("div");
  meta.className = "meta";

  const h4 = document.createElement("h4");
  h4.textContent = titleText;
  meta.appendChild(h4);

  const p = document.createElement("p");
  const genres = Array.isArray(movie?.genres) ? movie.genres : [];
  p.textContent = genres.slice(0, 2).map((g) => safeText(g)).join(" / ");
  meta.appendChild(p);

  article.appendChild(meta);
  link.appendChild(article);
  return link;
};

const renderMovieRow = (container, movies) => {
  container.innerHTML = "";
  if (!movies || !movies.length) return;
  movies.forEach((movie) => {
    container.appendChild(buildMovieTile(movie));
  });
};

const shuffleCopy = (arr) => {
  const out = [...arr];
  for (let i = out.length - 1; i > 0; i -= 1) {
    const j = Math.floor(Math.random() * (i + 1));
    [out[i], out[j]] = [out[j], out[i]];
  }
  return out;
};

const renderTopMovieSubset = () => {
  if (!Array.isArray(topMoviePool) || !topMoviePool.length) {
    topMoviesRow.innerHTML = "";
    return;
  }
  const view = shuffleCopy(topMoviePool).slice(0, TOP_VISIBLE_COUNT);
  renderMovieRow(topMoviesRow, view);
};

const stopTopMovieRotation = () => {
  if (topMovieRotateTimer) {
    clearInterval(topMovieRotateTimer);
    topMovieRotateTimer = null;
  }
};

const startTopMovieRotation = () => {
  stopTopMovieRotation();
  if (!topMoviePool.length || topMoviePool.length <= TOP_VISIBLE_COUNT) return;
  topMovieRotateTimer = setInterval(() => {
    if (homePage.classList.contains("hidden")) return;
    renderTopMovieSubset();
  }, TOP_ROTATE_MS);
};

const showRecommended = (movies, forceClear = false) => {
  if (!movies || !movies.length) {
    if (forceClear) {
      recommendedSection.classList.add("hidden");
      recommendedRow.innerHTML = "";
    }
    return;
  }
  recommendedSection.classList.remove("hidden");
  renderMovieRow(recommendedRow, movies);
};

const setLoading = (on, message = "Idle") => {
  setStatus(message);
  sendBtn.disabled = on;
};

const loadPosterWall = async () => {
  try {
    const data = await fetchJsonWithRetry("/poster-wall?count=50", {}, 2, 600);
    const posters = data.posters || [];
    posterTrack.innerHTML = "";
    if (!posters.length) return;

    const cols = 10;
    const rowHeight = 138;
    const neededRows = Math.ceil(window.innerHeight / rowHeight) + 3;
    const neededTiles = cols * neededRows;

    for (let i = 0; i < neededTiles; i += 1) {
      const url = posters[i % posters.length];
      const img = document.createElement("img");
      img.src = url;
      img.alt = "";
      posterTrack.appendChild(img);
    }
  } catch (_) {
    // ignore background fetch errors
  }
};

const loadTopMovies = async (genre = "") => {
  const qs = new URLSearchParams({ limit: String(TOP_POOL_LIMIT) });
  if (genre) qs.set("genre", genre);
  let data = null;
  try {
    data = await fetchJsonWithRetry(`/top-movies?${qs.toString()}`, {}, 3, 800);
  } catch (_) {
    data = await fetchJsonWithRetry(`/discover-movies?limit=${TOP_POOL_LIMIT}`, {}, 2, 900);
  }
  topMoviePool = Array.isArray(data?.movies) ? data.movies : [];
  renderTopMovieSubset();
  startTopMovieRotation();
};

const loadDiscoverMovies = async () => {
  const data = await fetchJsonWithRetry("/discover-movies?limit=50", {}, 3, 800);
  renderMovieRow(discoverGrid, data.movies || []);
};

const wsUrl = () => {
  const protocol = window.location.protocol === "https:" ? "wss" : "ws";
  const params = new URLSearchParams();
  if (PAGE_API_KEY) params.set("api_key", PAGE_API_KEY);
  const query = params.toString();
  return `${protocol}://${window.location.host}/ws/voice${query ? `?${query}` : ""}`;
};

const withAuthHeaders = (headers = {}) => {
  const out = { ...headers };
  if (PAGE_API_KEY) out["X-API-Key"] = PAGE_API_KEY;
  return out;
};

const sendWs = (payload) => {
  if (!ws || ws.readyState !== WebSocket.OPEN) return false;
  ws.send(JSON.stringify(payload));
  return true;
};

const clearAudioQueue = () => {
  audioQueue.forEach((url) => URL.revokeObjectURL(url));
  audioQueue = [];
};

const assistantSpeaking = () => isAudioPlaying || audioQueue.length > 0;

const normalizeText = (value) =>
  String(value || "")
    .toLowerCase()
    .replace(/[^a-z0-9\u0900-\u097f\s]/g, " ")
    .replace(/\s+/g, " ")
    .trim();

const detectQueryLanguageHint = (text, fallback = "en") => {
  const raw = String(text || "").trim();
  const fallbackHint = fallback === "hi" ? "hi" : "en";
  if (!raw) return fallbackHint;

  const latin = raw
    .toLowerCase()
    .replace(/[^a-z0-9\s]/g, " ")
    .replace(/\s+/g, " ")
    .trim();
  const words = latin ? latin.split(" ") : [];

  const hinglishMarkers = new Set([
    "main",
    "mai",
    "mujhe",
    "mujh",
    "mera",
    "meri",
    "mere",
    "tum",
    "aap",
    "ek",
    "kya",
    "hai",
    "hun",
    "hu",
    "ho",
    "hoon",
    "nahi",
    "nahin",
    "acha",
    "accha",
    "acchi",
    "achhi",
    "dekhna",
    "dekhni",
    "dekhne",
    "dekh",
    "chahta",
    "chahata",
    "chahti",
    "chahte",
    "chahiye",
    "batao",
    "dikhao",
    "sujhao",
    "sifarish",
    "karo",
    "kyu",
    "kyun",
  ]);
  let hinglishHits = 0;
  for (const w of words) {
    if (hinglishMarkers.has(w)) hinglishHits += 1;
  }

  const hasLatin = /[A-Za-z]/.test(raw);
  const hasDevanagari = /[\u0900-\u097f]/.test(raw);
  if (hasLatin && !hasDevanagari && hinglishHits >= 2) return "hi";
  if (hasLatin && !hasDevanagari) return "en";
  if (!hasDevanagari) return fallbackHint;

  const hindiMarkers = [
    "\u0939\u0948",
    "\u0939\u0942\u0901",
    "\u0939\u0948\u0902",
    "\u0915\u094d\u092f\u093e",
    "\u0915\u094c\u0928",
    "\u0915\u093f\u0938",
    "\u092e\u0941\u091d\u0947",
    "\u0906\u092a",
    "\u0914\u0930",
    "\u092e\u0947\u0902",
    "\u0938\u0947",
    "\u091a\u093e\u0939\u093f\u090f",
    "\u0938\u0941\u091d\u093e\u0935",
    "\u0926\u0947\u0916\u0928\u093e",
    "\u092b\u093f\u0932\u094d\u092e",
    "\u092e\u0942\u0935\u0940",
  ];
  const translitMarkers = [
    "\u0906\u0908",
    "\u0935\u093e\u0902\u091f",
    "\u091f\u0942",
    "\u0938\u0940",
    "\u0938\u092e",
    "\u092e\u094b\u0930",
    "\u0932\u093e\u0907\u0915",
    "\u0915\u093e\u0907\u0902\u0921",
    "\u0911\u092b",
    "\u092a\u094d\u0932\u0940\u091c",
    "\u0938\u091c\u0947\u0938\u094d\u091f",
    "\u0930\u0947\u0915\u092e\u0947\u0902\u0921",
  ];

  let hiHits = 0;
  let translitHits = 0;
  for (const token of hindiMarkers) {
    if (raw.includes(token)) hiHits += 1;
  }
  for (const token of translitMarkers) {
    if (raw.includes(token)) translitHits += 1;
  }

  // If recognizer is in English mode, prefer English unless text is clearly Hindi.
  if (fallbackHint === "en") {
    if (translitHits >= 1 && hiHits <= 2) return "en";
    if (hiHits === 0) return "en";
  }
  if (translitHits >= 2 && hiHits <= 1) return "en";
  return "hi";
};

const toTokens = (value) =>
  normalizeText(value)
    .split(" ")
    .filter((w) => w && w.length > 2);

const overlapRatio = (aTokens, bTokens) => {
  if (!aTokens.length || !bTokens.length) return 0;
  const bSet = new Set(bTokens);
  let hit = 0;
  aTokens.forEach((t) => {
    if (bSet.has(t)) hit += 1;
  });
  return hit / Math.max(1, Math.min(aTokens.length, bTokens.length));
};

const isLikelyAssistantEcho = (candidate) => {
  const c = normalizeText(candidate);
  if (!c) return false;
  const a = normalizeText(lastAssistantUtterance || assistantStreamText || convAgentText.textContent || "");
  if (!a) return false;
  if (c.length >= 10 && a.includes(c)) return true;
  const tail = a.slice(Math.max(0, a.length - 160));
  if (tail && c.includes(tail)) return true;
  const cTokens = toTokens(c);
  const aTokens = toTokens(a);
  if (overlapRatio(cTokens, aTokens) >= 0.6) return true;
  const tailTokens = toTokens(tail);
  if (overlapRatio(cTokens, tailTokens) >= 0.55) return true;
  return false;
};

const isMeaningfulAutoQuery = (text) => {
  const clean = String(text || "").trim();
  if (!clean) return false;
  const words = clean.split(/\s+/).filter(Boolean).length;
  if (words >= MIN_AUTO_QUERY_WORDS && clean.length >= MIN_AUTO_QUERY_CHARS) return true;
  const shortAllow = /^(hi|hello|hey|namaste|नमस्ते|thanks|thank you|धन्यवाद|शुक्रिया)$/i;
  return shortAllow.test(clean);
};

const interruptionPrompt = () => {
  const lastUser = String(convUserText.textContent || "");
  return /[\u0900-\u097f]/.test(lastUser)
    ? "मैंने आपकी बात सुनी। बताइए, आप मुझसे क्या करवाना चाहते हैं?"
    : "I heard you. What would you like me to do?";
};

const stopAssistantPlayback = (sendBargeIn = false) => {
  if (!audioPlayer.paused) {
    audioPlayer.pause();
  }
  audioPlayer.currentTime = 0;
  if (activeAudioUrl) {
    URL.revokeObjectURL(activeAudioUrl);
    activeAudioUrl = null;
  }
  clearAudioQueue();
  isAudioPlaying = false;
  if (sendBargeIn) {
    if (!bargeRequested) {
      sendWs({ type: "barge_in" });
      bargeRequested = true;
    }
    allowTranscriptDuringPlayback = true;
    updateConversation(undefined, interruptionPrompt());
    setStatus("Listening...");
  }
  playbackInterimText = "";
  playbackInterimAt = 0;
  audioStartedAt = 0;
  lastEnergy = 0;
  assistantEnergyFloor = 0;
  transcriptBlockUntil = Date.now() + (sendBargeIn ? 250 : VAD.echoSuppressMs);
  markActivity();
};

const playNextAudioChunk = async () => {
  if (isAudioPlaying || !audioQueue.length) {
    if (!audioQueue.length && isVoiceMode && !awaitingTurn) {
      setStatus("Listening...");
    }
    return;
  }
  const nextUrl = audioQueue.shift();
  activeAudioUrl = nextUrl;
  audioPlayer.src = nextUrl;
  isAudioPlaying = true;
  audioStartedAt = Date.now();
  allowTranscriptDuringPlayback = false;
  try {
    await audioPlayer.play();
    setStatus("Luma speaking...");
    markActivity();
  } catch (_) {
    isAudioPlaying = false;
    setStatus("Tap page once to enable audio playback");
  }
};

audioPlayer.onended = () => {
  isAudioPlaying = false;
  assistantEnergyFloor = 0;
  if (activeAudioUrl) {
    URL.revokeObjectURL(activeAudioUrl);
    activeAudioUrl = null;
  }
  if (!audioQueue.length) {
    transcriptBlockUntil = Date.now() + VAD.echoSuppressMs;
  }
  markActivity();
  playNextAudioChunk();
};

audioPlayer.onerror = () => {
  isAudioPlaying = false;
  assistantEnergyFloor = 0;
  if (activeAudioUrl) {
    URL.revokeObjectURL(activeAudioUrl);
    activeAudioUrl = null;
  }
  if (!audioQueue.length) {
    transcriptBlockUntil = Date.now() + VAD.echoSuppressMs;
  }
  markActivity();
  playNextAudioChunk();
};

const enqueueAudioChunk = (audioB64) => {
  if (!audioB64) return;
  const binary = atob(audioB64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) {
    bytes[i] = binary.charCodeAt(i);
  }
  const blob = new Blob([bytes], { type: "audio/mpeg" });
  const url = URL.createObjectURL(blob);
  audioQueue.push(url);
  playNextAudioChunk();
};

const handleWsMessage = (payload) => {
  const type = payload?.type;
  if (!type) return;

  if (type === "session_started") {
    sessionId = payload.session_id || sessionId;
    sessionToken = payload.session_token || sessionToken;
    return;
  }

  if (type === "turn_started") {
    markActivity();
    currentAssistantSource = String(payload.source || "query");
    assistantStreamText = "";
    assistantDisplayText = "";
    pendingTranscript = "";
    gotAudioThisTurn = false;
    bargeRequested = false;
    playbackInterimText = "";
    playbackInterimAt = 0;
    if (payload.source === "query" && payload.query) {
      updateConversation(payload.query, "Searching best movies...");
      setStatus("Searching best movies...");
      awaitingTurn = true;
      suppressBargeUntil = 0;
    } else if (payload.source === "greeting") {
      setStatus("Luma speaking...");
      awaitingTurn = true;
      suppressBargeUntil = Date.now() + 450;
      transcriptBlockUntil = Date.now() + 520;
    }
    return;
  }

  if (type === "text_delta") {
    markActivity();
    assistantStreamText += String(payload.delta || "");
    if (!gotAudioThisTurn && currentAssistantSource !== "query") {
      // Keep text/audio synchronized: before first audio chunk we can show deltas,
      // after audio starts we switch to sentence updates from spoken chunks.
      updateConversation(undefined, assistantStreamText);
    }
    return;
  }

  if (type === "movies_update") {
    markActivity();
    if (Array.isArray(payload.movies) && payload.movies.length) showRecommended(payload.movies);
    return;
  }

  if (type === "audio_chunk") {
    markActivity();
    gotAudioThisTurn = true;
    const sentence = String(payload.sentence || "").trim();
    if (sentence) {
      assistantDisplayText = `${assistantDisplayText}${assistantDisplayText ? "\n\n" : ""}${sentence}`.trim();
      updateConversation(undefined, assistantDisplayText);
    }
    enqueueAudioChunk(payload.audio_b64 || "");
    return;
  }

  if (type === "turn_complete") {
    markActivity();
    const completedSource = currentAssistantSource;
    sessionToken = payload.session_token !== undefined ? payload.session_token : sessionToken;
    if (!gotAudioThisTurn && payload.full_text) {
      updateConversation(undefined, payload.full_text);
    }
    if (Array.isArray(payload.movies) && payload.movies.length) showRecommended(payload.movies);
    lastAssistantUtterance = String(payload.full_text || assistantDisplayText || assistantStreamText || "").trim();
    transcriptBlockUntil = Date.now() + (completedSource === "greeting" ? 520 : 180);
    pendingTranscript = "";
    playbackInterimText = "";
    playbackInterimAt = 0;
    currentAssistantSource = "";
    awaitingTurn = false;
    bargeRequested = false;
    if (payload.end_session) {
      stopVoiceMode();
      return;
    }
    if (!assistantSpeaking() && isVoiceMode) setStatus("Listening...");
    return;
  }

  if (type === "turn_cancelled") {
    markActivity();
    awaitingTurn = false;
    bargeRequested = false;
    currentAssistantSource = "";
    if (isVoiceMode) setStatus("Listening...");
    return;
  }

  if (type === "barge_in_ack") {
    markActivity();
    bargeRequested = false;
    updateConversation(undefined, interruptionPrompt());
    setStatus("Listening...");
    return;
  }

  if (type === "error") {
    awaitingTurn = false;
    updateConversation(undefined, `Error: ${payload.detail || "Unknown voice error"}`);
    setStatus("Error");
  }
};

const connectVoiceSocket = async () =>
  new Promise((resolve, reject) => {
    if (ws && ws.readyState === WebSocket.OPEN) {
      wsReady = true;
      resolve();
      return;
    }

    ws = new WebSocket(wsUrl());
    ws.onopen = () => {
      wsReady = true;
      resolve();
    };
    ws.onerror = () => {
      wsReady = false;
      reject(new Error("Voice socket connection failed"));
    };
    ws.onclose = () => {
      wsReady = false;
      if (isVoiceMode) {
        setStatus("Voice connection closed");
      }
    };
    ws.onmessage = (event) => {
      try {
        handleWsMessage(JSON.parse(event.data));
      } catch (_) {
        // ignore malformed messages
      }
    };
  });

const closeVoiceSocket = () => {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.close(1000, "voice-stop");
  }
  ws = null;
  wsReady = false;
};

const primeAudioPlayback = async () => {
  if (audioPrimed) return;
  try {
    // Unlock media playback within user gesture flow for strict autoplay policies.
    const silent = new Blob([new Uint8Array([0])], { type: "audio/mpeg" });
    const url = URL.createObjectURL(silent);
    audioPlayer.muted = true;
    audioPlayer.src = url;
    await audioPlayer.play();
    audioPlayer.pause();
    audioPlayer.currentTime = 0;
    audioPlayer.src = "";
    audioPlayer.muted = false;
    URL.revokeObjectURL(url);
    audioPrimed = true;
  } catch (_) {
    audioPlayer.muted = false;
    // Keep running even if priming fails; normal play path will still try.
  }
};

const computeRmsEnergy = () => {
  if (!analyser) return 0;
  const data = new Uint8Array(analyser.fftSize);
  analyser.getByteTimeDomainData(data);
  let sum = 0;
  for (let i = 0; i < data.length; i += 1) {
    const normalized = (data[i] - 128) / 128;
    sum += normalized * normalized;
  }
  return Math.sqrt(sum / data.length);
};

const calibrateAmbientNoise = async (ms = VAD.calibrationMs) => {
  const started = Date.now();
  const samples = [];
  while (Date.now() - started < ms) {
    samples.push(computeRmsEnergy());
    // eslint-disable-next-line no-await-in-loop
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  const ambient = samples.length ? samples.reduce((a, b) => a + b, 0) / samples.length : 0;
  dynamicEnergyThreshold = Math.max(VAD.energyBase, ambient * VAD.calibrationMultiplier);
};

const setMicCaptureEnabled = (enabled) => {
  if (!stream) return;
  stream.getAudioTracks().forEach((track) => {
    track.enabled = enabled;
  });
};

const stopSpeechRecognition = () => {
  if (!speechRecognition) return;
  try {
    speechRecognition.onend = null;
    speechRecognition.stop();
  } catch (_) {
    // ignore stop errors
  }
  speechRecognition = null;
};

const startSpeechRecognition = () => {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR || speechRecognition) return;

  const recog = new SR();
  recog.continuous = true;
  recog.interimResults = true;
  recog.lang = RECOGNITION_LANGS[recognitionLangIndex];

  recog.onresult = (event) => {
    let interim = "";
    for (let i = event.resultIndex; i < event.results.length; i += 1) {
      interim += event.results[i][0].transcript || "";
    }
    const text = interim.trim();
    if (!text) return;
    const inTranscriptBlock = Date.now() < transcriptBlockUntil;
    lastRecognitionResultAt = Date.now();
    const recogLangHint = RECOGNITION_LANGS[recognitionLangIndex].startsWith("hi") ? "hi" : "en";
    const detectedHint = detectQueryLanguageHint(text, recogLangHint);
    lastDetectedLangHint = detectedHint;
    if (currentAssistantSource === "greeting" && inTranscriptBlock) return;
    if (assistantSpeaking() && !allowTranscriptDuringPlayback) {
      // Never accept transcript while assistant audio is active.
      // Barge-in is handled by VAD logic only to prevent self-capture.
      return;
    }
    if (inTranscriptBlock) return;

    pendingTranscript = text;
    if (isLikelyAssistantEcho(pendingTranscript)) return;
    lastSpeechAt = Date.now();
    updateConversation(text, undefined);
    markActivity();
  };

  recog.onerror = () => {};
  recog.onend = () => {
    if (isVoiceMode && !isMicMuted) {
      try {
        recog.lang = RECOGNITION_LANGS[recognitionLangIndex];
        recog.start();
      } catch (_) {
        // ignore browser restart race
      }
    }
  };

  speechRecognition = recog;
  try {
    speechRecognition.start();
  } catch (_) {
    // ignore browser permission/timing errors
  }
};

const submitVoiceQuery = (query) => {
  const clean = String(query || "").trim();
  if (!clean || !wsReady || awaitingTurn) return;
  if (Date.now() < transcriptBlockUntil) return;
  if (isLikelyAssistantEcho(clean)) return;
  if (!isMeaningfulAutoQuery(clean)) return;
  awaitingTurn = true;
  allowTranscriptDuringPlayback = false;
  setStatus("Searching best movies...");
  updateConversation(clean, "Searching best movies...");
  markActivity();
  const recogLangHint = RECOGNITION_LANGS[recognitionLangIndex].startsWith("hi") ? "hi" : "en";
  const langHint = detectQueryLanguageHint(clean, lastDetectedLangHint || recogLangHint);
  recognitionLangIndex = langHint === "hi" ? 0 : 1;
  sendWs({
    type: "user_query",
    query: clean,
    lang_hint: langHint,
    session_token: sessionToken || "",
  });
};

const processVadTick = () => {
  if (!isVoiceMode || isMicMuted || !analyser) return;

  const energy = computeRmsEnergy();
  lastEnergy = energy;
  const speaking = energy > dynamicEnergyThreshold;

  if (speaking) {
    lastSpeechAt = Date.now();
    markActivity();
    if (assistantSpeaking()) {
      const now = Date.now();
      const warmup = audioStartedAt && now - audioStartedAt < 350;
      const minPlaybackElapsed = audioStartedAt && now - audioStartedAt >= VAD.bargeMinAudioMs;
      assistantEnergyFloor = assistantEnergyFloor ? assistantEnergyFloor * 0.88 + energy * 0.12 : energy;
      const bargeLevel = Math.max(
        dynamicEnergyThreshold * VAD.bargeDynamicMultiplier,
        assistantEnergyFloor * VAD.bargeAssistantFloorMultiplier
      );
      const strongVoice = energy > bargeLevel;
      if (!speakingSince) speakingSince = now;
      const sustainedVoice = now - speakingSince >= VAD.bargeHoldMs;
      const strongVoiceOnly =
        now - speakingSince >= VAD.bargeStrongHoldMs && energy > bargeLevel * VAD.bargeStrongMultiplier;
      if (
        !warmup &&
        minPlaybackElapsed &&
        Date.now() >= suppressBargeUntil &&
        strongVoice &&
        (sustainedVoice || strongVoiceOnly)
      ) {
        stopAssistantPlayback(true);
        lastSpeechAt = Date.now();
        speakingSince = 0;
        playbackInterimText = "";
        playbackInterimAt = 0;
        setStatus("Listening...");
        return;
      }
      if (!strongVoice) speakingSince = 0;
    } else {
      speakingSince = 0;
    }
    return;
  }

  speakingSince = 0;
  if (
    pendingTranscript.trim() &&
    !assistantSpeaking() &&
    !awaitingTurn &&
    Date.now() >= transcriptBlockUntil &&
    Date.now() - lastSpeechAt >= VAD.silenceMs
  ) {
    const query = pendingTranscript.trim();
    pendingTranscript = "";
    submitVoiceQuery(query);
  }

  const idleFor = Date.now() - Math.max(lastSpeechAt || 0, lastActivityAt || 0);
  if (!awaitingTurn && !assistantSpeaking() && !pendingTranscript.trim() && idleFor >= MIC_IDLE_TIMEOUT_MS) {
    updateConversation(undefined, "Mic paused after 30 seconds of silence.");
    stopVoiceMode();
  }
};

const startVoiceMode = async () => {
  if (isVoiceMode) return;

  stream = await navigator.mediaDevices.getUserMedia({
    audio: {
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
      channelCount: 1,
    },
  });
  await primeAudioPlayback();

  await connectVoiceSocket();

  isVoiceMode = true;
  isMicMuted = false;
  awaitingTurn = false;
  pendingTranscript = "";
  speakingSince = 0;
  lastSpeechAt = Date.now();
  lastActivityAt = Date.now();
  lastRecognitionResultAt = Date.now();
  transcriptBlockUntil = Date.now() + VAD.echoSuppressMs;
  lastAssistantUtterance = "";
  suppressBargeUntil = Date.now() + 1200;
  setMicUi(true);
  muteBtn.classList.remove("active");
  setStatus("Listening...");

  audioContext = new (window.AudioContext || window.webkitAudioContext)();
  const source = audioContext.createMediaStreamSource(stream);
  analyser = audioContext.createAnalyser();
  analyser.fftSize = 1024;
  source.connect(analyser);

  startSpeechRecognition();
  await calibrateAmbientNoise();
  monitorTimer = setInterval(processVadTick, VAD.vadIntervalMs);

  sendWs({ type: "start_session", session_token: sessionToken || "" });
};

const stopVoiceMode = () => {
  isVoiceMode = false;
  awaitingTurn = false;
  pendingTranscript = "";
  speakingSince = 0;
  clearInterval(monitorTimer);
  monitorTimer = null;

  stopAssistantPlayback(false);
  stopSpeechRecognition();

  if (stream) stream.getTracks().forEach((track) => track.stop());
  stream = null;
  if (audioContext) {
    audioContext.close();
  }
  audioContext = null;
  analyser = null;
  suppressBargeUntil = 0;
  transcriptBlockUntil = 0;
  lastAssistantUtterance = "";

  isMicMuted = false;
  muteBtn.classList.remove("active");
  setMicUi(false);
  setStatus("Idle");

  closeVoiceSocket();
};

const postRecommend = async () => {
  const query = queryInput.value.trim();
  if (!query) return;

  if (isVoiceMode && wsReady) {
    submitVoiceQuery(query);
    return;
  }

  setLoading(true, "Searching best movies...");
  updateConversation(query, convAgentText.textContent);
  try {
    const res = await fetch("/recommend", {
      method: "POST",
      headers: withAuthHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ query, include_audio: false }),
    });
    if (!res.ok) throw new Error(`Request failed: ${res.status}`);
    const data = await res.json();
    updateConversation(query, data.text || "");
    showRecommended(data.movies || []);
    setStatus("Done");
  } catch (err) {
    updateConversation(undefined, `Error: ${err.message}`);
    setStatus("Error");
  } finally {
    setLoading(false, statusEl.textContent);
  }
};

const setTab = (tab) => {
  const isHome = tab === "home";
  homePage.classList.toggle("hidden", !isHome);
  discoverPage.classList.toggle("hidden", isHome);
  tabHome.classList.toggle("active", isHome);
  tabDiscover.classList.toggle("active", !isHome);
  if (isHome) startTopMovieRotation();
  else stopTopMovieRotation();
};

tabHome.addEventListener("click", () => setTab("home"));
tabDiscover.addEventListener("click", () => setTab("discover"));

genreChips.addEventListener("click", async (event) => {
  const btn = event.target.closest(".genre-chip");
  if (!btn) return;
  genreChips.querySelectorAll(".genre-chip").forEach((chip) => chip.classList.remove("active"));
  btn.classList.add("active");
  const genre = btn.getAttribute("data-genre") || "";
  try {
    await loadTopMovies(genre);
  } catch (_) {
    // keep current tiles on genre fetch failure
  }
});

sendBtn.addEventListener("click", postRecommend);
queryInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter") postRecommend();
});

voiceBtn.addEventListener("click", async () => {
  try {
    if (isVoiceMode) stopVoiceMode();
    else await startVoiceMode();
  } catch (err) {
    updateConversation(undefined, `Error: ${err.message}`);
    setStatus("Error");
    stopVoiceMode();
  }
});

muteBtn.addEventListener("click", () => {
  isMicMuted = !isMicMuted;
  muteBtn.classList.toggle("active", isMicMuted);
  setMicCaptureEnabled(!isMicMuted);
  if (isMicMuted) {
    stopSpeechRecognition();
    setStatus("Mic muted");
  } else {
    startSpeechRecognition();
    setStatus(isVoiceMode ? "Listening..." : "Idle");
  }
});

document.querySelectorAll(".row-nav").forEach((btn) => {
  btn.addEventListener("click", () => {
    const targetId = btn.getAttribute("data-target");
    const row = document.getElementById(targetId);
    if (!row) return;
    const dir = btn.classList.contains("left") ? -1 : 1;
    row.scrollBy({ left: dir * 360, behavior: "smooth" });
  });
});

const bootstrap = async () => {
  setTab("home");
  showRecommended([], true);
  await Promise.allSettled([loadTopMovies(""), loadDiscoverMovies(), loadPosterWall()]);
};

bootstrap();
window.addEventListener("resize", () => {
  loadPosterWall();
});
