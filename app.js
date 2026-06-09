const CONFIDENCE_THRESHOLD = 0.5;

const stateMap = {
  idle: {
    pill: "待機中",
    title: "等待投入",
    copy: "全程零按鈕，靠近即偵測，投入後自動判定。",
    roast: "靠近我，讓我看看你丟了什麼。",
    confidence: null,
    result: "idle",
  },
  detect: {
    pill: "Mock 偵測",
    title: "掃描中",
    copy: "L515 depth 已偵測到距離變化，準備擷取 RGB 畫面。",
    roast: "我看到你了，手上那個最好不是吸管。",
    confidence: 0.32,
    result: "detect",
  },
  cooldown: {
    pill: "冷卻中",
    title: "事件已記錄",
    copy: "語音與事件紀錄已完成，等待下一次靠近觸發。",
    roast: "下一位，請一次丟一樣就好。",
    confidence: null,
    result: "cooldown",
  },
  accept: {
    pill: "Accept",
    title: "分類正確",
    copy: "物件已被判定為可接受投入，事件已寫入本機 log。",
    roast: "......算你會。",
    confidence: 0.91,
    result: "accept",
  },
  reject: {
    pill: "Reject",
    title: "分類錯誤",
    copy: "判定結果與本垃圾桶接受類別不符，播放中度 roast 語音。",
    roast: "這不是一般垃圾，你最好再看一次。",
    confidence: 0.84,
    result: "reject",
  },
  multi: {
    pill: "Multi-object",
    title: "一次丟太多",
    copy: "num_objects 大於 1，依保留規則顯示多物件 reject。",
    roast: "一次丟一堆是在趕時間？分一下好嗎。",
    confidence: 0.76,
    result: "multi",
  },
  low: {
    pill: "Low confidence",
    title: "低信心",
    copy: "confidence 低於閾值，改播自嘲語音且不計入 accept/reject 統計。",
    roast: "我看不太出來欸，可能是我老花。",
    confidence: 0.43,
    result: "low",
  },
};

let events = [];
let acceptCount = 0;
let rejectCount = 0;
let connectedToBridge = false;
let localCooldownTimer = null;

const body = document.body;
const views = document.querySelectorAll(".view");
const tabButtons = document.querySelectorAll(".tab-button");
const statePill = document.querySelector("#state-pill");
const resultTitle = document.querySelector("#result-title");
const resultCopy = document.querySelector("#result-copy");
const confidenceFill = document.querySelector("#confidence-fill");
const confidenceValue = document.querySelector("#confidence-value");
const acceptCountEl = document.querySelector("#accept-count");
const rejectCountEl = document.querySelector("#reject-count");
const accuracyRateEl = document.querySelector("#accuracy-rate");
const displayEvents = document.querySelector("#display-events");
const adminLog = document.querySelector("#admin-log");
const thresholdControl = document.querySelector("#threshold-control");
const thresholdValue = document.querySelector("#threshold-value");
const volumeControl = document.querySelector("#volume-control");
const volumeValue = document.querySelector("#volume-value");
const queueStatus = document.querySelector("#queue-status");
const bridgeStatus = document.querySelector("#bridge-status");
const cameraFrame = document.querySelector("#camera-frame");
const cameraVideo = document.querySelector("#camera-video");
const cameraSnapshot = document.querySelector("#camera-snapshot");
const cameraToggle = document.querySelector("#camera-toggle");
const cameraMessage = document.querySelector("#camera-message");
const impactImage = document.querySelector("#impact-image");
const impactCopy = document.querySelector("#impact-copy");

let cameraStream = null;
let cameraStreamStarted = false;
let cameraRetryTimer = null;

const impactVisuals = {
  idle: {
    src: "assets/sea-turtle-display.png",
    alt: "海龜在海中游動",
    copy: "等待下一次投入，結果會同步切換海洋回饋。",
  },
  detect: {
    src: "assets/sea-turtle-display.png",
    alt: "海龜在海中游動",
    copy: "正在即時觀察投入物，等待模型結果穩定。",
  },
  cooldown: {
    src: "assets/sea-turtle-display.png",
    alt: "海龜在海中游動",
    copy: "事件已記錄，準備回到待機。",
  },
  accept: {
    src: "assets/sea-turtle-accept.png",
    alt: "開心海龜比出鼓勵手勢",
    copy: "分類正確，海龜表示很可以。",
  },
  reject: {
    src: "assets/sea-turtle-reject.png",
    alt: "傷心海龜舉手制止",
    copy: "分類錯誤，海龜正在制止這次投入。",
  },
  multi: {
    src: "assets/sea-turtle-reject.png",
    alt: "傷心海龜舉手制止",
    copy: "一次投入太多，請分開再試。",
  },
  low: {
    src: "assets/sea-turtle-reject.png",
    alt: "困惑海龜舉手提醒",
    copy: "AI 信心不足，請讓物件更清楚。",
  },
};

function nowIso() {
  return new Date().toISOString().slice(0, 19);
}

function nowTime() {
  return new Intl.DateTimeFormat("zh-TW", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(new Date());
}

function timeFromTs(ts) {
  if (!ts) return nowTime();
  const parsed = new Date(ts);
  if (Number.isNaN(parsed.getTime())) return nowTime();
  return new Intl.DateTimeFormat("zh-TW", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(parsed);
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (char) => {
    const entities = {
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    };
    return entities[char];
  });
}

function formatConfidence(value) {
  return typeof value === "number" && Number.isFinite(value) ? value.toFixed(2) : "--";
}

function resultLabel(result) {
  const labels = {
    accept: "Accept 正確",
    reject: "Reject 錯誤",
    multi: "Reject 多物件",
    low: "低信心",
    detect: "偵測中",
    cooldown: "冷卻中",
    idle: "待機",
  };
  return labels[result] || result;
}

function mockRecognitionResult(type) {
  const base = {
    event: "recognition_result",
    ts: nowIso(),
  };
  const samples = {
    accept: { class: "accept", confidence: 0.91, num_objects: 1, snapshot_path: "mock/l515-accept.jpg" },
    reject: { class: "reject", confidence: 0.84, num_objects: 1, snapshot_path: "mock/l515-reject.jpg" },
    multi: { class: "reject", confidence: 0.76, num_objects: 2, snapshot_path: "mock/l515-multi.jpg" },
    low: { class: "accept", confidence: 0.43, num_objects: 1, snapshot_path: "mock/l515-low-confidence.jpg" },
  };
  return { ...base, ...(samples[type] || samples.reject) };
}

function outcomeFromRecognition(payload, threshold = CONFIDENCE_THRESHOLD) {
  if (Number(payload.confidence) < threshold) return "low";
  if (Number(payload.num_objects) > 1) return "multi";
  return payload.class === "accept" ? "accept" : "reject";
}

function stateFor(type, overrides = {}) {
  const base = stateMap[type] || stateMap.idle;
  return {
    ...base,
    ...overrides,
    confidence: typeof overrides.confidence === "number" ? overrides.confidence : base.confidence,
    result: overrides.result || base.result,
  };
}

function setBridgeStatus(status, isOnline = false) {
  if (bridgeStatus) {
    bridgeStatus.textContent = status;
    bridgeStatus.parentElement.classList.toggle("offline", !isOnline);
  }
}

function setQueueStatus(status, isOnline = false) {
  if (queueStatus) {
    queueStatus.textContent = status;
    queueStatus.parentElement.classList.toggle("offline", !isOnline);
  }
}

function currentThreshold() {
  return thresholdControl ? Number(thresholdControl.value) : CONFIDENCE_THRESHOLD;
}

function setCameraMessage(message) {
  if (cameraMessage) cameraMessage.textContent = message;
}

async function startCamera() {
  showCameraStream();
}

function cameraStreamUrl() {
  return `/api/camera.mjpg?v=${Date.now()}`;
}

function showCameraStream() {
  if (!cameraFrame || !cameraSnapshot || cameraStreamStarted || window.location.protocol === "file:") return;
  window.clearTimeout(cameraRetryTimer);
  cameraStreamStarted = true;
  cameraSnapshot.src = cameraStreamUrl();
  cameraFrame.classList.add("camera-live");
  setCameraMessage("L515 RGB 即時串流中");
}

async function startBrowserCamera() {
  if (!cameraFrame || !cameraVideo) return;
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    setCameraMessage("此瀏覽器不支援相機存取");
    return;
  }

  try {
    if (cameraStream) {
      cameraStream.getTracks().forEach((track) => track.stop());
    }

    cameraStream = await navigator.mediaDevices.getUserMedia({
      audio: false,
      video: {
        facingMode: { ideal: "environment" },
        width: { ideal: 1280 },
        height: { ideal: 720 },
      },
    });
    cameraVideo.srcObject = cameraStream;
    cameraFrame.classList.add("camera-live");
    setCameraMessage("相機已啟用");
  } catch (error) {
    cameraFrame.classList.remove("camera-live");
    if (error && error.name === "NotAllowedError") {
      setCameraMessage("相機權限被拒絕，請允許後再試一次");
    } else if (!window.isSecureContext) {
      setCameraMessage("遠端瀏覽器需要 HTTPS 或 localhost 才能開相機");
    } else {
      setCameraMessage("找不到可用相機，請確認裝置連線");
    }
  }
}

function setDisplayState(displayState) {
  const state = stateFor(displayState.result, displayState);
  const impact = impactVisuals[state.result] || impactVisuals.idle;
  body.className = `state-${state.result}`;
  if (statePill) statePill.textContent = state.pill;
  if (resultTitle) resultTitle.textContent = state.title;
  if (resultCopy) resultCopy.textContent = state.copy;
  if (impactImage) {
    impactImage.src = impact.src;
    impactImage.alt = impact.alt;
  }
  if (impactCopy) impactCopy.textContent = impact.copy;

  if (state.confidence === null || state.confidence === undefined) {
    if (confidenceFill) confidenceFill.style.width = "0%";
    if (confidenceValue) confidenceValue.textContent = "--";
  } else {
    if (confidenceFill) confidenceFill.style.width = `${Math.round(state.confidence * 100)}%`;
    if (confidenceValue) confidenceValue.textContent = formatConfidence(state.confidence);
  }
}

function renderCounts() {
  if (acceptCountEl) acceptCountEl.textContent = String(acceptCount);
  if (rejectCountEl) rejectCountEl.textContent = String(rejectCount);
  if (accuracyRateEl) {
    const total = acceptCount + rejectCount;
    accuracyRateEl.textContent = total === 0 ? "--" : `${Math.round((acceptCount / total) * 100)}%`;
  }
}

function renderEvents() {
  if (events.length === 0) {
    if (displayEvents) {
      displayEvents.innerHTML = `
        <article class="event-card idle">
          <span class="metric-label">等待 Queue</span>
          <strong>尚無事件</strong>
          <p>正式模式會由 q_result 的 recognition_result 更新這裡。</p>
        </article>
      `;
    }
    if (adminLog) adminLog.innerHTML = `<tr><td colspan="4">尚無事件</td></tr>`;
    return;
  }

  if (displayEvents) {
    displayEvents.innerHTML = events
      .slice(0, 3)
      .map(
        (event) => `
          <article class="event-card ${escapeHtml(event.result)}">
            <span class="metric-label">${escapeHtml(event.time)}</span>
            <strong>${escapeHtml(resultLabel(event.result))}</strong>
            <p>conf ${escapeHtml(formatConfidence(event.confidence))} · ${escapeHtml(event.num_objects || 1)} 件物件</p>
          </article>
        `,
      )
      .join("");
  }

  if (adminLog) {
    adminLog.innerHTML = events
      .map(
        (event) => `
          <tr>
            <td>${escapeHtml(event.time)}</td>
            <td>${escapeHtml(resultLabel(event.result))}</td>
            <td>${escapeHtml(formatConfidence(event.confidence))}</td>
            <td>${escapeHtml(event.roast)}</td>
          </tr>
        `,
      )
      .join("");
  }
}

function appendLocalRecognition(payload) {
  const result = outcomeFromRecognition(payload, currentThreshold());
  const displayState = stateFor(result, {
    confidence: Number(payload.confidence),
    class: payload.class,
    num_objects: Number(payload.num_objects),
    snapshot_path: payload.snapshot_path,
    ts: payload.ts,
  });

  if (result === "accept") {
    acceptCount += 1;
  } else if (["reject", "multi"].includes(result)) {
    rejectCount += 1;
  }

  events = [
    {
      time: timeFromTs(payload.ts),
      result,
      class: payload.class,
      confidence: Number(payload.confidence),
      num_objects: Number(payload.num_objects),
      snapshot_path: payload.snapshot_path,
      ts: payload.ts,
      source: "local",
      roast: displayState.roast,
    },
    ...events,
  ].slice(0, 100);

  setDisplayState(displayState);
  renderCounts();
  renderEvents();
  scheduleLocalCooldown();
}

function scheduleLocalCooldown() {
  window.clearTimeout(localCooldownTimer);
  localCooldownTimer = window.setTimeout(() => {
    setDisplayState(stateMap.cooldown);
    localCooldownTimer = window.setTimeout(() => setDisplayState(stateMap.idle), 2000);
  }, 4000);
}

function applySnapshot(snapshot) {
  if (!snapshot || !snapshot.current || !snapshot.counts) return;
  events = Array.isArray(snapshot.events) ? snapshot.events : [];
  acceptCount = Number(snapshot.counts.accept || 0);
  rejectCount = Number(snapshot.counts.reject || 0);
  setDisplayState(snapshot.current);
  renderCounts();
  renderEvents();
  if (snapshot.current.event === "vision_preview") {
    setQueueStatus("Preview", true);
  } else if (snapshot.current.source === "queue") {
    setQueueStatus("Receiving", true);
  }
}

async function simulateViaServer(type) {
  if (type === "detect") {
    setDisplayState(stateMap.detect);
    return;
  }

  if (!connectedToBridge) {
    appendLocalRecognition(mockRecognitionResult(type));
    return;
  }

  try {
    const response = await fetch("/api/simulate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ result: type }),
    });
    if (!response.ok) throw new Error(`simulate failed: ${response.status}`);
    applySnapshot(await response.json());
  } catch {
    appendLocalRecognition(mockRecognitionResult(type));
  }
}

function exportCsv() {
  const rows = [["time", "result", "confidence", "roast", "source"]];
  events.forEach((event) => {
    rows.push([event.time, event.result, formatConfidence(event.confidence), event.roast, event.source || ""]);
  });
  const csv = rows.map((row) => row.map((cell) => `"${String(cell).replaceAll('"', '""')}"`).join(",")).join("\n");
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = "ai-emotional-bin-events.csv";
  link.click();
  URL.revokeObjectURL(url);
}

function startEventStream() {
  if (!window.EventSource || window.location.protocol === "file:") {
    setBridgeStatus("Local mock", false);
    setQueueStatus("Mock only", false);
    return;
  }

  const source = new EventSource("/events");
  source.addEventListener("open", () => {
    connectedToBridge = true;
    setBridgeStatus("Connected", true);
  });
  source.addEventListener("state", (event) => {
    connectedToBridge = true;
    applySnapshot(JSON.parse(event.data));
  });
  source.addEventListener("error", () => {
    connectedToBridge = false;
    setBridgeStatus("Reconnecting", false);
  });
}

async function loadInitialState() {
  if (window.location.protocol === "file:") {
    renderCounts();
    renderEvents();
    setDisplayState(stateMap.idle);
    return;
  }

  try {
    const response = await fetch("/api/state", { cache: "no-store" });
    if (!response.ok) throw new Error(`state failed: ${response.status}`);
    connectedToBridge = true;
    setBridgeStatus("Connected", true);
    applySnapshot(await response.json());
  } catch {
    connectedToBridge = false;
    setBridgeStatus("Local mock", false);
    setQueueStatus("Mock only", false);
    renderCounts();
    renderEvents();
    setDisplayState(stateMap.idle);
  }
}

tabButtons.forEach((button) => {
  button.addEventListener("click", () => {
    const target = button.dataset.view;
    tabButtons.forEach((item) => item.classList.toggle("active", item === button));
    views.forEach((view) => view.classList.toggle("active", view.id === `${target}-view`));
  });
});

document.querySelectorAll("[data-event]").forEach((button) => {
  button.addEventListener("click", () => simulateViaServer(button.dataset.event));
});

if (thresholdControl && thresholdValue) {
  thresholdControl.addEventListener("input", () => {
    thresholdValue.textContent = Number(thresholdControl.value).toFixed(2);
  });
}

if (volumeControl && volumeValue) {
  volumeControl.addEventListener("input", () => {
    volumeValue.textContent = `${volumeControl.value}%`;
  });
}

const exportCsvButton = document.querySelector("#export-csv");
if (exportCsvButton) exportCsvButton.addEventListener("click", exportCsv);

const simulateStreamButton = document.querySelector("#simulate-stream");
if (simulateStreamButton) {
  simulateStreamButton.addEventListener("click", () => {
    const sequence = ["accept", "reject", "multi", "low"];
    const next = sequence[Math.floor(Math.random() * sequence.length)];
    simulateViaServer(next);
  });
}

if (cameraToggle) {
  cameraToggle.addEventListener("click", startCamera);
}

if (cameraSnapshot) {
  cameraSnapshot.addEventListener("error", () => {
    cameraStreamStarted = false;
    cameraFrame?.classList.remove("camera-live");
    setCameraMessage("等待 /api/camera.mjpg 即時影像");
    window.clearTimeout(cameraRetryTimer);
    cameraRetryTimer = window.setTimeout(startCamera, 2000);
  });
}

renderCounts();
renderEvents();
setDisplayState(stateMap.idle);
startCamera();
loadInitialState();
startEventStream();
