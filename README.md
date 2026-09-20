# 🌪️ 戰鬥陀螺 Amazon Japan 雲端 24H 極速監控系統 (Render.com 部署指南)

專為 **Render.com** 雲端平台打造的 24 小時長駐戰鬥陀螺 (Beyblade X) 補貨監控服務。  
包含 **手機 Web 控制儀表板 (PWA)**、**LINE 官方帳號雙向聊天室遙控** 與 **Discord / LINE 官方自營現貨即時推播**。

---

## 🚀 部署至 Render.com 快速指南 (只需 3 分鐘)

### 步驟 1：建立 GitHub 儲存庫
1. 前往 [GitHub.com](https://github.com/) 登入您的帳號。
2. 點擊右上角 **+** ➡️ **New repository**。
3. Repository name 輸入 `beyblade-cloud-monitor`，選擇 **Private** (私人)，點擊 **Create repository**。
4. 將本資料夾（`cloud_deploy`）內的檔案 push 上傳至該儲存庫。

### 步驟 2：在 Render.com 建立免費 Web 服務
1. 前往 [Render.com](https://render.com/) 登入或註冊帳號。
2. 在控制面板右上角點擊 **New +** ➡️ 選擇 **Web Service**。
3. 連結您的 GitHub 帳號並選取剛建立的 `beyblade-cloud-monitor` 儲存庫。
4. 設定基本參數：
   - **Name**: `beyblade-monitor` (或自訂名稱)
   - **Region**: 建議選 `Singapore` 或 `Oregon` (連日本 Amazon 極快)
   - **Language**: `Python 3`
   - **Branch**: `main`
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `uvicorn app:app --host 0.0.0.0 --port $PORT`
   - **Instance Type**: 選擇 **Free**
5. 點擊最下方 **Deploy Web Service** 開始自動建置！

---

## 📱 串接 LINE 官方帳號 Webhook (手機打字遙控)

部署完成後，Render 會為您生成一組專屬 HTTPS 網址（例如：`https://beyblade-monitor.onrender.com`）。

1. 前往 [LINE Developers Console](https://developers.line.biz/console/)。
2. 進入您的官方帳號 Provider ➡️ 點選您的 Channel。
3. 在頁籤中找到 **「Webhook 網址 (Webhook URL)」**（就在您剛才截圖的位置）：
   - 填入：`https://您的Render網址.onrender.com/webhook/line`
   - 點擊 **Update (儲存)**，並點擊旁邊的 **Verify (驗證)**（回傳 Success 即代表成功！）。
4. 在下方開啟 **「Use webhook」** 開關（切換為啟用綠色）。

---

## 💬 手機 LINE 聊天室指令清單

只要在手機的 LINE 官方帳號聊天室中傳送以下文字，雲端主機即會自動執行：

| 傳送指令 | 說明 |
| :--- | :--- |
| `開始` 或 `start` | 啟動雲端 24H 背景極速監控 |
| `停止` 或 `stop` | 暫停雲端監控 |
| `查庫存` 或 `狀態` | 即時回傳目前 11 款陀螺的官方在庫狀態與價格 |
| `全檢` 或 `check` | 立即並發檢查所有陀螺最新庫存 |

---

## 💡 免費保活技巧 (Keep-Alive 24H 永不休眠)

Render Free 方案若 15 分鐘無連線會自動休眠。本專案已內建 `/health` 保活端點：
1. 前往免費定時監控網站 [Cron-job.org](https://cron-job.org/) 或 [UptimeRobot](https://uptimerobot.com/) 註冊免費帳號。
2. 新增一個 Monitor：
   - URL: `https://您的Render網址.onrender.com/health`
   - 間隔：設定為每 **10 分鐘** Ping 一次。
3. 這樣 Render 伺服器就會 **24 小時持續運作永不休眠**！
