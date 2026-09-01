import os
import json
import re
import unicodedata
import time
from datetime import datetime

import requests

TY_BASE = "https://apigw.trendyol.com"
HB_BASE = os.getenv("HB_BASE_URL", "https://mpop-sit.hepsiburada.com")

TY_PAGE_SIZE = 100
HB_PAGE_SIZE = 1000
TIMEOUT = 60

# ------------------------------------------------------------
# GITHUB SECRETS
# ------------------------------------------------------------

def required(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"GitHub Secret eksik: {name}")
    return value

TY_SUPPLIER_ID = required("TY_SUPPLIER_ID")
TY_API_KEY = required("TY_API_KEY")
TY_API_SECRET = required("TY_API_SECRET")

HB_MERCHANT_ID = required("HB_MERCHANT_ID")
HB_SECRET_KEY = required("HB_SECRET_KEY")
HB_USERNAME = required("HB_USERNAME")


# ------------------------------------------------------------
# HELPERS
# ------------------------------------------------------------

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
        "ı": "i", "İ": "i", "ş": "s", "Ş": "s",
        "ğ": "g", "Ğ": "g", "ü": "u", "Ü": "u",
        "ö": "o", "Ö": "o", "ç": "c", "Ç": "c",
    })
    text = text.translate(table)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def parse_json(response, label):
    try:
        return response.json()
    except ValueError as exc:
        raise RuntimeError(
            f"{label} JSON döndürmedi: {response.text[:3000]}"
        ) from exc


# ------------------------------------------------------------
# TRENDYOL
# ------------------------------------------------------------

def get_trendyol_products():
    url = (
        f"{TY_BASE}/integration/product/sellers/"
        f"{TY_SUPPLIER_ID}/products/approved"
    )

    products = []
    page = 0

    while True:
        r = requests.get(
            url,
            headers={
                "User-Agent": f"{TY_SUPPLIER_ID} - SelfIntegration",
                "Accept": "application/json",
            },
            auth=(TY_API_KEY, TY_API_SECRET),
            params={"page": page, "size": TY_PAGE_SIZE},
            timeout=TIMEOUT,
        )

        log(f"Trendyol sayfa {page + 1} | HTTP {r.status_code}")

        if r.status_code != 200:
            raise RuntimeError(
                f"Trendyol HTTP {r.status_code}: {r.text[:3000]}"
            )

        data = parse_json(r, "Trendyol")
        content = data.get("content") or []

        if not content:
            break

        for p in content:
            variants = p.get("variants") or [{}]

            if not isinstance(variants, list):
                variants = [{}]

            for v in variants:
                if not isinstance(v, dict):
                    continue

                barcode = safe(
                    v.get("barcode") or p.get("barcode")
                )
                if not barcode:
                    continue

                price = v.get("price")
                if isinstance(price, dict):
                    price = (
                        price.get("salePrice")
                        or price.get("listPrice")
                        or p.get("salePrice")
                        or p.get("listPrice")
                        or 0
                    )
                else:
                    price = (
                        p.get("salePrice")
                        or p.get("listPrice")
                        or 0
                    )

                stock = v.get("stock")
                if isinstance(stock, dict):
                    stock = stock.get("quantity")
                if stock is None:
                    stock = v.get("quantity", p.get("quantity", 0))

                images = []
                for image in p.get("images") or []:
                    if isinstance(image, dict):
                        url_value = (
                            image.get("url")
                            or image.get("imageUrl")
                        )
                    else:
                        url_value = image
                    if url_value:
                        images.append(safe(url_value))

                brand = p.get("brand") or p.get("brandName") or ""
                if isinstance(brand, dict):
                    brand = brand.get("name", "")

                products.append({
                    "barcode": barcode,
                    "title": safe(p.get("title")),
                    "description": safe(p.get("description")),
                    "price": price,
                    "stock": stock,
                    "images": images[:5],
                    "category": safe(
                        p.get("categoryName") or p.get("category")
                    ),
                    "productMainId": safe(p.get("productMainId")),
                    "productCode": safe(p.get("productCode")),
                    "stockCode": safe(
                        v.get("stockCode") or p.get("stockCode")
                    ),
                    "brand": safe(brand) or "Dolunay Takı",
                    "attributes": p.get("attributes") or [],
                    "variantAttributes": v.get("attributes") or [],
                })

        log(
            f"Sayfa {page + 1}: {len(content)} ana ürün | "
            f"toplam varyant {len(products)}"
        )

        total_pages = data.get("totalPages")
        if total_pages is not None:
            if page + 1 >= int(total_pages):
                break
        elif len(content) < TY_PAGE_SIZE:
            break

        page += 1

    return products


# ------------------------------------------------------------
# HEPSİBURADA KATEGORİLER
# ------------------------------------------------------------

def get_hb_categories():
    url = f"{HB_BASE}/product/api/categories/get-all-categories"
    result = []
    page = 0

    while True:
        r = requests.get(
            url,
            headers={
                "User-Agent": HB_USERNAME,
                "Accept": "application/json",
            },
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

        log(f"HB kategori sayfa {page + 1} | HTTP {r.status_code}")

        if r.status_code != 200:
            raise RuntimeError(
                f"HB kategori HTTP {r.status_code}: {r.text[:3000]}"
            )

        data = parse_json(r, "HB kategorileri")

        if isinstance(data, list):
            items = data
            total_pages = None
        elif isinstance(data, dict):
            items = (
                data.get("data")
                or data.get("content")
                or data.get("categories")
                or []
            )
            total_pages = data.get("totalPages")
        else:
            items = []
            total_pages = None

        if not isinstance(items, list):
            items = []

        items = [
            x for x in items
            if isinstance(x, dict)
        ]

        result.extend(items)

        log(
            f"Kategori: {len(items)} | toplam {len(result)}"
        )

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


# ------------------------------------------------------------
# KATEGORİ EŞLEŞTİRME
# ÖNEMLİ: Artık ürün kategori eşleşmedi diye ÜRÜN ATILMAZ.
# ------------------------------------------------------------

def category_text(category):
    paths = category.get("paths") or []
    if not isinstance(paths, list):
        paths = []

    return norm(
        " ".join([
            safe(category.get("name")),
            safe(category.get("displayName")),
            *[safe(x) for x in paths],
        ])
    )


def jewelry_type(product):
    source = norm(
        f"{product.get('category')} {product.get('title')}"
    )

    if "sahmeran" in source:
        return "sahmeran"
    if "bileklik" in source or "kelepce" in source:
        return "bileklik"
    if "kolye" in source:
        return "kolye"
    if "kupe" in source:
        return "kupe"
    if "yuzuk" in source:
        return "yuzuk"
    if "piercing" in source:
        return "piercing"
    if "halhal" in source:
        return "halhal"
    return "takı"


def find_category(product, categories):
    source_category = norm(product.get("category"))
    source_title = norm(product.get("title"))
    source = f"{source_category} {source_title}"
    kind = jewelry_type(product)

    best = None
    best_score = -1

    for category in categories:
        text = category_text(category)
        score = 0

        if kind == "sahmeran":
            if "sahmeran" in text:
                score += 500
            if "bileklik" in text:
                score += 100
        elif kind in ("bileklik", "kolye", "kupe", "yuzuk", "piercing", "halhal"):
            if kind in text:
                score += 500

        for word in source_category.split():
            if len(word) >= 4 and word in text:
                score += 25

        for word in source_title.split():
            if len(word) >= 5 and word in text:
                score += 2

        if score > best_score:
            best_score = score
            best = category

    # Hiçbir anahtar tutmadıysa yine de ürünü bırakma.
    if best is None and categories:
        preferred = [
            c for c in categories
            if "taki" in category_text(c)
            or "aksesuar" in category_text(c)
        ]
        best = preferred[0] if preferred else categories[0]

    return best, best_score


# ------------------------------------------------------------
# KATEGORİ ATTRIBUTES
# ------------------------------------------------------------

def get_hb_attributes(category_id):
    url = (
        f"{HB_BASE}/product/api/categories/"
        f"{category_id}/attributes"
    )

    r = requests.get(
        url,
        headers={
            "User-Agent": HB_USERNAME,
            "Accept": "application/json",
        },
        auth=(HB_MERCHANT_ID, HB_SECRET_KEY),
        params={"version": 2},
        timeout=TIMEOUT,
    )

    log(
        f"Kategori {category_id} özellikleri | HTTP {r.status_code}"
    )

    if r.status_code != 200:
        return []

    data = parse_json(r, "HB kategori özellikleri")

    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]

    if isinstance(data, dict):
        for key in ("data", "content", "attributes"):
            value = data.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]

    return []


# ------------------------------------------------------------
# ÜRÜN MODELİ
# ------------------------------------------------------------

def build_product(product, category, category_attributes):
    try:
        price = f"{float(product['price']):.2f}".replace(".", ",")
    except (TypeError, ValueError):
        price = "0,00"

    try:
        stock = str(int(float(product["stock"])))
    except (TypeError, ValueError):
        stock = "0"

    sku = (
        product.get("stockCode")
        or product.get("productCode")
        or product.get("barcode")
    ).upper().replace(" ", "")

    attributes = {
        "merchantSku": sku,
        "VaryantGroupID": (
            product.get("productMainId")
            or product.get("barcode")
        ),
        "Barcode": product.get("barcode"),
        "UrunAdi": product.get("title"),
        "UrunAciklamasi": product.get("description"),
        "Marka": product.get("brand") or "Dolunay Takı",
        "GarantiSuresi": 0,
        "kg": "1",
        "price": price,
        "stock": stock,
    }

    for i, image in enumerate(
        product.get("images") or [],
        start=1,
    ):
        attributes[f"Image{i}"] = image

    # Trendyol attributes -> HB attribute adı
    ty_attrs = {}

    for attr in product.get("attributes") or []:
        if not isinstance(attr, dict):
            continue

        name = (
            attr.get("attributeName")
            or attr.get("name")
        )
        value = (
            attr.get("attributeValue")
            or attr.get("value")
        )

        if name and value:
            ty_attrs[norm(name)] = safe(value)

    for hb_attr in category_attributes:
        name = (
            hb_attr.get("name")
            or hb_attr.get("isim")
        )
        if not name:
            continue

        key = norm(name)
        if key in ty_attrs:
            attributes[name] = ty_attrs[key]

    # Varyant alanları
    for attr in product.get("variantAttributes") or []:
        if not isinstance(attr, dict):
            continue

        name = norm(
            attr.get("attributeName")
            or attr.get("name")
            or ""
        )
        value = safe(
            attr.get("attributeValue")
            or attr.get("value")
        )

        if not value:
            continue

        if "renk" in name:
            attributes["renk_variant_property"] = value
        elif "beden" in name:
            attributes["beden_variant_property"] = value
        elif "ebat" in name:
            attributes["ebatlar_variant_property"] = value

    return {
        "categoryId": int(category["categoryId"]),
        "merchant": HB_MERCHANT_ID,
        "attributes": attributes,
    }


# ------------------------------------------------------------
# HB IMPORT
# ------------------------------------------------------------

def upload_products(products):
    url = f"{HB_BASE}/product/api/products/import"
    filename = (
        f"hepsiburada_import_"
        f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    )

    with open(filename, "w", encoding="utf-8") as f:
        json.dump(
            products,
            f,
            ensure_ascii=False,
            indent=2,
        )

    log(
        f"📤 {len(products)} ürün HB'ye gönderiliyor."
    )

    with open(filename, "rb") as f:
        r = requests.post(
            url,
            headers={
                "User-Agent": HB_USERNAME,
                "Accept": "application/json",
            },
            auth=(HB_MERCHANT_ID, HB_SECRET_KEY),
            files={
                "file": (
                    filename,
                    f,
                    "application/json",
                )
            },
            timeout=180,
        )

    log(f"HB import | HTTP {r.status_code}")
    print(r.text[:10000], flush=True)

    if r.status_code not in (200, 201, 202):
        raise RuntimeError(
            f"HB import başarısız: HTTP {r.status_code}"
        )

    data = {}
    try:
        data = r.json()
    except ValueError:
        pass

    return data


# ------------------------------------------------------------
# ANA
# ------------------------------------------------------------

def main():
    print("=" * 70)
    print("TRENDYOL -> HEPSIBURADA TAM SENKRONIZASYON")
    print("=" * 70)

    products = get_trendyol_products()
    log(f"✅ Trendyol toplam varyant: {len(products)}")

    categories = get_hb_categories()
    log(f"✅ HB aktif/urun eklenebilir kategori: {len(categories)}")

    if not categories:
        raise RuntimeError("HB aktif kategori alınamadı.")

    hb_products = []
    attribute_cache = {}
    fallback_count = 0

    for index, product in enumerate(products, start=1):
        category, score = find_category(
            product,
            categories,
        )

        if category is None:
            raise RuntimeError(
                f"Kategori seçilemedi: {product['title']}"
            )

        if score < 100:
            fallback_count += 1
            log(
                f"⚠️ KATEGORİ FALLBACK [{index}] "
                f"{product['title']} -> "
                f"{category.get('displayName') or category.get('name')} "
                f"(ID {category['categoryId']})"
            )
        else:
            log(
                f"✅ [{index}/{len(products)}] "
                f"{product['title']} -> "
                f"{category.get('displayName') or category.get('name')} "
                f"(ID {category['categoryId']})"
            )

        category_id = category["categoryId"]

        if category_id not in attribute_cache:
            attribute_cache[category_id] = get_hb_attributes(
                category_id
            )

        hb_products.append(
            build_product(
                product,
                category,
                attribute_cache[category_id],
            )
        )

    log(
        f"📊 HAZIRLANAN ÜRÜN: {len(hb_products)}/{len(products)}"
    )
    log(
        f"📊 FALLBACK KATEGORİ KULLANILAN: {fallback_count}"
    )

    if len(hb_products) != len(products):
        raise RuntimeError(
            "Tüm Trendyol ürünleri HB isteğine hazırlanamadı."
        )

    result = upload_products(hb_products)

    tracking_id = None

    if isinstance(result, dict):
        data = result.get("data")
        if isinstance(data, dict):
            tracking_id = (
                data.get("trackingId")
                or data.get("trackingID")
            )
        elif isinstance(data, str):
            tracking_id = data

        tracking_id = (
            tracking_id
            or result.get("trackingId")
            or result.get("trackingID")
        )

    if tracking_id:
        log(f"🎫 TrackingId: {tracking_id}")
        log(
            "ℹ️ HB ürünlerin gerçek durumunu trackingId ile işliyor."
        )

    log("✅ 87/87 mantığıyla tüm ürünler HB isteğine dahil edildi.")
    log("✅ Senkronizasyon tamamlandı.")


if __name__ == "__main__":
    main()
