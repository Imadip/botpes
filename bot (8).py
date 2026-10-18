from __future__ import annotations

import asyncio
import html as html_module
import logging
import json
import os
import time
from collections import defaultdict
from datetime import datetime
from typing import Optional
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

# ============================================================
# CONFIG — fill these in
# ============================================================
BOT_TOKEN = "8942747782:AAFAG5UHINBVEBgjCoBBgxpjWIh92eehFLg"
ADMIN_ID = 7452847726          # your numeric Telegram user ID (not username)
CHANNEL_ID = "@VXStorePes"     # channel username or numeric ID like -1001234567890

MARKUP_USD = 2      # added to client's price when currency is $
MARKUP_MAD = 20     # added to client's price when currency is MAD

# Price (in MAD) based on how many "non-free" (locked/premium) players are in the team.
NON_FREE_PRICE_TABLE = {
    0: 0,
    1: 20,
    2: 50,
    3: 80,
    4: 110,
    5: 140,
}
# For counts NOT listed above: price = BASE + (count-1) * STEP
NON_FREE_BASE = 20
NON_FREE_STEP = 30

# 1 USD = this many MAD
MAD_PER_USD = 10

ADMIN_CONTACT_USERNAME = "Imaaad_hk9"  # shown to buyers in the channel post, without @

SUBMISSIONS_FILE = "submissions.json"
BANS_FILE = "bans.json"
SETTINGS_FILE = "settings.json"   # stores admin-editable markup (profit) values

# ============================================================
# SPAM / RATE-LIMIT CONFIG
# ============================================================
SPAM_MAX_MESSAGES = 8       # max messages allowed within the window
SPAM_WINDOW_SECONDS = 10    # sliding window in seconds

# ============================================================
# HELPERS
# ============================================================
def price_from_non_free_count(count: int) -> float:
    """MAD price based on number of non-free players."""
    if count in NON_FREE_PRICE_TABLE:
        return NON_FREE_PRICE_TABLE[count]
    if count <= 0:
        return 0
    return NON_FREE_BASE + (count - 1) * NON_FREE_STEP


def H(text) -> str:
    """
    Escape a value for safe embedding inside an HTML-mode Telegram message.
    Converts & -> &amp;  < -> &lt;  > -> &gt;
    Always call this on ANY user-supplied string before putting it in a
    parse_mode="HTML" message.
    """
    return html_module.escape(str(text))


logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

PAYMENT_METHODS = {
    "paypal": "PayPal",
    "crypto": "Crypto",
    "cih": "CIH Bank",
    "orange": "Orange Money",
}

# ============================================================
# STATE
# ============================================================
class SubmitFlow(StatesGroup):
    waiting_photo          = State()   # main team photo
    waiting_extra_photos   = State()   # managers + other player photos (multi)
    waiting_managers_count = State()   # how many managers
    waiting_non_free_count = State()   # non-free player count
    waiting_price_confirm  = State()
    waiting_manual_price   = State()
    waiting_currency       = State()
    waiting_payment        = State()
    waiting_other_payment  = State()   # custom payment method text
    waiting_platform       = State()   # Konami or Google Play
    waiting_description    = State()   # account description


class AdminCustomPrice(StatesGroup):
    waiting_price = State()


# Waste-time states for spammers/banned users
class WasteTimeFlow(StatesGroup):
    step1 = State()
    step2 = State()
    step3 = State()
    step4 = State()
    step5 = State()
    loop  = State()


# ============================================================
# DATA STORE
# ============================================================
submissions: dict[str, dict] = {}
_next_id = 1

# Banned users: {user_id (str): {"reason": str, "banned_at": str, ...}}
banned_users: dict[str, dict] = {}

# Rate-limit tracker: {user_id: [timestamp, ...]}
_message_timestamps: dict[int, list] = defaultdict(list)


def _load():
    global submissions, _next_id
    if os.path.exists(SUBMISSIONS_FILE):
        try:
            with open(SUBMISSIONS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                submissions = data.get("submissions", {})
                _next_id = data.get("next_id", 1)
        except Exception:
            pass


def _save():
    with open(SUBMISSIONS_FILE, "w", encoding="utf-8") as f:
        json.dump({"submissions": submissions, "next_id": _next_id}, f, ensure_ascii=False, indent=2)


def _load_bans():
    global banned_users
    if os.path.exists(BANS_FILE):
        try:
            with open(BANS_FILE, "r", encoding="utf-8") as f:
                banned_users = json.load(f)
        except Exception:
            pass


def _save_bans():
    with open(BANS_FILE, "w", encoding="utf-8") as f:
        json.dump(banned_users, f, ensure_ascii=False, indent=2)


def _load_settings():
    """Load admin-editable markup (profit) values, falling back to the CONFIG defaults above."""
    global MARKUP_USD, MARKUP_MAD
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                MARKUP_USD = float(data.get("markup_usd", MARKUP_USD))
                MARKUP_MAD = float(data.get("markup_mad", MARKUP_MAD))
        except Exception:
            pass


def _save_settings():
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump({"markup_usd": MARKUP_USD, "markup_mad": MARKUP_MAD}, f, ensure_ascii=False, indent=2)


def new_submission_id() -> str:
    global _next_id
    sid = str(_next_id)
    _next_id += 1
    return sid


def _is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


def _is_banned(user_id: int) -> bool:
    return str(user_id) in banned_users


def _check_rate_limit(user_id: int) -> bool:
    """Returns True if the user has exceeded the rate limit (is spamming)."""
    now = time.time()
    timestamps = _message_timestamps[user_id]
    timestamps[:] = [t for t in timestamps if now - t < SPAM_WINDOW_SECONDS]
    timestamps.append(now)
    return len(timestamps) > SPAM_MAX_MESSAGES


# ============================================================
# KEYBOARDS
# ============================================================
def payment_method_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 PayPal",       callback_data="pay_paypal")],
            [InlineKeyboardButton(text="🪙 Crypto",       callback_data="pay_crypto")],
            [InlineKeyboardButton(text="🏦 CIH Bank",     callback_data="pay_cih")],
            [InlineKeyboardButton(text="📱 Orange Money", callback_data="pay_orange")],
            [InlineKeyboardButton(text="✍️ أخرى (اكتب طريقتك)", callback_data="pay_other")],
        ]
    )


def platform_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎮 Konami (eFootball)",    callback_data="platform_konami")],
            [InlineKeyboardButton(text="🟢 Google Play",           callback_data="platform_google")],
        ]
    )


def admin_action_kb(sid: str, accept_price: float, currency_symbol: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text=f"✅ قبول ({accept_price} {currency_symbol})",
                callback_data=f"admin_accept_{sid}"
            )],
            [InlineKeyboardButton(text="✏️ تحديد سعر مخصص", callback_data=f"admin_custom_{sid}")],
            [InlineKeyboardButton(text="❌ رفض",             callback_data=f"admin_reject_{sid}")],
        ]
    )


def platform_status_kb(sid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎮 Konami — تم القبول ✅",     callback_data=f"plat_konami_ok_{sid}")],
            [InlineKeyboardButton(text="🟢 Google Play — تم الرفض ❌", callback_data=f"plat_google_rej_{sid}")],
        ]
    )


def skip_description_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⏭️ تخطي (بدون وصف)", callback_data="skip_description")]
        ]
    )


# ============================================================
# SPAM GUARD
# ============================================================
async def _spam_guard(message: Message, state: FSMContext) -> bool:
    """
    Returns True if the message should be blocked (caller must return immediately).
    - Admin is always exempt.
    - Banned users are silently routed to waste-time mode.
    - Users exceeding the rate limit are auto-banned then routed to waste-time mode.
    """
    user_id = message.from_user.id

    if _is_admin(user_id):
        return False

    if _is_banned(user_id):
        current = await state.get_state()
        if current not in [
            WasteTimeFlow.step1, WasteTimeFlow.step2, WasteTimeFlow.step3,
            WasteTimeFlow.step4, WasteTimeFlow.step5, WasteTimeFlow.loop,
        ]:
            await _start_waste_time(message, state)
        return True

    if _check_rate_limit(user_id):
        banned_users[str(user_id)] = {
            "reason": "spam (auto-detected)",
            "banned_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "username": message.from_user.username or "",
            "full_name": message.from_user.full_name,
        }
        _save_bans()
        logging.warning(f"Auto-banned spammer: {user_id} ({message.from_user.full_name})")
        try:
            await bot.send_message(
                ADMIN_ID,
                f"🚨 <b>تم حظر مستخدم تلقائياً (سبام)</b>\n"
                f"👤 {H(message.from_user.full_name)} (@{H(message.from_user.username or 'بدون يوزر')})\n"
                f"🆔 ID: <code>{user_id}</code>\n"
                f"لإلغاء الحظر: /unban {user_id}",
                parse_mode="HTML",
            )
        except Exception:
            pass
        await state.clear()
        await _start_waste_time(message, state)
        return True

    return False


async def _start_waste_time(message: Message, state: FSMContext):
    """Route spammer/banned user into the fake verification flow."""
    await state.clear()
    await state.set_state(WasteTimeFlow.step1)
    await asyncio.sleep(1)
    await message.answer(
        "🔄 <b>يرجى إعادة التحقق من هويتك للمتابعة.</b>\n\n"
        "⚙️ الخطوة 1/5 — أرسل رمز التحقق الذي أُرسل إلى بريدك الإلكتروني:",
        parse_mode="HTML",
    )


# ============================================================
# WASTE-TIME FLOW HANDLERS
# ============================================================
_WASTE_FAKE_RESPONSES = [
    "⚠️ الرمز غير صحيح. حاول مرة أخرى:",
    "❌ خطأ في الرمز. تأكد من إدخال الرمز المكون من 6 أرقام:",
    "🔄 انتهت صلاحية الرمز، تم إرسال رمز جديد. أدخله هنا:",
    "⚠️ محاولة خاطئة. لديك محاولة أخيرة قبل تعليق حسابك:",
]
_waste_attempt: dict[int, int] = defaultdict(int)


@router.message(WasteTimeFlow.step1)
async def waste_step1(message: Message, state: FSMContext):
    await asyncio.sleep(2)
    await state.set_state(WasteTimeFlow.step2)
    await message.answer(
        "✅ تم استلام الرمز، يتم التحقق...\n\n"
        "📋 الخطوة 2/5 — أرسل اسمك الكامل كما هو مسجل في حسابك:"
    )


@router.message(WasteTimeFlow.step2)
async def waste_step2(message: Message, state: FSMContext):
    await asyncio.sleep(2)
    await state.set_state(WasteTimeFlow.step3)
    await message.answer(
        "✅ شكراً.\n\n"
        "📋 الخطوة 3/5 — أرسل رقم هاتفك المرتبط بالحساب (بالصيغة الدولية مثال: +212600000000):"
    )


@router.message(WasteTimeFlow.step3)
async def waste_step3(message: Message, state: FSMContext):
    await asyncio.sleep(2)
    await state.set_state(WasteTimeFlow.step4)
    await message.answer(
        "⚙️ جارٍ التحقق من رقم الهاتف...\n\n"
        "📋 الخطوة 4/5 — أرسل آخر 4 أرقام من رقم بطاقتك (لغرض التحقق فقط، لن تُستخدم):"
    )


@router.message(WasteTimeFlow.step4)
async def waste_step4(message: Message, state: FSMContext):
    await asyncio.sleep(3)
    await state.set_state(WasteTimeFlow.step5)
    await message.answer(
        "🔐 الخطوة 5/5 — حل السؤال الأمني:\n\n"
        "ما هو اسم الشارع الذي نشأت فيه؟"
    )


@router.message(WasteTimeFlow.step5)
async def waste_step5(message: Message, state: FSMContext):
    await asyncio.sleep(3)
    attempt = _waste_attempt[message.from_user.id]
    _waste_attempt[message.from_user.id] = attempt + 1

    if attempt < 3:
        resp = _WASTE_FAKE_RESPONSES[attempt % len(_WASTE_FAKE_RESPONSES)]
        await state.set_state(WasteTimeFlow.loop)
        await message.answer(f"❌ {resp}")
    else:
        await state.clear()
        _waste_attempt[message.from_user.id] = 0
        await message.answer(
            "🚫 <b>تم اكتشافك كمستخدم مزعج (SPAM).</b>\n\n"
            "لقد تم حظرك من استخدام هذا البوت.\n"
            "إذا كنت تعتقد أن هذا خطأ، تواصل مع المشرف.",
            parse_mode="HTML",
        )


@router.message(WasteTimeFlow.loop)
async def waste_loop(message: Message, state: FSMContext):
    await asyncio.sleep(2)
    attempt = _waste_attempt[message.from_user.id]
    _waste_attempt[message.from_user.id] = attempt + 1

    if attempt < 6:
        resp = _WASTE_FAKE_RESPONSES[attempt % len(_WASTE_FAKE_RESPONSES)]
        await message.answer(f"⚠️ {resp}")
    else:
        await state.clear()
        _waste_attempt[message.from_user.id] = 0
        await message.answer(
            "🚫 <b>تم اكتشافك كمستخدم مزعج (SPAM).</b>\n\n"
            "لقد تم حظرك من استخدام هذا البوت.\n"
            "إذا كنت تعتقد أن هذا خطأ، تواصل مع المشرف.",
            parse_mode="HTML",
        )


# ============================================================
# CLIENT FLOW — commands
# ============================================================
@router.message(CommandStart())
async def start(message: Message, state: FSMContext):
    if await _spam_guard(message, state):
        return
    await state.clear()
    await message.answer(
        "👋 <b>مرحباً بك في بوت بيع حسابات eFootball!</b> ⚽\n\n"
        "سنأخذ بعض المعلومات عن حسابك ثم نراجع طلبك وننشره في القناة.\n\n"
        "📸 <b>خطوة 1/7</b> — أرسل لي <b>صورة الفريق</b> الرئيسية التي تريد بيعها.",
        parse_mode="HTML",
    )
    await state.set_state(SubmitFlow.waiting_photo)


@router.message(Command("cancel"))
async def cancel_flow(message: Message, state: FSMContext):
    if await _spam_guard(message, state):
        return
    if await state.get_state() is None:
        await message.answer("لا يوجد طلب جارٍ حالياً.")
        return
    await state.clear()
    await message.answer("❌ تم إلغاء العملية. أرسل /start للبدء من جديد.")


@router.message(Command("help"))
async def help_cmd(message: Message, state: FSMContext):
    if await _spam_guard(message, state):
        return
    await message.answer(
        "🤖 <b>الأوامر المتاحة:</b>\n\n"
        "/start — بدء طلب بيع حساب جديد\n"
        "/status — التحقق من حالة آخر طلب أرسلته\n"
        "/cancel — إلغاء العملية الحالية\n"
        "/help — عرض هذه القائمة"
        + ("\n\n<b>أوامر المشرف:</b>\n"
           "/markup — عرض الربح الحالي\n"
           "/setmarkup MAD USD — تعديل الربح\n"
           "/pending — الطلبات قيد الانتظار\n"
           "/ban, /unban, /bans — إدارة الحظر"
           if _is_admin(message.from_user.id) else ""),
        parse_mode="HTML",
    )


@router.message(Command("status"))
async def status_cmd(message: Message, state: FSMContext):
    if await _spam_guard(message, state):
        return
    user_subs = [s for s in submissions.values() if s.get("client_id") == message.from_user.id]
    if not user_subs:
        await message.answer("لا يوجد لديك أي طلبات سابقة. أرسل /start لبدء طلب جديد.")
        return

    latest = user_subs[-1]
    status_labels = {
        "pending":                  "⏳ قيد المراجعة",
        "awaiting_client_response": "💬 بانتظار ردك على عرض السعر",
        "accepted":                 "✅ تم القبول والنشر",
        "rejected":                 "❌ مرفوض",
    }
    platform_status_labels = {
        "konami_accepted": "🎮 Konami — تم القبول ✅",
        "google_rejected":  "🟢 Google Play — تم الرفض ❌",
    }
    status_text = status_labels.get(latest["status"], H(latest["status"]))
    currency_symbol = "MAD" if latest["currency"] == "MAD" else "$"
    price_shown = latest.get("final_price", latest.get("proposed_price", latest["price"]))
    plat_line = ""
    if latest.get("platform_status"):
        plat_line = f"\nحالة المنصة: {platform_status_labels.get(latest['platform_status'], H(latest['platform_status']))}"

    sub_id = next((sid for sid, s in submissions.items() if s is latest), "—")

    await message.answer(
        "📋 <b>آخر طلب لك:</b>\n\n"
        f"🔢 رقم الطلب: <b>#{sub_id}</b>\n"
        f"الحالة: {status_text}{plat_line}\n"
        f"السعر: {price_shown} {currency_symbol}\n"
        f"طريقة الدفع: {H(latest.get('payment_method', '-'))}\n"
        f"المنصة: {H(latest.get('platform', '-'))}",
        parse_mode="HTML",
    )


@router.message(Command("pending"))
async def pending_cmd(message: Message):
    if not _is_admin(message.from_user.id):
        return
    pending_list = [
        (sid, s) for sid, s in submissions.items()
        if s["status"] in ("pending", "awaiting_client_response")
    ]
    if not pending_list:
        await message.answer("لا توجد طلبات قيد الانتظار حالياً. ✅")
        return
    lines = ["📋 <b>الطلبات قيد الانتظار:</b>\n"]
    for sid, s in pending_list:
        currency_symbol = "MAD" if s["currency"] == "MAD" else "$"
        status_note = "بانتظار رد العميل" if s["status"] == "awaiting_client_response" else "بانتظار مراجعتك"
        lines.append(f"#{sid} — {s['price']} {currency_symbol} — {status_note}")
    await message.answer("\n".join(lines), parse_mode="HTML")


# ============================================================
# ADMIN — MARKUP / PROFIT COMMANDS
# ============================================================
@router.message(Command("markup"))
async def markup_cmd(message: Message):
    if not _is_admin(message.from_user.id):
        return
    await message.answer(
        "💰 <b>الربح الحالي (Markup):</b>\n"
        f"MAD: +{MARKUP_MAD}\n"
        f"$ (USD): +{MARKUP_USD}\n\n"
        "لتغييره: <code>/setmarkup MAD_VALUE USD_VALUE</code>\n"
        "مثال: <code>/setmarkup 25 3</code>",
        parse_mode="HTML",
    )


@router.message(Command("setmarkup"))
async def setmarkup_cmd(message: Message):
    if not _is_admin(message.from_user.id):
        return
    global MARKUP_MAD, MARKUP_USD
    parts = message.text.strip().split()
    if len(parts) != 3:
        await message.answer(
            "الاستخدام: <code>/setmarkup MAD_VALUE USD_VALUE</code>\n"
            "مثال: <code>/setmarkup 25 3</code>",
            parse_mode="HTML",
        )
        return
    try:
        new_mad = float(parts[1].replace(",", "."))
        new_usd = float(parts[2].replace(",", "."))
    except ValueError:
        await message.answer("⚠️ القيم يجب أن تكون أرقاماً.")
        return

    MARKUP_MAD = new_mad
    MARKUP_USD = new_usd
    _save_settings()
    await message.answer(
        "✅ تم تحديث الربح (Markup):\n"
        f"MAD: +{MARKUP_MAD}\n"
        f"$ (USD): +{MARKUP_USD}\n\n"
        "ملاحظة: هذا يطبَّق على الطلبات الجديدة. لا يغيّر عروض القبول المُرسلة سابقاً للعميل.",
    )


# ============================================================
# ADMIN — BAN COMMANDS
# ============================================================
@router.message(Command("ban"))
async def ban_cmd(message: Message):
    if not _is_admin(message.from_user.id):
        return
    parts = message.text.strip().split(maxsplit=2)
    if len(parts) < 2:
        await message.answer("الاستخدام: /ban USER_ID [سبب اختياري]")
        return
    try:
        target_id = int(parts[1])
    except ValueError:
        await message.answer("⚠️ يجب أن يكون USER_ID رقماً صحيحاً.")
        return
    reason = parts[2] if len(parts) > 2 else "بدون سبب"
    banned_users[str(target_id)] = {
        "reason": reason,
        "banned_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "username": "",
        "full_name": "",
    }
    _save_bans()
    await message.answer(
        f"🚫 تم حظر المستخدم <code>{target_id}</code> بسبب: {H(reason)}",
        parse_mode="HTML",
    )
    try:
        await bot.send_message(
            target_id,
            "🚫 لقد تم حظرك من استخدام هذا البوت.\n"
            "للاستفسار، تواصل مع المشرف.",
        )
    except Exception:
        pass


@router.message(Command("unban"))
async def unban_cmd(message: Message):
    if not _is_admin(message.from_user.id):
        return
    parts = message.text.strip().split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("الاستخدام: /unban USER_ID")
        return
    try:
        target_id = int(parts[1])
    except ValueError:
        await message.answer("⚠️ يجب أن يكون USER_ID رقماً صحيحاً.")
        return
    if str(target_id) in banned_users:
        del banned_users[str(target_id)]
        _save_bans()
        await message.answer(
            f"✅ تم رفع الحظر عن المستخدم <code>{target_id}</code>.",
            parse_mode="HTML",
        )
        try:
            await bot.send_message(
                target_id,
                "✅ تم رفع الحظر عن حسابك. يمكنك استخدام البوت مرة أخرى عبر /start",
            )
        except Exception:
            pass
    else:
        await message.answer(
            f"ℹ️ المستخدم <code>{target_id}</code> غير محظور.",
            parse_mode="HTML",
        )


@router.message(Command("bans"))
async def bans_cmd(message: Message):
    if not _is_admin(message.from_user.id):
        return
    if not banned_users:
        await message.answer("✅ لا يوجد مستخدمون محظورون حالياً.")
        return
    lines = ["🚫 <b>قائمة المحظورين:</b>\n"]
    for uid, info in banned_users.items():
        name   = H(info.get("full_name") or info.get("username") or "—")
        reason = H(info.get("reason", "—"))
        date   = info.get("banned_at", "—")
        lines.append(f"🆔 <code>{uid}</code> — {name}\n   السبب: {reason} | {date}")
    await message.answer("\n\n".join(lines), parse_mode="HTML")


# ============================================================
# CLIENT FLOW — Step 1: main team photo
# ============================================================
@router.message(SubmitFlow.waiting_photo, F.photo)
async def got_photo(message: Message, state: FSMContext):
    if await _spam_guard(message, state):
        return
    await state.update_data(photo_file_ids=[message.photo[-1].file_id])
    await message.answer(
        "📸 <b>خطوة 2/7</b> — أرسل الآن <b>صور إضافية</b> (عدد المديرين، لاعبين آخرين، إلخ).\n\n"
        "يمكنك إرسال أكثر من صورة واحدة.\n"
        "عندما تنتهي، اضغط الزر أدناه ✅",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ انتهيت من إرسال الصور", callback_data="photos_done")]
        ]),
    )
    await state.set_state(SubmitFlow.waiting_extra_photos)


@router.message(SubmitFlow.waiting_photo)
async def not_photo(message: Message, state: FSMContext):
    if await _spam_guard(message, state):
        return
    await message.answer("⚠️ من فضلك أرسل صورة للفريق أولاً.")


# ============================================================
# CLIENT FLOW — Step 2: extra photos
# ============================================================
@router.message(SubmitFlow.waiting_extra_photos, F.photo)
async def got_extra_photo(message: Message, state: FSMContext):
    if await _spam_guard(message, state):
        return
    data = await state.get_data()
    ids = data.get("photo_file_ids", [])
    ids.append(message.photo[-1].file_id)
    await state.update_data(photo_file_ids=ids)
    await message.answer(
        f"✅ تمت إضافة الصورة ({len(ids)} صورة/صور حتى الآن).\n"
        "أرسل المزيد أو اضغط «انتهيت» للمتابعة.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ انتهيت من إرسال الصور", callback_data="photos_done")]
        ]),
    )


@router.message(SubmitFlow.waiting_extra_photos)
async def extra_photo_wrong(message: Message, state: FSMContext):
    if await _spam_guard(message, state):
        return
    await message.answer(
        "⚠️ أرسل صوراً فقط، أو اضغط «انتهيت» للمتابعة.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ انتهيت من إرسال الصور", callback_data="photos_done")]
        ]),
    )


@router.callback_query(SubmitFlow.waiting_extra_photos, F.data == "photos_done")
async def photos_done(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "👔 <b>خطوة 3/7</b> — كم عدد <b>المديرين (Managers)</b> في حسابك؟\n"
        "(أدخل رقمًا، مثال: 3)",
        parse_mode="HTML",
    )
    await callback.answer()
    await state.set_state(SubmitFlow.waiting_managers_count)


# ============================================================
# CLIENT FLOW — Step 3: managers count
# ============================================================
@router.message(SubmitFlow.waiting_managers_count)
async def got_managers_count(message: Message, state: FSMContext):
    if await _spam_guard(message, state):
        return
    text = message.text.strip()
    if not text.isdigit():
        await message.answer("⚠️ من فضلك أدخل رقمًا صحيحًا (مثال: 3).")
        return
    await state.update_data(managers_count=int(text))
    await message.answer(
        "⭐ <b>خطوة 4/7</b> — كم عدد اللاعبين الغير مجانيين (not free) في فريقك؟\n"
        "(أدخل الرقم فقط، مثال: 2)\n\n"
        "أو اضغط الزر أدناه إذا كنت تفضل تحديد السعر بنفسك:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✏️ أدخل السعر مباشرة", callback_data="manual_price")]
        ]),
    )
    await state.set_state(SubmitFlow.waiting_non_free_count)


# ============================================================
# CLIENT FLOW — Step 4: non-free count / manual price
# ============================================================
@router.message(SubmitFlow.waiting_non_free_count)
async def got_non_free_count(message: Message, state: FSMContext):
    if await _spam_guard(message, state):
        return
    text = message.text.strip()
    if not text.isdigit():
        await message.answer("⚠️ من فضلك أدخل رقم صحيح، أو اضغط زر «أدخل السعر مباشرة» بالأعلى.")
        return

    count = int(text)
    price_mad = price_from_non_free_count(count)
    await state.update_data(non_free_count=count, price_mad=price_mad, manual_flow=False)

    await message.answer(
        "➖➖➖➖➖➖➖➖➖➖\n"
        f"⭐ عدد اللاعبين الغير مجانيين: {count}\n"
        f"💰 السعر المحسوب: <b>{price_mad} MAD</b>\n"
        "➖➖➖➖➖➖➖➖➖➖\n\n"
        "هل توافق على هذا السعر؟",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ متابعة بهذا السعر", callback_data="price_confirm")],
            [InlineKeyboardButton(text="✏️ تعديل العدد",        callback_data="price_edit")],
        ]),
    )
    await state.set_state(SubmitFlow.waiting_price_confirm)


@router.callback_query(SubmitFlow.waiting_price_confirm, F.data == "price_confirm")
async def price_confirmed(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "💱 <b>خطوة 5/7</b> — بأي عملة تريد نشر وبيع الحساب؟",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🇲🇦 MAD (درهم)", callback_data="cur_mad"),
            InlineKeyboardButton(text="💵 USD ($)",     callback_data="cur_usd"),
        ]]),
    )
    await callback.answer()
    await state.set_state(SubmitFlow.waiting_currency)


@router.callback_query(SubmitFlow.waiting_price_confirm, F.data == "price_edit")
async def price_edit(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "⭐ حسناً، كم عدد اللاعبين الغير مجانيين (not free) في فريقك؟\n"
        "(أدخل الرقم فقط، مثال: 2)\n\n"
        "أو اضغط الزر أدناه لتحديد السعر بنفسك:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✏️ أدخل السعر مباشرة", callback_data="manual_price")]
        ]),
    )
    await callback.answer()
    await state.set_state(SubmitFlow.waiting_non_free_count)


@router.callback_query(SubmitFlow.waiting_non_free_count, F.data == "manual_price")
async def choose_manual_price(callback: CallbackQuery, state: FSMContext):
    await state.update_data(manual_flow=True, non_free_count=None)
    await callback.message.edit_text(
        "💱 <b>خطوة 5/7</b> — بأي عملة تريد تحديد السعر؟",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🇲🇦 MAD (درهم)", callback_data="cur_mad"),
            InlineKeyboardButton(text="💵 USD ($)",     callback_data="cur_usd"),
        ]]),
    )
    await callback.answer()
    await state.set_state(SubmitFlow.waiting_currency)


# ============================================================
# CLIENT FLOW — Step 5: currency
# ============================================================
@router.callback_query(SubmitFlow.waiting_currency, F.data.startswith("cur_"))
async def got_currency(callback: CallbackQuery, state: FSMContext):
    currency = "MAD" if callback.data == "cur_mad" else "USD"
    data = await state.get_data()
    manual_flow = data.get("manual_flow", False)

    if manual_flow:
        await state.update_data(currency=currency)
        await callback.message.edit_text(
            f"💰 أدخل السعر الذي تريده بـ{currency} (أرقام فقط، مثال: 60):"
        )
        await callback.answer()
        await state.set_state(SubmitFlow.waiting_manual_price)
        return

    price_mad = data["price_mad"]
    price = price_mad if currency == "MAD" else round(price_mad / MAD_PER_USD, 2)
    await state.update_data(currency=currency, price=price)
    await callback.message.edit_text(
        "💳 <b>خطوة 5/7</b> — اختر طريقة الدفع التي تريد استلام المال بها:",
        parse_mode="HTML",
        reply_markup=payment_method_kb(),
    )
    await callback.answer()
    await state.set_state(SubmitFlow.waiting_payment)


@router.message(SubmitFlow.waiting_manual_price)
async def got_manual_price(message: Message, state: FSMContext):
    if await _spam_guard(message, state):
        return
    text = message.text.strip().replace(",", ".")
    try:
        price = float(text)
    except ValueError:
        await message.answer("⚠️ من فضلك أدخل رقم صحيح فقط، مثال: 60")
        return
    await state.update_data(price=price)
    await message.answer(
        "💳 <b>خطوة 5/7</b> — اختر طريقة الدفع التي تريد استلام المال بها:",
        parse_mode="HTML",
        reply_markup=payment_method_kb(),
    )
    await state.set_state(SubmitFlow.waiting_payment)


# ============================================================
# CLIENT FLOW — Step 5b: payment method
# ============================================================
@router.callback_query(SubmitFlow.waiting_payment, F.data.startswith("pay_"))
async def got_payment_choice(callback: CallbackQuery, state: FSMContext):
    method_key = callback.data.split("_", 1)[1]

    if method_key == "other":
        await callback.message.edit_text(
            "✍️ اكتب طريقة الدفع التي لديك (مثال: Barid Bank, Inwi Money, Western Union...):"
        )
        await callback.answer()
        await state.set_state(SubmitFlow.waiting_other_payment)
        return

    method_label = PAYMENT_METHODS.get(method_key, method_key)
    await state.update_data(payment_method=method_label)
    await _ask_platform(callback.message, state, edit=True)
    await callback.answer()


@router.message(SubmitFlow.waiting_other_payment)
async def got_other_payment(message: Message, state: FSMContext):
    if await _spam_guard(message, state):
        return
    method_label = message.text.strip()
    if not method_label:
        await message.answer("⚠️ من فضلك اكتب طريقة الدفع التي لديك.")
        return
    await state.update_data(payment_method=method_label)
    await _ask_platform(message, state, edit=False)


async def _ask_platform(msg_or_cb, state: FSMContext, edit: bool):
    text = (
        "🎮 <b>خطوة 6/7</b> — ما المنصة التي سيتم التحويل عبرها؟\n\n"
        "اختر إذا كان الرابط عبر Konami أو Google Play:"
    )
    kb = platform_kb()
    if edit:
        await msg_or_cb.edit_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        await msg_or_cb.answer(text, reply_markup=kb, parse_mode="HTML")
    await state.set_state(SubmitFlow.waiting_platform)


# ============================================================
# CLIENT FLOW — Step 6: platform
# ============================================================
@router.callback_query(SubmitFlow.waiting_platform, F.data.startswith("platform_"))
async def got_platform(callback: CallbackQuery, state: FSMContext):
    platform_key = callback.data.split("_", 1)[1]
    platform_label = "🎮 Konami (eFootball)" if platform_key == "konami" else "🟢 Google Play"
    await state.update_data(platform=platform_label, platform_key=platform_key)

    await callback.message.edit_text(
        "📝 <b>خطوة 7/7</b> — اكتب <b>وصفاً لحسابك</b> (اختياري)\n\n"
        "مثال: «حساب قديم مع 5 لاعبين مميزين، بدون إنفاق سابق، جميع المهاجمين من الذهبي»\n\n"
        "يمكنك تخطي هذه الخطوة إذا لم تشأ إضافة وصف:",
        parse_mode="HTML",
        reply_markup=skip_description_kb(),
    )
    await callback.answer()
    await state.set_state(SubmitFlow.waiting_description)


# ============================================================
# CLIENT FLOW — Step 7: account description
# ============================================================
@router.message(SubmitFlow.waiting_description)
async def got_description(message: Message, state: FSMContext):
    if await _spam_guard(message, state):
        return
    await state.update_data(description=message.text.strip())
    await _finalize_submission(message, state)


@router.callback_query(SubmitFlow.waiting_description, F.data == "skip_description")
async def skip_description(callback: CallbackQuery, state: FSMContext):
    await state.update_data(description="")
    await callback.answer()
    await _finalize_submission(callback.message, state, trigger_user=callback.from_user)


# ============================================================
# FINALIZE SUBMISSION
# ============================================================
async def _finalize_submission(message_obj, state: FSMContext, trigger_user=None):
    data = await state.get_data()
    photo_file_ids = data["photo_file_ids"]
    price          = data["price"]
    currency       = data["currency"]
    non_free_count = data.get("non_free_count")
    managers_count = data.get("managers_count", "غير محدد")
    payment_method = data["payment_method"]
    platform_label = data["platform"]
    description    = data.get("description", "")

    sid = new_submission_id()

    if trigger_user:
        client_id       = trigger_user.id
        client_name     = trigger_user.full_name
        client_username = trigger_user.username
    else:
        client_id       = message_obj.from_user.id if hasattr(message_obj, "from_user") and message_obj.from_user else None
        client_name     = message_obj.from_user.full_name if hasattr(message_obj, "from_user") and message_obj.from_user else "—"
        client_username = message_obj.from_user.username if hasattr(message_obj, "from_user") and message_obj.from_user else None

    submissions[sid] = {
        "client_id":       client_id,
        "client_name":     client_name,
        "client_username": client_username,
        "photo_file_ids":  photo_file_ids,
        "price":           price,
        "currency":        currency,
        "non_free_count":  non_free_count,
        "managers_count":  managers_count,
        "payment_method":  payment_method,
        "platform":        platform_label,
        "platform_key":    data.get("platform_key"),
        "description":     description,
        "status":          "pending",
        "platform_status": None,
        "created_at":      datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    _save()

    currency_symbol = "MAD" if currency == "MAD" else "$"

    # ---- Confirm to client (HTML — all user text is escaped via H()) ----
    description_line = f"📝 الوصف: {H(description)}\n" if description else ""

    confirmation_text = (
        "✅ <b>تم إرسال طلبك بنجاح!</b>\n\n"
        f"🔢 <b>رقم طلبك: #{sid}</b>\n\n"
        f"📸 عدد الصور المرسلة: {len(photo_file_ids)}\n"
        f"👔 عدد المديرين: {managers_count}\n"
        f"💰 السعر: {price} {currency_symbol}\n"
        f"💳 طريقة الدفع: {H(payment_method)}\n"
        f"🎮 المنصة: {H(platform_label)}\n"
        f"{description_line}"
        "\n⏳ سيتم مراجعته والرد عليك قريباً.\n"
        "يمكنك التحقق من حالة طلبك في أي وقت عبر /status"
    )

    try:
        await message_obj.edit_text(confirmation_text, parse_mode="HTML")
    except Exception:
        await message_obj.answer(confirmation_text, parse_mode="HTML")

    await state.clear()

    # ---- Notify admin (HTML) ----
    markup       = MARKUP_MAD if currency == "MAD" else MARKUP_USD
    accept_price = price + markup

    desc_admin_line = f"📝 وصف الحساب: {H(description)}\n" if description else "📝 وصف الحساب: لا يوجد\n"

    caption = (
        f"🆕 <b>طلب جديد #{sid}</b>\n"
        "➖➖➖➖➖➖➖➖➖➖\n"
        f"👤 العميل: {H(client_name)} (@{H(client_username or 'بدون يوزر')})\n"
        f"🆔 ID: <code>{client_id}</code>\n"
        f"📸 عدد الصور: {len(photo_file_ids)}\n"
        f"👔 عدد المديرين: {managers_count}\n"
        f"⭐ لاعبين غير مجانيين: {non_free_count if non_free_count is not None else 'غير محدد (سعر مباشر)'}\n"
        f"💰 السعر المطلوب: {price} {currency_symbol}\n"
        f"💳 طريقة الدفع: {H(payment_method)}\n"
        f"🎮 المنصة: {H(platform_label)}\n"
        f"{desc_admin_line}"
        "➖➖➖➖➖➖➖➖➖➖\n"
        f"✅ القبول سينشر بسعر: <b>{accept_price} {currency_symbol}</b> (ربحك: +{markup})"
    )

    kb = admin_action_kb(sid, accept_price, currency_symbol)

    if len(photo_file_ids) == 1:
        await bot.send_photo(ADMIN_ID, photo=photo_file_ids[0], caption=caption, reply_markup=kb, parse_mode="HTML")
    else:
        from aiogram.types import InputMediaPhoto
        media = [InputMediaPhoto(media=fid) for fid in photo_file_ids]
        media[0] = InputMediaPhoto(media=photo_file_ids[0], caption=f"📸 {len(photo_file_ids)} صور — طلب #{sid}")
        await bot.send_media_group(ADMIN_ID, media=media)
        await bot.send_message(ADMIN_ID, caption, reply_markup=kb, parse_mode="HTML")


# ============================================================
# ADMIN FLOW
# ============================================================
@router.callback_query(F.data.startswith("admin_accept_"))
async def admin_accept(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        return await callback.answer("غير مصرح لك.", show_alert=True)

    sid = callback.data.split("_")[-1]
    sub = submissions.get(sid)
    if not sub or sub["status"] != "pending":
        return await callback.answer("هذا الطلب لم يعد متاحاً.", show_alert=True)

    markup      = MARKUP_MAD if sub["currency"] == "MAD" else MARKUP_USD
    final_price = sub["price"] + markup
    await finalize_and_post(sid, final_price, callback)


@router.callback_query(F.data.startswith("admin_custom_"))
async def admin_custom(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        return await callback.answer("غير مصرح لك.", show_alert=True)

    sid = callback.data.split("_")[-1]
    sub = submissions.get(sid)
    if not sub or sub["status"] != "pending":
        return await callback.answer("هذا الطلب لم يعد متاحاً.", show_alert=True)

    await state.update_data(pending_sid=sid)
    await state.set_state(AdminCustomPrice.waiting_price)
    await callback.message.answer(
        f"✏️ أدخل السعر النهائي الذي تريد نشره لطلب #{sid} (بعملة {sub['currency']}):"
    )
    await callback.answer()


@router.message(AdminCustomPrice.waiting_price)
async def admin_custom_price_received(message: Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return

    data = await state.get_data()
    sid  = data.get("pending_sid")
    sub  = submissions.get(sid)
    if not sub:
        await state.clear()
        return

    try:
        proposed_price = float(message.text.strip().replace(",", "."))
    except ValueError:
        await message.answer("⚠️ أدخل رقم صحيح فقط.")
        return

    await state.clear()
    sub["status"]         = "awaiting_client_response"
    sub["proposed_price"] = proposed_price
    _save()

    currency_symbol = "MAD" if sub["currency"] == "MAD" else "$"
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ أوافق",     callback_data=f"client_accept_{sid}"),
        InlineKeyboardButton(text="❌ لا أوافق", callback_data=f"client_reject_{sid}"),
    ]])
    try:
        await bot.send_message(
            sub["client_id"],
            f"💬 <b>عرض سعر لطلبك رقم #{sid}</b>\n\n"
            f"اقترحنا عليك سعر <b>{proposed_price} {currency_symbol}</b> لحسابك.\n"
            "هل توافق على هذا السعر؟",
            reply_markup=kb,
            parse_mode="HTML",
        )
        await message.answer(f"✅ تم إرسال السعر المقترح ({proposed_price} {currency_symbol}) للعميل، بانتظار رده.")
    except Exception as e:
        logging.warning(f"couldn't reach client {sub['client_id']}: {e}")
        await message.answer("⚠️ تعذر إرسال الرسالة للعميل (ربما حظر البوت).")


@router.callback_query(F.data.startswith("admin_reject_"))
async def admin_reject(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        return await callback.answer("غير مصرح لك.", show_alert=True)

    sid = callback.data.split("_")[-1]
    sub = submissions.get(sid)
    if not sub or sub["status"] != "pending":
        return await callback.answer("هذا الطلب لم يعد متاحاً.", show_alert=True)

    sub["status"] = "rejected"
    _save()

    try:
        await bot.send_message(
            sub["client_id"],
            f"❌ <b>طلبك رقم #{sid} تم رفضه.</b>\n\n"
            "نعتذر، لم يتم قبول طلب بيع حسابك هذه المرة.\n"
            "يمكنك المحاولة بسعر مختلف أو التواصل معنا لمزيد من التفاصيل.",
            parse_mode="HTML",
        )
    except Exception as e:
        logging.warning(f"couldn't notify client {sub['client_id']}: {e}")

    try:
        await callback.message.edit_caption(
            caption=(callback.message.caption or "") + "\n\n❌ تم الرفض.",
            parse_mode="HTML",
        )
    except Exception:
        try:
            await callback.message.edit_text(
                (callback.message.text or "") + "\n\n❌ تم الرفض.",
                parse_mode="HTML",
            )
        except Exception:
            pass
    await callback.answer("تم الرفض ✅")


# ============================================================
# CLIENT RESPONSE TO CUSTOM PRICE
# ============================================================
@router.callback_query(F.data.startswith("client_accept_"))
async def client_accept_price(callback: CallbackQuery):
    sid = callback.data.split("_")[-1]
    sub = submissions.get(sid)
    if not sub or sub["status"] != "awaiting_client_response":
        return await callback.answer("هذا العرض لم يعد متاحاً.", show_alert=True)
    if callback.from_user.id != sub["client_id"]:
        return await callback.answer("غير مصرح لك.", show_alert=True)

    proposed_price  = sub["proposed_price"]
    currency_symbol = "MAD" if sub["currency"] == "MAD" else "$"
    await callback.message.edit_text(
        f"✅ تمت الموافقة على السعر ({proposed_price} {currency_symbol})."
    )
    await callback.answer()
    await finalize_and_post(sid, proposed_price, callback)
    try:
        await bot.send_message(ADMIN_ID, f"✅ العميل وافق على السعر المقترح لطلب #{sid}. تم النشر في القناة.")
    except Exception:
        pass


@router.callback_query(F.data.startswith("client_reject_"))
async def client_reject_price(callback: CallbackQuery):
    sid = callback.data.split("_")[-1]
    sub = submissions.get(sid)
    if not sub or sub["status"] != "awaiting_client_response":
        return await callback.answer("هذا العرض لم يعد متاحاً.", show_alert=True)
    if callback.from_user.id != sub["client_id"]:
        return await callback.answer("غير مصرح لك.", show_alert=True)

    sub["status"] = "rejected"
    _save()
    await callback.message.edit_text("❌ تم رفض السعر المقترح.")
    await callback.answer()
    try:
        await bot.send_message(ADMIN_ID, f"❌ العميل رفض السعر المقترح لطلب #{sid}.")
    except Exception:
        pass


# ============================================================
# ADMIN — Platform status buttons
# ============================================================
@router.callback_query(F.data.startswith("plat_konami_ok_"))
async def plat_konami_accepted(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        return await callback.answer("غير مصرح لك.", show_alert=True)

    sid = callback.data.split("_")[-1]
    sub = submissions.get(sid)
    if not sub:
        return await callback.answer("الطلب غير موجود.", show_alert=True)

    sub["platform_status"] = "konami_accepted"
    _save()

    try:
        await bot.send_message(
            sub["client_id"],
            f"🎮 <b>Konami — تم القبول!</b> ✅\n\n"
            f"تم قبول طلبك رقم #{sid} عبر Konami.\n"
            f"💰 السعر النهائي: {sub.get('final_price', sub['price'])} {'MAD' if sub['currency'] == 'MAD' else '$'}",
            parse_mode="HTML",
        )
    except Exception:
        pass

    try:
        await callback.message.edit_text(
            (callback.message.text or "") + "\n\n🎮 Konami — تم القبول ✅",
            parse_mode="HTML",
        )
    except Exception:
        pass
    await callback.answer("تم تسجيل: Konami قبل ✅")


@router.callback_query(F.data.startswith("plat_google_rej_"))
async def plat_google_rejected(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        return await callback.answer("غير مصرح لك.", show_alert=True)

    sid = callback.data.split("_")[-1]
    sub = submissions.get(sid)
    if not sub:
        return await callback.answer("الطلب غير موجود.", show_alert=True)

    sub["platform_status"] = "google_rejected"
    _save()

    try:
        await bot.send_message(
            sub["client_id"],
            f"🟢 <b>Google Play — تم الرفض</b> ❌\n\n"
            f"للأسف، تم رفض طلبك رقم #{sid} من Google Play.\n"
            "يمكنك التواصل معنا لمعرفة التفاصيل أو المحاولة بطريقة أخرى.",
            parse_mode="HTML",
        )
    except Exception:
        pass

    try:
        await callback.message.edit_text(
            (callback.message.text or "") + "\n\n🟢 Google Play — تم الرفض ❌",
            parse_mode="HTML",
        )
    except Exception:
        pass
    await callback.answer("تم تسجيل: Google Play رفض ❌")


# ============================================================
# SHARED: finalize, post to channel, notify client
# ============================================================
async def finalize_and_post(sid: str, final_price: float, trigger=None):
    sub = submissions[sid]
    sub["status"]      = "accepted"
    sub["final_price"] = final_price
    _save()

    currency_symbol = "MAD" if sub["currency"] == "MAD" else "$"
    photo_file_ids  = sub.get("photo_file_ids", [sub.get("photo_file_id")])

    managers_line    = f"👔 عدد المديرين: {sub['managers_count']}\n" if sub.get("managers_count") is not None else ""
    non_free_line    = f"⭐ لاعبين غير مجانيين: {sub['non_free_count']}\n" if sub.get("non_free_count") is not None else ""
    # Description in channel post: escape user text
    description_line = f"📝 {H(sub['description'])}\n" if sub.get("description") else ""

    channel_caption = (
        "⚽ <b>فريق eFootball للبيع</b>\n"
        "➖➖➖➖➖➖➖➖➖➖\n"
        f"{managers_line}"
        f"{non_free_line}"
        f"{description_line}"
        f"💰 السعر: <b>{final_price} {currency_symbol}</b>\n"
        f"🎮 المنصة: {H(sub.get('platform', '-'))}\n"
        "➖➖➖➖➖➖➖➖➖➖\n"
        f"📩 للشراء، تواصل معنا: @{ADMIN_CONTACT_USERNAME}"
    )

    # Post to channel
    if len(photo_file_ids) == 1:
        await bot.send_photo(
            CHANNEL_ID,
            photo=photo_file_ids[0],
            caption=channel_caption,
            parse_mode="HTML",          # ← HTML, never Markdown
        )
    else:
        from aiogram.types import InputMediaPhoto
        media    = [InputMediaPhoto(media=fid) for fid in photo_file_ids]
        media[0] = InputMediaPhoto(media=photo_file_ids[0], caption=channel_caption, parse_mode="HTML")
        await bot.send_media_group(CHANNEL_ID, media=media)

    # Notify client
    try:
        await bot.send_message(
            sub["client_id"],
            f"✅ <b>تم قبول طلبك رقم #{sid} ونشره في القناة!</b>\n"
            f"💰 السعر النهائي المنشور: {final_price} {currency_symbol}",
            parse_mode="HTML",
        )
    except Exception as e:
        logging.warning(f"couldn't notify client {sub['client_id']}: {e}")

    # Platform-status buttons to admin
    try:
        await bot.send_message(
            ADMIN_ID,
            f"✅ تم نشر طلب #{sid} بسعر {final_price} {currency_symbol}.\n"
            "سجّل نتيجة المنصة أدناه:",
            reply_markup=platform_status_kb(sid),
        )
    except Exception:
        pass

    # Update admin message
    if isinstance(trigger, CallbackQuery) and trigger.message:
        suffix = f"\n\n✅ تم النشر بسعر {final_price} {currency_symbol}."
        try:
            if trigger.message.caption:
                await trigger.message.edit_caption(
                    caption=trigger.message.caption + suffix,
                    parse_mode="HTML",
                )
            else:
                await trigger.message.edit_text(
                    (trigger.message.text or "") + suffix,
                    parse_mode="HTML",
                )
        except Exception:
            pass
        await trigger.answer("تم النشر ✅")
    elif isinstance(trigger, Message):
        await trigger.answer(f"✅ تم النشر في القناة بسعر {final_price} {currency_symbol}.")


# ============================================================
# ENTRYPOINT
# ============================================================
async def main():
    _load()
    _load_bans()
    _load_settings()
    print("Bot is running...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
