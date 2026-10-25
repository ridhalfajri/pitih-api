# Pitih API — Backend & Telegram Bot Worker

Backend REST API dan Telegram Bot Worker untuk aplikasi pencatatan keuangan otomatis **Pitih (MoneyTrack)**.

---

## Fitur Utama
1. **Telegram Receipt Ingestion (`bot.py`)**:
   - Long-polling mendengarkan kiriman foto struk/bukti transfer ke bot Telegram.
   - Menggunakan AI Vision (9router / gemid) untuk parsing otomatis: Bank, Nominal, Tanggal, Pengirim, Merchant, No Ref.
   - Menyimpan transaksi ke SQLite (`keuangan.db`) dan membalas konfirmasi ke user.

2. **REST API Dashboard (`main.py`)**:
   - `GET /api/summary`: Metrik KPI (Pemasukan, Pengeluaran, Saldo Bersih, Total Transaksi).
   - `GET /api/transactions`: Riwayat transaksi lengkap dengan filter bank, jenis, tanggal, dan search.
   - `GET /api/charts/cashflow`: Data tren harian arus kas masuk vs keluar.
   - `GET /api/charts/breakdown-bank`: Proporsi volume per bank/channel.
   - `GET /api/accounts/balances`: Ringkasan saldo per akun rekening.
   - `POST /api/upload-receipt`: Upload manual foto struk langsung dari dashboard web.
   - `PUT /api/transactions/{id}` & `DELETE /api/transactions/{id}`: Edit dan hapus transaksi.
   - `POST /api/transactions/batch-delete`: Hapus transaksi secara massal.
   - `/api/receipts/{filename}`: Static image viewer untuk preview foto struk asli.

---

## Struktur Folder
```
apps/pitih-api/
├── database.py                 # SQLite database query & CRUD operations
├── vision_extractor.py         # AI Vision extraction prompt & client
├── bot.py                      # Telegram bot worker & receipt downloader
├── main.py                     # FastAPI server & route handlers
├── requirements.txt            # Python dependencies
├── Dockerfile                  # Container build file
├── .env.example                # Environment variables reference
└── docker-compose.example.yml  # Docker Compose integration guide
```

---

## Cara Menjalankan

### Opsi A: Via Docker Compose (Rekomendasi bersama `pitih-web`)
Pastikan volume `/opt/data/keuangan` di-mount ke container agar SQLite dan struk tersimpan persisten.
```bash
docker compose up -d --build pitih-api
```

### Opsi B: Menjalankan Langsung dengan Python (Lokal / Host)
```bash
cd apps/pitih-api
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export TELEGRAM_BOT_TOKEN_PITIH="your_token_here"
export DB_PATH="/opt/data/keuangan/keuangan.db"
export RECEIPTS_DIR="/opt/data/keuangan/receipts"
export AI_API_BASE="http://localhost:20128/v1"

uvicorn main:app --host 0.0.0.0 --port 8000
```
