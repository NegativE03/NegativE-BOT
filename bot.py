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
from pymongo import MongoClient, ReturnDocument

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI")
if not MONGO_URI:
    raise RuntimeError("Brak MONGO_URI w pliku .env")

mongo = MongoClient(
    MONGO_URI,
    serverSelectionTimeoutMS=5000,
    connectTimeoutMS=5000,
    socketTimeoutMS=8000
)

db = mongo["negative_bot"]

vacations_collection = db["vacations"]
recordings_collection = db["recordings"]
recording_stats_collection = db["recording_stats"]
day_member_polls_collection = db["day_member_polls"]
bot_counters_collection = db["bot_counters"]
work_credits_collection = db["work_credits"]
recording_locks_collection = db["recording_locks"]
recording_absences_collection = db["recording_absences"]
recording_attendance_choices_collection = db["recording_attendance_choices"]
recording_lateness_reports_collection = db["recording_lateness_reports"]
bot_state_collection = db["bot_state"]

from pymongo.errors import DuplicateKeyError, PyMongoError

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
    if interaction.response.type is discord.InteractionResponseType.deferred_channel_message:
        # Po defer trzeba zakończyć oryginalną odpowiedź. Zwykły follow-up może
        # pozostawić w Discordzie wiszące „BOT myśli...” bez końca.
        if kwargs.get("ephemeral", False):
            try:
                await interaction.delete_original_response()
            except (discord.NotFound, discord.HTTPException):
                pass
            return await interaction.followup.send(*args, **kwargs)

        edit_kwargs = dict(kwargs)
        edit_kwargs.pop("ephemeral", None)
        if args:
            edit_kwargs["content"] = args[0]
        return await interaction.edit_original_response(**edit_kwargs)

    if interaction.response.is_done():
        return await interaction.followup.send(*args, **kwargs)
    return await interaction.response.send_message(*args, **kwargs)

async def defer_slow_interaction(interaction: discord.Interaction):
    """Potwierdza komendę na tym samym obiekcie, który trafi do jej callbacku."""
    command_name = (interaction.data or {}).get("name")
    commands_with_own_initial_response = {
        "ticket",
        "clear",
        "nadajurlop",
        "zakonczurlop",
        "nagrywka",
        "statusnagrywki",
        "raportbrakuodpowiedzi"
    }
    if command_name in commands_with_own_initial_response:
        return True

    try:
        if not interaction.response.is_done():
            await interaction.response.defer()
    except discord.InteractionResponded:
        pass
    return True

bot.tree.interaction_check = defer_slow_interaction

@bot.event
async def setup_hook():
    print("SETUP HOOK")
    print("PRZED SYNC:", len(bot.tree.get_commands()))

    for cmd in bot.tree.get_commands():
        print(cmd.name)

    await restore_day_member_poll_views()
    await restore_double_absence_views()
    await restore_double_attendance_views()
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

    if not sync_recording_reactions_loop.is_running():
        sync_recording_reactions_loop.start()

    if not check_vacations.is_running():
        check_vacations.start()

    if not check_day_member_polls.is_running():
        check_day_member_polls.start()

    await ensure_personal_stats_panel()
    await refresh_active_recording_messages()
    await reconcile_existing_double_absences()
    await backfill_double_attendance_logs()

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
STATUS_PANEL_STATE_ID = "kaciej_arcade_status_panel"
BOT_REMOVING_ABSENCE_MESSAGE_IDS = set()

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
                title="🕹️ Kaciej Arcade • Status serwera",
                description=(
                    "## 🟢 ONLINE\n"
                    "Serwer jest dostępny i czeka na graczy."
                ),
                color=0x57F287,
                timestamp=now
            )
            embed.add_field(name="👥 Gracze", value=player_value, inline=False)
            embed.add_field(name="⚡ Dostępność", value="**Serwer działa prawidłowo**", inline=False)
            embed.add_field(
                name="🚀 Dołącz do serwera",
                value=(
                    "Otwórz konsolę **F8** i wklej:\n"
                    "```connect kaciejarcade.tknagrywki.pl```"
                ),
                inline=False
            )
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError) as error:
            print(f"❌ Kaciej Arcade status error: {error}")
            embed = discord.Embed(
                title="🕹️ Kaciej Arcade • Status serwera",
                description=(
                    "## 🔴 OFFLINE\n"
                    "Serwer chwilowo nie odpowiada. Panel sprawdzi go ponownie automatycznie."
                ),
                color=0xED4245,
                timestamp=now
            )
            embed.add_field(name="🛠️ Stan", value="**Brak połączenia z serwerem**", inline=False)
            embed.add_field(
                name="🔄 Co dalej?",
                value="Nie musisz nic robić — status odświeża się automatycznie co 3 minuty.",
                inline=False
            )

    if bot.user:
        embed.set_thumbnail(url=bot.user.display_avatar.url)
    embed.set_footer(
        text=(
            "Kaciej Arcade • Panel statusu • Aktualizacja co 3 minuty • "
            f"{now.strftime('%d.%m.%Y, %H:%M:%S')}"
        )
    )

    global STATUS_MESSAGE_ID
    message = None

    if STATUS_MESSAGE_ID is None:
        saved_panel = await asyncio.to_thread(
            bot_state_collection.find_one,
            {"_id": STATUS_PANEL_STATE_ID}
        )
        if saved_panel and saved_panel.get("message_id"):
            STATUS_MESSAGE_ID = int(saved_panel["message_id"])

    if STATUS_MESSAGE_ID:
        try:
            message = await channel.fetch_message(STATUS_MESSAGE_ID)
        except discord.NotFound:
            STATUS_MESSAGE_ID = None
        except (discord.Forbidden, discord.HTTPException) as error:
            print(f"❌ Nie udało się pobrać panelu statusu: {error}")
            return

    if message is None:
        known_titles = {
            "🕹️ Kaciej Arcade • Status serwera",
            "🎮 KACIEJ ARCADE",
            "🎮 STATUS SERWERÓW KACIEJOS"
        }
        try:
            async for previous_message in channel.history(limit=None):
                if (
                    previous_message.author == bot.user
                    and previous_message.embeds
                    and previous_message.embeds[0].title in known_titles
                ):
                    message = previous_message
                    break
        except (discord.Forbidden, discord.HTTPException) as error:
            print(f"❌ Nie udało się odnaleźć starego panelu statusu: {error}")
            return

    try:
        if message is None:
            message = await channel.send(embed=embed)
        else:
            await message.edit(embed=embed)
    except (discord.Forbidden, discord.HTTPException) as error:
        print(f"❌ Nie udało się zaktualizować panelu statusu: {error}")
        return

    STATUS_MESSAGE_ID = message.id
    await asyncio.to_thread(
        bot_state_collection.update_one,
        {"_id": STATUS_PANEL_STATE_ID},
        {"$set": {
            "channel_id": channel.id,
            "message_id": message.id,
            "updated_at": now.isoformat()
        }},
        upsert=True
    )

@update_server_status.before_loop
async def before_update_server_status():
    await bot.wait_until_ready()

@update_server_status.error
async def update_server_status_error(error):
    print(
        "❌ Pętla statusu Kaciej Arcade zatrzymała się: "
        f"{type(error).__name__}: {error}"
    )

    async def restart_status_loop():
        await asyncio.sleep(10)
        if not bot.is_closed() and not update_server_status.is_running():
            print("🔄 Ponowne uruchamianie pętli statusu Kaciej Arcade")
            update_server_status.start()

    asyncio.create_task(restart_status_loop())

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

    if message.id in BOT_REMOVING_ABSENCE_MESSAGE_IDS:
        BOT_REMOVING_ABSENCE_MESSAGE_IDS.discard(message.id)
        return

    if isinstance(message.channel, discord.Thread):
        recording = await asyncio.to_thread(
            recordings_collection.find_one,
            {"forum_thread_ids": message.channel.id}
        )
        if recording is not None:
            absence_was_active = True
            if recording.get("double_group_id"):
                selection = await asyncio.to_thread(
                    recording_absences_collection.find_one_and_delete,
                    {
                        "group_id": recording["double_group_id"],
                        "user_id": message.author.id,
                        "forum_id": message.channel.parent_id,
                        "reason_message_id": message.id,
                        "confirmed": True
                    }
                )
                absence_was_active = selection is not None
                if selection is not None:
                    await synchronize_double_group_attendance(
                        recording["double_group_id"]
                    )

            if absence_was_active:
                absence_log_channel = bot.get_channel(NAGRYWKI_LOGS_CHANNEL_ID)
                if absence_log_channel is None:
                    try:
                        absence_log_channel = await bot.fetch_channel(
                            NAGRYWKI_LOGS_CHANNEL_ID
                        )
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        absence_log_channel = None

                if absence_log_channel is not None:
                    announcement_id = int(recording.get(
                        "announcement_message_id", recording["message_id"]
                    ))
                    announcement_url = (
                        f"https://discord.com/channels/{GUILD_ID}/"
                        f"{NAGRYWKI_CHANNEL_ID}/{announcement_id}"
                    )
                    absence_embed = discord.Embed(
                        title="↩️ Użytkownik cofnął swoją nieobecność",
                        description=(
                            f"{message.author.mention} samodzielnie usunął wiadomość "
                            "ze zgłoszeniem nieobecności."
                        ),
                        color=discord.Color.orange(),
                        timestamp=datetime.now(ZoneInfo("Europe/Warsaw"))
                    )
                    absence_embed.add_field(
                        name="🎬 Nagrywka",
                        value=(
                            f"**{recording_display_name(recording)}**"
                            + (
                                f" • etap **{recording.get('double_position')}/2**"
                                if recording.get("double_group_id") else ""
                            )
                        ),
                        inline=False
                    )
                    absence_embed.add_field(
                        name="📅 Termin",
                        value=f"{recording['data']} • {recording['godzina']}",
                        inline=True
                    )
                    absence_embed.add_field(
                        name="🔗 Nagrywka",
                        value=f"[Przejdź do wiadomości]({announcement_url})",
                        inline=False
                    )
                    absence_embed.set_thumbnail(url=message.author.display_avatar.url)
                    absence_embed.set_footer(text=f"ID użytkownika: {message.author.id}")
                    try:
                        await absence_log_channel.send(
                            embed=absence_embed,
                            allowed_mentions=discord.AllowedMentions.none()
                        )
                    except (discord.Forbidden, discord.HTTPException) as error:
                        print(f"❌ Nie udało się wysłać logu samodzielnie cofniętej nieobecności: {error}")

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
async def on_raw_message_delete(payload):
    """Obsługuje stare, niebuforowane wiadomości nieobecności X2."""
    selection = await asyncio.to_thread(
        recording_absences_collection.find_one_and_delete,
        {"reason_message_id": payload.message_id, "confirmed": True}
    )
    if selection is None:
        return

    recordings = await asyncio.to_thread(
        lambda: list(recordings_collection.find({
            "double_group_id": selection["group_id"]
        }))
    )
    if not recordings:
        return

    await synchronize_double_group_attendance(selection["group_id"])
    selected_ids = {
        int(value) for value in selection.get("recording_message_ids", [])
    }
    selected_recordings = [
        recording for recording in recordings
        if int(recording["message_id"]) in selected_ids
    ]
    selected_names = ", ".join(
        f"{recording_display_name(recording)} ({recording.get('double_position')}/2)"
        for recording in selected_recordings
    ) or "Nagrywka X2"

    log_channel = bot.get_channel(NAGRYWKI_LOGS_CHANNEL_ID)
    if log_channel is None:
        try:
            log_channel = await bot.fetch_channel(NAGRYWKI_LOGS_CHANNEL_ID)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return

    first_recording = recordings[0]
    announcement_id = int(first_recording["announcement_message_id"])
    announcement_url = (
        f"https://discord.com/channels/{GUILD_ID}/"
        f"{NAGRYWKI_CHANNEL_ID}/{announcement_id}"
    )
    embed = discord.Embed(
        title="↩️ Użytkownik cofnął swoją nieobecność",
        description=(
            f"<@{selection['user_id']}> samodzielnie usunął wiadomość "
            "ze zgłoszeniem nieobecności."
        ),
        color=discord.Color.orange(),
        timestamp=datetime.now(ZoneInfo("Europe/Warsaw"))
    )
    embed.add_field(name="🎬 Nagrywka", value=f"**{selected_names}**", inline=False)
    embed.add_field(
        name="📅 Termin",
        value=f"{first_recording['data']} • {first_recording['godzina']}",
        inline=True
    )
    embed.add_field(
        name="🔗 Nagrywka",
        value=f"[Przejdź do wiadomości]({announcement_url})",
        inline=False
    )
    embed.set_footer(text=f"ID użytkownika: {selection['user_id']}")
    try:
        await log_channel.send(
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none()
        )
    except (discord.Forbidden, discord.HTTPException) as error:
        print(f"❌ Nie udało się wysłać logu usuniętej nieobecności X2: {error}")

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
    nagrywki = await asyncio.to_thread(load_recordings)
    if str(payload.message_id) in nagrywki:

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

            absence_authors_by_forum = await collect_absence_authors(
                nagrywka.get("forum_thread_ids", []),
                nagrywka
            )
            absent_user_ids = {
                user_id
                for authors in absence_authors_by_forum.values()
                for user_id in authors
            }
            if payload.user_id in absent_user_ids:
                await message.remove_reaction("✅", member)
                try:
                    await member.send(
                        "❌ Masz zapisaną nieobecność na tę nagrywkę. "
                        "Poproś administrację o użycie `/cofnijnieobecnosc`, "
                        "zanim ponownie potwierdzisz obecność."
                    )
                except (discord.Forbidden, discord.HTTPException):
                    pass
                return

            if payload.user_id not in nagrywka["uczestnicy"]:

                nagrywka["uczestnicy"].append(
                    payload.user_id
                )

                await asyncio.to_thread(save_recordings, nagrywki)

                embed = message.embeds[0]

                embed.set_field_at(
                    3,
                    name="✅ Potwierdzone osoby",
                    value=(
                        f"**{len(nagrywka['uczestnicy'])}** "
                        f"{polish_people_word(len(nagrywka['uczestnicy']))}"
                    ),
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
    nagrywki = await asyncio.to_thread(load_recordings)
    if str(payload.message_id) in nagrywki:

        nagrywka = nagrywki[str(payload.message_id)]

        if str(payload.emoji) == "✅":

            if payload.user_id in nagrywka["uczestnicy"]:

                nagrywka["uczestnicy"].remove(
                    payload.user_id
                )

                await asyncio.to_thread(save_recordings, nagrywki)

                embed = message.embeds[0]

                embed.set_field_at(
                    3,
                    name="✅ Potwierdzone osoby",
                    value=(
                        f"**{len(nagrywka['uczestnicy'])}** "
                        f"{polish_people_word(len(nagrywka['uczestnicy']))}"
                    ),
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
        nagrywki = await asyncio.to_thread(load_recordings)
        tracking_changed = False
        now = datetime.now(ZoneInfo("Europe/Warsaw"))

        for nagrywka in nagrywki.values():
            if nagrywka.get("stage_waiting", False):
                continue
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
                is_first_entry = user_key not in first_joined_at
                if tracking_window_started and is_first_entry:
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

                if (
                    not member.bot
                    and any(
                        role.id in {NAGRYWKOWICZE_ROLE_ID, TESTOWI_ROLE_ID}
                        for role in member.roles
                    )
                ):
                    nagrywka.setdefault("voice_exit_events", []).append({
                        "user_id": member.id,
                        "left_at": now.isoformat()
                    })
                    tracking_changed = True

                    exit_log_channel = bot.get_channel(LATE_EXIT_LOG_CHANNEL_ID)
                    if exit_log_channel:
                        exit_embed = discord.Embed(
                            title="🚪 Wyjście z trwającej nagrywki",
                            description=f"{member.mention} opuścił kanał nagrywkowy.",
                            color=discord.Color.orange(),
                            timestamp=now
                        )
                        exit_embed.set_thumbnail(url=member.display_avatar.url)
                        exit_embed.add_field(
                            name="🎬 Nagrywka",
                            value=f"**{nagrywka['opis']}**\n{nagrywka['data']} • {nagrywka['godzina']}",
                            inline=False
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
            await asyncio.to_thread(save_recordings, nagrywki)

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
KACIEJ_USER_ID = 1042829196747091988
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

def polish_people_word(count):
    if count == 1:
        return "osoba"
    if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        return "osoby"
    return "osób"

def recording_display_name(nagrywka):
    number = nagrywka.get("recording_number")
    if number is not None:
        return f"Nagrywka #{number}"
    return nagrywka.get("opis", "Nagrywka")

def next_recording_number():
    historical_count = recording_stats_collection.count_documents({})
    highest_stat = recording_stats_collection.find_one(
        {"recording_number": {"$exists": True}},
        sort=[("recording_number", -1)]
    )
    base_number = max(
        historical_count,
        int(highest_stat.get("recording_number") or 0) if highest_stat else 0
    )
    bot_counters_collection.update_one(
        {"_id": "recording_number"},
        {"$max": {"value": base_number}},
        upsert=True
    )
    counter = bot_counters_collection.find_one_and_update(
        {"_id": "recording_number"},
        {"$inc": {"value": 1}},
        return_document=ReturnDocument.AFTER
    )
    return int(counter["value"])

def recording_forum_content(opis, data, godzina):
    return (
        "🎬 **Termin nagrywki**\n\n"
        f"📅 **Data:** {data}\n"
        f"🕒 **Godzina:** {godzina} (Europe/Warsaw)\n"
        f"🔊 **Kanał VC:** <#{NAGRYWKI_VC_ID}>\n\n"
        "Jeżeli nie możesz pojawić się na nagrywce, zgłoś swoją nieobecność w tym poście."
    )

def double_recording_forum_content(recordings):
    first, second = recordings
    return (
        "🎬 **Podwójna nagrywka — zgłoszenie nieobecności**\n\n"
        f"📅 **Start całości:** {first['data']} • {first['godzina']}\n"
        f"1️⃣ **{recording_display_name(first)}:** od rozpoczęcia nagrywki\n"
        f"2️⃣ **{recording_display_name(second)}:** po zakończeniu etapu 1/2\n"
        f"🔊 **Kanał VC:** <#{NAGRYWKI_VC_ID}>\n\n"
        "### Jak zgłosić nieobecność?\n"
        "**1.** Wybierz z listy etap 1/2, etap 2/2 albo oba.\n"
        "**2.** Napisz poniżej powód nieobecności.\n\n"
        "🔒 Po wysłaniu powodu nieobecność zostanie zapisana, a bot usunie Twoje "
        "potwierdzenie z wybranych etapów. Ponowny zapis będzie możliwy dopiero po "
        "cofnięciu nieobecności przez administrację."
    )

class DoubleAbsenceSelect(Select):
    def __init__(self, group_id, recordings):
        self.group_id = group_id
        self.recordings = recordings
        first, second = recordings
        super().__init__(
            placeholder="Najpierw wybierz etap nieobecności...",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label="Nie będzie mnie na etapie 1/2",
                    description=f"{recording_display_name(first)} — od rozpoczęcia",
                    value=str(first["message_id"]),
                    emoji="1️⃣"
                ),
                discord.SelectOption(
                    label="Nie będzie mnie na etapie 2/2",
                    description=f"{recording_display_name(second)} — po etapie 1/2",
                    value=str(second["message_id"]),
                    emoji="2️⃣"
                ),
                discord.SelectOption(
                    label="Nie będzie mnie na obu etapach",
                    description="Nieobecność 1/2 oraz 2/2",
                    value="both",
                    emoji="⏩"
                )
            ],
            custom_id=f"double_absence:{group_id}"
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        allowed_roles = {NAGRYWKOWICZE_ROLE_ID, TESTOWI_ROLE_ID}
        if not any(role.id in allowed_roles for role in interaction.user.roles):
            await interaction.edit_original_response(
                content="❌ To zgłoszenie jest dostępne tylko dla pomocników i testowych."
            )
            return

        existing_absence = await asyncio.to_thread(
            recording_absences_collection.find_one,
            {
                "group_id": self.group_id,
                "user_id": interaction.user.id,
                "forum_id": interaction.channel.parent_id,
                "confirmed": True
            }
        )
        if existing_absence is not None:
            await interaction.edit_original_response(
                content=(
                    "❌ Masz już zapisaną nieobecność dla tej nagrywki. "
                    "Jeżeli chcesz ją zmienić lub wrócić na listę obecnych, "
                    "administracja musi najpierw użyć `/cofnijnieobecnosc`."
                )
            )
            return

        selected_ids = (
            [int(recording["message_id"]) for recording in self.recordings]
            if self.values[0] == "both"
            else [int(self.values[0])]
        )
        forum_id = interaction.channel.parent_id
        await asyncio.to_thread(
            recording_absences_collection.update_one,
            {
                "group_id": self.group_id,
                "user_id": interaction.user.id,
                "forum_id": forum_id
            },
            {"$set": {
                "recording_message_ids": selected_ids,
                "confirmed": False,
                "status": "awaiting_reason",
                "reason_message_id": None,
                "updated_at": datetime.now(ZoneInfo("Europe/Warsaw")).isoformat()
            }},
            upsert=True
        )
        selected_names = [
            recording_display_name(recording)
            for recording in self.recordings
            if int(recording["message_id"]) in selected_ids
        ]
        await interaction.edit_original_response(
            content=(
                "📝 Wybrano: **" + " i ".join(selected_names) + "**.\n"
                "Teraz napisz w tym poście **powód nieobecności**. Po jego wysłaniu "
                "nieobecność zostanie zapisana i zablokuje ponowny zapis."
            )
        )

class DoubleAbsenceView(View):
    def __init__(self, group_id, recordings):
        super().__init__(timeout=None)
        self.add_item(DoubleAbsenceSelect(group_id, recordings))

async def restore_double_absence_views():
    documents = await asyncio.to_thread(
        lambda: list(recordings_collection.find({"double_group_id": {"$exists": True}}))
    )
    groups = {}
    for document in documents:
        groups.setdefault(document["double_group_id"], []).append(document)

    for group_id, recordings in groups.items():
        if len(recordings) != 2:
            continue
        recordings.sort(key=lambda item: item.get("timestamp", ""))
        thread_ids = recordings[0].get("forum_thread_ids", [])
        for thread_id in thread_ids:
            bot.add_view(
                DoubleAbsenceView(group_id, recordings),
                message_id=int(thread_id)
            )

def build_double_recording_embed(recordings):
    recordings = sorted(recordings, key=lambda item: item.get("double_position", 0))
    embed = discord.Embed(
        title="🎬 PODWÓJNA NAGRYWKA",
        description=(
            "### 📢 Jeden start — dwa osobno rozliczane etapy\n"
            "**Jak się zapisać?**\n"
            "• Będziesz cały czas → wybierz **Będę na obu**.\n"
            "• Będziesz tylko na części → wybierz **Będę na jednym** i wskaż etap.\n"
            "• Spóźnisz się → wybierz **Spóźnię się** i podaj powód."
        ),
        color=discord.Color.blurple()
    )
    for fallback_position, recording in enumerate(recordings, start=1):
        position = int(recording.get("double_position", fallback_position))
        try:
            timestamp = datetime.fromisoformat(recording["timestamp"])
            relative = f"<t:{int(timestamp.timestamp())}:R>"
        except (KeyError, TypeError, ValueError):
            relative = "brak danych"
        participant_count = len(recording.get("uczestnicy", []))
        if recording.get("stage_waiting", False):
            timing_text = "▶️ **Start po zakończeniu etapu 1/2**"
        else:
            timing_text = (
                f"📅 **{recording['data']}** • 🕒 **{recording['godzina']}**\n"
                f"⏳ {relative}"
            )
        embed.add_field(
            name=f"{position}️⃣ {recording_display_name(recording)}",
            value=(
                f"{timing_text}\n"
                f"✅ Zapisani: **{participant_count} {polish_people_word(participant_count)}**"
            ),
            inline=False
        )
    embed.add_field(
        name="🔊 Miejsce spotkania",
        value=f"<#{NAGRYWKI_VC_ID}>",
        inline=False
    )
    if bot.user:
        embed.set_thumbnail(url=bot.user.display_avatar.url)
    embed.set_footer(text="NegativE* • Każdy termin jest liczony osobno w statystykach")
    return embed

async def refresh_double_announcement_after_removal(recording, remaining_recordings, action):
    group_id = recording.get("double_group_id")
    if not group_id:
        return
    channel = bot.get_channel(NAGRYWKI_CHANNEL_ID)
    if channel is None:
        return
    try:
        message = await channel.fetch_message(int(recording["announcement_message_id"]))
        remaining = sorted(
            [
                item for item in remaining_recordings.values()
                if item.get("double_group_id") == group_id
            ],
            key=lambda item: item.get("double_position", 0)
        )
        if remaining:
            embed = build_double_recording_embed(remaining)
            embed.description = (
                f"### {action}: {recording_display_name(recording)}\n"
                "Poniżej pozostały aktywny termin z podwójnej nagrywki."
            )
        else:
            embed = discord.Embed(
                title="✅ PODWÓJNA NAGRYWKA ZAKOŃCZONA",
                description="Oba terminy zostały zakończone albo usunięte.",
                color=discord.Color.green(),
                timestamp=datetime.now(ZoneInfo("Europe/Warsaw"))
            )
        await message.edit(embed=embed, view=None)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass

async def update_double_lateness_report(group_id, recordings):
    recordings = sorted(recordings, key=lambda item: item.get("double_position", 0))
    choices = await asyncio.to_thread(
        lambda: list(recording_attendance_choices_collection.find({
            "group_id": group_id,
            "status": "late",
            "late_reason": {"$nin": [None, ""]}
        }))
    )
    report_data = await asyncio.to_thread(
        recording_lateness_reports_collection.find_one,
        {"group_id": group_id}
    )
    if not choices and not report_data:
        return

    report_channel = bot.get_channel(REPORT_CHANNEL_ID)
    if report_channel is None:
        return

    embed = discord.Embed(
        title="⏰ RAPORT SPÓŹNIEŃ — NAGRYWKA X2",
        description=(
            "Zbiorcza lista zapowiedzianych spóźnień. "
            "Raport aktualizuje się po każdym nowym zgłoszeniu."
        ),
        color=discord.Color.orange(),
        timestamp=datetime.now(ZoneInfo("Europe/Warsaw"))
    )
    for position, recording in enumerate(recordings, start=1):
        recording_id = int(recording["message_id"])
        entries = []
        for choice in choices:
            selected_ids = {
                int(value) for value in choice.get("recording_message_ids", [])
            }
            if recording_id not in selected_ids:
                continue
            entries.append(
                f"<@{choice['user_id']}> — {choice['late_reason']}"
            )
        stage_timing = (
            f"{recording['data']} o {recording['godzina']}"
            if position == 1 else
            "po zakończeniu etapu 1/2"
        )
        embed.add_field(
            name=(
                f"{position}/2 • {recording_display_name(recording)} • "
                f"{stage_timing}"
            ),
            value=("\n".join(entries)[:1024] if entries else "Brak zgłoszonych spóźnień."),
            inline=False
        )
    embed.set_footer(text="NegativE* • Aktualizowany raport administracyjny")

    report_message = None
    if report_data and report_data.get("message_id"):
        try:
            report_message = await report_channel.fetch_message(
                int(report_data["message_id"])
            )
            await report_message.edit(
                embed=embed,
                allowed_mentions=discord.AllowedMentions.none()
            )
            await asyncio.to_thread(
                recording_lateness_reports_collection.update_one,
                {"group_id": group_id},
                {"$set": {
                    "updated_at": datetime.now(ZoneInfo("Europe/Warsaw")).isoformat()
                }}
            )
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            report_message = None

    if report_message is None:
        try:
            report_message = await report_channel.send(
                embed=embed,
                allowed_mentions=discord.AllowedMentions.none()
            )
        except (discord.Forbidden, discord.HTTPException):
            return
        await asyncio.to_thread(
            recording_lateness_reports_collection.update_one,
            {"group_id": group_id},
            {"$set": {
                "message_id": report_message.id,
                "channel_id": report_channel.id,
                "updated_at": datetime.now(ZoneInfo("Europe/Warsaw")).isoformat()
            }},
            upsert=True
        )

async def save_double_attendance_choice(
    interaction, group_id, selected_ids, status, late_reason=None
):
    recordings = await asyncio.to_thread(
        lambda: list(recordings_collection.find({"double_group_id": group_id}))
    )
    if len(recordings) != 2:
        await interaction.edit_original_response(content="❌ Ta podwójna nagrywka nie jest już aktywna.")
        return

    selected_id_set = {int(value) for value in selected_ids}
    confirmed_absences = await asyncio.to_thread(
        lambda: list(recording_absences_collection.find({
            "group_id": group_id,
            "user_id": interaction.user.id,
            "confirmed": True
        }))
    )
    absent_ids = {
        int(value)
        for absence in confirmed_absences
        for value in absence.get("recording_message_ids", [])
    }
    conflicting_ids = selected_id_set & absent_ids
    if conflicting_ids:
        conflicting_names = [
            recording_display_name(recording)
            for recording in recordings
            if int(recording["message_id"]) in conflicting_ids
        ]
        await interaction.edit_original_response(
            content=(
                "❌ Masz już zapisaną nieobecność na: **"
                + " i ".join(conflicting_names)
                + "**. Administracja musi najpierw użyć `/cofnijnieobecnosc`."
            )
        )
        return

    for recording in recordings:
        recording_id = int(recording["message_id"])
        if recording_id in selected_id_set:
            update = {
                "$pull": {"uczestnicy": str(interaction.user.id)}
            }
            await asyncio.to_thread(
                recordings_collection.update_one,
                {"_id": recording["_id"]},
                update
            )
            await asyncio.to_thread(
                recordings_collection.update_one,
                {"_id": recording["_id"]},
                {"$addToSet": {"uczestnicy": interaction.user.id}}
            )
        else:
            await asyncio.to_thread(
                recordings_collection.update_one,
                {"_id": recording["_id"]},
                {"$pull": {
                    "uczestnicy": {"$in": [interaction.user.id, str(interaction.user.id)]}
                }}
            )
    await asyncio.to_thread(
        recording_attendance_choices_collection.update_one,
        {"group_id": group_id, "user_id": interaction.user.id},
        {"$set": {
            "recording_message_ids": [int(value) for value in selected_ids],
            "status": status,
            "late_reason": late_reason,
            "updated_at": datetime.now(ZoneInfo("Europe/Warsaw")).isoformat()
        }},
        upsert=True
    )

    refreshed = await asyncio.to_thread(
        lambda: list(recordings_collection.find({"double_group_id": group_id}))
    )
    announcement_id = int(refreshed[0]["announcement_message_id"])
    channel = bot.get_channel(NAGRYWKI_CHANNEL_ID)
    if channel is not None:
        try:
            announcement = await channel.fetch_message(announcement_id)
            await announcement.edit(
                embed=build_double_recording_embed(refreshed),
                view=DoubleAttendanceView(group_id, refreshed)
            )
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

    selected_names = [
        recording_display_name(recording)
        for recording in refreshed
        if int(recording["message_id"]) in {int(value) for value in selected_ids}
    ]
    status_text = "obecność" if status == "present" else "spóźnienie"
    counts_text = " • ".join(
        f"{recording.get('double_position')}/2: **{len(recording.get('uczestnicy', []))}**"
        for recording in sorted(refreshed, key=lambda item: item.get("double_position", 0))
    )
    response_text = (
        f"✅ Zapisano **{status_text}**: **{' i '.join(selected_names)}**.\n"
        f"👥 Aktualnie zapisani — {counts_text}"
    )
    if status == "present" and len(selected_ids) == 1:
        response_text += (
            "\n⚠️ **Na drugi termin musisz zgłosić nieobecność** "
            "w odpowiednim poście nieobecności."
        )
    await interaction.edit_original_response(content=response_text)

    log_channel = bot.get_channel(NAGRYWKI_LOGS_CHANNEL_ID)
    if log_channel is not None:
        log_embed = discord.Embed(
            title=(
                "⏰ Zgłoszono spóźnienie — nagrywka X2"
                if status == "late" else
                "✅ Zapisano obecność — nagrywka X2"
            ),
            description=(
                f"{interaction.user.mention} wybrał(a): "
                f"**{' i '.join(selected_names)}**."
            ),
            color=(
                discord.Color.orange()
                if status == "late" else discord.Color.green()
            ),
            timestamp=datetime.now(ZoneInfo("Europe/Warsaw"))
        )
        sorted_recordings = sorted(
            refreshed,
            key=lambda item: item.get("double_position", 0)
        )
        first_recording, second_recording = sorted_recordings
        first_count = len(first_recording.get("uczestnicy", []))
        second_count = len(second_recording.get("uczestnicy", []))
        log_embed.add_field(
            name=f"1️⃣ Etap 1/2 — {recording_display_name(first_recording)}",
            value=(
                f"👥 Zapisani: **{first_count} "
                f"{polish_people_word(first_count)}**\n"
                f"📅 {first_recording['data']} • 🕒 {first_recording['godzina']}"
            ),
            inline=False
        )
        log_embed.add_field(
            name=f"2️⃣ Etap 2/2 — {recording_display_name(second_recording)}",
            value=(
                f"👥 Zapisani: **{second_count} "
                f"{polish_people_word(second_count)}**\n"
                "▶️ Start po zakończeniu etapu 1/2"
            ),
            inline=False
        )
        if late_reason:
            log_embed.add_field(
                name="📝 Powód spóźnienia",
                value=late_reason[:1024],
                inline=False
            )
        log_embed.set_thumbnail(url=interaction.user.display_avatar.url)
        log_embed.set_footer(
            text=f"Grupa: {group_id} • ID użytkownika: {interaction.user.id}"
        )
        try:
            await log_channel.send(
                embed=log_embed,
                allowed_mentions=discord.AllowedMentions.none()
            )
            await asyncio.to_thread(
                recording_attendance_choices_collection.update_one,
                {"group_id": group_id, "user_id": interaction.user.id},
                {"$set": {
                    "logged_at": datetime.now(ZoneInfo("Europe/Warsaw")).isoformat()
                }}
            )
        except (discord.Forbidden, discord.HTTPException):
            pass

    await update_double_lateness_report(group_id, refreshed)

async def backfill_double_attendance_logs(force_group_id=None):
    log_channel = bot.get_channel(NAGRYWKI_LOGS_CHANNEL_ID)
    if log_channel is None:
        try:
            log_channel = await bot.fetch_channel(NAGRYWKI_LOGS_CHANNEL_ID)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            print(f"❌ Nie znaleziono kanału nagrywki-logi: {NAGRYWKI_LOGS_CHANNEL_ID}")
            return 0

    # Zachowujemy treść istniejących logów. Samo ID użytkownika nie wystarcza,
    # bo ta sama osoba może mieć logi z wielu różnych nagrywek X2.
    historical_log_embeds = []
    try:
        async for message in log_channel.history(limit=100):
            if not message.embeds:
                continue
            embed = message.embeds[0]
            if "nagrywka X2" not in (embed.title or ""):
                continue
            historical_log_embeds.append(embed)
    except (discord.Forbidden, discord.HTTPException):
        pass

    active_recordings = await asyncio.to_thread(
        lambda: list(recordings_collection.find({
            "double_group_id": {"$exists": True}
        }))
    )
    groups = {}
    for recording in active_recordings:
        groups.setdefault(recording["double_group_id"], []).append(recording)

    restored_count = 0
    for group_id, recordings in groups.items():
        if len(recordings) != 2:
            continue
        recordings = sorted(
            recordings,
            key=lambda item: item.get("double_position", 0)
        )
        pending_choices = await asyncio.to_thread(
            lambda current_group=group_id: list(
                recording_attendance_choices_collection.find({"group_id": current_group})
            )
        )
        for choice in pending_choices:
            recording_names = {
                recording_display_name(recording) for recording in recordings
            }
            user_marker = f"ID użytkownika: {choice['user_id']}"
            mention_marker = f"<@{choice['user_id']}>"
            log_already_exists = any(
                (
                    user_marker in (old_embed.footer.text or "")
                    or mention_marker in (old_embed.description or "")
                )
                and any(
                    name in (old_embed.description or "")
                    or any(name in field.name or name in field.value for field in old_embed.fields)
                    for name in recording_names
                )
                for old_embed in historical_log_embeds
            )
            if log_already_exists and group_id != force_group_id:
                await asyncio.to_thread(
                    recording_attendance_choices_collection.update_one,
                    {"_id": choice["_id"]},
                    {"$set": {
                        "logged_at": datetime.now(ZoneInfo("Europe/Warsaw")).isoformat(),
                        "log_found_in_history": True
                    }}
                )
                continue
            selected_ids = {
                int(value) for value in choice.get("recording_message_ids", [])
            }
            selected_names = [
                recording_display_name(recording)
                for recording in recordings
                if int(recording["message_id"]) in selected_ids
            ]
            if not selected_names:
                continue

            status = choice.get("status", "present")
            embed = discord.Embed(
                title=(
                    "♻️ Zaległy log spóźnienia — nagrywka X2"
                    if status == "late" else
                    "♻️ Zaległy log obecności — nagrywka X2"
                ),
                description=(
                    f"<@{choice['user_id']}> wybrał(a): "
                    f"**{' i '.join(selected_names)}**."
                ),
                color=(
                    discord.Color.orange()
                    if status == "late" else discord.Color.green()
                ),
                timestamp=datetime.now(ZoneInfo("Europe/Warsaw"))
            )
            for recording in recordings:
                count = len(recording.get("uczestnicy", []))
                position = int(recording.get("double_position", 0))
                timing = (
                    f"📅 {recording['data']} • 🕒 {recording['godzina']}"
                    if position == 1 else
                    "▶️ Start po zakończeniu etapu 1/2"
                )
                embed.add_field(
                    name=f"{position}️⃣ Etap {position}/2 — {recording_display_name(recording)}",
                    value=(
                        f"👥 Zapisani: **{count} {polish_people_word(count)}**\n"
                        f"{timing}"
                    ),
                    inline=False
                )
            if choice.get("late_reason"):
                embed.add_field(
                    name="📝 Powód spóźnienia",
                    value=str(choice["late_reason"])[:1024],
                    inline=False
                )
            embed.set_footer(
                text=(
                    f"Nadrobiony automatycznie • Grupa: {group_id} • "
                    f"ID użytkownika: {choice['user_id']}"
                )
            )
            try:
                await log_channel.send(
                    embed=embed,
                    allowed_mentions=discord.AllowedMentions.none()
                )
            except (discord.Forbidden, discord.HTTPException) as error:
                print(f"❌ Nie udało się nadrobić logu X2 użytkownika {choice['user_id']}: {error}")
                continue

            await asyncio.to_thread(
                recording_attendance_choices_collection.update_one,
                {"_id": choice["_id"]},
                {"$set": {
                    "logged_at": datetime.now(ZoneInfo("Europe/Warsaw")).isoformat(),
                    "log_backfilled": True
                }}
            )
            restored_count += 1

    if restored_count:
        print(f"✅ Nadrobiono zaległe logi zapisów X2: {restored_count}")
    return restored_count

class DoubleLateReasonModal(Modal, title="Powód spóźnienia"):
    reason = TextInput(
        label="Dlaczego się spóźnisz?",
        placeholder="Wpisz powód spóźnienia...",
        style=discord.TextStyle.paragraph,
        required=True,
        min_length=3,
        max_length=500
    )

    def __init__(self, group_id, selected_ids):
        super().__init__()
        self.group_id = group_id
        self.selected_ids = selected_ids

    async def on_submit(self, interaction: discord.Interaction):
        reason = str(self.reason.value).strip()
        if len(reason) < 3:
            await interaction.response.send_message(
                "❌ Podaj prawdziwy powód spóźnienia (minimum 3 znaki).",
                ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await save_double_attendance_choice(
            interaction,
            self.group_id,
            self.selected_ids,
            "late",
            late_reason=reason
        )

class DoubleAttendanceTermSelect(Select):
    def __init__(self, group_id, recordings, status):
        self.group_id = group_id
        self.status = status
        recordings = sorted(recordings, key=lambda item: item.get("double_position", 0))
        options = [
            discord.SelectOption(
                label=f"{recording_display_name(recording)}"[:100],
                description=(
                    f"{recording['data']} • {recording['godzina']}"
                    if position == 1 else
                    "Rozpocznie się po zakończeniu etapu 1/2"
                ),
                value=str(recording["message_id"]),
                emoji=f"{position}️⃣"
            )
            for position, recording in enumerate(recordings, start=1)
        ]
        if status == "late":
            options.append(discord.SelectOption(
                label="Spóźnię się na obie",
                value="both",
                emoji="⏰"
            ))
        super().__init__(
            placeholder=(
                "Wybierz nagrywkę, na której będziesz..."
                if status == "present" else "Wybierz nagrywkę, na którą się spóźnisz..."
            ),
            min_values=1,
            max_values=1,
            options=options
        )
        self.recordings = recordings

    async def callback(self, interaction: discord.Interaction):
        selected_ids = (
            [int(recording["message_id"]) for recording in self.recordings]
            if self.values[0] == "both" else [int(self.values[0])]
        )
        if self.status == "late":
            await interaction.response.send_modal(
                DoubleLateReasonModal(self.group_id, selected_ids)
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        await save_double_attendance_choice(
            interaction, self.group_id, selected_ids, self.status
        )

class DoubleAttendanceTermView(View):
    def __init__(self, group_id, recordings, status):
        super().__init__(timeout=120)
        self.add_item(DoubleAttendanceTermSelect(group_id, recordings, status))

class DoubleAttendanceView(View):
    def __init__(self, group_id, recordings):
        super().__init__(timeout=None)
        self.group_id = group_id
        self.recordings = sorted(recordings, key=lambda item: item.get("double_position", 0))

    @discord.ui.button(
        label="Będę na obu etapach",
        emoji="✅",
        style=discord.ButtonStyle.success,
        custom_id="double_attendance:both"
    )
    async def present_both(self, interaction: discord.Interaction, button: Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await save_double_attendance_choice(
            interaction,
            self.group_id,
            [int(recording["message_id"]) for recording in self.recordings],
            "present"
        )

    @discord.ui.button(
        label="Będę na jednym etapie",
        emoji="1️⃣",
        style=discord.ButtonStyle.primary,
        custom_id="double_attendance:one"
    )
    async def present_one(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_message(
            (
                "Wybierz etap, na którym będziesz obecny.\n"
                "⚠️ Na drugi etap musisz później zgłosić nieobecność "
                "w odpowiednim poście."
            ),
            view=DoubleAttendanceTermView(self.group_id, self.recordings, "present"),
            ephemeral=True
        )

    @discord.ui.button(
        label="Spóźnię się",
        emoji="⏰",
        style=discord.ButtonStyle.secondary,
        custom_id="double_attendance:late"
    )
    async def late(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_message(
            "Wybierz etap, na który się spóźnisz. Następnie wpiszesz powód:",
            view=DoubleAttendanceTermView(self.group_id, self.recordings, "late"),
            ephemeral=True
        )

async def restore_double_attendance_views():
    documents = await asyncio.to_thread(
        lambda: list(recordings_collection.find({"double_group_id": {"$exists": True}}))
    )
    groups = {}
    for document in documents:
        groups.setdefault(document["double_group_id"], []).append(document)
    for group_id, recordings in groups.items():
        if len(recordings) == 2:
            bot.add_view(
                DoubleAttendanceView(group_id, recordings),
                message_id=int(recordings[0]["announcement_message_id"])
            )

def build_recording_embed(nagrywka):
    try:
        termin = datetime.fromisoformat(nagrywka["timestamp"])
        if termin.tzinfo is None:
            termin = termin.replace(tzinfo=ZoneInfo("Europe/Warsaw"))
        relative_time = f"<t:{int(termin.timestamp())}:R>"
    except (KeyError, TypeError, ValueError):
        relative_time = "Termin zostanie podany wkrótce"

    participants = len(nagrywka.get("uczestnicy", []))
    embed = discord.Embed(
        title=f"🎬 {recording_display_name(nagrywka).upper()}",
        description=(
            "### 📢 Nowy termin został zaplanowany!\n"
            "Sprawdź szczegóły i kliknij reakcję ✅, aby potwierdzić swój udział."
        ),
        color=discord.Color.blurple()
    )
    embed.add_field(name="📅 Data", value=f"**{nagrywka['data']}**", inline=True)
    embed.add_field(name="🕒 Godzina", value=f"**{nagrywka['godzina']}**", inline=True)
    embed.add_field(
        name="🔊 Miejsce spotkania",
        value=f"<#{NAGRYWKI_VC_ID}>\nStart {relative_time}",
        inline=False
    )
    embed.add_field(
        name="✅ Potwierdzone osoby",
        value=f"**{participants}** {polish_people_word(participants)}",
        inline=False
    )
    if bot.user:
        embed.set_thumbnail(url=bot.user.display_avatar.url)
    message_id = nagrywka.get("message_id")
    footer = "NegativE* • Reakcja ✅ oznacza potwierdzenie udziału"
    if message_id:
        footer += f" • ID: {message_id}"
    embed.set_footer(text=footer)
    return embed

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

async def refresh_active_recording_messages():
    nagrywki = await asyncio.to_thread(load_recordings)
    if not nagrywki:
        return

    channel = bot.get_channel(NAGRYWKI_CHANNEL_ID)
    recordings_changed = False
    for message_id, nagrywka in nagrywki.items():
        nagrywka["message_id"] = int(message_id)
        if nagrywka.get("recording_number") is None:
            nagrywka["recording_number"] = await asyncio.to_thread(next_recording_number)
            nagrywka["opis"] = recording_display_name(nagrywka)
            recordings_changed = True

        is_double = bool(nagrywka.get("double_group_id"))
        if is_double and nagrywka.get("double_position") != 1:
            continue

        if is_double and channel is not None:
            paired_recordings = sorted(
                [
                    recording for recording in nagrywki.values()
                    if recording.get("double_group_id") == nagrywka["double_group_id"]
                ],
                key=lambda item: item.get("double_position", 0)
            )
            if len(paired_recordings) == 2:
                try:
                    message = await channel.fetch_message(
                        int(nagrywka["announcement_message_id"])
                    )
                    await message.edit(
                        embed=build_double_recording_embed(paired_recordings),
                        view=DoubleAttendanceView(
                            nagrywka["double_group_id"], paired_recordings
                        )
                    )
                except (discord.NotFound, discord.Forbidden, discord.HTTPException) as error:
                    print(f"❌ Nie udało się odświeżyć podwójnej nagrywki: {error}")

        if channel is not None and not is_double:
            try:
                message = await channel.fetch_message(int(message_id))

                absence_authors_by_forum = await collect_absence_authors(
                    await find_recording_forum_threads(nagrywka),
                    nagrywka
                )
                absent_user_ids = {
                    user_id
                    for authors in absence_authors_by_forum.values()
                    for user_id in authors
                }

                # Reakcje na wiadomości są źródłem prawdy. Dzięki temu po restarcie
                # odzyskamy również potwierdzenia kliknięte zanim zapis nagrywki
                # zdążył trafić do bazy.
                confirmed_ids = []
                for reaction in message.reactions:
                    if str(reaction.emoji) != "✅":
                        continue
                    async for user in reaction.users():
                        if user.bot:
                            continue
                        member = message.guild.get_member(user.id)
                        if member and any(role.id == URLOP_ROLE_ID for role in member.roles):
                            continue
                        if user.id in absent_user_ids:
                            try:
                                await message.remove_reaction("✅", user)
                            except (discord.Forbidden, discord.HTTPException):
                                pass
                            continue
                        confirmed_ids.append(user.id)

                confirmed_ids = list(dict.fromkeys(confirmed_ids))
                if set(confirmed_ids) != set(nagrywka.get("uczestnicy", [])):
                    nagrywka["uczestnicy"] = confirmed_ids
                    recordings_changed = True

                await message.edit(embed=build_recording_embed(nagrywka))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as error:
                print(f"❌ Nie udało się odświeżyć terminu nagrywki {message_id}: {error}")

        for thread_id in await find_recording_forum_threads(nagrywka):
            try:
                thread = bot.get_channel(int(thread_id)) or await bot.fetch_channel(int(thread_id))
                was_archived = thread.archived
                was_locked = thread.locked
                if was_archived or was_locked:
                    await thread.edit(archived=False, locked=False)
                starter_message = await thread.fetch_message(thread.id)
                if nagrywka.get("double_group_id"):
                    paired_recordings = sorted(
                        [
                            recording for recording in nagrywki.values()
                            if recording.get("double_group_id") == nagrywka["double_group_id"]
                        ],
                        key=lambda item: item.get("timestamp", "")
                    )
                    if len(paired_recordings) == 2:
                        await starter_message.edit(
                            content=double_recording_forum_content(paired_recordings),
                            view=DoubleAbsenceView(
                                nagrywka["double_group_id"], paired_recordings
                            )
                        )
                else:
                    await starter_message.edit(content=recording_forum_content(
                        nagrywka["opis"], nagrywka["data"], nagrywka["godzina"]
                    ))
                if was_archived or was_locked:
                    await thread.edit(archived=was_archived, locked=was_locked)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as error:
                print(f"❌ Nie udało się odświeżyć postu nieobecności {thread_id}: {error}")

    if recordings_changed:
        await asyncio.to_thread(save_recordings, nagrywki)

async def sync_recording_reactions():
    """Synchronizuje uczestników w MongoDB z reakcjami pod aktywnym terminem."""
    nagrywki = await asyncio.to_thread(load_recordings)
    channel = bot.get_channel(NAGRYWKI_CHANNEL_ID)
    if channel is None:
        print(f"❌ Synchronizacja reakcji: brak kanału {NAGRYWKI_CHANNEL_ID}")
        return

    # Jeżeli rekord aktywnej nagrywki zniknął z MongoDB, odtwórz go z
    # najnowszej niezakończonej wiadomości na kanale terminów.
    if not nagrywki:
        try:
            recovered_message = None
            async for candidate in channel.history(limit=50):
                if candidate.author.id != bot.user.id or not candidate.embeds:
                    continue

                title = candidate.embeds[0].title or ""
                upper_title = title.upper()
                if (
                    "NAGRYWKA #" in upper_title
                    and "ODWOŁANA" not in upper_title
                    and "ZAKOŃCZONA" not in upper_title
                ):
                    recovered_message = candidate
                    break

            if recovered_message is None:
                print("⚠️ Synchronizacja reakcji: brak aktywnego rekordu i wiadomości do odzyskania")
                return

            recovered_embed = recovered_message.embeds[0]
            recovered_data = None
            recovered_time = None
            for field in recovered_embed.fields:
                if "Data" in field.name:
                    recovered_data = field.value.replace("**", "").strip()
                elif "Godzina" in field.name:
                    recovered_time = field.value.replace("**", "").strip()

            if not recovered_data or not recovered_time:
                print("❌ Synchronizacja reakcji: wiadomość nie zawiera daty lub godziny")
                return

            termin = datetime.strptime(
                f"{recovered_data} {recovered_time}", "%d.%m.%Y %H:%M"
            ).replace(tzinfo=ZoneInfo("Europe/Warsaw"))
            try:
                recording_number = int(
                    recovered_embed.title.split("#", 1)[1].split()[0]
                )
            except (IndexError, TypeError, ValueError):
                recording_number = await asyncio.to_thread(next_recording_number)

            recovered_recording = {
                "opis": f"Nagrywka #{recording_number}",
                "recording_number": recording_number,
                "message_id": recovered_message.id,
                "data": recovered_data,
                "godzina": recovered_time,
                "timestamp": termin.isoformat(),
                "uczestnicy": [],
                "reminder_sent": False,
                "started": datetime.now(ZoneInfo("Europe/Warsaw")) >= termin,
                "forum_thread_ids": [],
                "forums_closed": False,
                "report_sent": False,
                "missing_response_reminder_sent": False,
                "voice_seconds": {},
                "voice_joined_at": {},
                "first_voice_join_at": {},
                "voice_exit_events": []
            }
            recovered_recording["forum_thread_ids"] = await find_recording_forum_threads(
                recovered_recording
            )
            await asyncio.to_thread(
                recordings_collection.update_one,
                {"message_id": recovered_message.id},
                {"$set": recovered_recording},
                upsert=True
            )
            nagrywki = {str(recovered_message.id): recovered_recording}
            print(
                f"✅ Odzyskano aktywną nagrywkę {recovered_message.id} "
                "z wiadomości Discorda"
            )

        except Exception as error:
            print(
                "❌ Nie udało się odzyskać aktywnej nagrywki: "
                f"{type(error).__name__}: {error}"
            )
            return

    for message_id, nagrywka in nagrywki.items():
        if nagrywka.get("double_group_id"):
            continue
        try:
            message = await channel.fetch_message(int(message_id))
            participant_ids = []
            absence_authors_by_forum = await collect_absence_authors(
                await find_recording_forum_threads(nagrywka),
                nagrywka
            )
            absent_user_ids = {
                user_id
                for authors in absence_authors_by_forum.values()
                for user_id in authors
            }

            for reaction in message.reactions:
                if str(reaction.emoji) != "✅":
                    continue

                async for reacting_user in reaction.users(limit=None):
                    if reacting_user.bot:
                        continue

                    member = message.guild.get_member(reacting_user.id)
                    if member and any(role.id == URLOP_ROLE_ID for role in member.roles):
                        continue
                    if reacting_user.id in absent_user_ids:
                        try:
                            await message.remove_reaction("✅", reacting_user)
                        except (discord.Forbidden, discord.HTTPException):
                            pass
                        continue

                    participant_ids.append(reacting_user.id)

            participant_ids = list(dict.fromkeys(participant_ids))
            if set(participant_ids) == set(nagrywka.get("uczestnicy", [])):
                continue

            await asyncio.to_thread(
                recordings_collection.update_one,
                {"message_id": int(message_id)},
                {"$set": {"uczestnicy": participant_ids}}
            )
            nagrywka["uczestnicy"] = participant_ids
            nagrywka["message_id"] = int(message_id)
            await message.edit(embed=build_recording_embed(nagrywka))
            print(
                f"✅ Synchronizacja reakcji {message_id}: "
                f"zapisano {len(participant_ids)} osób"
            )

        except Exception as error:
            print(
                f"❌ Synchronizacja reakcji {message_id} nie powiodła się: "
                f"{type(error).__name__}: {error}"
            )

@tasks.loop(seconds=30)
async def sync_recording_reactions_loop():
    await sync_recording_reactions()

@sync_recording_reactions_loop.before_loop
async def before_sync_recording_reactions_loop():
    await bot.wait_until_ready()

async def collect_absence_authors(thread_ids, nagrywka=None):
    authors_by_forum = {forum_id: set() for forum_id in NIEOBECNOSCI_FORUM_IDS}

    if nagrywka and nagrywka.get("double_group_id"):
        selections = await asyncio.to_thread(
            lambda: list(recording_absences_collection.find({
                "group_id": nagrywka["double_group_id"],
                "confirmed": True,
                "recording_message_ids": int(nagrywka["message_id"])
            }))
        )
        for selection in selections:
            authors_by_forum.setdefault(int(selection["forum_id"]), set()).add(
                int(selection["user_id"])
            )
        return authors_by_forum

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

async def synchronize_double_group_attendance(group_id):
    group_recordings = await asyncio.to_thread(
        lambda: list(recordings_collection.find({"double_group_id": group_id}))
    )
    choices = await asyncio.to_thread(
        lambda: list(recording_attendance_choices_collection.find({"group_id": group_id}))
    )
    absences = await asyncio.to_thread(
        lambda: list(recording_absences_collection.find({
            "group_id": group_id,
            "confirmed": True
        }))
    )

    count_changes = []
    for group_recording in group_recordings:
        recording_id = int(group_recording.get("message_id", 0))
        before_count = len(group_recording.get("uczestnicy", []))
        absent_user_ids = {
            int(absence["user_id"])
            for absence in absences
            if recording_id in {
                int(value) for value in absence.get("recording_message_ids", [])
            }
        }
        participant_ids = sorted({
            int(choice["user_id"])
            for choice in choices
            if recording_id in {
                int(value) for value in choice.get("recording_message_ids", [])
            }
            and int(choice["user_id"]) not in absent_user_ids
        })
        await asyncio.to_thread(
            recordings_collection.update_one,
            {"_id": group_recording["_id"]},
            {"$set": {"uczestnicy": participant_ids}}
        )
        count_changes.append({
            "position": int(group_recording.get("double_position", 0)),
            "before": before_count,
            "after": len(participant_ids),
            "recording_id": recording_id
        })

    refreshed = await asyncio.to_thread(
        lambda: list(recordings_collection.find({"double_group_id": group_id}))
    )
    if not refreshed:
        return count_changes
    channel = bot.get_channel(NAGRYWKI_CHANNEL_ID)
    if channel is None:
        return count_changes
    try:
        announcement = await channel.fetch_message(
            int(refreshed[0]["announcement_message_id"])
        )
        await announcement.edit(
            embed=build_double_recording_embed(refreshed),
            view=DoubleAttendanceView(group_id, refreshed) if len(refreshed) == 2 else None
        )
    except (discord.NotFound, discord.Forbidden, discord.HTTPException) as error:
        print(f"❌ Nie udało się odświeżyć licznika X2 dla {group_id}: {error}")
    print(
        f"✅ Przeliczono zapisy X2 {group_id}: "
        + ", ".join(
            f"{change['position']}/2 {change['before']}→{change['after']}"
            for change in sorted(count_changes, key=lambda item: item["position"])
        )
    )
    return count_changes


async def remove_double_absence_from_signups(group_id, user_id, selected_ids):
    selected_ids = {int(value) for value in selected_ids}

    await asyncio.to_thread(
        recordings_collection.update_many,
        {
            "double_group_id": group_id,
            "message_id": {"$in": list(selected_ids)}
        },
        {"$pull": {
            "uczestnicy": {"$in": [int(user_id), str(user_id)]}
        }}
    )

    attendance_choices = await asyncio.to_thread(
        lambda: list(recording_attendance_choices_collection.find({
            "group_id": group_id,
            "user_id": {"$in": [int(user_id), str(user_id)]}
        }))
    )
    for attendance_choice in attendance_choices:
        remaining_ids = [
            int(value)
            for value in attendance_choice.get("recording_message_ids", [])
            if int(value) not in selected_ids
        ]
        if remaining_ids:
            await asyncio.to_thread(
                recording_attendance_choices_collection.update_one,
                {"_id": attendance_choice["_id"]},
                {"$set": {
                    "recording_message_ids": remaining_ids,
                    "updated_at": datetime.now(ZoneInfo("Europe/Warsaw")).isoformat()
                }}
            )
        else:
            await asyncio.to_thread(
                recording_attendance_choices_collection.delete_one,
                {"_id": attendance_choice["_id"]}
            )

    await synchronize_double_group_attendance(group_id)

@bot.listen("on_message")
async def enforce_double_absence_selection(message):
    if message.author.bot or not isinstance(message.channel, discord.Thread):
        return

    recording = await asyncio.to_thread(
        recordings_collection.find_one,
        {
            "double_group_id": {"$exists": True},
            "forum_thread_ids": message.channel.id
        }
    )
    if recording is None:
        return

    selection = await asyncio.to_thread(
        recording_absences_collection.find_one,
        {
            "group_id": recording["double_group_id"],
            "user_id": message.author.id,
            "forum_id": message.channel.parent_id
        }
    )
    if selection is None:
        try:
            await message.delete()
            await message.author.send(
                "❌ Najpierw wybierz z listy w poście nieobecności, czy nie będzie Cię "
                "na etapie 1/2, 2/2 czy na obu. Dopiero potem wpisz powód."
            )
        except (discord.Forbidden, discord.HTTPException):
            pass
        return


    if selection.get("confirmed"):
        try:
            await message.delete()
            await message.author.send(
                "❌ Masz już zapisaną nieobecność na tę podwójną nagrywkę. "
                "Jej zmianę musi najpierw odblokować administracja komendą "
                "`/cofnijnieobecnosc`."
            )
        except (discord.Forbidden, discord.HTTPException):
            pass
        return

    selected_ids = [
        int(value) for value in selection.get("recording_message_ids", [])
    ]
    await asyncio.to_thread(
        recording_absences_collection.update_one,
        {"_id": selection["_id"]},
        {"$set": {
            "confirmed": True,
            "status": "confirmed",
            "reason_message_id": message.id,
            "reason_preview": message.content[:500],
            "submitted_at": datetime.now(ZoneInfo("Europe/Warsaw")).isoformat()
        }}
    )
    try:
        await message.add_reaction("✅")
    except (discord.Forbidden, discord.HTTPException):
        pass

    await remove_double_absence_from_signups(
        recording["double_group_id"],
        message.author.id,
        selected_ids
    )

async def reconcile_existing_double_absences():
    selections = await asyncio.to_thread(
        lambda: list(recording_absences_collection.find())
    )
    reconciled = 0
    for selection in selections:
        group_id = selection.get("group_id")
        if not group_id:
            continue
        recordings = await asyncio.to_thread(
            lambda current_group=group_id: list(
                recordings_collection.find({"double_group_id": current_group})
            )
        )
        if not recordings:
            continue

        has_reason = bool(selection.get("confirmed"))
        reason_message_id = selection.get("reason_message_id")
        if not has_reason:
            forum_id = int(selection.get("forum_id", 0))
            thread_ids = {
                int(thread_id)
                for recording in recordings
                for thread_id in recording.get("forum_thread_ids", [])
            }
            if not thread_ids:
                thread_ids.update(
                    await find_recording_forum_threads(recordings[0])
                )
            for thread_id in thread_ids:
                try:
                    thread = bot.get_channel(thread_id) or await bot.fetch_channel(thread_id)
                    if int(thread.parent_id) != forum_id:
                        continue
                    async for old_message in thread.history(limit=None, oldest_first=True):
                        if old_message.author.id == int(selection["user_id"]):
                            has_reason = True
                            reason_message_id = old_message.id
                            break
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    continue
                if has_reason:
                    break

        if not has_reason:
            continue
        await asyncio.to_thread(
            recording_absences_collection.update_one,
            {"_id": selection["_id"]},
            {"$set": {
                "confirmed": True,
                "status": "approved_legacy",
                "reason_message_id": reason_message_id,
                "reconciled_at": datetime.now(ZoneInfo("Europe/Warsaw")).isoformat()
            }}
        )
        await remove_double_absence_from_signups(
            group_id,
            int(selection["user_id"]),
            selection.get("recording_message_ids", [])
        )
        print(
            "✅ Nieobecność X2 zsynchronizowana: "
            f"user={selection['user_id']}, "
            f"nagrywki={selection.get('recording_message_ids', [])}"
        )
        reconciled += 1

    if reconciled:
        print(f"✅ Zsynchronizowano zaległe nieobecności X2: {reconciled}")

async def boss_has_work_credit(nagrywka):
    work_credit = await asyncio.to_thread(
        work_credits_collection.find_one,
        {"user_id": BOSS_USER_ID}
    )
    return bool(
        work_credit
        and nagrywka.get("data") in work_credit.get("covered_dates", [])
    )

async def get_missing_recording_members(nagrywka, guild):
    thread_ids = await find_recording_forum_threads(nagrywka)
    absence_authors = await collect_absence_authors(thread_ids, nagrywka)
    confirmed_ids = set(nagrywka.get("uczestnicy", []))
    boss_covered_by_work = await boss_has_work_credit(nagrywka)
    missing_by_role = {}

    for role_id, forum_id in (
        (NAGRYWKOWICZE_ROLE_ID, NIEOBECNOSCI_FORUM_IDS[0]),
        (TESTOWI_ROLE_ID, NIEOBECNOSCI_FORUM_IDS[1])
    ):
        role = guild.get_role(role_id) if guild else None
        missing = []
        if role is not None:
            absent_ids = absence_authors.get(forum_id, set())
            for member in role.members:
                if member.bot:
                    continue
                if member.id == BOSS_USER_ID and boss_covered_by_work:
                    continue
                if member.id in confirmed_ids or member.id in absent_ids:
                    continue
                if any(member_role.id == URLOP_ROLE_ID for member_role in member.roles):
                    continue
                missing.append(member)
        missing_by_role[role_id] = missing

    return missing_by_role

async def build_recording_statistics(message_id, nagrywka, guild):
    thread_ids = await find_recording_forum_threads(nagrywka)
    absence_authors = await collect_absence_authors(thread_ids, nagrywka)
    confirmed_ids = {
        int(user_id)
        for user_id, seconds in nagrywka.get("voice_seconds", {}).items()
        if seconds >= MIN_VC_ATTENDANCE_SECONDS
    }
    boss_covered_by_work = await boss_has_work_credit(nagrywka)
    if boss_covered_by_work:
        confirmed_ids.add(BOSS_USER_ID)

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
            if member.id == BOSS_USER_ID and boss_covered_by_work:
                continue
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
        "opis": recording_display_name(nagrywka),
        "recording_number": nagrywka.get("recording_number"),
        "double_group_id": nagrywka.get("double_group_id"),
        "double_position": nagrywka.get("double_position"),
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
        "voice_exit_events": nagrywka.get("voice_exit_events", []),
        "forum_thread_ids": thread_ids
    }

async def send_recording_completion_report(message_id, nagrywka, statistics, end_time):
    report_channel = bot.get_channel(REPORT_CHANNEL_ID)
    if report_channel is None:
        print(f"❌ Nie znaleziono kanału raportów: {REPORT_CHANNEL_ID}")
        return

    try:
        start_time = datetime.fromisoformat(nagrywka["timestamp"])
        if start_time.tzinfo is None:
            start_time = start_time.replace(tzinfo=ZoneInfo("Europe/Warsaw"))
        elapsed_minutes = max(0, int((end_time - start_time).total_seconds() // 60))
    except (KeyError, TypeError, ValueError):
        elapsed_minutes = 0

    hours, minutes = divmod(elapsed_minutes, 60)
    if hours and minutes:
        elapsed_text = f"{hours} godz. {minutes} min"
    elif hours:
        elapsed_text = f"{hours} godz."
    elif minutes:
        elapsed_text = f"{minutes} min"
    else:
        elapsed_text = "mniej niż minutę"

    present_ids = statistics.get("confirmed_ids", [])
    present_text = "\n".join(f"<@{user_id}>" for user_id in present_ids)
    if not present_text:
        present_text = "Brak osób z zaliczonym minimum 35 minut."

    double_position = nagrywka.get("double_position")
    stage_suffix = f" {double_position}/2" if double_position else ""
    embed = discord.Embed(
        title=f"📋 RAPORT{stage_suffix} — {recording_display_name(nagrywka).upper()}",
        description=(
            f"Etap **{double_position}/2** został zakończony, a obecność zapisana w statystykach."
            if double_position else
            "Nagrywka została zakończona, a obecność zapisana w statystykach."
        ),
        color=discord.Color.green(),
        timestamp=end_time
    )
    embed.add_field(name="📅 Data", value=f"**{nagrywka['data']}**", inline=True)
    embed.add_field(name="🕒 Godzina rozpoczęcia", value=f"**{nagrywka['godzina']}**", inline=True)
    embed.add_field(name="⏱️ Rzeczywisty czas", value=f"**{elapsed_text}**", inline=True)
    embed.add_field(
        name=f"✅ Obecni ({len(present_ids)})",
        value=present_text[:1024],
        inline=False
    )
    if nagrywka.get("double_group_id"):
        lateness_report = await asyncio.to_thread(
            recording_lateness_reports_collection.find_one,
            {"group_id": nagrywka["double_group_id"]}
        )
        if lateness_report and lateness_report.get("message_id"):
            lateness_channel_id = int(
                lateness_report.get("channel_id", REPORT_CHANNEL_ID)
            )
            lateness_url = (
                f"https://discord.com/channels/{GUILD_ID}/"
                f"{lateness_channel_id}/{int(lateness_report['message_id'])}"
            )
            embed.add_field(
                name="⏰ Spóźnienia",
                value=f"[Otwórz zbiorczy raport spóźnień]({lateness_url})",
                inline=False
            )
    footer_stage = f" • Etap {double_position}/2" if double_position else ""
    embed.set_footer(
        text=f"ID terminu: {message_id}{footer_stage} • Minimum obecności: 35 minut"
    )

    await report_channel.send(
        embed=embed,
        allowed_mentions=discord.AllowedMentions.none()
    )

def load_vacations():

    vacations = {}

    for doc in vacations_collection.find():

        vacations[str(doc["user_id"])] = {
            key: value for key, value in doc.items()
            if key not in {"_id", "user_id"}
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

        payload = {
            key: value for key, value in vacation.items()
            if key != "_id"
        }

        vacations_collection.update_one(
            {
                "user_id": int(user_id)
            },
            {
                "$set": payload
            },
            upsert=True
        )
        print("SAVE RECORDINGS", data)

def load_recordings():

    recordings = {}

    for doc in recordings_collection.find():

        message_id = str(doc["message_id"])

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

def refresh_recording_lock(recordings):
    """Utrzymuje atomową blokadę zgodnie z faktyczną listą aktywnych nagrywek."""
    if not recordings:
        recording_locks_collection.delete_one({"_id": "active_recording"})
        return

    message_id, recording = next(iter(recordings.items()))
    recording_locks_collection.update_one(
        {"_id": "active_recording"},
        {
            "$set": {
                "message_id": int(message_id),
                "timestamp": recording.get("timestamp"),
                "data": recording.get("data"),
                "godzina": recording.get("godzina")
            },
            "$unset": {"expires_at": ""}
        },
        upsert=True
    )

@bot.tree.command(
    name="nadajurlop",
    description="Nadaje urlop nagrywkowiczowi"
)
@app_commands.describe(
    user="Nagrywkowicz",
    dni="Liczba dni urlopu",
    wiadomosc="Link do wiadomości ze zgłoszeniem urlopu",
    notatka="Opcjonalna notatka administracji"
)
async def nadajurlop(
    interaction: discord.Interaction,
    user: discord.Member,
    dni: int,
    wiadomosc: str,
    notatka: str = None
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
        "end": end_date.isoformat(),
        "note": notatka,
        "message_url": wiadomosc,
        "approved_by": interaction.user.id,
        "approved_at": datetime.now(ZoneInfo("Europe/Warsaw")).isoformat()
    }

    save_vacations(vacations)

    try:
        await user.send(
            f"🏖️ Twój urlop został zaakceptowany.\n\n"
            f"📅 Długość: **{dni} dni**\n"
            f"⏰ Powrót: **{end_date.strftime('%d.%m.%Y %H:%M')}**\n"
            f"🔗 Zgłoszenie: {wiadomosc}\n"
            + (f"📝 Notatka: **{notatka}**\n" if notatka else "")
            + "\nDo zobaczenia na nagrywkach! 🎬"
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

    embed.add_field(
        name="🔗 Zgłoszenie urlopowe",
        value=f"[Przejdź do wiadomości]({wiadomosc})",
        inline=False
    )
    if notatka:
        embed.add_field(
            name="📝 Notatka administracji",
            value=notatka[:1024],
            inline=False
        )

    await log_channel.send(embed=embed)

    await interaction.followup.send(
        f"✅ Nadano urlop dla {user.mention}.",
        ephemeral=True
    )

@bot.tree.command(
    name="odrzucurlop",
    description="Odrzuca zgłoszenie urlopowe wraz z powodem"
)
@app_commands.describe(
    user="Osoba, której zgłoszenie odrzucasz",
    powod="Powód odrzucenia urlopu",
    wiadomosc="Link do wiadomości ze zgłoszeniem urlopu"
)
async def odrzucurlop(
    interaction: discord.Interaction,
    user: discord.Member,
    powod: str,
    wiadomosc: str
):
    if not any(role.id in STAFF_ROLES for role in interaction.user.roles):
        await send_response(interaction, "❌ Nie masz uprawnień.", ephemeral=True)
        return

    dm_sent = True
    try:
        await user.send(
            "❌ **Twoje zgłoszenie urlopowe zostało odrzucone.**\n\n"
            f"📝 Powód: **{powod}**\n"
            f"🔗 Zgłoszenie: {wiadomosc}\n\n"
            "Jeśli potrzebujesz wyjaśnienia, skontaktuj się z administracją."
        )
    except (discord.Forbidden, discord.HTTPException):
        dm_sent = False

    log_channel = bot.get_channel(VACATION_LOG_CHANNEL_ID)
    if log_channel is not None:
        embed = discord.Embed(
            title="❌ Zgłoszenie urlopowe odrzucone",
            color=discord.Color.red(),
            timestamp=datetime.now(ZoneInfo("Europe/Warsaw"))
        )
        embed.add_field(name="🎬 Nagrywkowicz", value=user.mention, inline=False)
        embed.add_field(name="👤 Odrzucił", value=interaction.user.mention, inline=False)
        embed.add_field(name="📝 Powód", value=powod[:1024], inline=False)
        embed.add_field(
            name="🔗 Zgłoszenie urlopowe",
            value=f"[Przejdź do wiadomości]({wiadomosc})",
            inline=False
        )
        embed.set_footer(text=f"ID użytkownika: {user.id}")
        await log_channel.send(
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none()
        )

    await send_response(
        interaction,
        (
            f"✅ Odrzucono zgłoszenie urlopowe {user.mention}."
            + ("" if dm_sent else " ⚠️ Nie udało się wysłać wiadomości prywatnej.")
        ),
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
    data="Data (DD.MM.RRRR)",
    godzina="Godzina (HH:MM)"
)
async def nagrywka(
    interaction: discord.Interaction,
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

    active_recordings = await asyncio.to_thread(load_recordings)
    if active_recordings:
        existing_id, existing_recording = next(iter(active_recordings.items()))
        await interaction.followup.send(
            (
                "❌ Nie można utworzyć kolejnej nagrywki, dopóki poprzednia nie zostanie zakończona.\n\n"
                f"🎬 **{recording_display_name(existing_recording)}**\n"
                f"📅 {existing_recording['data']} • 🕒 {existing_recording['godzina']}\n"
                f"🔖 ID: `{existing_id}`\n\n"
                "Najpierw użyj `/zakoncznagrywke` albo `/odwolajnagrywke`."
            ),
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

    # Atomowa blokada zapobiega dwóm równoczesnym wywołaniom /nagrywka.
    # Zwykłe wcześniejsze sprawdzenie listy nie wystarczało, gdy dwóch
    # administratorów uruchomiło komendę niemal w tej samej chwili.
    reservation_now = datetime.now(ZoneInfo("Europe/Warsaw"))
    try:
        await asyncio.to_thread(
            recording_locks_collection.find_one_and_update,
            {
                "_id": "active_recording",
                "expires_at": {"$lt": reservation_now}
            },
            {
                "$set": {
                    "timestamp": termin.isoformat(),
                    "data": data,
                    "godzina": godzina,
                    "reserved_by": interaction.user.id,
                    "expires_at": reservation_now + timedelta(minutes=5)
                }
            },
            upsert=True,
            return_document=ReturnDocument.AFTER
        )
    except DuplicateKeyError:
        await interaction.followup.send(
            "❌ Inny termin jest właśnie tworzony albo aktywna nagrywka już istnieje. "
            "Nie można utworzyć tego samego terminu drugi raz.",
            ephemeral=True
        )
        return

    channel = bot.get_channel(
        NAGRYWKI_CHANNEL_ID
    )

    recording_number = await asyncio.to_thread(next_recording_number)
    opis = f"Nagrywka #{recording_number}"

    new_recording = {
        "opis": opis,
        "recording_number": recording_number,
        "data": data,
        "godzina": godzina,
        "timestamp": termin.isoformat(),
        "uczestnicy": []
    }
    embed = build_recording_embed(new_recording)

    message = await channel.send(
        embed=embed
    )

    new_recording["message_id"] = message.id
    await message.edit(embed=build_recording_embed(new_recording))

    # Zapis musi powstać zanim użytkownicy będą mogli kliknąć reakcję.
    # Wcześniej tworzenie postów forum opóźniało zapis o kilka sekund,
    # więc szybkie reakcje trafiały do zwykłych logów i nie były liczone.
    nagrywki = load_recordings()
    nagrywki[str(message.id)] = {
        "opis": opis,
        "recording_number": recording_number,
        "message_id": message.id,
        "data": data,
        "godzina": godzina,
        "timestamp": termin.isoformat(),
        "uczestnicy": [],
        "reminder_sent": False,
        "started": False,
        "forum_thread_ids": [],
        "forums_closed": False,
        "report_sent": False,
        "missing_response_reminder_sent": False,
        "voice_seconds": {},
        "voice_joined_at": {},
        "first_voice_join_at": {},
        "voice_exit_events": []
    }
    save_recordings(nagrywki)

    await asyncio.to_thread(
        recording_locks_collection.update_one,
        {"_id": "active_recording"},
        {
            "$set": {"message_id": message.id, "timestamp": termin.isoformat()},
            "$unset": {"expires_at": ""}
        }
    )

    await message.add_reaction("✅")

    post_title = recording_forum_title(data)
    post_content = recording_forum_content(opis, data, godzina)

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

    # Aktualizujemy wyłącznie ID postów, aby nie nadpisać reakcjami zapisanymi
    # równolegle podczas tworzenia postów na forum.
    await asyncio.to_thread(
        recordings_collection.update_one,
        {"message_id": message.id},
        {"$set": {"forum_thread_ids": forum_thread_ids}}
    )

    await interaction.followup.send(
        f"✅ Utworzono nagrywkę.\n"
        f"📍 {message.jump_url}",
        ephemeral=True
    )

@bot.tree.command(
    name="nagrywkax2",
    description="Tworzy dwa powiązane terminy nagrywek i wspólne nieobecności"
)
@app_commands.describe(
    data="Data obu nagrywek (DD.MM.RRRR)",
    godzina="Godzina rozpoczęcia podwójnej nagrywki (HH:MM)"
)
async def nagrywkax2(
    interaction: discord.Interaction,
    data: str,
    godzina: str
):
    if not any(role.id in STAFF_ROLES for role in interaction.user.roles):
        await send_response(interaction, "❌ Nie masz uprawnień.", ephemeral=True)
        return

    active_recordings = await asyncio.to_thread(load_recordings)
    if active_recordings:
        await send_response(
            interaction,
            "❌ Najpierw zakończ, odwołaj albo usuń wszystkie aktywne nagrywki.",
            ephemeral=True
        )
        return

    try:
        first_time = datetime.strptime(
            f"{data} {godzina}", "%d.%m.%Y %H:%M"
        ).replace(tzinfo=ZoneInfo("Europe/Warsaw"))
    except ValueError:
        await send_response(
            interaction,
            "❌ Niepoprawna data lub godzina. Przykład: `01.10.2026` i `18:00`.",
            ephemeral=True
        )
        return

    reservation_now = datetime.now(ZoneInfo("Europe/Warsaw"))
    try:
        await asyncio.to_thread(
            recording_locks_collection.find_one_and_update,
            {"_id": "active_recording", "expires_at": {"$lt": reservation_now}},
            {"$set": {
                "timestamp": first_time.isoformat(),
                "data": data,
                "godzina": godzina,
                "reserved_by": interaction.user.id,
                "expires_at": reservation_now + timedelta(minutes=5)
            }},
            upsert=True,
            return_document=ReturnDocument.AFTER
        )
    except DuplicateKeyError:
        await send_response(
            interaction,
            "❌ Inny termin jest właśnie tworzony albo aktywna nagrywka już istnieje.",
            ephemeral=True
        )
        return

    channel = bot.get_channel(NAGRYWKI_CHANNEL_ID)
    if channel is None:
        await asyncio.to_thread(
            recording_locks_collection.delete_one,
            {"_id": "active_recording"}
        )
        await send_response(interaction, "❌ Nie znaleziono kanału nagrywek.", ephemeral=True)
        return

    group_id = f"double-{int(reservation_now.timestamp() * 1000000)}"
    announcement = None
    try:
        recordings = []
        for position, (date_text, time_text, timestamp) in enumerate((
            (data, godzina, first_time),
            (data, godzina, first_time)
        ), start=1):
            recording_number = await asyncio.to_thread(next_recording_number)
            recording = {
                "opis": f"Nagrywka #{recording_number}",
                "recording_number": recording_number,
                "data": date_text,
                "godzina": time_text,
                "timestamp": timestamp.isoformat(),
                "uczestnicy": [],
                "reminder_sent": False,
                "started": False,
                "forum_thread_ids": [],
                "forums_closed": False,
                "report_sent": False,
                "missing_response_reminder_sent": False,
                "voice_seconds": {},
                "voice_joined_at": {},
                "first_voice_join_at": {},
                "voice_exit_events": [],
                "double_group_id": group_id,
                "double_position": position,
                "stage_waiting": position == 2
            }
            recordings.append(recording)

        announcement = await channel.send(embed=build_double_recording_embed(recordings))
        recordings[0]["message_id"] = announcement.id
        recordings[1]["message_id"] = -announcement.id
        for recording in recordings:
            recording["announcement_message_id"] = announcement.id

        await announcement.edit(
            embed=build_double_recording_embed(recordings),
            view=DoubleAttendanceView(group_id, recordings)
        )

        recordings_data = {
            str(recording["message_id"]): recording for recording in recordings
        }
        await asyncio.to_thread(save_recordings, recordings_data)
        await asyncio.to_thread(refresh_recording_lock, recordings_data)

        post_title = f"Nieobecność X2 {data} — {POLISH_WEEKDAYS[first_time.weekday()]}"

        forum_thread_ids = []
        for forum_id in NIEOBECNOSCI_FORUM_IDS:
            forum = bot.get_channel(forum_id)
            if not isinstance(forum, discord.ForumChannel):
                print(f"❌ Nie znaleziono forum nieobecności: {forum_id}")
                continue
            created_post = await forum.create_thread(
                name=post_title[:100],
                content=double_recording_forum_content(recordings),
                reason=f"Podwójna nagrywka utworzona przez {interaction.user}"
            )
            forum_thread_ids.append(created_post.thread.id)
            starter_message = await created_post.thread.fetch_message(created_post.thread.id)
            await starter_message.edit(
                view=DoubleAbsenceView(group_id, recordings)
            )

        await asyncio.to_thread(
            recordings_collection.update_many,
            {"double_group_id": group_id},
            {"$set": {"forum_thread_ids": forum_thread_ids}}
        )

        await send_response(
            interaction,
            (
                "✅ Utworzono podwójną nagrywkę z dwiema osobnymi obecnościami.\n"
                f"📍 {announcement.jump_url}"
            ),
            ephemeral=True
        )

    except Exception as error:
        print(f"❌ Nie udało się utworzyć podwójnej nagrywki: {type(error).__name__}: {error}")
        if announcement is not None:
            try:
                await announcement.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
        await asyncio.to_thread(
            recordings_collection.delete_many,
            {"double_group_id": group_id}
        )
        await asyncio.to_thread(
            recording_locks_collection.delete_one,
            {"_id": "active_recording"}
        )
        await send_response(
            interaction,
            "❌ Nie udało się utworzyć podwójnej nagrywki. Sprawdź logi bota.",
            ephemeral=True
        )

async def send_missing_response_report(nagrywka, guild, manual_by=None):
    missing_by_role = await get_missing_recording_members(nagrywka, guild)
    report_channel = bot.get_channel(REPORT_CHANNEL_ID)
    if report_channel is None:
        return False

    report_embed = discord.Embed(
        title="⚠️ Brak potwierdzenia obecności",
        description=(
            f"🎬 **{recording_display_name(nagrywka)}**\n"
            f"📅 {nagrywka['data']} o {nagrywka['godzina']} (Europe/Warsaw)\n\n"
            "Poniższe osoby nie dały reakcji ✅, nie zgłosiły nieobecności "
            "i nie mają aktywnego urlopu."
        ),
        color=discord.Color.orange(),
        timestamp=datetime.now(ZoneInfo("Europe/Warsaw"))
    )
    for role_id, label in (
        (NAGRYWKOWICZE_ROLE_ID, "🎬 Pomocnicy"),
        (TESTOWI_ROLE_ID, "🧪 Testowi")
    ):
        members = missing_by_role.get(role_id, [])
        value = "\n".join(member.mention for member in members) or "✅ Wszyscy odpowiedzieli"
        report_embed.add_field(name=label, value=value[:1024], inline=False)

    if manual_by is None:
        report_embed.set_footer(text="Raport automatyczny • godzinę przed nagrywką")
    else:
        report_embed.set_footer(text=f"Raport ręczny • wywołał {manual_by}")

    await report_channel.send(
        embed=report_embed,
        allowed_mentions=discord.AllowedMentions.none()
    )
    return True

@bot.tree.command(
    name="raportbrakuodpowiedzi",
    description="Wysyła raport osób bez potwierdzenia dla aktywnej nagrywki"
)
async def raportbrakuodpowiedzi(interaction: discord.Interaction):
    if not any(role.id in STAFF_ROLES for role in interaction.user.roles):
        await send_response(interaction, "❌ Nie masz uprawnień.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    nagrywki = await asyncio.to_thread(load_recordings)
    if not nagrywki:
        await interaction.followup.send("❌ Brak aktywnej nagrywki.", ephemeral=True)
        return

    message_id, nagrywka = next(iter(nagrywki.items()))
    sent = await send_missing_response_report(
        nagrywka,
        interaction.guild,
        manual_by=interaction.user.display_name
    )
    if not sent:
        await interaction.followup.send("❌ Nie znaleziono kanału raportów.", ephemeral=True)
        return

    await asyncio.to_thread(
        recordings_collection.update_one,
        {"message_id": int(message_id)},
        {"$set": {"report_sent": True}}
    )
    await interaction.followup.send(
        f"✅ Raport dla **{recording_display_name(nagrywka)}** został wysłany.",
        ephemeral=True
    )

@tasks.loop(minutes=1)
async def check_recordings():

    nagrywki = await asyncio.to_thread(load_recordings)

    changed = False

    for message_id, nagrywka in list(nagrywki.items()):

        # Co minutę synchronizuj zapisy z faktycznymi reakcjami Discorda.
        # Naprawia to również reakcje pominięte przez event podczas tworzenia
        # nagrywki albo krótkiej przerwy w działaniu bota.
        recording_channel = bot.get_channel(NAGRYWKI_CHANNEL_ID)
        if recording_channel is not None and not nagrywka.get("double_group_id"):
            try:
                recording_message = await recording_channel.fetch_message(int(message_id))
                reaction_participants = []

                for reaction in recording_message.reactions:
                    if str(reaction.emoji) != "✅":
                        continue

                    async for reacting_user in reaction.users():
                        if reacting_user.bot:
                            continue

                        reacting_member = recording_message.guild.get_member(reacting_user.id)
                        if reacting_member and any(
                            role.id == URLOP_ROLE_ID for role in reacting_member.roles
                        ):
                            continue

                        reaction_participants.append(reacting_user.id)

                reaction_participants = list(dict.fromkeys(reaction_participants))
                if set(reaction_participants) != set(nagrywka.get("uczestnicy", [])):
                    nagrywka["uczestnicy"] = reaction_participants
                    await recording_message.edit(embed=build_recording_embed(nagrywka))
                    changed = True
                    print(
                        f"✅ Zsynchronizowano reakcje nagrywki {message_id}: "
                        f"{len(reaction_participants)} osób"
                    )

            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as error:
                print(f"❌ Nie udało się zsynchronizować reakcji nagrywki {message_id}: {error}")

        # Etap 2/2 nie ma osobnej zaplanowanej godziny. Zaczyna się dokładnie
        # w chwili zakończenia etapu 1/2 komendą /zakonczetap.
        if nagrywka.get("stage_waiting", False):
            continue

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
            if not forum_thread_ids:
                forum_thread_ids = await find_recording_forum_threads(nagrywka)
            all_forums_closed = True

            if not forum_thread_ids:
                print(
                    f"❌ Brak postów nieobecności do zamknięcia dla "
                    f"{recording_display_name(nagrywka)}"
                )
                all_forums_closed = False

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
                print(
                    f"🔒 Zamknięto nieobecności dla {recording_display_name(nagrywka)} "
                    f"o {now.strftime('%d.%m.%Y %H:%M:%S')} Europe/Warsaw"
                )

        # PRYWATNE PRZYPOMNIENIE O BRAKU ODPOWIEDZI 8H PRZED
        if (
            not nagrywka.get("missing_response_reminder_sent", False)
            and 0 <= roznica <= 28800
            and bot.get_guild(GUILD_ID) is not None
        ):
            guild = bot.get_guild(GUILD_ID)
            missing_by_role = await get_missing_recording_members(nagrywka, guild)

            for missing_members in missing_by_role.values():
                for member in missing_members:
                    try:
                        reminder_embed = discord.Embed(
                            title="⚠️ Brak odpowiedzi na termin nagrywki",
                            description=(
                                "Nie potwierdziłeś jeszcze udziału ani nie zgłosiłeś nieobecności. "
                                "Daj znać, czy będziesz dostępny."
                            ),
                            color=discord.Color.orange(),
                            timestamp=now
                        )
                        reminder_embed.add_field(
                            name="🎬 Termin",
                            value=(
                                f"**{recording_display_name(nagrywka)}**\n"
                                f"📅 {nagrywka['data']} • 🕒 {nagrywka['godzina']}"
                            ),
                            inline=False
                        )
                        reminder_embed.add_field(
                            name="✅ Co zrobić?",
                            value=(
                                "Dodaj reakcję ✅ pod terminem, jeśli będziesz. "
                                "Jeżeli nie możesz przyjść, napisz w odpowiednim poście nieobecności."
                            ),
                            inline=False
                        )
                        reminder_embed.set_footer(text="Automatyczne przypomnienie • 8 godzin przed nagrywką")
                        await member.send(embed=reminder_embed)
                    except (discord.Forbidden, discord.HTTPException):
                        pass

            nagrywka["missing_response_reminder_sent"] = True
            changed = True

        # RAPORT BRAKU ODPOWIEDZI 1H PRZED
        if (
            not nagrywka.get("report_sent", False)
            and roznica <= 3600
            and bot.get_guild(GUILD_ID) is not None
        ):
            guild = bot.get_guild(GUILD_ID)
            if await send_missing_response_report(nagrywka, guild):
                nagrywka["report_sent"] = True
                changed = True

        # PRZYPOMNIENIE DLA ZAPISANYCH 1H PRZED
        if (
            not nagrywka["reminder_sent"]
            and 0 <= roznica <= 3600
        ):

            guild = bot.get_guild(GUILD_ID)

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

    if changed:

        await asyncio.to_thread(save_recordings, nagrywki)

@check_recordings.before_loop
async def before_check_recordings():
    await bot.wait_until_ready()

@check_recordings.error
async def check_recordings_error(error):
    print(
        "❌ Pętla nagrywek i przypomnień zatrzymała się: "
        f"{type(error).__name__}: {error}"
    )

    async def restart_recordings_loop():
        await asyncio.sleep(10)
        if not bot.is_closed() and not check_recordings.is_running():
            print("🔄 Ponowne uruchamianie pętli nagrywek i przypomnień")
            check_recordings.start()

    asyncio.create_task(restart_recordings_loop())

def recording_select_options(nagrywki):
    return [
        discord.SelectOption(
            label=recording_display_name(nagrywka)[:100],
            description=f"{nagrywka['data']} • {nagrywka['godzina']} • ID: {message_id}"[:100],
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
        if self.action != "edit":
            await interaction.response.defer(ephemeral=True, thinking=True)
        try:
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
        except Exception as error:
            print(
                f"❌ Błąd akcji {self.action} dla nagrywki {recording_id}: "
                f"{type(error).__name__}: {error}"
            )
            if interaction.response.is_done():
                await interaction.edit_original_response(
                    content="❌ Nie udało się wykonać tej operacji. Szczegóły zapisano w logach.",
                    view=None
                )
            else:
                await interaction.response.send_message(
                    "❌ Nie udało się wykonać tej operacji. Szczegóły zapisano w logach.",
                    ephemeral=True
                )

class RecordingActionView(View):
    def __init__(self, action, user=None):
        super().__init__(timeout=120)
        self.add_item(RecordingActionSelect(action, user))

class EditRecordingModal(Modal, title="Edytuj nagrywkę"):
    data = TextInput(label="Data (DD.MM.RRRR)", max_length=10)
    godzina = TextInput(label="Godzina (HH:MM)", max_length=5)

    def __init__(self, recording_id, nagrywka):
        super().__init__()
        self.recording_id = recording_id
        self.data.default = nagrywka["data"]
        self.godzina.default = nagrywka["godzina"]

    async def on_submit(self, interaction):
        try:
            termin = datetime.strptime(
                f"{self.data.value} {self.godzina.value}", "%d.%m.%Y %H:%M"
            ).replace(tzinfo=ZoneInfo("Europe/Warsaw"))
        except ValueError:
            await send_response(interaction, "❌ Niepoprawna data lub godzina.", ephemeral=True)
            return

        nagrywki = load_recordings()
        nagrywka = nagrywki.get(self.recording_id)
        if not nagrywka:
            await send_response(interaction, "❌ Ta nagrywka nie jest już aktywna.", ephemeral=True)
            return

        nagrywka.update({
            "data": self.data.value,
            "godzina": self.godzina.value,
            "timestamp": termin.isoformat(),
            "reminder_sent": False,
            "report_sent": False,
            "missing_response_reminder_sent": False,
            "forums_closed": False
        })
        save_recordings(nagrywki)

        channel = bot.get_channel(NAGRYWKI_CHANNEL_ID)
        try:
            message = await channel.fetch_message(int(self.recording_id))
            await message.edit(embed=build_recording_embed(nagrywka))
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
                    recording_display_name(nagrywka),
                    self.data.value,
                    self.godzina.value
                ))

                close_time = termin - timedelta(hours=3)
                if datetime.now(ZoneInfo("Europe/Warsaw")) >= close_time:
                    await thread.edit(archived=True, locked=True)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

        await send_response(interaction, "✅ Nagrywka została zaktualizowana.", ephemeral=True)

@bot.tree.command(
    name="edytujnagrywke",
    description="Zmienia datę i godzinę wybranej nagrywki"
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
                    label=recording_display_name(nagrywka)[:100],
                    description=(
                        f"{nagrywka['data']} "
                        f"{nagrywka['godzina']} • ID: {message_id}"
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

        await interaction.response.defer(ephemeral=True, thinking=True)

        message_id = self.values[0]

        nagrywki = load_recordings()

        nagrywka = nagrywki[message_id]

        if nagrywka.get("double_group_id"):
            await send_response(
                interaction,
                "❌ Podwójną nagrywkę odwołujesz komendą `/odwolajx2`.",
                ephemeral=True
            )
            return

        channel = bot.get_channel(
            NAGRYWKI_CHANNEL_ID
        )

        try:

            message = await channel.fetch_message(
                int(message_id)
            )

            embed = discord.Embed(
                title=f"❌ {recording_display_name(nagrywka).upper()} — ODWOŁANA",
                description=(
                    "### Termin został anulowany\n"
                    "Ta nagrywka nie odbędzie się. Posty nieobecności zostały automatycznie zamknięte."
                ),
                color=discord.Color.red(),
                timestamp=datetime.now(ZoneInfo("Europe/Warsaw"))
            )

            embed.add_field(
                name="📅 Data",
                value=f"**{nagrywka['data']}**",
                inline=True
            )
            embed.add_field(
                name="🕒 Godzina",
                value=f"**{nagrywka['godzina']}**",
                inline=True
            )
            embed.add_field(
                name="👤 Odwołano przez",
                value=interaction.user.mention,
                inline=False
            )
            embed.set_thumbnail(url=interaction.user.display_avatar.url)
            embed.set_footer(text=f"NegativE* • ID terminu: {message_id}")

            await message.edit(
                embed=embed,
                view=None
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

        # Przy X2 wspólny post pozostaje otwarty, dopóki drugi termin jest aktywny.
        double_sibling_exists = any(
            other_id != message_id
            and other.get("double_group_id") == nagrywka.get("double_group_id")
            for other_id, other in nagrywki.items()
        ) if nagrywka.get("double_group_id") else False
        thread_ids_to_close = (
            [] if double_sibling_exists else await find_recording_forum_threads(nagrywka)
        )
        for thread_id in thread_ids_to_close:
            try:
                thread = bot.get_channel(int(thread_id)) or await bot.fetch_channel(int(thread_id))
                await thread.edit(
                    archived=True,
                    locked=True,
                    reason=f"Nagrywka odwołana przez {interaction.user}"
                )
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as error:
                print(f"❌ Nie udało się zamknąć postu nieobecności {thread_id}: {error}")


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
        await asyncio.to_thread(refresh_recording_lock, nagrywki)
        await refresh_double_announcement_after_removal(
            nagrywka, nagrywki, "Odwołano jeden termin"
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


    active_recordings = load_recordings()
    if len(active_recordings) == 0:

        await send_response(interaction,
            "❌ Brak aktywnych nagrywek.",
            ephemeral=True
        )

        return

    if all(recording.get("double_group_id") for recording in active_recordings.values()):
        await send_response(
            interaction,
            "❌ To jest podwójna nagrywka. Użyj `/odwolajx2` i wybierz odpowiedni zakres.",
            ephemeral=True
        )
        return


    await send_response(interaction,
        "🎬 Wybierz nagrywkę:",
        view=CancelRecordingView(),
        ephemeral=True
    )

async def cancel_double_recording(interaction, recordings, only_second_stage=False):
    recordings = sorted(
        recordings,
        key=lambda item: int(item.get("double_position", 0))
    )
    target = recordings[-1] if only_second_stage else recordings[0]
    announcement_id = int(target["announcement_message_id"])
    channel = bot.get_channel(NAGRYWKI_CHANNEL_ID)

    if channel is not None:
        try:
            message = await channel.fetch_message(announcement_id)
            if only_second_stage:
                title = "❌ ETAP 2/2 ODWOŁANY"
                description = (
                    "Etap **1/2** został wcześniej zakończony. "
                    "Drugi etap podwójnej nagrywki nie odbędzie się."
                )
            else:
                title = "❌ PODWÓJNA NAGRYWKA ODWOŁANA"
                description = "Oba etapy nagrywki zostały odwołane."
            embed = discord.Embed(
                title=title,
                description=description,
                color=discord.Color.red(),
                timestamp=datetime.now(ZoneInfo("Europe/Warsaw"))
            )
            for recording in recordings:
                if only_second_stage and int(recording.get("double_position", 0)) != 2:
                    continue
                embed.add_field(
                    name=f"🎬 Etap {recording.get('double_position')}/2 — {recording_display_name(recording)}",
                    value=f"📅 **{recording['data']}** • 🕒 **{recording['godzina']}**",
                    inline=False
                )
            embed.add_field(
                name="👤 Odwołano przez",
                value=interaction.user.mention,
                inline=False
            )
            embed.set_footer(text=f"NegativE* • Grupa: {target['double_group_id']}")
            await message.edit(embed=embed, view=None)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

    thread_ids = set()
    participant_ids = set()
    for recording in recordings:
        if only_second_stage and int(recording.get("double_position", 0)) != 2:
            continue
        thread_ids.update(await find_recording_forum_threads(recording))
        participant_ids.update(recording.get("uczestnicy", []))

    for thread_id in thread_ids:
        try:
            thread = bot.get_channel(int(thread_id)) or await bot.fetch_channel(int(thread_id))
            await thread.edit(
                archived=True,
                locked=True,
                reason=f"Nagrywka X2 odwołana przez {interaction.user}"
            )
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as error:
            print(f"❌ Nie udało się zamknąć postu nieobecności X2 {thread_id}: {error}")

    for user_id in participant_ids:
        user = bot.get_user(int(user_id))
        if user is None:
            continue
        try:
            await user.send(
                "📢 **Zmiana w podwójnej nagrywce**\n\n"
                + (
                    "Etap **2/2** został odwołany."
                    if only_second_stage else
                    "Cała podwójna nagrywka została odwołana."
                )
            )
        except (discord.Forbidden, discord.HTTPException):
            pass

    log_channel = bot.get_channel(NAGRYWKI_LOGS_CHANNEL_ID)
    if log_channel is not None:
        log_embed = discord.Embed(
            title=(
                "❌ Odwołano etap 2/2 nagrywki X2"
                if only_second_stage else
                "❌ Odwołano całą nagrywkę X2"
            ),
            description="\n".join(
                f"• **{recording_display_name(recording)}** — {recording['data']} o {recording['godzina']}"
                for recording in recordings
                if not only_second_stage or int(recording.get("double_position", 0)) == 2
            ),
            color=discord.Color.red(),
            timestamp=datetime.now(ZoneInfo("Europe/Warsaw"))
        )
        log_embed.add_field(name="Odwołał", value=interaction.user.mention)
        await log_channel.send(
            embed=log_embed,
            allowed_mentions=discord.AllowedMentions.none()
        )

    active_recordings = load_recordings()
    group_id = target["double_group_id"]
    for message_id, recording in list(active_recordings.items()):
        if recording.get("double_group_id") != group_id:
            continue
        if only_second_stage and int(recording.get("double_position", 0)) != 2:
            continue
        del active_recordings[message_id]
    save_recordings(active_recordings)
    await asyncio.to_thread(refresh_recording_lock, active_recordings)

@bot.tree.command(
    name="odwolajx2",
    description="Odwołuje całą nagrywkę X2 albo tylko etap 2/2"
)
@app_commands.describe(zakres="Wybierz, co chcesz odwołać")
@app_commands.choices(zakres=[
    app_commands.Choice(name="Cała nagrywka X2", value="all"),
    app_commands.Choice(name="Tylko etap 2/2", value="second")
])
async def odwolajx2(
    interaction: discord.Interaction,
    zakres: app_commands.Choice[str]
):
    if not any(role.id in STAFF_ROLES for role in interaction.user.roles):
        await send_response(interaction, "❌ Nie masz uprawnień.", ephemeral=True)
        return

    recordings = [
        recording for recording in load_recordings().values()
        if recording.get("double_group_id")
    ]
    if not recordings:
        await send_response(
            interaction,
            "❌ Brak aktywnej podwójnej nagrywki.",
            ephemeral=True
        )
        return

    only_second_stage = zakres.value == "second"
    if only_second_stage:
        second_stage = next(
            (
                recording for recording in recordings
                if int(recording.get("double_position", 0)) == 2
            ),
            None
        )
        if second_stage is None:
            await send_response(interaction, "❌ Etap 2/2 nie jest aktywny.", ephemeral=True)
            return
        if not second_stage.get("first_stage_completed"):
            await send_response(
                interaction,
                "⛔ Etap 2/2 można odwołać osobno dopiero po zakończeniu 1/2 komendą `/zakonczetap`.",
                ephemeral=True
            )
            return

    await cancel_double_recording(interaction, recordings, only_second_stage)
    await send_response(
        interaction,
        (
            "✅ Etap **2/2** został odwołany, a posty nieobecności zamknięte."
            if only_second_stage else
            "✅ Cała nagrywka X2 została odwołana, a posty nieobecności zamknięte."
        ),
        ephemeral=True
    )

class DeleteRecordingSelect(Select):
    def __init__(self, recordings):
        super().__init__(
            placeholder="Wybierz zbugowany termin do trwałego usunięcia...",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=recording_display_name(recording)[:100],
                    description=(
                        f"{recording.get('data', '')} • {recording.get('godzina', '')} "
                        f"• ID: {message_id}"
                    )[:100],
                    value=message_id
                )
                for message_id, recording in list(recordings.items())[:25]
            ]
        )

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != BOSS_USER_ID:
            await send_response(
                interaction,
                "❌ Tylko właściciel może usuwać terminy.",
                ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        message_id = self.values[0]
        recordings = await asyncio.to_thread(load_recordings)
        recording = recordings.get(message_id)
        if recording is None:
            await interaction.edit_original_response(
                content="❌ Ten termin nie jest już aktywny.",
                view=None
            )
            return

        removed_message = False
        removed_threads = 0
        double_sibling_exists = any(
            other_id != message_id
            and other.get("double_group_id") == recording.get("double_group_id")
            for other_id, other in recordings.items()
        ) if recording.get("double_group_id") else False
        channel = bot.get_channel(NAGRYWKI_CHANNEL_ID)
        if double_sibling_exists:
            removed_message = True
        elif channel is not None:
            try:
                message = await channel.fetch_message(
                    int(recording.get("announcement_message_id", message_id))
                )
                await message.delete(reason=f"Zbugowany termin usunięty przez {interaction.user}")
                removed_message = True
            except discord.NotFound:
                removed_message = True
            except (discord.Forbidden, discord.HTTPException) as error:
                print(f"❌ Nie udało się usunąć wiadomości terminu {message_id}: {error}")

        thread_ids_to_delete = (
            [] if double_sibling_exists else await find_recording_forum_threads(recording)
        )
        for thread_id in thread_ids_to_delete:
            try:
                thread = bot.get_channel(int(thread_id)) or await bot.fetch_channel(int(thread_id))
                await thread.delete(reason=f"Zbugowany termin usunięty przez {interaction.user}")
                removed_threads += 1
            except discord.NotFound:
                removed_threads += 1
            except (discord.Forbidden, discord.HTTPException) as error:
                print(f"❌ Nie udało się usunąć postu nieobecności {thread_id}: {error}")

        await asyncio.to_thread(
            recordings_collection.delete_one,
            {"message_id": int(message_id)}
        )
        recordings.pop(message_id, None)
        await asyncio.to_thread(refresh_recording_lock, recordings)
        await refresh_double_announcement_after_removal(
            recording, recordings, "Usunięto jeden termin"
        )

        details = []
        if not removed_message:
            details.append("nie udało się usunąć wiadomości z kanału")
        if (
            not double_sibling_exists
            and removed_threads < len(recording.get("forum_thread_ids", []))
        ):
            details.append("nie udało się usunąć wszystkich postów nieobecności")
        warning = f"\n⚠️ {'; '.join(details)}." if details else ""

        await interaction.edit_original_response(
            content=(
                f"✅ Trwale usunięto **{recording_display_name(recording)}** "
                f"({recording.get('data')} • {recording.get('godzina')})."
                f"{warning}"
            ),
            view=None
        )

class DeleteRecordingView(View):
    def __init__(self, recordings):
        super().__init__(timeout=120)
        self.add_item(DeleteRecordingSelect(recordings))

@bot.tree.command(
    name="usuntermin",
    description="Trwale usuwa wybrany zbugowany termin nagrywki"
)
async def usuntermin(interaction: discord.Interaction):
    if interaction.user.id != BOSS_USER_ID:
        await send_response(
            interaction,
            "❌ Ta komenda jest dostępna wyłącznie dla właściciela.",
            ephemeral=True
        )
        return

    recordings = await asyncio.to_thread(load_recordings)
    if not recordings:
        await send_response(
            interaction,
            "❌ Brak aktywnych terminów do usunięcia.",
            ephemeral=True
        )
        return

    await send_response(
        interaction,
        "🗑️ Wybierz zbugowany termin, który mam trwale usunąć:",
        view=DeleteRecordingView(recordings),
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

    if nagrywka.get("double_group_id"):
        choice = await asyncio.to_thread(
            recording_attendance_choices_collection.find_one,
            {
                "group_id": nagrywka["double_group_id"],
                "user_id": user.id,
                "recording_message_ids": int(message_id)
            }
        )
        if choice is None:
            await interaction.edit_original_response(
                content=(
                    f"❌ {user.mention} nie ma potwierdzonej obecności na "
                    f"**{recording_display_name(nagrywka)}**."
                ),
                view=None
            )
            return

        remaining_ids = [
            int(value)
            for value in choice.get("recording_message_ids", [])
            if int(value) != int(message_id)
        ]
        if remaining_ids:
            await asyncio.to_thread(
                recording_attendance_choices_collection.update_one,
                {"_id": choice["_id"]},
                {"$set": {
                    "recording_message_ids": remaining_ids,
                    "updated_at": datetime.now(ZoneInfo("Europe/Warsaw")).isoformat()
                }}
            )
        else:
            await asyncio.to_thread(
                recording_attendance_choices_collection.delete_one,
                {"_id": choice["_id"]}
            )

        changes = await synchronize_double_group_attendance(
            nagrywka["double_group_id"]
        )
        change = next(
            (
                item for item in changes
                if int(item["recording_id"]) == int(message_id)
            ),
            None
        )
        count_text = (
            f" Licznik: **{change['before']} → {change['after']}**."
            if change else ""
        )
        await interaction.edit_original_response(
            content=(
                f"✅ Usunięto obecność {user.mention} z "
                f"**{recording_display_name(nagrywka)}** "
                f"(etap {nagrywka.get('double_position')}/2).{count_text}"
            ),
            view=None
        )
        return

    if user.id not in nagrywka.get("uczestnicy", []):
        await send_response(
            interaction,
            f"❌ {user.mention} nie ma potwierdzonej obecności na tej nagrywce.",
            ephemeral=True
        )
        return

    nagrywka["uczestnicy"].remove(user.id)
    await asyncio.to_thread(save_recordings, nagrywki)

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
                    3,
                    name="✅ Potwierdzone osoby",
                    value=(
                        f"**{len(nagrywka['uczestnicy'])}** "
                        f"{polish_people_word(len(nagrywka['uczestnicy']))}"
                    ),
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

class WithdrawAbsenceSelect(Select):
    def __init__(self, recordings, target_user, reason):
        self.recordings = recordings
        self.target_user = target_user
        self.reason = reason
        super().__init__(
            placeholder="Wybierz nagrywkę, z której cofasz nieobecność...",
            min_values=1,
            max_values=1,
            options=recording_select_options(recordings)
        )

    async def callback(self, interaction: discord.Interaction):
        if not any(role.id in STAFF_ROLES for role in interaction.user.roles):
            await send_response(interaction, "❌ Nie masz uprawnień.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        message_id = self.values[0]
        recording = load_recordings().get(message_id)
        if recording is None:
            await interaction.edit_original_response(
                content="❌ Ta nagrywka nie jest już aktywna.",
                view=None
            )
            return

        absence_removed = False
        forums_to_clear = set()
        if recording.get("double_group_id"):
            selections = await asyncio.to_thread(
                lambda: list(recording_absences_collection.find({
                    "group_id": recording["double_group_id"],
                    "user_id": self.target_user.id,
                    "recording_message_ids": int(message_id)
                }))
            )
            for selection in selections:
                remaining_ids = [
                    int(value)
                    for value in selection.get("recording_message_ids", [])
                    if int(value) != int(message_id)
                ]
                if remaining_ids:
                    await asyncio.to_thread(
                        recording_absences_collection.update_one,
                        {"_id": selection["_id"]},
                        {"$set": {
                            "recording_message_ids": remaining_ids,
                            "updated_at": datetime.now(ZoneInfo("Europe/Warsaw")).isoformat()
                        }}
                    )
                else:
                    await asyncio.to_thread(
                        recording_absences_collection.delete_one,
                        {"_id": selection["_id"]}
                    )
                    forums_to_clear.add(int(selection["forum_id"]))
                absence_removed = True
        else:
            forums_to_clear.update(NIEOBECNOSCI_FORUM_IDS)

        deleted_messages = 0
        thread_ids_to_scan = (
            await find_recording_forum_threads(recording)
            if not recording.get("double_group_id") or forums_to_clear
            else []
        )
        for thread_id in thread_ids_to_scan:
            try:
                thread = bot.get_channel(int(thread_id)) or await bot.fetch_channel(int(thread_id))
                if forums_to_clear and int(thread.parent_id) not in forums_to_clear:
                    continue
                async for message in thread.history(limit=None):
                    if message.author.id != self.target_user.id:
                        continue
                    BOT_REMOVING_ABSENCE_MESSAGE_IDS.add(message.id)
                    await message.delete(
                        reason=f"Nieobecność cofnięta przez {interaction.user}"
                    )
                    deleted_messages += 1
                    absence_removed = True
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as error:
                print(f"❌ Nie udało się usunąć wiadomości nieobecności z {thread_id}: {error}")

        if not absence_removed:
            await interaction.edit_original_response(
                content=(
                    f"❌ {self.target_user.mention} nie ma zapisanej nieobecności "
                    f"dla **{recording_display_name(recording)}**."
                ),
                view=None
            )
            return

        announcement_id = int(recording.get("announcement_message_id", message_id))
        announcement_url = (
            f"https://discord.com/channels/{GUILD_ID}/{NAGRYWKI_CHANNEL_ID}/{announcement_id}"
        )
        dm_sent = True
        try:
            await self.target_user.send(
                "↩️ **Twoja nieobecność została cofnięta.**\n\n"
                f"🎬 **{recording_display_name(recording)}**\n"
                f"📅 {recording['data']} • 🕒 {recording['godzina']}\n"
                f"📝 Powód: **{self.reason}**\n"
                f"🔗 Nagrywka: {announcement_url}\n\n"
                "Możesz teraz ponownie określić swoją obecność na tę nagrywkę."
            )
        except (discord.Forbidden, discord.HTTPException):
            dm_sent = False

        log_sent = False
        log_channel = bot.get_channel(NAGRYWKI_LOGS_CHANNEL_ID)
        if log_channel is None:
            try:
                log_channel = await bot.fetch_channel(NAGRYWKI_LOGS_CHANNEL_ID)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as error:
                print(f"❌ Nie znaleziono kanału logów cofniętych nieobecności: {error}")

        if log_channel is not None:
            log_embed = discord.Embed(
                title="↩️ Cofnięto nieobecność",
                description=(
                    f"Nieobecność {self.target_user.mention} została cofnięta.\n"
                    "Osoba może ponownie zapisać się na wskazaną nagrywkę."
                ),
                color=discord.Color.orange(),
                timestamp=datetime.now(ZoneInfo("Europe/Warsaw"))
            )
            log_embed.add_field(name="👤 Osoba", value=self.target_user.mention, inline=True)
            log_embed.add_field(name="🛡️ Cofnął", value=interaction.user.mention, inline=True)
            log_embed.add_field(
                name="🎬 Nagrywka",
                value=(
                    f"**{recording_display_name(recording)}**"
                    + (
                        f" • etap **{recording.get('double_position')}/2**"
                        if recording.get("double_group_id") else ""
                    )
                ),
                inline=False
            )
            log_embed.add_field(
                name="📅 Termin",
                value=f"{recording['data']} • {recording['godzina']}",
                inline=False
            )
            log_embed.add_field(name="📝 Powód", value=self.reason[:1024], inline=False)
            log_embed.add_field(
                name="🔗 Nagrywka",
                value=f"[Przejdź do wiadomości]({announcement_url})",
                inline=False
            )
            log_embed.set_thumbnail(url=self.target_user.display_avatar.url)
            log_embed.set_footer(
                text=(
                    f"ID użytkownika: {self.target_user.id} • "
                    f"Usunięte wiadomości: {deleted_messages}"
                )
            )
            try:
                await log_channel.send(
                    embed=log_embed,
                    allowed_mentions=discord.AllowedMentions.none()
                )
                log_sent = True
            except (discord.Forbidden, discord.HTTPException) as error:
                print(f"❌ Nie udało się wysłać logu cofniętej nieobecności: {error}")

        await interaction.edit_original_response(
            content=(
                f"✅ Cofnięto nieobecność {self.target_user.mention} dla "
                f"**{recording_display_name(recording)}**."
                + ("" if dm_sent else " ⚠️ Nie udało się wysłać wiadomości prywatnej.")
                + ("" if log_sent else " ⚠️ Nie udało się wysłać logu na kanał nagrywki-logi.")
            ),
            view=None
        )

class WithdrawAbsenceView(View):
    def __init__(self, recordings, target_user, reason):
        super().__init__(timeout=120)
        self.add_item(WithdrawAbsenceSelect(recordings, target_user, reason))

@bot.tree.command(
    name="cofnijnieobecnosc",
    description="Cofa nieobecność i ponownie pozwala osobie zapisać się na nagrywkę"
)
@app_commands.describe(
    user="Osoba, której nieobecność cofasz",
    powod="Opcjonalny powód cofnięcia nieobecności"
)
async def cofnijnieobecnosc(
    interaction: discord.Interaction,
    user: discord.Member,
    powod: str = "Decyzja administracji"
):
    if not any(role.id in STAFF_ROLES for role in interaction.user.roles):
        await send_response(interaction, "❌ Nie masz uprawnień.", ephemeral=True)
        return

    recordings = load_recordings()
    if not recordings:
        await send_response(interaction, "❌ Brak aktywnych nagrywek.", ephemeral=True)
        return

    await send_response(
        interaction,
        f"🎬 Wybierz nagrywkę, dla której cofasz nieobecność {user.mention}:",
        view=WithdrawAbsenceView(recordings, user, powod),
        ephemeral=True
    )

@bot.tree.command(
    name="naprawx2",
    description="Wymusza przeliczenie zapisów i odzyskanie logów aktywnej nagrywki X2"
)
async def naprawx2(interaction: discord.Interaction):
    if not any(role.id in STAFF_ROLES for role in interaction.user.roles):
        await send_response(interaction, "❌ Nie masz uprawnień.", ephemeral=True)
        return

    active_double_recordings = await asyncio.to_thread(
        lambda: list(recordings_collection.find({
            "double_group_id": {"$exists": True}
        }))
    )
    group_ids = list(dict.fromkeys(
        recording["double_group_id"] for recording in active_double_recordings
    ))
    if not group_ids:
        await send_response(
            interaction,
            "❌ Nie ma aktywnej podwójnej nagrywki.",
            ephemeral=True
        )
        return

    await reconcile_existing_double_absences()
    result_lines = []
    total_restored_logs = 0
    for group_id in group_ids:
        changes = await synchronize_double_group_attendance(group_id)
        total_restored_logs += await backfill_double_attendance_logs(
            force_group_id=group_id
        )
        readable_changes = ", ".join(
            f"etap {change['position']}/2: **{change['before']} → {change['after']}**"
            for change in sorted(changes, key=lambda item: item["position"])
        )
        result_lines.append(f"• {readable_changes}")

    await send_response(
        interaction,
        "✅ **Naprawa X2 zakończona.**\n"
        + "\n".join(result_lines)
        + f"\n♻️ Odzyskane logi: **{total_restored_logs}**",
        ephemeral=True
    )


@bot.tree.command(
    name="statusnagrywki",
    description="Pokazuje aktualny stan trwającej nagrywki"
)
async def statusnagrywki(interaction: discord.Interaction):
    if not any(role.id in STAFF_ROLES for role in interaction.user.roles):
        await send_response(interaction, "❌ Nie masz uprawnień.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    nagrywki = await asyncio.to_thread(load_recordings)
    if not nagrywki:
        await interaction.followup.send("❌ Brak aktywnej nagrywki.", ephemeral=True)
        return

    started_recordings = [
        (message_id, recording)
        for message_id, recording in nagrywki.items()
        if recording.get("started", False)
    ]

    if started_recordings:
        message_id, nagrywka = min(
            started_recordings,
            key=lambda item: item[1].get("timestamp", "")
        )
    else:
        message_id, nagrywka = min(
            nagrywki.items(),
            key=lambda item: item[1].get("timestamp", "")
        )

    try:
        termin = datetime.fromisoformat(nagrywka["timestamp"])
        if termin.tzinfo is None:
            termin = termin.replace(tzinfo=ZoneInfo("Europe/Warsaw"))
    except (KeyError, TypeError, ValueError):
        await interaction.followup.send("❌ Termin nagrywki ma nieprawidłowe dane.", ephemeral=True)
        return

    now = datetime.now(ZoneInfo("Europe/Warsaw"))
    if not nagrywka.get("started", False) or now < termin:
        embed = discord.Embed(
            title=f"🕒 {recording_display_name(nagrywka).upper()} — OCZEKUJE",
            description="Nagrywka nie została jeszcze rozpoczęta.",
            color=discord.Color.blue(),
            timestamp=now
        )
        embed.add_field(name="📅 Data", value=f"**{nagrywka['data']}**", inline=True)
        embed.add_field(name="🕒 Godzina", value=f"**{nagrywka['godzina']}**", inline=True)
        embed.add_field(name="⏳ Rozpoczęcie", value=f"<t:{int(termin.timestamp())}:R>", inline=True)
        embed.add_field(
            name="✅ Potwierdzone osoby",
            value=f"**{len(nagrywka.get('uczestnicy', []))}**",
            inline=False
        )
        embed.set_footer(text=f"ID terminu: {message_id}")
        await interaction.followup.send(embed=embed, ephemeral=True)
        return

    guild = interaction.guild
    allowed_role_ids = {NAGRYWKOWICZE_ROLE_ID, TESTOWI_ROLE_ID}
    eligible_ids = {
        member.id
        for role_id in allowed_role_ids
        for member in (guild.get_role(role_id).members if guild.get_role(role_id) else [])
        if not member.bot
    }

    voice_channel = guild.get_channel(NAGRYWKI_VC_ID)
    current_ids = {
        member.id
        for member in (voice_channel.members if isinstance(voice_channel, discord.VoiceChannel) else [])
        if member.id in eligible_ids
    }

    live_seconds = {
        int(user_id): int(seconds)
        for user_id, seconds in nagrywka.get("voice_seconds", {}).items()
        if int(user_id) in eligible_ids
    }
    for user_id, joined_text in nagrywka.get("voice_joined_at", {}).items():
        try:
            user_id_int = int(user_id)
            if user_id_int not in eligible_ids:
                continue
            joined_time = datetime.fromisoformat(joined_text)
            if joined_time.tzinfo is None:
                joined_time = joined_time.replace(tzinfo=ZoneInfo("Europe/Warsaw"))
            live_seconds[user_id_int] = live_seconds.get(user_id_int, 0) + max(
                0, int((now - joined_time).total_seconds())
            )
        except (TypeError, ValueError):
            continue

    qualified_ids = {
        user_id for user_id, seconds in live_seconds.items()
        if seconds >= MIN_VC_ATTENDANCE_SECONDS
    }
    late_lines = []
    for user_id, joined_text in nagrywka.get("first_voice_join_at", {}).items():
        try:
            user_id_int = int(user_id)
            if user_id_int not in eligible_ids:
                continue
            joined_time = datetime.fromisoformat(joined_text)
            if joined_time.tzinfo is None:
                joined_time = joined_time.replace(tzinfo=ZoneInfo("Europe/Warsaw"))
            late_seconds = int((joined_time - termin).total_seconds())
            if late_seconds > 0:
                late_lines.append(f"<@{user_id_int}> — **{(late_seconds + 59) // 60} min**")
        except (TypeError, ValueError):
            continue

    registered_ids = set(nagrywka.get("uczestnicy", [])) & eligible_ids
    registered_missing_ids = registered_ids - current_ids

    def mentions(user_ids, empty_text="Brak"):
        return "\n".join(f"<@{user_id}>" for user_id in sorted(user_ids))[:1024] or empty_text

    elapsed_minutes = max(0, int((now - termin).total_seconds() // 60))
    elapsed_hours, remaining_minutes = divmod(elapsed_minutes, 60)
    elapsed_text = (
        f"{elapsed_hours} godz. {remaining_minutes} min"
        if elapsed_hours else f"{remaining_minutes} min"
    )

    embed = discord.Embed(
        title=f"🔴 {recording_display_name(nagrywka).upper()} — TRWA",
        description=f"Nagrywka trwa już **{elapsed_text}**.",
        color=discord.Color.red(),
        timestamp=now
    )
    embed.add_field(
        name=f"🔊 Aktualnie na VC ({len(current_ids)})",
        value=mentions(current_ids, "Nikt z ekipy nie znajduje się obecnie na VC."),
        inline=False
    )
    embed.add_field(
        name=f"✅ Zaliczone 35 minut ({len(qualified_ids)})",
        value=mentions(qualified_ids, "Nikt nie zaliczył jeszcze wymaganego czasu."),
        inline=False
    )
    embed.add_field(
        name=f"⏰ Spóźnieni ({len(late_lines)})",
        value="\n".join(late_lines)[:1024] or "Brak spóźnień.",
        inline=False
    )
    embed.add_field(
        name=f"⚠️ Zapisani, których nie ma ({len(registered_missing_ids)})",
        value=mentions(registered_missing_ids, "Wszyscy zapisani są obecnie na VC."),
        inline=False
    )
    embed.set_footer(text=f"ID terminu: {message_id} • Dane aktualne w chwili użycia komendy")
    await interaction.followup.send(
        embed=embed,
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none()
    )

def recording_start_time(nagrywka):
    start_time = datetime.fromisoformat(nagrywka["timestamp"])
    if start_time.tzinfo is None:
        start_time = start_time.replace(tzinfo=ZoneInfo("Europe/Warsaw"))
    return start_time

async def finish_recording(interaction, message_id, nagrywka, nagrywki):
    now = datetime.now(ZoneInfo("Europe/Warsaw"))
    try:
        start_time = recording_start_time(nagrywka)
    except (KeyError, TypeError, ValueError):
        await send_response(
            interaction,
            "❌ Termin ma błędną datę rozpoczęcia. Nie został zakończony.",
            ephemeral=True
        )
        return False

    if now < start_time:
        await send_response(
            interaction,
            f"⛔ Tego terminu nie można jeszcze zakończyć. Rozpocznie się <t:{int(start_time.timestamp())}:R>.",
            ephemeral=True
        )
        return False

    # Zwykłą nagrywkę zamieniamy od razu w komunikat końcowy. W X2 jedna
    # wiadomość obsługuje oba etapy, więc jej wygląd odświeża osobny helper.
    if not nagrywka.get("double_group_id"):
        channel = bot.get_channel(NAGRYWKI_CHANNEL_ID)
        try:
            message = await channel.fetch_message(int(message_id))
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
            await message.edit(embed=embed, view=None)
        except (AttributeError, discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

    guild = bot.get_guild(GUILD_ID)
    if guild is not None:
        finalize_voice_sessions(nagrywka, now)
        statistics = await build_recording_statistics(message_id, nagrywka, guild)
        await asyncio.to_thread(
            recording_stats_collection.update_one,
            {"message_id": int(message_id)},
            {"$set": statistics},
            True
        )
        await send_recording_completion_report(
            message_id, nagrywka, statistics, now
        )

    if (
        nagrywka.get("double_group_id")
        and int(nagrywka.get("double_position", 0)) == 1
    ):
        for sibling in nagrywki.values():
            if (
                sibling.get("double_group_id") == nagrywka["double_group_id"]
                and int(sibling.get("double_position", 0)) == 2
            ):
                sibling["first_stage_completed"] = True
                sibling["stage_waiting"] = False
                sibling["started"] = True
                sibling["timestamp"] = now.isoformat()
                sibling["stage_started_at"] = now.isoformat()
                sibling["godzina"] = now.strftime("%H:%M")
                sibling["voice_seconds"] = {}
                sibling["voice_joined_at"] = {}
                sibling["first_voice_join_at"] = {}
                sibling["voice_exit_events"] = []
                sibling["reminder_sent"] = True
                sibling["report_sent"] = True
                sibling["missing_response_reminder_sent"] = True

                voice_channel = bot.get_channel(NAGRYWKI_VC_ID)
                if isinstance(voice_channel, discord.VoiceChannel):
                    for voice_member in voice_channel.members:
                        user_key = str(voice_member.id)
                        sibling["voice_joined_at"][user_key] = now.isoformat()
                        sibling["first_voice_join_at"][user_key] = now.isoformat()

    del nagrywki[str(message_id)]
    save_recordings(nagrywki)
    await asyncio.to_thread(refresh_recording_lock, nagrywki)
    await refresh_double_announcement_after_removal(
        nagrywka,
        nagrywki,
        f"Zakończono etap {nagrywka.get('double_position')}/2"
        if nagrywka.get("double_position") else "Zakończono nagrywkę"
    )
    return True

@bot.tree.command(
    name="zakonczetap",
    description="Kończy pierwszy etap aktywnej podwójnej nagrywki"
)
async def zakonczetap(interaction: discord.Interaction):
    if not any(role.id in STAFF_ROLES for role in interaction.user.roles):
        await send_response(interaction, "❌ Nie masz uprawnień.", ephemeral=True)
        return

    nagrywki = load_recordings()
    first_stage = next(
        (
            (message_id, nagrywka)
            for message_id, nagrywka in nagrywki.items()
            if nagrywka.get("double_group_id")
            and int(nagrywka.get("double_position", 0)) == 1
        ),
        None
    )
    if first_stage is None:
        await send_response(
            interaction,
            "❌ `/zakonczetap` działa tylko podczas pierwszego etapu podwójnej nagrywki.",
            ephemeral=True
        )
        return


    message_id, nagrywka = first_stage
    if await finish_recording(interaction, message_id, nagrywka, nagrywki):
        await send_response(
            interaction,
            "✅ Etap **1/2** został zakończony. Raport 1/2 jest już na kanale raportów.",
            ephemeral=True
        )

@bot.tree.command(
    name="zakoncznagrywke",
    description="Kończy aktywną nagrywkę albo drugi etap nagrywki X2"
)
async def zakoncznagrywke(
    interaction: discord.Interaction,
    recording_id: str = None
):
    if not any(role.id in STAFF_ROLES for role in interaction.user.roles):
        await send_response(interaction, "❌ Nie masz uprawnień.", ephemeral=True)
        return

    nagrywki = load_recordings()
    if not nagrywki:
        await send_response(interaction, "❌ Brak aktywnych nagrywek.", ephemeral=True)
        return

    double_recordings = [
        (message_id, nagrywka)
        for message_id, nagrywka in nagrywki.items()
        if nagrywka.get("double_group_id")
    ]
    if recording_id is None and double_recordings:
        first_active = any(
            int(nagrywka.get("double_position", 0)) == 1
            for _, nagrywka in double_recordings
        )
        if first_active:
            await send_response(
                interaction,
                "⛔ Najpierw zakończ etap **1/2** komendą `/zakonczetap`.",
                ephemeral=True
            )
            return
        second_stage = next(
            (
                item for item in double_recordings
                if int(item[1].get("double_position", 0)) == 2
            ),
            None
        )
        if second_stage:
            recording_id = second_stage[0]

    if recording_id is None:
        if len(nagrywki) == 1:
            recording_id = next(iter(nagrywki))
        else:
            await send_response(
                interaction,
                "🎬 Wybierz nagrywkę do zakończenia:",
                view=RecordingActionView("finish"),
                ephemeral=True
            )
            return

    nagrywka = nagrywki.get(str(recording_id))
    if nagrywka is None:
        await send_response(interaction, "❌ Nie znaleziono nagrywki.", ephemeral=True)
        return

    if nagrywka.get("double_group_id"):
        if int(nagrywka.get("double_position", 0)) == 1:
            await send_response(
                interaction,
                "⛔ Pierwszy etap nagrywki X2 kończysz komendą `/zakonczetap`.",
                ephemeral=True
            )
            return
        first_stage_active = any(
            item.get("double_group_id") == nagrywka.get("double_group_id")
            and int(item.get("double_position", 0)) == 1
            for item in nagrywki.values()
        )
        if first_stage_active:
            await send_response(
                interaction,
                "⛔ Najpierw zakończ etap **1/2** komendą `/zakonczetap`.",
                ephemeral=True
            )
            return

    if await finish_recording(
        interaction, str(recording_id), nagrywka, nagrywki
    ):
        success_text = (
            "✅ Etap **2/2** został zakończony. Raport 2/2 jest już na kanale raportów."
            if nagrywka.get("double_position") else
            "✅ Nagrywka została zakończona. Raport jest już na kanale raportów."
        )
        await send_response(
            interaction,
            success_text,
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
        if interaction.user.id == KACIEJ_USER_ID:
            await send_response(
                interaction,
                "👑 **Czego Ty tu szukasz, Olsztyński Książę?**",
                ephemeral=True
            )
            return

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

class DeleteRecordingStatisticsSelect(Select):
    def __init__(self, documents):
        self.documents = {
            str(document["message_id"]): document for document in documents
        }
        super().__init__(
            placeholder="Wybierz błędną nagrywkę do usunięcia ze statystyk...",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=recording_display_name(document)[:100],
                    description=(
                        f"{document.get('data', 'brak daty')} • "
                        f"{document.get('godzina', 'brak godziny')} • "
                        f"obecni: {len(document.get('confirmed_ids', []))} • "
                        f"ID: {document['message_id']}"
                    )[:100],
                    value=str(document["message_id"]),
                    emoji="🗑️"
                )
                for document in documents[:25]
            ]
        )

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != BOSS_USER_ID:
            await send_response(
                interaction,
                "❌ Ta komenda jest dostępna wyłącznie dla właściciela.",
                ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        message_id = int(self.values[0])
        document = self.documents.get(str(message_id))
        if document is None:
            await interaction.edit_original_response(
                content="❌ Nie znaleziono wybranego rekordu statystyk.",
                view=None
            )
            return

        result = await asyncio.to_thread(
            recording_stats_collection.delete_one,
            {"_id": document["_id"]}
        )
        if result.deleted_count == 0:
            await interaction.edit_original_response(
                content="❌ Ten rekord został już wcześniej usunięty.",
                view=None
            )
            return

        log_channel = bot.get_channel(NAGRYWKI_LOGS_CHANNEL_ID)
        if log_channel is not None:
            log_embed = discord.Embed(
                title="🗑️ Usunięto statystyki nagrywki",
                description=f"**{recording_display_name(document)}**",
                color=discord.Color.red(),
                timestamp=datetime.now(ZoneInfo("Europe/Warsaw"))
            )
            log_embed.add_field(
                name="📅 Termin",
                value=f"{document.get('data', 'brak')} • {document.get('godzina', 'brak')}",
                inline=True
            )
            log_embed.add_field(
                name="👥 Zapisana obecność",
                value=str(len(document.get("confirmed_ids", []))),
                inline=True
            )
            log_embed.add_field(
                name="👤 Usunął",
                value=interaction.user.mention,
                inline=False
            )
            log_embed.set_footer(text=f"ID statystyk: {message_id}")
            try:
                await log_channel.send(
                    embed=log_embed,
                    allowed_mentions=discord.AllowedMentions.none()
                )
            except (discord.Forbidden, discord.HTTPException):
                pass

        await interaction.edit_original_response(
            content=(
                f"✅ Usunięto **{recording_display_name(document)}** "
                f"({document.get('data', 'brak daty')} o "
                f"{document.get('godzina', 'brak godziny')}) ze wszystkich statystyk."
            ),
            view=None
        )

class DeleteRecordingStatisticsView(View):
    def __init__(self, documents):
        super().__init__(timeout=120)
        self.add_item(DeleteRecordingStatisticsSelect(documents))

@bot.tree.command(
    name="usunstatystyki",
    description="Trwale usuwa błędną nagrywkę ze statystyk"
)
async def usunstatystyki(interaction: discord.Interaction):
    if interaction.user.id != BOSS_USER_ID:
        await send_response(
            interaction,
            "❌ Ta komenda jest dostępna wyłącznie dla właściciela.",
            ephemeral=True
        )
        return

    documents = await asyncio.to_thread(
        lambda: list(
            recording_stats_collection.find().sort("timestamp", -1).limit(25)
        )
    )
    if not documents:
        await send_response(
            interaction,
            "❌ Nie ma żadnych zakończonych nagrywek w statystykach.",
            ephemeral=True
        )
        return

    await send_response(
        interaction,
        "🗑️ Wybierz błędną nagrywkę. Jej rekord zostanie trwale usunięty ze statystyk:",
        view=DeleteRecordingStatisticsView(documents),
        ephemeral=True
    )

@bot.tree.command(
    name="statystyki",
    description="Pokazuje statystyki zakończonych nagrywek"
)
@app_commands.describe(user="Opcjonalnie: statystyki konkretnej osoby")
async def statystyki(
    interaction: discord.Interaction,
    user: discord.Member = None
):
    if not any(role.id in STAFF_ROLES for role in interaction.user.roles):
        await send_response(
            interaction,
            "❌ Komenda `/statystyki` jest dostępna wyłącznie dla administracji. "
            f"Swoje podsumowanie możesz sprawdzić na kanale <#{PERSONAL_STATS_CHANNEL_ID}>.",
            ephemeral=True
        )
        return

    if user is not None and user.id == KACIEJ_USER_ID:
        await send_response(
            interaction,
            "🎬 **Serio? Gdyby go nie było, to nie byłoby nagrywek.**",
            ephemeral=True
        )
        return

    if user is not None and user.id == BOSS_USER_ID:
        await send_response(
            interaction,
            "🤨 **Szefa chcesz sprawdzać?**",
            ephemeral=True
        )
        return

    try:
        documents = await asyncio.wait_for(
            asyncio.to_thread(
                lambda: list(
                    recording_stats_collection.find()
                    .sort("timestamp", 1)
                    .max_time_ms(8000)
                )
            ),
            timeout=10
        )
    except (asyncio.TimeoutError, PyMongoError) as error:
        print(f"❌ Nie udało się pobrać statystyk: {type(error).__name__}: {error}")
        await send_response(
            interaction,
            "❌ Baza danych odpowiada zbyt wolno. Spróbuj ponownie za chwilę.",
            ephemeral=True
        )
        return

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
            "• Oddanego głosu **nie można zmienić**.\n"
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
        await interaction.response.defer(ephemeral=True, thinking=True)
        poll = await asyncio.to_thread(
            day_member_polls_collection.find_one,
            {"poll_id": self.poll_id, "closed": False}
        )
        if not poll:
            await send_response(interaction, "❌ To głosowanie jest już zakończone.", ephemeral=True)
            return

        if str(interaction.user.id) in poll.get("votes", {}):
            await send_response(
                interaction,
                "❌ Twój głos został już oddany i nie można go zmienić.",
                ephemeral=True
            )
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

        vote_result = await asyncio.to_thread(
            day_member_polls_collection.update_one,
            {
                "poll_id": self.poll_id,
                "closed": False,
                f"votes.{interaction.user.id}": {"$exists": False}
            },
            {"$set": {f"votes.{interaction.user.id}": candidate_id}}
        )
        if vote_result.modified_count == 0:
            await send_response(
                interaction,
                "❌ Twój głos został już oddany albo ankieta została zakończona.",
                ephemeral=True
            )
            return
        candidate = interaction.guild.get_member(candidate_id)
        candidate_name = candidate.display_name if candidate else f"ID {candidate_id}"
        await send_response(
            interaction,
            f"✅ Twój głos na **{candidate_name}** został zapisany.",
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
    for voter_id, candidate_id in votes.items():
        candidate_id = int(candidate_id)
        if candidate_id in counts and int(voter_id) != candidate_id:
            counts[candidate_id] += 1

    highest = max(counts.values(), default=0)
    winners = [user_id for user_id, count in counts.items() if count == highest and highest > 0]
    if highest == 1:
        vote_word = "głos"
    elif 2 <= highest % 10 <= 4 and not 12 <= highest % 100 <= 14:
        vote_word = "głosy"
    else:
        vote_word = "głosów"

    if not winners:
        result_text = "# 🗳️ Brak zwycięzcy\n## Nie oddano żadnego ważnego głosu"
    elif len(winners) == 1:
        result_text = (
            f"# 🥇 <@{winners[0]}>\n"
            f"## {highest} {vote_word}"
        )
    else:
        result_text = (
            "# 🤝 Remis zwycięzców\n"
            + ", ".join(f"<@{user_id}>" for user_id in winners)
            + f"\n## {highest} {vote_word}"
        )

    result_embed = discord.Embed(
        title=f"🏆 NAGRYWKOWICZ DNIA — {poll['recording_data']}",
        description=result_text,
        color=discord.Color.gold(),
        timestamp=datetime.now(ZoneInfo("Europe/Warsaw"))
    )
    if len(winners) == 1:
        winner = bot.get_user(winners[0])
        if winner:
            result_embed.set_thumbnail(url=winner.display_avatar.url)
    elif bot.user:
        result_embed.set_thumbnail(url=bot.user.display_avatar.url)
    result_embed.set_footer(text="NegativE* • Nagrywkowicz Dnia")

    channel = bot.get_channel(int(poll["channel_id"]))
    if channel is None:
        try:
            channel = await bot.fetch_channel(int(poll["channel_id"]))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            channel = None

    if channel is not None:
        try:
            message = await channel.fetch_message(int(poll["message_id"]))
            await message.edit(embed=result_embed, view=None)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

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

def combine_day_member_recordings(documents):
    combined = []
    double_groups = {}
    for document in documents:
        group_id = document.get("double_group_id")
        if group_id:
            double_groups.setdefault(group_id, []).append(document)
        else:
            combined.append(document)

    for group_id, stages in double_groups.items():
        # Ankietę X2 pokazujemy dopiero po zakończeniu obu etapów, żeby lista
        # kandydatów zawierała wszystkich faktycznie obecnych tego dnia.
        positions = {
            int(stage.get("double_position", 0)) for stage in stages
        }
        if positions != {1, 2}:
            continue
        stages = sorted(stages, key=lambda item: item.get("double_position", 0))
        first, second = stages
        confirmed_ids = sorted({
            int(user_id)
            for stage in stages
            for user_id in stage.get("confirmed_ids", [])
        })
        first_name = recording_display_name(first)
        second_name = recording_display_name(second)
        combined.append({
            "message_id": int(first["message_id"]),
            "opis": f"Nagrywka X2 — {first_name} + {second_name}",
            "data": first.get("data", second.get("data", "")),
            "godzina": first.get("godzina", ""),
            "timestamp": first.get("timestamp", ""),
            "confirmed_ids": confirmed_ids,
            "double_group_id": group_id,
            "double_stage_message_ids": [
                int(first["message_id"]), int(second["message_id"])
            ]
        })

    return sorted(
        combined,
        key=lambda item: item.get("timestamp", ""),
        reverse=True
    )

class DayMemberRecordingSelect(Select):
    def __init__(self, documents):
        self.documents = {str(doc["message_id"]): doc for doc in documents}
        super().__init__(
            placeholder="Wybierz zakończoną nagrywkę...",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=recording_display_name(doc)[:100],
                    description=(
                        f"{doc.get('data', '')} • {doc.get('godzina', '')} "
                        f"• ID: {doc['message_id']}"
                    )[:100],
                    value=str(doc["message_id"])
                )
                for doc in documents[:25]
            ]
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        document = self.documents.get(self.values[0])
        if not document:
            await send_response(interaction, "❌ Nie znaleziono nagrywki.", ephemeral=True)
            return

        allowed_role_ids = {NAGRYWKOWICZE_ROLE_ID, TESTOWI_ROLE_ID}
        candidates = []
        for user_id in document.get("confirmed_ids", []):
            member = interaction.guild.get_member(int(user_id))
            if (
                member
                and not member.bot
                and member.id != BOSS_USER_ID
                and any(role.id in allowed_role_ids for role in member.roles)
            ):
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
            "recording_opis": recording_display_name(document),
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
    documents = combine_day_member_recordings(documents)
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
        await interaction.response.defer(ephemeral=True, thinking=True)
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
    name="topnagrywkowiczdnia",
    description="Pokazuje ranking zwycięzców Nagrywkowicza Dnia"
)
async def topnagrywkowiczdnia(interaction: discord.Interaction):
    polls = await asyncio.to_thread(
        lambda: list(day_member_polls_collection.find(
            {"guild_id": interaction.guild.id, "closed": True},
            {"winner_ids": 1}
        ))
    )

    wins = {}
    for poll in polls:
        for user_id in poll.get("winner_ids", []):
            user_id = int(user_id)
            wins[user_id] = wins.get(user_id, 0) + 1

    if not wins:
        await send_response(
            interaction,
            "🏆 Nie ma jeszcze zakończonych ankiet ze zwycięzcą.",
            ephemeral=True
        )
        return

    ranking = sorted(wins.items(), key=lambda item: (-item[1], item[0]))
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for position, (user_id, win_count) in enumerate(ranking[:15], start=1):
        prefix = medals[position - 1] if position <= 3 else f"`{position}.`"
        win_word = "zwycięstwo" if win_count == 1 else (
            "zwycięstwa" if 2 <= win_count % 10 <= 4 and not 12 <= win_count % 100 <= 14
            else "zwycięstw"
        )
        lines.append(f"{prefix} <@{user_id}> — **{win_count} {win_word}**")

    embed = discord.Embed(
        title="🏆 TOPKA NAGRYWKOWICZA DNIA",
        description="\n".join(lines),
        color=discord.Color.gold(),
        timestamp=datetime.now(ZoneInfo("Europe/Warsaw"))
    )
    embed.add_field(
        name="📊 Podsumowanie",
        value=f"Zakończone ankiety: **{len(polls)}** • Zwycięzcy w rankingu: **{len(wins)}**",
        inline=False
    )
    if bot.user:
        embed.set_thumbnail(url=bot.user.display_avatar.url)
    embed.set_footer(text="Każdy remisowy zwycięzca otrzymuje jedno zwycięstwo")
    await send_response(
        interaction,
        embed=embed,
        allowed_mentions=discord.AllowedMentions.none()
    )

@bot.tree.command(
    name="praca",
    description="Zalicza Twoją obecność przez 5 kolejnych dni roboczych"
)
async def praca(interaction: discord.Interaction):
    if interaction.user.id != BOSS_USER_ID:
        await send_response(
            interaction,
            "❌ Ta komenda jest dostępna wyłącznie dla właściciela.",
            ephemeral=True
        )
        return

    current_date = datetime.now(ZoneInfo("Europe/Warsaw")).date()
    covered_dates = []
    candidate_date = current_date
    while len(covered_dates) < 5:
        if candidate_date.weekday() < 5:
            covered_dates.append(candidate_date.strftime("%d.%m.%Y"))
        candidate_date += timedelta(days=1)

    await asyncio.to_thread(
        work_credits_collection.update_one,
        {"user_id": BOSS_USER_ID},
        {"$set": {
            "covered_dates": covered_dates,
            "created_at": datetime.now(ZoneInfo("Europe/Warsaw")).isoformat()
        }},
        upsert=True
    )

    await send_response(
        interaction,
        (
            "💼 **Tryb pracy został włączony.**\n\n"
            "Nagrywki w poniższych dniach zostaną automatycznie zaliczone jako obecność:\n"
            + "\n".join(f"• **{date_text}**" for date_text in covered_dates)
        ),
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
