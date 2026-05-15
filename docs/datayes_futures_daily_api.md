# DataYes 期货日行情API使用说明

## API接口

**函数名**: `DataAPI.MktMFutdGet`

**功能**: 获取国内期货日行情信息（底层数据与期货日行情一致，区别是此接口可从主力连续合约角度获取连续行情）

## 参数说明

| 参数名 | 类型 | 描述 |
|--------|------|------|
| mainCon | int | 是否主力（持仓量），1-是；0-否。mainCon、contractMark、contractObject、tradeDate至少选择一个 |
| contractMark | str | 连续合约标志。mainCon、contractMark、contractObject、tradeDate至少选择一个 |
| contractObject | str | 期货合约标的代码（如M、RB、TA）。mainCon、contractMark、contractObject、tradeDate至少选择一个 |
| tradeDate | str | 交易日期，格式YYYYMMDD。mainCon、contractMark、contractObject、tradeDate至少选择一个 |
| startDate | str | 查询起始日期，格式"YYYYMMDD"，可空 |
| endDate | str | 查询截止日期，格式"YYYYMMDD"，可空 |
| field | list | 所需字段列表，可空 |
| pandas | str | 1表示返回pandas data frame，0表示返回csv，可空 |

**重要说明**：
- 至少需要选择：mainCon、contractMark、contractObject、tradeDate 中的一个参数
- **不支持使用ticker作为查询参数**（只能查询后从返回值中获取）
- 中金所结算价的更新时间为16:20
- 上期所无成交合约的结算价有可能在16:00左右修正

## 返回值说明

| 字段名 | 类型 | 描述 |
|--------|------|------|
| secID | str | 通联编制的证券编码 |
| ticker | str | **通用交易代码**（如m2601、m2605） |
| exchangeCD | str | 通联编制的交易市场编码 |
| secShortName | str | 合约简称 |
| secShortNameEN | str | 合约英文简称 |
| tradeDate | str | 交易日期 |
| contractObject | str | 期货合约标的代码（M、RB、TA等） |
| contractMark | str | 连续合约标志 |
| preSettlePrice | float | 昨结算价 |
| preClosePrice | float | 昨收盘价 |
| openPrice | float | 开盘价 |
| highestPrice | float | 最高价 |
| lowestPrice | float | 最低价 |
| settlePrice | float | **结算价** |
| closePrice | float | 今收盘价 |
| turnoverVol | int | 成交量（手），单边计算 |
| turnoverValue | float | 成交金额，单边计算 |
| openInt | int | 持仓量（手），单边计算 |
| chg | float | 涨跌（收盘价-昨结算） |
| chg1 | float | 涨跌1（今结算价-昨结算） |
| chgPct | float | 涨跌幅（（收盘价-昨结算价）/昨结算价） |
| mainCon | int | 是否主力（持仓量），1-是；0-否 |
| smainCon | int | 是否次主力（持仓量），1-是；0-否 |

## 使用示例

### 示例1：查询主力合约数据
```python
import pandas as pd
from datayes.api import DataAPI

# 查询M品种的主力合约数据
df = DataAPI.MktMFutdGet(
    mainCon=1,              # 查询主力合约
    contractObject="M",     # M品种（豆粕）
    startDate="20251101",
    endDate="20251130",
    pandas="1"
)
```

### 示例2：查询特定日期的行情
```python
# 查询2025年11月28日的所有合约数据
df = DataAPI.MktMFutdGet(
    tradeDate="20251128",
    pandas="1"
)
```

### 示例3：查询多个品种
```python
# 分别查询M、RB、TA的主力合约
for underlying_code in ["M", "RB", "TA"]:
    df = DataAPI.MktMFutdGet(
        mainCon=1,
        contractObject=underlying_code,
    startDate="20251101",
        endDate="20251130",
        pandas="1"
    )
```

## 关键要点

### 1. 如何获取特定合约（如m2601）的数据？
**错误方法**：
```python
# ❌ API不支持ticker作为查询参数
df = DataAPI.MktMFutdGet(ticker="m2601")
```

**正确方法**：
```python
# ✅ 使用contractObject查询，然后从返回结果中筛选
df = DataAPI.MktMFutdGet(
    contractObject="M",  # 查询M品种所有合约
    startDate="20250101",
    endDate="20250530",
    pandas="1"
)

# 从返回结果中筛选m2601
df_m2601 = df[df['ticker'] == 'm2601']
```

### 2. 主力合约识别
- 主力合约是按**持仓量**计算的
- 完整的主力连续标记可从`getMktFutPre`获取
- 返回值中的`mainCon=1`表示该合约是主力合约

### 3. 结算价注意事项
- **中金所**：结算价更新时间为16:20
- **上期所**：无成交合约的结算价可能在16:00左右修正
- 使用时注意时间差

## 在AgentQuant中的应用

### 换约逻辑（optimization2修复10）

**位置**：`src/apis/datayes/api.py`

**逻辑**：
1. 换约时需要获取旧合约（如m2601）的最新价格
2. 使用`contractObject + mainCon=1`查询主力合约数据
3. 从返回结果中筛选旧合约的ticker

**代码示例**：
```python
# 查询M品种主力合约数据（包含m2601和m2605）
old_contract_quotes = router.api.get_futures_daily_candles_optimized(
    underlying_code="M",     # 品种代码
    is_main=1,               # 查询主力合约
    start_date=trading_date - timedelta(days=5),
    end_date=trading_date + timedelta(days=1)
)

# 从主力合约数据中筛选m2601的价格
for quote in reversed(old_contract_quotes):
    if quote.ticker == "m2601":
        old_contract_price = quote.settle_price or quote.close_price
        break
```

## 更新日期

**创建日期**：2026-03-17
**数据来源**：DataYes API官方文档
