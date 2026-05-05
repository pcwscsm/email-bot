\# 架构详解



> 本文是对 \[README](../README.md) 中"架构"一节的延展，讲清楚每个设计决策的来龙去脉。如果你只想用这个工具，README 已经足够；如果你想理解或扩展它，本文为你而写。



\## 设计目标的优先级



工程项目的所有取舍都来自优先级排序。本项目的优先级（从高到低）：



1\. \*\*凭证安全\*\*——任何方案不能让账号密码处于风险中

2\. \*\*运行可靠性\*\*——某个数据源挂了，其他源不能受影响

3\. \*\*可扩展性\*\*——加新数据源的成本必须低

4\. \*\*可调试性\*\*——出问题时能快速定位

5\. \*\*代码可读性\*\*——三个月后回看自己也能懂

6\. \*\*运行效率\*\*——不追求毫秒级，能在分钟级响应即可

7\. \*\*特性完整性\*\*——能解决主要痛点即可，长尾不追求



这个排序解释了很多看似"不优雅"的工程决策：



\- 为什么部署在本地而不是云端？因为优先级 1（凭证）压倒了"云端 7×24 在线"的便利性

\- 为什么多个数据源串行处理？因为优先级 2（QQ 邮箱限频踩坑）压倒了优先级 6（效率）

\- 为什么不拆成多个 Python 文件？因为优先级 5（一个文件全文搜索更快）压倒了"模块化"的教科书规则



\## 三层架构



```

┌─────────────────────────────────────────────┐

│  L3 调度层    main() + slice\_state\_for()    │

│              定时触发 → 并发收集 → 串行处理   │

├─────────────────────────────────────────────┤

│  L2 数据层    Source 抽象 + 三种具体实现     │

│              IMAP / Web HTML / JSON API     │

├─────────────────────────────────────────────┤

│  L1 处理层    summarize\_item + push\_to\_feishu│

│              LLM 摘要 → 飞书卡片            │

└─────────────────────────────────────────────┘

```



每一层只依赖下一层，不能反向依赖。L2 不知道 L3 的存在，L1 不知道 L2 中具体哪个 source 产生的数据。



\## L2 详解：为什么需要 Source 抽象



\### 抽象的起源



这个项目最早只有 IMAP 一个数据源（v1.0），直接写成 `fetch\_new\_mails()` 函数就够用。



到了 v1.3 引入网页爬虫时，发现有一堆"邮件特有"逻辑混在了主流程里：



\- `fetch\_new\_mails` 直接读写 `state\["uids"]\[account\_name]`——爬虫源的状态结构完全不同（用 seen\_ids 而不是 last\_uid）

\- 主流程根据"邮件 vs 爬虫"做 if-else 分支——再加第三个源就要再加一个分支

\- 测试时要 mock 整个流程，因为状态读写、网络请求、LLM 调用全混在一起



\*\*这是教科书里"过早优化是万恶之源"的反例\*\*：当时没有提前抽象，结果三个月后想加新功能时不得不重构。



\### Source 契约



经过重构（v1.2）后，所有数据源遵守同一个契约：



```python

class Source:

&#x20;   def state\_keys(self) -> list\[str]:

&#x20;       """返回这个 source 会读写的所有状态键"""

&#x20;       ...

&#x20;   

&#x20;   def fetch(self, state\_slice: dict) -> tuple\[list\[Item], dict]:

&#x20;       """

&#x20;       给定本 source 关心的状态切片，返回新条目和更新后的状态。

&#x20;       必须是纯函数：不写文件、不发 HTTP、不修改全局状态。

&#x20;       """

&#x20;       ...

```



注意契约里的几条\*\*约束\*\*：



\*\*约束 1：fetch 必须是幂等的\*\*



调用两次 `fetch(state\_slice)` 用相同输入应该得到相同输出。这意味着内部不能有计数器、不能依赖当前时间作为决策因素（除了 IMAP 的 SINCE 查询那种例外）。



\*\*为什么这条重要\*\*：测试。你只有保证幂等性，才能写"喂同一份 mock 数据，断言输出相等"这种测试。



\*\*约束 2：fetch 不能写状态\*\*



返回的 `state\_updates` 由调度层（main）写入。这是\*\*关键设计决策\*\*——只有在 fetch 完全成功后才提交状态更新。如果 fetch 失败（抛异常或返回空），主流程不会调用 save\_state，保证下次重试时还能从同一起点开始。



\*\*约束 3：只能看到自己的状态切片\*\*



通过 `slice\_state\_for(source, full\_state)` 函数，每个 source 在 fetch 时只能看到自己的状态键。这防止了一个常见错误：A source 不小心读了 B source 的状态。



\### 三个具体实现的差异



| 维度 | IMAPSource | WebSource | JSONAPISource |

|---|---|---|---|

| 数据获取 | IMAP 协议 | HTTP + HTML 解析 | HTTP + JSON 解析 |

| 增量识别 | UID 大小比较 | seen\_ids 列表 | seen\_ids 列表 |

| 状态结构 | `{"last\_uid": int}` | `{"seen\_ids": list}` | `{"seen\_ids": list}` |

| 失败处理 | 网络/登录失败返回空 | 解析失败返回空 | API 错误返回空 |

| 第一次运行 | initial\_mode 控制 | 全部记入 baseline | first\_run\_preview 条数 |



注意 WebSource 和 JSONAPISource 的状态结构是一样的——这暗示它们其实可以共享更多代码。但目前没有合并，因为：



1\. 抽象层级再上一层会让代码更难读

2\. 它们的细节差异（HTML 解析 vs JSON 解析）足够大，强行复用反而难维护

3\. \*\*抽象不要超前\*\*——等真有第四个、第五个 source 再考虑



\## L1 详解：LLM 摘要的提示词分发



\### 为什么不用统一提示词



最早 v1.0 只有邮件，提示词里写"判断学校通知/导师邮件优先级最高"。



加爬虫源时发现这个判断逻辑套不上——网页通知没有"发件人"，"重要性"的判断维度完全不同。



加就业信息时问题更突出——"对计算机大类大一学生有用的实习"这种判断，跟"重要的邮件"是两个世界。



\### 解决方案



`summarize\_item` 函数根据 item 的 `source\_type` 字段路由到不同的 prompt 模板：



```python

PROMPT\_REGISTRY = {

&#x20;   "imap":            SUMMARY\_PROMPT\_MAIL,

&#x20;   "web":             SUMMARY\_PROMPT\_NOTICE,

&#x20;   "bjtu\_internship": SUMMARY\_PROMPT\_INTERNSHIP,

&#x20;   "bjtu\_fair":       SUMMARY\_PROMPT\_FAIR,

}

```



每个模板里"判断依据"那一段是定制的——这是真正决定 LLM 输出质量的部分。



\*\*关于提示词工程的一条经验\*\*：与其反复调整模板的措辞，不如把"判断依据"写得\*\*具体、可枚举、有反例\*\*。比如：



```

判断依据（核心是"对一名想做技术方向的本科低年级学生是否值得关注"）：

\- 高: 互联网/科技公司、外企、北京可达、对本科开放、不限专业或包含计算机/软件/电子/信息

\- 中: 国企/央企技术岗、北京周边城市、本硕都要但本科可投

\- 低: 偏远地区、销售/客服/行政岗、明确仅招硕博、明显非技术方向

```



LLM 看到具体的分类边界和示例，比看到"重要性高的内容"这种抽象描述准得多。



\## L3 详解：调度逻辑



主流程（main 函数）做的事：



```

1\. 加载 config 和 state

2\. 通过 build\_sources 工厂构造所有 Source 对象

3\. 用线程池并发调用所有 Source.fetch（max\_workers=1，实际是顺序执行）

4\. 收集所有 items 和 pending\_state\_updates

5\. 对每个 item 调用 summarize\_item，丢弃低重要性

6\. 把剩余 items 推送到飞书

7\. 提交 state\_updates 到 state.json

```



\### 为什么 max\_workers=1



QQ 邮箱有 IMAP 频率限制——并发登录两次以上会触发"请稍后再试"。踩过这个坑后干脆所有 source 顺序执行。



代价：跑一轮约 30-60 秒。\*\*在 10 分钟一次的调度下完全够用\*\*。



如果以后真的需要并发，应该是按"是否互相影响"分组——例如三个邮箱串行（共享 IMAP 频率限制风险）、爬虫源可以与邮箱并行。但目前没必要做。



\### 失败处理的"三层防御"



第一层（Source 内部）：网络错误、解析错误等捕获后返回空数据。



第二层（主流程并发处理）：每个 future 用 try/except 包裹，单个 source 抛异常不影响其他。



第三层（state 提交）：失败的 source 不产生 state\_updates，下次自然重试。



这三层组合起来意味着：\*\*最坏情况下系统会"暂时无声"——但不会数据损坏、不会崩溃、不会进入无限重试\*\*。



\## 数据持久化：state.json



为什么用 JSON 而不是 SQLite：



\- JSON 可以肉眼看 / 手动改（debug 友好）

\- 整个文件只有几十 KB，性能不是问题

\- 没有并发写需求（每 10 分钟一次）

\- 备份就是 `copy state.json state.json.bak`



JSON 的 schema：



```json

{

&#x20; "version": 2,

&#x20; "sources": {

&#x20;   "imap:QQ:INBOX":          {"last\_uid": 12345},

&#x20;   "imap:Gmail:INBOX":       {"last\_uid": 67},

&#x20;   "imap:BJTU:INBOX":        {"last\_uid": 1754684457},

&#x20;   "bjtu\_internship:BJTU实习": {"seen\_ids": \["intern-001", ...]},

&#x20;   "bjtu\_fair:BJTU宣讲会":     {"seen\_ids": \["fair-001", ...]}

&#x20; }

}

```



\*\*version 字段的作用\*\*：未来如果状态结构需要破坏性变更，可以做版本迁移（v1→v2 已经做过一次）。



\*\*source key 的格式 `{type}:{name}\[:{folder}]`\*\*：保证不同 source 的状态不会相互覆盖，同时人眼可读。



\## 扩展指南：加一个新 Source



参考 `BJTUFairSource` 的实现作为模板，加一个新 JSON API source 大概只需要：



```python

class MyNewSource(JSONAPISource):

&#x20;   source\_type = "my\_new\_source"

&#x20;   

&#x20;   def \_\_init\_\_(self, config):

&#x20;       super().\_\_init\_\_(config)

&#x20;       self.api\_url = config\["api\_url"]

&#x20;   

&#x20;   def \_fetch\_records(self):

&#x20;       resp = requests.get(self.api\_url, timeout=self.timeout)

&#x20;       return resp.json()\["data"]

&#x20;   

&#x20;   def \_record\_to\_item(self, record):

&#x20;       return {

&#x20;           "source\_type": self.source\_type,

&#x20;           "source\_name": self.name,

&#x20;           "item\_id":     f"my:{record\['id']}",

&#x20;           "title":       record\["title"],

&#x20;           "body":        record\["content"],

&#x20;           # ... 其他字段

&#x20;       }



\# 注册到 registry

JSONAPI\_SOURCE\_REGISTRY\["my\_new\_source"] = MyNewSource

```



然后在 config.json 里加一段：



```json

{

&#x20; "name": "MySource",

&#x20; "type": "my\_new\_source",

&#x20; "api\_url": "https://...",

&#x20; "max\_per\_run": 5

}

```



最后给它写一个对应的 prompt 模板（如果摘要逻辑跟现有的差异大）。



\*\*就这样。不需要改 main()、不需要改 state.json schema、不需要改 push\_to\_feishu\*\*。



这就是抽象的回报。



\## 一些"故意没做"的设计决策



每个工程项目都需要一份"不做什么"的清单。本项目主动放弃的：



\*\*不做插件系统\*\*：让 source 通过 import 第三方包注册——抽象太重，对单人项目过度。



\*\*不做配置 schema 校验\*\*：用 jsonschema / pydantic 校验 config——目前用"读不到字段就用默认值"的容错策略，简单且够用。



\*\*不做日志 rotation\*\*：单文件 bot.log 一直增长——每月手动清一次。等真的成为问题再做。



\*\*不做并发优化\*\*：max\_workers=1 + 串行处理已经够用——盲目并发会引入更多 bug。



\*\*不做模块拆分\*\*：保持单文件 1400 行——拆成多个文件后，全文搜索/重构难度反而上升。



\*\*不做可视化界面\*\*：所有交互通过 config.json + 飞书卡片——对工具类项目，文本配置 + 文本输出最稳定。



每一项决定都来自具体场景的权衡，而不是抽象原则。\*\*工程没有正确答案，只有特定场景下的合理选择\*\*。

