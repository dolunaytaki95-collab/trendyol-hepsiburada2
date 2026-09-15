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


SUPPLIER_ID = required("TY_SUPPLIER_ID")
TY_API_KEY = required("TY_API_KEY")
TY_API_SECRET = required("TY_API_SECRET")
HB_MERCHANT_ID = required("HB_MERCHANT_ID")
HB_SECRET_KEY = required("HB_SECRET_KEY")
HB_USERNAME = required("HB_USERNAME")


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


def _extract_tracking_id(data):
    if not isinstance(data, dict):
        return ""
    candidates = [data]
    nested = data.get("data")
    if isinstance(nested, dict):
        candidates.append(nested)
    for obj in candidates:
        for key in ("trackingId", "trackingID", "tracking_id", "id"):
            value = obj.get(key)
            if value:
                return safe(value)
    return ""


def wait_for_product_import(tracking_id, timeout_seconds=180):
    """Hepsiburada ürün importu async olduğu için stok sorgusundan önce tamamlanmasını bekler."""
    status_urls = [
        f"{HB_BASE}/product/api/products/import/{tracking_id}",
        f"{HB_BASE}/product/api/products/status/{tracking_id}",
    ]

    started = time.time()
    last_error = ""
    used_url_index = 0

    while time.time() - started < timeout_seconds:
        url = status_urls[used_url_index]
        response = requests.get(
            url,
            headers={"User-Agent": HB_USERNAME, "Accept": "application/json"},
            auth=(HB_MERCHANT_ID, HB_SECRET_KEY),
            timeout=TIMEOUT,
        )

        if response.status_code == 404 and used_url_index == 0:
            used_url_index = 1
            continue

        log(f"🔎 HB ürün import durumu | HTTP {response.status_code}")

        if response.status_code != 200:
            last_error = response.text[:2000]
            time.sleep(5)
            continue

        data = json_or_fail(response, "HB ürün import durum sorgusu")
        payload = data.get("data") if isinstance(data, dict) and isinstance(data.get("data"), dict) else data
        status = ""
        if isinstance(payload, dict):
            status = safe(payload.get("status") or payload.get("state") or payload.get("result")).upper()

        log(f"📌 HB ürün import durumu: {status or 'BİLİNMİYOR'}")

        if status in {"DONE", "SUCCESS", "SUCCEEDED", "COMPLETED", "FINISHED"}:
            log("✅ HB ürün importu tamamlandı; stok sorgusuna geçiliyor.")
            return

        if status in {"FAILED", "FAIL", "ERROR", "CANCELLED"}:
            raise RuntimeError(
                "HB ürün importu başarısız: "
                + json.dumps(data, ensure_ascii=False)[:10000]
            )

        time.sleep(5)

    raise RuntimeError(
        f"HB ürün importu {timeout_seconds} saniyede tamamlanmadı. "
        f"Son hata: {last_error[:1000]}"
    )


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

    data = json_or_fail(response, "HB ürün import")
    tracking_id = _extract_tracking_id(data)
    if not tracking_id:
        raise RuntimeError(
            "HB ürün importu kabul edildi fakat trackingId alınamadı."
        )

    log(f"🆔 HB ürün import Tracking ID: {tracking_id}")
    wait_for_product_import(tracking_id)


def _collect_listing_fields(obj):
    """
    Hepsiburada listing yanıtında alanlar farklı seviyelerde/nested gelebilir.
    İlk görülen dolu değerleri topla.
    """
    found = {
        "merchantSku": "",
        "hbSku": "",
        "barcode": "",
        "name": "",
        "availableStock": None,
        "isFulfilledByHB": False,
    }

    merchant_keys = {"merchantsku", "merchantsku", "seller_sku", "sellersku"}
    hb_keys = {"hbsku", "hepsiburadaSku".lower(), "hepsiburada_sku", "hepsiburadaid"}
    barcode_keys = {"barcode", "ean", "ean13", "gtin"}
    name_keys = {"productname", "name", "product_name", "title"}
    stock_keys = {"availablestock", "availablestockinfo", "available_stock", "stock"}

    def walk(x):
        if isinstance(x, dict):
            for raw_k, raw_v in x.items():
                k = safe(raw_k).replace("-", "_").lower()

                if isinstance(raw_v, (str, int, float)) and raw_v not in ("", None):
                    if not found["merchantSku"] and k in merchant_keys:
                        found["merchantSku"] = safe(raw_v)
                    if not found["hbSku"] and k in hb_keys:
                        found["hbSku"] = safe(raw_v)
                    if not found["barcode"] and k in barcode_keys:
                        found["barcode"] = safe(raw_v)
                    if not found["name"] and k in name_keys:
                        found["name"] = safe(raw_v)
                    if found["availableStock"] is None and k in stock_keys:
                        found["availableStock"] = raw_v

                if k in {"isfulfilledbyhb", "is_fulfilled_by_hb"} and raw_v is True:
                    found["isFulfilledByHB"] = True

                walk(raw_v)

        elif isinstance(x, list):
            for item in x:
                walk(item)

    walk(obj)
    return found


def _parse_listing_items(data):
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if not isinstance(data, dict):
        return []
    items = data.get("listings") or data.get("items") or data.get("data") or data.get("content") or []
    if isinstance(items, dict):
        items = items.get("listings") or items.get("items") or items.get("data") or items.get("content") or []
    return [x for x in items if isinstance(x, dict)] if isinstance(items, list) else []


def _listing_fields(item):
    return {
        "hepsiburadaSku": safe(
            item.get("hepsiburadaSku") or item.get("hbSku") or item.get("hepsiburada_sku")
        ),
        "merchantSku": safe(
            item.get("merchantSku") or item.get("merchantSKU") or item.get("sellerSku") or item.get("sellerSKU")
        ),
        "barcode": safe(item.get("barcode") or item.get("ean") or item.get("gtin")),
        "availableStock": item.get("availableStock") if item.get("availableStock") is not None else item.get("stock"),
    }


def get_listing_by_merchant_sku(merchant_sku):
    """Resmi Listing endpointindeki merchantSkuList filtresini kullanır."""
    if not merchant_sku:
        return None

    url = f"{HB_LISTING_BASE}/listings/merchantid/{HB_MERCHANT_ID}"
    response = requests.get(
        url,
        headers={
            "User-Agent": HB_USERNAME,
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        auth=(HB_MERCHANT_ID, HB_SECRET_KEY),
        params={"offset": 0, "limit": 10, "merchantSkuList": merchant_sku},
        timeout=TIMEOUT,
    )

    log(f"🔎 HB listing SKU={merchant_sku} | HTTP {response.status_code}")

    if response.status_code != 200:
        raise RuntimeError(
            f"HB listing sorgusu HTTP {response.status_code}: {response.text[:3000]}"
        )

    items = _parse_listing_items(json_or_fail(response, "HB listing"))
    if not items:
        return None
    return items[0]


def get_hb_listings():
    """Tanı/geri dönüş amacıyla mevcut listingleri sayfalı çeker."""
    url = f"{HB_LISTING_BASE}/listings/merchantid/{HB_MERCHANT_ID}"
    result = []
    offset = 0
    page_size = 100

    for _ in range(100):
        response = requests.get(
            url,
            headers={
                "User-Agent": HB_USERNAME,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            auth=(HB_MERCHANT_ID, HB_SECRET_KEY),
            params={"offset": offset, "limit": page_size},
            timeout=TIMEOUT,
        )
        log(f"HB listing offset={offset} limit={page_size} | HTTP {response.status_code}")
        if response.status_code != 200:
            raise RuntimeError(f"HB listing HTTP {response.status_code}: {response.text[:5000]}")
        items = _parse_listing_items(json_or_fail(response, "HB listing"))
        if not items:
            break
        result.extend(items)
        if len(items) < page_size:
            break
        offset += len(items)
    log(f"✅ HB listing toplamı: {len(result)}")
    return result

def sync_stocks(trendyol_products):
    """Trendyol stoklarını merchantSku filtresiyle bulup HB hepsiburadaSku üzerinden günceller."""
    updates = []
    matched = 0
    unchanged = 0
    missing = 0
    seen_hb = set()

    # Her ürün için önce kendi merchantSku'sunu, sonra ürün kodunu, sonra barkodu deneriz.
    # Listing endpointi resmi olarak merchantSkuList filtresini destekliyor.
    for product in trendyol_products:
        candidates = []
        for candidate in (product.get("stockCode"), product.get("productCode"), product.get("barcode")):
            candidate = safe(candidate)
            if candidate and candidate not in candidates:
                candidates.append(candidate)

        listing = None
        matched_candidate = ""
        for candidate in candidates:
            listing = get_listing_by_merchant_sku(candidate)
            if listing:
                matched_candidate = candidate
                break

        if not listing:
            missing += 1
            log(
                f"⚠️ HB eşleşmedi | {product['title']} | "
                f"denenen={','.join(candidates)}"
            )
            continue

        fields = _listing_fields(listing)
        hb_sku = fields["hepsiburadaSku"]
        if not hb_sku:
            missing += 1
            log(f"⚠️ HB listing bulundu ama hepsiburadaSku yok | {product['title']}")
            continue

        try:
            target_stock = max(0, int(float(product.get("stock") or 0)))
        except (TypeError, ValueError):
            target_stock = 0

        try:
            current_stock = int(float(fields["availableStock"])) if fields["availableStock"] is not None else None
        except (TypeError, ValueError):
            current_stock = None

        matched += 1
        hb_key = hb_sku.upper()
        if hb_key in seen_hb:
            continue
        seen_hb.add(hb_key)

        if current_stock == target_stock:
            unchanged += 1
            log(f"✅ HB stok zaten güncel | {hb_sku} | {current_stock}")
            continue

        updates.append({
            "hepsiburadaSku": hb_sku,
            "availableStock": target_stock,
        })
        log(
            f"🔄 STOK DEĞİŞİMİ | {product['title']} | "
            f"HB={hb_sku} | {current_stock} -> {target_stock} | merchantSku={matched_candidate}"
        )

    log(
        f"📊 Stok eşleşmesi: {matched} | güncellenecek: {len(updates)} | "
        f"aynı: {unchanged} | bulunamayan: {missing}"
    )

    if not updates:
        log("✅ Güncellenecek Hepsiburada stoğu yok.")
        return

    # Resmi Listing bulk inventory upload endpointi.
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

    log(f"📡 HB stok yükleme | HTTP {response.status_code} | {len(updates)} SKU")
    print(response.text[:10000], flush=True)

    if response.status_code not in (200, 201, 202):
        raise RuntimeError(
            f"HB stok yükleme başarısız: HTTP {response.status_code}: {response.text[:3000]}"
        )

    data = json_or_fail(response, "HB stok yükleme")
    upload_id = ""
    if isinstance(data, dict):
        upload_id = safe(
            data.get("inventoryUploadId")
            or data.get("id")
            or (data.get("data") or {}).get("inventoryUploadId")
            or (data.get("data") or {}).get("id")
        )
    if not upload_id:
        raise RuntimeError("HB stok yüklemesi kabul edildi ama inventoryUploadId/id dönmedi.")

    log(f"🆔 HB stok işlem ID: {upload_id}")
    status_url = f"{HB_LISTING_BASE}/listings/merchantid/{HB_MERCHANT_ID}/inventory-uploads/id/{upload_id}"

    for attempt in range(1, 31):
        time.sleep(2)
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
        log(f"🔎 HB stok işlem kontrolü {attempt}/30 | HTTP {status_response.status_code}")
        if status_response.status_code != 200:
            raise RuntimeError(
                f"HB stok işlem sorgusu başarısız: HTTP {status_response.status_code}: {status_response.text[:3000]}"
            )
        result = json_or_fail(status_response, "HB stok işlem sorgusu")
        payload = result.get("data") if isinstance(result, dict) and isinstance(result.get("data"), dict) else result
        status = safe(payload.get("status") or payload.get("state")).upper() if isinstance(payload, dict) else ""
        log(f"📌 HB stok işlem durumu: {status or 'BİLİNMİYOR'}")
        if status in {"SUCCESS", "SUCCEEDED", "COMPLETED", "DONE", "FINISHED"}:
            log("✅ HB stok güncellemesi tamamlandı.")
            return
        if status in {"FAILED", "FAIL", "ERROR", "CANCELLED"}:
            raise RuntimeError("HB stok işlemi başarısız: " + json.dumps(result, ensure_ascii=False)[:10000])

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
