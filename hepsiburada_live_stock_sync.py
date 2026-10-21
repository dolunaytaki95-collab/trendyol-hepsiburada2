import os
import json
import requests
from requests.auth import HTTPBasicAuth

def clean_val(val):
    """Gizli değişkenlerdeki tırnak ve boşluk karakterlerini temizler."""
    if not val:
        return ""
    return str(val).strip().strip("'").strip('"')

ty_supplier_id = clean_val(os.getenv("TY_SUPPLIER_ID"))
ty_api_key = clean_val(os.getenv("TY_API_KEY"))
ty_api_secret = clean_val(os.getenv("TY_API_SECRET"))

hb_merchant_id = clean_val(os.getenv("HB_MERCHANT_ID"))
hb_secret_key = clean_val(os.getenv("HB_SECRET_KEY"))
hb_base_url = clean_val(os.getenv("HB_LISTING_BASE_URL")) or "https://listing-external.hepsiburada.com"

def get_trendyol_products():
    if not ty_supplier_id or not ty_api_key or not ty_api_secret:
        raise ValueError("❌ HATA: Trendyol API anahtarları GitHub Secrets alanında eksik veya hatalı!")

    stock_map = {}
    page = 0
    size = 100
    url = f"https://api.trendyol.com/sapigw/suppliers/{ty_supplier_id}/products"

    # Cloudflare filtrelerini aşmak için entegrasyon başlığı
    headers = {
        "User-Agent": f"{ty_supplier_id} - SelfIntegration",
        "Accept": "application/json"
    }

    while True:
        params = {"page": page, "size": size}
        res = requests.get(url, params=params, auth=HTTPBasicAuth(ty_api_key, ty_api_secret), headers=headers)
        
        # Cloudflare 403 engeline takılırsa doğrudan sade isteğe düşer
        if res.status_code == 403:
            res = requests.get(url, params=params, auth=HTTPBasicAuth(ty_api_key, ty_api_secret))

        if res.status_code != 200:
            print(f"❌ Trendyol API Hatası! Kod: {res.status_code} - Yanıt: {res.text[:200]}")
            res.raise_for_status()

        data = res.json()
        products = data.get("content", [])
        if not products:
            break

        for p in products:
            barcode = p.get("barcode")
            if barcode:
                stock_map[barcode] = p.get("quantity", 0)

        total_pages = data.get("totalPages", 0)
        page += 1
        if page >= total_pages or len(products) < size:
            break

    print(f"✅ Trendyol'dan {len(stock_map)} adet ürünün stok bilgisi alındı.")
    return stock_map

def get_hepsiburada_skus():
    skus = []
    offset = 0
    limit = 200
    headers = {
        "Authorization": f"Bearer {hb_secret_key}",
        "Accept": "application/json"
    }

    while True:
        url = f"{hb_base_url}/listings/merchantid/{hb_merchant_id}?offset={offset}&limit={limit}"
        try:
            res = requests.get(url, headers=headers)
            if res.status_code == 200:
                data = res.json()
                listings = data.get("listings", []) or data.get("content", []) or []
                if not listings:
                    break
                for item in listings:
                    sku = item.get("merchantSku")
                    if sku:
                        skus.append(sku)
                if len(listings) < limit:
                    break
                offset += limit
            else:
                break
        except Exception:
            break

    return skus

def sync():
    print("1. Trendyol stok verileri çekiliyor...")
    ty_stocks = get_trendyol_products()

    print("2. Hepsiburada envanter listesi çekiliyor...")
    hb_skus = get_hepsiburada_skus()

    update_payload = []

    if hb_skus:
        print(f"   -> Hepsiburada'da {len(hb_skus)} ürün bulundu. Karşılaştırma yapılıyor...")
        for sku in hb_skus:
            # Trendyol'da stoğu tükenen veya silinen ürün Hepsiburada'da 0 yapılır
            target_stock = ty_stocks.get(sku, 0)
            update_payload.append({
                "merchantSku": sku,
                "availableStock": target_stock
            })
    else:
        print("   -> HB ilan listesi çekilemedi, Trendyol stokları doğrudan gönderiliyor...")
        for barcode, stock in ty_stocks.items():
            update_payload.append({
                "merchantSku": barcode,
                "availableStock": stock
            })

    print(f"3. Toplam {len(update_payload)} ürün için Hepsiburada stok güncellemesi gönderiliyor...")
    hb_url = f"{hb_base_url}/listings/merchantid/{hb_merchant_id}/inventory-uploads"
    hb_headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {hb_secret_key}"
    }

    batch_size = 200
    for i in range(0, len(update_payload), batch_size):
        batch = update_payload[i:i + batch_size]
        res = requests.post(hb_url, headers=hb_headers, data=json.dumps(batch))
        if res.status_code in [200, 201, 202]:
            print(f"✅ BAŞARILI: {len(batch)} adet ürün stoğu Hepsiburada'ya aktarıldı.")
        else:
            print(f"❌ Hepsiburada Güncelleme Hatası ({res.status_code}): {res.text}")

if __name__ == "__main__":
    sync()
