# minecraft-mod

A modded **Fabric 26.3** server (Java 25), hosted on the laptop. You develop mods on the desktop. This repo is the single source of truth for which mods the server **and** clients run.

```
desktop: mod repo ──tag v1.2.0──► GitHub Actions ──► GitHub release (jar)
desktop: python pipeline/pack.py update my-mod ; git commit ; git push
laptop:  Deploy (wallpaper button / Claude / python pipeline/deploy.py)
           git pull ─► hold starts ─► supervisor stop (save + backup) ─► snapshot mods/ + launcher
           ─► launcher matched to pack.toml's versions ─► packwiz-installer syncs server/mods from pack/
           ─► start ─► wait for "Running"
           └─ server doesn't come up? restore snapshot and start on the old mods and launcher
desktop client: Prism Launcher syncs the same pack before every launch
```

## Layout

| Path | |
|---|---|
| `pack/` | packwiz pack: `pack.toml` (MC + loader versions, which the server follows too) and `mods/*.pw.toml` (one per mod) |
| `pipeline/pack.py` | add, update and remove mods. Stdlib Python only, so the packwiz CLI isn't needed |
| `pipeline/deploy.py` | deploy to the laptop server (`--start`, `--no-pull`, `--rollback`) |
| `supervisor.json` | the second `C:\discord\mc_supervisor.py` instance: API `127.0.0.1:47803`, backups to `D:\mc-backups\minecraft-mod` |
| `server/` | the server; only `server.properties` and `config/` are in git |
| `state/` | deploy result, log and rollback snapshot (not in git) |
| `templates/mod-release.yml` | GitHub Actions release workflow to copy into each mod repo |

## Managing mods (on either machine)

```
python pipeline/pack.py list
python pipeline/pack.py add modrinth sodium          # external mods, by Modrinth slug
python pipeline/pack.py add github fletch-spec/my-mod   # your own, latest GitHub release
python pipeline/pack.py update                       # bump everything to the newest build for pack.toml's MC version
python pipeline/pack.py remove sodium
```

`side` is set from Modrinth's metadata. Client-only mods (shaders, minimaps) are kept off the server automatically.

## Server

- **Port 25566**, so it can run alongside the vanilla server on 25565.
- RAM is 2 to 6 GB; change `-Xmx` in `supervisor.json`.
- **Start and stop** happen only through the supervisor: the wallpaper card, `POST /mc/start` or `/mc/stop` on 47803, or the supervisor console. Never run Java directly.
- **Supervisor task:** "Minecraft Supervisor (Mod)" starts at logon.

## Changing the Minecraft or Fabric version

1. Make a fresh world backup first (`POST /mc/backup?force=1` on 47803): a world opened by a newer Minecraft can't go back.
2. Set `[versions] minecraft` / `fabric` in `pack/pack.toml`, then `python pipeline/pack.py refresh` and `python pipeline/pack.py update`. If a mod has no build for the new version, `update` says so; don't deploy until it has one.
3. Commit, push, and deploy.

While the server is stopped, `deploy.py` swaps `server/fabric-server-launch.jar` for Fabric's launcher for the new versions (from `meta.fabricmc.net`). The Minecraft server jar it needs (`server/.fabric/server/<mc>-server.jar`) is copied from the vanilla server's `C:\minecraft\server.jar` when that's the same version (the vanilla server is only read, never changed); otherwise the launcher downloads it on first start. The old launcher and server jar are snapshotted with the mods, so `--rollback` puts them back, but the world will already have been upgraded, so going back a Minecraft version also means restoring the world backup.

## Desktop client sync (Prism Launcher)

1. Create an instance for **26.3 + Fabric 0.19.5**.
2. Put `packwiz-installer-bootstrap.jar` (from `tools/`) in the instance's `.minecraft` folder.
3. Set the instance's pre-launch command to `"$INST_JAVA" -jar packwiz-installer-bootstrap.jar https://raw.githubusercontent.com/fletch-spec/mc-mod/main/pack/pack.toml`.

Each launch then pulls exactly the server's mods, plus the client-only ones. This needs the repo (and your mod repos) to be **public**, because the installer can't log in to GitHub.
