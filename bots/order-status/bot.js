/**
 * بوت واتساب لتتبع حالة الطلبات — أزياء الشيخة والأميرات
 * الزبونة ترسل رقم الطلب (4 رموز) لهذا الرقم، والبوت يرد بحالته الحالية.
 * رقم مخصص للتتبع فقط، منفصل تماماً عن أرقام استلام الطلبات الحقيقية.
 */

const {
  default: makeWASocket,
  DisconnectReason,
  useMultiFileAuthState,
} = require("@whiskeysockets/baileys");
const QRCode = require("qrcode");
const http = require("http");

const PORT = process.env.PORT || 10000;
const LOOKUP_URL = "https://europe-west1-alfursan-market.cloudfunctions.net/lookupOrderStatus";
const CODE_RE = /\b[23456789ABCDEFGHJKLMNPQRSTUVWXYZ]{4}\b/i;

const STATUS_LABELS = {
  "تجهيز": "🛠️ طلبج قيد التجهيز",
  "بالشحن": "🚚 طلبج بالطريق",
  "تم التسليم": "✅ طلبج تم تسليمه",
};

let lastQR = null;
let connected = false;

/* ══════════════ خادم فحص صحي + عرض رمز QR للربط ══════════════ */
http
  .createServer(async (req, res) => {
    if (connected) {
      res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
      return res.end("<h2>بوت تتبع الطلبات متصل وشغال ✅</h2>");
    }
    if (lastQR) {
      const qrImg = await QRCode.toDataURL(lastQR);
      res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
      return res.end(`
        <body style="font-family:sans-serif;text-align:center;padding:40px">
          <h2>امسحي رمز QR بواتساب رقم التتبع (٧٨٥٥٦٨٨٧٤٧)</h2>
          <p>الإعدادات ← الأجهزة المرتبطة ← ربط جهاز</p>
          <img src="${qrImg}" style="width:280px">
          <p>الصفحة تحدّث نفسها كل 10 ثواني...</p>
          <script>setTimeout(()=>location.reload(), 10000)</script>
        </body>`);
    }
    res.writeHead(200, { "Content-Type": "text/plain; charset=utf-8" });
    res.end("بوت تتبع الطلبات: جاري التجهيز...");
  })
  .listen(PORT, () => console.log(`🌐 خادم الفحص الصحي شغّال على المنفذ ${PORT}`));

/* ══════════════ الاتصال بواتساب ══════════════ */
async function start() {
  const authDir = process.env.AUTH_STATE_DIR || "auth_state";
  const { state, saveCreds } = await useMultiFileAuthState(authDir);
  const sock = makeWASocket({ auth: state, printQRInTerminal: false });

  sock.ev.on("creds.update", saveCreds);

  sock.ev.on("connection.update", (update) => {
    const { connection, lastDisconnect, qr } = update;
    if (qr) {
      lastQR = qr;
      console.log("📷 رمز QR جاهز — افتحي رابط الخدمة بالمتصفح لمسحه");
    }
    if (connection === "open") {
      connected = true;
      lastQR = null;
      console.log("✅ البوت متصل بواتساب");
    }
    if (connection === "close") {
      connected = false;
      const shouldReconnect =
        lastDisconnect?.error?.output?.statusCode !== DisconnectReason.loggedOut;
      console.log("⚠️ انقطع الاتصال، إعادة محاولة:", shouldReconnect);
      if (shouldReconnect) start();
    }
  });

  sock.ev.on("messages.upsert", async ({ messages, type }) => {
    if (type !== "notify") return;
    for (const msg of messages) {
      if (msg.key.fromMe || !msg.message) continue;
      const from = msg.key.remoteJid;
      if (!from || from.endsWith("@g.us")) continue; // نتجاهل رسائل الكروبات

      const text =
        msg.message.conversation ||
        msg.message.extendedTextMessage?.text ||
        "";
      const match = text.match(CODE_RE);

      if (!match) {
        await sock.sendMessage(from, {
          text: "السلام عليكم 🌸\nأرسلي رقم تتبع طلبج (4 رموز، مثال: A3F9) وراح أرد عليج بحالته.",
        });
        continue;
      }

      const code = match[0].toUpperCase();
      try {
        const res = await fetch(LOOKUP_URL, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ orderNumber: code }),
        });
        const data = await res.json();

        if (data.success) {
          const label = STATUS_LABELS[data.status] || data.status;
          await sock.sendMessage(from, {
            text: `رقم الطلب: ${data.orderNumber}\n${label}`,
          });
        } else {
          await sock.sendMessage(from, {
            text: `ماكو طلب برقم ${code}، تأكدي من الرقم وحاولي مرة ثانية 🙏`,
          });
        }
      } catch (err) {
        console.log("❌ خطأ بالاستعلام:", err.message);
        await sock.sendMessage(from, {
          text: "صار خطأ مؤقت، حاولي بعد شوي 🙏",
        });
      }
    }
  });
}

start();

process.on("SIGTERM", () => {
  console.log("👋 إيقاف البوت...");
  process.exit(0);
});
