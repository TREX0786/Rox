import os
import re
import html
import asyncio
import logging
import requests
from bs4 import BeautifulSoup

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# Token hardcoded
BOT_TOKEN = "9102479151:AAF6C_MhNU8-JqXTnaHiBUzjpErm5C--yn8"

USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "x-ig-app-id": "936619743392459",
    "accept": "*/*",
    "accept-language": "en-US,en;q=0.9",
    "referer": "https://www.instagram.com/",
}

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("igbot")

def _h(t):
    return html.escape(str(t or ""), quote=False)

# --- DATA FETCHING LAYER (API + HTML Fallback) ---

def fetch_profile(username: str):
    """Tries API first, then falls back to HTML parsing."""
    # 1. Try API
    api_url = "https://www.instagram.com/api/v1/users/web_profile_info/"
    try:
        r = requests.get(api_url, headers=HEADERS, params={"username": username}, timeout=10)
        if r.status_code == 200:
            data = r.json()
            user = (data.get("data") or {}).get("user")
            if user:
                return user
    except Exception as e:
        log.warning(f"API fetch failed for @{username}: {e}")

    # 2. Fallback to HTML Scraping
    log.info(f"API failed, trying HTML fallback for @{username}")
    try:
        r = requests.get(f"https://www.instagram.com/{username}/", headers=HEADERS, timeout=10)
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, 'html.parser')
            desc_tag = soup.find('meta', property='og:description')
            title_tag = soup.find('meta', property='og:title')
            img_tag = soup.find('meta', property='og:image')

            if not desc_tag:
                return None

            desc = desc_tag.get('content', '')
            title = title_tag.get('content', '') if title_tag else ""
            img_url = img_tag.get('content', '') if img_tag else ""

            # Parse "1.2M Followers, 300 Following, 4K Posts"
            followers, following, posts = 0, 0, 0
            f_match = re.search(r'([\d.,]+[KMB]?)\s*Followers', desc, re.IGNORECASE)
            if f_match: followers = f_match.group(1)
            
            fg_match = re.search(r'([\d.,]+[KMB]?)\s*Following', desc, re.IGNORECASE)
            if fg_match: following = fg_match.group(1)
            
            p_match = re.search(r'([\d.,]+[KMB]?)\s*Posts', desc, re.IGNORECASE)
            if p_match: posts = p_match.group(1)

            return {
                "username": username,
                "full_name": title.split("(")[0].strip() if title else "—",
                "biography": "Fetched via backup (API Blocked)",
                "profile_pic_url_hd": img_url,
                "edge_followed_by": {"count": followers},
                "edge_follow": {"count": following},
                "edge_owner_to_timeline_media": {"count": posts, "edges": []},
                "is_private": "Followers" not in desc and "Posts" not in desc
            }
    except Exception as e:
        log.error(f"HTML fallback failed for @{username}: {e}")

    return None

def extract_posts(user, limit=12):
    edges = ((user.get("edge_owner_to_timeline_media") or {}).get("edges") or [])
    out = []
    for edge in edges[:limit]:
        node = edge.get("node") or {}
        media = node.get("image_versions2") or {}
        candidates = media.get("candidates") or []
        
        url = ""
        if candidates:
            best = max(candidates, key=lambda c: c.get("width", 0) * c.get("height", 0))
            url = best.get("url", "")
        if not url:
            url = node.get("display_url") or node.get("thumbnail_src") or ""
            
        cap = ""
        cap_edges = (node.get("edge_media_to_caption") or {}).get("edges") or []
        if cap_edges:
            cap = (cap_edges[0].get("node") or {}).get("text", "")
            
        out.append({
            "shortcode": node.get("shortcode") or "",
            "url": url,
            "caption": cap,
            "is_video": bool(node.get("is_video")),
            "video_url": node.get("video_url") or "",
        })
    return out

def format_profile(user):
    username = user.get("username", "?")
    full_name = user.get("full_name") or "—"
    bio = (user.get("biography") or "").strip() or "—"
    private = " 🔒" if user.get("is_private") else ""
    followers = (user.get("edge_followed_by") or {}).get("count", 0)
    following = (user.get("edge_follow") or {}).get("count", 0)
    posts = (user.get("edge_owner_to_timeline_media") or {}).get("count", 0)
    
    return (
        f"<b>@{_h(username)}</b>{private}\n"
        f"<b>Name:</b> {_h(full_name)}\n"
        f"<b>Bio:</b> {_h(bio)}\n\n"
        f"<b>Followers:</b> {followers}\n"
        f"<b>Following:</b> {following}\n"
        f"<b>Posts:</b> {posts}"
    )

def format_post(i, post):
    sc = post.get("shortcode") or ""
    link = f"https://www.instagram.com/p/{sc}/" if sc else "—"
    kind = "🎬 video" if post.get("is_video") else "🖼 photo"
    cap = (post.get("caption") or "").strip()
    if len(cap) > 150: cap = cap[:147] + "..."
    return (
        f"<b>#{i}</b> · {kind}\n"
        f"{_h(cap) if cap else '<i>no caption</i>'}\n"
        f"<a href=\"{link}\">open post</a>"
    )

# --- TELEGRAM HANDLERS ---

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "👋 <b>Instagram Info Bot</b>\n\nSend any public username to get profile info and recent posts.",
        parse_mode=ParseMode.HTML
    )

async def handle_username(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    raw = (msg.text or "").strip().lstrip("@")
    
    if not raw or not USERNAME_RE.match(raw):
        await msg.reply_text("⚠️ Invalid username.")
        return

    status = await msg.reply_text(f"🔎 Fetching <b>@{_h(raw)}</b> ...", parse_mode=ParseMode.HTML)

    # Fetch in a separate thread so the bot doesn't freeze
    user = await asyncio.to_thread(fetch_profile, raw)

    if not user:
        await status.edit_text(f"❌ <b>@{_h(raw)}</b> not found or account is private.")
        return

    # Send Profile
    header = format_profile(user)
    pic = user.get("profile_pic_url_hd") or user.get("profile_pic_url") or ""
    
    sent = False
    if pic:
        try:
            await msg.reply_photo(photo=pic, caption=header[:1024], parse_mode=ParseMode.HTML)
            sent = True
        except Exception:
            pass # If photo fails, it falls back to text below
            
    if sent:
        await status.delete()
    else:
        await status.edit_text(header, parse_mode=ParseMode.HTML)

    # Send Posts
    posts = extract_posts(user, limit=12)
    if not posts:
        await msg.reply_text("ℹ️ No public posts found (or API was rate-limited).")
        return

    for i, post in enumerate(posts, 1):
        text = format_post(i, post)
        url = post.get("video_url") if post.get("is_video") else post.get("url")
        
        if not url:
            await msg.reply_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
            continue
            
        try:
            if post.get("is_video"):
                await msg.reply_video(video=url, caption=text[:1024], parse_mode=ParseMode.HTML)
            else:
                await msg.reply_photo(photo=url, caption=text[:1024], parse_mode=ParseMode.HTML)
        except Exception:
            # If Telegram fails to download the image, send text link instead
            await msg.reply_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
            
        await asyncio.sleep(0.5)

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_username))
    
    log.info("Bot is starting on cloud...")
    # Standard run_polling - Perfect for Render/Heroku/Docker
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()