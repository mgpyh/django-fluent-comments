"""
Signals relating to comments.
"""
from django.dispatch import Signal

comment_will_be_removed = Signal(providing_args=["comment", "request"])
