"""Vintage Timex Scout — surface a ranked, explained shortlist of vintage Timex
watches worth a collector's attention.

The pipeline spine (README §7): source -> normalize -> gate -> pre-rank ->
LLM judge -> order -> present -> act, with an in-session learning loop.
"""

__version__ = "0.1.0"
