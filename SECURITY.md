# Security policy

## Supported versions

Security fixes are released for the latest minor version (currently **2.0.x**).

## Reporting a vulnerability

Please **do not open a public issue**. Report privately through
[GitHub Security Advisories](https://github.com/itsonu/open-transfer/security/advisories/new)
("Report a vulnerability").

Include what you found, how to reproduce it, and the impact you expect. You'll get an acknowledgement within **3 working days** and a plan or fix timeline within **10 working days**. We'll credit you in the release notes unless you prefer otherwise.

## Scope

In scope: anything that lets someone read, write or delete files they shouldn't, bypass the PIN, escape the shared folder, run script in the app's origin, or crash the server remotely.

Out of scope — by design, and documented in the README:

- Anyone on the network can use an instance started **without** a PIN.
- Browser traffic is unencrypted HTTP unless you add an HTTPS reverse proxy. Files between **paired apps** are encrypted end to end; transfers with unpaired or older apps are plain HTTP and labelled so ([docs/encryption.md](docs/encryption.md)).
- Brute-forcing a 4-digit PIN over a long time (5 attempts/min per address); use a longer alphanumeric PIN for exposed setups.

## Hardening already in place

Path sanitisation and containment checks, CSRF (Origin / `Sec-Fetch-Site`) and DNS-rebinding (Host allow-list) protection, rate-limited constant-time PIN checks, signed `HttpOnly` sessions, strict CSP and security headers, sandboxed attachment-only downloads, CodeQL and `pip-audit` in CI. Details: [docs/architecture.md](docs/architecture.md#security-model).
