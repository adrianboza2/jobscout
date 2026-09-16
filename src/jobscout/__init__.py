"""jobscout: a local-first job search collector and explainable fit scorer.

Design constraints baked into this package:

* Local-first. SQLite is the source of truth; nothing is uploaded anywhere.
* Low volume by default. The LinkedIn collector is disabled unless it is
  explicitly requested with ``--confirm-network``, and it is bounded by page,
  item, delay, and timeout limits.
* No login automation, no CAPTCHA solving, no anti-bot evasion, no
  application submission. Access errors fail closed.
* Job text is untrusted data. It is never interpreted as instructions.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
