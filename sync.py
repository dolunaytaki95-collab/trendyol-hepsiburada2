import os
import json
import re
import time
import unicodedata
from datetime import datetime

import requests

# This job is intentionally STOCK-ONLY.
# It does not import/create products. It reads the live HB listings and
# updates their availableStock from current Trendyol quantities.

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
if not HB_USER_AGENT:
    raise RuntimeError("GitHub Secret/Variable eksik: HB_USER_AGENT")


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


def json_or_fail(response, label):
    try:
        return response.json()
    except ValueError as exc:
        raise RuntimeError(
            f"{label} JSON döndürmedi: {response.text[:3000]}"
        ) from exc


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
                raise RuntimeError(
                    f"HB API yetkilendirme hatası: HTTP {response.status_code} | "
                    f"URL={url} | User-Agent={HB_USER_AGENT} | "
                    f"Cevap={body}"
                )
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


def extract_listing_fields(obj):
    fields = {
        "merchantSku": "",
        "hbSku": "",
        "barcode": "",
        "name": "",
        "availableStock": None,
    }

    def walk(value):
        if isinstance(value, dict):
            for raw_key, raw_value in value.items():
                key = safe(raw_key).replace("-", "_").lower()

                if isinstance(raw_value, (str, int, float)) and raw_value not in ("", None):
                    if not fields["merchantSku"] and key in {
                        "merchantsku", "merchant_sku", "seller_sku", "sellersku"
                    }:
                        fields["merchantSku"] = safe(raw_value)

                    if not fields["hbSku"] and key in {
                        "hbsku", "hepsiburdasku", "hepsiburada_sku"
                    }:
                        fields["hbSku"] = safe(raw_value)

                    if not fields["barcode"] and key in {
                        "barcode", "ean", "ean13", "gtin"
                    }:
                        fields["barcode"] = safe(raw_value)

                    if not fields["name"] and key in {
                        "productname", "product_name", "producttitle",
                        "name", "title"
                    }:
                        fields["name"] = safe(raw_value)

                    if fields["availableStock"] is None and key in {
                        "availablestock", "available_stock",
                        "stock", "stockquantity"
                    }:
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
            params={
                "offset": offset,
                "limit": PAGE_SIZE,
            },
        )

        log(
            f"Hepsiburada listing offset={offset} "
            f"limit={PAGE_SIZE} | HTTP {response.status_code}"
        )

        if response.status_code != 200:
            raise RuntimeError(
                f"HB listing HTTP {response.status_code}: "
                f"{response.text[:3000]}"
            )

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
        barcode = sku_key(fields["barcode"])
        name = norm(fields["name"])

        row = {
            "raw": item,
            "fields": fields,
        }

        if merchant_sku:
            by_merchant_sku[merchant_sku] = row
        if hb_sku:
            by_hb_sku[hb_sku] = row
        if barcode:
            by_barcode[barcode] = row
        if name:
            by_name[name] = row

    return by_merchant_sku, by_hb_sku, by_barcode, by_name


def find_match(product, indexes):
    by_merchant_sku, by_hb_sku, by_barcode, by_name = indexes

    for candidate in (
        product["stockCode"],
        product["productCode"],
        product["barcode"],
    ):
        key = sku_key(candidate)
        if not key:
            continue

        if key in by_merchant_sku:
            return by_merchant_sku[key], "merchantSku"

        if key in by_hb_sku:
            return by_hb_sku[key], "hbSku"

        if key in by_barcode:
            return by_barcode[key], "barcode"

    exact_name = norm(product["title"])
    if exact_name and exact_name in by_name:
        return by_name[exact_name], "name"

    return None, ""


def upload_stock(updates):
    if not updates:
        log("✅ Güncellenecek stok bulunamadı.")
        return

    url = (
        f"{HB_LISTING_BASE}/listings/merchantid/"
        f"{HB_MERCHANT_ID}/inventory-uploads"
    )

    payload = [
        {
            "hepsiburadaSku": item["hepsiburadaSku"],
            "availableStock": item["availableStock"],
        }
        for item in updates
    ]

    response = hb_request(
        "POST",
        url,
        json=payload,
    )

    log(
        f"📡 HB stok yükleme | HTTP {response.status_code} | "
        f"{len(payload)} SKU"
    )
    print(response.text[:10000], flush=True)

    if response.status_code not in (200, 201, 202):
        raise RuntimeError(
            f"HB stok yükleme başarısız: HTTP {response.status_code}"
        )

    data = json_or_fail(response, "HB stok yükleme")

    upload_id = ""
    if isinstance(data, dict):
        nested = data.get("data")
        if isinstance(nested, dict):
            upload_id = safe(
                nested.get("id")
                or nested.get("inventoryUploadId")
                or nested.get("uploadId")
            )

        if not upload_id:
            upload_id = safe(
                data.get("id")
                or data.get("inventoryUploadId")
                or data.get("uploadId")
            )

    if not upload_id:
        raise RuntimeError(
            "HB stok yüklemesi kabul edildi ancak inventoryUploadId dönmedi."
        )

    log(f"🆔 HB stok işlem ID: {upload_id}")

    status_url = (
        f"{HB_LISTING_BASE}/listings/merchantid/"
        f"{HB_MERCHANT_ID}/inventory-uploads/id/{upload_id}"
    )

    for attempt in range(1, 21):
        time.sleep(3)

        response = hb_request("GET", status_url)

        log(
            f"🔎 HB stok işlem kontrolü {attempt}/20 | "
            f"HTTP {response.status_code}"
        )

        if response.status_code != 200:
            raise RuntimeError(
                f"HB stok işlem sorgusu başarısız: "
                f"HTTP {response.status_code}"
            )

        data = json_or_fail(response, "HB stok işlem sorgusu")
        payload = (
            data.get("data")
            if isinstance(data, dict) and isinstance(data.get("data"), dict)
            else data
        )

        status = ""
        if isinstance(payload, dict):
            status = safe(
                payload.get("status") or payload.get("state")
            ).upper()

        log(f"📌 HB stok işlem durumu: {status or 'BİLİNMİYOR'}")

        if status in {
            "SUCCESS", "SUCCEEDED", "COMPLETED", "DONE", "FINISHED"
        }:
            log("✅ HB stok güncellemesi TAMAMLANDI.")
            return

        if status in {"FAILED", "FAIL", "ERROR"}:
            raise RuntimeError(
                "HB stok işlemi başarısız: "
                + json.dumps(data, ensure_ascii=False)[:10000]
            )

    raise RuntimeError("HB stok işlemi 60 saniyede tamamlanmadı.")


def main():
    print("=" * 72)
    print("DOLUNAY TAKI — TRENDYOL -> HEPSİBURADA STOK SENKRONİZASYONU")
    print("=" * 72)

    products = get_trendyol_products()
    listings = get_all_hb_listings()

    indexes = build_indexes(listings)

    updates = []
    matched = 0
    unchanged = 0
    missing = 0
    seen = set()

    for product in products:
        row, method = find_match(product, indexes)

        if row is None:
            missing += 1
            log(
                f"⚠️ HB eşleşmedi | "
                f"{product['title']} | "
                f"stockCode={product['stockCode']} | "
                f"barcode={product['barcode']}"
            )
            continue

        fields = row["fields"]
        hb_sku = safe(fields["hbSku"])

        if not hb_sku:
            missing += 1
            log(
                f"⚠️ HB listing bulundu fakat hbSku yok | "
                f"{product['title']}"
            )
            continue

        normalized_hb = sku_key(hb_sku)

        if normalized_hb in seen:
            continue

        seen.add(normalized_hb)
        matched += 1

        target_stock = product["stock"]

        try:
            current_stock = int(float(fields["availableStock"]))
        except (TypeError, ValueError):
            current_stock = None

        if current_stock == target_stock:
            unchanged += 1
            continue

        updates.append(
            {
                "hepsiburadaSku": hb_sku,
                "availableStock": target_stock,
            }
        )

        log(
            f"🔄 STOK DEĞİŞİMİ | "
            f"{product['title']} | "
            f"HB={hb_sku} | "
            f"{current_stock} -> {target_stock} | "
            f"eşleşme={method}"
        )

    log(
        f"📊 SONUÇ | Trendyol={len(products)} | "
        f"HB listing={len(listings)} | "
        f"eşleşen={matched} | "
        f"güncellenecek={len(updates)} | "
        f"aynı={unchanged} | "
        f"eşleşmeyen={missing}"
    )

    upload_stock(updates)

    log("✅ STOK SENKRONİZASYONU BİTTİ.")


if __name__ == "__main__":
    main()
