"""/stoplocked — one page, one button, no release required."""

from django.contrib.auth import get_user_model
from django.shortcuts import redirect, render
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods

from accounts.ratelimit import client_ip, rate_limited

from . import switch

User = get_user_model()

#: Typed into the confirmation box before the feature can be turned off. A
#: button this consequential should not be reachable by a mis-tap on a phone.
CONFIRM_WORD = 'STOP'


@csrf_protect
@require_http_methods(['GET', 'POST'])
def stop_locked(request):
    from django.contrib.auth import authenticate

    from accounts.serializers import ensure_profile
    from web.views import ANALYTICS_SESSION_KEY, _analytics_admin

    error = ''

    if request.method == 'POST' and not _analytics_admin(request):
        if rate_limited(f'stoplocked:{client_ip(request)}', limit=8, window_seconds=900):
            error = 'Too many attempts. Try again later.'
        else:
            user = authenticate(
                username=(request.POST.get('username') or '').strip(),
                password=request.POST.get('password') or '',
            )
            if user is not None and ensure_profile(user).is_admin:
                request.session[ANALYTICS_SESSION_KEY] = True
                request.session['neat_stoplocked_admin'] = user.username
                request.session.set_expiry(60 * 60 * 8)
                return redirect('stop_locked')
            error = 'Those details are not valid here.'

    if not _analytics_admin(request):
        return render(request, 'lockedcities/login.html', {'error': error}, status=200)

    if request.GET.get('logout'):
        request.session.pop(ANALYTICS_SESSION_KEY, None)
        return redirect('stop_locked')

    if request.method == 'POST':
        admin = User.objects.filter(
            username=request.session.get('neat_stoplocked_admin', ''),
        ).first()
        action = request.POST.get('action')
        if action == 'off':
            if (request.POST.get('confirm') or '').strip().upper() != CONFIRM_WORD:
                error = f'Type {CONFIRM_WORD} to confirm.'
            else:
                switch.turn_off(admin)
                return redirect('stop_locked')
        elif action == 'on':
            switch.turn_on(admin)
            return redirect('stop_locked')

    context = switch.status()
    context['error'] = error
    context['confirm_word'] = CONFIRM_WORD
    return render(request, 'lockedcities/switch.html', context)
