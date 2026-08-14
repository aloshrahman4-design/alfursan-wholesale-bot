/**
 * بوت الجملة — يزامن 4 قنوات تليجرام مع موقع الجملة
 * كل قناة مختصة بصنف معين، والمنشورات صور بس بدون نص،
 * فالبوت يولّد اسم/رمز تلقائي لكل منتج (ح-1، ش-1...) عبر الدالة السحابية.
 */

const TelegramBot = require("node-telegram-bot-api");
const http = require("http");
const FormData = require("form-data");
const fetch = require("node-fetch");
const nativeFetch = global.fetch; // نستخدمه فقط لتحميل الملفات (أكثر استقراراً من node-fetch لطلبات GET البسيطة)

/* ══════════════ إعدادات ══════════════ */
const BOT_TOKEN = process.env.BOT_TOKEN || "";
const CLOUD_FUNCTION_URL =
  "https://addwholesaleproduct-799699952948.europe-west1.run.app";
const SHARED_SECRET = "azyaa-secret-2026-x9f";
const PORT = process.env.PORT || 10000;
const GEMINI_API_KEY = process.env.GEMINI_API_KEY || "";
const GEMINI_MODELS = ["gemini-flash-latest", "gemini-2.5-flash", "gemini-2.0-flash"];
function geminiUrl(model) {
  return `https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent?key=${GEMINI_API_KEY}`;
}
/* كل قناة: التصنيف المعروض بالموقع + رمز الترقيم التلقائي */
const CHANNELS = {
  AKiraq10:            { category: "أحذية وسليبرات", prefix: "ح" },
  fursanalyaqut:        { category: "شحاطات",          prefix: "ش" },
  alqurayshiljumla47:   { category: "منوعات",           prefix: "م" },
  dreambagstor:         { category: "حقائب",            prefix: "ق" },
};

/* ══════════════ تشغيل البوت ══════════════ */
if (!BOT_TOKEN) {
  console.error("❌ BOT_TOKEN غير موجود بمتغيرات البيئة! أضفه من Render → Environment وأعد النشر.");
  process.exit(1);
}
const bot = new TelegramBot(BOT_TOKEN, { polling: true });
console.log("✅ بوت الجملة شغّال ويستمع لـ", Object.keys(CHANNELS).length, "قنوات...");

bot.on("polling_error", (err) => {
  console.log("⚠️ خطأ اتصال بتلكرام:", err.message);
});

/* ══════════════ تجميع الألبومات (صور مع بعض بمنشور وحد) ══════════════ */
const pendingGroups = new Map(); // media_group_id -> { channel, items: [], timer }
const GROUP_WAIT_MS = 2500;

bot.on("channel_post", (msg) => {
  const channelUsername = msg.chat.username;
  if (!channelUsername || !CHANNELS[channelUsername]) return; // قناة غير مسجلة، نتجاهلها

  const photo = msg.photo ? msg.photo[msg.photo.length - 1] : null; // أعلى دقة
  const video = msg.video || null;

  if (!photo && !video) return; // منشور بدون وسائط (نص بس)، نتجاهله

  const groupId = msg.media_group_id || `single_${msg.message_id}`;

  if (!pendingGroups.has(groupId)) {
    pendingGroups.set(groupId, { channel: channelUsername, photos: [], video: null, timer: null });
  }
  const group = pendingGroups.get(groupId);
  if (photo) group.photos.push(photo.file_id);
  if (video) group.video = video.file_id;

  clearTimeout(group.timer);
  group.timer = setTimeout(() => finalizeGroup(groupId), GROUP_WAIT_MS);
});

async function finalizeGroup(groupId) {
  const group = pendingGroups.get(groupId);
  pendingGroups.delete(groupId);
  if (!group || !group.photos.length) return;

  const { category: defaultCategory, prefix } = CHANNELS[group.channel];
  console.log(`⏳ معالجة منشور من ${group.channel}: ${group.photos.length} صورة${group.video ? " + فيديو" : ""}`);

  try {
    const firstImageBuf = await downloadTelegramFile(group.photos[0]);
    const category = await classifyImage(firstImageBuf, defaultCategory);

    const fd = new FormData();
    fd.append("secret", SHARED_SECRET);
    fd.append("category", category);
    fd.append("code_prefix", prefix);

    fd.append("images", firstImageBuf, { filename: `img0.jpg`, contentType: "image/jpeg" });
    for (let i = 1; i < group.photos.length; i++) {
      const buf = await downloadTelegramFile(group.photos[i]);
      fd.append("images", buf, { filename: `img${i}.jpg`, contentType: "image/jpeg" });
    }
    if (group.video) {
      const buf = await downloadTelegramFile(group.video);
      fd.append("video", buf, { filename: "video.mp4", contentType: "video/mp4" });
    }

    const headers = fd.getHeaders();
    headers["Content-Length"] = fd.getLengthSync();
    const res = await fetch(CLOUD_FUNCTION_URL, { method: "POST", body: fd, headers });
    const data = await res.json();

    if (data.success) {
      console.log(`✅ انضاف المنتج: ${data.name} (${group.channel})`);
    } else {
      console.log(`❌ رفضت الدالة السحابية: ${data.error}`);
    }
  } catch (err) {
    console.log("❌ خطأ بمعالجة المنشور:", err.message, "| code:", err.code || err.type || "-", "| cause:", err.cause?.message || err.cause?.code || "-");
  }
}

/** يستخدم Gemini Vision لتحديد تصنيف دقيق للمنتج من الصورة، مع رجوع للتصنيف الافتراضي عند أي خطأ */
async function classifyImage(buffer, fallbackCategory) {
  if (!GEMINI_API_KEY) {
    console.log("⚠️ GEMINI_API_KEY غير موجود بمتغيرات البيئة — استخدام التصنيف الافتراضي");
    return fallbackCategory;
  }
  try {
    const base64 = buffer.toString("base64");
    const prompt =
      "شوف هذي الصورة لمنتج (حذاء، شحاطة، سليبر، أو حقيبة) وحدد تصنيف دقيق ومختصر له بالعربي " +
      "(مثال: شحاطة أطفال، حذاء رياضي رجالي، سليبر نسائي، حقيبة يد، حذاء أطفال). " +
      "رد بس بالتصنيف نفسه، بدون أي شرح إضافي، بجملة قصيرة (2-4 كلمات).";

    const res = await fetch(GEMINI_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        contents: [{
          parts: [
            { text: prompt },
            { inline_data: { mime_type: "image/jpeg", data: base64 } },
          ],
        }],
      }),
    });
    const data = await res.json();
    const text = data?.candidates?.[0]?.content?.parts?.[0]?.text?.trim();
    if (text && text.length < 40) {
      console.log(`🧠 تصنيف Gemini: ${text}`);
      return text;
    }
    console.log("⚠️ رد Gemini غير متوقع:", JSON.stringify(data).slice(0, 300));
  } catch (err) {
    console.log("⚠️ فشل تصنيف الصورة بالذكاء الاصطناعي:", err.message);
  }
  return fallbackCategory; // احتياطي عند أي فشل
}

/** يحمّل ملف من تليجرام عبر file_id ويرجعه كـ Buffer (مع إعادة محاولة وحدة عند الفشل) */
async function downloadTelegramFile(fileId, attempt = 1) {
  const file = await bot.getFile(fileId);
  const url = `https://api.telegram.org/file/bot${BOT_TOKEN}/${file.file_path}`;
  try {
    const doFetch = nativeFetch || fetch;
    const res = await doFetch(url);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const arrayBuf = await res.arrayBuffer();
    return Buffer.from(arrayBuf);
  } catch (err) {
    if (attempt < 3) {
      console.log(`⚠️ محاولة تحميل ملف فشلت (${attempt})، إعادة المحاولة...`);
      await new Promise((r) => setTimeout(r, 1000 * attempt));
      return downloadTelegramFile(fileId, attempt + 1);
    }
    throw err;
  }
}

/* ══════════════ خادم فحص صحي (Render يحتاجه + UptimeRobot) ══════════════ */
http
  .createServer((req, res) => {
    res.writeHead(200, { "Content-Type": "text/plain" });
    res.end("بوت الجملة شغّال");
  })
  .listen(PORT, () => console.log(`🌐 خادم الفحص الصحي شغّال على المنفذ ${PORT}`));

process.on("SIGTERM", () => {
  console.log("👋 إيقاف بوت الجملة...");
  bot.stopPolling();
  process.exit(0);
});
