"""
Post-Mortem Trade Analysis (Phase 11 Preparatory)

Analyzes recent buy trades, fetching historical data up to the exact moment
the trade was placed, and computing technical indicators (ADX, RSI, SMA, Volume)
as they appeared to the engine at that time. 

This helps identify "why" a trade was taken and validates proposed Phase 11
filters like the RSI Exhaustion gate and ADX trend strength gate.
"""

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd

# Add project root to sys.path so we can import project modules
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config import settings
from data.fetcher import fetch_symbol
from indicators.technicals import add_sma, add_rsi, add_adx
from reporting.logger import TradeLogger

# Map symbols to their sector ETF for Relative Strength comparison.
# If a symbol isn't here, it will just default to comparing against SPY.
SECTOR_MAP = {
    # Tech / Semiconductors
    "NVDA": "XLK", "MU": "XLK", "DELL": "XLK", "WDC": "XLK", "TER": "XLK",
    "AMKR": "XLK", "MPWR": "XLK", "COHR": "XLK", "FORM": "XLK", "CIEN": "XLK",
    "CLS": "XLK", "BE": "XLK", "MTZ": "XLK", "TIGO": "XLK", "SN": "XLK", "CDNS": "XLK",
    # Financials
    "ALLY": "XLF", "TFC": "XLF",
    # Materials
    "CCK": "XLB"
}

def analyze_trades(days: int):
    logger = TradeLogger(settings.TRADE_LOG_DB)
    trades = logger.read_all()
    
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    buy_trades = [t for t in trades if t["side"] == "buy" and datetime.fromisoformat(t["timestamp"]) >= cutoff]
    
    if not buy_trades:
        print(f"No buy trades found in the last {days} days.")
        return

    print(f"Found {len(buy_trades)} buy trades in the last {days} days.")
    print("⚠️  = Triggered proposed Phase 11 gates (ADX < 20 or RSI >= 70)")
    print("-" * 155)
    print(f"{'Trade Date':<12} | {'Bar Date':<12} | {'Sym':<5} | {'Strategy':<15} | {'Price':<8} | {'ADX_14':<10} | {'RSI_14':<10} | {'Vol_M10':<9} | {'Vol_M30':<9} | {'RS_SPY(20d)':<11} | {'RS_Sct(20d)':<11} | {'SMA_State'}")
    print("-" * 155)

    for trade in buy_trades:
        sym = trade["symbol"]
        ts_str = trade["timestamp"]
        ts = datetime.fromisoformat(ts_str)
        strategy = trade["strategy"]
        
        # Engine needs 200 days for SMA200, so fetch 300 calendar days prior
        start_fetch = ts - timedelta(days=300)
        end_fetch = ts
        
        try:
            df, _ = fetch_symbol(sym, start_fetch, end_fetch, "1Day", use_cache=True)
            if df.empty:
                continue
                
            # Keep only bars that closed *before* the trade execution timestamp.
            # This perfectly recreates the "last complete bar" the engine evaluated.
            df = df[df.index < ts].copy()
            if df.empty:
                continue
                
            # Compute Indicators
            df = add_sma(df, 20)
            df = add_sma(df, 50)
            df = add_sma(df, 200)
            df = add_rsi(df, 14)
            df = add_adx(df, 14)
            df["vol_median_10"] = df["volume"].rolling(10).median()
            df["vol_median_30"] = df["volume"].rolling(30).median()
            
            last_bar = df.iloc[-1]
            trade_date = ts.strftime("%Y-%m-%d")
            bar_date = last_bar.name.strftime("%Y-%m-%d")
            
            # Calculate SPY 20-day Relative Strength
            rs_spy_20 = float("nan")
            try:
                spy_df, _ = fetch_symbol("SPY", start_fetch, end_fetch, "1Day", use_cache=True)
                spy_df = spy_df[spy_df.index < ts].copy()
                if len(spy_df) >= 20 and len(df) >= 20:
                    spy_ret = (spy_df["close"].iloc[-1] - spy_df["close"].iloc[-20]) / spy_df["close"].iloc[-20]
                    sym_ret = (df["close"].iloc[-1] - df["close"].iloc[-20]) / df["close"].iloc[-20]
                    rs_spy_20 = (sym_ret - spy_ret) * 100  # As percentage points
            except Exception:
                pass
                
            # Calculate Sector ETF 20-day Relative Strength
            rs_sector_20 = float("nan")
            sector_etf = SECTOR_MAP.get(sym)
            if sector_etf:
                try:
                    sec_df, _ = fetch_symbol(sector_etf, start_fetch, end_fetch, "1Day", use_cache=True)
                    sec_df = sec_df[sec_df.index < ts].copy()
                    if len(sec_df) >= 20 and len(df) >= 20:
                        sec_ret = (sec_df["close"].iloc[-1] - sec_df["close"].iloc[-20]) / sec_df["close"].iloc[-20]
                        sym_ret = (df["close"].iloc[-1] - df["close"].iloc[-20]) / df["close"].iloc[-20]
                        rs_sector_20 = (sym_ret - sec_ret) * 100
                except Exception:
                    pass
            
            adx = last_bar.get("adx_14", float("nan"))
            rsi = last_bar.get("rsi_14", float("nan"))
            vol10 = last_bar.get("vol_median_10", float("nan"))
            vol30 = last_bar.get("vol_median_30", float("nan"))
            sma20 = last_bar.get("sma_20", float("nan"))
            sma50 = last_bar.get("sma_50", float("nan"))
            sma200 = last_bar.get("sma_200", float("nan"))
            close = last_bar["close"]
            
            # Formatting with warnings for proposed gates
            adx_warning = " ⚠️" if not pd.isna(adx) and adx < 20 else ""
            rsi_warning = " ⚠️" if not pd.isna(rsi) and rsi >= 70 else ""
            
            adx_str = f"{adx:.1f}{adx_warning}" if not pd.isna(adx) else "N/A"
            rsi_str = f"{rsi:.1f}{rsi_warning}" if not pd.isna(rsi) else "N/A"
            vol10_str = f"{vol10/1000:.0f}k" if not pd.isna(vol10) else "N/A"
            vol30_str = f"{vol30/1000:.0f}k" if not pd.isna(vol30) else "N/A"
            price_str = f"${trade['avg_fill_price']:.2f}" if trade.get('avg_fill_price') else "N/A"
            
            # Highlight negative relative strength
            rs_spy_warning = " ⚠️" if not pd.isna(rs_spy_20) and rs_spy_20 < 0 else ""
            rs_sec_warning = " ⚠️" if not pd.isna(rs_sector_20) and rs_sector_20 < 0 else ""
            
            rs_spy_str = f"{rs_spy_20:+.1f}%{rs_spy_warning}" if not pd.isna(rs_spy_20) else "N/A"
            rs_sec_str = f"{rs_sector_20:+.1f}%{rs_sec_warning}" if not pd.isna(rs_sector_20) else ("N/A" if not sector_etf else "Err")
            
            sma_state = ""
            if not pd.isna(sma20) and not pd.isna(sma50) and not pd.isna(sma200):
                if close > sma20 > sma50 > sma200:
                    sma_state = "C > 20 > 50 > 200"
                else:
                    sma_state = "Mixed"
                    
            print(f"{trade_date:<12} | {bar_date:<12} | {sym:<5} | {strategy:<15} | {price_str:<8} | {adx_str:<10} | {rsi_str:<10} | {vol10_str:<9} | {vol30_str:<9} | {rs_spy_str:<11} | {rs_sec_str:<11} | {sma_state}")
            
        except Exception as e:
            print(f"Error processing {sym} at {ts_str}: {e}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Post-Mortem Trade Analysis")
    parser.add_argument("--days", type=int, default=30, help="Number of days to look back")
    args = parser.parse_args()
    
    analyze_trades(args.days)
