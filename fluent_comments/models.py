from hashlib import md5

from django.conf import settings
from django.db import models, transaction
from django.utils.translation import ugettext_lazy as _
import django_comments
from django_comments import Comment
from django.db.models.signals import post_save, pre_save, pre_delete
from django_comments.models import BaseCommentAbstractModel
from django_comments.managers import CommentManager
from django.contrib.contenttypes.generic import GenericRelation
from django.contrib.sites.models import get_current_site
from django.contrib.auth import get_user_model
from django.core.mail import send_mail
from django.db import connection
from django.dispatch import receiver
from django_comments import signals
from django.template import RequestContext
from django.template.loader import render_to_string
from fluent_comments import appsettings
from fluent_comments import signals as fluent_signals

COMMENT_MAX_LENGTH = getattr(settings, 'COMMENT_MAX_LENGTH', 3000)

class FluentCommentManager(CommentManager):
    """
    Manager to optimize SQL queries for comments.
    """
    def get_queryset(self):
        return super(CommentManager, self).get_queryset().order_by('-created_time')

    def rebuild(self):
        self.update(tree=None)
        comments = list(self.filter(parent=None))
        while True:
            if comments:
                for comment in comments:
                    comment.save()
                parent = comments
                comments = list(self.filter(parent__in=parent))
            else:
                break

    #    self.model.objects.update(tree=None)
    #    for comment in self.iterator():
    #        comment.parent_save_first()

    # def comment_get_or_create(self, *args, **kwargs):
    #     try:
    #         comment = self.get(*args, **kwargs)
    #     except self.model.DoesNotExist:
    #         comment = self.model(*args, **kwargs)
    #         comment.comment_save()
    #         created = True
    #     else:
    #         created = False
    #     return comment, created


class MpttTreeLock(models.Model):
    unique_key = models.CharField(max_length=100)
    md5_key = models.CharField(max_length=32, unique=True)


@receiver(pre_save, sender=MpttTreeLock)
def build_unique_key_md5(sender, instance, **kwargs):
    if not instance.unique_key:
        md5_key = md5(instance.unique_key).hexdigest()
        instance.md5_key = md5_key

class MpttTree(models.Model):
    pass


class MPTT(models.Model):
    left = models.PositiveIntegerField()
    right = models.PositiveIntegerField()
    tree_id = models.ForeignKey(MpttTree)


class FluentComment(BaseCommentAbstractModel):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, verbose_name=_('user'), related_name="%(class)s_comments")
    created_time = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_time = models.DateTimeField(auto_now=True)
    comment = models.TextField(_('comment'), max_length=COMMENT_MAX_LENGTH)
    is_anonymous = models.BooleanField(default=False)
    parent = models.ForeignKey("self", null=True, blank=True)
    left = models.PositiveIntegerField()
    right = models.PositiveIntegerField()
    tree = models.ForeignKey(MpttTree, null=True)
    floor = models.IntegerField(default=-1)
    ip_address = models.GenericIPAddressField(_('IP address'), unpack_ipv4=True, blank=True, null=True)
    is_public = models.BooleanField(_('is public'), default=True,
                    help_text=_('Uncheck this box to make the comment effectively ' \
                                'disappear from the site.'))
    is_removed = models.BooleanField(_('is removed'), default=False,
                    help_text=_('Check this box if the comment is inappropriate. ' \
                                'A "This comment has been removed" message will ' \
                                'be displayed instead.'))
    platform = models.CharField(max_length=50, null=True, blank=True)
    images = models.ManyToManyField(settings.IMAGE_MODEL, null=True)

    objects = FluentCommentManager()


    def __init__(self, *args, **kwargs):
        super(FluentComment, self).__init__(*args, **kwargs)
        if self.is_anonymous:
            setattr(self, 'realuser', self.user)
            anony_user = get_user_model().objects.get_anonymous_user()
            setattr(self, 'user', anony_user)
            setattr(self, FluentComment.user.cache_name, anony_user)


    def name(self):
        return self.is_anonymous and get_user_model().objects.get_anonymous_user().username or self.user.username



    # def comment_save(self, *args, **kwargs):
    #     with transaction.atomic():
    #         fluent_signals.comment_save_before.send(sender=self.__class__, instance=self)
    #         self.save(*args, **kwargs)
    #         fluent_signals.comment_save_after.send(sender=self.__class__, instance=self)

    def parent_save_first(self):
        parent = self.parent
        if parent:
            parent.parent_save_first()
        # self.comment_save()
        self.save()


    def save(self, *args, **kwargs):
        with transaction.atomic():
            super(FluentComment, self).save(*args, **kwargs)

    def get_ancestors(self, ascending=False, include_self=False):
        if not self.parent:
            if not include_self:
                return FluentComment.objects.none()
            else:
                # Filter on pk for efficiency.
                qs = FluentComment.objects.filter(pk=self.pk)
        else:
            order_by = '-left' if ascending else 'left'
            left = self.left
            right = self.right
            if not include_self:
                left -= 1
                right += 1

            qs = FluentComment.objects.filter(
                is_removed=False,
                is_public=True,
                left__lte=left,
                right__gte=right,
                tree=self.tree,
                site__pk=settings.SITE_ID
            )

            qs = qs.order_by(order_by)
        return qs



    class Meta:
        # ordering = ('-created_time',)
        permissions = [("can_moderate", "Can moderate comments")]
        index_together = [
            ["site", "object_pk", "content_type", "is_public", "is_removed"],
        ]


# @receiver(fluent_signals.comment_save_before, sender=FluentComment)
@receiver(pre_save, sender=FluentComment)
def save_mptt_comment(sender, instance, *args, **kwargs):
    tree = instance.tree
    if tree:
        return
    parent = instance.parent
    if parent:
        right = parent.right
        tree = parent.tree
        tree_id = tree.id
        lock = MpttTreeLock(unique_key="tree_id={}".format(tree_id))
        lock.save()
        instance.left = right
        instance.right = right + 1
        instance.tree = tree
        table = sender._meta.db_table
        cursor = connection.cursor()
        cursor.execute("""
        UPDATE "{0}"
	            SET "left" = CASE
	                    WHEN "left" >= {1}
	                        THEN "left" + 2
	                    ELSE "left" END,
	                "right" = CASE
	                    WHEN "right" >= {1}
	                        THEN "right" + 2
	                    ELSE "right" END
	            WHERE "tree_id" = {2}""".format(table, right, tree_id))
        # sender.objects.filter(tree_id=tree_id).filter(right__gte=right).update(right=F('right') + 2)
        # sender.objects.filter(tree_id=tree_id).filter(left__gte=right).update(left=F('left') + 2)
    else:
        tree = MpttTree()
        tree.save()
        instance.left = 1
        instance.right = 2
        instance.tree = tree

# @receiver(fluent_signals.comment_save_after, sender=FluentComment)
@receiver(post_save, sender=FluentComment)
def save_mptt_comment_release_lock(sender, instance, *args, **kwargs):
    parent = instance.parent
    if parent:
        tree_id = parent.tree.id
        MpttTreeLock.objects.filter(unique_key="tree_id={}".format(tree_id)).delete()

# @receiver(fluent.signals.comment_will_be_removed)
# def remove_mptt_comment(sender, comment, request, **kwargs):
#     tree_id = comment.tree_id
#     left = comment.left
#     right = comment.right
#     lock = MpttTreeLock(unique_key="tree_id={}".format(tree_id))
#     lock.save()
#     table = sender._meta.db_table
#     cursor = connection.cursor()
#     cursor.execute("""
#     UPDATE "{0}"
# 	        SET "left" = CASE
# 	                WHEN "left" > {1}
# 	                    THEN "left" - 2
# 	                ELSE "left" END,
# 	            "right" = CASE
# 	                WHEN "right" >= {2}
# 	                    THEN "right" + 2
# 	                ELSE "right" END
# 	        WHERE "tree_id" = {3}""".format(table, left, right, tree_id))
#     lock.delete()



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
    qs = django_comments.get_model().objects.for_model(content_object).filter(site_id=settings.SITE_ID)

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
