# nekro-file-send

> 给 [NekroAgent](https://github.com/KroMiose/nekro-agent) 的**跨机器文件发送**方案：
> 「云端 NekoAgent + 本地 NapCat」部署下，让文件真正发得出去。

中文 · [English](README.en.md)

---

## 你大概遇到过这个错误

```
识别URL失败, uri= /app/nekro_agent_data/uploads/.../report.xlsx
```

或者：

```
发送文件失败：文件 "shared/report.xlsx" 不在合法的沙盒内容共享目录或上传目录下
```

**根因**：NekoAgent 默认假设 NapCat 和它跑在同一台机器上，于是把「容器内的本地路径」
直接交给 NapCat：`adapter.py` 里的 `_get_onebot_path()` 只做路径改写，不做传输。
当 NapCat 在另一台机器上时，那个路径不存在，NapCat 又把它当 URL 解析 → 必然失败。

## 本项目的两条路

### 1. 给 AI 用：`send_file_safe` 沙盒方法

AI 生成文件后调用它，内部把文件读成字节，用 **`base64://`** 上传
（`upload_private_file` / `upload_group_file` 都支持这种形式），**完全不依赖路径**。

还带**路径兜底**：AI 常把文件写到 `/app/shared/` 或传错目录，插件会：

| 顺序 | 策略 |
|---|---|
| ① | `/app/shared/xxx` → `<sandbox_root>/<container_key>/xxx` |
| ② | `/app/nekro_agent_data/xxx` → `<data_root>/xxx` |
| ③ | 已经是宿主路径 → 直读 |
| ④ | 相对路径（`./shared/x`）→ 自动补全 |
| ⑤ | **按文件名在沙盒目录树里搜** ← AI 传错目录也能救回来 |

### 2. 给人用：`/sendfile` 指令

零 token、**不经过 AI**，直接列出并发送沙盒里的文件：

```
/sendfile              → 列出可发送文件
/sendfile report.xlsx  → 发送该文件
```

### 3. 可选：让内置 `send_file` 也能用

`patches/patch_onebot_base64.sh` 把 NekoAgent 适配器的 `_send_files` 改成 base64 直传，
这样即便 AI 用了内置方法也能成功。

```bash
sudo bash patches/patch_onebot_base64.sh      # 幂等，可重复执行
```

> ⚠️ 容器重建 / 镜像升级后补丁会丢失，重跑一次即可。

## 安装

1. 把 `plugin/` 复制到 `data/plugins/workdir/file_send/`
2. 加进 `configs/nekro-agent.yaml` 的 `PLUGIN_ENABLED`：

   ```yaml
   PLUGIN_ENABLED:
     - maoxian1527-ops.file_send
   ```

3. 按自己的部署改配置里的 `data_root` 和 `sandbox_root`
4. `docker restart nekro_agent`
5. 自检：

   ```bash
   curl -s http://127.0.0.1:8022/plugins/maoxian1527-ops.file_send/status
   ```

## 验证

```bash
# 让 AI 生成并发送一个文件
用代码生成一个 test.txt，内容写 hello，然后发给我

# 或手动
/sendfile test.txt
```

日志里应看到：

```
[file_send] 收到路径: /app/shared/test.txt | container_key=sandbox_onebot_v11-private_123
[file_send] ✅ 已发送 test.txt（0.0 KB）
```

## 常见问题

**Q: 文件收到了但显示「文件待下载」？**
A: 私聊文件走 QQ 文件传输通道，需要在客户端点「下载」，这是 QQ 的行为，不是插件问题。

**Q: 大小限制？**
A: 默认 8MB（base64 直传的现实上限）。大文件建议传到对象存储再发链接。

**Q: NapCat 报「file not found」？**
A: 说明 NapCat 端的 base64 上传没生效，检查 NapCat 版本（建议 ≥ 4.x）和 OneBot 适配器配置。

## 许可

**PolyForm Noncommercial 1.0.0** —— 免费使用/修改/分发，**禁止商业用途**。

## 相关项目

- [nekro-media-forward](https://github.com/maoxian1527-ops/nekro-media-forward)：短视频自动合并转发
- [nekro-access-gate](https://github.com/maoxian1527-ops/nekro-access-gate)：订阅制白名单门禁
- [nekro-emotion-export](https://github.com/maoxian1527-ops/nekro-emotion-export)：零 token 表情导出
- [nekro-chat-memory](https://github.com/maoxian1527-ops/nekro-chat-memory)：滚动摘要式长期记忆