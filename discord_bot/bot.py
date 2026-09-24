"""Discord bot with localized commands and deletion recovery."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import traceback
from datetime import UTC, datetime

import discord
from discord import app_commands
from discord.ext import tasks

from discord_bot import media, storage
from discord_bot.audit_logging import (
    find_deleter,
    post_deleted_media,
    post_purge_report,
)
from discord_bot.localization import text


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("discord_bot")


class StarterBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True

        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.capture_tasks: dict[int, asyncio.Task[None]] = {}

    async def setup_hook(self) -> None:
        storage.initialize()
        self.cleanup_expired_messages.start()

        guild_id = os.getenv("DISCORD_GUILD_ID")
        if guild_id:
            try:
                guild = discord.Object(id=int(guild_id))
                self.tree.copy_global_to(guild=guild)
                commands = await self.tree.sync(guild=guild)
                logger.info(
                    "Synced %d slash commands to server %s.",
                    len(commands),
                    guild_id,
                )
            except ValueError as exc:
                traceback.print_exc()
                raise ValueError(
                    "DISCORD_GUILD_ID must be a numeric Discord server ID."
                ) from exc
        else:
            commands = await self.tree.sync()
            logger.info("Synced %d global slash commands.", len(commands))

    async def on_ready(self) -> None:
        if self.user:
            logger.info(
                "Connected to Discord as %s (ID: %s).",
                self.user,
                self.user.id,
            )

    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None:
            return

        storage.save_message(
            {
                "message_id": message.id,
                "guild_id": message.guild.id,
                "channel_id": message.channel.id,
                "author_id": message.author.id,
                "author_name": str(message.author),
                "content": message.content or "",
                "created_at": message.created_at.isoformat(),
                "attachments": [
                    {
                        "filename": attachment.filename,
                        "content_type": attachment.content_type or "",
                        "size": attachment.size,
                        "url": attachment.url,
                    }
                    for attachment in message.attachments
                ],
            }
        )

        task = asyncio.create_task(media.capture_message_media(message))
        self.capture_tasks[message.id] = task
        try:
            await task
        except Exception:
            traceback.print_exc()
            logger.exception("Could not finish archiving message %s.", message.id)
        finally:
            self.capture_tasks.pop(message.id, None)

    async def _wait_for_capture(self, message_id: int) -> None:
        task = self.capture_tasks.get(message_id)
        if task is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=30)
        except (TimeoutError, Exception):
            traceback.print_exc()
            logger.warning(
                "Media capture did not finish before deletion of message %s.",
                message_id,
            )

    async def on_raw_message_delete(
        self, payload: discord.RawMessageDeleteEvent
    ) -> None:
        if payload.guild_id is None:
            return

        occurred_at = datetime.now(UTC)
        guild = self.get_guild(payload.guild_id)
        if guild is None:
            return

        await self._wait_for_capture(payload.message_id)

        message = storage.get_message(payload.guild_id, payload.message_id)
        if message is None:
            return

        assets = storage.get_assets(payload.guild_id, payload.message_id)
        media_assets = [
            asset
            for asset in assets
            if asset["kind"] in {"image", "video", "sticker", "emoji"}
        ]
        if not media_assets:
            return

        deleter = await find_deleter(
            guild,
            payload.channel_id,
            author_id=int(message["author_id"]),
            occurred_at=occurred_at,
        )

        await post_deleted_media(
            guild,
            message,
            media_assets,
            deleter,
            unknown_deleter_key="unknown_author_delete",
        )

    async def on_raw_bulk_message_delete(
        self, payload: discord.RawBulkMessageDeleteEvent
    ) -> None:
        if payload.guild_id is None:
            return

        occurred_at = datetime.now(UTC)
        guild = self.get_guild(payload.guild_id)
        if guild is None:
            return

        message_ids = list(payload.message_ids)
        await asyncio.gather(
            *(self._wait_for_capture(message_id) for message_id in message_ids)
        )

        records = storage.get_messages(
            payload.guild_id,
            payload.channel_id,
            payload.message_ids,
        )

        deleter = await find_deleter(
            guild,
            payload.channel_id,
            bulk_count=len(message_ids),
            occurred_at=occurred_at,
        )

        await post_purge_report(
            guild,
            payload.channel_id,
            message_ids,
            records,
            deleter,
        )

        for message_id in message_ids:
            record = records.get(str(message_id))
            if record is None:
                continue

            assets = [
                asset
                for asset in storage.get_assets(payload.guild_id, message_id)
                if asset["kind"] in {"image", "video", "sticker", "emoji"}
            ]
            if assets:
                await post_deleted_media(guild, record, assets, deleter)

    @tasks.loop(hours=24)
    async def cleanup_expired_messages(self) -> None:
        paths, deleted_count = storage.expire_old_messages()
        media_root = storage.MEDIA_DIR.resolve()
        removed_files = 0
        for raw_path in paths:
            try:
                path = os.path.realpath(raw_path)
                if os.path.commonpath((str(media_root), path)) == str(media_root):
                    os.remove(path)
                    removed_files += 1
            except (OSError, ValueError):
                traceback.print_exc()
                logger.warning(
                    "Could not remove an expired archived media file."
                )

        logger.info(
            "Retention cleanup removed %d messages and %d media files.",
            deleted_count,
            removed_files,
        )


bot = StarterBot()


@bot.tree.command(
    name="ping",
    description="Check whether the bot is responding.",
)
async def ping(interaction: discord.Interaction) -> None:
    latency_ms = round(bot.latency * 1000)
    await interaction.response.send_message(
        f"Pong! Gateway latency: **{latency_ms} ms**"
    )


@bot.tree.command(
    name="hello",
    description="Get a greeting from the bot.",
)
async def hello(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(
        f"Hello, {interaction.user.mention}!"
    )


@bot.tree.command(
    name="serverinfo",
    description="Show information about this server.",
)
async def serverinfo(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(
            text("ar", "not_in_server"), ephemeral=True
        )
        return

    settings = storage.get_guild_settings(guild.id)
    language = settings["language"] if settings else "ar"

    embed = discord.Embed(
        title=text(language, "server_title"),
        color=discord.Color.blurple(),
    )
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)

    embed.add_field(
        name=text(language, "members"),
        value=str(guild.member_count or text(language, "unknown")),
    )
    embed.add_field(
        name=text(language, "created"),
        value=discord.utils.format_dt(guild.created_at, style="D"),
    )
    if guild.owner:
        embed.add_field(
            name=text(language, "owner"),
            value=guild.owner.mention,
            inline=False,
        )

    await interaction.response.send_message(embed=embed)


@bot.tree.command(
    name="about",
    description="Learn what this bot can do.",
)
async def about(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(
        "Try `/ping`, `/hello`, `/serverinfo`, and `/audit-setup`."
    )


@bot.tree.command(
    name="audit-setup",
    description="Choose the audit log channel and language.",
)
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(
    log_channel="Where audit reports and purge transcripts should be sent.",
    language="The language used in embeds for this server.",
)
@app_commands.choices(
    language=[
        app_commands.Choice(name="العربية", value="ar"),
        app_commands.Choice(name="English", value="en"),
    ]
)
async def audit_setup(
    interaction: discord.Interaction,
    log_channel: discord.TextChannel,
    language: app_commands.Choice[str],
) -> None:
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(
            text("ar", "not_in_server"), ephemeral=True
        )
        return

    storage.set_guild_settings(guild.id, log_channel.id, language.value)
    localized_language = text(
        language.value,
        "language_ar" if language.value == "ar" else "language_en",
    )

    await interaction.response.send_message(
        text(
            language.value,
            "setup_done",
            channel=log_channel.mention,
            language=localized_language,
        ),
        ephemeral=True,
    )


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
) -> None:
    settings = (
        storage.get_guild_settings(interaction.guild_id)
        if interaction.guild_id
        else None
    )
    language = settings["language"] if settings else "ar"

    if isinstance(error, app_commands.MissingPermissions):
        message = text(language, "admin_only")
    else:
        traceback.print_exception(type(error), error, error.__traceback__)
        logger.error("Slash command failed: %s", error)
        message = text(language, "command_error")

    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


def main() -> None:
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        logger.error(
            "DISCORD_BOT_TOKEN is missing. Add your bot token as a Replit Secret, then restart."
        )
        sys.exit(1)

    try:
        bot.run(token, log_handler=None)
    except discord.LoginFailure:
        traceback.print_exc()
        logger.error(
            "Discord rejected the bot token. Check the DISCORD_BOT_TOKEN secret."
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
