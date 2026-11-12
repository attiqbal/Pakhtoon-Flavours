"""Her own customer messages (from plans/message-templates.md), filled in by code so the wording never drifts.
Used on the order confirmation page and for one-tap WhatsApp messages from the dashboard."""
from .shop import money, setting


def brand() -> str:
    return setting("shop_name")


def order_confirmation(o: dict) -> str:
    dishes = "\n".join(f"   - {i['qty']} x {i['dish']} ({i['portion']})" for i in o["items"] if i["dish"] not in ("Nan", "Tandoori Nan"))
    naan = sum(i["qty"] for i in o["items"] if i["dish"] in ("Nan", "Tandoori Nan"))
    lines = [f"Thank you for your order with {brand()} 🌟", "",
             f"Your order (no. {o['id']}) has been confirmed with the following details:",
             f"• Dish(es):\n{dishes}" if dishes else "• Dish(es): none",
             f"• Naan: {'yes, ' + str(naan) if naan else 'no'}",
             f"• Date: {o['date_pretty']}", f"• Time: {o['time']}",
             f"• Collection / Delivery: {o['fulfilment'].capitalize()}"]
    if o["fulfilment"] == "delivery":
        lines += [f"• Delivery address: {o['address']}, {o['postcode']}", f"• Delivery charge: {money(o['delivery_fee'])}"]
    else:
        lines.append(f"• Collection from: {setting('kitchen_postcode')}")
    lines += ["", f"Total amount: {money(o['total'])}",
              f"Payment method: {payment_label(o)}", "",
              "Please let us know as soon as possible if there are any changes.", "",
              "We look forward to serving you and hope you enjoy your meal 😊"]
    return "\n".join(lines)


def payment_label(o: dict) -> str:
    return {"card": "Card (paid online)", "cash": "Cash on collection/delivery", "bank": "Bank transfer"}.get(o["payment_method"], o["payment_method"])


def payment_request(o: dict) -> str:
    from . import config
    if o["payment_method"] == "card":
        how = f"• Card, Apple Pay or Google Pay on your order page:\n{config.BASE_URL}/o/{o['code']}"
    elif o["payment_method"] == "bank":
        how = "• Bank transfer to:\n" + (setting("bank_details") or "(we'll send our bank details on WhatsApp)")
    else:
        how = "• Cash on collection/delivery"
    return (f"Thank you for confirming your order with {brand()} 🌟\n\n"
            f"Your total amount is {money(o['total'])}.\n\n"
            f"Payment can be made by:\n{how}\n\n"
            + ("Please confirm once payment has been made so we can proceed with preparing your order.\n\n" if o["payment_method"] != "cash" else "")
            + "Many thanks for your support 😊")


def cancellation_policy() -> str:
    return (f"Kindly note our cancellation policy for {brand()}:\n"
            "• Cancellations must be made at least 24 hours in advance.\n"
            "• For late cancellations or no-shows, we may not be able to offer a full refund as ingredients and "
            "preparation will already have started.\n\nThank you for your understanding and cooperation 😊")


def thank_you() -> str:
    return (f"Thank you for ordering from {brand()} 🌟\nWe truly appreciate your support and hope you enjoyed your meal.\n"
            "If there is anything we can improve, please feel free to let us know — your feedback means a lot to us.\n"
            "We look forward to serving you again 😊")


def review_request() -> str:
    link = setting("review_link")
    return (f"Thank you once again for choosing {brand()} 🌟\nIf you enjoyed your food, we would be very grateful if you "
            "could take a moment to leave us a review. Your feedback really helps our small business grow.\n"
            + (f"⭐ Leave a review here: {link}\n\n" if link else "")
            + f"And please follow us for updates, offers and behind-the-scenes videos:\n📸 Instagram: @{setting('instagram')} "
            f"🎵 TikTok: @{setting('tiktok')}\n\nThank you for your support — it truly means a lot to us 😊")


def delay_apology(reason: str, new_time: str) -> str:
    return ("We sincerely apologise for the delay with your order 🙏\n\n"
            f"Due to {reason or 'high demand'}, your order will be ready at approximately {new_time}.\n\n"
            "Thank you very much for your patience and understanding — we promise it will be worth the wait 😊")


def ready_now(o: dict) -> str:
    what = "is ready for collection" if o["fulfilment"] == "collection" else "is on its way to you"
    return f"Hi {o['customer_name'].split()[0]}, your {brand()} order no. {o['id']} {what} 😊"


TEMPLATES = [("confirmation", "Order confirmation"), ("payment", "Payment details"), ("policy", "Cancellation policy"),
             ("ready", "Ready / on its way"), ("delay", "Delay apology"), ("thanks", "Thank you"), ("review", "Review request")]


def render(key: str, o: dict, reason: str = "", new_time: str = "") -> str:
    return {"confirmation": lambda: order_confirmation(o), "payment": lambda: payment_request(o),
            "policy": cancellation_policy, "ready": lambda: ready_now(o),
            "delay": lambda: delay_apology(reason, new_time or "shortly"), "thanks": thank_you,
            "review": review_request}[key]()
