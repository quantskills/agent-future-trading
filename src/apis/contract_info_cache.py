"""
期货合约信息缓存模块
存储各品种的合约乘数、保证金率等静态信息
"""
from typing import Dict, Optional


class FuturesContractInfoCache:
    """期货合约信息缓存"""

    # 期货品种基础信息（硬编码，避免频繁查询）
    CONTRACT_INFO = {
        # 股指期货
        "IF": {
            "contract_multiplier": 300,
            "margin_rate_long": 0.15,
            "margin_rate_short": 0.15,
            "exchange": "XCFE",
            "exchange_name": "中国金融期货交易所",
            "contract_type": "stock_index",
            "minimum_tick": 0.2,
            "contract_name": "沪深300股指期货"
        },
        "IC": {
            "contract_multiplier": 200,
            "margin_rate_long": 0.15,
            "margin_rate_short": 0.15,
            "exchange": "XCFE",
            "exchange_name": "中国金融期货交易所",
            "contract_type": "stock_index",
            "minimum_tick": 0.2,
            "contract_name": "中证500股指期货"
        },
        "IH": {
            "contract_multiplier": 300,
            "margin_rate_long": 0.15,
            "margin_rate_short": 0.15,
            "exchange": "XCFE",
            "exchange_name": "中国金融期货交易所",
            "contract_type": "stock_index",
            "minimum_tick": 0.2,
            "contract_name": "上证50股指期货"
        },
        "IM": {
            "contract_multiplier": 200,
            "margin_rate_long": 0.15,
            "margin_rate_short": 0.15,
            "exchange": "XCFE",
            "exchange_name": "中国金融期货交易所",
            "contract_type": "stock_index",
            "minimum_tick": 0.2,
            "contract_name": "中证1000股指期货"
        },
        # 国债期货
        "T": {
            "contract_multiplier": 10000,
            "margin_rate_long": 0.02,
            "margin_rate_short": 0.02,
            "exchange": "XCFE",
            "exchange_name": "中国金融期货交易所",
            "contract_type": "financial",
            "minimum_tick": 0.005,
            "contract_name": "十年期国债期货"
        },
        "TF": {
            "contract_multiplier": 10000,
            "margin_rate_long": 0.02,
            "margin_rate_short": 0.02,
            "exchange": "XCFE",
            "exchange_name": "中国金融期货交易所",
            "contract_type": "financial",
            "minimum_tick": 0.005,
            "contract_name": "五年期国债期货"
        },
        "TS": {
            "contract_multiplier": 10000,
            "margin_rate_long": 0.015,
            "margin_rate_short": 0.015,
            "exchange": "XCFE",
            "exchange_name": "中国金融期货交易所",
            "contract_type": "financial",
            "minimum_tick": 0.005,
            "contract_name": "两年期国债期货"
        },
        # 黑色系
        "RB": {  # 螺纹钢
            "contract_multiplier": 10,
            "margin_rate_long": 0.10,
            "margin_rate_short": 0.12,
            "exchange": "XSHG",  # 上海期货交易所
            "exchange_name": "上海期货交易所",
            "contract_type": "metal",
            "minimum_tick": 1,
            "contract_name": "螺纹钢"
        },
        "I": {  # 铁矿石
            "contract_multiplier": 100,
            "margin_rate_long": 0.12,
            "margin_rate_short": 0.14,
            "exchange": "XDCE",  # 大连商品交易所
            "exchange_name": "大连商品交易所",
            "contract_type": "metal",
            "minimum_tick": 0.5,
            "contract_name": "铁矿石"
        },
        "HC": {  # 热轧卷板
            "contract_multiplier": 10,
            "margin_rate_long": 0.10,
            "margin_rate_short": 0.12,
            "exchange": "XSHG",
            "exchange_name": "上海期货交易所",
            "contract_type": "metal",
            "minimum_tick": 1,
            "contract_name": "热轧卷板"
        },
        "J": {  # 焦炭
            "contract_multiplier": 100,
            "margin_rate_long": 0.12,
            "margin_rate_short": 0.14,
            "exchange": "XDCE",
            "exchange_name": "大连商品交易所",
            "contract_type": "metal",
            "minimum_tick": 0.5,
            "contract_name": "焦炭"
        },
        "JM": {  # 焦煤
            "contract_multiplier": 60,
            "margin_rate_long": 0.12,
            "margin_rate_short": 0.14,
            "exchange": "XDCE",
            "exchange_name": "大连商品交易所",
            "contract_type": "metal",
            "minimum_tick": 0.5,
            "contract_name": "焦煤"
        },
        # 有色金属
        "CU": {  # 铜
            "contract_multiplier": 5,
            "margin_rate_long": 0.08,
            "margin_rate_short": 0.10,
            "exchange": "XSHG",
            "exchange_name": "上海期货交易所",
            "contract_type": "metal",
            "minimum_tick": 10,
            "contract_name": "铜"
        },
        "AL": {  # 铝
            "contract_multiplier": 5,
            "margin_rate_long": 0.07,
            "margin_rate_short": 0.09,
            "exchange": "XSHG",
            "exchange_name": "上海期货交易所",
            "contract_type": "metal",
            "minimum_tick": 5,
            "contract_name": "铝"
        },
        "ZN": {  # 锌
            "contract_multiplier": 5,
            "margin_rate_long": 0.08,
            "margin_rate_short": 0.10,
            "exchange": "XSHG",
            "exchange_name": "上海期货交易所",
            "contract_type": "metal",
            "minimum_tick": 5,
            "contract_name": "锌"
        },
        "NI": {  # 镍
            "contract_multiplier": 1,
            "margin_rate_long": 0.10,
            "margin_rate_short": 0.12,
            "exchange": "XSHG",
            "exchange_name": "上海期货交易所",
            "contract_type": "metal",
            "minimum_tick": 10,
            "contract_name": "镍"
        },
        "SN": {  # 锡
            "contract_multiplier": 1,
            "margin_rate_long": 0.09,
            "margin_rate_short": 0.11,
            "exchange": "XSHG",
            "exchange_name": "上海期货交易所",
            "contract_type": "metal",
            "minimum_tick": 10,
            "contract_name": "锡"
        },
        "PB": {  # 铅
            "contract_multiplier": 5,
            "margin_rate_long": 0.08,
            "margin_rate_short": 0.10,
            "exchange": "XSHG",
            "exchange_name": "上海期货交易所",
            "contract_type": "metal",
            "minimum_tick": 5,
            "contract_name": "铅"
        },
        # 贵金属
        "AU": {  # 黄金
            "contract_multiplier": 1000,
            "margin_rate_long": 0.08,
            "margin_rate_short": 0.10,
            "exchange": "XSHG",
            "exchange_name": "上海期货交易所",
            "contract_type": "metal",
            "minimum_tick": 0.02,
            "contract_name": "黄金"
        },
        # 能化
        "SC": {  # 原油
            "contract_multiplier": 1000,
            "margin_rate_long": 0.10,
            "margin_rate_short": 0.12,
            "exchange": "XSHG",  # 上海国际能源交易中心
            "exchange_name": "上海国际能源交易中心",
            "contract_type": "energy",
            "minimum_tick": 0.1,
            "contract_name": "原油"
        },
        "RU": {  # 天然橡胶
            "contract_multiplier": 10,
            "margin_rate_long": 0.09,
            "margin_rate_short": 0.11,
            "exchange": "XSHG",
            "exchange_name": "上海期货交易所",
            "contract_type": "chemical",
            "minimum_tick": 5,
            "contract_name": "天然橡胶"
        },
        "BU": {  # 石油沥青
            "contract_multiplier": 10,
            "margin_rate_long": 0.10,
            "margin_rate_short": 0.12,
            "exchange": "XSHG",
            "exchange_name": "上海期货交易所",
            "contract_type": "chemical",
            "minimum_tick": 1,
            "contract_name": "石油沥青"
        },
        "FU": {  # 燃料油
            "contract_multiplier": 50,
            "margin_rate_long": 0.10,
            "margin_rate_short": 0.12,
            "exchange": "XSHG",
            "exchange_name": "上海期货交易所",
            "contract_type": "chemical",
            "minimum_tick": 1,
            "contract_name": "燃料油"
        },
        "EG": {  # 乙二醇
            "contract_multiplier": 10,
            "margin_rate_long": 0.08,
            "margin_rate_short": 0.10,
            "exchange": "XDCE",
            "exchange_name": "大连商品交易所",
            "contract_type": "chemical",
            "minimum_tick": 1,
            "contract_name": "乙二醇"
        },
        "EB": {  # 苯乙烯
            "contract_multiplier": 5,
            "margin_rate_long": 0.07,
            "margin_rate_short": 0.09,
            "exchange": "XDCE",
            "exchange_name": "大连商品交易所",
            "contract_type": "chemical",
            "minimum_tick": 0.5,
            "contract_name": "苯乙烯"
        },
        "L": {  # 线性低密度聚乙烯
            "contract_multiplier": 5,
            "margin_rate_long": 0.07,
            "margin_rate_short": 0.09,
            "exchange": "XDCE",
            "exchange_name": "大连商品交易所",
            "contract_type": "chemical",
            "minimum_tick": 5,
            "contract_name": "线性低密度聚乙烯"
        },
        "PP": {  # 聚丙烯
            "contract_multiplier": 5,
            "margin_rate_long": 0.07,
            "margin_rate_short": 0.09,
            "exchange": "XDCE",
            "exchange_name": "大连商品交易所",
            "contract_type": "chemical",
            "minimum_tick": 1,
            "contract_name": "聚丙烯"
        },
        "PVC": {  # 聚氯乙烯
            "contract_multiplier": 5,
            "margin_rate_long": 0.07,
            "margin_rate_short": 0.09,
            "exchange": "XDCE",
            "exchange_name": "大连商品交易所",
            "contract_type": "chemical",
            "minimum_tick": 5,
            "contract_name": "聚氯乙烯"
        },
        "TA": {  # PTA
            "contract_multiplier": 5,
            "margin_rate_long": 0.07,
            "margin_rate_short": 0.09,
            "exchange": "XZCE",
            "exchange_name": "郑州商品交易所",
            "contract_type": "chemical",
            "minimum_tick": 2,
            "contract_name": "精对苯二甲酸(PTA)"
        },
        "MA": {  # 甲醇
            "contract_multiplier": 50,
            "margin_rate_long": 0.08,
            "margin_rate_short": 0.10,
            "exchange": "XZCE",
            "exchange_name": "郑州商品交易所",
            "contract_type": "chemical",
            "minimum_tick": 1,
            "contract_name": "甲醇"
        },
        "FG": {  # 平板玻璃
            "contract_multiplier": 20,
            "margin_rate_long": 0.08,
            "margin_rate_short": 0.10,
            "exchange": "XZCE",
            "exchange_name": "郑州商品交易所",
            "contract_type": "chemical",
            "minimum_tick": 1,
            "contract_name": "平板玻璃"
        },
        "SA": {  # 纯碱
            "contract_multiplier": 20,
            "margin_rate_long": 0.08,
            "margin_rate_short": 0.10,
            "exchange": "XZCE",
            "exchange_name": "郑州商品交易所",
            "contract_type": "chemical",
            "minimum_tick": 1,
            "contract_name": "纯碱"
        },
        "UR": {  # 尿素
            "contract_multiplier": 20,
            "margin_rate_long": 0.08,
            "margin_rate_short": 0.10,
            "exchange": "XZCE",
            "exchange_name": "郑州商品交易所",
            "contract_type": "chemical",
            "minimum_tick": 1,
            "contract_name": "尿素"
        },
        # 农产品
        "M": {  # 豆粕
            "contract_multiplier": 10,
            "margin_rate_long": 0.07,
            "margin_rate_short": 0.09,
            "exchange": "XDCE",
            "exchange_name": "大连商品交易所",
            "contract_type": "agricultural",
            "minimum_tick": 1,
            "contract_name": "豆粕"
        },
        "Y": {  # 豆油
            "contract_multiplier": 10,
            "margin_rate_long": 0.07,
            "margin_rate_short": 0.09,
            "exchange": "XDCE",
            "exchange_name": "大连商品交易所",
            "contract_type": "agricultural",
            "minimum_tick": 2,
            "contract_name": "豆油"
        },
        "P": {  # 棕榈油
            "contract_multiplier": 10,
            "margin_rate_long": 0.08,
            "margin_rate_short": 0.10,
            "exchange": "XDCE",
            "exchange_name": "大连商品交易所",
            "contract_type": "agricultural",
            "minimum_tick": 2,
            "contract_name": "棕榈油"
        },
        "A": {  # 黄大豆1号
            "contract_multiplier": 10,
            "margin_rate_long": 0.07,
            "margin_rate_short": 0.09,
            "exchange": "XDCE",
            "exchange_name": "大连商品交易所",
            "contract_type": "agricultural",
            "minimum_tick": 1,
            "contract_name": "黄大豆1号"
        },
        "B": {  # 黄大豆2号
            "contract_multiplier": 10,
            "margin_rate_long": 0.07,
            "margin_rate_short": 0.09,
            "exchange": "XDCE",
            "exchange_name": "大连商品交易所",
            "contract_type": "agricultural",
            "minimum_tick": 1,
            "contract_name": "黄大豆2号"
        },
        "C": {  # 玉米
            "contract_multiplier": 10,
            "margin_rate_long": 0.06,
            "margin_rate_short": 0.08,
            "exchange": "XDCE",
            "exchange_name": "大连商品交易所",
            "contract_type": "agricultural",
            "minimum_tick": 1,
            "contract_name": "玉米"
        },
        "CS": {  # 玉米淀粉
            "contract_multiplier": 10,
            "margin_rate_long": 0.06,
            "margin_rate_short": 0.08,
            "exchange": "XDCE",
            "exchange_name": "大连商品交易所",
            "contract_type": "agricultural",
            "minimum_tick": 1,
            "contract_name": "玉米淀粉"
        },
        "JD": {  # 鲜鸡蛋
            "contract_multiplier": 10,
            "margin_rate_long": 0.08,
            "margin_rate_short": 0.10,
            "exchange": "XDCE",
            "exchange_name": "大连商品交易所",
            "contract_type": "agricultural",
            "minimum_tick": 1,
            "contract_name": "鲜鸡蛋"
        },
        "LH": {  # 生猪
            "contract_multiplier": 16,
            "margin_rate_long": 0.08,
            "margin_rate_short": 0.10,
            "exchange": "XDCE",
            "exchange_name": "大连商品交易所",
            "contract_type": "agricultural",
            "minimum_tick": 5,
            "contract_name": "生猪"
        },
        "CF": {  # 棉花
            "contract_multiplier": 5,
            "margin_rate_long": 0.07,
            "margin_rate_short": 0.09,
            "exchange": "XZCE",  # 郑州商品交易所
            "exchange_name": "郑州商品交易所",
            "contract_type": "agricultural",
            "minimum_tick": 5,
            "contract_name": "棉花"
        },
        "SR": {  # 白糖
            "contract_multiplier": 10,
            "margin_rate_long": 0.07,
            "margin_rate_short": 0.09,
            "exchange": "XZCE",
            "exchange_name": "郑州商品交易所",
            "contract_type": "agricultural",
            "minimum_tick": 1,
            "contract_name": "白糖"
        },
        "OI": {  # 菜籽油
            "contract_multiplier": 10,
            "margin_rate_long": 0.07,
            "margin_rate_short": 0.09,
            "exchange": "XZCE",
            "exchange_name": "郑州商品交易所",
            "contract_type": "agricultural",
            "minimum_tick": 1,
            "contract_name": "菜籽油"
        },
        "RM": {  # 菜籽粕
            "contract_multiplier": 10,
            "margin_rate_long": 0.07,
            "margin_rate_short": 0.09,
            "exchange": "XZCE",
            "exchange_name": "郑州商品交易所",
            "contract_type": "agricultural",
            "minimum_tick": 1,
            "contract_name": "菜籽粕"
        },
        "RS": {  # 油菜籽
            "contract_multiplier": 10,
            "margin_rate_long": 0.07,
            "margin_rate_short": 0.09,
            "exchange": "XZCE",
            "exchange_name": "郑州商品交易所",
            "contract_type": "agricultural",
            "minimum_tick": 1,
            "contract_name": "油菜籽"
        },
        "WH": {  # 优质强筋小麦
            "contract_multiplier": 20,
            "margin_rate_long": 0.06,
            "margin_rate_short": 0.08,
            "exchange": "XZCE",
            "exchange_name": "郑州商品交易所",
            "contract_type": "agricultural",
            "minimum_tick": 1,
            "contract_name": "优质强筋小麦"
        },
        "PM": {  # 普通小麦
            "contract_multiplier": 50,
            "margin_rate_long": 0.05,
            "margin_rate_short": 0.07,
            "exchange": "XZCE",
            "exchange_name": "郑州商品交易所",
            "contract_type": "agricultural",
            "minimum_tick": 1,
            "contract_name": "普通小麦"
        },
        "RI": {  # 早籼稻
            "contract_multiplier": 20,
            "margin_rate_long": 0.06,
            "margin_rate_short": 0.08,
            "exchange": "XZCE",
            "exchange_name": "郑州商品交易所",
            "contract_type": "agricultural",
            "minimum_tick": 1,
            "contract_name": "早籼稻"
        },
        "LR": {  # 晚籼稻
            "contract_multiplier": 20,
            "margin_rate_long": 0.06,
            "margin_rate_short": 0.08,
            "exchange": "XZCE",
            "exchange_name": "郑州商品交易所",
            "contract_type": "agricultural",
            "minimum_tick": 1,
            "contract_name": "晚籼稻"
        },
        "JR": {  # 粳稻
            "contract_multiplier": 20,
            "margin_rate_long": 0.06,
            "margin_rate_short": 0.08,
            "exchange": "XZCE",
            "exchange_name": "郑州商品交易所",
            "contract_type": "agricultural",
            "minimum_tick": 1,
            "contract_name": "粳稻"
        },
        "AP": {  # 苹果
            "contract_multiplier": 10,
            "margin_rate_long": 0.08,
            "margin_rate_short": 0.10,
            "exchange": "XZCE",
            "exchange_name": "郑州商品交易所",
            "contract_type": "agricultural",
            "minimum_tick": 1,
            "contract_name": "苹果"
        },
        "CJ": {  # 干制红枣
            "contract_multiplier": 5,
            "margin_rate_long": 0.08,
            "margin_rate_short": 0.10,
            "exchange": "XZCE",
            "exchange_name": "郑州商品交易所",
            "contract_type": "agricultural",
            "minimum_tick": 5,
            "contract_name": "干制红枣"
        },
        "PK": {  # 花生
            "contract_multiplier": 5,
            "margin_rate_long": 0.08,
            "margin_rate_short": 0.10,
            "exchange": "XZCE",
            "exchange_name": "郑州商品交易所",
            "contract_type": "agricultural",
            "minimum_tick": 2,
            "contract_name": "花生"
        },
        "SF": {  # 硅铁
            "contract_multiplier": 5,
            "margin_rate_long": 0.10,
            "margin_rate_short": 0.12,
            "exchange": "XZCE",
            "exchange_name": "郑州商品交易所",
            "contract_type": "metal",
            "minimum_tick": 2,
            "contract_name": "硅铁"
        },
        "SM": {  # 锰硅
            "contract_multiplier": 5,
            "margin_rate_long": 0.10,
            "margin_rate_short": 0.12,
            "exchange": "XZCE",
            "exchange_name": "郑州商品交易所",
            "contract_type": "metal",
            "minimum_tick": 2,
            "contract_name": "锰硅"
        },
    }

    @classmethod
    def get_contract_info(cls, underlying_code: str) -> Optional[Dict]:
        """
        获取合约基础信息

        Args:
            underlying_code: 标的代码（如 IF、RB、M）

        Returns:
            合约信息字典，如果不存在返回None
        """
        return cls.CONTRACT_INFO.get(underlying_code.upper())

    @classmethod
    def get_multiplier(cls, underlying_code: str) -> float:
        """获取合约乘数"""
        info = cls.get_contract_info(underlying_code)
        return info['contract_multiplier'] if info else 1

    @classmethod
    def get_margin_rate(
        cls,
        underlying_code: str,
        is_long: bool = True
    ) -> float:
        """
        获取保证金率

        Args:
            underlying_code: 标的代码
            is_long: 是否为多头（True=多头，False=空头）
        """
        info = cls.get_contract_info(underlying_code)
        if info:
            return info['margin_rate_long'] if is_long else info['margin_rate_short']
        return 0.15  # 默认15%

    @classmethod
    def get_all_supported_codes(cls) -> list:
        """获取所有支持的期货品种代码"""
        return list(cls.CONTRACT_INFO.keys())

    @classmethod
    def get_contract_type(cls, underlying_code: str) -> str:
        """获取合约类型"""
        info = cls.get_contract_info(underlying_code)
        return info['contract_type'] if info else "unknown"
