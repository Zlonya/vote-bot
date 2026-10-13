import logging
import csv
import io
from datetime import datetime
from typing import Dict, List, Optional
from uuid import uuid4

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ConversationHandler,
    ContextTypes,
)

from models import User, VoteItem, Vote, VoteSession
from storage import Storage

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)
fh = logging.FileHandler("bot.log", encoding="utf-8")
fh.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
logger.addHandler(fh)

# Conversation states
# (no longer used - new_vote uses user_data flags)

# User ranking state storage: user_id -> {session_id: [ordered item names]}
user_ranking_state: Dict[int, Dict[str, List[str]]] = {}

# Admin ids currently awaiting a broadcast message
notify_awaiting: Dict[int, bool] = {}


class NotifyAwaitingFilter(filters.MessageFilter):
    def filter(self, message):
        return message.from_user and message.from_user.id in notify_awaiting


notify_awaiting_filter = NotifyAwaitingFilter()


def get_admin_keyboard():
    buttons = [
        [KeyboardButton("/approve_users")],
        [KeyboardButton("/list_users")],
        [KeyboardButton("/new_vote")],
        [KeyboardButton("/list_votes")],
        [KeyboardButton("/active")],
        [KeyboardButton("/results")],
        [KeyboardButton("/notify_all")],
        [KeyboardButton("/help")],
    ]
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)


def get_user_keyboard():
    buttons = [
        [KeyboardButton("/my_votes")],
        [KeyboardButton("/results")],
        [KeyboardButton("/help")],
    ]
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)


def is_admin(user_id: int, config: dict) -> bool:
    return user_id == config["admin_id"]


# ==================== HELPERS ====================

def esc(text: str) -> str:
    for ch in ("\\", "_", "*", "[", "]", "`", "(", ")"):
        text = text.replace(ch, "\\" + ch)
    return text


def build_ranking_keyboard(session_id: str, current_ranks: List[str]):
    keyboard = []
    for idx, item in enumerate(current_ranks):
        display_idx = idx + 1
        txt = f"{display_idx}. {item}"
        up = InlineKeyboardButton("▲", callback_data=f"rup_{session_id}_{idx}")
        dn = InlineKeyboardButton("▼", callback_data=f"rdn_{session_id}_{idx}")
        keyboard.append([InlineKeyboardButton(txt, callback_data="noop")])
        keyboard.append([up, dn])
    keyboard.append([InlineKeyboardButton("✅ Подтвердить", callback_data=f"rcon_{session_id}")])
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data=f"rcan_{session_id}")])
    return InlineKeyboardMarkup(keyboard)


def format_session_status(session: VoteSession, storage: Storage) -> str:
    approved = storage.get_approved_users()
    total = len(approved)
    voted_ids = {v.user_id for v in session.votes}
    voted = len(voted_ids)
    lines = [
        f"📊 *{esc(session.name)}* (ID: `{session.id}`)",
        f"Пункты: {esc(', '.join(i.name for i in session.items))}",
        f"Проголосовало: {voted}/{total}",
        f"Статус: {'✅ Активно' if session.is_active else '❌ Завершено'}",
        f"Результаты: {'✅ открыты' if session.results_published else '❌ скрыты'}",
    ]
    lines.append("")
    lines.append("✅ Проголосовавшие:")
    if voted_ids:
        for u in approved:
            if u.id in voted_ids:
                lines.append(f"  - {esc(u.nickname or u.username)}")
    else:
        lines.append("  (пока никто)")
    lines.append("")
    lines.append("❌ Не проголосовавшие:")
    not_voted = [u for u in approved if u.id not in voted_ids]
    if not_voted:
        for u in not_voted:
            lines.append(f"  - {esc(u.nickname or u.username)}")
    else:
        lines.append("  (все проголосовали)")
    return "\n".join(lines)


# ==================== ADMIN HANDLERS ====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name
    cfg = context.bot_data.get("config", {})
    storage: Storage = context.bot_data["storage"]

    if is_admin(user_id, cfg):
        await update.message.reply_text(
            "👋 Панель администратора. Полный список команд и описание бота: /help",
            reply_markup=get_admin_keyboard(),
        )
        return

    existing = storage.get_user(user_id)
    if not existing:
        context.user_data["reg_state"] = "awaiting_nickname"
        context.user_data["reg_id"] = user_id
        context.user_data["reg_username"] = username
        await update.message.reply_text(
            "Добро пожаловать! Вы ещё не зарегистрированы в системе.\n"
            "Введите ваш никнейм (как вас будут видеть в результатах):"
        )
        return

    if not existing.approved:
        if existing.username != username:
            existing.username = username
            storage.set_user(existing)
        await update.message.reply_text(
            "Вы зарегистрированы, но ваша заявка ещё не одобрена администратором.\n"
            "Ожидайте подтверждения."
        )
        return

    if existing.username != username:
        existing.username = username
        storage.set_user(existing)
    sessions = storage.get_active_sessions()
    msg = "👋 Вы зарегистрированы в системе голосования."
    if sessions:
        msg += f"\n\n📢 Активных голосований: {len(sessions)}\nИспользуйте /my_votes чтобы участвовать."
    await update.message.reply_text(
        msg,
        reply_markup=get_user_keyboard(),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    cfg = context.bot_data.get("config", {})
    storage: Storage = context.bot_data["storage"]

    if is_admin(user_id, cfg):
        await update.message.reply_text(
            "🤖 *Бот для проведения голосований*\n\n"
            "Система: админ создаёт голосования, зарегистрированные участники ранжируют пункты. "
            "Результаты считаются по группам: Жюри (A) с весом 0.7, Электорат (B) с весом 0.3. "
            "У группы B отбрасываются крайние оценки при трёх и более голосах.\n\n"
            "*Команды админа:*\n"
            "`/add_user <id> <ник> <A/B>` — добавить пользователя\n"
            "`/approve_users` — одобрить/отклонить заявки\n"
            "`/remove_user` — удалить пользователя (по кнопкам)\n"
            "`/list_users` — список пользователей по группам\n"
            "`/new_vote` — создать голосование (шаг за шагом)\n"
            "`/remove_vote` — удалить голосование (по кнопкам)\n"
            "`/list_votes` — список голосований (кто проголосовал)\n"
            "`/active` — активные голосования (завершить, напомнить)\n"
            "`/end_vote <ID>` — завершить голосование\n"
            "`/publish_results <ID>` — открыть результаты участникам\n"
            "`/results` — результаты завершённых голосований (+ скачать CSV)\n"
            "`/export <ID>` — выгрузить CSV с результатами\n"
            "`/notify_all` — разослать сообщение всем (текст/фото/файл)\n"
            "`/reset_all` — удалить всех пользователей и голосования\n\n"
            "*Для участников:*\n"
            "`/my_votes` — активные голосования и голосование\n"
            "`/results` — опубликованные результаты\n",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_admin_keyboard(),
        )
        return

    user = storage.get_user(user_id)
    if not user:
        await update.message.reply_text(
            "🤖 *Бот для проведения голосований*\n\n"
            "Чтобы участвовать, отправьте /start и пройдите регистрацию: "
            "укажите никнейм и выберите группу (Жюри или Электорат). "
            "После одобрения администратором вы сможете голосовать.\n\n"
            "*Команды:*\n"
            "`/start` — регистрация и вход\n"
            "`/my_votes` — активные голосования\n"
            "`/results` — опубликованные результаты",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if not user.approved:
        await update.message.reply_text(
            "🤖 *Бот для проведения голосований*\n\n"
            "Вы зарегистрированы, но ваша заявка ещё не одобрена администратором. "
            "Когда вас одобрят, сможете голосовать с помощью /my_votes.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    await update.message.reply_text(
        "🤖 *Бот для проведения голосований*\n\n"
        "Вам приходят голосования на ранжирование. Нажимайте «🗳 Голосовать», "
        "расставляйте пункты стрелками ▲▼ и подтверждайте выбор.\n\n"
        "*Команды:*\n"
        "`/my_votes` — активные голосования\n"
        "`/results` — результаты опубликованных голосований\n\n"
        "О новых голосованиях и напоминаниях приходят уведомления автоматически.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=get_user_keyboard(),
    )


async def add_user_handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 3:
        await update.message.reply_text(
            "Использование: /add_user <id или @username> <никнейм> <A/B>\n"
            "Пример: /add_user 123456789 Мария A"
        )
        return

    raw_id = args[0]
    nickname = args[1]
    group = args[2].upper()
    if group not in ("A", "B"):
        await update.message.reply_text("Группа должна быть A или B.")
        return

    storage: Storage = context.bot_data["storage"]
    uid = None
    uname = raw_id

    if raw_id.startswith("@"):
        uname = raw_id[1:]
        all_users = storage.get_all_users()
        found = [u for u in all_users if u.username == uname]
        if found:
            uid = found[0].id
        else:
            await update.message.reply_text(
                f"Пользователь @{uname} ещё не писал боту. Попросите его написать /start, "
                f"после чего повторите /add_user @{uname} {nickname} {group}."
            )
            return
    else:
        try:
            uid = int(raw_id)
        except ValueError:
            await update.message.reply_text("ID должен быть числом или @username.")
            return

    user = User(id=uid, username=uname, group=group, approved=False, nickname=nickname)
    storage.set_user(user)
    await update.message.reply_text(
        f"Пользователь {nickname} (группа {group}) добавлен.\n"
        f"Теперь одобрьте его через /approve_users"
    )


async def reg_group_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if not data.startswith("reg_group_"):
        return
    group = data.split("_")[2]

    reg_state = context.user_data.get("reg_state")
    if reg_state != "awaiting_group":
        await query.edit_message_text("❌ Регистрация не найдена. Нажмите /start заново.")
        return

    user_id = context.user_data["reg_id"]
    username = context.user_data["reg_username"]
    nickname = context.user_data["reg_nickname"]
    storage: Storage = context.bot_data["storage"]

    user = User(id=user_id, username=username, group=group, approved=False, nickname=nickname)
    storage.set_user(user)
    context.user_data.pop("reg_state", None)
    context.user_data.pop("reg_id", None)
    context.user_data.pop("reg_username", None)
    context.user_data.pop("reg_nickname", None)

    group_label = "Жюри" if group == "A" else "Электорат"

    await query.edit_message_text(
        f"✅ Заявка отправлена!\n"
        f"Никнейм: {nickname}\n"
        f"Категория: {group_label}\n\n"
        f"Ожидайте одобрения администратора."
    )


async def approve_users_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    storage: Storage = context.bot_data["storage"]
    users = storage.get_all_users()
    pending = [u for u in users if not u.approved]
    if not pending:
        await update.message.reply_text("Нет пользователей, ожидающих одобрения.")
        return

    keyboard = []
    for u in pending:
        label = f"{u.nickname}"
        if u.username:
            label += f" (@{u.username})"
        label += f" — группа {u.group}"
        keyboard.append([
            InlineKeyboardButton(f"✅ {label}", callback_data=f"approve_{u.id}"),
            InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_{u.id}"),
        ])
    keyboard.append([InlineKeyboardButton("✅ Одобрить всех", callback_data="approve_all")])
    keyboard.append([InlineKeyboardButton("❌ Отклонить всех", callback_data="reject_all")])
    keyboard.append([InlineKeyboardButton("◀ Назад", callback_data="approve_cancel")])

    await update.message.reply_text(
        "Выберите пользователя для одобрения:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def approve_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    storage: Storage = context.bot_data["storage"]
    cfg = context.bot_data.get("config", {})
    if not is_admin(query.from_user.id, cfg):
        await query.edit_message_text("❌ Только администратор может одобрять пользователей.")
        return

    if data == "approve_all":
        users = storage.get_all_users()
        for u in users:
            if not u.approved:
                u.approved = True
                storage.set_user(u)
        await query.edit_message_text("✅ Все пользователи одобрены.")
    elif data == "reject_all":
        users = storage.get_all_users()
        rejected = 0
        for u in users:
            if not u.approved:
                del storage.data["users"][str(u.id)]
                rejected += 1
        storage._save()
        await query.edit_message_text(f"❌ Отклонены все ({rejected}) пользователей.")
    elif data == "approve_cancel":
        await query.edit_message_text("❌ Отменено.")
    elif data.startswith("approve_"):
        uid = int(data.split("_")[1])
        u = storage.get_user(uid)
        if u:
            u.approved = True
            storage.set_user(u)
            await query.edit_message_text(f"✅ Пользователь {u.nickname or u.username} одобрен.")
    elif data.startswith("reject_"):
        uid = int(data.split("_")[1])
        u = storage.get_user(uid)
        if u:
            del storage.data["users"][str(uid)]
            storage._save()
            await query.edit_message_text(f"❌ Пользователь {u.nickname or u.username} отклонён.")


async def new_vote_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["vote_state"] = "awaiting_name"
    await update.message.reply_text(
        "Введите название голосования:\n(или /cancel для отмены)"
    )


async def new_vote_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reg_state = context.user_data.get("reg_state")
    if reg_state == "awaiting_nickname":
        nickname = update.message.text.strip()
        if not nickname:
            await update.message.reply_text("Никнейм не может быть пустым. Введите никнейм:")
            return
        context.user_data["reg_nickname"] = nickname
        context.user_data["reg_state"] = "awaiting_group"
        await update.message.reply_text(
            "Теперь выберите, кем вы являетесь:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Жюри", callback_data="reg_group_A")],
                [InlineKeyboardButton("Электорат", callback_data="reg_group_B")],
            ]),
        )
        return

    state = context.user_data.get("vote_state")
    if state == "awaiting_name":
        context.user_data["new_vote_name"] = update.message.text.strip()
        context.user_data["vote_state"] = "awaiting_items"
        await update.message.reply_text(
            "Введите пункты для голосования, каждый с новой строки:\n"
            "Пример:\n"
            "Вариант 1\n"
            "Вариант 2\n"
            "Вариант 3"
        )
        return

    if state == "awaiting_items":
        text = update.message.text.strip()
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        if len(lines) < 2:
            await update.message.reply_text("Нужно минимум 2 пункта. Попробуйте снова.")
            return

        items = [VoteItem(name=l) for l in lines]
        session = VoteSession(
            id=str(uuid4())[:8],
            name=context.user_data["new_vote_name"],
            items=items,
        )
        storage: Storage = context.bot_data["storage"]
        storage.create_session(session)

        context.user_data.pop("vote_state", None)
        context.user_data.pop("new_vote_name", None)

        await update.message.reply_text(
            f"✅ Голосование создано!\n\n"
            f"Название: {session.name}\n"
            f"ID: `{session.id}`\n"
            f"Пункты: {', '.join(l for l in lines)}"
        )

        # Notify all approved users
        approved = storage.get_approved_users()
        notified = 0
        errors = []
        for u in approved:
            try:
                await send_vote_to_user(context.bot, u, session, storage)
                notified += 1
            except Exception as e:
                errors.append(f"User {u.id} ({u.nickname or u.username}): {e}")
                logger.warning(f"Could not notify user {u.id}: {e}")

        msg = f"Уведомления отправлены {notified} из {len(approved)} пользователей."
        if errors:
            msg += "\n\nОшибки:\n" + "\n".join(errors)
        await update.message.reply_text(
            msg,
            reply_markup=get_admin_keyboard(),
        )


async def notify_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    cfg = context.bot_data.get("config", {})
    if not is_admin(user_id, cfg):
        await update.message.reply_text("❌ Только администратор.")
        return

    notify_awaiting[user_id] = True
    await update.message.reply_text(
        "📣 Отправьте сообщение для рассылки всем пользователям.\n\n"
        "Поддерживается любой формат: текст с форматированием, ссылки, фото, файлы и подписи.\n"
        "Сообщение будет доставлено в том же виде.\n\n"
        "Чтобы отменить — /cancel"
    )


async def notify_capture(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not notify_awaiting.get(user_id):
        return None

    cfg = context.bot_data.get("config", {})
    if not is_admin(user_id, cfg):
        await update.message.reply_text("❌ Только администратор.")
        notify_awaiting.pop(user_id, None)
        return True

    storage: Storage = context.bot_data["storage"]
    approved = storage.get_approved_users()

    sent = 0
    errors = []
    for u in approved:
        try:
            await context.bot.copy_message(
                chat_id=u.id,
                from_chat_id=update.effective_chat.id,
                message_id=update.message.message_id,
            )
            sent += 1
        except Exception as e:
            errors.append(f"@{u.username}: {e}")

    notify_awaiting.pop(user_id, None)
    msg = f"✅ Отправлено {sent} из {len(approved)} пользователей."
    if errors:
        msg += "\n\nОшибки:\n" + "\n".join(errors)
    await update.message.reply_text(msg)
    return True


async def list_votes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    storage: Storage = context.bot_data["storage"]
    sessions = storage.get_all_sessions()
    if not sessions:
        await update.message.reply_text("Нет голосований.")
        return

    for s in sessions:
        text = format_session_status(s, storage)
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def list_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    storage: Storage = context.bot_data["storage"]
    users = storage.get_all_users()
    if not users:
        await update.message.reply_text("Нет пользователей.")
        return

    group_a = [u for u in users if u.group == "A"]
    group_b = [u for u in users if u.group == "B"]

    lines = ["📋 *Список пользователей:*\n"]
    lines.append(f"*Группа A (вес 0.7):*")
    for u in group_a:
        status = "✅" if u.approved else "❌"
        lines.append(f"  {status} {esc(u.nickname or u.username)} (ID: {u.id})")
    lines.append(f"\n*Группа B (вес 0.3):*")
    for u in group_b:
        status = "✅" if u.approved else "❌"
        lines.append(f"  {status} {esc(u.nickname or u.username)} (ID: {u.id})")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def active_votes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    storage: Storage = context.bot_data["storage"]
    sessions = storage.get_active_sessions()
    if not sessions:
        await update.message.reply_text("Нет активных голосований.")
        return

    for s in sessions:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🛑 Завершить голосование", callback_data=f"endvote_{s.id}")],
            [InlineKeyboardButton("🔔 Напомнить непроголосовавшим", callback_data=f"remind_{s.id}")],
        ])
        await update.message.reply_text(
            format_session_status(s, storage), parse_mode=ParseMode.MARKDOWN,
            reply_markup=keyboard,
        )


async def end_vote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Использование: /end_vote <ID>")
        return

    storage: Storage = context.bot_data["storage"]
    session = storage.get_session(args[0])
    if not session:
        await update.message.reply_text("Голосование не найдено.")
        return
    if not session.is_active:
        await update.message.reply_text("❌ Голосование уже завершено.")
        return

    session.is_active = False
    session.is_finished = True
    session.results_published = False
    storage.update_session(session)

    # Only admin sees results; users must wait for publication
    results = compute_results(session, storage)
    results_text = format_results(session, results)
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Открыть результаты участникам", callback_data=f"pubres_{session.id}")]
    ])
    await update.message.reply_text(
        f"📊 Результаты *{esc(session.name)}* (видны только вам):\n\n"
        f"{results_text}",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=keyboard,
    )


async def end_vote_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cfg = context.bot_data.get("config", {})
    if not is_admin(query.from_user.id, cfg):
        await query.edit_message_text("❌ Только администратор может завершать голосования.")
        return

    data = query.data
    if not data.startswith("endvote_"):
        return
    session_id = data[len("endvote_"):]

    storage: Storage = context.bot_data["storage"]
    session = storage.get_session(session_id)
    if not session:
        await query.edit_message_text("❌ Голосование не найдено.")
        return
    if not session.is_active:
        await query.edit_message_text("❌ Голосование уже завершено.")
        return

    session.is_active = False
    session.is_finished = True
    session.results_published = False
    storage.update_session(session)

    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception as e:
        logger.warning(f"Could not remove endvote buttons: {e}")

    try:
        results = compute_results(session, storage)
        results_text = format_results(session, results)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 Открыть результаты участникам", callback_data=f"pubres_{session.id}")]
        ])
        await context.bot.send_message(
            query.from_user.id,
            f"✅ Голосование *{esc(session.name)}* завершено.\n\n"
            f"Результаты видны только вам.\n"
            f"📊 Результаты:\n{results_text}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=keyboard,
        )
    except Exception as e:
        logger.error(f"Error sending endvote results: {e}")


async def remind_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cfg = context.bot_data.get("config", {})
    if not is_admin(query.from_user.id, cfg):
        await query.edit_message_text("❌ Только администратор.")
        return

    data = query.data
    if not data.startswith("remind_"):
        return
    session_id = data[len("remind_"):]

    storage: Storage = context.bot_data["storage"]
    session = storage.get_session(session_id)
    if not session:
        await query.edit_message_text("❌ Голосование не найдено.")
        return

    approved = storage.get_approved_users()
    voted_ids = {v.user_id for v in session.votes}
    not_voted = [u for u in approved if u.id not in voted_ids]
    if not not_voted:
        await query.edit_message_text(f"✅ Все проголосовали в '{esc(session.name)}'.")
        return

    sent = 0
    errors = []
    for u in not_voted:
        try:
            await send_vote_to_user(context.bot, u, session, storage, reminder=True)
            sent += 1
        except Exception as e:
            errors.append(f"@{u.username}: {e}")
            logger.warning(f"Could not remind user {u.id}: {e}")

    msg = f"🔔 Напоминание отправлено {sent} из {len(not_voted)} непроголосовавших."
    if errors:
        msg += "\n\nОшибки:\n" + "\n".join(errors)
    await query.edit_message_text(msg)


async def pubres_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cfg = context.bot_data.get("config", {})
    if not is_admin(query.from_user.id, cfg):
        await query.edit_message_text("❌ Только администратор.")
        return

    data = query.data
    if not data.startswith("pubres_"):
        return
    session_id = data[len("pubres_"):]

    storage: Storage = context.bot_data["storage"]
    session = storage.get_session(session_id)
    if not session:
        await query.edit_message_text("❌ Голосование не найдено.")
        return
    if not session.is_finished:
        await query.edit_message_text("❌ Голосование ещё не завершено.")
        return

    session.results_published = True
    storage.update_session(session)

    results = compute_results(session, storage)
    results_text = format_results(session, results)

    sent = 0
    errors = []
    for v in session.votes:
        try:
            await context.bot.send_message(
                v.user_id,
                f"📊 Результаты голосования '{esc(session.name)}':\n\n{results_text}",
                parse_mode=ParseMode.MARKDOWN,
            )
            sent += 1
        except Exception as e:
            errors.append(f"User {v.user_id}: {e}")

    msg = f"✅ Результаты '{esc(session.name)}' опубликованы, отправлено {sent} участникам."
    if errors:
        msg += "\n\nОшибки:\n" + "\n".join(errors)
    try:
        await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.warning(f"Could not edit pubres message: {e}")


async def publish_results(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Использование: /publish_results <ID>")
        return

    storage: Storage = context.bot_data["storage"]
    session = storage.get_session(args[0])
    if not session:
        await update.message.reply_text("Голосование не найдено.")
        return
    if not session.is_finished:
        await update.message.reply_text("Сначала завершите голосование: /end_vote <ID>")
        return

    session.results_published = True
    storage.update_session(session)

    results = compute_results(session, storage)
    results_text = format_results(session, results)

    sent = 0
    errors = []
    for v in session.votes:
        try:
            await context.bot.send_message(
                v.user_id,
                f"📊 Результаты голосования '{esc(session.name)}':\n\n{results_text}",
                parse_mode=ParseMode.MARKDOWN,
            )
            sent += 1
        except Exception as e:
            errors.append(f"User {v.user_id}: {e}")

    msg = f"✅ Результаты '{esc(session.name)}' опубликованы, отправлено {sent} участникам."
    if errors:
        msg += "\n\nОшибки:\n" + "\n".join(errors)
    await update.message.reply_text(msg)


async def reset_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    cfg = context.bot_data.get("config", {})
    if not is_admin(user_id, cfg):
        await update.message.reply_text("❌ Только администратор.")
        return

    storage: Storage = context.bot_data["storage"]
    users_count = len(storage.get_all_users())
    sessions_count = len(storage.get_all_sessions())
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⚠️ Да, удалить всё", callback_data="reset_all_confirm")],
        [InlineKeyboardButton("❌ Отмена", callback_data="reset_all_cancel")],
    ])
    await update.message.reply_text(
        f"⚠️ Это удалит ВСЕ данные: {users_count} пользователей и {sessions_count} голосований.\n"
        f"Отмена действия невозможна. Продолжить?",
        reply_markup=keyboard,
    )


async def reset_all_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    cfg = context.bot_data.get("config", {})
    if not is_admin(user_id, cfg):
        await query.answer("❌ Только администратор", show_alert=True)
        return

    if query.data == "reset_all_confirm":
        storage: Storage = context.bot_data["storage"]
        storage.reset_all()
        context.user_data.pop("vote_state", None)
        context.user_data.pop("new_vote_name", None)
        await query.edit_message_text("✅ Все данные удалены. Бот сброшен к начальному состоянию.")
    else:
        await query.edit_message_text("❌ Отменено.")


async def remove_user_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    storage: Storage = context.bot_data["storage"]
    users = storage.get_all_users()
    if not users:
        await update.message.reply_text("Нет пользователей для удаления.")
        return

    keyboard = []
    for u in users:
        label = f"{u.nickname or u.username} (группа {u.group})"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"deluser_{u.id}")])
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="deluser_cancel")])

    await update.message.reply_text(
        "Выберите пользователя для удаления:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def remove_user_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    storage: Storage = context.bot_data["storage"]

    if data == "deluser_cancel":
        await query.edit_message_text("❌ Отменено.")
    elif data.startswith("deluser_"):
        uid = int(data.split("_")[1])
        if str(uid) in storage.data["users"]:
            del storage.data["users"][str(uid)]
            storage._save()
            await query.edit_message_text(f"✅ Пользователь удалён.")
        else:
            await query.edit_message_text("❌ Пользователь не найден.")


def build_results_csv(session: VoteSession, storage: Storage) -> bytes:
    results = compute_results(session, storage)

    users = storage.get_all_users()
    user_map = {u.id: f"{u.nickname or u.username} ({u.group})" for u in users}

    # Создаем маппинг user_id -> vote для быстрого доступа
    vote_map = {v.user_id: v for v in session.votes}

    output = io.StringIO()
    writer = csv.writer(output)

    # Sheet 1: per-item per-user scores
    header_users = ["Пункт"]
    voting_users = []
    for u in users:
        if u.id in vote_map:
            header_users.append(user_map[u.id])
            voting_users.append(u)

    writer.writerow(header_users + ["Средняя A (0.7)", "Средняя B (0.3)", "Итог"])

    for item in session.items:
        row = [item.name]
        scores_a = []
        scores_b = []

        # Итерируем в том же порядке, что и в заголовке
        for u in voting_users:
            vote = vote_map[u.id]
            rank = vote.rankings.get(item.name)
            if rank is not None:
                score = rank_to_score(rank, len(session.items))
                row.append(score)
                if u.group == "A":
                    scores_a.append(score)
                else:
                    scores_b.append(score)
            else:
                row.append("-")

        avg_a = sum(scores_a) / len(scores_a) if scores_a else 0


        if len(scores_b) >= 3:
            trimmed = sorted(scores_b)[1:-1]
            avg_b = sum(trimmed) / len(trimmed)
        elif scores_b:
            avg_b = sum(scores_b) / len(scores_b)
        else:
            avg_b = 0

        final_score = avg_a * 0.7 + avg_b * 0.3
        row.append(round(avg_a, 2))
        row.append(round(avg_b, 2))
        row.append(round(final_score, 2))
        writer.writerow(row)

    writer.writerow([])

    # Sheet 2: summary ranking
    writer.writerow(["Пункт", "Средняя группа A (0.7)", "Средняя группа B (0.3)", "Итоговый балл"])
    sorted_results = sorted(results, key=lambda r: r["final_score"])
    for r in sorted_results:
        writer.writerow([r["item"], r["avg_a"], r["avg_b"], r["final_score"]])

    return output.getvalue().encode("utf-8-sig")


async def export_results(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Использование: /export <ID>")
        return

    storage: Storage = context.bot_data["storage"]
    session = storage.get_session(args[0])
    if not session:
        await update.message.reply_text("Голосование не найдено.")
        return

    csv_bytes = build_results_csv(session, storage)
    await update.message.reply_document(
        document=io.BytesIO(csv_bytes),
        filename="results_{}.csv".format(session.id),
        caption=f"Детальные результаты: {session.name}",
    )


async def export_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    cfg = context.bot_data.get("config", {})
    if not is_admin(user_id, cfg):
        await query.answer("❌ Только для администратора", show_alert=True)
        return

    session_id = query.data.split("_", 1)[1]
    storage: Storage = context.bot_data["storage"]
    session = storage.get_session(session_id)
    if not session:
        await query.answer("❌ Голосование не найдено", show_alert=True)
        return

    csv_bytes = build_results_csv(session, storage)
    await context.bot.send_document(
        chat_id=user_id,
        document=io.BytesIO(csv_bytes),
        filename="results_{}.csv".format(session.id),
        caption=f"Детальные результаты: {session.name}",
    )


async def remove_vote_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    storage: Storage = context.bot_data["storage"]
    sessions = storage.get_all_sessions()
    if not sessions:
        await update.message.reply_text("Нет голосований.")
        return

    keyboard = []
    for s in sessions:
        label = f"{s.name} (ID: {s.id})"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"delvote_{s.id}")])
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="delvote_cancel")])

    await update.message.reply_text(
        "Выберите голосование для удаления:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def remove_vote_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    storage: Storage = context.bot_data["storage"]

    if data == "delvote_cancel":
        await query.edit_message_text("❌ Отменено.")
    elif data.startswith("delvote_"):
        sid = data.split("_", 1)[1]
        if storage.delete_session(sid):
            await query.edit_message_text("✅ Голосование удалено.")
        else:
            await query.edit_message_text("❌ Голосование не найдено.")


# ==================== USER HANDLERS ====================

async def my_votes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    storage: Storage = context.bot_data["storage"]
    user = storage.get_user(user_id)
    if not user or not user.approved:
        await update.message.reply_text("Вы не авторизованы для голосования.")
        return

    sessions = storage.get_active_sessions()
    if not sessions:
        await update.message.reply_text("Нет активных голосований.")
        return

    for s in sessions:
        # Check if user already voted
        already_voted = any(v.user_id == user_id for v in s.votes)
        status = "✅ Вы проголосовали" if already_voted else "❌ Не голосовали"
        text = (
            f"📊 *{esc(s.name)}*\n"
            f"Пункты: {esc(', '.join(i.name for i in s.items))}\n"
            f"Статус: {status}"
        )
        btn_label = "🗳 Переголосовать" if already_voted else "🗳 Голосовать"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(btn_label, callback_data=f"vote_{s.id}")]
        ])
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)


async def show_results(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    storage: Storage = context.bot_data["storage"]
    cfg = context.bot_data.get("config", {})
    is_adm = is_admin(user_id, cfg)
    user = storage.get_user(user_id) if not is_adm else User(id=user_id, username="admin", group="A", approved=True)
    if not user or not user.approved:
        await update.message.reply_text("Вы не авторизованы.")
        return

    sessions = storage.get_all_sessions()
    finished = [s for s in sessions if s.is_finished]
    if not finished:
        await update.message.reply_text("Нет завершённых голосований.")
        return

    for s in finished:
        if not is_adm and not s.results_published:
            continue
        results = compute_results(s, storage)
        results_text = format_results(s, results)
        header = f"📊 Результаты '{esc(s.name)}':\n\n"
        if is_adm:
            published = "✅ открыты для участников" if s.results_published else "❌ скрыты (для публикации: /publish\\_results " + s.id + ")"
            header += f"Статус: {published}\n\n"
        keyboard = None
        if is_adm:
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("📥 Скачать таблицу", callback_data=f"export_{s.id}")]
            ])
        await update.message.reply_text(
            header + results_text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=keyboard,
        )

    if not is_adm:
        published_any = any(s.results_published for s in finished)
        if not published_any:
            await update.message.reply_text(
                "Администратор ещё не открыл результаты ни одного голосования."
            )


async def send_vote_to_user(bot, user: User, session: VoteSession, storage: Storage, reminder: bool = False):
    if reminder:
        text = (
            f"🔔 Напоминание! Вы ещё не проголосовали: {session.name}\n\n"
            f"Пункты для ранжирования:\n"
        )
    else:
        text = (
            f"📢 Новое голосование: {session.name}\n\n"
            f"Пункты для ранжирования:\n"
        )
    for i, item in enumerate(session.items, 1):
        text += f"{i}. {item.name}\n"
    if reminder:
        text += "\nНажмите кнопку ниже, чтобы проголосовать."
    else:
        text += "\nНажмите кнопку ниже, чтобы проголосовать."

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🗳 Голосовать", callback_data=f"vote_{session.id}")]
    ])
    await bot.send_message(user.id, text, reply_markup=keyboard)


async def vote_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id

    storage: Storage = context.bot_data["storage"]
    user = storage.get_user(user_id)
    if not user or not user.approved:
        await query.edit_message_text("❌ Вы не авторизованы для голосования.")
        return

    if data.startswith("vote_"):
        session_id = data[5:]
        session = storage.get_session(session_id)
        if not session or not session.is_active:
            await query.edit_message_text("❌ Голосование недоступно.")
            return

        if user_id not in user_ranking_state:
            user_ranking_state[user_id] = {}
        items_ordered = [i.name for i in session.items]
        user_ranking_state[user_id][session_id] = list(items_ordered)

        await show_ranking(query, session, user_id)

    elif data.startswith("rup_") or data.startswith("rdn_"):
        parts = data.split("_", 3)
        direction = parts[0]
        session_id = parts[1]
        item_idx = int(parts[2])

        if user_id not in user_ranking_state or session_id not in user_ranking_state[user_id]:
            await query.edit_message_text("❌ Сессия голосования устарела. Начните заново.")
            return

        current = user_ranking_state[user_id][session_id]
        if direction == "rup" and item_idx > 0:
            current[item_idx], current[item_idx - 1] = current[item_idx - 1], current[item_idx]
        elif direction == "rdn" and item_idx < len(current) - 1:
            current[item_idx], current[item_idx + 1] = current[item_idx + 1], current[item_idx]

        session = _get_session(context, session_id)
        if session:
            await show_ranking(query, session, user_id, edit=True)

    elif data.startswith("rcon_"):
        session_id = data[5:]

        if user_id not in user_ranking_state or session_id not in user_ranking_state[user_id]:
            await query.edit_message_text("❌ Сессия голосования устарела. Начните заново.")
            return

        session = _get_session(context, session_id)
        if not session:
            return

        # Remove old vote if re-voting
        is_revote = any(v.user_id == user_id for v in session.votes)
        session.votes = [v for v in session.votes if v.user_id != user_id]

        current = user_ranking_state[user_id][session_id]
        rankings = {name: idx + 1 for idx, name in enumerate(current)}

        vote = Vote(user_id=user_id, rankings=rankings)
        session.votes.append(vote)
        storage.update_session(session)

        user_ranking_state[user_id].pop(session_id, None)
        if not user_ranking_state[user_id]:
            user_ranking_state.pop(user_id, None)

        result_lines = [f"✅ Ваш голос в '{session.name}' принят!\n"]
        for name, rank in sorted(rankings.items(), key=lambda x: x[1]):
            result_lines.append(f"  {rank}. {name}")
        await query.edit_message_text("\n".join(result_lines))

        cfg = context.bot_data.get("config", {})
        admin_id = cfg.get("admin_id")
        if admin_id:
            voter = storage.get_user(user_id)
            voter_name = (voter.nickname if voter and voter.nickname else
                          (query.from_user.username or query.from_user.first_name))
            action = "переголосовал" if is_revote else "проголосовал"
            await context.bot.send_message(
                admin_id,
                f"🗳 Пользователь {voter_name} {action} в '{session.name}'.",
            )

    elif data.startswith("rcan_"):
        session_id = data[5:]
        if user_id in user_ranking_state:
            user_ranking_state[user_id].pop(session_id, None)
            if not user_ranking_state[user_id]:
                user_ranking_state.pop(user_id, None)
        await query.edit_message_text("❌ Голосование отменено.")


def _get_session(context, session_id):
    storage: Storage = context.bot_data["storage"]
    return storage.get_session(session_id)


async def show_ranking(query, session, user_id, edit=False):
    current = user_ranking_state[user_id][session.id]
    text = f"📊 *{esc(session.name)}*\n\nРасставьте пункты по порядку — от лучшего к худшему (▲▼):\n\n"
    for i, name in enumerate(current, 1):
        text += f"  {i}. {esc(name)}\n"

    keyboard = build_ranking_keyboard(session.id, current)
    if edit:
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
    else:
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)


# ==================== RESULTS ====================

def rank_to_score(rank: int, total_items: int) -> int:
    """First place (rank=1) gets max score = total_items, last gets 1."""
    return total_items - rank + 1


def compute_results(session: VoteSession, storage: Storage) -> List[dict]:
    if not session.votes:
        return [{"item": i.name, "avg_a": 0, "avg_b": 0, "final_score": 0} for i in session.items]

    n = len(session.items)
    results = []
    for item in session.items:
        scores_a = []
        scores_b = []

        for v in session.votes:
            user = storage.get_user(v.user_id)
            if not user:
                continue
            rank = v.rankings.get(item.name)
            if rank is None:
                continue
            score = rank_to_score(rank, n)
            if user.group == "A":
                scores_a.append(score)
            else:
                scores_b.append(score)

        avg_a = sum(scores_a) / len(scores_a) if scores_a else 0

        # Group B: exclude min and max
        if len(scores_b) >= 3:
            scores_b_sorted = sorted(scores_b)
            trimmed = scores_b_sorted[1:-1]
            avg_b = sum(trimmed) / len(trimmed) if trimmed else 0
        elif scores_b:
            avg_b = sum(scores_b) / len(scores_b)
        else:
            avg_b = 0

        final_score = avg_a * 0.7 + avg_b * 0.3
        results.append({
            "item": item.name,
            "avg_a": round(avg_a, 2),
            "avg_b": round(avg_b, 2),
            "final_score": round(final_score, 2),
        })

    return results


def format_results(session: VoteSession, results: List[dict]) -> str:
    sorted_results = sorted(results, key=lambda r: r["final_score"], reverse=True)
    lines = []
    for i, r in enumerate(sorted_results, 1):
        lines.append(
            f"{i}. *{esc(r['item'])}* — {r['final_score']} "
            f"(A: {r['avg_a']}, B: {r['avg_b']})"
        )
    return "\n".join(lines)


# ==================== CANCEL ====================

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("vote_state", None)
    context.user_data.pop("new_vote_name", None)
    context.user_data.pop("reg_state", None)
    context.user_data.pop("reg_id", None)
    context.user_data.pop("reg_username", None)
    context.user_data.pop("reg_nickname", None)
    notify_awaiting.pop(update.effective_user.id, None)
    await update.message.reply_text("Отменено.", reply_markup=get_admin_keyboard())


# ==================== MAIN ====================

def run_bot(token: str, admin_id: int, proxy: str = ""):
    storage = Storage()

    builder = Application.builder().token(token)
    if proxy:
        from telegram.request import HTTPXRequest
        request = HTTPXRequest(
            connection_pool_size=8,
            connect_timeout=30.0,
            pool_timeout=30.0,
            proxy=proxy,
        )
        builder = builder.request(request).get_updates_request(request)
    else:
        builder = builder.connect_timeout(30).get_updates_connect_timeout(30)
    app = builder.build()
    app.bot_data["storage"] = storage
    app.bot_data["config"] = {"admin_id": admin_id}

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("add_user", add_user_handle))
    app.add_handler(CommandHandler("approve_users", approve_users_list))
    app.add_handler(CallbackQueryHandler(reg_group_callback, pattern="^reg_group_"))
    app.add_handler(CallbackQueryHandler(approve_callback, pattern="^(approve_|reject_)"))
    app.add_handler(CommandHandler("new_vote", new_vote_start))
    app.add_handler(MessageHandler(notify_awaiting_filter, notify_capture))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, new_vote_text))
    app.add_handler(CommandHandler("list_votes", list_votes))
    app.add_handler(CommandHandler("list_users", list_users))
    app.add_handler(CommandHandler("active", active_votes))
    app.add_handler(CommandHandler("end_vote", end_vote))
    app.add_handler(CallbackQueryHandler(end_vote_callback, pattern="^endvote_"))
    app.add_handler(CallbackQueryHandler(remind_callback, pattern="^remind_"))
    app.add_handler(CallbackQueryHandler(pubres_callback, pattern="^pubres_"))
    app.add_handler(CommandHandler("publish_results", publish_results))
    app.add_handler(CommandHandler("export", export_results))
    app.add_handler(CallbackQueryHandler(export_callback, pattern="^export_"))
    app.add_handler(CommandHandler("remove_user", remove_user_start))
    app.add_handler(CallbackQueryHandler(remove_user_callback, pattern="^deluser_"))
    app.add_handler(CommandHandler("remove_vote", remove_vote_start))
    app.add_handler(CallbackQueryHandler(remove_vote_callback, pattern="^delvote_"))
    app.add_handler(CommandHandler("notify_all", notify_all))
    app.add_handler(CommandHandler("reset_all", reset_all))
    app.add_handler(CallbackQueryHandler(reset_all_callback, pattern="^reset_all_"))
    app.add_handler(CommandHandler("results", show_results))
    app.add_handler(CommandHandler("my_votes", my_votes))
    app.add_handler(CallbackQueryHandler(vote_callback, pattern="^(vote_|rup_|rdn_|rcon_|rcan_)"))

    logger.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)
