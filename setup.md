# setup.md —— 给「另一个 agent」的安装与使用说明

这份文档写给**要接手这套值守链路的 agent**，不是写给人看的速查表。
按 Step 1→7 顺序做完，每一步都有「怎么确认成功」；后面是每天怎么跑、出问题怎么查。

## 前置条件：DSH 任务看板

**如果要使用本文的「DSH 看板卡 + cron + 一个 JIRA 卡一个执行会话」闭环，需要安装并启用任务看板插件。**

`jira-guardian` 本身不依赖任务看板：`guardian jira`、`guardian tick`、`guardian worktree` 都可以在命令行独立使用；只是它不会自己常驻、不会自己开 DSH 会话。任务看板插件提供这三件事：

1. 主路由卡的 cron 定时唤醒；
2. 为每张 JIRA 卡创建/重跑执行卡；
3. `reuseSession: true` 配合 rerun，令同一 JIRA 卡继续同一个 DSH session。

先在 DSH 里确认插件是否已经可用：

```text
打开任务看板；若能创建卡片、看到「定时运行」并设置 cron，就已经安装，无需重复装。
```

若没有任务看板，**先安装官方已验证的任务看板插件**（DSH 桌面 profile）：

```powershell
# 在 DSH 自带 CLI 可用的终端执行；普通安装会走当前 npm registry。
dsh plugin --profile desktop add @linxin666/dsh-client-ui-task-board
```

如果 `dsh` 不在 PATH，使用 DSH 安装目录里的命令（Windows 示例）：

```powershell
& "D:\DeepSeek Harness\resources\runtime\cli\bin\dsh.cmd" plugin --profile desktop add @linxin666/dsh-client-ui-task-board
```

安装完成后，**关闭并重新打开 DSH GUI**（或重启 DSH Web），然后验证：

1. 打开「任务看板」；
2. 能新建卡片；
3. 卡片详情里能看到「定时运行」并填写 cron；
4. agent 能调用 `task_board_create`、`task_board_run`、`task_board_schedule`。

若使用的不是 `desktop` profile，先确认实际 profile 名，再把 `--profile desktop` 换掉。不要盲目重复安装：已存在时先用 `dsh plugin --profile desktop why @linxin666/dsh-client-ui-task-board` 检查。

**不要把 GitLab 地址或 JIRA token 写进看板卡的 prompt**，凭据只放实例的 `config.json` 或环境变量。

如果不装任务看板，则改用任意外部调度器定时运行 `guardian tick scan --commit --limit-items`，并由自己的脚本创建/续跑执行会话、消费 outbox；工具包仍可用，但本文 Step 6 的 DSH 看板操作不适用。

---

你最终要搭出来的东西：

```
JIRA（只读查询）
   │  ① guardian tick scan --commit  → 算出"该派谁"、把任务说明写成文件
   ▼
调度会话（宿主 cron，例如 DSH 看板卡）  ② 建/续跑执行会话 + tick claim 占位
   │  ③ 执行会话读 workspace/prompt/<KEY>.md 干活：定位→改代码→验证→提交→推分支
   ▼
workspace/outbox/<KEY>.json（回执）
   │  ④ 调度会话读回执：发 JIRA 评论 → tick ack → 按 pushed 决定挪不挪卡
   ▼
JIRA（评论 + 状态流转）
```

约定（务必先接受这四条，否则后面会做错）：

1. **判定只在 Python 里**（`guardian tick`），调度会话和你的 prompt 都不许自己拼 JQL、不许自己判断"谁新谁旧"。
2. **一张 JIRA 卡 = 一张执行会话卡**，续跑同一个会话，不每轮开新会话。
3. **执行会话不自己发 JIRA 评论**，只写回执文件；评论由调度会话发，这样会话崩了结论也不丢。
4. **不许动源码主仓库的工作副本**，所有改动发生在 worktree（`workspace/repos/<仓库>`）。

---

## Step 1 装包

```powershell
# 假设包在本机 D:\Code\jira-guardian
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e D:\Code\jira-guardian
.venv\Scripts\guardian.exe --help
```

确认成功：

```powershell
.venv\Scripts\python.exe -c "import jira_guardian; print(jira_guardian.__version__)"
# 期望：0.3.0（或更高）
.venv\Scripts\guardian.exe --help
# 期望：列出 jira / tick / worktree / vision / doctor / init / card
```

**坑（高频）**：环境里若有 `PIP_TARGET`（不少公司镜像会设），`pip install` 会把包装到公共目录，
`import jira_guardian` 直接 `ModuleNotFoundError`，`guardian.exe` 也不会生成在你的 venv 里。先跑：

```powershell
Remove-Item Env:PIP_TARGET -ErrorAction SilentlyContinue
$env:PIP_TARGET   # 期望：空
```

再重装。如果之前装错了，检查目标目录里有没有 `jira_guardian-*.dist-info`、`__editable__*.pth` 并删掉。

## Step 2 建值守目录

值守目录只放配置和产物（不放代码）：

```powershell
guardian init --jira-url https://jira.example.com --token <PAT> `
    --project PROJ --me your.name `
    --repo my-service=D:\Code\my-service `
    --repo my-ui=D:\Code\my-ui
```

`--repo 名字=本地源码路径`，可重复。**路径必须是你机器上已经 clone 好的仓库**（这套工具不 clone，
它直接用你本机的凭据和分支），仓库里得配好 `origin` 远程。

产物：

```
./
├── config.json      # 唯一需要你改的文件
├── knowledge/       # 代码地图（每张卡的 prompt 都会带上）
├── data/            # watch-state.json：水位线 + 占位
└── workspace/       # attach / jira / outbox / prompt / repos
```

确认成功：`config.json` 里 `jira.default_jql` 形如
`project = PROJ AND issuetype = Bug AND assignee = currentUser() AND created >= startOfDay() AND resolution = Unresolved ORDER BY created DESC`。

## Step 3 改 config.json（四个地方必须对）

```jsonc
{
  "jira": {
    "base_url": "https://jira.example.com",
    "token": "...",             // 也可用环境变量 JIRA_TOKEN
    "verify_tls": false,        // 自签证书的内网 JIRA 设为 false
    "allow_writes": true,       // 要发评论就必须 true
    "default_jql": "..."        // 扫卡范围
  },
  "watch": {
    "status_in_progress": "In Progress",     // ← 各项目状态机名字不同，必须查真名
    "status_done": "Ready for Testing",      // ← 同上
    "done_statuses": ["Ready for Testing", "Ready For UAT", "UAT", "Done", "Closed", "Resolved"],
    "pin_cards": [],            // 钉住不受时间窗限制的卡（回归用）
    "ignore_cards": []          // 点名不要的卡，优先级最高
  },
  "dev": {
    "enabled": true,            // false = 只读分析模式，不改代码不挪卡
    "worktree_root": "workspace/repos",
    "repos": { "my-service": { "src": "D:\\Code\\my-service", "lang": "python", "verify": "python -m compileall -q src" } }
  },
  "llm": { "base_url": "...", "api_key": "...", "model": "gpt-5.6-terra", "vision_model": "gpt-5.6-terra" }
}
```

状态名怎么查（**不要猜**）：

```powershell
guardian jira transitions PROJ-123
# 期望：列出 {"id","name","to"}；把 to 里的真实名字填进 status_in_progress / status_done
```

`dev.repos.<名>.verify` 是"能证明改动可用"的命令，例如
Java `.\gradlew.bat --offline -q compileJava`、Python `python -m compileall -q src`、前端 `pnpm build`。

## Step 4 体检

```powershell
guardian doctor
```

逐项 `ok / warn / fail`，有 `fail` 时退出码 1。判读规则：

| 项 | 期望 | 说明 |
|---|---|---|
| `python` | ok | 需要 >= 3.10 |
| `config` | ok | 找不到会 fail，回到 Step 2 |
| `jira.auth` | ok | 打印你的账号；401 说明 token 失效或不是 Bearer |
| `jira.jql` | ok/warn | warn = 命中 0 张（"今天没活"，正常）；fail 是 JQL 语法或权限问题 |
| `git` | ok | 没有 git 后面全废 |
| `repo:<名>` | ok | 打印当前分支 + origin URL |
| `credential:<名>` | ok | **warn/fail 必须处理**：取不到凭据时 `clone/push` 会卡住等人输密码 |
| `worktree_root` | ok | 不可写就改路径 |
| `llm` | ok/warn | 只有看图/`vision` 用到，不配也能跑主流程 |
| `prompts` | ok | 模板渲染自检；fail 通常是模板占位符写错 |
| `state` | ok | 显示卡数与当前占位 |

凭据修复（Windows）通常是：`git credential manager` 里没有该 GitLab 主机的记录 → 手工 `git clone`
一次让它记住，或 `git config --global credential.helper manager` 后重试。

## Step 5 标存量卡为已读

不清水位线的话，第一轮会把**所有**符合条件的存量卡都派一遍（真会改代码、真会发评论）。

```powershell
guardian tick seed --jql "project = PROJ AND assignee = currentUser() AND resolution = Unresolved"
# 期望：{"ok":true,"seeded":N,...}
guardian tick scan
# 期望：{"changed":0,"none":true,"items":[]}
```

如果只想让某几张卡还能被扫到，把它们写进 `watch.pin_cards`（不受 `created >= startOfDay()` 限制）。

## Step 6 起调度

调度是宿主的事。以 DSH 看板为例：把 `guardian card` 的输出贴成一张卡卡的 prompt，cron 设 `*/15 9-19 * * 1-5`。

```powershell
guardian card      # 打印主路由卡片 prompt
```

卡片 prompt 一开跑就冻结，所以**流程改动 = 改模板/改这个 prompt + 新建卡**，不要指望改老卡。

主路由每轮只做六步（模板里已经写好，别自己发挥）：

1. `guardian tick scan --commit --limit-items` → 只处理 `dispatch: true` 的 item
2. `guardian tick outbox` → 拿回执
3. `guardian jira comment <KEY> --file <回执里的 comment 落成的临时文件>` → `guardian tick ack <KEY>`
4. `status=ok && pushed=true` → 确认卡在 `status_done`，不在就 `guardian jira transition <KEY> --to "<status_done>"`
5. 派单：`task_board_create`（或你宿主的等价物，`reuseSession: true`）→ 立刻 `guardian tick claim <KEY> --session <id> --board <卡 id>`，**不等待**
6. 只回一行：`本轮：新卡 N、评论续跑 M、回执 K、跳过 J`

执行卡上的 prompt 只写一句：`先读 workspace\prompt\<KEY>.md，按里面的要求做，结束前写好回执。`

## Step 7 端到端验一次（不要跳过）

拿一张**你自己的、无风险的**卡（或临时造一张），走一遍：

```powershell
guardian tick scan --commit          # 期望出现 kind=new, dispatch=true
dir workspace\prompt\PROJ-1.md       # 期望：任务说明（含仓库、验证命令、提交规范、回执路径）
```

然后跑一次调度（手工触发卡片），检查：

- 执行卡被创建/续跑，且 `guardian tick state` 里该卡 `in_flight: true`
- 执行会话产出 `workspace\outbox\PROJ-1.json`，`status=ok`、`pushed=true`（或 `blocked` 且有原因）
- JIRA 上出现评论，正文第一句是结论，末尾有 `[bot 标记]`
- 卡状态被挪到 `status_done`（只有 `pushed=true` 才应该挪）
- 再跑一次 `guardian tick scan` → `changed:0`（不会重复派）

四项都对，链路才算通。

---

## 每天怎么用

```powershell
guardian jira today                       # 今天指派给我的未解决卡
guardian jira get PROJ-1                  # 卡面 + 评论 + 附件
guardian jira attach PROJ-1               # 下载截图
guardian vision "这两张图里配置被拆成几块？" workspace\attach\PROJ-1\*.png
guardian tick state                       # 谁在跑、谁卡住了
guardian tick release PROJ-1              # 判定卡住时人工放行
guardian worktree status my-service       # 执行会话留下的 worktree 状态
guardian worktree clean my-service --branch PROJ-1 --delete-branch
```

常见组合：接手新项目 → `init` → `doctor` → `jira transitions` 查状态名 → `tick seed` → `card` 贴卡片。

## 出问题怎么查

| 现象 | 原因 / 处理 |
|---|---|
| `tick scan` 一直 `changed:0` | JQL 太窄（多数是 `created >= startOfDay()` 且今天没新卡）；`guardian jira today` 核对；要临时拉老卡用 `pin_cards` |
| 同一张卡被反复派 | 我们发的评论缺 `bot_mark` → 必须用 `guardian jira comment` 发（它自动补标记）；或 `ack` 没做、占位一直挂着（等 `in_flight_timeout_minutes` 自动放，或 `tick release`） |
| 卡上真有人回了，却没派 | 检查评论作者是不是你自己/bot；检查该卡是否在 `ignore_cards`；`guardian jira comments PROJ-1` 会标出哪些被判定为 bot |
| 执行会话说"已经修好了"但没改动 | **stale 分支坑**：远端删了、本地 ref 还在，worktree 落在旧提交上。用 `guardian worktree prepare <仓库> <KEY> --fresh`，输出里会明说"本地分支比基线多 N 个提交但远端没有" |
| `worktree prepare` 报"当前在分支 X，不是 Y" | 上一轮的 worktree 还在。加 `--fresh` 拆掉重开；或 `guardian worktree clean` |
| `push` 卡住/超时 | git 凭据不在（doctor 的 `credential:*` 会告知）；先 `git clone` 一次让凭据管理器记住 |
| 评论里 `###` 让 JIRA 很难看 | 一律用 `guardian jira comment`，它把标题压成 `**加粗**`；手发的话自己压 |
| LLM 看图 400 `Unsupported value: 'temperature'` | 网关只接受默认 temperature。`vision` 已不传该参数；自己调网关时也别传 |
| 评论发出去又被判定成"新评论" | 同上：缺 bot 标记。`guardian jira delcomment PROJ-1 <id>` 删掉，用 CLI 重发 |
| `tick scan --commit` 之后执行会话彻底失败 | 那一轮水位线已推进，评论不会再来。手工 `tick release` + 把该卡从 `data/watch-state.json` 删掉重扫 |

调试用的三个只读入口：`guardian tick scan`（不提交，看会派什么）、`guardian tick state`、`guardian tick outbox`。

## 硬性禁止

- 不要 `git add -A` / 不要提交 `.venv`、`node_modules`（被忽略就写 `.git/info/exclude`）
- 不要直接改 `data/watch-state.json`（除排障），水位线错乱会让卡重复派或漏派
- 不要在主仓库工作副本上改代码、切分支
- 不要让执行会话自己发评论（回执丢了还能重试，评论发错要人工删）
- 不要把 token 写进 prompt / 提交进仓库（放 `config.json` 或 `JIRA_TOKEN`，且 `config.json` 加进 `.gitignore`）

## 想改流程（模板在哪）

| 想改什么 | 改哪 |
|---|---|
| 执行会话怎么干活（步骤、提交规范、验证要求） | `jira_guardian/templates/dev.md`，或实例里 `prompts/dev.md` 覆盖 |
| 只读分析模式的说法 | `templates/readonly.md` |
| 主路由六步 / 状态机 / 硬约束 | `templates/router.md`（改完用 `guardian card` 重新生成卡片 prompt） |
| 派不派、什么时候派 | `jira_guardian/tick.py::build_items`，改完必须补 `tests/test_tick.py` 用例并全绿 |

改完自测：

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -v   # 14 个离线用例，不需要真 JIRA
```

用例里 `scan(commit=True)` 之后要显式 `settle()`（清占位）才能测下一轮 —— "占位没释放就不再派单"是设计意图，不是 bug。
