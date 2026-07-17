# astrbot_plugin_release_monitor

监控多个公开 GitHub 仓库的新 Release，并通过 Gotify 发送简短通知。

通知只包含仓库、版本、发布时间和 Release 链接，不转发完整更新说明。

## 配置

- `repositories`：仓库列表，例如 `owner/repo`。
- `github_token`：可选的 GitHub Token，用于提高 API 请求额度。
- `check_interval_minutes`：检查间隔，默认 30 分钟。
- `include_prereleases`：是否包含 prerelease，默认关闭。
- `notify_on_first_run`：首次运行是否通知当前版本，默认关闭。
- `gotify_channels`：Gotify 渠道列表，每个渠道包含 `name`、`url`、`token`、`priority`。

## 管理命令

- `/release_check`：立即检查所有仓库
- `/release_list`：查看监控仓库及当前版本
- `/release_status`：查看运行状态

## 持久化

状态保存到 AstrBot 数据目录：

```text
data/plugin_data/astrbot_plugin_release_monitor/release_state.json
```

文件使用临时文件加原子替换写入，记录每个仓库最近处理的 Release。
