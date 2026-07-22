"""Prometheus metrics -- lands in P1, not P4 (`plan.md`: "the fleet is
unobservable without it"). One process-global `prometheus_client` registry;
`metrics.py` holds every metric object so registry/reaper/driver/gateway
import from one place instead of each defining ad hoc counters."""

from __future__ import annotations
