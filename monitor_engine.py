"""
戰鬥陀螺 (Beyblade X) 雲端全方位多通路監控引擎核心
支援 7 大電商平台：
1. 🇯🇵 Amazon Japan (amazon_jp)
2. 🇹🇼 PChome 24h (pchome)
3. 🏬 M.M小舖 (mm_shop)
4. 🧸 麗嬰國際官網 (funbox_tw)
5. 🎯 童無忌玩具 (twj_toys)
6. 📚 誠品線上 (eslite)
7. 🦐 蝦皮 Funbox (shopee)
專為 Render.com / Linux 雲端環境優化，連線池複用、前段串流快速提取，0 耗 CPU。
"""

import json
import os
import re
import time
import threading
from datetime import datetime
from typing import Dict, Any, Tuple, Optional

import random
import requests
from bs4 import BeautifulSoup

try:
    from curl_cffi import requests as cffi_requests
except ImportError:
    cffi_requests = None

try:
    from playwright.sync_api import sync_playwright
    from playwright_stealth import stealth_sync
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

# 7 大電商賣場基礎設定與外觀定義
STORE_CONFIG = {
    "amazon_jp": {
        "key": "amazon_jp",
        "name": "Amazon Japan",
        "short_name": "Amazon JP",
        "icon": "fa-brands fa-amazon",
        "flag": "🇯🇵",
        "color": "#f59e0b",
        "btn_text": "⚡ 1-Click 官方直達",
        "default_url": "https://www.amazon.co.jp/dp/{id}?m=AN1VRQENFRJN5&th=1&psc=1",
        "id_label": "ASIN 或 Amazon 網址",
        "id_placeholder": "例如: B0HJ783F18 或 商品網址",
    },
    "pchome": {
        "key": "pchome",
        "name": "PChome 24h",
        "short_name": "PChome 24h",
        "icon": "fa-solid fa-cart-shopping",
        "flag": "🇹🇼",
        "color": "#ef4444",
        "btn_text": "🛒 PChome 直達",
        "default_url": "https://24h.pchome.com.tw/prod/{id}",
        "id_label": "商品編號或 PChome 網址",
        "id_placeholder": "例如: DEASSW-A900KGOV3 或 商品網址",
    },
    "mm_shop": {
        "key": "mm_shop",
        "name": "M.M小舖",
        "short_name": "M.M小舖",
        "icon": "fa-solid fa-store",
        "flag": "🏬",
        "color": "#06b6d4",
        "btn_text": "🏬 M.M小舖直達",
        "default_url": "https://mmtoyshop.com/item/{id}",
        "id_label": "商品 ID 或 M.M小舖網址",
        "id_placeholder": "例如: shopee6a3bdb48bde45 或 商品網址",
    },
    "funbox_tw": {
        "key": "funbox_tw",
        "name": "麗嬰國際官網",
        "short_name": "麗嬰官網",
        "icon": "fa-solid fa-cube",
        "flag": "🧸",
        "color": "#ec4899",
        "btn_text": "🧸 麗嬰官網直達",
        "default_url": "https://shop.funbox.com.tw/products/{id}",
        "id_label": "商品編號或麗嬰官網網址",
        "id_placeholder": "例如: sm53043 或 商品網址",
    },
    "twj_toys": {
        "key": "twj_toys",
        "name": "童無忌玩具",
        "short_name": "童無忌",
        "icon": "fa-solid fa-bullseye",
        "flag": "🎯",
        "color": "#10b981",
        "btn_text": "🎯 童無忌直達",
        "default_url": "https://www.twj.tw/products/{id}",
        "id_label": "商品代碼或童無忌網址",
        "id_placeholder": "例如: 20268beyblade-x-bx-00- 或 網址",
    },
    "eslite": {
        "key": "eslite",
        "name": "誠品線上",
        "short_name": "誠品線上",
        "icon": "fa-solid fa-book",
        "flag": "📚",
        "color": "#8b5cf6",
        "btn_text": "📚 誠品線上直達",
        "default_url": "https://www.eslite.com/product/{id}",
        "id_label": "商品編號或誠品網址",
        "id_placeholder": "例如: 2683194026001 或 商品網址",
    },
    "shopee": {
        "key": "shopee",
        "name": "蝦皮 Funbox",
        "short_name": "蝦皮 Funbox",
        "icon": "fa-solid fa-shrimp",
        "flag": "🦐",
        "color": "#f97316",
        "btn_text": "🦐 蝦皮直達",
        "default_url": "https://shopee.tw/product/{id}",
        "id_label": "蝦皮商品網址或代號",
        "id_placeholder": "例如: https://shopee.tw/product/... 或 -i.{shopid}.{itemid}",
    }
}


def get_shared_session():
    """共用 HTTP 連線池，最大化連線重用"""
    if not hasattr(get_shared_session, "_session"):
        s = requests.Session()
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=30,
            pool_maxsize=30,
            max_retries=1
        )
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        get_shared_session._session = s
    return get_shared_session._session


# =========================================================================
# 1. Amazon Japan Checker (多層防風控極速架構)
# 包含：
# - 真實現代瀏覽器 User-Agent 與 Client Hints (sec-ch-ua) 完整輪換池
# - 合理請求間隔 + 隨機浮動延遲 (Jitter) 與自動 503 冷卻保護
# - curl_cffi Chrome 124 TLS 偽裝 (繞過 CloudFront/Akamai TLS 指紋檢測)
# - Playwright + playwright-stealth 真實無頭瀏覽器備援引擎
def get_product_url(asin: str, official_only: bool = False) -> str:
    """生成 Amazon 購買商品網址 (支援官方直達與標準頁面)"""
    if official_only:
        return f"https://www.amazon.co.jp/dp/{asin}?m=AN1VRQENFRJN5&th=1&psc=1"
    return f"https://www.amazon.co.jp/dp/{asin}?th=1&psc=1"


def extract_asin(text: str) -> str:
    if not text:
        return ""
    text = text.strip()
    match = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", text, re.IGNORECASE)
    if match:
        return match.group(1).upper()
    match = re.search(r"\b([A-Z0-9]{10})\b", text, re.IGNORECASE)
    if match and not match.group(1).isdigit():
        return match.group(1).upper()
    return text.upper()


AMAZON_STEALTH_PROFILES = [
    {
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "sec_ch_ua": '"Chromium";v="128", "Not;A=Brand";v="24", "Google Chrome";v="128"',
        "sec_ch_ua_platform": '"Windows"',
        "impersonate": "chrome124",
    },
    {
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
        "sec_ch_ua": '"Not)A;Brand";v="99", "Google Chrome";v="127", "Chromium";v="127"',
        "sec_ch_ua_platform": '"Windows"',
        "impersonate": "chrome120",
    },
    {
        "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "sec_ch_ua": '"Chromium";v="128", "Not;A=Brand";v="24", "Google Chrome";v="128"',
        "sec_ch_ua_platform": '"macOS"',
        "impersonate": "chrome124",
    },
    {
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36 Edg/128.0.0.0",
        "sec_ch_ua": '"Chromium";v="128", "Not;A=Brand";v="24", "Microsoft Edge";v="128"',
        "sec_ch_ua_platform": '"Windows"',
        "impersonate": "edge101",
    },
    {
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36 Edg/127.0.0.0",
        "sec_ch_ua": '"Not)A;Brand";v="99", "Microsoft Edge";v="127", "Chromium";v="127"',
        "sec_ch_ua_platform": '"Windows"',
        "impersonate": "edge101",
    },
    {
        "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
        "sec_ch_ua": None,
        "sec_ch_ua_platform": None,
        "impersonate": "safari15_5",
    },
    {
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:129.0) Gecko/20100101 Firefox/129.0",
        "sec_ch_ua": None,
        "sec_ch_ua_platform": None,
        "impersonate": "firefox120",
    },
]

def get_amazon_stealth_headers(profile: dict = None) -> Tuple[dict, str]:
    """獲取真實瀏覽器輪換 Headers 與對應 Client Hints，帶日幣偏好 Cookie"""
    if not profile:
        profile = random.choice(AMAZON_STEALTH_PROFILES)
    headers = {
        "User-Agent": profile["user_agent"],
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Cookie": "i18n-prefs=JPY; lc-acbjp=ja_JP",
        "DNT": "1",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Priority": "u=0, i",
    }
    if profile.get("sec_ch_ua"):
        headers["sec-ch-ua"] = profile["sec_ch_ua"]
        headers["sec-ch-ua-mobile"] = "?0"
        headers["sec-ch-ua-platform"] = profile["sec_ch_ua_platform"]

    return headers, profile.get("impersonate", "chrome124")


def parse_amazon_html(html: str, asin: str, status_code: int = 200, url: str = "") -> Dict[str, Any]:
    """統一 Amazon 商品頁面 HTML 解析器 (精準 BuyBox、官方賣家、庫存、價格)"""
    if not url:
        url = get_product_url(asin)

    if status_code == 404 or "申し訳ございません。お探しのページが見つかりませんでした" in html:
        return {"ok": False, "msg": "頁面不存在 (404 未上架)"}
    elif status_code == 503:
        return {"ok": False, "msg": "Amazon 頻率限制 (503，已觸發自動冷卻)"}
    elif "/errors_page/validateCaptcha" in html or "api-services-support@amazon.com" in html:
        return {"ok": False, "msg": "Amazon 頻率限制 (CAPTCHA 驗證，已觸發自動冷卻)"}
    elif status_code != 200 and not ("<html" in html.lower()):
        return {"ok": False, "msg": f"HTTP {status_code}"}

    title_m = re.search(r'id="productTitle"[^>]*>(.*?)</span>', html, re.DOTALL)
    title = " ".join(title_m.group(1).split()) if title_m else ""

    has_cart = ('id="add-to-cart-button"' in html) or ('name="submit.add-to-cart"' in html)
    has_buy_now = ('id="buy-now-button"' in html) or ('name="submit.buy-now"' in html)
    has_preorder = ('preorder' in html.lower()) or ('予約注文' in html)

    no_featured_offer = (
        "おすすめ出品はありません" in html or
        "その他の出品者" in html or
        "没有精选优惠" in html
    ) and not (has_cart or has_buy_now or has_preorder)

    has_third_party_profile = 'id="sellerProfileTriggerId"' in html

    merchant_m = re.search(r'id="(?:merchantInfo|merchant-info)"[^>]*>(.*?)</div>', html, re.DOTALL)
    merchant_text = " ".join(re.sub(r'<[^>]+>', ' ', merchant_m.group(1)).split()) if merchant_m else ""

    fulfiller_m = re.search(r'id="(?:fulfillerInfo|fulfiller-info)"[^>]*>(.*?)</div>', html, re.DOTALL)
    fulfiller_text = " ".join(re.sub(r'<[^>]+>', ' ', fulfiller_m.group(1)).split()) if fulfiller_m else ""

    is_amazon_sold = (
        ("amazon.co.jp" in merchant_text.lower() or "アマゾン" in merchant_text) and
        not has_third_party_profile
    )
    is_amazon_fulfilled = "amazon" in fulfiller_text.lower()

    # 精準解析價格 (優先 Buybox / Apex 主價格區塊，嚴格排除特價劃線參考價與推薦卡片)
    price = ""
    try:
        soup = BeautifulSoup(html, "html.parser")
        
        # 1. 優先從主要購買區塊 (BuyBox) 提取
        buybox = soup.select_one("#buybox, #desktop_buybox, #tabular-buybox")
        if buybox:
            for p_elem in buybox.select(".priceToPay .a-offscreen, #price_inside_buybox, #newBuyBoxPrice, .a-price:not(.a-text-price):not(.basisPrice) .a-offscreen"):
                t = p_elem.get_text(strip=True)
                if t and ("￥" in t or t.replace(",", "").isdigit()):
                    price = t if "￥" in t else f"￥{t}"
                    break

        # 2. 其次從中央價格展示區 (Apex / CorePrice) 提取
        if not price:
            apex = soup.select_one("#corePriceDisplay_desktop_feature_div, #apex_desktop, #corePrice_feature_div")
            if apex:
                for p_elem in apex.select(".priceToPay .a-offscreen, .apexPriceToPay .a-offscreen, .a-price:not(.a-text-price):not(.basisPrice) .a-offscreen"):
                    t = p_elem.get_text(strip=True)
                    if t and ("￥" in t or t.replace(",", "").isdigit()):
                        price = t if "￥" in t else f"￥{t}"
                        break
                if not price:
                    whole = apex.select_one(".a-price-whole")
                    if whole and whole.get_text(strip=True):
                        price = f"￥{whole.get_text(strip=True)}"

        # 3. 補充檢查 tabular buybox 的賣家資訊
        if not merchant_text and buybox:
            for tr in buybox.select("#tabular-buybox tr, .tabular-buybox-container tr"):
                tr_text = tr.get_text()
                if "販売元" in tr_text or "Sold by" in tr_text:
                    merchant_text = tr_text.replace("販売元", "").replace("Sold by", "").strip()
                    if "amazon" in merchant_text.lower() or "アマゾン" in merchant_text:
                        is_amazon_sold = True
                        break
    except Exception:
        pass

    # 備援快速正則 (僅針對中央價格區塊，絕不跨全域 HTML)
    if not price:
        m_core = re.search(r'id="corePriceDisplay_desktop_feature_div"[^>]*>.*?(?:<span class="a-offscreen">\s*([^\s<]+)\s*</span>|￥\s*([\d,]+))', html, re.DOTALL)
        if m_core:
            p_val = m_core.group(1) or m_core.group(2)
            if p_val:
                price = p_val if "￥" in p_val else f"￥{p_val}"
        else:
            m_bb = re.search(r'id="price_inside_buybox"[^>]*>\s*([^\s<]+)\s*<', html)
            if m_bb:
                price = m_bb.group(1).strip()

    in_stock = False
    is_official = False
    seller_name = "第三方賣家"

    if (has_cart or has_buy_now or has_preorder) and not no_featured_offer:
        if is_amazon_sold:
            in_stock = True
            is_official = True
            seller_name = "Amazon.co.jp (官方自營)"
        else:
            in_stock = True
            is_official = False
            if merchant_text:
                seller_name = merchant_text[:25]
            if is_amazon_fulfilled:
                seller_name += " (Amazon 配送)"
    else:
        in_stock = False
        seller_name = "暫無官方現貨" if is_amazon_sold else "缺貨中"

    if not price:
        price = "官方缺貨中" if not in_stock else "價格載入中"

    return {
        "ok": True,
        "store": "amazon_jp",
        "asin": asin,
        "title": title,
        "price": price,
        "in_stock": in_stock,
        "is_official": is_official,
        "is_preorder": has_preorder,
        "seller": seller_name,
        "url": url,
        "raw_merchant": merchant_text
    }


class PlaywrightAmazonChecker:
    """真實無頭瀏覽器防檢測備援引擎 (Playwright + playwright-stealth)"""
    _lock = threading.Lock()

    @classmethod
    def is_available(cls) -> bool:
        if not PLAYWRIGHT_AVAILABLE:
            return False
        try:
            with sync_playwright() as p:
                exe = p.chromium.executable_path
                return bool(exe and os.path.exists(exe))
        except Exception:
            return False

    @classmethod
    def check_asin(cls, asin: str) -> Dict[str, Any]:
        if not PLAYWRIGHT_AVAILABLE:
            return {"ok": False, "msg": "未安裝 Playwright 套件"}
        asin = extract_asin(asin)
        if not asin:
            return {"ok": False, "msg": "無效 ASIN"}
        url = get_product_url(asin)

        with cls._lock:
            try:
                with sync_playwright() as p:
                    profile = random.choice(AMAZON_STEALTH_PROFILES)
                    browser = p.chromium.launch(
                        headless=True,
                        args=[
                            "--disable-blink-features=AutomationControlled",
                            "--no-sandbox",
                            "--disable-setuid-sandbox",
                            "--disable-infobars"
                        ]
                    )
                    context = browser.new_context(
                        viewport={"width": 1280, "height": 800},
                        locale="ja-JP",
                        timezone_id="Asia/Tokyo",
                        user_agent=profile["user_agent"]
                    )
                    page = context.new_page()
                    stealth_sync(page)
                    page.goto(url, wait_until="domcontentloaded", timeout=12000)
                    html = page.content()
                    browser.close()
                    return parse_amazon_html(html, asin, url=url)
            except Exception as e:
                return {"ok": False, "msg": f"Playwright 檢測失敗: {str(e)[:30]}"}


class AmazonJPChecker:
    """Amazon Japan 核心檢測器 (真實 Headers 輪換 + Jitter 隨機延遲 + Chrome TLS 偽裝)"""
    _last_req_time: float = 0.0
    _lock = threading.Lock()
    _cooloff_until: float = 0.0

    @classmethod
    def throttle(cls, interval: float = 0.6, enable_jitter: bool = True, jitter_min: float = 0.1, jitter_max: float = 0.35):
        """保證請求間隔 + Jitter 隨機延遲，遇到風控自動冷卻"""
        with cls._lock:
            now = time.time()
            if now < cls._cooloff_until:
                wait_cool = cls._cooloff_until - now
                time.sleep(wait_cool)
                now = time.time()

            delay = interval
            if enable_jitter and jitter_max > 0:
                jitter = random.uniform(jitter_min, jitter_max)
                delay += jitter

            elapsed = now - cls._last_req_time
            if elapsed < delay:
                time.sleep(delay - elapsed)
            cls._last_req_time = time.time()

    @classmethod
    def trigger_cooloff(cls, seconds: float = 20.0):
        with cls._lock:
            cls._cooloff_until = max(cls._cooloff_until, time.time() + seconds)

    @classmethod
    def check_asin(cls, asin: str, interval: float = 0.6, enable_jitter: bool = True, use_playwright: bool = False, proxy: Optional[str] = None, official_only: bool = False) -> Dict[str, Any]:
        """極速檢測 Amazon.co.jp 特定 ASIN 庫存 (支援真實 Headers輪換、Jitter延遲、TLS 偽裝與住宅代理)"""
        asin = extract_asin(asin)
        if not asin:
            return {"ok": False, "msg": "無效 ASIN"}

        # 若使用者指定啟用 Playwright 且環境支援，直接走真實瀏覽器
        if use_playwright and PlaywrightAmazonChecker.is_available():
            cls.throttle(interval, enable_jitter=enable_jitter)
            return PlaywrightAmazonChecker.check_asin(asin)

        cls.throttle(interval, enable_jitter=enable_jitter)
        url = get_product_url(asin, official_only=official_only)
        headers, imp = get_amazon_stealth_headers()

        html = None
        status_code = 200
        proxies = {"http": proxy, "https": proxy} if proxy else None

        # 第一優先：curl_cffi Chrome 124 TLS 指紋偽裝 (支援住宅代理)
        if cffi_requests:
            try:
                session = cffi_requests.Session(impersonate=imp, proxies=proxies)
                r = session.get(url, headers=headers, timeout=8)
                status_code = r.status_code
                html = r.text
            except Exception:
                html = None

        # 第二優先：requests 連線池 (帶真實 Headers 輪換與住宅代理)
        if not html:
            try:
                session = get_shared_session()
                r = session.get(url, headers=headers, proxies=proxies, timeout=8)
                status_code = r.status_code
                html = r.text
            except Exception as e:
                return {"ok": False, "msg": f"網路超時: {str(e)[:25]}"}

        if not html:
            return {"ok": False, "msg": "無法獲取頁面內容"}

        # 檢測 CAPTCHA 或 503 頻率限制
        if "/errors_page/validateCaptcha" in html or "api-services-support@amazon.com" in html:
            cls.trigger_cooloff(20.0)
            if PlaywrightAmazonChecker.is_available():
                return PlaywrightAmazonChecker.check_asin(asin)
            return {"ok": False, "msg": "Amazon 頻率限制 (CAPTCHA 驗證，已自動避讓冷卻 20s，建議設定住宅代理)"}

        if status_code == 503:
            cls.trigger_cooloff(20.0)
            if PlaywrightAmazonChecker.is_available():
                return PlaywrightAmazonChecker.check_asin(asin)
            return {"ok": False, "msg": "Amazon 頻率限制 (503，已自動避讓冷卻 20s，建議設定住宅代理)"}

        return parse_amazon_html(html, asin, status_code=status_code, url=url)


# =========================================================================
# 2. PChome 24h Checker (官方即時庫存按鈕 API，0.05 秒回傳)
# =========================================================================

class PChomeChecker:
    @staticmethod
    def extract_prod_id(text: str) -> str:
        if not text:
            return ""
        text = text.strip()
        m = re.search(r"([A-Z0-9]{6}-[A-Z0-9]{9}(?:-[0-9]{3})?)", text, re.IGNORECASE)
        if m:
            return m.group(1).upper()
        return text.upper()

    @classmethod
    def check_prod(cls, prod_id_or_url: str) -> Dict[str, Any]:
        prod_id = cls.extract_prod_id(prod_id_or_url)
        if not prod_id:
            return {"ok": False, "msg": "無效 PChome 商品編號"}

        product_url = f"https://24h.pchome.com.tw/prod/{prod_id}"
        session = get_shared_session()
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            "Referer": "https://24h.pchome.com.tw/"
        }

        # 1. 查詢即時購買按鈕狀態與庫存數量
        btn_url = f"https://ecapi.pchome.com.tw/ecshop/prodapi/v2/prod/button&id={prod_id}&fields=Seq,Id,Price,Qty,ButtonType,SaleStatus"
        try:
            r = session.get(btn_url, headers=headers, timeout=4)
            btn_data = r.json()
        except Exception as e:
            return {"ok": False, "msg": f"PChome 連線失敗: {str(e)[:25]}"}

        if not btn_data or not isinstance(btn_data, list):
            return {"ok": False, "msg": "查無此商品或已下架"}

        item_info = btn_data[0]
        button_type = item_info.get("ButtonType", "")
        qty = item_info.get("Qty", 0)
        price_val = item_info.get("Price", {}).get("P", 0)

        in_stock = (button_type == "ForSale") and (qty > 0)
        price_str = f"NT$ {price_val:,}" if price_val else "暫無價格"

        # 2. 自動抓取商品名稱 (透過快速搜尋 API)
        title = ""
        try:
            search_url = f"https://ecshweb.pchome.com.tw/search/v3.3/all/results?q={prod_id}"
            rs = session.get(search_url, headers=headers, timeout=3)
            s_data = rs.json()
            if s_data.get("prods"):
                title = s_data["prods"][0].get("name", "")
        except Exception:
            pass

        seller = "PChome 24h 購物"
        if not in_stock:
            status_text = "⚪ 缺貨中 / 暫無庫存"
        else:
            status_text = f"🟢 現貨有貨 (剩餘 {qty} 件)"

        return {
            "ok": True,
            "store": "pchome",
            "asin": prod_id,
            "title": title or prod_id,
            "price": price_str,
            "in_stock": in_stock,
            "is_official": True,
            "seller": seller,
            "url": product_url,
            "qty": qty,
            "status_text": status_text
        }


# =========================================================================
# 3. M.M小舖 Checker (Nuxt SSR 前 250KB 串流解析 Schema.org LD+JSON)
# =========================================================================

class MMShopChecker:
    @staticmethod
    def extract_item_id(text: str) -> str:
        if not text:
            return ""
        text = text.strip()
        m = re.search(r"mmtoyshop\.com/item/([a-zA-Z0-9_-]+)", text, re.IGNORECASE)
        if m:
            return m.group(1)
        return text.replace("https://", "").replace("http://", "").strip("/")

    @classmethod
    def check_item(cls, item_id_or_url: str) -> Dict[str, Any]:
        item_id = cls.extract_item_id(item_id_or_url)
        if not item_id:
            return {"ok": False, "msg": "無效 M.M小舖 商品 ID"}

        url = f"https://mmtoyshop.com/item/{item_id}"
        session = get_shared_session()
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        }

        try:
            r = session.get(url, headers=headers, stream=True, timeout=6)
            if r.status_code == 404:
                return {"ok": False, "msg": "商品頁面不存在 (404)"}
            
            content = b""
            for chunk in r.iter_content(chunk_size=32768):
                content += chunk
                if b'"@type":"Product"' in content or b'"@type": "Product"' in content:
                    try:
                        content += next(r.iter_content(chunk_size=32768))
                    except StopIteration:
                        pass
                    break
                if len(content) > 400000:
                    break
            r.close()
        except Exception as e:
            return {"ok": False, "msg": f"M.M小舖連線逾時: {str(e)[:25]}"}

        html = content.decode("utf-8", errors="ignore")
        product_ld = None
        for m in re.finditer(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html, re.DOTALL):
            try:
                d = json.loads(m.group(1))
                if isinstance(d, dict) and d.get("@type") == "Product":
                    product_ld = d
                    break
            except Exception:
                pass

        if product_ld:
            title = product_ld.get("name", "")
            offers = product_ld.get("offers", {})
            avail = offers.get("availability", "")
            in_stock = ("InStock" in avail) and ("OutOfStock" not in avail)
            price_val = offers.get("price", "")
            price_str = f"NT$ {price_val}" if price_val else "未標示"
        else:
            title_m = re.search(r"<title>(.*?)(?: - |\||M\.M).*?</title>", html)
            title = title_m.group(1).strip() if title_m else item_id
            in_stock = ("加入購物車" in html or "立即購買" in html) and ("已售完" not in html and "缺貨" not in html)
            p_m = re.search(r'(?:NT\$|\$)\s*([\d,]+)', html)
            price_str = f"NT$ {p_m.group(1)}" if p_m else "未標示"

        return {
            "ok": True,
            "store": "mm_shop",
            "asin": item_id,
            "title": title or item_id,
            "price": price_str,
            "in_stock": in_stock,
            "is_official": True,
            "seller": "M.M小舖",
            "url": url
        }


# =========================================================================
# 4 & 5. Cyberbiz Checker (通用於 麗嬰國際官網 & 童無忌玩具)
# =========================================================================

class CyberbizChecker:
    @staticmethod
    def extract_slug(text: str) -> str:
        if not text:
            return ""
        text = text.strip()
        m = re.search(r"/products/([a-zA-Z0-9%_-]+)", text)
        if m:
            return m.group(1)
        return text.replace("https://", "").replace("http://", "").strip("/")

    @classmethod
    def check_prod(cls, slug_or_url: str, store_key: str = "funbox_tw") -> Dict[str, Any]:
        slug = cls.extract_slug(slug_or_url)
        if not slug:
            return {"ok": False, "msg": "無效商品編號"}

        if store_key == "twj_toys":
            base_url = "https://www.twj.tw/products"
            seller_name = "童無忌玩具"
        else:
            base_url = "https://shop.funbox.com.tw/products"
            seller_name = "麗嬰國際官網 (Funbox)"

        url = f"{base_url}/{slug}"
        session = get_shared_session()
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        }

        try:
            r = session.get(url, headers=headers, stream=True, timeout=6)
            if r.status_code == 404:
                return {"ok": False, "msg": "商品頁面不存在 (404)"}

            content = b""
            for chunk in r.iter_content(chunk_size=16384):
                content += chunk
                if b'"@type":"Product"' in content or b'"@type": "Product"' in content:
                    try:
                        content += next(r.iter_content(chunk_size=16384))
                    except StopIteration:
                        pass
                    break
                if len(content) > 80000:
                    break
            r.close()
        except Exception as e:
            return {"ok": False, "msg": f"連線逾時: {str(e)[:25]}"}

        html = content.decode("utf-8", errors="ignore")
        product_ld = None
        for m in re.finditer(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html, re.DOTALL):
            try:
                d = json.loads(m.group(1))
                if isinstance(d, dict) and d.get("@type") == "Product":
                    product_ld = d
                    break
            except Exception:
                pass

        if product_ld:
            title = product_ld.get("name", "")
            offers = product_ld.get("offers", {})
            if isinstance(offers, list) and offers:
                offers = offers[0]
            avail = offers.get("availability", "")
            in_stock = ("InStock" in avail) and ("OutOfStock" not in avail)
            price_val = offers.get("price", "")
            try:
                price_str = f"NT$ {int(float(price_val)):,}" if price_val else "未標示"
            except Exception:
                price_str = f"NT$ {price_val}"
        else:
            title_m = re.search(r"<title>(.*?)</title>", html)
            title = title_m.group(1).strip() if title_m else slug
            in_stock = ("加入購物車" in html) and ("已售完" not in html and "缺貨" not in html)
            price_str = "未標示"

        return {
            "ok": True,
            "store": store_key,
            "asin": slug,
            "title": title or slug,
            "price": price_str,
            "in_stock": in_stock,
            "is_official": True,
            "seller": seller_name,
            "url": url
        }


# =========================================================================
# 6. Eslite Checker (誠品線上 Athena 官方 API)
# =========================================================================

class EsliteChecker:
    @staticmethod
    def extract_id(text: str) -> str:
        if not text:
            return ""
        text = text.strip()
        m = re.search(r"/product/([0-9]+)", text)
        if m:
            return m.group(1)
        m2 = re.search(r"(\d{10,24})", text)
        if m2:
            return m2.group(1)
        return text

    @classmethod
    def check_prod(cls, id_or_url: str) -> Dict[str, Any]:
        full_id = cls.extract_id(id_or_url)
        if not full_id:
            return {"ok": False, "msg": "無效誠品商品代號"}

        sn_id = full_id[-13:] if len(full_id) >= 13 else full_id
        product_url = f"https://www.eslite.com/product/{full_id}"

        api_url = f"https://athena.eslite.com/api/v3/products/{sn_id}"
        session = get_shared_session()
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            "Origin": "https://www.eslite.com",
            "Referer": "https://www.eslite.com/"
        }

        try:
            r = session.get(api_url, headers=headers, timeout=5)
            if r.status_code != 200:
                return {"ok": False, "msg": f"誠品 API HTTP {r.status_code}"}
            data = r.json()
        except Exception as e:
            return {"ok": False, "msg": f"誠品連線異常: {str(e)[:25]}"}

        if not data or not isinstance(data, list):
            return {"ok": False, "msg": "誠品未收錄或查無此商品"}

        item = data[0]
        title = item.get("product_name", "")
        price_val = item.get("retail_price") or item.get("final_price") or 0
        published = item.get("published", False)
        preorder = item.get("preorder", False)
        link = item.get("product_link") or product_url

        in_stock = bool(published or preorder)
        price_str = f"NT$ {int(price_val):,}" if price_val else "未標示"

        return {
            "ok": True,
            "store": "eslite",
            "asin": full_id,
            "title": title or full_id,
            "price": price_str,
            "in_stock": in_stock,
            "is_official": True,
            "seller": "誠品線上 (Eslite)",
            "url": link
        }


# =========================================================================
# 7. Shopee Checker (蝦皮 Funbox / 蝦皮商品，curl_cffi 繞過 Cloudflare/BFF)
# =========================================================================

class ShopeeChecker:
    @staticmethod
    def extract_ids(text: str) -> Tuple[str, str]:
        if not text:
            return "", ""
        text = text.strip()
        m = re.search(r"-i\.(\d+)\.(\d+)", text)
        if m:
            return m.group(1), m.group(2)
        m2 = re.search(r"/product/(\d+)/(\d+)", text)
        if m2:
            return m2.group(1), m2.group(2)
        m3 = re.search(r"(\d{6,12})[/_.](\d{6,14})", text)
        if m3:
            return m3.group(1), m3.group(2)
        return "", ""

    @classmethod
    def check_item(cls, text_or_url: str) -> Dict[str, Any]:
        shop_id, item_id = cls.extract_ids(text_or_url)
        if not shop_id or not item_id:
            url = text_or_url.strip()
        else:
            url = f"https://shopee.tw/product/{shop_id}/{item_id}"

        if not cffi_requests:
            return {"ok": False, "msg": "缺少 curl_cffi 套件，無法解析蝦皮"}

        try:
            session = cffi_requests.Session(impersonate="chrome124")
            r = session.get(url, timeout=8)
            if r.status_code == 404:
                return {"ok": False, "msg": "商品頁面不存在或已被刪除"}
            html = r.text
        except Exception as e:
            return {"ok": False, "msg": f"蝦皮請求逾時: {str(e)[:25]}"}

        m_state = re.search(r'<script[^>]*>\s*(\{"initialState":.*?)\s*</script>', html, re.DOTALL)
        if not m_state:
            return {"ok": False, "msg": "無法解析蝦皮商品狀態 (未取得初始資料)"}

        try:
            data = json.loads(m_state.group(1))
            cmap = data.get("initialState", {}).get("DOMAIN_PDP", {}).get("data", {}).get("PDP_BFF_DATA", {}).get("cachedMap", {})
        except Exception:
            cmap = {}

        if not cmap:
            in_stock = ("加入購物車" in html) and ("已售完" not in html)
            title_m = re.search(r"<title>(.*?)</title>", html)
            title = title_m.group(1) if title_m else "蝦皮商品"
            return {
                "ok": True,
                "store": "shopee",
                "asin": f"{shop_id}_{item_id}" if shop_id else url,
                "title": title,
                "price": "現貨檢視中",
                "in_stock": in_stock,
                "is_official": False,
                "seller": "蝦皮購物",
                "url": url
            }

        first_key = list(cmap.keys())[0]
        entry = cmap[first_key]
        item_data = entry.get("item") or {}
        price_data = entry.get("product_price") or {}


        title = item_data.get("title", "")
        item_status = item_data.get("item_status", "normal")
        normal_stock = item_data.get("normal_stock")
        current_stock = item_data.get("current_stock")

        price_val = price_data.get("price") or price_data.get("price_min") or 0
        if price_val:
            try:
                if price_val > 100000:
                    real_price = int(price_val / 100000)
                else:
                    real_price = int(price_val)
                price_str = f"NT$ {real_price:,}"
            except Exception:
                price_str = f"NT$ {price_val}"
        else:
            price_str = "未標示"

        if item_status in ("banned", "deleted"):
            in_stock = False
            status_desc = "已下架或封鎖"
        else:
            effective_stock = normal_stock if normal_stock is not None else current_stock
            if effective_stock is not None:
                in_stock = effective_stock > 0
                status_desc = f"庫存剩餘 {effective_stock} 件" if in_stock else "缺貨中 / 已售完"
            else:
                in_stock = ("已售完" not in html)
                status_desc = "有貨" if in_stock else "缺貨中"

        return {
            "ok": True,
            "store": "shopee",
            "asin": f"{shop_id}_{item_id}" if shop_id else url,
            "title": title or "蝦皮商品",
            "price": price_str,
            "in_stock": in_stock,
            "is_official": "funbox" in url.lower(),
            "seller": "Funbox 蝦皮官方旗艦店" if "funbox" in url.lower() else "蝦皮賣家",
            "url": url,
            "status_text": status_desc
        }


# =========================================================================
# 8. 通用調度中心 (依據 store 欄位派發)
# =========================================================================

def test_proxy_connection(proxy_url: str) -> Tuple[bool, str]:
    """測試住宅代理 (Residential Proxy) 連線與取得對外 IP"""
    if not proxy_url or not proxy_url.strip():
        return False, "未填寫代理網址"
    proxy_url = proxy_url.strip()
    proxies = {"http": proxy_url, "https": proxy_url}
    try:
        if cffi_requests:
            session = cffi_requests.Session(impersonate="chrome124", proxies=proxies)
            r = session.get("https://httpbin.org/ip", timeout=10)
            if r.status_code == 200:
                ip = r.json().get("origin", "未知")
                return True, f"✅ 代理連線成功！出口住宅 IP: {ip}"
        
        r = requests.get("https://httpbin.org/ip", proxies=proxies, timeout=10)
        if r.status_code == 200:
            ip = r.json().get("origin", "未知")
            return True, f"✅ 代理連線成功！出口住宅 IP: {ip}"
        return False, f"HTTP {r.status_code}"
    except Exception as e:
        return False, f"代理連線失敗: {str(e)[:40]}"


def check_store_item(item: Dict[str, Any], amazon_interval: float = 0.6, amazon_jitter: bool = True, use_playwright: bool = False, proxy: Optional[str] = None, only_amazon_seller: bool = True) -> Dict[str, Any]:
    """多通路統一檢查調度器 (支援 Amazon 隨機 Jitter 與住宅代理)"""
    store = item.get("store", "amazon_jp")
    asin = item.get("asin", "")

    if store == "amazon_jp":
        return AmazonJPChecker.check_asin(
            asin,
            interval=amazon_interval,
            enable_jitter=amazon_jitter,
            use_playwright=use_playwright,
            proxy=proxy,
            official_only=only_amazon_seller
        )
    elif store == "pchome":
        return PChomeChecker.check_prod(asin)
    elif store == "mm_shop":
        return MMShopChecker.check_item(asin)
    elif store in ("funbox_tw", "twj_toys"):
        return CyberbizChecker.check_prod(asin, store_key=store)
    elif store == "eslite":
        return EsliteChecker.check_prod(asin)
    elif store == "shopee":
        return ShopeeChecker.check_item(asin)
    else:
        return AmazonJPChecker.check_asin(
            asin,
            interval=amazon_interval,
            enable_jitter=amazon_jitter,
            use_playwright=use_playwright,
            proxy=proxy,
            official_only=only_amazon_seller
        )


def get_item_direct_url(item: Dict[str, Any]) -> str:
    """取得該商品的官方直接購買連結"""
    store = item.get("store", "amazon_jp")
    asin = item.get("asin", "")
    cfg = STORE_CONFIG.get(store, STORE_CONFIG["amazon_jp"])

    if store == "amazon_jp":
        return get_product_url(asin)
    elif store == "pchome":
        return f"https://24h.pchome.com.tw/prod/{asin}"
    elif store == "mm_shop":
        return f"https://mmtoyshop.com/item/{asin}"
    elif store == "funbox_tw":
        return f"https://shop.funbox.com.tw/products/{asin}"
    elif store == "twj_toys":
        return f"https://www.twj.tw/products/{asin}"
    elif store == "eslite":
        return f"https://www.eslite.com/product/{asin}"
    elif store == "shopee":
        if "_" in asin:
            sp, it = asin.split("_", 1)
            return f"https://shopee.tw/product/{sp}/{it}"
        return asin if asin.startswith("http") else f"https://shopee.tw/{asin}"
    return asin


# =========================================================================
# 9. 推播管理器 (Discord & LINE)
# =========================================================================

class NotificationManager:
    @staticmethod
    def send_discord(webhook_url: str, item_name: str, asin: str, price: str, seller: str, url: str, store: str = "amazon_jp") -> Tuple[bool, str]:
        """發送 Discord Webhook 秒殺推播"""
        if not webhook_url or not webhook_url.startswith("http"):
            return False, "未設定 Discord Webhook"

        cfg = STORE_CONFIG.get(store, STORE_CONFIG["amazon_jp"])
        store_title = cfg.get("name", "線上商城")
        flag = cfg.get("flag", "⚡")
        btn_text = cfg.get("btn_text", "👉 點此直達購買")

        payload = {
            "content": f"🚨 **【戰鬥陀螺補貨通知】** {flag} **{store_title}** 現貨開放購買！",
            "embeds": [
                {
                    "title": f"⚡ {item_name}",
                    "description": f"檢測到 {store_title} 開放現貨/預購！請立即點擊下方連結直達搶購。",
                    "url": url,
                    "color": 3066993,
                    "fields": [
                        {"name": "型號/名稱", "value": item_name, "inline": True},
                        {"name": "商品代號", "value": asin, "inline": True},
                        {"name": "即時價格", "value": price, "inline": True},
                        {"name": "販售通路", "value": f"{flag} {seller or store_title}", "inline": True},
                        {
                            "name": f"⚡ 官方直達搶購 ({btn_text})",
                            "value": f"🔥 **[【👉 點此直達商品頁搶購】]({url})**",
                            "inline": False
                        }
                    ],
                    "footer": {"text": f"{store_title} 戰鬥陀螺雲端監控器 • 極速秒殺系統"},
                    "timestamp": datetime.utcnow().isoformat()
                }
            ]
        }

        try:
            r = requests.post(webhook_url, json=payload, timeout=8)
            if r.status_code in (200, 204):
                return True, "Discord 推播成功"
            return False, f"Discord HTTP {r.status_code}"
        except Exception as e:
            return False, f"Discord 失敗: {str(e)[:30]}"

    @staticmethod
    def get_line_access_token(token_or_secret: str) -> str:
        token = token_or_secret.strip()
        if ":" in token or "," in token:
            parts = token.replace(",", ":").split(":")
            if len(parts) == 2 and parts[0].strip().isdigit():
                cid, csec = parts[0].strip(), parts[1].strip()
                try:
                    tok_res = requests.post(
                        "https://api.line.me/v2/oauth/accessToken",
                        data={"grant_type": "client_credentials", "client_id": cid, "client_secret": csec},
                        timeout=8
                    )
                    if tok_res.status_code == 200:
                        return tok_res.json().get("access_token", token)
                except Exception:
                    pass
        return token

    @classmethod
    def send_line_broadcast(cls, token_or_str: str, message: str) -> Tuple[bool, str]:
        if not token_or_str:
            return False, "未設定 LINE Token"

        token = cls.get_line_access_token(token_or_str)
        try:
            headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
            payload = {"messages": [{"type": "text", "text": message}]}
            r = requests.post("https://api.line.me/v2/bot/message/broadcast", headers=headers, json=payload, timeout=8)
            if r.status_code == 200:
                return True, "LINE 官方帳號推播成功！"
        except Exception:
            pass

        try:
            headers = {"Authorization": f"Bearer {token}"}
            r = requests.post("https://notify-api.line.me/api/notify", headers=headers, data={"message": message}, timeout=8)
            if r.status_code == 200:
                return True, "LINE Notify 發送成功！"
        except Exception:
            pass

        return False, "LINE 發送失敗"

    @classmethod
    def reply_line(cls, reply_token: str, token_or_str: str, message: str) -> bool:
        token = cls.get_line_access_token(token_or_str)
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        payload = {
            "replyToken": reply_token,
            "messages": [{"type": "text", "text": message}]
        }
        try:
            r = requests.post("https://api.line.me/v2/bot/message/reply", headers=headers, json=payload, timeout=8)
            return r.status_code == 200
        except Exception:
            return False
