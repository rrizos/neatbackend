"""Tell every install to download the newest Shorebird patch, now.

Run straight after `shorebird patch android` / `shorebird patch ios`. Without
it the patch is only found when an app next checks on its own, which for iOS
means the user's first launch downloads it and their *second* one runs it.
"""

from django.core.management.base import BaseCommand

from push.senders import send_code_push


class Command(BaseCommand):
    help = 'Send the silent push that makes installs fetch a new Shorebird patch.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--platform',
            choices=['ios', 'android'],
            help='Only wake one platform. Defaults to both.',
        )

    def handle(self, *args, **options):
        sent, failed = send_code_push(platform=options.get('platform'))
        self.stdout.write(f'woken: {sent}, failed: {failed}')
