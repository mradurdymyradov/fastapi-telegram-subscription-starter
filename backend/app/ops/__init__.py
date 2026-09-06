"""Operator commands — run by a human on the host, never by a scheduler.

Everything in this package is a `python -m app.ops.<name>` entry point for a
one-off, consequential operation that deliberately has no button in the admin
panel. They read the same models and services the running system does, so a
decision made here is the same decision the hourly jobs would make.
"""
