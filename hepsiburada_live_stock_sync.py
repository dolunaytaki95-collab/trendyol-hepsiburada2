import os
import json
import base64
import http.client
import requests

# Ortam değişkenlerini güvenli bir şekilde alıp temizliyoruz
def clean_val(val):
    return str(val or "").strip().strip("'").strip('"')

ty_supplier_id = clean_val(os.getenv("TY_SUPPLIER_ID"))
ty_api_key = clean_val(os.getenv("TY_API_KEY"))
ty_api_secret = clean_val(os.getenv("TY_API_SECRET"))

hb_merchant_id = clean_val(os.getenv("HB_MERCHANT_ID"))
hb_secret_key = clean_val(os.getenv("HB_SECRET_KEY"))
hb_base_url = clean_val(os.getenv("HB_LISTING_BASE_URL")) or "https://listing-external.hepsiburada.com"

def get_trendyol_stocks():
    print("1. Trendyol stok verileri çekiliyor (Raw HTTP Modülü)...")
    if not ty_supplier_id or not ty_api_key:
        raise ValueError("❌ HATA: Trendyol API bilgileri eksik!")

    stock_map = {}
    page = 0
    size = 100
    
    # Kimlik doğrulama şifrelemesi
    auth_str = f"{ty_api_key}:{ty_api_secret}"
    b64_auth = base64.b64encode(auth_str.encode('utf-8')).decode('utf-8')
    
    # Trendyol'un kabul ettiği tam uyumlu başlıklar
    headers = {
        "Authorization": f"Basic {b64_auth}",
        "User-Agent": f"{ty_supplier_id} - SelfIntegration",
        "Accept": "application/json",
        "Connection": "keep-alive"
    }
    
    # Doğrudan IP/Port seviyesinde saf bağlantı açıyoruz (WAF atlatma)
    conn = http.client.HTTPSConnection("api.trendyol.com")
    
    while True:
        path = f"/sapigw/suppliers/{ty_supplier_id}/products?approved=true&page={page}&size={size}"
        
        try:
            conn.request("GET", path, headers=headers)
            res = conn.getresponse()
            raw_data = res.read().decode('utf-8')
            
            if res.status != 200:
                print(f"❌ Trendyol API Hatası: {res.status}")
                print(f"Yanıt: {raw_data[:200]}")
                break
                
            data = json.loads(raw_data)
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
                
        except Exception as e:
            print(f"❌ Bağlantı hatası: {e}")
            break
            
    conn.close()
    print(f"✅ Trendyol'dan {len(stock_map)} adet benzersiz ürün başarıyla çekildi.")
    return stock_map

def get_hepsiburada_skus():
    print("2. Hepsiburada mevcut ilan listesi çekiliyor...")
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
            r = requests.get(url, headers=headers)
            if r.status_code == 200:
                data = r.json()
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

def sync_stocks():
    ty_stocks = get_trendyol_stocks()
    if not ty_stocks:
        print("❌ İşlem iptal edildi (Trendyol verisi alınamadığı için sıfırlama yapılamaz).")
        return
        
    hb_skus = get_hepsiburada_skus()
    update_payload = []
    
    if hb_skus:
        print(f"   -> Hepsiburada'da {len(hb_skus)} ürün bulundu. Stok sıfırlama algoritması çalışıyor...")
        for sku in hb_skus:
            # Trendyol'da olmayan veya stoğu 0 olanlar HB'da 0 olarak işaretleniyor
            target_stock = ty_stocks.get(sku, 0)
            update_payload.append({"merchantSku": sku, "availableStock": target_stock})
    else:
        for barcode, stock in ty_stocks.items():
            update_payload.append({"merchantSku": barcode, "availableStock": stock})
            
    print(f"3. Toplam {len(update_payload)} ürün Hepsiburada'ya gönderiliyor...")
    hb_url = f"{hb_base_url}/listings/merchantid/{hb_merchant_id}/inventory-uploads"
    hb_headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {hb_secret_key}"
    }
    
    batch_size = 200
    for i in range(0, len(update_payload), batch_size):
        batch = update_payload[i:i+batch_size]
        r = requests.post(hb_url, headers=hb_headers, json=batch)
        if r.status_code in [200, 201, 202]:
            print(f"✅ BAŞARILI: Hepsiburada stokları güncellendi. Fazla ürünler satışa kapatıldı.")
        else:
            print(f"❌ HB Güncelleme Hatası ({r.status_code}): {r.text}")

if __name__ == "__main__":
    sync_stocks()
