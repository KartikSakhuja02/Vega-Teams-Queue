"""
cogs/vision.py
--------------
Local vision AI cog — Ollama + Gemma 4.

How it works
------------
When a user sends a message with an image attachment in a channel the bot
can see, this cog:

  1. Validates the attachment (type, size).
  2. Downloads the image in memory (never written to disk).
  3. Acquires the GPU concurrency semaphore (max 1 at a time — RTX 2050 limit).
  4. Sends image + user's question (or a default prompt) to Ollama.
  5. Edits the "🔍 Analyzing..." reply with Gemma's response.
  6. Splits the response safely at word boundaries if > 2000 chars.

No slash command is added — this responds to regular messages with images.

Existing behavior is preserved:
  - Messages without images are completely ignored.
  - Slash commands (/register, /help, etc.) are unaffected.
  - The help_ticket.py on_message is scoped to ticket channels — no conflict.

Environment variables
---------------------
  OLLAMA_BASE_URL      Ollama server URL.                 Default: http://127.0.0.1:11434
  OLLAMA_MODEL         Model for vision.                  Default: gemma4:e4b
  VISION_CHANNEL_IDS   Comma-separated channel IDs to
                       restrict vision responses to.       Default: (all channels)
  VISION_MAX_IMAGE_MB  Max image size in megabytes.        Default: 10
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import discord
from discord.ext import commands

from utils import ollama_client

log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
_MAX_BYTES = int(os.getenv("VISION_MAX_IMAGE_MB", "10")) * 1024 * 1024

_VISION_CHANNEL_IDS: set[int] = set()
_raw_ids = os.getenv("VISION_CHANNEL_IDS", "").strip()
if _raw_ids:
    for _chunk in _raw_ids.split(","):
        _chunk = _chunk.strip()
        if _chunk.isdigit():
            _VISION_CHANNEL_IDS.add(int(_chunk))

_SUPPORTED_MIME_PREFIXES = ("image/png", "image/jpeg", "image/webp", "image/gif")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_image(attachment: discord.Attachment) -> bool:
    """True when the attachment is a supported image type."""
    ct = (attachment.content_type or "").lower()
    return any(ct.startswith(p) for p in _SUPPORTED_MIME_PREFIXES)


def _split_response(text: str, limit: int = 2000) -> list[str]:
    """
    Split a long string into chunks that fit in Discord's message limit.
    Splits at word boundaries (spaces / newlines) to avoid cutting mid-word.
    """
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        # Find the last space/newline within the limit
        split_at = text.rfind(" ", 0, limit)
        if split_at == -1:
            split_at = text.rfind("\n", 0, limit)
        if split_at == -1:
            split_at = limit   # Hard cut — no space found
        chunks.append(text[:split_at].rstrip())
        text = text[split_at:].lstrip()
    return chunks


# ── Cog ───────────────────────────────────────────────────────────────────────

class VisionCog(commands.Cog, name="Vision"):
    """
    Responds to image attachments with Ollama-powered Gemma 4 vision analysis.
    Only active when OLLAMA_BASE_URL / OLLAMA_MODEL are configured.
    """

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._ollama_checked = False   # avoid spamming startup checks

    async def cog_load(self) -> None:
        """Verify Ollama connection on startup — logs warning but does not block load."""
        ok, msg = await ollama_client.check_connection()
        if ok:
            log.info("Vision AI: %s", msg)
        else:
            log.warning(
                "Vision AI: Ollama unavailable at startup (%s). "
                "Image analysis will be disabled until Ollama is reachable.",
                msg,
            )
        self._ollama_checked = True

    # ── Message listener ──────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """
        Trigger vision analysis when a user sends an image.

        Guard conditions (all must pass before any work is done):
          1. Not a bot message
          2. Has at least one supported image attachment
          3. Channel is in VISION_CHANNEL_IDS (if that var is set)
          4. Image size ≤ VISION_MAX_IMAGE_MB
          5. Ollama is reachable (checked lazily per request)
        """
        # 1. Skip bots (including ourselves)
        if message.author.bot:
            return

        # 2. Find valid image attachments
        images = [a for a in message.attachments if _is_image(a)]
        if not images:
            return   # No image → existing behavior unchanged

        # 3. Channel filter (optional)
        if _VISION_CHANNEL_IDS and message.channel.id not in _VISION_CHANNEL_IDS:
            return

        # Only process the first image
        attachment = images[0]

        # 4. Size check
        if attachment.size > _MAX_BYTES:
            max_mb = _MAX_BYTES // (1024 * 1024)
            await message.reply(
                f"⚠️ Image too large ({attachment.size // (1024*1024)} MB). "
                f"Maximum allowed size is {max_mb} MB.",
                mention_author=False,
            )
            return

        # 5. Determine prompt
        prompt = message.content.strip() or ollama_client.DEFAULT_PROMPT

        # Send thinking indicator (will be edited with the final result)
        thinking_msg = await message.reply(
            "🔍 Analyzing...",
            mention_author=False,
        )

        # Acquire GPU concurrency semaphore — 1 inference at a time
        async with ollama_client.inference_semaphore:
            try:
                image_bytes = await attachment.read()
            except Exception as exc:
                log.error("Failed to download Discord attachment: %s", exc)
                await thinking_msg.edit(content="⚠️ Failed to download the image. Please try again.")
                return

            try:
                answer = await ollama_client.analyze_image(image_bytes, prompt)
            except RuntimeError as exc:
                # Log technical details internally; show user a clean message
                log.error("Ollama vision inference failed: %s", exc)
                await thinking_msg.edit(
                    content=(
                        "⚠️ Vision AI is currently unavailable. "
                        "Please try again in a moment."
                    )
                )
                return
            except Exception as exc:
                log.error("Unexpected error during vision inference: %s", exc, exc_info=True)
                await thinking_msg.edit(
                    content="⚠️ An unexpected error occurred during image analysis."
                )
                return

        # Send response (split if > 2000 chars)
        chunks = _split_response(answer)
        if not chunks:
            await thinking_msg.edit(content="⚠️ Received an empty response from the model.")
            return

        # Edit the thinking message with the first chunk
        await thinking_msg.edit(content=chunks[0])

        # Send remaining chunks as follow-up messages
        for chunk in chunks[1:]:
            await message.channel.send(chunk)

        log.info(
            "Vision: analyzed image from %s in #%s (%d chunk(s))",
            message.author,
            getattr(message.channel, "name", message.channel.id),
            len(chunks),
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(VisionCog(bot))
