import os
import asyncio
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional

from database import get_subscriptions_summary
from bot import get_notify_chat_id, send_message

_LAST_REMINDER_DATE: Optional[str] = None


def check_due_subscriptions(days_ahead: int = 2) -> List[Dict[str, Any]]:
    """Checks for active unpaid subscriptions due today or within the next `days_ahead` days."""
    now_dt = datetime.now()
    target_days = []
    for i in range(days_ahead + 1):
        future_dt = now_dt + timedelta(days=i)
        target_days.append((future_dt.day, i))

    summary = get_subscriptions_summary()
    due_items = []
    for it in summary.get("items", []):
        if not it.get("sudah_dibayar"):
            due = it.get("tanggal_jatuh_tempo", 1)
            match = next((td for td in target_days if td[0] == due), None)
            if match:
                due_items.append({
                    **it,
                    "days_left": match[1],
                })
    return due_items


async def send_subscription_reminder_now() -> Dict[str, Any]:
    global _LAST_REMINDER_DATE
    now_dt = datetime.now()
    today_str = now_dt.strftime("%Y-%m-%d")

    due_items = check_due_subscriptions(days_ahead=2)
    if not due_items:
        return {
            "status": "ok",
            "sent": False,
            "count": 0,
            "message": "Tidak ada tagihan yang mendekati jatuh tempo",
        }

    chat_id = get_notify_chat_id()
    if not chat_id:
        return {
            "status": "no_chat_id",
            "sent": False,
            "count": len(due_items),
            "message": "Chat ID Telegram belum tersedia",
        }

    lines = []
    for it in due_items:
        nom_fmt = f"Rp {int(it['estimasi_nominal']):,}".replace(",", ".")
        days_left = it["days_left"]
        due_txt = "Hari ini!" if days_left == 0 else ("Besok" if days_left == 1 else f"{days_left} hari lagi")
        lines.append(f"• ⏳ *{it['nama']}* — *{nom_fmt}* (Jatuh tempo: *Tgl {it['tanggal_jatuh_tempo']}* · {due_txt})")

    msg = (
        f"🔔 *Pengingat Tagihan Jatuh Tempo!* ({now_dt.strftime('%d %b %Y')})\n\n"
        f"Ada {len(due_items)} pos tagihan rutin yang perlu dibayar:\n"
        + "\n".join(lines)
        + "\n\n💡 _Status akan otomatis menjadi Lunas begitu mutasi transaksi tercatat di Pitih._"
    )

    await send_message(chat_id, msg)
    _LAST_REMINDER_DATE = today_str
    print(f"[SubscriptionReminder] Sent reminder for {len(due_items)} due subscriptions to chat_id {chat_id}")
    return {"status": "ok", "sent": True, "count": len(due_items), "items": due_items}


async def start_subscription_reminder_worker() -> None:
    print("[SubscriptionReminder] Automated subscription due date reminder worker started.")
    await asyncio.sleep(45)

    while True:
        try:
            now_dt = datetime.now()
            today_str = now_dt.strftime("%Y-%m-%d")
            global _LAST_REMINDER_DATE

            if _LAST_REMINDER_DATE != today_str:
                await send_subscription_reminder_now()

            await asyncio.sleep(4 * 3600)
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[SubscriptionReminder] Worker error: {e}")
            await asyncio.sleep(3600)
