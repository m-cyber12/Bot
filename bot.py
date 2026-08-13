# -*- coding: utf-8 -*-
"""
ربات تلگرام «تحلیلگر شخصیت احمقانه»
=====================================
کاربر اسم یا عکس پروفایلش رو می‌فرسته، ربات یه تحلیل طنز و کاملاً بی‌معنی
ولی «به نظر علمی» بر اساس یه الگوریتم ساده (هش حروف اسم / آیدی عددی)
تولید می‌کنه. نتیجه برای هر ورودی ثابته (deterministic) پس قابل اشتراک‌گذاریه.

ویژگی‌های رشد کاربر:
- حالت Inline: تایپ @نام_ربات یه‌اسم توی هر چتی -> نتیجه فوری
- خروجی به‌صورت کارت تصویری آماده برای استوری (در صورت نصب Pillow + فونت فارسی)
- دکمه «اشتراک‌گذاری نتیجه» با متن آماده برای فوروارد/کپی
- سیستم دعوت با لینک اختصاصی هر کاربر + لیدربورد دعوت‌کننده‌ها
- محدودیت روزانه‌ی تحلیل رایگان که با دعوت دوستان باز می‌شه (Growth Loop)
- ذخیره‌سازی ساده در SQLite (تعداد کاربران، تعداد دعوت‌ها، مصرف روزانه)
- دستور /stats برای ادمین

اجرا (حالت polling - لوکال یا Background Worker):
    pip install -r requirements.txt
    export BOT_TOKEN="۱۲۳۴۵۶:ABC..."
    export ADMIN_ID="123456789"        # آیدی عددی تلگرام خودت (اختیاری، برای /stats)
    export DAILY_FREE_USES="3"          # تعداد تحلیل رایگان روزانه (اختیاری، پیش‌فرض 3)
    export FONT_PATH="fonts/Vazirmatn-Bold.ttf"  # فونت فارسی برای خروجی تصویری (اختیاری)
    python bot.py

اجرا (حالت webhook - برای Web Service که باید به یه پورت HTTP گوش بده):
    ربات خودش تشخیص می‌ده باید webhook باشه یا polling:
    - اگه متغیر محیطی WEBHOOK_URL (یا RENDER_EXTERNAL_URL که خود Render خودکار ست می‌کنه)
      وجود داشته باشه -> حالت webhook فعال می‌شه و به PORT گوش می‌ده (health check رو هم پاس می‌کنه)
    - در غیر این صورت -> حالت polling معمولی (مناسب اجرای لوکال یا Background Worker)
"""

import hashlib
import io
import logging
import os
import random
import sqlite3
import time
from datetime import date

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputTextMessageContent,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    InlineQueryHandler,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------------------
# رندر کارت تصویری (اختیاری - در صورت نبود Pillow یا فونت، به‌صورت متنی کار می‌کنه)
# ---------------------------------------------------------------------------
try:
    from PIL import Image, ImageDraw, ImageFont

    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

try:
    import arabic_reshaper
    from bidi.algorithm import get_display

    RTL_AVAILABLE = True
except ImportError:
    RTL_AVAILABLE = False

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "PUT_YOUR_TOKEN_HERE")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0") or 0)
DAILY_FREE_USES = int(os.environ.get("DAILY_FREE_USES", "3"))
BONUS_PER_INVITE = int(os.environ.get("BONUS_PER_INVITE", "2"))
FONT_PATH = os.environ.get(
    "FONT_PATH", os.path.join(os.path.dirname(__file__), "fonts", "Vazirmatn-Bold.ttf")
)
DB_PATH = os.environ.get(
    "DB_PATH", os.path.join(os.path.dirname(__file__), "bot_data.db")
)

# آدرس عمومی سرویس، برای حالت webhook. روی Render (نوع Web Service) این متغیر
# به‌صورت خودکار توسط خود پلتفرم ست می‌شه؛ برای پلتفرم‌های دیگه می‌تونی WEBHOOK_URL
# رو دستی بدی. اگه هیچ‌کدوم ست نباشن، ربات به‌صورت polling اجرا می‌شه.
WEBHOOK_URL = os.environ.get("WEBHOOK_URL") or os.environ.get("RENDER_EXTERNAL_URL")
PORT = int(os.environ.get("PORT", "10000"))
# مسیر مخفی وبهوک؛ استفاده از خود توکن باعث می‌شه حدس زدنش عملاً غیرممکن باشه
WEBHOOK_PATH = hashlib.sha256(BOT_TOKEN.encode()).hexdigest()[:32]

# ---------------------------------------------------------------------------
# دیتابیس: کاربران، دعوت‌ها، مصرف روزانه و اعتبار جایزه‌ای
# ---------------------------------------------------------------------------

def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_seen INTEGER,
            invited_by INTEGER,
            invite_count INTEGER DEFAULT 0,
            usage_date TEXT DEFAULT '',
            uses_today INTEGER DEFAULT 0,
            bonus_credits INTEGER DEFAULT 0
        )"""
    )
    conn.commit()
    return conn


def register_user(user_id: int, username: str, invited_by: int | None):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    is_new = row is None
    if is_new:
        cur.execute(
            "INSERT INTO users (user_id, username, first_seen, invited_by) VALUES (?,?,?,?)",
            (user_id, username, int(time.time()), invited_by),
        )
        if invited_by and invited_by != user_id:
            cur.execute(
                "UPDATE users SET invite_count = invite_count + 1, bonus_credits = bonus_credits + ? "
                "WHERE user_id=?",
                (BONUS_PER_INVITE, invited_by),
            )
        conn.commit()
    else:
        # آپدیت یوزرنیم در صورت تغییر
        cur.execute("UPDATE users SET username=? WHERE user_id=?", (username, user_id))
        conn.commit()
    conn.close()
    return is_new


def get_stats():
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM users")
    total = cur.fetchone()[0]
    cur.execute(
        "SELECT username, invite_count FROM users ORDER BY invite_count DESC LIMIT 10"
    )
    top = cur.fetchall()
    conn.close()
    return total, top


def check_and_consume_quota(user_id: int) -> tuple[bool, int, int]:
    """
    بررسی می‌کنه کاربر امروز هنوز سهمیه‌ی رایگان یا اعتبار جایزه‌ای داره یا نه.
    در صورت داشتن، یه واحد مصرف می‌کنه.
    خروجی: (اجازه_داره, تعداد_رایگان_باقیمانده, اعتبار_جایزه‌ای_باقیمانده)
    """
    today = date.today().isoformat()
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "SELECT usage_date, uses_today, bonus_credits FROM users WHERE user_id=?",
        (user_id,),
    )
    row = cur.fetchone()
    if row is None:
        conn.close()
        return True, DAILY_FREE_USES - 1, 0

    usage_date, uses_today, bonus_credits = row
    if usage_date != today:
        uses_today = 0
        usage_date = today

    if uses_today < DAILY_FREE_USES:
        uses_today += 1
        cur.execute(
            "UPDATE users SET usage_date=?, uses_today=? WHERE user_id=?",
            (usage_date, uses_today, user_id),
        )
        conn.commit()
        conn.close()
        return True, DAILY_FREE_USES - uses_today, bonus_credits

    if bonus_credits > 0:
        bonus_credits -= 1
        cur.execute(
            "UPDATE users SET usage_date=?, uses_today=?, bonus_credits=? WHERE user_id=?",
            (usage_date, uses_today, bonus_credits, user_id),
        )
        conn.commit()
        conn.close()
        return True, 0, bonus_credits

    conn.close()
    return False, 0, 0


# ---------------------------------------------------------------------------
# «الگوریتم» طنز تحلیل شخصیت
# ---------------------------------------------------------------------------

CATEGORIES = {
    "حیوان زندگی قبلی 🧬": [
        "پنگوئنی که از سرما فرار کرده برزیل",
        "گربه‌ای که وسط جلسه کاری خوابیده",
        "طوطی‌ای که فقط جمله‌های ناقص یاد گرفته",
        "لاک‌پشتی که همیشه دیر رسیده ولی خودشو برنده می‌دونه",
        "کلاغی که رمز عبور همه رو حفظ بوده",
        "پاندایی که فقط برای عکس گرفتن بیدار می‌شده",
        "روباهی که در حال فرار از قسط بانک بوده",
        "خرگوشی که مسابقه رو باخته ولی هنوز داستان می‌گه چقدر نزدیک بود",
    ],
    "ابرقدرت 🚀": [
        "می‌تونه فقط وقتی هیچ‌کی نگاهش نمی‌کنه، اشیا رو جابجا کنه",
        "می‌تونه دقیقاً حدس بزنه پیام تایپ‌شده بعدی چیه، ولی نمی‌فرسته",
        "قدرت پیدا کردن جای پارک، فقط وقتی عجله نداره",
        "توانایی به خواب رفتن دقیقاً وسط فیلم مهم",
        "قدرت شنیدن صدای گشنگی یخچال از هر نقطه خونه",
        "می‌تونه با یه نگاه، وای‌فای رو قطع کنه",
        "توانایی گم کردن جوراب جفتش در ابعاد موازی",
    ],
    "غذای روح 🍕": [
        "پیتزایی که همیشه یه تکه کمتر از تعداد آدماس",
        "چایی‌ای که همیشه یا خیلی داغه یا سرد شده",
        "کیک تولدی که همه اول از روش برداشتن",
        "آبمیوه‌ای که تهش شکر ننشسته باشه پیدا نمی‌شه",
        "نون تازه‌ای که سر راه خونه نصفش تموم می‌شه",
        "چیپسی که همیشه ته کیسه‌اش خرده‌ریزه‌اس",
    ],
    "نقش در فیلم زندگیش 🎬": [
        "کسی که توی صحنه اکشن یهو یادش می‌ره شارژرشو کجا گذاشته",
        "دوست صمیمی قهرمان که هیچوقت اسمش رو تیتراژ درست نمی‌نویسن",
        "شخصیتی که هر بار قراره بمیره ولی یه بهونه پیدا می‌کنه",
        "کسی که وسط دیالوگ مهم گوشیش زنگ می‌خوره",
        "قهرمانی که ابرقدرتش رو یادش می‌ره دقیقاً لحظه حساس",
        "شخصیت فرعی که آخر فیلم معلوم می‌شه از اول همه‌چی رو می‌دونسته",
    ],
    "شغل در دنیای موازی 💼": [
        "تستر رسمی نمک غذا در رستوران‌های نجومی",
        "مسئول هماهنگی خواب گربه‌های شهر",
        "مترجم رسمی زبان سکوت بین دو نفر که قهرن",
        "کارشناس ارشد پیدا کردن ریموت گمشده",
        "متخصص باز کردن بسته‌بندی پلاستیکی سرسخت",
        "مدیرعامل شرکت تولید بهانه برای دیر رسیدن",
    ],
    "کلاس RPG ⚔️": [
        "جادوگری که طلسمش همیشه یه چیز دیگه می‌شه",
        "جنگجویی که شمشیرشو خونه جا گذاشته",
        "دزدی که فقط بیسکوییت می‌دزده",
        "شفادهنده‌ای که خودش همیشه اول می‌میره",
        "کماندار دقیقی که فقط تارگت اشتباه می‌زنه",
    ],
    "رنگ هاله ✨": [
        "بنفش با گلیتر اضافه",
        "نارنجی مثل نور غروب ولی همیشه یه ذره کج",
        "سبز یشمی با یه لکه قهوه ریخته روش",
        "آبی آروم که یهو قرمز می‌شه وقتی گشنشه",
        "طلایی محو، فقط صبح‌ها قابل دیدنه",
    ],
    "جمله‌ای که هیچوقت نگفته 💬": [
        "«الان وقت زیاد دارم»",
        "«باشه یه قسمت دیگه فقط و می‌خوابم»",
        "«نه لازم نیست دوباره چک کنم»",
        "«فردا حتماً زودتر می‌خوابم»",
        "«این آخرین باره که تنبلی می‌کنم»",
    ],
    "استیکر تلگرامی موردعلاقه‌اش 🐣": [
        "اونی که فقط تو دعوا می‌فرسته",
        "اونی که هیچکس معنیشو نمی‌فهمه ولی همیشه به‌جا میاد",
        "همون گربه‌ای که داره گریه می‌کنه پشت فرمون",
        "اونی که فقط برای وقتی که جواب نداره می‌فرسته",
    ],
    "دلیل واقعی دیر اومدنش ⏰": [
        "گشتن دنبال جفت جوراب برای نیم ساعت",
        "یه سریال نیم‌ساعته که آخرش دیده",
        "بحث با گوگل‌مپ سر بهترین مسیر",
        "چک کردن یخچال به امید معجزه",
        "آماده شدن سه بار با سه استایل مختلف",
    ],
    "چیزی که تو کمدش قایم کرده 🗄️": [
        "یه پاکت چیپس که برای روز بد نگه داشته",
        "شارژری که مال گوشی قبلیشه ولی دلش نمیاد دورش بندازه",
        "لباسی که فقط یه بار پوشیده و منتظره لاغر بشه بپوشدش",
        "دفترچه‌ای با یه صفحه برنامه‌ریزی و بقیه خالی",
    ],
    "طالع امروزش با شانس فردا 🔮": [
        "امروز روز خوبیه برای نگفتن نظر واقعیت",
        "فردا یه پیام قدیمی یهو جواب می‌گیره",
        "امروز یکی ازت تشکر می‌کنه که یادت نمیاد چرا",
        "فردا دقیقاً همون ساعتی بیدار می‌شی که نمی‌خواستی",
        "امروز یه چیزی که گم کرده بودی، جایی که هزار بار گشتی پیدا می‌شه",
    ],
}

INTRO_LINES = [
    "بر اساس الگوریتم فوق‌پیشرفته‌ی *کاملاً علمی* من 🧠✨، این نتیجه‌ی تحلیل شخصیتته:",
    "پردازش کوانتومی روی اسمت انجام شد، نتیجه اینه 😎:",
    "هوش مصنوعی داخلیم (که فقط بلده حدس بزنه) این رو گفت:",
]


def deterministic_seed(text: str) -> int:
    """تولید یه عدد ثابت از روی متن، تا نتیجه برای هر کاربر همیشه یکی باشه."""
    h = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return int(h[:16], 16)


def build_result(name: str, extra_seed: str = "") -> dict:
    """خروجی ساختاریافته: دسته‌ها + درصد شباهت. هم برای متن و هم برای تصویر استفاده می‌شه."""
    seed_val = deterministic_seed(f"{name}|{extra_seed}")
    rng = random.Random(seed_val)
    picked = {cat: rng.choice(options) for cat, options in CATEGORIES.items()}
    similarity = seed_val % 101
    return {"name": name, "items": picked, "similarity": similarity}


def format_as_text(result: dict) -> str:
    rng = random.Random(deterministic_seed(result["name"]))
    lines = [rng.choice(INTRO_LINES), ""]
    for cat, choice in result["items"].items():
        lines.append(f"*{cat}*\n{choice}")
    lines.append("")
    lines.append(f"📊 *درصد شباهت به یه آدم عادی:* {result['similarity']}٪")
    lines.append("")
    lines.append(f"🔖 نتیجه برای: *{result['name']}*")
    return "\n\n".join(lines)


def analyze(name: str, extra_seed: str = "") -> str:
    return format_as_text(build_result(name, extra_seed))


# ---------------------------------------------------------------------------
# رندر کارت تصویری برای اشتراک‌گذاری در استوری/چت
# ---------------------------------------------------------------------------

CARD_COLORS = [
    ((88, 24, 160), (255, 111, 97)),
    ((17, 60, 120), (0, 200, 200)),
    ((30, 30, 30), (255, 180, 0)),
    ((10, 90, 60), (200, 255, 120)),
]


def _shape(text: str) -> str:
    """آماده‌سازی متن فارسی برای رندر درست (شکل حروف + راست‌به‌چپ)."""
    if RTL_AVAILABLE:
        reshaped = arabic_reshaper.reshape(text)
        return get_display(reshaped)
    return text  # بدون شکل‌دهی؛ ممکنه حروف بریده به نظر برسن ولی از کار نمی‌افته


def _wrap(draw, text, font, max_width):
    words = text.split(" ")
    lines, cur = [], ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if draw.textlength(_shape(trial), font=font) <= max_width:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def render_card_image(result: dict) -> io.BytesIO | None:
    """یه کارت تصویری خوشگل از نتیجه می‌سازه. اگه Pillow یا فونت نبود، None برمی‌گردونه."""
    if not PIL_AVAILABLE or not os.path.exists(FONT_PATH):
        return None

    W, H = 900, 1400
    seed_val = deterministic_seed(result["name"])
    top_color, bottom_color = CARD_COLORS[seed_val % len(CARD_COLORS)]

    img = Image.new("RGB", (W, H), top_color)
    draw = ImageDraw.Draw(img)
    for y in range(H):
        t = y / H
        r = int(top_color[0] * (1 - t) + bottom_color[0] * t)
        g = int(top_color[1] * (1 - t) + bottom_color[1] * t)
        b = int(top_color[2] * (1 - t) + bottom_color[2] * t)
        draw.line([(0, y), (W, y)], fill=(r, g, b))

    title_font = ImageFont.truetype(FONT_PATH, 46)
    cat_font = ImageFont.truetype(FONT_PATH, 30)
    body_font = ImageFont.truetype(FONT_PATH, 26)
    small_font = ImageFont.truetype(FONT_PATH, 24)

    margin = 60
    y = 50

    header = _shape("🃏 تحلیلگر شخصیت احمقانه")
    draw.text((W / 2, y), header, font=title_font, fill="white", anchor="ma")
    y += 90

    name_line = _shape(f"نتیجه برای: {result['name']}")
    draw.text((W / 2, y), name_line, font=cat_font, fill=(255, 235, 180), anchor="ma")
    y += 70

    for cat, choice in result["items"].items():
        cat_shaped = _shape(cat)
        draw.text((W - margin, y), cat_shaped, font=cat_font, fill="white", anchor="ra")
        y += 42
        for line in _wrap(draw, choice, body_font, W - 2 * margin):
            draw.text(
                (W - margin, y), _shape(line), font=body_font, fill=(235, 235, 235), anchor="ra"
            )
            y += 36
        y += 18

    # نوار درصد شباهت
    bar_w = W - 2 * margin
    bar_h = 34
    bar_y = H - 160
    draw.rounded_rectangle(
        [margin, bar_y, margin + bar_w, bar_y + bar_h], radius=17, fill=(255, 255, 255, 60)
    )
    fill_w = int(bar_w * result["similarity"] / 100)
    draw.rounded_rectangle(
        [margin, bar_y, margin + fill_w, bar_y + bar_h], radius=17, fill=(255, 215, 0)
    )
    pct_label = _shape(f"{result['similarity']}٪ شباهت به آدم عادی")
    draw.text((W / 2, bar_y - 40), pct_label, font=small_font, fill="white", anchor="ma")

    footer = _shape("ساخته‌شده با ربات تحلیلگر شخصیت احمقانه 🤖")
    draw.text((W / 2, H - 50), footer, font=small_font, fill=(255, 255, 255), anchor="ma")

    buf = io.BytesIO()
    buf.name = "result.png"
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


# ---------------------------------------------------------------------------
# هندلرها
# ---------------------------------------------------------------------------

def share_keyboard(bot_username: str, name: str) -> InlineKeyboardMarkup:
    share_text = f"منم امتحان کردم 😂 برو اسمتو بفرست به @{bot_username}"
    url = f"https://t.me/share/url?url=https://t.me/{bot_username}&text={share_text}"
    buttons = [
        [InlineKeyboardButton("📤 اشتراک‌گذاری نتیجه", url=url)],
        [InlineKeyboardButton("🔁 دوباره تحلیل کن", callback_data=f"retry:{name}")],
    ]
    return InlineKeyboardMarkup(buttons)


def quota_exhausted_keyboard(bot_username: str, user_id: int) -> InlineKeyboardMarkup:
    link = f"https://t.me/{bot_username}?start={user_id}"
    share_text = f"بیا اسمتو بفرستی به @{bot_username} ببین چی درمیاره 😂"
    url = f"https://t.me/share/url?url={link}&text={share_text}"
    buttons = [[InlineKeyboardButton("📤 دعوت دوست برای اعتبار بیشتر", url=url)]]
    return InlineKeyboardMarkup(buttons)


async def deliver_result(update: Update, context: ContextTypes.DEFAULT_TYPE, name: str, extra_seed: str = ""):
    """منطق مشترک برای تولید و ارسال نتیجه (متنی یا تصویری) به همراه چک سهمیه."""
    user = update.effective_user
    allowed, free_left, bonus_left = check_and_consume_quota(user.id)

    if not allowed:
        await update.message.reply_text(
            "😅 سهمیه‌ی تحلیل رایگان امروزت تموم شده!\n"
            f"هر دعوت موفق {BONUS_PER_INVITE} تحلیل اضافه بهت می‌ده. فردا هم سهمیه‌ات ریست می‌شه.",
            reply_markup=quota_exhausted_keyboard(context.bot.username, user.id),
        )
        return

    result = build_result(name, extra_seed=extra_seed)
    caption = format_as_text(result)
    keyboard = share_keyboard(context.bot.username, name)
    image = render_card_image(result)

    if image is not None:
        await update.message.reply_photo(
            photo=image, caption=caption, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard
        )
    else:
        await update.message.reply_text(caption, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args
    invited_by = None
    if args:
        try:
            invited_by = int(args[0])
        except (ValueError, IndexError):
            invited_by = None

    is_new = register_user(user.id, user.username or user.first_name, invited_by)

    welcome = (
        "🃏 سلام! به *تحلیلگر شخصیت احمقانه* خوش اومدی.\n\n"
        "اسمتو، اسم یه دوستتو، یا حتی عکس پروفایلتو بفرست تا یه تحلیل *کاملاً علمی و بی‌ربط* "
        "بهت بدم 😄\n\n"
        f"هر روز {DAILY_FREE_USES} تحلیل رایگان داری؛ با دعوت دوستات هم اعتبار بیشتر می‌گیری /invite\n\n"
        "همچنین می‌تونی توی هر چتی از من به‌صورت inline استفاده کنی:\n"
        f"`@{context.bot.username} اسم_دوستت`"
    )
    await update.message.reply_text(welcome, parse_mode=ParseMode.MARKDOWN)

    if is_new and invited_by:
        try:
            await context.bot.send_message(
                invited_by,
                f"🎉 یکی از دوستات با لینک تو وارد ربات شد! {BONUS_PER_INVITE} تحلیل اضافه گرفتی.",
            )
        except Exception:
            pass


async def invite(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    register_user(user.id, user.username or user.first_name, None)
    link = f"https://t.me/{context.bot.username}?start={user.id}"
    await update.message.reply_text(
        "لینک اختصاصی دعوت تو 👇\n"
        f"{link}\n\n"
        f"هر کی با این لینک بیاد، {BONUS_PER_INVITE} تحلیل اضافه می‌گیری و توی /leaderboard بالاتر می‌ری!",
    )


async def credits_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    register_user(user.id, user.username or user.first_name, None)
    today = date.today().isoformat()
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "SELECT usage_date, uses_today, bonus_credits FROM users WHERE user_id=?",
        (user.id,),
    )
    row = cur.fetchone()
    conn.close()
    usage_date, uses_today, bonus_credits = row if row else (today, 0, 0)
    if usage_date != today:
        uses_today = 0
    free_left = max(0, DAILY_FREE_USES - uses_today)
    await update.message.reply_text(
        f"🎟️ سهمیه‌ی رایگان امروز: {free_left}/{DAILY_FREE_USES}\n"
        f"🎁 اعتبار جایزه‌ای (از دعوت‌ها): {bonus_credits}\n\n"
        "برای گرفتن اعتبار بیشتر دوستاتو دعوت کن: /invite"
    )


async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    total, top = get_stats()
    lines = [f"👥 تعداد کل کاربران: {total}", "", "🏆 برترین دعوت‌کننده‌ها:"]
    if not top or top[0][1] == 0:
        lines.append("هنوز کسی دعوت نکرده — اولین نفر باش! /invite")
    else:
        medals = ["🥇", "🥈", "🥉"]
        for i, (username, count) in enumerate(top):
            if count == 0:
                break
            prefix = medals[i] if i < 3 else f"{i+1}."
            lines.append(f"{prefix} @{username or 'کاربر ناشناس'} — {count} دعوت")
    await update.message.reply_text("\n".join(lines))


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ADMIN_ID and update.effective_user.id != ADMIN_ID:
        return
    total, top = get_stats()
    mode = f"webhook ({WEBHOOK_URL})" if WEBHOOK_URL else "polling"
    await update.message.reply_text(
        f"📊 تعداد کاربران ربات: {total}\n"
        f"🖼️ خروجی تصویری: {'فعال' if PIL_AVAILABLE and os.path.exists(FONT_PATH) else 'غیرفعال (فونت یا Pillow موجود نیست)'}\n"
        f"🔌 حالت اجرا: {mode}"
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    register_user(user.id, user.username or user.first_name, None)
    name = update.message.text.strip()
    if not name:
        return
    await deliver_result(update, context, name)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    register_user(user.id, user.username or user.first_name, None)
    photo = update.message.photo[-1]
    display_name = user.first_name or "این عکس"
    # از file_unique_id به عنوان seed استفاده می‌کنیم تا نتیجه برای همون عکس ثابت بمونه
    await deliver_result(update, context, display_name, extra_seed=photo.file_unique_id)


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    if data.startswith("retry:"):
        user = query.from_user
        allowed, free_left, bonus_left = check_and_consume_quota(user.id)
        if not allowed:
            await query.answer("سهمیه‌ی امروزت تموم شده! دوستاتو دعوت کن 🎁", show_alert=True)
            return
        await query.answer()
        name = data.split(":", 1)[1]
        # با اضافه کردن timestamp به seed یه نتیجه‌ی متفاوت می‌گیریم
        result = build_result(name, extra_seed=str(int(time.time()) // 5))
        caption = format_as_text(result)
        keyboard = share_keyboard(context.bot.username, name)
        image = render_card_image(result)
        try:
            if image is not None and query.message.photo:
                await query.edit_message_media(
                    media=__import__("telegram").InputMediaPhoto(
                        media=image, caption=caption, parse_mode=ParseMode.MARKDOWN
                    ),
                    reply_markup=keyboard,
                )
            else:
                await query.edit_message_text(
                    caption, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard
                )
        except Exception as e:
            logger.warning("retry edit failed: %s", e)
    else:
        await query.answer()


async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.inline_query.query.strip()
    if not query:
        return
    result_text = analyze(query)
    results = [
        InlineQueryResultArticle(
            id=hashlib.md5(query.encode()).hexdigest(),
            title=f"تحلیل شخصیت: {query}",
            description="بزن تا نتیجه رو بفرستی 😄",
            input_message_content=InputTextMessageContent(
                result_text, parse_mode=ParseMode.MARKDOWN
            ),
        )
    ]
    await update.inline_query.answer(results, cache_time=0)


def main():
    if BOT_TOKEN == "PUT_YOUR_TOKEN_HERE":
        raise SystemExit(
            "لطفاً متغیر محیطی BOT_TOKEN رو با توکن ربات تلگرامت تنظیم کن."
        )

    db_connect()  # ساخت جدول در صورت نبود

    if not PIL_AVAILABLE:
        logger.warning("Pillow نصب نیست؛ خروجی به‌صورت متنی خواهد بود.")
    elif not os.path.exists(FONT_PATH):
        logger.warning(
            "فونت فارسی در %s پیدا نشد؛ خروجی به‌صورت متنی خواهد بود. "
            "برای فعال‌سازی کارت تصویری، یه فونت مثل Vazirmatn رو دانلود و توی مسیر fonts/ بذار.",
            FONT_PATH,
        )
    if not RTL_AVAILABLE and PIL_AVAILABLE:
        logger.warning(
            "پکیج‌های arabic_reshaper/python-bidi نصب نیستن؛ متن فارسی روی تصویر ممکنه بریده به‌نظر برسه."
        )

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("invite", invite))
    app.add_handler(CommandHandler("credits", credits_cmd))
    app.add_handler(CommandHandler("leaderboard", leaderboard))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(InlineQueryHandler(inline_query))

    if WEBHOOK_URL:
        full_webhook_url = f"{WEBHOOK_URL.rstrip('/')}/{WEBHOOK_PATH}"
        logger.info("ربات در حال اجراست (حالت webhook) روی پورت %s ...", PORT)
        logger.info("Webhook URL: %s", full_webhook_url)
        # run_webhook خودش یه سرور HTTP روی 0.0.0.0:PORT بالا میاره؛ همین کافیه تا
        # health check پیش‌فرض Render (یه اتصال TCP ساده) پاس بشه.
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=WEBHOOK_PATH,
            webhook_url=full_webhook_url,
            drop_pending_updates=True,
        )
    else:
        logger.info("ربات در حال اجراست (حالت polling) ...")
        app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
