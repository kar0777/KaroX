# Puter

Puter is selectable in the interactive provider picker, but it does not open
the ordinary API-key form. Its official contract uses `puter.ai.chat()` and
`puter.ai.listModels()` with browser user authentication and the user-pays
model. It is not an OpenAI-compatible endpoint and has no API key to paste.

KaroX therefore shows an explicit contract screen instead of silently skipping
Puter or inventing a Base URL. A native browser-authenticated Puter bridge is
still required before Puter can be activated as the CLI's model provider.

Official contract references:

- <https://docs.puter.com/AI/chat/>
- <https://docs.puter.com/AI/listModels/>
- <https://docs.puter.com/user-pays-model/>
