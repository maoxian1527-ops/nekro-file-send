"""文件发送（跨机器）—— 让云端 NekoAgent 把文件发到本机 NapCat。

解决什么问题
------------
当 NekoAgent 跑在云服务器、NapCat 跑在你自己的电脑上（很常见的部署方式）时，
**NekoAgent 内置的 `send_file` 必然失败**：

    识别URL失败, uri=/app/nekro_agent_data/uploads/xxx/report.xlsx

原因：适配器把「容器内的本地路径」直接交给 NapCat，而 NapCat 在另一台机器上，
那个路径根本不存在；同时 NapCat 又把它当 URL 解析，于是报「识别URL失败」。

本插件提供两条路：

1. **`send_file_safe` 沙盒方法** —— 给 AI 用。内部把文件读成字节、以 `base64://`
   形式上传（`upload_private_file` / `upload_group_file` 都支持），不再依赖路径。
   同时带**路径兜底**：AI 常把文件写在 `/app/shared/xxx` 或传错目录，插件会按
   文件名去沙盒目录树里找回来，并把映射关系记进日志。

2. **`/sendfile` 指令** —— 给人用。零 token、不经过 AI，直接列出并发送沙盒里的文件。

另外附带一个**适配器补丁**（`patches/patch_onebot_base64.sh`），把 NekoAgent 内置
`send_file` 也改成 base64 直传 —— 这样即便 AI 用了内置方法也能成功。

> 适用于 NekroAgent 2.x，NapCat / OneBot v11 适配器。
"""

import base64
import re
from pathlib import Path
from typing import Annotated, Any, Dict, Optional, Tuple

from fastapi import APIRouter
from pydantic import Field

from nekro_agent.api.plugin import ConfigBase, NekroPlugin, SandboxMethodType
from nekro_agent.api.schemas import AgentCtx
from nekro_agent.services.command.base import CommandPermission
from nekro_agent.services.command.ctl import CmdCtl
from nekro_agent.services.command.schemas import Arg, CommandExecutionContext, CommandResponse

plugin = NekroPlugin(
    name="文件发送（跨机器）",
    module_name="file_send",
    description=(
        "在「云端 NekoAgent + 本地 NapCat」部署下安全地发送文件："
        "内置 send_file 会因路径不可达而失败，本插件改用 base64 直传，并提供 /sendfile 手动发送。"
    ),
    version="0.1.0",
    author="maoxian1527-ops",
    url="https://github.com/maoxian1527-ops/nekro-file-send",
    support_adapter=["onebot_v11"],
    allow_sleep=False,
)


@plugin.mount_config()
class FileSendConfig(ConfigBase):
    """发送设置。"""

    enabled: bool = Field(
        default=True,
        title="启用文件发送",
        description="关闭后沙盒方法与 /sendfile 都会拒绝工作。",
    )
    max_file_mb: int = Field(
        default=8,
        ge=1,
        le=50,
        title="单文件大小上限（MB）",
        description="base64 直传的现实上限；超过则拒绝发送并提示。",
    )
    allow_ai_send: bool = Field(
        default=True,
        title="允许 AI 调用发送方法",
        description="关闭后只有 /sendfile 指令能发文件。",
    )
    allow_command_send: bool = Field(
        default=True,
        title="允许使用 /sendfile 指令",
        description="关闭后指令不可用。",
    )
    data_root: str = Field(
        default="/home/ubuntu/srv/nekro_agent",
        title="NekoAgent 数据目录（宿主路径）",
        description=(
            "DATA_DIR 在宿主机上的绝对路径。用于把沙盒内的 /app/nekro_agent_data、"
            "/app/shared 映射回宿主真实路径。如果你不是用官方 docker-compose 部署，请改成自己的路径。"
        ),
    )
    sandbox_root: str = Field(
        default="/home/ubuntu/srv/nekro_agent/sandboxes",
        title="沙盒共享目录（宿主路径）",
        description="每个会话的 /app/shared 会挂载到 <此目录>/<container_key>/，用于按文件名兜底查找。",
    )


# ---------------------------------------------------------------- 会话与目标


def _parse_chat(chat_key: str) -> Optional[Tuple[str, int]]:
    match = re.search(r"onebot_v11-(group|private)_(\d+)$", str(chat_key or ""))
    if not match:
        return None
    return match.group(1), int(match.group(2))


def _onebot_target(ctx: "AgentCtx") -> Tuple[str, int]:
    parsed = _parse_chat(str(getattr(ctx, "chat_key", "") or ""))
    return parsed if parsed else ("private", 0)


async def _upload(data: bytes, chat_key: str, name: str) -> None:
    """以 base64 形式上传统一入口（跨机器可用）。"""
    from nonebot import get_bot

    parsed = _parse_chat(chat_key)
    if not parsed:
        raise RuntimeError(f"无法解析会话标识: {chat_key}")
    kind, target = parsed
    bot = get_bot()
    data_uri = "base64://" + base64.b64encode(data).decode("ascii")
    if kind == "group":
        await bot.call_api("upload_group_file", group_id=target, file=data_uri, name=name)
    else:
        await bot.call_api("upload_private_file", user_id=target, file=data_uri, name=name)


# ---------------------------------------------------------------- 路径解析


def _sandbox_dir(container_key: str) -> Path:
    root = str(getattr(plugin.get_config(), "sandbox_root", "") or "").strip()
    return Path(root) / container_key


def _resolve_host_path(raw: str, container_key: str) -> Optional[Path]:
    """把 AI 给的路径（可能是容器路径、相对路径、甚至错的目录）解析成宿主真实文件。"""
    cfg = plugin.get_config()
    data_root = str(cfg.data_root or "").strip().rstrip("/")
    sandbox_root = str(cfg.sandbox_root or "").strip().rstrip("/")
    raw = str(raw or "").strip()
    if not raw:
        return None

    candidates = []
    # ① 沙盒内的 /app/shared → <sandbox_root>/<container_key>/
    if raw.startswith("/app/shared/"):
        candidates.append(f"{sandbox_root}/{container_key}/" + raw[len("/app/shared/"):])
    # ② 沙盒内的 /app/nekro_agent_data → <data_root>/
    if raw.startswith("/app/nekro_agent_data/"):
        candidates.append(f"{data_root}/" + raw[len("/app/nekro_agent_data/"):])
    # ③ 本来就是宿主路径
    if raw.startswith(data_root):
        candidates.append(raw)
    # ④ 相对路径（沙盒 cwd 是 /app，./shared/x 等价 /app/shared/x）
    if not raw.startswith("/"):
        rel = raw[2:] if raw.startswith("./") else raw
        if rel.startswith("shared/"):
            rel = rel[len("shared/"):]
        candidates.append(f"{sandbox_root}/{container_key}/{rel}")

    for item in candidates:
        path = Path(item)
        if path.is_file():
            return path

    # ⑤ 兜底：按文件名在沙盒目录树里找（AI 经常传错目录但文件名是对的）
    name = Path(raw).name
    if not name:
        return None
    for root in (Path(sandbox_root) / container_key, Path(sandbox_root)):
        if not root.is_dir():
            continue
        for hit in root.rglob(name):
            if hit.is_file():
                plugin.logger.info(f"[file_send] 路径纠正：{raw} → {hit}")
                return hit
    return None


# ---------------------------------------------------------------- 沙盒方法


@plugin.mount_sandbox_method(
    SandboxMethodType.BEHAVIOR,
    name="发送文件给用户",
    description=(
        "【发文件的唯一正确方式】把沙盒里生成的文件发送给用户。"
        "本服务器是「云端 NekoAgent + 本地 NapCat」的跨机器部署，内置 send_file 会失败"
        "（报「识别URL失败」）。只要用户要求发文件，一律调用本方法。"
    ),
)
async def send_file_safe(_ctx: "AgentCtx", file_path: str, display_name: str = "") -> str:
    """把沙盒里的文件发送到当前聊天（base64 直传，跨机器可用）。

    什么时候用：用户要你把生成的文件 / 报告 / 表格 / 文本发给他时。

    Args:
        file_path: 文件路径。写文件时建议放在 `/app/shared/` 下（该目录已挂载到宿主，最稳）。
        display_name: 可选，发送时显示的文件名（留空则用原文件名）。
    """
    cfg = plugin.get_config()
    if not cfg.enabled or not cfg.allow_ai_send:
        return "文件发送功能已关闭。"

    raw = str(file_path or "").strip()
    chat_key = str(getattr(_ctx, "chat_key", "") or "")
    container_key = str(getattr(_ctx, "container_key", "") or "")
    plugin.logger.info(f"[file_send] 收到路径: {raw} | container_key={container_key}")

    host_path = _resolve_host_path(raw, container_key)
    if host_path is None or not host_path.is_file():
        return (
            f"文件不存在：{raw}\n"
            "（建议把文件写到 /app/shared/ 下再发送；该目录已挂载到宿主机。）"
        )

    size = host_path.stat().st_size
    limit = int(cfg.max_file_mb) * 1024 * 1024
    if size > limit:
        return f"文件太大（{round(size/1024/1024,1)} MB），超过 {cfg.max_file_mb} MB 上限。"

    name = str(display_name or "").strip() or host_path.name
    if not Path(name).suffix:
        name = name + host_path.suffix
    try:
        await _upload(host_path.read_bytes(), chat_key, name)
    except Exception as error:  # noqa: BLE001
        plugin.logger.exception(f"[file_send] 发送失败: {type(error).__name__}: {error}")
        return f"发送文件失败：{error}"
    plugin.logger.info(f"[file_send] ✅ 已发送 {name}（{round(size/1024,1)} KB）")
    return f"已发送文件 {name}（{round(size/1024,1)} KB）。"


# ---------------------------------------------------------------- 指令


@plugin.mount_command(
    name="sendfile",
    description="把沙盒里生成的文件发送到当前会话（零 token，不经过 AI）",
    permission=CommandPermission.PUBLIC,
    usage="/sendfile            列出可发送的文件\n/sendfile 文件名      发送该文件",
    category="工具",
    tags=["文件", "file", "发送"],
)
async def sendfile_command(
    context: "CommandExecutionContext",
    name: Annotated[str, Arg("文件名，留空则列出可发送文件", positional=True)] = "",
) -> "CommandResponse":
    cfg = plugin.get_config()
    if not cfg.enabled or not cfg.allow_command_send:
        return CmdCtl.failed("文件发送功能已关闭。")

    chat_key = str(getattr(context, "chat_key", "") or "")
    parsed = _parse_chat(chat_key)
    if not parsed:
        return CmdCtl.failed("无法识别当前会话。")
    kind, target = parsed
    container_key = f"sandbox_onebot_v11-{kind}_{target}"
    folder = _sandbox_dir(container_key)
    if not folder.is_dir():
        return CmdCtl.failed("找不到本会话的沙盒目录（还没有生成过文件）。")

    wanted = (name or "").strip()
    files = sorted(
        [f for f in folder.iterdir() if f.is_file() and f.suffix not in (".code", ".py")],
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    if not wanted:
        if not files:
            return CmdCtl.success("这个会话的沙盒里还没有可发送的文件。")
        lines = [f"📁 可发送文件（{len(files)} 个）："]
        for item in files[:15]:
            lines.append(f"• {item.name}（{round(item.stat().st_size/1024,1)} KB）")
        lines.append("\n发送方式：/sendfile 文件名")
        return CmdCtl.success("\n".join(lines))

    target_path = folder / wanted
    if not target_path.is_file():
        matches = [f for f in files if wanted in f.name]
        if len(matches) == 1:
            target_path = matches[0]
        elif len(matches) > 1:
            return CmdCtl.failed("匹配到多个文件：" + "、".join(m.name for m in matches[:5]))
        else:
            return CmdCtl.failed(f"没有找到文件：{wanted}\n（发 /sendfile 可查看列表）")

    size = target_path.stat().st_size
    if size > int(cfg.max_file_mb) * 1024 * 1024:
        return CmdCtl.failed(f"文件太大（{round(size/1024/1024,1)} MB），超过 {cfg.max_file_mb} MB 上限。")
    try:
        await _upload(target_path.read_bytes(), chat_key, target_path.name)
    except Exception as error:  # noqa: BLE001
        plugin.logger.exception(f"[file_send] /sendfile 失败: {error}")
        return CmdCtl.failed(f"发送失败：{error}")
    return CmdCtl.success(f"✅ 已发送 {target_path.name}（{round(size/1024,1)} KB）")


# ---------------------------------------------------------------- 自检


@plugin.mount_router()
def create_router() -> APIRouter:
    router = APIRouter()

    @router.get("/status")
    async def status():
        cfg = plugin.get_config()
        sandbox_root = Path(str(cfg.sandbox_root or ""))
        sessions: Dict[str, Any] = {}
        if sandbox_root.is_dir():
            for folder in sorted(sandbox_root.glob("sandbox_onebot_v11-*")):
                items = [
                    f.name
                    for f in folder.iterdir()
                    if f.is_file() and f.suffix not in (".code", ".py")
                ]
                if items:
                    sessions[folder.name] = items[:10]
        return {
            "ok": True,
            "enabled": cfg.enabled,
            "max_file_mb": cfg.max_file_mb,
            "data_root": cfg.data_root,
            "sandbox_root": cfg.sandbox_root,
            "sessions_with_files": sessions,
        }

    return router
