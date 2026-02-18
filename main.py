import os
import time
import asyncio
import logging
from logging.handlers import RotatingFileHandler

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from context7_client import Context7Client

load_dotenv()

LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")
os.makedirs(LOG_DIR, exist_ok=True)

LOG_FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)

console = logging.StreamHandler()
console.setFormatter(logging.Formatter(LOG_FMT))
root_logger.addHandler(console)

file_handler = RotatingFileHandler(
    os.path.join(LOG_DIR, "bot.log"),
    maxBytes=5 * 1024 * 1024,
    backupCount=5,
    encoding="utf-8",
)
file_handler.setFormatter(logging.Formatter(LOG_FMT))
root_logger.addHandler(file_handler)

log = logging.getLogger("bot")

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN is not set in .env")

CONTEXT7_API_KEY = os.getenv("CONTEXT7_API_KEY", "")

DEFAULT_LIBRARY = "/websites/all-hands_dev"

LIBRARY_CHOICES = [
    app_commands.Choice(name="Official Docs (default)", value="/websites/all-hands_dev"),
    app_commands.Choice(name="GitHub Repo", value="/openhands/openhands"),
    app_commands.Choice(name="All sources", value="__all__"),
]

ALL_LIBRARY_IDS = [
    "/websites/all-hands_dev",
    "/openhands/openhands",
]

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
ctx7 = Context7Client(api_key=CONTEXT7_API_KEY)


@bot.event
async def on_ready():
    log.info("Logged in as %s (ID: %s)", bot.user, bot.user.id)
    try:
        synced = await bot.tree.sync()
        log.info("Synced %d slash command(s)", len(synced))
    except Exception:
        log.exception("Failed to sync slash commands")


@bot.tree.command(name="ask", description="Ask a question about OpenHands")
@app_commands.describe(
    question="Your question about OpenHands",
    source="Which doc source to search (default: Official Docs)",
)
@app_commands.choices(source=LIBRARY_CHOICES)
async def ask_command(
    interaction: discord.Interaction,
    question: str,
    source: app_commands.Choice[str] | None = None,
):
    chosen = source.value if source else DEFAULT_LIBRARY
    lib_ids = ALL_LIBRARY_IDS if chosen == "__all__" else [chosen]
    source_label = source.name if source else "Official Docs"

    user = interaction.user
    guild = interaction.guild
    log.info(
        "/ask invoked by %s (%s) in %s — question: %r, source: %s",
        user, user.id, guild or "DM", question, source_label,
    )
    await interaction.response.defer(thinking=True)

    t0 = time.perf_counter()
    try:
        async def _fetch(lib_id: str) -> list[dict]:
            try:
                snippets = await ctx7.get_context(lib_id, question, response_type="json")
                if isinstance(snippets, list):
                    log.info("  %s returned %d snippet(s)", lib_id, len(snippets))
                    for s in snippets:
                        s["_source_lib"] = lib_id
                    return snippets
                log.warning("  %s returned unexpected type: %s", lib_id, type(snippets).__name__)
            except Exception:
                log.warning("  Failed to fetch from %s, skipping", lib_id, exc_info=True)
            return []

        results = await asyncio.gather(*[_fetch(lib) for lib in lib_ids])
        all_snippets = [s for batch in results for s in batch]

        elapsed = time.perf_counter() - t0

        if not all_snippets:
            log.info("No snippets found for %r (%.2fs)", question, elapsed)
            await interaction.followup.send(
                "No documentation found for that question. Try rephrasing it."
            )
            return

        log.info(
            "Fetched %d snippet(s) for %r (%.2fs)",
            len(all_snippets), question, elapsed,
        )
        embed = build_embed(question, all_snippets, source_label)
        await interaction.followup.send(embed=embed)

    except Exception as exc:
        log.exception("/ask failed for %r", question)
        await interaction.followup.send(f"Something went wrong: `{exc}`")


@bot.tree.command(name="help_oh", description="Show what this bot can do")
async def help_command(interaction: discord.Interaction):
    log.info("/help_oh invoked by %s (%s)", interaction.user, interaction.user.id)
    embed = discord.Embed(
        title="OpenHands Docs Bot",
        description=(
            "I answer questions about **OpenHands** using up-to-date documentation.\n\n"
            "**Commands:**\n"
            "`/ask <question> [source]` — Ask anything about OpenHands\n"
            "`/help_oh` — Show this message\n\n"
            "**Sources (optional dropdown):**\n"
            "• **Official Docs** — default, user-facing documentation\n"
            "• **SDK Docs** — building agents with the SDK\n"
            "• **GitHub Repo** — source code & dev docs\n"
            "• **All sources** — search everything\n\n"
            "**Example questions:**\n"
            "• How do I install OpenHands?\n"
            "• How to configure a custom agent?\n"
            "• What runtime sandbox options are available?\n"
            "• How does the event stream work?"
        ),
        color=0x5865F2,
    )
    embed.set_footer(text="Powered by Context7")
    await interaction.response.send_message(embed=embed)


def _dedup_snippets(snippets: list[dict]) -> list[dict]:
    """Remove near-duplicate snippets by comparing the first 200 chars of content."""
    seen: set[str] = set()
    unique: list[dict] = []
    for snip in snippets:
        content = snip.get("content", "")
        # Extract the first meaningful chunk as a fingerprint
        fingerprint = content[:200].strip().lower()
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        unique.append(snip)
    return unique


def _safe_truncate(text: str, limit: int) -> str:
    """Truncate text without breaking markdown code blocks."""
    if len(text) <= limit:
        return text

    truncated = text[: limit - 1]

    # Count opening/closing fences to see if we're inside a code block
    fence_count = truncated.count("```")
    if fence_count % 2 != 0:
        # We're inside an unclosed code block — cut before it opened
        last_open = truncated.rfind("```")
        truncated = truncated[:last_open].rstrip()

    return truncated + "…"


def build_embed(query: str, snippets: list[dict], source_label: str = "Official Docs") -> discord.Embed:
    embed = discord.Embed(
        title="OpenHands",
        description=f"**Q:** {query}",
        color=0x57F287,
    )

    deduped = _dedup_snippets(snippets)
    log.info("Deduped %d → %d unique snippet(s)", len(snippets), len(deduped))

    total_len = 0
    max_embed_len = 5500
    max_field_len = 1024  # Discord hard limit per field

    for snip in deduped[:6]:
        title = snip.get("title", "Untitled")
        content = snip.get("content", "")
        source = snip.get("source", "")

        if source:
            source_link = f"\n[Source]({source})"
        else:
            source_link = ""

        available = max_field_len - len(source_link) - 1
        content = _safe_truncate(content, available)

        field_text = content + source_link

        if not field_text.strip():
            continue
        if total_len + len(field_text) > max_embed_len:
            break
        total_len += len(field_text)

        embed.add_field(name=title[:256], value=field_text, inline=False)

    embed.set_footer(text=f"Source: {source_label} · Powered by Context7")
    return embed


bot.run(DISCORD_TOKEN)
