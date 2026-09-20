"""
戰鬥陀螺 Amazon Japan 雲端極速監控 Web 服務 (FastAPI + LINE Messaging API Webhook)
專為 Render.com 打造，提供手機 Web 儀表板、LINE 聊天室雙向遙控與 24H 背景自動推播。
"""

import concurrent.futures
import json
import os
import threading
import time
from datetime import datetime
from typing import Dict, List, Any

from fastapi import FastAPI, Request, BackgroundTasks, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from monitor_engine import AmazonJPChecker, NotificationManager, get_product_url

# 初始化目錄與檔案
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")

app = FastAPI(title="Beyblade Restock Monitor Cloud")
templates = Jinja2Templates(directory=TEMPLATES_DIR)

# 全域狀態
class MonitorState:
    def __init__(self):
        self.is_monitoring = True  # 雲端部署預設自動啟動監控
        self.stop_event = threading.Event()
        self.monitor_thread = None
        self.logs: List[Dict[str, str]] = []
        self.max_logs = 60
        self.config: Dict[str, Any] = self.load_config()
        self.in_stock_state: Dict[str, bool] = {}

    def load_config(self) -> Dict[str, Any]:
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {
            "interval_seconds": 5,
            "concurrent_mode": True,
            "only_amazon_seller": True,
            "discord_webhook": "",
            "line_token": "",
            "items": []
        }

    def save_config(self):
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(self.config, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def add_log(self, msg: str, level: str = "INFO"):
        ts = datetime.now().strftime("%H:%M:%S")
        self.logs.append({"time": ts, "msg": msg, "level": level})
        if len(self.logs) > self.max_logs:
            self.logs = self.logs[-self.max_logs:]
        print(f"[{ts}] [{level}] {msg}")

state = MonitorState()


def background_monitor_worker():
    """雲端 24H 背景監控輪詢核心"""
    state.add_log("=== 雲端 24H 背景監控線程已啟動 ===", "SUCCESS")
    while not state.stop_event.is_set():
        items = state.config.get("items", [])
        active_items = [(i, it) for i, it in enumerate(items) if it.get("enabled", True) and it.get("asin")]

        if not active_items:
            state.add_log("目前無啟用之監控商品", "WARNING")
            time.sleep(5)
            continue

        state.add_log(f"⚡ 開始檢查 {len(active_items)} 項商品...", "INFO")
        
        def worker(item_tuple):
            if state.stop_event.is_set():
                return None
            idx, item = item_tuple
            res = AmazonJPChecker.check_asin(item.get("asin"))
            return idx, item, res

        max_workers = min(len(active_items), 10)
        t0 = time.time()
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(worker, it) for it in active_items]
            for f in concurrent.futures.as_completed(futures):
                if state.stop_event.is_set():
                    break
                result = f.result()
                if result:
                    idx, item, res = result
                    handle_result(idx, item, res)

        dt = time.time() - t0
        state.add_log(f"⚡ 本輪同時檢查完成 (耗時 {dt:.2f} 秒)", "INFO")

        interval = max(3, state.config.get("interval_seconds", 5))
        for _ in range(int(interval * 10)):
            if state.stop_event.is_set():
                break
            time.sleep(0.1)


def handle_result(idx: int, item: dict, res: dict, is_manual: bool = False):
    now_str = datetime.now().strftime("%H:%M:%S")
    name = item.get("name")
    asin = item.get("asin")

    if not res.get("ok"):
        msg = res.get("msg", "未知錯誤")
        item["last_status"] = msg
        item["last_time"] = now_str
        state.add_log(f"[{name}] 檢查失敗: {msg}", "WARNING")
        return

    price = res.get("price", "-")
    seller = res.get("seller", "-")
    in_stock = res.get("in_stock", False)
    is_official = res.get("is_official", False)
    is_preorder = res.get("is_preorder", False)
    url = res.get("url", get_product_url(asin))

    status_text = ""
    is_alert_worthy = False

    if in_stock:
        if is_official:
            status_text = "🟢 官方現貨/預購" if not is_preorder else "🔵 官方開放預購"
            is_alert_worthy = True
        else:
            status_text = "🟡 第三方賣家現貨"
            if not state.config.get("only_amazon_seller", True):
                is_alert_worthy = True
    else:
        if res.get("no_featured_offer", False):
            status_text = "⚪ 暫無官方現貨 (僅第三方轉賣)"
        else:
            status_text = "⚪ 缺貨中 / 暫無在庫"

    item["last_status"] = status_text
    item["last_price"] = price
    item["last_seller"] = seller
    item["last_time"] = now_str
    state.save_config()

    prev_was_in_stock = state.in_stock_state.get(asin, False)
    state.in_stock_state[asin] = is_alert_worthy

    should_trigger = is_alert_worthy and (not prev_was_in_stock or is_manual)

    if is_alert_worthy:
        state.add_log(f"🔥【官方補貨】{name} ({asin}) 價格: {price}！", "SUCCESS")
    else:
        pass

    if should_trigger:
        trigger_notifications(name, asin, price, seller, url)


def trigger_notifications(name: str, asin: str, price: str, seller: str, url: str):
    """發送 Discord 與 LINE 官方推播"""
    product_url = get_product_url(asin)

    # 1. Discord 推播
    discord_url = state.config.get("discord_webhook", "").strip()
    if discord_url:
        def _send_d():
            ok, msg = NotificationManager.send_discord(discord_url, name, asin, price, seller, product_url)
            state.add_log(f"Discord: {msg}", "SUCCESS" if ok else "ERROR")
        threading.Thread(target=_send_d, daemon=True).start()

    # 2. LINE 官方帳號 Broadcast
    line_token = state.config.get("line_token", "").strip()
    if line_token:
        def _send_l():
            msg_body = (
                f"\n🚨【戰鬥陀螺官方補貨通知】\n"
                f"商品: {name}\n"
                f"官方價格: {price}\n"
                f"販售賣家: {seller}\n"
                f"🔥 1-Click 直達秒殺:\n{product_url}"
            )
            ok, msg = NotificationManager.send_line_broadcast(line_token, msg_body)
            state.add_log(f"LINE: {msg}", "SUCCESS" if ok else "ERROR")
        threading.Thread(target=_send_l, daemon=True).start()


def start_monitor():
    if not state.is_monitoring or state.monitor_thread is None or not state.monitor_thread.is_alive():
        state.is_monitoring = True
        state.stop_event.clear()
        state.monitor_thread = threading.Thread(target=background_monitor_worker, daemon=True)
        state.monitor_thread.start()


def stop_monitor():
    state.is_monitoring = False
    state.stop_event.set()
    state.add_log("=== 雲端監控線程已停止 ===", "INFO")


# 啟動時自動開始監控
@app.on_event("startup")
def on_startup():
    state.add_log("🌪️ 戰鬥陀螺雲端監控系統初始化...", "INFO")
    start_monitor()


# ================== API 路由 ==================

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """手機 Web 控制儀表板"""
    return templates.TemplateResponse(request=request, name="index.html")


@app.get("/health")
async def health_check():
    """保活與監控健康檢查 (用於 UptimeRobot 免費 Keep-Alive)"""
    return {
        "status": "healthy",
        "monitoring": state.is_monitoring,
        "items_count": len(state.config.get("items", [])),
        "timestamp": datetime.now().isoformat()
    }


@app.get("/api/status")
async def get_status():
    """獲取最新即時狀態與日誌"""
    return {
        "is_monitoring": state.is_monitoring,
        "interval_seconds": state.config.get("interval_seconds", 5),
        "items": state.config.get("items", []),
        "logs": state.logs
    }


@app.post("/api/start")
async def api_start():
    start_monitor()
    return {"ok": True, "status": "running"}


@app.post("/api/stop")
async def api_stop():
    stop_monitor()
    return {"ok": True, "status": "stopped"}


@app.post("/api/toggle_item")
async def api_toggle_item(index: int = Query(...)):
    items = state.config.get("items", [])
    if 0 <= index < len(items):
        items[index]["enabled"] = not items[index].get("enabled", True)
        state.save_config()
        state.add_log(f"已切換商品狀態: {items[index].get('name')}", "INFO")
        return {"ok": True, "enabled": items[index]["enabled"]}
    return {"ok": False, "msg": "無效索引"}


@app.post("/api/check_now")
async def api_check_now(background_tasks: BackgroundTasks):
    """立即多線程並發檢查所有項目 (同桌面版極速秒查)"""
    def _do_check():
        items = state.config.get("items", [])
        active_items = [(i, it) for i, it in enumerate(items) if it.get("enabled", True) and it.get("asin")]
        state.add_log(f"⚡ 立即並發全檢 ({len(active_items)} 項商品)...", "INFO")
        t0 = time.time()

        def worker(item_tuple):
            idx, item = item_tuple
            res = AmazonJPChecker.check_asin(item.get("asin"))
            return idx, item, res

        max_workers = min(len(active_items), 10)
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(worker, it) for it in active_items]
            for f in concurrent.futures.as_completed(futures):
                result = f.result()
                if result:
                    idx, item, res = result
                    handle_result(idx, item, res, is_manual=True)

        dt = time.time() - t0
        state.add_log(f"⚡ 全部 {len(active_items)} 項商品檢查完成 (共耗時 {dt:.2f} 秒)！", "SUCCESS")

    background_tasks.add_task(_do_check)
    return {"ok": True, "msg": "已開始極速並發全檢"}


# ================== LINE 聊天室雙向遙控 Webhook ==================

@app.post("/webhook/line")
async def line_webhook(request: Request):
    """接收 LINE 官方帳號的使用者對話訊息"""
    try:
        body = await request.body()
        data = json.loads(body.decode("utf-8"))
    except Exception:
        return JSONResponse({"status": "error", "msg": "Invalid JSON"}, status_code=400)

    events = data.get("events", [])
    line_token = state.config.get("line_token", "").strip()

    for ev in events:
        if ev.get("type") == "message" and ev.get("message", {}).get("type") == "text":
            reply_token = ev.get("replyToken")
            user_text = ev.get("message", {}).get("text", "").strip().lower()

            # 指令判斷
            if user_text in ("開始", "啟動", "start", "run"):
                start_monitor()
                reply_msg = "🟢【雲端監控已啟動】\n系統正在 24 小時為您監控 Amazon 官方現貨，有貨將第一時間推播！"
            elif user_text in ("停止", "暫停", "stop", "pause"):
                stop_monitor()
                reply_msg = "⏸【雲端監控已暫停】\n已停止背景檢查，需要時請輸入「開始」重新啟動。"
            elif user_text in ("查庫存", "狀態", "status", "庫存"):
                items = state.config.get("items", [])
                lines = ["📋【戰鬥陀螺即時庫存概況】"]
                for it in items:
                    name = it.get("name", "")[:15]
                    status = it.get("last_status", "待檢查")
                    price = it.get("last_price", "-")
                    lines.append(f"• {name}\n  狀態: {status} | 價格: {price}")
                lines.append("\n💡 點擊任一商品推播即可 1-Click 秒殺！")
                reply_msg = "\n".join(lines)
            elif user_text in ("全檢", "檢查", "check"):
                def _do_line_check():
                    active_items = [(i, it) for i, it in enumerate(state.config.get("items", [])) if it.get("enabled", True) and it.get("asin")]
                    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                        futures = [executor.submit(lambda it: (it[0], it[1], AmazonJPChecker.check_asin(it[1].get("asin"))), it) for it in active_items]
                        for f in concurrent.futures.as_completed(futures):
                            idx, item, res = f.result()
                            handle_result(idx, item, res, is_manual=True)
                threading.Thread(target=_do_line_check, daemon=True).start()
                reply_msg = "⚡ 已開始為您極速並發檢查所有陀螺！最新結果可在聊天室或 Web 儀表板查看。"
            else:
                reply_msg = (
                    "🤖【戰鬥陀螺雲端監控助手】\n"
                    "您可以傳送以下指令進行遙控：\n"
                    "👉 傳送「開始」：啟動 24H 雲端監控\n"
                    "👉 傳送「停止」：暫停雲端監控\n"
                    "👉 傳送「查庫存」：查看 11 款商品最新狀態\n"
                    "👉 傳送「全檢」：立即重新檢查所有商品"
                )

            if reply_token and line_token:
                NotificationManager.reply_line(reply_token, line_token, reply_msg)

    return JSONResponse({"status": "ok"})


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)
