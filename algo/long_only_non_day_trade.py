from zipline.pipeline import Pipeline
from pylivetrader.api import *
from pipeline_live.data.iex.pricing import USEquityPricing
from pipeline_live.data.iex.fundamentals import IEXCompany, IEXKeyStats
from pipeline_live.data.iex.factors import SimpleMovingAverage, AverageDollarVolume
from pipeline_live.data.polygon.filters import IsPrimaryShareEmulation
from pylivetrader.finance.execution import LimitOrder

import logbook
log = logbook.Logger('algo')

import numpy as np  # needed for NaN handling
import math  # ceil and floor are useful for rounding
import dateutil

from itertools import cycle

def record(*args, **kwargs):
    log.info('args={}, kwargs={}'.format(args, kwargs))

def initialize(context):
    context.MaxCandidates = 100
    context.MaxBuyOrdersAtOnce = 50
    context.MyLeastPrice = 3.00
    context.MyMostPrice = 25.00
    context.MyFireSalePrice = context.MyLeastPrice
    context.MyFireSaleAge = 6

    context.MaxInvestment = 150000

    # over simplistic tracking of position age
    if not hasattr(context, 'age') or not context.age:
        context.age = {}

    # Rebalance
    EveryThisManyMinutes = 10
    TradingDayHours = 6.5
    TradingDayMinutes = int(TradingDayHours * 60)
    for minutez in range(
        1,
        TradingDayMinutes,
        EveryThisManyMinutes
    ):
        schedule_function(
            my_rebalance,
            date_rules.every_day(),
            time_rules.market_open(
                minutes=minutez))

    # Prevent excessive logging of canceled orders at market close.
    schedule_function(
        cancel_open_orders,
        date_rules.every_day(),
        time_rules.market_close(
            hours=0,
            minutes=1))

    # Record variables at the end of each day.
    schedule_function(
        my_record_vars,
        date_rules.every_day(),
        time_rules.market_close())

    # Create our pipeline and attach it to our algorithm.
    my_pipe = make_pipeline(context)
    attach_pipeline(my_pipe, 'my_pipeline')

def make_pipeline(context):
    """
    Create our pipeline.
    """

    # Filter for primary share equities. IsPrimaryShare is a built-in filter.
    primary_share = IsPrimaryShareEmulation()

    # Equities listed as common stock (as opposed to, say, preferred stock).
    # 'ST00000001' indicates common stock.
    common_stock = IEXCompany.issueType.latest.eq('cs')

    # Equities not trading over-the-counter.
    not_otc = ~IEXCompany.exchange.latest.startswith(
        'OTC')

    # Not when-issued equities.
    not_wi = ~IEXCompany.symbol.latest.endswith('.WI')

    # Equities without LP in their name, .matches does a match using a regular
    # expression
    not_lp_name = ~IEXCompany.companyName.latest.matches(
        '.* L[. ]?P.?$')

    # Equities whose most recent Morningstar market cap is not null have
    # fundamental data and therefore are not ETFs.
    have_market_cap = IEXKeyStats.marketcap.latest.notnull()

    # At least a certain price
    price = USEquityPricing.close.latest
    AtLeastPrice = (price >= context.MyLeastPrice)
    AtMostPrice = (price <= context.MyMostPrice)

    # Filter for stocks that pass all of our previous filters.
    tradeable_stocks = (
        primary_share
        & common_stock
        & not_otc
        & not_wi
        & not_lp_name
        & have_market_cap
        & AtLeastPrice
        & AtMostPrice
    )

    LowVar = 6
    HighVar = 40

    log.info(
        '''
Algorithm initialized variables:
 context.MaxCandidates %s
 LowVar %s
 HighVar %s''' %
        (context.MaxCandidates, LowVar, HighVar))

    # High dollar volume filter.
    base_universe = AverageDollarVolume(
        window_length=20,
        mask=tradeable_stocks
    ).percentile_between(LowVar, HighVar)

    # Short close price average.
    ShortAvg = SimpleMovingAverage(
        inputs=[USEquityPricing.close],
        window_length=3,
        mask=base_universe
    )

    # Long close price average.
    LongAvg = SimpleMovingAverage(
        inputs=[USEquityPricing.close],
        window_length=45,
        mask=base_universe
    )

    percent_difference = (ShortAvg - LongAvg) / LongAvg

    # Filter to select securities to long.
    stocks_worst = percent_difference.bottom(context.MaxCandidates)
    securities_to_trade = (stocks_worst)

    return Pipeline(
        columns={
            'stocks_worst': stocks_worst
        },
        screen=(securities_to_trade),
    )


def my_compute_weights(context):
    """
    Compute ordering weights.
    """
    # Compute even target weights for our long positions and short positions.
    stocks_worst_weight = 1.00 / len(context.stocks_worst)

    return stocks_worst_weight


def before_trading_start(context, data):
    log.info("RUNNING before_trading_start")
    # Prevent running more than once a day:
    # https://docs.alpaca.markets/platform-migration/zipline-to-pylivetrader/#deal-with-restart
    today = get_datetime().floor('1D')
    last_date = getattr(context, 'last_date', None)
    if today == last_date:
        log.info("Skipping before_trading_start because it's already ran today")
        return

    context.output = pipeline_output('my_pipeline')

    context.stocks_worst = context.output[
        context.output['stocks_worst']].index.tolist()

    context.stocks_worst_weight = my_compute_weights(context)

    context.MyCandidate = cycle(context.stocks_worst)

    context.LowestPrice = context.MyLeastPrice  # reset beginning of day
    for stock in context.portfolio.positions:
        CurrPrice = float(data.current([stock], 'price'))
        if CurrPrice < context.LowestPrice:
            context.LowestPrice = CurrPrice
        if stock in context.age:
            context.age[stock] += 1
        else:
            context.age[stock] = 1
    for stock in context.age:
        if stock not in context.portfolio.positions:
            context.age[stock] = 0

    # Track the last run
    context.last_date = today

def my_rebalance(context, data):
    BuyFactor = .99
    SellFactor = 1.01
    cash = min(investment_limits(context)['remaining_to_invest'], context.portfolio.cash)

    cancel_open_buy_orders(context, data)

    # Order sell at profit target in hope that somebody actually buys it
    for stock in context.portfolio.positions:
        if not get_open_orders(stock):
            StockShares = context.portfolio.positions[stock].amount
            CurrPrice = float(data.current([stock], 'price'))
            CostBasis = float(context.portfolio.positions[stock].cost_basis)
            SellPrice = float(
                make_div_by_05(
                    CostBasis *
                    SellFactor,
                    buy=False))

            if np.isnan(SellPrice):
                pass  # probably best to wait until nan goes away
            elif (stock in context.age and context.age[stock] == 1):
                pass
            elif (
                stock in context.age
                and context.MyFireSaleAge <= context.age[stock]
                and (
                    context.MyFireSalePrice > CurrPrice
                    or CostBasis > CurrPrice
                )
            ):
                if (stock in context.age and context.age[stock] < 2):
                    pass
                elif stock not in context.age:
                    context.age[stock] = 1
                else:
                    SellPrice = float(
                        make_div_by_05(.95 * CurrPrice, buy=False))
                    order(stock, -StockShares,
                          style=LimitOrder(SellPrice)
                          )
            else:
                if (stock in context.age and context.age[stock] < 2):
                    pass
                elif stock not in context.age:
                    context.age[stock] = 1
                else:
                    order(stock, -StockShares,
                          style=LimitOrder(SellPrice)
                          )

    WeightThisBuyOrder = float(1.00 / context.MaxBuyOrdersAtOnce)
    for ThisBuyOrder in range(context.MaxBuyOrdersAtOnce):
        stock = context.MyCandidate.__next__()
        # This cancels open sales that would prevent these buys from being submitted if running
        # up against the PDT rule
        if stock in get_open_orders():
            for open_order in get_open_orders(stock):
                cancel_order(order)
        PH = data.history([stock], 'price', 20, '1d')
        PH_Avg = float(PH.mean())
        CurrPrice = float(data.current([stock], 'price'))
        if np.isnan(CurrPrice):
            pass  # probably best to wait until nan goes away
        else:
            if CurrPrice > float(1.25 * PH_Avg):
                BuyPrice = float(CurrPrice)
            else:
                BuyPrice = float(CurrPrice * BuyFactor)
            BuyPrice = float(make_div_by_05(BuyPrice, buy=True))
            StockShares = int(WeightThisBuyOrder * cash / BuyPrice)
            order(stock, StockShares,
                  style=LimitOrder(BuyPrice)
                  )

# if cents not divisible by .05, round down if buy, round up if sell

def make_div_by_05(s, buy=False):
    s *= 20.00
    s = math.floor(s) if buy else math.ceil(s)
    s /= 20.00
    return s

def my_record_vars(context, data):
    """
    Record variables at the end of each day.
    """

    # Record our variables.
    record(leverage=context.account.leverage)

    if 0 < len(context.age):
        MaxAge = context.age[max(
            context.age.keys(), key=(lambda k: context.age[k]))]
        MinAge = context.age[min(
            context.age.keys(), key=(lambda k: context.age[k]))]
        record(MaxAge=MaxAge)
        record(MinAge=MinAge)

    limits = investment_limits(context)
    record(ExcessCash=limits['excess_cash'])
    record(Invested=limits['invested'])
    record(RemainingToInvest=limits['remaining_to_invest'])

def log_open_order(StockToLog):
    oo = get_open_orders()
    if len(oo) == 0:
        return
    for stock, orders in oo.items():
        if stock == StockToLog:
            for o in orders:
                message = 'Found open order for {amount} shares in {stock}'
                log.info(message.format(amount=o.amount, stock=stock))


def log_open_orders():
    oo = get_open_orders()
    if len(oo) == 0:
        return
    for stock, orders in oo.items():
        for o in orders:
            message = 'Found open order for {amount} shares in {stock}'
            log.info(message.format(amount=o.amount, stock=stock))


def cancel_open_buy_orders(context, data):
    oo = get_open_orders()
    if len(oo) == 0:
        return
    for stock, orders in oo.items():
        for o in orders:
            # message = 'Canceling order of {amount} shares in {stock}'
            # log.info(message.format(amount=o.amount, stock=stock))
            if 0 < o.amount:  # it is a buy order
                cancel_order(o)


def cancel_open_orders(context, data):
    oo = get_open_orders()
    if len(oo) == 0:
        return
    for stock, orders in oo.items():
        for o in orders:
            # message = 'Canceling order of {amount} shares in {stock}'
            # log.info(message.format(amount=o.amount, stock=stock))
            cancel_order(o)

def investment_limits(context):
    cash = context.portfolio.cash
    portfolio_value = context.portfolio.portfolio_value
    invested = portfolio_value - cash
    remaining_to_invest = max(0, context.MaxInvestment - invested)
    excess_cash = max(0, cash - remaining_to_invest)

    return {
        "invested": invested,
        "remaining_to_invest": remaining_to_invest,
        "excess_cash": excess_cash
    }
