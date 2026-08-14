import os
import time
import threading
import logging
import traceback

import telebot
from telebot import apihelper
from telebot.types import InputMediaPhoto
from flask import Flask

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("price-bot")

# ---------- فحص الإعدادات الأساسية عند البدء ----------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("لازم تحدد متغير البيئة BOT_TOKEN بإعدادات Render")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FONT_PATH = os.path.join(BASE_DIR, "Cairo.ttf")
if not os.path.exists(FONT_PATH):
    # نسجل خطأ واضح بدل ما نخلي البرنامج ينهار بصمت وقت لأول صورة توصل
    log.error(f"⚠️ ملف الخط غير موجود بالمسار: {FONT_PATH}")
    log.error("تأكد إن Cairo.ttf مرفوع بنفس مجلد المشروع بـ GitHub.")

# فحص حاسم: بدون raqm، الكتابة العربية تطلع مقلوبة/مقطعة بهذا الخط.
# نتأكد ونسجل بوضوح بدل ما نكتشف المشكلة بعد ما توصل شكوى من المستخدم.
try:
    from PIL import features
    if features.check("raqm"):
        log.info("✅ raqm متوفر — التشكيل العربي راح يشتغل صح.")
    else:
        log.error(
            "❌ raqm غير متوفر بنسخة Pillow المثبتة! "
            "الكتابة العربية راح تطلع مقلوبة أو مقطعة. "
            "تأكد من ملف runtime.txt وإن Pillow تركبت من wheel جاهز مو من المصدر."
        )
except Exception:
    log.error("تعذر فحص توفر raqm:\n" + traceback.format_exc())

# نستورد بعد التأكد، وأي فشل هنا يطلع بالـ log بوضوح
try:
    from price_stamp import draw_price_box
    PRICE_STAMP_OK = True
except Exception:
    log.error("فشل استيراد price_stamp.py:\n" + traceback.format_exc())
    PRICE_STAMP_OK = False

apihelper.RETRY_ON_ERROR = True  # يعيد المحاولة تلقائياً على أخطاء الشبكة المؤقتة
bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None, threaded=True)

# ---------- خادم صغير بس لأجل UptimeRobot يخلي الخدمة صاحية ----------
app = Flask(__name__)
bot_status = {"running": False, "last_error": None, "restarts": 0}


@app.route("/")
def health():
    state = "alive ✅" if bot_status["running"] else "starting/restarting ⚠️"
    try:
        from PIL import features
        raqm_state = "raqm OK ✅" if features.check("raqm") else "raqm MISSING ❌ (الكتابة العربية راح تطلع مقلوبة)"
    except Exception:
        raqm_state = "raqm check failed"
    return (
        f"price bot status: {state} | restarts: {bot_status['restarts']} "
        f"| last_error: {bot_status['last_error']} | {raqm_state}"
    ), 200


# ---------- منطق البوت ----------
WELCOME = (
    "أهلاً! 👋\n"
    "أرسل صورة المنتج مع كتابة السعر بنفس الرسالة (caption).\n"
    "مثال:\n"
    "12 زوج\n"
    "67 الف\n\n"
    "وراح أرجعلك نفس الصورة وفيها صندوق السعر جاهز.\n\n"
    "✨ تقدر ترسل أكثر من صورة سوا (كألبوم) وتكتب السعر بواحدة منهم بس — "
    "وراح أرجعلك الكل بنفس التعديل."
)


@bot.message_handler(commands=["start", "help"])
def handle_start(message):
    try:
        bot.reply_to(message, WELCOME)
    except Exception:
        log.error("فشل إرسال رسالة الترحيب:\n" + traceback.format_exc())


def _download_and_stamp(file_id, caption):
    """يحمل صورة من تيليجرام، يحطلها صندوق السعر، ويرجع مسار الصورة الناتجة.
    يرمي Exception لو صار أي خطأ (يتم التعامل معه بمكان الاستدعاء)."""
    file_info = bot.get_file(file_id)
    downloaded = bot.download_file(file_info.file_path)

    input_path = f"/tmp/{file_id}_in.jpg"
    output_path = f"/tmp/{file_id}_out.jpg"
    with open(input_path, "wb") as f:
        f.write(downloaded)

    try:
        draw_price_box(input_path, caption, output_path)
    finally:
        if os.path.exists(input_path):
            os.remove(input_path)

    return output_path


# ---------- تجميع صور الألبومات (عدة صور بنفس الرسالة) ----------
GROUP_WAIT_SECONDS = 1.8  # مدة الانتظار بعد آخر صورة توصل بنفس الألبوم قبل المعالجة
media_groups = {}
media_groups_lock = threading.Lock()


def process_single_photo(chat_id, file_id, caption):
    try:
        output_path = _download_and_stamp(file_id, caption)
        try:
            with open(output_path, "rb") as f:
                bot.send_photo(chat_id, f)
        finally:
            if os.path.exists(output_path):
                os.remove(output_path)
    except Exception:
        log.error(f"فشل معالجة صورة من {chat_id}:\n" + traceback.format_exc())
        bot.send_message(
            chat_id,
            "⚠️ صار خطأ بمعالجة هذي الصورة تحديداً. جرب صورة ثانية، "
            "وإذا تكررت المشكلة خبرني بالتفصيل.",
        )


def flush_media_group(group_id):
    with media_groups_lock:
        group = media_groups.pop(group_id, None)
    if not group:
        return

    chat_id = group["chat_id"]
    caption = group["caption"]
    file_ids = group["file_ids"]

    if not caption:
        bot.send_message(
            chat_id,
            "⚠️ ما لكيت نص السعر بهذي المجموعة من الصور. "
            "لازم تكتب السعر بخانة الكتابة (caption) وأنت ترسل الصور، مو برسالة منفصلة.",
        )
        return

    output_paths = []
    try:
        for file_id in file_ids:
            try:
                output_paths.append(_download_and_stamp(file_id, caption))
            except Exception:
                log.error(f"فشل معالجة صورة ضمن ألبوم من {chat_id}:\n" + traceback.format_exc())

        if not output_paths:
            bot.send_message(chat_id, "⚠️ صار خطأ بمعالجة كل صور هذي المجموعة. جرب ترسلها من جديد.")
            return

        # نرسلهم سوا كألبوم واحد لو أكثر من صورة، أو صورة مفردة لو وحدة بس
        if len(output_paths) == 1:
            with open(output_paths[0], "rb") as f:
                bot.send_photo(chat_id, f)
        else:
            files = [open(p, "rb") for p in output_paths]
            try:
                media = [InputMediaPhoto(f) for f in files]
                bot.send_media_group(chat_id, media)
            finally:
                for f in files:
                    f.close()

        if len(output_paths) < len(file_ids):
            bot.send_message(
                chat_id,
                f"⚠️ تنبيه: {len(file_ids) - len(output_paths)} من الصور فشلت معالجتها ولم ترجع.",
            )

    except Exception:
        log.error(f"فشل إرسال ألبوم لـ {chat_id}:\n" + traceback.format_exc())
        bot.send_message(chat_id, "⚠️ صار خطأ بإرسال الصور المعدّلة. جرب مرة ثانية.")

    finally:
        for p in output_paths:
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass


@bot.message_handler(content_types=["photo"])
def handle_photo(message):
    if not PRICE_STAMP_OK:
        bot.reply_to(
            message,
            "⚠️ البوت لسه ما جاهز تماماً (مشكلة إعداد داخلية). خبر المسؤول التقني.",
        )
        return

    file_id = message.photo[-1].file_id
    caption = (message.caption or "").strip() or None

    # حالة الألبوم: عدة صور مرسلة سوا (نفس media_group_id)
    if message.media_group_id:
        group_id = message.media_group_id
        with media_groups_lock:
            group = media_groups.get(group_id)
            if group is None:
                group = {"chat_id": message.chat.id, "file_ids": [], "caption": None, "timer": None}
                media_groups[group_id] = group

            group["file_ids"].append(file_id)
            if caption:
                group["caption"] = caption

            if group["timer"]:
                group["timer"].cancel()
            timer = threading.Timer(GROUP_WAIT_SECONDS, flush_media_group, args=[group_id])
            timer.daemon = True
            group["timer"] = timer
            timer.start()
        return

    # صورة مفردة (مو ألبوم)
    if not caption:
        bot.reply_to(
            message,
            "⚠️ ما لكيت نص السعر. لازم تكتب السعر بنفس رسالة الصورة (caption)، مو برسالة منفصلة.",
        )
        return

    process_single_photo(message.chat.id, file_id, caption)


@bot.message_handler(func=lambda m: True, content_types=["text"])
def handle_text(message):
    try:
        bot.reply_to(message, "أرسل الصورة مع كتابة السعر بنفس الرسالة (caption)، مو نص لحاله.")
    except Exception:
        log.error("فشل الرد على رسالة نصية:\n" + traceback.format_exc())


# ---------- حلقة تشغيل صلبة: تعيد البوت تلقائياً لو انهار ----------
def run_bot_forever():
    while True:
        try:
            bot_status["running"] = True
            log.info("بدء/إعادة تشغيل بوت تيليجرام (polling)...")
            try:
                bot.remove_webhook()
            except Exception:
                pass
            bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
        except Exception:
            error_text = traceback.format_exc()
            log.error("انهار البوت بخطأ غير متوقع، راح يعيد المحاولة خلال 5 ثواني:\n" + error_text)
            bot_status["running"] = False
            bot_status["last_error"] = str(error_text.strip().splitlines()[-1]) if error_text else "unknown"
            bot_status["restarts"] += 1
            time.sleep(5)
        else:
            # infinity_polling ما يفترض يخلص عادي، بس لو خلص نعيده احتياط
            bot_status["running"] = False
            log.warning("توقف polling بدون خطأ واضح، إعادة التشغيل خلال 5 ثواني...")
            time.sleep(5)


if __name__ == "__main__":
    threading.Thread(target=run_bot_forever, daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
