import os
import requests
from requests.auth import HTTPBasicAuth
import json

ty_supplier_id = os.getenv("TY_SUPPLIER_ID", "").strip()
ty_api_key = os.getenv("TY_API_KEY", "").strip()
ty_api_secret = os.getenv("TY_API_SECRET", "").strip()

hb_merchant_id = os.getenv("HB_MERCHANT_ID", "").strip()
hb_secret_key = os.getenv("HB_SECRET_KEY", "").strip()
hb_base_url = os.getenv("HB_LISTING_BASE_URL", "https://listing-external.hepsiburada.com").strip()

def get_trendyol_stock_map():
    if not ty_supplier_id or not ty_api_key or not ty_api_secret:
        raise ValueError("❌ HATA: Trendyol API bilgileri Secrets alanında bulunamadı!")

    stock_map = {}
    page = 0
    size = 50  # Cloudflare WAF engeline takılmamak için küçük paket boyutu

    session = requests.Session()
    session.auth = HTTPBasicAuth(ty_api_key, ty_api_secret)

    while True:
        url = f"https://api.trendyol.com/sapigw/suppliers/{ty_supplier_id}/products"
        params = {
            "approved": "true",
            "page": page,
            "size": size
        }

        response = session.get(url, params=params)

        if response.status_code != 200:
            print(f"❌ Trendyol API Hatası! Sayfa: {page}, Kod: {response.status_code}")
            print(f"Yanıt: {response.text[:200]}")
            response.raise_for_status()

        data = response.json()
        products = data.get('content', [])

        if not products:
            break

        for p in products:
            barcode = p.get('barcode')
            if barcode:
                stock_map[barcode] = p.get('quantity', 0)

        total_pages = data.get('totalPages', 0)
        page += 1

        if page >= total_pages or len(products) < size:
            break

    print(f"   -> Trendyol'dan {len(stock_map)} adet ürünün güncel stok verisi çekildi.")
    return stock_map

def get_hepsiburada_all_skus():
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
            response = requests.get(url, headers=headers)
            if response.status_code == 200:
                data = response.json()
                listings = data.get('listings', []) or data.get('content', []) or []
                if not listings:
                    break

                for item in listings:
                    sku = item.get('merchantSku')
                    if sku:
                        skus.append(sku)

                if len(listings) < limit:
                    break
                offset += limit
            else:
                print(f"[BİLGİ] HB ilan listesi çekilemedi (Kod: {response.status_code}).")
                break
        except Exception as e:
            print(f"[BİLGİ] HB ilan listesi çekilirken hata: {e}")
            break

    return skus

def sync_stocks():
    print("1. Trendyol stok verileri çekiliyor...")
    ty_stocks = get_trendyol_stock_map()

    print("2. Hepsiburada mevcut ilan listesi çekiliyor...")
    hb_skus = get_hepsiburada_all_skus()

    update_payload = []

    if hb_skus:
        print(f"   -> Hepsiburada'da {len(hb_skus)} adet ürün bulundu. Karşılaştırma yapılıyor...")
        for sku in hb_skus:
            # Trendyol'da stoğu tükenen veya silinen ürünler HB tarafında 0 yapılır
            target_stock = ty_stocks.get(sku, 0)
            update_payload.append({
                "merchantSku": sku,
                "availableStock": target_stock
            })
    else:
        print("   -> HB ilan listesi çekilemediği için Trendyol listesindeki veriler gönderiliyor...")
        for barcode, stock in ty_stocks.items():
            update_payload.append({
                "merchantSku": barcode,
                "availableStock": stock
            })

    print(f"3. Toplam {len(update_payload)} ürün için Hepsiburada stok güncellemesi gönderiliyor...")

    hb_url = f"{hb_base_url}/listings/merchantid/{hb_merchant_id}/inventory-uploads"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {hb_secret_key}"
    }

    batch_size = 200
    for i in range(0, len(update_payload), batch_size):
        batch = update_payload[i:i + batch_size]
        response = requests.post(hb_url, headers=headers, data=json.dumps(batch))

        if response.status_code in [200, 201, 202]:
            print(f"✅ BAŞARILI: {len(batch)} üründen oluşan stok paketi Hepsiburada'ya iletildi.")
        else:
            print(f"❌ HATA: Kod {response.status_code} - Yanıt: {response.text}")

if __name__ == "__main__":
    sync_stocks()
