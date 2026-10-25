# nekro-file-send

> **Cross-machine file sending** for [NekroAgent](https://github.com/KroMiose/nekro-agent):
> make files actually arrive when NekoAgent runs in the cloud and NapCat runs at home.

[中文](README.md) · English

---

## The error you probably hit

```
识别URL失败, uri= /app/nekro_agent_data/uploads/.../report.xlsx
```

**Root cause**: NekoAgent assumes NapCat runs on the same machine and hands it a
*container-local path* (`_get_onebot_path()` only rewrites the path). When NapCat runs
elsewhere that path doesn't exist, and NapCat tries to parse it as a URL → always fails.

## What this project does

1. **`send_file_safe` sandbox method** — reads the file and uploads it as `base64://`,
   path-independent. Includes a path resolver with a *filename search fallback* so AI
   mistakes like writing to `/app/shared/` or passing a wrong directory still work.
2. **`/sendfile` command** — zero-token, no AI involved: list and send files from the sandbox.
3. **Optional adapter patch** (`patches/patch_onebot_base64.sh`) — make the built-in
   `send_file` use base64 too. Idempotent; re-run after container rebuilds.

## Install

1. Copy `plugin/` to `data/plugins/workdir/file_send/`
2. Add `maoxian1527-ops.file_send` to `PLUGIN_ENABLED`
3. Adjust `data_root` / `sandbox_root` for your deployment
4. `docker restart nekro_agent`

## License

**PolyForm Noncommercial 1.0.0** — free to use/modify/share, **commercial use prohibited**.