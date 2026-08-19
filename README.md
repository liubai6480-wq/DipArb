# DipArb Monitor — Polymarket UP/DOWN 信号监测

7×24 只读监测 Polymarket 5/15 分钟 BTC/ETH/SOL/XRP UP/DOWN 市场，检测三类套利信号
（瞬时暴跌 / 瞬时暴涨 / 定价偏差），模拟两腿成交与盈亏，**每小时生成一份 Markdown 报告**。

回答一个决定性问题：**“3 秒 15% 暴跌”一天到底出现几次？** 先用真实行情频率数据决定
是否值得做实盘（方案 P1）。

> ⚠️ **只读 + 模拟。不下单、不接私钥、不碰钱包。** 符合 radar-only 纪律。

---

## 它做什么

```
gamma-api.polymarket.com  →  找 UP/DOWN 市场（每轮次开始时）
clob.polymarket.com/book  →  订单簿轮询（0.3s/tick）
7 家交易所现货价          →  底层价格（OKX→Bybit→Coinbase→Kraken→Gate→KuCoin→Binance 顺序 failover，1s/tick）
        ↓
信号检测（HANDOFF §5）
  · 3 秒窗口 ≥15% 暴跌    →  模拟 Leg1（买暴跌侧）
  · 3 秒窗口 ≥15% 暴涨    →  模拟 Leg1（买对手侧，预期均值回归）
  · 估计胜率 vs 盘口价偏差 →  模拟 Leg1（买被低估侧）
        ↓
模拟交易状态机
  waiting → leg1_filled → done（对冲完成）/ expired（超时或轮末退出）
        ↓
每小时报告 → reports/YYYYMMDD-HH.md
```

---

## 快速开始

```bash
# 本地自测（跑 60 秒，连真实行情，结束输出一份报告后退出）
python monitor.py --test 60

# 常驻运行（订单簿 0.3s/tick，价格 1s/tick，整点出报告）
python monitor.py
```

零第三方依赖，纯 Python 标准库，Python 3.11+。

---

## 部署到 Railway

### 1. 推到 GitHub

```bash
cd diparb-monitor
git init
git add .
git commit -m "DipArb monitor: P1 signal-frequency monitoring"
git branch -M main
git remote add origin <你的仓库地址>
git push -u origin main
```

###  Railway 新建项目 → Deploy from GitHub repo → 选这个仓库。

3. **添加 Volume**（关键，报告持久化用）：
   Settings → Volumes → 新建，挂载路径填 `/data`。

4. 环境变量（Settings → Variables，均有默认值，可不设）：

   | 变量 | 默认 | 说明 |
   |---|---|---|
   | `MONITOR_COINS` | `btc` | 监测币种 (btc/eth/sol/xrp) |
   | `MONITOR_MINUTES` | `5` | 轮次时长 (5 或 15) |
   | `MONITOR_DIP_THRESHOLD` | `0.15` | 暴跌/暴涨/偏差阈值 |
   | `MONITOR_SUM_TARGET` | `0.92` | 两腿总成本上限（触发 Leg2） |
   | `MONITOR_LEG2_TIMEOUT` | `60` | Leg2 超时秒（超时模拟市价退出） |
   | `REPORT_DIR` | `./reports` | 报告目录（Railway 上设 `/data/reports`） |
   | `DEBUG` | (关) | 设 `1` 开调试日志 |

5. 部署完成，Logs 应显示：
   ```
   DipArb Monitor 启动 | BTC 5m | dip≥0.15 | sumTarget=0.92 | 报告: /data/reports
   新轮次: btc-updown-5m-... | price-to-beat ~$64,xxx
   ```

Procfile 已配置为 `worker` 进程类型（常驻，非 web）。

---

## 每小时报告样例

`reports/20260819-20.md`：

```markdown
# DipArb 监测报告 20260819-20

## 本小时
- 监测市场: BTC 5m
- 本小时轮次: 12
- 本小时信号: dip=2 surge=1 mispricing=0 (总 3)
- 本小时对冲完成: 2 | 模拟退出: 1 | 对冲成功率: 67%
- 本小时模拟成交: 3 笔 | 本小时模拟盈亏: $+0.62

## 信号频率（P1 关键指标 · 用累计值，更稳定）
- 累计监测轮次: 288
- 累计信号: 72 (dip=48 surge=18 mispricing=6)
- 累计信号/轮: 0.250
- 按此估算日信号: 72 (5m 周期每天 288 轮)
- 累计对冲完成: 48 | 累计模拟退出: 24 | 累计对冲成功率: 67%
- 累计模拟盈亏: $+15.40

## 最近价格
- BTC: $64,873.10 (源: OKX)
- UP ask: 0.440 | DOWN ask: 0.550

## 本小时模拟成交明细
- hedge total=0.9100 | pnl=$+0.90
- ...
```

**看什么**：`累计信号/轮` 和 `按此估算日信号`——这是 P1 的核心产出。
如果一周下来日信号个位数且对冲成功率低，说明该策略在你的延迟条件下不值得做实盘。

---

## 配置参数（HANDOFF §9）

| 参数 | 默认 | 含义 |
|---|---|---|
| `dipThreshold` | 0.15 | 三类信号统一阈值（15%） |
| `slidingWindowMs` | 3000 | 价格变动测量窗口（3 秒） |
| `windowMinutes` | 2 | 轮次开始后 Leg1 检测窗口（2 分钟） |
| `sumTarget` | 0.92 | 触发 Leg2 的两腿总成本上限 |
| `leg2Timeout` | 60s | Leg1 成交后等待 Leg2 超时（超时模拟退出） |
| `maxSlippage` | 0.02 | 模拟目标价 = ask × 1.02 |
| `shares` | 10 | 每轮模拟张数 |

---

## 项目结构

```
diparb-monitor/
├── monitor.py          # 主程序（单文件，纯 stdlib）
├── Procfile            # Railway 进程类型：worker: python monitor.py
├── runtime.txt         # Python 版本
├── requirements.txt    # 空（无第三方依赖）
├── .gitignore
├── .env.example        # 环境变量示例
└── README.md
```

---

## 数据源

| 用途 | 源 | 说明 |
|---|---|---|
| 找 UP/DOWN 市场 | `gamma-api.polymarket.com/events?slug=...` | slug = `{coin}-updown-{5m\|15m}-{对齐Unix秒}` |
| 订单簿 | `clob.polymarket.com/book?token_id=...` | asks/bids，0.3s 轮询 |
| 底层价格 | OKX / Bybit / Coinbase / Kraken / Gate / KuCoin / Binance | 顺序 failover，Binance 兜底 |

> Polymarket 官方锚定 Chainlink，但公开 API 无 Chainlink 端点。P1 监测用交易所现货价
> 参考（差异 0.01–0.03%），对 dip/surge 信号无影响，对 mispricing 信号精度有小影响。
> P3 实盘前如需精确对齐可接 Chainlink 数据流。

---

## 已知限制（P1 阶段可接受）

1. 模拟成交按 ask 价全额成交，未计真实滑点/手续费（P2 修正）。
2. 轮询模式（0.3s）vs 专业做市商 WebSocket，延迟差异大——P1 只统计“信号是否出现”，
   不证明“能抢到”。
3. 底层价用交易所现货，mispricing 信号精度受 1–2 秒价差影响（dip/surge 不受影响）。
4. 不处理 CLOB V2 下 merge / 结算（P3 才涉及）。
5. Railway 免费额度有限，长期 7×24 跑需付费计划（$5/月）。

---

## 阶段规划

| 阶段 | 内容 | 产出 |
|---|---|---|
| **P1（本项目）** | Railway 只读监测 + 模拟盈亏 + 每小时报告 | 真实信号频率数据 |
| P2 | 用 P1 数据回测滑点/手续费，验证信号频率 | 是否值得实盘的判断 |
| P3 | 接 CLOB V2 SDK 下单（live 适配器接线） | 可执行版 |
| P4 | $50 小额实盘 + 对比模拟偏差 | 实盘验证 |

**P1 是决定性问题：先证明“3 秒 15% 暴跌”一周出现多少次，再谈赚钱。**

---

## 许可

MIT。策略逻辑源自 [MrFadiAi/Polymarket-bot](https://github.com/MrFadiAi/Polymarket-bot) (MIT)
的 DipArb 规格，本监测器为独立实现。
