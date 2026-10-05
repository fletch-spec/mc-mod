"""Manage the packwiz pack in ../pack without the packwiz CLI (stdlib only).

The pack is the single list of mods for the server AND clients: the laptop
server syncs from it on deploy, and Prism Launcher syncs your desktop client
from the same pack.toml (see README). Files are standard packwiz format.

    python pack.py list
    python pack.py add modrinth <slug-or-id> [--side both|client|server]
    python pack.py add github <owner/repo> [--side ...]   latest release's jar
    python pack.py add url <url> --name <name> [--side ...]
    python pack.py update [<name> ...]                     all updatable mods if none given
    python pack.py remove <name>
    python pack.py refresh                                 rehash index.toml and pack.toml

Every command that changes mods refreshes the hashes, so commit right after.
Private GitHub repos need GITHUB_TOKEN set, and clients can't download from
them, so keep mod repos public.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import tomllib
import urllib.parse
import urllib.request
from pathlib import Path

PACK = Path(__file__).resolve().parent.parent / "pack"
MODS = PACK / "mods"
UA = "minecraft-mod-pipeline/1.0"  # Modrinth asks for an identifying User-Agent


# ---------- tiny TOML writer (packwiz files only use strings, bools and tables) ----------

def _val(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    return json.dumps(str(v))  # JSON string escaping is valid TOML basic-string escaping


def dump_toml(data: dict, prefix="") -> str:
    lines = [f"{k} = {_val(v)}" for k, v in data.items() if not isinstance(v, (dict, list))]
    out = "\n".join(lines) + ("\n" if lines else "")
    for k, v in data.items():
        name = f"{prefix}{k}"
        if isinstance(v, dict):
            out += f"\n[{name}]\n" + dump_toml(v, name + ".")
        elif isinstance(v, list):  # array of tables
            for item in v:
                out += f"\n[[{name}]]\n" + dump_toml(item, name + ".")
    return out


def read_toml(path: Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


# ---------- HTTP ----------

def get(url, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=60) as res:
        return res.read()


def get_json(url, headers=None):
    return json.loads(get(url, headers))


def github_headers():
    h = {"Accept": "application/vnd.github+json"}
    if token := os.environ.get("GITHUB_TOKEN"):
        h["Authorization"] = f"Bearer {token}"
    return h


# ---------- pack ----------

def versions():
    return read_toml(PACK / "pack.toml")["versions"]


def slugify(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def write_mod(key, meta):
    MODS.mkdir(parents=True, exist_ok=True)
    (MODS / f"{key}.pw.toml").write_text(dump_toml(meta), encoding="utf-8", newline="\n")
    print(f"{key}: {meta['filename']}")


def refresh():
    files = sorted(p for p in PACK.rglob("*")
                   if p.is_file() and p.name not in ("pack.toml", "index.toml"))
    index = {"hash-format": "sha256", "files": []}
    for p in files:
        entry = {"file": p.relative_to(PACK).as_posix(), "hash": sha(p.read_bytes(), "sha256")}
        if p.name.endswith(".pw.toml"):
            entry["metafile"] = True
        index["files"].append(entry)
    raw = dump_toml(index).encode()
    (PACK / "index.toml").write_bytes(raw)
    pack = read_toml(PACK / "pack.toml")
    pack["index"]["hash"] = sha(raw, "sha256")
    (PACK / "pack.toml").write_text(dump_toml(pack), encoding="utf-8", newline="\n")
    print(f"Refreshed index ({len(files)} files).")


def sha(data, algo):
    return hashlib.new(algo, data).hexdigest()


# ---------- sources ----------

def side_from_modrinth(project):
    c, s = project.get("client_side"), project.get("server_side")
    if s == "unsupported":
        return "client"
    if c == "unsupported":
        return "server"
    return "both"


def modrinth_meta(project_id, side=None):
    v = versions()
    project = get_json(f"https://api.modrinth.com/v2/project/{project_id}")
    q = urllib.parse.urlencode({"loaders": json.dumps(["fabric"]),
                                "game_versions": json.dumps([v["minecraft"]])})
    found = get_json(f"https://api.modrinth.com/v2/project/{project['id']}/version?{q}")
    if not found:
        sys.exit(f"{project['slug']}: no Fabric build for Minecraft {v['minecraft']}.")
    ver = found[0]  # newest first
    f = next((f for f in ver["files"] if f["primary"]), ver["files"][0])
    return project["slug"], {
        "name": project["title"],
        "filename": f["filename"],
        "side": side or side_from_modrinth(project),
        "download": {"url": f["url"], "hash-format": "sha512", "hash": f["hashes"]["sha512"]},
        "update": {"modrinth": {"mod-id": project["id"], "version": ver["id"]}},
    }


def github_meta(repo, side=None):
    rel = get_json(f"https://api.github.com/repos/{repo}/releases/latest", github_headers())
    jars = [a for a in rel["assets"] if a["name"].endswith(".jar")
            and not re.search(r"-(sources|javadoc|dev)\.jar$", a["name"])]
    if len(jars) != 1:
        sys.exit(f"{repo} {rel['tag_name']}: expected one mod jar in the release, "
                 f"found {[a['name'] for a in jars]}.")
    a = jars[0]
    data = get(a["browser_download_url"])
    # The pipeline re-reads [update.github] itself; packwiz-installer ignores it.
    return slugify(repo.split("/")[1]), {
        "name": repo.split("/")[1],
        "filename": a["name"],
        "side": side or "both",
        "download": {"url": a["browser_download_url"], "hash-format": "sha512",
                     "hash": sha(data, "sha512")},
        "update": {"github": {"slug": repo, "tag": rel["tag_name"]}},
    }


def url_meta(url, name, side=None):
    data = get(url)
    return slugify(name), {
        "name": name,
        "filename": urllib.parse.unquote(url.rsplit("/", 1)[1].split("?")[0]),
        "side": side or "both",
        "download": {"url": url, "hash-format": "sha512", "hash": sha(data, "sha512")},
    }


# ---------- commands ----------

def cmd_list(_):
    for p in sorted(MODS.glob("*.pw.toml")):
        m = read_toml(p)
        src = next(iter(m.get("update", {})), "url")
        print(f"{p.name[:-8]:<28} {m['side']:<7} {src:<9} {m['filename']}")


def cmd_add(a):
    if a.source == "modrinth":
        key, meta = modrinth_meta(a.target, a.side)
    elif a.source == "github":
        key, meta = github_meta(a.target, a.side)
    else:
        if not a.name:
            sys.exit("add url needs --name.")
        key, meta = url_meta(a.target, a.name, a.side)
    write_mod(key, meta)
    refresh()


def cmd_update(a):
    paths = [MODS / f"{n}.pw.toml" for n in a.names] or sorted(MODS.glob("*.pw.toml"))
    changed = 0
    for p in paths:
        old = read_toml(p)
        upd = old.get("update", {})
        if "modrinth" in upd:
            _, new = modrinth_meta(upd["modrinth"]["mod-id"], old["side"])
        elif "github" in upd:
            _, new = github_meta(upd["github"]["slug"], old["side"])
        else:
            continue
        if new["download"]["hash"] != old["download"]["hash"]:
            write_mod(p.name[:-8], new)
            changed += 1
    print(f"{changed} mod(s) updated.")
    refresh()


def cmd_remove(a):
    p = MODS / f"{a.name}.pw.toml"
    if not p.exists():
        sys.exit(f"No mod '{a.name}'. See: python pack.py list")
    p.unlink()
    refresh()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list").set_defaults(fn=cmd_list)
    p = sub.add_parser("add")
    p.add_argument("source", choices=["modrinth", "github", "url"])
    p.add_argument("target")
    p.add_argument("--name")
    p.add_argument("--side", choices=["both", "client", "server"])
    p.set_defaults(fn=cmd_add)
    p = sub.add_parser("update")
    p.add_argument("names", nargs="*")
    p.set_defaults(fn=cmd_update)
    p = sub.add_parser("remove")
    p.add_argument("name")
    p.set_defaults(fn=cmd_remove)
    sub.add_parser("refresh").set_defaults(fn=lambda _: refresh())
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
