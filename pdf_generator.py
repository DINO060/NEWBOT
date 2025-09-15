import asyncio
import logging
import os
import re
from typing import List, Dict, Optional
from pathlib import Path
from PIL import Image
import img2pdf
import aiohttp
from aiohttp import ClientTimeout
import aiofiles
import fitz  # PyMuPDF pour vérification
from config import config

logger = logging.getLogger(__name__)


class OptimizedPDFGenerator:
    """Générateur PDF optimisé avec vérification stricte des pages et support fichiers locaux."""

    def __init__(self):
        self.temp_files: List[str] = []
        self.session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self):
        timeout = ClientTimeout(
            total=getattr(config, 'http_timeout', 60),
            connect=10,
            sock_read=getattr(config, 'http_timeout', 60),
        )
        self.session = aiohttp.ClientSession(
            timeout=timeout,
            connector=aiohttp.TCPConnector(limit=10, force_close=True),
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self._cleanup()
        if self.session:
            await self.session.close()

    async def generate_manga_pdf(self, manga_pages: List, output_path: str) -> Dict:
        try:
            if not manga_pages:
                return {'success': False, 'error': 'Aucune page fournie'}
            # Local vs URL
            first_src = manga_pages[0].src if hasattr(manga_pages[0], 'src') else str(manga_pages[0])
            is_local = not str(first_src).lower().startswith(('http://', 'https://'))
            if is_local:
                image_paths = await self._process_local_images(manga_pages)
                expected_count = len(image_paths)
            else:
                if len(manga_pages) > getattr(config, 'max_pages', 200):
                    manga_pages = manga_pages[:config.max_pages]
                image_paths = await self._download_all_images_with_verification(manga_pages)
                if not image_paths:
                    return {'success': False, 'error': 'Aucune image téléchargée'}
                expected_count = len(manga_pages)
            rgb_images = await self._convert_all_to_rgb(image_paths)
            pdf_bytes = await self._create_pdf_with_verification(rgb_images, expected_count)
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            with open(output_path, 'wb') as f:
                f.write(pdf_bytes)
            final_page_count = self._verify_pdf_pages(output_path)
            file_size_mb = len(pdf_bytes) / (1024 * 1024)
            if is_local:
                return {
                    'success': True,
                    'path': output_path,
                    'size_mb': file_size_mb,
                    'pages': final_page_count,
                    'expected_pages': final_page_count,
                    'missing_pages': 0,
                    'is_scribd': True,
                }
            return {
                'success': True,
                'path': output_path,
                'size_mb': file_size_mb,
                'pages': final_page_count,
                'expected_pages': expected_count,
                'missing_pages': max(0, expected_count - final_page_count),
            }
        except Exception as e:
            logger.error(f"Erreur génération PDF: {e}", exc_info=True)
            return {'success': False, 'error': str(e)}

    async def _download_all_images_with_verification(self, manga_pages: List) -> List[str]:
        downloaded: List[str] = []
        chapter_temp_dir = os.path.join(config.temp_dir, f"chapter_{os.getpid()}_{len(manga_pages)}")
        os.makedirs(chapter_temp_dir, exist_ok=True)
        width = len(str(len(manga_pages)))
        tasks = []
        for idx, page in enumerate(manga_pages, start=1):
            padded_idx = str(idx).zfill(width)
            tasks.append(self._download_single_verified(page, padded_idx, chapter_temp_dir))
        batch_size = getattr(config, 'download_batch_size', 5)
        for i in range(0, len(tasks), batch_size):
            batch = tasks[i:i + batch_size]
            try:
                results = await asyncio.wait_for(asyncio.gather(*batch, return_exceptions=True), timeout=120)
                for result in results:
                    if isinstance(result, str) and os.path.exists(result):
                        downloaded.append(result)
                        self.temp_files.append(result)
            except asyncio.TimeoutError:
                pass
        if downloaded:
            from utils import natural_sort_key
            downloaded = sorted(downloaded, key=lambda p: natural_sort_key(Path(p)))
        return downloaded

    async def _download_single_verified(self, page, index: str, output_dir: str) -> Optional[str]:
        max_retries = 3
        for attempt in range(max_retries):
            try:
                if not self.session:
                    raise RuntimeError("Session non initialisée")
                headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                    'Accept': 'image/webp,image/apng,image/*,*/*;q=0.8',
                    'Accept-Encoding': 'gzip, deflate, br',
                    'Cache-Control': 'no-cache',
                    'Referer': getattr(page, 'referer', 'https://www.google.com/')
                }
                url = page.src if hasattr(page, 'src') else str(page)
                async with self.session.get(url, headers=headers, ssl=False) as response:
                    if response.status != 200:
                        if attempt < max_retries - 1:
                            await asyncio.sleep(2 ** attempt)
                            continue
                        return None
                    content = await response.read()
                    content_type = response.headers.get('Content-Type', '')
                    ext = '.jpg'
                    if 'png' in content_type:
                        ext = '.png'
                    elif 'webp' in content_type:
                        ext = '.webp'
                    filename = f"page_{index}{ext}"
                    filepath = os.path.join(output_dir, filename)
                    async with aiofiles.open(filepath, 'wb') as f:
                        await f.write(content)
                    if os.path.getsize(filepath) == 0:
                        os.remove(filepath)
                        raise ValueError("Fichier vide")
                    return filepath
            except Exception:
                if attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt)
        return None

    async def _convert_all_to_rgb(self, image_paths: List[str]) -> List[str]:
        rgb_images: List[str] = []
        for img_path in image_paths:
            try:
                rgb_path = await self._convert_to_rgb(img_path)
                if rgb_path:
                    rgb_images.append(rgb_path)
                    if rgb_path != img_path:
                        self.temp_files.append(rgb_path)
            except Exception:
                pass
        return rgb_images

    async def _convert_to_rgb(self, image_path: str) -> Optional[str]:
        try:
            with Image.open(image_path) as img:
                if img.mode == 'RGB':
                    return image_path
                rgb_img = img.convert('RGB')
                base_name = Path(image_path).stem
                output_dir = Path(image_path).parent
                rgb_path = output_dir / f"{base_name}_rgb.jpg"
                quality = getattr(config, 'pdf_quality', 85)
                rgb_img.save(str(rgb_path), 'JPEG', quality=quality, optimize=True)
                return str(rgb_path)
        except Exception:
            return None

    async def _create_pdf_with_verification(self, image_paths: List[str], expected_pages: int) -> bytes:
        valid_paths = [p for p in image_paths if os.path.exists(p) and os.path.getsize(p) > 0]
        if not valid_paths:
            raise ValueError("Aucune image valide pour créer le PDF")
        try:
            return img2pdf.convert(valid_paths)
        except Exception:
            from io import BytesIO
            images = []
            for path in valid_paths:
                try:
                    img = Image.open(path)
                    if img.mode != 'RGB':
                        img = img.convert('RGB')
                    images.append(img)
                except Exception:
                    pass
            if not images:
                raise ValueError("Aucune image valide pour le PDF")
            buf = BytesIO()
            images[0].save(buf, "PDF", resolution=100.0, save_all=True, append_images=images[1:])
            return buf.getvalue()

    def _verify_pdf_pages(self, pdf_path: str) -> int:
        try:
            doc = fitz.open(pdf_path)
            n = doc.page_count
            doc.close()
            return n
        except Exception:
            return -1

    async def _cleanup(self):
        for temp_file in self.temp_files:
            try:
                if os.path.exists(temp_file):
                    os.remove(temp_file)
            except Exception:
                pass
        self.temp_files.clear()

    async def _process_local_images(self, manga_pages: List) -> List[str]:
        processed: List[str] = []
        for page in manga_pages:
            try:
                src = page.src if hasattr(page, 'src') else str(page)
                if not os.path.exists(src):
                    continue
                if os.path.getsize(src) == 0:
                    continue
                processed.append(src)
            except Exception:
                pass
        return processed


