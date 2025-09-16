"""
Admin functions and Force Join management for PDF Bot
"""
import re
import logging
from typing import List
from functools import wraps

from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pyrogram.errors import UserNotParticipant, ChatAdminRequired, UsernameNotOccupied
from pyrogram.enums import ChatMemberStatus, ParseMode

from utils.database import db
from config import ADMIN_IDS

logger = logging.getLogger(__name__)

# Parse admin IDs from config
ADMIN_ID_LIST = [int(x) for x in str(ADMIN_IDS).split(',') if x.strip()] if ADMIN_IDS else []

def is_admin(user_id: int) -> bool:
    """Check if user is admin"""
    return user_id in ADMIN_ID_LIST

def admin_only(func):
    """Decorator to restrict access to admins only"""
    @wraps(func)
    async def wrapper(client, message, *args, **kwargs):
        if not is_admin(message.from_user.id):
            await message.reply_text("❌ Admins only.", quote=True)
            return
        return await func(client, message, *args, **kwargs)
    return wrapper

async def is_user_in_channel(client: Client, user_id: int) -> bool:
    """Check if user is member of all required channels"""
    # Admins bypass
    if is_admin(user_id):
        return True
    
    # Get channels from database
    channels = await db.get_forced_channels()
    if not channels:
        return True
    
    valid_statuses = [
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.OWNER,
    ]
    
    # Check membership in all channels
    for channel in channels:
        try:
            member = await client.get_chat_member(channel, user_id)
            if member.status not in valid_statuses:
                return False
        except UserNotParticipant:
            return False
        except ChatAdminRequired:
            # Bot not admin in channel, allow access
            logger.warning(f"Bot not admin in channel: @{channel}")
            continue
        except UsernameNotOccupied:
            # Channel doesn't exist
            logger.error(f"Channel not found: @{channel}")
            continue
        except Exception as e:
            logger.error(f"Error checking membership for @{channel}: {e}")
            continue
    
    return True

async def send_force_join_message(client: Client, message: Message):
    """Send force join message with channel buttons"""
    channels = await db.get_forced_channels()
    if not channels:
        return
    
    # Build channel buttons
    buttons = []
    for channel in channels:
        buttons.append([
            InlineKeyboardButton(f"📢 Join @{channel}", url=f"https://t.me/{channel}")
        ])
    
    # Add verification button
    buttons.append([
        InlineKeyboardButton("✅ I have joined", callback_data="check_joined")
    ])
    
    # Build message text
    text = (
        "🚫 **Access Denied!**\n\n"
        "To use this bot, you must first join our channel(s):\n"
    )
    for channel in channels:
        text += f"👉 @{channel}\n"
    text += (
        "\n✅ Click the button(s) above to join.\n"
        "Once done, tap **I have joined** to continue.\n\n"
        "_Thank you for your support!_ 💙"
    )
    
    await message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode=ParseMode.MARKDOWN
    )

@Client.on_callback_query(filters.regex("^check_joined$"))
async def check_joined_handler(client: Client, query: CallbackQuery):
    """Handle 'I have joined' button click"""
    user_id = query.from_user.id
    
    is_member = await is_user_in_channel(client, user_id)
    
    if is_member:
        await query.answer("✅ Thank you! You can now use the bot.", show_alert=True)
        await query.message.delete()
        
        # Show welcome message
        from link_bot.core import send_welcome_message
        await send_welcome_message(client, user_id)
    else:
        await query.answer("❌ You haven't joined the channel yet!", show_alert=True)

# Admin commands for managing forced channels

@Client.on_message(filters.command("addfsub") & filters.private)
@admin_only
async def addfsub_handler(client: Client, message: Message):
    """Add forced subscription channels"""
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.reply_text(
            "Usage:\n`/addfsub @channel1 @channel2 ...`",
            quote=True,
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    # Parse channel names
    raw = re.split(r"[,\s]+", args[1].strip())
    channels = [x for x in (s.lstrip("@").lstrip("#") for s in raw) if x]
    
    # Add to database
    new_list = await db.add_forced_channels(channels)
    
    await message.reply_text(
        "✅ Forced-sub channels updated:\n" + 
        "\n".join(f"• @{c}" for c in new_list),
        quote=True
    )

@Client.on_message(filters.command("delfsub") & filters.private)
@admin_only
async def delfsub_handler(client: Client, message: Message):
    """Remove forced subscription channels"""
    args = message.text.split(maxsplit=1)
    
    if len(args) < 2:
        # Clear all channels
        await db.set_forced_channels([])
        await message.reply_text("✅ All forced-sub channels removed.", quote=True)
        return
    
    # Parse channel names to remove
    raw = re.split(r"[,\s]+", args[1].strip())
    channels = [x for x in (s.lstrip("@").lstrip("#") for s in raw) if x]
    
    # Remove from database
    new_list = await db.remove_forced_channels(channels)
    
    if new_list:
        await message.reply_text(
            "✅ Remaining forced-sub channels:\n" + 
            "\n".join(f"• @{c}" for c in new_list),
            quote=True
        )
    else:
        await message.reply_text("✅ No forced-sub channels configured.", quote=True)

@Client.on_message(filters.command("channels") & filters.private)
@admin_only
async def channels_handler(client: Client, message: Message):
    """List all forced subscription channels"""
    channels = await db.get_forced_channels()
    
    if not channels:
        await message.reply_text("ℹ️ No forced-sub channels configured.", quote=True)
        return
    
    await message.reply_text(
        "📋 Forced-sub channels:\n" + 
        "\n".join(f"• @{c}" for c in channels),
        quote=True
    )

# Admin commands for bot management

@Client.on_message(filters.command("broadcast") & filters.private)
@admin_only
async def broadcast_handler(client: Client, message: Message):
    """Broadcast message to all users"""
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.reply_text(
            "Usage:\n`/broadcast Your message here`",
            quote=True,
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    broadcast_text = args[1]
    
    # Get all users
    users = await db.get_all_users()
    
    # Send broadcast
    success = 0
    failed = 0
    
    status = await message.reply_text(f"📢 Broadcasting to {len(users)} users...")
    
    for user_id in users:
        try:
            await client.send_message(user_id, broadcast_text)
            success += 1
        except Exception as e:
            logger.error(f"Broadcast failed for {user_id}: {e}")
            failed += 1
        
        # Update status every 10 users
        if (success + failed) % 10 == 0:
            await status.edit_text(
                f"📢 Broadcasting...\n"
                f"Progress: {success + failed}/{len(users)}\n"
                f"Success: {success}\n"
                f"Failed: {failed}"
            )
    
    await status.edit_text(
        f"✅ Broadcast complete!\n"
        f"Total: {len(users)}\n"
        f"Success: {success}\n"
        f"Failed: {failed}"
    )

@Client.on_message(filters.command("stats") & filters.private)
@admin_only
async def stats_handler(client: Client, message: Message):
    """Show bot statistics"""
    # Get stats from database
    user_count = await db.count_users()
    stats = await db.get_stats()
    
    # Format message
    text = (
        "📊 **Bot Statistics**\n\n"
        f"👥 Total Users: {user_count:,}\n"
        f"📄 Files Processed: {stats['files']:,}\n"
        f"💾 Storage Used: {format_bytes(stats['storage_bytes'])}\n"
    )
    
    await message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

@Client.on_message(filters.command("setadmin") & filters.private)
@admin_only
async def setadmin_handler(client: Client, message: Message):
    """Add a new admin"""
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.reply_text(
            "Usage:\n`/setadmin USER_ID`",
            quote=True,
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    try:
        new_admin_id = int(args[1])
    except ValueError:
        await message.reply_text("❌ Invalid user ID", quote=True)
        return
    
    if new_admin_id not in ADMIN_ID_LIST:
        ADMIN_ID_LIST.append(new_admin_id)
        await message.reply_text(f"✅ User {new_admin_id} is now an admin", quote=True)
    else:
        await message.reply_text("ℹ️ User is already an admin", quote=True)

@Client.on_message(filters.command("deladmin") & filters.private)
@admin_only
async def deladmin_handler(client: Client, message: Message):
    """Remove an admin"""
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.reply_text(
            "Usage:\n`/deladmin USER_ID`",
            quote=True,
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    try:
        admin_id = int(args[1])
    except ValueError:
        await message.reply_text("❌ Invalid user ID", quote=True)
        return
    
    if admin_id in ADMIN_ID_LIST:
        ADMIN_ID_LIST.remove(admin_id)
        await message.reply_text(f"✅ User {admin_id} is no longer an admin", quote=True)
    else:
        await message.reply_text("ℹ️ User is not an admin", quote=True)

@Client.on_message(filters.command("admins") & filters.private)
@admin_only
async def admins_handler(client: Client, message: Message):
    """List all admins"""
    if not ADMIN_ID_LIST:
        await message.reply_text("ℹ️ No admins configured", quote=True)
        return
    
    text = "👮 **Bot Admins:**\n\n"
    for admin_id in ADMIN_ID_LIST:
        try:
            user = await client.get_users(admin_id)
            text += f"• {user.mention} ({admin_id})\n"
        except Exception:
            text += f"• User {admin_id}\n"
    
    await message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

# Helper functions

def format_bytes(n: int) -> str:
    """Format bytes to human readable format"""
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024:
            return f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} PB"
