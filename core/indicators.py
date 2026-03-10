import pandas as pd
import numpy as np


class IndicatorCalculator:
    """Manual implementation of specific technical indicators."""

    @staticmethod
    def rsi(series, period=5):
        delta = series.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
        rs = gain / (loss.replace(0, 0.0001))
        return 100 - (100 / (1 + rs))

    @staticmethod
    def atr(df, period=1):
        tr1 = df["High"] - df["Low"]
        tr2 = (df["High"] - df["Close"].shift(1)).abs()
        tr3 = (df["Low"] - df["Close"].shift(1)).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        return tr.rolling(window=period).mean()

    @staticmethod
    def ema(series, period=9):
        return series.ewm(span=period, adjust=False).mean()

    @staticmethod
    def vwap(df):
        v = df["Volume"]
        p = (df["High"] + df["Low"] + df["Close"]) / 3
        return (p * v).cumsum() / v.cumsum()

    @staticmethod
    def stoch(df, k_period, smooth_k, smooth_d):
        low_min = df["Low"].rolling(window=k_period).min()
        high_max = df["High"].rolling(window=k_period).max()
        diff = high_max - low_min
        k_line = 100 * (df["Close"] - low_min) / (diff.replace(0, 0.0001))
        if smooth_k > 1:
            k_line = k_line.rolling(window=smooth_k).mean()
        d_line = k_line.rolling(window=smooth_d).mean()
        return k_line, d_line
