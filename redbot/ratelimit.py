#!/usr/bin/env python

"""
Rate Limiting for RED, the Resource Expert Droid.
"""

from collections import defaultdict

import thor.loop


class RateLimiter:
    limits = {}
    counts = {}
    periods = {}
    watching = set()

    def __init__(self, loop: thor.loop.LoopBase) -> None:
        self.loop = loop

    def setup(self, metric_name: str, limit: int, period: int) -> None:
        if not metric_name in self.watching:
            self.limits[metric_name] = limit
            self.counts[metric_name] = defaultdict(int)
            self.periods[metric_name] = period
            self.loop.schedule(period, self.clear, metric_name)
            self.watching.add(metric_name)

    def increment(self, metric_name, discriminator) -> None:
        if not metric_name in self.watching:
            return
        self.counts[metric_name][discriminator] += 1
        if self.counts[metric_name][discriminator] > self.limits[metric_name]:
            raise RateLimitError

    def clear(self, metric_name):
        self.counts[metric_name] = defaultdict(int)
        self.loop.schedule(self.periods[metric_name], self.clear, metric_name)


ratelimiter = RateLimiter()


class RateLimitViolation(Exception):
    pass