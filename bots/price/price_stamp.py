"""
Price Stamp
يحدد أفضل زاوية فاضية بالصورة، ويحط صندوق أسود بنص ذهبي فيها.
"""
from PIL import Image, ImageDraw, ImageFont, ImageOps
import numpy as np
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FONT_PATH = os.path.join(BASE_DIR, "Cairo.ttf")
BOX_COLOR = (10, 10, 10, 235)       # أسود شبه معتم
TEXT_COLOR = (255, 200, 40)         # ذهبي/أصفر
PADDING = 28
CORNER_MARGIN_RATIO = 0.03          # مسافة الصندوق عن حافة الصورة
MAX_LINES = 4                        # حماية من نص طويل جداً يخرب التصميم
MAX_BOX_WIDTH_RATIO = 0.85           # الصندوق ما يتعدى 85% من عرض الصورة


def get_bold_font(size):
    # خط Cairo (وأغلب الخطوط العربية الحديثة) ما يحتوي أشكال حروف جاهزة
    # بديلة - يعتمد بالكامل على محرك تشكيل حقيقي (raqm) يوصل الحروف
    # ويرتبها صح وقت الرسم. لازم يكون raqm متوفر بالسيرفر (نضمنه عبر
    # تثبيت نسخة بايثون مستقرة بملف runtime.txt).
    font = ImageFont.truetype(FONT_PATH, size, layout_engine=ImageFont.Layout.RAQM)
    try:
        font.set_variation_by_name("Bold")
    except Exception:
        pass
    return font


def shape_arabic(text):
    # النص يمرر كما هو، وRaqm يتكفل بالتشكيل والاتجاه الصحيح أثناء الرسم
    return text


def find_best_corner(img, box_w, box_h):
    """يفحص أركان الصورة الأربعة ويرجع أهدأ ركن (أقل تفاصيل/تباين)."""
    gray = np.array(img.convert("L"))
    h, w = gray.shape
    corners = {
        "top_left": gray[0:box_h, 0:box_w],
        "top_right": gray[0:box_h, w - box_w:w],
        "bottom_left": gray[h - box_h:h, 0:box_w],
        "bottom_right": gray[h - box_h:h, w - box_w:w],
    }
    variances = {name: region.std() for name, region in corners.items()}
    best = min(variances, key=variances.get)
    return best, variances


def wrap_text_lines(text):
    # النص ممكن يجي بأكثر من سطر (يفصلها المستخدم بـ \n)
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    return lines[:MAX_LINES]


def draw_price_box(image_path, price_text, output_path):
    img = Image.open(image_path)
    img = ImageOps.exif_transpose(img)  # يصحح دوران الصورة حسب بيانات EXIF (صور الموبايل)
    img = img.convert("RGBA")
    w, h = img.size

    lines = wrap_text_lines(price_text)
    if not lines:
        lines = ["بدون سعر"]  # حماية احتياطية، ما يفترض يصير لأن البوت يتحقق قبل

    font_size = max(28, int(h * 0.045))
    max_box_w = int(w * MAX_BOX_WIDTH_RATIO)

    tmp_draw = ImageDraw.Draw(img)

    # نقلل حجم الخط تدريجياً لو النص أعرض من المسموح بالصورة
    while True:
        font = get_bold_font(font_size)
        shaped_lines = [shape_arabic(line) for line in lines]
        line_sizes = [tmp_draw.textbbox((0, 0), sl, font=font) for sl in shaped_lines]
        text_w = max(b[2] - b[0] for b in line_sizes)
        box_w = text_w + PADDING * 2
        if box_w <= max_box_w or font_size <= 14:
            break
        font_size -= 2

    line_h = max(b[3] - b[1] for b in line_sizes)
    line_spacing = int(line_h * 0.35)
    total_text_h = len(lines) * line_h + (len(lines) - 1) * line_spacing
    box_h = total_text_h + PADDING * 2

    corner, variances = find_best_corner(img, box_w, box_h)
    margin_x = int(w * CORNER_MARGIN_RATIO)
    margin_y = int(h * CORNER_MARGIN_RATIO)

    if corner == "top_left":
        box_xy = (margin_x, margin_y)
    elif corner == "top_right":
        box_xy = (w - box_w - margin_x, margin_y)
    elif corner == "bottom_left":
        box_xy = (margin_x, h - box_h - margin_y)
    else:
        box_xy = (w - box_w - margin_x, h - box_h - margin_y)

    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    odraw = ImageDraw.Draw(overlay)
    box_rect = [box_xy[0], box_xy[1], box_xy[0] + box_w, box_xy[1] + box_h]
    radius = int(box_h * 0.15)
    odraw.rounded_rectangle(box_rect, radius=radius, fill=BOX_COLOR)

    ty = box_xy[1] + PADDING
    for sl, b in zip(shaped_lines, line_sizes):
        lw = b[2] - b[0]
        tx = box_xy[0] + (box_w - lw) / 2
        odraw.text((tx, ty), sl, font=font, fill=TEXT_COLOR)
        ty += line_h + line_spacing

    result = Image.alpha_composite(img, overlay).convert("RGB")
    result.save(output_path, quality=95)
    return corner, variances


if __name__ == "__main__":
    import sys
    image_path, price_text, output_path = sys.argv[1], sys.argv[2], sys.argv[3]
    corner, variances = draw_price_box(image_path, price_text, output_path)
    print(f"Best corner: {corner}")
    print(f"Variances: {variances}")
