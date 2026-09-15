import os
import json
import re
import unicodedata
import time
from datetime import datetime

import requests

TRENDYOL_BASE = "https://apigw.trendyol.com"
HB_BASE = os.getenv("HB_BASE_URL", "https://mpop-sit.hepsiburada.com").rstrip("/")
HB_LISTING_BASE = os.getenv("HB_LISTING_BASE_URL", "").strip().rstrip("/")
if not HB_LISTING_BASE:
    HB_LISTING_BASE = HB_BASE.replace("mpop-sit.hepsiburada.com", "listing-external-sit.hepsiburada.com").replace("mpop.hepsiburada.com", "listing-external.hepsiburada.com")
TY_PAGE_SIZE = 100
HB_PAGE_SIZE = 1000
TIMEOUT = 60


def required(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"GitHub Secret eksik: {name}")
    return value


def required_any(*names):
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    raise RuntimeError(f"GitHub Secret eksik: {' / '.join(names)}")


SUPPLIER_ID = required_any("TY_SUPPLIER_ID", "TY_TEDARIKCI_ID")
TY_API_KEY = required("TY_API_KEY")
TY_API_SECRET = required("TY_API_SECRET")
HB_MERCHANT_ID = required_any("HB_MERCHANT_ID", "HB_TUCCAR_ID")
HB_SECRET_KEY = required("HB_SECRET_KEY")
HB_USERNAME = os.getenv("HB_USERNAME", "Dolunay-Taki-Sync").strip() or "Dolunay-Taki-Sync"


def log(message):
    print(f"[{datetime.now().strftime('%d.%m.%Y %H:%M:%S')}] {message}", flush=True)


def safe(value):
    return "" if value is None else str(value).strip()


def norm(value):
    text = safe(value).lower()
    table = str.maketrans({
        "ı":"i","ş":"s","ğ":"g","ü":"u","ö":"o","ç":"c",
        "İ":"i","Ş":"s","Ğ":"g","Ü":"u","Ö":"o","Ç":"c",
    })
    text = text.translate(table)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def json_or_fail(response, label):
    try:
        return response.json()
    except ValueError as exc:
        raise RuntimeError(f"{label} JSON döndürmedi: {response.text[:3000]}") from exc


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
            raise RuntimeError(
                f"Trendyol HTTP {response.status_code}: {response.text[:3000]}"
            )

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

                price = (
                    price_data.get("salePrice")
                    if isinstance(price_data, dict)
                    else None
                )
                if price is None and isinstance(price_data, dict):
                    price = price_data.get("listPrice")
                if price is None:
                    price = product.get("salePrice", product.get("listPrice", 0))

                stock = (
                    stock_data.get("quantity")
                    if isinstance(stock_data, dict)
                    else None
                )
                if stock is None:
                    stock = variant.get("quantity", product.get("quantity", 0))

                images = []
                for image in product.get("images") or []:
                    image_url = (
                        image.get("url") or image.get("imageUrl")
                        if isinstance(image, dict)
                        else image
                    )
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

        log(
            f"Sayfa {page + 1}: {len(content)} ana ürün | "
            f"toplam varyant {len(result)}"
        )

        total_pages = data.get("totalPages")
        if total_pages is not None:
            if page + 1 >= int(total_pages):
                break
        elif len(content) < TY_PAGE_SIZE:
            break

        page += 1

    return result


def get_hb_categories():
    url = f"{HB_BASE}/product/api/categories/get-all-categories"
    headers = {"User-Agent": HB_USERNAME, "Accept": "application/json"}
    result = []
    page = 0

    while True:
        response = requests.get(
            url,
            headers=headers,
            auth=(HB_MERCHANT_ID, HB_SECRET_KEY),
            params={
                "leaf": "true",
                "status": "ACTIVE",
                "available": "true",
                "page": page,
                "size": HB_PAGE_SIZE,
            },
            timeout=TIMEOUT,
        )

        log(f"HB kategori sayfa {page + 1} | HTTP {response.status_code}")

        if response.status_code != 200:
            raise RuntimeError(
                f"HB kategori HTTP {response.status_code}: {response.text[:3000]}"
            )

        data = json_or_fail(response, "HB kategori")

        if isinstance(data, list):
            items = data
            total_pages = None
        else:
            items = (
                data.get("data")
                or data.get("content")
                or data.get("categories")
                or []
            )
            total_pages = data.get("totalPages")

        if not isinstance(items, list):
            items = []

        result.extend(x for x in items if isinstance(x, dict))

        log(f"Kategori: {len(items)} | toplam {len(result)}")

        if total_pages is not None:
            if page + 1 >= int(total_pages):
                break
        elif len(items) < HB_PAGE_SIZE:
            break

        page += 1

    return [
        c for c in result
        if c.get("leaf") is True
        and safe(c.get("status")).upper() == "ACTIVE"
        and c.get("available") is True
    ]


def category_text(category):
    paths = category.get("paths") or []
    if not isinstance(paths, list):
        paths = []
    return norm(" ".join([
        safe(category.get("name")),
        safe(category.get("displayName")),
        *[safe(x) for x in paths],
    ]))


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
        score += sum(
            20 for word in norm(product["category"]).split()
            if len(word) >= 4 and word in text
        )
        score += sum(
            3 for word in norm(product["title"]).split()
            if len(word) >= 5 and word in text
        )

        if score > best_score:
            best_score = score
            best = category

    return best


def get_hb_attributes(category_id):
    url = f"{HB_BASE}/product/api/categories/{category_id}/attributes"

    response = requests.get(
        url,
        headers={"User-Agent": HB_USERNAME, "Accept": "application/json"},
        auth=(HB_MERCHANT_ID, HB_SECRET_KEY),
        params={"version": 2},
        timeout=TIMEOUT,
    )

    log(f"Kategori {category_id} özellikleri | HTTP {response.status_code}")

    if response.status_code != 200:
        return []

    data = json_or_fail(response, "HB kategori özellikleri")

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

    try:
        stock = str(int(float(product["stock"])))
    except (TypeError, ValueError):
        stock = "0"

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
        if not name:
            continue
        key = norm(name)
        if key in ty_attrs:
            attributes[name] = ty_attrs[key]

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

    return {
        "categoryId": category["categoryId"],
        "merchant": HB_MERCHANT_ID,
        "attributes": attributes,
    }


def upload_products(products):
    url = f"{HB_BASE}/product/api/products/import"
    filename = f"hepsiburada_import_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"

    with open(filename, "w", encoding="utf-8") as file:
        json.dump(products, file, ensure_ascii=False, indent=2)

    log(f"📄 {len(products)} ürünlük JSON oluşturuldu: {filename}")

    with open(filename, "rb") as file:
        response = requests.post(
            url,
            headers={"User-Agent": HB_USERNAME, "Accept": "application/json"},
            auth=(HB_MERCHANT_ID, HB_SECRET_KEY),
            files={"file": (filename, file, "application/json")},
            timeout=180,
        )

    log(f"📡 HB ürün import | HTTP {response.status_code}")
    print(response.text[:10000], flush=True)

    if response.status_code not in (200, 201, 202):
        raise RuntimeError(
            f"HB import başarısız: HTTP {response.status_code}"
        )



def get_hb_listings():
    """Hepsiburada mevcut listinglerini çeker. Stok güncellemesi için merchantSku -> hepsiburadaSku eşlemesi yapılır."""
    url = f"{HB_LISTING_BASE}/listings/merchantid/{HB_MERCHANT_ID}"
    result = []
    offset = 0

    while True:
        response = requests.get(
            url,
            headers={
                "User-Agent": HB_USERNAME,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            auth=(HB_MERCHANT_ID, HB_SECRET_KEY),
            params={"offset": offset, "limit": HB_PAGE_SIZE},
            timeout=TIMEOUT,
        )

        log(f"HB listing offset={offset} | HTTP {response.status_code}")
        if response.status_code != 200:
            raise RuntimeError(
                f"HB listing HTTP {response.status_code}: {response.text[:5000]}"
            )

        data = json_or_fail(response, "HB listing")
        if isinstance(data, list):
            items = data
            total_count = None
        elif isinstance(data, dict):
            items = data.get("listings") or data.get("items") or data.get("data") or []
            total_count = data.get("totalCount")
            if isinstance(items, dict):
                items = items.get("listings") or items.get("items") or items.get("data") or []
        else:
            items = []
            total_count = None

        if not isinstance(items, list):
            items = []

        result.extend(x for x in items if isinstance(x, dict))

        if not items:
            break
        if total_count is not None and len(result) >= int(total_count):
            break
        if len(items) < HB_PAGE_SIZE:
            break

        offset += len(items)

    log(f"✅ HB listing toplamı: {len(result)}")
    return result


def sku_key(value):
    return re.sub(r"\s+", "", safe(value)).upper()


def sync_stocks(trendyol_products):
    """Trendyol quantity değerlerini Hepsiburada Listing Envanter servisine aktarır."""
    listings = get_hb_listings()

    by_merchant_sku = {}
    by_hb_sku = {}
    for item in listings:
        merchant_sku = sku_key(item.get("merchantSku") or item.get("merchantSKU"))
        hb_sku = safe(item.get("hepsiburadaSku") or item.get("hbSku"))
        if merchant_sku and hb_sku:
            by_merchant_sku[merchant_sku] = item
            by_hb_sku[sku_key(hb_sku)] = item

    updates = []
    matched = 0
    missing = 0
    unchanged = 0

    for product in trendyol_products:
        candidates = [
            product.get("stockCode"),
            product.get("productCode"),
            product.get("barcode"),
        ]
        item = None
        matched_key = ""
        for candidate in candidates:
            key = sku_key(candidate)
            if key and key in by_merchant_sku:
                item = by_merchant_sku[key]
                matched_key = key
                break

        if not item:
            # Bazı listing cevaplarında merchantSku ile birlikte HB SKU aranabilir.
            for candidate in candidates:
                key = sku_key(candidate)
                if key and key in by_hb_sku:
                    item = by_hb_sku[key]
                    matched_key = key
                    break

        if not item:
            missing += 1
            log(f"⚠️ HB listing bulunamadı | {product['title']} | {product.get('barcode')}")
            continue

        matched += 1
        hb_sku = safe(item.get("hepsiburadaSku") or item.get("hbSku"))
        target_stock = int(product.get("stock") or 0)
        current_stock = item.get("availableStock")
        try:
            current_stock = int(float(current_stock))
        except (TypeError, ValueError):
            current_stock = None

        if current_stock == target_stock:
            unchanged += 1
            continue

        updates.append({
            "hepsiburadaSku": hb_sku,
            "merchantSku": safe(item.get("merchantSku")) or safe(matched_key),
            "availableStock": target_stock,
            "maximumPurchasableQuantity": max(1, target_stock) if target_stock > 0 else 0,
        })
        log(f"🔄 STOK DEĞİŞİMİ | {hb_sku} | {current_stock} -> {target_stock}")

    log(f"📊 Stok eşleşmesi: {matched} | güncellenecek: {len(updates)} | aynı: {unchanged} | bulunamayan: {missing}")

    if not updates:
        log("✅ Güncellenecek stok yok.")
        return

    url = f"{HB_LISTING_BASE}/listings/merchantid/{HB_MERCHANT_ID}/inventory-uploads"
    response = requests.post(
        url,
        headers={
            "User-Agent": HB_USERNAME,
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        auth=(HB_MERCHANT_ID, HB_SECRET_KEY),
        json=updates,
        timeout=TIMEOUT,
    )

    log(f"📡 HB stok yükleme | HTTP {response.status_code}")
    print(response.text[:10000], flush=True)

    if response.status_code not in (200, 201, 202):
        raise RuntimeError(f"HB stok yükleme başarısız: HTTP {response.status_code}")

    data = json_or_fail(response, "HB stok yükleme")
    raw = data.get("data") if isinstance(data, dict) else None
    if isinstance(raw, dict):
        upload_id = safe(raw.get("id") or raw.get("inventoryUploadId") or raw.get("uploadId"))
    else:
        upload_id = ""
    if not upload_id and isinstance(data, dict):
        upload_id = safe(data.get("id") or data.get("inventoryUploadId") or data.get("uploadId"))

    if not upload_id:
        raise RuntimeError("HB stok yüklemesi HTTP olarak kabul edildi ancak inventoryUploadId dönmedi.")

    log(f"🆔 HB stok işlem ID: {upload_id}")

    status_url = f"{HB_LISTING_BASE}/listings/merchantid/{HB_MERCHANT_ID}/inventory-uploads/id/{upload_id}"
    for attempt in range(1, 21):
        time.sleep(3)
        status_response = requests.get(
            status_url,
            headers={
                "User-Agent": HB_USERNAME,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            auth=(HB_MERCHANT_ID, HB_SECRET_KEY),
            timeout=TIMEOUT,
        )
        log(f"🔎 HB stok işlem kontrolü {attempt}/20 | HTTP {status_response.status_code}")
        if status_response.status_code != 200:
            raise RuntimeError(f"HB stok işlem sorgusu başarısız: HTTP {status_response.status_code}: {status_response.text[:3000]}")

        status_data = json_or_fail(status_response, "HB stok işlem sorgusu")
        check = status_data.get("data") if isinstance(status_data, dict) else None
        if not isinstance(check, dict):
            check = status_data if isinstance(status_data, dict) else {}
        status = safe(check.get("status") or check.get("state")).upper()
        log(f"📌 HB stok işlem durumu: {status or 'BİLİNMİYOR'}")

        if status in {"SUCCESS", "SUCCEEDED", "COMPLETED", "DONE", "FINISHED"}:
            log("✅ HB stok güncellemesi tamamlandı.")
            return
        if status in {"FAILED", "FAIL", "ERROR"}:
            raise RuntimeError("HB stok işlemi başarısız: " + json.dumps(status_data, ensure_ascii=False)[:10000])

    raise RuntimeError("HB stok işlemi 60 saniye içinde tamamlanmadı.")


def main():
    print("=" * 70)
    print("TRENDYOL -> HEPSİBURADA GITHUB ACTIONS SENKRONİZASYONU")
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

        hb_products.append(
            build_hb_product(
                product,
                category,
                attribute_cache[category_id],
            )
        )

    log(
        f"📊 Sonuç: {len(hb_products)} hazırlanmış, "
        f"{unmatched} kategori eşleşmedi"
    )

    if not hb_products:
        raise RuntimeError("Hiç ürün hazırlanamadı.")

    upload_products(hb_products)

    # Ürün importundan ayrı olarak mevcut HB listing stoklarını güncelle.
    sync_stocks(products)

    log("✅ ÜRÜN + STOK SENKRONİZASYONU TAMAMLANDI.")


if __name__ == "__main__":
    main()
