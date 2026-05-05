import pytest
import pandas as pd
from strategies.spy_options_reversion import SPYOptionsReversionStrategy

def test_spy_options_time_stop():
    strategy = SPYOptionsReversionStrategy(rsi_length=14, rsi_threshold=30)
    
    # Create a DataFrame with various times
    # Including Wednesday 3:25 PM, Wednesday 3:30 PM, Wednesday 3:35 PM
    dates = [
        pd.Timestamp("2026-05-06 15:25:00", tz="US/Eastern"), # Wed
        pd.Timestamp("2026-05-06 15:30:00", tz="US/Eastern"), # Wed
        pd.Timestamp("2026-05-06 15:35:00", tz="US/Eastern"), # Wed
        pd.Timestamp("2026-05-07 10:00:00", tz="US/Eastern"), # Thu
    ]
    df = pd.DataFrame({"close": [100, 101, 102, 103]}, index=dates)
    
    # Needs to be > 19 bars for RSI length 14 + 5 buffer.
    # We pad the dataframe
    pad_dates = pd.date_range(end=dates[0] - pd.Timedelta(minutes=5), periods=20, freq="5min", tz="US/Eastern")
    df_pad = pd.DataFrame({"close": [100] * 20}, index=pad_dates)
    
    df = pd.concat([df_pad, df])
    
    signals = strategy._raw_signals(df)
    
    # Check the exits
    exits = signals.exits.iloc[-4:] # the 4 test dates
    assert not exits.iloc[0], "Should not exit at 3:25 PM"
    assert exits.iloc[1], "Should exit at 3:30 PM"
    assert exits.iloc[2], "Should exit at 3:35 PM"
    assert not exits.iloc[3], "Should not exit on Thursday"
