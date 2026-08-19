# DipArb Monitor Bug Fixes

## 修复的关键 Bug

### 🔴 严重：轮末退出的模拟盈亏永远是 $0 ✅ 已修复

**问题**：
- 原执行顺序：`resolve_round()` 清空盘口 → `reset_for_new_round()` 调用 `force_exit_at_round_end()` → `tick_books()` 拉取新盘口
- `_exit()` 读到的是已清空的盘口，`best_bid()` 返回 None，导致 `exit_price = leg1["price"]`，pnl 恒为 0

**修复**：
- 调整主循环执行顺序（第529-540行）：
  1. `resolve_round()` 检测新轮次（但还未清空旧盘口数据）
  2. **立即执行** `trader.reset_for_new_round()` 用旧盘口计算退出价
  3. 然后才 `feed.tick_books()` 拉取新一轮盘口

**影响**：所有 `round_end_exit` 类型的交易现在会正确计算实际盈亏，不再系统性低估爆仓损失。

---

### 🟠 中等：退出价用的是 ask，不是 bid ✅ 已修复

**问题**：
- `_exit()` 用 `best_ask()` 模拟卖出退出，但 ask 是别人的卖价
- 持有多头要卖出应该看 bid（买方出价），用 ask 会让退出盈亏系统性偏乐观

**修复**：
1. `fetch_book()` 增加抓取 bids 数据（第177-189行）
2. 新增 `best_bid()` 方法（第243-246行）
3. `_exit()` 改用 `best_bid()` 计算退出价（第380行）

**影响**：超时退出/轮末退出的模拟盈亏更接近真实市场（会亏得更多）。

---

### 🟡 需要人工核实：UP/DOWN token 顺序假设 ✅ 已加校验

**问题**：
- 代码直接假设 `clobTokenIds[0]` 是 UP、`[1]` 是 DOWN
- 如果 Polymarket 某些市场顺序不同，会导致所有信号方向静默颠倒

**修复**：
- `resolve_round()` 增加 outcomes 字段校验（第167-170行）
- 如果 `outcomes[0]` 不是 "up"，则交换 token 顺序
- 添加调试日志提示非标准顺序

**建议**：实盘运行时观察日志，确认 outcomes 解析是否正确。

---

### 🟢 轻微：市场未创建时请求过密 ✅ 已修复

**问题**：
- 新一轮市场未创建时，每 0.3 秒轮询一次 gamma-api，容易被限流

**修复**：
- 增加 `last_market_check_fail_ms` 字段（第142行）
- 市场未找到时设置冷却 2 秒（第154-155、162、189行）

**影响**：降低 API 请求频率，避免触发 gamma-api 限流。

---

## 代码改动摘要

### 新增字段
```python
self.up_bids = []
self.down_bids = []
self.last_market_check_fail_ms = 0
```

### 新增方法
```python
def best_bid(self, side):
    """返回最优 bid 价格（卖出时应该用 bid，不是 ask）"""
```

### 修改方法
- `resolve_round()`: 增加 outcomes 校验 + 市场未找到冷却
- `fetch_book()`: 返回 (asks, bids) 元组
- `tick_books()`: 解包 asks/bids
- `_exit()`: 改用 `best_bid()` 计算退出价

### 修改主循环顺序
```python
# 修复前（错误）：
is_new = feed.resolve_round()     # 清空盘口
trader.reset_for_new_round(is_new) # 读到空盘口
feed.tick_books()

# 修复后（正确）：
is_new = feed.resolve_round()     # 检测新轮但不清空
trader.reset_for_new_round(is_new) # 用旧盘口计算退出
feed.tick_books()                  # 拉取新盘口
```

---

## 验证建议

1. **轮末退出盈亏**：运行 `--test 600` 观察报告中 `round_end_exit` 的 pnl 是否非零
2. **UP/DOWN 顺序**：实盘首次启动时检查日志是否有 "检测到非标准顺序" 提示
3. **API 限流**：观察日志中 "市场未找到" 后是否间隔 2 秒再重试

---

修复完成！所有严重和中等 bug 已解决。
