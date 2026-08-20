from .base import *  # noqa: F401, F403

DEBUG = False

# Security. Header enforcement is duplicated at the proxy (Caddy) in issue #28;
# setting it here too means a deployment without that proxy is still hardened.
SECURE_SSL_REDIRECT = True
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
X_FRAME_OPTIONS = "DENY"

# Load balancers and uptime probes hit /healthz over plain HTTP inside the
# private network; without this exemption they would only ever see the 301.
SECURE_REDIRECT_EXEMPT = [r"^healthz$"]
