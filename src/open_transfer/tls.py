"""``--https``: a self-signed certificate so browsers reach the app over TLS.

A browser's own hop to its app can't use ot-file/1: a page on
``http://<LAN address>`` isn't a secure context (no WebCrypto) and the
browser shares no key with the app. TLS gives that hop ephemeral (ECDHE)
session keys. There is no CA for a LAN address, so browsers warn once; the
owner's page shows the certificate's SHA-256 fingerprint to compare.

The certificate is kept so a browser's "proceed" keeps holding; its key is
sealed in the vault (:mod:`open_transfer.keystore`) in ``<state>/tls.json``.
"""

from __future__ import annotations

import datetime
import hashlib
import ipaddress
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from open_transfer import network
from open_transfer.devices import _read_json, write_private
from open_transfer.keystore import Locked, Vault, open_vault

log = logging.getLogger("open_transfer")

VALID_DAYS = 397  # the most browsers accept for a leaf certificate
RENEW_DAYS = 30


def certificate(state_dir: Path, vault: Vault | None = None) -> tuple[bytes, bytes]:
    """(certificate PEM, private key PEM): the kept one, or a new one."""
    vault = vault or open_vault(state_dir)
    path = state_dir / "tls.json"
    saved = _read_json(path)
    try:
        cert_pem = str(saved["cert"]).encode()
        cert = x509.load_pem_x509_certificate(cert_pem)
        left = cert.not_valid_after_utc - datetime.datetime.now(datetime.timezone.utc)
        if left > datetime.timedelta(days=RENEW_DAYS):
            return cert_pem, vault.open(str(saved["key"]), "tls")
    except (KeyError, ValueError, Locked):
        pass  # none yet, damaged, expiring or locked: a new one (browsers warn again)
    cert_pem, key_pem = _new_certificate()
    try:
        write_private(path, {"cert": cert_pem.decode(), "key": vault.seal(key_pem, "tls")})
    except OSError:
        log.debug("could not save the HTTPS certificate", exc_info=True)
    return cert_pem, key_pem


def _new_certificate() -> tuple[bytes, bytes]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, f"Open Transfer on {network.hostname()}")]
    )
    ips = {"127.0.0.1", "::1", *network.lan_ips()}
    alt: list[x509.GeneralName] = [x509.DNSName("localhost"), x509.DNSName(network.hostname())]
    alt += [x509.IPAddress(ipaddress.ip_address(ip)) for ip in sorted(ips)]
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=VALID_DAYS))
        .add_extension(x509.SubjectAlternativeName(alt), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return cert.public_bytes(serialization.Encoding.PEM), key_pem


def fingerprint(cert_pem: bytes) -> str:
    der = x509.load_pem_x509_certificate(cert_pem).public_bytes(serialization.Encoding.DER)
    return ":".join(f"{b:02X}" for b in hashlib.sha256(der).digest())


def ssl_adapter(cert_pem: bytes, key_pem: bytes) -> Any:
    """cheroot's TLS adapter. Python's ssl only loads keys from files, so the key is
    written to an owner-only temporary file for the moment it's read, then deleted."""
    from cheroot.ssl.builtin import BuiltinSSLAdapter

    with tempfile.TemporaryDirectory(prefix="ot-tls-") as folder:
        cert_path, key_path = os.path.join(folder, "cert.pem"), os.path.join(folder, "key.pem")
        for path, data in ((cert_path, cert_pem), (key_path, key_pem)):
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
        return BuiltinSSLAdapter(cert_path, key_path)
