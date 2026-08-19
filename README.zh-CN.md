# wake-watcher

[English](README.md) · **简体中文**

[![License: MIT](https://img.shields.io/badge/License-MIT-informational.svg)](LICENSE)
[![Zero dependencies](https://img.shields.io/badge/运行时依赖-0-brightgreen.svg)](#零依赖)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](pyproject.toml)

**跟休眠、跟电源管理没有任何关系。** wake-watcher 盯的是**在一次回合中途被瞬态错误
打断、从此卡住不动的 Claude Code agent**——然后给它补上那一条能让它继续跑下去的
retry。

它为之存在的故障长这样：一个后台会话已经干了四十分钟，连接在响应中途断了。这一
回合永远没有正常结束——所以 `Stop` / `SubagentStop` 根本不会触发，没有崩溃，也没有
任何东西被记成失败。会话就那么杵在那儿，攥着一堆没人来取的成果，直到你碰巧想起来
看一眼。它需要的其实只有一个词：`retry`。

wake-watcher 是一个跑在你本机的守护进程，替你去看：轮询、把错误拿去跟一份规则文件
比对、只唤醒**可以安全唤醒的**，然后回头验证这个会话到底有没有真的续上。

**战绩，截至 2026-08-18。** 大约两个月、6 个项目实例：**167 个会话被唤醒过共 419
次，其中 130 个会话——约 78%——被确认真的续跑了。** "确认"是双信号口径：retry
**送达了**，**并且**在验收窗口内真的出现了一次新回合。光是送达，永远不算救活。
剩下那约 22% 也没有被悄悄抹掉——每一次都浮出了一条人能看见的告警。 ---

## 装它之前先读这一段

唤醒不产生新工作，它让**本来就在进行中的**工作继续——风险全在这句话里。
**如果那个任务的下一步是部署、是花钱、是删数据，让它续跑就会执行那一步。**
wake-watcher 对你的任务在做什么没有任何模型；它读的是一条错误字符串、一个进程状态、
一个文件时间戳。从它站的位置看，续跑一次 lint 和续跑一次破坏性操作长得一模一样。

"做不可逆的事之前先问人"这件事，只能由任务自己来做。wake-watcher 不会绕过这种关卡
——它送进去的那条 retry 不替任何人作答——但它也不会替你新增一道。

所以默认值是刻意保守的：

- **装完不会自己跑。** 你不发话，它不扫描、不唤醒。
- **本 README 给你的第一条命令是 dry-run**，不是 start。先让它空跑一天，看它会怎么
  判，再决定要不要让它真动手。

[`THREAT-MODEL.md`](THREAT-MODEL.md) 是风险优先的那一份：三个爆炸半径过大的面、
哪些测到了哪些没测到、以及为什么上游一次改动就可能让这个工具失效。真要把它指向一个
你在意的项目、而且只打算读一页，那就读那一页，别读这一页。 ---

## 它唤醒什么，又坚决不碰什么

规则以数据形式放在 [`src/wake_watcher/patterns.json`](src/wake_watcher/patterns.json)
里，不写死在代码里——每一条都带着它为什么在这儿的出处。分类器**没见过的字符串一律
默认拒绝**：一个不认识的错误是人的问题，不是重试的候选。

| 会唤醒——瞬态基础设施错误 | 绝不唤醒——这是一个真答案 |
|---|---|
| `connection closed mid-response`、`connection closed while thinking`、`stream idle timeout`、`unable to connect to api` | `403`、`request not allowed`、`permission denied` |
| `rate limited`（且明确写着 *not your usage limit*）、`server is temporarily limiting requests`、`overloaded_error` | `usage limit reached`、`quota exceeded`、`out of credits`、`insufficient credits` |
| `API Error: 500 / 502 / 503 / 529`、`503 service unavailable`、`internal server error` | `user rejected` / `cancelled` / `interrupted`、`denied` |
| `this is not your fault`、`please retry`、`temporarily unavailable` | `authentication failed`、`invalid api key`，以及 `400` 这类请求构造错误 |

这种不对称是故意的。少唤醒一次，代价是你晚点手动推一下某个卡住的会话；多唤醒一次，
代价可能是把一个真实故障无限重试掉，而不是端到你面前。

拿不准某条字符串会被怎么判，不用跑任何东西：

```sh
wake-watcher --check-string "<那条错误原文>"
``` ---

## 安装

需要 Python 3.9+，以及已经装好的 Claude Code。macOS 或 Linux。

```sh
git clone https://github.com/NatureBlueee/wake-watcher ~/.local/share/wake-watcher
cd ~/.local/share/wake-watcher
./install.sh --dry-run     # 它会做的每一处改动，全列出来
./install.sh
```

这一步只往你的 PATH 上放一条命令：`wake-watcherctl`。它不启动任何东西，也不注册任何
服务——wake-watcher 是**按项目逐个 opt-in** 的。

然后进到你想让它看的那个项目：

```sh
cd /path/to/your/project
wake-watcherctl dry              # 前台运行，什么都不唤醒——从这条开始
```

`dry` 会跑完整的扫描循环，把它**本来会做的每一个判断**打出来。让它跟着一次真实的
工作时段跑一遍。等到"它会做的"和"你会做的"对得上了，再：

```sh
wake-watcherctl init             # 为**当前这个目录**生成并加载 launchd/systemd 服务
```

`./install.sh --uninstall` 把机器恢复原状。

### 子命令

| | |
|---|---|
| `wake-watcherctl dry [name]` | 前台 `--dry-run`。只判断、只打印，不唤醒任何东西。**在 `start` / `init` 之前先跑它。** |
| `wake-watcherctl once [name]` | 真跑一轮扫描然后退出。注意：这条**是会唤醒的**，它是调试用的，不是"安全试跑"。 |
| `wake-watcherctl start [name]` | 后台进程，不装系统服务；登出或重启就没了。 |
| `wake-watcherctl stop [name]` | 停掉 `start` 起来的那个进程 |
| `wake-watcherctl status [name]` | 在不在跑，以及 pid |
| `wake-watcherctl tail [name] [n]` | 看这个实例日志的最后 n 行（默认 40） |
| `wake-watcherctl init [name]` | 为当前目录生成并加载 launchd/systemd 服务 |
| `wake-watcherctl uninstall [name]` | 卸掉那个服务；日志和状态保留 |

`name` 用来标识一个实例，默认取当前目录的 basename。一个 name = 一个项目 = 一个状态
目录 = 一个服务——正是这一点让一份安装可以同时看好几个项目而互不打架。

被监控的项目**永远是你运行 `init` 时所在的那个目录**，而且每次都被无条件写死进去：
因为代码自带的兜底默认值是从"wake-watcher 自己装在哪"推出来的，一旦它装在公共位置，
那个默认值就跟你想监控的项目毫无关系了。

要紧急全停，见 [`SECURITY.md`](SECURITY.md) 里的紧急关停步骤。停掉进程就停掉了全部：
不扫描、不唤醒、不执行命令钩子。 ---

## 它怎么判

六道闸，每一道都是某次翻车留下来的：

- **只唤醒没人在开的车。** 默认只有当**没有活进程**还攥着这个会话时，它才算候选——
  这样绝不会跟一个已经在驱动同一会话的管理进程抢方向盘。
- **项目根作用域。** 它只处理被指定的项目根之下的会话，路径按真实文件系统解析，
  同名相近的兄弟目录不会被顺手扫进来。
- **免唤醒名单（do-not-wake）。** 每轮重读、立刻生效，并且**压过其他所有信号**。
- **快车道有硬上限，之后进小时级慢车道。** 同一个会话尝试几次之后，间隔从秒变成
  小时。它不可能退化成高频重试循环——这个项目历史上有过一次通宵事故，正是这种形状：
  一个把自己回声误读成"有进展"的循环，把进程一层层堆起来，最后机器倒了。
- **断网时整轮延后。** 机器自己连不上网时，整轮扫描延后，而不是把每个会话都当成一次
  新鲜的失败去反应。一台断网的笔记本，不该烧掉任何会话的唤醒预算。
- **双信号验收。** 只有"消息送达"**并且**"窗口内真的出现了一次非错误的新回合"，才
  记成救活。"消息发出去了"不等于"agent 回来了"。

[`docs/WHY.md`](docs/WHY.md) 里有每一道闸背后的那次事故——包括三条**上过生产、然后
被砍掉**的检测思路。

### 哪些测到了，哪些没有

**决策层**——要不要唤醒、唤醒谁——是做过变异测试的：七条契约被逐条故意打断，每一次
测试套件都变红。**执行层**——往活着的终端里敲字、判断进程死活、按墙上时钟判定验收
窗口——天天在生产里跑，但自动化测试**并没有**真正把它钉住。"人会发现不对"和"测试
会在上线前拦住"是两回事，[`THREAT-MODEL.md`](THREAT-MODEL.md) 把这两句分开写，而不是
糊成一个覆盖率数字。 ---

## 一次连着失败六回的记录

有一个会话连续收到了六次唤醒尝试。六次全部落在"**送达了，但没救活**"：retry 文本
确实进了输入框，而 600 秒的验收窗口内没有出现任何真回合。六比零。

这条记录摆在这里是故意的，因为**没有发生的事**才是重点：

- 六次里**没有一次**被记成成功。双信号验收拒绝把"消息到了"算成"agent 又开始干活了"
  ——而那恰恰是一个不那么诚实的工具会拿来报数的口径。
- 它**没有**变成 60 秒一轮的重试循环。快车道触顶之后，这个会话掉进小时级慢车道，
  并且一直待在那里。
- 六次**每一次**都浮出了一条人能看见的告警。

PTY 注入依赖它控制不了的终端渲染时序，这个依赖是结构性的，不是"后来修好了"的 bug。
一个有时候救不活、并且如实告诉你它这次没救活的工具，比一个只报捷报的工具值钱。 ---

## 它做不到的事

边界写在前面，方便你对照自己的需求：

- **交互式会话只会收到提醒，不会被自动续跑。** `claude attach` 官方只支持 background
  session——这是当前 CLI 的能力边界，不是这里的取舍。你那个交互主窗口可以被标出来让
  你去看一眼，但没法被自动续上。
- **它依赖未公开的内部结构。** 磁盘上的作业状态文件、transcript 记录的形状、
  `claude agents --json`、attach TUI 的渲染时序——没有一样是有契约的公开接口。上游一次
  改动就可能让它失效。设计上的应对是**要坏就大声坏**：读不懂就拒绝动作并且喊出来，
  绝不允许心跳照打、整夜"什么都没找到"、从外面看还像在正常工作。
- **Linux 尚未实机验证。** 上面那份战绩是在 macOS/launchd 上挣出来的。systemd 模板
  渲染的是 `/etc/systemd/system/` 下的**系统级 unit**，安装需要 sudo——它是按
  **untested（未实机验证）** 发出来的，欢迎回报结果。
- **不支持 Windows。** PTY 注入建立在 `pty.fork()` 上，那是 POSIX-only。

### PTY 注入是独立开关，默认关闭

要把一条 retry 交给一个**还活着**的会话，只能模拟一个人把它敲进去：开一个伪终端、
attach 上去、等输入框真的渲染出来、把文本写进去、再脱离。这是本代码库里**爆炸半径
最大**的原语——会话拿到这条输入之后做什么，wake-watcher 事先不会审。

它挂在自己的开关 `WAKE_WATCHER_ENABLE_PTY_INJECT` 后面，**不显式打开就是关的**。
打开之前，请先读 [`THREAT-MODEL.md`](THREAT-MODEL.md) 的 *PTY injection* 一节。

同一节还讲了 `WAKE_WATCHER_LIVENESS_CMD`：那是一个**任意命令执行钩子**，默认不设；
一旦设了，请按"往 crontab 里加一行"的标准去审那条命令模板，而不是按"改个配置项"。 ---

## 零依赖

`pyproject.toml` 没有任何运行时依赖。只有 Python 标准库，Python 3.9+。

这是**属性**，不是审美偏好。它是一个用系统 Python 无人值守跑着、还要去戳别的无人值守
会话的守护进程；在这样一个进程里塞一棵依赖树，等于埋一个没人盯着的隐患。
`pip install -e '.[dev]'` 只多装一样东西：`pytest`，而且只在跑测试时需要。 ---

## 姊妹项目：quotapool

[quotapool](https://github.com/NatureBlueee/quotapool) 治的是另一种病。它管的是
**"额度窗口烧完了，于是全停"**，而且要它有意义，你手上得有不止一份 Claude 订阅。
wake-watcher 管的是**"网络或服务端的瞬态错误，让某一个会话在回合中途卡住了"**——
这件事在**单账号、单机、凌晨三点**照样会发生。

两者不互相替代。额度耗尽和响应被打断是两种故障、两种修法；只有一份订阅的人，
照样需要这一个。 ---

## 其余文档

| | |
|---|---|
| [`THREAT-MODEL.md`](THREAT-MODEL.md) | 风险优先的那一份：三个高杠杆面、测到了什么没测到什么、兼容性本身就是风险面 |
| [`SECURITY.md`](SECURITY.md) | 它持有什么（不持有凭据）、怎么报漏洞、怎么紧急关停 |
| [`docs/WHY.md`](docs/WHY.md) | 每个机制为什么长成现在这样，以及是哪次事故换来的 |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | 怎么跑测试套件，以及两条不可商量的规矩 |
| [`src/wake_watcher/patterns.json`](src/wake_watcher/patterns.json) | 规则数据本身——每条都带着它的出处 |

改动规则数据、veto 逻辑、`WAKE_WATCHER_LIVENESS_CMD` 或 PTY 注入路径，合并前**必须**
有人做安全评审。CI 全绿在那里是必要条件，不是充分条件。 ---

## 许可

MIT，见 [`LICENSE`](LICENSE)。

不是 Anthropic 的产品，与 Anthropic 无关联。它不持有任何自己的凭据——它动手的方式是
调用你**已经登录好的** `claude` CLI，跟你自己在终端里敲的是同一个二进制。
