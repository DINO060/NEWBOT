import asyncio
import logging
import time
import random
import os
from typing import List, Dict
from dataclasses import dataclass

from playwright.async_api import async_playwright, TimeoutError as PWTimeout
# Stealth is optional; newer versions may not expose stealth_async
try:
    from playwright_stealth import stealth_async as _stealth_async
    async def apply_stealth(page):
        await _stealth_async(page)
except Exception:
    async def apply_stealth(page):
        # No-op fallback if stealth is unavailable
        return

from config import config
from scribd_scraper import scrape_scribd

logger = logging.getLogger(__name__)

# User-Agent desktop constant
UA_DESKTOP = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


@dataclass
class MangaPage:
    """Représente une page de manga"""
    src: str
    page_num: int
    width: int = 0
    height: int = 0
    referer: str = ""


class MangaScraper:
    """Scraper de manga avec Playwright et anti-détection (support Scribd)."""

    def __init__(self):
        self.playwright = None
        self.browser = None
        self.page = None
        self.context = None

    async def __aenter__(self):
        await self.setup()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.cleanup()

    async def setup(self):
        """Initialiser Playwright avec configuration anti-bot et timeouts augmentés"""
        logger.info("Initialisation du scraper avec timeouts augmentés")
        self.playwright = await async_playwright().start()

        launch_args = [
            '--no-sandbox',
            '--disable-setuid-sandbox',
            '--disable-dev-shm-usage',
            '--disable-blink-features=AutomationControlled',
            '--disable-features=site-per-process',
            '--disable-gpu',
            '--disable-web-security',
            '--disable-features=IsolateOrigins,site-per-process',
        ]

        self.browser = await self.playwright.chromium.launch(
            headless=config.headless,
            args=launch_args,
        )

        self.context = await self.browser.new_context(
            viewport={'width': 1920, 'height': 2400},
            device_scale_factor=getattr(config, 'device_scale_factor', 2),
            user_agent=UA_DESKTOP,
        )

        self.page = await self.context.new_page()

        # Augmenter les timeouts par défaut d'après la config (ms)
        self.page.set_default_timeout(getattr(config, 'sel_timeout_ms', 30000))
        self.page.set_default_navigation_timeout(getattr(config, 'nav_timeout_ms', 60000))

        await apply_stealth(self.page)
        await self.setup_resource_blocking()

    async def setup_resource_blocking(self):
        """Bloquer les ressources inutiles pour accélérer le chargement"""

        async def block_resources(route):
            r = route.request
            try:
                if r.resource_type in ['font', 'stylesheet', 'media', 'websocket']:
                    await route.abort()
                    return
                url = r.url
                if any(x in url for x in [
                    'google-analytics', 'facebook', 'twitter', 'doubleclick', 'amazon-adsystem'
                ]):
                    await route.abort()
                    return
            except Exception:
                # En cas de doute, continuer
                pass
            await route.continue_()

        await self.page.route('**/*', block_resources)

    async def scrape_chapter(self, url: str) -> Dict:
        """Scraper un chapitre avec gestion robuste des timeouts (Scribd inclus)"""
        try:
            logger.info(f"Scraping: {url}")

            # Détection Scribd
            if 'scribd.com' in url.lower():
                logger.info("Site Scribd détecté - utilisation de l'adapter spécialisé")
                return await self._scrape_scribd_document(url)

            # Navigation avec timeout augmenté et fallback
            try:
                await self.page.goto(
                    url,
                    wait_until='domcontentloaded',
                    timeout=getattr(config, 'nav_timeout_ms', 60000),
                )
                logger.info("Page chargée (DOM ready)")
                try:
                    await self.page.wait_for_load_state('networkidle', timeout=15000)
                    logger.info("Network idle atteint")
                except PWTimeout:
                    logger.warning("Network idle timeout - continuation sans attendre")
            except PWTimeout:
                logger.error("TIMEOUT_NAV: échec de navigation")
                return {
                    'success': False,
                    'error': f"Le site met trop de temps à répondre (>{getattr(config, 'nav_timeout_ms', 60000)//1000}s)",
                    'timeout': True,
                }

            # Fermeture de popups
            await self._try_close_popups()

            # Si l'URL pointe vers une page série (liste de chapitres), ouvrir un chapitre
            await self._maybe_open_chapter_from_series(url)

            # Attendre des images visibles
            await self._wait_until_images_rendered()

            # Scroll intelligent avec durée max (secondes)
            await self.intelligent_scroll()

            # Re-attendre des images après scroll
            await self._force_lazyload_images()
            await self._wait_until_images_rendered(timeout_ms=max(45000, getattr(config, 'sel_timeout_ms', 30000)))

            # Extraction
            images = await self.extract_manga_images()
            logger.info(f"✅ Trouvé {len(images)} images")
            return {'success': True, 'pages': images, 'total': len(images)}

        except PWTimeout as e:
            logger.error(f"Timeout Playwright: {e}")
            return {
                'success': False,
                'error': f"⏳ Timeout après {getattr(config, 'sel_timeout_ms', 30000)//1000}s",
                'timeout': True,
            }
        except Exception as e:
            logger.error(f"Erreur scraping: {e}", exc_info=True)
            return {'success': False, 'error': f"Erreur lors du scraping: {str(e)}"}

    async def _maybe_open_chapter_from_series(self, original_url: str):
        """Si la page est une page de série (et non un chapitre), tenter d'ouvrir un chapitre lisible."""
        try:
            current_url = self.page.url
            # Heuristique simple: sur ManhuaPlus (et similaires), les chapitres sont sous /chapter/
            if "/chapter/" in current_url:
                return
            if "/manga/" in current_url:
                # Essayer des boutons de lecture courants
                candidates = [
                    "a:has-text('Read')",
                    "a:has-text('Read Now')",
                    "a:has-text('Read First')",
                    "a:has-text('Start Reading')",
                ]
                for sel in candidates:
                    try:
                        btn = self.page.locator(sel).first
                        if await btn.is_visible(timeout=1500):
                            await btn.click(timeout=3000)
                            try:
                                await self.page.wait_for_load_state('domcontentloaded', timeout=15000)
                            except PWTimeout:
                                pass
                            # Si cela nous a amené sur un chapitre, on sort
                            if "/chapter/" in self.page.url:
                                await self._try_close_popups()
                                return
                    except Exception:
                        continue

                # Sinon, prendre le premier lien de chapitre disponible
                try:
                    # Préférer un lien dans la liste des chapitres si présente
                    chapter_link = self.page.locator(".wp-manga-chapter a[href*='/chapter/']").first
                    if await chapter_link.count() == 0:
                        chapter_link = self.page.locator("a[href*='/chapter/']").first
                    if await chapter_link.count() > 0:
                        href = await chapter_link.get_attribute('href')
                        if href:
                            await self.page.goto(href, wait_until='domcontentloaded', timeout=getattr(config, 'nav_timeout_ms', 60000))
                            try:
                                await self.page.wait_for_load_state('networkidle', timeout=10000)
                            except PWTimeout:
                                pass
                            await self._try_close_popups()
                except Exception:
                    # Pas bloquant
                    pass
        except Exception:
            # Ne pas casser le flux si cette étape échoue
            return

    async def _force_lazyload_images(self):
        """Forcer le chargement des images lazy (data-src -> src, etc.)."""
        try:
            await self.page.evaluate(
                """
                () => {
                  const imgs = Array.from(document.querySelectorAll('img'));
                  imgs.forEach(img => {
                    const dataSrc = img.getAttribute('data-src') || img.getAttribute('data-original') || img.getAttribute('data-lazy-src');
                    const dataSrcSet = img.getAttribute('data-srcset');
                    if (dataSrc && !img.src) { img.src = dataSrc; }
                    if (dataSrcSet) { img.srcset = dataSrcSet; }
                    if (img.loading === 'lazy') { img.loading = 'eager'; }
                  });
                  window.scrollBy(0, 80);
                }
                """
            )
            await self.page.wait_for_timeout(800)
        except Exception:
            pass

    async def _try_close_popups(self):
        """Tenter de fermer les popups courants"""
        selectors = [
            "text=/^(Yes|I am over 18|Agree|Continue|Accept|OK|Got it)/i",
            "button:has-text('Accept')",
            "button:has-text('Continue')",
            "[class*='close']:visible",
            "[class*='dismiss']:visible",
        ]
        for selector in selectors:
            try:
                el = self.page.locator(selector).first
                if await el.is_visible(timeout=1000):
                    await el.click(timeout=2000)
                    logger.info(f"Popup fermé: {selector}")
                    await asyncio.sleep(0.5)
            except Exception:
                continue

    async def _wait_until_images_rendered(self, min_w=300, min_h=500, timeout_ms=None):
        """Attendre que des images de taille manga soient rendues"""
        if timeout_ms is None:
            timeout_ms = getattr(config, 'sel_timeout_ms', 30000)
        try:
            await self.page.wait_for_function(
                """(minW, minH) => {
                    const imgs = Array.from(document.querySelectorAll('img'));
                    return imgs.some(img => {
                        return (img.naturalWidth >= minW && img.naturalHeight >= minH) ||
                               (img.width >= minW && img.height >= minH);
                    });
                }""",
                arg=(min_w, min_h),
                timeout=timeout_ms,
            )
            logger.info("Images de manga détectées")
        except PWTimeout:
            logger.warning("Timeout en attendant les images - continuation")

    async def intelligent_scroll(self):
        """Scroll intelligent avec timeout étendu"""
        logger.info("Début du scroll intelligent")
        start_time = time.time()
        last_height = 0
        stable_count = 0
        idle_rounds = 4
        max_time_s = max(60, int(getattr(config, 'scroll_timeout', 180)))

        await self.page.wait_for_timeout(800)

        while True:
            if (time.time() - start_time) > max_time_s:
                logger.info(f"Timeout de scroll atteint ({max_time_s}s)")
                break
            current_height = await self.page.evaluate('document.body.scrollHeight')
            if current_height == last_height:
                stable_count += 1
                if stable_count >= idle_rounds:
                    logger.info("Contenu stable, fin du scroll")
                    break
            else:
                stable_count = 0
                last_height = current_height

            scroll_distance = random.randint(600, 1000)
            scroll_method = random.choice([
                f"window.scrollBy({{top: {scroll_distance}, behavior: 'smooth'}})",
                f"window.scrollBy(0, {scroll_distance})",
                "window.scrollTo(0, document.body.scrollHeight * 0.9)",
            ])
            await self.page.evaluate(scroll_method)
            await self.page.wait_for_timeout(random.randint(500, 1000))
            if random.random() < 0.2:
                await self.simulate_human_behavior()

        await self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await self.page.wait_for_timeout(1000)
        logger.info("Scroll terminé")

    async def simulate_human_behavior(self):
        x = random.randint(100, 800)
        y = random.randint(100, 600)
        await self.page.mouse.move(x, y)
        await asyncio.sleep(random.uniform(0.3, 0.8))

    async def extract_manga_images(self) -> List[MangaPage]:
        images_data = await self.page.evaluate('''
            () => {
                function getBestSrc(img){
                  if (img.srcset){
                    const sources = img.srcset.split(',').map(s=>{
                      const parts = s.trim().split(/\s+/);
                      const url = parts[0];
                      const desc = parts[1] || '';
                      let width = 0;
                      if (desc.endsWith('w')){ width = parseInt(desc); }
                      else if (desc.endsWith('x')){ width = Math.round((parseFloat(desc)||1) * (img.naturalWidth||img.width||0)); }
                      return {url, width};
                    });
                    sources.sort((a,b)=>b.width-a.width);
                    if (sources.length && sources[0].width>0) return sources[0].url;
                  }
                  return img.getAttribute('data-original') ||
                         img.getAttribute('data-src-large') ||
                         img.currentSrc || img.src ||
                         img.getAttribute('data-src') ||
                         img.getAttribute('data-lazy-src') || '';
                }
                const images = Array.from(document.querySelectorAll('img'));
                const urls = new Set();
                const results = [];
                images.forEach((img, index) => {
                    const src = getBestSrc(img);
                    if (src && !urls.has(src)) {
                        urls.add(src);
                        results.push({
                            src,
                            width: img.naturalWidth || img.width || 0,
                            height: img.naturalHeight || img.height || 0,
                            alt: img.alt || '',
                            className: img.className || '',
                            index
                        });
                    }
                });
                return results.filter(img => img.src && img.width > 0);
            }
        ''')
        manga_pages = []
        page_num = 1
        for img in images_data:
            if self.is_manga_page(img):
                manga_pages.append(MangaPage(
                    src=img['src'],
                    page_num=page_num,
                    width=img['width'],
                    height=img['height'],
                    referer=self.page.url,
                ))
                page_num += 1
        return manga_pages

    def is_manga_page(self, img_data: Dict) -> bool:
        if img_data['width'] < 400 or img_data['height'] < 500:
            return False
        ui_keywords = ['icon', 'logo', 'avatar', 'button', 'banner', 'ad', 'thumb', 'advertisement']
        text = (img_data.get('src', '') + img_data.get('alt', '') + img_data.get('className', '')).lower()
        if any(keyword in text for keyword in ui_keywords):
            return False
        if img_data['width'] > 0 and img_data['height'] > 0:
            aspect_ratio = img_data['width'] / img_data['height']
            if aspect_ratio > 2.0:
                return False
        src_lower = img_data.get('src', '').lower()
        valid_extensions = ['.jpg', '.jpeg', '.png', '.webp']
        has_valid_ext = any(ext in src_lower for ext in valid_extensions)
        return has_valid_ext or 'image' in src_lower

    async def _scrape_scribd_document(self, url: str) -> Dict:
        """Scraper un document Scribd (captures locales)."""
        try:
            from pathlib import Path
            workdir = Path(config.temp_dir) / f"scribd_{os.getpid()}"
            images, metadata = await scrape_scribd(self.page, url, str(workdir))
            if not images:
                return {'success': False, 'error': 'Aucune page capturée depuis Scribd', 'is_scribd': True}
            manga_pages: List[MangaPage] = []
            for idx, img_path in enumerate(images, start=1):
                manga_pages.append(MangaPage(src=str(img_path), page_num=idx))
            result = {
                'success': True,
                'pages': manga_pages,
                'total': len(manga_pages),
                'is_scribd': True,
                'scribd_metadata': metadata,
            }
            total = metadata.get('total_pages') if isinstance(metadata, dict) else None
            if total and len(images) < total:
                result['warning'] = f"⚠️ {total - len(images)} pages n'ont pas pu être capturées (preview ou lazy-load)"
            return result
        except Exception as e:
            logger.error(f"Erreur Scribd: {e}", exc_info=True)
            return {'success': False, 'error': f"Erreur Scribd: {str(e)}", 'is_scribd': True}

    async def cleanup(self):
        try:
            if self.page:
                await self.page.close()
            if self.context:
                await self.context.close()
            if self.browser:
                await self.browser.close()
            if self.playwright:
                await self.playwright.stop()
        except Exception as e:
            logger.error(f"Erreur cleanup: {e}")


