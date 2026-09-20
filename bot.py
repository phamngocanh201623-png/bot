import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Optional

import aiosqlite
import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
)

# ==================== CẤU HÌNH ====================
BOT_TOKEN = "8919674640:AAFqp_9oUOfoj_fuZFhBGwjriOdH6h4tcJY"
YEUMONEY_TOKEN = "7787eb1815ffb5a7712eb4f146dcfa19a72c7c79434b5a3aab94bbfde9fdfe7c"
ADMIN_IDS = [7272729673]                   # Thay bằng Telegram ID của bạn (số)

REWARD_PER_LINK = 380                     # điểm / link
MIN_WITHDRAW = 50_000                    # rút tối thiểu
WAIT_SECONDS = 45                         # phải chờ ít nhất 45s mới claim
DAILY_LIMIT = 100                          # tối đa nhiệm vụ / ngày
DB_PATH = "bot_data.db"

# Link đích mặc định khi tạo shortlink (có thể đổi)
DEFAULT_DESTINATION = "https://google.com"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)


# ==================== FSM ====================
class WithdrawStates(StatesGroup):
    waiting_stk = State()
    waiting_bank = State()
    waiting_name = State()


# ==================== DATABASE ====================
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                balance INTEGER DEFAULT 0,
                total_earned INTEGER DEFAULT 0,
                created_at TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                short_link TEXT,
                status TEXT DEFAULT 'pending',  -- pending / completed / cancelled
                created_at REAL,
                completed_at REAL,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS withdrawals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                amount INTEGER,
                stk TEXT,
                bank_name TEXT,
                holder_name TEXT,
                status TEXT DEFAULT 'pending',  -- pending / approved / rejected
                created_at TEXT,
                processed_at TEXT,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
        """)
        await db.commit()


async def ensure_user(user_id: int, username: str = None, full_name: str = None):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
        if not await cur.fetchone():
            await db.execute(
                "INSERT INTO users (user_id, username, full_name, created_at) VALUES (?, ?, ?, ?)",
                (user_id, username, full_name, datetime.now().isoformat())
            )
            await db.commit()


async def get_balance(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        return row[0] if row else 0


async def add_balance(user_id: int, amount: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET balance = balance + ?, total_earned = total_earned + ? WHERE user_id = ?",
            (amount, amount, user_id)
        )
        await db.commit()


async def deduct_balance(user_id: int, amount: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        if not row or row[0] < amount:
            return False
        await db.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (amount, user_id))
        await db.commit()
        return True


async def get_active_task(user_id: int) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id, short_link, created_at FROM tasks WHERE user_id = ? AND status = 'pending' ORDER BY id DESC LIMIT 1",
            (user_id,)
        )
        row = await cur.fetchone()
        if row:
            return {"id": row[0], "short_link": row[1], "created_at": row[2]}
        return None


async def count_today_tasks(user_id: int) -> int:
    today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM tasks WHERE user_id = ? AND created_at >= ?",
            (user_id, today_start)
        )
        row = await cur.fetchone()
        return row[0] if row else 0


async def create_task(user_id: int, short_link: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO tasks (user_id, short_link, created_at) VALUES (?, ?, ?)",
            (user_id, short_link, time.time())
        )
        await db.commit()
        return cur.lastrowid


async def complete_task(task_id: int, user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT status FROM tasks WHERE id = ? AND user_id = ?",
            (task_id, user_id)
        )
        row = await cur.fetchone()
        if not row or row[0] != "pending":
            return False
        await db.execute(
            "UPDATE tasks SET status = 'completed', completed_at = ? WHERE id = ?",
            (time.time(), task_id)
        )
        await db.commit()
        return True


async def create_withdrawal(user_id: int, amount: int, stk: str, bank: str, name: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """INSERT INTO withdrawals (user_id, amount, stk, bank_name, holder_name, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (user_id, amount, stk, bank, name, datetime.now().isoformat())
        )
        await db.commit()
        return cur.lastrowid


# ==================== YEUMONEY API ====================
async def create_yeumoney_link(destination: str = DEFAULT_DESTINATION) -> Optional[str]:
    url = f"https://yeumoney.com/QL_api.php?token={YEUMONEY_TOKEN}&url={destination}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, allow_redirects=False, timeout=15) as resp:
                if resp.status in (301, 302, 303, 307, 308):
                    location = resp.headers.get("Location")
                    if location and "yeumoney.com" in location:
                        return location
                # fallback: đọc body nếu có
                text = await resp.text()
                if "yeumoney.com/" in text:
                    # cố gắng lấy link ngắn từ body (hiếm khi cần)
                    pass
    except Exception as e:
        logger.error(f"Yeumoney error: {e}")
    return None


# ==================== KEYBOARD ====================
def main_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📥 Nhận nhiệm vụ"), KeyboardButton(text="💰 Số dư")],
            [KeyboardButton(text="💸 Rút tiền"), KeyboardButton(text="📊 Thống kê")],
            [KeyboardButton(text="ℹ️ Hướng dẫn")],
        ],
        resize_keyboard=True
    )


def task_kb(task_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Đã vượt xong", callback_data=f"claim_{task_id}")],
        [InlineKeyboardButton(text="❌ Hủy nhiệm vụ", callback_data=f"cancel_{task_id}")],
    ])


# ==================== HANDLERS ====================
@router.message(CommandStart())
async def cmd_start(message: Message):
    await ensure_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.full_name
    )
    text = (
        f"👋 Xin chào <b>{message.from_user.full_name}</b>!\n\n"
        f"Bot kiếm tiền vượt link Yeumoney\n"
        f"• Thưởng: <b>{REWARD_PER_LINK}đ</b> / link\n"
        f"• Rút tối thiểu: <b>{MIN_WITHDRAW:,}đ</b>\n\n"
        f"Bấm <b>Nhận nhiệm vụ</b> để bắt đầu."
    )
    await message.answer(text, reply_markup=main_kb(), parse_mode="HTML")


@router.message(F.text == "📥 Nhận nhiệm vụ")
async def get_task(message: Message):
    user_id = message.from_user.id
    await ensure_user(user_id, message.from_user.username, message.from_user.full_name)

    # Kiểm tra nhiệm vụ đang làm
    active = await get_active_task(user_id)
    if active:
        await message.answer(
            f"⚠️ Bạn đang có nhiệm vụ chưa hoàn thành!\n\n"
            f"Link: {active['short_link']}\n\n"
            f"Hãy vượt xong rồi bấm ✅ Đã vượt xong.",
            reply_markup=task_kb(active["id"])
        )
        return

    # Giới hạn ngày
    today_count = await count_today_tasks(user_id)
    if today_count >= DAILY_LIMIT:
        await message.answer(f"⛔ Bạn đã làm đủ {DAILY_LIMIT} nhiệm vụ hôm nay. Mai quay lại nhé!")
        return

    msg = await message.answer("⏳ Đang tạo link nhiệm vụ...")

    short_link = await create_yeumoney_link()
    if not short_link:
        await msg.edit_text("❌ Lỗi tạo link Yeumoney. Thử lại sau vài phút.")
        return

    task_id = await create_task(user_id, short_link)

    text = (
        f"🎯 <b>Nhiệm vụ #{task_id}</b>\n\n"
        f"Link cần vượt:\n<code>{short_link}</code>\n\n"
        f"📌 Cách làm:\n"
        f"1. Mở link trên (sao chép hoặc bấm)\n"
        f"2. Làm theo các bước của Yeumoney\n"
        f"3. Sau khi xong, quay lại đây bấm <b>✅ Đã vượt xong</b>\n\n"
        f"⏱ Phải chờ ít nhất <b>{WAIT_SECONDS} giây</b> mới được xác nhận.\n"
        f"💰 Thưởng: <b>{REWARD_PER_LINK}đ</b>"
    )
    await msg.edit_text(text, reply_markup=task_kb(task_id), parse_mode="HTML")


@router.callback_query(F.data.startswith("claim_"))
async def claim_task(callback: CallbackQuery):
    task_id = int(callback.data.split("_")[1])
    user_id = callback.from_user.id

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id, short_link, created_at, status FROM tasks WHERE id = ? AND user_id = ?",
            (task_id, user_id)
        )
        row = await cur.fetchone()

    if not row:
        await callback.answer("Không tìm thấy nhiệm vụ!", show_alert=True)
        return

    _, short_link, created_at, status = row

    if status != "pending":
        await callback.answer("Nhiệm vụ này đã được xử lý rồi!", show_alert=True)
        return

    elapsed = time.time() - created_at
    if elapsed < WAIT_SECONDS:
        remain = int(WAIT_SECONDS - elapsed)
        await callback.answer(f"Vui lòng chờ thêm {remain} giây nữa!", show_alert=True)
        return

    # Hoàn thành
    ok = await complete_task(task_id, user_id)
    if not ok:
        await callback.answer("Lỗi xác nhận!", show_alert=True)
        return

    await add_balance(user_id, REWARD_PER_LINK)
    new_balance = await get_balance(user_id)

    await callback.message.edit_text(
        f"✅ <b>Hoàn thành nhiệm vụ #{task_id}</b>\n\n"
        f"+{REWARD_PER_LINK}đ\n"
        f"Số dư hiện tại: <b>{new_balance:,}đ</b>\n\n"
        f"Bấm «Nhận nhiệm vụ» để làm tiếp!",
        parse_mode="HTML"
    )
    await callback.answer("Đã cộng điểm!")


@router.callback_query(F.data.startswith("cancel_"))
async def cancel_task(callback: CallbackQuery):
    task_id = int(callback.data.split("_")[1])
    user_id = callback.from_user.id

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE tasks SET status = 'cancelled' WHERE id = ? AND user_id = ? AND status = 'pending'",
            (task_id, user_id)
        )
        await db.commit()

    await callback.message.edit_text("❌ Đã hủy nhiệm vụ.")
    await callback.answer()


@router.message(F.text == "💰 Số dư")
async def show_balance(message: Message):
    balance = await get_balance(message.from_user.id)
    await message.answer(
        f"💰 <b>Số dư của bạn</b>\n\n"
        f"<b>{balance:,}đ</b>\n\n"
        f"Rút tối thiểu: {MIN_WITHDRAW:,}đ",
        parse_mode="HTML"
    )


@router.message(F.text == "📊 Thống kê")
async def show_stats(message: Message):
    user_id = message.from_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT balance, total_earned FROM users WHERE user_id = ?", (user_id,)
        )
        row = await cur.fetchone()
        balance = row[0] if row else 0
        total = row[1] if row else 0

        cur = await db.execute(
            "SELECT COUNT(*) FROM tasks WHERE user_id = ? AND status = 'completed'", (user_id,)
        )
        done = (await cur.fetchone())[0]

        today = await count_today_tasks(user_id)

    await message.answer(
        f"📊 <b>Thống kê</b>\n\n"
        f"• Số dư: <b>{balance:,}đ</b>\n"
        f"• Tổng kiếm: <b>{total:,}đ</b>\n"
        f"• Nhiệm vụ đã hoàn thành: <b>{done}</b>\n"
        f"• Hôm nay đã làm: <b>{today}/{DAILY_LIMIT}</b>",
        parse_mode="HTML"
    )


@router.message(F.text == "ℹ️ Hướng dẫn")
async def guide(message: Message):
    text = (
        "<b>Hướng dẫn sử dụng</b>\n\n"
        f"1. Bấm <b>Nhận nhiệm vụ</b> → bot gửi link Yeumoney\n"
        f"2. Mở link, làm theo hướng dẫn vượt\n"
        f"3. Quay lại bot, chờ {WAIT_SECONDS}s rồi bấm <b>Đã vượt xong</b>\n"
        f"4. Nhận {REWARD_PER_LINK}đ vào số dư\n\n"
        f"• Rút tiền khi số dư ≥ {MIN_WITHDRAW:,}đ\n"
        f"• Mỗi ngày tối đa {DAILY_LIMIT} nhiệm vụ\n"
        f"• Chỉ được 1 nhiệm vụ đang làm cùng lúc"
    )
    await message.answer(text, parse_mode="HTML")


# ==================== RÚT TIỀN ====================
@router.message(F.text == "💸 Rút tiền")
async def withdraw_start(message: Message, state: FSMContext):
    balance = await get_balance(message.from_user.id)
    if balance < MIN_WITHDRAW:
        await message.answer(
            f"❌ Số dư không đủ.\n"
            f"Hiện có: <b>{balance:,}đ</b>\n"
            f"Cần tối thiểu: <b>{MIN_WITHDRAW:,}đ</b>",
            parse_mode="HTML"
        )
        return

    await state.set_state(WithdrawStates.waiting_stk)
    await message.answer(
        f"💸 <b>Rút tiền</b>\n\n"
        f"Số dư: <b>{balance:,}đ</b>\n"
        f"Bạn sẽ rút toàn bộ số dư.\n\n"
        f"Gửi <b>số tài khoản ngân hàng</b> của bạn:",
        parse_mode="HTML"
    )


@router.message(WithdrawStates.waiting_stk)
async def withdraw_stk(message: Message, state: FSMContext):
    stk = message.text.strip()
    if not stk or len(stk) < 6:
        await message.answer("Số tài khoản không hợp lệ. Gửi lại:")
        return
    await state.update_data(stk=stk)
    await state.set_state(WithdrawStates.waiting_bank)
    await message.answer("Gửi <b>tên ngân hàng</b> (VD: Vietcombank, MB, Techcombank...):", parse_mode="HTML")


@router.message(WithdrawStates.waiting_bank)
async def withdraw_bank(message: Message, state: FSMContext):
    bank = message.text.strip()
    if len(bank) < 2:
        await message.answer("Tên ngân hàng không hợp lệ. Gửi lại:")
        return
    await state.update_data(bank=bank)
    await state.set_state(WithdrawStates.waiting_name)
    await message.answer("Gửi <b>họ tên chủ tài khoản</b> (đúng như trên thẻ/STK):", parse_mode="HTML")


@router.message(WithdrawStates.waiting_name)
async def withdraw_name(message: Message, state: FSMContext):
    name = message.text.strip()
    if len(name) < 5:
        await message.answer("Họ tên không hợp lệ. Gửi lại:")
        return

    data = await state.get_data()
    await state.clear()

    user_id = message.from_user.id
    balance = await get_balance(user_id)

    if balance < MIN_WITHDRAW:
        await message.answer("Số dư đã thay đổi, không đủ điều kiện rút.")
        return

    # Trừ tiền trước
    ok = await deduct_balance(user_id, balance)
    if not ok:
        await message.answer("Lỗi trừ số dư. Thử lại.")
        return

    wid = await create_withdrawal(user_id, balance, data["stk"], data["bank"], name)

    # Thông báo user
    await message.answer(
        f"✅ Đã gửi yêu cầu rút tiền #{wid}\n\n"
        f"Số tiền: <b>{balance:,}đ</b>\n"
        f"STK: <code>{data['stk']}</code>\n"
        f"Ngân hàng: {data['bank']}\n"
        f"Chủ TK: {name}\n\n"
        f"Admin sẽ duyệt trong thời gian sớm nhất.",
        parse_mode="HTML"
    )

    # Báo admin
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                f"🔔 <b>Yêu cầu rút tiền #{wid}</b>\n\n"
                f"User: <code>{user_id}</code> @{message.from_user.username or 'N/A'}\n"
                f"Số tiền: <b>{balance:,}đ</b>\n"
                f"STK: <code>{data['stk']}</code>\n"
                f"NH: {data['bank']}\n"
                f"Tên: {name}\n\n"
                f"/duyet_{wid}  |  /tuchoi_{wid}",
                parse_mode="HTML"
            )
        except Exception:
            pass


# ==================== ADMIN ====================
@router.message(Command("duyet"))
async def admin_approve(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    try:
        wid = int(message.text.split("_")[1] if "_" in message.text else message.text.split()[1])
    except Exception:
        await message.answer("Dùng: /duyet_ID hoặc /duyet ID")
        return

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT user_id, amount, status FROM withdrawals WHERE id = ?", (wid,)
        )
        row = await cur.fetchone()
        if not row:
            await message.answer("Không tìm thấy yêu cầu.")
            return
        user_id, amount, status = row
        if status != "pending":
            await message.answer(f"Yêu cầu đã ở trạng thái: {status}")
            return
        await db.execute(
            "UPDATE withdrawals SET status = 'approved', processed_at = ? WHERE id = ?",
            (datetime.now().isoformat(), wid)
        )
        await db.commit()

    await message.answer(f"✅ Đã duyệt rút #{wid} - {amount:,}đ")
    try:
        await bot.send_message(user_id, f"✅ Yêu cầu rút tiền #{wid} ({amount:,}đ) đã được duyệt và chuyển khoản.")
    except Exception:
        pass


@router.message(Command("tuchoi"))
async def admin_reject(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    try:
        wid = int(message.text.split("_")[1] if "_" in message.text else message.text.split()[1])
    except Exception:
        await message.answer("Dùng: /tuchoi_ID hoặc /tuchoi ID")
        return

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT user_id, amount, status FROM withdrawals WHERE id = ?", (wid,)
        )
        row = await cur.fetchone()
        if not row:
            await message.answer("Không tìm thấy yêu cầu.")
            return
        user_id, amount, status = row
        if status != "pending":
            await message.answer(f"Yêu cầu đã ở trạng thái: {status}")
            return
        await db.execute(
            "UPDATE withdrawals SET status = 'rejected', processed_at = ? WHERE id = ?",
            (datetime.now().isoformat(), wid)
        )
        # Hoàn tiền
        await db.execute(
            "UPDATE users SET balance = balance + ? WHERE user_id = ?",
            (amount, user_id)
        )
        await db.commit()

    await message.answer(f"❌ Đã từ chối #{wid} và hoàn {amount:,}đ cho user.")
    try:
        await bot.send_message(user_id, f"❌ Yêu cầu rút #{wid} bị từ chối. Số tiền đã được hoàn lại số dư.")
    except Exception:
        pass


@router.message(Command("admin"))
async def admin_panel(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM users")
        total_users = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM tasks WHERE status = 'completed'")
        total_tasks = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM withdrawals WHERE status = 'pending'")
        pending = (await cur.fetchone())[0]
        cur = await db.execute("SELECT SUM(amount) FROM withdrawals WHERE status = 'pending'")
        pending_money = (await cur.fetchone())[0] or 0

    await message.answer(
        f"🛠 <b>Admin Panel</b>\n\n"
        f"Users: {total_users}\n"
        f"Nhiệm vụ hoàn thành: {total_tasks}\n"
        f"Rút đang chờ: {pending} ({pending_money:,}đ)\n\n"
        f"Lệnh: /duyet_ID | /tuchoi_ID",
        parse_mode="HTML"
    )


# ==================== MAIN ====================
async def main():
    await init_db()
    logger.info("Bot starting...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
