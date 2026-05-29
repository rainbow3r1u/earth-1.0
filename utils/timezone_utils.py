"""
时区处理工具类 - 统一项目中的时区处理逻辑

规范：
1. 存储时区：UTC（所有时间戳存储使用UTC）
2. 显示时区：北京时间（用户界面显示使用北京时间）
3. 转换规则：
   - 用户输入 → 北京时间 → UTC → 存储/处理
   - 存储/处理 → UTC → 北京时间 → 用户显示
"""
from datetime import datetime, timedelta, timezone
from typing import Union, Optional
import pandas as pd


class TimezoneUtils:
    """时区处理工具类"""

    # 北京时间偏移量（UTC+8）
    BEIJING_OFFSET = timedelta(hours=8)

    @staticmethod
    def utc_to_beijing(dt_utc: Union[datetime, pd.Timestamp]) -> datetime:
        """
        UTC时间转北京时间（用于显示）

        Args:
            dt_utc: UTC时间（datetime或pd.Timestamp）

        Returns:
            北京时间（datetime对象）
        """
        if isinstance(dt_utc, pd.Timestamp):
            dt_utc = dt_utc.to_pydatetime()
        return dt_utc + TimezoneUtils.BEIJING_OFFSET

    @staticmethod
    def beijing_to_utc(dt_beijing: Union[datetime, pd.Timestamp]) -> datetime:
        """
        北京时间转UTC时间（用于存储）

        Args:
            dt_beijing: 北京时间（datetime或pd.Timestamp）

        Returns:
            UTC时间（datetime对象）
        """
        if isinstance(dt_beijing, pd.Timestamp):
            dt_beijing = dt_beijing.to_pydatetime()
        return dt_beijing - TimezoneUtils.BEIJING_OFFSET

    @staticmethod
    def get_beijing_now() -> datetime:
        """
        获取当前北京时间

        Returns:
            当前北京时间
        """
        return datetime.now(timezone.utc) + TimezoneUtils.BEIJING_OFFSET

    @staticmethod
    def get_utc_now() -> datetime:
        """
        获取当前UTC时间

        Returns:
            当前UTC时间（带时区信息 timezone.utc，aware datetime）
        """
        return datetime.now(timezone.utc)

    @staticmethod
    def parse_beijing_time(time_str: str, format_str: str = '%Y-%m-%d %H:%M:%S') -> datetime:
        """
        解析北京时间字符串

        Args:
            time_str: 时间字符串（北京时间）
            format_str: 时间格式字符串

        Returns:
            北京时间（datetime对象）
        """
        dt_beijing = datetime.strptime(time_str, format_str)
        return dt_beijing

    @staticmethod
    def parse_beijing_time_to_utc(time_str: str, format_str: str = '%Y-%m-%d %H:%M:%S') -> datetime:
        """
        解析北京时间字符串并转换为UTC时间

        Args:
            time_str: 时间字符串（北京时间）
            format_str: 时间格式字符串

        Returns:
            UTC时间（datetime对象）
        """
        dt_beijing = TimezoneUtils.parse_beijing_time(time_str, format_str)
        return TimezoneUtils.beijing_to_utc(dt_beijing)

    @staticmethod
    def format_beijing_time(dt: Union[datetime, pd.Timestamp],
                           format_str: str = '%Y-%m-%d %H:%M:%S') -> str:
        """
        格式化时间为北京时间字符串

        Args:
            dt: 时间（UTC或北京时间）
            format_str: 时间格式字符串

        Returns:
            北京时间字符串
        """
        if isinstance(dt, pd.Timestamp):
            dt = dt.to_pydatetime()

        # 如果输入是UTC时间，先转换为北京时间
        if TimezoneUtils._is_likely_utc(dt):
            dt_beijing = TimezoneUtils.utc_to_beijing(dt)
        else:
            dt_beijing = dt

        return dt_beijing.strftime(format_str)

    @staticmethod
    def format_utc_time(dt: Union[datetime, pd.Timestamp],
                       format_str: str = '%Y-%m-%d %H:%M:%S') -> str:
        """
        格式化时间为UTC时间字符串

        Args:
            dt: 时间（UTC或北京时间）
            format_str: 时间格式字符串

        Returns:
            UTC时间字符串
        """
        if isinstance(dt, pd.Timestamp):
            dt = dt.to_pydatetime()

        # 如果输入是北京时间，先转换为UTC时间
        if not TimezoneUtils._is_likely_utc(dt):
            dt_utc = TimezoneUtils.beijing_to_utc(dt)
        else:
            dt_utc = dt

        return dt_utc.strftime(format_str)

    @staticmethod
    def _is_likely_utc(dt: datetime) -> bool:
        """
        判断时间是否可能是UTC时间（启发式判断）

        *** DEPRECATED: This heuristic is unreliable. ***
        It misclassifies UTC 16:00-23:00 by assuming hour<=15 means UTC.
        For example, 2024-01-01 18:00 UTC would be classified as Beijing time.
        TODO: Remove this heuristic and require explicit timezone info (e.g., tzinfo attribute).
              Until then, prefer checking dt.tzinfo directly for aware datetimes.

        规则：如果时间的小时部分在0-23范围内，且没有明显的北京时间特征
        """
        # 简单的启发式判断：如果小时部分较小（0-8），可能是UTC
        # 如果小时部分较大（16-23），可能是北京时间
        hour = dt.hour
        return hour <= 15  # 简单判断，实际使用时需要根据业务逻辑调整

    @staticmethod
    def validate_timezone_consistency() -> bool:
        """
        验证时区处理一致性

        Returns:
            是否一致
        """
        try:
            # 测试1：UTC转北京时间再转回UTC
            utc_now = datetime.utcnow()
            beijing = TimezoneUtils.utc_to_beijing(utc_now)
            utc_back = TimezoneUtils.beijing_to_utc(beijing)
            assert abs((utc_now - utc_back).total_seconds()) < 1, "UTC转换不一致"

            # 测试2：北京时间转UTC再转回北京时间
            beijing_now = TimezoneUtils.get_beijing_now()
            utc_from_beijing = TimezoneUtils.beijing_to_utc(beijing_now)
            beijing_back = TimezoneUtils.utc_to_beijing(utc_from_beijing)
            assert abs((beijing_now - beijing_back).total_seconds()) < 1, "北京时间转换不一致"

            # 测试3：字符串解析和格式化
            test_time_str = "2024-01-01 08:00:00"
            dt_beijing = TimezoneUtils.parse_beijing_time(test_time_str)
            dt_utc = TimezoneUtils.beijing_to_utc(dt_beijing)
            formatted = TimezoneUtils.format_beijing_time(dt_utc)
            assert formatted == test_time_str, "字符串转换不一致"

            print("时区处理一致性验证通过")
            return True

        except AssertionError as e:
            print(f"时区处理一致性验证失败: {e}")
            return False


# 便捷函数
def utc_to_beijing(dt_utc: Union[datetime, pd.Timestamp]) -> datetime:
    """UTC时间转北京时间（便捷函数）"""
    return TimezoneUtils.utc_to_beijing(dt_utc)


def beijing_to_utc(dt_beijing: Union[datetime, pd.Timestamp]) -> datetime:
    """北京时间转UTC时间（便捷函数）"""
    return TimezoneUtils.beijing_to_utc(dt_beijing)


def get_beijing_now() -> datetime:
    """获取当前北京时间（便捷函数）"""
    return TimezoneUtils.get_beijing_now()


def format_beijing_time(dt: Union[datetime, pd.Timestamp],
                       format_str: str = '%Y-%m-%d %H:%M:%S') -> str:
    """格式化时间为北京时间字符串（便捷函数）"""
    return TimezoneUtils.format_beijing_time(dt, format_str)


if __name__ == '__main__':
    # 运行一致性测试
    TimezoneUtils.validate_timezone_consistency()

    # 示例用法
    print("\n示例用法:")

    # 当前时间
    utc_now = datetime.utcnow()
    beijing_now = get_beijing_now()

    print(f"当前UTC时间: {utc_now}")
    print(f"当前北京时间: {beijing_now}")
    print(f"UTC转北京时间: {utc_to_beijing(utc_now)}")
    print(f"北京时间转UTC: {beijing_to_utc(beijing_now)}")

    # 字符串解析
    time_str = "2024-01-01 08:00:00"
    dt_beijing = TimezoneUtils.parse_beijing_time(time_str)
    dt_utc = TimezoneUtils.beijing_to_utc(dt_beijing)

    print(f"\n字符串解析示例:")
    print(f"输入字符串: {time_str}")
    print(f"解析为北京时间: {dt_beijing}")
    print(f"转换为UTC: {dt_utc}")
    print(f"格式化回北京时间: {format_beijing_time(dt_utc)}")