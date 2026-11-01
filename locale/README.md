# Translations

`*/LC_MESSAGES/django.po` in here are generated, not hand-created:

```
make i18n-extract   # pulls new/changed strings from templates/ and apps/*.py
make i18n-compile    # compiles the .po files this directory holds into .mo
```

Edit the `.po` files' `msgstr` entries to translate; `{placeholder}` tokens
inside a `msgid` are Python `str.format()` slots (from
`apps.notifications.events`'s copy templates) and must survive into the
translation unchanged.

See `config/settings/base.py`'s `LANGUAGES` for which locales are wired up.
