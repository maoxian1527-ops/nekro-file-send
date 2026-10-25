#!/bin/bash
# 给 NekroAgent 的 OneBot 适配器打补丁：发文件时改用 base64:// 直传
# 背景：跨机器部署（云端 NekoAgent + 本地 NapCat）时本地路径在 NapCat 端不存在，
#       内置 send_file 必然报「识别URL失败」。base64 直传不依赖路径。
# 注意：容器重建/镜像更新后补丁会丢失，重跑本脚本即可。
set -uo pipefail
F=/app/nekro_agent/adapters/onebot_v11/adapter.py
CID=nekro_agent
MARK="[cross-machine-base64]"

if sudo docker exec $CID grep -q "cross-machine-base64" $F 2>/dev/null; then
  echo "✅ 补丁已存在，无需重复"
  exit 0
fi

sudo docker exec $CID cp $F $F.bak-base64
sudo docker exec -i $CID python3 - "$F" <<'PY'
import pathlib, sys
path = pathlib.Path(sys.argv[1])
text = path.read_text(encoding='utf-8')
old = "            onebot_path = _get_onebot_path(Path(copied_path))"
new = (
    "            # [cross-machine-base64] 跨机器部署：本地路径在协议端不存在，改用 base64 直传\n"
    "            try:\n"
    "                import base64 as _b64\n"
    "                _raw = Path(copied_path).read_bytes()\n"
    "                if len(_raw) <= 8 * 1024 * 1024:\n"
    "                    onebot_path = 'base64://' + _b64.b64encode(_raw).decode('ascii')\n"
    "                else:\n"
    "                    onebot_path = str(_get_onebot_path(Path(copied_path)))\n"
    "            except Exception:\n"
    "                onebot_path = str(_get_onebot_path(Path(copied_path)))"
)
if old not in text:
    print('MISS: 目标行未找到')
    raise SystemExit(1)
path.write_text(text.replace(old, new, 1), encoding='utf-8')
print('PATCHED')
PY
RC=$?
if [ $RC -ne 0 ]; then echo "❌ 补丁未应用（退出码 $RC）"; exit 1; fi

echo "===== 确认写入 ====="
sudo docker exec $CID grep -c "cross-machine-base64" $F || true
echo "===== 语法检查 ====="
if sudo docker exec $CID python3 -m py_compile $F; then
  echo "   ✅ py_compile OK"
else
  echo "   ❌ 语法错误，回滚"
  sudo docker exec $CID cp $F.bak-base64 $F
  exit 1
fi
echo "===== 重启 ====="
sudo docker restart $CID >/dev/null
for i in $(seq 1 24); do sleep 5; [ "$(sudo docker inspect $CID --format '{{.State.Health.Status}}' 2>/dev/null)" = "healthy" ] && { echo "   healthy(第$i次)"; break; }; done
echo "   完成"