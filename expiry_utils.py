import datetime


def get_calc_and_trade_expiry(expiries: list, today: datetime.date = None):
    """expiries: sorted ascending list of upcoming expiry dates (front-week
    first), from IIFLClient.get_nifty_futures_expiries(). Returns
    (calc_expiry, trade_expiry, is_expiry_day):
    - calc_expiry: always the current front-week expiry - signal
      calculation always uses this contract regardless of the day.
    - trade_expiry: same as calc_expiry on a normal day; rolls to the
      next-week expiry specifically ON the front-week's own expiry day,
      per locked design (calc on expiring contract, trade next-week).
    """
    today = today or datetime.date.today()
    if not expiries:
        raise ValueError("No expiry dates available - check contract master fetch.")

    front = expiries[0]
    is_expiry_day = (today == front)

    if is_expiry_day:
        if len(expiries) < 2:
            raise ValueError("Expiry day detected but no next-week expiry found in contract master.")
        trade_expiry = expiries[1]
    else:
        trade_expiry = front

    return front, trade_expiry, is_expiry_day
