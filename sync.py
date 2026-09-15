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
HB_USERNAME = required_any("HB_USERNAME", "HB_KULLANICI_ADI")
HB_PASSWORD = os.getenv("HB_KULLANICI_SIFRE", "").strip() or HB_SECRET_KEY


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




def normalize_id(value):
    """Kimlik alanlarını HB'nin istediği boşluksuz/BÜYÜK HARF biçiminde karşılaştırır."""
    if value is None:
        return ""
    text = safe(value)
    return re.sub(r"\s+", "", text).upper()


def _walk_scalars(obj, path=""):
    """Listing JSON'u nested olsa bile bütün scalar alanları gez."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            child = f"{path}.{key}" if path else str(key)
            yield from _walk_scalars(value, child)
    elif isinstance(obj, list):
        for i, value in enumerate(obj):
            yield from _walk_scalars(value, f"{path}[{i}]")
    else:
        yield path, obj


def _key_norm(key):
    return re.sub(r"[^a-z0-9]", "", safe(key).lower())


def extract_listing_ids(item):
    """Listing içinden merchant SKU / HB SKU / barkod alanlarını güvenli biçimde çıkarır."""
    aliases = {
        "merchantsku", "sellersku", "sku", "hepsiburadaSku".lower(), "hbsku",
        "barcode", "barcodes", "ean", "productbarcode", "productsku",
        "listingid", "merchantproductid"
    }
    values = {"merchant": set(), "hb": set(), "barcode": set()}

    for path, value in _walk_scalars(item):
        key = _key_norm(path.split(".")[-1]).strip("[]0123456789")
        nv = normalize_id(value)
        if not nv:
            continue
        if key in {"merchantsku", "sellersku", "sku", "productsku", "merchantproductid"}:
            values["merchant"].add(nv)
        elif key in {"hepsiburdasku", "hbsku"}:
            values["hb"].add(nv)
        elif key in {"barcode", "barcodes", "ean", "productbarcode"}:
            values["barcode"].add(nv)

    return values


def extract_listing_title(item):
    candidates = []
    preferred = {
        "productname", "producttitle", "title", "name", "urunadi", "productnametr"
    }
    for path, value in _walk_scalars(item):
        key = _key_norm(path.split(".")[-1]).strip("[]0123456789")
        if key in preferred:
            text = safe(value)
            if text:
                candidates.append(text)
    return candidates[0] if candidates else ""


def get_hb_listings():
    """Hepsiburada listinglerini tam ve nested alanları koruyarak çeker."""
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
            auth=(HB_USERNAME, HB_PASSWORD),
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


def find_hb_listing(product, listings):
    """Önce kesin ID eşleşmesi, sonra güvenli tam başlık eşleşmesi yapar."""
    candidates = {
        normalize_id(product.get("stockCode")),
        normalize_id(product.get("productCode")),
        normalize_id(product.get("barcode")),
    }
    candidates.discard("")
    title = norm(product.get("title"))

    # 1) merchantSku / hbSku / barcode üzerinde kesin eşleşme
    exact = []
    for item in listings:
        ids = extract_listing_ids(item)
        all_ids = ids["merchant"] | ids["hb"] | ids["barcode"]
        if candidates & all_ids:
            exact.append(item)

    if len(exact) == 1:
        return exact[0], "ID"
    if len(exact) > 1:
        # Merchant SKU eşleşmesi varsa onu tercih et.
        for item in exact:
            ids = extract_listing_ids(item)
            if candidates & ids["merchant"]:
                return item, "merchantSku"
        return None, "AMBIGUOUS"

    # 2) Tam normalize edilmiş ürün adı: yanlış ürüne stok yazmamak için fuzzy yok.
    if title:
        title_hits = []
        for item in listings:
            item_title = norm(extract_listing_title(item))
            if item_title and item_title == title:
                title_hits.append(item)
        if len(title_hits) == 1:
            return title_hits[0], "TITLE"
        if len(title_hits) > 1:
            return None, "AMBIGUOUS_TITLE"

    return None, "MISSING"


def sync_stocks(trendyol_products):
    """Trendyol quantity değerlerini Hepsiburada Listing Envanter servisine aktarır."""
    listings = get_hb_listings()

    # İlk birkaç listingin gerçek kimlik alanlarını logla; eşleşme sorununu tekrar yaşatmamak için.
    for i, item in enumerate(listings[:5], start=1):
        ids = extract_listing_ids(item)
        title = extract_listing_title(item)
        log(
            f"🔎 HB örnek listing #{i} | merchant={sorted(ids['merchant'])[:3]} | "
            f"hb={sorted(ids['hb'])[:3]} | barcode={sorted(ids['barcode'])[:3]} | "
            f"title={title[:120]}"
        )

    updates = []
    matched = 0
    missing = 0
    unchanged = 0
    ambiguous = 0

    seen_hb_skus = set()

    for product in trendyol_products:
        item, match_type = find_hb_listing(product, listings)

        if not item:
            if match_type.startswith("AMBIGUOUS"):
                ambiguous += 1
                log(f"⚠️ HB listing eşleşmesi belirsiz | {product['title']}")
            else:
                missing += 1
                log(
                    f"⚠️ HB listing bulunamadı | {product['title']} | "
                    f"stockCode={product.get('stockCode')} | "
                    f"productCode={product.get('productCode')} | "
                    f"barcode={product.get('barcode')}"
                )
            continue

        ids = extract_listing_ids(item)
        hb_skus = ids["hb"]
        merchant_skus = ids["merchant"]

        if not hb_skus:
            log(f"⚠️ HB listing bulundu ama hbSku yok | {product['title']}")
            missing += 1
            continue

        hb_sku = sorted(hb_skus)[0]
        if hb_sku in seen_hb_skus:
            continue
        seen_hb_skus.add(hb_sku)

        matched += 1

        current_stock = None
        for path, value in _walk_scalars(item):
            key = _key_norm(path.split(".")[-1]).strip("[]0123456789")
            if key in {"availablestock", "availablequantity", "stock", "quantity", "inventory"}:
                try:
                    current_stock = int(float(value))
                    break
                except (TypeError, ValueError):
                    pass

        try:
            target_stock = max(0, int(float(product.get("stock") or 0)))
        except (TypeError, ValueError):
            target_stock = 0

        if current_stock == target_stock:
            unchanged += 1
            continue

        updates.append({
            "hepsiburadaSku": hb_sku,
            "availableStock": target_stock,
        })
        log(
            f"🔄 STOK DEĞİŞİMİ [{match_type}] | HB={hb_sku} | "
            f"merchant={sorted(merchant_skus)[:1]} | {current_stock} -> {target_stock}"
        )

    log(
        f"📊 Stok eşleşmesi: {matched} | güncellenecek: {len(updates)} | "
        f"aynı: {unchanged} | bulunamayan: {missing} | belirsiz: {ambiguous}"
    )

    if not updates:
        log("✅ Güncellenecek stok yok.")
        return

    if len(updates) > 4000:
        raise RuntimeError("Tek stok yüklemesinde 4000 SKU limiti aşıldı.")

    url = f"{HB_LISTING_BASE}/listings/merchantid/{HB_MERCHANT_ID}/inventory-uploads"
    response = requests.post(
        url,
        headers={
            "User-Agent": HB_USERNAME,
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        auth=(HB_USERNAME, HB_PASSWORD),
        json=updates,
        timeout=TIMEOUT,
    )

    log(f"📡 HB stok yükleme | HTTP {response.status_code}")
    print(response.text[:10000], flush=True)

    if response.status_code not in (200, 201, 202):
        raise RuntimeError(f"HB stok yükleme başarısız: HTTP {response.status_code}")

    data = json_or_fail(response, "HB stok yükleme")
    upload_id = ""
    if isinstance(data, dict):
        raw = data.get("data")
        if isinstance(raw, dict):
            upload_id = safe(raw.get("id") or raw.get("inventoryUploadId") or raw.get("uploadId"))
        if not upload_id:
            upload_id = safe(data.get("id") or data.get("inventoryUploadId") or data.get("uploadId"))

    if not upload_id:
        raise RuntimeError("HB stok yüklemesi kabul edildi ancak inventoryUploadId dönmedi.")

    log(f"🆔 HB stok işlem ID: {upload_id}")

    status_url = f"{HB_LISTING_BASE}/listings/merchantid/{HB_MERCHANT_ID}/inventory-uploads/id/{upload_id}"
    last_status = ""
    for attempt in range(1, 31):
        time.sleep(2)
        status_response = requests.get(
            status_url,
            headers={
                "User-Agent": HB_USERNAME,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            auth=(HB_USERNAME, HB_PASSWORD),
            timeout=TIMEOUT,
        )
        log(f"🔎 HB stok işlem kontrolü {attempt}/30 | HTTP {status_response.status_code}")
        if status_response.status_code != 200:
            raise RuntimeError(
                f"HB stok işlem sorgusu başarısız: HTTP {status_response.status_code}: "
                f"{status_response.text[:3000]}"
            )

        status_data = json_or_fail(status_response, "HB stok işlem sorgusu")
        check = status_data.get("data") if isinstance(status_data, dict) else None
        if not isinstance(check, dict):
            check = status_data if isinstance(status_data, dict) else {}
        status = safe(check.get("status") or check.get("state")).upper()
        last_status = status
        log(f"📌 HB stok işlem durumu: {status or 'BİLİNMİYOR'}")

        if status in {"SUCCESS", "SUCCEEDED", "COMPLETED", "DONE", "FINISHED"}:
            errors = check.get("errors") or status_data.get("errors") if isinstance(status_data, dict) else []
            if errors:
                print("⚠️ HB stok satır hataları:\n" + json.dumps(errors, ensure_ascii=False, indent=2)[:10000], flush=True)
            log("✅ HB stok güncellemesi tamamlandı.")
            return
        if status in {"FAILED", "FAIL", "ERROR"}:
            raise RuntimeError(
                "HB stok işlemi başarısız: " + json.dumps(status_data, ensure_ascii=False)[:10000]
            )

    raise RuntimeError(f"HB stok işlemi 60 saniye içinde tamamlanmadı. Son durum: {last_status or 'BİLİNMİYOR'}")

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
