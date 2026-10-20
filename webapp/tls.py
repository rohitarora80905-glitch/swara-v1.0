"""
A self-signed certificate for the console's HTTPS listener.

Browsers allow microphone access only on secure origins (HTTPS) or on
localhost, so a phone or a second laptop that opens the console over the LAN
must do so over HTTPS.  The certificate is generated once into runtime/tls/
and regenerated when this computer's address changes.  Browsers still show a
one-time warning because nobody vouches for the certificate; that is expected.
"""
from __future__ import annotations

import datetime
import ipaddress
import os
import socket


def local_ipv4s() -> set[str]:
    ips = {"127.0.0.1"}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except socket.gaierror:
        pass
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
    except OSError:
        pass
    finally:
        s.close()
    return ips


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _cert_covers(cert_path: str, ips: set[str]) -> bool:
    try:
        from cryptography import x509
        with open(cert_path, "rb") as f:
            cert = x509.load_pem_x509_certificate(f.read())
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        have = {str(ip) for ip in san.get_values_for_type(x509.IPAddress)}
        return ips <= have and cert.not_valid_after_utc > datetime.datetime.now(datetime.timezone.utc)
    except Exception:
        return False


def ensure_cert(dirpath: str) -> tuple[str, str]:
    """Return (certificate, key) PEM paths, generating them if needed."""
    cert_path, key_path = os.path.join(dirpath, "console.crt"), os.path.join(dirpath, "console.key")
    ips = local_ipv4s()
    if os.path.exists(cert_path) and os.path.exists(key_path) and _cert_covers(cert_path, ips):
        return cert_path, key_path

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    os.makedirs(dirpath, exist_ok=True)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "SWARA console"),
                      x509.NameAttribute(NameOID.ORGANIZATION_NAME, "SWARA")])
    sans: list = [x509.DNSName("localhost")]
    try:
        sans.append(x509.DNSName(socket.gethostname()))
    except Exception:
        pass
    sans += [x509.IPAddress(ipaddress.IPv4Address(ip)) for ip in sorted(ips)]
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName(sans), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    with open(key_path, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()))
    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    return cert_path, key_path
