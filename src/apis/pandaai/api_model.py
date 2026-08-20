"""
PandaAI futures data models used by agent-future-trading.
"""

from pydantic import BaseModel, Field
from typing import Optional, Dict


class FuturesContract(BaseModel):
    """中国期货合约基本信息模型"""
    contract_id: str = Field(description="合约代码，如 IF2501")
    underlying_code: str = Field(description="标的代码，如 IF")
    contract_name: str = Field(description="合约名称")
    exchange: str = Field(description="交易所代码")
    list_date: str = Field(description="上市日期")
    last_trade_date: str = Field(description="最后交易日")
    contract_multiplier: float = Field(description="合约乘数")
    trading_unit: str = Field(description="交易单位")


class FuturesDailyQuote(BaseModel):
    """中国期货日频行情数据模型"""
    contract_id: str = Field(description="合约代码")
    trade_date: str = Field(description="交易日期")
    open: float = Field(description="开盘价")
    high: float = Field(description="最高价")
    low: float = Field(description="最低价")
    close: float = Field(description="收盘价")
    volume: int = Field(description="成交量")
    turnover: float = Field(description="成交额")
    open_interest: int = Field(description="持仓量")
    settle_price: Optional[float] = Field(default=None, description="结算价")
    pre_settle_price: Optional[float] = Field(default=None, description="昨结算价")
    pre_close_price: Optional[float] = Field(default=None, description="昨收盘价")
    limit_up: Optional[float] = Field(default=None, description="涨停价")
    limit_down: Optional[float] = Field(default=None, description="跌停价")


class FuturesDailyQuoteOptimized(BaseModel):
    """期货日频行情数据模型（优化版 - 支持双价格机制和连续合约）"""
    # 基础信息
    ticker: str = Field(description="合约代码，如 IF2501 或 IFL0连续")
    trade_date: str = Field(description="交易日期")
    sec_short_name: Optional[str] = Field(default=None, description="合约简称")
    exchange_cd: Optional[str] = Field(default=None, description="交易所代码")

    # 价格数据（双价格机制）
    pre_settle_price: float = Field(description="昨结算价（涨跌计算基准）")
    pre_close_price: Optional[float] = Field(default=None, description="昨收盘价")
    open_price: float = Field(description="开盘价")
    highest_price: float = Field(description="最高价")
    lowest_price: float = Field(description="最低价")
    close_price: float = Field(description="今收盘价")
    settle_price: Optional[float] = Field(default=None, description="今结算价（盯市结算用）")
    limit_up: Optional[float] = Field(default=None, description="当日涨停价")
    limit_down: Optional[float] = Field(default=None, description="当日跌停价")

    # 成交数据
    turnover_vol: int = Field(description="成交量，单位：手，单边计算")
    turnover_value: float = Field(description="成交金额，单边计算")
    open_int: int = Field(description="持仓量，单位：手，单边计算")

    # 涨跌数据
    chg: float = Field(description="涨跌 = 收盘价-昨结算")
    chg1: Optional[float] = Field(default=None, description="涨跌1 = 今结算价-昨结算")
    chg_pct: float = Field(description="涨跌幅 = (收盘价-昨结算)/昨结算")

    # 合约标记
    main_con: int = Field(description="是否主力（持仓量），1-是；0-否")
    smain_con: Optional[int] = Field(default=None, description="是否次主力（持仓量），1-是；0-否")
    contract_mark: Optional[str] = Field(default=None, description="连续合约标志：L0/L1/L2/L3/L4/L6/L9")
    contract_object: Optional[str] = Field(default=None, description="期货合约标的代码，如 IF/IC/RB")

    @property
    def is_main_contract(self) -> bool:
        """判断是否为主力合约"""
        return self.main_con == 1

    @property
    def is_continuous_contract(self) -> bool:
        """判断是否为连续合约"""
        return self.contract_mark is not None

    @property
    def daily_pnl_per_lot(self) -> float:
        """计算每手当日盈亏（用于盯市）

        公式：当日盈亏/手 = (今结算价 - 昨结算价) × 合约乘数
        """
        if self.settle_price and self.pre_settle_price:
            return self.settle_price - self.pre_settle_price
        # 如果没有结算价，使用收盘价近似
        return self.close_price - self.pre_settle_price


class FuturesContractInfo(BaseModel):
    """期货合约基础信息（扩展版）"""
    ticker: str = Field(description="合约代码，如 IF2501 或 IFL0")
    underlying_code: str = Field(description="标的代码，如 IF、IC、RB")
    contract_name: str = Field(description="合约名称")

    # 合约规格
    contract_multiplier: float = Field(description="合约乘数，如300（股指）或10（商品）")
    minimum_tick: float = Field(description="最小变动价位")

    # 保证金率
    margin_rate_long: float = Field(description="多头保证金率（如0.15表示15%）")
    margin_rate_short: float = Field(description="空头保证金率")

    # 交易所信息
    exchange: str = Field(description="交易所代码，如 XCFE、XSHG、XDCE")
    exchange_name: str = Field(description="交易所名称")

    # 合约类型
    contract_type: str = Field(description="合约类型：stock_index/financial/metal/energy/chemical/agricultural")

    # 交割信息
    delivery_date: Optional[str] = Field(default=None, description="交割日期")
    last_trading_date: Optional[str] = Field(default=None, description="最后交易日")

    # 连续合约标记
    is_continuous: bool = Field(default=False, description="是否为连续合约")
    contract_mark: Optional[str] = Field(default=None, description="连续标志：L0/L1/L2等")
    is_main: bool = Field(default=False, description="是否为主力合约")


class FuturesMainContract(BaseModel):
    """期货主力合约信息"""
    underlying_code: str = Field(description="标的代码")
    main_contract: str = Field(description="主力合约代码")
    trade_date: str = Field(description="交易日期")


class FuturesMargin(BaseModel):
    """期货保证金数据"""
    contract_id: str = Field(description="合约代码")
    long_margin_rate: float = Field(description="多头保证金率")
    short_margin_rate: float = Field(description="空头保证金率")
    update_date: str = Field(description="更新日期")


class FuturesSettlementRecord(BaseModel):
    """期货盯市结算记录"""
    trading_date: str = Field(description="结算日期")

    # 账户余额（按交易所公式）
    previous_balance: float = Field(description="上一交易日结算准备金余额")
    current_balance: float = Field(description="当日结算准备金余额")
    previous_account_equity: float = Field(default=0, description="Previous cash balance plus reserved margin")
    current_account_equity: float = Field(default=0, description="Current cash balance plus reserved margin")
    cash_available: float = Field(default=0, description="Current cash available after settlement")
    reserved_margin: float = Field(default=0, description="Current reserved margin")

    # 保证金
    previous_margin: float = Field(default=0, description="上一交易日交易保证金")
    current_margin: float = Field(default=0, description="当日交易保证金")
    margin_as_asset_prev: float = Field(default=0, description="上一交易日作为保证金的资产")
    margin_as_asset_curr: float = Field(default=0, description="当日作为保证金的资产")

    # 盈亏与资金流
    daily_pnl: float = Field(default=0, description="当日盈亏")
    deposit: float = Field(default=0, description="入金")
    withdraw: float = Field(default=0, description="出金")
    commission: float = Field(default=0, description="手续费")

    # 风险指标
    margin_ratio: float = Field(default=0, description="保证金占用比例")
    is_warning: bool = Field(default=False, description="是否触发预警")
    is_liquidation: bool = Field(default=False, description="是否触发强平")

    # 持仓明细
    positions_detail: Dict[str, Dict] = Field(default_factory=dict, description="各品种持仓明细")
