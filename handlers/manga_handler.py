"""
Handler pour intégrer les fonctionnalités manga/webtoon dans le bot PDF
"""
import os
import logging
import asyncio
from pathlib import Path
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.enums import ParseMode, ChatAction

from utils import is_valid_url, generate_filename

logger = logging.getLogger(__name__)

try:
	from manga_scraper import MangaScraper
	from pdf_generator import OptimizedPDFGenerator
	MANGA_AVAILABLE = True
except Exception as e:
	MANGA_AVAILABLE = False
	logger.warning(f"Manga features unavailable: {e}")


@Client.on_message(filters.command(["manga", "webtoon"]) & filters.private)
async def cmd_manga_start(client: Client, message: Message):
	"""Commande pour démarrer la conversion manga/webtoon"""
	if not MANGA_AVAILABLE:
		await message.reply_text("❌ Manga features are not installed.")
		return
	user_id = message.from_user.id
	keyboard = InlineKeyboardMarkup([
		[InlineKeyboardButton("📖 Convert Chapter", callback_data=f"manga_convert:{user_id}")],
		[InlineKeyboardButton("⚙️ Manga Settings", callback_data=f"manga_settings:{user_id}")],
		[InlineKeyboardButton("🔙 Back to PDF", callback_data=f"back_to_pdf:{user_id}")],
	])
	await message.reply_text(
		"🎨 **Manga/Webtoon to PDF Converter**\n\n"
		"Send me a manga/webtoon chapter URL and I'll convert it to PDF!\n\n"
		"Supported: Webtoons, MangaDex, Manganato, most manga sites",
		reply_markup=keyboard,
		parse_mode=ParseMode.MARKDOWN,
	)


@Client.on_message(filters.text & filters.private)
async def handle_manga_url(client: Client, message: Message):
	"""Détecte et traite les URLs manga automatiquement"""
	if not MANGA_AVAILABLE:
		return
	text = (message.text or "").strip()
	if not is_valid_url(text):
		return
	if not any(d in text.lower() for d in ['webtoon', 'mangadex', 'manganato', 'manga', 'chapter']):
		return
	# Store into main session
	from pdf import sessions, ensure_session_dict
	sess = ensure_session_dict(message.from_user.id)
	sess['manga_url'] = text
	keyboard = InlineKeyboardMarkup([
		[InlineKeyboardButton("📥 Download as PDF", callback_data=f"manga_download:{message.from_user.id}")],
		[InlineKeyboardButton("🔧 Process then Edit", callback_data=f"manga_process:{message.from_user.id}")],
		[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel:{message.from_user.id}")],
	])
	await message.reply_text(
		f"🔗 **Manga/Webtoon URL Detected!**\n\nURL: `{text[:50]}{'...' if len(text) > 50 else ''}`\n\nWhat would you like to do?",
		reply_markup=keyboard,
		parse_mode=ParseMode.MARKDOWN,
	)


async def process_manga_url(client: Client, chat_id: int, user_id: int, url: str, then_edit: bool = False):
	"""Traite une URL manga et génère le PDF"""
	from pdf import ensure_session_dict, get_user_temp_dir
	status = await client.send_message(chat_id, "🔍 Analyzing chapter...")
	try:
		# Lazy import to avoid NameError if optional deps are missing at module load
		try:
			from manga_scraper import MangaScraper
			from pdf_generator import OptimizedPDFGenerator
		except Exception as imp_err:
			logger.error(f"Manga features unavailable in process_manga_url: {imp_err}")
			await status.edit_text("❌ Manga/Scribd features are not installed.")
			return None

		await client.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
		await status.edit_text("🕷️ Extracting pages...\n⏳ This may take up to 2 minutes for slow sites")
		async with MangaScraper() as scraper:
			result = await scraper.scrape_chapter(url)
		if not result.get('success'):
			await status.edit_text(f"❌ Error: {result.get('error', 'Unknown error')}")
			return None
		pages = result.get('pages') or []
		if not pages:
			await status.edit_text("⚠️ No manga pages found. The site may be blocking bots.")
			return None
		from config import config
		if len(pages) > getattr(config, 'max_pages', 200):
			pages = pages[:config.max_pages]
			await status.edit_text(f"⚠️ Chapter too long. Processing first {config.max_pages} pages...")
			await asyncio.sleep(1)
		await status.edit_text(f"📄 Generating PDF...\n📊 {len(pages)} pages detected\n⏳ Downloading and processing...")
		filename = generate_filename(url)
		user_dir = get_user_temp_dir(user_id)
		output_path = user_dir / filename
		async with OptimizedPDFGenerator() as generator:
			pdf_result = await generator.generate_manga_pdf(pages, str(output_path))
		if not pdf_result.get('success'):
			await status.edit_text(f"❌ Error generating PDF: {pdf_result.get('error', 'Unknown')}")
			return None
		if then_edit:
			from pdf import sessions
			sess = ensure_session_dict(user_id)
			sess['file_id'] = None
			sess['file_path'] = str(output_path)
			sess['file_name'] = filename
			sess['is_manga_pdf'] = True
			await status.edit_text("✅ PDF generated! Now you can edit it.")
			from pdf import build_pdf_actions_keyboard
			keyboard = build_pdf_actions_keyboard(user_id)
			await client.send_message(
				chat_id,
				f"📄 **Manga PDF ready for editing**\nPages: {pdf_result.get('pages', '?')}\nSize: {pdf_result.get('size_mb', 0):.1f}MB\n\nWhat would you like to do?",
				reply_markup=keyboard,
				parse_mode=ParseMode.MARKDOWN,
			)
		else:
			await status.edit_text(f"📤 Sending PDF...\n📊 {pdf_result['pages']} pages\n📏 {pdf_result['size_mb']:.1f}MB")
			with open(output_path, 'rb') as pdf_file:
				await client.send_document(
					chat_id=chat_id,
					document=pdf_file,
					file_name=filename,
				)
			# Send details after the file (not as caption)
			try:
				await client.send_message(
					chat_id,
					f"✅ **Manga PDF Generated!**\n📊 Pages: {pdf_result['pages']}\n📏 Size: {pdf_result['size_mb']:.1f}MB\n🔗 {url}",
					parse_mode=ParseMode.MARKDOWN,
				)
			except Exception:
				pass
			try:
				os.remove(output_path)
			except Exception:
				pass
		await status.delete()
		return str(output_path) if then_edit else True
	except Exception as e:
		logger.error(f"Error processing manga URL: {e}", exc_info=True)
		await status.edit_text(f"❌ An error occurred: {str(e)[:200]}")
		return None


@Client.on_callback_query(filters.regex(r"^manga_"))
async def manga_callback_handler(client: Client, query):
	await query.answer()
	if not MANGA_AVAILABLE:
		await query.message.edit_text("❌ Manga features are not installed.")
		return
	data = query.data
	user_id = query.from_user.id
	from pdf import sessions
	if data.startswith("manga_convert:"):
		from pdf import ensure_session_dict
		sess = ensure_session_dict(user_id)
		sess['awaiting_manga_url'] = True
		await query.message.edit_text(
			"🔗 **Send me the manga/webtoon chapter URL**\n\nExamples:\n• `https://www.webtoons.com/.../ep-1/...`\n• `https://mangadex.org/chapter/...`\n• Any manga chapter URL\n\nSend /cancel to abort.",
			parse_mode=ParseMode.MARKDOWN,
		)
	elif data.startswith("manga_download:"):
		sess = sessions.get(user_id, {})
		url = sess.get('manga_url')
		if not url:
			await query.message.edit_text("❌ No URL found. Please send the URL again.")
			return
		await query.message.edit_text("⏳ Starting download...")
		await process_manga_url(client, query.message.chat.id, user_id, url, then_edit=False)
	elif data.startswith("manga_process:"):
		sess = sessions.get(user_id, {})
		url = sess.get('manga_url')
		if not url:
			await query.message.edit_text("❌ No URL found. Please send the URL again.")
			return
		await query.message.edit_text("⏳ Processing for editing...")
		await process_manga_url(client, query.message.chat.id, user_id, url, then_edit=True)
	elif data.startswith("manga_settings:"):
		from config import config
		keyboard = InlineKeyboardMarkup([
			[InlineKeyboardButton("📏 Max Pages", callback_data=f"manga_maxpages:{user_id}")],
			[InlineKeyboardButton("🎨 PDF Quality", callback_data=f"manga_quality:{user_id}")],
			[InlineKeyboardButton("🔙 Back", callback_data=f"manga_back:{user_id}")],
		])
		await query.message.edit_text(
			f"⚙️ **Manga Converter Settings**\n\n• Max pages: {config.max_pages}\n• PDF quality: {config.pdf_quality}%\n• Compression: {'Off' if config.no_compression else 'On'}\n\nChoose an option:",
			reply_markup=keyboard,
			parse_mode=ParseMode.MARKDOWN,
		)


