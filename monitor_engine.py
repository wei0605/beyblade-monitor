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
        "impersonate": "firefox",
    },
]

AMAZON_DEFAULT_ZIPCODE = "103-0003"

def get_amazon_stealth_headers(profile: dict = None, custom_cookie: Optional[str] = None) -> Tuple[dict, str]:
    """獲取真實瀏覽器輪換 Headers 與對應 Client Hints，支援注入真實登入 Cookie (或預設日幣與日本境內郵遞區號 103-0003)"""
    if not profile:
        profile = random.choice(AMAZON_STEALTH_PROFILES)
    
    default_cookie = f"i18n-prefs=JPY; lc-acbjp=ja_JP; glow-zipcode={AMAZON_DEFAULT_ZIPCODE}"
    if custom_cookie and custom_cookie.strip():
        c_str = custom_cookie.strip().rstrip(";")
        if "i18n-prefs" not in c_str:
            c_str += "; i18n-prefs=JPY"
        if "lc-acbjp" not in c_str:
            c_str += "; lc-acbjp=ja_JP"
        if "glow-zipcode" not in c_str:
            c_str += f"; glow-zipcode={AMAZON_DEFAULT_ZIPCODE}"
        final_cookie = c_str
    else:
        final_cookie = default_cookie

    headers = {
        "User-Agent": profile["user_agent"],
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Cookie": final_cookie,
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
    """統一 Amazon 商品頁面 HTML 解析器 (精準 BuyBox、官方自營 vs 第三方賣家、庫存、價格)"""
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

    soup = BeautifulSoup(html, "html.parser")
    title_el = soup.select_one("#productTitle")
    title = title_el.get_text(strip=True) if title_el else ""

    # Buybox 購買容器
    buybox = soup.select_one("#buybox, #desktop_buybox, #tabular-buybox")

    # 1. 嚴格檢查購買按鈕 (限定於 Buybox 內，絕不跨全網頁掃描文字，徹底防止贊助廣告腳本詞彙干擾)
    has_cart = False
    has_buy_now = False
    has_preorder = False

    if buybox:
        has_cart = bool(buybox.select_one("#add-to-cart-button, [name='submit.add-to-cart']"))
        has_buy_now = bool(buybox.select_one("#buy-now-button, [name='submit.buy-now']"))
        has_preorder = bool(buybox.select_one("#preorder-button, [name='submit.preorder'], .a-button-preorder, input[value*='予約']"))
    else:
        has_cart = bool(soup.select_one("#add-to-cart-button, [name='submit.add-to-cart']"))
        has_buy_now = bool(soup.select_one("#buy-now-button, [name='submit.buy-now']"))
        has_preorder = bool(soup.select_one("#preorder-button, [name='submit.preorder']"))

    avail_el = soup.select_one("#availability")
    avail_text = avail_el.get_text(strip=True) if avail_el else ""
    is_sold_out = any(k in avail_text for k in ["現在在庫切れ", "一時的に在庫切れ", "在庫切れです", "この商品は現在お取り扱いできません"])

    no_featured_offer = (
        "おすすめ出品はありません" in html or
        "その他の出品者" in html or
        "没有精选优惠" in html
    ) and not (has_cart or has_buy_now or has_preorder)

    # 2. 精準解析價格 (優先 Buybox -> Apex -> CorePrice，嚴格排除特價劃線參考價與推薦卡片)
    price = ""
    if buybox:
        for p_elem in buybox.select(".priceToPay .a-offscreen, #price_inside_buybox, #newBuyBoxPrice, .a-price:not(.a-text-price):not(.basisPrice) .a-offscreen"):
            t = p_elem.get_text(strip=True)
            m = re.search(r"(?:JP)?\s*[￥¥]\s*([\d,]+)", t)
            if m:
                price = f"￥{m.group(1)}"
                break
            elif t.replace(",", "").isdigit():
                price = f"￥{t}"
                break

    if not price:
        apex = soup.select_one("#corePriceDisplay_desktop_feature_div, #apex_desktop, #corePrice_feature_div")
        if apex:
            for p_elem in apex.select(".priceToPay .a-offscreen, .apexPriceToPay .a-offscreen, .a-price:not(.a-text-price):not(.basisPrice) .a-offscreen"):
                t = p_elem.get_text(strip=True)
                m = re.search(r"(?:JP)?\s*[￥¥]\s*([\d,]+)", t)
                if m:
                    price = f"￥{m.group(1)}"
                    break
                elif t and t.replace(",", "").isdigit():
                    price = f"￥{t}"
                    break
            if not price:
                whole = apex.select_one(".a-price-whole")
                if whole and whole.get_text(strip=True):
                    w_txt = whole.get_text(strip=True).replace(",", "")
                    if w_txt.isdigit():
                        price = f"￥{whole.get_text(strip=True)}"

    # 備援快速正則 (僅針對中央價格區塊)
    if not price:
        m_core = re.search(r'id="corePriceDisplay_desktop_feature_div"[^>]*>.*?(?:[￥¥]\s*([\d,]+))', html, re.DOTALL)
        if m_core:
            price = f"￥{m_core.group(1)}"
        else:
            m_bb = re.search(r'id="price_inside_buybox"[^>]*>\s*([^\s<]+)\s*<', html)
            if m_bb:
                m_sub = re.search(r"(?:JP)?\s*[￥¥]\s*([\d,]+)", m_bb.group(1))
                if m_sub:
                    price = f"￥{m_sub.group(1)}"

    # 3. 庫存判定 (無有效價格或無購買按鈕，絕不可能判定為有貨，杜絕「價格載入中」偽狀態)
    has_buy_button = (has_cart or has_buy_now or has_preorder)
    in_stock = has_buy_button and bool(price) and price not in ("-", "缺貨中") and not is_sold_out and not no_featured_offer

    # 4. 精準賣家判定 (官方自營 vs 第三方真實店名)
    seller_name = ""
    merchant_text = ""
    tabular_text = ""
    is_official = False
    is_amazon_fulfilled = False

    if in_stock:
        # A. 檢查 BuyBox 內是否有第三方賣家專屬連結 (#sellerProfileTriggerId)
        bb_seller = buybox.select_one("#sellerProfileTriggerId") if buybox else None

        # B. 檢查 tabular buybox 內的出荷元與販売元
        tabular_text = ""
        if buybox:
            for tr in buybox.select("tr, .tabular-buybox-row, div[class*='tabular']"):
                row_txt = tr.get_text(" ", strip=True)
                tabular_text += " " + row_txt
                if "販売元" in row_txt or "Sold by" in row_txt:
                    s_link = tr.select_one("a, #sellerProfileTriggerId")
                    if s_link and s_link.get_text(strip=True):
                        seller_name = s_link.get_text(strip=True)
                    else:
                        parts = row_txt.split("販売元") if "販売元" in row_txt else row_txt.split("Sold by")
                        if len(parts) > 1 and parts[1].strip():
                            seller_name = parts[1].strip()
                if "出荷元" in row_txt or "Ships from" in row_txt:
                    if "amazon" in row_txt.lower() or "アマゾン" in row_txt:
                        is_amazon_fulfilled = True

        # C. 檢查 #merchant-info
        merchant_info_el = soup.select_one("#merchant-info")
        merchant_text = merchant_info_el.get_text(" ", strip=True) if merchant_info_el else ""

        if bb_seller and bb_seller.get_text(strip=True):
            raw_s = bb_seller.get_text(strip=True)
            if "amazon" in raw_s.lower() or "アマゾン" in raw_s:
                is_official = True
                seller_name = "Amazon.co.jp (官方自營)"
            else:
                is_official = False
                if is_amazon_fulfilled or "amazon" in tabular_text.lower():
                    seller_name = f"{raw_s} (第三方, Amazon 配送)"
                else:
                    seller_name = f"{raw_s} (第三方賣家)"
        elif seller_name:
            s_lower = seller_name.strip().lower()
            if s_lower in ("amazon.co.jp", "アマゾン", "amazon") or "amazon.co.jp" in s_lower:
                is_official = True
                seller_name = "Amazon.co.jp (官方自營)"
            else:
                is_official = False
                if is_amazon_fulfilled:
                    seller_name = f"{seller_name} (第三方, Amazon 配送)"
                else:
                    seller_name = f"{seller_name} (第三方賣家)"
        elif merchant_text:
            if "amazon.co.jp" in merchant_text.lower() or "アマゾン" in merchant_text:
                if "が販売" in merchant_text or "販売、発送" in merchant_text or "Amazon.co.jp が発送" not in merchant_text:
                    is_official = True
                    seller_name = "Amazon.co.jp (官方自營)"
            s_match = re.search(r'([^\s]+)\s*が販売', merchant_text)
            if s_match and not is_official:
                seller_name = f"{s_match.group(1).strip()} (第三方賣家)"

        # D. 核心原則：如果 BuyBox 有現貨購買按鈕且沒有第三方賣家 profile 標記，則必為 Amazon.co.jp 官方自營！
        if not seller_name:
            is_official = True
            seller_name = "Amazon.co.jp (官方自營)"
    else:
        price = "-"
        seller_name = "-"

    # 5. 官方自營價 vs 第三方最低價提取
    if in_stock and is_official:
        official_price = price
    else:
        official_price = "官方缺貨"

    # 第三方最低價提取 (從第三方 Buybox、#dynamic-aod-ingress-box、#olp_feature_div 等容器提取)
    tp_ints = []
    if in_stock and not is_official and price and price != "-":
        m = re.search(r"[\d,]+", price)
        if m:
            val = int(m.group(0).replace(",", ""))
            if val >= 500:
                tp_ints.append(val)

    for box in soup.select("#dynamic-aod-ingress-box, #olp_feature_div, #moreBuyingChoices_feature_div, .olp-touch-link, #all-offers-display"):
        for p_el in box.select(".a-color-price, .a-price .a-offscreen, .a-size-small.a-color-price"):
            t = p_el.get_text(strip=True)
            m = re.search(r"(?:JP)?\s*[￥¥]\s*([\d,]+)", t)
            if m:
                v = int(m.group(1).replace(",", ""))
                if v >= 500:
                    tp_ints.append(v)
        txt = box.get_text(" ", strip=True)
        for m in re.finditer(r"(?:JP)?\s*[￥¥]\s*([\d,]+)", txt):
            v = int(m.group(1).replace(",", ""))
            if v >= 500:
                tp_ints.append(v)

    # 當官方自營有貨時，第三方價格必須嚴格排除官方自營金額
    if is_official and in_stock and price:
        m_off = re.search(r"[\d,]+", price)
        if m_off:
            off_val = int(m_off.group(0).replace(",", ""))
            tp_ints = [p for p in tp_ints if p > off_val]

    third_party_cheapest = f"￥{min(tp_ints):,}" if tp_ints else "-"

    return {
        "ok": True,
        "store": "amazon_jp",
        "asin": asin,
        "title": title,
        "price": price,
        "official_price": official_price,
        "third_party_price": third_party_cheapest,
        "in_stock": in_stock,
        "is_official": is_official,
        "is_preorder": has_preorder,
        "seller": seller_name,
        "url": url,
        "raw_merchant": merchant_text,
        "no_featured_offer": no_featured_offer
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


class KeepaChecker:
    """Keepa 官方 Amazon 電商 API 庫存與價格檢測器 (極速、零封鎖風險、支援日本亞馬遜 domain=5)"""
    _last_req_time: float = 0.0
    _lock = threading.Lock()

    @classmethod
    def test_keepa_api(cls, api_key: str) -> Tuple[bool, str, int]:
        """測試 Keepa API Key 是否有效並取得剩餘 Token 額度"""
        if not api_key or not api_key.strip():
            return False, "未填寫 Keepa API Key", 0
        api_key = api_key.strip()
        url = f"https://api.keepa.com/token?key={api_key}"
        try:
            r = requests.get(url, timeout=10)
            if r.status_code == 200:
                data = r.json()
                if "tokensLeft" in data:
                    tokens = int(data.get("tokensLeft", 0))
                    refill = data.get("refillRate", 0)
                    return True, f"✅ Keepa API 連線成功！剩餘 Token: {tokens} (補充速率: {refill}/分)", tokens
            try:
                data = r.json()
                err_msg = data.get("error", {}).get("message") or f"HTTP {r.status_code}"
            except Exception:
                err_msg = f"HTTP {r.status_code}"
            return False, f"Keepa API 驗證失敗: {err_msg}", 0
        except Exception as e:
            return False, f"Keepa API 連線逾時或錯誤: {str(e)[:40]}", 0

    @classmethod
    def check_asin(cls, asin: str, api_key: str) -> Dict[str, Any]:
        """透過 Keepa Product API 查詢 Amazon.co.jp ASIN 庫存、價格與販售者 (domain=5 代表日本)"""
        asin = extract_asin(asin)
        if not asin:
            return {"ok": False, "msg": "無效 ASIN"}
        if not api_key or not api_key.strip():
            return {"ok": False, "msg": "未設定 Keepa API Key"}

        api_key = api_key.strip()
        url = f"https://api.keepa.com/product?key={api_key}&domain=5&asin={asin}&stats=1"

        try:
            r = requests.get(url, timeout=12)
            if r.status_code != 200:
                try:
                    err_data = r.json()
                    err_msg = err_data.get("error", {}).get("message") or f"HTTP {r.status_code}"
                except Exception:
                    err_msg = f"HTTP {r.status_code}"
                return {"ok": False, "msg": f"Keepa 查詢失敗: {err_msg}"}

            data = r.json()
            products = data.get("products", [])
            if not products:
                return {"ok": False, "msg": f"Keepa 未找到此商品 ({asin})"}

            prod = products[0]
            title = prod.get("title") or f"Amazon 商品 ({asin})"
            stats = prod.get("stats", {}) or {}
            current = stats.get("current", []) or []

            # Keepa stats.current 定義 (單位：日圓整數):
            # 0: AMAZON 本體售價 (-1 為無價格)
            # 1: NEW 新品第三方售價
            # 18: BUY_BOX_SHIPPING 含運購物車價
            amazon_price = current[0] if len(current) > 0 and current[0] is not None else -1
            new_price = current[1] if len(current) > 1 and current[1] is not None else -1
            buybox_price = stats.get("buyBoxPrice", -1)
            buybox_is_amazon = bool(stats.get("buyBoxIsAmazon", False))

            # availabilityAmazon: -1: 無官方, 0: 現貨可購買, 1: 預購, 2: 延遲出貨, 3: 停產
            avail_amazon = prod.get("availabilityAmazon", -1)

            in_stock = False
            is_official = False
            is_preorder = False
            price_str = "-"
            official_price = "官方缺貨"
            third_party_price = "-"
            seller_name = "-"

            if avail_amazon in (0, 1, 2) and (amazon_price > 0 or buybox_is_amazon):
                in_stock = True
                is_official = True
                if avail_amazon == 1:
                    is_preorder = True
                off_val = amazon_price if amazon_price > 0 else buybox_price
                if off_val > 0:
                    official_price = f"￥{off_val:,}"
                    price_str = official_price
                seller_name = "Amazon.co.jp (官方自營)"
            elif buybox_price and buybox_price > 0:
                in_stock = True
                is_official = buybox_is_amazon
                price_str = f"￥{buybox_price:,}"
                if is_official:
                    official_price = price_str
                    seller_name = "Amazon.co.jp (官方自營)"
                else:
                    seller_name = "第三方賣家 (BuyBox)"
                    third_party_price = price_str
            elif new_price and new_price > 0:
                in_stock = True
                is_official = False
                price_str = f"￥{new_price:,}"
                third_party_price = price_str
                seller_name = "第三方賣家"

            if new_price and new_price > 0 and third_party_price == "-":
                third_party_price = f"￥{new_price:,}"

            return {
                "ok": True,
                "asin": asin,
                "title": title,
                "price": price_str,
                "official_price": official_price,
                "third_party_price": third_party_price,
                "in_stock": in_stock,
                "is_official": is_official,
                "is_preorder": is_preorder,
                "seller": seller_name,
                "url": get_product_url(asin, official_only=is_official),
                "source": "keepa",
                "tokens_left": data.get("tokensLeft", 0)
            }
        except Exception as e:
            return {"ok": False, "msg": f"Keepa API 異常: {str(e)[:30]}"}


class AmazonJPChecker:
    """Amazon Japan 核心檢測器 (支援自訂登入 Cookie + Keepa 自動備援 + 真實 Headers 輪換 + Jitter 隨機延遲 + Chrome TLS 偽裝)"""
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
    def trigger_cooloff(cls, seconds: float = 180.0):
        with cls._lock:
            cls._cooloff_until = max(cls._cooloff_until, time.time() + seconds)

    _session_lock = threading.Lock()
    _cffi_session = None
    _session_proxy = None
    _session_cookie = None

    @classmethod
    def get_cffi_session(cls, proxy: Optional[str] = None, imp: str = "chrome124", custom_cookie: Optional[str] = None):
        """獲取已注入自訂 Cookie 或日本境內郵遞區號 (103-0003) 與 JPY 日幣的持久化連線池 Session"""
        if not cffi_requests:
            return None
        with cls._session_lock:
            if cls._cffi_session is None or cls._session_proxy != proxy or cls._session_cookie != custom_cookie:
                proxies = {"http": proxy, "https": proxy} if proxy else None
                try:
                    s = cffi_requests.Session(impersonate=imp, proxies=proxies)
                    headers, _ = get_amazon_stealth_headers(custom_cookie=custom_cookie)
                    try:
                        # 1. 若使用者有提供自訂 Cookie，直接注入，不進行可能觸發驗證碼的初始首頁訪問
                        if custom_cookie and custom_cookie.strip():
                            for part in custom_cookie.split(";"):
                                if "=" in part:
                                    k, v = part.strip().split("=", 1)
                                    s.cookies.set(k.strip(), v.strip(), domain=".amazon.co.jp")
                        else:
                            # 2. 先訪問首頁建立 session-id 與基礎 Cookie
                            s.get("https://www.amazon.co.jp/", headers=headers, timeout=10)
                            # 注入日本境內郵遞區號 103-0003 (東京都中央區日本橋)
                            addr_url = "https://www.amazon.co.jp/portal-migration/hz/glow/address-change?actionSource=glow"
                            s.post(
                                addr_url,
                                headers={**headers, "Content-Type": "application/x-www-form-urlencoded"},
                                data={
                                    "locationType": "LOCATION_INPUT",
                                    "zipCode": AMAZON_DEFAULT_ZIPCODE,
                                    "storeContext": "generic",
                                    "deviceType": "web",
                                    "pageType": "Detail",
                                    "actionSource": "glow"
                                },
                                timeout=8
                            )
                        # 3. 嚴格鎖定幣別為日圓 JPY 與日語 ja_JP
                        s.cookies.set("i18n-prefs", "JPY", domain=".amazon.co.jp")
                        s.cookies.set("lc-acbjp", "ja_JP", domain=".amazon.co.jp")
                        s.cookies.set("glow-zipcode", AMAZON_DEFAULT_ZIPCODE, domain=".amazon.co.jp")
                    except Exception:
                        pass
                    cls._cffi_session = s
                    cls._session_proxy = proxy
                    cls._session_cookie = custom_cookie
                except Exception:
                    cls._cffi_session = None
            return cls._cffi_session

    @classmethod
    def reset_cffi_session(cls):
        with cls._session_lock:
            cls._cffi_session = None

    @classmethod
    def check_asin(
        cls,
        asin: str,
        interval: float = 0.6,
        enable_jitter: bool = True,
        use_playwright: bool = False,
        proxy: Optional[str] = None,
        official_only: bool = False,
        custom_cookie: Optional[str] = None,
        keepa_api_key: Optional[str] = None,
        keepa_mode: str = "fallback"
    ) -> Dict[str, Any]:
        """極速檢測 Amazon.co.jp 特定 ASIN 庫存 (支援自訂登入 Cookie、Keepa 備援與優先模式、Jitter延遲、TLS 偽裝與住宅代理)"""
        asin = extract_asin(asin)
        if not asin:
            return {"ok": False, "msg": "無效 ASIN"}

        # 若使用者指定優先使用 Keepa 官方 API
        if keepa_mode == "primary" and keepa_api_key:
            keepa_res = KeepaChecker.check_asin(asin, keepa_api_key)
            if keepa_res.get("ok"):
                return keepa_res
            # 若 Keepa 失敗，自動嘗試降級回爬蟲繼續執行

        # 若使用者指定啟用 Playwright 且環境支援，直接走真實瀏覽器
        if use_playwright and PlaywrightAmazonChecker.is_available():
            cls.throttle(interval, enable_jitter=enable_jitter)
            return PlaywrightAmazonChecker.check_asin(asin)

        cls.throttle(interval, enable_jitter=enable_jitter)
        url = get_product_url(asin, official_only=official_only)
        headers, imp = get_amazon_stealth_headers(custom_cookie=custom_cookie)

        html = None
        status_code = 200
        proxies = {"http": proxy, "https": proxy} if proxy else None

        # 第一優先：curl_cffi Chrome 124 TLS 指紋偽裝 (支援持久化連線池與自訂登入 Cookie)
        if cffi_requests:
            try:
                session = cls.get_cffi_session(proxy=proxy, imp=imp, custom_cookie=custom_cookie)
                if session:
                    r = session.get(url, headers=headers, timeout=12)
                    status_code = r.status_code
                    html = r.text
            except Exception:
                cls.reset_cffi_session()
                html = None

        # 第二優先：requests 連線池 (帶真實 Headers 輪換與住宅代理)
        if not html:
            try:
                session = get_shared_session()
                r = session.get(url, headers=headers, proxies=proxies, timeout=10)
                status_code = r.status_code
                html = r.text
            except Exception as e:
                if keepa_api_key and keepa_mode in ("fallback", "primary"):
                    kp_res = KeepaChecker.check_asin(asin, keepa_api_key)
                    if kp_res.get("ok"):
                        kp_res["status_note"] = "連線超時，Keepa 備援接手"
                        return kp_res
                return {"ok": False, "msg": f"網路超時: {str(e)[:25]}"}

        if not html:
            if keepa_api_key and keepa_mode in ("fallback", "primary"):
                kp_res = KeepaChecker.check_asin(asin, keepa_api_key)
                if kp_res.get("ok"):
                    kp_res["status_note"] = "頁面為空，Keepa 備援接手"
                    return kp_res
            return {"ok": False, "msg": "無法獲取頁面內容"}

        # 檢測 CAPTCHA 或 503 頻率限制
        is_captcha = ("/errors_page/validateCaptcha" in html or "api-services-support@amazon.com" in html)
        is_503 = (status_code == 503)

        if is_captcha or is_503:
            cls.trigger_cooloff(180.0)
            # 【方案 5 關鍵】：若有 Keepa API Key，無縫自動降級切換為 Keepa API 查詢！
            if keepa_api_key and keepa_mode in ("fallback", "primary"):
                kp_res = KeepaChecker.check_asin(asin, keepa_api_key)
                if kp_res.get("ok"):
                    kp_res["status_note"] = "Amazon 觸發風控，已由 Keepa 官方 API 接手"
                    return kp_res

            if PlaywrightAmazonChecker.is_available():
                return PlaywrightAmazonChecker.check_asin(asin)
            
            reason = "CAPTCHA 驗證" if is_captcha else "503 頻率限制"
            return {"ok": False, "msg": f"Amazon {reason} (建議填寫 Cookie/住宅代理，或填入 Keepa API Key 啟用自動備援)"}

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

        is_funbox = any(k in (title or "").lower() for k in ["funbox", "麗嬰", "麗嬰國際"])
        seller = "funbox 麗嬰國際 (PChome)" if is_funbox else "PChome 24h 購物"
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
    """測試住宅代理 (Residential Proxy) 連線與取得對外 IP (支援自動補全 http:// 與多端點重試)"""
    if not proxy_url or not proxy_url.strip():
        return False, "未填寫代理網址"
    proxy_url = proxy_url.strip()
    if not (proxy_url.startswith("http://") or proxy_url.startswith("https://") or proxy_url.startswith("socks5://")):
        proxy_url = f"http://{proxy_url}"

    proxies = {"http": proxy_url, "https": proxy_url}
    ip_services = [
        "https://api.ipify.org?format=json",
        "https://httpbin.org/ip"
    ]

    last_err = ""
    for test_url in ip_services:
        try:
            if cffi_requests:
                session = cffi_requests.Session(impersonate="chrome124", proxies=proxies)
                r = session.get(test_url, timeout=15)
                if r.status_code == 200:
                    data = r.json()
                    ip = data.get("ip") or data.get("origin") or "未知"
                    return True, f"✅ 代理連線成功！出口住宅 IP: {ip}"
            
            r = requests.get(test_url, proxies=proxies, timeout=15)
            if r.status_code == 200:
                data = r.json()
                ip = data.get("ip") or data.get("origin") or "未知"
                return True, f"✅ 代理連線成功！出口住宅 IP: {ip}"
        except Exception as e:
            last_err = str(e)
            continue

    return False, f"代理連線超時: {last_err[:50]}"


def check_store_item(
    item: Dict[str, Any],
    amazon_interval: float = 0.6,
    amazon_jitter: bool = True,
    use_playwright: bool = False,
    proxy: Optional[str] = None,
    only_amazon_seller: bool = True,
    custom_cookie: Optional[str] = None,
    keepa_api_key: Optional[str] = None,
    keepa_mode: str = "fallback"
) -> Dict[str, Any]:
    """多通路統一檢查調度器 (支援 Amazon 隨機 Jitter、自訂登入 Cookie、Keepa 備援與住宅代理)"""
    store = item.get("store", "amazon_jp")
    asin = item.get("asin", "")

    if store == "amazon_jp":
        return AmazonJPChecker.check_asin(
            asin,
            interval=amazon_interval,
            enable_jitter=amazon_jitter,
            use_playwright=use_playwright,
            proxy=proxy,
            official_only=False,
            custom_cookie=custom_cookie,
            keepa_api_key=keepa_api_key,
            keepa_mode=keepa_mode
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
            official_only=False,
            custom_cookie=custom_cookie,
            keepa_api_key=keepa_api_key,
            keepa_mode=keepa_mode
        )


def get_item_direct_url(item: Dict[str, Any]) -> str:
    """取得該商品的官方直接購買連結 (Amazon 帶有 m=AN1VRQENFRJN5 官方直達)"""
    store = item.get("store", "amazon_jp")
    asin = item.get("asin", "")
    cfg = STORE_CONFIG.get(store, STORE_CONFIG["amazon_jp"])

    if store == "amazon_jp":
        return get_product_url(asin, official_only=True)
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
