import os
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = os.path.join(os.getcwd(), ".playwright")

import re
import asyncio
import zipfile
import shutil
import logging
import uuid
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

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

TOKEN = os.environ.get("BOT_TOKEN", "8801492248:AAHJP8iEWT19ZcpBU_blm1pNYki3k3vNcK4")

CHOOSING, SINGLE_LINK, BATCH_LINKS = range(3)

BASE_DIR = Path(__file__).parent
DOWNLOADS_DIR = BASE_DIR / "downloads"
DOWNLOADS_DIR.mkdir(exist_ok=True)

SHOPEE_LINK_RE = re.compile(
    r"https?://(?:[\w-]+\.)*(?:shopee\.[\w.]+|shp\.ee|s\.shopee\.[\w.]+)/[\w\-./!?=&%+@#]+",
    re.IGNORECASE,
)

def find_shopee_link(text: str) -> str | None:
    match = SHOPEE_LINK_RE.search(text.strip())
    return match.group(0) if match else None

def find_all_shopee_links(text: str) -> list[str]:
    return SHOPEE_LINK_RE.findall(text)


async def resolve_url(url: str, context: ContextTypes.DEFAULT_TYPE) -> str:
    if "universal-link" in url or "universal_link" in url:
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        redir = params.get("redir")
        if redir:
            url = unquote(redir[0])
            
    if "shp.ee" in url or "s.shopee" in url:
        browser_ctx = context.bot_data.get("browser")
        if browser_ctx:
            try:
                page = await browser_ctx.new_page()
                await page.goto(url, timeout=15000)
                url = page.url
                await page.close()
            except Exception:
                pass
    return url


async def get_video_url_playwright(url: str, browser_ctx) -> str | None:
    try:
        page = await browser_ctx.new_page()
        video_urls = []

        async def handle_response(response):
            try:
                content_type = response.headers.get("content-type", "")
                req_url = response.url
                
                # 1. Pegar links de arquivos MP4 diretos
                if ".mp4" in req_url:
                    if req_url not in video_urls:
                        video_urls.append(req_url)
                
                # 2. Pegar links dentro do JSON da API (frequentemente são a versão limpa)
                elif "application/json" in content_type and ("api/v" in req_url or "graphql" in req_url):
                    text = await response.text()
                    import re
                    mp4s = re.findall(r'https?://[^"]+\.mp4[^"]*', text)
                    for mp4 in mp4s:
                        clean_mp4 = mp4.replace('\\u002F', '/').replace('\\/', '/')
                        if clean_mp4 not in video_urls:
                            video_urls.append(clean_mp4)
            except Exception:
                pass

        page.on("response", handle_response)

        await page.goto(url, wait_until="domcontentloaded", timeout=25000)
        await page.wait_for_timeout(3000)

        try:
            play_btn = page.locator('[class*="video"], [class*="play"], [aria-label*="play"], [data-testid*="video"]').first
            if await play_btn.count() > 0:
                await play_btn.click(timeout=3000)
                await page.wait_for_timeout(2000)
        except Exception:
            pass

        try:
            await page.evaluate("""
                () => {
                    const videos = document.querySelectorAll('video');
                    videos.forEach(v => { if (v.src) v.play(); });
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

        await page.close()

        # Filtrar e priorizar URLs sem marca d'água
        mp4_urls = [u for u in video_urls if ".mp4" in u]
        
        if mp4_urls:
            # Função para penalizar URLs com 'wm' (watermark)
            def score_url(u):
                u_lower = u.lower()
                score = 0
                if "wm" in u_lower or "watermark" in u_lower:
                    score += 100
                return score
                
            mp4_urls.sort(key=score_url)
            
            best_url = mp4_urls[0]
            
            # Tentar remover _wm da URL para forçar o vídeo limpo
            import re
            clean_forced = re.sub(r'(_wm|-wm|watermark)', '', best_url, flags=re.IGNORECASE)
            
            # A mágica final para vídeos curtos da Shopee: remover a numeração no final do arquivo
            # que indica o render com marca d'água (ex: .1600355.8302.mp4 -> .mp4)
            clean_forced = re.sub(r'\.\d+\.\d+\.mp4$', '.mp4', clean_forced, flags=re.IGNORECASE)
            
            if clean_forced != best_url:
                # Se mudou, colocar a URL limpa como prioridade 1 para testar no download
                return clean_forced
                
            return best_url

        return video_urls[0] if video_urls else None
    except Exception as e:
        logger.error(f"Erro Playwright get_video_url: {e}")
        return None


async def download_video(url: str, dest: Path) -> bool:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://shopee.com.br/",
    }
    urls_to_try = [url]

    for target in urls_to_try:
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(follow_redirects=True, timeout=60, headers=headers) as client:
                    async with client.stream("GET", target) as resp:
                        if resp.status_code == 200:
                            with open(dest, "wb") as f:
                                async for chunk in resp.aiter_bytes(chunk_size=1024 * 128):
                                    f.write(chunk)
                            if dest.stat().st_size > 0:
                                return True
            except Exception as e:
                logger.warning(f"Download tentativa {attempt + 1}: {e}")
                await asyncio.sleep(1)
    return False


async def send_video_by_url(context, chat_id: int, video_url: str, caption: str, reply_markup=None) -> bool:
    try:
        await context.bot.send_video(
            chat_id=chat_id,
            video=video_url,
            caption=caption,
            supports_streaming=True,
            reply_markup=reply_markup,
            read_timeout=60,
            write_timeout=60,
            connect_timeout=30,
        )
        return True
    except Exception as e:
        logger.warning(f"Envio direto por URL falhou: {e}")
        return False


async def process_single_link(update: Update, context: ContextTypes.DEFAULT_TYPE, link: str) -> bool:
    chat_id = update.effective_chat.id
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    status_msg = await context.bot.send_message(
        chat_id=chat_id,
        text="⏳ *Buscando vídeo de forma segura...*",
        parse_mode="Markdown",
    )

    session_dir = DOWNLOADS_DIR / f"s_{chat_id}_{uuid.uuid4().hex[:6]}"
    session_dir.mkdir(parents=True, exist_ok=True)

    try:
        resolved = await resolve_url(link, context)
        video_url = await get_video_url_playwright(resolved, context.bot_data["browser"])

        if not video_url:
            await status_msg.edit_text(
                "❌ *Vídeo não encontrado.*\n\n"
                "Possíveis motivos:\n"
                "• O produto não possui vídeo\n"
                "• O link está incorreto ou expirado\n\n"
                "Tente novamente com /start",
                parse_mode="Markdown",
            )
            return False

        keyboard = [[InlineKeyboardButton("⬅️ Voltar ao Menu", callback_data="restart")]]
        markup = InlineKeyboardMarkup(keyboard)

        await status_msg.edit_text("⏳ *Enviando vídeo (Super rápido!)...*", parse_mode="Markdown")

        sent = await send_video_by_url(
            context, chat_id, video_url,
            "✅ Vídeo baixado da Shopee com sucesso!",
            reply_markup=markup,
        )

        if sent:
            await status_msg.delete()
            return True

        await status_msg.edit_text("⏳ *Baixando vídeo pesadamente...*", parse_mode="Markdown")

        video_path = session_dir / "video.mp4"
        success = await download_video(video_url, video_path)

        if not success or not video_path.exists() or video_path.stat().st_size == 0:
            await status_msg.edit_text("❌ *Falha ao baixar o vídeo.*\nTente com /start", parse_mode="Markdown")
            return False

        await status_msg.edit_text("⏳ *Fazendo upload para o Telegram...*", parse_mode="Markdown")
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO)

        with open(video_path, "rb") as vf:
            await context.bot.send_video(
                chat_id=chat_id,
                video=vf,
                caption="✅ Vídeo baixado com sucesso!",
                supports_streaming=True,
                reply_markup=markup,
                read_timeout=120,
                write_timeout=120,
            )

        await status_msg.delete()
        return True

    except Exception as e:
        logger.error(f"Erro ao processar link: {e}")
        try:
            await status_msg.edit_text(f"❌ *Erro ao processar o vídeo.*\nTente com /start", parse_mode="Markdown")
        except Exception:
            pass
        return False
    finally:
        if session_dir.exists():
            shutil.rmtree(session_dir, ignore_errors=True)


async def process_batch_links(update: Update, context: ContextTypes.DEFAULT_TYPE, links: list[str]) -> bool:
    chat_id = update.effective_chat.id
    total = len(links)

    status_msg = await context.bot.send_message(
        chat_id=chat_id,
        text=f"📦 *Lote: {total} link(s)*\n\n[{'░' * 10}] 0%\n\n⏳ Processando com navegador em background...",
        parse_mode="Markdown",
    )

    session_dir = DOWNLOADS_DIR / f"b_{chat_id}_{uuid.uuid4().hex[:6]}"
    session_dir.mkdir(parents=True, exist_ok=True)

    # Limit to 3 concurrent Playwright tabs to avoid crashing on free tier
    semaphore = asyncio.Semaphore(3)
    completed = 0
    downloaded = []
    failed = []
    lock = asyncio.Lock()

    async def process_one(idx: int, link: str):
        nonlocal completed
        async with semaphore:
            try:
                resolved = await resolve_url(link, context)
                video_url = await get_video_url_playwright(resolved, context.bot_data["browser"])
                if not video_url:
                    async with lock:
                        failed.append(f"Link {idx}: sem vídeo")
                        completed += 1
                    return

                video_path = session_dir / f"video_{idx:03d}.mp4"
                ok = await download_video(video_url, video_path)

                async with lock:
                    if ok and video_path.exists() and video_path.stat().st_size > 0:
                        downloaded.append(video_path)
                    else:
                        failed.append(f"Link {idx}: download falhou")
                    completed += 1

            except Exception as e:
                logger.error(f"Erro link {idx}: {e}")
                async with lock:
                    failed.append(f"Link {idx}: erro")
                    completed += 1

            try:
                pct = int((completed / total) * 100)
                bar_filled = int((completed / total) * 10)
                bar = "█" * bar_filled + "░" * (10 - bar_filled)
                await status_msg.edit_text(
                    f"📦 *Lote: {total} link(s)*\n\n[{bar}] {pct}%\n✅ {len(downloaded)} | ❌ {len(failed)}\n\nProcessando {completed}/{total}...",
                    parse_mode="Markdown",
                )
            except Exception:
                pass

    tasks = [process_one(idx, link) for idx, link in enumerate(links, 1)]
    await asyncio.gather(*tasks)

    try:
        if not downloaded:
            await status_msg.edit_text("❌ *Nenhum vídeo baixado.*\nTente novamente com /start", parse_mode="Markdown")
            return False

        keyboard = [[InlineKeyboardButton("⬅️ Voltar ao Menu", callback_data="restart")]]
        markup = InlineKeyboardMarkup(keyboard)

        if len(downloaded) == 1:
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO)
            caption = f"✅ *1 vídeo* baixado!"
            if failed:
                caption += f"\n⚠️ {len(failed)} link(s) falharam"

            with open(downloaded[0], "rb") as vf:
                await context.bot.send_video(
                    chat_id=chat_id, video=vf, caption=caption,
                    supports_streaming=True, reply_markup=markup,
                    parse_mode="Markdown", read_timeout=120, write_timeout=120,
                )
            await status_msg.delete()
            return True

        await status_msg.edit_text("📁 *Criando ZIP...*", parse_mode="Markdown")

        zip_path = session_dir / "videos_shopee.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for vf in sorted(downloaded):
                zf.write(vf, vf.name)

        zip_size = zip_path.stat().st_size
        if zip_size > 50 * 1024 * 1024:
            await status_msg.edit_text(f"❌ *ZIP muito grande* ({zip_size / (1024*1024):.1f}MB > 50MB).\nTente com menos vídeos.", parse_mode="Markdown")
            return False

        await status_msg.edit_text("📤 *Enviando ZIP...*", parse_mode="Markdown")
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_DOCUMENT)

        caption_parts = [f"✅ *{len(downloaded)} vídeo(s)* baixado(s)!"]
        if failed:
            caption_parts.append(f"\n⚠️ *{len(failed)} falha(s):*")
            for fl in failed[:5]:
                caption_parts.append(f"• {fl}")
            if len(failed) > 5:
                caption_parts.append(f"• ...e mais {len(failed) - 5}")

        with open(zip_path, "rb") as zf:
            await context.bot.send_document(
                chat_id=chat_id, document=zf,
                filename="videos_shopee.zip", caption="\n".join(caption_parts),
                parse_mode="Markdown", reply_markup=markup,
                read_timeout=120, write_timeout=120,
            )

        await status_msg.delete()
        return True

    except Exception as e:
        logger.error(f"Erro no lote: {e}")
        try:
            await status_msg.edit_text(f"❌ *Erro no processamento.*\nTente novamente.", parse_mode="Markdown")
        except Exception:
            pass
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
        "🛍️ *Shopee Video Downloader Profissional*\n\n"
        "Olá! Eu baixo vídeos de produtos da Shopee rapidamente.\n\n"
        "🎬 *Vídeo único* — Envie um link\n"
        "📦 *Lista* — Envie vários links de uma vez"
    )

    if update.callback_query:
        await update.callback_query.answer()
        try:
            await update.callback_query.edit_message_text(
                welcome_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown",
            )
        except Exception:
            await update.callback_query.message.reply_text(
                welcome_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown",
            )
    else:
        await update.message.reply_text(
            welcome_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown",
        )
    return CHOOSING

async def choice_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == "single":
        await query.edit_message_text(
            "🎬 *Modo: Vídeo Único*\n\nEnvie o link do produto da Shopee.\n\n📎 Aceito qualquer formato de link:\n• `https://shopee.com.br/produto-i.123.456`\n• `https://br.shp.ee/abc123`\n• `https://s.shopee.com.br/abc`\n\n💡 Cole o link diretamente do app ou site.",
            parse_mode="Markdown",
        )
        return SINGLE_LINK
    elif query.data == "batch":
        await query.edit_message_text(
            "📦 *Modo: Lista de Vídeos*\n\nEnvie os links, *um por linha*.\n\n📎 Exemplo:\n`https://shopee.com.br/produto-1-i.123.456`\n`https://br.shp.ee/abc123`\n\n⚠️ Envie todos em *uma única mensagem*.\n⚡ Todos são processados *simultaneamente* em background!",
            parse_mode="Markdown",
        )
        return BATCH_LINKS
    return CHOOSING

async def handle_single_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    link = find_shopee_link(text)
    if not link:
        await update.message.reply_text(
            "⚠️ *Link inválido!*\nEnvie um link válido da Shopee.", parse_mode="Markdown",
        )
        return SINGLE_LINK
    await process_single_link(update, context, link)
    return ConversationHandler.END

async def handle_batch_links(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    links = find_all_shopee_links(text)
    if not links:
        await update.message.reply_text("⚠️ *Nenhum link da Shopee encontrado!*", parse_mode="Markdown")
        return BATCH_LINKS
    links = list(dict.fromkeys(links))
    await update.message.reply_text(f"✅ *{len(links)} link(s) detectado(s)!*\n⚡ Iniciando download simultâneo em background...", parse_mode="Markdown")
    await process_batch_links(update, context, links)
    return ConversationHandler.END

async def restart_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await start(update, context)

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    keyboard = [[InlineKeyboardButton("🔄 Recomeçar", callback_data="restart")]]
    await update.message.reply_text("❌ Operação cancelada.", reply_markup=InlineKeyboardMarkup(keyboard))
    return ConversationHandler.END

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    help_text = (
        "📖 *Como usar:*\n\n1️⃣ /start — Iniciar\n2️⃣ Escolha vídeo único ou lista\n3️⃣ Envie o(s) link(s) da Shopee\n4️⃣ Receba o vídeo em segundos!\n\n*Formatos aceitos:*\n• Links completos da Shopee\n• Links curtos (shp.ee, s.shopee)"
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")

async def unknown_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.strip()
    link = find_shopee_link(text)
    if link:
        await process_single_link(update, context, link)
    else:
        await update.message.reply_text("🤔 Não entendi! Use /start para iniciar o bot.")

async def post_init(application: Application) -> None:
    await application.bot.set_my_commands([
        BotCommand("start", "Iniciar o bot"),
        BotCommand("ajuda", "Como usar o bot"),
        BotCommand("cancelar", "Cancelar operação atual"),
    ])
    logger.info("Comandos do bot registrados.")

async def main() -> None:
    app = Application.builder().token(TOKEN).post_init(post_init).build()

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CallbackQueryHandler(restart_handler, pattern="^restart$"),
        ],
        states={
            CHOOSING: [CallbackQueryHandler(choice_handler, pattern="^(single|batch)$")],
            SINGLE_LINK: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_single_link)],
            BATCH_LINKS: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_batch_links)],
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

    logger.info("Iniciando Playwright e o Bot...")
    
    # KEEP PLAYWRIGHT OPEN PERSISTENTLY
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-extensions",
                "--mute-audio"
            ],
        )
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            viewport={"width": 1366, "height": 768},
            locale="pt-BR",
        )
        
        # Injetar script para burlar detecção simples
        await ctx.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        """)

        app.bot_data["browser"] = ctx
        
        async with app:
            await app.start()
            await app.updater.start_polling(drop_pending_updates=True)
            logger.info("Bot rodando com navegador persistente!")
            try:
                while True:
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                pass
            finally:
                await app.updater.stop()
                await app.stop()
        
        await browser.close()

if __name__ == "__main__":
    from keep_alive import keep_alive
    keep_alive()
    asyncio.run(main())
