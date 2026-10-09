# Security Policy

## Supported versions

Security fixes are applied to the latest commit on `main`. This project does
not currently maintain older release branches.

## Reporting a vulnerability

Please do not open a public issue for a suspected vulnerability or credential
exposure. Use GitHub's private vulnerability reporting instead:

<https://github.com/ml451953014/llm-transit-gateway/security/advisories/new>

Include the affected endpoint or component, reproduction steps, impact, and a
minimal proof of concept when possible. Remove API keys, service-account JSON,
tokens, request contents, and other sensitive data before submitting a report.

You should receive an acknowledgement within seven days. A fix and disclosure
timeline will be coordinated according to severity and exploitability.

## Deployment boundary

LLM Transit Gateway is local-first and binds to `127.0.0.1` by default. Its
inference API only checks that a Bearer token is present; it is not a complete
authentication system. Do not expose the gateway directly to the public
Internet. Put an authenticated reverse proxy in front of it when remote access
is required.
