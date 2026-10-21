import os
import json
import urllib.request
import urllib.parse
import base64
import ssl

# Environment değişkenleri (Boşluk temizlemeli)
ty_supplier_id = os.getenv("TY_SUPPLIER_ID", "").strip()
ty_api_key = os.getenv("TY_API_KEY", "").strip()
ty_api_secret = os.getenv("TY_API_SECRET", "").strip()

hb_merchant_id = os.getenv("HB_MERCHANT_ID", "").strip()
hb_secret_key = os.getenv("HB_SECRET_KEY", "").strip()
hb_base_url = os.getenv("HB_LISTING_BASE_URL", "https://listing-external.hepsiburada.com").strip()

def get_trendyol_stock_map():
    """
    Python'ın dahili 'urllib' kütüphanesi ile Trendyol'dan verileri çeker.
    Requests kütüphanesinin Cloudflare WAF 403 engeline takılmasını önler.
    """
    if not ty_supplier_id or not ty_api_key or not ty_api_secret:
        raise ValueError("❌ HATA: Trendyol API bilgileri (TY_SUPPLIER_ID, TY_API_KEY, TY_API_SECRET) Secrets alanında bulunamadı!")

    stock_map = {}
    page = 0
    size = 100

    # Basic Auth kimlik doğrulama dizesi
    auth_bytes = f"{ty_api_key}:{ty_api_secret}".encode('utf-8')
    base64_auth = base64.b64encode(auth_bytes).decode('utf-8')

    ctx = ssl.create_default_context()

    while True:
        url = f"https://api.trendyol.com/sapigw/suppliers/{ty_supplier_id}/products?approved=true&page={page}&size={size}"
        req = urllib.request.Request(url)
        
        # Cloudflare filtresini geçen başlıklar
        req.add_header("Authorization", f"Basic {base64_auth}")
        req.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
        req.add_header("Accept", "application/json")

        try:
            with urllib.request.urlopen(req, context=ctx) as response:
                if response.status != 200:
                    print(f"❌ Trendyol API Hatası! Sayfa: {page}, Kod: {response.status}")
                    break

                res_body = response.read().decode('utf-8')
                data = json.loads(res_body)
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

        except urllib.error.HTTPError as e:
            print(f"❌ HTTP Hatası ({e.code}): {e.reason}")
            error_body = e.read().decode('utf-8', errors='ignore')
            print(f"Gelen Yanıt: {error_body[:200]}")
            raise e

    print(f"   -> Trendyol'dan {len(stock_map)} adet benzersiz ürünün stok verisi alındı.")
    return stock_map

def get_hepsiburada_all_skus():
    """
    Hepsiburada'da kayıtlı olan tüm ürünlerin Stok Kodlarını (merchantSku) çeker.
    """
    skus = []
    offset = 0
    limit = 200

    ctx = ssl.create_default_context()

    while True:
        url = f"{hb_base_url}/listings/merchantid/{hb_merchant_id}?offset={offset}&limit={limit}"
        req = urllib.request.Request(url)
        req.add_header("Authorization", f"Bearer {hb_secret_key}")
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", "Mozilla/5.0")

        try:
            with urllib.request.urlopen(req, context=ctx) as response:
                if response.status == 200:
                    res_body = response.read().decode('utf-8')
                    data = json.loads(res_body)
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
                    print(f"[BİLGİ] HB ilan listesi çekilemedi (Kod: {response.status}).")
                    break
        except Exception as e:
            print(f"[BİLGİ] HB ilan listesi çekilirken hata: {e}")
            break

    return skus

def sync_stocks():
    print("1. Trendyol stok verileri çekiliyor (urllib entegrasyonu)...")
    ty_stocks = get_trendyol_stock_map()

    print("2. Hepsiburada mevcut ilan listesi çekiliyor...")
    hb_skus = get_hepsiburada_all_skus()

    update_payload = []

    if hb_skus:
        print(f"   -> Hepsiburada'da {len(hb_skus)} adet ürün bulundu. Karşılaştırma yapılıyor...")
        for sku in hb_skus:
            # Trendyol'da bulunmayan veya stoğu tükenen ürün Hepsiburada'da 0 yapılır
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
    json_data = json.dumps(update_payload).encode('utf-8')

    req = urllib.request.Request(hb_url, data=json_data, method='POST')
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {hb_secret_key}")
    req.add_header("User-Agent", "Mozilla/5.0")

    ctx = ssl.create_default_context()

    try:
        with urllib.request.urlopen(req, context=ctx) as response:
            if response.status in [200, 201, 202]:
                print("✅ BAŞARILI: Stok güncelleme paketi Hepsiburada'ya iletildi.")
            else:
                res_body = response.read().decode('utf-8')
                print(f"❌ HATA: Kod {response.status} - Yanıt: {res_body}")
    except urllib.error.HTTPError as e:
        error_body = e.read().decode('utf-8', errors='ignore')
        print(f"❌ Hepsiburada Yükleme Hatası ({e.code}): {error_body}")

if __name__ == "__main__":
    sync_stocks()
