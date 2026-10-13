import asyncio
import random
import logging
import subprocess
import sys
import os
import re
import time
import shutil
import secrets
import json
import discord
from discord.ext import commands, tasks
import docker
import asyncio
import sqlite3
from dotenv import load_dotenv
from datetime import datetime, timezone, timedelta

# Load environment variables
load_dotenv()

# Configuration from .env
TOKEN = os.getenv('TOKEN', 'DISCORD_BOT_TOKEN')
ADMIN_ID = int(os.getenv('ADMIN_ID', 0))  # Admin user ID for checks
BOT_STATUS_NAME = os.getenv('BOT_STATUS_NAME', 'RDX')
WATERMARK = os.getenv('WATERMARK', 'Powered by RDX')
# VPS Defaults from .env
DEFAULT_RAM = os.getenv('DEFAULT_RAM', '2g')  # e.g., '2g', '4G'
DEFAULT_CPU = os.getenv('DEFAULT_CPU', '1')  # Lowered default to '1' to avoid common errors
DEFAULT_DISK = os.getenv('DEFAULT_DISK', '10G')  # e.g., '20G' - Note: Disk limit not enforced in container
VPS_HOSTNAME = os.getenv('VPS_HOSTNAME', 'rdx-free')  # Base hostname, append user ID
SERVER_LIMIT = int(os.getenv('SERVER_LIMIT', 1))
TOTAL_SERVER_LIMIT = int(os.getenv('TOTAL_SERVER_LIMIT', 50))  # Global total running server limit
DATABASE_FILE = os.getenv('DATABASE_FILE', 'vps_bot.db')
BOT_VERSION = os.getenv('BOT_VERSION', '1.0.0')  # Used dynamically in embed footers

# VPS validity / refill (renewal) system for VPS created with !deploy
VPS_VALIDITY_DAYS = float(os.getenv('VPS_VALIDITY_DAYS', 7))            # validity granted on deploy and added per refill
VPS_REFILL_AFTER_DAYS = float(os.getenv('VPS_REFILL_AFTER_DAYS', 5))    # refill window opens this long after deploy/last refill
VPS_EXPIRY_WARNING_HOURS = float(os.getenv('VPS_EXPIRY_WARNING_HOURS', 24))  # expiry warning DM lead time
VPS_EXPIRY_CHECK_MINUTES = float(os.getenv('VPS_EXPIRY_CHECK_MINUTES', 5))   # how often the background checker runs

# Channel where normal users are allowed to run !deploy and !manage.
# Admin (ADMIN_ID) can use both commands from any channel. 0 = not configured/invalid.
# Multiple servers: put several channel IDs separated by commas, e.g.
#   DEPLOY_COMMAND_CHANNEL_ID=193949293949,29394992919
_deploy_channel_raw = os.getenv('DEPLOY_COMMAND_CHANNEL_ID', '').strip()
DEPLOY_COMMAND_CHANNEL_IDS = set()
for _part in _deploy_channel_raw.replace(';', ',').replace(' ', ',').split(','):
    _part = _part.strip()
    if _part.isdigit() and int(_part) > 0:
        DEPLOY_COMMAND_CHANNEL_IDS.add(int(_part))
DEPLOY_COMMAND_CHANNEL_ID = min(DEPLOY_COMMAND_CHANNEL_IDS) if DEPLOY_COMMAND_CHANNEL_IDS else 0

# ---- Review gate: VPS stays OFF until the user posts the review phrase ----
# .env:  REVIEW=<review channel id>   (several servers: REVIEW=111,222)
REVIEW_PHRASE = os.getenv('REVIEW_MESSAGE', 'Legit got a FREE VPS').strip()
_review_raw = (os.getenv('REVIEW') or os.getenv('REVIEW_CHANNEL_ID') or '').strip()
REVIEW_CHANNEL_IDS = set()
for _rp in _review_raw.replace(';', ',').replace(' ', ',').split(','):
    _rp = _rp.strip()
    if _rp.isdigit() and int(_rp) > 0:
        REVIEW_CHANNEL_IDS.add(int(_rp))
_review_busy = set()
_review_enforced = False
# Bot restart must NOT switch running VPS off. (Set True only if you ever want that again.)
REVIEW_STOP_ON_START = False


def review_enabled():
    return bool(REVIEW_CHANNEL_IDS)


def _norm_text(text):
    return " ".join((text or "").lower().split())


def review_phrase_matches(text):
    # Very forgiving: all important words of the phrase must appear somewhere in the
    # message (any case, any order, extra text/typos around them are fine).
    # Small words (a / an / the / got) are optional.
    stop = {"a", "an", "the", "got"}
    toks = [t for t in re.findall(r'[a-z0-9]+', REVIEW_PHRASE.lower()) if t not in stop]
    if not toks:
        return False
    low = (text or "").lower()
    return all(t in low for t in toks)


def is_review_verified(user_id):
    conn = get_db_connection()
    row = conn.execute('SELECT 1 FROM review_verified WHERE user_id = ?', (user_id,)).fetchone()
    conn.close()
    return row is not None


def mark_review_verified(user_id):
    conn = get_db_connection()
    conn.execute('INSERT OR IGNORE INTO review_verified (user_id) VALUES (?)', (user_id,))
    conn.commit()
    conn.close()


def set_review_hold(container_id, value):
    conn = get_db_connection()
    conn.execute('UPDATE vps SET review_hold = ? WHERE container_id = ?', (value, container_id))
    conn.commit()
    conn.close()


def get_review_held_vps(user_id):
    conn = get_db_connection()
    rows = conn.execute('SELECT * FROM vps WHERE user_id = ? AND review_hold = 1', (user_id,)).fetchall()
    conn.close()
    return rows


def review_required_for_vps(vps):
    # Admin VPS and paid VPS (hostname rdx-paid) never need a review.
    if not review_enabled() or not vps:
        return False
    try:
        uid, host = vps['user_id'], vps['hostname']
    except Exception:
        return False
    if uid == ADMIN_ID or host == 'rdx-paid':
        return False
    return not is_review_verified(uid)


def review_channels_text(guild=None):
    ids = sorted(REVIEW_CHANNEL_IDS)
    if guild is not None:
        own = []
        for c in ids:
            ch = bot.get_channel(c)
            if ch is not None and getattr(ch, 'guild', None) is not None and ch.guild.id == guild.id:
                own.append(c)
        if own:
            ids = own
    return " ".join(f"<#{c}>" for c in ids)


def review_required_embed(guild=None):
    return discord.Embed(
        title="🔒 Review required",
        description=f"Pehle {review_channels_text(guild)} me ye message post karo, tabhi VPS chalega:\n```{REVIEW_PHRASE}```",
        color=discord.Color.red()
    )


# LXC/LXD backend image mapping. This project targets LXD (the modern LXC
# implementation, controlled via the `lxc` client CLI) rather than classic
# LXC tooling (lxc-create/lxc-start), since LXD is what ships on virtually
# all current Ubuntu/Debian hosts and gives us a single JSON-friendly CLI
# for create/start/stop/limits/stats. See the LXC section near `is_lxc_available`
# for host requirements.
LXC_UBUNTU_IMAGE = os.getenv('LXC_UBUNTU_IMAGE', 'ubuntu:22.04')
LXC_DEBIAN_IMAGE = os.getenv('LXC_DEBIAN_IMAGE', 'images:debian/12')

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('vps_bot.log'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# Intents
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)
bot.remove_command('help')  # replaced by our own !help panel below
client = docker.from_env()

def is_admin(member):
    if not isinstance(member, discord.Member):
        logger.warning("is_admin called with non-Member object")
        return False
    # Check user ID for admin access
    return member.id == ADMIN_ID

# Database setup with SQLite3
def init_db():
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    default_ram = DEFAULT_RAM
    default_cpu = DEFAULT_CPU
    default_disk = DEFAULT_DISK
    sql = f'''
        CREATE TABLE IF NOT EXISTS vps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            container_id TEXT UNIQUE NOT NULL,
            container_name TEXT NOT NULL,
            os_type TEXT NOT NULL,
            hostname TEXT NOT NULL,
            status TEXT DEFAULT 'stopped',
            ssh_command TEXT,
            ram TEXT DEFAULT '{default_ram}',
            cpu TEXT DEFAULT '{default_cpu}',
            disk TEXT DEFAULT '{default_disk}',
            suspended INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (user_id)
        )
    '''
    cursor.execute(sql)
    cursor.execute("PRAGMA table_info(vps)")
    columns = [col[1] for col in cursor.fetchall()]
    if 'suspended' not in columns:
        cursor.execute("ALTER TABLE vps ADD COLUMN suspended INTEGER DEFAULT 0")
    if 'expires_at' not in columns:
        # Safe migration: add_vps() writes this column; older DBs created before
        # it existed would otherwise fail on every VPS creation.
        cursor.execute("ALTER TABLE vps ADD COLUMN expires_at TIMESTAMP")
    if 'backend' not in columns:
        # Safe migration for Docker + LXC support. Existing VPS records default
        # to 'docker' so they keep being managed exactly as before.
        cursor.execute("ALTER TABLE vps ADD COLUMN backend TEXT DEFAULT 'docker'")
        cursor.execute("UPDATE vps SET backend = 'docker' WHERE backend IS NULL")
    # Refill/renewal system columns (safe migration; existing rows keep NULL = not managed by the refill system).
    # expiry_at is the existing 'expires_at' column; "status" is the existing 'status' column.
    for col, ddl in (
        ('deployed_at', 'TIMESTAMP'),
        ('refill_available_at', 'TIMESTAMP'),
        ('last_refill_at', 'TIMESTAMP'),
        ('refill_used_for_cycle', 'INTEGER DEFAULT 0'),
        ('expiry_status', "TEXT DEFAULT 'active'"),
        ('reminder_sent', 'INTEGER DEFAULT 0'),
        ('expiry_warning_sent', 'INTEGER DEFAULT 0'),
        ('review_hold', 'INTEGER DEFAULT 0'),
        ('qemu_password', 'TEXT'),
    ):
        if col not in columns:
            cursor.execute(f"ALTER TABLE vps ADD COLUMN {col} {ddl}")
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS bans (
            user_id INTEGER PRIMARY KEY
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    ''')
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('deploy_enabled', '1')")
    # Which backend new VPS instances use: 'docker' or 'lxc'. Toggled with !lxc on/off.
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('container_backend', 'docker')")
    # Whether the "Convert to QEMU" button in !manage is open to everyone ('on') or admin-only ('off', default).
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('qemu_mode', 'off')")
    cursor.execute('CREATE TABLE IF NOT EXISTS review_verified (user_id INTEGER PRIMARY KEY, verified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)')
    conn.commit()
    conn.close()

init_db()

def get_db_connection():
    conn = sqlite3.connect(DATABASE_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def get_setting(key, default=None):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT value FROM settings WHERE key = ?', (key,))
    row = cursor.fetchone()
    conn.close()
    return row['value'] if row else default

def set_setting(key, value):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value', (key, value))
    conn.commit()
    conn.close()

def is_deploy_enabled():
    return get_setting('deploy_enabled', '1') == '1'


def get_container_backend():
    """Which backend NEW VPS instances are created with: 'docker' or 'lxc'."""
    value = get_setting('container_backend', 'docker')
    return value if value in ('docker', 'lxc') else 'docker'


def set_container_backend(value):
    set_setting('container_backend', value)


def is_docker_available():
    """Best-effort check that the Docker daemon is reachable."""
    try:
        client.ping()
        return True
    except Exception:
        pass
    try:
        result = subprocess.run(["docker", "info"], capture_output=True, timeout=10)
        return result.returncode == 0
    except Exception:
        return False


def is_lxc_available():
    """
    Best-effort check that LXD is installed and reachable on this host.

    This project targets LXD (the `lxc` client CLI) rather than classic
    LXC (lxc-create/lxc-start/lxc-attach), since LXD is the actively
    maintained implementation and ships a single JSON-friendly CLI/API
    that covers create/start/stop/limits/stats in one place. Host
    requirements (Ubuntu/Debian):
        sudo snap install lxd
        sudo lxd init            # answer the setup prompts
        sudo usermod -aG lxd $USER   # so the bot process can run `lxc` without sudo
    """
    if shutil.which('lxc') is None:
        return False
    try:
        result = subprocess.run(["lxc", "list"], capture_output=True, timeout=10)
        return result.returncode == 0
    except Exception:
        return False


# snap installs binaries in /snap/bin, which is often missing from the bot's PATH
if '/snap/bin' not in os.environ.get('PATH', ''):
    os.environ['PATH'] = os.environ.get('PATH', '') + os.pathsep + '/snap/bin'

_lxd_install_lock = asyncio.Lock()


def _progress_bar(pct, width=20):
    pct = max(0, min(100, int(pct)))
    filled = int(width * pct / 100)
    return "█" * filled + "░" * (width - filled) + f" {pct}%"


def _lxd_install_steps():
    is_root = hasattr(os, "geteuid") and os.geteuid() == 0
    sudo = "" if is_root else "sudo -n "
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    steps = [
        ("Installing requirements (snapd)",
         f"command -v snap >/dev/null 2>&1 || ({sudo}apt-get update -y && {sudo}apt-get install -y snapd && {sudo}systemctl enable --now snapd.socket; sleep 5)", 60),
        ("Installing LXD",
         f"command -v lxc >/dev/null 2>&1 || {sudo}snap install lxd", 120),
        ("Initializing LXD",
         f"{sudo}lxd waitready --timeout 120; {sudo}lxd init --auto", 30),
        ("Setting permissions",
         (f"{sudo}usermod -aG lxd {user}" if user and user != "root" else "true"), 5),
        ("Downloading container image",
         f"{sudo}lxc image copy {LXC_UBUNTU_IMAGE} local: --auto-update 2>&1 || true", 90),
        ("Verifying LXC",
         f"{sudo}lxc list >/dev/null", 10),
    ]
    return steps


async def async_install_lxd(progress_cb=None):
    """
    Auto-install + initialise LXD with live progress.
    progress_cb(state: dict) is called repeatedly with:
      {"steps": [(title, status)], "pct": int, "current": str}
    status is one of: 'pending', 'running', 'done', 'failed'.
    Returns (ok, log_tail).
    """
    steps = _lxd_install_steps()
    n = len(steps)
    state = {"steps": [[t, "pending"] for t, _, _ in steps], "pct": 0, "current": steps[0][0]}
    log_tail = ""
    done_flag = False

    async def ticker():
        while not done_flag:
            if progress_cb:
                try:
                    await progress_cb(state)
                except Exception as e:
                    logger.warning(f"progress_cb error: {e}")
            await asyncio.sleep(3)

    tick_task = asyncio.create_task(ticker())
    ok = True
    try:
        for i, (title, cmd, expected) in enumerate(steps):
            state["steps"][i][1] = "running"
            state["current"] = title
            start = time.time()
            real_pct = None
            full = f'export DEBIAN_FRONTEND=noninteractive; export PATH="$PATH:/snap/bin"; {cmd}'
            proc = await asyncio.create_subprocess_exec(
                "bash", "-c", full,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
            )

            async def reader():
                nonlocal real_pct, log_tail
                while True:
                    chunk = await proc.stdout.read(1024)
                    if not chunk:
                        break
                    text = chunk.decode(errors="ignore")
                    log_tail = (log_tail + text)[-1500:]
                    found = re.findall(r'(\d{1,3})%', text)
                    if found:
                        real_pct = min(100, int(found[-1]))

            async def estimator():
                # animate progress inside the step (real % from snap if available,
                # otherwise a smooth time-based estimate that never reaches 100)
                while True:
                    if real_pct is not None:
                        inner = real_pct / 100.0
                    else:
                        t = time.time() - start
                        inner = 0.95 * (1 - 2.718281828 ** (-t / max(expected / 2, 1)))
                    state["pct"] = int(((i + inner) / n) * 100)
                    await asyncio.sleep(1)

            est = asyncio.create_task(estimator())
            try:
                await asyncio.wait_for(asyncio.gather(reader(), proc.wait()), timeout=900)
            except asyncio.TimeoutError:
                proc.kill()
                state["steps"][i][1] = "failed"
                log_tail += "\nStep timed out."
                ok = False
            finally:
                est.cancel()
            if not ok:
                break
            if proc.returncode != 0:
                state["steps"][i][1] = "failed"
                ok = False
                break
            state["steps"][i][1] = "done"
            state["pct"] = int(((i + 1) / n) * 100)
        if ok:
            state["pct"] = 100
            state["current"] = "Completed"
    except Exception as e:
        logger.error(f"LXD install error: {e}")
        log_tail += f"\n{e}"
        ok = False
    finally:
        done_flag = True
        tick_task.cancel()
        if progress_cb:
            try:
                await progress_cb(state)
            except Exception:
                pass
    if ok:
        logger.info("LXD installed and initialised")
    else:
        logger.error(f"LXD install failed: {log_tail}")
    return ok, log_tail


def _lxd_progress_embed(state, final_ok=None):
    icons = {"pending": "⬜", "running": "⏳", "done": "✅", "failed": "❌"}
    lines = "\n".join(f"{icons[st]} {title}" for title, st in state["steps"])
    if final_ok is True:
        color, title = discord.Color.green(), "✅ LXC/LXD Ready"
    elif final_ok is False:
        color, title = discord.Color.red(), "❌ LXC/LXD Install Failed"
    else:
        color, title = discord.Color.orange(), "⏳ Setting up LXC/LXD"
    embed = discord.Embed(title=title, color=color)
    embed.add_field(name="Progress", value=f"`{_progress_bar(state['pct'])}`", inline=False)
    embed.add_field(name="Steps", value=lines, inline=False)
    if final_ok is None:
        embed.set_footer(text=f"Now: {state['current']} • updates live")
    return embed


def get_host_mem_gb():
    """Total host RAM in GB, used for LXC resource validation (Docker uses client.info())."""
    try:
        with open('/proc/meminfo') as f:
            for line in f:
                if line.startswith('MemTotal:'):
                    kb = int(line.split()[1])
                    return kb / (1024 ** 2)
    except Exception as e:
        logger.error(f"Failed to read host memory info: {e}")
    return 0.0

# ---------------------------------------------------------------------------
# VPS expiry / refill (renewal) system
# All timing is derived from timestamps stored in the vps table (survives restarts).
# Only VPS created via !deploy (refill_available_at NOT NULL) are managed by it.
# ---------------------------------------------------------------------------

def _parse_ts(value):
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _dts(dt):
    """Discord timestamp: renders in each viewer's local time."""
    return f"<t:{int(dt.timestamp())}:F>"


def _fmt_remaining(delta):
    total_minutes = max(int(delta.total_seconds() // 60), 0)
    days, rem = divmod(total_minutes, 1440)
    hours, minutes = divmod(rem, 60)
    if days == 0 and hours == 0:
        return f"{max(minutes, 1)} minutes"
    return f"{days} days / {hours} hours"


def restore_refill_state(container_id, old):
    conn = get_db_connection()
    conn.execute('''
        UPDATE vps SET expires_at = ?, deployed_at = ?, refill_available_at = ?, last_refill_at = ?,
               refill_used_for_cycle = ?, expiry_status = ?, reminder_sent = ?, expiry_warning_sent = ?, suspended = ?
        WHERE container_id = ?
    ''', (old['expires_at'], old['deployed_at'], old['refill_available_at'], old['last_refill_at'],
          old['refill_used_for_cycle'], old['expiry_status'], old['reminder_sent'], old['expiry_warning_sent'],
          old['suspended'], container_id))
    conn.commit()
    conn.close()


def refill_vps_record(container_id, expected_refill_available_at):
    """Atomically apply a refill. Returns new expiry datetime, or None if the row changed underneath us."""
    now = datetime.now(timezone.utc)
    conn = get_db_connection()
    try:
        row = conn.execute('SELECT expires_at FROM vps WHERE container_id = ?', (container_id,)).fetchone()
        new_expiry = _parse_ts(row['expires_at']) + timedelta(days=VPS_VALIDITY_DAYS)
        cur = conn.execute('''
            UPDATE vps SET expires_at = ?, last_refill_at = ?, refill_available_at = ?, refill_used_for_cycle = 1,
                   reminder_sent = 0, expiry_warning_sent = 0
            WHERE container_id = ? AND refill_available_at = ? AND expiry_status = 'active'
        ''', (new_expiry.isoformat(), now.isoformat(), (now + timedelta(days=VPS_REFILL_AFTER_DAYS)).isoformat(),
              container_id, expected_refill_available_at))
        conn.commit()
        return new_expiry if cur.rowcount == 1 else None
    finally:
        conn.close()


@bot.command(name="vpsrefill")
async def vpsrefill_cmd(ctx, vps_identifier: str = None):
    # Only ever looks at the caller's own VPS.
    own = [v for v in get_user_vps(ctx.author.id) if v['refill_available_at']]
    if not own:
        await ctx.send(embed=discord.Embed(description="You have no VPS eligible for refill.", color=discord.Color.red()))
        return
    if vps_identifier:
        ident = vps_identifier.lower()
        matches = [v for v in own if ident in v['container_id'].lower() or ident in v['container_name'].lower()]
        if not matches:
            await ctx.send(embed=discord.Embed(description="No VPS of yours matches that ID/name. Use `!list` to see your VPS.", color=discord.Color.red()))
            return
        vps = matches[0]
    elif len(own) == 1:
        vps = own[0]
    else:
        await ctx.send(embed=discord.Embed(description="You have multiple VPS. Specify which one: `!vpsrefill <vps id or name>` (see `!list`).", color=discord.Color.orange()))
        return

    now = datetime.now(timezone.utc)
    if vps['expiry_status'] == 'expired' or _parse_ts(vps['expires_at']) <= now:
        await ctx.send(embed=discord.Embed(description="❌ This VPS has already expired and can no longer be refilled. Please contact an admin.", color=discord.Color.red()))
        return
    available_at = _parse_ts(vps['refill_available_at'])
    if now < available_at:
        if vps['refill_used_for_cycle']:
            desc = f"⚠️ Your VPS has already been refilled for this cycle.\n\nNext refill available:\n{_dts(available_at)}"
        else:
            desc = (f"⏳ VPS refill is not available yet.\n\nYou can refill your VPS after {VPS_REFILL_AFTER_DAYS:g} days.\n\n"
                    f"Remaining: {_fmt_remaining(available_at - now)}")
        await ctx.send(embed=discord.Embed(description=desc, color=discord.Color.orange()))
        return

    new_expiry = refill_vps_record(vps['container_id'], vps['refill_available_at'])
    if new_expiry is None:
        await ctx.send(embed=discord.Embed(description="⚠️ Your VPS has already been refilled for this cycle.", color=discord.Color.orange()))
        return
    logger.info(f"VPS {vps['container_id']} refilled by {ctx.author.id}; new expiry {new_expiry.isoformat()}")
    await ctx.send(embed=discord.Embed(
        description=f"♻️ VPS Renewed Successfully!\n\nYour VPS has been renewed for another {VPS_VALIDITY_DAYS:g} days.\n\nNew Expiry:\n{_dts(new_expiry)}",
        color=discord.Color.green()))


async def _dm_user(user_id, description, color):
    """Returns True if handled (sent, or DMs closed so retrying is pointless); False to retry later."""
    try:
        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
        await user.send(embed=discord.Embed(description=description, color=color))
        return True
    except discord.Forbidden:
        logger.warning(f"Cannot DM user {user_id} (DMs closed)")
        return True
    except Exception as e:
        logger.error(f"DM to {user_id} failed, will retry: {e!r}")
        return False


async def _expire_vps(row):
    cid = row['container_id']
    conn = get_db_connection()
    # Same mechanism as admin "suspend": stop the VPS and mark it suspended (nothing is deleted).
    conn.execute("UPDATE vps SET expiry_status = 'expired', suspended = 1 WHERE container_id = ? AND expiry_status = 'active'", (cid,))
    conn.commit()
    conn.close()
    logger.info(f"VPS {cid} (user {row['user_id']}) expired; suspending")
    try:
        if await backend_stop(cid, row['backend'] or 'docker'):
            update_vps_status(cid, "stopped")
    except Exception as e:
        logger.error(f"Stopping expired VPS {cid} failed (will retry): {e!r}")
    await _dm_user(row['user_id'],
                   "⌛ **VPS Expired**\n\nYour VPS was not renewed in time and has been suspended. Please contact an admin if you need it restored.",
                   discord.Color.red())


async def _check_one_vps(row, now):
    cid = row['container_id']
    if row['expiry_status'] == 'expired':
        # Retry stopping if an earlier stop failed (skip if an admin unsuspended it).
        if row['suspended'] and row['status'] == 'running':
            if await backend_stop(cid, row['backend'] or 'docker'):
                update_vps_status(cid, "stopped")
        return
    expires_at = _parse_ts(row['expires_at'])
    if expires_at <= now:
        await _expire_vps(row)
        return
    conn = get_db_connection()
    try:
        available_at = _parse_ts(row['refill_available_at'])
        if now >= available_at and not row['reminder_sent']:
            done = await _dm_user(row['user_id'],
                f"🔔 **VPS Renewal Available**\n\nYour VPS is now eligible for renewal.\n\nUse:\n`!vpsrefill`\n\n"
                f"Your VPS will expire on:\n{_dts(expires_at)}\n\nRenew it before expiry to keep your VPS active.",
                discord.Color.blue())
            if done:
                conn.execute("UPDATE vps SET reminder_sent = 1, refill_used_for_cycle = 0 WHERE container_id = ?", (cid,))
                conn.commit()
        if expires_at - now <= timedelta(hours=VPS_EXPIRY_WARNING_HOURS) and not row['expiry_warning_sent']:
            done = await _dm_user(row['user_id'],
                "⚠️ **VPS Expiry Warning**\n\nYour VPS will expire in approximately 24 hours.\n\nUse:\n`!vpsrefill`\n\n"
                f"to renew your VPS for another {VPS_VALIDITY_DAYS:g} days.\n\nExpires: {_dts(expires_at)}",
                discord.Color.orange())
            if done:
                conn.execute("UPDATE vps SET expiry_warning_sent = 1 WHERE container_id = ?", (cid,))
                conn.commit()
    finally:
        conn.close()


async def run_vps_expiry_check():
    now = datetime.now(timezone.utc)
    conn = get_db_connection()
    try:
        rows = conn.execute("SELECT * FROM vps WHERE refill_available_at IS NOT NULL AND expires_at IS NOT NULL").fetchall()
    finally:
        conn.close()
    for row in rows:
        try:
            await _check_one_vps(row, now)
        except Exception as e:
            logger.error(f"Expiry check failed for VPS {row['container_id']}: {e!r}")


@tasks.loop(minutes=VPS_EXPIRY_CHECK_MINUTES)
async def vps_expiry_checker():
    try:
        await run_vps_expiry_check()
    except Exception as e:  # never let the loop die
        logger.error(f"VPS expiry checker error: {e!r}")


DEPLOY_COOLDOWN_SECONDS = 100
deploy_cooldowns = {}
deploy_in_progress = set()
deploy_queue = []
deploy_queue_lock = asyncio.Lock()
deploy_worker_running = False
deploy_current = None  # (ctx, user, os_type) currently being deployed, guarded by deploy_queue_lock
DEPLOY_JOB_TIMEOUT_SECONDS = 1800  # safety net so one stuck deployment can never block the queue forever


def validate_deploy_channel_config():
    """Log a clear warning/error if DEPLOY_COMMAND_CHANNEL_ID is missing or invalid."""
    if not _deploy_channel_raw:
        logger.error("DEPLOY_COMMAND_CHANNEL_ID is not set in .env - normal users cannot use !deploy/!manage until it is set (admin is unaffected).")
    elif not DEPLOY_COMMAND_CHANNEL_IDS:
        logger.error(f"DEPLOY_COMMAND_CHANNEL_ID is invalid ({_deploy_channel_raw!r}); it must be a numeric Discord channel ID. Normal users cannot use !deploy/!manage until fixed.")


async def deploy_channel_allowed(ctx):
    """Admin may use the command anywhere; everyone else only in DEPLOY_COMMAND_CHANNEL_ID.
    Sends a short notice and returns False if not allowed."""
    if is_admin(ctx.author):
        return True
    if ctx.channel.id in DEPLOY_COMMAND_CHANNEL_IDS:
        return True
    if DEPLOY_COMMAND_CHANNEL_IDS:
        msg = "This command can only be used in the designated deployment channel."
    else:
        msg = "The deployment channel has not been configured yet. Please contact an admin."
    await ctx.send(embed=discord.Embed(description=msg, color=discord.Color.red()))
    return False

def add_user(user_id, username):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)', (user_id, username))
    conn.commit()
    conn.close()

def add_ban(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('INSERT OR IGNORE INTO bans (user_id) VALUES (?)', (user_id,))
    conn.commit()
    conn.close()

def remove_ban(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM bans WHERE user_id = ?', (user_id,))
    conn.commit()
    conn.close()

def is_banned(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT 1 FROM bans WHERE user_id = ?', (user_id,))
    banned = cursor.fetchone() is not None
    conn.close()
    return banned

def add_vps(user_id, container_id, container_name, os_type, hostname, ssh_command, ram=DEFAULT_RAM, cpu=DEFAULT_CPU, disk=DEFAULT_DISK, expiry_days=30, backend='docker', refill_managed=False):
    ssh_command = clean_ssh_text(ssh_command)
    conn = get_db_connection()
    cursor = conn.cursor()
    now = datetime.now(timezone.utc)
    expires_at = (now + timedelta(days=expiry_days)).isoformat() if expiry_days else None
    cursor.execute('''
        INSERT INTO vps (user_id, container_id, container_name, os_type, hostname, status, ssh_command, ram, cpu, disk, suspended, expires_at, backend)
        VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, 0, ?, ?)
    ''', (user_id, container_id, container_name, os_type, hostname, ssh_command, ram, cpu, disk, expires_at, backend))
    if refill_managed and expires_at:
        # Timers start only now, after successful creation.
        cursor.execute('''
            UPDATE vps SET deployed_at = ?, refill_available_at = ?, last_refill_at = NULL,
                   refill_used_for_cycle = 0, expiry_status = 'active', reminder_sent = 0, expiry_warning_sent = 0
            WHERE container_id = ?
        ''', (now.isoformat(), (now + timedelta(days=VPS_REFILL_AFTER_DAYS)).isoformat(), container_id))
    conn.commit()
    conn.close()

def get_user_vps(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM vps WHERE user_id = ? ORDER BY created_at DESC', (user_id,))
    vps_list = cursor.fetchall()
    conn.close()
    return vps_list

def count_user_vps(user_id):
    return len(get_user_vps(user_id))

def get_vps_by_container_id(container_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM vps WHERE container_id = ?', (container_id,))
    vps = cursor.fetchone()
    conn.close()
    return vps

def get_vps_by_identifier(user_id, identifier):
    vps_list = get_user_vps(user_id)
    if not identifier:
        return vps_list[0] if vps_list else None
    identifier_lower = identifier.lower()
    for vps in vps_list:
        if (identifier_lower in vps['container_id'].lower() or
            identifier_lower in vps['container_name'].lower()):
            return vps
    return None

def update_vps_status(container_id, status):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('UPDATE vps SET status = ? WHERE container_id = ?', (status, container_id))
    conn.commit()
    conn.close()

def update_vps_ssh(container_id, ssh_command):
    ssh_command = clean_ssh_text(ssh_command)
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('UPDATE vps SET ssh_command = ? WHERE container_id = ?', (ssh_command, container_id))
    conn.commit()
    conn.close()

def update_vps_qemu(container_id, is_qemu, password=None):
    conn = get_db_connection()
    conn.execute('UPDATE vps SET backend = ?, qemu_password = ? WHERE container_id = ?',
                 ('qemu' if is_qemu else 'docker', password, container_id))
    conn.commit()
    conn.close()


def qemu_mode_enabled():
    return get_setting('qemu_mode', 'off') == 'on'


def update_vps_suspended(container_id, suspended):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('UPDATE vps SET suspended = ? WHERE container_id = ?', (suspended, container_id))
    conn.commit()
    conn.close()


def update_vps_resources(container_id, ram, cpu):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('UPDATE vps SET ram = ?, cpu = ? WHERE container_id = ?', (ram, cpu, container_id))
    conn.commit()
    conn.close()


def delete_vps(container_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM vps WHERE container_id = ?', (container_id,))
    conn.commit()
    conn.close()

def get_total_instances():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(*) FROM vps WHERE status = "running"')
    count = cursor.fetchone()[0]
    conn.close()
    return count

def parse_gb(resource_str):
    match = re.match(r'(\d+(?:\.\d+)?)([mMgG])?', resource_str.lower())
    if match:
        num = float(match.group(1))
        unit = match.group(2) or 'g'
        if unit in ['g', '']:
            return num
        elif unit in ['m']:
            return num / 1024.0
    return 0.0

def get_uptime(container_id, backend='docker'):
    if backend == 'lxc':
        return get_lxc_uptime(container_id)
    if backend == 'qemu':
        try:
            out = subprocess.check_output(["screen", "-list"], stderr=subprocess.STDOUT).decode()
            return "Running" if f".qemu-{container_id}" in out else "Not running"
        except Exception:
            return "Not running"
    try:
        output = subprocess.check_output(["docker", "inspect", "-f", "{{.State.StartedAt}}", container_id], stderr=subprocess.STDOUT).decode().strip()
        if output == "<no value>":
            return "Not running"
        start_time = datetime.fromisoformat(output.replace('Z', '+00:00'))
        now = datetime.now(timezone.utc)
        uptime = now - start_time
        days = uptime.days
        hours, remainder = divmod(uptime.seconds, 3600)
        minutes, _ = divmod(remainder, 60)
        return f"{days}d {hours}h {minutes}m"
    except Exception as e:
        logger.error(f"Uptime error for {container_id}: {e}")
        return "Unknown"

def get_stats(container_id, backend='docker'):
    if backend == 'lxc':
        return get_lxc_stats(container_id)
    if backend == 'qemu':
        return {'cpu': 'N/A (QEMU)', 'mem': 'N/A (QEMU)', 'net': 'N/A (QEMU)'}
    try:
        output = subprocess.check_output([
            "docker", "stats", "--no-stream", "--format",
            "{{.CPUPerc}}\t{{.MemUsage}}\t{{.NetIO}}",
            container_id
        ], stderr=subprocess.STDOUT).decode().strip()
        parts = output.split('\t')
        if len(parts) == 3:
            cpu, mem, net = parts
            return {'cpu': cpu, 'mem': mem, 'net': net}
    except Exception as e:
        logger.error(f"Stats error for {container_id}: {e}")
    return {'cpu': 'N/A', 'mem': 'N/A', 'net': 'N/A'}

def get_logs(container_id, lines=50, backend='docker'):
    if backend == 'lxc':
        return get_lxc_logs(container_id, lines)
    if backend == 'qemu':
        return "Logs aren't available for QEMU VMs. Use the SSHX console instead."
    try:
        output = subprocess.check_output(["docker", "logs", "--tail", str(lines), container_id], stderr=subprocess.STDOUT).decode()
        return output[-2000:]  # Truncate for Discord limit
    except Exception as e:
        logger.error(f"Logs error for {container_id}: {e}")
        return "Failed to fetch logs"


# ----------------------------- LXC/LXD equivalents --------------------------

def _lxc_list_json(name=None):
    cmd = ["lxc", "list"]
    if name:
        cmd.append(name)
    cmd += ["--format", "json"]
    try:
        output = subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=15).decode()
        return json.loads(output)
    except Exception as e:
        logger.error(f"lxc list error for {name}: {e}")
        return []

def get_lxc_uptime(container_id):
    data = _lxc_list_json(container_id)
    if not data:
        return "Unknown"
    inst = data[0]
    if inst.get('status') != 'Running':
        return "Not running"
    last_used = inst.get('last_used_at')
    if not last_used:
        return "Unknown"
    try:
        start_time = datetime.fromisoformat(last_used.replace('Z', '+00:00'))
        now = datetime.now(timezone.utc)
        uptime = now - start_time
        days = uptime.days
        hours, remainder = divmod(uptime.seconds, 3600)
        minutes, _ = divmod(remainder, 60)
        return f"{days}d {hours}h {minutes}m"
    except Exception as e:
        logger.error(f"LXC uptime parse error for {container_id}: {e}")
        return "Unknown"

def get_lxc_stats(container_id):
    data = _lxc_list_json(container_id)
    if not data:
        return {'cpu': 'N/A', 'mem': 'N/A', 'net': 'N/A'}
    inst = data[0]
    state = inst.get('state') or {}
    cpu_info = state.get('cpu') or {}
    mem_info = state.get('memory') or {}
    network = state.get('network') or {}
    cpu_usage_ns = cpu_info.get('usage')
    cpu_str = f"{cpu_usage_ns / 1e9:.1f}s CPU time" if cpu_usage_ns is not None else "N/A"
    mem_usage = mem_info.get('usage')
    mem_str = f"{mem_usage / (1024 ** 2):.0f} MiB" if mem_usage is not None else "N/A"
    if network:
        rx_total = sum((iface.get('counters', {}) or {}).get('bytes_received', 0) for iface in network.values())
        tx_total = sum((iface.get('counters', {}) or {}).get('bytes_sent', 0) for iface in network.values())
        net_str = f"{rx_total / 1024 / 1024:.2f}MB / {tx_total / 1024 / 1024:.2f}MB"
    else:
        net_str = "N/A"
    return {'cpu': cpu_str, 'mem': mem_str, 'net': net_str}

def get_lxc_logs(container_id, lines=50):
    # LXD containers run a full init system, so there's no single "app log"
    # like `docker logs`. Best-effort equivalent: journalctl inside the
    # container when running, falling back to the boot console log.
    try:
        output = subprocess.check_output(
            ["lxc", "exec", container_id, "--", "journalctl", "-n", str(lines), "--no-pager"],
            stderr=subprocess.STDOUT, timeout=20
        ).decode()
        return output[-2000:]
    except Exception as e:
        logger.warning(f"LXC journalctl logs failed for {container_id}, trying console log: {e}")
    try:
        output = subprocess.check_output(
            ["lxc", "console", container_id, "--show-log"], stderr=subprocess.STDOUT, timeout=20
        ).decode()
        return output[-2000:]
    except Exception as e:
        logger.error(f"LXC logs error for {container_id}: {e}")
        return "Failed to fetch logs"

# Async Docker helpers
def _lxcfs_mount_args(skip_meminfo=False):
    """If lxcfs is running on the host, bind its fake /proc files into the
    container so `free -h`, neofetch, htop, uptime show ONLY the allotted
    RAM/CPU instead of the whole host."""
    base = "/var/lib/lxcfs"
    files = ["cpuinfo", "diskstats", "meminfo", "stat", "swaps", "uptime", "loadavg"]
    args = []
    if _lxcfs_active():
        for f in files:
            if f == "meminfo" and skip_meminfo:
                continue
            if os.path.exists(f"{base}/proc/{f}"):
                args += ["-v", f"{base}/proc/{f}:/proc/{f}:rw"]
        if os.path.exists(f"{base}/sys/devices/system/cpu/online"):
            args += ["-v", f"{base}/sys/devices/system/cpu:/sys/devices/system/cpu:rw"]
    return args



# ---- RAM/CPU masking: VPS must see only its allotted RAM, not the host's ----
FAKE_PROC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_proc")


def _lxcfs_active():
    return os.path.ismount("/var/lib/lxcfs") and os.path.exists("/var/lib/lxcfs/proc/meminfo")


# .env:  SHOW_RAM_MULTIPLIER=2   -> a VPS with 16g real RAM shows 32g (and used x2) inside
try:
    SHOW_RAM_MULTIPLIER = min(8.0, max(0.25, float(os.getenv('SHOW_RAM_MULTIPLIER', '2') or 2)))
except ValueError:
    SHOW_RAM_MULTIPLIER = 1.0


def _meta_path(path):
    return (path[:-len(".meminfo")] if path.endswith(".meminfo") else path) + ".meta"


def _read_fake_meta(path):
    import json
    try:
        with open(_meta_path(path)) as f:
            m = json.load(f)
        return {"real": float(m["real"]), "mult": float(m["mult"])}
    except Exception:
        return None


def _write_fake_meminfo(path, total_bytes, used_bytes, mult=None):
    # total_bytes / used_bytes are the REAL numbers; the file shows them x mult.
    import json
    if mult is None:
        mult = SHOW_RAM_MULTIPLIER
    os.makedirs(os.path.dirname(path), exist_ok=True)
    total_kb = int(total_bytes * mult // 1024)
    used_kb = max(0, min(int(used_bytes * mult // 1024), total_kb))
    free_kb = total_kb - used_kb
    lines = [
        ("MemTotal", total_kb), ("MemFree", free_kb), ("MemAvailable", free_kb),
        ("Buffers", 0), ("Cached", 0), ("SwapCached", 0),
        ("Active", used_kb), ("Inactive", 0),
        ("SwapTotal", 0), ("SwapFree", 0), ("Dirty", 0), ("Writeback", 0),
        ("Shmem", 0), ("Slab", 0), ("SReclaimable", 0), ("SUnreclaim", 0),
    ]
    data = "".join(f"{k + ':':<16}{v:>10} kB\n" for k, v in lines)
    with open(path, "w") as f:   # same inode, so the bind mount keeps working
        f.write(data)
    with open(_meta_path(path), "w") as f:
        json.dump({"real": total_bytes, "mult": mult}, f)


def _meminfo_mount_args(container_name, ram, hostname=""):
    """Fallback when lxcfs is not usable (e.g. container-type VPS):
    bind-mount a fake /proc/meminfo showing only the allotted RAM."""
    mult = _vps_ram_mult(hostname)
    if _lxcfs_active() and mult == 1:
        return []
    try:
        path = os.path.join(FAKE_PROC_DIR, f"{container_name}.meminfo")
        total = parse_gb(ram) * (1024 ** 3) / mult   # REAL limit; file shows total x mult
        _write_fake_meminfo(path, total, total * 0.03, mult=mult)
        return ["-v", f"{path}:/proc/meminfo:ro"]
    except Exception as e:
        logger.warning(f"fake meminfo setup failed: {e}")
        return []


def _is_paid_host(hostname):
    return (hostname or "") == "rdx-paid"


def _vps_ram_mult(hostname):
    # Paid VPS always get exactly what was sold. Free VPS: real RAM = shown RAM / multiplier.
    return 1.0 if _is_paid_host(hostname) else SHOW_RAM_MULTIPLIER


async def _container_name_of(container_id):
    proc = await asyncio.create_subprocess_exec(
        "docker", "inspect", "--format", "{{.Name}}", str(container_id),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await asyncio.wait_for(proc.communicate(), timeout=20.0)
    return out.decode().strip().lstrip("/")


async def _real_ram_bytes_for(container_id, shown_bytes):
    # For upgrades: real limit = shown RAM / that container's multiplier (from its meta file).
    try:
        name = await _container_name_of(container_id)
        meta = _read_fake_meta(os.path.join(FAKE_PROC_DIR, f"{name}.meminfo")) if name else None
        mult = meta["mult"] if meta else 1.0
    except Exception:
        mult = 1.0
    return shown_bytes / (mult or 1.0)


def _read_fake_total(path):
    try:
        with open(path) as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024
    except Exception:
        pass
    return 0


async def _sync_fake_meminfo_total(container_id, ram):
    """After a RAM upgrade, make the fake meminfo show the new total."""
    name = await _container_name_of(container_id)
    path = os.path.join(FAKE_PROC_DIR, f"{name}.meminfo")
    if name and os.path.exists(path):
        meta = _read_fake_meta(path)
        m = meta["mult"] if meta else 1.0
        _write_fake_meminfo(path, parse_gb(ram) * (1024 ** 3) / m, 0, mult=m)


def _parse_docker_size(text):
    m = re.match(r"\s*([\d.]+)\s*([a-zA-Z]*)", text or "")
    if not m:
        return 0
    units = {"b": 1, "kb": 1000, "mb": 1000**2, "gb": 1000**3, "tb": 1000**4,
             "kib": 1024, "mib": 1024**2, "gib": 1024**3, "tib": 1024**4}
    return float(m.group(1)) * units.get(m.group(2).lower(), 1)


@tasks.loop(seconds=15)
async def fake_meminfo_updater():
    """Keeps the fake meminfo 'used' value live (and follows RAM upgrades)."""
    if (_lxcfs_active() and SHOW_RAM_MULTIPLIER == 1) or not os.path.isdir(FAKE_PROC_DIR):
        return
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "stats", "--no-stream", "--format", "{{.Name}}|{{.MemUsage}}",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=40.0)
        for line in out.decode(errors="ignore").splitlines():
            name, _, usage = line.partition("|")
            path = os.path.join(FAKE_PROC_DIR, f"{name.strip()}.meminfo")
            if not os.path.exists(path):
                continue
            used_s, _, lim_s = usage.partition(" / ")
            used = _parse_docker_size(used_s)
            meta = _read_fake_meta(path)
            if meta:
                limit, mult = meta["real"], meta["mult"]
            else:
                limit, mult = (_read_fake_total(path) or _parse_docker_size(lim_s)), 1.0
            if limit > 0:
                _write_fake_meminfo(path, limit, used, mult=mult)
    except Exception as e:
        logger.warning(f"fake meminfo update failed: {e}")


async def async_docker_run(image, hostname, ram, cpu, disk, container_name):
    _mult = _vps_ram_mult(hostname)
    _real_bytes = int(parse_gb(ram) * (1024 ** 3) / _mult)
    cmd = [
        "docker", "run", "-d",
        "--privileged", "--cap-add=ALL",
        "--restart", "unless-stopped",
        f"--memory={_real_bytes}",
        f"--memory-swap={_real_bytes}",
        f"--cpus={min(float(cpu), float(os.cpu_count() or 1))}",
        f"--hostname={hostname}",
        f"--name={container_name}",
        *_lxcfs_mount_args(skip_meminfo=(_mult != 1)),
        *_meminfo_mount_args(container_name, ram, hostname),
        image,
        "tail", "-f", "/dev/null"
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60.0)
        if proc.returncode != 0:
            logger.error(f"Docker run failed: {stderr.decode()}")
            return None
        return stdout.decode().strip()
    except asyncio.TimeoutError:
        logger.error("Docker run timed out")
        return None
    except Exception as e:
        logger.error(f"Docker run error: {e}")
        return None

async def async_docker_upgrade(container_id, ram, cpu):
    try:
        mem_bytes = int(await _real_ram_bytes_for(container_id, parse_gb(ram) * (1024 ** 3)))
        cmd = [
            "docker", "update",
            "--memory", str(mem_bytes),
            "--memory-swap", str(mem_bytes),
            "--cpus", str(min(float(cpu), float(os.cpu_count() or 1))),
            container_id
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.error(f"docker update failed for {container_id}: {stderr.decode()}")
            return False
        try:
            await _sync_fake_meminfo_total(container_id, ram)
        except Exception as e:
            logger.warning(f"fake meminfo sync failed: {e}")
        return True
    except Exception as e:
        logger.error(f"async_docker_upgrade failed for {container_id}: {e}")
        return False


async def async_docker_start(container_id):
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "start", container_id,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL
        )
        await asyncio.wait_for(proc.communicate(), timeout=30.0)
        return proc.returncode == 0
    except asyncio.TimeoutError:
        logger.warning(f"Docker start timeout for {container_id}")
        return False
    except Exception as e:
        logger.error(f"Docker start error for {container_id}: {e}")
        return False

async def async_docker_stop(container_id):
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "stop", container_id,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL
        )
        await asyncio.wait_for(proc.communicate(), timeout=30.0)
        return proc.returncode == 0
    except asyncio.TimeoutError:
        logger.warning(f"Docker stop timeout for {container_id}")
        try:
            await asyncio.create_subprocess_exec("docker", "kill", container_id, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL).communicate()
        except:
            pass
        return False
    except Exception as e:
        logger.error(f"Docker stop error for {container_id}: {e}")
        return False

async def async_docker_restart(container_id):
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "restart", container_id,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL
        )
        await asyncio.wait_for(proc.communicate(), timeout=30.0)
        return proc.returncode == 0
    except asyncio.TimeoutError:
        logger.warning(f"Docker restart timeout for {container_id}")
        return False
    except Exception as e:
        logger.error(f"Docker restart error for {container_id}: {e}")
        return False

async def async_docker_rm(container_id):
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "rm", "-f", container_id,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL
        )
        await proc.communicate()
        return proc.returncode == 0
    except Exception as e:
        logger.error(f"Docker rm error for {container_id}: {e}")
        return False

async def async_install_tmate(container_id, os_type):
    install_cmd = "curl -sSf https://sshx.io/get | sh"
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", container_id, "bash", "-c", install_cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120.0)
        if proc.returncode != 0:
            logger.warning(f"sshx install warning for {container_id}: {stderr.decode()}")
        else:
            logger.info(f"sshx installed in {container_id}")
    except asyncio.TimeoutError:
        logger.error(f"sshx install timeout for {container_id}")
    except Exception as e:
        logger.error(f"Failed to install sshx in {container_id}: {e}")

# sshx / tmate print coloured output: strip the ANSI codes (they show up as "[0m" in Discord)
_ANSI_RE = re.compile(r'\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-Z\\-_]')


def clean_ssh_text(text):
    if not text:
        return text
    t = _ANSI_RE.sub('', str(text))
    t = re.sub(r'\[[0-9;]*m', '', t)   # leftovers whose ESC byte got lost
    return t.strip()


def clean_existing_ssh_lines():
    try:
        conn = get_db_connection()
        rows = conn.execute("SELECT container_id, ssh_command FROM vps WHERE ssh_command IS NOT NULL AND ssh_command != ''").fetchall()
        fixed = 0
        for r in rows:
            c = clean_ssh_text(r['ssh_command'])
            if c != r['ssh_command']:
                conn.execute('UPDATE vps SET ssh_command = ? WHERE container_id = ?', (c, r['container_id']))
                fixed += 1
        conn.commit()
        conn.close()
        if fixed:
            logger.info(f"Cleaned colour codes from {fixed} stored ssh link(s)")
    except Exception as e:
        logger.warning(f"clean_existing_ssh_lines failed: {e!r}")


# SSH capture
async def capture_ssh_session_line(process):
    while True:
        try:
            output = await asyncio.wait_for(process.stdout.readline(), timeout=30.0)
            if not output:
                break
            output = clean_ssh_text(output.decode('utf-8', errors='ignore'))
            if "https://sshx.io/s/" in output:
                m = re.search(r'https://sshx\.io/s/[A-Za-z0-9_\-]+(?:#[A-Za-z0-9_\-+/=]+)?', output)
                if m:
                    return m.group(0)
                idx = output.find("https://sshx.io/s/")
                return output[idx:].strip()
        except asyncio.TimeoutError:
            break
    return None

async def async_install_real_tmate(container_id):
    install_cmd = "apt-get update && apt-get install -y tmate"
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", container_id, "bash", "-c", install_cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120.0)
        if proc.returncode != 0:
            logger.warning(f"tmate install warning for {container_id}: {stderr.decode()}")
        else:
            logger.info(f"real tmate installed in {container_id}")
    except asyncio.TimeoutError:
        logger.error(f"tmate install timeout for {container_id}")
    except Exception as e:
        logger.error(f"Failed to install real tmate in {container_id}: {e}")


async def capture_real_tmate_line(process, max_lines=40, max_seconds=45):
    start = time.time()
    lines_seen = 0
    while True:
        if time.time() - start > max_seconds or lines_seen >= max_lines:
            logger.info("capture_real_tmate_line: giving up (retry loop, tmate.io unreachable)")
            try:
                process.kill()
            except ProcessLookupError:
                pass
            break
        try:
            output = await asyncio.wait_for(process.stdout.readline(), timeout=10.0)
            if not output:
                logger.info("capture_real_tmate_line: stdout closed, no more output")
                break
            output = output.decode('utf-8').strip()
            lines_seen += 1
            logger.info(f"tmate raw line: {output!r}")
            if "ssh session:" in output.lower():
                idx = output.lower().find("ssh session:")
                return clean_ssh_text(output[idx + len("ssh session:"):])
        except asyncio.TimeoutError:
            logger.info("capture_real_tmate_line: timed out waiting for a line")
            break
    return None


async def docker_exec_realtmate(container_id):
    try:
        exec_cmd = await asyncio.create_subprocess_exec(
            "docker", "exec", container_id, "tmate", "-F",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        logger.info(f"tmate -F started for {container_id}, pid={exec_cmd.pid}")
        return exec_cmd
    except Exception as e:
        logger.error(f"real tmate exec failed: {e}")
        return None


async def docker_exec_tmate(container_id):
    try:
        exec_cmd = await asyncio.create_subprocess_exec(
            "docker", "exec", container_id, "sshx",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        return exec_cmd
    except Exception as e:
        logger.error(f"sshx exec failed: {e}")
        return None

# ---------------------------------------------------------------------------
# Async LXC/LXD helpers — mirror the Docker helpers above so the rest of the
# bot can treat both backends the same way (see the dispatch functions below).
# ---------------------------------------------------------------------------

async def async_lxc_apply_limits(container_name, ram, cpu, disk=None):
    """Best-effort CPU/RAM/disk limits via `lxc config`. Logs (doesn't raise)
    on failure, since disk quotas depend on the storage backend (zfs/btrfs
    support them, dir/lvm without thin-pool quotas may not)."""
    mem_gb = parse_gb(ram)
    mem_value = f"{mem_gb}GB" if mem_gb else "1GB"
    limit_cmds = [
        ["lxc", "config", "set", container_name, "limits.memory", mem_value],
        ["lxc", "config", "set", container_name, "limits.cpu", str(cpu)],
        ["lxc", "config", "set", container_name, "limits.memory.swap", "false"],
    ]
    for cmd in limit_cmds:
        try:
            proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=20.0)
            if proc.returncode != 0:
                logger.warning(f"LXC limit command failed ({' '.join(cmd)}): {stderr.decode()}")
        except Exception as e:
            logger.warning(f"LXC limit command error ({' '.join(cmd)}): {e}")
    if disk:
        try:
            disk_gb = parse_gb(disk)
            if disk_gb:
                cmd = ["lxc", "config", "device", "override", container_name, "root", f"size={int(disk_gb)}GB"]
                proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
                _, stderr = await asyncio.wait_for(proc.communicate(), timeout=20.0)
                if proc.returncode != 0:
                    logger.warning(f"LXC disk limit not applied for {container_name} (storage pool may not support quotas): {stderr.decode()}")
        except Exception as e:
            logger.warning(f"LXC disk limit error for {container_name}: {e}")


async def async_lxc_launch(image, hostname, ram, cpu, disk, container_name):
    cmd = ["lxc", "launch", image, container_name]

    async def _cleanup():
        # remove a half-created container so a retry doesn't hit "already exists"
        try:
            p = await asyncio.create_subprocess_exec(
                "lxc", "delete", "--force", container_name,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await asyncio.wait_for(p.communicate(), timeout=60.0)
        except Exception:
            pass

    try:
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300.0)
        if proc.returncode != 0:
            logger.error(f"LXC launch failed ({' '.join(cmd)}): {stderr.decode(errors='ignore')} {stdout.decode(errors='ignore')}")
            await _cleanup()
            return None
    except asyncio.TimeoutError:
        logger.error(f"LXC launch timed out ({' '.join(cmd)}) - first launch downloads the image, try again")
        try:
            proc.kill()
        except Exception:
            pass
        await _cleanup()
        return None
    except Exception as e:
        logger.error(f"LXC launch error: {e}")
        return None
    await async_lxc_apply_limits(container_name, ram, cpu, disk)
    return container_name


async def _lxc_simple_action(container_id, action, timeout=30.0):
    try:
        proc = await asyncio.create_subprocess_exec(
            "lxc", action, container_id,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        if proc.returncode != 0:
            logger.error(f"LXC {action} failed for {container_id}: {stderr.decode()}")
        return proc.returncode == 0
    except asyncio.TimeoutError:
        logger.warning(f"LXC {action} timeout for {container_id}")
        return False
    except Exception as e:
        logger.error(f"LXC {action} error for {container_id}: {e}")
        return False


async def async_lxc_start(container_id):
    return await _lxc_simple_action(container_id, "start")


async def async_lxc_stop(container_id):
    return await _lxc_simple_action(container_id, "stop")


async def async_lxc_restart(container_id):
    return await _lxc_simple_action(container_id, "restart")


async def async_lxc_rm(container_id):
    try:
        proc = await asyncio.create_subprocess_exec(
            "lxc", "delete", "--force", container_id,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
        )
        await proc.communicate()
        return proc.returncode == 0
    except Exception as e:
        logger.error(f"LXC delete error for {container_id}: {e}")
        return False


async def async_lxc_upgrade(container_id, ram, cpu):
    try:
        await async_lxc_apply_limits(container_id, ram, cpu)
        return True
    except Exception as e:
        logger.error(f"async_lxc_upgrade failed for {container_id}: {e}")
        return False


async def async_install_tmate_lxc(container_id):
    install_cmd = "curl -sSf https://sshx.io/get | sh"
    try:
        proc = await asyncio.create_subprocess_exec(
            "lxc", "exec", container_id, "--", "bash", "-c", install_cmd,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120.0)
        if proc.returncode != 0:
            logger.warning(f"sshx install warning (lxc) for {container_id}: {stderr.decode()}")
        else:
            logger.info(f"sshx installed in lxc container {container_id}")
    except asyncio.TimeoutError:
        logger.error(f"sshx install timeout (lxc) for {container_id}")
    except Exception as e:
        logger.error(f"Failed to install sshx in lxc container {container_id}: {e}")


async def async_install_real_tmate_lxc(container_id):
    install_cmd = "apt-get update && apt-get install -y tmate"
    try:
        proc = await asyncio.create_subprocess_exec(
            "lxc", "exec", container_id, "--", "bash", "-c", install_cmd,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120.0)
        if proc.returncode != 0:
            logger.warning(f"tmate install warning (lxc) for {container_id}: {stderr.decode()}")
        else:
            logger.info(f"real tmate installed in lxc container {container_id}")
    except asyncio.TimeoutError:
        logger.error(f"tmate install timeout (lxc) for {container_id}")
    except Exception as e:
        logger.error(f"Failed to install real tmate in lxc container {container_id}: {e}")


async def lxc_exec_realtmate(container_id):
    try:
        exec_cmd = await asyncio.create_subprocess_exec(
            "lxc", "exec", container_id, "--", "tmate", "-F",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        logger.info(f"tmate -F started for lxc container {container_id}")
        return exec_cmd
    except Exception as e:
        logger.error(f"real tmate exec failed (lxc): {e}")
        return None


async def lxc_exec_tmate(container_id):
    try:
        exec_cmd = await asyncio.create_subprocess_exec(
            "lxc", "exec", container_id, "--", "sshx",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        return exec_cmd
    except Exception as e:
        logger.error(f"sshx exec failed (lxc): {e}")
        return None


QEMU_STEPS = ["Installing QEMU", "Preparing base image", "Building VM disk", "Writing cloud-init", "Starting VM"]
QEMU_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "qemu_vms")
QEMU_CACHE_DIR = os.path.join(QEMU_DIR, "_cache")
QEMU_IMAGES = {
    "ubuntu": ("https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-amd64.img", "ubuntu-jammy.img"),
    "debian": ("https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-generic-amd64.qcow2", "debian-bookworm.qcow2"),
}


def _qemu_progress_embed(state):
    icons = {"pending": "⬜", "running": "⏳", "done": "✅", "failed": "❌"}
    lines = "\n".join(f"{icons[st]} {title}" for title, st in state["steps"])
    embed = discord.Embed(title="⏳ Converting to QEMU", color=discord.Color.orange())
    embed.add_field(name="Progress", value=f"`{_progress_bar(state['pct'])}`", inline=False)
    embed.add_field(name="Steps", value=lines, inline=False)
    embed.set_footer(text=f"Now: {state['current']}")
    return embed


def _qemu_screen_name(container_id):
    return f"qemu-{container_id}"


async def _qrun(cmd, timeout=120):
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    return proc.returncode, out.decode(errors="ignore")


async def ensure_qemu_host_deps():
    missing = [b for b in ("qemu-system-x86_64", "qemu-img", "cloud-localds", "screen") if not shutil.which(b)]
    if missing:
        rc, out = await _qrun(["bash", "-c",
            "export DEBIAN_FRONTEND=noninteractive; apt-get update -y && "
            "(apt-get install -y qemu-system-x86_64 qemu-utils cloud-image-utils screen genisoimage || "
            "apt-get install -y qemu-system-x86-64 qemu-utils cloud-image-utils screen genisoimage)"], timeout=600)
        if rc != 0:
            raise RuntimeError(f"Failed installing QEMU dependencies: {out[-600:]}")
    if not shutil.which("sshx"):
        rc, out = await _qrun(["bash", "-c", "curl -sSf https://sshx.io/get | sh"], timeout=180)
        if rc != 0:
            raise RuntimeError(f"Failed installing sshx on host: {out[-600:]}")


async def qemu_get_base_image(os_type):
    url, fname = QEMU_IMAGES.get(os_type, QEMU_IMAGES["ubuntu"])
    os.makedirs(QEMU_CACHE_DIR, exist_ok=True)
    path = os.path.join(QEMU_CACHE_DIR, fname)
    if os.path.exists(path) and os.path.getsize(path) > 50 * 1024 * 1024:
        return path
    tmp = path + ".part"
    rc, out = await _qrun(["wget", "-q", "-O", tmp, url], timeout=1800)
    if rc != 0 or not os.path.exists(tmp):
        raise RuntimeError(f"Failed downloading {os_type} cloud image: {out[-600:]}")
    os.replace(tmp, path)
    return path


async def async_qemu_start(container_id, ram="2g", cpu="1"):
    name = _qemu_screen_name(container_id)
    rc, _ = await _qrun(["bash", "-c", f"screen -list | grep -q '\\.{name}[[:space:]]'"], timeout=10)
    if rc == 0:
        return True  # already running
    vm_dir = os.path.join(QEMU_DIR, container_id)
    disk_path = os.path.join(vm_dir, "disk.qcow2")
    seed_path = os.path.join(vm_dir, "seed.iso")
    if not (os.path.exists(disk_path) and os.path.exists(seed_path)):
        logger.error(f"qemu start: missing disk/seed for {container_id}")
        return False
    mem_mb = max(256, int(parse_gb(ram) * 1024))
    vcpus = max(1, int(float(cpu)))
    kvm_flag = ["-enable-kvm"] if os.path.exists("/dev/kvm") and os.access("/dev/kvm", os.R_OK | os.W_OK) else []
    qemu_cmd = [
        "qemu-system-x86_64", *kvm_flag,
        "-m", str(mem_mb), "-smp", str(vcpus), "-cpu", "qemu64",
        "-drive", f"file={disk_path},format=qcow2,if=virtio",
        "-drive", f"file={seed_path},format=raw,if=virtio",
        "-boot", "order=c",
        "-netdev", "user,id=n0", "-device", "virtio-net-pci,netdev=n0",
        "-nographic", "-serial", "mon:stdio",
    ]
    rc, out = await _qrun(["screen", "-dmS", name, *qemu_cmd], timeout=30)
    return rc == 0


async def async_qemu_stop(container_id):
    name = _qemu_screen_name(container_id)
    await _qrun(["bash", "-c", f"screen -S {name} -X quit"], timeout=10)
    await _qrun(["pkill", "-f", f"qemu-system-x86_64.*{container_id}"], timeout=10)
    return True


async def async_qemu_rm(container_id):
    await async_qemu_stop(container_id)
    try:
        shutil.rmtree(os.path.join(QEMU_DIR, container_id), ignore_errors=True)
    except Exception:
        pass
    return True


async def qemu_exec_sshx(container_id):
    name = _qemu_screen_name(container_id)
    try:
        return await asyncio.create_subprocess_exec(
            "sshx", "--shell", f"screen -x {name}",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except Exception as e:
        logger.error(f"qemu sshx exec failed: {e}")
        return None


async def convert_vps_to_qemu(vps, progress_cb=None):
    state = {"steps": [[t, "pending"] for t in QEMU_STEPS], "pct": 0, "current": QEMU_STEPS[0]}

    async def tick(i, status, pct):
        state["steps"][i][1] = status
        state["current"] = QEMU_STEPS[i]
        state["pct"] = pct
        if progress_cb:
            await progress_cb(state)

    container_id = vps["container_id"]
    try:
        await tick(0, "running", 5)
        await ensure_qemu_host_deps()
        await tick(0, "done", 15)

        await tick(1, "running", 20)
        base_image = await qemu_get_base_image(vps["os_type"])
        await tick(1, "done", 35)

        await tick(2, "running", 40)
        vm_dir = os.path.join(QEMU_DIR, container_id)
        os.makedirs(vm_dir, exist_ok=True)
        disk_path = os.path.join(vm_dir, "disk.qcow2")
        seed_path = os.path.join(vm_dir, "seed.iso")
        rc, out = await _qrun(["qemu-img", "create", "-f", "qcow2", "-F", "qcow2", "-b", base_image, disk_path], timeout=60)
        if rc != 0:
            raise RuntimeError(f"qemu-img create failed: {out[-500:]}")
        disk_size = f"{max(1, int(parse_gb(vps['disk'])))}G"
        rc, out = await _qrun(["qemu-img", "resize", disk_path, disk_size], timeout=60)
        if rc != 0:
            raise RuntimeError(f"qemu-img resize failed: {out[-500:]}")
        await tick(2, "done", 55)

        await tick(3, "running", 60)
        password = secrets.token_urlsafe(9)
        user_data = (
            "#cloud-config\n"
            f"hostname: {vps['hostname']}\n"
            "ssh_pwauth: true\n"
            "disable_root: false\n"
            "chpasswd:\n"
            "  list: |\n"
            f"    root:{password}\n"
            "  expire: false\n"
        )
        meta_data = f"instance-id: iid-{container_id}\nlocal-hostname: {vps['hostname']}\n"
        with open(os.path.join(vm_dir, "user-data"), "w") as f:
            f.write(user_data)
        with open(os.path.join(vm_dir, "meta-data"), "w") as f:
            f.write(meta_data)
        rc, out = await _qrun(["cloud-localds", seed_path, os.path.join(vm_dir, "user-data"), os.path.join(vm_dir, "meta-data")], timeout=60)
        if rc != 0:
            raise RuntimeError(f"cloud-localds failed: {out[-500:]}")
        await tick(3, "done", 75)

        await tick(4, "running", 85)
        old_backend = vps["backend"]
        update_vps_qemu(container_id, 1, password)
        try:
            await backend_rm(container_id, old_backend)
        except Exception as e:
            logger.warning(f"qemu convert: old container cleanup failed: {e!r}")
        if not await async_qemu_start(container_id, vps["ram"], vps["cpu"]):
            raise RuntimeError("Failed to start the QEMU VM.")
        update_vps_status(container_id, "running")
        update_vps_ssh(container_id, "")
        await tick(4, "done", 100)
        return True, password
    except Exception as e:
        logger.error(f"QEMU conversion failed for {container_id}: {e!r}")
        return False, str(e)


# ---------------------------------------------------------------------------
# Backend dispatch — VPS-management abstraction (VPSBackend equivalent).
# Every VPS record has a 'backend' column ('docker' or 'lxc') set at creation
# time; all lifecycle actions look at THAT value, not the current !lxc on/off
# setting, so existing VPS are never silently migrated between backends.
# ---------------------------------------------------------------------------

async def create_backend_container(backend, os_type, hostname, ram, cpu, disk, container_name):
    if backend == 'lxc':
        image = LXC_UBUNTU_IMAGE if os_type == "ubuntu" else LXC_DEBIAN_IMAGE
        return await async_lxc_launch(image, hostname, ram, cpu, disk, container_name)
    image = "rdx-ubuntu:22.04" if os_type == "ubuntu" else "rdx-debian:bookworm"
    return await async_docker_run(image, hostname, ram, cpu, disk, container_name)


async def backend_start(container_id, backend):
    if backend == 'lxc':
        return await async_lxc_start(container_id)
    if backend == 'qemu':
        return await async_qemu_start(container_id)
    return await async_docker_start(container_id)


async def backend_stop(container_id, backend):
    if backend == 'lxc':
        return await async_lxc_stop(container_id)
    if backend == 'qemu':
        return await async_qemu_stop(container_id)
    return await async_docker_stop(container_id)


async def backend_restart(container_id, backend):
    if backend == 'lxc':
        return await async_lxc_restart(container_id)
    if backend == 'qemu':
        await async_qemu_stop(container_id)
        return await async_qemu_start(container_id)
    return await async_docker_restart(container_id)


async def backend_rm(container_id, backend):
    if backend == 'lxc':
        return await async_lxc_rm(container_id)
    if backend == 'qemu':
        return await async_qemu_rm(container_id)
    return await async_docker_rm(container_id)


async def backend_upgrade(container_id, backend, ram, cpu):
    if backend == 'lxc':
        return await async_lxc_upgrade(container_id, ram, cpu)
    return await async_docker_upgrade(container_id, ram, cpu)


async def backend_install_tmate(container_id, backend, os_type):
    if backend == 'lxc':
        return await async_install_tmate_lxc(container_id)
    if backend == 'qemu':
        return True  # sshx is ensured on the host during conversion
    return await async_install_tmate(container_id, os_type)


async def backend_exec_tmate(container_id, backend):
    if backend == 'lxc':
        return await lxc_exec_tmate(container_id)
    if backend == 'qemu':
        return await qemu_exec_sshx(container_id)
    return await docker_exec_tmate(container_id)


async def backend_install_real_tmate(container_id, backend):
    if backend == 'lxc':
        return await async_install_real_tmate_lxc(container_id)
    return await async_install_real_tmate(container_id)


async def backend_exec_realtmate(container_id, backend):
    if backend == 'lxc':
        return await lxc_exec_realtmate(container_id)
    return await docker_exec_realtmate(container_id)

# ---------------------------------------------------------------------------
# Core VPS action helpers — ctx/interaction agnostic.
# Used by both the "!" prefix commands and the !manage button panel so the
# real backend logic (Docker, tmate, SQLite) only lives in one place.
# ---------------------------------------------------------------------------

def os_name_of(os_type):
    return "Ubuntu 22.04" if os_type == "ubuntu" else "Debian 12"


def _vps_backend(vps):
    """Backend a given VPS row was created with ('docker' or 'lxc'). Never the
    current !lxc on/off setting — existing VPS always keep their own backend."""
    try:
        return vps['backend'] or 'docker'
    except (KeyError, IndexError):
        return 'docker'


async def core_start(vps, force=False):
    if vps['suspended']:
        return False, "suspended"
    if not force and review_required_for_vps(vps):
        return False, "review"
    container_id = vps['container_id']
    ok = await backend_start(container_id, _vps_backend(vps))
    if ok:
        update_vps_status(container_id, "running")
    return ok, None


async def core_stop(vps):
    container_id = vps['container_id']
    ok = await backend_stop(container_id, _vps_backend(vps))
    if ok:
        update_vps_status(container_id, "stopped")
    return ok, None


async def core_restart(vps):
    if review_required_for_vps(vps):
        return False, "review"
    container_id = vps['container_id']
    ok = await backend_restart(container_id, _vps_backend(vps))
    if ok:
        update_vps_status(container_id, "running")
    return ok, None


async def core_regen_ssh(vps):
    """Generate a fresh tmate SSH session for a running VPS. Returns the ssh line or None."""
    if not vps or vps['status'] != "running":
        return None
    exec_process = await backend_exec_tmate(vps['container_id'], _vps_backend(vps))
    if not exec_process:
        return None
    ssh_line = await capture_ssh_session_line(exec_process)
    if ssh_line:
        update_vps_ssh(vps['container_id'], ssh_line)
    return ssh_line


async def core_reinstall(vps, os_type):
    """Stop/remove the old container and provision a fresh one with the same
    resources, on the SAME backend the VPS was already using."""
    container_id = vps['container_id']
    user_id = vps['user_id']
    hostname = vps['hostname']
    ram, cpu, disk = vps['ram'], vps['cpu'], vps['disk']
    backend = _vps_backend(vps)
    old_refill_state = dict(vps) if vps['refill_available_at'] else None  # keep expiry/refill timers across reinstall
    await backend_stop(container_id, backend)
    await asyncio.sleep(2)
    await backend_rm(container_id, backend)
    delete_vps(container_id)
    suffix = random.randint(1000, 9999)
    new_container_name = f"{os_type}-vps-{user_id}-{suffix}"
    new_container_id = await create_backend_container(backend, os_type, hostname, ram, cpu, disk, new_container_name)
    if not new_container_id:
        return None
    await backend_install_tmate(new_container_id, backend, os_type)
    await asyncio.sleep(10)
    exec_process = await backend_exec_tmate(new_container_id, backend)
    ssh_line = await capture_ssh_session_line(exec_process) if exec_process else None
    if ssh_line:
        add_vps(user_id, new_container_id, new_container_name, os_type, hostname, ssh_line, ram, cpu, disk, backend=backend)
        if old_refill_state:
            restore_refill_state(new_container_id, old_refill_state)
        return get_vps_by_container_id(new_container_id)
    await backend_rm(new_container_id, backend)
    return None


async def core_create(user_id, username, os_type, ram, cpu, disk, executor_is_admin, expiry_days=30, paid=False, refill_managed=False, review_gate=False):
    """
    Full VPS creation flow: ban check -> limits (bypassed for admins) -> backend
    availability check -> host resource validation (always enforced) ->
    create container on the CURRENTLY SELECTED backend (!lxc on/off) -> tmate
    -> DB insert (with that backend recorded on the row).
    Returns {"ok": bool, "reason": str|None, "vps": row|None, "ssh_line": str|None}
    """
    add_user(user_id, username)
    if is_banned(user_id):
        return {"ok": False, "reason": "banned"}
    # Count limits are based on the person EXECUTING the creation, not the target user.
    # Admins bypass SERVER_LIMIT and TOTAL_SERVER_LIMIT entirely.
    if not executor_is_admin:
        if count_user_vps(user_id) >= SERVER_LIMIT:
            return {"ok": False, "reason": "limit"}
        if get_total_instances() >= TOTAL_SERVER_LIMIT:
            return {"ok": False, "reason": "total_limit"}

    backend = get_container_backend()

    # Never silently fall back to the other backend: fail loudly if the
    # currently-selected one isn't actually available on this host.
    if backend == 'lxc':
        if not is_lxc_available():
            return {"ok": False, "reason": "lxc_unavailable"}
    else:
        if not is_docker_available():
            return {"ok": False, "reason": "docker_unavailable"}

    # Host CPU/RAM validation always applies, admin or not.
    try:
        req_cpu = float(cpu)
        req_ram = parse_gb(ram)
        if backend == 'lxc':
            host_cpus = os.cpu_count() or 1
            host_mem_gb = get_host_mem_gb()
        else:
            host_info = client.info()
            host_cpus = host_info['NCPU']
            host_mem_gb = host_info['MemTotal'] / (1024 ** 3)
        # CPU host-limit check removed on request (any CPU count is allowed)
        # RAM host-limit check removed on request (any RAM size is allowed)
    except Exception as e:
        logger.error(f"Resource validation failed: {e}")
        return {"ok": False, "reason": "validation_error"}

    hostname = "rdx-paid" if paid else f"{VPS_HOSTNAME}-{user_id}"
    suffix = random.randint(1000, 9999)
    container_name = f"{os_type}-vps-{user_id}-{suffix}"
    container_id = await create_backend_container(backend, os_type, hostname, ram, cpu, disk, container_name)
    if not container_id:
        return {"ok": False, "reason": "docker_fail" if backend == "docker" else "lxc_fail"}
    await asyncio.sleep(5)
    await backend_install_tmate(container_id, backend, os_type)
    await asyncio.sleep(10)
    exec_process = await backend_exec_tmate(container_id, backend)
    ssh_line = await capture_ssh_session_line(exec_process) if exec_process else None
    if not ssh_line:
        await backend_stop(container_id, backend)
        await asyncio.sleep(2)
        await backend_rm(container_id, backend)
        return {"ok": False, "reason": "ssh_fail"}
    add_vps(user_id, container_id, container_name, os_type, hostname, ssh_line, ram, cpu, disk, expiry_days, backend=backend, refill_managed=refill_managed)
    if review_gate and review_enabled() and user_id != ADMIN_ID and not is_review_verified(user_id):
        # First-time user: switch the VPS off again and never hand out the sshx link yet.
        try:
            await backend_stop(container_id, backend)
        except Exception as e:
            logger.error(f"review hold: stop failed for {container_id}: {e!r}")
        update_vps_status(container_id, "stopped")
        update_vps_ssh(container_id, "")
        set_review_hold(container_id, 1)
        return {"ok": True, "reason": None, "vps": get_vps_by_container_id(container_id), "ssh_line": None, "review_pending": True}
    return {"ok": True, "reason": None, "vps": get_vps_by_container_id(container_id), "ssh_line": ssh_line}


CREATE_FAILURE_MESSAGES = {
    "banned": "You are banned from creating VPS instances.",
    "limit": f"You have reached the limit of {SERVER_LIMIT} VPS instances.",
    "total_limit": f"Global server limit reached: {TOTAL_SERVER_LIMIT} total running instances.",
    "cpu": "Requested CPU exceeds the host's available limit.",
    "ram": "Requested RAM exceeds the host's available limit.",
    "validation_error": "Resource validation failed. Please contact an admin.",
    "docker_fail": "Failed to create Docker container.",
    "lxc_fail": "Failed to create LXC/LXD container.",
    "lxc_unavailable": "LXC mode is currently selected, but LXD is not available on this host. Ask an admin to install/configure LXD or run `!lxc off` to use Docker.",
    "docker_unavailable": "Docker mode is currently selected, but Docker is not available on this host. Please contact an admin.",
    "ssh_fail": "Creation failed: Unable to generate SSH session.",
}


async def run_create_flow(ctx, target_user, os_type, ram, cpu, disk, executor_is_admin, expiry_days=30, paid=False, refill_managed=False, review_gate=False):
    """Shared VPS-creation flow used by !deploy and !admin-create."""
    username = str(target_user)
    async with ctx.typing():
        result = await core_create(target_user.id, username, os_type, ram, cpu, disk, executor_is_admin, expiry_days, paid=paid, refill_managed=refill_managed, review_gate=review_gate)
    if result["ok"] and result.get("review_pending"):
        vps = result["vps"]
        pend = discord.Embed(
            title="VPS Created - Review Required",
            description=f"Your VPS is created but it is **OFF** for now.\nPost this message in {review_channels_text(ctx.guild)} to start it:\n```{REVIEW_PHRASE}```\nAfter that the VPS starts automatically and the SSHX details are sent to your DMs.",
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc)
        )
        pend.set_footer(text=f"QuantaForge • v{BOT_VERSION} • RDX")
        try:
            await target_user.send(embed=pend)
        except discord.Forbidden:
            logger.warning(f"Cannot DM user {target_user.id} for review notice")
        await ctx.send(embed=discord.Embed(
            description=f"VPS for {target_user.mention} is created but OFF. Post `{REVIEW_PHRASE}` in {review_channels_text(ctx.guild)} to start it.",
            color=discord.Color.orange()
        ))
        return True
    if result["ok"]:
        vps = result["vps"]
        embed = discord.Embed(
            title="VPS Instance Created",
            description=f"OS: {os_name_of(os_type)}\nRAM: {ram} | CPU: {cpu} | Disk: {disk}\nExpires in: {expiry_days} days\n```{result['ssh_line']}```",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_footer(text=f"QuantaForge • v{BOT_VERSION} • RDX")
        try:
            await target_user.send(embed=embed)
            dm_note = "Check your DMs for access details." if target_user.id == ctx.author.id else f"Access details were sent to {target_user}'s DMs."
        except discord.Forbidden:
            logger.warning(f"Cannot DM user {target_user.id} for creation")
            dm_note = "Could not DM the access details (their DMs are closed)."
        await ctx.send(embed=discord.Embed(
            description=f"VPS for {target_user.mention} is ready ({vps['container_name']}). {dm_note}",
            color=discord.Color.green()
        ))
        return True
    await ctx.send(embed=discord.Embed(
        description=CREATE_FAILURE_MESSAGES.get(result["reason"], "VPS creation failed."),
        color=discord.Color.red()
    ))
    return False


# ---------------------------------------------------------------------------
# !manage panel — embed + interactive button view
# ---------------------------------------------------------------------------

def build_manage_embed(vps, node_label="Local Host"):
    status_running = vps['status'] == "running"
    status_emoji = "🟢" if status_running else "🔴"
    uptime = get_uptime(vps['container_id'], _vps_backend(vps)) if status_running else "Not running"
    stats = get_stats(vps['container_id'], _vps_backend(vps)) if status_running else {'cpu': 'N/A', 'mem': 'N/A', 'net': 'N/A'}
    status_text = "🚫 Suspended" if vps['suspended'] else f"{status_emoji} {vps['status'].title()}"

    embed = discord.Embed(
        title=f"⭐ VPS Management - {vps['container_name']}",
        color=discord.Color.from_rgb(88, 101, 242),
        timestamp=datetime.now(timezone.utc)
    )
    embed.add_field(
        name="🖥️ VPS Information",
        value=(
            f"**Managing Container:** `{vps['container_name']}`\n"
            f"**Node:** {node_label}\n"
            f"**OS:** {os_name_of(vps['os_type'])}\n"
            f"**Status:** {status_text}\n"
            f"**Uptime:** {uptime}"
        ),
        inline=False
    )
    embed.add_field(
        name="⚙️ Allocated Resources",
        value=f"**RAM:** {vps['ram']}\n**CPU:** {vps['cpu']}\n**Storage:** {vps['disk']}",
        inline=False
    )
    embed.add_field(
        name="⏰ Expiration",
        value=(f"**Status:** {'Expired' if vps['expires_at'] and (datetime.fromisoformat(vps['expires_at']) - datetime.now(timezone.utc)).days < 0 else ('Active' if vps['expires_at'] else 'Not configured')}\n"
               f"**Expiration Date:** {datetime.fromisoformat(vps['expires_at']).strftime('%Y-%m-%d') if vps['expires_at'] else 'N/A'}\n"
               f"**Days Remaining:** {max((datetime.fromisoformat(vps['expires_at']) - datetime.now(timezone.utc)).days, 0) if vps['expires_at'] else 'N/A'}"),
        inline=False
    )
    embed.add_field(
        name="📈 Live Usage",
        value=f"**CPU Usage:** {stats['cpu']}\n**Memory Usage:** {stats['mem']}\n**Network I/O:** {stats['net']}",
        inline=False
    )
    embed.add_field(
        name="🎮 Controls",
        value="Use the buttons below to manage your VPS",
        inline=False
    )
    embed.set_footer(text=f"QuantaForge • v{BOT_VERSION} • RDX")
    return embed


class VpsSelectView(discord.ui.View):
    # Dropdown shown by !manage when a user owns more than one VPS.
    def __init__(self, owner_id, vps_list):
        super().__init__(timeout=180)
        self.owner_id = owner_id
        options = []
        for v in vps_list[:25]:
            icon = "🟢" if v['status'] == "running" else "🔴"
            options.append(discord.SelectOption(
                label=str(v['container_name'])[:100],
                value=str(v['container_id'])[:100],
                description=f"{os_name_of(v['os_type'])} | {v['ram']} RAM | {v['cpu']} CPU | {v['status']}"[:100],
                emoji=icon))
        select = discord.ui.Select(placeholder="Select a VPS to manage", options=options, min_values=1, max_values=1)
        select.callback = self.on_select
        self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id and not is_admin(interaction.user):
            await interaction.response.send_message("This menu isn't yours.", ephemeral=True)
            return False
        return True

    async def on_select(self, interaction: discord.Interaction):
        chosen = interaction.data['values'][0]
        vps = get_vps_by_container_id(chosen)
        if not vps or (vps['user_id'] != self.owner_id and not is_admin(interaction.user)):
            await interaction.response.send_message("VPS not found.", ephemeral=True)
            return
        if review_required_for_vps(vps):
            await interaction.response.send_message(embed=review_required_embed(interaction.guild), ephemeral=True)
            return
        await interaction.response.edit_message(
            content=None, embed=build_manage_embed(vps),
            view=ManagePanelView(owner_id=self.owner_id, container_id=vps['container_id']))


class ManagePanelView(discord.ui.View):
    def __init__(self, owner_id, container_id):
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.container_id = container_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id and not is_admin(interaction.user):
            await interaction.response.send_message("This VPS panel isn't yours to control.", ephemeral=True)
            return False
        return True

    async def _refresh(self, interaction: discord.Interaction):
        vps = get_vps_by_container_id(self.container_id)
        if vps:
            try:
                await interaction.message.edit(embed=build_manage_embed(vps), view=self)
            except Exception as e:
                logger.error(f"Panel refresh failed: {e}")

    @discord.ui.button(label="Reinstall", emoji="🔄", style=discord.ButtonStyle.secondary)
    async def reinstall_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        vps = get_vps_by_container_id(self.container_id)
        if not vps:
            await interaction.followup.send("VPS not found.", ephemeral=True)
            return
        if review_required_for_vps(vps):
            await interaction.followup.send(embed=review_required_embed(interaction.guild), ephemeral=True)
            return
        new_vps = await core_reinstall(vps, vps['os_type'])
        if new_vps:
            self.container_id = new_vps['container_id']
            embed = discord.Embed(title="VPS Reinstalled", description=f"```{new_vps['ssh_command']}```", color=discord.Color.green())
            try:
                user = await bot.fetch_user(new_vps['user_id'])
                await user.send(embed=embed)
            except discord.Forbidden:
                pass
            await interaction.followup.send("VPS reinstalled. Check your DMs for the new SSH session.", ephemeral=True)
            await self._refresh(interaction)
        else:
            await interaction.followup.send("Reinstall failed.", ephemeral=True)

    @discord.ui.button(label="Convert to QEMU", emoji="🖥️", style=discord.ButtonStyle.secondary, row=1)
    async def qemu_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        vps = get_vps_by_container_id(self.container_id)
        if not vps:
            await interaction.response.send_message("VPS not found.", ephemeral=True)
            return
        if vps['backend'] == 'qemu':
            await interaction.response.send_message("This VPS is already running as a QEMU VM.", ephemeral=True)
            return
        if not (is_admin(interaction.user) or qemu_mode_enabled()):
            await interaction.response.send_message("QEMU conversion is admin testing only right now.", ephemeral=True)
            return
        warn = discord.Embed(
            title="⚠️ Convert to a real QEMU VM?",
            description=(
                "This replaces your container VPS with a full virtual machine (QEMU/KVM).\n\n"
                "- Your VPS may become **slower**, especially without hardware virtualization (KVM).\n"
                "- It can take several minutes (downloading the OS image, building the disk).\n"
                "- Your current container and its data will be stopped and **cannot be converted back**.\n"
                "- You get a fresh OS with a random **root** password.\n\n"
                "Click **Confirm** to continue, or **Cancel** to stay on the container."
            ),
            color=discord.Color.orange()
        )
        await interaction.response.send_message(embed=warn, view=QemuConfirmView(interaction.user.id, self.container_id), ephemeral=True)


    @discord.ui.button(label="Start", emoji="▶️", style=discord.ButtonStyle.success)
    async def start_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        vps = get_vps_by_container_id(self.container_id)
        if not vps:
            await interaction.followup.send("VPS not found.", ephemeral=True)
            return
        ok, reason = await core_start(vps)
        if reason == "suspended":
            await interaction.followup.send("This VPS is suspended by an admin. Contact support.", ephemeral=True)
        elif reason == "review":
            await interaction.followup.send(embed=review_required_embed(interaction.guild), ephemeral=True)
        elif ok:
            await interaction.followup.send("VPS started.", ephemeral=True)
            await self._refresh(interaction)
        else:
            await interaction.followup.send("Failed to start the VPS.", ephemeral=True)

    @discord.ui.button(label="Stop", emoji="⏸️", style=discord.ButtonStyle.danger)
    async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        vps = get_vps_by_container_id(self.container_id)
        if not vps:
            await interaction.followup.send("VPS not found.", ephemeral=True)
            return
        ok, _ = await core_stop(vps)
        if ok:
            await interaction.followup.send("VPS stopped.", ephemeral=True)
            await self._refresh(interaction)
        else:
            await interaction.followup.send("Failed to stop the VPS.", ephemeral=True)

    @discord.ui.button(label="SSHX", emoji="🔑", style=discord.ButtonStyle.primary)
    async def ssh_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        vps = get_vps_by_container_id(self.container_id)
        if not vps:
            await interaction.followup.send("VPS not found.", ephemeral=True)
            return
        if vps['status'] != "running":
            await interaction.followup.send("VPS must be running to start an SSH session.", ephemeral=True)
            return
        exec_process = await backend_exec_tmate(vps['container_id'], _vps_backend(vps))
        if not exec_process:
            await interaction.followup.send("Failed to start SSH session.", ephemeral=True)
            return
        ssh_line = await capture_ssh_session_line(exec_process)
        if ssh_line:
            update_vps_ssh(vps['container_id'], ssh_line)
            embed = discord.Embed(title="New SSH Session", description=f"```{ssh_line}```", color=discord.Color.green())
            try:
                user = await bot.fetch_user(vps['user_id'])
                await user.send(embed=embed)
                await interaction.followup.send("New SSH session sent to your DMs.", ephemeral=True)
            except discord.Forbidden:
                await interaction.followup.send(f"New SSH session generated:\n```{ssh_line}```", ephemeral=True)
        else:
            await interaction.followup.send("Failed to start SSH session. Try again shortly.", ephemeral=True)

    @discord.ui.button(label="Tmate", emoji="🖥️", style=discord.ButtonStyle.primary)
    async def tmate_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        vps = get_vps_by_container_id(self.container_id)
        if not vps:
            await interaction.followup.send("VPS not found.", ephemeral=True)
            return
        if vps['status'] != "running":
            await interaction.followup.send("VPS must be running to start a Tmate session.", ephemeral=True)
            return
        await backend_install_real_tmate(vps['container_id'], _vps_backend(vps))
        exec_process = await backend_exec_realtmate(vps['container_id'], _vps_backend(vps))
        if not exec_process:
            await interaction.followup.send("Failed to start Tmate session.", ephemeral=True)
            return
        ssh_line = await capture_real_tmate_line(exec_process)
        if ssh_line:
            embed = discord.Embed(title="New Tmate Session", description=f"```{ssh_line}```", color=discord.Color.green())
            try:
                user = await bot.fetch_user(vps['user_id'])
                await user.send(embed=embed)
                await interaction.followup.send("New Tmate session sent to your DMs.", ephemeral=True)
            except discord.Forbidden:
                await interaction.followup.send(f"New Tmate session generated:\n```{ssh_line}```", ephemeral=True)
        else:
            await interaction.followup.send("Failed to start Tmate session. Try again shortly.", ephemeral=True)

    

    @discord.ui.button(label="Stats", emoji="📊", style=discord.ButtonStyle.secondary)
    async def stats_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        vps = get_vps_by_container_id(self.container_id)
        if not vps:
            await interaction.followup.send("VPS not found.", ephemeral=True)
            return
        uptime = get_uptime(vps['container_id'], _vps_backend(vps))
        stats = get_stats(vps['container_id'], _vps_backend(vps))
        embed = discord.Embed(title=f"📊 Stats — {vps['container_name']}", color=discord.Color.blue(), timestamp=datetime.now(timezone.utc))
        embed.add_field(name="CPU Usage", value=stats['cpu'], inline=True)
        embed.add_field(name="Memory Usage", value=stats['mem'], inline=True)
        embed.add_field(name="Network I/O", value=stats['net'], inline=True)
        embed.add_field(name="Uptime", value=uptime, inline=True)
        embed.add_field(name="Allocated", value=f"{vps['ram']} RAM | {vps['cpu']} CPU | {vps['disk']} Disk", inline=False)
        embed.set_footer(text=f"QuantaForge • v{BOT_VERSION} • RDX")
        await interaction.followup.send(embed=embed, ephemeral=True)


class QemuConfirmView(discord.ui.View):
    def __init__(self, owner_id, container_id):
        super().__init__(timeout=120)
        self.owner_id = owner_id
        self.container_id = container_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id and not is_admin(interaction.user):
            await interaction.response.send_message("This confirmation isn't yours.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
        vps = get_vps_by_container_id(self.container_id)
        if not vps or vps['backend'] == 'qemu':
            await interaction.followup.send("VPS not found or already converted.", ephemeral=True)
            return
        state0 = {"steps": [[t, "pending"] for t in QEMU_STEPS], "pct": 0, "current": QEMU_STEPS[0]}
        msg = await interaction.followup.send(embed=_qemu_progress_embed(state0), wait=True)

        async def _update(state):
            try:
                await msg.edit(embed=_qemu_progress_embed(state))
            except Exception:
                pass

        ok, result = await convert_vps_to_qemu(vps, _update)
        if not ok:
            await msg.edit(embed=discord.Embed(title="❌ QEMU conversion failed", description=f"```{str(result)[-800:]}```", color=discord.Color.red()))
            return
        password = result
        final = discord.Embed(title="✅ Converted to QEMU", description="Your VPS is now a real virtual machine.", color=discord.Color.green())
        final.add_field(name="Username", value="root", inline=True)
        final.add_field(name="Password", value=f"||{password}||", inline=True)
        final.add_field(name="How to connect", value="Click **SSHX** in `!manage` — it opens the VM's console. Log in with the credentials above.", inline=False)
        await msg.edit(embed=final)
        try:
            user = await bot.fetch_user(vps['user_id'])
            await user.send(embed=final)
        except discord.Forbidden:
            pass

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="Cancelled.", embed=None, view=self)


# ---------------------------------------------------------------------------
# User "!" commands
# ---------------------------------------------------------------------------

@bot.command(name="help")
async def help_cmd(ctx):
    embed = discord.Embed(title="📖 QuantaForge VPS — Help", color=discord.Color.blue(), timestamp=datetime.now(timezone.utc))
    embed.set_author(name=bot.user.name, icon_url=bot.user.avatar.url if bot.user.avatar else None)
    embed.add_field(
        name="👤 User Commands",
        value=(
            "`!help` — Show this help menu\n"
            "`!manage [vps]` — Manage your VPS\n"
            "`!ssh [vps]` — Get VPS SSH information\n"
            "`!stats [vps]` — View VPS statistics\n"
            "`!list` — List all your VPS instances\n"
            "`!start <vps>` — Start a VPS\n"
            "`!stop <vps>` — Stop a VPS\n"
            "`!restart <vps>` — Restart a VPS\n"
            "`!reinstall <vps> [os]` — Reinstall a VPS\n"
            "`!remove <vps>` — Remove a VPS\n"
            "`!logs <vps> [lines]` — View recent VPS logs\n"
            "`!vpsrefill [vps]` — Renew your VPS for another 7 days when your refill window opens\n"
            "`!about` — Show bot & developer information\n"
            "`!ping` — Check the bot's latency\n"
            "`!deploy <os>` — 🔒 Admin only (contact an admin to request a VPS)"
        ),
        inline=False
    )
    if ADMIN_ID > 0 and is_admin(ctx.author):
        embed.add_field(
            name="🛠️ Admin Commands (Admin Only)",
            value=(
                "`!deploy <os>` — Create a VPS for yourself\n"
                "`!admin-create <user> <os> [ram] [cpu] [disk]` — Create VPS for a user (no count limit)\n"
                "`!admin-manage <user> <vps> <action>` — start/stop/restart/delete/suspend/unsuspend\n"
                "`!admin-list` — List all VPS instances\n"
                "`!admin-list-users` — List users with VPS counts\n"
                "`!admin-stats` — View bot statistics\n"
                "`!admin-vps-info <user> <vps>` — Full details for a user's VPS\n"
                "`!admin-logs <user> <vps> [lines]` — View logs for a user's VPS\n"
                "`!admin-delete-user <user>` — Delete all VPS for a user\n"
                "`!admin-ban <user>` / `!admin-unban <user>` — Ban/unban a user\n"
                "`!admin-kill-all` — Stop all running VPS instances\n"
                "`!lxc on` / `!lxc off` — Switch the backend (LXC/Docker) used for NEW VPS only\n"
                "`!set-avatar` (with an image attached) — Update the bot's avatar"
            ),
            inline=False
        )
    embed.set_footer(text=f"QuantaForge • v{BOT_VERSION} • RDX")
    await ctx.send(embed=embed)


@bot.command(name="manage")
async def manage_cmd(ctx, vps_identifier: str = None):
    if not await deploy_channel_allowed(ctx):
        return
    if not vps_identifier:
        _all_vps = get_user_vps(ctx.author.id)
        if len(_all_vps) > 1:
            await ctx.send(embed=discord.Embed(title="Select a VPS", description=f"You have {len(_all_vps)} VPS instances. Pick the one you want to manage:", color=discord.Color.blue()),
                           view=VpsSelectView(ctx.author.id, _all_vps))
            return
    vps = get_vps_by_identifier(ctx.author.id, vps_identifier)
    if not vps:
        await ctx.send(embed=discord.Embed(description="You have no VPS instances to manage.", color=discord.Color.red()))
        return
    if review_required_for_vps(vps):
        await ctx.send(embed=review_required_embed(ctx.guild))
        return
    embed = build_manage_embed(vps)
    view = ManagePanelView(owner_id=ctx.author.id, container_id=vps['container_id'])
    await ctx.send(embed=embed, view=view)


@bot.command(name="ssh")
async def ssh_cmd(ctx, vps_identifier: str = None):
    vps = get_vps_by_identifier(ctx.author.id, vps_identifier)
    if not vps:
        await ctx.send(embed=discord.Embed(description="No VPS found.", color=discord.Color.red()))
        return
    if review_required_for_vps(vps):
        await ctx.send(embed=review_required_embed(ctx.guild))
        return
    if vps['ssh_command']:
        embed = discord.Embed(title=f"SSH Info — {vps['container_name']}", description=f"```{vps['ssh_command']}```", color=discord.Color.blue(), timestamp=datetime.now(timezone.utc))
        embed.add_field(name="Status", value=vps['status'], inline=True)
        embed.set_footer(text=f"QuantaForge • v{BOT_VERSION} • RDX")
        try:
            await ctx.author.send(embed=embed)
            await ctx.send("SSH information sent to your DMs.")
        except discord.Forbidden:
            await ctx.send(embed=embed)
    else:
        await ctx.send(embed=discord.Embed(description="No SSH session on file. Use `!manage` and hit Regen Password to generate one.", color=discord.Color.red()))


@bot.command(name="stats")
async def stats_cmd(ctx, vps_identifier: str = None):
    vps = get_vps_by_identifier(ctx.author.id, vps_identifier)
    if not vps:
        await ctx.send(embed=discord.Embed(description="No VPS found.", color=discord.Color.red()))
        return
    uptime = get_uptime(vps['container_id'], _vps_backend(vps))
    stats = get_stats(vps['container_id'], _vps_backend(vps))
    embed = discord.Embed(title=f"📊 Stats — {vps['container_name']}", color=discord.Color.blue(), timestamp=datetime.now(timezone.utc))
    embed.add_field(name="CPU Usage", value=stats['cpu'], inline=True)
    embed.add_field(name="Memory Usage", value=stats['mem'], inline=True)
    embed.add_field(name="Network I/O", value=stats['net'], inline=True)
    embed.add_field(name="Uptime", value=uptime, inline=True)
    embed.add_field(name="Allocated", value=f"{vps['ram']} RAM | {vps['cpu']} CPU | {vps['disk']} Disk", inline=False)
    embed.set_footer(text=f"QuantaForge • v{BOT_VERSION} • RDX")
    await ctx.send(embed=embed)


@bot.command(name="list")
async def list_cmd(ctx):
    vps_list = get_user_vps(ctx.author.id)
    if not vps_list:
        await ctx.send(embed=discord.Embed(description="You have no VPS instances.", color=discord.Color.red()))
        return
    embed = discord.Embed(title="Your VPS Instances", color=discord.Color.blue(), timestamp=datetime.now(timezone.utc))
    embed.set_author(name=bot.user.name, icon_url=bot.user.avatar.url if bot.user.avatar else None)
    for vps in vps_list[:25]:
        status_emoji = "🟢" if vps['status'] == "running" else "🔴"
        backend = _vps_backend(vps)
        uptime = get_uptime(vps['container_id'], backend)
        suspended_text = "(Suspended)" if vps['suspended'] else ""
        embed.add_field(
            name=f"{status_emoji} {vps['container_name']} ({vps['os_type']}) {suspended_text}",
            value=f"ID: ```{vps['container_id']}```\nHostname: {vps['hostname']}\nBackend: {backend}\nStatus: {vps['status']}\nUptime: {uptime}\nResources: {vps['ram']} RAM | {vps['cpu']} CPU | {vps['disk']} Disk",
            inline=False
        )
    embed.set_footer(text=f"QuantaForge • v{BOT_VERSION} • RDX")
    await ctx.send(embed=embed)


@bot.command(name="start")
async def start_cmd(ctx, vps_identifier: str):
    vps = get_vps_by_identifier(ctx.author.id, vps_identifier)
    if not vps:
        await ctx.send(embed=discord.Embed(description="No VPS found.", color=discord.Color.red()))
        return
    if review_required_for_vps(vps):
        await ctx.send(embed=review_required_embed(ctx.guild))
        return
    ok, reason = await core_start(vps)
    if reason == "suspended":
        await ctx.send(embed=discord.Embed(description="This VPS is suspended by an admin. Contact support.", color=discord.Color.red()))
        return
    if ok:
        ssh_line = await core_regen_ssh(get_vps_by_container_id(vps['container_id']))
        desc = f"OS: {os_name_of(vps['os_type'])}"
        desc += "\nNew SSH session sent to DMs." if ssh_line else "\nFailed to generate new SSH session."
        if ssh_line:
            try:
                await ctx.author.send(embed=discord.Embed(title="New SSH Session Generated", description=f"```{ssh_line}```", color=discord.Color.green()))
            except discord.Forbidden:
                pass
        embed = discord.Embed(title="VPS Started Successfully", description=desc, color=discord.Color.green(), timestamp=datetime.now(timezone.utc))
        embed.set_footer(text=f"QuantaForge • v{BOT_VERSION} • RDX")
        await ctx.send(embed=embed)
    else:
        await ctx.send(embed=discord.Embed(description="Failed to start the VPS.", color=discord.Color.red()))


@bot.command(name="stop")
async def stop_cmd(ctx, vps_identifier: str):
    vps = get_vps_by_identifier(ctx.author.id, vps_identifier)
    if not vps:
        await ctx.send(embed=discord.Embed(description="No VPS found.", color=discord.Color.red()))
        return
    ok, _ = await core_stop(vps)
    if ok:
        embed = discord.Embed(title="VPS Stopped Successfully", description=f"OS: {os_name_of(vps['os_type'])}", color=discord.Color.green(), timestamp=datetime.now(timezone.utc))
        embed.set_footer(text=f"QuantaForge • v{BOT_VERSION} • RDX")
        await ctx.send(embed=embed)
    else:
        await ctx.send(embed=discord.Embed(description="Failed to stop the VPS.", color=discord.Color.red()))


@bot.command(name="restart")
async def restart_cmd(ctx, vps_identifier: str):
    vps = get_vps_by_identifier(ctx.author.id, vps_identifier)
    if not vps:
        await ctx.send(embed=discord.Embed(description="No VPS found.", color=discord.Color.red()))
        return
    if review_required_for_vps(vps):
        await ctx.send(embed=review_required_embed(ctx.guild))
        return
    ok, _ = await core_restart(vps)
    if ok:
        ssh_line = await core_regen_ssh(get_vps_by_container_id(vps['container_id']))
        desc = f"OS: {os_name_of(vps['os_type'])}"
        desc += "\nNew SSH session sent to DMs." if ssh_line else "\nFailed to generate new SSH session."
        if ssh_line:
            try:
                await ctx.author.send(embed=discord.Embed(title="New SSH Session Generated", description=f"```{ssh_line}```", color=discord.Color.green()))
            except discord.Forbidden:
                pass
        embed = discord.Embed(title="VPS Restarted Successfully", description=desc, color=discord.Color.green(), timestamp=datetime.now(timezone.utc))
        embed.set_footer(text=f"QuantaForge • v{BOT_VERSION} • RDX")
        await ctx.send(embed=embed)
    else:
        await ctx.send(embed=discord.Embed(description="Failed to restart the VPS.", color=discord.Color.red()))


@bot.command(name="reinstall")
async def reinstall_cmd(ctx, vps_identifier: str, os_type: str = "ubuntu"):
    os_type = os_type.lower()
    if os_type not in ("ubuntu", "debian"):
        await ctx.send("Invalid OS type. Use `ubuntu` or `debian`.")
        return
    vps = get_vps_by_identifier(ctx.author.id, vps_identifier)
    if not vps:
        await ctx.send(embed=discord.Embed(description="No VPS found.", color=discord.Color.red()))
        return
    if review_required_for_vps(vps):
        await ctx.send(embed=review_required_embed(ctx.guild))
        return
    async with ctx.typing():
        new_vps = await core_reinstall(vps, os_type)
    if new_vps:
        embed = discord.Embed(title="VPS Reinstalled Successfully", description=f"OS: {os_name_of(os_type)}\n```{new_vps['ssh_command']}```", color=discord.Color.green(), timestamp=datetime.now(timezone.utc))
        embed.set_footer(text=f"QuantaForge • v{BOT_VERSION} • RDX")
        try:
            await ctx.author.send(embed=embed)
            await ctx.send(embed=discord.Embed(description="VPS has been reinstalled. Check your DMs for details.", color=discord.Color.green()))
        except discord.Forbidden:
            await ctx.send(embed=embed)
    else:
        await ctx.send(embed=discord.Embed(description="Reinstall failed.", color=discord.Color.red()))


@bot.command(name="remove")
async def remove_cmd(ctx, vps_identifier: str):
    vps = get_vps_by_identifier(ctx.author.id, vps_identifier)
    if not vps:
        await ctx.send(embed=discord.Embed(description="VPS not found.", color=discord.Color.red()))
        return
    container_id = vps['container_id']
    backend = _vps_backend(vps)
    async with ctx.typing():
        await backend_stop(container_id, backend)
        await asyncio.sleep(2)
        await backend_rm(container_id, backend)
        delete_vps(container_id)
    embed = discord.Embed(title="VPS Removed Successfully", color=discord.Color.green(), timestamp=datetime.now(timezone.utc))
    embed.set_footer(text=f"QuantaForge • v{BOT_VERSION} • RDX")
    await ctx.send(embed=embed)


@bot.command(name="logs")
async def logs_cmd(ctx, vps_identifier: str, lines: int = 50):
    vps = get_vps_by_identifier(ctx.author.id, vps_identifier)
    if not vps:
        await ctx.send(embed=discord.Embed(description="VPS not found.", color=discord.Color.red()))
        return
    log_text = get_logs(vps['container_id'], lines, _vps_backend(vps))
    embed = discord.Embed(title=f"Logs for {vps['container_name']}", color=discord.Color.blue(), timestamp=datetime.now(timezone.utc))
    embed.add_field(name="Recent Logs", value=f"```{log_text}```", inline=False)
    embed.set_footer(text=f"QuantaForge • v{BOT_VERSION} • RDX")
    await ctx.send(embed=embed)


@bot.command(name="ping")
async def ping_cmd(ctx):
    latency = round(bot.latency * 1000)
    embed = discord.Embed(title="🏓 Pong!", description=f"Latency: {latency}ms", color=discord.Color.green(), timestamp=datetime.now(timezone.utc))
    embed.set_footer(text=f"QuantaForge • v{BOT_VERSION} • RDX")
    await ctx.send(embed=embed)


@bot.command(name="about")
async def about_cmd(ctx):
    embed = discord.Embed(
        title="⚡ QuantaForge VPS",
        description=(
            "**QuantaForge VPS** is a Discord-based VPS management bot designed for "
            "fast, simple, and reliable VPS deployment and management."
        ),
        color=discord.Color.from_rgb(88, 101, 242)
    )
    embed.add_field(
        name="✨ Features",
        value=(
            "⚡ Fast VPS Deployment\n"
            "🖥️ VPS Management\n"
            "🐳 Docker Powered Infrastructure\n"
            "🔐 Secure SSH Access\n"
            "📊 VPS Monitoring\n"
            "🛠️ Admin Controls\n"
            "🚀 Easy Discord-Based Management"
        ),
        inline=False
    )
    embed.add_field(
        name="👨‍💻 Developer Information",
        value="**Developer:** RDX\n**Built with:** Python • discord.py • Docker",
        inline=False
    )
    embed.add_field(
        name="🔗 Social Links",
        value="📺 **YouTube:** [@therdxzone](https://youtube.com/@therdxzone?si=-m3vvbye7SHF6Ln9)",
        inline=False
    )
    embed.set_footer(text=f"QuantaForge • v{BOT_VERSION} • RDX")
    embed.timestamp = discord.utils.utcnow()

    view = discord.ui.View()
    view.add_item(discord.ui.Button(
        label="YouTube",
        emoji="📺",
        style=discord.ButtonStyle.link,
        url="https://youtube.com/@therdxzone?si=-m3vvbye7SHF6Ln9"
    ))
    await ctx.send(embed=embed, view=view)


@bot.command(name="deploy")
async def deploy_cmd(ctx, os_type: str = "ubuntu"):
    global deploy_worker_running
    if not await deploy_channel_allowed(ctx):
        return
    os_type = os_type.lower()
    if os_type not in ("ubuntu", "debian"):
        await ctx.send("Invalid OS type. Use `ubuntu` or `debian`.")
        return

    if is_admin(ctx.author):
        await run_create_flow(ctx, ctx.author, os_type, DEFAULT_RAM, DEFAULT_CPU, DEFAULT_DISK, executor_is_admin=True, expiry_days=VPS_VALIDITY_DAYS, refill_managed=True)
        return

    if not is_deploy_enabled():
        embed = discord.Embed(description="!deploy is currently turned off by admin. Please contact admin to create VPS.", color=discord.Color.red())
        await ctx.send(embed=embed)
        return

    if ctx.author.id in deploy_in_progress:
        await ctx.send(embed=discord.Embed(description="⚠️ You already have a deployment request in progress.", color=discord.Color.orange()))
        return

    last_used = deploy_cooldowns.get(ctx.author.id)
    if last_used is not None:
        elapsed = time.time() - last_used
        if elapsed < DEPLOY_COOLDOWN_SECONDS:
            remaining = int(DEPLOY_COOLDOWN_SECONDS - elapsed)
            await ctx.send(embed=discord.Embed(description=f"Please wait {remaining}s before using `!deploy` again.", color=discord.Color.orange()))
            return

    if count_user_vps(ctx.author.id) >= SERVER_LIMIT:
        await ctx.send(embed=discord.Embed(description=f"You already have a VPS. Limit is {SERVER_LIMIT} per user.", color=discord.Color.red()))
        return

    deploy_cooldowns[ctx.author.id] = time.time()
    deploy_in_progress.add(ctx.author.id)

    async with deploy_queue_lock:
        deploy_queue.append((ctx, ctx.author, os_type))
        # Position counts the deployment currently running (if any) plus everything waiting.
        position = len(deploy_queue) + (1 if deploy_current is not None else 0)
        need_start = not deploy_worker_running
        if need_start:
            deploy_worker_running = True

    await ctx.send(embed=discord.Embed(description=f"⏳ Your deployment has been added to the queue.\nPosition: #{position}", color=discord.Color.blue()))

    if need_start:
        bot.loop.create_task(deploy_queue_worker())


async def deploy_queue_worker():
    """Single worker: processes queued deployments strictly one at a time."""
    global deploy_worker_running, deploy_current
    try:
        while True:
            async with deploy_queue_lock:
                if not deploy_queue:
                    deploy_worker_running = False
                    deploy_current = None
                    return
                job = deploy_queue.pop(0)
                deploy_current = job
                waiting = list(deploy_queue)
            job_ctx, target_user, job_os_type = job
            try:
                # Tell users still waiting their updated position (current job = #1).
                for idx, (w_ctx, w_user, _w_os) in enumerate(waiting, start=2):
                    try:
                        await w_ctx.send(f"{w_user.mention} ⏳ Queue update — your position is now #{idx}.")
                    except Exception:
                        pass
                try:
                    await job_ctx.send(f"{target_user.mention} 🚀 Your deployment is now starting...")
                except Exception:
                    pass
                ok = await asyncio.wait_for(
                    run_create_flow(job_ctx, target_user, job_os_type, DEFAULT_RAM, DEFAULT_CPU, DEFAULT_DISK, executor_is_admin=False, expiry_days=VPS_VALIDITY_DAYS, refill_managed=True, review_gate=True),
                    timeout=DEPLOY_JOB_TIMEOUT_SECONDS
                )
                msg = "✅ Deployment completed successfully." if ok else "❌ Deployment failed."
                try:
                    await job_ctx.send(f"{target_user.mention} {msg}")
                except Exception:
                    pass
            except Exception as e:
                logger.error(f"Queued deploy failed for {target_user.id}: {e!r}")
                try:
                    await job_ctx.send(f"{target_user.mention} ❌ Deployment failed.")
                except Exception:
                    pass
            finally:
                deploy_in_progress.discard(target_user.id)
                async with deploy_queue_lock:
                    deploy_current = None
            await asyncio.sleep(10)
    except BaseException:
        # Fatal/cancelled: make sure the queue is never left permanently "running" with no worker.
        deploy_worker_running = False
        deploy_current = None
        raise


@bot.command(name="deployvps")
async def deployvps_toggle_cmd(ctx, state: str = None):
    if not is_admin(ctx.author):
        await ctx.send(embed=discord.Embed(description="This command is restricted to admins only.", color=discord.Color.red()))
        return
    if state is None or state.lower() not in ("on", "off"):
        current = "ON" if is_deploy_enabled() else "OFF"
        await ctx.send(embed=discord.Embed(description=f"Usage: `!deployvps on` or `!deployvps off`\nCurrently: **{current}**", color=discord.Color.blue()))
        return
    if state.lower() == "on":
        set_setting("deploy_enabled", "1")
        await ctx.send(embed=discord.Embed(description="✅ `!deploy` is now **enabled** for all users.", color=discord.Color.green()))
    else:
        set_setting("deploy_enabled", "0")
        await ctx.send(embed=discord.Embed(description="🚫 `!deploy` is now **disabled** for regular users. Admins can still use it.", color=discord.Color.red()))


@bot.command(name="lxc")
async def lxc_toggle_cmd(ctx, state: str = None):
    """Admin-only: switch which backend NEW VPS instances are created with.
    Does NOT touch any existing VPS — those keep using whatever backend
    they were originally created on."""
    if not is_admin(ctx.author):
        await ctx.send(embed=discord.Embed(description="This command is restricted to admins only.", color=discord.Color.red()))
        return
    if state is None or state.lower() not in ("on", "off"):
        current = get_container_backend()
        await ctx.send(embed=discord.Embed(
            description=(
                f"Usage: `!lxc on` or `!lxc off`\n"
                f"Currently: **{'LXC' if current == 'lxc' else 'Docker'}** "
                f"(used for newly created VPS instances only; existing VPS are unaffected)."
            ),
            color=discord.Color.blue()
        ))
        return

    state = state.lower()
    if state == "on":
        if not is_lxc_available():
            async with _lxd_install_lock:
                if not is_lxc_available():
                    msg = await ctx.send(embed=discord.Embed(
                        title="⏳ Setting up LXC/LXD",
                        description="Starting install...",
                        color=discord.Color.orange()
                    ))

                    last = {}

                    async def _update(state):
                        last["s"] = {"steps": [list(x) for x in state["steps"]], "pct": state["pct"], "current": state["current"]}
                        await msg.edit(embed=_lxd_progress_embed(state))

                    ok, log_text = await async_install_lxd(_update)
                    if not ok or not is_lxc_available():
                        fail_state = last.get("s") or {"steps": [[t, "pending"] for t, _, _ in _lxd_install_steps()], "pct": 0, "current": ""}
                        err = _lxd_progress_embed(fail_state, final_ok=False)
                        err.description = (
                            "LXC mode **not** enabled. Bot ko root se chalao (ya passwordless sudo do), ya manually:\n"
                            "```\nsudo snap install lxd\nsudo lxd init --auto\nsudo usermod -aG lxd $USER\n```"
                        )
                        err.add_field(name="Last log", value=f"```\n{log_text[-700:] or 'no output'}\n```", inline=False)
                        await msg.edit(embed=err)
                        return
                    done_state = {"steps": [[t, "done"] for t, _, _ in _lxd_install_steps()], "pct": 100, "current": "Completed"}
                    await msg.edit(embed=_lxd_progress_embed(done_state, final_ok=True))
        set_container_backend('lxc')
        await ctx.send(embed=discord.Embed(
            description="✅ LXC mode **enabled**. New VPS instances will now be created as LXC/LXD containers. Existing VPS instances keep running as-is.",
            color=discord.Color.green()
        ))
    else:
        if not is_docker_available():
            embed = discord.Embed(
                title="Docker Unavailable",
                description="Docker does not appear to be available on this host, so Docker mode was **not** enabled. The bot did **not** fall back to LXC — please ensure the Docker daemon is running, then retry.",
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
            return
        set_container_backend('docker')
        await ctx.send(embed=discord.Embed(
            description="✅ Docker mode **enabled**. New VPS instances will now be created as Docker containers. Existing VPS instances keep running as-is.",
            color=discord.Color.green()
        ))


# ---------------------------------------------------------------------------
# Admin "!" commands
# ---------------------------------------------------------------------------

@bot.command(name="admin-create")
async def admin_create_cmd(ctx, target_user: discord.User, os_type: str, ram: str = None, cpu: str = None, disk: str = None, days: int = None, paid: str = "no"):
    if not is_admin(ctx.author):
        await ctx.send(embed=discord.Embed(description="This command is restricted to admins only.", color=discord.Color.red()))
        return
    os_type = os_type.lower()
    if os_type not in ("ubuntu", "debian"):
        await ctx.send("Invalid OS type. Use `ubuntu` or `debian`.")
        return
    ram = ram or DEFAULT_RAM
    cpu = cpu or DEFAULT_CPU
    disk = disk or DEFAULT_DISK
    # No SERVER_LIMIT / TOTAL_SERVER_LIMIT check here — admins are exempt (see core_create),
    # and this command is already admin-only. Host resource validation still applies.
    expiry_days = days if days is not None else 30
    is_paid = str(paid).lower() in ("yes", "true", "paid", "1")
    await run_create_flow(ctx, target_user, os_type, ram, cpu, disk, executor_is_admin=True, expiry_days=expiry_days, paid=is_paid, review_gate=(not is_paid))


@bot.command(name="admin-qemu")
async def admin_qemu_cmd(ctx, state: str = None):
    if not is_admin(ctx.author):
        await ctx.send(embed=discord.Embed(description="This command is restricted to admins only.", color=discord.Color.red()))
        return
    if state is None or state.lower() not in ("on", "off"):
        cur = "on" if qemu_mode_enabled() else "off"
        await ctx.send(embed=discord.Embed(description=f"Usage: `!admin-qemu on` or `!admin-qemu off`. Currently: **{cur}**.", color=discord.Color.blue()))
        return
    set_setting('qemu_mode', state.lower())
    if state.lower() == "on":
        desc = "✅ **Convert to QEMU** is now available to all users in `!manage`."
    else:
        desc = "🔒 **Convert to QEMU** is now admin testing only."
    await ctx.send(embed=discord.Embed(description=desc, color=discord.Color.green() if state.lower() == "on" else discord.Color.orange()))


@bot.command(name="admin-manage")
async def admin_manage_cmd(ctx, target_user: discord.User, vps_identifier: str, action: str, new_ram: str = None, new_cpu: str = None):
    if not is_admin(ctx.author):
        await ctx.send(embed=discord.Embed(description="This command is restricted to admins only.", color=discord.Color.red()))
        return
    action = action.lower()
    if action not in ("start", "stop", "restart", "delete", "suspend", "unsuspend", "upgrade"):
        await ctx.send("Invalid action. Use one of: start, stop, restart, delete, suspend, unsuspend, upgrade.")
        return
    vps = get_vps_by_identifier(target_user.id, vps_identifier)
    if not vps:
        await ctx.send(embed=discord.Embed(description="VPS not found for this user.", color=discord.Color.red()))
        return
    container_id = vps['container_id']
    backend = _vps_backend(vps)
    success = False
    msg = ""
    if action == "delete":
        await backend_stop(container_id, backend)
        await asyncio.sleep(2)
        await backend_rm(container_id, backend)
        delete_vps(container_id)
        success = True
        msg = f"Deleted VPS for {target_user}"
    elif action in ("start", "stop", "restart"):
        if action == "start":
            success = await backend_start(container_id, backend)
            update_vps_status(container_id, "running")
        elif action == "stop":
            success = await backend_stop(container_id, backend)
            update_vps_status(container_id, "stopped")
        else:
            success = await backend_restart(container_id, backend)
            update_vps_status(container_id, "running")
        msg = f"{action.title()}ed VPS for {target_user}"
    elif action == "suspend":
        success = await backend_stop(container_id, backend)
        if success:
            update_vps_status(container_id, "stopped")
            update_vps_suspended(container_id, 1)
        msg = f"Suspended VPS for {target_user}"
    elif action == "unsuspend":
        update_vps_suspended(container_id, 0)
        success = True
        msg = f"Unsuspended VPS for {target_user}. They can now start it."
    elif action == "upgrade":
        if not new_ram or not new_cpu:
            await ctx.send("Please provide new RAM and CPU. Example: !admin-manage @user <vps_id> upgrade 12g 4")
            return
        success = await backend_upgrade(container_id, backend, new_ram, new_cpu)
        if success:
            update_vps_resources(container_id, new_ram, new_cpu)
        msg = f"Upgraded VPS for {target_user} to {new_ram} RAM / {new_cpu} CPU (data preserved)."
    if success:
        embed = discord.Embed(title="Admin Action Completed", description=msg, color=discord.Color.green(), timestamp=datetime.now(timezone.utc))
        embed.set_footer(text=f"QuantaForge • v{BOT_VERSION} • RDX")
        await ctx.send(embed=embed)
    else:
        await ctx.send(embed=discord.Embed(description="Action failed.", color=discord.Color.red()))


@bot.command(name="admin-kill-all")
async def admin_kill_all_cmd(ctx):
    if not is_admin(ctx.author):
        await ctx.send(embed=discord.Embed(description="This command is restricted to admins only.", color=discord.Color.red()))
        return
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT container_id, backend FROM vps WHERE status = "running"')
    running = cursor.fetchall()
    conn.close()
    stopped = 0
    async with ctx.typing():
        for row in running:
            cid = row['container_id']
            row_backend = row['backend'] or 'docker'
            if await backend_stop(cid, row_backend):
                update_vps_status(cid, "stopped")
                stopped += 1
                logger.info(f"Stopped {cid}")
    embed = discord.Embed(title="Admin: Kill All Running VPS", description=f"Successfully stopped {stopped} running VPS instances.", color=discord.Color.green(), timestamp=datetime.now(timezone.utc))
    embed.set_footer(text=f"QuantaForge • v{BOT_VERSION} • RDX")
    await ctx.send(embed=embed)


@bot.command(name="admin-list")
async def admin_list_cmd(ctx):
    if not is_admin(ctx.author):
        await ctx.send(embed=discord.Embed(description="This command is restricted to admins only.", color=discord.Color.red()))
        return
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT u.username, v.container_id, v.container_name, v.os_type, v.hostname, v.status, v.ram, v.cpu, v.disk, v.suspended
        FROM vps v JOIN users u ON v.user_id = u.user_id
        ORDER BY v.created_at DESC
    ''')
    all_vps = cursor.fetchall()
    conn.close()
    if not all_vps:
        await ctx.send(embed=discord.Embed(description="No VPS instances found.", color=discord.Color.red()))
        return
    embed = discord.Embed(title="All VPS Instances", color=discord.Color.blue(), timestamp=datetime.now(timezone.utc))
    embed.set_author(name=bot.user.name, icon_url=bot.user.avatar.url if bot.user.avatar else None)
    for row in all_vps[:25]:
        status_emoji = "🟢" if row['status'] == "running" else "🔴"
        suspended_text = "(Suspended)" if row['suspended'] else ""
        embed.add_field(
            name=f"{status_emoji} {row['username']} - {row['container_name']} ({row['os_type']}) {suspended_text}",
            value=f"ID: ```{row['container_id']}```\nHostname: {row['hostname']}\nStatus: {row['status']}\nResources: {row['ram']} RAM | {row['cpu']} CPU | {row['disk']} Disk",
            inline=False
        )
    footer = f"QuantaForge • v{BOT_VERSION} • RDX"
    if len(all_vps) > 25:
        footer += f" | Showing first 25 of {len(all_vps)}"
    embed.set_footer(text=footer)
    await ctx.send(embed=embed)


@bot.command(name="admin-list-users")
async def admin_list_users_cmd(ctx):
    if not is_admin(ctx.author):
        await ctx.send(embed=discord.Embed(description="This command is restricted to admins only.", color=discord.Color.red()))
        return
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT u.username, COUNT(v.id) as total_vps,
               SUM(CASE WHEN v.status = 'running' THEN 1 ELSE 0 END) as running_vps
        FROM users u LEFT JOIN vps v ON u.user_id = v.user_id
        GROUP BY u.user_id, u.username
        ORDER BY total_vps DESC
    ''')
    users = cursor.fetchall()
    conn.close()
    if not users:
        await ctx.send(embed=discord.Embed(description="No users found.", color=discord.Color.red()))
        return
    embed = discord.Embed(title="Users Overview", color=discord.Color.blue(), timestamp=datetime.now(timezone.utc))
    embed.set_author(name=bot.user.name, icon_url=bot.user.avatar.url if bot.user.avatar else None)
    for row in users[:25]:
        total = row['total_vps'] or 0
        running = row['running_vps'] or 0
        embed.add_field(name=row['username'], value=f"Total VPS: {total} | Running: {running}", inline=False)
    embed.set_footer(text=f"QuantaForge • v{BOT_VERSION} • RDX")
    await ctx.send(embed=embed)


@bot.command(name="admin-stats")
async def admin_stats_cmd(ctx):
    if not is_admin(ctx.author):
        await ctx.send(embed=discord.Embed(description="This command is restricted to admins only.", color=discord.Color.red()))
        return
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(*) FROM users')
    num_users = cursor.fetchone()[0]
    cursor.execute('SELECT COUNT(*) FROM vps')
    num_vps = cursor.fetchone()[0]
    cursor.execute('SELECT COUNT(*) FROM vps WHERE status="running"')
    num_running = cursor.fetchone()[0]
    cursor.execute('SELECT COUNT(*) FROM bans')
    num_banned = cursor.fetchone()[0]
    cursor.execute('SELECT ram, cpu, disk FROM vps WHERE status="running"')
    rows = cursor.fetchall()
    total_cpu = sum(float(row['cpu']) for row in rows)
    total_ram = sum(parse_gb(row['ram']) for row in rows)
    total_disk = sum(parse_gb(row['disk']) for row in rows)
    conn.close()
    embed = discord.Embed(title="Bot Statistics", color=discord.Color.blue(), timestamp=datetime.now(timezone.utc))
    embed.set_author(name=bot.user.name, icon_url=bot.user.avatar.url if bot.user.avatar else None)
    embed.add_field(name="Total Users", value=num_users, inline=True)
    embed.add_field(name="Banned Users", value=num_banned, inline=True)
    embed.add_field(name="Total VPS", value=num_vps, inline=True)
    embed.add_field(name="Running VPS", value=num_running, inline=True)
    embed.add_field(name="Total CPU Allocated", value=f"{total_cpu} cores", inline=True)
    embed.add_field(name="Total RAM Allocated", value=f"{total_ram:.1f} GB", inline=True)
    embed.add_field(name="Total Disk Allocated", value=f"{total_disk:.1f} GB", inline=True)
    embed.set_footer(text=f"QuantaForge • v{BOT_VERSION} • RDX")
    await ctx.send(embed=embed)


@bot.command(name="admin-delete-user")
async def admin_delete_user_cmd(ctx, target_user: discord.User):
    if not is_admin(ctx.author):
        await ctx.send(embed=discord.Embed(description="This command is restricted to admins only.", color=discord.Color.red()))
        return
    user_id = target_user.id
    vps_list = get_user_vps(user_id)
    deleted = 0
    async with ctx.typing():
        for vps in vps_list:
            container_id = vps['container_id']
            backend = _vps_backend(vps)
            await backend_stop(container_id, backend)
            await asyncio.sleep(2)
            await backend_rm(container_id, backend)
            delete_vps(container_id)
            deleted += 1
            logger.info(f"Deleted VPS {container_id} for user {user_id}")
    embed = discord.Embed(description=f"Deleted {deleted} VPS instances for {target_user}.", color=discord.Color.green(), timestamp=datetime.now(timezone.utc))
    embed.set_footer(text=f"QuantaForge • v{BOT_VERSION} • RDX")
    await ctx.send(embed=embed)


@bot.command(name="admin-ban")
async def admin_ban_cmd(ctx, target_user: discord.User):
    if not is_admin(ctx.author):
        await ctx.send(embed=discord.Embed(description="This command is restricted to admins only.", color=discord.Color.red()))
        return
    add_ban(target_user.id)
    embed = discord.Embed(description=f"Banned {target_user} from creating VPS instances.", color=discord.Color.green(), timestamp=datetime.now(timezone.utc))
    embed.set_footer(text=f"QuantaForge • v{BOT_VERSION} • RDX")
    await ctx.send(embed=embed)


@bot.command(name="admin-unban")
async def admin_unban_cmd(ctx, target_user: discord.User):
    if not is_admin(ctx.author):
        await ctx.send(embed=discord.Embed(description="This command is restricted to admins only.", color=discord.Color.red()))
        return
    remove_ban(target_user.id)
    embed = discord.Embed(description=f"Unbanned {target_user}.", color=discord.Color.green(), timestamp=datetime.now(timezone.utc))
    embed.set_footer(text=f"QuantaForge • v{BOT_VERSION} • RDX")
    await ctx.send(embed=embed)


@bot.command(name="admin-vps-info")
async def admin_vps_info_cmd(ctx, target_user: discord.User, vps_identifier: str):
    if not is_admin(ctx.author):
        await ctx.send(embed=discord.Embed(description="This command is restricted to admins only.", color=discord.Color.red()))
        return
    vps = get_vps_by_identifier(target_user.id, vps_identifier)
    if not vps:
        await ctx.send(embed=discord.Embed(description="VPS not found.", color=discord.Color.red()))
        return
    container_id = vps['container_id']
    backend = _vps_backend(vps)
    uptime = get_uptime(container_id, backend)
    stats = get_stats(container_id, backend)
    embed = discord.Embed(title=f"{target_user.name} - VPS Details: {vps['container_name']}", color=discord.Color.blue(), timestamp=datetime.now(timezone.utc))
    embed.set_author(name=bot.user.name, icon_url=bot.user.avatar.url if bot.user.avatar else None)
    embed.add_field(name="OS", value=os_name_of(vps['os_type']), inline=True)
    embed.add_field(name="Hostname", value=vps['hostname'], inline=True)
    embed.add_field(name="Backend", value=backend, inline=True)
    embed.add_field(name="Status", value=vps['status'], inline=True)
    embed.add_field(name="Suspended", value="Yes" if vps['suspended'] else "No", inline=True)
    embed.add_field(name="Container ID", value=f"```{container_id}```", inline=False)
    embed.add_field(name="Allocated Resources", value=f"{vps['ram']} RAM | {vps['cpu']} CPU | {vps['disk']} Disk", inline=False)
    embed.add_field(name="Current Usage", value=f"CPU: {stats['cpu']} | Mem: {stats['mem']}", inline=False)
    embed.add_field(name="Uptime", value=uptime, inline=True)
    embed.add_field(name="Network I/O", value=stats['net'], inline=False)
    embed.add_field(name="Created At", value=vps['created_at'], inline=True)
    if vps['ssh_command']:
        ssh_trunc = vps['ssh_command'][:100] + "..." if len(vps['ssh_command']) > 100 else vps['ssh_command']
        embed.add_field(name="SSH Command", value=f"```{ssh_trunc}```", inline=False)
    embed.set_footer(text=f"QuantaForge • v{BOT_VERSION} • RDX")
    await ctx.send(embed=embed)


@bot.command(name="admin-logs")
async def admin_logs_cmd(ctx, target_user: discord.User, vps_identifier: str, lines: int = 50):
    if not is_admin(ctx.author):
        await ctx.send(embed=discord.Embed(description="This command is restricted to admins only.", color=discord.Color.red()))
        return
    vps = get_vps_by_identifier(target_user.id, vps_identifier)
    if not vps:
        await ctx.send(embed=discord.Embed(description="VPS not found.", color=discord.Color.red()))
        return
    log_text = get_logs(vps['container_id'], lines, _vps_backend(vps))
    embed = discord.Embed(title=f"Logs for {target_user.name}'s {vps['container_name']}", color=discord.Color.blue(), timestamp=datetime.now(timezone.utc))
    embed.set_author(name=bot.user.name, icon_url=bot.user.avatar.url if bot.user.avatar else None)
    embed.add_field(name="Recent Logs", value=f"```{log_text}```", inline=False)
    embed.set_footer(text=f"QuantaForge • v{BOT_VERSION} • RDX")
    await ctx.send(embed=embed)


@bot.command(name="set-avatar")
async def set_avatar_cmd(ctx):
    """Admin-only: update the bot's Discord avatar from an attached image.
    (Requires Discord API support via bot.user.edit(avatar=...); some changes
    may still need to be made manually in the Discord Developer Portal.)"""
    if not is_admin(ctx.author):
        await ctx.send(embed=discord.Embed(description="This command is restricted to admins only.", color=discord.Color.red()))
        return
    if not ctx.message.attachments:
        await ctx.send("Attach an image with `!set-avatar` to update the bot's avatar.")
        return
    try:
        image_bytes = await ctx.message.attachments[0].read()
        await bot.user.edit(avatar=image_bytes)
        await ctx.send(embed=discord.Embed(description="Bot avatar updated.", color=discord.Color.green()))
    except discord.HTTPException as e:
        await ctx.send(embed=discord.Embed(description=f"Failed to update avatar: {e}", color=discord.Color.red()))


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(embed=discord.Embed(description=f"Missing argument: `{error.param.name}`. Use `!help` for usage.", color=discord.Color.red()))
        return
    if isinstance(error, (commands.BadArgument, commands.UserNotFound)):
        await ctx.send(embed=discord.Embed(description="Invalid argument provided. Use `!help` for usage.", color=discord.Color.red()))
        return
    logger.error(f"Command error in {ctx.command}: {error}")
    await ctx.send(embed=discord.Embed(description="Something went wrong running that command.", color=discord.Color.red()))


@tasks.loop(minutes=5)
async def sync_statuses():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT container_id, status, backend FROM vps')
    rows = cursor.fetchall()
    conn.close()
    for row in rows:
        cid = row['container_id']
        stat = row['status']
        row_backend = row['backend'] or 'docker'
        if row_backend == 'lxc':
            try:
                data = _lxc_list_json(cid)
                if not data:
                    if stat != "stopped":
                        update_vps_status(cid, "stopped")
                        logger.info(f"Updated non-existent LXC {cid} to stopped")
                    continue
                lxc_status = data[0].get('status', '')
                out = "running" if lxc_status == "Running" else "stopped"
                if out != stat:
                    update_vps_status(cid, out)
                    logger.info(f"Updated status of {cid} to {out}")
            except Exception as e:
                logger.error(f"Status sync error for {cid}: {e}")
        else:
            try:
                out = subprocess.check_output(["docker", "inspect", "-f", "{{.State.Status}}", cid]).decode().strip()
                if out != stat:
                    update_vps_status(cid, out)
                    logger.info(f"Updated status of {cid} to {out}")
            except subprocess.CalledProcessError:
                if stat != "stopped":
                    update_vps_status(cid, "stopped")
                    logger.info(f"Updated non-existent {cid} to stopped")
            except Exception as e:
                logger.error(f"Status sync error for {cid}: {e}")

# Events
# ---- Docker base images: build automatically on first start if missing ----
_DOCKERFILES = {
    "rdx-ubuntu:22.04": "FROM ubuntu:22.04\nENV DEBIAN_FRONTEND=noninteractive\nRUN apt-get update && apt-get install -y systemd systemd-sysv sudo curl wget nano git tmate openssh-server python3 ca-certificates neofetch && rm -rf /var/lib/apt/lists/*\n",
    "rdx-debian:bookworm": "FROM debian:bookworm\nENV DEBIAN_FRONTEND=noninteractive\nRUN apt-get update && apt-get install -y systemd systemd-sysv sudo curl wget nano git tmate openssh-server python3 ca-certificates neofetch && rm -rf /var/lib/apt/lists/*\n",
}


async def ensure_docker_images():
    if not shutil.which("docker"):
        return
    for tag, dockerfile in _DOCKERFILES.items():
        try:
            chk = await asyncio.create_subprocess_exec(
                "docker", "image", "inspect", tag,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await chk.communicate()
            if chk.returncode == 0:
                continue
            logger.info(f"Docker image {tag} missing - building it (first start only, takes a few minutes)...")
            proc = await asyncio.create_subprocess_exec(
                "docker", "build", "-t", tag, "-",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            out, _ = await asyncio.wait_for(proc.communicate(dockerfile.encode()), timeout=1200)
            if proc.returncode == 0:
                logger.info(f"Docker image {tag} built.")
            else:
                logger.error(f"Docker image {tag} build failed: {out.decode(errors='ignore')[-800:]}")
        except Exception as e:
            logger.error(f"ensure_docker_images({tag}) error: {e}")



async def review_enforce_existing():
    # Turn OFF every running VPS whose owner has not posted the review phrase yet.
    global _review_enforced
    if _review_enforced or not review_enabled():
        return
    _review_enforced = True
    await asyncio.sleep(5)
    for cid in sorted(REVIEW_CHANNEL_IDS):
        if bot.get_channel(cid) is None:
            logger.warning(f"REVIEW channel {cid} not found among channels the bot can see (is the bot invited to that server?).")
    logger.info(f"Review channels: {sorted(REVIEW_CHANNEL_IDS)} | phrase: {REVIEW_PHRASE!r}")
    if not REVIEW_STOP_ON_START:
        return
    try:
        conn = get_db_connection()
        rows = conn.execute("SELECT * FROM vps WHERE status = 'running'").fetchall()
        conn.close()
    except Exception as e:
        logger.error(f"review enforce: DB read failed: {e!r}")
        return
    notified = set()
    for row in rows:
        try:
            if not review_required_for_vps(row):
                continue
            if await backend_stop(row['container_id'], row['backend'] or 'docker'):
                update_vps_status(row['container_id'], "stopped")
                set_review_hold(row['container_id'], 1)
                logger.info(f"Review gate: stopped VPS {row['container_id']} of user {row['user_id']}")
            if row['user_id'] not in notified:
                notified.add(row['user_id'])
                await _dm_user(row['user_id'],
                               f"Your VPS was turned **OFF**.\nPost this message in {review_channels_text()} to turn it ON again. It starts automatically and the SSHX details are sent here:\n```{REVIEW_PHRASE}```",
                               discord.Color.orange())
        except Exception as e:
            logger.error(f"review enforce failed for {row['container_id']}: {e!r}")


@bot.listen('on_message')
async def review_listener(message):
    if message.author.bot or not review_enabled():
        return
    if message.channel.id not in REVIEW_CHANNEL_IDS:
        return
    if not review_phrase_matches(message.content):
        return
    uid = message.author.id
    if uid in _review_busy:
        return
    _review_busy.add(uid)
    try:
        mark_review_verified(uid)
        try:
            await message.add_reaction("✅")
        except Exception:
            pass
        held = get_review_held_vps(uid)
        started = 0
        for row in held:
            try:
                ok, reason = await core_start(row, force=True)
                if not ok:
                    logger.error(f"Review start failed for {row['container_id']}: {reason}")
                    continue
                await asyncio.sleep(5)
                fresh = get_vps_by_container_id(row['container_id'])
                ssh_line = await core_regen_ssh(fresh)
                set_review_hold(row['container_id'], 0)
                started += 1
                if ssh_line:
                    emb = discord.Embed(title="VPS Started - SSHX details",
                                        description=f"Thanks for the review! Your VPS is running now.\n```{ssh_line}```",
                                        color=discord.Color.green(), timestamp=datetime.now(timezone.utc))
                else:
                    emb = discord.Embed(description="Your VPS started, but the SSHX link could not be generated. Use `!manage` -> SSHX.",
                                        color=discord.Color.orange())
                emb.set_footer(text=f"QuantaForge • v{BOT_VERSION} • RDX")
                try:
                    await message.author.send(embed=emb)
                except discord.Forbidden:
                    await message.channel.send(f"{message.author.mention} VPS started, but your DMs are closed. Open DMs and use `!ssh`.")
            except Exception as e:
                logger.error(f"Review start error for {row['container_id']}: {e!r}")
        if not held:
            try:
                await message.author.send(embed=discord.Embed(
                    description="✅ Review received! You can start your VPS now with `!manage`.",
                    color=discord.Color.green()))
            except discord.Forbidden:
                pass
    finally:
        _review_busy.discard(uid)


@bot.event
async def on_ready():
    clean_existing_ssh_lines()
    asyncio.create_task(ensure_docker_images())
    if not fake_meminfo_updater.is_running():
        fake_meminfo_updater.start()
    change_status.start()
    sync_statuses.start()
    if not vps_expiry_checker.is_running():
        vps_expiry_checker.start()
    validate_deploy_channel_config()
    asyncio.create_task(review_enforce_existing())
    for _cid in sorted(DEPLOY_COMMAND_CHANNEL_IDS):
        if bot.get_channel(_cid) is None:
            logger.warning(f"DEPLOY_COMMAND_CHANNEL_ID {_cid} was not found among channels the bot can see (is the bot invited to that server?).")
    logger.info(f"Deploy command channels: {sorted(DEPLOY_COMMAND_CHANNEL_IDS)}")
    logger.info(f'Bot ready: {bot.user}')

@tasks.loop(seconds=10)
async def change_status():
    try:
        count = get_total_instances()
        status = f"{BOT_STATUS_NAME} | {count} Active"
        await bot.change_presence(activity=discord.Game(name=status))
    except Exception as e:
        logger.error(f"Status update failed: {e}")

if __name__ == "__main__":
    if not TOKEN:
        logger.error("TOKEN not set in .env")
        sys.exit(1)

# NOTE: The previous "100% CPU/load auto-off" feature (a background monitor
# loop that force-restarted whichever VPS hit ~90% CPU) has been intentionally
# removed. A VPS reaching high CPU/load is never automatically stopped,
# restarted, or otherwise touched by the bot for either Docker or LXC VPS.
# sync_statuses (above) only reads and mirrors container status into the DB —
# it never stops/starts/restarts anything based on resource usage.
bot.run(TOKEN)
