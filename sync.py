import os
import json
import re
import time
import unicodedata
from datetime import datetime

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

TRENDYOL_BASE = "https://apigw.trendyol.com"
HB_BASE = os.getenv("HB_BASE_URL", "https://mpop-sit.hepsiburada.com").rstrip("/")
HB_LISTING_BASE = os.getenv(
    "HB_LISTING_BASE_URL",
    "https://listing-external-sit.hepsiburada.com",
).rstrip("/")

TY_PAGE_SIZE = 100
HB_PAGE_SIZE = 1000
TIMEOUT = 60
UPLOAD_POLL_SECONDS = 3
UPLOAD_POLL_ATTEMPTS = 30


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
HB_USER_AGENT = os.getenv("HB_USER_AGENT", "").strip() or os.getenv("HB_USERNAME", "").strip() or "DolunayTaki"


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


def json_or_fail(response, label):
    try:
        return response.json()
    except ValueError as exc:
        raise RuntimeError(f"{label} JSON döndürmedi: {response.text[:3000]}") from exc


def hb_session():
    session = requests.Session()
    retry = Retry(
        total=4,
        connect=4,
        read=4,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST"]),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.headers.update({
        "User-Agent": HB_USER_AGENT,
        "Accept": "application/json",
    })
    session.auth = (HB_MERCHANT_ID, HB_SECRET_KEY)
    return session


def get_trendyol_products():
    url = f"{TRENDYOL_BASE}/integration/product/sellers/{SUPPLIER_ID}/products/approved"
    headers = {
        "User-Agent": f"{SUPPLIER_ID} - SelfIntegration",
        "Accept": "application/json",
    }
    result = []
    page = 0

    while True:
        response = requests.get(
            url,
            headers=headers,
            auth=(TY_API_KEY, TY_API_SECRET),
            params={"page": page, "size": TY_PAGE_SIZE},
            timeout=TIMEOUT,
        )
        log(f"Trendyol sayfa {page + 1} | HTTP {response.status_code}")
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

                price_data = variant.get("price") or {}
                stock_data = variant.get("stock") or {}

                price = None
                if isinstance(price_data, dict):
                    price = price_data.get("salePrice")
                    if price is None:
                        price = price_data.get("listPrice")
                if price is None:
                    price = product.get("salePrice", product.get("listPrice", 0))

                stock = stock_data.get("quantity") if isinstance(stock_data, dict) else None
                if stock is None:
                    stock = variant.get("quantity")
                if stock is None:
                    stock = product.get("quantity", 0)
                try:
                    stock = max(0, int(float(stock)))
                except (TypeError, ValueError):
                    stock = 0

                images = []
                for image in product.get("images") or []:
                    image_url = image.get("url") or image.get("imageUrl") if isinstance(image, dict) else image
                    if image_url:
                        images.append(safe(image_url))

                brand = product.get("brand")
                if isinstance(brand, dict):
                    brand = brand.get("name", "")
                brand = safe(brand or product.get("brandName") or "Dolunay Takı")

                result.append({
                    "barcode": barcode,
                    "title": safe(product.get("title")),
                    "description": safe(product.get("description")),
                    "price": price,
                    "stock": stock,
                    "images": images[:10],
                    "category": safe(product.get("categoryName") or product.get("category")),
                    "productMainId": safe(product.get("productMainId")),
                    "productCode": safe(product.get("productCode")),
                    "stockCode": safe(variant.get("stockCode") or product.get("stockCode")),
                    "brand": brand,
                    "attributes": product.get("attributes") or [],
                    "variantAttributes": variant.get("attributes") or [],
                })

        total_pages = data.get("totalPages")
        if total_pages is not None:
            if page + 1 >= int(total_pages):
                break
        elif len(content) < TY_PAGE_SIZE:
            break
        page += 1

    return result


def hb_get(path, params=None, label="HB"):
    session = hb_session()
    url = f"{HB_LISTING_BASE}{path}" if path.startswith("/listings") else f"{HB_BASE}{path}"
    response = session.get(url, params=params, headers={"Content-Type": "application/json"}, timeout=TIMEOUT)
    log(f"{label} | HTTP {response.status_code}")
    if response.status_code == 401:
        raise RuntimeError(
            "Hepsiburada API 401: MerchantId/SecretKey veya servis yetkilendirmesi reddedildi. "
            "Kod doğru endpoint ve Basic Auth kullanıyor."
        )
    if response.status_code != 200:
        raise RuntimeError(f"{label} HTTP {response.status_code}: {response.text[:5000]}")
    return json_or_fail(response, label)


def hb_post(path, payload, label="HB", listing=True):
    session = hb_session()
    base = HB_LISTING_BASE if listing else HB_BASE
    url = f"{base}{path}"
    response = session.post(
        url,
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=TIMEOUT,
    )
    log(f"{label} | HTTP {response.status_code}")
    if response.status_code == 401:
        raise RuntimeError(
            "Hepsiburada API 401: MerchantId/SecretKey veya servis yetkilendirmesi reddedildi."
        )
    if response.status_code not in (200, 201, 202):
        raise RuntimeError(f"{label} HTTP {response.status_code}: {response.text[:5000]}")
    if not response.text.strip():
        return {}
    return json_or_fail(response, label)


def get_hb_categories():
    result = []
    page = 0
    while True:
        data = hb_get(
            "/product/api/categories/get-all-categories",
            params={"leaf": "true", "status": "ACTIVE", "available": "true", "page": page, "size": HB_PAGE_SIZE},
            label=f"HB kategori sayfa {page + 1}",
        )
        if isinstance(data, list):
            items = data
            total_pages = None
        else:
            items = data.get("data") or data.get("content") or data.get("categories") or []
            total_pages = data.get("totalPages")
        if not isinstance(items, list):
            items = []
        result.extend(x for x in items if isinstance(x, dict))
        if total_pages is not None:
            if page + 1 >= int(total_pages):
                break
        elif len(items) < HB_PAGE_SIZE:
            break
        page += 1
    return [c for c in result if c.get("leaf") is True and safe(c.get("status")).upper() == "ACTIVE" and c.get("available") is True]


def category_text(category):
    paths = category.get("paths") or []
    if not isinstance(paths, list):
        paths = []
    return norm(" ".join([safe(category.get("name")), safe(category.get("displayName")), *[safe(x) for x in paths]]))


def find_category(product, categories):
    source = norm(f"{product['category']} {product['title']}")
    if "bileklik" in source or "kelepce" in source:
        keys = ["bileklik", "kelepce", "sahmeran"]
    elif "kolye" in source:
        keys = ["kolye"]
    elif "kupe" in source:
        keys = ["kupe"]
    elif "yuzuk" in source:
        keys = ["yuzuk"]
    elif "piercing" in source:
        keys = ["piercing"]
    elif "sahmeran" in source:
        keys = ["sahmeran", "bileklik"]
    else:
        keys = []

    best = None
    best_score = -1
    for category in categories:
        text = category_text(category)
        score = sum(100 for key in keys if key in text)
        score += sum(20 for word in norm(product["category"]).split() if len(word) >= 4 and word in text)
        score += sum(3 for word in norm(product["title"]).split() if len(word) >= 5 and word in text)
        if score > best_score:
            best_score = score
            best = category
    return best


def get_hb_attributes(category_id):
    data = hb_get(
        f"/product/api/categories/{category_id}/attributes",
        params={"version": 2},
        label=f"Kategori {category_id} özellikleri",
    )
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ("data", "content", "attributes"):
            value = data.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
    return []


def build_hb_product(product, category, hb_attributes):
    try:
        price = f"{float(product['price']):.2f}".replace(".", ",")
    except (TypeError, ValueError):
        price = "0,00"
    stock = str(int(product["stock"]))
    sku = product["stockCode"] or product["productCode"] or product["barcode"]
    attributes = {
        "merchantSku": sku,
        "VaryantGroupID": product["productMainId"] or sku,
        "Barcode": product["barcode"],
        "UrunAdi": product["title"],
        "UrunAciklamasi": product["description"],
        "Marka": product["brand"],
        "GarantiSuresi": 0,
        "kg": "1",
        "price": price,
        "stock": stock,
    }
    for i, image in enumerate(product["images"], start=1):
        attributes[f"Image{i}"] = image
    ty_attrs = {}
    for attr in product["attributes"]:
        if not isinstance(attr, dict):
            continue
        name = attr.get("attributeName") or attr.get("name")
        value = attr.get("attributeValue") or attr.get("value")
        if name and value:
            ty_attrs[norm(name)] = safe(value)
    for hb_attr in hb_attributes:
        if not isinstance(hb_attr, dict):
            continue
        name = hb_attr.get("name") or hb_attr.get("isim")
        if name and norm(name) in ty_attrs:
            attributes[name] = ty_attrs[norm(name)]
    for attr in product["variantAttributes"]:
        if not isinstance(attr, dict):
            continue
        name = norm(attr.get("attributeName") or attr.get("name") or "")
        value = safe(attr.get("attributeValue") or attr.get("value"))
        if not value:
            continue
        if "renk" in name:
            attributes["renk_variant_property"] = value
        elif "beden" in name:
            attributes["beden_variant_property"] = value
        elif "ebat" in name:
            attributes["ebatlar_variant_property"] = value
    return {"categoryId": category["categoryId"], "merchant": HB_MERCHANT_ID, "attributes": attributes}


def upload_products(products):
    filename = f"hepsiburada_import_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(filename, "w", encoding="utf-8") as file:
        json.dump(products, file, ensure_ascii=False, indent=2)
    session = hb_session()
    response = session.post(
        f"{HB_BASE}/product/api/products/import",
        files={"file": (filename, open(filename, "rb"), "application/json")},
        timeout=180,
    )
    log(f"HB ürün import | HTTP {response.status_code}")
    print(response.text[:10000], flush=True)
    if response.status_code not in (200, 201, 202):
        raise RuntimeError(f"HB import başarısız: HTTP {response.status_code}")


def get_hb_listings():
    result = []
    offset = 0
    while True:
        data = hb_get(
            f"/listings/merchantid/{HB_MERCHANT_ID}",
            params={"offset": offset, "limit": HB_PAGE_SIZE},
            label=f"HB listing offset={offset}",
        )
        items = data.get("listings") if isinstance(data, dict) else data
        if not isinstance(items, list):
            items = data.get("items", []) if isinstance(data, dict) else []
        result.extend(x for x in items if isinstance(x, dict))
        total = data.get("totalCount") if isinstance(data, dict) else None
        log(f"HB listing kayıtları: +{len(items)} | toplam {len(result)}")
        if not items or len(items) < HB_PAGE_SIZE:
            break
        if total is not None and len(result) >= int(total):
            break
        offset += len(items)
    return result


def make_stock_updates(trendyol_products, hb_listings):
    by_merchant_sku = {}
    by_barcode = {}
    for listing in hb_listings:
        ms = sku_key(listing.get("merchantSku"))
        hbsku = safe(listing.get("hepsiburadaSku"))
        barcode = sku_key(listing.get("barcode") or listing.get("Barcode"))
        if ms and hbsku:
            by_merchant_sku[ms] = listing
        if barcode and hbsku:
            by_barcode[barcode] = listing

    updates = []
    matched = 0
    missing = 0
    skipped_hbh = 0
    same = 0
    seen = set()

    for p in trendyol_products:
        candidates = [p.get("stockCode"), p.get("productCode"), p.get("barcode")]
        listing = None
        for c in candidates:
            key = sku_key(c)
            if key and key in by_merchant_sku:
                listing = by_merchant_sku[key]
                break
        if listing is None:
            key = sku_key(p.get("barcode"))
            listing = by_barcode.get(key)

        if listing is None:
            missing += 1
            log(f"⚠️ HB eşleşmedi | {p['title']} | {p.get('stockCode')} | {p.get('barcode')}")
            continue

        matched += 1
        if listing.get("isFulfilledByHB") is True:
            skipped_hbh += 1
            log(f"⏭️ FBH ürün atlandı | HB SKU={listing.get('hepsiburadaSku')}")
            continue

        hbsku = safe(listing.get("hepsiburadaSku"))
        if not hbsku or hbsku in seen:
            continue
        seen.add(hbsku)

        target = int(p["stock"])
        try:
            current = int(float(listing.get("availableStock")))
        except (TypeError, ValueError):
            current = None

        if current == target:
            same += 1
            continue

        updates.append({
            "hepsiburadaSku": hbsku,
            "merchantSku": safe(listing.get("merchantSku")),
            "availableStock": target,
            "maximumPurchasableQuantity": max(1, target),
        })
        log(f"🔄 STOK DEĞİŞİMİ | HB SKU={hbsku} | {current} -> {target}")

    log(f"📊 Eşleşti={matched} | güncellenecek={len(updates)} | aynı={same} | eşleşmedi={missing} | FBH={skipped_hbh}")
    return updates


def wait_upload(upload_id):
    path = f"/listings/merchantid/{HB_MERCHANT_ID}/stock-uploads/id/{upload_id}"
    for attempt in range(1, UPLOAD_POLL_ATTEMPTS + 1):
        time.sleep(UPLOAD_POLL_SECONDS)
        data = hb_get(path, label=f"HB stok işlem kontrolü {attempt}/{UPLOAD_POLL_ATTEMPTS}")
        status = safe(data.get("status")).upper()
        log(f"📌 HB stok işlem durumu: {status or 'BİLİNMİYOR'}")
        if status in {"SUCCESS", "SUCCEEDED", "COMPLETED", "DONE", "FINISHED"}:
            errors = data.get("errors") or []
            if errors:
                log("⚠️ Satır hataları:")
                print(json.dumps(errors, ensure_ascii=False, indent=2), flush=True)
            return
        if status in {"FAILED", "FAIL", "ERROR"}:
            raise RuntimeError("HB stok işlemi başarısız: " + json.dumps(data, ensure_ascii=False)[:10000])
    raise RuntimeError("HB stok işlemi zaman aşımına uğradı.")


def upload_stock(updates):
    if not updates:
        log("✅ Güncellenecek Hepsiburada stoğu yok.")
        return
    if len(updates) > 1000:
        raise RuntimeError("Tek stok yüklemesinde en fazla 1000 SKU gönderilebilir.")
    data = hb_post(
        f"/listings/merchantid/{HB_MERCHANT_ID}/stock-uploads",
        updates,
        label=f"HB stok yükleme ({len(updates)} SKU)",
        listing=True,
    )
    upload_id = safe(data.get("id") or data.get("inventoryUploadId") or data.get("uploadId"))
    if not upload_id:
        raise RuntimeError("HB stok yükleme başarılı görünüyor ancak işlem ID'si dönmedi: " + json.dumps(data, ensure_ascii=False)[:5000])
    log(f"🆔 HB stok işlem ID: {upload_id}")
    wait_upload(upload_id)
    log("✅ HB stok güncellemesi tamamlandı.")


def sync_stocks(products):
    hb_listings = get_hb_listings()
    updates = make_stock_updates(products, hb_listings)
    upload_stock(updates)


def main():
    print("=" * 70)
    print("TRENDYOL -> HEPSİBURADA ÜRÜN + STOK SENKRONİZASYONU")
    print("=" * 70)
    products = get_trendyol_products()
    log(f"✅ Trendyol varyant sayısı: {len(products)}")

    categories = get_hb_categories()
    log(f"✅ HB kategori sayısı: {len(categories)}")

    hb_products = []
    attribute_cache = {}
    unmatched = 0
    for index, product in enumerate(products, start=1):
        log(f"🔄 [{index}/{len(products)}] {product['title']}")
        category = find_category(product, categories)
        if not category:
            unmatched += 1
            log(f"⚠️ Kategori eşleşmedi: {product['category']}")
            continue
        category_id = category["categoryId"]
        if category_id not in attribute_cache:
            attribute_cache[category_id] = get_hb_attributes(category_id)
        hb_products.append(build_hb_product(product, category, attribute_cache[category_id]))

    log(f"📊 Ürün import sonucu: {len(hb_products)} hazırlanmış, {unmatched} kategori eşleşmedi")
    if hb_products:
        upload_products(hb_products)

    # Ürün importundan bağımsız gerçek stok senkronizasyonu.
    sync_stocks(products)
    log("✅ ÜRÜN + STOK SENKRONİZASYONU TAMAMLANDI.")


if __name__ == "__main__":
    main()
