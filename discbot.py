import asyncio
import os
import time
from collections import deque

import discord
import yt_dlp
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
COOKIES_FILE = os.getenv("YTDLP_COOKIES")  # optional, see README

IDLE_TIMEOUT = 5 * 60  # seconds with nothing playing before leaving
ALONE_TIMEOUT = 60  # seconds with no humans in the channel before leaving

YTDL_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",  # lets users type a search instead of a URL
    "source_address": "0.0.0.0",
}
if COOKIES_FILE:
    YTDL_OPTIONS["cookiefile"] = COOKIES_FILE

FFMPEG_OPTIONS = {
    # Stream URLs from YouTube can drop mid-song; these flags make ffmpeg reconnect.
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}

ytdl = yt_dlp.YoutubeDL(YTDL_OPTIONS)


class Track:
    def __init__(self, title, stream_url, webpage_url, requester):
        self.title = title
        self.stream_url = stream_url
        self.webpage_url = webpage_url
        self.requester = requester

    @classmethod
    async def from_query(cls, query, requester):
        # yt-dlp is blocking, so run it off the event loop
        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(None, lambda: ytdl.extract_info(query, download=False))
        if "entries" in data:  # search results come back as a list
            if not data["entries"]:
                raise ValueError("No results found.")
            data = data["entries"][0]
        return cls(data["title"], data["url"], data.get("webpage_url", query), requester)


class Music(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.queues = {}  # guild_id -> deque[Track]
        self.text_channels = {}  # guild_id -> channel to post the auto-leave notice in
        self.idle_since = {}  # guild_id -> time.monotonic() when playback stopped
        self.alone_since = {}  # guild_id -> time.monotonic() when the last human left

    async def cog_load(self):
        self.check_inactivity.start()

    async def cog_unload(self):
        self.check_inactivity.cancel()

    def get_queue(self, guild_id):
        return self.queues.setdefault(guild_id, deque())

    @tasks.loop(seconds=15)
    async def check_inactivity(self):
        now = time.monotonic()
        connected = {vc.guild.id for vc in self.bot.voice_clients}
        # Forget guilds we've left, so a stale timestamp can't kick us right after rejoining
        for timers in (self.idle_since, self.alone_since):
            for guild_id in list(timers):
                if guild_id not in connected:
                    del timers[guild_id]

        for vc in list(self.bot.voice_clients):
            guild_id = vc.guild.id
            if vc.is_playing() or vc.is_paused():
                self.idle_since.pop(guild_id, None)
            else:
                self.idle_since.setdefault(guild_id, now)

            if any(not m.bot for m in vc.channel.members):
                self.alone_since.pop(guild_id, None)
            else:
                self.alone_since.setdefault(guild_id, now)

            if now - self.alone_since.get(guild_id, now) >= ALONE_TIMEOUT:
                reason = "everyone left the channel"
            elif now - self.idle_since.get(guild_id, now) >= IDLE_TIMEOUT:
                reason = "nothing has played for 5 minutes"
            else:
                continue

            self.get_queue(guild_id).clear()
            # An uncaught error here would stop the loop for good, so just log it
            try:
                await vc.disconnect()
                channel = self.text_channels.get(guild_id)
                if channel:
                    await channel.send(f"Left the voice channel because {reason}.")
            except discord.HTTPException as e:
                print(f"Auto-leave error: {e}")

    @check_inactivity.before_loop
    async def before_check_inactivity(self):
        await self.bot.wait_until_ready()

    async def ensure_voice(self, ctx):
        """Connect to (or move to) the caller's voice channel. Returns the VoiceClient or None."""
        if ctx.author.voice is None or ctx.author.voice.channel is None:
            await ctx.send("You need to be in a voice channel to use this command.")
            return None

        channel = ctx.author.voice.channel
        # Discord silently ignores join requests without these, which surfaces as a 30s timeout
        perms = channel.permissions_for(ctx.guild.me)
        if not (perms.view_channel and perms.connect):
            await ctx.send(f"I don't have permission to join **{channel.name}**. Give me View Channel and Connect there.")
            return None

        self.text_channels[ctx.guild.id] = ctx.channel
        vc = ctx.voice_client
        if vc is None:
            return await channel.connect()
        if vc.channel != channel:
            await vc.move_to(channel)
        return vc

    def play_next(self, ctx):
        queue = self.get_queue(ctx.guild.id)
        vc = ctx.voice_client
        if not queue or vc is None:
            return

        track = queue.popleft()
        source = discord.PCMVolumeTransformer(
            discord.FFmpegPCMAudio(track.stream_url, **FFMPEG_OPTIONS), volume=0.5
        )

        def after(error):
            if error:
                print(f"Player error: {error}")
            # `after` runs in another thread, so hop back onto the bot's loop
            self.bot.loop.call_soon_threadsafe(self.play_next, ctx)

        vc.play(source, after=after)
        asyncio.run_coroutine_threadsafe(
            ctx.send(f"Now playing: **{track.title}** (requested by {track.requester.display_name})"),
            self.bot.loop,
        )

    @commands.hybrid_command(description="Join your voice channel")
    async def join(self, ctx):
        vc = await self.ensure_voice(ctx)
        if vc:
            await ctx.send(f"Joined **{vc.channel.name}**.")

    @commands.hybrid_command(description="Play a YouTube URL or search term")
    async def play(self, ctx, *, query: str):
        vc = await self.ensure_voice(ctx)
        if vc is None:
            return

        await ctx.defer()  # yt-dlp lookups can take a few seconds
        try:
            track = await Track.from_query(query, ctx.author)
        except Exception as e:
            await ctx.send(f"Couldn't load that video: {e}")
            return

        self.get_queue(ctx.guild.id).append(track)
        if vc.is_playing() or vc.is_paused():
            await ctx.send(f"Queued: **{track.title}**")
        else:
            await ctx.send(f"Loading **{track.title}**...")
            self.play_next(ctx)

    @commands.hybrid_command(description="Skip the current song")
    async def skip(self, ctx):
        vc = ctx.voice_client
        if vc and vc.is_playing():
            vc.stop()  # triggers `after`, which plays the next track
            await ctx.send("Skipped.")
        else:
            await ctx.send("Nothing is playing.")

    @commands.hybrid_command(description="Pause playback")
    async def pause(self, ctx):
        vc = ctx.voice_client
        if vc and vc.is_playing():
            vc.pause()
            await ctx.send("Paused.")
        else:
            await ctx.send("Nothing is playing.")

    @commands.hybrid_command(description="Resume playback")
    async def resume(self, ctx):
        vc = ctx.voice_client
        if vc and vc.is_paused():
            vc.resume()
            await ctx.send("Resumed.")
        else:
            await ctx.send("Nothing is paused.")

    @commands.hybrid_command(description="Show the queue")
    async def queue(self, ctx):
        queue = self.get_queue(ctx.guild.id)
        if not queue:
            await ctx.send("The queue is empty.")
            return
        lines = [f"{i}. {t.title}" for i, t in enumerate(list(queue)[:10], start=1)]
        await ctx.send("**Up next:**\n" + "\n".join(lines))

    @commands.hybrid_command(description="Stop playback and clear the queue")
    async def stop(self, ctx):
        self.get_queue(ctx.guild.id).clear()
        if ctx.voice_client:
            ctx.voice_client.stop()
        await ctx.send("Stopped and cleared the queue.")

    @commands.hybrid_command(description="Leave the voice channel")
    async def leave(self, ctx):
        self.get_queue(ctx.guild.id).clear()
        if ctx.voice_client:
            await ctx.voice_client.disconnect()
            await ctx.send("Left the voice channel.")
        else:
            await ctx.send("I'm not in a voice channel.")


class MusicBot(commands.Bot):
    async def setup_hook(self):
        await self.add_cog(Music(self))
        await self.tree.sync()  # registers the /slash versions of the commands


intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = MusicBot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"Missing argument: `{error.param.name}`")
    elif isinstance(error, commands.CommandNotFound):
        return
    else:
        print(f"Command error: {error}")
        await ctx.send("Something went wrong running that command.")


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN is not set. Copy .env.example to .env and add your token.")
    bot.run(TOKEN)
