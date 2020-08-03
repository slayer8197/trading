from pylivetrader.api import order_target, symbol

import logbook

log = logbook.Logger('algo')

def initialize(context):
    context.i = 0
    context.asset = symbol('SPY')

def handle_data(context, data):
    # Compute averages
    # data.history() has to be called with the same params
    # from above and returns a pandas dataframe.
    short_mavg = data.history(context.asset, 'price', bar_count=20, frequency="4h").mean()
    long_mavg = data.history(context.asset, 'price', bar_count=40, frequency="4h").mean()

    log.info(
            '''
            Short: %s
            Long:  %s
            ''' % (short_mavg, long_mavg))
    # Trading logic
    if short_mavg > long_mavg:
        # order_target orders as many shares as needed to
        # achieve the desired number of shares.
        order_target(context.asset, 100)
    elif short_mavg < long_mavg:
        order_target(context.asset, 0)
