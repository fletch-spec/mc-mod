"""Deploy the pack to the laptop's modded server.

    python deploy.py              pull, sync mods, restart if it was running
    python deploy.py --start      ...and start it even if it was stopped
    python deploy.py --no-pull    use the checkout as it is
    python deploy.py --rollback   put back the mods and launcher from before the last deploy

Steps: git pull -> hold starts -> stop via the supervisor (it saves and backs
up) -> snapshot mods/ and the launcher -> match the Fabric launcher to
pack.toml's minecraft and fabric versions -> packwiz-installer syncs mods/
from pack/ -> release the hold -> start -> wait for "Running". If the server
doesn't come up, the snapshot is restored and it starts again on the old mods.

The launcher is Fabric's server launcher jar (fabric-server-launch.jar), which
has its Minecraft and loader versions built in and downloads the rest on first
start. When pack.toml asks for other versions, the matching jar comes from
Fabric's meta API, and the Minecraft server jar it needs is copied from the
vanilla server (C:\\minecraft, only ever read) if that one is the same version.

Never touches Java directly: start/stop always go through the supervisor
(127.0.0.1:47803, see ../supervisor.json). The dashboard's Stop closes the
supervisor, so a closed one means "stopped", and starting launches its
scheduled task ("task" in supervisor.json) first. Result goes to state/deploy.json,
which the wallpaper dashboard shows; output is appended to state/deploy.log.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server"
STATE = ROOT / "state"
LOCK = STATE / "deploy.lock"
RESULT = STATE / "deploy.json"
SNAPSHOT = STATE / "rollback" / "mods"
LAUNCHER_SNAPSHOT = STATE / "rollback" / "launcher"
LAUNCHER = SERVER / "fabric-server-launch.jar"
VANILLA_JAR = Path(r"C:\minecraft\server.jar")  # the vanilla server's; only ever read
FABRIC_META = "https://meta.fabricmc.net/v2"
BOOTSTRAP = ROOT / "tools" / "packwiz-installer-bootstrap.jar"
CFG = json.loads((ROOT / "supervisor.json").read_text(encoding="utf-8"))
HOLD = Path(CFG["hold_file"])
API = f"http://127.0.0.1:{CFG['api_port']}"
TOKEN_FILE = Path(r"C:\discord\control_token.txt")
START_TIMEOUT = 300  # seconds for the server to reach "Running"
SUPERVISOR_WAIT = 30  # seconds for a freshly launched supervisor to answer


def log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    with open(STATE / "deploy.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


class DeployError(Exception):
    pass


# ---------- supervisor ----------

def supervisor(method, path, timeout=10):
    req = urllib.request.Request(API + path, method=method, headers={
        "X-Control-Token": TOKEN_FILE.read_text(encoding="utf-8").strip()})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return json.load(res)
    except urllib.error.HTTPError as e:
        return json.load(e)
    except OSError as e:
        raise DeployError(f"The modded server's supervisor isn't reachable ({e}).") from e


def supervisor_up():
    try:
        supervisor("GET", "/status", timeout=3)
        return True
    except DeployError:
        return False


def ensure_supervisor():
    """Launch the supervisor's scheduled task if it isn't running, and wait for it."""
    if supervisor_up():
        return
    log(f"Launching the supervisor (task '{CFG['task']}')...")
    r = subprocess.run(["schtasks", "/run", "/tn", CFG["task"]], capture_output=True, text=True)
    if r.returncode:
        raise DeployError(f"Couldn't launch the supervisor: {(r.stderr or r.stdout).strip()}")
    deadline = time.monotonic() + SUPERVISOR_WAIT
    while time.monotonic() < deadline:
        time.sleep(1)
        if supervisor_up():
            return
    raise DeployError(f"The supervisor didn't come up within {SUPERVISOR_WAIT} s.")


def wait_until_up():
    deadline = time.monotonic() + START_TIMEOUT
    time.sleep(5)
    while time.monotonic() < deadline:
        st = supervisor("GET", "/status")
        if st["status"].startswith("Running"):
            return True
        if not st["running"]:  # Java exited: crash, incompatible mods, ...
            return False
        time.sleep(3)
    return False


def start_server():
    ensure_supervisor()
    log(supervisor("POST", "/mc/start?who=deploy")["message"])
    if wait_until_up():
        log("Server is up.")
        return True
    log("Server did not come up. Last lines of server/logs/latest.log:")
    for line in tail(SERVER / "logs" / "latest.log", 15):
        log("  " + line)
    return False


def tail(path, n):
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
    except OSError:
        return []


# ---------- steps ----------

def git(*args):
    r = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True)
    if r.returncode:
        raise DeployError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout.strip()


def pull():
    if not git("remote"):
        log("No git remote yet, skipping pull.")
        return
    if git("status", "--porcelain", "--", "pack"):
        raise DeployError("pack/ has uncommitted changes; commit or discard them first.")
    log(git("pull", "--ff-only") or "Pulled.")


# ---------- launcher ----------

def launcher_versions(jar=LAUNCHER):
    """(minecraft, fabric) that a Fabric server launcher jar launches, or None."""
    try:
        with zipfile.ZipFile(jar) as z:
            props = z.read("install.properties").decode("utf-8")
    except (OSError, KeyError, zipfile.BadZipFile):
        return None
    kv = dict(line.split("=", 1) for line in props.splitlines() if "=" in line)
    return kv.get("game-version"), kv.get("fabric-loader-version")


def server_jar(mc):
    """Where the launcher keeps (or downloads) the Minecraft server jar."""
    return SERVER / ".fabric" / "server" / f"{mc}-server.jar"


def jar_version(jar):
    """Minecraft version of a vanilla server jar, or None."""
    try:
        with zipfile.ZipFile(jar) as z:
            return json.loads(z.read("version.json"))["id"]
    except (OSError, KeyError, ValueError, zipfile.BadZipFile):
        return None


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "minecraft-mod-pipeline/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as res:
            return res.read()
    except OSError as e:
        raise DeployError(f"Couldn't download {url} ({e}).") from e


def sync_launcher():
    """Make the launcher match pack.toml. Returns a change note, or None if it already did."""
    with open(ROOT / "pack" / "pack.toml", "rb") as f:
        want = tomllib.load(f)["versions"]
    mc, loader = want["minecraft"], want["fabric"]
    have = launcher_versions()
    if have == (mc, loader):
        return None
    log(f"Switching the launcher to Minecraft {mc}, Fabric {loader}...")
    installers = json.loads(fetch(f"{FABRIC_META}/versions/installer"))
    installer = next(v["version"] for v in installers if v["stable"])
    tmp = LAUNCHER.with_suffix(".tmp")
    tmp.write_bytes(fetch(f"{FABRIC_META}/versions/loader/{mc}/{loader}/{installer}/server/jar"))
    got = launcher_versions(tmp)
    if got != (mc, loader):
        tmp.unlink()
        raise DeployError(f"Fabric's meta API returned a launcher for {got}, "
                          f"not Minecraft {mc} with Fabric {loader}.")
    tmp.replace(LAUNCHER)

    dest = server_jar(mc)
    if dest.exists():
        log(f"  Minecraft {mc}'s server jar is already there.")
    elif jar_version(VANILLA_JAR) == mc:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(VANILLA_JAR, dest.with_suffix(".tmp"))
        dest.with_suffix(".tmp").replace(dest)
        log(f"  Copied the Minecraft {mc} server jar from {VANILLA_JAR}.")
    else:
        log(f"  The launcher will download Minecraft {mc} on first start.")
    old = f"{have[0]}/{have[1]}" if have else "none"
    return f"launcher {old} -> {mc}/{loader}"


# ---------- snapshot ----------

# packwiz.json is packwiz-installer's record of the files it manages. It's
# snapshotted with mods/ so that after a rollback the record still matches the
# jars on disk, and the next sync replaces them instead of leaving duplicates.
# The launcher is snapshotted with the Minecraft server jar it launches.

def snapshot():
    shutil.rmtree(SNAPSHOT.parent, ignore_errors=True)
    SNAPSHOT.parent.mkdir(parents=True)
    if (SERVER / "mods").exists():
        shutil.copytree(SERVER / "mods", SNAPSHOT)
    if (SERVER / "packwiz.json").exists():
        shutil.copy2(SERVER / "packwiz.json", SNAPSHOT.parent / "packwiz.json")
    if LAUNCHER.exists():
        LAUNCHER_SNAPSHOT.mkdir()
        shutil.copy2(LAUNCHER, LAUNCHER_SNAPSHOT / LAUNCHER.name)
        have = launcher_versions()
        if have and server_jar(have[0]).exists():
            shutil.copy2(server_jar(have[0]), LAUNCHER_SNAPSHOT / server_jar(have[0]).name)


def restore():
    if not SNAPSHOT.parent.exists():
        raise DeployError("No rollback snapshot to restore.")
    shutil.rmtree(SERVER / "mods", ignore_errors=True)
    if SNAPSHOT.exists():
        shutil.copytree(SNAPSHOT, SERVER / "mods")
    record = SNAPSHOT.parent / "packwiz.json"
    if record.exists():
        shutil.copy2(record, SERVER / "packwiz.json")
    else:
        (SERVER / "packwiz.json").unlink(missing_ok=True)
    if not LAUNCHER_SNAPSHOT.exists():  # snapshot from before launchers were kept
        log("Restored mods/ from the snapshot.")
        return
    for f in LAUNCHER_SNAPSHOT.iterdir():
        if f.name == LAUNCHER.name:
            shutil.copy2(f, LAUNCHER)
        else:  # the Minecraft server jar
            server_jar("").parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, server_jar("").parent / f.name)
    have = launcher_versions() or ("?", "?")
    log(f"Restored mods/ and the launcher (Minecraft {have[0]}, Fabric {have[1]}) from the snapshot.")


def sync_mods():
    uri = (ROOT / "pack" / "pack.toml").as_uri()
    r = subprocess.run(["java", "-jar", str(BOOTSTRAP), "--bootstrap-no-update", "-g", "-s", "server", uri],
                       cwd=SERVER, capture_output=True, text=True)
    for line in r.stdout.splitlines():
        if line.strip():
            log("  " + line)
    if r.returncode:
        raise DeployError(f"packwiz-installer failed (exit {r.returncode}): {r.stderr.strip()[-500:]}")


def mod_list():
    return sorted(p.name for p in (SERVER / "mods").glob("*.jar"))


def stop_if_running():
    if not supervisor_up():
        return False  # Stop closes the supervisor, so the server is down
    st = supervisor("GET", "/status")
    if st.get("busy") or st.get("backing_up"):
        raise DeployError("The server is busy (stopping or backing up); try again shortly.")
    if not st["running"]:
        return False
    log("Stopping the server (it saves and backs up first)...")
    log(supervisor("POST", "/mc/stop?wait=1&who=deploy", timeout=900)["message"])
    return True


def deploy(a):
    was_running = False
    if a.rollback:
        HOLD.touch()
        try:
            was_running = stop_if_running()
            restore()
        finally:
            HOLD.unlink(missing_ok=True)
        ok = start_server() if (was_running or a.start) else True
        return ok, "Rolled back to the mods and launcher from before the last deploy."

    if not a.no_pull:
        pull()
    HOLD.touch()  # the supervisor refuses starts (Steve, dashboard) while we swap files
    try:
        was_running = stop_if_running()
        snapshot()
        before = set(mod_list())
        launcher_change = None
        try:
            launcher_change = sync_launcher()
            log("Syncing mods from pack/...")
            sync_mods()
            sync_error = None
        except DeployError as e:
            restore()
            sync_error = e
        after = set(mod_list())
    finally:
        HOLD.unlink(missing_ok=True)
    if sync_error:
        if was_running:
            start_server()  # back up on the old mods
        raise sync_error

    changes = [f"+{m}" for m in sorted(after - before)] + [f"-{m}" for m in sorted(before - after)]
    log("Mod changes: " + (", ".join(changes) or "none"))
    if launcher_change:
        log("Launcher: " + launcher_change)
        changes.append(launcher_change)
    if not (was_running or a.start):
        return True, f"Synced ({len(changes)} change(s)); server left stopped."
    if start_server():
        return True, f"Deployed ({len(changes)} change(s))."

    log("Rolling back to the previous mods...")
    HOLD.touch()
    try:
        restore()
    finally:
        HOLD.unlink(missing_ok=True)
    if start_server():
        return False, "New mods failed to start; rolled back and the server is running on the old ones."
    return False, "New mods failed to start, and the server also failed on the old ones. Check server/logs."


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", action="store_true", help="start the server even if it was stopped")
    ap.add_argument("--no-pull", action="store_true", help="don't git pull first")
    ap.add_argument("--rollback", action="store_true", help="restore the mods and launcher from before the last deploy")
    a = ap.parse_args()

    STATE.mkdir(exist_ok=True)
    try:
        fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        sys.exit(f"A deploy is already running (remove {LOCK} if it crashed).")
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)

    started = time.time()
    try:
        ok, message = deploy(a)
    except DeployError as e:
        ok, message = False, str(e)
    except Exception as e:  # still record a result for the dashboard
        ok, message = False, f"Deploy crashed: {e!r}"
    finally:
        LOCK.unlink(missing_ok=True)

    try:
        commit = git("log", "-1", "--format=%h %s")
    except DeployError:
        commit = None
    RESULT.write_text(json.dumps({
        "ok": ok, "message": message, "commit": commit, "started": started,
        "finished": time.time(), "mods": mod_list(),
    }, indent=2), encoding="utf-8")
    log(("OK: " if ok else "FAILED: ") + message)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
