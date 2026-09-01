# astrbot_plugin_release_monitor

监控 GitHub.com 和 GitLab.com 的公开仓库 Commit、Release、Pre-release, 并通过 Gotify 发送通知

## 支持的事件

- Commit: 监控指定分支的最新提交
- Release: 监控最新正式版本
- Pre-release: 监控最新预发布版本

每个仓库可以独立启用三类事件。留空 `branch` 时使用平台默认分支

## 配置

- `repositories`: 仓库配置列表
- `platform`: `github` 或 `gitlab`
- `repository`: `owner/project` 或 GitHub.com/GitLab.com 仓库地址
- `branch`: Commit 监控分支, 留空使用平台默认分支
- `monitor_commit`: 是否监控 Commit, 默认关闭
- `monitor_release`: 是否监控正式 Release, 默认关闭
- `monitor_prerelease`: 是否监控 Pre-release, 默认关闭
- `github_token`: 可选的 GitHub Token, 用于提高 API 请求额度
- `check_interval_minutes`: 定时检查间隔, 默认 30 分钟
- `notify_on_first_run`: 首次检查是否通知当前状态, 默认关闭
- `gotify_channels`: Gotify 通知渠道列表, 每个渠道包含 `name`、`url`、`token`、`priority`

配置示例:

```json
{
  "repositories": [
    {
      "platform": "github",
      "repository": "owner/project",
      "branch": "main",
      "monitor_commit": true,
      "monitor_release": true,
      "monitor_prerelease": true
    },
    {
      "platform": "gitlab",
      "repository": "group/project",
      "branch": "",
      "monitor_commit": true,
      "monitor_release": true,
      "monitor_prerelease": true
    }
  ],
  "github_token": "",
  "check_interval_minutes": 30,
  "notify_on_first_run": false,
  "gotify_channels": [
    {
      "name": "默认渠道",
      "url": "https://gotify.example.com",
      "token": "应用 Token",
      "priority": 5
    }
  ]
}
```

## 管理命令

- `/release_check`: 立即检查所有已启用事件
- `/release_list`: 查看监控平台、仓库、分支、事件开关和最近事件版本
- `/release_status`: 查看运行状态、仓库数量、平台数量、事件数量和 Gotify 渠道数量

三个命令仅限管理员使用

## Gotify 通知

通知包含平台、仓库、事件类型、分支、版本或 Commit SHA、标题、作者、时间和链接。每个事件会分别记录 Commit、正式 Release 和 Pre-release, 避免重复通知

## GitLab Pre-release 规则

GitLab Releases API 没有原生 Pre-release 字段。本插件按 tag 名称末尾的 `-alpha`、`-beta`、`-rc`、`-dev`、`-nightly`、`-preview` 或 `-pre` 识别 Pre-release, 后缀后可带点号和数字。例如 `v1.2.0-rc.1` 会被识别为 Pre-release。GitLab Upcoming Release 会被忽略

## 状态文件

状态保存到 AstrBot 数据目录:

```text
data/plugin_data/astrbot_plugin_release_monitor/release_state.json
```
