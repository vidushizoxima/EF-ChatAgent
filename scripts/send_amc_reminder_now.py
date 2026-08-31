#!/usr/bin/env python3
"""
send_amc_reminder_now.py — one-shot AMC reminder to a single handset.

Sends three messages in sequence via the +91 95995 59646 production number:
  1. AMC renewal reminder text with offers + a dummy Stripe payment link
  2. The offer brochure PDF (uploaded then sent as a document)
  3. A follow-up text with interactive CTA buttons

Usage:
    .venv/bin/python scripts/send_amc_reminder_now.py
"""

import os
import sys
import httpx
from dotenv import dotenv_values

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH = os.path.join(ROOT, ".env")
GRAPH = "https://graph.facebook.com/v23.0"

# Production WABA number: +91 95995 59646
PHONE_NUMBER_ID = "1302822852910141"
RECIPIENT = "919899643944"

# Brochure PDF
BROCHURE_PATH = os.path.join(ROOT, "eureka-forbes-offer-brochure.pdf")
BROCHURE_FILENAME = "Eureka Forbes - Monsoon Offers.pdf"

# Dummy Stripe payment link
STRIPE_PAYMENT_LINK = "https://buy.stripe.com/test_eVa3cS4gM2bK1234abc"


def token() -> str:
    return (dotenv_values(ENV_PATH) or {}).get("WHATSAPP_TOKEN", "") or ""


def send_text(tok: str, text: str, preview_url: bool = True):
    """Send a plain text message."""
    r = httpx.post(
        f"{GRAPH}/{PHONE_NUMBER_ID}/messages",
        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
        json={
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": RECIPIENT,
            "type": "text",
            "text": {"preview_url": preview_url, "body": text},
        },
        timeout=30.0,
    )
    if r.status_code == 200:
        msg_id = r.json().get("messages", [{}])[0].get("id", "?")
        print(f"  ✅ Text sent — wamid: {msg_id}")
        return True
    print(f"  ❌ Text failed: {r.status_code} {r.text}")
    return False


def upload_media(tok: str, path: str, mime_type: str = "application/pdf"):
    """Upload a file and return its media id."""
    if not os.path.exists(path):
        print(f"  ❌ File not found: {path}")
        return None
    url = f"{GRAPH}/{PHONE_NUMBER_ID}/media"
    with open(path, "rb") as f:
        r = httpx.post(
            url,
            headers={"Authorization": f"Bearer {tok}"},
            files={
                "file": (os.path.basename(path), f, mime_type),
                "messaging_product": (None, "whatsapp"),
                "type": (None, mime_type),
            },
            timeout=120.0,
        )
    if r.status_code == 200:
        media_id = r.json().get("id")
        print(f"  📄 Uploaded {os.path.basename(path)} → media id {media_id}")
        return media_id
    print(f"  ❌ Upload failed: {r.status_code} {r.text}")
    return None


def send_document(tok: str, media_id: str, filename: str, caption: str = ""):
    """Send an already-uploaded document."""
    doc = {"id": media_id, "filename": filename}
    if caption:
        doc["caption"] = caption[:1024]
    r = httpx.post(
        f"{GRAPH}/{PHONE_NUMBER_ID}/messages",
        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
        json={
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": RECIPIENT,
            "type": "document",
            "document": doc,
        },
        timeout=30.0,
    )
    if r.status_code == 200:
        msg_id = r.json().get("messages", [{}])[0].get("id", "?")
        print(f"  ✅ Document sent — wamid: {msg_id}")
        return True
    print(f"  ❌ Document failed: {r.status_code} {r.text}")
    return False


def main():
    tok = token()
    if not tok:
        print("❌ WHATSAPP_TOKEN is empty in .env")
        sys.exit(1)

    print(f"\n🚀 Sending AMC reminder to +{RECIPIENT} from +91 95995 59646\n")

    # ── Message 1: AMC Reminder + Offers + Stripe Link ─────────────────────
    reminder_text = (
        "🔔 *AMC Renewal Reminder — Eureka Forbes*\n"
        "\n"
        "Dear Customer,\n"
        "\n"
        "Your Eureka Forbes Annual Maintenance Contract (AMC) is due for renewal. "
        "Renewing on time ensures uninterrupted service visits, priority support, "
        "and genuine spare parts for your water purifier.\n"
        "\n"
        "🎁 *Monsoon Special Offer — Limited Period!*\n"
        "✅ *20% OFF* on AMC renewal\n"
        "✅ *20% OFF* on all Eureka Forbes products\n"
        "✅ *FREE* pre-monsoon health check-up for your appliance\n"
        "📅 Offer valid until *8 September 2026*\n"
        "\n"
        "💳 *Renew Now — Quick & Secure Payment:*\n"
        f"{STRIPE_PAYMENT_LINK}\n"
        "\n"
        "📎 We've also attached our complete monsoon offer brochure with all "
        "product details and pricing.\n"
        "\n"
        "Reply here or call us at *+91 80492 80357* to renew instantly. "
        "Our team is ready to help!\n"
        "\n"
        "— Team Eureka Forbes 💧"
    )

    print("1️⃣  Sending AMC reminder text with offers & payment link...")
    send_text(tok, reminder_text, preview_url=True)

    # ── Message 2: Offer Brochure PDF ──────────────────────────────────────
    print("2️⃣  Uploading offer brochure PDF...")
    media_id = upload_media(tok, BROCHURE_PATH)
    if media_id:
        print("3️⃣  Sending brochure document...")
        send_document(
            tok,
            media_id,
            BROCHURE_FILENAME,
            caption=(
                "📋 Eureka Forbes — Monsoon 2026 Offer Brochure\n"
                "20% off across all products & AMC renewals. Valid until 8 September 2026."
            ),
        )
    else:
        print("⚠️  Skipping document send — upload failed")

    # ── Message 3: Follow-up with payment link ────────────────────────────
    followup_text = (
        "💡 *Quick Links:*\n"
        "\n"
        f"💳 Pay securely: {STRIPE_PAYMENT_LINK}\n"
        "📞 Call us: +91 80492 80357\n"
        "💬 Or just reply here — we'll take care of the rest!\n"
        "\n"
        "_This is an automated reminder from Eureka Forbes. "
        "Reply STOP to opt out of future reminders._"
    )

    print("4️⃣  Sending follow-up with quick links...")
    send_text(tok, followup_text, preview_url=True)

    print("\n✅ Done!\n")


if __name__ == "__main__":
    main()
