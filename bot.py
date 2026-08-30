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

    embed = discord.Embed(
        title="🎮 STATUS SERWERÓW KACIEJOS",
        color=discord.Color.green()
    )

    servers = {
        "Kaciej-Arcade": "83.168.68.62:30200"
    }

    async with aiohttp.ClientSession() as session:

        for name, ip in servers.items():

            try:
                async with session.get(
                    f"http://{ip}/players.json",
                    timeout=5
                ) as response:

                    players = await response.json(content_type=None)

                async with session.get(
                    f"http://{ip}/info.json",
                    timeout=5
                ) as response:

                    info = await response.json(content_type=None)

                max_clients = info.get(
                    "vars",
                    {}
                ).get(
                    "sv_maxClients",
                    "?"
                )

                embed.add_field(
                    name=f"🟢 {name}",
                    value=f"👥 {len(players)}/{max_clients}",
                    inline=False
                )

            except Exception:
                embed.add_field(
                    name=f"🔴 {name}",
                    value="Offline",
                    inline=False
                )

    embed.set_footer(
        text=(
            "Ostatnia aktualizacja: "
            f"{datetime.now(ZoneInfo('Europe/Warsaw')).strftime('%H:%M:%S')}"
        )
    )

    global STATUS_MESSAGE_ID

    try:

        if STATUS_MESSAGE_ID:

            message = await channel.fetch_message(
                STATUS_MESSAGE_ID
            )

            await message.edit(
                embed=embed
            )

        else:

            message = await channel.send(
                embed=embed
            )

            STATUS_MESSAGE_ID = message.id

    except:

        message = await channel.send(
            embed=embed
        )

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

                    await nagrywki_log.send(
                        f"➕ {member.mention} zapisał się na nagrywkę:\n"
                        f"🎬 {nagrywka['opis']}"
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

                    await nagrywki_log.send(
                        f"➖ {user_text} wypisał się z nagrywki:\n"
                        f"🎬 {nagrywka['opis']}"
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
NIEOBECNOSCI_FORUM_IDS = (
    1504918682642419712,
    1504918725478977607
)
REPORT_CHANNEL_ID = 1543600442766794753
NAGRYWKOWICZE_ROLE_ID = 1504910374963511316
TESTOWI_ROLE_ID = 1504911316173717625

async def find_recording_forum_threads(nagrywka):
    """Odzyskuje posty także dla nagrywek utworzonych przed zapisem ich ID."""
    saved_ids = [int(thread_id) for thread_id in nagrywka.get("forum_thread_ids", [])]
    if saved_ids:
        return saved_ids

    expected_name = (
        f"Nagrywka {nagrywka['data']} {nagrywka['godzina']} — {nagrywka['opis']}"
    )[:100]
    found_ids = []

    for forum_id in NIEOBECNOSCI_FORUM_IDS:
        forum = bot.get_channel(forum_id)
        if not isinstance(forum, discord.ForumChannel):
            continue

        matching_thread = next(
            (thread for thread in forum.threads if thread.name == expected_name),
            None
        )

        if matching_thread is None:
            try:
                async for thread in forum.archived_threads(limit=100):
                    if thread.name == expected_name:
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
    godzina="Godzina (HH:MM)"
)
async def nagrywka(
    interaction: discord.Interaction,
    opis: str,
    data: str,
    godzina: str
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
        value=godzina,
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

    post_title = f"Nagrywka {data} {godzina} — {opis}"[:100]
    post_content = (
        "🎬 **Termin nagrywki**\n\n"
        f"📝 **Opis:** {opis}\n"
        f"📅 **Data:** {data}\n"
        f"🕒 **Godzina:** {godzina} (Europe/Warsaw)\n"
        f"🔊 **Kanał VC:** <#{NAGRYWKI_VC_ID}>\n\n"
        "Jeżeli nie możesz pojawić się na nagrywce, zgłoś swoją nieobecność w tym poście."
    )

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
        "report_sent": False
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

    for message_id, nagrywka in nagrywki.items():

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

            changed = True


    if changed:

        save_recordings(nagrywki)

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
                color=discord.Color.red()
            )

            embed.add_field(
                name="🎬 Nagrywka",
                value=nagrywka["opis"],
                inline=False
            )

            embed.add_field(
                name="👤 Odwołał",
                value=interaction.user.mention,
                inline=False
            )

            await log_channel.send(
                embed=embed
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


    nagrywki = load_recordings()

    if len(nagrywki) == 0:

        await send_response(interaction,
            "❌ Brak aktywnych nagrywek.",
            ephemeral=True
        )

        return


    message_id = list(nagrywki.keys())[-1]

    nagrywka = nagrywki[message_id]

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
    name="zakoncznagrywke",
    description="Kończy aktywną nagrywkę"
)
async def zakoncznagrywke(
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


    nagrywki = load_recordings()

    if len(nagrywki) == 0:

        await send_response(interaction,
            "❌ Brak aktywnych nagrywek.",
            ephemeral=True
        )

        return


    message_id = list(nagrywki.keys())[-1]

    nagrywka = nagrywki[message_id]


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


    del nagrywki[message_id]

    save_recordings(
        nagrywki
    )


    await send_response(interaction,
        "✅ Nagrywka została zakończona.",
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
