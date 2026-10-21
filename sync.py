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

        for product in content
