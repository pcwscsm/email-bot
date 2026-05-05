\# 部署指南



> 本文是完整的从零部署流程。如果你只想看核心命令，\[README 的"快速开始"](../README.md#快速开始) 部分已经够了。本文针对每一步的具体操作 + 可能遇到的问题给出详细说明。



\## 适用场景



本项目设计为在\*\*用户自己的电脑（Windows）\*\*上长期运行，不是云端服务。原因：



\- 邮箱授权码、API Key 等凭证留在本机最安全

\- 本地 Windows 任务计划程序作为调度器零成本、零依赖

\- 一次配置后基本不需要维护



如果你想部署在 Linux / Mac / 云服务器，方法类似但调度部分需要改用 cron 而不是 Task Scheduler。



\## 前置条件



\- Windows 10 / 11

\- Python 3.10 或更高版本

\- Git（用于 clone 仓库）

\- 一个支持 IMAP 的邮箱（QQ / Gmail / 学校邮箱等）

\- 一个飞书账号（用于推送）

\- 一个 LLM API（推荐通义千问 DashScope，国内可用、有免费额度）



\## 部署流程



\### 步骤 1：获取代码



```powershell

git clone https://github.com/<your-username>/email-bot.git

cd email-bot

```



\### 步骤 2：安装 Python 依赖



```powershell

pip install -r requirements.txt

```



如果你电脑同时装了多个 Python 版本，注意确认 `pip` 对应的是你打算用的那个 Python。可以用 `python --version` 和 `pip --version` 检查。



\### 步骤 3：生成配置文件



```powershell

python email\_bot.py --setup

```



这会在 `%USERPROFILE%\\EmailBot\\config.json` 创建一份配置模板。



或者，你可以直接手动复制：



```powershell

mkdir %USERPROFILE%\\EmailBot

copy config.example.json %USERPROFILE%\\EmailBot\\config.json

```



后者的好处是模板里有详细注释（\_comment 字段），引导你怎么填。



\### 步骤 4：填写配置



打开 `%USERPROFILE%\\EmailBot\\config.json`，按下面分步填写。



\#### 4.1 邮箱凭证



每个邮箱都需要 IMAP 授权码（\*\*不是登录密码\*\*）。获取方式：



\*\*QQ 邮箱\*\*：

1\. 登录 web 端：\[https://mail.qq.com](https://mail.qq.com)

2\. 设置 → 账户

3\. 找到"IMAP/SMTP服务"，开启

4\. 按提示完成短信验证，获得 16 位授权码

5\. 把这个授权码填到 config 的 `password` 字段



\*\*Gmail\*\*：

1\. 登录 Google 账号

2\. 安全性 → 两步验证（必须先开启）

3\. 应用专用密码

4\. 选择"邮件"，生成 16 位密码

5\. 填入 config



\*\*北交大邮箱（Coremail）\*\*：

1\. 登录 mail.bjtu.edu.cn

2\. 选项/设置 → POP3/IMAP/SMTP

3\. 开启 IMAP 服务

4\. 一些 Coremail 系统直接使用邮箱密码作为 IMAP 密码（不需要单独授权码），具体看你学校的设置



\#### 4.2 LLM API Key



推荐通义千问 DashScope（阿里云）：



1\. 注册：\[https://dashscope.aliyun.com](https://dashscope.aliyun.com)

2\. 实名认证（注册时提示）

3\. 控制台 → API-KEY 管理 → 创建新的 API-KEY

4\. 复制 key（以 `sk-` 开头），填入 config 的 `dashscope\_api\_key`



费用说明：qwen-turbo 模型对个人使用几乎是免费的（开通时送的额度足够用一两年）。如果运行量大，每月也只有几块钱。



\#### 4.3 飞书机器人 Webhook



1\. 在飞书 PC 客户端进入一个群（建议新建一个专门接收推送的群，比如"个人助手"）

2\. 群设置（右上角齿轮）→ 群机器人 → 添加机器人

3\. 选择"自定义机器人"

4\. 设置名称（任意）和描述

5\. \*\*关键步骤\*\*：安全设置那一栏，\*\*取消"自定义关键词"勾选\*\*（或者把关键词设为 `通知`、`速递` 等本程序卡片里会出现的词）

6\. 复制 Webhook 地址，填入 config 的 `feishu.webhook`



\### 步骤 5：dry-run 验证



```powershell

python email\_bot.py --dry-run

```



预期看到的输出（约 10-30 秒）：



```

\[2026-XX-XX HH:MM:SS]\[INFO] ===== Email Bot v1.4 start =====

&#x20; \[QQ] connecting to imap.qq.com...

&#x20; \[QQ/INBOX] N new mail(s)

&#x20; \[Gmail] connecting to imap.gmail.com...

&#x20; \[Gmail/INBOX] no new mail

&#x20; ...

&#x20; \[api:BJTU实习] first run: N records seen, pushing 3 preview

&#x20; \[api:BJTU宣讲会] first run: N records seen, pushing 3 preview

Total new items: N

Dry run: would push N item(s)

===== Done =====

```



如果看到这些日志，说明所有数据源都连上了。\*\*dry-run 不会推送到飞书\*\*，但会调用 LLM——LLM 调用是 dry-run 的代价（这就是它叫"几乎完整"而不是"完全空跑"的原因）。



\#### 常见错误



| 错误信息 | 原因 | 解决 |

|---|---|---|

| `connection/login failed` | 邮箱授权码错 / IMAP 没开 | 检查 4.1 步骤 |

| `LLM call failed` | API key 错 / 没额度 / 网络问题 | 检查 4.2 步骤 |

| `LLM 调用失败` 出现在摘要里 | 同上，但不致命 | 检查 LLM 配置 |



\### 步骤 6：第一次正式运行



```powershell

python email\_bot.py

```



这次会真的推送到飞书。第一次运行的特殊之处：



\- 邮箱：按 `initial\_mode` 决定怎么处理历史邮件（`recent24h` 拉最近 24 小时；`unseen` 拉所有未读；`uid\_only` 不推送只记录基线）

\- 实习/宣讲会：按 `first\_run\_preview` 决定推送几条样例（默认 3）



第一次运行可能会一次推十几条。\*\*这是正常的\*\*，之后每次运行只推增量，会变得很安静（每天 1-3 条）。



\### 步骤 7：注册定时任务



```powershell

python email\_bot.py --install-task

```



这会创建一个 Windows 任务计划程序条目，每 10 分钟自动跑一次 `email\_bot.py`。



验证：按 `Win+R`，输入 `taskschd.msc`，回车。在"任务计划程序库"里应该能找到一个名为 `EmailBot-Poll` 的任务。



如果想改频率（默认 10 分钟），可以在任务计划程序里手动改"触发器"。或者卸载后改 `email\_bot.py` 里 `install\_task` 函数的 `--MO` 参数（min 单位）重新装。



\#### 卸载定时任务



```powershell

python email\_bot.py --uninstall-task

```



\## 日常使用



\### 看运行日志



```powershell

type %USERPROFILE%\\EmailBot\\bot.log

```



或者用任何文本编辑器打开。日志会一直追加，几个月后可能几 MB——目前没有自动 rotation，需要时手动清理。



\### 查看当前状态



```powershell

type %USERPROFILE%\\EmailBot\\state.json

```



里面记录了每个数据源处理到哪里。\*\*不建议手动改\*\*——除非你知道自己在干什么。



\### 重置状态（紧急按钮）



如果 state.json 出问题，或者想强制重新建立基线：



```powershell

python email\_bot.py --reset-state

```



之后下一次运行会按 `initial\_mode` 和 `first\_run\_preview` 的设定重新建立基线。



\### 临时停跑



打开任务计划程序 → 右键 `EmailBot-Poll` → 禁用。等想恢复时改成"启用"即可。\*\*不需要卸载重装\*\*。



\## 安全建议



\### 凭证管理



\- `config.json` 含所有凭证，\*\*绝不能进入 git 仓库\*\*（`.gitignore` 里已经过滤）

\- 不要把 config.json 复制到桌面/共享文件夹

\- 笔记本丢失时优先做的事：登录所有相关服务（QQ 邮箱、Gmail、DashScope、飞书）重置凭证



\### 凭证泄露应急流程



如果发现 config.json 可能被他人看到（共享屏幕忘了关、被人借电脑等）：



1\. \*\*立刻\*\*重置所有相关凭证（邮箱授权码、DashScope key、飞书 webhook 重新生成）

2\. 不要"先看看有没有真的泄露"——先重置再说

3\. 然后检查这些服务的最近登录记录，看有没有异常



凭证重置只要几分钟，账户被滥用的成本可能是几百块账单或者邮件被偷看。\*\*预防成本极低，不预防成本极高\*\*。



\### 物理访问控制



\- 给 Windows 账户设密码（你笔记本自己用就够了）

\- 不在咖啡馆/图书馆等公共网络上跑这个工具（避免邮箱密码经过不可信网络——虽然 IMAP-SSL 已经加密，但多一层保护没坏处）



\## 故障排查



\### 飞书没有收到推送



1\. 看 `bot.log` 最近的"Feishu push"那行

2\. 如果是 `Feishu push OK`：飞书侧问题（机器人被群管理员禁用？）

3\. 如果是 `Feishu API: {...}`：看错误码，常见是 `19024` 关键词不匹配（参见步骤 4.3 的安全设置）

4\. 如果是 `Feishu push failed`：网络问题或 webhook URL 写错



\### LLM 摘要总是 "LLM 调用失败"



1\. 检查 `dashscope\_api\_key` 是否正确

2\. 控制台看是否有额度（DashScope 有免费试用额度，用完后需要充值或换免费模型）

3\. 国内用户极少数情况会遇到防火墙问题——如果是这种情况，考虑换用其他 LLM 服务



\### 某个邮箱总是 connection failed



1\. 看是不是临时网络问题（一两次失败正常，每次都失败才是问题）

2\. 检查 IMAP 是否还开着（有些邮箱会因长期不用关闭 IMAP 服务）

3\. 重新生成授权码（旧的可能过期）



\### 学校邮箱拉不到邮件但 webmail 能看到



1\. 检查 `initial\_mode`：用 `unseen` 模式更可靠

2\. 用 `--list-folders` 看是不是邮件在其他文件夹（学校邮箱有时把通知归类）

3\. 如果还不行，开 `--reset-state` 重新建立基线



\## 卸载



完整卸载流程：



```powershell

\# 1. 停掉定时任务

python email\_bot.py --uninstall-task



\# 2. 删除运行时数据（含凭证！）

rmdir /S %USERPROFILE%\\EmailBot



\# 3. 删除代码

cd ..

rmdir /S email-bot



\# 4. （可选）撤销凭证：登录 QQ/Gmail/DashScope/飞书，撤销/删除对应授权

```



凭证撤销那一步\*\*强烈建议做\*\*——即使本地 config.json 已经删了，万一有备份残留也无所谓。

