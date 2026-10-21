import os
import json
import subprocess
import requests

def get_env(key):
    return str(os.getenv(key, "")).strip()

ty_supplier_id = get_env("TY_SUPPLIER_ID")
ty_api_key = get_env("TY_API_KEY")
ty_api_secret = get_env("TY_API_SECRET")

hb_merchant_id = get_env("HB_MERCHANT_ID")
hb_secret_key = get_env("HB_SECRET_KEY")
hb_base_url = get_env("HB_LISTING_BASE_URL") or "https://listing-external.hepsiburada.com"

def get_trendyol_via_curl():
    print("1. Trendyol stok verileri çekiliyor (CURL Bypass Yöntemi)...")
    if not ty_supplier_id or not ty_api_key:
        raise ValueError("❌ Hata: Trendyol API bilgileri eksik!")

    import base64
    auth_string = f"{ty_api_key}:{ty_api_secret}"
    base64_auth = base64.b64encode(auth_string.encode('utf-8')).decode('utf-8')
    
    stock_map = {}
    page = 0
    size = 200
    
    while True:
        url = f"https://api.trendyol.com/sapigw/suppliers/{ty_supplier_id}/products?approved=true&page={page}&size={size}"
        
        # Python kütüphanelerini ezerek doğrudan işletim sisteminin ağ komutunu (curl) kullanıyoruz
        cmd = [
            "curl", "-s", "-X", "GET", url,
            "-H", f"Authorization: Basic {base64_auth}",
            "-H", f"User-Agent: {ty_supplier_id} - SelfIntegration",
            "-H", "Accept: application/json"
        ]
        
        result = subprocess.run(cmd, capture_output=True, text=True)
        
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            print(f"❌ Beklenmeyen Yanıt (WAF Engeli veya HTML): {result.stdout[:200]}")
            break
            
        products = data.get("content", [])
        if not products:
            break
            
        for p in products:
            barcode = p.get("barcode")
            if barcode:
                stock_map[barcode] = p.get("quantity", 0)
                
        if len(products) < size:
            break
        page += 1
        
    print(f"   -> Trendyol'dan {len(stock_map)} adet ürün başarıyla çekildi.")
    return stock_map

def get_hb_skus():
    skus = []
    offset = 0
    limit = 500
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
    ty_stocks = get_trendyol_via_curl()
    if not ty_stocks:
        print("❌ Hata: Trendyol verisi alınamadığı için senkronizasyon iptal edildi.")
        return

    print("2. Hepsiburada mevcut ilan listesi çekiliyor...")
    hb_skus = get_hb_skus()
    update_payload = []

    if hb_skus:
        print(f"   -> Hepsiburada'da {len(hb_skus)} ürün bulundu. Karşılaştırma yapılıyor...")
        for sku in hb_skus:
            # Trendyol'da stoğu tükenen ürün (haritada yoksa) HB'da 0 yapılır
            target_stock = ty_stocks.get(sku, 0)
            update_payload.append({
                "merchantSku": sku,
                "availableStock": target_stock
            })
    else:
        print("   -> HB listesi çekilemedi, Trendyol'daki ürünler doğrudan gönderiliyor...")
        for barcode, stock in ty_stocks.items():
            update_payload.append({
                "merchantSku": barcode,
                "availableStock": stock
            })

    print(f"3. Toplam {len(update_payload)} ürün Hepsiburada'ya aktarılıyor...")
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
            print(f"✅ BAŞARILI: {len(batch)} adet ürün stoğu Hepsiburada üzerinde güncellendi.")
        else:
            print(f"❌ HB Güncelleme Hatası ({res.status_code}): {res.text}")

if __name__ == "__main__":
    sync()
