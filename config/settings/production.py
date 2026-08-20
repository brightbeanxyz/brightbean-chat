import os

# Set before importing base, not after. base.py decides at import time whether
# to enforce the secret/host checks, and it decides off the environment's DEBUG
# value — so a stray DEBUG=true (which .env.example ships, and `make setup`
# copies) would send it down the development path: the hardcoded, repo-public
# SECRET_KEY and ENCRYPTION_KEY_SALT, no ALLOWED_HOSTS check, and then this
# module quietly setting DEBUG=False again afterwards. The result boots looking
# healthy while signing every token and encrypting every credential under a key
# anyone can read out of the repository.
#
# Forced rather than setdefault: this module *is* production. DEBUG=True here
# is never something the environment gets to ask for.
os.environ["DEBUG"] = "false"

from .base import *  # noqa: E402, F401, F403

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
