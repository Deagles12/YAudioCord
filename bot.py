import os
import sys
import asyncio
import shutil
import subprocess
import zipfile
from pathlib import Path
from urllib.request import urlopen

try:
    import discord
    from discord.ext import commands
except Exception as e:
    print("❌ Помилка: пакет 'discord' не встановлено або не вдалося імпортувати.")
    print("Встановіть його командою: pip install -U discord.py")
    raise SystemExit from e

try:
    import yt_dlp
except Exception as e:
    print("❌ Помилка: пакет 'yt-dlp' не встановлено або не вдалося імпортувати.")
    print("Встановіть його командою: pip install -U yt-dlp")
    raise SystemExit from e

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

BOT_PREFIX = "!"
try:
    from bot_token import TOKEN as FILE_TOKEN
except Exception:
    FILE_TOKEN = None

TOKEN = FILE_TOKEN or os.environ.get("DISCORD_TOKEN")

# ----------------------------
# Node.js runtime bootstrap
# ----------------------------
NODE_VERSION = "v22.11.0"
NODE_FOLDER = f"node-{NODE_VERSION}-win-x64"
NODE_DOWNLOAD_URL = f"https://nodejs.org/dist/{NODE_VERSION}/{NODE_FOLDER}.zip"
NODE_RUNTIME_DIR = Path(__file__).resolve().parent / ".js_runtime"


def _download_file(url: str, destination: Path):
    with urlopen(url) as response, open(destination, "wb") as output:
        shutil.copyfileobj(response, output)


def _prepend_to_path(directory: Path):
    path_str = os.environ.get("PATH", "")
    directory_str = str(directory)
    if directory_str not in path_str.split(os.pathsep):
        os.environ["PATH"] = directory_str + os.pathsep + path_str


def _find_js_runtime() -> str | None:
    for candidate in ("node", "nodejs", "bun", "deno"):
        path = shutil.which(candidate)
        if path:
            return path

    local_dir = NODE_RUNTIME_DIR / NODE_FOLDER
    local_executable = local_dir / "node.exe"
    if local_executable.exists():
        _prepend_to_path(local_dir)
        return str(local_executable)
    return None


def _ensure_js_runtime() -> str:
    runtime = _find_js_runtime()
    if runtime:
        return runtime

    NODE_RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    archive_path = NODE_RUNTIME_DIR / "node.zip"
    _download_file(NODE_DOWNLOAD_URL, archive_path)
    with zipfile.ZipFile(archive_path) as archive:
        archive.extractall(NODE_RUNTIME_DIR)
    try:
        archive_path.unlink(missing_ok=True)
    except Exception:
        pass

    runtime = _find_js_runtime()
    if runtime:
        return runtime
    raise RuntimeError(
        "❌ Не вдалося встановити Node.js автоматично."
        "Встановіть його вручну або додайте у PATH."
    )


JS_RUNTIME_PATH = _ensure_js_runtime()

# ----------------------------
# FFmpeg locator
# ----------------------------
_IMAGEIO_MODULE = None


def _load_imageio_ffmpeg():
    global _IMAGEIO_MODULE
    if _IMAGEIO_MODULE is not None:
        return _IMAGEIO_MODULE

    try:
        import imageio_ffmpeg  # type: ignore

        _IMAGEIO_MODULE = imageio_ffmpeg
        return _IMAGEIO_MODULE
    except Exception:
        pass

    python = sys.executable or "python"
    try:
        subprocess.check_call(
            [python, "-m", "pip", "install", "--quiet", "imageio-ffmpeg"]
        )
    except Exception:
        return None

    try:
        import imageio_ffmpeg  # type: ignore

        _IMAGEIO_MODULE = imageio_ffmpeg
    except Exception:
        _IMAGEIO_MODULE = None
    return _IMAGEIO_MODULE


def _locate_ffmpeg_executable() -> str:
    candidates = [
        os.environ.get("FFMPEG_EXECUTABLE"),
        os.environ.get("FFMPEG_PATH"),
        os.environ.get("FFMPEG"),
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return path

    which_path = shutil.which("ffmpeg")
    if which_path:
        return which_path

    imageio_ffmpeg = _load_imageio_ffmpeg()
    if imageio_ffmpeg is not None:
        exe_path = imageio_ffmpeg.get_ffmpeg_exe()
        if exe_path and os.path.isfile(exe_path):
            return exe_path

    raise RuntimeError(
        "Не знайдено FFmpeg. Встановіть FFmpeg і додайте його в PATH."
    )


# ----------------------------
# YTDL Source
# ----------------------------
class YTDLSource(discord.PCMVolumeTransformer):
    # Оновлено під 2025-12:
    # - js_runtimes: явно вказуємо node.exe
    # - remote_components: підтягуємо EJS з github
    # - extractor_args: player_client=default (як радить yt-dlp у warning)
    YTDL_OPTS = {
        "format": "bestaudio/best",
        "quiet": True,
        "noplaylist": True,
        "default_search": "auto",
        "source_address": "0.0.0.0",
        "extractor_args": {"youtube": {"player_client": ["default"]}},
        "js_runtimes": [f"node:{JS_RUNTIME_PATH}"],
        "remote_components": ["ejs:github"],
    }

    FFMPEG_OPTS = {
        "before_options": "-nostdin -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
        "options": "-vn",
    }

    ytdl = yt_dlp.YoutubeDL(YTDL_OPTS)
    FFMPEG_EXECUTABLE = None

    def __init__(self, source, *, data, volume=0.5):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get("title")
        self.url = data.get("url")

    @classmethod
    async def create_source(cls, url, *, loop=None):
        loop = loop or asyncio.get_event_loop()
        data = await loop.run_in_executor(
            None, lambda: cls.ytdl.extract_info(url, download=False)
        )

        if "entries" in data:
            data = data["entries"][0]

        if not data.get("url"):
            raise RuntimeError("yt-dlp не повернув прямий url для стріму.")

        filename = data["url"]
        return cls(
            discord.FFmpegPCMAudio(
                filename,
                executable=cls.get_ffmpeg_executable(),
                **cls.FFMPEG_OPTS,
            ),
            data=data,
        )

    @classmethod
    def get_ffmpeg_executable(cls) -> str:
        if cls.FFMPEG_EXECUTABLE is None:
            cls.FFMPEG_EXECUTABLE = _locate_ffmpeg_executable()
        return cls.FFMPEG_EXECUTABLE


# ----------------------------
# Music Cog
# ----------------------------
class MusicBot(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.queues = {}  # guild_id -> asyncio.Queue[YTDLSource]
        self.current = {}  # guild_id -> YTDLSource | None
        self.player_tasks = {}  # guild_id -> asyncio.Task
        self.guild_locks = {}  # guild_id -> asyncio.Lock
        self.last_voice_channel_id = {}  # guild_id -> channel_id

    def get_queue(self, guild_id: int) -> asyncio.Queue:
        if guild_id not in self.queues:
            self.queues[guild_id] = asyncio.Queue()
        return self.queues[guild_id]

    def get_lock(self, guild_id: int) -> asyncio.Lock:
        if guild_id not in self.guild_locks:
            self.guild_locks[guild_id] = asyncio.Lock()
        return self.guild_locks[guild_id]

    def ensure_player_task(self, guild_id: int):
        task = self.player_tasks.get(guild_id)
        if task is None or task.done():
            self.player_tasks[guild_id] = asyncio.create_task(self.audio_player_task(guild_id))

    async def ensure_voice_connected(self, guild: discord.Guild) -> discord.VoiceClient | None:
        vc = guild.voice_client
        if vc is not None and vc.is_connected():
            return vc

        channel_id = self.last_voice_channel_id.get(guild.id)
        if not channel_id:
            return None

        channel = guild.get_channel(channel_id)
        if channel is None:
            try:
                channel = await guild.fetch_channel(channel_id)
            except Exception:
                return None

        if not isinstance(channel, discord.VoiceChannel):
            return None

        try:
            return await channel.connect(reconnect=True)
        except Exception:
            return None

    async def audio_player_task(self, guild_id: int):
        await self.bot.wait_until_ready()
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return

        queue = self.get_queue(guild_id)

        while True:
            try:
                source = await queue.get()
            except asyncio.CancelledError:
                break

            self.current[guild_id] = source

            vc = await self.ensure_voice_connected(guild)
            if vc is None or not vc.is_connected():
                self.current[guild_id] = None
                continue

            finished = asyncio.Event()

            def _after(err):
                if err:
                    print(f"Player error: {err}")
                try:
                    self.bot.loop.call_soon_threadsafe(finished.set)
                except Exception:
                    pass

            try:
                # На випадок, якщо відвалилось між ensure і play
                if not vc.is_connected():
                    vc = await self.ensure_voice_connected(guild)
                if vc is None or not vc.is_connected():
                    self.current[guild_id] = None
                    continue

                vc.play(source, after=_after)
                await finished.wait()
            except discord.ClientException as e:
                # Тут ловимо "Not connected to voice."
                print(f"Play failed: {e}")
            except Exception as e:
                print(f"Unexpected play error: {e}")
            finally:
                self.current[guild_id] = None

    @commands.hybrid_command(name="join")
    async def join(self, ctx: commands.Context):
        if ctx.author.voice is None or ctx.author.voice.channel is None:
            await ctx.send("🔊 Спочатку зайди у голосовий канал.")
            return

        channel = ctx.author.voice.channel
        self.last_voice_channel_id[ctx.guild.id] = channel.id

        if ctx.voice_client is not None:
            await ctx.voice_client.move_to(channel)
        else:
            await channel.connect(reconnect=True)

        self.ensure_player_task(ctx.guild.id)
        await ctx.send(f"🎧 Приєднався до каналу **{channel.name}**")

    @commands.hybrid_command(name="play")
    async def play(self, ctx: commands.Context, *, url: str):
        if ctx.voice_client is None or not ctx.voice_client.is_connected():
            await ctx.invoke(self.join)

        if ctx.voice_client is None or not ctx.voice_client.is_connected():
            return

        self.last_voice_channel_id[ctx.guild.id] = ctx.voice_client.channel.id
        self.ensure_player_task(ctx.guild.id)

        lock = self.get_lock(ctx.guild.id)
        async with lock:
            msg = await ctx.send("⏳ Завантажую...")
            try:
                source = await YTDLSource.create_source(url, loop=self.bot.loop)
            except Exception as e:
                await msg.edit(content=f"❌ Сталася помилка при обробці посилання:\n```{e}```")
                return

            queue = self.get_queue(ctx.guild.id)
            await queue.put(source)
            await msg.edit(content=f"🎶 Додав у чергу: **{source.title}**")

    @commands.hybrid_command(name="skip")
    async def skip(self, ctx: commands.Context):
        if ctx.voice_client is not None and ctx.voice_client.is_playing():
            ctx.voice_client.stop()
            await ctx.send("⏭️ Трек пропущено.")
        else:
            await ctx.send("😕 Зараз нічого не грає.")

    @commands.hybrid_command(name="stop")
    async def stop(self, ctx: commands.Context):
        if ctx.voice_client is not None:
            queue = self.get_queue(ctx.guild.id)
            while not queue.empty():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            ctx.voice_client.stop()
            await ctx.send("⛔ Відтворення зупинено і чергу очищено.")
        else:
            await ctx.send("🤔 Я не в голосовому каналі.")

    @commands.hybrid_command(name="leave", aliases=["disconnect"])
    async def leave(self, ctx: commands.Context):
        # зупиняємо таску плеєра (якщо є)
        task = self.player_tasks.get(ctx.guild.id)
        if task is not None and not task.done():
            task.cancel()

        if ctx.voice_client is not None:
            await ctx.voice_client.disconnect()
            await ctx.send("👋 Відключився від голосового каналу.")
        else:
            await ctx.send("😅 Я і так не в голосовому каналі.")

    @commands.hybrid_command(name="now")
    async def now_playing(self, ctx: commands.Context):
        current = self.current.get(ctx.guild.id)
        if current:
            await ctx.send(f"🎧 Зараз грає: **{current.title}**")
        else:
            await ctx.send("🔇 Зараз нічого не грає.")

    @commands.hybrid_command(name="ping")
    async def ping(self, ctx: commands.Context):
        latency_ms = round(self.bot.latency * 1000)
        await ctx.send(f"🏓 Pong. Затримка: {latency_ms} мс")


class MyBot(commands.Bot):
    async def setup_hook(self):
        await super().setup_hook()
        await self.add_cog(MusicBot(self))
        try:
            await self.tree.sync()
        except Exception:
            pass


bot = MyBot(command_prefix=BOT_PREFIX, intents=intents)


@bot.event
async def on_ready():
    print(f"🔌 Увійшов як {bot.user} (ID: {bot.user.id})")
    print("---------")


def main():
    token = TOKEN
    if not token:
        raise RuntimeError(
            "❌ Токен бота не задано. Установіть змінну DISCORD_TOKEN "
            "або заповніть файл bot_token.py."
        )
    bot.run(token)


if __name__ == "__main__":
    main()
