import os
import re
import asyncio
import zipfile
import shutil
import logging
import uuid
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

import httpx

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

HEADERS_MOBILE = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 13; SM-G991B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    "Referer": "https://shopee.com.br/",
}

HEADERS_DESKTOP = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    "Referer": "https://shopee.com.br/",
    "X-Requested-With": "XMLHttpRequest",
}


def find_shopee_link(text: str) -> str | None:
    match = SHOPEE_LINK_RE.search(text.strip())
    return match.group(0) if match else None


def find_all_shopee_links(text: str) -> list[str]:
    return SHOPEE_LINK_RE.findall(text)


async def resolve_url(url: str) -> str:
    if "universal-link" in url or "universal_link" in url:
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        redir = params.get("redir")
        if redir:
            url = unquote(redir[0])
            logger.info(f"Universal link extraído: {url}")

    if extract_ids(url):
        return url

    for attempt in range(3):
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=20, headers=HEADERS_MOBILE) as client:
                r = await client.get(url)
                resolved = str(r.url)
                logger.info(f"URL resolvida: {url} -> {resolved}")
                return resolved
        except Exception as e:
            logger.warning(f"Tentativa {attempt + 1}/3 de resolver URL falhou: {e}")
            if attempt < 2:
                await asyncio.sleep(1.5 * (attempt + 1))
    return url


def extract_ids(url: str) -> tuple[str, str] | None:
    match = re.search(r"i\.(\d+)\.(\d+)", url)
    if match:
        return match.group(1), match.group(2)

    match = re.search(r"product/(\d+)/(\d+)", url)
    if match:
        return match.group(1), match.group(2)

    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    shop = params.get("shopid") or params.get("shop_id")
    item = params.get("itemid") or params.get("item_id")
    if shop and item:
        return shop[0], item[0]

    match = re.search(r"\.(\d{5,})\.(\d{5,})", url)
    if match:
        return match.group(1), match.group(2)

    return None


def extract_video_from_response(data: dict) -> str | None:
    item_data = data.get("data") or data.get("item") or {}

    video_list = item_data.get("video_info_list") or []
    if isinstance(video_list, list):
        for v in video_list:
            if not isinstance(v, dict):
                continue

            for fmt_key in ["formats", "video_url_list"]:
                fmt_list = v.get(fmt_key) or []
                if isinstance(fmt_list, list):
                    for fmt in fmt_list:
                        if isinstance(fmt, dict):
                            u = fmt.get("url")
                            if u:
                                return u

            u = v.get("video_url") or v.get("url")
            if u:
                return u

            default_fmt = v.get("default_format")
            if isinstance(default_fmt, dict):
                u = default_fmt.get("url")
                if u:
                    return u

    video = item_data.get("video")
    if isinstance(video, dict):
        u = video.get("video_url") or video.get("url")
        if u:
            return u

    tier_vars = item_data.get("tier_variations") or []
    if isinstance(tier_vars, list):
        for tv in tier_vars:
            if isinstance(tv, dict):
                u = tv.get("video")
                if u and isinstance(u, str):
                    return u

    return None


async def fetch_video_url(shop_id: str, item_id: str) -> str | None:
    api_urls = [
        f"https://shopee.com.br/api/v4/item/get?itemid={item_id}&shopid={shop_id}",
        f"https://shopee.com.br/api/v2/item/get?itemid={item_id}&shopid={shop_id}",
    ]

    headers_list = [HEADERS_MOBILE, HEADERS_DESKTOP]

    for api_url in api_urls:
        for headers in headers_list:
            for attempt in range(2):
                try:
                    h = {**headers, "Referer": f"https://shopee.com.br/product/{shop_id}/{item_id}"}
                    async with httpx.AsyncClient(timeout=15, headers=h) as client:
                        r = await client.get(api_url)
                        if r.status_code != 200:
                            continue

                        data = r.json()
                        video_url = extract_video_from_response(data)
                        if video_url:
                            logger.info(f"Video encontrado via API: {api_url}")
                            return video_url

                except Exception as e:
                    logger.warning(f"API falhou ({api_url}): {e}")
                    if attempt < 1:
                        await asyncio.sleep(0.5)

    return None


async def scrape_video_from_html(url: str) -> str | None:
    for headers in [HEADERS_MOBILE, HEADERS_DESKTOP]:
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=20, headers=headers) as client:
                r = await client.get(url)
                if r.status_code != 200:
                    continue
                html = r.text

                patterns = [
                    r'"video_url"\s*:\s*"(https?://[^"]+\.mp4[^"]*)"',
                    r'"video"\s*:\s*\{[^}]*"url"\s*:\s*"(https?://[^"]+)"',
                    r'<meta[^>]+property="og:video"[^>]+content="(https?://[^"]+)"',
                    r'<meta[^>]+content="(https?://[^"]+)"[^>]+property="og:video"',
                    r'<video[^>]+src="(https?://[^"]+)"',
                    r'"playUrl"\s*:\s*"(https?://[^"]+)"',
                    r'"play_url"\s*:\s*"(https?://[^"]+)"',
                ]

                for pattern in patterns:
                    match = re.search(pattern, html, re.IGNORECASE)
                    if match:
                        video_url = match.group(1).replace("\\u002F", "/").replace("\\/", "/")
                        logger.info(f"Video encontrado via HTML scraping")
                        return video_url

        except Exception as e:
            logger.warning(f"HTML scraping falhou: {e}")
    return None


async def get_video_url(link: str) -> str | None:
    resolved = await resolve_url(link)
    logger.info(f"Link final resolvido: {resolved}")

    ids = extract_ids(resolved)
    if not ids:
        logger.warning(f"IDs não encontrados em: {resolved}")
        video_url = await scrape_video_from_html(resolved)
        if video_url:
            return video_url
        return None

    shop_id, item_id = ids
    logger.info(f"IDs: shop={shop_id}, item={item_id}")

    video_url = await fetch_video_url(shop_id, item_id)
    if video_url:
        return video_url

    logger.info("API falhou, tentando HTML scraping...")
    product_url = f"https://shopee.com.br/product/{shop_id}/{item_id}"
    video_url = await scrape_video_from_html(product_url)
    if video_url:
        return video_url

    video_url = await scrape_video_from_html(resolved)
    return video_url


async def download_video(url: str, dest: Path) -> bool:
    urls_to_try = [url]
    clean = re.sub(r"\.\d+\.\d+\.mp4", ".mp4", url)
    if clean != url:
        urls_to_try.insert(0, clean)

    for target in urls_to_try:
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(follow_redirects=True, timeout=120, headers=HEADERS_MOBILE) as client:
                    async with client.stream("GET", target) as resp:
                        if resp.status_code != 200:
                            break
                        with open(dest, "wb") as f:
                            async for chunk in resp.aiter_bytes(chunk_size=1024 * 128):
                                f.write(chunk)
                        if dest.stat().st_size > 0:
                            return True
            except Exception as e:
                logger.warning(f"Download tentativa {attempt + 1}/3: {e}")
                if attempt < 2:
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
        text="⏳ *Buscando vídeo...*",
        parse_mode="Markdown",
    )

    session_dir = DOWNLOADS_DIR / f"s_{chat_id}_{uuid.uuid4().hex[:6]}"
    session_dir.mkdir(parents=True, exist_ok=True)

    try:
        video_url = await get_video_url(link)

        if not video_url:
            await status_msg.edit_text(
                "❌ *Vídeo não encontrado.*\n\n"
                "Possíveis motivos:\n"
                "• O produto não possui vídeo\n"
                "• O link está incorreto ou expirado\n"
                "• A Shopee bloqueou o acesso temporariamente\n\n"
                "Tente novamente com /start",
                parse_mode="Markdown",
            )
            return False

        keyboard = [[InlineKeyboardButton("⬅️ Voltar ao Menu", callback_data="restart")]]
        markup = InlineKeyboardMarkup(keyboard)

        await status_msg.edit_text("⏳ *Enviando vídeo...*", parse_mode="Markdown")

        sent = await send_video_by_url(
            context, chat_id, video_url,
            "✅ Vídeo baixado da Shopee com sucesso!",
            reply_markup=markup,
        )

        if sent:
            await status_msg.delete()
            return True

        await status_msg.edit_text("⏳ *Baixando vídeo...*", parse_mode="Markdown")

        video_path = session_dir / "video.mp4"
        success = await download_video(video_url, video_path)

        if not success or not video_path.exists() or video_path.stat().st_size == 0:
            await status_msg.edit_text(
                "❌ *Falha ao baixar o vídeo.*\n\n"
                "O servidor da Shopee pode estar bloqueando.\n"
                "Tente novamente mais tarde com /start",
                parse_mode="Markdown",
            )
            return False

        file_size = video_path.stat().st_size
        if file_size > 50 * 1024 * 1024:
            await status_msg.edit_text(
                "❌ *O vídeo é muito grande* (> 50MB).\n\n"
                "O Telegram não permite arquivos maiores que 50MB.\n"
                "Tente outro vídeo com /start",
                parse_mode="Markdown",
            )
            return False

        await status_msg.edit_text("⏳ *Enviando vídeo...*", parse_mode="Markdown")
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO)

        with open(video_path, "rb") as vf:
            await context.bot.send_video(
                chat_id=chat_id,
                video=vf,
                caption="✅ Vídeo baixado da Shopee com sucesso!",
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
            await status_msg.edit_text(
                f"❌ *Erro ao processar o vídeo.*\n\n"
                f"Detalhes: `{str(e)[:200]}`\n\n"
                f"Tente novamente com /start",
                parse_mode="Markdown",
            )
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
        text=f"📦 *Lote: {total} link(s)*\n\n"
             f"[{'░' * 10}] 0%\n\n"
             f"⏳ Processando simultaneamente...",
        parse_mode="Markdown",
    )

    session_dir = DOWNLOADS_DIR / f"b_{chat_id}_{uuid.uuid4().hex[:6]}"
    session_dir.mkdir(parents=True, exist_ok=True)

    semaphore = asyncio.Semaphore(5)
    completed = 0
    downloaded = []
    failed = []
    lock = asyncio.Lock()

    async def process_one(idx: int, link: str):
        nonlocal completed
        async with semaphore:
            try:
                video_url = await get_video_url(link)
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
                    f"📦 *Lote: {total} link(s)*\n\n"
                    f"[{bar}] {pct}%\n"
                    f"✅ {len(downloaded)} | ❌ {len(failed)}\n\n"
                    f"Processando {completed}/{total}...",
                    parse_mode="Markdown",
                )
            except Exception:
                pass

    tasks = [process_one(idx, link) for idx, link in enumerate(links, 1)]
    await asyncio.gather(*tasks)

    try:
        if not downloaded:
            await status_msg.edit_text(
                "❌ *Nenhum vídeo baixado.*\n\n"
                "Os produtos podem não ter vídeos ou os links estão incorretos.\n\n"
                "Tente novamente com /start",
                parse_mode="Markdown",
            )
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
            await status_msg.edit_text(
                f"❌ *ZIP muito grande* ({zip_size / (1024*1024):.1f}MB > 50MB).\n\n"
                f"Tente com menos vídeos usando /start",
                parse_mode="Markdown",
            )
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
                filename="videos_shopee.zip",
                caption="\n".join(caption_parts),
                parse_mode="Markdown", reply_markup=markup,
                read_timeout=120, write_timeout=120,
            )

        await status_msg.delete()
        return True

    except Exception as e:
        logger.error(f"Erro no lote: {e}")
        try:
            await status_msg.edit_text(
                f"❌ *Erro no processamento.*\n\n"
                f"Detalhes: `{str(e)[:200]}`\n\n"
                f"Tente novamente com /start",
                parse_mode="Markdown",
            )
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
        "🛍️ *Shopee Video Downloader*\n\n"
        "Olá! Eu baixo vídeos de produtos da Shopee.\n\n"
        "🎬 *Vídeo único* — Envie um link\n"
        "📦 *Lista* — Envie vários links de uma vez"
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
            "Envie o link do produto da Shopee.\n\n"
            "📎 Aceito qualquer formato de link:\n"
            "• `https://shopee.com.br/produto-i.123.456`\n"
            "• `https://br.shp.ee/abc123`\n"
            "• `https://s.shopee.com.br/abc`\n\n"
            "💡 Cole o link diretamente do app ou site.",
            parse_mode="Markdown",
        )
        return SINGLE_LINK

    elif query.data == "batch":
        await query.edit_message_text(
            "📦 *Modo: Lista de Vídeos*\n\n"
            "Envie os links, *um por linha*.\n\n"
            "📎 Exemplo:\n"
            "`https://shopee.com.br/produto-1-i.123.456`\n"
            "`https://br.shp.ee/abc123`\n"
            "`https://shopee.com.br/produto-2-i.789.012`\n\n"
            "⚠️ Envie todos em *uma única mensagem*.\n"
            "⚡ Todos são processados *simultaneamente*!",
            parse_mode="Markdown",
        )
        return BATCH_LINKS

    return CHOOSING


async def handle_single_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    link = find_shopee_link(text)

    if not link:
        await update.message.reply_text(
            "⚠️ *Link inválido!*\n\n"
            "Envie um link válido da Shopee.\n\n"
            "📎 Exemplos aceitos:\n"
            "• `https://shopee.com.br/produto-i.123.456`\n"
            "• `https://br.shp.ee/abc123`\n\n"
            "Ou envie /cancelar para voltar.",
            parse_mode="Markdown",
        )
        return SINGLE_LINK

    await process_single_link(update, context, link)
    return ConversationHandler.END


async def handle_batch_links(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    links = find_all_shopee_links(text)

    if not links:
        await update.message.reply_text(
            "⚠️ *Nenhum link da Shopee encontrado!*\n\n"
            "Envie os links um por linha.\n\n"
            "📎 Exemplo:\n"
            "`https://shopee.com.br/produto-1-i.123.456`\n"
            "`https://br.shp.ee/abc123`\n\n"
            "Ou envie /cancelar para voltar.",
            parse_mode="Markdown",
        )
        return BATCH_LINKS

    links = list(dict.fromkeys(links))

    await update.message.reply_text(
        f"✅ *{len(links)} link(s) detectado(s)!*\n\n"
        f"⚡ Iniciando download simultâneo...",
        parse_mode="Markdown",
    )

    await process_batch_links(update, context, links)
    return ConversationHandler.END


async def restart_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await start(update, context)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    keyboard = [[InlineKeyboardButton("🔄 Recomeçar", callback_data="restart")]]
    await update.message.reply_text(
        "❌ Operação cancelada.\n\nUse /start para recomeçar.",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return ConversationHandler.END


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    help_text = (
        "📖 *Como usar:*\n\n"
        "1️⃣ /start — Iniciar\n"
        "2️⃣ Escolha vídeo único ou lista\n"
        "3️⃣ Envie o(s) link(s) da Shopee\n"
        "4️⃣ Receba o vídeo em segundos!\n\n"
        "*Comandos:*\n"
        "/start — Iniciar o bot\n"
        "/ajuda — Esta mensagem\n"
        "/cancelar — Cancelar operação\n\n"
        "*Formatos aceitos:*\n"
        "• Links completos da Shopee\n"
        "• Links curtos (shp.ee, s.shopee)\n"
        "• Limite do Telegram: 50MB por arquivo"
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")


async def unknown_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.strip()
    link = find_shopee_link(text)

    if link:
        await process_single_link(update, context, link)
    else:
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
        logger.info("Bot rodando!")
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
