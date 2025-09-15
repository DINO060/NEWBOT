import asyncio
import re
import os
import logging
from pathlib import Path
from typing import List, Tuple, Optional, Dict
from playwright.async_api import Page, TimeoutError as PWTimeout

logger = logging.getLogger(__name__)


class ScribdScraper:
	"""Scraper spécialisé pour documents Scribd"""

	@staticmethod
	async def close_popups(page: Page):
		popup_texts = ["Yes", "Agree", "Continue", "Accept", "OK", "Got it", "I am over 18", "Allow all", "I understand"]
		for text in popup_texts:
			try:
				button = page.get_by_role("button", name=re.compile(text, re.I))
				if await button.is_visible(timeout=500):
					await button.click()
					await page.wait_for_timeout(300)
			except Exception:
				pass
		for selector in ["button[aria-label*=Accept]","button[aria-label*=Close]","button:has-text('Accept')","[data-testid*=accept]",".close-button","[class*=dismiss]"]:
			try:
				if await page.locator(selector).first.is_visible(timeout=500):
					await page.locator(selector).first.click()
					await page.wait_for_timeout(300)
			except Exception:
				pass

	@staticmethod
	async def detect_total_pages(page: Page) -> Optional[int]:
		detection_script = r"""
		() => {
			const pageCountEl = document.querySelector('[data-e2e="page-count"]');
			if (pageCountEl) {
				const match = pageCountEl.textContent.match(/\d+/g);
				if (match && match.length) return parseInt(match[match.length - 1], 10);
			}
			const selectors = ['.page_total', '.page_count_total', '.page_count', '.document-info', '[class*=page-count]', '[class*=total-pages]'];
			for (const sel of selectors) {
				const el = document.querySelector(sel);
				if (el) {
					const text = el.textContent || '';
					const patterns = [/of\s+(\d+)/i, /(\d+)\s*pages?/i, /total[:\s]+(\d+)/i];
					for (const pattern of patterns) {
						const m = text.match(pattern);
						if (m) return parseInt(m[1], 10);
					}
				}
			}
			const pageIds = [...document.querySelectorAll('[id^="outer_page_"], [id^="page_"], [class*="page-container"]')]
				.map(el => { const m = el.id.match(/\d+/) || el.className.match(/\d+/); return m ? parseInt(m[0], 10) : 0; })
				.filter(n => n > 0);
			if (pageIds.length > 0) return Math.max(...pageIds);
			const canvases = document.querySelectorAll('canvas[class*=page], canvas[id*=page]');
			if (canvases.length > 0) return canvases.length;
			return null;
		}
		"""
		try:
			total = await page.evaluate(detection_script)
			return total
		except Exception:
			return None

	@staticmethod
	async def scroll_to_load_all_pages(page: Page, target_pages: Optional[int] = None, max_iterations: int = 250):
		last_count = 0
		stable_count = 0
		max_stable = 8
		for _ in range(max_iterations):
			await page.evaluate("() => { const s = Math.floor(window.innerHeight * 0.85); window.scrollBy(0, s); }")
			await page.wait_for_timeout(400)
			current_count = await page.evaluate("() => document.querySelectorAll('[id^=\"outer_page_\"], [id^=\"page_\"], [class*=\"page-container\"], canvas, .page-wrapper').length")
			if target_pages and current_count >= target_pages:
				break
			if current_count == last_count:
				stable_count += 1
				if stable_count >= max_stable:
					break
			else:
				stable_count = 0
				last_count = current_count
		await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
		await page.wait_for_timeout(1000)

	@staticmethod
	async def capture_pages(page: Page, output_dir: Path) -> List[Path]:
		output_dir.mkdir(parents=True, exist_ok=True)
		page_containers = await page.query_selector_all("""
			div[id^='outer_page_'], 
			div[id^='page_'], 
			div[class*='page-container'],
			.page-wrapper
		""")
		if not page_containers:
			page_containers = await page.query_selector_all("canvas")
		if not page_containers:
			return []
		width = len(str(len(page_containers)))
		captured: List[Path] = []
		for i, container in enumerate(page_containers, start=1):
			try:
				canvas = await container.query_selector("canvas")
				target = canvas if canvas else container
				filename = f"page_{str(i).zfill(width)}.png"
				filepath = output_dir / filename
				await page.evaluate("(el) => el.scrollIntoView({block: 'center'})", target)
				await page.wait_for_timeout(200)
				await target.screenshot(path=str(filepath), type="png", animations="disabled")
				captured.append(filepath)
			except Exception:
				continue
		return captured

	@classmethod
	async def scrape_document(cls, page: Page, url: str, output_dir: Path, cookies: Optional[Dict] = None) -> Dict:
		results = {'success': False, 'pages_captured': 0, 'total_pages': None, 'is_preview': False, 'images': [], 'error': None}
		try:
			if cookies:
				await page.context.add_cookies([cookies])
			await page.goto(url, wait_until='domcontentloaded', timeout=90000)
			await page.wait_for_selector("div[id^='outer_page_'], canvas, .page-container", timeout=45000)
			await cls.close_popups(page)
			total_pages = await cls.detect_total_pages(page)
			await cls.scroll_to_load_all_pages(page, total_pages)
			images = await cls.capture_pages(page, output_dir)
			results['images'] = images
			results['pages_captured'] = len(images)
			results['total_pages'] = total_pages
			results['success'] = bool(images)
		except PWTimeout as e:
			results['error'] = f"Timeout: {str(e)}"
		except Exception as e:
			results['error'] = str(e)
		return results


async def scrape_scribd(page: Page, url: str, workdir: str = "./temp") -> Tuple[List[Path], Dict]:
	output_dir = Path(workdir) / "scribd_pages"
	scribd_cookie = None
	scribd_session = os.getenv("SCRIBD_SESSION")
	if scribd_session:
		scribd_cookie = {"name": "_scribd_session", "value": scribd_session, "domain": ".scribd.com", "path": "/", "httpOnly": True, "secure": True}
	result = await ScribdScraper.scrape_document(page, url, output_dir, cookies=scribd_cookie)
	return result['images'], result


