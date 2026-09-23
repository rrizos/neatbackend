"""What a link opening looks like when nobody opened it.

Only the backfill uses this. New rows settle the question a better way — the
page itself says when a browser rendered it (see `confirm_open` in views.py) —
but the rows recorded before that existed have nothing to go on except what
they were fetched with, so this is the best that can be said about them.

The numbers that prompted it: one link shared once on 2026-09-23 recorded
fourteen openings within eleven minutes. Six announced themselves as
`facebookexternalhit`; five more came from Meta's own address range wearing an
iPhone user agent, which is the other way Facebook fetches a preview; one was
a datacenter address claiming to be Chrome on Windows. Two were people.
"""

import ipaddress

#: Substrings that appear in the user agent of something that is not a reader.
#: Matched case-insensitively. `bot` alone covers Googlebot, Twitterbot,
#: LinkedInBot, PetalBot and the rest of the family.
CRAWLER_HINTS = (
    'bot', 'crawler', 'spider', 'facebookexternalhit', 'whatsapp', 'telegram',
    'slack', 'discord', 'skypeuripreview', 'embedly', 'quora link preview',
    'pinterest', 'preview', 'curl', 'wget', 'python-requests', 'go-http-client',
    'libwww', 'scrapy', 'headlesschrome', 'phantomjs', 'java/', 'axios',
)

#: Meta's published ranges (AS32934). Their preview fetcher does not always
#: say what it is, so the address is the only thing that gives it away.
META_RANGES = tuple(ipaddress.ip_network(n) for n in (
    '31.13.24.0/21', '31.13.64.0/18', '45.64.40.0/22', '66.220.144.0/20',
    '69.63.176.0/20', '69.171.224.0/19', '74.119.76.0/22', '102.132.96.0/20',
    '103.4.96.0/22', '129.134.0.0/16', '157.240.0.0/16', '163.114.128.0/17',
    '173.252.64.0/18', '179.60.192.0/22', '185.60.216.0/22', '185.89.216.0/22',
    '204.15.20.0/22',
))


def looks_automated(user_agent, ip_address):
    """True when this fetch was made by something that cannot install an app."""
    ua = (user_agent or '').lower()
    if not ua:
        # Every browser sends one. Something that does not is not a browser.
        return True
    if any(hint in ua for hint in CRAWLER_HINTS):
        return True
    try:
        address = ipaddress.ip_address((ip_address or '').strip())
    except ValueError:
        return False
    return any(address in network for network in META_RANGES)
