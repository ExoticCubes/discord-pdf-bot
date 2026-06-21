import os
import re
import asyncio
from dataclasses import dataclass, field
from typing import Optional
 
import discord
from discord.ext import commands
import PyPDF2
import edge_tts


# Configuration
 
BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
VOICE_NAME = "en-GB-RyanNeural"   # here to change the voices 
SPEECH_RATE = "-10%"             # - will slow it down, + will speeds it up
MAX_CHUNK_CHARS = 800
 
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
 
bot = commands.Bot(command_prefix="!", intents=intents)
 
# Text processing helpers
 
def clean_text(text: Optional[str]) -> str:
    if not text:
        return ""
    text = text.replace("-\n", "")     # rejoin hyphenated words split across lines
    text = text.replace("\n", " ")     # turn remaining line breaks into spaces
    text = re.sub(r"\s+", " ", text)   # here to collapse repeated whitespace
    return text.strip()
 
 
def split_into_chunks(text: str, max_len: int = MAX_CHUNK_CHARS):
    sentences = re.split(r"(?<=[.!?]) +", text)
    chunks, current = [], ""
    for sentence in sentences:
        if len(current) + len(sentence) < max_len:
            current += " " + sentence
        else:
            if current.strip():
                chunks.append(current.strip())
            current = sentence
    if current.strip():
        chunks.append(current.strip())
    return chunks
 
 
# Per-guild reading session
 
@dataclass
class Session:
    chunks: list
    voice_client: discord.VoiceClient
    text_channel: discord.abc.Messageable
    index: int = 0
    current_file: Optional[str] = None
    stopped: bool = False
 
 
sessions: dict[int, Session] = {}
 
 
async def play_next(guild_id: int):
    session = sessions.get(guild_id)
    if not session or session.stopped:
        return
 
    # Clean up the file that just finished playing
    if session.current_file and os.path.exists(session.current_file):
        try:
            os.remove(session.current_file)
        except OSError:
            pass
 
    if session.index >= len(session.chunks):
        await finish_session(guild_id, message="Finished reading the PDF.")
        return
 
    text = session.chunks[session.index]
    chunk_num = session.index
    session.index += 1
 
    filename = f"temp_{guild_id}_{chunk_num}.mp3"
    session.current_file = filename
 
    try:
        communicate = edge_tts.Communicate(text, voice=VOICE_NAME, rate=SPEECH_RATE)
        await communicate.save(filename)
    except Exception as e:
        await session.text_channel.send(f"Error generating speech: {e}")
        await finish_session(guild_id)
        return
 
    if session.stopped:
        # here if !stop was called while audio was generating
        if os.path.exists(filename):
            os.remove(filename)
        return
 
    source = discord.FFmpegPCMAudio(filename)
 
    def after_playing(error):
        if error:
            print(f"Playback error: {error}")
        asyncio.run_coroutine_threadsafe(play_next(guild_id), bot.loop)
 
    session.voice_client.play(source, after=after_playing)
 
 
async def finish_session(guild_id: int, message: Optional[str] = None):
    session = sessions.pop(guild_id, None)
    if not session:
        return
    if session.current_file and os.path.exists(session.current_file):
        try:
            os.remove(session.current_file)
        except OSError:
            pass
    if session.voice_client and session.voice_client.is_connected():
        await session.voice_client.disconnect()
    if message:
        try:
            await session.text_channel.send(message)
        except Exception:
            pass
 
# Discord Commands
 
@bot.command(name="read")
async def read(ctx: commands.Context):
    if not ctx.message.attachments:
        await ctx.send("Attach a PDF file to this message, e.g. `!read` with a file.")
        return
 
    attachment = ctx.message.attachments[0]
    if not attachment.filename.lower().endswith(".pdf"):
        await ctx.send("That doesn't look like a PDF file.")
        return
 
    if not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.send("You need to be in a voice channel first.")
        return
 
    if ctx.guild.id in sessions:
        await ctx.send("I'm already reading something in this server. Use `!stop` first.")
        return
 
    await ctx.send(f"Downloading and processing **{attachment.filename}**...")
 
    pdf_path = f"pdf_{ctx.guild.id}.pdf"
    await attachment.save(pdf_path)
 
    try:
        reader = PyPDF2.PdfReader(pdf_path)
        full_text = ""
        for page in reader.pages:
            full_text += clean_text(page.extract_text()) + " "
    except Exception as e:
        await ctx.send(f"Couldn't read that PDF: {e}")
        os.remove(pdf_path)
        return
    finally:
        if os.path.exists(pdf_path):
            os.remove(pdf_path)
 
    chunks = split_into_chunks(full_text)
    if not chunks:
        await ctx.send("Couldn't find any readable text in that PDF (it may be scanned images).")
        return
 
    voice_channel = ctx.author.voice.channel
    voice_client = ctx.voice_client
    if voice_client is None:
        voice_client = await voice_channel.connect()
    elif voice_client.channel != voice_channel:
        await voice_client.move_to(voice_channel)
 
    sessions[ctx.guild.id] = Session(
        chunks=chunks,
        voice_client=voice_client,
        text_channel=ctx.channel,
    )
 
    await ctx.send(f"Reading **{attachment.filename}** ({len(chunks)} parts)...")
    await play_next(ctx.guild.id)
 
 
@bot.command(name="pause")
async def pause(ctx: commands.Context):
    vc = ctx.voice_client
    if vc and vc.is_playing():
        vc.pause()
        await ctx.send("Paused.")
    else:
        await ctx.send("Nothing is playing right now.")
 
 
@bot.command(name="resume")
async def resume(ctx: commands.Context):
    vc = ctx.voice_client
    if vc and vc.is_paused():
        vc.resume()
        await ctx.send("Resumed.")
    else:
        await ctx.send("Nothing is paused right now.")
 
 
@bot.command(name="skip")
async def skip(ctx: commands.Context):
    vc = ctx.voice_client
    if vc and (vc.is_playing() or vc.is_paused()):
        vc.stop()  # will trigger the after-callback, which advances to the next chunk
        await ctx.send("Skipped to the next part.")
    else:
        await ctx.send("Nothing to skip.")
 
 
@bot.command(name="stop")
async def stop(ctx: commands.Context):
    session = sessions.get(ctx.guild.id)
    if not session:
        await ctx.send("I'm not reading anything right now.")
        return
 
    session.stopped = True
    if session.voice_client:
        session.voice_client.stop()
 
    await finish_session(ctx.guild.id, message="Stopped reading and left the voice channel.")
 
 
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
 
 
if __name__ == "__main__":
    if not BOT_TOKEN:
        print("Set the DISCORD_BOT_TOKEN environment variable.")
    else:
        bot.run(BOT_TOKEN)
