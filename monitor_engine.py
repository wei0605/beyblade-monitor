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
from typing import Dict, Any, Tuple, Optional, List

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
        "name": "蝦皮 funbox 特賣會",
        "short_name": "蝦皮 Funbox",
        "icon": "fa-solid fa-shrimp",
        "flag": "🦐",
        "color": "#f97316",
        "btn_text": "🦐 蝦皮特賣會直達",
        "default_url": "https://shopee.tw/funbox5120",
        "id_label": "蝦皮商品網址或代號",
        "id_placeholder": "例如: https://shopee.tw/product/285705541/... 或 -i.285705541.{itemid}",
    },
    "amazon_stealth": {
        "key": "amazon_stealth",
        "name": "Amazon 突襲上架清單",
        "short_name": "Amazon 突襲",
        "icon": "fa-solid fa-bolt",
        "flag": "⚡",
        "color": "#eab308",
        "btn_text": "⚡ Amazon 直達",
        "default_url": "https://www.amazon.co.jp/dp/{id}?m=AN1VRQENFRJN5&th=1&psc=1",
        "id_label": "ASIN 或 Amazon 網址",
        "id_placeholder": "例如: B0HJ783F18 或 突襲待公布 ASIN",
    }
}


_shared_session_initialized = False
_shared_session_lock = threading.Lock()

def get_shared_session(proxy: Optional[str] = None):
    """共用 HTTP 連線池，最大化連線重用並確保具備日本境內郵遞區號 (103-0003) 與日幣憑證"""
    global _shared_session_initialized
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

    s = get_shared_session._session
    if not _shared_session_initialized:
        with _shared_session_lock:
            if not _shared_session_initialized:
                try:
                    headers, _ = get_amazon_stealth_headers()
                    addr_url = "https://www.amazon.co.jp/portal-migration/hz/glow/address-change?actionSource=glow"
                    proxies = {"http": proxy, "https": proxy} if proxy else None
                    s.post(
                        addr_url,
                        headers={**headers, "Content-Type": "application/x-www-form-urlencoded", "x-requested-with": "XMLHttpRequest"},
                        data={
                            "locationType": "LOCATION_INPUT",
                            "zipCode": AMAZON_DEFAULT_ZIPCODE,
                            "storeContext": "generic",
                            "deviceType": "web",
                            "pageType": "Detail",
                            "actionSource": "glow"
                        },
                        proxies=proxies,
                        timeout=8
                    )
                    s.cookies.set("i18n-prefs", "JPY", domain=".amazon.co.jp")
                    s.cookies.set("lc-acbjp", "ja_JP", domain=".amazon.co.jp")
                    _shared_session_initialized = True
                except Exception:
                    pass
    return s


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


def clean_seller_name(s: str) -> str:
    """清理賣家與出貨者名稱，移除前後綴標籤 (例如：販売元:、出荷元:、卖家:、Sold by:)"""
    if not s:
        return ""
    cleaned = re.sub(
        r'^(?:販売元|出荷元|卖家|賣家|發貨人|发货人|銷售商|销售商|出品者|由|from|ships\s+from(?:\s+and\s+sold\s+by)?|sold\s+by)[\s:：/／・]*',
        '',
        s.strip(),
        flags=re.IGNORECASE
    ).strip()
    return cleaned


def is_amazon_name(s: str) -> bool:
    """精準判定賣家或出貨者是否為 Amazon 官方自營 (支援多國語言與前綴後綴過濾)"""
    if not s:
        return False
    s_low = s.lower().strip()

    # 1. 移除常見前綴標籤
    s_clean = re.sub(
        r'^(?:販売元|出荷元|卖家|賣家|發貨人|发货人|銷售商|销售商|出品者|由|from|ships\s+from(?:\s+and\s+sold\s+by)?|sold\s+by)[\s:：/／・]*',
        '',
        s_low,
        flags=re.IGNORECASE
    ).strip()

    # 2. 移除常見後綴標籤
    s_clean = re.sub(
        r'[\s:：/／・]*(?:が発送.*|が販売.*|配送|発送|发货|發貨|\(官方自營\)|\(官方\))$',
        '',
        s_clean,
        flags=re.IGNORECASE
    ).strip()

    # 3. 官方標準名稱直接匹配
    if s_clean in (
        "amazon.co.jp", "amazon", "アマゾン", "amazon japan", "amazon.com",
        "亚马逊", "亞馬遜", "amazon official", "amazon direct"
    ):
        return True

    if s_clean.startswith("amazon.co.jp") or s_clean.startswith("amazon japan") or s_clean.startswith("amazon.com"):
        return True

    # 4. 包含 amazon / アマゾン / 亞馬遜 / 亚马逊 的安全比對
    if re.search(r'\bamazon(?:\.co\.jp|\.com)?\b', s_clean) or any(k in s_clean for k in ("アマゾン", "亞馬遜", "亚马逊")):
        # 排除第三方店名關鍵字 (評分、店鋪、專營、代購等)
        if re.search(r'(?:出品者|seller|score|マーケットプレイス|store|shop|商店|專營|专营|代購|代购)', s_clean):
            return False
        core_tokens = re.findall(r'[\w\u4e00-\u9fff\u3040-\u30ff]+', s_clean)
        allowed_tokens = {"amazon", "co", "jp", "com", "japan", "アマゾン", "亞馬遜", "亚马逊", "配送", "自營", "自营", "官方", "直營", "直营", "us"}
        if core_tokens and all(tok in allowed_tokens for tok in core_tokens):
            return True

    return False


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
    buybox = soup.select_one("#buybox, #desktop_buybox, #desktop_qualifiedBuyBox, #tabular-buybox, #qualifiedBuybox, div[id*='buyBox'], div[id*='buybox']")

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
    buybox_shipping = 0
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

        deliv_elem = buybox.select_one("[data-csa-c-delivery-price]")
        if deliv_elem and deliv_elem.get("data-csa-c-delivery-price"):
            m_shp = re.search(r"[\d,]+", deliv_elem.get("data-csa-c-delivery-price"))
            if m_shp:
                buybox_shipping = int(m_shp.group(0).replace(",", ""))

        if buybox_shipping == 0:
            deliv_box = buybox.select_one("#deliveryMessageMirId, #mir-layout-DELIVERY_BLOCK, [id*='delivery'], [id*='ship'], .delivery-message, #delivery-block")
            deliv_text = deliv_box.get_text(" ", strip=True) if deliv_box else ""
            if deliv_text and not any(k in deliv_text for k in ["無料配送", "送料無料", "Free Delivery", "Prime", "免運", "免費配送"]):
                m_shp = re.search(r'(?:配送料|送料|配送費|配送|delivery)[^\d￥¥]{0,10}[￥¥]\s*([\d,]+)', deliv_text, re.I)
                if not m_shp:
                    m_shp = re.search(r'\+\s*[￥¥]\s*([\d,]+)', deliv_text)
                if m_shp:
                    buybox_shipping = int(m_shp.group(1).replace(",", ""))

        if buybox_shipping == 0:
            for row in buybox.select("tr, .tabular-buybox-row"):
                r_txt = row.get_text(" ", strip=True)
                if any(k in r_txt for k in ["配送料", "送料", "Shipping"]):
                    m_shp = re.search(r'[￥¥]\s*([\d,]+)', r_txt)
                    if m_shp:
                        buybox_shipping = int(m_shp.group(1).replace(",", ""))
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

    # 若 BuyBox 含有運費 (例如第三方自出貨 FBM)，將運費加入價格中顯示
    if price and buybox_shipping > 0 and price not in ("-", "缺貨中"):
        m_p = re.search(r"[\d,]+", price)
        if m_p:
            p_val = int(m_p.group(0).replace(",", ""))
            price = f"￥{p_val + buybox_shipping:,}"

    # 3. 庫存判定 (無有效價格或無購買按鈕，絕不可能判定為有貨，杜絕「價格載入中」偽狀態)
    has_buy_button = (has_cart or has_buy_now or has_preorder)
    in_stock = has_buy_button and bool(price) and price not in ("-", "缺貨中") and not is_sold_out and not no_featured_offer

    # 4. 精準賣家判定 (嚴格規定：賣家是 Amazon 且 運送也是 Amazon，才算官方自營！)
    raw_seller = ""
    raw_fulfiller = ""

    if in_stock:
        # A. 現代 Amazon BuyBox ODF (Offer Display Feature)
        m_feat = soup.select_one('[offer-display-feature-name="desktop-merchant-info"] [data-csa-c-slot-id="odf-feature-text-desktop-merchant-info"], [offer-display-feature-name="desktop-merchant-info"] .offer-display-feature-text, [offer-display-feature-name="desktop-merchant-info"] a')
        if m_feat and m_feat.get_text(strip=True):
            raw_seller = clean_seller_name(m_feat.get_text(strip=True))
        elif soup.select('[offer-display-feature-name="desktop-merchant-info"]'):
            cand = soup.select('[offer-display-feature-name="desktop-merchant-info"]')[-1].get_text(" ", strip=True)
            raw_seller = clean_seller_name(cand)

        f_feat = soup.select_one('[offer-display-feature-name="desktop-fulfiller-info"] [data-csa-c-slot-id="odf-feature-text-desktop-fulfiller-info"], [offer-display-feature-name="desktop-fulfiller-info"] .offer-display-feature-text')
        if f_feat and f_feat.get_text(strip=True):
            raw_fulfiller = clean_seller_name(f_feat.get_text(strip=True))
        elif soup.select('[offer-display-feature-name="desktop-fulfiller-info"]'):
            cand = soup.select('[offer-display-feature-name="desktop-fulfiller-info"]')[-1].get_text(" ", strip=True)
            raw_fulfiller = clean_seller_name(cand)

        # B. 傳統表格 Tabular BuyBox (支援日文、繁簡中文、英文及「发货人 / 卖家」合併行)
        bb_container = buybox or soup.select_one("#tabular-buybox, #desktop_qualifiedBuyBox, #buybox, div[id*='buyBox'], div[id*='buybox']")
        if bb_container:
            combined_pattern = r'(?:发货人\s*[/／]\s*卖家|發貨人\s*[/／]\s*賣家|出荷元\s*[/／・]\s*販売元|Ships\s+from\s*[/／]\s*Sold\s+by)'
            seller_pattern = r'(?:販売元|Sold\s+by|卖家|賣家|销售商|銷售商|出品者)'
            fulfiller_pattern = r'(?:出荷元|Ships\s+from|发货人|發貨人|出货元|出貨元)'

            for row in bb_container.select("tr, .tabular-buybox-row, div[class*='tabular']"):
                row_txt = row.get_text(" ", strip=True)
                left_col = row.select_one(".tabular-buybox-column-left, td:first-child, .tabular-buybox-label, th")
                right_col = row.select_one(".tabular-buybox-column-right, td:last-child, .tabular-buybox-value, td:nth-child(2)")
                left_txt = left_col.get_text(" ", strip=True) if left_col else ""
                right_txt = right_col.get_text(" ", strip=True) if right_col else ""

                # 檢查合一標籤 (例如：发货人 / 卖家 Amazon.co.jp)
                if re.search(combined_pattern, left_txt or row_txt, re.IGNORECASE):
                    val = right_txt
                    if not val:
                        parts = re.split(combined_pattern, row_txt, flags=re.IGNORECASE)
                        if len(parts) > 1 and parts[1].strip():
                            val = parts[1].strip()
                    if val:
                        c_val = clean_seller_name(val)
                        if not raw_seller:
                            raw_seller = c_val
                        if not raw_fulfiller:
                            raw_fulfiller = c_val
                    continue

                # 賣家 row
                if re.search(seller_pattern, left_txt or row_txt, re.IGNORECASE) and not raw_seller:
                    s_link = row.select_one("a, #sellerProfileTriggerId")
                    if s_link and s_link.get_text(strip=True):
                        raw_seller = clean_seller_name(s_link.get_text(strip=True))
                    elif right_txt:
                        raw_seller = clean_seller_name(right_txt)
                    else:
                        parts = re.split(seller_pattern, row_txt, flags=re.IGNORECASE)
                        if len(parts) > 1 and parts[1].strip():
                            raw_seller = clean_seller_name(parts[1].strip())

                # 出貨/配送 row
                if re.search(fulfiller_pattern, left_txt or row_txt, re.IGNORECASE) and not raw_fulfiller:
                    if right_txt:
                        raw_fulfiller = clean_seller_name(right_txt)
                    else:
                        parts = re.split(fulfiller_pattern, row_txt, flags=re.IGNORECASE)
                        if len(parts) > 1 and parts[1].strip():
                            raw_fulfiller = clean_seller_name(parts[1].strip())

        # C. 傳統 #merchant-info (嚴格區分賣家與寄送者，避免國際/中文/日文頁面誤判)
        m_info = soup.select_one("#merchant-info")
        if m_info:
            # 先檢查是否有第三方賣家專屬連結
            seller_link = m_info.select_one("a[href*='seller'], a[href*='shops'], a#sellerProfileTriggerId, a")
            if seller_link and seller_link.get_text(strip=True):
                if not raw_seller:
                    raw_seller = clean_seller_name(seller_link.get_text(strip=True))

            m_txt = m_info.get_text(" ", strip=True)

            # 先判斷是否為官方自營專屬文案 (必須是 Amazon「販売/销售」或「sold by Amazon」，且不可帶有第三方連結)
            official_keywords = [
                "Amazon.co.jp が販売", "アマゾンが販売", "Amazon.co.jpが販売", "アマゾン が販売", "販売、発送します",
                "Amazon.co.jp 发货和销售", "Amazon.co.jp 销售并", "由 Amazon.co.jp 发货并销售", "由 Amazon.co.jp 销售",
                "由 Amazon.co.jp 發貨並銷售", "由 Amazon.co.jp 銷售", "发货并销售", "發貨並銷售"
            ]
            if any(k in m_txt for k in official_keywords) or "sold by amazon" in m_txt.lower():
                if not seller_link:
                    raw_seller = "Amazon.co.jp"

            if not raw_seller:
                # 日文: 提取 が販売 之前的店名
                s_match = re.search(r'(?:この商品は、|この出品は、)?\s*([^,、\n]+?)\s*が販売', m_txt)
                if s_match:
                    cand_s = clean_seller_name(s_match.group(1).strip())
                    raw_seller = "Amazon.co.jp" if is_amazon_name(cand_s) else cand_s
                # 中文: 由 XXX 销售 / 由 XXX 銷售
                s_match_cn = re.search(r'(?:此商品由|本商品由|由)?\s*([^,，、\n]+?)\s*(?:销售|銷售|發貨並銷售|发货并销售)', m_txt)
                if s_match_cn and not raw_seller:
                    cand_s = clean_seller_name(s_match_cn.group(1).strip())
                    raw_seller = "Amazon.co.jp" if is_amazon_name(cand_s) else cand_s
                # 英文: sold by XXX
                s_match_en = re.search(r'sold by\s+([^.,\n]+)', m_txt, re.I)
                if s_match_en and not raw_seller:
                    cand_s = clean_seller_name(s_match_en.group(1).strip())
                    raw_seller = "Amazon.co.jp" if is_amazon_name(cand_s) else cand_s

            # 檢查運送
            if not raw_fulfiller:
                if any(k in m_txt for k in ["Amazon.co.jp が発送", "amazon が発送", "アマゾンが発送", "Amazon.co.jpが発送", "販売、発送します", "由 Amazon 配送", "由 Amazon.co.jp 配送", "由Amazon配送", "Amazon配送", "由 Amazon 发货", "由 Amazon 發貨"]):
                    raw_fulfiller = "Amazon.co.jp"
                elif "ships from amazon" in m_txt.lower() or "fulfilled by amazon" in m_txt.lower() or "ships from and sold by amazon" in m_txt.lower():
                    raw_fulfiller = "Amazon.co.jp"

        # D. 檢查 BuyBox 內的 sellerProfileTriggerId 連結
        if not raw_seller and bb_container:
            bb_seller = bb_container.select_one("#sellerProfileTriggerId")
            if bb_seller and bb_seller.get_text(strip=True):
                raw_seller = clean_seller_name(bb_seller.get_text(strip=True))

        # E. 檢查 BuyBox 內是否有配送標籤
        if not raw_fulfiller and bb_container:
            bb_txt = bb_container.get_text(" ", strip=True)
            if any(k in bb_txt for k in ["由Amazon配送", "由 Amazon 配送", "Amazon配送", "アマゾンが発送", "Amazon.co.jp が発送", "Ships from Amazon", "Fulfilled by Amazon"]):
                raw_fulfiller = "Amazon.co.jp"

        is_seller_amazon = is_amazon_name(raw_seller)
        is_fulfiller_amazon = is_amazon_name(raw_fulfiller)

        # 嚴格判定：賣家是 Amazon 且 運送也是 Amazon 才是官方自營！
        if is_seller_amazon and is_fulfiller_amazon:
            is_official = True
            seller_name = "Amazon.co.jp (官方自營)"
        else:
            is_official = False
            clean_s = clean_seller_name(raw_seller)
            if clean_s:
                if is_fulfiller_amazon:
                    seller_name = f"{clean_s} (第三方, Amazon 配送)"
                else:
                    seller_name = f"{clean_s} (第三方賣家)"
            else:
                seller_name = "第三方賣家"
    else:
        price = "-"
        seller_name = "-"
        is_official = False

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

    for box in soup.select("#dynamic-aod-ingress-box, #olp_feature_div, #moreBuyingChoices_feature_div, .olp-touch-link, div[id*='aod-ingress'], div[id*='unqualified-buybox'], div[id*='buying-options']"):
        for p_el in box.select(".a-color-price, .a-price .a-offscreen, .a-price-whole, .a-size-small.a-color-price, .apex-pricetopay-value"):
            t = p_el.get_text(strip=True)
            m = re.search(r"(?:JP)?\s*[￥¥]\s*([\d,]+)", t)
            if m:
                v = int(m.group(1).replace(",", ""))
                if v >= 500:
                    tp_ints.append(v)

    # 當官方自營有貨時，第三方價格必須嚴格排除官方自營金額 (官方售價絕不可變為第三方價格)
    if is_official and in_stock and price:
        m_off = re.search(r"[\d,]+", price)
        if m_off:
            off_val = int(m_off.group(0).replace(",", ""))
            tp_ints = [p for p in tp_ints if p != off_val]

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
        "raw_merchant": f"Seller: {raw_seller}, Fulfiller: {raw_fulfiller}",
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

            if new_price and new_price > 0:
                off_val = None
                if is_official and official_price != "官方缺貨":
                    m_off = re.search(r"[\d,]+", official_price)
                    if m_off:
                        off_val = int(m_off.group(0).replace(",", ""))

                # 嚴格排除官方售價被誤歸為第三方最低價
                if off_val is None or new_price != off_val:
                    if third_party_price == "-":
                        third_party_price = f"￥{new_price:,}"
                    else:
                        m_tp = re.search(r"[\d,]+", third_party_price)
                        if m_tp:
                            cur_tp_val = int(m_tp.group(0).replace(",", ""))
                            if new_price < cur_tp_val:
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
                session = get_shared_session(proxy=proxy)
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

        res = parse_amazon_html(html, asin, status_code=status_code, url=url)

        # 深度提取：若非官方自營有貨，或沒有精選優惠(no_featured_offer)，或目前無第三方報價，
        # 額外自 AOD (All Offers Display) 提取所有第三方賣家 (包含運送為個人賣家/非官方自出貨 FBM) 的最低價
        if not res.get("is_official", False) or res.get("no_featured_offer", False) or res.get("third_party_price") == "-":
            try:
                aod_url = f"https://www.amazon.co.jp/gp/product/ajax/aodAjaxMain?asin={asin}&pc=dp"
                s_to_use = session if ('session' in locals() and session) else get_shared_session(proxy=proxy)
                if s_to_use:
                    aod_headers = {k: v for k, v in headers.items() if k.lower() != "cookie"}
                    aod_headers["Referer"] = url
                    r_aod = s_to_use.get(aod_url, headers=aod_headers, proxies=proxies, timeout=6)
                    if r_aod.status_code == 200 and len(r_aod.text) > 500:
                        aod_soup = BeautifulSoup(r_aod.text, "html.parser")
                        aod_tp_ints = []
                        for of in aod_soup.select("#aod-pinned-offer, #aod-offer"):
                            s_el = of.select_one("#aod-offer-soldBy, [id*='soldBy']")
                            f_el = of.select_one("#aod-offer-shipsFrom, [id*='shipsFrom']")

                            s_txt = ""
                            if s_el:
                                s_right = s_el.select_one(".a-col-right, td:last-child")
                                if s_right:
                                    s_link = s_right.select_one("a")
                                    s_txt = s_link.get_text(strip=True) if s_link else s_right.get_text(" ", strip=True)
                                else:
                                    s_txt = s_el.get_text(" ", strip=True)

                            f_txt = ""
                            if f_el:
                                f_right = f_el.select_one(".a-col-right, td:last-child")
                                f_txt = f_right.get_text(" ", strip=True) if f_right else f_el.get_text(" ", strip=True)

                            has_tp_link = bool(s_el and s_el.select_one("a[href*='seller'], a[href*='shops'], #sellerProfileTriggerId"))
                            is_s_amz = is_amazon_name(s_txt) and not has_tp_link
                            is_f_amz = is_amazon_name(f_txt)
                            is_offer_official = is_s_amz and is_f_amz

                            found_p = None
                            for p_el in of.select(".a-price .a-offscreen, .a-price-whole, .apex-pricetopay-value, [id^='aod-price-']"):
                                t = p_el.get_text(strip=True)
                                m = re.search(r"[\d,]+", t)
                                if m:
                                    v = int(m.group(0).replace(",", ""))
                                    if v >= 500:
                                        found_p = v
                                        break

                            # 提取運費 (含自出貨 FBM 個人賣家、未達免運門檻等運費，嚴格加總)
                            shipping_fee = 0
                            deliv_p_el = of.select_one("[data-csa-c-delivery-price]")
                            if deliv_p_el and deliv_p_el.get("data-csa-c-delivery-price"):
                                m_shp = re.search(r"[\d,]+", deliv_p_el.get("data-csa-c-delivery-price"))
                                if m_shp:
                                    shipping_fee = int(m_shp.group(0).replace(",", ""))

                            if shipping_fee == 0:
                                deliv_box = of.select_one(".aod-delivery-promise-column, .aod-unified-delivery, [id*='delivery'], [id*='ship']")
                                deliv_text = deliv_box.get_text(" ", strip=True) if deliv_box else of.get_text(" ", strip=True)
                                if not any(k in deliv_text for k in ["無料配送", "送料無料", "Free Delivery", "Prime", "免運", "免費配送"]):
                                    m_shp = re.search(r'(?:配送料|送料|配送費|配送|delivery)[^\d￥¥]{0,10}[￥¥]\s*([\d,]+)', deliv_text, re.I)
                                    if not m_shp:
                                        m_shp = re.search(r'\+\s*[￥¥]\s*([\d,]+)', deliv_text)
                                    if m_shp:
                                        shipping_fee = int(m_shp.group(1).replace(",", ""))

                            if found_p:
                                total_offer_price = found_p + shipping_fee
                                if is_offer_official:
                                    res["is_official"] = True
                                    res["official_price"] = f"￥{found_p:,}"
                                    res["seller"] = "Amazon.co.jp (官方自營)"
                                    res["in_stock"] = True
                                else:
                                    # 包含所有個人賣家、FBM (賣家自出貨) 以及 FBA，嚴格加上運費
                                    aod_tp_ints.append(total_offer_price)

                        all_cands = []
                        cur_tp = res.get("third_party_price", "-")
                        if cur_tp and cur_tp != "-":
                            m = re.search(r"[\d,]+", cur_tp)
                            if m:
                                all_cands.append(int(m.group(0).replace(",", "")))
                        all_cands.extend(aod_tp_ints)

                        if res.get("is_official") and res.get("official_price") and res["official_price"] != "官方缺貨":
                            m_off = re.search(r"[\d,]+", res["official_price"])
                            if m_off:
                                off_val = int(m_off.group(0).replace(",", ""))
                                all_cands = [p for p in all_cands if p != off_val]

                        if all_cands:
                            res["third_party_price"] = f"￥{min(all_cands):,}"
                        elif res.get("is_official"):
                            res["third_party_price"] = "-"
            except Exception:
                pass

        return res


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
    def is_official_funbox(cls, pid: str, name: str = "", session=None) -> Tuple[bool, str]:
        """檢核 PChome 商品是否為官方 Funbox 麗嬰國際直營上架
        官方商品特色：商品頁標題必含 'funbox 麗嬰國際'，或品名明確標示 'funbox 麗嬰國際'。
        非官方轉賣商通常為 'TAKARA TOMY ...'、'日本代購'、'水貨'。
        """
        # 1. 先行快速過濾品名 (若品名本身已明確標示 funbox 麗嬰國際)
        if "funbox 麗嬰國際" in name or ("funbox" in name.lower() and "麗嬰國際" in name):
            return True, name

        if not pid:
            return False, name

        # 2. 透過 PChome 搜尋 API 反向驗證商品索引是否帶有 "funbox 麗嬰國際" (精準且無 429 風險)
        try:
            r = requests.get(f"https://ecshweb.pchome.com.tw/search/v3.3/all/results?q=funbox 麗嬰國際 {pid}", timeout=3)
            if r.status_code == 200:
                for p in r.json().get("prods", []):
                    if p.get("Id") == pid:
                        prod_title = p.get("name") or name
                        return True, f"funbox 麗嬰國際 {prod_title}" if "funbox" not in prod_title.lower() else prod_title
        except Exception:
            pass

        # 3. 備援：若可連線商品頁 HTML，檢驗 <title> 是否含有 "funbox 麗嬰國際"
        try:
            r = requests.get(f"https://24h.pchome.com.tw/prod/{pid}", headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}, timeout=3)
            if r.status_code == 200:
                m = re.search(r"<title>(.*?)</title>", r.text, re.IGNORECASE)
                page_title = m.group(1).strip() if m else ""
                t_lower = page_title.lower()
                if "funbox 麗嬰國際" in page_title or ("funbox" in t_lower and "麗嬰" in page_title):
                    return True, page_title
        except Exception:
            pass

        return False, name

    @classmethod
    def check_pchome_stealth(cls, keyword: str) -> Dict[str, Any]:
        """PChome 關鍵字突襲搜尋監控 (僅限 Funbox 麗嬰國際官方上架)"""
        search_url = f"https://ecshweb.pchome.com.tw/search/v3.3/all/results?q={keyword}"
        session = get_shared_session()
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            "Accept": "application/json"
        }
        try:
            rs = session.get(search_url, headers=headers, timeout=5)
            if rs.status_code == 200:
                s_data = rs.json()
                clean_kw = keyword.lower().replace("-", "").strip()
                for p in s_data.get("prods", []):
                    name = p.get("name", "")
                    name_clean = name.lower().replace("-", "")
                    if clean_kw in name_clean and any(k in name.lower() for k in ["beyblade", "戰鬥陀螺", "陀螺", "takara"]):
                        pid = p.get("Id", "")
                        price_val = p.get("price", 0)
                        price_str = f"NT$ {price_val:,}" if price_val else "未標示"
                        is_funbox, page_title = cls.is_official_funbox(pid, name, session=session)
                        if not is_funbox:
                            # 非 Funbox 麗嬰國際官方上架，為第三方轉賣或平行輸入，略過不判定為官方突襲現貨
                            continue
                        return {
                            "ok": True,
                            "store": "pchome",
                            "asin": keyword,
                            "title": page_title or name,
                            "price": price_str,
                            "in_stock": True,
                            "is_official": True,
                            "seller": "funbox 麗嬰國際 (PChome 官方)",
                            "url": f"https://24h.pchome.com.tw/prod/{pid}",
                            "status_text": "🟢 PChome 突襲上架現貨！"
                        }
        except Exception:
            pass

        return {
            "ok": True,
            "store": "pchome",
            "asin": keyword,
            "title": f"BEYBLADE X {keyword}",
            "price": "-",
            "in_stock": False,
            "is_official": True,
            "seller": "funbox 麗嬰國際 (PChome 官方)",
            "url": f"https://24h.pchome.com.tw/search/?q={keyword}",
            "status_text": "⚪ 尚未上架 (待突襲發布)"
        }

    @classmethod
    def check_prod(cls, prod_id_or_url: str) -> Dict[str, Any]:
        prod_id = cls.extract_prod_id(prod_id_or_url)
        if not prod_id:
            return {"ok": False, "msg": "無效 PChome 商品編號"}

        # 若識別碼為型號關鍵字 (例如 "CX-05", "UX-15", "BX-52") -> 突襲搜尋模式
        if "-" in prod_id and len(prod_id) <= 8 and not prod_id.startswith("DE"):
            return cls.check_pchome_stealth(prod_id)

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

        is_funbox, page_title = cls.is_official_funbox(prod_id, title, session=session)
        seller = "funbox 麗嬰國際 (PChome 官方)" if is_funbox else "PChome 第三方賣家"
        if not in_stock:
            status_text = "⚪ 缺貨中 / 暫無庫存"
        else:
            status_text = f"🟢 現貨有貨 (剩餘 {qty} 件)"

        return {
            "ok": True,
            "store": "pchome",
            "asin": prod_id,
            "title": page_title or title or prod_id,
            "price": price_str,
            "in_stock": in_stock,
            "is_official": is_funbox,
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
    def check_mm_stealth(cls, keyword: str) -> Dict[str, Any]:
        """M.M小舖關鍵字突襲搜尋監控"""
        search_url = f"https://mmtoyshop.com/search?q={keyword}"
        session = get_shared_session()
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        }
        try:
            r = session.get(search_url, headers=headers, timeout=6)
            if r.status_code == 200:
                found_items = re.findall(r'/item/([a-zA-Z0-9_-]+)', r.text)
                clean_kw = keyword.lower().replace("-", "").strip()
                for it_id in set(found_items):
                    res = cls.check_item(it_id)
                    if res.get("ok") and clean_kw in res.get("title", "").lower().replace("-", ""):
                        res["asin"] = keyword
                        return res
        except Exception:
            pass

        return {
            "ok": True,
            "store": "mm_shop",
            "asin": keyword,
            "title": f"BEYBLADE X {keyword}",
            "price": "-",
            "in_stock": False,
            "is_official": True,
            "seller": "M.M小舖",
            "url": f"https://mmtoyshop.com/search?q={keyword}",
            "status_text": "⚪ 尚未上架 (待突襲發布)"
        }

    @classmethod
    def check_item(cls, item_id_or_url: str) -> Dict[str, Any]:
        item_id = cls.extract_item_id(item_id_or_url)
        if not item_id:
            return {"ok": False, "msg": "無效 M.M小舖 商品 ID"}

        # 若識別碼為型號關鍵字 (例如 "CX-05", "UX-15", "BX-52") -> 突襲搜尋模式
        if "-" in item_id and len(item_id) <= 8 and not item_id.startswith("shopee"):
            return cls.check_mm_stealth(item_id)

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
    def check_twj_stealth(cls, keyword: str) -> Dict[str, Any]:
        """童無忌玩具關鍵字突襲搜尋監控"""
        search_url = f"https://www.twj.tw/search?q={keyword}"
        session = get_shared_session()
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        }
        try:
            r = session.get(search_url, headers=headers, timeout=6)
            if r.status_code == 200:
                found_slugs = re.findall(r'/products/([a-zA-Z0-9%_-]+)', r.text)
                clean_kw = keyword.lower().replace("-", "").strip()
                for s in set(found_slugs):
                    s_clean = s.lower().replace("-", "")
                    if clean_kw in s_clean and any(k in s.lower() for k in ["beyblade", "bx", "ux", "cx"]):
                        res = cls.check_prod(s, store_key="twj_toys")
                        if res.get("ok") and res.get("price") != "未標示":
                            res["asin"] = keyword
                            return res
        except Exception:
            pass

        return {
            "ok": True,
            "store": "twj_toys",
            "asin": keyword,
            "title": f"BEYBLADE X {keyword}",
            "price": "-",
            "in_stock": False,
            "is_official": True,
            "seller": "童無忌玩具",
            "url": f"https://www.twj.tw/search?q={keyword}",
            "status_text": "⚪ 尚未上架 (待突襲發布)"
        }

    @classmethod
    def check_funbox_stealth(cls, keyword: str) -> Dict[str, Any]:
        """麗嬰國際官網關鍵字突襲搜尋監控"""
        search_url = f"https://shop.funbox.com.tw/search?q={keyword}"
        session = get_shared_session()
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        }
        try:
            r = session.get(search_url, headers=headers, timeout=6)
            if r.status_code == 200:
                found_slugs = re.findall(r'/products/([a-zA-Z0-9%_-]+)', r.text)
                clean_kw = keyword.lower().replace("-", "").strip()
                for s in set(found_slugs):
                    s_clean = s.lower().replace("-", "")
                    if clean_kw in s_clean and any(k in s.lower() for k in ["beyblade", "bx", "ux", "cx"]):
                        res = cls.check_prod(s, store_key="funbox_tw")
                        if res.get("ok") and res.get("price") != "未標示":
                            res["asin"] = keyword
                            return res
        except Exception:
            pass

        return {
            "ok": True,
            "store": "funbox_tw",
            "asin": keyword,
            "title": f"BEYBLADE X {keyword}",
            "price": "-",
            "in_stock": False,
            "is_official": True,
            "seller": "麗嬰國際官網 (Funbox)",
            "url": f"https://shop.funbox.com.tw/search?q={keyword}",
            "status_text": "⚪ 尚未上架 (待突襲發布)"
        }

    @classmethod
    def check_prod(cls, slug_or_url: str, store_key: str = "funbox_tw") -> Dict[str, Any]:
        raw_text = str(slug_or_url).strip()
        slug = cls.extract_slug(raw_text)
        if not slug:
            return {"ok": False, "msg": "無效商品編號"}

        # 若識別碼為型號關鍵字 (例如 "CX-05", "UX-15", "BX-52") -> 調用突襲搜尋模式
        if not slug.startswith("http") and ("-" in slug and len(slug) <= 8):
            if store_key == "twj_toys" and not slug.startswith("202"):
                return cls.check_twj_stealth(slug)
            elif store_key == "funbox_tw" and not slug.startswith("sm"):
                return cls.check_funbox_stealth(slug)

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
    def check_keyword_stealth(cls, keyword: str) -> Dict[str, Any]:
        """誠品線上關鍵字突襲搜尋監控 (Athena API v2)"""
        search_q = f"BEYBLADE {keyword}"
        api_url = f"https://athena.eslite.com/api/v2/search?q={search_q}&size=15"
        session = get_shared_session()
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            "Origin": "https://www.eslite.com",
            "Referer": "https://www.eslite.com/"
        }
        try:
            r = session.get(api_url, headers=headers, timeout=5)
            if r.status_code == 200:
                data = r.json()
                hits = data.get("hits", {}).get("hit", [])
                clean_kw = keyword.lower().replace("-", "").strip()
                for h in hits:
                    f = h.get("fields", {})
                    name = f.get("name") or ""
                    # 必須同時符合型號關鍵字與戰鬥陀螺標籤
                    name_clean = name.lower().replace("-", "")
                    if clean_kw in name_clean and any(k in name.lower() for k in ["beyblade", "戰鬥陀螺", "陀螺"]):
                        price_val = f.get("final_price") or f.get("retail_price") or 0
                        stock = f.get("stock", 0)
                        sn = f.get("eslite_sn") or keyword
                        in_stock = (stock > 0)
                        price_str = f"NT$ {int(price_val):,}" if price_val else "未標示"
                        return {
                            "ok": True,
                            "store": "eslite",
                            "asin": keyword,
                            "title": name,
                            "price": price_str,
                            "in_stock": in_stock,
                            "is_official": True,
                            "seller": "誠品線上 (Eslite)",
                            "url": f"https://www.eslite.com/product/{sn}",
                            "status_text": f"🟢 誠品突襲上架現貨！(庫存 {stock} 件)" if in_stock else "⚪ 誠品已建檔但缺貨中"
                        }
        except Exception:
            pass

        return {
            "ok": True,
            "store": "eslite",
            "asin": keyword,
            "title": f"BEYBLADE X {keyword}",
            "price": "-",
            "in_stock": False,
            "is_official": True,
            "seller": "誠品線上 (Eslite)",
            "url": f"https://www.eslite.com/search?keyword=BEYBLADE+{keyword}",
            "status_text": "⚪ 尚未上架 (待突襲發布)"
        }

    @classmethod
    def check_prod(cls, id_or_url: str) -> Dict[str, Any]:
        raw_text = str(id_or_url).strip()
        full_id = cls.extract_id(raw_text)
        if not full_id:
            return {"ok": False, "msg": "無效誠品商品代號"}

        # 若識別碼非長條碼數字 (例如 "CX-05", "UX-15", "BX-52" 等關鍵字型號) -> 突襲搜尋模式
        if not full_id.isdigit() or len(full_id) < 10:
            keyword = raw_text.replace("https://", "").replace("http://", "").strip("/")
            return cls.check_keyword_stealth(keyword)

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
        raw_text = str(text_or_url).strip()
        shop_id, item_id = cls.extract_ids(raw_text)
        if not shop_id or not item_id:
            # 若為純型號關鍵字 (例如 "CX-05", "UX-15", "BX-52" 等) -> 突襲待命守候模式
            if not raw_text.startswith("http") and not re.search(r"\d{6,}", raw_text):
                return {
                    "ok": True,
                    "store": "shopee",
                    "asin": raw_text,
                    "title": f"BEYBLADE X {raw_text}",
                    "price": "-",
                    "in_stock": False,
                    "is_official": True,
                    "seller": "funbox 特賣會 (蝦皮官方)",
                    "url": f"https://shopee.tw/search?keyword={raw_text}&shop=285705541",
                    "status_text": "⚪ 尚未上架 (待突襲發布)"
                }
            url = raw_text
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
            "is_official": "funbox" in url.lower() or "285705541" in url,
            "seller": "funbox 特賣會 (蝦皮官方)" if ("funbox" in url.lower() or "285705541" in url) else "蝦皮賣家",
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

    if store in ("amazon_jp", "amazon_stealth"):
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
    if item.get("url"):
        return item["url"]

    store = item.get("store", "amazon_jp")
    asin = item.get("asin", "")
    cfg = STORE_CONFIG.get(store, STORE_CONFIG["amazon_jp"])

    is_model_code = bool(re.search(r"^(?:BX|UX|CX)-\d{2}[A-Z]?$", asin, re.I))

    if store in ("amazon_jp", "amazon_stealth"):
        return get_product_url(asin, official_only=True)
    elif store == "pchome":
        if is_model_code:
            return f"https://24h.pchome.com.tw/search/?q={asin}"
        return f"https://24h.pchome.com.tw/prod/{asin}"
    elif store == "mm_shop":
        if is_model_code:
            return f"https://mmtoyshop.com/search?q={asin}"
        return f"https://mmtoyshop.com/item/{asin}"
    elif store == "funbox_tw":
        if is_model_code:
            return f"https://shop.funbox.com.tw/search?q={asin}"
        return f"https://shop.funbox.com.tw/products/{asin}"
    elif store == "twj_toys":
        if is_model_code:
            return f"https://www.twj.tw/search?q={asin}"
        return f"https://www.twj.tw/products/{asin}"
    elif store == "eslite":
        if is_model_code:
            return f"https://www.eslite.com/search?keyword=BEYBLADE+{asin}"
        return f"https://www.eslite.com/product/{asin}"
    elif store == "shopee":
        if "_" in asin:
            sp, it = asin.split("_", 1)
            return f"https://shopee.tw/product/{sp}/{it}"
        if is_model_code:
            return f"https://shopee.tw/search?keyword={asin}&shop=285705541"
        return asin if asin.startswith("http") else f"https://shopee.tw/funbox5120"
    return asin


# =========================================================================
# 9. 最新上架雷達 (Latest Arrival Radar) & 智慧型號/關鍵字比對
# =========================================================================

def fetch_latest_store_products(store_key: str, proxy: Optional[str] = None) -> List[Dict[str, Any]]:
    """單一賣場最新上架商品抓取器 (單次請求獲取最新 20~50 筆商品，供防突襲雷達比對，省 99% 請求次數)"""
    results: List[Dict[str, Any]] = []
    session = get_shared_session(proxy=proxy)

    if store_key == "pchome":
        url = "https://ecshweb.pchome.com.tw/search/v3.3/all/results?q=戰鬥陀螺&sort=new&page=1"
        try:
            r = session.get(url, timeout=6)
            if r.status_code == 200:
                data = r.json()
                for p in data.get("prods", []):
                    name = p.get("name", "")
                    price = p.get("price", 0)
                    pid = p.get("Id", "")
                    is_funbox = "funbox 麗嬰國際" in name or ("funbox" in name.lower() and "麗嬰" in name)
                    results.append({
                        "store": "pchome",
                        "title": name,
                        "price": f"NT$ {price:,}" if price else "未標示",
                        "url": f"https://24h.pchome.com.tw/prod/{pid}",
                        "item_id": pid,
                        "seller": "funbox 麗嬰國際 (PChome 官方)" if is_funbox else "PChome 第三方賣家",
                        "is_official": is_funbox,
                        "in_stock": True
                    })
        except Exception:
            pass

    elif store_key == "eslite":
        url = "https://athena.eslite.com/api/v2/search?q=BEYBLADE&size=30"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            "Origin": "https://www.eslite.com",
            "Referer": "https://www.eslite.com/"
        }
        try:
            r = session.get(url, headers=headers, timeout=6)
            if r.status_code == 200:
                data = r.json()
                for h in data.get("hits", {}).get("hit", []):
                    f = h.get("fields", {})
                    name = f.get("name") or ""
                    price = f.get("final_price") or f.get("retail_price") or 0
                    sn = f.get("eslite_sn") or ""
                    stock = f.get("stock", 0)
                    results.append({
                        "store": "eslite",
                        "title": name,
                        "price": f"NT$ {int(price):,}" if price else "未標示",
                        "url": f"https://www.eslite.com/product/{sn}",
                        "item_id": sn,
                        "seller": "誠品線上 (Eslite)",
                        "in_stock": stock > 0
                    })
        except Exception:
            pass

    elif store_key in ("twj_toys", "funbox_tw"):
        if store_key == "twj_toys":
            base_search = "https://www.twj.tw/search?q=BEYBLADE&sort_by=created-descending"
            seller_name = "童無忌玩具"
        else:
            base_search = "https://shop.funbox.com.tw/search?q=BEYBLADE&sort_by=created-descending"
            seller_name = "麗嬰國際官網 (Funbox)"

        try:
            r = session.get(base_search, timeout=7)
            if r.status_code == 200:
                found_slugs = re.findall(r'/products/([a-zA-Z0-9%_-]+)', r.text)
                seen_slugs = set()
                for slug in found_slugs[:25]:
                    if slug in seen_slugs:
                        continue
                    seen_slugs.add(slug)
                    res = CyberbizChecker.check_prod(slug, store_key=store_key)
                    if res.get("ok"):
                        results.append({
                            "store": store_key,
                            "title": res.get("title", slug),
                            "price": res.get("price", "未標示"),
                            "url": res.get("url", f"https://www.twj.tw/products/{slug}" if store_key == "twj_toys" else f"https://shop.funbox.com.tw/products/{slug}"),
                            "item_id": slug,
                            "seller": seller_name,
                            "in_stock": res.get("in_stock", True)
                        })
        except Exception:
            pass

    elif store_key == "shopee":
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            "Origin": "https://shopee.tw",
            "Referer": "https://shopee.tw/funbox5120"
        }
        s_client = None
        if cffi_requests:
            try:
                s_client = cffi_requests.Session(impersonate="chrome124", proxies={"http": proxy, "https": proxy} if proxy else None)
            except Exception:
                s_client = None

        if s_client:
            try:
                shopee_url = "https://shopee.tw/api/v4/recommend/recommend?bundle=shop_page_product_tab_main&limit=30&offset=0&section_id=0&shop_id=285705541&sort_type=1"
                r = s_client.get(shopee_url, headers=headers, timeout=8)
                if r.status_code == 200:
                    data = r.json()
                    sections = data.get("data", {}).get("sections", [])
                    items = []
                    for sec in sections:
                        items.extend(sec.get("data", {}).get("item", []))
                    if not items:
                        items = data.get("items", [])

                    for it in items:
                        name = it.get("name") or it.get("title") or ""
                        price_val = it.get("price") or 0
                        if price_val > 100000:
                            price_str = f"NT$ {int(price_val / 100000):,}"
                        elif price_val > 0:
                            price_str = f"NT$ {int(price_val):,}"
                        else:
                            price_str = "未標示"
                        item_id = str(it.get("itemid", ""))
                        shop_id = str(it.get("shopid", "285705541"))
                        prod_url = f"https://shopee.tw/product/{shop_id}/{item_id}" if item_id else "https://shopee.tw/funbox5120"
                        results.append({
                            "store": "shopee",
                            "title": name,
                            "price": price_str,
                            "url": prod_url,
                            "item_id": f"{shop_id}_{item_id}",
                            "seller": "funbox 特賣會 (蝦皮官方)",
                            "is_official": True,
                            "in_stock": True
                        })
            except Exception:
                pass

    elif store_key == "mm_shop":
        url = "https://mmtoyshop.com/search?q=戰鬥陀螺"
        try:
            r = session.get(url, timeout=7)
            if r.status_code == 200:
                found_items = re.findall(r'/item/([a-zA-Z0-9_-]+)', r.text)
                seen_ids = set()
                for item_id in found_items[:15]:
                    if item_id in seen_ids:
                        continue
                    seen_ids.add(item_id)
                    res = MMShopChecker.check_item(item_id)
                    if res.get("ok"):
                        results.append({
                            "store": "mm_shop",
                            "title": res.get("title", item_id),
                            "price": res.get("price", "未標示"),
                            "url": res.get("url", f"https://mmtoyshop.com/item/{item_id}"),
                            "item_id": item_id,
                            "seller": "M.M小舖",
                            "in_stock": res.get("in_stock", True)
                        })
        except Exception:
            pass

    return results


def match_product_with_stealth_catalog(
    product_title: str,
    stealth_catalog: List[Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    """比對商品標題是否命中防突襲清單中的陀螺型號 (BX-00, UX-00, CX-00) 或關鍵字
    傳回匹配的型號資料字典，若未命中則傳回 None
    """
    if not product_title:
        return None

    title_clean = product_title.strip()
    title_upper = title_clean.upper()

    # 1. 優先以正規表示法比對 BX-00 / UX-00 / CX-00 系列型號
    m = re.search(r'\b((?:BX|UX|CX)-\d{2,3}[A-Z]?)\b', title_upper)
    if not m:
        m = re.search(r'\b((?:BX|UX|CX)\d{2,3}[A-Z]?)\b', title_upper)

    model_code = None
    if m:
        raw_code = m.group(1).upper()
        if "-" not in raw_code:
            model_code = f"{raw_code[:2]}-{raw_code[2:]}"
        else:
            model_code = raw_code

    # 2. 從清單中比對對應型號
    if model_code:
        for it in stealth_catalog:
            asin = str(it.get("asin", "")).strip().upper()
            if asin == model_code:
                return {
                    "model": model_code,
                    "catalog_item": it,
                    "matched_by": "model_code"
                }

    # 3. 比對清單中特殊陀螺商品名稱 (如白龍、烈火、德拉克等關鍵字)
    title_low = title_clean.lower()
    for it in stealth_catalog:
        c_name = str(it.get("name", "")).strip()
        asin = str(it.get("asin", "")).strip().upper()
        clean_name = re.sub(r'^(?:BEYBLADE\s*X\s*)?(?:BX|UX|CX)-\d{2,3}[A-Z]?\s*', '', c_name, flags=re.I).strip()
        if clean_name and len(clean_name) >= 3 and clean_name.lower() in title_low:
            return {
                "model": asin,
                "catalog_item": it,
                "matched_by": "keyword"
            }

    return None


# =========================================================================
# 10. 推播管理器 (Discord & LINE)
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
            "content": f"🚨 **【補貨】{item_name}** | {flag} **{store_title}** 現貨開放購買！",
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
