# Microsoft Rewards 每日任务自动完成

自动做完 `https://rewards.bing.com/earn` 上**用 PC 就能完成**的日常任务，每天 **78–92 分**。带本地监控面板、定时调度和完成通知。

- **运行时零 AI**：纯 DOM 解析 + 点击，不调用任何大模型
- **幂等**：没东西可做就秒退，不会反复折腾账号
- **拟人化**：有头浏览器、随机点击间隔、独立 profile、每天只跑几次
- **自适应**：卡片按结构识别，任务改文案、换链接、加新卡都照常工作

## 每天能拿多少

| 任务 | 分值 | 处理方式 |
|---|---|---|
| 每日任务卡片（搜索类，约 7 张） | +5 × 7 | 逐张点开，等加载后关闭 |
| 「完成此拼图」入口 | +5 | 点开即完成 |
| Edge 推广卡片 | +5 × 2 | 点开 `microsoftedgewelcome` 页面 |
| 必应搜索连续打卡 | +3 | 在 cn.bing.com 搜索框真实输入一次关键词 |
| 每日活动卡片（3 张） | +10 × 3 | 展开「每日连续打卡活动」后逐张点开 |
| 活动连续打卡奖励 | +30 | 上面 3 张点亮后自动到账 |
| 「月度精选指南」长期打卡 | +50 | 每天推 1 步，5 天完成 |

**自动跳过的**（PC 端做不到）：「需要银牌级别」的卡、必应 App 签到、要求安装 Rewards 桌面应用的长期任务。

> 每日活动那 3 张卡看着像「每日一题」，其实是带 `rnoreward=1` 的普通 Bing 搜索链接，**点开即完成，不需要答题**。

## 工作原理

1. 用 Playwright 以 `channel="msedge"` 驱动**系统自带的 Edge 内核**（不下载 Chromium），加载独立 profile
2. 收集页面上所有「文本里带 `+数字`」的可点元素，剔除包含同样匹配的祖先节点，按 `y` 坐标分到「日常任务」区
3. 「锁定」状态不看文案里的「需要银牌级别」，而是查 `aria-disabled` / `disabled` / `pointer-events`——账号升级后自动解锁，无需改代码
4. 逐张点击，等 popup 加载完再关；每张点完回列表确认它是否消失或积分是否上涨（两种都算完成）
5. 搜索打卡是真的在 `#sb_form_q` 里输入关键词再回车，不是伪造请求
6. 长期卡每张每次最多推 1 个子活动，避免触发风控

## 环境要求

- Windows 10 / 11
- Microsoft Edge，且已登录一个 Bing 账号
- Python 3.10+

> 只支持 Windows：脚本用到了 Win32 的闲置检测、计划任务和 WinRT 通知。

## 安装

```powershell
git clone <this-repo> rewards
cd rewards

python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

copy config.example.json config.json
```

> 用系统 Edge 时**不需要** `playwright install chromium`。如果 `channel="msedge"` 报找不到浏览器，再执行 `python -m playwright install chromium`。

### 首次登录（只做一次）

```powershell
python login_probe.py
```

会打开一个 Edge 窗口，手动登录 Bing 账号。脚本检测到登录成功后自动保存到 `profile/` 并退出。这个 profile 之后由 `run.py` 复用，**不要删**。

## 使用

```powershell
# 跑一次
python run.py

# 按自动规则跑（在时间窗内 + 今天没做完 + 键鼠闲置够久）
python run.py --auto

# 面板
启动面板.cmd          # 或 python serve.py 后打开 http://127.0.0.1:8765

# 计划任务：每 30 分钟唤醒一次，判断是否该跑
python schedule.py install
python schedule.py status
python schedule.py remove
```

面板能看到最近 14 天的运行次数、完成卡片数、今日积分、各项打卡状态、失败原因，也能一键开关自动运行或立即跑一次。

## 配置

`config.json`（从 `config.example.json` 复制）：

| 键 | 默认 | 说明 |
|---|---|---|
| `enabled` | true | 总开关，面板上的按钮改的就是它 |
| `headless` | false | 建议保持 false，有头运行更像真人 |
| `min_delay` / `max_delay` | 2.0 / 5.0 | 两次点击之间的随机间隔（秒） |
| `do_search_streak` | true | 必应搜索连续打卡 |
| `do_activities` | true | 3 张每日活动卡 |
| `do_punchcards` | true | 长期打卡，每天推 1 步 |
| `notify` | true | 跑完弹 Windows 通知 |
| `auto_window` | `["07:00","23:30"]` | 允许自动运行的时间段 |
| `idle_only` | true | 只在电脑闲着时跑 |
| `idle_seconds` | 300 | 键鼠闲置多少秒算「闲着」 |
| `recheck_hours` | 6 | 距上次成功运行多久后再复查一次，用来捡当天新增的卡片 |
| `search_keywords` | 见文件 | 搜索打卡用的关键词池，随机挑一个 |

### 为什么需要 `recheck_hours`

微软会在白天调整任务池。实测某天中午分母从 `175` 变成 `180`，多出一张 +5 的新卡。如果只按「今天跑成功过就不再跑」，这张卡就永远拿不到。所以成功运行 6 小时后允许再查一遍，没新东西会 20 秒内退出。

## 降低被检测的做法

- 用系统自带 Edge 内核，不用 Playwright 捆绑的 Chromium
- 去掉 `--enable-automation`、注入脚本抹掉 `navigator.webdriver`、加 `--disable-blink-features=AutomationControlled`
- 中文语言 + 上海时区 + 1440×900 视口，与真实中文用户一致
- 点击间隔 2–5 秒随机，任务顺序按页面顺序，不并发、不多线程
- 每天只跑几次，做完立即停；不做移动端 UA 伪装、不换 IP
- 长期打卡每天只推 1 步，不暴力刷

## 已知限制

- 页面数据是服务端直出，`/api/getuserinfo` 被 CORS 挡住，所以只能靠解析 DOM 判断状态
- 微软改版后选择器可能失效，改 `run.py` 里的 `COLLECT` / `PROGRESS` / `QUEST_JS` 即可
- 「签到」必须用必应手机 App，PC 端无解
- 「需要银牌级别」的卡要等账号升级，脚本会自动纳入，无需改动
- 部分长期任务的奖励是会员而不是积分（如 Super Duolingo），这类默认跳过

## 免责声明

本项目仅供学习与技术交流，用于研究浏览器自动化与页面结构解析。

使用自动化工具可能违反 [Microsoft 服务条款](https://www.microsoft.com/servicesagreement)，可能导致账号被限制、积分被撤销。**请自行评估风险，作者不承担任何由使用本代码产生的后果。** 建议仅在自己的账号上、以接近真人的频率使用。

## License

MIT
