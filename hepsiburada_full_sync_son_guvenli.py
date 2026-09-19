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
MAX_LISTING_PAGES = 100
MAX_RETRIES = 4
MISSING_RUN_THRESHOLD = int(os.getenv("MISSING_RUN_THRESHOLD", "2"))
NEW_PRODUCT_SYNC = os.getenv("NEW_PRODUCT_SYNC", "true").strip().lower() == "true"
STATE_FILE = Path(os.getenv("SYNC_STATE_FILE", "sync_state.json"))
CATEGORY_CACHE = Path(os.getenv("HB_CATEGORY_CACHE", "hb_category_cache.json"))


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
    raise RuntimeError("Canlı tam senkronizasyon SIT endpoint'i ile çalıştırılamaz.")


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
    text = safe(value)
    if not text:
        return None
    if text.count(",") == 1 and text.count(".") >= 1:
        text = text.replace(".", "").replace(",", ".")
    elif "," in text:
        text = text.replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def price_hb(value):
    number = parse_number(value)
    if number is None:
        return None
    return f"{number:.2f}".replace(".", ",")


def json_or_fail(response, label):
    try:
        return response.json()
    except ValueError as exc:
        raise RuntimeError(f"{label} JSON döndürmedi: {response.text[:5000]}") from exc


def hb_request(method, url, **kwargs):
    headers = dict(kwargs.pop("headers", {}) or {})
    headers.setdefault("User-Agent", "dolunaytaki_dev")
    headers.setdefault("Accept", "application/json")

    has_files = "files" in kwargs
    if not has_files:
        headers.setdefault("Content-Type", "application/json")
    kwargs["headers"] = headers
    kwargs["auth"] = (HB_MERCHANT_ID, HB_SECRET_KEY)
    kwargs.setdefault("timeout", TIMEOUT)

    last = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.request(method, url, **kwargs)
            last = response
        except requests.RequestException as exc:
            if attempt >= MAX_RETRIES:
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
        raise RuntimeError(f"Trendyol HTTP {response.status_code}: {response.text[:5000]}")
    return json_or_fail(response, "Trendyol")


def extract_images(product):
    raw = product.get("images") or product.get("image") or []
    if isinstance(raw, str):
        return [raw]
    if not isinstance(raw, list):
        return []
    result = []
    for item in raw:
        if isinstance(item, str):
            result.append(item)
        elif isinstance(item, dict):
            for key in ("url", "imageUrl", "src"):
                if item.get(key):
                    result.append(safe(item[key]))
                    break
    return result[:5]


def extract_attributes(product, variant):
    attrs = {}
    candidates = []
    for obj in (product, variant):
        if not isinstance(obj, dict):
            continue
        for key in ("attributes", "attributeValues", "productAttributes"):
            value = obj.get(key)
            if isinstance(value, list):
                candidates.extend(value)
            elif isinstance(value, dict):
                candidates.append(value)
    for item in candidates:
        if not isinstance(item, dict):
            continue
        name = safe(item.get("name") or item.get("attributeName") or item.get("key"))
        value = item.get("value") or item.get("attributeValue") or item.get("text")
        if name and value is not None:
            attrs[norm(name)] = safe(value)
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
                raw_category = product.get("categoryName") or product.get("category")
                category_name = extract_category_name(raw_category)
                products.append({
                    "identity": sku_key(variant.get("stockCode") or variant.get("productCode") or barcode),
                    "barcode": barcode,
                    "stock": stock,
                    "stockCode": safe(variant.get("stockCode") or product.get("stockCode")),
                    "productCode": safe(variant.get("productCode") or product.get("productCode")),
                    "title": safe(product.get("title") or product.get("productName")),
                    "description": safe(product.get("description") or product.get("descriptionContent")),
                    "brand": safe(product.get("brand") or product.get("brandName")),
                    "categoryName": category_name,
                    "categoryRaw": raw_category,
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
    fields = {
        "merchantSku": "",
        "hbSku": "",
        "barcode": "",
        "name": "",
        "price": None,
        "availableStock": None,
    }

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
    listings = []
    offset = 0
    for _ in range(MAX_LISTING_PAGES):
        response = hb_request("GET", url, params={"offset": offset, "limit": PAGE_SIZE})
        log(f"Hepsiburada listing offset={offset} limit={PAGE_SIZE} | HTTP {response.status_code}")
        if response.status_code != 200:
            raise RuntimeError(f"HB listing HTTP {response.status_code}: {response.text[:5000]}")
        data = json_or_fail(response, "HB listing")
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = data.get("listings") or data.get("items") or data.get("data") or data.get("content") or []
            if isinstance(items, dict):
                items = items.get("listings") or items.get("items") or items.get("data") or items.get("content") or []
        else:
            items = []
        items = [x for x in items if isinstance(x, dict)]
        if not items:
            break
        listings.extend(items)
        if len(items) < PAGE_SIZE:
            break
        offset += len(items)
    log(f"✅ Canlı Hepsiburada listing: {len(listings)}")
    return listings


def build_hb_indexes(listings):
    idx = {"merchantSku": {}, "hbSku": {}, "barcode": {}, "name": {}}
    for item in listings:
        fields = extract_listing_fields(item)
        row = {"raw": item, "fields": fields}
        if fields["merchantSku"]:
            idx["merchantSku"][sku_key(fields["merchantSku"])] = row
        if fields["hbSku"]:
            idx["hbSku"][sku_key(fields["hbSku"])] = row
        if fields["barcode"]:
            idx["barcode"][barcode_key(fields["barcode"])] = row
        if fields["name"]:
            idx["name"][norm(fields["name"])] = row
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
        return ""
    url = f"{HB_LISTING_BASE}/listings/merchantid/{HB_MERCHANT_ID}/inventory-uploads"
    payload = [{
        "hepsiburadaSku": row["hepsiburadaSku"],
        "merchantSku": row["merchantSku"],
        "price": price_hb(row["price"]),
        "availableStock": int(row["availableStock"]),
    } for row in rows]
    response = hb_request("POST", url, json=payload)
    log(f"📡 HB stok/fiyat yükleme | HTTP {response.status_code} | {len(payload)} SKU")
    body = json_or_fail(response, "HB stok/fiyat")
    if response.status_code not in (200, 201, 202):
        raise RuntimeError(f"HB stok/fiyat başarısız: {response.text[:5000]}")
    data = body.get("data") if isinstance(body, dict) and isinstance(body.get("data"), dict) else body
    upload_id = safe(data.get("id") or data.get("inventoryUploadId") or data.get("uploadId")) if isinstance(data, dict) else ""
    if not upload_id:
        raise RuntimeError(f"HB stok/fiyat kabul edildi ancak işlem ID dönmedi: {body}")
    status_url = f"{HB_LISTING_BASE}/listings/merchantid/{HB_MERCHANT_ID}/inventory-uploads/id/{upload_id}"
    for attempt in range(1, 21):
        time.sleep(3)
        status_response = hb_request("GET", status_url)
        if status_response.status_code != 200:
            raise RuntimeError(f"HB stok/fiyat durum HTTP {status_response.status_code}: {status_response.text[:5000]}")
        status_body = json_or_fail(status_response, "HB stok/fiyat durum")
        status_data = status_body.get("data") if isinstance(status_body, dict) and isinstance(status_body.get("data"), dict) else status_body
        status = safe(status_data.get("status") or status_data.get("state")).upper() if isinstance(status_data, dict) else ""
        log(f"📌 HB stok/fiyat durum {attempt}/20 | {status or 'BİLİNMİYOR'}")
        if status in {"SUCCESS", "SUCCEEDED", "COMPLETED", "DONE", "FINISHED"}:
            log(f"✅ HB stok/fiyat tamamlandı | işlem={upload_id}")
            return upload_id
        if status in {"FAILED", "FAIL", "ERROR"}:
            raise RuntimeError(f"HB stok/fiyat başarısız: {status_body}")
    raise RuntimeError("HB stok/fiyat durumu 60 saniyede tamamlanmadı.")


def _load_cached_categories():
    if not CATEGORY_CACHE.exists():
        return []
    try:
        cached = json.loads(CATEGORY_CACHE.read_text(encoding="utf-8"))
    except Exception:
        return []
    if isinstance(cached, dict) and isinstance(cached.get("categories"), list):
        rows = cached["categories"]
    elif isinstance(cached, list):
        rows = cached
    else:
        rows = []
    return [x for x in rows if isinstance(x, dict)]


def _extract_page_rows(body):
    """Return (rows, meta) for the several response envelopes used by HB."""
    meta = body if isinstance(body, dict) else {}
    data = body.get("data") if isinstance(body, dict) else body
    rows = data

    if isinstance(data, dict):
        # Data itself may contain both rows and pagination metadata.
        meta = data
        for key in ("content", "items", "data"):
            value = data.get(key)
            if isinstance(value, list):
                rows = value
                break
        else:
            rows = []

    if not isinstance(rows, list):
        rows = []
    return [x for x in rows if isinstance(x, dict)], meta


def _fetch_categories_with_size(size):
    """Fetch all pages; do not assume a short page is the final page.

    Some live responses cap size to 500 even when a larger size is requested.
    The official API exposes totalPages/last, but this routine also safely walks
    forward until an empty page when those fields are absent.
    """
    url = f"{HB_CATALOG_BASE}/product/api/categories/get-all-categories"
    # Hepsiburada support confirmed the live category request format:
    # /product/api/categories/get-all-categories
    # with leaf=true, status=ACTIVE, available=true, page, size and version=1.
    # Do not add merchantId here; use the exact endpoint/query shape supplied by HB.
    base_params = {
        "leaf": "true",
        "status": "ACTIVE",
        "available": "true",
        "size": size,
        "version": "1",
    }
    rows_all = []
    seen_signatures = set()

    for page in range(0, 200):
        params = dict(base_params)
        params["page"] = page
        response = hb_request("GET", url, params=params)
        log(f"📚 HB kategori | HTTP {response.status_code} | page={page} size={size} | status=ACTIVE | available=true | leaf=true | version=1")

        if response.status_code != 200:
            raise RuntimeError(
                f"HB kategori HTTP {response.status_code}: {response.text[:5000]}"
            )

        body = json_or_fail(response, "HB kategori")
        page_rows, meta = _extract_page_rows(body)
        if not page_rows:
            break

        # Guard against a broken API returning the exact same page forever.
        sig = tuple(sorted(str(extract_category_id(x)) for x in page_rows[:50]))
        sig_key = (page, sig)
        if sig_key in seen_signatures:
            break
        seen_signatures.add(sig_key)
        rows_all.extend(page_rows)

        last = meta.get("last") if isinstance(meta, dict) else None
        total_pages = meta.get("totalPages") if isinstance(meta, dict) else None
        number = meta.get("number") if isinstance(meta, dict) else page
        number_of_elements = meta.get("numberOfElements") if isinstance(meta, dict) else len(page_rows)

        if last is True:
            break
        if isinstance(total_pages, int) and total_pages > 0 and page + 1 >= total_pages:
            break
        if isinstance(number_of_elements, int) and number_of_elements == 0:
            break
        # IMPORTANT: do not stop merely because len(page_rows) < requested size.
        # The live service can clamp a request for size=2000 down to 500.
        if isinstance(number, int) and number != page:
            # Service controls its own page number; continue using sequential page values.
            pass

    unique = {}
    for category in rows_all:
        cid = extract_category_id(category)
        if cid is not None:
            unique[cid] = category
    return list(unique.values())


def get_categories():
    """Load a complete usable category set, with safe cached fallback."""
    cached = _load_cached_categories()
    cache_count = len(cached)

    # Use the exact live category endpoint/query format supplied by Hepsiburada.
    # Larger size is attempted first; smaller sizes are retained as a defensive fallback.
    last_error = None
    for size in (2000, 1000, 500, 100):
        try:
            categories = _fetch_categories_with_size(size)
            if categories:
                CATEGORY_CACHE.write_text(
                    json.dumps(
                        {"version": 3, "categories": categories},
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                log(f"📚 HB aktif leaf kategori: {len(categories)}")
                return categories
        except Exception as exc:
            last_error = exc
            log(f"⚠️ Kategori çekimi başarısız | size={size} | {exc}")

    # If live category service is temporarily unavailable, keep working with
    # the last cached tree rather than breaking stock/price synchronization.
    if cached:
        log(f"🗂️ Canlı kategori servisi kullanılamıyor; mevcut önbellek kullanılacak: {cache_count} kategori")
        return cached

    log(f"⚠️ Kullanılabilir kategori listesi yok | {last_error or 'bilinmeyen hata'}")
    return []


def extract_category_name(value):
    if isinstance(value, dict):
        return safe(
            value.get("name")
            or value.get("categoryName")
            or value.get("title")
            or value.get("displayName")
        )
    return safe(value)


def extract_category_id(value):
    if isinstance(value, dict):
        raw = value.get("categoryId")
        if raw is None:
            raw = value.get("id")
        if raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def choose_category(product, categories):
    """Choose a single, defensible HB leaf category.

    The Trendyol category id is NOT assumed to equal the HB category id.
    Matching therefore uses the category name/path first. Fuzzy matching is
    only accepted when one result is clearly unique; otherwise the product is
    held instead of risking the wrong category.
    """
    target = norm(product.get("categoryName"))
    if not target:
        target = norm(extract_category_name(product.get("categoryRaw")))
    if not target:
        return None

    usable = []
    for c in categories:
        cid = extract_category_id(c)
        name = safe(c.get("name"))
        if cid is None or not name:
            continue
        leaf = c.get("leaf")
        available = c.get("available")
        status = norm(c.get("status"))
        if leaf is False or available is False:
            continue
        if status and status not in {"aktif", "active"}:
            continue
        usable.append(c)

    target_tokens = set(target.split())

    # 1) Exact category name.
    exact = [c for c in usable if norm(c.get("name")) == target]
    if len(exact) == 1:
        return exact[0]

    # 2) Exact match against the end/leaf part of the path.
    exact_path = []
    for c in usable:
        path_tokens = [norm(part) for part in safe(c.get("paths")).split("/") if norm(part)]
        if path_tokens and path_tokens[-1] == target:
            exact_path.append(c)
    if len(exact_path) == 1:
        return exact_path[0]

    # 3) Exact token inclusion. Require all target tokens and, where possible,
    # matching product type/material tokens.
    candidates = []
    for c in usable:
        hay = norm(f"{c.get('name', '')} {c.get('paths', '')}")
        hay_tokens = set(hay.split())
        if target_tokens and target_tokens.issubset(hay_tokens):
            candidates.append(c)
    if len(candidates) == 1:
        return candidates[0]

    # 4) Common jewelry category rules, but only if the result is unique.
    for trigger in ("piercing", "sahmeran", "bileklik", "kolye", "kupe", "yuzuk"):
        if trigger not in target:
            continue
        candidates = []
        target_materials = {t for t in ("celik", "gumus", "altin", "bijuteri") if t in target_tokens}
        for c in usable:
            hay = norm(f"{c.get('name', '')} {c.get('paths', '')}")
            hay_tokens = set(hay.split())
            if trigger in hay_tokens or trigger in norm(c.get("name")):
                if target_materials and not target_materials.intersection(hay_tokens):
                    continue
                candidates.append(c)
        if len(candidates) == 1:
            return candidates[0]

    # 5) Conservative fuzzy match. Never pick a weak or tied winner.
    scored = []
    for c in usable:
        name_norm = norm(c.get("name"))
        path_norm = norm(c.get("paths"))
        hay_tokens = set(f"{name_norm} {path_norm}".split())
        overlap = len(target_tokens & hay_tokens) / max(1, len(target_tokens))
        score = SequenceMatcher(None, target, name_norm).ratio()
        if overlap >= 0.75 and score >= 0.75:
            scored.append((overlap, score, c))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    if len(scored) == 1:
        return scored[0][2]
    if len(scored) >= 2:
        best, second = scored[0], scored[1]
        if best[0] == 1.0 and (best[0] > second[0] or best[1] - second[1] >= 0.15):
            return best[2]
        if best[0] - second[0] >= 0.25:
            return best[2]
    return None

def get_category_attributes(category_id):
    response = hb_request("GET", f"{HB_CATALOG_BASE}/product/api/categories/{category_id}/attributes")
    if response.status_code != 200:
        raise RuntimeError(f"HB kategori özellik HTTP {response.status_code}: {response.text[:5000]}")
    body = json_or_fail(response, "HB kategori özellik")
    data = body.get("data") if isinstance(body, dict) else {}
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        result = []
        for key in ("baseAttributes", "attributes", "variantAttributes"):
            values = data.get(key) or []
            if isinstance(values, list):
                result.extend(values)
        return result
    return []


def infer_attribute(name, product):
    key = norm(name)
    source = product.get("attributes") or {}
    aliases = {
        "renk": ("renk", "color"),
        "materyal": ("materyal", "material"),
        "tas": ("tas", "stone"),
        "cinsiyet": ("cinsiyet", "gender"),
        "model": ("model", "model adi", "model adı"),
        "marka": ("marka", "brand"),
    }
    for alias in aliases.get(key, ()):
        val = source.get(alias)
        if val:
            return val
    if key in {"marka", "brand"}:
        return product.get("brand") or None
    text = norm(f"{product.get('title','')} {product.get('description','')}")
    keyword_map = {
        "materyal": ("celik", "çelik", "altin", "gold", "gumus", "silver", "paslanmaz"),
        "cinsiyet": ("kadin", "kadın", "erkek", "unisex"),
        "renk": ("siyah", "gold", "silver", "gumus", "altin", "rose", "beyaz"),
        "tas": ("zirkon", "tas", "taş", "inci"),
    }
    for token in keyword_map.get(key, ()):
        if norm(token) in text:
            return token
    return None


def build_catalog_payload(product, category, attributes):
    merchant_sku = sku_key(product["stockCode"] or product["productCode"] or product["barcode"])
    if not merchant_sku or not product["barcode"] or product.get("price") is None:
        return None, ["merchantSku, barcode veya price"]
    base = {
        "merchantSku": merchant_sku,
        "VaryantGroupID": sku_key(product["productCode"] or merchant_sku),
        "UrunAdi": product["title"][:250],
        "UrunAciklamasi": product["description"],
        "Barcode": product["barcode"],
        "Marka": product["brand"],
        "price": price_hb(product["price"]),
        "stock": str(product["stock"]),
    }
    for i, image in enumerate(product.get("images") or [], 1):
        if i <= 5 and image:
            base[f"Image{i}"] = image
    missing = []
    for attr in attributes:
        name = safe(attr.get("name"))
        if not name:
            continue
        key = norm(name)
        if key in {norm(k) for k in base}:
            continue
        value = infer_attribute(name, product)
        mandatory = bool(attr.get("mandatory") is True or attr.get("isMandatory") is True)
        if value is None and mandatory:
            missing.append(name)
            continue
        if value is not None:
            base[name] = value
    if missing:
        return None, missing
    return [{
        "categoryId": int(category["categoryId"]),
        "merchant": HB_MERCHANT_ID,
        "attributes": base,
    }], []


def import_catalog_file(payload):
    url = f"{HB_CATALOG_BASE}/product/api/products/import"
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    files = {"file": ("products.json", raw, "application/json")}
    response = hb_request("POST", url, files=files)
    log(f"📦 HB katalog ürün import | HTTP {response.status_code} | {len(payload)} ürün")
    body = json_or_fail(response, "HB katalog import")
    if response.status_code not in (200, 201, 202):
        raise RuntimeError(f"HB katalog import HTTP {response.status_code}: {response.text[:5000]}")
    data = body.get("data") if isinstance(body, dict) else body
    if isinstance(data, dict):
        tracking = safe(data.get("trackingId"))
    else:
        tracking = safe(body.get("trackingId")) if isinstance(body, dict) else ""
    if not tracking:
        raise RuntimeError(f"HB katalog import trackingId dönmedi: {body}")
    log(f"🆔 Katalog trackingId: {tracking}")
    return tracking


def catalog_status(tracking_id):
    response = hb_request("GET", f"{HB_CATALOG_BASE}/product/api/products/status/{tracking_id}")
    if response.status_code != 200:
        raise RuntimeError(f"HB katalog status HTTP {response.status_code}: {response.text[:5000]}")
    return json_or_fail(response, "HB katalog status")


def summarize_catalog_status(body):
    data = body.get("data") if isinstance(body, dict) else []
    if isinstance(data, dict):
        data = [data]
    result = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            continue
        result.append({
            "merchantSku": safe(item.get("merchantSku")),
            "hbSku": safe(item.get("hbSku")),
            "productStatus": safe(item.get("productStatus")),
            "importStatus": safe(item.get("importStatus")),
            "taskDetails": item.get("taskDetails"),
            "validationResults": item.get("validationResults"),
            "importMessages": item.get("importMessages"),
        })
    return result


def wait_catalog_status(tracking_id):
    for attempt in range(1, 21):
        time.sleep(3)
        summary = summarize_catalog_status(catalog_status(tracking_id))
        log(f"🔎 Katalog durum {attempt}/20 | {summary}")
        if not summary:
            continue
        import_states = {x["importStatus"].upper() for x in summary if x["importStatus"]}
        if "FAILED" in import_states:
            raise RuntimeError(f"Katalog import FAILED | trackingId={tracking_id} | {summary}")
        terminal = {x["productStatus"].upper() for x in summary if x["productStatus"]}
        if terminal & {"CREATED", "MATCHED", "PRE_MATCHED", "WAITING", "MISSING_INFO", "REJECTED"}:
            return summary
        if "SUCCESS" in import_states:
            return summary
    raise RuntimeError(f"Katalog trackingId 60 saniyede sonuçlanmadı: {tracking_id}")


def load_state():
    if not STATE_FILE.exists():
        return {"products": {}}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("products"), dict):
            return {"products": {}}
        return data
    except Exception:
        return {"products": {}}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def process_existing_products(products, listings, state):
    idx = build_hb_indexes(listings)
    updates = []
    seen = set()
    new_products = []
    for product in products:
        identity = product["identity"]
        if not identity:
            continue
        seen.add(identity)
        row, method = find_hb_match(product, idx)
        entry = state["products"].setdefault(identity, {})
        entry.update({
            "stockCode": product["stockCode"],
            "productCode": product["productCode"],
            "barcode": product["barcode"],
            "title": product["title"],
            "lastPrice": product["price"],
        })
        if row is None:
            entry["missingRuns"] = int(entry.get("missingRuns", 0))
            entry["lastSeen"] = datetime.now().isoformat()
            new_products.append(product)
            continue
        entry["missingRuns"] = 0
        entry["lastSeen"] = datetime.now().isoformat()
        fields = row["fields"]
        hb_sku = safe(fields["hbSku"])
        merchant_sku = safe(fields["merchantSku"]) or sku_key(product["stockCode"] or product["productCode"] or product["barcode"])
        if not hb_sku:
            log(f"⚠️ Eşleşti ama hbSku yok | {product['title']}")
            continue
        entry["hbSku"] = hb_sku
        entry["merchantSku"] = merchant_sku
        entry["lastHbPrice"] = fields["price"]
        current_stock = parse_number(fields["availableStock"])
        target_price = product["price"]
        if target_price is None:
            log(f"⚠️ Trendyol fiyat yok, sadece stok değişecek | {product['title']}")
            target_price = fields["price"]
        stock_changed = current_stock is None or int(current_stock) != product["stock"]
        price_changed = target_price is not None and (fields["price"] is None or abs(float(fields["price"]) - float(target_price)) > 0.005)
        if stock_changed or price_changed:
            if target_price is None:
                log(f"⚠️ HB fiyatı da yok; güvenli biçimde atlandı | {product['title']}")
                continue
            updates.append({
                "hepsiburadaSku": hb_sku,
                "merchantSku": merchant_sku,
                "price": target_price,
                "availableStock": product["stock"],
            })
            log(f"🔄 GÜNCELLE | {product['title']} | Price={target_price:.2f} | Stock={product['stock']} | eşleşme={method}")
    return idx, updates, new_products, seen


def process_new_products(new_products, state):
    if not NEW_PRODUCT_SYNC or not new_products:
        return
    categories = get_categories()
    if not categories:
        for product in new_products:
            identity = product["identity"]
            entry = state["products"].setdefault(identity, {})
            entry["newProductStatus"] = "CATEGORY_SERVICE_UNAVAILABLE"
            log(
                f"⏸️ Yeni ürün bekletildi | kategori servisi kullanılamıyor | "
                f"{product['title']} | barkod={product['barcode']}"
            )
        return
    for product in new_products:
        identity = product["identity"]
        entry = state["products"].setdefault(identity, {})
        if entry.get("trackingId") and entry.get("catalogStatus") not in {"FAILED", "REJECTED"}:
            continue
        category = choose_category(product, categories)
        if not category:
            log(f"⏸️ Yeni ürün gönderilmedi: güvenli kategori eşleşmesi bulunamadı | {product['title']} | kategori={product.get('categoryName')}")
            entry["newProductStatus"] = "CATEGORY_REVIEW_REQUIRED"
            continue
        try:
            attrs = get_category_attributes(category.get("categoryId"))
        except Exception as exc:
            entry["newProductStatus"] = "CATEGORY_ATTRIBUTES_UNAVAILABLE"
            log(f"⏸️ Yeni ürün bekletildi: kategori özellikleri alınamadı | {product['title']} | {exc}")
            continue
        if not category.get("categoryId"):
            log(f"⏸️ Yeni ürün gönderilmedi: kategoriId yok | {product['title']}")
            entry["newProductStatus"] = "CATEGORY_ID_MISSING"
            continue
        payload, missing = build_catalog_payload(product, category, attrs)
        if payload is None:
            log(f"⏸️ Yeni ürün gönderilmedi: zorunlu alan eksik | {product['title']} | {', '.join(missing)}")
            entry["newProductStatus"] = "MANDATORY_FIELDS_REQUIRED"
            entry["missingFields"] = missing
            continue
        try:
            tracking_id = import_catalog_file(payload)
            entry["trackingId"] = tracking_id
            entry["catalogStatus"] = "SUBMITTED"
            summary = wait_catalog_status(tracking_id)
            # Never auto-approve matched products: HB requires merchant review of the matched catalog item.
            statuses = {x["productStatus"].upper() for x in summary if x["productStatus"]}
            if "MATCHED" in statuses or "PRE_MATCHED" in statuses:
                log(f"🟡 Yeni ürün eşleşti; MPOP üzerinden onay/red gerekiyor | {product['title']} | trackingId={tracking_id}")
                entry["newProductStatus"] = "MATCH_REVIEW_REQUIRED"
            else:
                entry["newProductStatus"] = "SUBMITTED"
            for item in summary:
                if item["hbSku"]:
                    entry["hbSku"] = item["hbSku"]
                    break
        except Exception as exc:
            entry["catalogStatus"] = "FAILED"
            log(f"❌ Yeni ürün gönderimi başarısız | {product['title']} | {exc}")


def process_removed_products(idx, state, seen):
    rows = []
    for identity, entry in state.get("products", {}).items():
        if identity in seen:
            continue
        missing_runs = int(entry.get("missingRuns", 0)) + 1
        entry["missingRuns"] = missing_runs
        if missing_runs < MISSING_RUN_THRESHOLD:
            continue
        hb_sku = safe(entry.get("hbSku"))
        merchant_sku = safe(entry.get("merchantSku")) or identity
        if not hb_sku:
            row = idx["merchantSku"].get(merchant_sku) or idx["hbSku"].get(merchant_sku)
            if row:
                hb_sku = safe(row["fields"]["hbSku"])
                merchant_sku = safe(row["fields"]["merchantSku"]) or merchant_sku
        price = parse_number(entry.get("lastHbPrice"))
        if not hb_sku or price is None:
            continue
        rows.append({
            "hepsiburadaSku": hb_sku,
            "merchantSku": merchant_sku,
            "price": price,
            "availableStock": 0,
        })
        entry["lastAction"] = "STOCK_ZEROED"
        log(f"⛔ Trendyol'dan kaldırılmış; HB stok 0 yapılacak | {merchant_sku}")
    return rows


def main():
    print("=" * 78)
    print("DOLUNAY TAKI — TRENDYOL -> HEPSİBURADA TAM SENKRONİZASYON")
    print("=" * 78)
    state = load_state()
    products = get_trendyol_products()
    listings = get_all_hb_listings()
    idx, updates, new_products, seen = process_existing_products(products, listings, state)

    if updates:
        upload_listing_rows(updates)

    process_new_products(new_products, state)

    # Refresh listing data after catalog submissions only on the next run; do not immediately
    # assume a catalog import created a listing. HB states can be WAITING/MATCHED/CREATED.
    removed_updates = process_removed_products(idx, state, seen)
    if removed_updates:
        upload_listing_rows(removed_updates)

    save_state(state)
    log(
        f"📊 SONUÇ | Trendyol={len(products)} | HB listing={len(listings)} | "
        f"güncelleme={len(updates)} | yeni={len(new_products)} | "
        f"stok0={len(removed_updates)}"
    )
    log("✅ TAM SENKRONİZASYON BİTTİ.")


if __name__ == "__main__":
    main()
