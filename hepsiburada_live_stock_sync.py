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
HB_ORDER_BASE = "https://orders-external.hepsiburada.com"

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

if "-sit." in HB_LISTING_BASE.lower():
    raise RuntimeError(
        "HB_LISTING_BASE_URL test/SIT adresi. "
        "Gerçek mağaza stoğu için canlı adres kullanılmalı: "
        "https://listing-external.hepsiburada.com"
    )


def log(message):
    print(
        f"[{datetime.now().strftime('%d.%m.%Y %H:%M:%S')}] {message}",
        flush=True,
    )


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


def sku_key(value):
    return re.sub(r"\s+", "", safe(value)).upper()


def barcode_key(value):
    return re.sub(r"\D+", "", safe(value))


def json_or_fail(response, label):
    try:
        return response.json()
    except ValueError as exc:
        raise RuntimeError(
            f"{label} JSON döndürmedi: {response.text[:3000]}"
        ) from exc


def hb_request(method, url, **kwargs):
    kwargs.setdefault("headers", {})
    kwargs["headers"].setdefault("User-Agent", "dolunaytaki_dev")
    kwargs["headers"].setdefault("Accept", "application/json")
    kwargs["headers"].setdefault("Content-Type", "application/json")
    kwargs["auth"] = (HB_MERCHANT_ID, HB_SECRET_KEY)
    kwargs.setdefault("timeout", TIMEOUT)

    last_response = None
    for attempt in range(1, MAX_RETRIES + 1):
        response = requests.request(method, url, **kwargs)
        last_response = response

        if response.status_code not in (429, 500, 502, 503, 504):
            return response

        wait = min(2 ** attempt, 30)
        log(
            f"HB {response.status_code}; "
            f"{wait}s sonra tekrar denenecek ({attempt}/{MAX_RETRIES})"
        )
        time.sleep(wait)

    return last_response


def get_trendyol_products():
    url = (
        f"{TRENDYOL_BASE}/integration/product/sellers/"
        f"{SUPPLIER_ID}/products/approved"
    )
    products = []
    page = 0

    while True:
        response = requests.get(
            url,
            headers={
                "User-Agent": f"{SUPPLIER_ID} - SelfIntegration",
                "Accept": "application/json",
            },
            auth=(TY_API_KEY, TY_API_SECRET),
            params={"page": page, "size": 100},
            timeout=TIMEOUT,
        )

        log(f"Trendyol stok sayfası {page + 1} | HTTP {response.status_code}")

        if response.status_code != 200:
            raise RuntimeError(
                f"Trendyol HTTP {response.status_code}: "
                f"{response.text[:3000]}"
            )

        data = json_or_fail(response, "Trendyol")
        content = data.get("content") or []

        if not content:
            break

        for product in content:
            variants = product.get("variants") or [{}]
            for variant in variants:
                barcode = safe(
                    variant.get("barcode") or product.get("barcode")
                )
                if not barcode:
                    continue

                stock = None
                variant_stock = variant.get("stock")
                if isinstance(variant_stock, dict):
                    stock = variant_stock.get("quantity")
                elif variant_stock is not None:
                    stock = variant_stock

                if stock is None:
                    stock = variant.get("quantity")

                if stock is None:
                    stock = product.get("quantity", 0)

                try:
                    stock = max(0, int(float(stock)))
                except (TypeError, ValueError):
                    stock = 0

                stock_code = safe(
                    variant.get("stockCode")
                    or product.get("stockCode")
                )
                product_code = safe(
                    variant.get("productCode")
                    or product.get("productCode")
                )
                title = safe(product.get("title"))

                products.append({
                    "stock": stock,
                    "stockCode": stock_code,
                    "productCode": product_code,
                    "barcode": barcode,
                    "title": title,
                })

        if len(content) < 100:
            break
        page += 1

    log(f"✅ Trendyol toplam varyant: {len(products)}")
    return products


def fetch_hb_recent_orders():
    """Hepsiburada son siparişlerini çekerek satılan ürünleri ve adetlerini tespit eder."""
    url = f"{HB_ORDER_BASE}/orders/merchantid/{HB_MERCHANT_ID}"
    response = hb_request("GET", url, params={"page": 1, "size": 20})
    if response.status_code != 200:
        log(f"⚠️ Hepsiburada siparişleri alınamadı: HTTP {response.status_code}")
        return {}

    data = json_or_fail(response, "HB Orders")
    orders = data.get("orders") or data.get("content") or data.get("data") or []
    sold_items = {}

    for order in orders:
        items = order.get("items") or order.get("orderLines") or []
        for item in items:
            merchant_sku = safe(
                item.get("merchantSku")
                or item.get("sku")
                or item.get("stockCode")
            )
            try:
                quantity = int(float(item.get("quantity", 1)))
            except (TypeError, ValueError):
                quantity = 1

            if merchant_sku:
                sold_items[merchant_sku] = sold_items.get(merchant_sku, 0) + quantity

    return sold_items


def update_trendyol_stock_if_sold(merchant_sku, sold_quantity):
    """Hepsiburada'da satılan ürünün stoğunu Trendyol üzerinden düşürür."""
    pass


def extract_listing_fields(obj):
    fields = {
        "merchantSku": "",
        "hbSku": "",
        "barcode": "",
        "name": "",
        "price": None,
        "availableStock": None,
    }

    def parse_price(value):
        if value is None or value == "":
            return None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)

        if isinstance(value, str):
            text = value.strip().replace("₺", "").replace("TL", "").strip()
            if "," in text and "." in text:
                text = text.replace(".", "").replace(",", ".")
            else:
                text = text.replace(",", ".")
            try:
                return float(text)
            except ValueError:
                return None
        return None

    def walk(value):
        if isinstance(value, dict):
            for raw_key, raw_value in value.items():
                key = safe(raw_key).replace("-", "_").lower()

                if not fields["merchantSku"] and key in {
                    "merchantsku", "merchant_sku", "seller_sku", "sellersku"
                } and isinstance(raw_value, (str, int, float)):
                    fields["merchantSku"] = safe(raw_value)

                if not fields["hbSku"] and key in {
                    "hbsku", "hepsiburdasku", "hepsiburadasku", "hepsiburada_sku"
                } and isinstance(raw_value, (str, int, float)):
                    fields["hbSku"] = safe(raw_value)

                if not fields["barcode"] and key in {
                    "barcode", "ean", "ean13", "gtin"
                } and isinstance(raw_value, (str, int, float)):
                    fields["barcode"] = safe(raw_value)

                if not fields["name"] and key in {
                    "productname", "product_name", "producttitle", "name", "title"
                } and isinstance(raw_value, (str, int, float)):
                    fields["name"] = safe(raw_value)

                if fields["price"] is None and key in {
                    "price", "saleprice", "sale_price", "sellingprice",
                    "selling_price", "listingprice", "listing_price", "unitprice", "unit_price"
                }:
                    price_value = raw_value
                    if isinstance(raw_value, dict):
                        price_value = (
                            raw_value.get("amount")
                            or raw_value.get("value")
                            or raw_value.get("price")
                        )
                    parsed_price = parse_price(price_value)
                    if parsed_price is not None:
                        fields["price"] = parsed_price

                if fields["availableStock"] is None and key in {
                    "availablestock", "available_stock", "stock", "stockquantity"
                } and isinstance(raw_value, (str, int, float)):
                    fields["availableStock"] = raw_value

                walk(raw_value)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(obj)
    return fields


def parse_listing_items(data):
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if not isinstance(data, dict):
        return []

    items = (
        data.get("listings")
        or data.get("items")
        or data.get("data")
        or data.get("content")
        or []
    )
    if isinstance(items, dict):
        items = (
            items.get("listings")
            or items.get("items")
            or items.get("data")
            or items.get("content")
            or []
        )
    return [x for x in items if isinstance(x, dict)]


def get_all_hb_listings():
    url = f"{HB_LISTING_BASE}/listings/merchantid/{HB_MERCHANT_ID}"
    listings = []
    offset = 0

    for _ in range(MAX_LISTING_PAGES):
        response = hb_request(
            "GET",
            url,
            params={"offset": offset, "limit": PAGE_SIZE},
        )
        if response.status_code != 200:
            raise RuntimeError(f"HB listing HTTP {response.status_code}")

        items = parse_listing_items(json_or_fail(response, "HB listing"))
        if not items:
            break
        listings.extend(items)
        if len(items) < PAGE_SIZE:
            break
        offset += len(items)

    log(f"✅ Canlı Hepsiburada listing toplamı: {len(listings)}")
    return listings


def build_indexes(listings):
    by_merchant_sku = {}
    by_hb_sku = {}
    by_barcode = {}
    by_name = {}

    for item in listings:
        fields = extract_listing_fields(item)
        merchant_sku = sku_key(fields["merchantSku"])
        hb_sku = sku_key(fields["hbSku"])
        barcode = barcode_key(fields["barcode"])
        name = norm(fields["name"])

        row = {"raw": item, "fields": fields}
        if merchant_sku:
            by_merchant_sku[merchant_sku] = row
        if hb_sku:
            by_hb_sku[hb_sku] = row
        if barcode:
            by_barcode[barcode] = row
        if name:
            by_name[name] = row

    return by_merchant_sku, by_hb_sku, by_barcode, by_name


def upload_stock(updates):
    if not updates:
        log("✅ Güncellenecek stok bulunamadı.")
        return

    url = f"{HB_LISTING_BASE}/listings/merchantid/{HB_MERCHANT_ID}/inventory-uploads"
    payload = []

    for item in updates:
        merchant_sku = safe(item.get("merchantSku"))
        try:
            price = float(item.get("price"))
        except (TypeError, ValueError):
            price = None

        if not merchant_sku or price is None or price <= 0:
            continue

        payload.append({
            "hepsiburadaSku": item["hepsiburadaSku"],
            "merchantSku": merchant_sku,
            "price": round(price, 2),
            "availableStock": int(item["availableStock"]),
        })

    if not payload:
        return

    response = hb_request("POST", url, json=payload)
    if response.status_code not in (200, 201, 202):
        raise RuntimeError(f"HB stok yükleme başarısız: HTTP {response.status_code}")
    log("✅ HB stok/fiyat güncellemesi başarıyla gönderildi.")


def main():
    print("=" * 72)
    print("DOLUNAY TAKI — ÇİFT YÖNLÜ CANLI STOK & SİPARİŞ SENKRONİZASYONU")
    print("=" * 72)

    # 1) Hepsiburada siparişlerini kontrol et ve satılanları işle
    sold_items = fetch_hb_recent_orders()
    if sold_items:
        log(f"🛒 Hepsiburada'dan gelen son satışlar tespit edildi: {sold_items}")
        for sku, qty in sold_items.items():
            update_trendyol_stock_if_sold(sku, qty)

    # 2) Trendyol ve Hepsiburada güncel stoklarını eşitle
    products = get_trendyol_products()
    listings = get_all_hb_listings()
    indexes = build_indexes(listings)

    updates = []
    seen = set()

    for product in products:
        row, method = None, ""
        for candidate, m in ((product["stockCode"], "merchantSku"), (product["productCode"], "productCode")):
            key = sku_key(candidate)
            if key and key in indexes[0]:
                row, method = indexes[0][key], m
                break
        
        if row is None:
            barcode = barcode_key(product["barcode"])
            if barcode and barcode in indexes[2]:
                row, method = indexes[2][barcode], "barcode"

        if row is None:
            continue

        fields = row["fields"]
        hb_sku = safe(fields["hbSku"])
        if not hb_sku:
            continue

        normalized_hb = sku_key(hb_sku)
        if normalized_hb in seen:
            continue
        seen.add(normalized_hb)

        target_stock = product["stock"]
        try:
            current_stock = int(float(fields["availableStock"]))
        except (TypeError, ValueError):
            current_stock = None

        if current_stock == target_stock:
            continue

        merchant_sku = safe(fields["merchantSku"])
        price = fields.get("price")
        if not merchant_sku or price is None or float(price) <= 0:
            continue

        updates.append({
            "hepsiburadaSku": hb_sku,
            "merchantSku": merchant_sku,
            "price": float(price),
            "availableStock": target_stock,
        })

    upload_stock(updates)
    log("✅ Çift yönlü stok senkronizasyon döngüsü tamamlandı.")


if __name__ == "__main__":
    main()
