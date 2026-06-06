# display

顯示與互動 UI — AI 情緒垃圾筒的 Display 子系統。

## 職責

- 使用者端公告螢幕：以大型相機畫面為主，顯示目前判定、今日統計、最近事件。
- 即時相機畫面：在瀏覽器中嵌入 live camera preview，配合辨識結果顯示紅 / 綠 / 黃光效。
- 海洋回饋視覺：依狀態切換生成海龜圖，`assets/sea-turtle-accept.png` 用於 accept，`assets/sea-turtle-reject.png` 用於 reject / multi / low，`assets/sea-turtle-display.png` 用於待機。
- Admin 控制面板：獨立於 `/admin`，顯示系統健康狀態、demo 模擬、CSV 匯出。
- 消費 vision 的 `recognition_result`，並更新狀態機與本機事件紀錄。
- 播放或交接 roast / accept 語音素材；沒有音檔時 UI 不失敗。

Display 不負責模型推論、L515 depth 距離觸發、使用者確認按鈕或公開的 firmware 指令回傳流程。

## 架構

正式整合仍維持中心契約 v0.3：

```text
firmware -- q_detected / user_detected --> vision -- q_result / recognition_result --> display
```

本 repo 新增一層本機 Python Web Bridge，只把 `q_result` 的事件轉給瀏覽器：

- `GET /`：Display UI。
- `GET /admin`：Admin 控制面板。
- `GET /events`：SSE stream，推送目前狀態與事件紀錄。
- `GET /api/state`：目前狀態 snapshot。
- `POST /api/simulate`：demo-only，產生 mock `recognition_result`。

這不是外部雲端 API，也不改三模組的 public contract。Web Bridge 的目的只是讓 Jetson、筆電投影、同 LAN 手機或另一台電腦能用瀏覽器觀看 Display。Admin 不放在公開 Display 畫面的 tab 裡，避免 demo 現場誤用控制項。

## 使用方式

單獨跑 Display UI：

```bash
python3 server.py --host 0.0.0.0 --port 8080
```

本機開：

```text
http://localhost:8080
```

Admin 開：

```text
http://localhost:8080/admin
```

同 LAN 遠端瀏覽器開：

```text
http://<jetson-ip>:8080
```

相機畫面使用瀏覽器 `getUserMedia`。`localhost` 可直接要求權限；同 LAN 遠端瀏覽器若使用 `http://<ip>`，部分瀏覽器會因非安全來源而阻擋相機權限。正式展示建議在接相機的 Jetson / 筆電本機瀏覽器開 Display，或另行配置 HTTPS。

整合時由 launcher 建立 `multiprocessing.Queue`，再呼叫 `run_display_server(q_result)`：

```python
from multiprocessing import Queue

from server import run_display_server

q_result = Queue()
run_display_server(q_result, host="0.0.0.0", port=8080)
```

## 狀態規則

- `confidence < 0.5`：顯示 low confidence，不增加 accept/reject 統計。
- `num_objects > 1`：顯示 multi-object reject，增加 reject 統計。
- `class == "accept"` 且信心足夠：顯示 accept，增加 accept 統計。
- `class == "reject"` 且信心足夠：顯示 reject，增加 reject 統計。
- result 狀態會進入 cooldown，再回到 idle。
- UI 邊框光效：accept 為綠光、reject / multi 為紅光、low confidence 為黃光。
- 原本的角色式視覺已移除；公開 Display 以相機畫面與判定光效為第一視覺。

## Demo Mock

畫面上的 Mock 按鈕只呼叫 `/api/simulate`，用來展示 `recognition_result` 進入 Display 後的畫面變化。正式 demo 接線時，事件來源應是 vision 放入 `q_result` 的 payload。

## 驗證

```bash
python3 -m unittest discover -s tests -v
```

詳細 API 契約見 monorepo `docs/api-contract.md`；本 repo 以中心 v0.3 contract 為準。
