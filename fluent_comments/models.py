from django.conf import settings
from django.db import models
from django.utils.translation import ugettext_lazy as _
from django_comments import Comment
from django_comments.models import BaseCommentAbstractModel
from django_comments.managers import CommentManager
from django.contrib.contenttypes.generic import GenericRelation
from django.contrib.sites.models import get_current_site
from django.core.mail import send_mail
from django.dispatch import receiver
from django_comments import signals
from django.template import RequestContext
from django.template.loader import render_to_string
from fluent_comments import appsettings

COMMENT_MAX_LENGTH = getattr(settings, 'COMMENT_MAX_LENGTH', 3000)

class FluentCommentManager(CommentManager):
    """
    Manager to optimize SQL queries for comments.
    """
    def get_queryset(self):
        return super(CommentManager, self).get_queryset().select_related('user')


class FluentComment(BaseCommentAbstractModel):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, verbose_name=_('user'), related_name="%(class)s_comments")
    created_time = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_time = models.DateTimeField(auto_now=True)
    comment = models.TextField(_('comment'), max_length=COMMENT_MAX_LENGTH)
    is_anonymous = models.BooleanField(default=False)
    parent = models.ForeignKey("self", null=True, blank=True)
    floor = models.IntegerField(default=-1)
    ip_address = models.GenericIPAddressField(_('IP address'), unpack_ipv4=True, blank=True, null=True)
    is_public = models.BooleanField(_('is public'), default=True,
                    help_text=_('Uncheck this box to make the comment effectively ' \
                                'disappear from the site.'))
    is_removed = models.BooleanField(_('is removed'), default=False,
                    help_text=_('Check this box if the comment is inappropriate. ' \
                                'A "This comment has been removed" message will ' \
                                'be displayed instead.'))

    objects = FluentCommentManager()

    def name(self):
        return self.is_anonymous and _("Anonymous User") or self.user.username

    class Meta:
        ordering = ('-created_time',)
        permissions = [("can_moderate", "Can moderate comments")]


@receiver(signals.comment_was_posted)
def on_comment_posted(sender, comment, request, **kwargs):
    """
    Send email notification of a new comment to site staff when email notifications have been requested.
    """
    # This code is copied from django_comments.moderation.
    # That code doesn't offer a RequestContext, which makes it really
    # hard to generate proper URL's with FQDN in the email
    #
    # Instead of implementing this feature in the moderator class, the signal is used instead
    # so the notification feature works regardless of a manual moderator.register() call in the project.
    if not appsettings.FLUENT_COMMENTS_USE_EMAIL_NOTIFICATION:
        return

    recipient_list = [manager_tuple[1] for manager_tuple in settings.MANAGERS]
    site = get_current_site(request)
    content_object = comment.content_object

    subject = u'[{0}] New comment posted on "{1}"'.format(site.name, content_object)
    context = {
        'site': site,
        'comment': comment,
        'content_object': content_object
    }

    message = render_to_string("comments/comment_notification_email.txt", context, RequestContext(request))
    send_mail(subject, message, settings.DEFAULT_FROM_EMAIL, recipient_list, fail_silently=True)


def get_comments_for_model(content_object, include_moderated=False):
    """
    Return the QuerySet with all comments for a given model.
    """
    qs = comments.get_model().objects.for_model(content_object)

    if not include_moderated:
        qs = qs.filter(is_public=True, is_removed=False)

    return qs


class CommentsRelation(GenericRelation):
    """
    A :class:`~django.contrib.contenttypes.generic.GenericRelation` which can be applied to a parent model that
    is expected to have comments. For example:

    .. code-block:: python

        class Article(models.Model):
            comments_set = CommentsRelation()
    """
    def __init__(self, *args, **kwargs):
        super(CommentsRelation, self).__init__(
            to=comments.get_model(),
            content_type_field='content_type',
            object_id_field='object_pk',
            **kwargs
        )


try:
    from south.modelsinspector import add_ignored_fields
except ImportError:
    pass
else:
    # South 0.7.x ignores GenericRelation fields but doesn't ignore subclasses.
    # Taking the same fix as applied in http://south.aeracode.org/ticket/414
    _name_re = "^" + __name__.replace(".", "\.")
    add_ignored_fields((
        _name_re + "\.CommentsRelation",
    ))
