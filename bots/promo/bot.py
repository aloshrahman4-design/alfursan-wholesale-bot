import os
import time
import html
import json
import threading
import logging
import traceback
from datetime import datetime

import requests
import telebot
from telebot import apihelper
from flask import Flask
from dotenv import load_dotenv

load_dotenv(override=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("promo-bot")

# ---------- فحص الإعدادات الأساسية عند البدء ----------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("لازم تحدد متغير البيئة TELEGRAM_BOT_TOKEN بإعدادات Render")

META_ACCESS_TOKEN = os.environ.get("META_ACCESS_TOKEN")
if not META_ACCESS_TOKEN:
    raise RuntimeError("لازم تحدد متغير البيئة META_ACCESS_TOKEN بإعدادات Render")

# حساب "عيون الفرسان" — نقدر نغيره بمتغير بيئة لو احتجنا مستقبلاً بدون تعديل الكود
META_AD_ACCOUNT_ID = os.environ.get("META_AD_ACCOUNT_ID", "1539637770660053")
META_API_VERSION = "v21.0"
META_GRAPH_URL = f"https://graph.facebook.com/{META_API_VERSION}"

# ---------- إعدادات التقرير التلقائي ----------
TARGET_CHAT_ID = int(os.environ.get("TARGET_CHAT_ID", "132209039"))
REPORT_INTERVAL_HOURS = float(os.environ.get("REPORT_INTERVAL_HOURS", "2"))

# ---------- إعدادات Gemini (ميزات الذكاء الاصطناعي — اختيارية) ----------
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
GEMINI_API_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
)
if not GEMINI_API_KEY:
    log.warning(
        "GEMINI_API_KEY غير محدد — ميزات الدردشة الذكية والتحليل التلقائي راح تكون معطلة."
    )

apihelper.RETRY_ON_ERROR = True  # يعيد المحاولة تلقائياً على أخطاء الشبكة المؤقتة
bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN, parse_mode="HTML", threaded=True)

# ---------- خادم صغير بس لأجل UptimeRobot يخلي الخدمة صاحية ----------
app = Flask(__name__)
bot_status = {"running": False, "last_error": None, "restarts": 0}


@app.route("/")
def health():
    state = "alive ✅" if bot_status["running"] else "starting/restarting ⚠️"
    return (
        f"promo bot status: {state} | restarts: {bot_status['restarts']} "
        f"| last_error: {bot_status['last_error']}"
    ), 200


# ---------- منطق سحب التقرير من Meta Marketing API ----------
_account_currency_cache = {"value": None}


def get_account_currency():
    """يجيب عملة الحساب الإعلاني ويخزنها بذاكرة مؤقتة (نادراً ما تتغير)."""
    if _account_currency_cache["value"]:
        return _account_currency_cache["value"]

    resp = requests.get(
        f"{META_GRAPH_URL}/act_{META_AD_ACCOUNT_ID}",
        params={"fields": "currency", "access_token": META_ACCESS_TOKEN},
        timeout=15,
    )
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"فشل جلب عملة الحساب: {data['error'].get('message')}")

    currency = data.get("currency", "")
    _account_currency_cache["value"] = currency
    return currency


def get_insights_for_period(date_preset):
    """يجيب أداء لحساب عيون الفرسان لفترة زمنية معينة (today, yesterday...)."""
    resp = requests.get(
        f"{META_GRAPH_URL}/act_{META_AD_ACCOUNT_ID}/insights",
        params={
            "fields": "spend,ctr,actions,clicks,impressions",
            "date_preset": date_preset,
            "access_token": META_ACCESS_TOKEN,
        },
        timeout=20,
    )
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"فشل جلب بيانات الأداء: {data['error'].get('message')}")

    rows = data.get("data", [])
    if not rows:
        return None

    row = rows[0]
    spend = float(row.get("spend", 0) or 0)
    ctr = float(row.get("ctr", 0) or 0)
    clicks = int(row.get("clicks", 0) or 0)
    impressions = int(row.get("impressions", 0) or 0)

    actions = row.get("actions", []) or []
    results = sum(int(float(a.get("value", 0))) for a in actions)

    return {
        "spend": spend,
        "ctr": ctr,
        "clicks": clicks,
        "impressions": impressions,
        "results": results,
    }


def get_today_insights():
    return get_insights_for_period("today")


def get_yesterday_insights():
    return get_insights_for_period("yesterday")


def get_campaign_insights_for_period(date_preset):
    """يجيب أداء كل حملة لحالها لحساب عيون الفرسان، مرتبة حسب الإنفاق تنازلياً."""
    resp = requests.get(
        f"{META_GRAPH_URL}/act_{META_AD_ACCOUNT_ID}/insights",
        params={
            "level": "campaign",
            "fields": "campaign_name,spend,ctr,actions,clicks,impressions",
            "date_preset": date_preset,
            "limit": 200,
            "access_token": META_ACCESS_TOKEN,
        },
        timeout=20,
    )
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"فشل جلب بيانات الحملات: {data['error'].get('message')}")

    campaigns = []
    for row in data.get("data", []):
        spend = float(row.get("spend", 0) or 0)
        if spend <= 0:
            continue  # نتجاهل الحملات اللي إنفاقها صفر لليوم

        ctr = float(row.get("ctr", 0) or 0)
        actions = row.get("actions", []) or []
        results = sum(int(float(a.get("value", 0))) for a in actions)

        campaigns.append({
            "name": row.get("campaign_name", "بدون اسم"),
            "spend": spend,
            "ctr": ctr,
            "results": results,
        })

    campaigns.sort(key=lambda c: c["spend"], reverse=True)
    return campaigns


# ---------- منطق الإجراءات التنفيذية (وقف/تشغيل حملة، تعديل ميزانية) ----------
# عملات "بدون كسور عشرية" بميتا — الميزانية فيها تُرسل بالوحدة الكاملة
# (مو بالسنت). باقي العملات (USD, EUR, GBP, SAR, AED...) تستخدم x100.
ZERO_DECIMAL_CURRENCIES = {
    "BIF", "DJF", "GNF", "ISK", "JPY", "KMF", "KRW", "PYG", "RWF",
    "UGX", "VND", "VUV", "XAF", "XOF", "XPF", "HUF", "TWD",
}

MAX_BUDGET_INCREASE_PCT = 50  # سقف الزيادة المسموحة بميزانية دفعة وحدة
PENDING_ACTION_TTL_SECONDS = 300  # 5 دقائق مهلة التأكيد
ACTIONS_LOG_FILE = os.environ.get("ACTIONS_LOG_FILE", "actions.log")

STATUS_AR = {
    "ACTIVE": "نشطة ▶️",
    "PAUSED": "موقوفة ⏸️",
    "CAMPAIGN_PAUSED": "موقوفة ⏸️",
    "ARCHIVED": "مؤرشفة 🗄️",
    "DELETED": "محذوفة",
    "IN_PROCESS": "قيد المعالجة",
    "WITH_ISSUES": "فيها مشكلة ⚠️",
}


def status_ar(status_value):
    return STATUS_AR.get(status_value, status_value or "غير معروف")


def currency_offset(currency):
    return 1 if currency in ZERO_DECIMAL_CURRENCIES else 100


def minor_to_major(minor_units, currency):
    return minor_units / currency_offset(currency)


def major_to_minor(major_value, currency):
    return round(major_value * currency_offset(currency))


def meta_post(object_id, params):
    """يرسل تعديل فعلي (POST) لكائن على Meta Graph API ويتحقق من الخطأ."""
    resp = requests.post(
        f"{META_GRAPH_URL}/{object_id}",
        data={**params, "access_token": META_ACCESS_TOKEN},
        timeout=20,
    )
    data = resp.json()
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(data["error"].get("message", "خطأ غير معروف من Meta"))
    return data


def get_all_campaigns():
    """يجيب كل حملات حساب عيون الفرسان (id, name, status, الميزانية اليومية إن وجدت)."""
    resp = requests.get(
        f"{META_GRAPH_URL}/act_{META_AD_ACCOUNT_ID}/campaigns",
        params={
            "fields": "id,name,status,effective_status,daily_budget",
            "limit": 500,
            "access_token": META_ACCESS_TOKEN,
        },
        timeout=20,
    )
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"فشل جلب قائمة الحملات: {data['error'].get('message')}")
    return data.get("data", [])


def get_campaign_spend_today(campaign_id):
    """يجيب إنفاق حملة معينة لليوم (للسياق برسالة التأكيد فقط)."""
    try:
        resp = requests.get(
            f"{META_GRAPH_URL}/{campaign_id}/insights",
            params={"fields": "spend", "date_preset": "today", "access_token": META_ACCESS_TOKEN},
            timeout=15,
        )
        rows = resp.json().get("data", [])
        if rows:
            return float(rows[0].get("spend", 0) or 0)
    except Exception:
        pass
    return 0.0


def meta_pause_campaign(campaign_id):
    return meta_post(campaign_id, {"status": "PAUSED"})


def meta_resume_campaign(campaign_id):
    return meta_post(campaign_id, {"status": "ACTIVE"})


def meta_set_campaign_budget(campaign_id, new_minor_units):
    return meta_post(campaign_id, {"daily_budget": int(new_minor_units)})


def find_matching_campaigns(query, campaigns):
    """مطابقة آمنة لاسم الحملة اللي ذكرها المستخدم مقابل القائمة الحقيقية من ميتا."""
    if not query:
        return []
    q = query.strip().lower()
    if not q:
        return []

    for c in campaigns:
        if str(c.get("id", "")).strip() == query.strip():
            return [c]

    exact = [c for c in campaigns if c.get("name", "").strip().lower() == q]
    if exact:
        return exact

    contains = [
        c for c in campaigns
        if q in c.get("name", "").lower() or c.get("name", "").lower() in q
    ]
    return contains


def log_action(chat_id, action_type, campaign_name, campaign_id, details, success, error=None):
    """يسجل كل إجراء تنفيذي فعلي بملف منفصل عن سجل التطبيق العام — للمراجعة."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    status_word = "نجح ✅" if success else "فشل ❌"
    line = f"[{ts}] chat={chat_id} action={action_type} campaign=\"{campaign_name}\" ({campaign_id}) | {details} -> {status_word}"
    if error:
        line += f" | خطأ: {error}"
    try:
        with open(ACTIONS_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        log.error("فشل الكتابة لملف سجل الإجراءات:\n" + traceback.format_exc())
    log.info(line)


# ---------- حالة التأكيد المعلق (خطوتين إجباري لأي إجراء تنفيذي) ----------
_pending_actions = {}
_pending_lock = threading.Lock()


def set_pending_action(chat_id, action):
    action["created_at"] = time.time()
    with _pending_lock:
        _pending_actions[chat_id] = action


def get_pending_action(chat_id):
    with _pending_lock:
        action = _pending_actions.get(chat_id)
    if not action:
        return None
    if time.time() - action["created_at"] > PENDING_ACTION_TTL_SECONDS:
        clear_pending_action(chat_id)
        return None
    return action


def clear_pending_action(chat_id):
    with _pending_lock:
        _pending_actions.pop(chat_id, None)


CURRENCY_NAMES = {
    "USD": "دولار",
    "IQD": "دينار عراقي",
    "SAR": "ريال سعودي",
    "AED": "درهم إماراتي",
    "EUR": "يورو",
    "GBP": "جنيه إسترليني",
}


def translate_currency(code):
    return CURRENCY_NAMES.get(code, code)


def ctr_label(ctr_value):
    """يوصف أداء نسبة النقر: زين فوق 10%، متوسط بين 5 و10%، ضعيف تحت 5%."""
    if ctr_value > 10:
        return "زين 👍"
    if ctr_value >= 5:
        return "متوسط 😐"
    return "ضعيف ⚠️"


def pct_change(today_val, yesterday_val):
    if not yesterday_val:
        return None
    change = ((today_val - yesterday_val) / yesterday_val) * 100
    arrow = "📈" if change > 0 else ("📉" if change < 0 else "➖")
    return f"{arrow} {change:+.1f}%"


def fmt_cmp(today_val, yesterday_val):
    result = pct_change(today_val, yesterday_val)
    return f" ({result})" if result else ""


# ---------- منطق الذكاء الاصطناعي (Gemini) ----------
GEMINI_STYLE_NOTE = (
    "رد بالعربي بلهجة عراقية واضحة ومباشرة، وبدون رموز تنسيق مثل ** أو # "
    "لأن الرد ينرسل كنص عادي بتيليجرام."
)


def call_gemini(prompt, temperature=0.4, max_output_tokens=500):
    """يرسل prompt لـ Gemini ويرجع النص المولد، أو None لو ماكو مفتاح أو صار خطأ."""
    if not GEMINI_API_KEY:
        return None

    try:
        resp = requests.post(
            f"{GEMINI_API_URL}?key={GEMINI_API_KEY}",
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": temperature,
                    "maxOutputTokens": max_output_tokens,
                    # نطفي التفكير الداخلي (thinking) عشان الرد يطلع مباشرة
                    # وما ياكل حصة الـ tokens قبل لا يوصل للجواب الفعلي
                    "thinkingConfig": {"thinkingLevel": "minimal"},
                },
            },
            timeout=30,
        )
        data = resp.json()
        if "error" in data:
            raise RuntimeError(data["error"].get("message"))

        candidates = data.get("candidates", []) or []
        if not candidates:
            return None

        parts = candidates[0].get("content", {}).get("parts", []) or []
        text = "".join(p.get("text", "") for p in parts).strip()
        text = text.replace("**", "").replace("__", "")  # تنظيف تنسيق ماركداون
        return text or None
    except Exception:
        log.error("فشل الاتصال بـ Gemini:\n" + traceback.format_exc())
        return None


def extract_json_object(text):
    """يستخرج أول كائن JSON صالح من رد Gemini، حتى لو ملفوف بـ ```json."""
    if not text:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        return json.loads(cleaned[start:end + 1])
    except Exception:
        return None


def detect_action_intent(text, campaigns):
    """يستخدم Gemini لتحديد هل الرسالة تطلب إجراء تنفيذي (وقف/تشغيل/ميزانية) أو مجرد سؤال عادي.
    يرجع بس النية المستخرجة — مطابقة اسم الحملة الفعلية تصير لاحقاً محلياً، ما نثق بأي ID يذكره Gemini."""
    if not GEMINI_API_KEY:
        return None

    names_list = "\n".join(f"- {c.get('name', '')}" for c in campaigns[:150]) or "(ماكو حملات حالياً)"
    prompt = (
        "انته محلل نوايا لبوت إدارة إعلانات. حدد هل رسالة المستخدم تحت تطلب "
        "تنفيذ إجراء فعلي على حملة إعلانية معينة (إيقاف الحملة، تشغيلها من "
        "جديد، أو تعديل ميزانيتها اليومية)، أو إنها مجرد سؤال أو دردشة عادية "
        "ما تطلب تنفيذ أي شي.\n\n"
        "أسماء الحملات الحالية المتوفرة بالحساب (استخدمها بس عشان تطابق اسم "
        "الحملة اللي يقصدها المستخدم، لا تخترع اسم مو موجود بالقائمة):\n"
        f"{names_list}\n\n"
        "رد بصيغة JSON فقط بدون أي نص أو شرح إضافي، بالضبط بهذا الشكل:\n"
        '{"is_action": true أو false, '
        '"action_type": "pause" أو "resume" أو "budget" أو null, '
        '"campaign_query": "اسم الحملة كما يقصدها المستخدم، مطابق لاسم من القائمة قدر الإمكان" أو null, '
        '"budget_direction": "increase" أو "decrease" أو null, '
        '"budget_mode": "percent" أو "amount" أو null, '
        '"budget_value": رقم أو null}\n\n'
        f"رسالة المستخدم: {text}"
    )
    raw = call_gemini(prompt, temperature=0.0, max_output_tokens=250)
    parsed = extract_json_object(raw)
    if not parsed or not parsed.get("is_action"):
        return None
    return parsed


def build_performance_context(insights, campaigns, currency):
    """يبني ملخص نصي لبيانات أداء اليوم والحملات عشان يصير سياق لـ Gemini."""
    lines = [
        "بيانات أداء اليوم لحساب الإعلانات 'عيون الفرسان':",
        f"- الإنفاق الكلي: {insights['spend']:.2f} {translate_currency(currency)}",
        f"- نسبة النقر الكلية: {insights['ctr']:.2f}%",
        f"- النقرات: {insights['clicks']}",
        f"- الظهور: {insights['impressions']}",
        f"- عدد النتائج: {insights['results']}",
    ]

    if campaigns:
        lines.append("تفصيل الحملات (الأعلى إنفاقاً أول):")
        for c in campaigns[:20]:
            lines.append(
                f"  * {c['name']}: إنفاق {c['spend']:.2f} {translate_currency(currency)}, "
                f"نسبة نقر {c['ctr']:.2f}%, نتائج {c['results']}"
            )
    else:
        lines.append("ماكو تفصيل حملات متوفر حالياً.")

    return "\n".join(lines)


def build_ai_recommendation(insights, campaigns, currency):
    """يطلب من Gemini اقتراح إجراء أو اثنين بناءً على بيانات الأداء — اقتراح فقط، بدون أي تنفيذ تلقائي."""
    context = build_performance_context(insights, campaigns, currency)
    prompt = (
        "انته محلل إعلانات محترف. بالاعتماد على بيانات الأداء تحت، اقترح "
        "إجراء واحد أو اثنين بس، واضحين ومباشرين، ممكن صاحب الحساب يسويهم "
        "لتحسين الأداء (مثلاً زيادة ميزانية حملة معينة، إيقاف حملة ضعيفة، "
        "تعديل استهداف...). هذا اقتراح فقط لصاحب القرار — ما تفترض إنه راح "
        "ينفذ تلقائياً، وما تطلب تأكيد أو تسأل أسئلة، بس اطرح الاقتراح "
        f"مباشرة بجملتين أو ثلاث كحد أقصى. {GEMINI_STYLE_NOTE}\n\n{context}"
    )
    return call_gemini(prompt, temperature=0.5, max_output_tokens=300)


def answer_performance_question(question, insights, campaigns, currency):
    """يرد على سؤال المستخدم الحر بالاعتماد على بيانات أداء اليوم."""
    context = build_performance_context(insights, campaigns, currency)
    prompt = (
        "انته مساعد ذكي متخصص بتحليل أداء حساب إعلاني، تجاوب على سؤال "
        "صاحب الحساب بالاعتماد على البيانات تحت فقط. لو السؤال مو متعلق "
        "بأداء الإعلانات، جاوب بأدب وذكّره إنه يقدر يسأل عن الأداء أو "
        f"يرسل /report. {GEMINI_STYLE_NOTE}\n\n"
        f"{context}\n\n"
        f"سؤال المستخدم: {question}"
    )
    return call_gemini(prompt)


def build_report_message():
    insights = get_today_insights()
    today_str = datetime.now().strftime("%Y-%m-%d")

    if insights is None:
        return (
            "📊 <b>تقرير اليوم — عيون الفرسان</b>\n"
            f"📅 {today_str}\n\n"
            "لا يوجد إنفاق أو بيانات مسجلة لليوم لحد الآن."
        )

    try:
        currency = get_account_currency()
    except Exception:
        log.error("فشل جلب العملة، راح نكمل بدونها:\n" + traceback.format_exc())
        currency = ""

    try:
        yesterday = get_yesterday_insights()
    except Exception:
        log.error("فشل جلب بيانات الأمس للمقارنة:\n" + traceback.format_exc())
        yesterday = None

    spend_str = f"{insights['spend']:.2f} {translate_currency(currency)}".strip()

    if yesterday:
        spend_cmp = fmt_cmp(insights['spend'], yesterday['spend'])
        ctr_cmp = fmt_cmp(insights['ctr'], yesterday['ctr'])
        clicks_cmp = fmt_cmp(insights['clicks'], yesterday['clicks'])
        results_cmp = fmt_cmp(insights['results'], yesterday['results'])
    else:
        spend_cmp = ctr_cmp = clicks_cmp = results_cmp = ""

    lines = [
        "📊 <b>تقرير اليوم — عيون الفرسان</b>",
        f"📅 {today_str}",
        "",
        f"💰 الإنفاق: <b>{spend_str}</b>{spend_cmp}",
        f"📈 نسبة النقر: <b>{insights['ctr']:.2f}%</b> ({ctr_label(insights['ctr'])}){ctr_cmp}",
        f"🖱️ النقرات: <b>{insights['clicks']}</b>{clicks_cmp}",
        f"👁️ الظهور: <b>{insights['impressions']}</b>",
        f"🎯 عدد النتائج: <b>{insights['results']}</b>{results_cmp}",
    ]

    try:
        campaigns = get_campaign_insights_for_period("today")
    except Exception:
        log.error("فشل جلب تفصيل الحملات:\n" + traceback.format_exc())
        campaigns = []

    if campaigns:
        # نحدد أعلى 20 حملة إنفاقاً بس عشان ما تتجاوز الرسالة حد تيليجرام (4096 حرف)
        shown, rest = campaigns[:20], campaigns[20:]

        lines.append("")
        lines.append("📋 <b>تفصيل الحملات</b>")
        for c in shown:
            spend_line = f"{c['spend']:.2f} {translate_currency(currency)}".strip()
            lines.append(
                f"🔸 <b>{html.escape(c['name'])}</b>\n"
                f"   💰 {spend_line} · 📈 نسبة النقر {c['ctr']:.2f}% ({ctr_label(c['ctr'])}) · 🎯 {c['results']} نتيجة"
            )
        if rest:
            lines.append(f"<i>و{len(rest)} حملة إضافية بإنفاق أقل...</i>")

    if yesterday is not None:
        lines.append("\n<i>النسب المئوية مقارنة بأمس بنفس الوقت</i>")

    try:
        ai_note = build_ai_recommendation(insights, campaigns, currency)
    except Exception:
        log.error("فشل تحليل Gemini للتقرير:\n" + traceback.format_exc())
        ai_note = None

    if ai_note:
        lines.append("")
        lines.append("🤖 <b>تحليل ذكي (اقتراح فقط، بدون أي تنفيذ تلقائي)</b>")
        lines.append(html.escape(ai_note))

    return "\n".join(lines)


# ---------- التقرير التلقائي المجدول ----------
def send_scheduled_report():
    try:
        report = build_report_message()
        bot.send_message(TARGET_CHAT_ID, report)
        log.info(f"تم إرسال التقرير التلقائي إلى {TARGET_CHAT_ID}")
    except Exception:
        log.error("فشل إرسال التقرير التلقائي:\n" + traceback.format_exc())


def run_scheduler():
    interval_seconds = REPORT_INTERVAL_HOURS * 3600
    log.info(f"جدولة التقارير التلقائية كل {REPORT_INTERVAL_HOURS} ساعة")
    while True:
        time.sleep(interval_seconds)
        send_scheduled_report()


# ---------- معالجة طلبات التنفيذ (وقف/تشغيل/تعديل ميزانية) ----------
def handle_pause_intent(message, campaign, currency):
    chat_id = message.chat.id
    status = campaign.get("effective_status")
    if status in ("PAUSED", "CAMPAIGN_PAUSED"):
        bot.reply_to(message, f"ℹ️ حملة '<b>{html.escape(campaign['name'])}</b>' موقوفة أصلاً.")
        return True

    spend_today = get_campaign_spend_today(campaign["id"])
    spend_note = f" (تصرف حالياً {spend_today:.2f} {translate_currency(currency)} يومياً)" if spend_today else ""

    set_pending_action(chat_id, {
        "type": "pause",
        "campaign_id": campaign["id"],
        "campaign_name": campaign["name"],
    })
    bot.reply_to(
        message,
        f"⏸️ راح أوقف حملة '<b>{html.escape(campaign['name'])}</b>'{spend_note}.\n\n"
        "اكتب <b>تأكيد</b> للمتابعة، أو أي رسالة ثانية للإلغاء (تنلغى تلقائياً بعد 5 دقائق بدون رد)."
    )
    return True


def handle_resume_intent(message, campaign, currency):
    chat_id = message.chat.id
    status = campaign.get("effective_status")
    if status == "ACTIVE":
        bot.reply_to(message, f"ℹ️ حملة '<b>{html.escape(campaign['name'])}</b>' شغالة أصلاً.")
        return True

    set_pending_action(chat_id, {
        "type": "resume",
        "campaign_id": campaign["id"],
        "campaign_name": campaign["name"],
    })
    bot.reply_to(
        message,
        f"▶️ راح أشغل حملة '<b>{html.escape(campaign['name'])}</b>' (حالتها الحالية: {status_ar(status)}).\n\n"
        "اكتب <b>تأكيد</b> للمتابعة، أو أي رسالة ثانية للإلغاء (تنلغى تلقائياً بعد 5 دقائق بدون رد)."
    )
    return True


def handle_budget_intent(message, campaign, currency, intent):
    chat_id = message.chat.id
    current_minor = campaign.get("daily_budget")
    if current_minor is None:
        bot.reply_to(
            message,
            f"⚠️ حملة '<b>{html.escape(campaign['name'])}</b>' ما عندها ميزانية يومية على "
            "مستوى الحملة نفسها (يمكن الميزانية محددة على مستوى المجموعة الإعلانية)، "
            "ما أقدر أعدلها من هنا.",
        )
        return True

    current_minor = int(current_minor)
    current_major = minor_to_major(current_minor, currency)

    direction = intent.get("budget_direction")
    mode = intent.get("budget_mode")
    value = intent.get("budget_value")

    if direction not in ("increase", "decrease") or mode not in ("percent", "amount") or value is None:
        bot.reply_to(
            message,
            "⚠️ ما فهمت التغيير المطلوب بالميزانية بدقة. حدد إذا تريد زيادة أو "
            "تقليل، وبنسبة مئوية أو مبلغ محدد. مثال: 'زود ميزانية حملة كذا 20%' "
            "أو 'قلل ميزانية حملة كذا 2 دولار'.",
        )
        return True

    try:
        value = float(value)
    except (TypeError, ValueError):
        bot.reply_to(message, "⚠️ القيمة المطلوبة للميزانية مو واضحة، حاول مرة ثانية برقم محدد.")
        return True

    if value <= 0:
        bot.reply_to(message, "⚠️ لازم تحدد قيمة أكبر من صفر.")
        return True

    if mode == "percent":
        if direction == "increase" and value > MAX_BUDGET_INCREASE_PCT:
            bot.reply_to(
                message,
                f"🚫 أقصى زيادة مسموحة دفعة وحدة هي {MAX_BUDGET_INCREASE_PCT}%. "
                "اطلب نسبة أقل أو سويها على دفعتين.",
            )
            return True
        factor = (1 + value / 100) if direction == "increase" else (1 - value / 100)
        new_major = current_major * factor
    else:  # amount
        max_increase = current_major * (MAX_BUDGET_INCREASE_PCT / 100)
        if direction == "increase" and value > max_increase:
            bot.reply_to(
                message,
                f"🚫 أقصى زيادة مسموحة دفعة وحدة هي {MAX_BUDGET_INCREASE_PCT}% من الميزانية "
                f"الحالية، يعني أقصى مبلغ ممكن تزيده هسه هو {max_increase:.2f} "
                f"{translate_currency(currency)}.",
            )
            return True
        new_major = current_major + value if direction == "increase" else current_major - value

    if new_major <= 0:
        bot.reply_to(message, "🚫 ما أقدر أخلي الميزانية صفر أو أقل من صفر.")
        return True

    new_minor = major_to_minor(new_major, currency)
    if new_minor <= 0:
        bot.reply_to(message, "🚫 القيمة الجديدة صغيرة جداً، حدد مبلغ أكبر.")
        return True

    set_pending_action(chat_id, {
        "type": "budget",
        "campaign_id": campaign["id"],
        "campaign_name": campaign["name"],
        "old_minor": current_minor,
        "new_minor": new_minor,
        "old_major": current_major,
        "new_major": new_major,
    })

    bot.reply_to(
        message,
        f"💰 راح أعدل ميزانية حملة '<b>{html.escape(campaign['name'])}</b>' اليومية:\n"
        f"من <b>{current_major:.2f}</b> إلى <b>{new_major:.2f}</b> {translate_currency(currency)}\n\n"
        "اكتب <b>تأكيد</b> للمتابعة، أو أي رسالة ثانية للإلغاء (تنلغى تلقائياً بعد 5 دقائق بدون رد)."
    )
    return True


def try_handle_action_request(message, text):
    """يحاول يكتشف نية تنفيذية بالرسالة وينشئ طلب تأكيد معلق إذا لقى وحدة صالحة.
    يرجع True لو تعامل مع الرسالة (حتى لو برفضها)، و False لو ما اكو نية تنفيذية أصلاً."""
    try:
        campaigns = get_all_campaigns()
    except Exception:
        log.error("فشل جلب قائمة الحملات لاكتشاف النية:\n" + traceback.format_exc())
        return False

    intent = detect_action_intent(text, campaigns)
    if not intent:
        return False

    action_type = intent.get("action_type")
    campaign_query = (intent.get("campaign_query") or "").strip()

    matches = find_matching_campaigns(campaign_query, campaigns)
    if not matches:
        bot.reply_to(
            message,
            f"❌ ما لقيت حملة اسمها يشبه '{html.escape(campaign_query)}'. تأكد من الاسم وحاول مرة ثانية.",
        )
        return True
    if len(matches) > 1:
        names = "\n".join(f"- {html.escape(c['name'])}" for c in matches[:10])
        bot.reply_to(
            message,
            f"⚠️ لقيت أكثر من حملة تنطبق على '{html.escape(campaign_query)}':\n{names}\n\nحدد الاسم بالضبط.",
        )
        return True

    campaign = matches[0]

    try:
        currency = get_account_currency()
    except Exception:
        currency = ""

    if action_type == "pause":
        return handle_pause_intent(message, campaign, currency)
    if action_type == "resume":
        return handle_resume_intent(message, campaign, currency)
    if action_type == "budget":
        return handle_budget_intent(message, campaign, currency, intent)
    return False


def execute_pending_action(message, pending):
    chat_id = message.chat.id
    action_type = pending["type"]
    campaign_id = pending["campaign_id"]
    campaign_name = pending["campaign_name"]

    try:
        if action_type == "pause":
            meta_pause_campaign(campaign_id)
            log_action(chat_id, "pause", campaign_name, campaign_id, "إيقاف الحملة", True)
            bot.reply_to(message, f"✅ تم إيقاف حملة '<b>{html.escape(campaign_name)}</b>'.")
        elif action_type == "resume":
            meta_resume_campaign(campaign_id)
            log_action(chat_id, "resume", campaign_name, campaign_id, "تشغيل الحملة", True)
            bot.reply_to(message, f"✅ تم تشغيل حملة '<b>{html.escape(campaign_name)}</b>'.")
        elif action_type == "budget":
            meta_set_campaign_budget(campaign_id, pending["new_minor"])
            details = f"من {pending['old_major']:.2f} إلى {pending['new_major']:.2f} (وحدات صغرى: {pending['old_minor']} -> {pending['new_minor']})"
            log_action(chat_id, "budget", campaign_name, campaign_id, details, True)
            bot.reply_to(message, f"✅ تم تعديل ميزانية حملة '<b>{html.escape(campaign_name)}</b>'.")
    except Exception as e:
        log.error(f"فشل تنفيذ إجراء {action_type} على {campaign_id}:\n" + traceback.format_exc())
        log_action(chat_id, action_type, campaign_name, campaign_id, "محاولة تنفيذ فاشلة", False, error=str(e))
        bot.reply_to(message, f"❌ فشل تنفيذ الإجراء: {html.escape(str(e))}")


# ---------- أوامر البوت ----------
WELCOME = (
    "أهلاً! 👋\n"
    "أنا بوت تقارير حساب <b>عيون الفرسان</b> الإعلاني.\n\n"
    "أرسل /report وراح أجيبلك أداء اليوم: الإنفاق، نسبة النقر، وعدد "
    "النتائج، مع تحليل ذكي واقتراحات.\n"
    "أو اسألني أي سؤال عن الأداء بلغتك العادية وراح أجاوبك.\n\n"
    "من نفس محادثة الإدارة، تقدر تطلب مني إجراء فعلي مثل: 'أوقف حملة كذا' "
    "أو 'شغل حملة كذا' أو 'زود ميزانية حملة كذا 15%' — راح ألخصلك بالضبط "
    "شنو راح يصير وتحتاج تكتب 'تأكيد' قبل ما أنفذ أي شي."
)


@bot.message_handler(commands=["start", "help"])
def handle_start(message):
    try:
        bot.reply_to(message, WELCOME)
    except Exception:
        log.error("فشل إرسال رسالة الترحيب:\n" + traceback.format_exc())


@bot.message_handler(commands=["report"])
def handle_report(message):
    try:
        bot.send_chat_action(message.chat.id, "typing")
    except Exception:
        pass

    try:
        report = build_report_message()
        bot.reply_to(message, report)
    except Exception:
        log.error(f"فشل بناء التقرير لـ {message.chat.id}:\n" + traceback.format_exc())
        bot.reply_to(
            message,
            "⚠️ صار خطأ بجلب بيانات الأداء من Meta. جرب مرة ثانية بعد شوي، "
            "وإذا تكررت المشكلة تأكد من صلاحية META_ACCESS_TOKEN.",
        )


@bot.message_handler(func=lambda m: True, content_types=["text"])
def handle_text(message):
    chat_id = message.chat.id
    text = (message.text or "").strip()

    # ---- الإجراءات التنفيذية مسموحة بس من محادثة الإدارة (TARGET_CHAT_ID) ----
    if chat_id == TARGET_CHAT_ID:
        pending = get_pending_action(chat_id)
        if pending is not None:
            if text == "تأكيد":
                clear_pending_action(chat_id)
                try:
                    bot.send_chat_action(chat_id, "typing")
                except Exception:
                    pass
                execute_pending_action(message, pending)
                return
            else:
                clear_pending_action(chat_id)
                try:
                    bot.reply_to(message, "❌ تم إلغاء الطلب المعلق.")
                except Exception:
                    log.error("فشل إرسال رسالة الإلغاء:\n" + traceback.format_exc())
                # نكمل تحت عادي — الرسالة الجديدة ممكن تكون سؤال أو طلب تنفيذي جديد

        if GEMINI_API_KEY:
            try:
                if try_handle_action_request(message, text):
                    return
            except Exception:
                log.error("فشل معالجة طلب تنفيذي:\n" + traceback.format_exc())
                bot.reply_to(message, "⚠️ صار خطأ بمعالجة الطلب التنفيذي. جرب مرة ثانية.")
                return

    if not GEMINI_API_KEY:
        try:
            bot.reply_to(message, "أرسل /report عشان أجيبلك تقرير أداء اليوم.")
        except Exception:
            log.error("فشل الرد على رسالة نصية:\n" + traceback.format_exc())
        return

    try:
        bot.send_chat_action(message.chat.id, "typing")
    except Exception:
        pass

    try:
        insights = get_today_insights()
        if insights is None:
            bot.reply_to(
                message,
                "ماكو بيانات أداء مسجلة لليوم لحد الآن. أرسل /report للتأكد.",
            )
            return

        try:
            currency = get_account_currency()
        except Exception:
            currency = ""

        try:
            campaigns = get_campaign_insights_for_period("today")
        except Exception:
            campaigns = []

        answer = answer_performance_question(message.text, insights, campaigns, currency)
        if answer:
            # parse_mode=None عشان رد Gemini الحر ما ينكسر لو فيه رمز HTML خاص
            bot.reply_to(message, answer, parse_mode=None)
        else:
            bot.reply_to(
                message,
                "⚠️ ما گدرت أوصل لـ Gemini هسه. جرب /report للتقرير المباشر.",
            )
    except Exception:
        log.error(f"فشل الرد الذكي لـ {message.chat.id}:\n" + traceback.format_exc())
        bot.reply_to(message, "⚠️ صار خطأ. جرب /report للتقرير المباشر.")


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
    threading.Thread(target=run_scheduler, daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
