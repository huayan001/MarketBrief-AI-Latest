# MarketBrief AI

面向活跃个人投资者的“AI + 量化指标”多市场研究工作台：Free 完成单次研究，Pro 持续监控并节省时间。

第一版只做四件事：

1. 输入股票、指数或币种代码
2. 展示行情和技术指标
3. 生成 AI 或本地规则分析报告
4. 扫描关注列表、多时间段买卖观察信号，并保存历史报告

## 启动

先创建虚拟环境、安装 Python 与 Node 依赖，并在 `api_keys.env` 中填写 MySQL（必填）与可选 SMTP / AI Key：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
npm install
cp api_keys.env.example api_keys.env
```

Waffo Pancake 支付需要 Node.js 20+。

macOS / Linux:

```bash
chmod +x run.sh
./run.sh
```

Windows:

```bat
run.bat
```

打开浏览器访问：

```text
http://127.0.0.1:8787
```

首次使用需邮箱验证码登录。未配置 SMTP 时，验证码会打印在服务端终端日志中。

如果 8787 被占用，终端会显示新的可用端口，例如 `http://127.0.0.1:8788`。

## 示例代码

- 美股：`AAPL`, `NVDA`, `SPY`
- 港股：`0700.HK`
- A 股：`600519`, `002859`, `300408`（也支持 `600519.SS`, `000001.SZ`）
- 日股：`7203.T`
- 英股：`AZN.L`
- 加密资产：`BTC-USD`, `ETH-USD`

## 数据源校验

输入代码后可以点击“查找代码”，工具会从 Yahoo Finance 返回候选标的，帮助确认代码是否查对。

分析结果会显示“数据健康”卡片：

- 行情源名称
- 交易所
- 最后一根 K 线日期
- 可用 K 线数量
- 数据状态：数据正常、历史数据较短、数据可能过期、疑似代码映射错误
- 修正建议只针对疑似把股票代码误写成 `-USD` 的代币化标的（名称含 tokenized / PreStocks）。`BTC-USD` 这类已确认的加密资产不会被标成「疑似代码映射错误」

## AI 配置

不配置 API key 也可以生成本地规则报告。

如果要接入大模型，直接打开项目里的 `api_keys.env`，把你使用的平台 key 填进去，保存后重新启动工具即可。

OpenAI:

```text
OPENAI_API_KEY=你的 OpenAI key
```

DeepSeek:

```text
DEEPSEEK_API_KEY=你的 DeepSeek key
```

OpenRouter:

```text
OPENROUTER_API_KEY=你的 OpenRouter key
```

## 自选股雷达

把标的加入关注列表后，点击“扫描”或“扫描关注”，工具会批量刷新关注标的，并按综合评分、涨跌幅、风险评分或信号强度排序。

扫描结果会展示：

- 当前价格和涨跌幅
- 综合评分与风险评分
- 买入观察、持有观察、等待确认、卖出/回避等信号
- 信号置信度
- 数据源提醒
- 最近信号变化记录

## 自动简报

登录后点击顶部“自动简报”，可以设置：

- 每日执行时间与用户时区
- 最低信号置信度
- 仅推送新信号或信号变化
- 邮件、企业微信、飞书、Telegram

Free 支持邮件简报；Pro 支持四个渠道。后台每 30 秒检查一次到期任务，同一用户同一天只执行一次。没有达到条件的重要变化时会记录任务，但不会发送无意义通知。

邮件复用平台 SMTP。企业微信、飞书和 Telegram 由每位 Pro 用户在工作台中配置，凭据加密入库且接口只返回脱敏摘要。生产环境必须设置独立加密主密钥：

```text
NOTIFICATION_ENCRYPTION_KEY=使用-openssl-rand-base64-32-生成
```

“立即生成一次”可用于验证分析链路。实际发送前请先配置对应渠道；发送成功或失败都会记录在“最近推送”。

第二轮可靠性增强：通知失败会指数退避重试 3 次；服务重启后会补跑当天已到时间且尚未执行的任务；行情主源失败时会切换备用域名，并可使用 6 小时内的进程缓存兜底。“测试所选渠道”可以单独检查凭据和网络连接。

生产环境建议将 Web 服务与定时任务拆开运行：

```text
# Web 进程
BRIEF_SCHEDULER_MODE=external ./run.sh

# 独立任务进程
./.venv/bin/python brief_worker.py
```

健康检查为 `GET /api/health`，仅返回数据库、调度模式和各通知渠道是否已配置，不暴露密钥。

第三轮加入了可运营闭环：自动简报窗口会检查“关注列表、渠道凭据、启用开关”三项首次配置；展示最近任务及各渠道发送结果；失败通知可在不重新分析、不重复扣额度的情况下单独重发。顶部“反馈”入口会把功能异常、报告质量、推送和定价反馈保存到 `product_feedback` 表，便于早期人工回访和排优先级。

生产环境还应配置：

```text
APP_ENV=production
COOKIE_SECURE=1
```

## Free + Pro 套餐

基础行情、新闻、技术指标图、数据健康和本地规则信号永久开放。服务端统一校验套餐与用量，不能通过直接调用 API 绕过。

Free：

- 每日 10 次单标分析
- 5 个关注标的
- 每日 1 次雷达扫描，每次最多 5 个标的
- 每月 5 次 AI 深度报告；额度用完后自动回落为本地规则报告
- 保留 10 份报告和 7 天信号变化

Pro `US$19.99/月`：

- 每日 200 次单标分析
- 30 个关注标的
- 每日 10 次雷达扫描，每次最多 30 个标的
- 每月 100 次 AI 深度报告
- 保留 200 份报告和 90 天信号变化

上线时已有用户会获得一次性 14 天 Pro 赠送期。登录后点击顶部“套餐与用量”可以查看当前套餐、剩余额度、续费/取消状态和套餐对比。

## Waffo Pancake 测试订阅

当前接入会在 Waffo Test 创建或复用 `MarketBrief AI Pro Monthly` 月度订阅商品，价格为
`USD 19.99/month`。生产商品和 Production Key 不会被自动创建。

1. 打开 Waffo Dashboard → **API & Development**，在页面顶部复制 Merchant ID。
2. 在同页 **API Keys** 中创建 **Test** API Key，并立即下载私钥；私钥不会再次显示。
3. 只在本地 `api_keys.env` 中加入以下两个 Waffo 变量，不要把真实私钥粘贴到聊天或提交到 Git：

```text
WAFFO_MERCHANT_ID=MER_xxx
WAFFO_PRIVATE_KEY="-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----"
```

商户应只有一个 Store。首次发起 checkout 时，程序会查找或创建带固定标记的
`MarketBrief AI Pro Monthly` 测试订阅商品；Store ID 和 Product ID 不需要环境变量。

启动应用后，用公开 HTTPS 地址注册测试 webhook（地址必须转发到当前应用）：

```bash
npm run waffo:webhook -- "https://your-test-host.example/api/payments/webhook"
```

登录工作台并点击“升级 Pro”。结账页会在新标签打开，使用成功测试卡：

```text
卡号：4576750000000110
有效期：任意未来日期
CVC：任意值
```

成功后，页面应显示“Pro 订阅已激活”，服务端应打印包含
`[waffo] subscription.activated`、`linked=True` 的日志；同时可在 Dashboard 的
webhook delivery 记录中确认 HTTP 200。

测试 webhook 会验签、幂等处理并校验用户归属、商品、币种、金额和计费周期。当前状态机处理：

- `subscription.activated`
- `subscription.payment_succeeded`
- `subscription.canceling`
- `subscription.uncanceled`
- `subscription.canceled`
- `subscription.past_due`

`canceling` 在当前周期结束前仍保留 Pro；`canceled` 或权益到期后降级到 Free。`past_due`
提供 3 天宽限期。权益只由服务端已验签 webhook、订阅状态和周期结束时间决定。

## 数据和存储

- 行情数据来自 Yahoo Finance chart API
- 用户、报告、关注列表、信号记录、订阅和用量计数保存在 MySQL
- 登录方式为邮箱验证码（无密码）
- 不自动下单，不连接券商账户
- 买卖信号和多时间段信号仅用于研究观察，不构成交易指令

## 免责声明

本工具仅用于研究和信息分析，不构成投资建议、交易建议或自动交易指令。
