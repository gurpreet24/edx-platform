"""
Django Celery tasks for service status app
"""

import logging
from smtplib import SMTPException

import requests
import simplejson
from celery import Task, task
from celery.exceptions import MaxRetriesExceededError
from django.conf import settings
from django.core.mail import send_mail

from edxmako.shortcuts import render_to_string
from openedx.core.djangoapps.site_configuration import helpers as configuration_helpers

ACE_ROUTING_KEY = getattr(settings, 'ACE_ROUTING_KEY', None)
TASK_LOG = logging.getLogger('edx.celery.task')
log = logging.getLogger(__name__)


class BaseSoftwareSecureTask(Task):
    """
    Base task class for use with Software Secure request.

    Permits updating information about user attempt in correspondence to submitting
    request to software secure.
    """
    abstract = True

    def on_success(self, response, task_id, args, kwargs):
        """
        Update SoftwareSecurePhotoVerification object corresponding to this
        task with info about success.

        Updates user verification attempt to "submitted" if the response was ok otherwise
        set it to "must_retry".
        """

        def log_info_on_success():
            """
            Log the information about the request and response.
            """
            headers = kwargs['headers']
            body = kwargs['body']
            receipt_id = user_verification.receipt_id
            TASK_LOG.info('Sent request to Software Secure for receipt ID %s.', receipt_id)
            log.debug("Headers:\n{}\n\n".format(headers))
            log.debug("Body:\n{}\n\n".format(body))
            log.debug(u"Return code: {}".format(response.status_code))
            log.debug(u"Return message:\n\n{}\n\n".format(response.text))

        def update_user_verification():
            """
            Updates user verification attempt to the relevant status.
            """
            if response.ok:
                return user_verification.mark_submit()
            user_verification.mark_must_retry(response.text)

        user_verification = kwargs['user_verification']
        update_user_verification()
        log_info_on_success()


@task(routing_key=ACE_ROUTING_KEY)
def send_verification_status_email(context):
    """
    Spins a task to send verification status email to the learner
    """
    subject = context.get('subject')
    message = render_to_string(context.get('template'), context.get('email_vars'))
    from_addr = configuration_helpers.get_value(
        'email_from_address',
        settings.DEFAULT_FROM_EMAIL
    )
    dest_addr = context.get('email')

    try:
        send_mail(
            subject,
            message,
            from_addr,
            [dest_addr],
            fail_silently=False
        )
    except SMTPException:
        log.warning(u"Failure in sending verification status e-mail to %s", dest_addr)


@task(
    base=BaseSoftwareSecureTask,
    bind=True,
    default_retry_delay=settings.SOFTWARE_SECURE_REQUEST_RETRY_DELAY,
    max_retries=settings.SOFTWARE_SECURE_RETRY_MAX_ATTEMPTS,
    routing_key=settings.SOFTWARE_SECURE_VERIFICATION_ROUTING_KEY,
)
def send_request_to_ss_for_user(self, user_verification, headers, body):
    """
    Assembles a submission to Software Secure.

    Keyword Arguments:
        user_verification SoftwareSecurePhotoVerification model object.
        headers (dict): headers to send with software secure request.
        body (dict): request body for software secure.
    Returns:
        request.Response
    """
    retries = self.request.retries
    try:
        response = requests.post(
            settings.VERIFY_STUDENT["SOFTWARE_SECURE"]["API_URL"],
            headers=headers,
            data=simplejson.dumps(body, indent=2, sort_keys=True, ensure_ascii=False).encode('utf-8'),
            verify=False
        )
        return response
    except Exception:  # pylint: disable=bare-except
        log.error(
            (
                'Retrying sending request to Software Secure for user: %s, Receipt ID: %s '
                'attempt#: %s of %s'
            ),
            user_verification.user,
            retries,
            settings.SOFTWARE_SECURE_RETRY_MAX_ATTEMPTS,
        )
        try:
            self.retry()
        except MaxRetriesExceededError:
            user_verification.mark_must_retry()
            TASK_LOG.error(
                'Software Secure submission failed for user %s, setting status to must_retry',
                user_verification.user.username,
                exc_info=True
            )
