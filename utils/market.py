import pandas as pd
import numpy as np

def apply_synthetic_sip_volume(df: pd.DataFrame, is_daily: bool = False) -> pd.DataFrame:
    """
    Scales IEX volume to approximate total consolidated market volume (SIP).
    Applies time-of-day multipliers to account for the "Volume Smile" where
    IEX market share is lowest at the open and close.
    """
    if df.empty or "volume" not in df.columns:
        return df

    df = df.copy()
    
    if is_daily:
        # Static 20x multiplier for daily bars (representing ~5% IEX daily share)
        df['volume'] = df['volume'] * 20.0
        return df

    # 1. Ensure index is timezone aware and convert to US/Eastern
    if df.index.tz is None:
        eastern_times = df.index.tz_localize('UTC').tz_convert('America/New_York').time
    else:
        eastern_times = df.index.tz_convert('America/New_York').time
    
    # 2. Define the time boundaries
    t_0930 = pd.to_datetime('09:30:00').time()
    t_1000 = pd.to_datetime('10:00:00').time()
    t_1100 = pd.to_datetime('11:00:00').time()
    t_1400 = pd.to_datetime('14:00:00').time()
    t_1545 = pd.to_datetime('15:45:00').time()
    t_1600 = pd.to_datetime('16:00:00').time()
    
    # 3. Vectorized condition mapping
    conditions = [
        (eastern_times >= t_0930) & (eastern_times < t_1000),  # Open
        (eastern_times >= t_1100) & (eastern_times < t_1400),  # Mid-day
        (eastern_times >= t_1545) & (eastern_times <= t_1600), # Close
    ]
    
    multipliers = [
        55.0,  # Open
        25.0,  # Mid-day
        65.0   # Close
    ]
    
    # Default to 35x for times not matching above buckets
    dynamic_multiplier = np.select(conditions, multipliers, default=35.0)
    
    df['volume'] = df['volume'] * dynamic_multiplier
    return df
