# -*- mode: python ; coding: utf-8 -*-
"""Use the shared legacy recipe with the validated, source-built Vista runtime."""
VISTA_BUILD = True
exec(compile(open('Druta-win7.spec', encoding='utf-8').read(), 'Druta-win7.spec', 'exec'))
