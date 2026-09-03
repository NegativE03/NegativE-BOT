import discord
import json
import os
import io
import asyncio
import aiohttp
from discord.ext import commands
from discord import app_commands
from discord.ui import Button, View, Modal, TextInput, Select
from discord.ext import tasks
from datetime import datetime, timedelta
from dotenv import load_dotenv
from zoneinfo import ZoneInfo
from pymongo import MongoClient

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI")
if not MONGO_URI:
    raise RuntimeError("Brak MONGO_URI w pliku .env")

mongo = MongoClient(
    MONGO_URI,
    serverSelectionTimeoutMS=5000,
    connectTimeoutMS=5000
)

db = mongo["negative_bot"]

vacations_collection = db["vacations"]
recordings_collection = db["recordings"]
recording_stats_collection = db["recording_stats"]
day_member_polls_collection = db["day_member_polls"]

from pymongo.errors import PyMongoError

try:
    mongo.admin.command("ping")
    print("✅ MongoDB connected!")
    print("DB:", db.name)
    print("Collections:", db.list_collection_names())

except PyMongoError as e:
    print("❌ MongoDB ERROR:", e)

TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise RuntimeError("Brak TOKEN w pliku .env")

GUILD_ID = 1504878677106626630

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.reactions = True
intents.guilds = True
intents.guild_messages = True

bot = commands.Bot(command_prefix="!", intents=intents)

async def send_response(interaction: discord.Interaction, *args, **kwargs):
    """Odpowiada poprawnie niezależnie od tego, czy interakcja była odroczona."""
    if interaction.response.is_done():
        return await interaction.followup.send(*args, **kwargs)
    return await interaction.response.send_message(*args, **kwargs)

async def defer_slow_interaction(interaction: discord.Interaction):
    """Zapobiega komunikatowi „aplikacja nie odpowiada” dla wolniejszych komend."""
    await asyncio.sleep(2)
    try:
        if not interaction.response.is_done():
            await interaction.response.defer()
    except discord.InteractionResponded:
        pass

@bot.event
async def on_interaction(interaction: discord.Interaction):
    if interaction.type is discord.InteractionType.application_command:
        asyncio.create_task(defer_slow_interaction(interaction))

@bot.event
async def setup_hook():
    print("SETUP HOOK")
    print("PRZED SYNC:", len(bot.tree.get_commands()))

    for cmd in bot.tree.get_commands():
        print(cmd.name)

    await restore_day_member_poll_views()
    bot.add_view(PersonalStatsView())


@bot.event
async def on_ready():

    guild = discord.Object(id=GUILD_ID)

    bot.tree.copy_global_to(guild=guild)

    synced = await bot.tree.sync(guild=guild)

    print(f"ZSYNCHRONIZOWANO {len(synced)} KOMEND")

    print("=== KOMENDY ===")
    for cmd in synced:
        print(cmd.name)

    print("===============")

    print(f"Zalogowano jako {bot.user}")

    await bot.change_presence(
        activity=discord.Game(
            name="KACIEJOS - SERWER NAGRYWKOWY"
        )
    )

    if not update_server_status.is_running():
        update_server_status.start()

    if not check_recordings.is_running():
        check_recordings.start()

    if not check_vacations.is_running():
        check_vacations.start()

    if not check_day_member_polls.is_running():
        check_day_member_polls.start()

    await ensure_personal_stats_panel()

# /ping
@bot.tree.command(name="ping", description="Sprawdza opóźnienie bota")
async def ping(interaction: discord.Interaction):
    await send_response(interaction,
        f"🏓 Pong! {round(bot.latency * 1000)}ms"
    )


# /clear
@bot.tree.command(name="clear", description="Usuwa wiadomości")
@app_commands.describe(ilosc="Ile wiadomości usunąć")
async def clear(interaction: discord.Interaction, ilosc: int):

    if not interaction.user.guild_permissions.manage_messages:
        await send_response(interaction,
            "❌ Nie masz uprawnień.",
            ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    await interaction.channel.purge(limit=ilosc)

    await interaction.followup.send(
        f"✅ Usunięto {ilosc} wiadomości.",
        ephemeral=True
    )


## /kick
@bot.tree.command(name="kick", description="Wyrzuca użytkownika")
@app_commands.describe(
    user="Osoba do wyrzucenia",
    powod="Powód"
)
async def kick(
    interaction: discord.Interaction,
    user: discord.Member,
    powod: str = "Brak powodu"
):

    if not interaction.user.guild_permissions.kick_members:
        await send_response(interaction,
            "❌ Nie masz uprawnień.",
            ephemeral=True
        )
        return

    try:
        await user.kick(reason=powod)

        await send_response(interaction,
            f"👢 {user.mention} został wyrzucony.\nPowód: {powod}"
        )

    except discord.Forbidden:
        await send_response(interaction,
            "❌ Nie mogę wyrzucić tego użytkownika. Sprawdź pozycję ról i uprawnienia bota.",
            ephemeral=True
        )

# /ban
@bot.tree.command(name="ban", description="Banuje użytkownika")
@app_commands.describe(
    user="Osoba do zbanowania",
    powod="Powód"
)
async def ban(
    interaction: discord.Interaction,
    user: discord.Member,
    powod: str = "Brak powodu"
):

    if not interaction.user.guild_permissions.ban_members:
        await send_response(interaction,
            "❌ Nie masz uprawnień.",
            ephemeral=True
        )
        return

    try:
        await user.ban(reason=powod)

        await send_response(interaction,
            f"🔨 {user.mention} został zbanowany.\nPowód: {powod}"
        )

    except discord.Forbidden:
        await send_response(interaction,
            "❌ Nie mogę zbanować tego użytkownika. Sprawdź pozycję ról i uprawnienia bota.",
            ephemeral=True
        )

# /warn
@bot.tree.command(name="warn", description="Nadaje ostrzeżenie użytkownikowi")
@app_commands.describe(
    user="Osoba do ostrzeżenia",
    powod="Powód ostrzeżenia"
)
async def warn(
    interaction: discord.Interaction,
    user: discord.Member,
    powod: str
):

    if not interaction.user.guild_permissions.moderate_members:
        await send_response(interaction,
            "❌ Nie masz uprawnień.",
            ephemeral=True
        )
        return

    if not os.path.exists("warnings.json"):
        with open("warnings.json", "w") as f:
            json.dump({}, f)

    with open("warnings.json", "r") as f:
        warnings = json.load(f)

    user_id = str(user.id)

    if user_id not in warnings:
        warnings[user_id] = []

    warnings[user_id].append(powod)

    with open("warnings.json", "w") as f:
        json.dump(warnings, f, indent=4)

    # DM do użytkownika
    try:
        await user.send(
            f"⚠️ Otrzymałeś ostrzeżenie na serwerze **{interaction.guild.name}**\n\n"
            f"Powód: **{powod}**"
        )
    except:
        pass

    await send_response(interaction,
        f"⚠️ {user.mention} otrzymał ostrzeżenie.\nPowód: **{powod}**"
    )

    # /warnings
@bot.tree.command(name="warnings", description="Pokazuje ostrzeżenia użytkownika")
@app_commands.describe(
    user="Użytkownik"
)
async def warnings_cmd(
    interaction: discord.Interaction,
    user: discord.Member
):

    if not os.path.exists("warnings.json"):
        await send_response(interaction,
            "Brak ostrzeżeń."
        )
        return

    with open("warnings.json", "r") as f:
        warnings = json.load(f)

    user_id = str(user.id)

    if user_id not in warnings or len(warnings[user_id]) == 0:
        await send_response(interaction,
            f"✅ {user.mention} nie ma ostrzeżeń."
        )
        return

    tekst = ""

    for i, warn in enumerate(warnings[user_id], start=1):
        tekst += f"{i}. {warn}\n"

    await send_response(interaction,
        f"⚠️ Ostrzeżenia użytkownika {user.mention}:\n\n{tekst}"
    )

# /unwarn
@bot.tree.command(name="unwarn", description="Usuwa wybranego warna")
@app_commands.describe(
    user="Użytkownik",
    numer="Numer warna do usunięcia"
)
async def unwarn(
    interaction: discord.Interaction,
    user: discord.Member,
    numer: int
):

    if not interaction.user.guild_permissions.moderate_members:
        await send_response(interaction,
            "❌ Nie masz uprawnień.",
            ephemeral=True
        )
        return

    if not os.path.exists("warnings.json"):
        await send_response(interaction,
            "❌ Brak ostrzeżeń.",
            ephemeral=True
        )
        return

    with open("warnings.json", "r") as f:
        warnings = json.load(f)

    user_id = str(user.id)

    if user_id not in warnings:
        await send_response(interaction,
            "❌ Ten użytkownik nie ma ostrzeżeń.",
            ephemeral=True
        )
        return

    if numer < 1 or numer > len(warnings[user_id]):
        await send_response(interaction,
            "❌ Nieprawidłowy numer warna.",
            ephemeral=True
        )
        return

    usuniety = warnings[user_id].pop(numer - 1)

    with open("warnings.json", "w") as f:
        json.dump(warnings, f, indent=4)

    await send_response(interaction,
        f"✅ Usunięto warna nr {numer} użytkownikowi {user.mention}\nPowód: **{usuniety}**"
    )

TICKET_CATEGORY_ID = 1513593653556150303

STAFF_ROLES = [
    1504909609507487924,  # Kaciej
    1504909619825217778,  # Opiekun Ekipy
    1504909621721301112,  # Administrator
    1504909623147368699   # Moderator
]

STATUS_CHANNEL_ID = 1513930933525413959
STATUS_MESSAGE_ID = None

class TicketModal(Modal, title="Nowe zgłoszenie"):

    temat = TextInput(
        label="Temat zgłoszenia",
        placeholder="Np. Problem z nagrywką",
        required=True,
        max_length=100
    )

    opis = TextInput(
        label="Opis problemu",
        placeholder="Opisz dokładnie sytuację",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=1000
    )

    dowody = TextInput(
        label="Dowody / Linki",
        placeholder="Link do screena, filmu itp.",
        required=False,
        max_length=500
    )

    async def on_submit(self, interaction: discord.Interaction):

        guild = interaction.guild

        category = guild.get_channel(TICKET_CATEGORY_ID)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(
                view_channel=False
            ),
            interaction.user: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True
            )
        }

        for role_id in STAFF_ROLES:
            role = guild.get_role(role_id)

            if role:
                overwrites[role] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True
                )

        channel = await guild.create_text_channel(
            name=f"ticket-{interaction.user.name}",
            category=category,
            overwrites=overwrites
        )

        embed = discord.Embed(
            title="🎫 Nowe zgłoszenie",
            color=discord.Color.blue()
        )

        embed.add_field(
            name="👤 Autor",
            value=interaction.user.mention,
            inline=False
        )

        embed.add_field(
            name="📌 Temat",
            value=str(self.temat),
            inline=False
        )

        embed.add_field(
            name="📝 Opis",
            value=str(self.opis),
            inline=False
        )

        embed.add_field(
            name="📎 Dowody",
            value=str(self.dowody) if self.dowody else "Brak",
            inline=False
        )

        mentions = " ".join(
            f"<@&{role_id}>"
            for role_id in STAFF_ROLES
        )

        await channel.send(
            content=mentions,
            embed=embed
        )

        await send_response(interaction,
            f"✅ Ticket utworzony: {channel.mention}",
            ephemeral=True
        )

@bot.tree.command(
    name="ticket",
    description="Tworzy nowe zgłoszenie"
)
async def ticket(interaction: discord.Interaction):

    await interaction.response.send_modal(
        TicketModal()
    )

@bot.tree.command(
    name="ticketpanel",
    description="Wysyła panel ticketów"
)
async def ticketpanel(interaction: discord.Interaction):

    if not interaction.user.guild_permissions.administrator:
        await send_response(interaction,
            "❌ Nie masz uprawnień.",
            ephemeral=True
        )
        return

    embed = discord.Embed(
        title="🎫 SYSTEM TICKETÓW",
        description=(
            "Masz problem lub pytanie?\n\n"
            "Użyj komendy **/ticket** aby utworzyć zgłoszenie."
        ),
        color=discord.Color.blue()
    )

    await interaction.channel.send(
        embed=embed
    )

    await send_response(interaction,
        "✅ Panel wysłany.",
        ephemeral=True
    )

TICKET_LOG_CHANNEL = 1513601454630240398

@bot.tree.command(
    name="zamknij",
    description="Zamyka ticket i zapisuje transcript"
)
async def zamknij(interaction: discord.Interaction):

    if not interaction.channel.name.startswith("ticket-"):
        await send_response(interaction,
            "❌ Ta komenda działa tylko w ticketach.",
            ephemeral=True
        )
        return

    await send_response(interaction,
        "🔒 Zamykanie ticketa..."
    )

    log_channel = bot.get_channel(TICKET_LOG_CHANNEL)

    transcript = []

    async for message in interaction.channel.history(
        limit=None,
        oldest_first=True
    ):

        line = (
            f"[{message.created_at.strftime('%d.%m.%Y %H:%M:%S')}] "
            f"{message.author}: "
            f"{message.content}"
        )

        transcript.append(line)

    transcript_text = "\n".join(transcript)

    file = discord.File(
        io.BytesIO(transcript_text.encode("utf-8")),
        filename=f"{interaction.channel.name}.txt"
    )

    embed = discord.Embed(
        title="🎫 Ticket zamknięty",
        color=discord.Color.red()
    )

    embed.add_field(
        name="Kanał",
        value=interaction.channel.name,
        inline=False
    )

    embed.add_field(
        name="Zamknął",
        value=interaction.user.mention,
        inline=False
    )

    await log_channel.send(
        embed=embed,
        file=file
    )

    await asyncio.sleep(5)

    await interaction.channel.delete()

@tasks.loop(minutes=3)
async def update_server_status():
    print("STATUS LOOP START")
    channel = bot.get_channel(STATUS_CHANNEL_ID)
    if not channel:
        return
    server_name = "Kaciej Arcade"
    server_address = "83.168.68.62:30200"
    now = datetime.now(ZoneInfo("Europe/Warsaw"))

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(
                f"http://{server_address}/players.json",
                timeout=aiohttp.ClientTimeout(total=5)
            ) as response:
                response.raise_for_status()
                players = await response.json(content_type=None)

            async with session.get(
                f"http://{server_address}/info.json",
                timeout=aiohttp.ClientTimeout(total=5)
            ) as response:
                response.raise_for_status()
                info = await response.json(content_type=None)

            max_clients_raw = info.get("vars", {}).get("sv_maxClients", "?")
            try:
                max_clients = int(max_clients_raw)
            except (TypeError, ValueError):
                max_clients = None

            player_count = len(players)
            if max_clients:
                filled = min(10, round((player_count / max_clients) * 10))
                capacity_bar = "🟩" * filled + "⬛" * (10 - filled)
                player_value = f"**{player_count} / {max_clients}**\n{capacity_bar}"
            else:
                player_value = f"**{player_count} graczy**"

            embed = discord.Embed(
                title="🎮 KACIEJ ARCADE",
                description=(
                    "### 🟢 SERWER ONLINE\n"
                    "Serwer działa prawidłowo i jest gotowy do gry."
                ),
                color=discord.Color.green(),
                timestamp=now
            )
            embed.add_field(name="👥 Gracze online", value=player_value, inline=False)
            embed.add_field(
                name="🚀 Jak dołączyć?",
                value="Skopiuj i wklej w konsoli **F8**:\n```connect kaciejarcade.tknagrywki.pl```",
                inline=False
            )
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError) as error:
            print(f"❌ Kaciej Arcade status error: {error}")
            embed = discord.Embed(
                title="🎮 KACIEJ ARCADE",
                description=(
                    "### 🔴 SERWER OFFLINE\n"
                    "Serwer jest obecnie niedostępny albo nie odpowiada. Spróbuj ponownie później."
                ),
                color=discord.Color.red(),
                timestamp=now
            )
            embed.add_field(name="🔧 Status", value="**Brak połączenia**", inline=True)

    if bot.user:
        embed.set_thumbnail(url=bot.user.display_avatar.url)
    embed.set_footer(
        text=(
            "Kaciej Arcade • Automatyczna aktualizacja co 3 minuty • "
            f"{now.strftime('%d.%m.%Y, %H:%M:%S')}"
        )
    )

    global STATUS_MESSAGE_ID
    try:
        message = None
        if STATUS_MESSAGE_ID:
            message = await channel.fetch_message(STATUS_MESSAGE_ID)
        else:
            async for previous_message in channel.history(limit=25):
                if (
                    previous_message.author == bot.user
                    and previous_message.embeds
                    and previous_message.embeds[0].title in (
                        "🎮 KACIEJ ARCADE",
                        "🎮 STATUS SERWERÓW KACIEJOS"
                    )
                ):
                    message = previous_message
                    break

        if message:
            await message.edit(embed=embed)
        else:
            message = await channel.send(embed=embed)
        STATUS_MESSAGE_ID = message.id
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        message = await channel.send(embed=embed)
        STATUS_MESSAGE_ID = message.id

# Logi wiadomości
MESSAGE_LOGS_CHANNEL_ID = 1513882214188978288

# Logi reakcji
REACTION_LOGS_CHANNEL_ID = 1513882235273613312

# Logi VC
VC_LOGS_CHANNEL_ID = 1513885346159657052

@bot.event
async def on_message_delete(message):

    if message.author.bot:
        return

    log_channel = bot.get_channel(MESSAGE_LOGS_CHANNEL_ID)

    if not log_channel:
        return

    embed = discord.Embed(
        title="🗑️ Wiadomość usunięta",
        color=discord.Color.red()
    )

    embed.add_field(
        name="👤 Autor",
        value=message.author.mention,
        inline=False
    )

    embed.add_field(
        name="📍 Kanał",
        value=message.channel.mention,
        inline=False
    )

    embed.add_field(
        name="📝 Treść",
        value=message.content if message.content else "*Brak treści*",
        inline=False
    )

    await log_channel.send(embed=embed)

@bot.event
async def on_message_edit(before, after):

    if before.author.bot:
        return

    if before.content == after.content:
        return

    log_channel = bot.get_channel(MESSAGE_LOGS_CHANNEL_ID)

    if not log_channel:
        return

    embed = discord.Embed(
        title="✏️ Wiadomość edytowana",
        color=discord.Color.orange()
    )

    embed.add_field(
        name="👤 Autor",
        value=before.author.mention,
        inline=False
    )

    embed.add_field(
        name="📍 Kanał",
        value=before.channel.mention,
        inline=False
    )

    embed.add_field(
        name="📝 Przed",
        value=before.content if before.content else "*Brak treści*",
        inline=False
    )

    embed.add_field(
        name="📝 Po",
        value=after.content if after.content else "*Brak treści*",
        inline=False
    )

    embed.add_field(
        name="🔗 Wiadomość",
        value=f"[Przejdź do wiadomości]({after.jump_url})",
        inline=False
    )

    await log_channel.send(embed=embed)

@bot.event
async def on_raw_reaction_add(payload):

    print("RAW ADD WYWOŁANE")

    if payload.user_id == bot.user.id:
        return

    guild = bot.get_guild(payload.guild_id)

    if not guild:
        return

    member = guild.get_member(payload.user_id)

    channel = guild.get_channel(payload.channel_id)

    if not channel:
        return

    message = await channel.fetch_message(payload.message_id)

    # NAGRYWKI
    if str(payload.message_id) in load_recordings():

        nagrywki = load_recordings()
        nagrywka = nagrywki[str(payload.message_id)]

        if str(payload.emoji) == "✅":

            if member and any(
                role.id == URLOP_ROLE_ID
                for role in member.roles
            ):

                await message.remove_reaction(
                    "✅",
                    member
                )

                return

            if payload.user_id not in nagrywka["uczestnicy"]:

                nagrywka["uczestnicy"].append(
                    payload.user_id
                )

                save_recordings(nagrywki)

                embed = message.embeds[0]

                embed.set_field_at(
                    4,
                    name="✅ Biorę udział",
                    value=f"{len(nagrywka['uczestnicy'])} osób",
                    inline=False
                )

                await message.edit(embed=embed)

                nagrywki_log = bot.get_channel(
                    NAGRYWKI_LOGS_CHANNEL_ID
                )

                if nagrywki_log:
                    log_embed = discord.Embed(
                        title="✅ Nowe potwierdzenie obecności",
                        description=f"**{nagrywka['opis']}**",
                        color=discord.Color.green(),
                        timestamp=datetime.now(ZoneInfo("Europe/Warsaw"))
                    )
                    log_embed.add_field(
                        name="👤 Uczestnik",
                        value=member.mention,
                        inline=True
                    )
                    log_embed.add_field(
                        name="📅 Termin",
                        value=f"{nagrywka['data']} • {nagrywka['godzina']}",
                        inline=True
                    )
                    log_embed.add_field(
                        name="👥 Potwierdzone osoby",
                        value=str(len(nagrywka["uczestnicy"])),
                        inline=True
                    )
                    log_embed.set_thumbnail(url=member.display_avatar.url)
                    log_embed.set_footer(text=f"ID użytkownika: {member.id}")

                    await nagrywki_log.send(
                        embed=log_embed,
                        allowed_mentions=discord.AllowedMentions.none()
                    )

            return

    # ZWYKŁE LOGI REAKCJI
    log_channel = bot.get_channel(
        REACTION_LOGS_CHANNEL_ID
    )

    if not log_channel:
        return

    embed = discord.Embed(
        title="➕ Reakcja dodana",
        color=discord.Color.green()
    )

    embed.add_field(
        name="👤 Użytkownik",
        value=member.mention if member else f"ID: {payload.user_id}",
        inline=False
    )

    embed.add_field(
        name="😀 Emoji",
        value=str(payload.emoji),
        inline=False
    )

    embed.add_field(
        name="📍 Kanał",
        value=channel.mention,
        inline=False
    )

    embed.add_field(
        name="🔗 Wiadomość",
        value=f"[Przejdź do wiadomości]({message.jump_url})",
        inline=False
    )

    await log_channel.send(embed=embed)

@bot.event
async def on_raw_reaction_remove(payload):

    print("RAW REMOVE WYWOŁANE")

    guild = bot.get_guild(payload.guild_id)

    if not guild:
        return

    member = guild.get_member(payload.user_id)

    channel = guild.get_channel(payload.channel_id)

    if not channel:
        return

    message = await channel.fetch_message(payload.message_id)

    # NAGRYWKI
    if str(payload.message_id) in load_recordings():

        nagrywki = load_recordings()
        nagrywka = nagrywki[str(payload.message_id)]

        if str(payload.emoji) == "✅":

            if payload.user_id in nagrywka["uczestnicy"]:

                nagrywka["uczestnicy"].remove(
                    payload.user_id
                )

                save_recordings(nagrywki)

                embed = message.embeds[0]

                embed.set_field_at(
                    4,
                    name="✅ Biorę udział",
                    value=f"{len(nagrywka['uczestnicy'])} osób",
                    inline=False
                )

                await message.edit(embed=embed)

                nagrywki_log = bot.get_channel(
                    NAGRYWKI_LOGS_CHANNEL_ID
                )

                if nagrywki_log:

                    user_text = (
                        member.mention
                        if member
                        else f"ID: {payload.user_id}"
                    )

                    log_embed = discord.Embed(
                        title="➖ Wycofano potwierdzenie",
                        description=f"**{nagrywka['opis']}**",
                        color=discord.Color.orange(),
                        timestamp=datetime.now(ZoneInfo("Europe/Warsaw"))
                    )
                    log_embed.add_field(
                        name="👤 Uczestnik",
                        value=user_text,
                        inline=True
                    )
                    log_embed.add_field(
                        name="📅 Termin",
                        value=f"{nagrywka['data']} • {nagrywka['godzina']}",
                        inline=True
                    )
                    log_embed.add_field(
                        name="👥 Pozostałe potwierdzenia",
                        value=str(len(nagrywka["uczestnicy"])),
                        inline=True
                    )
                    if member:
                        log_embed.set_thumbnail(url=member.display_avatar.url)
                    log_embed.set_footer(text=f"ID użytkownika: {payload.user_id}")

                    await nagrywki_log.send(
                        embed=log_embed,
                        allowed_mentions=discord.AllowedMentions.none()
                    )

            return

    # ZWYKŁE LOGI REAKCJI
    log_channel = bot.get_channel(
        REACTION_LOGS_CHANNEL_ID
    )

    if not log_channel:
        return

    embed = discord.Embed(
        title="➖ Reakcja usunięta",
        color=discord.Color.red()
    )

    embed.add_field(
        name="👤 Użytkownik",
        value=member.mention if member else f"ID: {payload.user_id}",
        inline=False
    )

    embed.add_field(
        name="😀 Emoji",
        value=str(payload.emoji),
        inline=False
    )

    embed.add_field(
        name="📍 Kanał",
        value=channel.mention,
        inline=False
    )

    embed.add_field(
        name="🔗 Wiadomość",
        value=f"[Przejdź do wiadomości]({message.jump_url})",
        inline=False
    )

    await log_channel.send(embed=embed)

@bot.event
async def on_voice_state_update(member, before, after):

    if before.channel != after.channel:
        nagrywki = load_recordings()
        tracking_changed = False
        now = datetime.now(ZoneInfo("Europe/Warsaw"))

        for nagrywka in nagrywki.values():
            joined_at = nagrywka.setdefault("voice_joined_at", {})
            voice_seconds = nagrywka.setdefault("voice_seconds", {})
            first_joined_at = nagrywka.setdefault("first_voice_join_at", {})
            user_key = str(member.id)

            try:
                termin = datetime.fromisoformat(nagrywka["timestamp"])
                if termin.tzinfo is None:
                    termin = termin.replace(tzinfo=ZoneInfo("Europe/Warsaw"))
            except (KeyError, TypeError, ValueError):
                continue

            if after.channel and after.channel.id == NAGRYWKI_VC_ID:
                tracking_window_started = now >= termin - timedelta(hours=3)
                recording_not_finished = now <= termin + timedelta(
                    minutes=nagrywka.get("duration_minutes", 90)
                )
                is_first_entry = user_key not in first_joined_at
                if tracking_window_started and recording_not_finished and is_first_entry:
                    first_joined_at[user_key] = now.isoformat()
                    tracking_changed = True

                    if (
                        now > termin
                        and not member.bot
                        and any(
                            role.id in {NAGRYWKOWICZE_ROLE_ID, TESTOWI_ROLE_ID}
                            for role in member.roles
                        )
                    ):
                        late_seconds = int((now - termin).total_seconds())
                        late_minutes = (late_seconds + 59) // 60
                        late_log_channel = bot.get_channel(LATE_EXIT_LOG_CHANNEL_ID)
                        if late_log_channel:
                            late_embed = discord.Embed(
                                title="⏰ Spóźnienie na nagrywkę",
                                description=f"{member.mention} dołączył po rozpoczęciu nagrywki.",
                                color=discord.Color.orange(),
                                timestamp=now
                            )
                            late_embed.set_thumbnail(url=member.display_avatar.url)
                            late_embed.add_field(
                                name="🎬 Nagrywka",
                                value=f"**{nagrywka['opis']}**\n{nagrywka['data']} • {nagrywka['godzina']}",
                                inline=False
                            )
                            late_embed.add_field(name="⌛ Spóźnienie", value=f"**{late_minutes} min**", inline=True)
                            late_embed.add_field(
                                name="🕒 Pierwsze wejście",
                                value=f"<t:{int(now.timestamp())}:T>",
                                inline=True
                            )
                            late_embed.set_footer(text=f"ID użytkownika: {member.id}")
                            await late_log_channel.send(
                                embed=late_embed,
                                allowed_mentions=discord.AllowedMentions.none()
                            )

                if nagrywka.get("started", False) and user_key not in joined_at:
                    joined_at[user_key] = now.isoformat()
                    tracking_changed = True

            if (
                nagrywka.get("started", False)
                and before.channel
                and before.channel.id == NAGRYWKI_VC_ID
            ):
                joined_text = joined_at.pop(user_key, None)
                if joined_text:
                    joined_time = datetime.fromisoformat(joined_text)
                    voice_seconds[user_key] = voice_seconds.get(user_key, 0) + max(
                        0,
                        int((now - joined_time).total_seconds())
                    )
                    tracking_changed = True

                planned_end = termin + timedelta(minutes=nagrywka.get("duration_minutes", 90))
                if (
                    now < planned_end
                    and not member.bot
                    and any(
                        role.id in {NAGRYWKOWICZE_ROLE_ID, TESTOWI_ROLE_ID}
                        for role in member.roles
                    )
                ):
                    remaining_seconds = int((planned_end - now).total_seconds())
                    remaining_minutes = (remaining_seconds + 59) // 60
                    nagrywka.setdefault("early_exit_events", []).append({
                        "user_id": member.id,
                        "left_at": now.isoformat(),
                        "minutes_before_end": remaining_minutes
                    })
                    tracking_changed = True

                    exit_log_channel = bot.get_channel(LATE_EXIT_LOG_CHANNEL_ID)
                    if exit_log_channel:
                        exit_embed = discord.Embed(
                            title="🚪 Wcześniejsze wyjście z nagrywki",
                            description=f"{member.mention} opuścił kanał przed planowanym końcem.",
                            color=discord.Color.red(),
                            timestamp=now
                        )
                        exit_embed.set_thumbnail(url=member.display_avatar.url)
                        exit_embed.add_field(
                            name="🎬 Nagrywka",
                            value=f"**{nagrywka['opis']}**\n{nagrywka['data']} • {nagrywka['godzina']}",
                            inline=False
                        )
                        exit_embed.add_field(
                            name="⏳ Do końca pozostało",
                            value=f"**{remaining_minutes} min**",
                            inline=True
                        )
                        exit_embed.add_field(
                            name="🕒 Godzina wyjścia",
                            value=f"<t:{int(now.timestamp())}:T>",
                            inline=True
                        )
                        exit_embed.set_footer(text=f"ID użytkownika: {member.id}")
                        await exit_log_channel.send(
                            embed=exit_embed,
                            allowed_mentions=discord.AllowedMentions.none()
                        )

        if tracking_changed:
            save_recordings(nagrywki)

    log_channel = bot.get_channel(VC_LOGS_CHANNEL_ID)

    if not log_channel:
        return

    # Dołączenie do VC
    if before.channel is None and after.channel is not None:

        embed = discord.Embed(
            title="🔊 Dołączono do kanału głosowego",
            color=discord.Color.green()
        )

        embed.add_field(
            name="👤 Użytkownik",
            value=member.mention,
            inline=False
        )

        embed.add_field(
            name="🎤 Kanał",
            value=after.channel.mention,
            inline=False
        )

        await log_channel.send(embed=embed)

    # Opuszczenie VC
    elif before.channel is not None and after.channel is None:

        embed = discord.Embed(
            title="🔇 Opuszczono kanał głosowy",
            color=discord.Color.red()
        )

        embed.add_field(
            name="👤 Użytkownik",
            value=member.mention,
            inline=False
        )

        embed.add_field(
            name="🎤 Kanał",
            value=before.channel.mention,
            inline=False
        )

        await log_channel.send(embed=embed)

    # Przejście między VC
    elif (
        before.channel is not None
        and after.channel is not None
        and before.channel != after.channel
    ):

        embed = discord.Embed(
            title="🔄 Zmieniono kanał głosowy",
            color=discord.Color.orange()
        )

        embed.add_field(
            name="👤 Użytkownik",
            value=member.mention,
            inline=False
        )

        embed.add_field(
            name="⬅️ Z kanału",
            value=before.channel.mention,
            inline=True
        )

        embed.add_field(
            name="➡️ Na kanał",
            value=after.channel.mention,
            inline=True
        )

        await log_channel.send(embed=embed)

URLOP_ROLE_ID = 1504950644841124020
VACATION_LOG_CHANNEL_ID = 1513887745511264369
NAGRYWKI_CHANNEL_ID = 1504917763737518282
NAGRYWKI_LOGS_CHANNEL_ID = 1513890500296577156
NAGRYWKI_VC_ID = 1504922555595882547
LATE_EXIT_LOG_CHANNEL_ID = 1545193551774752859
NIEOBECNOSCI_FORUM_IDS = (
    1504918682642419712,
    1504918725478977607
)
REPORT_CHANNEL_ID = 1543600442766794753
PERSONAL_STATS_CHANNEL_ID = 1504927664635646104
NAGRYWKOWICZE_ROLE_ID = 1504910374963511316
TESTOWI_ROLE_ID = 1504911316173717625
BOSS_USER_ID = 308263498226597888
MIN_VC_ATTENDANCE_SECONDS = 35 * 60
POLISH_WEEKDAYS = (
    "poniedziałek",
    "wtorek",
    "środa",
    "czwartek",
    "piątek",
    "sobota",
    "niedziela"
)

def recording_forum_title(date_text):
    recording_date = datetime.strptime(date_text, "%d.%m.%Y")
    weekday = POLISH_WEEKDAYS[recording_date.weekday()]
    return f"Nieobecność {date_text} — {weekday}"

def recording_forum_content(opis, data, godzina, duration):
    return (
        "🎬 **Termin nagrywki**\n\n"
        f"📝 **Opis:** {opis}\n"
        f"📅 **Data:** {data}\n"
        f"🕒 **Godzina:** {godzina} (Europe/Warsaw)\n"
        f"⏱️ **Planowany czas:** {duration} minut\n"
        f"🔊 **Kanał VC:** <#{NAGRYWKI_VC_ID}>\n\n"
        "Jeżeli nie możesz pojawić się na nagrywce, zgłoś swoją nieobecność w tym poście."
    )

def finalize_voice_sessions(nagrywka, end_time):
    joined_at = nagrywka.setdefault("voice_joined_at", {})
    voice_seconds = nagrywka.setdefault("voice_seconds", {})

    for user_key, joined_text in list(joined_at.items()):
        try:
            joined_time = datetime.fromisoformat(joined_text)
            voice_seconds[user_key] = voice_seconds.get(user_key, 0) + max(
                0,
                int((end_time - joined_time).total_seconds())
            )
        except (TypeError, ValueError):
            pass
        del joined_at[user_key]

async def find_recording_forum_threads(nagrywka):
    """Odzyskuje posty także dla nagrywek utworzonych przed zapisem ich ID."""
    saved_ids = [int(thread_id) for thread_id in nagrywka.get("forum_thread_ids", [])]
    if saved_ids:
        return saved_ids

    expected_names = {
        recording_forum_title(nagrywka["data"]),
        (
            f"Nagrywka {nagrywka['data']} {nagrywka['godzina']} — "
            f"{nagrywka['opis']}"
        )[:100]
    }
    found_ids = []

    for forum_id in NIEOBECNOSCI_FORUM_IDS:
        forum = bot.get_channel(forum_id)
        if not isinstance(forum, discord.ForumChannel):
            continue

        matching_thread = next(
            (thread for thread in forum.threads if thread.name in expected_names),
            None
        )

        if matching_thread is None:
            try:
                async for thread in forum.archived_threads(limit=100):
                    if thread.name in expected_names:
                        matching_thread = thread
                        break
            except discord.HTTPException as error:
                print(f"❌ Nie udało się przejrzeć archiwum forum {forum_id}: {error}")

        if matching_thread is not None:
            found_ids.append(matching_thread.id)

    return found_ids

async def collect_absence_authors(thread_ids):
    authors_by_forum = {forum_id: set() for forum_id in NIEOBECNOSCI_FORUM_IDS}

    for thread_id in thread_ids:
        thread = bot.get_channel(int(thread_id))
        if thread is None:
            try:
                thread = await bot.fetch_channel(int(thread_id))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                continue

        if not isinstance(thread, discord.Thread):
            continue

        try:
            async for message in thread.history(limit=None, oldest_first=True):
                if not message.author.bot:
                    authors_by_forum.setdefault(thread.parent_id, set()).add(message.author.id)
        except (discord.Forbidden, discord.HTTPException) as error:
            print(f"❌ Nie udało się odczytać nieobecności z postu {thread_id}: {error}")

    return authors_by_forum

async def build_recording_statistics(message_id, nagrywka, guild):
    thread_ids = await find_recording_forum_threads(nagrywka)
    absence_authors = await collect_absence_authors(thread_ids)
    confirmed_ids = {
        int(user_id)
        for user_id, seconds in nagrywka.get("voice_seconds", {}).items()
        if seconds >= MIN_VC_ATTENDANCE_SECONDS
    }

    eligible_ids = set()
    absent_ids = set()
    vacation_ids = set()
    missing_ids = set()

    for role_id, forum_id in (
        (NAGRYWKOWICZE_ROLE_ID, NIEOBECNOSCI_FORUM_IDS[0]),
        (TESTOWI_ROLE_ID, NIEOBECNOSCI_FORUM_IDS[1])
    ):
        role = guild.get_role(role_id)
        if role is None:
            continue

        forum_absent_ids = absence_authors.get(forum_id, set())
        for member in role.members:
            if member.bot:
                continue

            eligible_ids.add(member.id)
            if any(member_role.id == URLOP_ROLE_ID for member_role in member.roles):
                vacation_ids.add(member.id)
            elif member.id in forum_absent_ids:
                absent_ids.add(member.id)
            elif member.id not in confirmed_ids:
                missing_ids.add(member.id)

    recording_start = datetime.fromisoformat(nagrywka["timestamp"])
    if recording_start.tzinfo is None:
        recording_start = recording_start.replace(tzinfo=ZoneInfo("Europe/Warsaw"))

    first_entries = {}
    late_minutes = {}
    early_minutes = {}
    for user_id in confirmed_ids & eligible_ids:
        entry_text = nagrywka.get("first_voice_join_at", {}).get(str(user_id))
        if not entry_text:
            continue
        try:
            entry_time = datetime.fromisoformat(entry_text)
            if entry_time.tzinfo is None:
                entry_time = entry_time.replace(tzinfo=ZoneInfo("Europe/Warsaw"))
            difference_seconds = int((entry_time - recording_start).total_seconds())
            first_entries[str(user_id)] = entry_time.isoformat()
            if difference_seconds > 0:
                late_minutes[str(user_id)] = (difference_seconds + 59) // 60
            elif difference_seconds < 0:
                early_minutes[str(user_id)] = (abs(difference_seconds) + 59) // 60
        except (TypeError, ValueError):
            continue

    return {
        "message_id": int(message_id),
        "opis": nagrywka["opis"],
        "data": nagrywka["data"],
        "godzina": nagrywka["godzina"],
        "timestamp": nagrywka["timestamp"],
        "archived_at": datetime.now(ZoneInfo("Europe/Warsaw")).isoformat(),
        "eligible_ids": sorted(eligible_ids),
        "confirmed_ids": sorted(confirmed_ids & eligible_ids),
        "absent_ids": sorted(absent_ids),
        "vacation_ids": sorted(vacation_ids),
        "missing_ids": sorted(missing_ids),
        "first_voice_join_at": first_entries,
        "late_minutes": late_minutes,
        "early_minutes": early_minutes,
        "early_exit_events": nagrywka.get("early_exit_events", []),
        "forum_thread_ids": thread_ids
    }

def load_vacations():

    vacations = {}

    for doc in vacations_collection.find():

        vacations[str(doc["user_id"])] = {
            "end": doc["end"]
        }

    return vacations


def save_vacations(data):

    existing = {
        str(doc["user_id"])
        for doc in vacations_collection.find({}, {"user_id": 1})
    }

    current = set(data.keys())

    for user_id in existing - current:
        vacations_collection.delete_one({
            "user_id": int(user_id)
        })

    for user_id, vacation in data.items():

        vacations_collection.update_one(
            {
                "user_id": int(user_id)
            },
            {
                "$set": {
                    "end": vacation["end"]
                }
            },
            upsert=True
        )
        print("SAVE RECORDINGS", data)

def load_recordings():

    recordings = {}

    for doc in recordings_collection.find():

        message_id = str(doc.pop("message_id"))

        recordings[message_id] = doc

    return recordings


def save_recordings(data):

    existing = {
        str(doc["message_id"])
        for doc in recordings_collection.find({}, {"message_id": 1})
    }

    current = set(data.keys())

    for message_id in existing - current:
        recordings_collection.delete_one({
            "message_id": int(message_id)
        })

    for message_id, recording in data.items():

        recordings_collection.update_one(
            {
                "message_id": int(message_id)
            },
            {
                "$set": recording
            },
            upsert=True
        )

@bot.tree.command(
    name="nadajurlop",
    description="Nadaje urlop nagrywkowiczowi"
)
@app_commands.describe(
    user="Nagrywkowicz",
    dni="Liczba dni urlopu"
)
async def nadajurlop(
    interaction: discord.Interaction,
    user: discord.Member,
    dni: int
):
    await interaction.response.defer(
        ephemeral=True
    )

    if not any(
        role.id in STAFF_ROLES
        for role in interaction.user.roles
    ):
        await send_response(interaction,
            "❌ Nie masz uprawnień.",
            ephemeral=True
        )
        return

    role = interaction.guild.get_role(URLOP_ROLE_ID)

    if role in user.roles:
        await send_response(interaction,
            "❌ Ten nagrywkowicz jest już na urlopie.",
            ephemeral=True
        )
        return

    if dni < 1:
        await send_response(
            interaction,
            "❌ Liczba dni urlopu musi być większa od zera.",
            ephemeral=True
        )
        return

    end_date = datetime.now(ZoneInfo("Europe/Warsaw")) + timedelta(days=dni)

    await user.add_roles(role)

    vacations = load_vacations()

    vacations[str(user.id)] = {
        "end": end_date.isoformat()
    }

    save_vacations(vacations)

    try:
        await user.send(
            f"🏖️ Twój urlop został zaakceptowany.\n\n"
            f"📅 Długość: **{dni} dni**\n"
            f"⏰ Powrót: **{end_date.strftime('%d.%m.%Y %H:%M')}**\n\n"
            f"Do zobaczenia na nagrywkach! 🎬"
        )
    except:
        pass

    log_channel = bot.get_channel(VACATION_LOG_CHANNEL_ID)

    embed = discord.Embed(
        title="🏖️ Urlop nadany",
        color=discord.Color.green()
    )

    embed.add_field(
        name="🎬 Nagrywkowicz",
        value=user.mention,
        inline=False
    )

    embed.add_field(
        name="👤 Nadał",
        value=interaction.user.mention,
        inline=False
    )

    embed.add_field(
        name="📅 Długość",
        value=f"{dni} dni",
        inline=False
    )

    embed.add_field(
        name="⏰ Powrót",
        value=end_date.strftime("%d.%m.%Y %H:%M"),
        inline=False
    )

    await log_channel.send(embed=embed)

    await interaction.followup.send(
        f"✅ Nadano urlop dla {user.mention}.",
        ephemeral=True
    )

@bot.tree.command(
    name="urlopy",
    description="Pokazuje aktywne urlopy"
)
async def urlopy(interaction: discord.Interaction):

    vacations = load_vacations()

    if len(vacations) == 0:
        await send_response(interaction,
            "📋 Brak aktywnych urlopów."
        )
        return

    tekst = ""

    for user_id, data in vacations.items():

        member = interaction.guild.get_member(
            int(user_id)
        )

        koniec = datetime.fromisoformat(
            data["end"]
        )

        tekst += (
            f"🎬 {member.mention if member else user_id}\n"
            f"⏰ {koniec.strftime('%d.%m.%Y %H:%M')}\n\n"
        )

    await send_response(interaction,
        f"📋 **Aktywne urlopy:**\n\n{tekst}"
    )

@bot.tree.command(
    name="zakonczurlop",
    description="Kończy urlop nagrywkowicza"
)
@app_commands.describe(
    user="Nagrywkowicz"
)
async def zakonczurlop(
    interaction: discord.Interaction,
    user: discord.Member
):  

    await interaction.response.defer(
        ephemeral=True
    )

    if not any(
        role.id in STAFF_ROLES
        for role in interaction.user.roles
    ):
        await send_response(interaction,
            "❌ Nie masz uprawnień.",
            ephemeral=True
        )
        return

    vacations = load_vacations()

    if str(user.id) not in vacations:
        await send_response(interaction,
            "❌ Ten nagrywkowicz nie jest na urlopie.",
            ephemeral=True
        )
        return

    role = interaction.guild.get_role(
        URLOP_ROLE_ID
    )

    await user.remove_roles(role)

    del vacations[str(user.id)]

    save_vacations(vacations)

    try:
        await user.send(
            "🔔 Twój urlop został zakończony wcześniej."
        )
    except:
        pass

    log_channel = bot.get_channel(
        VACATION_LOG_CHANNEL_ID
    )

    embed = discord.Embed(
        title="🛑 Urlop zakończony",
        color=discord.Color.red()
    )

    embed.add_field(
        name="🎬 Nagrywkowicz",
        value=user.mention,
        inline=False
    )

    embed.add_field(
        name="👤 Zakończył",
        value=interaction.user.mention,
        inline=False
    )

    await log_channel.send(embed=embed)

    await interaction.followup.send(
        f"✅ Zakończono urlop {user.mention}.",
        ephemeral=True
    )

@tasks.loop(minutes=1)
async def check_vacations():
    """Zdejmuje rolę urlopową po terminie i usuwa wpis z MongoDB."""
    vacations = await asyncio.to_thread(load_vacations)
    if not vacations:
        return

    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        return

    role = guild.get_role(URLOP_ROLE_ID)
    if role is None:
        print("❌ Nie znaleziono roli urlopowej")
        return

    now = datetime.now(ZoneInfo("Europe/Warsaw"))
    changed = False

    for user_id, data in list(vacations.items()):
        try:
            end_date = datetime.fromisoformat(data["end"])
            if end_date.tzinfo is None:
                end_date = end_date.replace(tzinfo=ZoneInfo("Europe/Warsaw"))
        except (KeyError, TypeError, ValueError):
            print(f"❌ Nieprawidłowa data urlopu dla użytkownika {user_id}")
            continue

        if end_date > now:
            continue

        member = guild.get_member(int(user_id))
        if member is None:
            try:
                member = await guild.fetch_member(int(user_id))
            except (discord.NotFound, discord.HTTPException):
                member = None

        if member is not None and role in member.roles:
            try:
                await member.remove_roles(role, reason="Automatyczne zakończenie urlopu")
                try:
                    await member.send("🔔 Twój urlop dobiegł końca. Rola urlopowa została zdjęta.")
                except discord.HTTPException:
                    pass
            except discord.HTTPException as error:
                print(f"❌ Nie udało się zdjąć roli urlopowej użytkownikowi {user_id}: {error}")
                continue

        del vacations[user_id]
        changed = True

    if changed:
        await asyncio.to_thread(save_vacations, vacations)

@bot.tree.command(
    name="nagrywka",
    description="Tworzy termin nagrywki"
)
@app_commands.describe(
    opis="Opis nagrywki",
    data="Data (DD.MM.RRRR)",
    godzina="Godzina (HH:MM)",
    czas="Planowany czas nagrywki w minutach"
)
async def nagrywka(
    interaction: discord.Interaction,
    opis: str,
    data: str,
    godzina: str,
    czas: int = 90
):

    await interaction.response.defer(
        ephemeral=True
    )

    if not any(
        role.id in STAFF_ROLES
        for role in interaction.user.roles
    ):
        await interaction.followup.send(
            "❌ Nie masz uprawnień.",
            ephemeral=True
        )
        return

    try:
        termin = datetime.strptime(
            f"{data} {godzina}",
            "%d.%m.%Y %H:%M"
        ).replace(tzinfo=ZoneInfo("Europe/Warsaw"))

    except ValueError:

        await interaction.followup.send(
            "❌ Niepoprawny format daty lub godziny.\n"
            "Przykład: 15.06.2026 i 18:00",
            ephemeral=True
        )

        return

    if czas < 35 or czas > 720:
        await interaction.followup.send(
            "❌ Czas nagrywki musi wynosić od 35 do 720 minut.",
            ephemeral=True
        )
        return

    channel = bot.get_channel(
        NAGRYWKI_CHANNEL_ID
    )

    embed = discord.Embed(
        title="🎬 TERMIN NAGRYWKI",
        color=discord.Color.blue()
    )

    embed.add_field(
        name="📝 Opis",
        value=opis,
        inline=False
    )

    embed.add_field(
        name="📅 Data",
        value=data,
        inline=True
    )

    embed.add_field(
        name="🕒 Godzina",
        value=f"{godzina}\n⏱️ {czas} min",
        inline=True
    )

    embed.add_field(
        name="🔊 Kanał VC",
        value=f"<#{NAGRYWKI_VC_ID}>",
        inline=False
    )

    embed.add_field(
        name="✅ Biorę udział",
        value="0 osób",
        inline=False
    )

    embed.set_footer(
        text="Kliknij ✅ aby zapisać się na nagrywkę."
    )

    message = await channel.send(
        embed=embed
    )

    await message.add_reaction("✅")

    post_title = recording_forum_title(data)
    post_content = recording_forum_content(opis, data, godzina, czas)

    forum_thread_ids = []

    for forum_id in NIEOBECNOSCI_FORUM_IDS:
        forum = bot.get_channel(forum_id)
        if not isinstance(forum, discord.ForumChannel):
            print(f"❌ Nie znaleziono forum nieobecności: {forum_id}")
            continue

        try:
            created_post = await forum.create_thread(
                name=post_title,
                content=post_content,
                reason=f"Automatyczny post dla nagrywki utworzonej przez {interaction.user}"
            )
            forum_thread_ids.append(created_post.thread.id)
        except discord.HTTPException as error:
            print(f"❌ Nie udało się utworzyć postu na forum {forum_id}: {error}")

    nagrywki = load_recordings()

    nagrywki[str(message.id)] = {
        "opis": opis,
        "data": data,
        "godzina": godzina,
        "timestamp": termin.isoformat(),
        "uczestnicy": [],
        "reminder_sent": False,
        "started": False,
        "forum_thread_ids": forum_thread_ids,
        "forums_closed": False,
        "report_sent": False,
        "duration_minutes": czas,
        "voice_seconds": {},
        "voice_joined_at": {},
        "first_voice_join_at": {},
        "early_exit_events": []
    }

    save_recordings(nagrywki)

    await interaction.followup.send(
        f"✅ Utworzono nagrywkę.\n"
        f"📍 {message.jump_url}",
        ephemeral=True
    )

@tasks.loop(minutes=1)
async def check_recordings():

    nagrywki = load_recordings()

    changed = False

    for message_id, nagrywka in list(nagrywki.items()):

        termin = datetime.fromisoformat(
            nagrywka["timestamp"]
        )

        if termin.tzinfo is None:
            termin = termin.replace(
                tzinfo=ZoneInfo("Europe/Warsaw")
            )

        now = datetime.now(
            ZoneInfo("Europe/Warsaw")
        )

        roznica = (
            termin - now
        ).total_seconds()

        # ZAMKNIĘCIE I ZABLOKOWANIE POSTÓW 3H PRZED NAGRYWKĄ
        if roznica <= 10800 and not nagrywka.get("forums_closed", False):
            forum_thread_ids = nagrywka.get("forum_thread_ids", [])
            all_forums_closed = True

            for thread_id in forum_thread_ids:
                thread = bot.get_channel(int(thread_id))

                if thread is None:
                    try:
                        thread = await bot.fetch_channel(int(thread_id))
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException) as error:
                        print(f"❌ Nie udało się pobrać postu forum {thread_id}: {error}")
                        all_forums_closed = False
                        continue

                if not isinstance(thread, discord.Thread):
                    print(f"❌ Kanał {thread_id} nie jest postem forum")
                    all_forums_closed = False
                    continue

                try:
                    await thread.edit(
                        archived=True,
                        locked=True,
                        reason="Automatyczne zamknięcie 3 godziny przed nagrywką (Europe/Warsaw)"
                    )
                except (discord.Forbidden, discord.HTTPException) as error:
                    print(f"❌ Nie udało się zamknąć postu forum {thread_id}: {error}")
                    all_forums_closed = False

            if all_forums_closed:
                nagrywka["forums_closed"] = True
                changed = True

        # PRZYPOMNIENIE 1H PRZED
        if (
            not nagrywka["reminder_sent"]
            and 0 <= roznica <= 3600
        ):

            guild = bot.get_guild(GUILD_ID)
            forum_thread_ids = await find_recording_forum_threads(nagrywka)

            if forum_thread_ids != nagrywka.get("forum_thread_ids", []):
                nagrywka["forum_thread_ids"] = forum_thread_ids
                if forum_thread_ids:
                    nagrywka["forums_closed"] = False
                changed = True

            absence_authors = await collect_absence_authors(forum_thread_ids)
            confirmed_ids = set(nagrywka["uczestnicy"])
            missing_by_role = {}

            role_forum_pairs = (
                (NAGRYWKOWICZE_ROLE_ID, NIEOBECNOSCI_FORUM_IDS[0]),
                (TESTOWI_ROLE_ID, NIEOBECNOSCI_FORUM_IDS[1])
            )

            if guild is not None:
                for role_id, forum_id in role_forum_pairs:
                    role = guild.get_role(role_id)
                    missing = []

                    if role is not None:
                        absent_ids = absence_authors.get(forum_id, set())
                        for member in role.members:
                            if member.bot:
                                continue
                            if member.id in confirmed_ids or member.id in absent_ids:
                                continue
                            if any(member_role.id == URLOP_ROLE_ID for member_role in member.roles):
                                continue
                            missing.append(member)

                    missing_by_role[role_id] = missing

            if not nagrywka.get("report_sent", False):
                report_channel = bot.get_channel(REPORT_CHANNEL_ID)
                if report_channel is not None:
                    embed = discord.Embed(
                        title="⚠️ Brak potwierdzenia obecności",
                        description=(
                            f"🎬 **{nagrywka['opis']}**\n"
                            f"📅 {nagrywka['data']} o {nagrywka['godzina']} "
                            "(Europe/Warsaw)\n\n"
                            "Poniższe osoby nie dały reakcji ✅, nie zgłosiły "
                            "nieobecności i nie mają aktywnego urlopu."
                        ),
                        color=discord.Color.orange()
                    )

                    for role_id, label in (
                        (NAGRYWKOWICZE_ROLE_ID, "🎬 Nagrywkowicze"),
                        (TESTOWI_ROLE_ID, "🧪 Testowi")
                    ):
                        members = missing_by_role.get(role_id, [])
                        value = "\n".join(member.mention for member in members) or "✅ Wszyscy odpowiedzieli"
                        embed.add_field(name=label, value=value[:1024], inline=False)

                    await report_channel.send(embed=embed)
                    nagrywka["report_sent"] = True
                    changed = True

            for user_id in nagrywka["uczestnicy"]:

                if guild is None:
                    break

                try:
                    user = await bot.fetch_user(user_id)
                except:
                    continue

                member = guild.get_member(user_id)

                if (
                    member
                    and any(
                        role.id == URLOP_ROLE_ID
                        for role in member.roles
                    )
                ):
                    continue

                try:

                    await user.send(
                        f"⏰ **Przypomnienie!**\n\n"
                        f"Za godzinę rozpoczyna się nagrywka:\n\n"
                        f"🎬 {nagrywka['opis']}\n"
                        f"📅 {nagrywka['data']}\n"
                        f"🕒 {nagrywka['godzina']}\n\n"
                        f"🔊 Kanał:\n"
                        f"<#{NAGRYWKI_VC_ID}>"
                    )

                except:
                    pass

            nagrywka["reminder_sent"] = True

            changed = True


        # START NAGRYWKI
        if (
            not nagrywka["started"]
            and now >= termin
        ):

            mentions = []

            for user_id in nagrywka["uczestnicy"]:

                user = bot.get_user(user_id)

                if not user:
                    continue

                guild = bot.get_guild(GUILD_ID)

                member = guild.get_member(user_id)

                if (
                    member
                    and any(
                        role.id == URLOP_ROLE_ID
                        for role in member.roles
                    )
                ):
                    continue

                mentions.append(
                    member.mention
                )

                try:

                    await user.send(
                        f"🔴 **Nagrywka właśnie się rozpoczęła!**\n\n"
                        f"🎬 {nagrywka['opis']}\n\n"
                        f"🔊 Dołącz tutaj:\n"
                        f"<#{NAGRYWKI_VC_ID}>"
                    )

                except:
                    pass


            channel = bot.get_channel(
                NAGRYWKI_CHANNEL_ID
            )

            if channel and mentions:

                await channel.send(
                    "🎬 **Nagrywka właśnie się rozpoczyna!**\n\n"
                    + " ".join(mentions)
                    + f"\n\n🔊 Kanał:\n<#{NAGRYWKI_VC_ID}>"
                )


            nagrywka["started"] = True
            nagrywka.setdefault("voice_seconds", {})
            joined_at = nagrywka.setdefault("voice_joined_at", {})
            first_joined_at = nagrywka.setdefault("first_voice_join_at", {})
            voice_channel = bot.get_channel(NAGRYWKI_VC_ID)

            if isinstance(voice_channel, discord.VoiceChannel):
                for voice_member in voice_channel.members:
                    joined_at.setdefault(str(voice_member.id), now.isoformat())
                    first_joined_at.setdefault(str(voice_member.id), now.isoformat())

            changed = True

        duration_minutes = nagrywka.get("duration_minutes", 90)
        automatic_end = termin + timedelta(minutes=duration_minutes)

        if nagrywka.get("started", False):
            voice_channel = bot.get_channel(NAGRYWKI_VC_ID)
            joined_at = nagrywka.setdefault("voice_joined_at", {})
            first_joined_at = nagrywka.setdefault("first_voice_join_at", {})
            nagrywka.setdefault("voice_seconds", {})
            if isinstance(voice_channel, discord.VoiceChannel):
                for voice_member in voice_channel.members:
                    if str(voice_member.id) not in joined_at:
                        joined_at[str(voice_member.id)] = now.isoformat()
                        changed = True
                    if str(voice_member.id) not in first_joined_at:
                        first_joined_at[str(voice_member.id)] = now.isoformat()
                        changed = True

        if nagrywka.get("started", False) and now >= automatic_end:
            finalize_voice_sessions(nagrywka, now)
            guild = bot.get_guild(GUILD_ID)

            if guild is not None:
                statistics = await build_recording_statistics(message_id, nagrywka, guild)
                statistics["ended_automatically"] = True
                await asyncio.to_thread(
                    recording_stats_collection.update_one,
                    {"message_id": int(message_id)},
                    {"$set": statistics},
                    True
                )

            recording_channel = bot.get_channel(NAGRYWKI_CHANNEL_ID)
            if recording_channel is not None:
                try:
                    message = await recording_channel.fetch_message(int(message_id))
                    finished_embed = discord.Embed(
                        title="✅ NAGRYWKA ZAKOŃCZONA AUTOMATYCZNIE",
                        description=nagrywka["opis"],
                        color=discord.Color.green()
                    )
                    finished_embed.add_field(
                        name="⏱️ Minimalna obecność",
                        value="35 minut na kanale VC",
                        inline=False
                    )
                    await message.edit(embed=finished_embed, view=None)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException) as error:
                    print(f"❌ Nie udało się oznaczyć zakończonej nagrywki {message_id}: {error}")

            del nagrywki[message_id]
            changed = True


    if changed:

        save_recordings(nagrywki)

def recording_select_options(nagrywki):
    return [
        discord.SelectOption(
            label=nagrywka["opis"][:50],
            description=f"{nagrywka['data']} {nagrywka['godzina']}"[:100],
            value=message_id
        )
        for message_id, nagrywka in nagrywki.items()
    ][:25]

class RecordingActionSelect(Select):
    def __init__(self, action, user=None):
        self.action = action
        self.target_user = user
        super().__init__(
            placeholder="Wybierz konkretną nagrywkę...",
            min_values=1,
            max_values=1,
            options=recording_select_options(load_recordings())
        )

    async def callback(self, interaction):
        recording_id = self.values[0]
        if self.action == "remind":
            await przypomnijnagrywke.callback(interaction, recording_id)
        elif self.action == "remove":
            await usunobecnosc.callback(interaction, self.target_user, recording_id)
        elif self.action == "finish":
            await zakoncznagrywke.callback(interaction, recording_id)
        elif self.action == "edit":
            nagrywka = load_recordings().get(recording_id)
            if nagrywka:
                await interaction.response.send_modal(EditRecordingModal(recording_id, nagrywka))

class RecordingActionView(View):
    def __init__(self, action, user=None):
        super().__init__(timeout=120)
        self.add_item(RecordingActionSelect(action, user))

class EditRecordingModal(Modal, title="Edytuj nagrywkę"):
    opis = TextInput(label="Opis", max_length=1000)
    data = TextInput(label="Data (DD.MM.RRRR)", max_length=10)
    godzina = TextInput(label="Godzina (HH:MM)", max_length=5)
    czas = TextInput(label="Czas w minutach", max_length=3)

    def __init__(self, recording_id, nagrywka):
        super().__init__()
        self.recording_id = recording_id
        self.opis.default = nagrywka["opis"]
        self.data.default = nagrywka["data"]
        self.godzina.default = nagrywka["godzina"]
        self.czas.default = str(nagrywka.get("duration_minutes", 90))

    async def on_submit(self, interaction):
        try:
            termin = datetime.strptime(
                f"{self.data.value} {self.godzina.value}", "%d.%m.%Y %H:%M"
            ).replace(tzinfo=ZoneInfo("Europe/Warsaw"))
            duration = int(self.czas.value)
            if not 35 <= duration <= 720:
                raise ValueError
        except ValueError:
            await send_response(interaction, "❌ Niepoprawna data, godzina lub czas.", ephemeral=True)
            return

        nagrywki = load_recordings()
        nagrywka = nagrywki.get(self.recording_id)
        if not nagrywka:
            await send_response(interaction, "❌ Ta nagrywka nie jest już aktywna.", ephemeral=True)
            return

        nagrywka.update({
            "opis": self.opis.value,
            "data": self.data.value,
            "godzina": self.godzina.value,
            "timestamp": termin.isoformat(),
            "duration_minutes": duration,
            "reminder_sent": False,
            "report_sent": False,
            "forums_closed": False
        })
        save_recordings(nagrywki)

        channel = bot.get_channel(NAGRYWKI_CHANNEL_ID)
        try:
            message = await channel.fetch_message(int(self.recording_id))
            embed = message.embeds[0]
            embed.set_field_at(0, name="📝 Opis", value=self.opis.value, inline=False)
            embed.set_field_at(1, name="📅 Data", value=self.data.value, inline=True)
            embed.set_field_at(2, name="🕒 Godzina", value=f"{self.godzina.value}\n⏱️ {duration} min", inline=True)
            await message.edit(embed=embed)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

        for thread_id in await find_recording_forum_threads(nagrywka):
            try:
                thread = bot.get_channel(int(thread_id)) or await bot.fetch_channel(int(thread_id))
                was_archived = thread.archived
                was_locked = thread.locked
                if was_archived or was_locked:
                    await thread.edit(archived=False, locked=False)

                await thread.edit(name=recording_forum_title(self.data.value))
                starter_message = await thread.fetch_message(thread.id)
                await starter_message.edit(content=recording_forum_content(
                    self.opis.value,
                    self.data.value,
                    self.godzina.value,
                    duration
                ))

                close_time = termin - timedelta(hours=3)
                if datetime.now(ZoneInfo("Europe/Warsaw")) >= close_time:
                    await thread.edit(archived=True, locked=True)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

        await send_response(interaction, "✅ Nagrywka została zaktualizowana.", ephemeral=True)

@bot.tree.command(
    name="edytujnagrywke",
    description="Zmienia termin, opis i czas wybranej nagrywki"
)
async def edytujnagrywke(interaction: discord.Interaction):
    if not any(role.id in STAFF_ROLES for role in interaction.user.roles):
        await send_response(interaction, "❌ Nie masz uprawnień.", ephemeral=True)
        return

    if not load_recordings():
        await send_response(interaction, "❌ Brak aktywnych nagrywek.", ephemeral=True)
        return

    await send_response(
        interaction,
        "🎬 Wybierz nagrywkę do edycji:",
        view=RecordingActionView("edit"),
        ephemeral=True
    )

class CancelRecordingSelect(Select):

    def __init__(self):

        nagrywki = load_recordings()

        options = []

        for message_id, nagrywka in nagrywki.items():

            options.append(
                discord.SelectOption(
                    label=nagrywka["opis"][:50],
                    description=(
                        f"{nagrywka['data']} "
                        f"{nagrywka['godzina']}"
                    )[:100],
                    value=message_id
                )
            )

        super().__init__(
            placeholder="Wybierz nagrywkę do odwołania...",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(
        self,
        interaction: discord.Interaction
    ):

        message_id = self.values[0]

        nagrywki = load_recordings()

        nagrywka = nagrywki[message_id]

        channel = bot.get_channel(
            NAGRYWKI_CHANNEL_ID
        )

        try:

            message = await channel.fetch_message(
                int(message_id)
            )

            embed = discord.Embed(
                title="❌ NAGRYWKA ODWOŁANA",
                description=(
                    "Ta nagrywka została "
                    "odwołana przez administrację."
                ),
                color=discord.Color.red()
            )

            embed.add_field(
                name="🎬 Nagrywka",
                value=nagrywka["opis"],
                inline=False
            )

            await message.edit(
                embed=embed
            )

        except:

            await send_response(interaction,
                "❌ Nie udało się odnaleźć wiadomości.",
                ephemeral=True
            )

            return


        # DM do uczestników
        for user_id in nagrywka["uczestnicy"]:

            user = bot.get_user(user_id)

            if not user:
                continue

            try:

                await user.send(
                    f"📢 **Nagrywka została odwołana.**\n\n"
                    f"🎬 {nagrywka['opis']}\n"
                    f"📅 {nagrywka['data']}\n"
                    f"🕒 {nagrywka['godzina']}"
                )

            except:
                pass


        # Logi
        log_channel = bot.get_channel(
            NAGRYWKI_LOGS_CHANNEL_ID
        )

        if log_channel:

            embed = discord.Embed(
                title="❌ Nagrywka odwołana",
                description=f"**{nagrywka['opis']}**",
                color=discord.Color.red()
            )

            embed.add_field(
                name="📅 Termin",
                value=f"{nagrywka['data']} • {nagrywka['godzina']}",
                inline=True
            )

            embed.add_field(
                name="👤 Odwołał",
                value=interaction.user.mention,
                inline=True
            )
            embed.timestamp = datetime.now(ZoneInfo("Europe/Warsaw"))
            embed.set_thumbnail(url=interaction.user.display_avatar.url)
            embed.set_footer(text=f"ID nagrywki: {message_id}")

            await log_channel.send(
                embed=embed,
                allowed_mentions=discord.AllowedMentions.none()
            )


        # Usuń z JSON
        del nagrywki[message_id]

        save_recordings(
            nagrywki
        )


        await send_response(interaction,
            "✅ Nagrywka została odwołana.",
            ephemeral=True
        )

class CancelRecordingView(View):
    def __init__(self):

        super().__init__(timeout=60)

        self.add_item(
            CancelRecordingSelect()
        )

@bot.tree.command(
    name="odwolajnagrywke",
    description="Odwołuje nagrywkę"
)
async def odwolajnagrywke(
    interaction: discord.Interaction
):

    if not any(
        role.id in STAFF_ROLES
        for role in interaction.user.roles
    ):

        await send_response(interaction,
            "❌ Nie masz uprawnień.",
            ephemeral=True
        )

        return


    if len(load_recordings()) == 0:

        await send_response(interaction,
            "❌ Brak aktywnych nagrywek.",
            ephemeral=True
        )

        return


    await send_response(interaction,
        "🎬 Wybierz nagrywkę:",
        view=CancelRecordingView(),
        ephemeral=True
    )

@bot.tree.command(
    name="przypomnijnagrywke",
    description="Wysyła ręczne przypomnienie o nagrywce"
)
async def przypomnijnagrywke(
    interaction: discord.Interaction,
    recording_id: str = None
):

    if not any(
        role.id in STAFF_ROLES
        for role in interaction.user.roles
    ):

        await send_response(interaction,
            "❌ Nie masz uprawnień.",
            ephemeral=True
        )

        return


    nagrywki = load_recordings()

    if len(nagrywki) == 0:

        await send_response(interaction,
            "❌ Brak aktywnych nagrywek.",
            ephemeral=True
        )

        return

    if recording_id is None:
        await send_response(
            interaction,
            "🎬 Wybierz nagrywkę do przypomnienia:",
            view=RecordingActionView("remind"),
            ephemeral=True
        )
        return

    message_id = recording_id

    nagrywka = nagrywki.get(message_id)
    if nagrywka is None:
        await send_response(interaction, "❌ Nie znaleziono nagrywki.", ephemeral=True)
        return

    wyslano = 0


    for user_id in nagrywka["uczestnicy"]:

        try:

            user = await bot.fetch_user(user_id)

            await user.send(
                f"⏰ **Przypomnienie!**\n\n"
                f"🎬 {nagrywka['opis']}\n"
                f"📅 {nagrywka['data']}\n"
                f"🕒 {nagrywka['godzina']}\n\n"
                f"🔊 Kanał:\n"
                f"<#{NAGRYWKI_VC_ID}>"
            )

            wyslano += 1

        except:
            pass


    await send_response(interaction,
        f"✅ Wysłano przypomnienie do {wyslano} osób.",
        ephemeral=True
    )

@bot.tree.command(
    name="usunobecnosc",
    description="Usuwa potwierdzenie osoby, która nie przyszła na nagrywkę"
)
@app_commands.describe(user="Osoba, której potwierdzenie ma zostać usunięte")
async def usunobecnosc(
    interaction: discord.Interaction,
    user: discord.Member,
    recording_id: str = None
):
    if not any(role.id in STAFF_ROLES for role in interaction.user.roles):
        await send_response(
            interaction,
            "❌ Nie masz uprawnień.",
            ephemeral=True
        )
        return

    nagrywki = load_recordings()
    if not nagrywki:
        await send_response(
            interaction,
            "❌ Brak aktywnych nagrywek.",
            ephemeral=True
        )
        return

    if recording_id is None:
        await send_response(
            interaction,
            "🎬 Wybierz nagrywkę:",
            view=RecordingActionView("remove", user),
            ephemeral=True
        )
        return

    message_id = recording_id
    nagrywka = nagrywki.get(message_id)
    if nagrywka is None:
        await send_response(interaction, "❌ Nie znaleziono nagrywki.", ephemeral=True)
        return

    if user.id not in nagrywka.get("uczestnicy", []):
        await send_response(
            interaction,
            f"❌ {user.mention} nie ma potwierdzonej obecności na tej nagrywce.",
            ephemeral=True
        )
        return

    nagrywka["uczestnicy"].remove(user.id)
    save_recordings(nagrywki)

    channel = bot.get_channel(NAGRYWKI_CHANNEL_ID)
    reaction_removed = False

    if channel is not None:
        try:
            message = await channel.fetch_message(int(message_id))
            await message.remove_reaction("✅", user)
            reaction_removed = True

            if message.embeds:
                embed = message.embeds[0]
                embed.set_field_at(
                    4,
                    name="✅ Biorę udział",
                    value=f"{len(nagrywka['uczestnicy'])} osób",
                    inline=False
                )
                await message.edit(embed=embed)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as error:
            print(f"❌ Nie udało się usunąć reakcji użytkownika {user.id}: {error}")

    result = (
        f"✅ Usunięto potwierdzenie {user.mention}. "
        "Osoba nie zostanie policzona jako obecna."
    )
    if not reaction_removed:
        result += "\n⚠️ Wpis w bazie usunięto, ale nie udało się usunąć reakcji na Discordzie."

    await send_response(interaction, result, ephemeral=True)

@bot.tree.command(
    name="zakoncznagrywke",
    description="Kończy aktywną nagrywkę"
)
async def zakoncznagrywke(
    interaction: discord.Interaction,
    recording_id: str = None
):

    if not any(
        role.id in STAFF_ROLES
        for role in interaction.user.roles
    ):

        await send_response(interaction,
            "❌ Nie masz uprawnień.",
            ephemeral=True
        )

        return


    nagrywki = load_recordings()

    if len(nagrywki) == 0:

        await send_response(interaction,
            "❌ Brak aktywnych nagrywek.",
            ephemeral=True
        )

        return

    if recording_id is None:
        await send_response(
            interaction,
            "🎬 Wybierz nagrywkę do zakończenia:",
            view=RecordingActionView("finish"),
            ephemeral=True
        )
        return

    message_id = recording_id
    nagrywka = nagrywki.get(message_id)
    if nagrywka is None:
        await send_response(interaction, "❌ Nie znaleziono nagrywki.", ephemeral=True)
        return


    channel = bot.get_channel(
        NAGRYWKI_CHANNEL_ID
    )


    try:

        message = await channel.fetch_message(
            int(message_id)
        )

        embed = discord.Embed(
            title="✅ NAGRYWKA ZAKOŃCZONA",
            color=discord.Color.green()
        )

        embed.add_field(
            name="🎬 Nagrywka",
            value=nagrywka["opis"],
            inline=False
        )

        embed.add_field(
            name="👤 Zakończył",
            value=interaction.user.mention,
            inline=False
        )

        await message.edit(
            embed=embed,
            view=None
        )

    except:
        pass

    guild = bot.get_guild(GUILD_ID)
    if guild is not None:
        finalize_voice_sessions(
            nagrywka,
            datetime.now(ZoneInfo("Europe/Warsaw"))
        )
        statistics = await build_recording_statistics(message_id, nagrywka, guild)
        await asyncio.to_thread(
            recording_stats_collection.update_one,
            {"message_id": int(message_id)},
            {"$set": statistics},
            True
        )

    del nagrywki[message_id]

    save_recordings(
        nagrywki
    )


    await send_response(interaction,
        "✅ Nagrywka została zakończona.",
        ephemeral=True
    )

class PersonalStatsView(View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Pokaż moje statystyki",
        emoji="📊",
        style=discord.ButtonStyle.primary,
        custom_id="personal_recording_statistics"
    )
    async def show_statistics(self, interaction: discord.Interaction, button: Button):
        allowed_role_ids = {NAGRYWKOWICZE_ROLE_ID, TESTOWI_ROLE_ID}
        if not any(role.id in allowed_role_ids for role in interaction.user.roles):
            await send_response(
                interaction,
                "❌ Ten panel jest dostępny wyłącznie dla pomocników i testowych.",
                ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        documents = await asyncio.to_thread(
            lambda: list(recording_stats_collection.find(
                {"eligible_ids": interaction.user.id},
                {"confirmed_ids": 1, "absent_ids": 1, "missing_ids": 1}
            ))
        )
        present = sum(interaction.user.id in doc.get("confirmed_ids", []) for doc in documents)
        absent = sum(interaction.user.id in doc.get("absent_ids", []) for doc in documents)
        missing = sum(interaction.user.id in doc.get("missing_ids", []) for doc in documents)
        required = present + absent + missing
        attendance = round((present / required) * 100, 1) if required else 0

        embed = discord.Embed(
            title="📊 Twoje podsumowanie nagrywek",
            description=f"Prywatne statystyki dla **{interaction.user.display_name}**",
            color=discord.Color.blurple(),
            timestamp=datetime.now(ZoneInfo("Europe/Warsaw"))
        )
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        embed.add_field(name="✅ Obecności", value=f"**{present}**", inline=True)
        embed.add_field(name="📝 Nieobecności", value=f"**{absent}**", inline=True)
        embed.add_field(name="⚠️ Bez odpowiedzi", value=f"**{missing}**", inline=True)
        embed.add_field(
            name="📈 Frekwencja",
            value=f"**{attendance}%**",
            inline=False
        )
        embed.set_footer(text="To podsumowanie jest widoczne tylko dla Ciebie")
        await send_response(interaction, embed=embed, ephemeral=True)

async def ensure_personal_stats_panel():
    channel = bot.get_channel(PERSONAL_STATS_CHANNEL_ID)
    if channel is None:
        print(f"❌ Nie znaleziono kanału panelu statystyk: {PERSONAL_STATS_CHANNEL_ID}")
        return

    panel_embed = discord.Embed(
        title="📊 TWOJE STATYSTYKI NAGRYWEK",
        description=(
            "Chcesz sprawdzić swoje aktualne podsumowanie?\n\n"
            "Kliknij przycisk poniżej, a bot prywatnie pokaże Ci:\n"
            "✅ liczbę obecności,\n"
            "📝 liczbę zgłoszonych nieobecności,\n"
            "⚠️ liczbę braków odpowiedzi,\n"
            "📈 procent frekwencji."
        ),
        color=discord.Color.blurple()
    )
    if bot.user:
        panel_embed.set_thumbnail(url=bot.user.display_avatar.url)
    panel_embed.set_footer(text="Dane są widoczne wyłącznie dla osoby klikającej przycisk")

    panel_message = None
    try:
        async for message in channel.history(limit=25):
            if (
                message.author == bot.user
                and message.embeds
                and message.embeds[0].title == "📊 TWOJE STATYSTYKI NAGRYWEK"
            ):
                panel_message = message
                break

        if panel_message:
            await panel_message.edit(embed=panel_embed, view=PersonalStatsView())
        else:
            await channel.send(
                embed=panel_embed,
                view=PersonalStatsView(),
                allowed_mentions=discord.AllowedMentions.none()
            )
    except (discord.Forbidden, discord.HTTPException) as error:
        print(f"❌ Nie udało się utworzyć panelu statystyk: {error}")

@bot.tree.command(
    name="statystyki",
    description="Pokazuje statystyki zakończonych nagrywek"
)
@app_commands.describe(user="Opcjonalnie: statystyki konkretnej osoby")
async def statystyki(
    interaction: discord.Interaction,
    user: discord.Member = None
):
    if user is not None and user.id == BOSS_USER_ID:
        await send_response(
            interaction,
            "🤨 **Szefa chcesz sprawdzać?**",
            ephemeral=True
        )
        return

    documents = await asyncio.to_thread(
        lambda: list(recording_stats_collection.find().sort("timestamp", 1))
    )

    if not documents:
        await send_response(
            interaction,
            "📊 Brak zakończonych nagrywek w statystykach.",
            ephemeral=True
        )
        return

    if user is not None:
        total = sum(user.id in doc.get("eligible_ids", []) for doc in documents)
        confirmed = sum(user.id in doc.get("confirmed_ids", []) for doc in documents)
        absent = sum(user.id in doc.get("absent_ids", []) for doc in documents)
        vacations = sum(user.id in doc.get("vacation_ids", []) for doc in documents)
        missing = sum(user.id in doc.get("missing_ids", []) for doc in documents)
        required = max(total - vacations, 0)
        attendance = round((confirmed / required) * 100, 1) if required else 0

        recent_results = []
        for doc in reversed(documents):
            if user.id not in doc.get("eligible_ids", []):
                continue
            if user.id in doc.get("confirmed_ids", []):
                status = "✅ Potwierdzona obecność"
            elif user.id in doc.get("absent_ids", []):
                status = "📝 Zgłoszona nieobecność"
            elif user.id in doc.get("vacation_ids", []):
                status = "🏖️ Urlop"
            else:
                status = "⚠️ Brak odpowiedzi"
            recent_results.append(
                f"`{doc.get('data', 'brak daty')}` • {status}"
            )
            if len(recent_results) == 5:
                break

        embed = discord.Embed(
            title="📊 Karta frekwencji",
            description=(
                f"### {user.mention}\n"
                "Podsumowanie zakończonych nagrywek"
            ),
            color=discord.Color.blurple(),
            timestamp=datetime.now(ZoneInfo("Europe/Warsaw"))
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.add_field(
            name="📈 Frekwencja",
            value=f"**{attendance}%**\n`{confirmed}/{required}` wymaganych nagrywek",
            inline=False
        )
        embed.add_field(name="✅ Obecności", value=f"**{confirmed}**", inline=True)
        embed.add_field(name="📝 Zgłoszone", value=f"**{absent}**", inline=True)
        embed.add_field(name="⚠️ Bez odpowiedzi", value=f"**{missing}**", inline=True)
        embed.add_field(name="🏖️ Urlopy", value=f"**{vacations}**", inline=True)
        embed.add_field(name="🎬 Wszystkie terminy", value=f"**{total}**", inline=True)
        embed.add_field(name="📋 Wymagane", value=f"**{required}**", inline=True)
        embed.add_field(
            name="🕘 Ostatnie wyniki",
            value="\n".join(recent_results) or "Brak historii dla tej osoby.",
            inline=False
        )
        embed.set_footer(text="Urlopy nie obniżają procentu frekwencji")
    else:
        confirmed = sum(len(doc.get("confirmed_ids", [])) for doc in documents)
        absent = sum(len(doc.get("absent_ids", [])) for doc in documents)
        vacations = sum(len(doc.get("vacation_ids", [])) for doc in documents)
        missing = sum(len(doc.get("missing_ids", [])) for doc in documents)
        required = confirmed + absent + missing
        attendance = round((confirmed / required) * 100, 1) if required else 0

        missing_counts = {}
        for doc in documents:
            for user_id in doc.get("missing_ids", []):
                missing_counts[user_id] = missing_counts.get(user_id, 0) + 1

        ranking = sorted(missing_counts.items(), key=lambda item: item[1], reverse=True)[:10]
        ranking_text = "\n".join(
            f"`{position}.` <@{user_id}> — **{count}**"
            for position, (user_id, count) in enumerate(ranking, start=1)
        ) or "✅ Brak nieusprawiedliwionych nieobecności"

        embed = discord.Embed(
            title="📊 Centrum statystyk nagrywek",
            description=(
                f"### Ogólna frekwencja: **{attendance}%**\n"
                f"Dane z **{len(documents)}** zakończonych nagrywek"
            ),
            color=discord.Color.blurple(),
            timestamp=datetime.now(ZoneInfo("Europe/Warsaw"))
        )
        embed.add_field(name="🎬 Nagrywki", value=f"**{len(documents)}**", inline=True)
        embed.add_field(name="✅ Obecności", value=f"**{confirmed}**", inline=True)
        embed.add_field(name="📋 Wymagane", value=f"**{required}**", inline=True)
        embed.add_field(name="📝 Zgłoszone", value=f"**{absent}**", inline=True)
        embed.add_field(name="🏖️ Urlopy", value=f"**{vacations}**", inline=True)
        embed.add_field(name="⚠️ Bez odpowiedzi", value=f"**{missing}**", inline=True)
        embed.add_field(
            name="🚨 Najwięcej braków odpowiedzi",
            value=ranking_text,
            inline=False
        )
        embed.set_footer(
            text="Użyj /statystyki user:@osoba, aby zobaczyć kartę konkretnej osoby"
        )

    await send_response(interaction, embed=embed)

def day_member_poll_embed(poll, guild):
    candidate_lines = []
    for position, user_id in enumerate(poll["candidate_ids"], start=1):
        member = guild.get_member(int(user_id))
        candidate_lines.append(
            f"⭐ **{position}.** {member.mention if member else f'<@{user_id}>'}"
        )

    closes_at = datetime.fromisoformat(poll["closes_at"])
    embed = discord.Embed(
        title="🏆 NAGRYWKOWICZ DNIA",
        description=(
            "### 🗳️ Głosowanie zostało rozpoczęte!\n"
            "Wybierz osobę, która Twoim zdaniem najlepiej zaprezentowała się podczas nagrywki."
        ),
        color=discord.Color.gold(),
        timestamp=datetime.now(ZoneInfo("Europe/Warsaw"))
    )
    embed.add_field(
        name="🎬 Nagrywka",
        value=(
            f"**{poll['recording_opis']}**\n"
            f"📅 {poll['recording_data']}  •  🕒 {poll['recording_godzina']}"
        ),
        inline=False
    )
    embed.add_field(
        name=f"🌟 Kandydaci ({len(candidate_lines)})",
        value="\n".join(candidate_lines),
        inline=False
    )
    embed.add_field(
        name="🕛 Zakończenie",
        value=f"<t:{int(closes_at.timestamp())}:F>\n<t:{int(closes_at.timestamp())}:R>",
        inline=True
    )
    embed.add_field(
        name="📌 Oddawanie głosu",
        value="Użyj menu znajdującego się pod wiadomością.",
        inline=True
    )
    embed.add_field(
        name="⚖️ Zasady głosowania",
        value=(
            "• Każda osoba ma **jeden głos**.\n"
            "• Głos można zmienić do zakończenia ankiety.\n"
            "• **Nie można głosować na samego siebie.**\n"
            "• Wyniki pozostają ukryte do końca głosowania."
        ),
        inline=False
    )
    if bot.user:
        embed.set_thumbnail(url=bot.user.display_avatar.url)
    embed.set_footer(text="NegativE* • Wyniki pojawią się automatycznie o północy")
    return embed

class DayMemberVoteSelect(Select):
    def __init__(self, poll):
        guild = bot.get_guild(int(poll["guild_id"]))
        options = []
        for user_id in poll["candidate_ids"]:
            member = guild.get_member(int(user_id)) if guild else None
            saved_name = poll.get("candidate_names", {}).get(str(user_id))
            options.append(discord.SelectOption(
                label=(member.display_name if member else saved_name or f"Użytkownik {user_id}")[:100],
                value=str(user_id),
                emoji="⭐"
            ))

        super().__init__(
            placeholder="Wybierz Nagrywkowicza Dnia...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id=f"day_member_vote:{poll['poll_id']}"
        )
        self.poll_id = poll["poll_id"]

    async def callback(self, interaction: discord.Interaction):
        poll = await asyncio.to_thread(
            day_member_polls_collection.find_one,
            {"poll_id": self.poll_id, "closed": False}
        )
        if not poll:
            await send_response(interaction, "❌ To głosowanie jest już zakończone.", ephemeral=True)
            return

        candidate_id = int(self.values[0])
        if candidate_id not in poll.get("candidate_ids", []):
            await send_response(interaction, "❌ Nieprawidłowy kandydat.", ephemeral=True)
            return

        if candidate_id == interaction.user.id:
            await send_response(
                interaction,
                "❌ Nie możesz zagłosować na samego siebie. Wybierz inną osobę.",
                ephemeral=True
            )
            return

        await asyncio.to_thread(
            day_member_polls_collection.update_one,
            {"poll_id": self.poll_id, "closed": False},
            {"$set": {f"votes.{interaction.user.id}": candidate_id}}
        )
        candidate = interaction.guild.get_member(candidate_id)
        candidate_name = candidate.display_name if candidate else f"ID {candidate_id}"
        await send_response(
            interaction,
            f"✅ Twój głos na **{candidate_name}** został zapisany. Możesz go później zmienić.",
            ephemeral=True
        )

class DayMemberVoteView(View):
    def __init__(self, poll):
        super().__init__(timeout=None)
        self.add_item(DayMemberVoteSelect(poll))

async def restore_day_member_poll_views():
    polls = await asyncio.to_thread(
        lambda: list(day_member_polls_collection.find({"closed": False}))
    )
    for poll in polls:
        if poll.get("message_id"):
            bot.add_view(DayMemberVoteView(poll), message_id=int(poll["message_id"]))

async def close_day_member_poll(poll_id, closed_by=None):
    poll = await asyncio.to_thread(
        day_member_polls_collection.find_one,
        {"poll_id": poll_id, "closed": False}
    )
    if not poll:
        return False

    votes = poll.get("votes", {})
    counts = {int(user_id): 0 for user_id in poll.get("candidate_ids", [])}
    valid_vote_count = 0
    for voter_id, candidate_id in votes.items():
        candidate_id = int(candidate_id)
        if candidate_id in counts and int(voter_id) != candidate_id:
            counts[candidate_id] += 1
            valid_vote_count += 1

    highest = max(counts.values(), default=0)
    winners = [user_id for user_id, count in counts.items() if count == highest and highest > 0]
    ranking = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    medals = ("🥇", "🥈", "🥉")
    ranking_lines = []
    for position, (user_id, count) in enumerate(ranking, start=1):
        marker = medals[position - 1] if position <= 3 else f"`{position}.`"
        if count == 1:
            vote_word = "głos"
        elif 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
            vote_word = "głosy"
        else:
            vote_word = "głosów"
        ranking_lines.append(f"{marker} <@{user_id}>  •  **{count} {vote_word}**")
    ranking_text = "\n".join(ranking_lines) or "Brak kandydatów."

    if not winners:
        result_text = "### 🗳️ Brak rozstrzygnięcia\nNie oddano żadnego ważnego głosu."
    elif len(winners) == 1:
        result_text = f"### 🎉 Zwycięzcą zostaje <@{winners[0]}>!\nGratulacje — zdobywasz tytuł **Nagrywkowicza Dnia**!"
    else:
        result_text = "### 🤝 Mamy remis!\nTytuł zdobywają: " + ", ".join(f"<@{user_id}>" for user_id in winners)

    result_embed = discord.Embed(
        title="🏆 NAGRYWKOWICZ DNIA — WYNIKI",
        description=result_text,
        color=discord.Color.gold(),
        timestamp=datetime.now(ZoneInfo("Europe/Warsaw"))
    )
    result_embed.add_field(
        name="🎬 Nagrywka",
        value=f"**{poll['recording_opis']}**\n{poll['recording_data']} • {poll['recording_godzina']}",
        inline=False
    )
    result_embed.add_field(name="📊 Końcowa klasyfikacja", value=ranking_text[:1024], inline=False)
    result_embed.add_field(name="🗳️ Ważne głosy", value=f"**{valid_vote_count}**", inline=True)
    if closed_by:
        result_embed.add_field(name="🔒 Zakończył", value=closed_by.mention, inline=True)
    else:
        result_embed.add_field(name="🕛 Zakończenie", value="Automatycznie o północy", inline=True)
    if len(winners) == 1:
        winner = bot.get_user(winners[0])
        if winner:
            result_embed.set_thumbnail(url=winner.display_avatar.url)
    elif bot.user:
        result_embed.set_thumbnail(url=bot.user.display_avatar.url)
    result_embed.set_footer(text="NegativE* • Dziękujemy za udział w głosowaniu")

    channel = bot.get_channel(int(poll["channel_id"]))
    if channel is None:
        try:
            channel = await bot.fetch_channel(int(poll["channel_id"]))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            channel = None

    if channel is not None:
        try:
            message = await channel.fetch_message(int(poll["message_id"]))
            closed_embed = message.embeds[0] if message.embeds else discord.Embed()
            closed_embed.title = "🔒 Nagrywkowicz Dnia — głosowanie zakończone"
            closed_embed.color = discord.Color.dark_grey()
            await message.edit(embed=closed_embed, view=None)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

        await channel.send(
            embed=result_embed,
            allowed_mentions=discord.AllowedMentions.none()
        )

    await asyncio.to_thread(
        day_member_polls_collection.update_one,
        {"poll_id": poll_id},
        {"$set": {
            "closed": True,
            "closed_at": datetime.now(ZoneInfo("Europe/Warsaw")).isoformat(),
            "winner_ids": winners,
            "result_counts": {str(user_id): count for user_id, count in counts.items()}
        }}
    )
    return True

@tasks.loop(minutes=1)
async def check_day_member_polls():
    now = datetime.now(ZoneInfo("Europe/Warsaw"))
    polls = await asyncio.to_thread(
        lambda: list(day_member_polls_collection.find({"closed": False}))
    )
    for poll in polls:
        try:
            closes_at = datetime.fromisoformat(poll["closes_at"])
            if closes_at.tzinfo is None:
                closes_at = closes_at.replace(tzinfo=ZoneInfo("Europe/Warsaw"))
            if now >= closes_at:
                await close_day_member_poll(poll["poll_id"])
        except (KeyError, TypeError, ValueError) as error:
            print(f"❌ Błędne dane ankiety Nagrywkowicza Dnia: {error}")

class DayMemberRecordingSelect(Select):
    def __init__(self, documents):
        self.documents = {str(doc["message_id"]): doc for doc in documents}
        super().__init__(
            placeholder="Wybierz zakończoną nagrywkę...",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=doc.get("opis", "Nagrywka")[:100],
                    description=f"{doc.get('data', '')} {doc.get('godzina', '')}"[:100],
                    value=str(doc["message_id"])
                )
                for doc in documents[:25]
            ]
        )

    async def callback(self, interaction: discord.Interaction):
        document = self.documents.get(self.values[0])
        if not document:
            await send_response(interaction, "❌ Nie znaleziono nagrywki.", ephemeral=True)
            return

        allowed_role_ids = {NAGRYWKOWICZE_ROLE_ID, TESTOWI_ROLE_ID}
        candidates = []
        for user_id in document.get("confirmed_ids", []):
            member = interaction.guild.get_member(int(user_id))
            if member and not member.bot and any(role.id in allowed_role_ids for role in member.roles):
                candidates.append(member.id)

        candidates = list(dict.fromkeys(candidates))
        if not candidates:
            await send_response(
                interaction,
                "❌ Na tej nagrywce nie było żadnego obecnego pomocnika ani testowego.",
                ephemeral=True
            )
            return
        if len(candidates) > 25:
            await send_response(interaction, "❌ Ankieta może zawierać maksymalnie 25 osób.", ephemeral=True)
            return

        now = datetime.now(ZoneInfo("Europe/Warsaw"))
        closes_at = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        date_key = now.strftime("%Y-%m-%d")
        existing = await asyncio.to_thread(
            day_member_polls_collection.find_one,
            {"guild_id": interaction.guild.id, "date_key": date_key}
        )
        if existing:
            await send_response(interaction, "❌ Ankieta Nagrywkowicza Dnia została już dzisiaj utworzona.", ephemeral=True)
            return

        poll_id = f"{interaction.guild.id}-{int(now.timestamp() * 1000000)}"
        poll = {
            "poll_id": poll_id,
            "guild_id": interaction.guild.id,
            "channel_id": interaction.channel.id,
            "message_id": None,
            "recording_message_id": int(document["message_id"]),
            "recording_opis": document.get("opis", "Nagrywka"),
            "recording_data": document.get("data", "brak daty"),
            "recording_godzina": document.get("godzina", "brak godziny"),
            "candidate_ids": candidates,
            "candidate_names": {
                str(user_id): interaction.guild.get_member(user_id).display_name
                for user_id in candidates
            },
            "votes": {},
            "date_key": date_key,
            "created_at": now.isoformat(),
            "closes_at": closes_at.isoformat(),
            "closed": False
        }
        await asyncio.to_thread(day_member_polls_collection.insert_one, poll)
        poll_message = await interaction.channel.send(
            embed=day_member_poll_embed(poll, interaction.guild),
            view=DayMemberVoteView(poll),
            allowed_mentions=discord.AllowedMentions.none()
        )
        poll["message_id"] = poll_message.id
        await asyncio.to_thread(
            day_member_polls_collection.update_one,
            {"poll_id": poll_id},
            {"$set": {"message_id": poll_message.id}}
        )
        await send_response(interaction, "✅ Ankieta została opublikowana.", ephemeral=True)

class DayMemberRecordingView(View):
    def __init__(self, documents):
        super().__init__(timeout=120)
        self.add_item(DayMemberRecordingSelect(documents))

@bot.tree.command(
    name="nagrywkowiczdnia",
    description="Tworzy ankietę Nagrywkowicza Dnia"
)
async def nagrywkowiczdnia(interaction: discord.Interaction):
    if not any(role.id in STAFF_ROLES for role in interaction.user.roles):
        await send_response(interaction, "❌ Nie masz uprawnień.", ephemeral=True)
        return

    documents = await asyncio.to_thread(
        lambda: list(recording_stats_collection.find().sort("timestamp", -1).limit(25))
    )
    if not documents:
        await send_response(interaction, "❌ Brak zakończonych nagrywek.", ephemeral=True)
        return

    await send_response(
        interaction,
        "🎬 Wybierz nagrywkę, dla której chcesz utworzyć ankietę:",
        view=DayMemberRecordingView(documents),
        ephemeral=True
    )

class CloseDayMemberPollSelect(Select):
    def __init__(self, polls):
        super().__init__(
            placeholder="Wybierz ankietę do zakończenia...",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=poll.get("recording_opis", "Ankieta")[:100],
                    description=f"{poll.get('recording_data', '')} • utworzona {poll.get('date_key', '')}"[:100],
                    value=poll["poll_id"]
                )
                for poll in polls[:25]
            ]
        )

    async def callback(self, interaction: discord.Interaction):
        if await close_day_member_poll(self.values[0], interaction.user):
            await send_response(interaction, "✅ Ankieta została zakończona, a wyniki opublikowane.", ephemeral=True)
        else:
            await send_response(interaction, "❌ Ankieta jest już zakończona.", ephemeral=True)

class CloseDayMemberPollView(View):
    def __init__(self, polls):
        super().__init__(timeout=120)
        self.add_item(CloseDayMemberPollSelect(polls))

@bot.tree.command(
    name="zakoncznagrywkowiczdnia",
    description="Kończy ankietę Nagrywkowicza Dnia i publikuje wyniki"
)
async def zakoncznagrywkowiczdnia(interaction: discord.Interaction):
    if not any(role.id in STAFF_ROLES for role in interaction.user.roles):
        await send_response(interaction, "❌ Nie masz uprawnień.", ephemeral=True)
        return

    polls = await asyncio.to_thread(
        lambda: list(day_member_polls_collection.find({"guild_id": interaction.guild.id, "closed": False}))
    )
    if not polls:
        await send_response(interaction, "❌ Brak aktywnych ankiet.", ephemeral=True)
        return

    await send_response(
        interaction,
        "🏆 Wybierz ankietę do zakończenia:",
        view=CloseDayMemberPollView(polls),
        ephemeral=True
    )

@bot.tree.command(
    name="naprawurlopy",
    description="Odbudowuje listę urlopów na podstawie ról"
)
async def naprawurlopy(
    interaction: discord.Interaction
):

    if not any(
        role.id in STAFF_ROLES
        for role in interaction.user.roles
    ):
        await send_response(interaction,
            "❌ Nie masz uprawnień.",
            ephemeral=True
        )
        return

    role = interaction.guild.get_role(
        URLOP_ROLE_ID
    )

    vacations = load_vacations()

    dodano = 0

    for member in role.members:

        if str(member.id) not in vacations:

            vacations[str(member.id)] = {
                "end": (
                    datetime.now(ZoneInfo("Europe/Warsaw"))
                    + timedelta(days=30)
                ).isoformat()
            }

            dodano += 1

    save_vacations(vacations)

    await send_response(interaction,
        f"✅ Odbudowano {dodano} urlopów.",
        ephemeral=True
    )

bot.run(TOKEN)
