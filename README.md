# 📰 Finance NewsReader — VPS 财经新闻桌面挂件

无边框、始终置顶的桌面新闻条：通过 SSH 从你的服务器读取 `daily_news.txt`（可由定时任务自动生成），滚动显示财经资讯。

> ⚠️ 本工具需要 SSH 连接服务器。**代码里不含任何服务器信息**，首次运行会弹图形向导。

## 运行

```bash
pip install paramiko
python news_reader.py
```

首次运行弹出配置向导：

| 字段 | 说明 |
|---|---|
| 服务器地址 / SSH 端口 / 用户名 | 你的 VPS 信息 |
| 私钥文件 | 点「浏览…」选择，如 `~/.ssh/id_ed25519` |
| 远程文件 | 服务器上要读取的文件路径，如 `/root/daily_news.txt` |
| 刷新间隔 | 秒，默认 300（5 分钟） |
| 全局快捷键 | **点一下输入框，直接按组合键** |

## 配置文件

| 系统 | 路径 |
|---|---|
| Windows | `%APPDATA%\NewsReader\config.json` |
| Linux | `~/.config/NewsReader/config.json` |
| macOS | `~/Library/Application Support/NewsReader/config.json` |

```json
{
  "host": "你的服务器IP",
  "port": 22,
  "user": "你的用户名",
  "key_file": "C:\\Users\\你\\.ssh\\id_ed25519",
  "news_path": "/root/daily_news.txt",
  "refresh_sec": 300,
  "hotkey": "ctrl+shift+n"
}
```

**重配**：删掉文件重启程序即可再次弹向导。

## 修改快捷键

方式一：向导里点击快捷键框直接按键。
方式二：改配置里的 `"hotkey"`（小写、`+` 连接、主键一个字母/数字；`""` = 不启用）。

**冲突处理**：注册失败会自动降级为按键轮询，功能仍可用；彻底解决就换成罕见组合或设 `""`，改用界面上的 ⬇ 按钮。

## ⚠️ 安全提示

- 别把 `config.json` 提交到 Git
- 私钥留在 `~/.ssh/`，不要拷进项目目录
- 建议用低权限账号，不要直接用 root

## License

MIT