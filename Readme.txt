import discord
from discord import app_commands
from aiohttp import web
import asyncio
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("voice-bot")

# ─── Session Storage ──────────────────────────────────────────────────────────

@dataclass
class VoiceSession:
    token: str
    guild_id: int
    channel_id: int
    client: discord.Client
    voice_client: discord.VoiceClient
    task: asyncio.Task


sessions: dict[str, VoiceSession] = {}


def session_key(guild_id: int, channel_id: int) -> str:
    return f"{guild_id}:{channel_id}"


async def _cleanup(sess: VoiceSession) -> None:
    try:
        if sess.voice_client.is_connected():
            await sess.voice_client.disconnect(force=True)
    except Exception:
        pass
    try:
        await sess.client.close()
    except Exception:
        pass
    sess.task.cancel()


async def join_by_token(token: str, guild_id: int, channel_id: int) -> None:
    key = session_key(guild_id, channel_id)

    if key in sessions:
        await _cleanup(sessions.pop(key))

    guest_intents = discord.Intents.default()
    guest_intents.voice_states = True
    client = discord.Client(intents=guest_intents)

    ready_event = asyncio.Event()

    @client.event
    async def on_ready():
        ready_event.set()
        logger.info(f"Guest client ready: {client.user}")

    @client.event
    async def on_voice_state_update(
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        if member != client.user:
            return
        if before.channel and not after.channel:
            logger.warning(f"Disconnected from voice — reconnecting: {key}")
            asyncio.create_task(_rejoin(key))

    task = asyncio.create_task(client.start(token))

    try:
        await asyncio.wait_for(ready_event.wait(), timeout=30.0)
    except asyncio.TimeoutError:
        task.cancel()
        await client.close()
        raise ValueError("تعذّر تسجيل الدخول — تأكد من صحة التوكن.")

    try:
        guild = await client.fetch_guild(guild_id)
    except discord.NotFound:
        task.cancel()
        await client.close()
        raise ValueError(f"لم يتم العثور على السيرفر (ID: {guild_id}). تأكد أن البوت موجود فيه.")

    try:
        channel = await guild.fetch_channel(channel_id)
    except discord.NotFound:
        task.cancel()
        await client.close()
        raise ValueError(f"لم يتم العثور على الروم (ID: {channel_id}).")

    if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
        task.cancel()
        await client.close()
        raise ValueError("الـ ID المدخل ليس روماً صوتياً.")

    vc = await channel.connect(self_deaf=True, self_mute=True)
    sessions[key] = VoiceSession(token, guild_id, channel_id, client, vc, task)
    logger.info(f"Joined voice — guild={guild_id} channel={channel_id}")


async def _rejoin(key: str, attempt: int = 1) -> None:
    await asyncio.sleep(3)
    if key not in sessions:
        return
    sess = sessions[key]
    try:
        await join_by_token(sess.token, sess.guild_id, sess.channel_id)
        logger.info(f"Rejoined: {key}")
    except Exception as e:
        logger.error(f"Rejoin failed ({attempt}): {e}")
        if attempt < 5:
            await asyncio.sleep(10 * attempt)
            asyncio.create_task(_rejoin(key, attempt + 1))


def leave_session(guild_id: int, channel_id: int) -> bool:
    key = session_key(guild_id, channel_id)
    if key not in sessions:
        return False
    sess = sessions.pop(key)
    asyncio.create_task(_cleanup(sess))
    logger.info(f"Left voice — {key}")
    return True


def leave_guild(guild_id: int) -> int:
    removed = 0
    for key in list(sessions):
        g, _ = key.split(":")
        if int(g) == guild_id:
            sess = sessions.pop(key)
            asyncio.create_task(_cleanup(sess))
            removed += 1
    return removed


# ─── Modal ────────────────────────────────────────────────────────────────────

class AddVoiceModal(discord.ui.Modal, title="🎙️ ثبّت حسابك في الفويس 24/7"):
    token_input = discord.ui.TextInput(
        label="توكن حسابك الشخصي في Discord",
        placeholder="توكن حسابك — مو توكن بوت",
        min_length=20,
        style=discord.TextStyle.short,
    )
    guild_id_input = discord.ui.TextInput(
        label="ID السيرفر (Guild ID)",
        placeholder="مثال: 123456789012345678",
        min_length=17,
        max_length=20,
        style=discord.TextStyle.short,
    )
    channel_id_input = discord.ui.TextInput(
        label="ID الروم الصوتي (Channel ID)",
        placeholder="مثال: 987654321098765432",
        min_length=17,
        max_length=20,
        style=discord.TextStyle.short,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        token       = self.token_input.value.strip()
        guild_str   = self.guild_id_input.value.strip()
        channel_str = self.channel_id_input.value.strip()

        if not guild_str.isdigit() or not channel_str.isdigit():
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ ID غير صحيح",
                    description="ID السيرفر والروم يجب أن يكونا أرقاماً فقط.",
                    color=0xED4245,
                ),
                ephemeral=True,
            )
            return

        try:
            await join_by_token(token, int(guild_str), int(channel_str))
            embed = discord.Embed(
                title="✅ حسابك الآن في الفويس 24/7",
                description=(
                    "**حسابك** ثابت في الروم الصوتي بدون توقف ✅\n\n"
                    f"🏠 السيرفر: `{guild_str}`\n"
                    f"🔊 الروم: `{channel_str}`\n\n"
                    "يرجع تلقائياً إذا انقطع."
                ),
                color=0x57F287,
            )
            embed.set_footer(text="استخدم /leave_voice للخروج")
            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as exc:
            logger.error(f"add_voice modal error: {exc}")
            embed = discord.Embed(
                title="❌ فشل الاتصال",
                description=(
                    f"**سبب الخطأ:**\n{exc}\n\n"
                    "**تأكد من:**\n"
                    "• صحة التوكن\n"
                    "• أن البوت موجود في السيرفر\n"
                    "• أن ID الروم صحيح"
                ),
                color=0xED4245,
            )
            await interaction.followup.send(embed=embed, ephemeral=True)


# ─── Bot Setup ────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot  = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)


# ─── Slash Commands ───────────────────────────────────────────────────────────

@tree.command(name="add_voice", description="ثبّت حسابك الشخصي في روم صوتي 24/7")
async def cmd_add_voice(interaction: discord.Interaction):
    await interaction.response.send_modal(AddVoiceModal())


@tree.command(name="leave_voice", description="أطلع البوت من الروم الصوتي")
async def cmd_leave_voice(interaction: discord.Interaction):
    removed = leave_guild(interaction.guild_id) if interaction.guild_id else 0
    if removed:
        embed = discord.Embed(
            title="👋 خرج من الفويس",
            description=f"تم إخراج البوت من **{removed}** روم صوتي.",
            color=0x57F287,
        )
    else:
        embed = discord.Embed(
            title="❌ البوت مو في فويس",
            description="البوت مو موجود في أي روم صوتي في هذا السيرفر.",
            color=0xED4245,
        )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="status", description="اعرض الفويس اللي البوت فيه حالياً")
async def cmd_status(interaction: discord.Interaction):
    if not sessions:
        embed = discord.Embed(
            title="📊 الحالة",
            description="البوت مو في أي فويس حالياً.",
            color=0x5865F2,
        )
    else:
        lines = [
            f"🔊 السيرفر `{s.guild_id}` — الروم `{s.channel_id}`"
            for s in sessions.values()
        ]
        embed = discord.Embed(
            title=f"📊 الحالة — {len(sessions)} اتصال نشط",
            description="\n".join(lines),
            color=0x57F287,
        )
        embed.set_footer(text="يشتغل 24/7")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="help", description="اعرض جميع الأوامر المتاحة")
async def cmd_help(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🤖 Voice Bot — الأوامر",
        description="البوت يثبت حسابك في الفويس **24/7** بدون توقف.",
        color=0x5865F2,
    )
    embed.add_field(
        name="/add_voice",
        value="يفتح نموذج يطلب التوكن + ID السيرفر + ID الروم ثم يدخل الفويس.",
        inline=False,
    )
    embed.add_field(name="/leave_voice", value="يطلع البوت من الفويس.", inline=False)
    embed.add_field(name="/status",      value="يعرض الاتصالات النشطة.", inline=False)
    embed.add_field(name="/help",        value="يعرض هذه القائمة.",      inline=False)
    embed.set_footer(text="البوت يشتغل 24/7 — يرجع تلقائياً إذا انقطع")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ─── DM Handler ───────────────────────────────────────────────────────────────

HELP_EMBED_DM = lambda: discord.Embed(
    title="🤖 Voice Bot — الأوامر",
    description="البوت يثبت حسابك في الفويس **24/7** بدون توقف.",
    color=0x5865F2,
).add_field(
    name="📌 في السيرفر",
    value=(
        "`/add_voice` — يفتح نموذج: توكن + ID السيرفر + ID الروم\n"
        "`/leave_voice` — يطلع البوت\n"
        "`/status` — الاتصالات النشطة\n"
        "`/help` — الأوامر"
    ),
    inline=False,
).add_field(
    name="💬 في الخاص",
    value="`help` | `status` | `ping` | `source`",
    inline=False,
).set_footer(text="البوت يشتغل 24/7")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if not isinstance(message.channel, discord.DMChannel):
        return

    cmd = message.content.strip().lower().lstrip("!")

    # ── help ──
    if cmd in ("help", "مساعدة"):
        await message.reply(embed=HELP_EMBED_DM())
        return

    # ── status ──
    if cmd in ("status", "الحالة"):
        if not sessions:
            embed = discord.Embed(
                title="📊 الحالة",
                description="البوت مو في أي فويس حالياً.",
                color=0xED4245,
            )
        else:
            lines = [f"🔊 `{s.guild_id}` → `{s.channel_id}`" for s in sessions.values()]
            embed = discord.Embed(
                title=f"📊 الحالة — {len(sessions)} اتصال نشط",
                description="\n".join(lines),
                color=0x57F287,
            )
        await message.reply(embed=embed)
        return

    # ── ping ──
    if cmd == "ping":
        latency = round(bot.latency * 1000)
        embed = discord.Embed(
            title="🏓 Pong!",
            description=f"البوت شغال ✅\nالاستجابة: **{latency}ms**",
            color=0x57F287,
        )
        await message.reply(embed=embed)
        return

    # ── source ──
    if cmd in ("source", "السورس"):
        main_path = Path(__file__).resolve()
        if main_path.exists():
            await message.reply(
                embed=discord.Embed(
                    title="📦 الملف المصدري",
                    description="يتم إرسال `main.py`...",
                    color=0x5865F2,
                )
            )
            with open(main_path, "rb") as f:
                await message.channel.send(file=discord.File(f, filename="main.py"))
            await message.channel.send(
                embed=discord.Embed(
                    title="✅ تم الإرسال",
                    description="تم إرسال `main.py` بنجاح.",
                    color=0x57F287,
                )
            )
        else:
            await message.reply("⚠️ لم يتم العثور على الملف.")
        return

    # ── default ──
    await message.reply(
        embed=discord.Embed(
            description="مرحباً! 👋\nاكتب `help` لعرض الأوامر.\nلإضافة البوت للفويس استخدم `/add_voice` في السيرفر.",
            color=0x5865F2,
        )
    )


# ─── Ready ────────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    await tree.sync()
    logger.info(f"Bot online: {bot.user} | Synced slash commands")
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.watching,
            name="voice channels 24/7",
        )
    )


# ─── HTTP Health Server ───────────────────────────────────────────────────────

async def start_health_server():
    port = int(os.environ.get("PORT", 8080))

    async def health(_request):
        return web.Response(
            text='{"status":"ok"}',
            content_type="application/json",
        )

    app = web.Application()
    app.router.add_get("/api/healthz", health)
    app.router.add_get("/healthz", health)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Health server listening on port {port}")


# ─── Entry Point ──────────────────────────────────────────────────────────────

async def _run():
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        logger.error("DISCORD_BOT_TOKEN is not set!")
        sys.exit(1)

    await start_health_server()

    async with bot:
        await bot.start(token, reconnect=True)


if __name__ == "__main__":
    asyncio.run(_run())
