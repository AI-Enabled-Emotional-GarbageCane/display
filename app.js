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
    pill: "偵測到使用者",
    title: "掃描中",
    copy: "D435 Depth 已偵測到距離變化，準備擷取 RGB 畫面。",
    roast: "我看到你了，手上那個最好不是吸管。",
    confidence: 0.32,
    result: "detect",
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
    copy: "判定結果與投入桶別不符，播放中度 roast 語音。",
    roast: "這是塑膠不是紙，你眼睛被蛤蜊夾到了？",
    confidence: 0.84,
    result: "reject",
  },
  multi: {
    pill: "Multi-object",
    title: "一次丟太多",
    copy: "YOLO 偵測到多個物件，依規則直接 reject。",
    roast: "一次丟一堆是在趕投胎？分一下好嗎。",
    confidence: 0.76,
    result: "multi",
  },
  low: {
    pill: "Low confidence",
    title: "低信心",
    copy: "confidence 低於閾值，改播自嘲語音並保留快照供後續檢查。",
    roast: "我看不太出來欸，可能是我老花。",
    confidence: 0.43,
    result: "low",
  },
};

const initialEvents = [
  {
    time: "20:58:42",
    result: "accept",
    confidence: 0.91,
    roast: "......算你會。",
  },
  {
    time: "20:57:18",
    result: "reject",
    confidence: 0.84,
    roast: "這是塑膠不是紙，你眼睛被蛤蜊夾到了？",
  },
  {
    time: "20:55:03",
    result: "low",
    confidence: 0.43,
    roast: "我看不太出來欸，可能是我老花。",
  },
];

let events = [...initialEvents];
let acceptCount = 42;
let rejectCount = 8;

const body = document.body;
const views = document.querySelectorAll(".view");
const tabButtons = document.querySelectorAll(".tab-button");
const statePill = document.querySelector("#state-pill");
const resultTitle = document.querySelector("#result-title");
const resultCopy = document.querySelector("#result-copy");
const roastLine = document.querySelector("#roast-line");
const confidenceFill = document.querySelector("#confidence-fill");
const confidenceValue = document.querySelector("#confidence-value");
const acceptCountEl = document.querySelector("#accept-count");
const rejectCountEl = document.querySelector("#reject-count");
const turtleCountEl = document.querySelector("#turtle-count");
const displayEvents = document.querySelector("#display-events");
const adminLog = document.querySelector("#admin-log");
const thresholdControl = document.querySelector("#threshold-control");
const thresholdValue = document.querySelector("#threshold-value");
const volumeControl = document.querySelector("#volume-control");
const volumeValue = document.querySelector("#volume-value");

function nowTime() {
  return new Intl.DateTimeFormat("zh-TW", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(new Date());
}

function resultLabel(result) {
  const labels = {
    accept: "Accept 正確",
    reject: "Reject 錯誤",
    multi: "Reject 多物件",
    low: "低信心",
    detect: "偵測中",
    idle: "待機",
  };
  return labels[result] || result;
}

function setState(type, shouldLog = true) {
  const state = stateMap[type] || stateMap.idle;
  body.className = `state-${state.result}`;
  statePill.textContent = state.pill;
  resultTitle.textContent = state.title;
  resultCopy.textContent = state.copy;
  roastLine.textContent = state.roast;

  if (state.confidence === null) {
    confidenceFill.style.width = "0%";
    confidenceValue.textContent = "--";
  } else {
    confidenceFill.style.width = `${Math.round(state.confidence * 100)}%`;
    confidenceValue.textContent = state.confidence.toFixed(2);
  }

  if (shouldLog && ["accept", "reject", "multi", "low"].includes(type)) {
    if (type === "accept") {
      acceptCount += 1;
    } else {
      rejectCount += 1;
    }

    events = [
      {
        time: nowTime(),
        result: type,
        confidence: state.confidence,
        roast: state.roast,
      },
      ...events,
    ].slice(0, 12);
    renderCounts();
    renderEvents();
  }
}

function renderCounts() {
  acceptCountEl.textContent = String(acceptCount);
  rejectCountEl.textContent = String(rejectCount);
  turtleCountEl.textContent = String(Math.floor(acceptCount / 3));
}

function renderEvents() {
  displayEvents.innerHTML = events
    .slice(0, 3)
    .map(
      (event) => `
        <article class="event-card ${event.result}">
          <span class="metric-label">${event.time}</span>
          <strong>${resultLabel(event.result)}</strong>
          <p>conf ${event.confidence.toFixed(2)} · ${event.roast}</p>
        </article>
      `,
    )
    .join("");

  adminLog.innerHTML = events
    .map(
      (event) => `
        <tr>
          <td>${event.time}</td>
          <td>${resultLabel(event.result)}</td>
          <td>${event.confidence.toFixed(2)}</td>
          <td>${event.roast}</td>
        </tr>
      `,
    )
    .join("");
}

function exportCsv() {
  const rows = [["time", "result", "confidence", "roast"]];
  events.forEach((event) => {
    rows.push([event.time, event.result, event.confidence.toFixed(2), event.roast]);
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

tabButtons.forEach((button) => {
  button.addEventListener("click", () => {
    const target = button.dataset.view;
    tabButtons.forEach((item) => item.classList.toggle("active", item === button));
    views.forEach((view) => view.classList.toggle("active", view.id === `${target}-view`));
  });
});

document.querySelectorAll("[data-event]").forEach((button) => {
  button.addEventListener("click", () => setState(button.dataset.event));
});

thresholdControl.addEventListener("input", () => {
  thresholdValue.textContent = Number(thresholdControl.value).toFixed(2);
});

volumeControl.addEventListener("input", () => {
  volumeValue.textContent = `${volumeControl.value}%`;
});

document.querySelector("#export-csv").addEventListener("click", exportCsv);
document.querySelector("#simulate-stream").addEventListener("click", () => {
  const sequence = ["detect", "accept", "reject", "multi", "low"];
  const next = sequence[Math.floor(Math.random() * sequence.length)];
  setState(next);
});

renderCounts();
renderEvents();
setState("idle", false);
