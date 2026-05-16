import discord
from discord.http import Route
from aiohttp import web
import asyncio
import logging
import os
import sys
import types
from dataclasses import dataclass
from pathlib import Path

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("voice-bot")

# ─── Voice Sessions ───────────────────────────────────────────────────────────

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

    # discord.py يضيف "Bot " قبل كل توكن تلقائياً
    # نتجاوز هذا عشان يشتغل مع توكن الحساب الشخصي
    async def _user_static_login(self, raw_token: str):
        self.token = raw_token
        data = await self.request(Route("GET", "/users/@me"))
        return data

    client.http.static_login = types.MethodType(_user_static_login, client.http)

    ready_event = asyncio.Event()

    @client.event
    async def on_ready():
        ready_event.set()
        logger.info(f"User session ready: {client.user}")

    @client.event
    async def on_voice_state_update(member, before, after):
        if member != client.user:
            return
        if before.channel and not after.channel:
            logger.warning(f"Disconnected — reconnecting: {key}")
            asyncio.create_task(_rejoin(key))

    task = asyncio.create_task(client.start(token))

    try:
        await asyncio.wait_for(ready_event.wait(), timeout=30.0)
    except asyncio.TimeoutError:
        task.cancel()
        await client.close()
        raise ValueError("تعذّر تسجيل الدخول — تأكد من صحة التوكن الشخصي.")
    except discord.LoginFailure:
        task.cancel()
        await client.close()
        raise ValueError("التوكن غير صحيح — تأكد أنك نسخت توكن حسابك الشخصي كاملاً.")

    try:
        guild = await client.fetch_guild(guild_id)
    except discord.NotFound:
        task.cancel()
        await client.close()
        raise ValueError(f"لم يتم العثور على السيرفر (ID: {guild_id}).")
    except discord.Forbidden:
        task.cancel()
        await client.close()
        raise ValueError(f"الحساب مو عضو في هذا السيرفر (ID: {guild_id}).")

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


def leave_guild(guild_id: int) -> int:
    removed = 0
    for key in list(sessions):
        g, _ = key.split(":")
        if int(g) == guild_id:
            sess = sessions.pop(key)
            asyncio.create_task(_cleanup(sess))
            removed += 1
    return removed


# ─── Multi-step Add Flow (per user) ───────────────────────────────────────────

@dataclass
class AddFlow:
    state: str = "token"   # token → guild → channel
    token: str = ""
    guild_id: str = ""


pending_flows: dict[int, AddFlow] = {}   # user_id → flow


# ─── Bot ──────────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True

bot = discord.Client(intents=intents)


def _help_embed() -> discord.Embed:
    e = discord.Embed(
        title="🎙️ Voice Bot — الأوامر",
        description=(
            "كل شيء يشتغل من **الخاص** مباشرة.\n"
            "مو محتاج تضيفني للسيرفر أبداً."
        ),
        color=0x5865F2,
    )
    e.add_field(
        name="▶️  add",
        value="يبدأ عملية التثبيت — يسألك خطوة خطوة عن التوكن، ID السيرفر، ID الروم.",
        inline=False,
    )
    e.add_field(
        name="⏹️  leave <guild_id>",
        value="يوقف التثبيت ويطلع من الفويس.",
        inline=False,
    )
    e.add_field(
        name="📊  status",
        value="يعرض الاتصالات النشطة حالياً.",
        inline=False,
    )
    e.add_field(
        name="❓  help",
        value="يعرض هذه القائمة.",
        inline=False,
    )
    e.set_footer(text="يشتغل 24/7 — يرجع تلقائياً لو انقطع")
    return e


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if not isinstance(message.channel, discord.DMChannel):
        return

    user_id = message.author.id
    text    = message.content.strip()
    cmd     = text.lower().lstrip("!/")

    # ── خطوات add الجارية ─────────────────────────────────────────────────────
    if user_id in pending_flows:
        flow = pending_flows[user_id]

        if cmd in ("cancel", "إلغاء"):
            del pending_flows[user_id]
            await message.channel.send(embed=discord.Embed(
                title="🚫 تم الإلغاء", color=0xED4245))
            return

        if flow.state == "token":
            flow.token  = text
            flow.state  = "guild"
            try:
                await message.delete()
            except Exception:
                pass
            await message.channel.send(embed=discord.Embed(
                title="2️⃣ ايدي السيرفر (Guild ID)",
                description="الآن أرسل **ID السيرفر** اللي حسابك فيه.",
                color=0x5865F2,
            ))
            return

        if flow.state == "guild":
            if not text.isdigit():
                await message.channel.send("❌ ID السيرفر أرقام فقط — حاول مرة ثانية.")
                return
            flow.guild_id = text
            flow.state    = "channel"
            await message.channel.send(embed=discord.Embed(
                title="3️⃣ ايدي الروم الصوتي (Channel ID)",
                description="أرسل **ID الروم الصوتي** اللي تبي تثبت فيه.",
                color=0x5865F2,
            ))
            return

        if flow.state == "channel":
            if not text.isdigit():
                await message.channel.send("❌ ID الروم أرقام فقط — حاول مرة ثانية.")
                return

            channel_id = int(text)
            guild_id   = int(flow.guild_id)
            token      = flow.token
            del pending_flows[user_id]

            wait_msg = await message.channel.send(embed=discord.Embed(
                title="⏳ جاري الاتصال...",
                description="يتم الدخول للروم الصوتي، انتظر لحظة.",
                color=0xFEE75C,
            ))
            try:
                await join_by_token(token, guild_id, channel_id)
                await wait_msg.edit(embed=discord.Embed(
                    title="✅ حسابك الآن في الفويس 24/7",
                    description=(
                        f"🏠 السيرفر: `{guild_id}`\n"
                        f"🔊 الروم: `{channel_id}`\n\n"
                        "يرجع تلقائياً لو انقطع.\n"
                        f"للإيقاف أرسل: `leave {guild_id}`"
                    ),
                    color=0x57F287,
                ))
            except Exception as exc:
                logger.error(f"join error: {exc}")
                await wait_msg.edit(embed=discord.Embed(
                    title="❌ فشل الاتصال",
                    description=str(exc),
                    color=0xED4245,
                ))
            return

    # ── أوامر رئيسية ──────────────────────────────────────────────────────────

    if cmd == "add":
        pending_flows[user_id] = AddFlow()
        await message.channel.send(embed=discord.Embed(
            title="1️⃣ توكن حسابك الشخصي",
            description=(
                "أرسل **توكن حسابك الشخصي** في Discord.\n\n"
                "⚠️ مو توكن بوت — توكن **حسابك الشخصي أنت**.\n"
                "سيتم حذف رسالة التوكن تلقائياً للخصوصية.\n\n"
                "للإلغاء أرسل `cancel`."
            ),
            color=0x5865F2,
        ))
        return

    if cmd.startswith("leave"):
        parts = text.split()
        if len(parts) >= 2 and parts[1].isdigit():
            removed = leave_guild(int(parts[1]))
            if removed:
                await message.channel.send(embed=discord.Embed(
                    title="👋 تم الخروج من الفويس ✅", color=0x57F287))
            else:
                await message.channel.send(embed=discord.Embed(
                    title="❌", description="مو موجود في فويس في هذا السيرفر.", color=0xED4245))
        else:
            if not sessions:
                await message.channel.send(embed=discord.Embed(
                    title="❌", description="مو موجود في أي فويس حالياً.", color=0xED4245))
            else:
                lines = [f"• `{s.guild_id}` ← الروم `{s.channel_id}`" for s in sessions.values()]
                await message.channel.send(embed=discord.Embed(
                    title="⚠️ حدد السيرفر",
                    description="استخدم: `leave <guild_id>`\n\n**الاتصالات النشطة:**\n" + "\n".join(lines),
                    color=0xFEE75C,
                ))
        return

    if cmd in ("status", "الحالة"):
        if not sessions:
            embed = discord.Embed(
                title="📊 الحالة",
                description="مو في أي فويس حالياً.",
                color=0xED4245,
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
        await message.channel.send(embed=embed)
        return

    if cmd == "ping":
        latency = round(bot.latency * 1000)
        await message.channel.send(embed=discord.Embed(
            title="🏓 Pong!",
            description=f"البوت شغال ✅\nالاستجابة: **{latency}ms**",
            color=0x57F287,
        ))
        return

    if cmd in ("help", "مساعدة", "?", "start", "هلب"):
        await message.channel.send(embed=_help_embed())
        return

    # default
    await message.channel.send(embed=discord.Embed(
        description="👋 أهلاً! أرسل `help` لعرض الأوامر، أو `add` للبدء.",
        color=0x5865F2,
    ))


@bot.event
async def on_ready():
    logger.info(f"Bot online: {bot.user}")
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.watching,
            name="DMs | اكتب help",
        )
    )


# ─── HTTP Health ───────────────────────────────────────────────────────────────

async def start_health_server():
    port = int(os.environ.get("PORT", 8080))

    async def health(_req):
        return web.Response(text='{"status":"ok"}', content_type="application/json")

    app = web.Application()
    app.router.add_get("/api/healthz", health)
    app.router.add_get("/healthz", health)

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    logger.info(f"Health server on port {port}")


# ─── Entry ────────────────────────────────────────────────────────────────────

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
