import os
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = os.path.join(os.getcwd(), ".playwright")
import re
import asyncio
import zipfile
import shutil
import logging
import uuid
from pathlib import Path

import httpx
from playwright.async_api import async_playwright

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ChatAction

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TOKEN = "8801492248:AAHJP8iEWT19ZcpBU_blm1pNYki3k3vNcK4"

CHOOSING, SINGLE_LINK, BATCH_LINKS = range(3)

BASE_DIR = Path(__file__).parent
DOWNLOADS_DIR = BASE_DIR / "downloads"
DOWNLOADS_DIR.mkdir(exist_ok=True)

SHOPEE_LINK_PATTERN = re.compile(
    r"https?://(?:[\w-]+\.)?shope[e]\.[\w.]+/[\w\-./!?=&%+@#]+",
    re.IGNORECASE,
)

SHOPEE_SHORT_PATTERN = re.compile(
    r"https?://s\.shopee\.[\w.]+/[\w]+",
    re.IGNORECASE,
)


def is_shopee_link(text: str) -> bool:
    return bool(SHOPEE_LINK_PATTERN.match(text.strip())) or bool(SHOPEE_SHORT_PATTERN.match(text.strip()))


def extract_shopee_links(text: str) -> list[str]:
    links = []
    for line in text.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        if is_shopee_link(line):
            links.append(line)
        else:
            return []
    return links


async def extract_video_url(page, url: str) -> str | None:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Referer": "https://shopee.com.br/",
        "Accept": "application/json",
        "X-Requested-With": "XMLHttpRequest",
    }
    
    target_url = url
    try:
        if "s.shopee.com.br" in url or "shp.ee" in url:
            async with httpx.AsyncClient(follow_redirects=True, timeout=10) as client:
                r = await client.get(url, headers=headers)
                target_url = str(r.url)
    except Exception:
        pass

    match = re.search(r"i\.(\d+)\.(\d+)", target_url)
    if match:
        shop_id = match.group(1)
        item_id = match.group(2)
        api_url = f"https://shopee.com.br/api/v4/item/get?itemid={item_id}&shopid={shop_id}"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(api_url, headers=headers)
                if r.status_code == 200:
                    res_data = r.json()
                    item_data = res_data.get("data", {})
                    if not item_data:
                        item_data = res_data.get("item", {})
                    
                    video_list = item_data.get("video_info_list", [])
                    if video_list and isinstance(video_list, list):
                        v_info = video_list[0]
                        if isinstance(v_info, dict):
                            v_url = v_info.get("video_url") or v_info.get("url")
                            if not v_url and "default_format" in v_info:
                                v_url = v_info["default_format"].get("url")
                            if v_url:
                                return v_url
        except Exception:
            pass

    if page is None:
        return None

    video_urls = []

    def handle_response(response):
        content_type = response.headers.get("content-type", "")
        req_url = response.url
        if ".mp4" in req_url or "video" in content_type:
            if req_url not in video_urls:
                video_urls.append(req_url)

    page.on("response", handle_response)

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(3000)

        try:
            play_btn = page.locator('[class*="video"], [class*="play"], [aria-label*="play"], [data-testid*="video"]').first
            if await play_btn.count() > 0:
                await play_btn.click(timeout=3000)
                await page.wait_for_timeout(3000)
        except Exception:
            pass

        try:
            await page.evaluate("""
                () => {
                    const videos = document.querySelectorAll('video');
                    videos.forEach(v => {
                        if (v.src) v.play();
                    });
                }
            """)
            await page.wait_for_timeout(2000)
        except Exception:
            pass

        try:
            src = await page.evaluate("""
                () => {
                    const videos = document.querySelectorAll('video');
                    for (const v of videos) {
                        if (v.src && v.src.startsWith('http')) return v.src;
                        const source = v.querySelector('source');
                        if (source && source.src && source.src.startsWith('http')) return source.src;
                    }
                    return null;
                }
            """)
            if src and src not in video_urls:
                video_urls.insert(0, src)
        except Exception:
            pass

        if not video_urls:
            try:
                await page.mouse.wheel(0, 600)
                await page.wait_for_timeout(2000)

                src = await page.evaluate("""
                    () => {
                        const videos = document.querySelectorAll('video');
                        for (const v of videos) {
                            if (v.src && v.src.startsWith('http')) return v.src;
                            const source = v.querySelector('source');
                            if (source && source.src && source.src.startsWith('http')) return source.src;
                        }
                        return null;
                    }
                """)
                if src:
                    video_urls.insert(0, src)
            except Exception:
                pass

    except Exception:
        return None

    mp4_urls = [u for u in video_urls if ".mp4" in u]
    if mp4_urls:
        return mp4_urls[0]

    return video_urls[0] if video_urls else None


async def download_video(url: str, dest_path: Path) -> bool:
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Referer": "https://shopee.com.br/",
        }
        urls_to_try = [url]
        clean_url = re.sub(r"\.[0-9]+\.[0-9]+\.mp4", ".mp4", url)
        if clean_url != url:
            urls_to_try.insert(0, clean_url)
        
        for target_url in urls_to_try:
            try:
                async with httpx.AsyncClient(follow_redirects=True, timeout=120) as client:
                    async with client.stream("GET", target_url, headers=headers) as resp:
                        if resp.status_code == 200:
                            with open(dest_path, "wb") as f:
                                async for chunk in resp.aiter_bytes(chunk_size=1024 * 64):
                                    f.write(chunk)
                            return True
            except Exception:
                pass
        return False
    except Exception:
        return False


async def process_single_link(update: Update, context: ContextTypes.DEFAULT_TYPE, link: str) -> bool:
    chat_id = update.effective_chat.id
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    status_msg = await context.bot.send_message(
        chat_id=chat_id,
        text="⏳ *Processando o link...*\n\n🔍 Acessando a página do produto...",
        parse_mode="Markdown",
    )

    session_dir = DOWNLOADS_DIR / f"session_{chat_id}_{uuid.uuid4().hex[:8]}"
    session_dir.mkdir(parents=True, exist_ok=True)

    try:
        video_url = await extract_video_url(None, link)
        if not video_url:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    args=[
                        "--no-sandbox",
                        "--disable-setuid-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-blink-features=AutomationControlled",
                    ],
                )
                ctx = await browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                    viewport={"width": 1366, "height": 768},
                    locale="pt-BR",
                )
                await ctx.add_init_script("""
                    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                """)
                page = await ctx.new_page()
                await status_msg.edit_text(
                    "⏳ *Processando o link...*\n\n🎬 Buscando o vídeo na página...",
                    parse_mode="Markdown",
                )
                video_url = await extract_video_url(page, link)
                await browser.close()

        if not video_url:
            await status_msg.edit_text(
                "❌ *Não foi possível encontrar o vídeo neste link.*\n\n"
                "Possíveis motivos:\n"
                "• O produto não possui vídeo\n"
                "• O link está incorreto ou expirado\n"
                "• A Shopee bloqueou o acesso temporariamente\n\n"
                "Tente novamente com /start",
                parse_mode="Markdown",
            )
            return False

        await status_msg.edit_text(
            "⏳ *Processando o link...*\n\n⬇️ Baixando o vídeo...",
            parse_mode="Markdown",
        )

        video_path = session_dir / "video.mp4"
        success = await download_video(video_url, video_path)

        if not success or not video_path.exists() or video_path.stat().st_size == 0:
            await status_msg.edit_text(
                "❌ *Falha ao baixar o vídeo.*\n\n"
                "O servidor da Shopee pode estar bloqueando o download.\n"
                "Tente novamente mais tarde com /start",
                parse_mode="Markdown",
            )
            return False

        file_size = video_path.stat().st_size
        if file_size > 50 * 1024 * 1024:
            await status_msg.edit_text(
                "❌ *O vídeo é muito grande* (> 50MB).\n\n"
                "O Telegram não permite envio de arquivos maiores que 50MB.\n"
                "Tente outro vídeo com /start",
                parse_mode="Markdown",
            )
            return False

        await status_msg.edit_text(
            "⏳ *Processando o link...*\n\n📤 Enviando o vídeo...",
            parse_mode="Markdown",
        )

        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO)

        keyboard = [
            [InlineKeyboardButton("⬅️ Voltar ao Menu", callback_data="restart")],
        ]

        with open(video_path, "rb") as vf:
            await context.bot.send_video(
                chat_id=chat_id,
                video=vf,
                caption="✅ Vídeo baixado da Shopee com sucesso!",
                supports_streaming=True,
                reply_markup=InlineKeyboardMarkup(keyboard),
            )

        await status_msg.delete()
        return True

    except Exception as e:
        logger.error(f"Erro ao processar link: {e}")
        await status_msg.edit_text(
            f"❌ *Ocorreu um erro ao processar o vídeo.*\n\n"
            f"Detalhes: `{str(e)[:200]}`\n\n"
            f"Tente novamente com /start",
            parse_mode="Markdown",
        )
        return False
    finally:
        if session_dir.exists():
            shutil.rmtree(session_dir, ignore_errors=True)


async def process_batch_links(update: Update, context: ContextTypes.DEFAULT_TYPE, links: list[str]) -> bool:
    chat_id = update.effective_chat.id
    total = len(links)

    status_msg = await context.bot.send_message(
        chat_id=chat_id,
        text=f"📦 *Processamento em lote iniciado*\n\n"
             f"Total de links: {total}\n"
             f"Progresso: 0/{total}\n\n"
             f"⏳ Isso pode levar alguns minutos...",
        parse_mode="Markdown",
    )

    session_dir = DOWNLOADS_DIR / f"batch_{chat_id}_{uuid.uuid4().hex[:8]}"
    session_dir.mkdir(parents=True, exist_ok=True)

    downloaded_files = []
    failed_links = []

    pending_links = []

    try:
        for idx, link in enumerate(links, 1):
            progress_bar = "█" * int((idx / total) * 10) + "░" * (10 - int((idx / total) * 10))
            try:
                await status_msg.edit_text(
                    f"📦 *Processamento em lote*\n\n"
                    f"Progresso: {idx}/{total}\n"
                    f"[{progress_bar}] {int((idx / total) * 100)}%\n\n"
                    f"🔍 Buscando vídeo {idx}...",
                    parse_mode="Markdown",
                )
            except Exception:
                pass

            video_url = await extract_video_url(None, link)
            if video_url:
                video_filename = f"video_{idx:03d}.mp4"
                video_path = session_dir / video_filename
                success = await download_video(video_url, video_path)
                if success and video_path.exists() and video_path.stat().st_size > 0:
                    downloaded_files.append(video_path)
                else:
                    pending_links.append((idx, link))
            else:
                pending_links.append((idx, link))
            await asyncio.sleep(0.5)

        if pending_links:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    args=[
                        "--no-sandbox",
                        "--disable-setuid-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-blink-features=AutomationControlled",
                    ],
                )
                ctx = await browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                    viewport={"width": 1366, "height": 768},
                    locale="pt-BR",
                )
                await ctx.add_init_script("""
                    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                """)

                for idx, link in pending_links:
                    try:
                        await status_msg.edit_text(
                            f"📦 *Processamento em lote (Alternativo)*\n\n"
                            f"Buscando vídeo {idx} com Playwright...",
                            parse_mode="Markdown",
                        )
                    except Exception:
                        pass

                    page = await ctx.new_page()
                    video_url = await extract_video_url(page, link)
                    await page.close()

                    if not video_url:
                        failed_links.append(f"Link {idx}: {link[:50]}...")
                        continue

                    video_filename = f"video_{idx:03d}.mp4"
                    video_path = session_dir / video_filename

                    success = await download_video(video_url, video_path)
                    if success and video_path.exists() and video_path.stat().st_size > 0:
                        downloaded_files.append(video_path)
                    else:
                        failed_links.append(f"Link {idx}: {link[:50]}...")

                    await asyncio.sleep(1)

                await browser.close()

        if not downloaded_files:
            await status_msg.edit_text(
                "❌ *Nenhum vídeo foi baixado com sucesso.*\n\n"
                "Possíveis motivos:\n"
                "• Os produtos não possuem vídeos\n"
                "• Os links estão incorretos\n"
                "• A Shopee bloqueou o acesso\n\n"
                "Tente novamente com /start",
                parse_mode="Markdown",
            )
            return False

        await status_msg.edit_text(
            f"📦 *Processamento em lote*\n\n"
            f"✅ {len(downloaded_files)} vídeo(s) baixado(s)\n"
            f"❌ {len(failed_links)} falha(s)\n\n"
            f"📁 Criando arquivo ZIP...",
            parse_mode="Markdown",
        )

        zip_path = session_dir / "videos_shopee.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for video_file in sorted(downloaded_files):
                zf.write(video_file, video_file.name)

        zip_size = zip_path.stat().st_size
        if zip_size > 50 * 1024 * 1024:
            await status_msg.edit_text(
                f"❌ *O arquivo ZIP é muito grande* ({zip_size / (1024*1024):.1f}MB).\n\n"
                f"O Telegram permite no máximo 50MB.\n"
                f"Tente com menos vídeos usando /start",
                parse_mode="Markdown",
            )
            return False

        await status_msg.edit_text(
            f"📦 *Processamento em lote*\n\n"
            f"📤 Enviando o arquivo ZIP...",
            parse_mode="Markdown",
        )

        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_DOCUMENT)

        caption_lines = [f"✅ *{len(downloaded_files)} vídeo(s)* baixado(s) com sucesso!"]
        if failed_links:
            caption_lines.append(f"\n⚠️ *{len(failed_links)} link(s) falharam:*")
            for fl in failed_links[:5]:
                caption_lines.append(f"• {fl}")
            if len(failed_links) > 5:
                caption_lines.append(f"• ...e mais {len(failed_links) - 5}")

        keyboard = [
            [InlineKeyboardButton("⬅️ Voltar ao Menu", callback_data="restart")],
        ]

        with open(zip_path, "rb") as zf:
            await context.bot.send_document(
                chat_id=chat_id,
                document=zf,
                filename="videos_shopee.zip",
                caption="\n".join(caption_lines),
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )

        await status_msg.delete()
        return True

    except Exception as e:
        logger.error(f"Erro no processamento em lote: {e}")
        await status_msg.edit_text(
            f"❌ *Ocorreu um erro no processamento em lote.*\n\n"
            f"Detalhes: `{str(e)[:200]}`\n\n"
            f"Tente novamente com /start",
            parse_mode="Markdown",
        )
        return False
    finally:
        if session_dir.exists():
            shutil.rmtree(session_dir, ignore_errors=True)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()

    keyboard = [
        [InlineKeyboardButton("🎬 Um único vídeo", callback_data="single")],
        [InlineKeyboardButton("📦 Lista de vídeos", callback_data="batch")],
    ]

    welcome_text = (
        "🛍️ *Shopee Video Downloader Bot*\n\n"
        "Olá! Eu baixo vídeos de produtos da Shopee para você.\n\n"
        "Escolha uma opção abaixo:\n\n"
        "🎬 *Um único vídeo* — Envie um link e receba o vídeo\n"
        "📦 *Lista de vídeos* — Envie vários links e receba um ZIP"
    )

    if update.callback_query:
        await update.callback_query.answer()
        try:
            await update.callback_query.edit_message_text(
                welcome_text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown",
            )
        except Exception:
            await update.callback_query.message.reply_text(
                welcome_text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown",
            )
    else:
        await update.message.reply_text(
            welcome_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )

    return CHOOSING


async def choice_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "single":
        await query.edit_message_text(
            "🎬 *Modo: Vídeo Único*\n\n"
            "Envie o link do produto da Shopee que contém o vídeo.\n\n"
            "📎 Exemplo:\n"
            "`https://shopee.com.br/produto-exemplo-i.123.456`\n\n"
            "💡 Você pode copiar o link diretamente do app ou site da Shopee.",
            parse_mode="Markdown",
        )
        return SINGLE_LINK

    elif query.data == "batch":
        await query.edit_message_text(
            "📦 *Modo: Lista de Vídeos*\n\n"
            "Envie os links dos produtos da Shopee, *um por linha*.\n\n"
            "📎 Exemplo:\n"
            "`https://shopee.com.br/produto-1-i.123.456`\n"
            "`https://shopee.com.br/produto-2-i.789.012`\n"
            "`https://shopee.com.br/produto-3-i.345.678`\n\n"
            "⚠️ *Regras:*\n"
            "• Um link por linha\n"
            "• Apenas links da Shopee\n"
            "• Envie todos os links em *uma única mensagem*\n\n"
            "💡 Você pode enviar quantos links quiser!",
            parse_mode="Markdown",
        )
        return BATCH_LINKS

    return CHOOSING


async def handle_single_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()

    if not is_shopee_link(text):
        await update.message.reply_text(
            "⚠️ *Link inválido!*\n\n"
            "Por favor, envie um link válido da Shopee.\n\n"
            "📎 Exemplo:\n"
            "`https://shopee.com.br/produto-exemplo-i.123.456`\n\n"
            "Ou envie /cancelar para voltar ao menu.",
            parse_mode="Markdown",
        )
        return SINGLE_LINK

    await process_single_link(update, context, text)
    return ConversationHandler.END


async def handle_batch_links(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    lines = text.split("\n")

    if len(lines) == 1 and not is_shopee_link(lines[0].strip()):
        await update.message.reply_text(
            "⚠️ *Formato inválido!*\n\n"
            "Envie os links *um por linha*, em uma única mensagem.\n\n"
            "📎 Exemplo correto:\n"
            "`https://shopee.com.br/produto-1-i.123.456`\n"
            "`https://shopee.com.br/produto-2-i.789.012`\n\n"
            "❌ Não envie links separados por espaço ou vírgula.\n\n"
            "Ou envie /cancelar para voltar ao menu.",
            parse_mode="Markdown",
        )
        return BATCH_LINKS

    links = extract_shopee_links(text)

    if not links:
        invalid_lines = []
        for i, line in enumerate(lines, 1):
            line = line.strip()
            if line and not is_shopee_link(line):
                invalid_lines.append(f"Linha {i}: `{line[:60]}`")

        error_text = (
            "⚠️ *Alguns links são inválidos!*\n\n"
            "Linhas com problema:\n"
        )
        for il in invalid_lines[:5]:
            error_text += f"• {il}\n"

        error_text += (
            "\n📎 Todos os links devem ser da Shopee, um por linha.\n"
            "Corrija e envie novamente, ou /cancelar para voltar."
        )

        await update.message.reply_text(error_text, parse_mode="Markdown")
        return BATCH_LINKS

    await update.message.reply_text(
        f"✅ *{len(links)} link(s) detectado(s)!*\n\n"
        f"Iniciando o download...",
        parse_mode="Markdown",
    )

    await process_batch_links(update, context, links)
    return ConversationHandler.END


async def restart_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await start(update, context)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()

    keyboard = [
        [InlineKeyboardButton("🔄 Recomeçar", callback_data="restart")],
    ]

    await update.message.reply_text(
        "❌ Operação cancelada.\n\n"
        "Use /start para recomeçar quando quiser.",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return ConversationHandler.END


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    help_text = (
        "📖 *Como usar o Shopee Video Downloader:*\n\n"
        "1️⃣ Envie /start para iniciar\n"
        "2️⃣ Escolha entre baixar um vídeo ou uma lista\n"
        "3️⃣ Envie o(s) link(s) do(s) produto(s) da Shopee\n"
        "4️⃣ Aguarde o bot processar e enviar o vídeo\n\n"
        "*Comandos:*\n"
        "/start - Iniciar o bot\n"
        "/ajuda - Mostrar esta mensagem\n"
        "/cancelar - Cancelar a operação atual\n\n"
        "*Dicas:*\n"
        "• Certifique-se de que o produto tem vídeo na página\n"
        "• Links curtos da Shopee também funcionam\n"
        "• No modo lista, envie um link por linha\n"
        "• O limite do Telegram é 50MB por arquivo"
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")


async def unknown_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🤔 Não entendi! Use /start para iniciar o bot.",
    )


async def post_init(application: Application) -> None:
    await application.bot.set_my_commands([
        BotCommand("start", "Iniciar o bot"),
        BotCommand("ajuda", "Como usar o bot"),
        BotCommand("cancelar", "Cancelar operação atual"),
    ])
    logger.info("Bot iniciado com sucesso!")


async def main() -> None:
    app = Application.builder().token(TOKEN).post_init(post_init).build()

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CallbackQueryHandler(restart_handler, pattern="^restart$"),
        ],
        states={
            CHOOSING: [
                CallbackQueryHandler(choice_handler, pattern="^(single|batch)$"),
            ],
            SINGLE_LINK: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_single_link),
            ],
            BATCH_LINKS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_batch_links),
            ],
        },
        fallbacks=[
            CommandHandler("cancelar", cancel),
            CommandHandler("start", start),
            CallbackQueryHandler(restart_handler, pattern="^restart$"),
        ],
        per_chat=True,
        per_user=True,
        per_message=False,
    )

    app.add_handler(conv_handler)
    app.add_handler(CallbackQueryHandler(restart_handler, pattern="^restart$"))
    app.add_handler(CommandHandler("ajuda", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown_message))

    logger.info("Iniciando o bot...")
    async with app:
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        logger.info("Bot está rodando! Pressione Ctrl+C para parar.")
        try:
            while True:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            await app.updater.stop()
            await app.stop()


if __name__ == "__main__":
    from keep_alive import keep_alive
    keep_alive()
    asyncio.run(main())
