#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DipArb Monitor — Polymarket UP/DOWN 信号监测 + 模拟盈亏 + 每小时报告
====================================================================
纯 Python stdlib，零第三方依赖。部署在 Railway（或任意 Python 3.11 环境）
7x24 只读监测：抓真实订单簿 + 真实底层价格，按 HANDOFF §5 检测三类信号
（瞬时暴跌 / 瞬时暴涨 / 定价偏差），模拟两腿成交与盈亏，每小时生成一份
Markdown 报告，用来回答 P1 决定性问题：“3 秒 15% 暴跌”一天到底出现几次。

安全边界：只读监测 + 模拟成交。不下单、不接私钥、不碰钱包。

数据源:
  - gamma-api.polymarket.com  找 UP/DOWN 市场 (slug={coin}-updown-{5m|15m}-{ts})
  - clob.polymarket.com/book   订单簿 (asks/bids)
  - 7 家交易所现货价 (OKX/Bybit/Coinbase/Kraken/Gate/KuCoin/Binance 顺序 failover)

用法:
  python monitor.py              常驻运行（订单簿 0.3s/tick，价格 1s/tick，整点出报告）
  python monitor.py --test 60    自测：跑 60 秒后输出一份报告并退出

环境变量（均有默认值）:
  MONITOR_COINS=btc              监测币种 (btc/eth/sol/xrp，逗号分隔取第一个)
  MONITOR_MINUTES=5              轮次时长 (5 或 15，逗号分隔取第一个)
  MONITOR_DIP_THRESHOLD=0.15     暴跌/暴涨/偏差阈值
  MONITOR_SUM_TARGET=0.92        两腿总成本上限（触发 Leg2）
  MONITOR_LEG2_TIMEOUT=60        Leg1 成交后等待 Leg2 的超时秒（超时则模拟市价退出）
  REPORT_DIR=./reports           报告输出目录
  DEBUG=1                        开启调试日志
"""

import datetime
import json
import os
import sys
import time
import urllib.request

# ==================== 配置 ====================

COIN = os.environ.get("MONITOR_COINS", "btc").split(",")[0].strip().lower()
MINUTES = int(os.environ.get("MONITOR_MINUTES", "5").split(",")[0].strip())
DIP_THRESHOLD = float(os.environ.get("MONITOR_DIP_THRESHOLD", "0.15"))
SURGE_THRESHOLD = DIP_THRESHOLD  # 与 dip 同阈值（HANDOFF §9）
SUM_TARGET = float(os.environ.get("MONITOR_SUM_TARGET", "0.92"))
LEG2_TIMEOUT_S = int(os.environ.get("MONITOR_LEG2_TIMEOUT", "60"))
SLIDING_WINDOW_MS = 3000
WINDOW_MINUTES = 2
MAX_SLIPPAGE = 0.02
SHARES = 10
ENABLE_SURGE = True
REPORT_DIR = os.environ.get("REPORT_DIR", "./reports")

TICK_BOOK_S = 0.3       # 订单簿轮询间隔
TICK_PRICE_S = 1.0      # 底层价格轮询间隔
MAX_HISTORY = 100       # 价格历史条数上限（HANDOFF §3.2）
SIGNAL_COOLDOWN_MS = 1000

DEBUG = os.environ.get("DEBUG") == "1"


# ==================== 工具 ====================

def http_get(url, timeout=8):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def now_ms():
    return int(time.time() * 1000)


def aligned_ts(ts, minutes):
    """对齐到 minutes 分钟边界（Unix 秒）。"""
    return ts - (ts % (minutes * 60))


class Logger:
    def __init__(self, debug=False):
        self.debug = debug

    def log(self, msg):
        print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

    def d(self, msg):
        if self.debug:
            self.log("  " + msg)


log = Logger(debug=DEBUG)


# ==================== 统计（累计计数，报告按小时取增量） ====================

class Stats:
    """累计事件计数器。报告每小时取一次 delta，得到“本小时”的真实数字。"""

    def __init__(self):
        self.rounds = 0          # 监测过的轮次数
        self.dip = 0             # 成交的 dip 信号数
        self.surge = 0           # 成交的 surge 信号数
        self.mispricing = 0      # 成交的 mispricing 信号数
        self.leg1_filled = 0     # 模拟 Leg1 成交数 = 接受的信号总数
        self.leg2_filled = 0     # 模拟完成对冲数
        self.leg2_exits = 0      # 模拟紧急/轮末退出数

    def snapshot(self):
        return {
            "rounds": self.rounds, "dip": self.dip, "surge": self.surge,
            "mispricing": self.mispricing, "leg1_filled": self.leg1_filled,
            "leg2_filled": self.leg2_filled, "leg2_exits": self.leg2_exits,
        }

    def delta(self, prev):
        s = self.snapshot()
        return {k: s[k] - prev.get(k, 0) for k in s}


# ==================== 行情层 ====================

class MarketFeed:
    """找市场 + 订单簿 + 底层价格（多源 failover）。"""

    def __init__(self):
        self.coin = COIN
        self.minutes = MINUTES
        self.up_token = None
        self.down_token = None
        self.condition_id = None
        self.round_key = None            # 当前轮次标识
        self.up_asks = []                # [(price, size), ...] 升序
        self.down_asks = []
        self.underlying = 0.0            # 最新底层价
        self.price_source = ""           # 当前生效的价格源
        self.price_history = []          # [{ts, up, down}]
        self.round_open_prices = None    # (up, down)
        self.price_to_beat = 0.0
        self.round_start_ms = 0

    # --- 市场查找 ---
    def resolve_round(self):
        """按当前时间对齐轮次，拿到 up/down token。返回 True=已进入新轮次。"""
        ts = aligned_ts(int(time.time()), self.minutes)
        key = f"{self.coin}-{self.minutes}m-{ts}"
        if key == self.round_key:
            return False
        slug = f"{self.coin}-updown-{self.minutes}m-{ts}"
        try:
            events = http_get(f"https://gamma-api.polymarket.com/events?slug={slug}")
            if not events:
                log.d(f"市场 {slug} 未找到（可能尚未创建）")
                return False
            m = events[0]["markets"][0]
            ids = json.loads(m["clobTokenIds"])  # clobTokenIds 是 JSON 字符串，需二次解析
            self.up_token, self.down_token = ids[0], ids[1]
            self.condition_id = m.get("conditionId")
            self.round_key = key
            self.round_start_ms = ts * 1000
            self.price_history = []          # 新轮清空历史（HANDOFF §4.1.e）
            self.up_asks, self.down_asks = [], []
            self.round_open_prices = None
            self.price_to_beat = self.underlying or 0
            log.log(f"新轮次: {slug} | price-to-beat ~${self.price_to_beat:,.2f}")
            return True
        except Exception as e:
            log.d(f"resolve_round 失败: {e}")
            return False

    # --- 订单簿 ---
    def fetch_book(self, token_id):
        if not token_id:
            return []
        try:
            book = http_get(f"https://clob.polymarket.com/book?token_id={token_id}", timeout=6)
            asks = [(float(l["price"]), float(l["size"])) for l in book.get("asks", [])]
            asks.sort(key=lambda x: x[0])   # 升序：[0] = best(最低) ask
            return asks
        except Exception:
            return []

    def tick_books(self):
        self.up_asks = self.fetch_book(self.up_token)
        self.down_asks = self.fetch_book(self.down_token)
        if self.up_asks and self.down_asks:
            up = self.up_asks[0][0]
            down = self.down_asks[0][0]
            if self.round_open_prices is None:
                self.round_open_prices = (up, down)
            # HANDOFF §3.2/§8.8：两侧价格都有效时才记录历史
            self.price_history.append({"ts": now_ms(), "up": up, "down": down})
            if len(self.price_history) > MAX_HISTORY:
                self.price_history = self.price_history[-MAX_HISTORY:]

    # --- 底层价格（多源 failover）---
    @staticmethod
    def _price_sources(coin):
        base_map = {"btc": "BTC", "eth": "ETH", "sol": "SOL", "xrp": "XRP"}
        base = base_map.get(coin, "")
        if not base:
            return []
        kraken_base = "XBT" if base == "BTC" else base  # Kraken 用 XBT 表示 BTC
        return [
            # (名称, URL, 解析函数) —— Binance 放最后兜底（数据中心 IP 可能被风控）
            ("OKX", f"https://www.okx.com/api/v5/market/ticker?instId={base}-USDT",
             lambda r: r["data"][0]["last"]),
            ("Bybit", f"https://api.bybit.com/v5/market/tickers?category=spot&symbol={base}USDT",
             lambda r: r["result"]["list"][0]["lastPrice"]),
            ("Coinbase", f"https://api.coinbase.com/v2/prices/{base}-USD/spot",
             lambda r: r["data"]["amount"]),
            ("Kraken", f"https://api.kraken.com/0/public/Ticker?pair={kraken_base}USD",
             lambda r: list(r["result"].values())[0]["c"][0]),
            ("Gate", f"https://api.gateio.ws/api/v4/spot/tickers?currency_pair={base}_USDT",
             lambda r: r[0]["last"]),
            ("KuCoin", f"https://api.kucoin.com/api/v1/market/orderbook/level1?symbol={base}-USDT",
             lambda r: r["data"]["price"]),
            ("Binance", f"https://api.binance.com/api/v3/ticker/price?symbol={base}USDT",
             lambda r: r["price"]),
        ]

    def tick_price(self):
        for name, url, parse in self._price_sources(self.coin):
            try:
                r = http_get(url, timeout=6)
                price = float(parse(r))
                if price > 0:
                    self.underlying = price
                    self.price_source = name
                    return
            except Exception:
                continue
        log.d("⚠️ 所有价格源失败，保留旧值")

    def best_ask(self, side):
        arr = self.up_asks if side == "UP" else self.down_asks
        return arr[0][0] if arr else 1.0  # 空盘口返回 1.0（无效，检测会跳过）

    def price_at(self, side, ms_ago):
        """滑动窗口查询：返回 ms_ago 毫秒前的价格，找不到返回 None（HANDOFF §3.2）。"""
        target = now_ms() - ms_ago
        for e in reversed(self.price_history):
            if e["ts"] <= target:
                return e["up"] if side == "UP" else e["down"]
        return None


# ==================== 信号检测（HANDOFF §5 移植，纯函数，不计数） ====================

class SignalDetector:
    """返回 (type, side, drop, price) 或 None。计数由 PaperTrader 在接受信号时做。"""

    def __init__(self, feed):
        self.feed = feed

    @staticmethod
    def estimate_up_win_rate(cur, beat):
        if beat <= 0:
            return 0.5
        shift = ((cur - beat) / beat) * 10  # 敏感度 10：底层 ±1% → 胜率 ±10%
        return max(0.05, min(0.95, 0.5 + shift))

    def detect(self):
        f = self.feed
        if not f.round_open_prices:
            return None
        # 窗口：轮次开始后 WINDOW_MINUTES 分钟内才检测 Leg1（HANDOFF §5.2.a）
        elapsed_min = (now_ms() - f.round_start_ms) / 60000
        if elapsed_min > WINDOW_MINUTES:
            return None
        up, down = f.best_ask("UP"), f.best_ask("DOWN")
        if up >= 1 or down >= 1:           # 无效价格
            return None
        up_ago = f.price_at("UP", SLIDING_WINDOW_MS)
        down_ago = f.price_at("DOWN", SLIDING_WINDOW_MS)

        # Pattern 1: 瞬时暴跌（买暴跌侧）
        if up_ago and up_ago > 0:
            drop = (up_ago - up) / up_ago
            if drop >= DIP_THRESHOLD:
                return ("dip", "UP", drop, up)
        if down_ago and down_ago > 0:
            drop = (down_ago - down) / down_ago
            if drop >= DIP_THRESHOLD:
                return ("dip", "DOWN", drop, down)

        # Pattern 2: 瞬时暴涨 → 买对手方（预期均值回归）
        if ENABLE_SURGE and up_ago and down_ago:
            if up_ago > 0:
                surge = (up - up_ago) / up_ago
                if surge >= SURGE_THRESHOLD:
                    return ("surge", "DOWN", surge, down)
            if down_ago > 0:
                surge = (down - down_ago) / down_ago
                if surge >= SURGE_THRESHOLD:
                    return ("surge", "UP", surge, up)

        # Pattern 3: 定价偏差（估计胜率 vs 盘口价）
        if f.price_to_beat > 0 and f.underlying > 0:
            wr = self.estimate_up_win_rate(f.underlying, f.price_to_beat)
            up_mis = wr - up
            down_mis = (1 - wr) - down
            if up_mis >= DIP_THRESHOLD:
                return ("mispricing", "UP", up_mis, up)
            if down_mis >= DIP_THRESHOLD:
                return ("mispricing", "DOWN", down_mis, down)
        return None


# ==================== 模拟交易层 ====================

class PaperTrader:
    """状态机：waiting → leg1_filled → done | expired（单轮内只接受一次 Leg1）。"""

    def __init__(self, feed, stats):
        self.feed = feed
        self.stats = stats
        self.leg1 = None          # {side, price, shares, ts}
        self.phase = "waiting"    # waiting | leg1_filled | done | expired
        self.trades = []          # 累计完成的模拟交易
        self.last_signal_ms = 0

    def _leg2_total(self):
        if not self.leg1:
            return None
        hedge = "DOWN" if self.leg1["side"] == "UP" else "UP"
        cur = self.feed.best_ask(hedge)
        if cur >= 1:
            return None
        return self.leg1["price"] + cur * (1 + MAX_SLIPPAGE)

    def on_signal(self, sig):
        sig_type, side, drop, price = sig
        now = now_ms()
        if now - self.last_signal_ms < SIGNAL_COOLDOWN_MS:
            return
        self.last_signal_ms = now
        if self.phase != "waiting":
            return
        # 接受信号 → 模拟 Leg1 成交（按目标价 = ask*(1+maxSlippage)）
        target = price * (1 + MAX_SLIPPAGE)
        self.leg1 = {"side": side, "price": target, "shares": SHARES, "ts": now}
        self.phase = "leg1_filled"
        self.stats.leg1_filled += 1
        self.stats.__dict__[sig_type] += 1
        log.log(f"🎯 模拟 Leg1: {side} x{SHARES} @ {target:.4f} ({sig_type} {drop * 100:.1f}%)")

    def check_leg2(self):
        if self.phase != "leg1_filled" or not self.leg1:
            return
        # Leg2 超时 → 模拟市价退出（卖在当前 best ask）
        if now_ms() - self.leg1["ts"] > LEG2_TIMEOUT_S * 1000:
            self._exit("leg2_timeout")
            return
        total = self._leg2_total()
        if total is not None and total <= SUM_TARGET:
            profit = (1 - total) * self.leg1["shares"]
            self.trades.append({"pnl": profit, "note": f"hedge total={total:.4f}", "type": "hedge"})
            self.phase = "done"
            self.stats.leg2_filled += 1
            log.log(f"💰 模拟完成对冲: 成本 {total:.4f} | 模拟利润 ${profit:.2f}")

    def force_exit_at_round_end(self):
        """轮次结束时若仍持有未对冲 Leg1，按当前价模拟退出，避免交易被静默丢弃。"""
        if self.phase == "leg1_filled":
            self._exit("round_end_exit")

    def _exit(self, reason):
        if not self.leg1:
            return
        exit_price = self.feed.best_ask(self.leg1["side"])
        if exit_price >= 1:                 # 盘口空 → 假设平价退出（保守取 0 损益）
            exit_price = self.leg1["price"]
        pnl = (exit_price - self.leg1["price"]) * self.leg1["shares"]
        self.trades.append({"pnl": pnl, "note": f"{reason} exit @ {exit_price:.4f}", "type": "exit"})
        self.phase = "expired"
        self.stats.leg2_exits += 1
        log.log(f"⚠️ 模拟退出({reason}): {self.leg1['side']} @ {exit_price:.4f} | 模拟盈亏 ${pnl:+.2f}")

    def reset_for_new_round(self, is_new_round):
        if is_new_round:
            self.force_exit_at_round_end()
            self.phase = "waiting"
            self.leg1 = None


# ==================== 报告层（每小时取增量，数字正确） ====================

class Reporter:
    def __init__(self):
        os.makedirs(REPORT_DIR, exist_ok=True)
        self.hour_key = None
        self.stats_snapshot = {}
        self.trade_count = 0

    def _roll_forward(self, stats, trader):
        self.stats_snapshot = stats.snapshot()
        self.trade_count = len(trader.trades)

    def maybe_report(self, stats, trader, feed, force=False):
        key = datetime.datetime.now().strftime("%Y%m%d-%H")
        if self.hour_key is None:        # 启动：建立基线，不出报告
            self.hour_key = key
            self._roll_forward(stats, trader)
            return
        if not force and key == self.hour_key:
            return
        self._write(stats, trader, feed, key)
        self.hour_key = key
        self._roll_forward(stats, trader)

    def _write(self, stats, trader, feed, key):
        delta = stats.delta(self.stats_snapshot)
        snap = stats.snapshot()
        new_trades = trader.trades[self.trade_count:]
        pnl = sum(t["pnl"] for t in new_trades)
        h_hedges = delta["leg2_filled"]
        h_exits = delta["leg2_exits"]
        h_sig = delta["leg1_filled"]
        h_rounds = delta["rounds"]
        hedge_rate = (h_hedges / h_sig * 100) if h_sig else 0
        # P1 关键指标用累计值：长时间运行下累计信号/累计轮次最稳定，
        # 单小时 delta 因轮次/信号时序会错位（首末轮半截），累计口径不受影响。
        cum_rounds = max(snap["rounds"], 1)
        cum_sig = snap["leg1_filled"]
        cum_hedges = snap["leg2_filled"]
        cum_exits = snap["leg2_exits"]
        cum_pnl = sum(t["pnl"] for t in trader.trades)
        cum_hedge_rate = (cum_hedges / cum_sig * 100) if cum_sig else 0
        rounds_per_day = 1440 // MINUTES
        sig_per_round = cum_sig / cum_rounds
        lines = [
            f"# DipArb 监测报告 {key}",
            "",
            "## 本小时",
            f"- 监测市场: {COIN.upper()} {MINUTES}m",
            f"- 本小时轮次: {h_rounds}",
            f"- 本小时信号: dip={delta['dip']} surge={delta['surge']} mispricing={delta['mispricing']} (总 {h_sig})",
            f"- 本小时对冲完成: {h_hedges} | 模拟退出: {h_exits} | 对冲成功率: {hedge_rate:.0f}%",
            f"- 本小时模拟成交: {len(new_trades)} 笔 | 本小时模拟盈亏: ${pnl:+.2f}",
            "",
            "## 信号频率（P1 关键指标 · 用累计值，更稳定）",
            f"- 累计监测轮次: {snap['rounds']}",
            f"- 累计信号: {cum_sig} (dip={snap['dip']} surge={snap['surge']} mispricing={snap['mispricing']})",
            f"- 累计信号/轮: {sig_per_round:.3f}",
            f"- 按此估算日信号: {sig_per_round * rounds_per_day:.1f} ({MINUTES}m 周期每天 {rounds_per_day} 轮)",
            f"- 累计对冲完成: {cum_hedges} | 累计模拟退出: {cum_exits} | 累计对冲成功率: {cum_hedge_rate:.0f}%",
            f"- 累计模拟盈亏: ${cum_pnl:+.2f}",
            "",
            "## 最近价格",
            f"- {COIN.upper()}: ${feed.underlying:,.2f} (源: {feed.price_source or 'N/A'})",
            f"- UP ask: {feed.best_ask('UP'):.3f} | DOWN ask: {feed.best_ask('DOWN'):.3f}",
            "",
            "## 本小时模拟成交明细",
        ]
        for t in new_trades[-20:]:
            lines.append(f"- {t['note']} | pnl=${t['pnl']:+.2f}")
        if not new_trades:
            lines.append("- (无)")
        path = os.path.join(REPORT_DIR, f"{key}.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        log.log(f"📊 报告: {path} | 累计信号 {cum_sig}/{snap['rounds']}轮 | 本小时信号 {h_sig}/轮次 {h_rounds} | 累计盈亏 ${cum_pnl:+.2f}")


# ==================== 主循环 ====================

def parse_test_seconds():
    if "--test" in sys.argv:
        i = sys.argv.index("--test")
        if i + 1 < len(sys.argv):
            try:
                return int(sys.argv[i + 1])
            except ValueError:
                pass
    return 0


def main():
    test_seconds = parse_test_seconds()

    stats = Stats()
    feed = MarketFeed()
    detector = SignalDetector(feed)
    trader = PaperTrader(feed, stats)
    reporter = Reporter()

    log.log(f"DipArb Monitor 启动 | {COIN.upper()} {MINUTES}m | dip≥{DIP_THRESHOLD} | sumTarget={SUM_TARGET} | 报告: {REPORT_DIR}")
    start = time.time()
    last_book = 0.0
    last_price = 0.0
    # 启动即建立统计基线（出第一份报告前的参照点），避免首轮计数被 delta 抵消。
    reporter.maybe_report(stats, trader, feed)

    while True:
        now = time.time()
        # 底层价格 1s 一次
        if now - last_price >= TICK_PRICE_S:
            feed.tick_price()
            last_price = now
        # 订单簿 0.3s 一次
        if now - last_book >= TICK_BOOK_S:
            is_new = feed.resolve_round()
            if is_new:
                stats.rounds += 1
            trader.reset_for_new_round(is_new)
            feed.tick_books()
            last_book = now
            # Leg1 只在 waiting 阶段检测（一轮一次，避免重复计数）
            if trader.phase == "waiting":
                sig = detector.detect()
                if sig:
                    trader.on_signal(sig)
            trader.check_leg2()
        # 每小时报告
        reporter.maybe_report(stats, trader, feed)
        # 自测模式
        if test_seconds and time.time() - start > test_seconds:
            log.log("自测结束，输出最终报告并退出")
            reporter.maybe_report(stats, trader, feed, force=True)
            break
        time.sleep(0.05)


if __name__ == "__main__":
    main()
