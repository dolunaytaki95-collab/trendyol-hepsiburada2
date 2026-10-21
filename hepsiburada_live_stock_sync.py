import os
import requests
from requests.auth import HTTPBasicAuth
import json

ty_supplier_id = os.getenv("TY_SUPPLIER_ID")
ty_api_key = os.getenv("TY_API_KEY")
ty_api_secret = os.getenv("TY_API_SECRET")

hb_merchant_id = os.getenv("HB_MERCHANT_ID")
hb_secret_key = os.getenv("HB_SECRET_KEY")
hb_base_url = os.getenv("HB_LISTING_BASE_URL", "https://listing-external.hepsiburada.com")

def get_trendyol_stock_map():
    """
    Trendyol'daki tüm Onaylı ürünleri çeker ve {Barkod: Stok} haritası oluşturur.
    """
    url = f"https://api.trendyol.com/sapigw/suppliers/{ty_supplier_id}/products?approved=true&size=1000"
    response = requests.get(url, auth=HTTPBasicAuth(ty_api_key, ty_api_secret))
    response.raise_for_status()
    
    products = response.json().get('content', [])
    # Trendyol barkodunu key, stoğunu value yapıyoruz
    stock_map = {}
    for p in products:
        barcode = p.get('barcode')
        if barcode:
            stock_map[barcode] = p.get('quantity', 0)
    return stock_map

def get_hepsiburada_all_skus():
    """
    Hepsiburada'da kayıtlı olan TÜM ürünlerin Stok Kodlarını (merchantSku) çeker.
    """
    url = f"{hb_base_url}/listings/merchantid/{hb_merchant_id}?offset=0&limit=1000"
    headers = {
        "Authorization": f"Bearer {hb_secret_key}",
        "Accept": "application/json"
    }
    
    try:
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            data = response.json()
            listings = data.get('listings', []) or data.get('content', []) or []
            return [item.get('merchantSku') for item in listings if item.get('merchantSku')]
        else:
            print(f"[BİLGİ] HB İlan listesi çekilemedi (Kod: {response.status_code}). Varsayılan moda geçiliyor.")
            return []
    except Exception as e:
        print(f"[BİLGİ] HB İlan listesi çekilirken hata: {e}")
        return []

def sync_stocks():
    print("1. Trendyol stok verileri çekiliyor...")
    ty_stocks = get_trendyol_stock_map()
    print(f"   -> Trendyol'dan {len(ty_stocks)} adet ürün alındı.")

    print("2. Hepsiburada mevcut ilan listesi çekiliyor...")
    hb_skus = get_hepsiburada_all_skus()
    
    update_payload = []

    if hb_skus:
        print(f"   -> Hepsiburada'da {len(hb_skus)} adet ürün bulundu. Karşılaştırma yapılıyor...")
        # Hepsiburada'daki HER BİR ürünü kontrol et
        for sku in hb_skus:
            # Ürün Trendyol'da varsa onun stoğunu al, YOKSA STOĞU 0 YAP!
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
    
    response = requests.post(hb_url, headers=headers, data=json.dumps(update_payload))
    
    if response.status_code in [200, 201, 202]:
        print("✅ BAŞARILI: Güncelleme paketi Hepsiburada'ya iletildi.")
    else:
        print(f"❌ HATA: Kod {response.status_code} - Yanıt: {response.text}")

if __name__ == "__main__":
    sync_stocks()
