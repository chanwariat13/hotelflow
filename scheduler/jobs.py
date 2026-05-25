# scheduler/jobs.py
from services import database as db
from services.whatsapp import send_text, send_to_phones, send_media_b64
from services.helpers import fmt_date, ist_now, html_to_pdf_b64, build_bill_html
from services.cache import get_all_occupied_rooms, get_session, delete_session, delete_room, delete_pending
from datetime import datetime, timedelta
import pytz, logging

IST = pytz.timezone("Asia/Kolkata")
logger = logging.getLogger(__name__)


async def job_daily_report():
    try:
        hotels = await db.get_all_hotels()
        for h in hotels:
            if not h.get("is_active"): continue
            hid = h["id"]
            s = await db.get_daily_revenue(hid)
            yesterday = (datetime.now(IST)-timedelta(days=1)).strftime("%d %b %Y")
            total = float(s.get("total_revenue",0))
            room  = float(s.get("room_revenue",0))
            food  = float(s.get("food_revenue",0))
            svc   = float(s.get("service_revenue",0))
            pend  = float(s.get("pending_revenue",0))
            cash  = float(s.get("cash_collected",0))
            online= float(s.get("online_collected",0))
            active= int(s.get("active_guests",0))
            ci    = int(s.get("checkins_today",0))
            co    = int(s.get("checkouts_today",0))
            msg = (f"📊 *DAILY REPORT — {h['hotel_name']}*\n━━━━━━━━━━━━━━━━━━\n"
                   f"📅 {yesterday}\n\n🏨 Occupied: {active} | Check-ins: {ci} | Checkouts: {co}\n\n"
                   f"💰 *REVENUE*\n🏠 Room: ₹{room:.0f}\n🍽️ Food: ₹{food:.0f}\n🛎️ Services: ₹{svc:.0f}\n"
                   f"━━━━━━━━━━━━━━━━━━\n*Total: ₹{total:.0f}*\n"
                   f"💵 Cash: ₹{cash:.0f} | 🌐 Online: ₹{online:.0f}\n⏳ Pending: ₹{pend:.0f}\n"
                   f"━━━━━━━━━━━━━━━━━━\n_Auto report · HotelFlow_")
            phones = await db.get_staff_phones(hid, ["owner","manager"])
            instance = h["instance_name"]
            await send_to_phones(instance, phones, msg)
    except Exception as e:
        logger.error(f"Daily report error: {e}")


async def job_monthly_report():
    try:
        hotels = await db.get_all_hotels()
        for h in hotels:
            if not h.get("is_active"): continue
            hid = h["id"]
            s = await db.get_monthly_stats(hid)
            foods = await db.get_top_food(hid)
            month = (s.get("report_month") or "Last Month").strip()
            msg = (f"📅 *MONTHLY REPORT — {month.upper()}*\n━━━━━━━━━━━━━━━━━━\n"
                   f"🏨 Bookings: {int(s.get('total_bookings',0))} | "
                   f"Guests: {int(s.get('unique_guests',0))} | "
                   f"Completed: {int(s.get('completed_stays',0))}\n"
                   f"📅 Avg Stay: {float(s.get('avg_stay_days',0)):.1f} days\n\n"
                   f"💰 Room: ₹{float(s.get('room_revenue',0)):.0f} | "
                   f"Food: ₹{float(s.get('food_revenue',0)):.0f}\n"
                   f"━━━━━━━━━━━━━━━━━━\n*TOTAL: ₹{float(s.get('total_revenue',0)):.0f}*\n")
            if foods:
                msg += "\n🍽️ *TOP FOOD*\n"
                for i,f in enumerate(foods[:3]):
                    msg += f"{'🥇🥈🥉'[i]} {f.get('item_name','')} ({f.get('order_count',0)} orders)\n"
            msg += "\n━━━━━━━━━━━━━━━━━━\n_Monthly auto-report · HotelFlow_"
            phones = await db.get_staff_phones(hid, ["owner","manager"])
            await send_to_phones(h["instance_name"], phones, msg)
    except Exception as e:
        logger.error(f"Monthly report error: {e}")


async def job_reminder_1():
    """Night before checkout reminder."""
    try:
        for h in await db.get_all_hotels():
            if not h.get("is_active"): continue
            hid = h["id"]; instance = h["instance_name"]
            checkout_h = h.get("checkout_hour",11)
            guests = await db.get_tomorrow_checkouts(hid)
            if not guests: continue
            for g in guests:
                fname = (g.get("guest_name") or "Guest").split()[0]
                pend = float(g.get("total_pending",0))
                await send_text(instance, g["guest_phone"],
                    f"🌙 *Checkout Reminder*\n━━━━━━━━━━━━━━━━━━\n"
                    f"Namaste *{fname}*! 🙏\n\nYour checkout is *tomorrow* at *{checkout_h}:00 AM*.\n\n"
                    f"🏨 Room: {g.get('room_number')} | 💰 Pending: ₹{pend:.0f}\n\n"
                    f"⚠️ Late checkout attracts extra charges.\nType *my bill* to see charges.\n\n"
                    f"— {h['hotel_name']}")
            staff_msg = f"📋 *TOMORROW'S CHECKOUTS*\n━━━━━━━━━━━━━━━━━━\n\n"
            for g in guests:
                staff_msg += (f"🏨 Room {g.get('room_number')} — {g.get('guest_name')}\n"
                              f"   📱 {g.get('guest_phone')} | 💰 ₹{float(g.get('total_pending',0)):.0f}\n\n")
            staff_msg += f"⏰ Checkout by {checkout_h}:00 AM"
            phones = await db.get_staff_phones(hid, ["owner","manager","staff"])
            await send_to_phones(instance, phones, staff_msg)
    except Exception as e:
        logger.error(f"Reminder 1 error: {e}")


async def job_reminder_2():
    """Morning of checkout reminder."""
    try:
        for h in await db.get_all_hotels():
            if not h.get("is_active"): continue
            hid = h["id"]; instance = h["instance_name"]
            checkout_h = h.get("checkout_hour",11)
            guests = await db.get_today_checkouts(hid)
            for g in guests:
                fname = (g.get("guest_name") or "Guest").split()[0]
                pend = float(g.get("total_pending",0))
                await send_text(instance, g["guest_phone"],
                    f"⏰ *Checkout Today!*\n━━━━━━━━━━━━━━━━━━\n"
                    f"Good morning *{fname}*! 🌅\n\n"
                    f"🏨 Room *{g.get('room_number')}* checkout is *today at {checkout_h}:00 AM*.\n\n"
                    f"💰 Pending: ₹{pend:.0f}\n\nType *my bill* · *checkout* · *pay online*\n\n"
                    f"— {h['hotel_name']}")
    except Exception as e:
        logger.error(f"Reminder 2 error: {e}")


async def job_late_alert():
    """Alert staff about late checkouts."""
    try:
        for h in await db.get_all_hotels():
            if not h.get("is_active"): continue
            hid = h["id"]; instance = h["instance_name"]
            checkout_h = h.get("checkout_hour",11)
            guests = await db.get_today_checkouts(hid)
            if not guests: continue
            msg = f"⚠️ *LATE CHECKOUT ALERT*\n━━━━━━━━━━━━━━━━━━\n\nNot checked out yet:\n\n"
            for g in guests:
                msg += (f"🏨 Room *{g.get('room_number')}* — {g.get('guest_name')}\n"
                        f"   📱 {g.get('guest_phone')} | 💰 ₹{float(g.get('total_pending',0)):.0f}\n\n")
            msg += f"⏰ Extra charges apply!\nUse *CHECKOUT R{{room}}* to process."
            phones = await db.get_staff_phones(hid)
            await send_to_phones(instance, phones, msg)
    except Exception as e:
        logger.error(f"Late alert error: {e}")


async def job_auto_late_charge():
    """Auto-apply late checkout fee."""
    try:
        from datetime import date
        for h in await db.get_all_hotels():
            if not h.get("is_active"): continue
            hid = h["id"]; instance = h["instance_name"]
            late_charge = float(h.get("late_charge_flat",500) or 500)
            checkout_h = h.get("checkout_hour",11)
            guests = await db.get_today_checkouts(hid)
            charged = []
            for g in guests:
                bid = g["booking_id"]
                existing = await db.fetchval(
                    "SELECT COUNT(*) FROM stay_charges WHERE booking_id=$1 AND service_type='Late Checkout' AND charge_date=CURRENT_DATE", bid)
                if existing: continue
                await db.insert_stay_charge({"booking_id":bid,"charge_date":date.today(),
                    "service_type":"Late Checkout",
                    "description":f"Late checkout — past {checkout_h}:00 AM",
                    "amount":late_charge,"total":late_charge,"payment_status":"Pending","hotel_id":hid})
                await send_text(instance, g["guest_phone"],
                    f"⚠️ *Late Checkout Charge*\n━━━━━━━━━━━━━━━━━━\n"
                    f"Dear *{g.get('guest_name')}*,\n\n"
                    f"🏨 Room: *{g.get('room_number')}*\n"
                    f"💰 Late charge: *₹{late_charge:.0f}* added.\n\n"
                    f"Please checkout immediately.\nType *checkout* when ready. 🙏")
                charged.append(g)
            if charged:
                staff_msg = (f"⚠️ *AUTO LATE CHARGES*\n━━━━━━━━━━━━━━━━━━\n"
                             f"₹{late_charge:.0f} added to:\n\n")
                for g in charged:
                    staff_msg += f"🏨 Room *{g.get('room_number')}* — {g.get('guest_name')}\n"
                phones = await db.get_staff_phones(hid)
                await send_to_phones(instance, phones, staff_msg)
    except Exception as e:
        logger.error(f"Auto late charge error: {e}")


async def job_auto_cleanup():
    """Every 30 min: clean expired Redis sessions."""
    try:
        occupied = await get_all_occupied_rooms()
        for item in occupied:
            room = item["room"]; phone = item["phone"].replace("PENDING:","")
            session = await get_session(phone)
            if not session:
                await delete_room(room)
                continue
            if session.get("sessionType") == "HOTEL":
                co = session.get("checkoutDate","")
                if co:
                    try:
                        deadline = IST.localize(datetime.strptime(co,"%Y-%m-%d").replace(hour=23,minute=59))
                        if datetime.now(IST) > deadline:
                            hid = session.get("hotelId",1)
                            await delete_session(phone); await delete_room(room); await delete_pending(phone)
                            await db.set_room_vacant(room, hid)
                    except: pass
    except Exception as e:
        logger.error(f"Auto cleanup error: {e}")
