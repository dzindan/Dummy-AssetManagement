"""HTTPS for the central server: a self-signed certificate made on the
server itself (`AssetManagementTool.exe make-cert`), so sign-ins, session
cookies and the data never cross the network in clear text.

Writes, next to server.json (%ProgramData%\\AssetManagementTool\\tls\\):
  server.crt / server.key   PEM, what cheroot serves with (the key readable
                            by Administrators / SYSTEM / the service account only)
  AssetManagementTool.cer   the certificate alone, for the clients' "Trusted
                            Root Certification Authorities" (GPO, or
                            certutil -addstore Root on each PC) - without it
                            browsers warn "Not secure" on every visit.

The certificate names the server the way people type it: its short name,
its full DNS name, its IP addresses, localhost.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import os
import socket
import subprocess

VALID_DAYS = 5 * 365
CERT_NAME = "server.crt"
KEY_NAME = "server.key"
CER_NAME = "AssetManagementTool.cer"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def default_names() -> list[str]:
    """This server's short name, full DNS name and IPv4 addresses (+ localhost)."""
    names = []
    host = socket.gethostname()
    for name in (host, socket.getfqdn(), "localhost"):
        if name and name.lower() not in (n.lower() for n in names):
            names.append(name)
    try:
        ips = {info[4][0] for info in socket.getaddrinfo(host, None, socket.AF_INET)}
    except OSError:
        ips = set()
    for ip in sorted(ips | {"127.0.0.1"}):
        names.append(ip)
    return names


def _san(names: list[str]):
    from cryptography import x509

    out = []
    for name in names:
        try:
            out.append(x509.IPAddress(ipaddress.ip_address(name)))
        except ValueError:
            out.append(x509.DNSName(name))
    return out


def make_cert(out_dir: str, names: list[str] | None = None, days: int = VALID_DAYS) -> dict:
    """Make the key + self-signed certificate. -> {cert, key, cer, names,
    sha256, not_after}. An earlier pair in out_dir is kept as *.old."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    names = [n.strip() for n in (names or default_names()) if n and n.strip()]
    if not names:
        raise ValueError("No server name for the certificate")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0]),
                         x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Asset Management Tool")])
    now = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + dt.timedelta(days=days))
        .add_extension(x509.SubjectAlternativeName(_san(names)), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(digital_signature=True, key_encipherment=True, content_commitment=False,
                                     data_encipherment=False, key_agreement=False, key_cert_sign=False,
                                     crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    os.makedirs(out_dir, exist_ok=True)
    paths = {"cert": os.path.join(out_dir, CERT_NAME), "key": os.path.join(out_dir, KEY_NAME),
             "cer": os.path.join(out_dir, CER_NAME)}
    for path in paths.values():
        if os.path.exists(path):
            os.replace(path, path + ".old")
    with open(paths["key"], "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()))
    restrict_key(paths["key"])
    with open(paths["cert"], "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(paths["cer"], "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.DER))
    return {**paths, "names": names, "sha256": cert.fingerprint(hashes.SHA256()).hex(),
            "not_after": cert.not_valid_after_utc.date().isoformat()}


def _icacls(path: str, *args: str) -> bool:
    if os.name != "nt":
        return False
    proc = subprocess.run(["icacls", path, *args], capture_output=True, text=True, creationflags=_NO_WINDOW)
    return proc.returncode == 0


def restrict_key(path: str) -> bool:
    """The private key: Administrators, SYSTEM (SIDs, so a non-English
    Windows works too) and whoever made it; the service account is added by
    grant_key()."""
    if os.name != "nt":
        os.chmod(path, 0o600)
        return True
    me = os.environ.get("USERNAME", "")
    domain = os.environ.get("USERDOMAIN", "")
    who = f"{domain}\\{me}" if domain else me
    grants = ["*S-1-5-32-544:F", "*S-1-5-18:F"] + ([f"{who}:F"] if me else [])
    return _icacls(path, "/inheritance:r", "/grant:r", *grants)


def grant_key(path: str, user: str) -> bool:
    """install-task: the account the server runs as may read the key."""
    return bool(path) and os.path.exists(path) and _icacls(path, "/grant", f"{user}:R")


def cert_info(cert_file: str) -> dict:
    """What Settings / the console says about the certificate in use."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes

    with open(cert_file, "rb") as f:
        cert = x509.load_pem_x509_certificate(f.read())
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        names = [str(v) for v in san.get_values_for_type(x509.DNSName)] + \
                [str(v) for v in san.get_values_for_type(x509.IPAddress)]
    except x509.ExtensionNotFound:
        names = []
    return {"names": names, "sha256": cert.fingerprint(hashes.SHA256()).hex(),
            "not_after": cert.not_valid_after_utc.date().isoformat(),
            "self_signed": cert.issuer == cert.subject}
