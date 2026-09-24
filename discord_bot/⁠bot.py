import discord
from discord import app_commands
import os

class MyBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.all())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()
        print(f"Logged in as {self.user}!")

bot = MyBot()

log_channels = {}

@bot.tree.command(name="audit-setup", description="اختر قناة اللوق")
@app_commands.describe(log_channel="اختر قناة اللوق الخاصة بالحدث")
async def audit_setup(interaction: discord.Interaction, log_channel: discord.TextChannel):
    log_channels[interaction.guild_id] = log_channel.id
    await interaction.response.send_message(f"تم تعيين قناة اللوق بنجاح: {log_channel.mention}", ephemeral=True)

@bot.event
async def on_message_delete(message: discord.Message):
    if message.author.bot or not message.guild:
        return
    
    channel_id = log_channels.get(message.guild.id)
    if not channel_id:
        return
    
    log_chan = message.guild.get_channel(channel_id)
    if not log_chan:
        return

    content_type = "رسالة نصية"
    
    if message.attachments:
        att = message.attachments[0]
        if att.content_type and "video" in att.content_type:
            content_type = "فيديو"
        elif att.content_type and "image" in att.content_type:
            content_type = "صورة"
        else:
            content_type = "ملف/مرفق"
    elif message.stickers:
        content_type = "ملصق"
    elif ":" in message.content and "<" in message.content:
        content_type = "إيموجي مخصص"

    embed = discord.Embed(
        title=f"تم حذف {content_type}",
        description=f"**المستخدم:** {message.author} ({message.author.mention})\n**القناة:** {message.channel.mention}",
        color=0x2b2d31
    )
    
    if message.content:
        embed.add_field(name="نص الرسالة", value=message.content, inline=False)
        
    if message.attachments:
        embed.set_image(url=message.attachments[0].url)

    await log_chan.send(embed=embed)

bot.run(os.getenv("TOKEN"))
