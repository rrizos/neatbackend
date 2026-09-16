"""Attribution at the moment an account appears.

A signal rather than an edit to accounts/views.py, for two reasons: signing up
happens by three different routes (the form, Apple, Google) and all three end
in a User being created, and because this repository's signup code is the last
place that should grow a dependency on a referral programme.
"""

import logging

from django.contrib.auth import get_user_model
from django.db.models.signals import post_save
from django.dispatch import receiver

logger = logging.getLogger(__name__)


@receiver(post_save, sender=get_user_model(), dispatch_uid='ambassadors_attribute')
def attribute_new_account(sender, instance, created, **kwargs):
    if not created:
        return
    try:
        from .matching import match_on_network

        # One call, both programmes: it picks whichever link was opened most
        # recently from this address. See match_on_network for why recency
        # beats "ambassadors first".
        match_on_network(instance)
    except Exception:
        # Deliberately swallowed. This runs inside the signup transaction, and
        # nobody should ever fail to join Neat because a referral count broke.
        logger.exception('ambassador attribution failed for user %s', instance.pk)
