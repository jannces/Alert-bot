"""Tamad Strategy scanner — backtesting package.

Replays the exact live detection code (:mod:`strategy.tamad_strategy`) over
historical MEXC candles and measures, per candidate confirmation filter,
how often TP2/TP3 is reached before the stop. Used to decide with data —
not intuition — which confirmations improve the strategy.
"""
