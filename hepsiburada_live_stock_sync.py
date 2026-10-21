import os
import json
import re
import time
import unicodedata
from datetime import datetime

import requests

TRENDYOL_BASE = "https://apigw.trendyol.com"
HB_LISTING_BASE = os.getenv(
    "HB_LISTING_BASE_URL",
    "https://listing-external.hepsiburada.com",
).rstrip("/")

TIMEOUT = 60
PAGE_SIZE = 100
MAX_LISTING_PAGES = 100
MAX_RETRIES = 4


def required(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"GitHub Secret eksik: {name}")
    return value


SUPPLIER_ID = required("TY_SUPPLIER_ID")
TY_API_KEY = required("TY_API_KEY")
TY_API_SECRET = required("TY_API_SECRET")
HB_MERCHANT_ID = required("HB_MERCHANT_ID")
HB_SECRET_KEY = required("HB_SECRET_KEY")
HB_USER_AGENT = os.getenv("HB_USER_AGENT", "dolunaytaki_dev").strip()


def log(message):
    print(f"[{datetime.now().strftime('%d.%m.%Y %H:%M:%S')}] {message}", flush=True)


def safe(value):
    return "" if value is None else str(value).strip()


def norm(value):
    text = safe(value).lower()
    table = str.maketrans({
        "ı": "i", "ş": "s", "ğ": "g", "ü": "u", "ö": "o", "ç": "c",
        "İ": "i", "Ş": "s", "Ğ": "g", "Ü": "u", "Ö": "o", "Ç": "c",
    })
    text = text.translate(table)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def words_key(value):
    """Başlıktaki kelimeleri alfabetik sıraya dizerek esnek eşleşme sağlar."""
    tokens = norm(value).split()
    return " ".join(sorted(set(tokens)))


def sku_key(value):
    return re.sub(r"\s+", "", safe(value)).upper()


def json_or_fail(response, label):
    try:
        return response.json()
    except ValueError as exc:
        raise RuntimeError(f"{label} JSON döndürmedi: {response.text[:3000]}") from exc


def hb_request(method, url, **kwargs):
    kwargs.setdefault("headers", {})
    kwargs["headers"].setdefault("User-Agent", HB_USER_AGENT)
    kwargs["headers"].setdefault("Accept", "application/json")
    kwargs["headers"].setdefault("Content-Type", "application/json")
    kwargs["auth"] = (HB_MERCHANT_ID, HB_SECRET_KEY)
    kwargs.setdefault("timeout", TIMEOUT)

    last_response = None
    for attempt in range(1, MAX_RETRIES + 1):
        response = requests.request(method, url, **kwargs)
        last_response = response

        if response.status_code not in (429, 500, 502, 503, 504):
            if response.status_code in (401, 403):
                body = response.text[:3000].replace("\n", " ")
                raise RuntimeError(f"HB API yetkilendirme hatası: HTTP {response.status_code} | Cevap={body}")
            return response

        wait = min(2 ** attempt, 30)
        time.sleep(wait)

    return last_response


def get_trendyol_products():
    url = f"{TRENDYOL_BASE}/integration/product/sellers/{SUPPLIER_ID}/products/approved"
    products = []
    page = 0

    while True:
        response = requests.get(
            url,
            headers={"User-Agent": f"{SUPPLIER_ID} - SelfIntegration", "Accept": "application/json"},
            auth=(TY_API_KEY, TY_API_SECRET),
            params={"page": page, "size": 100},
            timeout=TIMEOUT,
        )

        if response.status_code != 200:
            raise RuntimeError(f"Trendyol HTTP {response.status_code}: {response.text[:3000]}")

        data = json_or_fail(response, "Trendyol")
        content = data.get("content") or []
        if not content:
            break

        for product in content:
            variants = product.get("variants") or [{}]
            for variant in variants:
                barcode = safe(variant.get("barcode") or product.get("barcode"))
                if not barcode:
                    continue

                stock = variant.get("stock")
                if isinstance(stock, dict):
                    stock = stock.get("quantity")
                if stock is None:
                    stock = variant.get("quantity") or product.get("quantity", 0)

                try:
                    stock = max(0, int(float(stock)))
                except (TypeError, ValueError):
                    stock = 0

                price = variant.get("salePrice") or variant.get("price") or product.get("salePrice") or product.get("price")
                try:
                    price = float(price) if price not in (None, "") else None
                except (TypeError, ValueError):
                    price = None

                products.append({
                    "stock": stock,
                    "stockCode": safe(variant.get("stockCode") or product.get("stockCode")),
                    "productCode": safe(variant.get("productCode") or product.get("productCode")),
                    "barcode": barcode,
                    "title": safe(product.get("title")),
                    "price": price,
                })

        if len(content) < 100:
            break
        page += 1

    log(f"✅ Trendyol toplam ürün/varyant: {len(products)}")
    return products


def extract_listing_fields(obj):
    fields = {"merchantSku": "", "hbSku": "", "barcode": "", "name": "", "price": None, "availableStock": None}

    def walk(value):
        if isinstance(value, dict):
            for raw_key, raw_value in value.items():
                key = safe(raw_key).replace("-", "_").lower()
                if isinstance(raw_value, (str, int, float)) and raw_value not in ("", None):
                    if not fields["merchantSku"] and key in {"merchantsku", "merchant_sku", "seller_sku"}:
                        fields["merchantSku"] = safe(raw_value)
                    if not fields["hbSku"] and key in {"hbsku", "hepsiburadasku"}:
                        fields["hbSku"] = safe(raw_value)
                    if not fields["barcode"] and key in {"barcode", "ean", "gtin"}:
                        fields["barcode"] = safe(raw_value)
                    if not fields["name"] and key in {"productname", "product_name", "name", "title"}:
                        fields["name"] = safe(raw_value)
                    if fields["price"] is None and key in {"price", "saleprice", "listprice"}:
                        fields["price"] = raw_value
                    if fields["availableStock"] is None and key in {"availablestock", "stock"}:
                        fields["availableStock"] = raw_value
                walk(raw_value)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(obj)
    return fields


def get_all_hb_listings():
    url = f"{HB_LISTING_BASE}/listings/merchantid/{HB_MERCHANT_ID}"
    listings = []
    offset = 0

    for _ in range(MAX_LISTING_PAGES):
        response = hb_request("GET", url, params={"offset": offset, "limit": PAGE_SIZE})
        if response.status_code != 200:
            raise RuntimeError(f"HB listing HTTP {response.status_code}: {response.text[:3000]}")

        data = json_or_fail(response, "HB listing")
        items = data.get("listings") or data.get("items") or data.get("content") or []
        if isinstance(items, dict):
            items = items.get("listings") or items.get("items") or []

        if not items:
            break

        listings.extend([x for x in items if isinstance(x, dict)])
        if len(items) < PAGE_SIZE:
            break
        offset += len(items)

    log(f"✅ Hepsiburada toplam ilan: {len(listings)}")
    return listings


def build_indexes(listings):
    by_merchant_sku = {}
    by_hb_sku = {}
    by_barcode = {}
    by_name = {}
    by_title_words = {}

    for item in listings:
        fields = extract_listing_fields(item)
        row = {"raw": item, "fields": fields}

        m_sku = sku_key(fields["merchantSku"])
        h_sku = sku_key(fields["hbSku"])
        b_code = sku_key(fields["barcode"])
        name_exact = norm(fields["name"])
        name_words = words_key(fields["name"])

        if m_sku:
            by_merchant_sku[m_sku] = row
        if h_sku:
            by_hb_sku[h_sku] = row
        if b_code:
            by_barcode[b_code] = row
        if name_exact:
            by_name[name_exact] = row
        if name_words:
            by_title_words[name_words] = row

    return by_merchant_sku, by_hb_sku, by_barcode, by_name, by_title_words


def find_match(product, indexes):
    by_merchant_sku, by_hb_sku, by_barcode, by_name, by_title_words = indexes

    # 1. Kod/Barkod Eşleşmesi
    for candidate in (product["stockCode"], product["productCode"], product["barcode"]):
        key = sku_key(candidate)
        if not key:
            continue
        if key in by_merchant_sku:
            return by_merchant_sku[key], "merchantSku"
        if key in by_hb_sku:
            return by_hb_sku[key], "hbSku"
        if key in by_barcode:
            return by_barcode[key], "barcode"

    # 2. Birebir İsim Eşleşmesi
    exact_name = norm(product["title"])
    if exact_name and exact_name in by_name:
        return by_name[exact_name], "birebir_isim"

    # 3. Esnek Kelime Kümesi İsim Eşleşmesi
    tw_key = words_key(product["title"])
    if tw_key and tw_key in by_title_words:
        return by_title_words[tw_key], "esnek_isim"

    return None, ""


def upload_stock(updates):
    if not updates:
        log("✅ Güncellenecek stok bulunamadı.")
        return

    url = f"{HB_LISTING_BASE}/listings/merchantid/{HB_MERCHANT_ID}/inventory-uploads"
    payload = [
        {
            "hepsiburadaSku": item.get("hepsiburadaSku", ""),
            "merchantSku": item.get("merchantSku", ""),
            "price": item.get("price"),
            "availableStock": item.get("availableStock", 0),
        }
        for item in updates
    ]

    response = hb_request("POST", url, json=payload)
    log(f"📡 HB stok yükleme gönderildi | HTTP {response.status_code} | {len(payload)} Ürün")

    if response.status_code not in (200, 201, 202):
        raise RuntimeError(f"HB stok yükleme başarısız: HTTP {response.status_code}")

    data = json_or_fail(response, "HB stok yükleme")
    upload_id = safe((data.get("data") or {}).get("id") or data.get("id"))

    if upload_id:
        status_url = f"{HB_LISTING_BASE}/listings/merchantid/{HB_MERCHANT_ID}/inventory-uploads/id/{upload_id}"
        for _ in range(20):
            time.sleep(3)
            res = hb_request("GET", status_url)
            if res.status_code == 200:
                st_data = json_or_fail(res, "HB sorgu")
                status = safe((st_data.get("data") or st_data).get("status")).upper()
                if status in {"SUCCESS", "SUCCEEDED", "COMPLETED", "DONE"}:
                    log("✅ Hepsiburada stok güncellemesi başarıyla tamamlandı.")
                    return


def main():
    log("=" * 60)
    log("GÜÇLENDİRİLMİŞ STOK SENKRONİZASYONU BAŞLADI")
    log("=" * 60)

    products = get_trendyol_products()
    listings = get_all_hb_listings()
    indexes = build_indexes(listings)

    updates = []
    matched = 0
    seen_hb_skus = set()

    for product in products:
        row, method = find_match(product, indexes)
        if row is None:
            continue

        fields = row["fields"]
        hb_sku = safe(fields["hbSku"])
        if not hb_sku:
            continue

        normalized_hb = sku_key(hb_sku)
        if normalized_hb in seen_hb_skus:
            continue

        seen_hb_skus.add(normalized_hb)
        matched += 1

        target_stock = product["stock"]
        merchant_sku = safe(fields.get("merchantSku"))
        price = product.get("price") or fields.get("price")

        if not merchant_sku or price in (None, ""):
            continue

        updates.append({
            "hepsiburadaSku": hb_sku,
            "merchantSku": merchant_sku,
            "price": price,
            "availableStock": target_stock,
        })
        log(f"🔗 EŞLEŞTİ ({method}) | {product['title'][:35]}... -> Stok: {target_stock}")

    # Trendyol'da kesinlikle bulunmayan eski ilanları sıfırlama
    zeroed_out = 0
    for item in listings:
        fields = extract_listing_fields(item)
        hb_sku = safe(fields.get("hbSku"))
        if not hb_sku:
            continue

        normalized_hb = sku_key(hb_sku)
        if normalized_hb not in seen_hb_skus:
            try:
                curr_stock = int(float(fields.get("availableStock") or 0))
            except (TypeError, ValueError):
                curr_stock = 0

            if curr_stock > 0:
                m_sku = safe(fields.get("merchantSku"))
                price = fields.get("price")
                if m_sku and price not in (None, ""):
                    updates.append({
                        "hepsiburadaSku": hb_sku,
                        "merchantSku": m_sku,
                        "price": price,
                        "availableStock": 0,
                    })
                    zeroed_out += 1
                    seen_hb_skus.add(normalized_hb)
                    log(f"📉 SIFIRLANDI (Trendyol'da Yok) | HB SKU: {hb_sku}")

    log(f"📊 ÖZET | Trendyol={len(products)} | HB Toplam={len(listings)} | Eşleşen={matched} | Sıfırlanan={zeroed_out}")
    upload_stock(updates)


if __name__ == "__main__":
    main()
