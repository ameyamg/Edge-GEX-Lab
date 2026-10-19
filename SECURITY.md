# Security Policy

## Supported Versions

Edge GEX Lab is an actively developed open-source project.

Security fixes are generally applied to the latest released version and the current `main` branch.

| Version               | Supported |
| --------------------- | --------- |
| Latest release        | Yes       |
| Current `main` branch | Yes       |
| Older releases        | No        |

## Reporting a Vulnerability

If you discover a security vulnerability in Edge GEX Lab, please **do not open a public GitHub issue**.

Instead, please use GitHub's **private vulnerability reporting** feature from the Security section of this repository.

Please include, when possible:

- A description of the vulnerability
- Steps to reproduce the issue
- The potential impact
- Any affected files or components
- Suggested fixes, if you have any

Please avoid publicly disclosing the vulnerability until it has been reviewed and, when necessary, a fix has been released.

## Security-Sensitive Areas

Edge GEX Lab works with potentially sensitive information including:

- Alpaca API keys in `.env`
- The local `claude` CLI call behind "Explain this map" (it must stay tool-free and keyless)
- Network calls to Alpaca and Cboe (REST only; the app must never place an order or open a websocket)

Reports involving exposure of this information, unauthorized access, credential handling, unexpected network communication, or unsafe file handling are particularly important.

## Data and Credentials

Edge GEX Lab is designed as a local-first application. The server binds to `127.0.0.1` and has no authentication, so it must not be exposed to a network.

Users should never commit API keys, `.env` files, local caches, or other private data to the repository.

API credentials should only be stored using the configuration methods documented by the project.

## Responsible Disclosure

Please allow reasonable time to investigate and address a reported vulnerability before publicly discussing it.

Security researchers who responsibly report valid vulnerabilities are appreciated and may be credited in the corresponding release notes or security advisory if they wish.
