/**
 * بوت الجملة — يزامن 4 قنوات تليجرام مع موقع الجملة
 * كل قناة مختصة بصنف معين، والمنشورات صور بس بدون نص،
 * فالبوت يولّد اسم/رمز تلقائي لكل منتج (ح-1، ش-1...) عبر الدالة السحابية.
 */

const TelegramBot = require("node-telegram-bot-api");
const http = require("http");
const FormData = require("form-data");
const fetch = require("node-fetch");

/* ══════════════ إعدادات ══════════════ */
const BOT_TOKEN = process.env.BOT_TOKEN || "8716372882:AAELsj9Sc5eDUZz9QxikcXicm0qLHyOV4q8";
const CLOUD_FUNCTION_URL =
  "https://addwholesaleproduct-799699952948.europe-west1.run.app";
const SHARED_SECRET = "azyaa-secret-2026-x9f";
const PORT = process.env.PORT || 10000;

/* كل قناة: التصنيف المعروض بالموقع + رمز الترقيم التلقائي */
const CHANNELS = {
  AKiraq10:            { category: "أحذية وسليبرات", prefix: "ح" },
  fursanalyaqut:        { category: "شحاطات",          prefix: "ش" },
  alqurayshiljumla47:   { category: "منوعات",           prefix: "م" },
  dreambagstor:         { category: "حقائب",            prefix: "ق" },
};

/* ══════════════ تشغيل البوت ══════════════ */
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

  const { category, prefix } = CHANNELS[group.channel];
  console.log(`⏳ معالجة منشور من ${group.channel}: ${group.photos.length} صورة${group.video ? " + فيديو" : ""}`);

  try {
    const fd = new FormData();
    fd.append("secret", SHARED_SECRET);
    fd.append("category", category);
    fd.append("code_prefix", prefix);

    for (let i = 0; i < group.photos.length; i++) {
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
    console.log("❌ خطأ بمعالجة المنشور:", err.message);
  }
}

/** يحمّل ملف من تليجرام عبر file_id ويرجعه كـ Buffer */
async function downloadTelegramFile(fileId) {
  const file = await bot.getFile(fileId);
  const url = `https://api.telegram.org/file/bot${BOT_TOKEN}/${file.file_path}`;
  const res = await fetch(url);
  const arrayBuf = await res.arrayBuffer();
  return Buffer.from(arrayBuf);
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
