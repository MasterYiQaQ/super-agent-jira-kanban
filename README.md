# jira-guardian

把 JIRA 上指派给你的卡，变成一条能自己跑完的流水线：**发现增量 → 派给执行会话 → 改代码/验证/提交 → 回写评论 → 挪卡**。

零第三方依赖（只用标准库），`pip install -e` 就能用。判定逻辑全在 Python 里，调度交给宿主（DSH 看板卡的 cron 就行），所以换项目、换状态机只改 `config.json`，不碰调度卡。

```
                ┌──────────────┐   ①tick scan    ┌─────────────────┐
  JIRA ────────▶│  guardian    │────────────────▶│ workspace/prompt │
  (只读查询)     │  tick        │  ②该干啥写文件   │ /<KEY>.md        │
                └──────┬───────┘                 └────────┬────────┘
                       │ ③items(dispatch=true)            │ ④执行会话读它干活
                       ▼                                  ▼
                ┌──────────────┐                 ┌──────────────────┐
                │ 看板卡 cron   │────────────────▶│ 执行会话(改码/验证)│
                │ (DSH task)   │  创建/续跑同一卡  └────────┬─────────┘
                └──────┬───────┘                          │ ⑤写回执
                       │ ⑦发评论 + 挪卡                     ▼
                       └──────────────▶ JIRA      workspace/outbox/<KEY>.json
```

## 安装

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e D:\Code\jira-guardian
.venv\Scripts\guardian.exe --help
```

> 坑：如果环境里设了 `PIP_TARGET`（比如公司镜像把包装到公共目录），`pip install` 会装到别处，
> `import jira_guardian` 直接 ModuleNotFoundError。临时的解法是 `Remove-Item Env:PIP_TARGET` 再装。
> `guardian doctor` 会先自检这一点。

## 三步接手一个环境

```powershell
# 1. 建一个值守目录（config.json + data/ + workspace/ + knowledge/）
guardian init --jira-url https://jira.example.com --token <PAT> `
    --project PROJ --me your.name `
    --repo my-service=D:\Code\my-service `
    --repo my-ui=D:\Code\my-ui

# 2. 体检：凭据、JQL、git、仓库、worktree、LLM、模板渲染，逐项过一遍
guardian doctor

# 3. 把存量卡标成"已读"，避免一上来把历史卡全派一遍
guardian tick seed --jql "project = PROJ AND assignee = currentUser() AND resolution = Unresolved"
```

然后把 `guardian card` 的输出贴成一张看板卡（DSH：卡 → 定时运行 → cron `*/15 9-19 * * 1-5`）。
看板卡的 prompt **一开跑就冻结**，所以真正的业务逻辑写在 `guardian card` 渲染出来的模板里，改流程 = 改模板 + 新建卡。

## 判定规则（`tick scan` 唯一说了算）

| 情况 | 结果 |
|---|---|
| 水位线里没有这张卡 | `kind=new`，派单（第 1 轮） |
| 有比上次记录的更新、且是**真人**的评论 | `kind=comment`，派单（轮次 +1，评论原文进 prompt） |
| 我们自己的评论（带 `bot_mark`） | 不算新评论，不触发 |
| 状态变了但没新评论（自己挪卡 / 测试打回没留话） | `kind=status`，**不派单**，只同步水位线 |
| 上一轮还在跑（占位没释放，且没超时） | `kind=in_flight`，跳过 |
| 卡在 `done_statuses` 里 | 只同步，不派单 |
| 在 `watch.ignore_cards` 里 | 完全不看（连 `pin_cards` 也压得住） |
| 掉出 JQL 窗口但 `active=true` | 按 key 补拉，评论照样跟进 |
| 有了 `resolution` | 从跟进清单摘掉 |

卡片标题是 Bug **不等于一定改代码**。执行会话会先读卡和代码：若证明现有行为符合已确认规则，而偏差来自需求理解、验收口径或使用方式，执行会话必须写 `status=blocked` 回执并提供代码证据、差异和待确认问题；主路由仍会将该结论追加为 JIRA 评论，但不会提交、推分支或挪完成态。

设计上的两个硬边界：

- **状态变化从不单独派单**。执行会话开工第一步就是自己把卡挪到 `In Progress`、干完挪到 `Ready for Testing`；如果"状态变了就派"，每张卡都会自己触发自己两轮。
- **占位（in_flight）由主路由登记、由回执释放**。这样"派出去但会话崩了"不会无限重派，也给了 `in_flight_timeout_minutes` 一个兜底。

## 命令表

### `guardian jira`（读卡、看图、回写）

| 命令 | 作用 |
|---|---|
| `guardian jira me` | 当前账号 |
| `guardian jira today [--jql --limit]` | 按默认 JQL 列卡 |
| `guardian jira get KEY [--comments N]` | 卡面 + 评论 + 附件清单 |
| `guardian jira comments KEY` | 只听评论（标出哪些是 bot 的） |
| `guardian jira attach KEY [--out --list --force]` | 下载附件到 `workspace/attach/<KEY>/` |
| `guardian jira comment KEY --file f.md` | 发评论：自动把 `###` 压成 `**加粗**`、末尾补 `bot_mark` |
| `guardian jira delcomment KEY ID` | 删评论（发错了用） |
| `guardian jira transitions KEY` / `transition KEY --to "In Progress"` | 看/执行状态流转（幂等：已在目标状态返回 `changed:false`） |
| `guardian jira search "<jql>"` | 临时查询 |

### `guardian tick`（判定）

| 命令 | 作用 |
|---|---|
| `tick scan [--commit] [--limit-items] [--no-comments]` | 算增量；`--commit` 推进水位线（真派单必须加） |
| `tick outbox` | 列出待回写的回执 |
| `tick ack KEY` | 回执消费完：删文件 + 释放占位 |
| `tick claim KEY --session <id> --board <卡 id>` | 派单后立刻登记占位 |
| `tick release KEY` | 人工放开占位（判定卡住了用） |
| `tick seed [--jql]` | 把存量卡标成已读 |
| `tick state` | 水位线现状 |

### `guardian worktree`（给执行会话准备干净代码）

| 命令 | 作用 |
|---|---|
| `worktree prepare <仓库> <分支> [--fresh]` | `fetch --prune` + 从 `origin/HEAD` 开分支到 `workspace/repos/<仓库>` |
| `worktree status <仓库>` | 当前分支、HEAD、脏文件 |
| `worktree clean <仓库> [--delete-branch]` | 拆 worktree（可选删分支） |

`prepare` 专门治一个坑：**远端分支删了、本地 remote-tracking ref 还在**，直接 `worktree add -b KEY origin/KEY` 会落在上一轮的旧提交上，看起来"改过了"其实什么都没重做。它会：

- `git fetch --prune origin` 把死掉的 ref 清掉；
- 本地已有同名分支时，报告"比基线多 N 个提交、远端已经没有这个分支"；
- `--fresh` 时删掉本地分支并从基线重开。

### 其它

| 命令 | 作用 |
|---|---|
| `guardian vision "<问题>" <图片...>` | 用多模态看图（bug 卡常常只有 before/after 图） |
| `guardian doctor` | 环境体检，`ok/warn/fail` 逐项 |
| `guardian init ...` | 初始化值守目录 |
| `guardian card` | 打印看板卡 prompt |

全局 `--root <dir>` 指定值守目录（默认当前目录，或 `JG_ROOT`）。

## 回执契约

执行会话不自己发评论，只写文件——这样会话崩了也不会丢结论，主路由还能重试：

`workspace/outbox/<KEY>.json`

```json
{
  "key": "PROJ-1", "round": 2, "status": "ok | blocked | failed",
  "summary": "一句话结论",
  "comment": "要发到 JIRA 的评论 Markdown",
  "error": "",
  "repos": ["my-service"], "branch": "PROJ-1", "commit": "<sha>", "pushed": true,
  "files": ["src/foo.py"], "verify": "python -m compileall -q src → 通过"
}
```

主路由：`status=ok && pushed=true` → 发评论、ack、确认卡在 `status_done`；`blocked/failed` → 只发评论，不挪卡、不 release（留给人看）。

## 配置要点

- `jira.default_jql`：扫卡范围。想"只看今天新建"就带 `created >= startOfDay()`；`watch.pin_cards` 里的卡不受时间窗限制（用来钉一张回归卡）。
- `watch.ignore_cards`：点名不要的卡，优先级最高。
- `watch.status_in_progress` / `status_done` / `done_statuses`：各项目状态机名字不同，按实际填（`guardian jira transitions KEY` 能看到真名）。
- `dev.enabled=false`：只读模式，只出分析结论，不改代码不挪卡。
- `dev.repos.<名字>.src` 指**本地已有的源码目录**（不重新 clone，直接用你机器上的凭据和分支）；`verify` 是该仓库的"能证明改动可用"的命令。
- `llm.*`：看图用，OpenAI 兼容即可。**不要传 temperature**：有些网关（如 gpt-5.6-*）只接受默认值，带 `0.1` 会 400。
- 模板：放 `prompts/dev.md`、`prompts/readonly.md` 可以覆盖内置模板，`{{KEY}}` 这类占位符漏填会直接报错。

## 和 DSH 看板卡的配合

要跑完整的「定时扫描 → 自动开执行会话 → 回写」闭环，DSH 里必须已安装并启用**任务看板插件**。它提供 cron、看板卡和 `reuseSession` / rerun；本 Python 包不替代这些宿主能力。

安装（DSH desktop profile）：

```powershell
dsh plugin --profile desktop add @linxin666/dsh-client-ui-task-board
```

若 `dsh` 不在 PATH，调用 DSH 自带的 `dsh.cmd`；安装后关闭并重新打开 DSH GUI。能打开任务看板、创建卡片并在详情中看到「定时运行」即为成功。先用 `dsh plugin --profile desktop why @linxin666/dsh-client-ui-task-board` 检查已装状态，避免重复安装。完整步骤见 [setup.md](setup.md)。

没有任务看板时，`guardian jira` / `tick` / `worktree` / `vision` 依旧能在命令行使用；只需用其他调度器和会话执行器替代下文的看板操作。

看板卡（cron 唤醒）的 prompt 只写六步：`tick scan --commit` → `tick outbox` → 发评论+ack → 校验收口状态 → 建/续跑执行卡 + `tick claim` → 只回一行。

要点：

- **一张 JIRA 卡固定一张看板卡**，`reuseSession: true` + `task_board_run {rerun: true}` 续跑**同一个会话**（上下文不丢）。
- 卡片 prompt 是冻结的，每轮任务说明写 `workspace/prompt/<KEY>.md`，卡 prompt 只写一句"先读这个文件"。
- 派完不等待；结果靠 `outbox` + `tick ack` 回到主路由。

## 已知限制

- 状态名各项目不同，`done_statuses` 配错会导致"测试打回"被当成完成态。
- 一张卡同时改多个仓库时，`branch` 只能记一个（约定同名分支，卡号）。
- `tick scan --commit` 之后如果执行会话彻底跑挂，那一轮水位线已经推进，需要人工 `tick seed` 或删掉该卡重扫。
- 没有并发保护：两个主路由同时跑会重复派单（靠看板 cron 保证只有一个）。
- 只做了 Windows + GitLab 的实测；Linux/macOS 应该能跑（路径用 `pathlib`），但没验过。

## 开发

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -v   # 14 个离线用例，不需要 JIRA
```

`tests/test_tick.py` 用 `FakeJira` 顶替真 JIRA，覆盖每条判定；注意用例里 `scan(commit=True)` 之后要显式 `settle()`（清占位）才能测下一轮，因为"占位没释放就不再派单"本身是设计意图。
