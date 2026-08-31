# GitHub/GitLab 多事件监控扩展实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将插件从仅监控 GitHub Release 扩展为支持 GitHub.com 与 GitLab.com 公开仓库，并允许每个仓库独立监控 Commit、正式 Release、Pre-release。

**Architecture:** 使用统一的仓库目标配置和统一事件模型，将 GitHub/GitLab API 访问封装到独立适配器中。检查引擎只处理标准化事件和按事件类型保存的状态；通知层继续复用 Gotify。GitLab API 没有 GitHub 式原生 `prerelease` 字段，因此 GitLab Release 的预发布分类使用版本标签约定识别，并明确记录该限制。

**Tech Stack:** Python 3、`requests`、AstrBot 插件 API、`asyncio`、`unittest`。

**Spec:** 本计划即为本次已确认的设计规格；范围限定为 GitHub.com/GitLab.com 公开仓库，不加入自建 GitLab、Webhook、Commit 历史批量补发或其他通知渠道。

## Global Constraints

- 仅支持 GitHub.com 和 GitLab.com 的公开仓库。
- 每个仓库的 `monitor_commit`、`monitor_release`、`monitor_prerelease` 均为独立布尔开关，新配置默认全部关闭。
- `branch` 为空时使用平台 API 返回的默认分支；Commit 只通知指定分支最新一次变化。
- 首次运行只记录各事件当前状态，除非配置 `notify_on_first_run: true`。
- 正式 Release 与 Pre-release 必须按事件类型分别保存去重状态。
- 保留旧版 `repositories` 配置和 `include_prereleases` 语义：旧配置自动视为 GitHub 目标，`false` 映射为正式 Release，`true` 映射为正式 Release 加 Pre-release。
- GitLab Pre-release 使用不区分大小写的标签后缀规则：`-alpha`、`-beta`、`-rc`、`-dev`、`-nightly`、`-preview`、`-pre`，以及这些后缀后面的点号或数字；GitLab Upcoming Release 不作为 Pre-release。
- 不自动创建 Git 提交，不执行 `git push`。

---

## 文件结构与职责

| 文件 | 操作 | 职责 |
|---|---|---|
| `main.py` | 修改 | 配置读取、定时循环、统一检查流程、状态持久化、命令和 Gotify 通知编排 |
| `providers.py` | 新建 | 统一 `RepositoryTarget`、`RepositoryEvent` 类型，以及 GitHub/GitLab API 适配器 |
| `_conf_schema.json` | 修改 | 暴露平台、仓库、分支和三个监控开关 |
| `README.md` | 修改 | 更新支持范围、配置示例、GitLab Pre-release 分类规则和命令说明 |
| `tests/test_main.py` | 修改 | 覆盖配置兼容、适配器标准化、事件去重、状态迁移、命令输出 |
| `tests/test_providers.py` | 新建 | 独立测试 GitHub/GitLab API 响应解析与 URL/标签分类 |

## 统一接口

`providers.py` 提供以下可测试接口：

```python
@dataclass(frozen=True)
class RepositoryTarget:
    platform: str                 # "github" 或 "gitlab"
    repository: str               # GitHub owner/name 或 GitLab group/subgroup/project
    branch: str = ""
    monitor_commit: bool = False
    monitor_release: bool = False
    monitor_prerelease: bool = False

    @property
    def target_key(self) -> str:   # 例如 "github:owner/name"
        ...

@dataclass(frozen=True)
class RepositoryEvent:
    event_type: str                # "commit"、"release"、"prerelease"
    key: str                       # SHA 或 release id/tag/url
    title: str
    version: str
    author: str
    published_at: str
    url: str
    branch: str = ""

class RepositoryProvider(Protocol):
    def fetch_latest_commit(self, target: RepositoryTarget) -> Optional[RepositoryEvent]: ...
    def fetch_latest_release(self, target: RepositoryTarget, prerelease: bool) -> Optional[RepositoryEvent]: ...

class GitHubProvider:
    ...

class GitLabProvider:
    ...

def parse_target(item: Any, legacy_mode: bool = False) -> Optional[RepositoryTarget]: ...
def parse_targets(value: Any) -> List[RepositoryTarget]: ...
def is_gitlab_prerelease_tag(tag: str) -> bool: ...
```

### Task 1: 建立统一配置与平台适配器

**Files:**
- Create: `providers.py`
- Create: `tests/test_providers.py`

**Interfaces:**
- `main.py` 调用 `parse_targets`、`GitHubProvider`、`GitLabProvider`；`parse_target` 作为单项解析和单元测试接口。
- 适配器只返回 `RepositoryEvent` 或 `None`，不读写插件状态，不发送通知。

- [ ] **Step 1: 写适配器和配置解析失败测试**

测试必须覆盖：只接受 `github`/`gitlab`；仓库值支持 `owner/name`、GitHub 地址和 GitLab 地址；重复或非法目标被忽略；新配置的三个开关默认关闭；`parse_targets` 对旧配置映射到正式版和预发布开关。

- [ ] **Step 2: 写 GitHub 响应解析测试**

使用 `unittest.mock.patch("providers.requests.get")` 模拟：Commit 列表取第一项；Release 列表过滤 draft，并分别取最新正式版和最新 Pre-release；请求 URL 正确编码；响应 404 返回 `None`，其他 HTTP 错误抛出。

- [ ] **Step 3: 写 GitLab 响应解析测试**

模拟 GitLab `/api/v4/projects/{url_encoded_path}/repository/commits` 和 `/releases` 响应，验证项目路径整体 URL 编码、Commit 字段转换、Release `tag_name`/`released_at`/`_links.self` 转换，并验证预发布标签后缀规则及 `upcoming_release` 排除行为。

- [ ] **Step 4: 实现统一类型与标签分类**

新增不可变 dataclass；`target_key` 使用 `platform:repository`；`is_gitlab_prerelease_tag` 使用单一编译正则匹配上述后缀，避免在调用方重复判断。

- [ ] **Step 5: 实现 GitHubProvider**

使用现有 GitHub API 请求头和可选 Bearer Token：

```text
GET https://api.github.com/repos/{owner/name}/commits?sha={branch}&per_page=1
GET https://api.github.com/repos/{owner/name}/releases?per_page=20
```

分支为空时先请求仓库元数据获取默认分支，再在 Commit 请求中使用该分支；元数据请求本身不带分支参数。Release 过滤 `draft`，按 `prerelease` 参数筛选正式版或预发布版，再按 `published_at`、`created_at` 的 ISO 时间降序选择最新一项，并用 `id`、`tag_name`、`html_url` 依次生成稳定 key。

- [ ] **Step 6: 实现 GitLabProvider**

使用 GitLab.com REST API；分支为空时先请求项目元数据取得 `default_branch`，再将其传给 Commit 请求：

```text
GET https://gitlab.com/api/v4/projects/{quote(repository, safe='')}/repository/commits?per_page=1
GET https://gitlab.com/api/v4/projects/{quote(repository, safe='')}/repository/commits?ref_name={branch}&per_page=1
GET https://gitlab.com/api/v4/projects/{quote(repository, safe='')}/releases?order_by=released_at&sort=desc&per_page=20
```

Release 列表先排除 `upcoming_release`，再按 `is_gitlab_prerelease_tag(tag_name)` 分成正式版和预发布版。Commit 使用 `id`，Release 使用 `tag_name` 或 `_links.self` 生成稳定 key。

- [ ] **Step 7: 运行适配器测试**

运行：`python -m unittest tests.test_providers -v`

预期：所有配置解析、URL 构造、响应转换和 GitLab 标签分类测试通过。

### Task 2: 重构检查引擎与状态模型

**Files:**
- Modify: `main.py`
- Modify: `tests/test_main.py`

**Interfaces:**
- `ReleaseMonitorPlugin.repositories` 改为 `List[RepositoryTarget]`。
- `check_releases()` 保留原方法名以兼容 AstrBot 调用，但内部升级为检查所有事件。
- 新增 `check_events() -> List[RepositoryEvent]`，由 `check_releases()` 调用并把事件转换为原有命令可读摘要。

- [ ] **Step 1: 写状态和去重失败测试**

测试必须验证：Commit SHA 变化通知一次；正式版与预发布版使用独立 key；同一个 Release 后续检查不重复通知；一个目标的一个事件失败时仍继续检查其他事件和其他目标。

- [ ] **Step 2: 定义新状态结构并兼容旧文件**

新状态结构固定为：

```json
{
  "github:owner/repo": {
    "commit": {"key": "sha", "title": "...", "checked_at": "..."},
    "release": {"key": "id-or-tag", "tag_name": "v1.0.0", "checked_at": "..."},
    "prerelease": {"key": "id-or-tag", "tag_name": "v1.1.0-rc.1", "checked_at": "..."}
  }
}
```

`parse_targets` 读取旧的字符串列表或旧对象列表时补齐 `platform="github"`；旧对象没有任何 `monitor_*` 字段时，将 `include_prereleases=false` 映射为 `monitor_release=true`，将 `include_prereleases=true` 映射为 `monitor_release=true, monitor_prerelease=true`。新对象只读取三个显式开关，三项均缺省为 false。读取旧的 `{repo: {release_key, tag_name, ...}}` 状态时迁移到 `github:{repo}.release`，不主动发送迁移通知；保存时只写新结构。

- [ ] **Step 3: 实现按目标选择提供者**

在插件初始化时建立 `{"github": GitHubProvider(...), "gitlab": GitLabProvider()}` 映射；不在检查循环中复制平台判断。没有任何监控开关的目标不发 API 请求，但仍可在列表命令中显示。

- [ ] **Step 4: 实现事件检查和通知判定**

按目标开关依次请求 Commit、正式 Release、Pre-release。事件状态缺失时只记录；状态 key 变化时调用现有 Gotify 多渠道通知。通知成功数量保留在摘要中；无有效 Gotify 渠道时记录事件但成功数为 0，避免下次重复判断为新事件。

- [ ] **Step 5: 实现统一通知格式**

标题格式为 `GitHub Commit Update`、`GitHub Release Update`、`GitHub Pre-release Update`、`GitLab Commit Update`、`GitLab Release Update` 或 `GitLab Pre-release Update`。正文包含平台、仓库、事件类型、分支（Commit）、版本或短 SHA、作者、时间和链接；不包含完整 Release body 或 Commit message。

- [ ] **Step 6: 更新初始化、终止和并发保护**

保留现有后台轮询、`check_lock`、`state_lock`、原子状态写入和任务取消逻辑；定时循环调用统一检查方法；没有有效目标时不创建监控任务；目标有效但没有渠道时继续轮询并记录警告。

- [ ] **Step 7: 运行主流程测试**

运行：`python -m unittest tests.test_main -v`

预期：旧版状态可恢复，新版三类事件去重正确，后台任务、管理员限制和原有 Gotify 行为测试通过。

### Task 3: 更新管理命令和配置界面

**Files:**
- Modify: `_conf_schema.json`
- Modify: `main.py`
- Modify: `tests/test_main.py`

**Interfaces:**
- `/release_list` 继续保留命令名以兼容旧调用，但输出所有目标及已启用事件。
- `/release_status` 增加 GitHub/GitLab 目标数和 Commit/Release/Pre-release 开关统计。

- [ ] **Step 1: 写命令输出测试**

验证 `/release_list` 显示平台、仓库、分支、启用事件及各事件当前版本/短 SHA；验证 `/release_status` 显示目标总数、平台数量、事件开关数量、Gotify 渠道数和最后检查时间；非管理员仍返回“仅管理员可用”。

- [ ] **Step 2: 更新配置 schema**

将仓库模板字段改为：`platform`（枚举或字符串，默认 `github`）、`repository`（字符串）、`branch`（字符串，默认空）、`monitor_commit`（bool，默认 false）、`monitor_release`（bool，默认 false）、`monitor_prerelease`（bool，默认 false）。保留 `repo` 和 `include_prereleases` 的代码兼容读取，但新 schema 不再把 Release 作为默认监控项。

- [ ] **Step 3: 更新命令实现和统计**

按统一目标和事件状态生成稳定、简短的文本；未检查事件显示“尚未检查”，禁用事件不显示版本值；保留所有管理员权限判断。

- [ ] **Step 4: 运行命令测试**

运行：`python -m unittest tests.test_main -v`

预期：命令输出测试全部通过，旧命令名和权限行为未回归。

### Task 4: 更新文档、回归测试和最终验证

**Files:**
- Modify: `README.md`
- Modify: `tests/test_main.py`
- Modify: `tests/test_providers.py`

- [ ] **Step 1: 更新 README 配置示例**

同时展示 GitHub 只监控 Commit、GitHub 监控正式版与预发布、GitLab 监控三类事件的配置；说明平台、仓库、分支和三个开关的含义，并明确新配置默认全部关闭。

- [ ] **Step 2: 记录 GitLab Pre-release 限制**

明确 GitLab.com API 没有独立的 Pre-release 字段，本插件按标签后缀识别；列出支持的后缀；说明不符合约定的 GitLab 预发布标签会被视为正式 Release，用户可通过规范化 tag 命名规避误分类。

- [ ] **Step 3: 增加边界回归测试**

覆盖空仓库、全部开关关闭、无 Release、只有 Pre-release、GitLab Upcoming Release、请求 404、单目标失败不影响其他目标、旧状态文件恢复和临时文件清理。

- [ ] **Step 4: 运行完整测试套件**

运行：`python -m unittest discover -s tests -v`

预期：所有测试通过，退出码为 0。

- [ ] **Step 5: 做静态检查与差异核对**

运行：`python -m py_compile main.py providers.py`、`git diff --check`、`git status --short`。确认没有调试输出、没有意外修改现有用户文件、没有提交或推送操作。

## 自审结果

- **需求覆盖：** 平台支持由 Task 1 覆盖；三类事件、首次运行、去重和旧状态迁移由 Task 2 覆盖；三个可勾选配置和命令展示由 Task 3 覆盖；GitLab Pre-release 限制、文档和边界验证由 Task 4 覆盖。
- **占位符检查：** 未使用 TBD、TODO 或“后续补充”；每个实现步骤均给出文件、接口、API 路径或测试命令。
- **类型一致性：** `RepositoryTarget`、`RepositoryEvent`、`parse_target`、两个 Provider 和 `check_events` 的名称、参数、返回值在任务之间一致。
- **范围检查：** 未加入自建 GitLab、Webhook、批量补发历史 Commit、额外通知渠道或无明确需求的抽象。
- **关键限制：** GitLab Pre-release 是标签规则推断，不是 API 原生字段；该行为会在 README、适配器测试和边界测试中明确固定。

## 执行与审查要求

执行时按 Task 1 至 Task 4 顺序推进，每个 Task 完成后运行该 Task 的测试。全部完成后，以本计划文件作为 Spec，针对本次工作区差异分别进行 Standards 和 Spec 两个维度的代码审查；任何 P0/P1/P2/P3 问题都必须修复并重新审查，直至两个维度均无问题后再交付。只报告和修复本次任务相关问题，不执行 Git 提交或推送。
