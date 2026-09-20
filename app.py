"""
戰鬥陀螺 全通路雲端極速監控 Web 服務 (FastAPI + LINE Messaging API Webhook)
支援 7 大賣場：Amazon Japan / PChome 24h / M.M小舖 / 麗嬰官網 / 童無忌 / 誠品線上 / 蝦皮 Funbox
專為 Render.com 打造，提供手機分頁儀表板、LINE 雙向遙控與 24H 背景自動推播。
"""

import concurrent.futures
import json
import os
import sys
import threading
import time
from datetime import datetime
from typing import Dict, List, Any

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass


from fastapi import FastAPI, Request, BackgroundTasks, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from monitor_engine import (
    AmazonJPChecker, PChomeChecker, MMShopChecker,
    CyberbizChecker, EsliteChecker, ShopeeChecker,
    check_store_item, get_item_direct_url,
    STORE_CONFIG, NotificationManager, get_product_url, extract_asin
)

# 初始化目錄與檔案
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")

app = FastAPI(title="Beyblade Multi-Store Restock Monitor Cloud")
templates = Jinja2Templates(directory=TEMPLATES_DIR)

# 全域狀態
class MonitorState:
    def __init__(self):
        self.is_monitoring = True
        self.stop_event = threading.Event()
        self.monitor_thread = None
        self.logs: List[Dict[str, str]] = []
        self.max_logs = 70
        self.config: Dict[str, Any] = self.load_config()
        self.in_stock_state: Dict[str, bool] = {}

    def load_config(self) -> Dict[str, Any]:
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                    # 確保現有項目相容性：未標記 store 者預設為 amazon_jp
                    for it in cfg.get("items", []):
                        if "store" not in it:
                            it["store"] = "amazon_jp"
                    return cfg
            except Exception:
                pass
        return {
            "interval_seconds": 5,
            "concurrent_mode": True,
            "only_amazon_seller": True,
            "enable_discord": True,
            "enable_line": True,
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
        try:
            print(f"[{ts}] [{level}] {msg}")
        except Exception:
            try:
                safe_msg = msg.encode('ascii', errors='replace').decode()
                print(f"[{ts}] [{level}] {safe_msg}")
            except Exception:
                pass


state = MonitorState()


def background_monitor_worker():
    """雲端 24H 背景監控輪詢核心 (多平台並發)"""
    state.add_log("=== 雲端 24H 多賣場背景監控已啟動 ===", "SUCCESS")
    while not state.stop_event.is_set():
        items = state.config.get("items", [])
        active_items = [(i, it) for i, it in enumerate(items) if it.get("enabled", True) and it.get("asin")]

        if not active_items:
            time.sleep(5)
            continue

        state.add_log(f"⚡ 開始檢查 {len(active_items)} 項商品 (跨 7 大賣場)...", "INFO")
        
        def worker(item_tuple):
            if state.stop_event.is_set():
                return None
            idx, item = item_tuple
            res = check_store_item(item)
            return idx, item, res

        max_workers = min(len(active_items), 12)
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
    store = item.get("store", "amazon_jp")
    item_key = f"{store}_{asin}"

    if not res.get("ok"):
        msg = res.get("msg", "檢測異常")
        item["last_status"] = msg
        item["last_time"] = now_str
        state.add_log(f"[{name}] 檢查失敗: {msg}", "WARNING")
        return

    price = res.get("price", "-")
    seller = res.get("seller", "-")
    in_stock = res.get("in_stock", False)
    is_official = res.get("is_official", True)
    is_preorder = res.get("is_preorder", False)
    url = res.get("url") or get_item_direct_url(item)

    status_text = ""
    is_alert_worthy = False

    if in_stock:
        if store == "amazon_jp":
            if is_official:
                status_text = "🟢 官方現貨/預購" if not is_preorder else "🔵 官方開放預購"
                is_alert_worthy = True
            else:
                status_text = "🟡 第三方賣家現貨"
                if not state.config.get("only_amazon_seller", True):
                    is_alert_worthy = True
        else:
            status_text = res.get("status_text") or "🟢 平台現貨開放！"
            is_alert_worthy = True
    else:
        if res.get("status_text"):
            status_text = res["status_text"]
        elif res.get("no_featured_offer", False):
            status_text = "⚪ 暫無官方現貨 (僅第三方轉賣)"
        else:
            status_text = "⚪ 缺貨中 / 暫無庫存"

    item["last_status"] = status_text
    item["last_price"] = price
    item["last_seller"] = seller
    item["last_time"] = now_str
    state.save_config()

    prev_was_in_stock = state.in_stock_state.get(item_key, False)
    state.in_stock_state[item_key] = is_alert_worthy

    should_trigger = is_alert_worthy and (not prev_was_in_stock or is_manual)

    if is_alert_worthy:
        store_cfg = STORE_CONFIG.get(store, STORE_CONFIG["amazon_jp"])
        flag = store_cfg.get("flag", "⚡")
        state.add_log(f"🔥【補貨通知】{flag} {name} 價格: {price}！", "SUCCESS")

    if should_trigger:
        trigger_notifications(item, name, asin, price, seller, url)


def trigger_notifications(item: dict, name: str, asin: str, price: str, seller: str, url: str):
    """發送 Discord 與 LINE 官方推播 (包含賣場資訊與一鍵直達)"""
    store = item.get("store", "amazon_jp")
    store_cfg = STORE_CONFIG.get(store, STORE_CONFIG["amazon_jp"])
    store_name = store_cfg.get("name", "線上商城")
    flag = store_cfg.get("flag", "⚡")
    btn_text = store_cfg.get("btn_text", "👉 點此直達購買")

    # 1. Discord 推播
    if state.config.get("enable_discord", True):
        discord_url = state.config.get("discord_webhook", "").strip()
        if discord_url:
            def _send_d():
                ok, msg = NotificationManager.send_discord(discord_url, name, asin, price, seller, url, store=store)
                state.add_log(f"Discord ({store_name}): {msg}", "SUCCESS" if ok else "ERROR")
            threading.Thread(target=_send_d, daemon=True).start()

    # 2. LINE 官方帳號 Broadcast
    if state.config.get("enable_line", True):
        line_token = state.config.get("line_token", "").strip()
        if line_token:
            def _send_l():
                msg_body = (
                    f"\n🚨【戰鬥陀螺補貨通知】\n"
                    f"通路: {flag} {store_name}\n"
                    f"商品: {name}\n"
                    f"即時價格: {price}\n"
                    f"店家/賣家: {seller}\n"
                    f"🔥 {btn_text}:\n{url}"
                )
                ok, msg = NotificationManager.send_line_broadcast(line_token, msg_body)
                state.add_log(f"LINE ({store_name}): {msg}", "SUCCESS" if ok else "ERROR")
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


@app.on_event("startup")
def on_startup():
    state.add_log("🌪️ 戰鬥陀螺 7 大賣場雲端監控系統初始化...", "INFO")
    start_monitor()


# ================== API 路由 ==================

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """手機 Web 控制儀表板 (含分頁)"""
    return templates.TemplateResponse(request=request, name="index.html")


@app.get("/health")
async def health_check():
    """保活與健康檢查"""
    return {
        "status": "healthy",
        "monitoring": state.is_monitoring,
        "items_count": len(state.config.get("items", [])),
        "timestamp": datetime.now().isoformat()
    }


@app.get("/api/status")
async def get_status():
    """獲取最新即時狀態、商品清單、賣場定義與日誌"""
    items = state.config.get("items", [])
    # 統計各賣場商品數
    store_counts = {}
    for s_key in STORE_CONFIG.keys():
        store_counts[s_key] = 0
    for it in items:
        s = it.get("store", "amazon_jp")
        store_counts[s] = store_counts.get(s, 0) + 1

    return {
        "is_monitoring": state.is_monitoring,
        "interval_seconds": state.config.get("interval_seconds", 5),
        "only_amazon_seller": state.config.get("only_amazon_seller", True),
        "enable_discord": state.config.get("enable_discord", True),
        "enable_line": state.config.get("enable_line", True),
        "discord_webhook": state.config.get("discord_webhook", ""),
        "line_token": state.config.get("line_token", ""),
        "stores": STORE_CONFIG,
        "store_counts": store_counts,
        "items": items,
        "logs": state.logs
    }


@app.post("/api/settings")
async def api_update_settings(req: Request):
    """更新全域推播與監控設定"""
    data = await req.json()
    if "interval_seconds" in data:
        state.config["interval_seconds"] = max(2, int(data["interval_seconds"]))
    if "only_amazon_seller" in data:
        state.config["only_amazon_seller"] = bool(data["only_amazon_seller"])
    if "enable_discord" in data:
        state.config["enable_discord"] = bool(data["enable_discord"])
    if "enable_line" in data:
        state.config["enable_line"] = bool(data["enable_line"])
    if "discord_webhook" in data:
        state.config["discord_webhook"] = str(data["discord_webhook"]).strip()
    if "line_token" in data:
        state.config["line_token"] = str(data["line_token"]).strip()
    state.save_config()
    state.add_log("⚙️ 雲端推播與系統設定已儲存！", "SUCCESS")
    return {"ok": True, "config": state.config}


@app.post("/api/add_item")
async def api_add_item(req: Request, background_tasks: BackgroundTasks):
    """新增監控商品 (跨 7 大賣場)"""
    data = await req.json()
    store = data.get("store", "amazon_jp").strip()
    if store not in STORE_CONFIG:
        store = "amazon_jp"

    name = str(data.get("name", "")).strip()
    raw_input = str(data.get("asin", "")).strip()
    note = str(data.get("note", "")).strip()

    if not raw_input:
        return JSONResponse({"ok": False, "msg": "請輸入商品編號或網址！"}, status_code=400)

    # 針對不同賣場標準化 ID 提取
    if store == "amazon_jp":
        asin = extract_asin(raw_input)
    elif store == "pchome":
        asin = PChomeChecker.extract_prod_id(raw_input)
    elif store == "mm_shop":
        asin = MMShopChecker.extract_item_id(raw_input)
    elif store in ("funbox_tw", "twj_toys"):
        asin = CyberbizChecker.extract_slug(raw_input)
    elif store == "eslite":
        asin = EsliteChecker.extract_id(raw_input)
    elif store == "shopee":
        sp, it = ShopeeChecker.extract_ids(raw_input)
        asin = f"{sp}_{it}" if sp and it else raw_input
    else:
        asin = raw_input

    if not asin:
        asin = raw_input

    if not name:
        name = f"新商品 ({asin[:15]})"

    new_item = {
        "enabled": True,
        "store": store,
        "name": name,
        "asin": asin,
        "note": note,
        "last_status": "待檢查",
        "last_price": "-",
        "last_seller": "-",
        "last_time": "-"
    }
    state.config.setdefault("items", []).append(new_item)
    new_idx = len(state.config["items"]) - 1
    state.save_config()
    
    store_name = STORE_CONFIG[store]["short_name"]
    state.add_log(f"➕ 已新增追蹤商品: [{store_name}] {name} ({asin})", "SUCCESS")

    def _check_one():
        res = check_store_item(new_item)
        if res.get("ok") and res.get("title") and ("新商品 (" in new_item["name"]):
            new_item["name"] = res["title"]
            state.save_config()
        handle_result(new_idx, new_item, res, is_manual=True)

    background_tasks.add_task(_check_one)
    return {"ok": True, "item": new_item}


@app.post("/api/edit_item")
async def api_edit_item(req: Request, background_tasks: BackgroundTasks):
    """編輯現有商品"""
    data = await req.json()
    idx = int(data.get("index", -1))
    items = state.config.get("items", [])
    if not (0 <= idx < len(items)):
        return JSONResponse({"ok": False, "msg": "無效商品編號！"}, status_code=400)

    store = data.get("store", items[idx].get("store", "amazon_jp")).strip()
    if store not in STORE_CONFIG:
        store = "amazon_jp"

    name = str(data.get("name", "")).strip()
    raw_input = str(data.get("asin", "")).strip()
    note = str(data.get("note", "")).strip()

    if not name:
        return JSONResponse({"ok": False, "msg": "請輸入商品名稱或型號！"}, status_code=400)

    if store == "amazon_jp":
        asin = extract_asin(raw_input)
    elif store == "pchome":
        asin = PChomeChecker.extract_prod_id(raw_input)
    elif store == "mm_shop":
        asin = MMShopChecker.extract_item_id(raw_input)
    elif store in ("funbox_tw", "twj_toys"):
        asin = CyberbizChecker.extract_slug(raw_input)
    elif store == "eslite":
        asin = EsliteChecker.extract_id(raw_input)
    elif store == "shopee":
        sp, it = ShopeeChecker.extract_ids(raw_input)
        asin = f"{sp}_{it}" if sp and it else raw_input
    else:
        asin = raw_input

    item = items[idx]
    item["store"] = store
    item["name"] = name
    item["asin"] = asin
    item["note"] = note
    state.save_config()
    
    store_name = STORE_CONFIG[store]["short_name"]
    state.add_log(f"✏️ 已更新商品: [{store_name}] {name} ({asin})", "INFO")

    def _check_one():
        res = check_store_item(item)
        handle_result(idx, item, res, is_manual=True)

    background_tasks.add_task(_check_one)
    return {"ok": True, "item": item}


@app.post("/api/delete_item")
async def api_delete_item(index: int = Query(...)):
    """刪除指定商品"""
    items = state.config.get("items", [])
    if 0 <= index < len(items):
        deleted = items.pop(index)
        state.save_config()
        state.add_log(f"🗑️ 已刪除商品: {deleted.get('name')}", "INFO")
        return {"ok": True, "msg": "已刪除"}
    return JSONResponse({"ok": False, "msg": "無效商品索引"}, status_code=400)


@app.post("/api/test_discord")
async def api_test_discord(req: Request):
    """從 Web 介面測試 Discord 推播"""
    data = await req.json()
    webhook_url = data.get("webhook_url", "").strip() or state.config.get("discord_webhook", "").strip()
    if not webhook_url:
        return JSONResponse({"ok": False, "msg": "請填寫 Discord Webhook 網址！"}, status_code=400)

    prod_url = get_product_url("B0H861Y9Y3")
    ok, msg = NotificationManager.send_discord(
        webhook_url,
        "【測試】UX-21 赫爾茲地獄 (雲端多賣場推播測試)",
        "B0H861Y9Y3",
        "￥4,500",
        "Amazon.co.jp (官方自營)",
        prod_url,
        store="amazon_jp"
    )
    return {"ok": ok, "msg": msg}


@app.post("/api/test_line")
async def api_test_line(req: Request):
    """從 Web 介面測試 LINE 推播"""
    data = await req.json()
    line_token = data.get("line_token", "").strip() or state.config.get("line_token", "").strip()
    if not line_token:
        return JSONResponse({"ok": False, "msg": "請填寫 LINE Token 或 Channel ID:Secret！"}, status_code=400)

    prod_url = get_product_url("B0H861Y9Y3")
    msg_body = f"\n🚨【戰鬥陀螺補貨通知測試】\n通路: 🇯🇵 Amazon Japan\n商品: 【測試】UX-21 赫爾茲地獄\n價格: ￥4,500\n賣家: Amazon.co.jp (官方自營)\n🔥 1-Click 官方直達:\n{prod_url}"
    ok, msg = NotificationManager.send_line_broadcast(line_token, msg_body)
    return {"ok": ok, "msg": msg}


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
    """立即多線程並發全檢 (跨 7 大賣場秒級檢查)"""
    def _do_check():
        items = state.config.get("items", [])
        active_items = [(i, it) for i, it in enumerate(items) if it.get("enabled", True) and it.get("asin")]
        state.add_log(f"⚡ 立即並發全檢 ({len(active_items)} 項商品)...", "INFO")
        t0 = time.time()

        def worker(item_tuple):
            idx, item = item_tuple
            res = check_store_item(item)
            return idx, item, res

        max_workers = min(len(active_items), 12)
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

            if user_text in ("開始", "啟動", "start", "run"):
                start_monitor()
                reply_msg = "🟢【雲端監控已啟動】\n系統正在 24 小時為您跨 7 大賣場監控戰鬥陀螺，有貨將第一時間推播！"
            elif user_text in ("停止", "暫停", "stop", "pause"):
                stop_monitor()
                reply_msg = "⏸【雲端監控已暫停】\n已停止背景檢查，需要時請輸入「開始」重新啟動。"
            elif user_text in ("查庫存", "狀態", "status", "庫存"):
                items = state.config.get("items", [])
                lines = ["📋【戰鬥陀螺 7 大賣場庫存概況】"]
                
                # 分組統計
                by_store = {}
                for it in items:
                    s = it.get("store", "amazon_jp")
                    by_store.setdefault(s, []).append(it)
                
                for s_key, s_cfg in STORE_CONFIG.items():
                    s_items = by_store.get(s_key, [])
                    if not s_items:
                        continue
                    lines.append(f"\n{s_cfg['flag']} 【{s_cfg['name']}】({len(s_items)}項):")
                    for it in s_items[:4]:
                        name = it.get("name", "")[:14]
                        status = it.get("last_status", "待檢查")
                        price = it.get("last_price", "-")
                        lines.append(f"• {name} | {status} | {price}")
                    if len(s_items) > 4:
                        lines.append(f"  ...還有 {len(s_items)-4} 項請上網頁查看")
                
                lines.append("\n💡 點擊任一推播即可直達該賣場下單！")
                reply_msg = "\n".join(lines)
            elif user_text in ("全檢", "檢查", "check"):
                def _do_line_check():
                    active_items = [(i, it) for i, it in enumerate(state.config.get("items", [])) if it.get("enabled", True) and it.get("asin")]
                    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                        futures = [executor.submit(lambda it: (it[0], it[1], check_store_item(it[1])), it) for it in active_items]
                        for f in concurrent.futures.as_completed(futures):
                            idx, item, res = f.result()
                            handle_result(idx, item, res, is_manual=True)
                threading.Thread(target=_do_line_check, daemon=True).start()
                reply_msg = "⚡ 已開始為您並發全檢 7 大賣場的所有陀螺商品！最新結果可在聊天室或 Web 儀表板查看。"
            else:
                reply_msg = (
                    "🤖【戰鬥陀螺雲端監控助手】\n"
                    "支援 7 大電商賣場即時秒殺！\n"
                    "👉 傳送「開始」：啟動 24H 雲端監控\n"
                    "👉 傳送「停止」：暫停雲端監控\n"
                    "👉 傳送「查庫存」：依賣場查看最新庫存\n"
                    "👉 傳送「全檢」：立即重新檢查所有商品"
                )

            if reply_token and line_token:
                NotificationManager.reply_line(reply_token, line_token, reply_msg)

    return JSONResponse({"status": "ok"})


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)
