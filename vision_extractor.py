import os
import json
import base64
import re
from typing import Dict, Any, Optional
import httpx

AI_API_BASE = os.getenv("AI_API_BASE", "http://9router:20128/v1")
AI_API_KEY = os.getenv("AI_API_KEY", "sk-9router-local")
AI_MODEL = os.getenv("AI_MODEL", "gemid")

PROMPT = """
Kamu adalah sistem AI OCR dan ekstraksi mutasi bukti transfer/struk perbankan dan e-wallet Indonesia (BCA, Mandiri, BRI, BNI, BSI, Bank Jago, Seabank, GoPay, OVO, Dana, QRIS, dll).

Analisa gambar struk/bukti transfer ini secara teliti dan keluarkan HANYA satu blok JSON valid tanpa markdown formatting tambahan atau pembungkus lain (plain JSON string):
{
  "tanggal": "YYYY-MM-DD HH:MM:SS", (gunakan jam transaksi bila ada, bila tidak ada pakai 12:00:00, bila tahun tidak ada asumsikan 2026)
  "jenis": "keluar" atau "masuk", (default "keluar" jika transfer ke orang lain / pembayaran QRIS / beli barang)
  "kategori": "Makanan & Minuman" | "Operasional" | "Transport" | "Belanja" | "Gaji / Pemasukan" | "Lainnya",
  "nominal": 0.0, (angka float bersih; untuk transaksi "keluar" jika ada Biaya Admin / Biaya Transfer dan belum termasuk di Total, jumlahkan ke nominal agar sesuai potongan saldo rekening)
  "akun": "Nama Bank / E-Wallet", (contoh: "BCA", "Mandiri", "BSI", "Jago", "Seabank", "GoPay", "OVO")
  "merchant": "Nama penerima / pengirim / nama toko merchant",
  "catatan": "Keterangan transaksi singkat atau jenis transaksi misal Pembayaran QRIS",
  "ref_id": "Nomor referensi / No Transaksi / Terminal ID jika tertera di struk",
  "ai_confidence": 0.98
}

PENTING KHUSUS TRANSAKSI QRIS:
- Jika transaksi merupakan pembayaran QRIS, ekstrak nama gerai/toko merchant (misal: "BeClean", "Kopi Kenangan", bukan "-" atau "QRIS").
- Tuliskan rincian barang belanja / makanan pada "catatan" jika tertera di struk (misal: "Cuci sepatu & helm", "2 Kopi Susu").
- Tentukan kategori akurat berdasarkan nama merchant / rincian barang (misal: kafe/makanan -> "Makanan & Minuman", cuci/laundry/minimarket/fashion -> "Belanja").
"""

def image_to_base64(image_path: str) -> str:
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")

async def extract_receipt_data(image_path: str) -> Dict[str, Any]:
    b64_data = image_to_base64(image_path)
    ext = os.path.splitext(image_path)[1].lower().replace(".", "")
    if ext == "jpg":
        ext = "jpeg"
    elif ext not in ["png", "jpeg", "webp"]:
        ext = "jpeg"
        
    mime = f"image/{ext}"
    image_url = f"data:{mime};base64,{b64_data}"
    
    headers = {
        "Authorization": f"Bearer {AI_API_KEY}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "model": AI_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": PROMPT},
                    {
                        "type": "image_url",
                        "image_url": {"url": image_url}
                    }
                ]
            }
        ],
        "temperature": 0.1,
        "stream": False
    }
    
    async with httpx.AsyncClient(timeout=90.0) as client:
        url = f"{AI_API_BASE.rstrip('/')}/chat/completions"
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        
        content_type = resp.headers.get("content-type", "")
        raw_text = ""
        
        if "application/json" in content_type:
            data = resp.json()
            raw_text = data["choices"][0]["message"]["content"].strip()
        else:
            # Fallback for SSE / event-stream
            text_content = resp.text
            chunks = []
            for line in text_content.splitlines():
                line = line.strip()
                if line.startswith("data: ") and line != "data: [DONE]":
                    try:
                        chunk_json = json.loads(line[6:])
                        delta = chunk_json.get("choices", [{}])[0].get("delta", {})
                        if "content" in delta:
                            chunks.append(delta["content"])
                    except Exception:
                        continue
            raw_text = "".join(chunks).strip() or text_content.strip()
        
    print(f"[VisionExtractor] Raw AI output: {raw_text[:200]}...")
    
    # Strip markdown code blocks if present
    clean_str = raw_text
    if "```" in clean_str:
        clean_str = re.sub(r"```(?:json)?\s*", "", clean_str)
        clean_str = clean_str.replace("```", "").strip()
        
    match = re.search(r"\{.*\}", clean_str, re.DOTALL)
    if match:
        json_str = match.group(0)
        parsed = json.loads(json_str)
        return parsed
    else:
        raise ValueError(f"Failed to parse JSON from AI response: {raw_text}")
