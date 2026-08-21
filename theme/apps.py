from django.apps import AppConfig


class ThemeConfig(AppConfig):
    """Holds the compiled Tailwind bundle so the app-directories static finder serves it.

    The app is deliberately empty otherwise: no models, no views, no
    ``django-tailwind`` integration. ``theme/static/css/dist/styles.css`` is a
    build artefact produced by the root ``npm run build:css`` script, and being
    an installed app is the only reason ``{% static 'css/dist/styles.css' %}``
    resolves — ``STATICFILES_DIRS`` does not cover it.
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "theme"
