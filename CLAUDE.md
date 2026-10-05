# minecraft-mod

Modded Fabric 26.2 server on the laptop (Java 25). See README.md for the full pipeline.

- Mods are listed only in `pack/` (packwiz format). Change them with `python pipeline/pack.py ...`, never by copying jars into `server/mods`. Commit after every pack change.
- Deploy with `python pipeline/deploy.py`. Never start, stop or kill Java yourself. Server control goes through the supervisor API on 127.0.0.1:47803. Its token is in `C:\discord\control_token.txt`; never print it.
- `deploy.py` is the only thing that swaps mod files. If it fails, read `state/deploy.log` and `server/logs/latest.log` before changing anything. `--rollback` restores the previous mods.
- Mod source code lives in separate GitHub repos, each releasing jars with `templates/mod-release.yml`. Don't build mods on this laptop.
- The vanilla server (`C:\minecraft`, supervisor on 47801) is a separate thing. Leave it alone unless asked.
