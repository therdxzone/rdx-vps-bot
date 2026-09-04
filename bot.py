import asyncio
import random
import logging
import subprocess
import sys
import os
import re
import time
import discord
from discord.ext import commands, tasks
import docker
import asyncio
import sqlite3
from dotenv import load_dotenv
from datetime import datetime, timezone

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

DEPLOY_COOLDOWN_SECONDS = 100
deploy_cooldowns = {}
deploy_in_progress = set()
deploy_queue = []
deploy_queue_lock = asyncio.Lock()
deploy_worker_running = False 

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

def add_vps(user_id, container_id, container_name, os_type, hostname, ssh_command, ram=DEFAULT_RAM, cpu=DEFAULT_CPU, disk=DEFAULT_DISK, expiry_days=30):
    conn = get_db_connection()
    cursor = conn.cursor()
    from datetime import timedelta
    expires_at = (datetime.now(timezone.utc) + timedelta(days=expiry_days)).isoformat() if expiry_days else None
    cursor.execute('''
        INSERT INTO vps (user_id, container_id, container_name, os_type, hostname, status, ssh_command, ram, cpu, disk, suspended, expires_at)
        VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, 0, ?)
    ''', (user_id, container_id, container_name, os_type, hostname, ssh_command, ram, cpu, disk, expires_at))
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
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('UPDATE vps SET ssh_command = ? WHERE container_id = ?', (ssh_command, container_id))
    conn.commit()
    conn.close()

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

def get_uptime(container_id):
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

def get_stats(container_id):
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

def get_logs(container_id, lines=50):
    try:
        output = subprocess.check_output(["docker", "logs", "--tail", str(lines), container_id], stderr=subprocess.STDOUT).decode()
        return output[-2000:]  # Truncate for Discord limit
    except Exception as e:
        logger.error(f"Logs error for {container_id}: {e}")
        return "Failed to fetch logs"

# Async Docker helpers
async def async_docker_run(image, hostname, ram, cpu, disk, container_name):
    cmd = [
        "docker", "run", "-d",
        "--privileged", "--cap-add=ALL",
        "--restart", "unless-stopped",
        f"--memory={ram}",
        f"--cpus={cpu}",
        f"--hostname={hostname}",
        f"--name={container_name}",
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
        mem_bytes = parse_gb(ram) * (1024 ** 3)
        cmd = [
            "docker", "update",
            "--memory", str(mem_bytes),
            "--memory-swap", str(mem_bytes),
            "--cpus", str(cpu),
            container_id
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.error(f"docker update failed for {container_id}: {stderr.decode()}")
            return False
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

# SSH capture
async def capture_ssh_session_line(process):
    while True:
        try:
            output = await asyncio.wait_for(process.stdout.readline(), timeout=30.0)
            if not output:
                break
            output = output.decode('utf-8').strip()
            if "https://sshx.io/s/" in output:
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
                return output[idx + len("ssh session:"):].strip()
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
# Core VPS action helpers — ctx/interaction agnostic.
# Used by both the "!" prefix commands and the !manage button panel so the
# real backend logic (Docker, tmate, SQLite) only lives in one place.
# ---------------------------------------------------------------------------

def os_name_of(os_type):
    return "Ubuntu 22.04" if os_type == "ubuntu" else "Debian 12"


async def core_start(vps):
    if vps['suspended']:
        return False, "suspended"
    container_id = vps['container_id']
    ok = await async_docker_start(container_id)
    if ok:
        update_vps_status(container_id, "running")
    return ok, None


async def core_stop(vps):
    container_id = vps['container_id']
    ok = await async_docker_stop(container_id)
    if ok:
        update_vps_status(container_id, "stopped")
    return ok, None


async def core_restart(vps):
    container_id = vps['container_id']
    ok = await async_docker_restart(container_id)
    if ok:
        update_vps_status(container_id, "running")
    return ok, None


async def core_regen_ssh(vps):
    """Generate a fresh tmate SSH session for a running VPS. Returns the ssh line or None."""
    if not vps or vps['status'] != "running":
        return None
    exec_process = await docker_exec_tmate(vps['container_id'])
    if not exec_process:
        return None
    ssh_line = await capture_ssh_session_line(exec_process)
    if ssh_line:
        update_vps_ssh(vps['container_id'], ssh_line)
    return ssh_line


async def core_reinstall(vps, os_type):
    """Stop/remove the old container and provision a fresh one with the same resources."""
    container_id = vps['container_id']
    user_id = vps['user_id']
    hostname = vps['hostname']
    ram, cpu, disk = vps['ram'], vps['cpu'], vps['disk']
    await async_docker_stop(container_id)
    await asyncio.sleep(2)
    await async_docker_rm(container_id)
    delete_vps(container_id)
    suffix = random.randint(1000, 9999)
    new_container_name = f"{os_type}-vps-{user_id}-{suffix}"
    image = "rdx-ubuntu:22.04" if os_type == "ubuntu" else "rdx-debian:bookworm"
    new_container_id = await async_docker_run(image, hostname, ram, cpu, disk, new_container_name)
    if not new_container_id:
        return None
    await async_install_tmate(new_container_id, os_type)
    await asyncio.sleep(10)
    exec_process = await docker_exec_tmate(new_container_id)
    ssh_line = await capture_ssh_session_line(exec_process) if exec_process else None
    if ssh_line:
        add_vps(user_id, new_container_id, new_container_name, os_type, hostname, ssh_line, ram, cpu, disk)
        return get_vps_by_container_id(new_container_id)
    await async_docker_rm(new_container_id)
    return None


async def core_create(user_id, username, os_type, ram, cpu, disk, executor_is_admin, expiry_days=30, paid=False):
    """
    Full VPS creation flow: ban check -> limits (bypassed for admins) -> host
    resource validation (always enforced) -> Docker run -> tmate -> DB insert.
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
    # Host CPU/RAM validation always applies, admin or not.
    try:
        host_info = client.info()
        host_cpus = host_info['NCPU']
        host_mem_gb = host_info['MemTotal'] / (1024 ** 3)
        req_cpu = float(cpu)
        req_ram = parse_gb(ram)
        if req_cpu > host_cpus:
            return {"ok": False, "reason": "cpu", "host_cpus": host_cpus}
        if req_ram > host_mem_gb:
            return {"ok": False, "reason": "ram", "host_mem_gb": host_mem_gb}
    except Exception as e:
        logger.error(f"Resource validation failed: {e}")
        return {"ok": False, "reason": "validation_error"}
    hostname = "rdx-paid" if paid else f"{VPS_HOSTNAME}-{user_id}"
    suffix = random.randint(1000, 9999)
    container_name = f"{os_type}-vps-{user_id}-{suffix}"
    image = "rdx-ubuntu:22.04" if os_type == "ubuntu" else "rdx-debian:bookworm"
    container_id = await async_docker_run(image, hostname, ram, cpu, disk, container_name)
    if not container_id:
        return {"ok": False, "reason": "docker_fail"}
    await asyncio.sleep(5)
    await async_install_tmate(container_id, os_type)
    await asyncio.sleep(10)
    exec_process = await docker_exec_tmate(container_id)
    ssh_line = await capture_ssh_session_line(exec_process) if exec_process else None
    if not ssh_line:
        await async_docker_stop(container_id)
        await asyncio.sleep(2)
        await async_docker_rm(container_id)
        return {"ok": False, "reason": "ssh_fail"}
    add_vps(user_id, container_id, container_name, os_type, hostname, ssh_line, ram, cpu, disk, expiry_days)
    return {"ok": True, "reason": None, "vps": get_vps_by_container_id(container_id), "ssh_line": ssh_line}


CREATE_FAILURE_MESSAGES = {
    "banned": "You are banned from creating VPS instances.",
    "limit": f"You have reached the limit of {SERVER_LIMIT} VPS instances.",
    "total_limit": f"Global server limit reached: {TOTAL_SERVER_LIMIT} total running instances.",
    "cpu": "Requested CPU exceeds the host's available limit.",
    "ram": "Requested RAM exceeds the host's available limit.",
    "validation_error": "Resource validation failed. Please contact an admin.",
    "docker_fail": "Failed to create Docker container.",
    "ssh_fail": "Creation failed: Unable to generate SSH session.",
}


async def run_create_flow(ctx, target_user, os_type, ram, cpu, disk, executor_is_admin, expiry_days=30, paid=False):
    """Shared VPS-creation flow used by !deploy and !admin-create."""
    username = str(target_user)
    async with ctx.typing():
        result = await core_create(target_user.id, username, os_type, ram, cpu, disk, executor_is_admin, expiry_days, paid=paid)
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
        return
    await ctx.send(embed=discord.Embed(
        description=CREATE_FAILURE_MESSAGES.get(result["reason"], "VPS creation failed."),
        color=discord.Color.red()
    ))


# ---------------------------------------------------------------------------
# !manage panel — embed + interactive button view
# ---------------------------------------------------------------------------

def build_manage_embed(vps, node_label="Local Host"):
    status_running = vps['status'] == "running"
    status_emoji = "🟢" if status_running else "🔴"
    uptime = get_uptime(vps['container_id']) if status_running else "Not running"
    stats = get_stats(vps['container_id']) if status_running else {'cpu': 'N/A', 'mem': 'N/A', 'net': 'N/A'}
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
        exec_process = await docker_exec_tmate(vps['container_id'])
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
        await async_install_real_tmate(vps['container_id'])
        exec_process = await docker_exec_realtmate(vps['container_id'])
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
        uptime = get_uptime(vps['container_id'])
        stats = get_stats(vps['container_id'])
        embed = discord.Embed(title=f"📊 Stats — {vps['container_name']}", color=discord.Color.blue(), timestamp=datetime.now(timezone.utc))
        embed.add_field(name="CPU Usage", value=stats['cpu'], inline=True)
        embed.add_field(name="Memory Usage", value=stats['mem'], inline=True)
        embed.add_field(name="Network I/O", value=stats['net'], inline=True)
        embed.add_field(name="Uptime", value=uptime, inline=True)
        embed.add_field(name="Allocated", value=f"{vps['ram']} RAM | {vps['cpu']} CPU | {vps['disk']} Disk", inline=False)
        embed.set_footer(text=f"QuantaForge • v{BOT_VERSION} • RDX")
        await interaction.followup.send(embed=embed, ephemeral=True)


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
                "`!set-avatar` (with an image attached) — Update the bot's avatar"
            ),
            inline=False
        )
    embed.set_footer(text=f"QuantaForge • v{BOT_VERSION} • RDX")
    await ctx.send(embed=embed)


@bot.command(name="manage")
async def manage_cmd(ctx, vps_identifier: str = None):
    vps = get_vps_by_identifier(ctx.author.id, vps_identifier)
    if not vps:
        await ctx.send(embed=discord.Embed(description="You have no VPS instances to manage.", color=discord.Color.red()))
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
    uptime = get_uptime(vps['container_id'])
    stats = get_stats(vps['container_id'])
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
        uptime = get_uptime(vps['container_id'])
        suspended_text = "(Suspended)" if vps['suspended'] else ""
        embed.add_field(
            name=f"{status_emoji} {vps['container_name']} ({vps['os_type']}) {suspended_text}",
            value=f"ID: ```{vps['container_id']}```\nHostname: {vps['hostname']}\nStatus: {vps['status']}\nUptime: {uptime}\nResources: {vps['ram']} RAM | {vps['cpu']} CPU | {vps['disk']} Disk",
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
    async with ctx.typing():
        await async_docker_stop(container_id)
        await asyncio.sleep(2)
        await async_docker_rm(container_id)
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
    log_text = get_logs(vps['container_id'], lines)
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
    os_type = os_type.lower()
    if os_type not in ("ubuntu", "debian"):
        await ctx.send("Invalid OS type. Use `ubuntu` or `debian`.")
        return

    if is_admin(ctx.author):
        await run_create_flow(ctx, ctx.author, os_type, DEFAULT_RAM, DEFAULT_CPU, DEFAULT_DISK, executor_is_admin=True)
        return

    if not is_deploy_enabled():
        embed = discord.Embed(description="!deploy is currently turned off by admin. Please contact admin to create VPS.", color=discord.Color.red())
        await ctx.send(embed=embed)
        return

    if ctx.author.id in deploy_in_progress:
        await ctx.send(embed=discord.Embed(description="You are already in the queue or your VPS is being created. Please wait.", color=discord.Color.orange()))
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
        position = len(deploy_queue)
        need_start = not deploy_worker_running
        if need_start:
            deploy_worker_running = True

    if position == 1 and need_start:
        await ctx.send(embed=discord.Embed(description="Creating your VPS now...", color=discord.Color.blue()))
    else:
        await ctx.send(embed=discord.Embed(description=f"You are in queue #{position}. Please wait, your VPS will be created shortly.", color=discord.Color.blue()))

    if need_start:
        bot.loop.create_task(deploy_queue_worker())


async def deploy_queue_worker():
    global deploy_worker_running
    while True:
        async with deploy_queue_lock:
            if not deploy_queue:
                deploy_worker_running = False
                return
            job_ctx, target_user, job_os_type = deploy_queue.pop(0)
        try:
            await run_create_flow(job_ctx, target_user, job_os_type, DEFAULT_RAM, DEFAULT_CPU, DEFAULT_DISK, executor_is_admin=False, expiry_days=30)
        except Exception as e:
            logger.error(f"Queued deploy failed for {target_user.id}: {e}")
            try:
                await job_ctx.send(embed=discord.Embed(description="Something went wrong creating your VPS. Please contact an admin.", color=discord.Color.red()))
            except Exception:
                pass
        finally:
            deploy_in_progress.discard(target_user.id)
        await asyncio.sleep(10)


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
    await run_create_flow(ctx, target_user, os_type, ram, cpu, disk, executor_is_admin=True, expiry_days=expiry_days, paid=is_paid)


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
    success = False
    msg = ""
    if action == "delete":
        await async_docker_stop(container_id)
        await asyncio.sleep(2)
        await async_docker_rm(container_id)
        delete_vps(container_id)
        success = True
        msg = f"Deleted VPS for {target_user}"
    elif action in ("start", "stop", "restart"):
        if action == "start":
            success = await async_docker_start(container_id)
            update_vps_status(container_id, "running")
        elif action == "stop":
            success = await async_docker_stop(container_id)
            update_vps_status(container_id, "stopped")
        else:
            success = await async_docker_restart(container_id)
            update_vps_status(container_id, "running")
        msg = f"{action.title()}ed VPS for {target_user}"
    elif action == "suspend":
        success = await async_docker_stop(container_id)
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
        success = await async_docker_upgrade(container_id, new_ram, new_cpu)
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
    cursor.execute('SELECT container_id FROM vps WHERE status = "running"')
    running = cursor.fetchall()
    conn.close()
    stopped = 0
    async with ctx.typing():
        for row in running:
            cid = row['container_id']
            if await async_docker_stop(cid):
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
            await async_docker_stop(container_id)
            await asyncio.sleep(2)
            await async_docker_rm(container_id)
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
    uptime = get_uptime(container_id)
    stats = get_stats(container_id)
    embed = discord.Embed(title=f"{target_user.name} - VPS Details: {vps['container_name']}", color=discord.Color.blue(), timestamp=datetime.now(timezone.utc))
    embed.set_author(name=bot.user.name, icon_url=bot.user.avatar.url if bot.user.avatar else None)
    embed.add_field(name="OS", value=os_name_of(vps['os_type']), inline=True)
    embed.add_field(name="Hostname", value=vps['hostname'], inline=True)
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
    log_text = get_logs(vps['container_id'], lines)
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
    cursor.execute('SELECT container_id, status FROM vps')
    for row in cursor.fetchall():
        cid = row['container_id']
        stat = row['status']
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
    conn.close()

# Events
@bot.event
async def on_ready():
    change_status.start()
    sync_statuses.start()
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

# ================= VPS CPU MONITOR (auto-restart on high load) =================
import time
import docker as _docker_sdk
from discord.ext import tasks

MONITOR_CHANNEL_NAME = "monitor"
CPU_THRESHOLD = 90.0
CHECK_INTERVAL = 15      # seconds between checks
RESTART_COOLDOWN = 300   # seconds before same VPS can auto-restart again

_docker_client = _docker_sdk.from_env()
_last_restart = {}
_monitor_messages = {}   # guild_id -> discord.Message (live-edit hota hai)

def _get_container_cpu_percent(container):
    try:
        stats = container.stats(stream=False)
        cpu_delta = stats["cpu_stats"]["cpu_usage"]["total_usage"] - stats["precpu_stats"]["cpu_usage"]["total_usage"]
        sys_delta = stats["cpu_stats"]["system_cpu_usage"] - stats["precpu_stats"]["system_cpu_usage"]
        cpus = stats["cpu_stats"].get("online_cpus", 1)
        if sys_delta > 0 and cpu_delta > 0:
            return (cpu_delta / sys_delta) * cpus * 100.0
    except Exception:
        pass
    return 0.0

async def _ensure_monitor_channel(guild):
    channel = discord.utils.get(guild.text_channels, name=MONITOR_CHANNEL_NAME)
    if channel is None:
        overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False)}
        for role in guild.roles:
            if role.permissions.administrator:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True)
        channel = await guild.create_text_channel(MONITOR_CHANNEL_NAME, overwrites=overwrites)
    return channel

@tasks.loop(seconds=CHECK_INTERVAL)
async def _vps_monitor_loop():
    try:
        containers = await asyncio.to_thread(_docker_client.containers.list)
        usage = []
        for c in containers:
            cpu = await asyncio.to_thread(_get_container_cpu_percent, c)
            usage.append((c, cpu))

        for guild in bot.guilds:
            channel = await _ensure_monitor_channel(guild)

            embed = discord.Embed(title="🖥️ VPS Live Monitor", color=0x2ecc71)
            for c, cpu in sorted(usage, key=lambda x: x[1], reverse=True):
                embed.add_field(name=c.name, value=f"CPU: {cpu:.1f}%", inline=False)
            embed.set_footer(text=f"Threshold: {CPU_THRESHOLD}% | Har {CHECK_INTERVAL}s update")

            msg = _monitor_messages.get(guild.id)
            if msg is None:
                _monitor_messages[guild.id] = await channel.send(embed=embed)
            else:
                try:
                    await msg.edit(embed=embed)
                except discord.NotFound:
                    _monitor_messages[guild.id] = await channel.send(embed=embed)

            if usage:
                top_container, top_cpu = max(usage, key=lambda x: x[1])
                if top_cpu >= CPU_THRESHOLD:
                    now = time.time()
                    if now - _last_restart.get(top_container.id, 0) > RESTART_COOLDOWN:
                        _last_restart[top_container.id] = now
                        try:
                            await asyncio.to_thread(top_container.restart)
                            await channel.send(f"⚠️ **{top_container.name}** {top_cpu:.1f}% CPU par tha — force restart kar diya.")
                        except Exception as e:
                            await channel.send(f"❌ {top_container.name} restart fail: {e}")
    except Exception as e:
        print(f"[VPS Monitor] Error: {e}")

@_vps_monitor_loop.before_loop
async def _before_vps_monitor():
    await bot.wait_until_ready()

async def _start_vps_monitor(*_a, **_kw):
    if not _vps_monitor_loop.is_running():
        _vps_monitor_loop.start()

bot.add_listener(_start_vps_monitor, "on_ready")
# ================================================================================
bot.run(TOKEN)
