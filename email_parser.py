import os
import re
import json
import html
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Dict, Any, Optional
import httpx

from vision_extractor import AI_API_BASE, AI_API_KEY, AI_MODEL

EMAIL_AI_PROMPT = """
Kamu adalah sistem AI ekstraksi mutasi transaksi dari email notifikasi perbankan dan e-wallet Indonesia (BCA, Mandiri, BRI, BNI, BSI / BYOND, Bank Jago, Seabank, GoPay, OVO, Dana, QRIS, dll).

Analisa pengirim, subjek, dan isi teks email di bawah ini. Tentukan apakah email ini merupakan notifikasi transaksi/mutasi keuangan yang sah (bukan email promo, iklan, newsletter, atau OTP login).

PENTING KHUSUS TRANSAKSI QRIS:
- Jika transaksi adalah pembayaran QRIS (seperti QRIS BCA/myBCA, BSI, Mandiri, dll), pastikan field "merchant" diisi dengan nama gerai/toko merchant (misal: "BeClean", "Kopi Kenangan", bukan "-" atau "QRIS").
- Ekstrak lokasi toko/merchant jika ada (contoh: "JAKARTA TIMUR").
- Tuliskan rincian barang/keperluan pada "catatan" bila tertera, atau format catatan informatif: "Pembayaran QRIS ke [Merchant] ([Lokasi])".
- Jangan mengkategorikan transaksi QRIS sebagai "Tagihan & Utilitas" hanya karena subjek email mengandung "Internet Transaction Journal". Tentukan kategori berdasarkan jenis usaha merchant (misal laundry/retail -> "Belanja & Retail", makanan/cafe -> "Makanan & Minuman").

Keluarkan HANYA satu blok JSON valid tanpa markdown tambahan:
{
  "is_transaction": true,
  "tanggal": "YYYY-MM-DD HH:MM:SS",
  "jenis": "keluar" atau "masuk",
  "kategori": "Makanan & Minuman" | "Belanja & Retail" | "Transportasi" | "Tagihan & Utilitas" | "Transfer & Top Up" | "Client Payment" | "Vendor/Operasional" | "Server Subscriptions" | "Gaji & Payroll" | "Refund" | "Lainnya",
  "nominal": 0.0,
  "akun": "BCA" | "Mandiri" | "BSI (BYOND)" | "BRI" | "BNI" | "Jago" | "Seabank" | "GoPay" | "OVO" | "QRIS",
  "merchant": "Nama penerima / pengirim / merchant",
  "catatan": "Keterangan singkat jenis transaksi",
  "ref_id": "Nomor referensi / ID transaksi jika ada atau -",
  "saldo_akhir": null,
  "ai_confidence": 0.96
}
Jika bukan email transaksi finansial (misal iklan/promo/OTP), kembalikan {"is_transaction": false, "nominal": 0}.
"""


def strip_html_to_text(raw_html_or_text: str) -> str:
    if not raw_html_or_text:
        return ""
    text = raw_html_or_text
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", text)
    text = re.sub(r"(?i)<\s*(?:br\s*/?|/p|/div|/tr|/li|/h[1-6])\s*>", "\n", text)
    text = re.sub(r"(?i)<\s*td[^>]*>", " : ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    lines = []
    for line in text.splitlines():
        cleaned = re.sub(r"[ \t\xa0]+", " ", line).strip()
        cleaned = re.sub(r"^(?::\s*)+", "", cleaned).strip()
        if cleaned:
            lines.append(cleaned)
    return "\n".join(lines)


def mask_sensitive_text(text: str, max_len: int = 200) -> str:
    if not text:
        return ""
    clean = re.sub(r"\s+", " ", text).strip()
    masked = re.sub(r"\b(\d{2})\d{4,10}(\d{2})\b", r"\1****\2", clean)
    if len(masked) > max_len:
        return masked[: max_len - 1] + "…"
    return masked


def parse_indonesian_amount(raw: str) -> Optional[float]:
    if not raw:
        return None
    s = re.sub(r"(?i)(?:rp\.?|idr|\s|\+|-)", "", str(raw)).strip()
    s = re.sub(r"[^0-9.,]", "", s)
    if not s:
        return None

    # Format: 1.250.000,00 or 75.900,50
    if re.search(r",\d{1,2}$", s):
        parts = s.rsplit(",", 1)
        integer_part = parts[0].replace(".", "").replace(",", "")
        decimal_part = parts[1]
        s = f"{integer_part}.{decimal_part}"
    # Format: 1,250,000.00
    elif re.search(r"\.\d{1,2}$", s) and "," in s:
        s = s.replace(",", "")
    # Format: 1.250.000 or 75.900 (dots as thousand separators)
    elif "." in s and "," not in s:
        dot_parts = s.split(".")
        if len(dot_parts) > 2 or (len(dot_parts) == 2 and len(dot_parts[1]) == 3):
            s = s.replace(".", "")
    # Format: 1,250,000 (commas as thousand separators)
    elif "," in s and "." not in s:
        comma_parts = s.split(",")
        if len(comma_parts) > 2 or (len(comma_parts) == 2 and len(comma_parts[1]) == 3):
            s = s.replace(",", "")
        else:
            s = s.replace(",", ".")

    try:
        val = float(s)
        return val if val > 0 else None
    except ValueError:
        return None


def normalize_email_datetime(email_date: Optional[str] = None, body_text: str = "") -> str:
    # Try parsing explicit date pattern inside body text first if present (e.g. 25/10/2026 14:30:12 or 2026-10-25 14:30)
    iso_match = re.search(r"\b(20\d{2}-\d{2}-\d{2})[ T](\d{2}:\d{2}(?::\d{2})?)\b", body_text)
    if iso_match:
        d_part, t_part = iso_match.group(1), iso_match.group(2)
        if len(t_part) == 5:
            t_part += ":00"
        return f"{d_part} {t_part}"

    dmy_match = re.search(
        r"\b(\d{1,2})[/\-](\d{1,2})[/\-](20\d{2})\s+(?:pukul\s+|jam\s+)?(\d{1,2}:\d{2}(?::\d{2})?)",
        body_text,
        re.IGNORECASE,
    )
    if dmy_match:
        day, month, year, t_part = (
            int(dmy_match.group(1)),
            int(dmy_match.group(2)),
            int(dmy_match.group(3)),
            dmy_match.group(4),
        )
        t_parts = t_part.split(":")
        hh = int(t_parts[0])
        mm = int(t_parts[1])
        ss = int(t_parts[2]) if len(t_parts) > 2 else 0
        if 1 <= month <= 12 and 1 <= day <= 31:
            return f"{year:04d}-{month:02d}-{day:02d} {hh:02d}:{mm:02d}:{ss:02d}"

    if email_date:
        trimmed = email_date.strip()
        if re.match(r"^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}$", trimmed):
            return trimmed
        if re.match(r"^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}$", trimmed):
            return f"{trimmed}:00"
        try:
            dt = parsedate_to_datetime(trimmed)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            try:
                dt = datetime.fromisoformat(trimmed.replace("Z", "+00:00"))
                return dt.strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                pass

    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def detect_gmail_forwarding_verification(
    sender: str, subject: str, body_text: str
) -> Optional[Dict[str, Optional[str]]]:
    s_low = (sender or "").lower()
    subj_low = (subject or "").lower()
    body_low = (body_text or "").lower()
    combined = f"{subject}\n{body_text}"

    is_forwarding_mail = (
        "forwarding-noreply@google.com" in s_low
        or "forwarding-noreply@google.com" in body_low
        or (
            ("forwarding" in subj_low or "penerusan" in subj_low)
            and (
                "gmail" in subj_low
                or "confirmation" in subj_low
                or "konfirmasi" in subj_low
            )
        )
    )
    if not is_forwarding_mail:
        return None

    code_match = re.search(
        r"(?:Confirmation\s*code|Kode\s*konfirmasi|kode\s*verifikasi|#)\s*[:#]?\s*(\d{7,10})",
        combined,
        re.IGNORECASE,
    )
    url_match = re.search(
        r"(https://(?:mail(?:-settings)?|isolated\.mail)\.google\.com/mail/vf-[^\s\"<>]+)",
        combined,
        re.IGNORECASE,
    )
    code = code_match.group(1) if code_match else None
    url = url_match.group(1) if url_match else None
    if code or url:
        return {"code": code, "url": url}
    return None


def infer_category(merchant: str, catatan: str, jenis: str) -> str:
    combined = f"{merchant or ''} {catatan or ''}".lower()
    # Strip generic bank transaction phrases that cause false positive category matches
    combined = re.sub(r"\binternet\s+transaction\s+journal\b", "", combined)
    combined = re.sub(r"\binternet\s+banking\b", "", combined)
    combined = re.sub(r"\s+", " ", combined).strip()

    if (jenis or "keluar").lower() == "masuk":
        if any(k in combined for k in ("gaji", "payroll", "salary", "tunjangan")):
            return "Gaji & Payroll"
        if any(k in combined for k in ("refund", "cashback", "pengembalian")):
            return "Refund"
        return "Client Payment"

    if any(
        k in combined
        for k in (
            "kopi",
            "coffee",
            "chuseyo",
            "fore",
            "kenangan",
            "janji jiwa",
            "starbucks",
            "resto",
            "warung",
            "cafe",
            "food",
            "gofood",
            "grabfood",
            "shopeefood",
            "pecel",
            "makan",
            "minum",
            "bakso",
            "nasi",
            "ayam",
            "sate",
            "padang",
            "bakery",
            "kitchen",
            "toast",
            "tea",
            "mie",
            "bebek",
            "snack",
            "martabak",
            "gorengan",
            "boba",
            "roti",
            "cake",
            "burger",
            "pizza",
            "soto",
            "dapur",
            "kuliner",
        )
    ):
        return "Makanan & Minuman"
    if any(
        k in combined
        for k in (
            "grab",
            "gojek",
            "goride",
            "gocar",
            "pertamina",
            "shell",
            "parkir",
            "tol",
            "flazz",
            "e-money",
            "kai",
            "kereta",
            "traveloka",
            "tiket",
            "bluebird",
            "bensin",
            "transport",
        )
    ):
        return "Transportasi"
    if any(
        k in combined
        for k in (
            "pln",
            "listrik",
            "telkom",
            "indihome",
            "telkomsel",
            "xl",
            "indosat",
            "by.u",
            "pdam",
            "bpjs",
            "pulsa",
            "paket data",
            "tagihan",
            "wifi",
            "biznet",
            "myrepublic",
            "firstmedia",
            "internet",
        )
    ):
        return "Tagihan & Utilitas"
    if any(
        k in combined
        for k in (
            "aws",
            "cloud",
            "cloudflare",
            "digitalocean",
            "vercel",
            "github",
            "openai",
            "anthropic",
            "hostinger",
            "niagahoster",
            "domain",
            "server",
            "vps",
        )
    ):
        return "Server Subscriptions"
    if any(
        k in combined
        for k in (
            "tokopedia",
            "shopee",
            "lazada",
            "blibli",
            "alfamart",
            "indomaret",
            "superindo",
            "hypermart",
            "mall",
            "mart",
            "toko",
            "store",
            "shop",
            "market",
            "swalayan",
            "kelontong",
            "belanja",
            "uniqlo",
            "ikea",
            "clean",
            "laundry",
            "cuci",
            "londri",
            "wash",
            "setrika",
            "fashion",
            "boutique",
            "distro",
            "optik",
            "apotek",
            "pharmacy",
        )
    ):
        return "Belanja & Retail"
    if any(
        k in combined
        for k in (
            "transfer",
            "bi-fast",
            "bifast",
            "top up",
            "topup",
            "isi saldo",
            "virtual account",
            "pindah dana",
        )
    ):
        return "Transfer & Top Up"
    return "Lainnya"


def _detect_bank_account(sender: str, subject: str, body_text: str) -> Optional[str]:
    # Also inspect original From header inside forwarded message body if present
    fwd_from_match = re.search(
        r"(?:From|Dari)\s*:\s*([^\n\r]+)", body_text, re.IGNORECASE
    )
    fwd_from = fwd_from_match.group(1) if fwd_from_match else ""
    probe = f"{sender}\n{fwd_from}\n{subject}\n{body_text[:1000]}".lower()

    if any(k in probe for k in ("bankbsi.co.id", "byond", "bank syariah indonesia", "bsi mobile", "notifikasi bsi")):
        return "BSI"
    if any(k in probe for k in ("bca.co.id", "klikbca", "mybca", "bca mobile", "m-bca", "tahapan bca", "bank central asia")):
        return "BCA"
    if any(k in probe for k in ("bankmandiri.co.id", "mandiricare", "livin", "bank mandiri")):
        return "Mandiri"
    if any(k in probe for k in ("bri.co.id", "brimo", "bank rakyat indonesia")):
        return "BRI"
    if any(k in probe for k in ("bni.co.id", "wondr", "bank negara indonesia")):
        return "BNI"
    if any(k in probe for k in ("jago.com", "bank jago", "kantong jago")):
        return "Jago"
    if any(k in probe for k in ("seabank.co.id", "seabank")):
        return "Seabank"
    if any(k in probe for k in ("gopay", "gojek.com")):
        return "GoPay"
    if any(k in probe for k in ("ovo.id", "notifikasi ovo", "pembayaran ovo")):
        return "OVO"
    return None


def parse_bank_email_deterministic(
    sender: str,
    subject: str,
    body_text: str,
    email_date: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    akun = _detect_bank_account(sender, subject, body_text)
    if not akun:
        return None

    combined = f"{subject}\n{body_text}"
    combined_low = combined.lower()

    # Ignore non-transaction security/OTP/login alert emails
    if any(
        ignore_kw in combined_low
        for ignore_kw in (
            "kode otp",
            "one time password",
            "jangan berikan kode",
            "verifikasi login",
            "aktivasi perangkat",
            "ubah password",
            "reset password",
        )
    ) and not any(
        tx_kw in combined_low
        for tx_kw in ("nominal", "sejumlah", "total bayar", "jumlah", "berhasil", "sukses")
    ):
        return None

    # 1. Extract Nominal (prefer Total Bayar / Total Transaksi first if present, else Nominal + Biaya Admin)
    nominal: Optional[float] = None
    used_total_field = False
    total_match = re.search(
        r"(?:Total\s+Payment|Total\s+Bayar|Total\s+Transaksi|Total\s+Pembayaran|Total\s+Debet)\s*(?:[:\-=]\s*|\n\s*)(?:Rp\.?|IDR)?\s*([0-9][0-9.,]{2,})",
        combined,
        re.IGNORECASE,
    )
    if total_match:
        parsed_tot = parse_indonesian_amount(total_match.group(1))
        if parsed_tot and parsed_tot >= 100:
            nominal = parsed_tot
            used_total_field = True

    if not nominal:
        amount_patterns = [
            r"(?:Nominal(?:\s+Transaksi|\s+Transfer|\s+Pembayaran)?|Sejumlah|Jumlah(?:\s+Transfer|\s+Pembayaran)?|Amount|Nilai\s+Transaksi|Total)\s*(?:[:\-=]\s*|\n\s*)(?:Rp\.?|IDR)?\s*([0-9][0-9.,]{2,})",
            r"(?: sebesar| sejumlah| senilai)\s*(?:Rp\.?|IDR)\s*([0-9][0-9.,]{2,})",
            r"(?:Rp\.?|IDR)\s*([0-9]{1,3}(?:\.[0-9]{3})+(?:,[0-9]{2})?|[0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]{2})?|[0-9]{4,})",
        ]
        for pat in amount_patterns:
            m = re.search(pat, combined, re.IGNORECASE)
            if m:
                parsed_amt = parse_indonesian_amount(m.group(1))
                if parsed_amt and parsed_amt >= 100:
                    nominal = parsed_amt
                    break

    if not nominal or nominal <= 0:
        return None
    # 2. Determine Jenis ('masuk' vs 'keluar')
    jenis = "keluar"
    masuk_indicators = (
        "transfer masuk",
        "dana masuk",
        "uang masuk",
        "terima uang",
        "menerima transfer",
        "penerimaan dana",
        "kredit",
        "credit",
        "top up berhasil masuk",
        "pencairan",
        "refund",
        "cashback masuk",
    )
    keluar_indicators = (
        "pembayaran qris",
        "pembayaran berhasil",
        "transfer berhasil",
        "transfer ke",
        "pembelian",
        "tarik tunai",
        "debet",
        "debit",
        "uang keluar",
        "dana keluar",
        "top up e-wallet",
    )
    if any(ind in combined_low for ind in masuk_indicators) and not any(
        k in combined_low for k in ("pembayaran qris", "transfer ke rekening", "jenis transaksi : debet")
    ):
        jenis = "masuk"
    elif any(ind in combined_low for ind in keluar_indicators):
        jenis = "keluar"

    # 3. Extract Merchant / Counterparty
    merchant = "-"
    if jenis == "masuk":
        merchant_patterns = [
            r"(?:Sender\s+Name|Nama\s+Pengirim|Pengirim|Dari\s+Rekening|Dari|Sumber\s+Dana|Sender)\s*(?:[:\-=]\s*|\n\s*)([^\n\r|]+)",
            r"(?:Rekening\s+Pengirim)\s*(?:[:\-=]\s*|\n\s*)([^\n\r|]+)",
            r"(?:Rekening\s+Penerima|Penerima|Merchant|Tujuan)\s*(?:[:\-=]\s*|\n\s*)([^\n\r|]+)",
        ]
    else:
        merchant_patterns = [
            r"(?:Beneficiary\s+Name|Nama\s+Penerima|Nama\s+Merchant|Merchant\s+Name|Nama\s+Tujuan|Nama\s+Beneficiary|Recipient\s+Name|Payment\s+[Tt]o|Payment\s+[Ff]or|Bayar\s+[Kk]e|Pembayaran\s+[Kk]e)\s*(?:[:\-=]\s*|\n\s*)([^\n\r|]+(?:\n\s*Bank\s+[^\n\r|]+)?)",
            r"(?:Tujuan(?:\s+Transfer)?|Penerima|Ke\s+Rekening|Kepada|Penyedia\s+Jasa|Biller|Merchant)\s*(?:[:\-=]\s*|\n\s*)([^\n\r|]+(?:\n\s*Bank\s+[^\n\r|]+)?)",
            r"(?:Dari\s+Pengirim|Pengirim)\s*(?:[:\-=]\s*|\n\s*)([^\n\r|]+)",
        ]
    for pat in merchant_patterns:
        m = re.search(pat, body_text, re.IGNORECASE)
        if m:
            lines = [re.sub(r"\s+", " ", ln).strip(" :.-") for ln in m.group(1).splitlines() if ln.strip()]
            if lines:
                primary = re.sub(r"\s*[-/]\s*\d{6,}.*$", "", lines[0]).strip()
                if len(lines) > 1 and "bank" in lines[1].lower():
                    bank_dest = re.sub(r"\s*[-/]\s*\d{6,}.*$", "", lines[1]).strip()
                    candidate = f"{primary} ({bank_dest})"
                else:
                    candidate = primary
                if (
                    candidate
                    and len(candidate) >= 2
                    and not re.match(r"^\d+$", candidate)
                    and candidate.lower() not in ("rp", "idr", "berhasil", "sukses")
                ):
                    merchant = candidate[:80]
                    break
    # 4. Extract Reference ID
    ref_id = "-"
    ref_patterns = [
        r"(?:No\.?\s*Referensi|Nomor\s*Referensi|Reference\s*No\.?|Ref\s*ID|No\.?\s*Transaksi|ID\s*Transaksi|No\.?\s*Jurnal|RRN|No\.?\s*Struk)\s*(?:[:\-=#]\s*|\n\s*)([A-Za-z0-9/\-_]{5,40})",
        r"\b(FT\d{5,}[A-Z0-9]{2,})\b",
    ]
    for pat in ref_patterns:
        m = re.search(pat, combined, re.IGNORECASE)
        if m:
            cand_ref = m.group(1).strip()
            if cand_ref and cand_ref.lower() not in ("berhasil", "sukses", "transaksi"):
                ref_id = cand_ref
                break

    # 5. Extract Saldo Akhir if included in notification
    saldo_akhir: Optional[float] = None
    saldo_match = re.search(
        r"(?:Saldo\s+Akhir|Sisa\s+Saldo|Saldo\s+Tersedia|Ending\s+Balance|Balance)\s*[:\-=]?\s*(?:Rp\.?|IDR)?\s*([0-9][0-9.,]{2,})",
        body_text,
        re.IGNORECASE,
    )
    if saldo_match:
        saldo_akhir = parse_indonesian_amount(saldo_match.group(1))

    # 6. Extract Biaya Transfer / Biaya Admin for outgoing transactions
    admin_fee = 0.0
    if jenis == "keluar":
        fee_match = re.search(
            r"(?:Biaya\s+Transfer|Biaya\s+Admin(?:istrasi)?|Biaya\s+Transaksi|Biaya\s+Layanan|Admin\s+Fee)\s*(?:[:\-=]\s*|\n\s*)(?:Rp\.?|IDR)?\s*([0-9][0-9.,]*)",
            body_text,
            re.IGNORECASE,
        )
        if fee_match:
            parsed_fee = parse_indonesian_amount(fee_match.group(1))
            if parsed_fee and 0 < parsed_fee <= 50000:
                admin_fee = parsed_fee
                if used_total_field and nominal > admin_fee:
                    nominal = nominal - admin_fee
    # Extract merchant location if present
    merchant_location = None
    loc_match = re.search(
        r"(?:Merchant\s+Location|Lokasi\s+Merchant|Lokasi|Kota)\s*(?:[:\-=]\s*|\n\s*)([^\n\r]+)",
        body_text,
        re.IGNORECASE,
    )
    if loc_match:
        loc_val = re.sub(r"\s+", " ", loc_match.group(1)).strip(" :.-")
        loc_val = re.sub(r",?\s*\d{5}.*$", "", loc_val).strip(" ,.-")
        if loc_val and len(loc_val) >= 2 and loc_val.lower() not in ("berhasil", "sukses"):
            merchant_location = loc_val[:50]

    is_qris = any(
        k in combined_low
        for k in ("qris", "qris payment", "pembayaran qris")
    )

    # Extract transfer type (e.g. "Transfer to BCA Account", "Transfer BI-FAST")
    transfer_type = None
    tx_type_m = re.search(
        r"(?:Transfer\s+Type|Jenis\s+Transaksi|Metode\s+Transfer|Transaction\s+Type)\s*(?:[:\-=]\s*|\n\s*)([^\n\r]+)",
        body_text,
        re.IGNORECASE,
    )
    if tx_type_m:
        t_cand = re.sub(r"\s+", " ", tx_type_m.group(1)).strip(" :.-")
        if t_cand and t_cand.lower() not in ("-", "debet", "credit", "keluar", "masuk"):
            transfer_type = t_cand[:80]

    # Extract user remarks if any
    remarks = None
    remarks_m = re.search(
        r"(?:Remarks|Berita|Catatan|Keterangan|Deskripsi)\s*(?:[:\-=]\s*|\n\s*)([^\n\r]+)",
        body_text,
        re.IGNORECASE,
    )
    if remarks_m:
        r_cand = re.sub(r"\s+", " ", remarks_m.group(1)).strip(" :.-")
        if r_cand and r_cand.lower() not in ("-", "none", "null", "tidak ada", "reference no", "no referensi", "nomor referensi"):
            remarks = r_cand[:120]

    if remarks:
        catatan = remarks
    elif transfer_type:
        catatan = transfer_type
    else:
        clean_subj = re.sub(r"^(?:Fwd?|Re|Trs)\s*:\s*", "", subject or "", flags=re.IGNORECASE).strip()
        catatan = clean_subj or f"Notifikasi Email {akun}"

    if is_qris:
        loc_str = f" ({merchant_location})" if merchant_location else ""
        if merchant and merchant != "-" and merchant.lower() != "qris":
            catatan = f"Pembayaran QRIS — {merchant}{loc_str}"
        elif merchant_location:
            catatan = f"Pembayaran QRIS{loc_str}"
        else:
            catatan = "Pembayaran QRIS"
    elif "transfer" in (transfer_type or "").lower() or "transfer" in (subject or "").lower() or "transfer" in (catatan or "").lower():
        if merchant and merchant != "-":
            type_label = f" ({transfer_type})" if transfer_type and transfer_type.lower() != "transfer" else ""
            catatan = f"Transfer ke {merchant} — {remarks}" if remarks else f"Transfer ke {merchant}{type_label}"
    ewallet_m = re.search(r"e-wallet\s*-\s*([A-Za-z0-9_]+)", body_text, re.IGNORECASE)
    if ewallet_m:
        ew_name = ewallet_m.group(1).strip()
        merchant = "ShopeePay" if ew_name.lower() in ("shopeepay", "shopee") else (ew_name.upper() if ew_name.lower() in ("ovo", "dana", "gopay") else ew_name.capitalize())
        catatan = f"Top Up e-Wallet - {merchant}"
    if admin_fee > 0:
        fmt_fee = f"Rp {admin_fee:,.0f}".replace(",", ".")
        catatan = f"{catatan} (+ Biaya Admin {fmt_fee})"

    tanggal = normalize_email_datetime(email_date, body_text)
    clean_subject_for_cat = re.sub(r"(?i)\binternet\s+transaction\s+journal\b", "", subject or "").strip()
    clean_subject_for_cat = re.sub(r"(?i)\binternet\s+banking\b", "", clean_subject_for_cat).strip()
    kategori = infer_category(merchant, f"{clean_subject_for_cat} {catatan}".strip(), jenis)
    confidence = 0.99 if ref_id != "-" else 0.95
    return {
        "tanggal": tanggal,
        "jenis": jenis,
        "kategori": kategori,
        "nominal": float(nominal),
        "biaya_admin": float(admin_fee),
        "akun": akun,
        "merchant": merchant,
        "merchant_location": merchant_location,
        "catatan": catatan,
        "ref_id": ref_id,
        "saldo_akhir": saldo_akhir,
        "ai_confidence": confidence,
    }


async def extract_email_with_ai(
    sender: str,
    subject: str,
    body_text: str,
    email_date: Optional[str] = None,
) -> Dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {AI_API_KEY}",
        "Content-Type": "application/json",
    }
    default_date = normalize_email_datetime(email_date, body_text)
    user_content = (
        f"{EMAIL_AI_PROMPT}\n\n"
        f"Waktu Email: {default_date}\n"
        f"Pengirim: {sender}\n"
        f"Subjek: {subject}\n"
        f"Isi Email:\n{body_text[:4000]}"
    )
    payload = {
        "model": AI_MODEL,
        "messages": [{"role": "user", "content": user_content}],
        "temperature": 0.1,
        "stream": False,
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        url = f"{AI_API_BASE.rstrip('/')}/chat/completions"
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()

        content_type = resp.headers.get("content-type", "")
        raw_text = ""
        if "application/json" in content_type:
            data = resp.json()
            raw_text = data["choices"][0]["message"]["content"].strip()
        else:
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

    clean_str = raw_text
    if "```" in clean_str:
        clean_str = re.sub(r"```(?:json)?\s*", "", clean_str)
        clean_str = clean_str.replace("```", "").strip()

    match = re.search(r"\{.*\}", clean_str, re.DOTALL)
    if not match:
        raise ValueError(f"Format JSON tidak ditemukan dari respons AI: {raw_text[:200]}")

    parsed = json.loads(match.group(0))
    if not parsed.get("is_transaction", True) or float(parsed.get("nominal") or 0) <= 0:
        return {"kind": "ignored", "reason": "Bukan email mutasi transaksi finansial"}

    nominal = float(parsed.get("nominal") or 0)
    jenis = (parsed.get("jenis") or "keluar").lower()
    if jenis not in ("masuk", "keluar"):
        jenis = "keluar"
    akun = parsed.get("akun") or _detect_bank_account(sender, subject, body_text) or "BCA"
    merchant = parsed.get("merchant") or "-"
    catatan = parsed.get("catatan") or subject or f"Notifikasi Email {akun}"
    kategori = parsed.get("kategori") or infer_category(merchant, catatan, jenis)
    ref_id = parsed.get("ref_id") or "-"
    tanggal = parsed.get("tanggal") or default_date
    saldo_akhir_raw = parsed.get("saldo_akhir")
    saldo_akhir = float(saldo_akhir_raw) if saldo_akhir_raw is not None else None
    confidence = float(parsed.get("ai_confidence") or 0.94)

    return {
        "kind": "transaction",
        "parser": "ai_llm",
        "data": {
            "tanggal": tanggal,
            "jenis": jenis,
            "kategori": kategori,
            "nominal": nominal,
            "akun": akun,
            "merchant": merchant,
            "catatan": catatan,
            "ref_id": ref_id,
            "saldo_akhir": saldo_akhir,
            "ai_confidence": confidence,
        },
    }


async def parse_incoming_email(
    sender: str,
    subject: str,
    body_text: str,
    body_html: Optional[str] = None,
    email_date: Optional[str] = None,
) -> Dict[str, Any]:
    html_text = strip_html_to_text(body_html) if body_html else ""
    plain_text = strip_html_to_text(body_text) if "<" in (body_text or "") and ">" in (body_text or "") else (body_text or "").strip()
    combined_body = f"{plain_text}\n{html_text}".strip() if html_text and html_text not in plain_text else (plain_text or html_text)

    # 1. Check Gmail auto-forwarding verification email
    verif = detect_gmail_forwarding_verification(sender, subject, combined_body)
    if verif:
        return {
            "kind": "verification",
            "code": verif.get("code"),
            "url": verif.get("url"),
        }

    # 2. Try deterministic multi-bank parser
    det = parse_bank_email_deterministic(sender, subject, combined_body, email_date=email_date)
    if det and float(det.get("nominal") or 0) > 0:
        return {
            "kind": "transaction",
            "parser": "deterministic",
            "data": det,
        }

    # 3. Pre-filter obvious non-bank system/security emails before AI LLM fallback
    probe_low = f"{sender}\n{subject}\n{combined_body[:1500]}".lower()
    if any(
        sys_dom in (sender or "").lower()
        for sys_dom in ("accounts.google.com", "notifications.google.com", "googleplay-noreply@google.com")
    ) and "forwarded message" not in probe_low:
        return {"kind": "ignored", "reason": "Email sistem/keamanan Google (bukan mutasi bank)"}

    fin_keywords = (
        "rp", "idr", "nominal", "sejumlah", "transfer", "qris", "pembayaran",
        "transaksi", "mutasi", "bca", "bsi", "byond", "mandiri", "livin",
        "bri", "brimo", "bni", "wondr", "jago", "seabank", "gopay", "ovo", "dana",
    )
    if not any(kw in probe_low for kw in fin_keywords):
        return {"kind": "ignored", "reason": "Bukan email mutasi transaksi finansial"}

    # 4. Fallback to AI LLM via 9router
    try:
        return await extract_email_with_ai(sender, subject, combined_body, email_date=email_date)
    except Exception as e:
        raise RuntimeError(f"Gagal mengekstrak email dengan parser deterministik maupun AI fallback: {e}")
