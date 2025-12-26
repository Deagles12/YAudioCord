import os
import sys
import asyncio
import logging
import shutil
import subprocess
import zipfile
from pathlib import Path
from urllib.request import urlopen, Request

import discord
from discord.ext import commands

import yt_dlp


# =========================
# Logging
# =========================
discord.utils.setup_logging(level=logging.INFO)
log = logging.getLogger("yaudiocord")


# =========================
# Discord intents / token
# =========================
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

BOT_PREFIX = "!"

try:
    from bot_token import TOKEN as FILE_TOKEN  # type: ignore
except Exception:
    FILE_TOKEN = None

TOKEN = FILE_TOKEN or os.environ.get("DISCORD_TOKEN")


# =========================
# Optional: Auto-install JS runtime (Node)
# =========================
NODE_VERSION = "v22.11.0"
NODE_FOLDER = f"node-{NODE_VERSION}-win-x64"
NODE_DOWNLOAD_URL = f"https://nodejs.org/dist/{NODE_VERSION}/{NODE_FOLDER}.zip"
NODE_RUNTIME_DIR = Path(__file__).resolve().parent / ".js_runtime"


def _download_file(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    req = Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
    )
    with urlopen(req) as response, open(destination, "wb") as output:
        shutil.copyfileobj(response, output)


def _prepend_to_path(directory: Path) -> None:
    path_str = os.environ.get("PATH", "")
    directory_str = str(directory)
    if directory_str not in path_str.split(os.pathsep):
        os.environ["PATH"] = directory_str + os.pathsep + path_str


def _find_js_runtime() -> str | None:
    for candidate in ("deno", "node", "nodejs", "bun", "qjs", "quickjs"):
        path = shutil.which(candidate)
        if path:
            return path

    local_dir = NODE_RUNTIME_DIR / NODE_FOLDER
    local_executable = local_dir / "node.exe"
    if local_executable.exists():
        _prepend_to_path(local_dir)
        return str(local_executable)

    return None


def _ensure_js_runtime() -> str | None:
    runtime = _find_js_runtime()
    if runtime:
        return runtime

    # Try to auto-download Node on Windows
    try:
        NODE_RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        archive_path = NODE_RUNTIME_DIR / "node.zip"
        log.info("JS runtime not found. Downloading Node.js to %s ...", NODE_RUNTIME_DIR)
        _download_file(NODE_DOWNLOAD_URL, archive_path)
        with zipfile.ZipFile(archive_path) as archive:
            archive.extractall(NODE_RUNTIME_DIR)
        try:
            archive_path.unlink(missing_ok=True)
        except Exception:
            pass

        runtime = _find_js_runtime()
        if runtime:
            log.info("Node.js runtime ready: %s", runtime)
            return runtime
    except Exception:
        log.exception("Failed to auto-install Node.js runtime.")

    return None


JS_RUNTIME_PATH = _ensure_js_runtime()


# =========================
# Ensure FFmpeg
# =========================
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
        subprocess.check_call([python, "-m", "pip", "install", "--quiet", "imageio-ffmpeg"])
    except Exception:
        return None

    try:
        import imageio_ffmpeg  # type: ignore

        _IMAGEIO_MODULE = imageio_ffmpeg
        return _IMAGEIO_MODULE
    except Exception:
        return None


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

    raise RuntimeError("❌ Не знайдено FFmpeg. Встановіть FFmpeg і додайте його в PATH.")


# =========================
# Ensure yt-dlp EJS scripts
# =========================
def _ensure_ytdlp_default_deps() -> bool:
    """
    yt-dlp for YouTube now needs:
      - JS runtime
      - EJS challenge solver scripts (yt-dlp-ejs)
    For pip installs, recommended path is: pip install -U "yt-dlp[default]"
    If we cannot ensure that, we will fallback to remote_components=ejs:github.
    (See yt-dlp wiki EJS guide.)
    """
    try:
        import yt_dlp_ejs  # type: ignore  # noqa: F401

        return True
    except Exception:
        pass

    python = sys.executable or "python"
    try:
        log.info('Installing yt-dlp default deps: pip install -U "yt-dlp[default]" ...')
        subprocess.check_call([python, "-m", "pip", "install", "-U", "yt-dlp[default]"])
    except Exception:
        log.warning('Could not install "yt-dlp[default]". Will fallback to remote_components=ejs:github.')
        return False

    try:
        import yt_dlp_ejs  # type: ignore  # noqa: F401

        return True
    except Exception:
        log.warning("yt_dlp_ejs still not importable after installation attempt.")
        return False


HAS_EJS_PACKAGE = _ensure_ytdlp_default_deps()


# =========================
# YTDL audio source
# =========================
class YTDLSource(discord.PCMVolumeTransformer):
    FFMPEG_EXECUTABLE: str | None = None

    # FFmpeg: more stable reconnect + no stdin.
    FFMPEG_BEFORE = "-nostdin -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
    FFMPEG_OPTS = {
        "before_options": FFMPEG_BEFORE,
        "options": "-vn -loglevel warning",
    }

    @classmethod
    def get_ffmpeg_executable(cls) -> str:
        if cls.FFMPEG_EXECUTABLE is None:
            cls.FFMPEG_EXECUTABLE = _locate_ffmpeg_executable()
        return cls.FFMPEG_EXECUTABLE

    @staticmethod
    def _build_ytdl_opts() -> dict:
        # Core opts
        opts: dict = {
            "format": "bestaudio/best",
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "default_search": "auto",
            "source_address": "0.0.0.0",
            "socket_timeout": 30,
            "retries": 3,
            "fragment_retries": 3,
            "skip_unavailable_fragments": True,
            "consoletitle": False,
            "cachedir": False,
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                "Accept-Language": "en-US,en;q=0.9,uk;q=0.8",
            },
            # Reduce SABR/web_safari issues: ask for default player client
            # Equivalent to: --extractor-args "youtube:player_client=default"
            "extractor_args": {"youtube": {"player_client": ["default"]}},
        }

        # Enable JS runtime if available (Node/Deno/Bun/QuickJS).
        # For Node specifically, wiki suggests enabling via --js-runtimes node or node:/path.
        if JS_RUNTIME_PATH:
            # If it's node.exe path we force node:<path>. Otherwise, allow generic runtime name.
            base = Path(JS_RUNTIME_PATH).name.lower()
            if base.startswith("node"):
                opts["js_runtimes"] = [f"node:{JS_RUNTIME_PATH}"]
            elif base.startswith("deno"):
                opts["js_runtimes"] = [f"deno:{JS_RUNTIME_PATH}"]
            elif base.startswith("bun"):
                opts["js_runtimes"] = [f"bun:{JS_RUNTIME_PATH}"]
            else:
                # Best-effort: still pass path as generic
                opts["js_runtimes"] = [JS_RUNTIME_PATH]

        # Ensure EJS scripts availability:
        # If package not available, allow yt-dlp to fetch EJS scripts from GitHub.
        if not HAS_EJS_PACKAGE:
            opts["remote_components"] = ["ejs:github"]

        return opts

    def __init__(self, source: discord.AudioSource, *, data: dict, volume: float = 0.6):
        super().__init__(source, volume)
        self.data = data
        self.title: str = data.get("title") or "Unknown title"
        self.webpage_url: str = data.get("webpage_url") or ""
        self.stream_url: str = data.get("url") or ""

    @classmethod
    async def create_source(cls, query_or_url: str) -> "YTDLSource":
        loop = asyncio.get_running_loop()
        ytdl = yt_dlp.YoutubeDL(cls._build_ytdl_opts())

        def _extract():
            return ytdl.extract_info(query_or_url, download=False)

        data = await loop.run_in_executor(None, _extract)

        if not data:
            raise RuntimeError("yt-dlp повернув порожню відповідь.")

        if "entries" in data and data["entries"]:
            data = data["entries"][0]

        if not data.get("url"):
            raise RuntimeError("Не вдалося отримати stream URL (data['url']).")

        # Use Opus if possible for lower overhead, but PCMTransformer is fine on top.
        audio = discord.FFmpegOpusAudio(
            data["url"],
            executable=cls.get_ffmpeg_executable(),
            **cls.FFMPEG_OPTS,
        )
        return cls(audio, data=data)


# =========================
# Per-guild player
# =========================
class GuildPlayer:
    def __init__(self, bot: commands.Bot, guild_id: int):
        self.bot = bot
        self.guild_id = guild_id
        self.queue: asyncio.Queue[YTDLSource | None] = asyncio.Queue()
        self.current: YTDLSource | None = None
        self.voice_channel_id: int | None = None
        self._task: asyncio.Task | None = None
        self._stop_flag = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop_flag.clear()
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.create_task(self._player_loop(), name=f"player:{self.guild_id}")
        self._task.add_done_callback(self._on_done)

    def _on_done(self, task: asyncio.Task) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("Player task crashed (guild=%s)", self.guild_id)

    async def stop(self, disconnect: bool = False) -> None:
        self._stop_flag.set()

        # Clear queue
        while not self.queue.empty():
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break

        self.current = None

        guild = self.bot.get_guild(self.guild_id)
        if guild and guild.voice_client:
            try:
                guild.voice_client.stop()
            except Exception:
                pass
            if disconnect:
                try:
                    await guild.voice_client.disconnect(force=True)
                except Exception:
                    pass

        if self._task and not self._task.done():
            self._task.cancel()

    async def _ensure_voice(self) -> discord.VoiceClient | None:
        guild = self.bot.get_guild(self.guild_id)
        if not guild:
            return None

        vc = guild.voice_client
        if vc and vc.is_connected():
            return vc

        if self.voice_channel_id is None:
            return None

        channel = guild.get_channel(self.voice_channel_id)
        if not isinstance(channel, discord.VoiceChannel):
            return None

        try:
            vc = await channel.connect(reconnect=True, timeout=20)
            return vc
        except Exception:
            log.exception("Failed to (re)connect to voice (guild=%s)", self.guild_id)
            return None

    async def _player_loop(self) -> None:
        while not self._stop_flag.is_set():
            source = await self.queue.get()
            if source is None:
                break

            self.current = source
            vc = await self._ensure_voice()
            if vc is None:
                log.warning("No voice connection. Dropping track: %s", source.title)
                self.current = None
                continue

            finished = asyncio.Event()

            def _after(err: Exception | None):
                if err:
                    log.warning("Audio player error (guild=%s): %s", self.guild_id, err)
                if self._loop:
                    self._loop.call_soon_threadsafe(finished.set)

            try:
                vc.play(source, after=_after)
            except discord.ClientException as e:
                # Not connected to voice, try a single reconnect and retry
                log.warning("play() failed (guild=%s): %s", self.guild_id, e)
                await asyncio.sleep(1.0)
                vc = await self._ensure_voice()
                if vc:
                    try:
                        vc.play(source, after=_after)
                    except Exception as e2:
                        log.warning("Retry play() failed (guild=%s): %s", self.guild_id, e2)
                        finished.set()
                else:
                    finished.set()

            await finished.wait()
            await asyncio.sleep(0.25)
            self.current = None


# =========================
# Music Cog
# =========================
class MusicBot(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.players: dict[int, GuildPlayer] = {}
        self.ytdl_lock = asyncio.Lock()

    def get_player(self, guild_id: int) -> GuildPlayer:
        player = self.players.get(guild_id)
        if not player:
            player = GuildPlayer(self.bot, guild_id)
            self.players[guild_id] = player
        return player

    @commands.hybrid_command(name="join")
    async def join(self, ctx: commands.Context):
        if not ctx.guild:
            await ctx.send("❌ Це працює тільки на сервері.")
            return

        if ctx.author.voice is None or ctx.author.voice.channel is None:
            await ctx.send("🔊 Спочатку зайди у голосовий канал 🙂")
            return

        channel = ctx.author.voice.channel
        player = self.get_player(ctx.guild.id)
        player.voice_channel_id = channel.id
        player.start()

        if ctx.voice_client is not None:
            try:
                await ctx.voice_client.move_to(channel)
            except Exception:
                await ctx.send("❌ Не зміг переміститися у канал. Перевір права.")
                return
        else:
            try:
                await channel.connect(reconnect=True, timeout=20)
            except Exception:
                await ctx.send("❌ Не зміг підключитись до голосового. Перевір права/регіон/мережу.")
                return

        await ctx.send(f"🎧 Приєднався до **{channel.name}** ✅")

    @commands.hybrid_command(name="play")
    async def play(self, ctx: commands.Context, *, url: str):
        if not ctx.guild:
            await ctx.send("❌ Це працює тільки на сервері.")
            return

        if ctx.voice_client is None or not ctx.voice_client.is_connected():
            await ctx.invoke(self.join)

        if ctx.voice_client is None or not ctx.voice_client.is_connected():
            return

        player = self.get_player(ctx.guild.id)
        player.start()

        msg = await ctx.send("⏳ Дістаю аудіо з YouTube...")

        # yt-dlp is not happy with parallel extract_info sometimes. Serialize it.
        async with self.ytdl_lock:
            try:
                source = await YTDLSource.create_source(url)
            except Exception as e:
                await msg.edit(content=f"❌ Помилка yt-dlp:\n```{e}```")
                return

        await player.queue.put(source)
        await msg.edit(content=f"🎶 Додав у чергу: **{source.title}** ✅")

    @commands.hybrid_command(name="skip")
    async def skip(self, ctx: commands.Context):
        if ctx.voice_client is not None and (ctx.voice_client.is_playing() or ctx.voice_client.is_paused()):
            ctx.voice_client.stop()
            await ctx.send("⏭️ Пропущено ✅")
        else:
            await ctx.send("😕 Зараз нічого не грає.")

    @commands.hybrid_command(name="stop")
    async def stop(self, ctx: commands.Context):
        if not ctx.guild:
            await ctx.send("❌ Це працює тільки на сервері.")
            return

        player = self.get_player(ctx.guild.id)
        await player.stop(disconnect=False)
        await ctx.send("⛔ Зупинив відтворення і очистив чергу ✅")

    @commands.hybrid_command(name="leave", aliases=["disconnect"])
    async def leave(self, ctx: commands.Context):
        if not ctx.guild:
            await ctx.send("❌ Це працює тільки на сервері.")
            return

        player = self.get_player(ctx.guild.id)
        await player.stop(disconnect=True)
        await ctx.send("👋 Відключився від голосового ✅")

    @commands.hybrid_command(name="now")
    async def now_playing(self, ctx: commands.Context):
        if not ctx.guild:
            await ctx.send("❌ Це працює тільки на сервері.")
            return

        player = self.get_player(ctx.guild.id)
        if player.current:
            await ctx.send(f"🎧 Зараз грає: **{player.current.title}**")
        else:
            await ctx.send("🔇 Зараз нічого не грає.")

    @commands.hybrid_command(name="ping")
    async def ping(self, ctx: commands.Context):
        latency_ms = round(self.bot.latency * 1000)
        await ctx.send(f"🏓 Pong: {latency_ms} ms")


# =========================
# Bot
# =========================
class MyBot(commands.Bot):
    async def setup_hook(self):
        await super().setup_hook()
        await self.add_cog(MusicBot(self))
        try:
            await self.tree.sync()
        except Exception:
            pass

    async def on_ready(self):
        log.info("🔌 Увійшов як %s (ID: %s)", self.user, getattr(self.user, "id", "unknown"))


def main():
    if not TOKEN:
        raise RuntimeError(
            "❌ Токен бота не задано. Установіть DISCORD_TOKEN або заповніть bot_token.py."
        )

    bot = MyBot(command_prefix=BOT_PREFIX, intents=intents)
    bot.run(TOKEN)


if __name__ == "__main__":
    main()
