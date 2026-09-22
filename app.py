"""
戰鬥陀螺 全通路雲端極速監控 Web 服務 (FastAPI + LINE Messaging API Webhook)
支援 7 大賣場：Amazon Japan / PChome 24h / M.M小舖 / 麗嬰官網 / 童無忌 / 誠品線上 / 蝦皮 Funbox
專為 Render.com 打造，提供手機分頁儀表板、各賣場獨立 Discord Webhook & 推播開關、LINE 雙向遙控與 24H 背景自動推播。
"""

import concurrent.futures
import gc
import json
import os
import random
import sys
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Any

TZ_GMT8 = timezone(timedelta(hours=8))

def get_now_gmt8() -> datetime:
    return datetime.now(TZ_GMT8)

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
    STORE_CONFIG, NotificationManager, get_product_url, extract_asin,
    PLAYWRIGHT_AVAILABLE, cffi_requests, test_proxy_connection,
    KeepaChecker
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
        self.store_threads: Dict[str, threading.Thread] = {}
        self._save_lock = threading.Lock()
        self.logs: List[Dict[str, str]] = []
        self.max_logs = 80
        self.config: Dict[str, Any] = self.load_config()
        self.in_stock_state: Dict[str, bool] = {}

    def load_config(self) -> Dict[str, Any]:
        cfg = {}
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
            except Exception:
                cfg = {}

        # 基礎預設值
        cfg.setdefault("interval_seconds", 5)
        cfg.setdefault("concurrent_mode", True)
        cfg.setdefault("only_amazon_seller", True)
        cfg.setdefault("enable_discord", True)
        cfg.setdefault("enable_line", True)
        cfg.setdefault("discord_webhook", "")
        cfg.setdefault("line_token", "")
        cfg.setdefault("proxy_url", "")
        cfg.setdefault("proxy_enabled", True)
        cfg.setdefault("amazon_custom_cookie", "")
        cfg.setdefault("keepa_api_key", "")
        cfg.setdefault("keepa_enabled", bool(cfg.get("keepa_api_key")))
        cfg.setdefault("keepa_mode", "fallback")
        cfg.setdefault("items", [])
        cfg.setdefault("store_settings", {})

        # 預設各賣場專屬 Webhook
        preset_hooks = {
            "pchome": "https://ptb.discord.com/api/webhooks/1551281208799793233/0mXgFbPr6LgHNVtlILCYmDbw6lGyNfgvyS6EeCcWS5pjcGVl5RGksuOVbI3QeTXryKc7",
            "mm_shop": "https://ptb.discord.com/api/webhooks/1551281302509068400/OarsOGYXf_G7RqY2tA5FgB8x4e6iGW0ngnkCbxwVrdI9ougiI7VL3vq_6m9Rdp-EDsl3",
            "funbox_tw": "https://ptb.discord.com/api/webhooks/1551281372901937313/LMmnfrhoDdtO5WFK_Ppzo3hyrMBM-prQiiBIM4G2Bu5DzH9kAzvv7HhnjWWJ-ZJ8Lens",
            "twj_toys": "https://ptb.discord.com/api/webhooks/1551281418661920822/0E1F8TSdfg2oekkcfm8OTwnCp9f5PVFYFpMfMjSgP6EZhiqSZjGojAnIAfqk6Ato2BA9",
            "shopee": "https://ptb.discord.com/api/webhooks/1551281517194514544/ISz6kbL3ODvED505ByVNPHNOvq6pThpH7F_u2v1_L4_LqjhCWCt7phi2p_9yPox2hRiK",
            "amazon_stealth": cfg.get("discord_webhook", "")
        }

        # 確保現有項目相容性：未標記 store 者預設為 amazon_jp
        for it in cfg.get("items", []):
            if "store" not in it:
                it["store"] = "amazon_jp"

        # 確保 8 大賣場皆有獨立推播與頻率設定
        for s_key in STORE_CONFIG.keys():
            if s_key not in cfg["store_settings"]:
                default_wh = preset_hooks.get(s_key, "")
                if s_key in ("amazon_jp", "amazon_stealth") and not default_wh:
                    default_wh = cfg.get("discord_webhook", "")
                cfg["store_settings"][s_key] = {
                    "enable_monitoring": True,
                    "enable_notifications": True,
                    "discord_webhook": default_wh,
                    "item_interval_seconds": 0.6 if s_key in ("amazon_jp", "amazon_stealth") else 3.0
                }
            else:
                cfg["store_settings"][s_key].setdefault("enable_monitoring", True)
                cfg["store_settings"][s_key].setdefault("enable_notifications", True)
                if not cfg["store_settings"][s_key].get("discord_webhook"):
                    cfg["store_settings"][s_key]["discord_webhook"] = preset_hooks.get(s_key, "")
                cfg["store_settings"][s_key].setdefault("item_interval_seconds", 0.6 if s_key in ("amazon_jp", "amazon_stealth") else 3.0)

        return cfg

    def is_store_monitored(self, store: str) -> bool:
        """檢查特定賣場是否開啟了監控 (True 表示要爬取，False 表示略過)"""
        s_set = self.config.get("store_settings", {}).get(store, {})
        return s_set.get("enable_monitoring", True)

    def get_amazon_item_interval(self) -> float:
        """獲取 Amazon 每項商品檢查間隔 (秒，預設 0.6 秒)"""
        s_val = self.config.get("store_settings", {}).get("amazon_jp", {}).get("item_interval_seconds")
        if s_val is not None:
            try:
                return max(0.1, float(s_val))
            except (ValueError, TypeError):
                pass
        r_val = self.config.get("amazon_item_interval", 0.6)
        try:
            return max(0.1, float(r_val))
        except (ValueError, TypeError):
            return 0.6

    def get_amazon_jitter(self) -> bool:
        """獲取 Amazon 是否啟用隨機延遲 Jitter (預設 True)"""
        s_val = self.config.get("store_settings", {}).get("amazon_jp", {}).get("enable_jitter")
        if s_val is not None:
            return bool(s_val)
        return bool(self.config.get("amazon_jitter", True))

    def get_amazon_use_playwright(self) -> bool:
        """獲取 Amazon 是否啟用 Playwright 真實瀏覽器內核防檢測 (預設 False，優先使用 curl_cffi TLS 偽裝)"""
        s_val = self.config.get("store_settings", {}).get("amazon_jp", {}).get("use_playwright")
        if s_val is not None:
            return bool(s_val)
        return bool(self.config.get("amazon_use_playwright", False))

    def get_proxy_url(self) -> str:
        """獲取全域或 Amazon 專用住宅代理 (Residential Proxy)"""
        if not self.config.get("proxy_enabled", True):
            return ""
        return self.config.get("proxy_url", "").strip()

    def get_amazon_custom_cookie(self) -> str:
        """獲取 Amazon 自訂登入 Cookie (解決 503 與 CAPTCHA)"""
        return self.config.get("amazon_custom_cookie", "").strip()

    def get_keepa_api_key(self) -> str:
        """獲取 Keepa 官方 API Key"""
        if not self.config.get("keepa_enabled", True):
            return ""
        return self.config.get("keepa_api_key", "").strip()

    def get_keepa_mode(self) -> str:
        """獲取 Keepa 運作模式: fallback (自動降級備援) | primary (優先使用) | disabled (停用)"""
        return self.config.get("keepa_mode", "fallback").strip()

    def save_config(self):
        with self._save_lock:
            try:
                with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                    json.dump(self.config, f, ensure_ascii=False, indent=2)
            except Exception:
                pass

    def add_log(self, msg: str, level: str = "INFO"):
        ts = get_now_gmt8().strftime("%H:%M:%S")
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


def check_items_for_store(store_key: str, is_manual: bool = False):
    """專門為單一賣場執行的極速檢查函式 (各賣場完全隔離、獨立抓價、互不干擾)"""
    if store_key not in STORE_CONFIG:
        return
    s_cfg = STORE_CONFIG[store_key]
    s_name = s_cfg["short_name"]

    items = state.config.get("items", [])
    store_items = []
    for idx, it in enumerate(items):
        if not it.get("enabled", True) or not it.get("asin"):
            continue
        if it.get("store", "amazon_jp") == store_key:
            store_items.append((idx, it))

    if not store_items:
        if is_manual:
            state.add_log(f"提示: [{s_name}] 目前無啟用監控的商品", "INFO")
        return

    amazon_delay = state.get_amazon_item_interval()
    amazon_jitter = state.get_amazon_jitter()
    proxy = state.get_proxy_url()
    only_official = state.config.get("only_amazon_seller", True)
    custom_cookie = state.get_amazon_custom_cookie() if store_key in ("amazon_jp", "amazon_stealth") else None
    keepa_key = state.get_keepa_api_key() if store_key in ("amazon_jp", "amazon_stealth") else None
    keepa_mode = state.get_keepa_mode() if store_key in ("amazon_jp", "amazon_stealth") else "fallback"

    extras = []
    if store_key in ("amazon_jp", "amazon_stealth"):
        if proxy:
            extras.append("住宅代理")
        if custom_cookie:
            extras.append("自訂Cookie")
        if keepa_key and keepa_mode != "disabled":
            extras.append(f"Keepa {keepa_mode}")
    tag = f" ({', '.join(extras)})" if extras else ""
    state.add_log(f"⚡ 檢查 [{s_name}] {len(store_items)} 項商品{tag}...", "INFO")
    t0 = time.time()

    def worker(item_tuple):
        if state.stop_event.is_set():
            return None
        idx, item = item_tuple
        res = check_store_item(
            item,
            amazon_interval=amazon_delay,
            amazon_jitter=amazon_jitter,
            proxy=proxy,
            only_amazon_seller=only_official,
            custom_cookie=custom_cookie,
            keepa_api_key=keepa_key,
            keepa_mode=keepa_mode
        )
        return idx, item, res

    max_workers = min(len(store_items), 4)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = []
        for it in store_items:
            if state.stop_event.is_set():
                break
            futures.append(executor.submit(worker, it))
            if store_key == "amazon_jp" and amazon_delay > 0:
                stagger = amazon_delay + (random.uniform(0.08, 0.25) if amazon_jitter else 0)
                time.sleep(stagger)

        for f in concurrent.futures.as_completed(futures):
            if state.stop_event.is_set():
                break
            result = f.result()
            if result:
                idx, item, res = result
                handle_result(idx, item, res, is_manual=is_manual)

    dt = time.time() - t0
    state.add_log(f"⚡ [{s_name}] 檢查完成 (耗時 {dt:.2f} 秒)", "SUCCESS" if is_manual else "INFO")
    gc.collect()


def store_monitor_worker(store_key: str):
    """單一賣場專屬 24H 獨立輪詢線程 (各賣場跑各自獨立的輪詢週期，彼此完全並行、互不等待)
    例如：PChome 跑每 3 秒週期，Amazon 跑專屬 0.6s 間隔週期，彼此完全非同步，絕無卡頓。
    """
    s_cfg = STORE_CONFIG.get(store_key, {})
    s_name = s_cfg.get("short_name", store_key)

    # 錯開各賣場初次啟動時間 (依序錯開 0.25s)，避免一開機瞬間全部賣場同毫秒出發
    keys_list = list(STORE_CONFIG.keys())
    offset = keys_list.index(store_key) * 0.25 if store_key in keys_list else 0.0
    time.sleep(offset)

    while not state.stop_event.is_set():
        # 如果該賣場未被啟用監控，每秒檢查一次開關狀態
        if not state.is_store_monitored(store_key):
            for _ in range(10):
                if state.stop_event.is_set():
                    break
                time.sleep(0.1)
            continue

        # 執行該賣場的專屬商品抓價與補貨檢查 (獨立執行，絕不阻塞其他賣場)
        check_items_for_store(store_key, is_manual=False)

        # 取得該賣場專屬的輪詢週期 (秒)
        s_set = state.config.get("store_settings", {}).get(store_key, {})
        store_interval = float(s_set.get("item_interval_seconds", state.config.get("interval_seconds", 5)))
        store_interval = max(2.0, store_interval)

        # 依該賣場自訂間隔休眠，切片為 0.1s 以保證隨時響應 stop_event
        sleep_slices = int(store_interval * 10)
        for _ in range(sleep_slices):
            if state.stop_event.is_set():
                break
            time.sleep(0.1)

def handle_result(idx: int, item: dict, res: dict, is_manual: bool = False):
    now_str = get_now_gmt8().strftime("%H:%M:%S")
    name = item.get("name")
    asin = item.get("asin")
    store = item.get("store", "amazon_jp")
    item_key = f"{store}_{asin}"

    store_cfg = STORE_CONFIG.get(store, STORE_CONFIG["amazon_jp"])
    store_short = store_cfg.get("short_name", store)
    s_settings = state.config.get("store_settings", {}).get(store, {})
    store_notify_enabled = s_settings.get("enable_notifications", True)

    if res.get("status_note"):
        state.add_log(f"[{store_short} - {name}] ℹ️ {res['status_note']}", "INFO")

    if not res.get("ok"):
        msg = res.get("msg", "檢測異常")
        item["last_status"] = msg
        item["last_time"] = now_str
        state.add_log(f"[{store_short} - {name}] 檢查失敗: {msg}", "WARNING")
        return

    price = res.get("price", "-")
    seller = res.get("seller", "-")
    in_stock = res.get("in_stock", False)
    is_official = res.get("is_official", True)
    is_preorder = res.get("is_preorder", False)
    official_price = res.get("official_price", "-")
    third_party_price = res.get("third_party_price", "-")
    url = res.get("url") or get_item_direct_url(item)

    status_text = ""
    is_alert_worthy = False

    if in_stock:
        if store == "amazon_jp":
            if is_official:
                status_text = "🟢 官方現貨/預購" if not is_preorder else "🔵 官方開放預購"
                is_alert_worthy = True
            else:
                status_text = f"🟡 第三方優質賣家: {price}" if price and price != "-" else "🟡 第三方優質賣家"
                # 使用者明確要求：推播只要推播官方補貨的通知就好 金額也顯示官方金額就好
                is_alert_worthy = False
        else:
            status_text = res.get("status_text") or "🟢 平台現貨開放！"
            is_alert_worthy = True
    else:
        if res.get("status_text"):
            status_text = res["status_text"]
        elif res.get("no_featured_offer", False):
            status_text = "⚪ 官方缺貨中 (僅轉賣選項)"
        else:
            status_text = "⚪ 缺貨中 / 暫無庫存"

    # 正規化價格顯示 (杜絕出現「官方缺貨中」混淆文字)
    clean_price = price if price and price not in ("官方缺貨中", "價格載入中") else ("-" if not in_stock else price)

    item["last_status"] = status_text
    item["last_price"] = clean_price
    item["last_seller"] = seller
    item["last_time"] = now_str
    item["official_price"] = official_price
    item["third_party_price"] = third_party_price
    state.save_config()

    prev_was_in_stock = state.in_stock_state.get(item_key, False)
    state.in_stock_state[item_key] = is_alert_worthy

    should_trigger = is_alert_worthy and (not prev_was_in_stock or is_manual)

    if is_alert_worthy:
        flag = store_cfg.get("flag", "⚡")
        display_price = official_price if (store == "amazon_jp" and official_price and official_price != "官方缺貨") else price
        state.add_log(f"🔥【官方補貨】{flag} [{store_short}] {name} 官方自營價: {display_price}！", "SUCCESS")

    if should_trigger:
        if store_notify_enabled:
            notify_price = official_price if (store == "amazon_jp" and official_price and official_price != "官方缺貨") else price
            notify_url = get_item_direct_url(item)
            trigger_notifications(item, name, asin, notify_price, seller, notify_url)
        else:
            state.add_log(f"🔕 [{store_short}] 已關閉推播通知，已略過本次推播", "INFO")


def trigger_notifications(item: dict, name: str, asin: str, price: str, seller: str, url: str):
    """發送 Discord 與 LINE 官方推播 (支援各賣場獨立 Webhook 與獨立開關)"""
    store = item.get("store", "amazon_jp")
    store_cfg = STORE_CONFIG.get(store, STORE_CONFIG["amazon_jp"])
    store_name = store_cfg.get("name", "線上商城")
    flag = store_cfg.get("flag", "⚡")
    btn_text = store_cfg.get("btn_text", "👉 點此直達購買")

    s_settings = state.config.get("store_settings", {}).get(store, {})
    if not s_settings.get("enable_notifications", True):
        return

    # 1. Discord 推播 (優先使用該賣場專屬 Webhook，若無則回退全域 Webhook)
    if state.config.get("enable_discord", True):
        discord_url = s_settings.get("discord_webhook", "").strip() or state.config.get("discord_webhook", "").strip()
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
                    f"🚨【補貨】{name}\n"
                    f"通路: {flag} {store_name}\n"
                    f"即時價格: {price}\n"
                    f"店家/賣家: {seller}\n"
                    f"🔥 {btn_text}:\n{url}"
                )
                ok, msg = NotificationManager.send_line_broadcast(line_token, msg_body)
                state.add_log(f"LINE ({store_name}): {msg}", "SUCCESS" if ok else "ERROR")
            threading.Thread(target=_send_l, daemon=True).start()


def start_monitor():
    state.is_monitoring = True
    state.stop_event.clear()
    state.add_log("=== 雲端 24H 各賣場獨立並行監控已啟動 (7 大賣場獨立線程，互不等待) ===", "SUCCESS")
    for s_key in STORE_CONFIG.keys():
        t = state.store_threads.get(s_key)
        if t is None or not t.is_alive():
            t = threading.Thread(target=store_monitor_worker, args=(s_key,), daemon=True, name=f"Worker-{s_key}")
            state.store_threads[s_key] = t
            t.start()


def stop_monitor():
    state.is_monitoring = False
    state.stop_event.set()
    state.add_log("=== 雲端各賣場獨立監控線程已停止 ===", "INFO")


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
    return {
        "status": "healthy",
        "monitoring": state.is_monitoring,
        "items_count": len(state.config.get("items", [])),
        "timestamp_gmt8": get_now_gmt8().isoformat()
    }


@app.get("/api/status")
async def get_status():
    """獲取最新即時狀態、商品清單、賣場定義、獨立賣場設定與日誌"""
    items = state.config.get("items", [])
    store_counts = {s_key: 0 for s_key in STORE_CONFIG.keys()}
    for it in items:
        s = it.get("store", "amazon_jp")
        store_counts[s] = store_counts.get(s, 0) + 1

    return {
        "is_monitoring": state.is_monitoring,
        "current_time_gmt8": get_now_gmt8().strftime("%Y-%m-%d %H:%M:%S"),
        "interval_seconds": state.config.get("interval_seconds", 5),
        "amazon_item_interval": state.get_amazon_item_interval(),
        "amazon_jitter": state.get_amazon_jitter(),
        "amazon_use_playwright": state.get_amazon_use_playwright(),
        "proxy_url": state.config.get("proxy_url", ""),
        "proxy_enabled": state.config.get("proxy_enabled", True),
        "amazon_custom_cookie": state.get_amazon_custom_cookie(),
        "keepa_api_key": state.config.get("keepa_api_key", ""),
        "keepa_enabled": state.config.get("keepa_enabled", bool(state.config.get("keepa_api_key"))),
        "keepa_mode": state.get_keepa_mode(),
        "curl_cffi_available": (cffi_requests is not None),
        "playwright_available": False,
        "only_amazon_seller": state.config.get("only_amazon_seller", True),
        "enable_discord": state.config.get("enable_discord", True),
        "enable_line": state.config.get("enable_line", True),
        "discord_webhook": state.config.get("discord_webhook", ""),
        "line_token": state.config.get("line_token", ""),
        "stores": STORE_CONFIG,
        "store_counts": store_counts,
        "store_settings": state.config.get("store_settings", {}),
        "items": items,
        "logs": state.logs
    }


@app.post("/api/settings")
async def api_update_settings(req: Request):
    """更新全域與各賣場獨立設定"""
    data = await req.json()
    if "interval_seconds" in data:
        state.config["interval_seconds"] = max(2, int(data["interval_seconds"]))
    if "amazon_item_interval" in data:
        try:
            val = max(0.1, float(data["amazon_item_interval"]))
            state.config["amazon_item_interval"] = val
            state.config.setdefault("store_settings", {}).setdefault("amazon_jp", {})["item_interval_seconds"] = val
        except (ValueError, TypeError):
            pass
    if "amazon_jitter" in data:
        val = bool(data["amazon_jitter"])
        state.config["amazon_jitter"] = val
        state.config.setdefault("store_settings", {}).setdefault("amazon_jp", {})["enable_jitter"] = val
    if "amazon_use_playwright" in data:
        val = bool(data["amazon_use_playwright"])
        state.config["amazon_use_playwright"] = val
        state.config.setdefault("store_settings", {}).setdefault("amazon_jp", {})["use_playwright"] = val
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
    if "proxy_url" in data:
        state.config["proxy_url"] = str(data["proxy_url"]).strip()
    if "proxy_enabled" in data:
        state.config["proxy_enabled"] = bool(data["proxy_enabled"])
    if "amazon_custom_cookie" in data:
        state.config["amazon_custom_cookie"] = str(data["amazon_custom_cookie"]).strip()
    if "keepa_api_key" in data:
        state.config["keepa_api_key"] = str(data["keepa_api_key"]).strip()
    if "keepa_enabled" in data:
        state.config["keepa_enabled"] = bool(data["keepa_enabled"])
    if "keepa_mode" in data:
        k_mode = str(data["keepa_mode"]).strip().lower()
        state.config["keepa_mode"] = k_mode if k_mode in ("fallback", "primary", "disabled") else "fallback"

    # 各賣場獨立設定 (獨立通知開關 & 獨立 Discord Webhook & 商品間隔)
    if "store_settings" in data and isinstance(data["store_settings"], dict):
        for s_key, s_val in data["store_settings"].items():
            if s_key in STORE_CONFIG:
                state.config.setdefault("store_settings", {}).setdefault(s_key, {})
                if "enable_monitoring" in s_val:
                    state.config["store_settings"][s_key]["enable_monitoring"] = bool(s_val["enable_monitoring"])
                if "enable_notifications" in s_val:
                    state.config["store_settings"][s_key]["enable_notifications"] = bool(s_val["enable_notifications"])
                if "discord_webhook" in s_val:
                    state.config["store_settings"][s_key]["discord_webhook"] = str(s_val["discord_webhook"]).strip()
                if "enable_jitter" in s_val:
                    val = bool(s_val["enable_jitter"])
                    state.config["store_settings"][s_key]["enable_jitter"] = val
                    if s_key == "amazon_jp":
                        state.config["amazon_jitter"] = val
                if "use_playwright" in s_val:
                    val = bool(s_val["use_playwright"])
                    state.config["store_settings"][s_key]["use_playwright"] = val
                    if s_key == "amazon_jp":
                        state.config["amazon_use_playwright"] = val
                if "item_interval_seconds" in s_val:
                    try:
                        ival = max(0.1, float(s_val["item_interval_seconds"]))
                        state.config["store_settings"][s_key]["item_interval_seconds"] = ival
                        if s_key == "amazon_jp":
                            state.config["amazon_item_interval"] = ival
                    except (ValueError, TypeError):
                        pass

    state.save_config()
    state.add_log("⚙️ 雲端全域與各賣場專屬設定已儲存！", "SUCCESS")
    return {"ok": True, "config": state.config}


@app.post("/api/test_keepa")
async def api_test_keepa(req: Request):
    """測試 Keepa API Key 是否有效並取得剩餘 Token 額度"""
    try:
        data = await req.json()
    except Exception:
        data = {}
    api_key = data.get("api_key", "").strip() or state.get_keepa_api_key()
    if not api_key:
        return JSONResponse({"ok": False, "msg": "請先填寫 Keepa API Key！"}, status_code=400)
    ok, msg, tokens = KeepaChecker.test_keepa_api(api_key)
    return {"ok": ok, "msg": msg, "tokens": tokens}


@app.post("/api/toggle_store_monitor")
async def api_toggle_store_monitor(store: str = Query(...)):
    """單鍵切換特定賣場之【監控開關】(是否納入輪詢爬取)"""
    if store in STORE_CONFIG:
        cur = state.config.setdefault("store_settings", {}).setdefault(store, {}).get("enable_monitoring", True)
        new_val = not cur
        state.config["store_settings"][store]["enable_monitoring"] = new_val
        state.save_config()
        s_name = STORE_CONFIG[store]["short_name"]
        state.add_log(f"📡 已{'【開啟】' if new_val else '【暫停】'} [{s_name}] 賣場背景監控", "SUCCESS" if new_val else "WARNING")
        return {"ok": True, "store": store, "enabled": new_val}
    return JSONResponse({"ok": False, "msg": "無效賣場"}, status_code=400)


@app.post("/api/toggle_store_notify")
async def api_toggle_store_notify(store: str = Query(...)):
    """單鍵切換特定賣場之【推播開關】"""
    if store in STORE_CONFIG:
        cur = state.config.setdefault("store_settings", {}).setdefault(store, {}).get("enable_notifications", True)
        new_val = not cur
        state.config["store_settings"][store]["enable_notifications"] = new_val
        state.save_config()
        s_name = STORE_CONFIG[store]["short_name"]
        state.add_log(f"🔔 已{'【開啟】' if new_val else '【關閉】'} [{s_name}] 賣場推播通知", "SUCCESS" if new_val else "WARNING")
        return {"ok": True, "store": store, "enabled": new_val}
    return JSONResponse({"ok": False, "msg": "無效賣場"}, status_code=400)


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

    if store in ("amazon_jp", "amazon_stealth"):
        asin = extract_asin(raw_input) or raw_input
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

    if store in ("amazon_jp", "amazon_stealth"):
        asin = extract_asin(raw_input) or raw_input
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


@app.post("/api/batch_add_items")
async def api_batch_add_items(req: Request, background_tasks: BackgroundTasks):
    """批次匯入多個型號至指定賣場 (支援以換行、逗號、空格分隔多組型號)"""
    data = await req.json()
    store = data.get("store", "shopee").strip()
    if store not in STORE_CONFIG:
        store = "shopee"

    raw_text = str(data.get("text", "")).strip()
    if not raw_text:
        return JSONResponse({"ok": False, "msg": "請輸入要匯入的商品型號！"}, status_code=400)

    # 提取所有型號 (例如 BX-50, UX-15, CX-05 等)
    tokens = re.split(r"[\r\n,，;；\s]+", raw_text)
    pattern = re.compile(r"^(?:BX|UX|CX)-\d{2}[A-Z]?$", re.I)

    existing_asins = {
        str(it.get("asin", "")).strip().upper()
        for it in state.config.get("items", [])
        if it.get("store") == store
    }

    added_count = 0
    new_items_to_check = []

    for t in tokens:
        clean_t = t.strip().upper()
        if not clean_t:
            continue
        # 若為非 Amazon 通路，規範為 BX/UX/CX 系列型號；若為 Amazon 通路則支援 ASIN 或型號
        if store not in ("amazon_jp", "amazon_stealth") and not pattern.match(clean_t):
            # 嘗試從字串中尋找型號
            m = re.search(r"((?:BX|UX|CX)-\d{2}[A-Z]?)", clean_t, re.I)
            if m:
                clean_t = m.group(1).upper()
            else:
                continue

        if clean_t in existing_asins:
            continue

        item_name = f"BEYBLADE X {clean_t}"
        new_item = {
            "enabled": True,
            "store": store,
            "name": item_name,
            "asin": clean_t,
            "note": "批次清單匯入",
            "last_status": "⚪ 尚未上架 (待突襲發布)",
            "last_price": "-",
            "last_seller": "-",
            "last_time": "-"
        }
        state.config.setdefault("items", []).append(new_item)
        existing_asins.add(clean_t)
        new_items_to_check.append((len(state.config["items"]) - 1, new_item))
        added_count += 1

    if added_count > 0:
        state.save_config()
        s_name = STORE_CONFIG[store]["short_name"]
        state.add_log(f"📦 已批次匯入 {added_count} 個型號至 [{s_name}] 防突襲清單", "SUCCESS")

        def _bg_check_batch():
            for idx, it in new_items_to_check:
                if state.stop_event.is_set():
                    break
                res = check_store_item(it)
                handle_result(idx, it, res, is_manual=True)
                time.sleep(0.1)

        background_tasks.add_task(_bg_check_batch)
        return {"ok": True, "added_count": added_count, "msg": f"成功匯入 {added_count} 個商品！"}
    else:
        return JSONResponse({"ok": False, "msg": "未能識別出新的有效型號，或清單中的型號已存在於該賣場！"}, status_code=400)



@app.post("/api/test_discord")
async def api_test_discord(req: Request):
    """從 Web 介面測試指定賣場之 Discord 推播"""
    data = await req.json()
    store = data.get("store", "amazon_jp")
    webhook_url = data.get("webhook_url", "").strip()

    if not webhook_url:
        s_settings = state.config.get("store_settings", {}).get(store, {})
        webhook_url = s_settings.get("discord_webhook", "").strip() or state.config.get("discord_webhook", "").strip()

    if not webhook_url:
        return JSONResponse({"ok": False, "msg": "請填寫該賣場專屬或全域 Discord Webhook 網址！"}, status_code=400)

    s_cfg = STORE_CONFIG.get(store, STORE_CONFIG["amazon_jp"])
    prod_url = s_cfg["default_url"].replace("{id}", "TEST-ITEM")
    ok, msg = NotificationManager.send_discord(
        webhook_url,
        f"【測試】{s_cfg['name']} 專屬推播測試",
        "TEST-SAMPLE",
        "NT$ 999",
        s_cfg["name"],
        prod_url,
        store=store
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
    msg_body = f"🚨【補貨測試】UX-21 赫爾茲地獄\n通路: 🇯🇵 Amazon Japan\n價格: ￥4,500\n賣家: Amazon.co.jp (官方自營)\n🔥 1-Click 官方直達:\n{prod_url}"
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


@app.post("/api/batch_toggle_store_items")
async def api_batch_toggle_store_items(req: Request):
    """一鍵啟用或略過指定賣場的所有商品/防突襲清單"""
    data = await req.json()
    store = data.get("store")
    enabled = bool(data.get("enabled", True))
    series = data.get("series")  # 可選: "BX", "UX", "CX" 或 None
    
    items = state.config.get("items", [])
    count = 0
    for it in items:
        if it.get("store") == store:
            if series:
                asin = str(it.get("asin", "")).upper()
                if not asin.startswith(series):
                    continue
            it["enabled"] = enabled
            count += 1
            
    state.save_config()
    s_name = STORE_CONFIG.get(store, {}).get("short_name", store)
    series_tag = f" {series} 系列" if series else ""
    state.add_log(f"已一鍵{'開啟' if enabled else '停用'} [{s_name}]{series_tag} 共 {count} 項商品監控", "INFO")
    return {"ok": True, "count": count, "enabled": enabled}


@app.post("/api/check_store_now")
async def api_check_store_now(background_tasks: BackgroundTasks, store: str = Query(...)):
    """單獨立即檢查特定賣場的所有商品 (例如只查 PChome 或只查 Amazon)"""
    if store not in STORE_CONFIG:
        return JSONResponse({"ok": False, "msg": "無效賣場"}, status_code=400)

    s_name = STORE_CONFIG[store]["name"]
    background_tasks.add_task(check_items_for_store, store, True)
    return {"ok": True, "msg": f"已開始檢查 {s_name} 商品"}


@app.post("/api/check_now")
async def api_check_now(background_tasks: BackgroundTasks):
    """立即檢查所有已開啟監控之賣場商品"""
    def _do_all():
        for s_key in STORE_CONFIG.keys():
            if state.is_store_monitored(s_key):
                check_items_for_store(s_key, is_manual=True)
    background_tasks.add_task(_do_all)
    return {"ok": True, "msg": "已開始全賣場極速檢查"}


@app.post("/api/test_proxy")
async def api_test_proxy(req: Request):
    """測試住宅代理 (Residential Proxy) 連線與取得對外 IP"""
    data = await req.json()
    proxy_url = data.get("proxy_url", "").strip() or state.get_proxy_url()
    if not proxy_url:
        return JSONResponse({"ok": False, "msg": "請輸入住宅代理網址！(格式: http://user:pass@host:port)"}, status_code=400)

    ok, msg = test_proxy_connection(proxy_url)
    return {"ok": ok, "msg": msg}

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
                
                by_store = {}
                for it in items:
                    s = it.get("store", "amazon_jp")
                    by_store.setdefault(s, []).append(it)
                
                for s_key, s_cfg in STORE_CONFIG.items():
                    s_items = by_store.get(s_key, [])
                    if not s_items:
                        continue
                    s_settings = state.config.get("store_settings", {}).get(s_key, {})
                    is_on = s_settings.get("enable_notifications", True)
                    bell = "🔔" if is_on else "🔕(已關閉推播)"
                    lines.append(f"\n{s_cfg['flag']} 【{s_cfg['name']}】{bell} ({len(s_items)}項):")
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
                    "👉 傳送「查庫存」：依賣場查看最新庫存與推播狀態\n"
                    "👉 傳送「全檢」：立即重新檢查所有商品"
                )

            if reply_token and line_token:
                NotificationManager.reply_line(reply_token, line_token, reply_msg)

    return JSONResponse({"status": "ok"})


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)
