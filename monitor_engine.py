"""
Amazon Japan 戰鬥陀螺 (Beyblade X) 雲端監控引擎核心
專為 Render.com / Linux 雲端環境優化，支援極速並發、微錯開防封與精準 BuyBox 判定。
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from typing import Dict, Any, Tuple, Optional

import requests
from bs4 import BeautifulSoup

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


def get_product_url(asin: str) -> str:
    """生成鎖定官方自營的 1-Click 極速購買商品頁網址"""
    return f"https://www.amazon.co.jp/dp/{asin}?m=AN1VRQENFRJN5&th=1&psc=1"


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


class AmazonJPChecker:
    _session = None

    @classmethod
    def get_session(cls):
        if cls._session is None:
            s = requests.Session()
            adapter = requests.adapters.HTTPAdapter(
                pool_connections=25,
                pool_maxsize=25,
                max_retries=1
            )
            s.mount("https://", adapter)
            cls._session = s
        return cls._session

    @classmethod
    def check_asin(cls, asin: str) -> Dict[str, Any]:
        """極速檢測 Amazon.co.jp 特定 ASIN 庫存與官方自營狀態 (雲端連線池 + 高速正則解析，0 耗 CPU)"""
        asin = extract_asin(asin)
        if not asin:
            return {"ok": False, "msg": "無效 ASIN"}

        url = get_product_url(asin)
        html = None
        status_code = 200

        session = cls.get_session()
        headers = {
            "Accept-Language": "ja-JP,ja;q=0.9",
            "Cookie": "i18n-prefs=JPY; lc-acbjp=ja_JP",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }

        try:
            r = session.get(url, headers=headers, timeout=4)
            status_code = r.status_code
            html = r.text
        except Exception as e:
            return {"ok": False, "msg": f"網路超時: {str(e)[:25]}"}

        if not html:
            return {"ok": False, "msg": "無法獲取頁面內容"}

        # 驗證碼與錯誤頁判定
        if "/errors_page/validateCaptcha" in html or "api-services-support@amazon.com" in html:
            return {"ok": False, "msg": "Amazon 頻率限制 (CAPTCHA 驗證)"}

        if status_code == 404 or "申し訳ございません。お探しのページが見つかりませんでした" in html:
            return {"ok": False, "msg": "頁面不存在 (404 未上架)"}
        elif status_code == 503:
            return {"ok": False, "msg": "Amazon 頻率限制 (503)"}
        elif status_code != 200 and not ("<html" in html.lower()):
            return {"ok": False, "msg": f"HTTP {status_code}"}

        # 高速正則抽取標題 (免解析整個 1MB DOM 樹，大幅降低 CPU 佔用)
        title_m = re.search(r'id="productTitle"[^>]*>(.*?)</span>', html, re.DOTALL)
        title = " ".join(title_m.group(1).split()) if title_m else ""

        # 購買/預購按鈕判斷 (極速子字串檢索)
        has_cart = ('id="add-to-cart-button"' in html) or ('name="submit.add-to-cart"' in html)
        has_buy_now = ('id="buy-now-button"' in html) or ('name="submit.buy-now"' in html)
        has_preorder = ('preorder' in html.lower()) or ('予約注文' in html)

        no_featured_offer = (
            "おすすめ出品はありません" in html or
            "その他の出品者" in html or
            "没有精选优惠" in html
        ) and not (has_cart or has_buy_now or has_preorder)

        has_third_party_profile = 'id="sellerProfileTriggerId"' in html

        # 賣家資訊高速抽取
        merchant_m = re.search(r'id="(?:merchantInfo|merchant-info)"[^>]*>(.*?)</div>', html, re.DOTALL)
        merchant_text = " ".join(re.sub(r'<[^>]+>', ' ', merchant_m.group(1)).split()) if merchant_m else ""

        fulfiller_m = re.search(r'id="(?:fulfillerInfo|fulfiller-info)"[^>]*>(.*?)</div>', html, re.DOTALL)
        fulfiller_text = " ".join(re.sub(r'<[^>]+>', ' ', fulfiller_m.group(1)).split()) if fulfiller_m else ""

        is_amazon_sold = (
            ("amazon.co.jp" in merchant_text.lower() or "アマゾン" in merchant_text) and
            not has_third_party_profile
        )
        is_amazon_fulfilled = "amazon" in fulfiller_text.lower()

        # 價格高速正則抽取
        price = ""
        m_price = re.search(r'class="a-price\s*[^"]*".*?<span class="a-offscreen">\s*([^\s<]+)\s*</span>', html, re.DOTALL)
        if m_price:
            price = m_price.group(1).strip()
        else:
            m_price2 = re.search(r'id="(?:priceblock_ourprice|priceblock_dealprice|price_inside_buybox)"[^>]*>\s*([^\s<]+)\s*<', html, re.DOTALL)
            if m_price2:
                price = m_price2.group(1).strip()
            else:
                m_price3 = re.search(r'￥\s*([\d,]+)', html[:200000])
                if m_price3:
                    price = f"￥{m_price3.group(1)}"

        # 官方現貨/庫存判斷
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
            "asin": asin,
            "title": title,
            "price": price,
            "in_stock": in_stock,
            "is_official": is_official,
            "is_preorder": has_preorder,
            "no_featured_offer": no_featured_offer,
            "seller": seller_name,
            "url": url,
            "raw_merchant": merchant_text
        }


class NotificationManager:
    @staticmethod
    def send_discord(webhook_url: str, item_name: str, asin: str, price: str, seller: str, url: str) -> Tuple[bool, str]:
        """發送 Discord Webhook 秒殺推播"""
        if not webhook_url or not webhook_url.startswith("http"):
            return False, "未設定 Discord Webhook"

        product_url = get_product_url(asin)
        payload = {
            "content": "🚨 **【戰鬥陀螺官方補貨通知】** Amazon Japan 官方自營開放購買/預購！",
            "embeds": [
                {
                    "title": f"⚡ {item_name}",
                    "description": "檢測到 Amazon.co.jp 官方自營現貨！請立即點擊下方連結搶購。",
                    "url": product_url,
                    "color": 3066993,
                    "fields": [
                        {"name": "型號/名稱", "value": item_name, "inline": True},
                        {"name": "ASIN", "value": asin, "inline": True},
                        {"name": "官方價格", "value": price, "inline": True},
                        {"name": "販售者", "value": "Amazon.co.jp (官方自營)", "inline": True},
                        {
                            "name": "⚡ 官方 1-Click 極速秒殺 (點擊直達)",
                            "value": (
                                f"🔥 **[【👉 點此直達官方商品頁 (點橘色「今すぐ買う」1-Click 秒殺)】]({product_url})**\n"
                                f"*(💡秘訣：點開直接按橘色今すぐ買う按鈕下單)*"
                            ),
                            "inline": False
                        }
                    ],
                    "footer": {"text": "Amazon Japan 戰鬥陀螺雲端監控器 • 官方直販秒殺系統"},
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
        """若輸入為 Channel ID:Secret 則自動向 LINE 申請 Access Token"""
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
        """透過 LINE Messaging API Broadcast 推播給所有加好友的使用者"""
        if not token_or_str:
            return False, "未設定 LINE Token"

        token = cls.get_line_access_token(token_or_str)

        # 優先嘗試 LINE Messaging API Broadcast
        try:
            headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
            payload = {"messages": [{"type": "text", "text": message}]}
            r = requests.post("https://api.line.me/v2/bot/message/broadcast", headers=headers, json=payload, timeout=8)
            if r.status_code == 200:
                return True, "LINE 官方帳號推播成功！"
        except Exception:
            pass

        # 回退至舊版 LINE Notify
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
        """回覆 LINE 聊天室指令"""
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
