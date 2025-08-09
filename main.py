import asyncio
import logging
import aiomysql
from telebot.async_telebot import AsyncTeleBot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError, ChannelInvalidError, ChannelPrivateError, PeerIdInvalidError
from telethon.sessions import SQLiteSession
import os
from dotenv import load_dotenv

# .env faylidan sozlamalarni o‘qish
load_dotenv()
API_TOKEN = os.getenv('API_TOKEN')
API_ID = int(os.getenv('API_ID'))
API_HASH = os.getenv('API_HASH')
MYSQL_HOST = os.getenv('MYSQL_HOST')
MYSQL_USER = os.getenv('MYSQL_USER')
MYSQL_PASSWORD = os.getenv('MYSQL_PASSWORD')
MYSQL_DB = os.getenv('MYSQL_DB')
ADMIN_USER_ID = int(os.getenv('ADMIN_USER_ID', 5764455157))  # .env dan yoki default qiymat

# Logging sozlamalari
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

bot = AsyncTeleBot(API_TOKEN)

# Sessiya va ma'lumotlar bazasi locklari
session_locks = {}
db_lock = asyncio.Lock()

# MySQL ulanish pooli
async def create_db_pool():
    return await aiomysql.create_pool(
        host=MYSQL_HOST,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        db=MYSQL_DB,
        autocommit=True
    )

# Ma'lumotlar bazasini boshlash
async def init_db():
    async with db_lock:
        pool = await create_db_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as c:
                await c.execute('''CREATE TABLE IF NOT EXISTS users (
                                    user_id BIGINT PRIMARY KEY,
                                    balance INT DEFAULT 0
                                )''')
                await c.execute('''CREATE TABLE IF NOT EXISTS accounts (
                                    user_id BIGINT,
                                    phone VARCHAR(20),
                                    session_file VARCHAR(255),
                                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                                )''')
                await c.execute('''CREATE TABLE IF NOT EXISTS messages (
                                    message_id BIGINT AUTO_INCREMENT PRIMARY KEY,
                                    user_id BIGINT,
                                    phone VARCHAR(20),
                                    group_ids TEXT,
                                    message_text TEXT,
                                    media_file_id VARCHAR(255),
                                    send_interval INT,
                                    is_recurring INT DEFAULT 0,
                                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                                )''')
        pool.close()
        await pool.wait_closed()

# Sessiya lockini olish
def get_session_lock(session_file):
    if session_file not in session_locks:
        session_locks[session_file] = asyncio.Lock()
    return session_locks[session_file]

# Foydalanuvchi ma'lumotlari
user_data = {}
recurring_tasks = {}

# Boshlang‘ich xabar
@bot.message_handler(commands=['start'])
async def send_welcome(message):
    user_id = message.from_user.id
    async with db_lock:
        pool = await create_db_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as c:
                await c.execute("INSERT IGNORE INTO users (user_id, balance) VALUES (%s, 0)", (user_id,))
        pool.close()
        await pool.wait_closed()

    markup = InlineKeyboardMarkup()
    markup.row_width = 1
    markup.add(
        InlineKeyboardButton("📋 AKKOUNTLARIM", callback_data="my_accounts"),
        InlineKeyboardButton("➕ AKKOUNT QUSHISH", callback_data="add_account"),
        InlineKeyboardButton("💰 HISOBIM", callback_data="my_balance"),
        InlineKeyboardButton("🔄 TAKRORIY XABARLAR", callback_data="manage_recurring")
    )
    await bot.send_message(message.chat.id, "Assalomu alaykum! Botimizga hush kelibsiz! Kerakli bo‘limni tanlang 👇", reply_markup=markup)

# Callback so'rovlarni qayta ishlash
@bot.callback_query_handler(func=lambda call: True)
async def callback_query(call):
    user_id = call.from_user.id
    try:
        if call.data == "my_accounts":
            await show_accounts(call)
        elif call.data == "add_account":
            user_data[user_id] = {"step": "phone"}
            await bot.send_message(call.message.chat.id, "📱 Telefon raqamni kiriting (+998xxxxxxxxx):")
        elif call.data == "my_balance":
            await show_balance(call)
        elif call.data == "manage_recurring":
            await show_recurring_messages(call)
        elif call.data.startswith("account_"):
            account_index = int(call.data.split("_")[1])
            async with db_lock:
                pool = await create_db_pool()
                async with pool.acquire() as conn:
                    async with conn.cursor() as c:
                        await c.execute("SELECT phone FROM accounts WHERE user_id = %s", (user_id,))
                        accounts = await c.fetchall()
                pool.close()
                await pool.wait_closed()
            if account_index < len(accounts):
                user_data[user_id] = {"selected_phone": accounts[account_index][0], "step": "group_ids"}
                await bot.send_message(call.message.chat.id, f"📞 {accounts[account_index][0]} raqami tanlandi. Guruh yoki kanal ID larini kiriting (vergul bilan ajrating, masalan: -100123456789,-100987654321):")
        elif call.data == "top_up_balance":
            await bot.send_message(call.message.chat.id, "Hisobni to‘ldirish uchun @Baxriddinovich_dev ga murojaat qiling.",
                                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("To‘lov qilish", url="https://t.me/Baxriddinovich_dev")]]))
        elif call.data == "back":
            await send_welcome(call.message)
        elif call.data.startswith("cancel_"):
            message_id = int(call.data.split("_")[1])
            await cancel_recurring_message(user_id, message_id, call.message.chat.id)
    except Exception as e:
        logger.error(f"Callback xatosi: {e}")
        await bot.send_message(call.message.chat.id, f"❌ Xato yuz berdi: {e}")

# Akkauntlarni ko‘rsatish
async def show_accounts(call):
    user_id = call.from_user.id
    async with db_lock:
        pool = await create_db_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as c:
                await c.execute("SELECT phone FROM accounts WHERE user_id = %s", (user_id,))
                accounts = await c.fetchall()
        pool.close()
        await pool.wait_closed()

    if not accounts:
        await bot.send_message(call.message.chat.id, "Sizda hali hech qanday akkaunt yo‘q.")
        return

    text = "Sizning akkauntlaringiz:\n"
    for i, account in enumerate(accounts, 1):
        text += f"{i}. {account[0]}\n"

    markup = InlineKeyboardMarkup()
    for i in range(len(accounts)):
        markup.add(InlineKeyboardButton(str(i + 1), callback_data=f"account_{i}"))
    markup.add(InlineKeyboardButton("Ortga qaytish", callback_data="back"))
    await bot.send_message(call.message.chat.id, text, reply_markup=markup)

# Hisobni ko‘rsatish
async def show_balance(call):
    user_id = call.from_user.id
    async with db_lock:
        pool = await create_db_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as c:
                await c.execute("SELECT balance FROM users WHERE user_id = %s", (user_id,))
                balance = (await c.fetchone())[0]
        pool.close()
        await pool.wait_closed()

    text = f"Sizning ID: {user_id}\nSizning hisobingiz: {balance} so‘m"
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("Hisobimni to‘ldirish", callback_data="top_up_balance"))
    markup.add(InlineKeyboardButton("Ortga qaytish", callback_data="back"))
    await bot.send_message(call.message.chat.id, text, reply_markup=markup)

# Takroriy xabarlarni ko‘rsatish
async def show_recurring_messages(call):
    user_id = call.from_user.id
    async with db_lock:
        pool = await create_db_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as c:
                await c.execute("SELECT message_id, group_ids, message_text, send_interval FROM messages WHERE user_id = %s AND is_recurring = 1", (user_id,))
                messages = await c.fetchall()
        pool.close()
        await pool.wait_closed()

    if not messages:
        await bot.send_message(call.message.chat.id, "Sizda takroriy xabarlar yo‘q.")
        return

    text = "Sizning takroriy xabarlaringiz:\n"
    markup = InlineKeyboardMarkup()
    for msg in messages:
        message_id, group_ids, message_text, send_interval = msg
        text += f"ID: {message_id}, Kanallar: {group_ids}, Matn: {message_text}, Interval: {send_interval} min\n"
        markup.add(InlineKeyboardButton(f"Cancel ID {message_id}", callback_data=f"cancel_{message_id}"))
    markup.add(InlineKeyboardButton("Ortga qaytish", callback_data="back"))
    await bot.send_message(call.message.chat.id, text, reply_markup=markup)

# Takroriy xabarni bekor qilish
async def cancel_recurring_message(user_id, message_id, chat_id):
    if (user_id, message_id) in recurring_tasks:
        recurring_tasks[(user_id, message_id)].cancel()
        del recurring_tasks[(user_id, message_id)]
    async with db_lock:
        pool = await create_db_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as c:
                await c.execute("DELETE FROM messages WHERE message_id = %s AND user_id = %s", (message_id, user_id))
        pool.close()
        await pool.wait_closed()
    await bot.send_message(chat_id, f"Xabar ID {message_id} bekor qilindi.")

# Matn va rasm xabarlarini qayta ishlash
@bot.message_handler(content_types=['text', 'photo'])
async def handle_text_photo(message):
    user_id = message.from_user.id
    if user_id not in user_data:
        return

    step = user_data[user_id].get("step")
    if step == "phone":
        phone = message.text.strip().replace(" ", "")
        if not phone.startswith("+998") or len(phone) != 13:
            await bot.send_message(message.chat.id, "❌ Raqam noto‘g‘ri. Iltimos, +998xxxxxxxxx formatida kiriting.")
            return
        user_data[user_id]["phone"] = phone
        user_data[user_id]["step"] = "code"
        await bot.send_message(message.chat.id, "⏳ Kod yuborilmoqda...")
        await send_code_request(user_id, phone)
    elif step == "code":
        user_data[user_id]["code"] = message.text.strip()
        user_data[user_id]["step"] = "password"
        await bot.send_message(message.chat.id, "🔐 2-bosqichli parolni kiriting:")
    elif step == "password":
        password = message.text.strip()
        await complete_login(user_id, message, password)
    elif step == "group_ids":
        try:
            group_ids = [int(gid) for gid in message.text.split(",") if gid.strip()]
            session_file = f"sessions/session_{user_id}_{user_data[user_id]['selected_phone']}.session"
            async with get_session_lock(session_file):
                client = TelegramClient(session_file, api_id=API_ID, api_hash=API_HASH, session=SQLiteSession(session_file, timeout=10))
                await client.connect()
                if not await client.is_user_authorized():
                    await bot.send_message(message.chat.id, "❌ Akkaunt avtorizatsiya qilinmagan.")
                    await client.disconnect()
                    return
                invalid_ids = []
                for gid in group_ids:
                    try:
                        await client.get_input_entity(gid)
                    except (ChannelInvalidError, ChannelPrivateError, PeerIdInvalidError):
                        invalid_ids.append(gid)
                await client.disconnect()
                if invalid_ids:
                    await bot.send_message(message.chat.id, f"❌ Quyidagi ID lar noto‘g‘ri yoki kirish huquqi yo‘q: {invalid_ids}. Iltimos, to‘g‘ri ID larni kiriting.")
                    return
            user_data[user_id]["group_ids"] = group_ids
            user_data[user_id]["step"] = "message_content"
            await bot.send_message(message.chat.id, "📝 Yuboriladigan xabar matnini kiriting yoki rasm yuboring:")
        except ValueError:
            await bot.send_message(message.chat.id, "Iltimos, to‘g‘ri guruh yoki kanal ID larini kiriting (masalan: -100123456789,-100987654321).")
    elif step == "message_content":
        user_data[user_id]["message_text"] = message.text if message.text else ""
        user_data[user_id]["media_file_id"] = message.photo[-1].file_id if message.photo else None
        user_data[user_id]["step"] = "send_interval"
        await bot.send_message(message.chat.id, "⏰ Xabar har qancha vaqtda takrorlanib yuborilsin (minutda)?")
    elif step == "send_interval":
        try:
            send_interval = int(message.text)
            if send_interval <= 0:
                await bot.send_message(message.chat.id, "Iltimos, 1 yoki undan katta raqam kiriting.")
                return
            user_data[user_id]["send_interval"] = send_interval
            await schedule_message(user_id)
            await bot.send_message(message.chat.id, f"✅ Xabar har {send_interval} minutda yuboriladi.")
            user_data[user_id]["step"] = None
        except ValueError:
            await bot.send_message(message.chat.id, "Iltimos, raqam kiriting.")

# Telegram kirish
async def send_code_request(user_id, phone):
    os.makedirs("sessions", exist_ok=True)
    session_file = f"sessions/session_{user_id}_{phone}.session"
    async with get_session_lock(session_file):
        client = TelegramClient(session_file, api_id=API_ID, api_hash=API_HASH, session=SQLiteSession(session_file, timeout=10))
        try:
            await client.connect()
            if not await client.is_user_authorized():
                sent_code = await client.send_code_request(phone)
                user_data[user_id]["phone_code_hash"] = sent_code.phone_code_hash
                user_data[user_id]["client"] = client
                await bot.send_message(user_id, "✅ Kod yuborildi! 📩 SMS kodni kiriting:")
            else:
                await client.disconnect()
        except Exception as e:
            logger.error(f"Kod yuborish xatosi: {e}")
            await bot.send_message(user_id, f"❌ Kod yuborish xatosi: {e}")

async def complete_login(user_id, message, password):
    phone = user_data[user_id]["phone"]
    code = user_data[user_id]["code"]
    phone_code_hash = user_data[user_id].get("phone_code_hash")
    client = user_data[user_id].get("client")
    session_file = f"sessions/session_{user_id}_{phone}.session"

    async with get_session_lock(session_file):
        if not client:
            client = TelegramClient(session_file, api_id=API_ID, api_hash=API_HASH, session=SQLiteSession(session_file, timeout=10))
            await client.connect()

        try:
            if not await client.is_user_authorized():
                try:
                    await client.sign_in(phone, code, phone_code_hash=phone_code_hash)
                    await bot.send_message(message.chat.id, "❌ Faqat ikki bosqichli tasdiqlash (2FA) yoqilgan akkauntlar qabul qilinadi.")
                    user_data[user_id]["step"] = None
                    user_data[user_id]["client"] = None
                    return
                except SessionPasswordNeededError:
                    if not password:
                        await bot.send_message(message.chat.id, "❌ 2-bosqichli parol talab qilinadi.")
                        return
                    try:
                        await client.sign_in(password=password)
                    except Exception as e:
                        await bot.send_message(message.chat.id, f"❌ Parol noto‘g‘ri: {e}.")
                        user_data[user_id]["step"] = "password"
                        return
            async with db_lock:
                pool = await create_db_pool()
                async with pool.acquire() as conn:
                    async with conn.cursor() as c:
                        await c.execute("INSERT INTO accounts (user_id, phone, session_file) VALUES (%s, %s, %s)", (user_id, phone, session_file))
                pool.close()
                await pool.wait_closed()
            await bot.send_message(message.chat.id, "✅ Akkaunt muvaffaqiyatli qo‘shildi!")
            user_data[user_id]["step"] = None
            user_data[user_id]["client"] = None
            await client.disconnect()
        except Exception as e:
            logger.error(f"Kirish xatosi: {e}")
            await bot.send_message(message.chat.id, f"❌ Kirish xatosi: {e}")

# Xabar yuborishni rejalashtirish
async def schedule_message(user_id):
    phone = user_data[user_id]["selected_phone"]
    group_ids = user_data[user_id]["group_ids"]
    message_text = user_data[user_id]["message_text"]
    media_file_id = user_data[user_id]["media_file_id"]
    send_interval = user_data[user_id]["send_interval"]

    async with db_lock:
        pool = await create_db_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as c:
                await c.execute("INSERT INTO messages (user_id, phone, group_ids, message_text, media_file_id, send_interval, is_recurring) VALUES (%s, %s, %s, %s, %s, %s, 1)",
                                (user_id, phone, ",".join(map(str, group_ids)), message_text, media_file_id, send_interval))
                message_id = c.lastinsertid
        pool.close()
        await pool.wait_closed()

    task = asyncio.create_task(run_recurring_message(user_id, message_id, phone, group_ids, message_text, media_file_id, send_interval))
    recurring_tasks[(user_id, message_id)] = task

async def run_recurring_message(user_id, message_id, phone, group_ids, message_text, media_file_id, send_interval):
    try:
        while True:
            await send_message_to_channels(user_id, phone, group_ids, message_text, media_file_id)
            await asyncio.sleep(send_interval * 60)
    except asyncio.CancelledError:
        logger.info(f"Takroriy xabar {message_id} foydalanuvchi {user_id} uchun bekor qilindi")
    except Exception as e:
        logger.error(f"Takroriy xabar {message_id} da xato: {e}")
        await bot.send_message(user_id, f"❌ Takroriy xabar yuborishda xato: {e}")

async def send_message_to_channels(user_id, phone, group_ids, message_text, media_file_id):
    session_file = f"sessions/session_{user_id}_{phone}.session"
    async with get_session_lock(session_file):
        try:
            client = TelegramClient(session_file, api_id=API_ID, api_hash=API_HASH, session=SQLiteSession(session_file, timeout=10))
            await client.connect()
            if not await client.is_user_authorized():
                logger.error("Client avtorizatsiya qilinmagan")
                await bot.send_message(user_id, "❌ Akkaunt avtorizatsiya qilinmagan.")
                return

            for gid in group_ids:
                try:
                    entity = await client.get_input_entity(gid)
                    if media_file_id:
                        file_info = await bot.get_file(media_file_id)
                        file_path = file_info.file_path
                        downloaded_file = await bot.download_file(file_path)
                        with open(f"temp_media_{gid}.jpg", "wb") as f:
                            f.write(downloaded_file)
                        await client.send_file(entity, f"temp_media_{gid}.jpg", caption=message_text)
                        os.remove(f"temp_media_{gid}.jpg")
                    else:
                        await client.send_message(entity, message_text)
                    logger.info(f"Xabar {gid} kanaliga {phone} orqali yuborildi")
                except (ChannelInvalidError, ChannelPrivateError, PeerIdInvalidError) as e:
                    logger.error(f"Kanal {gid} uchun xato: {e}")
                    await bot.send_message(user_id, f"❌ Kanal {gid} ga xabar yuborishda xato: {e}")
            await client.disconnect()
        except Exception as e:
            logger.error(f"Xabar yuborishda xato: {e}")
            await bot.send_message(user_id, f"❌ Xabar yuborishda xato: {e}")

# Admin paneli
@bot.message_handler(commands=['admin'])
async def admin_panel(message):
    user_id = message.from_user.id
    if user_id != ADMIN_USER_ID:
        await bot.send_message(message.chat.id, "❌ Sizda admin huquqlari yo‘q.")
        return

    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("📊 Statistika", callback_data="stats"),
        InlineKeyboardButton("👤 Foydalanuvchilarni boshqarish", callback_data="manage_users")
    )
    await bot.send_message(message.chat.id, "Admin paneli:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data in ["stats", "manage_users", "add_funds", "remove_funds"])
async def admin_callbacks(call):
    user_id = call.from_user.id
    if user_id != ADMIN_USER_ID:
        await bot.send_message(call.message.chat.id, "❌ Sizda admin huquqlari yo‘q.")
        return

    try:
        if call.data == "stats":
            async with db_lock:
                pool = await create_db_pool()
                async with pool.acquire() as conn:
                    async with conn.cursor() as c:
                        await c.execute("SELECT COUNT(*) FROM users")
                        user_count = (await c.fetchone())[0]
                        await c.execute("SELECT COUNT(*) FROM accounts")
                        account_count = (await c.fetchone())[0]
                        await c.execute("SELECT COUNT(*) FROM messages")
                        message_count = (await c.fetchone())[0]
                pool.close()
                await pool.wait_closed()
            await bot.send_message(call.message.chat.id, f"📊 Statistika:\nFoydalanuvchilar: {user_count}\nAkkauntlar: {account_count}\nXabarlar: {message_count}")
        elif call.data == "manage_users":
            markup = InlineKeyboardMarkup()
            markup.add(
                InlineKeyboardButton("💸 Pul qo‘shish", callback_data="add_funds"),
                InlineKeyboardButton("💸 Pul ayirish", callback_data="remove_funds")
            )
            await bot.send_message(call.message.chat.id, "Foydalanuvchi ID sini kiriting:", reply_markup=markup)
            user_data[user_id] = {"step": "manage_user_id"}
    except Exception as e:
        logger.error(f"Admin callback xatosi: {e}")
        await bot.send_message(call.message.chat.id, f"❌ Xato yuz berdi: {e}")

@bot.message_handler(func=lambda message: user_data.get(message.from_user.id, {}).get("step") == "manage_user_id")
async def manage_user_id(message):
    user_id = message.from_user.id
    if user_id != ADMIN_USER_ID:
        await bot.send_message(message.chat.id, "❌ Sizda admin huquqlari yo‘q.")
        return

    try:
        target_user_id = int(message.text)
        async with db_lock:
            pool = await create_db_pool()
            async with pool.acquire() as conn:
                async with conn.cursor() as c:
                    await c.execute("SELECT COUNT(*) FROM users WHERE user_id = %s", (target_user_id,))
                    exists = (await c.fetchone())[0]
            pool.close()
            await pool.wait_closed()
        if not exists:
            await bot.send_message(message.chat.id, "❌ Bunday foydalanuvchi topilmadi.")
            return
        user_data[user_id]["target_user_id"] = target_user_id
        user_data[user_id]["step"] = "manage_funds"
        await bot.send_message(message.chat.id, "Qancha pul qo‘shish/ayirish (so‘mda)?")
    except ValueError:
        await bot.send_message(message.chat.id, "Iltimos, to‘g‘ri ID kiriting.")
    except Exception as e:
        logger.error(f"Foydalanuvchi ID boshqaruv xatosi: {e}")
        await bot.send_message(message.chat.id, f"❌ Xato yuz berdi: {e}")

@bot.message_handler(func=lambda message: user_data.get(message.from_user.id, {}).get("step") == "manage_funds")
async def manage_funds(message):
    user_id = message.from_user.id
    if user_id != ADMIN_USER_ID:
        await bot.send_message(message.chat.id, "❌ Sizda admin huquqlari yo‘q.")
        return

    try:
        amount = int(message.text)
        target_user_id = user_data[user_id]["target_user_id"]
        async with db_lock:
            pool = await create_db_pool()
            async with pool.acquire() as conn:
                async with conn.cursor() as c:
                    await c.execute("UPDATE users SET balance = balance + %s WHERE user_id = %s", (amount, target_user_id))
            pool.close()
            await pool.wait_closed()
        await bot.send_message(message.chat.id, f"Hisob o‘zgartirildi: {amount} so‘m")
        user_data[user_id]["step"] = None
    except ValueError:
        await bot.send_message(message.chat.id, "Iltimos, raqam kiriting.")
    except Exception as e:
        logger.error(f"Hisob boshqaruv xatosi: {e}")
        await bot.send_message(message.chat.id, f"❌ Xato yuz berdi: {e}")

# Takroriy vazifalarni tiklash
async def restore_recurring_tasks():
    async with db_lock:
        pool = await create_db_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as c:
                await c.execute("SELECT message_id, user_id, phone, group_ids, message_text, media_file_id, send_interval FROM messages WHERE is_recurring = 1")
                messages = await c.fetchall()
        pool.close()
        await pool.wait_closed()

    for msg in messages:
        message_id, user_id, phone, group_ids, message_text, media_file_id, send_interval = msg
        group_ids = [int(gid) for gid in group_ids.split(",") if gid.strip()]
        task = asyncio.create_task(run_recurring_message(user_id, message_id, phone, group_ids, message_text, media_file_id, send_interval))
        recurring_tasks[(user_id, message_id)] = task

# Botni ishga tushirish
async def main():
    await init_db()
    await restore_recurring_tasks()
    print("🚀 Bot ishga tushdi")
    await bot.polling()

if __name__ == "__main__":
    asyncio.run(main())