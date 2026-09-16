import json
import os
import re
import time
import unicodedata
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

import requests

TRENDYOL_BASE = "https://apigw.trendyol.com"
HB_LISTING_BASE = os.getenv("HB_LISTING_BASE_URL", "https://listing-external.hepsiburada.com").rstrip("/")
HB_CATALOG_BASE = os.getenv("HB_CATALOG_BASE_URL", "https://mpop.hepsiburada.com").rstrip("/")
TIMEOUT = 60
PAGE_SIZE = 100
MAX_RETRIES = 4
CATALOG_PAGE_SIZE = 2000
CATEGORY_CACHE = Path(os.getenv("HB_CATEGORY_CACHE", "hb_category_cache.json"))
STATE_FILE = Path(os.getenv("SYNC_STATE_FILE", "sync_state.json"))
MISSING_RUN_THRESHOLD = int(os.getenv("MISSING_RUN_THRESHOLD", "2"))
NEW_PRODUCT_SYNC = os.getenv("NEW_PRODUCT_SYNC", "true").strip().lower() == "true"
CATALOG_AUTO_UPLOAD = os.getenv("CATALOG_AUTO_UPLOAD", "true").strip().lower() == "true"


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

if "-sit." in HB_LISTING_BASE.lower() or "-sit." in HB_CATALOG_BASE.lower():
    raise RuntimeError("Canlı workflow SIT endpoint'i ile çalıştırılamaz.")


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


def sku_key(value):
    return re.sub(r"\s+", "", safe(value)).upper()


def barcode_key(value):
    return re.sub(r"\D+", "", safe(value))


def parse_number(value):
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = safe(value).replace(".", "").replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def json_or_fail(response, label):
    try:
        return response.json()
    except ValueError as exc:
        raise RuntimeError(f"{label} JSON döndürmedi: {response.text[:3000]}") from exc


def hb_request(method, url, **kwargs):
    kwargs.setdefault("headers", {})
    kwargs["headers"].setdefault("User-Agent", "dolunaytaki_dev")
    kwargs["headers"].setdefault("Accept", "application/json")
    kwargs["headers"].setdefault("Content-Type", "application/json")
    kwargs["auth"] = (HB_MERCHANT_ID, HB_SECRET_KEY)
    kwargs.setdefault("timeout", TIMEOUT)
    last = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.request(method, url, **kwargs)
            last = response
        except requests.RequestException as exc:
            if attempt == MAX_RETRIES:
                raise RuntimeError(f"Hepsiburada bağlantı hatası: {exc}") from exc
            time.sleep(min(2 ** attempt, 30))
            continue
        if response.status_code not in (429, 500, 502, 503, 504):
            return response
        time.sleep(min(2 ** attempt, 30))
    return last


def ty_get(url, params):
    response = requests.get(
        url,
        headers={"User-Agent": f"{SUPPLIER_ID} - SelfIntegration", "Accept": "application/json"},
        auth=(TY_API_KEY, TY_API_SECRET),
        params=params,
        timeout=TIMEOUT,
    )
    if response.status_code != 200:
        raise RuntimeError(f"Trendyol HTTP {response.status_code}: {response.text[:3000]}")
    return json_or_fail(response, "Trendyol")


def extract_images(product):
    raw = product.get("images") or product.get("image") or []
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        out = []
        for item in raw:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict):
                for key in ("url", "imageUrl", "src"):
                    if item.get(key):
                        out.append(safe(item[key]))
                        break
        return out[:5]
    return []


def extract_attributes(product, variant):
    candidates = []
    for obj in (variant, product):
        for key in ("attributes", "attributeValues", "variants", "productAttributes"):
            value = obj.get(key) if isinstance(obj, dict) else None
            if isinstance(value, list):
                candidates.extend(value)
            elif isinstance(value, dict):
                candidates.append(value)
    attrs = {}
    for item in candidates:
        if isinstance(item, dict):
            name = safe(item.get("name") or item.get("attributeName") or item.get("key"))
            value = safe(item.get("value") or item.get("attributeValue") or item.get("text"))
            if name and value:
                attrs[norm(name)] = value
    return attrs


def get_trendyol_products():
    products = []
    page = 0
    while True:
        data = ty_get(
            f"{TRENDYOL_BASE}/integration/product/sellers/{SUPPLIER_ID}/products/approved",
            {"page": page, "size": 100},
        )
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
                    stock = variant.get("quantity", product.get("quantity", 0))
                try:
                    stock = max(0, int(float(stock)))
                except (TypeError, ValueError):
                    stock = 0
                sale_price = (
                    variant.get("salePrice")
                    or variant.get("sellingPrice")
                    or variant.get("price")
                    or product.get("salePrice")
                    or product.get("sellingPrice")
                    or product.get("price")
                )
                products.append({
                    "barcode": barcode,
                    "stock": stock,
                    "stockCode": safe(variant.get("stockCode") or product.get("stockCode")),
                    "productCode": safe(variant.get("productCode") or product.get("productCode")),
                    "title": safe(product.get("title") or product.get("productName")),
                    "description": safe(product.get("description") or product.get("descriptionContent")),
                    "brand": safe(product.get("brand") or product.get("brandName")),
                    "categoryName": safe(product.get("categoryName") or product.get("category")),
                    "categoryId": safe(product.get("categoryId")),
                    "images": extract_images(product),
                    "price": parse_number(sale_price),
                    "attributes": extract_attributes(product, variant),
                })
        if len(content) < 100:
            break
        page += 1
    log(f"✅ Trendyol ürün/varyant: {len(products)}")
    return products


def extract_listing_fields(obj):
    fields = {"merchantSku": "", "hbSku": "", "barcode": "", "name": "", "price": None, "availableStock": None}

    def walk(value):
        if isinstance(value, dict):
            for raw_key, raw_value in value.items():
                key = safe(raw_key).replace("-", "_").lower()
                if isinstance(raw_value, (str, int, float)) and raw_value not in ("", None):
                    if not fields["merchantSku"] and key in {"merchantsku", "merchant_sku", "seller_sku", "sellersku"}:
                        fields["merchantSku"] = safe(raw_value)
                    if not fields["hbSku"] and key in {"hbsku", "hepsiburdasku", "hepsiburadasku", "hepsiburada_sku"}:
                        fields["hbSku"] = safe(raw_value)
                    if not fields["barcode"] and key in {"barcode", "ean", "ean13", "gtin"}:
                        fields["barcode"] = safe(raw_value)
                    if not fields["name"] and key in {"productname", "product_name", "producttitle", "name", "title"}:
                        fields["name"] = safe(raw_value)
                    if fields["availableStock"] is None and key in {"availablestock", "available_stock", "stock", "stockquantity"}:
                        fields["availableStock"] = raw_value
                    if fields["price"] is None and key in {"price", "saleprice", "sellingprice"}:
                        fields["price"] = parse_number(raw_value)
                walk(raw_value)
        elif isinstance(value, list):
            for item in value:
                walk(item)
    walk(obj)
    return fields


def get_all_hb_listings():
    url = f"{HB_LISTING_BASE}/listings/merchantid/{HB_MERCHANT_ID}"
    listings, offset = [], 0
    while True:
        response = hb_request("GET", url, params={"offset": offset, "limit": PAGE_SIZE})
        if response.status_code != 200:
            raise RuntimeError(f"HB listing HTTP {response.status_code}: {response.text[:3000]}")
        data = json_or_fail(response, "HB listing")
        items = data if isinstance(data, list) else (data.get("listings") or data.get("items") or data.get("data") or data.get("content") or [])
        if isinstance(items, dict):
            items = items.get("items") or items.get("data") or items.get("content") or []
        if not items:
            break
        listings.extend([x for x in items if isinstance(x, dict)])
        if len(items) < PAGE_SIZE:
            break
        offset += len(items)
    log(f"✅ Canlı Hepsiburada listing: {len(listings)}")
    return listings


def build_hb_indexes(listings):
    idx = {"merchantSku": {}, "hbSku": {}, "barcode": {}, "name": {}}
    for item in listings:
        f = extract_listing_fields(item)
        row = {"raw": item, "fields": f}
        if f["merchantSku"]: idx["merchantSku"][sku_key(f["merchantSku"])] = row
        if f["hbSku"]: idx["hbSku"][sku_key(f["hbSku"])] = row
        if f["barcode"]: idx["barcode"][barcode_key(f["barcode"])] = row
        if f["name"]: idx["name"][norm(f["name"])] = row
    return idx


def find_hb_match(product, idx):
    for candidate in (product["stockCode"], product["productCode"]):
        key = sku_key(candidate)
        if key and key in idx["merchantSku"]:
            return idx["merchantSku"][key], "merchantSku"
        if key and key in idx["hbSku"]:
            return idx["hbSku"][key], "hbSku"
    bkey = barcode_key(product["barcode"])
    if bkey and bkey in idx["barcode"]:
        return idx["barcode"][bkey], "barcode"
    n = norm(product["title"])
    if n and n in idx["name"]:
        return idx["name"][n], "name"
    return None, ""


def upload_listing_rows(rows):
    if not rows:
        return
    url = f"{HB_LISTING_BASE}/listings/merchantid/{HB_MERCHANT_ID}/inventory-uploads"
    payload = []
    for row in rows:
        payload.append({
            "hepsiburadaSku": row["hepsiburadaSku"],
            "merchantSku": row["merchantSku"],
            "price": row["price"],
            "availableStock": row["availableStock"],
        })
    response = hb_request("POST", url, json=payload)
    log(f"📡 HB stok/fiyat yükleme | HTTP {response.status_code} | {len(payload)} SKU")
    body = json_or_fail(response, "HB stok/fiyat")
    if response.status_code not in (200, 201, 202):
        raise RuntimeError(f"HB stok/fiyat başarısız: {response.text[:5000]}")
    upload_id = ""
    if isinstance(body, dict):
        d = body.get("data") if isinstance(body.get("data"), dict) else body
        upload_id = safe(d.get("id") or d.get("inventoryUploadId") or d.get("uploadId")) if isinstance(d, dict) else ""
    if not upload_id:
        raise RuntimeError(f"HB stok/fiyat kabul edildi ancak işlem ID yok: {body}")
    status_url = f"{HB_LISTING_BASE}/listings/merchantid/{HB_MERCHANT_ID}/inventory-uploads/id/{upload_id}"
    for _ in range(20):
        time.sleep(3)
        r = hb_request("GET", status_url)
        if r.status_code != 200:
            raise RuntimeError(f"HB stok/fiyat durum HTTP {r.status_code}: {r.text[:3000]}")
        data = json_or_fail(r, "HB stok/fiyat durum")
        p = data.get("data") if isinstance(data, dict) and isinstance(data.get("data"), dict) else data
        status = safe(p.get("status") or p.get("state")).upper() if isinstance(p, dict) else ""
        log(f"📌 HB stok/fiyat durum: {status or 'BİLİNMİYOR'}")
        if status in {"SUCCESS", "SUCCEEDED", "COMPLETED", "DONE", "FINISHED"}:
            log(f"✅ HB stok/fiyat tamamlandı | işlem={upload_id}")
            return upload_id
        if status in {"FAILED", "FAIL", "ERROR"}:
            raise RuntimeError(f"HB stok/fiyat başarısız: {data}")
    raise RuntimeError("HB stok/fiyat durumu 60 saniyede tamamlanmadı.")


def catalog_post(path, payload):
    response = hb_request("POST", f"{HB_CATALOG_BASE}{path}", json=payload)
    data = json_or_fail(response, f"HB katalog {path}")
    if response.status_code not in (200, 201, 202):
        raise RuntimeError(f"HB katalog HTTP {response.status_code}: {response.text[:5000]}")
    return data


def catalog_status(tracking_id):
    return json_or_fail(
        hb_request("GET", f"{HB_CATALOG_BASE}/product/api/products/status/{tracking_id}"),
        "HB katalog durum",
    )


def upload_fast_listing(product):
    merchant_sku = sku_key(product["stockCode"] or product["productCode"] or product["barcode"])
    payload = [{
        "merchantId": HB_MERCHANT_ID,
        "merchantSku": merchant_sku,
        "productName": product["title"][:250],
        "barcode": product["barcode"],
    }]
    data = catalog_post("/product/api/products/fastlisting", payload)
    tracking_id = safe(data.get("trackingId") or (data.get("data") or {}).get("trackingId") if isinstance(data, dict) else "")
    if tracking_id:
        log(f"🆕 Hızlı ürün yükleme trackingId={tracking_id} | {merchant_sku}")
        return tracking_id
    log(f"⚠️ Hızlı ürün yükleme trackingId dönmedi | {merchant_sku} | {data}")
    return ""




def get_categories():
    if CATEGORY_CACHE.exists():
        try:
            return json.loads(CATEGORY_CACHE.read_text(encoding="utf-8"))
        except Exception:
            pass
    categories = []
    page = 0
    while True:
        r = hb_request("GET", f"{HB_CATALOG_BASE}/product/api/categories/get-all-categories",
                       params={"page": page, "size": CATALOG_PAGE_SIZE, "leaf": "true", "status": "AKTİF", "available": "true"})
        if r.status_code != 200:
            raise RuntimeError(f"HB kategori HTTP {r.status_code}: {r.text[:3000]}")
        data = json_or_fail(r, "HB categories")
        rows = data.get("data") if isinstance(data, dict) else []
        if not isinstance(rows, list) or not rows:
            break
        categories.extend(rows)
        if len(rows) < CATALOG_PAGE_SIZE:
            break
        page += 1
    CATEGORY_CACHE.write_text(json.dumps(categories, ensure_ascii=False), encoding="utf-8")
    log(f"📚 HB aktif leaf kategori: {len(categories)}")
    return categories


def choose_category(product, categories):
    target = norm(f"{product.get('categoryName','')} {product.get('title','')}")
    if not target:
        return None
    # Strong jewelry hints keep selection inside the relevant branch.
    hints = ["taki", "kolye", "bileklik", "kupe", "yuzuk", "piercing", "aksesuar"]
    scored = []
    for c in categories:
        name = norm(c.get("name"))
        path = norm(c.get("paths"))
        hay = f"{name} {path}"
        score = SequenceMatcher(None, target, hay).ratio()
        score += 0.12 * sum(1 for h in hints if h in hay and h in target)
        scored.append((score, c))
    scored.sort(key=lambda x: x[0], reverse=True)
    if not scored or scored[0][0] < 0.38:
        return None
    return scored[0][1]


def get_category_attributes(category_id):
    r = hb_request("GET", f"{HB_CATALOG_BASE}/product/api/categories/{category_id}/attributes")
    if r.status_code != 200:
        raise RuntimeError(f"HB kategori özellik HTTP {r.status_code}: {r.text[:3000]}")
    data = json_or_fail(r, "HB category attributes")
    if data.get("success") is False:
        raise RuntimeError(f"HB kategori özellik hatası: {data}")
    d = data.get("data") or {}
    attrs = []
    if isinstance(d, list):
        attrs = d
    elif isinstance(d, dict):
        for key in ("baseAttributes", "attributes", "variantAttributes"):
            vals = d.get(key) or []
            if isinstance(vals, list):
                attrs.extend(vals)
    return attrs


def build_catalog_payload(product, category, attrs):
    source_attrs = product.get("attributes") or {}
    norm_source = {norm(k): v for k, v in source_attrs.items()}
    common = {
        "merchantSku": sku_key(product["stockCode"] or product["productCode"] or product["barcode"]),
        "VaryantGroupID": sku_key(product["stockCode"] or product["productCode"] or product["barcode"]),
        "UrunAdi": product["title"][:250],
        "UrunAciklamasi": product["description"],
        "Barcode": product["barcode"],
        "Marka": product["brand"],
        "price": f"{product['price']:.2f}" if product.get("price") is not None else "0.00",
        "stock": str(product["stock"]),
    }
    for i, url in enumerate(product.get("images") or [], start=1):
        if i <= 5 and url:
            common[f"Image{i}"] = url
    missing = []
    for attr in attrs:
        name = safe(attr.get("name"))
        if not name:
            continue
        if name in {"merchantSku", "Barcode", "UrunAdi", "UrunAciklamasi", "Marka", "price", "stock", "VaryantGroupID"}:
            continue
        key = norm(name)
        value = norm_source.get(key)
        if value is None:
            # Common jewelry aliases from Trendyol.
            aliases = {
                "renk": ["renk", "color"],
                "materyal": ["materyal", "material"],
                "tas": ["tas", "stone"],
                "cinsiyet": ["cinsiyet", "gender"],
            }
            for alias in aliases.get(key, []):
                if alias in norm_source:
                    value = norm_source[alias]
                    break
        if value is None and attr.get("mandatory") is True:
            missing.append(name)
        elif value is not None:
            common[name] = value
    if missing:
        return None, missing
    return [{"categoryId": int(category.get("categoryId")), "merchant": HB_MERCHANT_ID, "attributes": common}], []


def upload_catalog_product(product):
    categories = get_categories()
    category = choose_category(product, categories)
    if not category:
        log(f"⏸️ Kategori otomatik bulunamadı | {product['title']}")
        return ""
    attrs = get_category_attributes(category.get("categoryId"))
    payload, missing = build_catalog_payload(product, category, attrs)
    if payload is None:
        log(f"⏸️ Zorunlu kategori alanları eksik | {product['title']} | {', '.join(missing)}")
        return ""
    data = catalog_post("/product/api/products/import", payload)
    tracking_id = ""
    if isinstance(data, dict):
        tracking_id = safe(data.get("trackingId") or (data.get("data") or {}).get("trackingId"))
    if not tracking_id:
        log(f"⚠️ Katalog ürün gönderildi fakat trackingId alınamadı | {product['title']} | {data}")
        return ""
    log(f"📦 YENİ ÜRÜN KATALOĞA GÖNDERİLDİ | {product['title']} | trackingId={tracking_id}")
    for _ in range(20):
        time.sleep(3)
        status = catalog_status(tracking_id)
        state = safe(status.get("status")) if isinstance(status, dict) else ""
        log(f"📌 Katalog durum | {tracking_id} | {state or status}")
        if state in {"SUCCESS", "SUCCEEDED", "COMPLETED", "DONE", "FINISHED", "CREATED"}:
            return tracking_id
        if state in {"FAILED", "ERROR", "REJECTED"}:
            log(f"❌ Katalog ürün sonucu başarısız | {status}")
            return ""
    return tracking_id

def load_state():
    if not STATE_FILE.exists():
        return {"products": {}}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"products": {}}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def main():
    products = get_trendyol_products()
    listings = get_all_hb_listings()
    idx = build_hb_indexes(listings)
    state = load_state()
    seen_now = set()
    updates = []
    new_products = []

    for p in products:
        identity = sku_key(p["stockCode"] or p["productCode"] or p["barcode"])
        if not identity:
            continue
        seen_now.add(identity)
        row, method = find_hb_match(p, idx)
        if row is None:
            new_products.append(p)
            log(f"🆕 HB'de yok | {p['title']} | barcode={p['barcode']} | sku={identity}")
            continue
        f = row["fields"]
        hb_sku = safe(f["hbSku"])
        merchant_sku = safe(f["merchantSku"]) or identity
        if not hb_sku:
            log(f"⚠️ HB listing eşleşti ama hbSku yok | {p['title']}")
            continue
        target_price = p["price"] if p["price"] is not None else f["price"]
        if target_price is None:
            log(f"⚠️ Fiyat yok, güncelleme atlandı | {p['title']}")
            continue
        current_stock = int(float(f["availableStock"])) if f["availableStock"] not in (None, "") else None
        if current_stock != p["stock"] or f["price"] is None or abs(float(f["price"]) - float(target_price)) > 0.005:
            updates.append({"hepsiburadaSku": hb_sku, "merchantSku": merchant_sku, "price": round(float(target_price), 2), "availableStock": p["stock"]})
            log(f"🔄 GÜNCELLE | {p['title']} | HB={hb_sku} | MerchantSku={merchant_sku} | Price={target_price:.2f} | Stock={p['stock']} | eşleşme={method}")
        state["products"].setdefault(identity, {"missingRuns": 0})
        state["products"][identity]["missingRuns"] = 0

    if updates:
        upload_listing_rows(updates)

    if NEW_PRODUCT_SYNC and new_products:
        log(f"🆕 Yeni ürün adayı: {len(new_products)}")
        if CATALOG_AUTO_UPLOAD:
            for p in new_products:
                try:
                    tracking = upload_fast_listing(p)
                    if not tracking:
                        upload_catalog_product(p)
                except Exception as exc:
                    log(f"⚠️ Yeni ürün aktarımı başarısız | {p['title']} | {exc}")

    # Products removed from Trendyol: do not delete catalog rows. After the configured
    # grace period, existing HB listings are set to stock=0 so they stop selling.
    removed_updates = []
    for identity, meta in list(state.get("products", {}).items()):
        if identity in seen_now:
            continue
        meta["missingRuns"] = int(meta.get("missingRuns", 0)) + 1
        if meta["missingRuns"] < MISSING_RUN_THRESHOLD:
            continue
        row = idx["merchantSku"].get(identity)
        if not row:
            continue
        f = row["fields"]
        if not f["hbSku"]:
            continue
        price = f["price"]
        if price is None:
            continue
        if f["availableStock"] in (None, 0, "0"):
            continue
        removed_updates.append({"hepsiburadaSku": f["hbSku"], "merchantSku": f["merchantSku"] or identity, "price": price, "availableStock": 0})
        log(f"⛔ Trendyol'dan kaldırılmış | HB stok 0 yapılacak | MerchantSku={identity}")
    if removed_updates:
        upload_listing_rows(removed_updates)
    for identity in list(state.get("products", {})):
        if identity not in seen_now and state["products"][identity].get("missingRuns", 0) >= MISSING_RUN_THRESHOLD:
            state["products"][identity]["lastAction"] = "stock_zeroed"
    save_state(state)

    log(f"📊 SONUÇ | TY={len(products)} | HB={len(listings)} | güncelleme={len(updates)} | yeni={len(new_products)} | kaldırılan={len(removed_updates)}")
    log("✅ TAM SENKRONİZASYON BİTTİ.")


if __name__ == "__main__":
    main()
