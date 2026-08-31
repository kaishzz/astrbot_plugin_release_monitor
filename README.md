# astrbot_plugin_release_monitor

监控 GitHub.com 和 GitLab.com 的公开仓库 Commit、Release、Pre-release，并通过 Gotify 发送简短通知。

每个仓库可以独立勾选需要监控的事件。Commit 只监控指定分支的最新一次变化；通知不转发完整更新说明或 Commit message。

## 配置

- `repositories`：仓库列表。每个仓库包含 `platform`、`repository`、`branch` 和三个监控开关。
- `platform`：`github` 或 `gitlab`，仅支持 GitHub.com/GitLab.com 公开仓库。
- `repository`：填写 `owner/repo` 或对应平台的仓库地址。
- `branch`：Commit 监控分支，留空使用平台默认分支。
- `monitor_commit`：是否监控最新 Commit，默认关闭。
- `monitor_release`：是否监控正式 Release，默认关闭。
- `monitor_prerelease`：是否监控 Pre-release，默认关闭。
- `github_token`：可选的 GitHub Token，用于提高 API 请求额度；GitLab 公开仓库无需 Token。
- `check_interval_minutes`：检查间隔，默认 30 分钟。
- `notify_on_first_run`：首次运行是否通知当前状态，默认关闭。
- `gotify_channels`：Gotify 渠道列表，每个渠道包含 `name`、`url`、`token`、`priority`。

仓库配置示例：

```json
[
  {
    "platform": "github",
    "repository": "owner/commit-only-project",
    "branch": "main",
    "monitor_commit": true,
    "monitor_release": false,
    "monitor_prerelease": false
  },
  {
    "platform": "github",
    "repository": "owner/stable-project",
    "monitor_release": true,
    "monitor_prerelease": true
  },
  {
    "platform": "gitlab",
    "repository": "group/project",
    "monitor_commit": true,
    "monitor_release": true,
    "monitor_prerelease": true
  }
]
```

GitLab Releases API 没有 GitHub 式的原生 Pre-release 字段。本插件根据 tag 名称后缀识别预发布版本：`-alpha`、`-beta`、`-rc`、`-dev`、`-nightly`、`-preview`、`-pre`，以及这些后缀后面的点号或数字。例如 `v1.2.0-rc.1` 会被识别为 Pre-release；不符合命名约定的 GitLab 预发布 tag 会被视为正式 Release。GitLab Upcoming Release 会被忽略。

## 管理命令

- `/release_check`：立即检查所有启用的事件
- `/release_list`：查看监控平台、仓库、分支、启用事件及当前 Commit/Release/Pre-release 状态
- `/release_status`：查看运行状态、平台数量和各事件监控数量

## 持久化

状态保存到 AstrBot 数据目录：

```text
data/plugin_data/astrbot_plugin_release_monitor/release_state.json
```

文件使用临时文件加原子替换写入，记录每个仓库各类事件最近处理的 Commit、Release 和 Pre-release。
