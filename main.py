import logging
import smtplib
import ssl
from email.message import EmailMessage
import asyncio
import os
import time # Import time for potential rate limiting info

from aiogram import Bot, Dispatcher, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters import Text, Command
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.types import (
    ParseMode, ReplyKeyboardRemove, User,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton
)
from aiogram.utils import executor
from aiogram.utils.exceptions import MessageToDeleteNotFound, BotBlocked, MessageCantBeDeleted, MessageNotModified, CantParseEntities
from flask import Flask
from threading import Thread

app = Flask('')

@app.route('/')
def home():
    return "Bot is alive!"

def run():
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = Thread(target=run)
    t.start()
# --- Logging ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(name)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger(__name__)

# --- CONFIGURATION ---
# !!! REPLACE WITH YOUR ACTUAL BOT TOKEN !!!
BOT_TOKEN = "8442980102:AAG6MuC0K99DF9RMTXJqE8yiH2zZP8jgH6g"
# !!! REPLACE WITH YOUR NUMERIC TELEGRAM USER ID !!!
OWNER_ID = 7680218987

# --- Premium User Management ---
PREMIUM_USERS_FILE = "premium_users.txt"
premium_users = set()

# --- Constants ---
INTER_EMAIL_DELAY_SECONDS = 20.0 # Delay between sending emails from the SAME account (in seconds)
MAX_EMAILS_PER_RUN = 200 # Max emails per account per run (adjust as needed)
MAX_SENDER_ACCOUNTS = 20 # Limit number of sender accounts a user can add per run

# --- Bot Setup ---
storage = MemoryStorage() # Simple storage suitable for Replit
bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher(bot, storage=storage)

# --- Active Task Management ---
# Dictionary to store active sending tasks and their stop events
# Key: user_id, Value: asyncio.Event
active_sending_tasks = {}

# --- Persistence Functions ---
def load_premium_users():
    """Loads premium user IDs from the file."""
    global premium_users
    premium_users = set()
    try:
        if os.path.exists(PREMIUM_USERS_FILE):
            with open(PREMIUM_USERS_FILE, 'r') as f:
                # Ensure only valid integer IDs are added
                loaded_ids = {int(line.strip()) for line in f if line.strip().isdigit()}
                premium_users.update(loaded_ids)
            log.info(f"Loaded {len(premium_users)} premium users from {PREMIUM_USERS_FILE}.")
        else:
            log.info(f"{PREMIUM_USERS_FILE} not found. Starting with empty premium list.")
            # Create the file if it doesn't exist to avoid errors on first save
            with open(PREMIUM_USERS_FILE, 'w') as f:
                pass
            log.info(f"Created empty {PREMIUM_USERS_FILE}.")
    except ValueError as e:
        log.error(f"Error converting user ID to int in {PREMIUM_USERS_FILE}: {e}. Check file content.")
    except Exception as e:
        log.error(f"Error loading premium users from {PREMIUM_USERS_FILE}: {e}")

def save_premium_users():
    """Saves the current set of premium user IDs to the file."""
    global premium_users
    try:
        with open(PREMIUM_USERS_FILE, 'w') as f:
            for user_id in sorted(list(premium_users)):
                f.write(f"{user_id}\n")
        log.info(f"Saved {len(premium_users)} premium users to {PREMIUM_USERS_FILE}.")
    except Exception as e:
        log.error(f"Error saving premium users to {PREMIUM_USERS_FILE}: {e}")

# --- Define States for FSM ---
class ReportForm(StatesGroup):
    # Sender Account Gathering
    waiting_for_email = State()         # Step 1a: Get sender email
    waiting_for_password = State()      # Step 1b: Get sender password
    ask_more_accounts = State()         # Step 1c: Ask if user wants to add more senders

    # SMTP and Target Details
    waiting_for_smtp_server = State()   # Step 2
    waiting_for_smtp_port = State()     # Step 3
    waiting_for_target_email = State()  # Step 4

    # Email Content and Count
    waiting_for_subject = State()       # Step 5
    waiting_for_body = State()          # Step 6
    waiting_for_count = State()         # Step 7

    # Final Confirmation
    waiting_for_confirmation = State()  # Waiting for final confirmation button

# --- Helper Functions ---
def is_allowed_user(user: User) -> bool:
    """Checks if the user is the owner or a premium user."""
    return user.id == OWNER_ID or user.id in premium_users

async def delete_message_safely(message: types.Message):
    """Attempts to delete a message, ignoring common safe errors."""
    if not message: return # Safety check
    try:
        await message.delete()
    except (MessageToDeleteNotFound, BotBlocked, MessageCantBeDeleted) as e:
        # These errors are common and usually safe to ignore silently
        log.debug(f"Minor error deleting message {message.message_id} in chat {message.chat.id}: {e}")
    except Exception as e:
        # Log other unexpected errors during deletion
        log.warning(f"Unexpected error deleting message {message.message_id} in chat {message.chat.id}: {e}")

async def safe_edit_text(message: types.Message, text: str, reply_markup=None, **kwargs):
    """Attempts to edit message text, catching common errors."""
    try:
        await message.edit_text(text, reply_markup=reply_markup, **kwargs)
        return True
    except MessageNotModified:
        log.debug(f"Message {message.message_id} was not modified.")
        return True # Still considered success in terms of state
    except (MessageCantBeDeleted, MessageToDeleteNotFound): # Sometimes editing raises these too
        log.warning(f"Could not edit message {message.message_id} (might be deleted or inaccessible).")
        return False
    except CantParseEntities as e:
         log.error(f"Failed to edit message {message.message_id} due to parsing error: {e}. Text: {text[:200]}...")
         # Try sending plain text version
         try:
             await message.edit_text(text, reply_markup=reply_markup, parse_mode=None) # Disable parse mode
             log.info("Successfully edited message with plain text after parse error.")
             return True
         except Exception as e_plain:
             log.error(f"Failed to edit message {message.message_id} even with plain text: {e_plain}")
             return False
    except Exception as e:
        log.error(f"Unexpected error editing message {message.message_id}: {e}")
        return False


# --- Email Sending Function (Handles Multiple Senders, Delay, and Stopping) ---
async def send_emails_async(user_data: dict, user_id: int, status_message: types.Message, stop_event: asyncio.Event) -> tuple[bool, str]:
    """
    Sends emails using multiple sender accounts with delays, updating a status message,
    and allowing the process to be stopped via an event.

    Args:
        user_data: Dictionary containing FSM state data.
        user_id: The Telegram ID of the user initiating the request.
        status_message: The message to edit with progress updates.
        stop_event: An asyncio.Event used to signal stopping the task.

    Returns:
        A tuple (overall_success: bool, final_result_message: str)
    """
    sender_accounts = user_data.get('sender_accounts', [])
    smtp_server = user_data.get('smtp_server')
    smtp_port = user_data.get('smtp_port')
    target_email = user_data.get('target_email')
    subject = user_data.get('subject')
    body = user_data.get('body')
    count = user_data.get('count')

    # --- Initial Data Validation ---
    if not all([sender_accounts, smtp_server, smtp_port, target_email, subject, body, count]):
        log.error(f"User {user_id}: Missing data for sending email: {user_data}")
        return False, "❌ Internal error: Missing required data. Please start over using /report."

    try:
        port = int(smtp_port)
        count_int = int(count)
        if not (1 <= port <= 65535): raise ValueError("Invalid port range")
        if not (1 <= count_int <= MAX_EMAILS_PER_RUN) : raise ValueError(f"Count must be between 1 and {MAX_EMAILS_PER_RUN}")
    except ValueError as e:
        log.error(f"User {user_id}: Invalid port or count. Port='{smtp_port}', Count='{count}'. Error: {e}")
        return False, f"❌ Invalid input: {e}. Please check port (1-65535) and count (1-{MAX_EMAILS_PER_RUN})."

    total_senders = len(sender_accounts)
    log.info(f"User {user_id}: Starting email task. Target={target_email}, Count per Sender={count_int}, Senders={total_senders}, SMTP={smtp_server}:{port}, Delay={INTER_EMAIL_DELAY_SECONDS}s")

    overall_results_summary = []
    total_successfully_sent = 0
    total_attempted = count_int * total_senders
    context = ssl.create_default_context()
    task_stopped_early = False
    start_time_total = time.monotonic()

    # Generate the initial keyboard with the stop button
    stop_keyboard = InlineKeyboardMarkup().add(InlineKeyboardButton("Stop Sending ⏹️", callback_data="stop_sending"))

    # --- Loop Through Each Sender Account ---
    for account_index, account in enumerate(sender_accounts):
        if stop_event.is_set():
            log.info(f"User {user_id}: Stop requested before processing sender {account_index + 1}.")
            task_stopped_early = True
            break # Exit sender loop

        sender_email = account.get('email')
        sender_password = account.get('password') # Password will be used shortly
        sender_status_prefix = f"📧 Sender {account_index + 1}/{total_senders} (<code>{sender_email}</code>):"

        if not sender_email or not sender_password:
            log.warning(f"User {user_id}: Skipping invalid account data at index {account_index}")
            overall_results_summary.append(f"{sender_status_prefix} Skipped (incomplete credentials).")
            continue # Skip to the next sender account

        log.info(f"User {user_id}: Processing sender {account_index + 1}/{total_senders}: {sender_email}")

        await safe_edit_text(
            status_message,
            f"⚙️ Processing Sender {account_index + 1}/{total_senders}...\n"
            f"Email: <code>{sender_email}</code>\n"
            f"Attempting connection to {smtp_server}:{port}...",
            reply_markup=stop_keyboard
        )

        server = None
        sent_count_this_sender = 0
        errors_this_sender = []
        start_time_sender = time.monotonic()

        try:
            # --- Check for stop signal before connection attempt ---
            if stop_event.is_set():
                log.info(f"User {user_id}: Stop requested before connecting sender {account_index + 1}.")
                task_stopped_early = True
                break

            # --- Establish Connection ---
            log.debug(f"User {user_id}: Attempting connection to {smtp_server}:{port} for {sender_email}")
            if port == 465: # SSL Connection
                server = smtplib.SMTP_SSL(smtp_server, port, timeout=30, context=context)
            else: # Standard Connection + STARTTLS
                server = smtplib.SMTP(smtp_server, port, timeout=30)
                server.ehlo()
                server.starttls(context=context)
                server.ehlo()
            log.info(f"User {user_id}: Connection successful for {sender_email}.")

            # --- Check for stop signal before login ---
            if stop_event.is_set():
                log.info(f"User {user_id}: Stop requested before logging in sender {account_index + 1}.")
                task_stopped_early = True
                break

            # --- Login ---
            log.debug(f"User {user_id}: Attempting login for {sender_email}.")
            await safe_edit_text(
                status_message,
                f"⚙️ Processing Sender {account_index + 1}/{total_senders}...\n"
                f"Email: <code>{sender_email}</code>\n"
                f"Authenticating...",
                reply_markup=stop_keyboard
            )
            server.login(sender_email, sender_password)
            log.info(f"User {user_id}: Login successful for {sender_email}.")

            # --- Send Loop ---
            log.info(f"User {user_id}: Starting send loop for {sender_email}. Count: {count_int}")
            for i in range(count_int):
                 # --- Check for stop signal at the start of each send cycle ---
                if stop_event.is_set():
                    log.info(f"User {user_id}: Stop requested during send loop for {sender_email} (before email {i+1}).")
                    task_stopped_early = True
                    break # Exit inner loop

                current_email_num = i + 1
                total_progress_count = (account_index * count_int) + current_email_num
                progress_percent = (total_progress_count / total_attempted) * 100 if total_attempted > 0 else 0

                # Update status *before* the delay
                status_text = (
                     f"⏳ Sending... ({progress_percent:.1f}%)\n"
                     f"{sender_status_prefix}\n"
                     f"Preparing email {current_email_num}/{count_int} to <code>{target_email}</code>...\n"
                     f"Waiting {INTER_EMAIL_DELAY_SECONDS}s..."
                 )
                await safe_edit_text(status_message, status_text, reply_markup=stop_keyboard)

                # --- Apply Delay (check stop event during sleep if possible, asyncio.sleep handles cancellation) ---
                try:
                     await asyncio.wait_for(stop_event.wait(), timeout=INTER_EMAIL_DELAY_SECONDS)
                     # If wait() finished, it means the event was set
                     log.info(f"User {user_id}: Stop detected during delay for {sender_email} (email {current_email_num}).")
                     task_stopped_early = True
                     break # Exit inner loop
                except asyncio.TimeoutError:
                     # This is the normal path - the sleep finished without the event being set
                     pass
                except asyncio.CancelledError:
                     # If the parent task is cancelled
                     log.info(f"User {user_id}: Sending task cancelled externally during delay for {sender_email}.")
                     task_stopped_early = True
                     break


                # --- Double check stop signal *after* delay ---
                if stop_event.is_set():
                    log.info(f"User {user_id}: Stop requested after delay for {sender_email} (before sending email {current_email_num}).")
                    task_stopped_early = True
                    break # Exit inner loop

                # Update status just before sending
                status_text = (
                     f"⏳ Sending... ({progress_percent:.1f}%)\n"
                     f"{sender_status_prefix}\n"
                     f"Sending email {current_email_num}/{count_int} to <code>{target_email}</code>..."
                 )
                await safe_edit_text(status_message, status_text, reply_markup=stop_keyboard)

                # --- Send the email ---
                try:
                    msg = EmailMessage()
                    msg['Subject'] = subject
                    msg['From'] = sender_email
                    msg['To'] = target_email
                    msg.set_content(body)

                    server.send_message(msg)
                    sent_count_this_sender += 1
                    total_successfully_sent += 1
                    log.info(f"User {user_id}: [{sender_email}] Email {current_email_num}/{count_int} sent to {target_email}.")

                except smtplib.SMTPSenderRefused as e_loop:
                    error_msg = f"Sender address <code>{sender_email}</code> refused (maybe blocked or rate-limited?). Stopping for this sender. Error: {e_loop}"
                    log.error(f"User {user_id}: {error_msg}")
                    errors_this_sender.append(f"Email #{current_email_num}: Sender refused. Stopped.")
                    break # Stop sending from this account
                except Exception as e_loop:
                    error_msg = f"Failed sending email #{current_email_num}. Error: {e_loop}"
                    log.error(f"User {user_id}: [{sender_email}] Error sending email {current_email_num}: {e_loop}")
                    errors_this_sender.append(error_msg)
                    # Optional: Stop if too many errors occur for this sender
                    if len(errors_this_sender) > 5:
                         errors_this_sender.append("Too many consecutive errors, stopping for this sender.")
                         break

            log.info(f"User {user_id}: Finished send loop for {sender_email}. Sent: {sent_count_this_sender}/{count_int}.")
            if task_stopped_early: break # Exit outer loop if stopped in inner loop

        # --- Connection/Authentication Error Handling (for this sender) ---
        except smtplib.SMTPAuthenticationError:
            error_msg = ("🔑 Authentication failed. Check email/password. "
                       "<i>(Did you use an App Password if needed?)</i>")
            log.error(f"User {user_id}: Authentication failed for {sender_email} on {smtp_server}:{port}.")
            errors_this_sender.append(error_msg)
        except smtplib.SMTPConnectError as e:
            error_msg = f"🔌 Could not connect to <code>{smtp_server}:{port}</code>. Check server/port/firewall. Error: {e}"
            log.error(f"User {user_id}: {error_msg}")
            errors_this_sender.append(error_msg)
        except smtplib.SMTPServerDisconnected:
             error_msg = "🔌 Server disconnected unexpectedly."
             log.error(f"User {user_id}: Server disconnected for {sender_email} at {smtp_server}:{port}.")
             errors_this_sender.append(error_msg)
        except ConnectionRefusedError:
            error_msg = f"🔌 Connection refused by <code>{smtp_server}:{port}</code>."
            log.error(f"User {user_id}: Connection refused for {sender_email} at {smtp_server}:{port}.")
            errors_this_sender.append(error_msg)
        except TimeoutError:
            error_msg = f"⏳ Connection/operation timed out for <code>{smtp_server}:{port}</code>."
            log.error(f"User {user_id}: Timeout for {sender_email} at {smtp_server}:{port}.")
            errors_this_sender.append(error_msg)
        except ssl.SSLError as e:
            error_msg = f"🔒 SSL Error: {e}. (Common if port 465 used without SSL or port 587 without STARTTLS)."
            log.error(f"User {user_id}: SSL Error for {sender_email} at {smtp_server}:{port}. Error: {e}")
            errors_this_sender.append(error_msg)
        except smtplib.SMTPException as e:
             error_msg = f"✉️ SMTP Error: <code>{e}</code>"
             log.error(f"User {user_id}: SMTP Error for {sender_email} at {smtp_server}:{port}. Error: {e}")
             errors_this_sender.append(error_msg)
        except Exception as e:
            error_msg = f"⚙️ Unexpected error: <code>{e}</code>"
            log.exception(f"User {user_id}: An unexpected error occurred for sender {sender_email}: {e}")
            errors_this_sender.append(error_msg)
        finally:
            if server:
                try:
                    server.quit()
                    log.info(f"User {user_id}: SMTP connection closed for {sender_email}.")
                except Exception as e_quit:
                     log.warning(f"User {user_id}: Error during server.quit() for {sender_email}: {e_quit}") # Ignore errors during quit

            # --- Check stop signal again before compiling results for this sender ---
            if stop_event.is_set(): task_stopped_early = True

            # --- Compile results for this sender ---
            sender_time = time.monotonic() - start_time_sender
            result_line = f"{sender_status_prefix} "
            if task_stopped_early:
                result_line += f"⏹️ Stopped. Sent {sent_count_this_sender} emails before stopping."
                if errors_this_sender:
                    result_line += f"\n   Errors before stop:\n   - " + "\n   - ".join(errors_this_sender)
            elif sent_count_this_sender == count_int:
                result_line += f"✅ Sent all {count_int} emails. ({sender_time:.1f}s)"
            elif sent_count_this_sender > 0:
                result_line += (f"⚠️ Sent {sent_count_this_sender}/{count_int} emails. ({sender_time:.1f}s)\n"
                               f"   Errors:\n   - " + "\n   - ".join(errors_this_sender))
            else: # sent_count_this_sender == 0
                result_line += (f"❌ Failed to send any emails. ({sender_time:.1f}s)\n"
                               f"   Errors:\n   - " + "\n   - ".join(errors_this_sender))
            overall_results_summary.append(result_line)

            # If stopped, no need to process further senders
            if task_stopped_early: break


    # --- Final Summary ---
    total_time = time.monotonic() - start_time_total
    if task_stopped_early:
        final_message = "⏹️ <b>Email Sending Task Stopped by User</b> ⏹️\n\n"
    else:
        final_message = "🏁 <b>Email Sending Task Complete</b> 🏁\n\n"

    final_message += f"Target: <code>{target_email}</code>\n"
    final_message += f"Requested per Sender: {count_int}\n"
    final_message += f"Total Attempted Before Stop/Completion: {total_successfully_sent + len([e for r in overall_results_summary for e in r.splitlines() if 'Error' in e or 'Failed' in e])} (approx)\n" # Estimate attempted based on success/errors
    final_message += f"<b>Total Successfully Sent: {total_successfully_sent}</b>\n"
    final_message += f"Total Time: {total_time:.2f} seconds\n\n"
    final_message += "<b>--- Sender Results ---</b>\n"
    final_message += "\n\n".join(overall_results_summary) # Use double newline for better separation

    # Determine overall success (consider stopped tasks partially successful if > 0 sent)
    overall_success = total_successfully_sent > 0

    # Log the final outcome
    log.info(f"User {user_id}: Task finished. Stopped Early: {task_stopped_early}. Overall Success: {overall_success}. Sent: {total_successfully_sent}. Time: {total_time:.2f}s")

    return overall_success, final_message


# --- Bot Handlers ---

# /start command
@dp.message_handler(commands=['start'], state='*')
async def cmd_start(message: types.Message, state: FSMContext):
    """Handles the /start command, clears state, and shows the main menu."""
    await state.finish() # Clear any previous state
    user = message.from_user
    log.info(f"User {user.id} ({user.full_name} / @{user.username or 'no_username'}) started the bot.")

    # Stop any active sending task for this user if they restart
    if message.from_user.id in active_sending_tasks:
         stop_event = active_sending_tasks.pop(message.from_user.id)
         stop_event.set()
         log.info(f"User {message.from_user.id} used /start, stopping their active sending task.")
         await message.reply("ℹ️ Any active email sending task has been stopped.")


    # Use ReplyKeyboardMarkup for persistent buttons
    start_keyboard = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=False) # False = persistent
    start_keyboard.add(KeyboardButton("📊 Start Report"))
    start_keyboard.add(KeyboardButton("❓ Help"))
    start_keyboard.add(KeyboardButton("🚫 Cancel Task")) # Add explicit cancel button

    start_msg = f"""⚡️ Welcome {message.from_user.first_name} to 𝕸𝖆𝖎𝖑 𝕱𝖚𝖈𝖐*𝖗 ⚡️
ᴛʜᴇ ᴜʟᴛɪᴍᴀᴛᴇ ꜱᴘᴀᴍ ᴘʟᴀʏɢʀᴏᴜɴᴅ ꜰᴏʀ ꜱᴀᴠᴀɢᴇ ꜱᴇɴᴅᴇʀꜱ.
𝗙𝗢𝗥𝗚𝗘𝗧 𝗥𝗨𝗟𝗘𝗦. 𝗙𝗘𝗔𝗥 𝗡𝗢 𝗙𝗜𝗟𝗧𝗘𝗥𝗦.

━━━━━━━━━━━━━
🔥 𝘽𝙤𝙩 𝘼𝙧𝙨𝙚𝙣𝙖𝙡:
• 𝙎𝙈𝘼𝙎𝙃 𝙄𝙉𝘽𝙊𝙓𝙀𝙎 𝙬𝙞𝙩𝙝 𝙃𝙞𝙜𝙝-𝙑𝙤𝙡𝙪𝙢𝙚 𝘽𝙡𝙖𝙨𝙩𝙨
• Use 𝗠𝗨𝗟𝗧𝗜𝗣𝗟𝗘 sender accounts per run!
• Built-in 𝗗𝗘𝗟𝗔𝗬 system to manage sending rate.
• ⏹️ **STOP** sending midway if needed!
• 𝘽𝙮𝙥𝙖𝙨𝙨 𝘿𝙚𝙩𝙚𝙘𝙩𝙞𝙤𝙣 𝙡𝙞𝙠𝙚 𝙖 𝙂𝙝𝙤𝙨𝙩 (maybe 😉)
• 𝙁𝙖𝙨𝙩. 𝙁𝙚𝙖𝙧𝙡𝙚𝙨𝙨. 𝙁𝙞𝙡𝙩𝙚𝙧-𝙋𝙧𝙤𝙤𝙛 (use responsibly!).

━━━━━━━━━━━━━
⚙️ 𝙃𝙤𝙬 𝙩𝙤 𝙐𝙨𝙚 𝙏𝙝𝙚 𝘽𝙀𝘼𝙎𝙏:
📌 Press '📊 Start Report' to launch your attack.
📌 Tap '❓ Help' to learn all commands.
📌 Tap '🚫 Cancel Task' to abort setup.
📌 Use the '⏹️ Stop Sending' button during the process if needed.

━━━━━━━━━━━━━
Stay Ruthless. Stay Untouchable.
Bot by @sanam_kinggod (Modified)
"""
    await message.reply(start_msg, reply_markup=start_keyboard)

# /help command (also handles Help button)
@dp.message_handler(Text(equals="❓ Help", ignore_case=True), state='*')
@dp.message_handler(commands=['help'], state='*')
async def cmd_help(message: types.Message, state: FSMContext):
    """Displays help information and cancels any active FSM state."""
    current_state = await state.get_state()
    user_id = message.from_user.id
    is_command = message.text.startswith('/')

    # Stop any active sending task for this user if they ask for help
    reply_mk = None
    if user_id in active_sending_tasks:
         stop_event = active_sending_tasks.pop(user_id)
         stop_event.set()
         log.info(f"User {user_id} used help, stopping their active sending task.")
         await message.reply("ℹ️ Any active email sending task has been stopped.")
         # Keep main keyboard after stopping
         start_keyboard = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=False)
         start_keyboard.add(KeyboardButton("📊 Start Report"), KeyboardButton("❓ Help"), KeyboardButton("🚫 Cancel Task"))
         reply_mk = start_keyboard


    # If the user is in the middle of a setup process, cancel it first
    if current_state is not None:
        log.info(f"User {user_id} used help, cancelling state: {current_state}")
        await state.finish()
        # Send cancellation message separately if triggered by text/button
        if not is_command:
             await message.reply("ℹ️ Current setup operation cancelled by requesting help.", reply_markup=ReplyKeyboardRemove())
             # Keep the main keyboard if they pressed the "Help" button and weren't sending
             if reply_mk is None: # Only set if not already set by stopping a task
                 start_keyboard = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=False)
                 start_keyboard.add(KeyboardButton("📊 Start Report"), KeyboardButton("❓ Help"), KeyboardButton("🚫 Cancel Task"))
                 reply_mk = start_keyboard
        # If triggered by /help command or if we already set reply_mk from stopping a task
        elif reply_mk is None:
             reply_mk = ReplyKeyboardRemove() # Remove keyboard for command version if no task was stopped
    elif reply_mk is None:
        # No state, no task stopped - keep main keyboard if button used, remove if command used
        if not is_command:
            start_keyboard = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=False)
            start_keyboard.add(KeyboardButton("📊 Start Report"), KeyboardButton("❓ Help"), KeyboardButton("🚫 Cancel Task"))
            reply_mk = start_keyboard
        else:
            reply_mk = ReplyKeyboardRemove()


    help_text = (
        "╭━━━━━━━━━━━━━━━━━━━╮\n"
        "    <b>⚙️ 𝙃𝙀𝙇𝙋 & 𝘾𝙊𝙈𝙈𝘼𝙉𝘿𝙎</b>\n"
        "╰━━━━━━━━━━━━━━━━━━━╯\n\n"

        "<b>📌 USER COMMANDS:</b>\n"
        "📊 <code>/report</code> or <i>'Start Report' Button</i>\n"
        "   ┗ Begins the process to send mass emails.\n"
        f"   Requires <b>Premium Access</b>. Add up to {MAX_SENDER_ACCOUNTS} sender accounts.\n"
        f"   Sends up to {MAX_EMAILS_PER_RUN} emails per account with a {INTER_EMAIL_DELAY_SECONDS:.1f}s delay.\n\n"

        "❓ <code>/help</code> or <i>'Help' Button</i>\n"
        "   ┗ Shows this message. Cancels setup & stops active sending.\n\n"

        "🚫 <code>/cancel</code> or <i>'Cancel Task' Button</i>\n"
        "   ┗ Stops the current email setup process. Does NOT stop emails already sending (use the 'Stop Sending' button for that).\n\n"

        "⏹️ <i>'Stop Sending' Button</i>\n"
        "   ┗ Appears while emails are being sent. Click to abort the sending process."
    )

    if user_id == OWNER_ID:
        help_text += (
            "\n<b>👑 OWNER COMMANDS:</b>\n"
            "🔑 <code>/addpremium [user_id]</code>\n"
            "   ┗ Grant premium access.\n\n"
            "🔒 <code>/removepremium [user_id]</code>\n"
            "   ┗ Revoke premium access.\n\n"
            "👥 <code>/listpremium</code>\n"
            "   ┗ Show premium user IDs.\n"
        )

    help_text += "\n<b>⚠️ Disclaimer:</b> Use this bot responsibly and ethically. Spamming is often illegal and against Terms of Service.\n"
    help_text += "<b>🧠 Bot by:</b> @sanam_kinggod (Modified)"

    await message.reply(help_text, parse_mode="HTML", reply_markup=reply_mk, disable_web_page_preview=True)

# /cancel command (also handles Cancel Task button) - for SETUP cancellation
@dp.message_handler(Text(equals="🚫 Cancel Task", ignore_case=True), state='*')
@dp.message_handler(commands=['cancel'], state='*')
async def cmd_cancel_setup(message: types.Message, state: FSMContext):
    """Handles the /cancel command or 'Cancel Task' button, terminating the current FSM setup state."""
    user_id = message.from_user.id
    current_state = await state.get_state()
    is_button = message.text.startswith("🚫")

    # Check if a sending task is active for this user - /cancel or button should NOT stop sending
    if user_id in active_sending_tasks:
         await message.reply(
             "⚠️ You have an active email sending task running.\n"
             "The 'Cancel' command/button only stops the setup process.\n"
             "To stop the sending emails, please use the '⏹️ Stop Sending' button on the status message.",
             reply_markup=None # Don't change keyboard
         )
         return # Don't cancel FSM state if sending

    if current_state is None:
        log.info(f"User {user_id} tried to cancel setup, but no active FSM state.")
        await message.reply(
            "✅ You are not in the middle of setting up a task. Nothing to cancel.",
            reply_markup=ReplyKeyboardRemove() if not is_button else None # Clean up if command used
        )
         # Re-show main keyboard if they used the button and no state was active
        if is_button:
            start_keyboard = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=False)
            start_keyboard.add(KeyboardButton("📊 Start Report"), KeyboardButton("❓ Help"), KeyboardButton("🚫 Cancel Task"))
            await message.answer("Main Menu:", reply_markup=start_keyboard)
        return

    log.info(f"Cancelling FSM state {current_state} for user {user_id} via cancel command/button.")
    await state.finish()

    # Provide clear feedback and restore the main keyboard
    start_keyboard = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=False)
    start_keyboard.add(KeyboardButton("📊 Start Report"), KeyboardButton("❓ Help"), KeyboardButton("🚫 Cancel Task"))

    await message.reply(
        "🚫 <b>Setup Cancelled.</b>\n"
        "The email configuration process has been stopped. You are back at the main menu.",
        reply_markup=start_keyboard,
        parse_mode="HTML"
    )


# --- Owner Commands ---
# ... (addpremium, removepremium, listpremium handlers remain the same) ...
@dp.message_handler(Command("addpremium"), user_id=OWNER_ID, state="*")
async def cmd_add_premium(message: types.Message):
    """Owner command to grant premium access to a user."""
    args = message.get_args().split()
    if not args or not args[0].isdigit():
        await message.reply(
            "⚠️ <b>Usage:</b> <code>/addpremium <user_id></code>\n"
            "Example: <code>/addpremium 123456789</code>",
            parse_mode="HTML"
        )
        return

    try:
        user_id_to_add = int(args[0])
    except ValueError:
        await message.reply("⚠️ Invalid User ID. Please provide a numeric Telegram User ID.", parse_mode="HTML")
        return

    if user_id_to_add == OWNER_ID:
        await message.reply("👑 You are the owner, you already have full access.", parse_mode="HTML")
        return
    if user_id_to_add <= 0:
         await message.reply("⚠️ Invalid User ID (must be positive).", parse_mode="HTML")
         return

    if user_id_to_add in premium_users:
        await message.reply(f"ℹ️ User <code>{user_id_to_add}</code> already has premium access.", parse_mode="HTML")
    else:
        premium_users.add(user_id_to_add)
        save_premium_users() # Persist the change
        log.info(f"Owner {message.from_user.id} added premium for {user_id_to_add}")
        await message.reply(
            f"✅ <b>Success!</b>\nUser <code>{user_id_to_add}</code> now has premium access.",
            parse_mode="HTML"
        )
        # Try to notify the user
        try:
            await bot.send_message(
                user_id_to_add,
                "🎉 Congratulations! You have been granted <b>Premium Access</b> to the Mail Sender Bot by the owner!",
                parse_mode="HTML"
            )
            log.info(f"Successfully notified user {user_id_to_add} about premium grant.")
        except BotBlocked:
            log.warning(f"Could not notify user {user_id_to_add} (premium grant): Bot blocked by user.")
            await message.reply(f"(Note: Could not notify user {user_id_to_add} as they may have blocked the bot.)", parse_mode="HTML", disable_web_page_preview=True)
        except Exception as e:
            log.warning(f"Could not notify user {user_id_to_add} about premium grant: {e}")
            await message.reply(f"(Note: Failed to notify user {user_id_to_add} due to an error: {e})", parse_mode="HTML", disable_web_page_preview=True)

@dp.message_handler(Command("removepremium"), user_id=OWNER_ID, state="*")
async def cmd_remove_premium(message: types.Message):
    """Owner command to revoke premium access."""
    args = message.get_args().split()
    if not args or not args[0].isdigit():
        await message.reply(
            "⚠️ <b>Usage:</b> <code>/removepremium <user_id></code>\n"
            "Example: <code>/removepremium 987654321</code>",
            parse_mode="HTML"
        )
        return

    try:
        user_id_to_remove = int(args[0])
    except ValueError:
        await message.reply("⚠️ Invalid User ID. Please provide a numeric Telegram User ID.", parse_mode="HTML")
        return

    if user_id_to_remove == OWNER_ID:
        await message.reply("⛔️ Cannot remove the owner's implicit access.", parse_mode="HTML")
        return
    if user_id_to_remove <= 0:
         await message.reply("⚠️ Invalid User ID (must be positive).", parse_mode="HTML")
         return

    if user_id_to_remove in premium_users:
        premium_users.discard(user_id_to_remove) # Use discard to avoid error if not present
        save_premium_users() # Persist the change
        log.info(f"Owner {message.from_user.id} removed premium for {user_id_to_remove}")
        await message.reply(
            f"❌ <b>Premium access revoked</b> for user <code>{user_id_to_remove}</code>.",
            parse_mode="HTML"
        )
        # Try to notify the user
        try:
            await bot.send_message(
                user_id_to_remove,
                "ℹ️ Your <b>Premium Access</b> to the Mail Sender Bot has been revoked by the owner.",
                parse_mode="HTML"
            )
            log.info(f"Successfully notified user {user_id_to_remove} about premium removal.")
        except BotBlocked:
             log.warning(f"Could not notify user {user_id_to_remove} (premium removal): Bot blocked by user.")
             await message.reply(f"(Note: Could not notify user {user_id_to_remove} as they may have blocked the bot.)", parse_mode="HTML", disable_web_page_preview=True)
        except Exception as e:
            log.warning(f"Could not notify user {user_id_to_remove} about premium removal: {e}")
            await message.reply(f"(Note: Failed to notify user {user_id_to_remove} due to an error: {e})", parse_mode="HTML", disable_web_page_preview=True)
    else:
        await message.reply(f"⚠️ User <code>{user_id_to_remove}</code> does not currently have premium access.", parse_mode="HTML")

@dp.message_handler(Command("listpremium"), user_id=OWNER_ID, state="*")
async def cmd_list_premium(message: types.Message):
    """Owner command to list all premium users."""
    if not premium_users:
        await message.reply(
            "📭 Currently, no users have explicit premium access (besides you, the owner).",
            parse_mode="HTML"
        )
        return

    user_list = "\n".join([f"• <code>{uid}</code>" for uid in sorted(list(premium_users))])
    count = len(premium_users)
    await message.reply(
        f"👥 <b>Premium Users ({count}):</b>\n{user_list}",
        parse_mode="HTML",
        disable_web_page_preview=True # Avoid potential previews if IDs look like links
    )


# --- Report Command and FSM Handlers ---

# Step 0: Initiate Report (/report command or "📊 Start Report" button)
@dp.message_handler(Text(equals="📊 Start Report", ignore_case=True), state=None)
@dp.message_handler(commands=['report'], state=None)
async def cmd_report_start(message: types.Message, state: FSMContext):
    """Starts the email report configuration process."""
    user = message.from_user
    if not is_allowed_user(user):
        log.warning(f"Unauthorized /report attempt by {user.id} ({user.full_name} / @{user.username or 'no_username'})")
        start_keyboard = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=False)
        start_keyboard.add(KeyboardButton("📊 Start Report"), KeyboardButton("❓ Help"), KeyboardButton("🚫 Cancel Task"))
        await message.reply("🚫 Access Denied: This feature requires <b>Premium Access</b>. Please contact the owner if you believe this is an error.",
                            reply_markup=start_keyboard) # Show main keyboard for non-premium
        return

    # Check if this user already has a task running
    if user.id in active_sending_tasks:
        await message.reply("⚠️ You already have an email sending task in progress. Please wait for it to finish or stop it before starting a new one.")
        return

    log.info(f"User {user.id} starting /report process.")
    # Initialize the list for sender accounts in the state
    await state.update_data(sender_accounts=[])
    # Start by asking for the first email
    await ReportForm.waiting_for_email.set()
    await message.reply("🚀 Okay, let's configure the mass email report.\n\n"
                        "<b>Step 1a: Sender Account 1</b>\n"
                        "📧 Enter the <b>first</b> sender email address (e.g., <code>you@gmail.com</code>):\n\n"
                        "<i>(Use '🚫 Cancel Task' button or /cancel anytime to stop setup)</i>",
                        reply_markup=ReplyKeyboardRemove()) # Remove main keyboard during FSM

# Step 1a: Get Sender Email
# ... (handler remains the same) ...
@dp.message_handler(state=ReportForm.waiting_for_email, content_types=types.ContentType.TEXT)
async def process_email(message: types.Message, state: FSMContext):
    """Receives and validates the sender's email address."""
    email_text = message.text.strip()
    # Simple validation (can be improved with regex if needed)
    if '@' not in email_text or '.' not in email_text.split('@')[-1] or ' ' in email_text or len(email_text) < 6:
         await message.reply("⚠️ Invalid format. Please enter a valid email address (e.g., <code>name@domain.com</code>).")
         return # Remain in the same state

    # Temporarily store the current email being added
    await state.update_data(current_email=email_text)
    await ReportForm.waiting_for_password.set() # Move to password state
    await message.reply(f"📧 Email: <code>{email_text}</code>\n\n"
                        "<b>Step 1b:</b> 🔑 Enter the <b>password</b> or <b>App Password</b> for this email account.\n\n"
                        "<b>⚠️ SECURITY: This message containing your password will be deleted automatically.</b>")
    # Don't delete the user's email input, only the password input later


# Step 1b: Get Sender Password
# ... (handler remains the same) ...
@dp.message_handler(state=ReportForm.waiting_for_password, content_types=types.ContentType.TEXT)
async def process_password(message: types.Message, state: FSMContext):
    """Receives the password, stores the email/password pair, and asks about adding more accounts."""
    password_text = message.text # Don't strip passwords, they might have spaces

    if not password_text: # Basic check if password is empty
        # Delete the user's empty message
        await delete_message_safely(message)
        await message.answer("❌ Password cannot be empty. Please try entering the password again.") # Use answer to avoid replying to deleted msg
        return # Remain in the same state

    # Get the email stored temporarily and the list of accounts
    user_data = await state.get_data()
    current_email = user_data.get('current_email')
    sender_accounts = user_data.get('sender_accounts', [])

    if not current_email:
        log.error(f"User {message.from_user.id}: State error - current_email missing when processing password.")
        await state.finish()
        await message.answer("❌ An internal error occurred (missing email). Please start over with /report.", reply_markup=ReplyKeyboardRemove()) # Use answer
        await delete_message_safely(message)
        return

    # Add the new account details to the list
    sender_accounts.append({'email': current_email, 'password': password_text})
    await state.update_data(sender_accounts=sender_accounts, current_email=None) # Clear temporary email

    log.info(f"User {message.from_user.id}: Added sender account #{len(sender_accounts)}. Email: {current_email}")

    # Delete the password message **after** processing it
    await delete_message_safely(message)

    # Check if max accounts reached
    if len(sender_accounts) >= MAX_SENDER_ACCOUNTS:
        log.info(f"User {message.from_user.id}: Reached max sender accounts ({MAX_SENDER_ACCOUNTS}). Moving to SMTP setup.")
        await ReportForm.waiting_for_smtp_server.set()
        await message.answer(f"✅ Sender account #{len(sender_accounts)} added.\n" # Use answer
                            f"You have reached the maximum of {MAX_SENDER_ACCOUNTS} sender accounts for this run.\n\n"
                            "<b>Step 2: SMTP Server</b>\n"
                            "🖥️ Enter the SMTP server address (e.g., <code>smtp.gmail.com</code>, <code>smtp.office365.com</code>):")
    else:
        # Ask if they want to add more
        await ReportForm.ask_more_accounts.set()
        keyboard = InlineKeyboardMarkup(row_width=2)
        keyboard.add(
            InlineKeyboardButton("➕ Add Another Account", callback_data="add_more_account"),
            InlineKeyboardButton("✅ Done Adding Accounts", callback_data="done_adding_accounts")
        )
        await message.answer(f"✅ Sender account #{len(sender_accounts)} (<code>{current_email}</code>) added successfully.\n\n" # Use answer
                            f"Do you want to add another sender account? (Max: {MAX_SENDER_ACCOUNTS})",
                            reply_markup=keyboard)


# Step 1c: Ask More Accounts (Callback Query Handler)
# ... (handler remains the same) ...
@dp.callback_query_handler(state=ReportForm.ask_more_accounts)
async def process_ask_more_accounts(callback_query: types.CallbackQuery, state: FSMContext):
    """Handles the response to whether the user wants to add more sender accounts."""
    await callback_query.answer() # Acknowledge the button press

    # Check if user already started sending (shouldn't happen in this state, but safety check)
    if callback_query.from_user.id in active_sending_tasks:
         await callback_query.message.edit_text("⚠️ Cannot modify setup while a sending task is active.", reply_markup=None)
         return

    if callback_query.data == "add_more_account":
        # Go back to asking for email
        await ReportForm.waiting_for_email.set()
        await callback_query.message.edit_text(
            f"Okay, let's add the next sender account.\n\n"
            f"<b>Step 1a: Sender Account #{len((await state.get_data()).get('sender_accounts', [])) + 1}</b>\n"
            f"📧 Enter the next sender email address:", reply_markup=None) # Remove buttons
    elif callback_query.data == "done_adding_accounts":
        # Proceed to SMTP server details
        await ReportForm.waiting_for_smtp_server.set()
        user_data = await state.get_data()
        sender_count = len(user_data.get('sender_accounts', []))
        await callback_query.message.edit_text(
            f"👍 Great! You've added {sender_count} sender account(s).\n\n"
            "<b>Step 2: SMTP Server</b>\n"
            "🖥️ Enter the SMTP server address (e.g., <code>smtp.gmail.com</code>, <code>smtp.office365.com</code>):", reply_markup=None) # Remove buttons
    else:
        # Should not happen with the defined buttons
        log.warning(f"User {callback_query.from_user.id}: Received unexpected callback data '{callback_query.data}' in state ask_more_accounts.")
        try:
            await callback_query.message.edit_text("🤔 Unexpected response. Please choose one of the buttons.", reply_markup=callback_query.message.reply_markup) # Keep existing buttons
        except MessageNotModified:
            pass

# Step 2: Get SMTP Server
# ... (handler remains the same) ...
@dp.message_handler(state=ReportForm.waiting_for_smtp_server, content_types=types.ContentType.TEXT)
async def process_smtp_server(message: types.Message, state: FSMContext):
    """Receives and validates the SMTP server address."""
    smtp_server_text = message.text.strip().lower() # Store lowercase for consistency
    # Basic validation
    if not smtp_server_text or ' ' in smtp_server_text or '.' not in smtp_server_text or len(smtp_server_text) < 4:
        await message.reply("⚠️ Please enter a valid SMTP server address (e.g., <code>smtp.example.com</code>).")
        return
    await state.update_data(smtp_server=smtp_server_text)
    await ReportForm.waiting_for_smtp_port.set() # Use next() equivalent
    await message.reply(f"🖥️ SMTP Server: <code>{smtp_server_text}</code>\n\n"
                        "<b>Step 3: SMTP Port</b>\n"
                        "🔌 Enter the SMTP port number (e.g., <code>587</code> for TLS, <code>465</code> for SSL):")

# Step 3: Get SMTP Port
# ... (handler remains the same) ...
@dp.message_handler(state=ReportForm.waiting_for_smtp_port, content_types=types.ContentType.TEXT)
async def process_smtp_port(message: types.Message, state: FSMContext):
    """Receives and validates the SMTP port."""
    port_text = message.text.strip()
    if not port_text.isdigit():
        await message.reply("❌ Port must be a number (e.g., <code>587</code> or <code>465</code>).")
        return
    try:
        port_int = int(port_text)
        if not 1 <= port_int <= 65535:
            await message.reply("❌ Port number must be between 1 and 65535.")
            return
    except ValueError:
        await message.reply("❌ Invalid number format for port.")
        return

    await state.update_data(smtp_port=port_int)
    await ReportForm.waiting_for_target_email.set()
    await message.reply(f"🔌 SMTP Port: <code>{port_int}</code>\n\n"
                        "<b>Step 4: Target Recipient</b>\n"
                        "🎯 Enter the <b>single</b> target email address where all emails will be sent:")


# Step 4: Get Target Email
# ... (handler remains the same) ...
@dp.message_handler(state=ReportForm.waiting_for_target_email, content_types=types.ContentType.TEXT)
async def process_target_email(message: types.Message, state: FSMContext):
    """Receives and validates the target email address."""
    target_email_text = message.text.strip()
    # Simple validation
    if '@' not in target_email_text or '.' not in target_email_text.split('@')[-1] or ' ' in target_email_text or len(target_email_text) < 6:
        await message.reply("⚠️ Please enter a valid single target email address.")
        return
    await state.update_data(target_email=target_email_text)
    await ReportForm.waiting_for_subject.set()
    await message.reply(f"🎯 Target Email: <code>{target_email_text}</code>\n\n"
                        "<b>Step 5: Email Subject</b>\n"
                        "📝 Enter the subject line for the emails:")


# Step 5: Get Subject
# ... (handler remains the same) ...
@dp.message_handler(state=ReportForm.waiting_for_subject, content_types=types.ContentType.TEXT)
async def process_subject(message: types.Message, state: FSMContext):
    """Receives the email subject line."""
    subject_text = message.text.strip()
    if not subject_text:
        await message.reply("❌ Subject cannot be empty. Please enter a subject line.")
        return
    await state.update_data(subject=subject_text)
    await ReportForm.waiting_for_body.set()
    await message.reply(f"📝 Subject: <code>{subject_text}</code>\n\n"
                        "<b>Step 6: Email Body</b>\n"
                        "📋 Enter the main content (body) of the email:")


# Step 6: Get Body
# ... (handler remains the same) ...
@dp.message_handler(state=ReportForm.waiting_for_body, content_types=types.ContentType.TEXT)
async def process_body(message: types.Message, state: FSMContext):
    """Receives the email body content."""
    body_text = message.text # Allow multiline, don't strip leading/trailing whitespace aggressively unless intended
    if not body_text.strip(): # Check if it's effectively empty
        await message.reply("❌ Body cannot be empty. Please enter the email content.")
        return
    await state.update_data(body=body_text)
    await ReportForm.waiting_for_count.set()
    await message.reply("📋 Email body captured.\n\n"
                        f"<b>Step 7: Email Count</b>\n"
                        f"🔢 Enter how many emails to send <b>from each</b> sender account (1-{MAX_EMAILS_PER_RUN}):")


# Step 7: Get Count
# ... (handler remains the same, leads to confirmation) ...
@dp.message_handler(state=ReportForm.waiting_for_count, content_types=types.ContentType.TEXT)
async def process_count(message: types.Message, state: FSMContext):
    """Receives and validates the number of emails to send per sender."""
    count_text = message.text.strip()
    if not count_text.isdigit():
        await message.reply(f"❌ Please enter a valid number between 1 and {MAX_EMAILS_PER_RUN}.")
        return
    try:
        count_int = int(count_text)
        if not 1 <= count_int <= MAX_EMAILS_PER_RUN:
            await message.reply(f"❌ Count must be between 1 and {MAX_EMAILS_PER_RUN}.")
            return
    except ValueError:
        await message.reply(f"❌ Invalid number format. Enter a number between 1 and {MAX_EMAILS_PER_RUN}.")
        return

    await state.update_data(count=count_int)
    user_data = await state.get_data()

    # --- Display Confirmation ---
    sender_emails = [acc['email'] for acc in user_data.get('sender_accounts', [])]
    sender_list_str = "\n".join([f"  • <code>{email}</code>" for email in sender_emails])
    if not sender_list_str: sender_list_str = "<i>None configured!</i>"

    confirmation_text = (
        f"<b>✨ Final Confirmation ✨</b>\n\n"
        f"Please review the details before sending:\n\n"
        f"<b>Sender Accounts ({len(sender_emails)}):</b>\n{sender_list_str}\n\n"
        f"<b>SMTP Server:</b> <code>{user_data.get('smtp_server', 'N/A')}</code>\n"
        f"<b>SMTP Port:</b> <code>{user_data.get('smtp_port', 'N/A')}</code>\n\n"
        f"<b>Target Recipient:</b> <code>{user_data.get('target_email', 'N/A')}</code>\n"
        f"<b>Subject:</b> <code>{user_data.get('subject', 'N/A')}</code>\n"
        f"<b>Emails per Sender:</b> <code>{count_int}</code>\n"
        f"<b>Total Emails to Send:</b> <code>{count_int * len(sender_emails)}</code>\n"
        f"<b>Delay Between Sends:</b> {INTER_EMAIL_DELAY_SECONDS:.1f} seconds\n\n"
        f"⚠️ <b>Warning:</b> Sending large volumes of email may violate terms of service or laws. Proceed responsibly.\n\n"
        f"Ready to launch the email barrage?"
    )

    confirm_keyboard = InlineKeyboardMarkup(row_width=2)
    confirm_keyboard.add(
        InlineKeyboardButton("✅ Yes, Send Now!", callback_data="confirm_send"),
        InlineKeyboardButton("❌ No, Cancel Setup", callback_data="cancel_setup_final") # Changed callback data
    )

    await ReportForm.waiting_for_confirmation.set()
    await message.reply(confirmation_text, reply_markup=confirm_keyboard)


# Step 8: Handle Confirmation Buttons (Callback Query)
@dp.callback_query_handler(state=ReportForm.waiting_for_confirmation)
async def process_confirmation(callback_query: types.CallbackQuery, state: FSMContext):
    """Handles the final confirmation buttons (Send or Cancel Setup)."""
    user_id = callback_query.from_user.id
    await callback_query.answer() # Acknowledge button press

    if callback_query.data == "cancel_setup_final":
        log.info(f"User {user_id} cancelled the email task setup at final confirmation.")
        await state.finish()
        try:
            # Restore main keyboard after cancellation
            start_keyboard = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=False)
            start_keyboard.add(KeyboardButton("📊 Start Report"), KeyboardButton("❓ Help"), KeyboardButton("🚫 Cancel Task"))
            await callback_query.message.edit_text("❌ Setup cancelled by user.", reply_markup=None) # Remove inline buttons
            await callback_query.message.answer("Main menu:", reply_markup=start_keyboard) # Show main keyboard
        except MessageNotModified:
            pass # Ignore if message is already edited
        except Exception as e:
            log.error(f"Error editing message after cancel confirmation: {e}")
        return

    if callback_query.data == "confirm_send":
        # Double-check if task is already running for this user
        if user_id in active_sending_tasks:
            await callback_query.message.edit_text("⚠️ You already have a sending task running.", reply_markup=None)
            return

        log.info(f"User {user_id} confirmed the email task. Initiating sending process.")

        # Create the stop event and store it
        stop_event = asyncio.Event()
        active_sending_tasks[user_id] = stop_event

        # Edit the message to show "Sending..." immediately + Stop button
        status_message = None
        stop_keyboard = InlineKeyboardMarkup().add(InlineKeyboardButton("Stop Sending ⏹️", callback_data="stop_sending"))
        try:
            await callback_query.message.edit_text(
                "🚀 <b>Initiating Email Sending Process...</b>\n\n"
                "Please wait. This may take some time depending on the number of emails and delays.\n"
                "You will receive a final report here.\n"
                "<i>Press the button below to stop early.</i>",
                reply_markup=stop_keyboard # Add stop button here
            )
            status_message = callback_query.message # Store the message object for updates
        except Exception as e:
             log.error(f"Error editing message to 'Initiating...': {e}")
             # Try sending a new message if editing fails
             await callback_query.message.answer("🚀 Initiating Email Sending Process...\n(Status updates might be limited if initial message edit failed).")
             # In this case, we can't easily update progress or add stop button reliably.
             # We'll proceed, but stopping might not work via button. Task will still run.

        user_data = await state.get_data()
        await state.finish() # Finish FSM state *before* starting the long task

        # --- Start the potentially long email sending task ---
        success = False
        final_message = "An unknown error occurred during sending."
        try:
             # Pass the stop_event to the sending function
             success, final_message = await send_emails_async(user_data, user_id, status_message, stop_event)
        except Exception as e:
             log.exception(f"User {user_id}: Unhandled exception in send_emails_async wrapper: {e}")
             final_message = f"❌ An unexpected error occurred during the sending process: {e}"
             success = False # Ensure success is false on exception
        finally:
             # --- Cleanup: Remove the task from active list regardless of outcome ---
             active_sending_tasks.pop(user_id, None)
             log.info(f"User {user_id}: Sending task finished or stopped. Removed from active tasks.")

             # Send the final result
             try:
                 if status_message: # If we could edit the original message
                      # Edit final time, removing the stop button
                      await safe_edit_text(status_message, final_message, reply_markup=None, disable_web_page_preview=True)
                 else: # If editing failed earlier, send the result as a new message
                      await bot.send_message(callback_query.message.chat.id, final_message, disable_web_page_preview=True)

                 # Restore main keyboard after completion/stop
                 start_keyboard = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=False)
                 start_keyboard.add(KeyboardButton("📊 Start Report"), KeyboardButton("❓ Help"), KeyboardButton("🚫 Cancel Task"))
                 await bot.send_message(callback_query.message.chat.id, "Return to main menu:", reply_markup=start_keyboard)

             except Exception as e:
                 log.error(f"Error sending/editing final result message for user {user_id}: {e}")
                 # Try sending as a fallback if edit failed
                 try:
                      await bot.send_message(callback_query.message.chat.id, final_message, disable_web_page_preview=True)
                 except Exception as e2:
                      log.error(f"Fallback sending of final result also failed for user {user_id}: {e2}")


# --- Stop Sending Callback Handler ---
@dp.callback_query_handler(text="stop_sending", state="*") # Handle this callback regardless of FSM state
async def handle_stop_sending(callback_query: types.CallbackQuery, state: FSMContext):
    """Handles the 'Stop Sending' button press."""
    user_id = callback_query.from_user.id
    log.info(f"User {user_id} pressed the stop sending button.")

    stop_event = active_sending_tasks.get(user_id)

    if stop_event:
        if not stop_event.is_set():
             stop_event.set() # Signal the sending task to stop
             log.info(f"Stop event set for user {user_id}'s task.")
             await callback_query.answer("⏹️ Stop requested! Processing...", show_alert=False)
             # Edit the message to indicate stopping and remove the button
             try:
                 await callback_query.message.edit_text(
                     f"{callback_query.message.text}\n\n"
                     f"<b>⏹️ Stop requested by user. Finishing current step and stopping...</b>",
                     reply_markup=None # Remove the button
                 )
             except MessageNotModified: pass
             except Exception as e:
                 log.warning(f"Could not edit message to show 'Stopping...' for user {user_id}: {e}")
        else:
             log.info(f"Stop already requested for user {user_id}'s task.")
             await callback_query.answer("⏹️ Stop already in progress.", show_alert=False)
             # Optionally remove button if message edit failed previously
             try:
                 if callback_query.message.reply_markup: # Only edit if buttons exist
                      await callback_query.message.edit_reply_markup(reply_markup=None)
             except Exception: pass # Ignore errors removing markup here
    else:
        log.warning(f"User {user_id} pressed stop, but no active task found in tracker.")
        await callback_query.answer("⚠️ Task already completed or no active task found.", show_alert=True)
        # Remove the button if it's somehow still there
        try:
            if callback_query.message.reply_markup:
                 await callback_query.message.edit_reply_markup(reply_markup=None)
        except Exception: pass


# --- Catch-all for unexpected text messages during FSM ---
@dp.message_handler(state=ReportForm.all_states, content_types=types.ContentType.ANY) # More specific state catch
async def handle_unexpected_fsm_input(message: types.Message, state: FSMContext):
    """Handles unexpected input types or text when the bot expects specific FSM input."""
    current_state = await state.get_state()
    log.warning(f"User {message.from_user.id} sent unexpected input '{message.text or message.content_type}' in FSM state {current_state}")

    state_map = {
        ReportForm.waiting_for_email.state: "an email address",
        ReportForm.waiting_for_password.state: "a password",
        ReportForm.waiting_for_smtp_server.state: "an SMTP server address",
        ReportForm.waiting_for_smtp_port.state: "an SMTP port number",
        ReportForm.waiting_for_target_email.state: "a target email address",
        ReportForm.waiting_for_subject.state: "an email subject",
        ReportForm.waiting_for_body.state: "the email body text",
        ReportForm.waiting_for_count.state: "a number (how many emails)",
        ReportForm.ask_more_accounts.state: "a button click ('Add Another' or 'Done')",
        ReportForm.waiting_for_confirmation.state: "a button click ('Send' or 'Cancel')",
    }
    expected_input = state_map.get(current_state, "specific input for the current step")

    # Provide guidance based on content type
    if message.content_type != types.ContentType.TEXT:
         await message.reply(f"⚠️ Please send text input. I was expecting {expected_input}.\n"
                             f"Use '🚫 Cancel Task' button or /cancel to stop the setup.")
    else:
         await message.reply(f"⚠️ Unexpected input. I was expecting {expected_input}.\n"
                             f"Please provide the requested information, or use '🚫 Cancel Task' button or /cancel to stop the setup.")
    # Optionally delete the user's unexpected message
    # await delete_message_safely(message)


# --- Main Execution ---
if __name__ == '__main__':
    log.info("Bot starting...")
    # Load premium users from file on startup
    load_premium_users()
    log.info(f"Owner ID set to: {OWNER_ID}")
    log.info(f"Premium users loaded: {premium_users if premium_users else 'None'}")
    log.info(f"Max emails per run: {MAX_EMAILS_PER_RUN}, Max sender accounts: {MAX_SENDER_ACCOUNTS}, Delay: {INTER_EMAIL_DELAY_SECONDS}s")

    # Start polling
    log.info("Starting polling...")
    async def on_startup(dispatcher):
        log.info("Bot polling started successfully!")
        # You could add bot commands setup here if desired
        # await dispatcher.bot.set_my_commands([...])
    keep_alive()
    executor.start_polling(dp, skip_updates=True, on_startup=on_startup)
